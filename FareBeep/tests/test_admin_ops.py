"""ADMIN OPS - visibility + replay verification.

Answers, with tests:
  Q1 no ADMIN_TOKEN (or wrong header) -> 404, and the surface never leaks.
  Q2 the snapshot reflects real dispatch state: queued/failed counts,
     dead letters with replayability, beeps, bookings.
  Q3 replay re-dispatches a payload-backed dead letter and marks it done.
  Q4 a dead letter with NO stored payload refuses to replay (explicit
     reason), and a non-dead id 404s.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from FareBeep import main
from FareBeep.models import Base, ChatState, ProcessedMessage, SessionStatus, \
    Subscription, User, utcnow


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
    monkeypatch.setattr(main, "SessionLocal", session_factory)
    monkeypatch.setattr(main, "ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setattr(main.brain, "GEMINI_API_KEY", None)
    monkeypatch.setattr(main, "GROQ_API_KEY", None)
    return TestClient(main.app)


H = {"X-Admin-Token": "test-admin-token"}


def _seed_dead(db, mid, phone, payload=None):
    db.add(ProcessedMessage(
        message_id=mid, phone=phone, status="failed", attempts=3,
        last_error="boom", payload=payload, created_at=utcnow()))
    db.commit()


def _seed_queued(db, mid, phone):
    db.add(ProcessedMessage(
        message_id=mid, phone=phone, status="queued", attempts=0,
        created_at=utcnow()))
    db.commit()


def test_admin_closed_without_token(client, monkeypatch):
    monkeypatch.setattr(main, "ADMIN_TOKEN", None)
    assert client.get("/admin/ops", headers=H).status_code == 404
    assert client.get("/admin/ops").status_code == 404
    r = client.post("/admin/ops/dead-letters/x/replay", headers=H)
    assert r.status_code == 404


def test_cockpit_page_gate_and_shell(client, monkeypatch):
    """The cockpit shell follows the same closed-surface rule: 404 when
    ADMIN_TOKEN is unset (even with a header). When open, the page is
    served no-store and must NOT contain the token - the data still
    demands the header via fetch."""
    monkeypatch.setattr(main, "ADMIN_TOKEN", None)
    assert client.get("/admin", headers=H).status_code == 404
    assert client.get("/admin").status_code == 404

    monkeypatch.setattr(main, "ADMIN_TOKEN", "test-admin-token")
    r = client.get("/admin")
    assert r.status_code == 200
    assert "Ops cockpit" in r.text
    assert "test-admin-token" not in r.text
    assert r.headers["cache-control"] == "no-store"
    assert client.get("/admin",
                      headers={"X-Admin-Token": "nope"}).status_code == 200, \
        "the shell is public-ish; the DATA is what the token guards"


def test_admin_wrong_token_404(client):
    assert client.get("/admin/ops",
                      headers={"X-Admin-Token": "nope"}).status_code == 404
    assert client.get("/admin/ops").status_code == 404


def test_ops_snapshot_reflects_state(client, session_factory):
    db = session_factory()
    _seed_dead(db, "DL1", "+234801", {"message_id": "DL1",
                                      "from_number": "+234801"})
    _seed_dead(db, "DL2", "+234802", None)  # pre-recovery row: no payload
    _seed_queued(db, "Q1", "+234801")
    u = User(phone="+234801")
    db.add(u)
    db.commit()
    db.add(Subscription(user_id=u.user_id, origin="LOS", destination="ABV",
                        paused=False))
    db.add(Subscription(user_id=u.user_id, origin="LOS", destination="PHC",
                        paused=True))
    db.commit()
    db.close()

    r = client.get("/admin/ops", headers=H)
    assert r.status_code == 200
    snap = r.json()
    assert snap["inbound"]["by_status"] == {"failed": 2, "queued": 1}
    assert snap["inbound"]["stale_queued_5m"] == 0
    dead = {d["message_id"]: d for d in snap["dead_letters_recent"]}
    assert dead["DL1"]["replayable"] is True
    assert dead["DL2"]["replayable"] is False
    assert dead["DL1"]["attempts"] == 3
    assert snap["beeps"] == {"total": 2, "active": 1, "paused": 1}
    assert snap["bookings"] == {}


def test_ops_support_rows_carry_transcript_and_relay(client, session_factory):
    """The cockpit's loop-closer: an open thread surfaces with its
    transcript tail and the exact relay command, so the human reads
    context and answers without leaving the page."""
    db = session_factory()
    db.add(ChatState(phone="+234801", support_ticket={
        "id": "SUP-ABC123", "opened_at": utcnow().isoformat(),
        "trigger": "auto: payment/refund", "status": "open",
        "messages": [
            {"side": "user", "at": utcnow().isoformat(),
             "text": "I was charged twice"},
            {"side": "admin", "at": utcnow().isoformat(),
             "text": "checking now"},
        ]}))
    db.commit()
    db.close()

    snap = client.get("/admin/ops", headers=H).json()
    rows = snap["support"]["open_threads"]
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "SUP-ABC123"
    assert row["phone"] == "+234801"
    assert row["messages_count"] == 2
    assert [m["side"] for m in row["messages"]] == ["user", "admin"]
    assert row["messages"][0]["text"] == "I was charged twice"
    assert row["relay"] == "R +234801 "


def test_replay_redispatches_and_marks_done(client, session_factory,
                                            monkeypatch):
    payload = {"message_id": "DL1", "from_number": "+234801",
               "message_type": "text", "text": "hi", "raw": {}}
    db = session_factory()
    _seed_dead(db, "DL1", "+234801", payload)
    db.close()

    seen = []

    def fake_dispatch(m):
        seen.append(m.message_id)

    monkeypatch.setattr(main, "_dispatch_single", fake_dispatch)

    r = client.post("/admin/ops/dead-letters/DL1/replay", headers=H)
    assert r.status_code == 200
    assert r.json()["replayed"] is True
    assert seen == ["DL1"]

    db = session_factory()
    row = db.get(ProcessedMessage, "DL1")
    assert row.status == "done"
    db.close()

    # replaying again now 404s: it is no longer a dead letter
    r = client.post("/admin/ops/dead-letters/DL1/replay", headers=H)
    assert r.status_code == 404


def test_replay_refuses_payloadless_and_unknown(client, session_factory):
    db = session_factory()
    _seed_dead(db, "DL2", "+234802", None)
    db.close()

    r = client.post("/admin/ops/dead-letters/DL2/replay", headers=H)
    assert r.status_code == 200
    assert r.json() == {"replayed": False,
                        "reason": "no stored payload (pre-recovery row)"}

    r = client.post("/admin/ops/dead-letters/UNKNOWN/replay", headers=H)
    assert r.status_code == 404


def test_booking_status_key_is_plain_string(client, session_factory):
    from FareBeep.models import BookingSession
    import uuid
    db = session_factory()
    u = User(phone="+234809")
    db.add(u)
    db.commit()
    db.add(BookingSession(
        id=uuid.uuid4(), user_id=u.user_id,
        payment_ref="FB-test", total_price=50000.0,
        status=SessionStatus.PENDING))
    db.commit()
    db.close()

    snap = client.get("/admin/ops", headers=H).json()
    assert snap["bookings"] == {"pending": 1}
