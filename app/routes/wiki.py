import os

from flask import render_template, abort, request, redirect, url_for, flash, Response
from flask_login import current_user, login_required

from app import db
from app.models import User, Wiki, Page
from app.git_sync import (
    read_file_from_repo, list_files_in_repo,
    sync_page_to_repo, regenerate_public_mirror,
)
from app.acl import parse_acl, can_read, can_write, resolve_visibility
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
    total_stars = sum(w.star_count for w in wikis)
    return render_template("profile.html", owner=owner, wikis=wikis, total_stars=total_stars)


@wiki_bp.route("/@<username>/<slug>.zip")
def wiki_zip(username, slug):
    """ZIP download — git archive. owner gets full repo, others get public mirror."""
    import subprocess
    owner = User.query.filter_by(username=username).first_or_404()
    wiki = Wiki.query.filter_by(owner_id=owner.id, slug=slug).first_or_404()

    is_owner = current_user.is_authenticated and current_user.id == wiki.owner_id
    from app.git_backend import _repo_path
    repo = _repo_path(username, slug, public=not is_owner)

    if not os.path.isdir(repo):
        abort(404)

    proc = subprocess.run(
        ["git", "archive", "--format=zip", "HEAD"],
        cwd=repo, capture_output=True,
    )
    if proc.returncode != 0:
        abort(500)

    return Response(
        proc.stdout,
        content_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{slug}.zip"'},
    )


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


@wiki_bp.route("/@<username>/<slug>/<path:page_path>/edit", methods=["GET", "POST"])
@login_required
def edit_page(username, slug, page_path):
    owner = User.query.filter_by(username=username).first_or_404()
    wiki = Wiki.query.filter_by(owner_id=owner.id, slug=slug).first_or_404()

    file_path = page_path if page_path.endswith(".md") else page_path + ".md"
    page = Page.query.filter_by(wiki_id=wiki.id, path=file_path).first()
    is_owner = current_user.id == wiki.owner_id

    acl_rules = _load_acl_rules(username, slug)
    if not is_owner and not can_write(file_path, acl_rules, current_user.username):
        return render_template("permission_error.html", owner=owner, wiki=wiki), 403

    if request.method == "POST":
        content = request.form.get("content", "")
        visibility = request.form.get("visibility", "private")

        if not page:
            page = Page(wiki_id=wiki.id, path=file_path, author=current_user.username)
            db.session.add(page)

        page.visibility = visibility
        page.author = current_user.username

        # extract frontmatter for metadata
        import hashlib, os
        fm = {}
        body = content
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                for line in parts[1].strip().split("\n"):
                    if ":" in line:
                        k, v = line.split(":", 1)
                        fm[k.strip().lower()] = v.strip()
                body = parts[2].strip()

        page.title = fm.get("title", os.path.splitext(os.path.basename(file_path))[0])
        page.frontmatter_json = fm
        page.content_hash = hashlib.sha256(content.encode()).hexdigest()
        page.excerpt = body[:200].replace("\n", " ").strip()
        page.search_vector = db.func.to_tsvector("english", f"{page.title or ''} {body}")

        if visibility == "private":
            page.private_content = content
        else:
            page.private_content = None
            sync_page_to_repo(username, slug, file_path, content)

        db.session.commit()
        regenerate_public_mirror(username, slug, acl_rules)

        return redirect(url_for("wiki.wiki_page", username=username, slug=slug, page_path=page_path))

    # GET: load existing content
    if page and page.private_content:
        content = page.private_content
    else:
        content = read_file_from_repo(username, slug, file_path) or ""

    visibility = page.visibility if page else resolve_visibility(file_path, acl_rules)

    return render_template("editor.html",
        owner=owner, wiki=wiki, page_path=file_path,
        content=content, visibility=visibility)


@wiki_bp.route("/@<username>/<slug>/new", methods=["GET", "POST"])
@login_required
def new_page(username, slug):
    owner = User.query.filter_by(username=username).first_or_404()
    wiki = Wiki.query.filter_by(owner_id=owner.id, slug=slug).first_or_404()

    if request.method == "POST":
        page_path = request.form.get("path", "").strip()
        if not page_path.endswith(".md"):
            page_path += ".md"
        # redirect to edit with the new path
        return redirect(url_for("wiki.edit_page",
            username=username, slug=slug, page_path=page_path.replace(".md", "")))

    acl_rules = _load_acl_rules(username, slug)
    default_vis = resolve_visibility("wiki/new-page.md", acl_rules)

    return render_template("editor.html",
        owner=owner, wiki=wiki, page_path="wiki/new-page.md",
        content="", visibility=default_vis)
