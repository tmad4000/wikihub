# wikihub.md backup & restore runbook

Tracks ticket [wikihub-alfy](https://github.com/tmad4000/wikihub/issues). The
nightly backup ships three artifacts (postgres dump + bare-repo tarball + .env)
to GCS, where they age into cheaper storage and are deleted after a year.
Untested backups don't exist, so this doc also includes the exact restore
sequence.

## What is backed up

Every night at 03:00 UTC the production Lightsail box runs
`/opt/wikihub-app/scripts/backup.sh`. That script writes four files into
`gs://wikihub-backups-932822f5/daily/YYYY/MM/DD/`:

| File | Source | Why |
|---|---|---|
| `db-<TS>.dump` | `pg_dump --format=custom wikihub` | users, pages metadata, ACLs, API key hashes, audit log, private page bodies |
| `repos-<TS>.tar.gz` | `tar -czf … /opt/wikihub-app/repos` | authoritative bare git repos for every wiki (public + public-mirror) |
| `env-<TS>.txt` | `/opt/wikihub-app/.env` | `SECRET_KEY`, `DATABASE_URL`, OAuth client secrets, `ADMIN_TOKEN`, mail creds |
| `manifest-<TS>.txt` | sha256 of the above three | restore-time integrity check |

Postgres custom format is compressed and supports `pg_restore` parallelism.
The repos tar is gzipped because bare repos contain pre-packed `*.pack` files,
so the marginal compression on the rest of the tree is worth the seconds.

**Not backed up:** OS-level config (nginx site, systemd unit), Cloudflare zone,
listhub, MCP server (stateless), the dev environment. Out of scope per the
ticket. nginx/systemd live in this repo and can be replayed from `git pull`.

## Where backups live

- **Bucket:** `gs://wikihub-backups-932822f5` (region `us-east1`, uniform
  bucket-level access)
- **GCP project:** `boreal-conquest-464203-v2`
- **Service account:** `wikihub-backup-writer@boreal-conquest-464203-v2.iam.gserviceaccount.com`
  with `roles/storage.objectAdmin` scoped to this bucket only
- **SA key on the box:** `/etc/wikihub/gcs-key.json` (mode 600, owned by root)

### Encryption choice

Google-managed encryption (default). The bucket is private, the SA key is
mode-600 root-only on the Lightsail box, and the only thing in the bucket
that's actually sensitive is the `.env` snapshot — and an attacker who got
that key already had root on the box. Adding a passphrase + KMS would mean
managing a separate secret to enable disaster recovery, which makes the
recovery path more fragile in exchange for a marginal threat-model gain.
Revisit if/when wikihub holds something that materially raises the bar
(payment data, etc.).

### Retention

Lifecycle rules on the bucket (see `gcloud storage buckets describe gs://wikihub-backups-932822f5 --format=json | jq .lifecycle`):

| Age | Action |
|---|---|
| 0–29 days | STANDARD storage class |
| 30 days | transition to NEARLINE |
| 90 days | transition to COLDLINE |
| 365 days | delete |

Per-day backup is currently ~156 MiB (DB 11 MiB, repos 145 MiB, env+manifest
~1 KiB). Steady state with one daily backup:

- 30 × 156 MiB STANDARD ≈ 4.6 GiB
- 60 × 156 MiB NEARLINE ≈ 9.1 GiB
- 275 × 156 MiB COLDLINE ≈ 41.7 GiB

At us-east1 list prices that's roughly **$0.30–0.50/month** total. Egress on
restore is the only meaningful cost driver and we don't restore often.

## Schedule & monitoring

- **Timer:** `/etc/systemd/system/wikihub-backup.timer` — `OnCalendar=*-*-* 03:00:00 UTC`,
  `Persistent=true`, `RandomizedDelaySec=2min`.
- **Service:** `/etc/systemd/system/wikihub-backup.service`. Runs as root.
- **On failure:** `OnFailure=wikihub-backup-alert.service` writes
  `/var/log/wikihub-backup.FAIL` and emits a `logger`-tagged syslog line. The
  backup script also writes the same flag from its bash `ERR` trap, so a
  silent failure in the script itself still surfaces.
- **Log:** `/var/log/wikihub-backup.log` (one file, append-only, rotated by
  the system's default logrotate).

### Daily checks (the operator habit)

```bash
# Has any backup failed since last success?
ssh ubuntu@54.145.123.7 'ls -la /var/log/wikihub-backup.FAIL 2>/dev/null && echo "FAILED — investigate"'

# When did the last backup actually run?
ssh ubuntu@54.145.123.7 'sudo systemctl status wikihub-backup.timer wikihub-backup.service --no-pager'

# Last 30 lines of the log
ssh ubuntu@54.145.123.7 'sudo tail -30 /var/log/wikihub-backup.log'

# What's actually in the bucket today?
gcloud storage ls --long --readable-sizes gs://wikihub-backups-932822f5/daily/$(date -u +%Y/%m/%d)/
```

When the FAIL flag is present: read `/var/log/wikihub-backup.log`, fix the
underlying cause, then `sudo rm /var/log/wikihub-backup.FAIL` and run the
backup by hand: `sudo systemctl start wikihub-backup.service`.

### Verified failure modes

The failure path was deliberately exercised on 2026-04-28 by pointing the
script at a nonexistent bucket. Result: script exited 1, FAIL flag was
created, `wikihub-backup-alert.service` ran, and a `wikihub-backup BACKUP
FAILED` line landed in syslog (visible via
`journalctl -u wikihub-backup-alert.service`). That's the contract — if you
break the alert chain, prove it works again before declaring done.

## Restore

The companion script `scripts/restore.sh` downloads the artifacts and prints
the next-step commands. It does **not** auto-restore, on purpose — you should
always restore to a scratch DB first.

### Quickstart: pull yesterday's backup down for inspection

```bash
ssh ubuntu@54.145.123.7
sudo /opt/wikihub-app/scripts/restore.sh latest
# or:
sudo /opt/wikihub-app/scripts/restore.sh 2026-04-28
```

The script downloads `db-…dump`, `repos-…tar.gz`, `env-…txt`, and
`manifest-…txt` to `/tmp/wikihub-restore-<stamp>/`, runs `sha256sum -c`
against the manifest, and prints the next commands.

### Full restore — fresh box, total disaster

This is the recovery you walk through once a year as a drill. Estimated wall
time on a clean Ubuntu 24.04 Lightsail instance: ~30 minutes. RPO ≤ 24 h
(yesterday's 03:00 UTC), RTO ≈ 30–60 min.

1. **Stand the box back up.** Provision Ubuntu 24.04, install postgres-16,
   nginx, python3.12-venv, git. Clone wikihub: `git clone https://github.com/tmad4000/wikihub /opt/wikihub-app`.
2. **Drop the SA key into place.** Recreate
   `/etc/wikihub/gcs-key.json` from 1Password (or generate a fresh key for
   `wikihub-backup-writer@…` via `gcloud iam service-accounts keys create`).
   Mode 600, owned by root.
3. **Install gcloud:** follow `https://cloud.google.com/sdk/docs/install#deb`.
4. **Pull the backup:**
   ```bash
   sudo /opt/wikihub-app/scripts/restore.sh latest
   SCRATCH=$(sudo ls -td /tmp/wikihub-restore-* | head -1)
   ```
5. **Restore postgres:**
   ```bash
   sudo -u postgres createdb wikihub
   sudo -u postgres pg_restore --no-owner --no-acl -d wikihub "$SCRATCH"/db-*.dump
   sudo -u postgres psql -d wikihub -c "SELECT count(*) FROM users;"
   ```
6. **Restore the repos:**
   ```bash
   sudo systemctl stop wikihub 2>/dev/null || true
   sudo mkdir -p /opt/wikihub-app/repos
   sudo tar -xzf "$SCRATCH"/repos-*.tar.gz -C /opt/wikihub-app/repos
   sudo chown -R ubuntu:ubuntu /opt/wikihub-app/repos
   ```
7. **Restore .env:**
   ```bash
   sudo cp "$SCRATCH"/env-*.txt /opt/wikihub-app/.env
   sudo chown ubuntu:ubuntu /opt/wikihub-app/.env
   sudo chmod 600 /opt/wikihub-app/.env
   ```
8. **Reinstall the post-receive hook into every bare repo.** This is the
   step easiest to forget — the hook lives at `/opt/wikihub-app/hooks/post-receive`
   in the repo and gets symlinked into each `*.git/hooks/post-receive` at wiki
   creation time. After a tar restore, redo this:
   ```bash
   for r in $(find /opt/wikihub-app/repos -maxdepth 3 -type d -name '*.git'); do
     sudo ln -sf /opt/wikihub-app/hooks/post-receive "$r/hooks/post-receive"
     sudo chmod +x "$r/hooks/post-receive"
   done
   ```
9. **Boot the app, sanity-check, hand back DNS:**
   ```bash
   sudo systemctl start wikihub
   curl -s -o /dev/null -w '%{http_code}\n' http://localhost:5100/
   ```

### What if the .env is lost

The .env contains:

- `SECRET_KEY` — losing it invalidates all existing sessions but is otherwise
  not catastrophic. Generate a new one (`python3 -c "import secrets; print(secrets.token_hex(32))"`),
  paste into a new `.env`, restart wikihub. Users will need to re-login.
- `DATABASE_URL` — reconstructible from local postgres setup.
- `ADMIN_TOKEN` — generate a new one and update wherever it's referenced.
- OAuth client secrets — these are issued by Google. Without them, OAuth
  login is broken until you regenerate the client in the GCP console for the
  wikihub OAuth app and paste the new secret in.
- Mail credentials — recoverable from the mail provider's dashboard.

Everything in .env is recoverable; nothing is unique-and-only-here. So a
total .env loss is annoying but not data-destroying. The bigger risk is the
`SECRET_KEY` change kicking everyone out at once — communicate before doing
it deliberately.

### Restore drill — last verified 2026-04-28

Procedure: ran `restore.sh latest` on the prod box, verified manifest, restored
into scratch DB `wikihub_restore_test`, queried row counts (52 users, 6,070
pages, 133 wikis — matched live), extracted repos to `/tmp/wikihub-repo-drill`,
ran `git -C tejas/cortex-runbooks.git log --oneline | head` and saw the live
commit history. Cleaned up after.

Repeat this drill quarterly. If it stops working, that's a P1 bug, not a doc
nit.

## Rotating the GCS service account key

Every ~12 months, or sooner if the key is suspected leaked.

```bash
# On the Mac Mini (or anywhere you have gcloud auth):
gcloud config set project boreal-conquest-464203-v2

# Generate a new key
gcloud iam service-accounts keys create /tmp/wikihub-gcs-key-new.json \
  --iam-account=wikihub-backup-writer@boreal-conquest-464203-v2.iam.gserviceaccount.com

# Ship it
scp /tmp/wikihub-gcs-key-new.json ubuntu@54.145.123.7:/tmp/
ssh ubuntu@54.145.123.7 'sudo mv /tmp/wikihub-gcs-key-new.json /etc/wikihub/gcs-key.json && sudo chown root:root /etc/wikihub/gcs-key.json && sudo chmod 600 /etc/wikihub/gcs-key.json'

# Run a backup by hand to confirm the new key works
ssh ubuntu@54.145.123.7 'sudo systemctl start wikihub-backup.service'
ssh ubuntu@54.145.123.7 'sudo journalctl -u wikihub-backup.service --no-pager -n 30'

# Once confirmed, list and delete the OLD key
gcloud iam service-accounts keys list \
  --iam-account=wikihub-backup-writer@boreal-conquest-464203-v2.iam.gserviceaccount.com
gcloud iam service-accounts keys delete <OLD_KEY_ID> \
  --iam-account=wikihub-backup-writer@boreal-conquest-464203-v2.iam.gserviceaccount.com

# Wipe the local copy
rm /tmp/wikihub-gcs-key-new.json
```

## RPO and RTO

- **RPO (recovery point objective)**: ≤ 24 hours. Backups run at 03:00 UTC; in
  the worst case you'd lose 23 h 59 m of writes. If that's ever unacceptable,
  add an hourly DB-only backup (cheap because the DB is 11 MiB).
- **RTO (recovery time objective)**: ≤ 1 hour from a fresh box, assuming you
  have shell access to GCP and 1Password. Most of the wall time is provisioning
  and pulling the 145 MiB repos tar. The actual restore commands take <5 min.

## Files in this repo

- `scripts/backup.sh` — the nightly script
- `scripts/restore.sh` — companion download + verify script
- `scripts/wikihub-backup.service` — systemd oneshot
- `scripts/wikihub-backup.timer` — systemd timer (03:00 UTC daily)
- `scripts/wikihub-backup-alert.service` — `OnFailure=` handler
- `docs/backup-and-restore.md` — this doc
