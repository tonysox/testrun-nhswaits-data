#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# ALERT DRILL — the AC §G4 end-to-end proof, runnable on a laptop with no
# accounts anywhere (STANDARD-email-alerts §0).
#
#   signup -> double-opt-in confirmation email -> confirm -> material change
#   detected -> alert email rendered and captured -> one-click unsubscribe ->
#   re-run produces NO email -> consent + send_log rows exhibited
#
# Everything runs locally: the alerts-api Worker under `wrangler dev --local`
# with a local D1, and tools/mailcatcher.py standing in for the ESP. The ONLY
# difference at launch is MAIL_API_URL/MAIL_API_TOKEN and a deployed Worker.
#
# Usage: bash scripts/alert_drill.sh [evidence-dir]
# ---------------------------------------------------------------------------
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EVIDENCE="${1:-$REPO_ROOT/drill-evidence}"
API="http://127.0.0.1:8787"
CATCHER="http://127.0.0.1:1080"
ADMIN_SECRET="local-drill-admin-secret"
TEST_EMAIL="drill-subscriber@example.invalid"
PROVIDER="RCF"           # Airedale NHS Foundation Trust
SPECIALTY="C_410"        # Joint and muscle conditions
ENTITY="${PROVIDER}|${SPECIALTY}"
LABEL="Joint and muscle conditions at Airedale NHS Foundation Trust"
PAGE="https://testrun-nhswaits.pages.dev/hospital/airedale-nhs-foundation-trust/joint-and-muscle-conditions/"
WORDING_VERSION="v1"
WORDING_TEXT="One plain email when this figure materially changes — when the typical wait moves by a week or more, or the number of people waiting changes by 10% or at least 25 people. No newsletter, no marketing. Unsubscribe in one click."

mkdir -p "$EVIDENCE"
MAILDIR="$EVIDENCE/mailcatch"
rm -rf "$MAILDIR"; mkdir -p "$MAILDIR"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

# Both helpers are started in their own process GROUP and the group is killed on
# exit. `wrangler dev` spawns a workerd child that outlives a plain kill of the
# npx wrapper, and a survivor holding port 8787 against a deleted local D1 file
# is exactly how a rerun turns into a mystery (SQLITE_CANTOPEN). Kill any
# survivor of a previous drill before starting, by exact command line.
pgids=()
cleanup() { for g in "${pgids[@]:-}"; do kill -- "-$g" 2>/dev/null || true; done; }
trap cleanup EXIT
pkill -f 'wrangler dev --local --port 8787' 2>/dev/null || true
pkill -f 'tools/mailcatcher.py' 2>/dev/null || true
sleep 1
for port in 8787 1080; do
  if curl -sf -m 2 "http://127.0.0.1:$port/" >/dev/null 2>&1; then
    echo "port $port is still in use by something we did not start — stopping"; exit 1
  fi
done

# --- 0. fresh local D1 ------------------------------------------------------
step "0. local D1: apply schema (fresh state for the drill)"
cd "$REPO_ROOT/alerts-api"
rm -rf .wrangler/state
env -u CLOUDFLARE_API_TOKEN -u CLOUDFLARE_ACCOUNT_ID \
  npx wrangler d1 execute turnbeck-alerts --local --file schema.sql -y >/dev/null
echo "schema applied to the local D1"

# --- 1. mail catcher --------------------------------------------------------
step "1. start the local mail catcher (no ESP account exists)"
setsid python3 "$REPO_ROOT/tools/mailcatcher.py" --port 1080 --dir "$MAILDIR" \
  > "$EVIDENCE/mailcatcher.log" 2>&1 &
pgids+=($!)
for _ in $(seq 1 30); do curl -sf "$CATCHER/health" >/dev/null && break; sleep 0.5; done
curl -sf "$CATCHER/health"; echo

# --- 2. the Worker ----------------------------------------------------------
step "2. start alerts-api under wrangler dev --local (real Worker runtime, local D1)"
setsid env -u CLOUDFLARE_API_TOKEN -u CLOUDFLARE_ACCOUNT_ID \
  npx wrangler dev --local --port 8787 > "$EVIDENCE/worker.log" 2>&1 &
pgids+=($!)
for _ in $(seq 1 60); do curl -sf "$API/api/health" >/dev/null && break; sleep 1; done
curl -sf "$API/api/health"; echo

# --- 3. signup --------------------------------------------------------------
step "3. signup (exactly what the watch-this island posts)"
curl -sf -X POST "$API/api/subscribe" -H 'Content-Type: application/json' \
  -d "$(python3 -c '
import json,sys
print(json.dumps({"email":sys.argv[1],"entity":sys.argv[2],"label":sys.argv[3],
 "wording_version":sys.argv[4],"wording_text":sys.argv[5],"page_url":sys.argv[6]}))
' "$TEST_EMAIL" "$ENTITY" "$LABEL" "$WORDING_VERSION" "$WORDING_TEXT" "$PAGE")" \
  | tee "$EVIDENCE/01-subscribe-response.json"; echo

step "3b. the confirmation email as captured"
curl -sf "$CATCHER/messages" > "$EVIDENCE/02-messages-after-signup.json"
python3 - "$EVIDENCE/02-messages-after-signup.json" <<'PY'
import json,sys
msgs=json.load(open(sys.argv[1]))
assert len(msgs)==1, f"expected 1 captured message, got {len(msgs)}"
m=msgs[0]
print(f"--- {m['path']}  (stream: {m['stream']}) ---")
print(m['eml'])
PY

step "3c. state before confirming: subscriber pending, watch inactive"
curl -sf -H "Authorization: Bearer $ADMIN_SECRET" "$API/api/admin/export" \
  > "$EVIDENCE/03-export-before-confirm.json"
python3 - "$EVIDENCE/03-export-before-confirm.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
print("subscribers:", json.dumps(d["subscribers"], indent=2))
print("watches:", json.dumps(d["watches"], indent=2))
w=[x for x in d["watches"]]
assert d["subscribers"][0]["status"]=="pending", "subscriber should be pending before confirm"
assert w[0]["active"]==0, "watch must be inactive until the double opt-in is confirmed"
print("OK: nothing is active before the confirmation click")
PY

step "3d. the send job BEFORE confirmation must email nobody"
cd "$REPO_ROOT"
python3 scripts/make_drill_deltas.py --entity "$PROVIDER" "$SPECIALTY" \
  --median-delta 2.5 --out "$EVIDENCE/drill-deltas.json" | tee "$EVIDENCE/04-synthetic-deltas.txt"
PYTHONPATH=scripts python3 scripts/alerts_send.py \
  --deltas "$EVIDENCE/drill-deltas.json" --api "$API" --admin-secret "$ADMIN_SECRET" \
  --mail-url "$CATCHER/email" --mail-token unused \
  | tee "$EVIDENCE/05-send-before-confirm.txt"
COUNT=$(curl -sf "$CATCHER/messages" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))')
[ "$COUNT" = "1" ] || { echo "FAIL: an unconfirmed subscriber was emailed"; exit 1; }
echo "OK: still 1 captured message (the confirmation) — an unconfirmed watch is never alerted"

# --- 4. confirm -------------------------------------------------------------
step "4. click the confirmation link from the email"
CONFIRM_URL=$(python3 - "$EVIDENCE/02-messages-after-signup.json" <<'PY'
import json,re,sys
m=json.load(open(sys.argv[1]))[0]
print(re.search(r'http://\S*?/api/confirm\?token=[A-Za-z0-9._-]+', m['text']).group(0))
PY
)
echo "confirm URL from the email body: $CONFIRM_URL"
curl -sf "$CONFIRM_URL" > "$EVIDENCE/06-confirm-page.html"
grep -o "That's confirmed." "$EVIDENCE/06-confirm-page.html" || { echo "FAIL: confirm page"; exit 1; }

step "4b. consent evidence after confirming"
curl -sf -H "Authorization: Bearer $ADMIN_SECRET" "$API/api/admin/export" \
  > "$EVIDENCE/07-export-after-confirm.json"
python3 - "$EVIDENCE/07-export-after-confirm.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
print("subscribers:", json.dumps(d["subscribers"], indent=2))
print("watches:", json.dumps(d["watches"], indent=2))
print("consent_wordings:", json.dumps(d["consent_wordings"], indent=2))
print("consent_events:", json.dumps(d["consent_events"], indent=2))
assert d["subscribers"][0]["status"]=="active"
assert d["watches"][0]["active"]==1
events=[e["event"] for e in d["consent_events"]]
assert events==["signup","confirm"], events
assert d["consent_events"][0]["wording_version"]=="v1"
assert d["consent_wordings"][0]["text"].startswith("One plain email")
print("OK: who / when / how / from where / exact wording are all on record")
PY

# --- 5. the alert ------------------------------------------------------------
step "5. run the send job against the synthetic material change"
PYTHONPATH=scripts python3 scripts/alerts_send.py \
  --deltas "$EVIDENCE/drill-deltas.json" --api "$API" --admin-secret "$ADMIN_SECRET" \
  --mail-url "$CATCHER/email" --mail-token unused \
  | tee "$EVIDENCE/08-send-run-1.txt"

step "5b. THE ALERT EMAIL, exactly as a mail client would receive it"
curl -sf "$CATCHER/messages" > "$EVIDENCE/09-messages-after-alert.json"
python3 - "$EVIDENCE/09-messages-after-alert.json" <<'PY'
import json,sys
msgs=json.load(open(sys.argv[1]))
assert len(msgs)==2, f"expected 2 captured messages, got {len(msgs)}"
m=msgs[1]
print(f"--- {m['path']}  (stream: {m['stream']}) ---")
print(m['eml'])
assert m['headers'].get('List-Unsubscribe'), "RFC 8058: List-Unsubscribe header missing"
assert m['headers'].get('List-Unsubscribe-Post')=="List-Unsubscribe=One-Click"
body=m['text'].lower()
for banned in ("sign up","subscribe to","our other","newsletter","offer","sponsor","discount"):
    assert banned not in body, f"promotional wording in a service message: {banned}"
print("OK: one-click unsubscribe headers present; no promotional content (PECR mixed-content rule)")
PY

step "5c. re-run the SAME month — send_log must suppress a second email"
PYTHONPATH=scripts python3 scripts/alerts_send.py \
  --deltas "$EVIDENCE/drill-deltas.json" --api "$API" --admin-secret "$ADMIN_SECRET" \
  --mail-url "$CATCHER/email" --mail-token unused \
  | tee "$EVIDENCE/10-send-run-2-same-month.txt"
COUNT=$(curl -sf "$CATCHER/messages" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))')
[ "$COUNT" = "2" ] || { echo "FAIL: the rerun sent again ($COUNT messages)"; exit 1; }
echo "OK: still 2 captured messages — idempotent by send_log UNIQUE(watch_id, month)"

# --- 6. unsubscribe ----------------------------------------------------------
step "6. one-click unsubscribe, driven from the email's List-Unsubscribe header"
UNSUB_URL=$(python3 - "$EVIDENCE/09-messages-after-alert.json" <<'PY'
import json,sys
m=json.load(open(sys.argv[1]))[1]
print(m['headers']['List-Unsubscribe'].strip('<>'))
PY
)
echo "List-Unsubscribe: $UNSUB_URL"
echo "-- GET (the human page) --"
curl -sf "$UNSUB_URL" | grep -o "Yes, stop emailing me" || { echo "FAIL: unsubscribe page"; exit 1; }
echo "-- POST (RFC 8058 one-click, what the mail client fires) --"
curl -sf -X POST "$UNSUB_URL" -H 'Content-Type: application/x-www-form-urlencoded' \
  -d 'List-Unsubscribe=One-Click' | tee "$EVIDENCE/11-unsubscribe-post.txt"; echo

# --- 7. a NEW month must still send nothing ---------------------------------
step "7. a fresh month with another material change — an unsubscribed person gets nothing"
python3 scripts/make_drill_deltas.py --entity "$PROVIDER" "$SPECIALTY" \
  --median-delta 4.0 --month 2026-07 --out "$EVIDENCE/drill-deltas-next.json" \
  | tee "$EVIDENCE/12-synthetic-deltas-next.txt"
PYTHONPATH=scripts python3 scripts/alerts_send.py \
  --deltas "$EVIDENCE/drill-deltas-next.json" --api "$API" --admin-secret "$ADMIN_SECRET" \
  --mail-url "$CATCHER/email" --mail-token unused \
  | tee "$EVIDENCE/13-send-run-3-after-unsubscribe.txt"
COUNT=$(curl -sf "$CATCHER/messages" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))')
[ "$COUNT" = "2" ] || { echo "FAIL: an unsubscribed subscriber was emailed ($COUNT messages)"; exit 1; }
echo "OK: still 2 captured messages — the unsubscribe holds for a brand-new material change"

# --- 8. final records --------------------------------------------------------
step "8. final consent + send_log records"
curl -sf -H "Authorization: Bearer $ADMIN_SECRET" "$API/api/admin/export" \
  > "$EVIDENCE/14-export-final.json"
python3 - "$EVIDENCE/14-export-final.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
for k in ("subscribers","watches","consent_wordings","consent_events","send_log"):
    print(f"{k}:", json.dumps(d[k], indent=2))
assert d["subscribers"][0]["status"]=="unsubscribed"
assert d["subscribers"][0]["unsubscribed_at"]
assert d["watches"][0]["active"]==0
assert [e["event"] for e in d["consent_events"]]==["signup","confirm","unsubscribe"]
assert d["consent_events"][-1]["method"]=="one-click-post"
assert len(d["send_log"])==1 and d["send_log"][0]["sent_at"]
print("OK: full consent trail, one send logged, subscriber unsubscribed")
PY

printf '\n\033[1mALERT DRILL PASSED\033[0m — evidence in %s\n' "$EVIDENCE"
