-- wikihub-9nfh: verified external hostnames for public wiki routing.
-- Apply on production:
--   sudo -u postgres psql wikihub < /tmp/2026-09-15_custom_domains.sql
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
