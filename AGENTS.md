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

**recurring issues:** use `bd label add <id> recurring` to tag bugs that keep coming back. before working on related code, check `bd list --label recurring` and verify those areas aren't regressed. current recurring issues:
- **wikihub-58c** (sidebar indentation) — check child nesting depth after any sidebar CSS/template change

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

## design system

obsidian + amber. see `.interface-design/system.md` for tokens. key points:
- warm blacks (#0f0e0c), not GitHub blues
- amber accent (#d4a04a), not blue
- `[[wikihub]]` logo with bracket signature
- borders only, no shadows
- link icon for unlisted (not eye)

## roadmap: agent onboarding (planned, not yet implemented)

The following design is the direction for how any portable agent (OpenClaw, Claude Code, custom) will integrate with WikiHub. Not yet built — listed here so any agent reading the repo learns the model.

### Three-layer model

Adopts the OpenClaw convention verbatim (162 community templates drop-in compatible). Cross-validated against Claude Code memory model, Letta core-memory blocks, ChatGPT Custom Instructions/Memory, and the ID-RAG paper (MIT, 2509.25299). See `~/memory/research/agent-context-structures-2026-04-23.md`.

A user's `@user/portable-self` wiki has this layout:

```
SOUL.md      # Tier 1 — agent identity (~300-500 tok). Stable across users.
USER.md      # Tier 1 — who the human is (~500-1500 tok). Preferences, role, focus.
AGENTS.md    # Tier 1 — operating procedures (~500-1500 tok). NOT named CLAUDE.md.
TOOLS.md     # Tier 1 — tool reach-for guide (~200-500 tok, optional)
MEMORY.md    # Tier 1 — auto-written learnings index (≤200 lines cap)

memory/
  YYYY-MM-DD.md     # Tier 3 — daily logs; today + yesterday auto-read,
                    #          older retrieved via memory_search
  topics/<topic>.md # Tier 3 — auto-written topic detail files
skills/<name>/
  SKILL.md          # Tier 2 — Anthropic's YAML-frontmatter skill spec.
                    #          Metadata always loaded; body on trigger.
  resources/...     # Tier 3 — bundled scripts/refs, read on demand
wiki/               # Tier 3 — RAG-retrievable pages (the bulk of WikiHub content)
```

Aggregate Tier 1 budget: **~3-8K tokens.** Per-file cap: 20K characters (mirrors OpenClaw).

**Why USER.md separate from SOUL.md:** OpenClaw and Letta both split these. Agent identity stays stable when the user changes; user facts can be edited without rewriting the soul. Don't merge them.

Spec: **wikihub-3r5q** (portable-self structure).

### Bootstrap pattern

A single connection string contains everything an agent needs:
```
https://wh_<key>@wikihub.md/
```
(basic-auth-style — preferred over query param because it doesn't leak via referrer or browser history)

Given that string, the agent makes ONE call to `GET /api/v1/me/bootstrap` and receives:

```json
{
  "user": { ... },
  "scope": { ... },
  "always_loaded": {
    "soul":   "<full SOUL.md>",
    "user":   "<full USER.md>",
    "agents": "<full AGENTS.md>",
    "tools":  "<full TOOLS.md or null>",
    "memory": "<MEMORY.md head — first 200 lines>"
  },
  "skills":    [ {"name": "...", "description": "...", "path": "..."} ],
  "wikis":     [ ... per-wiki summaries ... ],
  "discovery": { ... API entrypoints ... }
}
```

The agent stuffs `always_loaded.*` verbatim into its system prompt. Skills go in by metadata only; bodies are fetched via standard page reads when triggered. Wikis are RAG. Spec: **wikihub-4rel** (bootstrap-by-URL+key).

### Scoped keys (DM mode vs group mode)

Mental model: **main key = full access; other keys = restricted access to the same account.**

- A scoped child key is minted from a parent key via `POST /api/v1/keys/scoped` with: `label`, `max_visibility` ceiling (e.g. `public-edit` excludes private + unlisted), `wikis_allowlist` / `wikis_denylist`, `read_only`, `expires_at`
- Revoking the parent cascades to all children
- Audit-logged on every use; visible in /settings; individually revocable
- When a scoped key authenticates a browser session, the UI shows a persistent banner indicating restricted access (so a human never confuses it for their main session)
- Scope dimensions deliberately small: NO path globs, NO grandchildren

Spec: **wikihub-gzlt** (scoped/restricted API keys).

### Why this shape

- Wiki-as-config means the user edits one place (their wiki) to update agent behavior — no separate config file to learn
- Per-page visibility means the same `portable-self` wiki can be public OR scoped — agents see only what their key permits
- Framework-independent: any HTTP client can do this; no Claude/OpenAI/OpenClaw-specific bindings required
- Mirrors `CLAUDE.md` (always loaded) vs Claude skills (metadata + on-demand bodies), so agents that already understand that distinction map onto WikiHub trivially

### When changing this design

Update **all** of: this file, `~/memory/wikihub.md` (Mac Mini), the relevant ticket descriptions (gzlt/4rel/3r5q), and once routes ship: `/AGENTS.md` route, `/llms.txt`, `/agents` HTML, `/.well-known/wikihub.json`, landing page, MCP server tools list, CLI README. (Today only the first two are necessary because the routes don't yet describe this design.)

## Landing the Plane (Session Completion)

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd sync
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
