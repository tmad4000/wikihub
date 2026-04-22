-- wikihub-ks5t.5: password_reset_tokens table.
--
-- Forgot-password flow mints a single-use, 30-minute token, emails a reset
-- link, and marks the email verified when the link is redeemed.
--
-- Applied on prod with:
--   ssh ubuntu@54.145.123.7 "sudo -u postgres psql -d wikihub -f -" \
--     < migrations/2026-04-22_password_reset_tokens.sql
--
-- Idempotent: safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id         SERIAL PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash VARCHAR(256) NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_password_reset_tokens_user_id
    ON password_reset_tokens (user_id);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'wikihub') THEN
        EXECUTE 'ALTER TABLE password_reset_tokens OWNER TO wikihub';
        EXECUTE 'ALTER SEQUENCE password_reset_tokens_id_seq OWNER TO wikihub';
    END IF;
END$$;

COMMIT;
