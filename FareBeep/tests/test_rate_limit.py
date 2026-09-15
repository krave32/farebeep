"""RATE LIMIT - per-phone anti-abuse verification.

Answers, with tests:
  Q1 under the limit every message is processed; over it, throttled.
  Q2 exactly ONE cooldown note per window (silence after it), and the
     window sliding means the phone recovers without a restart.
  Q3 STOP is never throttled - even mid-flood, opting out works.
  Q4 throttling is per-phone: a flooder does not silence anyone else.
  Q5 end-to-end: a throttled turn never reaches the concierge logic.
"""
import pytest

from FareBeep import main


@pytest.fixture
def clock(monkeypatch):
    """Controllable time.monotonic inside main's namespace only."""
    state = {"now": 1000.0}

    monkeypatch.setattr(main, "time", type("T", (), {
        "monotonic": staticmethod(lambda: state["now"])}))
    return state


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(main, "_say", lambda p, m, n=None, h=False:
                        out.append((p, m)))
    return out


@pytest.fixture(autouse=True)
def _clean_buckets():
    main._rate_hits.clear()
    main._rate_cooled.clear()
    yield
    main._rate_hits.clear()
    main._rate_cooled.clear()


def test_under_limit_processed_over_limit_throttled(clock, sent,
                                                    monkeypatch):
    monkeypatch.setattr(main, "_RATE_LIMIT", 3)
    for i in range(3):
        assert main._rate_allow("+2341", f"msg {i}") is True
    assert main._rate_allow("+2341", "one more") is False
    assert main._rate_allow("+2341", "again") is False


def test_one_cooldown_note_per_window_then_recovery(clock, sent,
                                                    monkeypatch):
    monkeypatch.setattr(main, "_RATE_LIMIT", 2)
    for i in range(3):  # 2 pass, 3rd throttles
        main._rate_allow("+2341", f"m{i}")
    assert len(sent) == 1
    assert main._rate_allow("+2341", "more") is False
    assert len(sent) == 1, "cooldown note repeated within the window"

    clock["now"] += 61  # window fully slid past
    assert main._rate_allow("+2341", "fresh") is True
    assert main._rate_allow("+2341", "and more") is True
    assert main._rate_allow("+2341", "capped again") is False
    assert len(sent) == 2, "new window gets a fresh cooldown note"


def test_stop_never_throttled(clock, sent, monkeypatch):
    monkeypatch.setattr(main, "_RATE_LIMIT", 1)
    assert main._rate_allow("+2341", "hi") is True
    for _ in range(5):
        assert main._rate_allow("+2341", "STOP") is True
    assert sent == [], "STOP path must not leak a cooldown note"


def test_per_phone_isolation(clock, sent, monkeypatch):
    monkeypatch.setattr(main, "_RATE_LIMIT", 1)
    assert main._rate_allow("+2341", "hi") is True
    assert main._rate_allow("+2341", "flood") is False
    assert main._rate_allow("+2342", "hi") is True, \
        "a flooder must not throttle another phone"


def test_throttled_turn_never_reaches_concierge(clock, monkeypatch,
                                                session_factory=None):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from FareBeep.models import Base

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(main, "SessionLocal", factory)
    monkeypatch.setattr(main, "ADMIN_TOKEN", None)
    monkeypatch.setattr(main.brain, "GEMINI_API_KEY", None)
    monkeypatch.setattr(main, "GROQ_API_KEY", None)
    monkeypatch.setattr(main, "_RATE_LIMIT", 1)

    reached = []
    monkeypatch.setattr(main, "_try_pick",
                        lambda db, text, phone: reached.append(text) or None)

    client = TestClient(main.app)
    assert main._handle_incoming_message("+2341", "first") is None
    assert reached == ["first"]
    main._handle_incoming_message("+2341", "second")  # throttled
    assert reached == ["first"], "throttled message reached the concierge"
