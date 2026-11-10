# Curation: negative and positive judgements

Tracking: `wikihub-f3b7`. Code: `app/curation.py`, `app/routes/api_curation.py`,
`app/moderation_cli.py`. Migration: `migrations/2026-11-09_curation_judgements.sql`.
Design source: the Noos semantic-web plan, decisions D3 and D4 (2026-11-09).

## Why

WikiHub keeps signup open. The 2026-11-03 trust gate (`app/trust.py`) withholds discovery from accounts
that are new or unverified, but it never looks at content. A promotional account that signs up with
Google and waits 14 days becomes trusted. On 2026-11-09, six such accounts were still listed on
/explore and in llms.txt.

Jacob's policy (2026-11-09): unless it is "super, super spam", do not delete. Affiliate and SEO accounts
are deprioritized: "almost an anti-signal for a smart crawler if we design it right". A visible notice
says the content has been curated against: "not quite a blacklist but a light blacklist". Negative
curation is the mirror of positive curation.

Suspension (`wikihub suspend`) stays reserved for the worst cases, such as link farms and automated
link placers.

## Model

`curation_judgements`, one row per judgement:

| Column | Meaning |
|---|---|
| `scope_type` | `account`, `wiki` or `page` |
| `scope_id` | `users.id`, `wikis.id` or `pages.id` |
| `polarity` | `negative` or `positive` |
| `reason` | negative: `promotional`, `affiliate_farm`, `automated_link_placement`, `low_quality`, `off_topic`. Positive: `featured`, `official`, `editor_pick` |
| `curator_user_id` | who recorded it (defaults to the `@wikihub` system user) |
| `note` | moderator note; never shown publicly |
| `created_at`, `revoked_at` | revoking sets `revoked_at`; rows are never deleted |

Database constraints tie each reason to its polarity. A partial unique index allows at most one active
judgement per (scope, polarity, reason).

**Resolution.** A page is judged through its owner, its wiki and itself. If any of the three has an
active negative judgement, the page is curated against. When several apply, a negative judgement beats
a positive one, then the most specific scope wins (page, then wiki, then account), then the newest.

## Effects

### Negative judgements

Content is never deleted and stays readable by link. The judgement adds:

- **Banner** at the top of every page in scope: "Curated against: promotional. Still readable; not
  recommended by WikiHub." It links to `/curation`, which explains the policy. Owners see it too.
- **Meta tag:** `<meta name="wikihub:curation" content="negative:promotional">`. Positive judgements emit
  `positive:<reason>` and show no banner.
- **No indexing:** `<meta name="robots" content="noindex">` plus `X-Robots-Tag: noindex`, and
  `rel="nofollow ugc noopener noreferrer"` on external links in rendered content.
- **Left out of discovery:** /explore (including Editorial Picks), /activity, /activity.rss, /people,
  /llms.txt, /llms-full.txt, the landing page's featured wikis and `GET /api/v1/wikis`. WikiHub serves
  no sitemap.xml today; a future one must use the same filters.
- **Search:** sorted last in `GET /api/v1/search`, not removed.
- **API:** wiki objects (`GET /api/v1/wikis`, `GET /api/v1/wikis/{owner}/{slug}`), page objects
  (`GET .../pages`, `GET .../pages/{path}`) and search results carry
  `curation: {id, polarity, reason, label, scope, at}` or `null`.

### How it reuses the trust plumbing

- An account-scope negative judgement makes `is_trusted()` and `trusted_user_clause()` false. Every
  existing trust check (noindex, nofollow, all discovery queries) therefore applies to it unchanged.
- Wiki and page scopes go through `app.curation.not_curated_against(...)`, which is added to
  `discoverable_wiki_ids()`, the activity query and the llms queries.
- `moderation.before_request` resolves the judgement for `/@user/wiki/page` URLs (including
  subdomain-rewritten ones) and stores it in `request.environ["wikihub.curation"]`. `base.html` renders
  the banner and meta tag from it.

Unlike the trust gate, curation applies whether or not `TRUST_GATE_ENABLED` is set, because it is an
explicit editorial act.

### Positive judgements

`featured`, `official` and `editor_pick` on a wiki, or on a page (which counts for its wiki), add that
wiki to Explore's Editorial Picks after the official `@wikihub/wikihub` card. A pick must still be
discoverable: a negative judgement or an untrusted owner removes it. Account-scope positive judgements
are recorded and exported but do not change Explore yet.

## Operating it

Every mutating command is a dry run unless `--execute` is passed.

```bash
flask --app wsgi.py wikihub flag seo --polarity negative --reason promotional --note "faceswapai.com CTA"
flask --app wsgi.py wikihub flag seo --reason promotional --execute
flask --app wsgi.py wikihub flag @someone/some-wiki --reason low-quality --execute
flask --app wsgi.py wikihub flag @someone/some-wiki/path/to/page --reason off_topic --execute
flask --app wsgi.py wikihub flag @wikihub/wikihub --reason official --execute
flask --app wsgi.py wikihub unflag seo                       # revoke all active judgements on @seo
flask --app wsgi.py wikihub unflag seo --reason promotional --execute
flask --app wsgi.py wikihub curation-list [--all] [--json]
```

Targets: `username` or `@username` (account), `@username/wiki` (wiki), `@username/wiki/path` (page;
`.md` is optional). Reasons accept hyphens (`affiliate-farm`). `--polarity` is optional, but if given
it must match the reason. Flagging something that is already flagged is a no-op. Each mutation writes a
`security` log event (`admin.curation.flag` / `admin.curation.unflag`).

## Export for the Noos backbone

`GET /api/v1/curation[?since=ISO8601][&cursor=…][&limit=N]` (default 500, max 1000). WikiHub never calls
Noos; Noos polls this endpoint and mirrors each row as a `curation` annotation on the URL (plan D3/D4).

```json
{
  "judgements": [{
    "id": 7,
    "polarity": "negative",
    "reason": "promotional",
    "label": "promotional",
    "scope": {"type": "wiki", "account": "seo", "wiki": "seo"},
    "url": "https://wikihub.md/@seo/seo",
    "wiki_urls": ["https://wikihub.md/@seo/seo"],
    "curator": "wikihub",
    "at": "2026-11-10T02:00:00+00:00",
    "revoked_at": null,
    "active": true
  }],
  "next_cursor": null,
  "has_more": false,
  "generated_at": "…",
  "docs": "https://wikihub.md/curation"
}
```

- Field names follow plan D4: `polarity`, `reason`, `scope`, `curator`, `at`, `note`.
- Reasons use underscores (`affiliate_farm`, `editor_pick`). D4 writes them with hyphens; the Noos
  side should map `_` to `-` if it prefers hyphens.
- `url` uses the apex path form (`BASE_URL/@user/wiki[/page]`), the stable identifier WikiHub also uses
  in llms.txt. It answers 301 to the canonical subdomain host, so a mirror keyed on canonical URLs
  should follow the redirect once.
- For an account, `url` is the profile and `wiki_urls` lists its public wikis.
- Rows are ordered by when they last changed (`coalesce(revoked_at, created_at)`), and revocations are
  included (`active: false`). Poll with the previous `next_cursor`, or with `since=<last sync>` using a
  `Z` suffix.
- **Public callers** get no `note`, no curator account, and no rows whose target is a private wiki or a
  non-public page, so the export cannot reveal private names.
- **Admin callers** (`Authorization: Bearer $ADMIN_TOKEN`) also get `note`, `curator_user` and
  private targets.

## Not built here

These belong to other workstreams or later steps:

- community "flag this" from the reader;
- endogenous quality scores (outbound-link density, keyword-stuffed metadata);
- Noos-side ingestion (W1);
- the reader sidebar that shows `/api/about` (W5 later).

Rollout to production is a separate, operator-run step: see `ops/curation-rollout-2026-11.md`.
