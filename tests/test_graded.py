"""The PSA 7-10 filter: parsing salesByGrade keys and keeping only cards with
graded sales in range. Offline: the eBay stats are written by hand in the shape
tracker.ppt._dig_ebay produces from the API reply.

Run:  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker import ppt  # noqa: E402
from tracker.report import graded_text  # noqa: E402

PSA_7_10 = {"enabled": True, "grader": "psa", "min_grade": 7, "max_grade": 10, "min_sales": 1}


def stats(count, median=None):
    return {"count": count, "median": median, "market_7d": None}


class ParseGrade(unittest.TestCase):
    def test_whole_and_half_grades(self):
        self.assertEqual(ppt.parse_grade("psa10"), ("psa", 10.0))
        self.assertEqual(ppt.parse_grade("psa8_5"), ("psa", 8.5))
        self.assertEqual(ppt.parse_grade("CGC9_5"), ("cgc", 9.5))

    def test_not_a_grade(self):
        for key in ("ungraded", "psa", "psa9_69", "", "10"):
            self.assertIsNone(ppt.parse_grade(key), key)


class GradedInRange(unittest.TestCase):
    def test_only_psa_7_to_10_with_sales_best_first(self):
        entry = {"grades": {
            "psa6": stats(9, 40), "psa7": stats(3, 90), "psa8_5": stats(2, 140),
            "psa10": stats(5, 900), "psa9": stats(0, 300),       # no sales
            "cgc10": stats(4, 700), "bgs9_5": stats(1, 500),     # other graders
        }}
        got = ppt.graded_in_range(entry, PSA_7_10)
        self.assertEqual([g["label"] for g in got], ["PSA 10", "PSA 8.5", "PSA 7"])
        self.assertEqual(got[0]["count"], 5)
        self.assertEqual(got[0]["median"], 900)

    def test_missing_or_odd_data(self):
        self.assertEqual(ppt.graded_in_range(None, PSA_7_10), [])
        self.assertEqual(ppt.graded_in_range({"grades": {"psa10": {"count": None}}}, PSA_7_10), [])
        self.assertEqual(ppt.graded_in_range({"grades": {"psa10": {"count": "x"}}}, PSA_7_10), [])


class ApplyFilter(unittest.TestCase):
    def setUp(self):
        self.alerts = [{"pid": 1, "c7": 40}, {"pid": 2, "c7": 30}, {"pid": 3, "c7": 20}, {"pid": 4, "c7": 10}]
        self.ebay = {
            1: {"grades": {"psa9": stats(4, 450), "psa10": stats(2, 1200)}},
            2: {"grades": {"psa6": stats(8, 60), "cgc10": stats(3, 800)}},   # nothing in range
            "3": {"grades": {"psa7": stats(1, 95)}},                          # key as a string
            # card 4 was not looked up today (budget): no data
        }

    def test_keeps_cards_with_psa_7_to_10_sales(self):
        kept = ppt.apply_graded_filter(self.alerts, self.ebay, PSA_7_10)
        self.assertEqual([a["pid"] for a in kept], [1, 3])
        self.assertEqual([g["label"] for g in kept[0]["graded"]], ["PSA 10", "PSA 9"])
        self.assertNotIn("graded", self.alerts[0])          # inputs left untouched

    def test_min_sales(self):
        kept = ppt.apply_graded_filter(self.alerts, self.ebay, {**PSA_7_10, "min_sales": 3})
        self.assertEqual([a["pid"] for a in kept], [1])

    def test_report_text(self):
        self.assertEqual(graded_text([["PSA 10", 1200.0, 2], ["PSA 9", None, 4]]),
                         "PSA 10 $1,200 (2) · PSA 9 – (4)")


if __name__ == "__main__":
    unittest.main()
