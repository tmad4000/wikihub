-- wikihub-20ct: per-user wiki cap override.
--
-- Why: the wiki cap was a single flat constant (MAX_WIKIS_PER_USER = 50). The
-- owner (@jacobcole) hit it. We raised the registered default to 500 in config
-- and added a nullable per-user override that wins when set. NULL means "use
-- the config default". Set a large value to make a specific account
-- effectively unlimited without hardcoding a username anywhere.
--
-- To grant the owner an effectively-unlimited cap on prod (user_id=4):
--   UPDATE users SET wiki_limit = 100000 WHERE username = 'jacobcole';
-- (see scripts/set_wiki_limit.py for a flask-shell helper.)
--
-- Apply on prod (GCP):
--   gcloud compute scp migrations/2026-07-16_user_wiki_limit.sql \
--     wikihub-prod:/tmp/ --project=wikihub-prod --zone=us-east1-b
--   gcloud compute ssh wikihub-prod --project=wikihub-prod --zone=us-east1-b \
--     --command='sudo -u postgres psql -d wikihub -f /tmp/2026-07-16_user_wiki_limit.sql'
--
-- Idempotent: safe to re-run.

BEGIN;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS wiki_limit INTEGER NULL;

COMMIT;
