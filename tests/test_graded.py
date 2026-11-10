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
from tracker.report import condition_text, graded_text  # noqa: E402

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


def card_reply(variants: dict, used: dict = None) -> dict:
    """{printing: {condition: [(date, price, volume), ...]}} in the API's shape."""
    reply = {"priceHistory": {"variants": {
        pr: {c: {"history": [{"date": d, "price": p, "volume": v} for d, p, v in pts]}
             for c, pts in conds.items()} for pr, conds in variants.items()}}}
    if used:
        reply["prices"] = {"variants": {pr: {"conditionUsed": c} for pr, c in used.items()}}
    return reply


class Conditions(unittest.TestCase):
    def test_dig_latest_price_and_30_day_sales(self):
        got = ppt._dig_conditions(card_reply({"Holofoil": {
            "Near Mint": [("2026-11-10", 410.0, 2), ("2026-11-08", 400.0, None), ("2026-11-09", 405.0, 1)],
            "Damaged": [("2026-11-09", None, None)],               # nothing to show
        }}, used={"Holofoil": "Near Mint"}))
        self.assertEqual(got, {"Holofoil": {"used": "Near Mint",
                                            "by": {"Near Mint": {"price": 410.0, "sales_30d": 3.0}}}})

    def test_odd_replies(self):
        self.assertEqual(ppt._dig_conditions({}), {})
        self.assertEqual(ppt._dig_conditions({"priceHistory": {"variants": {"Holofoil": "x"}}}), {})

    def test_printing_match_order_and_flag(self):
        entry = {"conditions": {
            "Holofoil": {"used": "Lightly Played", "by": {
                "Lightly Played": {"price": 340.0, "sales_30d": 3.0},
                "Near Mint": {"price": 410.0, "sales_30d": 12.0}}},
            "Reverse Holofoil": {"used": None, "by": {"Near Mint": {"price": 90.0, "sales_30d": 1.0}}},
        }}
        rows, used = ppt.conditions_for(entry, "Holofoil")
        self.assertEqual([r["label"] for r in rows], ["NM", "LP"])
        self.assertEqual(used, "Lightly Played")
        self.assertEqual(ppt.conditions_for(entry, "Normal"), ([], None))   # two printings, none match
        a = ppt.attach_conditions([{"pid": 1, "printing": "Holofoil", "tags": ["x"]}], {1: entry})[0]
        self.assertEqual(a["tags"], ["x", "price is for Lightly Played, not Near Mint"])

    def test_filter_on_chosen_conditions(self):
        def alert(pid, **sold):
            names = {"NM": "Near Mint", "LP": "Lightly Played", "MP": "Moderately Played"}
            return {"pid": pid, "conditions": [{"label": k, "condition": names[k], "price": 1.0,
                                               "sales_30d": float(n)} for k, n in sold.items()]}
        alerts = [alert(1, NM=2), alert(2, MP=5), alert(3, LP=1, MP=1), {"pid": 4}]   # 4: not looked up
        ccfg = {"enabled": True, "conditions": ["Near Mint", "Lightly Played"], "min_sales_30d": 1}
        self.assertEqual([a["pid"] for a in ppt.apply_condition_filter(alerts, ccfg)], [1, 3])
        self.assertEqual([a["pid"] for a in ppt.apply_condition_filter(alerts, {**ccfg, "min_sales_30d": 2})], [1])

    def test_report_text(self):
        self.assertEqual(condition_text([["NM", 410.0, 12], ["LP", None, 0]]), "NM $410 (12) · LP – (0)")


if __name__ == "__main__":
    unittest.main()
