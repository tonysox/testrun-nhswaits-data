#!/usr/bin/env python3
"""Diff-confinement proof for a full recompute (W0c).

Usage: recompute_verify.py <before.csv> <after.csv>

HARD-FAILS (exit 1) unless, comparing the pre-recompute and recomputed
normalised CSVs:
  - row counts are identical;
  - the ordered row_key sequences are identical;
  - every column EXCEPT pct_within_18_weeks is byte-identical row by row.

Writes the keyed audit evidence (per-column change counts + per-month pct
fill coverage + a change sample) to data/diffs/<stamp>-recompute-pct.txt —
this replaces the raw csv-diff dump for this publish: 100k+ single-column
changes would be a ~15MB text file, and the confinement assertion here is
the stronger, exact statement of the same evidence.
"""
import csv
import sys
from collections import Counter
from datetime import datetime, timezone

ALLOWED = {"pct_within_18_weeks"}


def main(before_path, after_path):
    with open(before_path, newline="") as f:
        before = list(csv.DictReader(f))
    with open(after_path, newline="") as f:
        after = list(csv.DictReader(f))

    errors = []
    if len(before) != len(after):
        errors.append(f"row count changed: {len(before)} -> {len(after)}")
    keys_b = [r["row_key"] for r in before]
    keys_a = [r["row_key"] for r in after]
    if keys_b != keys_a:
        sb, sa = set(keys_b), set(keys_a)
        errors.append(f"row_key sequence changed: {len(sa - sb)} added, "
                      f"{len(sb - sa)} removed, order_equal={keys_b == keys_a}")

    col_changes = Counter()
    sample = []
    if not errors:
        for b, a in zip(before, after):
            for col in b:
                if b[col] != a[col]:
                    col_changes[col] += 1
                    if col in ALLOWED and len(sample) < 10:
                        sample.append((b["row_key"], b[col], a[col]))
    illegal = {c: n for c, n in col_changes.items() if c not in ALLOWED}
    if illegal:
        errors.append(f"changes OUTSIDE the pct column: {illegal}")

    pct_before = sum(1 for r in before if r["pct_within_18_weeks"].strip())
    pct_after = sum(1 for r in after if r["pct_within_18_weeks"].strip())
    by_month = Counter()
    filled_by_month = Counter()
    for r in after:
        by_month[r["month"]] += 1
        if r["pct_within_18_weeks"].strip():
            filled_by_month[r["month"]] += 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = f"data/diffs/{stamp}-recompute-pct.txt"
    with open(out, "w") as f:
        f.write("W0c full recompute from archived raw releases — "
                "diff-confinement evidence\n")
        f.write(f"generated_at: {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"rows before={len(before)} after={len(after)} "
                f"(identical={len(before) == len(after)})\n")
        f.write(f"row_key sequence identical: {keys_b == keys_a}\n")
        f.write(f"changed cells by column: {dict(col_changes)}\n")
        f.write(f"pct_within_18_weeks filled: {pct_before} -> {pct_after} "
                f"of {len(after)} rows\n")
        f.write("pct coverage by month (filled/rows):\n")
        for m in sorted(by_month):
            f.write(f"  {m}: {filled_by_month[m]}/{by_month[m]}\n")
        f.write("sample changes (row_key: before -> after):\n")
        for key, vb, va in sample:
            f.write(f"  {key}: {vb!r} -> {va!r}\n")
        if errors:
            f.write("FAILED:\n")
            for e in errors:
                f.write(f"  {e}\n")
    print(open(out).read())

    if errors:
        for e in errors:
            print(f"::error::recompute verify: {e}")
        sys.exit(1)
    if pct_after == 0:
        print("::error::recompute verify: pct column still entirely empty")
        sys.exit(1)
    print(f"recompute verify OK — evidence: {out}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: recompute_verify.py <before.csv> <after.csv>")
        sys.exit(2)
    main(sys.argv[1], sys.argv[2])
