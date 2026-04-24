# WikiHub MCP Server

Model Context Protocol server for [WikiHub](https://wikihub.md). Plugs into
**Claude Desktop**, **Claude Code**, **ChatGPT (custom connector / deep research)**,
or any other MCP client. Same process, multiple clients.

Ported from the [noos MCP server](https://github.com/tmad4000/noos) — same
structure, same per-request auth isolation, adapted to WikiHub's REST API
(`/api/v1`) and Bearer-token auth.

---

## Tool surface (17 tools)

| Tool                         | What it does                                            | Auth? |
| ---------------------------- | ------------------------------------------------------- | ----- |
| `wikihub_whoami`             | Identity of the current api key                         | yes   |
| `wikihub_search`             | Fuzzy full-text search across pages                     | no    |
| `wikihub_get_page`           | Read one page's content                                 | no *  |
| `wikihub_list_pages`         | List every readable page in a wiki                      | no *  |
| `wikihub_get_wiki`           | Wiki metadata                                           | no    |
| `wikihub_commit_log`         | Git history for a wiki                                  | no    |
| `wikihub_shared_with_me`     | Wikis/pages shared with the caller                      | yes   |
| `wikihub_create_wiki`        | Create a new wiki                                       | yes   |
| `wikihub_create_page`        | Create a page                                           | **†** |
| `wikihub_update_page`        | Patch an existing page                                  | yes   |
| `wikihub_append_section`     | Append markdown under an optional `## heading`          | yes   |
| `wikihub_delete_page`        | Delete a page                                           | yes   |
| `wikihub_set_visibility`     | public / public-edit / private / unlisted               | yes   |
| `wikihub_share`              | Grant read/edit to a user or email                      | yes   |
| `wikihub_list_grants`        | Inspect ACL grants                                      | yes   |
| `wikihub_fork_wiki`          | Fork a public wiki                                      | yes   |
| `wikihub_register_agent`     | Self-register an account, return an api_key            | no    |
| `search`, `fetch`            | ChatGPT Deep Research aliases (same data, DR shape)    | no    |

\* Private pages require an api key with read access.
† Creating pages on `public-edit` wikis is allowed anonymously — pass
`anonymous: true`. Otherwise an api key is required.

---

## Install

```bash
cd mcp-server
npm install
npm run build
```

Leaves a runnable file at `mcp-server/dist/index.js`.

## Run locally

```bash
WIKIHUB_API_KEY=wh_yourkey node dist/index.js
```

Loggy output goes to **stderr** (MCP uses stdout for protocol traffic).

## Get an API key

Two paths:

1. **Web / CLI:** `curl -X POST https://wikihub.md/api/v1/accounts -H 'Content-Type: application/json' -d '{"username":"my-agent"}'`
   Response contains the api_key (shown once).
2. **Self-register via the server itself:** with no key set, call the
   `wikihub_register_agent` tool — it returns an `api_key`. Save and
   set it as `WIKIHUB_API_KEY` in your MCP client config.

## Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "wikihub": {
      "command": "node",
      "args": ["/Users/jacobcole/code/wikihub/mcp-server/dist/index.js"],
      "env": {
        "WIKIHUB_API_KEY": "wh_yourkey"
      }
    }
  }
}
```

Restart Claude Desktop. The `wikihub_*` tools show up in the tool picker.

## Claude Code (stdio)

```bash
claude mcp add -s user wikihub -- \
  env WIKIHUB_API_KEY=wh_yourkey node /Users/jacobcole/code/wikihub/mcp-server/dist/index.js
```

Or edit `~/.claude/mcp.json` directly with the same shape as the Claude Desktop
example above.

## Claude Code / remote (HTTP)

If you deploy the HTTP transport at `https://mcp.wikihub.md/mcp`:

```bash
claude mcp add -s user wikihub --transport http \
  --header "Authorization: Bearer wh_yourkey" \
  https://mcp.wikihub.md/mcp
```

## ChatGPT (custom connector / deep research)

1. Host the Streamable-HTTP server (`npm run start:http`) behind TLS — see
   **Deploy** below.
2. ChatGPT → Settings → Connectors → Custom → new connector:
   - **URL:** `https://mcp.wikihub.md/mcp`
   - **Auth:** custom header, `Authorization: Bearer wh_yourkey`

ChatGPT Deep Research uses the `search` / `fetch` tool pair (both registered).

## Dev

```bash
npm run dev   # tsc --watch
```

All source is in `src/`:

- `src/api.ts` — thin WikiHub REST client (`/api/v1`)
- `src/server.ts` — `buildServer(config)` with all tool registrations
- `src/instructions.ts` — personalized server-level instructions
- `src/index.ts` — stdio entrypoint
- `src/http.ts` — Streamable HTTP entrypoint

## Env vars

| Var               | Default                | Notes                                                 |
| ----------------- | ---------------------- | ----------------------------------------------------- |
| `WIKIHUB_API_URL` | `https://wikihub.md`   | Override for local dev (e.g. `http://localhost:5100`) |
| `WIKIHUB_API_KEY` | _unset_                | Required for writes and private reads                 |
| `PORT`            | `4200`                 | HTTP transport listen port                            |
| `HOST`            | `0.0.0.0`              | HTTP transport listen interface                       |

### Auth header precedence (HTTP transport)

1. `Authorization: Bearer <key>` — preferred
2. `x-api-key: <key>` — convenience
3. `?key=<key>` query param — Claude Desktop workaround
4. `WIKIHUB_API_KEY` env — local/single-tenant fallback

### Cloudflare

WikiHub's origin is behind Cloudflare. The API client sends
`User-Agent: curl/8.0` by default because some non-curl UAs are blocked.
Override via `WikihubConfig.userAgent` if you need to identify differently.

## Deploy

Sibling to the main app on Lightsail `wikihub-dev` (54.145.123.7):

```bash
ssh -i ~/.ssh/wikihub-dev-key ubuntu@54.145.123.7 \
  'cd /opt/wikihub-app/mcp-server && git pull && npm install && npm run build && sudo systemctl restart wikihub-mcp'
```

A suggested systemd unit file:

```ini
# /etc/systemd/system/wikihub-mcp.service
[Unit]
Description=WikiHub MCP server (Streamable HTTP)
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/wikihub-app/mcp-server
Environment=PORT=4200
Environment=HOST=127.0.0.1
Environment=WIKIHUB_API_URL=https://wikihub.md
ExecStart=/usr/bin/node dist/http.js
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Nginx front:

```nginx
server {
  listen 443 ssl http2;
  server_name mcp.wikihub.md;
  # ... TLS config ...
  location / {
    proxy_pass http://127.0.0.1:4200;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 300;
  }
}
```

Cloudflare DNS: A record `mcp.wikihub.md → 54.145.123.7` (proxied).

## License

Same as the parent wikihub repo.
