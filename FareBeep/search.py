"""THE SHARED LEDGER - optimal search flow for FareBeep.

Exact sequence, enforced in `LedgerSearch.search()`:

   1. Incoming Request  - user asks for (origin, destination, date)
   2. Ledger Check      - query Supabase `fare_ledger` for a matching row with
                          `last_updated > now - TTL` (8 min on peak trunk
                          routes Mon/Fri, 15 min otherwise - see get_ledger_ttl)
   3. THE HIT           - cached price returned immediately (<500ms)
   4. THE MISS          - stale or missing -> the live inventory supplier
                          (Travels247/QuickAir, see SupplierLiveEngine)
   5. Normalization     - a local Python dict (iata.py) maps city names to IATA
                          codes BEFORE anything touches an API (prevents the
                          "Abuja" -> API-error class of bugs)
   6. Ledger Update     - UPSERT the new data into `fare_ledger` so the whole
                          community benefits from this search

The ledger-first ordering is what makes FareBeep a *community* utility: the
first user's search pays for the live supplier call; everyone else for the
next 8-15 minutes gets a free, <500ms hit.

PRICE GUARDRAIL: aggregated fares on thin routes are sometimes anomalous.
`search()` flags `above_guardrail` so the conversational layer can say
"prices are unusually high" instead of quoting a number that looks broken.
"""
import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from FareBeep.config import FARE_PRICE_GUARDRAIL_NGN, SLEEP_SCALE
from FareBeep.iata import resolve_iata
from FareBeep.models import FareLedger, Subscription, utcnow

logger = logging.getLogger("farebeep.search")

# ---------------------------------------------------------------------------
# Ledger freshness - how long a cached fare is trusted as "Live"
# ---------------------------------------------------------------------------
# The busiest trunk routes, where Nigerian airfares move every ~3 minutes.
VOLATILE_ROUTES = frozenset({
    ("LOS", "ABV"), ("ABV", "LOS"),
    ("LOS", "PHC"), ("PHC", "LOS"),
    ("ABV", "PHC"), ("PHC", "ABV"),
})
PEAK_WEEKDAYS = (0, 4)   # Monday, Friday - the heaviest travel days


def get_ledger_ttl(origin: str, destination: str,
                   clock: Callable = None) -> int:
    """Minutes a `fare_ledger` row is served as "Live" without a re-fetch.

    8 minutes on the volatile trunk routes on Mondays and Fridays, 15
    minutes everywhere else (cached prices must never feel bait-and-switch
    at BOOK time).
    """
    now = (clock or utcnow)()
    if (origin.upper(), destination.upper()) in VOLATILE_ROUTES \
            and now.weekday() in PEAK_WEEKDAYS:
        return 8
    return 15


# ---------------------------------------------------------------------------
# Adaptive TTL - freshness that follows the flight, not a fixed clock
# ---------------------------------------------------------------------------
# Layers on top of get_ledger_ttl (which stays the untouched base rule).
# Pure cache policy: no provider specifics live here.
PROXIMITY_IMMINENT_DAYS = 2    # flying within 2 days -> 5 min
PROXIMITY_IMMINENT_TTL = 5
PROXIMITY_MID_DAYS = 14        # 14-59 days out -> 30 min (stable horizon)
PROXIMITY_MID_TTL = 30
PROXIMITY_FAR_DAYS = 60        # 60+ days out -> 45 min (prices barely move)
PROXIMITY_FAR_TTL = 45
WATCHER_TTL_CAP = 10           # watched routes never staler than 10 min
TTL_FLOOR = 3                  # never trust anything older than this as live
TTL_CEILING = 60               # never hammer providers fresher than this

FESTIVE_START = (12, 15)       # Dec 15 - Jan 5: peak season, trunk rule all
FESTIVE_END = (1, 5)
# Fixed-date public holidays (peak travel). Movable feasts (Eid etc.)
# are added here when dated - the set is data, not logic.
NIGERIA_PUBLIC_HOLIDAYS = frozenset({
    (1, 1),    # New Year's Day
    (5, 1),    # Workers' Day
    (6, 12),   # Democracy Day
    (10, 1),   # Independence Day
    (12, 25),  # Christmas Day
    (12, 26),  # Boxing Day
})


def _as_date(value):
    """'YYYY-MM-DD' / datetime / date -> datetime, or None (no date)."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _is_peak_season(now) -> bool:
    """Festive window or public holiday: the whole network flies peak."""
    if (now.month == FESTIVE_START[0] and now.day >= FESTIVE_START[1]):
        return True
    if (now.month == FESTIVE_END[0] and now.day <= FESTIVE_END[1]):
        return True
    return (now.month, now.day) in NIGERIA_PUBLIC_HOLIDAYS


def _route_watched(db, origin: str, destination: str) -> bool:
    """Any active subscription on this route? Never raises: cache policy
    must never break a search, worst case the base TTL applies."""
    if db is None:
        return False
    try:
        return (db.query(Subscription.id)
                .filter(Subscription.origin == origin.upper(),
                        Subscription.destination == destination.upper())
                .first() is not None)
    except Exception:
        return False


def get_adaptive_ttl(db, origin: str, destination: str,
                     flight_date=None, clock: Callable = None) -> int:
    """Freshness minutes, adapted to the flight and its watchers.

    Layers (each documented, each bounded by the 3-60 guardrails):
      base      - get_ledger_ttl (8 trunk-peak incl. festive/holiday
                  all-route peak, else 15)
      proximity - <=2 days out: 5 (moves fastest, matters most)
                  3-13 days: base (normal booking window)
                  14-59 days: 30, 60+ days: 45 (stable horizon, save calls)
                  no date: base (dateless window answers stay loose)
      watchers  - any subscription on the route caps staleness at 10
    """
    now = (clock or utcnow)()
    today = now.date() if isinstance(now, datetime) else now
    base = get_ledger_ttl(origin, destination, lambda: now)
    if _is_peak_season(now):
        base = min(base, 8)
    target = _as_date(flight_date)
    if target is None:
        ttl = base
    else:
        days_out = (target.date() - today).days
        if days_out < 0:
            # Already flown: a history lookup, not live inventory -
            # no urgency layer, base rule applies.
            ttl = base
        elif days_out <= PROXIMITY_IMMINENT_DAYS:
            ttl = min(base, PROXIMITY_IMMINENT_TTL)
        elif days_out < PROXIMITY_MID_DAYS:
            ttl = base
        elif days_out < PROXIMITY_FAR_DAYS:
            ttl = PROXIMITY_MID_TTL
        else:
            ttl = PROXIMITY_FAR_TTL
    watched = _route_watched(db, origin, destination)
    if ttl > WATCHER_TTL_CAP and watched:
        ttl = WATCHER_TTL_CAP
    ttl = max(TTL_FLOOR, min(TTL_CEILING, ttl))
    logger.debug("Adaptive TTL %s->%s date=%s watchers=%s: %s min",
                 origin, destination, flight_date, watched, ttl)
    return ttl


# ---------------------------------------------------------------------------
# Low-price anomaly verification - the ledger's OTHER sanity filter
# ---------------------------------------------------------------------------
ANOMALY_DROP_THRESHOLD = 0.20       # >20% below the previous row -> suspicious
ANOMALY_RECHECK_WAIT_SECONDS = SLEEP_SCALE * 90   # settle time before the confirm call
CONFIRMATION_TOLERANCE = 0.05       # second call within 5% -> the price is real

# ---------------------------------------------------------------------------
# The live engine: the inventory suppliers (Travels247 / QuickAir)
# ---------------------------------------------------------------------------
def _run_off_loop(coro):
    """Run `coro` to completion from sync code.

    The agent.py rule: one loop per thread, search + close ride the SAME
    loop (httpx binds its pool to the first loop). When a loop is ALREADY
    running on this thread (an async endpoint called in directly), the
    whole search is lifted onto a fresh thread that owns its own loop
    instead of raising - a search must work from anywhere."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box = {}

    def _runner():
        try:
            box["result"] = asyncio.run(coro)
        except BaseException as e:   # re-raised below on the calling thread
            box["error"] = e

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _offer_to_fare(offer: dict) -> dict:
    """Normalized supplier offer -> the fare dict the ledger + replies speak.

    The exact shape the live engine contract has always returned
    ({price, currency, airline, verify_link, ...}); airline_name wins over
    the 3-letter code because replies read "Air Peace", not "P4".
    verify_link is None: no public URL exists for a supplier offer - the
    ledger stores the price and booking proceeds by token."""
    return {
        "price": float(offer["price"]),
        "currency": offer.get("currency") or "NGN",
        "airline": (offer.get("airline_name") or offer.get("airline")
                    or "Unknown"),
        "departs_at": offer.get("departure_time"),
        "arrival_time": offer.get("arrival_time"),
        "duration": offer.get("duration"),
        "flight_number": offer.get("flight_no"),
        "seats_left": offer.get("seats_left"),
        "verify_link": None,
        # Carried through so the chat booking gate can store it on the
        # booking session: token + traveller name at webhook time = a
        # REAL supplier PNR after payment (no extra user round-trip).
        "booking_token": offer.get("booking_token"),
    }


class SupplierLiveEngine:
    """The LIVE fare engine: the inventory supplier (Travels247/QuickAir,
    chosen by INVENTORY_PROVIDER via suppliers.get_inventory_client) behind
    LedgerSearch's sync seam.

    The supplier clients are async (httpx.AsyncClient) while every
    LedgerSearch caller runs on worker threads, so each fetch opens ONE
    event loop for the search and closes the client on that same loop.

    Failure semantics: 'supplier down / no offers' is fetch() -> None and
    fetch_list() -> [] - a vendor outage degrades to 'no live fare' in the
    chat, never a crashed turn. Prices arrive in NGN straight from the
    supplier; no FX conversion anywhere.
    """

    def _offers(self, origin: str, destination: str, flight_date: str) -> list:
        """One supplier search on one event loop, client closed after."""
        from FareBeep.suppliers import get_inventory_client

        async def _search_and_close():
            sky = get_inventory_client()
            try:
                return await sky.search_offers(origin, destination, flight_date)
            finally:
                await sky.close()

        return _run_off_loop(_search_and_close())

    def fetch(self, origin: str, destination: str,
              flight_date: str) -> Optional[dict]:
        """Cheapest BOOKABLE supplier fare, or None when the supplier has
        nothing usable. Only offers carrying a booking_token count -
        without one the fare can be quoted but never priced or reserved."""
        from FareBeep.suppliers import pick_cheapest
        try:
            offers = self._offers(origin, destination, flight_date)
        except Exception as e:
            logger.warning("Supplier search failed %s->%s on %s: %s",
                           origin, destination, flight_date, e)
            return None
        best = pick_cheapest(offers)
        return _offer_to_fare(best) if best is not None else None

    def fetch_list(self, origin: str, destination: str,
                   flight_date: str, limit: int = 3) -> list:
        """Top-N cheapest supplier fares, ONE per airline, ranked ascending.

        Every offer (bookable or not) may appear - the ranked reply is
        honest about what the supplier returns; the BOOK handshake still
        re-verifies live before money moves."""
        try:
            offers = self._offers(origin, destination, flight_date)
        except Exception as e:
            logger.warning("Supplier search failed %s->%s on %s: %s",
                           origin, destination, flight_date, e)
            return []
        fares = [_offer_to_fare(o) for o in offers
                 if o.get("price") is not None]
        fares.sort(key=lambda f: f["price"])
        # ONE result per airline (the cheapest): the ranked reply should read
        # like a person - "Air Peace ₦X, Rano Air ₦Y" - not three flights on
        # the same airline. When only one airline serves the route this
        # naturally collapses to a single fare (the classic reply).
        seen = {}
        for f in fares:
            seen.setdefault((f["airline"] or "Unknown").lower(), f)
        fares = [seen[k] for k in seen]
        fares.sort(key=lambda f: f["price"])
        return fares[:max(1, limit)]


# ---------------------------------------------------------------------------
# The shared-ledger search service (the 6-step flow)
# ---------------------------------------------------------------------------
class LedgerOnlyEngine:
    """Probe engine: fetch() always returns None, so LedgerSearch.search()
    answers from the Shared Ledger or reports a miss - the caller then
    serves the miss live itself (the supplier clients in /tools/search,
    the Groq agent, and SupplierLiveEngine for the chat paths)."""

    def fetch(self, origin, destination, flight_date):
        return None

    def fetch_list(self, origin, destination, flight_date, limit=3):
        return []


class LedgerSearch:
    """The single entry point for every fare request.

    Guarantee: the database is ALWAYS consulted before the API.
    (Verified by tests/test_search_db_first.py)

    ledger_ttl_minutes: explicit freshness window for tests/ops. None ->
    get_ledger_ttl(origin, destination) - the dynamic 8/15-minute rule.
    """

    def __init__(self, db: Session, live: Callable = None,
                 ledger_ttl_minutes: int = None, clock: Callable = None,
                 price_guardrail: float = None):
        self.db = db
        self.live = live or SupplierLiveEngine()
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
        if self.ledger_ttl_minutes is not None:
            ttl = self.ledger_ttl_minutes
        else:
            ttl = get_adaptive_ttl(self.db, origin, destination,
                                   flight_date, self.clock)
        cutoff = self.clock() - timedelta(minutes=ttl)
        return (
            self.db.query(FareLedger)
            .filter(FareLedger.origin == origin,
                    FareLedger.destination == destination,
                    FareLedger.flight_date == flight_date,
                    FareLedger.last_updated > cutoff)
            .first()
        )

    # -- dateless questions: cheapest known fare in the window ------------
    def window_cheapest(self, origin: str, destination: str,
                        days: int = 14) -> Optional[dict]:
        """Cheapest FRESH ledger fare for any flight_date in
        [today, today + days].

        Answers "how much is Lagos to Abuja" instantly from known data -
        no live calls, no waiting, no date interrogation. The caller asks
        for a date only when it needs a live check or a booking.

        Returns {price, currency, airline, flight_date, verify_link,
        source, above_guardrail} like search(), or None when the ledger
        knows nothing on this route.
        """
        self.call_order.append("ledger")
        if self.ledger_ttl_minutes is not None:
            ttl = self.ledger_ttl_minutes
        else:
            # Window answers are best-known, not date-pinned: no proximity
            # layer (None date), but watchers + season still apply.
            ttl = get_adaptive_ttl(self.db, origin, destination,
                                   None, self.clock)
        cutoff = self.clock() - timedelta(minutes=ttl)
        today = self.clock().strftime("%Y-%m-%d")
        end = (self.clock() + timedelta(days=max(days, 1))).strftime("%Y-%m-%d")
        row = (
            self.db.query(FareLedger)
            .filter(FareLedger.origin == origin,
                    FareLedger.destination == destination,
                    FareLedger.flight_date >= today,
                    FareLedger.flight_date <= end,
                    FareLedger.last_updated > cutoff)
            .order_by(FareLedger.price.asc())
            .first()
        )
        if row is None:
            return None
        # NOTE: the live fare_ledger.flight_date column is Postgres DATE
        # while the model declares String - rows come back as
        # datetime.date, which is not JSON-serializable. Coerce here so
        # every caller gets the "YYYY-MM-DD" string contract.
        return {
            "price": row.price,
            "currency": row.currency,
            "airline": row.airline,
            "flight_date": str(row.flight_date)[:10],
            "verify_link": row.verify_link,
            "source": "ledger",
            "checked_at": _iso_instant(row.last_updated),
            "above_guardrail": row.price > self.price_guardrail,
        }

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

    # -- step 6b: the low-price honesty gate --------------------------------
    def _previous_fare(self, origin: str, destination: str,
                       flight_date: str) -> Optional[FareLedger]:
        """The last known price for this route+date - EVEN if it has expired
        (a stale row is still the best anomaly baseline)."""
        return (self.db.query(FareLedger)
                .filter(FareLedger.origin == origin,
                        FareLedger.destination == destination,
                        FareLedger.flight_date == flight_date)
                .first())

    def _verify_and_upsert(self, origin: str, destination: str,
                           flight_date: str, fare: dict,
                           verify: bool = True) -> dict:
        """Upsert `fare` unless it is a suspiciously LOW anomaly.

        A price more than ANOMALY_DROP_THRESHOLD below the previous row
        for the same route+date (airlines pull glitch fares back within
        ~3 minutes) is re-checked after ANOMALY_RECHECK_WAIT_SECONDS: a
        second live call within CONFIRMATION_TOLERANCE makes the low
        price real; a bounce back discards it and the ledger takes the
        higher result instead. First-ever rows and surge prices skip the
        re-check entirely.

        verify=False skips ONLY the 90s hold (browse paths: chat, voice,
        ranked lists - the BOOK handshake always re-verifies before
        money moves). The surge skip always applies.

        Returns the fare that is actually true (possibly the second,
        higher one) so the caller quotes what was cached.
        """
        if fare["price"] > self.price_guardrail:
            logger.warning(
                "Surge price %s->%s on %s = NGN %.2f - NOT upserted into "
                "the shared ledger", origin, destination, flight_date,
                fare["price"])
            return fare

        prev = self._previous_fare(origin, destination, flight_date)
        if (verify and prev is not None
                and fare["price"] < prev.price * (1.0 - ANOMALY_DROP_THRESHOLD)):
            logger.warning(
                "Suspicious low price %s->%s on %s = NGN %s vs previous "
                "NGN %s - re-verifying in %ss",
                origin, destination, flight_date, fare["price"], prev.price,
                ANOMALY_RECHECK_WAIT_SECONDS)
            time.sleep(ANOMALY_RECHECK_WAIT_SECONDS)
            second = self.live.fetch(origin, destination, flight_date)
            if second is None:
                logger.warning(
                    "Low-price re-check failed for %s->%s on %s - the "
                    "unverified price is NOT cached", origin, destination,
                    flight_date)
                return fare
            if second["price"] <= fare["price"] * (1.0 + CONFIRMATION_TOLERANCE):
                logger.info("Low price CONFIRMED %s->%s on %s = NGN %s",
                            origin, destination, flight_date, fare["price"])
            else:
                logger.info("Low price BOUNCED %s->%s on %s: NGN %s -> NGN %s "
                            "- discarding the glitch",
                            origin, destination, flight_date, fare["price"],
                            second["price"])
                fare = second

        self._ledger_upsert(origin, destination, flight_date, fare["price"],
                            fare["currency"], fare.get("airline"),
                            fare.get("verify_link"))
        logger.info("LEDGER MISS -> UPSERTED %s->%s on %s = %s",
                    origin, destination, flight_date, fare["price"])
        return fare

    # -- the 6-step flow -----------------------------------------------------
    def search(self, origin, destination, flight_date,
               force_refresh: bool = False, verify: bool = True) -> Optional[dict]:
        """Resolve a fare for (origin, destination, date). Ledger first.

        force_refresh = True (the BOOK handshake): the Shared Ledger is
        IGNORED and the engine is queried LIVE. "Ensure the seat still
        exists at the quoted price" - the settlement brief. The result is
        still UPSERTed when sane, so the community ledger benefits too.

        verify = False skips ONLY the 90s low-anomaly hold (chat/voice
        browse paths - nobody waits 90s mid-conversation; the BOOK
        handshake always re-verifies before money moves). Surge prices
        are never cached either way.

        Returns:
            {price, currency, airline, flight_date, verify_link, source}
            where source == "ledger" (step 3 hit) or "live" (step 4 miss),
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
                    # ISO instant the fare was last checked (drives the
                    # "cached ~N min ago" label - never present a ledger
                    # fare as freshly checked).
                    "checked_at": _iso_instant(cached.last_updated),
                    "above_guardrail": cached.price > self.price_guardrail,
                }

        # step 4: the miss -> the live supplier (Travels247/QuickAir)
        result = self.live.fetch(o, d, date_str)
        if result is None:
            logger.info("No live fare for %s->%s on %s", o, d, date_str)
            return None

        # step 6: ledger update - the community benefit. The surge filter
        # (high anomalies) and _verify_and_upsert (low anomalies) live
        # here, so a broken number can never poison the ledger.
        result = self._verify_and_upsert(o, d, date_str, result,
                                         verify=verify)
        return {**result, "flight_date": date_str, "source": "live",
                "checked_at": _iso_instant(self.clock()),
                "above_guardrail": result["price"] > self.price_guardrail}

    def search_list(self, origin, destination, flight_date,
                    limit: int = 3) -> tuple:
        """Ranked fare list for the "reply 1, 2 or 3" flow.

        Live-fetched (the ledger caches one price, not a list - a list-cache
        column is a flagged follow-up). The cheapest SANE fare is still
        upserted so the community ledger benefits.

        Returns (fares, surge_fares): `fares` is the ranked list of sane
        (<= guardrail) fares, sorted by price; `surge_fares` are the results
        above the guardrail (with prices, so the bot can say "prices are
        unusually high from ₦X" instead of "no fares"). Never raises.
        """
        o = resolve_iata(origin)
        d = resolve_iata(destination)
        if not o or not d:
            return [], []
        date_str = _as_date_str(flight_date)
        try:
            fares = self.live.fetch_list(o, d, date_str, limit=limit)
        except Exception as e:
            # A broken injected engine must degrade to "no fares" in the
            # chat, never a crashed turn (SupplierLiveEngine already
            # swallows its own supplier failures).
            logger.warning("search_list failed (%s) - returning empty", e)
            return [], []
        sane = [f for f in fares if f["price"] <= self.price_guardrail]
        surge = [f for f in fares if f["price"] > self.price_guardrail]
        checked_at = _iso_instant(self.clock())
        for f in sane:
            f["flight_date"] = date_str
            # Live-fetched seconds ago (the ledger caches one price, not
            # lists): stamp it so replies can say "checked just now".
            f.setdefault("checked_at", checked_at)
            f.setdefault("source", "live")
        if sane:
            best = sane[0]
            try:
                # Browse path: cache sane prices WITHOUT the 90s anomaly
                # hold (a ranked list must answer now; the BOOK handshake
                # re-verifies before money moves). Surge still skipped.
                self._verify_and_upsert(o, d, date_str, best, verify=False)
            except Exception:
                logger.exception("search_list: ledger upsert failed")
        return sane, surge


def _is_postgres(db) -> bool:
    """True when the session is bound to the Postgres Shared Ledger
    (driver psycopg2 / dialect postgresql)."""
    return db.get_bind().dialect.name == "postgresql"


def _as_date_str(flight_date) -> str:
    """Accept a datetime, date, or "YYYY-MM-DD"; return "YYYY-MM-DD".
    A missing date (user didn't say when) defaults to today in
    Africa/Lagos - never crash, never silently UTC."""
    if flight_date in (None, ""):
        from FareBeep.dates import lagos_today
        return lagos_today().isoformat()
    if isinstance(flight_date, str):
        return flight_date[:10]
    if isinstance(flight_date, datetime):
        return flight_date.strftime("%Y-%m-%d")
    return flight_date.strftime("%Y-%m-%d")  # datetime.date


def _iso_instant(value) -> Optional[str]:
    """Storage datetimes (naive UTC on SQLite, aware on Postgres) and
    live clock readings -> ISO strings. Fare dicts travel through JSON
    (chat_state, tool results), so datetimes must never ride along raw."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return value.isoformat()
    except Exception:
        return None


def _checked_age_minutes(checked_at, now=None) -> Optional[int]:
    """Whole minutes since checked_at (ISO string or datetime). None when
    unknown/unparseable - callers then claim nothing about freshness."""
    if not checked_at:
        return None
    try:
        from datetime import timezone
        ts = (datetime.fromisoformat(str(checked_at))
              if isinstance(checked_at, str) else checked_at)
        ref = now or datetime.now(timezone.utc).replace(tzinfo=None)
        if (ts.tzinfo is None) != (ref.tzinfo is None):
            # Storage is naive UTC, Postgres reads aware: normalize to
            # naive (both sides are UTC by convention).
            ts = ts.replace(tzinfo=None)
            ref = ref.replace(tzinfo=None)
        return max(0, int((ref - ts).total_seconds() // 60))
    except Exception:
        return None


def fare_freshness(fare: dict, now=None) -> str:
    """One-line honesty label for a quoted fare. Live engine results say
    "checked just now"; ledger rows say "cached ~N min ago" from their
    checked_at; anything without provenance says "" (claim nothing)."""
    src = ((fare or {}).get("source") or "")
    if src in ("live", "247travels"):
        return "checked just now"
    if src == "ledger":
        mins = _checked_age_minutes((fare or {}).get("checked_at"), now)
        if mins is None:
            return "cached fare"
        if mins < 1:
            return "checked just now"
        return f"cached ~{mins} min ago"
    return ""
