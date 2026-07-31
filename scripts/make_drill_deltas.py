#!/usr/bin/env python3
"""Build a SYNTHETIC next-month deltas document for the alert drill (ADR-02 §E2E).

Deliberately NOT a hand-written JSON fixture. It reads the real accumulated
normalised layer, copies the latest month forward as a synthetic next month,
injects ONE engineered movement into ONE entity, and then runs the REAL
threshold module (scripts/alerts_thresholds.py) over both months. So the drill
exercises the same materiality code that production will run — a fixture would
have proved only that JSON parses.

The output is written to a scratch path; the published data/deltas/latest.json
is never touched.

  python3 scripts/make_drill_deltas.py --entity RCF C_410 --median-delta 2.5 \
      --out /tmp/drill-deltas.json
"""
import argparse
import csv
import json
import sys

from alerts_thresholds import compute_deltas, entity_key, prev_calendar_month

FIELDS = ["month", "provider_code", "specialty_code", "waiting_list",
          "median_wait_weeks_est"]


def next_calendar_month(month):
    y, m = int(month[:4]), int(month[5:7])
    return f"{y + 1}-01" if m == 12 else f"{y}-{m + 1:02d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/normalised/rtt_trust_specialty.csv")
    ap.add_argument("--entity", nargs=2, metavar=("PROVIDER", "SPECIALTY"), required=True)
    ap.add_argument("--median-delta", type=float, default=2.5,
                    help="weeks to add to the target entity in the synthetic month")
    ap.add_argument("--month", default=None, help="synthetic month (default: latest + 1)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    target = entity_key(*args.entity)

    with open(args.csv, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        months = set()
        latest_rows = []
        rows_by_month = {}
        for r in reader:
            months.add(r["month"])
            rows_by_month.setdefault(r["month"], []).append(r)
    latest = max(months)
    latest_rows = rows_by_month[latest]
    synth_month = args.month or next_calendar_month(latest)

    # The baseline is always relabelled as the synthetic month's PREVIOUS
    # calendar month, so the deltas document is a true month-on-month pair even
    # when the drill jumps ahead (run 3 uses a later month to prove the
    # unsubscribe holds for a brand-new change).
    base_month = prev_calendar_month(synth_month)
    base = []
    synth = []
    found = False
    for r in latest_rows:
        keep = {k: r[k] for k in FIELDS}
        prev_row = dict(keep)
        prev_row["month"] = base_month
        base.append(prev_row)
        nxt = dict(keep)
        nxt["month"] = synth_month
        if entity_key(r["provider_code"], r["specialty_code"]) == target:
            found = True
            med = r["median_wait_weeks_est"].strip()
            if med == "":
                print(f"target {target} has no median in {latest} — cannot inject a movement",
                      file=sys.stderr)
                return 2
            nxt["median_wait_weeks_est"] = f"{float(med) + args.median_delta:.1f}"
        synth.append(nxt)

    if not found:
        print(f"target {target} not present in {latest}", file=sys.stderr)
        return 2

    doc = compute_deltas(base + synth)
    assert doc["month"] == synth_month, doc["month"]
    assert doc["prev_month"] == prev_calendar_month(synth_month)

    hit = next(e for e in doc["entities"] if e["key"] == target)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)

    material = [e for e in doc["entities"] if e.get("material")]
    print(f"synthetic month {synth_month} (baseline {base_month} copied from real {latest}): "
          f"{len(doc['entities'])} entities, {len(material)} material")
    print(f"  target {target}: median {hit['prev']['med']} -> {hit['curr']['med']} "
          f"(delta {hit['delta_med']}), material={hit['material']}, reasons={hit['reasons']}")
    print(f"  written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
