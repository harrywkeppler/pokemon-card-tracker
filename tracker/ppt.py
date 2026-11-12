"""Optional sales-volume lookup through the PokemonPriceTracker API.

Only runs when a PPT_API_KEY secret is present. On the free plan (100
credits a day) that covers about 25 cards daily, so the budget goes to:
  1. cards in watchlist.csv            (tracked every day -> real baselines)
  2. today's alerts, strongest first   (does volume back the price move?)
  3. cards that alerted recently       (keeps their baseline building)

NOTE: written from the provider's published field list
(https://www.pokemonpricetracker.com/llms.txt); the first raw reply is
saved to data/debug/ppt_last_response.json so the parsing can be checked
against a live response.
"""
from __future__ import annotations

import csv
import json
import os
import re
import time
import urllib.error
import urllib.request


def read_watchlist(root: str) -> list[int]:
    p = os.path.join(root, "watchlist.csv")
    out = []
    if os.path.exists(p):
        with open(p, encoding="utf-8", newline="") as f:
            for row in csv.reader(f):
                if row and row[0].strip().isdigit():
                    out.append(int(row[0].strip()))
    return out


def pick_targets(watchlist: list[int], alerts: list[dict], alert_log: list[dict],
                 today_iso: str, cfg: dict) -> list[int]:
    v = cfg["volume_api"]
    limit = max(int(v["daily_credits"] // v["credits_per_card"]), 0)
    seen, out = set(), []

    def add(pid):
        if pid not in seen and len(out) < limit:
            seen.add(pid)
            out.append(pid)

    for pid in watchlist:
        add(pid)
    for a in alerts:
        add(a["pid"])
    recent_days = sorted({r["date"] for r in alert_log})[-v["recent_alert_days"]:]
    for r in reversed(alert_log):
        if r["date"] in recent_days and r["productId"].isdigit():
            add(int(r["productId"]))
    return out


def _dig_history(card: dict) -> dict:
    """Sum units sold per day across printings/conditions."""
    per_day: dict = {}
    variants = ((card.get("priceHistory") or {}).get("variants")) or {}
    for _printing, conds in variants.items():
        if not isinstance(conds, dict):
            continue
        for _cond, body in conds.items():
            hist = body.get("history") if isinstance(body, dict) else None
            for pt in hist or []:
                d, vol = pt.get("date"), pt.get("volume")
                if d:  # a null volume means no sale that day: record it as zero
                    per_day[str(d)[:10]] = per_day.get(str(d)[:10], 0.0) + float(vol or 0)
    return per_day


def _dig_ebay(card: dict) -> dict | None:
    e = card.get("ebay")
    if not isinstance(e, dict):
        return None
    out = {"sales_velocity": e.get("salesVelocity"), "total_sales": e.get("totalSales"),
           "grades": {}}
    for grade, body in (e.get("salesByGrade") or {}).items():
        if isinstance(body, dict):
            out["grades"][grade] = {
                "count": body.get("count"),
                "median": body.get("medianPrice"),
                "market_7d": body.get("marketPrice7Day"),
            }
    return out


# salesByGrade keys: grader + whole grade, "_5" for a half grade (psa10, psa8_5, cgc9_5)
GRADE_KEY = re.compile(r"^([a-z]+)(\d+)(?:_(5))?$")


def parse_grade(key: str):
    """'psa8_5' -> ('psa', 8.5); None for a key that is not a grader and grade."""
    m = GRADE_KEY.match(str(key).strip().lower())
    if not m:
        return None
    return m.group(1), int(m.group(2)) + (0.5 if m.group(3) else 0.0)


def graded_in_range(ebay_entry: dict | None, gcfg: dict) -> list[dict]:
    """The chosen grader's grades within [min_grade, max_grade] that have sales, best first."""
    out = []
    for key, st in ((ebay_entry or {}).get("grades") or {}).items():
        g = parse_grade(key)
        if not g or g[0] != gcfg["grader"].lower() \
                or not (gcfg["min_grade"] <= g[1] <= gcfg["max_grade"]):
            continue
        try:
            count = int(st.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            continue
        out.append({"label": f"{g[0].upper()} {g[1]:g}", "grade": g[1], "count": count,
                    "median": st.get("median"), "market_7d": st.get("market_7d")})
    return sorted(out, key=lambda x: -x["grade"])


def apply_graded_filter(alerts: list[dict], ebay: dict, gcfg: dict) -> list[dict]:
    """Keep alerts whose card has enough graded sales in range, with them attached
    as a["graded"]. A card the lookup budget did not reach today has no data and
    is left out: it cannot be shown to have graded sales."""
    kept = []
    for a in alerts:
        g = graded_in_range(ebay.get(a["pid"]) or ebay.get(str(a["pid"])), gcfg)
        if g and sum(x["count"] for x in g) >= int(gcfg.get("min_sales", 1)):
            kept.append({**a, "graded": g})
    return kept


# TCGplayer's raw-card conditions, best first, with the short names shown in reports
CONDITIONS = [("Near Mint", "NM"), ("Lightly Played", "LP"), ("Moderately Played", "MP"),
              ("Heavily Played", "HP"), ("Damaged", "DMG")]
CONDITION_ABBR = dict(CONDITIONS)


def _dig_conditions(card: dict) -> dict:
    """{printing: {"used": condition the market price is for, or None, "days": days of history,
                   "by": {condition: {"price": latest price, "sales_30d": copies sold}}}}

    From priceHistory.variants[printing][condition]: `latestPrice`, and `history`
    (one point per day with `market` and `volume`), plus
    prices.variants[printing].conditionUsed. "sales_30d" counts the sales in
    whatever history the plan returns: 3 days on the free plan, more on paid
    ones; "days" says how many."""
    out: dict = {}
    hist = ((card.get("priceHistory") or {}).get("variants")) or {}
    for printing, conds in hist.items():
        if not isinstance(conds, dict):
            continue
        by, days = {}, 0
        for cond, body in conds.items():
            points = body.get("history") if isinstance(body, dict) else None
            points = sorted((p for p in points or [] if isinstance(p, dict) and p.get("date")),
                            key=lambda p: str(p["date"]))
            latest = body.get("latestPrice") if isinstance(body, dict) else None
            price = latest if isinstance(latest, (int, float)) else next(
                (p.get("market", p.get("price")) for p in reversed(points)
                 if isinstance(p.get("market", p.get("price")), (int, float))), None)
            sales = sum(float(p.get("volume") or 0) for p in points)
            days = max(days, len({str(p["date"])[:10] for p in points}))
            if price is not None or sales:
                by[str(cond)] = {"price": price, "sales_30d": sales}
        if by:
            out[str(printing)] = {"used": None, "days": days, "by": by}
    for printing, body in (((card.get("prices") or {}).get("variants")) or {}).items():
        if isinstance(body, dict) and isinstance(body.get("conditionUsed"), str):
            out.setdefault(str(printing), {"used": None, "days": 0, "by": {}})["used"] = body["conditionUsed"]
    return out


def history_days(ebay: dict) -> int:
    """Days of condition history the lookups returned (the plan's window), 0 if none."""
    return max((v.get("days") or 0 for e in ebay.values() if isinstance(e, dict)
                for v in (e.get("conditions") or {}).values() if isinstance(v, dict)), default=0)


def conditions_for(entry: dict | None, printing: str):
    """The alert's printing (or, failing that, the only one there is): its
    conditions best first as [{label, condition, price, sales_30d}] and the
    condition TCGplayer's market price is for."""
    variants = (entry or {}).get("conditions") or {}
    v = variants.get(printing) or (next(iter(variants.values())) if len(variants) == 1 else None)
    if not v:
        return [], None
    rows = [{"label": abbr, "condition": name, **v["by"][name]}
            for name, abbr in CONDITIONS if name in v["by"]]
    return rows, v.get("used")


def attach_conditions(alerts: list[dict], ebay: dict) -> list[dict]:
    """Add a["conditions"] to alerts on cards looked up today, and a tag when the
    tracked market price is not a Near Mint price."""
    out = []
    for a in alerts:
        rows, used = conditions_for(ebay.get(a["pid"]) or ebay.get(str(a["pid"])), a["printing"])
        if not rows and not used:
            out.append(a)
            continue
        tags = list(a.get("tags", []))
        if used and used != "Near Mint":
            tags.append(f"price is for {used}, not Near Mint")
        out.append({**a, "conditions": rows, "tags": tags})
    return out


def apply_condition_filter(alerts: list[dict], ccfg: dict) -> list[dict]:
    """Keep alerts on cards with enough sales in the chosen conditions over the
    last 30 days (needs attach_conditions first). A card not looked up today has
    no condition data and is left out."""
    wanted = set(ccfg.get("conditions") or [])
    need = float(ccfg.get("min_sales_30d", 1))
    return [a for a in alerts
            if a.get("conditions")
            and sum(c["sales_30d"] for c in a["conditions"] if c["condition"] in wanted) >= need]


def enrich(cfg: dict, store, targets: list[int], api_key: str, today_iso: str) -> dict:
    """Fetch volume, graded eBay stats and condition prices for `targets`.
    Returns {pid: stats}. 30 days of history cost the same credits as 3."""
    base = cfg["ppt_base_url"].rstrip("/")
    vol = store.read_volume()
    ebay: dict = {}
    saved_debug = False
    for pid in targets:
        url = f"{base}/cards/{pid}?includeHistory=true&includeEbay=true&days=30&limit=1"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {api_key}", "User-Agent": cfg["user_agent"]})
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                remaining = resp.headers.get("X-RateLimit-Daily-Remaining")
                body = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            print(f"  volume API stopped: HTTP {e.code}")
            break
        except Exception as e:
            print(f"  volume API error for {pid}: {e}")
            continue
        if not saved_debug:
            dbg = os.path.join(store.data, "debug")
            os.makedirs(dbg, exist_ok=True)
            with open(os.path.join(dbg, "ppt_last_response.json"), "w", encoding="utf-8") as f:
                f.write(json.dumps(body)[:20000])
            saved_debug = True
        card = body.get("data", body) if isinstance(body, dict) else body
        if isinstance(card, list):
            card = card[0] if card else {}
        if not isinstance(card, dict):
            continue
        for d, v in _dig_history(card).items():
            vol.setdefault(pid, {})[d] = v  # recent days get revised: overwrite
        e = _dig_ebay(card) or {}
        conds = _dig_conditions(card)
        if conds:
            e["conditions"] = conds
        if e:
            ebay[pid] = e
        try:
            if remaining is not None and int(remaining) < cfg["volume_api"]["credits_per_card"]:
                break
        except ValueError:
            pass
        time.sleep(cfg["volume_api"].get("pause_seconds", 1.1))  # 60 calls a minute
    store.write_volume(vol)
    store._save(f"ebay/{today_iso}.json", {str(k): v for k, v in ebay.items()})
    return ebay
