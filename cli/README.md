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
| `auth login` | Add a new account without overwriting existing ones (gh-style multi-account). |
| `auth switch <profile>` | Set the active profile. |
| `auth status` | List all profiles and mark the active one. |
| `auth list` | Print profile names (one per line; active marked with `*`). |
| `auth logout [profile]` | Remove a profile (defaults to active). |
| `new <slug>` | Create a wiki. |
| `ls <owner/slug>` | List pages in a wiki. |
| `read <owner/slug/path>` | Print a page's markdown to stdout. |
| `write <owner/slug/path>` | Create or update a page (`--file`, `--content`, or stdin). |
| `publish <file> --to <owner/slug/path>` | File-first variant of `write`. |
| `rm <owner/slug/path>` | Delete a page. |
| `search <query>` | Full-text search (`--wiki owner/slug` to scope). |
| `share add/ls/rm <owner/slug>` | Manage collaborators on a wiki. |
| `mcp-config` | Print `mcpServers` JSON to wire WikiHub's MCP endpoint into an agent. |
| `version` | Print CLI version. |

`write` and `publish` accept `--visibility public|public-edit|private|unlisted|unlisted-edit`.
When omitted, page visibility inherits from `.wikihub/acl`; ACL-only
`public-view` and `unlisted-view` directives are returned as page-level
`public` and `unlisted`. Unlisted pages are readable by direct URL and appear
in that wiki's own navigation/sidebar and per-wiki RSS feed for viewers who can
read them, but remain excluded from global discovery surfaces such as search,
explore, global activity, global RSS, and profiles.
To pin a page in the wiki sidebar, include `pinned: true` in the page
frontmatter and write or publish the full markdown file again. Pinning only
affects readable pages; it does not grant access.

`.wikihub/*` paths are wiki plumbing rather than normal pages. `wikihub write`,
`publish`, and `rm` may target `.wikihub/acl` when the active profile owns the
wiki; ACL writes reindex inherited page visibility and refresh the public
mirror. Other `.wikihub/*` paths are rejected by the generic page API and are
excluded from `ls`, `read`, search/discovery, history, zip exports, agent
context, and public git mirrors.

`read` uses the REST page-read endpoint's access semantics: a missing page is
`404 not_found`, while an existing page outside the active profile's read access
is `403 forbidden` for authenticated callers or `401 authentication_required`
for anonymous reads. Restricted responses confirm existence but never include
the page title, content, or frontmatter.

## Auth

Credentials are read in this order (first wins):

1. `--server` / `--api-key` CLI flags
2. Env vars: `WIKIHUB_SERVER`, `WIKIHUB_USERNAME`, `WIKIHUB_API_KEY`
3. `~/.wikihub/credentials.json`, profile selected by:
   - explicit `--profile NAME` on the CLI, else
   - the `_active` profile pointer (set by `auth switch` / `auth login`), else
   - `default`

### Multiple accounts (gh-style)

Use `auth login` to add accounts without overwriting the one you're already logged in with:

```bash
# first login → saved as "default", becomes active
wikihub auth login --username alice --password ****

# second login → saved as "bob@wikihub.md" (auto-named), becomes active
wikihub auth login --username bob --password ****

# switch back
wikihub auth switch default

# see what you have
wikihub auth status
#   profile              username  server
# * default              alice     https://wikihub.md
#   bob@wikihub.md       bob       https://wikihub.md
# credentials: /home/you/.wikihub/credentials.json

# remove one (defaults to active if no name given)
wikihub auth logout bob@wikihub.md
```

Pass `--profile NAME` on any command to override the active profile for a single invocation.

### Credentials file format

```json
{
  "_active": "default",
  "default": {
    "server": "https://wikihub.md",
    "username": "you",
    "api_key": "wh_..."
  }
}
```

The top-level `_active` key tracks the active profile. Mode: `0600`.

## Relationship to git and MCP

The CLI is **one** of three authoring surfaces. Pick the one that fits:

- **git** — clone/push over HTTPS is the canonical flow for bulk editing, version history, or working offline.
- **MCP** at `{server}/mcp` — for agents that speak MCP natively (Claude Code, MCP-compatible tools).
- **CLI** (this) — for shell scripts, cron jobs, onboarding, and any pipe-friendly workflow.

All three wrap the same REST API at `/api/v1/*`.
