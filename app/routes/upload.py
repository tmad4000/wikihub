"""
web upload routes for wikihub.

supports:
- folder/zip drag-drop upload → unpacks, commits, syncs to DB
- create new wiki from scratch
"""

import io
import os
import zipfile

from flask import render_template, request, redirect, url_for, flash
from flask_login import current_user, login_required

from app import db
from app.acl import resolve_visibility
from app.models import Wiki, Page
from app.content_utils import parse_markdown_document
from app.git_sync import regenerate_public_mirror, read_file_from_repo, scaffold_wiki, sync_page_to_repo
from app.routes import main_bp
from app.wiki_ops import create_wiki_for_user, index_repo_pages, load_acl_rules, update_page_metadata


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

        from flask import current_app
        wiki_count = Wiki.query.filter_by(owner_id=current_user.id).count()
        if wiki_count >= current_app.config["MAX_WIKIS_PER_USER"]:
            flash(f"You've reached the limit of {current_app.config['MAX_WIKIS_PER_USER']} wikis")
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
            scaffold_wiki(current_user.username, slug)
            _index_repo_pages(current_user.username, slug, wiki.id)

        acl_rules = load_acl_rules(current_user.username, slug)
        regenerate_public_mirror(current_user.username, slug, acl_rules)

        return redirect(url_for("wiki.wiki_index", username=current_user.username, slug=slug))

    return render_template("new_wiki.html")
def _process_uploads(username, slug, wiki_id, files):
    """process uploaded files — writes each to git and indexes in DB."""
    for f in files:
        if not f.filename:
            continue

        filename = f.filename
        # handle zip files
        if filename.lower().endswith(".zip"):
            _process_zip(username, slug, wiki_id, f)
            continue

        content = f.read()
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            text = None

        # write to git
        sync_page_to_repo(username, slug, filename, text if text else content.decode("latin-1"))

        if filename.endswith(".md") and text:
            _index_page(wiki_id, filename, text, username, slug)

    # also scaffold ACL if not present
    if ".wikihub/acl" not in [f.filename for f in files if f.filename]:
        acl_content = (
            "# wikihub ACL\n"
            "* private\n"
        )
        sync_page_to_repo(username, slug, ".wikihub/acl", acl_content)


def _process_zip(username, slug, wiki_id, zip_file):
    """unpack a zip file and commit all contents."""
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
            return any(p.startswith(".") for p in parts) or name.startswith("__MACOSX/")
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

        for info in entries:
            filepath = info.filename
            content = zf.read(info)
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                text = content.decode("latin-1")

            sync_page_to_repo(username, slug, filepath, text)

            if filepath.endswith(".md"):
                _index_page(wiki_id, filepath, text, username, slug)


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
    page.visibility = resolve_visibility(path, acl_rules, frontmatter.get("visibility"))
    update_page_metadata(page, content, frontmatter)
    db.session.commit()


def _index_repo_pages(username, slug, wiki_id):
    """index all .md files from a repo into the DB."""
    wiki = Wiki.query.get(wiki_id)
    if wiki:
        index_repo_pages(username, slug, wiki, reset=True)
