"""Client for tcgcsv.com, the free daily mirror of TCGplayer prices.

House rules from https://tcgcsv.com/docs that this module follows:
  * one sync per day, and only when last-updated.txt has moved on
  * a custom User-Agent on every request
  * a short pause between requests
  * each daily price archive is downloaded once and never again
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


class Client:
    def __init__(self, base_url: str, user_agent: str, pause: float = 0.15):
        self.base = base_url.rstrip("/")
        self.ua = user_agent
        self.pause = pause
        self.requests_made = 0

    # ---- low-level -----------------------------------------------------
    def _get(self, path: str, tries: int = 4) -> bytes:
        url = f"{self.base}{path}"
        last = None
        for attempt in range(tries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": self.ua})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = resp.read()
                self.requests_made += 1
                time.sleep(self.pause)
                return data
            except urllib.error.HTTPError as e:
                last = e
                e.close()
                if e.code in (403, 404):
                    raise
                time.sleep(2 + 5 * attempt)
            except Exception as e:  # network hiccup
                last = e
                time.sleep(2 + 5 * attempt)
        raise RuntimeError(f"GET {url} failed after {tries} tries: {last}")

    def _json(self, path: str) -> list:
        body = json.loads(self._get(path))
        if isinstance(body, dict):
            if body.get("success") is False:
                raise RuntimeError(f"{path}: {body.get('errors')}")
            return body.get("results", [])
        return body

    # ---- endpoints -----------------------------------------------------
    def last_updated(self) -> datetime:
        raw = self._get("/last-updated.txt").decode().strip()
        return parse_ts(raw)

    def groups(self, category: int) -> list:
        return self._json(f"/tcgplayer/{category}/groups")

    def products(self, category: int, group_id: int) -> list:
        return self._json(f"/tcgplayer/{category}/{group_id}/products")

    def prices(self, category: int, group_id: int) -> list:
        return self._json(f"/tcgplayer/{category}/{group_id}/prices")

    # ---- daily archive (used once, to seed history) -----------------------
    def archive_prices(self, day: str, categories: list[int]) -> dict | None:
        """Return {category: {group_id: [price rows]}} for one past day,
        or None when no archive exists for that day."""
        try:
            blob = self._get(f"/archive/tcgplayer/prices-{day}.ppmd.7z", tries=2)
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return None
            raise
        tmp = tempfile.mkdtemp(prefix="tcgcsv_")
        try:
            arc = os.path.join(tmp, "a.7z")
            with open(arc, "wb") as f:
                f.write(blob)
            out = os.path.join(tmp, "x")
            os.makedirs(out)
            _extract(arc, out, categories)
            return _read_extracted(out, categories)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def parse_ts(raw: str) -> datetime:
    raw = raw.strip().replace("Z", "+00:00")
    # trim sub-microsecond digits some builds emit
    if "." in raw:
        head, tail = raw.split(".", 1)
        frac, tz = tail, ""
        for sep in ("+", "-"):
            if sep in tail:
                frac, rest = tail.split(sep, 1)
                tz = sep + rest
                break
        raw = f"{head}.{frac[:6]}{tz}"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _wanted(name: str, categories: list[int]) -> bool:
    parts = name.replace("\\", "/").strip("/").split("/")
    # layout: <day>/<category>/<group>/prices
    return (
        len(parts) >= 3
        and parts[-1] == "prices"
        and parts[-3] in {str(c) for c in categories}
    )


def _extract(arc: str, out: str, categories: list[int]) -> None:
    try:
        import py7zr  # type: ignore

        with py7zr.SevenZipFile(arc, "r") as z:
            names = [n for n in z.getnames() if _wanted(n, categories)]
            if names:
                z.extract(path=out, targets=names)
                return
    except Exception as e:  # fall through to the 7z binary
        print(f"  py7zr could not read archive ({e}); trying 7z binary")
    exe = shutil.which("7z") or shutil.which("7zz") or shutil.which("7za")
    if not exe:
        raise RuntimeError("need the py7zr package or a 7z binary to read archives")
    subprocess.run([exe, "x", "-y", f"-o{out}", arc], check=True,
                   stdout=subprocess.DEVNULL)


def _read_extracted(out: str, categories: list[int]) -> dict:
    result: dict = {c: {} for c in categories}
    for root, _dirs, files in os.walk(out):
        if "prices" not in files:
            continue
        rel = os.path.relpath(os.path.join(root, "prices"), out)
        if not _wanted(rel, categories):
            continue
        parts = rel.replace("\\", "/").split("/")
        try:
            cat, gid = int(parts[-3]), int(parts[-2])
        except ValueError:
            continue
        try:
            with open(os.path.join(root, "prices"), "rb") as f:
                body = json.load(f)
        except Exception:
            continue
        rows = body.get("results", []) if isinstance(body, dict) else body
        result[cat][gid] = rows
    return result
