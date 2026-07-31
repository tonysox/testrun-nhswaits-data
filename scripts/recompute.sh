#!/usr/bin/env bash
# Recompute the ENTIRE normalised layer from the archived raw Releases (W0c).
#
# Why: pct_within_18_weeks shipped empty for every row (denominator bug —
# see pct_within_18 in pipeline.py). The raw layer is immutable and complete,
# so the honest fix is to re-derive all vintages from the archived bytes,
# prove the only change is the pct column filling, and publish once.
#
# How: replays state/raw_checksums.txt IN ITS ORIGINAL APPEND ORDER (the
# authoritative ingest sequence — latest-vintage-wins semantics reproduce
# exactly). Per vintage: download the Release asset -> verify sha256 against
# the ledger -> stage work/{raw.zip,meta.json} -> pipeline.py process with
# RECOMPUTE=true (all gates run; state ledgers untouched). Any failure ABORTS
# the whole recompute (unlike backfill there is no skip-and-continue: a
# recompute that cannot reproduce a vintage must not publish).
#
# Afterwards scripts/recompute_verify.py proves diff-confinement (identical
# row_keys/row count, non-pct columns byte-identical) and writes the keyed
# evidence file into data/diffs/. The workflow commits only if that passes.
set -euo pipefail

REPO="${GITHUB_REPOSITORY:-tonysox/testrun-nhswaits-data}"
LEDGER="state/raw_checksums.txt"
[ -s "$LEDGER" ] || { echo "::error::$LEDGER missing/empty"; exit 1; }

# Snapshot the currently-published CSV for the diff-confinement proof.
cp data/normalised/rtt_trust_specialty.csv work_before_recompute.csv

n=0
while read -r sha tag url; do
  [ -n "${sha:-}" ] || continue
  n=$((n+1))
  echo "=================================================================="
  echo "=== recompute vintage $n: $tag"
  echo "=================================================================="
  rm -rf work && mkdir -p work

  # Stream to stdout and let the shell write the file (portable: also works
  # where gh itself is filesystem-confined, e.g. a snap install).
  gh release download "$tag" --repo "$REPO" --pattern raw.zip --output - \
    > work/raw.zip

  got=$(sha256sum work/raw.zip | cut -d' ' -f1)
  if [ "$got" != "$sha" ]; then
    echo "::error::raw integrity: $tag asset sha256 $got != ledger $sha"
    exit 1
  fi
  echo "--- sha256 verified against ledger ($sha)"

  python3 - "$sha" "$tag" "$url" <<'EOF'
import json, sys
sha, tag, url = sys.argv[1:4]
with open("work/meta.json", "w") as f:
    json.dump({"sha256": sha, "tag": tag, "source_url": url}, f)
EOF

  if [[ "$tag" == raw-backfill-* ]]; then
    export BACKFILL_MONTH="${tag#raw-backfill-}"
  else
    unset BACKFILL_MONTH || true
  fi
  RECOMPUTE=true python3 scripts/pipeline.py process
done < "$LEDGER"

echo "=================================================================="
echo "recompute complete: $n vintages re-derived"
echo "=================================================================="

python3 scripts/recompute_verify.py work_before_recompute.csv \
  data/normalised/rtt_trust_specialty.csv
