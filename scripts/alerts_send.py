#!/usr/bin/env python3
"""Alert send job (ARCHITECTURE ADR-02).

Runs in the same Actions run as a successful publish. Reads the deltas document
the pipeline already writes (data/deltas/latest.json, thresholds owned by
scripts/alerts_thresholds.py) and the active watches from alerts-api, and sends
ONE strictly-neutral service message per watched entity that moved materially.

Three rules this file exists to keep:

1. PECR mixed-content rule is absolute (STANDARD-email-alerts §3). The message
   carries a figure, the movement, one plain sentence, a link to the page and an
   unsubscribe link. No promotion, no cross-sell, no sibling sites, ever. The
   monetisation is the ad-carrying page the link goes to.
2. Idempotency is structural, not hopeful: the job CLAIMS send_log(watch,month)
   before sending, and the UNIQUE constraint means a rerun claims nothing and
   therefore sends nothing.
3. The transport is one switch. --mail-url points at the local catcher in a
   proof-of-concept build and at the ESP at launch; --mail-url '' renders and
   logs without sending.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

from alerts_thresholds import (
    MEDIAN_DELTA_WEEKS,
    THRESHOLDS_VERSION,
    WAITING_LIST_FLOOR,
    WAITING_LIST_PCT,
)

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def month_label(ym):
    y, m = ym.split("-")
    return f"{MONTHS[int(m) - 1]} {y}"


def _request(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=30) as res:
        raw = res.read()
        return json.loads(raw) if raw else {}


# ---------------------------------------------------------------------------
# the template — read it as a patient would
# ---------------------------------------------------------------------------

# STANDARD-data-pipeline 8.1: below this many people waiting, no median is
# quoted anywhere - page, hub, table, chart or email.
SMALL_N_FLOOR = 20


def render(watch, ent, month, prev_month, site_origin, page_path):
    """Plain-text service message. Reading age ~9; no jargon; no promotion."""
    label = watch["label"]
    curr, prev = ent.get("curr") or {}, ent.get("prev") or {}
    reasons = ent.get("reasons") or []
    lines = []

    if ent.get("vanished"):
        subject = f"{label} — no longer in the NHS figures"
        lines += [
            f"{label} is no longer listed in the NHS waiting-times figures for "
            f"{month_label(month)}.",
            "",
            "That usually means the hospital did not report this treatment area this "
            "month. It does not mean the service has stopped. We will email you again "
            "if it comes back.",
        ]
    else:
        med_now, med_prev = curr.get("med"), prev.get("med")
        wl_now, wl_prev = curr.get("wl"), prev.get("wl")
        headline_bits = []
        # THE DENOMINATOR FLOOR (STANDARD-data-pipeline 8.1, QA D-120).
        # The site withholds the typical wait below n=20 because a median over a
        # handful of people is one person's wait wearing a population's clothes.
        # An email is the same figure in a different envelope, and it is the one
        # place the reader cannot click through to the caveat before believing
        # it — so the same floor applies, in the same words. The raw count is
        # still sent, because a count is a fact the data can carry.
        small_now = wl_now is not None and wl_now < SMALL_N_FLOOR
        small_prev = wl_prev is not None and wl_prev < SMALL_N_FLOOR
        if "median" in reasons and (small_now or small_prev):
            subject = f"{label} — the number of people waiting has changed"
            headline_bits.append(
                f"Fewer than {SMALL_N_FLOOR} people are waiting here, which is too "
                "few for a reliable typical wait, so we are not quoting one. We "
                "only send you the count for this queue."
            )
        elif "median" in reasons and med_now is not None and med_prev is not None:
            # The sentence must add up as written. Both figures are shown
            # rounded to whole weeks (the site's headline rule), so the
            # difference is stated from the ROUNDED figures — never the raw
            # ones, or a reader gets "14 weeks, was 12 weeks, 2.5 weeks longer".
            now_w, prev_w = round(med_now), round(med_prev)
            gap = abs(now_w - prev_w)
            direction = "longer" if med_now > med_prev else "shorter"
            subject = f"{label} — typical wait now about {now_w} weeks"
            change = (f"{gap} {'week' if gap == 1 else 'weeks'} {direction}"
                      if gap else f"slightly {direction}")
            headline_bits.append(
                f"The typical wait for {label} is now about {now_w} weeks. "
                f"Last month it was about {prev_w} weeks — {change}."
            )
        else:
            subject = f"{label} — the number of people waiting has changed"
        if "waiting_list" in reasons and wl_now is not None and wl_prev is not None:
            direction = "more" if wl_now > wl_prev else "fewer"
            headline_bits.append(
                f"{wl_now:,} people are waiting, {abs(wl_now - wl_prev):,} {direction} "
                f"than last month."
            )
        lines += headline_bits

    lines += [
        "",
        f"Figures: NHS England monthly waiting times, {month_label(month)}, "
        f"compared with {month_label(prev_month)}. Waits are published in weekly "
        "ranges, so the typical wait is a close estimate.",
        "",
        f"See the full page: {site_origin}{page_path}",
        "",
        "Why you got this: you asked us to email you when this figure changes "
        "materially. We email when the typical wait moves by "
        f"{MEDIAN_DELTA_WEEKS:g} week or more, or when the number of people waiting "
        f"changes by {int(WAITING_LIST_PCT * 100)}% or at least {WAITING_LIST_FLOOR} "
        f"people (rule version {THRESHOLDS_VERSION}).",
        "",
        f"Stop these emails in one click: {watch['unsubscribe_url']}",
        "",
        "Turnbeck — NHS waiting times & your right to choose faster care.",
        "England only. This is general information, not medical advice.",
    ]
    return subject, "\n".join(lines)


def page_path_for(entity_key, page_hint):
    """The result page for this watch. The signup recorded where it came from,
    which is the honest source of truth for the link back."""
    if page_hint:
        try:
            from urllib.parse import urlsplit
            path = urlsplit(page_hint).path
            if path and path != "/":
                return path
        except ValueError:
            pass
    return "/"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deltas", default="data/deltas/latest.json")
    ap.add_argument("--api", required=True, help="alerts-api base URL")
    ap.add_argument("--admin-secret", required=True)
    ap.add_argument("--mail-url", default="", help="ESP send endpoint ('' = render only)")
    ap.add_argument("--mail-token", default="")
    ap.add_argument("--mail-from", default="alerts@turnbeck.invalid")
    ap.add_argument("--site-origin", default="https://testrun-nhswaits.pages.dev")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be sent; claim nothing, send nothing")
    args = ap.parse_args()

    with open(args.deltas, encoding="utf-8") as fh:
        doc = json.load(fh)
    month, prev_month = doc.get("month"), doc.get("prev_month")
    if not month or not prev_month:
        print("no month-on-month comparison available — nothing to alert on")
        return 0
    by_key = {e["key"]: e for e in doc.get("entities", [])}

    auth = {"Authorization": f"Bearer {args.admin_secret}"}
    watches = _request(f"{args.api}/api/admin/watches", headers=auth)["watches"]
    print(f"month {month} vs {prev_month}: {len(watches)} active watch(es)")

    sent = skipped = unchanged = 0
    for w in watches:
        ent = by_key.get(w["entity_key"])
        if ent is None:
            print(f"  - {w['entity_key']}: not in this month's deltas — no email")
            unchanged += 1
            continue
        if not ent.get("material"):
            print(f"  - {w['entity_key']}: moved, but not materially — no email")
            unchanged += 1
            continue

        subject, text = render(
            w, ent, month, prev_month, args.site_origin,
            page_path_for(w["entity_key"], w.get("page_path")),
        )
        summary = json.dumps({k: ent.get(k) for k in ("delta_med", "delta_wl", "reasons")})

        if args.dry_run:
            print(f"  - {w['entity_key']}: WOULD SEND to {w['email']} — {subject}")
            sent += 1
            continue

        claim = _request(f"{args.api}/api/admin/send-log", "POST",
                         {"watch_id": w["watch_id"], "month": month,
                          "delta_summary": summary, "claim": True}, auth)
        if not claim.get("claimed"):
            print(f"  - {w['entity_key']}: already emailed for {month} — skipped (send_log)")
            skipped += 1
            continue

        message_id = "not-sent"
        if args.mail_url:
            payload = {
                "From": args.mail_from,
                "To": w["email"],
                "Subject": subject,
                "TextBody": text,
                # Alerts are subscriber-list mail in a mailbox provider's eyes
                # whatever PECR calls them, so they ride the broadcast stream.
                "MessageStream": "broadcast",
                "Headers": [
                    {"Name": "List-Unsubscribe",
                     "Value": f"<{w['unsubscribe_url']}>"},
                    {"Name": "List-Unsubscribe-Post",
                     "Value": "List-Unsubscribe=One-Click"},
                ],
            }
            try:
                res = _request(args.mail_url, "POST", payload,
                               {"X-Postmark-Server-Token": args.mail_token})
                message_id = res.get("MessageID", "sent")
            except urllib.error.URLError as exc:
                print(f"  ! {w['entity_key']}: transport failed ({exc}) — send_log claimed, "
                      "not marked sent; rerun after fixing the transport")
                continue
        _request(f"{args.api}/api/admin/send-log", "POST",
                 {"watch_id": w["watch_id"], "month": month, "message_id": message_id}, auth)
        print(f"  + {w['entity_key']}: emailed {w['email']} — {subject} [{message_id}]")
        sent += 1

    print(f"done: {sent} sent, {skipped} suppressed by send_log, {unchanged} not material")
    return 0


if __name__ == "__main__":
    sys.exit(main())
