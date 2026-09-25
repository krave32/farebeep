"""TRAVELS247 CLIENT - JWT cache, search/pricing/reserve, retry policy."""
import asyncio
import json

import httpx
import pytest

from FareBeep import travels247 as sky_mod
from FareBeep.travels247 import (Travels247Client, Travels247Error,
                                     pick_cheapest)


def _run(coro):
    return asyncio.run(coro)


LOGIN = {"status": "success", "code": "LOGIN_SUCCESS",
         "data": {"user_id": "USR-001", "email": "p@x.com", "role": "api",
                  "access_token": "TOK123", "refresh_token": "REF123",
                  "token_type": "Bearer", "expires_in": 900}}

OFFER = {"flight_no": "P47123", "airline": "P4",
         "airline_name": "Air Peace",
         "departure_code": "LOS", "arrival_code": "ABV",
         "departure_time": "08:30", "arrival_time": "09:25",
         "duration_time": "0h 55m", "class": "Economy",
         "baggage": "20kg", "price": 98000.0, "currency": "NGN",
         "booking_token": "btk_aaa"}


class _Script:
    """Route-aware fake Travels247 API: (method, path) -> queue of (status, body)."""

    def __init__(self, routes):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls = []

    def __call__(self, request):
        try:
            payload = request.read()
        except Exception:
            payload = b""
        self.calls.append((request.method, request.url.path,
                           request.headers.get("authorization", ""),
                           (json.loads(payload) if payload else {})))
        q = self.routes.get((request.method, request.url.path))
        assert q, f"unexpected Travels247 call {request.method} {request.url.path}"
        status, body = q.pop(0)
        return httpx.Response(status, json=body, request=request)


def _client(routes, **kw):
    script = _Script(routes)
    http = httpx.AsyncClient(transport=httpx.MockTransport(script))
    kw.setdefault("email", "p@x.com")
    kw.setdefault("password", "pw")
    return Travels247Client(http_client=http, **kw), script


def _search_routes(offer=OFFER, n_logins=1, n_searches=1):
    search_ok = (200, {"success": True,
                       "data": {"meta": {}, "flights": [offer]}})
    return {("POST", "/api/login"): [(200, LOGIN)] * n_logins,
            ("POST", "/api/flights/search"): [search_ok] * n_searches}


def test_login_token_is_cached_across_calls():
    client, script = _client(_search_routes(n_searches=2))
    try:
        first = _run(client.search_offers("LOS", "ABV", "2026-08-20"))
        second = _run(client.search_offers("LOS", "ABV", "2026-08-20"))
        assert first[0]["price"] == 98000.0
        assert second[0]["booking_token"] == "btk_aaa"
        logins = [c for c in script.calls if c[1] == "/api/login"]
        assert len(logins) == 1          # one login, two searches
        search_auth = [c for c in script.calls
                       if c[1] == "/api/flights/search"][0][2]
        assert search_auth == "Bearer TOK123"
    finally:
        _run(client.close())


def test_search_normalizes_offer_fields():
    client, script = _client(_search_routes())
    try:
        (offer,) = _run(client.search_offers("LOS", "ABV", "2026-08-20"))
        assert offer == {
            "flight_no": "P47123", "airline": "P4",
            "airline_name": "Air Peace",
            "departure_code": "LOS", "arrival_code": "ABV",
            "departure_time": "08:30", "arrival_time": "09:25",
            "duration": "0h 55m", "baggage": "20kg", "cabin": "Economy",
            "price": 98000.0, "currency": "NGN", "seats_left": None,
            "booking_token": "btk_aaa",
        }
    finally:
        _run(client.close())


def test_search_normalizes_nested_segment_shape():
    """The live wire format: flight facts in segments[0][0], price +
    booking token at top level (seen on the 247travels test env)."""
    nested = {
        "segments": [[{
            "img": "QI", "flight_no": "QI300", "airline": "IBOM AIR",
            "departure_code": "LOS", "arrival_code": "ABV",
            "departure_time": "07:00 am", "arrival_time": "08:15 am",
            "duration_time": "1h 15m", "baggage": "20 kg",
            "class": "Economy"}]],
        "price": 108401.53, "currency": "NGN", "seats_left": 9,
        "booking_token": "btk_nested"}
    client, script = _client(_search_routes(offer=nested))
    try:
        (offer,) = _run(client.search_offers("LOS", "ABV", "2026-09-14"))
        assert offer == {
            "flight_no": "QI300", "airline": "QI",
            "airline_name": "Ibom Air",
            "departure_code": "LOS", "arrival_code": "ABV",
            "departure_time": "07:00 am", "arrival_time": "08:15 am",
            "duration": "1h 15m", "baggage": "20 kg", "cabin": "Economy",
            "price": 108401.53, "currency": "NGN",
            "seats_left": 9, "booking_token": "btk_nested",
        }
    finally:
        _run(client.close())


def test_search_empty_and_unusable_offers():
    routes = {("POST", "/api/login"): [(200, LOGIN)],
              ("POST", "/api/flights/search"): [
                  (200, {"success": True, "data": {"meta": {}, "flights": []}}),
                  (200, {"success": True,
                         "data": {"meta": {},
                                  "flights": [{"flight_no": "X1"}]}})]}
    client, _ = _client(routes)
    try:
        assert _run(client.search_offers("LOS", "ABV", "2026-08-20")) == []
        assert _run(client.search_offers("LOS", "ABV", "2026-08-20")) == []
    finally:
        _run(client.close())


def test_expired_token_forces_relogin_and_retry():
    routes = {("POST", "/api/login"): [(200, LOGIN), (200, LOGIN)],
              ("POST", "/api/flights/search"): [
                  (401, {"success": False}),
                  (200, {"success": True,
                         "data": {"meta": {}, "flights": [OFFER]}})]}
    client, script = _client(routes)
    try:
        (offer,) = _run(client.search_offers("LOS", "ABV", "2026-08-20"))
        assert offer["price"] == 98000.0
        assert len([c for c in script.calls
                    if c[1] == "/api/login"]) == 2
    finally:
        _run(client.close())


def test_retry_on_502_then_success():
    routes = {("POST", "/api/login"): [(200, LOGIN)],
              ("POST", "/api/flights/search"): [
                  (502, {"success": False}),
                  (200, {"success": True,
                         "data": {"meta": {}, "flights": [OFFER]}})]}
    client, script = _client(routes, max_retries=3)
    try:
        (offer,) = _run(client.search_offers("LOS", "ABV", "2026-08-20"))
        assert offer["flight_no"] == "P47123"
        assert len([c for c in script.calls
                    if c[1] == "/api/flights/search"]) == 2
    finally:
        _run(client.close())


def test_plain_400_is_fatal_immediately():
    routes = {("POST", "/api/login"): [(200, LOGIN)],
              ("POST", "/api/flights/search"): [(400, {"success": False})]}
    client, script = _client(routes)
    try:
        with pytest.raises(Travels247Error):
            _run(client.search_offers("LOS", "ABV", "2026-08-20"))
        assert len([c for c in script.calls
                    if c[1] == "/api/flights/search"]) == 1
    finally:
        _run(client.close())


def test_missing_credentials_raise_before_any_call(monkeypatch):
    monkeypatch.setattr(sky_mod, "TRAVELS247_EMAIL", None)
    monkeypatch.setattr(sky_mod, "TRAVELS247_PASSWORD", None)
    script = _Script({})
    http = httpx.AsyncClient(transport=httpx.MockTransport(script))
    client = Travels247Client(http_client=http)
    try:
        with pytest.raises(Travels247Error):
            _run(client.search_offers("LOS", "ABV", "2026-08-20"))
        assert script.calls == []
    finally:
        _run(client.close())


def test_verify_price_returns_refreshed_token():
    pricing = {"success": True,
               "data": {"booking_token": "btk_bbb", "verified": True,
                        "price_changed": True, "original_price": 98000.0,
                        "verified_price": 99500.0, "currency": "NGN",
                        "expires_at": "2026-05-17 14:30:00"}}
    routes = {("POST", "/api/login"): [(200, LOGIN)],
              ("POST", "/api/flights/pricing"): [(200, pricing)]}
    client, _ = _client(routes)
    try:
        out = _run(client.verify_price("btk_aaa"))
        assert out == {"verified": True, "price_changed": True,
                       "original_price": 98000.0, "verified_price": 99500.0,
                       "currency": "NGN", "booking_token": "btk_bbb",
                       "expires_at": "2026-05-17 14:30:00"}
    finally:
        _run(client.close())


def test_reserve_returns_pnr():
    reserve = {"success": True,
               "data": {"pnr": "ABC123", "booking_reference": "ABC123",
                        "carrier": "P4", "status": "confirmed",
                        "ticket_deadline": "2026-05-19 14:30:00"}}
    routes = {("POST", "/api/login"): [(200, LOGIN)],
              ("POST", "/api/flights/reserve"): [(200, reserve)]}
    client, script = _client(routes)
    try:
        out = _run(client.reserve("btk_bbb", {"primary_guest": {}}))
        assert out["pnr"] == "ABC123"
        assert out["status"] == "confirmed"
        # An explicit primary_guest dict passes through untouched.
        reserve_call = next(c for c in script.calls
                            if c[1] == "/api/flights/reserve")
        assert reserve_call[3]["travellers"] == {"primary_guest": {}}
    finally:
        _run(client.close())


def test_reserve_wraps_flat_traveller_in_primary_guest():
    """REGRESSION (live smoke 25 Sep 2026): the vendor rejected a flat
    traveller dict with "travellers.primary_guest is required" - the
    client wraps it so callers never learn the wire shape."""
    reserve = {"success": True,
               "data": {"pnr": "XYZ789", "status": "confirmed"}}
    routes = {("POST", "/api/login"): [(200, LOGIN)],
              ("POST", "/api/flights/reserve"): [(200, reserve)]}
    client, script = _client(routes)
    try:
        out = _run(client.reserve("btk_bbb", {"first_name": "Smoke",
                                              "last_name": "Test"}))
        assert out["pnr"] == "XYZ789"
        reserve_call = next(c for c in script.calls
                            if c[1] == "/api/flights/reserve")
        assert reserve_call[3]["travellers"] == {"primary_guest": {
            "first_name": "Smoke", "last_name": "Test"}}
    finally:
        _run(client.close())


def test_pick_cheapest_needs_a_booking_token():
    assert pick_cheapest([]) is None
    assert pick_cheapest([{"price": 1.0}]) is None
    assert pick_cheapest([{"price": 99.0, "booking_token": "b"},
                          {"price": 50.0, "booking_token": "a"}])["price"] == 50.0
