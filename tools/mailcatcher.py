#!/usr/bin/env python3
"""Local mail catcher — the ESP stand-in for proof-of-concept builds.

STANDARD-email-alerts §0: a proof-of-concept build opens NO email accounts. It
proves the whole loop against a catcher instead, because a catcher exercises
every line of OUR code — the templates, the headers, the consent records, the
unsubscribe flow — and an ESP account teaches us nothing more.

The catcher speaks the same HTTP shape as Postmark's /email endpoint, which is
what both callers (the alerts-api Worker and the Python send job) already use.
Swapping to a real ESP at launch is two variables: MAIL_API_URL + MAIL_API_TOKEN.

It writes every captured message to disk as a real RFC 5322 .eml — headers and
all — so what you read in the evidence is exactly what a mail client receives.

  POST /email          Postmark-shaped send        -> {"MessageID": ...}
  GET  /messages       captured messages as JSON
  GET  /messages/<n>   one message, with its .eml text

Usage: python3 tools/mailcatcher.py [--port 1080] [--dir .mailcatch]
"""
import argparse
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, parseaddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MESSAGES = []
LOCK = threading.Lock()
OUT_DIR = ".mailcatch"


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60] or "message"


def store(payload):
    """Build a real RFC 5322 message from a Postmark-shaped payload."""
    now = datetime.now(timezone.utc)
    msg = EmailMessage()
    msg["From"] = payload.get("From", "")
    msg["To"] = payload.get("To", "")
    msg["Subject"] = payload.get("Subject", "")
    msg["Date"] = format_datetime(now)
    message_id = str(uuid.uuid4())
    msg["Message-ID"] = f"<{message_id}@mailcatcher.local>"
    for h in payload.get("Headers") or []:
        name, value = h.get("Name"), h.get("Value")
        if name and value is not None:
            msg[name] = value
    msg.set_content(payload.get("TextBody", ""))

    index = len(MESSAGES)
    record = {
        "index": index,
        "message_id": message_id,
        "received_at": now.isoformat(),
        "to": parseaddr(payload.get("To", ""))[1],
        "from": parseaddr(payload.get("From", ""))[1],
        "subject": payload.get("Subject", ""),
        "stream": payload.get("MessageStream", ""),
        "headers": {k: v for k, v in msg.items()},
        "text": payload.get("TextBody", ""),
        "eml": msg.as_string(),
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{index:03d}-{_slug(record['subject'])}.eml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(record["eml"])
    record["path"] = path
    with LOCK:
        MESSAGES.append(record)
    print(f"[mailcatcher] captured -> {path}  (to {record['to']}, stream {record['stream']})",
          flush=True)
    return record


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # quiet; we print our own line per capture
        pass

    def _send(self, code, body, content_type="application/json"):
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        if self.path.rstrip("/") != "/email":
            return self._send(404, json.dumps({"error": "not_found"}))
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, json.dumps({"ErrorCode": 300, "Message": "bad json"}))
        record = store(payload)
        # Postmark's success shape, so the callers need no special-casing.
        self._send(200, json.dumps({
            "ErrorCode": 0,
            "Message": "OK",
            "MessageID": record["message_id"],
            "SubmittedAt": record["received_at"],
            "To": record["to"],
        }))

    def do_GET(self):
        if self.path.rstrip("/") == "/messages":
            with LOCK:
                return self._send(200, json.dumps(MESSAGES, indent=2))
        m = re.match(r"^/messages/(\d+)/?$", self.path)
        if m:
            i = int(m.group(1))
            with LOCK:
                if i >= len(MESSAGES):
                    return self._send(404, json.dumps({"error": "no_such_message"}))
                return self._send(200, json.dumps(MESSAGES[i], indent=2))
        if self.path.rstrip("/") == "/health":
            return self._send(200, json.dumps({"ok": True, "captured": len(MESSAGES)}))
        self._send(404, json.dumps({"error": "not_found"}))


def main():
    global OUT_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=1080)
    ap.add_argument("--dir", default=".mailcatch")
    args = ap.parse_args()
    OUT_DIR = args.dir
    os.makedirs(OUT_DIR, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[mailcatcher] listening on http://127.0.0.1:{args.port} -> {OUT_DIR}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
