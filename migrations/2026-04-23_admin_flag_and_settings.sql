-- wikihub-3w46 + wikihub-2jn.2: add is_admin flag on users and admin_settings
-- key/value table for server-wide toggles (curator_enabled etc).
--
-- Why:
--   * wikihub-3w46 — mint a real admin role so we can stop juggling ADMIN_TOKEN
--     query params. Seeds jacobcole so the /admin index is reachable on day one.
--   * wikihub-2jn.2 — admin-settings store backs the Curator enable/disable
--     toggle. DB value overrides the CURATOR_ENABLED env default when set.
--
-- Apply locally:
--   psql wikihub -f migrations/2026-04-23_admin_flag_and_settings.sql
-- Apply on dev box:
--   ssh -i ~/.ssh/wikihub-dev-key ubuntu@54.145.123.7 \
--     'cd /opt/wikihub-app && sudo -u postgres psql -d wikihub \
--        -f migrations/2026-04-23_admin_flag_and_settings.sql'
--
-- Idempotent: safe to re-run.

BEGIN;

ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS admin_settings (
    id          SERIAL PRIMARY KEY,
    key         VARCHAR(128) NOT NULL UNIQUE,
    value       TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_admin_settings_key ON admin_settings(key);

-- Seed jacobcole as admin (no-op if the account doesn't exist yet).
UPDATE users SET is_admin = TRUE WHERE username = 'jacobcole';

COMMIT;
