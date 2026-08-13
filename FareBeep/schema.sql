-- ============================================================================
-- FareBeep - Transactional Utility
-- Supabase SQL schema. Run this in the Supabase SQL Editor (Dashboard -> SQL)
-- or with: psql "$SUPABASE_DB_URL" -f schema.sql
--
-- This schema is the "shared ledger" + transactional state machine.
-- It replaces the SaaS-style structures in naijafly with a minimal,
-- transaction-centric store:
--   users            - identity of a Meta/WhatsApp phone number
--   subscriptions    - (migrated from naijafly.models.UserSubscription)
--                      route + target-price, flattened to IATA codes
--   fare_ledger      - community search cache (15-min TTL), one row per
--                      (origin, destination, flight_date) - UPSERT target
--   booking_sessions - the 10-minute transactional loop state machine
--   status_watches   - per-booking flight status watch (3h pre-departure)
--   status_events    - status-change log + template-message dedupe
--   fx_rates         - daily USD->NGN snapshots (price-tracking history)
-- ============================================================================

begin;

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------------------
-- users (extracted from naijafly SeenUser concept; phone-identity based)
-- ---------------------------------------------------------------------------
create table if not exists users (
    user_id           uuid primary key default gen_random_uuid(),
    phone             text not null unique,          -- e.g. +2348012345678
    name              text,
    email             text,
    preferred_currency text not null default 'NGN',
    first_seen_at     timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- subscriptions (migrated from naijafly UserSubscription)
-- ---------------------------------------------------------------------------
create table if not exists subscriptions (
    id            bigint generated always as identity primary key,
    user_id       uuid not null references users(user_id) on delete cascade,
    origin        text not null,          -- IATA code, e.g. 'LOS'
    destination   text not null,          -- IATA code, e.g. 'ABV'
    target_price  numeric,                -- NULL = alert on any >10% drop
    target_date   date,                   -- NULL = rolling window
    last_price          numeric,          -- last observed fare (baseline)
    last_alerted_price  numeric,          -- dedupe: never re-alert same price
    created_at    timestamptz not null default now()
);

create index if not exists idx_subscriptions_user on subscriptions (user_id);

-- ---------------------------------------------------------------------------
-- fare_ledger - THE SHARED LEDGER (community cache)
-- One row per route+date. last_updated drives the 15-minute TTL.
-- ---------------------------------------------------------------------------
create table if not exists fare_ledger (
    id           bigint generated always as identity primary key,
    origin       text not null,           -- IATA, e.g. 'ABV'
    destination  text not null,           -- IATA, e.g. 'PHC'
    flight_date  date not null,
    price        numeric not null,        -- NGN
    currency     text not null default 'NGN',
    airline      text,                    -- best carrier on that route/date
    verify_link  text,                    -- Google Flights link for the user
    last_updated timestamptz not null default now(),
    unique (origin, destination, flight_date)
);

create index if not exists idx_fare_ledger_ttl
    on fare_ledger (origin, destination, flight_date, last_updated);

-- ---------------------------------------------------------------------------
-- booking_sessions - THE SETTLEMENT ENGINE'S 10-MINUTE TRANSACTIONAL LOOP
-- status machine: pending -> paid | expired | failed
-- (paid AFTER expiry flips the row to 'expired' + raises a REFUND REQUIRED
--  alert - the airline API is never called for a late payment)
-- ---------------------------------------------------------------------------
create table if not exists booking_sessions (
    id                uuid primary key default gen_random_uuid(),
    user_id           uuid not null references users(user_id) on delete cascade,
    origin            text not null,
    destination       text not null,
    flight_date       date not null,
    flight_iata       text,               -- e.g. 'P47123' when the user named the flight
    scheduled_departure timestamptz,      -- departure time driving the 3h status watch
    airline_price     numeric not null,          -- NET fare from the LIVE SerpApi hit
    markup            numeric not null default 5000,  -- ARHA_MARKUP_NGN flat margin
    processing_fee    numeric not null,          -- Paystack fee (user-funded)
    total_price       numeric not null,          -- what the user pays (incl. markup + fee)
    currency          text not null default 'NGN',
    flight_details    jsonb,                     -- {airline, route, net_price, source}
    status            text not null default 'pending'
                      check (status in ('pending','paid','expired','failed')),
    expires_at        timestamptz not null,      -- created_at + 10 minutes (price lock)
    payment_ref       text unique,               -- Paystack reference (FB-<hex>)
    paystack_access_code text,
    callback_url      text,
    paid_at           timestamptz,
    created_at        timestamptz not null default now()
);

create index if not exists idx_booking_sessions_status
    on booking_sessions (status, expires_at);
create index if not exists idx_booking_sessions_user
    on booking_sessions (user_id);
create index if not exists idx_booking_sessions_ref
    on booking_sessions (payment_ref);

-- ---------------------------------------------------------------------------
-- status_watches - the 3-hour pre-departure watch window
-- ---------------------------------------------------------------------------
create table if not exists status_watches (
    id                  uuid primary key default gen_random_uuid(),
    booking_id          uuid not null references booking_sessions(id) on delete cascade,
    user_id             uuid not null references users(user_id) on delete cascade,
    flight_iata         text not null,           -- e.g. 'P47123' (Air Peace 7123)
    flight_date         date not null,
    scheduled_departure timestamptz not null,    -- departure stored in booking
    watch_starts_at     timestamptz not null,    -- departure - 3 hours
    last_status         text,                    -- last known aviationstack status
    initiated           boolean not null default false,
    last_checked_at     timestamptz,
    created_at          timestamptz not null default now()
);

create index if not exists idx_status_watches_due
    on status_watches (initiated, watch_starts_at, scheduled_departure);

-- ---------------------------------------------------------------------------
-- status_events - status-change log (template-message dedupe)
-- ---------------------------------------------------------------------------
create table if not exists status_events (
    id            bigint generated always as identity primary key,
    watch_id      uuid not null references status_watches(id) on delete cascade,
    status        text not null,          -- e.g. 'delayed' | 'cancelled' | ...
    previous      text,
    detail        text,
    template_sent boolean not null default false,
    created_at    timestamptz not null default now()
);

create index if not exists idx_status_events_watch
    on status_events (watch_id, created_at);

-- ---------------------------------------------------------------------------
-- fx_rates - USD->NGN price-tracking snapshots (one row per fetch window)
-- Written by worker.record_fx_rate(); read for the naira trend history.
-- ---------------------------------------------------------------------------
create table if not exists fx_rates (
    id         bigint generated always as identity primary key,
    usd_ngn    numeric not null,          -- NGN per 1 USD
    source     text,                      -- e.g. 'open.er-api.com'
    fetched_at timestamptz not null default now()
);

create index if not exists idx_fx_rates_fetched on fx_rates (fetched_at desc);

commit;
