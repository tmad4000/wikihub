# milkdown-src — WikiHub Milkdown bundle build

Builds `app/static/js/milkdown-bundle.js`, the Visual (WYSIWYG) editor bundle
used by the shared editor surface (`app/templates/_editor_surface.html` +
`app/static/js/editor-surface.js`) in the page editor and suggest-edit flow.

## Why this exists

ListHub originally shipped a prebuilt, **commonmark-only** `milkdown-bundle.js`
with no in-repo build tooling. That bundle escaped WikiHub-specific syntax on
save (the WYSIWYG round-trip corrupted content):

| Input              | Old (commonmark-only) output | This bundle |
|--------------------|------------------------------|-------------|
| `[[SomePage]]`     | `\[\[SomePage]]`             | `[[SomePage]]` |
| `$$\int_0^1$$`     | `$$\int\_0^1$$`              | `$\int_0^1$` (math, unescaped) |
| `![[img.png]]`     | `!\[\[img.png]]`             | `![[img.png]]` |

This build adds **lossless round-tripping** for math, wikilinks, and embeds.
See `wikihub-oyf4`.

## What's in the bundle

`createMilkdownEditor(container, initialMarkdown, onUpdate)` — **unchanged API**.
On top of the legacy commonmark + history + listener stack it adds:

- **Math** — official `@milkdown/plugin-math` (KaTeX). `$inline$` and
  `$$display$$`. KaTeX CSS is inlined into the bundle and injected as a
  `<style>` tag at runtime (single-file output, no separate `.css` asset).
- **Wikilinks** — `[[target]]` and `[[target|alias]]` via a custom plugin
  (`wikilink-plugin.mjs`): a `$remark` plugin that (a) splits `[[..]]` out of
  mdast text nodes on parse and (b) registers an `mdast-util-to-markdown`
  handler that re-emits the **literal** `[[..]]` with NO backslash escaping,
  plus a `$nodeSchema` (inline atom) with `parseMarkdown`/`toMarkdown` runners
  and a `$inputRule` for live typing. Pattern per Milkdown discussion #1674 /
  the marker-plugin example.
- **Embeds** — `![[embed]]`, same custom-node approach.

## Visual editor additions (wikihub-k7k5)

- **GFM** via `@milkdown/preset-gfm` (tables, task lists, strikethrough,
  autolinks, footnotes).
- **`probeRoundTrip(markdown)`** — parses and re-serializes on a detached editor
  and compares `normalizeForProbe()` forms. The editor opens a page in Visual
  only when this is ok; otherwise Markdown with an "Open visual anyway" notice.
  Normalization ignores marker style (`*`/`-`, `_`/`*`), blank-line placement,
  table padding, defensive backslash escapes, and autolink brackets; anything
  else (reference links, re-nested emphasis, inline `$$x$$` which the renderer
  shows as display math but Visual would rewrite to `$x$`) keeps the page in
  Markdown. The probe runs on every entry into Visual (load, tab, "Load latest")
  unless the user chose "Open visual anyway" or `?mode=visual`.
- **Sync on doc change** (`surface-plugin.mjs`): `onUpdate` fires only for
  document changes after mount, so toolbar/slash actions sync and merely
  viewing a page never rewrites it. `editor.wikihub.flush()` before submit.
- **Surface API** on `editor.wikihub`: `run(cmd)`, `activeState()`,
  `insertWikilink()`, `replaceRange()`, `toggleTaskAt()`; `[[` and `/` trigger
  events (`wikihub:trigger`, `wikihub:menukey`) for the host page's menus.
- **List fix**: Milkdown 7.20 stores list `spread` as the string `"false"`,
  which remark-stringify treats as truthy, turning every tight list loose on
  save. `remarkListSpreadFix` coerces it before the list handlers run.
- `prosemirror-model` is pinned to 1.25.11 via `overrides`: 1.25.12 validates
  attributes on create and rejects Milkdown 7.20's string `spread`, which made
  every list disappear from the Visual editor.

## Build

```bash
cd milkdown-src
npm install --before=YYYY-MM-DD   # a date >=1 month before the build (dependency policy)
npm run build                     # esbuild -> ../app/static/js/milkdown-bundle.js
```

`package-lock.json` is not committed; `--before` keeps transitive packages
inside the one-month window too. The 2026-11-03 build used
`--before=2026-10-03`.

Output is a single minified ESM file exporting `createMilkdownEditor` (and a few
named Milkdown ctx symbols). `node_modules/` is gitignored; the committed
artifact is `app/static/js/milkdown-bundle.js`.

## Test (acceptance — wikihub-oyf4)

```bash
cd milkdown-src
node test-roundtrip.mjs
```

Loads the built bundle in headless Chromium (puppeteer), calls
`createMilkdownEditor` directly, performs a real (no-op) ProseMirror edit to
force re-serialization through the listener, and asserts the three ticket
constructs round-trip identically:

- `See [[SomePage]] and [[a/b|Alias]].`
- `Math $E=mc^2$ and $$\int_0^1 x^2 dx$$` (math re-emits unescaped; note inline
  `$$...$$` mid-paragraph normalizes to `$...$` per remark-math — content is
  byte-identical, only the delimiter count changes; a `$$` block on its own
  line round-trips as a `$$` block). Because WikiHub's renderer shows inline
  `$$...$$` as display math, that delimiter change is visible, so the probe
  keeps such pages in Markdown (wikihub-k7k5 review)
- `Embed ![[diagram.png]]`

Browser driver: `PUPPETEER_PATH=/path/to/node_modules/puppeteer`, or
`PLAYWRIGHT_PATH` / the global Playwright install. Skips (exit 0) when neither
is available. Also covers GFM round-trips, the probe, and "no sync on load".

## Pinned dependency versions

Policy: no dependency newer than ~1 month old (build date 2026-06-08, so
everything is dated on/before 2026-05-08). Milkdown 7.21.x (2026-05-12) was
**rejected** as too recent; 7.20.0 is the newest qualifying line.

| Package | Version | npm publish date | ≥1 month old? |
|---|---|---|---|
| `@milkdown/core` | 7.20.0 | 2026-03-30 | yes |
| `@milkdown/ctx` | 7.20.0 | 2026-03-30 | yes |
| `@milkdown/preset-commonmark` | 7.20.0 | 2026-03-30 | yes |
| `@milkdown/plugin-history` | 7.20.0 | 2026-03-30 | yes |
| `@milkdown/plugin-listener` | 7.20.0 | 2026-03-30 | yes |
| `@milkdown/plugin-math` | 7.5.9 | 2024-12-16 | yes (latest published) |
| `@milkdown/prose` | 7.20.0 | 2026-03-30 | yes |
| `@milkdown/transformer` | 7.20.0 | 2026-03-30 | yes |
| `@milkdown/utils` | 7.20.0 | 2026-03-30 | yes |
| `@milkdown/preset-gfm` | 7.20.0 | 2026-03-30 | yes (added 2026-11-03) |
| `mdast-util-to-markdown` | 2.1.2 | 2024-11-04 | yes (direct import for the list fix) |
| `prosemirror-model` (override) | 1.25.11 | 2026-07-11 | yes |
| `katex` | 0.16.45 | 2026-04-05 | yes |
| `esbuild` (dev) | 0.27.7 | 2026-04-02 | yes |

`@milkdown/plugin-math@7.5.9` peers `@milkdown/core ^7.2.0`, satisfied by 7.20.0.
npm prints a "no longer supported" deprecation notice for it — it is the latest
published version and works against the 7.20 core; tracked as a known risk.
