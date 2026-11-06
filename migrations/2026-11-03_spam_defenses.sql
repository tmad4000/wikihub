-- 2026-11-03 spam defenses + moderation (wikihub-9ras, wikihub-1ug3, wikihub-i8lo)
--
-- Why: a link-farm operator and several promotional SEO accounts used instant
-- signup + instant public pages. This adds:
--   * users.trusted          admin-set trust override (see app/trust.py)
--   * users.suspended_at     suspended accounts cannot authenticate; content 410s
--   * users.suspended_reason free-text moderation note
--   * rate_limit_buckets     Postgres-backed fixed-window limiter shared by all workers
-- and marks the owner's accounts trusted.
--
-- Idempotent. Apply (production):
--   gcloud compute scp migrations/2026-11-03_spam_defenses.sql wikihub-prod:/tmp/ \
--     --project=wikihub-prod --zone=us-east1-b --tunnel-through-iap
--   gcloud compute ssh wikihub-prod --project=wikihub-prod --zone=us-east1-b --tunnel-through-iap \
--     --command='sudo -u postgres psql -d wikihub -f /tmp/2026-11-03_spam_defenses.sql'
-- Apply BEFORE restarting the app on the new code (the model references the columns).

ALTER TABLE users ADD COLUMN IF NOT EXISTS trusted BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE users ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ NULL;
ALTER TABLE users ADD COLUMN IF NOT EXISTS suspended_reason TEXT NULL;

CREATE TABLE IF NOT EXISTS rate_limit_buckets (
    key VARCHAR(200) NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key, window_start)
);
CREATE INDEX IF NOT EXISTS ix_rate_limit_buckets_window_start ON rate_limit_buckets (window_start);

UPDATE users SET trusted = true
 WHERE username IN ('jacobcole', 'tmad4000', 'jacob', 'jacobcole-psi', 'wikihub');
