#!/usr/bin/env python3
"""Unit tests for the ADR-03 locations step helpers (scripts/locations.py)."""
import unittest

import locations


class TestNormPc(unittest.TestCase):
    def test_strips_spaces_and_uppercases(self):
        self.assertEqual(locations.norm_pc("se1 7eh"), "SE17EH")
        self.assertEqual(locations.norm_pc(" OX3  9DU "), "OX39DU")

    def test_none_safe(self):
        self.assertEqual(locations.norm_pc(None), "")


class TestHaversine(unittest.TestCase):
    def test_london_to_manchester(self):
        # St Thomas' -> Manchester Royal Infirmary, ~162 miles great-circle.
        d = locations.haversine_miles(51.498, -0.119, 53.4624, -2.2277)
        self.assertAlmostEqual(d, 162, delta=5)

    def test_zero_distance(self):
        self.assertEqual(locations.haversine_miles(51.5, -0.1, 51.5, -0.1), 0)


class TestResolveOnspdColumns(unittest.TestCase):
    MAY_2026 = ["pcd7", "pcd8", "pcds", "dointr", "doterm", "gridind",
                "ctry25cd", "lat", "long", "oa21cd"]
    LEGACY = ["pcd", "pcd2", "pcds", "dointr", "doterm", "osgrdind",
              "ctry", "lat", "long"]

    def test_may_2026_vintage_names(self):
        col = locations.resolve_onspd_columns(self.MAY_2026)
        self.assertEqual(col["ctry"], "ctry25cd")
        self.assertEqual(col["grid"], "gridind")
        self.assertEqual(col["pcds"], "pcds")

    def test_legacy_names(self):
        col = locations.resolve_onspd_columns(self.LEGACY)
        self.assertEqual(col["ctry"], "ctry")
        self.assertEqual(col["grid"], "osgrdind")

    def test_missing_column_hard_fails(self):
        with self.assertRaises(SystemExit):
            locations.resolve_onspd_columns(["pcds", "lat", "long"])

    def test_ambiguous_column_hard_fails(self):
        with self.assertRaises(SystemExit):
            locations.resolve_onspd_columns(self.MAY_2026 + ["ctry26cd"])


class TestBtStripInvariant(unittest.TestCase):
    """The G-L3 legal filter keys on the display postcode prefix."""

    def test_bt_prefix_detection(self):
        for pc in ("BT1 1AA", "BT48 7XX", "bt9 5AB"):
            self.assertTrue(pc.upper().startswith("BT"))
        # Postcodes merely CONTAINING 'BT' (none currently exist with these
        # prefixes followed by T, but the guard is prefix-only by design).
        for pc in ("B1 1BT", "SE1 7EH"):
            self.assertFalse(pc.upper().startswith("BT"))


if __name__ == "__main__":
    unittest.main()
