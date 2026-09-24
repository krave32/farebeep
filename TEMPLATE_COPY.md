# WhatsApp Template Copy Pack

Paste-ready definitions for the two templates the code sends. Meta rejects a
send if the name, variable count, variable order, or button count don't match
the approved template — so submit these **exactly as written**.

Where each is sent from:

| Template | Sender | Trigger |
|---|---|---|
| `farebeep_flight_status` | `status.py::_on_status_change` | tracked flight becomes DELAYED / CANCELLED / DIVERTED / LANDED |
| `farebeep_price_drop` | `alerts.py::_deliver_beep` | a Beep's route price drops below target/baseline |

If a price-drop template send fails (e.g. not yet approved), `alerts.py`
falls back to plain text inside an open 24h window — the Beep still goes
out, just not proactively. Status pushes have **no fallback**: they only
fire outside the window, so `farebeep_flight_status` must exist before any
tracked flight can alert.

Both sends use `language: en_US` — create each template in
**English (US)** or the send will fail the locale match.

---

## Template 1 — `farebeep_flight_status`

- **Category:** UTILITY (transactional update on a service the user opted into)
- **Variables:** 2 body params, positional, both text
- **Buttons:** **NONE** — `status.py` sends no button payloads; a template
  with buttons would make every send error out

**Body (paste into Meta's template builder):**

```
✈️ Flight {{1}} update: {{2}}.

Reply TRACK {{1}} for live details, or message me here any time.
```

| Placeholder | Example value sent |
|---|---|
| `{{1}}` | `P47123` (flight IATA) |
| `{{2}}` | `DELAYED` (uppercase status) |

---

## Template 2 — `farebeep_price_drop`

- **Category:** MARKETING (promotional pricing content) — UTILITY is
  routinely rejected for price-drop copy
- **Variables:** 6 body params, positional, in exactly this order
- **Buttons:** exactly 2 quick-reply buttons (the code sends a payload for
  each, index 0 and 1)

**Body (paste into Meta's template builder):**

```
Hi {{1}}! 📉 Fare Beep: {{2}} → {{3}} on {{4}} just dropped to NGN {{5}} — your target was NGN {{6}}.

Tap below and I'll re-check it live before you pay.
```

| Placeholder | Example value sent |
|---|---|
| `{{1}}` | `Ngozi` (user name, or `there`) |
| `{{2}}` | `Lagos` (origin **city**, not code) |
| `{{3}}` | `Abuja` (destination city) |
| `{{4}}` | `2026-10-02` (or `your dates`) |
| `{{5}}` | `96500` (already comma-formatted) |
| `{{6}}` | `110000` (baseline/target) |

**Buttons:**

| Index | Label | Payload sent on tap |
|---|---|---|
| 0 | `Book this fare` | `beep:<subscription_id>` → routed by the webhook tap handler, re-checks the fare and opens booking |
| 1 | `Dismiss` | `dismiss` → handled as **deliberate silence** (`cards.translate_tap` maps it to None by design; no error, no reply) |

> Dismissing tells the bot nothing actionable — the beep stays active. To
> actually silence a beep, the user replies **PAUSE** (pauses that
> subscription, `RESUME` brings it back, `CANCEL` deletes it). If you'd
> rather Dismiss pause the beep that triggered it, that's a small
> enhancement — say the word.

---

## Submission checklist (WhatsApp Manager)

1. WhatsApp Manager → **Message templates** → **Create template**
2. Name: exactly `farebeep_flight_status` / `farebeep_price_drop`
3. Language: **English (US)** · Category as above
4. Type: Standard (body) — add the 2 quick-reply buttons only on the
   price-drop template, labels exactly as above
5. Submit → approvals are typically minutes to a day
6. After approval, both names already match `config.py`
   (`META_TEMPLATE_FLIGHT_STATUS`, `META_TEMPLATE_PRICE_DROP`) — no code
   change needed. `farebeep_price_drop` is now also pinned in `.env`.

## Sample filled messages (what users will actually see)

**Flight status:**

> ✈️ Flight P47123 update: DELAYED.
>
> Reply TRACK P47123 for live details, or message me here any time.

**Price drop:**

> Hi Ngozi! 📉 Fare Beep: Lagos → Abuja on 2026-10-02 just dropped to NGN 96,500 — your target was NGN 110,000.
>
> Tap below and I'll re-check it live before you pay.
>
> [Book this fare] [Dismiss]
