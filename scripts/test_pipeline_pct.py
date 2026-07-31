#!/usr/bin/env python3
"""Unit tests for pct_within_18 (W0c — the pct_within_18_weeks fix).

Background: pct_within_18_weeks was empty for ALL published rows because the
old code divided by the source's 'Total' column, which is blank on every
Incomplete Pathways row of the extract (only Completed Pathways parts
populate it). The denominator must instead be the sum of the week-band
columns themselves (= patients with a known clock start), which is exactly
the denominator behind NHS's published 'within 18 weeks' measure.
"""
import unittest

from pipeline import pct_within_18

# The extract's band layout: 'Gt 00 To 01' .. 'Gt 103 To 104' (uppers 1..104)
# plus the open-ended 'Gt 104 Weeks' band (upper coded as 999). 105 bands.
UPPERS = list(range(1, 105)) + [999]


def counts_from(sparse):
    """Build the 105-long count vector from {upper_week: count}."""
    counts = [0.0] * len(UPPERS)
    for upper, c in sparse.items():
        counts[UPPERS.index(upper)] = float(c)
    return counts


class TestPctWithin18(unittest.TestCase):

    def test_hand_worked_real_row(self):
        """Real source row, hand-worked.

        Row: May 2026 full extract (raw-2026-07-30, sha256 a832...dd1a's
        vintage), RTT Part 'Incomplete Pathways', Provider H3W7Q
        'ACES CHELMSFORD', Treatment Function C_130 Ophthalmology,
        Commissioner 06Q NHS Essex ICB. Non-zero band cells:

          <=18 weeks: Gt00-01:2  Gt01-02:3  Gt02-03:4  Gt03-04:7  Gt04-05:4
                      Gt05-06:2  Gt06-07:6  Gt07-08:3  Gt08-09:2  Gt09-10:5
                      Gt10-11:4  Gt11-12:3  Gt13-14:1  Gt17-18:1
          >18 weeks:  Gt20-21:1  Gt22-23:1  Gt28-29:1  Gt30-31:1

        Arithmetic:
          numerator   = 2+3+4+7+4+2+6+3+2+5+4+3+1+1          = 47
          denominator = 47 + (1+1+1+1)                        = 51
                        (matches the row's 'Total All' of 51; its 'Patients
                        with unknown clock start date' cell is blank)
          pct         = 100 * 47 / 51 = 92.156862...  -> '92.2' (1 dp)

        The row's own 'Total' cell is BLANK — dividing by it is the bug this
        test pins down.
        """
        counts = counts_from({
            1: 2, 2: 3, 3: 4, 4: 7, 5: 4, 6: 2, 7: 6, 8: 3, 9: 2, 10: 5,
            11: 4, 12: 3, 14: 1, 18: 1,          # within 18 weeks -> 47
            21: 1, 23: 1, 29: 1, 31: 1,          # beyond 18 weeks -> 4
        })
        self.assertEqual(sum(counts), 51.0)
        self.assertEqual(pct_within_18(UPPERS, counts), "92.2")

    def test_missing_bands_are_null_never_zero(self):
        """All-blank/zero bands: no denominator exists -> '' (null), NOT '0.0'.
        (Blank cells reach the aggregator as 0.0 via num().)"""
        self.assertEqual(pct_within_18(UPPERS, [0.0] * len(UPPERS)), "")

    def test_true_zero_percent_is_distinct_from_null(self):
        """Everyone waiting >18 weeks is a real 0.0%, not a null."""
        self.assertEqual(pct_within_18(UPPERS, counts_from({40: 7})), "0.0")

    def test_18_week_boundary(self):
        """'Gt 17 To 18' (upper 18) is INSIDE the standard; 'Gt 18 To 19'
        (upper 19) is outside. One patient in each -> 50.0%."""
        self.assertEqual(
            pct_within_18(UPPERS, counts_from({18: 1, 19: 1})), "50.0")

    def test_open_ended_band_in_denominator(self):
        """The 'Gt 104 Weeks' band (upper coded 999) counts in the
        denominator only: 3 within, 1 waiting >104 weeks -> 75.0%."""
        self.assertEqual(
            pct_within_18(UPPERS, counts_from({5: 3, 999: 1})), "75.0")


if __name__ == "__main__":
    unittest.main()
