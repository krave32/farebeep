---
name: retro
description: Weekly engineering retrospective from git history - what shipped, what stalled, what to fix next. Use for a period review ("retro", "weekly review", "what did we do").
---

You run the weekly retrospective for FareBeep from the git record and current
state. Read-only; no changes.

Sources: `git log --oneline --since="7 days ago"` (or the period asked for),
`git status`, the open todo list, and CI status (`gh run list` if configured).

Produce, concisely:

1. Shipped — each commit grouped by theme (features, fixes, hardening,
   infrastructure) with its hash. One line each.
2. Health — tests passing count, CI green?, production /health state, any
   incidents (rate limits, deploy failures, vendor drift).
3. Stalled / stuck — work started but not finished, blocked items (e.g.
   Tiqwa token pending, Infobip credentials), and WHY.
4. Trends — repeated failure patterns (e.g. time-bomb tests, rate-limit
   fallbacks, retry storms) worth a permanent fix.
5. Next week — max 3 concrete priorities tied to the strategy (WhatsApp
   launch on Tiqwa), each with a suggested first step.

Keep it to a page. Be honest about what did NOT get done; the point is
learning, not celebration.
