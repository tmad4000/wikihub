# wikihub — Philosophy

The living document for wikihub's core beliefs, vision, and why it exists.

---

## Humanity's Archive

The archive for humanity isn't one library with one voice. It's a constellation of individual knowledge bases — each one a person's understanding of their corner of the world — that can be traversed, forked, and cross-referenced.

Wikipedia chose consensus. One article per topic, edit wars to resolve disagreements. That works for encyclopedic facts but kills perspective, voice, and the kind of knowledge that comes from lived experience.

wikihub is the opposite. Every person gets their own archive. Karpathy's wiki about transformers is different from your wiki about transformers. Both are valuable. Neither replaces the other. The platform's job is to make the full constellation navigable.

### Why this matters now

The Karpathy moment — when the most respected ML researcher in the world chose to publish his knowledge as flat markdown in a git repo instead of a blog, book, or course — validated the format. Markdown files in a git repo IS the right shape for knowledge in the AI era. Not databases, not CMS, not Notion.

But GitHub is a code host, not a knowledge host. No rendering, no access control, no search, no social layer. There's a gap between "I want a Karpathy-style knowledge base" and "I can publish one." wikihub fills that gap.

### The agent angle is the moat

Anyone can build a wiki host. The `.wikihub/acl` + MCP + content negotiation + `AGENTS.md` onboarding surface means agents are first-class citizens. An agent can sign up, create a wiki, publish pages, read other wikis, and discover knowledge — all without a browser. That's what makes wikihub not just "another wiki platform" but the knowledge layer for the agent era.

## The Librarian

Inspired by Neal Stephenson's Snow Crash — an AI daemon with instant recall of the entire library, used as a conversational research partner. Not a search engine. A guide.

The Librarian is wikihub's AI agent. It has read access to every public wiki on the platform. You don't query it with keywords — you talk to it. "I'm trying to understand X" and it points you to the three wikis that cover it best, explains how their perspectives differ, finds connections between domains that individual authors can't see.

Attribution is built in. When the Librarian references your wiki to answer someone's question, you get visibility. Your knowledge has value because it's discoverable and citable. The stringer model from Snow Crash — contributors' work has economic value because the platform makes it findable.

The Librarian is explicitly software. A daemon, not a person. But a daemon with personality — helpful, patient, thorough. It's the one entity that has read everything on the platform.

## Core beliefs

- **Infrastructure, not app.** wikihub is the memory layer + git host. Agents are clients. The site stands alone and serves any LLM/agent.
- **Individual voice over consensus.** Every person's archive is theirs. No edit wars, no "neutral point of view" policy. Voice is a feature.
- **Separate repos, seamless integration.** A coding agent should need <50 lines to fully operate a wiki.
- **YAGNI.** Ship without anti-abuse machinery, without comments, without collaborative editing. Iterate reactively.
- **API for writes, git pull for reads.** Same split as listhub.
- **Read liberally, write conservatively.** Postel's Law for frontmatter compatibility.
- **Trust the agent era on velocity.** No time estimates for coding-agent work.
- **Attribution as currency.** When the Librarian cites your wiki, that's the reward. Make knowledge findable and citable.
- **The archive outlives the platform.** Every wiki is a git repo you can clone and take with you. If wikihub disappears, the knowledge survives.

---

*This document is the soul of the project. Update it when beliefs change or sharpen.*
