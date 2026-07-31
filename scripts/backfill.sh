#!/usr/bin/env bash
# Supervised 24-month backfill driver (ARCHITECTURE ADR-04).
#
# Usage: backfill.sh <from YYYY-MM> [<to YYYY-MM>]
# Iterates oldest -> newest. Per month: discover + fetch (checksum dedupe) ->
# raw-archive as Release raw-backfill-YYYY-MM (+ sidecar) -> ALL gates ->
# append to the accumulated normalised layer -> csv-diff evidence -> commit.
# A month failing gates is SKIPPED AND LOGGED (state/backfill_log.jsonl),
# never force-parsed; the loop continues with the next month.
#
# Loop mode (one runner for the whole range) was chosen over 24 separate
# workflow dispatches to cap Actions time: one checkout/setup, sequential
# months, one commit per published month so every vintage is a distinct,
# diffable version in git history.
set -u -o pipefail

FROM="${1:?usage: backfill.sh <from YYYY-MM> [<to YYYY-MM>]}"
TO="${2:-$FROM}"
[[ "$FROM" =~ ^[0-9]{4}-(0[1-9]|1[0-2])$ ]] || { echo "::error::bad from month $FROM"; exit 2; }
[[ "$TO"   =~ ^[0-9]{4}-(0[1-9]|1[0-2])$ ]] || { echo "::error::bad to month $TO"; exit 2; }

months() {
  local y=${FROM%-*} m=${FROM#*-} ty=${TO%-*} tm=${TO#*-}
  m=$((10#$m)); tm=$((10#$tm))
  while (( y < ty || (y == ty && m <= tm) )); do
    printf '%04d-%02d\n' "$y" "$m"
    if (( m == 12 )); then y=$((y+1)); m=1; else m=$((m+1)); fi
  done
}

git config user.name "nhswaits-pipeline"
git config user.email "actions@users.noreply.github.com"

published=0 skipped=0 noop=0

log_skip() {  # $1=month $2=stage $3=reason-file(optional)
  python3 - "$1" "$2" "${3:-}" <<'EOF'
import json, sys
from datetime import datetime, timezone
month, stage, logfile = sys.argv[1], sys.argv[2], sys.argv[3]
reason = ""
if logfile:
    try:
        lines = [l.strip() for l in open(logfile, errors="replace") if l.strip()]
        reason = " | ".join(lines[-3:])[:500]
    except OSError:
        pass
entry = {"month": month, "skipped_at": datetime.now(timezone.utc).isoformat(),
         "stage": stage, "reason": reason}
with open("state/backfill_log.jsonl", "a") as f:
    f.write(json.dumps(entry) + "\n")
print(f"::warning::backfill {month} SKIPPED at {stage}: {reason[:200]}")
EOF
  git add state/backfill_log.jsonl
  git commit -q -m "backfill: $1 skipped ($2)" && git push -q
}

for M in $(months); do
  echo "=================================================================="
  echo "=== backfill $M"
  echo "=================================================================="
  rm -rf work
  export BACKFILL_MONTH="$M"
  export GITHUB_OUTPUT="$PWD/work_outputs.env"
  : > "$GITHUB_OUTPUT"

  if ! python3 scripts/pipeline.py fetch 2>&1 | tee work_fetch.log; then
    log_skip "$M" "fetch" work_fetch.log
    skipped=$((skipped+1)); continue
  fi
  changed=$(grep -oP '(?<=^changed=).*' "$GITHUB_OUTPUT" | tail -1)
  if [ "$changed" != "true" ]; then
    echo "--- $M already archived (checksum dedupe) — no-op"
    noop=$((noop+1)); continue
  fi
  tag=$(grep -oP '(?<=^tag=).*' "$GITHUB_OUTPUT" | tail -1)

  # Raw layer FIRST (archive raw ALWAYS, before parse/gates). Never overwrite
  # an existing tag: if it exists the bytes are already safely archived.
  if gh release view "$tag" --repo "$GITHUB_REPOSITORY" >/dev/null 2>&1; then
    echo "--- release $tag already exists (raw already archived)"
  else
    gh release create "$tag" work/raw.zip work/sidecar.json \
      --repo "$GITHUB_REPOSITORY" \
      --title "Raw snapshot $tag" \
      --notes "Historical backfill vintage for $M (as published at download time — NHS's final revision to date). Untouched source bytes + sidecar (fetched_at, source_url, sha256, HTTP headers). Immutable raw layer." \
      || { log_skip "$M" "raw-archive" ""; skipped=$((skipped+1)); continue; }
  fi

  if ! python3 scripts/pipeline.py process 2>&1 | tee work_process.log; then
    log_skip "$M" "gates" work_process.log
    skipped=$((skipped+1)); continue
  fi

  STAMP="$(date -u +%Y%m%d-%H%M%S)"
  mkdir -p data/diffs
  if git show HEAD:data/normalised/rtt_trust_specialty.csv > /tmp/prev.csv 2>/dev/null \
     && [ -s /tmp/prev.csv ]; then
    csv-diff /tmp/prev.csv data/normalised/rtt_trust_specialty.csv \
      --key=row_key > "data/diffs/${STAMP}.txt" || true
    head -5 "data/diffs/${STAMP}.txt"
  else
    echo "first publish - no previous version to diff" > "data/diffs/${STAMP}.txt"
  fi

  git add data state
  git commit -q -m "backfill: $M (raw $tag)"
  git push -q || { echo "::error::git push failed for $M"; exit 1; }
  published=$((published+1))
  echo "--- $M published"
done

rm -f work_fetch.log work_process.log work_outputs.env
echo "=================================================================="
echo "backfill complete: published=$published skipped=$skipped noop=$noop"
echo "=================================================================="
if [ -s state/backfill_log.jsonl ]; then
  echo "skip log:"
  cat state/backfill_log.jsonl
fi
