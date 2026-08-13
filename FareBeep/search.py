"""THE SHARED LEDGER - optimal search flow for FareBeep.

Exact sequence (per the reconstruction brief), enforced in `LedgerSearch.search()`:

   1. Incoming Request  - user asks for (origin, destination, date)
   2. Ledger Check      - query Supabase `fare_ledger` for a matching row with
                          `last_updated > now - 20 minutes`
   3. THE HIT           - cached price returned immediately (<500ms)
   4. THE MISS          - stale or missing -> call SerpApi (Google Flights engine)
   5. Normalization     - a local Python dict (iata.py) maps city names to IATA
                          codes BEFORE anything touches an API (prevents the
                          "Abuja" -> API-error class of bugs)
   6. Ledger Update     - UPSERT the new data into `fare_ledger` so the whole
                          community benefits from this search

The ledger-first ordering is what makes FareBeep a *community* utility: the
first user's search pays for the SerpApi call; everyone else for the next
20 minutes gets a free, <500ms hit.

PRICE GUARDRAIL: Google Flights data on thin routes is sometimes anomalous
(measured live: LOS->AKR returned $440 = ₦660k when real fares are $40-90).
`search()` flags `above_guardrail` so the conversational layer can say
"prices are unusually high" instead of quoting a number that looks broken.
"""
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from FareBeep.config import (FARE_PRICE_GUARDRAIL_NGN, FX_RATE_NGN_PER_USD,
                             FX_RATE_TTL_HOURS, FX_SAFETY_MARGIN,
                             SERPAPI_API_KEY, SERPAPI_CURRENCY, SERPAPI_ENGINE,
                             SERPAPI_GL_REGION)
from FareBeep.iata import resolve_iata
from FareBeep.models import FareLedger, utcnow

logger = logging.getLogger("farebeep.search")

SERPAPI_BASE_URL = "https://serpapi.com/search.json"
DEFAULT_TIMEOUT = 12.0

# ---------------------------------------------------------------------------
# USD -> NGN - tracked live rate (Google-basis + margin, floored)
# ---------------------------------------------------------------------------
FX_API_URL = "https://open.er-api.com/v6/latest/USD"
_fx_cache = {"ts": 0.0, "rate": None, "live": None}


def fetch_usd_ngn(http_client: httpx.Client = None) -> Optional[float]:
    """The OFFICIAL/Google-basis USD->NGN rate (open.er-api.com, free,
    updated daily ~midnight UTC). Returns None on failure."""
    try:
        client = http_client or httpx.Client(timeout=8.0)
        resp = client.get(FX_API_URL)
        resp.raise_for_status()
        rate = float((resp.json().get("rates") or {}).get("NGN"))
        return rate if rate > 0 else None
    except Exception as e:
        logger.warning("FX API failed: %s", e)
        return None


def ngn_per_usd(floor: float = None, ttl_hours: int = None,
                safety_margin: float = None,
                http_client: httpx.Client = None) -> float:
    """The NGN rate used for quoting fares.

    = tracked official rate x (1 + safety_margin), floored at
      FX_RATE_NGN_PER_USD.

    Why: prices track what Google Flights shows in naira (its conversion is
    on the official/CBN basis) while the +3% buffer and the absolute floor
    (parallel-market rate) protect the founder's margin when the naira
    moves - the 'price tracking' the settlement brief wants.

    Cached for FX_RATE_TTL_HOURS; on API failure the floor is used.
    """
    floor = FX_RATE_NGN_PER_USD if floor is None else floor
    ttl_hours = FX_RATE_TTL_HOURS if ttl_hours is None else ttl_hours
    safety_margin = FX_SAFETY_MARGIN if safety_margin is None else safety_margin
    now = time.time()
    if _fx_cache["rate"] is not None and now - _fx_cache["ts"] < ttl_hours * 3600:
        return _fx_cache["rate"]
    live = fetch_usd_ngn(http_client)
    if live is None:
        logger.warning("FX: falling back to floor %s", floor)
        return floor
    rate = max(floor, live * (1.0 + safety_margin))
    _fx_cache.update(ts=now, rate=rate, live=live)
    logger.info("FX: official %s x (1+%.2f) = %s (floor %s)", live,
                safety_margin, round(rate, 2), floor)
    return rate


def _ngn_verify_link(link: str) -> str:
    """Force the shared Google Flights link to NGN so the user sees fares in
    the same currency the bot quotes (SerpApi still fetches USD internally;
    the link shown is always NGN), with Nigeria as the locale (gl=NG)."""
    if not link:
        return link
    if re.search(r"[?&]curr=[^&]+", link, re.IGNORECASE):
        link = re.sub(r"([?&])curr=[^&]+", r"\1curr=NGN", link,
                      flags=re.IGNORECASE)
    else:
        sep = "&" if "?" in link else "?"
        link = f"{link}{sep}curr=NGN"
    if re.search(r"[?&]gl=[^&]+", link, re.IGNORECASE):
        link = re.sub(r"([?&])gl=[^&]+", r"\1gl=NG", link,
                      flags=re.IGNORECASE)
    return link


class SearchError(Exception):
    """Raised when the live API cannot produce a fare."""


# ---------------------------------------------------------------------------
# The live engine: SerpApi -> Google Flights
# ---------------------------------------------------------------------------
class SerpApiGoogleFlights:
    """SerpApi wrapper around the Google Flights engine (one-way, economy)."""

    def __init__(self, api_key: str = None, engine: str = None,
                 currency: str = None, fx_rate: float = None,
                 gl: str = None, http_client: httpx.Client = None):
        self.api_key = api_key or SERPAPI_API_KEY
        self.engine = engine or SERPAPI_ENGINE
        self.currency = currency or SERPAPI_CURRENCY
        # injected rate wins (tests); otherwise the LIVE daily rate, floored
        self.fx_rate = fx_rate
        self.gl = gl or SERPAPI_GL_REGION
        self._http = http_client

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=DEFAULT_TIMEOUT)
        return self._http

    def fetch(self, origin: str, destination: str,
              flight_date: str) -> Optional[dict]:
        """Ask the Google Flights engine for the cheapest one-way fare.

        Args:
            origin/destination: IATA codes (already normalized upstream).
            flight_date: "YYYY-MM-DD".

        Returns a normalized dict {price, currency, airline, verify_link}
        or None if the engine has no data. Prices are returned in NGN
        (SerpApi rejects NGN - verified live - so USD is fetched and
        converted with the configured rate).
        """
        if not self.api_key:
            raise SearchError(
                "SERPAPI_API_KEY not set - cannot call the Google Flights engine.")

        params = {
            "engine": self.engine,
            "departure_id": origin,
            "arrival_id": destination,
            "outbound_date": flight_date,
            "type": "2",            # one-way (1 = round trip, 2 = one way)
            "currency": self.currency,
            "hl": "en",
            "gl": self.gl,          # region focus (ng = Nigeria results bias)
            "api_key": self.api_key,
        }
        try:
            resp = self._client().get(SERPAPI_BASE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            raise SearchError(f"SerpApi request failed: {e}") from e

        # The engine returns prices in `self.currency` (default USD - NGN is
        # not supported). Convert to NGN so the ledger + markup math stay
        # consistent for the whole utility.
        def _to_ngn(price_usd: float) -> float:
            rate = self.fx_rate or ngn_per_usd()
            return round(float(price_usd) * rate, 2)

        cheapest = self._cheapest_flight(data)
        if cheapest is None:
            logger.info("SerpApi: no results for %s->%s on %s",
                        origin, destination, flight_date)
            return None

        price, airline, link = cheapest
        return {
            "price": _to_ngn(price),
            "currency": "NGN",
            "airline": airline,
            "verify_link": _ngn_verify_link(link),
        }

    def _cheapest_flight(self, data: dict) -> Optional[tuple]:
        """Return (price_usd, airline, link) of the cheapest one-way.

        Priority: SerpApi's `best_flights` -> cheapest `other_flights`
        (one-way legs only) -> Google's `price_insights.lowest_price`.
        """
        candidates = []

        for group in (data.get("best_flights") or []):
            if not group:
                continue
            price = group.get("price")
            if price is None:
                continue
            flights = group.get("flights") or []
            airline = (flights[0].get("airline") if flights else None) \
                or group.get("airline") or "Unknown"
            candidates.append((float(price), airline, group.get("link")))

        for row in (data.get("other_flights") or []):
            if not row or row.get("type") != "One way":
                continue
            price = row.get("price")
            if price is None:
                continue
            flights = row.get("flights") or []
            airline = (flights[0].get("airline") if flights else None) \
                or "Unknown"
            candidates.append((float(price), airline, data.get(
                "search_metadata", {}).get("google_flights_url")))

        if candidates:
            return min(candidates, key=lambda c: c[0])

        lowest = (data.get("price_insights") or {}).get("lowest_price")
        if lowest is not None:
            link = data.get("search_metadata", {}).get("google_flights_url")
            return float(lowest), "Google Flights", link

        return None


# ---------------------------------------------------------------------------
# The shared-ledger search service (the 6-step flow)
# ---------------------------------------------------------------------------
class LedgerSearch:
    """The single entry point for every fare request.

    Guarantee: the database is ALWAYS consulted before the API.
    (Verified by tests/test_search_db_first.py)
    """

    def __init__(self, db: Session, live: Callable = None,
                 ledger_ttl_minutes: int = 20, clock: Callable = None,
                 price_guardrail: float = None):
        self.db = db
        self.live = live or SerpApiGoogleFlights()
        self.ledger_ttl_minutes = ledger_ttl_minutes
        self.price_guardrail = (
            price_guardrail if price_guardrail is not None
            else FARE_PRICE_GUARDRAIL_NGN)
        self.clock = clock or utcnow
        self.call_order: list[str] = []   # observable: ["ledger", ...] before ["api", ...]

    # -- step 2+3: the ledger check ----------------------------------------
    def _ledger_hit(self, origin: str, destination: str,
                    flight_date: str) -> Optional[FareLedger]:
        """Query `fare_ledger` for a FRESH (last_updated > now - TTL) match."""
        self.call_order.append("ledger")
        cutoff = self.clock() - timedelta(minutes=self.ledger_ttl_minutes)
        return (
            self.db.query(FareLedger)
            .filter(FareLedger.origin == origin,
                    FareLedger.destination == destination,
                    FareLedger.flight_date == flight_date,
                    FareLedger.last_updated > cutoff)
            .first()
        )

    # -- step 6: the ledger update (UPSERT semantics) ----------------------
    def _ledger_upsert(self, origin: str, destination: str, flight_date: str,
                       price: float, currency: str, airline: str,
                       verify_link: str) -> FareLedger:
        """Insert-or-update the row for (origin, destination, flight_date).

        The unique constraint on (origin, destination, flight_date) in
        schema.sql guarantees one row per route+date; re-running a search
        simply overwrites price + last_updated, i.e. an UPSERT.

        Postgres path (Supabase Shared Ledger): a single
        INSERT ... ON CONFLICT (origin, destination, flight_date)
        DO UPDATE ... statement - atomic, one round-trip (cloud latency
        friendly). SQLite fallback keeps the select-then-write dance.
        """
        now = self.clock()
        if _is_postgres(self.db):
            stmt = pg_insert(FareLedger).values(
                origin=origin, destination=destination, flight_date=flight_date,
                price=price, currency=currency, airline=airline,
                verify_link=verify_link, last_updated=now)
            stmt = stmt.on_conflict_do_update(
                index_elements=[FareLedger.origin, FareLedger.destination,
                                FareLedger.flight_date],
                set_={"price": price, "currency": currency,
                      "airline": airline, "verify_link": verify_link,
                      "last_updated": now})
            self.db.execute(stmt)
            self.db.commit()
            row = self.db.query(FareLedger).filter(
                FareLedger.origin == origin,
                FareLedger.destination == destination,
                FareLedger.flight_date == flight_date).first()
            return row

        row = (self.db.query(FareLedger)
               .filter(FareLedger.origin == origin,
                       FareLedger.destination == destination,
                       FareLedger.flight_date == flight_date)
               .first())
        if row is None:
            row = FareLedger(origin=origin, destination=destination,
                             flight_date=flight_date, price=price,
                             currency=currency, airline=airline,
                             verify_link=verify_link, last_updated=now)
            self.db.add(row)
        else:
            row.price, row.currency = price, currency
            row.airline, row.verify_link = airline, verify_link
            row.last_updated = now
        self.db.commit()
        return row

    # -- the 6-step flow -----------------------------------------------------
    def search(self, origin, destination, flight_date,
               force_refresh: bool = False) -> Optional[dict]:
        """Resolve a fare for (origin, destination, date). Ledger first.

        force_refresh = True (the BOOK handshake): the Shared Ledger is
        IGNORED and the engine is queried LIVE. "Ensure the seat still
        exists at the quoted price" - the settlement brief. The result is
        still UPSERTed when sane, so the community ledger benefits too.

        Returns:
            {price, currency, airline, flight_date, verify_link, source}
            where source == "ledger" (step 3 hit) or "serpapi" (step 4 miss),
            or None when neither the ledger nor the engine has data.
        """
        # step 5 (applied up front so APIs never see a raw city name):
        # local Python dict maps cities -> IATA before any external call.
        o = resolve_iata(origin)
        d = resolve_iata(destination)
        if not o or not d:
            logger.warning("Search rejected: unresolvable IATA codes "
                           "origin=%r destination=%r", origin, destination)
            return None
        date_str = _as_date_str(flight_date)

        # step 2: ledger check -> step 3: the hit (skipped on force_refresh)
        if not force_refresh:
            cached = self._ledger_hit(o, d, date_str)
            if cached is not None:
                logger.info("LEDGER HIT (<500ms): %s->%s on %s = %s",
                            o, d, date_str, cached.price)
                return {
                    "price": cached.price,
                    "currency": cached.currency,
                    "airline": cached.airline,
                    "flight_date": date_str,
                    "verify_link": cached.verify_link,
                    "source": "ledger",
                    "above_guardrail": cached.price > self.price_guardrail,
                }

        # step 4: the miss -> SerpApi (Google Flights engine)
        result = self.live.fetch(o, d, date_str)
        if result is None:
            logger.info("No live fare for %s->%s on %s", o, d, date_str)
            return None

        above = result["price"] > self.price_guardrail

        # step 6: ledger update (UPSERT) - the community benefit. SANE prices
        # only: a surge/anomaly must never poison the ledger, or every user
        # for the next 20 minutes would be quoted the broken number too.
        if above:
            logger.warning(
                "Surge price %s->%s on %s = NGN %.2f - NOT upserted into "
                "the shared ledger", o, d, date_str, result["price"])
        else:
            self._ledger_upsert(o, d, date_str, result["price"],
                                result["currency"], result["airline"],
                                result.get("verify_link"))
            logger.info("LEDGER MISS -> UPSERTED %s->%s on %s = %s",
                        o, d, date_str, result["price"])
        return {**result, "flight_date": date_str, "source": "serpapi",
                "above_guardrail": above}


def _is_postgres(db) -> bool:
    """True when the session is bound to the Postgres Shared Ledger
    (driver psycopg2 / dialect postgresql)."""
    return db.get_bind().dialect.name == "postgresql"


def _as_date_str(flight_date) -> str:
    """Accept a datetime, date, or "YYYY-MM-DD"; return "YYYY-MM-DD".
    A missing date (user didn't say when) defaults to today - never crash."""
    if flight_date in (None, ""):
        return datetime.utcnow().strftime("%Y-%m-%d")
    if isinstance(flight_date, str):
        return flight_date[:10]
    if isinstance(flight_date, datetime):
        return flight_date.strftime("%Y-%m-%d")
    return flight_date.strftime("%Y-%m-%d")  # datetime.date
