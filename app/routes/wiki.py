import difflib
import os
import subprocess
from datetime import timezone
from urllib.parse import unquote, urlparse

from flask import Response, abort, jsonify, redirect, render_template, request, url_for

from app.url_utils import page_path_from_url_path, url_path_from_page_path
from flask_login import current_user

from app import db
from app.acl import can_read, can_write, grants_for_user, matches_serve_inline, resolve_grants, resolve_visibility
from app.backlinks import get_backlinks_for_page
from app.content_utils import has_private_bands, parse_markdown_document, set_visibility_in_content
from app.discovery import discoverable_page_for_wiki, visible_wikis_for_owner
from app.git_backend import _repo_path
from app.git_sync import read_file_from_repo, read_file_bytes_from_repo, list_files_in_repo, regenerate_public_mirror, remove_page_from_repo, sync_page_to_repo, update_mirror_page
from app.models import (
    Page,
    Proposal,
    ProposalComment,
    ProposalPagePatch,
    ProposalRevision,
    Fork,
    Star,
    User,
    UsernameRedirect,
    Wiki,
    WikiSlugRedirect,
    Wikilink,
    utcnow,
)
from app.renderer import build_html_embed_figure, extract_toc, render_page
from app.routes import wiki_bp
from app.wiki_ops import index_repo_pages, load_acl_rules, load_serve_inline_patterns, refresh_wikilinks_for_page, sync_wiki_counters, update_page_metadata


def _recently_updated_pages(wiki, limit=8, public_only=False):
    """get the most recently updated pages for a wiki."""
    query = Page.query.filter_by(wiki_id=wiki.id)
    if public_only:
        query = query.filter(Page.visibility.in_(('public', 'public-view', 'public-edit')))
    return query.order_by(Page.updated_at.desc()).limit(limit).all()


def _get_backlinks(page):
    """get pages that link to this page via wikilinks.

    Delegates to app.backlinks.get_backlinks_for_page so the API route and
    reader view stay in lockstep. See that module for resolution semantics
    (including the unresolved-alias forward-ref fallback).
    """
    return get_backlinks_for_page(page)


def _get_link_graph(page, wiki, viewer_filter=True):
    """get wikilink graph centered on a page (outgoing + incoming links).

    When ``viewer_filter`` is True (default), neighboring pages the viewer
    cannot read are excluded from the graph, along with their edges. The
    center page is assumed already authorized by the caller (it would not
    have rendered otherwise). (wikihub-8888.2)
    """
    if not page or not hasattr(page, 'id') or not page.id:
        return {"nodes": [], "links": []}

    nodes = {}
    links = []
    page_url = _page_url(wiki.owner.username, wiki.slug, page.path)
    nodes[page.id] = {"id": page.id, "title": page.title or page.path.replace('.md', ''), "url": page_url, "current": True}

    apply_filter = viewer_filter and not _is_owner(wiki)
    acl_rules = None
    owner_obj = None
    if apply_filter:
        owner_obj = db.session.get(User, wiki.owner_id)
        acl_rules = load_acl_rules(owner_obj.username, wiki.slug)

    def _neighbor_visible(neighbor_page):
        if not apply_filter:
            return True
        return _viewer_can_read_page(wiki, neighbor_page, acl_rules=acl_rules, owner=owner_obj)

    # outgoing links
    outgoing = Wikilink.query.filter_by(source_page_id=page.id).all()
    for wl in outgoing:
        if not wl.target_page_id:
            continue
        if wl.target_page_id not in nodes:
            tgt = db.session.get(Page, wl.target_page_id)
            if not tgt or not _neighbor_visible(tgt):
                continue
            nodes[tgt.id] = {"id": tgt.id, "title": tgt.title or tgt.path.replace('.md', ''), "url": _page_url(wiki.owner.username, wiki.slug, tgt.path), "current": False}
        if wl.target_page_id in nodes:
            links.append({"source": page.id, "target": wl.target_page_id})

    # incoming links (backlinks)
    incoming = Wikilink.query.filter_by(target_page_id=page.id).all()
    for wl in incoming:
        if wl.source_page_id not in nodes:
            src = db.session.get(Page, wl.source_page_id)
            if not src or not _neighbor_visible(src):
                continue
            nodes[src.id] = {"id": src.id, "title": src.title or src.path.replace('.md', ''), "url": _page_url(wiki.owner.username, wiki.slug, src.path), "current": False}
        if wl.source_page_id in nodes:
            links.append({"source": wl.source_page_id, "target": page.id})

    return {"nodes": list(nodes.values()), "links": links}


def _get_full_graph(wiki, viewer_filter=True):
    """get full wikilink graph for an entire wiki.

    When ``viewer_filter`` is True (default), filters nodes to pages the
    current viewer can read, and only includes edges where BOTH endpoints
    are visible to the viewer. An edge from a private page to a public page
    would otherwise reveal the private page's existence (wikihub-8888.2).
    """
    pages = Page.query.filter_by(wiki_id=wiki.id).all()
    owner = db.session.get(User, wiki.owner_id)

    visible_ids = None
    if viewer_filter and not _is_owner(wiki):
        acl_rules = load_acl_rules(owner.username, wiki.slug)
        visible_ids = {
            p.id for p in pages
            if _viewer_can_read_page(wiki, p, acl_rules=acl_rules, owner=owner)
        }
        pages = [p for p in pages if p.id in visible_ids]

    # build node map, skip index.md from being a hub (still a node, just filter its links)
    nodes = {}
    index_ids = set()
    for p in pages:
        url = _page_url(owner.username, wiki.slug, p.path)
        dir_path = p.path.rsplit("/", 1)[0] if "/" in p.path else ""
        nodes[p.id] = {
            "id": p.id,
            "title": p.title or p.path.replace(".md", "").rsplit("/", 1)[-1],
            "url": url,
            "dir": dir_path,
        }
        if p.path in ("index.md", "README.md") or p.path.endswith("/index.md"):
            index_ids.add(p.id)

    page_ids = [p.id for p in pages]
    all_links = Wikilink.query.filter(
        Wikilink.source_page_id.in_(page_ids),
        Wikilink.target_page_id.isnot(None),
    ).all()

    # exclude index pages entirely — they link to everything and create a useless hub
    links = []
    for wl in all_links:
        if wl.source_page_id in index_ids or wl.target_page_id in index_ids:
            continue
        if wl.target_page_id in nodes:
            links.append({"source": wl.source_page_id, "target": wl.target_page_id})

    # remove index nodes from the graph
    for idx_id in index_ids:
        nodes.pop(idx_id, None)

    # count links per node
    link_count = {}
    for l in links:
        link_count[l["source"]] = link_count.get(l["source"], 0) + 1
        link_count[l["target"]] = link_count.get(l["target"], 0) + 1

    # only include nodes that have at least 1 link (skip orphans for cleaner graph)
    connected_ids = set(link_count.keys())
    filtered_nodes = [n for n in nodes.values() if n["id"] in connected_ids]

    return {"nodes": filtered_nodes, "links": links}


def _resolve_owner(username):
    owner = User.query.filter_by(username=username).first()
    if owner:
        return owner, None

    redirect_row = UsernameRedirect.query.filter_by(old_username=username).first()
    if redirect_row and redirect_row.expires_at > utcnow():
        return redirect_row.user, redirect_row
    return None, None


def _get_owner_and_wiki_or_404(username, slug):
    owner, redirect_row = _resolve_owner(username)
    if not owner:
        abort(404)
    wiki = Wiki.query.filter_by(owner_id=owner.id, slug=slug).first()
    if not wiki:
        # check for slug redirect
        slug_redir = WikiSlugRedirect.query.filter_by(owner_id=owner.id, old_slug=slug).first()
        if slug_redir and slug_redir.wiki:
            return owner, slug_redir.wiki, slug_redir
        abort(404)
    return owner, wiki, redirect_row


def _is_owner(wiki):
    return current_user.is_authenticated and current_user.id == wiki.owner_id


def _use_public_repo(wiki, acl_rules=None):
    """Should we read from the public mirror? False if owner or has ACL grants."""
    if _is_owner(wiki):
        return False
    if current_user.is_authenticated and acl_rules:
        if grants_for_user(acl_rules, current_user.username):
            return False
    return True


def _repo_access(wiki, acl_rules=None):
    """Return (use_public, acl_filter_user) tuple for repo reads.

    - Owner: (False, None) — full access to authoritative repo
    - ACL grantee: (False, username) — authoritative repo, filtered by can_read
    - Everyone else: (True, None) — public mirror only
    """
    if _is_owner(wiki):
        return False, None
    if current_user.is_authenticated and acl_rules:
        uname = current_user.username
        if grants_for_user(acl_rules, uname):
            return False, uname
    return True, None


def _viewer_can_read_page(wiki, page, acl_rules=None, owner=None):
    """Can the current viewer read this Page?

    Canonical reader-route logic, extracted for reuse by history / commit /
    graph / tag routes (wikihub-8888).

    - Owner of the wiki: always True
    - Otherwise: gate on can_read(path, acl_rules, username, page.visibility)

    Page is a Page row OR an object exposing .path and .visibility.
    """
    if page is None:
        return False
    if _is_owner(wiki):
        return True
    if acl_rules is None:
        if owner is None:
            owner = db.session.get(User, wiki.owner_id)
        acl_rules = load_acl_rules(owner.username, wiki.slug)
    user_name = current_user.username if current_user.is_authenticated else None
    return can_read(page.path, acl_rules, user_name, page.visibility)


def _viewer_can_see_any_page(wiki, acl_rules=None, owner=None):
    """Does the viewer have read access to ANY page in this wiki?

    Used to gate wiki-level surfaces (history, commit diff) where access to
    one page implies access to the historical record. Owners and ACL grantees
    see authoritative; anonymous users who can read at least one public page
    see the public mirror; viewers with no access at all get a permission error.
    """
    if _is_owner(wiki):
        return True
    if acl_rules is None:
        if owner is None:
            owner = db.session.get(User, wiki.owner_id)
        acl_rules = load_acl_rules(owner.username, wiki.slug)
    # Anyone can see public/public-view/public-edit and unlisted-* pages.
    public_visibilities = ("public", "public-view", "public-edit", "unlisted", "unlisted-view", "unlisted-edit")
    has_public = (
        Page.query.filter_by(wiki_id=wiki.id)
        .filter(Page.visibility.in_(public_visibilities))
        .first()
        is not None
    )
    if has_public:
        return True
    # ACL grantee with a per-user grant against any page → can see authoritative
    if current_user.is_authenticated and acl_rules:
        if grants_for_user(acl_rules, current_user.username):
            return True
    return False


def _sibling_wikis(owner, current_wiki):
    """other wikis by the same owner, for cross-wiki nav in sidebar."""
    from app.discovery import visible_wikis_for_owner
    wikis = visible_wikis_for_owner(owner, current_user)
    return [w for w in wikis if w.id != current_wiki.id]


def _render_permission_error(owner, wiki, status_code=None):
    if status_code is None:
        status_code = 401 if not current_user.is_authenticated else 403
    return render_template("permission_error.html", owner=owner, wiki=wiki), status_code


def _render_restricted(owner, wiki):
    """wikihub-dkp8: the resource EXISTS but the viewer can't read it.

    Renders the distinct "This page is restricted" screen with 403 semantics
    (existence acknowledged, no content/title/frontmatter leaked). Contrast with
    a genuinely-missing path, which stays a plain 404 (`error.html`).
    """
    return render_template("permission_error.html", owner=owner, wiki=wiki, restricted=True), 403


def _restricted_json():
    """wikihub-dkp8: agent/markdown-facing counterpart to `_render_restricted`.

    Existence acknowledged with 403 (authed) / 401 (anon) so agents can tell
    "restricted" apart from a 404 "does not exist". Never leaks page fields.
    """
    from flask import jsonify, make_response
    status = 401 if not current_user.is_authenticated else 403
    body = {
        "error": "authentication_required" if status == 401 else "forbidden",
        "message": "This page is restricted — it exists but you don't have access.",
        "sign_in_url": "https://wikihub.md/auth/login",
    }
    resp = make_response(jsonify(body), status)
    resp.headers["Cache-Control"] = "no-store"
    if status == 401:
        resp.headers["WWW-Authenticate"] = 'Bearer realm="wikihub"'
    return resp


def _normalize_folder_path(raw_path):
    clean = (raw_path or "").replace("\\", "/").strip().strip("/")
    clean = clean.removesuffix("/index.md").removesuffix("/index").removesuffix(".md")
    segments = [segment for segment in clean.split("/") if segment]
    if not segments or any(segment in {".", ".."} for segment in segments):
        return None
    return "/".join(segments)


def _visible_files(username, slug, wiki, public=False, acl_filter_user=None, pages_by_path=None):
    """List visible files in a wiki repo.

    Args:
        public: If True, read from public mirror.
        acl_filter_user: If set, read from authoritative repo but filter
            through can_read for this username (for ACL grantees who aren't owners).
        pages_by_path: Pre-loaded {path: Page} dict to avoid duplicate DB queries.
    """
    files = list_files_in_repo(username, slug, public=public)
    if not public and not acl_filter_user:
        # owner view — everything
        return [path for path in files if not path.startswith(".wikihub/") and not path.endswith(".gitkeep")]

    if acl_filter_user:
        # non-owner with ACL grants — read authoritative repo, filter by can_read
        acl_rules = load_acl_rules(username, slug)
        if pages_by_path is None:
            pages_by_path = {p.path: p for p in Page.query.filter_by(wiki_id=wiki.id).all()}
        return [
            path
            for path in files
            if not path.startswith(".wikihub/")
            and not path.endswith(".gitkeep")
            and can_read(path, acl_rules, acl_filter_user, (pages_by_path[path].visibility if path in pages_by_path else None))
        ]

    # Anonymous / public-mirror viewer. The sidebar is in-wiki navigation, not a
    # discovery surface: 'unlisted' governs search/explore/profile listings, but a
    # viewer who already possesses the wiki link and can_read a page must see it in
    # the wiki's own page tree (wikihub #17). Gate on can_read (which admits public,
    # public-edit, and unlisted), NOT is_discoverable. The public mirror already
    # excludes private pages, so files here are readable-by-URL by construction.
    acl_rules = load_acl_rules(username, slug)
    if pages_by_path is None:
        pages_by_path = {p.path: p for p in Page.query.filter_by(wiki_id=wiki.id).all()}
    return [
        path
        for path in files
        if not path.startswith(".wikihub/")
        and not path.endswith(".gitkeep")
        and can_read(path, acl_rules, None, (pages_by_path[path].visibility if path in pages_by_path else None))
    ]


def _folder_url(username, slug, folder_path):
    clean = folder_path.strip("/")
    if not clean:
        return f"/@{username}/{slug}"
    return f"/@{username}/{slug}/{url_path_from_page_path(clean, strip_md=False)}/"


def _page_url(username, slug, page_path):
    return f"/@{username}/{slug}/{url_path_from_page_path(page_path, strip_md=True)}"


_SIDEBAR_NON_CONTENT_ROOTS = {
    "-",
    "commit",
    "activity",
    "graph",
    "graph.json",
    "history",
    "llms.txt",
    "preview",
    "reindex",
    "settings",
    "sidebar.json",
    "tag",
}


def _resolve_markdown_page(wiki, raw_page_path):
    """Resolve a clean URL path to an existing markdown Page row."""
    page_path = page_path_from_url_path(raw_page_path)
    raw_file_path = raw_page_path if raw_page_path.endswith(".md") else raw_page_path + ".md"
    file_path = raw_file_path
    page = Page.query.filter_by(wiki_id=wiki.id, path=file_path).first()
    if page is None and "_" in raw_page_path:
        space_path = page_path if page_path.endswith(".md") else page_path + ".md"
        page = Page.query.filter_by(wiki_id=wiki.id, path=space_path).first()
        if page:
            file_path = space_path
    return page, file_path


def _latest_proposal_patch(proposal):
    revision = proposal.revisions.order_by(None).order_by(ProposalRevision.revision_number.desc()).first()
    if not revision:
        return None, None
    return revision, revision.patches.first()


def _proposal_diff(base_content, proposed_content, path):
    return list(difflib.unified_diff(
        (base_content or "").splitlines(),
        (proposed_content or "").splitlines(),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        lineterm="",
    ))


def _proposal_participant_can_view(proposal, current_page, owner, wiki, patch):
    if _is_owner(wiki):
        return True
    if current_user.is_authenticated and proposal.author_id == current_user.id:
        return True
    acl_rules = load_acl_rules(owner.username, wiki.slug)
    username_for_acl = current_user.username if current_user.is_authenticated else None
    return bool(current_page and can_read(patch.page_path, acl_rules, username_for_acl, current_page.visibility))


def _proposal_participant_name():
    return current_user.username if current_user.is_authenticated else "anonymous"


def _add_proposal_comment(proposal, body, event="comment"):
    body = (body or "").strip()
    if not body:
        return None
    comment = ProposalComment(
        proposal_id=proposal.id,
        author_id=current_user.id if current_user.is_authenticated else None,
        author_name=_proposal_participant_name(),
        body=body,
        event=event,
    )
    db.session.add(comment)
    return comment


def _normalize_sidebar_current_path(current_path):
    if current_path is None:
        return None

    path = unquote((current_path or "").strip())
    if not path or path == "/":
        return None

    path = path.lstrip("/")
    if path.endswith("/"):
        return _normalize_folder_path(path)
    return path.removesuffix("/")


def _sidebar_current_path_from_referrer(username, slug):
    if not request.referrer:
        return None

    parsed = urlparse(request.referrer)
    prefix = f"/@{username}/{slug}"
    if parsed.path == prefix or parsed.path == prefix + "/":
        return "index.md"
    if not parsed.path.startswith(prefix + "/"):
        return None

    relative = unquote(parsed.path[len(prefix) + 1 :])
    if not relative:
        return "index.md"
    if relative.endswith("/"):
        return _normalize_folder_path(page_path_from_url_path(relative.rstrip("/")))

    parts = [part for part in relative.split("/") if part]
    if not parts:
        return "index.md"
    if parts[0] in _SIDEBAR_NON_CONTENT_ROOTS:
        return None
    if parts[-1] in {"edit", "history"} and len(parts) > 1:
        parts = parts[:-1]

    page_path = page_path_from_url_path("/".join(parts))
    if not page_path:
        return None
    if "." not in os.path.basename(page_path):
        page_path = f"{page_path}.md"
    return page_path


def _sidebar_current_path_from_request(username, slug):
    explicit_current = _normalize_sidebar_current_path(request.args.get("current"))
    if explicit_current:
        return explicit_current
    return _sidebar_current_path_from_referrer(username, slug)


def _build_sidebar_tree(username, slug, wiki, public=False, current_path=None, acl_filter_user=None):
    current_path = _normalize_sidebar_current_path(current_path)
    root = {"children": {}}
    pages_by_path = {p.path: p for p in Page.query.filter_by(wiki_id=wiki.id).all()}

    for path in sorted(_visible_files(username, slug, wiki, public=public, acl_filter_user=acl_filter_user, pages_by_path=pages_by_path)):
        parts = path.split("/")
        cursor = root["children"]
        for depth, part in enumerate(parts[:-1]):
            folder_path = "/".join(parts[: depth + 1])
            folder_is_current = current_path == folder_path
            node = cursor.setdefault(
                ("folder", folder_path),
                {
                    "kind": "folder",
                    "name": part,
                    "path": folder_path,
                    "url": _folder_url(username, slug, folder_path),
                    "active": folder_is_current,
                    "current": folder_is_current,
                    "ancestor_of_current": False,
                    "children": {},
                },
            )
            cursor = node["children"]

        filename = parts[-1]
        if filename in {"index.md", "README.md"} and len(parts) > 1:
            continue

        page = pages_by_path.get(path)
        updated = page.updated_at.isoformat() if page and page.updated_at else None
        # Pinned pages (frontmatter `pinned: true`) float to a top section of the
        # sidebar. Frontmatter-wins mirrors the visibility precedent. Integrators
        # (e.g. GroupBrain) can pin rules / chat-history pages the same way.
        page_pinned = bool(page and (page.frontmatter_json or {}).get("pinned")) if page else False
        page_is_current = current_path == path
        # HTML decks open in the embedded viewer (deck inside reader chrome) rather
        # than replacing the wiki page with the bare standalone deck. The viewer is
        # the same _page_url with ?view=1 — the route falls back to raw serving when
        # the path isn't serve-inline-opted-in. (wikihub-ntpc)
        page_url = _page_url(username, slug, path)
        if path.lower().endswith((".html", ".htm")):
            page_url = page_url + "?view=1"
        cursor[("page", path)] = {
            "kind": "page",
            "name": filename.replace(".md", ""),
            "path": path,
            "url": page_url,
            "active": page_is_current,
            "current": page_is_current,
            "ancestor_of_current": False,
            "visibility": page.visibility if page else "private",
            "pinned": page_pinned,
            "updated_at": updated,
            "children": {},
        }

    def normalize(children):
        items = list(children.values())
        for item in items:
            if item["kind"] == "folder":
                item["children"] = normalize(item["children"])
                item["ancestor_of_current"] = any(child["active"] for child in item["children"])
                item["active"] = item["current"] or item["ancestor_of_current"]
                child_dates = [c["updated_at"] for c in item["children"] if c.get("updated_at")]
                item["updated_at"] = max(child_dates) if child_dates else None
            else:
                item.setdefault("updated_at", None)
        # Pinned pages first (top section), then folders, then alphabetical pages.
        return sorted(
            items,
            key=lambda item: (
                not item.get("pinned", False),
                item["kind"] != "folder",
                item["name"].lower(),
                item["path"],
            ),
        )

    return normalize(root["children"])


def _activity_time(value):
    if value is None:
        return utcnow()
    return value


def _viewer_can_see_proposal(proposal, owner, wiki):
    revision, patch = _latest_proposal_patch(proposal)
    if not patch:
        return False
    page = Page.query.filter_by(wiki_id=wiki.id, path=patch.page_path).first()
    return _proposal_participant_can_view(proposal, page, owner, wiki, patch)


def _wiki_activity_items(owner, wiki, acl_rules, limit=60):
    """Build a mixed recent-activity feed from existing durable rows.

    This intentionally avoids a new event table. Page rows, proposals, stars,
    and forks already capture the product events users care about, and each item
    is filtered through the same ACL checks used by reader/history routes.
    """
    items = []
    visible_pages = []
    for page in Page.query.filter_by(wiki_id=wiki.id).order_by(Page.updated_at.desc()).limit(120).all():
        if _viewer_can_read_page(wiki, page, acl_rules=acl_rules, owner=owner):
            visible_pages.append(page)
            verb = "created" if abs((_activity_time(page.updated_at) - _activity_time(page.created_at)).total_seconds()) < 2 else "updated"
            items.append({
                "kind": "page",
                "label": f"Page {verb}",
                "title": page.title or page.path.replace(".md", "").rsplit("/", 1)[-1],
                "detail": page.path,
                "url": _page_url(owner.username, wiki.slug, page.path),
                "actor": page.author if not page.anonymous else "anonymous",
                "timestamp": _activity_time(page.updated_at),
                "visibility": page.visibility,
            })

    if _is_owner(wiki) or current_user.is_authenticated:
        proposals = (
            Proposal.query.filter_by(wiki_id=wiki.id)
            .order_by(Proposal.updated_at.desc())
            .limit(50)
            .all()
        )
        for proposal in proposals:
            if not _viewer_can_see_proposal(proposal, owner, wiki):
                continue
            items.append({
                "kind": "proposal",
                "label": f"Suggestion {proposal.status.replace('_', ' ')}",
                "title": proposal.title,
                "detail": proposal.page_path,
                "url": url_for("wiki.proposal_detail", username=owner.username, slug=wiki.slug, proposal_id=proposal.id),
                "actor": proposal.author_name or "anonymous",
                "timestamp": _activity_time(proposal.updated_at),
                "visibility": "private" if proposal.status != "accepted" else None,
            })
            comment = proposal.comments.order_by(None).order_by(ProposalComment.created_at.desc()).first()
            if comment:
                items.append({
                    "kind": "comment",
                    "label": comment.event.replace("_", " ").title(),
                    "title": proposal.title,
                    "detail": (comment.body or "")[:140],
                    "url": url_for("wiki.proposal_detail", username=owner.username, slug=wiki.slug, proposal_id=proposal.id),
                    "actor": comment.author_name or "anonymous",
                    "timestamp": _activity_time(comment.created_at),
                    "visibility": None,
                })

    if visible_pages:
        user_ids = set()
        stars = Star.query.filter_by(wiki_id=wiki.id).order_by(Star.created_at.desc()).limit(25).all()
        forks = Fork.query.filter_by(source_wiki_id=wiki.id).order_by(Fork.created_at.desc()).limit(25).all()
        user_ids.update(star.user_id for star in stars)
        user_ids.update(fork.user_id for fork in forks)
        users = {u.id: u for u in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}

        for star in stars:
            actor = users.get(star.user_id)
            items.append({
                "kind": "star",
                "label": "Wiki starred",
                "title": wiki.title or wiki.slug,
                "detail": f"@{actor.username}" if actor else "Someone",
                "url": f"/@{owner.username}/{wiki.slug}",
                "actor": actor.username if actor else None,
                "timestamp": _activity_time(star.created_at),
                "visibility": None,
            })

        for fork in forks:
            actor = users.get(fork.user_id)
            forked_wiki = db.session.get(Wiki, fork.forked_wiki_id)
            fork_url = f"/@{actor.username}/{forked_wiki.slug}" if actor and forked_wiki else f"/@{owner.username}/{wiki.slug}"
            items.append({
                "kind": "fork",
                "label": "Wiki forked",
                "title": wiki.title or wiki.slug,
                "detail": f"@{actor.username}/{forked_wiki.slug}" if actor and forked_wiki else "Fork created",
                "url": fork_url,
                "actor": actor.username if actor else None,
                "timestamp": _activity_time(fork.created_at),
                "visibility": None,
            })

    return sorted(items, key=lambda item: item["timestamp"], reverse=True)[:limit]


def _folder_listing(username, slug, wiki, folder_path="", public=False, acl_filter_user=None):
    prefix = folder_path.strip("/")
    files = _visible_files(username, slug, wiki, public=public, acl_filter_user=acl_filter_user)
    pages_by_path = {page.path: page for page in Page.query.filter_by(wiki_id=wiki.id).all()}
    seen = set()
    items = []

    # collect pages per folder so we can derive folder visibility from children
    folder_pages = {}  # child_path -> list of Page objects

    for path in files:
        if prefix:
            if not path.startswith(prefix + "/"):
                continue
            relative = path[len(prefix) + 1 :]
        else:
            relative = path

        if "/" in relative:
            child = relative.split("/", 1)[0]
            child_path = f"{prefix}/{child}".strip("/")
            page = pages_by_path.get(path)
            if page:
                folder_pages.setdefault(child_path, []).append(page)
            if child_path in seen:
                continue
            seen.add(child_path)
            items.append(
                {
                    "kind": "folder",
                    "name": child,
                    "path": child_path,
                    "url": _folder_url(username, slug, child_path),
                    "_folder_key": child_path,
                }
            )
            continue

        if relative in {"index.md", "README.md"} and prefix:
            continue

        page = pages_by_path.get(path)
        items.append(
            {
                "kind": "page",
                "name": relative.replace(".md", ""),
                "path": path,
                "url": _page_url(username, slug, path),
                "visibility": page.visibility if page else resolve_visibility(path, load_acl_rules(username, slug)),
                "updated_at": page.updated_at if page else None,
            }
        )

    acl_rules = load_acl_rules(username, slug)
    for item in items:
        if item.get("_folder_key"):
            children = folder_pages.get(item["_folder_key"], [])
            if children:
                vis_set = {p.visibility for p in children}
                item["visibility"] = vis_set.pop() if len(vis_set) == 1 else "mixed"
                item["updated_at"] = max((p.updated_at for p in children if p.updated_at), default=None)
            else:
                item["visibility"] = resolve_visibility(item["_folder_key"], acl_rules)
                item["updated_at"] = None
            del item["_folder_key"]

    return sorted(items, key=lambda item: (item["kind"] != "folder", item["name"].lower()))


def _folder_index_content(username, slug, folder_path, public=False):
    candidates = []
    clean = folder_path.strip("/")
    if clean:
        candidates = [f"{clean}/index.md", f"{clean}/README.md"]
    else:
        candidates = ["index.md", "README.md"]

    for candidate in candidates:
        content = read_file_from_repo(username, slug, candidate, public=public)
        if content is not None:
            return candidate, content
    return None, None


def _git_history(username, slug, public=False, path=None, limit=50):
    from app.git_backend import _repo_path

    repo = _repo_path(username, slug, public=public)
    if not os.path.isdir(repo):
        return []

    # use %x00 (NUL) as record separator — cleaner than \x1e with --name-only
    cmd = [
        "git", "-C", repo, "log",
        f"--max-count={limit}",
        "--format=%H%x1f%an%x1f%aI%x1f%s",
        "--name-only",
    ]
    if path:
        cmd += ["--", path]

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return []

    # parse: each commit is a format line followed by a blank line, then file names, then another blank line
    # format line has exactly 3 \x1f separators; file lines have none
    commits = []
    current = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\x1f")
        if len(parts) == 4:
            # this is a format line = start of a new commit
            if current:
                commits.append(current)
            sha, author, date, message = parts
            current = {
                "sha": sha,
                "author": author,
                "date": date,
                "message": message,
                "files_changed": [],
            }
        elif current:
            # this is a filename belonging to the current commit
            current["files_changed"].append(line)
    if current:
        commits.append(current)

    return commits


def _git_diff(username, slug, sha, public=False, path=None):
    """get the diff for a specific commit."""
    from app.git_backend import _repo_path

    repo = _repo_path(username, slug, public=public)
    if not os.path.isdir(repo):
        return None

    # verify the sha exists in this repo
    verify = subprocess.run(
        ["git", "-C", repo, "cat-file", "-t", sha],
        capture_output=True, text=True, check=False,
    )
    if verify.returncode != 0:
        return None

    # check if this commit has a parent
    parent_check = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", f"{sha}~1"],
        capture_output=True, text=True, check=False,
    )

    if parent_check.returncode == 0:
        cmd = ["git", "-C", repo, "diff", f"{sha}~1", sha, "--no-color"]
        if path:
            cmd += ["--", path]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return result.stdout if result.returncode == 0 else None

    # first commit / no parent — use diff-tree --root which works in bare repos
    cmd = ["git", "-C", repo, "diff-tree", "-p", "--no-color", "--root", sha]
    if path:
        cmd += ["--", path]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else None


@wiki_bp.route("/@<username>", strict_slashes=False)
def user_profile(username):
    owner, redirect_row = _resolve_owner(username)
    if not owner:
        abort(404)
    if redirect_row:
        return redirect(url_for("wiki.user_profile", username=owner.username), code=302)

    personal_wiki = Wiki.query.filter_by(owner_id=owner.id, slug=owner.username).first()
    all_wikis = Wiki.query.filter_by(owner_id=owner.id).order_by(Wiki.updated_at.desc()).all()
    wikis = visible_wikis_for_owner(owner, current_user)
    for wiki in all_wikis:
        sync_wiki_counters(wiki)
    total_stars = sum(w.star_count for w in wikis)

    personal_content = None
    personal_rendered_html = None
    personal_sidebar = []
    private_band_warning = False
    is_owner = bool(personal_wiki and _is_owner(personal_wiki))

    if personal_wiki:
        use_public = not is_owner
        _, personal_content = _folder_index_content(owner.username, personal_wiki.slug, "", public=use_public)
        if personal_content:
            personal_rendered_html = render_page(personal_content, owner.username, personal_wiki.slug)
            personal_sidebar = _build_sidebar_tree(owner.username, personal_wiki.slug, personal_wiki, public=use_public, current_path="index.md")
            private_band_warning = not use_public and has_private_bands(personal_content)
        elif any(wiki.id == personal_wiki.id for wiki in wikis):
            personal_sidebar = _build_sidebar_tree(owner.username, personal_wiki.slug, personal_wiki, public=not is_owner)

    other_wikis = [wiki for wiki in wikis if wiki.slug != owner.username]
    profile_page = discoverable_page_for_wiki(personal_wiki.id, viewer_is_owner=is_owner) if personal_wiki else None
    personal_visible = bool(personal_wiki and (is_owner or profile_page or personal_sidebar))
    return render_template(
        "profile.html",
        owner=owner,
        wikis=other_wikis,
        total_stars=total_stars,
        personal_wiki=personal_wiki,
        personal_rendered_html=personal_rendered_html,
        private_band_warning=private_band_warning,
        sidebar_items=personal_sidebar,
        personal_excerpt=(profile_page and (profile_page.frontmatter_json or {}).get("bio")) or (profile_page.excerpt if profile_page else None),
        personal_is_public=bool(profile_page),
        visible_wiki_count=len(other_wikis) + (1 if personal_visible else 0),
        is_owner=is_owner,
    )


@wiki_bp.route("/@<username>/<slug>/llms.txt")
def wiki_llms_txt(username, slug):
    """per-wiki LLM-readable index."""
    owner = User.query.filter_by(username=username).first_or_404()
    wiki = Wiki.query.filter_by(owner_id=owner.id, slug=slug).first_or_404()

    lines = [
        f"# {wiki.title or wiki.slug}",
        f"> {wiki.description or 'A wiki on wikihub.'}",
        f"",
        f"Owner: @{owner.username}",
        f"URL: /@{owner.username}/{wiki.slug}",
        f"",
        "## Pages",
    ]

    pages = Page.query.filter_by(wiki_id=wiki.id).filter(
        Page.visibility.in_(["public", "public-view", "public-edit"])
    ).order_by(Page.path).all()

    for p in pages:
        url = _page_url(owner.username, wiki.slug, p.path)
        lines.append(f"- [{p.title or p.path}]({url})")

    if not pages:
        lines.append("(no public pages)")

    return Response(
        "\n".join(lines),
        content_type="text/plain; charset=utf-8",
    )


@wiki_bp.route("/@<username>/<slug>.zip")
def wiki_zip(username, slug):
    owner, wiki, redirect_row = _get_owner_and_wiki_or_404(username, slug)
    if redirect_row:
        return redirect(url_for("wiki.wiki_zip", username=owner.username, slug=wiki.slug), code=302)

    from app.git_backend import _repo_path

    repo = _repo_path(owner.username, wiki.slug, public=not _is_owner(wiki))
    if not os.path.isdir(repo):
        abort(404)

    proc = subprocess.run(["git", "archive", "--format=zip", "HEAD"], cwd=repo, capture_output=True)
    if proc.returncode != 0:
        abort(500)

    return Response(
        proc.stdout,
        content_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{slug}.zip"'},
    )


SIDEBAR_ASYNC_THRESHOLD = 200  # wikis with more pages than this get client-side sidebar


def _sidebar_for_wiki(username, slug, wiki, public=False, current_path=None, acl_filter_user=None):
    """return sidebar items, or None if the wiki is too large (use sidebar.json instead)."""
    page_count = Page.query.filter_by(wiki_id=wiki.id).count()
    if page_count > SIDEBAR_ASYNC_THRESHOLD:
        return None  # template will fetch sidebar.json client-side
    return _build_sidebar_tree(username, slug, wiki, public=public, current_path=current_path, acl_filter_user=acl_filter_user)


@wiki_bp.route("/@<username>/<slug>/sidebar.json")
def sidebar_json(username, slug):
    """lightweight JSON manifest for client-side sidebar rendering."""
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    acl_rules = load_acl_rules(owner.username, wiki.slug)
    use_public, acl_filter_user = _repo_access(wiki, acl_rules)
    tree = _build_sidebar_tree(
        owner.username,
        wiki.slug,
        wiki,
        public=use_public,
        current_path=_sidebar_current_path_from_request(owner.username, wiki.slug),
        acl_filter_user=acl_filter_user,
    )
    return jsonify(tree)


@wiki_bp.route("/@<username>/<slug>/settings", strict_slashes=False)
def wiki_settings(username, slug):
    """wiki settings page — subdomain, visibility, danger zone."""
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    if not _is_owner(wiki):
        return _render_permission_error(owner, wiki)
    return render_template("wiki_settings.html", owner=owner, wiki=wiki)


@wiki_bp.route("/@<username>/<slug>", strict_slashes=False)
def wiki_index(username, slug):
    owner, wiki, redirect_row = _get_owner_and_wiki_or_404(username, slug)
    if redirect_row:
        return redirect(url_for("wiki.wiki_index", username=owner.username, slug=wiki.slug), code=302)

    # Personal wiki: redirect to profile page
    if slug == owner.username:
        return redirect(url_for("wiki.user_profile", username=owner.username), code=302)

    acl_rules = load_acl_rules(owner.username, wiki.slug)
    use_public, acl_filter_user = _repo_access(wiki, acl_rules)
    recently_updated = _recently_updated_pages(wiki, public_only=use_public and not acl_filter_user)
    page_path, content = _folder_index_content(owner.username, wiki.slug, "", public=use_public)
    siblings = _sibling_wikis(owner, wiki)
    if content is None:
        items = _folder_listing(owner.username, wiki.slug, wiki, "", public=use_public, acl_filter_user=acl_filter_user)
        if use_public and not items:
            # wikihub-dkp8: the wiki row EXISTS. If it has any pages this viewer
            # simply can't see → restricted (403). Only a genuinely empty wiki
            # (no pages at all) falls back to the ambiguous 404.
            if Page.query.filter_by(wiki_id=wiki.id).count() > 0:
                return _render_restricted(owner, wiki)
            return render_template("permission_error.html", owner=owner, wiki=wiki), 404
        return render_template(
            "folder.html",
            owner=owner,
            wiki=wiki,
            items=items,
            sidebar_items=_sidebar_for_wiki(owner.username, wiki.slug, wiki, public=use_public, acl_filter_user=acl_filter_user),
            folder_path="",
            rendered_html=None,
            breadcrumb=[],
            recently_updated=recently_updated,
            sibling_wikis=siblings,
        )

    page = Page.query.filter_by(wiki_id=wiki.id, path=page_path).first()
    if page is None:
        page = type("Page", (), {"path": page_path, "title": wiki.title, "visibility": "private", "updated_at": wiki.updated_at})()

    rendered_html = render_page(content, owner.username, wiki.slug, current_page_path=page_path)
    is_owner = _is_owner(wiki)
    management_items = _folder_listing(owner.username, wiki.slug, wiki, "", public=False) if is_owner else None
    user_name = current_user.username if current_user.is_authenticated else None
    user_can_edit = is_owner or can_write(page_path, acl_rules, user_name, page.visibility if page else None)
    return render_template(
        "reader.html",
        owner=owner,
        wiki=wiki,
        page=page,
        rendered_html=rendered_html,
        toc=extract_toc(rendered_html),
        backlinks=_get_backlinks(page),
        link_graph=_get_link_graph(page, wiki),
        full_graph_url=f"/@{owner.username}/{wiki.slug}/graph",
        recently_updated=recently_updated,
        sidebar_items=_sidebar_for_wiki(owner.username, wiki.slug, wiki, public=use_public, current_path=page_path, acl_filter_user=acl_filter_user),
        private_band_warning=not use_public and has_private_bands(content),
        json_ld_author=owner.display_name or owner.username,
        management_items=management_items,
        sibling_wikis=siblings,
        user_can_edit=user_can_edit,
    )


@wiki_bp.route("/@<username>/<slug>/-/suggest/<path:page_path>", methods=["GET", "POST"])
def suggest_edit(username, slug, page_path):
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    page, file_path = _resolve_markdown_page(wiki, page_path)
    if not page:
        abort(404)

    acl_rules = load_acl_rules(owner.username, wiki.slug)
    is_owner = _is_owner(wiki)
    username_for_acl = current_user.username if current_user.is_authenticated else None
    if not is_owner and not can_read(file_path, acl_rules, username_for_acl, page.visibility):
        return render_template("permission_error.html", owner=owner, wiki=wiki), 404

    user_can_write = is_owner or can_write(file_path, acl_rules, username_for_acl, page.visibility)
    read_public = not user_can_write and _use_public_repo(wiki, acl_rules)
    content = read_file_from_repo(owner.username, wiki.slug, file_path, public=read_public)
    if content is None:
        abort(404)
    if request.method == "POST":
        proposed_content = request.form.get("content", "")
        note = request.form.get("note", "").strip()
        title = request.form.get("title", "").strip() or f"Suggested edit to {page.title or file_path}"
        author_name = current_user.username if current_user.is_authenticated else (
            request.form.get("author_name", "").strip() or "anonymous"
        )

        # Suggestions never change page visibility or path. Owners can still make
        # those changes explicitly in the normal editor.
        proposed_content = set_visibility_in_content(proposed_content, page.visibility)

        proposal = Proposal(
            wiki_id=wiki.id,
            page_id=page.id,
            page_path=file_path,
            author_id=current_user.id if current_user.is_authenticated else None,
            author_name=author_name,
            title=title,
            status="pending",
            base_content_hash=page.content_hash,
        )
        db.session.add(proposal)
        db.session.flush()

        revision = ProposalRevision(proposal_id=proposal.id, revision_number=1, note=note)
        db.session.add(revision)
        db.session.flush()

        db.session.add(ProposalPagePatch(
            revision_id=revision.id,
            page_path=file_path,
            base_content_hash=page.content_hash,
            base_content=content,
            proposed_content=proposed_content,
        ))
        if note:
            _add_proposal_comment(proposal, note, event="submitted")
        db.session.commit()
        return redirect(url_for(
            "wiki.proposal_detail",
            username=owner.username,
            slug=wiki.slug,
            proposal_id=proposal.id,
        ))

    return render_template(
        "suggest_edit.html",
        owner=owner,
        wiki=wiki,
        page=page,
        page_path=file_path,
        content=content,
        is_authenticated=current_user.is_authenticated,
    )


@wiki_bp.route("/@<username>/<slug>/-/proposals")
def proposal_list(username, slug):
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    if not _is_owner(wiki):
        return render_template("permission_error.html", owner=owner, wiki=wiki), 403

    proposals = (
        Proposal.query
        .filter_by(wiki_id=wiki.id)
        .order_by(Proposal.status.asc(), Proposal.created_at.desc())
        .all()
    )
    return render_template("proposals.html", owner=owner, wiki=wiki, proposals=proposals)


@wiki_bp.route("/@<username>/<slug>/-/proposals/<int:proposal_id>")
def proposal_detail(username, slug, proposal_id):
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    proposal = Proposal.query.filter_by(id=proposal_id, wiki_id=wiki.id).first_or_404()
    revision, patch = _latest_proposal_patch(proposal)
    if not patch:
        abort(404)

    current_page = Page.query.filter_by(wiki_id=wiki.id, path=patch.page_path).first()
    if not _proposal_participant_can_view(proposal, current_page, owner, wiki, patch):
        return render_template("permission_error.html", owner=owner, wiki=wiki), 403

    stale = bool(current_page and patch.base_content_hash and current_page.content_hash != patch.base_content_hash)
    can_resubmit = current_user.is_authenticated and proposal.author_id == current_user.id and proposal.status == "changes_requested"
    return render_template(
        "proposal_detail.html",
        owner=owner,
        wiki=wiki,
        proposal=proposal,
        revision=revision,
        patch=patch,
        revisions=proposal.revisions.order_by(None).order_by(ProposalRevision.revision_number.desc()).all(),
        comments=proposal.comments.order_by(ProposalComment.created_at.asc()).all(),
        diff_lines=_proposal_diff(patch.base_content, patch.proposed_content, patch.page_path),
        stale=stale,
        is_owner=_is_owner(wiki),
        can_resubmit=can_resubmit,
    )


@wiki_bp.route("/@<username>/<slug>/-/proposals/<int:proposal_id>/accept", methods=["POST"])
def accept_proposal(username, slug, proposal_id):
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    if not _is_owner(wiki):
        return render_template("permission_error.html", owner=owner, wiki=wiki), 403

    proposal = Proposal.query.filter_by(id=proposal_id, wiki_id=wiki.id).first_or_404()
    if proposal.status != "pending":
        return redirect(url_for("wiki.proposal_detail", username=owner.username, slug=wiki.slug, proposal_id=proposal.id))

    _, patch = _latest_proposal_patch(proposal)
    if not patch:
        abort(404)

    page = Page.query.filter_by(wiki_id=wiki.id, path=patch.page_path).first()
    if not page:
        abort(404)
    if patch.base_content_hash and page.content_hash != patch.base_content_hash:
        return redirect(url_for("wiki.proposal_detail", username=owner.username, slug=wiki.slug, proposal_id=proposal.id, stale="1"))

    content = set_visibility_in_content(patch.proposed_content, page.visibility)
    frontmatter, _ = parse_markdown_document(content)
    page.visibility = frontmatter.get("visibility") or page.visibility
    page.author = proposal.author_name
    update_page_metadata(page, content, frontmatter)
    refresh_wikilinks_for_page(page, content)

    author_name = proposal.author_name or "anonymous"
    author_email = f"{author_name}@wikihub"
    sync_page_to_repo(
        owner.username,
        wiki.slug,
        page.path,
        content,
        message=f"Accept suggestion #{proposal.id} for {page.path}",
        author_name=author_name,
        author_email=author_email,
    )
    update_mirror_page(owner.username, wiki.slug, page.path, load_acl_rules(owner.username, wiki.slug))

    proposal.status = "accepted"
    proposal.reviewed_by_id = current_user.id
    proposal.reviewed_at = utcnow()
    db.session.commit()
    return redirect(_page_url(owner.username, wiki.slug, page.path))


@wiki_bp.route("/@<username>/<slug>/-/proposals/<int:proposal_id>/reject", methods=["POST"])
def reject_proposal(username, slug, proposal_id):
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    if not _is_owner(wiki):
        return render_template("permission_error.html", owner=owner, wiki=wiki), 403

    proposal = Proposal.query.filter_by(id=proposal_id, wiki_id=wiki.id).first_or_404()
    if proposal.status == "pending":
        proposal.status = "rejected"
        proposal.reviewed_by_id = current_user.id
        proposal.reviewed_at = utcnow()
        db.session.commit()
    return redirect(url_for("wiki.proposal_detail", username=owner.username, slug=wiki.slug, proposal_id=proposal.id))


@wiki_bp.route("/@<username>/<slug>/-/proposals/<int:proposal_id>/comment", methods=["POST"])
def comment_proposal(username, slug, proposal_id):
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    proposal = Proposal.query.filter_by(id=proposal_id, wiki_id=wiki.id).first_or_404()
    _, patch = _latest_proposal_patch(proposal)
    if not patch:
        abort(404)
    current_page = Page.query.filter_by(wiki_id=wiki.id, path=patch.page_path).first()
    if not _proposal_participant_can_view(proposal, current_page, owner, wiki, patch):
        return render_template("permission_error.html", owner=owner, wiki=wiki), 403

    _add_proposal_comment(proposal, request.form.get("body"), event="comment")
    db.session.commit()
    return redirect(url_for("wiki.proposal_detail", username=owner.username, slug=wiki.slug, proposal_id=proposal.id))


@wiki_bp.route("/@<username>/<slug>/-/proposals/<int:proposal_id>/request-changes", methods=["POST"])
def request_proposal_changes(username, slug, proposal_id):
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    if not _is_owner(wiki):
        return render_template("permission_error.html", owner=owner, wiki=wiki), 403

    proposal = Proposal.query.filter_by(id=proposal_id, wiki_id=wiki.id).first_or_404()
    if proposal.status == "pending":
        proposal.status = "changes_requested"
        proposal.reviewed_by_id = current_user.id
        proposal.reviewed_at = utcnow()
        _add_proposal_comment(proposal, request.form.get("body"), event="changes_requested")
        db.session.commit()
    return redirect(url_for("wiki.proposal_detail", username=owner.username, slug=wiki.slug, proposal_id=proposal.id))


@wiki_bp.route("/@<username>/<slug>/-/proposals/<int:proposal_id>/resubmit", methods=["POST"])
def resubmit_proposal(username, slug, proposal_id):
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    proposal = Proposal.query.filter_by(id=proposal_id, wiki_id=wiki.id).first_or_404()
    if not current_user.is_authenticated or proposal.author_id != current_user.id:
        return render_template("permission_error.html", owner=owner, wiki=wiki), 403
    if proposal.status != "changes_requested":
        return redirect(url_for("wiki.proposal_detail", username=owner.username, slug=wiki.slug, proposal_id=proposal.id))

    page = Page.query.filter_by(wiki_id=wiki.id, path=proposal.page_path).first()
    if not page:
        abort(404)
    acl_rules = load_acl_rules(owner.username, wiki.slug)
    if not can_read(proposal.page_path, acl_rules, current_user.username, page.visibility):
        return render_template("permission_error.html", owner=owner, wiki=wiki), 403

    user_can_write = can_write(proposal.page_path, acl_rules, current_user.username, page.visibility)
    read_public = not user_can_write and _use_public_repo(wiki, acl_rules)
    base_content = read_file_from_repo(owner.username, wiki.slug, proposal.page_path, public=read_public)
    if base_content is None:
        abort(404)

    proposed_content = set_visibility_in_content(request.form.get("content", ""), page.visibility)
    note = request.form.get("note", "").strip()
    latest_revision = proposal.revisions.order_by(None).order_by(ProposalRevision.revision_number.desc()).first()
    next_number = (latest_revision.revision_number if latest_revision else 0) + 1

    revision = ProposalRevision(proposal_id=proposal.id, revision_number=next_number, note=note)
    db.session.add(revision)
    db.session.flush()
    db.session.add(ProposalPagePatch(
        revision_id=revision.id,
        page_path=proposal.page_path,
        base_content_hash=page.content_hash,
        base_content=base_content,
        proposed_content=proposed_content,
    ))
    proposal.status = "pending"
    proposal.base_content_hash = page.content_hash
    proposal.reviewed_by_id = None
    proposal.reviewed_at = None
    _add_proposal_comment(proposal, note or f"Submitted revision {next_number}.", event="resubmitted")
    db.session.commit()
    return redirect(url_for("wiki.proposal_detail", username=owner.username, slug=wiki.slug, proposal_id=proposal.id))


@wiki_bp.route("/@<username>/<slug>/<path:page_path>/graph.json")
def page_graph_json(username, slug, page_path):
    """return wikilink graph data for a page as JSON.

    wikihub-8888.2: ACL-gate the central page, and filter the neighborhood
    through the viewer's read access.
    """
    raw_page_path = page_path
    page_path = page_path_from_url_path(page_path)
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    raw_file_path = raw_page_path if raw_page_path.endswith(".md") else raw_page_path + ".md"
    file_path = raw_file_path
    page = Page.query.filter_by(wiki_id=wiki.id, path=file_path).first()
    if page is None and "_" in raw_page_path:
        space_path = page_path if page_path.endswith(".md") else page_path + ".md"
        page = Page.query.filter_by(wiki_id=wiki.id, path=space_path).first()
    from flask import jsonify
    if page is None:
        return jsonify({"nodes": [], "links": []}), 404
    acl_rules = load_acl_rules(owner.username, wiki.slug)
    if not _viewer_can_read_page(wiki, page, acl_rules=acl_rules, owner=owner):
        status = 401 if not current_user.is_authenticated else 403
        return jsonify({"error": "forbidden", "nodes": [], "links": []}), status
    return jsonify(_get_link_graph(page, wiki))


@wiki_bp.route("/@<username>/<slug>/graph.json")
def wiki_graph_json(username, slug):
    """return full wikilink graph for a wiki as JSON. wikihub-8888.2: filter to visible pages."""
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    from flask import jsonify
    return jsonify(_get_full_graph(wiki))


@wiki_bp.route("/@<username>/<slug>/graph")
def wiki_graph(username, slug):
    """full-screen interactive graph view. wikihub-8888.2: filter to visible pages."""
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    return render_template(
        "graph.html",
        owner=owner,
        wiki=wiki,
        graph_data=_get_full_graph(wiki),
    )


@wiki_bp.route("/@<username>/<slug>/tag/<tag_name>")
def wiki_tag_index(username, slug, tag_name):
    """Tag index. wikihub-8888.3: filter pages to those the viewer can read."""
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    # JSON (not JSONB) — filter in Python after pulling pages for this wiki.
    # Volume per-wiki is bounded; this avoids JSONB-only operators.
    candidates = Page.query.filter_by(wiki_id=wiki.id).order_by(Page.title.asc()).all()
    pages = []
    for p in candidates:
        fm = p.frontmatter_json or {}
        tags = fm.get("tags") if isinstance(fm, dict) else None
        if isinstance(tags, str):
            tags = [tags]
        if not isinstance(tags, (list, tuple)):
            continue
        if tag_name in tags:
            pages.append(p)
    if not _is_owner(wiki):
        acl_rules = load_acl_rules(owner.username, wiki.slug)
        pages = [
            p for p in pages
            if _viewer_can_read_page(wiki, p, acl_rules=acl_rules, owner=owner)
        ]
    return render_template("folder.html", owner=owner, wiki=wiki, items=[
        {
            "kind": "page",
            "name": page.title or page.path,
            "path": page.path,
            "url": _page_url(owner.username, wiki.slug, page.path),
            "visibility": page.visibility,
            "updated_at": page.updated_at,
        }
        for page in pages
    ], sidebar_items=_sidebar_for_wiki(owner.username, wiki.slug, wiki, public=not _is_owner(wiki)), folder_path=f"tag:{tag_name}", rendered_html=None, breadcrumb=[("Tags", None), (tag_name, None)])


@wiki_bp.route("/@<username>/<slug>/history")
def wiki_history(username, slug):
    """Wiki history. wikihub-8888.1: ACL-gate; only owners/grantees see authoritative."""
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    acl_rules = load_acl_rules(owner.username, wiki.slug)
    if not _viewer_can_see_any_page(wiki, acl_rules=acl_rules, owner=owner):
        return _render_permission_error(owner, wiki)
    use_public, _ = _repo_access(wiki, acl_rules)
    raw_commits = _git_history(owner.username, wiki.slug, public=use_public)
    # filter out internal event log commits (noise)
    commits = [c for c in raw_commits if not c["message"].startswith("Log ")]
    return render_template("folder.html", owner=owner, wiki=wiki, items=[], sidebar_items=_sidebar_for_wiki(owner.username, wiki.slug, wiki, public=use_public), folder_path="history", rendered_html=None, breadcrumb=[("History", None)], history_commits=commits)


@wiki_bp.route("/@<username>/<slug>/activity")
def wiki_activity(username, slug):
    """Recent wiki activity, filtered to what the current viewer may know."""
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    acl_rules = load_acl_rules(owner.username, wiki.slug)
    if not _viewer_can_see_any_page(wiki, acl_rules=acl_rules, owner=owner):
        return _render_permission_error(owner, wiki)
    use_public, acl_filter_user = _repo_access(wiki, acl_rules)
    return render_template(
        "activity.html",
        owner=owner,
        wiki=wiki,
        activity_items=_wiki_activity_items(owner, wiki, acl_rules),
        sidebar_items=_sidebar_for_wiki(owner.username, wiki.slug, wiki, public=use_public, current_path="activity", acl_filter_user=acl_filter_user),
        recently_updated=_recently_updated_pages(wiki, public_only=use_public and not acl_filter_user),
        sibling_wikis=_sibling_wikis(owner, wiki),
    )


@wiki_bp.route("/@<username>/<slug>/activity.rss")
def wiki_activity_rss(username, slug):
    """Per-wiki RSS feed.

    Visibility mirrors the pages themselves: an anonymous request sees any page
    it could reach by direct link (public AND unlisted), but never private
    pages. Owners/grantees additionally see private pages (via can_read). We
    reuse _viewer_can_read_page so ACL and frontmatter rules stay canonical.
    """
    from app.feeds import activity_entry, render_rss

    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    acl_rules = load_acl_rules(owner.username, wiki.slug)
    if not _viewer_can_see_any_page(wiki, acl_rules=acl_rules, owner=owner):
        return _render_permission_error(owner, wiki)
    query = Page.query.filter_by(wiki_id=wiki.id)
    has_acl_grants = (
        current_user.is_authenticated
        and acl_rules
        and grants_for_user(acl_rules, current_user.username)
    )
    if not _is_owner(wiki) and not has_acl_grants:
        query = query.filter(Page.visibility.in_((
            "public", "public-view", "public-edit",
            "unlisted", "unlisted-view", "unlisted-edit",
        )))
    query = query.order_by(Page.updated_at.desc(), Page.id.desc())
    visible = []
    offset = 0
    batch_size = 200
    while len(visible) < 50:
        candidates = query.offset(offset).limit(batch_size).all()
        if not candidates:
            break
        for p in candidates:
            if _viewer_can_read_page(wiki, p, acl_rules=acl_rules, owner=owner):
                visible.append(p)
                if len(visible) == 50:
                    break
        offset += batch_size
    entries = [
        activity_entry(p, wiki, owner.username, request.host_url) for p in visible
    ]
    site = request.host_url.rstrip("/")
    wiki_url = f"{site}/@{owner.username}/{wiki.slug}"
    xml = render_rss(
        feed_title=f"{wiki.title or wiki.slug} — activity",
        feed_link=wiki_url,
        self_url=f"{wiki_url}/activity.rss",
        description=f"Recent page activity in {wiki.title or wiki.slug}.",
        entries=entries,
    )
    return Response(xml, mimetype="application/rss+xml")


@wiki_bp.route("/@<username>/<slug>/<path:folder_path>/history")
def page_history(username, slug, folder_path):
    """Page/folder history. wikihub-8888.1: ACL-gate against the specific page when one exists."""
    raw_folder_path = folder_path
    folder_path = page_path_from_url_path(folder_path)
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    path = raw_folder_path if raw_folder_path.endswith(".md") else f"{raw_folder_path}.md"
    acl_rules = load_acl_rules(owner.username, wiki.slug)
    # If there's a Page row at this exact path, gate on it. If not (folder
    # history, deleted page), fall back to the wiki-level "any visible page"
    # check.
    page = Page.query.filter_by(wiki_id=wiki.id, path=path).first()
    if page is not None:
        if not _viewer_can_read_page(wiki, page, acl_rules=acl_rules, owner=owner):
            return _render_permission_error(owner, wiki)
    elif not _viewer_can_see_any_page(wiki, acl_rules=acl_rules, owner=owner):
        return _render_permission_error(owner, wiki)
    use_public, _ = _repo_access(wiki, acl_rules)
    raw_commits = _git_history(owner.username, wiki.slug, public=use_public, path=path)
    commits = [c for c in raw_commits if not c["message"].startswith("Log ")]
    return render_template("folder.html", owner=owner, wiki=wiki, items=[], sidebar_items=_sidebar_for_wiki(owner.username, wiki.slug, wiki, public=use_public, current_path=path), folder_path=f"{path} history", rendered_html=None, breadcrumb=[("History", None)], history_commits=commits)


@wiki_bp.route("/@<username>/<slug>/commit/<sha>")
def wiki_commit(username, slug, sha):
    """show diff for a single commit. wikihub-8888.1: never fall back to authoritative for non-owner/non-grantee."""
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    acl_rules = load_acl_rules(owner.username, wiki.slug)
    if not _viewer_can_see_any_page(wiki, acl_rules=acl_rules, owner=owner):
        return _render_permission_error(owner, wiki)
    use_public, _ = _repo_access(wiki, acl_rules)

    # Owners/grantees read authoritative directly; everyone else is hard-gated
    # to the public mirror (no fallback — falling back leaks private diffs).
    diff_text = _git_diff(owner.username, wiki.slug, sha, public=use_public)
    if diff_text is None:
        abort(404)

    # parse diff into structured hunks for rendering
    diff_lines = []
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            diff_lines.append(("meta", line))
        elif line.startswith("@@"):
            diff_lines.append(("hunk", line))
        elif line.startswith("+"):
            diff_lines.append(("add", line))
        elif line.startswith("-"):
            diff_lines.append(("del", line))
        elif line.startswith("diff --git"):
            diff_lines.append(("file", line))
        else:
            diff_lines.append(("ctx", line))

    # get commit metadata
    history = _git_history(owner.username, wiki.slug, public=use_public, limit=200)
    commit = next((c for c in history if c["sha"] == sha), None)

    return render_template(
        "diff.html",
        owner=owner,
        wiki=wiki,
        sha=sha,
        commit=commit,
        diff_lines=diff_lines,
        # wikihub-8vwd: _sidebar_for_wiki returns None for large wikis (client
        # fetches sidebar.json). diff.html iterates sidebar_items directly, so
        # coalesce to [] here AND guard the template loop to prevent
        # 'NoneType is not iterable'.
        sidebar_items=_sidebar_for_wiki(owner.username, wiki.slug, wiki, public=use_public) or [],
    )


@wiki_bp.route("/@<username>/<slug>/<path:page_path>", strict_slashes=False)
def wiki_page(username, slug, page_path):
    raw_page_path = page_path

    # Wikipedia-style URLs: redirect space-encoded URLs to underscore form.
    if " " in raw_page_path:
        canonical_path = url_path_from_page_path(raw_page_path, strip_md=False)
        canonical_url = f"/@{username}/{slug}/{canonical_path}"
        if request.path.endswith("/"):
            canonical_url += "/"
        return redirect(canonical_url, code=301)

    # Normalize: underscores in URL → spaces for filesystem lookup
    page_path = page_path_from_url_path(raw_page_path)

    # root index/README should be served by wiki_index, not as a standalone page
    if page_path.rstrip("/") in ("index", "index.md", "README", "README.md"):
        return redirect(url_for("wiki.wiki_index", username=username, slug=slug), code=302)

    owner, wiki, redirect_row = _get_owner_and_wiki_or_404(username, slug)
    if redirect_row:
        redirect_path = url_path_from_page_path(page_path, strip_md=False)
        return redirect(f"/@{owner.username}/{wiki.slug}/{redirect_path}", code=302)

    siblings = _sibling_wikis(owner, wiki)

    # Non-markdown files: inline preview for known-safe formats, download for everything else.
    # Markdown is the only first-class format (chrome, sidebar, search, graph). Other extensions
    # are second-class: viewable/downloadable but not part of the wiki graph.
    _MARKDOWN_EXTS = {".md", ".markdown", ".mdown", ".mkd"}
    _INLINE_EXTS = {
        # text — served as text/plain (or specific text mime); browsers render inline
        ".txt", ".log", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".toml",
        # images — browsers render inline
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
        # PDF — browsers render in built-in viewer
        ".pdf",
        # audio/video — browsers play in built-in player
        ".mp3", ".mp4", ".wav", ".ogg", ".webm",
        # fonts
        ".woff", ".woff2", ".ttf", ".eot",
        # archives — browsers download these naturally via correct mime
        ".zip", ".tar", ".gz",
    }
    # Active-content (HTML) extensions: a *top-level navigation* to one of these
    # executes script in the wiki origin, so they are only served inline when the
    # OWNER explicitly opts the path in via .wikihub/serve-inline (see wikihub-6ag).
    # Default stays attachment to prevent stored-XSS against other wikihub users'
    # session cookies.
    #
    # NOTE: .svg is deliberately NOT here. SVG is in _INLINE_EXTS and served as
    # image/svg+xml so markdown image embeds (![[diagram.svg]] -> <img src>) keep
    # working; an <img>-loaded SVG cannot run scripts. (Gating SVG by default would
    # regress embedded diagrams.) Owners who store script-bearing SVGs as standalone
    # documents accept the same direct-navigation behavior SVG has always had.
    _ACTIVE_EXTS = {".html", ".htm", ".xhtml"}
    ext = os.path.splitext(page_path)[1].lower()
    if ext and ext not in _MARKDOWN_EXTS and not request.path.endswith("/"):
        import mimetypes
        is_owner = _is_owner(wiki)
        acl_rules = load_acl_rules(owner.username, wiki.slug)
        user_name = current_user.username if current_user.is_authenticated else None
        # Non-markdown files can still have a Page row with explicit visibility
        # (set via the API). Page-row visibility wins over the file-path ACL.
        page = Page.query.filter_by(wiki_id=wiki.id, path=page_path).first()
        file_vis = page.visibility if page else resolve_visibility(page_path, acl_rules)
        if not is_owner and not can_read(page_path, acl_rules, user_name, file_vis):
            # wikihub-dkp8: a non-markdown file with an explicit Page row EXISTS
            # but is restricted → 403 restricted screen (agents get JSON). Only a
            # bare path with no Page row (unknown file) falls through to 404.
            if page is not None:
                if "text/html" not in request.headers.get("Accept", ""):
                    return _restricted_json()
                return _render_restricted(owner, wiki)
            abort(404)
        use_public = _use_public_repo(wiki, acl_rules)
        data = read_file_bytes_from_repo(owner.username, wiki.slug, page_path, public=use_public)
        if data is None and use_public:
            data = read_file_bytes_from_repo(owner.username, wiki.slug, page_path, public=False)
        if data is None:
            # File not in repo with this extension — fall through to markdown path
            # (which will try .md suffix and 404 if nothing matches).
            pass
        else:
            headers = {"Cache-Control": "public, max-age=3600"}
            # Owner opt-in: serve an active-content file (HTML/SVG/etc.) inline as
            # its real type, but ONLY if the path is allowlisted in
            # .wikihub/serve-inline. Hardened with a CSP sandbox + nosniff so the
            # rendered document is isolated (no same-origin DOM/cookie access).
            # Read from the authoritative repo (NOT use_public): .wikihub/* plumbing
            # files are stripped from the public mirror, so passing public=True here
            # always returned [] and the opt-in never applied (wikihub-6ag bug).
            serve_inline_patterns = load_serve_inline_patterns(
                owner.username, wiki.slug
            )
            opted_in = matches_serve_inline(page_path, serve_inline_patterns)
            # Embedded viewer (wikihub-ntpc): ?view=1 on an opted-in HTML deck
            # renders the deck inside the wiki reader chrome via a sandboxed
            # iframe + '↗ open' pop-out, instead of replacing the page with the
            # bare standalone deck (wikihub-6ag, unchanged). The iframe src and
            # pop-out both point at the raw .html URL (no ?view).
            if ext in _ACTIVE_EXTS and request.args.get("view") and opted_in:
                raw_url = f"/@{owner.username}/{wiki.slug}/{url_path_from_page_path(page_path, strip_md=False)}"
                basename = os.path.basename(page_path)
                rendered_html = build_html_embed_figure(raw_url, basename, height=720)
                # reader.html derives its breadcrumb from page.path internally.
                viewer_page = type("Page", (), {
                    "path": page_path,
                    "title": basename,
                    "excerpt": None,
                    "visibility": file_vis,
                    "updated_at": page.updated_at if page else wiki.updated_at,
                })()
                use_public_viewer, acl_filter_user_viewer = _repo_access(wiki, acl_rules)
                return render_template(
                    "reader.html",
                    owner=owner,
                    wiki=wiki,
                    page=viewer_page,
                    rendered_html=rendered_html,
                    toc=[],
                    backlinks=[],
                    link_graph={"nodes": [], "edges": []},
                    recently_updated=_recently_updated_pages(wiki, public_only=use_public_viewer and not acl_filter_user_viewer),
                    sidebar_items=_sidebar_for_wiki(owner.username, wiki.slug, wiki, public=use_public_viewer, current_path=page_path, acl_filter_user=acl_filter_user_viewer),
                    private_band_warning=False,
                    json_ld_author=owner.display_name or owner.username,
                    sibling_wikis=siblings,
                    page_grants=[],
                    user_can_edit=False,
                    pending_proposals_count=0,
                )
            if ext in _ACTIVE_EXTS:
                # Stored HTML carries a leading YAML frontmatter block (visibility
                # etc.) that is indexed into the Page row. Strip it before serving
                # so it doesn't render as literal "--- visibility: ... ---" text at
                # the top of the document (or land in a downloaded file). (wikihub-htfm)
                try:
                    from app.content_utils import parse_markdown_document
                    _meta, _body = parse_markdown_document(data.decode("utf-8"))
                    if _meta:
                        data = _body.encode("utf-8")
                except (UnicodeDecodeError, ValueError):
                    pass
                if opted_in:
                    content_type = mimetypes.guess_type(page_path)[0] or "text/html"
                    headers["Content-Disposition"] = f'inline; filename="{os.path.basename(page_path)}"'
                    # Hardening: sandbox the document (scripts may run but in a
                    # null origin — no access to wikihub cookies / same-origin DOM),
                    # and forbid MIME sniffing.
                    headers["Content-Security-Policy"] = "sandbox allow-scripts allow-popups allow-forms"
                    headers["X-Content-Type-Options"] = "nosniff"
                    return Response(data, content_type=content_type, headers=headers)
                # Not opted in: force download. (Default safe behavior.)
                content_type = "application/octet-stream"
                headers["Content-Disposition"] = f'attachment; filename="{os.path.basename(page_path)}"'
                headers["X-Content-Type-Options"] = "nosniff"
                return Response(data, content_type=content_type, headers=headers)
            if ext in _INLINE_EXTS:
                # Known-safe passive types: serve with their natural Content-Type so
                # browsers render/preview them (images, PDF, text, audio/video, ...).
                content_type = mimetypes.guess_type(page_path)[0] or "application/octet-stream"
                # nosniff stops a passive type from being reinterpreted as HTML.
                headers["X-Content-Type-Options"] = "nosniff"
                if content_type == "application/pdf":
                    headers["Content-Disposition"] = f'inline; filename="{os.path.basename(page_path)}"'
            else:
                # Unknown / unrecognized extension: force octet-stream + attachment.
                # Still downloadable, but never executed in the wiki origin.
                content_type = "application/octet-stream"
                headers["Content-Disposition"] = f'attachment; filename="{os.path.basename(page_path)}"'
                headers["X-Content-Type-Options"] = "nosniff"
            return Response(data, content_type=content_type, headers=headers)

    if request.path.endswith("/"):
        acl_rules = load_acl_rules(owner.username, wiki.slug)
        use_public, acl_filter_user = _repo_access(wiki, acl_rules)
        content_path, content = _folder_index_content(owner.username, wiki.slug, page_path, public=use_public)
        items = _folder_listing(owner.username, wiki.slug, wiki, page_path, public=use_public, acl_filter_user=acl_filter_user)
        if use_public and not content and not items:
            # wikihub-dkp8: distinguish "folder exists but its pages are
            # restricted" (403) from "no such folder" (404). A folder exists iff
            # any page's path lives under it.
            folder_prefix = page_path.strip("/") + "/"
            has_hidden = Page.query.filter_by(wiki_id=wiki.id).filter(
                Page.path.like(folder_prefix.replace("%", r"\%").replace("_", r"\_") + "%", escape="\\")
            ).first() is not None
            if has_hidden:
                return _render_restricted(owner, wiki)
            return render_template("permission_error.html", owner=owner, wiki=wiki), 404
        breadcrumb = []
        running = []
        for segment in page_path.strip("/").split("/"):
            if not segment:
                continue
            running.append(segment)
            breadcrumb.append((segment, _folder_url(owner.username, wiki.slug, "/".join(running))))
        return render_template(
            "folder.html",
            owner=owner,
            wiki=wiki,
            items=items,
            sidebar_items=_sidebar_for_wiki(owner.username, wiki.slug, wiki, public=use_public, current_path=page_path.strip("/"), acl_filter_user=acl_filter_user),
            folder_path=page_path.strip("/"),
            rendered_html=render_page(content, owner.username, wiki.slug, current_page_path=content_path) if content else None,
            breadcrumb=breadcrumb,
            private_band_warning=bool(content and not use_public and has_private_bands(content)),
            sibling_wikis=siblings,
        )

    # Try literal URL path first (preserves underscores in filenames),
    # then fall back to underscore→space (Wikipedia-style convenience).
    # This matches Gollum 5.0's approach: exact match first, lenient fallback second.
    raw_file_path = raw_page_path if raw_page_path.endswith(".md") else raw_page_path + ".md"
    file_path = raw_file_path
    page = Page.query.filter_by(wiki_id=wiki.id, path=file_path).first()

    if page is None and "_" in raw_page_path:
        space_path = page_path if page_path.endswith(".md") else page_path + ".md"
        page = Page.query.filter_by(wiki_id=wiki.id, path=space_path).first()
        if page:
            file_path = space_path

    acl_rules = load_acl_rules(owner.username, wiki.slug)
    user_name = current_user.username if current_user.is_authenticated else None
    is_owner = _is_owner(wiki)

    if page and not is_owner and not can_read(page.path, acl_rules, user_name, page.visibility):
        # wikihub-dkp8: the page EXISTS but this viewer can't read it. Return a
        # distinct "restricted" signal (403/401) instead of the ambiguous 404 —
        # existence-but-no-access reads very differently from "never created".
        # wikihub-3rjt: agent requests (Accept: text/markdown, or .md suffix
        # with markdown Accept) must NOT receive HTML permission_error — that
        # would be parsed as the page's content. Return JSON 4xx with
        # WWW-Authenticate hint so agents know auth is required.
        wants_markdown_now = "text/markdown" in request.headers.get("Accept", "")
        is_md_url = raw_page_path.endswith(".md") or page_path.endswith(".md")
        if wants_markdown_now or (is_md_url and not request.headers.get("Accept", "").startswith("text/html")):
            return _restricted_json()
        return _render_restricted(owner, wiki)

    use_public, acl_filter_user = _repo_access(wiki, acl_rules)
    # For individual pages: if the page is private, always read from authoritative repo
    if not use_public:
        content = read_file_from_repo(owner.username, wiki.slug, file_path, public=False)
    else:
        page_vis = page.visibility if page else "public"
        if page_vis == "private":
            # Private page but user passed can_read above — read from authoritative repo
            content = read_file_from_repo(owner.username, wiki.slug, file_path, public=False)
        else:
            content = read_file_from_repo(owner.username, wiki.slug, file_path, public=True)
    if content is None:
        # fallback: try authoritative repo (handles edge cases)
        content = read_file_from_repo(owner.username, wiki.slug, file_path, public=False)
    if content is None:
        # wikihub-3rjt: agent-friendly content negotiation on miss. If the
        # client asked for markdown (or used a .md URL without HTML Accept),
        # return JSON 4xx so they can distinguish "missing/private" from
        # "valid markdown page" instead of getting an HTML 404 body.
        wants_markdown_miss = "text/markdown" in request.headers.get("Accept", "")
        is_md_url_miss = raw_page_path.endswith(".md") or page_path.endswith(".md")
        accept_hdr = request.headers.get("Accept", "")
        if wants_markdown_miss or (is_md_url_miss and "text/html" not in accept_hdr):
            from flask import jsonify, make_response
            status = 401 if not current_user.is_authenticated else 404
            body = {
                "error": "authentication_required" if status == 401 else "not_found",
                "message": "Page is private, does not exist, or you lack access",
                "sign_in_url": "https://wikihub.md/auth/login",
            }
            resp = make_response(jsonify(body), status)
            resp.headers["Cache-Control"] = "no-store"
            if status == 401:
                resp.headers["WWW-Authenticate"] = 'Bearer realm="wikihub"'
            return resp
        abort(404)

    wants_markdown = "text/markdown" in request.headers.get("Accept", "")
    html_url_path = url_path_from_page_path(raw_page_path if raw_page_path.endswith(".md") else page_path, strip_md=True)
    md_url_path = url_path_from_page_path(file_path, strip_md=False)

    # .md in URL: browsers get redirected to the clean HTML URL,
    # API clients requesting text/markdown get raw markdown.
    if page_path.endswith(".md") and not wants_markdown:
        redirect_url = f"/@{owner.username}/{wiki.slug}/{html_url_path}"
        if request.args.get("fragment"):
            redirect_url += "?fragment=1"
        return redirect(redirect_url, code=302)
    if wants_markdown:
        return Response(
            content,
            content_type="text/markdown; charset=utf-8",
            headers={
                "Vary": "Accept",
                "Link": f'</@{owner.username}/{wiki.slug}/{html_url_path}>; rel="alternate"; type="text/html"',
            },
        )

    if not page:
        page = type("Page", (), {"path": file_path, "title": os.path.basename(file_path).replace(".md", ""), "visibility": resolve_visibility(file_path, acl_rules), "updated_at": wiki.updated_at})()

    rendered_html = render_page(content, owner.username, wiki.slug, current_page_path=file_path)

    # Side peek (wikihub-9k18): ?fragment=1 returns the rendered article body
    # only (no chrome) so a reader can open a same-wiki link in a slide-over
    # panel without navigating away. This flows through the SAME ACL checks
    # above (private pages already abort 404 before reaching here), so the
    # fragment endpoint never leaks private content the full page wouldn't.
    if request.args.get("fragment"):
        from flask import jsonify
        page_title = getattr(page, "title", None) or os.path.basename(file_path).replace(".md", "")
        canonical = f"/@{owner.username}/{wiki.slug}/{html_url_path}"
        resp = jsonify({
            "title": page_title,
            "html": rendered_html,
            "url": canonical,
            "path": html_url_path,
        })
        resp.headers["Cache-Control"] = "no-store"
        return resp

    page_grants = resolve_grants(file_path, acl_rules) if is_owner else []
    user_can_edit = is_owner or can_write(file_path, acl_rules, user_name, page.visibility if page else None)
    pending_proposals_count = 0
    if is_owner:
        pending_proposals_count = Proposal.query.filter_by(
            wiki_id=wiki.id,
            page_path=file_path,
            status="pending",
        ).count()
    return render_template(
        "reader.html",
        owner=owner,
        wiki=wiki,
        page=page,
        rendered_html=rendered_html,
        toc=extract_toc(rendered_html),
        backlinks=_get_backlinks(page),
        link_graph=_get_link_graph(page, wiki),
        recently_updated=_recently_updated_pages(wiki, public_only=use_public and not acl_filter_user),
        sidebar_items=_sidebar_for_wiki(owner.username, wiki.slug, wiki, public=use_public, current_path=file_path, acl_filter_user=acl_filter_user),
        private_band_warning=not use_public and has_private_bands(content),
        json_ld_author=owner.display_name or owner.username,
        sibling_wikis=siblings,
        page_grants=page_grants,
        user_can_edit=user_can_edit,
        pending_proposals_count=pending_proposals_count,
    ), 200, {
        "Vary": "Accept",
        "Link": f'</@{owner.username}/{wiki.slug}/{md_url_path}>; rel="alternate"; type="text/markdown"',
    }


@wiki_bp.route("/@<username>/<slug>/preview", methods=["POST"])
def preview_page(username, slug):
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    content = request.get_json(silent=True) or {}
    markdown = content.get("content", "")
    html = render_page(markdown, owner.username, wiki.slug)
    return {"html": html}


@wiki_bp.route("/@<username>/<slug>/<path:page_path>/edit", methods=["GET", "POST"])
def edit_page(username, slug, page_path):
    raw_page_path = page_path
    page_path = page_path_from_url_path(page_path)
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    raw_file_path = raw_page_path if raw_page_path.endswith(".md") else raw_page_path + ".md"
    file_path = raw_file_path
    page = Page.query.filter_by(wiki_id=wiki.id, path=file_path).first()
    if page is None and "_" in raw_page_path:
        space_path = page_path if page_path.endswith(".md") else page_path + ".md"
        page = Page.query.filter_by(wiki_id=wiki.id, path=space_path).first()
        if page:
            file_path = space_path
    acl_rules = load_acl_rules(owner.username, wiki.slug)
    is_owner = _is_owner(wiki)
    username_for_acl = current_user.username if current_user.is_authenticated else None

    if not is_owner and not can_write(file_path, acl_rules, username_for_acl, page.visibility if page else None):
        return _render_permission_error(owner, wiki)

    if request.method == "POST":
        content = request.form.get("content", "")
        new_path = request.form.get("path", "").strip()
        if new_path and not new_path.endswith(".md"):
            new_path += ".md"
        target_path = new_path if (is_owner and new_path) else file_path
        renamed = target_path != file_path

        visibility = request.form.get("visibility", page.visibility if page else resolve_visibility(target_path, acl_rules))
        if is_owner:
            content = set_visibility_in_content(content, visibility)
        elif page:
            content = set_visibility_in_content(content, page.visibility)

        frontmatter, _ = parse_markdown_document(content)
        page_visibility = frontmatter.get("visibility") or resolve_visibility(target_path, acl_rules)

        if renamed and page:
            remove_page_from_repo(owner.username, wiki.slug, file_path)
            page.path = target_path
        elif not page:
            page = Page(wiki_id=wiki.id, path=target_path)
            db.session.add(page)

        page.visibility = page_visibility
        page.author = current_user.username if current_user.is_authenticated else None
        update_page_metadata(page, content, frontmatter)
        db.session.flush()
        refresh_wikilinks_for_page(page, content)
        author_name = current_user.username if current_user.is_authenticated else "anonymous"
        author_email = f"{author_name}@wikihub" if current_user.is_authenticated else "anon@wikihub"
        msg = f"Rename {file_path} → {target_path}" if renamed else f"Update {target_path}"
        sync_page_to_repo(owner.username, wiki.slug, target_path, content, message=msg, author_name=author_name, author_email=author_email)
        if renamed:
            regenerate_public_mirror(owner.username, wiki.slug, acl_rules)
        else:
            update_mirror_page(owner.username, wiki.slug, target_path, acl_rules)
        db.session.commit()
        return redirect(_page_url(owner.username, wiki.slug, target_path))

    content = read_file_from_repo(owner.username, wiki.slug, file_path, public=False) or ""
    visibility = page.visibility if page else resolve_visibility(file_path, acl_rules)

    page_grants = resolve_grants(file_path, acl_rules) if is_owner else []
    return render_template(
        "editor.html",
        owner=owner,
        wiki=wiki,
        page_path=file_path,
        content=content,
        visibility=visibility,
        is_owner=is_owner,
        page_grants=page_grants,
        existing_page=bool(page),
        initial_content_hash=(page.content_hash if page else None),
    )


@wiki_bp.route("/@<username>/<slug>/new", methods=["GET", "POST"])
def new_page(username, slug):
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    is_owner = _is_owner(wiki)
    acl_rules = load_acl_rules(owner.username, wiki.slug)
    username_for_acl = current_user.username if current_user.is_authenticated else None

    if request.method == "POST":
        page_path = request.form.get("path", "").strip()
        if not page_path.endswith(".md"):
            page_path += ".md"
        if not is_owner and not can_write(page_path, acl_rules, username_for_acl):
            return _render_permission_error(owner, wiki)
        content = request.form.get("content", "")

        visibility = request.form.get("visibility", resolve_visibility(page_path, acl_rules))
        if is_owner:
            content = set_visibility_in_content(content, visibility)

        frontmatter, _ = parse_markdown_document(content)
        page_visibility = frontmatter.get("visibility") or resolve_visibility(page_path, acl_rules)

        page = Page(wiki_id=wiki.id, path=page_path)
        db.session.add(page)
        page.visibility = page_visibility
        page.author = current_user.username if current_user.is_authenticated else None
        update_page_metadata(page, content, frontmatter)
        db.session.flush()
        refresh_wikilinks_for_page(page, content)
        author_name = current_user.username if current_user.is_authenticated else "anonymous"
        author_email = f"{author_name}@wikihub" if current_user.is_authenticated else "anon@wikihub"
        sync_page_to_repo(owner.username, wiki.slug, page_path, content, message=f"Create {page_path}", author_name=author_name, author_email=author_email)
        update_mirror_page(owner.username, wiki.slug, page_path, acl_rules)
        db.session.commit()
        return redirect(_page_url(owner.username, wiki.slug, page_path))

    requested_path = request.args.get("path", "").strip()
    if requested_path:
        # If path ends with / it's a folder prefix — generate a new page name inside it
        if requested_path.endswith("/"):
            prefix = requested_path
            base = f"{prefix}new-page"
            page_path = f"{base}.md"
            n = 2
            while Page.query.filter_by(wiki_id=wiki.id, path=page_path).first():
                page_path = f"{base}-{n}.md"
                n += 1
        else:
            page_path = requested_path if requested_path.endswith(".md") else requested_path + ".md"
    else:
        base = "new-page"
        page_path = f"{base}.md"
        n = 2
        while Page.query.filter_by(wiki_id=wiki.id, path=page_path).first():
            page_path = f"{base}-{n}.md"
            n += 1
    if not is_owner and not can_write(page_path, acl_rules, username_for_acl):
        return _render_permission_error(owner, wiki)
    default_vis = resolve_visibility(page_path, acl_rules)
    return render_template(
        "editor.html",
        owner=owner,
        wiki=wiki,
        page_path=page_path,
        content="",
        visibility=default_vis,
        is_owner=_is_owner(wiki),
        existing_page=False,
        initial_content_hash=None,
    )


@wiki_bp.route("/@<username>/<slug>/new-folder", methods=["GET", "POST"])
def new_folder(username, slug):
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    if not _is_owner(wiki):
        return _render_permission_error(owner, wiki)

    acl_rules = load_acl_rules(owner.username, wiki.slug)
    parent_path = page_path_from_url_path(request.values.get("parent", "").strip().strip("/"))

    if request.method == "POST":
        folder_name = request.form.get("folder_name", "").strip().strip("/")
        if folder_name and parent_path:
            folder_path = _normalize_folder_path(f"{parent_path}/{folder_name}")
        else:
            folder_path = _normalize_folder_path(folder_name)
        if not folder_path:
            return render_template(
                "new_folder.html",
                owner=owner,
                wiki=wiki,
                parent_path=parent_path,
                default_visibility=resolve_visibility(f"{(parent_path + '/' if parent_path else '')}new-folder/index.md", acl_rules),
                error="Enter a valid folder path.",
            ), 400

        file_path = f"{folder_path}/index.md"
        existing_content = read_file_from_repo(owner.username, wiki.slug, file_path, public=False)
        if existing_content is not None:
            edit_path = url_path_from_page_path(f"{folder_path}/index", strip_md=True)
            return redirect(f"/@{owner.username}/{wiki.slug}/{edit_path}/edit")

        visibility = request.form.get("visibility") or resolve_visibility(file_path, acl_rules)
        folder_title = folder_path.split("/")[-1].replace("-", " ").replace("_", " ").title()
        content = (
            f"---\n"
            f"title: {folder_title}\n"
            f"visibility: {visibility}\n"
            f"---\n\n"
            f"# {folder_title}\n\n"
        )

        page = Page.query.filter_by(wiki_id=wiki.id, path=file_path).first()
        if not page:
            page = Page(wiki_id=wiki.id, path=file_path)
            db.session.add(page)

        frontmatter, _ = parse_markdown_document(content)
        page.visibility = frontmatter.get("visibility") or resolve_visibility(file_path, acl_rules)
        page.author = current_user.username
        update_page_metadata(page, content, frontmatter)
        db.session.flush()
        refresh_wikilinks_for_page(page, content)
        sync_page_to_repo(
            owner.username,
            wiki.slug,
            file_path,
            content,
            message=f"Create folder {folder_path}",
            author_name=current_user.username,
            author_email=f"{current_user.username}@wikihub",
        )
        update_mirror_page(owner.username, wiki.slug, file_path, acl_rules)
        db.session.commit()
        edit_path = url_path_from_page_path(f"{folder_path}/index", strip_md=True)
        return redirect(f"/@{owner.username}/{wiki.slug}/{edit_path}/edit")

    default_target = f"{(parent_path + '/' if parent_path else '')}new-folder/index.md"
    return render_template(
        "new_folder.html",
        owner=owner,
        wiki=wiki,
        parent_path=parent_path,
        default_visibility=resolve_visibility(default_target, acl_rules),
        error=None,
    )


@wiki_bp.route("/@<username>/<slug>/reindex", methods=["POST"])
def reindex_wiki(username, slug):
    owner = User.query.filter_by(username=username).first_or_404()
    wiki = Wiki.query.filter_by(owner_id=owner.id, slug=slug).first_or_404()
    if not current_user.is_authenticated or current_user.id != owner.id:
        abort(403)
    if not os.path.isdir(_repo_path(username, slug)):
        return jsonify(ok=False, message="no git repo found for this wiki"), 422
    index_repo_pages(username, slug, wiki, reset=True)
    regenerate_public_mirror(username, slug, load_acl_rules(username, slug))
    db.session.commit()
    return jsonify(ok=True, reindexed=1)


@wiki_bp.route("/@<username>/reindex", methods=["POST"])
def reindex_all_wikis(username):
    owner = User.query.filter_by(username=username).first_or_404()
    if not current_user.is_authenticated or current_user.id != owner.id:
        abort(403)
    wikis = Wiki.query.filter_by(owner_id=owner.id).all()
    count = 0
    for wiki in wikis:
        if not os.path.isdir(_repo_path(username, wiki.slug)):
            continue
        index_repo_pages(username, wiki.slug, wiki, reset=True)
        regenerate_public_mirror(username, wiki.slug, load_acl_rules(username, wiki.slug))
        count += 1
    db.session.commit()
    return jsonify(ok=True, reindexed=count)
