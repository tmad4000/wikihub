# Curation: judgements and enforcement

- **Tracking:** `wikihub-f3b7`.
- **Code:** `app/curation.py` (resolution and enforcement), `app/routes/api_curation.py` (export),
  `app/moderation_cli.py` (operator commands), `app/templates/base.html` (notice and meta).
- **Migration:** `migrations/2026-11-09_curation_judgements.sql`.
- **Design:** Noos semantic-web plan D4 (Jacob, 2026-11-09), amended by plan section 3c (Astra review,
  items 6, 7 and 9).

## Why

WikiHub keeps signup open. The 2026-11-03 trust gate (`app/trust.py`) withholds discovery from accounts
that are new or unverified, but it never looks at content. A promotional account that signs up with
Google and waits 14 days becomes trusted. On 2026-11-09, six such accounts were still listed on
/explore and in llms.txt.

Jacob's policy: unless it is "super, super spam", do not delete. Affiliate and SEO accounts are
deprioritized: "almost an anti-signal for a smart crawler if we design it right". A visible notice
says the content has been curated against: "not quite a blacklist but a light blacklist". Negative
curation is the mirror of positive curation.

## Two layers

| | Judgement | Enforcement |
|---|---|---|
| What | An attributed, versioned, contestable opinion about a wiki, page or account | What WikiHub does on its own surfaces while a judgement is in force |
| Who decides | Curator (published as "WikiHub editors") under a policy version | WikiHub, from the judgement |
| Where it lives | `curation_judgements`; exported at `GET /api/v1/curation` | Notice, discovery queries, noindex, `nofollow ugc`, search order |
| Who consumes it | Noos (mirrored as `curation` annotations), crawlers, agents; each decides how far to trust WikiHub's editors | WikiHub's own readers and its discovery surfaces |

Each exported judgement carries `enforcement.wikihub`, the list of actions WikiHub applies. That way a
consumer can tell the opinion apart from WikiHub's response to it.

## Judgement model (`curation_judgements`)

| Column | Meaning |
|---|---|
| `scope_type`, `scope_id` | `wiki` (the default), `page`, or `account` (only when chosen explicitly) |
| `polarity`, `reason` | negative: `promotional`, `affiliate_farm`, `automated_link_placement`, `low_quality`, `off_topic`. Positive: `featured`, `official`, `editor_pick`. A database constraint ties each reason to its polarity. |
| `status` | `active`, `retracted` (unflag), `superseded` (replaced by `superseded_by`), or `expired` |
| `evidence` | **Public** list of URLs or short texts supporting the judgement; exported |
| `note` | Private moderator note; never shown publicly or exported to non-admins |
| `affected_version` | Git sha of the wiki's authoritative repo when judged (wiki and page scope), so a later reader can tell whether the content has changed since |
| `policy_version` | Version of this document the judgement was made under (`CURATION_POLICY_VERSION`, currently `2026-11-09`) |
| `review_at` | When an editor should look at it again (default: 180 days); `curation-list --due` |
| `expires_at` | Optional. After it passes, the judgement has no effects and reads `expired`, without anyone touching the row. |
| `superseded_by` | The judgement that replaced this one |
| `reply_url` | The owner's published reply or appeal, linked from the notice and the export |
| `curator_user_id` | The account that recorded it. Admin export only; the public attribution is "WikiHub editors". |
| `created_at`, `updated_at` | `updated_at` changes on every status or reply change and drives the export |

A judgement is **in force** when `status = 'active'` and `expires_at` is unset or in the future. Only
judgements in force have effects. Rows are never deleted. A partial unique index allows at most one
active judgement per (scope, polarity, reason).

### Scope and resolution

- **Wiki is the default scope.** `flag @user/wiki` judges that wiki, and `flag @user/wiki/path` judges
  one page. A whole account is judged only with `--scope account` and a bare username, and the notice
  then says "for everything published by @user". The notice always states its scope: "this page",
  "this wiki (@user/wiki)" or "everything published by @user".
- **Resolution path.** A page is judged through itself, its wiki and its owner. Negative and positive
  judgements resolve separately: within each, the most specific scope wins (page, then wiki, then
  account), then the newest.
- **A positive judgement never cancels a negative one,** whatever their scopes. Both are shown (notice
  lines, meta tokens, API field) and enforcement follows the negative.
- **Index pages count too.** A judgement on an index page (`index.md` or `README.md`) also applies where
  that page is served without its own path: the wiki root, a folder URL, and the profile `/@user`
  (which renders `@user/user`'s index).

## Enforcement

### Negative judgements in force

Content is never deleted and stays readable by link. WikiHub applies:

- **Notice** at the top of every page in scope, server-rendered. For example: "**Curated against:
  promotional** by WikiHub editors, for this wiki (@user/wiki). Still readable; not recommended by
  WikiHub." It has a *Why?* link to the reason on `/curation`, a reply/appeal link
  (`/curation?judgement=ID#reply`, plus the owner's published reply when `reply_url` is set) and a
  dismiss button.
  - Dismissal is per reader and per judgement (`localStorage`). It hides only the notice; noindex and
    exclusion are unaffected.
  - The owner, when signed in, sees an extra line: the judgement is about their content, nothing was
    removed, and how to reply or appeal.
- **noindex,** server-rendered both as `<meta name="robots" content="noindex">` and as the
  `X-Robots-Tag: noindex` header.
- **`rel="nofollow ugc noopener noreferrer"`** on external links in rendered content. This is
  link-level and is not a robots directive.
- **Left out of discovery:** /explore (including Editorial Picks), /activity, /activity.rss, /people,
  /llms.txt, /llms-full.txt, the landing page's featured wikis and `GET /api/v1/wikis`.
- **Search:** sorted last in `GET /api/v1/search`, not removed.

### How enforcement reuses the trust plumbing

- An account-scope negative judgement makes `is_trusted()` and `trusted_user_clause()` false, so every
  existing trust check applies to it unchanged.
- Wiki and page scopes go through `not_curated_against(...)`, which is added to
  `discoverable_wiki_ids()`, the activity query and the llms queries.
- `moderation.before_request` resolves the judgement for every `/@user/...` URL, including
  subdomain-rewritten ones, and `base.html` renders the notice and meta tag from it.
- Unlike the trust gate, curation applies whether or not `TRUST_GATE_ENABLED` is set, because it is an
  explicit editorial act.

### Positive judgements in force

- `featured`, `official` and `editor_pick` on a wiki, or on a page (which counts for its wiki), add that
  wiki to Explore's Editorial Picks after the official `@wikihub/wikihub` card.
- Pages in scope get a small "Editor's pick by WikiHub editors" line and a `positive:<reason>` meta
  token. Nothing is enforced against them.
- A pick must still be discoverable: a negative judgement or an untrusted owner removes it.
- Account-scope positive judgements are recorded and exported but do not change Explore yet.

### Standards notes

- **noindex must be served, not blocked.** It is server-rendered in both the header and the meta tag.
  `robots.txt` must never disallow wiki URLs: a crawler that is blocked from fetching a page cannot see
  its noindex. Today `robots.txt` only disallows auth endpoints.
- **Sitemap omission is not exclusion.** WikiHub serves no sitemap.xml today. A future one must use
  `trusted_user_clause()` and `not_curated_against()`, but noindex remains the actual control.
- **None of this is access control.** noindex, nofollow and curation do not protect private material;
  the ACL does.
- **`<meta name="wikihub:curation">`** carries space-separated `polarity:reason` tokens, negative first
  (for example `negative:promotional positive:editor_pick`). The same data is in the API `curation`
  field and the export. Documented consumers:
  - the Ideaflow Augment extension (W2), which shows the judgement in its side panel;
  - the Noos ingest of WikiHub pages (W1/D3), which uses the export as the source of truth and the meta
    tag only as a hint for pages it crawls;
  - third-party agents that read pages directly.

  Other crawlers may ignore it. Search engines are governed by the noindex, not by this tag.

## API field

Wiki objects (`GET /api/v1/wikis`, `GET /api/v1/wikis/{owner}/{slug}`), page objects
(`GET .../pages`, `GET .../pages/{path}`) and search results carry `curation`. It is `null`, or:

```json
{"negative": {"id": 7, "polarity": "negative", "reason": "promotional", "label": "promotional",
              "scope": "wiki", "scope_description": "this wiki (@seo/seo)", "status": "active",
              "curator": "WikiHub editors", "policy_version": "2026-11-09", "at": "…",
              "reply_url": null, "info_url": "/curation#promotional",
              "enforcement": ["banner", "noindex", "nofollow_ugc", "excluded_from_discovery", "search_last"]},
 "positive": null}
```

## Operating it

Every mutating command is a dry run unless `--execute` is passed. Each mutation writes a `security`
log event (`admin.curation.flag` / `admin.curation.unflag`).

```bash
flask --app wsgi.py wikihub flag @seo/seo --reason promotional \
    --evidence https://faceswapai.com/ --note "CTA-only page" [--execute]          # wiki (default)
flask --app wsgi.py wikihub flag @someone/wiki/path/to/page --reason off-topic --execute   # one page
flask --app wsgi.py wikihub flag seo --scope account --reason promotional --execute        # whole account, explicit
flask --app wsgi.py wikihub flag @wikihub/wikihub --reason official --execute
flask --app wsgi.py wikihub flag @seo/seo --reason affiliate-farm --supersedes 7 --execute  # replace #7
flask --app wsgi.py wikihub flag @x/y --reason low-quality --expires-days 90 --execute     # self-expiring
flask --app wsgi.py wikihub unflag @seo/seo [--reason promotional] --execute               # status=retracted
flask --app wsgi.py wikihub curation-reply 7 https://seo.wikihub.md/seo/reply --execute    # link the owner's reply
flask --app wsgi.py wikihub curation-list [--all] [--due] [--json]
```

- Reasons accept hyphens (`affiliate-farm`).
- `--polarity` is optional, but if given it must match the reason.
- Flagging something that already has the same judgement in force is a no-op.
- Evidence is public; write it as you would want the owner to read it.

**Replies and appeals.** The owner uses *Send feedback* and cites the judgement number shown in the
notice. An editor then does one of three things:

- retracts the judgement (`unflag`);
- replaces it with a corrected one (`flag … --supersedes ID`);
- links the owner's published reply (`curation-reply`), so readers see both sides.

## Saying what a page is (plan 3d: "no access" is not "not found")

Curation never makes a page look missing:

- A curated-against page answers **200** with the notice. Discovery surfaces leave it out, but a
  direct link always works and the notice says why the page is not recommended.
- A suspended account's URLs answer **410 Gone** with a plain "Account suspended" explanation, not 404.
- WikiHub's existing screens (PR #50) still distinguish "not signed in", "no access" and "does not
  exist".
- The export counts the judgements it may not show (`omitted_non_public`). Only the substance of private
  content (titles, bodies, wiki names) stays hidden.

## Removal is a different action

Curation never removes anything. Removal stays available, as a separate and audited action, for:

- **exposed personal data,** such as doxxing, private contact details or credentials;
- **malicious payloads,** such as malware, phishing or exploit pages;
- **link farms run by one operator** (account suspension).

Tools:

- `wikihub suspend USER [--purge]`: suspended URLs answer 410, and the user row is kept as a
  tombstone.
- `POST /api/v1/admin/delete-page`.

Both write `security` log events (`admin.suspend` and the admin-action records), which are the audit
record. Removal is recorded there and not as a curation judgement, so the curation export never
implies that removed content still exists.

## Export contract (for the Noos backbone)

`GET /api/v1/curation[?since=ISO8601][&cursor=…][&limit=N]`, default limit 500, maximum 1000.

- **WikiHub is the authoritative writer** (`"authoritative": true`). Noos mirrors judgements by
  polling this endpoint; WikiHub never calls Noos.
- **Stable ids.** Upsert by `id`; it is idempotent.
- **Ordering.** Every judgement is listed, in force or not, ordered by `(updated_at, id)`.
  - Retractions and supersessions bump `updated_at`, so incremental polling sees them. Propagate them
    as revocations.
  - Expiry does **not** bump `updated_at`. Compute it from `expires_at`; the row's `status` reads
    `expired` once it passes.
- **Polling.** Pass the previous `next_cursor` (`<updated_at Z>|<id>`), or `since=<last sync, UTC with
  a Z suffix>`.
- **Resync.** A periodic full resync, with no `since`, also catches judgements whose target became
  public after the last poll.
- **Item fields:**
  - `id`, `polarity`, `reason`, `label`, `status`, `in_force`;
  - `scope` `{type, account, wiki, path}`;
  - `url`: the scope's URL in apex form `https://wikihub.md/@user/wiki[/page]`, which answers 301 to
    the canonical subdomain host;
  - `wiki_urls`: for an account scope, its public wikis;
  - `curator` `{name: "WikiHub editors", handle: "wikihub"}`;
  - `evidence`, `affected_version`, `policy_version`, `reply_url`, `superseded_by`;
  - `at` (= `created_at`), `created_at`, `updated_at`, `review_at`, `expires_at`;
  - `enforcement.wikihub`.

  Field names follow plan D4 (`polarity`, `reason`, `scope`, `curator`, `at`, `note`). Reasons use
  underscores, where D4 writes hyphens; a consumer that wants hyphens should map `_` to `-`.
- **Public callers** never get `note` or `curator_user`, and get no row whose target is a private wiki
  or a non-public page. Each response still reports how many it left out (`omitted_non_public`):
  their existence is counted, never named.
- **Admin callers** (`Authorization: Bearer $ADMIN_TOKEN`) get everything.

## Not built here

These belong to other workstreams or later steps:

- a reply form inside WikiHub (replies go through *Send feedback* for now);
- community "flag this" from the reader;
- endogenous quality scores;
- W3C Web Annotation serialization of the export, which the Noos side (W1) owns;
- the reader sidebar showing `/api/about`.

Rollout to production is a separate, operator-run step: see `ops/curation-rollout-2026-11.md`.
