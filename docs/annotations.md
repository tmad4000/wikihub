# Reader annotations and assistant drafts (wikihub-3add)

The reader shows comments below the article. Select text to attach a passage, or use Comment on page. Ask can suggest a comment or page with citations. The reader previews the exact text, target and audience before the signed-in visitor approves the write.

## Annotations

`GET|POST /@<username>/<slug>/-/pages/<page_id>/annotations`

Host forms, all served by the same route (the subdomain middleware rewrites them):
apex `wikihub.md/@<user>/<slug>/-/pages/<id>/annotations`, user subdomain
`<user>.wikihub.md/<slug>/-/pages/<id>/annotations`, wiki subdomain or custom domain
`<host>/-/pages/<id>/annotations`. Use the form for the host the reader is on, because
session cookies default to the current host. The apex never 301s this path.

**CSRF:** both methods need the session token in `X-CSRFToken` (the `<meta
name="csrf-token">` value). The base template's fetch wrapper adds it to non-GET
requests only. **For GET the UI must add the header itself.** A missing or invalid
token returns `400 {"error":"csrf_failed"}` (GET) or Flask-WTF's 400 (POST).

**GET** (anyone who can read the page, signed in or not):

```json
{"comments": [{"id": 1, "author": "alice", "body": "…", "quote": "…",
               "detached": false, "owner_only": false, "created_at": "2026-11-25T…"}],
 "before": null, "revision": "<pages.content_hash>", "viewer_id": 42}
```

- Each request checks up to 50 candidate comments and returns the visible ones in chronological order. A batch can be empty and still have a continuation cursor. Pass the opaque `before` cursor on GET as the same viewer on the same page to fetch the preceding batch. An invalid cursor returns `400 invalid_cursor`. A null cursor indicates the start of the thread.
- `quote` is `""` for a whole-page comment.
- `viewer_id` is the signed-in user's id, or `null` when signed out. Send it back
  on POST.
- **Hidden from non-owners**:
  - comments from an earlier incarnation of the page: the page was deleted and
    recreated at the same path. A reset reindex does *not* count; comments
    keep their page identity across it.
  - anchored comments whose originally public passage now exists only inside
    an owner-only private section. Both the quote and the comment are hidden.
- `detached` is true when the quote no longer occurs exactly once in the text this
  viewer sees (removed, or now ambiguous). Never guessed.
- `owner_only` comments are returned only to the wiki owner.
- Send the returned `revision` when previewing or posting a comment.
- Errors: `401 authentication_required` (anonymous on a restricted page),
  `403 forbidden` (signed in, no read access), `404 not_found` (no such page in this
  wiki).

**Audience preview**: POST to the same endpoint with `?preview=1`, sending
`viewer_id`, `revision`, and `quote`. This checks the passage without writing a
comment and returns `audience` (`owner_only`, `private`, `unlisted`, or `public`).
Show this audience before approval and send it with the approved comment.

**POST** (signed-in reader with read access), JSON:

```json
{"body": "text", "quote": "selected text or empty", "request_id": "<uuid4>",
 "revision": "<content_hash>", "viewer_id": 42, "previewed_audience": "public"}
```

| Result | Status | Body |
|---|---|---|
| missing or changed audience | 409 | `audience_changed` with current `audience`; show it and require approval again |
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

After access checks, the server checks for a duplicate before comparing revisions
or applying rate limits. It filters the original comment for the current viewer. Concurrent submissions share the
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
- The body and quote share the comment’s audience. Render the body as plain text.
- Comments by suspended accounts are hidden.

**Durability:** rows are keyed by `(wiki_id, page_path)`, not by page id, because
page ids change when a wiki is reindexed with reset. Consequences:
- A comment follows its path. Renaming a page leaves its comments at the old path.
- If a page is deleted and later recreated at the same path, the old comments
  are hidden from non-owners and shown to the owner as detached. Detection uses
  the stored page id, which a reset reindex carries over for the same path.
- There is no delete or moderation endpoint yet (follow-up).

Comment submissions use per-user minute, hour and day limits plus a daily
per-page limit. Draft creation uses a per-user hourly limit. The environment
settings and their defaults are in [config.py](../config.py).

## Assistant drafts: `POST /@<username>/<slug>/new` with `assistant_draft=1`

Before approval, a signed-in contributor checks the destination with
`GET /@<username>/<slug>/new?assistant_target=<path>`. A successful check
returns the canonical `path` and `visibility`. Changing the filename requires
another check. Owners see a private audience for every new draft.

The signed-in reader submits the following fields on the new-page form:

- `assistant_draft=1`
- `viewer_id`: the signed-in user's id, from the tab that previewed the draft.
  Missing or non-numeric gives `400 invalid_viewer_id`; a different user gives
  `409 viewer_mismatch`.
- `approved=1`: explicit approval. Without it: `400 approval_required`.
- `path`: a simple Markdown path. Each segment starts with an ASCII letter, digit
  or underscore. Subsequent characters can also be spaces, hyphens, periods or
  parentheses. Use a relative path with slash-separated segments and no trailing
  spaces in a segment. The input limit is 200 characters after trimming. The
  server appends `.md` if missing. Invalid paths return `400 invalid_path`.
- `content`: Markdown with at least one non-whitespace character, up to `MAX_PAGE_SIZE` UTF-8 bytes.
- `previewed_visibility`: **required for non-owners.** It must equal the
  visibility the page will actually get, which comes from the ACL/inherited
  default. Otherwise: `409 visibility_mismatch` with `visibility`, and nothing
  is written.
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
- **Rate limit:** exceeding the hourly limit returns
  `429 rate_limited`. A matching existing page returns before the rate check.
- **Responses:** with `Accept: application/json`, success is `201` (or `200` for a
  repeat) with `{"path", "page_id", "url", "visibility", "created"}`, and errors
  are `{"error", "message", …}`. Without it, success is a `303` redirect to the
  page and errors are plain HTTP errors.
- **Audit:** a `security_event` `assistant_draft.create` is logged.

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
