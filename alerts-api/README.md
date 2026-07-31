# alerts-api — the alerts backend

The only always-on component of an otherwise static site (ARCHITECTURE ADR-01):
one Cloudflare Worker, one D1 database, six routes. Signup, double opt-in,
unsubscribe, and a Bearer-guarded read/write path for the send job.

## State today: proven locally, deploy owed

| | Status |
|---|---|
| Worker code | written, typechecked, runs under `wrangler dev --local` |
| D1 schema | applied to a local D1 on every drill run |
| Full loop | **proven end to end** by `scripts/alert_drill.sh`, in CI weekly |
| Deployed Worker + hosted D1 | **owed.** The account API token has Workers and R2 scope but not D1: `wrangler d1 list` returns `Authentication error [code: 10000]`. A denied permission is a stop, not something to work around, so the deploy waits for a token with D1 scope. |
| ESP account | **none, deliberately.** A proof-of-concept build opens no email accounts (STANDARD-email-alerts §0). |

Because the hidden preview has no public users, nothing is lost by the deploy
being owed: no live signup can exist before launch. The site's watch form reads
its endpoint from one config value (`site.alertsApi`) and, when that value is
empty, says plainly that alerts are not switched on rather than pretending.

## Running the whole thing locally

```bash
cd alerts-api && npm install
bash ../scripts/alert_drill.sh          # from the repo root: the full G4 proof
```

The drill starts `tools/mailcatcher.py` (the ESP stand-in), starts the Worker
with a local D1, then walks: signup → confirmation email captured → confirm →
synthetic material change through the real threshold module → alert email
captured → same-month rerun suppressed by `send_log` → RFC 8058 one-click
unsubscribe → a brand-new material month sends nothing. Every message is written
to `drill-evidence/mailcatch/*.eml` as a real RFC 5322 message.

## The one switch

Email leaves through `MAIL_API_URL` + `MAIL_API_TOKEN`, in Postmark's `/email`
shape. Locally those point at the catcher. At launch they point at the ESP and
nothing else changes — same templates, same headers, same consent records.
`MAIL_API_URL=""` is a legitimate third state: render and log, never pretend.

## Configuration

| Name | Where | Meaning |
|---|---|---|
| `ADMIN_SECRET` | secret | Bearer token the send job presents |
| `TOKEN_SECRET` | secret | HMAC key for confirm/unsubscribe tokens |
| `SITE_ORIGIN` | var | where result pages live (links in emails) |
| `API_ORIGIN` | var | this Worker's origin (confirm/unsubscribe links) |
| `MAIL_API_URL` / `MAIL_API_TOKEN` / `MAIL_FROM` | var / secret | the send switch |
| `TURNSTILE_SECRET` | secret, optional | absent = local mode, human check skipped rather than silently passed |
| `ALLOWED_ORIGINS` | var | CORS allowlist; `*` locally |

## What the schema is for

`schema.sql` is shaped by PECR/UK GDPR evidence rules, not by convenience. The
question a regulator asks is "show me what this person was shown when they
consented" — so `consent_wordings` stores the exact string, versioned, and
`consent_events` records who/when/how/from where against that version.
`send_log`'s `UNIQUE (watch_id, month)` is the idempotency guarantee: the send
job claims before it sends, so a rerun claims nothing and sends nothing.

## Deploy, when the token allows it

```bash
wrangler d1 create turnbeck-alerts             # put the id in wrangler.toml
wrangler d1 execute turnbeck-alerts --file schema.sql
wrangler secret put ADMIN_SECRET
wrangler secret put TOKEN_SECRET
wrangler deploy
```

Then set `ALERTS_API_URL` / `ALERTS_ADMIN_SECRET` / `MAIL_API_URL` /
`MAIL_API_TOKEN` as repo secrets — the `alerts` step in `ingest.yml` skips
itself until they exist — build the site with `ALERTS_API=<worker origin>`, and
re-run the drill against the deployed pair.
