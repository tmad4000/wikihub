"""
web upload routes for wikihub.

supports:
- folder/zip drag-drop upload → unpacks, commits, syncs to DB
- create new wiki from scratch
- anonymous upload: auto-mints an ephemeral account, returns the API key to claim later
"""

import io
import os
import secrets
import zipfile

from flask import current_app, jsonify, render_template, request, redirect, url_for, flash
from flask_login import current_user, login_required, login_user

from app import db
from app.acl import normalize_page_visibility, resolve_visibility
from app.auth_utils import generate_api_key, rate_limit_writes
from app.credentials_hint import build_client_config, resolve_server_url
from app.models import ApiKey, User, Wiki, Page
from app.content_utils import parse_markdown_document
from app.git_sync import regenerate_public_mirror, read_file_from_repo, scaffold_wiki, sync_page_to_repo
from app.page_utils import is_wikihub_plumbing_path
from app.routes import main_bp
from app.wiki_ops import create_wiki_for_user, ensure_personal_wiki, index_repo_pages, load_acl_rules, refresh_wikilinks_for_page, reindex_wiki_pages_and_mirror, update_page_metadata


@main_bp.route("/new", methods=["GET", "POST"])
@login_required
def create_wiki_web():
    if request.method == "POST":
        slug = request.form.get("slug", "").strip().lower()
        slug = "".join(c for c in slug if c.isalnum() or c in "-_")
        title = request.form.get("title", slug).strip()
        description = request.form.get("description", "").strip()

        if not slug:
            flash("Slug is required")
            return render_template("new_wiki.html"), 400

        if Wiki.query.filter_by(owner_id=current_user.id, slug=slug).first():
            flash(f"Wiki '{slug}' already exists")
            return render_template("new_wiki.html"), 409

        wiki_count = Wiki.query.filter_by(owner_id=current_user.id).count()
        limit = current_user.effective_wiki_limit()
        if wiki_count >= limit:
            flash(f"You've reached the limit of {limit} wikis")
            return render_template("new_wiki.html"), 429

        wiki = create_wiki_for_user(current_user, slug=slug, title=title, description=description, scaffold=False)
        db.session.commit()

        # check for uploaded files
        uploaded = request.files.getlist("files")
        if uploaded and uploaded[0].filename:
            try:
                _process_uploads(current_user.username, slug, wiki.id, uploaded)
            except ValueError as e:
                flash(str(e))
                return render_template("new_wiki.html"), 413
        else:
            template = request.form.get("template", "structured")
            scaffold_wiki(current_user.username, slug, template=template)
            _index_repo_pages(current_user.username, slug, wiki.id)

        acl_rules = load_acl_rules(current_user.username, slug)
        regenerate_public_mirror(current_user.username, slug, acl_rules)

        return redirect(url_for("wiki.wiki_index", username=current_user.username, slug=slug))

    return render_template("new_wiki.html")


@main_bp.route("/new-anonymous", methods=["POST"])
@rate_limit_writes(max_per_minute=5, max_per_ip_per_minute=5)
def create_wiki_anonymous():
    """Mint an ephemeral account and publish the dropped files in one call.
    Response body carries the api_key so the anon user can later claim/keep
    the account; the session is also logged in so the redirect to the new
    wiki lands the drafter as its owner."""
    if current_user.is_authenticated:
        return {"error": "already_authed", "redirect_to": url_for("main.create_wiki_web")}, 400

    slug = request.form.get("slug", "").strip().lower()
    slug = "".join(c for c in slug if c.isalnum() or c in "-_")
    title = (request.form.get("title") or slug).strip()
    description = request.form.get("description", "").strip()

    if not slug:
        return {"error": "bad_request", "message": "slug is required"}, 400

    anon_name = None
    for _ in range(8):
        candidate = f"anon-{secrets.token_hex(4)}"
        if not User.query.filter_by(username=candidate).first():
            anon_name = candidate
            break
    if not anon_name:
        return {"error": "conflict", "message": "could not mint anon username"}, 500

    user = User(username=anon_name, email=None, display_name=None, password_hash=None)
    db.session.add(user)
    db.session.flush()
    ensure_personal_wiki(user)

    raw_key, key_hash, key_prefix = generate_api_key()
    db.session.add(ApiKey(user_id=user.id, key_hash=key_hash, key_prefix=key_prefix, label="Anonymous upload key"))

    if Wiki.query.filter_by(owner_id=user.id, slug=slug).first():
        db.session.rollback()
        return {"error": "conflict", "message": f"Wiki '{slug}' already exists"}, 409

    wiki = create_wiki_for_user(user, slug=slug, title=title, description=description, scaffold=False)
    db.session.commit()

    uploaded = request.files.getlist("files")
    if uploaded and uploaded[0].filename:
        try:
            _process_uploads(user.username, slug, wiki.id, uploaded)
        except ValueError as e:
            return {"error": "bad_request", "message": str(e)}, 413
    else:
        scaffold_wiki(user.username, slug)
        _index_repo_pages(user.username, slug, wiki.id)

    acl_rules = load_acl_rules(user.username, slug)
    regenerate_public_mirror(user.username, slug, acl_rules)

    login_user(user, remember=False)

    server_url = resolve_server_url(current_app, request)
    return jsonify({
        "wiki_url": url_for("wiki.wiki_index", username=user.username, slug=slug),
        "username": user.username,
        "api_key": raw_key,
        "client_config": build_client_config(user.username, raw_key, server_url),
        "claim_hint": "Save your api_key to keep editing this wiki. You can add an email at /settings to claim the account permanently.",
    }), 201


def _process_uploads(username, slug, wiki_id, files):
    """process uploaded files — writes each to git and indexes in DB."""
    entries = []
    for f in files:
        if not f.filename:
            continue

        filename = f.filename
        # handle zip files
        if filename.lower().endswith(".zip"):
            entries.extend(_extract_zip_entries(f))
            continue

        content = f.read()
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            text = content.decode("latin-1")

        entries.append((filename, text))

    uploaded_paths = [path for path, _text in entries]
    has_uploaded_acl = ".wikihub/acl" in uploaded_paths

    for path, text in entries:
        sync_page_to_repo(username, slug, path, text)

    if not has_uploaded_acl:
        acl_content = (
            "# wikihub ACL\n"
            "* private\n"
        )
        sync_page_to_repo(username, slug, ".wikihub/acl", acl_content)

    if has_uploaded_acl:
        wiki = Wiki.query.get(wiki_id)
        if wiki:
            reindex_wiki_pages_and_mirror(username, slug, wiki)
            db.session.commit()
        return

    for path, text in entries:
        if path.endswith(".md") and not is_wikihub_plumbing_path(path):
            _index_page(wiki_id, path, text, username, slug)


def _extract_zip_entries(zip_file):
    """unpack a zip file into repo path/content pairs."""
    from flask import current_app
    max_files = current_app.config["MAX_UPLOAD_FILES"]
    max_page = current_app.config["MAX_PAGE_SIZE"]

    data = zip_file.read()
    if not data or len(data) < 4:
        raise ValueError("Uploaded file is empty or too small to be a zip")
    try:
        zf_obj = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("Uploaded file is not a valid zip archive")
    with zf_obj as zf:
        def _skip(name):
            parts = name.split("/")
            return name.startswith("__MACOSX/") or any(p.startswith(".") and p != ".wikihub" for p in parts)
        entries = [i for i in zf.infolist() if not i.is_dir() and not _skip(i.filename)]

        # strip common top-level directory wrapper (e.g. notes/ wrapping everything)
        if entries:
            prefixes = set()
            for e in entries:
                first = e.filename.split("/", 1)[0]
                prefixes.add(first)
            if len(prefixes) == 1 and all("/" in e.filename for e in entries):
                prefix = prefixes.pop() + "/"
                for e in entries:
                    e.filename = e.filename[len(prefix):]
                entries = [e for e in entries if e.filename]  # drop empty after strip

        if len(entries) > max_files:
            raise ValueError(f"Zip contains {len(entries)} files, max is {max_files}")

        oversized = [i.filename for i in entries if i.file_size > max_page]
        if oversized:
            names = ", ".join(oversized[:5])
            more = f" (+{len(oversized) - 5} more)" if len(oversized) > 5 else ""
            raise ValueError(f"Files too large (2MB max): {names}{more}")

        extracted = []
        for info in entries:
            filepath = info.filename
            content = zf.read(info)
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                text = content.decode("latin-1")

            extracted.append((filepath, text))
        return extracted


def _index_page(wiki_id, path, content, username, slug):
    """create a Page row from content."""
    acl_rules = load_acl_rules(username, slug)
    try:
        frontmatter, _ = parse_markdown_document(content)
    except Exception:
        frontmatter = {}
    page = Page.query.filter_by(wiki_id=wiki_id, path=path).first()
    if not page:
        page = Page(wiki_id=wiki_id, path=path)
        db.session.add(page)
    page.visibility = normalize_page_visibility(resolve_visibility(path, acl_rules, frontmatter.get("visibility"))) or "private"
    update_page_metadata(page, content, frontmatter)
    db.session.flush()
    refresh_wikilinks_for_page(page, content)
    db.session.commit()


def _index_repo_pages(username, slug, wiki_id):
    """index all .md files from a repo into the DB."""
    wiki = Wiki.query.get(wiki_id)
    if wiki:
        index_repo_pages(username, slug, wiki, reset=True)
