-- world-issue-tracker-julr.6: in-app "Send feedback" (WIT widget).
--
-- Holds the latest verified Ideaflow ID token per (issuer, subject),
-- Fernet-encrypted with a key derived from SECRET_KEY, so the backend can
-- mint 15-minute WIT feedback tokens (POST /auth/wit-feedback-token). Never
-- sent to the browser. Fresh/production databases get this table from
-- db.create_all() at startup; this file exists for manual/idempotent apply.
--
-- Apply on production (optional; startup create_all already creates it):
--   gcloud compute scp migrations/2026-11-05_ideaflow_feedback_credentials.sql wikihub-prod:/tmp/ \
--     --project=wikihub-prod --zone=us-east1-b --tunnel-through-iap
--   gcloud compute ssh wikihub-prod --project=wikihub-prod --zone=us-east1-b --tunnel-through-iap \
--     --command='sudo -u postgres psql -d wikihub -f /tmp/2026-11-05_ideaflow_feedback_credentials.sql'
--
-- Idempotent and purely additive.

CREATE TABLE IF NOT EXISTS ideaflow_feedback_credentials (
    issuer VARCHAR(256) NOT NULL,
    subject VARCHAR(256) NOT NULL,
    id_token_encrypted TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (issuer, subject)
);

DO $$
DECLARE
    app_owner name;
BEGIN
    SELECT tableowner::name INTO app_owner
      FROM pg_tables WHERE schemaname = 'public' AND tablename = 'users';
    IF app_owner IS NULL THEN
        RAISE EXCEPTION 'Cannot determine WikiHub application role from public.users';
    END IF;
    EXECUTE format('ALTER TABLE public.ideaflow_feedback_credentials OWNER TO %I', app_owner);
END
$$;
