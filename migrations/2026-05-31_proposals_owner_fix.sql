-- wikihub-bug-fix: prod 500 on every wiki-page render (any deep path), logged in
-- as the owner. Trace: app/routes/wiki.py:1432 does
--   Proposal.query.filter_by(wiki_id=..., page_path=..., status='pending').count()
-- which 500s with: psycopg2.errors.InsufficientPrivilege: permission denied for table proposals.
--
-- Root cause: migrations/2026-05-08_suggested_edit_proposals.sql and
-- migrations/2026-05-08_proposal_comments.sql were applied as the `postgres`
-- superuser (per their header instructions). The new tables and their
-- sequences ended up owned by `postgres`, not by the app role `wikihub`
-- (which owns every other table in the schema). The `wikihub` role then has
-- no SELECT/INSERT/UPDATE/DELETE on them, so any page render that touches
-- the table for the owner-pending-proposals count blows up.
--
-- All sibling pre-existing tables (pages, wikis, ...) are owned by `wikihub`,
-- so the correct fix is to transfer ownership of the four new proposal tables
-- (and their sequences) to `wikihub`. This is idempotent — re-runs are no-ops.
--
-- Apply:
--   gcloud compute scp migrations/2026-05-31_proposals_owner_fix.sql \
--     wikihub-prod:/tmp/ --project=wikihub-prod --zone=us-east1-b \
--   && gcloud compute ssh wikihub-prod --project=wikihub-prod --zone=us-east1-b \
--        --command='sudo -u postgres psql -d wikihub -f /tmp/2026-05-31_proposals_owner_fix.sql'

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename='proposals') THEN
        EXECUTE 'ALTER TABLE proposals OWNER TO wikihub';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename='proposal_revisions') THEN
        EXECUTE 'ALTER TABLE proposal_revisions OWNER TO wikihub';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename='proposal_page_patches') THEN
        EXECUTE 'ALTER TABLE proposal_page_patches OWNER TO wikihub';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename='proposal_comments') THEN
        EXECUTE 'ALTER TABLE proposal_comments OWNER TO wikihub';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname='proposals_id_seq' AND relkind='S') THEN
        EXECUTE 'ALTER SEQUENCE proposals_id_seq OWNER TO wikihub';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname='proposal_revisions_id_seq' AND relkind='S') THEN
        EXECUTE 'ALTER SEQUENCE proposal_revisions_id_seq OWNER TO wikihub';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname='proposal_page_patches_id_seq' AND relkind='S') THEN
        EXECUTE 'ALTER SEQUENCE proposal_page_patches_id_seq OWNER TO wikihub';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname='proposal_comments_id_seq' AND relkind='S') THEN
        EXECUTE 'ALTER SEQUENCE proposal_comments_id_seq OWNER TO wikihub';
    END IF;
END $$;
