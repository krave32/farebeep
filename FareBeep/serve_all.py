"""FareBeep all-in-one - web + worker + poller in ONE process.

The Railway-simple mode: one service, one command, everything runs together.
The worker and poller loops run as daemon threads alongside uvicorn. A
Postgres advisory lock (`pg_try_advisory_lock`) guarantees that even with
multiple web replicas only ONE of them runs the loops - the rest are pure
API.

Run:  python -m FareBeep.serve_all      (Railway `web`; PORT env honored)
Local: PORT=8001 python -m FareBeep.serve_all
"""
import logging
import os
import sys
import threading

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("farebeep.serve_all")

from sqlalchemy import text

from FareBeep.config import TELEGRAM_BOT_TOKEN
from FareBeep.database import SessionLocal, init_db, verify_connection
from FareBeep.models import Base

ADVISORY_LOCK_KEY = 8391028


def _leader_lock():
    """Try to become the loops leader. Returns a held connection or None.

    pg_try_advisory_lock never blocks; the lock dies with the connection,
    so a crashed leader releases it automatically."""
    conn = SessionLocal().connection()
    try:
        if conn.execute(text("select pg_try_advisory_lock(:k)"),
                        {"k": ADVISORY_LOCK_KEY}).scalar():
            return conn
    except Exception:
        logger.exception("Advisory lock check failed")
    try:
        conn.close()
    except Exception:
        pass
    return None


def _run_worker():
    from FareBeep.worker import main as worker_main
    try:
        worker_main()
    except Exception as e:
        logger.error("Worker thread died: %s", e)


def _run_poller():
    if not TELEGRAM_BOT_TOKEN:
        logger.info("No TELEGRAM_BOT_TOKEN - poller thread skipped")
        return
    from FareBeep.poller import main as poller_main
    try:
        poller_main()
    except Exception as e:
        logger.error("Poller thread died: %s", e)


def main():
    init_db(Base)
    if not verify_connection():
        sys.exit(1)

    leader = _leader_lock()
    if leader is not None:
        logger.info("All-in-one: holding loops lock (worker + poller active)")
        threading.Thread(target=_run_worker, daemon=True,
                         name="farebeep-worker").start()
        threading.Thread(target=_run_poller, daemon=True,
                         name="farebeep-poller").start()
    else:
        logger.info("All-in-one: another replica owns the loops - web only")

    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("FareBeep.main:app", host="0.0.0.0", port=port,
                log_level="info")


if __name__ == "__main__":
    main()
