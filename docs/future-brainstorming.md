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
