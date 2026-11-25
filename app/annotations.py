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
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip_stack = []  # tag names of open skipped elements

    def handle_starttag(self, tag, attrs):
        classes = set((dict(attrs).get("class") or "").split())
        if self._skip_stack or tag in _SKIP_TAGS or classes & _SKIP_CLASSES:
            if tag not in _VOID_TAGS:
                self._skip_stack.append(tag)
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if self._skip_stack:
            for i in range(len(self._skip_stack) - 1, -1, -1):
                if self._skip_stack[i] == tag:
                    del self._skip_stack[i:]
                    break
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self._skip_stack:
            self.parts.append(data)


def html_text(html):
    parser = _TextExtractor()
    parser.feed(html or "")
    parser.close()
    return " ".join("".join(parser.parts).split())


def match_key(text):
    return " ".join((text or "").split())


def count_occurrences(haystack_key, needle_key):
    """Overlapping occurrence count (an overlap is still an ambiguity)."""
    if not needle_key:
        return 0
    count, start = 0, 0
    while True:
        at = haystack_key.find(needle_key, start)
        if at < 0:
            return count
        count += 1
        if count > 1:
            return count  # callers only distinguish 0 / 1 / many
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
            self._raw = read_file_from_repo(self.owner.username, self.wiki.slug, self.page.path, public=False) or ""
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
        return content_for_viewer(content, viewer_is_owner=False) or ""

    def _render_key(self, markdown, owner_view):
        html = render_page(
            markdown, self.owner.username, self.wiki.slug,
            current_page_path=self.page.path, mark_private_bands=owner_view,
        )
        return match_key(html_text(html))

    def text_key(self, which="viewer"):
        """'viewer': what this viewer sees. 'public': band-stripped owner text.
        'full': everything (owner view). Only 'viewer' is ever exposed."""
        if which not in self._texts:
            if which == "viewer":
                key = self._render_key(self._viewer_markdown(), owner_view=self.is_owner)
            elif which == "public":
                key = self._render_key(content_for_viewer(self._authoritative(), viewer_is_owner=False) or "", owner_view=False)
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
    new_ids = dict(Page.query.with_entities(Page.path, Page.id)
                   .filter(Page.wiki_id == wiki_id, Page.path.in_(list(prior_ids))).all())
    moved = 0
    for path, old_id in prior_ids.items():
        new_id = new_ids.get(path)
        if new_id is None or new_id == old_id:
            continue
        moved += (PageAnnotation.query
                  .filter_by(wiki_id=wiki_id, page_path=path, page_id=old_id)
                  .update({"page_id": new_id}, synchronize_session=False))
    db.session.flush()
    return moved


def serialize(annotation, view, author_names):
    """Viewer-scoped JSON for one comment, or None if this viewer must not
    see it at all.

    Hidden from non-owners (shown to the owner as detached):
    - comments made on an earlier incarnation of the page (deleted and
      recreated at the same path; page id differs);
    - anchored comments whose originally public passage now exists only
      inside an owner-only private band -- the quote and the comment
      would reveal or discuss band text."""
    quote = annotation.quote or ""
    detached = False
    other_incarnation = annotation.page_id is not None and annotation.page_id != view.page.id
    if other_incarnation:
        if not view.is_owner:
            return None
        detached = True
    if quote and not detached:
        q = match_key(quote)
        visible_matches = count_occurrences(view.text_key("viewer"), q)
        detached = visible_matches != 1
        if visible_matches == 0 and not view.is_owner and count_occurrences(view.text_key("full"), q) >= 1:
            return None
    return {
        "id": annotation.id,
        "author": author_names.get(annotation.author_id),
        "body": annotation.body,
        "quote": quote,
        "detached": detached,
        "owner_only": bool(annotation.owner_only),
        "created_at": annotation.created_at.isoformat() if annotation.created_at else None,
    }
