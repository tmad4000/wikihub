---
name: connector-docs-import
description: >
  (Re-)import Jacob's connector-doc web — the recursive network of
  *.jacobcole.net Google Docs (systematicawesome / "Systematic Awesome"
  universe) — into WikiHub at @jacobcole/systematic-awesome, as faithfully
  (1:1) as possible, with a backup snapshot taken FIRST. Use when the user
  says "re-run the systematic awesome import", "refresh my connector docs on
  wikihub", "re-import the jacobcole.net docs", or "sync my Google Docs web
  into the wiki".
---

# Connector-docs → WikiHub import

Faithfully mirror the `*.jacobcole.net` Google-Docs web into
`@jacobcole/systematic-awesome`. **Goal: as 1:1 as possible.** Do not
restructure, rename, or summarize unless the user explicitly asks.

## Where everything lives (the reference Jacob asked for)

| Thing | Location |
|---|---|
| Source scrape (HTML, one file per doc) | `noos-prod:~/noos/backups/google-docs/html/*.html` (`ssh noos-prod`, AWS Lightsail `3.216.129.34`) |
| Scrape manifest (filename → source URL + docId) | `noos-prod:~/noos/backups/google-docs/export-results.json` (also `export-results-v2.json`, `dead-links-report.json`) |
| Last scrape date | recorded as `SCRAPE_DATE` in the import script (2026-01-30 as of writing) |
| Local working copy used by the importer | `/tmp/sa-scrape/` (`*.html` + `export-results.json`) |
| Scraper (refreshes from Google) | `scripts/scrape_gdocs.py` |
| Importer (HTML → markdown → WikiHub) | `scripts/import_systematicawesome.py` |
| Import API key | `~/.config/wikihub/jacobcole-import-key.txt` |
| Destination | wiki `@jacobcole/systematic-awesome` on `https://wikihub.md` |

## How the importer works (so "1:1" is understood)

`scripts/import_systematicawesome.py`:
- one wiki page per HTML file; filename stem → page path (`<stem>.md`)
- cleans HTML (drops `<style>`/`class=`, unwraps Google redirect URLs), then
  `pandoc -f html -t gfm` → markdown
- converts internal `*.jacobcole.net` links to `[[wikilinks]]` when the target
  exists in the scrape
- writes frontmatter: `title`, `source_url`, `source_gdoc`, `scraped_at`,
  `imported_by: wikihub-q7gz`, `visibility: public`
- **idempotent + safe:** updates its own prior imports in place (matches on
  `imported_by: wikihub-q7gz`); if a page exists that is NOT one of our
  imports, it writes to `<stem>.import.YYYY-MM-DD.md` and flags it for review —
  it never silently clobbers human edits.

Known fidelity gaps (call out to the user, don't silently accept):
- inline images (`data:` URIs) are omitted (size limit) — replaced with a note
- `pandoc` GFM normalizes some formatting
- WikiHub's Cloudflare WAF rejects page writes containing shell one-liners
  (`curl … | bash`, `npm install -g …`); such pages fail with an HTML 403.
  Workaround: write those via the origin directly (the GCP `wikihub-prod` box's
  local API on `127.0.0.1:5100`, bypassing Cloudflare) — see
  `docs/deploy.md` for SSH.

## Procedure

### 0. SNAPSHOT FIRST — never import without a fresh backup
A nightly backup already runs (`wikihub-backup.timer` → GCS bucket
`wikihub-backups-932822f5`). Before a big import, force an on-demand one:
```
gcloud compute ssh wikihub-prod --project=wikihub-prod --zone=us-east1-b \
  --tunnel-through-iap --command='sudo systemctl start wikihub-backup.service \
    && journalctl -u wikihub-backup.service --no-pager -n 5'
```
Rollback companion if needed: `scripts/restore.sh` (see `docs/backup-and-restore.md`).

### 1. (Optional) refresh the scrape from Google
Only if you want newer-than-last-scrape content. Needs Google auth; run on
noos-prod:
```
ssh noos-prod 'cd ~/noos && python3 <path>/scrape_gdocs.py'   # see scripts/scrape_gdocs.py
```
If you skip this, you re-import the existing 2026-01-30 scrape verbatim.

### 2. Sync the scrape locally
```
mkdir -p /tmp/sa-scrape
rsync -az noos-prod:~/noos/backups/google-docs/html/ /tmp/sa-scrape/
scp noos-prod:~/noos/backups/google-docs/export-results.json /tmp/sa-scrape/
ls /tmp/sa-scrape/*.html | wc -l   # sanity: expect ~70+ docs
```

### 3. Dry-run, then import
```
python3 scripts/import_systematicawesome.py --dry-run      # prints per-page plan
python3 scripts/import_systematicawesome.py                # ~5 req/s, idempotent
python3 scripts/import_systematicawesome.py --only=<stem>  # single page (good for testing)
```

### 4. Verify
- counts line at the end: `created / updated / skipped / flagged / errors`
- spot-check a few pages render and that `[[wikilinks]]` resolve
- review anything under `*.import.YYYY-MM-DD.md` (collisions with non-import pages)

## Testing the skill cheaply
Use `--only=<stem>` (e.g. `--only=thoughtfulweb`) for a single-page round-trip
before a full run. Combine with `--dry-run` first.
