"""FX price-tracking snapshots (worker.record_fx_rate)."""
from datetime import datetime, timedelta

import pytest

from FareBeep import worker
from FareBeep.models import FxRate, utcnow


@pytest.fixture
def fresh_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from FareBeep.models import Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


@pytest.fixture
def rate_ok(monkeypatch):
    monkeypatch.setattr("FareBeep.search.fetch_usd_ngn",
                        lambda http_client=None: 1360.25)


@pytest.fixture
def rate_down(monkeypatch):
    monkeypatch.setattr("FareBeep.search.fetch_usd_ngn",
                        lambda http_client=None: None)


def test_records_snapshot_when_stale(fresh_db, rate_ok):
    rate = worker.record_fx_rate(db=fresh_db)
    assert rate == 1360.25
    rows = fresh_db.query(FxRate).all()
    assert len(rows) == 1
    assert rows[0].usd_ngn == 1360.25
    assert rows[0].source == "open.er-api.com"


def test_skips_snapshot_when_fresh_enough(fresh_db, rate_ok):
    fresh_db.add(FxRate(usd_ngn=1425.0, source="open.er-api.com",
                        fetched_at=utcnow()))
    fresh_db.commit()
    rate = worker.record_fx_rate(db=fresh_db)
    assert rate is None
    assert fresh_db.query(FxRate).count() == 1


def test_retries_after_ttl(fresh_db, rate_ok):
    fresh_db.add(FxRate(usd_ngn=1425.0, source="open.er-api.com",
                        fetched_at=utcnow() - timedelta(hours=13)))
    fresh_db.commit()
    rate = worker.record_fx_rate(db=fresh_db)
    assert rate == 1360.25
    assert fresh_db.query(FxRate).count() == 2


def test_api_failure_skips_without_row(fresh_db, rate_down):
    rate = worker.record_fx_rate(db=fresh_db)
    assert rate is None
    assert fresh_db.query(FxRate).count() == 0