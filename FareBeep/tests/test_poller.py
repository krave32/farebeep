"""Unit tests for the tunnel-free Telegram poller."""
from FareBeep import poller


class _FakeResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"result": [
            {"update_id": 10, "message": {"chat": {"id": 555}, "text": "hi"}},
            {"update_id": 11,
             "message": {"chat": {"id": 556}, "text": "Lagos to Abuja"}}]}


class _FakeClient:
    def get(self, url, params):
        self.captured = (url, params)
        return _FakeResp()


def test_poll_once_routes_messages_and_advances_offset(monkeypatch):
    handled = []
    monkeypatch.setattr(poller, "_handle",
                        lambda cid, text: handled.append((cid, text)))
    client = _FakeClient()
    nxt = poller.poll_once(client, 0)
    assert nxt == 12
    assert handled == [("555", "hi"), ("556", "Lagos to Abuja")]
    assert client.captured[1]["timeout"] == 25
    assert client.captured[1]["offset"] == 0


def test_poll_once_ignores_updates_without_text(monkeypatch):
    handled = []
    monkeypatch.setattr(poller, "_handle",
                        lambda cid, text: handled.append((cid, text)))

    class _Silent(_FakeResp):
        def json(self):
            return {"result": [
                {"update_id": 20, "message": {"chat": {"id": 1}}},
                {"update_id": 21, "message": {}}]}

    class _Client(_FakeClient):
        def get(self, url, params):
            self.captured = (url, params)
            return _Silent()

    assert poller.poll_once(_Client(), 19) == 22
    assert handled == []


def test_poll_once_offset_never_goes_backwards(monkeypatch):
    handled = []
    monkeypatch.setattr(poller, "_handle",
                        lambda cid, text: handled.append((cid, text)))
    client = _FakeClient()
    assert poller.poll_once(client, 9) == 12