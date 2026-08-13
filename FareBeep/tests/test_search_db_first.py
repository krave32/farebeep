"""VERIFICATION - the search flow must ALWAYS check the database before the API.

These tests pin the exact 6-step contract from the reconstruction brief:
  1 request -> 2 ledger check -> 3 hit (<500ms) | 4 miss -> SerpApi ->
  5 normalization (local dict) -> 6 ledger UPSERT (community benefit)
"""
from datetime import timedelta

import pytest

from FareBeep.iata import resolve_iata
from FareBeep.models import FareLedger, User, utcnow
from FareBeep.search import LedgerSearch, SearchError


class FakeLiveApi:
    """Records every call so tests can prove order + count."""

    def __init__(self, fare=None):
        self.fare = fare or {"price": 98000.0, "currency": "NGN",
                             "airline": "Air Peace",
                             "verify_link": "https://google.com/travel/flights?q=x"}
        self.calls = []

    def fetch(self, origin, destination, flight_date):
        self.calls.append((origin, destination, flight_date))
        return self.fare


@pytest.fixture
def search(db):
    live = FakeLiveApi()
    return LedgerSearch(db, live=live, ledger_ttl_minutes=20), live


def test_ledger_is_checked_before_the_api(search):
    """Order guarantee: 'ledger' must precede any 'api' activity."""
    service, _ = search
    service.search("Lagos", "Abuja", "2026-08-20")
    assert service.call_order[0] == "ledger"


def test_miss_then_upsert_means_second_call_is_a_hit(search):
    """Step 4 miss -> step 6 UPSERT -> next request = step 3 hit (no API)."""
    service, live = search
    first = service.search("Abuja", "Port Harcourt", "2026-08-21")
    assert first["source"] == "serpapi"
    assert len(live.calls) == 1

    second = service.search("Abuja", "Port Harcourt", "2026-08-21")
    assert second["source"] == "ledger"
    assert second["price"] == 98000.0
    assert len(live.calls) == 1          # the API was NOT called again


def test_fresh_hit_never_calls_the_api(search):
    """A previously cached route+date returns instantly, zero API spend."""
    service, live = search
    service.search("Lagos", "Kano", "2026-08-22")
    live.calls.clear()
    hit = service.search("Lagos", "Kano", "2026-08-22")
    assert hit["source"] == "ledger"
    assert live.calls == []


def test_stale_ledger_forces_a_miss(search, db):
    """last_updated older than the 20-min TTL -> the engine is called again."""
    service, live = search
    service.search("Lagos", "Kano", "2026-08-23")
    live.calls.clear()
    row = db.query(FareLedger).first()
    row.last_updated = utcnow() - timedelta(minutes=21)
    db.commit()

    service.clock = lambda: utcnow() + timedelta(minutes=16)
    result = service.search("Lagos", "Kano", "2026-08-23")
    assert result["source"] == "serpapi"
    assert live.calls == [("LOS", "KAN", "2026-08-23")]


def test_api_receives_normalized_iata_not_city_names(search):
    """Step 5: the local dict (iata.py) must map cities BEFORE SerpApi.

    'Abuja' -> 'ABV', 'Port Harcourt' -> 'PHC' - the engine never sees
    a raw city name, so it can never throw a 'invalid airport' error.
    """
    service, live = search
    service.search("Abuja", "Port Harcourt", "2026-08-24")
    assert live.calls == [("ABV", "PHC", "2026-08-24")]


def test_ledger_row_is_upserted_not_duplicated(search, db):
    """The community ledger keeps ONE row per (route, date); re-search updates."""
    service, _ = search
    service.search("Lagos", "Abuja", "2026-08-25")
    service.search("Lagos", "Abuja", "2026-08-25")
    rows = db.query(FareLedger).all()
    assert len(rows) == 1
    assert rows[0].origin == "LOS" and rows[0].destination == "ABV"


def test_unresolvable_route_returns_none(search):
    """Garbage city names are rejected by the dict, never sent to an API."""
    service, live = search
    result = service.search("Xanadu", "Atlantis", "2026-08-26")
    assert result is None
    assert live.calls == []


def test_resolve_iata_ph_vs_phc():
    """The classic LLM mistake - 'PH' must resolve to 'PHC'."""
    assert resolve_iata("PH") == "PHC"
    assert resolve_iata("abuja") == "ABV"
    assert resolve_iata("Port Harcourt") == "PHC"
    assert resolve_iata("LOS") == "LOS"
    assert resolve_iata("Mars") is None
