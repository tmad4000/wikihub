# A2: unlisted pages stay out of activity (wikihub-0t3y)

Builder scope: source, tests, review and merge only. **Production execution belongs
exclusively to orchestrator Paseo chat `4f013b60`.** No production DB read, write,
migration, restart or deployment was performed by Sol. This runbook complements
[the deployment guide](../docs/deploy.md) and the lessons in
[curation rollout](curation-rollout-2026-11.md). Do not rerun curation/suspension
commands from that historical rollout.

## Result and disposable rehearsal

Both per-wiki `/activity` and `/activity.rss` exclude `unlisted`, historical
`unlisted-view`, and `unlisted-edit`, including for owners and ACL grantees.
Related suggestions/comments are omitted when any current target is unlisted.
The HTML activity page's Recently updated panel follows the same rule. Public
entries remain visible; private entries retain their existing owner/grantee ACLs.
Direct-link reads, normal reader navigation and direct proposal access still work.
The global feeds already excluded unlisted content. No schema, dependency,
configuration, authentication or worker-topology change is required.

Run with the existing project dependencies and a disposable local PostgreSQL:

```bash
WORKER_TEST_DATABASE_URL=postgresql://localhost/postgres \
  .venv/bin/python scripts/check-activity-feeds.py
```

The runner refuses a non-loopback host or an admin DB other than `/postgres`,
creates a unique database, sets `DATABASE_URL`/`REPOS_DIR` itself, and drops only
that database on exit. Git repositories and the backup archive are created under
this checkout's `evidence/` and removed. On the M4 Sol used the existing venv
`/Users/jacob/code/wikihub/.venv/bin/python`; no dependencies were installed.

Observed after the fix:

```text
PASS all-viewer unlisted exclusion + direct access
PASS HTML private ACL + social events
PASS global activity visibility
PASS RSS XML + host links
PASS private-only RSS metadata denial
PASS older public entry after 305 hidden rows
PASS unlisted reader sidebar still works
PASS PostgreSQL custom backup archive readable
7 targeted checks plus backup archive check passed; disposable database only
```

The all-viewer regression was run first against the old code and failed with
`AssertionError: ('owner', 'Hidden Feed Marker')`. The matrix covers anonymous,
owner, member and ACL grantee; all unlisted aliases, public/public-edit and
private controls; single- and mixed-target suggestions/comments; direct access;
and a public-to-unlisted visibility change. Older public HTML/RSS rows survive a
newer 305-row mix of private/unlisted pages. The full suite runs in CI. A separate disposable Git rehearsal moved from the
old code to an exact release target, then back to a saved rollback branch; a
dirty live-footer customization survived both checkouts. Every Bash block below
was syntax-checked without executing production commands.

Visual review could not run here: browser inventory returned no browsers/apps,
with `Native apps: Error: Sky Computer Use service startup request failed`.
A browser check is reserved below for the orchestrator; HTTP/HTML/XML tests are
not claimed as visual evidence.

## Orchestrator: preflight and backup

Use Bash on the operator machine. Set the exact reviewed PR commit (or its tested
merge), not a moving `origin/main` target. Fill the two sample URLs from existing
content: a public wiki and an already-known unlisted page within it; do not create
production fixtures. No credentials are placed in command arguments.

```bash
set -euo pipefail
A2_TARGET=REPLACE_WITH_REVIEWED_FULL_COMMIT_SHA
A2_RUN=$(date -u +%Y%m%dT%H%M%SZ)
A2_WIKI=https://wikihub.md/@OWNER/WIKI
A2_UNLISTED=https://wikihub.md/@OWNER/WIKI/KNOWN_UNLISTED_PAGE
[[ $A2_TARGET =~ ^[0-9a-f]{40}$ ]]
whssh() {
  gcloud compute ssh wikihub-prod --project=wikihub-prod --zone=us-east1-b \
    --tunnel-through-iap --account=874749606899-compute@developer.gserviceaccount.com \
    --command "$1"
}
```

Record live HEAD/branch and review dirty paths and the exact release delta:

```bash
whssh 'cd /opt/wikihub-app && sudo git -c safe.directory=* rev-parse HEAD && sudo git -c safe.directory=* branch --show-current && sudo git -c safe.directory=* status --short && systemctl show wikihub -p User'
whssh "cd /opt/wikihub-app && sudo git -c safe.directory=* fetch origin && sudo git -c safe.directory=* show --no-patch --oneline '$A2_TARGET' && sudo git -c safe.directory=* log --oneline HEAD..'$A2_TARGET' && sudo git -c safe.directory=* log --oneline '$A2_TARGET'..HEAD && sudo git -c safe.directory=* diff --stat HEAD '$A2_TARGET'"
```

Preserve the live `_footer.html` customization and untracked operational files.
If the live line has unique commits, unrelated changes would ship, dirty files
would overlap, or another rollout is active, stop and resolve with the owner.
Do not reset, stash, force, or pull-merge through it. This A2 release must not
silently include a different session's unreviewed work.

Create a rollback branch at the exact live commit and a readable custom DB dump:

```bash
whssh "cd /opt/wikihub-app && sudo git -c safe.directory=* branch 'rollback/a2-$A2_RUN' HEAD"
whssh "sudo bash -o pipefail -c 'umask 077; sudo -u postgres pg_dump --format=custom wikihub > /var/backups/wikihub-a2-$A2_RUN.dump' && sudo test -s /var/backups/wikihub-a2-$A2_RUN.dump && sudo pg_restore --list /var/backups/wikihub-a2-$A2_RUN.dump >/dev/null"
```

Keep `$A2_RUN`, `$A2_TARGET`, the prior HEAD, backup pathname and rollback branch
in the release record. This patch has **no migration and creates no new table**.
Do not run migration, reindex, curation or ingestion commands for A2.

## Orchestrator: deploy and verify

After the audits and green-CI/independent-review checks, create a release branch
at the fixed target. Checkout refuses overlapping dirty files; do not defeat it.
It preserves the prior main branch, rollback branch and unrelated live footer.

```bash
A2_SINCE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
whssh "cd /opt/wikihub-app && sudo git -c safe.directory=* checkout -b 'release/a2-$A2_RUN' '$A2_TARGET' && sudo systemctl restart wikihub && sudo systemctl is-active --quiet wikihub && sudo git -c safe.directory=* rev-parse HEAD"
for url in https://wikihub.md/ https://wikihub.md/explore https://wikihub.md/auth/login https://wikihub.md/activity https://wikihub.md/activity.rss "$A2_WIKI/activity" "$A2_WIKI/activity.rss" "$A2_UNLISTED"; do
  code=$(curl -sS -L --max-time 30 -H 'Cache-Control: no-cache' -o /dev/null -w '%{http_code}' "$url")
  printf '%s %s\n' "$code" "$url"
  test "$code" = 200
done
curl -fsSL --max-time 30 "$A2_WIKI/activity.rss" | python3 -c 'import sys,xml.etree.ElementTree as E; root=E.fromstring(sys.stdin.buffer.read()); assert root.tag == "rss"; print("PASS RSS XML")'
whssh "sudo journalctl -u wikihub --since '$A2_SINCE' --no-pager -p err"
```

Expect HTTP 200s, valid RSS,
an active service, exact target SHA, and zero new errors. Also inspect recent
service logs for tracebacks/database/worker errors even if their priority was
not classified as `err`.

In a signed-out browser open the public wiki's Activity page and RSS. Confirm
public edits remain, and the known unlisted page's title/path is absent from
activity entries and the Activity page's Recently updated panel. Confirm the
unlisted direct URL remains readable. Repeat with the owner and an existing ACL
grantee: unlisted entries remain absent; their authorized private activity remains.
Do not change grants or publish fixtures just for this check. Check the tab names
Activity and the wiki, and that private-page previews stay neutral. Record these
live/UI outcomes separately; the builder's disposable results are not live proof.

## Orchestrator: rollback

If rollout verification fails, keep evidence and return to the saved code. Since
there are no data/schema changes, **do not restore the DB dump as routine rollback**
(that would discard intervening writes). Preserve dirty files as above:

```bash
whssh "cd /opt/wikihub-app && sudo git -c safe.directory=* checkout 'rollback/a2-$A2_RUN' && sudo systemctl restart wikihub && sudo systemctl is-active --quiet wikihub && sudo git -c safe.directory=* rev-parse HEAD"
```

Verify the prior SHA, all ordinary public 200s and zero new service errors. The old
code lists unlisted activity again; record that regression and prefer a reviewed
fix-forward once the service is stable. Keep the dump for disaster recovery under
[backup and restore](../docs/backup-and-restore.md); never delete pages to roll back.
