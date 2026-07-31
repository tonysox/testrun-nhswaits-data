/**
 * alerts-api — the whole always-on surface of an otherwise static site
 * (ARCHITECTURE ADR-01). One Worker, one D1 database, six routes.
 *
 *   POST /api/subscribe          capture + double-opt-in confirmation email
 *   GET  /api/confirm?token=      the click that IS the Art-7 consent record
 *   GET  /api/unsubscribe?token=  human page with a one-click button
 *   POST /api/unsubscribe         RFC 8058 List-Unsubscribe-Post target
 *   GET  /api/admin/watches       send job reads active watches (Bearer)
 *   POST /api/admin/send-log      send job claims/records a send   (Bearer)
 *   GET  /api/admin/export        consent evidence dump            (Bearer)
 *
 * Email leaves through ONE switch: MAIL_API_URL + MAIL_API_TOKEN, shaped like
 * Postmark's /email endpoint. In a proof-of-concept build that points at the
 * local mail catcher (tools/mailcatcher.py) and NO account exists anywhere; at
 * launch the same two variables point at the real ESP. Nothing else changes.
 */

export interface Env {
  DB: D1Database;
  ADMIN_SECRET: string;
  TOKEN_SECRET: string;
  SITE_ORIGIN: string; // where result pages live (links in emails)
  API_ORIGIN: string; // where this Worker lives (confirm/unsubscribe links)
  MAIL_API_URL: string; // '' = render but do not send (safe default)
  MAIL_API_TOKEN: string;
  MAIL_FROM: string;
  TURNSTILE_SECRET?: string; // absent = local mode, human-check skipped
  ALLOWED_ORIGINS?: string; // comma-separated; '*' in local mode
}

const RATE_LIMIT_PER_HOUR = 5;
const CONFIRM_TOKEN_DAYS = 7;

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

const nowIso = (): string => new Date().toISOString();

const enc = new TextEncoder();

function b64url(bytes: ArrayBuffer | Uint8Array): string {
  const arr = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let s = '';
  for (const b of arr) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function unb64url(s: string): Uint8Array {
  const pad = s.replace(/-/g, '+').replace(/_/g, '/');
  const bin = atob(pad + '='.repeat((4 - (pad.length % 4)) % 4));
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}

async function hmac(secret: string, data: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    'raw',
    enc.encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  );
  return b64url(await crypto.subtle.sign('HMAC', key, enc.encode(data)));
}

interface TokenPayload {
  s: number; // subscriber id
  w?: number; // watch id
  a: 'confirm' | 'unsub';
  e?: number; // expiry (epoch seconds); unsubscribe tokens never expire
}

async function mintToken(secret: string, payload: TokenPayload): Promise<string> {
  const body = b64url(enc.encode(JSON.stringify(payload)));
  return `${body}.${await hmac(secret, body)}`;
}

async function readToken(secret: string, token: string): Promise<TokenPayload | null> {
  const [body, sig] = token.split('.');
  if (!body || !sig) return null;
  const expect = await hmac(secret, body);
  // constant-time-ish compare
  if (expect.length !== sig.length) return null;
  let diff = 0;
  for (let i = 0; i < expect.length; i++) diff |= expect.charCodeAt(i) ^ sig.charCodeAt(i);
  if (diff !== 0) return null;
  try {
    const payload = JSON.parse(new TextDecoder().decode(unb64url(body))) as TokenPayload;
    if (payload.e && payload.e < Math.floor(Date.now() / 1000)) return null;
    return payload;
  } catch {
    return null;
  }
}

const EMAIL_RE = /^[^@\s]+@[^@\s.]+\.[^@\s]+$/;

function json(data: unknown, status = 200, extra: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json', ...extra },
  });
}

function corsHeaders(env: Env, origin: string | null): Record<string, string> {
  const allowed = (env.ALLOWED_ORIGINS ?? '*').split(',').map((s) => s.trim());
  const allow = allowed.includes('*') ? (origin ?? '*') : allowed.includes(origin ?? '') ? origin! : '';
  if (!allow) return {};
  return {
    'Access-Control-Allow-Origin': allow,
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
    Vary: 'Origin',
  };
}

/** A plain HTML page in the site's voice. No tracking, no scripts. */
function page(title: string, bodyHtml: string, status = 200): Response {
  return new Response(
    `<!doctype html><html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>${title}</title>
<style>
  body{font-family:system-ui,sans-serif;max-width:38rem;margin:0 auto;padding:3rem 1.25rem;line-height:1.45;color:#241c18;background:#fdfaf8}
  h1{font-size:1.6rem;line-height:1.2}
  p{max-width:60ch}
  button{font:inherit;font-weight:600;color:#fdfaf8;background:#8a4322;border:0;border-radius:4px;padding:.75rem 1.25rem;min-height:44px;cursor:pointer}
  @media (prefers-color-scheme: dark){body{color:#eee5e0;background:#1a1614}button{color:#1a1614;background:#e0a483}}
</style></head><body>${bodyHtml}</body></html>`,
    { status, headers: { 'Content-Type': 'text/html; charset=utf-8', 'X-Robots-Tag': 'noindex, nofollow' } },
  );
}

// ---------------------------------------------------------------------------
// email
// ---------------------------------------------------------------------------

interface Mail {
  to: string;
  subject: string;
  text: string;
  headers?: Record<string, string>;
  stream: 'transactional' | 'broadcast';
}

/** The single send switch. Returns a message id, or null when no transport is
 *  configured (which is a legitimate state: render and log, never pretend). */
async function sendMail(env: Env, mail: Mail): Promise<string | null> {
  if (!env.MAIL_API_URL) return null;
  const res = await fetch(env.MAIL_API_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Postmark-Server-Token': env.MAIL_API_TOKEN ?? '',
    },
    body: JSON.stringify({
      From: env.MAIL_FROM,
      To: mail.to,
      Subject: mail.subject,
      TextBody: mail.text,
      MessageStream: mail.stream,
      Headers: Object.entries(mail.headers ?? {}).map(([Name, Value]) => ({ Name, Value })),
    }),
  });
  if (!res.ok) throw new Error(`mail transport ${res.status}`);
  const body = (await res.json()) as { MessageID?: string };
  return body.MessageID ?? 'sent';
}

// ---------------------------------------------------------------------------
// routes
// ---------------------------------------------------------------------------

async function rateLimited(env: Env, ip: string): Promise<boolean> {
  const window = nowIso().slice(0, 13); // hour bucket
  await env.DB.prepare(
    `INSERT INTO rate_limit (ip, window_start, count) VALUES (?, ?, 1)
     ON CONFLICT(ip, window_start) DO UPDATE SET count = count + 1`,
  )
    .bind(ip, window)
    .run();
  const row = await env.DB.prepare('SELECT count FROM rate_limit WHERE ip = ? AND window_start = ?')
    .bind(ip, window)
    .first<{ count: number }>();
  return (row?.count ?? 0) > RATE_LIMIT_PER_HOUR;
}

async function verifyTurnstile(env: Env, token: string | undefined, ip: string): Promise<boolean> {
  if (!env.TURNSTILE_SECRET) return true; // local / proof-of-concept mode
  if (!token) return false;
  const res = await fetch('https://challenges.cloudflare.com/turnstile/v0/siteverify', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ secret: env.TURNSTILE_SECRET, response: token, remoteip: ip }),
  });
  const body = (await res.json()) as { success?: boolean };
  return body.success === true;
}

async function handleSubscribe(req: Request, env: Env): Promise<Response> {
  const ip = req.headers.get('CF-Connecting-IP') ?? req.headers.get('X-Forwarded-For') ?? 'local';
  const ua = req.headers.get('User-Agent') ?? '';
  let body: Record<string, string>;
  try {
    body = (await req.json()) as Record<string, string>;
  } catch {
    return json({ error: 'bad_json' }, 400);
  }
  const email = (body.email ?? '').trim().toLowerCase();
  const entity = (body.entity ?? '').trim();
  const label = (body.label ?? '').trim();
  const wordingVersion = (body.wording_version ?? '').trim();
  const wordingText = (body.wording_text ?? '').trim();
  const pageUrl = (body.page_url ?? '').trim();

  if (!EMAIL_RE.test(email)) return json({ error: 'bad_email' }, 400);
  if (!/^[A-Za-z0-9]+\|[A-Za-z0-9_]+$/.test(entity)) return json({ error: 'bad_entity' }, 400);
  if (!wordingVersion || !wordingText) return json({ error: 'missing_consent_wording' }, 400);
  if (!(await verifyTurnstile(env, body.turnstile_token, ip))) return json({ error: 'human_check_failed' }, 403);
  if (await rateLimited(env, ip)) return json({ error: 'rate_limited' }, 429);

  const at = nowIso();
  // The exact wording shown, stored once per version.
  await env.DB.prepare(
    'INSERT OR IGNORE INTO consent_wordings (version, text, first_used_at) VALUES (?, ?, ?)',
  )
    .bind(wordingVersion, wordingText, at)
    .run();

  let sub = await env.DB.prepare('SELECT id, status FROM subscribers WHERE email = ?')
    .bind(email)
    .first<{ id: number; status: string }>();
  if (!sub) {
    const ins = await env.DB.prepare(
      "INSERT INTO subscribers (email, status, created_at) VALUES (?, 'pending', ?)",
    )
      .bind(email, at)
      .run();
    sub = { id: Number(ins.meta.last_row_id), status: 'pending' };
  } else if (sub.status === 'unsubscribed') {
    // A previously-unsubscribed address may sign up again — that is a fresh,
    // freshly-evidenced consent, not a resurrection of the old one.
    await env.DB.prepare("UPDATE subscribers SET status = 'pending', unsubscribed_at = NULL WHERE id = ?")
      .bind(sub.id)
      .run();
  }

  await env.DB.prepare(
    `INSERT INTO watches (subscriber_id, entity_key, label, page_path, active, created_at)
     VALUES (?, ?, ?, ?, 0, ?)
     ON CONFLICT(subscriber_id, entity_key) DO UPDATE SET label = excluded.label`,
  )
    .bind(sub.id, entity, label, pageUrl, at)
    .run();
  const watch = await env.DB.prepare(
    'SELECT id, active FROM watches WHERE subscriber_id = ? AND entity_key = ?',
  )
    .bind(sub.id, entity)
    .first<{ id: number; active: number }>();

  await env.DB.prepare(
    `INSERT INTO consent_events (subscriber_id, watch_id, event, at, ip, user_agent, page_url, wording_version, method)
     VALUES (?, ?, 'signup', ?, ?, ?, ?, ?, 'form')`,
  )
    .bind(sub.id, watch!.id, at, ip, ua, pageUrl, wordingVersion)
    .run();

  const token = await mintToken(env.TOKEN_SECRET, {
    s: sub.id,
    w: watch!.id,
    a: 'confirm',
    e: Math.floor(Date.now() / 1000) + CONFIRM_TOKEN_DAYS * 86400,
  });
  const confirmUrl = `${env.API_ORIGIN}/api/confirm?token=${token}`;

  const text = [
    `Please confirm you want this alert.`,
    ``,
    `You asked to be emailed when the wait for ${label} changes.`,
    ``,
    `Confirm here: ${confirmUrl}`,
    ``,
    `This link works for ${CONFIRM_TOKEN_DAYS} days. If you did not ask for this, ignore this email — nothing will be sent.`,
    ``,
    `What you were shown when you signed up:`,
    `"${wordingText}"`,
    ``,
    `Turnbeck — NHS waiting times & your right to choose faster care.`,
  ].join('\n');

  let messageId: string | null = null;
  try {
    messageId = await sendMail(env, {
      to: email,
      subject: `Confirm your alert — ${label}`,
      text,
      stream: 'transactional',
    });
  } catch {
    return json({ error: 'mail_failed' }, 502);
  }

  return json({ ok: true, status: 'pending_confirmation', mail_sent: messageId !== null });
}

async function handleConfirm(url: URL, req: Request, env: Env): Promise<Response> {
  const payload = await readToken(env.TOKEN_SECRET, url.searchParams.get('token') ?? '');
  if (!payload || payload.a !== 'confirm')
    return page('Link not valid', '<h1>That link is not valid any more.</h1><p>Confirmation links work for seven days. Sign up again from the hospital page and we will send a fresh one.</p>', 400);

  const at = nowIso();
  await env.DB.prepare(
    "UPDATE subscribers SET status = 'active', confirmed_at = COALESCE(confirmed_at, ?) WHERE id = ?",
  )
    .bind(at, payload.s)
    .run();
  if (payload.w) await env.DB.prepare('UPDATE watches SET active = 1 WHERE id = ?').bind(payload.w).run();

  const wording = await env.DB.prepare(
    `SELECT wording_version FROM consent_events WHERE subscriber_id = ? AND event = 'signup'
     ORDER BY id DESC LIMIT 1`,
  )
    .bind(payload.s)
    .first<{ wording_version: string }>();

  await env.DB.prepare(
    `INSERT INTO consent_events (subscriber_id, watch_id, event, at, ip, user_agent, page_url, wording_version, method)
     VALUES (?, ?, 'confirm', ?, ?, ?, ?, ?, 'link')`,
  )
    .bind(
      payload.s,
      payload.w ?? null,
      at,
      req.headers.get('CF-Connecting-IP') ?? 'local',
      req.headers.get('User-Agent') ?? '',
      url.toString(),
      wording?.wording_version ?? null,
    )
    .run();

  const watch = payload.w
    ? await env.DB.prepare('SELECT label FROM watches WHERE id = ?').bind(payload.w).first<{ label: string }>()
    : null;

  return page(
    'Alert confirmed — Turnbeck',
    `<h1>That's confirmed.</h1><p>We'll email you when the wait for <strong>${watch?.label ?? 'this treatment'}</strong> changes by a week or more, or when the number of people waiting changes by 10% or at least 25 people.</p><p>Every email has a one-click unsubscribe link. <a href="${env.SITE_ORIGIN}/">Back to the site</a>.</p>`,
  );
}

async function unsubscribe(env: Env, req: Request, payload: TokenPayload, method: string): Promise<void> {
  const at = nowIso();
  await env.DB.prepare(
    "UPDATE subscribers SET status = 'unsubscribed', unsubscribed_at = ? WHERE id = ?",
  )
    .bind(at, payload.s)
    .run();
  await env.DB.prepare('UPDATE watches SET active = 0 WHERE subscriber_id = ?').bind(payload.s).run();
  await env.DB.prepare(
    `INSERT INTO consent_events (subscriber_id, watch_id, event, at, ip, user_agent, page_url, method)
     VALUES (?, ?, 'unsubscribe', ?, ?, ?, NULL, ?)`,
  )
    .bind(
      payload.s,
      payload.w ?? null,
      at,
      req.headers.get('CF-Connecting-IP') ?? 'local',
      req.headers.get('User-Agent') ?? '',
      method,
    )
    .run();
}

// ---------------------------------------------------------------------------

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);
    const cors = corsHeaders(env, req.headers.get('Origin'));

    if (req.method === 'OPTIONS') return new Response(null, { status: 204, headers: cors });

    if (url.pathname === '/api/health') return json({ ok: true, at: nowIso() }, 200, cors);

    if (url.pathname === '/api/subscribe' && req.method === 'POST') {
      const res = await handleSubscribe(req, env);
      for (const [k, v] of Object.entries(cors)) res.headers.set(k, v);
      return res;
    }

    if (url.pathname === '/api/confirm' && req.method === 'GET') return handleConfirm(url, req, env);

    if (url.pathname === '/api/unsubscribe') {
      const token = url.searchParams.get('token') ?? '';
      const payload = await readToken(env.TOKEN_SECRET, token);
      if (!payload || payload.a !== 'unsub')
        return page('Link not valid', '<h1>That unsubscribe link is not valid.</h1><p>Use the link in any alert email, or email us and we will remove you by hand.</p>', 400);

      if (req.method === 'POST') {
        // RFC 8058 one-click. Mail clients POST here without a human visiting.
        await unsubscribe(env, req, payload, 'one-click-post');
        return new Response('Unsubscribed', { status: 200, headers: { 'Content-Type': 'text/plain' } });
      }
      // GET = the human page. The button posts back to this same URL.
      return page(
        'Stop these emails — Turnbeck',
        `<h1>Stop these emails?</h1><p>One click and we stop. Nothing else happens, and we keep only the record that you asked us to stop.</p>
<form method="post" action="/api/unsubscribe?token=${token}"><button type="submit">Yes, stop emailing me</button></form>`,
      );
    }

    // ---- admin (send job) ----
    if (url.pathname.startsWith('/api/admin/')) {
      const auth = req.headers.get('Authorization') ?? '';
      if (auth !== `Bearer ${env.ADMIN_SECRET}`) return json({ error: 'unauthorised' }, 401);

      if (url.pathname === '/api/admin/watches' && req.method === 'GET') {
        const rows = await env.DB.prepare(
          `SELECT w.id AS watch_id, w.entity_key, w.label, w.page_path, w.threshold_kind,
                  s.id AS subscriber_id, s.email, s.status
           FROM watches w JOIN subscribers s ON s.id = w.subscriber_id
           WHERE w.active = 1 AND s.status = 'active'
           ORDER BY w.id`,
        ).all();
        // Each watch travels with its own unsubscribe token so the send job
        // never needs the signing secret.
        const watches = [];
        for (const r of rows.results as Record<string, unknown>[]) {
          watches.push({
            ...r,
            unsubscribe_url: `${env.API_ORIGIN}/api/unsubscribe?token=${await mintToken(env.TOKEN_SECRET, {
              s: Number(r.subscriber_id),
              w: Number(r.watch_id),
              a: 'unsub',
            })}`,
          });
        }
        return json({ watches });
      }

      if (url.pathname === '/api/admin/send-log' && req.method === 'POST') {
        const body = (await req.json()) as {
          watch_id: number;
          month: string;
          delta_summary?: string;
          message_id?: string;
          claim?: boolean;
        };
        if (body.claim) {
          // Claim-before-send: the UNIQUE(watch_id, month) constraint is the
          // idempotency guarantee. A rerun of the job claims nothing and
          // therefore sends nothing.
          try {
            await env.DB.prepare(
              'INSERT INTO send_log (watch_id, month, delta_summary, claimed_at) VALUES (?, ?, ?, ?)',
            )
              .bind(body.watch_id, body.month, body.delta_summary ?? null, nowIso())
              .run();
            return json({ claimed: true });
          } catch {
            return json({ claimed: false, duplicate: true });
          }
        }
        await env.DB.prepare(
          'UPDATE send_log SET message_id = ?, sent_at = ? WHERE watch_id = ? AND month = ?',
        )
          .bind(body.message_id ?? null, nowIso(), body.watch_id, body.month)
          .run();
        return json({ ok: true });
      }

      if (url.pathname === '/api/admin/export' && req.method === 'GET') {
        const [subs, watches, wordings, events, sends] = await Promise.all([
          env.DB.prepare('SELECT * FROM subscribers ORDER BY id').all(),
          env.DB.prepare('SELECT * FROM watches ORDER BY id').all(),
          env.DB.prepare('SELECT * FROM consent_wordings').all(),
          env.DB.prepare('SELECT * FROM consent_events ORDER BY id').all(),
          env.DB.prepare('SELECT * FROM send_log ORDER BY id').all(),
        ]);
        return json({
          subscribers: subs.results,
          watches: watches.results,
          consent_wordings: wordings.results,
          consent_events: events.results,
          send_log: sends.results,
        });
      }
    }

    return json({ error: 'not_found' }, 404);
  },
};
