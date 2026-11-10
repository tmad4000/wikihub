-- 2026-11-09 negative + positive curation judgements (wikihub-f3b7)
--
-- Why: the 2026-11-03 trust gate judges account age and verified identity,
-- never content. Promotional accounts that signed up with Google and waited
-- 14 days pass it (diagnosed 2026-11-09: @seo, @deepswapai,
-- @elongflywikihubxb, @productshotai, @visaforma, @cellularport). Jacob's
-- policy (Noos semantic-web plan D4): unless it is "super, super spam", do
-- not delete; curate against it. This table records editorial judgements
-- about an account, wiki or page:
--   negative (promotional, affiliate_farm, automated_link_placement,
--             low_quality, off_topic): banner, noindex, nofollow ugc, out of
--             discovery, sorted last in search. Content stays readable.
--   positive (featured, official, editor_pick): Explore editorial picks.
-- Revoking sets revoked_at; rows are never deleted. See docs/curation.md.
--
-- Idempotent (IF NOT EXISTS / DO-block guards). Additive only: no existing
-- table or row changes, so the old code keeps working against the new
-- schema. NOTE: create_app() runs db.create_all() at startup, so restarting
-- the app on code containing app/models.py:CurationJudgement creates this
-- table (with the same constraints and indexes) even if nobody runs this
-- file. Running it first is still the reviewed path: it makes the DDL an
-- explicit, backed-up step instead of a side effect of the first worker boot.
--
-- Apply (production) — back up first:
--   gcloud compute ssh wikihub-prod --project=wikihub-prod --zone=us-east1-b --tunnel-through-iap \
--     --command='sudo -u postgres pg_dump --format=custom wikihub | sudo tee /var/backups/wikihub-pre-curation-$(date +%Y%m%d%H%M).dump >/dev/null && ls -l /var/backups/wikihub-pre-curation-*.dump'
--   gcloud compute scp migrations/2026-11-09_curation_judgements.sql wikihub-prod:/tmp/ \
--     --project=wikihub-prod --zone=us-east1-b --tunnel-through-iap
--   gcloud compute ssh wikihub-prod --project=wikihub-prod --zone=us-east1-b --tunnel-through-iap \
--     --command='sudo -u postgres psql -v ON_ERROR_STOP=1 -d wikihub -f /tmp/2026-11-09_curation_judgements.sql'
-- Apply BEFORE restarting the app on the new code.
--
-- Rollback (only after reverting the app to a build without app/curation.py;
-- this discards every judgement, so export them first):
--   \copy (SELECT * FROM curation_judgements) TO '/tmp/curation_judgements.csv' CSV HEADER
--   DROP TABLE IF EXISTS curation_judgements;
-- To undo the effect of one judgement without a rollback, use
--   flask --app wsgi.py wikihub unflag TARGET --execute

CREATE TABLE IF NOT EXISTS curation_judgements (
    id SERIAL PRIMARY KEY,
    scope_type VARCHAR(16) NOT NULL,
    scope_id INTEGER NOT NULL,
    polarity VARCHAR(16) NOT NULL,
    reason VARCHAR(40) NOT NULL,
    curator_user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
    note TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ NULL
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_curation_scope_type') THEN
        ALTER TABLE curation_judgements ADD CONSTRAINT ck_curation_scope_type
            CHECK (scope_type IN ('account', 'wiki', 'page'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_curation_polarity') THEN
        ALTER TABLE curation_judgements ADD CONSTRAINT ck_curation_polarity
            CHECK (polarity IN ('positive', 'negative'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_curation_reason') THEN
        ALTER TABLE curation_judgements ADD CONSTRAINT ck_curation_reason
            CHECK ((polarity = 'negative' AND reason IN ('promotional', 'affiliate_farm',
                        'automated_link_placement', 'low_quality', 'off_topic'))
                OR (polarity = 'positive' AND reason IN ('featured', 'official', 'editor_pick')));
    END IF;
END $$;

-- at most one active judgement per (scope, polarity, reason)
CREATE UNIQUE INDEX IF NOT EXISTS uq_curation_active
    ON curation_judgements (scope_type, scope_id, polarity, reason)
    WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_curation_created_at ON curation_judgements (created_at);
CREATE INDEX IF NOT EXISTS ix_curation_revoked_at ON curation_judgements (revoked_at);
