-- wikihub-b6lc: suggested edit proposal flow.
-- Adds durable review objects, revisions, and page patches for non-destructive
-- suggested edits.
--
-- Apply:
--   gcloud compute ssh --project=wikihub-prod --zone=us-east1-b wikihub-prod --command "sudo -u postgres psql -d wikihub -f -" < migrations/2026-05-08_suggested_edit_proposals.sql

CREATE TABLE IF NOT EXISTS proposals (
    id SERIAL PRIMARY KEY,
    wiki_id INTEGER NOT NULL REFERENCES wikis(id) ON DELETE CASCADE,
    page_id INTEGER REFERENCES pages(id) ON DELETE SET NULL,
    page_path TEXT NOT NULL,
    author_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    author_name VARCHAR(256),
    title VARCHAR(256) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    base_content_hash VARCHAR(64),
    reviewed_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_proposals_wiki_id ON proposals(wiki_id);
CREATE INDEX IF NOT EXISTS ix_proposals_page_id ON proposals(page_id);
CREATE INDEX IF NOT EXISTS ix_proposals_status ON proposals(status);

CREATE TABLE IF NOT EXISTS proposal_revisions (
    id SERIAL PRIMARY KEY,
    proposal_id INTEGER NOT NULL REFERENCES proposals(id) ON DELETE CASCADE,
    revision_number INTEGER NOT NULL,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_proposal_revision_number UNIQUE (proposal_id, revision_number)
);

CREATE INDEX IF NOT EXISTS ix_proposal_revisions_proposal_id ON proposal_revisions(proposal_id);

CREATE TABLE IF NOT EXISTS proposal_page_patches (
    id SERIAL PRIMARY KEY,
    revision_id INTEGER NOT NULL REFERENCES proposal_revisions(id) ON DELETE CASCADE,
    page_path TEXT NOT NULL,
    base_content_hash VARCHAR(64),
    base_content TEXT NOT NULL,
    proposed_content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_proposal_page_patches_revision_id ON proposal_page_patches(revision_id);
