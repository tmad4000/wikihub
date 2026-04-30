"""
backlinks: incoming wikilink references for a page.

Two surfaces use this:
- the reader view (templates/reader.html → routes/wiki.py:_get_backlinks)
- the API endpoint (GET .../pages/<path>/backlinks; ?include=backlinks on read_page)

Resolution strategy:
- Primary: Wikilink rows whose target_page_id == page.id
  (set at refresh-time when the link target resolves cleanly).
- Fallback: Wikilink rows in the same wiki where target_page_id IS NULL
  but target_path matches one of the page's aliases.
  This catches forward references — page A links to [[B]] before B exists,
  and once B is created we want B to show A as a backlink without forcing
  every page in the wiki to be re-refreshed.

Cross-wiki backlinks are intentionally out of scope for v1 — wikilinks resolve
within a single wiki today. Tracked in wikihub-yqe6.
"""

from sqlalchemy import or_

from app import db
from app.content_utils import page_reference_aliases
from app.models import Page, Wikilink


def get_backlinks_for_page(page):
    """Return a list of Page objects that wikilink to `page`.

    Sorted by source page path for stable ordering. De-duplicated — even if
    one source page has multiple wikilinks to this target, it appears once.
    """
    if not page or not getattr(page, "id", None):
        return []

    aliases = page_reference_aliases(page.path, page.title)

    # Two queries unioned at the Python level — easier to reason about than
    # a single SQL OR with a subquery, and the second is rare in practice.
    primary_ids = {
        wl.source_page_id
        for wl in Wikilink.query.filter_by(target_page_id=page.id).all()
    }

    # Forward-ref fallback: same wiki, unresolved target, alias match.
    alias_match_ids = set()
    if aliases:
        rows = (
            db.session.query(Wikilink.source_page_id)
            .join(Page, Wikilink.source_page_id == Page.id)
            .filter(
                Page.wiki_id == page.wiki_id,
                Wikilink.target_page_id.is_(None),
                Wikilink.target_path.in_(aliases),
            )
            .all()
        )
        alias_match_ids = {row[0] for row in rows}

    source_ids = primary_ids | alias_match_ids
    # Drop self-links — pages that wikilink to themselves shouldn't backlink to themselves.
    source_ids.discard(page.id)
    if not source_ids:
        return []

    sources = Page.query.filter(Page.id.in_(source_ids)).order_by(Page.path.asc()).all()
    return sources


def serialize_backlink(page):
    """Compact JSON shape used by the API."""
    return {
        "id": page.id,
        "path": page.path,
        "title": page.title,
        "visibility": page.visibility,
        "excerpt": page.excerpt or "",
        "updated_at": page.updated_at.isoformat() if page.updated_at else None,
    }
