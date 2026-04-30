-- Searibu PostgreSQL schema — run once in the Supabase SQL editor.
--
-- Tables:
--   users                      authentication
--   water_level_observations   Luwes telemetry records
--   fetch_log                  scheduler fetch audit log
--   subscriptions              user billing plan
--   payments                   Midtrans payment records

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ── Users ────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    full_name     TEXT        NOT NULL,
    email         TEXT        NOT NULL UNIQUE,
    password_hash TEXT        NOT NULL,
    salt          TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

-- ── Water level observations ─────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS water_level_observations (
    id           SERIAL PRIMARY KEY,
    rec          INTEGER     NOT NULL UNIQUE,
    station_id   INTEGER,
    station_name TEXT,
    imei         TEXT,
    level_m      REAL,
    recorded_at  TIMESTAMPTZ NOT NULL,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_obs_recorded_at ON water_level_observations(recorded_at);
CREATE INDEX IF NOT EXISTS idx_obs_imei        ON water_level_observations(imei);
CREATE INDEX IF NOT EXISTS idx_obs_rec         ON water_level_observations(rec);

-- ── Fetch log ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS fetch_log (
    id          SERIAL PRIMARY KEY,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    imei        TEXT,
    status      TEXT,
    rec         INTEGER,
    level_m     REAL,
    recorded_at TIMESTAMPTZ,
    message     TEXT
);

CREATE INDEX IF NOT EXISTS idx_fetch_log_imei_time ON fetch_log(imei, fetched_at);

-- ── Subscriptions ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS subscriptions (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER     NOT NULL UNIQUE REFERENCES users(id),
    plan        TEXT        NOT NULL DEFAULT 'free',
    status      TEXT        NOT NULL DEFAULT 'active',
    starts_at   TIMESTAMPTZ,
    expires_at  TIMESTAMPTZ,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sub_user ON subscriptions(user_id);

-- ── Payments ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS payments (
    id            SERIAL PRIMARY KEY,
    user_id       INTEGER     NOT NULL REFERENCES users(id),
    order_id      TEXT        NOT NULL UNIQUE,
    snap_token    TEXT,
    plan          TEXT        NOT NULL,
    amount_idr    INTEGER     NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'pending',
    midtrans_id   TEXT,
    payment_type  TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    settled_at    TIMESTAMPTZ,
    raw_webhook   JSONB
);

CREATE INDEX IF NOT EXISTS idx_payments_order  ON payments(order_id);
CREATE INDEX IF NOT EXISTS idx_payments_user   ON payments(user_id);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);

-- Disable RLS for tables accessed exclusively via the backend service role.
ALTER TABLE users                    DISABLE ROW LEVEL SECURITY;
ALTER TABLE water_level_observations DISABLE ROW LEVEL SECURITY;
ALTER TABLE fetch_log                DISABLE ROW LEVEL SECURITY;
ALTER TABLE subscriptions            DISABLE ROW LEVEL SECURITY;
ALTER TABLE payments                 DISABLE ROW LEVEL SECURITY;