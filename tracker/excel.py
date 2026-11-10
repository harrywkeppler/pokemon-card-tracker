"""Write the daily Excel workbook (out/tracker.xlsx).

The workflow uploads it to a fixed download link, so Excel can pull it with
Data > Get Data > From Web and refresh it in place. Values are written as plain
numbers rather than formulas: Excel's web import reads stored values, and a
formula written by this script has no stored value until Excel recalculates.
"""
from __future__ import annotations

import os
from datetime import date
from urllib.parse import quote_plus

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from .report import caution_tags

FONT = "Arial"
BASE = Font(name=FONT, size=10)
BOLD = Font(name=FONT, size=10, bold=True)
HEAD = Font(name=FONT, size=10, bold=True, color="FFFFFF")
HEAD_FILL = PatternFill("solid", fgColor="1F3A5F")
LINK = Font(name=FONT, size=10, color="0563C1", underline="single")
TITLE = Font(name=FONT, size=14, bold=True)
MUTED = Font(name=FONT, size=10, color="666666")

PCT = '[Color10]+0.0%;[Red]-0.0%;0.0%'
USD = '$#,##0.00'
TIMES = '0.0"×"'
INT = '0'

DOWNLOAD_URL = "https://github.com/{repo}/releases/download/latest/tracker.xlsx"


def _links(pid, meta):
    name = meta.get("name") or ""
    setname = (meta.get("group") or "").split(": ", 1)[-1]
    q = quote_plus(" ".join(f"pokemon {name} {meta.get('number', '')} {setname}".split()))
    qs = quote_plus(" ".join(f"pokemon {name.split('(')[0]}".split()))
    return {
        "TCGplayer": meta.get("url") or f"https://www.tcgplayer.com/product/{pid}",
        "eBay sold": f"https://www.ebay.com/sch/i.html?_nkw={q}&LH_Sold=1&LH_Complete=1",
        "eBay live": f"https://www.ebay.com/sch/i.html?_nkw={q}&_sop=15",
        "Facebook": f"https://www.facebook.com/marketplace/search/?query={qs}",
    }


def _frac(x):
    return None if x is None else round(x / 100.0, 4)


def _row(r, catalog, langs, kind):
    m = catalog.get(str(r["pid"]), {})
    tags = r.get("tags")
    if tags is None:
        tags = caution_tags(r)
    z = r.get("z7")
    out = {
        "Signal": (r.get("severity") or "").capitalize(),
        "New today": "Yes" if r.get("is_new") else "",
        "Card": m.get("name") or f"Product {r['pid']}",
        "Set": m.get("group", ""),
        "Number": m.get("number", ""),
        "Rarity": m.get("rarity", ""),
        "Printing": r["printing"],
        "Language": langs.get(str(m.get("cat")), ""),
        "Price": round(r["price"], 2),
        "1-day change": _frac(r["c1"]),
        "7-day change": _frac(r["c7"]),
        "30-day change": _frac(r["c30"]),
        "Price 1 day ago": r.get("p1"),
        "Price 7 days ago": r.get("p7"),
        "Price 30 days ago": r.get("p30"),
        "vs. normal": None if z is None else round(abs(z), 1),
        "Rank": None if r.get("pctile") is None else round(r["pctile"]),
        "Sale days (30d)": r.get("sales30"),
        "Why": ", ".join(r.get("reasons", [])),
        "Notes": ", ".join(tags),
        "Product ID": r["pid"],
    }
    out.update(_links(r["pid"], m))
    return out


FORMATS = {
    "Price": USD, "Price 1 day ago": USD, "Price 7 days ago": USD, "Price 30 days ago": USD,
    "1-day change": PCT, "7-day change": PCT, "30-day change": PCT,
    "vs. normal": TIMES, "Rank": INT, "Sale days (30d)": INT, "Product ID": INT,
}
WIDTHS = {
    "Signal": 9, "New today": 10, "Card": 34, "Set": 30, "Number": 10, "Rarity": 18,
    "Printing": 16, "Language": 10, "Why": 36, "Notes": 44, "Product ID": 11,
    "TCGplayer": 14, "eBay sold": 14, "eBay live": 14, "Facebook": 14,
}
LINK_COLS = ("TCGplayer", "eBay sold", "eBay live", "Facebook")

ALERT_COLS = ["Signal", "New today", "Card", "Set", "Number", "Printing", "Language", "Price",
              "1-day change", "7-day change", "30-day change", "Price 7 days ago", "vs. normal",
              "Rank", "Sale days (30d)", "Why", "Notes", *LINK_COLS, "Product ID"]
MOVER_COLS = ["Card", "Set", "Number", "Printing", "Language", "Price", "Price 7 days ago",
              "7-day change", "30-day change", "vs. normal", "Rank", "Sale days (30d)", "Notes",
              *LINK_COLS, "Product ID"]
ALL_COLS = ["Card", "Set", "Number", "Rarity", "Printing", "Language", "Price",
            "1-day change", "7-day change", "30-day change", "Price 1 day ago",
            "Price 7 days ago", "Price 30 days ago", "vs. normal", "Rank", "Sale days (30d)",
            "Notes", *LINK_COLS, "Product ID"]


def _table_sheet(wb, title, table_name, cols, rows, empty_note):
    ws = wb.create_sheet(title)
    ws.append(cols)
    for c in ws[1]:
        c.font, c.fill = HEAD, HEAD_FILL
        c.alignment = Alignment(vertical="center")
    if not rows:
        ws.append([empty_note] + [None] * (len(cols) - 1))
    for r in rows:
        ws.append([r.get(c) for c in cols])
    n = ws.max_row
    for j, col in enumerate(cols, start=1):
        letter = get_column_letter(j)
        ws.column_dimensions[letter].width = WIDTHS.get(col, 13)
        fmt = FORMATS.get(col)
        for i in range(2, n + 1):
            cell = ws.cell(row=i, column=j)
            cell.font = BASE
            if fmt:
                cell.number_format = fmt
            if col in LINK_COLS and rows and cell.value:
                cell.hyperlink = cell.value  # the address stays the value, so it survives a web refresh
                cell.font = LINK
    ws.freeze_panes = "B2" if cols[0] == "Card" else "D2"
    tab = Table(displayName=table_name, ref=f"A1:{get_column_letter(len(cols))}{n}")
    tab.tableStyleInfo = TableStyleInfo(name="TableStyleLight9", showRowStripes=True)
    ws.add_table(tab)
    return ws


def write_workbook(path, cfg, result, catalog, series, dates_used, today_iso, alert_log, meta,
                   repo=None):
    langs = cfg["categories"]
    rows = result["rows"]
    alerts = result["alerts"]
    main = [_row(a, catalog, langs, "alert") for a in alerts if a["severity"] != "watch"]
    watch = [_row(a, catalog, langs, "alert") for a in alerts if a["severity"] == "watch"]
    alert_by_key = {(a["pid"], a["printing"]): a for a in alerts}

    in_band = [r for r in rows if r["in_band"]]
    md = cfg["thresholds"]["min_dollar_move"]
    movers = [r for r in in_band if r["c7"] is not None and abs(r["price"] - r["p7"]) >= md]

    def mover(r):
        a = alert_by_key.get((r["pid"], r["printing"]))
        row = _row(a or r, catalog, langs, "mover")
        if a:
            row["Notes"] = ", ".join(["also in Alerts"] + [t for t in a["tags"] if t != "thinly traded"])
        return row

    gainers = [mover(r) for r in sorted(movers, key=lambda r: -r["c7"])[:50]]
    losers = [mover(r) for r in sorted(movers, key=lambda r: r["c7"])[:50]]
    every = [_row(alert_by_key.get((r["pid"], r["printing"])) or r, catalog, langs, "all")
             for r in sorted(in_band, key=lambda r: -r["price"])]

    wb = Workbook()
    about = wb.active
    about.title = "About"
    _table_sheet(wb, "Alerts", "tblAlerts", ALERT_COLS, main, "No alerts today")
    _table_sheet(wb, "Watch list", "tblWatch", ALERT_COLS, watch, "Nothing on the watch list today")
    _table_sheet(wb, "Top gainers", "tblGainers", MOVER_COLS, gainers, "Not enough history yet")
    _table_sheet(wb, "Top decliners", "tblDecliners", MOVER_COLS, losers, "Not enough history yet")
    _table_sheet(wb, "All cards", "tblCards", ALL_COLS, every, "No cards in band")

    # alert history: every alert ever logged, newest first
    hist_cols = ["Date", "Card", "Set", "Printing", "Signal", "Direction", "Price",
                 "7-day change", "Why and notes", "Product ID"]
    hist = []
    for h in reversed(alert_log):
        try:
            price = float(h["price"])
        except (TypeError, ValueError):
            price = None
        c7 = h.get("chg_7d_pct")
        hist.append({"Date": h["date"], "Card": h["name"], "Set": h["set"],
                     "Printing": h["printing"], "Signal": h["severity"].capitalize(),
                     "Direction": h["direction"].capitalize(), "Price": price,
                     "7-day change": _frac(float(c7)) if c7 not in ("", None) else None,
                     "Why and notes": h["reasons"],
                     "Product ID": int(h["productId"]) if h["productId"].isdigit() else None})
    WIDTHS.update({"Date": 11, "Direction": 10, "Why and notes": 70})
    _table_sheet(wb, "Alert history", "tblHistory", hist_cols, hist, "No alerts logged yet")

    # price history: one row per card in the band, one column per stored day
    days = dates_used[-60:]
    ords = [date.fromisoformat(d).toordinal() for d in days]
    ph_cols = ["Card", "Set", "Printing", "Product ID"] + days
    ph_rows = []
    for r in sorted(in_band, key=lambda r: -r["price"]):
        m = catalog.get(str(r["pid"]), {})
        byday = {o[0]: o[1] for o in series.get((r["pid"], r["printing"]), [])}
        row = {"Card": m.get("name") or f"Product {r['pid']}", "Set": m.get("group", ""),
               "Printing": r["printing"], "Product ID": r["pid"]}
        for d, o in zip(days, ords):
            row[d] = byday.get(o)
        ph_rows.append(row)
    for d in days:
        FORMATS[d] = USD
        WIDTHS[d] = 11
    _table_sheet(wb, "Price history", "tblPrices", ph_cols, ph_rows, "No price history yet")

    _about(about, cfg, meta, repo, len(main), len(watch))
    wb.move_sheet("About", offset=len(wb.sheetnames))  # About last; Alerts opens first
    wb.active = 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    wb.save(path)
    return path


def _about(ws, cfg, meta, repo, n_alerts, n_watch):
    T = cfg["thresholds"]
    band = cfg["band"]
    url = DOWNLOAD_URL.format(repo=repo) if repo else "(the tracker's download link)"
    lines = [
        ("Pokémon card tracker", TITLE),
        (f"Prices as of {meta['date']} · generated {meta['generated']}", MUTED),
        ("", None),
        (f"Cards tracked: {meta['universe']:,} printings priced ${band['min']:,}–${band['max']:,}", BASE),
        (f"Alerts today: {n_alerts} (high and medium) · watch list: {n_watch}", BASE),
        (f"Days of price history: {meta['days_of_history']}", BASE),
        ("", None),
        ("Keep this workbook up to date automatically", BOLD),
        ("A new copy of this file is published every day at this link:", BASE),
        (url, LINK),
        ("To have Excel refresh it for you (Microsoft 365 Excel, Windows or Mac):", BASE),
        ("  1. In a new, blank workbook: Data > Get Data > From Other Sources > From Web.", BASE),
        ("  2. Paste the link above and press OK.", BASE),
        ("  3. In the Navigator, tick 'Select multiple items', choose the tables you want "
         "(tblAlerts, tblCards, ...) and press Load.", BASE),
        ("  4. To update: Data > Refresh All. To make it automatic: Data > Queries & Connections, "
         "right-click a query > Properties > tick 'Refresh data when opening the file'.", BASE),
        ("  The link columns (TCGplayer, eBay sold, eBay live, Facebook) hold web addresses. They are "
         "clickable in the downloaded file; in a refreshed copy, paste one into your browser.", MUTED),
        ("", None),
        ("Sheets", BOLD),
        ("Alerts: high and medium signals today. Watch list: weaker signals (30-day trend, "
         "1-day jump, or more frequent sales on their own).", BASE),
        ("Top gainers / Top decliners: the 50 biggest 7-day moves, alert or not, with caution notes.", BASE),
        ("All cards: every card in the band with 1, 7 and 30-day changes. Alert history: every alert so far.", BASE),
        ("Price history: daily price of every card in the band for the last 60 stored days.", BASE),
        ("", None),
        ("Columns", BOLD),
        ("Price: TCGplayer market price for the raw (ungraded) card, built from recent completed sales.", BASE),
        ("vs. normal: the 7-day move divided by the card's usual 7-day swing. 3.0× = three times "
         "bigger than usual for that card.", BASE),
        ("Rank: percentile of the 7-day move among all tracked cards today (100 = biggest gain, 0 = biggest drop).", BASE),
        ("Sale days (30d): days in the last 30 on which the price changed, a rough count of days with "
         "sales. 2 or fewer = thinly traded.", BASE),
        ("", None),
        ("How a card is flagged", BOLD),
        ("A price move only counts when the lowest or median listing moved the same way, or two or more "
         "separate sales did. One sale moving the price 3× or more with flat listings is ignored.", BASE),
        (f"7-day move: ±{T['move_7d_pct']}% and ±${T['min_dollar_move']}, confirmed, at least "
         f"{T['z_7d_min']}× the card's normal swing. High = {T['high_severity_7d_pct']}%+ backed by "
         "listings and repeat sales, or with a jump in sales activity.", BASE),
        (f"1-day jump: ±{T['move_1d_pct']}% with listings agreeing. 30-day trend: ±{T['move_30d_pct']}% "
         f"from 2+ sales. Selling more often: price changed on {T['activity_min_sale_days']}+ of the "
         f"last 7 days at {T['activity_spike_ratio']}× the usual pace.", BASE),
        ("", None),
        ("Values here are calculated by the tracker from its stored daily prices, so they are numbers, "
         "not formulas. Source: TCGplayer prices via tcgcsv.com. Not financial advice.", MUTED),
    ]
    ws.column_dimensions["A"].width = 120
    for i, (text, font) in enumerate(lines, start=1):
        c = ws.cell(row=i, column=1, value=text or None)
        c.font = font or BASE
        c.alignment = Alignment(wrap_text=True, vertical="top")
        if font is LINK and text.startswith("http"):
            c.hyperlink = text
