"""Hourly scan: new eBay listings priced well under comparable sales and listings.

For each card scanned (watchlist, today's alerts and cards with condition data
first, then the rest of the price band in rotation) it searches eBay's newest
fixed-price listings and keeps those that
  * went up 2-12 hours ago (itemOriginDate, so a relisted card is not "new")
    and are still unsold (only active listings are returned),
  * are the same card (name, number, language, edition, holo type in the
    title; no lots, proxies or customs),
  * are raw Near Mint / Lightly Played, or graded within graded_filter
    (PSA 7-10), read from eBay's condition descriptors, else from the title,
and flags one priced at least min_gap_pct and min_gap_dollars under any of:
  * recent sales in that condition (TCGplayer, from the PokemonPriceTracker
    lookup) or that grade (PSA sale median), or TCGplayer's market price for
    a raw card when no condition price is known,
  * the median of other current eBay listings of the card in the same
    condition or grade (at least min_comparable_listings of them).
Watchers are shown when eBay returns watchCount (needs eBay's approval).

Writes docs/listings.json, LISTINGS.md and out/listings.xlsx; remembers which
listings it has already shown in data/listings/state.json.
"""
from __future__ import annotations

import json
import os
import re
import statistics
from datetime import datetime, timedelta, timezone

from . import ebay, ppt

COND_SHORT = dict(ppt.CONDITIONS)                       # "Near Mint" -> "NM"
DESCRIPTOR_CONDITION = [("near mint", "Near Mint"), ("excellent", "Lightly Played"),
                        ("very good", "Moderately Played"), ("poor", "Heavily Played")]
TITLE_CONDITION = [(re.compile(r"\b(nm|nm-mt|nm/m|near[\s-]?mint)\b", re.I), "Near Mint"),
                   (re.compile(r"\b(lp|lightly[\s-]?played|excellent)\b", re.I), "Lightly Played"),
                   (re.compile(r"\b(mp|moderately[\s-]?played)\b", re.I), "Moderately Played"),
                   (re.compile(r"\b(hp|heavily[\s-]?played|damaged|dmg)\b", re.I), "Heavily Played")]
GRADE_IN_TITLE = re.compile(r"\b(psa|cgc|bgs|sgc|tag)\s*[-:#]?\s*(10|[1-9](?:\.5)?)\b", re.I)
GRADER_IN_TEXT = re.compile(r"\b(PSA|CGC|BGS|SGC|TAG)\b")


def _parse_time(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _money(obj):
    try:
        return float((obj or {}).get("value"))
    except (TypeError, ValueError):
        return None


def descriptor_info(item: dict | None) -> dict:
    """Grader, grade and card condition from a full item's conditionDescriptors."""
    out = {}
    for d in (item or {}).get("conditionDescriptors") or []:
        name = str(d.get("name") or "").lower()
        text = " ".join(str(v.get("content") or "") for v in d.get("values") or [])
        if "grader" in name:
            m = GRADER_IN_TEXT.search(text) or GRADER_IN_TEXT.search(text.upper())
            if m:
                out["grader"] = m.group(1).lower()
            elif "beckett" in text.lower():
                out["grader"] = "bgs"
        elif name.startswith("grade"):
            m = re.search(r"\d+(?:\.5)?", text)
            if m:
                out["grade"] = float(m.group())
        elif "condition" in name:
            low = text.lower()
            out["condition"] = next((c for k, c in DESCRIPTOR_CONDITION if k in low), None)
    return out


def classify(summary: dict, full: dict | None = None) -> dict | None:
    """{"kind": "graded", grader, grade} or {"kind": "raw", condition (None if unstated)}."""
    cid = str(summary.get("conditionId") or (full or {}).get("conditionId") or "")
    title = summary.get("title") or ""
    info = descriptor_info(full)
    if cid == "2750" or info.get("grader"):
        grader, grade = info.get("grader"), info.get("grade")
        if not (grader and grade is not None):
            m = GRADE_IN_TITLE.search(title)
            if m:
                grader, grade = m.group(1).lower(), float(m.group(2))
        if grader and grade is not None:
            return {"kind": "graded", "grader": grader, "grade": grade,
                    "label": f"{grader.upper()} {grade:g}"}
        return None
    if cid == "4000" or (cid == "" and not GRADE_IN_TITLE.search(title)):
        cond = info.get("condition") or next((c for rx, c in TITLE_CONDITION if rx.search(title)), None)
        return {"kind": "raw", "condition": cond,
                "label": f"Raw {COND_SHORT.get(cond, cond)}" if cond else "Raw, condition not stated"}
    return None


def same_class(a: dict | None, b: dict | None) -> bool:
    if not a or not b or a["kind"] != b["kind"]:
        return False
    if a["kind"] == "graded":
        return a["grader"] == b["grader"] and a["grade"] == b["grade"]
    return a["condition"] is not None and a["condition"] == b["condition"]


def clean_name(name: str) -> str:
    """'Charmander - 113/112 (Delta Species)' -> 'Charmander'"""
    name = re.split(r"\s+-\s+", name)[0]
    return re.sub(r"\s*\([^)]*\)", "", name).strip()


def matches_card(title: str, card: dict, exclude: list[str]) -> bool:
    t = f" {title.lower()} "
    set_name = (card.get("set") or "").lower()
    # a word that is part of the card's own set name ("Celebrations") is no reason to skip it
    if any(re.search(rf"\b{re.escape(w.lower())}\b", t) and w.lower() not in set_name for w in exclude):
        return False
    words = re.findall(r"[a-z0-9']+", clean_name(card["name"]).lower())
    if words and words[0] not in t:
        return False
    num = (card.get("number") or "").split("/")[0].strip()
    digits = re.sub(r"^\D*0*", "", num) if num else ""
    if digits and not re.search(rf"(?<![\d]){re.escape(digits)}(?![\d])", re.sub(r"(?<=\D)0+(?=\d)", "", t)):
        return False
    is_jp = bool(re.search(r"\b(japanese|japan|jpn|jp)\b", t))
    if is_jp != (card.get("lang") == "Japanese"):
        return False
    printing = (card.get("printing") or "").lower()
    if re.search(r"\b1st\s*ed", t) and "1st edition" not in printing:
        return False
    if "reverse" in t and "reverse" not in printing:
        return False
    return True


def _latest_ppt(store) -> dict:
    folder = os.path.join(store.data, "ebay")
    try:
        files = sorted(f for f in os.listdir(folder) if f.endswith(".json"))
    except OSError:
        return {}
    for f in reversed(files[-3:]):
        try:
            with open(os.path.join(folder, f), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            continue
    return {}


def pick_cards(cfg: dict, store, root: str, now: datetime, ppt_cache: dict) -> list[dict]:
    """Cards to scan this hour: priority cards first, then the band in rotation."""
    lc = cfg["listing_scan"]
    dates = store.dates()
    if not dates:
        return []
    catalog = store.load_catalog()
    snap = store.read_snapshot(dates[-1])
    lo, hi = cfg["band"]["min"], cfg["band"]["max"]
    cards = {}
    for (pid, printing), (market, _mid, _low) in snap.items():
        m = catalog.get(str(pid)) or {}
        if market is None or not (lo <= market <= hi) or not m.get("is_card"):
            continue
        cards.setdefault(pid, {"pid": pid, "printing": printing, "market": market,
                               "name": m.get("name") or "", "set": m.get("group", ""),
                               "number": m.get("number", ""), "url": m.get("url", ""),
                               "lang": cfg["categories"].get(str(m.get("cat")), "")})
    first = list(ppt.read_watchlist(root))
    try:
        with open(os.path.join(root, "docs", "latest.json"), encoding="utf-8") as f:
            first += [a["id"] for a in json.load(f).get("alerts", [])]
    except (OSError, ValueError, KeyError):
        pass
    first += [int(k) for k in ppt_cache if str(k).isdigit()]
    chosen, seen = [], set()
    for pid in first:
        if pid in cards and pid not in seen:
            seen.add(pid)
            chosen.append(cards[pid])
    n = int(lc.get("cards_per_run", 120))
    rest = sorted((c for p, c in cards.items() if p not in seen), key=lambda c: -c["market"])
    slots = max(n - len(chosen), 0)
    if rest and slots:
        start = (int(now.timestamp() // 3600) * slots) % len(rest)
        chosen += (rest[start:] + rest[:start])[:slots]
    return chosen[:n]


def comparisons(card: dict, cls: dict, entry: dict | None, others: list[float], lc: dict) -> list[dict]:
    """Comparable prices for a listing: recent sales first, then current listings."""
    out = []
    if cls["kind"] == "graded":
        rows = ppt.graded_in_range(entry, {"grader": cls["grader"], "min_grade": cls["grade"],
                                           "max_grade": cls["grade"]})
        if rows and isinstance(rows[0]["median"], (int, float)):
            out.append({"label": f"{cls['label']} sold median ({rows[0]['count']} sales)",
                        "price": float(rows[0]["median"])})
    else:
        rows, _used = ppt.conditions_for(entry, card["printing"])
        row = next((r for r in rows if r["condition"] == cls["condition"]), None)
        if row and isinstance(row["price"], (int, float)):
            out.append({"label": f"TCGplayer {row['label']} price", "price": float(row["price"])})
        elif cls["condition"] == "Near Mint" and card.get("market"):
            out.append({"label": "TCGplayer market price (condition varies)", "price": card["market"]})
    if len(others) >= int(lc.get("min_comparable_listings", 3)):
        out.append({"label": f"eBay listings median ({len(others)})", "price": statistics.median(others)})
    return out


def _set_words_in_title(f: dict) -> int:
    """How many words of the card's set name (minus a 'SV2a:'-style code) the title contains."""
    name = re.sub(r"^[^:]*:\s*", "", f.get("set") or "").lower()
    words = [w for w in re.findall(r"[a-z0-9&']+", name) if len(w) > 2]
    t = f.get("title", "").lower()
    return sum(1 for w in words if w in t)


def dedupe(finds: list[dict]) -> list[dict]:
    """One row per eBay listing. A listing that matched several tracked cards (same
    name and number in different sets) stays with the card whose set name its
    title names most, then the one it is furthest under; biggest gap first."""
    best = {}
    for f in finds:
        key = f["item_id"]
        rank = (_set_words_in_title(f), f["gap_pct"])
        if key not in best or rank > best[key][0]:
            best[key] = (rank, f)
    out = [f for _, f in best.values()]
    out.sort(key=lambda f: (-f["gap_pct"], f["listed_hours_ago"]))
    return out


def run(cfg: dict, store, root: str, now: datetime | None = None) -> dict:
    lc = cfg.get("listing_scan") or {}
    now = now or datetime.now(timezone.utc)
    result = {"generated": now.strftime("%Y-%m-%d %H:%M UTC"), "generated_local": eastern(now),
              "finds": [], "scanned": 0,
              "calls": 0, "note": None,
              "window_hours": [lc.get("min_age_hours", 2), lc.get("max_age_hours", 12)]}
    if not lc.get("enabled"):
        return result
    cid = os.environ.get("EBAY_CLIENT_ID", "").strip()
    secret = os.environ.get("EBAY_CLIENT_SECRET", "").strip()
    if not (cid and secret):
        # nothing is written, so an hourly run without keys commits nothing
        result["note"] = "The listing scan needs EBAY_CLIENT_ID and EBAY_CLIENT_SECRET, which are not set."
        print(result["note"])
        return result

    ppt_cache = _latest_ppt(store)
    cards = pick_cards(cfg, store, root, now, ppt_cache)
    client = ebay.Client(lc.get("ebay_base_url", "https://api.ebay.com"), cid, secret,
                         cfg["user_agent"], pause=float(lc.get("pause_seconds", 0.2)))
    g = cfg.get("graded_filter") or {}
    want_conds = set(lc.get("conditions") or ["Near Mint", "Lightly Played"])
    min_age, max_age = float(lc.get("min_age_hours", 2)), float(lc.get("max_age_hours", 12))
    exclude = lc.get("exclude_words") or []

    pending = []   # (card, summary, others in the same search)
    try:
        for card in cards:
            q = " ".join(f"pokemon {clean_name(card['name'])} {card['number']}"
                         f"{' japanese' if card['lang'] == 'Japanese' else ''}".split())
            floor = round(card["market"] * float(lc.get("min_price_share", 0.2)), 2)
            items = client.search(q, lc.get("category_ids", ""),
                                  ["buyingOptions:{FIXED_PRICE}", f"price:[{floor}..]", "priceCurrency:USD"],
                                  int(lc.get("results_per_card", 50)))
            result["scanned"] += 1
            same = [s for s in items if matches_card(s.get("title") or "", card, exclude)]
            for s in same:
                born = _parse_time(s.get("itemOriginDate") or s.get("itemCreationDate"))
                age = (now - born).total_seconds() / 3600 if born else None
                if age is not None and min_age <= age <= max_age:
                    pending.append((card, s, same, age))
        ids = list(dict.fromkeys(s["itemId"] for _, s, _, _ in pending))   # each listing once
        full = client.get_items(ids) if ids else {}
    except ebay.EbayError as e:
        result["note"] = f"eBay scan stopped early: {e}"
        print(result["note"])
        full = {}
    result["calls"] = client.calls

    finds = []
    for card, s, same, age in pending:
        cls = classify(s, full.get(s["itemId"]))
        if not cls:
            continue
        if cls["kind"] == "graded":
            if g and not (cls["grader"] == str(g.get("grader", "psa")).lower()
                          and g.get("min_grade", 7) <= cls["grade"] <= g.get("max_grade", 10)):
                continue
        elif cls["condition"] not in want_conds:
            continue
        price = _money(s.get("price"))
        if price is None:
            continue
        ship = _money(((s.get("shippingOptions") or [{}])[0]).get("shippingCost")) or 0.0
        total = round(price + ship, 2)
        others = []
        for o in same:
            if o.get("itemId") == s.get("itemId") or not same_class(cls, classify(o)):
                continue
            op = _money(o.get("price"))
            if op is not None:
                others.append(op + (_money(((o.get("shippingOptions") or [{}])[0]).get("shippingCost")) or 0.0))
        entry = ppt_cache.get(str(card["pid"]))
        comps = comparisons(card, cls, entry, others, lc)
        best = None
        for c in comps:
            gap = c["price"] - total
            pct = gap / c["price"] * 100 if c["price"] else 0
            c["gap_pct"], c["gap_usd"] = round(pct, 1), round(gap, 2)
            if pct >= float(lc.get("min_gap_pct", 25)) and gap >= float(lc.get("min_gap_dollars", 20)):
                if best is None or pct > best["gap_pct"]:
                    best = c
        if not best:
            continue
        watch = s.get("watchCount") if s.get("watchCount") is not None else (full.get(s["itemId"]) or {}).get("watchCount")
        finds.append({
            "item_id": s["itemId"], "title": s.get("title", ""), "url": s.get("itemWebUrl", ""),
            "pid": card["pid"], "card": card["name"], "set": card["set"], "number": card["number"],
            "printing": card["printing"], "lang": card["lang"], "tcgplayer": card["url"],
            "kind": cls["kind"], "label": cls["label"], "price": price, "shipping": ship, "total": total,
            "listed_hours_ago": round(age, 1), "watchers": watch, "comps": comps,
            "gap_pct": best["gap_pct"], "gap_usd": best["gap_usd"], "vs": best["label"],
        })

    finds = dedupe(finds)[:int(lc.get("max_finds", 100))]
    state = store._load("listings/state.json", {})
    keep_after = now - timedelta(hours=max_age * 2)
    state = {k: v for k, v in state.items() if (_parse_time(v) or now) >= keep_after}
    for f in finds:
        f["new"] = f["item_id"] not in state
        state.setdefault(f["item_id"], now.isoformat())
    store._save("listings/state.json", state)
    finds.sort(key=lambda f: (-f["gap_pct"], f["listed_hours_ago"]))
    result["finds"] = finds
    write_outputs(root, result)
    print(f"Listing scan: {result['scanned']} cards, {result['calls']} eBay calls, "
          f"{len(finds)} listings {lc.get('min_gap_pct', 25)}%+ under comparable prices "
          f"({sum(f['new'] for f in finds)} new this hour).")
    return result


def eastern(t: datetime) -> str:
    """'Wed 11 Nov, 11:22 am ET' (US Eastern; UTC if the time-zone data is missing)."""
    try:
        from zoneinfo import ZoneInfo
        t = t.astimezone(ZoneInfo("America/New_York"))
        zone = "ET"
    except Exception:  # noqa: BLE001 - no tz database: say UTC rather than fail
        t = t.astimezone(timezone.utc)
        zone = "UTC"
    hour = t.strftime("%I").lstrip("0")
    return f"{t.strftime('%a')} {t.day} {t.strftime('%b')}, {hour}:{t.strftime('%M')} {t.strftime('%p').lower()} {zone}"


def _usd(x):
    return "–" if x is None else f"${x:,.2f}"


def write_outputs(root: str, result: dict) -> None:
    os.makedirs(os.path.join(root, "docs"), exist_ok=True)
    # the workbook is uploaded, not committed: always rewrite it, so its
    # "Last updated" line shows the latest check even after a quiet hour
    try:
        _workbook(os.path.join(root, "out", "listings.xlsx"), result, _daily_alerts(root))
    except ImportError:
        pass
    try:   # an empty hour after an empty hour changes nothing: keep the files, skip a commit
        with open(os.path.join(root, "docs", "listings.json"), encoding="utf-8") as f:
            before = json.load(f)
        if not result["finds"] and not before.get("finds") and before.get("note") == result.get("note"):
            return
    except (OSError, ValueError):
        pass
    with open(os.path.join(root, "docs", "listings.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
    lo, hi = result["window_hours"]
    L = [f"# New eBay listings under comparable prices: {result.get('generated_local') or result['generated']}", "",
         f"Listings that went up {lo:g}–{hi:g} hours ago and are still for sale, priced well under "
         f"recent sales or other listings of the same card in the same condition or grade. "
         f"{result['scanned']} cards scanned this hour.", ""]
    if result.get("note"):
        L += [f"> {result['note']}", ""]
    if result["finds"]:
        L += ["| Listing | Card | Condition | Price + ship | Under | Compared with | Watchers | Listed |",
              "|---|---|---|--:|--:|---|--:|--:|"]
        for x in result["finds"]:
            title = x["title"].replace("|", "/")[:70]
            new = " **new**" if x.get("new") else ""
            L.append(f"| [{title}]({x['url']}){new} | {x['card']}<br><sub>{x['set']} · {x['number']}</sub> | "
                     f"{x['label']} | {_usd(x['total'])} | {x['gap_pct']:.0f}% ({_usd(x['gap_usd'])}) | "
                     f"{x['vs']}: {_usd(next(c['price'] for c in x['comps'] if c['label'] == x['vs']))} | "
                     f"{'–' if x['watchers'] is None else x['watchers']} | {x['listed_hours_ago']:.0f} h ago |")
    else:
        L.append("Nothing this hour.")
    L += ["", "_eBay listings via the Browse API; sale prices via PokemonPriceTracker and TCGplayer. "
          "Check photos and seller before buying. Not financial advice._", ""]
    with open(os.path.join(root, "LISTINGS.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def _daily_alerts(root: str):
    """docs/latest.json from the daily run, or None."""
    try:
        with open(os.path.join(root, "docs", "latest.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


WEAK_COMPARISON = "condition varies"
SUSPECT_WORDS = re.compile(r"celebration|classic collection|anniversary|reprint|proxy|custom|orica", re.I)


def double_checks(finds: list[dict]) -> list[str]:
    """Plain-English reasons to look closely before trusting a find."""
    out = []
    for x in finds:
        why = []
        if WEAK_COMPARISON in x["vs"]:
            why.append("compared only with TCGplayer's general market price, which may not be this condition")
        m = re.search(r"eBay listings median \((\d+)\)", x["vs"])
        if m and int(m.group(1)) < 5:
            why.append(f"compared with only {m.group(1)} other listings")
        if x["gap_pct"] >= 60:
            why.append(f"{x['gap_pct']:.0f}% under is unusually large")
        if SUSPECT_WORDS.search(x["title"]):
            why.append("title mentions a reprint or custom")
        if why:
            out.append(f"{x['card']} ({x['set']} {x['number']}), {x['label']} at ${x['total']:,.2f}: "
                       + "; ".join(why) + ". Check the photos and set symbol.")
    return out


def _summary_sheet(wb, result: dict, daily) -> None:
    """A one-page overview placed first: new eBay deals, top deals, top daily alerts."""
    from openpyxl.styles import Alignment, Font, PatternFill
    BASE, BOLD = Font(name="Arial", size=10), Font(name="Arial", size=10, bold=True)
    HEAD = Font(name="Arial", size=10, bold=True, color="FFFFFF")
    FILL = PatternFill("solid", fgColor="1F3A5F")
    LINK = Font(name="Arial", size=10, color="0563C1", underline="single")
    MUTED = Font(name="Arial", size=10, color="666666")
    ws = wb.create_sheet("Summary", 0)
    for col, w in zip("ABCDEFGH", (34, 26, 14, 12, 12, 34, 12, 10)):
        ws.column_dimensions[col].width = w
    row = 1

    def line(text, font=BASE):
        nonlocal row
        c = ws.cell(row=row, column=1, value=text)
        c.font = font
        row += 1

    def header(cols):
        nonlocal row
        for j, h in enumerate(cols, start=1):
            c = ws.cell(row=row, column=j, value=h)
            c.font, c.fill = HEAD, FILL
        row += 1

    def put(values, formats=None):
        nonlocal row
        for j, v in enumerate(values, start=1):
            c = ws.cell(row=row, column=j, value=v)
            c.font = BASE
            if formats and formats.get(j):
                c.number_format = formats[j]
            if isinstance(v, str) and v.startswith("http"):
                c.hyperlink, c.value, c.font = v, "Open", LINK
        row += 1

    finds = result["finds"]
    new = [x for x in finds if x.get("new")]
    line("Pokémon tracker summary", Font(name="Arial", size=14, bold=True))
    line(f"Last updated: {result.get('generated_local') or result['generated']}", MUTED)
    row += 1
    line("eBay deals", Font(name="Arial", size=12, bold=True))
    line(f"{len(finds)} listings priced well under comparable prices, {len(new)} new since the previous "
         f"check. {result['scanned']} cards searched.")
    if result.get("note"):
        line(f"Problem with this scan: {result['note']}", Font(name="Arial", size=10, color="C00000"))
    top = sorted(finds, key=lambda x: (not x.get("new"), -x["gap_pct"]))[:5]
    if top:
        header(["Card", "Set · number", "Condition", "Total", "Under by", "Compared with", "Listed", "eBay"])
        for x in top:
            put([("NEW · " if x.get("new") else "") + x["card"], f"{x['set']} · {x['number']}", x["label"],
                 x["total"], x["gap_pct"] / 100.0, x["vs"], f"{x['listed_hours_ago']:.0f} h ago", x["url"]],
                {4: "$#,##0.00", 5: "0%"})
        line("All listings are on the New listings sheet.", MUTED)
    else:
        line("No deals right now.", MUTED)
    row += 1

    line("Daily price alerts", Font(name="Arial", size=12, bold=True))
    if daily and daily.get("meta"):
        m = daily["meta"]
        alerts = daily.get("alerts") or []
        main = [a for a in alerts if a.get("sev") in ("high", "medium")]
        line(f"Prices as of {m.get('date')}: {len(main)} high or medium alerts "
             f"({sum(1 for a in main if a.get('new'))} new), {m.get('watch', 0)} on the watch list.")
        if m.get("filter_note"):
            line(m["filter_note"], MUTED)
        high = [a for a in alerts if a.get("sev") == "high"][:5]
        if high:
            header(["Card", "Set · number", "Language", "Price", "7-day change", "Why", "New", "TCGplayer"])
            for a in high:
                c7 = a.get("c7")
                put([a.get("n", ""), f"{a.get('s', '')} · {a.get('no', '')}", a.get("lang", ""), a.get("p"),
                     None if c7 is None else c7 / 100.0, ", ".join(a.get("why") or []),
                     "Yes" if a.get("new") else "", a.get("url") or None],
                    {4: "$#,##0.00", 5: "+0.0%;-0.0%"})
        else:
            line("No high alerts today.", MUTED)
        line("Everything else is in tracker.xlsx (same download link).", MUTED)
    else:
        line("The daily alerts file isn't available yet.", MUTED)
    row += 1

    checks = double_checks(finds)
    line("Worth double-checking", Font(name="Arial", size=12, bold=True))
    for text in checks or ["Nothing stands out."]:
        c = ws.cell(row=row, column=1, value=text)
        c.font = BASE
        c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=8)
        ws.row_dimensions[row].height = 28
        row += 1
    row += 1
    line("Not financial advice.", MUTED)


def _workbook(path: str, result: dict, daily=None) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo
    cols = ["New", "Listing", "Card", "Set", "Number", "Condition / grade", "Price", "Shipping",
            "Total", "Under by", "Under by $", "Compared with", "Comparable price", "Watchers",
            "Listed (hours ago)", "eBay link", "TCGplayer", "Item ID"]
    wb = Workbook()
    ws = wb.active
    ws.title = "New listings"
    n_new = sum(1 for x in result["finds"] if x.get("new"))
    ws["A1"] = (f"Last updated: {result.get('generated_local') or result['generated']}  ·  "
                f"{len(result['finds'])} listings, {n_new} new since the previous check  ·  "
                f"{result['scanned']} cards searched")
    ws["A1"].font = Font(name="Arial", size=11, bold=True)
    ws.append([])
    ws.append(cols)                                   # header on row 3, data from row 4
    for c in ws[3]:
        c.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F3A5F")
    for x in result["finds"]:
        comp = next(c["price"] for c in x["comps"] if c["label"] == x["vs"])
        ws.append(["Yes" if x.get("new") else "", x["title"], x["card"], x["set"], x["number"],
                   x["label"], x["price"], x["shipping"], x["total"], x["gap_pct"] / 100.0,
                   x["gap_usd"], x["vs"], comp, x["watchers"], x["listed_hours_ago"], x["url"],
                   x["tcgplayer"], x["item_id"]])
    if not result["finds"]:
        ws.append(["", result.get("note") or "Nothing this hour."] + [None] * (len(cols) - 2))
    fmt = {"Price": "$#,##0.00", "Shipping": "$#,##0.00", "Total": "$#,##0.00",
           "Under by": "0%", "Under by $": "$#,##0.00", "Comparable price": "$#,##0.00",
           "Listed (hours ago)": "0.0"}
    widths = {"Listing": 60, "Card": 28, "Set": 26, "Condition / grade": 16, "Compared with": 36}
    for j, name in enumerate(cols, start=1):
        letter = get_column_letter(j)
        ws.column_dimensions[letter].width = widths.get(name, 12)
        for i in range(4, ws.max_row + 1):
            cell = ws.cell(row=i, column=j)
            cell.font = Font(name="Arial", size=10)
            if name in fmt:
                cell.number_format = fmt[name]
            if name in ("eBay link", "TCGplayer") and isinstance(cell.value, str) and cell.value.startswith("http"):
                cell.hyperlink = cell.value
                cell.font = Font(name="Arial", size=10, color="0563C1", underline="single")
    ws.freeze_panes = "C4"
    tab = Table(displayName="tblListings", ref=f"A3:{get_column_letter(len(cols))}{ws.max_row}")
    tab.tableStyleInfo = TableStyleInfo(name="TableStyleLight9", showRowStripes=True)
    ws.add_table(tab)
    _summary_sheet(wb, result, daily)
    wb.active = 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    wb.save(path)
