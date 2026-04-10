import os
import subprocess
from datetime import timezone
from urllib.parse import quote

from flask import Response, abort, redirect, render_template, request, url_for
from flask_login import current_user

from app import db
from app.acl import can_read, can_write, resolve_visibility
from app.content_utils import has_private_bands, parse_markdown_document, set_visibility_in_content
from app.discovery import discoverable_page_for_wiki, visible_wikis_for_owner
from app.git_sync import read_file_from_repo, list_files_in_repo, regenerate_public_mirror, remove_page_from_repo, sync_page_to_repo
from app.models import Page, User, UsernameRedirect, Wiki, Wikilink, utcnow
from app.renderer import extract_toc, render_page
from app.routes import wiki_bp
from app.wiki_ops import load_acl_rules, refresh_wikilinks_for_page, sync_wiki_counters, update_page_metadata


def _recently_updated_pages(wiki, limit=8, public_only=False):
    """get the most recently updated pages for a wiki."""
    query = Page.query.filter_by(wiki_id=wiki.id)
    if public_only:
        query = query.filter(Page.visibility.in_(('public', 'public-edit')))
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
    page_url = f"/@{wiki.owner.username}/{wiki.slug}/{page.path.replace('.md', '')}"
    nodes[page.id] = {"id": page.id, "title": page.title or page.path.replace('.md', ''), "url": page_url, "current": True}

    # outgoing links
    outgoing = Wikilink.query.filter_by(source_page_id=page.id).all()
    for wl in outgoing:
        if wl.target_page_id and wl.target_page_id not in nodes:
            tgt = db.session.get(Page, wl.target_page_id)
            if tgt:
                nodes[tgt.id] = {"id": tgt.id, "title": tgt.title or tgt.path.replace('.md', ''), "url": f"/@{wiki.owner.username}/{wiki.slug}/{tgt.path.replace('.md', '')}", "current": False}
        if wl.target_page_id:
            links.append({"source": page.id, "target": wl.target_page_id})

    # incoming links (backlinks)
    incoming = Wikilink.query.filter_by(target_page_id=page.id).all()
    for wl in incoming:
        if wl.source_page_id not in nodes:
            src = db.session.get(Page, wl.source_page_id)
            if src:
                nodes[src.id] = {"id": src.id, "title": src.title or src.path.replace('.md', ''), "url": f"/@{wiki.owner.username}/{wiki.slug}/{src.path.replace('.md', '')}", "current": False}
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
        url = f"/@{owner.username}/{wiki.slug}/{p.path.replace('.md', '')}"
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
        abort(404)
    return owner, wiki, redirect_row


def _is_owner(wiki):
    return current_user.is_authenticated and current_user.id == wiki.owner_id


def _normalize_folder_path(raw_path):
    clean = (raw_path or "").replace("\\", "/").strip().strip("/")
    clean = clean.removesuffix("/index.md").removesuffix("/index").removesuffix(".md")
    segments = [segment for segment in clean.split("/") if segment]
    if not segments or any(segment in {".", ".."} for segment in segments):
        return None
    return "/".join(segments)


def _visible_files(username, slug, wiki, public=False):
    files = list_files_in_repo(username, slug, public=public)
    if not public:
        return [path for path in files if not path.startswith(".wikihub/") and not path.endswith(".gitkeep")]

    discoverable = {
        page.path
        for page in Page.query.filter_by(wiki_id=wiki.id).all()
        if page.visibility in ("public", "public-edit")
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
    return f"/@{username}/{slug}/{quote(clean, safe='/')}/"


def _page_url(username, slug, page_path):
    return f"/@{username}/{slug}/{quote(page_path.replace('.md', ''), safe='/')}"


def _build_sidebar_tree(username, slug, wiki, public=False, current_path=None):
    root = {"children": {}}

    for path in sorted(_visible_files(username, slug, wiki, public=public)):
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

        cursor[("page", path)] = {
            "kind": "page",
            "name": filename.replace(".md", ""),
            "path": path,
            "url": _page_url(username, slug, path),
            "active": current_path == path,
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


def _folder_listing(username, slug, wiki, folder_path="", public=False):
    prefix = folder_path.strip("/")
    files = _visible_files(username, slug, wiki, public=public)
    pages_by_path = {page.path: page for page in Page.query.filter_by(wiki_id=wiki.id).all()}
    seen = set()
    items = []

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
            if child_path in seen:
                continue
            seen.add(child_path)
            items.append(
                {
                    "kind": "folder",
                    "name": child,
                    "path": child_path,
                    "url": _folder_url(username, slug, child_path),
                    "visibility": resolve_visibility(child_path, load_acl_rules(username, slug)),
                    "updated_at": None,
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

    cmd = [
        "git",
        "-C",
        repo,
        "log",
        "--format=%H%x1f%an%x1f%aI%x1f%s%x1e",
        "--name-only",
        f"--max-count={limit}",
    ]
    if path:
        cmd += ["--", path]

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return []

    commits = []
    for chunk in result.stdout.split("\x1e"):
        chunk = chunk.strip()
        if not chunk:
            continue
        lines = [line for line in chunk.splitlines() if line.strip()]
        sha, author, date, message = lines[0].split("\x1f")
        commits.append(
            {
                "sha": sha,
                "author": author,
                "date": date,
                "message": message,
                "files_changed": lines[1:],
            }
        )
    return commits


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
        personal_excerpt=profile_page.excerpt if profile_page else None,
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
        Page.visibility.in_(["public", "public-edit"])
    ).order_by(Page.path).all()

    for p in pages:
        url = f"/@{owner.username}/{wiki.slug}/{p.path.replace('.md', '')}"
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
        return redirect(url_for("wiki.wiki_zip", username=owner.username, slug=slug), code=302)

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


@wiki_bp.route("/@<username>/<slug>", strict_slashes=False)
def wiki_index(username, slug):
    owner, wiki, redirect_row = _get_owner_and_wiki_or_404(username, slug)
    if redirect_row:
        return redirect(url_for("wiki.wiki_index", username=owner.username, slug=slug), code=302)

    # Personal wiki: redirect to profile page
    if slug == owner.username:
        return redirect(url_for("wiki.user_profile", username=owner.username), code=302)

    use_public = not _is_owner(wiki)
    recently_updated = _recently_updated_pages(wiki, public_only=use_public)
    page_path, content = _folder_index_content(owner.username, wiki.slug, "", public=use_public)
    if content is None:
        return render_template(
            "folder.html",
            owner=owner,
            wiki=wiki,
            items=_folder_listing(owner.username, wiki.slug, wiki, "", public=use_public),
            sidebar_items=_build_sidebar_tree(owner.username, wiki.slug, wiki, public=use_public),
            folder_path="",
            rendered_html=None,
            breadcrumb=[],
            recently_updated=recently_updated,
        )

    page = Page.query.filter_by(wiki_id=wiki.id, path=page_path).first()
    if page is None:
        page = type("Page", (), {"path": page_path, "title": wiki.title, "visibility": "private", "updated_at": wiki.updated_at})()

    rendered_html = render_page(content, owner.username, wiki.slug)
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
        sidebar_items=_build_sidebar_tree(owner.username, wiki.slug, wiki, public=use_public, current_path=page_path),
        private_band_warning=not use_public and has_private_bands(content),
        json_ld_author=owner.display_name or owner.username,
    )


@wiki_bp.route("/@<username>/<slug>/<path:page_path>/graph.json")
def page_graph_json(username, slug, page_path):
    """return wikilink graph data for a page as JSON."""
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    file_path = page_path if page_path.endswith(".md") else page_path + ".md"
    page = Page.query.filter_by(wiki_id=wiki.id, path=file_path).first()
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
    ], sidebar_items=_build_sidebar_tree(owner.username, wiki.slug, wiki, public=not _is_owner(wiki)), folder_path=f"tag:{tag_name}", rendered_html=None, breadcrumb=[("Tags", None), (tag_name, None)])


@wiki_bp.route("/@<username>/<slug>/history")
def wiki_history(username, slug):
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    commits = _git_history(owner.username, wiki.slug, public=not _is_owner(wiki))
    return render_template("folder.html", owner=owner, wiki=wiki, items=[], sidebar_items=_build_sidebar_tree(owner.username, wiki.slug, wiki, public=not _is_owner(wiki)), folder_path="history", rendered_html=None, breadcrumb=[("History", None)], history_commits=commits)


@wiki_bp.route("/@<username>/<slug>/<path:folder_path>/history")
def page_history(username, slug, folder_path):
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    path = folder_path if folder_path.endswith(".md") else f"{folder_path}.md"
    commits = _git_history(owner.username, wiki.slug, public=not _is_owner(wiki), path=path)
    return render_template("folder.html", owner=owner, wiki=wiki, items=[], sidebar_items=_build_sidebar_tree(owner.username, wiki.slug, wiki, public=not _is_owner(wiki), current_path=path), folder_path=f"{path} history", rendered_html=None, breadcrumb=[("History", None)], history_commits=commits)


@wiki_bp.route("/@<username>/<slug>/<path:page_path>", strict_slashes=False)
def wiki_page(username, slug, page_path):
    owner, wiki, redirect_row = _get_owner_and_wiki_or_404(username, slug)
    if redirect_row:
        return redirect(url_for("wiki.wiki_page", username=owner.username, slug=slug, page_path=page_path), code=302)

    if request.path.endswith("/"):
        use_public = not _is_owner(wiki)
        content_path, content = _folder_index_content(owner.username, wiki.slug, page_path, public=use_public)
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
            items=_folder_listing(owner.username, wiki.slug, wiki, page_path, public=use_public),
            sidebar_items=_build_sidebar_tree(owner.username, wiki.slug, wiki, public=use_public, current_path=page_path.strip("/")),
            folder_path=page_path.strip("/"),
            rendered_html=render_page(content, owner.username, wiki.slug) if content else None,
            breadcrumb=breadcrumb,
            private_band_warning=bool(content and not use_public and has_private_bands(content)),
        )

    file_path = page_path if page_path.endswith(".md") else page_path + ".md"
    page = Page.query.filter_by(wiki_id=wiki.id, path=file_path).first()
    acl_rules = load_acl_rules(owner.username, wiki.slug)
    user_name = current_user.username if current_user.is_authenticated else None
    is_owner = _is_owner(wiki)

    if page and not is_owner and not can_read(page.path, acl_rules, user_name, page.visibility):
        return render_template("permission_error.html", owner=owner, wiki=wiki), 403

    use_public = not is_owner and (page.visibility if page else "public") != "private"
    content = read_file_from_repo(owner.username, wiki.slug, file_path, public=use_public)
    if content is None and page and page.visibility == "private" and current_user.is_authenticated:
        content = read_file_from_repo(owner.username, wiki.slug, file_path, public=False)
    if content is None:
        abort(404)

    wants_markdown = "text/markdown" in request.headers.get("Accept", "")
    if wants_markdown or page_path.endswith(".md"):
        return Response(
            content,
            content_type="text/markdown; charset=utf-8",
            headers={
                "Vary": "Accept",
                "Link": f'</@{owner.username}/{wiki.slug}/{quote(page_path.replace(".md", ""), safe="/")}>; rel="alternate"; type="text/html"',
            },
        )

    if not page:
        page = type("Page", (), {"path": file_path, "title": os.path.basename(file_path).replace(".md", ""), "visibility": resolve_visibility(file_path, acl_rules), "updated_at": wiki.updated_at})()

    rendered_html = render_page(content, owner.username, wiki.slug)
    return render_template(
        "reader.html",
        owner=owner,
        wiki=wiki,
        page=page,
        rendered_html=rendered_html,
        toc=extract_toc(rendered_html),
        backlinks=_get_backlinks(page),
        link_graph=_get_link_graph(page, wiki),
        recently_updated=_recently_updated_pages(wiki, public_only=not is_owner),
        sidebar_items=_build_sidebar_tree(owner.username, wiki.slug, wiki, public=use_public, current_path=file_path),
        private_band_warning=not use_public and has_private_bands(content),
        json_ld_author=owner.display_name or owner.username,
    ), 200, {
        "Vary": "Accept",
        "Link": f'</@{owner.username}/{wiki.slug}/{quote(page_path, safe="/")}.md>; rel="alternate"; type="text/markdown"',
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
    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    file_path = page_path if page_path.endswith(".md") else page_path + ".md"
    page = Page.query.filter_by(wiki_id=wiki.id, path=file_path).first()
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
        regenerate_public_mirror(owner.username, wiki.slug, acl_rules)
        db.session.commit()
        return redirect(url_for("wiki.wiki_page", username=owner.username, slug=wiki.slug, page_path=target_path.replace(".md", "")))

    content = read_file_from_repo(owner.username, wiki.slug, file_path, public=False) or ""
    visibility = page.visibility if page else resolve_visibility(file_path, acl_rules)

    return render_template(
        "editor.html",
        owner=owner,
        wiki=wiki,
        page_path=file_path,
        content=content,
        visibility=visibility,
        is_owner=is_owner,
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
        regenerate_public_mirror(owner.username, wiki.slug, acl_rules)
        db.session.commit()
        return redirect(url_for("wiki.wiki_page", username=owner.username, slug=wiki.slug, page_path=page_path.replace(".md", "")))

    acl_rules = load_acl_rules(owner.username, wiki.slug)
    requested_path = request.args.get("path", "").strip()
    if requested_path:
        page_path = requested_path if requested_path.endswith(".md") else requested_path + ".md"
    else:
        base = "wiki/new-page"
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
    parent_path = request.values.get("parent", "").strip().strip("/")

    if request.method == "POST":
        folder_path = _normalize_folder_path(request.form.get("folder_path"))
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
            return redirect(url_for("wiki.edit_page", username=owner.username, slug=wiki.slug, page_path=f"{folder_path}/index"))

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
        regenerate_public_mirror(owner.username, wiki.slug, acl_rules)
        db.session.commit()
        return redirect(url_for("wiki.edit_page", username=owner.username, slug=wiki.slug, page_path=f"{folder_path}/index"))

    default_target = f"{(parent_path + '/' if parent_path else '')}new-folder/index.md"
    return render_template(
        "new_folder.html",
        owner=owner,
        wiki=wiki,
        parent_path=parent_path,
        default_visibility=resolve_visibility(default_target, acl_rules),
        error=None,
    )
