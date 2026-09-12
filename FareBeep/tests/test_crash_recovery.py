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
def test_same_phone_batches_never_interleave(client, monkeypatch,
                                             session_factory):
    """Two racing batches for one phone: work-sharing + per-phone lock
    mean all six turns run exactly once, in saved arrival order, with no
    interleave - whichever thread wins still does A-then-B."""
    import time
    records = []
    rec_lock = threading.Lock()

    def _slow_dispatch(msg):
        time.sleep(0.05)
        with rec_lock:
            records.append(msg.text)

    monkeypatch.setattr(main, "_dispatch_single", _slow_dispatch)

    def _raw(tag, i, ts):
        return {"id": f"w-{tag}-{i}", "from": "+234801",
                "timestamp": str(ts), "type": "text",
                "text": {"body": f"{tag}-{i}"}}

    batch_a = main._classify_all_entries(
        {"entry": [{"changes": [{"value": {"messages": [
            _raw("A", 0, 1), _raw("A", 1, 2), _raw("A", 2, 3)]}}]}]})
    batch_b = main._classify_all_entries(
        {"entry": [{"changes": [{"value": {"messages": [
            _raw("B", 0, 11), _raw("B", 1, 12), _raw("B", 2, 13)]}}]}]})
    assert len(main._claim_inbound_messages(batch_a)) == 3
    assert len(main._claim_inbound_messages(batch_b)) == 3

    t1 = threading.Thread(target=main._process_inbound_batch,
                          args=(batch_a,))
    t2 = threading.Thread(target=main._process_inbound_batch,
                          args=(batch_b,))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)
    assert records == ["A-0", "A-1", "A-2", "B-0", "B-1", "B-2"]


def test_arrival_order_beats_batch_race(client, monkeypatch,
                                        session_factory):
    """The exact cross-request race: A claimed first (earlier arrival),
    but B's batch runs first. Saved arrival order still wins - A then B."""
    calls = []
    monkeypatch.setattr(main, "_handle_incoming_message",
                        lambda phone, text: calls.append(text))

    def _raw(mid, ts, body):
        return {"id": mid, "from": "+234801", "timestamp": str(ts),
                "type": "text", "text": {"body": body}}

    batch_a = main._classify_all_entries(
        {"entry": [{"changes": [{"value": {"messages": [
            _raw("wamid-first", 1, "first")]}}]}]})
    batch_b = main._classify_all_entries(
        {"entry": [{"changes": [{"value": {"messages": [
            _raw("wamid-second", 2, "second")]}}]}]})
    main._claim_inbound_messages(batch_a)  # arrives first...
    main._claim_inbound_messages(batch_b)  # ...arrives second...
    main._process_inbound_batch(batch_b)   # ...but B's batch runs first
    assert calls == ["first", "second"]


def test_sender_timestamp_leads_claim_order(client, monkeypatch,
                                            session_factory):
    """Order rule pin: sender timestamp first, claim time second. B was
    claimed first (arrival inversion), but A's earlier sender timestamp
    still runs first - one payload can arrive wire-shuffled."""
    calls = []
    monkeypatch.setattr(main, "_handle_incoming_message",
                        lambda phone, text: calls.append(text))

    def _raw(mid, ts, body):
        return {"id": mid, "from": "+234801", "timestamp": str(ts),
                "type": "text", "text": {"body": body}}

    batch_b = main._classify_all_entries(
        {"entry": [{"changes": [{"value": {"messages": [
            _raw("wamid-late", 2, "second")]}}]}]})
    batch_a = main._classify_all_entries(
        {"entry": [{"changes": [{"value": {"messages": [
            _raw("wamid-early", 1, "first")]}}]}]})
    main._claim_inbound_messages(batch_b)  # claimed first...
    main._claim_inbound_messages(batch_a)  # ...claimed second...
    main._process_inbound_batch(batch_b + batch_a)
    assert calls == ["first", "second"]


def test_sweep_recovers_stale_without_restart(client, monkeypatch,
                                              session_factory):
    """P1: a queued row older than the cutoff is re-dispatched by the
    periodic sweep - no restart, no Meta redelivery. A fresh queued row
    (live task may own it) is left strictly alone."""
    from datetime import timedelta
    from FareBeep.models import utcnow
    calls = []
    monkeypatch.setattr(main, "_handle_incoming_message",
                        lambda phone, text: calls.append(text))

    stale = main._classify_all_entries(
        {"entry": [{"changes": [{"value": {"messages": [
            {"id": "wamid-stale", "from": "+234801", "timestamp": "1",
             "type": "text", "text": {"body": "stale turn"}}]}}]}]})
    # Fresh turn belongs to ANOTHER customer: the sweep trigger is
    # stale-phone-scoped, so it must stay queued and undispatched.
    fresh = main._classify_all_entries(
        {"entry": [{"changes": [{"value": {"messages": [
            {"id": "wamid-fresh", "from": "+234802", "timestamp": "2",
             "type": "text", "text": {"body": "fresh turn"}}]}}]}]})
    main._claim_inbound_messages(stale)
    main._claim_inbound_messages(fresh)
    db = session_factory()
    db.query(ProcessedMessage).filter_by(
        message_id="wamid-stale").first().created_at = (
            utcnow() - timedelta(minutes=10))
    db.commit()
    db.close()

    assert main.recover_orphaned_inbound(stale_minutes=5) == 1
    assert calls == ["stale turn"]
    assert _row(session_factory, "wamid-stale").status == "done"
    assert _row(session_factory, "wamid-fresh").status == "queued"


def test_legacy_row_without_payload_parked_not_looped(client, monkeypatch,
                                                      session_factory):
    """Pre-recovery rows (queued, no payload) can never replay: the sweep
    parks them exhausted with an explicit error, and redelivery drops
    them instead of cycling forever."""
    from datetime import timedelta
    from FareBeep.models import utcnow
    db = session_factory()
    db.add(ProcessedMessage(message_id="wamid-legacy", phone="+234801",
                            message_type="text", status="queued",
                            created_at=utcnow() - timedelta(minutes=30)))
    db.commit()
    db.close()
    calls = []
    monkeypatch.setattr(main, "_handle_incoming_message",
                        lambda phone, text: calls.append(text))
    assert main.recover_orphaned_inbound(stale_minutes=5) == 0
    row = _row(session_factory, "wamid-legacy")
    assert row.status == "failed" and row.attempts == main.MAX_INBOUND_ATTEMPTS
    assert "no stored payload" in (row.last_error or "")
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "wamid-legacy", "from": "+234801", "timestamp": "9",
         "type": "text", "text": {"body": "retry"}}]}}]}]}
    assert _post(client, payload).status_code == 200
    assert calls == []


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


# ---------------------------------------------------------------------------
# P3 - payload column lands on a pre-existing processed_messages table
# ---------------------------------------------------------------------------
def test_payload_column_migrates_existing_table(tmp_path):
    """Old table (pre-payload DDL) + live row -> ADD COLUMN keeps the row,
    and the ORM reads/writes the new JSON column afterwards."""
    import sqlite3
    f = tmp_path / "old.db"
    conn = sqlite3.connect(str(f))
    conn.execute(
        "CREATE TABLE processed_messages (message_id TEXT PRIMARY KEY, "
        "phone TEXT, message_type TEXT, status TEXT DEFAULT 'queued', "
        "attempts INTEGER DEFAULT 0, last_error TEXT, "
        "processed_at TIMESTAMP, created_at TIMESTAMP)")
    conn.execute(
        "INSERT INTO processed_messages VALUES ('w-old', '+234801', "
        "'text', 'done', 1, NULL, NULL, '2026-09-01 00:00:00')")
    conn.commit()
    conn.close()

    engine = create_engine(f"sqlite:///{f}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE processed_messages ADD COLUMN payload JSON")
    conn = sqlite3.connect(str(f))
    try:
        assert conn.execute(
            "SELECT message_id, status, payload FROM processed_messages"
        ).fetchall() == [("w-old", "done", None)]
    finally:
        conn.close()

    Session = sessionmaker(bind=engine)
    db = Session()
    row = db.query(ProcessedMessage).filter_by(
        message_id="w-old").first()
    assert row.status == "done" and row.payload is None
    row.payload = {"message_id": "w-old", "text": "hi"}
    db.commit()
    assert db.query(ProcessedMessage).filter_by(
        message_id="w-old").first().payload["text"] == "hi"
    db.close()


def test_postgres_additive_statements_present():
    """No live Postgres here, so this pins the exact statements the
    Supabase migration path will run (ADD COLUMN IF NOT EXISTS is
    Postgres-only and can never execute on the SQLite suite)."""
    from pathlib import Path
    parent = Path(main.__file__).parent
    db_src = (parent / "database.py").read_text(encoding="utf-8")
    assert '("processed_messages", "payload", "JSONB")' in db_src
    assert "ADD COLUMN IF NOT EXISTS" in db_src
    sql = (parent / "schema.sql").read_text(encoding="utf-8")
    assert "payload      jsonb" in sql


# ---------------------------------------------------------------------------
# Worker wiring - orphan sweep rides the leader-only run_cycles
# ---------------------------------------------------------------------------
def test_run_cycles_includes_orphan_sweep(monkeypatch):
    import FareBeep.worker as worker_mod

    class _FakeDB:
        def close(self):
            pass

    monkeypatch.setattr(worker_mod, "SessionLocal", lambda: _FakeDB())
    monkeypatch.setattr("FareBeep.transactions.BookingService",
                        lambda db: type("B", (), {
                            "expire_stale_sessions": staticmethod(
                                lambda: 2)})())
    monkeypatch.setattr("FareBeep.status.StatusService",
                        lambda *a, **k: type("S", (), {
                            "run_watch_cycle": lambda self: 1})())
    monkeypatch.setattr("FareBeep.alerts.SubscriptionMonitor",
                        lambda *a, **k: type("M", (), {
                            "run_cycle": lambda self: 3})())
    monkeypatch.setattr("FareBeep.notifier.get_notifier", lambda: object())
    monkeypatch.setattr("FareBeep.status.AviationstackClient", lambda: object())
    swept = {}

    def _fake_recover(db=None, stale_minutes=0):
        swept["cutoff"] = stale_minutes
        return 7

    monkeypatch.setattr(main, "recover_orphaned_inbound", _fake_recover)
    result = worker_mod.run_cycles()
    assert result == {"expired": 2, "status_pushes": 1, "fare_beeps": 3,
                      "orphans_recovered": 7}
    assert swept == {"cutoff": 5}
