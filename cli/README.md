# wikihub-cli

Thin command-line wrapper over the WikiHub REST API. Reads `~/.wikihub/credentials.json` (default profile) or `WIKIHUB_*` env vars for auth.

## Install

```bash
# dev (editable) install — from repo root
pip install -e cli/

# or once published
pipx install wikihub-cli
```

## Quick start

```bash
# 1. sign up (saves key to ~/.wikihub/credentials.json)
wikihub signup --username you --password secret --server https://wikihub.md

# 2. create a wiki
wikihub new notes --title "My notes"

# 3. write a page (from stdin, file, or inline)
echo "# Hello" | wikihub write you/notes/index.md
wikihub write you/notes/idea.md --file draft.md
wikihub write you/notes/quick.md --content "# quick note"

# 4. read it back
wikihub read you/notes/index.md

# 5. search
wikihub search "hello" --wiki you/notes
```

## Commands

| Command | Purpose |
|---|---|
| `signup` | Create an account, save credentials. |
| `login` | Log in by username+password, or save an existing `--save-api-key`. |
| `logout` | Remove a profile from the credentials file. |
| `whoami` | Print the authenticated account. |
| `new <slug>` | Create a wiki. |
| `ls <owner/slug>` | List pages in a wiki. |
| `read <owner/slug/path>` | Print a page's markdown to stdout. |
| `write <owner/slug/path>` | Create or update a page (`--file`, `--content`, or stdin). |
| `publish <file> --to <owner/slug/path>` | File-first variant of `write`. |
| `rm <owner/slug/path>` | Delete a page. |
| `search <query>` | Full-text search (`--wiki owner/slug` to scope). |
| `mcp-config` | Print `mcpServers` JSON to wire WikiHub's MCP endpoint into an agent. |
| `version` | Print CLI version. |

## Auth

Credentials are read in this order (first wins):

1. `--server` / `--api-key` CLI flags
2. Env vars: `WIKIHUB_SERVER`, `WIKIHUB_USERNAME`, `WIKIHUB_API_KEY`
3. `~/.wikihub/credentials.json`, profile selected by `--profile` (default `default`)

File format (matches the `client_config` blob returned by signup):

```json
{
  "default": {
    "server": "https://wikihub.md",
    "username": "you",
    "api_key": "wh_..."
  }
}
```

Mode: `0600`.

## Relationship to git and MCP

The CLI is **one** of three authoring surfaces. Pick the one that fits:

- **git** — clone/push over HTTPS is the canonical flow for bulk editing, version history, or working offline.
- **MCP** at `{server}/mcp` — for agents that speak MCP natively (Claude Code, MCP-compatible tools).
- **CLI** (this) — for shell scripts, cron jobs, onboarding, and any pipe-friendly workflow.

All three wrap the same REST API at `/api/v1/*`.
