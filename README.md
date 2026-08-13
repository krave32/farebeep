# FareBeep (v2)

Nigerian domestic flight fare Beeps + booking/settlement engine.
Standalone successor to the naijafly prototype - shared ledger is Supabase.

- FastAPI app: `FareBeep/main.py` (Procfile `web`)
- Fare-drop tracking worker: `FareBeep/worker.py` (Procfile `worker`)
- Telegram long-poll transport (no tunnel): `FareBeep/poller.py` (Procfile `poller`)
- Schema: `FareBeep/schema.sql` (run once in Supabase SQL Editor)
- Local SQLite -> Supabase migration: `migrate_local_to_supabase.py`
- Env template: `FareBeep/.env.example` (never commit `.env`)

Railway: create three services from this repo (`web`, `worker`, `poller`)
with `SUPABASE_DB_URL` + the keys from `FareBeep/.env` in Variables.