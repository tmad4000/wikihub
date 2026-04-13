import os
import subprocess
from datetime import timezone

from flask import Response, abort, jsonify, redirect, render_template, request, url_for

from app.url_utils import page_path_from_url_path, url_path_from_page_path
from flask_login import current_user

from app import db
from app.acl import can_read, can_write, grants_for_user, resolve_grants, resolve_visibility
from app.content_utils import has_private_bands, parse_markdown_document, set_visibility_in_content
from app.discovery import discoverable_page_for_wiki, visible_wikis_for_owner
from app.git_backend import _repo_path
from app.git_sync import read_file_from_repo, read_file_bytes_from_repo, list_files_in_repo, regenerate_public_mirror, remove_page_from_repo, sync_page_to_repo, update_mirror_page
from app.models import Page, User, UsernameRedirect, Wiki, WikiSlugRedirect, Wikilink, utcnow
from app.renderer import extract_toc, render_page
from app.routes import wiki_bp
from app.wiki_ops import index_repo_pages, load_acl_rules, refresh_wikilinks_for_page, sync_wiki_counters, update_page_metadata


def _recently_updated_pages(wiki, limit=8, public_only=False):
    """get the most recently updated pages for a wiki."""
    query = Page.query.filter_by(wiki_id=wiki.id)
    if public_only:
        query = query.filter(Page.visibility.in_(('public', 'public-view', 'public-edit')))
    return query.order_by(Page.updated_at.desc()).limit(limit).all()


def _get_backlinks(page):
    """get pages that link to this page via wikilinks."""
    if not page or not hasattr(page, 'id') or not page.id:
        return []
    links = (
        Wikilink.query
        .filter_by(target_page_id=page.id)
        .join(Page, Wikilink.source_page_id == Page.id)
        .add_entity(Page)
        .all()
    )
    return [source_page for _, source_page in links]


def _get_link_graph(page, wiki):
    """get wikilink graph centered on a page (outgoing + incoming links)."""
    if not page or not hasattr(page, 'id') or not page.id:
        return {"nodes": [], "links": []}

    nodes = {}
    links = []
    page_url = _page_url(wiki.owner.username, wiki.slug, page.path)
    nodes[page.id] = {"id": page.id, "title": page.title or page.path.replace('.md', ''), "url": page_url, "current": True}

    # outgoing links
    outgoing = Wikilink.query.filter_by(source_page_id=page.id).all()
    for wl in outgoing:
        if wl.target_page_id and wl.target_page_id not in nodes:
            tgt = db.session.get(Page, wl.target_page_id)
            if tgt:
                nodes[tgt.id] = {"id": tgt.id, "title": tgt.title or tgt.path.replace('.md', ''), "url": _page_url(wiki.owner.username, wiki.slug, tgt.path), "current": False}
        if wl.target_page_id:
            links.append({"source": page.id, "target": wl.target_page_id})

    # incoming links (backlinks)
    incoming = Wikilink.query.filter_by(target_page_id=page.id).all()
    for wl in incoming:
        if wl.source_page_id not in nodes:
            src = db.session.get(Page, wl.source_page_id)
            if src:
                nodes[src.id] = {"id": src.id, "title": src.title or src.path.replace('.md', ''), "url": _page_url(wiki.owner.username, wiki.slug, src.path), "current": False}
        links.append({"source": wl.source_page_id, "target": page.id})

    return {"nodes": list(nodes.values()), "links": links}


def _get_full_graph(wiki):
    """get full wikilink graph for an entire wiki."""
    pages = Page.query.filter_by(wiki_id=wiki.id).all()
    owner = db.session.get(User, wiki.owner_id)

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


def _sibling_wikis(owner, current_wiki):
    """other wikis by the same owner, for cross-wiki nav in sidebar."""
    from app.discovery import visible_wikis_for_owner
    wikis = visible_wikis_for_owner(owner, current_user)
    return [w for w in wikis if w.id != current_wiki.id]


def _normalize_folder_path(raw_path):
    clean = (raw_path or "").replace("\\", "/").strip().strip("/")
    clean = clean.removesuffix("/index.md").removesuffix("/index").removesuffix(".md")
    segments = [segment for segment in clean.split("/") if segment]
    if not segments or any(segment in {".", ".."} for segment in segments):
        return None
    return "/".join(segments)


def _visible_files(username, slug, wiki, public=False, acl_filter_user=None):
    """List visible files in a wiki repo.

    Args:
        public: If True, read from public mirror.
        acl_filter_user: If set, read from authoritative repo but filter
            through can_read for this username (for ACL grantees who aren't owners).
    """
    files = list_files_in_repo(username, slug, public=public)
    if not public and not acl_filter_user:
        # owner view — everything
        return [path for path in files if not path.startswith(".wikihub/") and not path.endswith(".gitkeep")]

    if acl_filter_user:
        # non-owner with ACL grants — read authoritative repo, filter by can_read
        acl_rules = load_acl_rules(username, slug)
        pages_by_path = {p.path: p for p in Page.query.filter_by(wiki_id=wiki.id).all()}
        return [
            path
            for path in files
            if not path.startswith(".wikihub/")
            and not path.endswith(".gitkeep")
            and can_read(path, acl_rules, acl_filter_user, (pages_by_path[path].visibility if path in pages_by_path else None))
        ]

    discoverable = {
        page.path
        for page in Page.query.filter_by(wiki_id=wiki.id).all()
        if page.visibility in ("public", "public-view", "public-edit")
    }
    return [
        path
        for path in files
        if not path.startswith(".wikihub/")
        and not path.endswith(".gitkeep")
        and path in discoverable
    ]


def _folder_url(username, slug, folder_path):
    clean = folder_path.strip("/")
    if not clean:
        return f"/@{username}/{slug}"
    return f"/@{username}/{slug}/{url_path_from_page_path(clean, strip_md=False)}/"


def _page_url(username, slug, page_path):
    return f"/@{username}/{slug}/{url_path_from_page_path(page_path, strip_md=True)}"


def _build_sidebar_tree(username, slug, wiki, public=False, current_path=None, acl_filter_user=None):
    root = {"children": {}}
    pages_by_path = {p.path: p for p in Page.query.filter_by(wiki_id=wiki.id).all()}

    for path in sorted(_visible_files(username, slug, wiki, public=public, acl_filter_user=acl_filter_user)):
        parts = path.split("/")
        cursor = root["children"]
        for depth, part in enumerate(parts[:-1]):
            folder_path = "/".join(parts[: depth + 1])
            node = cursor.setdefault(
                ("folder", folder_path),
                {
                    "kind": "folder",
                    "name": part,
                    "path": folder_path,
                    "url": _folder_url(username, slug, folder_path),
                    "active": current_path == folder_path,
                    "children": {},
                },
            )
            cursor = node["children"]

        filename = parts[-1]
        if filename in {"index.md", "README.md"} and len(parts) > 1:
            continue

        page = pages_by_path.get(path)
        cursor[("page", path)] = {
            "kind": "page",
            "name": filename.replace(".md", ""),
            "path": path,
            "url": _page_url(username, slug, path),
            "active": current_path == path,
            "visibility": page.visibility if page else "private",
            "children": {},
        }

    def normalize(children):
        items = list(children.values())
        for item in items:
            if item["kind"] == "folder":
                item["children"] = normalize(item["children"])
                item["active"] = item["active"] or any(child["active"] for child in item["children"])
        return sorted(items, key=lambda item: (item["kind"] != "folder", item["name"].lower()))

    return normalize(root["children"])


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
    use_public = not _is_owner(wiki)
    tree = _build_sidebar_tree(owner.username, wiki.slug, wiki, public=use_public)
    return jsonify(tree)


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
    management_items = _folder_listing(owner.username, wiki.slug, wiki, "", public=False) if _is_owner(wiki) else None
    return render_template(
        "reader.html",
        owner=owner,
        wiki=wiki,
        page=page,
        rendered_html=rendered_html,
        toc=extract_toc(rendered_html),
        backlinks=_get_backlinks(page),
        link_graph=_get_full_graph(wiki),
        full_graph_url=f"/@{owner.username}/{wiki.slug}/graph",
        recently_updated=recently_updated,
        sidebar_items=_sidebar_for_wiki(owner.username, wiki.slug, wiki, public=use_public, current_path=page_path, acl_filter_user=acl_filter_user),
        private_band_warning=not use_public and has_private_bands(content),
        json_ld_author=owner.display_name or owner.username,
        management_items=management_items,
        sibling_wikis=siblings,
    )


@wiki_bp.route("/@<username>/<slug>/<path:page_path>/graph.json")
def page_graph_json(username, slug, page_path):
    """return wikilink graph data for a page as JSON."""
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
    return jsonify(_get_link_graph(page, wiki))


@wiki_bp.route("/@<username>/<slug>/graph.json")
def wiki_graph_json(username, slug):
    """return full wikilink graph for a wiki as JSON."""
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    from flask import jsonify
    return jsonify(_get_full_graph(wiki))


@wiki_bp.route("/@<username>/<slug>/graph")
def wiki_graph(username, slug):
    """full-screen interactive graph view."""
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    return render_template(
        "graph.html",
        owner=owner,
        wiki=wiki,
        graph_data=_get_full_graph(wiki),
    )


@wiki_bp.route("/@<username>/<slug>/tag/<tag_name>")
def wiki_tag_index(username, slug, tag_name):
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    pages = (
        Page.query.filter_by(wiki_id=wiki.id)
        .filter(Page.frontmatter_json["tags"].astext.contains(tag_name))
        .order_by(Page.title.asc())
        .all()
    )
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
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    # always read from authoritative repo — public mirror is linearized to 1 commit
    raw_commits = _git_history(owner.username, wiki.slug, public=False)
    # filter out internal event log commits (noise)
    commits = [c for c in raw_commits if not c["message"].startswith("Log ")]
    return render_template("folder.html", owner=owner, wiki=wiki, items=[], sidebar_items=_sidebar_for_wiki(owner.username, wiki.slug, wiki, public=not _is_owner(wiki)), folder_path="history", rendered_html=None, breadcrumb=[("History", None)], history_commits=commits)


@wiki_bp.route("/@<username>/<slug>/<path:folder_path>/history")
def page_history(username, slug, folder_path):
    raw_folder_path = folder_path
    folder_path = page_path_from_url_path(folder_path)
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    path = raw_folder_path if raw_folder_path.endswith(".md") else f"{raw_folder_path}.md"
    # always read from authoritative repo
    raw_commits = _git_history(owner.username, wiki.slug, public=False, path=path)
    commits = [c for c in raw_commits if not c["message"].startswith("Log ")]
    return render_template("folder.html", owner=owner, wiki=wiki, items=[], sidebar_items=_sidebar_for_wiki(owner.username, wiki.slug, wiki, public=not _is_owner(wiki), current_path=path), folder_path=f"{path} history", rendered_html=None, breadcrumb=[("History", None)], history_commits=commits)


@wiki_bp.route("/@<username>/<slug>/commit/<sha>")
def wiki_commit(username, slug, sha):
    """show diff for a single commit."""
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    is_owner = _is_owner(wiki)

    # try public mirror first, fall back to authoritative repo
    diff_text = _git_diff(owner.username, wiki.slug, sha, public=True)
    use_public = True
    if diff_text is None:
        diff_text = _git_diff(owner.username, wiki.slug, sha, public=False)
        use_public = False
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
        sidebar_items=_sidebar_for_wiki(owner.username, wiki.slug, wiki, public=use_public),
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

    # serve non-markdown files (images, PDFs, etc.) as raw binary
    _MARKDOWN_EXTS = {".md", ".markdown", ".mdown", ".mkd"}
    _BINARY_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".pdf", ".zip", ".tar", ".gz", ".mp3", ".mp4", ".wav", ".ogg", ".webm", ".woff", ".woff2", ".ttf", ".eot", ".csv", ".json", ".xml", ".yaml", ".yml", ".toml"}
    ext = os.path.splitext(page_path)[1].lower()
    if ext and ext in _BINARY_EXTS and not request.path.endswith("/"):
        import mimetypes
        is_owner = _is_owner(wiki)
        acl_rules = load_acl_rules(owner.username, wiki.slug)
        user_name = current_user.username if current_user.is_authenticated else None
        # check ACL: use the file's directory ACL (binary files don't have Page rows)
        file_vis = resolve_visibility(page_path, acl_rules)
        if not is_owner and not can_read(page_path, acl_rules, user_name, file_vis):
            abort(404)
        use_public = _use_public_repo(wiki, acl_rules)
        data = read_file_bytes_from_repo(owner.username, wiki.slug, page_path, public=use_public)
        if data is None and not use_public:
            pass  # already tried authoritative repo
        elif data is None:
            data = read_file_bytes_from_repo(owner.username, wiki.slug, page_path, public=False)
        if data is None:
            abort(404)
        content_type = mimetypes.guess_type(page_path)[0] or "application/octet-stream"
        headers = {"Cache-Control": "public, max-age=3600"}
        if content_type == "application/pdf":
            headers["Content-Disposition"] = f'inline; filename="{os.path.basename(page_path)}"'
        return Response(data, content_type=content_type, headers=headers)

    if request.path.endswith("/"):
        acl_rules = load_acl_rules(owner.username, wiki.slug)
        use_public, acl_filter_user = _repo_access(wiki, acl_rules)
        content_path, content = _folder_index_content(owner.username, wiki.slug, page_path, public=use_public)
        items = _folder_listing(owner.username, wiki.slug, wiki, page_path, public=use_public, acl_filter_user=acl_filter_user)
        if use_public and not content and not items:
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
        return render_template("permission_error.html", owner=owner, wiki=wiki), 404

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
        abort(404)

    wants_markdown = "text/markdown" in request.headers.get("Accept", "")
    html_url_path = url_path_from_page_path(raw_page_path if raw_page_path.endswith(".md") else page_path, strip_md=True)
    md_url_path = url_path_from_page_path(file_path, strip_md=False)

    # .md in URL: browsers get redirected to the clean HTML URL,
    # API clients requesting text/markdown get raw markdown.
    if page_path.endswith(".md") and not wants_markdown:
        return redirect(f"/@{owner.username}/{wiki.slug}/{html_url_path}", code=302)
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
    page_grants = resolve_grants(file_path, acl_rules) if is_owner else []
    user_can_edit = is_owner or can_write(file_path, acl_rules, user_name, page.visibility if page else None)
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
        return render_template("permission_error.html", owner=owner, wiki=wiki), 403

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
    )


@wiki_bp.route("/@<username>/<slug>/new", methods=["GET", "POST"])
def new_page(username, slug):
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    is_owner = _is_owner(wiki)

    if request.method == "POST":
        page_path = request.form.get("path", "").strip()
        if not page_path.endswith(".md"):
            page_path += ".md"
        content = request.form.get("content", "")

        acl_rules = load_acl_rules(owner.username, wiki.slug)
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

    acl_rules = load_acl_rules(owner.username, wiki.slug)
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
    default_vis = resolve_visibility(page_path, acl_rules)
    return render_template(
        "editor.html",
        owner=owner,
        wiki=wiki,
        page_path=page_path,
        content="",
        visibility=default_vis,
        is_owner=_is_owner(wiki),
    )


@wiki_bp.route("/@<username>/<slug>/new-folder", methods=["GET", "POST"])
def new_folder(username, slug):
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    if not _is_owner(wiki):
        return render_template("permission_error.html", owner=owner, wiki=wiki), 403

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
