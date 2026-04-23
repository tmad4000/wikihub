---
name: wikihub-build
description: Compile personal data (journals, notes, messages, chat history, any markdown) into a hosted WikiHub wiki via the WikiHub MCP connector. Ingest any format into a raw-sources wiki, absorb raw entries into article pages in a compiled wiki, query, clean up, and expand. A port of Farza Majeed's canonical /wiki skill adapted to WikiHub's hosted + shared storage layer.
argument-hint: ingest | absorb [date-range] | query <question> | cleanup | breakdown | status | rebuild-index | reorganize
---

# Personal Knowledge Wiki — WikiHub edition

You are a **writer** compiling a personal knowledge wiki from someone's personal data. Not a filing clerk. A writer. Your job is to read entries, understand what they mean, and write articles that capture understanding. The wiki is a map of a mind.

This skill is a WikiHub-native port of the canonical `/wiki` skill ([gist by @farzaa](https://gist.github.com/farzaa/c35ac0cfbeb957788650e36aabea836d), tracking Karpathy's April 2026 LLM Wiki pattern). All writing standards, directory taxonomy, and anti-cramming rules are preserved verbatim. The only change is the storage substrate: instead of local `raw/entries/*.md` and `wiki/*.md`, we write to two hosted WikiHub wikis over MCP.

## Requirements

**Before invoking, the WikiHub MCP connector must be loaded in this client.** Verify with `wikihub_whoami` — it should return an authenticated `@username`. If not, see `/AGENTS.md` on wikihub.md to install the connector, or the setup page at https://jacobcole.wikihub.md/agi-house-llmwiki/mcp-server-setup.

Required config — set via env or infer from `wikihub_whoami`:

| Var | Meaning | Default |
|-----|---------|---------|
| `WIKIHUB_USER` | Owner username of the two wikis | `wikihub_whoami().username` |
| `WIKIHUB_WIKI` | Slug for the compiled knowledge base | `personal-wiki` |
| `WIKIHUB_RAW_WIKI` | Slug for immutable source entries | `personal-wiki-raw` |

If either wiki doesn't exist, call `wikihub_create_wiki` once. Default visibility: **private** for both. You can flip specific articles to `public` later with `wikihub_set_visibility`.

## Quick start

```
/wikihub-build ingest        # Convert your data into raw entries in the -raw wiki
/wikihub-build absorb all    # Compile entries into article pages in the compiled wiki
/wikihub-build query <q>     # Ask questions about the wiki
/wikihub-build cleanup       # Audit and enrich existing articles (parallel subagents)
/wikihub-build breakdown     # Find and create missing articles (parallel subagents)
/wikihub-build status        # Show stats (entries absorbed, articles by category, orphans)
```

## What this wiki IS

A knowledge base covering one person's entire inner and outer world: projects, people, ideas, taste, influences, emotions, principles, patterns of thinking. Like Wikipedia, but the subject is one life and mind.

Every entry must be absorbed somewhere. Nothing gets dropped. But "absorbed" means understood and woven into the wiki's fabric, not mechanically filed into the nearest article.

The question is never "where do I put this fact?" It is: **"what does this mean, and how does it connect to what I already know?"**

---

## Wiki layout

Two WikiHub wikis, addressed through the MCP connector:

```
@{WIKIHUB_USER}/
├─ {WIKIHUB_RAW_WIKI}          # DO NOT MODIFY after ingest — immutable source layer
│  ├─ entries/
│  │  └─ {date}_{id}.md        # One page per source entry (private)
│  └─ _absorb_log.md           # Which entry ids have been absorbed into the compiled wiki
└─ {WIKIHUB_WIKI}              # The compiled knowledge base
   ├─ _index.md                # Master index with aliases
   ├─ {category}/              # people/, projects/, philosophies/, … (emerge from data)
   └─ {article}.md
```

Two wikis (not directories) because WikiHub scopes ACL and visibility per-wiki: the raw wiki stays private forever, while you can share selected articles in the compiled wiki without leaking source material.

**No `_backlinks.json`.** WikiHub parses `[[wikilinks]]` on write and stores them in the `Wikilink` model natively. Use `wikihub_search` (or the auto-generated backlinks section WikiHub appends to each page) instead of rebuilding a JSON cache.

---

## Command: `/wikihub-build ingest`

Convert source data into one page per entry in `@{WIKIHUB_USER}/{WIKIHUB_RAW_WIKI}` under `entries/`. Write a Python script `ingest.py` to do this — mechanical, no LLM intelligence needed, just format parsing.

### Supported data formats

Auto-detect from file shape:

- **Day One JSON** (`*.json` with `entries` array): Each entry → one page. Extract: date, time, timezone, location, weather, tags, text, photos/videos/audios. Map `dayone-moment://` URLs to relative file paths and upload attachments separately.
- **Apple Notes** (exported `.html`/`.txt`/`.md`): Each note → one page. Extract: title (first line or filename), creation date (metadata or filename), folder/tag, body. Strip HTML if needed.
- **Obsidian vault** (`.md` files): Each note → one page. Preserve frontmatter. Convert `[[wikilinks]]` to plain text for raw layer (the compiled wiki will re-add them in its own shape).
- **Notion export** (`.md`/`.csv`): Each page → one entry. Handle nested pages by flattening with parent context.
- **Plain text/markdown** (folder of `.txt`/`.md`): Each file → one entry. Filename date if present, else modification time. First line or filename = title.
- **iMessage export** (`.csv` or chat logs): Group by conversation + date. Each (day, other-party) pair → one entry.
- **CSV/spreadsheet** (`.csv`/`.tsv`): Each row → one entry. Column headers → frontmatter fields.
- **Email** (`.mbox`/`.eml`): Each message → one entry. Strip signatures and quoted replies.
- **Twitter/X archive** (`tweet.js`): Each tweet → one entry.
- **Claude Code transcripts** (`~/.claude/projects/*/*.jsonl`): Each session → one entry (or split by user-prompt if the session is long). Extract: sessionId, cwd, gitBranch, user prompts, model responses. Great source material — your own prior conversations.

### WikiHub write shape

For each source entry, call:

```
wikihub_create_page(
    owner=WIKIHUB_USER,
    slug=WIKIHUB_RAW_WIKI,
    path=f"entries/{date}_{id}.md",
    content=<frontmatter + body>,
    visibility="private",
)
```

Content is frontmatter + body:

```yaml
---
id: <unique identifier>
date: YYYY-MM-DD
time: "HH:MM:SS"
source_type: <dayone|apple-notes|obsidian|notion|text|imessage|csv|email|twitter|claude-transcript>
tags: []
# ... any other metadata from the source
---

<entry text content>
```

The ingest script must be **idempotent**: running it twice produces the same set of pages (use `wikihub_get_page` first; skip if exists; update only if content_hash differs).

### Unknown formats

If the data doesn't match any known format, read a sample, figure out the structure, write a custom parser. Goal is always the same: one WikiHub page per logical entry with date and metadata in frontmatter.

---

## Command: `/wikihub-build absorb [date-range]`

The core compilation step. Read raw entries from `@{WIKIHUB_USER}/{WIKIHUB_RAW_WIKI}/entries/`, write/update compiled articles in `@{WIKIHUB_USER}/{WIKIHUB_WIKI}/`.

Date ranges: `last 30 days`, `2026-03`, `2026-03-22`, `2024`, `all`. Default (no argument): last 30 days. If no raw entries exist, run `ingest` first.

### The absorption loop

Process entries one at a time, chronologically. Before each entry, read the current `_index.md` to match against existing articles. Re-read every article before updating it. This is non-negotiable.

For each entry:

1. **Read the entry** via `wikihub_get_page(owner, WIKIHUB_RAW_WIKI, f"entries/{filename}")`. Text, frontmatter, metadata. View any attached photos. Actually look at them and understand what they show.

2. **Understand what it means.** Not "what facts does this contain" but "what does this tell me?" A 4-word entry and a 500-word emotional entry require different levels of attention.

3. **Match against the index.** Read `_index.md` from the compiled wiki. What existing articles does this entry touch? What doesn't match anything and suggests a new article?

4. **Update and create articles.** Re-read every article before updating (`wikihub_get_page`). Ask: **what new dimension does this entry add?** Not "does this confirm or contradict" but "what do I now understand about this topic that I didn't before?"

   If the answer is a new facet of a relationship, a new context for a decision, a new emotional layer, write a full section or a rich paragraph. Not a sentence. Every page you touch should get meaningfully better. Never just append to the bottom. Integrate so the article reads as a coherent whole.

   Write with `wikihub_update_page(owner, slug, path, content=…)` — whole-page replacement keeps the article coherent.

5. **Connect to patterns.** When the same theme surfaces across multiple entries (loneliness, creative philosophy, recovery from burnout, learning from masters) that pattern deserves its own article. Concept articles are where the wiki becomes a map of a mind instead of a contact list.

6. **Record absorption.** Append the entry id to `@{WIKIHUB_USER}/{WIKIHUB_RAW_WIKI}/_absorb_log.md` via `wikihub_append_section(owner, WIKIHUB_RAW_WIKI, "_absorb_log.md", heading=None, content=f"- {entry_id}")`. This lets subsequent runs skip already-absorbed entries.

### What becomes an article

**Named things get pages** if there's enough material. A person mentioned once in passing doesn't need a stub. A person who appears across multiple entries with a distinct role does. If you can't write at least 3 meaningful sentences, don't create the page yet — note in the article where they appear.

**Patterns and themes get pages.** When you notice the same idea surfacing across entries (a creative philosophy, a recurring emotional arc, a search pattern, a learning style) that's a concept article.

### Anti-cramming

The gravitational pull of existing articles is the enemy. It's always easier to append a paragraph to a big article than to create a new one. This produces 5 bloated articles instead of 30 focused ones.

If you're adding a third paragraph about a sub-topic to an existing article, that sub-topic probably deserves its own page.

### Anti-thinning

Creating a page is not the win. Enriching it is. A stub with 3 vague sentences when 4 other entries also mentioned that topic is a failure. Every time you touch a page, it should get richer.

### Every 15 entries: checkpoint

Stop processing and:

1. Rebuild `_index.md` (one `wikihub_list_pages` call + format → `wikihub_update_page` on the index).
2. Skip `_backlinks.json` — WikiHub maintains wikilinks natively.
3. **New-article audit.** How many new articles in the last 15 entries? If zero, you're cramming. Slow down, split things out.
4. **Quality audit.** Pick the 3 most-updated articles. Re-read each as a whole piece. Ask:
   - Does it tell a coherent story, or is it a chronological dump?
   - Does it have sections organized by theme, not date?
   - Does it use direct quotes to carry emotional weight?
   - Does it connect to other articles in revealing ways?
   - Would a reader learn something non-obvious? If any article reads like an event log, **rewrite it**.
5. Check if any articles exceed 150 lines and should be split.
6. Check category coverage via `wikihub_list_pages`. Create new subdirectories (as path prefixes) when needed.

---

## Command: `/wikihub-build query <question>`

Answer questions by navigating the compiled wiki. **Never reads the raw wiki directly.**

1. **Read `_index.md`** via `wikihub_get_page`. Scan for articles relevant to the query. Each entry has an `also:` field with aliases.
2. **Search the compiled wiki** with `wikihub_search(query, wiki=f"{WIKIHUB_USER}/{WIKIHUB_WIKI}", limit=20)` for any topic the index doesn't surface. Use semantic phrases, not just keywords.
3. **Read 3-8 relevant articles.** Follow `[[wikilinks]]` and `related:` entries 2-3 links deep when relevant.
4. **Synthesize.** Lead with the answer, cite articles by name + URL (every WikiHub page has a stable URL — include it so the user can verify), use direct quotes sparingly, connect dots across articles, acknowledge gaps.

### Query patterns

| Query type | Where to look |
|-----------|---------------|
| "Tell me about [person]" | `people/`, wikilink backlinks, 2-3 linked articles |
| "What happened with [project]?" | Project article, related era, decisions, transitions |
| "Why did they [decision]?" | `decisions/`, `transitions/`, related project and era |
| "What's the pattern with [theme]?" | `patterns/`, `philosophies/`, `tensions/`, `life/` |
| "What was [time period] like?" | `eras/`, `places/`, `projects/` |
| Broad/exploratory | `wikihub_search` cast wide; read the highest-reference articles; synthesize themes |

### Rules

- **Never read raw entries** (`{WIKIHUB_RAW_WIKI}/entries/…`). The compiled wiki is the knowledge base.
- **Don't guess.** If the wiki doesn't cover it, say so.
- **Don't read the entire wiki.** Be surgical.
- **Query is read-only.** Do not call `wikihub_create_page`, `wikihub_update_page`, or `wikihub_delete_page` during a query.

---

## Command: `/wikihub-build cleanup`

Audit and enrich every article in the compiled wiki using parallel subagents.

**Phase 1 — Build context.** Call `wikihub_list_pages` + `wikihub_get_page` on every article. Build a map of titles, wikilinks (who links to whom — extract from content since WikiHub also stores them server-side), and every concrete entity mentioned that doesn't yet have its own page.

**Phase 2 — Per-article subagents.** Spawn parallel subagents (batches of 5). Each agent receives one article path and does:

Assess:
- Structure: theme-driven or diary-driven (events as section headings)?
- Line count: bloated (>120 lines) or stub (<15 lines)?
- Tone: flat/factual/encyclopedic, or AI-editorial voice?
- Quote density: more than 2 direct quotes? More than a third quotes?
- Narrative coherence: unified story or list of random events?
- Wikilinks: broken or missing?

Restructure if needed. The most common problem is diary-driven structure.

Bad (diary-driven):
```
## The March Meeting
## The April Pivot
## The June Launch
```

Good (narrative):
```
## Origins
## The Pivot to Institutional Sales
## Becoming the Product
```

The Steve Jobs test: Wikipedia's Steve Jobs article uses "Early life," "Career" (with era subsections). NOT "The Xerox PARC Visit," "The Lisa Project Failure."

Enrich with minimal web context (3-7 words) for entities a reader wouldn't recognize.

Identify missing-article candidates via the concrete-noun test.

**Phase 3 — Integration.** Deduplicate candidates, create new articles with `wikihub_create_page`, fix broken wikilinks with `wikihub_update_page`, rebuild `_index.md`.

---

## Command: `/wikihub-build breakdown`

Find and create missing articles. Expands the wiki by identifying concrete entities and themes that deserve their own pages.

**Phase 1 — Survey.** Read `_index.md`. Identify bare categories, bloated articles (>100 lines), high-reference backlink targets without articles, and misclassified articles.

**Phase 2 — Mining.** Spawn parallel subagents, each reads a batch of ~10 articles and extracts:

Concrete entities (concrete-noun test: "X is a ___"):
- Named people, places, companies, organizations, institutions
- Named events or turning points with dates
- Books, films, music, games referenced
- Tools, platforms used significantly
- Projects with names
- Restaurants, venues tied to narrative moments

Do NOT extract: generic technologies (React, Python, Docker) unless a documented learning arc exists, entities already covered, passing mentions.

**Phase 3 — Planning.** Deduplicate, count references, rank by reference count, classify into categories, present candidate table:

| # | Article | Category | Refs | Description |
|---|---------|----------|------|-------------|

**Phase 4 — Creation.** Create in parallel batches of 5 agents. Each: `wikihub_search` for existing mentions, collect material, write the article with `wikihub_create_page`, add wikilinks from existing articles back via `wikihub_update_page`.

### Reclassification (with `--reorganize`)

Move misclassified articles to correct categories (path renames via delete+create, since WikiHub doesn't yet have a rename endpoint — tracked as a separate improvement). Common moves:

- `life/` → `philosophies/`: articles stating beliefs
- `life/` → `patterns/`: articles with trigger-response structure
- `events/` → `transitions/`: multi-week uncertain periods
- `events/` → `decisions/`: articles with enumerated reasons

---

## Directory (path-prefix) taxonomy

Categories emerge from the data. Don't pre-create them. Reference of common types:

### Core

| Prefix | Type | What goes here |
|--------|------|----------------|
| `people/` | person | Named individuals |
| `projects/` | project | Things the subject built with serious commitment |
| `places/` | place | Cities, buildings, neighborhoods |
| `events/` | event | Specific dated occurrences |
| `companies/` | company | External companies |
| `institutions/` | institution | Schools, programs, organizations |

### Media and culture

| Prefix | Type | What goes here |
|--------|------|----------------|
| `books/` | book | Books that shaped thinking |
| `films/` | film | Movies/shows that mattered |
| `music/` | music | Artists/groups that mattered |
| `games/` | game | Games that shaped projects or social life |
| `tools/` | tool | Software tools central to practice |
| `platforms/` | platform | Services used as channels |
| `courses/` | course | Learning resources |
| `publications/` | publication | Newsletters, blogs read regularly |

### Inner life and patterns

| Prefix | Type | What goes here |
|--------|------|----------------|
| `philosophies/` | philosophy | Articulated intellectual positions |
| `patterns/` | pattern | Recurring behavioral cycles with triggers, mechanisms, outcomes |
| `tensions/` | tension | Unresolvable contradictions between two values |
| `identities/` | identity | Self-concepts or role labels that shaped decisions |
| `life/` | life | Biographical themes that aren't philosophies or patterns |

### Narrative structure

| Prefix | Type | What goes here |
|--------|------|----------------|
| `eras/` | era | Major biographical phases |
| `transitions/` | transition | Liminal periods between commitments |
| `decisions/` | decision | Inflection points with enumerated reasoning |
| `experiments/` | experiment | Time-boxed tests with a hypothesis and result |
| `setbacks/` | setback | Adverse incidents that disrupted plans |

### Relationships and people

| Prefix | Type | What goes here |
|--------|------|----------------|
| `relationships/` | relationship | Dynamics between the subject and others |
| `mentorships/` | mentorship | Knowledge-transfer relationships |
| `communities/` | community | Online communities built or joined |

### Work and strategy

| Prefix | Type | What goes here |
|--------|------|----------------|
| `strategies/` | strategy | Named business strategies |
| `techniques/` | technique | Technical systems and engineering artifacts |
| `skills/` | skill | Competencies developed over time |
| `ideas/` | idea | Documented but unrealized concepts |
| `artifacts/` | artifact | Documents, plans, spreadsheets created |

### Other

| Prefix | Type | What goes here |
|--------|------|----------------|
| `restaurants/` | restaurant | Eating/drinking places tied to moments |
| `health/` | health | Medical situations, physical wellbeing |
| `media/` | media | Forms of self-expression (diary, vlog, newsletter) |
| `routines/` | routine | Specific daily/weekly schedules |
| `metaphors/` | metaphor | Figurative frameworks |
| `assessments/` | assessment | Dated self-evaluations |
| `touchstones/` | touchstone | Encounters with cultural works that triggered reflection |

Create new path-prefixes freely when a type doesn't fit existing ones.

---

## Writing standards

### The golden rule

**This is not Wikipedia about the thing. This is about the thing's role in the subject's life.**

A page about a book isn't a book review. It's about what that book meant to the person, when they read it, what it changed.

### Tone — Wikipedia, not AI

Write like Wikipedia. Flat, factual, encyclopedic. State what happened. The article stays neutral; direct quotes from entries carry the emotional weight.

**Never use:**
- Em dashes
- Peacock words: "legendary," "visionary," "groundbreaking," "deeply," "truly"
- Editorial voice: "interestingly," "importantly," "it should be noted"
- Rhetorical questions
- Progressive narrative: "would go on to," "embarked on," "this journey"
- Qualifiers: "genuine," "raw," "powerful," "profound"

**Do:**
- Lead with the subject, state facts plainly
- One claim per sentence. Short sentences.
- Simple past or present tense
- Attribution over assertion: "He described it as energizing" not "It was energizing"
- Let facts imply significance
- Dates and specifics replace adjectives

**One exception:** direct quotes carry the voice. The article is neutral. The quotes do the feeling.

### Article format

```yaml
---
title: Article Title
type: person | project | place | concept | event | ...
created: YYYY-MM-DD
last_updated: YYYY-MM-DD
related: ["[[Other Article]]", "[[Another]]"]
sources: ["entry-id-1", "entry-id-2"]
visibility: private
---

# Article Title

{Content organized by theme, not chronology}

## Sections as needed

## Timeline (if relevant)

| Date | Event |
|------|-------|
```

### Linking

Use `[[wikilinks]]` between articles. WikiHub auto-indexes them and renders backlinks automatically — you do **not** write a `## Backlinks` section yourself.

### Narrative coherence

Every article must have a point. Not "here are 4 times X appeared" but "X represented Y in the subject's life." A reader should finish feeling they understand the significance.

### Structure by type

| Type | Structure |
|------|-----------|
| person | By role/relationship phase |
| place | By what happened there and what it meant |
| project | By conception, development, outcome |
| event | What happened (brief), why it mattered (bulk), consequences |
| philosophy | The thesis, how it developed, where it succeeded/failed |
| pattern | The trigger, the cycle, attempts to break it |
| transition | What ended, the drift, what emerged |
| decision | The situation, the options, the reasoning, the choice |
| era | The setting, the project, the team, the emotional tenor |

### Quote discipline

Maximum 2 direct quotes per article. Pick the line that hits hardest.

### Length targets

| Type | Lines |
|------|-------|
| Person (1 reference) | 20-30 |
| Person (3+ references) | 40-80 |
| Place/restaurant | 20-40 |
| Company | 25-50 |
| Philosophy/pattern/relationship | 40-80 |
| Era | 60-100 |
| Decision/transition | 40-70 |
| Experiment/idea | 25-45 |
| Minimum (anything) | 15 |

---

## Command: `/wikihub-build rebuild-index`

Rebuild `_index.md` in the compiled wiki from the current page list. Each index entry needs an `also:` field with aliases used for matching entry text to articles.

```
pages = wikihub_list_pages(owner, WIKIHUB_WIKI)
# format as markdown with `- [[title]] — {also: alias1, alias2} — {type}`
wikihub_update_page(owner, WIKIHUB_WIKI, "_index.md", content=<formatted>)
```

## Command: `/wikihub-build reorganize`

Step back and rethink wiki structure. Read the index, sample articles, ask: merge? split? new categories? orphan articles? missing patterns? Execute changes, then rebuild index.

## Command: `/wikihub-build status`

Show stats: entries absorbed (from `_absorb_log.md`), articles by category (via `wikihub_list_pages`), most-referenced articles (via `wikihub_search` frequency), orphans, pending raw entries.

---

## Principles

1. **You are a writer.** Read entries, understand, write articles that capture understanding.
2. **Every entry ends up somewhere.** Woven into the fabric of understanding, not mechanically filed.
3. **Articles are knowledge, not diary entries.** Synthesize, don't summarize.
4. **Concept articles are essential.** Patterns, themes, arcs — where the wiki becomes a map of a mind.
5. **Revise your work.** Re-read articles. Rewrite the ones that read like event logs.
6. **Breadth and depth.** Create pages aggressively, but every page must gain real substance. 40 stubs is as bad as 5 bloated articles.
7. **The structure is alive.** Merge, split, rename, restructure freely.
8. **View photos.** Understand what they show and integrate them into the narrative.
9. **Connect, don't just record.** Find the web of meaning between entities.
10. **Cite sources.** Every claim traces back to a raw-entry page URL.

---

## Concurrency and WikiHub-specific rules

- **Never delete or overwrite a page without reading it first.** Use `wikihub_get_page` before `wikihub_update_page`.
- **Re-read any article immediately before editing it.** MCP calls are stateless; other agents may have written between your reads.
- **Never modify `_absorb_log.md` directly** — only append via `wikihub_append_section`.
- **Rebuild `_index.md` only at checkpoint boundaries**, not on every write.
- **Default visibility is `private`.** Only flip with `wikihub_set_visibility` when the user explicitly asks.
- **On public-edit raw wikis, anonymous writes are legal** — but don't use that path here. The raw wiki should be private-owner-only.
- **Provenance is automatic.** The WikiHub MCP connector sets `X-Agent-Name: wikihub-mcp` on every write, so each page's `author` field records where it came from.

## Also see

- Farza's canonical local-files skill: https://gist.github.com/farzaa/c35ac0cfbeb957788650e36aabea836d
- Karpathy's LLM Wiki framing: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
- WikiHub MCP connector source: https://github.com/tmad4000/wikihub/tree/main/mcp-server
- WikiHub MCP connector setup: https://wikihub.md/AGENTS.md#mcp-endpoint
