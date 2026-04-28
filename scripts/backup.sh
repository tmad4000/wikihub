#!/usr/bin/env bash
# Nightly wikihub backup.
#
# Captures three artifacts and uploads them to GCS:
#   1. postgres custom-format dump of the `wikihub` DB
#   2. tarball of /opt/wikihub-app/repos (bare git repos — authoritative content)
#   3. /opt/wikihub-app/.env (secrets — restricted bucket only)
# Plus a sha256 manifest of all three.
#
# Run as root via systemd timer (wikihub-backup.timer). Logs to
# /var/log/wikihub-backup.log. On any failure, exits non-zero, drops a sentinel
# file at /var/log/wikihub-backup.FAIL, and systemd's OnFailure= unit runs
# wikihub-backup-alert.service to surface the failure.
#
# Restore companion: scripts/restore.sh
# Doc: docs/backup-and-restore.md

set -euo pipefail

BUCKET="${WIKIHUB_BACKUP_BUCKET:-wikihub-backups-932822f5}"
GCS_KEY="${WIKIHUB_GCS_KEY:-/etc/wikihub/gcs-key.json}"
APP_DIR="${WIKIHUB_APP_DIR:-/opt/wikihub-app}"
LOG_FILE="/var/log/wikihub-backup.log"
FAIL_FLAG="/var/log/wikihub-backup.FAIL"
SCRATCH="$(mktemp -d /tmp/wikihub-backup.XXXXXX)"
TS="$(date -u +%Y%m%d-%H%M%S)"
DATE_PREFIX="$(date -u +%Y/%m/%d)"

trap 'rm -rf "$SCRATCH"' EXIT

log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a "$LOG_FILE"; }

on_error() {
  local rc=$?
  log "FAILED with exit $rc at line ${BASH_LINENO[0]}"
  date -u +%FT%TZ > "$FAIL_FLAG"
  exit $rc
}
trap on_error ERR

log "===== backup start ts=$TS bucket=$BUCKET ====="
START=$(date +%s)

# Activate the GCS service account credentials (idempotent).
gcloud auth activate-service-account --key-file="$GCS_KEY" --quiet >> "$LOG_FILE" 2>&1

# 1. Postgres dump (custom format = compressed + parallel-restorable).
DB_FILE="$SCRATCH/db-$TS.dump"
log "pg_dump wikihub -> $DB_FILE"
sudo -u postgres pg_dump --format=custom --compress=6 wikihub > "$DB_FILE"
DB_SIZE=$(stat -c%s "$DB_FILE")
log "  db dump size: $DB_SIZE bytes"

# 2. Tarball of the bare repos.
REPOS_FILE="$SCRATCH/repos-$TS.tar.gz"
log "tar repos -> $REPOS_FILE"
tar -czf "$REPOS_FILE" -C "$APP_DIR/repos" .
REPOS_SIZE=$(stat -c%s "$REPOS_FILE")
log "  repos tar size: $REPOS_SIZE bytes"

# 3. .env snapshot.
ENV_FILE="$SCRATCH/env-$TS.txt"
cp "$APP_DIR/.env" "$ENV_FILE"
chmod 600 "$ENV_FILE"
ENV_SIZE=$(stat -c%s "$ENV_FILE")
log "  env size: $ENV_SIZE bytes"

# Manifest with sha256 of all three.
MANIFEST="$SCRATCH/manifest-$TS.txt"
{
  echo "wikihub backup manifest"
  echo "timestamp: $TS"
  echo "host: $(hostname)"
  echo
  ( cd "$SCRATCH" && sha256sum "db-$TS.dump" "repos-$TS.tar.gz" "env-$TS.txt" )
} > "$MANIFEST"
log "manifest:"
sed 's/^/  /' "$MANIFEST" | tee -a "$LOG_FILE"

# Upload all four to GCS under daily/YYYY/MM/DD/.
GCS_PREFIX="gs://$BUCKET/daily/$DATE_PREFIX"
log "uploading to $GCS_PREFIX/"
gcloud storage cp "$DB_FILE" "$REPOS_FILE" "$ENV_FILE" "$MANIFEST" "$GCS_PREFIX/" --quiet >> "$LOG_FILE" 2>&1

END=$(date +%s)
DUR=$(( END - START ))
TOTAL=$(( DB_SIZE + REPOS_SIZE + ENV_SIZE ))
log "===== backup OK duration=${DUR}s total=${TOTAL}B ====="

# Clear any prior failure flag — most recent run succeeded.
rm -f "$FAIL_FLAG"
