"""The model: turn daily price snapshots into ranked movers and alerts.

For every card printing priced inside the band it works out
  * percent change over 1, 7 and 30 days
  * a z-score: the 7-day move divided by what is normal for THAT card
    (its own day-to-day volatility over the prior weeks), so a jumpy card
    needs a bigger move to count than a steady one
  * a percentile: where the 7-day move ranks against every other card today
  * two sanity checks: did listing prices move the same way ("confirmed"),
    and has the move held for more than one day ("sustained")
and raises an alert when the thresholds in config.json are crossed.
"""
from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from datetime import date

SIGMA_FLOOR = 0.005      # never treat a card as steadier than 0.5% a day
MIN_RETURNS = 12         # history needed before a z-score is trusted
LAG_SLACK_DAYS = 4       # how stale a "7 days ago" price may be


def load_series(store, days: int) -> tuple[dict, list[str]]:
    """{key: [(ordinal_day, market, mid), ...]} over the last `days` snapshots."""
    dates = store.dates()[-days:]
    series: dict = {}
    for d in dates:
        o = date.fromisoformat(d).toordinal()
        for key, (market, mid, _low) in store.read_snapshot(d).items():
            if market is None or market <= 0:
                continue
            series.setdefault(key, []).append((o, market, mid))
    return series, dates


def _at_lag(obs: list, today: int, lag: int):
    """Latest observation on or before today-lag, if not too stale."""
    target = today - lag
    i = bisect_right([o[0] for o in obs], target) - 1
    if i < 0:
        return None
    if target - obs[i][0] > LAG_SLACK_DAYS:
        return None
    return obs[i]


def _sigma(obs: list, upto: int):
    """Typical daily log-return of a card, using observations up to `upto`."""
    rets = []
    prev = None
    for o, m, _ in obs:
        if o > upto:
            break
        if prev is not None and o > prev[0]:
            rets.append(math.log(m / prev[1]) / math.sqrt(o - prev[0]))
        prev = (o, m)
    if len(rets) < MIN_RETURNS:
        return None
    sd = statistics.pstdev(rets)
    med = statistics.median(rets)
    mad = 1.4826 * statistics.median(abs(r - med) for r in rets)
    return max(0.5 * (sd + mad), SIGMA_FLOOR)


def _pct(a, b):
    return None if (a is None or b is None or b <= 0) else (a / b - 1.0) * 100.0


def volume_signal(vol_by_day: dict, today_iso: str, ratio_min: float, min_sales: float):
    """Compare the last 3 complete days of sales with the prior four weeks."""
    if not vol_by_day:
        return None
    today = date.fromisoformat(today_iso).toordinal()
    pts = {date.fromisoformat(d).toordinal(): v for d, v in vol_by_day.items()}
    # the newest day is partial, so the "recent" window ends yesterday
    recent_days = [today - 1, today - 2, today - 3]
    recent = sum(pts.get(d, 0.0) for d in recent_days)
    base_days = [d for d in pts if today - 31 <= d <= today - 4]
    out = {"recent_3d": recent, "baseline_3d": None, "ratio": None, "spike": False,
           "days_tracked": len(pts)}
    if len(base_days) >= 7:
        span = max(base_days) - min(base_days) + 1
        base = sum(pts[d] for d in base_days) / span * 3.0
        out["baseline_3d"] = base
        out["ratio"] = recent / max(base, 1.0)
        out["spike"] = recent >= min_sales and out["ratio"] >= ratio_min
    return out


def compute(cfg: dict, series: dict, catalog: dict, today_iso: str,
            volume: dict | None = None, alert_log: list | None = None) -> dict:
    T = cfg["thresholds"]
    lo, hi = cfg["band"]["min"], cfg["band"]["max"]
    today = date.fromisoformat(today_iso).toordinal()
    volume = volume or {}

    rows = []
    for key, obs in series.items():
        if obs[-1][0] != today:
            continue  # no price today
        pid, printing = key
        meta = catalog.get(str(pid))
        if cfg.get("cards_only", True) and not (meta and meta.get("is_card")):
            continue
        p0, mid0 = obs[-1][1], obs[-1][2]
        o1 = obs[-2] if len(obs) >= 2 and today - obs[-2][0] <= LAG_SLACK_DAYS else None
        o7 = _at_lag(obs, today, 7)
        o30 = _at_lag(obs, today, 30)
        p7 = o7[1] if o7 else None
        in_band_now = lo <= p0 <= hi
        was_in_band = p7 is not None and lo <= p7 <= hi
        if not (in_band_now or was_in_band):
            continue

        c1 = _pct(p0, o1[1]) if o1 else None
        c7 = _pct(p0, p7)
        c30 = _pct(p0, o30[1]) if o30 else None
        sigma = _sigma(obs, today - 7)
        z7 = (math.log(p0 / p7) / (sigma * math.sqrt(7))) if (sigma and p7) else None
        s1 = _sigma(obs, today - 1)
        z1 = (math.log(p0 / o1[1]) / (s1 * math.sqrt(max(today - o1[0], 1)))) if (s1 and o1) else None

        confirmed = sustained = False
        if c7 is not None and o7:
            mid7 = o7[2]
            cm = _pct(mid0, mid7) if (mid0 and mid7) else None
            confirmed = cm is not None and cm * c7 > 0 and abs(cm) >= 0.4 * abs(c7)
            if o1 and o1[0] > o7[0]:
                c_prev = _pct(o1[1], p7)
                sustained = c_prev is not None and c_prev * c7 > 0 and abs(c_prev) >= 0.5 * abs(c7)

        rows.append({
            "pid": pid, "printing": printing, "price": p0,
            "p1": o1[1] if o1 else None, "p7": p7, "p30": o30[1] if o30 else None,
            "c1": c1, "c7": c7, "c30": c30, "z7": z7, "z1": z1,
            "confirmed": confirmed, "sustained": sustained,
            "in_band": in_band_now, "days": len(obs),
            "vol": volume_signal(volume.get(pid), today_iso,
                                 T["volume_spike_ratio"], T["volume_min_sales"]),
        })

    # cross-sectional percentile of the 7-day move
    ranked = sorted(r["c7"] for r in rows if r["c7"] is not None)
    n = len(ranked)
    for r in rows:
        r["pctile"] = (bisect_right(ranked, r["c7"]) / n * 100.0) if (n and r["c7"] is not None) else None

    # recent alerts, to tell new signals from continuing ones
    recent = set()
    if alert_log:
        days_logged = sorted({a["date"] for a in alert_log if a["date"] < today_iso})[-3:]
        for a in alert_log:
            if a["date"] in days_logged:
                recent.add((a["productId"], a["printing"], a["direction"]))

    alerts = []
    tail = T["percentile_tail"]
    md = T["min_dollar_move"]
    for r in rows:
        reasons, tags = [], []
        c1, c7, c30, z7, z1, p0 = r["c1"], r["c7"], r["c30"], r["z7"], r["z1"], r["price"]

        if c7 is not None and abs(c7) >= T["move_7d_pct"] and abs(p0 - r["p7"]) >= md:
            if (z7 is not None and abs(z7) >= T["z_7d_min"]) or \
               (z7 is None and abs(c7) >= T["move_7d_pct_no_history"]):
                reasons.append("7-day move")
        if c1 is not None and abs(c1) >= T["move_1d_pct"] and abs(p0 - r["p1"]) >= md:
            if z1 is None or abs(z1) >= T["z_1d_min"]:
                reasons.append("1-day jump")
        if c30 is not None and abs(c30) >= T["move_30d_pct"] and abs(p0 - r["p30"]) >= md:
            reasons.append("30-day trend")
        if r["vol"] and r["vol"]["spike"]:
            reasons.append("volume spike")
        if not reasons:
            continue

        lead = next((c for c in (c7, c1, c30) if c is not None and abs(c) >= 1), None)
        direction = "up" if (lead or 0) >= 0 else "down"
        if r["pctile"] is not None and c7 is not None:
            if r["pctile"] >= 100 - tail and c7 > 0:
                tags.append(f"top {tail}% of movers")
            elif r["pctile"] <= tail and c7 < 0:
                tags.append(f"bottom {tail}% of movers")
        if r["p7"] is not None and r["p7"] < cfg["band"]["min"] <= p0:
            tags.append(f"broke above ${cfg['band']['min']:g}")
        if r["p7"] is not None and r["p7"] >= cfg["band"]["min"] > p0:
            tags.append(f"fell below ${cfg['band']['min']:g}")
        if r["confirmed"]:
            tags.append("listings agree")
        if r["sustained"]:
            tags.append("held 2+ days")

        suspect = r["p1"] is not None and (p0 / r["p1"] > 4 or p0 / r["p1"] < 0.25)
        if suspect:
            tags.append("extreme jump: verify before acting")

        if suspect:
            severity = "watch"
        elif "7-day move" in reasons and abs(c7) >= T["high_severity_7d_pct"] \
                and (r["confirmed"] or r["sustained"]):
            severity = "high"
        elif "7-day move" in reasons or "volume spike" in reasons:
            severity = "medium"
        else:
            severity = "watch"

        r2 = dict(r)
        r2.update(reasons=reasons, tags=tags, direction=direction, severity=severity,
                  is_new=(str(r["pid"]), r["printing"], direction) not in recent)
        alerts.append(r2)

    order = {"high": 0, "medium": 1, "watch": 2}
    alerts.sort(key=lambda a: (order[a["severity"]],
                               -abs(a["c7"] if a["c7"] is not None else (a["c1"] or 0))))
    return {"rows": rows, "alerts": alerts, "universe": len(rows)}
