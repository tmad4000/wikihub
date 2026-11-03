-- wikihub-hnp4: /people "Recently edited" sort aggregates MAX(pages.updated_at)
-- per wiki owner, and the reader's "Recently updated" block orders a wiki's
-- pages by updated_at. A composite (wiki_id, updated_at) btree serves both
-- (btrees scan in either direction, so no DESC needed).
--
-- Idempotent. Apply on production (GCP):
--   gcloud compute scp migrations/2026-11-03_pages_wiki_updated_idx.sql wikihub-prod:/tmp/ --project=wikihub-prod --zone=us-east1-b --tunnel-through-iap
--   gcloud compute ssh wikihub-prod --project=wikihub-prod --zone=us-east1-b --tunnel-through-iap \
--     --command='sudo -u postgres psql -d wikihub -f /tmp/2026-11-03_pages_wiki_updated_idx.sql'
CREATE INDEX IF NOT EXISTS ix_pages_wiki_updated_at ON pages (wiki_id, updated_at);
