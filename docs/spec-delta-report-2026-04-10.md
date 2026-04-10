# Spec Delta Report: Original (Harrison) → Current (2026-04-10)

**184 lines added, 20 lines modified.** No deletions of Harrison's decisions — everything is additive or clarifying.

---

## New Sections Added (not in original)

| Section | Summary | Who added |
|---------|---------|-----------|
| **Personal wiki** | Every user gets auto-provisioned personal wiki on signup. `/@username` = profile. Can't delete. Default private. | Claude (2026-04-10 03:45 PT) |
| **Official @wikihub wiki** | Platform docs/showcase wiki. Auto-created on startup. | Claude (2026-04-10 03:45 PT) |
| **Wiki REST API (v1)** | Full CRUD endpoints for wikis (create, read, update, delete, fork, star) | Harrison's agent (Moonflower2022, 2026-04-09 20:34 PT) |
| **Search API (v1)** | Cross-wiki full-text search endpoint spec | Harrison's agent (2026-04-09 20:34 PT) |
| **DX / Testing** | "For Agents" nav link, Explore = people + wikis, test login buttons, @wikihub naming | Claude (2026-04-10 03:45-04:22 PT) |
| **Open design questions** | 5 flagged questions with beads tickets: sidebar refs, Quartz indexes, curation model, bio frontmatter, explore ordering | Claude (2026-04-10 ~05:30 PT) |

---

## Decisions Changed from Original

| Topic | Harrison's Original | Current | Who changed |
|-------|-------------------|---------|-------------|
| **Private pages storage** | "Private pages never enter git. Live in Postgres only." | "All pages live in authoritative git repo, including private. Two-repo model handles access." | Harrison's agent (2026-04-10 via spec update) |
| **JSON-LD** | Deferred to v2 | **Restored to v1** (~15 lines Jinja) | Harrison's agent (2026-04-09 20:34 PT) |
| **Tag index pages** | Deferred to v2 | **Restored to v1** | Harrison's agent (2026-04-09 20:34 PT) |
| **events.jsonl audit** | Cut from v1 | **Restored to v1** (~30 lines) | Harrison's agent (2026-04-09 20:34 PT) |
| **Featured curation** | "Most stars wins automatically, admin override v2" | "Three-layer: editorial picks + most-starred + recent. Editorial = @wikihub wiki pages." | Claude (2026-04-10) |
| **Suggested edits** | "Deferred to v2" | "Planned for v2 with explicit intent. Plan data model now." | Harrison's agent (2026-04-09 20:34 PT) |
| **AGENTS.md** | Brief description | Full 9-point structured content spec with curl examples, plain-English agent instructions | Harrison's agent (2026-04-09 20:34 PT) |

---

## Expanded (not changed, but significantly more detail)

- **Flask auth dispatch** — spec now documents how ALL web reads (wiki index, page view, sidebar, ZIP) are dispatched by owner-vs-public
- **Unlisted visibility** — clarified: exists in public mirror (accessible by URL) but excluded from sidebar/search/explore
- **API key management** — expanded from brief mention to full spec: settings page, `/auth/token`, `/keys` CRUD, magic sign-in links
- **Folder page layout** — expanded from one sentence to detailed Notion-style spec (breadcrumb, index.md + contents listing, sidebar tree)
- **Cross-wiki wikilinks** — new: `[[/@user/wiki/page]]` syntax specified
- **Wiki history viewer** — new: web + API endpoints for commit history

---

## Things Built But NOT Fully in Spec (implied by actions)

These were implemented during this session but the spec may not fully capture them:

1. **Sidebar navigation model** — personal wiki sidebar heading links to `/@username` (profile), not wiki root. Non-personal wikis link to wiki root. This UX decision isn't spec'd.
2. **Explore page ordering** — Editorial Picks → Wikis (all) → People. The spec still mentions "popular + recent" in some places.
3. **Content import workflow** — we imported RSI Wiki, AdmitSphere, ListHub content. No spec for "how to import external content" as a user feature.
4. **Deployment architecture** — separate Lightsail instance for collaborator isolation. Not in spec (infrastructure, not product).
5. **Wiki title = display name for personal wikis** — personal wiki `title` field uses `display_name`, not "username's wiki". Not spec'd.
6. **Edit profile button** — profile page shows "Edit profile" button for owner. Not spec'd.

---

## Open Questions Flagged (need your input)

1. **Bio frontmatter** (`wikihub-722`) — Should bio/avatar/links be in frontmatter, DB fields, or just the markdown body?
2. **Editorial picks model** (`wikihub-52z`) — Hardcoded to @wikihub personal wiki. Should there be a real curation system?
3. **Community index** (`wikihub-dz5`) — @wikihub wiki as "awesome list of awesome lists" — is this the right model?
4. **Cross-wiki sidebar refs** (`wikihub-v02`) — Symlink-like references to other wikis in sidebar
5. **Quartz-style indexes** (`wikihub-jjt`) — Rich folder overview pages with excerpts

---

*Generated 2026-04-10 from `git diff 8ddceb5:wikihub-spec-final-2026-04-08.md HEAD:wikihub-spec-final-2026-04-08.md`*
