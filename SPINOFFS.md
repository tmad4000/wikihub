# LLM Wiki spinoffs

everything spun off from Karpathy's LLM Wiki gist (2026-04-02).
source: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f

## direct implementations

- **Pratiyush/llm-wiki** — full implementation with MCP server, 7 tools (wiki_query, wiki_search, wiki_list_sources, wiki_read_page, wiki_lint, wiki_sync, wiki_export). works from Claude Desktop, Cursor, ChatGPT desktop. no server/db needed. https://github.com/Pratiyush/llm-wiki
- **MehmetGoekce/llm-wiki** — L1/L2 cache architecture, Logseq + Obsidian support. writeup: https://mehmetgoekce.substack.com/p/i-built-karpathys-llm-wiki-with-claude — https://github.com/MehmetGoekce/llm-wiki
- **kothari-nikunj/llm-wiki** — "Personal Wiki" implementation. https://github.com/kothari-nikunj/llm-wiki
- **MindStudio guide** — step-by-step "How to Build a Personal Knowledge Base With Claude Code". https://www.mindstudio.ai/blog/andrej-karpathy-llm-wiki-knowledge-base-claude-code

## extensions & evolutions

- **LLM Wiki v2** (rohitg00) — extends original with lessons from agentmemory (persistent memory engine for AI agents). addresses what breaks at scale. https://gist.github.com/rohitg00/2067ab416f7bbe447c1977edaaa681e2
- **Swarm Knowledge Base** — scales to 10-agent system with supervisory model that scores/validates draft pages before they enter the live wiki. solves compounding hallucination problem.
- **Nous Research / Hermes-Agent** — LLM Wiki as built-in skill in their agent framework. any Hermes agent gets wiki-building out of the box. https://x.com/NousResearch/status/2041378745332961462

## novel applications

- **Namanopedia / lifewiki** (Naman Ambavi) — paste a name, get auto-generated Wikipedia. agent researches the web, compiles 40-50+ articles with infoboxes, wikilinks, citations, categories. ~3 minutes. built by founder of Induced (AI web data extraction). https://x.com/namanambavi/status/2041999388680319226
- **Local LLM Knowledge Base with Obsidian** — guide for running the whole pattern locally, no cloud. https://www.modemguides.com/blogs/ai-infrastructure/local-llm-knowledge-base-obsidian-setup-guide
- **Waykee Cortex** — team-oriented variant with strict hierarchical inheritance. combines "Knowledge" layer (what exists) with "Work" layer (tasks, bugs, milestones), so issues inherit dual context automatically.

## ecosystem tooling

- **llm-wiki-compiler** — npm CLI with incremental rebuilds for the wiki compilation step.
- **MindOS** — multi-agent variant sharing a single wiki across Claude Code, Cursor, Gemini CLI, and others.
- **agent-wiki** — pure markdown, works with Claude Code, Codex, or Cursor.

## key coverage

- Analytics Vidhya: https://www.analyticsvidhya.com/blog/2026/04/llm-wiki-by-andrej-karpathy/
- VentureBeat: https://venturebeat.com/data/karpathy-shares-llm-knowledge-base-architecture-that-bypasses-rag-with-an
- MindStudio (wiki vs RAG comparison): https://www.mindstudio.ai/blog/llm-wiki-vs-rag-markdown-knowledge-base-comparison
- Antigravity (complete guide): https://antigravity.codes/blog/karpathy-llm-wiki-idea-file

## gap in the market

all of these are local-only, single-user tools. nobody has built the hosted, collaborative, multi-user version — which is WikiHub.
