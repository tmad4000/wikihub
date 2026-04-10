# deployment guide

## prerequisites

- SSH access to the server via `ssh -i ~/.ssh/wikihub-dev-key ubuntu@54.145.123.7`
- or via alias `ssh wikihub-dev` if `~/.ssh/config` is set up:
  ```
  Host wikihub-dev
      HostName 54.145.123.7
      User ubuntu
      IdentityFile ~/.ssh/wikihub-dev-key
  ```

## the deploy process

### 1. test locally first

```bash
source .venv/bin/activate && python3 tests/test_e2e.py
```

all 12 e2e tests must pass. do not deploy with failing tests.

### 2. commit everything that changed

**check for unstaged files.** the most common deploy failure is forgetting to stage a file. if you changed `models.py` AND `renderer.py` AND `wiki.py`, all three must be committed. one missing file = import error = 502 on production.

```bash
git status          # look at EVERY modified file
git diff            # review what changed
git add <files>     # add specific files, not git add .
git commit
git push origin main
```

### 3. deploy

```bash
ssh -i ~/.ssh/wikihub-dev-key ubuntu@54.145.123.7 \
  "cd /opt/wikihub-app && git pull && sudo systemctl restart wikihub"
```

### 4. verify the deploy worked

```bash
# check it's not 502
curl -s -o /dev/null -w "%{http_code}" https://wikihub.globalbr.ai/

# if 502, check logs immediately
ssh -i ~/.ssh/wikihub-dev-key ubuntu@54.145.123.7 \
  "sudo journalctl -u wikihub --no-pager -n 30"
```

common 502 causes:
- **ImportError** — forgot to commit a file. fix: commit the file, push, pull, restart.
- **missing DB extension** — e.g. `pg_trgm`. fix: `psql postgresql://wikihub:wikihub_dev_2026@localhost/wikihub -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"`
- **missing DB table** — app calls `db.create_all()` on startup, but if the import fails it never gets there.

### 5. smoke test on production

after confirming the site is up (200), test the specific things you changed:
- if you changed a route, hit it with curl
- if you changed UI, open it in agent-browser against `https://wikihub.globalbr.ai`
- if you changed search, `curl https://wikihub.globalbr.ai/api/v1/search?q=test`

## server details

| what | where |
|---|---|
| code | `/opt/wikihub-app` |
| venv | `/opt/wikihub-app/.venv` |
| env vars | `/opt/wikihub-app/.env` |
| systemd unit | `wikihub.service` |
| process | gunicorn on port 5100 |
| reverse proxy | nginx → gunicorn, Cloudflare in front (SSL) |
| database | `postgresql://wikihub:wikihub_dev_2026@localhost/wikihub` |
| git repos | `/opt/wikihub-app/repos/` |

## useful commands

```bash
# logs (follow)
ssh -i ~/.ssh/wikihub-dev-key ubuntu@54.145.123.7 "sudo journalctl -u wikihub -f"

# restart without pulling
ssh -i ~/.ssh/wikihub-dev-key ubuntu@54.145.123.7 "sudo systemctl restart wikihub"

# query production DB
ssh -i ~/.ssh/wikihub-dev-key ubuntu@54.145.123.7 \
  "psql postgresql://wikihub:wikihub_dev_2026@localhost/wikihub"

# run a flask CLI command on server
ssh -i ~/.ssh/wikihub-dev-key ubuntu@54.145.123.7 \
  "cd /opt/wikihub-app && source .venv/bin/activate && source .env && flask --app wsgi.py wikihub reindex --all"
```

## DB migrations

there is no alembic. schema changes happen via `db.create_all()` which runs on app startup. this creates new tables but does NOT drop columns or tables. if you need to add a column to an existing table, run ALTER TABLE manually on the server:

```bash
ssh -i ~/.ssh/wikihub-dev-key ubuntu@54.145.123.7 \
  "psql postgresql://wikihub:wikihub_dev_2026@localhost/wikihub -c 'ALTER TABLE pages ADD COLUMN new_col TEXT;'"
```

extensions (like `pg_trgm`) are auto-created by `app/__init__.py` on startup, but only if the DB user has permission. if it fails, create manually via psql.
