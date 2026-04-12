# wikihub — future brainstorming

Ideas and design directions captured during spec review sessions. Not committed to any version yet — this is the holding pen for things worth thinking about more.

---

## Karpathy-informed onboarding flow (2026-04-10)

The Karpathy LLM wiki signal: the most respected ML researcher chose flat markdown in a git repo over a blog, book, or course. This validated the format and created demand with no supply. wikihub fills the gap — but the onboarding should feel like "publish your knowledge" not "create an account on a platform."

### Current flow (generic SaaS)
Sign up → empty dashboard → "Create wiki" button → fill form → empty wiki → now what?

### Proposed flow (knowledge-first)

**Arrival:** See beautiful rendered wikis (official wiki + featured content) → "I want mine to look like that"

**Three entry points:**
1. **"Drop your files"** — drag a folder of .md files, get a wiki instantly. Account created behind the scenes if needed.
2. **"Start from a template"** — pick the Karpathy skeleton (schema.md, index.md, log.md, wiki/, raw/) or a blank wiki.
3. **"Connect your vault"** — `git remote add wikihub ...` + `git push` for Obsidian users.

**Immediate payoff:** Content is live and rendered within 60 seconds of arriving. Personal wiki exists, `index.md` is your profile. You're not "setting up an account," you're *publishing*.

**Discovery comes after creation:** Explore, star, fork happen once there's content in the system. Not the entry point.

### Concrete implications

- **Landing page:** Show a live preview — "here's what your wiki will look like" with a dogfood wiki rendered as the example. CTA is "publish" not "sign up."
- **Signup:** Nearly invisible. Drag-and-drop or git push creates the account as a side effect. For web: username + optional email, done, you're in the editor. For API: already one POST.
- **Post-signup:** Land in personal wiki's `index.md` editor, not an empty dashboard. Template pre-filled with Karpathy-style skeleton. Message is "write something" not "configure your account."
- **`/explore`:** Where the official wiki earns its keep. New users who aren't ready to publish yet browse real content — curated picks, most-starred, official docs. The "what is this place" moment.

### Why this matters

The audience (Karpathy-gist wave, Obsidian vault owners, ML researchers) already has content. They don't need to be convinced to write — they need a place to put what they've already written. The onboarding should be a funnel from "I have files" to "they're live" with minimal friction. Account setup, ACL configuration, and social features are all things that happen *after* the first publish, not before.

---

## The Librarian / Archive Vision (2026-04-10)

### Literary references

**Snow Crash — The Librarian (primary inspiration)**

Neal Stephenson's Snow Crash features "The Librarian" — an AI daemon in the Metaverse with instant recall of the entire Library of Congress (merged with the CIA into the "Central Intelligence Corporation"). The protagonist Hiro uses the Librarian as a conversational research partner. Key properties:

- Conversational, not search-based. Hiro doesn't type queries — he talks, follows threads, goes on tangents, and the Librarian adjusts.
- Has access to everything. The entire corpus.
- A daemon, not a person. Explicitly software — no pretense of being human. A tool with personality.
- Connects dots the human can't. Hiro discovers the Snow Crash virus by following threads through ancient Sumerian linguistics, neurolinguistics, and modern drug culture — a path he couldn't have found alone.
- The CIC is an information marketplace. "Stringers" (contributors) get paid when their information is used. The Library isn't Wikipedia — it's a network of individual contributors whose knowledge has economic value.

**Ready Player One — Halliday's Journals + The Curator**

- Halliday's Journals: a public archive on the planet Incipio in the OASIS — one person's entire life and knowledge rendered as explorable rooms. Every film, game, book he ever saw is archived with metadata.
- The Curator (Ogden Morrow): secretly the co-creator, disguised as a robotic "Jeeves-like" librarian who helps visitors navigate. Has complete knowledge but guides toward discovery rather than giving direct answers.
- Anorak's Almanac: the downloadable version — Halliday's journal as a portable PDF.

### How this maps to wikihub

| Literary concept | wikihub equivalent |
|---|---|
| Halliday's Journals / Library of Congress | Each user's wiki — a person's complete knowledge archive |
| Planet Incipio / The CIC | The wikihub platform — where all archives live and are discoverable |
| The Librarian / The Curator | An AI agent that navigates across all public wikis, guides discovery, finds cross-wiki connections |
| Anorak's Almanac | `git clone` / ZIP download — the portable, offline version |
| The Easter Egg Hunt | Cross-wiki connections — wikilinks, shared tags, knowledge graphs |
| Stringers getting paid | Attribution as currency — when the Librarian references your wiki, you get visibility, stars, forks |

### The "Humanity's Archive" vision

Wikipedia chose consensus — one voice, one article, edit wars. That works for facts but kills perspective.

wikihub is the opposite: every person gets their own archive. Karpathy's wiki isn't "the article about transformers" — it's Karpathy's understanding. Someone else's wiki about the same topic would be different. Both valuable. Neither replaces the other.

The archive for humanity isn't one library — it's a constellation of individual knowledge bases that can be traversed, forked, and cross-referenced. The platform's job is to make that constellation navigable.

### Naming / branding options

| Layer | Option A | Option B | Option C |
|---|---|---|---|
| Platform | wikihub | The Library | Humanity's Archive |
| AI agent | The Librarian | The Curator | The Archivist |
| Individual exports | Almanac | Journal | Archive |
| Curated showcase | The Journals | Featured | The Collection |
| Vision tagline | "The archive for humanity" | "Your knowledge, humanity's library" | "Every mind, an archive" |

**Strong contenders for the platform name:**
- **wikihub** — functional, already in use, domain locked (wikihub.md)
- **The Library** — evokes Snow Crash directly, simple, but maybe too generic
- **Humanity's Archive** — the grand vision name, better as a tagline than a product name

**Strongest combo:** Keep wikihub as the product name. Use "Humanity's Archive" as the vision/tagline. Name the AI agent "The Librarian."

### The Librarian as a product feature

The Librarian would be wikihub's AI agent — accessible via `@librarian` on the platform or in conversational mode via Cmd+K:

- Has read access to all public wikis on the platform
- Conversational — ask questions, it pulls from across wikis and synthesizes
- Finds cross-wiki connections individual authors can't see
- Credits sources — "according to @karpathy's transformer wiki..." (attribution built in)
- The one entity that has read everything on the platform
- Explicitly a tool/daemon, not pretending to be human — but with personality
- Powered by the MCP surface — uses the same API any agent uses, just the best-informed one

---

## Social Graph + Collective Intelligence (2026-04-10, Harrison Qian brainstorm)

### Follow users + personalized feed (`wikihub-9bi`)

Follow other users on WikiHub. When you search, results from people you follow are prioritized.

**Key insight (Harrison):** Instagram/Twitter influencers promote restaurants, products, businesses — but you can't search against what people you follow have recommended. ChatGPT can't search your social graph. WikiHub can because wiki content is structured and searchable.

**Vision:** "WikiHub influencers" — people known for curating great knowledge in specific domains. Following them means their recommendations surface first in your searches. A new generation of wiki influencers.

### Cross-wiki search across trusted friends (`wikihub-7nl`)

When searching, optionally search across wikis that friends have shared with you.

**Example:** "Who are investors I should talk to for my next funding round?" → searches across your friends' shared wikis, surfaces connections with intro paths.

Harrison: "One of the greatest offerings you can do is open your wiki to someone." The trust relationship enables knowledge sharing that wouldn't happen on a public platform. Since we have trusted relationships, you can list people and provide intro paths.

### Contribute-back from AI conversations — Stack Overflow for agents (`wikihub-fuj`)

When someone gets coding help or research answers from an AI conversation, contribute the Q&A back to a public wiki in an anonymized way.

Harrison: "Everyone's getting coding help in a private context and not online. Stack Overflow is a ghost town. But what if you can contribute back your AI answers in an anonymized way?"

This is the Claude Collective Intelligence (CCI) pattern applied to WikiHub. A "Stack Overflow for agents" — literally could be Stack Overflow, but for the agent era.

### Wiki matchmaking — opt-in discovery (`wikihub-bh0`)

Based on private wiki content (opt-in only), suggest people with complementary thinking/skills.

- "This person thinks a lot like you"
- "This person has really complementary thinking/skills to you"

Harrison: "As the gods of WikiHub, we can shape how all kinds of connections happen. We have godlike matchmaker power."

**Critical:** Must be opt-in. Privacy-sensitive since it's based on private content. Careful about information asymmetry — founders never want to broadcast that they need investors.

### SEO + LLM crawlability (`wikihub-76d`)

Wiki content is part of the internet and crawlable. Backlinked wiki pages boost SEO. When someone asks ChatGPT "vegetarian restaurants in Palo Alto", WikiHub content should surface.

Harrison: "If we have a bunch of different pages that are backlinked to each other, our SEO will go crazy."

### Fan wiki auto-updaters (`wikihub-acj`)

Automated scripts that watch new episodes of shows, read new books, and update fan wikis. Good example of the daily log + wiki page pattern.

Harrison: "The daily notes are like a changelog. And then the articles are like the tags. That's how you project IdeaFlow into WikiHub."

### Daily log → wiki projection pattern

The IdeaFlow/Karpathy shared insight: the daily log structure is the changelog, and wiki pages are the persistent articles. When you ingest a new source, it updates the log and ripple-updates the relevant wiki pages. This is how personal knowledge management (daily notes) maps to the wiki structure.
