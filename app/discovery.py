from app import db
from app.models import Page, Wiki
from app.page_utils import content_page_path_filter, is_content_page_path


DISCOVERABLE_VISIBILITIES = ("public", "public-view", "public-edit")


def _is_self_viewer(viewer, owner):
    return bool(viewer and getattr(viewer, "is_authenticated", False) and viewer.id == owner.id)


def discoverable_wiki_ids(visibilities=DISCOVERABLE_VISIBILITIES, trusted_only=True):
    """Wiki ids with at least one discoverable page. With trusted_only (the
    default) wikis owned by untrusted, suspended or curated-against accounts
    are dropped, as are wikis curated against and pages curated against —
    see app/trust.py (wikihub-9ras) and app/curation.py (wikihub-f3b7)."""
    from app.curation import not_curated_against
    listed = not_curated_against(wiki_id_col=Page.wiki_id, page_id_col=Page.id) if trusted_only else None
    safe_q = (db.session.query(Page.wiki_id)
              .filter(Page.visibility.in_(visibilities))
              .filter(content_page_path_filter(Page.path))
              .filter(~Page.path.contains("..")))
    risky_q = (db.session.query(Page.wiki_id, Page.path)
               .filter(Page.visibility.in_(visibilities))
               .filter(content_page_path_filter(Page.path))
               .filter(Page.path.contains("..")))
    if listed is not None:
        safe_q = safe_q.filter(listed)
        risky_q = risky_q.filter(listed)
    safe_ids = {wiki_id for (wiki_id,) in safe_q.distinct().all()}
    risky_ids = {wiki_id for wiki_id, path in risky_q.all() if is_content_page_path(path)}
    ids = safe_ids | risky_ids
    if trusted_only and ids:
        from app.models import User
        from app.trust import trusted_user_clause
        allowed = {
            wiki_id for (wiki_id,) in db.session.query(Wiki.id)
            .join(User, Wiki.owner_id == User.id)
            .filter(trusted_user_clause())
            .all()
        }
        ids &= allowed
    return ids


def visible_wikis_for_owner(owner, viewer=None):
    wikis = Wiki.query.filter_by(owner_id=owner.id).order_by(Wiki.updated_at.desc()).all()
    if _is_self_viewer(viewer, owner):
        return wikis

    visible_ids = discoverable_wiki_ids(trusted_only=False)
    return [wiki for wiki in wikis if wiki.id in visible_ids]


def discoverable_page_for_wiki(wiki_id, viewer_is_owner=False):
    for path in ("index.md", "README.md"):
        query = Page.query.filter_by(wiki_id=wiki_id, path=path)
        if not viewer_is_owner:
            query = query.filter(Page.visibility.in_(DISCOVERABLE_VISIBILITIES))
        page = query.first()
        if page and is_content_page_path(page.path):
            return page
    return None


READABLE_BY_LINK_VISIBILITIES = ("public", "public-view", "public-edit", "unlisted", "unlisted-view", "unlisted-edit")


def viewer_can_read_wiki(wiki, viewer=None, owner=None, acl_rules=None):
    """Can `viewer` (a User or None) read anything in this wiki?

    Same rule as the web reader's _viewer_can_see_any_page: the owner; anyone
    when at least one content page is public or unlisted (readable by link);
    an ACL grantee. Wiki-level metadata (title, description, counts) must only
    be served when this is true: a private wiki's title is content of
    something the viewer cannot read (plan 3d). Existence is not secret:
    callers answer 401/403, not 404."""
    from app.acl import grants_for_user
    from app.models import User
    from app.wiki_ops import load_acl_rules

    if viewer is not None and getattr(viewer, "id", None) == wiki.owner_id:
        return True
    paths = (db.session.query(Page.path)
             .filter(Page.wiki_id == wiki.id, Page.visibility.in_(READABLE_BY_LINK_VISIBILITIES))
             .filter(content_page_path_filter(Page.path)))
    if any(is_content_page_path(path) for (path,) in paths.yield_per(100)):
        return True
    if viewer is None or not getattr(viewer, "username", None):
        return False
    if acl_rules is None:
        if owner is None:
            owner = db.session.get(User, wiki.owner_id)
        acl_rules = load_acl_rules(owner.username, wiki.slug)
    return bool(acl_rules and grants_for_user(acl_rules, viewer.username))
