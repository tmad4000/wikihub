# wikihub

GitHub for LLM wikis. Flask + Postgres + bare git.

**When the user describes a feature request or new behavior, always create a beads ticket for it.** Beads is the living record of all product decisions — use `bd create` for new features/bugs, `bd show` for context, `bd close` when done. Do not maintain a separate spec file.

## running locally

```bash
source .venv/bin/activate
SECRET_KEY=dev DATABASE_URL=postgresql://localhost/wikihub REPOS_DIR=./repos ADMIN_TOKEN=devtoken flask --app wsgi.py run
```

postgres must be running (`brew services start postgresql@16`).

## testing

```bash
source .venv/bin/activate && python3 tests/test_e2e.py
```

tests use a separate `wikihub_test` database. create it once: `/opt/homebrew/opt/postgresql@16/bin/createdb wikihub_test`.

tests are minimal and intentional — each one verifies a real user flow end-to-end, not individual functions:

1. **agent account creation** — POST /api/v1/accounts, get key, authenticate
2. **wiki lifecycle** — create wiki, add page, read HTML + markdown, update, delete
3. **search** — full-text search via API
4. **social** — star, fork, unstar across two users
5. **zip upload** — create wiki via web form with zip file
6. **agent surfaces** — all discovery endpoints respond (llms.txt, AGENTS.md, .well-known/*)
7. **ACL permissions** — private pages not readable without auth

don't add unit tests for individual functions. if something breaks, add an e2e test that covers the broken flow. tests should run in <10 seconds.

## architecture

- `cli/` — `wikihub-cli` Python CLI package (pip-installable; wraps `/api/v1`)
- `app/` — Flask app (factory pattern in `__init__.py`)
- `app/models.py` — SQLAlchemy models (users, wikis, pages, stars, forks, api_keys, wikilinks, audit_log)
- `app/acl.py` — CODEOWNERS-pattern ACL parser for `.wikihub/acl`
- `app/git_backend.py` — git Smart HTTP (clone/push), ported from listhub
- `app/git_sync.py` — DB→git plumbing (does NOT fire hooks), public mirror regeneration
- `app/renderer.py` — markdown-it-py with wikilinks, KaTeX, footnotes, Obsidian embeds
- `app/auth_utils.py` — password hashing, API key gen/verify, Bearer auth decorators
- `app/routes/` — blueprints: main, auth, api, api_wikis, wiki, agent_surfaces, upload
- `hooks/post-receive` — git→DB sync (installed into each wiki's bare repo)
- `.interface-design/system.md` — design tokens and component patterns

## key invariants

- **git is source of truth for public content.** Postgres is derived index. private pages live in Postgres only.
- **DB→git sync does NOT fire hooks.** this prevents infinite sync loops.
- **two repos per wiki:** `repos/<user>/<slug>.git` (authoritative) + `repos/<user>/<slug>-public.git` (derived mirror).
- **frontmatter visibility wins over ACL file** (most specific wins).
- **API keys start with `wh_`**, SHA-256 hashed in DB, shown once on creation.

## deployment

- **Live URL:** https://wikihub.globalbr.ai
- **Server:** AWS Lightsail instance `wikihub-dev` (54.145.123.7)
- **SSH:** `ssh -i ~/.ssh/wikihub-dev-key ubuntu@54.145.123.7`
- **Code on server:** `/opt/wikihub-app`
- **DB:** PostgreSQL `wikihub` database (local to server)
- **Process:** gunicorn on port 5100, managed by systemd (`wikihub.service`)
- **Reverse proxy:** nginx on server, Cloudflare DNS in front (proxied, handles SSL)
- **ListHub also on same box:** `/opt/listhub-app` at https://listhub2.globalbr.ai

**Deploy:**
```bash
ssh -i ~/.ssh/wikihub-dev-key ubuntu@54.145.123.7 'cd /opt/wikihub-app && git pull && sudo systemctl restart wikihub'
```

**Secrets** are in `/opt/wikihub-app/.env` on the server (not in repo). Collaborator access keys are in `wikihub-dev-access/` (gitignored).

## issue tracking (beads)

`bd` is the issue tracker. all bugs and features are tracked as beads.

```bash
bd list              # all issues (open + closed)
bd list --open       # open issues only
bd show wikihub-xxx  # details on a specific issue
bd close wikihub-xxx -r "reason"   # close with reason
bd create            # create new issue
```

when starting a batch of work, run `bd list` to see open issues. close beads as you fix them with `-r` explaining what was done.

**ticket-first rule:** always create a beads ticket before implementing a feature or fix. close the ticket when done. this is the project's workflow — no exceptions.

**recurring issues:** bugs that keep coming back despite being "fixed". this is the most important pattern in this repo.

how it works:
1. when a bug reappears after being fixed, tag it: `bd label add <id> recurring`
2. before working on code in a recurring-issue area, check: `bd list --label recurring`
3. after making changes in that area, explicitly verify the recurring issues aren't regressed
4. when fixing a recurring bug, add a regression test or comment in the code explaining why it breaks

current recurring issues:
- **wikihub-58c** (sidebar indentation) — child nesting depth breaks after sidebar CSS/template changes
- **wikihub-bnj** (right-hand sidebar disappearing) — TOC, graph, and contextual widgets vanish after reader.html changes. reader.html is fragile — always diff against the last known good state before committing.
- **wikihub-9c8j** (sidebar sort order) — items reorder unexpectedly on click. maintain stable sort.

**why this matters:** multiple agents work on this repo in parallel. agent A fixes the sidebar, agent B modifies the same template for a different feature and accidentally reverts agent A's fix. the recurring label is a canary — if you see it, be extra careful with that file.

## signing into the dev app for visual testing

Because the dev DB doesn't mirror production, use the magic-link flow to create a
fresh test account and log yourself (or agent-browser / chrome-devtools MCP) into
an authenticated session in one shot:

```bash
USER="themetest-$(date +%s)"
KEY=$(curl -s -X POST http://localhost:5100/api/v1/accounts \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"password\":\"testpass12345\"}" \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['api_key'])")
URL=$(curl -s -X POST http://localhost:5100/api/v1/auth/magic-link \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"next":"/settings"}' | python3 -c "import json,sys;print(json.load(sys.stdin)['login_url'])")
echo "$URL"   # paste into the browser / navigate in chrome-devtools
```

For prod smoke tests, swap the server URL and use an existing account's password
or API key (jacobcole's key lives in 1Password — see
`~/.claude/projects/-Users-jacobcole-code-wikihub/memory/reference_wikihub_credentials.md`).

## verification: agent-browser is mandatory

**after making any UI or route change, verify it with `agent-browser` against the running dev server.** do not rely solely on e2e tests or curl. the user expects visual confirmation that the change works.

workflow:
1. start the dev server in tmux: `tmux new-session -d -s wikihub "source .venv/bin/activate && SECRET_KEY=dev DATABASE_URL=postgresql://localhost/wikihub REPOS_DIR=./repos ADMIN_TOKEN=devtoken flask --app wsgi.py run --port 5100 --debug"`
2. run e2e tests to catch regressions
3. use `agent-browser` to walk through the actual user flow:
   - `agent-browser open <url>` — open a page
   - `agent-browser snapshot` — read the DOM (find refs for elements)
   - `agent-browser fill <ref> "text"` — type into inputs
   - `agent-browser click <ref>` — click buttons/links
   - `agent-browser eval "js expression"` — check DOM state
   - `agent-browser screenshot /tmp/name.png` — take a screenshot, then `Read` it to visually inspect
4. screenshot the result and visually confirm it looks correct
5. after deploying to production, repeat the smoke test against `https://wikihub.globalbr.ai`

**do not skip agent-browser verification.** backend tests passing does not mean the UI works. the user will test in their browser and find the bug you missed.

## deploy checklist

see `docs/deploy.md` for full details. the short version:

1. `python3 tests/test_e2e.py` — all tests pass
2. `git status` — **every modified file is committed** (most common deploy failure is a missing file)
3. `git push origin main`
4. `ssh -i ~/.ssh/wikihub-dev-key ubuntu@54.145.123.7 "cd /opt/wikihub-app && git pull && sudo systemctl restart wikihub"`
5. `curl -s -o /dev/null -w "%{http_code}" https://wikihub.globalbr.ai/` — must be 200, not 502
6. if 502, check logs: `ssh -i ~/.ssh/wikihub-dev-key ubuntu@54.145.123.7 "sudo journalctl -u wikihub --no-pager -n 30"`
7. agent-browser smoke test on production for the specific changes made

## agent instruction surfaces (keep in sync)

when changing agent-facing docs or setup instructions, ALL of these must be updated together:

| Surface | Location | What it serves |
|---------|----------|---------------|
| `/AGENTS.md` route | `app/routes/agent_surfaces.py:102` | Plain markdown agent setup (quick start, API, MCP) |
| `/llms.txt` route | `app/routes/agent_surfaces.py:41` | LLM-readable site index |
| `/llms-full.txt` route | `app/routes/agent_surfaces.py:75` | All public pages expanded |
| `/agents` HTML page | `app/templates/agents.html` | Rendered human-readable agent docs |
| `/.well-known/mcp/server-card.json` | `app/routes/agent_surfaces.py:205` | MCP server discovery card |
| `/.well-known/wikihub.json` | `app/routes/agent_surfaces.py:234` | Bootstrap manifest |
| `/@user/wiki/llms.txt` | `app/routes/agent_surfaces.py:247` | Per-wiki LLM index |
| Landing page | `app/templates/landing.html` | Homepage with setup instructions |
| `AGENTS.md` (repo) | `AGENTS.md` (this file) | Developer/agent instructions for codebase |
| `MCP_TOOLS` list | `app/routes/agent_surfaces.py:21` | Tool definitions for MCP endpoint |
| CLI README | `cli/README.md` | `wikihub-cli` usage docs |
| CLI subcommand registry | `cli/wikihub_cli/__main__.py` (`build_parser`) | Actual CLI surface — keep in sync with docs |

**rule:** if you add a new API endpoint or change auth flow, update ALL surfaces above.

## design system

obsidian + amber. see `.interface-design/system.md` for tokens. key points:
- warm blacks (#0f0e0c), not GitHub blues
- amber accent (#d4a04a), not blue
- `[[wikihub]]` logo with bracket signature
- borders only, no shadows
- link icon for unlisted (not eye)
