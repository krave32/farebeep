---
name: ship
description: Take finished work to production - tests, review, conventional commit, changelog, push (CI), then verify the Railway deploy. Use when work is done and ready to deploy ("ship it", "deploy", "release").
---

You run the release workflow end to end for FareBeep (git -> GitHub Actions ->
Railway auto-deploy). Nothing ships broken.

Steps, in order, stopping at the first failure:

1. Verify clean intent — `git status`; confirm only intended files changed and
   no secrets (grep the diff for `sk-`, tokens, the Supabase DB URL, real API
   keys). Never commit `.env` or secrets.
2. Test — full suite green:
   `.\venv\Scripts\python.exe -m pytest FareBeep\tests -q`
   If new code has no test, add one or do not ship.
3. Review — run a read-only self-review (correctness, edge cases, vendor-payload
   resilience via providers.py). Optionally suggest invoking @reviewer.
4. Commit — one conventional commit: `fix:` / `feat:` / `refactor:` /
   `chore:` / `test:`, concise subject, body explaining WHY. Matches repo style.
5. Push + CI — `git push`; confirm GitHub Actions goes green (GitHub CLI:
   `gh run list` / `gh run watch`).
6. Deploy verify — after the push, Railway auto-deploys. Confirm the live app
   is healthy: `GET https://web-production-374ef.up.railway.app/health` returns
   `"status":"ok"`. Report the new deployment ID from Railway if reachable.
7. Changelog — one line per release in a short note for the user (what shipped,
   what to watch).

Report: the commit hash, CI result, deployment status, and the health-check
JSON. If any step fails, stop and hand back to @investigate with the evidence.
