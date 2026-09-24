"""247TRAVELS INVENTORY - the ONLY flight-supply client (async).

Service: 247travels.com/api ("Travels247" is their product name - in THIS
repo the inventory side is always called "travels247", never bare
"skylink", so it cannot be confused with the DELAY feed, which lives
in FareBeep/flight_status.py and is a different API).

Auth: TRAVELS247_EMAIL + TRAVELS247_PASSWORD -> JWT access_token.

The booking flow, per the vendor integration guide v1.0:

  1. login          POST /api/login -> JWT access_token (900s) + refresh_token
  2. search         POST /api/flights/search (search_mode="external") ->
                    offers, each with a booking_token
  3. pricing        POST /api/flights/pricing (booking_token) -> verified_price,
                    price_changed + a REFRESHED booking_token (always use the
                    token from THIS response for reserve, never the search one)
  4. reserve        POST /api/flights/reserve (booking_token + travellers) ->
                    confirmed PNR. Unconditional: Travels247 creates a LIVE PNR
                    on every valid call, so FareBeep only calls reserve AFTER
                    Paystack confirms payment (see transactions.settle_payment).

Resilience follows providers.RetryClient policy: retries on connect
errors, timeouts, 429 (honouring Retry-After) and 5xx with exponential
backoff + jitter; 4xx (except 408/429) is fatal immediately. A 401
triggers one forced re-login + retry (the cached token may have died
early). Pass http_client=<httpx.AsyncClient> in tests (MockTransport).
"""
import asyncio
import logging
import random
import time
from typing import Any, Optional

import httpx

from FareBeep.config import (HTTP_MAX_RETRIES, HTTP_TIMEOUT, SLEEP_SCALE,
                              TRAVELS247_BASE_URL,
                              TRAVELS247_EMAIL, TRAVELS247_PASSWORD)

logger = logging.getLogger("farebeep.travels247")

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
TOKEN_SAFETY_MARGIN = 60.0   # re-login a minute before the 900s token dies


class Travels247Error(Exception):
    """A 247travels call failed after retries, or the payload was unusable."""


class Travels247Client:
    """Async wrapper for 247travels inventory: login -> search -> price -> reserve."""

    def __init__(self, base_url: str = None, email: str = None,
                 password: str = None, timeout: float = None,
                 max_retries: int = None,
                 http_client: httpx.AsyncClient = None):
        self.base_url = (base_url or TRAVELS247_BASE_URL).rstrip("/")
        self.email = email or TRAVELS247_EMAIL
        self.password = password or TRAVELS247_PASSWORD
        self.timeout = timeout or HTTP_TIMEOUT
        self.max_retries = max_retries or HTTP_MAX_RETRIES
        self._http = http_client or httpx.AsyncClient(timeout=self.timeout)
        self._owns = http_client is None
        self._token: Optional[str] = None
        self._token_expires_at = 0.0

    async def close(self) -> None:
        if self._owns:
            await self._http.aclose()

    # -- resilient HTTP -------------------------------------------------
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
                    raise Travels247Error(
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
                raise Travels247Error(
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

    # -- auth ------------------------------------------------------------
    async def _access_token(self) -> str:
        if self._token is None or time.monotonic() >= self._token_expires_at:
            await self.login()
        return self._token

    async def login(self) -> str:
        """POST /api/login; caches the 900s token (roles api/admin only)."""
        if not self.email or not self.password:
            raise Travels247Error(
                "TRAVELS247_EMAIL / TRAVELS247_PASSWORD not set - add the "
                "partner api-role credentials to FareBeep/.env "
                "(NOT the delay-feed key - see FareBeep/flight_status.py)")
        data = await self._request(
            "POST", "/login", auth=False,
            json={"email": self.email, "password": self.password})
        try:
            payload = data["data"]
            token = payload["access_token"]
            expires_in = int(payload.get("expires_in", 900))
        except (KeyError, TypeError, ValueError) as e:
            raise Travels247Error(
                f"Travels247 login response unusable: {str(data)[:200]}") from e
        self._token = token
        self._token_expires_at = (time.monotonic()
                                  + max(expires_in - TOKEN_SAFETY_MARGIN, 60.0))
        return token

    # -- inventory ---------------------------------------------------------
    async def search_offers(self, origin: str, destination: str,
                            flight_date: str, adults: int = 1,
                            children: int = 0, infants: int = 0,
                            cabin: str = "economy",
                            currency: str = "NGN") -> list:
        """POST /api/flights/search (search_mode="external", oneway).

        Returns normalized offers: {flight_no, airline, airline_name,
        departure_code, arrival_code, departure_time, arrival_time,
        duration, baggage, cabin, price, currency, booking_token}.
        """
        data = await self._request(
            "POST", "/flights/search",
            json={"search_mode": "external",
                  "from": origin, "to": destination,
                  "flight_type": "oneway",
                  "flights_departure_date": flight_date[:10],
                  "adults": adults, "children": children,
                  "infants": infants, "class": cabin,
                  "currency": currency})
        flights = (data.get("data") or {}).get("flights") or []
        offers = [self._normalize_offer(f, currency) for f in flights]
        return [o for o in offers if o is not None]

    @staticmethod
    def _normalize_offer(f: dict, currency: str) -> Optional[dict]:
        """Flatten one offer to the stable normalized shape.

        TWO wire shapes exist and both are accepted: the flat shape
        ({flight_no, airline, price, booking_token} at top level) and
        the nested shape where flight facts live in segments[0][0]
        ({img, flight_no, airline, departure/arrival_*}) with price /
        booking_token at top level. Missing flight_no or price skips
        the offer (it can be neither shown nor booked).
        """
        try:
            segs = f.get("segments") or []
            seg = segs[0] if segs else None
            if isinstance(seg, list):  # segments[0][0]: legs of leg 1
                seg = seg[0] if seg else None
            if not isinstance(seg, dict):
                seg = {}

            def _pick(*names):
                for n in names:
                    if f.get(n) is not None:
                        return f[n]
                    if seg.get(n) is not None:
                        return seg[n]
                return None

            flight_no = _pick("flight_no")
            price = _pick("price")
            if not flight_no or price is None:
                return None
            raw_code = f.get("airline") or seg.get("img")
            code = (raw_code if isinstance(raw_code, str)
                    and len(raw_code.strip()) <= 3 else None)
            name = seg.get("airline") or f.get("airline_name") or code
            return {
                "flight_no": flight_no,
                "airline": code or seg.get("img") or f.get("airline"),
                "airline_name": (name.title() if isinstance(name, str)
                                 else name),
                "departure_code": _pick("departure_code"),
                "arrival_code": _pick("arrival_code"),
                "departure_time": _pick("departure_time"),
                "arrival_time": _pick("arrival_time"),
                "duration": _pick("duration_time", "duration"),
                "baggage": _pick("baggage"),
                "cabin": _pick("class", "cabin"),
                "seats_left": _pick("seats_left"),
                "price": float(price),
                "currency": f.get("currency") or currency,
                "booking_token": f.get("booking_token"),
            }
        except (KeyError, TypeError, ValueError):
            logger.warning("Skipping unusable Travels247 offer: %s",
                           str(f)[:160])
            return None

    async def verify_price(self, booking_token: str, adults: int = 1,
                           children: int = 0, infants: int = 0,
                           cabin: str = "economy",
                           currency: str = "NGN") -> dict:
        """POST /api/flights/pricing - re-validates ONE offer live.

        Returns {verified, price_changed, original_price, verified_price,
        currency, booking_token (REFRESHED - use this for reserve),
        expires_at}. Always call immediately before reserve.
        """
        data = await self._request(
            "POST", "/flights/pricing",
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
            raise Travels247Error(
                f"Travels247 pricing response unusable: {str(data)[:200]}") from e

    async def reserve(self, booking_token: str, travellers: dict,
                      adults: int = 1, children: int = 0, infants: int = 0,
                      ticket_time_limit_hours: int = 48) -> dict:
        """POST /api/flights/reserve - creates the confirmed PNR.

        UNCONDITIONAL: Travels247 tickets on every valid call, so this must
        only run AFTER Paystack confirms payment (ToS). Returns {pnr,
        booking_reference, carrier, status, ticket_deadline}.
        """
        data = await self._request(
            "POST", "/flights/reserve",
            json={"booking_token": booking_token,
                  "travellers": travellers,
                  "passengers": {"adults": adults, "children": children,
                                 "infants": infants},
                  "ticket_time_limit_hours": ticket_time_limit_hours})
        try:
            p = data["data"]
            pnr = p["pnr"]
        except (KeyError, TypeError) as e:
            raise Travels247Error(
                f"Travels247 reserve response unusable: {str(data)[:200]}") from e
        return {
            "pnr": pnr,
            "booking_reference": p.get("booking_reference") or pnr,
            "carrier": p.get("carrier"),
            "status": p.get("status"),
            "ticket_deadline": p.get("ticket_deadline"),
        }


def pick_cheapest(offers: list) -> Optional[dict]:
    """The lowest-priced offer that carries a booking_token (without one
    it can be quoted but never priced or reserved)."""
    usable = [o for o in offers if o.get("booking_token")]
    if not usable:
        return None
    return min(usable, key=lambda o: o["price"])


__all__ = ["Travels247Client", "Travels247Error", "pick_cheapest"]
