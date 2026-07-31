# Source contract — ONS Postcode Directory (ONSPD)

Per-source contract per STANDARD-data-pipeline §4, for the ADR-03 locations
step (`scripts/locations.py`, `.github/workflows/locations.yml`). Changing
anything here is a recorded decision.

## Legal basis (STANDARD-legal-compliance §1, G1b verified 2026-07-31 — BINDING)

- ONSPD is **OGL v3 for Great Britain**. **Northern Ireland (BT\*) postcodes
  are carved out** — internal-use EUL only; commercial reuse would need an
  LPS licence we do not hold.
- **⇒ MANDATORY FILTER, implemented + gated:** `scripts/locations.py build`
  **strips every row whose postcode starts with `BT` before ANY use** —
  before the provider join and before centroid computation. Gate G-L3
  hard-fails the run if (a) any BT\* value survives into any retained set,
  or (b) zero BT rows were stripped (a UK-wide ONSPD always contains NI
  rows, so a zero count means the filter or the extract is broken). The
  stripped-row count is recorded in `data/locations/providers.json`
  (`bt_rows_stripped`) and `state/locations_history.jsonl`. The product is
  England-only, which already makes NI data unnecessary — the strip is belt
  and braces on top of that, per the legal GO-with-conditions.
- **Attribution the web build must carry, all three lines VERBATIM**
  (year = ONSPD edition year):
  - `Contains OS data © Crown copyright and database right <yyyy>`
  - `Contains Royal Mail data © Royal Mail copyright and database right <yyyy>`
  - `Source: Office for National Statistics licensed under the Open Government Licence v.3.0`
- Anonymous download, no registration. No endorsement by ONS/OS/Royal Mail
  may be implied.

## What we ingest

The full-UK ONSPD CSV (one row per postcode, live + terminated; ~2.7M rows),
used for two derived outputs only — no postcode-level data is republished:

1. **Provider join:** ODS provider postcode → `lat`/`long` (rows with a grid
   reference, `osgrdind != 9`; terminated postcodes still join — a hospital's
   coordinates stay valid — with a note on the provider record).
2. **Outcode centroids:** mean lat/long per England postcode district
   (`ctry == E92000001`, live only — no `doterm` —, gridded only) →
   `data/locations/outcodes.json` (the near-you tool's client-side bundle,
   ADR-03). **Count correction vs ADR-03:** the ADR's "~2,900" estimate is
   the GB live figure (measured 2,897, ONSPD May 2026); **England
   live+gridded is 2,223** (England-ever 2,342; the grid filter drops only
   non-geographic outcodes such as GIR/BN91/CH90). Gate G-L4 bounds
   [2,100..2,500] reflect England reality.

## URL discovery strategy (never hardcode item ids or filenames)

ONSPD editions are published on the ONS **Open Geography Portal**, which is
ArcGIS Online under the hood. Item ids change every edition; the public
search API is the stable entry point:

1. `GET https://www.arcgis.com/sharing/rest/search?q="ONS Postcode Directory"
   owner:ONSGeography_data&f=json&sortField=modified&sortOrder=desc`
2. Take the newest result with `type == "CSV Collection"` and title matching
   `ONS Postcode Directory (<Month> <Year>)` exactly (this excludes the User
   Guide, multi-CSV variants, feature services and the version-2 products).
3. Item metadata: `…/content/items/<id>?f=json` (name + size);
   payload: `…/content/items/<id>/data` (zip, ~250MB).
4. Inside the zip exactly one `Data/ONSPD_<MON>_<YYYY>_UK.csv` is expected —
   more or fewer is a contract breach and hard-fails.

Fallback if the ArcGIS search moves: the human landing page is
geoportal.statistics.gov.uk → Postcodes → ONS Postcode Directories.

## Cadence

ONSPD is **quarterly** (February, May, August, November). The locations step
is `workflow_dispatch` only — refresh quarterly-ish or when provider churn
demands it; the RTT ingest never touches this source. The edition used is
recorded in `data/locations/providers.json.onspd_edition` and in
`state/locations_history.jsonl`.

## Fields used (by name, never by position — vintage-suffix gotcha)

`pcds` (display postcode; outcode = text before the space), `lat`, `long`,
country code, termination date, grid-reference indicator. **Column names
mutate between editions** (verified 2026-07-31: the May 2026 edition uses
`ctry25cd` and `gridind` where older editions/documentation say `ctry` and
`osgrdind`; boundary-vintage suffixes like `25cd` roll forward annually).
`scripts/locations.py:resolve_onspd_columns` therefore resolves each needed
field by pattern; a pattern that stops matching exactly one column is a
contract breach and hard-fails. Grid indicator `9` rows carry the 99.999999
no-grid sentinel and are excluded from both join and centroids.

## Gates

- **G-L3** BT-strip verification (above).
- **G-L4** outcode count sanity: 2,500..3,200.
- **G-L5** England bounding-box check on published provider coords.
- **G-L6** `outcodes.json` gzipped ≤80KB hard ceiling (ADR-03 target ≤60KB;
  actual size printed every run).

## Contingency / tripwires

- Licence change on ONSPD or the NI carve-out → re-open the legal chain
  before the next refresh (this file is the tripwire location).
- Schema drift (missing named field / >1 data CSV in zip) → hard fail, no
  publish, last-good untouched.
- NSPL is NOT a drop-in substitute (different centroid methodology); using
  it would be a recorded decision + legal re-check.
