"""
wiki + page REST API endpoints.
"""

import json
import os
import subprocess
from urllib.parse import quote

from flask import Response, current_app, jsonify, request

from app import db
from app.models import User, Wiki, Page, Star, Fork, Wikilink, utcnow
from app.auth_utils import api_auth_optional, api_auth_required
from app.git_backend import init_wiki_repo
from app.git_sync import (
    apply_repo_changes,
    append_event_to_repo,
    list_files_in_repo,
    read_file_from_repo,
    regenerate_public_mirror,
    remove_page_from_repo,
    sync_page_to_repo,
)
from app.acl import can_read, can_write, resolve_visibility
from app.content_utils import (
    page_reference_aliases,
    parse_markdown_document,
    rewrite_wikilinks,
    set_visibility_in_content,
)
from app.routes import api_bp
from app.wiki_ops import (
    create_wiki_for_user,
    delete_wiki_repos,
    index_repo_pages,
    load_acl_rules,
    refresh_wikilinks_for_page,
    sync_wiki_counters,
    update_page_metadata,
)


def _resolve_owner_username(username):
    owner = User.query.filter_by(username=username).first()
    if owner:
        return owner, None
    redirect = None
    try:
        from app.models import UsernameRedirect
    except Exception:
        UsernameRedirect = None
    if UsernameRedirect:
        redirect = UsernameRedirect.query.filter_by(old_username=username).first()
    if redirect and redirect.expires_at > utcnow():
        return User.query.get(redirect.user_id), redirect
    return None, None


def _get_wiki_or_404(owner_username, slug):
    owner, redirect = _resolve_owner_username(owner_username)
    if not owner:
        return None, None, ({"error": "not_found", "message": "User not found"}, 404)
    wiki = Wiki.query.filter_by(owner_id=owner.id, slug=slug).first()
    if not wiki:
        return None, None, ({"error": "not_found", "message": "Wiki not found"}, 404)
    return owner, wiki, None


def _current_author():
    user = getattr(request, "current_user", None)
    if user:
        return user.username, f"{user.username}@wikihub"
    return "anonymous", "anon@wikihub"


def _current_username():
    user = getattr(request, "current_user", None)
    return user.username if user else None


def _load_page_content(owner, slug, page_path, public=False):
    return read_file_from_repo(owner, slug, page_path, public=public)


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

    wiki_count = Wiki.query.filter_by(owner_id=user.id).count()
    if wiki_count >= current_app.config["MAX_WIKIS_PER_USER"]:
        return {"error": "too_many", "message": f"You've reached the limit of {current_app.config['MAX_WIKIS_PER_USER']} wikis"}, 429

    wiki = create_wiki_for_user(
        user,
        slug=slug,
        title=data.get("title", slug),
        description=data.get("description", ""),
        scaffold=True,
    )
    db.session.commit()
    append_event_to_repo(user.username, wiki.slug, "wiki.create", actor=user.username)

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
    sync_wiki_counters(wiki)

    return jsonify({
        "id": wiki.id,
        "owner": owner_user.username,
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
    if wiki.slug == owner_user.username:
        return {"error": "forbidden", "message": "Personal wikis cannot be deleted"}, 403

    db.session.delete(wiki)
    db.session.commit()
    delete_wiki_repos(owner_user.username, wiki.slug)
    return "", 204


# --- fork / star ---

@api_bp.route("/wikis/<owner>/<slug>/fork", methods=["POST"])
@api_auth_required
def fork_wiki(owner, slug):
    """server-side git clone --bare into caller's namespace."""
    owner_user, source_wiki, err = _get_wiki_or_404(owner, slug)
    if err:
        return err

    user = request.current_user
    if Wiki.query.filter_by(owner_id=user.id, slug=slug).first():
        return {"error": "conflict", "message": f"You already have a wiki called '{slug}'"}, 409

    # clone the public mirror (or authoritative if owner)
    is_owner = user.id == source_wiki.owner_id
    from app.git_backend import _repo_path
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

    # init public mirror for the fork
    pub_repo = _repo_path(user.username, slug, public=True)
    if not os.path.isdir(pub_repo):
        subprocess.run(["git", "init", "--bare", pub_repo], check=True, capture_output=True)
        subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                       cwd=pub_repo, check=True, capture_output=True)

    sync_page_to_repo(user.username, slug, ".wikihub/acl", "* private\n", message="Reset ACL to private after fork")
    index_repo_pages(user.username, slug, forked_wiki, reset=True)
    regenerate_public_mirror(user.username, slug, load_acl_rules(user.username, slug))
    append_event_to_repo(user.username, slug, "wiki.fork", actor=user.username, forked_from=f"{owner_user.username}/{source_wiki.slug}")
    sync_wiki_counters(source_wiki)
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
    db.session.flush()
    sync_wiki_counters(wiki)
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
    db.session.flush()
    sync_wiki_counters(wiki)
    db.session.commit()

    return jsonify({"starred": False, "star_count": wiki.star_count})


# --- page endpoints ---

@api_bp.route("/wikis/<owner>/<slug>/pages", methods=["POST"])
@api_auth_optional
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

    max_page = current_app.config["MAX_PAGE_SIZE"]
    if len(content.encode("utf-8")) > max_page:
        return {"error": "too_large", "message": f"Page content exceeds {max_page // (1024*1024)}MB limit"}, 413

    if Page.query.filter_by(wiki_id=wiki.id, path=path).first():
        return {"error": "conflict", "message": f"Page '{path}' already exists"}, 409

    acl_rules = load_acl_rules(owner_user.username, wiki.slug)
    user = getattr(request, "current_user", None)
    is_owner = bool(user and user.id == wiki.owner_id)
    if not is_owner:
        inherited_visibility = resolve_visibility(path, acl_rules)
        if not can_write(path, acl_rules, _current_username(), inherited_visibility):
            return {"error": "forbidden", "message": "You need edit access to this page"}, 403

    if is_owner and visibility:
        content = set_visibility_in_content(content, visibility)

    frontmatter, _ = parse_markdown_document(content)
    visibility = frontmatter.get("visibility") or resolve_visibility(path, acl_rules)
    if not is_owner and visibility != resolve_visibility(path, acl_rules):
        visibility = resolve_visibility(path, acl_rules)
        content = set_visibility_in_content(content, visibility)

    page = Page(
        wiki_id=wiki.id,
        path=path,
        visibility=visibility,
        author=_current_username(),
    )
    update_page_metadata(page, content, frontmatter)
    db.session.add(page)
    db.session.flush()
    refresh_wikilinks_for_page(page, content)
    author_name, author_email = _current_author()
    sync_page_to_repo(owner_user.username, wiki.slug, path, content, message=f"Create {path}", author_name=author_name, author_email=author_email)
    append_event_to_repo(owner_user.username, wiki.slug, "page.create", path=path, visibility=page.visibility, actor=_current_username())
    regenerate_public_mirror(owner_user.username, wiki.slug, acl_rules)
    db.session.commit()

    return jsonify({
        "id": page.id,
        "path": page.path,
        "title": page.title,
        "visibility": page.visibility,
        "url": f"/@{owner_user.username}/{wiki.slug}/{quote(path.replace('.md', ''), safe='/')}",
    }), 201


@api_bp.route("/wikis/<owner>/<slug>/pages", methods=["GET"])
@api_auth_optional
def list_pages(owner, slug):
    owner_user, wiki, err = _get_wiki_or_404(owner, slug)
    if err:
        return err

    acl_rules = load_acl_rules(owner_user.username, wiki.slug)
    user = getattr(request, "current_user", None)
    username = user.username if user else None
    is_owner = bool(user and user.id == wiki.owner_id)

    pages = []
    for page in Page.query.filter_by(wiki_id=wiki.id).order_by(Page.path.asc()).all():
        if is_owner or can_read(page.path, acl_rules, username, page.visibility):
            pages.append({
                "path": page.path,
                "title": page.title,
                "visibility": page.visibility,
                "updated_at": page.updated_at.isoformat(),
            })
    return jsonify({"pages": pages, "total": len(pages)})


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
    is_owner = bool(user and user.id == wiki.owner_id)
    acl_rules = load_acl_rules(owner_user.username, wiki.slug)
    if not is_owner:
        if not can_read(page.path, acl_rules, user.username if user else None, page.visibility):
            return {"error": "forbidden", "message": "You don't have access to this page"}, 403

    use_public_repo = not is_owner and page.visibility != "private"
    content = read_file_from_repo(owner_user.username, wiki.slug, page.path, public=use_public_repo)
    if content is None and page.visibility == "private" and user:
        content = read_file_from_repo(owner_user.username, wiki.slug, page.path, public=False)

    etag = f'"{page.content_hash}"' if page.content_hash else None

    wants_markdown = "text/markdown" in request.headers.get("Accept", "")
    if wants_markdown:
        resp = Response(
            content or "",
            content_type="text/markdown; charset=utf-8",
            headers={"Vary": "Accept"},
        )
        if etag:
            resp.headers["ETag"] = etag
        return resp

    resp = jsonify({
        "id": page.id,
        "path": page.path,
        "title": page.title,
        "visibility": page.visibility,
        "content": content,
        "excerpt": page.excerpt,
        "frontmatter": page.frontmatter_json,
        "content_hash": page.content_hash,
        "updated_at": page.updated_at.isoformat(),
    })
    if etag:
        resp.headers["ETag"] = etag
    return resp


@api_bp.route("/wikis/<owner>/<slug>/pages/<path:page_path>", methods=["PUT"])
@api_auth_optional
def replace_page(owner, slug, page_path):
    owner_user, wiki, err = _get_wiki_or_404(owner, slug)
    if err:
        return err

    page = Page.query.filter_by(wiki_id=wiki.id, path=page_path).first()
    if not page:
        return {"error": "not_found", "message": "Page not found"}, 404

    user = getattr(request, "current_user", None)
    is_owner = bool(user and user.id == wiki.owner_id)
    acl_rules = load_acl_rules(owner_user.username, wiki.slug)
    if not is_owner:
        if not can_write(page.path, acl_rules, _current_username(), page.visibility):
            return {"error": "forbidden", "message": "You need edit access to this page"}, 403

    # optimistic locking: reject if client's ETag doesn't match current content_hash
    if_match = request.headers.get("If-Match")
    if if_match:
        expected = if_match.strip().strip('"')
        if expected != page.content_hash:
            return {"error": "conflict", "message": "Page was modified since you last read it (ETag mismatch). Re-fetch and retry."}, 409

    data = request.get_json(silent=True) or {}
    content = data.get("content", "")
    new_visibility = data.get("visibility")

    if new_visibility and not is_owner:
        return {"error": "forbidden", "message": "Only the owner can change visibility"}, 403
    if is_owner and new_visibility:
        content = set_visibility_in_content(content, new_visibility)
    elif not is_owner:
        content = set_visibility_in_content(content, page.visibility)

    frontmatter, _ = parse_markdown_document(content)
    page.visibility = frontmatter.get("visibility") or new_visibility or page.visibility or resolve_visibility(page.path, acl_rules)
    update_page_metadata(page, content, frontmatter)
    page.author = _current_username()
    refresh_wikilinks_for_page(page, content)
    author_name, author_email = _current_author()
    sync_page_to_repo(owner_user.username, wiki.slug, page.path, content, message=f"Update {page.path}", author_name=author_name, author_email=author_email)
    regenerate_public_mirror(owner_user.username, wiki.slug, acl_rules)
    db.session.commit()

    return jsonify({"id": page.id, "path": page.path, "title": page.title, "visibility": page.visibility})


@api_bp.route("/wikis/<owner>/<slug>/pages/<path:page_path>", methods=["PATCH"])
@api_auth_optional
def patch_page(owner, slug, page_path):
    owner_user, wiki, err = _get_wiki_or_404(owner, slug)
    if err:
        return err

    page = Page.query.filter_by(wiki_id=wiki.id, path=page_path).first()
    if not page:
        return {"error": "not_found", "message": "Page not found"}, 404

    user = getattr(request, "current_user", None)
    is_owner = bool(user and user.id == wiki.owner_id)
    acl_rules = load_acl_rules(owner_user.username, wiki.slug)
    if not is_owner:
        if not can_write(page.path, acl_rules, _current_username(), page.visibility):
            return {"error": "forbidden", "message": "You need edit access to this page"}, 403

    # optimistic locking: reject if client's ETag doesn't match current content_hash
    if_match = request.headers.get("If-Match")
    if if_match:
        expected = if_match.strip().strip('"')
        if expected != page.content_hash:
            return {"error": "conflict", "message": "Page was modified since you last read it (ETag mismatch). Re-fetch and retry."}, 409

    data = request.get_json(silent=True) or {}
    new_path = data.get("new_path")
    content = data.get("content")
    append_section = data.get("append_section")
    requested_visibility = data.get("visibility")

    if requested_visibility and not is_owner:
        return {"error": "forbidden", "message": "Only the owner can change visibility"}, 403

    current_content = read_file_from_repo(owner_user.username, wiki.slug, page.path, public=False) or ""
    updated_content = current_content

    if content is not None:
        updated_content = content
    elif append_section:
        heading = append_section.get("heading", "").strip()
        section_content = append_section.get("content", "").rstrip()
        if not heading or not section_content:
            return {"error": "bad_request", "message": "append_section requires heading and content"}, 400
        updated_content = current_content.rstrip() + f"\n\n## {heading}\n\n{section_content}\n"

    if is_owner and requested_visibility:
        updated_content = set_visibility_in_content(updated_content, requested_visibility)
    elif not is_owner:
        updated_content = set_visibility_in_content(updated_content, page.visibility)

    author_name, author_email = _current_author()

    if new_path:
        if Page.query.filter_by(wiki_id=wiki.id, path=new_path).first():
            return {"error": "conflict", "message": f"Path '{new_path}' already exists"}, 409

        old_path = page.path
        old_aliases = page_reference_aliases(old_path, page.title)
        rewritten_pages = []
        repo_changes = [{"action": "delete", "path": old_path}]

        for candidate in Page.query.filter_by(wiki_id=wiki.id).all():
            candidate_content = read_file_from_repo(owner_user.username, wiki.slug, candidate.path, public=False) or ""
            if candidate.id == page.id:
                candidate_content = updated_content
            rewritten = rewrite_wikilinks(candidate_content, old_aliases, new_path)
            if candidate.id == page.id or rewritten != candidate_content:
                target_path = new_path if candidate.id == page.id else candidate.path
                repo_changes.append({"action": "write", "path": target_path, "content": rewritten})
                rewritten_pages.append((candidate, target_path, rewritten))

        apply_repo_changes(
            owner_user.username,
            wiki.slug,
            repo_changes,
            f"Rename {old_path} -> {new_path}",
            author_name=author_name,
            author_email=author_email,
        )
        append_event_to_repo(owner_user.username, wiki.slug, "page.rename", old_path=old_path, new_path=new_path, actor=_current_username())

        for candidate, target_path, rewritten in rewritten_pages:
            candidate.path = target_path
            frontmatter, _ = parse_markdown_document(rewritten)
            candidate.visibility = frontmatter.get("visibility") or resolve_visibility(target_path, acl_rules)
            candidate.author = _current_username() if candidate.id == page.id else candidate.author
            update_page_metadata(candidate, rewritten, frontmatter)
        db.session.flush()
        for candidate, _, rewritten in rewritten_pages:
            refresh_wikilinks_for_page(candidate, rewritten)
    else:
        frontmatter, _ = parse_markdown_document(updated_content)
        page.visibility = frontmatter.get("visibility") or requested_visibility or page.visibility or resolve_visibility(page.path, acl_rules)
        page.author = _current_username()
        update_page_metadata(page, updated_content, frontmatter)
        db.session.flush()
        refresh_wikilinks_for_page(page, updated_content)
        sync_page_to_repo(
            owner_user.username,
            wiki.slug,
            page.path,
            updated_content,
            message=f"Update {page.path}",
            author_name=author_name,
            author_email=author_email,
        )
        append_event_to_repo(owner_user.username, wiki.slug, "page.update", path=page.path, visibility=page.visibility, actor=_current_username())

    regenerate_public_mirror(owner_user.username, wiki.slug, acl_rules)
    db.session.commit()

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

    remove_page_from_repo(owner_user.username, wiki.slug, page.path)
    append_event_to_repo(owner_user.username, wiki.slug, "page.delete", path=page.path, actor=request.current_user.username)
    db.session.delete(page)
    db.session.commit()

    acl_rules = load_acl_rules(owner_user.username, wiki.slug)
    regenerate_public_mirror(owner_user.username, wiki.slug, acl_rules)

    return "", 204


@api_bp.route("/wikis/<owner>/<slug>/pages/<path:page_path>/visibility", methods=["POST"])
@api_auth_required
def set_page_visibility(owner, slug, page_path):
    owner_user, wiki, err = _get_wiki_or_404(owner, slug)
    if err:
        return err
    if request.current_user.id != wiki.owner_id:
        return {"error": "forbidden", "message": "Only the owner can change visibility"}, 403

    page = Page.query.filter_by(wiki_id=wiki.id, path=page_path).first()
    if not page:
        return {"error": "not_found", "message": "Page not found"}, 404

    data = request.get_json(silent=True) or {}
    visibility = data.get("visibility")
    if not visibility:
        return {"error": "bad_request", "message": "visibility is required"}, 400

    content = read_file_from_repo(owner_user.username, wiki.slug, page.path, public=False) or ""
    content = set_visibility_in_content(content, visibility)
    frontmatter, _ = parse_markdown_document(content)
    page.visibility = frontmatter.get("visibility") or visibility
    update_page_metadata(page, content, frontmatter)
    refresh_wikilinks_for_page(page, content)
    sync_page_to_repo(owner_user.username, wiki.slug, page.path, content, message=f"Set visibility for {page.path}")
    append_event_to_repo(owner_user.username, wiki.slug, "page.visibility", path=page.path, visibility=page.visibility, actor=request.current_user.username)
    regenerate_public_mirror(owner_user.username, wiki.slug, load_acl_rules(owner_user.username, wiki.slug))
    db.session.commit()

    return jsonify({"path": page.path, "visibility": page.visibility})


@api_bp.route("/wikis/<owner>/<slug>/pages/<path:page_path>/share", methods=["POST"])
@api_auth_required
def share_page(owner, slug, page_path):
    owner_user, wiki, err = _get_wiki_or_404(owner, slug)
    if err:
        return err
    if request.current_user.id != wiki.owner_id:
        return {"error": "forbidden", "message": "Only the owner can manage sharing"}, 403

    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip().lower()
    role = (data.get("role") or "").strip().lower()
    if not username or role not in {"read", "edit"}:
        return {"error": "bad_request", "message": "username and role (read|edit) are required"}, 400

    acl_text = read_file_from_repo(owner_user.username, wiki.slug, ".wikihub/acl", public=False) or "* private\n"
    acl_line = f"{page_path} @{username}:{role}"
    if acl_line not in acl_text.splitlines():
        acl_text = acl_text.rstrip() + f"\n{acl_line}\n"
        sync_page_to_repo(owner_user.username, wiki.slug, ".wikihub/acl", acl_text, message=f"Share {page_path} with @{username}:{role}")
        append_event_to_repo(owner_user.username, wiki.slug, "page.share", path=page_path, grant=f"@{username}:{role}", actor=request.current_user.username)
        index_repo_pages(owner_user.username, wiki.slug, wiki, reset=True)
        regenerate_public_mirror(owner_user.username, wiki.slug, load_acl_rules(owner_user.username, wiki.slug))
        db.session.commit()

    return jsonify({"path": page_path, "grant": f"@{username}:{role}"})


@api_bp.route("/wikis/<owner>/<slug>/pages/<path:page_path>/append-section", methods=["POST"])
@api_auth_optional
def append_section(owner, slug, page_path):
    payload = request.get_json(silent=True) or {}
    patch_payload = {
        "heading": payload.get("heading"),
        "content": payload.get("content"),
    }
    headers = {}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    with current_app.test_client() as client:
        resp = client.patch(
            f"/api/v1/wikis/{owner}/{slug}/pages/{page_path}",
            json={"append_section": patch_payload},
            headers=headers,
        )
        return (resp.get_data(), resp.status_code, resp.headers.items())


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
                Wiki.owner_id == user.id,
                Page.visibility.in_(["public", "public-edit"]),
                db.and_(Wiki.owner_id == user.id, Page.visibility.in_(["unlisted", "unlisted-edit"])),
            )
        )
    else:
        query = query.filter(Page.visibility.in_(["public", "public-edit"]))

    # tag filter — search tags as text cast of the JSON field
    if tag:
        query = query.filter(
            db.cast(Page.frontmatter_json["tags"], db.String).contains(tag)
        )

    # fuzzy search: combine full-text, ILIKE on title/path, and trigram similarity
    like_pattern = f"%{q}%"
    ts_query = db.func.plainto_tsquery("english", q)
    fuzzy_filter = db.or_(
        Page.search_vector.op("@@")(ts_query),
        Page.title.ilike(like_pattern),
        Page.path.ilike(like_pattern),
    )
    query = query.filter(fuzzy_filter)

    total = query.count()
    # rank: full-text rank + trigram similarity on title for ordering
    ts_rank = db.func.ts_rank(Page.search_vector, ts_query)
    trgm_sim = db.func.similarity(db.func.coalesce(Page.title, Page.path), q)
    results = query.order_by(
        (ts_rank + trgm_sim).desc()
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


@api_bp.route("/wikis/<owner>/<slug>/history", methods=["GET"])
@api_auth_optional
def wiki_history(owner, slug):
    owner_user, wiki, err = _get_wiki_or_404(owner, slug)
    if err:
        return err

    from app.git_backend import _repo_path

    user = getattr(request, "current_user", None)
    is_owner = bool(user and user.id == wiki.owner_id)
    repo = _repo_path(owner_user.username, wiki.slug, public=not is_owner)
    if not os.path.isdir(repo):
        return jsonify({"commits": [], "total": 0})

    limit = min(int(request.args.get("limit", 20)), 100)
    offset = max(int(request.args.get("offset", 0)), 0)
    path = request.args.get("path")

    cmd = [
        "git",
        "-C",
        repo,
        "log",
        "--format=%H%x1f%an%x1f%aI%x1f%s%x1e",
        "--name-only",
        f"--max-count={limit}",
        f"--skip={offset}",
    ]
    if path:
        cmd += ["--", path]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return {"error": "git_error", "message": result.stderr.strip() or "Unable to read history"}, 500

    commits = []
    for chunk in result.stdout.split("\x1e"):
        chunk = chunk.strip()
        if not chunk:
            continue
        lines = [line for line in chunk.splitlines() if line.strip()]
        header = lines[0].split("\x1f")
        files_changed = lines[1:]
        commits.append({
            "sha": header[0],
            "author": header[1],
            "date": header[2],
            "message": header[3],
            "files_changed": files_changed,
        })

    return jsonify({"commits": commits, "total": len(commits)})


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
    update_page_metadata(page, content, frontmatter)
    db.session.flush()
    refresh_wikilinks_for_page(page, content)
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

    acl_rules = load_acl_rules(username, slug)
    regenerate_public_mirror(username, slug, acl_rules)
    return jsonify({"status": "ok"})
