#!/usr/bin/env python3
"""Unit tests for the ADR-02 material-change contract (alerts_thresholds)."""
import unittest

from alerts_thresholds import (compute_deltas, entity_key, material_change,
                               prev_calendar_month)


class TestMaterialChange(unittest.TestCase):
    # -- median component ---------------------------------------------------
    def test_median_delta_exactly_one_week_is_material(self):
        material, reasons = material_change(100, 100, 4.0, 5.0)
        self.assertTrue(material)
        self.assertEqual(reasons, ["median"])

    def test_median_delta_below_one_week_is_not_material(self):
        material, reasons = material_change(100, 100, 4.0, 4.9)
        self.assertFalse(material)
        self.assertEqual(reasons, [])

    def test_median_delta_negative_direction_counts(self):
        material, reasons = material_change(100, 100, 5.5, 4.5)
        self.assertTrue(material)
        self.assertEqual(reasons, ["median"])

    # -- waiting-list component: the 25 floor -------------------------------
    def test_wl_floor_beats_ten_percent_for_small_providers(self):
        # prev=100 -> max(10, 25) = 25: a jitter of 24 must NOT alert
        self.assertFalse(material_change(100, 124, None, None)[0])
        # ...but exactly 25 does
        material, reasons = material_change(100, 125, None, None)
        self.assertTrue(material)
        self.assertEqual(reasons, ["waiting_list"])

    def test_wl_ten_percent_governs_large_providers(self):
        # prev=1000 -> max(100, 25) = 100
        self.assertFalse(material_change(1000, 1099, None, None)[0])
        self.assertTrue(material_change(1000, 1100, None, None)[0])
        self.assertTrue(material_change(1000, 900, None, None)[0])  # decrease too

    def test_wl_prev_zero_uses_floor(self):
        # prev=0 -> max(0, 25) = 25
        self.assertFalse(material_change(0, 24, None, None)[0])
        self.assertTrue(material_change(0, 25, None, None)[0])

    def test_both_components_reported(self):
        material, reasons = material_change(100, 200, 3.0, 6.0)
        self.assertTrue(material)
        self.assertEqual(sorted(reasons), ["median", "waiting_list"])

    # -- null handling -------------------------------------------------------
    def test_null_to_null_median_skips(self):
        material, reasons = material_change(100, 105, None, None)
        self.assertFalse(material)
        self.assertEqual(reasons, [])

    def test_one_sided_null_median_skips_that_component(self):
        # median appearing from null is not a computable delta — never alerts
        self.assertFalse(material_change(100, 100, None, 9.9)[0])
        self.assertFalse(material_change(100, 100, 9.9, None)[0])

    def test_one_sided_null_wl_skips_that_component(self):
        self.assertFalse(material_change(None, 99999, 4.0, 4.0)[0])


class TestEntityKey(unittest.TestCase):
    def test_key_matches_watches_schema(self):
        self.assertEqual(entity_key("RGT", "C_110"), "RGT|C_110")


class TestPrevCalendarMonth(unittest.TestCase):
    def test_mid_year(self):
        self.assertEqual(prev_calendar_month("2026-05"), "2026-04")

    def test_january_wraps(self):
        self.assertEqual(prev_calendar_month("2026-01"), "2025-12")


def _row(month, pcode, tf, wl, med):
    return {"month": month, "provider_code": pcode, "specialty_code": tf,
            "waiting_list": wl, "median_wait_weeks_est": med}


class TestComputeDeltas(unittest.TestCase):
    def test_adjacent_months_produce_deltas(self):
        rows = [_row("2026-04", "RGT", "C_110", "1000", "4.0"),
                _row("2026-05", "RGT", "C_110", "1100", "4.5")]
        doc = compute_deltas(rows)
        self.assertEqual(doc["month"], "2026-05")
        self.assertEqual(doc["prev_month"], "2026-04")
        (ent,) = doc["entities"]
        self.assertEqual(ent["key"], "RGT|C_110")
        self.assertEqual(ent["delta_wl"], 100)
        self.assertEqual(ent["delta_med"], 0.5)
        self.assertTrue(ent["material"])           # wl: 100 >= max(100, 25)
        self.assertEqual(ent["reasons"], ["waiting_list"])

    def test_gap_months_emit_no_fake_deltas(self):
        # only 2024-05 and 2026-05 present (mid-backfill state): a 24-month
        # jump must NOT be presented as a month-on-month movement
        rows = [_row("2024-05", "RGT", "C_110", "500", "3.0"),
                _row("2026-05", "RGT", "C_110", "1100", "4.5")]
        doc = compute_deltas(rows)
        self.assertIsNone(doc["prev_month"])
        self.assertEqual(doc["entities"], [])

    def test_vanished_entity_flagged_material(self):
        rows = [_row("2026-04", "RGT", "C_110", "1000", "4.0"),
                _row("2026-04", "RGT", "C_120", "300", "5.0"),
                _row("2026-05", "RGT", "C_110", "1001", "4.0")]
        doc = compute_deltas(rows)
        vanished = [e for e in doc["entities"] if e.get("vanished")]
        self.assertEqual(len(vanished), 1)
        self.assertEqual(vanished[0]["key"], "RGT|C_120")
        self.assertTrue(vanished[0]["material"])
        self.assertEqual(vanished[0]["reasons"], ["no_longer_reported"])

    def test_appeared_entity_not_material(self):
        rows = [_row("2026-04", "RGT", "C_110", "1000", "4.0"),
                _row("2026-05", "RGT", "C_110", "1001", "4.0"),
                _row("2026-05", "RGT", "C_130", "50", "2.0")]
        doc = compute_deltas(rows)
        appeared = [e for e in doc["entities"] if e.get("appeared")]
        self.assertEqual(len(appeared), 1)
        self.assertEqual(appeared[0]["key"], "RGT|C_130")
        self.assertFalse(appeared[0]["material"])

    def test_null_median_both_sides_skips(self):
        rows = [_row("2026-04", "RGT", "C_110", "1000", ""),
                _row("2026-05", "RGT", "C_110", "1010", "")]
        doc = compute_deltas(rows)
        (ent,) = doc["entities"]
        self.assertIsNone(ent["delta_med"])
        self.assertFalse(ent["material"])


if __name__ == "__main__":
    unittest.main()
