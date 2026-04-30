-- wikihub-yqe6: indexes for backlinks lookup.
--
-- Why: app/backlinks.py:get_backlinks_for_page issues two queries against
-- the wikilinks table:
--   (1) WHERE target_page_id = :id              -- primary backlink lookup
--   (2) WHERE target_page_id IS NULL
--       AND target_path IN (:aliases)           -- forward-ref fallback
--       AND source_page.wiki_id = :wiki_id      (joined)
-- Without indexes both queries are sequential scans of wikilinks. Hub pages
-- (e.g. Body Masters) accumulate hundreds of incoming refs over time and the
-- reader view + new /backlinks API both hit this on every page load.
--
-- Apply locally:
--   psql wikihub -f migrations/2026-04-29_wikilinks_backlink_indexes.sql
-- Apply on dev box:
--   ssh -i ~/.ssh/wikihub-dev-key ubuntu@54.145.123.7 \
--     'cd /opt/wikihub-app && sudo -u postgres psql -d wikihub -f migrations/2026-04-29_wikilinks_backlink_indexes.sql'
--
-- Idempotent: safe to re-run.

BEGIN;

-- Primary lookup: backlinks where the target resolved cleanly at refresh-time.
CREATE INDEX IF NOT EXISTS ix_wikilinks_target_page_id
    ON wikilinks (target_page_id)
    WHERE target_page_id IS NOT NULL;

-- Forward-ref fallback: unresolved targets matched by raw path/alias.
-- Partial index keeps it small — most rows have target_page_id set.
CREATE INDEX IF NOT EXISTS ix_wikilinks_unresolved_target_path
    ON wikilinks (target_path)
    WHERE target_page_id IS NULL;

COMMIT;
