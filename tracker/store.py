"""Files the tracker keeps in the repository.

data/prices/YYYY-MM-DD.csv.gz   one price snapshot per day
data/catalog.json               card names, sets, numbers, links
data/state.json                 bookkeeping (last sync, archives already used)
data/alert_log.csv              every alert ever raised
data/volume.csv                 daily sales counts (only with a volume API key)
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import os
from datetime import date, timedelta

Key = tuple  # (productId:int, printing:str)


class Store:
    def __init__(self, root: str):
        self.root = root
        self.data = os.path.join(root, "data")
        self.prices_dir = os.path.join(self.data, "prices")
        os.makedirs(self.prices_dir, exist_ok=True)

    # ---- small json files ------------------------------------------------
    def _load(self, name: str, default):
        p = os.path.join(self.data, name)
        if not os.path.exists(p):
            return default
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def _save(self, name: str, obj) -> None:
        p = os.path.join(self.data, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        os.replace(tmp, p)

    def load_state(self) -> dict:
        return self._load("state.json", {"archives_done": [], "last_updated": None})

    def save_state(self, state: dict) -> None:
        self._save("state.json", state)

    def load_catalog(self) -> dict:
        return self._load("catalog.json", {})

    def save_catalog(self, cat: dict) -> None:
        self._save("catalog.json", cat)

    # ---- snapshots -------------------------------------------------------
    def snapshot_path(self, day: str) -> str:
        return os.path.join(self.prices_dir, f"{day}.csv.gz")

    def has_snapshot(self, day: str) -> bool:
        return os.path.exists(self.snapshot_path(day))

    def dates(self) -> list[str]:
        out = [f[:10] for f in os.listdir(self.prices_dir) if f.endswith(".csv.gz")]
        return sorted(out)

    def write_snapshot(self, day: str, rows: dict) -> int:
        """rows: {(pid, printing): (market, mid, low)}"""
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["productId", "printing", "market", "mid", "low"])
        for (pid, sub) in sorted(rows):
            m, mid, low = rows[(pid, sub)]
            w.writerow([pid, sub, _f(m), _f(mid), _f(low)])
        with open(self.snapshot_path(day), "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
                gz.write(buf.getvalue().encode("utf-8"))
        return len(rows)

    def read_snapshot(self, day: str) -> dict:
        out = {}
        with gzip.open(self.snapshot_path(day), "rt", encoding="utf-8", newline="") as f:
            r = csv.reader(f)
            next(r, None)
            for row in r:
                if len(row) < 5:
                    continue
                out[(int(row[0]), row[1])] = (_n(row[2]), _n(row[3]), _n(row[4]))
        return out

    def prune(self, keep_daily_days: int) -> int:
        """Older than keep_daily_days: keep one snapshot a week (Mondays)."""
        ds = self.dates()
        if not ds:
            return 0
        cutoff = date.fromisoformat(ds[-1]) - timedelta(days=keep_daily_days)
        removed = 0
        for d in ds:
            dd = date.fromisoformat(d)
            if dd < cutoff and dd.weekday() != 0:
                os.remove(self.snapshot_path(d))
                removed += 1
        return removed

    # ---- alert log -------------------------------------------------------
    LOG_FIELDS = ["date", "productId", "printing", "name", "set", "direction",
                  "severity", "reasons", "price", "chg_7d_pct"]

    def read_alert_log(self) -> list[dict]:
        p = os.path.join(self.data, "alert_log.csv")
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))

    def write_alert_log(self, rows: list[dict]) -> None:
        p = os.path.join(self.data, "alert_log.csv")
        with open(p, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=self.LOG_FIELDS, lineterminator="\n")
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in self.LOG_FIELDS})

    # ---- volume (optional) -----------------------------------------------
    def read_volume(self) -> dict:
        """{productId: {day: units_sold}}"""
        p = os.path.join(self.data, "volume.csv")
        out: dict = {}
        if not os.path.exists(p):
            return out
        with open(p, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                try:
                    out.setdefault(int(r["productId"]), {})[r["date"]] = float(r["volume"])
                except (KeyError, ValueError):
                    continue
        return out

    def write_volume(self, vol: dict) -> None:
        p = os.path.join(self.data, "volume.csv")
        with open(p, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["date", "productId", "volume"])
            for pid in sorted(vol):
                for d in sorted(vol[pid]):
                    w.writerow([d, pid, _f(vol[pid][d])])


def _f(x) -> str:
    if x is None:
        return ""
    return f"{float(x):.2f}".rstrip("0").rstrip(".")


def _n(s: str):
    return float(s) if s not in ("", None) else None
