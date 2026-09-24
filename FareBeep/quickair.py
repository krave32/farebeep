"""QuickAir supplier client (quickair.app) - Nigerian B2B flight inventory.

Contract-complete implementation of the supplier interface (see
suppliers.py). QuickAir has no public API docs; the endpoints below mirror
the 247travels portal-API shape (email/password login -> bearer token ->
search/price/reserve), which is the common pattern for Nigerian agency
portals. VERIFY each constant against the real dashboard's network tab
the moment agency access exists - everything vendor-specific lives in
the marked block so adaptation is one place.

Response mapping is deliberately defensive: unknown field names raise
QuickAirError with the raw payload in the message (same idiom as
travels247.py), never silently wrong prices.
"""
import asyncio
import logging
import random
import time
from typing import Any, Optional

import httpx

from FareBeep.config import (QUICKAIR_BASE_URL, QUICKAIR_EMAIL,
                             QUICKAIR_PASSWORD, SLEEP_SCALE)

logger = logging.getLogger("farebeep.quickair")

HTTP_TIMEOUT = 25.0
HTTP_MAX_RETRIES = 3
TOKEN_SAFETY_MARGIN = 30.0
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class QuickAirError(Exception):
    """Raised for auth failures, unusable responses, and exhausted retries."""


# ---------------------------------------------------------------------------
# VENDOR BLOCK - the only place that knows QuickAir's wire format.
# Verify/adjust against the real dashboard before production use.
# ---------------------------------------------------------------------------
VENDOR = {
    # Paths are relative to base_url (which already ends in /api, the
    # same convention as travels247).
    "login_path": "/login",              # POST {email, password}
    "search_path": "/flights/search",    # POST search payload
    "pricing_path": "/flights/pricing",
    "reserve_path": "/flights/reserve",
    "token_field": ("data", "access_token"),
    "expires_field": ("data", "expires_in"),
    "offers_field": ("data", "offers"),    # list of raw offers
    "map": {                               # raw offer -> normalized offer
        "flight_no": ("flight_no", "flightNumber"),
        "airline": ("airline", "airlineCode"),
        "airline_name": ("airline_name", "airlineName"),
        "departure_code": ("departure_code", "from", "origin"),
        "arrival_code": ("arrival_code", "to", "destination"),
        "departure_time": ("departure_time", "departTime"),
        "arrival_time": ("arrival_time", "arriveTime"),
        "duration": ("duration",),
        "baggage": ("baggage",),
        "cabin": ("cabin",),
        "price": ("price", "fare", "totalPrice"),
        "currency": ("currency",),
        "booking_token": ("booking_token", "token"),
    },
}
# ---------------------------------------------------------------------------


def _dig(obj: Any, path: tuple) -> Any:
    """Follow a tuple of keys into nested dicts; None anywhere = miss."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _first(raw: dict, names: tuple):
    for n in names:
        if raw.get(n) is not None:
            return raw[n]
    return None


def _normalize(raw: dict) -> Optional[dict]:
    """Raw vendor offer -> the normalized contract. Price must parse."""
    m = VENDOR["map"]
    try:
        price = float(_first(raw, m["price"]))
    except (TypeError, ValueError):
        return None
    flight_no = _first(raw, m["flight_no"])
    return {
        "flight_no": str(flight_no) if flight_no is not None else "",
        "airline": str(_first(raw, m["airline"]) or ""),
        "airline_name": str(_first(raw, m["airline_name"]) or ""),
        "departure_code": str(_first(raw, m["departure_code"]) or "").upper(),
        "arrival_code": str(_first(raw, m["arrival_code"]) or "").upper(),
        "departure_time": _first(raw, m["departure_time"]),
        "arrival_time": _first(raw, m["arrival_time"]),
        "duration": _first(raw, m["duration"]),
        "baggage": _first(raw, m["baggage"]),
        "cabin": _first(raw, m["cabin"]) or "economy",
        "price": price,
        "currency": str(_first(raw, m["currency"]) or "NGN"),
        "booking_token": _first(raw, m["booking_token"]),
    }


class QuickAirClient:
    """Async wrapper for QuickAir inventory: login -> search -> price -> reserve."""

    def __init__(self, base_url: str = None, email: str = None,
                 password: str = None, timeout: float = None,
                 max_retries: int = None,
                 http_client: httpx.AsyncClient = None):
        self.base_url = (base_url or QUICKAIR_BASE_URL).rstrip("/")
        self.email = email if email is not None else QUICKAIR_EMAIL
        self.password = password if password is not None else QUICKAIR_PASSWORD
        self.timeout = timeout or HTTP_TIMEOUT
        self.max_retries = max_retries or HTTP_MAX_RETRIES
        self._http = http_client or httpx.AsyncClient(timeout=self.timeout)
        self._owns = http_client is None
        self._token: Optional[str] = None
        self._token_expires_at = 0.0

    async def close(self) -> None:
        if self._owns:
            await self._http.aclose()

    # -- resilient HTTP (same idiom as travels247) ------------------------
    async def _request(self, method: str, path: str, *,
                       auth: bool = True, _reauth: bool = False,
                       **kw) -> Any:
        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            attempt += 1
            headers = dict(kw.pop("headers", {}) or {})
            if auth:
                headers["Authorization"] = f"Bearer {await self._access_token()}"
            try:
                resp = await self._http.request(method, url,
                                                headers=headers, **kw)
            except (httpx.ConnectError, httpx.TimeoutException,
                    httpx.RemoteProtocolError) as e:
                if attempt > self.max_retries:
                    raise QuickAirError(
                        f"{method} {path} failed after {attempt} attempts: "
                        f"{e}") from e
                await asyncio.sleep(self._backoff(None, attempt))
                continue
            if resp.status_code == 401 and auth and not _reauth:
                self._token_expires_at = 0.0
                return await self._request(method, path, auth=auth,
                                           _reauth=True, **kw)
            if resp.status_code == 200:
                return resp.json()
            if (resp.status_code not in RETRYABLE_STATUS
                    or attempt > self.max_retries):
                raise QuickAirError(
                    f"{method} {path} -> HTTP {resp.status_code}: "
                    f"{resp.text[:200]}")
            await asyncio.sleep(self._backoff(resp, attempt))

    @staticmethod
    def _backoff(resp: Optional[httpx.Response], attempt: int) -> float:
        if resp is not None:
            retry_after = resp.headers.get("retry-after")
            try:
                if retry_after is not None:
                    return SLEEP_SCALE * min(float(retry_after), 30.0)
            except ValueError:
                pass
        return SLEEP_SCALE * min(0.5 * (2 ** (attempt - 1)), 8.0) \
            + SLEEP_SCALE * random.uniform(0, 0.25)

    # -- auth ---------------------------------------------------------------
    async def _access_token(self) -> str:
        if self._token is None or time.monotonic() >= self._token_expires_at:
            await self.login()
        return self._token

    async def login(self) -> str:
        """POST login; caches the bearer token until shortly before expiry."""
        if not self.email or not self.password:
            raise QuickAirError(
                "QUICKAIR_EMAIL / QUICKAIR_PASSWORD not set - add the "
                "agency credentials to FareBeep/.env")
        data = await self._request(
            "POST", VENDOR["login_path"], auth=False,
            json={"email": self.email, "password": self.password})
        try:
            token = _dig(data, VENDOR["token_field"])
            if not token:
                raise KeyError("no token in response")
            expires_in = int(_dig(data, VENDOR["expires_field"]) or 900)
        except (KeyError, TypeError, ValueError) as e:
            raise QuickAirError(
                f"QuickAir login response unusable: {str(data)[:200]}") from e
        self._token = token
        self._token_expires_at = (time.monotonic()
                                  + max(expires_in - TOKEN_SAFETY_MARGIN, 60.0))
        return token

    # -- inventory -----------------------------------------------------------
    async def search_offers(self, origin: str, destination: str,
                            flight_date: str, adults: int = 1,
                            children: int = 0, infants: int = 0,
                            cabin: str = "economy",
                            currency: str = "NGN") -> list:
        """POST search -> normalized offers (identical contract to
        travels247; see suppliers.py)."""
        data = await self._request(
            "POST", VENDOR["search_path"],
            json={"search_mode": "external",
                  "from": origin, "to": destination,
                  "flight_type": "oneway",
                  "departure_date": flight_date,
                  "passengers": {"adults": adults, "children": children,
                                 "infants": infants},
                  "class": cabin, "currency": currency})
        raw = _dig(data, VENDOR["offers_field"])
        if raw is None and isinstance(data, dict):
            raw = data.get("data") if isinstance(data.get("data"), list) \
                else (data.get("offers") if isinstance(data.get("offers"),
                                                       list) else None)
        if not isinstance(raw, list):
            raise QuickAirError(
                f"QuickAir search response unusable: {str(data)[:200]}")
        offers = [o for o in (_normalize(r) for r in raw
                              if isinstance(r, dict)) if o is not None]
        return offers

    async def verify_price(self, booking_token: str, adults: int = 1,
                           children: int = 0, infants: int = 0,
                           cabin: str = "economy",
                           currency: str = "NGN") -> dict:
        """Re-validate ONE offer live. Returns the same dict shape as
        travels247.verify_price; always call immediately before reserve."""
        data = await self._request(
            "POST", VENDOR["pricing_path"],
            json={"booking_token": booking_token,
                  "passengers": {"adults": adults, "children": children,
                                 "infants": infants},
                  "currency": currency, "class": cabin})
        try:
            p = data["data"]
            return {
                "verified": bool(p.get("verified", True)),
                "price_changed": bool(p.get("price_changed", False)),
                "original_price": float(p.get("original_price",
                                              p["verified_price"])),
                "verified_price": float(p["verified_price"]),
                "currency": p.get("currency") or currency,
                "booking_token": p.get("booking_token") or booking_token,
                "expires_at": p.get("expires_at"),
            }
        except (KeyError, TypeError, ValueError) as e:
            raise QuickAirError(
                f"QuickAir pricing response unusable: {str(data)[:200]}") from e

    async def reserve(self, booking_token: str, travellers: dict,
                      adults: int = 1, children: int = 0, infants: int = 0,
                      ticket_time_limit_hours: int = 48) -> dict:
        """Creates the confirmed PNR. UNCONDITIONAL ticketing: only run
        AFTER Paystack confirms payment (ToS)."""
        data = await self._request(
            "POST", VENDOR["reserve_path"],
            json={"booking_token": booking_token,
                  "travellers": travellers,
                  "passengers": {"adults": adults, "children": children,
                                 "infants": infants},
                  "ticket_time_limit_hours": ticket_time_limit_hours})
        try:
            p = data["data"]
            pnr = p["pnr"]
        except (KeyError, TypeError) as e:
            raise QuickAirError(
                f"QuickAir reserve response unusable: {str(data)[:200]}") from e
        return {
            "pnr": pnr,
            "booking_reference": p.get("booking_reference") or pnr,
            "carrier": p.get("carrier"),
            "status": p.get("status"),
            "ticket_deadline": p.get("ticket_deadline"),
        }


__all__ = ["QuickAirClient", "QuickAirError"]
