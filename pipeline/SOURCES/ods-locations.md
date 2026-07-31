# Source contract — NHS ODS provider reference data (names, addresses, postcodes)

Per-source contract per STANDARD-data-pipeline §4, for the ADR-03 locations
step (`scripts/locations.py`, `.github/workflows/locations.yml`). Changing
anything here is a recorded decision.

## Legal basis (STANDARD-legal-compliance §1, G1b verified 2026-07-31 — BINDING)

- ODS organisation reference data is **OGL** (org-level, no personal data).
- **Legacy etr/ets CSV extracts are DEAD and MUST NEVER be used** — the legal
  GO-with-conditions names Data Search & Export (DSE) or the FHIR R4 API as
  canonical.
- **Attribution line the web build must carry** (verbatim, month = ODS fetch
  month): `Source: NHS England Organisation Data Service, <month yyyy>`
- **No NHS endorsement implied anywhere** — this dataset is reference data,
  not a partnership; ODS/NHS naming discipline applies to all copy near it.
  This is NOT the Syndication regime (that is the dentist-data row).

## What we ingest

One organisation record per `provider_code` present in
`data/normalised/rtt_trust_specialty.csv` (all months — currently 562 codes;
532 in the latest month). Fields kept: `Name`, `GeoLoc.Location.PostCode`,
`Status`, `LastChangeDate`. Postcodes are joined to coordinates via ONSPD
(see `onspd.md`); ODS itself carries no coordinates.

## Access route — CHOSEN: ORD API, with the deprecation diarised

- **Chosen (interim): ORD API 2-0-0** —
  `GET https://directory.spineservices.nhs.uk/ORD/2-0-0/organisations/<code>`
  (JSON, unauthenticated, no registration, one lookup per code, 0.15s
  politeness delay, 404 = definitive unknown-code answer).
- **Why not DSE:** DSE (odsdatasearchandexport.nhs.uk) is an interactive
  portal for manual exports — no stable unauthenticated automation API
  (probed 2026-07-31: no programmatic endpoint). It remains the **manual
  break-glass fallback**: export the relevant org roles by hand and commit
  the export if the APIs are down at refresh time.
- **Why not FHIR R4 yet:** the Organisation Data Terminology (ODS) FHIR R4
  API on `api.service.nhs.uk` returns 401 without an API key (verified by
  probe 2026-07-31); registering an application is an **owner-batch item**.
- **⚠ DEPRECATION DIARY — ORD API is under deprecation review, expected
  retirement ~September 2027.** Migration to the FHIR R4 API (needs the
  owner's API key) MUST land before then. Tripwire: the locations workflow
  fails loudly if ORD starts erroring; treat any ORD 5xx/410 wave or NHS
  deprecation notice as the migration trigger, not a retry problem.

## URL discovery strategy

Endpoint is stable and versioned (`/ORD/2-0-0/`); provider codes come from the
normalised layer, never hardcoded. No scraping or filename discovery needed.
If the base URL dies, check the ODS pages on digital.nhs.uk for the successor
(expected: the R4 API above) — do NOT fall back to any `etr`/`ets` download
that legacy tutorials still link.

## Cadence

Occasional, manual (`workflow_dispatch` only — locations churn is rare and the
RTT ingest must not absorb a 235MB ONSPD download 2x/day). Refresh when:
new providers appear in the normalised layer (unresolved list grows), an NHS
reorganisation lands (trust mergers), or quarterly alongside a new ONSPD
edition. The gate output lists every unresolved code so drift is visible.

## Gates (locations step)

- **G-L1/G-L2 completeness:** every provider_code resolves to
  code+name+postcode+coords or is FLAGGED with a reason in
  `data/locations/providers.json.unresolved`; **≥95% resolution required to
  publish** — below that the run hard-fails and last-good stays untouched.
- **G-L5 sanity:** published coordinates must fall inside an England bounding
  box.

## Contingency

1. ORD outage: retries ×3; still down → run fails loudly, last-good
   `data/locations/*` remains live (locations staleness is benign for weeks).
2. ORD retired: migrate to FHIR R4 (`api.service.nhs.uk`, owner API key) —
   same fields, same gates.
3. Both APIs unavailable: manual DSE export, committed with this contract
   updated to record the deviation.
