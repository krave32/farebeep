"""FareBeep worker - the background loops that keep the state machine honest.

Two run modes:

  default (poll loop)   - `python -m FareBeep.worker`
     1. Booking sweep     - pending booking_sessions past expires_at -> expired
     2. Status Beep cycle - open 3-hour status watches -> aviationstack ->
                            proactive WhatsApp template on status change
     3. Fare Beep cycle   - subscriptions -> ledger -> WhatsApp alert on a
                            target-price hit or >10% drop (alerts.py)

  --scheduled (APScheduler, ported from naijafly v1's fare_worker) - the
    TRACKING checks run every TRACKING_POLL_HOURS (default 4h), and the
    fast sweep (bookings + status watches) keeps its own short interval:
     `python -m FareBeep.worker --scheduled`

Run:  python -m FareBeep.worker        (from the parent of FareBeep/)
or:   python -c "import FareBeep.worker; FareBeep.worker.main()"
"""
import logging
import sys
import time
from typing import Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("farebeep.worker")

from FareBeep.config import (FX_RATE_TTL_HOURS, STATUS_POLL_SECONDS,  # noqa: E402
                             STATUS_WATCH_LEAD_HOURS, TRACKING_POLL_HOURS)
from FareBeep.database import SessionLocal, init_db  # noqa: E402


def record_fx_rate(db=None) -> Optional[float]:
    """PRICE TRACKING snapshot: fetch the official USD->NGN rate and persist
    an fx_rates row unless the last snapshot is fresher than
    FX_RATE_TTL_HOURS. Returns the recorded rate (None = skipped/down).

    The row history is the founder's view of naira movement - the 'price
    tracking' half of the FX strategy; ngn_per_usd() uses the live rate."""
    from datetime import datetime, timedelta

    from FareBeep.config import FX_RATE_TTL_HOURS
    from FareBeep.models import FxRate
    from FareBeep.search import fetch_usd_ngn

    db = db or SessionLocal()
    try:
        latest = db.query(FxRate) \
            .order_by(FxRate.fetched_at.desc()).first()
        if latest is not None and \
                latest.fetched_at > datetime.utcnow() - timedelta(hours=FX_RATE_TTL_HOURS):
            return None
        rate = fetch_usd_ngn()
        if rate is None:
            logger.warning("FX snapshot skipped: rate API unavailable")
            return None
        db.add(FxRate(usd_ngn=rate, source="open.er-api.com",
                      fetched_at=datetime.utcnow()))
        db.commit()
        logger.info("FX snapshot recorded: USD->NGN %s", rate)
        return rate
    except Exception as e:
        logger.error("FX snapshot failed: %s", e)
        return None
    finally:
        db.close()


def run_cycles(notifier=None, api=None) -> dict:
    """One pass of every housekeeping loop; returns counts for tests/monitoring."""
    from FareBeep.alerts import SubscriptionMonitor
    from FareBeep.config import META_TEMPLATE_FLIGHT_STATUS
    from FareBeep.notifier import get_notifier
    from FareBeep.status import AviationstackClient, StatusService
    from FareBeep.transactions import BookingService

    db = SessionLocal()
    try:
        bookings = BookingService(db)
        expired = bookings.expire_stale_sessions()

        status = StatusService(
            db,
            api=api or AviationstackClient(),
            notifier=notifier or get_notifier(),
            template_name=META_TEMPLATE_FLIGHT_STATUS,
        )
        pushed = status.run_watch_cycle()

        beeps = SubscriptionMonitor(db, notifier=notifier or get_notifier()) \
            .run_cycle()
        logger.info("Worker cycle: %d session(s) expired, %d status push(es), "
                    "%d fare beep(s)", expired, pushed, beeps)
        return {"expired": expired, "status_pushes": pushed, "fare_beeps": beeps}
    finally:
        db.close()


def run_fare_cycle(notifier=None) -> int:
    """The TRACKING check: scan subscriptions and Beep on target hits / drops.
    Scheduled every TRACKING_POLL_HOURS in --scheduled mode."""
    from FareBeep.alerts import SubscriptionMonitor
    from FareBeep.notifier import get_notifier

    db = SessionLocal()
    try:
        beeps = SubscriptionMonitor(db, notifier=notifier or get_notifier()) \
            .run_cycle()
        logger.info("Fare Beep cycle: %d beep(s)", beeps)
        return beeps
    finally:
        db.close()


def build_scheduler():
    """APScheduler config (ported from naijafly v1 fare_worker.py) - the
    tracking checks every 4h, the sweep/status loops stay fast."""
    from apscheduler.schedulers.blocking import BlockingScheduler

    scheduler = BlockingScheduler()
    scheduler.add_job(run_fare_cycle, "interval",
                      hours=TRACKING_POLL_HOURS, next_run_time=time_now())
    scheduler.add_job(record_fx_rate, "interval",
                      hours=FX_RATE_TTL_HOURS, next_run_time=time_now())
    scheduler.add_job(run_cycles, "interval", seconds=STATUS_POLL_SECONDS)
    return scheduler


def time_now():
    from datetime import datetime
    return datetime.now()


def main_scheduled():
    from FareBeep.database import verify_connection
    from FareBeep.models import Base
    init_db(Base)
    verify_connection()
    logger.info("FareBeep worker (APScheduler): tracking Beeps every %sh, "
                "sweep+status every %ss", TRACKING_POLL_HOURS, STATUS_POLL_SECONDS)
    build_scheduler().start()


def main():
    from FareBeep.database import verify_connection
    from FareBeep.models import Base
    init_db(Base)
    verify_connection()
    logger.info("FareBeep worker started (poll=%ss, watch lead=%sh)",
                STATUS_POLL_SECONDS, STATUS_WATCH_LEAD_HOURS)
    record_fx_rate()   # first snapshot straight away
    while True:
        try:
            run_cycles()
        except Exception as e:
            logger.error("Worker cycle failed: %s", e)
        record_fx_rate()   # TTL-guarded: at most one row per FX_RATE_TTL_HOURS
        time.sleep(STATUS_POLL_SECONDS)


if __name__ == "__main__":
    if "--scheduled" in sys.argv:
        main_scheduled()
    else:
        main()
