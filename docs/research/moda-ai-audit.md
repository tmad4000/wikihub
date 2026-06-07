# moda.ai audit

**Date:** 2026-06-07
**Ticket:** wikihub-djtl
**Time-box:** ~30 min
**Verdict:** Not relevant. No tickets filed.

## What is moda.ai?

`moda.ai` itself is a parked domain on Afternic (for sale). The product Jacob
likely meant is **moda.app** — an AI-native design tool. Think Canva or Figma
with a Cursor-style AI sidebar that builds and iterates designs directly on a
fully editable 2D vector canvas. Outputs: slides, social posts, documents,
carousels, websites, HTML docs (beta).

- Site: https://moda.app/
- Pricing: https://moda.app/pricing
- Product Hunt: https://www.producthunt.com/products/moda-2
- Engineering blog post about their agent stack:
  https://blog.langchain.com/how-moda-builds-production-grade-ai-design-agents-with-deep-agents/
- Target user: non-designers (marketers, founders, salespeople) who need
  on-brand visual assets fast
- Business model: freemium SaaS. Free ($0, 10 members, 1.5K AI credits) →
  Pro ($30/seat) → Ultra ($100/seat, SSO/SAML, orgs & teams, shared workspaces)
- Open source: no, closed commercial product
- Differentiator: AI agent operates on a real vector canvas (every element
  remains selectable/movable/editable) rather than generate-and-replace

## Relevance to wikihub

Wikihub is a markdown-wiki-as-a-service for human + agent collaboration on
text content. moda.app is a visual design tool for image/slide/social
assets. Different product, different canvas, different file format,
different collaboration shape. The overlap is essentially zero.

Specifically on Jacob's seed question — **password-protected share links** —
moda.app does not appear to offer this feature publicly. Their sharing model
is real-time multi-user collaboration on a workspace (members invited into a
team), not unauthenticated-viewer share-by-link. So there's no clever
implementation to borrow for wikihub-4z08.

## Features worth borrowing

None. The interesting things about moda.app (vector canvas, AI design agent,
brand kits) are all design-tool primitives that don't map to a markdown wiki.

Their **"AI agent edits a live canvas alongside the human"** UX *is* arguably
the analog of wikihub-qz5l (agent-collab editor for markdown), but wikihub
already has a clearer model in mind (agent edits markdown via API while human
edits in the browser). moda.app's implementation doesn't add anything wikihub
hasn't already considered.

## Recommended tickets

None.

## Note for wikihub-4z08

moda.app does NOT have a notable password-share-link UX to copy. Reference
implementations for that ticket should still be Notion (share-to-web with
password), Dropbox (link with password), and 1Password share links.

---

## 2026-06-07 update — partial correction (the previous verdict was wrong)

Jacob surfaced a LinkedIn post from Moda's CEO that flips the verdict on
relevance. The original audit treated moda.app as a "vector design tool" —
correct as far as the product surface I looked at, but it MISSED that the
same company ships a second product (or pivoted positioning) that is
**directly competitive with wikihub on its highest-leverage use case**.

### Source

- LinkedIn post by **Anvisha Pai** (CEO of Moda, ex-Dropbox PM, repeat
  Y Combinator–backed founder):
  https://www.linkedin.com/posts/anvisha_we-just-replaced-notion-with-html-docs-that-ugcPost-7467971913806483457-uf_D/
- Post date: 2026-06-03 (~4 days before this update)
- Same parent company / domain as moda.app

### Verbatim post text

> We just replaced Notion with HTML docs. That might sound crazy, but it
> completely changed our entire workflow.
>
> Every HTML doc we generated with Claude lived on someone's personal
> computer. We couldn't comment on them or share them with anyone else.
> We'd end up emailing files back and forth like it's 2005.
>
> So we built the infrastructure HTML docs never had: a collaboration
> layer. A Google Drive for HTML docs with a WYSIWYG editor, commenting,
> share links, and password protection. And it works through MCP. Just
> ask Claude to upload the doc to Moda, and it's live and shareable
> instantly.

### Why this matters

This is **almost exactly** the use case Jacob articulated for wikihub-xnan
(2026-06-07): "HTML pages are an interesting use case because it replaces
what people are using Notion for." Moda has shipped that exact thesis.

Direct mapping to wikihub tickets:

| Moda feature (from post) | wikihub equivalent / status |
|---|---|
| "A Google Drive for HTML docs" — store + share HTML files | **wikihub-xnan** (P1) — render uploaded HTML safely, Notion-mini-sites use case |
| WYSIWYG editor on HTML docs | **wikihub-qz5l** epic + **wikihub-w0s4** (Milkdown + Yjs + Hocuspocus) |
| Commenting on HTML docs | **wikihub-f4m9** (comments + open-for-comments) |
| Share links | already shipped (per-page visibility tiers: public/unlisted/public-edit) |
| **Password protection on share links** | **wikihub-4z08** (Notion-style) — Moda IS now a reference implementation to study |
| "Works through MCP. Ask Claude to upload the doc to Moda" | wikihub MCP server already deployed at https://mcp.wikihub.md/mcp (17 tools, ported from noos pattern) |

**Strategic implication**: Moda has validated that "HTML docs as the
collaboration unit, with comment/share/password + MCP" is a real product
with real customers. Wikihub's planned stack does the same thing but with
markdown as the primary canvas and HTML as an upload-and-publish layer
(wikihub-xnan). Either direction (markdown-primary with HTML upload, vs
HTML-primary like Moda) can win; what matters is shipping the collab layer
on top of one or the other, with MCP as the agent-onboarding seam.

### Concrete actions taken

1. Updated **wikihub-xnan** (HTML render-safe) — added reference to this
   audit and to Moda as a direct competitor / proof of demand.
2. Updated **wikihub-4z08** (password-protected share links) — added Moda
   to the reference-implementations list (now: Notion + Dropbox + 1Password
   + Moda).
3. Updated **wikihub-f4m9** (comments) — flagged Moda as a competitor that
   ships comments on HTML docs specifically; useful product reference for
   the "comments on a non-markdown content type" UX.
4. This audit file kept open as the canonical "what Moda is" reference;
   ticket **wikihub-djtl** stays closed (the moda.ai-domain confusion is
   genuinely irrelevant; the moda.app product is the one that matters).

### Limitations of this update

- I haven't signed up for Moda's HTML-doc product yet, so I can't speak
  to the actual UX (is the password-share a per-link form? a workspace
  setting? what's the lockout policy? what's the share-link URL shape?).
  Worth a one-session product walk-through if wikihub gets serious about
  the HTML-as-Notion-replacement direction.
- Anvisha's LinkedIn post doesn't link to a public demo or docs URL for
  the HTML product specifically — moda.app currently shows the
  vector-design product as primary. The HTML product may be in beta,
  invite-only, or a soft launch.

### One open question for Jacob

If we ship wikihub-xnan (HTML render safe) + wikihub-f4m9 (comments) +
wikihub-4z08 (password share) + a refresh of the /agents install page for
the existing MCP server, do we essentially have feature parity with
Moda's HTML-doc offering — at no extra engineering cost beyond what's
already planned? If yes, the strategic move is to **ship those four
tickets in a deliberate Moda-competitive bundle** rather than treat them
as independent backlog items.
