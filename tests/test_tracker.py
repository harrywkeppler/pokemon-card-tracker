"""End-to-end test against a local stand-in for tcgcsv.com.

Builds a simulated market with known planted moves, serves it in tcgcsv's URL
layout (including 7z daily archives), runs the tracker twice (two consecutive
days) and checks that the right cards are flagged.

Run:  python -m unittest discover -s tests -v      (needs: pip install py7zr)
"""
import json
import math
import os
import random
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import py7zr  # noqa: E402

from tracker.__main__ import main  # noqa: E402
from tracker.store import Store  # noqa: E402

T = date(2026, 11, 9)          # "today" on the first run
HIST = 50                      # days of simulated history before T
GROUPS = {3: [101, 102, 103, 104], 85: [901, 902]}
PER_GROUP = 60

PLANTS = {
    101001: "A steady $200 -> $300 over 5 days",
    101002: "B steady $400 -> $270 over 7 days",
    101003: "C steady $150, +20% today only",
    101004: "D volatile card, +20% on the week (normal for it)",
    101005: "E $70 -> $130, breaks into the band",
    101006: "F sealed box $500 -> $800 (not a card)",
    101007: "G $120 -> $50 crash out of the band",
    101008: "H data glitch $300 -> $2000 in one day",
    101009: "J steady $600, +25% on day T+1",
    101010: "K thin card $250, one outlier sale to $2,500, listings flat",
    101011: "L thin card $300 -> $420 on one sale, listings follow",
    101012: "M thin card $180, sells far more often this week, price steady",
}
FIXED_LISTINGS = {101010: (240.0, 262.0)}   # low, mid stay put


def build_market():
    rng = random.Random(7)
    days = [T - timedelta(days=HIST - i) for i in range(HIST + 2)]  # ... T, T+1
    iT = HIST
    prices, products = {}, {}
    for cat, gids in GROUPS.items():
        for gid in gids:
            products[(cat, gid)] = []
            for i in range(1, PER_GROUP + 1):
                pid = gid * 1000 + i
                base = math.exp(rng.uniform(math.log(5), math.log(3000)))
                sig = rng.choice([0.004, 0.008, 0.012, 0.02])
                path, p = [], base
                for _ in days:
                    p *= math.exp(rng.gauss(0, sig))
                    path.append(p)
                prices[pid] = path
                ext = [{"name": "Number", "displayName": "Card Number", "value": f"{i:03d}/{PER_GROUP}"},
                       {"name": "Rarity", "displayName": "Rarity", "value": "Ultra Rare"}]
                products[(cat, gid)].append({
                    "productId": pid, "name": f"Testmon {pid}", "cleanName": f"Testmon {pid}",
                    "groupId": gid, "categoryId": cat,
                    "url": f"https://www.tcgplayer.com/product/{pid}/x", "extendedData": ext})

    def flat(v):
        return [v * (1 + 0.002 * math.sin(k)) for k in range(len(days))]

    a = flat(200)
    for k in range(5):
        a[iT - 4 + k] = 200 + 100 * (k + 1) / 5
    a[iT + 1] = 301
    b = flat(400)
    for k in range(7):
        b[iT - 6 + k] = 400 - 130 * (k + 1) / 7
    b[iT + 1] = 269
    c = flat(150)
    c[iT] = 180
    c[iT + 1] = 181
    d = [250 * math.exp(rng.gauss(0, 0.07)) for _ in days]
    d[iT] = d[iT - 7] * 1.2
    e = flat(70)
    e[iT - 2], e[iT - 1], e[iT], e[iT + 1] = 95, 115, 130, 131
    f = flat(500)
    for k in range(4):
        f[iT - 3 + k] = 500 + 300 * (k + 1) / 4
    g = flat(120)
    g[iT - 1], g[iT], g[iT + 1] = 80, 50, 50
    h = flat(300)
    h[iT] = 2000
    h[iT + 1] = 300
    j = flat(600)
    j[iT + 1] = 750
    k = [250.0] * len(days)
    for i in range(iT - 3, len(days)):
        k[i] = 2500.0
    k[iT - 30] = 255.0
    k[iT - 20] = 250.0
    k[iT - 12] = 252.0
    k[iT - 9] = 250.0
    lthin = [300.0] * len(days)
    lthin[iT - 40] = 305.0
    for i in range(iT - 2, len(days)):
        lthin[i] = 420.0
    mthin = [180.0] * len(days)
    for i in (iT - 25, iT - 15):
        mthin[i] = 182.0
    for i, v in zip(range(iT - 5, iT + 1), [183, 180, 184, 181, 183, 182]):
        mthin[i] = float(v)
    for pid, path in zip(sorted(PLANTS), [a, b, c, d, e, f, g, h, j, k, lthin, mthin]):
        prices[pid] = path
    # F is sealed product: no card number
    for p in products[(3, 101)]:
        if p["productId"] == 101006:
            p["extendedData"] = []
            p["name"] = "Testmon Booster Box"
    return days, prices, products


def price_rows(prices, products, cat, gid, idx):
    rows = []
    for p in products[(cat, gid)]:
        m = round(prices[p["productId"]][idx], 2)
        low, mid = FIXED_LISTINGS.get(p["productId"], (round(m * 0.95, 2), round(m * 1.06, 2)))
        rows.append({"productId": p["productId"], "lowPrice": low,
                     "midPrice": mid, "highPrice": round(m * 2, 2),
                     "marketPrice": m, "directLowPrice": None, "subTypeName": "Holofoil"})
    # a row with no market price must be ignored
    rows.append({"productId": gid * 1000 + 999, "lowPrice": 300, "midPrice": 300,
                 "highPrice": 300, "marketPrice": None, "directLowPrice": None,
                 "subTypeName": "Normal"})
    return rows


class FakeSite:
    def __init__(self, workdir):
        self.days, self.prices, self.products = build_market()
        self.idx = HIST          # index of the current day
        self.archive_hits = {}
        self.bad_agents = 0
        self.ppt_cards = {}      # stand-in PokemonPriceTracker replies: {pid: card body}
        self.ppt_hits = []
        self.arc_dir = os.path.join(workdir, "arc")
        os.makedirs(self.arc_dir)
        for i in range(HIST - 45, HIST + 1):
            self._make_archive(i)

    def _make_archive(self, i):
        day = self.days[i].isoformat()
        src = tempfile.mkdtemp()
        try:
            for cat, gids in GROUPS.items():
                for gid in gids:
                    d = os.path.join(src, day, str(cat), str(gid))
                    os.makedirs(d)
                    with open(os.path.join(d, "prices"), "w") as f:
                        json.dump({"success": True, "errors": [],
                                   "results": price_rows(self.prices, self.products, cat, gid, i)}, f)
            junk = os.path.join(src, day, "1", "55")   # another game: must be skipped
            os.makedirs(junk)
            with open(os.path.join(junk, "prices"), "w") as f:
                json.dump({"success": True, "errors": [], "results": [
                    {"productId": 1, "marketPrice": 999, "midPrice": 999, "lowPrice": 999,
                     "subTypeName": "Normal"}]}, f)
            filters = [{"id": py7zr.FILTER_PPMD, "order": 6, "mem": 24}]
            with py7zr.SevenZipFile(os.path.join(self.arc_dir, f"prices-{day}.ppmd.7z"),
                                    "w", filters=filters) as z:
                z.writeall(os.path.join(src, day), arcname=day)
        finally:
            shutil.rmtree(src)

    def handler(site):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, body, code=200):
                if isinstance(body, (dict, list)):
                    body = json.dumps(body).encode()
                elif isinstance(body, str):
                    body = body.encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                ua = self.headers.get("User-Agent", "")
                if not ua.startswith("PokemonCardTracker/"):
                    site.bad_agents += 1
                    return self._send("blocked", 403)
                parts = self.path.split("?")[0].strip("/").split("/")
                today = site.days[site.idx]
                if parts[0] == "ppt" and len(parts) == 3 and parts[1] == "cards":
                    if self.headers.get("Authorization") != "Bearer test-key":
                        return self._send("unauthorized", 401)
                    pid = int(parts[2])
                    site.ppt_hits.append(pid)
                    if pid not in site.ppt_cards:
                        return self._send({"error": "not found"}, 404)
                    return self._send({"data": site.ppt_cards[pid]})
                if self.path == "/last-updated.txt":
                    return self._send(f"{today.isoformat()}T20:00:05.909835123Z")
                if parts[0] == "archive":
                    name = parts[-1]
                    site.archive_hits[name] = site.archive_hits.get(name, 0) + 1
                    p = os.path.join(site.arc_dir, name)
                    if not os.path.exists(p):
                        return self._send("nope", 404)
                    with open(p, "rb") as f:
                        return self._send(f.read())
                if parts[0] == "tcgplayer" and len(parts) == 3 and parts[2] == "groups":
                    cat = int(parts[1])
                    res = [{"groupId": g, "name": f"SET{g}: Test Set {g}", "abbreviation": f"T{g}",
                            "categoryId": cat} for g in GROUPS.get(cat, [])]
                    if cat == 3:
                        res.append({"groupId": 777, "name": "Empty Promo Set", "categoryId": 3})
                    return self._send({"totalItems": len(res), "success": True, "errors": [], "results": res})
                if parts[0] == "tcgplayer" and len(parts) == 4:
                    cat, gid = int(parts[1]), int(parts[2])
                    if (cat, gid) not in site.products:
                        return self._send("missing", 403)
                    if parts[3] == "prices":
                        return self._send({"success": True, "errors": [],
                                           "results": price_rows(site.prices, site.products, cat, gid, site.idx)})
                    if parts[3] == "products":
                        res = site.products[(cat, gid)]
                        return self._send({"totalItems": len(res), "success": True, "errors": [], "results": res})
                return self._send("?", 404)
        return H


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="pct_test_")
        cls.site = FakeSite(cls.work)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeSite.handler(cls.site))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.repo = os.path.join(cls.work, "repo")
        os.makedirs(cls.repo)
        with open(os.path.join(ROOT, "config.json")) as f:
            cfg = json.load(f)
        cfg["request_pause_seconds"] = 0
        with open(os.path.join(cls.repo, "config.json"), "w") as f:
            json.dump(cfg, f)
        os.environ["TCGCSV_BASE_URL"] = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        os.environ.pop("PPT_API_KEY", None)
        main(["run", "--root", cls.repo])
        cls.day1 = cls._latest()
        shutil.copy(os.path.join(cls.repo, "docs", "index.html"), os.path.join(cls.work, "day1.html"))
        cls.hits_after_day1 = dict(cls.site.archive_hits)
        main(["run", "--root", cls.repo])          # same day again: must be a no-op download
        cls.site.idx += 1                           # next day
        main(["run", "--root", cls.repo])
        cls.day2 = cls._latest()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        os.environ.pop("TCGCSV_BASE_URL", None)
        keep = os.environ.get("KEEP_TEST_OUTPUT")
        if keep:
            shutil.copytree(cls.repo, keep, dirs_exist_ok=True)
        shutil.rmtree(cls.work, ignore_errors=True)

    @classmethod
    def _latest(cls):
        with open(os.path.join(cls.repo, "docs", "latest.json")) as f:
            return json.load(f)

    def _alert(self, payload, pid):
        return next((a for a in payload["alerts"] if a["id"] == pid), None)

    def test_history_seeded_once(self):
        st = Store(self.repo)
        self.assertEqual(len(st.dates()), 47)                 # 45 archive days + T + T+1
        self.assertTrue(all(v == 1 for v in self.site.archive_hits.values()))
        self.assertEqual(self.hits_after_day1, self.site.archive_hits)  # nothing re-downloaded
        self.assertEqual(self.site.bad_agents, 0)

    def test_other_games_and_null_prices_ignored(self):
        snap = Store(self.repo).read_snapshot(T.isoformat())
        self.assertNotIn((1, "Normal"), snap)
        self.assertFalse(any(pid % 1000 == 999 for pid, _ in snap))
        self.assertTrue(all(25 <= v[0] <= 10000 for v in snap.values()))

    def test_planted_rise(self):
        a = self._alert(self.day1, 101001)
        self.assertIsNotNone(a)
        self.assertEqual((a["dir"], a["sev"]), ("up", "high"))
        self.assertAlmostEqual(a["c7"], 50, delta=1.5)
        self.assertIn("7-day move", a["why"])
        self.assertTrue(any(t.endswith("sales same way") for t in a["tags"]))
        self.assertTrue(a["new"])
        self.assertGreaterEqual(a["pc"], 98)

    def test_planted_fall(self):
        b = self._alert(self.day1, 101002)
        self.assertEqual((b["dir"], b["sev"]), ("down", "high"))
        self.assertAlmostEqual(b["c7"], -32.5, delta=1.5)

    def test_one_day_jump_is_medium(self):
        c = self._alert(self.day1, 101003)
        self.assertEqual(c["sev"], "medium")
        self.assertIn("1-day jump", c["why"])
        self.assertFalse(any(t.endswith("sales same way") for t in c["tags"]))

    def test_volatile_card_not_flagged_for_normal_swing(self):
        d = self._alert(self.day1, 101004)
        self.assertTrue(d is None or "7-day move" not in d["why"])

    def test_breakout_into_band(self):
        e = self._alert(self.day1, 101005)
        self.assertIsNotNone(e)
        self.assertIn("broke above $100", e["tags"])

    def test_sealed_product_excluded(self):
        self.assertIsNone(self._alert(self.day1, 101006))

    def test_crash_out_of_band_still_reported(self):
        g = self._alert(self.day1, 101007)
        self.assertIsNotNone(g)
        self.assertEqual(g["dir"], "down")
        self.assertIn("fell below $100", g["tags"])

    def test_glitch_is_downgraded(self):
        h = self._alert(self.day1, 101008)
        self.assertEqual(h["sev"], "watch")
        self.assertTrue(any("verify" in t for t in h["tags"]))

    def test_outlier_sale_ignored(self):
        self.assertIsNone(self._alert(self.day1, 101010))

    def test_thin_card_move_confirmed_by_listings(self):
        lt = self._alert(self.day1, 101011)
        self.assertIsNotNone(lt)
        self.assertIn("7-day move", lt["why"])
        self.assertIn("listings agree", lt["tags"])
        self.assertIn("thinly traded", lt["tags"])
        self.assertEqual(lt["sev"], "medium")

    def test_selling_more_often(self):
        m = self._alert(self.day1, 101012)
        self.assertIsNotNone(m)
        self.assertEqual(m["why"], ["selling more often"])
        self.assertEqual(m["sev"], "watch")

    def test_second_day(self):
        a = self._alert(self.day2, 101001)
        self.assertIsNotNone(a)
        self.assertFalse(a["new"])                     # continuing, not new
        j = self._alert(self.day2, 101009)
        self.assertIsNotNone(j)
        self.assertTrue(j["new"])
        self.assertIsNone(self._alert(self.day1, 101009))

    def test_few_false_alarms(self):
        planted = set(PLANTS)
        noise = [a for a in self.day1["alerts"] if a["id"] not in planted]
        self.assertLessEqual(len(noise), 3, [(a["id"], a["why"], a["c7"]) for a in noise])

    def test_outputs_written(self):
        for rel in ("docs/index.html", "REPORT.md", "data/alert_log.csv", "data/catalog.json"):
            self.assertTrue(os.path.getsize(os.path.join(self.repo, rel)) > 100, rel)
        with open(os.path.join(self.repo, "docs", "index.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertNotIn("/*__DATA__*/null", html)
        from openpyxl import load_workbook
        wb = load_workbook(os.path.join(self.repo, "out", "tracker.xlsx"))
        self.assertEqual(wb.sheetnames[:3], ["My view", "Settings", "Alerts"])
        self.assertEqual(wb["_view"].sheet_state, "hidden")
        tables = {t for ws in wb.worksheets for t in ws.tables}
        self.assertEqual(tables, {"tblAlerts", "tblWatch", "tblGainers", "tblDecliners",
                                  "tblCards", "tblHistory", "tblPrices"})
        ids = [r[-1] for r in wb["Alerts"].iter_rows(min_row=2, values_only=True)]
        self.assertIn(101001, ids)
        self.assertNotIn(101010, ids)
        log = Store(self.repo).read_alert_log()
        self.assertEqual(len({r["date"] for r in log}), 2)

    def test_workbook_view_formulas_point_at_the_right_columns(self):
        from openpyxl import load_workbook
        from openpyxl.utils import get_column_letter
        wb = load_workbook(os.path.join(self.repo, "out", "tracker.xlsx"))
        cards = wb["All cards"]
        letter = {c.value: get_column_letter(c.column) for c in cards[1]}
        last = cards.max_row
        # settings: named cells with defaults from config.json
        names = {n: wb.defined_names[n].attr_text for n in wb.defined_names}
        self.assertEqual(names["setMinPrice"], "Settings!$B$4")
        self.assertEqual(wb["Settings"]["B4"].value, 100)
        self.assertEqual(wb["Settings"]["B6"].value, 0.15)
        # hidden helper: one row per card, reading that row's own price and signal
        hv = wb["_view"]
        self.assertEqual(hv.max_row, last)
        f = hv[f"A{last}"].value
        self.assertIn(f"'All cards'!{letter['Price']}{last}>=setMinPrice", f)
        self.assertIn(f"'All cards'!{letter['Signal']}{last}", f)
        self.assertIn(f"N('All cards'!{letter['Graded sales count']}{last})>0", f)
        # My view row 1 picks the largest sort key and reads the Card column
        vw = wb["My view"]
        self.assertEqual(vw["A5"].value, f'=IFERROR(MATCH(LARGE(_view!$B$2:$B${last},1),_view!$B$2:$B${last},0),"")')
        self.assertIn(f"'All cards'!${letter['Card']}$2:${letter['Card']}${last}", vw["C5"].value)
        self.assertEqual([c.value for c in vw[4]][2:4], ["Card", "Set"])


def ppt_card(grades, conditions=None, used=None):
    """A card reply in the API's shape: graded sales stats per grade key, and
    per-condition history for the Holofoil printing: {condition: (price, sold per day)}."""
    card = {"ebay": {"salesVelocity": 0.5, "totalSales": sum(n for n, _ in grades.values()),
                     "salesByGrade": {k: {"count": n, "medianPrice": med, "marketPrice7Day": med}
                                      for k, (n, med) in grades.items()}}}
    if conditions:
        days = [(T - timedelta(days=i)).isoformat() for i in range(3)]
        card["priceHistory"] = {"variants": {"Holofoil": {
            cond: {"history": [{"date": d, "price": price, "volume": sold or None} for d in days]}
            for cond, (price, sold) in conditions.items()}}}
    if used:
        card["prices"] = {"variants": {"Holofoil": {"conditionUsed": used}}}
    return card


class GradedEndToEnd(unittest.TestCase):
    """With PPT_API_KEY set, only alerts on cards with PSA 7-10 sales are kept."""

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="pct_graded_")
        cls.site = FakeSite(cls.work)
        cls.site.ppt_cards = {
            101001: ppt_card({"psa10": (3, 1250.0), "psa9": (5, 480.0), "psa6": (9, 90.0)},
                             {"Near Mint": (305.0, 1), "Lightly Played": (250.0, 0),
                              "Damaged": (90.0, 2)}, used="Lightly Played"),
            # PSA 7-10 sales, but raw copies only sell Moderately Played: fails the condition filter
            101007: ppt_card({"psa9": (2, 70.0)}, {"Moderately Played": (40.0, 1)}),
            101002: ppt_card({"psa6": (4, 150.0), "cgc10": (2, 900.0)}),   # nothing in PSA 7-10
            101003: ppt_card({"psa7": (0, None)}),                            # no sales
        }
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeSite.handler(cls.site))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.repo = os.path.join(cls.work, "repo")
        os.makedirs(cls.repo)
        with open(os.path.join(ROOT, "config.json")) as f:
            cfg = json.load(f)
        cfg["request_pause_seconds"] = 0
        cfg["ppt_base_url"] = f"{base}/ppt"
        cfg["volume_api"]["pause_seconds"] = 0
        with open(os.path.join(cls.repo, "config.json"), "w") as f:
            json.dump(cfg, f)
        os.environ["TCGCSV_BASE_URL"] = base
        os.environ["PPT_API_KEY"] = "test-key"
        try:
            main(["run", "--root", cls.repo])
        finally:
            os.environ.pop("PPT_API_KEY", None)
            os.environ.pop("TCGCSV_BASE_URL", None)
        with open(os.path.join(cls.repo, "docs", "latest.json")) as f:
            cls.payload = json.load(f)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.work, ignore_errors=True)

    def test_only_cards_with_psa_7_to_10_sales_and_nm_lp_sales_kept(self):
        self.assertIn(101002, self.site.ppt_hits)          # alerts were looked up...
        self.assertIn(101007, self.site.ppt_hits)
        self.assertEqual([a["id"] for a in self.payload["alerts"]], [101001])   # ...and filtered

    def test_condition_prices_and_not_near_mint_flag(self):
        a = self.payload["alerts"][0]
        self.assertEqual(a["cond"], [["NM", 305.0, 3], ["LP", 250.0, 0], ["DMG", 90.0, 6]])
        self.assertIn("price is for Lightly Played, not Near Mint", a["tags"])

    def test_psa_prices_attached_best_grade_first(self):
        a = self.payload["alerts"][0]
        self.assertEqual(a["gr"], [["PSA 10", 1250.0, 3], ["PSA 9", 480.0, 5]])

    def test_note_in_outputs(self):
        note = self.payload["meta"]["filter_note"]
        self.assertTrue(note.startswith("Only cards with PSA 7–10 sales: 2 of "), note)
        self.assertIn("Only cards sold in Near Mint or Lightly Played in the last 30 days: 1 of 2 kept.", note)
        with open(os.path.join(self.repo, "REPORT.md"), encoding="utf-8") as f:
            md = f.read()
        self.assertIn(note, md)
        self.assertIn("PSA 10 $1,250 (3) · PSA 9 $480 (5)", md)
        self.assertIn("NM $305 (3) · LP $250 (0) · DMG $90 (6)", md)
        log = Store(self.repo).read_alert_log()
        self.assertEqual({r["productId"] for r in log}, {"101001"})


class NoKeyNote(unittest.TestCase):
    def test_unfiltered_without_key_and_says_so(self):
        # EndToEnd runs without PPT_API_KEY: alerts stay unfiltered and the note says why
        note = EndToEnd.day1["meta"]["filter_note"]
        self.assertIn("PSA 7–10 and condition filters need PPT_API_KEY", note)


if __name__ == "__main__":
    unittest.main()
