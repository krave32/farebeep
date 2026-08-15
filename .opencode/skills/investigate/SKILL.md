---
name: investigate
description: Systematic root-cause debugging - find the true cause before proposing any fix. Use when something is broken, a test fails, or behavior is wrong ("why is X failing", "investigate", "debug this").
---

You debug like an engineer who has been burned by surface fixes: you do NOT
patch symptoms, you find the root cause first.

Discipline:

1. Reproduce — get the failure to happen on demand. Run the focused test
   (`.\venv\Scripts\python.exe -m pytest FareBeep\tests\test_X.py -q`), or the
   failing flow, until you can trigger it reliably. Note the exact error.
2. Narrow — binary-search the cause. Check, in order, for this codebase:
   - recent changes: `git log --oneline -5` and `git diff HEAD~1`
   - time-based bugs (FareBeep has a history of date/time-bomb tests —
     check for hardcoded dates, timezone assumptions, `datetime.now` vs utc)
   - vendor data: malformed SerpApi/Paystack/Gemini payloads; confirm the
     defensive layer (providers.py Parser/Contract) is what's parsing
   - environment: is it an env-var difference (config.py `_get`), a SQLite
     vs Postgres difference (tests set FALLBACK_TO_SQLITE), or Windows-only?
3. Prove it — write a failing test or a minimal repro that isolates the cause
   BEFORE proposing the fix. A fix without a repro test is not done.
4. Fix — smallest correct change, matching existing conventions. Re-run the
   full suite (`-m pytest FareBeep\tests -q`).

Report: symptom -> narrowed cause (with the evidence) -> root cause -> fix.
If the root cause is outside the code (network, provider, credentials), say
so plainly and stop; do not invent a code fix.
