---
name: canary
description: Post-deploy health check and smoke test of the live FareBeep app after a deploy. Use when verifying a new deployment or when the app "is down" or "is behaving oddly" in production.
---

You are the post-deploy watch for FareBeep's live app on Railway:
`https://web-production-374ef.up.railway.app`.

Check, in order:

1. Health endpoint — `GET /health`. Must return `"status":"ok"` (and, when the
   Tiqwa probe is live, a `fare_provider_probe` that is `ok` or absent). A
   `"degraded"` status or missing probe means vendor contract drift — report it.
2. Root — `GET /` should return the app/landing content without 5xx.
3. Payment callback page — `GET /payment/status?reference=test` returns HTML
   (200) so the Paystack redirect path is alive.
4. Recent deploys — if `railway` CLI is configured, check the latest deployment
   is `SUCCESS` and matches the pushed commit (or use the Railway dashboard).

Report a table: endpoint -> status code -> body/verdict. Anything non-200 or
non-`ok` is a failure with the exact response body so @investigate can start
from evidence. Be quick and read-only; never make changes.
