# FareBeep x ElevenLabs agent setup

How the pieces fit: the ElevenLabs agent IS the conversation (it talks to
the user on WhatsApp natively). FareBeep's FastAPI app is its hands - the
agent calls `/tools/search` to find fares and `/tools/reserve` to lock one
and get a Paystack link. Ticketing happens automatically after payment.

## 1. Connect WhatsApp (dashboard)

ElevenLabs dashboard -> Integrations -> WhatsApp -> Import account.
Complete the Meta authorization flow for your WhatsApp Business number,
then assign your FareBeep agent to the number. Inbound user messages now
reach the agent; the agent's replies go back through WhatsApp directly.
(FareBeep's own `/webhook/meta` stays as the fallback/debug channel.)

## 2. Register the two webhook tools

EASIEST: run `python FareBeep/provision_eleven_agent.py` (with
`ELEVENLABS_API_KEY` + `ELEVENLABS_TOOL_SECRET` in your environment) -
it creates both tools and the agent via the ElevenLabs API. Manual
dashboard alternative below.

Base URL (production): `https://web-production-374ef.up.railway.app`
(Local test: your tunnel URL, e.g. `https://<name>.trycloudflare.com`.)

For EACH tool, add a custom auth header (dashboard: tool -> Authentication
-> Custom headers):

```
X-FareBeep-Tool-Secret: <ELEVENLABS_TOOL_SECRET from FareBeep/.env>
```

### Tool 1: search_fares

- Method/URL: `POST {BASE}/tools/search`
- When: user asks for fares (route and/or date mentioned).
- Parameters (JSON):
  - `phone` (string, required) - the caller's WhatsApp number. Use the
    `{{system__caller_id}}` dynamic variable.
  - `origin` (string, required) - city or IATA ("Lagos", "LOS", "Abj", "Eko").
  - `destination` (string, required) - city or IATA.
  - `flight_date` (string, required) - YYYY-MM-DD.
  - `adults` (integer, optional, default 1).
- Returns: `summary` (read it out), `price_ngn`, `booking_token`,
  `airline`, `flight_no`, plus `note` (read it out when present - it
  means prices are unusually high).

### Tool 2: reserve_fare

- Method/URL: `POST {BASE}/tools/reserve`
- When: user says book/lock/pay for a specific offer the agent presented.
- Parameters (JSON):
  - `phone` (string, required) - `{{system__caller_id}}`.
  - `booking_token` (string, required) - from the search result. If the
    search happened long ago, search again first (tokens expire).
  - `origin`, `destination`, `flight_date` (strings, required) - same
    route the token was issued for.
  - `airline` (string, optional) - shown in the lock message.
  - `passengers` (object, optional) - `{adults, children, infants}`.
  - `travellers` (object, optional but recommended) - full passenger
    details for ticketing (title, first_name, last_name, dob, gender,
    phone, passport_number, passport_expiry, nationality...). Without
    these the ticket cannot auto-issue after payment.
- Returns: `payment_link`, `total_amount`, `expires_at`, `summary`.
  Read the summary out and send the payment link.

## 3. System prompt (paste into the agent)

```
ROLE
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
Today is {{system__date}}. Convert relative dates yourself: "tomorrow",
"next Friday", "on the 21st". Always confirm the full date back to the
user once ("Got it - Lagos to Abuja on Friday, 22 August"). If no date
is given, ask for it. Never assume today.

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
```

## 4. Test checklist

1. Message the WhatsApp number: "Lagos to Abuja tomorrow" -> agent
   calls search_fares -> quotes a real fare with airline + price.
2. "Book it" -> agent calls reserve_fare -> you get a Paystack link.
3. Pay (test mode) -> "BOOKING CONFIRMED" message with a PNR arrives.
4. Wrong tool secret -> `/tools/*` returns 403 (check app logs).
5. Secret rotation: change `ELEVENLABS_TOOL_SECRET` in Railway env AND in
   both dashboard tools at the same time.
