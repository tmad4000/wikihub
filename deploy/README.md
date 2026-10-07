# WikiHub production workers

The GCP service runs in `/opt/wikihub-app` on `wikihub-prod` in project
`wikihub-prod`, zone `us-east1-b`. Its systemd `ExecStart` must be:

```
/opt/wikihub-app/.venv/bin/gunicorn -c /opt/wikihub-app/deploy/gunicorn.conf.py wsgi:app
```

Keep the existing service user, working directory and environment file. Remove
old command-line worker/preload flags because they override the configuration.
Use a systemd drop-in with an empty `ExecStart=` followed by the command above;
back up the current unit/drop-ins before applying, then daemon-reload/restart.
Preserve the live footer customization and untracked operational files when
fast-forwarding the release checkout.

Two processes with four request threads each keep capacity available when a
reader is slow. Each worker initializes the application and its PostgreSQL pool
after fork. The previous `--preload` setting inherited live database connections
from application startup, causing connection failures across workers. Application
startup runs the existing idempotent schema/bootstrap operations; initial setup
of an empty database should be completed once before starting multiple workers.

`python tests/test_gunicorn_runtime.py` launches actual Gunicorn workers and
asserts worker-local initialization and a successful health response while two
requests are held open. It fails with the previous two-worker preload command.
CI runs the same test with the existing Gunicorn 23 version.

After deployment, verify public `/`, `/explore`, `/healthz`, `/auth/login` and a
public wiki reader. Inspect worker/database errors and concurrent request
latencies. For rollback, restore the saved service drop-in and reload/restart;
this change does not alter application data. The old configuration has known
pool inheritance and saturation defects, so prefer correcting a failed rollout.
