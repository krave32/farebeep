"""CLOUD DATABASE MIGRATION - Postgres (Supabase) specific behaviour:
- psycopg2 driver normalization on every Postgres URL scheme
- fare_ledger UPSERT compiles to INSERT ... ON CONFLICT (one round-trip)
- SQLite fallback path stays select-then-write (no ON CONFLICT)
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql

from FareBeep import database
from FareBeep.search import LedgerSearch


# ---------------------------------------------------------------------------
# make_engine / URL normalization
# ---------------------------------------------------------------------------
def test_normalize_postgres_url_schemes():
    for raw in ("postgres://u:p@host/db",
                "postgresql://u:p@host/db",
                "postgresql+psycopg2://u:p@host/db"):
        assert database._normalize_postgres_url(raw) == \
            "postgresql+psycopg2://u:p@host/db"


def test_normalize_leaves_sqlite_alone():
    assert database._normalize_postgres_url(
        "sqlite:///tmp/x.db") == "sqlite:///tmp/x.db"


def test_make_engine_uses_psycopg2_driver():
    engine = database.make_engine("postgres://u:p@host/db", "Supabase")
    assert engine.url.drivername == "postgresql+psycopg2"


def test_make_engine_cloud_pool_settings():
    engine = database.make_engine("postgresql+psycopg2://u:p@host/db",
                                  "Supabase")
    assert engine.pool.size() == database._POOL_SIZE
    assert engine.pool._max_overflow == database._POOL_MAX_OVERFLOW
    assert engine.pool._pre_ping is True


# ---------------------------------------------------------------------------
# fare_ledger UPSERT - Postgres ON CONFLICT vs SQLite fallback
# ---------------------------------------------------------------------------
class _FakeRow:
    origin = "LOS"
    destination = "ABV"
    flight_date = "2026-08-20"


class _FakeQuery:
    def filter(self, *a, **k):
        return self

    def first(self):
        return _FakeRow()


class _FakePGSession:
    """Session stand-in: postgres dialect bind, records the executed stmt."""

    def __init__(self):
        self.executed = None

    def get_bind(self):
        return create_engine("postgresql+psycopg2://u:p@host/db")

    def execute(self, stmt):
        self.executed = stmt

    def commit(self):
        pass

    def query(self, *a, **k):
        return _FakeQuery()


def test_postgres_upsert_uses_on_conflict():
    fake = _FakePGSession()
    ledger = LedgerSearch(fake, clock=lambda: datetime(2026, 8, 13, 12, 0))
    row = ledger._ledger_upsert("LOS", "ABV", "2026-08-20", 112575.0,
                                "NGN", "Air Peace", "https://x/f")
    assert row.origin == "LOS"
    sql = str(fake.executed.compile(dialect=postgresql.dialect()))
    assert "INSERT INTO fare_ledger" in sql
    assert "ON CONFLICT" in sql
    assert "(origin, destination, flight_date)" in sql
    assert "DO UPDATE" in sql


def test_sqlite_upsert_still_works_without_on_conflict(db):
    """SQLite fallback must keep the select-then-write semantics (SQLite has
    no ON CONFLICT in this shape) - proven by the existing in-memory DB."""
    ledger = LedgerSearch(db, clock=lambda: datetime(2026, 8, 13, 12, 0))
    from FareBeep.models import FareLedger
    row = ledger._ledger_upsert("LOS", "ABV", "2026-08-20", 112575.0,
                                "NGN", "Air Peace", "https://x/f")
    assert row.id is not None
    # second upsert overwrites, still one row
    ledger._ledger_upsert("LOS", "ABV", "2026-08-20", 98000.0,
                          "NGN", "Air Peace", "https://x/f")
    rows = db.query(FareLedger).filter(
        FareLedger.origin == "LOS", FareLedger.destination == "ABV",
        FareLedger.flight_date == "2026-08-20").all()
    assert len(rows) == 1
    assert rows[0].price == 98000.0