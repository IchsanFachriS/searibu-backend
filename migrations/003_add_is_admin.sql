-- Migration 003: Add is_admin column to users table
-- Run in Supabase SQL editor AFTER 002_add_role.sql

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN users.is_admin IS 'Super admin flag — grants full access to all features without subscription requirement';

CREATE INDEX IF NOT EXISTS idx_users_is_admin ON users(is_admin) WHERE is_admin = TRUE;

-- To promote a user to admin, run:
-- UPDATE users SET is_admin = TRUE WHERE email = 'your@email.com';