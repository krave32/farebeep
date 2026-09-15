"""Submit the two outbound message templates to Meta for approval.
Both UTILITY. Body parameter order must match templates.py exactly.

Usage: python submit_templates.py
"""
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from meta_flow_setup import load_env  # noqa: E402

WABA = "1162930926914560"
env = load_env()
ver = env["META_API_VERSION"]
TOK = env["META_ACCESS_TOKEN"]

TEMPLATES = [
    {
        "name": "farebeep_price_drop",
        "category": "UTILITY",
        "language": "en",
        "components": [
            {
                "type": "BODY",
                "text": ("Price beep: your watched fare from {{1}} has "
                         "dropped. Departure: {{2}}. Airline: {{3}}. "
                         "Was {{4}}, now {{5}} - you save {{6}}. "
                         "Fare last checked {{7}}. Fares change fast, so "
                         "open the bot chat to lock this price."),
                "example": {
                    "body_text": [[
                        "LOS → ABV", "2026-09-22", "Air Peace",
                        "₦145,000", "₦98,000", "₦47,000 (32%)",
                        "15 Sep, 16:00",
                    ]]
                },
            },
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "QUICK_REPLY", "text": "Track price"},
                    {"type": "QUICK_REPLY", "text": "Dismiss"},
                ],
            },
        ],
    },
    {
        "name": "farebeep_flight_status",
        "category": "UTILITY",
        "language": "en",
        "components": [
            {
                "type": "BODY",
                "text": ("Flight update for {{1}}: {{2}}. Check in with "
                         "the airline using your booking reference if this "
                         "affects your travel plans."),
                "example": {"body_text": [["P4 1234",
                                           "Departure delayed by 35 minutes."]]},
            },
        ],
    },
]

for tpl in TEMPLATES:
    body = json.dumps(tpl).encode()
    req = urllib.request.Request(
        f"https://graph.facebook.com/{ver}/{WABA}/message_templates",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {TOK}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"{tpl['name']}: OK ->", json.loads(r.read()))
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        if "name_already_exists" in err:
            print(f"{tpl['name']}: already exists, skipping")
        else:
            print(f"{tpl['name']}: FAILED HTTP {e.code} ->", err)
