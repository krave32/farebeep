"""Tests for the WhatsApp integration."""
import json, pytest
from FareBeep.whatsapp.router import classify_message, detect_command, MessageType
from FareBeep.whatsapp.flows import _validate_beep

def test_classify_text():
    entry = {"changes": [{"value": {"messages": [{"id": "w1", "from": "234801", "timestamp": "1", "type": "text", "text": {"body": "Lagos to Abuja"}}]}}]}
    msgs = classify_message(entry)
    assert len(msgs) == 1 and msgs[0].message_type == MessageType.TEXT and msgs[0].text == "Lagos to Abuja"

def test_classify_button():
    entry = {"changes": [{"value": {"messages": [{"id": "w2", "from": "234801", "timestamp": "1", "type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": "book_x", "title": "Book"}}}]}}]}
    msgs = classify_message(entry)
    assert msgs[0].message_type == MessageType.BUTTON_REPLY and msgs[0].button_id == "book_x"

def test_classify_flow():
    entry = {"changes": [{"value": {"messages": [{"id": "w3", "from": "234801", "timestamp": "1", "type": "interactive", "interactive": {"type": "nfm_reply", "nfm_reply": {"flow_token": "tk", "response_json": '{"origin":"LOS"}'}}}]}}]}
    msgs = classify_message(entry)
    assert msgs[0].message_type == MessageType.FLOW_RESPONSE and msgs[0].flow_data == {"origin": "LOS"}

def test_status_filtered():
    entry = {"changes": [{"value": {"statuses": [{"id": "w1", "status": "delivered"}]}}]}
    assert classify_message(entry) == []

def test_commands():
    assert detect_command("menu") == "menu"
    assert detect_command("Hi") == "menu"
    assert detect_command("STOP") == "stop"
    assert detect_command("Lagos to Abuja") is None

def test_validate_beep_valid():
    assert _validate_beep({"origin": "LOS", "destination": "ABV", "departure_date": "2026-09-18", "passengers": 1}) is None

def test_validate_beep_same_city():
    assert "differ" in _validate_beep({"origin": "LOS", "destination": "LOS", "departure_date": "2026-09-18"}).lower()

def test_validate_beep_past():
    assert "past" in _validate_beep({"origin": "LOS", "destination": "ABV", "departure_date": "2020-01-01"}).lower()

def test_validate_beep_missing():
    assert _validate_beep({"destination": "ABV"}) is not None
