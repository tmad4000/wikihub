# WikiHub MCP Connector — Design Blueprint

> Generated 2026-04-22 by a research subagent exploring the WikiHub codebase + curator agent + noos MCP patterns. This is the design pass; MVP code not yet written. Authoritative ground truth is marked "verified" below.

## Verified Ground Truth

**WikiHub repo:** `/Users/jacobcole/code/wikihub/` — Python 3 / Flask / PostgreSQL / bare git. Deployed on Lightsail `wikihub-dev` (54.145.123.7), gunicorn port 5100, nginx + Cloudflare in front. **Not the same box as noos** (`3.216.129.34`).

**WikiHub MCP endpoint already exists** at `wikihub.md/mcp` (GET + POST, JSON-RPC), implemented in `app/routes/agent_surfaces.py:539-570`. Thin internal proxy over the REST API (via Flask test client), stateless, registers 16 tools (`MCP_TOOLS` list, lines 21-38). **NOT built on `@modelcontextprotocol/sdk`** — it's a hand-rolled JSON-RPC dispatcher.

**Curator agent:** `app/routes/agent_chat.py`. HTTP SSE endpoint at `/agent/chat`. Calls Anthropic SDK directly (claude-sonnet-4-20250514), spawns an agentic loop with 5 tools: `read_file`, `write_file`, `list_files`, `search_content`, `wikihub_api`. Auth: per-user Anthropic key (stored encrypted in `users.llm_api_key_encrypted`) or OAuth credentials from per-user `CLAUDE_CONFIG_DIR`. **Not invocable from the MCP endpoint today** — browser/SSE only.

**Data model** (verified in `app/models.py`):
- `User` owns `Wiki` (slug-keyed, unique per owner)
- `Page` lives inside a `Wiki` (path-keyed, e.g. `wiki/hello.md`)
- Page fields: `path`, `title`, `visibility` (public / public-edit / private), `frontmatter_json`, `excerpt`, `content_hash`, `anonymous`, `claimable`, `search_vector` (FTS tsvector)
- `Wikilink` tracks `[[wikilink]]` back-references between pages
- `ApiKey`: `key_hash`, `key_prefix` (`wh_` + 8 chars), **`agent_name`, `agent_version`** already present (useful for provenance — no schema migration needed)

**Auth:** `Bearer wh_...` header. Keys rotate independently per label. Credentials convention: `~/.wikihub/credentials.json` (mode 0600).

**REST surface inventory** (from `api_wikis.py` + `api.py` + `agent_surfaces.py`):

| Method | Endpoint | Auth |
|--------|----------|------|
| POST | `/api/v1/accounts` | None (self-register) |
| GET | `/api/v1/accounts/me` | Optional |
| POST | `/api/v1/auth/magic-link` | Optional |
| POST | `/api/v1/wikis` | Required |
| GET | `/api/v1/wikis/<owner>/<slug>` | Optional |
| PATCH | `/api/v1/wikis/<owner>/<slug>` | Required (owner) |
| DELETE | `/api/v1/wikis/<owner>/<slug>` | Required (owner) |
| POST | `/api/v1/wikis/<owner>/<slug>/pages` | Required |
| GET | `/api/v1/wikis/<owner>/<slug>/pages` | Optional |
| GET | `/api/v1/wikis/<owner>/<slug>/pages/<path>` | Optional |
| PATCH | `/api/v1/wikis/<owner>/<slug>/pages/<path>` | Required |
| DELETE | `/api/v1/wikis/<owner>/<slug>/pages/<path>` | Required |
| POST | `/api/v1/wikis/<owner>/<slug>/pages/<path>/append-section` | Required |
| POST | `/api/v1/wikis/<owner>/<slug>/pages/<path>/visibility` | Required |
| POST | `/api/v1/wikis/<owner>/<slug>/share/bulk` | Required |
| GET | `/api/v1/wikis/<owner>/<slug>/grants` | Required |
| GET | `/api/v1/wikis/<owner>/<slug>/history` | Optional |
| GET | `/api/v1/search?q=` | Optional |
| POST | `/agent/chat` | Required (SSE) |

**SessionEnd hook** is already live in `~/.claude/settings.json:61-79` — fires `cci-capture.sh`. Input JSON has `transcript_path`, `session_id`, `cwd`, `reason`. Transcript is a JSONL file.

**Key gotcha:** Cloudflare blocks non-curl UAs. The MCP server's `httpx` calls must set `headers={"User-Agent": "curl/8.0"}` as a default in the API layer.

---

## Architecture Decision: Where the MCP Server Lives

The existing `/mcp` route in Flask is functional but non-standard — it lacks proper SSE streaming, server-info negotiation, and schema-validated input. It covers 16 tools but misses `wikihub_register_agent` and provenance-tracking fields.

**Recommendation: Build a standalone Python MCP server at `~/code/wikihub/mcp-server/`** — sibling to `app/`, thin adapter over the REST API, identical pattern to noos's TypeScript server. Uses the `mcp` PyPI package (`Server` + `stdio/http` transports). Reasons:

1. The hand-rolled Flask endpoint cannot stream SSE properly from the `flask test_client` proxy path (no real socket).
2. Mixing MCP SDK logic into Flask blueprints complicates the codebase.
3. Clean separation: new server calls `https://wikihub.md/api/v1` externally, just as noos's server calls `https://globalbr.ai/api`.
4. When/if the old `/mcp` endpoint needs removal, it's a one-line comment.

---

## Proposed Tool Surface

```typescript
// Pseudo-TypeScript interfaces for the 9+1 tool surface

interface WikihubConfig {
  baseUrl: string;        // default: "https://wikihub.md"
  apiKey?: string;        // "wh_..." — from x-api-key header or WIKIHUB_API_KEY env
  username?: string;      // for constructing /@username/... URLs
}

// read-only (no key required for public content)
wikihub_search(query: string, wiki?: string, limit?: number): SearchResult[]
wikihub_get_page(owner: string, wiki: string, path: string): PageDetail
wikihub_list_pages(owner: string, wiki: string): PageSummary[]

// write (key required)
wikihub_create_page(owner: string, wiki: string, path: string,
                    content: string, visibility?: string): CreatedPage
wikihub_update_page(owner: string, wiki: string, path: string,
                    content?: string, visibility?: string): UpdatedPage
wikihub_append_section(owner: string, wiki: string, path: string,
                       section_heading: string, content: string): void
wikihub_delete_page(owner: string, wiki: string, path: string): void

// meta
wikihub_list_wikis(owner: string): WikiSummary[]
wikihub_register_agent(username: string, email?: string): { api_key: string }

// curator (new — invokes /agent/chat SSE, returns final text)
wikihub_curate(prompt: string, owner: string, wiki: string,
               page?: string, session_id?: string): { text: string, session_id: string }
```

`wikihub_curate` is the unique value-add. It wraps the existing SSE `POST /agent/chat` endpoint, consuming the stream and returning the final assistant text. Any MCP client (Claude Desktop, ChatGPT) can invoke the Curator without managing SSE.

---

## File Paths for New Files

```
~/code/wikihub/
└── mcp-server/
    ├── pyproject.toml          # package: wikihub-mcp-server; deps: mcp, httpx, click
    ├── README.md
    ├── Dockerfile              # python:3.12-slim, single process, PORT=4200
    └── src/
        └── wikihub_mcp/
            ├── __init__.py
            ├── __main__.py     # entry: python -m wikihub_mcp [--stdio | --http]
            ├── api.py          # thin httpx wrapper over /api/v1, User-Agent: curl/8.0 default
            ├── server.py       # build_server(config) → mcp.Server with all tools
            └── http.py         # Starlette ASGI app: POST/GET/DELETE /mcp, GET /healthz

~/.claude/hooks/
└── wikihub-session-end.sh      # new hook (see Live Update design)

~/.claude/settings.json         # add new SessionEnd hook entry (edit, not new file)
```

Deployment on Lightsail 54.145.123.7:
```
/opt/wikihub-app/mcp-server/    # git subtree or symlink
```
Systemd unit `wikihub-mcp.service` runs `uvicorn wikihub_mcp.http:app --port 4200`. Nginx proxies `wikihub.md/mcp-sdk` (or subdomain `mcp.wikihub.md`) to port 4200. The existing hand-rolled `/mcp` stays untouched during transition.

Cloudflare DNS: A record for `mcp.wikihub.md` → `54.145.123.7` (zone ID `7cdf0e2256d7c01cae276da4d7b0b334`).

---

## Wire Diagram: Live Update Hook

```
Claude session ends
       │
       ▼
~/.claude/settings.json SessionEnd hook
       │  (stdin: {transcript_path, session_id, cwd, reason})
       ▼
~/.claude/hooks/wikihub-session-end.sh
  1. Read transcript_path JSONL — count lines, check cwd
  2. Skip if < 8 lines (too short) or cwd matches excluded paths
  3. Extract last N turns (e.g. last 20 JSONL lines)
  4. EVALUATOR: claude --print --model haiku-4-5 with a ~200-token prompt:
        "Does this turn contain a decision, design insight, completed
         feature, bug fix, research finding, or durable fact worth
         documenting? Answer YES or NO only."
  5. If NO → exit 0 (silent, no wiki write)
  6. If YES → claude --print --model haiku-4-5 with synthesis prompt:
        "Summarize this session into a wiki page. Title: one line.
         Body: markdown, ≤ 500 words. Focus on durable facts."
     → capture stdout as SYNTHESIZED_MD
  7. Derive page path: sessions/YYYY-MM-DD-<session-id-prefix>-<slug>.md
  8. POST /api/v1/wikis/jacobcole/claude-sessions/pages
        body: { path, content: SYNTHESIZED_MD, visibility: "private" }
        headers:
          Authorization: Bearer $WIKIHUB_API_KEY
          User-Agent: curl/8.0
          X-Agent-Name: wikihub-session-hook
          X-Agent-Version: 0.1.0
  9. Log result to ~/.claude/logs/wikihub-hook.log
```

**Evaluator design — two-stage, not one:**
- Stage 1: cheap YES/NO call (~1K tokens). Exits early for ~80% of sessions.
- Stage 2: synthesis. Fires only on YES.
- Both use `claude-haiku-4-5` (fast, cheap), NOT Sonnet.
- Cap: one wiki write per session.

**Idempotency:** Page path includes session_id prefix, making collisions impossible. The hash check (content_hash on Page model) is a belt-and-suspenders guard for manual re-runs.

**Rate cap:** Min-line threshold + YES/NO filter together mean most sessions produce no write. No debounce needed at MVP.

---

## Integration with noos Portable Context

Complementary, keep separate:
- **noos:** structured facts, relationships, tagged nodes. Queryable graph. "Who / what / how."
- **WikiHub:** narrative pages, session logs, long-form prose. Human-readable. "Why / what happened."

Cross-linking (v2, not MVP): after writing a wiki page, call `noos_relate` to link the new page URL (as a note node) to the relevant noos subtree node.

The `portable-context-ingest` skill should eventually split into two paths:
- Facts path (current behavior): extract entities → `noos add`
- Narratives path (new): extract story/rationale → `wikihub write`

MVP keeps them separate and un-linked.

---

## Provenance (Matching noos's `createdByKeyId` / `sourceType`)

WikiHub's `ApiKey` model already has `agent_name` and `agent_version` fields (models.py:171-172). The MCP server sets `X-Agent-Name: wikihub-mcp` and `X-Agent-Version: 0.1.0` on all writes — these land in `ApiKey.agent_name/agent_version` at key lookup time. **No DB migration needed.** Per-page provenance (which key wrote which page) is in git commit history (author = Curator or API key's username).

Gap vs noos: WikiHub doesn't currently persist a `createdByKeyId` on Page itself — it's all in git history. If we want in-app per-page filtering (like `noos list --key ChatGPT`), Page needs its own provenance columns. Non-blocking for MVP; candidate for v1.2.

---

## Risks and Open Questions

1. **`User-Agent: curl/8.0` requirement.** Cloudflare blocks non-curl UAs. Put it in `api.py` defaults, not per-call. Easy to forget.

2. **Curator via MCP nests SSE inside SSE.** `wikihub_curate` consumes an SSE stream internally. In HTTP/Streamable MCP transport, the outer request is also SSE. Nesting via `httpx` is straightforward but must be buffered correctly — collect all `data:` lines, emit final text as one MCP tool result block. **Test this path early.**

3. **`/agent/chat` requires per-user Anthropic key OR server env key.** For single-user (Jacob) use, `ANTHROPIC_API_KEY` on the server is fine. For multi-tenant, this is gated by the per-user key — agents without an Anthropic key get 400.

4. **Hook timing.** `SessionEnd` fires after session exits — transcript JSONL is complete. The `cci-capture.sh` precedent (hook:10-14) confirms `transcript_path` is a real file path. New hook runs in parallel with `cci-capture.sh` — both fire on the same event, no ordering dependency.

5. **Personal wiki slug.** Hook needs a target wiki slug (e.g. `claude-sessions`). Env var `WIKIHUB_SESSION_WIKI` with a default. The wiki must be created once manually or by the hook's first run.

---

## Build Order

### MVP (ship first)
1. `~/code/wikihub/mcp-server/src/wikihub_mcp/api.py` — httpx wrapper, 9 REST methods, `User-Agent: curl/8.0` default
2. `server.py` — 9 tools registered (no `wikihub_curate` yet)
3. `http.py` — Starlette ASGI, `POST/GET/DELETE /mcp`, `/healthz`, per-request auth from `x-api-key` header or `?key=` param (matching noos pattern)
4. systemd unit + nginx proxy on Lightsail, Cloudflare DNS for `mcp.wikihub.md`
5. Add to `~/.claude/settings.json` mcpServers
6. Test: `wikihub_search`, `wikihub_get_page`, `wikihub_create_page`

### v1.1 — Live update hook
7. `~/.claude/hooks/wikihub-session-end.sh` — evaluator + synthesis + REST write
8. Add to `SessionEnd` in `~/.claude/settings.json`
9. Create `jacobcole/claude-sessions` wiki on wikihub.md

### v2 — Curator + cross-linking
10. `wikihub_curate` tool (SSE consumer over `/agent/chat`)
11. `portable-context-ingest` skill split: facts → noos, narratives → wikihub
12. Post-write `noos_relate` in hook to cross-link wiki pages to noos subtree

---

## Essential Files for Understanding the Domain

- `~/code/wikihub/AGENTS.md` — authoritative architecture, deployment, invariants
- `~/code/wikihub/app/routes/agent_surfaces.py` — existing MCP endpoint (lines 539-570) and tool list (lines 21-38)
- `~/code/wikihub/app/routes/agent_chat.py` — Curator agent implementation
- `~/code/wikihub/app/routes/api_wikis.py` — REST surface for wikis and pages
- `~/code/wikihub/app/models.py` — data model (User, Wiki, Page, ApiKey)
- `~/code/wikihub/app/auth_utils.py` — auth decorators and key schema
- `~/code/noos/mcp-server/src/server.ts` — noos MCP pattern to mirror
- `~/code/noos/mcp-server/src/http.ts` — per-request auth isolation pattern
- `~/code/noos/mcp-server/src/api.ts` — thin API wrapper pattern
- `~/.claude/hooks/cci-capture.sh` — SessionEnd hook shape (stdin JSON schema)
- `~/.claude/settings.json` — existing hooks config to extend
- `~/.claude/skills/portable-context-ingest/SKILL.md` — portable context skill to split in v2
- `~/.claude/projects/-Users-jacobcole-code/memory/wikihub-upload-pattern.md` — `User-Agent: curl/8.0` gotcha
