"""SHARED DATES - strict tool validation, Lagos today, one expression parser.

Task #4: prices/dates/actions are validated Python, never model output.
The agent tools and brain._local_date resolve through FareBeep.dates, so
both paths agree on every expression - including "next tomorrow".
"""
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from langchain_core.messages import AIMessage

from FareBeep import agent as agent_mod
from FareBeep.agent import SYSTEM_PROMPT, agent_reply, build_tools
from FareBeep.dates import (DateError, lagos_today, parse_expression,
                            validate_adults, validate_flight_date)

T0 = date(2026, 9, 12)  # a Saturday; pinned so weekday math is stable


@pytest.fixture
def user(db):
    from FareBeep.models import User
    u = User(phone="+2348012345678")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_lagos_today_is_a_date_in_lagos():
    assert lagos_today() == datetime.now().astimezone(
        ZoneInfo("Africa/Lagos")).date()


def test_strict_accepts_real_future_date():
    assert validate_flight_date("2026-09-20", today=T0) == "2026-09-20"
    assert validate_flight_date("2026-09-12", today=T0) == "2026-09-12"


@pytest.mark.parametrize("bad", ["20-09-2026", "20/09/2026", "tomorrow",
                                 "next friday", "", "  ", "2026-9-2",
                                 "2026-13-01", "2026-02-30", "2026-00-10",
                                 None, 20260920])
def test_strict_rejects_shape_and_unreal(bad):
    with pytest.raises(DateError):
        validate_flight_date(bad, today=T0)


def test_strict_rejects_past():
    with pytest.raises(DateError, match="past"):
        validate_flight_date("2026-09-11", today=T0)


def test_adults_bounds():
    assert validate_adults(None) == 1
    assert validate_adults(2) == 2
    for bad in (0, -1, 10, "two", "x", 2.5):
        with pytest.raises(DateError):
            validate_adults(bad)


def test_next_tomorrow_is_day_after_tomorrow():
    assert parse_expression("next tomorrow", today=T0) == "2026-09-14"
    assert parse_expression("day after tomorrow", today=T0) == "2026-09-14"
    assert parse_expression("tomorrow", today=T0) == "2026-09-13"
    assert parse_expression("Lagos to Abuja next tomorrow",
                            today=T0) == "2026-09-14"


def test_relative_weekday_rules():
    # T0 is Saturday 2026-09-12: upcoming Friday is Sep 18 either way.
    assert parse_expression("friday", today=T0) == "2026-09-18"
    assert parse_expression("today", today=T0) == "2026-09-12"
    # Mid-week they split: Wed 2026-09-09, bare "friday" is 2 days out
    # but "next friday" is Friday of next calendar week.
    wed = date(2026, 9, 9)
    assert parse_expression("friday", today=wed) == "2026-09-11"
    assert parse_expression("next friday", today=wed) == "2026-09-18"


def test_prices_and_times_are_not_dates():
    assert parse_expression("below 80k", today=T0) is None
    assert parse_expression("10:30 flight", today=T0) is None
    assert parse_expression("15000 naira", today=T0) is None


def test_brain_wrapper_defaults_to_lagos():
    from FareBeep import brain
    assert brain._local_date("tomorrow") == (
        lagos_today() + timedelta(days=1)).isoformat()
    assert brain._local_date("next tomorrow") == (
        lagos_today() + timedelta(days=2)).isoformat()


def _tools(db, user, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("invalid dates must never reach live APIs")

    monkeypatch.setattr(agent_mod, "get_inventory_client", _boom)
    return {t.name: t for t in build_tools(db, user.phone)}


def test_search_tool_rejects_bad_date(db, user, monkeypatch):
    tools = _tools(db, user, monkeypatch)
    out = json.loads(tools["search_fares"].invoke(
        {"origin": "LOS", "destination": "ABV",
         "flight_date": "tomorrow"}))
    assert out["found"] is False and "bad date" in out["error"]
    out = json.loads(tools["search_fares"].invoke(
        {"origin": "LOS", "destination": "ABV",
         "flight_date": "2020-01-01"}))
    assert out["found"] is False and "past" in out["error"]


def test_reserve_tool_rejects_bad_date(db, user, monkeypatch):
    tools = _tools(db, user, monkeypatch)
    out = json.loads(tools["reserve_fare"].invoke(
        {"booking_token": "btk_x", "origin": "LOS", "destination": "ABV",
         "flight_date": "31-02-2026", "passenger_name": "Ada Obi"}))
    assert out["locked"] is False and "bad date" in out["error"]


def test_subscribe_tool_rejects_bad_date(db, user, monkeypatch):
    tools = _tools(db, user, monkeypatch)
    out = json.loads(tools["subscribe_alerts"].invoke(
        {"origin": "LOS", "destination": "ABV", "flight_date": "whenever"}))
    assert out["subscribed"] is False and "bad date" in out["error"]


class _ScriptedLLM:
    def __init__(self, script):
        self.script = list(script)

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        item = self.script.pop(0)
        if isinstance(item, str):
            return AIMessage(content=item)
        name, args = item
        return AIMessage(content="", tool_calls=[
            {"name": name, "args": args, "id": "call-1",
             "type": "tool_call"}])


def test_reply_carries_tool_price_verbatim(db, user, monkeypatch):
    """The model reads the figure out; the figure itself comes from the
    tool result. This pins the exact digits end to end with a fake LLM."""
    from FareBeep.models import FareLedger
    from FareBeep.models import utcnow
    monkeypatch.setattr(agent_mod, "GROQ_API_KEY", "test-key")
    db.add(FareLedger(origin="LOS", destination="ABV",
                      flight_date="2026-10-02", price=87500.0,
                      currency="NGN", airline="Rano Air", verify_link=None,
                      last_updated=utcnow()))
    db.commit()
    llm = _ScriptedLLM([
        ("search_fares", {"origin": "Lagos", "destination": "Abuja",
                          "flight_date": "2026-10-02"}),
        "Rano Air does LOS->ABV on 2026-10-02 for NGN 87,500. Want it?",
    ])
    monkeypatch.setattr("FareBeep.dates.lagos_today",
                        lambda: date(2026, 9, 1))
    reply = agent_reply(db, user, "Lagos to Abuja on 2026-10-02", llm=llm)
    assert "87,500" in reply  # exact tool figure, not a paraphrase


def test_prompt_teaches_next_tomorrow_correctly():
    assert "DAY AFTER tomorrow" in SYSTEM_PROMPT
    assert '"next tomorrow") means tomorrow' not in SYSTEM_PROMPT.lower()


def test_ambiguous_slash_reads_both_ways():
    from FareBeep.dates import ambiguous_date_hint
    hint = ambiguous_date_hint("Lagos to Abuja 05/06", today=T0)
    assert hint is not None and "5 June" in hint and "6 May" in hint
    # One-way readings never ask: month-first impossible, day-first only.
    assert ambiguous_date_hint("Lagos to Abuja 08/31", today=T0) is None
    assert ambiguous_date_hint("Lagos to Abuja 13/06", today=T0) is None
    assert ambiguous_date_hint("Lagos to Abuja 06/06", today=T0) is None
    # An explicit ISO date wins outright.
    assert ambiguous_date_hint("Lagos to Abuja 05/06 on 2026-10-02",
                               today=T0) is None
    # No slash, no question.
    assert ambiguous_date_hint("Lagos to Abuja tomorrow", today=T0) is None


def test_local_parse_flags_ambiguous_slash():
    from FareBeep.brain import parse_intent
    intent = parse_intent("Lagos to Abuja 05/06", force_local=True)
    assert intent.intent == "fare" and intent.date is None
    assert intent.date_hint and "June" in intent.date_hint


def test_local_parse_leaves_clear_dates_alone():
    from FareBeep.brain import parse_intent
    intent = parse_intent("Lagos to Abuja 08/31", force_local=True)
    assert intent.date_hint is None and intent.date is not None
    intent = parse_intent("Lagos to Abuja tomorrow", force_local=True)
    assert intent.date_hint is None and intent.date is not None


def test_today_helpers_use_lagos():
    from FareBeep import brain
    from FareBeep.agent import _today as agent_today
    assert brain._today() == lagos_today().isoformat()
    assert lagos_today().strftime("%d %B %Y") in agent_today()
