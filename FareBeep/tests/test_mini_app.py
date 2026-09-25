"""TELEGRAM MINI APP - the 'Set a beep' form (the WhatsApp-Flow mirror).

The one-engine/two-transports contract: the Mini App page is a transport,
identity is Telegram-signed initData, and the submit lands in the SAME
SubscriptionMonitor.subscribe the WhatsApp Flow completion uses.
"""
import hashlib
import hmac as hmac_mod
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from FareBeep import main
from FareBeep.config import TELEGRAM_BOT_TOKEN
from FareBeep.models import Base, Subscription, User

SECRET = "farebeep-test-secret"
CHAT = 555002


def _sign(payload: dict) -> str:
    """Real Telegram WebApp initData, signed the way Telegram signs it:
    the data-check string uses the URL-ENCODED values exactly as they
    appear in the query string, HMAC-SHA256 keyed by
    HMAC-SHA256('WebAppData', bot token)."""
    pairs = {**payload, "auth_date": str(int(time.time()))}
    encoded = urlencode(pairs)                     # k=v&k=v (values encoded)
    check = "\n".join(sorted(encoded.split("&")))
    secret = hmac_mod.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode(),
                          hashlib.sha256).digest()
    sig = hmac_mod.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}&hash={sig}"


def _init_data() -> str:
    return _sign({"user": json.dumps(
        {"id": CHAT, "first_name": "Gideon"}, separators=(",", ":"))})


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def client(monkeypatch, session_factory):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(main, "SessionLocal", session_factory)
    return TestClient(main.app), session_factory


def test_init_data_rejects_bad_signature(client):
    test_client, _ = client
    r = test_client.post("/mini/beep/submit", json={
        "initData": "user=%7B%22id%22%3A1%7D&hash=deadbeef",
        "origin": "LOS", "destination": "ABV", "dropwatch": True})
    assert r.status_code == 401
    assert "verify" in r.json()["error"].lower()


def test_init_data_rejects_empty(client):
    test_client, _ = client
    r = test_client.post("/mini/beep/submit", json={
        "initData": "", "origin": "LOS", "destination": "ABV"})
    assert r.status_code == 401


def test_page_is_served(client):
    test_client, _ = client
    r = test_client.get("/mini/beep")
    assert r.status_code == 200
    assert "Set a beep" in r.text
    assert "telegram-web-app.js" in r.text


def test_submit_dropwatch_creates_subscription(client, session_factory):
    test_client, sf = client
    r = test_client.post("/mini/beep/submit", json={
        "initData": _init_data(), "origin": "Lagos",
        "destination": "Port Harcourt", "departure_date": "2026-10-01",
        "target_price": None, "dropwatch": True})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "Port Harcourt" in data["summary"]
    db = sf()
    user = db.query(User).filter_by(phone=str(CHAT)).first()
    assert user is not None                      # identity from initData
    sub = db.query(Subscription).filter_by(user_id=user.user_id).first()
    db.close()
    assert sub.origin == "LOS" and sub.destination == "PHC"
    assert sub.target_price is None              # drop-watch
    assert sub.target_date.strftime("%Y-%m-%d") == "2026-10-01"


def test_submit_with_target_price(client, session_factory):
    test_client, sf = client
    r = test_client.post("/mini/beep/submit", json={
        "initData": _init_data(), "origin": "LOS", "destination": "ABV",
        "target_price": 90000, "dropwatch": False})
    assert r.status_code == 200
    db = sf()
    sub = db.query(Subscription).first()
    db.close()
    assert sub.target_price == 90000.0


def test_submit_rejects_same_origin_destination(client):
    test_client, _ = client
    r = test_client.post("/mini/beep/submit", json={
        "initData": _init_data(), "origin": "LOS", "destination": "Lagos",
        "dropwatch": True})
    assert r.status_code == 422


def test_submit_rejects_no_watch_mode(client):
    test_client, _ = client
    r = test_client.post("/mini/beep/submit", json={
        "initData": _init_data(), "origin": "LOS", "destination": "ABV",
        "target_price": None, "dropwatch": False})
    assert r.status_code == 422


def test_submit_is_idempotent_per_route(client, session_factory):
    test_client, sf = client
    body = {"initData": _init_data(), "origin": "LOS", "destination": "ABV",
            "dropwatch": True}
    test_client.post("/mini/beep/submit", json=body)
    test_client.post("/mini/beep/submit", json=body)
    db = sf()
    assert db.query(Subscription).count() == 1   # power idempotency holds
    db.close()


def test_set_beep_button_opens_mini_app_on_https(client, monkeypatch):
    """The Telegram set_beep tap sends the Mini App button when the app
    runs on a public https origin (the Mini App transport)."""
    test_client, sf = client
    monkeypatch.setattr(main, "MESSAGING_PROVIDER", "telegram")
    monkeypatch.setattr(main, "APP_BASE_URL", "https://farebeep.ng")
    sent = []

    class _Spy:
        def send_beep_app(self, to, url):
            sent.append((to, url))
            return True

        def __getattr__(self, name):
            return lambda *a, **k: True

    monkeypatch.setattr(main, "notifier", _Spy())
    main._send_beep_flow("555009")
    assert sent == [("555009", "https://farebeep.ng/mini/beep")]


def test_set_beep_falls_back_to_track_without_https(client, monkeypatch):
    """localhost dev origin -> Mini App cannot open -> guided TRACK path."""
    test_client, sf = client
    monkeypatch.setattr(main, "MESSAGING_PROVIDER", "telegram")
    monkeypatch.setattr(main, "APP_BASE_URL", "http://localhost:8000")
    handled = []
    monkeypatch.setattr(main, "_handle_incoming_message",
                        lambda phone, text: handled.append((phone, text)))
    main._send_beep_flow("555009")
    assert handled == [("555009", "TRACK")]
