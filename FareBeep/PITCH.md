# FareBeep — Pitch Document

**Prepared for:** Caleb Egwuenu, CEO — Tiqwa
**Prepared by:** Damilola Akinluwo — FareBeep
**Date:** August 2026
**Contact:** [damilola.akinluwo@yourmail.com] | [+234 8XX XXX XXXX]

---

## 1. One-liner

**FareBeep is the WhatsApp storefront for Nigerian domestic flights** — a conversational bot where a passenger sends *"Lagos to Abuja tomorrow"* and gets a real fare, a Paystack payment link, and proactive flight-status alerts, all in the same chat thread.

---

## 2. The problem

- **Booking Nigerian domestic flights is a WhatsApp conversation already** — it just happens manually, through a human travel agent. Every agent in Nigeria runs their business on WhatsApp DMs: price checks, payment links, follow-ups. That conversation is slow, error-prone, and scales with headcount.
- **Fares are chaotic.** Air Peace, Arik, Ibom, United Nigeria, Max Air and others price differently per day; passengers get burned by yesterday's price or surprise delays.
- **Flight status is a black box.** No scheduled airline text tells you a delay is coming. Passengers find out at the airport, or not at all.
- **OTAs and travel-tech platforms reach agencies, not passengers.** The industry's consumer distribution layer is missing — and it lives inside WhatsApp.

## 3. The solution

FareBeep automates the entire agent conversation in one WhatsApp thread:

1. **Search** — user sends a natural-language message; Gemini extracts route + date (e.g. *"Lagos to Abuja tomorrow"*, *"book PHC to Enugu Saturday"*).
2. **Fare** — real prices from Google Flights (via SerpApi), converted to NGN with an operator-set FX rate, cached in a local ledger to cut cost and latency.
3. **Book** — one reply to "BOOK" creates a 10-minute booking session and a Paystack payment link (airline price + NGN 3,000 markup + 1.5% processing fee, grossed up so the utility nets the full markup).
4. **Ticket** — Paystack webhook (HMAC-SHA512 verified) settles the session; payments arriving after the 10-minute expiry are rejected and refund-flagged — the airline API is never called on an expired session.
5. **Status Beep** — 3 hours before departure, FareBeep starts watching the flight (Aviationstack). If it turns *delayed, cancelled, diverted, or landed*, the passenger gets a proactive WhatsApp template message.

Every webhook is HMAC-verified; every message is conversationally parsed; every failure is logged and never crashes the flow.

## 4. Why WhatsApp, why now

- **Nigeria lives in WhatsApp.** WhatsApp is the de facto commerce and customer-service channel for tens of millions of Nigerians — it is where flight shopping and booking intent already happens organically.
- **Zero learning curve.** No app install, no account creation. The phone number *is* the account.
- **Meta's WhatsApp Business Cloud API** gives free test-number messaging and template messages for proactive alerts — the same infrastructure global brands build on.
- **The competition skipped this layer.** Nigerian travel startups and OTAs built web apps and demand search-engine traffic; none own the conversational, WhatsApp-native booking moment.

## 5. Product tour (live demo script)

| Message from user | What FareBeep does |
|---|---|
| `Lagos to Abuja tomorrow` | Returns cheapest fare + airline + verification link, with total incl. fees |
| `BOOK Lagos to Abuja` | Creates 10-min booking session + Paystack payment link in-chat |
| *(pays on Paystack)* | Webhook verifies → session marked paid → ticket provisioning hook |
| `Track P47123` | Registers a status watch; 3h before departure it starts polling |
| *(flight delayed)* | Proactive WhatsApp template: "P47123 now DELAYED" |
| `HI` / `HELP` | Onboarding intro + command list; first-contact welcome |

Pricing transparency is built in: the reply itemizes airline price, service fee, and total — no hidden charges, unlike the manual-agent status quo.

## 6. Business model

- **Markup per ticket:** NGN 3,000 flat + 1.5% processing fee (user-funded via gross-up, so margin is never eaten by payment fees).
- **Relevant volume:** Nigeria's busiest domestic route (Lagos–Abuja) alone moves hundreds of thousands of passengers a year. Even a small share of that is a healthy revenue line per booking.
- **Future revenue:** fare-alert subscriptions, corporate status-watch contracts, affiliate fare placements.

## 7. Why Tiqwa (the ask)

Tiqwa sells travel inventory and infrastructure: the One API Call (Amadeus Enterprise + consolidator + SOTO + African local airlines), Anchor OTA platform, Travel Wahoo corporate tool. **What Tiqwa doesn't have is a direct-to-passenger conversational channel.** FareBeep is that channel — and it fits Tiqwa's stack in three concrete ways:

1. **Distribution partnership (recommended start):** FareBeep becomes a live consumer storefront riding on Tiqwa's booking API. Tiqwa's inventory (including SOTO and local-carrier tickets its consolidator already covers) reaches passengers directly; FareBeep brings the engagement layer. Revenue share per booking.
2. **White-label for Tiqwa clients:** Tiqwa's agencies, tour operators, and fintech customers get a WhatsApp booking bot out of the box — Tiqwa resells the conversational engine under its own brand, on top of its own inventory.
3. **Corporate plug-in for Travel Wahoo:** fare-drop alerts and proactive status pushes for corporate travelers, delivered into WhatsApp.

This is a product-fit conversation: **your inventory, our channel, shared revenue.**

## 8. Current status (honest)

- **Built and tested:** full transactional loop (search → book → pay → ticket → status watch) implemented; 31 tests passing on the FareBeep suite; HMAC-verified Meta and Paystack webhooks; NDPA-compliant privacy model (phone number only, one-word opt-out deletes data).
- **Live now:** fare search (Google Flights), pricing engine, booking sessions, Paystack test-mode links, Aviationstack status polling — all operational.
- **Pending:** Meta WhatsApp Business production approval and live webhook wiring (test-number demo is ready to show); Paystack live keys; ticketing API hookup (Tiqwa's booking API is the natural candidate).

## 9. Proposed next step

A **4-week pilot**: FareBeep goes live on Tiqwa's API for the Lagos–Abuja corridor, with 20–50 test passengers booking real fares through WhatsApp. Success metric: bookings completed through the chat plus passenger status-alert satisfaction. If the pilot converts, we formalize a revenue-share agreement and expand routes.

## 10. The team

Damilola Akinluwo — solo builder who shipped the FareBeep system end to end: conversational NLP, webhook security, the transactional booking loop, and the status-watch engine, backed by a 250+ test codebase.

---

*FareBeep — flight pricing, booking, and status, in the conversation you already have open.*
