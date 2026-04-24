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

## regression-test rule (wikihub-qzje)

**Every bug fix MUST include a regression test that would have failed before the fix.** No exceptions.

- If you close a `bd type=bug`, the commit that closes it MUST touch `tests/`.
- If the bug lives in a programmatically-untestable layer (browser JS behavior, real OAuth round-trip, real DNS), the fix must EITHER add the testing infrastructure OR file a dependency ticket blocking the bug closure on adding that infra.
- Before saying "done", ask yourself: *"if someone reintroduced the exact bug, would my test catch it?"* If no, the test is insufficient.
- When you ship a bug fix without a test, label the bug `needs-regression-test`. Monthly sweep clears the backlog.

**Why this exists:** without it, agents (me included) keep shipping fixes without tests, and the same bugs re-emerge. Recent examples: `wikihub-35gh` (git-push hook) had no test; `/people` subdomain 404 had no test; the reader share modal has no JS-level test (hence a false-positive audit flag).

The goal (per `wikihub-qzje`): an agent running `python3 tests/test_e2e.py` can be confident that if it passes, no user-facing flow is broken — so Jacob never has to click-test before the agent says "done."

## schema changes

No Alembic. Fresh databases are built with `db.create_all()` at startup. For
existing databases (prod, dev, test) that need DDL changes — new columns,
dropped constraints, new indexes — drop a one-off SQL file in `migrations/`:

```
migrations/YYYY-MM-DD_short_description.sql
```

Make the SQL idempotent (`IF NOT EXISTS`, `DROP CONSTRAINT IF EXISTS`) so it
can be re-run safely. Apply with:

```bash
ssh ubuntu@54.145.123.7 "sudo -u postgres psql -d wikihub -f -" < migrations/FILE.sql
```

The header comment of each migration file should document its why + the exact
apply command. Keep migrations in chronological order.

## architecture

- `cli/` — `wikihub-cli` Python CLI package (pip-installable; wraps `/api/v1`)
- `mcp-server/` — standalone TypeScript MCP server (19 tools, stdio + Streamable HTTP transports). Deployed at `https://mcp.wikihub.md/mcp`. Source of truth for the Claude Connector. Ported from `noos/mcp-server/`. See `mcp-server/README.md`.
- `skills/` — checked-in Claude Code skills. `skills/wikihub-build/SKILL.md` is the port of Farza Majeed's `/wiki` skill to WikiHub (hosted storage instead of local files). Users install with `curl … > ~/.claude/skills/wikihub-build/SKILL.md`.
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

## core product principles

These are design invariants. If you find yourself "fixing" one as a bug, STOP and re-read.

### 1. One-command agent onboarding (wikihub-94dn)

`POST /api/v1/accounts {"username": "my-agent"}` returns an immediately-usable API key. No email, no verification, no browser. Email verification is **enrichment**, never a gate on core use. Reading/writing your own wikis, creating additional keys, sharing with existing users — all work without a verified email. See `wikihub-94dn` for the full spec.

### 2. Anonymous posting is a feature, not a security gap

`/@owner/wiki/new` accepting anonymous GET + POST on **public-edit** wikis is INTENDED. Low-friction contribution is core to the product. Anyone on the internet can land on a public-edit wiki and start writing without signing up.

Anonymous pages are marked `anonymous=True, claimable=True` by default. Any logged-in user can later **claim** an anonymous page as their own (first-come-first-served, abuse risk explicitly accepted per wikihub-7b2r). Owners who want to disable this lock the wiki's visibility (`private`) or set `anonymous=False, claimable=False` via the UI.

**The `can_write` check is correctly gated by `Page.visibility == "public-edit"` or ACL grant — not by login status.** If a code review tool flags "anonymous users can create pages" as a security hole, that's the tool misunderstanding the product.

### 3. Email verification unlocks invite materialization, not basic use

Pending invites (share-by-email-before-signup, wikihub-skp7) materialize when the user's email is verified — either via an invite-link token click (yjsv), Google OAuth auto-verify, or the standard verify-email flow. Basic wiki reading/writing on your own content works regardless.

### 4. Collaboration roadmap — phases

Explicit phase ordering (Jacob 2026-04-22):

1. **NOW** — Explicit per-user edit access (already shipped: wikihub-iga9 bulk share, wikihub-skp7 pending invites, wikihub-yjsv invite tokens). **Verify this works end-to-end before anything else.**
2. **NOW** — Public-edit + git behavior must be correct. Anonymous writes on public-edit wikis, git push as a first-class write path (currently blocked by wikihub-35gh).
3. **NOW** — All obvious bugs from the workflow audit (wikihub-i481 folder UX, reader share-modal `invited` handling, etc.). Sweep them.
4. **LATER** — Comment / suggestion workflow for collaborators. Asynchronous, non-blocking review. Think about the design but don't build yet.
5. **DEFERRED** — Real-time collaborative editing (Google Docs–style presence, cursors, CRDTs). **Explicitly not a v1 requirement.** Async collaboration via public-edit + suggestions is the product direction.

## deployment

- **Live URL:** https://wikihub.md (legacy alias: https://wikihub.globalbr.ai, still redirects)
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
5. after deploying to production, repeat the smoke test against `https://wikihub.md`

**do not skip agent-browser verification.** backend tests passing does not mean the UI works. the user will test in their browser and find the bug you missed.

## deploy checklist

see `docs/deploy.md` for full details. the short version:

1. `python3 tests/test_e2e.py` — all tests pass
2. `git status` — **every modified file is committed** (most common deploy failure is a missing file)
3. `git push origin main`
4. `ssh -i ~/.ssh/wikihub-dev-key ubuntu@54.145.123.7 "cd /opt/wikihub-app && git pull && sudo systemctl restart wikihub"`
5. `curl -s -o /dev/null -w "%{http_code}" https://wikihub.md/` — must be 200, not 502
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
