"""Hourly listing scan against a local stand-in for eBay's Browse API.

Two cards: a raw Charizard with TCGplayer condition prices and a Lugia with PSA
sale medians (as the PokemonPriceTracker lookup stores them). The fake eBay
serves new, old, mismatched and fairly priced listings around one bargain of
each kind; only the bargains may be flagged.

Run:  python -m unittest discover -s tests -v
"""
import base64
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tracker import listings  # noqa: E402
from tracker.store import Store  # noqa: E402

NOW = datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc)


def ago(hours):
    return (NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def item(iid, title, price, hours, cond="4000", ship=None, descriptors=None, watch=None):
    s = {"itemId": iid, "title": title, "price": {"value": f"{price:.2f}", "currency": "USD"},
         "conditionId": cond, "itemOriginDate": ago(hours), "itemCreationDate": ago(hours),
         "itemWebUrl": f"https://www.ebay.com/itm/{iid}", "buyingOptions": ["FIXED_PRICE"]}
    if ship is not None:
        s["shippingOptions"] = [{"shippingCost": {"value": f"{ship:.2f}", "currency": "USD"}}]
    if watch is not None:
        s["watchCount"] = watch
    full = dict(s, conditionDescriptors=[{"name": n, "values": [{"content": v}]}
                                         for n, v in (descriptors or [])])
    return s, full


RAW_NM = [("Card Condition", "Near mint or better")]
RAW_LP = [("Card Condition", "Excellent")]
PSA = lambda g: [("Professional Grader", "Professional Sports Authenticator (PSA)"), ("Grade", g)]  # noqa: E731

LISTINGS = {
    "Charizard": [
        item("c-deal", "Charizard ex 223/197 Obsidian Flames Holo", 250, 3, ship=5, descriptors=RAW_NM),
        item("c-lp-fair", "Charizard ex 223/197 Obsidian Flames LP", 320, 4, descriptors=RAW_LP),
        item("c-too-new", "Charizard ex 223/197 NM pack fresh", 200, 1, descriptors=RAW_NM),
        item("c-too-old", "Charizard ex 223/197 NM", 410, 20, descriptors=RAW_NM),
        item("c-lot", "Charizard ex 223/197 NM lot of 3", 150, 3, descriptors=RAW_NM),
        item("c-japanese", "Charizard ex 223/197 Japanese NM", 120, 3, descriptors=RAW_NM),
        item("c-wrong-number", "Charizard ex 199/197 NM", 110, 3, descriptors=RAW_NM),
        item("c-listed-1", "Charizard ex 223/197 Obsidian Flames NM", 430, 30, descriptors=RAW_NM),
        item("c-listed-2", "Charizard ex 223/197 Near Mint", 445, 40, descriptors=RAW_NM),
    ],
    "Lugia": [
        item("l-psa10-deal", "Lugia 9/111 Neo Genesis PSA 10 Gem Mint", 1500, 5, cond="2750",
             descriptors=PSA("10"), watch=7),
        item("l-psa6", "Lugia 9/111 Neo Genesis PSA 6", 300, 5, cond="2750", descriptors=PSA("6")),
        item("l-cgc10", "Lugia 9/111 Neo Genesis CGC 10", 900, 5, cond="2750",
             descriptors=[("Professional Grader", "Certified Guaranty Company (CGC)"), ("Grade", "10")]),
    ],
}


class FakeEbay:
    def __init__(self):
        self.searches, self.item_calls, self.tokens = [], 0, 0

    def handler(site):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                want = "Basic " + base64.b64encode(b"test-id:test-secret").decode()
                if self.path != "/identity/v1/oauth2/token" or self.headers.get("Authorization") != want:
                    return self._send({"error": "invalid_client"}, 401)
                site.tokens += 1
                return self._send({"access_token": "tok", "expires_in": 7200})

            def do_GET(self):
                if self.headers.get("Authorization") != "Bearer tok":
                    return self._send({}, 401)
                u = urlparse(self.path)
                qs = {k: v[0] for k, v in parse_qs(u.query).items()}
                if u.path == "/buy/browse/v1/item_summary/search":
                    site.searches.append(qs)
                    for key, rows in LISTINGS.items():
                        if key.lower() in qs["q"].lower():
                            return self._send({"itemSummaries": [s for s, _ in rows]})
                    return self._send({"itemSummaries": []})
                if u.path == "/buy/browse/v1/item":
                    site.item_calls += 1
                    ids = qs["item_ids"].split(",")
                    full = {f["itemId"]: f for rows in LISTINGS.values() for _, f in rows}
                    return self._send({"items": [full[i] for i in ids if i in full]})
                return self._send({}, 404)
        return H


class ListingScan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="pct_listings_")
        cls.site = FakeEbay()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeEbay.handler(cls.site))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        with open(os.path.join(ROOT, "config.json")) as f:
            cls.cfg = json.load(f)
        cls.cfg["listing_scan"].update(ebay_base_url=f"http://127.0.0.1:{cls.srv.server_address[1]}",
                                       pause_seconds=0, cards_per_run=5)
        st = cls.store = Store(cls.root)
        st.save_catalog({
            "5001": {"name": "Charizard ex", "group": "Obsidian Flames", "number": "223/197", "cat": 3,
                     "url": "https://www.tcgplayer.com/product/5001", "is_card": True},
            "5002": {"name": "Lugia", "group": "Neo Genesis", "number": "9/111", "cat": 3,
                     "url": "https://www.tcgplayer.com/product/5002", "is_card": True},
        })
        st.write_snapshot("2026-11-10", {(5001, "Holofoil"): (400.0, 450.0, 380.0),
                                         (5002, "Holofoil"): (1000.0, 1100.0, 950.0)})
        st._save("ebay/2026-11-10.json", {
            "5001": {"grades": {}, "conditions": {"Holofoil": {"used": "Near Mint", "by": {
                "Near Mint": {"price": 420.0, "sales_30d": 6.0},
                "Lightly Played": {"price": 330.0, "sales_30d": 2.0}}}}},
            "5002": {"grades": {"psa10": {"count": 4, "median": 2500.0, "market_7d": None},
                                "psa9": {"count": 6, "median": 900.0, "market_7d": None}}},
        })
        os.environ["EBAY_CLIENT_ID"], os.environ["EBAY_CLIENT_SECRET"] = "test-id", "test-secret"
        try:
            cls.result = listings.run(cls.cfg, st, cls.root, now=NOW)
            cls.again = listings.run(cls.cfg, st, cls.root, now=NOW + timedelta(minutes=60))
        finally:
            os.environ.pop("EBAY_CLIENT_ID", None)
            os.environ.pop("EBAY_CLIENT_SECRET", None)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.root, ignore_errors=True)

    def find(self, iid, result=None):
        return next((f for f in (result or self.result)["finds"] if f["item_id"] == iid), None)

    def test_only_the_two_bargains_biggest_gap_first(self):
        self.assertEqual([f["item_id"] for f in self.result["finds"]], ["l-psa10-deal", "c-deal"])

    def test_listing_enters_the_window_next_hour(self):
        # an hour later the 1-hour-old listing is 2 hours old: now in the window, and new
        self.assertEqual({f["item_id"]: f["new"] for f in self.again["finds"]},
                         {"l-psa10-deal": False, "c-deal": False, "c-too-new": True})

    def test_raw_bargain_against_sales_and_listings(self):
        f = self.find("c-deal")
        self.assertEqual((f["label"], f["total"], f["new"]), ("Raw NM", 255.0, True))
        comps = {c["label"]: c for c in f["comps"]}
        self.assertAlmostEqual(comps["TCGplayer NM price"]["gap_pct"], 39.3, delta=0.1)
        # other NM listings of the card: too new (200), too old (410), 430, 445 -> median 420
        self.assertEqual(comps["eBay listings median (4)"]["price"], 420.0)
        self.assertEqual(f["vs"], "TCGplayer NM price")

    def test_graded_bargain_against_psa_sales(self):
        f = self.find("l-psa10-deal")
        self.assertEqual((f["label"], f["watchers"]), ("PSA 10", 7))
        self.assertEqual(f["vs"], "PSA 10 sold median (4 sales)")
        self.assertEqual(f["gap_pct"], 40.0)

    def test_searches_ask_for_newest_fixed_price(self):
        q = self.site.searches[0]
        self.assertEqual(q["sort"], "newlyListed")
        self.assertIn("buyingOptions:{FIXED_PRICE}", q["filter"])
        self.assertEqual(q["category_ids"], "183454")
        self.assertEqual(self.site.tokens, 2)                 # one sign-in per run
        self.assertEqual(self.result["calls"], 3)             # 2 searches + 1 batched item lookup

    def test_outputs_and_seen_state(self):
        with open(os.path.join(self.root, "LISTINGS.md"), encoding="utf-8") as fh:
            md = fh.read()
        self.assertIn("c-deal", md)
        self.assertIn("PSA 10 sold median (4 sales): $2,500.00", md)
        with open(os.path.join(self.root, "docs", "listings.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(len(data["finds"]), 3)                       # the second run's
        self.assertTrue(os.path.exists(os.path.join(self.root, "out", "listings.xlsx")))
        self.assertFalse(self.find("c-deal", self.again)["new"])   # shown last hour already

    def test_without_keys_nothing_is_written(self):
        root = tempfile.mkdtemp(prefix="pct_nokeys_")
        try:
            r = listings.run(self.cfg, Store(root), root, now=NOW)
            self.assertIn("EBAY_CLIENT_ID", r["note"])
            self.assertFalse(os.path.exists(os.path.join(root, "LISTINGS.md")))
        finally:
            shutil.rmtree(root, ignore_errors=True)


class Matching(unittest.TestCase):
    CARD = {"name": "Charmander - 113/112 (Delta Species)", "number": "113/112", "lang": "English",
            "printing": "Holofoil"}

    def test_names_numbers_editions(self):
        ok = lambda t, card=self.CARD: listings.matches_card(t, card, ["lot", "proxy"])  # noqa: E731
        self.assertEqual(listings.clean_name(self.CARD["name"]), "Charmander")
        self.assertTrue(ok("Charmander 113/112 FireRed LeafGreen Holo NM"))
        self.assertFalse(ok("Charmander 112/112 Holo"))
        self.assertFalse(ok("Charmander 113/112 PROXY card"))
        self.assertFalse(ok("Charmander 113/112 Reverse Holo"))
        self.assertFalse(ok("Charmander 113/112 1st Edition"))
        promo = {"name": "Pikachu", "number": "SWSH050", "lang": "English", "printing": "Holofoil"}
        self.assertTrue(ok("Pikachu SWSH050 promo", promo))

    def test_classify(self):
        raw = listings.classify({"conditionId": "4000", "title": "x"},
                                {"conditionDescriptors": [{"name": "Card Condition",
                                                           "values": [{"content": "Excellent"}]}]})
        self.assertEqual(raw["label"], "Raw LP")
        self.assertEqual(listings.classify({"conditionId": "4000", "title": "Charizard ex NM"})["condition"],
                         "Near Mint")
        g = listings.classify({"conditionId": "2750", "title": "Lugia BGS 9.5"})
        self.assertEqual((g["grader"], g["grade"]), ("bgs", 9.5))
        self.assertIsNone(listings.classify({"conditionId": "2750", "title": "graded slab"}))


if __name__ == "__main__":
    unittest.main()
