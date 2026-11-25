-- wikihub-3add: page_annotations table (reader comments on pages / passages).
--
-- Fresh and existing databases also get this table from db.create_all() at
-- app startup (created by the app role, so already correctly owned). This
-- file is for applying it ahead of a restart, and it is idempotent.
--
-- Production applies migrations as the PostgreSQL administrator while the app
-- connects as the role that owns public.wikis, so the DO block at the end
-- hands the table and its sequence to that role (a postgres-owned table made
-- every request 500 on 2026-11-10).
--
-- Apply (production):
--   gcloud compute scp migrations/2026-11-25_page_annotations.sql wikihub-prod:/tmp/ \
--     --project=wikihub-prod --zone=us-east1-b --tunnel-through-iap
--   gcloud compute ssh wikihub-prod --project=wikihub-prod --zone=us-east1-b --tunnel-through-iap \
--     --command='sudo -u postgres psql -d wikihub -v ON_ERROR_STOP=1 -f /tmp/2026-11-25_page_annotations.sql'
-- Then check ownership (expect the app role, e.g. wikihub, on both rows):
--   sudo -u postgres psql -d wikihub -c "SELECT relname, pg_get_userbyid(relowner) FROM pg_class
--     WHERE relname IN ('page_annotations','page_annotations_id_seq')"

CREATE TABLE IF NOT EXISTS page_annotations (
    id          SERIAL PRIMARY KEY,
    wiki_id     INTEGER NOT NULL REFERENCES wikis(id) ON DELETE CASCADE,
    page_path   TEXT NOT NULL,
    page_id     INTEGER,
    author_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    request_id  VARCHAR(36) NOT NULL,
    body        TEXT NOT NULL,
    quote       TEXT NOT NULL DEFAULT '',
    owner_only  BOOLEAN NOT NULL DEFAULT FALSE,
    revision    VARCHAR(64),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_page_annotations_author_request UNIQUE (author_id, request_id)
);

CREATE INDEX IF NOT EXISTS ix_page_annotations_page
    ON page_annotations (wiki_id, page_path, created_at, id);

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

    EXECUTE format('ALTER TABLE public.page_annotations OWNER TO %I', app_owner);
    EXECUTE format('ALTER SEQUENCE public.page_annotations_id_seq OWNER TO %I', app_owner);
END
$$;
