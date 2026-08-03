#!/usr/bin/env python3
"""Unit tests for the alert template (ADR-02).

The template is where PECR compliance actually lives, so it is tested like a
gate, not eyeballed: no promotional content, an unsubscribe line every time, a
sentence whose arithmetic matches the figures it shows, and the threshold rule
quoted from the versioned module rather than retyped.
"""
import json
import os
import re
import unittest

from alerts_send import render, page_path_for, _floor
from alerts_thresholds import MEDIAN_DELTA_WEEKS, THRESHOLDS_VERSION

WATCH = {
    "watch_id": 1,
    "entity_key": "RCF|C_410",
    "label": "Joint and muscle conditions at Airedale NHS Foundation Trust",
    "email": "someone@example.invalid",
    "page_path": "https://site.example/hospital/airedale/joint-and-muscle-conditions/",
    "unsubscribe_url": "https://api.example/api/unsubscribe?token=abc.def",
}
SITE = "https://site.example"

# Words that would turn a service message into direct marketing under the
# mixed-content rule (STANDARD-email-alerts §3, Amex £90k precedent).
PROMOTIONAL = ["newsletter", "sign up", "subscribe to", "our other site", "offer",
               "sponsor", "discount", "upgrade", "premium", "follow us", "share this"]


def ent(prev_med, curr_med, prev_wl=400, curr_wl=400, reasons=("median",)):
    return {
        "key": "RCF|C_410",
        "prev": {"wl": prev_wl, "med": prev_med},
        "curr": {"wl": curr_wl, "med": curr_med},
        "delta_wl": None if curr_wl is None or prev_wl is None else curr_wl - prev_wl,
        "delta_med": round(curr_med - prev_med, 1),
        "material": True,
        "reasons": list(reasons),
    }


class TemplateTests(unittest.TestCase):
    def render(self, e):
        return render(WATCH, e, "2026-06", "2026-05", SITE,
                      page_path_for("RCF|C_410", WATCH["page_path"]))

    def test_arithmetic_in_the_sentence_matches_the_figures_shown(self):
        # 11.5 -> 14.0 rounds to 12 and 14: the sentence must say TWO weeks,
        # not the raw 2.5, or the reader can see it does not add up.
        _, body = self.render(ent(11.5, 14.0))
        self.assertIn("now about 14 weeks", body)
        self.assertIn("was about 12 weeks", body)
        self.assertIn("2 weeks longer", body)
        self.assertNotIn("2.5 weeks", body)

    def test_one_week_is_singular(self):
        _, body = self.render(ent(12.0, 13.0))
        self.assertIn("1 week longer", body)
        self.assertNotIn("1 weeks", body)

    def test_a_tiny_queue_gets_no_typical_wait_in_the_email(self):
        # STANDARD-data-pipeline 8.1 / QA D-120. An email is the one place the
        # reader cannot click through to the caveat before believing the number,
        # so the denominator floor has to hold here too.
        _, body = self.render(ent(12.0, 30.0, prev_wl=4, curr_wl=3))
        self.assertNotIn("typical wait for", body)
        self.assertNotIn("about 30 weeks", body)
        self.assertIn("too few for a reliable typical wait", body)

    def test_the_floor_does_not_silence_a_real_queue(self):
        # Two-sided: the fix must not turn every alert into a refusal.
        _, body = self.render(ent(12.0, 14.0, prev_wl=400, curr_wl=420))
        self.assertIn("typical wait for", body)
        self.assertNotIn("too few for a reliable typical wait", body)

    def test_a_queue_that_shrank_below_the_floor_stops_quoting_a_median(self):
        _, body = self.render(ent(12.0, 14.0, prev_wl=400, curr_wl=8))
        self.assertIn("too few for a reliable typical wait", body)

    def test_shorter_waits_read_as_shorter(self):
        _, body = self.render(ent(14.0, 11.5))
        self.assertIn("shorter", body)
        self.assertNotIn("longer", body)

    def test_rounding_collision_does_not_produce_zero_weeks(self):
        # 12.4 -> 13.4 both round to 12 and 13... but 12.6 -> 13.4 rounds to
        # 13 and 13. The sentence must not claim "0 weeks longer".
        _, body = self.render(ent(12.6, 13.4))
        self.assertNotIn("0 weeks", body)
        self.assertIn("slightly longer", body)

    def test_waiting_list_movement_is_reported_in_people(self):
        _, body = self.render(ent(12.0, 12.2, 400, 500, reasons=("waiting_list",)))
        self.assertIn("500 people are waiting", body)
        self.assertIn("100 more than last month", body)

    def test_every_message_carries_a_one_click_unsubscribe(self):
        for e in (ent(11.5, 14.0), ent(14.0, 11.5),
                  {"key": "RCF|C_410", "prev": {"wl": 1, "med": 1.0}, "curr": None,
                   "vanished": True, "material": True, "reasons": ["no_longer_reported"]}):
            _, body = self.render(e)
            self.assertIn(WATCH["unsubscribe_url"], body)
            self.assertIn("Stop these emails in one click", body)

    def test_no_promotional_content_anywhere(self):
        subject, body = self.render(ent(11.5, 14.0))
        text = f"{subject}\n{body}".lower()
        for word in PROMOTIONAL:
            self.assertNotIn(word, text, f"promotional wording in a service message: {word}")

    def test_threshold_rule_is_quoted_from_the_versioned_module(self):
        _, body = self.render(ent(11.5, 14.0))
        self.assertIn(f"{MEDIAN_DELTA_WEEKS:g} week or more", body)
        self.assertIn(f"rule version {THRESHOLDS_VERSION}", body)

    def test_message_links_back_to_the_page_the_person_signed_up_on(self):
        _, body = self.render(ent(11.5, 14.0))
        self.assertIn(f"{SITE}/hospital/airedale/joint-and-muscle-conditions/", body)

    def test_vanished_entity_is_explained_honestly(self):
        subject, body = self.render(
            {"key": "RCF|C_410", "prev": {"wl": 400, "med": 12.0}, "curr": None,
             "vanished": True, "material": True, "reasons": ["no_longer_reported"]})
        self.assertIn("no longer", subject.lower())
        self.assertIn("It does not mean the service has stopped", body)

    def test_jargon_stays_out_of_the_email(self):
        _, body = self.render(ent(11.5, 14.0))
        for word in ("RTT", "pathway", "normalised", "banded", "median"):
            self.assertNotIn(word, body, f"jargon leaked into a patient email: {word}")

    def test_boundary_lines_are_present(self):
        _, body = self.render(ent(11.5, 14.0))
        self.assertIn("England only", body)
        self.assertIn("not medical advice", body)

    def test_estimate_is_flagged(self):
        _, body = self.render(ent(11.5, 14.0))
        self.assertIn("close estimate", body)


class FloorAppliesToTheWholeMessageTests(unittest.TestCase):
    """D-174. Every check in this file read the BODY.

    The subject line is the half of an email a phone shows on the lock screen
    without the reader opening anything, and it is built from the same median
    ("… — typical wait now about 14 weeks"). A floored body plus an unfloored
    subject publishes the withheld figure to more people than the body ever
    reaches. The floor is now asserted over subject and body as ONE string.
    """

    def render(self, e):
        return render(WATCH, e, "2026-06", "2026-05", SITE,
                      page_path_for("RCF|C_410", WATCH["page_path"]))

    def test_a_missing_count_is_treated_as_below_the_floor(self):
        # The fail-open: `small_now = wl_now is not None and wl_now < 20` made a
        # MISSING count read as "not small", so the month where the denominator
        # did not arrive — the month you would least trust a median from — got
        # the median quoted, in the subject and in the body.
        subject, body = self.render(ent(12.0, 30.0, prev_wl=400, curr_wl=None))
        self.assertIn("too few for a reliable typical wait", body)
        self.assertNotIn("30 weeks", f"{subject}\n{body}")

    def test_a_missing_previous_count_is_treated_as_below_the_floor(self):
        subject, body = self.render(ent(12.0, 30.0, prev_wl=None, curr_wl=400))
        self.assertIn("too few for a reliable typical wait", body)
        self.assertNotIn("30 weeks", f"{subject}\n{body}")

    def test_the_subject_of_a_floored_message_carries_no_wait(self):
        for prev_wl, curr_wl in ((400, 3), (4, 400), (None, 400), (400, None), (2, 2)):
            subject, body = self.render(ent(12.0, 30.0, prev_wl=prev_wl, curr_wl=curr_wl))
            self.assertIsNone(
                re.search(r"\d[\d,.]*\s*(?:wk|wks|week|weeks)\b", subject),
                f"subject quotes a wait for a floored queue ({prev_wl}->{curr_wl}): {subject}",
            )

    def test_the_floor_is_asserted_over_subject_and_body_together(self):
        # Directly: a subject that breaks the floor must stop the send job even
        # when the body is spotless.
        floored = {"key": "X", "prev": {"wl": 4, "med": 12.0},
                   "curr": {"wl": 3, "med": 30.0}, "reasons": ["median"]}
        with self.assertRaises(AssertionError):
            _floor("Somewhere — typical wait now about 30 weeks", "3 people are waiting.", floored)

    def test_the_floor_does_not_fire_on_a_real_queue(self):
        # Two-sided: the guard must not stop an entitled message.
        subject, body = self.render(ent(12.0, 14.0, prev_wl=400, curr_wl=420))
        self.assertIn("14 weeks", f"{subject}\n{body}")


class FloorBackstopSeesEqualValuesTests(unittest.TestCase):
    """D-190. The backstop excluded the queue count from its probe with a raw
    substring replace, which deletes those digits EVERYWHERE — so it could not
    see a median that happened to equal the count. Verified blind at wl=9/med 9,
    wl=14/med 14 and wl=18/med 18, and X7M7U ACES LAKESIDE (n=9, median 8.8 -> 9)
    sits in that blind spot in the currently published month. Exclusion is now
    by declared span, so equal values are two different spans.
    """

    def floored_ent(self, wl, med):
        return {"key": "X", "prev": {"wl": wl, "med": float(med)},
                "curr": {"wl": wl, "med": float(med)}, "reasons": ["median"]}

    def test_a_median_equal_to_the_queue_count_is_still_seen(self):
        for wl in (9, 14, 18):
            with self.subTest(wl=wl):
                with self.assertRaises(AssertionError):
                    _floor(f"Now about {wl} weeks: Somewhere",
                           f"{wl} people are waiting here.",
                           self.floored_ent(wl, wl))

    def test_the_published_aces_lakeside_case(self):
        # X7M7U|C_130, 2026-05: wl 9, median 8.8, which rounds to 9.
        ent_ = {"key": "X7M7U|C_130", "prev": {"wl": 11, "med": 4.2},
                "curr": {"wl": 9, "med": 8.8}, "reasons": ["median"]}
        with self.assertRaises(AssertionError):
            _floor("Now about 9 weeks: Eye care at ACES Lakeside",
                   "9 people are waiting here.", ent_)

    def test_a_median_unlike_the_count_was_always_seen(self):
        # the control: this case was caught before the fix too.
        with self.assertRaises(AssertionError):
            _floor("Now about 30 weeks: Somewhere", "9 people are waiting here.",
                   self.floored_ent(9, 30))

    def test_the_backstop_still_lets_a_real_count_through(self):
        # Two-sided: exclusion by span must not become exclusion of nothing.
        subject, body = render(
            WATCH, ent(12.0, 30.0, prev_wl=19, curr_wl=9, reasons=("median", "waiting_list")),
            "2026-06", "2026-05", SITE, page_path_for("RCF|C_410", WATCH["page_path"]))
        self.assertIn("9 people are waiting", body)
        self.assertIn("9 people now waiting", subject)

    def test_no_control_marker_ever_reaches_a_reader(self):
        for e in (ent(11.5, 14.0), ent(12.0, 30.0, prev_wl=4, curr_wl=3),
                  ent(12.0, 12.2, 400, 500, reasons=("waiting_list",))):
            subject, body = render(WATCH, e, "2026-06", "2026-05", SITE, "/x/")
            self.assertNotIn("\x02", f"{subject}{body}")
            self.assertNotIn("\x03", f"{subject}{body}")


class TheFloorSentenceDescribesItsOwnMonthTests(unittest.TestCase):
    """D-191. One sentence — "Fewer than 20 people are waiting here" — was used
    whichever month was short. On a queue that GREW past the floor it rendered
    directly above "52 people are waiting, 33 more than last month": a claim
    about last month sitting on top of a claim about this month, contradicting
    it. And it promised "We only send you the count for this queue" in messages
    that then printed no count at all.
    """

    def render(self, e):
        return render(WATCH, e, "2026-06", "2026-05", SITE,
                      page_path_for("RCF|C_410", WATCH["page_path"]))

    def test_a_queue_that_grew_past_the_floor_says_last_month(self):
        # DXN|C_410 in the 2026-05 deltas: 19 -> 52, +33.
        _, body = self.render(
            ent(12.0, 30.0, prev_wl=19, curr_wl=52, reasons=("median", "waiting_list")))
        self.assertIn("were waiting here last month", body)
        self.assertNotIn("are waiting here now", body)
        self.assertNotIn("are waiting here this month", body)
        self.assertIn("52 people are waiting, 33 more than last month", body)

    def test_a_queue_below_the_floor_now_says_now(self):
        _, body = self.render(ent(12.0, 30.0, prev_wl=400, curr_wl=8))
        self.assertIn("are waiting here now", body)
        self.assertNotIn("were waiting here last month", body)

    def test_a_queue_short_in_both_months_says_both(self):
        _, body = self.render(ent(12.0, 30.0, prev_wl=4, curr_wl=3))
        self.assertIn("this month", body)
        self.assertIn("last month", body)

    def test_it_never_promises_a_count_it_does_not_send(self):
        for prev_wl, curr_wl, reasons in (
            (8, 8, ("median",)), (19, 52, ("median",)), (400, 8, ("median",)),
            (4, 3, ("median",)), (19, 52, ("median", "waiting_list")),
        ):
            with self.subTest(prev=prev_wl, curr=curr_wl):
                _, body = self.render(ent(12.0, 30.0, prev_wl, curr_wl, reasons))
                self.assertNotIn("only send you the count", body)
                self.assertRegex(body, r"\b[\d,]+ (?:person is|people are) waiting")

    def test_a_message_with_no_count_at_all_says_so(self):
        _, body = self.render(ent(12.0, 30.0, prev_wl=400, curr_wl=None))
        self.assertIn("do not have this month's count", body)


class SubjectLeadsWithTheFigureTests(unittest.TestCase):
    """D-193. The subject ran up to 132 characters with the figure LAST, so the
    number the alert exists to deliver was the first thing a lock screen cut."""

    def render(self, e):
        return render(WATCH, e, "2026-06", "2026-05", SITE,
                      page_path_for("RCF|C_410", WATCH["page_path"]))

    def test_the_median_subject_opens_with_the_figure(self):
        subject, _ = self.render(ent(11.5, 14.0))
        self.assertTrue(subject.startswith("Now about 14 weeks:"), subject)

    def test_the_count_subject_opens_with_the_figure(self):
        subject, _ = self.render(ent(12.0, 12.2, 400, 500, reasons=("waiting_list",)))
        self.assertTrue(subject.startswith("500 people now waiting:"), subject)

    def test_the_figure_survives_a_lock_screen_truncation(self):
        for e, needle in ((ent(11.5, 14.0), "14 weeks"),
                          (ent(12.0, 12.2, 400, 500, reasons=("waiting_list",)), "500 people")):
            subject, _ = self.render(e)
            for width in (40, 60, 90):
                with self.subTest(width=width, subject=subject):
                    self.assertIn(needle, subject[:width])

    def test_the_preview_line_says_something_the_subject_does_not(self):
        subject, body = self.render(ent(11.5, 14.0))
        preview = body.split("\n")[0]
        self.assertNotEqual(preview[:30], subject[:30])
        # the movement is the thing the subject cannot carry
        self.assertIn("2 weeks longer than last month", preview)
        self.assertNotIn("longer", subject)

    def test_the_vanished_preview_is_not_the_subject_again(self):
        subject, body = self.render(
            {"key": "RCF|C_410", "prev": {"wl": 400, "med": 12.0}, "curr": None,
             "vanished": True, "material": True, "reasons": ["no_longer_reported"]})
        preview = body.split("\n")[0]
        self.assertTrue(subject.startswith("No longer in the NHS figures:"), subject)
        self.assertIn("did not report this treatment area", preview)


class EveryPublishedDeltaRendersWithoutContradictingItselfTests(unittest.TestCase):
    """The gate, not the example. Renders a message for EVERY material entity in
    the deltas document this repo actually publishes and asserts the three
    properties above hold on all of them — so a regression fails on real data,
    not only on the cases someone thought to write down."""

    DELTAS = os.path.join(os.path.dirname(__file__), "..", "data", "deltas", "latest.json")

    def test_every_material_entity(self):
        if not os.path.exists(self.DELTAS):
            self.skipTest("deltas document not present in this checkout")
        with open(self.DELTAS, encoding="utf-8") as fh:
            doc = json.load(fh)
        month, prev_month = doc["month"], doc["prev_month"]
        checked = floored_msgs = 0
        for e in doc.get("entities", []):
            if not e.get("material"):
                continue
            watch = dict(WATCH, entity_key=e["key"],
                         label=f"Eye care at Some Hospital ({e['key']})")
            subject, body = render(watch, e, month, prev_month, SITE, "/x/")
            checked += 1
            whole = f"{subject}\n{body}"
            self.assertNotIn("\x02", whole)
            self.assertNotIn("only send you the count", body)
            curr = e.get("curr") or {}
            wl_now = curr.get("wl")
            if "are waiting here now" in body:
                floored_msgs += 1
                self.assertIsNotNone(wl_now)
                self.assertLess(wl_now, 20, f"{e['key']}: says 'now' but n={wl_now}")
            if "were waiting here last month" in body:
                floored_msgs += 1
                self.assertGreaterEqual(
                    wl_now, 20, f"{e['key']}: says only last month was short but n={wl_now}")
            if "people are waiting here this month" in body:
                floored_msgs += 1
                self.assertLess(wl_now, 20)
            # every message that withholds a median must still carry a count,
            # or say the count is missing
            if "so we are not quoting one" in body:
                self.assertTrue(
                    re.search(r"\b[\d,]+ (?:person is|people are) waiting", body)
                    or "do not have this month's count" in body,
                    f"{e['key']}: withheld the wait and sent no count",
                )
        self.assertGreater(checked, 500, f"sampler read only {checked} entities")
        self.assertGreater(floored_msgs, 20, f"only {floored_msgs} floored messages seen")
        print(f"\n  rendered {checked} material entities from {month}; "
              f"{floored_msgs} carried a floor sentence")


class PagePathTests(unittest.TestCase):
    def test_full_url_is_reduced_to_a_path(self):
        self.assertEqual(
            page_path_for("X|Y", "https://site.example/hospital/a/b/?near=SW1A"),
            "/hospital/a/b/")

    def test_missing_hint_falls_back_to_the_home_page(self):
        self.assertEqual(page_path_for("X|Y", None), "/")
        self.assertEqual(page_path_for("X|Y", "https://site.example/"), "/")


if __name__ == "__main__":
    unittest.main()
