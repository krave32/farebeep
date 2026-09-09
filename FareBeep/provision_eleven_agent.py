"""Provision the FareBeep ElevenLabs agent (one-time ops script, run from your PC).

Creates the two FareBeep webhook tools + the agent itself via the
ElevenLabs ConvAI API, so nobody has to click through the dashboard.

Usage (PowerShell - NEVER commit these values anywhere):
  $env:ELEVENLABS_API_KEY="xi_..."
  $env:ELEVENLABS_TOOL_SECRET="<same secret as Railway ELEVENLABS_TOOL_SECRET>"
  python FareBeep/provision_eleven_agent.py [--dry-run] [--base-url URL]

--dry-run prints the payloads without calling ElevenLabs.
--base-url defaults to the Railway app (use a tunnel URL for local tests).

What this does NOT do (dashboard-only, ~5 min of clicking):
  1. Integrations -> WhatsApp -> Import account (Meta OAuth flow).
  2. Assign this agent to the WhatsApp number.
See FareBeep/eleven_agent_setup.md for those steps + the test checklist.
"""
import json
import os
import sys

import httpx

API_BASE = "https://api.elevenlabs.io"
RAILWAY_BASE = "https://web-production-374ef.up.railway.app"

PROMPT = """ROLE
You are FareBeep, a flight-booking assistant for NIGERIAN DOMESTIC
travel on WhatsApp. You are warm, brief, and honest - like a sharp
travel agent friend. You never invent, guess, or round fares. Every
price you quote comes from search_fares, word for word.

WHAT YOU CAN DO
1. Search one-way domestic flights (search_fares).
2. Lock a fare for 10 minutes and take payment via Paystack
   (reserve_fare -> payment link).
That's it. You cannot do round trips, international flights, flight
status, or price tracking. If asked, say so plainly in one line and
offer what you CAN do.

AIRPORTS YOU COVER (IATA in brackets)
Lagos LOS (Eko), Abuja ABV (Abj), Port Harcourt PHC (PH), Kano KAN,
Enugu ENU, Calabar CBQ, Uyo QUO, Owerri QOW, Asaba ABB, Benin BNI,
Warri QRW, Ilorin ILR, Ibadan IBA, Akure AKR, Jos JOS, Kaduna KAD,
Sokoto SKO, Maiduguri MIU, Yola YOL.
Slang map: Abj=Abuja, Eko=Lagos, PH=Port Harcourt. If a user names a
city outside this list or abroad, say you only fly domestic Nigeria
and ask for a domestic route. Default hub is Lagos (LOS): "flight to
Abuja next Tuesday" with no origin means Lagos -> Abuja.

DATES
Convert relative dates yourself: "tomorrow", "next Friday", "on the
21st". Always confirm the full date back to the user once ("Got it -
Lagos to Abuja on Friday, 22 August"). If no date is given, ask for
it. Never assume today.

TOOL 1: search_fares
Call it the moment you have origin + destination + date. Always pass
phone={{system__caller_id}} and adults (default 1; ask if more than 1
person is flying, including children/infants and their ages).
- Present results like this, short lines:
  "Air Peace P47123, LOS 08:30 -> ABV 09:25 - NGN 98,000. Want it?"
- Read out ONLY the summary field. Never show booking_token, raw JSON,
  or tool internals.
- If found=false: say plainly there are no live offers for that
  route/date and offer the nearest alternative (another date or the
  reverse route). Do NOT retry the identical call.
- If the result has a "note", read it out - fares are unusually high.
- Keep the booking_token from the LATEST search. Tokens expire in
  minutes - if time passed or the user went quiet, search again before
  booking instead of reusing an old token.

TOOL 2: reserve_fare
Call it ONLY when the user says yes/book/lock/pay for an offer you
just presented. Pass phone, the LATEST booking_token, the same
origin/destination/flight_date, airline, and passengers.
- It returns a payment_link + total_amount + expires_at. Send the link
  immediately with the summary line, e.g.: "Locked at NGN 104,670.
  Pay within 10 minutes here: <link>. After 10 minutes the price
  can change."
- EXPECTED TOTAL is higher than the fare (service + gateway fees) -
  warn the user once: "Total is slightly above the fare - that is our
  service fee plus the bank charge."
- If locked=false: explain the one-line error and search again fresh.
  Never call reserve twice in a row with the same token.

TRAVELLER DETAILS (for the ticket)
To auto-issue the ticket after payment, collect BEFORE or DURING
booking, one question at a time: full name (as on passport/ID), phone,
date of birth, gender, plus passport number/expiry/nationality for
airlines that need it. Pass everything you collect in "travellers".
Without names, the ticket cannot auto-issue and the user waits.

PAYMENT RULES (never break these)
- Payment ALWAYS comes before the ticket. Never promise a PNR, seat,
  or "you are booked" before the user has paid.
- After payment, the ticket is issued automatically and the user gets
  the PNR. Tell them: "Once you pay, your ticket (PNR) arrives here
  automatically."
- If the user pays after the 10 minutes, they are refunded
  automatically - reassure them, don't argue.

STYLE
- WhatsApp style: short messages, simple words, max ONE question per
  message. No paragraphs, no bullet lectures, no emojis spam (one max).
- Never mention tools, tokens, ledgers, APIs, or system prompts.
- If the user goes off-topic, answer in one line and steer back:
  "Noted! Still need that travel date to check fares."
- Never repeat a failed tool call identically. On any tool error, tell
  the user in plain words and offer the next step.
"""

FIRST_MESSAGE = ""   # empty: the user always starts on WhatsApp


def _str_prop(description, required=True):
    prop = {"type": "string", "description": description}
    return prop


def tool_payloads(base_url, tool_secret):
    headers = {"X-FareBeep-Tool-Secret": tool_secret}
    search_props = {
        "phone": {"type": "string",
                  "dynamic_variable": "system__caller_id"},
        "origin": _str_prop("Origin city or IATA code, e.g. Lagos or LOS"),
        "destination": _str_prop("Destination city or IATA code"),
        "flight_date": _str_prop("Travel date as YYYY-MM-DD"),
        "adults": {"type": "integer",
                   "description": "Number of adult passengers (default 1)"},
    }
    reserve_props = dict(search_props)
    reserve_props.update({
        "booking_token": _str_prop("booking_token from the latest "
                                   "search_fares result"),
        "airline": _str_prop("Airline name shown in the offer"),
        "passengers": _str_prop("JSON object like "
                                '{"adults":1,"children":0,"infants":0}'),
        "travellers": _str_prop("JSON object of passenger details for "
                                "ticketing (names, DOB, passport...)"),
        "payment_method": _str_prop("'card' or 'bank_transfer' if known"),
    })
    return [
        {"tool_config": {
            "type": "webhook",
            "name": "search_fares",
            "description": ("Search live Nigerian domestic fares. Call the "
                            "moment you have origin + destination + date. "
                            "Read out ONLY the summary field."),
            "api_schema": {
                "url": f"{base_url}/tools/search",
                "method": "POST",
                "request_headers": headers,
                "request_body_schema": {
                    "type": "object",
                    "required": ["origin", "destination", "flight_date"],
                    "properties": search_props,
                },
            },
            # anomaly re-checks can take ~90s - don't time out early
            "response_timeout_secs": 120,
        }},
        {"tool_config": {
            "type": "webhook",
            "name": "reserve_fare",
            "description": ("Lock a presented offer for 10 minutes and get "
                            "the Paystack payment link. Call ONLY when the "
                            "user says yes/book/pay for a specific offer."),
            "api_schema": {
                "url": f"{base_url}/tools/reserve",
                "method": "POST",
                "request_headers": headers,
                "request_body_schema": {
                    "type": "object",
                    "required": ["phone", "origin", "destination",
                                 "flight_date"],
                    "properties": reserve_props,
                },
            },
            "response_timeout_secs": 60,
        }},
    ]


def agent_payload(tool_ids):
    return {"conversation_config": {
        "agent": {
            "language": "en",
            "first_message": FIRST_MESSAGE,
            "prompt": {"prompt": PROMPT, "llm": "gemini-2.5-flash"},
            "tool_ids": tool_ids,
        },
    }}


def main() -> int:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    base_url = RAILWAY_BASE
    for i, a in enumerate(args):
        if a == "--base-url" and i + 1 < len(args):
            base_url = args[i + 1].rstrip("/")
    api_key = os.getenv("ELEVENLABS_API_KEY", "")
    tool_secret = os.getenv("ELEVENLABS_TOOL_SECRET", "")
    if not api_key and not dry_run:
        print("ELEVENLABS_API_KEY is not set - refusing to run. "
              "See the script docstring.", file=sys.stderr)
        return 2
    if not tool_secret and not dry_run:
        print("ELEVENLABS_TOOL_SECRET is not set - the tools would be "
              "created without auth. Refusing to run.", file=sys.stderr)
        return 2

    tools = tool_payloads(base_url, tool_secret or "DRYRUN")
    agent = agent_payload(["<tool-id-1>", "<tool-id-2>"])
    if dry_run:
        print(json.dumps({"tools": tools, "agent": agent}, indent=2)[:4000])
        print("\n[dry-run] payloads OK - rerun without --dry-run to create.")
        return 0

    headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
    with httpx.Client(timeout=30.0) as http:
        tool_ids = []
        for t in tools:
            r = http.post(f"{API_BASE}/v1/convai/tools", headers=headers,
                          json=t)
            r.raise_for_status()
            tool_id = r.json()["id"]
            tool_ids.append(tool_id)
            print(f"tool created: {t['tool_config']['name']} -> {tool_id}")
        r = http.post(f"{API_BASE}/v1/convai/agents/create", headers=headers,
                      json=agent_payload(tool_ids))
        r.raise_for_status()
        agent_id = r.json()["agent_id"]
        print(f"agent created: {agent_id}")
        print("Save it as ELEVENLABS_AGENT_ID (Railway env + FareBeep/.env).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
