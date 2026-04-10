# wikihub design system

## Direction

**Obsidian + marginalia.** Warm dark surfaces evoking volcanic glass. Amber/gold accents like a desk lamp illuminating paper. The `[[wikilink]]` double bracket is the visual signature — it appears in the logo, in rendered content, in search, everywhere.

Not GitHub. Not Notion. This is a knowledge publishing tool for people who live in dark-mode editors and read dense technical content.

## Intent

- **Who:** ML researchers, developers, knowledge workers. At their desks, switching between VS Code and browser. Strong typography opinions.
- **What:** Publish, read, search, navigate, control access, fork knowledge.
- **Feel:** Like reading a well-typeset paper in a dark room with a warm lamp. Dense enough to be useful, spacious enough to breathe. Serious but not corporate. Obsidian — dark, glassy, precise, catches light at edges.

## Logo

`[[wikihub]]` — literal double brackets, set in IBM Plex Mono. The brackets are the product's signature made typographic. They're slightly dimmer than "wikihub" — `--text-tertiary` for brackets, `--text-primary` for the word.

## Depth strategy

**Borders only.** No shadows. Obsidian is flat and glassy — depth comes from warm-tinted semi-transparent borders, not lifted surfaces. Higher elevation = slightly warmer/lighter background.

## Tokens

```css
:root {
  /* surfaces — obsidian warm */
  --bg-base: #0f0e0c;
  --bg-surface: #191714;
  --bg-elevated: #221f1b;
  --bg-overlay: #2a2622;

  /* text — warm ink */
  --text-primary: #e8e0d4;
  --text-secondary: #b8ad9e;
  --text-tertiary: #887d6e;
  --text-muted: #5a5248;

  /* accent — amber/gold */
  --accent: #d4a04a;
  --accent-hover: #e0b35a;
  --accent-muted: rgba(212, 160, 74, 0.12);
  --accent-subtle: rgba(212, 160, 74, 0.06);

  /* semantic */
  --color-public: #7a9e6b;
  --color-private: #c45c3c;
  --color-unlisted: #b8ad9e;
  --color-edit: #d4a04a;
  --color-wikilink: #d4a04a;
  --color-wikilink-broken: #c45c3c;
  --color-external: #7a9bb5;
  --color-success: #7a9e6b;
  --color-warning: #d4a04a;
  --color-error: #c45c3c;

  /* borders — warm tinted, semi-transparent */
  --border-default: rgba(200, 180, 150, 0.08);
  --border-emphasis: rgba(200, 180, 150, 0.15);
  --border-strong: rgba(200, 180, 150, 0.25);
  --border-accent: rgba(212, 160, 74, 0.4);

  /* controls */
  --control-bg: rgba(0, 0, 0, 0.2);
  --control-bg-hover: rgba(200, 180, 150, 0.08);
  --control-border: rgba(200, 180, 150, 0.12);

  /* typography */
  --font-body: 'Inter', -apple-system, sans-serif;
  --font-mono: 'IBM Plex Mono', 'Menlo', monospace;

  /* spacing */
  --sp-1: 4px;
  --sp-2: 8px;
  --sp-3: 12px;
  --sp-4: 16px;
  --sp-6: 24px;
  --sp-8: 32px;
  --sp-12: 48px;
  --sp-16: 64px;

  /* radius — obsidian sharp */
  --radius-sm: 4px;
  --radius-md: 6px;
  --radius-lg: 10px;
}
```

## Typography

- **Headlines:** Inter, 700 weight, letter-spacing -0.02em. Tight, typeset feel.
- **Body:** Inter, 400 weight, 16px min, line-height 1.7.
- **Labels/meta:** Inter, 500 weight, 13px, letter-spacing 0.01em.
- **Code:** IBM Plex Mono, 14.5px, line-height 1.6.
- **Logo:** IBM Plex Mono, brackets in `--text-tertiary`.

## Component patterns

### Nav bar
- Background: `--bg-base`, bottom border `--border-default`
- Logo: `[[wikihub]]` in mono font
- Height: 56px. Items centered vertically.
- No glassmorphism/blur. Clean border separation.

### Cards (wiki cards, feature cards)
- Background: `--bg-surface`
- Border: 1px solid `--border-default`
- Radius: `--radius-md`
- Padding: `--sp-6`
- Hover: border shifts to `--border-emphasis`
- No shadow.

### Buttons
- Primary: `--accent` bg, `#0f0e0c` text, radius `--radius-sm`
- Secondary: transparent bg, `--border-emphasis` border, `--text-secondary` text
- Ghost: transparent bg, no border, `--text-secondary` text, hover `--control-bg-hover`
- Min height: 44px (touch target)

### Inputs
- Background: `--control-bg` (inset, darker than surroundings)
- Border: 1px solid `--control-border`
- Focus: border `--border-accent`
- Radius: `--radius-sm`

### Sidebar (reader/folder views)
- Same bg as canvas (`--bg-base`), NOT different color
- Right border `--border-default` for separation
- Active page: left accent border `--border-accent`, bg `--accent-subtle`

### Visibility badges
All icons are inline SVGs (Lucide-style, 14x14, stroke="currentColor"), never emoji.
- Public: `--color-public` text + border, **globe** SVG
- Private: `--color-private` text + border, **lock** SVG
- Unlisted: `--color-unlisted` text + border, **link/chain** SVG (NOT eye — "accessible by URL only")
- Edit variants: base icon + **pencil** SVG side by side (globe + pencil, link + pencil)
- Pill shape: `--radius-sm` padding, subtle bg tint of the semantic color
- Icon containers: `display: inline-flex; align-items: center; gap: 3px;`

### Wikilinks
- Resolved: `--color-wikilink` (amber), no underline, underline on hover
- Unresolved: `--color-wikilink-broken` (red oxide), dashed underline always
- External: `--color-external` (slate blue), subtle arrow indicator

## Decisions locked

- No shadows anywhere. Borders only.
- Sidebar same bg as canvas. Border-separated.
- Warm amber accent, not blue.
- Logo is `[[wikihub]]` with bracket motif.
- All colors warm-shifted. No cool grays.
