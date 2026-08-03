#!/usr/bin/env python3
"""Provider locations step (ARCHITECTURE ADR-03 — near-you data).

Separate from the 2x/day RTT ingest: locations change rarely, so this runs on
workflow_dispatch only (.github/workflows/locations.yml). Same discipline as
pipeline.py: fetch -> gates -> publish; a gate breach hard-fails BEFORE any
publish file is written and last-good data/locations/* stays untouched.

Subcommands:
  ods    resolve every provider_code in the normalised layer via the NHS ODS
         ORD API (one lookup per code) -> work/ods_records.json.
         LEGAL (STANDARD-legal-compliance §1, G1b 2026-07-31): provider data
         must come from ODS DSE or the FHIR R4 API, never legacy etr/ets CSVs.
         The R4 API (api.service.nhs.uk) needs an API key = owner-batch item;
         DSE is a manual portal. ORD is the sanctioned interim (deprecation
         ~Sept 2027 diarised in pipeline/SOURCES/ods-locations.md).
  onspd  discover + download the latest ONS Postcode Directory (ONSPD) CSV
         Collection from the Open Geography Portal -> work/<edition>.zip.
  build  single streaming pass over the ONSPD CSV:
           - LEGAL condition: strip ALL BT* postcodes FIRST (NI carve-out from
             OGL — see pipeline/SOURCES/onspd.md), count what was stripped;
           - England subset (ctry E92000001) for join + centroids;
           - join provider postcode -> lat/lng;
           - ~2,900 outcode centroids (live, grid-referenced postcodes only);
         then gates, then publish:
           data/locations/providers.json  (code, name, postcode, town, county,
                                           lat, lng)
           data/locations/outcodes.json   (outcode -> [lat, lng])
           data/locations/nearest.json    (code -> 8 nearest provider codes+miles)

  towns  back-fill `town`/`county` onto the PUBLISHED providers.json from the
         same ORD API, without re-running the ~1GB ONSPD download (W10/D-157).

Gates (all fail loudly before publish):
  G-L1 ODS resolution: every provider_code resolves or is flagged; >=95%
       resolution required to publish; every failure listed.
  G-L2 postcode join: resolved providers must gain coordinates or be flagged;
       the >=95% floor applies to the FINAL (code+name+postcode+coords) set.
  G-L3 BT-strip verification: zero BT* rows survive into any retained set.
  G-L4 outcode count sanity: 2,500..3,200 (England has ~2,900 districts).
  G-L5 coordinate sanity: published coords inside England bounding box.
  G-L6 outcodes.json payload: gzipped size reported; hard ceiling 80KB
       (ADR-03 target is <=60KB).
  G-L7 label disambiguator (`towns`): every published provider resolves a town,
       or nothing is written - a half-populated town field would disambiguate
       some colliding display names and silently leave others ambiguous.
"""
import csv
import gzip
import hashlib
import io
import json
import math
import os
import re
import sys
import time
import urllib.request
import zipfile
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.environ.get("WORK_DIR", os.path.join(REPO_ROOT, "work"))
NORM_CSV = os.path.join(REPO_ROOT, "data", "normalised", "rtt_trust_specialty.csv")
LOC_DIR = os.path.join(REPO_ROOT, "data", "locations")
STATE = os.path.join(REPO_ROOT, "state")
HISTORY = os.path.join(STATE, "locations_history.jsonl")
ODS_JSON = os.path.join(WORK, "ods_records.json")
ONSPD_META = os.path.join(WORK, "onspd_meta.json")
UA = "testrun-nhswaits-data locations step (playbook validation; contact: github.com/tonysox)"

ORD_BASE = "https://directory.spineservices.nhs.uk/ORD/2-0-0/organisations"
ARCGIS_SEARCH = ("https://www.arcgis.com/sharing/rest/search"
                 "?q=%22ONS%20Postcode%20Directory%22%20owner%3AONSGeography_data"
                 "&f=json&num=20&sortField=modified&sortOrder=desc")
ARCGIS_ITEM = "https://www.arcgis.com/sharing/rest/content/items/{id}?f=json"
ARCGIS_DATA = "https://www.arcgis.com/sharing/rest/content/items/{id}/data"

ENGLAND_CTRY = "E92000001"
# England bounding box (generous: Scilly -> Berwick).
LAT_MIN, LAT_MAX = 49.8, 55.9
LNG_MIN, LNG_MAX = -6.5, 2.0
RESOLUTION_FLOOR = 0.95
# CHALLENGE NOTE (2026-07-31): ADR-03 estimated "~2,900" England outcodes, but
# that figure is the GB live count (measured 2,897 in ONSPD May 2026). The
# true England live+gridded count is 2,223 (England-ever 2,342; the grid
# filter only drops non-geographic outcodes such as GIR/BN91). Bounds set to
# England reality; evidence in pipeline/SOURCES/onspd.md.
OUTCODE_MIN, OUTCODE_MAX = 2100, 2500
OUTCODES_GZ_CEILING = 80 * 1024  # hard gate; ADR-03 target is 60KB
NEAREST_N = 8


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def http_get(url, timeout=120, retries=3, backoff=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            # 404 = definitive answer (unknown code), do not retry.
            if e.code == 404:
                return 404, b""
            last = e
        except Exception as e:  # noqa: BLE001 - retried, re-raised below
            last = e
        time.sleep(backoff * (attempt + 1))
    raise RuntimeError(f"GET {url} failed after {retries} attempts: {last}")


def provider_codes():
    """Every provider_code in the normalised layer (the ADR-03 population)."""
    codes = {}
    with open(NORM_CSV, newline="") as f:
        for row in csv.DictReader(f):
            codes[row["provider_code"]] = row["provider_name"]
    return codes


def norm_pc(pc):
    return re.sub(r"\s+", "", (pc or "").upper())


# ---------------------------------------------------------------- ods
def cmd_ods():
    os.makedirs(WORK, exist_ok=True)
    codes = provider_codes()
    print(f"provider codes in normalised layer: {len(codes)}")
    records, failures = {}, {}
    for i, (code, rtt_name) in enumerate(sorted(codes.items())):
        status, body = http_get(f"{ORD_BASE}/{code}", timeout=30)
        if status == 404:
            failures[code] = {"reason": "not found in ODS (ORD 404)",
                              "rtt_name": rtt_name}
        else:
            try:
                org = json.loads(body)["Organisation"]
                loc = org.get("GeoLoc", {}).get("Location", {})
                pc = loc.get("PostCode")
                if not pc:
                    failures[code] = {"reason": "ODS record has no postcode",
                                      "rtt_name": rtt_name}
                else:
                    records[code] = {"name": org["Name"],
                                     "postcode": pc,
                                     # W10 / QA D-157: the PLACE. Two providers
                                     # 420 miles apart both publish as "DUCHY
                                     # HOSPITAL"; the town is what tells a
                                     # reader which one they are looking at.
                                     # County is the second resort for two
                                     # same-named providers in the same town.
                                     "town": (loc.get("Town") or "").strip(),
                                     "county": (loc.get("County") or "").strip(),
                                     "status": org.get("Status"),
                                     "last_change": org.get("LastChangeDate")}
            except (KeyError, ValueError) as e:
                failures[code] = {"reason": f"unparseable ODS response: {e}",
                                  "rtt_name": rtt_name}
        if (i + 1) % 100 == 0:
            print(f"  {i + 1}/{len(codes)} looked up")
        time.sleep(0.15)  # politeness: unauthenticated public API
    out = {"fetched_at": utcnow(), "api": f"{ORD_BASE} (ORD 2-0-0)",
           "total_codes": len(codes), "resolved": len(records),
           "failures": failures, "records": records}
    with open(ODS_JSON, "w") as f:
        json.dump(out, f, indent=1, sort_keys=True)
    rate = len(records) / len(codes)
    print(f"ODS resolution: {len(records)}/{len(codes)} = {rate:.1%}; "
          f"{len(failures)} failures")
    for code, why in sorted(failures.items()):
        print(f"  FLAGGED {code}: {why['reason']} (RTT name: {why['rtt_name']})")
    # G-L1 floor is enforced at build time against the FINAL set; a total ODS
    # outage should still fail fast here.
    if rate < RESOLUTION_FLOOR:
        print(f"::error::G-L1 ODS resolution {rate:.1%} < {RESOLUTION_FLOOR:.0%} "
              "- refusing to continue")
        sys.exit(1)


# ---------------------------------------------------------------- onspd
def discover_onspd():
    """Find the newest 'ONS Postcode Directory (<Month> <Year>)' CSV Collection
    on the Open Geography Portal via the public ArcGIS search API (item ids and
    editions change quarterly - never hardcode)."""
    status, body = http_get(ARCGIS_SEARCH, timeout=60)
    results = json.loads(body).get("results", [])
    pat = re.compile(r"^ONS Postcode Directory \((January|February|March|April|May|"
                     r"June|July|August|September|October|November|December) \d{4}\)$")
    for r in results:  # already sorted newest-modified first
        if r.get("type") == "CSV Collection" and pat.match(r.get("title", "")):
            return r["id"], r["title"]
    raise RuntimeError("no ONSPD CSV Collection found on the Open Geography Portal "
                       "- discovery strategy in pipeline/SOURCES/onspd.md may need updating")


def cmd_onspd():
    os.makedirs(WORK, exist_ok=True)
    item_id, title = discover_onspd()
    status, body = http_get(ARCGIS_ITEM.format(id=item_id), timeout=60)
    item = json.loads(body)
    name, size = item["name"], item.get("size", 0)
    dest = os.path.join(WORK, name)
    print(f"latest ONSPD: {title} (item {item_id}, {name}, {size / 1e6:.0f}MB)")
    if os.path.exists(dest) and os.path.getsize(dest) == size:
        print("already downloaded - reusing cached zip")
    else:
        print("downloading...")
        req = urllib.request.Request(ARCGIS_DATA.format(id=item_id),
                                     headers={"User-Agent": UA})
        h = hashlib.sha256()
        with urllib.request.urlopen(req, timeout=600) as resp, open(dest, "wb") as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
                f.write(chunk)
        print(f"downloaded {os.path.getsize(dest) / 1e6:.0f}MB sha256={h.hexdigest()}")
    with open(ONSPD_META, "w") as f:
        json.dump({"edition": title, "item_id": item_id, "zip": dest,
                   "size": os.path.getsize(dest), "fetched_at": utcnow()}, f)


# ---------------------------------------------------------------- build
def resolve_onspd_columns(fieldnames):
    """ONSPD column names carry boundary-vintage suffixes that change between
    editions (verified May 2026: 'ctry25cd', 'gridind'; older editions used
    'ctry', 'osgrdind'). Resolve the fields we need by pattern, by name never
    by position; missing = contract breach, hard fail (onspd.md)."""
    def find(pattern, what):
        hits = [c for c in fieldnames if re.fullmatch(pattern, c)]
        if len(hits) != 1:
            print(f"::error::ONSPD column for {what} not uniquely resolvable "
                  f"(pattern {pattern!r} -> {hits}) - onspd.md contract breach")
            sys.exit(1)
        return hits[0]
    return {
        "pcds": find(r"pcds", "display postcode"),
        "lat": find(r"lat", "latitude"),
        "long": find(r"long", "longitude"),
        "ctry": find(r"ctry(\d{2}cd)?", "country code"),
        "doterm": find(r"doterm", "termination date"),
        "grid": find(r"(os)?gr(i)?d?ind", "grid-reference indicator"),
    }


def haversine_miles(lat1, lng1, lat2, lng2):
    r = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def cmd_build():
    with open(ODS_JSON) as f:
        ods = json.load(f)
    with open(ONSPD_META) as f:
        onspd_meta = json.load(f)
    codes = provider_codes()

    wanted = {}  # normalised postcode -> list of provider codes
    for code, rec in ods["records"].items():
        wanted.setdefault(norm_pc(rec["postcode"]), []).append(code)

    # Single streaming pass over the ONSPD CSV inside the zip.
    zf = zipfile.ZipFile(onspd_meta["zip"])
    csv_names = [n for n in zf.namelist()
                 if re.search(r"Data/ONSPD_[A-Z]+_\d{4}_UK\.csv$", n)]
    if len(csv_names) != 1:
        print(f"::error::expected exactly one Data/ONSPD_*_UK.csv in the zip, "
              f"got {csv_names} - onspd.md contract breach")
        sys.exit(1)
    print(f"streaming {csv_names[0]} from {os.path.basename(onspd_meta['zip'])}")

    bt_stripped = 0
    total_rows = 0
    pc_hits = {}       # normalised postcode -> (lat, lng, ctry, doterm)
    out_sum = {}       # outcode -> [sum_lat, sum_lng, n]  (England, live, gridded)
    with zf.open(csv_names[0]) as raw:
        reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig",
                                                 newline=""))
        col = resolve_onspd_columns(reader.fieldnames)
        for row in reader:
            total_rows += 1
            pcds = row[col["pcds"]]
            # LEGAL condition (STANDARD-legal-compliance §1): Northern Ireland
            # BT* postcodes are OUTSIDE OGL - strip before ANY use.
            if pcds.upper().startswith("BT"):
                bt_stripped += 1
                continue
            npc = norm_pc(pcds)
            gridded = row[col["grid"]] != "9"  # 9 = no grid reference
            if npc in wanted and gridded:
                pc_hits[npc] = (float(row[col["lat"]]), float(row[col["long"]]),
                                row[col["ctry"]], row[col["doterm"]])
            # Centroids: England, live (no termination date), gridded.
            if (row[col["ctry"]] == ENGLAND_CTRY and not row[col["doterm"]]
                    and gridded):
                outcode = pcds.split(" ")[0].strip()
                s = out_sum.setdefault(outcode, [0.0, 0.0, 0])
                s[0] += float(row[col["lat"]])
                s[1] += float(row[col["long"]])
                s[2] += 1
    print(f"ONSPD rows: {total_rows}; BT* stripped: {bt_stripped}; "
          f"provider postcodes matched: {len(pc_hits)}/{len(wanted)}; "
          f"England outcodes: {len(out_sum)}")

    # G-L3: nothing BT-prefixed may survive.
    survivors = ([pc for pc in pc_hits if pc.startswith("BT")]
                 + [oc for oc in out_sum if oc.startswith("BT")])
    if survivors:
        print(f"::error::G-L3 BT-strip breach: {survivors[:10]}")
        sys.exit(1)
    if bt_stripped == 0:
        print("::error::G-L3 sanity: 0 BT rows stripped - a UK-wide ONSPD always "
              "contains NI postcodes, so the filter (or the extract) is wrong")
        sys.exit(1)

    # Assemble providers + flags.
    providers, unresolved = [], []
    for code in sorted(codes):
        rec = ods["records"].get(code)
        if rec is None:
            fail = ods["failures"].get(code, {"reason": "missing from ODS pass"})
            unresolved.append({"code": code, "name": codes[code],
                               "reason": fail["reason"]})
            continue
        hit = pc_hits.get(norm_pc(rec["postcode"]))
        if hit is None:
            unresolved.append({"code": code, "name": rec["name"],
                               "reason": f"postcode {rec['postcode']} not in "
                                         "ONSPD (post BT-strip, gridded)"})
            continue
        lat, lng, ctry, doterm = hit
        note = None
        if ctry != ENGLAND_CTRY:
            note = f"postcode outside England subset (ctry={ctry})"
        elif doterm:
            note = f"terminated postcode (doterm={doterm}) - coords still valid"
        p = {"code": code, "name": rec["name"], "postcode": rec["postcode"],
             "lat": round(lat, 5), "lng": round(lng, 5)}
        # W10 / QA D-157: the label disambiguator. Published only when ODS
        # actually carries it, so a consumer can tell "no town" from "no data".
        if rec.get("town"):
            p["town"] = rec["town"]
        if rec.get("county"):
            p["county"] = rec["county"]
        if note:
            p["note"] = note
        providers.append(p)

    rate = len(providers) / len(codes)
    print(f"final resolution: {len(providers)}/{len(codes)} = {rate:.1%}")
    for u in unresolved:
        print(f"  FLAGGED {u['code']}: {u['reason']} ({u['name']})")

    # G-L1/G-L2 floor on the FINAL set.
    if rate < RESOLUTION_FLOOR:
        print(f"::error::G-L1/G-L2 resolution {rate:.1%} < {RESOLUTION_FLOOR:.0%} "
              "- refusing to publish")
        sys.exit(1)
    # G-L5 coordinate sanity.
    bad = [p for p in providers
           if not (LAT_MIN <= p["lat"] <= LAT_MAX and LNG_MIN <= p["lng"] <= LNG_MAX)]
    if bad:
        print(f"::error::G-L5 coords outside England box: "
              f"{[(p['code'], p['lat'], p['lng']) for p in bad[:10]]}")
        sys.exit(1)
    # G-L7 (W11 / D-163): THE TOWN IS A GATE ON THE BUILD PATH TOO.
    # `towns` (the back-fill) has refused to publish a half-populated town set
    # since W10, but `build` — the path the locations workflow actually runs —
    # published the field only "when ODS carries it", with nothing asserting
    # that it does. The site's name disambiguator consumes this field: without
    # it, two hospitals that share a name silently degrade to an ODS code on a
    # page a patient chooses from, and every gate downstream stays green
    # because a code is still a distinct label. An input a fix depends on gets
    # a gate at the point it is produced, not only where it is read.
    missing_town = missing_towns(providers)
    if missing_town:
        print(f"::error::G-L7 {len(missing_town)} resolved providers have no ODS "
              f"town: {missing_town[:20]} - refusing to publish a half-usable "
              "disambiguator")
        sys.exit(1)
    print(f"G-L7: all {len(providers)} resolved providers carry an ODS town")
    # G-L4 outcode sanity.
    if not (OUTCODE_MIN <= len(out_sum) <= OUTCODE_MAX):
        print(f"::error::G-L4 outcode count {len(out_sum)} outside "
              f"[{OUTCODE_MIN}, {OUTCODE_MAX}]")
        sys.exit(1)

    outcodes = {oc: [round(s[0] / s[2], 4), round(s[1] / s[2], 4)]
                for oc, s in sorted(out_sum.items())}
    outcodes_bytes = json.dumps(outcodes, separators=(",", ":")).encode()
    gz = len(gzip.compress(outcodes_bytes, 9))
    print(f"outcodes.json: {len(outcodes)} outcodes, {len(outcodes_bytes)}B raw, "
          f"{gz}B gzipped (ADR-03 target <=61440B)")
    if gz > OUTCODES_GZ_CEILING:
        print(f"::error::G-L6 outcodes.json gzipped {gz}B > ceiling "
              f"{OUTCODES_GZ_CEILING}B")
        sys.exit(1)

    # Nearest-N precompute (trust -> trust, haversine).
    nearest = {}
    pts = [(p["code"], p["lat"], p["lng"]) for p in providers]
    for code, lat, lng in pts:
        dists = sorted(
            ((haversine_miles(lat, lng, lat2, lng2), c2)
             for c2, lat2, lng2 in pts if c2 != code))
        nearest[code] = [{"code": c2, "mi": round(d, 1)}
                         for d, c2 in dists[:NEAREST_N]]

    # ---- all gates green: publish.
    os.makedirs(LOC_DIR, exist_ok=True)
    generated_at = utcnow()
    providers_doc = {
        "generated_at": generated_at,
        "ods_source": ods["api"],
        "ods_fetched_at": ods["fetched_at"],
        "onspd_edition": onspd_meta["edition"],
        "bt_rows_stripped": bt_stripped,
        "resolution": {"total_codes": len(codes), "resolved": len(providers),
                       "flagged": len(unresolved), "rate": round(rate, 4)},
        "providers": providers,
        "unresolved": unresolved,
    }
    with open(os.path.join(LOC_DIR, "providers.json"), "w") as f:
        json.dump(providers_doc, f, indent=1)
        f.write("\n")
    with open(os.path.join(LOC_DIR, "outcodes.json"), "wb") as f:
        f.write(outcodes_bytes)
        f.write(b"\n")
    with open(os.path.join(LOC_DIR, "nearest.json"), "w") as f:
        json.dump({"generated_at": generated_at, "n": NEAREST_N,
                   "nearest": nearest}, f, separators=(",", ":"))
        f.write("\n")
    os.makedirs(STATE, exist_ok=True)
    with open(HISTORY, "a") as f:
        f.write(json.dumps({
            "at": generated_at, "onspd_edition": onspd_meta["edition"],
            "total_codes": len(codes), "resolved": len(providers),
            "flagged": len(unresolved), "rate": round(rate, 4),
            "bt_rows_stripped": bt_stripped, "outcodes": len(outcodes),
            "outcodes_gz_bytes": gz}) + "\n")
    print("published data/locations/{providers,outcodes,nearest}.json")



def missing_towns(providers):
    """G-L7: the provider codes with no usable ODS town (W11 / D-163).

    One function, used by BOTH the full `build` and the `towns` back-fill, so
    the two paths cannot disagree about what "published" means.
    """
    return [p["code"] for p in providers if not str(p.get("town") or "").strip()]


# ---------------------------------------------------------------- towns
def cmd_towns():
    """Back-fill `town`/`county` onto an ALREADY PUBLISHED providers.json.

    W10 / QA D-157. `build` now carries the town through from the `ods` pass,
    but a full rebuild also re-downloads a ~1GB ONSPD zip and re-derives every
    centroid — which would change coordinates that nobody asked to change, on a
    wave whose only new requirement is a label. This step touches exactly one
    field per provider, from exactly the same source `ods` uses, and records
    when it did so. It is not a substitute for `build`: the next full run
    produces the same field from the same API.

    G-L7: every published provider must come back with a town, or the run
    refuses to write. A partial town set would disambiguate some colliding
    labels and silently leave others ambiguous, which is the defect.
    """
    path = os.path.join(LOC_DIR, "providers.json")
    with open(path) as f:
        doc = json.load(f)
    providers = doc["providers"]
    print(f"back-filling town/county for {len(providers)} published providers")
    for i, p in enumerate(providers):
        status, body = http_get(f"{ORD_BASE}/{p['code']}", timeout=30)
        town = county = ""
        if status != 404:
            try:
                loc = json.loads(body)["Organisation"].get(
                    "GeoLoc", {}).get("Location", {})
                town = (loc.get("Town") or "").strip()
                county = (loc.get("County") or "").strip()
            except (KeyError, ValueError) as e:
                print(f"  unparseable ODS response for {p['code']}: {e}")
        if town:
            p["town"] = town
        else:
            p.pop("town", None)
        if county:
            p["county"] = county
        else:
            p.pop("county", None)
        if (i + 1) % 100 == 0:
            print(f"  {i + 1}/{len(providers)} looked up")
        time.sleep(0.15)  # politeness: unauthenticated public API
    missing = missing_towns(providers)
    if missing:
        print(f"::error::G-L7 {len(missing)} published providers have no ODS "
              f"town: {missing[:20]} - refusing to publish a half-usable "
              "disambiguator")
        sys.exit(1)
    doc["ods_towns_fetched_at"] = utcnow()
    with open(path, "w") as f:
        json.dump(doc, f, indent=1)
        f.write("\n")
    print(f"published town/county for {len(providers)} providers in {path}")


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("ods", "onspd", "build", "towns"):
        print("usage: locations.py ods|onspd|build|towns")
        sys.exit(2)
    {"ods": cmd_ods, "onspd": cmd_onspd, "build": cmd_build,
     "towns": cmd_towns}[sys.argv[1]]()


if __name__ == "__main__":
    main()
