-- ============================================================
-- Searibu — PostgreSQL Schema (Supabase)
-- Migration dari SQLite: auth.db + luwes_raw.db + billing.db
--
-- Jalankan di Supabase SQL Editor:
--   1. Buka https://supabase.com/dashboard
--   2. Pilih project → SQL Editor → New Query
--   3. Paste seluruh file ini → Run
-- ============================================================

-- ── Extensions ─────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================
-- 1. TABEL USERS  (dari auth.db)
-- ============================================================
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

-- ============================================================
-- 2. TABEL WATER LEVEL OBSERVATIONS  (dari luwes_raw.db)
-- ============================================================
CREATE TABLE IF NOT EXISTS water_level_observations (
    id           SERIAL PRIMARY KEY,
    rec          INTEGER     NOT NULL UNIQUE,   -- ID unik dari Luwes API
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

-- ============================================================
-- 3. TABEL FETCH LOG  (dari luwes_raw.db)
-- ============================================================
CREATE TABLE IF NOT EXISTS fetch_log (
    id          SERIAL PRIMARY KEY,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    imei        TEXT,
    status      TEXT,          -- 'ok' | 'duplicate' | 'error'
    rec         INTEGER,
    level_m     REAL,
    recorded_at TIMESTAMPTZ,
    message     TEXT
);

CREATE INDEX IF NOT EXISTS idx_fetch_log_imei_time ON fetch_log(imei, fetched_at);

-- ============================================================
-- 4. TABEL SUBSCRIPTIONS  (dari billing.db)
-- ============================================================
CREATE TABLE IF NOT EXISTS subscriptions (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER     NOT NULL UNIQUE REFERENCES users(id),
    plan        TEXT        NOT NULL DEFAULT 'free',
        -- 'free' | 'pro_monthly' | 'pro_annual'
    status      TEXT        NOT NULL DEFAULT 'active',
        -- 'active' | 'expired' | 'cancelled'
    starts_at   TIMESTAMPTZ,
    expires_at  TIMESTAMPTZ,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sub_user ON subscriptions(user_id);

-- ============================================================
-- 5. TABEL PAYMENTS  (dari billing.db)
-- ============================================================
CREATE TABLE IF NOT EXISTS payments (
    id            SERIAL PRIMARY KEY,
    user_id       INTEGER     NOT NULL REFERENCES users(id),
    order_id      TEXT        NOT NULL UNIQUE,
    snap_token    TEXT,
    plan          TEXT        NOT NULL,
    amount_idr    INTEGER     NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'pending',
        -- 'pending' | 'settlement' | 'expire' | 'cancel' | 'deny'
    midtrans_id   TEXT,
    payment_type  TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    settled_at    TIMESTAMPTZ,
    raw_webhook   JSONB
);

CREATE INDEX IF NOT EXISTS idx_payments_order  ON payments(order_id);
CREATE INDEX IF NOT EXISTS idx_payments_user   ON payments(user_id);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);

-- ============================================================
-- 6. ROW LEVEL SECURITY (RLS) — opsional tapi direkomendasikan
--    Aktifkan jika menggunakan Supabase Auth atau API langsung
-- ============================================================

-- Nonaktifkan RLS untuk tabel yang hanya diakses via backend
-- (backend menggunakan service_role key yang bypass RLS)
ALTER TABLE users                      DISABLE ROW LEVEL SECURITY;
ALTER TABLE water_level_observations   DISABLE ROW LEVEL SECURITY;
ALTER TABLE fetch_log                  DISABLE ROW LEVEL SECURITY;
ALTER TABLE subscriptions              DISABLE ROW LEVEL SECURITY;
ALTER TABLE payments                   DISABLE ROW LEVEL SECURITY;

-- ============================================================
-- Verifikasi — jalankan setelah migration untuk cek tabel
-- ============================================================
-- SELECT table_name FROM information_schema.tables
-- WHERE table_schema = 'public'
-- ORDER BY table_name;