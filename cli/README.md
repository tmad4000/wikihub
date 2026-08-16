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

- **git** — clone/push over HTTPS is the canonical flow for bulk editing, version history, or working offline. Clone with `wikihub clone`, not a bare `git clone` — see below.
- **MCP** at `{server}/mcp` — for agents that speak MCP natively (Claude Code, MCP-compatible tools).
- **CLI** (this) — for shell scripts, cron jobs, onboarding, and any pipe-friendly workflow.

All three wrap the same REST API at `/api/v1/*`.

### `wikihub clone` — and why a bare `git clone` bites owners

```bash
wikihub clone jacobcole/notes          # OWNER/SLUG
wikihub clone notes                    # your own wiki
wikihub clone jacobcole/notes ./dir    # explicit directory
wikihub clone notes --no-persist       # don't store the auth header in .git/config
```

Every wiki is backed by **two** repos: the authoritative one (owner) and a
derived public mirror (everyone else). The server picks between them based on
HTTP Basic auth.

The trap: git only sends credentials **after** a 401 challenge, and a public
wiki never challenges on `git-upload-pack`. So a bare `git clone` — even with
credentials embedded in the URL — is silently anonymous and gives you the
**mirror**. The mirror is regenerated with fresh commits, so it shares no
history with the authoritative repo. Everything looks fine until you push:

```
 ! [rejected]  main -> main (fetch first)
```

…and `git pull --rebase` never converges, because fetch keeps returning the
mirror head while push targets the authoritative repo. (`GIT_TRACE_PACKET=1 git
push` reveals it: receive-pack advertises a sha your fetch has never seen.)

`wikihub clone` sends `Authorization: Basic …` preemptively via
`http.extraHeader`, so the server dispatches you to the authoritative repo from
the start, and persists that header into the clone's `.git/config` (chmod 600 —
it contains your API key; pass `--no-persist` to skip).

Equivalent by hand:

```bash
AUTH=$(printf 'USER:API_KEY' | base64)
git -c http.extraHeader="Authorization: Basic $AUTH" \
  clone https://wikihub.md/@USER/SLUG.git
```

Non-owners need none of this — the public mirror is the correct repo for them,
and anonymous clone works as expected.

`.wikihub/*` plumbing files (`acl`, `serve-inline`) are writable **only** over
git — the REST API rejects those paths — so this is the path you need for, e.g.,
opting an HTML page into inline serving.
