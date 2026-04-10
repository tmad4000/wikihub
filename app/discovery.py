from app import db
from app.models import Page, Wiki


DISCOVERABLE_VISIBILITIES = ("public", "public-edit")


def _is_self_viewer(viewer, owner):
    return bool(viewer and getattr(viewer, "is_authenticated", False) and viewer.id == owner.id)


def discoverable_wiki_ids():
    return {
        wiki_id
        for (wiki_id,) in db.session.query(Page.wiki_id)
        .filter(Page.visibility.in_(DISCOVERABLE_VISIBILITIES))
        .distinct()
        .all()
    }


def visible_wikis_for_owner(owner, viewer=None):
    wikis = Wiki.query.filter_by(owner_id=owner.id).order_by(Wiki.updated_at.desc()).all()
    if _is_self_viewer(viewer, owner):
        return wikis

    visible_ids = discoverable_wiki_ids()
    return [wiki for wiki in wikis if wiki.id in visible_ids]


def discoverable_page_for_wiki(wiki_id, viewer_is_owner=False):
    query = Page.query.filter_by(wiki_id=wiki_id)
    if not viewer_is_owner:
        query = query.filter(Page.visibility.in_(DISCOVERABLE_VISIBILITIES))

    page = query.filter_by(path="index.md").first()
    if page:
        return page
    return query.filter_by(path="README.md").first()
