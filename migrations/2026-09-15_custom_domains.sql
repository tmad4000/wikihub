-- wikihub-9nfh: verified external hostnames for public wiki routing.
-- Apply on production:
--   gcloud compute scp migrations/2026-09-15_custom_domains.sql wikihub-prod:/tmp/ \
--     --project=wikihub-prod --zone=us-east1-b
--   gcloud compute ssh wikihub-prod --project=wikihub-prod --zone=us-east1-b \
--     --command='sudo -u postgres psql -d wikihub -f /tmp/2026-09-15_custom_domains.sql'
CREATE TABLE IF NOT EXISTS custom_domains (
    id SERIAL PRIMARY KEY,
    wiki_id INTEGER NOT NULL REFERENCES wikis(id) ON DELETE CASCADE,
    hostname VARCHAR(253) NOT NULL UNIQUE,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    verification_token VARCHAR(64) NOT NULL UNIQUE,
    verified_at TIMESTAMPTZ,
    tls_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    provider VARCHAR(32),
    provider_hostname_id VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_custom_domains_wiki_id ON custom_domains(wiki_id);
CREATE INDEX IF NOT EXISTS ix_custom_domains_hostname ON custom_domains(hostname);
CREATE UNIQUE INDEX IF NOT EXISTS uq_custom_domains_one_active_per_wiki
    ON custom_domains(wiki_id) WHERE status = 'active';

-- Production migrations are applied as the PostgreSQL administrator, while
-- WikiHub connects as the same application role that owns the existing
-- `wikis` table. Keep the new table and its SERIAL sequence under that role;
-- otherwise normal requests cannot read or write custom-domain state.
DO $$
DECLARE
    app_owner name;
BEGIN
    SELECT tableowner::name
      INTO app_owner
      FROM pg_tables
     WHERE schemaname = 'public'
       AND tablename = 'wikis';

    IF app_owner IS NULL THEN
        RAISE EXCEPTION 'Cannot determine WikiHub application role from public.wikis';
    END IF;

    EXECUTE format('ALTER TABLE public.custom_domains OWNER TO %I', app_owner);
    EXECUTE format('ALTER SEQUENCE public.custom_domains_id_seq OWNER TO %I', app_owner);
END
$$;
