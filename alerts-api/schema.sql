-- alerts-api store (ARCHITECTURE ADR-01). D1 / SQLite.
--
-- The shape is dictated by PECR + UK GDPR evidence rules (STANDARD-email-alerts
-- §3): for every subscriber we must be able to show WHO consented, WHEN, HOW,
-- from WHERE, and THE EXACT WORDING THEY WERE SHOWN. That is why the wording is
-- a versioned row of its own rather than a string baked into a template.

CREATE TABLE IF NOT EXISTS subscribers (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  email              TEXT NOT NULL UNIQUE,
  status             TEXT NOT NULL CHECK (status IN ('pending', 'active', 'unsubscribed', 'suppressed')),
  created_at         TEXT NOT NULL,
  confirmed_at       TEXT,
  unsubscribed_at    TEXT,
  -- two-rail law (STANDARD-email-alerts §1): the digest rail exists in the
  -- schema and is never ticked in v1. No UI, no sends, no scope creep.
  digest_opt_in      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS watches (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  subscriber_id  INTEGER NOT NULL REFERENCES subscribers(id),
  entity_key     TEXT NOT NULL,             -- provider_code|specialty_code (ADR-02)
  label          TEXT NOT NULL,             -- human words, echoed back in the email
  page_path      TEXT,                      -- where the visitor signed up
  threshold_kind TEXT NOT NULL DEFAULT 'material',
  active         INTEGER NOT NULL DEFAULT 0, -- 0 until the double opt-in confirms
  created_at     TEXT NOT NULL,
  UNIQUE (subscriber_id, entity_key)
);
CREATE INDEX IF NOT EXISTS watches_entity ON watches(entity_key, active);

-- The exact wording shown, versioned. first_used_at is when we first served it.
CREATE TABLE IF NOT EXISTS consent_wordings (
  version       TEXT PRIMARY KEY,
  text          TEXT NOT NULL,
  first_used_at TEXT NOT NULL
);

-- The Art-7 / PECR evidence trail. Append-only by contract.
CREATE TABLE IF NOT EXISTS consent_events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  subscriber_id   INTEGER NOT NULL REFERENCES subscribers(id),
  watch_id        INTEGER REFERENCES watches(id),
  event           TEXT NOT NULL CHECK (event IN ('signup', 'confirm', 'unsubscribe')),
  at              TEXT NOT NULL,
  ip              TEXT,
  user_agent      TEXT,
  page_url        TEXT,
  wording_version TEXT REFERENCES consent_wordings(version),
  method          TEXT                      -- 'form', 'link', 'one-click-post'
);
CREATE INDEX IF NOT EXISTS consent_events_sub ON consent_events(subscriber_id);

-- Idempotency for the send job: one alert per watch per data month, ever.
CREATE TABLE IF NOT EXISTS send_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  watch_id      INTEGER NOT NULL REFERENCES watches(id),
  month         TEXT NOT NULL,
  delta_summary TEXT,
  message_id    TEXT,
  claimed_at    TEXT NOT NULL,
  sent_at       TEXT,
  UNIQUE (watch_id, month)
);

-- Per-IP rate limiting on the only public write endpoint.
CREATE TABLE IF NOT EXISTS rate_limit (
  ip           TEXT NOT NULL,
  window_start TEXT NOT NULL,
  count        INTEGER NOT NULL,
  PRIMARY KEY (ip, window_start)
);
