from flask import render_template, abort, request
from flask_login import current_user

from app.models import User, Wiki, Page
from app.git_sync import read_file_from_repo, list_files_in_repo
from app.acl import parse_acl, can_read, resolve_visibility
from app.renderer import render_page
from app.routes import wiki_bp


def _load_acl_rules(username, slug):
    acl_content = read_file_from_repo(username, slug, ".wikihub/acl")
    if acl_content:
        return parse_acl(acl_content)
    return []


def _build_sidebar(username, slug, current_path=None):
    """build sidebar items from the repo's file tree."""
    files = list_files_in_repo(username, slug)
    items = []
    for f in sorted(files):
        if f.startswith(".wikihub/") or f.endswith(".gitkeep"):
            continue
        depth = f.count("/")
        label = f.rsplit("/", 1)[-1].replace(".md", "")
        url = f"/@{username}/{slug}/{f.replace('.md', '')}"
        items.append({
            "url": url,
            "label": label,
            "active": f == current_path,
            "indent_class": f"indent-{depth}" if depth > 0 else "",
        })
    return items


@wiki_bp.route("/@<username>")
def user_profile(username):
    owner = User.query.filter_by(username=username).first()
    if not owner:
        abort(404)

    wikis = Wiki.query.filter_by(owner_id=owner.id).order_by(Wiki.updated_at.desc()).all()
    return render_template("profile.html", owner=owner, wikis=wikis)


@wiki_bp.route("/@<username>/<slug>")
def wiki_index(username, slug):
    owner = User.query.filter_by(username=username).first()
    if not owner:
        abort(404)
    wiki = Wiki.query.filter_by(owner_id=owner.id, slug=slug).first()
    if not wiki:
        abort(404)

    # try to render index.md or README.md
    for index_path in ("index.md", "README.md"):
        content = read_file_from_repo(username, slug, index_path)
        if content:
            page = Page.query.filter_by(wiki_id=wiki.id, path=index_path).first()
            if not page:
                page = type("Page", (), {"path": index_path, "title": wiki.title, "visibility": "public"})()

            rendered = render_page(content, username, slug)
            sidebar_items = _build_sidebar(username, slug, index_path)
            return render_template("reader.html",
                owner=owner, wiki=wiki, page=page,
                rendered_html=rendered, sidebar_items=sidebar_items)

    # no index — show file listing
    sidebar_items = _build_sidebar(username, slug)
    return render_template("folder.html", owner=owner, wiki=wiki, items=sidebar_items)


@wiki_bp.route("/@<username>/<slug>/<path:page_path>")
def wiki_page(username, slug, page_path):
    owner = User.query.filter_by(username=username).first()
    if not owner:
        abort(404)
    wiki = Wiki.query.filter_by(owner_id=owner.id, slug=slug).first()
    if not wiki:
        abort(404)

    # content negotiation: Accept: text/markdown -> raw markdown
    accept = request.headers.get("Accept", "")
    wants_markdown = "text/markdown" in accept

    # find the page (try with .md extension)
    file_path = page_path
    if not file_path.endswith(".md"):
        file_path = file_path + ".md"

    page = Page.query.filter_by(wiki_id=wiki.id, path=file_path).first()
    is_owner = current_user.is_authenticated and current_user.id == wiki.owner_id

    # check permissions
    acl_rules = _load_acl_rules(username, slug)
    user_name = current_user.username if current_user.is_authenticated else None

    if page and not is_owner:
        if not can_read(page.path, acl_rules, user_name, page.visibility):
            return render_template("permission_error.html", owner=owner, wiki=wiki), 403

    # read content
    if page and page.visibility == "private" and page.private_content:
        content = page.private_content
    else:
        content = read_file_from_repo(username, slug, file_path, public=not is_owner)

    if not content:
        abort(404)

    # raw markdown response
    if wants_markdown or page_path.endswith(".md"):
        from flask import Response
        return Response(
            content,
            content_type="text/markdown; charset=utf-8",
            headers={
                "Vary": "Accept",
                "Link": f'</@{username}/{slug}/{page_path.replace(".md", "")}>; rel="alternate"; type="text/html"',
            },
        )

    if not page:
        page = type("Page", (), {
            "path": file_path, "title": file_path.replace(".md", "").rsplit("/", 1)[-1],
            "visibility": resolve_visibility(file_path, acl_rules),
        })()

    rendered = render_page(content, username, slug)
    sidebar_items = _build_sidebar(username, slug, file_path)

    return render_template("reader.html",
        owner=owner, wiki=wiki, page=page,
        rendered_html=rendered, sidebar_items=sidebar_items,
    ), 200, {
        "Vary": "Accept",
        "Link": f'</@{username}/{slug}/{page_path}.md>; rel="alternate"; type="text/markdown"',
    }
