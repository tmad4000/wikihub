-- Durable one-use delegated assertions; no token or content storage.
-- Apply: psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/2026-11-09_connector_assertion_nonces.sql
CREATE TABLE IF NOT EXISTS connector_assertion_nonces (
  jti_hash varchar(64) PRIMARY KEY,
  expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_connector_assertion_nonces_expires_at ON connector_assertion_nonces (expires_at);
