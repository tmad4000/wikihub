# wikihub — final spec (2026-04-08, updated 2026-04-10)

This is the consolidated specification for wikihub, combining all decisions from the side session and its branch fork on 2026-04-08, resolutions from the 2026-04-09 review session, and updates from the 2026-04-10 review. This document is self-contained — it supersedes the earlier `wikihub-spec-state-from-side-session-2026-04-08.md` and `wikihub-post-fork-delta-2026-04-08.md`.

---

## Name and shape

- **Name locked:** wikihub
- **One-liner:** GitHub for LLM wikis — a hosting platform for Karpathy-style markdown knowledge bases with Google-Drive-style per-file access control, native agent API (MCP + REST + content negotiation), and social features (fork, star, suggested edits).
- **First users (v1):** Ourselves via dogfood migrations (admitsphere, RSI wiki, systematic altruism wiki, jacobcole.net/labs, the CRM); the Karpathy-gist wave of early adopters who have their own LLM wikis and no shared place to publish; Obsidian vault owners looking for an agent-aware publish target better than Obsidian Publish or Quartz.
- **Distribution goal (not a design persona):** Get Boris / team at Anthropic using wikihub. Surfaced in a Granola meeting transcript as a partnership angle. Do not design v1 around Boris; design around the three first-user groups above and treat the Anthropic pitch as a post-launch outreach move.
- **Future target (architecture must not preclude):** Anthropic intranet — LLM wikis for companies. Implies multi-tenancy / org support later; the principal abstraction in the ACL model reserves space for this at zero current cost.
- **Built from scratch** in a new repo. Not grafted onto listhub. Copies listhub's git plumbing patterns verbatim (`git_backend.py`, `git_sync.py`, `hooks/post-receive`) — those files are battle-tested and should be ported with path generalization, not rewritten.
- **Reader aesthetic bar:** KaTeX, code highlighting, footnotes, clean dark typography. Justification: the Karpathy-wave audience expects a real reader (many of them run ML research wikis with math and code).
- **Landing page framing (from user story research 2026-04-09):** Lead with "Drop your files, get a URL" — the #1 user story is zero-config publishing, not agent APIs. Secondary pitch: "Your agent can read and write it too" (MCP + REST + content negotiation). The agent-first surface is the retention hook; publishing is the acquisition hook. Don't lead with git or API on the landing page.

### Personal wiki (auto-provisioned on signup) — added 2026-04-10

Every user gets a **default personal wiki** created automatically at signup. Architecturally identical to any other wiki (same bare git repo pair, same ACL system, same rendering) but semantically special:

- **Auto-created on signup** with slug matching the username. `/@username` resolves to this wiki's `index.md` — it IS the user's profile page.
- **Can't be deleted.** It is the user's identity on the platform. Other wikis can be deleted; the personal wiki cannot.
- **Default private ACL.** Scaffolded with `* private` like every other wiki (updated 2026-04-10). The personal wiki starts locked — users explicitly publish what they want. Same private-by-default invariant as all wikis.
- **Profile-aware frontmatter.** The `index.md` supports optional profile fields in frontmatter (`bio:`, `avatar:`, `links:`) that the profile template renders specially — similar to GitHub's profile README pattern. **[OPEN QUESTION — wikihub-722]** This was a Claude-proposed design (2026-04-10 03:45 PT). Needs human review: should bio/avatar/links live in frontmatter, in DB fields with a settings UI, or should the rendered markdown body simply BE the profile with no special fields? Current implementation works but the decision is not locked.
- **Other wikis listed below.** When visiting `/@username`, the personal wiki's index renders as the main content, with the user's other wikis listed below or in a sidebar.
- **Discoverability respects viewer context.** The owner sees all of their wikis on `/@username`; everyone else sees only discoverable project wikis (`public`, `public-edit`). This is a people page, not a private wiki-name leak.

Other wikis a user creates are projects/topics. The personal wiki is *you*.

### Official wikihub wiki — added 2026-04-10

wikihub has a **platform-level wiki** at `/@wikihub/wiki` (or `/@wikihub/docs`) — the official knowledge base, docs, and curated showcase. This is:

- **The canonical docs site** — how-to guides, API reference, onboarding walkthroughs live here as wiki pages, not static docs.
- **A curated showcase** — featured wikis, example use cases, and community highlights are pages in this wiki, editable by admins, not just an algorithmic feed.
- **A dogfood exemplar** — demonstrates what a well-structured wiki looks like on the platform (good ACL usage, folder structure, frontmatter, tags).
- **Linked from the landing page** — the `/explore` featured section draws from both the official wiki's curated content AND the algorithmic most-starred feed (see Featured curation below).

## Stack

- **Backend:** Flask + **Postgres** + Jinja + bare git
- **Renderer:** markdown-it + markdown-it-footnote + markdown-it-katex + highlight.js + markdown-it-image-figures + custom wikilink plugin + custom Obsidian-embed plugin (for `![[image.png]]` and `![[image.png|300]]` syntax) + external-links-in-new-tab config.
- **No JS framework.** Server-rendered dark-theme templates, CSS variables. Minimal vanilla JS where needed.
- **Mobile-friendly from v1** — see "Non-functional requirements" below. Not polish; a day-one hard requirement.
- **Web editor: Milkdown** (locked 2026-04-09). Port from listhub (ProseMirror-based WYSIWYG + raw textarea toggle, bundled as `static/js/milkdown-bundle.js`). Add custom wikilink `[[...]]` ProseMirror node + InputRule + autocomplete popover backed by a `GET /resolve?q=...` FTS endpoint. Round-trips with Obsidian for free (`[[target]]` / `[[target|label]]`).
- **Domain: wikihub.md** (locked 2026-04-09). Server deployment TBD (same Lightsail box or new instance — decide at deploy time).

### Why Postgres (not SQLite)

Earlier sessions drifted toward SQLite on scope-cut grounds. Overruled — Postgres is locked. Reasons: concurrent writes on shared tables (stars, forks, ACL grants), `jsonb` for frontmatter and ACL rows, `tsvector` + GIN for cross-wiki full-text search, room for row-level security later. The "1-week scope" worry is not load-bearing for Postgres-vs-SQLite because coding agents don't feel that choice as a velocity difference.

## Non-functional requirements

- **Mobile-friendly v1.** Every page works on a 375px-wide viewport. Mobile-first CSS (`min-width` breakpoints, not `max-width`). Touch targets >=44x44px. Sidebar collapses to a hamburger drawer on narrow viewports. Cmd+K is full-screen modal on mobile. Editor is virtual-keyboard friendly, no hover-dependent UI. Tables and code blocks scroll horizontally, not clip. Images responsive (`max-width: 100%`). Body font minimum 16px to avoid iOS zoom-on-focus.
- **Mockup-first workflow.** Wikihub has a `mockups/` directory from day one. Every significant UI surface gets a standalone HTML mockup (inline CSS/JS, no backend dependencies) before any implementation code is written. Required surfaces before coding: landing, `/@user` profile, wiki reader view, wiki edit view, Cmd+K search, `/explore`, folder view, visibility panel, permission error page, plus mobile versions of all of these. Pattern ported from listhub's `mockups/` directory workflow.

---

## Data architecture — the load-bearing invariant

**Bare git repo = source of truth. Postgres = derived index, rebuildable from repos.**

- Every wiki has its own bare repo at `repos/<user>/<slug>.git` (repo-per-wiki, confirmed — not repo-per-user).
- **Markdown page content does NOT live in Postgres** (confirmed 2026-04-09, clarified 2026-04-10). DB stores metadata only: `pages` table has id, wiki_id, path, title, visibility, frontmatter_json, excerpt (~200 chars), content_hash, timestamps. Content reads go through `git cat-file` from the bare repo (~3-5ms per file, batch via `git cat-file --batch`). This eliminates content sync bugs — the DB is purely derived metadata, not a content replica. The search index (`tsvector`) is populated from content at index time but no content column exists in Postgres.
- **All pages live in the authoritative git repo — including private pages** (clarified 2026-04-10). The two-repo model handles access control: private pages exist in the authoritative repo (owner-only access) and are excluded from the public mirror during regeneration. There is no Postgres-only content tier. This means the authoritative repo is the complete backup — `wikihub reindex` can rebuild all Postgres metadata from git HEAD.
- **Binary files (images, PDFs, attachments) live in the git repo alongside markdown**, same as Obsidian treats attachments. No quota in v1. Git LFS is NOT used in v1. Future escape hatch when a wiki outgrows in-repo binaries: globalbr.ai already has an S3 bucket that can host external blobs, with markdown links rewritten server-side to signed HTTPS URLs (v2+).
- Postgres stores: users, wikis, pages (derived metadata + search index), wikilinks, stars, forks, sessions, API keys, audit rows. Social graph (stars, forks) is ONLY in Postgres and can't be rebuilt from repos — those need DB backup. All page content (public and private) lives in the authoritative git repo and can be fully reindexed from git HEAD.
- **Two-way sync** copied from listhub:
  - DB->git: Flask writes via git plumbing (`GIT_INDEX_FILE`, `hash-object`, `update-index --cacheinfo`, `write-tree`, `commit-tree`, `update-ref`). This path does NOT fire hooks, which is why the loop below doesn't run forever.
  - git->DB: `post-receive` hook parses pushed .md files and calls the admin REST API. Fires on `git push`, NOT on `update-ref`.
- **Reindex commands** from day one: `wikihub reindex <wiki>`, `wikihub reindex --all`, `wikihub verify <wiki>` (diff Postgres against HEAD, report mismatches).

## Per-wiki storage: authoritative + public mirror

Every wiki exists as **two bare git repos**:

- `repos/<user>/<slug>.git` — authoritative. Owner only. All files, private + public. `.wikihub/acl` lives here.
- `repos/<user>/<slug>-public.git` — derived public mirror. Regenerated on every push to the authoritative. Contains only public files, with `<!-- private -->` bands stripped from markdown and `.wikihub/acl` itself omitted.

**Flask dispatches clones by auth:**

```
GET /@alice/crm.git/info/refs?service=git-upload-pack
  owner?  -> git-http-backend on repos/alice/crm.git
  else?   -> git-http-backend on repos/alice/crm-public.git
```

Stock `git-http-backend`, stock `git-upload-pack`. `git clone`, `fetch`, `push`, `blame`, `bisect`, LFS, partial clone — all work unchanged on both repos. No custom libgit2, no wire-protocol surgery. The cleverness happens once per push in the regeneration hook, not per clone.

**Public mirror history is linearized.** Each regeneration force-updates `HEAD` to a single new commit `Public snapshot @ <source-sha>`. No history preservation on the mirror. `git blame` on the mirror is deliberately useless; authorship surfaces from Postgres on rendered pages. Mirror is a publishing artifact, not a parallel history.

**Two separate bare repos (not two refs in one repo)** — chosen for auditability. Filesystem permissions separate them, and you can `ls -R` the public mirror on disk to confirm no private content leaked.

**Private pages live in the authoritative repo only** (clarified 2026-04-10). They are excluded from the public mirror during regeneration. The two-repo split IS the access control — the authoritative repo is owner-only (via filesystem permissions + Flask auth dispatch), and the public mirror contains only what non-owners should see. No per-file filtering at clone time, no custom pack surgery. The `git` layer is coarse (owner-or-not); the ACL file + Postgres handle fine-grained per-file visibility for web rendering and search.

**Flask dispatches web reads by auth too (added 2026-04-10):**

The same owner-or-public-mirror split applies to ALL web-facing reads, not just git clones:

- **Wiki index** (`/@user/slug`): reads `index.md`/`README.md` from authoritative repo for owner, public mirror for everyone else.
- **Page view** (`/@user/slug/page`): reads content from authoritative repo for owner, public mirror for everyone else.
- **Sidebar**: lists files from authoritative repo for owner, public mirror for everyone else. Additionally, non-owner sidebar filters to **discoverable pages only** (public, public-edit) — unlisted pages are in the public mirror (so they render when accessed by URL) but excluded from listings/sidebar. This preserves the "accessible by URL, not indexed" contract.
- **ZIP download**: already dispatched by auth (owner gets authoritative, others get public mirror).

**Unlisted = accessible but not discoverable.** Unlisted pages exist in the public mirror so they can be rendered when a non-owner visits the URL directly. But they are excluded from sidebar listings, search results, and the `/explore` page. The sidebar filters by checking `Page.visibility` in Postgres — only `public` and `public-edit` pass the discoverability check.

**v2: ACL grants in sidebar.** Currently, non-owner sidebar shows only public-mirror content. A user with `@alice:read` grant on a private page won't see it in the sidebar. To support this, merge DB-backed private pages the user `can_read()` into the sidebar alongside public-mirror files. Don't switch to authoritative repo reads for grantees — that would leak private bands in otherwise public pages.

### Why not dynamic pack filtering

We explored dynamically filtering a git pack per request (rewriting tree/commit hashes at clone time via libgit2) and rejected it as a research-project-in-the-wrong-shape. The two-repo pattern gives us stock git tooling, zero custom wire protocol, and on-disk auditability.

---

## ACL storage: `.wikihub/acl` (CODEOWNERS-pattern file)

The single most important design choice. Access control lives in a git-tracked file at `.wikihub/acl` using the same pattern as `.gitignore`, `.gitattributes`, and `CODEOWNERS`: **glob rules, most-specific wins, private by default, comments with `#`**.

Example (this is what every new wiki scaffolds to, header included so LLMs reading the file cold understand the format):

```
# wikihub ACL — declarative access control for this wiki.
#
# Rules are glob patterns. Most-specific pattern wins. Default is `private`.
#
# Visibility: private | public | public-edit | unlisted | unlisted-edit
# Grants:     @user:read | @user:edit
#
# Examples:
#   * private                      # everything private (the default)
#   wiki/** public                 # publish the wiki/ subtree (read-only)
#   wiki/secret.md private         # override: this one stays private
#   wiki/collab.md public-edit     # anyone can edit this page
#   drafts/** unlisted             # accessible by URL, not indexed
#   wiki/project.md @alice:edit    # share with a specific user

* private

wiki/**                   public
wiki/karpathy-private.md  private
wiki/collab.md            public-edit
private-crm/**            @alice:edit
drafts/**                 unlisted
community/**              unlisted-edit
```

### Why CODEOWNERS pattern won

Alternatives considered and rejected:
- **Per-file YAML sidecars** (`page.md` + `page.md.acl`): doubles tree entries, orphan risk, rename drift.
- **Frontmatter only**: markdown-only, can't express bulk rules or non-markdown files (PDFs, images).
- **Flat enumeration index** (one file listing every path): merge conflicts, doesn't express folder patterns, doesn't scale.
- **CODEOWNERS pattern** (selected): git-native, diffable, blameable, file-type agnostic, private-by-default is one line, scales tiny-to-huge, safe failure modes.

### `.wikihub/` platform dotfolder

Peer to `.git/`, `.github/`, `.claude/`. Future home of `config`, `schema.md`, `webhooks`, etc. Agents that know `.github/` recognize `.wikihub/` immediately as platform metadata.

### Safe failure modes

- Missing `.wikihub/acl` -> whole wiki treated as private.
- Malformed file -> push rejected with clear error; previous version stays in effect.
- Unknown rule types -> parsed, logged as warnings, don't break the file.

---

## Frontmatter and ACL file compose via specificity, not authority

Frontmatter is NOT "just a hint." Both are authoritative for different scopes, composed by specificity.

Precedence ladder (most specific wins):

1. **Frontmatter on the file** — most specific, wins for that file.
2. **ACL file rule matching the path**, most-specific pattern first.
3. **Repo default** (`* private`, implicit).

No two-sources-of-truth problem because they operate at different granularities. The ACL file is for **bulk patterns** ("everything in `wiki/` is public"). Frontmatter is for **single-file exceptions** ("this one page is different"). Same resolution model as `git config` (system < global < local < flags) or CSS specificity.

When an agent writes `visibility: public` in a frontmatter, the server treats it as an authoritative change for that file, logs the change, and enforces it.

## Obsidian frontmatter compatibility

**Keys wikihub uses:** `visibility`, `share`, `title`, `description`. None collide with Obsidian reserved keys.

**Read liberally, write conservatively** (Postel's Law applied to config files):

- **On read (v1):** honor `visibility:` and `tags:` (both list and flow syntax, strip leading `#`). These are the two that matter for v1 publishing and search.
- **On read (v1.5):** add `publish: true/false` alias for `visibility:`, `aliases` for wikilink resolution, `permalink` for URL slugs, `description` for OG tags, `cssclass`/`cssclasses` for styling. Blog-post-worthy Obsidian Publish migration story.
- **On write:** only touch `visibility:`. Never clobber keys we don't own. Round-trips cleanly to Obsidian and back.

---

## Permission model

Three orthogonal axes:

1. **Read audience:** owner | grantees | link-holders | signed-in | anyone
2. **Write audience:** same ladder
3. **Discoverable:** indexed | hidden

**Internal mode names stay short** (`public`, `public-edit`, `unlisted`, `unlisted-edit`) — that's what the ACL file uses and what the API returns. **User-facing UI labels disambiguate** with parentheticals: `public (read)`, `public (edit)`, `unlisted (read)`, `unlisted (edit)`, `private`, `signed-in (read)`.

### v1 mode vocabulary (simplified 2026-04-09)

Two orthogonal properties: **visibility** (who discovers it) × **editable** (can non-owners write).

| Mode | Read | Write | Discoverable |
|---|---|---|---|
| `private` | owner only | owner only | no |
| `public` | anyone | owner only | yes |
| `public-edit` | anyone | **anyone, anonymous OK** | yes |
| `unlisted` | URL holders | owner only | no |
| `unlisted-edit` | URL holders | **anyone with URL, anonymous OK** | no |

**"Shared" is a modifier, not a mode.** A `private` file with `@alice:read` = shared with alice. A `public` file with `@alice:edit` = alice can edit, visitors read-only. Grants layer on top of any base visibility.

**Both `public-edit` and `unlisted-edit` allow ANONYMOUS writes in v1. No account required. Google Docs link-edit model.**

**Removed from v1 (deferred to v2):** `signed-in` mode, `link-share` mode, `link:token:role` grants, `group:name:role` grants, `comment` role. All deferred alongside the link-token generation UI, group management UI, and comments feature.

### Grant syntax (v1)

ACL file grants: `@user:read`, `@user:edit`. That's it for v1.

v2 additions: `group:name:role`, `link:token:role`, `comment` role, `admin` role.

### v1 web UI dropdown

5 options: Private, Public (read), Public (edit), Unlisted (read), Unlisted (edit). Plus a "Share with user" field for `@username:read` or `@username:edit` grants.

### Visibility badges

Every page, wiki card, search result, explore entry, profile item, and sidebar row shows a small visibility icon (lock for private, globe for public, eye for unlisted, pencil-in-circle for edit variants). Badges are clickable and open the visibility dropdown for the owner. Table-stakes UX, not polish.

---

## `<!-- private -->` HTML comment sections

Ships in v1 as a lightweight sub-file privacy tool:

- `<!-- private -->...<!-- /private -->` blocks in markdown are **stripped from the public mirror and from content-negotiated markdown responses**.
- They REMAIN in the authoritative repo. Anyone with owner access sees them.
- UI shows a visible warning on pages that contain private bands: "this page has sections visible only to editors; not a security boundary — don't use it for real secrets."
- Framed as a **parasitic syntax** (rides on markdown's HTML-preserving rule), a **best-effort convenience feature**, not a security primitive.
- Doesn't compose with the share graph — binary public-or-owner, not share-aware. For finer control, split the private chunk into its own file under a per-file ACL.

---

## v1 ships WITHOUT anti-abuse machinery (major scope cut)

A full anti-vandalism plan was drafted and then **entirely cut from v1** at user's direction. Ship anonymous-writes naked; iterate reactively when problems actually occur.

**OUT of v1 (deferred to v2/v3):**
- Per-IP / per-token / per-wiki write rate limits
- Honeypot form field
- Actor logging beyond basic author field
- Moderation view (edit history filtered for owner)
- Revert tooling beyond git native (no bulk revert, no "revert all edits by actor X")
- Panic button (instant disable of anonymous writes on a wiki)
- Auto-under-attack mode (state machine on edit rate)
- Owner notifications (email/webhook on anonymous edits)
- Quarantine queue (pending-review edits)
- Proof-of-work reactive throttling
- Body/link size caps on writes
- CAPTCHA escape hatch
- "Are you sure?" warnings when enabling public-edit
- "Default off for public-edit on new wikis" safety
- `crawl: false` per-wiki opt-out from llms.txt
- Edit filters, trust levels, spam heuristics

**IN for v1 (basic plumbing only):**
- Anonymous git commits use `anonymous <anon@wikihub>` as the git author (required by git itself).
- Pages table has a nullable author field. No elaborate polymorphic actor.
- Basic signup rate limit per IP is still in (infra, not content moderation — confirm if asked).

**Philosophy locked:** "ship it, iterate reactively when real problems happen." The inversion from fail-closed to fail-open is deliberate.

---

## Agent-first surface

From `agent-first-web-brief-2026-04.md` — all cheap, all shipping in v1:

- **Content negotiation** on every page URL. `Accept: text/markdown` -> raw markdown with frontmatter, no chrome. `Vary: Accept` and `Link: rel=alternate; type=text/markdown` headers. Also serve `<url>.md` as the primary machine-readable URL. **Cloudflare caveat (validated 2026-04-09):** Cloudflare ignores `Vary: Accept` for cache keying on non-Enterprise plans. Mitigation: bypass Cloudflare cache for wiki page routes (`/@*` URLs) via Cache Rule; cache only static assets (CSS/JS/fonts). Content negotiation works at the origin; `.md` suffix is the reliable agent path through any CDN. Optionally add a 15-line CF Worker for cache-key sharding by `Accept` header later.
- **`/llms.txt`** and **`/llms-full.txt`** auto-generated per wiki and site-wide. Stripe pattern (curated top with an `## Optional` bucket for long tail).
- **`/AGENTS.md`** at site root AND **`/agents`** as a rendered HTML page (same content, dual format). This is the primary onboarding surface for agents. Content is structured as plain-English step-by-step instructions that an LLM can follow without parsing OpenAPI. Includes:
  1. **One-call registration:** `POST /api/v1/accounts` with optional username/email → 201 with `{user_id, username, api_key}`. API key shown once, agent must save it. No browser, no CAPTCHA.
  2. **Auth:** `Authorization: Bearer <api_key>` on all subsequent calls. Git HTTP Basic accepts API key as password.
  3. **Create a wiki:** `POST /api/v1/wikis {slug, title?, description?}` → 201 with wiki metadata.
  4. **Add pages:** `POST /api/v1/wikis/:owner/:slug/pages {path, content, visibility?}`.
  5. **Read/search:** `GET /api/v1/wikis/:owner/:slug/pages/*path`, `GET /api/v1/search?q=...`.
  6. **MCP endpoint:** `https://wikihub.md/mcp` — full tool suite, same capabilities as REST.
  7. **Content negotiation:** `Accept: text/markdown` on any page URL returns raw markdown.
  8. **Copyable curl examples** for registration, wiki creation, and page creation.
  9. **"Plain English instructions for Claude / ChatGPT"** section — the same steps written as natural language that an agent can follow verbatim. This is the bridge between "we have an API" and "agents can use it cold."
- **`/.well-known/mcp/server-card.json`** (SEP-1649 shape) and **`/.well-known/mcp`** (SEP-1960 shape). Both shipped — major MCP clients implement both speculatively and they're cheap.
- **`/.well-known/wikihub.json`** site bootstrap manifest (API base, MCP URL, signup URL, docs URL) so an agent pointed at the domain can self-bootstrap.
- **Server-hosted MCP server** (`wikihub-mcp`) wrapping the REST API. Tools: `whoami`, `search`, `read_page`, `list_pages`, `create_page`, `update_page`, `append_section`, `delete_page`, `set_visibility`, `share`, `create_wiki`, `fork_wiki`, `commit_log`.
- **WebMCP** tool registration on edit pages via `navigator.modelContext.registerTool` (Chrome 146 flag-only as of April 2026, feature-detected). Reuses logged-in browser session.
- **JSON-LD** — **restored to v1** (2026-04-10). `@type: Article` structured data on HTML responses with author, datePublished, dateModified, license. ~15 lines of Jinja template. Zero dependencies, zero cost to ship.

## Agent-native auth

- `POST /api/v1/accounts {display_name?, email?}` -> `201 {user_id, username, api_key}`. Email optional, username server-assigned if omitted, no CAPTCHA, no verification required.
- **API key management (updated 2026-04-10).** Keys use the GitHub/Stripe model: hashed (SHA-256) in the DB, shown in full only at creation time. But generating new keys is frictionless:
  - **Settings page** (`/settings`): web UI for account management, personal-wiki/profile entry points, and agent credentials. Shows all keys (by prefix), "Generate new key" button that displays the full key inline with a copy button, and per-key "Revoke" button. Accessible from the logged-in account menu in the nav bar.
  - **`POST /api/v1/auth/token {username, password}`**: agent-native endpoint — exchange credentials for a fresh API key. No browser needed. This is the canonical "I lost my key" or "I'm a new agent that only knows a password" path.
  - **`POST /api/v1/keys`** (Bearer auth): create additional keys programmatically if you already have one.
  - **`GET /api/v1/keys`** (Bearer auth): list keys by prefix, label, last_used_at, agent_name. Does NOT return full key values (hashed).
  - **`DELETE /api/v1/keys/:id`** (Bearer auth): revoke a key.
  - An agent should never be permanently locked out — if it has a password, `/auth/token` always works. If it has an existing key, `/keys` creates a new one.
- **Three web login methods** (updated 2026-04-10): Google OAuth, username+password, and API key paste. The login page has all three as equal options.
- **Magic sign-in links (added 2026-04-10).** `POST /api/v1/auth/magic-link` (Bearer auth) returns a short-lived single-use browser login URL for that same account, e.g. `{"login_url": "https://wikihub.md/auth/magic/wl_...", "expires_at": "..."}`. Visiting the link establishes a normal web session and invalidates the token immediately. This is for the "my agent should just hand me a one-click sign-in link" workflow without exposing the API key itself.
- `PATCH /api/v1/accounts/me` for programmatic rename (username, display_name, email independently). Username change -> old slug redirects for 90 days.
- `POST /claim-email` for post-hoc email affiliation. Email is not identity; it's an optional attachment.
- ~~Per-key scopes~~ — **cut from v1** (2026-04-09). Every API key is `read+write`. Add scopes when users ask for read-only keys.
- Soft `X-Agent-Name` / `X-Agent-Version` header logged; surfaced in user's dashboard ("this key was used by `claude-code@1.2.3`").
- `/api/v1/delegation` endpoint — **deferred to v2** (2026-04-09). RFC 8693 token exchange for scoped, short-lived agent tokens. No current users; skip the stub.
- Git HTTP Basic Auth accepts password OR API key (listhub convention).
- **Google OAuth + local email/password** (locked 2026-04-09). Noos OAuth dropped entirely. Google OAuth covers the ML/Anthropic crowd; local email/password covers everyone else and agents.
- Signup rate limit per IP (infra, not moderation) probably still in v1.

## Page REST API (v1)

- `POST /api/v1/wikis/:owner/:slug/pages` — create a page at a new path.
- `GET /api/v1/wikis/:owner/:slug/pages/*path` — read (respects content negotiation for `Accept: text/markdown`).
- `PUT /api/v1/wikis/:owner/:slug/pages/*path` — full replace.
- `PATCH /api/v1/wikis/:owner/:slug/pages/*path` — partial update (frontmatter patches, append, etc.).
- **`PATCH /api/v1/wikis/:owner/:slug/pages/*path {new_path: "..."}`** — rename/move. Performs git-mv-equivalent via plumbing, scans all pages for `[[old-title]]` / `[[old/path]]` wikilinks and rewrites them to the new path, commits everything atomically with message `Rename <old> -> <new>`. Git's rename detection preserves blame continuity. MCP tool: `move_page(old_path, new_path)`.
- `DELETE /api/v1/wikis/:owner/:slug/pages/*path` — remove.

## Wiki REST API (v1)

- `POST /api/v1/wikis` — create a wiki. Body: `{slug, title?, description?}`. Returns `201 {id, owner, slug, title, clone_url, web_url}`. Initializes bare repo with Karpathy skeleton.
- `GET /api/v1/wikis/:owner/:slug` — wiki metadata (title, description, star_count, fork_count, page_count, created_at, updated_at).
- `PATCH /api/v1/wikis/:owner/:slug` — update wiki metadata (title, description).
- `DELETE /api/v1/wikis/:owner/:slug` — delete wiki and both repos. Requires owner auth.
- `POST /api/v1/wikis/:owner/:slug/fork` — fork a wiki into caller's namespace.
- `POST /api/v1/wikis/:owner/:slug/star` / `DELETE /api/v1/wikis/:owner/:slug/star` — star/unstar.

## Search API (v1)

- `GET /api/v1/search?q=<query>&scope=<global|wiki>&wiki=<owner/slug>&tag=<name>&limit=20&offset=0` — cross-wiki full-text search. Returns `{results: [{wiki, page, title, excerpt, visibility, tags, score}], total}`. Scoped to what the authenticated user can see. `scope=wiki` + `wiki` param restricts to a single wiki.

## Error response convention

Every error response returns a JSON body: `{"error": "forbidden", "message": "You need edit access to this page"}`. Keep it simple for v1. **v1.5:** add structured `suggested_actions[]` array (fork, request_access) so agents can offer recovery menus.

---

## Ingestion (v1 only)

- **`git push`** — user has wiki locally, adds wikihub remote, pushes. Post-receive parses and syncs to Postgres.
- **Folder / zip upload** — web drag-drop. Server unpacks, commits with "Initial import from upload" message, runs through the same post-receive path.
- **Scaffold a blank wiki** — "Create wiki" button seeds the three-layer Karpathy skeleton (`schema.md`, `index.md`, `log.md`, `raw/`, `wiki/`, `.wikihub/acl` with private-default header) and one initial commit.

**v2 deferred:** paste-unstructured-text -> LLM, URL/repo connect, omni-convert (PDF/DOCX/txt/video -> markdown), HTML/Google Sites importer, SFTP (session-batched commits + SSH key endpoint — the encrypted, usable version of FTP; plain FTP killed).

---

## Social layer (v1)

- **Fork** — server-side `git clone --bare` under caller's namespace, plus a matching regen of the public mirror. Copies Page rows into Postgres, **resets visibility to `private` (the repo-default `* private` line)** — the forker explicitly republishes if they want it public. Sets `forked_from_id`. Free from the bare-repo model.
- **Star** — single counter and row. Standard.
- **Suggested edits — planned for v2** (confirmed 2026-04-10). Fork is valuable for v1; the "suggest edits back upstream" PR-like mechanic (diff format, list UI, accept/reject, cherry-pick) moves to v2 alongside inline diff UI. Unlike other v2 deferrals, this one has explicit intent to build — plan the data model (source_wiki, source_commit, target_wiki, diff) now so the fork schema is forward-compatible.
- **ZIP download** — `GET /@alice/wiki.zip` returns a zip of the wiki's working tree. Flask dispatches by auth: owner gets the authoritative repo's tree; non-owners get the public mirror's tree. Server implementation: `git archive --format=zip HEAD` streaming.
- **People browsing + `/explore`** — mixed discovery surface (updated 2026-04-10):
  - **`/explore` includes people AND wikis.** The top of the page shows a people strip/grid so identity is browseable, not just repos. This is where ListHub's "people are a first-class browse object" idea carries over.
  - **Dedicated `/people` index.** `/people` is the complete people directory; `/explore` is the blended overview. Nav exposes both `Explore` and `People`.
  - **Editorial picks** — admin-curated featured wikis from the official `/@wikihub` wiki. Hand-picked quality content, especially important for cold start when there are zero stars.
  - **Algorithmic popular** — most-starred wikis, automatic. Provides social proof once the platform has enough activity.
  - **Recent** — newest public wikis, chronological.
  - **Person page shape.** Clicking a person goes to `/@username`, which is the user's personal wiki first, then their project wikis underneath. Unlike ListHub, the person page is not a separate social profile shell; it is the personal wiki itself.
  The official wikihub wiki IS the curated showcase — featured entries are pages in `/@wikihub/wiki` with links to the highlighted user wikis. This solves cold start (editorial content exists from day one), provides editorial control (not just a popularity contest), and means the featured section is itself a wiki page that's editable, versionable, and dog-foods the platform.
- **Star / fork count, profiles, `/@user` pages** ship in v1.
- **Wiki history viewer (added 2026-04-10):**
  - **`/@user/wiki/history`** — web page showing the git commit log for the wiki. Paginated list of commits: author, date, message, changed files. Styled like GitHub's commit list. Auth-dispatched: owner sees full history from the authoritative repo; non-owners see public mirror history (linearized, less useful — but still shows the latest snapshot commit).
  - **`/@user/wiki/path/to/page/history`** — per-page history. Filters the commit log to commits that touched this file. Shows diffs between versions.
  - **`GET /api/v1/wikis/:owner/:slug/history?path=&limit=20&offset=0`** — REST endpoint returning commit log as JSON: `{commits: [{sha, author, date, message, files_changed}], total}`. Optional `path` param scopes to a single file. This is the `commit_log` MCP tool's backing endpoint.
  - Implementation: `git log --format=json` (or parsed `git log --pretty=format:...`) on the appropriate bare repo. Streaming for large histories.

---

## Search

- **Cmd+K omnisearch cross-wiki from day one.** Scoped to what the viewer can see. Postgres `tsvector` + GIN index. Global and per-wiki scope options.
- **Search-or-create fallback.** When cross-wiki search returns zero hits, Cmd+K offers "Create `<query>`" as the first action — creates a new page in the currently-focused wiki with the query as the title and drops the user into the editor. Matches Obsidian and Notion behavior.
- **Tag filters.** Cmd+K accepts `tag:<name>` prefix to filter by tag. Tags come from frontmatter's `tags:` field (Obsidian-compatible).
- **Tag index pages — restored to v1** (2026-04-10). `/@user/wiki/tag/:name` renders a list of every page in the wiki with that tag. Tag counts shown on wiki landing page. Tag search via Cmd+K `tag:<name>` prefix also stays.

---

## Rendering behavior (v1)

- **Content negotiation** on every page URL (see Agent surfaces above).
- **Wikilinks** `[[Page]]` and `[[path/to/page]]` resolved via the custom markdown-it plugin. Unresolved wikilinks render as red-dashed with a "create" affordance for users with edit permission.
- **Cross-wiki wikilinks (added 2026-04-10).** `[[/@user/wiki-slug/page]]` resolves across wikis at the platform layer. Syntax: any wikilink target starting with `/@` is treated as an absolute cross-wiki reference. Rendered as a normal link to that URL — no git submodules, no repo embedding. The personal wiki can link to project wikis and vice versa. Resolver checks target page existence across wikis (same DB query, different wiki_id). Unresolved cross-wiki links render as red-dashed like intra-wiki broken links. **Not submodules** — submodules add git complexity (pinned versions, nested auth, agent footguns) for no user benefit. Cross-wiki linking is a platform-layer feature, not a git-layer feature.
- **Inline images**: standard markdown `![alt](path.png)` AND Obsidian embed syntax `![[image.png]]`, with optional width specifier `![[image.png|300]]` -> 300px. Image paths resolve relative to the page. Covered formats: png, jpg, jpeg, gif, webp, svg, avif.
- **Folder page layout (updated 2026-04-10).** When browsing to `/@user/wiki/folder/`:
  1. **Breadcrumb** — `wiki / brainstorms /` with each segment linked.
  2. **index.md content** rendered first (if `index.md` exists, or `README.md` as fallback). This is the folder's "page" — primary content, Notion-style.
  3. **Contents listing** below the rendered content — table/list of files and subfolders in this folder: name, visibility badge, last modified. Subfolders show a folder icon and link to their own folder page. Files link to their reader view.
  4. **No index.md?** Just show the contents listing (GitHub-style auto-generated directory view).
  Visibility cascades from the folder's ACL glob unless the index file's own frontmatter overrides.
- **Sidebar folder behavior (Notion-style, added 2026-04-10).** The sidebar is an expandable tree, not a flat list:
  - **Toggle arrow** on folders → expand/collapse inline, showing children indented underneath. No page navigation on toggle.
  - **Click the folder name** (not the arrow) → navigate to the folder page (`/@user/wiki/folder/`, renders index.md or auto-listing).
  - **Click a page** inside the expanded tree → navigate to that page.
  - Expand/collapse state persists across navigation (stored in `localStorage`).
  - Folders with no visible children (all private, viewer has no access) are hidden from the tree.
  - Deep nesting supported — the tree is recursive. Indent with consistent spacing per level.
  - **Universal creation affordances.** Wherever the owner sees the sidebar tree (reader view, folder view, personal wiki/profile view), the same creation actions appear underneath it: `New page` and `New folder`.
- **Folder creation UX (added 2026-04-10).** There is no separate folder model in Postgres. Creating a folder means creating `<folder>/index.md` through a small web form:
  - The form asks for **folder path** plus **visibility for the folder index page**.
  - On submit, wikihub scaffolds `<folder>/index.md` with frontmatter + heading and redirects into the editor.
  - This keeps folders git-native, makes the sidebar universal, and avoids inventing a second content type whose only job would be to proxy to `index.md`.
- **External links** open in new tab (`target="_blank" rel="noopener noreferrer"`). Internal wikilinks stay in the current tab.
- **KaTeX** for `$inline$` and `$$display$$` math. **highlight.js** for fenced code blocks. **markdown-it-footnote** for `[^1]` footnotes.
- **`<!-- private -->...<!-- /private -->` bands** stripped from public-mirror serving and `Accept: text/markdown` responses, but kept in the authoritative repo. Visible UI warning on pages that contain private bands. **Implementation requirement (validated 2026-04-09):** stripping MUST use markdown-it's parsed AST (token stream), NOT raw text regex. Raw regex incorrectly strips markers inside fenced code blocks, frontmatter, and nested HTML comments. AST-based stripping is safe because fenced code content never generates `html_block` tokens. Additional rules: case-insensitive matching (`/<!--\s*private\s*-->/i`), unclosed bands fail closed (everything after the opening marker is private), frontmatter block is excluded from scanning.

---

## Web editor (v1)

- Markdown editor with `[[wikilink]]` autocomplete.
- Live preview with the full reader pipeline (KaTeX, code highlighting, footnotes).
- Visibility toggles that write to frontmatter or the ACL file as appropriate.
- **New-page form pre-fills inherited visibility.** When creating a page under an ACL-governed folder (e.g., `wiki/entities/`), the editor's visibility field defaults to whatever `.wikihub/acl` would resolve for that path (e.g., `public` if `wiki/** public` matches). User can override. This makes the cascade visible and predictable rather than silently applied after save.
- **Concurrent edits: true last-write-wins, no optimistic locking** (locked 2026-04-09). If two REST `PUT` requests arrive simultaneously, the second silently overwrites the first. No `If-Match` header, no 409 Conflict. Git history preserves both commits — revert is the recovery path. Add optimistic locking in v2 if users report data loss.
- Collaborative editing (CRDT / OT / realtime) is **v2+, not v1**. Not biased toward realtime; async-first.

---

## Listhub code to port verbatim

From listhub, battle-tested:

- `git_backend.py` — git Smart HTTP (clone/push/receive-pack/upload-pack), repo init, hook install. Generalize paths for multi-wiki.
- `git_sync.py` — DB->git plumbing using `GIT_INDEX_FILE` + `hash-object` + `update-index --cacheinfo` + `write-tree` + `commit-tree` + `update-ref`. No working tree. Does NOT fire hooks (critical invariant — prevents two-way-sync loop).
- `hooks/post-receive` — parses pushed .md files, extracts frontmatter, calls admin API to upsert Page rows.

Do a 30-minute spike to confirm no hidden coupling to listhub's flat item schema before committing to the port.

---

## Beads (Yegge model) — resolved

The "Yegge model" is **beads** (github.com/steveyegge/beads), Steve Yegge's git-friendly AI-agent issue tracker, already installed in listhub's `.beads/` dir. Architecture: SQLite (WAL) as operational store + `.beads/issues.jsonl` as git-tracked export + git hooks for bidirectional sync.

Beads's source of truth is SQLite (structured); wikihub's is git (authored markdown) — opposite directions, deliberate.

Lessons worth porting into wikihub:
- Line-oriented formats for anything under `.wikihub/` that might merge-conflict (CODEOWNERS-pattern ACL already aligned).
- Hash-based IDs (already aligned via nanoid).
- **`.wikihub/events.jsonl` audit export — restored to v1** (2026-04-10). Append-only JSONL log of content-and-permission mutations (page create/update/delete, ACL changes, visibility flips, forks). NOT stars/views/reactions (those are Postgres counters). Implementation cost is ~30 lines (one `open(path, 'a')` call per mutation). Events inherit privacy of their resource. Having a git-tracked audit trail from day one means never retroactively reconstructing "who changed what."

---

## Migration targets (dogfood)

Queued for week-one launch to validate before any marketing:

- admitsphere (currently elsewhere)
- RSI wiki (currently elsewhere)
- Systematic altruism wiki (currently Google Sites)
- jacobcole.net/labs (uses git-crypt pattern)
- Jacob's CRM (markdown-based personal CRM)

Each gets a one-off Python script to scrape/transform and produce a zip we upload. Scripts are throwaway. No general-purpose importers in v1.

---

## v2 / v3 deferred list

**v2:**
- **Split folder into wiki (added 2026-04-10).** Promote a folder from the personal wiki (or any wiki) into its own standalone project wiki. One-time operation: `git subtree split` or equivalent plumbing extracts the folder's history into a new bare repo pair, creates the Wiki row in Postgres, reindexes pages, and optionally replaces the original folder with a cross-wiki link (`[[/@user/new-wiki/index]]`). Use case: a user's personal wiki grows a subtree (`chronicpain/`, `ml-notes/`) that deserves its own ACL, star/fork surface, and clone URL. The inverse ("merge wiki into folder") is deliberately NOT supported in v2 — it's a one-way promotion.
- **ListHub data migration (added 2026-04-10).** One-time import of ListHub's SQLite items (7K+ items, subfolder structure) into WikiHub. Each ListHub user's items become pages in their personal wiki. ListHub's `file_path` (already has subfolder structure like `startx/lists/file.md`) maps directly to WikiHub page paths. ListHub visibility (`public`/`private`/`unlisted`) maps to WikiHub frontmatter visibility. Timestamps, slugs, and content preserved. Natural project folders can optionally be split into project wikis post-import.
- Paste-unstructured-text -> LLM generates wiki (server-hosted coding agent with platform key; leaning headless Claude Code over raw Anthropic tool-use)
- URL / repo connect (import from public git repos)
- Omni-convert upload (PDF/DOCX/txt/video -> markdown via pandoc/tika/whisper/OCR)
- HTML / Google Sites importer
- Comments on pages and wikis (comments role already in ACL vocabulary)
- D3 force-graph render of wikilinks
- Inline diff UI for suggested edits
- Friends-list / group creation UI (grant syntax is v1, the management UI is v2)
- Real-time collaborative editing
- **Anti-abuse machinery** — highlighted v2 priorities from architecture validation: (1) IP rate limit on writes (10/min/IP, infra not moderation), (2) panic button toggle to instantly disable anonymous writes per wiki, (3) "revert last N anonymous edits" button in web UI. Remaining: moderation view, bulk revert, under-attack mode, notifications, quarantine, PoW, CAPTCHA, body caps, honeypot, owner notifications
- Server-hosted cloud agent (talk-to-an-agent-we-host via web UI, per-user folder sandbox via UNIX permissions)
- ~~Featured curation admin surface~~ — **moved to v1** (2026-04-10). Three-layer curation: editorial picks from official wiki + most-starred + recent.
- **Landing page live background** — featured/popular wiki cards slowly drifting, orbiting, or scrolling behind the hero section as a living backdrop. Pure CSS animation (transform + keyframes, no JS). Cards are real data pulled from the featured wikis endpoint. Shows social proof and makes the page feel alive. Requires enough public wikis to look good — ship after dogfood migrations populate the platform. V1 landing page ships with a clean empty background; this replaces it once there's content worth showing.
- SFTP upload path (session-batched commits, SSH key endpoint, `sshfs`/`rsync` compatible)
- Link-share token expiry (v1 link tokens are permanent; add expiry syntax or Postgres-side override in v2)
- **Private page enumeration hardening** — return 404 instead of 403 for unauthorized private page access, so attackers can't discover page names by probing URLs. Low priority; current 403 is correct behavior, just reveals existence.
- Optimistic locking on REST API (If-Match / ETag / 409 Conflict)
- **Twitter auth / tweet-to-verify** — signup requires tweeting a specific post (e.g., "I just created my wiki on @wikihub") and pasting the tweet URL to verify. Viral distribution mechanic + lightweight sybil resistance. Agent-compatible: agent tweets via user's Twitter API key or user pastes the link. Similar pattern to Farcaster Frames and various crypto airdrops.
- **MCP server: expose wiki content for AI agent queries (added 2026-04-10).** Standalone MCP server (`mcp_server.py`) that exposes wiki tools (`list_pages`, `read_page`, `search_wiki`, `get_wiki_info`, `read_llms_txt`) over stdio. Any Claude Code or AI agent can connect and read/search wiki pages natively — the LLM does its own reasoning, no embeddings needed. Works against any wikihub instance (local or remote). Inspired by Farzapedia's `/wiki-query` (which just reads files) and gbrain's MCP layer. P0.
- **Recently updated feed (added 2026-04-10).** Show last N modified pages on wiki homepage and user profiles. Data from git history. Like Farzapedia's "Recently updated" sidebar. P1.
- **Auto-generated table of contents (added 2026-04-10).** TOC from `##` headings on rendered pages. markdown-it-py TOC plugin or custom heading extraction. Bonus: per-section `[edit]` links like Wikipedia/Farzapedia. P1.
- **Backlinks / "what links here" (added 2026-04-10).** Surface pages that link to the current page in the sidebar. Data already exists in `wikilinks` table — query and render. P1.
- **Wiki homepage with categories & stats (added 2026-04-10).** Rich wiki landing from frontmatter tags: group pages by `type:` tag, article/category counts, featured article section, browse-by-category grid. Like Farzapedia's "417 articles across 41 categories" homepage.
- **Infobox rendering from frontmatter (added 2026-04-10).** Render certain frontmatter keys as a Wikipedia-style infobox sidebar (photo, "known for", "current role"). Pure template work on existing frontmatter parsing. Inspired by Farzapedia person pages.
- **Obsidian / markdown repo import (added 2026-04-10).** Discover and import Obsidian vaults and plain markdown repos. Convert `[[wikilinks]]` (already supported in renderer), preserve folder structure. Extends existing zip upload. Inspired by gbrain's import pipeline.
- **Article type system (added 2026-04-10).** `type:` frontmatter key (person, book, place, artifact, assessment) with per-type rendering — person gets infobox, book gets cover, place gets map link. Builds on infobox feature. Inspired by Farzapedia's category system.

**v3:**
- git-crypt escape hatch for encrypted personal wikis (see jacobcole.net/labs pattern)
- Multi-human-one-agent session semantics
- Full Wikipedia-grade moderation tooling (edit filters, trust levels, auto-revert bots)
- Org / multi-tenant surface (Anthropic intranet target — principal table shape is ready)

---

## Things proceeding unless vetoed (updated 2026-04-09)

- **`<!-- private -->` visible UI warning** on pages containing private bands.
- **SFTP deferred to v2.** Plain FTP killed; SFTP is the "upload without git" path.
- **Reader stack:** markdown-it + markdown-it-footnote + markdown-it-katex + highlight.js + custom wikilink plugin, Inter / IBM Plex Mono, dark theme, CSS variables.
- **Don't fork Quartz**, use as style reference.
- **Ship all cheap agent manifests:** AGENTS.md, llms.txt, llms-full.txt, both .well-known/mcp shapes, wikihub.json bootstrap, Accept: text/markdown content negotiation with Vary and Link headers.
- **Write commit authors** use `anonymous <anon@wikihub>` for anonymous writes.
- **Deferred from earlier "proceeding" list:** principal abstraction (just use `user_id` FKs in v1, add polymorphic principals when groups/link-tokens/orgs ship), commenter role (deferred with comments), JSON-LD (deferred), per-key scopes (deferred).

---

## Open questions

All prior open questions resolved as of 2026-04-10. No open spec questions remain.

### Resolved 2026-04-10

- ~~Quartz~~ → **locked as style reference only.** Don't fork. Use for renderer aesthetic choices.
- ~~Server deployment~~ → **Lightsail as default**, same box as ListHub if capacity allows. Decide final topology at deploy time. Domain is `wikihub.md` (locked).
- ~~Signup rate-limit-per-IP~~ → **stays.** It's infra (bot flood protection), not content moderation. ~5 lines of middleware.

### Resolved in 2026-04-09 session (no longer open)

- ~~Auth providers~~ → Google OAuth + local. Noos dropped.
- ~~Featured curation~~ → ~~most stars wins automatically. Admin override v2.~~ **Updated 2026-04-10:** three-layer curation in v1: editorial picks (official wiki) + most-starred + recent. Solves cold start, provides editorial control.
- ~~Concurrent-edit resolution~~ → true last-write-wins, no optimistic locking v1.
- ~~Milkdown vs simpler~~ → Milkdown, ported from listhub with wikilink plugin added.
- ~~Content in DB or git-only~~ → git-only for public content. DB has metadata + search index only.
- ~~ACL file vs Postgres for link-shares~~ → ACL file for all grants. No expiry in v1.
- ~~FTP/SFTP~~ → SFTP deferred to v2. Plain FTP killed.

### Resolved in 2026-04-10 session

- ~~Personal wiki~~ → every user gets a default personal wiki on signup, slug = username, can't be deleted, default private ACL, `/@username` resolves to it.
- ~~Official platform wiki~~ → `/@wikihub` wiki serves as docs, curated showcase, and dogfood exemplar.
- ~~events.jsonl~~ → restored to v1. Append-only audit of content+permission mutations, ~30 lines.
- ~~JSON-LD~~ → restored to v1. ~15 lines of Jinja template, zero cost.
- ~~Suggested edits~~ → confirmed planned for v2 (not just "deferred" — actively intended). Plan data model now.
- ~~API key management~~ → hashed in DB (GitHub model), but frictionless generation: settings page with generate/copy/revoke UI, `POST /auth/token` for agents with password, `POST /keys` for agents with existing key.
- ~~Web auth~~ → three login methods (Google OAuth, username+password, API key paste), settings page at `/settings`, logout link in nav.
- ~~Folder view route~~ → spec'd (Quartz-style index.md in subfolders), has mockup + template, route not yet wired. Implementation gap, not spec gap.
- ~~Tag index pages~~ → restored to v1. `/@user/wiki/tag/:name` renders tagged page list. Tag counts on wiki landing.
- ~~Landing page framing~~ → "Drop your files, get a URL" confirmed. Publishing = acquisition hook, agent API = retention hook.

---

## Philosophy

- **Infrastructure, not app.** wikihub is the memory layer + git host. Agents are clients. The site stands alone and serves any LLM/agent.
- **Separate repos, seamless integration.** A coding agent should need <50 lines to fully operate a wiki.
- **YAGNI.** Ship v1 without anti-abuse machinery, without comments, without a collaborative-editing stack. Iterate reactively.
- **API for writes, git pull for reads.** Same split as listhub.
- **Read liberally, write conservatively** (Postel's Law for frontmatter compatibility).
- **Trust the agent era on velocity.** No time estimates in weeks/months for coding-agent work.

---

## Developer experience / testing — added 2026-04-10

### "For Agents" nav link — added 2026-04-10

The top nav bar includes a prominent **"For Agents"** link (amber, monospace, always visible to all users) that points to `/agents`. This makes the agent-first nature of wikihub immediately obvious to every visitor. The `/agents` page documents all agent integration surfaces: MCP endpoint, REST API, content negotiation, llms.txt, git clone, and one-call registration. This link appears on **all pages including the landing page** — nav is consistent site-wide.

### Explore = people + wikis (no separate People page) — added 2026-04-10

The `/explore` page shows people first, then wikis — there is no separate `/people` nav link. One page, two sections. The Explore page already had a people section with an "All people" link; making `/people` a separate top-level nav item was redundant clutter. The nav is now: **Explore | For Agents | [user/auth]**. For authenticated users, the user/auth area is a standard account trigger (avatar + handle) that opens a small menu containing at least **View profile, Settings, Logout** rather than exposing Settings/Logout as always-on top-level links.

### Official @wikihub account — added 2026-04-10

The `@wikihub` account is the platform's official identity. Name stays as `@wikihub` — clean, obvious, matches the platform (like `@github` on GitHub). The `@wikihub/wiki` wiki is the canonical docs/showcase site. Content should include: getting started guide, API reference, example wiki structures, curated showcase of good wikis, and changelog. The account is auto-created on app startup (`ensure_official_wiki()`).

### Test login buttons

The login page shows one-click "Login as alice" / "Login as bob" buttons when the `TESTING_LOGIN=1` environment variable is set. These buttons:

- POST to `/auth/test-login/<username>`, which auto-creates the account if it doesn't exist and logs in immediately.
- Are completely hidden when `TESTING_LOGIN` is not set — zero surface area in production.
- Enable fast manual QA of multi-user flows (permissions, starring, forking) without juggling passwords or API keys.

---

## Open design questions — added 2026-04-10

### P0: Editor and new-page web experience broken (wikihub-18e)

The entire manual page editing and creation flow via the web UI needs a full QA pass and fix. This blocks dogfooding — without a working editor, the site is API-only. Includes: new page form, Milkdown editor loading, save flow, visibility setting, raw textarea fallback.

### Cross-wiki references in sidebar (wikihub-v02)

Allow wikis to reference or alias other wikis in their sidebar navigation. The @wikihub community wiki could have a "Featured" folder that links to @jacobcole/rsi-wiki, @jacobcole/admitsphere, etc. as navigable sidebar entries. Like symlinks — you don't own the content but it appears in your wiki's nav. This would make the community index wiki a real navigational hub, not just a page with links. Possible mechanisms: `.wikihub/references` file, frontmatter field, or virtual folder entries.

### Quartz-style folder index pages (wikihub-jjt)

When visiting a wiki root or folder with no index.md, render a Quartz-style overview with page title + excerpt cards instead of a plain file listing. Could be opt-in via `layout: quartz-index` frontmatter or the default empty-folder behavior. Would make wikis more browsable without requiring authors to manually maintain index pages.

### Community curation model (wikihub-dz5, wikihub-52z)

The @wikihub wiki is the editorial/curated index ("awesome list of awesome lists"). The `/explore` page is the algorithmic view. Key questions: Should `/explore` have its own editorial section, or just link to the community wiki? How do wikis get featured — admin flag, suggested-edit PR, or just a page edit in the community wiki? Current implementation: editorial picks on explore is hardcoded to show @wikihub's personal wiki. Community curation is just markdown pages with links. No special machinery yet.

### Bio/profile frontmatter (wikihub-722)

Profile-aware frontmatter (`bio:`, `avatar:`, `links:`) in personal wiki `index.md` — Claude-proposed design. Works but needs human review on whether bio belongs in frontmatter, DB fields with settings UI, or just the markdown body. See ticket for full options.

### Explore page ordering (wikihub-gw0)

Sort accounts and wikis by recent activity instead of creation order. Currently shows Editorial Picks → all Wikis → People, but wikis are ordered by updated_at. Could factor in stars, page count, or recency of edits.

---

## Research docs to consult

- `research/wikihub-spec-work-2026-04-08/agent-first-web-brief-2026-04.md` — authoritative source for agent-first web standards (WebMCP, llms.txt, .well-known manifests, content negotiation, agent-native auth, agent identity).
- `research/llm-wiki-research.md` — five-sense taxonomy of "LLM wiki."
- `research/llm-wikis-catalog.md` — ~85 concrete wikis across the five senses.

---

*Final consolidated spec. Supersedes `wikihub-spec-state-from-side-session-2026-04-08.md` and `wikihub-post-fork-delta-2026-04-08.md`.*
