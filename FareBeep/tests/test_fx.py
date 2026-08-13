"""USD -> NGN conversion - live rate with floor (search.py fx layer)."""
import time

import pytest

from FareBeep import search
from FareBeep.config import FX_RATE_NGN_PER_USD


class _FakeResp:
    def __init__(self, rates):
        self._rates = rates

    def raise_for_status(self):
        pass

    def json(self):
        return {"result": "success", "rates": self._rates}


class _FakeClient:
    def __init__(self, rates):
        self._rates = rates
        self.calls = 0

    def get(self, url, params=None):
        self.calls += 1
        return _FakeResp(self._rates)


def test_live_rate_is_floored(monkeypatch):
    """A live rate BELOW the floor must never be used (margin protection)."""
    monkeypatch.setattr(search, "_fx_cache", {"ts": 0.0, "rate": None})
    client = _FakeClient({"NGN": 1300.0})          # weaker than floor 1425
    rate = search.ngn_per_usd(floor=1425.0, http_client=client)
    assert rate == 1425.0


def test_live_rate_above_floor_is_used(monkeypatch):
    """Live rate wins, but the safety margin (official x 1.03) is applied -
    this is the 'Google-basis' quote: it tracks Google's naira display while
    a small buffer protects the founder."""
    monkeypatch.setattr(search, "_fx_cache", {"ts": 0.0, "rate": None})
    client = _FakeClient({"NGN": 1440.0})
    rate = search.ngn_per_usd(floor=1425.0, safety_margin=0.03, http_client=client)
    assert rate == 1440.0 * 1.03            # 1483.2


def test_safety_margin_can_be_disabled(monkeypatch):
    monkeypatch.setattr(search, "_fx_cache", {"ts": 0.0, "rate": None})
    client = _FakeClient({"NGN": 1440.0})
    rate = search.ngn_per_usd(floor=1425.0, safety_margin=0.0, http_client=client)
    assert rate == 1440.0


def test_margin_never_pushes_below_floor(monkeypatch):
    """live x 1.03 landing under the floor must still be floored."""
    monkeypatch.setattr(search, "_fx_cache", {"ts": 0.0, "rate": None})
    client = _FakeClient({"NGN": 1350.0})   # x1.03 = 1390.5 < floor 1425
    rate = search.ngn_per_usd(floor=1425.0, safety_margin=0.03, http_client=client)
    assert rate == 1425.0


def test_rate_is_cached_until_ttl(monkeypatch):
    monkeypatch.setattr(search, "_fx_cache", {"ts": time.time(), "rate": 1440.0})
    client = _FakeClient({"NGN": 9999.0})
    rate = search.ngn_per_usd(floor=1425.0, ttl_hours=12, http_client=client)
    assert rate == 1440.0
    assert client.calls == 0


def test_api_failure_falls_back_to_floor(monkeypatch):
    monkeypatch.setattr(search, "_fx_cache", {"ts": 0.0, "rate": None})

    class _Boom:
        def get(self, url, params=None):
            raise RuntimeError("network down")

    rate = search.ngn_per_usd(floor=1425.0, http_client=_Boom())
    assert rate == 1425.0