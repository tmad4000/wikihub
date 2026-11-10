# Curation rollout runbook (2026-11)

> **STATUS: NOT EXECUTED.** Nothing in this file has been run against production. It was written
> with the code (`wikihub-f3b7`) so an operator can run it later, after the orchestrator and Jacob
> decide to deploy and approve the account actions. Every mutating command is shown dry run first.

Policy source: Noos semantic-web plan D4 (Jacob, 2026-11-09). Unless it is "super, super spam", do not
delete. Suspend the worst. Negative-curate promotional accounts. Leave empty accounts alone until they
publish. Account list: 2026-11-03 trust-and-safety review
(`agent-worktrees/wikihub-scam-review-20261103/report.md`). Gate diagnosis:
`semantic-web-2026-11-09/w5-gate-diagnosis.md`.

## Shell setup (operator's machine)

```bash
# Service account per the release policy; add --tunnel-through-iap because port 22 is firewalled.
G="gcloud compute ssh wikihub-prod --project=wikihub-prod --zone=us-east1-b --tunnel-through-iap \
   --account=874749606899-compute@developer.gserviceaccount.com"
# The app reads plain environment variables (no dotenv), so export .env while sourcing it.
FLASK='cd /opt/wikihub-app && . .venv/bin/activate && set -a && . ./.env && set +a && flask --app wsgi.py wikihub'
```

## 0. Preconditions

- The pull request for `feat/negative-curation` is reviewed and merged, with CI green.
- Jacob or the orchestrator has approved (a) the deploy, (b) the migration and (c) the account list
  below. Each is a separate decision.
- Record what production runs now. On 2026-11-09 it reported `c20bd29`, a branch commit rather than a
  commit on main, so `git pull` is not a no-op and may need a checkout:

```bash
$G --command='cd /opt/wikihub-app && git rev-parse HEAD && git status --short | head && git branch --show-current'
curl -s https://wikihub.md/api/v1 | head -c 400; echo
```

## 1. Backup

The nightly backup (`docs/backup-and-restore.md`) runs at 03:00 UTC. Take a fresh one anyway:

```bash
$G --command='sudo -u postgres pg_dump --format=custom wikihub | sudo tee /var/backups/wikihub-pre-curation-$(date +%Y%m%d%H%M).dump >/dev/null && ls -l /var/backups/wikihub-pre-curation-*.dump'
```

## 2. Migration (additive; creates `curation_judgements`)

```bash
gcloud compute scp migrations/2026-11-09_curation_judgements.sql wikihub-prod:/tmp/ \
  --project=wikihub-prod --zone=us-east1-b --tunnel-through-iap \
  --account=874749606899-compute@developer.gserviceaccount.com
$G --command='sudo -u postgres psql -v ON_ERROR_STOP=1 -d wikihub -f /tmp/2026-11-09_curation_judgements.sql'
$G --command="sudo -u postgres psql -d wikihub -c '\\d curation_judgements'"
```

`create_app()` would also create the table on first boot through `db.create_all()`. Running the file
first makes the DDL an explicit, backed-up step.

## 3. Deploy

Follow the AGENTS.md deploy checklist (`git pull` and `systemctl restart wikihub`), adjusted for the
branch noted in step 0. Then check:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://wikihub.md/                  # 200
curl -s -o /dev/null -w '%{http_code}\n' https://wikihub.md/curation          # 200
curl -s https://wikihub.md/api/v1/curation | head -c 300; echo               # {"judgements": [], ...}
```

## 4. Account actions: dry runs

Nothing changes until `--execute`. Review the output before step 5.

### 4a. Suspend (D4: the link-farm operator, the automated link placer, the empty junk account)

No `--purge`: content and git history stay on disk. Deleting needs its own decision.

```bash
for u in user_b8aceefe vccbiz7985 vccbiz-probe-80365 project-links-agent-20261009 anon-92c215e9; do
  $G --command="$FLASK suspend $u --reason 'Nov 3 T&S review: link farm / automated link placement (plan D4)'"
done
```

Optional: also record the reason as a judgement, so the Noos mirror carries the anti-signal for these
domains even though the URLs answer 410. Decide this together with 4a.

```bash
$G --command="$FLASK flag user_b8aceefe --reason affiliate_farm --note 'vccbusiness.com 1,103 links; linkpilot-ai.ramerlabs.com 1,456'"
$G --command="$FLASK flag vccbiz7985 --reason affiliate_farm --note 'same operator as user_b8aceefe'"
$G --command="$FLASK flag project-links-agent-20261009 --reason automated_link_placement --note 'automated RU link placer'"
```

### 4b. Negative-curate the ten promotional accounts (account scope, reason `promotional`)

| Account | Note (from the Nov 3 review) |
|---|---|
| elongflywikihubxb | xiabao.org Telegram-bot CTA |
| roxicc | roxi.cc VPN |
| nestack | nestackagents.com, nestack.com |
| seo | faceswapai.com |
| deepswapai | deepswapai.com (same operator as seo) |
| franchisenegosyo | franchisenegosyo.com |
| productshotai | productshotai.app |
| peptidetracker | glp1.app, GLP-1 dosing |
| visaforma | visaforma.com.ng |
| cellularport | cellularport.com profile spam |

```bash
$G --command="$FLASK flag elongflywikihubxb --polarity negative --reason promotional --note 'xiabao.org Telegram-bot CTA (Nov 3 review)'"
$G --command="$FLASK flag roxicc            --polarity negative --reason promotional --note 'roxi.cc VPN (Nov 3 review)'"
$G --command="$FLASK flag nestack           --polarity negative --reason promotional --note 'nestackagents.com (Nov 3 review)'"
$G --command="$FLASK flag seo               --polarity negative --reason promotional --note 'faceswapai.com (Nov 3 review)'"
$G --command="$FLASK flag deepswapai        --polarity negative --reason promotional --note 'deepswapai.com, same operator as seo (Nov 3 review)'"
$G --command="$FLASK flag franchisenegosyo  --polarity negative --reason promotional --note 'franchisenegosyo.com (Nov 3 review)'"
$G --command="$FLASK flag productshotai     --polarity negative --reason promotional --note 'productshotai.app (Nov 3 review)'"
$G --command="$FLASK flag peptidetracker    --polarity negative --reason promotional --note 'glp1.app GLP-1 dosing (Nov 3 review)'"
$G --command="$FLASK flag visaforma         --polarity negative --reason promotional --note 'visaforma.com.ng (Nov 3 review)'"
$G --command="$FLASK flag cellularport      --polarity negative --reason promotional --note 'cellularport.com profile spam (Nov 3 review)'"
```

### 4c. Leave alone (D4)

No action until they publish: wranker0, cabbazar23, cabbazar43, divebookingsau, adammueller777,
ahmadgaber54, shamadevi8900, davaidlisa, carrystack. Re-check with
`$G --command="$FLASK abuse-report --days 30"`.

## 5. Execute

Re-run exactly the approved commands from step 4 with `--execute` appended. For example:

```bash
$G --command="$FLASK flag seo --polarity negative --reason promotional --note 'faceswapai.com (Nov 3 review)' --execute"
$G --command="$FLASK suspend user_b8aceefe --reason 'Nov 3 T&S review: link farm (plan D4)' --execute"
$G --command="$FLASK curation-list"
```

## 6. Verify from the public site

Expected: the six formerly trusted promotional accounts are gone from discovery, and their pages show
the banner and the meta tag and are noindexed.

```bash
UA='wikihub-curation-verify/1.0'
curl -s -A "$UA" https://wikihub.md/explore  > /tmp/explore.html
curl -s -A "$UA" https://wikihub.md/llms.txt > /tmp/llms.txt
curl -s -A "$UA" 'https://wikihub.md/api/v1/wikis?limit=200' > /tmp/wikis.json
for u in seo deepswapai elongflywikihubxb productshotai visaforma cellularport roxicc nestack franchisenegosyo peptidetracker; do
  printf '%-18s explore=%s llms=%s api=%s\n' "$u" "$(grep -c "@$u/" /tmp/explore.html)" \
    "$(grep -c "@$u/" /tmp/llms.txt)" "$(grep -c "\"owner\": *\"$u\"" /tmp/wikis.json)"
done                                                       # expect 0 / 0 / 0 everywhere
curl -s -A "$UA" -D - https://elongflywikihubxb.wikihub.md/telegram-admin-wiki/fake-verification-bots -o /tmp/p.html \
  | grep -i '^x-robots-tag'                                # x-robots-tag: noindex
grep -o '<meta name="wikihub:curation"[^>]*>' /tmp/p.html  # content="negative:promotional"
grep -c 'Curated against: promotional' /tmp/p.html         # 1
grep -o 'rel="nofollow ugc[^"]*"' /tmp/p.html | head -1
curl -s -o /dev/null -w '%{http_code}\n' https://wikihub.md/@user_b8aceefe   # 410 once suspended
curl -s https://wikihub.md/api/v1/curation | python3 -m json.tool | head -40   # no notes in public output
```

## 7. Noos mirror (W1, separate owner)

Noos polls `GET https://wikihub.md/api/v1/curation?cursor=…` and stores each row as a `curation`
annotation on `url` and `wiki_urls` (plan D4 field names). WikiHub has nothing to run for this.

## Rollback

- One judgement: `$G --command="$FLASK unflag <target> --execute"`. The row stays, with `revoked_at` set.
- One suspension: `$G --command="$FLASK unsuspend <user> --execute"`.
- The whole feature: redeploy the previous build. The table can stay; old code ignores it. To drop it,
  export it first (see the rollback notes in `migrations/2026-11-09_curation_judgements.sql`), then
  restore from the step 1 dump only if something else went wrong.
