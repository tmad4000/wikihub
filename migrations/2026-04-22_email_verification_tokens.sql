-- wikihub-ks5t.3: email_verification_tokens table.
--
-- Mint a token on signup (or when a user attaches a new email in settings),
-- email a verify link, set email_verified_at on click. Verification is
-- non-blocking — signup + all actions still work immediately. The banner
-- in base.html nudges the user to verify; it doesn't gate anything.
--
-- Applied on prod with:
--   ssh ubuntu@54.145.123.7 "sudo -u postgres psql -d wikihub -f -" \
--     < migrations/2026-04-22_email_verification_tokens.sql
--
-- Idempotent: safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS email_verification_tokens (
    id         SERIAL PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash VARCHAR(256) NOT NULL UNIQUE,
    new_email  VARCHAR(256) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_email_verification_tokens_user_id
    ON email_verification_tokens (user_id);

-- Transfer ownership so the `wikihub` app role can write (see pending_invites migration).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'wikihub') THEN
        EXECUTE 'ALTER TABLE email_verification_tokens OWNER TO wikihub';
        EXECUTE 'ALTER SEQUENCE email_verification_tokens_id_seq OWNER TO wikihub';
    END IF;
END$$;

COMMIT;
