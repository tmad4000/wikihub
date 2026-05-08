-- wikihub-7cus: proposal discussion comments and request-changes workflow.
--
-- Apply:
--   gcloud compute ssh --project=wikihub-prod --zone=us-east1-b wikihub-prod --command "sudo -u postgres psql -d wikihub -f -" < migrations/2026-05-08_proposal_comments.sql

CREATE TABLE IF NOT EXISTS proposal_comments (
    id SERIAL PRIMARY KEY,
    proposal_id INTEGER NOT NULL REFERENCES proposals(id) ON DELETE CASCADE,
    author_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    author_name VARCHAR(256),
    body TEXT NOT NULL,
    event VARCHAR(32) NOT NULL DEFAULT 'comment',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_proposal_comments_proposal_id ON proposal_comments(proposal_id);
