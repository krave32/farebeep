"""CRASH + ORDER + RETENTION + MIGRATION + SINGLE-PATH verification.

Answers, with tests:
  Q1 restart-after-ack -> orphaned queued rows are re-dispatched at boot
     from the stored payload (no Meta redelivery needed).
  Q2 concurrent batches for one phone never interleave (per-phone lock).
  Q3 3x-failed messages are retained with the error and dead-lettered.
  Q4 new tables are created on a pre-existing database without touching
     old rows (file-DB migration simulation).
  Q5 the live webhook path never touches whatsapp/handlers+sensors - one
     reply per inbound, no double-processing path.
"""
import hashlib
import hmac
import json
import logging
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from FareBeep import main
from FareBeep.models import Base, DeliveryReceipt, ProcessedMessage
from FareBeep.whatsapp.router import InboundMessage, MessageType


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
    monkeypatch.setattr(main, "META_VERIFY_TOKEN", "test-verify-token")
    monkeypatch.setattr(main, "META_APP_SECRET", "test-app-secret")
    monkeypatch.setattr(main, "SessionLocal", session_factory)
    monkeypatch.setattr(main.brain, "GEMINI_API_KEY", None)
    monkeypatch.setattr(main, "GROQ_API_KEY", None)
    monkeypatch.setattr(main, "MetaWhatsapp",
                        lambda *a, **k: type("W", (), {
                            "send_typing_indicator": staticmethod(
                                lambda to: True)})())
    sent = []
    monkeypatch.setattr(main, "notifier", type("N", (), {
        "send_text": staticmethod(
            lambda to, body: sent.append((to, body)) or True)})())
    client = TestClient(main.app)
    client.sent = sent
    return client


def _sig(body: bytes) -> str:
    return "sha256=" + hmac.new(b"test-app-secret", body,
                                hashlib.sha256).hexdigest()


def _post(client, payload):
    body = json.dumps(payload).encode()
    return client.post("/webhook/meta", content=body,
                       headers={"X-Hub-Signature-256": _sig(body)})


def _text_msg(mid, phone, body, ts):
    return {"id": mid, "from": phone, "timestamp": str(ts),
            "type": "text", "text": {"body": body}}


def _row(session_factory, mid):
    db = session_factory()
    try:
        return db.query(ProcessedMessage).filter_by(
            message_id=mid).first()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Q1 - crash after ack
# ---------------------------------------------------------------------------
def test_claim_stores_replayable_payload(client, monkeypatch,
                                         session_factory):
    monkeypatch.setattr(main, "_handle_incoming_message",
                        lambda phone, text: None)
    payload = {"entry": [{"changes": [{"value": {"messages": [
        _text_msg("wamid-p1", "+234801", "hello", 1)]}}]}]}
    assert _post(client, payload).status_code == 200
    snap = _row(session_factory, "wamid-p1").payload
    assert snap["message_id"] == "wamid-p1"
    assert snap["text"] == "hello" and snap["from_number"] == "+234801"


def test_orphan_redispatched_after_restart(client, monkeypatch,
                                           session_factory):
    """Simulated crash: classified + claimed, background never ran.
    Boot recovery replays the stored payload exactly once."""
    msgs = main._classify_all_entries(
        {"entry": [{"changes": [{"value": {"messages": [
            _text_msg("wamid-crash", "+234801", "lagos to abuja", 4)
        ]}}]}]})
    assert main._claim_inbound_messages(msgs) != []  # 200 would go out here
    assert _row(session_factory, "wamid-crash").status == "queued"
    # ... process dies, restarts, startup sweep runs:
    calls = []
    monkeypatch.setattr(main, "_handle_incoming_message",
                        lambda phone, text: calls.append((phone, text)))
    db = session_factory()
    try:
        assert main.recover_orphaned_inbound(db) == 1
    finally:
        db.close()
    assert calls == [("+234801", "lagos to abuja")]
    assert _row(session_factory, "wamid-crash").status == "done"


def test_startup_wires_recovery(monkeypatch):
    wired = {}
    monkeypatch.setattr(main, "init_db",
                        lambda base: wired.setdefault("init", True))
    import FareBeep.database as dbmod
    monkeypatch.setattr(dbmod, "verify_connection", lambda: True)
    monkeypatch.setattr(main, "recover_orphaned_inbound",
                        lambda db=None: wired.setdefault("recover", True)
                        or 0)
    main._startup()
    assert wired == {"init": True, "recover": True}


# ---------------------------------------------------------------------------
# Q2 - cross-request ordering
# ---------------------------------------------------------------------------
def test_same_phone_batches_never_interleave(monkeypatch, session_factory):
    import time
    records = []
    rec_lock = threading.Lock()

    def _slow_dispatch(msg):
        time.sleep(0.05)
        with rec_lock:
            records.append((msg.button_title, msg.text))

    monkeypatch.setattr(main, "_dispatch_single", _slow_dispatch)
    monkeypatch.setattr(main, "_mark_processed",
                        lambda *a, **k: None)

    def _batch(tag, base):
        return [InboundMessage(
            message_id=f"w-{tag}-{i}", from_number="+234801",
            message_type=MessageType.TEXT, timestamp=str(base + i), raw={},
            text=f"{tag}-{i}", button_title=tag) for i in range(3)]

    t1 = threading.Thread(target=main._process_inbound_batch,
                          args=(_batch("A", 1),))
    t2 = threading.Thread(target=main._process_inbound_batch,
                          args=(_batch("B", 11),))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)
    tags = [t for t, _ in records]
    assert sorted(tags) == ["A", "A", "A", "B", "B", "B"]
    assert tags[:3] == [tags[0]] * 3 and tags[3:] == [tags[3]] * 3


# ---------------------------------------------------------------------------
# Q3 - dead-letter retention
# ---------------------------------------------------------------------------
def test_three_fails_retained_then_dropped(client, monkeypatch,
                                           session_factory, caplog):
    calls = []

    def _boom(phone, text):
        calls.append(text)
        raise RuntimeError("provider down")

    monkeypatch.setattr(main, "_handle_incoming_message", _boom)
    payload = {"entry": [{"changes": [{"value": {"messages": [
        _text_msg("wamid-dl", "+234801", "book", 2)]}}]}]}
    with caplog.at_level(logging.ERROR, logger="farebeep.main"):
        for _ in range(4):
            assert _post(client, payload).status_code == 200
    assert calls == ["book", "book", "book"]  # 4th redelivery dropped
    row = _row(session_factory, "wamid-dl")
    assert row.status == "failed" and row.attempts == 3
    assert "provider down" in (row.last_error or "")
    assert "Dead-letter" in caplog.text


# ---------------------------------------------------------------------------
# Q4 - safe creation on an existing deployment
# ---------------------------------------------------------------------------
def test_new_tables_created_without_touching_legacy_rows(tmp_path):
    """An old deployment DB (users table + data, nothing else) gains the
    new tables via the same create_all() init_db() runs - legacy rows
    byte-identical afterwards."""
    f = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(f))
    conn.execute("CREATE TABLE users (user_id TEXT PRIMARY KEY, "
                 "phone TEXT UNIQUE, name TEXT)")
    conn.execute("INSERT INTO users VALUES ('u-1', '+234801', 'Ada')")
    conn.commit()
    conn.close()

    engine = create_engine(f"sqlite:///{f}")
    Base.metadata.create_all(engine)  # what init_db(Base) does
    names = set(inspect(engine).get_table_names())
    assert {"processed_messages", "delivery_receipts"} <= names
    conn = sqlite3.connect(str(f))
    try:
        assert conn.execute("SELECT phone, name FROM users").fetchall() == [
            ("+234801", "Ada")]
    finally:
        conn.close()

    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(ProcessedMessage(message_id="w", phone="+234801",
                            message_type="text", status="queued"))
    db.add(DeliveryReceipt(message_id="o", phone="+234801",
                           status="delivered"))
    db.commit()
    assert db.query(ProcessedMessage).count() == 1
    assert db.query(DeliveryReceipt).count() == 1
    db.close()


def test_schema_sql_declares_new_tables():
    from pathlib import Path
    sql = (Path(main.__file__).parent / "schema.sql").read_text()
    assert "create table if not exists processed_messages" in sql
    assert "create table if not exists delivery_receipts" in sql


# ---------------------------------------------------------------------------
# Q5 - one live path
# ---------------------------------------------------------------------------
def test_live_path_bypasses_whatsapp_handlers(client, monkeypatch):
    import FareBeep.whatsapp.handlers as handlers
    import FareBeep.whatsapp.sender as sender

    def _forbidden(*a, **k):
        raise AssertionError("second processing path must not run")

    monkeypatch.setattr(handlers, "handle_inbound", _forbidden)
    monkeypatch.setattr(sender, "send_text", _forbidden)
    monkeypatch.setattr(sender, "send_buttons", _forbidden)
    monkeypatch.setattr(main, "_handle_incoming_message",
                        lambda phone, text: main.notifier.send_text(
                            phone, f"echo:{text}"))
    payload = {"entry": [{"changes": [{"value": {"messages": [
        _text_msg("wamid-1path", "+234801", "hello", 1)]}}]}]}
    assert _post(client, payload).status_code == 200
    assert client.sent == [("+234801", "echo:hello")]
