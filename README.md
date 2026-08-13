# Araha — Nigeria's Flight Fare Tracker + Crowdsourced Boarding Status

WhatsApp-first fare alerts and passenger-reported flight status for **Nigerian domestic routes**.
The full loop is real end-to-end: **a message in triggers a reply, a status escalation
triggers a push, a fare drop triggers a push.**

## Scope

Araha tracks **Nigeria-domestic flights only** — all routes are between Nigerian
airports (LOS, ABV, PHC, ENU, BNI, KAN, CBQ, ILR, QOW, AKR). All fares are in NGN.

**Why not West Africa-wide?** Cross-border routes (Accra, Dakar, Abidjan, Freetown)
were removed to focus on the market where Araha has the strongest carrier coverage
and the highest demand. Cross-border routes also required multi-currency handling
(GHS, XOF, SLL) that added complexity without proportional value.

**Why is Dana Air removed?** Dana Air (IATA: 9J) was suspended by the Nigerian NCAA
in April 2024 following a runway incident and has ceased operations. Its route
obligations were transferred to NG Eagle, a newer carrier now tracked instead.
Dana Air is kept in `WEST_AFRICAN_AIRLINES` as a historical reference only.

**Why is Amadeus secondary?** Amadeus GDS does not cover any Nigerian domestic carrier
directly (Air Peace, Arik Air, Ibom Air, etc. are not on GDS). Google Flights is the
meaningful real-data path for this scope. Amadeus code is retained for potential
future international route expansion.

## Architecture

```
docker-compose / Railway
├── db       Postgres 15 (fares, subscriptions, reports, scores, alert history)
├── app      FastAPI: /webhook/whatsapp (Twilio inbound), /admin, /health
└── worker   APScheduler: FX refresh + fare ingestion + price-drop pushes
```

Fare ingestion and boarding status stay **decoupled**: different services
(`fare_service` vs `status_service`), different trust models, no shared logic.

## Running it

```bash
# Docker
docker-compose up --build

# Local development
python -m uvicorn app.main:app --reload
python -m app.workers.fare_worker

# Production (Railway via start.sh)
sh start.sh  # runs both web + worker in one process
```

Copy `.env.example` to `.env` and fill in real values to go live. Without Twilio
credentials, pushes go to the container log in CONSOLE mode instead of WhatsApp —
nothing else changes.

## Bot commands

| Message | Effect |
|---|---|
| `SUBSCRIBE LOS ABV` | Fare-drop alerts, rolling 30-day window |
| `SUBSCRIBE LOS ABV 80000` | Same, with target price |
| `SUBSCRIBE LOS ABV 2026-08-15` | Fare-drop alerts for that specific date only |
| `SUBSCRIBE LOS ABV 2026-08-15 80000` | Specific date + target price |
| `FARE LOS ABV` | Cheapest fare in next 30 days |
| `FARE LOS ABV 2026-08-15` | Cheapest fare for that specific date |
| `TRACK P47123 2026-07-20` | Live boarding/gate/delay pushes for that flight |
| `boarding now gate 12` (while tracking) | Files a status report |
| `HELP` | Command list |
| `UNSUBSCRIBE` | Remove all alerts and delete your data |

### Date-aware fares

- **Rolling window** (default): searches fares across the next 30 days. The worker
  samples dates every `FARE_WINDOW_SAMPLE_DAYS` (default 5) within the window to
  avoid multiplying API calls.
- **Specific date**: pass a YYYY-MM-DD date to search only that date.
- `get_cheapest_fare()` is always scoped — it never compares a cheap Tuesday fare
  against an expensive Christmas fare on the same route.

## Fare data sources

Toggle with the `FARE_SOURCE` env var — `mock` (default), `google`, `amadeus`, or
`hybrid`. No redeploy needed.

| Source | What it provides | API key needed? | Nigeria-domestic relevance |
|---|---|---|---|
| `MockFareIngestor` | Deterministic test data, realistic price jitter | No | Dev/test only |
| `GoogleFlightsIngestor` | **Google Flights fares — covers Air Peace, Arik, Ibom, etc.** | No | **PRIMARY** |
| `AmadeusFareIngestor` | GDS fares (international carriers) | Yes (free tier) | Secondary (no NG domestic coverage) |
| `HybridIngestor` | Google Flights + Amadeus combined | Amadeus key only | Secondary |

### Tracked routes (Nigeria domestic only)

| Route | Description | Expected carriers |
|---|---|---|
| LOS-ABV / ABV-LOS | Lagos - Abuja | Air Peace, Arik, Ibom, United Nigeria |
| LOS-PHC / PHC-LOS | Lagos - Port Harcourt | Air Peace, Arik |
| LOS-ENU / ENU-LOS | Lagos - Enugu | Air Peace, Enugu Air, Ibom |
| LOS-BNI / BNI-LOS | Lagos - Benin City | Air Peace, Arik |
| LOS-KAN / KAN-LOS | Lagos - Kano | Air Peace, Max Air |
| LOS-CBQ / CBQ-LOS | Lagos - Calabar | Ibom Air, Air Peace |
| LOS-ILR / ILR-LOS | Lagos - Ilorin | Air Peace, Overland |
| LOS-QOW / QOW-LOS | Lagos - Owerri | Air Peace, United Nigeria |
| ABV-PHC / PHC-ABV | Abuja - Port Harcourt | Air Peace, Arik |
| ABV-ENU / ENU-ABV | Abuja - Enugu | Air Peace, Enugu Air |
| ABV-BNI / BNI-ABV | Abuja - Benin City | Air Peace |
| ABV-KAN / KAN-ABV | Abuja - Kano | Air Peace, Max Air |
| ABV-CBQ / CBQ-ABV | Abuja - Calabar | Ibom Air |
| PHC-ENU / ENU-PHC | Port Harcourt - Enugu | Air Peace |

### Nigerian domestic carriers

| Airline | IATA | Google Flights? | Amadeus GDS? |
|---|---|---|---|
| Air Peace | P4 | **Yes** | No |
| Arik Air | W3 | **Yes** | No |
| Ibom Air | QI | Likely | No |
| United Nigeria Airlines | UN | Possible | No |
| Green Africa Airways | NK | Possible | No |
| ValueJet | VK | Possible | No |
| Overland Airways | OF | Limited | No |
| NG Eagle | NE | Possible (new carrier) | No |
| Max Air | MX | Primarily Kano hub | No |
| Umza Air | UM | Limited | No |
| Enugu Air | Q9 | Limited | No |
| ~~Dana Air~~ | ~~9J~~ | ~~Defunct (NCAA suspended April 2024)~~ | No |

### Google Flights safety filter

The `fli` library's internal `Airline` enum has **incorrect string values** for
some carriers (e.g. `Airline.P4.value` returns `'Aerolineas Sosa'` — a Honduran
airline — instead of `'Air Peace'`). The ingestor was fixed to use
`flight.primary_airline.name` (correct IATA code) and
`flight.primary_airline_name` (correct human name) instead of `leg.airline.value`.

As a permanent safety net, `GoogleFlightsIngestor` maintains a
`NIGERIAN_DOMESTIC_AIRLINES` allow-list. Any result attributed to an airline
NOT in that set for a Nigeria-domestic route is **dropped and logged as
suspicious** rather than returned as a real fare. A dropped result is fine;
a wrong result shown to a user as real is not.

### Tuning knobs

| Env var | Default | Description |
|---|---|---|
| `FARE_POLL_MINUTES` | 5 | Worker poll interval |
| `FARE_WINDOW_DAYS` | 30 | Rolling window length |
| `FARE_WINDOW_SAMPLE_DAYS` | 5 | Sample every N days within window |
| `FARE_SOURCE` | mock | Active ingestor: mock/google/amadeus/hybrid |
| `GOOGLE_FLIGHTS_CURRENCY` | NGN | Currency for Google Flights queries |
| `ADMIN_USER` | *(unset)* | HTTP Basic Auth username for /admin |
| `ADMIN_PASSWORD` | *(unset)* | HTTP Basic Auth password for /admin |

## Admin authentication

The `/admin` dashboard is protected with HTTP Basic Auth. Set both `ADMIN_USER`
and `ADMIN_PASSWORD` environment variables before deployment:

```bash
# Local (.env file)
ADMIN_USER=admin
ADMIN_PASSWORD=your-strong-password

# Railway
railway variables set ADMIN_USER=admin ADMIN_PASSWORD=your-strong-password
```

If either variable is unset, the admin panel runs **unprotected** with a clear
startup warning in the logs. Never deploy to production without these set.

## Privacy & data protection (NDPA)

Araha complies with the Nigeria Data Protection Act (NDPA) 2023.
See [PRIVACY.md](PRIVACY.md) for the full privacy policy.

**Summary:**
- **Collected:** phone number, subscribed routes, reported flight statuses
- **Not collected:** name, email, payment info, location
- **Removal:** Send `UNSUBSCRIBE` (or `CANCEL`, `QUIT`, `REMOVE`) to remove
  all alerts and have your personal data deleted/anonymized. The literal
  word `STOP` is intercepted by Twilio at the platform level — our
  `/webhook/optout` endpoint receives a callback to handle data deletion.
- Historical records are anonymized (phone number replaced with `[deleted]`)
  rather than kept tied to your identity.
- The onboarding message includes a data-use notice per NDPA requirements.

### Twilio opt-out webhook setup

Twilio intercepts `STOP`, `START`, `HELP`, and `UNSUBSCRIBE` at the platform
level. To ensure our database cleanup runs when a user opts out:

1. In Twilio Console → Messaging → Settings → WhatsApp Sandbox Settings
2. Set **Opt-out management** URL to:
   `https://endearing-celebration-production-f9f3.up.railway.app/webhook/optout`
3. Set **Opt-in management** URL to:
   `https://endearing-celebration-production-f9f3.up.railway.app/webhook/optin`

This guarantees NDPA-compliant data deletion even though `STOP` never
reaches our `/webhook/whatsapp` endpoint.

## Notification emoji scheme (centralized in notify_templates.py)

| Message type | Emoji |
|---|---|
| Subscribe / Track confirmation | ✅ |
| Fare found (FARE query) | 💰 |
| No fare/route data yet | 🔎 |
| Status report logged (pending) | 📝 |
| Rate-limited ("too fast") | ⏳ |
| Unparsed/unclear report | ❓ |
| Fare-drop alert (push) | 📉 |
| Boarding confirmed (push) | 🛫 |
| Gate change confirmed (push) | 🚪 |
| Delay confirmed (push) | ⏰ |
| Not-boarding confirmed (push) | 🕓 |
| Other/generic status (push) | 🔔 |
| HELP text | *(none)* |

## Boarding-status trust model (`app/services/status_service.py`)

- 1 report → **pending** (never pushed)
- 2+ **distinct** reporters, same state, within 30 min → **confirmed** → pushed
  once (deduped via `push_log`) to everyone subscribed to that flight
- Conflicting states → **disputed**, surfaced in admin, never silently overwritten
- Exception: a bucket with ≥2 reporters AND ≥2× the rival bucket wins
  (majority-wins); minority reports are marked disputed

### Anti-abuse
- Rate limit: 1 report / reporter / flight / 5 min
- Reporter scoring: contradiction rate > 50% over ≥3 reports → **flagged**
- Reward hook: `reporter_scores.credits` + `trust_level` columns for future use

## Tests

```bash
cd araha && python -m pytest tests -v
```

**226 passing** — covers: fare ingestion (mock + Amadeus + Google Flights safety
filter), FX conversion, price-alert triggering (date-aware), status parsing,
confirmation/dispute/majority-wins, emoji templates, FARE_SOURCE toggle, Dana Air
removal, rolling-window sampling, specific-date subscriptions, date-scoped queries,
command parsing with dates, implausible-airline filtering, conversation state,
admin authentication, unsubscribe/NDPA data anonymization, onboarding flow,
cross-date alert deduplication, and Twilio platform-level STOP interception.

## What's REAL vs MOCKED

| Component | Status |
|---|---|
| WhatsApp inbound webhook (Twilio → TwiML reply) | **REAL** |
| WhatsApp outbound pushes (Twilio SDK) | **REAL**; console fallback |
| Price-drop alert worker (APScheduler, date-aware) | **REAL** |
| Boarding-status confirm → push loop | **REAL** |
| FX rates (open.er-api.com, keyless, NGN only) | **REAL** with cached fallback |
| Admin view (`/admin`) | **REAL** (HTTP Basic Auth protected) |
| Fare data (mock) | **REAL** code, fake prices — default for dev |
| Fare data (Google Flights) | **REAL** via fli library — **primary for production** |
| Fare data (Amadeus) | **REAL** code, secondary — no NG domestic carrier coverage |

## STILL not done

1. **Production WhatsApp Business API approval** — sandbox only.
2. **Google Flights rate-limit hardening** — at very high poll frequencies.
3. **fli legal review** — reverse-engineered API; review Google ToS before
   commercial deployment at scale.
