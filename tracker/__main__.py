"""Command line entry point:  python -m tracker run"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
from datetime import date, timedelta

from . import ppt, report, signals
from .store import Store
from .tcgcsv import Client


def load_config(root: str) -> dict:
    with open(os.path.join(root, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    if os.environ.get("TCGCSV_BASE_URL"):
        cfg["tcgcsv_base_url"] = os.environ["TCGCSV_BASE_URL"]
    return cfg


def _keep(row: dict, cfg: dict):
    m = row.get("marketPrice")
    if m is None or not (cfg["store_floor"] <= m <= cfg["store_ceiling"]):
        return None
    key = (int(row["productId"]), row.get("subTypeName") or "Normal")
    return key, (m, row.get("midPrice"), row.get("lowPrice"))


def _catalog_entry(p: dict, group: dict, cat: int) -> dict:
    ext = {e.get("name"): e.get("value") for e in p.get("extendedData") or []}
    return {
        "name": p.get("name") or p.get("cleanName") or "",
        "group": group.get("name", ""), "gid": group.get("groupId"), "cat": cat,
        "number": ext.get("Number") or "", "rarity": ext.get("Rarity") or "",
        "url": p.get("url") or f"https://www.tcgplayer.com/product/{p.get('productId')}",
        "is_card": bool(ext.get("Number")),
    }


def sync_today(cfg: dict, store: Store, client: Client) -> str | None:
    """Pull today's prices if tcgcsv has published a new build. Returns the day."""
    state = store.load_state()
    stamp = client.last_updated()
    day = stamp.date().isoformat()
    if store.has_snapshot(day):
        print(f"Snapshot for {day} already stored; nothing new to download.")
        return day

    catalog = store.load_catalog()
    cats = [int(c) for c in cfg["categories"]]
    rows: dict = {}
    wanted: dict = {}   # (cat, gid) -> (group, product ids not yet in the catalog)
    for cat in cats:
        try:
            groups = client.groups(cat)
        except Exception as e:
            print(f"WARNING: category {cat} skipped ({e})")
            continue
        got = 0
        for g in groups:
            gid = g["groupId"]
            try:
                prices = client.prices(cat, gid)
            except urllib.error.HTTPError:
                continue  # some groups have no price file
            for p in prices:
                kept = _keep(p, cfg)
                if not kept:
                    continue
                key, val = kept
                rows[key] = val
                got += 1
                if str(key[0]) not in catalog:
                    wanted.setdefault((cat, gid), (g, set()))[1].add(key[0])
        print(f"Category {cat} ({cfg['categories'][str(cat)]}): "
              f"{len(groups)} sets, {got} prices kept")
    if not rows:
        raise SystemExit("No prices downloaded; leaving stored data untouched.")

    for (cat, gid), (g, pids) in wanted.items():
        try:
            for p in client.products(cat, gid):
                if p.get("productId") in pids:
                    catalog[str(p["productId"])] = _catalog_entry(p, g, cat)
        except Exception as e:
            print(f"WARNING: could not load card details for set {gid} ({e})")
    store.save_catalog(catalog)
    store.write_snapshot(day, rows)
    state["last_updated"] = stamp.isoformat()
    store.save_state(state)
    print(f"Stored {len(rows)} prices for {day} ({client.requests_made} requests).")
    return day


def backfill(cfg: dict, store: Store, client: Client, today_iso: str) -> None:
    """One-time: seed history from tcgcsv's daily archives. Each archive is
    downloaded once and remembered in state.json."""
    state = store.load_state()
    if state.get("backfill_complete"):
        return
    cats = [int(c) for c in cfg["categories"]]
    done = set(state.get("archives_done", []))
    today = date.fromisoformat(today_iso)
    added = 0
    for back in range(1, int(cfg["backfill_days"]) + 1):
        d = (today - timedelta(days=back)).isoformat()
        if store.has_snapshot(d) or d in done:
            continue
        try:
            data = client.archive_prices(d, cats)
        except Exception as e:
            print(f"WARNING: archive {d} unreadable ({e}); will not retry it")
            data = None
        if data:
            rows = {}
            for cat in data:
                for gid, prices in data[cat].items():
                    for p in prices:
                        kept = _keep(p, cfg)
                        if kept:
                            rows[kept[0]] = kept[1]
            if rows:
                store.write_snapshot(d, rows)
                added += 1
        done.add(d)
        state["archives_done"] = sorted(done)
        store.save_state(state)
    state["backfill_complete"] = True
    store.save_state(state)
    print(f"History seeded: {added} past days added from archives.")


def analyse(cfg: dict, store: Store, root: str) -> dict:
    dates = store.dates()
    if not dates:
        raise SystemExit("No price data stored yet.")
    today_iso = dates[-1]
    series, used = signals.load_series(store, int(cfg["history_days"]))
    catalog = store.load_catalog()
    log = [a for a in store.read_alert_log() if a["date"] != today_iso]
    result = signals.compute(cfg, series, catalog, today_iso, store.read_volume(), log)

    ebay = {}
    key = os.environ.get("PPT_API_KEY", "").strip()
    if key:
        targets = ppt.pick_targets(ppt.read_watchlist(root), result["alerts"], log, today_iso, cfg)
        print(f"Volume lookup for {len(targets)} cards")
        ebay = ppt.enrich(cfg, store, targets, key, today_iso)
        result = signals.compute(cfg, series, catalog, today_iso, store.read_volume(), log)

    # Graded-sales (PSA 7-10) and condition filters: both come from the same
    # PokemonPriceTracker lookup, so a card must pass every filter that is on.
    notes, missing = [], []
    gcfg = cfg.get("graded_filter") or {}
    ccfg = cfg.get("condition_filter") or {}
    if key:
        result["alerts"] = ppt.attach_conditions(result["alerts"], ebay)
    if gcfg.get("enabled"):
        label = f"{gcfg['grader'].upper()} {gcfg['min_grade']:g}–{gcfg['max_grade']:g}"
        if key:
            before = len(result["alerts"])
            result["alerts"] = ppt.apply_graded_filter(result["alerts"], ebay, gcfg)
            notes.append(f"Only cards with {label} sales: {len(result['alerts'])} of "
                         f"{before} alerts kept ({len(ebay)} cards checked today).")
        else:
            missing.append(label)
    if ccfg.get("enabled"):
        names = " or ".join(ccfg.get("conditions") or [])
        if key:
            before = len(result["alerts"])
            result["alerts"] = ppt.apply_condition_filter(result["alerts"], ccfg)
            notes.append(f"Only cards sold in {names} in the last 30 days: "
                         f"{len(result['alerts'])} of {before} kept.")
        else:
            missing.append("condition")
    if missing:
        notes.append(f"The {' and '.join(missing)} filter{'s need' if len(missing) > 1 else ' needs'} "
                     f"PPT_API_KEY, which is not set: showing raw-card alerts unfiltered.")
    filter_note = " ".join(notes) or None
    if filter_note:
        print(filter_note)

    for a in result["alerts"]:
        m = catalog.get(str(a["pid"]), {})
        log.append({"date": today_iso, "productId": str(a["pid"]), "printing": a["printing"],
                    "name": m.get("name", ""), "set": m.get("group", ""),
                    "direction": a["direction"], "severity": a["severity"],
                    "reasons": "; ".join(a["reasons"] + a["tags"]),
                    "price": f"{a['price']:.2f}",
                    "chg_7d_pct": "" if a["c7"] is None else f"{a['c7']:.1f}"})
    store.write_alert_log(log)

    payload = report.build_payload(cfg, result, catalog, series, today_iso, log, len(used), ebay,
                                   filter_note)
    report.write_dashboard(root, payload)
    report.write_markdown(root, payload)
    try:
        from . import excel
        xl = excel.write_workbook(os.path.join(root, "out", "tracker.xlsx"), cfg, result,
                                  catalog, series, used, today_iso, log, payload["meta"],
                                  repo=os.environ.get("GITHUB_REPOSITORY"))
        print(f"Excel workbook written: {os.path.relpath(xl, root)}")
    except ImportError:
        print("openpyxl not installed; skipping the Excel workbook")
    m = payload["meta"]
    print(f"{today_iso}: {m['universe']} cards in band, {m['alerts']} alerts "
          f"({m['new_alerts']} new; {m['up']} up, {m['down']} down), {m['watch']} on watch.")
    return payload


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tracker")
    ap.add_argument("command", choices=["run", "report", "listings"],
                    help="run = download today's prices then analyse; report = analyse stored data only; "
                         "listings = scan new eBay listings for underpriced cards (hourly)")
    ap.add_argument("--root", default=os.getcwd())
    args = ap.parse_args(argv)
    cfg = load_config(args.root)
    store = Store(args.root)
    if args.command == "listings":
        from . import listings
        listings.run(cfg, store, args.root)
        return 0
    if args.command == "run":
        client = Client(cfg["tcgcsv_base_url"], cfg["user_agent"], cfg["request_pause_seconds"])
        day = sync_today(cfg, store, client)
        backfill(cfg, store, client, day)
        removed = store.prune(int(cfg["keep_daily_days"]))
        if removed:
            print(f"Thinned {removed} old daily snapshots to weekly.")
    analyse(cfg, store, args.root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
