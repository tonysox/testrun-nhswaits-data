#!/usr/bin/env python3
"""NHS RTT waiting-times ingest pipeline (testrun — playbook validation exercise).

Implements STANDARD-data-pipeline.md:
  fetch -> archive raw ALWAYS -> parse (schema fingerprint) -> quality gates -> publish.
Raw layer DEVIATION: GitHub Release assets instead of R2 (token lacks R2 scope, testrun D-002).

Subcommands:
  fetch    discover latest Full-CSV extract on the NHS landing page, download,
           checksum-dedupe against state/raw_checksums.txt, write sidecar JSON.
           Emits GITHUB_OUTPUT: changed=true|false, tag=<release tag>.
  process  (only when changed) parse + normalise + gates + write publish files.
           Hard-fails BEFORE any publish file is written on fingerprint/gate breach.

Env:
  DRILL=true   corrupted-file drill: force-changed, corrupt the extracted CSV
               (rename a column + truncate rows) before parse; gates must block.
  WORK_DIR     scratch dir (default ./work)
"""
import csv
import hashlib
import io
import json
import os
import re
import sys
import urllib.request
import zipfile
from datetime import date, datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.environ.get("WORK_DIR", os.path.join(REPO_ROOT, "work"))
STATE = os.path.join(REPO_ROOT, "state")
NORM_DIR = os.path.join(REPO_ROOT, "data", "normalised")
DIFF_DIR = os.path.join(REPO_ROOT, "data", "diffs")
NORM_CSV = os.path.join(NORM_DIR, "rtt_trust_specialty.csv")
SUMMARY_JSON = os.path.join(NORM_DIR, "summary.json")
UA = "testrun-nhswaits-data pipeline (playbook validation; contact: github.com/tonysox)"
# DRILL modes: "" (off) | "full" (rename column + truncate -> fingerprint gate)
#              | "truncate" (truncate only -> row-count gate)
DRILL_MODE = os.environ.get("DRILL", "").lower().replace("true", "full")
DRILL = DRILL_MODE in ("full", "truncate")

MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}

# Key source columns accessed BY NAME, never by position (standard §4).
KEY_COLS = ["Period", "Provider Org Code", "Provider Org Name",
            "Treatment Function Code", "Treatment Function Name",
            "RTT Part Description", "Total", "Total All"]


def log(msg):
    print(f"[pipeline] {msg}", flush=True)


def fail(msg):
    print(f"::error::{msg}", flush=True)
    print(f"[pipeline] HARD FAIL: {msg}", flush=True)
    sys.exit(1)


def gh_output(key, value):
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"{key}={value}\n")
    log(f"output {key}={value}")


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=120)


# ---------------------------------------------------------------- discovery
def landing_pages():
    """Per-financial-year URLs (new URL every April) — follow the landing page,
    never hardcode file URLs (standard §4)."""
    today = date.today()
    fy_start = today.year if today.month >= 4 else today.year - 1
    pages = []
    for y in (fy_start, fy_start - 1):
        pages.append("https://www.england.nhs.uk/statistics/statistical-work-areas/"
                     f"rtt-waiting-times/rtt-data-{y}-{(y + 1) % 100:02d}/")
    return pages


def discover_latest():
    link_re = re.compile(
        r'href="(https://www\.england\.nhs\.uk/statistics/wp-content/[^"]*'
        r'Full-CSV-data-file-([A-Z][a-z]{2})(\d{2})[^"]*\.zip)"')
    found = []
    for page in landing_pages():
        try:
            html = http_get(page).read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            log(f"landing page {page} not reachable ({e}) — trying previous FY")
            continue
        for url, mon, yy in link_re.findall(html):
            if mon in MONTHS:
                found.append((2000 + int(yy), MONTHS[mon], url))
        if found:
            break
    if not found:
        fail("discovery: no Full-CSV-data-file link found on any RTT landing page "
             "(source-change contingency: check URL pattern / page layout)")
    found.sort()
    y, m, url = found[-1]
    log(f"discovered latest extract: {y}-{m:02d} -> {url}")
    return url


# ---------------------------------------------------------------- fetch
def cmd_fetch():
    os.makedirs(WORK, exist_ok=True)
    url = discover_latest()
    resp = http_get(url)
    blob = resp.read()
    headers = {k: v for k, v in resp.headers.items()
               if k.lower() in ("last-modified", "etag", "content-type", "content-length")}
    sha = hashlib.sha256(blob).hexdigest()
    log(f"fetched {len(blob)} bytes sha256={sha}")

    seen = set()
    ck_path = os.path.join(STATE, "raw_checksums.txt")
    if os.path.exists(ck_path):
        seen = {l.strip().split()[0] for l in open(ck_path) if l.strip()}

    if sha in seen and not DRILL:
        log("checksum already archived — source unchanged, NO-OP (dedupe by checksum)")
        gh_output("changed", "false")
        return

    raw_path = os.path.join(WORK, "raw.zip")
    with open(raw_path, "wb") as f:
        f.write(blob)
    sidecar = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source_url": url,
        "sha256": sha,
        "http_headers": headers,
        "drill": DRILL,
    }
    with open(os.path.join(WORK, "sidecar.json"), "w") as f:
        json.dump(sidecar, f, indent=2)

    stamp = date.today().isoformat()
    tag = f"drill-raw-{os.environ.get('GITHUB_RUN_ID', 'local')}" if DRILL else f"raw-{stamp}"
    with open(os.path.join(WORK, "meta.json"), "w") as f:
        json.dump({"sha256": sha, "tag": tag, "source_url": url}, f)
    gh_output("changed", "true")
    gh_output("tag", tag)


# ---------------------------------------------------------------- parse helpers
def infer_dtype(values):
    saw_float = False
    saw_any = False
    for v in values:
        v = v.strip()
        if v == "":
            continue
        saw_any = True
        try:
            int(v)
        except ValueError:
            try:
                float(v)
                saw_float = True
            except ValueError:
                return "str"
    if not saw_any:
        return "empty"
    return "float" if saw_float else "int"


def schema_fingerprint(header, sample_rows):
    cols = []
    for i, name in enumerate(header):
        dtype = infer_dtype([r[i] for r in sample_rows if i < len(r)])
        cols.append(f"{name}:{dtype}")
    return hashlib.sha256("\n".join(cols).encode()).hexdigest(), cols


def band_upper(colname):
    m = re.match(r"Gt (\d+) To (\d+) Weeks SUM 1$", colname)
    if m:
        return int(m.group(2))
    if re.match(r"Gt (\d+) Weeks SUM 1$", colname):
        return 999
    return None


def num(v):
    v = (v or "").strip()
    if v == "":
        return 0.0
    return float(v)


def median_from_bands(band_counts):
    """band_counts: list of (upper_week, count) ascending. Linear interpolation."""
    total = sum(c for _, c in band_counts)
    if total <= 0:
        return ""
    half = total / 2.0
    cum = 0.0
    prev_upper = 0
    for upper, cnt in band_counts:
        if cum + cnt >= half and cnt > 0:
            lo = prev_upper
            hi = upper if upper != 999 else prev_upper + 1
            frac = (half - cum) / cnt
            return f"{lo + frac * (hi - lo):.1f}"
        cum += cnt
        prev_upper = upper if upper != 999 else prev_upper
    return ""


# ---------------------------------------------------------------- process
def corrupt_for_drill(csv_path):
    """Deliberately broken file: renamed column + truncated rows (drill spec).
    mode 'truncate' keeps the header intact so the row-count gate (not the
    fingerprint gate) is the one that must block publication."""
    log(f"DRILL MODE ({DRILL_MODE}): corrupting extracted CSV")
    tmp = csv_path + ".corrupt"
    with open(csv_path, newline="", encoding="utf-8-sig") as fin, \
            open(tmp, "w", newline="") as fout:
        r = csv.reader(fin)
        w = csv.writer(fout)
        header = next(r)
        if DRILL_MODE == "full":
            header = ["Prov Code (renamed by drill)" if h == "Provider Org Code" else h
                      for h in header]
        w.writerow(header)
        for i, row in enumerate(r):
            if i >= 5000:  # truncate: a tiny fraction of the ~178k rows
                break
            w.writerow(row)
    os.replace(tmp, csv_path)


def cmd_process():
    meta = json.load(open(os.path.join(WORK, "meta.json")))
    with zipfile.ZipFile(os.path.join(WORK, "raw.zip")) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            fail(f"expected exactly 1 CSV inside ZIP, got {names}")
        csv_path = os.path.join(WORK, "extract.csv")
        with z.open(names[0]) as src, open(csv_path, "wb") as dst:
            dst.write(src.read())

    if DRILL:
        corrupt_for_drill(csv_path)

    # ---- read header + sample, fingerprint BEFORE full parse/publish (standard §4)
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        r = csv.reader(f)
        header = next(r)
        sample = []
        for row in r:
            sample.append(row)
            if len(sample) >= 200:
                break
    fp, cols = schema_fingerprint(header, sample)
    fp_path = os.path.join(STATE, "schema_fingerprint.txt")
    if os.path.exists(fp_path):
        stored = open(fp_path).read().strip().split()[0]
        if fp != stored:
            missing = [c for c in KEY_COLS if c not in header]
            fail("SCHEMA FINGERPRINT MISMATCH — refusing to parse/publish. "
                 f"stored={stored[:12]} got={fp[:12]}; key columns missing: {missing or 'none'}. "
                 "Inspect the raw release asset, update the parser deliberately, "
                 "then refresh state/schema_fingerprint.txt.")
        log(f"schema fingerprint OK ({fp[:12]})")
    else:
        log(f"first run: recording schema fingerprint {fp[:12]}")

    missing = [c for c in KEY_COLS if c not in header]
    if missing:
        fail(f"required columns missing: {missing}")

    idx = {c: header.index(c) for c in KEY_COLS}
    bands = [(header.index(c), band_upper(c)) for c in header if band_upper(c) is not None]
    if len(bands) < 50:
        fail(f"expected >=50 week-band columns, found {len(bands)}")

    # ---- normalise: trust x specialty for Incomplete Pathways (the waiting list)
    agg = {}
    period_raw = None
    src_rows = 0
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        r = csv.reader(f)
        next(r)
        for row in r:
            src_rows += 1
            if row[idx["RTT Part Description"]] != "Incomplete Pathways":
                continue
            period_raw = row[idx["Period"]]
            key = (row[idx["Provider Org Code"]].strip(),
                   row[idx["Provider Org Name"]].strip(),
                   row[idx["Treatment Function Code"]].strip(),
                   row[idx["Treatment Function Name"]].strip())
            a = agg.setdefault(key, {"bands": [0.0] * len(bands), "total": 0.0,
                                     "total_all": 0.0})
            for bi, (ci, _) in enumerate(bands):
                a["bands"][bi] += num(row[ci]) if ci < len(row) else 0.0
            a["total"] += num(row[idx["Total"]])
            a["total_all"] += num(row[idx["Total All"]])

    if not agg or not period_raw:
        fail("no Incomplete Pathways rows found — refusing to publish")

    m = re.match(r"RTT-([A-Za-z]+)-(\d{4})", period_raw)
    if not m or m.group(1)[:3] not in MONTHS:
        fail(f"unrecognised Period value: {period_raw!r}")
    month = f"{m.group(2)}-{MONTHS[m.group(1)[:3]]:02d}"

    uppers = [u for _, u in bands]
    new_rows = []
    for (pcode, pname, tfcode, tfname), a in sorted(agg.items()):
        within18 = sum(c for u, c in zip(uppers, a["bands"]) if u <= 18)
        pct18 = f"{100.0 * within18 / a['total']:.1f}" if a["total"] > 0 else ""
        med = median_from_bands(sorted(zip(uppers, a["bands"])))
        new_rows.append({
            "row_key": f"{month}|{pcode}|{tfcode}",
            "month": month,
            "provider_code": pcode,
            "provider_name": pname,
            "specialty_code": tfcode,
            "specialty_name": tfname,
            "waiting_list": str(int(a["total_all"])),
            "median_wait_weeks_est": med,
            "pct_within_18_weeks": pct18,
        })
    log(f"normalised {src_rows} source rows -> {len(new_rows)} trust x specialty rows "
        f"for {month}")

    # ---- quality gates (standard §4: plain asserts) — BEFORE any publish write
    hist_path = os.path.join(STATE, "ingest_history.jsonl")
    hist = []
    if os.path.exists(hist_path):
        hist = [json.loads(l) for l in open(hist_path) if l.strip()]
    if hist:
        trailing = [h["normalised_rows"] for h in hist[-6:]]
        avg = sum(trailing) / len(trailing)
        delta = (len(new_rows) - avg) / avg
        if abs(delta) > 0.20:
            fail(f"GATE row-count: {len(new_rows)} rows vs trailing avg {avg:.0f} "
                 f"({delta:+.0%}) exceeds +/-20% — publish blocked, last-good untouched")
        log(f"gate row-count OK ({delta:+.1%} vs trailing avg)")
    else:
        log("gate row-count: no history yet (first ingest) — recording baseline")

    ceilings = {"provider_code": 0.005, "specialty_code": 0.005, "waiting_list": 0.005,
                "provider_name": 0.05}
    for col, ceil in ceilings.items():
        nulls = sum(1 for r_ in new_rows if not r_[col].strip())
        rate = nulls / len(new_rows)
        if rate > ceil:
            fail(f"GATE null-rate: {col} {rate:.1%} > ceiling {ceil:.1%} — publish blocked")
    log("gate null-rates OK")

    y, mo = int(month[:4]), int(month[5:7])
    age_days = (date.today() - date(y, mo, 28)).days
    if age_days > 150:
        fail(f"GATE staleness: newest data month {month} is {age_days} days old "
             "(>150) — source has gone stale, investigate publication schedule")
    log(f"gate staleness OK (data month {month}, ~{age_days}d old)")

    bad = [r_ for r_ in new_rows
           if r_["pct_within_18_weeks"] and not 0 <= float(r_["pct_within_18_weeks"]) <= 100]
    if bad:
        fail(f"GATE sanity: {len(bad)} rows with pct_within_18_weeks outside 0..100")
    # Cross-foot: the source's own C_999 'Total' specialty rows must reconcile
    # with the sum of the individual specialties (catches silent parse errors).
    sum_total_rows = sum(int(r_["waiting_list"]) for r_ in new_rows
                         if r_["specialty_code"] == "C_999")
    sum_spec_rows = sum(int(r_["waiting_list"]) for r_ in new_rows
                        if r_["specialty_code"] != "C_999")
    if sum_total_rows and abs(sum_total_rows - sum_spec_rows) / sum_total_rows > 0.01:
        fail(f"GATE cross-foot: C_999 totals {sum_total_rows} vs specialty sum "
             f"{sum_spec_rows} differ by >1%")
    log("gate sanity + cross-foot OK")

    if DRILL:
        # Belt and braces: the drill must never publish even if the corruption
        # somehow slipped every gate above.
        fail("DRILL reached the publish stage — gates failed to block a corrupt file. "
             "This is itself a gate: drill runs never publish.")

    # ---- publish: merge with prior months, write normalised CSV + summary
    fieldnames = list(new_rows[0].keys())
    old_rows = []
    if os.path.exists(NORM_CSV):
        with open(NORM_CSV, newline="") as f:
            old_rows = [r_ for r_ in csv.DictReader(f) if r_["month"] != month]
    merged = old_rows + new_rows
    os.makedirs(NORM_DIR, exist_ok=True)
    with open(NORM_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(merged)

    months = sorted({r_["month"] for r_ in merged})
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "months_present": months,
        "latest_month": months[-1],
        "trusts": len({r_["provider_code"] for r_ in merged}),
        "specialties": len({r_["specialty_code"] for r_ in merged}),
        "rows": len(merged),
        # National total = sum of the source's own per-trust C_999 'Total' rows
        # (summing specialty rows too would double-count).
        "waiting_list_total_latest_month": sum(
            int(r_["waiting_list"]) for r_ in merged
            if r_["month"] == months[-1] and r_["specialty_code"] == "C_999"),
        "raw_release_tag": meta["tag"],
        "source_url": meta["source_url"],
        "raw_sha256": meta["sha256"],
    }
    with open(SUMMARY_JSON, "w") as f:
        json.dump(summary, f, indent=2)

    # ---- update state (fingerprint, checksum ledger, ingest history)
    os.makedirs(STATE, exist_ok=True)
    with open(fp_path, "w") as f:
        f.write(f"{fp}  recorded {datetime.now(timezone.utc).isoformat()}\n")
    with open(os.path.join(STATE, "raw_checksums.txt"), "a") as f:
        f.write(f"{meta['sha256']}  {meta['tag']}  {meta['source_url']}\n")
    with open(hist_path, "a") as f:
        f.write(json.dumps({"date": date.today().isoformat(), "month": month,
                            "normalised_rows": len(new_rows),
                            "source_rows": src_rows}) + "\n")
    log(f"published: {len(merged)} rows, summary: {json.dumps(summary)}")
    gh_output("month", month)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "fetch":
        cmd_fetch()
    elif cmd == "process":
        cmd_process()
    else:
        print("usage: pipeline.py fetch|process")
        sys.exit(2)
