"""THE CONVERSATIONAL LAYER - Gemini intent parsing + LOCAL FALLBACK + reply
personality pass. IATA safety stays local regardless of which parser fires."""
from datetime import date, timedelta

import httpx

from FareBeep import brain
from FareBeep.brain import Intent


def test_local_parser_resolves_route_without_gemini():
    """No key, no network: 'Lagos to Abuja tomorrow' must STILL become a fare
    request (the old behavior degraded to a help menu - that's gone)."""
    intent = brain.parse_intent("Lagos to Abuja tomorrow", api_key=None)
    assert intent.intent == "fare"
    assert intent.origin_iata == "LOS"
    assert intent.destination_iata == "ABV"
    assert intent.date == (date.today() + timedelta(days=1)).isoformat()


def test_local_parser_bare_ordinal_day_future_this_month():
    """User asks for 'the 31st' (no month) on Aug 13 -> Aug 31 same year."""
    today = date.today()
    try:
        expected = date(today.year, 8, 31)
        if expected < today:
            expected = date(today.year + 1, 8, 31)
    except ValueError:
        return
    intent = brain._local_parse("fare lagos to abuja on the 31st")
    assert intent.date == expected.isoformat()


def test_local_parser_bare_ordinal_day_past_rolls_to_next_month():
    """'the 5th' on Aug 13 is past -> same day next month (Sep 5)."""
    today = date.today()
    if today.day < 5:
        expected = date(today.year, today.month, 5)
    else:
        expected = date(today.year, today.month + 1, 5) if today.month < 12 \
            else date(today.year + 1, 1, 5)
    intent = brain._local_parse("fare lagos to abuja on the 5th")
    assert intent.date == expected.isoformat()


def test_local_parser_bare_number_is_current_month_day():
    """A PLAIN number with no suffix: '31' = the 31st of the CURRENT month."""
    today = date.today()
    try:
        expected = date(today.year, today.month, 31)
    except ValueError:
        expected = None
    intent = brain._local_parse("fare lagos to abuja 31")
    if expected is None or expected < today:
        month, year = (today.month + 1, today.year) if today.month < 12 \
            else (1, today.year + 1)
        expected = date(year, month, 31)
    assert intent.date == expected.isoformat()


def test_local_parser_bare_number_past_rolls_to_next_month():
    """'5' on Aug 13 is past -> same day next month."""
    today = date.today()
    if today.day < 5:
        expected = date(today.year, today.month, 5)
    else:
        expected = date(today.year, today.month + 1, 5) if today.month < 12 \
            else date(today.year + 1, 1, 5)
    intent = brain._local_parse("book lagos to abuja 5")
    assert intent.intent == "book"
    assert intent.date == expected.isoformat()


def test_local_parser_slash_date_day_month():
    """'31/08' and '31-08' = 31 August (Nigerian day/month order)."""
    today = date.today()
    try:
        expected = date(today.year, 8, 31)
        if expected < today:
            expected = date(today.year + 1, 8, 31)
    except ValueError:
        return
    intent = brain._local_parse("fare lagos to abuja 31/08")
    assert intent.date == expected.isoformat()
    intent = brain._local_parse("fare lagos to abuja 31-08")
    assert intent.date == expected.isoformat()


def test_local_parser_slash_date_us_order():
    """'08/31' (US order) must still resolve to 31 August."""
    today = date.today()
    try:
        expected = date(today.year, 8, 31)
        if expected < today:
            expected = date(today.year + 1, 8, 31)
    except ValueError:
        return
    intent = brain._local_parse("fare lagos to abuja 08/31")
    assert intent.date == expected.isoformat()


def test_local_parser_times_and_prices_are_not_dates():
    """'10am', '10:30' and '80k' must never be read as a day."""
    today = date.today()
    intent = brain._local_parse(
        f"fare lagos to abuja tomorrow at 10am")
    assert intent.date == (today + timedelta(days=1)).isoformat()
    intent = brain._local_parse("fare lagos to abuja 10:30")
    assert intent.date is None
    intent = brain._local_parse("alert me when lagos abuja drops below 80k")
    assert intent.date is None
    assert intent.target_price == 80000.0


def test_local_parser_month_plus_day_still_wins():
    """'31st August' -> Aug 31 (year rolls only if that date is already past)."""
    today = date.today()
    try:
        expected = date(today.year, 8, 31)
        if expected < today:
            expected = date(today.year + 1, 8, 31)
    except ValueError:
        return
    intent = brain._local_parse("fare lagos to abuja 31st of august")
    assert intent.date == expected.isoformat()


def test_local_parser_handles_multiword_cities():
    intent = brain._local_parse("book port harcourt to asaba on friday")
    assert intent.intent == "book"
    assert intent.origin_iata == "PHC"
    assert intent.destination_iata == "ABB"


def test_local_parser_subscribe_with_target():
    intent = brain._local_parse("alert me when lagos to abuja drops below 80k")
    assert intent.intent == "subscribe"
    assert intent.origin_iata == "LOS"
    assert intent.destination_iata == "ABV"
    assert intent.target_price == 80000.0


def test_local_parser_subscribe_explicit_price():
    intent = brain._local_parse("subscribe LOS ABV 60000")
    assert intent.intent == "subscribe"
    assert intent.target_price == 60000.0


def test_local_parser_track_flight():
    intent = brain._local_parse("track P47123")
    assert intent.intent == "status"
    assert intent.flight == "P47123"


def test_local_parser_unsubscribe():
    intent = brain._local_parse("unsubscribe")
    assert intent.intent == "unsubscribe"


def test_local_parser_greeting_is_help():
    intent = brain._local_parse("hello")
    assert intent.intent == "help"


def test_local_parser_destination_only_stays_incomplete():
    """Pass 1 extraction: 'I'm going to Abuja' = destination ONLY. The
    concierge (Pass 2) must ask for the rest - never invent an origin."""
    intent = brain._local_parse("I'm going to Abuja")
    assert intent.intent == "fare"
    assert intent.destination_iata == "ABV"
    assert intent.origin_iata is None
    assert intent.is_partial is True


def test_local_parser_extracts_user_name():
    intent = brain._local_parse("my name is Damilola, Lagos to Abuja tomorrow")
    assert intent.name == "Damilola"
    assert intent.origin_iata == "LOS"
    assert intent.destination_iata == "ABV"


def test_local_parser_weekday_date_next_tuesday():
    from datetime import date, timedelta
    intent = brain._local_parse("find me a flight to abj for next tuesday")
    target = (1 - date.today().weekday()) % 7
    expected = (date.today() + timedelta(days=7 + target)).isoformat()
    assert intent.date == expected


def test_local_parser_weekday_date_this_friday():
    from datetime import date, timedelta
    intent = brain._local_parse("abuja to lagos this friday")
    target = (4 - date.today().weekday()) % 7
    expected = (date.today() + timedelta(days=7 if target == 0 else target)).isoformat()
    assert intent.date == expected


def test_gemini_json_allows_null_origin():
    """Gemini must be allowed to return origin: null - Pass 1 honesty."""
    json = '{"intent":"fare","origin":null,"destination":"Abuja","date":null,"flight":null,"name":null}'
    intent = brain._build_intent(json, "I'm going to Abuja")
    assert intent.destination_iata == "ABV"
    assert intent.origin_iata is None


def test_local_parser_ignores_gibberish():
    assert brain._local_parse("asdf qwerty 123") is None


def test_gemini_success_still_wins_over_local():
    """Gemini JSON is preferred; the local parser is only the fallback."""
    class _FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"candidates": [{"content": {"parts": [{
                "text": '{"intent":"fare","origin":"Abuja","destination":'
                        '"Enugu","date":"2026-08-20","target_price":null,'
                        '"flight":null}'}]}}]}

    class _FakeClient:
        def __init__(self, timeout=None):
            pass

        def post(self, url, json):
            return _FakeResp()

        def close(self):
            pass

    intent = brain.parse_intent("abuja to enugu", api_key="x", model="y",
                                http_client=_FakeClient())
    assert intent.intent == "fare"
    assert intent.date == "2026-08-20"


def test_build_from_gemini_json_resolves_via_local_dict():
    """The LLM returns city names; iata.py (not the LLM) decides the codes."""
    json = '{"intent":"fare","origin":"Abuja","destination":"Port Harcourt","date":"2026-08-20","flight":null}'
    intent = brain._build_intent(json, "abuja to port harcourt tomorrow")
    assert intent.intent == "fare"
    assert intent.origin_iata == "ABV"
    assert intent.destination_iata == "PHC"
    assert intent.date == "2026-08-20"


def test_non_json_gemini_reply_falls_back_to_local():
    """Gemini garbage -> local parser still rescues a clear route."""
    intent = brain.parse_intent("Lagos to Abuja tomorrow",
                                api_key="x", model="y",
                                http_client=_FailingClient())
    assert intent.intent == "fare"
    assert intent.origin_iata == "LOS"


def test_invalid_intent_name_is_clamped():
    intent = brain._build_intent('{"intent":"fart","origin":null,"destination":null,"date":null,"flight":null}', "x")
    assert intent.intent == "help"


def test_concise_prompt_does_not_allow_filler():
    """The efficiency rule: Gemini is told to emit ONLY the JSON object."""
    assert "CONCISE" in brain.SYSTEM_PROMPT
    assert "No preamble" in brain.SYSTEM_PROMPT


class _FailingClient:
    """httpx-like client that always raises - simulates Gemini being down."""

    class _Resp:
        def raise_for_status(self):
            raise httpx.HTTPError("down")

        def json(self):
            return {}

    def __init__(self, timeout=None):
        pass

    def post(self, *a, **k):
        raise httpx.ConnectError("simulated network down")

    def close(self):
        pass


def test_compose_reply_without_key_returns_greeted_template(monkeypatch):
    monkeypatch.setattr(brain, "GEMINI_API_KEY", None)
    out = brain.compose_reply("Fare LOS -> ABV NGN 118,500")
    # no-AI fallback must STILL be concierge-warm: greeting + full template
    assert out.startswith("Beep! 🎫")
    assert "Fare LOS -> ABV NGN 118,500" in out


def test_compose_reply_with_name_greets_personally(monkeypatch):
    monkeypatch.setattr(brain, "GEMINI_API_KEY", None)
    out = brain.compose_reply("Fare LOS -> ABV NGN 118,500", user_name="damilola")
    assert out.startswith("Hi Damilola! 😊")


def test_compose_reply_failure_returns_greeted_template():
    tpl = "Fare LOS -> ABV NGN 118,500"
    out = brain.compose_reply(tpl, api_key="x", model="y",
                              http_client=_FailingClient())
    assert out.startswith("Beep! 🎫")
    assert tpl in out


def test_compose_reply_humanizes_on_success():
    class _FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"candidates": [{"content": {"parts": [
                {"text": "Hi there! Lagos to Abuja today? It's ₦118,500 with "
                         "Air Peace - a solid deal. Check it: "
                         "https://example.com/fare"}]}}]}

    class _FakeClient:
        def __init__(self, timeout=None):
            pass

        def post(self, url, json):
            return _FakeResp()

        def close(self):
            pass

    out = brain.compose_reply("Fare LOS -> ABV NGN 118,500",
                              api_key="x", model="y", http_client=_FakeClient())
    assert "118,500" in out
    # the persona pass must OPEN with a greeting (concierge rule)
    assert out.startswith("Hi there!")