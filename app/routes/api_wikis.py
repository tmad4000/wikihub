"""
wiki + page REST API endpoints.

POST   /api/v1/wikis                              create wiki
GET    /api/v1/wikis/:owner/:slug                  wiki metadata
PATCH  /api/v1/wikis/:owner/:slug                  update wiki
DELETE /api/v1/wikis/:owner/:slug                  delete wiki
POST   /api/v1/wikis/:owner/:slug/pages            create page
GET    /api/v1/wikis/:owner/:slug/pages/*path       read page
PUT    /api/v1/wikis/:owner/:slug/pages/*path       full replace
PATCH  /api/v1/wikis/:owner/:slug/pages/*path       partial update / rename
DELETE /api/v1/wikis/:owner/:slug/pages/*path       delete page
POST   /api/v1/wikis/:owner/:slug/fork              fork
POST   /api/v1/wikis/:owner/:slug/star              star
DELETE /api/v1/wikis/:owner/:slug/star              unstar
GET    /api/v1/search                              search
"""

import hashlib
import os

from flask import request, jsonify

from app import db
from app.models import User, Wiki, Page, Star, Fork, Wikilink
from app.auth_utils import api_auth_required, api_auth_optional
from app.git_backend import init_wiki_repo
from app.git_sync import (
    scaffold_wiki, sync_page_to_repo, remove_page_from_repo,
    read_file_from_repo, list_files_in_repo, regenerate_public_mirror,
)
from app.acl import parse_acl, resolve_visibility, can_read, can_write
from app.routes import api_bp


def _get_wiki_or_404(owner_username, slug):
    owner = User.query.filter_by(username=owner_username).first()
    if not owner:
        return None, None, ({"error": "not_found", "message": "User not found"}, 404)
    wiki = Wiki.query.filter_by(owner_id=owner.id, slug=slug).first()
    if not wiki:
        return None, None, ({"error": "not_found", "message": "Wiki not found"}, 404)
    return owner, wiki, None


def _load_acl_rules(owner_username, slug):
    acl_content = read_file_from_repo(owner_username, slug, ".wikihub/acl")
    if acl_content:
        return parse_acl(acl_content)
    return []


def _extract_frontmatter(content):
    """extract frontmatter dict and body from markdown content."""
    metadata = {}
    body = content
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].strip().split("\n"):
                line = line.strip()
                if ":" in line:
                    key, val = line.split(":", 1)
                    metadata[key.strip().lower()] = val.strip()
            body = parts[2].strip()
    return metadata, body


def _update_page_metadata(page, content, frontmatter=None):
    """update page's derived metadata from content."""
    if frontmatter is None:
        frontmatter, body = _extract_frontmatter(content)
    else:
        _, body = _extract_frontmatter(content)

    page.title = frontmatter.get("title", os.path.splitext(os.path.basename(page.path))[0])
    page.frontmatter_json = frontmatter
    page.content_hash = hashlib.sha256(content.encode()).hexdigest()
    page.excerpt = body[:200].replace("\n", " ").strip() if body else ""

    # update search vector
    search_text = f"{page.title or ''} {body or ''}"
    page.search_vector = db.func.to_tsvector("english", search_text)


# --- wiki endpoints ---

@api_bp.route("/wikis", methods=["POST"])
@api_auth_required
def create_wiki():
    user = request.current_user
    data = request.get_json(silent=True) or {}

    slug = data.get("slug", "").strip().lower()
    if not slug:
        return {"error": "bad_request", "message": "slug is required"}, 400

    slug = "".join(c for c in slug if c.isalnum() or c in "-_")
    if not slug:
        return {"error": "bad_request", "message": "invalid slug"}, 400

    if Wiki.query.filter_by(owner_id=user.id, slug=slug).first():
        return {"error": "conflict", "message": f"Wiki '{slug}' already exists"}, 409

    wiki = Wiki(
        owner_id=user.id,
        slug=slug,
        title=data.get("title", slug),
        description=data.get("description", ""),
    )
    db.session.add(wiki)
    db.session.commit()

    # init git repos + scaffold
    init_wiki_repo(user.username, slug)
    scaffold_wiki(user.username, slug)

    # index scaffold pages
    for fpath in list_files_in_repo(user.username, slug):
        if fpath.endswith(".md"):
            content = read_file_from_repo(user.username, slug, fpath)
            if content:
                page = Page(wiki_id=wiki.id, path=fpath, visibility="private")
                _update_page_metadata(page, content)
                db.session.add(page)
    db.session.commit()

    # regenerate public mirror
    acl_rules = _load_acl_rules(user.username, slug)
    regenerate_public_mirror(user.username, slug, acl_rules)

    return jsonify({
        "id": wiki.id,
        "owner": user.username,
        "slug": wiki.slug,
        "title": wiki.title,
        "clone_url": f"/@{user.username}/{slug}.git",
        "web_url": f"/@{user.username}/{slug}",
    }), 201


@api_bp.route("/wikis/<owner>/<slug>", methods=["GET"])
@api_auth_optional
def get_wiki(owner, slug):
    owner_user, wiki, err = _get_wiki_or_404(owner, slug)
    if err:
        return err

    return jsonify({
        "id": wiki.id,
        "owner": owner,
        "slug": wiki.slug,
        "title": wiki.title,
        "description": wiki.description,
        "star_count": wiki.star_count,
        "fork_count": wiki.fork_count,
        "page_count": wiki.pages.count(),
        "created_at": wiki.created_at.isoformat(),
        "updated_at": wiki.updated_at.isoformat(),
    })


@api_bp.route("/wikis/<owner>/<slug>", methods=["PATCH"])
@api_auth_required
def update_wiki(owner, slug):
    owner_user, wiki, err = _get_wiki_or_404(owner, slug)
    if err:
        return err

    if request.current_user.id != wiki.owner_id:
        return {"error": "forbidden", "message": "Only the owner can update wiki metadata"}, 403

    data = request.get_json(silent=True) or {}
    if "title" in data:
        wiki.title = data["title"]
    if "description" in data:
        wiki.description = data["description"]
    db.session.commit()

    return jsonify({"id": wiki.id, "title": wiki.title, "description": wiki.description})


@api_bp.route("/wikis/<owner>/<slug>", methods=["DELETE"])
@api_auth_required
def delete_wiki(owner, slug):
    owner_user, wiki, err = _get_wiki_or_404(owner, slug)
    if err:
        return err

    if request.current_user.id != wiki.owner_id:
        return {"error": "forbidden", "message": "Only the owner can delete a wiki"}, 403

    db.session.delete(wiki)
    db.session.commit()
    return "", 204


# --- fork / star ---

@api_bp.route("/wikis/<owner>/<slug>/fork", methods=["POST"])
@api_auth_required
def fork_wiki(owner, slug):
    """server-side git clone --bare into caller's namespace."""
    import subprocess
    owner_user, source_wiki, err = _get_wiki_or_404(owner, slug)
    if err:
        return err

    user = request.current_user
    if Wiki.query.filter_by(owner_id=user.id, slug=slug).first():
        return {"error": "conflict", "message": f"You already have a wiki called '{slug}'"}, 409

    # clone the public mirror (or authoritative if owner)
    is_owner = user.id == source_wiki.owner_id
    from app.git_backend import _repo_path, init_wiki_repo
    src_repo = _repo_path(owner, slug, public=not is_owner)
    dst_repo = _repo_path(user.username, slug)

    os.makedirs(os.path.dirname(dst_repo), exist_ok=True)
    subprocess.run(["git", "clone", "--bare", src_repo, dst_repo], check=True, capture_output=True)

    # create wiki record (visibility reset to private per spec)
    forked_wiki = Wiki(
        owner_id=user.id,
        slug=slug,
        title=source_wiki.title,
        description=source_wiki.description,
        forked_from_id=source_wiki.id,
    )
    db.session.add(forked_wiki)

    # create fork record
    fork_record = Fork(
        source_wiki_id=source_wiki.id,
        forked_wiki_id=0,  # placeholder, set after flush
        user_id=user.id,
    )
    db.session.flush()
    fork_record.forked_wiki_id = forked_wiki.id
    db.session.add(fork_record)

    # increment fork count
    source_wiki.fork_count += 1

    # init public mirror for the fork
    pub_repo = _repo_path(user.username, slug, public=True)
    if not os.path.isdir(pub_repo):
        subprocess.run(["git", "init", "--bare", pub_repo], check=True, capture_output=True)
        subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                       cwd=pub_repo, check=True, capture_output=True)

    db.session.commit()

    return jsonify({
        "id": forked_wiki.id,
        "owner": user.username,
        "slug": slug,
        "forked_from": f"{owner}/{slug}",
        "web_url": f"/@{user.username}/{slug}",
    }), 201


@api_bp.route("/wikis/<owner>/<slug>/star", methods=["POST"])
@api_auth_required
def star_wiki(owner, slug):
    owner_user, wiki, err = _get_wiki_or_404(owner, slug)
    if err:
        return err

    user = request.current_user
    existing = Star.query.filter_by(user_id=user.id, wiki_id=wiki.id).first()
    if existing:
        return {"error": "conflict", "message": "Already starred"}, 409

    star = Star(user_id=user.id, wiki_id=wiki.id)
    db.session.add(star)
    wiki.star_count += 1
    db.session.commit()

    return jsonify({"starred": True, "star_count": wiki.star_count}), 201


@api_bp.route("/wikis/<owner>/<slug>/star", methods=["DELETE"])
@api_auth_required
def unstar_wiki(owner, slug):
    owner_user, wiki, err = _get_wiki_or_404(owner, slug)
    if err:
        return err

    user = request.current_user
    star = Star.query.filter_by(user_id=user.id, wiki_id=wiki.id).first()
    if not star:
        return {"error": "not_found", "message": "Not starred"}, 404

    db.session.delete(star)
    wiki.star_count = max(0, wiki.star_count - 1)
    db.session.commit()

    return jsonify({"starred": False, "star_count": wiki.star_count})


# --- page endpoints ---

@api_bp.route("/wikis/<owner>/<slug>/pages", methods=["POST"])
@api_auth_required
def create_page(owner, slug):
    owner_user, wiki, err = _get_wiki_or_404(owner, slug)
    if err:
        return err

    data = request.get_json(silent=True) or {}
    path = data.get("path", "").strip()
    content = data.get("content", "")
    visibility = data.get("visibility")

    if not path:
        return {"error": "bad_request", "message": "path is required"}, 400

    if Page.query.filter_by(wiki_id=wiki.id, path=path).first():
        return {"error": "conflict", "message": f"Page '{path}' already exists"}, 409

    # check write permission
    user = request.current_user
    is_owner = user.id == wiki.owner_id
    if not is_owner:
        acl_rules = _load_acl_rules(owner, slug)
        if not can_write(path, acl_rules, user.username):
            return {"error": "forbidden", "message": "You need edit access to this page"}, 403

    # resolve visibility from ACL if not explicitly set
    if not visibility:
        acl_rules = _load_acl_rules(owner, slug)
        visibility = resolve_visibility(path, acl_rules)

    page = Page(
        wiki_id=wiki.id,
        path=path,
        visibility=visibility,
        author=user.username,
    )
    _update_page_metadata(page, content)

    # private pages: content in DB only, never git
    if visibility == "private":
        page.private_content = content
    else:
        sync_page_to_repo(owner, slug, path, content)

    db.session.add(page)
    db.session.commit()

    # regenerate public mirror
    acl_rules = _load_acl_rules(owner, slug)
    regenerate_public_mirror(owner, slug, acl_rules)

    return jsonify({
        "id": page.id,
        "path": page.path,
        "title": page.title,
        "visibility": page.visibility,
        "url": f"/@{owner}/{slug}/{path.replace('.md', '')}",
    }), 201


@api_bp.route("/wikis/<owner>/<slug>/pages/<path:page_path>", methods=["GET"])
@api_auth_optional
def read_page(owner, slug, page_path):
    owner_user, wiki, err = _get_wiki_or_404(owner, slug)
    if err:
        return err

    # try with and without .md extension
    page = Page.query.filter_by(wiki_id=wiki.id, path=page_path).first()
    if not page and not page_path.endswith(".md"):
        page = Page.query.filter_by(wiki_id=wiki.id, path=page_path + ".md").first()

    if not page:
        return {"error": "not_found", "message": "Page not found"}, 404

    # check read permission
    user = getattr(request, "current_user", None)
    is_owner = user and user.id == wiki.owner_id
    if not is_owner:
        acl_rules = _load_acl_rules(owner, slug)
        if not can_read(page.path, acl_rules, user.username if user else None, page.visibility):
            return {"error": "forbidden", "message": "You don't have access to this page"}, 403

    # read content
    if page.visibility == "private" and page.private_content:
        content = page.private_content
    else:
        content = read_file_from_repo(owner, slug, page.path, public=not is_owner)

    return jsonify({
        "id": page.id,
        "path": page.path,
        "title": page.title,
        "visibility": page.visibility,
        "content": content,
        "excerpt": page.excerpt,
        "frontmatter": page.frontmatter_json,
        "updated_at": page.updated_at.isoformat(),
    })


@api_bp.route("/wikis/<owner>/<slug>/pages/<path:page_path>", methods=["PUT"])
@api_auth_required
def replace_page(owner, slug, page_path):
    owner_user, wiki, err = _get_wiki_or_404(owner, slug)
    if err:
        return err

    page = Page.query.filter_by(wiki_id=wiki.id, path=page_path).first()
    if not page:
        return {"error": "not_found", "message": "Page not found"}, 404

    user = request.current_user
    is_owner = user.id == wiki.owner_id
    if not is_owner:
        acl_rules = _load_acl_rules(owner, slug)
        if not can_write(page.path, acl_rules, user.username):
            return {"error": "forbidden", "message": "You need edit access to this page"}, 403

    data = request.get_json(silent=True) or {}
    content = data.get("content", "")
    new_visibility = data.get("visibility")

    if new_visibility:
        page.visibility = new_visibility

    _update_page_metadata(page, content)
    page.author = user.username

    if page.visibility == "private":
        page.private_content = content
    else:
        page.private_content = None
        sync_page_to_repo(owner, slug, page.path, content)

    db.session.commit()

    acl_rules = _load_acl_rules(owner, slug)
    regenerate_public_mirror(owner, slug, acl_rules)

    return jsonify({"id": page.id, "path": page.path, "title": page.title, "visibility": page.visibility})


@api_bp.route("/wikis/<owner>/<slug>/pages/<path:page_path>", methods=["PATCH"])
@api_auth_required
def patch_page(owner, slug, page_path):
    owner_user, wiki, err = _get_wiki_or_404(owner, slug)
    if err:
        return err

    page = Page.query.filter_by(wiki_id=wiki.id, path=page_path).first()
    if not page:
        return {"error": "not_found", "message": "Page not found"}, 404

    user = request.current_user
    is_owner = user.id == wiki.owner_id
    if not is_owner:
        acl_rules = _load_acl_rules(owner, slug)
        if not can_write(page.path, acl_rules, user.username):
            return {"error": "forbidden", "message": "You need edit access to this page"}, 403

    data = request.get_json(silent=True) or {}

    # handle rename/move
    new_path = data.get("new_path")
    if new_path:
        if Page.query.filter_by(wiki_id=wiki.id, path=new_path).first():
            return {"error": "conflict", "message": f"Path '{new_path}' already exists"}, 409

        old_path = page.path
        # read current content
        if page.private_content:
            content = page.private_content
        else:
            content = read_file_from_repo(owner, slug, old_path) or ""

        # remove old, write new
        remove_page_from_repo(owner, slug, old_path)
        page.path = new_path
        if page.visibility != "private":
            sync_page_to_repo(owner, slug, new_path, content)

    # update content if provided
    if "content" in data:
        content = data["content"]
        _update_page_metadata(page, content)
        page.author = user.username
        if page.visibility == "private":
            page.private_content = content
        else:
            page.private_content = None
            sync_page_to_repo(owner, slug, page.path, content)

    if "visibility" in data:
        page.visibility = data["visibility"]

    db.session.commit()

    acl_rules = _load_acl_rules(owner, slug)
    regenerate_public_mirror(owner, slug, acl_rules)

    return jsonify({"id": page.id, "path": page.path, "title": page.title, "visibility": page.visibility})


@api_bp.route("/wikis/<owner>/<slug>/pages/<path:page_path>", methods=["DELETE"])
@api_auth_required
def delete_page(owner, slug, page_path):
    owner_user, wiki, err = _get_wiki_or_404(owner, slug)
    if err:
        return err

    page = Page.query.filter_by(wiki_id=wiki.id, path=page_path).first()
    if not page:
        return {"error": "not_found", "message": "Page not found"}, 404

    if request.current_user.id != wiki.owner_id:
        return {"error": "forbidden", "message": "Only the owner can delete pages"}, 403

    remove_page_from_repo(owner, slug, page.path)
    db.session.delete(page)
    db.session.commit()

    acl_rules = _load_acl_rules(owner, slug)
    regenerate_public_mirror(owner, slug, acl_rules)

    return "", 204


# --- search ---

@api_bp.route("/search", methods=["GET"])
@api_auth_optional
def search_pages():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": [], "total": 0})

    scope = request.args.get("scope", "global")
    wiki_param = request.args.get("wiki")
    tag = request.args.get("tag")
    limit = min(int(request.args.get("limit", 20)), 100)
    offset = int(request.args.get("offset", 0))

    query = Page.query.join(Wiki).join(User, Wiki.owner_id == User.id)

    # scope to specific wiki
    if scope == "wiki" and wiki_param:
        parts = wiki_param.split("/", 1)
        if len(parts) == 2:
            query = query.filter(User.username == parts[0], Wiki.slug == parts[1])

    # only show pages the user can see
    user = getattr(request, "current_user", None)
    if user:
        query = query.filter(
            db.or_(
                Page.visibility.in_(["public", "public-edit", "unlisted", "unlisted-edit"]),
                Wiki.owner_id == user.id,
            )
        )
    else:
        query = query.filter(Page.visibility.in_(["public", "public-edit"]))

    # full-text search
    query = query.filter(
        Page.search_vector.op("@@")(db.func.plainto_tsquery("english", q))
    )

    # tag filter
    if tag:
        query = query.filter(Page.frontmatter_json["tags"].astext.contains(tag))

    total = query.count()
    results = query.order_by(
        db.func.ts_rank(Page.search_vector, db.func.plainto_tsquery("english", q)).desc()
    ).offset(offset).limit(limit).all()

    return jsonify({
        "results": [{
            "wiki": f"{r.wiki.owner.username}/{r.wiki.slug}",
            "page": r.path,
            "title": r.title,
            "excerpt": r.excerpt,
            "visibility": r.visibility,
            "tags": (r.frontmatter_json or {}).get("tags", []),
        } for r in results],
        "total": total,
    })


# --- admin endpoints (called by post-receive hook) ---

@api_bp.route("/admin/sync-page", methods=["POST"])
def admin_sync_page():
    """internal endpoint for post-receive hook to upsert page metadata."""
    auth = request.headers.get("Authorization", "")
    from flask import current_app
    expected = current_app.config.get("ADMIN_TOKEN", "")
    if not expected or auth != f"Bearer {expected}":
        return {"error": "unauthorized"}, 401

    data = request.get_json(silent=True) or {}
    username = data["username"]
    slug = data["slug"]
    path = data["path"]

    owner = User.query.filter_by(username=username).first()
    if not owner:
        return {"error": "not_found", "message": "User not found"}, 404
    wiki = Wiki.query.filter_by(owner_id=owner.id, slug=slug).first()
    if not wiki:
        return {"error": "not_found", "message": "Wiki not found"}, 404

    page = Page.query.filter_by(wiki_id=wiki.id, path=path).first()
    if not page:
        page = Page(wiki_id=wiki.id, path=path)
        db.session.add(page)

    content = data.get("content", "")
    frontmatter = data.get("frontmatter", {})
    page.visibility = data.get("visibility", "private")
    _update_page_metadata(page, content, frontmatter)
    db.session.commit()

    return jsonify({"id": page.id, "path": page.path}), 200


@api_bp.route("/admin/delete-page", methods=["POST"])
def admin_delete_page():
    auth = request.headers.get("Authorization", "")
    from flask import current_app
    expected = current_app.config.get("ADMIN_TOKEN", "")
    if not expected or auth != f"Bearer {expected}":
        return {"error": "unauthorized"}, 401

    data = request.get_json(silent=True) or {}
    username = data["username"]
    slug = data["slug"]
    path = data["path"]

    owner = User.query.filter_by(username=username).first()
    if not owner:
        return "", 204
    wiki = Wiki.query.filter_by(owner_id=owner.id, slug=slug).first()
    if not wiki:
        return "", 204
    page = Page.query.filter_by(wiki_id=wiki.id, path=path).first()
    if page:
        db.session.delete(page)
        db.session.commit()
    return "", 204


@api_bp.route("/admin/regenerate-mirror", methods=["POST"])
def admin_regenerate_mirror():
    auth = request.headers.get("Authorization", "")
    from flask import current_app
    expected = current_app.config.get("ADMIN_TOKEN", "")
    if not expected or auth != f"Bearer {expected}":
        return {"error": "unauthorized"}, 401

    data = request.get_json(silent=True) or {}
    username = data["username"]
    slug = data["slug"]

    acl_rules = _load_acl_rules(username, slug)
    regenerate_public_mirror(username, slug, acl_rules)
    return jsonify({"status": "ok"})
