-- wikihub-39pe: Ideaflow ID as a confidential OIDC relying party.
--
-- external_identities maps an external OIDC (issuer, subject) pair onto an
-- existing local WikiHub user id. Rows are immutable once created — the app
-- never updates issuer/subject in place, only inserts or (never) deletes.
-- Two uniqueness constraints enforce the invariants from
-- ~/memory/research/global-identity-architecture-2026-09-16.md:
--   (issuer, subject)  -> at most one local user  (one global identity, one account)
--   (user_id, issuer)  -> at most one subject      (one account, one link per issuer)
--
-- Apply on production:
--   gcloud compute scp migrations/2026-09-17_external_identities.sql wikihub-prod:/tmp/ \
--     --project=wikihub-prod --zone=us-east1-b
--   gcloud compute ssh wikihub-prod --project=wikihub-prod --zone=us-east1-b \
--     --command='sudo -u postgres psql -d wikihub -f /tmp/2026-09-17_external_identities.sql'
--
-- Idempotent: safe to re-run. Purely additive — does not touch users, wikis,
-- or any other existing table, so it does not affect existing local user
-- ids, ownership, sessions, Google OAuth, password, or API-key login.

CREATE TABLE IF NOT EXISTS external_identities (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    issuer VARCHAR(256) NOT NULL,
    subject VARCHAR(256) NOT NULL,
    email VARCHAR(256),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_external_identities_user_id ON external_identities(user_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_external_identity_issuer_subject
    ON external_identities(issuer, subject);
CREATE UNIQUE INDEX IF NOT EXISTS uq_external_identity_user_issuer
    ON external_identities(user_id, issuer);

-- Same ownership convention as migrations/2026-09-15_custom_domains.sql:
-- production migrations run as the PostgreSQL administrator, while WikiHub
-- connects as the application role that owns `users`. Keep the new table
-- and its SERIAL sequence under that role.
DO $$
DECLARE
    app_owner name;
BEGIN
    SELECT tableowner::name
      INTO app_owner
      FROM pg_tables
     WHERE schemaname = 'public'
       AND tablename = 'users';

    IF app_owner IS NULL THEN
        RAISE EXCEPTION 'Cannot determine WikiHub application role from public.users';
    END IF;

    EXECUTE format('ALTER TABLE public.external_identities OWNER TO %I', app_owner);
    EXECUTE format('ALTER SEQUENCE public.external_identities_id_seq OWNER TO %I', app_owner);
END
$$;
