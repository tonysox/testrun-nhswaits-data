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
import re
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

# ---------------------------------------------------------------------------
# D-190: THE FLOOR BACKSTOP EXCLUDES BY STRUCTURE, NOT BY SUBSTRING
#
# _floor() used to strike the queue count out of its probe with a raw
# str.replace of the count's digits. That is exclusion by VALUE, and it deletes
# every occurrence of those digits from the message — including the ones that
# were never the count. A queue of 9 whose median rounds to 9 therefore made
# "typical wait now about 9 weeks" read as "typical wait now about   weeks" to
# the backstop, and the assertion passed on a leaked median. Verified blind at
# wl=9/med 9, wl=14/med 14 and wl=18/med 18 — and X7M7U ACES LAKESIDE (n=9,
# median 8.8 -> 9) sits in that blind spot in the CURRENT published month.
#
# The fix is to declare the safe spans where they are written. A number the
# floor may ignore is wrapped in markers AT THE POINT THE TEMPLATE EMITS IT, so
# the probe removes those exact character ranges and nothing else. A median that
# happens to share the count's digits is a different span, and stays visible.
# The markers are control characters, never valid in the copy, and _floor strips
# them from what it returns — a marker reaching a reader is itself an assertion
# failure.
_SAFE_OPEN, _SAFE_CLOSE = "\x02", "\x03"
_SAFE_SPAN = re.compile(f"{_SAFE_OPEN}[^{_SAFE_CLOSE}]*{_SAFE_CLOSE}")


def _safe(text):
    """Mark a span the denominator floor is licensed to ignore: a raw count (a
    fact the data can carry at any n) or one of the reference numbers the rule
    itself is stated in."""
    return f"{_SAFE_OPEN}{text}{_SAFE_CLOSE}"


def _unmark(text):
    return text.replace(_SAFE_OPEN, "").replace(_SAFE_CLOSE, "")


def _count_words(n):
    """"1 person" / "52 people" — the count, marked safe, with its own plural."""
    return f"{_safe(f'{n:,}')} {'person' if n == 1 else 'people'}"


def render(watch, ent, month, prev_month, site_origin, page_path):
    """Plain-text service message. Reading age ~9; no jargon; no promotion.

    D-193: THE SUBJECT LEADS WITH THE FIGURE. It used to be
    ``"<label> — typical wait now about 14 weeks"``, with the number at the very
    end — 142 characters at the longest provider name in the 2026-05 build (the
    audit measured 132 on the case it opened) — so the one thing the alert
    exists to tell a reader was the first thing a phone cut. The figure now comes
    first and the label follows it, and the first body line — the preview a mail
    client shows beside the subject — says something the subject does not.
    """
    label = watch["label"]
    curr, prev = ent.get("curr") or {}, ent.get("prev") or {}
    reasons = ent.get("reasons") or []
    lines = []

    if ent.get("vanished"):
        subject = f"No longer in the NHS figures: {label}"
        lines += [
            # preview line: the CAUSE and the reassurance, which the subject
            # does not carry.
            f"The hospital did not report this treatment area for "
            f"{month_label(month)}. It does not mean the service has stopped.",
            "",
            f"{label} is therefore not listed in the NHS waiting-times figures "
            "this month. We will email you again if it comes back.",
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
        # D-174: FAIL CLOSED WHEN THE DENOMINATOR IS UNKNOWN. This read
        # `wl_now is not None and wl_now < SMALL_N_FLOOR`, so a month where the
        # count did not come through — exactly the month you would least trust
        # a median from — evaluated to "not small" and the email quoted the
        # typical wait. A missing denominator is not a large denominator.
        small_now = wl_now is None or wl_now < SMALL_N_FLOOR
        small_prev = wl_prev is None or wl_prev < SMALL_N_FLOOR
        # The count sentence, built ONCE so the floored branch cannot promise a
        # count the message never prints (D-191, second half).
        if wl_now is not None and wl_prev is not None and "waiting_list" in reasons:
            moved = "more" if wl_now > wl_prev else "fewer"
            count_line = (
                f"{_count_words(wl_now)} are waiting, "
                f"{_safe(f'{abs(wl_now - wl_prev):,}')} {moved} than last month."
                if wl_now != 1 else
                f"{_count_words(wl_now)} is waiting, "
                f"{_safe(f'{abs(wl_now - wl_prev):,}')} {moved} than last month."
            )
        elif wl_now is not None:
            count_line = (
                f"{_count_words(wl_now)} {'is' if wl_now == 1 else 'are'} waiting here."
            )
        else:
            count_line = None
        counted = False

        if "median" in reasons and (small_now or small_prev):
            # D-191: THE FLOOR SENTENCE MUST DESCRIBE THE MONTH IT IS TRUE OF.
            # One sentence covered both cases: "Fewer than 20 people are waiting
            # here". On a queue that GREW past the floor — 19 last month, 52 now
            # — that is a false statement about the current month, and it was
            # printed immediately above "52 people are waiting, 33 more than last
            # month", which is the current month. Two adjacent sentences, one
            # month apart, contradicting each other. 17 entities in the 2026-05
            # deltas reach this branch with a current count at or above the
            # floor; 4 of them also print the count sentence, which is the exact
            # adjacency the audit quoted (DXN|C_410 is 19 -> 52, +33).
            #
            # The reason we withhold is unchanged (a month-on-month comparison
            # needs BOTH months above the floor), but it is now stated about
            # whichever month is actually short.
            if small_now and small_prev:
                why = (
                    f"Fewer than {_safe(SMALL_N_FLOOR)} people are waiting here this "
                    "month, and fewer than that were waiting last month, which is "
                    "too few for a reliable typical wait, so we are not quoting one."
                )
            elif small_now:
                why = (
                    f"Fewer than {_safe(SMALL_N_FLOOR)} people are waiting here now, "
                    "which is too few for a reliable typical wait, so we are not "
                    "quoting one."
                )
            else:
                why = (
                    f"Fewer than {_safe(SMALL_N_FLOOR)} people were waiting here last "
                    "month, which is too few for a reliable typical wait to compare "
                    "this month against, so we are not quoting one."
                )
            headline_bits.append(why)
            # ...and the count follows, or the message says the count is missing
            # too. It used to say "We only send you the count for this queue"
            # and then print no count at all whenever the movement was a median
            # move rather than a waiting-list move — 273 of the entities in the
            # 2026-05 deltas that reach this branch.
            if count_line:
                headline_bits.append(count_line)
            else:
                headline_bits.append(
                    "We do not have this month's count for this queue either."
                )
            counted = True
            subject = (
                f"{_count_words(wl_now)} now waiting: {label}"
                if wl_now is not None else f"Waiting-list update: {label}"
            )
        elif "median" in reasons and med_now is not None and med_prev is not None:
            # The sentence must add up as written. Both figures are shown
            # rounded to whole weeks (the site's headline rule), so the
            # difference is stated from the ROUNDED figures — never the raw
            # ones, or a reader gets "14 weeks, was 12 weeks, 2.5 weeks longer".
            now_w, prev_w = round(med_now), round(med_prev)
            gap = abs(now_w - prev_w)
            direction = "longer" if med_now > med_prev else "shorter"
            # D-193: figure first, so it survives a lock-screen truncation.
            subject = f"Now about {now_w} weeks: {label}"
            change = (f"{gap} {'week' if gap == 1 else 'weeks'} {direction}"
                      if gap else f"slightly {direction}")
            # D-193: the preview line leads with the MOVEMENT — the thing the
            # subject does not say — and still states both figures, so the body
            # stands alone in a client that shows no subject.
            headline_bits.append(
                f"Waits here are {change} than last month. The typical wait for "
                f"{label} is now about {now_w} weeks; last month it was about "
                f"{prev_w} weeks."
            )
        else:
            subject = (
                f"{_count_words(wl_now)} now waiting: {label}"
                if wl_now is not None else f"Waiting-list update: {label}"
            )
        if not counted and count_line and "waiting_list" in reasons:
            headline_bits.append(count_line)
        lines += headline_bits

    lines += [
        "",
        f"Figures: NHS England monthly waiting times, {month_label(month)}, "
        f"compared with {month_label(prev_month)}. Waits are published in weekly "
        "ranges, so the typical wait is a close estimate.",
        "",
        f"See the full page: {site_origin}{page_path}",
        "",
        # The rule has to be quoted in its own numbers, so those numbers are
        # declared safe HERE, as spans, rather than matched by a second regex
        # that could drift out of step with this sentence (D-190).
        "Why you got this: you asked us to email you when this figure changes "
        "materially. We email when the typical wait moves by "
        f"{_safe(f'{MEDIAN_DELTA_WEEKS:g} week')} or more, or when the number of "
        f"people waiting changes by {_safe(f'{int(WAITING_LIST_PCT * 100)}%')} or at "
        f"least {_safe(WAITING_LIST_FLOOR)} people "
        f"(rule version {THRESHOLDS_VERSION}).",
        "",
        f"Stop these emails in one click: {watch['unsubscribe_url']}",
        "",
        "Turnbeck — NHS waiting times & your right to choose faster care.",
        "England only. This is general information, not medical advice.",
    ]
    body = "\n".join(lines)
    return _floor(subject, body, ent)


# The withheld figure, in any of the shapes this template could print it.
_WAIT_CLAIM = re.compile(
    r"(?<![\d.])\d[\d,.]*\s*(?:wk|wks|week|weeks)\b|(?<![\d.])\d[\d,.]*\s*%"
)


def _floor(subject, body, ent):
    """D-174: THE FLOOR APPLIES TO THE WHOLE MESSAGE, AS ONE STRING.

    Every check — here and in test_alerts_send.py — read the BODY. The subject
    line is the half of an email that a phone shows on the lock screen without
    the reader opening anything, and it is built from the same median:
    ``"… — typical wait now about 14 weeks"``. A floored message whose body
    correctly refuses to quote a median could still put that median on the lock
    screen, and nothing in either repo would have noticed.

    So the assertion is made once, over ``subject + "\\n" + body``, and it is an
    assertion rather than a log line: a message that breaks the floor is not
    sent in a degraded form, it stops the send job.

    D-190: the exclusion is STRUCTURAL. See _safe() above — the probe drops the
    exact character spans the template declared safe, never every occurrence of
    a value, so a median equal to the queue count is still visible to it.
    """
    curr, prev = ent.get("curr") or {}, ent.get("prev") or {}
    wl_now, wl_prev = curr.get("wl"), prev.get("wl")
    floored = (
        ent.get("vanished")
        or wl_now is None
        or wl_now < SMALL_N_FLOOR
        or wl_prev is None
        or wl_prev < SMALL_N_FLOOR
    )
    if floored:
        whole = f"{subject}\n{body}"
        probe = _SAFE_SPAN.sub(" ", whole)
        leaked = _WAIT_CLAIM.search(probe)
        assert not leaked, (
            "the alert message quotes a wait for a queue below the "
            f"{SMALL_N_FLOOR}-person floor: {leaked.group(0)!r} in "
            f"{probe[max(0, leaked.start() - 40):leaked.end() + 20]!r}"
        )
    subject, body = _unmark(subject), _unmark(body)
    # A marker that reached a reader would be a rendering bug AND would mean the
    # probe above read a different string from the one being sent.
    assert _SAFE_OPEN not in f"{subject}{body}" and _SAFE_CLOSE not in f"{subject}{body}"
    return subject, body


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
