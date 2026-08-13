"""ONE-SHOT migration: copy local SQLite data into the Supabase Shared Ledger.

    python migrate_local_to_supabase.py

Reads every row from FareBeep/farebeep_local.db and writes it into the
database at SUPABASE_DB_URL (psycopg2). Idempotent: rows that already exist
(unique keys) are skipped, so re-running after a fix never duplicates.

Does NOT drop or touch anything on the destination - it only inserts.
Identity-PK tables (subscriptions, fare_ledger, ...) get fresh ids on the
cloud side; uuid-PK tables (users, booking_sessions, status_watches) keep
their ids so relationships survive.
"""
import sys
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.types import Uuid

from FareBeep.config import BASE_DIR, SUPABASE_DB_URL
from FareBeep.models import (BookingSession, FareLedger, FxRate,
                             StatusEvent, StatusWatch, Subscription, User)

IDENTITY_PKS = {"subscriptions", "fare_ledger", "status_events", "fx_rates"}
MODELS = [
    (User, ["user_id"]),
    (Subscription, None),               # no unique constraint in schema.sql
    (FareLedger, ["origin", "destination", "flight_date"]),
    (FxRate, None),
    (BookingSession, ["payment_ref"]),
    (StatusWatch, ["id"]),
    (StatusEvent, ["id"]),          # id regenerates; reruns may duplicate
]

UUID_COLUMNS = [
    c.name for c in BookingSession.__table__.columns if isinstance(c.type, Uuid)
]


def _coerce(row: dict) -> dict:
    """uuid-columns arrive as strings from SQLite; PG needs uuid objects."""
    return {k: (uuid.UUID(str(v)) if k in UUID_COLUMNS and v is not None
                else v) for k, v in row.items()}


def main() -> int:
    if not SUPABASE_DB_URL:
        print("SUPABASE_DB_URL is not set - nothing to migrate to.")
        return 1
    src = create_engine(f"sqlite:///{(BASE_DIR / 'farebeep_local.db').as_posix()}")
    dst = create_engine(SUPABASE_DB_URL, pool_pre_ping=True)

    total = 0
    with src.connect() as s:
        with dst.begin() as d:
            for model, conflict_cols in MODELS:
                table = model.__table__
                rows = [dict(r._mapping) for r in s.execute(select(model))]
                if not rows:
                    print(f"{table.name}: 0 rows (skip)")
                    continue
                if table.name in IDENTITY_PKS:
                    pk = next(c for c in table.columns if c.primary_key)
                    rows = [{k: v for k, v in r.items() if k != pk.name}
                            for r in rows]
                stmt = pg_insert(table).values(
                    [_coerce(r) for r in rows])
                if conflict_cols:
                    stmt = stmt.on_conflict_do_nothing(
                        index_elements=conflict_cols)
                d.execute(stmt)
                total += len(rows)
                print(f"{table.name}: copied {len(rows)} row(s)")
    print(f"Migration complete - {total} row(s) copied to Supabase.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
