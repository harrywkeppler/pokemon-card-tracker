"""Write the dashboard (docs/index.html) and the plain summary (REPORT.md)."""
from __future__ import annotations

import json
import os
import statistics
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SPARK_DAYS = 60


def _r(x, nd=1):
    return None if x is None else round(x, nd)


def _spark(obs: list, today: int) -> list:
    return [[o - today, round(m, 2)] for o, m, _ in obs if today - o <= SPARK_DAYS]


def _detail(r: dict, catalog: dict, series: dict, today: int, cats: dict, ebay: dict) -> dict:
    m = catalog.get(str(r["pid"]), {})
    v = r.get("vol") or None
    return {
        "id": r["pid"], "pr": r["printing"],
        "n": m.get("name") or f"Product {r['pid']}", "s": m.get("group", ""),
        "no": m.get("number", ""), "ra": m.get("rarity", ""),
        "lang": cats.get(str(m.get("cat")), ""), "url": m.get("url", ""),
        "p": round(r["price"], 2), "c1": _r(r["c1"]), "c7": _r(r["c7"]), "c30": _r(r["c30"]),
        "z": _r(r["z7"]), "pc": _r(r["pctile"]),
        "sp": _spark(series.get((r["pid"], r["printing"]), []), today),
        "sev": r.get("severity"), "dir": r.get("direction"),
        "why": r.get("reasons", []), "tags": r.get("tags", []), "new": r.get("is_new", False),
        "vol": None if not v else {"r": _r(v["recent_3d"], 0), "b": _r(v["baseline_3d"], 1),
                                   "x": _r(v["ratio"], 1), "spike": v["spike"]},
        "ebay": ebay.get(r["pid"]) or ebay.get(str(r["pid"])),
    }


def build_payload(cfg, result, catalog, series, today_iso, alert_log, n_dates, ebay=None) -> dict:
    today = date.fromisoformat(today_iso).toordinal()
    cats = cfg["categories"]
    ebay = ebay or {}
    rows = result["rows"]
    in_band = [r for r in rows if r["in_band"]]
    with7 = [r for r in in_band if r["c7"] is not None]
    md = cfg["thresholds"]["min_dollar_move"]
    movers = [r for r in with7 if abs(r["price"] - r["p7"]) >= md]
    gain = sorted(movers, key=lambda r: -r["c7"])[:40]
    lose = sorted(movers, key=lambda r: r["c7"])[:40]
    alerts = result["alerts"]

    universe = []
    for r in sorted(in_band, key=lambda r: -r["price"]):
        m = catalog.get(str(r["pid"]), {})
        universe.append([r["pid"], m.get("name") or f"Product {r['pid']}", m.get("group", ""),
                         m.get("number", ""), r["printing"], cats.get(str(m.get("cat")), ""),
                         round(r["price"], 2), _r(r["c1"]), _r(r["c7"]), _r(r["c30"]),
                         _r(r["z7"]), _r(r["pctile"])])

    recent_days = sorted({a["date"] for a in alert_log})[-30:]
    history = [a for a in alert_log if a["date"] in recent_days][-600:]
    history.reverse()

    return {
        "meta": {
            "date": today_iso,
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "band": cfg["band"], "thresholds": cfg["thresholds"],
            "universe": len(in_band), "days_of_history": n_dates,
            "alerts": len(alerts), "new_alerts": sum(1 for a in alerts if a["is_new"]),
            "up": sum(1 for a in alerts if a["direction"] == "up"),
            "down": sum(1 for a in alerts if a["direction"] == "down"),
            "median_c7": _r(statistics.median(r["c7"] for r in with7), 2) if with7 else None,
            "volume_enabled": any(r.get("vol") for r in rows),
            "languages": sorted(set(cats.values())),
        },
        "alerts": [_detail(r, catalog, series, today, cats, ebay) for r in alerts],
        "gainers": [_detail(r, catalog, series, today, cats, ebay) for r in gain],
        "losers": [_detail(r, catalog, series, today, cats, ebay) for r in lose],
        "universe": universe,
        "history": history,
    }


def write_dashboard(root: str, payload: dict) -> str:
    with open(os.path.join(HERE, "dashboard.html"), encoding="utf-8") as f:
        tpl = f.read()
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    out_dir = os.path.join(root, "docs")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "index.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(tpl.replace("/*__DATA__*/null", blob))
    with open(os.path.join(out_dir, "latest.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": payload["meta"], "alerts": payload["alerts"]}, f,
                  ensure_ascii=False, separators=(",", ":"))
    return out


def _fmt_pct(x):
    return "–" if x is None else f"{x:+.1f}%"


def write_markdown(root: str, payload: dict) -> str:
    m = payload["meta"]
    L = [f"# Pokémon card tracker: {m['date']}", "",
         f"{m['universe']:,} card printings priced ${m['band']['min']:,}–${m['band']['max']:,} "
         f"(TCGplayer market price) · {m['days_of_history']} days of history · "
         f"**{m['alerts']} alerts** ({m['new_alerts']} new, {m['up']} up, {m['down']} down)", ""]
    if m["days_of_history"] < 8:
        L += ["> Still building history: 7-day signals start once 8 days of prices are stored.", ""]

    def table(title, items, limit):
        if not items:
            return
        L.extend([f"## {title}", "",
                  "| Card | Set | Price | 1d | 7d | 30d | Why |", "|---|---|--:|--:|--:|--:|---|"])
        for a in items[:limit]:
            name = f"[{a['n']}]({a['url']})" if a["url"] else a["n"]
            extra = " · ".join(filter(None, [a["no"], a["pr"], a["lang"]]))
            why = ", ".join(a["why"] + a["tags"])
            if a.get("sev"):
                why = f"**{a['sev']}**{' (new)' if a['new'] else ''}: {why}"
            L.append(f"| {name}<br><sub>{extra}</sub> | {a['s']} | ${a['p']:,.2f} | "
                     f"{_fmt_pct(a['c1'])} | {_fmt_pct(a['c7'])} | {_fmt_pct(a['c30'])} | {why} |")
        L.append("")

    table("Alerts", payload["alerts"], 60)
    table("Biggest 7-day gainers", payload["gainers"], 15)
    table("Biggest 7-day decliners", payload["losers"], 15)
    L += ["", f"_Generated {m['generated']}. Prices: TCGplayer via tcgcsv.com._", ""]
    out = os.path.join(root, "REPORT.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    return out
