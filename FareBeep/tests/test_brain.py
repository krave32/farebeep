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


def test_local_parser_next_week_weekday_is_the_following_week():
    """'next week thursday' = the Thursday of the FOLLOWING week (not this
    week's, not a random +7 days)."""
    today = date.today()
    expected = today + timedelta(days=7 + ((3 - today.weekday()) % 7))
    assert brain._local_date("next week thursday") == expected.isoformat()
    intent = brain._local_parse("i am going to lagos on next week thursday")
    assert intent.intent == "fare"
    assert intent.destination_iata == "LOS"
    assert intent.origin_iata is None          # going TO Lagos, origin unknown
    assert intent.date == expected.isoformat()


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


def test_local_parser_i_am_name_not_a_verb():
    """'I'm Tunde' captures the name; 'I'm going to Abuja' must NOT."""
    intent = brain._local_parse("I'm Tunde")
    assert intent.name == "Tunde"
    intent = brain._local_parse("i am going to abuja tomorrow")
    assert intent.name is None
    assert intent.intent == "fare"
    assert intent.destination_iata == "ABV"


# ---------------------------------------------------------------------------
# SENTENCE UNDERSTANDING - the parser must read sentences, not just keywords
# ---------------------------------------------------------------------------

def test_sentence_booking_status_is_status_not_book():
    """'check my booking' is about an EXISTING booking - status, never book."""
    for msg in ("check my booking", "what is the status of my booking",
                "track my booking", "my booking status please"):
        intent = brain._local_parse(msg)
        assert intent.intent == "status", msg


def test_sentence_bare_status_and_flight_phrases():
    for msg in ("status", "where is my flight", "when is my flight",
                "is my flight on time", "flight status", "track my flight",
                "when is my flight to Abuja"):
        intent = brain._local_parse(msg)
        assert intent.intent == "status", msg


def test_sentence_price_question_is_fare_not_status():
    """'how much is my flight' asks a PRICE question - fare, not status."""
    intent = brain._local_parse("how much is my flight to Abuja")
    assert intent.intent == "fare"
    assert intent.destination_iata == "ABV"


def test_sentence_price_query_without_route_verbs():
    """'Lagos Abuja price' has no to/from/fly - must still be a fare."""
    intent = brain._local_parse("Lagos Abuja price")
    assert intent.intent == "fare"
    assert intent.origin_iata == "LOS"
    assert intent.destination_iata == "ABV"
    intent = brain._local_parse("what's the cost from Lagos to Abuja on Friday")
    assert intent.intent == "fare"


def test_sentence_subscribe_phrasing():
    """'I want to be notified when it drops below 100k' - subscribe."""
    for msg in ("notify me when lagos to abuja drops below 100k",
                "i want to be notified when it drops below 100k",
                "let me know when lagos abuja goes below 60k",
                "alert me at 15000 for lagos abuja"):
        intent = brain._local_parse(msg)
        assert intent.intent == "subscribe", msg
    intent = brain._local_parse("notify me when lagos to abuja drops below 100k")
    assert intent.target_price == 100000.0


def test_sentence_track_route_is_subscribe_track_flight_is_status():
    intent = brain._local_parse("track lagos to abuja")
    assert intent.intent == "subscribe"
    assert intent.origin_iata == "LOS"
    assert intent.destination_iata == "ABV"
    intent = brain._local_parse("track P47123")
    assert intent.intent == "status"
    assert intent.flight == "P47123"


def test_sentence_small_bare_number_is_a_date_not_a_price():
    """'track lagos to abuja 5' means the 5th - target_price must stay null."""
    intent = brain._local_parse("track lagos to abuja 5")
    assert intent.intent == "subscribe"
    assert intent.target_price is None
    assert intent.date is not None


def test_sentence_unsubscribe_phrasing():
    for msg in ("I don't want alerts anymore", "no more alerts please",
                "opt out", "cancel my alerts", "turn off alerts",
                "remove my alerts", "stop", "don't text me anymore"):
        intent = brain._local_parse(msg)
        assert intent.intent == "unsubscribe", msg


def test_sentence_book_pay_for_booking():
    """'I want to pay for my booking' is the buy action - book."""
    intent = brain._local_parse("I want to pay for my booking")
    assert intent.intent == "book"


def test_sentence_casual_chat_is_help():
    for msg in ("thanks", "ok", "good morning", "what can you do",
                "hello there", "i'll think about it"):
        intent = brain._local_parse(msg)
        assert intent.intent == "help", msg


def test_sentence_destination_only_with_price_word():
    """'cheapest flight to Abuja next tuesday' = fare, destination + date."""
    intent = brain._local_parse("cheapest flight to Abuja next tuesday")
    assert intent.intent == "fare"
    assert intent.destination_iata == "ABV"
    assert intent.date is not None


def test_prompt_defines_fare_and_decision_order():
    """The prompt must TEACH the model what 'fare' means and how to
    disambiguate - the old prompt never defined 'fare' at all."""
    assert '"fare" = asks the PRICE' in brain.SYSTEM_PROMPT
    assert "unsubscribe > status > subscribe >" in brain.SYSTEM_PROMPT
    assert "Examples - match the pattern" in brain.SYSTEM_PROMPT


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


# ---------------------------------------------------------------------------
# PASS 2 C - ranked fare list: AI narration + natural-language picking
# ---------------------------------------------------------------------------
def _pick_fares():
    return [
        {"airline": "Dana Air", "departs_at": "06:00",
         "flight_number": "9J 333", "price": 98000.0},
        {"airline": "Air Peace", "departs_at": "07:10",
         "flight_number": "P4 111", "price": 118500.0},
        {"airline": "Green Africa", "departs_at": "08:00",
         "flight_number": "9J 222", "price": 154000.0},
    ]


def test_local_resolve_pick_ordinal():
    assert brain._local_resolve_pick("the second one", _pick_fares()) == 2
    assert brain._local_resolve_pick("first", _pick_fares()) == 1
    assert brain._local_resolve_pick("the last one", _pick_fares()) == 3


def test_local_resolve_pick_airline_time_price():
    assert brain._local_resolve_pick("Air Peace please", _pick_fares()) == 2
    assert brain._local_resolve_pick("the 7am flight", _pick_fares()) == 2
    assert brain._local_resolve_pick("the cheapest", _pick_fares()) == 1
    assert brain._local_resolve_pick("the Dana one", _pick_fares()) == 1


def test_local_resolve_pick_ambiguous_or_none():
    # conflicting signals -> None; a question / small talk -> None
    assert brain._local_resolve_pick(
        "is air peace the cheapest?", _pick_fares()) is None
    assert brain._local_resolve_pick(
        "are they the same airline?", _pick_fares()) is None
    assert brain._local_resolve_pick("thanks", _pick_fares()) is None


def test_resolve_pick_without_key_respects_fallback_toggle(monkeypatch):
    """No key + second brain OFF -> graceful None (no hand-written guesser).
    Flipping the toggle back on restores the local resolver."""
    monkeypatch.setattr(brain, "GEMINI_API_KEY", None)
    monkeypatch.setattr(brain, "_ENABLE_LOCAL_PICK_FALLBACK", False)
    assert brain.resolve_pick("the second one", _pick_fares()) is None
    monkeypatch.setattr(brain, "_ENABLE_LOCAL_PICK_FALLBACK", True)
    assert brain.resolve_pick("the second one", _pick_fares()) == 2
    assert brain.resolve_pick("are they the same airline?",
                              _pick_fares()) is None


def test_resolve_pick_gemini_down_no_local_guess_when_disabled(monkeypatch):
    """Gemini unreachable + second brain OFF -> None, so the bot answers
    'which one?' gracefully instead of guessing with hand-written rules."""
    monkeypatch.setattr(brain, "_ENABLE_LOCAL_PICK_FALLBACK", False)
    assert brain.resolve_pick("the second one", _pick_fares(),
                              api_key="x", model="y",
                              http_client=_FailingClient()) is None


def test_resolve_pick_gemini_number():
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"candidates": [{"content": {"parts": [
                {"text": "2"}]}}]}

    class _FakeClient:
        def __init__(self, timeout=None):
            pass

        def post(self, url, json):
            return _Resp()

        def close(self):
            pass

    assert brain.resolve_pick("the early one", _pick_fares(),
                              api_key="x", model="y",
                              http_client=_FakeClient()) == 2


def test_resolve_pick_gemini_zero_is_not_a_pick():
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"candidates": [{"content": {"parts": [
                {"text": "0"}]}}]}

    class _FakeClient:
        def __init__(self, timeout=None):
            pass

        def post(self, url, json):
            return _Resp()

        def close(self):
            pass

    # "0" from Gemini = a question / not a pick -> None (no booking)
    assert brain.resolve_pick("is that the cheapest?", _pick_fares(),
                              api_key="x", model="y",
                              http_client=_FakeClient()) is None


def test_compose_ranked_reply_without_key_returns_greeted_template(monkeypatch):
    monkeypatch.setattr(brain, "GEMINI_API_KEY", None)
    out = brain.compose_ranked_reply(_pick_fares(), "Lagos", "Abuja",
                                     "2026-08-29")
    assert out.startswith("Beep! 🎫")
    assert "Here's what I found Lagos -> Abuja on 2026-08-29" in out
    assert "1. Dana Air, leaves 06:00 - ₦98,000" in out
    assert "Which one would you like? Reply 1, 2 or 3." in out


def test_compose_ranked_reply_failure_returns_greeted_template():
    out = brain.compose_ranked_reply(_pick_fares(), "Lagos", "Abuja",
                                     "2026-08-29", api_key="x", model="y",
                                     http_client=_FailingClient())
    assert out.startswith("Beep! 🎫")
    assert "1. Dana Air, leaves 06:00 - ₦98,000" in out


def test_compose_ranked_reply_narrates_on_success():
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"candidates": [{"content": {"parts": [
                {"text": "Hi Damilola! 😊 Good options for that day - the "
                         "Dana at 06:00 is the best value at ₦98,000. Reply "
                         "1, 2 or 3 to lock one."}]}}]}

    class _FakeClient:
        def __init__(self, timeout=None):
            pass

        def post(self, url, json):
            return _Resp()

        def close(self):
            pass

    out = brain.compose_ranked_reply(_pick_fares(), "Lagos", "Abuja",
                                     "2026-08-29", user_name="Damilola",
                                     api_key="x", model="y",
                                     http_client=_FakeClient())
    assert out.startswith("Hi Damilola! 😊")
    assert "₦98,000" in out


def test_compose_unclear_pick_reply_without_key_echoes_and_relists(monkeypatch):
    """No key: the 'which one?' echoes the user's own words + re-lists the
    real options. It apologizes and re-asks, it never guesses."""
    monkeypatch.setattr(brain, "GEMINI_API_KEY", None)
    out = brain.compose_unclear_pick_reply("the purple one", _pick_fares())
    assert out.startswith("Beep! 🎫")
    assert "didn't quite catch" in out
    assert '"the purple one"' in out          # echoes what the user said
    assert "1. Dana Air, leaves 06:00 - ₦98,000" in out
    assert "Which one would you like? Reply 1, 2 or 3." in out


def test_compose_unclear_pick_reply_failure_fallback():
    out = brain.compose_unclear_pick_reply("the purple one", _pick_fares(),
                                           api_key="x", model="y",
                                           http_client=_FailingClient())
    assert out.startswith("Beep! 🎫")
    assert "the purple one" in out
    assert "2. Air Peace, leaves 07:10 - ₦118,500" in out


def test_compose_unclear_pick_reply_tailors_on_success():
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"candidates": [{"content": {"parts": [
                {"text": "Hi Damilola! 😊 Sorry - I don't have a 'purple' "
                         "option today. Here's what's available:\n1. Dana "
                         "Air, leaves 06:00 - ₦98,000\n2. Air Peace, leaves "
                         "07:10 - ₦118,500\n3. Green Africa, leaves 08:00 - "
                         "₦154,000\n\nWhich one would you like?"}]}}]}

    class _FakeClient:
        def __init__(self, timeout=None):
            pass

        def post(self, url, json):
            return _Resp()

        def close(self):
            pass

    out = brain.compose_unclear_pick_reply("the purple one", _pick_fares(),
                                           user_name="Damilola", api_key="x",
                                           model="y",
                                           http_client=_FakeClient())
    assert out.startswith("Hi Damilola! 😊")
    assert "purple" in out          # tailored to what it observed
    assert "₦154,000" in out        # prices kept exact