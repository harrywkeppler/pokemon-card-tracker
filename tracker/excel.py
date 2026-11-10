"""Write the daily Excel workbook (out/tracker.xlsx).

The workflow uploads it to a fixed download link, so Excel can pull it with
Data > Get Data > From Web and refresh it in place. The data tables hold plain
numbers rather than formulas: Excel's web import reads stored values, and a
formula written by this script has no stored value until Excel recalculates.

The adjustable part lives apart from the tables: a Settings sheet (defaults from
config.json "excel_view") and a My view sheet whose formulas list the cards that
match, recalculated by Excel when the file opens and whenever a setting changes.
Its helper formulas sit on a hidden sheet, so the tables stay plain values.
"""
from __future__ import annotations

import os
from datetime import date
from urllib.parse import quote_plus

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.workbook.properties import CalcProperties
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.table import Table, TableStyleInfo

from .report import caution_tags, condition_text, graded_text

FONT = "Arial"
BASE = Font(name=FONT, size=10)
BOLD = Font(name=FONT, size=10, bold=True)
HEAD = Font(name=FONT, size=10, bold=True, color="FFFFFF")
HEAD_FILL = PatternFill("solid", fgColor="1F3A5F")
LINK = Font(name=FONT, size=10, color="0563C1", underline="single")
TITLE = Font(name=FONT, size=14, bold=True)
MUTED = Font(name=FONT, size=10, color="666666")

PCT = '[Color10]+0.0%;[Red]-0.0%;0.0%'
PCT0 = '0%'
USD = '$#,##0.00'
TIMES = '0.0"×"'
INT = '0'

INPUT_FILL = PatternFill("solid", fgColor="FFF2CC")   # the cells a person changes
VIEW_ROWS = 250                                        # rows the My view sheet can list

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


def _num(x):
    return x if isinstance(x, (int, float)) else None


def _row(r, catalog, langs, kind, want_conds=("Near Mint", "Lightly Played")):
    m = catalog.get(str(r["pid"]), {})
    graded = r.get("graded") or []
    conds = r.get("conditions") or []
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
        # from the PokemonPriceTracker lookup; blank for cards not checked that day
        "Graded sales": graded_text([[g["label"], _num(g["median"]), g["count"]] for g in graded]),
        "Graded sales count": sum(g["count"] for g in graded) if graded else None,
        "Conditions (30d sales)": condition_text([[c["label"], _num(c["price"]), int(c["sales_30d"])]
                                                  for c in conds]),
        "Condition sales (30d)": int(sum(c["sales_30d"] for c in conds
                                         if c["condition"] in want_conds)) if conds else None,
        "Product ID": r["pid"],
    }
    out.update(_links(r["pid"], m))
    return out


FORMATS = {
    "Price": USD, "Price 1 day ago": USD, "Price 7 days ago": USD, "Price 30 days ago": USD,
    "1-day change": PCT, "7-day change": PCT, "30-day change": PCT,
    "vs. normal": TIMES, "Rank": INT, "Sale days (30d)": INT, "Product ID": INT,
    "Graded sales count": INT, "Condition sales (30d)": INT,
}
WIDTHS = {
    "Signal": 9, "New today": 10, "Card": 34, "Set": 30, "Number": 10, "Rarity": 18,
    "Printing": 16, "Language": 10, "Why": 36, "Notes": 44, "Product ID": 11,
    "TCGplayer": 14, "eBay sold": 14, "eBay live": 14, "Facebook": 14,
    "Graded sales": 30, "Conditions (30d sales)": 34, "Graded sales count": 12,
    "Condition sales (30d)": 13,
}
LINK_COLS = ("TCGplayer", "eBay sold", "eBay live", "Facebook")

ALERT_COLS = ["Signal", "New today", "Card", "Set", "Number", "Printing", "Language", "Price",
              "1-day change", "7-day change", "30-day change", "Price 7 days ago", "vs. normal",
              "Rank", "Sale days (30d)", "Why", "Notes", "Graded sales", "Conditions (30d sales)",
              *LINK_COLS, "Product ID"]
MOVER_COLS = ["Card", "Set", "Number", "Printing", "Language", "Price", "Price 7 days ago",
              "7-day change", "30-day change", "vs. normal", "Rank", "Sale days (30d)", "Notes",
              "Graded sales", "Conditions (30d sales)", *LINK_COLS, "Product ID"]
ALL_COLS = ["Card", "Set", "Number", "Rarity", "Printing", "Language", "Signal", "Price",
            "1-day change", "7-day change", "30-day change", "Price 1 day ago",
            "Price 7 days ago", "Price 30 days ago", "vs. normal", "Rank", "Sale days (30d)",
            "Notes", "Graded sales", "Graded sales count", "Conditions (30d sales)",
            "Condition sales (30d)", *LINK_COLS, "Product ID"]


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
    want = tuple((cfg.get("condition_filter") or {}).get("conditions") or ("Near Mint", "Lightly Played"))
    rows = result["rows"]
    alerts = result["alerts"]
    main = [_row(a, catalog, langs, "alert", want) for a in alerts if a["severity"] != "watch"]
    watch = [_row(a, catalog, langs, "alert", want) for a in alerts if a["severity"] == "watch"]
    alert_by_key = {(a["pid"], a["printing"]): a for a in alerts}

    in_band = [r for r in rows if r["in_band"]]
    md = cfg["thresholds"]["min_dollar_move"]
    movers = [r for r in in_band if r["c7"] is not None and abs(r["price"] - r["p7"]) >= md]

    def mover(r):
        a = alert_by_key.get((r["pid"], r["printing"]))
        row = _row(a or r, catalog, langs, "mover", want)
        if a:
            row["Notes"] = ", ".join(["also in Alerts"] + [t for t in a["tags"] if t != "thinly traded"])
        return row

    gainers = [mover(r) for r in sorted(movers, key=lambda r: -r["c7"])[:50]]
    losers = [mover(r) for r in sorted(movers, key=lambda r: r["c7"])[:50]]
    every = [_row(alert_by_key.get((r["pid"], r["printing"])) or r, catalog, langs, "all", want)
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
    wb.move_sheet("About", offset=len(wb.sheetnames))  # About last
    _view_sheets(wb, cfg, meta, len(every))            # My view and Settings open first
    wb.active = 0
    wb.calculation = CalcProperties(fullCalcOnLoad=True)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    wb.save(path)
    return path


SIGNALS = '{"Watch","Medium","High"}'   # ranked: a minimum of Watch means any signal


def _settings(cfg, meta):
    """(defined name, label, default, number format or dropdown options, help) per setting."""
    v = {"min_price": cfg["band"]["min"], "max_price": cfg["band"]["max"], "min_move_pct": 15,
         "direction": "Any", "min_signal": "Any", "language": "Any", "min_sale_days": 0,
         "require_graded": "No", "require_condition": "No", "sort_by": "Biggest move",
         **(cfg.get("excel_view") or {})}
    g = cfg.get("graded_filter") or {}
    grades = f"{g.get('grader', 'psa').upper()} {g.get('min_grade', 7):g}–{g.get('max_grade', 10):g}"
    conds = (cfg.get("condition_filter") or {}).get("conditions") or ["Near Mint", "Lightly Played"]
    short = "/".join("".join(w[0] for w in c.split()) for c in conds)        # NM/LP
    langs = ["Any", *(meta.get("languages") or sorted(set(cfg["categories"].values())))]
    return [
        ("setMinPrice", "Minimum price", v["min_price"], USD, "TCGplayer market price, raw card"),
        ("setMaxPrice", "Maximum price", v["max_price"], USD, ""),
        ("setMinMove", "Minimum 7-day move, up or down", v["min_move_pct"] / 100.0, PCT0,
         "0% lists every card in the price range"),
        ("setDirection", "Direction", v["direction"], ["Any", "Up", "Down"], ""),
        ("setMinSignal", "Minimum signal", v["min_signal"], ["Any", "Watch", "Medium", "High"],
         "Any = alert or not; Watch = any alert; High = strongest only"),
        ("setLanguage", "Language", v["language"], langs, ""),
        ("setMinSaleDays", "Minimum sale days in the last 30", v["min_sale_days"], INT,
         "days the price changed: a rough count of days with sales"),
        ("setNeedGraded", f"Require {grades} sales", v["require_graded"], ["No", "Yes"],
         "only cards looked up that day have this data (needs the API key)"),
        ("setNeedCondition", f"Require {short} sales in 30 days", v["require_condition"], ["No", "Yes"],
         "same: only cards looked up that day"),
        ("setSortBy", "Sort by", v["sort_by"], ["Biggest move", "Highest price", "Lowest price"], ""),
    ]


VIEW_COLS = [  # (header on My view, column on All cards, number format, width)
    ("Card", "Card", None, 34), ("Set", "Set", None, 28), ("Number", "Number", None, 10),
    ("Printing", "Printing", None, 14), ("Language", "Language", None, 10),
    ("Signal", "Signal", None, 9), ("Price", "Price", USD, 11),
    ("7-day change", "7-day change", PCT, 11), ("30-day change", "30-day change", PCT, 11),
    ("Sale days (30d)", "Sale days (30d)", INT, 10), ("Graded sales", "Graded sales", None, 30),
    ("Conditions (30d sales)", "Conditions (30d sales)", None, 34), ("Notes", "Notes", None, 40),
    ("TCGplayer", "TCGplayer", "link", 11), ("eBay sold", "eBay sold", "link", 11),
]


def _view_sheets(wb, cfg, meta, n_cards):
    """Settings (the inputs) and My view (the cards that match them), placed first.

    A hidden _view sheet holds one row per row of All cards: whether it matches
    and a sort key that is unique (row number added as a tiny fraction). My view
    row k then shows the card with the k-th largest key, using INDEX/MATCH/LARGE
    only, so it works in every Excel since 2010 as well as Excel for the web."""
    col = {name: get_column_letter(i) for i, name in enumerate(ALL_COLS, start=1)}
    last = n_cards + 1                      # All cards data rows are 2..last

    st = wb.create_sheet("Settings", 0)
    st.column_dimensions["A"].width = 38
    st.column_dimensions["B"].width = 16
    st.column_dimensions["C"].width = 60
    st["A1"] = "Settings for My view"
    st["A1"].font = TITLE
    st["A2"] = "Change the yellow cells; My view updates at once. Defaults come from config.json (excel_view)."
    st["A2"].font = MUTED
    for i, (name, label, default, kind, hint) in enumerate(_settings(cfg, meta), start=4):
        st.cell(row=i, column=1, value=label).font = BOLD
        cell = st.cell(row=i, column=2, value=default)
        cell.font, cell.fill = BASE, INPUT_FILL
        if isinstance(kind, list):
            dv = DataValidation(type="list", formula1='"' + ",".join(kind) + '"', allow_blank=False,
                                showErrorMessage=True, error="Pick one of the listed values")
            st.add_data_validation(dv)
            dv.add(cell)
        else:
            cell.number_format = kind
        st.cell(row=i, column=3, value=hint or None).font = MUTED
        wb.defined_names[name] = DefinedName(name, attr_text=f"Settings!$B${i}")

    hv = wb.create_sheet("_view")
    hv.sheet_state = "hidden"
    hv["A1"], hv["B1"] = "In view", "Order"
    ac = "'All cards'!"
    for i in range(2, last + 1):
        ref = lambda name: f"{ac}{col[name]}{i}"   # noqa: E731
        p, c7 = ref("Price"), f"N({ref('7-day change')})"
        hv[f"A{i}"] = (
            f"=AND({p}>=setMinPrice,{p}<=setMaxPrice,ABS({c7})>=setMinMove,"
            f'OR(setDirection="Any",AND(setDirection="Up",{c7}>0),AND(setDirection="Down",{c7}<0)),'
            f"IFERROR(MATCH({ref('Signal')},{SIGNALS},0),0)>=IFERROR(MATCH(setMinSignal,{SIGNALS},0),0),"
            f'OR(setLanguage="Any",{ref("Language")}=setLanguage),'
            f"N({ref('Sale days (30d)')})>=setMinSaleDays,"
            f'OR(setNeedGraded="No",N({ref("Graded sales count")})>0),'
            f'OR(setNeedCondition="No",N({ref("Condition sales (30d)")})>0))')
        hv[f"B{i}"] = (f'=IF(A{i},IF(setSortBy="Highest price",{p},IF(setSortBy="Lowest price",'
                       f"1000000-{p},ABS({c7})))+ROW()/1000000000,\"\")")

    vw = wb.create_sheet("My view", 0)
    vw["B1"] = "My view"
    vw["B1"].font = TITLE
    flags, order = f"_view!$A$2:$A${last}", f"_view!$B$2:$B${last}"
    vw["B2"] = (f'=COUNTIF({flags},TRUE)&" cards match the Settings sheet"&IF(COUNTIF({flags},TRUE)>'
                f'{VIEW_ROWS},", showing the first {VIEW_ROWS}","")&". Prices as of {meta["date"]}."')
    vw["B2"].font = MUTED
    head = ["Row", "#"] + [h for h, *_ in VIEW_COLS]
    for j, h in enumerate(head, start=1):
        c = vw.cell(row=4, column=j, value=h)
        c.font, c.fill = HEAD, HEAD_FILL
    vw.column_dimensions["A"].hidden = True       # the matching row's number on All cards
    vw.column_dimensions["B"].width = 5
    for k in range(1, VIEW_ROWS + 1):
        r = 4 + k
        vw.cell(row=r, column=1, value=f'=IFERROR(MATCH(LARGE({order},{k}),{order},0),"")')
        vw.cell(row=r, column=2, value=f'=IF($A{r}="","",{k})').font = MUTED
        for j, (_, src, fmt, _w) in enumerate(VIEW_COLS, start=3):
            rng = f"{ac}${col[src]}$2:${col[src]}${last}"
            pick = f"INDEX({rng},$A{r})"
            if fmt == "link":
                f = f'=IF($A{r}="","",HYPERLINK({pick},"Open"))'
            else:
                f = f'=IF($A{r}="","",IF({pick}="","",{pick}))'
            c = vw.cell(row=r, column=j, value=f)
            c.font = LINK if fmt == "link" else BASE
            if fmt and fmt != "link":
                c.number_format = fmt
    for j, (_, _s, _f, width) in enumerate(VIEW_COLS, start=3):
        vw.column_dimensions[get_column_letter(j)].width = width
    vw.freeze_panes = "D5"
    wb.move_sheet("Settings", offset=-(wb.sheetnames.index("Settings") - 1))   # My view, Settings, ...


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
        ("Adjust what you see", BOLD),
        ("My view lists the cards that match the Settings sheet: price range, size and direction of "
         "the 7-day move, signal, language, sale days, and whether PSA 7–10 or NM/LP sales are required. "
         "Change a yellow cell on Settings and My view re-sorts at once.", BASE),
        ("Each day's file opens with the defaults in config.json under \"excel_view\" (min_price, "
         "max_price, min_move_pct, direction, min_signal, language, min_sale_days, require_graded, "
         "require_condition, sort_by). Change them there to keep your choices from day to day.", BASE),
        ("My view and Settings use formulas, which Excel works out when it opens this file. A copy "
         "pulled in with Get Data > From Web brings the data tables only, without them.", MUTED),
        ("", None),
        ("Sheets", BOLD),
        ("Alerts: high and medium signals today. Watch list: weaker signals (30-day trend, "
         "1-day jump, or more frequent sales on their own).", BASE),
        ("Top gainers / Top decliners: the 50 biggest 7-day moves, alert or not, with caution notes.", BASE),
        ("All cards: every card in the band with 1, 7 and 30-day changes. Alert history: every alert so far.", BASE),
        ("Price history: daily price of every card in the band for the last 60 stored days.", BASE),
        ("Graded sales: PSA 7–10 median sale price per grade, with the number of sales. Conditions: "
         "TCGplayer price per condition with copies sold in 30 days (NM, LP, MP, HP, DMG). Both come "
         "from the PokemonPriceTracker lookup and are blank for cards it did not check that day.", BASE),
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
