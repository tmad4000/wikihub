# deployment guide

## prerequisites

- `gcloud` access to project `wikihub-prod`
- SSH access to instance `wikihub-prod` in `us-east1-b`; add
  `--tunnel-through-iap` when direct port 22 access is unavailable

## the deploy process

### 1. test locally first

```bash
source .venv/bin/activate && python3 tests/test_e2e.py
```

all e2e tests must pass. do not deploy with failing tests. For parallel or
isolated runs, set `DATABASE_URL` and `REPOS_DIR` before invoking the test
harness.

For worker-runtime changes, also run the real HTTP regression described in
[production workers](#production-workers). For directory/discovery changes,
run the directory scaling regression described there. CI runs both against
PostgreSQL 16.

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

Preserve the live footer customization and untracked operational files when
fast-forwarding the release checkout. For worker-runtime changes, apply the
[production worker configuration](#production-workers) before restarting;
pulling the source alone does not update systemd's command.

```bash
gcloud compute ssh wikihub-prod --project=wikihub-prod --zone=us-east1-b \
  --command='cd /opt/wikihub-app && sudo git pull && sudo systemctl restart wikihub'
```

### 4. verify the deploy worked

```bash
# check it's not 502
curl -s -o /dev/null -w "%{http_code}" https://wikihub.md/

# if 502, check logs immediately
gcloud compute ssh wikihub-prod --project=wikihub-prod --zone=us-east1-b \
  --command='sudo journalctl -u wikihub --no-pager -n 30'
```

common 502 causes:
- **ImportError** — forgot to commit a file. fix: commit the file, push, pull, restart.
- **missing DB extension** — e.g. `pg_trgm`. fix on the instance with
  `sudo -u postgres psql -d wikihub -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"`.
- **missing DB table** — app calls `db.create_all()` on startup, but if the import fails it never gets there.

### 5. smoke test on production

after confirming the site is up (200), test the specific things you changed:
- if you changed a route, hit it with curl
- if you changed UI, open it in agent-browser against `https://wikihub.md`
- if you changed search, `curl https://wikihub.md/api/v1/search?q=test`

## server details

| what | where |
|---|---|
| code | `/opt/wikihub-app` |
| venv | `/opt/wikihub-app/.venv` |
| env vars | `/opt/wikihub-app/.env` |
| systemd unit | `wikihub.service` |
| process | gunicorn on port 5100 |
| reverse proxy | nginx → gunicorn, Cloudflare in front (SSL) |
| database | PostgreSQL 16 database `wikihub`, local to the instance |
| git repos | `/opt/wikihub-app/repos/` |

## production workers

The versioned [Gunicorn configuration](../deploy/gunicorn.conf.py) owns the
worker topology, bind address and timeout. Each worker must initialize the
application and its PostgreSQL pool after fork: application startup opens live
database connections that must never be inherited through preloading. Request
threads keep capacity available when readers are slow.

The systemd `ExecStart` must load that configuration:

```text
/opt/wikihub-app/.venv/bin/gunicorn -c /opt/wikihub-app/deploy/gunicorn.conf.py wsgi:app
```

Keep the existing service user, working directory and environment file. Remove
old command-line worker/preload flags because they override the configuration.
Use a systemd drop-in with an empty `ExecStart=` followed by the command above;
back up the current unit/drop-ins before applying, then daemon-reload/restart.
Application startup runs the existing idempotent schema/bootstrap operations;
complete initial setup of an empty database once before starting multiple
workers.

[The runtime regression](../tests/test_gunicorn_runtime.py) launches actual
Gunicorn workers using the versioned configuration. It checks independent
worker initialization and PostgreSQL backends, concurrent public and
authenticated private reads, denied anonymous private reads, and request
capacity while readers are held open. It fails under the previous two-worker
preload configuration.

[The directory scaling regression](../tests/test_directory_scaling.py) seeds
120 accounts and bounds directory SELECT queries for anonymous and owner
views. It checks private owner visibility, public-only discovery for other
accounts, `index.md` precedence over `README.md`, directory counts and limit
behavior, and anonymous `/explore` rendering without private excerpts.

Run these regressions with the existing project dependencies and a disposable
PostgreSQL server. `WORKER_TEST_DATABASE_URL` must identify an existing database on that
server, with a user allowed to create databases. Each regression creates and
drops its own uniquely named database; never point either at production.

```bash
WORKER_TEST_DATABASE_URL=postgresql://localhost/postgres \
  .venv/bin/python tests/test_gunicorn_runtime.py
WORKER_TEST_DATABASE_URL=postgresql://localhost/postgres \
  .venv/bin/python tests/test_directory_scaling.py
```

[Runtime CI](../.github/workflows/runtime.yml) supplies the disposable database
and installs the dependencies from `requirements.txt`.

After deployment, verify public `/`, `/explore`, `/auth/login` and a known
public wiki reader URL; each must return HTTP 200 for an anonymous request.
Inspect worker/database errors and concurrent request latencies. For rollback,
restore the saved service drop-in and reload/restart; the worker configuration
does not alter application data. The old configuration has known pool
inheritance and saturation defects, so prefer correcting a failed rollout.

## useful commands

```bash
# logs (follow)
gcloud compute ssh wikihub-prod --project=wikihub-prod --zone=us-east1-b \
  --command='sudo journalctl -u wikihub -f'

# restart without pulling
gcloud compute ssh wikihub-prod --project=wikihub-prod --zone=us-east1-b \
  --command='sudo systemctl restart wikihub'

# query production DB
gcloud compute ssh wikihub-prod --project=wikihub-prod --zone=us-east1-b \
  --command='sudo -u postgres psql -d wikihub'

# run a flask CLI command on server
gcloud compute ssh wikihub-prod --project=wikihub-prod --zone=us-east1-b \
  --command='cd /opt/wikihub-app && source .venv/bin/activate && source .env && flask --app wsgi.py wikihub reindex --all'
```

## custom-domain cutover

The owner-facing settings/API flow proves hostname ownership but does not
provision DNS or TLS. After the domain reaches `verified`:

1. Point the hostname at the `dns_target` returned by the custom-domain API
   (configured with `CUSTOM_DOMAIN_TARGET`, default `domains.wikihub.md`).
2. Provision HTTPS at the edge and origin as required by the hostname's DNS
   provider. Do not activate a hostname that still has certificate errors.
3. Verify the public URL and a representative deep link over HTTPS.
4. Activate it on the production instance:

   ```bash
   gcloud compute ssh wikihub-prod --project=wikihub-prod --zone=us-east1-b \
     --command='cd /opt/wikihub-app && source .venv/bin/activate && source .env && flask --app wsgi.py wikihub activate-custom-domain docs.example.org'
   ```

5. Confirm that an anonymous apex URL redirects to the custom hostname, a
   signed-in request remains on `*.wikihub.md`, and the custom hostname serves
   the clean page URL. Activating a replacement demotes the previous active
   hostname for that wiki.

The activation command records a Git-backed audit event and refuses domains
that have not passed ownership verification. Its active TLS status is an
operator assertion, so the HTTPS check above is required before activation.

## DB migrations

There is no Alembic. `db.create_all()` creates missing tables on app startup but
does not alter existing tables or constraints. Put production DDL in an
idempotent, dated `migrations/*.sql` file and apply that checked-in file:

```bash
gcloud compute scp migrations/FILE.sql wikihub-prod:/tmp/ \
  --project=wikihub-prod --zone=us-east1-b
gcloud compute ssh wikihub-prod --project=wikihub-prod --zone=us-east1-b \
  --command='sudo -u postgres psql -d wikihub -f /tmp/FILE.sql'
```

Each migration header must state why it exists and include the exact production
apply command. Extensions such as `pg_trgm` are created by `app/__init__.py` on
startup when the DB user has permission; otherwise create them manually as the
PostgreSQL superuser.

When a migration creates tables as the PostgreSQL superuser, it must transfer
the new table and any owned sequences to the application role. Derive that role
from an existing application table such as `public.wikis`; do not hard-code a
deployment-specific username.
