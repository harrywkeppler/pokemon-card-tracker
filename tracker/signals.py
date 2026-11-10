"""The model: turn daily price snapshots into ranked movers and alerts.

TCGplayer's market price only changes when a card sells, so for most cards in
the $100-$5,000 band the daily price is a step line: flat for days, then a
jump on the next sale. A single sale can be an outlier (a graded copy sold as
raw, a misprint, a typo), so the model treats one sale as weak evidence and
asks for confirmation before alerting.

For every card printing it works out
  * percent change over 1, 7 and 30 days
  * "vs. normal": the move divided by that card's own usual price swing,
    measured from the size of its typical price change per sale and how
    often it sells, so jumpy cards need bigger moves to count
  * a percentile: where the 7-day move ranks against every other card today
  * sale days: days in the last 7 / 30 on which the price changed, a rough
    free stand-in for how often the card sells
  * confirmation: did listing prices (lowest or median listing) move the
    same way, and did two or more separate sales move the same way
and raises an alert when the thresholds in config.json are crossed.
"""
from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from datetime import date

STEP_FLOOR = 0.02        # never treat a card's typical price change as under 2%
MIN_UPDATES = 3          # past sales needed before "vs. normal" is trusted
LAG_SLACK_DAYS = 4       # how stale a "7 days ago" price may be
CHANGE_EPS = 0.001       # a price change smaller than 0.1% is not a sale
AGREE_SHARE = 0.4        # listings must move at least 40% as far, same way
OUTLIER_STEP = math.log(3.0)  # one sale moving the price 3x or more


def load_series(store, days: int) -> tuple[dict, list[str]]:
    """{key: [(ordinal_day, market, mid, low), ...]} over the last `days` snapshots."""
    dates = store.dates()[-days:]
    series: dict = {}
    for d in dates:
        o = date.fromisoformat(d).toordinal()
        for key, (market, mid, low) in store.read_snapshot(d).items():
            if market is None or market <= 0:
                continue
            series.setdefault(key, []).append((o, market, mid, low))
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


def _updates(obs: list) -> list:
    """[(day, log return)] for each observation where the market price changed."""
    out = []
    for a, b in zip(obs, obs[1:]):
        r = math.log(b[1] / a[1])
        if abs(r) > CHANGE_EPS:
            out.append((b[0], r))
    return out


def _normal_swing(obs: list, ups: list, upto: int, horizon: int):
    """Typical size of this card's price move over `horizon` days, judged
    only from history up to `upto`. None when there is too little history."""
    past = [r for o, r in ups if o <= upto]
    if len(past) < MIN_UPDATES:
        return None
    step = max(1.4826 * statistics.median(abs(r) for r in past), STEP_FLOOR)
    span = max(min(upto, obs[-1][0]) - obs[0][0], 1)
    rate = len(past) / span                     # sales per day
    return step * math.sqrt(max(1.0, horizon * rate))


def _pct(a, b):
    return None if (a is None or b is None or b <= 0) else (a / b - 1.0) * 100.0


def _listings_agree(now: tuple, then: tuple, move_pct) -> bool:
    """Lowest or median listing moved the same way, at least 40% as far."""
    if move_pct is None or then is None:
        return False
    for i in (3, 2):  # low, mid
        c = _pct(now[i], then[i]) if (now[i] and then[i]) else None
        if c is not None and c * move_pct > 0 and abs(c) >= AGREE_SHARE * abs(move_pct):
            return True
    return False


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
        now = obs[-1]
        p0 = now[1]
        o1 = obs[-2] if len(obs) >= 2 and today - obs[-2][0] <= LAG_SLACK_DAYS else None
        o7 = _at_lag(obs, today, 7)
        o30 = _at_lag(obs, today, 30)
        p7 = o7[1] if o7 else None
        in_band_now = lo <= p0 <= hi
        was_in_band = p7 is not None and lo <= p7 <= hi
        if not (in_band_now or was_in_band):
            continue

        ups = _updates(obs)
        c1 = _pct(p0, o1[1]) if o1 else None
        c7 = _pct(p0, p7)
        c30 = _pct(p0, o30[1]) if o30 else None

        s7 = _normal_swing(obs, ups, today - 7, 7)
        z7 = (math.log(p0 / p7) / s7) if (s7 and p7) else None
        s1 = _normal_swing(obs, ups, today - 1, 1)
        z1 = (math.log(p0 / o1[1]) / s1) if (s1 and o1) else None

        ups7 = [r for o, r in ups if o > today - 7]
        ups30 = [r for o, r in ups if o > today - 30]
        # a sale "goes the same way" if it moved the price in the direction of
        # the overall move by a meaningful share of it (ignores penny changes)
        m7 = abs(math.log(p0 / p7)) if p7 else 0.0
        m30 = abs(math.log(p0 / o30[1])) if o30 else 0.0
        same7 = sum(1 for r in ups7 if c7 is not None and r * c7 > 0
                    and abs(r) >= max(0.01, 0.15 * m7))
        same30 = sum(1 for r in ups30 if c30 is not None and r * c30 > 0
                     and abs(r) >= max(0.01, 0.15 * m30))
        prior = [o for o, _ in ups if today - 30 < o <= today - 7]
        prior_rate = len(prior) / 23.0

        agree7 = _listings_agree(now, o7, c7)
        agree1 = _listings_agree(now, o1, c1)
        agree30 = _listings_agree(now, o30, c30)
        big7 = max((abs(r) for r in ups7), default=0.0)
        outlier7 = big7 >= OUTLIER_STEP and not agree7

        rows.append({
            "pid": pid, "printing": printing, "price": p0,
            "p1": o1[1] if o1 else None, "p7": p7, "p30": o30[1] if o30 else None,
            "c1": c1, "c7": c7, "c30": c30, "z7": z7, "z1": z1,
            "agree7": agree7, "agree1": agree1, "agree30": agree30,
            "same7": same7, "same30": same30, "outlier7": outlier7,
            "sales7": len(ups7), "sales30": len(ups30), "prior_rate": prior_rate,
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
    act_ratio = T.get("activity_spike_ratio", 3.0)
    act_min = T.get("activity_min_sale_days", 3)
    for r in rows:
        reasons, tags = [], []
        c1, c7, c30, z7, z1, p0 = r["c1"], r["c7"], r["c30"], r["z7"], r["z1"], r["price"]

        confirmed7 = r["agree7"] or r["same7"] >= 2
        if c7 is not None and abs(c7) >= T["move_7d_pct"] and abs(p0 - r["p7"]) >= md \
                and confirmed7 and not r["outlier7"]:
            if (z7 is not None and abs(z7) >= T["z_7d_min"]) or \
               (z7 is None and abs(c7) >= T["move_7d_pct_no_history"]):
                reasons.append("7-day move")
        if c1 is not None and abs(c1) >= T["move_1d_pct"] and abs(p0 - r["p1"]) >= md \
                and r["agree1"] and (z1 is None or abs(z1) >= T["z_1d_min"]):
            reasons.append("1-day jump")
        if c30 is not None and abs(c30) >= T["move_30d_pct"] and abs(p0 - r["p30"]) >= md \
                and r["agree30"] and r["same30"] >= 2:
            reasons.append("30-day trend")
        if r["sales7"] >= act_min and r["sales7"] / 7.0 >= act_ratio * max(r["prior_rate"], 1 / 23.0):
            reasons.append("selling more often")
        if r["vol"] and r["vol"]["spike"]:
            reasons.append("volume spike")
        if not reasons:
            continue
        price_reasons = [x for x in reasons if x in ("7-day move", "1-day jump", "30-day trend")]

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
        if r["agree7"] or (price_reasons == ["1-day jump"] and r["agree1"]):
            tags.append("listings agree")
        if r["same7"] >= 2:
            tags.append(f"{r['same7']} sales same way")
        if r["sales30"] <= 2:
            tags.append("thinly traded")

        suspect = r["p1"] is not None and (p0 / r["p1"] > 4 or p0 / r["p1"] < 0.25)
        if suspect:
            tags.append("extreme jump: verify before acting")

        strong7 = "7-day move" in reasons and abs(c7) >= T["high_severity_7d_pct"]
        if suspect:
            severity = "watch"
        elif strong7 and ((r["agree7"] and r["same7"] >= 2) or "selling more often" in reasons
                          or "volume spike" in reasons):
            severity = "high"
        elif "7-day move" in reasons or "volume spike" in reasons or \
                ("selling more often" in reasons and price_reasons):
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
