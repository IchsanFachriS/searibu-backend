-- Migration 002: Add role column to users table
-- Run in Supabase SQL editor AFTER 001_initial_schema.sql

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'general';

-- Valid values: 'general' | 'researcher'
-- general    = Pengguna Umum / Masyarakat Umum
-- researcher = Peneliti / Profesional

COMMENT ON COLUMN users.role IS 'User role: general (Pengguna Umum) or researcher (Peneliti/Profesional)';

-- Update index to include role for fast lookups
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);