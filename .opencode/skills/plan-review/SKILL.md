---
name: plan-review
description: Review a feature or plan before building it - scope challenge, edge cases, test plan, deploy plan. Use when starting a new task, feature, or big refactor ("plan this", "what's the risk").
---

You are reviewing a plan the way a CEO and a senior engineer would, before any
code is written. This is FareBeep: a Python/FastAPI travel utility on Supabase
+ Railway with pytest CI. Think in terms of THIS codebase.

Run the review on the task/feature at hand:

1. Scope challenge — is this feature essential to the current goal (launch on
   WhatsApp via Tiqwa)? Can it ship smaller? Cut anything that isn't load-bearing.
2. Architecture fit — does it touch the shared ledger (search.py UPSERT),
   the 10-minute booking window, intent parsing (brain.py), or the payment
   path? Those are sacred; changes there need explicit justification.
3. Edge cases — what breaks on: rate limits (Gemini ~6/min), vendor payload
   drift (providers.py), double webhook delivery, expired booking sessions,
   naira volatility (FX guardrail)?
4. Test plan — name the specific pytest files/tests that must cover this.
   No behavior ships without a test.
5. Deploy plan — what env vars (config.py), schema changes (schema.sql),
   Railway deploys, and rollback look like. Include a PII/NDPA note if it
   touches passenger data.

Output: a concise decision — SHIP-AS-IS / SHIP-TRIMMED / REDESIGN — with the
3 highest-risk points and their mitigations. No code.
