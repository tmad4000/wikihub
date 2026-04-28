#!/usr/bin/env bash
# WikiHub restore companion to backup.sh.
#
# Usage:
#   restore.sh YYYY-MM-DD              # download latest backup for that UTC date
#   restore.sh YYYY-MM-DD HHMMSS       # specific backup if multiple in a day
#   restore.sh latest                  # whatever is most recent
#
# Downloads the day's three artifacts to a scratch directory under
# /tmp/wikihub-restore-<date>-<ts>/, verifies the sha256 manifest, then PRINTS
# next-step commands the operator should run by hand. We deliberately do NOT
# auto-restore production — restores are a human decision.

set -euo pipefail

BUCKET="${WIKIHUB_BACKUP_BUCKET:-wikihub-backups-932822f5}"
GCS_KEY="${WIKIHUB_GCS_KEY:-/etc/wikihub/gcs-key.json}"

if [[ $# -lt 1 ]]; then
  echo "usage: $0 YYYY-MM-DD [HHMMSS]   (or 'latest')" >&2
  exit 64
fi

# Activate the SA (idempotent; only needed if running non-interactively).
if [[ -r "$GCS_KEY" ]]; then
  gcloud auth activate-service-account --key-file="$GCS_KEY" --quiet >/dev/null 2>&1 || true
fi

if [[ "$1" == "latest" ]]; then
  echo "Looking up most recent manifest in gs://$BUCKET/daily/ ..."
  LATEST=$(gcloud storage ls "gs://$BUCKET/daily/**/manifest-*.txt" 2>/dev/null | sort | tail -1)
  if [[ -z "$LATEST" ]]; then
    echo "ERROR: no backups found in gs://$BUCKET/daily/" >&2
    exit 2
  fi
  PREFIX="$(dirname "$LATEST")"
  STAMP="$(basename "$LATEST" .txt | sed 's/^manifest-//')"
  DATE_PART="${STAMP%-*}"
  TIME_PART="${STAMP##*-}"
else
  DATE_PART="$1"
  TIME_PART="${2:-}"
  YEAR="${DATE_PART:0:4}"
  MONTH="${DATE_PART:5:2}"
  DAY="${DATE_PART:8:2}"
  PREFIX="gs://$BUCKET/daily/$YEAR/$MONTH/$DAY"
  if [[ -z "$TIME_PART" ]]; then
    # Find the latest manifest for that day.
    M=$(gcloud storage ls "$PREFIX/manifest-*.txt" 2>/dev/null | sort | tail -1 || true)
    if [[ -z "$M" ]]; then
      echo "ERROR: no manifest under $PREFIX" >&2
      exit 2
    fi
    STAMP="$(basename "$M" .txt | sed 's/^manifest-//')"
    TIME_PART="${STAMP##*-}"
  fi
  STAMP="${DATE_PART//-/}-$TIME_PART"
fi

SCRATCH="/tmp/wikihub-restore-$STAMP"
mkdir -p "$SCRATCH"
echo "Downloading backup $STAMP -> $SCRATCH"

gcloud storage cp \
  "$PREFIX/db-$STAMP.dump" \
  "$PREFIX/repos-$STAMP.tar.gz" \
  "$PREFIX/env-$STAMP.txt" \
  "$PREFIX/manifest-$STAMP.txt" \
  "$SCRATCH/" --quiet

echo
echo "===== sha256 verification ====="
( cd "$SCRATCH" && sha256sum -c <(grep -E '^[0-9a-f]{64}  ' "manifest-$STAMP.txt") )

cat <<EOF

===== artifacts ready in $SCRATCH =====
  $(ls -lh "$SCRATCH" | tail -n +2)

===== NEXT STEPS (run by hand — this script does NOT touch production) =====

# 1. Postgres restore — use a SCRATCH database first to verify, never overwrite
#    production blindly. Drop a scratch DB and restore into it:
sudo -u postgres dropdb --if-exists wikihub_restore_test
sudo -u postgres createdb wikihub_restore_test
sudo -u postgres pg_restore --no-owner --no-acl -d wikihub_restore_test "$SCRATCH/db-$STAMP.dump"
sudo -u postgres psql -d wikihub_restore_test -c "SELECT count(*) FROM users;"
sudo -u postgres psql -d wikihub_restore_test -c "SELECT count(*) FROM pages;"

# 2. Repos restore — extract to a scratch dir, eyeball one wiki, then sync into place
mkdir -p /tmp/wikihub-repos-restored
tar -xzf "$SCRATCH/repos-$STAMP.tar.gz" -C /tmp/wikihub-repos-restored
ls /tmp/wikihub-repos-restored | head
# To replace production repos (only after stopping wikihub.service):
#   sudo systemctl stop wikihub
#   sudo mv /opt/wikihub-app/repos /opt/wikihub-app/repos.broken.\$(date +%s)
#   sudo mkdir /opt/wikihub-app/repos
#   sudo tar -xzf "$SCRATCH/repos-$STAMP.tar.gz" -C /opt/wikihub-app/repos
#   sudo chown -R ubuntu:ubuntu /opt/wikihub-app/repos
#   sudo systemctl start wikihub

# 3. .env restore — review BEFORE overwriting (contents may have rotated since)
cat "$SCRATCH/env-$STAMP.txt"
# If you really want to roll back:
#   sudo cp /opt/wikihub-app/.env /opt/wikihub-app/.env.bak
#   sudo cp "$SCRATCH/env-$STAMP.txt" /opt/wikihub-app/.env
#   sudo chown ubuntu:ubuntu /opt/wikihub-app/.env
#   sudo chmod 600 /opt/wikihub-app/.env
#   sudo systemctl restart wikihub

EOF
