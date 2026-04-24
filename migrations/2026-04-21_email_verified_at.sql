-- wikihub-ks5t.1: email_verified_at column + partial unique index on verified emails.
--
-- Why: the old table-level UNIQUE on users.email enforced uniqueness even for
-- unverified claims, meaning the first user to type foo@bar.com as an
-- (unverified) email blocked the real foo@bar.com owner from ever registering.
-- After this migration, uniqueness is enforced ONLY on verified emails, so
-- multiple users may temporarily claim the same unverified email and the first
-- to verify wins.
--
-- Applied on prod with:
--   ssh ubuntu@54.145.123.7 "sudo -u postgres psql -d wikihub -f -" \
--     < migrations/2026-04-21_email_verified_at.sql
--
-- Idempotent: safe to re-run.

BEGIN;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS email_verified_at TIMESTAMPTZ NULL;

ALTER TABLE users
    DROP CONSTRAINT IF EXISTS users_email_key;

CREATE UNIQUE INDEX IF NOT EXISTS ux_users_email_verified
    ON users (email)
    WHERE email_verified_at IS NOT NULL;

COMMIT;
