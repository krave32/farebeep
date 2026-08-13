"""FareBeep - a Transactional Utility (NOT a SaaS).

Modules:
  database.py     - SQLAlchemy over Supabase Postgres
  models.py       - User + Subscription (migrated from naijafly) + ledger/
                    booking/status tables
  iata.py         - the local city->IATA dictionary (never trust the LLM)
  search.py       - THE SHARED LEDGER (ledger-first, SerpApi on miss, UPSERT)
  transactions.py - THE 10-MINUTE LOOP (booking_session + Paystack + refund flag)
  brain.py        - Gemini 1.5 Flash intent parsing (concise JSON only)
  main.py         - Meta Cloud API webhook (handshake + hmac receiver)
  notifier.py     - Meta WhatsApp outbound (text + templates)
  status.py       - Aviationstack status monitor (3-hour watch window)
  worker.py       - background loops (expiry sweep + status beep)
"""
