"""USD -> NGN snapshot fetch (worker.fetch_usd_ngn) - the FX history feed.

Quoting NEVER converts currencies: live supplier fares arrive in NGN. This
feed exists only so worker.record_fx_rate() can chart naira movement over
time (models.FxRate).
"""


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

    def get(self, url, params=None):
        return _FakeResp(self._rates)


def test_live_rate_is_returned():
    from FareBeep import worker
    assert worker.fetch_usd_ngn(_FakeClient({"NGN": 1440.0})) == 1440.0


def test_non_positive_rate_is_rejected():
    from FareBeep import worker
    assert worker.fetch_usd_ngn(_FakeClient({"NGN": 0})) is None


def test_api_failure_returns_none():
    from FareBeep import worker

    class _Boom:
        def get(self, url, params=None):
            raise RuntimeError("network down")

    assert worker.fetch_usd_ngn(_Boom()) is None
