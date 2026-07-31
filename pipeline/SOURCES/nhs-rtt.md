# Source contract — NHS England RTT waiting times (consultant-led, full CSV extract)

Per-source contract per STANDARD-data-pipeline §4. This file is the single
place that pins how this source behaves and how the pipeline is allowed to
treat it. Changing anything here is a recorded decision.

## What we ingest

Monthly "Full CSV data file" extract (ZIP, one CSV inside, ~178k rows) from
NHS England's Referral to Treatment (RTT) statistics. We normalise the
**Incomplete Pathways** part (= the waiting list) to one row per
trust × specialty per month.

## URL discovery strategy (never hardcode file URLs)

- Landing pages are **per financial year** (new URL every April):
  `https://www.england.nhs.uk/statistics/statistical-work-areas/rtt-waiting-times/rtt-data-<YYYY>-<YY+1>/`
- File links match
  `Full-CSV-data-file-<Mon><YY>-ZIP-…​.zip` under
  `https://www.england.nhs.uk/statistics/wp-content/…` with **random suffixes**
  (e.g. `…-3jBgba.zip`) — filenames are NOT stable; always re-discover from the
  landing page.
- **Live ingest:** newest month across the current FY page (falling back to the
  previous FY page around April).
- **Backfill (`BACKFILL_MONTH`):** the requested month is discovered on **its
  own FY page**; where several variants exist, the highest revision wins:
  `-revised-2` > `-revised` > unrevised (`scripts/pipeline.py:revision_rank`).

## Expected schema + fingerprint

- 121 columns; fingerprint = sha256 of the ordered `(column:dtype)` list
  (dtype inferred from a 200-row sample). Live baseline in
  `state/schema_fingerprint.txt`; the backfill sequence keeps its own baseline
  in `state/backfill_fingerprint.txt` (armed from the first backfill month,
  bootstrap rule D-028).
- Preflight fact (2026-07-31): the fingerprint is **identical from the May 2024
  revised extract through May 2026** (`a8329971f21b…`) — no schema churn inside
  the 24-month backfill window.
- Key columns, always accessed **by name, never by position**: `Period`,
  `Provider Org Code`, `Provider Org Name`, `Treatment Function Code`,
  `Treatment Function Name`, `RTT Part Description`, `Total`, `Total All`,
  plus the `Gt X To Y Weeks SUM 1` band columns (≥50 expected).
- Any fingerprint mismatch hard-fails BEFORE parse/publish; in backfill mode a
  failing month is skipped and logged (`state/backfill_log.jsonl`), never
  force-parsed.

## Blank-`Total` quirk + derived measures (normalised schema)

The extract's `Total` column is **blank on every Incomplete Pathways row**
(only the Completed Pathways parts populate it) — never divide by it. The
normalised layer (`month, provider_code/name, specialty_code/name` +
measures) derives, per trust × specialty × month, from the week-band columns:

- `waiting_list` = sum of `Total All` (bands + patients with an unknown
  clock start) — the published waiting-list size.
- `median_wait_weeks_est` = linear interpolation across the cumulated bands.
- `pct_within_18_weeks` = sum of the ≤18-week band counts (`Gt 00 To 01` …
  `Gt 17 To 18`) ÷ sum of **all** band counts (= patients with a known clock
  start), 1 dp. This band-sum denominator reproduces NHS's published
  national "within 18 weeks" figure exactly (65.5% for May 2026, verified
  against the C_999 rows in the W0c recompute).
- **Null-honesty:** a row whose bands sum to zero gets `''` (null) for the
  band-derived measures — never a fake 0. Blank band cells count as 0.
- History note: rows published before the W0c full recompute (2026-07) had
  `pct_within_18_weeks` empty — the old code divided by the blank `Total`
  column. Fixed in normalisation and recomputed across all vintages from the
  archived raw releases (`.github/workflows/recompute.yml`); evidence in
  `data/diffs/*-recompute-pct.txt`.

## The national per-specialty layer (`national_medians.csv`)

A "typical for England" figure must be **patient-weighted**. It is derived per
specialty × month by summing every band across all providers reporting that
specialty and then applying the same `median_from_bands` / `pct_within_18`
estimators to that one pooled distribution — England treated as a single queue.

Do **not** compute it as the median (or mean) of the per-provider
`median_wait_weeks_est` values. That treats a 68-patient clinic as equal to a
3,370-patient trust; measured on May 2026 it understated the patient-weighted
figure on **all 24** treatment areas (+0.1 to +3.4 weeks) and changed the
direction of a consumer's "vs England" comparison on 879 of 4,126 rows (QA
D-101). Gates enforce it: the national row count must equal the specialty
count, every specialty must yield a computable median, the national `C_999`
waiting list must cross-foot to the trust layer's `C_999` sum, and each
national median must fall inside the range of the provider medians it pools.

`summary.json` carries `national_medians_method` stating this in words, so a
downstream consumer cannot mistake the column for an average of providers.

## Per-month provenance (`summary.month_sources`)

`summary.source_url` / `raw_sha256` / `raw_release_tag` describe the **last
ingest**, which during a backfill is a historical month — not `latest_month`.
Anything citing the provenance of a DISPLAYED month (e.g. a schema.org
`Dataset.isBasedOn`) must read `summary.month_sources[<month>]`, which records
the source URL, checksum and release tag for each month present (QA D-109).

## Embedded-totals quirk (C_999)

The extract embeds its own per-trust totals as specialty code **`C_999`
("Total")**. Summing every row double-counts (~14.37M vs the true ~7.18M).
Rules:
- national totals are computed **only** from `C_999` rows;
- the cross-foot gate reconciles `C_999` totals against the sum of individual
  specialties (>1% divergence blocks publication);
- `C_999` rows are flagged/aggregated separately in `summary.json`, never
  counted as dimension members (D-035).

## History-accumulation contract (ARCHITECTURE ADR-04 — pinned)

- `data/normalised/rtt_trust_specialty.csv` retains **every month ever
  ingested**, one row per `row_key = month|provider|specialty`, ordered
  deterministically by (month, provider, specialty).
- **Latest vintage wins per row_key:** re-ingesting a month (e.g. an NHS
  revision) replaces that month's rows ONLY; all other months are untouched.
- Every revision is **evidenced in `data/diffs/`** (keyed csv-diff against the
  previous published version) — the audit layer alerts and the statistics page
  draw on.
- `summary.json.months_present` is the authoritative month list; per-entity
  series for the web build are derived from this one file (ordered
  `[{m, wl, med}]`, nulls stay nulls).
- On each publish, `data/deltas/latest.json` carries per-entity month-on-month
  deltas (latest month vs its previous **calendar** month; empty if that month
  is absent) per the versioned thresholds in `scripts/alerts_thresholds.py`
  (ADR-02). No emails are sent from the pipeline (W4 consumes this file).

## Revision policy

- NHS **revises past months ~every 6 months** (April/October-ish), publishing
  `…-revised[-N].zip` variants on the month's FY page. Revisions arrive
  through the normal live ingest as changed rows for old months and show up in
  `data/diffs/` — they update pages but **never email subscribers** (ADR-02).
- **Backfilled vintages are as-published-at-download:** a backfilled month is
  NHS's *final revision available at download time*, not the figure originally
  published in that month. Sidecars on `raw-backfill-*` releases record this.
  First-publication vintages for the backfill window are not recoverable from
  the landing pages; from 2026-07 onward the live 2×/day ingest captures every
  vintage as it appears.

## Cadence + staleness

- Monthly, pre-announced (usually a Thursday, ~2nd week), covering the month
  ~6–7 weeks prior. Polled 2×/day (07:23, 15:41 UTC) with checksum dedupe.
- Staleness gate: newest data month older than **150 days** blocks publication
  and pages the operator. The gate is N/A in backfill mode (historical by
  definition).

## Licence + attribution

Open Government Licence v3 (NHS England statistical work areas). Attribution:
"Source: NHS England, Consultant-led Referral to Treatment Waiting Times" +
month + link, shown beside every figure (YMYL source-beside-figure rule).

## Contingency plan

- **Landing page layout / URL pattern changes:** discovery fails loudly
  ("no Full-CSV-data-file link found") — publication blocks, last-good stays
  published with its "data as of" stamp; fix discovery deliberately.
- **Schema changes:** fingerprint gate blocks before parse; inspect the raw
  release asset, adapt the parser, refresh the recorded fingerprint in the
  same commit.
- **Source outage:** 2×/day polling self-heals transient failures; sustained
  absence surfaces via the staleness gate (and, once provisioned, the missed
  Healthchecks heartbeat).
- **Licence change tripwire:** any move away from OGL v3 on the RTT pages =
  stop ingest, escalate to owner (legal gate).
