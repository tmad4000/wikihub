-- wikihub-yjsv: token-backed one-click invite verification.
--
-- Each PendingInvite now has a random token embedded in the invite-email URL
-- as ?it=<token>. Signup/login with a valid token counts as proof of email
-- ownership — the click itself becomes the verification event.
--
-- Applied on prod with:
--   ssh ubuntu@54.145.123.7 "sudo -u postgres psql -d wikihub -f -" \
--     < migrations/2026-04-22_pending_invites_token.sql
--
-- Idempotent: safe to re-run. Nullable column so pre-existing rows still
-- work via the fallback verify-email flow.

BEGIN;

ALTER TABLE pending_invites
    ADD COLUMN IF NOT EXISTS token VARCHAR(64) NULL;

CREATE INDEX IF NOT EXISTS ix_pending_invites_token
    ON pending_invites (token)
    WHERE token IS NOT NULL;

COMMIT;
