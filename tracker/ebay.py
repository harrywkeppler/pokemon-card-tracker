"""Minimal client for eBay's Browse API (official, read-only).

Needs a free eBay developer account: the Production App ID and Cert ID go in
the EBAY_CLIENT_ID and EBAY_CLIENT_SECRET secrets. An application token comes
from the client-credentials grant; the default allowance is 5,000 Browse calls
a day.

  search:    GET /buy/browse/v1/item_summary/search   (q, filter, sort, limit)
  get_items: GET /buy/browse/v1/item?item_ids=...      (up to 20, with the
             trading-card condition descriptors: card condition, grader, grade)

Fields used are from eBay's published spec (developer.ebay.com/develop/api/
spec/browse_api.json). watchCount is only returned to apps eBay has granted
access through an App Check ticket; without it the field is absent.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request

SCOPE = "https://api.ebay.com/oauth/api_scope"


class EbayError(Exception):
    pass


class Client:
    def __init__(self, base_url: str, client_id: str, client_secret: str, user_agent: str,
                 marketplace: str = "EBAY_US", pause: float = 0.0):
        self.base = base_url.rstrip("/")
        self.client_id, self.client_secret = client_id, client_secret
        self.user_agent, self.marketplace, self.pause = user_agent, marketplace, pause
        self.token = None
        self.calls = 0

    def _token(self) -> str:
        if self.token:
            return self.token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": SCOPE}).encode()
        req = urllib.request.Request(f"{self.base}/identity/v1/oauth2/token", data=body, headers={
            "Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.token = json.loads(resp.read())["access_token"]
        except (urllib.error.HTTPError, KeyError, ValueError) as e:
            raise EbayError(f"eBay sign-in failed ({e}); check EBAY_CLIENT_ID / EBAY_CLIENT_SECRET")
        return self.token

    def _get(self, path: str, params: dict) -> dict:
        url = f"{self.base}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self._token()}", "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
            "User-Agent": self.user_agent})
        self.calls += 1
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                out = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {}
            raise EbayError(f"eBay HTTP {e.code} on {path}")
        if self.pause:
            time.sleep(self.pause)
        return out

    def search(self, q: str, category_ids: str, filters: list[str], limit: int = 50) -> list[dict]:
        """Active listings for `q`, newest first (by itemOriginDate, so relists keep their age)."""
        params = {"q": q, "sort": "newlyListed", "limit": str(limit)}
        if category_ids:
            params["category_ids"] = category_ids
        if filters:
            params["filter"] = ",".join(filters)
        return self._get("/buy/browse/v1/item_summary/search", params).get("itemSummaries") or []

    def get_items(self, item_ids: list[str]) -> dict:
        """{itemId: item} for up to 20 ids per call (batched here)."""
        out = {}
        for i in range(0, len(item_ids), 20):
            chunk = item_ids[i:i + 20]
            for it in self._get("/buy/browse/v1/item", {"item_ids": ",".join(chunk)}).get("items") or []:
                if it.get("itemId"):
                    out[it["itemId"]] = it
        return out
