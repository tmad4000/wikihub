-- wikihub-skp7: pending_invites table for share-by-email-before-signup.
--
-- When a wiki is shared to an email that has no account yet, we stash a
-- PendingInvite row instead of failing. On email verification (e.g. Google
-- OAuth or a future /auth/verify-email flow), invites for that address are
-- materialized into the wiki's .wikihub/acl file.
--
-- Applied on prod with:
--   ssh ubuntu@54.145.123.7 "sudo -u postgres psql -d wikihub -f -" \
--     < migrations/2026-04-21_pending_invites.sql
--
-- Idempotent: safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS pending_invites (
    id            SERIAL PRIMARY KEY,
    wiki_id       INTEGER NOT NULL REFERENCES wikis(id) ON DELETE CASCADE,
    pattern       VARCHAR(512) NOT NULL,
    email         VARCHAR(256) NOT NULL,
    role          VARCHAR(16)  NOT NULL,
    invited_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_pending_invite UNIQUE (wiki_id, pattern, email)
);

CREATE INDEX IF NOT EXISTS ix_pending_invites_email ON pending_invites (email);

-- The app connects as role `wikihub` but this migration is typically run as
-- `postgres`; without an explicit ownership/grant, the app gets
-- `permission denied for table pending_invites`. Transfer ownership so the
-- app has full CRUD without a separate GRANT dance on every index.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'wikihub') THEN
        EXECUTE 'ALTER TABLE pending_invites OWNER TO wikihub';
        EXECUTE 'ALTER SEQUENCE pending_invites_id_seq OWNER TO wikihub';
    END IF;
END$$;

COMMIT;
