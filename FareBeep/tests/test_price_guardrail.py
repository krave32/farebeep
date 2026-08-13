"""PRICE GUARDRAIL + NIGERIA-FIRST LINKS - the sanity layer for fares."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from FareBeep.models import Base, FareLedger, utcnow
from FareBeep.search import LedgerSearch, _ngn_verify_link


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session


def _fake_live(price_ngn, link):
    class FakeLive:
        def __init__(self):
            self.calls = 0

        def fetch(self, origin, destination, flight_date):
            self.calls += 1
            return {"price": price_ngn, "currency": "NGN",
                    "airline": "Test Air", "verify_link": link}

    return FakeLive()


def _search(db, live, guardrail=250000.0):
    return LedgerSearch(db, live=live, clock=utcnow, price_guardrail=guardrail)


def test_normal_fare_not_flagged(session_factory):
    db = session_factory()
    live = _fake_live(118500.0, "https://x.google.com/1")
    result = _search(db, live).search("LOS", "ABV", "2026-08-14")
    assert result["price"] == 118500.0
    assert result["above_guardrail"] is False


def test_anomalous_fare_flagged(session_factory):
    db = session_factory()
    live = _fake_live(660000.0, "https://x.google.com/2")
    result = _search(db, live).search("LOS", "AKR", "2026-08-14")
    assert result["above_guardrail"] is True


def test_ledger_hit_keeps_guardrail_flag(session_factory):
    db = session_factory()
    row = FareLedger(origin="LOS", destination="AKR", flight_date="2026-08-14",
                     price=660000.0, currency="NGN", airline="Test Air",
                     verify_link="https://x.google.com/2", last_updated=utcnow())
    db.add(row)
    db.commit()
    # seed the ledger row first so search() returns a HIT (no live call)
    live = _fake_live(0.0, "unused")
    result = _search(db, live).search("LOS", "AKR", "2026-08-14")
    assert result["source"] == "ledger"
    assert result["above_guardrail"] is True
    assert live.calls == 0


def test_guardrail_is_exclusive_above_not_at(session_factory):
    db = session_factory()
    live = _fake_live(250000.0, "https://x.google.com/3")
    result = _search(db, live).search("LOS", "ABV", "2026-08-14")
    assert result["above_guardrail"] is False


def test_surge_price_never_enters_the_shared_ledger(session_factory):
    """Sanity-first ledger: an anomalous fare must NOT be cached - otherwise
    every user for the next 20 minutes gets quoted the broken number."""
    db = session_factory()
    live = _fake_live(660000.0, "https://x.google.com/2")
    result = _search(db, live).search("LOS", "AKR", "2026-08-14")
    assert result["above_guardrail"] is True
    assert db.query(FareLedger).count() == 0


def test_sane_price_enters_the_shared_ledger(session_factory):
    db = session_factory()
    live = _fake_live(118500.0, "https://x.google.com/1")
    result = _search(db, live).search("LOS", "ABV", "2026-08-14")
    assert result["above_guardrail"] is False
    row = db.query(FareLedger).first()
    assert row is not None and row.price == 118500.0


def test_link_currency_rewritten_to_ngn():
    link = "https://www.google.com/travel/flights?hl=en&gl=ng&curr=USD&tfs=ABC123"
    out = _ngn_verify_link(link)
    assert "curr=NGN" in out
    assert "curr=USD" not in out
    assert "tfs=ABC123" in out


def test_link_without_currency_gets_ngn_appended():
    out = _ngn_verify_link("https://www.google.com/travel/flights?hl=en")
    assert out.endswith("&curr=NGN")


def test_link_other_currency_rewritten():
    out = _ngn_verify_link("https://www.google.com/travel/flights?curr=EUR&hl=en")
    assert "curr=NGN" in out
    assert "curr=EUR" not in out


def test_empty_link_passthrough():
    assert _ngn_verify_link("") == ""
    assert _ngn_verify_link(None) is None
