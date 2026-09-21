# FareBeep — Nigeria's flight fares, locked in chat

**FareBeep is the chat storefront for Nigerian domestic flights.** Send
*"Lagos to Abuja tomorrow"* to the bot and get a real fare, hold it for ten
minutes behind a Paystack link, and get beeped when the price drops — no app,
no forms, no silent price jumps.

The whole loop is real end-to-end: **a message in triggers a reply, a fare
drop triggers a push, a payment settles a ticket.**

## The three beeps

| Beep | What it does | Where it lives |
|---|---|---|
| **Fare beep** | Watches a route; pushes when the price drops >10% or hits your target | `alerts.py`, `worker.py` |
| **Status beep** | Watches a flight ahead of departure; pushes on gate/delay/board changes | `flight_status.py`, `worker.py` |
| **Booking** | 10-minute price lock → Paystack → PNR + branded ticket PDF | `payments.py`, `travels247.py`, `main.py` |

## Architecture

```
User chat (Telegram live · WhatsApp via Meta Cloud API)
        │  webhooks (or long-polling — no tunnel needed)
        ▼
FastAPI  FareBeep/main.py          ← one process, every endpoint
  │  conversation: Groq agent (tool loop) → guided fallback (brain.py)
  │  inventory:    Shared Ledger → 247travels live on miss (search.py)
  │  settlement:   Paystack HMAC-verified webhook (payments.py)
  │  flows:        encrypted /flow endpoint for "Set a beep" screens
  │                (whatsapp/flows.py + flow_screens.json)
  ▼
Supabase Postgres — the Shared Ledger (schema.sql / models.py)
        ▲
        │ loops (leader-elected by a Postgres advisory lock)
FareBeep/worker.py  ·  serve_all.py runs them in-process
  booking sweep · fare beeps · status beeps · ledger warmer · FX snapshots
```

**The Shared Ledger** is the product's core trick: every search result is
UPSERTed into `fare_ledger`, so the first user's search pays for the live
call and everyone else on that route for the next 8–15 minutes gets a
free <500 ms hit (`search.py` documents the exact contract).

**Price guardrail:** anomalous live fares (thin-route glitches measured in
the wild) are flagged, never quoted as real (`FARE_PRICE_GUARDRAIL_NGN`).

**Two brains, one honest fallback:** when `GROQ_API_KEY` is set, a Groq
tool-loop agent drives the conversation (`agent.py`) with per-phone memory
in `chat_state`. If Groq fails, the turn degrades to the deterministic
intent parser (`brain.py`) — the user never sees an error page.

## Repository layout

```
FareBeep/
  main.py            FastAPI app: webhooks (meta/twilio/telegram), /flow,
                     /tools/* (ElevenLabs agent hands), booking + tickets
                     pages, /webhook/paystack, /admin/ops + /admin cockpit,
                     /health, landing page
  agent.py           Groq + LangChain conversational agent (tool loop)
  brain.py           deterministic intent parser (fallback + date parsing)
  search.py          Shared Ledger search: ledger-first, SerpApi on miss
  travels247.py      247travels.com inventory client (async, JWT)
  providers.py       retry/parse/contract layer over external APIs
  alerts.py          fare-beep trigger rules (target price / >10% drop)
  flight_status.py   delay-feed client (DELAY_API_URL; Aviationstack fallback)
  payments.py        Paystack money math + webhook verification
  worker.py          background loops (bookings, fare beeps, status beeps)
  warmer.py          route warmer: keeps top-route ledger rows fresh
  poller.py          Telegram long-polling transport (tunnel-free dev)
  serve_all.py       web + worker + poller in ONE process (Railway mode)
  database.py        SQLAlchemy engine + idempotent additive migrations
  models.py / schema.sql   10 tables incl. fare_ledger, booking_sessions,
                     chat_state, delivery_receipts
  chatstate.py       per-phone conversation state (DB-backed)
  cards.py           interactive fare cards + button tap routing
  iata.py            city-name → IATA dictionary (the LLM never decides codes)
  dates.py           shared date parsing (Africa/Lagos)
  emailer.py         Resend delivery for voucher/boarding-pass PDFs
  config.py          every env var, defaulted and commented
  whatsapp/          flows.py (encrypted /flow endpoint), flow_screens.json,
                     sender.py, router.py, verify.py, templates
  web/               landing page (index.html/styles.css/scene.js),
                     admin cockpit (admin.html), logo + og.png social card
  tests/             38 files — see Tests below
meta_flow_setup.py   create/refresh the Set-a-Beep WhatsApp Flow (draft only)
set_flow_key.py      generate + upload the Flows RSA keypair
submit_templates.py  submit the two outbound templates to Meta
repoint_webhook.py   repoint the Meta webhook to a new deployment URL
simulate_*.py        local rehearsals: full chat journey / Paystack webhook
ops/                 read-only DB peeks (peek_beeps, probe_live_flow)
start-dev.bat        Windows dev launcher: server + cloudflared tunnel
```

## Running it

```bash
# 1. Environment
cp FareBeep/.env.example FareBeep/.env    # then fill in what you have

# 2. Dependencies (Python 3.11+)
python -m venv venv
venv/Scripts/pip install -r FareBeep/requirements.txt   # Windows
# pip install -r FareBeep/requirements.txt              # macOS/Linux

# 3. Database — Supabase Postgres
#    Run schema.sql once in the Supabase SQL Editor, set SUPABASE_DB_URL.
#    The app applies its own additive migrations on startup.

# 4. Run — pick ONE:
python -m uvicorn FareBeep.main:app --port 8000    # web only
python -m FareBeep.worker                          # + background loops
python -m FareBeep.serve_all                       # everything in one process
python -m FareBeep.poller                          # Telegram without a tunnel
```

Windows shortcut: `start-dev.bat` opens the server plus a cloudflared
tunnel for the Meta webhook.

**Transport switching** — `MESSAGING_PROVIDER` picks the channel:
`telegram` (fastest test path: Bot API, no approval), `meta` (WhatsApp
Cloud API — production), `twilio` (legacy). Pushes without credentials go
to the log instead — nothing else changes.

**Key env vars** (all in `.env.example` with comments): `SUPABASE_DB_URL`,
`MESSAGING_PROVIDER`, `TELEGRAM_BOT_TOKEN`, `META_*`, `GROQ_API_KEY`,
`SERPAPI_API_KEY`, `TRAVELS247_*`, `PAYSTACK_*`, `ADMIN_TOKEN`
(unlocks `/admin` + `/admin/ops`; unset = those surfaces 404, closed by
design), `ADMIN_ALERT_PHONE` (the support-relay console), `BEEP_FLOW_MODE`
(`draft` while building the flow, `published` at go-live).

## The booking settlement contract

`final_price = (net_fare + ARHA_MARKUP_NGN + PAYSTACK_FLAT_FEE_NAIRA)
/ (1 − PROCESSING_FEE_RATE)` — the customer funds the gateway fee so the
utility nets the full markup on every ticket. Payments arriving after the
10-minute lock are rejected and refund-flagged; the airline API is never
called on an expired session.

## Ops

- `GET /health` — liveness
- `GET /admin` — the ops cockpit (browser UI): health strip, open support
  threads with transcripts + one-tap relay commands, dead-letter replay.
  Shell is served only when `ADMIN_TOKEN` is set; data always demands the
  `X-Admin-Token` header — the token never rides a URL.
- `GET /admin/ops` — the same snapshot as JSON, for scripts.
- Support relay: users who say `SUPPORT` (or hit money trouble) get a
  human thread; the admin replies from their own chat with
  `R <phone> <text>`, lists with `/open`, closes with `/done <phone>`.

## Tests

```bash
venv/Scripts/python.exe -m pytest FareBeep/tests -q    # Windows
python -m pytest FareBeep/tests -q                     # macOS/Linux
```

**496 passing** across 38 files — the conversation pipeline (concierge,
pick gates, rate limiting), the Shared Ledger (ledger-first search, FX
floor, price guardrail), the flow endpoint (validator rules, encryption,
idempotent subscribe), the settlement engine (HMAC, expiry, refund
flagging), ticket/boarding-pass PDFs, delivery retries + dead letters,
crash recovery, the support relay, the ops cockpit, and a full
**E2E beep-pipeline smoke test** (flow → subscription → alert against
mocked Meta APIs).

## What's REAL vs mocked

| Component | Status |
|---|---|
| Telegram transport (webhook + long-polling) | **REAL** — live |
| WhatsApp Cloud API (Meta) inbound/outbound | **REAL** — code complete, awaiting Meta business approval |
| Shared Ledger search → 247travels live fares | **REAL** |
| Paystack settlement + webhook verification | **REAL** |
| Groq conversational agent + guided fallback | **REAL** |
| WhatsApp Flow ("Set a beep", encrypted endpoint) | **REAL** — draft mode until Meta publishes the flow |
| Ticket + boarding-pass PDFs (reportlab) + Resend email | **REAL** |
| Fare data in dev without `TRAVELS247_*` | mocked by the ledger/tests — never shipped to users |

## Go-live checklist (WhatsApp)

1. Meta business verification → WABA production approval
2. `python meta_flow_setup.py` (create flow) → publish it in Business Manager
3. `python set_flow_key.py` (flows encryption keypair)
4. `python repoint_webhook.py <url>` → set `BEEP_FLOW_MODE=published`
5. `python submit_templates.py` → approve the two utility templates

## Privacy

Chat data (phone, routes, bookings) lives in your Supabase; `STOP` (or
`delete my data`) wipes the user's rows. No `ADMIN_TOKEN` in any committed
file; secrets live only in `.env` (git-ignored).
