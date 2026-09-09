"""ROUTE WARMER - keep top-route fares fresh so answers are instant.

The ledger only learns when a human asks; unwatched routes go stale and
every question costs a live call. The warmer re-searches the busiest
routes on a timer and upserts the cheapest sane fare per route+date.

247travels ONLY (single-source decision): no SerpApi, no scraping here.
Surge prices are skipped, never cached; no 90s anomaly hold on this
path (browse-speed - the BOOK handshake re-verifies before money).

Cost: len(routes) x len(days) live searches per cycle. Tune
WARM_ROUTES / WARM_DAYS_AHEAD / WARM_INTERVAL_MINUTES to the API
budget. Subscriber routes join automatically (their watchers deserve
fresh baselines).
"""
import asyncio
import logging
from datetime import datetime, timedelta

logger = logging.getLogger("farebeep.warmer")

# Busiest corridors first (FAAN 2025 + MMA2 ops data), then Kano/Enugu.
# Directed both ways: returns matter as much as outbound legs.
WARM_ROUTES = [
    ("LOS", "ABV"), ("ABV", "LOS"),
    ("LOS", "PHC"), ("PHC", "LOS"),
    ("ABV", "PHC"), ("PHC", "ABV"),
    ("LOS", "ABB"), ("ABB", "LOS"),
    ("LOS", "KAN"), ("KAN", "LOS"),
    ("LOS", "ENU"), ("ENU", "LOS"),
]
WARM_DAYS_AHEAD = (1, 3, 7)

# Process-local gate: the worker calls run_warmer_if_due() every cycle,
# the actual warm run happens at most once per interval.
_last_warm = None


def _warm_interval_minutes() -> int:
    from FareBeep.config import WARM_INTERVAL_MINUTES
    try:
        return max(1, int(WARM_INTERVAL_MINUTES))
    except (TypeError, ValueError):
        return 15


def _subscriber_routes(db) -> list:
    """Distinct (origin, destination) pairs with active watchers."""
    from FareBeep.models import Subscription
    try:
        rows = (db.query(Subscription.origin, Subscription.destination)
                .distinct().all())
    except Exception:
        logger.warning("Warmer: could not read subscriptions")
        return []
    return [(o.upper(), d.upper()) for o, d in rows if o and d]


async def _warm_once(client, ledger, jobs) -> dict:
    """One live search per (route, date); cheapest sane fare upserted.

    Surge prices are skipped (never cached), misses and errors counted.
    No anomaly hold: browse-speed caching, BOOK re-verifies anyway.
    """
    from FareBeep.travels247 import pick_cheapest
    stats = {"upserted": 0, "skipped_surge": 0, "misses": 0, "errors": 0}
    for origin, destination, flight_date in jobs:
        try:
            offers = await client.search_offers(
                origin, destination, flight_date)
        except Exception as e:
            logger.warning("Warmer miss %s->%s %s: %s",
                           origin, destination, flight_date, e)
            stats["errors"] += 1
            continue
        best = pick_cheapest(offers or [])
        if best is None:
            stats["misses"] += 1
            continue
        if best["price"] > ledger.price_guardrail:
            logger.warning("Warmer skipping surge %s->%s %s = NGN %s",
                           origin, destination, flight_date, best["price"])
            stats["skipped_surge"] += 1
            continue
        ledger._ledger_upsert(
            origin, destination, flight_date, best["price"],
            best.get("currency") or "NGN", best.get("airline_name"), None)
        stats["upserted"] += 1
    return stats


def warm_ledger(db=None, routes=None, days_ahead=None, client=None,
                clock=None) -> dict:
    """Refresh fares for routes x days. Returns counts; never raises.

    A single asyncio.run covers the whole batch (one event loop for all
    searches AND the client close - see agent.py _search_and_close for
    why two loops crash).
    """
    from FareBeep.database import SessionLocal
    from FareBeep.models import utcnow
    from FareBeep.search import LedgerOnlyEngine, LedgerSearch
    from FareBeep.travels247 import Travels247Client

    own_db = db is None
    db = db or SessionLocal()
    now = (clock or utcnow)()
    today = now.date() if isinstance(now, datetime) else now
    days = tuple(days_ahead) if days_ahead else WARM_DAYS_AHEAD
    wanted = list(routes) if routes else list(WARM_ROUTES)
    for sub in _subscriber_routes(db):
        if sub not in wanted:
            wanted.append(sub)
    dates = [(today + timedelta(days=n)).strftime("%Y-%m-%d") for n in days]
    jobs = [(o, d, dt) for o, d in wanted for dt in dates]

    ledger = LedgerSearch(db, live=LedgerOnlyEngine())
    own_client = client is None
    client = client or Travels247Client()
    stats = {"routes": len(wanted), "dates": list(dates), "upserted": 0,
             "skipped_surge": 0, "misses": 0, "errors": 0}

    async def _run():
        try:
            await client.login()
        except Exception as e:
            logger.error("Warmer aborted: 247travels login failed: %s", e)
            stats["errors"] = len(jobs)
            return stats
        try:
            result = await _warm_once(client, ledger, jobs)
            stats.update(result)
        finally:
            try:
                await client.close()
            except Exception:
                pass
        return stats

    try:
        return asyncio.run(_run())
    finally:
        if own_db:
            try:
                db.close()
            except Exception:
                pass


def run_warmer_if_due(db=None, client=None, clock=None):
    """Worker entry: warm at most once per WARM_INTERVAL_MINUTES.

    Returns the warm_ledger() stats dict, or None when the interval has
    not elapsed yet. Process-local gate (single runner enforced by the
    serve_all loops lock in production).
    """
    from FareBeep.models import utcnow
    global _last_warm
    now = (clock or utcnow)()
    if _last_warm is not None:
        elapsed = (now - _last_warm).total_seconds() / 60.0
        if elapsed < _warm_interval_minutes():
            return None
    stats = warm_ledger(db=db, client=client, clock=clock)
    _last_warm = now
    logger.info("Warmer cycle: %s", stats)
    return stats


if __name__ == "__main__":
    """One-shot warm: python -m FareBeep.warmer (Railway one-off dyno,
    local backfill). Not interval-gated - warms immediately."""
    from FareBeep.database import init_db
    from FareBeep.models import Base
    init_db(Base)
    print(warm_ledger())
