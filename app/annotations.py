"""Reader annotations (wikihub-3add): comments on a page, optionally anchored
to a quoted passage of its rendered text.

Every read is scoped to the requesting viewer with the reader's own rules:
``can_read`` (owner sees all), the public mirror for non-owners unless the
page is private, and private bands stripped for everyone but the owner.

Anchoring is by quote text only (the contract carries no offsets), matched
against the page's *rendered* text with whitespace collapsed so DOM
selection whitespace never matters. That match is deliberately
conservative: a selection must occur exactly once in the viewer's rendered
text when it is posted, and a stored comment counts as attached only while
its quote still occurs exactly once. Zero or several occurrences mean
"detached" -- never a guess.

Owner-only passages: when the owner quotes text that is not uniquely present
in the band-stripped (public) rendering, the comment is stored owner_only
and stays that way. For non-owners, a quote that now exists only inside a
private band (the owner moved it there later) is withheld.
"""

from __future__ import annotations

import uuid
from html.parser import HTMLParser

from app.acl import can_read
from app.content_utils import content_for_viewer
from app.git_sync import read_file_from_repo
from app.page_utils import is_wikihub_plumbing_path
from app.renderer import render_page

MAX_BODY_CHARS = 4000
MAX_QUOTE_CHARS = 1000
MAX_CITATIONS = 10
CONTEXT_CHARS = 32  # rendered-text context stored on each side of a quote


class ContentUnavailable(RuntimeError):
    """The page's content could not be read. Callers fail closed (503):
    never treat an unreadable repository as an empty page."""


# Who can read a comment posted on this page (shown before approval and
# rechecked at commit).
AUDIENCE_LABELS = {
    "public": "Anyone who can read this public page",
    "link": "Anyone with the link to this unlisted page",
    "restricted": "The wiki owner and people with access to this private page",
    "owner": "Owner only (the passage is in a private section)",
}


def comment_audience(view, owner_only):
    from app.acl import normalize_page_visibility, resolve_visibility

    if owner_only:
        return "owner"
    effective = resolve_visibility(view.page.path, view.acl_rules, view.page.visibility)
    vis = normalize_page_visibility(effective) or "private"
    if vis in ("public", "public-edit", "public-view"):
        return "public"
    if vis in ("unlisted", "unlisted-edit", "unlisted-view"):
        return "link"
    return "restricted"


def ensure_annotation_columns(db):
    """Idempotent in-place upgrade for databases created before these
    columns existed (e.g. a preview database). create_all() does not alter
    existing tables. Caller commits."""
    for ddl in (
        "ALTER TABLE page_annotations ADD COLUMN IF NOT EXISTS anchor_prefix TEXT NULL",
        "ALTER TABLE page_annotations ADD COLUMN IF NOT EXISTS anchor_suffix TEXT NULL",
        "ALTER TABLE page_annotations ADD COLUMN IF NOT EXISTS citations JSON NULL",
    ):
        db.session.execute(db.text(ddl))

_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "details", "div", "dl", "dt",
    "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
    "li", "main", "nav", "ol", "p", "pre", "section", "summary", "table", "tbody", "td",
    "tfoot", "th", "thead", "tr", "ul",
}
_SKIP_TAGS = {"script", "style", "template", "noscript"}
_SKIP_CLASSES = {"private-band-label"}  # owner-view chrome, not page text
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class _TextExtractor(HTMLParser):
    """Visible text of rendered page HTML, plus which of it is inside an
    owner-view private section (<section class="private-band">)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []  # (text, in_private_band)
        self._skip_stack = []  # tag names of open skipped elements
        self._sections = []  # one bool per open <section>: is it a private band?

    def _in_band(self):
        return any(self._sections)

    def handle_starttag(self, tag, attrs):
        classes = set((dict(attrs).get("class") or "").split())
        if self._skip_stack or tag in _SKIP_TAGS or classes & _SKIP_CLASSES:
            if tag not in _VOID_TAGS:
                self._skip_stack.append(tag)
            return
        if tag == "section":
            self._sections.append("private-band" in classes)
        if tag in _BLOCK_TAGS:
            self.parts.append((" ", self._in_band()))

    def handle_endtag(self, tag):
        if self._skip_stack:
            for i in range(len(self._skip_stack) - 1, -1, -1):
                if self._skip_stack[i] == tag:
                    del self._skip_stack[i:]
                    break
            return
        if tag in _BLOCK_TAGS:
            self.parts.append((" ", self._in_band()))
        if tag == "section" and self._sections:
            self._sections.pop()

    def handle_data(self, data):
        if not self._skip_stack:
            self.parts.append((data, self._in_band()))


def html_text_and_mask(html):
    """(text, mask): whitespace collapsed exactly like html_text(); mask[i]
    is True where text[i] comes from inside a private band."""
    parser = _TextExtractor()
    parser.feed(html or "")
    parser.close()
    out, mask, pending = [], [], False
    for chunk, in_band in parser.parts:
        for ch in chunk:
            if ch.isspace():
                pending = True
                continue
            if pending and out:
                out.append(" ")
                mask.append(False)
            pending = False
            out.append(ch)
            mask.append(in_band)
    return "".join(out), mask


def html_text(html):
    return html_text_and_mask(html)[0]


def match_key(text):
    return " ".join((text or "").split())


def count_occurrences(haystack_key, needle_key, limit=None):
    """Overlapping occurrence count (an overlap is still an ambiguity).
    limit: stop counting once this many are found."""
    if not needle_key:
        return 0
    count, start = 0, 0
    while True:
        at = haystack_key.find(needle_key, start)
        if at < 0:
            return count
        count += 1
        if limit is not None and count >= limit:
            return count
        start = at + 1


class PageView:
    """One viewer's view of one page: access, text, and the owner's full text
    when needed for protecting band passages (never returned to non-owners)."""

    def __init__(self, owner, wiki, page, viewer, acl_rules):
        self.owner = owner
        self.wiki = wiki
        self.page = page
        self.viewer = viewer
        self.acl_rules = acl_rules
        self.is_owner = bool(viewer and viewer.id == wiki.owner_id)
        self._raw = None
        self._texts = {}

    def can_read(self):
        page = self.page
        if page is None or is_wikihub_plumbing_path(page.path):
            return False
        if self.is_owner:
            return True
        return can_read(page.path, self.acl_rules, self.viewer.username if self.viewer else None, page.visibility)

    def _authoritative(self):
        if self._raw is None:
            raw = read_file_from_repo(self.owner.username, self.wiki.slug, self.page.path, public=False)
            if raw is None:
                # Indexed page but unreadable content: fail closed. Treating it
                # as "" would make band-anchored comments look public.
                raise ContentUnavailable(self.page.path)
            self._raw = raw
        return self._raw

    def _viewer_markdown(self):
        if self.is_owner:
            return self._authoritative()
        # Same source choice as the reader: mirror for non-private pages,
        # authoritative for private pages the viewer was granted; bands
        # stripped either way (fails closed if stripping fails).
        content = None
        if self.page.visibility != "private":
            content = read_file_from_repo(self.owner.username, self.wiki.slug, self.page.path, public=True)
        if content is None:
            content = self._authoritative()
        try:
            stripped = content_for_viewer(content, viewer_is_owner=False)
        except Exception as exc:  # band stripping failed: never show raw
            raise ContentUnavailable(self.page.path) from exc
        if stripped is None:
            raise ContentUnavailable(self.page.path)
        return stripped

    def _render_key(self, markdown, owner_view, with_mask=False):
        html = render_page(
            markdown, self.owner.username, self.wiki.slug,
            current_page_path=self.page.path, mark_private_bands=owner_view,
        )
        text, mask = html_text_and_mask(html)
        return (text, mask) if with_mask else text

    def full_band_mask(self):
        """Owner-view text and its private-band mask (server-side only)."""
        if "full_mask" not in self._texts:
            text, mask = self._render_key(self._authoritative(), owner_view=True, with_mask=True)
            self._texts["full"] = text
            self._texts["full_mask"] = mask
        return self._texts["full"], self._texts["full_mask"]

    def text_key(self, which="viewer"):
        """'viewer': what this viewer sees. 'public': band-stripped owner text.
        'full': everything (owner view). Only 'viewer' is ever exposed."""
        if which not in self._texts:
            if which == "viewer":
                key = self._render_key(self._viewer_markdown(), owner_view=self.is_owner)
            elif which == "public":
                try:
                    public = content_for_viewer(self._authoritative(), viewer_is_owner=False)
                except Exception as exc:
                    raise ContentUnavailable(self.page.path) from exc
                key = self._render_key(public or "", owner_view=False)
            else:
                key = self._render_key(self._authoritative(), owner_view=True)
            self._texts[which] = key
        return self._texts[which]


def parse_request_id(value):
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value.strip()))
    except (ValueError, AttributeError):
        return None


def rekey_after_reset(wiki_id, prior_ids):
    """After a reset reindex recreated page rows, point annotations that were
    on the OLD row of a path at the NEW row of the same path. Only exact
    old-id matches move, so comments from an earlier, deleted incarnation of
    the page stay marked as such. Caller commits."""
    from app import db
    from app.models import Page, PageAnnotation

    if not prior_ids:
        return 0
    annotated_pages = (db.session.query(Page.path, Page.id, PageAnnotation.page_id)
                       .join(PageAnnotation, db.and_(PageAnnotation.wiki_id == Page.wiki_id,
                                                    PageAnnotation.page_path == Page.path))
                       .filter(Page.wiki_id == wiki_id)
                       .distinct().all())
    moved = 0
    for path, new_id, old_id in annotated_pages:
        if old_id is None or prior_ids.get(path) != old_id or new_id == old_id:
            continue
        moved += (PageAnnotation.query
                  .filter_by(wiki_id=wiki_id, page_path=path, page_id=old_id)
                  .update({"page_id": new_id}, synchronize_session=False))
    db.session.flush()
    return moved


def anchor_context(text_key, quote_key):
    """(prefix, suffix) around the first occurrence of quote_key."""
    at = text_key.find(quote_key)
    if at < 0:
        return None, None
    end = at + len(quote_key)
    return text_key[max(0, at - CONTEXT_CHARS):at], text_key[end:end + CONTEXT_CHARS]


def context_matches(text_key, quote_key, prefix, suffix):
    """How many occurrences of quote_key sit between exactly this prefix and
    suffix (the context recorded when the comment was made)."""
    count, start = 0, 0
    while quote_key:
        at = text_key.find(quote_key, start)
        if at < 0:
            break
        end = at + len(quote_key)
        if (at >= len(prefix) and text_key[at - len(prefix):at] == prefix
                and text_key[end:end + len(suffix)] == suffix):
            count += 1
        start = at + 1
    return count


def _common_suffix_len(a, b):
    n = 0
    while n < len(a) and n < len(b) and a[-1 - n] == b[-1 - n]:
        n += 1
    return n


def _common_prefix_len(a, b):
    n = 0
    while n < len(a) and n < len(b) and a[n] == b[n]:
        n += 1
    return n


def best_context(text_key, quote_key, prefix, suffix):
    """(best score, start offsets of the occurrences that share it) for
    occurrences of quote_key, scored by how much of the recorded
    prefix/suffix still surrounds them. (-1, []) when the words are absent."""
    best, positions, start = -1, [], 0
    while quote_key:
        at = text_key.find(quote_key, start)
        if at < 0:
            break
        end = at + len(quote_key)
        score = (_common_suffix_len(text_key[max(0, at - len(prefix)):at], prefix)
                 + _common_prefix_len(text_key[end:end + len(suffix)], suffix))
        if score > best:
            best, positions = score, [at]
        elif score == best:
            positions.append(at)
        start = at + 1
    return best, positions


def _anchor_state(annotation, view, q):
    """'attached' | 'detached' | 'hidden' for this viewer.

    The commented occurrence is the one whose surroundings best match the
    context recorded at creation, so a copy of the same words elsewhere
    (public or in a private band) never stands in for it. For non-owners:
    if the best match exists only in the owner's full text -- the commented
    passage is inside a private section now -- the comment is hidden; ties
    that only the full text has are treated the same way (any doubt hides).
    Attachment itself needs the words exactly once in the viewer's text, as
    the reader highlights only unambiguous passages."""
    viewer_text = view.text_key("viewer")
    visible = count_occurrences(viewer_text, q)
    if view.is_owner:
        return "attached" if visible == 1 else "detached"
    prefix, suffix = annotation.anchor_prefix, annotation.anchor_suffix
    full_text, band_mask = view.full_band_mask()
    if prefix is None or suffix is None:  # comments made before context was stored
        full = count_occurrences(full_text, q)
        if full > visible or (visible == 0 and full >= 1):
            return "hidden"
        return "attached" if visible == 1 else "detached"
    _, best_positions = best_context(full_text, q, prefix, suffix)
    if any(any(band_mask[at:at + len(q)]) for at in best_positions):
        # The best match for the commented occurrence (or a tie with it)
        # lies inside a private section now.
        return "hidden"
    if visible == 0 and best_positions:
        return "hidden"  # the words remain only where this viewer can't see
    return "attached" if visible == 1 else "detached"


def serialize(annotation, view, author_names, citations=None):
    """Viewer-scoped JSON for one comment, or None if this viewer must not
    see it at all.

    Hidden from non-owners (shown to the owner as detached):
    - comments made on an earlier incarnation of the page (deleted and
      recreated at the same path; page id differs);
    - anchored comments whose commented passage now exists only inside an
      owner-only private section (see _anchor_state).
    Raises ContentUnavailable when the page content cannot be read."""
    quote = annotation.quote or ""
    detached = False
    other_incarnation = annotation.page_id is not None and annotation.page_id != view.page.id
    if other_incarnation:
        if not view.is_owner:
            return None
        detached = True
    if quote and not detached:
        state = _anchor_state(annotation, view, match_key(quote))
        if state == "hidden":
            return None
        detached = state == "detached"
    return {
        "id": annotation.id,
        "author": author_names.get(annotation.author_id),
        "body": annotation.body,
        "quote": quote,
        "detached": detached,
        "owner_only": bool(annotation.owner_only),
        "audience": comment_audience(view, annotation.owner_only),
        "citations": citations if citations is not None else [],
        "created_at": annotation.created_at.isoformat() if annotation.created_at else None,
    }
