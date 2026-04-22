-- wikihub-7b2r: anonymous posting + claimable flag on pages.
--
-- Why: let agents/users post without attribution. anonymous=True hides
-- Page.author from the API/UI (the column stays populated for audit).
-- claimable=True (only meaningful when anonymous) lets any authed user
-- claim the page first-come-first-served.
--
-- Apply locally:
--   psql wikihub -f migrations/2026-04-21_anonymous_posting.sql
-- Apply on dev box:
--   ssh -i ~/.ssh/wikihub-dev-key ubuntu@54.145.123.7 \
--     'cd /opt/wikihub-app && sudo -u postgres psql -d wikihub -f migrations/2026-04-21_anonymous_posting.sql'
--
-- Idempotent: safe to re-run.

BEGIN;

ALTER TABLE pages ADD COLUMN IF NOT EXISTS anonymous BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE pages ADD COLUMN IF NOT EXISTS claimable BOOLEAN NOT NULL DEFAULT FALSE;

-- partial index: fast lookup of "pages still claimable"
CREATE INDEX IF NOT EXISTS idx_pages_claimable
  ON pages(anonymous, claimable)
  WHERE anonymous = TRUE AND claimable = TRUE;

COMMIT;
