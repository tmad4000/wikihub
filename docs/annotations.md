# Reader annotations and assistant drafts (wikihub-3add)

The reader shows comments below the article. Select text to attach a passage, or use Comment on page. Ask can suggest a comment or page with citations. The reader previews the exact text, target and audience before the signed-in visitor approves the write.

## Annotations

`GET|POST /@<username>/<slug>/-/pages/<page_id>/annotations`

Host forms, all served by the same route (the subdomain middleware rewrites them):
apex `wikihub.md/@<user>/<slug>/-/pages/<id>/annotations`, user subdomain
`<user>.wikihub.md/<slug>/-/pages/<id>/annotations`, wiki subdomain or custom domain
`<host>/-/pages/<id>/annotations`. Use the form for the host the reader is on, because
session cookies are per host. The apex never 301s this path.

**CSRF:** both methods need the session token in `X-CSRFToken` (the `<meta
name="csrf-token">` value). The base template's fetch wrapper adds it to non-GET
requests only. **For GET the UI must add the header itself.** A missing or invalid
token returns `400 {"error":"csrf_failed"}` (GET) or Flask-WTF's 400 (POST).

**GET** (anyone who can read the page, signed in or not):

```json
{"comments": [{"id": 1, "author": "alice", "body": "…", "quote": "…",
               "detached": false, "owner_only": false, "created_at": "2026-11-25T…"}],
 "revision": "<pages.content_hash>", "viewer_id": 42}
```

- Each request checks up to 50 candidate comments and returns the visible ones in chronological order. A batch can be empty and still have a continuation cursor. Pass the returned `before` cursor on GET for the preceding page. A null cursor indicates the start of the thread.
- `quote` is `""` for a whole-page comment.
- `viewer_id` is the signed-in user's id, or `null` when signed out. Send it back
  on POST.
- **Hidden from non-owners** (the owner sees these with `detached: true`):
  - comments from an earlier incarnation of the page: the page was deleted and
    recreated at the same path. A reset reindex does *not* count; comments
    keep their page identity across it.
  - anchored comments whose originally public passage now exists only inside
    an owner-only private section. Both the quote and the comment are hidden.
- `detached` is true when the quote no longer occurs exactly once in the text this
  viewer sees (removed, or now ambiguous). Never guessed.
- `owner_only` comments are returned only to the wiki owner.
- `created_at`, and the top-level `revision` and `viewer_id`, are additions to the agreed contract.
  The UI can use `revision` for the next POST.
- Errors: `401 authentication_required` (anonymous on a restricted page),
  `403 forbidden` (signed in, no read access), `404 not_found` (no such page in this
  wiki).

**POST** (signed-in reader with read access), JSON:

```json
{"body": "text", "quote": "selected text or empty", "request_id": "<uuid4>",
 "revision": "<content_hash>", "viewer_id": 42}
```

| Result | Status | Body |
|---|---|---|
| created | 201 | `{"comment": {…}}` |
| retry of the same `request_id` with the same body/quote/page | 200 | `{"comment": {…original…}, "duplicate": true}`. `comment` is `null` if that comment is now hidden from this viewer |
| `viewer_id` missing or not an integer | 400 | `invalid_viewer_id` |
| `viewer_id` ≠ signed-in user (stale tab after sign-out or account switch) | 409 | `viewer_mismatch` (reload) |
| same `request_id`, different content | 409 | `request_id_reused` |
| `revision` ≠ current page hash | 409 | `stale_revision` + `revision` (current) |
| quote not in the viewer's rendered text | 409 | `selection_not_found` |
| quote occurs more than once | 409 | `ambiguous_selection` (ask for a longer selection) |
| empty / >4000-char body | 400 | `invalid_body` |
| quote not a string / >1000 chars | 400 | `invalid_quote` |
| bad UUID | 400 | `invalid_request_id` |
| missing revision | 400 | `invalid_revision` |
| not JSON | 415 | `unsupported_media_type` |
| anonymous | 401 | `authentication_required` |
| rate limited | 429 | `rate_limited` + `retry_after` (header `Retry-After`) |

Retry dedup is checked before the revision and rate limits, so a retried POST always
gets its original comment back. A concurrent double-submit is resolved through the
`(author_id, request_id)` unique constraint.

**Anchoring:** the quote is compared against the page's server-rendered text with
whitespace runs and block boundaries collapsed to a single space.
Text that only exists after client-side rendering (KaTeX math, sidepeek chrome) will
not match. The UI should send the selection's plain text (`Selection.toString()`).

**Privacy rules:**
- Every read uses the viewer's own access: `can_read`, and the public mirror for
  non-owners of non-private pages. Private sections are stripped for everyone but
  the owner.
- Only the owner can anchor to text inside a private section. Such a comment is
  stored `owner_only=true`, and it never becomes visible to others, even if the
  section is later made public.
- If an owner later moves an already-commented public passage into a private
  section, that comment is hidden from non-owners.
- The comment **body** is reader-authored text and is visible to every reader of the
  page. The UI must render it as plain text, never as HTML.
- Comments by suspended accounts are hidden.

**Durability:** rows are keyed by `(wiki_id, page_path)`, not by page id, because
page ids change when a wiki is reindexed with reset. Consequences:
- A comment follows its path. Renaming a page leaves its comments at the old path.
- If a page is deleted and later recreated at the same path, the old comments
  are hidden from non-owners and shown to the owner as detached. Detection uses
  the stored page id, which a reset reindex carries over for the same path.
- There is no delete or moderation endpoint yet (follow-up).

**Rate limits** (Postgres buckets shared across workers; env-configurable):

- `ANNOTATION_MAX_PER_USER_PER_MINUTE`: 6
- `ANNOTATION_MAX_PER_USER_PER_HOUR`: 60
- `ANNOTATION_MAX_PER_USER_PER_DAY`: 300
- `ANNOTATION_MAX_PER_PAGE_PER_DAY`: 500

## Assistant drafts: `POST /@<username>/<slug>/new` with `assistant_draft=1`

This extends the existing new-page form POST. Without `assistant_draft=1` the
behavior is unchanged. Form fields:

- `assistant_draft=1`
- `viewer_id`: the signed-in user's id, from the tab that previewed the draft.
  Missing or non-numeric gives `400 invalid_viewer_id`; a different user gives
  `409 viewer_mismatch`.
- `approved=1`: explicit approval. Without it: `400 approval_required`.
- `path`: a simple Markdown path. Allowed characters are letters, digits, space,
  `_ - . ( )` and `/` separators. Segments can't be empty, `.`, `..`, or start with
  a dot (this blocks `.wikihub` and `.git`), and can't have leading or trailing
  spaces. No backslash, no absolute path, at most 200 characters. `.md` is
  appended if missing. Otherwise: `400 invalid_path`.
- `content`: non-empty Markdown up to `MAX_PAGE_SIZE`.
- `previewed_path` (fully qualified `page_path`), `previewed_visibility` and `previewed_content_hash`: **required for everyone**,
  taken from the preview below. Both are recomputed at commit, together with
  the ACL. Any difference returns `409 preview_changed` with `visibility` and a
  fresh `preview`, and nothing is written.
- CSRF token as for any form POST (`csrf_token` field or `X-CSRFToken` header).

Rules:

- **Sign-in:** required. Otherwise `401 authentication_required`.
- **Authorization:** checked on the final normalized path. The owner always may;
  others need `can_write` there. Otherwise `403 forbidden`.
- **Owner's drafts:** always created **private**, whatever their frontmatter says.
- **Non-owner's drafts:** their frontmatter visibility is forced to the inherited
  default, matching the existing contributor rule (wikihub-tq01).
- **No overwrite:** an existing page, or any file at the path, returns
  `409 exists`.
- **Repeats:** an exact repeat by the same user returns success with
  `created: false`, covering both a double submit and a race lost on the unique
  constraint. A different user's identical text still gets `exists`.
- **Rate limit:** `ASSISTANT_DRAFT_MAX_PER_USER_PER_HOUR` (default 30) gives
  `429 rate_limited`. A repeat doesn't count against it.
- **Responses:** with `Accept: application/json`, success is `201` (or `200` for a
  repeat) with `{"path", "page_id", "url", "visibility", "created"}`, and errors
  are `{"error", "message", …}`. Without it, success is a `303` redirect to the
  page and errors are plain HTTP errors.
- **Audit:** a `security_event` `assistant_draft.create` is logged.

## Review follow-ups (feat/annotations-review-3add)

These supersede anything above that disagrees.

**Fail closed.** If an indexed page's content can't be read, or band stripping
fails, GET and POST return `503 {"error":"content_unavailable"}`. An unreadable
repository is never treated as an empty page, which would make band-anchored
comments look public.

**Anchoring across private sections.**
- Each comment stores up to 32 characters of rendered-text context on each
  side of its quote (`anchor_prefix`/`anchor_suffix`, never returned).
- For non-owners, the commented occurrence is whichever copy of the words best
  matches that context in the owner's full text. If that occurrence, or a tie
  with it, lies inside a private section, the comment is **hidden**. If the
  words now exist only where the viewer can't see them, it is also hidden.
- A copy of the same words added inside a band elsewhere changes nothing for
  readers, so there is no side channel.
- `detached` still means "not exactly once in this viewer's text".
- Comments made before context was stored use the conservative rule: hidden
  if any copy of the words is private.

**Audience: shown before approval, rechecked at commit.**

`POST …/annotations` with `{"preview": true, "quote", "revision", "viewer_id"}`
writes nothing and returns:

```json
{"preview": {"audience": "public|link|restricted|owner",
             "audience_label": "Anyone who can read this public page",
             "owner_only": false, "page_path": "@owner/wiki/path.md",
             "page_id": 1, "quote": "…", "revision": "<hash>"}}
```

- The real POST must include `"audience": "<value from the preview>"`. It is
  recomputed at commit, and a difference is
  `409 audience_changed` + `audience`, `audience_label`, `page_path`.
- Each GET comment also carries `audience`.

**Citations stay navigable and safe.**
- POST may include `"citations": [{"n": 1, "url": "/@owner/wiki/page#:~:text=…"}]`,
  at most 10. Only same-site wiki page paths are accepted, and the author must
  be able to read each target. Otherwise: `400 invalid_citations`.
- Each GET comment returns `citations: [{n, title, url, quote}]`, resolved
  **for the current viewer**: targets they can't read are omitted.
- A quote (and its `#:~:text=` link) is kept only if the words are in the
  viewer's own view of the target. A quote from a private section never
  reaches readers.
- The UI turns `[n]` in the body into links and lists the sources, using DOM
  APIs only.

**Budgets.**
- `ANNOTATION_MAX_ATTEMPTS_PER_USER_PER_MINUTE` (30) throttles every write
  attempt.
- The comment budgets (per user per minute/hour/day, and per page per day) are
  checked before writing and charged only after a comment is stored. Rejected,
  invalid, duplicate and preview requests are free.
- Page buckets are keyed by a SHA-256 of the full path, so long paths never
  share a budget.
- The draft budget works the same way: checked first, charged only when a page
  is created. Collisions and rejections are free.

**Assistant drafts: preview, round trip, recovery.**
- `assistant_draft=1&preview=1`, with the same fields minus `approved`, writes
  nothing and returns `{"preview": {path, page_path, url, visibility,
  audience_label, content, content_hash}}`. `content` is the **exact Markdown
  that will be saved**, after the visibility rules: owner drafts are rewritten
  to `visibility: private`.
- **Round trip:** the page's URL must route back to the reader for exactly this
  path. `400 invalid_path` is returned for:
  - reserved first segments (`history`, `graph`, `activity`, `edit`, `tag`, …)
    and any `edit`/`history` segment;
  - folder names ending in `.md`;
  - mixing spaces and underscores;
  - a space/underscore twin of an existing page, since both would share one
    address.

  The `GET ?assistant_target=` check applies the same rules.
- **Recovery:** if a previous submission's Git commit landed but its DB
  commit didn't, an identical retry by the same user indexes the page and
  answers `200 {"created": false, "recovered": true}`. A recovery requires all
  of: same content, same author, and the assistant-draft commit subject. A
  file written by anyone else is still `409 exists`.
- Success responses also include `page_path`.

**DB.**
- New nullable columns: `page_annotations.anchor_prefix`, `anchor_suffix`
  (TEXT) and `citations` (JSON).
- They are added in place at startup (`ensure_annotation_columns`), so
  existing databases such as the M5 preview DB upgrade on restart. The
  migration file adds them too.
- Verified against a preview DB created by the reviewed snapshot (`734ce05`)
  through `demos/reader-conversations/server.py`.

## Release notes

1. **Database:** new table `page_annotations`.
   - The app creates it at startup through `db.create_all()`, as the app role,
     so a normal deploy and restart is enough.
   - To create it ahead of the restart (optional), apply
     `migrations/2026-11-25_page_annotations.sql`. Its header has the commands;
     it is idempotent and transfers ownership to the role that owns `wikis`.
   - Either way, check afterwards that `page_annotations` and
     `page_annotations_id_seq` are owned by the app role (`wikihub`). The query is
     in the file header.
2. **Config:** none is required. The limits above have defaults and are optional
   env vars.
3. **nginx:** no change. The endpoints are ordinary `/@…` routes; POSTs are small
   JSON or form posts.
4. **Rollback:**
   - Reverting the code is safe; the table is simply unused.
   - To also remove the data: `DROP TABLE page_annotations;`. This is
     irreversible, so take a backup first.
5. **Not included:** comment delete/moderation, and page-rename rekeying.

Regression tests:

- `test_page_annotations_scope_anchoring_and_dedup`
- `test_page_annotations_migration_preserves_app_owner`
- `test_assistant_draft_new_page_rules`
