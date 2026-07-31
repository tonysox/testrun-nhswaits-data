# testrun-nhswaits-data

**Test-run scraper — playbook validation exercise, not a product.** Safe to delete.

Working title for the case under test: **Turnbeck** (simulated pick; no domain bought).

Git-scraping pipeline for NHS England RTT (referral-to-treatment) waiting times,
built to `STANDARD-data-pipeline.md`:

- `.github/workflows/ingest.yml` — scheduled 2x/day (odd minutes) + manual dispatch;
  `drill: true` input runs the corrupted-file drill (renamed column + truncated rows
  must be blocked by the gates and must not touch last-good data).
- `scripts/pipeline.py` — discover latest Full-CSV extract from the RTT landing page
  (per-FY URLs, unstable filenames — never hardcode), fetch, checksum-dedupe,
  archive raw, parse, schema-fingerprint, quality gates, publish.
- **Raw layer** — untouched source ZIP + sidecar JSON as GitHub **Release assets**
  (`raw-<date>` tags). *Deviation:* the standard says Cloudflare R2; the available
  token lacks R2 scope (testrun D-002), so Releases stand in. Never overwritten;
  deduped by sha256.
- **Normalised layer** — `data/normalised/rtt_trust_specialty.csv`
  (row_key, month, provider code/name, specialty code/name, waiting-list size,
  estimated median wait in weeks, % within 18 weeks — Incomplete Pathways only),
  plus `data/normalised/summary.json` for the site build.
- **National layer** — `data/normalised/national_medians.csv`
  (row_key = `month|specialty`, providers reporting, England waiting-list size,
  **patient-weighted** median wait, % within 18 weeks). One row per treatment
  area per month. This is the ONLY sanctioned source for a "typical for
  England" figure: it is derived by summing the weekly wait bands across every
  provider reporting the specialty and interpolating the median of that single
  pooled distribution. Taking the median (or mean) of provider medians is
  **wrong** and is the defect this layer exists to prevent — it lets a
  68-patient clinic weigh the same as a 3,370-patient trust and understates the
  England figure on every treatment area (QA D-101).
- **Diff layer** — `data/diffs/<stamp>.txt` csv-diff between consecutive versions
  (the alert-feed input).
- **Gates** (fail loudly BEFORE publish; last-good stays untouched):
  schema fingerprint (ordered column+dtype hash) · row-count delta ±20% vs trailing
  average · null-rate ceilings on key columns · staleness (>150 days) · sanity.
- **Heartbeat** — placeholder step; production pings Healthchecks.io on success only.
- **Locations layer (ADR-03)** — `.github/workflows/locations.yml` (manual
  `workflow_dispatch`; occasional refresh, never on the ingest cron) runs
  `scripts/locations.py`: every provider_code in the normalised layer is
  resolved to name+postcode via the NHS ODS ORD API (legal contract +
  ~Sept-2027 deprecation diary in `pipeline/SOURCES/ods-locations.md` — never
  legacy etr/ets), joined to coordinates via the latest ONSPD (**all BT\*
  postcodes stripped at ingest** — NI licence carve-out, gated; see
  `pipeline/SOURCES/onspd.md`). Emits `data/locations/providers.json`
  (≥95% resolution gate, failures flagged), `outcodes.json` (~2,900 England
  outcode centroids for the client-side near-you tool) and `nearest.json`
  (8 nearest providers per provider, build-time haversine).

Source: NHS England RTT statistical work area (full CSV data file, monthly,
revised ~every 6 months — hence every raw vintage is kept).
