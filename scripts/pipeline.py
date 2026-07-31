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
  BACKFILL_MONTH=YYYY-MM
               supervised backfill mode (ADR-04): fetch THAT month's historical
               full extract from its financial-year landing page (preferring the
               highest '-revised[-N]' variant = final revision as published at
               download time), raw-archive under raw-backfill-YYYY-MM, run all
               gates (baselines arm from the first backfill month, D-028;
               row-count compares within the backfill sequence; staleness N/A
               for historical vintages) and append to the accumulated layer.
               Gate failure = the month is skipped and logged by the driver
               (scripts/backfill.sh), never force-parsed.
  RECOMPUTE=true
               re-derivation mode: `process` re-normalises an ALREADY-ARCHIVED
               raw vintage (work/raw.zip + work/meta.json staged by
               scripts/recompute.sh from the raw-* / raw-backfill-* Releases).
               All parse + gate logic runs unchanged; the state ledgers
               (raw_checksums.txt, ingest_history.jsonl, fingerprint stamps)
               are NOT appended — a recompute is not a new ingest.
               Combine with BACKFILL_MONTH for backfill-vintage semantics.
  WORK_DIR     scratch dir (default ./work)

History-accumulation contract (ADR-04, pinned in pipeline/SOURCES/nhs-rtt.md):
the normalised CSV retains EVERY month ever ingested, one row per
row_key = month|provider|specialty; latest vintage wins per row_key (a re-ingest
of a month replaces that month's rows only); revisions are evidenced in
data/diffs/. On each publish, data/deltas/latest.json carries the per-entity
month-on-month deltas (alerts_thresholds.py, ADR-02) for the W4 send job.
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from alerts_thresholds import compute_deltas  # noqa: E402  (ADR-02 module)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.environ.get("WORK_DIR", os.path.join(REPO_ROOT, "work"))
STATE = os.path.join(REPO_ROOT, "state")
NORM_DIR = os.path.join(REPO_ROOT, "data", "normalised")
DIFF_DIR = os.path.join(REPO_ROOT, "data", "diffs")
NORM_CSV = os.path.join(NORM_DIR, "rtt_trust_specialty.csv")
# National per-specialty series, patient-weighted (D-101). One row per
# month|specialty; consumed by the web build as THE England comparison figure.
NAT_CSV = os.path.join(NORM_DIR, "national_medians.csv")
SUMMARY_JSON = os.path.join(NORM_DIR, "summary.json")
DELTAS_DIR = os.path.join(REPO_ROOT, "data", "deltas")
DELTAS_JSON = os.path.join(DELTAS_DIR, "latest.json")
UA = "testrun-nhswaits-data pipeline (playbook validation; contact: github.com/tonysox)"
# DRILL modes: "" (off) | "full" (rename column + truncate -> fingerprint gate)
#              | "truncate" (truncate only -> row-count gate)
DRILL_MODE = os.environ.get("DRILL", "").lower().replace("true", "full")
DRILL = DRILL_MODE in ("full", "truncate")
BACKFILL = os.environ.get("BACKFILL_MONTH", "").strip()
if BACKFILL and not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", BACKFILL):
    print(f"::error::invalid BACKFILL_MONTH {BACKFILL!r} (want YYYY-MM)")
    sys.exit(2)
if BACKFILL and DRILL:
    print("::error::BACKFILL_MONTH and DRILL are mutually exclusive")
    sys.exit(2)
RECOMPUTE = os.environ.get("RECOMPUTE", "").lower() == "true"
if RECOMPUTE and DRILL:
    print("::error::RECOMPUTE and DRILL are mutually exclusive")
    sys.exit(2)

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
LINK_RE = re.compile(
    r'href="(https://www\.england\.nhs\.uk/statistics/wp-content/[^"]*'
    r'Full-CSV-data-file-([A-Z][a-z]{2})(\d{2})[^"]*\.zip)"')


def fy_page(fy_start_year):
    return ("https://www.england.nhs.uk/statistics/statistical-work-areas/"
            f"rtt-waiting-times/rtt-data-{fy_start_year}-{(fy_start_year + 1) % 100:02d}/")


def landing_pages():
    """Per-financial-year URLs (new URL every April) — follow the landing page,
    never hardcode file URLs (standard §4)."""
    today = date.today()
    fy_start = today.year if today.month >= 4 else today.year - 1
    return [fy_page(fy_start), fy_page(fy_start - 1)]


def scrape_extract_links(page):
    html = http_get(page).read().decode("utf-8", "replace")
    return [(2000 + int(yy), MONTHS[mon], url)
            for url, mon, yy in LINK_RE.findall(html) if mon in MONTHS]


def revision_rank(url):
    """NHS republishes revised vintages as ...-revised.zip / ...-revised-2.zip.
    Higher = later revision; an unrevised file ranks 0."""
    m = re.search(r"-revised(?:-(\d+))?", url)
    if not m:
        return 0
    return int(m.group(1)) if m.group(1) else 1


def discover_latest():
    found = []
    for page in landing_pages():
        try:
            found = scrape_extract_links(page)
        except Exception as e:  # noqa: BLE001
            log(f"landing page {page} not reachable ({e}) — trying previous FY")
            continue
        if found:
            break
    if not found:
        fail("discovery: no Full-CSV-data-file link found on any RTT landing page "
             "(source-change contingency: check URL pattern / page layout)")
    found.sort(key=lambda t: (t[0], t[1], revision_rank(t[2])))
    y, m, url = found[-1]
    log(f"discovered latest extract: {y}-{m:02d} -> {url}")
    return url


def discover_month(month):
    """Backfill discovery: the requested month's extract from ITS financial-year
    landing page, preferring the highest revision (= final as-published-at-
    download; NHS revises past months ~6-monthly)."""
    y, m = int(month[:4]), int(month[5:7])
    fy = y if m >= 4 else y - 1
    page = fy_page(fy)
    try:
        links = scrape_extract_links(page)
    except Exception as e:  # noqa: BLE001
        fail(f"backfill discovery: FY landing page {page} not reachable ({e})")
    candidates = [url for (ly, lm, url) in links if (ly, lm) == (y, m)]
    if not candidates:
        fail(f"backfill discovery: no Full-CSV-data-file link for {month} on {page} "
             "(month may predate the extract format or the page layout changed)")
    candidates.sort(key=revision_rank)
    url = candidates[-1]
    log(f"backfill {month}: {len(candidates)} candidate(s), picked revision rank "
        f"{revision_rank(url)} -> {url}")
    return url


# ---------------------------------------------------------------- fetch
def cmd_fetch():
    os.makedirs(WORK, exist_ok=True)
    url = discover_month(BACKFILL) if BACKFILL else discover_latest()
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
    if BACKFILL:
        sidecar["backfill_month"] = BACKFILL
        sidecar["vintage_note"] = ("historical vintage as published at download "
                                   "time (NHS's final revision to date, not the "
                                   "original first publication)")
    with open(os.path.join(WORK, "sidecar.json"), "w") as f:
        json.dump(sidecar, f, indent=2)

    stamp = date.today().isoformat()
    if DRILL:
        tag = f"drill-raw-{os.environ.get('GITHUB_RUN_ID', 'local')}"
    elif BACKFILL:
        tag = f"raw-backfill-{BACKFILL}"
    else:
        tag = f"raw-{stamp}"
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


def pct_within_18(uppers, band_counts):
    """% of the incomplete-pathways waiting list at <= 18 weeks, from the
    week-band columns. uppers[i] is the upper bound (weeks) of band i
    (999 = the open-ended 'Gt 104 Weeks' band); band_counts[i] its count.

    Denominator = sum of ALL band counts (= patients with a known clock
    start). The source's own 'Total' column CANNOT be the denominator: it is
    blank on every Incomplete Pathways row of the extract (only the Completed
    Pathways parts populate it) — that blank column is why this metric was
    silently empty for every published row until W0c. The band-sum
    denominator reproduces NHS's published national 'within 18 weeks' figure
    (65.5% for May 2026) exactly.

    Null-honesty: if the bands sum to zero (all cells blank/zero — e.g. a row
    whose patients all have an unknown clock start), no percentage is
    computable and '' (null) is returned — never a fake 0. Blank individual
    band cells count as 0, the same convention median_from_bands uses.
    """
    denom = sum(band_counts)
    if denom <= 0:
        return ""
    within = sum(c for u, c in zip(uppers, band_counts) if u <= 18)
    return f"{100.0 * within / denom:.1f}"


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
    # Backfill fingerprints live in their own baseline namespace: the baseline
    # arms from the FIRST backfill month (bootstrap rule D-028) and the gate
    # compares within the backfill sequence, leaving the live pipeline's
    # baseline untouched.
    fp_name = "backfill_fingerprint.txt" if BACKFILL else "schema_fingerprint.txt"
    fp_path = os.path.join(STATE, fp_name)
    if os.path.exists(fp_path):
        stored = open(fp_path).read().strip().split()[0]
        if fp != stored:
            missing = [c for c in KEY_COLS if c not in header]
            fail("SCHEMA FINGERPRINT MISMATCH — refusing to parse/publish. "
                 f"stored={stored[:12]} got={fp[:12]}; key columns missing: {missing or 'none'}. "
                 "Inspect the raw release asset, update the parser deliberately, "
                 f"then refresh state/{fp_name}.")
        log(f"schema fingerprint OK ({fp[:12]})")
    else:
        log(f"first {'backfill ' if BACKFILL else ''}run: recording schema "
            f"fingerprint {fp[:12]} in state/{fp_name}")
    if BACKFILL:
        live_fp_path = os.path.join(STATE, "schema_fingerprint.txt")
        if os.path.exists(live_fp_path):
            live_fp = open(live_fp_path).read().strip().split()[0]
            log(f"backfill fingerprint vs LIVE baseline: "
                f"{'MATCH' if fp == live_fp else 'DIFFERS (informational)'}")

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
            a = agg.setdefault(key, {"bands": [0.0] * len(bands),
                                     "total_all": 0.0})
            for bi, (ci, _) in enumerate(bands):
                a["bands"][bi] += num(row[ci]) if ci < len(row) else 0.0
            # NB: the 'Total' column is NOT aggregated — it is blank on every
            # Incomplete Pathways row (see pct_within_18); 'Total All' is the
            # published waiting-list size (bands + unknown clock start).
            a["total_all"] += num(row[idx["Total All"]])

    if not agg or not period_raw:
        fail("no Incomplete Pathways rows found — refusing to publish")

    m = re.match(r"RTT-([A-Za-z]+)-(\d{4})", period_raw)
    if not m or m.group(1)[:3] not in MONTHS:
        fail(f"unrecognised Period value: {period_raw!r}")
    month = f"{m.group(2)}-{MONTHS[m.group(1)[:3]]:02d}"
    if BACKFILL and month != BACKFILL:
        fail(f"backfill safety: requested month {BACKFILL} but the fetched file "
             f"contains {month} — wrong file, refusing to publish")

    uppers = [u for _, u in bands]

    # ---- national per-specialty aggregate (QA D-101).
    # The national "typical wait" for a specialty MUST be patient-weighted: the
    # median of ONE pooled England-wide distribution, not the median of 500-odd
    # provider medians. Taking the median of provider medians lets a 68-patient
    # clinic count the same as a 3,370-patient trust, which understated the
    # England figure on every specialty and flipped the direction of the site's
    # headline "vs England" comparison on 26% of result pages.
    # Here we sum each weekly band across every provider reporting the
    # specialty, then run the SAME median_from_bands/pct_within_18 estimators
    # over that pooled distribution — so the national figure and the per-trust
    # figure are computed by identical maths on different populations.
    nat_agg = {}
    for (pcode, pname, tfcode, tfname), a in agg.items():
        n = nat_agg.setdefault(tfcode, {"bands": [0.0] * len(bands),
                                        "total_all": 0.0,
                                        "providers": 0,
                                        "names": {}})
        for bi in range(len(bands)):
            n["bands"][bi] += a["bands"][bi]
        n["total_all"] += a["total_all"]
        n["providers"] += 1
        n["names"][tfname] = n["names"].get(tfname, 0) + 1

    nat_rows = []
    for tfcode, n in sorted(nat_agg.items()):
        # specialty_name is near-constant across providers; take the modal one.
        tfname = max(n["names"].items(), key=lambda kv: (kv[1], kv[0]))[0]
        nat_rows.append({
            "row_key": f"{month}|{tfcode}",
            "month": month,
            "specialty_code": tfcode,
            "specialty_name": tfname,
            "providers": str(n["providers"]),
            "waiting_list": str(int(n["total_all"])),
            "median_wait_weeks_est": median_from_bands(sorted(zip(uppers, n["bands"]))),
            "pct_within_18_weeks": pct_within_18(uppers, n["bands"]),
        })

    new_rows = []
    for (pcode, pname, tfcode, tfname), a in sorted(agg.items()):
        pct18 = pct_within_18(uppers, a["bands"])
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
    # Backfill row counts are compared WITHIN the backfill sequence (baseline
    # arms from the first backfill month, D-028); live ingests keep comparing
    # against the trailing window of all prior ingests.
    if BACKFILL:
        gate_hist = [h for h in hist if h.get("mode") == "backfill"]
    else:
        gate_hist = hist
    if gate_hist:
        trailing = [h["normalised_rows"] for h in gate_hist[-6:]]
        avg = sum(trailing) / len(trailing)
        delta = (len(new_rows) - avg) / avg
        if abs(delta) > 0.20:
            fail(f"GATE row-count: {len(new_rows)} rows vs trailing avg {avg:.0f} "
                 f"({delta:+.0%}) exceeds +/-20% — publish blocked, last-good untouched")
        log(f"gate row-count OK ({delta:+.1%} vs trailing avg"
            f"{' of backfill sequence' if BACKFILL else ''})")
    else:
        log(f"gate row-count: no {'backfill ' if BACKFILL else ''}history yet "
            "(first ingest of this sequence) — recording baseline")

    ceilings = {"provider_code": 0.005, "specialty_code": 0.005, "waiting_list": 0.005,
                "provider_name": 0.05}
    for col, ceil in ceilings.items():
        nulls = sum(1 for r_ in new_rows if not r_[col].strip())
        rate = nulls / len(new_rows)
        if rate > ceil:
            fail(f"GATE null-rate: {col} {rate:.1%} > ceiling {ceil:.1%} — publish blocked")
    log("gate null-rates OK")

    if BACKFILL:
        log("gate staleness N/A (backfill: historical vintage, "
            "as-published-at-download)")
    else:
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

    # ---- gates on the national per-specialty series (D-101)
    if len(nat_rows) != len({r_["specialty_code"] for r_ in new_rows}):
        fail(f"GATE national: {len(nat_rows)} national rows vs "
             f"{len({r_['specialty_code'] for r_ in new_rows})} specialties in the "
             "trust layer — the national aggregate lost or invented a specialty")
    empty_med = [r_ for r_ in nat_rows if not r_["median_wait_weeks_est"]]
    if empty_med:
        fail(f"GATE national: {len(empty_med)} specialties have no computable "
             "national median — refusing to publish an England figure we cannot derive")
    nat_c999 = next((r_ for r_ in nat_rows if r_["specialty_code"] == "C_999"), None)
    if nat_c999 and int(nat_c999["waiting_list"]) != sum_total_rows:
        fail(f"GATE national cross-foot: national C_999 waiting list "
             f"{nat_c999['waiting_list']} != trust-layer C_999 sum {sum_total_rows}")
    # A patient-weighted national median must sit inside the range of the
    # provider medians it pools; outside that range means a summing bug.
    med_by_spec = {}
    for r_ in new_rows:
        if r_["median_wait_weeks_est"]:
            med_by_spec.setdefault(r_["specialty_code"], []).append(
                float(r_["median_wait_weeks_est"]))
    for r_ in nat_rows:
        pool = med_by_spec.get(r_["specialty_code"], [])
        if not pool:
            continue
        v = float(r_["median_wait_weeks_est"])
        if not (min(pool) - 0.05 <= v <= max(pool) + 0.05):
            fail(f"GATE national range: {r_['specialty_code']} national median {v} "
                 f"outside provider range [{min(pool)}, {max(pool)}]")
    log(f"gate national OK ({len(nat_rows)} specialties, patient-weighted)")

    if DRILL:
        # Belt and braces: the drill must never publish even if the corruption
        # somehow slipped every gate above.
        fail("DRILL reached the publish stage — gates failed to block a corrupt file. "
             "This is itself a gate: drill runs never publish.")

    # ---- publish: merge with prior months, write normalised CSV + summary
    # History-accumulation contract (ADR-04): keep every month ever ingested;
    # latest vintage wins per row_key (this ingest replaces ONLY its own
    # month's rows); deterministic (month, provider, specialty) ordering.
    fieldnames = list(new_rows[0].keys())
    old_rows = []
    if os.path.exists(NORM_CSV):
        with open(NORM_CSV, newline="") as f:
            old_rows = [r_ for r_ in csv.DictReader(f) if r_["month"] != month]
    merged = sorted(old_rows + new_rows,
                    key=lambda r_: (r_["month"], r_["provider_code"],
                                    r_["specialty_code"]))
    os.makedirs(NORM_DIR, exist_ok=True)
    with open(NORM_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(merged)

    # National per-specialty series: same accumulate + latest-vintage-wins
    # contract, keyed on month|specialty (D-101).
    nat_fieldnames = list(nat_rows[0].keys())
    nat_old = []
    if os.path.exists(NAT_CSV):
        with open(NAT_CSV, newline="") as f:
            nat_old = [r_ for r_ in csv.DictReader(f) if r_["month"] != month]
    nat_merged = sorted(nat_old + nat_rows,
                        key=lambda r_: (r_["month"], r_["specialty_code"]))
    with open(NAT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=nat_fieldnames)
        w.writeheader()
        w.writerows(nat_merged)

    months = sorted({r_["month"] for r_ in merged})

    # Per-month provenance (D-109): summary.raw_* describe the LAST INGEST,
    # which during a backfill is not the month the site displays. A page that
    # cites its source file (schema.org Dataset.isBasedOn) needs the file THAT
    # month came from, so record it per month and carry prior entries forward.
    month_sources = {}
    if os.path.exists(SUMMARY_JSON):
        try:
            month_sources = json.load(open(SUMMARY_JSON)).get("month_sources", {}) or {}
        except (ValueError, OSError):
            month_sources = {}
    month_sources[month] = {
        "source_url": meta["source_url"],
        "raw_sha256": meta["sha256"],
        "raw_release_tag": meta["tag"],
        "derived_at": datetime.now(timezone.utc).isoformat(),
    }
    month_sources = {k: month_sources[k] for k in sorted(month_sources) if k in months}
    missing_prov = [m_ for m_ in months if m_ not in month_sources]
    if missing_prov:
        log(f"note: {len(missing_prov)} month(s) have no recorded source file yet "
            f"(pre-dating the provenance ledger): {missing_prov[:3]}...")

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
        # NB: the raw_* / source_url fields describe the LAST INGEST (which
        # during a backfill is a historical month), not necessarily
        # latest_month — last_ingest_month disambiguates.
        "last_ingest_month": month,
        "raw_release_tag": meta["tag"],
        "source_url": meta["source_url"],
        "raw_sha256": meta["sha256"],
        # Per-month provenance: which source file each displayed month came
        # from (D-109). Consumers citing a month MUST read this, not the
        # last-ingest fields above.
        "month_sources": month_sources,
        # The national per-specialty layer and how it is derived, stated in the
        # contract itself so a consumer cannot mistake it for an average of
        # provider figures (D-101).
        "national_medians_file": "national_medians.csv",
        "national_medians_method": (
            "patient-weighted: the weekly wait bands are summed across every "
            "provider reporting the specialty in the month, then the median is "
            "interpolated from that single pooled distribution — the same "
            "estimator used for each provider row, applied to England as one "
            "queue. It is NOT the median (or mean) of provider medians."),
        "national_medians_rows": len(nat_merged),
    }
    with open(SUMMARY_JSON, "w") as f:
        json.dump(summary, f, indent=2)

    # ---- per-entity month-on-month deltas for the W4 send job (ADR-02).
    # Computed from the accumulated layer: latest month vs its previous
    # CALENDAR month (empty until that month exists — no fake deltas).
    deltas = compute_deltas(merged)
    os.makedirs(DELTAS_DIR, exist_ok=True)
    with open(DELTAS_JSON, "w") as f:
        json.dump(deltas, f)
    n_material = sum(1 for e in deltas["entities"] if e["material"])
    log(f"deltas: {deltas['month']} vs {deltas['prev_month']} -> "
        f"{len(deltas['entities'])} entities, {n_material} material")

    # ---- update state (fingerprint, checksum ledger, ingest history)
    if RECOMPUTE:
        log("recompute: state ledgers untouched (this raw vintage is already "
            "checksummed + in ingest history; a recompute is a re-derivation, "
            "not a new ingest)")
    else:
        os.makedirs(STATE, exist_ok=True)
        with open(fp_path, "w") as f:
            f.write(f"{fp}  recorded {datetime.now(timezone.utc).isoformat()}\n")
        with open(os.path.join(STATE, "raw_checksums.txt"), "a") as f:
            f.write(f"{meta['sha256']}  {meta['tag']}  {meta['source_url']}\n")
        hist_entry = {"date": date.today().isoformat(), "month": month,
                      "normalised_rows": len(new_rows), "source_rows": src_rows}
        if BACKFILL:
            hist_entry["mode"] = "backfill"
        with open(hist_path, "a") as f:
            f.write(json.dumps(hist_entry) + "\n")
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
