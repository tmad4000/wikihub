# Curation rollout runbook (2026-11)

> **STATUS: NOT EXECUTED.** Nothing in this file has been run against production. The orchestrator's
> own dry-run attempt was refused by the permission classifier, so execution waits for Jacob, either
> in an interactive session or through an allow rule. Run every step as a dry run first; append
> `--execute` only to the commands listed in step 5.

- **Account list approved by Jacob,** 2026-11-09 ~19:20 MST (plan section 3d, item 4, with the
  orchestrator's narrowing about anon-92c215e9):

  | Action | Accounts |
  |---|---|
  | Suspend, **no purge** | user_b8aceefe, vccbiz7985, vccbiz-probe-80365, project-links-agent-20261009 |
  | Negative-curate, wiki scope, reason `promotional` | elongflywikihubxb, roxicc, nestack, seo, deepswapai, franchisenegosyo, productshotai, peptidetracker, visaforma, cellularport |
  | Leave alone | anon-92c215e9, and the nine empty accounts (wranker0, cabbazar23, cabbazar43, divebookingsau, adammueller777, ahmadgaber54, shamadevi8900, davaidlisa, carrystack) |

- **Sources:**
  - account details: the 2026-11-03 trust-and-safety review,
    `agent-worktrees/wikihub-scam-review-20261103/report.md`;
  - why these accounts pass the trust gate: `semantic-web-2026-11-09/w5-gate-diagnosis.md`;
  - how curation works: `docs/curation.md`.

## Shell setup (operator's machine)

```bash
# Service account per the release policy; port 22 is firewalled, so tunnel through IAP.
G="gcloud compute ssh wikihub-prod --project=wikihub-prod --zone=us-east1-b --tunnel-through-iap \
   --account=874749606899-compute@developer.gserviceaccount.com"
```

**Reading `/opt/wikihub-app/.env` on the host needs sudo.** The documented non-sudo form in
`docs/deploy.md` (`cd /opt/wikihub-app && source .venv/bin/activate && source .env && flask …`) fails
with `Permission denied`. The app also reads plain environment variables (there is no dotenv), so the
file must be exported while it is sourced. Every Flask command below therefore has this shape:

```bash
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub <ARGS>'"
```

The `flag` command reads each wiki's git sha with `git -c safe.directory=*`, so running it as root
does not trip git's ownership check.

## 0. Preconditions

- PR #53 (`feat/negative-curation`) is reviewed and merged, with CI green.
- Jacob's go to execute.
- Record what production runs now. On 2026-11-09 it reported `c20bd29`, a branch commit rather than a
  commit on main, so `git pull` is not a no-op and may need a checkout:

```bash
$G --command='cd /opt/wikihub-app && git rev-parse HEAD && git status --short | head && git branch --show-current && systemctl show wikihub -p User'
```

## 1. Backup

```bash
$G --command='sudo -u postgres pg_dump --format=custom wikihub | sudo tee /var/backups/wikihub-pre-curation-$(date +%Y%m%d%H%M).dump >/dev/null && ls -l /var/backups/wikihub-pre-curation-*.dump'
```

The nightly backup (`docs/backup-and-restore.md`) also runs at 03:00 UTC.

## 2. Migration (additive; creates `curation_judgements`)

```bash
gcloud compute scp migrations/2026-11-09_curation_judgements.sql wikihub-prod:/tmp/ \
  --project=wikihub-prod --zone=us-east1-b --tunnel-through-iap \
  --account=874749606899-compute@developer.gserviceaccount.com
$G --command='sudo -u postgres psql -v ON_ERROR_STOP=1 -d wikihub -f /tmp/2026-11-09_curation_judgements.sql'
$G --command="sudo -u postgres psql -d wikihub -c '\\d curation_judgements'"
```

**Apply the migration before restarting on the new code.** `create_app()` would otherwise create the
table on first boot through `db.create_all()`, outside the backed-up step and possibly in a race
between booting workers. Step 3 comes only after the `\d curation_judgements` check above succeeds.

## 3. Deploy

Only after step 2 has succeeded: follow the AGENTS.md deploy checklist (`git pull` and
`systemctl restart wikihub`), adjusted for the branch noted in step 0. Then check:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://wikihub.md/            # 200
curl -s -o /dev/null -w '%{http_code}\n' https://wikihub.md/curation    # 200
curl -s https://wikihub.md/api/v1/curation | head -c 300; echo         # {"authoritative":true,"judgements":[],...}
```

The suspensions in step 4a need only code that is already deployed (PR #45). They can run before
steps 1–3 if Jacob wants them first.

## 4. Dry runs (nothing changes)

### 4a. Suspend (no `--purge`; content and git history stay on disk)

```bash
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub suspend user_b8aceefe --reason \"Nov 3 T&S review: link farm (vccbusiness.com, ramerlabs.com); plan D4/3d\"'"
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub suspend vccbiz7985 --reason \"Nov 3 T&S review: same operator as user_b8aceefe; plan D4/3d\"'"
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub suspend vccbiz-probe-80365 --reason \"Nov 3 T&S review: same operator as user_b8aceefe; plan D4/3d\"'"
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub suspend project-links-agent-20261009 --reason \"Nov 3 T&S review: automated link placement; plan D4/3d\"'"
```

Each prints `[DRY RUN] suspend @… wikis=N pages=N api_keys=N` and `(no changes made; …)`.

### 4b. Negative-curate ten wikis (wiki scope, reason `promotional`; requires step 3)

The wiki is each account's promotional wiki, as named in the Nov 3 review. Evidence is **public**: it
is exported and visible to the owner.

```bash
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub flag @elongflywikihubxb/telegram-admin-wiki --polarity negative --reason promotional --evidence https://xiabao.org/ --note \"Nov 3 T&S review; plan D4/3d\"'"
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub flag @roxicc/proxy-client-guides --polarity negative --reason promotional --evidence https://roxi.cc/ --note \"Nov 3 T&S review; plan D4/3d\"'"
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub flag @nestack/nestack --polarity negative --reason promotional --evidence https://nestackagents.com/ --note \"Nov 3 T&S review; plan D4/3d\"'"
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub flag @seo/seo --polarity negative --reason promotional --evidence https://faceswapai.com/ --note \"Nov 3 T&S review; plan D4/3d\"'"
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub flag @deepswapai/deepswapai --polarity negative --reason promotional --evidence https://deepswapai.com/ --note \"Nov 3 T&S review; plan D4/3d\"'"
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub flag @franchisenegosyo/philippine-franchise-notes --polarity negative --reason promotional --evidence https://franchisenegosyo.com/ --note \"Nov 3 T&S review; plan D4/3d\"'"
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub flag @productshotai/ecommerce-product-photos --polarity negative --reason promotional --evidence https://productshotai.app/ --note \"Nov 3 T&S review; plan D4/3d\"'"
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub flag @peptidetracker/peptide-dosing-reference --polarity negative --reason promotional --evidence https://glp1.app/ --note \"Nov 3 T&S review; plan D4/3d\"'"
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub flag @visaforma/visaforma --polarity negative --reason promotional --evidence https://visaforma.com.ng/ --note \"Nov 3 T&S review; plan D4/3d\"'"
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub flag @cellularport/cellularport --polarity negative --reason promotional --evidence https://cellularport.com/ --note \"Nov 3 T&S review; plan D4/3d\"'"
```

Each prints `[DRY RUN] flag @user/wiki (wiki id=N) negative:promotional`, plus `policy_version` and
`affected_version`. An `unknown wiki` error means the slug changed since Nov 3: stop and re-check that
account rather than guessing. On the profile (`/@user`), the notice shows only where the judged wiki is
the personal wiki (`@user/user`), as for nestack, seo, deepswapai, visaforma and cellularport.

### 4c. Leave alone (no command)

anon-92c215e9, wranker0, cabbazar23, cabbazar43, divebookingsau, adammueller777, ahmadgaber54,
shamadevi8900, davaidlisa, carrystack. Re-check later with:

```bash
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub abuse-report --days 30'"
```

## 5. Execute: the same commands with `--execute`

```bash
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub suspend user_b8aceefe --reason \"Nov 3 T&S review: link farm (vccbusiness.com, ramerlabs.com); plan D4/3d\" --execute'"
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub suspend vccbiz7985 --reason \"Nov 3 T&S review: same operator as user_b8aceefe; plan D4/3d\" --execute'"
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub suspend vccbiz-probe-80365 --reason \"Nov 3 T&S review: same operator as user_b8aceefe; plan D4/3d\" --execute'"
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub suspend project-links-agent-20261009 --reason \"Nov 3 T&S review: automated link placement; plan D4/3d\" --execute'"

$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub flag @elongflywikihubxb/telegram-admin-wiki --polarity negative --reason promotional --evidence https://xiabao.org/ --note \"Nov 3 T&S review; plan D4/3d\" --execute'"
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub flag @roxicc/proxy-client-guides --polarity negative --reason promotional --evidence https://roxi.cc/ --note \"Nov 3 T&S review; plan D4/3d\" --execute'"
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub flag @nestack/nestack --polarity negative --reason promotional --evidence https://nestackagents.com/ --note \"Nov 3 T&S review; plan D4/3d\" --execute'"
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub flag @seo/seo --polarity negative --reason promotional --evidence https://faceswapai.com/ --note \"Nov 3 T&S review; plan D4/3d\" --execute'"
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub flag @deepswapai/deepswapai --polarity negative --reason promotional --evidence https://deepswapai.com/ --note \"Nov 3 T&S review; plan D4/3d\" --execute'"
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub flag @franchisenegosyo/philippine-franchise-notes --polarity negative --reason promotional --evidence https://franchisenegosyo.com/ --note \"Nov 3 T&S review; plan D4/3d\" --execute'"
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub flag @productshotai/ecommerce-product-photos --polarity negative --reason promotional --evidence https://productshotai.app/ --note \"Nov 3 T&S review; plan D4/3d\" --execute'"
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub flag @peptidetracker/peptide-dosing-reference --polarity negative --reason promotional --evidence https://glp1.app/ --note \"Nov 3 T&S review; plan D4/3d\" --execute'"
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub flag @visaforma/visaforma --polarity negative --reason promotional --evidence https://visaforma.com.ng/ --note \"Nov 3 T&S review; plan D4/3d\" --execute'"
$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub flag @cellularport/cellularport --polarity negative --reason promotional --evidence https://cellularport.com/ --note \"Nov 3 T&S review; plan D4/3d\" --execute'"

$G --command="sudo -n bash -c 'cd /opt/wikihub-app && set -a && . ./.env && set +a && .venv/bin/flask --app wsgi.py wikihub curation-list'"
```

## 6. Verify from the public site

```bash
UA='wikihub-curation-verify/1.0'
curl -s -A "$UA" https://wikihub.md/explore  > /tmp/explore.html
curl -s -A "$UA" https://wikihub.md/llms.txt > /tmp/llms.txt
for u in elongflywikihubxb roxicc nestack seo deepswapai franchisenegosyo productshotai peptidetracker visaforma cellularport; do
  printf '%-18s explore=%s llms=%s\n' "$u" "$(grep -c "@$u/" /tmp/explore.html)" "$(grep -c "@$u/" /tmp/llms.txt)"
done      # expect 0 / 0 for the judged wikis (other wikis of these accounts, if any, are untouched by wiki scope)
curl -s -A "$UA" -D - https://elongflywikihubxb.wikihub.md/telegram-admin-wiki/fake-verification-bots -o /tmp/p.html \
  | grep -i '^x-robots-tag'                                # x-robots-tag: noindex
grep -o '<meta name="wikihub:curation"[^>]*>' /tmp/p.html  # content="negative:promotional"
grep -o 'Curated against: promotional</strong>[^<]*' /tmp/p.html   # "... by WikiHub editors, for this wiki (@elongflywikihubxb/telegram-admin-wiki)."
grep -o 'rel="nofollow ugc[^"]*"' /tmp/p.html | head -1
for u in user_b8aceefe vccbiz7985 vccbiz-probe-80365 project-links-agent-20261009; do
  printf '%-30s %s\n' "$u" "$(curl -s -o /dev/null -w '%{http_code}' -A "$UA" https://wikihub.md/@$u)"
done      # 410 (plain "Account suspended" page, not a 404)
curl -s https://wikihub.md/api/v1/curation | python3 -m json.tool | head -40   # ten wiki judgements, no notes
```

## 7. Noos mirror (W1, separate owner)

Noos polls `GET https://wikihub.md/api/v1/curation?cursor=…` and upserts each row by `id` as a
`curation` annotation on `url`/`wiki_urls`, propagating status changes (retracted, superseded,
expired). WikiHub is the authoritative writer and has nothing to run for this.

## 8. Review cadence

Judgements get `review_at` 180 days out by default. List the ones that are due with the `curation-list
--due` form of the command, then retract, supersede or leave each one.

## Rollback

- **One judgement:** run `unflag @user/wiki --execute` in the same command shape. The row stays, with
  `status=retracted`, and the export propagates the retraction.
- **Correct a judgement:** `flag @user/wiki --reason <new> --supersedes <id> --execute`.
- **One suspension:** `unsuspend <user> --execute`. No content was purged, so everything comes back.
- **The whole feature:** redeploy the previous build. The table can stay, because old code ignores it.
  To drop it, export it first (see the rollback notes in
  `migrations/2026-11-09_curation_judgements.sql`). Restore from the step 1 dump only if something else
  went wrong.
