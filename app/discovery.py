from app import db
from app.models import Page, Wiki
from app.page_utils import content_page_path_filter, is_content_page_path


DISCOVERABLE_VISIBILITIES = ("public", "public-view", "public-edit")


def _is_self_viewer(viewer, owner):
    return bool(viewer and getattr(viewer, "is_authenticated", False) and viewer.id == owner.id)


def discoverable_wiki_ids(visibilities=DISCOVERABLE_VISIBILITIES):
    return {
        wiki_id for wiki_id, path in db.session.query(Page.wiki_id, Page.path)
        .filter(Page.visibility.in_(visibilities))
        .filter(content_page_path_filter(Page.path))
        .distinct()
        .all()
        if is_content_page_path(path)
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
    pages = [page for page in query.all() if is_content_page_path(page.path)]

    page = next((page for page in pages if page.path == "index.md"), None)
    if page:
        return page
    page = next((page for page in pages if page.path == "README.md"), None)
    return page
