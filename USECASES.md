# LLM wiki use cases and landscape

research compiled 2026-04-10. based on the Karpathy LLM Wiki viral moment and surrounding ecosystem.

## the karpathy moment (2026-04-02)

andrej karpathy posted a github gist describing a pattern for using LLMs to build and maintain structured markdown wikis. went viral: 5,000+ stars, 1,294 forks, 325K+ views in 48 hours. the core claim: RAG is stateless and wasteful — instead, pre-compile knowledge into a wiki that accumulates over time.

three-layer architecture:
1. **raw sources** (immutable) — PDFs, articles, transcripts, images. LLM reads but never modifies.
2. **the wiki** (LLM-maintained) — markdown files the LLM creates, updates, and cross-references.
3. **the schema** (rules) — a config file (like CLAUDE.md) telling the agent how to structure pages.

key insight: when you add a new source, the LLM reads it, extracts entities, updates existing pages, notes contradictions. knowledge compounds instead of resetting per query. for small knowledge bases (<100K tokens), this cuts token usage by ~95% vs RAG.

gist: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f

## most popular use cases

### 1. personal knowledge / "second brain"

the #1 use case. people filing journal entries, articles, podcast notes, research papers into a structured interlinked wiki. obsidian is the dominant viewer (graph view + wikilinks). the wiki replaces scattered notes with a living knowledge graph maintained by AI.

### 2. research vaults

long-horizon research where you're reading dozens of papers over weeks/months. the wiki synthesizes across sources so you don't re-derive connections every time you ask a question. academics and independent researchers are the heaviest users.

### 3. auto-wikipedia / biographical wikis

example: "namanopedia" / lifewiki — paste a name, get a full wikipedia-style article. an AI agent researches the web and compiles 40-50+ articles with infoboxes, wikilinks, citations, and categories in ~3 minutes. this is the flashiest demo but unclear how much sustained usage it gets.

### 4. company internal knowledge

engineering teams experimenting with LLM wikis for internal docs, onboarding materials, decision-support systems. comparing vendor pitches against structured criteria, tracking decisions, encoding constraints between projects. this is the highest-value use case but adoption is early.

### 5. agent memory

nous research integrated the pattern into hermes-agent as a built-in skill. the wiki becomes persistent memory for AI agents, solving the "forgetting everything between sessions" problem. agentmemory is a related project focused specifically on this.

### 6. meeting/call archives

feeding meeting transcripts and customer call recordings into a wiki that maintains a living record of decisions, action items, and evolving understanding of clients. practical for sales and product teams.

## implementations and tools

| project | description |
|---------|-------------|
| karpathy/llm-wiki (gist) | the original idea file — copy-paste into claude code or codex |
| LLM Wiki v2 (rohitg00) | extends the pattern with production lessons from agentmemory |
| Pratiyush/llm-wiki | ships with MCP server (7 tools), works from claude desktop, cursor, chatgpt desktop |
| MehmetGoekce/llm-wiki | L1/L2 cache architecture with logseq + obsidian support |
| hermes-agent (nous research) | LLM wiki as a built-in agent skill |
| "swarm knowledge base" | 10-agent system with supervisory models that validate pages before they enter the wiki |

## RAG vs LLM wiki vs agent search

| | RAG | LLM wiki (karpathy) | agent search (wikihub direction) |
|---|---|---|---|
| retrieval | vector similarity over chunks | full context load (small wikis) | tool-based navigation: list, read, search, follow links |
| knowledge accumulation | none — stateless per query | yes — wiki grows with each source | yes — wiki is the accumulated knowledge |
| structure preservation | destroys it (chunking) | creates it (pages, links) | navigates it (wikilinks, graph) |
| scaling | scales to millions of chunks | breaks at ~100K tokens | scales with agent's ability to navigate |
| cost | embedding + retrieval + synthesis | one-time compilation cost | no embedding cost, uses existing full-text search |
| infrastructure | vector DB, embedding API | local files + LLM | postgres full-text search (already built) |

## where wikihub fits

every implementation above is local-first — a directory of markdown files on your laptop. nobody has solved:

- **collaboration** — two people contributing to the same wiki
- **hosting** — sharing a wiki publicly or with a team
- **versioning** — git-backed history, diffs, forks
- **discovery** — finding and forking other people's wikis
- **agent access** — MCP/API interface for agents to read and write wikis remotely

wikihub is the only product building this layer. the karpathy pattern generates the content; wikihub hosts, versions, and shares it.

## key twitter/X posts

- karpathy's original tweet: https://x.com/karpathy/status/2040470801506541998
- karpathy's gist follow-up: https://x.com/karpathy/status/2040470801506541998
- yuchen jin's architecture diagram: https://x.com/Yuchenj_UW/status/2040482771576197377
- priyanka vergadia "RAG obsolete" thread (viral): https://x.com/pvergadia/status/2041705712863101277
- nous research / hermes-agent integration: https://x.com/NousResearch/status/2041378745332961462
- namanopedia demo: https://x.com/namanambavi/status/2041999388680319226
- meta alchemist on obsidian skills: https://x.com/meta_alchemist/status/2041279751999078813

## sources

- https://www.analyticsvidhya.com/blog/2026/04/llm-wiki-by-andrej-karpathy/
- https://venturebeat.com/data/karpathy-shares-llm-knowledge-base-architecture-that-bypasses-rag-with-an
- https://www.mindstudio.ai/blog/llm-wiki-vs-rag-markdown-knowledge-base-comparison
- https://www.mindstudio.ai/blog/andrej-karpathy-llm-wiki-knowledge-base-claude-code
- https://antigravity.codes/blog/karpathy-llm-wiki-idea-file
- https://evoailabs.medium.com/why-andrej-karpathys-llm-wiki-is-the-future-of-personal-knowledge-7ac398383772
- https://medium.com/data-science-in-your-pocket/andrej-karpathys-llm-wiki-bye-bye-rag-ee27730251f7
- https://gist.github.com/rohitg00/2067ab416f7bbe447c1977edaaa681e2
- https://github.com/Pratiyush/llm-wiki
