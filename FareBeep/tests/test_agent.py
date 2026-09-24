"""GROQ AGENT - tools, memory, turn loop, and Meta wiring."""
import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from FareBeep import agent as agent_mod
from FareBeep import chatstate, main, transactions
from FareBeep.agent import SYSTEM_PROMPT, agent_reply, build_tools
from FareBeep.models import (Base, BookingSession, FareLedger, Subscription,
                         User, utcnow)


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session


@pytest.fixture
def user(db):
    u = User(phone="+2348012345678")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    from datetime import date
    monkeypatch.setattr(
        transactions, "initialize_paystack_payment",
        lambda ref, total, email: {
            "access_code": f"AC_{ref}",
            "authorization_url": f"https://paystack.com/pay/{ref}"})
    monkeypatch.setattr(agent_mod, "GROQ_API_KEY", "test-groq-key")
    # Freeze "today" before every hardcoded fixture date (2026-08-20 and
    # later): past-date validation must not rot this suite as real time
    # passes the fixture dates.
    monkeypatch.setattr("FareBeep.dates.lagos_today",
                        lambda: date(2026, 8, 1))


OFFER = {"flight_no": "P47123", "airline": "P4",
         "airline_name": "Air Peace",
         "departure_code": "LOS", "arrival_code": "ABV",
         "departure_time": "08:30", "arrival_time": "09:25",
         "duration": "0h 55m", "baggage": "20kg", "cabin": "Economy",
         "price": 98000.0, "currency": "NGN", "booking_token": "btk_aaa"}


class FakeTravels247:
    def __init__(self, offers=None):
        self.offers = offers if offers is not None else [dict(OFFER)]

    async def search_offers(self, *a, **k):
        return [dict(o) for o in self.offers]

    async def verify_price(self, booking_token, **k):
        return {"verified": True, "price_changed": False,
                "original_price": 98000.0, "verified_price": 98000.0,
                "currency": "NGN", "booking_token": "btk_bbb",
                "expires_at": None}

    async def close(self):
        pass


class FakeLLM:
    """Scripted Groq stand-in: strings -> final replies, (name, args)
    tuples -> tool calls. Records every message list it was given."""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.seen.append(list(messages))
        item = self.script.pop(0)
        if isinstance(item, str):
            return AIMessage(content=item)
        name, args = item
        return AIMessage(content="", tool_calls=[
            {"name": name, "args": args,
             "id": f"call-{len(self.seen)}", "type": "tool_call"}])


def _tools(db, user, monkeypatch, offers=None):
    monkeypatch.setattr(agent_mod, "get_inventory_client",
                        lambda *a, **k: FakeTravels247(offers))
    return {t.name: t for t in build_tools(db, user.phone)}


def test_prompt_holds_the_honesty_rules():
    text = SYSTEM_PROMPT.lower()
    assert "never invent" in text
    assert "payment always comes before the ticket" in text
    assert "never repeat a failed tool call identically" in text
    assert "greet them back" in text
    assert "fresh session" in text


def test_search_tool_serves_ledger_hit(db, user, monkeypatch):
    db.add(FareLedger(origin="LOS", destination="ABV",
                      flight_date="2026-08-20", price=98000.0,
                      currency="NGN", airline="Air Peace",
                      verify_link=None, last_updated=utcnow()))
    db.commit()
    tools = _tools(db, user, monkeypatch, offers=[])

    import json
    out = json.loads(tools["search_fares"].invoke(
        {"origin": "Lagos", "destination": "Abuja",
         "flight_date": "2026-08-20"}))

    assert out["found"] is True
    assert out["source"] == "ledger"
    assert out["price_ngn"] == 98000.0
    assert chatstate.get_last_fare(db, user.phone)["price"] == 98000.0


def test_search_tool_miss_goes_247travels_and_caches(db, user, monkeypatch):
    tools = _tools(db, user, monkeypatch)

    import json
    out = json.loads(tools["search_fares"].invoke(
        {"origin": "LOS", "destination": "ABV",
         "flight_date": "2026-08-21"}))

    assert out["found"] is True
    assert out["source"] == "247travels"
    assert out["booking_token"] == "btk_aaa"
    assert db.query(FareLedger).one().price == 98000.0


def test_search_tool_dateless_returns_window_cheapest(db, user, monkeypatch):
    """No date: instant answer from the best known fare - past fares
    excluded, no live calls, last_fare recorded for a bare BOOK."""
    from datetime import timedelta
    day1 = (utcnow() + timedelta(days=3)).strftime("%Y-%m-%d")
    day2 = (utcnow() + timedelta(days=5)).strftime("%Y-%m-%d")
    past = (utcnow() - timedelta(days=2)).strftime("%Y-%m-%d")
    for fd, price in ((day1, 98000.0), (day2, 75000.0), (past, 50000.0)):
        db.add(FareLedger(origin="LOS", destination="ABV", flight_date=fd,
                          price=price, currency="NGN", airline="Air Peace",
                          verify_link=None, last_updated=utcnow()))
    db.commit()

    def _no_live(*a, **k):
        raise AssertionError("dateless search must stay on the ledger")

    monkeypatch.setattr(agent_mod, "get_inventory_client", _no_live)
    tools = {t.name: t for t in build_tools(db, user.phone)}

    import json
    out = json.loads(tools["search_fares"].invoke(
        {"origin": "Lagos", "destination": "Abuja", "flight_date": ""}))

    assert out["found"] is True
    assert out["source"] == "ledger"
    assert out["price_ngn"] == 75000.0
    assert out["flight_date"] == day2
    assert isinstance(out["flight_date"], str)  # PG DATE rows coerce
    assert chatstate.get_last_fare(db, user.phone)["price"] == 75000.0


def test_search_tool_dateless_empty_ledger_asks_for_date(db, user, monkeypatch):
    """No date and nothing known: say so and ask for a date - still no
    live calls (a blind multi-date scan would be slow and costly)."""

    def _no_live(*a, **k):
        raise AssertionError("dateless search must stay on the ledger")

    monkeypatch.setattr(agent_mod, "get_inventory_client", _no_live)
    tools = {t.name: t for t in build_tools(db, user.phone)}

    import json
    out = json.loads(tools["search_fares"].invoke(
        {"origin": "LOS", "destination": "ABV"}))

    assert out["found"] is False
    assert "travel date" in out["error"]


def test_reserve_tool_locks_and_links(db, user, monkeypatch):
    tools = _tools(db, user, monkeypatch)

    import json
    out = json.loads(tools["reserve_fare"].invoke(
        {"booking_token": "btk_aaa", "origin": "LOS",
         "destination": "ABV", "flight_date": "2026-08-21",
         "passenger_name": "Ada Obi"}))

    assert out["locked"] is True
    assert out["payment_link"].startswith("https://paystack.com/pay/FB-")
    session = db.query(BookingSession).one()
    assert session.flight_details["booking_token"] == "btk_bbb"
    assert session.flight_details["travellers"]["primary_guest"][
        "first_name"] == "Ada"


def test_reserve_tool_refuses_without_name(db, user, monkeypatch):
    tools = _tools(db, user, monkeypatch)

    import json
    out = json.loads(tools["reserve_fare"].invoke(
        {"booking_token": "btk_aaa", "origin": "LOS",
         "destination": "ABV", "flight_date": "2026-08-21",
         "passenger_name": ""}))

    assert out["locked"] is False
    assert db.query(BookingSession).count() == 0


def test_agent_reply_runs_tool_then_answers(db, user, monkeypatch):
    monkeypatch.setattr(agent_mod, "get_inventory_client",
                        lambda *a, **k: FakeTravels247())
    llm = FakeLLM([
        ("search_fares", {"origin": "Lagos", "destination": "Abuja",
                          "flight_date": "2026-08-21"}),
        "Air Peace at NGN 98,000. Want it?",
    ])

    reply = agent_reply(db, user, "Lagos to Abuja tomorrow", llm=llm)

    assert reply == "Air Peace at NGN 98,000. Want it?"
    assert chatstate.get_agent_history(db, user.phone) == [
        {"role": "user", "content": "Lagos to Abuja tomorrow"},
        {"role": "assistant", "content": reply}]


def test_agent_reply_carries_history_into_next_turn(db, user, monkeypatch):
    monkeypatch.setattr(agent_mod, "get_inventory_client",
                        lambda *a, **k: FakeTravels247())
    llm = FakeLLM(["first answer", "second answer"])

    agent_reply(db, user, "hello", llm=llm)
    agent_reply(db, user, "and tomorrow?", llm=llm)

    second_turn = llm.seen[1]
    assert len(second_turn) == 4   # system + prior user/ai + new user
    assert second_turn[1].content == "hello"
    assert second_turn[2].content == "first answer"
    assert len(chatstate.get_agent_history(db, user.phone)) == 4


def test_agent_reply_unknown_tool_recovers(db, user, monkeypatch):
    llm = FakeLLM([("nope", {}), "Let me check that properly."])

    reply = agent_reply(db, user, "hi", llm=llm)

    assert reply == "Let me check that properly."


def test_meta_message_routes_to_agent_when_configured(monkeypatch,
                                                      session_factory):
    """GROQ key set -> the agent owns the turn, Gemini never runs."""
    monkeypatch.setattr(main, "SessionLocal", session_factory)
    monkeypatch.setattr(main, "GROQ_API_KEY", "test-groq-key")
    monkeypatch.setattr("FareBeep.agent.agent_reply",
                        lambda db, user, text: "agent says hi")
    monkeypatch.setattr(
        main.brain, "parse_intent",
        lambda text: (_ for _ in ()).throw(AssertionError("brain ran")))
    sent = []
    monkeypatch.setattr(main.notifier, "send_text",
                        lambda to, body: sent.append((to, body)))

    main._handle_incoming_message("+2348012345678", "hello")

    assert sent == [("+2348012345678", "agent says hi")]


def test_repeated_tool_errors_get_trouble_not_fallback(db, user, monkeypatch):
    """Backend down on every round: an honest trouble message (recorded
    in history), never the blame-the-user "send your route again"."""
    def _boom(*a, **k):
        raise RuntimeError("247travels down")

    monkeypatch.setattr(agent_mod, "get_inventory_client", _boom)
    llm = FakeLLM([("search_fares", {"origin": "ABV",
                                     "destination": "PHC",
                                     "flight_date": "2026-09-08"})] * 4)
    reply = agent_reply(db, user, "Abuja to Port Harcourt tomorrow", llm=llm)
    assert reply == agent_mod.TROUBLE
    assert "route again" not in reply
    hist = chatstate.get_agent_history(db, user.phone)
    assert hist[-1]["content"] == agent_mod.TROUBLE


def test_live_search_and_close_share_one_loop(db, user, monkeypatch):
    """REGRESSION: search_offers() + close() in two separate asyncio.run()
    calls crash with "Event loop is closed" once a real connection pool
    is bound - which failed EVERY live search (bot answered FALLBACK).
    Served by a local stdlib HTTP server: no mocks, no network."""
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    offer = {"flight_no": "P47123", "airline": "P4",
             "airline_name": "Air Peace", "price": 98000.0,
             "currency": "NGN", "booking_token": "btk_live"}

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, payload):
            body = _json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            if self.path == "/api/login":
                self._send({"data": {"access_token": "T",
                                     "expires_in": 900}})
            elif self.path == "/api/flights/search":
                self._send({"data": {"flights": [offer]}})
            else:
                self.send_error(404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from FareBeep.travels247 import Travels247Client as real_cls
        base = f"http://127.0.0.1:{server.server_port}/api"
        monkeypatch.setattr(
            agent_mod, "get_inventory_client",
            lambda *a, **k: real_cls(base_url=base, email="e@x.com",
                                     password="pw"))
        tools = {t.name: t for t in build_tools(db, user.phone)}
        import json
        out = json.loads(tools["search_fares"].invoke(
            {"origin": "LOS", "destination": "ABV",
             "flight_date": "2026-09-14"}))
        assert out["found"] is True
        assert out["price_ngn"] == 98000.0
        # And again: clients are per-call, every call must survive close.
        out2 = json.loads(tools["search_fares"].invoke(
            {"origin": "LOS", "destination": "ABV",
             "flight_date": "2026-09-14"}))
        assert out2["found"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_search_summary_includes_baggage_and_seats(db, user, monkeypatch):
    """Live-offer extras ride into the quoted summary line."""
    rich = {"flight_no": "QI300", "airline": "QI",
            "airline_name": "Ibom Air",
            "departure_code": "LOS", "arrival_code": "ABV",
            "departure_time": "07:00 am", "arrival_time": "08:15 am",
            "price": 108402.0, "currency": "NGN",
            "baggage": "20 kg", "seats_left": 9,
            "booking_token": "btk_rich"}
    tools = _tools(db, user, monkeypatch, offers=[rich])

    import json
    out = json.loads(tools["search_fares"].invoke(
        {"origin": "LOS", "destination": "ABV",
         "flight_date": "2026-09-14"}))

    assert out["found"] is True
    assert "20 kg checked" in out["summary"]
    assert "only 9 left" in out["summary"]


def test_subscribe_tool_creates_watch(db, user, monkeypatch):
    tools = {t.name: t for t in build_tools(db, user.phone)}

    import json
    out = json.loads(tools["subscribe_alerts"].invoke(
        {"origin": "Lagos", "destination": "Abuja",
         "target_price": 80000.0, "flight_date": "2026-09-14"}))

    assert out["subscribed"] is True
    assert out["target_price"] == 80000.0
    sub = db.query(Subscription).filter_by(user_id=user.user_id).one()
    assert (sub.origin, sub.destination) == ("LOS", "ABV")
    assert sub.target_price == 80000.0


def test_subscribe_tool_drop_watch_needs_no_target(db, user, monkeypatch):
    tools = {t.name: t for t in build_tools(db, user.phone)}

    import json
    out = json.loads(tools["subscribe_alerts"].invoke(
        {"origin": "LOS", "destination": "ABV"}))

    assert out["subscribed"] is True
    assert out["target_price"] is None
    assert "genuine drop" in out["summary"]


def test_subscribe_tool_fairytale_target_warns(db, user, monkeypatch):
    from datetime import timedelta
    day = (utcnow() + timedelta(days=3)).strftime("%Y-%m-%d")
    db.add(FareLedger(origin="LOS", destination="ABV", flight_date=day,
                      price=100000.0, currency="NGN", airline="Air Peace",
                      last_updated=utcnow()))
    db.commit()
    tools = {t.name: t for t in build_tools(db, user.phone)}

    import json
    out = json.loads(tools["subscribe_alerts"].invoke(
        {"origin": "LOS", "destination": "ABV",
         "target_price": 50000.0}))

    assert out["subscribed"] is True  # armed anyway, warned honestly
    assert out["realism_warning"] is not None
    assert "50%" in out["realism_warning"]
    sub = db.query(Subscription).filter_by(user_id=user.user_id).one()
    assert sub.target_price == 50000.0


def test_subscribe_tool_sane_target_no_warning(db, user, monkeypatch):
    from datetime import timedelta
    day = (utcnow() + timedelta(days=3)).strftime("%Y-%m-%d")
    db.add(FareLedger(origin="LOS", destination="ABV", flight_date=day,
                      price=100000.0, currency="NGN", airline="Air Peace",
                      last_updated=utcnow()))
    db.commit()
    tools = {t.name: t for t in build_tools(db, user.phone)}

    import json
    out = json.loads(tools["subscribe_alerts"].invoke(
        {"origin": "LOS", "destination": "ABV",
         "target_price": 95000.0}))

    assert out["subscribed"] is True
    assert out["realism_warning"] is None


def test_subscribe_tool_needs_user_and_route(db, user, monkeypatch):
    tools = {t.name: t for t in build_tools(db, "+2348000000099")}

    import json
    out = json.loads(tools["subscribe_alerts"].invoke(
        {"origin": "LOS", "destination": "ABV"}))
    assert out["subscribed"] is False  # unknown phone, no User row

    tools = {t.name: t for t in build_tools(db, user.phone)}
    out = json.loads(tools["subscribe_alerts"].invoke(
        {"origin": "LOS", "destination": ""}))
    assert out["subscribed"] is False


def _seed_watch(db, user, origin="LOS", destination="ABV", target=80000.0):
    db.add(Subscription(user_id=user.user_id, origin=origin,
                        destination=destination, target_price=target))
    db.commit()


def test_manage_tool_lists_pause_resume_cancel(db, user, monkeypatch):
    _seed_watch(db, user)
    tools = {t.name: t for t in build_tools(db, user.phone)}

    import json
    out = json.loads(tools["manage_alerts"].invoke({"action": "list"}))
    assert len(out["watches"]) == 1
    assert out["watches"][0]["route"] == "LOS->ABV"

    out = json.loads(tools["manage_alerts"].invoke(
        {"action": "pause", "number": 1}))
    assert "Paused" in out["summary"]
    assert db.query(Subscription).one().paused is True

    out = json.loads(tools["manage_alerts"].invoke(
        {"action": "resume", "origin": "Lagos", "destination": "Abuja"}))
    assert "Resumed" in out["summary"]
    assert db.query(Subscription).one().paused is False

    out = json.loads(tools["manage_alerts"].invoke(
        {"action": "cancel", "number": 1}))
    assert "Cancelled" in out["summary"]
    assert db.query(Subscription).count() == 0


def test_manage_tool_edit_and_dropwatch(db, user, monkeypatch):
    _seed_watch(db, user)
    tools = {t.name: t for t in build_tools(db, user.phone)}

    import json
    out = json.loads(tools["manage_alerts"].invoke(
        {"action": "edit", "number": 1, "target_price": 70000.0}))
    assert "70,000" in out["summary"]
    assert db.query(Subscription).one().target_price == 70000.0

    out = json.loads(tools["manage_alerts"].invoke(
        {"action": "dropwatch", "number": 1}))
    assert "genuine drop" in out["summary"]
    assert db.query(Subscription).one().target_price is None


def test_manage_tool_ambiguous_and_unknown(db, user, monkeypatch):
    _seed_watch(db, user)
    _seed_watch(db, user, destination="PHC", target=None)
    tools = {t.name: t for t in build_tools(db, user.phone)}

    import json
    out = json.loads(tools["manage_alerts"].invoke(
        {"action": "pause", "origin": "Lagos"}))
    assert out["ok"] is False  # names both watches: ask, don't guess
    assert db.query(Subscription).filter_by(paused=True).count() == 0

    out = json.loads(tools["manage_alerts"].invoke({"action": "explode"}))
    assert out["ok"] is False


def test_agent_turn_pauses_watch(db, user, monkeypatch):
    _seed_watch(db, user)
    llm = FakeLLM([("manage_alerts", {"action": "pause", "number": 1}),
                   "Paused your Lagos watch."])
    reply = agent_reply(db, user, "pause my lagos alert", llm=llm)
    assert "Paused your Lagos" in reply
    assert db.query(Subscription).one().paused is True
