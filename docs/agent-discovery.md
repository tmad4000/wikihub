# agent discovery — research & plan

How do we make it so that a user pasting "set me up on wikihub.md" into a coding
agent results in the agent reliably landing on `/AGENTS.md` (the plain-markdown
onboarding recipe) instead of guessing or fumbling through HTML?

This doc captures the standards landscape as of **April 2026** and what wikihub
currently does / should do.

## the problem

Test run (2026-04-22) — fetch `https://wikihub.md/` and inspect what a
reading-agent would actually see:

- `<title>` is marketing-flavored: "wikihub — GitHub for LLM wikis". No agent
  signal.
- `<head>` has **no** `<link rel="alternate">` pointing at `/llms.txt` or
  `/AGENTS.md`.
- `<head>` has no `<meta name="ai-*">` either.
- Visible body links all say `/agents` (the HTML). Zero mentions of
  `/AGENTS.md` (the plain markdown).

So an agent has to either (a) guess `/AGENTS.md` exists, or (b) see the visible
"For Agents" link and click through to HTML and then re-extract.

The earlier ticket [wikihub-gvb1](closed) shipped a great `/AGENTS.md` with a
one-shot onboarding recipe — but the homepage doesn't tell agents to read it.

## the standards (April 2026)

| Mechanism | Status | Who uses it |
|-----------|--------|-------------|
| `/llms.txt` (Jeremy Howard, 2024) | Proposed. 844k+ sites. | Anthropic docs, Cloudflare, Stripe. No major AI provider officially parses it yet. |
| `/AGENTS.md` at root | Donated to Linux Foundation's Agentic AI Foundation, Dec 2025. | OpenAI Codex reads it. Web-root adoption growing. |
| `<link rel="alternate" type="text/markdown">` in `<head>` | Bona-fide HTML standard (same mechanism as RSS discovery since 2003). | Cloudflare, Vercel, Jekyll/Hugo plugins, TYPO3. |
| `Accept: text/markdown` content negotiation | HTTP standard. | Well-designed agents send it (Vercel's "agent-friendly pages" pattern). |
| HTTP `Link:` header (`rel="alternate"`) | RFC 8288. | Some crawlers check; less common than rel tags. |
| `/.well-known/agent-card.json` (A2A), `/.well-known/mcp/server-card.json` (MCP / SEP-1649) | Competing IETF drafts — 11 of them. | MCP servers, A2A ecosystem. |
| `agent-manifest.txt` (formerly agents.txt) | Proposed March 2026. | Early. |

**Key insight**: no single authoritative standard yet. Fragmentation is real.
The safe bet is to implement multiple overlapping signals so any reasonable
agent finds at least one.

## how LLMs actually discover things (the tricks)

1. **WebFetch-style extractors often drop `<head>`.** They convert HTML →
   markdown and keep the main content. So `<link rel>` alone is not enough —
   a visible one-liner at the top of the body catches extractors that drop head
   tags.

2. **Content negotiation is the highest-quality signal.** If a request to `/`
   arrives with `Accept: text/markdown`, a smart server returns AGENTS.md
   content directly. This is the cleanest possible UX: one URL, multiple
   formats.

3. **The URL itself is a hint.** Agents trained on recent data pattern-match:
   if your domain is `wikihub.md`, an agent seeing that domain may
   implicitly try `/AGENTS.md`. wikihub's `.md` domain already helps.

4. **Do NOT stuff the page `<title>`.** Putting "agents:" in the title
   harms humans more than it helps agents. The `<link rel>` + visible
   top-of-body line gets 95% of the win cleanly.

5. **HTTP `Link:` header is a cheap bonus.** Some crawlers check it before
   parsing the body.

6. **Training-data bias.** Models trained after llms.txt gained traction
   (2025+) pattern-match on `/llms.txt`. Older models may not. Don't rely
   on this — use it as one of many signals.

## wikihub's current state (as of 2026-04-22)

- ✅ `/AGENTS.md` exists (plain markdown, includes "onboarding in one shot" recipe — shipped wikihub-gvb1)
- ✅ `/llms.txt` exists (plain-text index, links to `/agents` HTML)
- ✅ `/llms-full.txt` exists (expanded index with all public pages)
- ✅ `/.well-known/mcp/server-card.json` exists (MCP discovery)
- ✅ `/.well-known/wikihub.json` exists (bootstrap manifest)
- ✅ `/agents` HTML page exists
- ❌ **No `<link rel="alternate">` tags in page `<head>`.**
- ❌ **No visible top-of-body agent banner on landing page.**
- ❌ **No content negotiation on `/` for `Accept: text/markdown`.**
- ❌ **No HTTP `Link:` header on landing route.**
- ❌ **`/llms.txt` Documentation section points at `/agents` HTML, not `/AGENTS.md` markdown.**
- ❌ **Visible "paste this prompt" copy on landing says `wikihub.md/agents`, not `wikihub.md/AGENTS.md`.**

## recommended stack (bang-for-buck order)

1. **Head-level signals** — `<link rel="alternate" type="text/markdown" href="/AGENTS.md">` and `<link rel="alternate" type="text/plain" href="/llms.txt">` in `base.html` (propagates to every page). Also emit HTTP `Link:` header on `/`.
2. **Visible body signals** — one-line banner at the top of `landing.html`: "🤖 Agents: read [/AGENTS.md](/AGENTS.md) for one-shot setup." Flip the paste-prompt example from `wikihub.md/agents` to `wikihub.md/AGENTS.md`.
3. **Promote `/AGENTS.md` in `/llms.txt`** — move it to the top of the Documentation block, above `/agents`.
4. **Content negotiation on `/`** — `Accept: text/markdown` on `/` returns `/AGENTS.md` content directly. Vercel-pattern. Clean but a little more work (need to wire Accept-based dispatch on the landing route).

Items 1, 2, 3 are quick wins (~15 min each). Item 4 is a heavier change.

## sources

- llms.txt spec: https://llmstxt.org/
- AGENTS.md: https://agents.md/
- AGENTS.md donated to Linux Foundation (Dec 2025): https://copymarkdown.com/agents-md-explained/
- Cloudflare — Introducing Markdown for Agents: https://blog.cloudflare.com/markdown-for-agents/
- Vercel — agent-friendly pages with content negotiation: https://vercel.com/blog/making-agent-friendly-pages-with-content-negotiation
- Serving Markdown for AI Agents (Jekyll): https://code.dblock.org/2026/01/15/serving-markdown-for-ai-agents.html
- MCP well-known discovery (2026): https://www.ekamoira.com/blog/mcp-server-discovery-implement-well-known-mcp-json-2026-guide
- State of agent-protocol standardization (11 IETF drafts): https://global-chat.io/experiments/ietf-expiry
- agent-manifest.txt proposal: https://dev.to/jaspervanveen/agentstxt-a-proposed-web-standard-for-ai-agents-20lb
