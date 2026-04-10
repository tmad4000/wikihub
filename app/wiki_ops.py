import hashlib
import os
import shutil

from app import db
from app.acl import parse_acl, resolve_visibility
from app.content_utils import extract_wikilinks, parse_markdown_document
from app.git_backend import _repo_path, init_wiki_repo
from app.git_sync import list_files_in_repo, read_file_from_repo, regenerate_public_mirror, scaffold_wiki, sync_page_to_repo
from app.models import Page, Wikilink, Wiki, Star, Fork, User


def load_acl_rules(username, slug):
    acl_content = read_file_from_repo(username, slug, ".wikihub/acl")
    return parse_acl(acl_content) if acl_content else []


def update_page_metadata(page, content, frontmatter=None):
    if frontmatter is None:
        frontmatter, body = parse_markdown_document(content)
    else:
        _, body = parse_markdown_document(content)

    page.title = frontmatter.get("title", os.path.splitext(os.path.basename(page.path))[0])
    page.frontmatter_json = frontmatter
    page.content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    page.excerpt = body[:200].replace("\n", " ").strip() if body else ""
    page.search_vector = db.func.to_tsvector("english", f"{page.title or ''} {body or ''}")
    return frontmatter, body


def refresh_wikilinks_for_page(page, content):
    Wikilink.query.filter_by(source_page_id=page.id).delete()
    targets = extract_wikilinks(content)
    if not targets:
        return

    wiki_pages = {
        candidate.path: candidate.id
        for candidate in Page.query.filter_by(wiki_id=page.wiki_id).all()
    }
    basename_pages = {}
    for candidate in Page.query.filter_by(wiki_id=page.wiki_id).all():
        basename = candidate.path.rsplit("/", 1)[-1]
        basename_pages.setdefault(basename, candidate.id)
        if basename.endswith(".md"):
            basename_pages.setdefault(basename[:-3], candidate.id)

    seen = set()
    for target in targets:
        if target in seen:
            continue
        seen.add(target)
        normalized = target if target.endswith(".md") else f"{target}.md"
        target_page_id = wiki_pages.get(normalized) or wiki_pages.get(target) or basename_pages.get(target)
        db.session.add(
            Wikilink(
                source_page_id=page.id,
                target_path=target,
                target_page_id=target_page_id,
            )
        )


def sync_wiki_counters(wiki):
    wiki.star_count = Star.query.filter_by(wiki_id=wiki.id).count()
    wiki.fork_count = Fork.query.filter_by(source_wiki_id=wiki.id).count()


def create_wiki_for_user(user, slug, title=None, description="", scaffold=True):
    wiki = Wiki(
        owner_id=user.id,
        slug=slug,
        title=title or slug,
        description=description or "",
    )
    db.session.add(wiki)
    db.session.flush()

    init_wiki_repo(user.username, slug)
    if scaffold:
        scaffold_wiki(user.username, slug)
        index_repo_pages(user.username, slug, wiki, reset=True)
        regenerate_public_mirror(user.username, slug, load_acl_rules(user.username, slug))

    return wiki


def ensure_personal_wiki(user):
    personal = Wiki.query.filter_by(owner_id=user.id, slug=user.username).first()
    if personal:
        return personal
    return create_wiki_for_user(
        user,
        slug=user.username,
        title=user.display_name or user.username,
        description=None,
        scaffold=True,
    )


def ensure_official_wiki():
    """Ensure the @wikihub user exists. The personal wiki (slug=wikihub) is
    auto-created by ensure_personal_wiki; no separate 'wiki' slug needed."""
    user = User.query.filter_by(username="wikihub").first()
    if not user:
        user = User(username="wikihub", display_name="wikihub")
        db.session.add(user)
        db.session.flush()

    ensure_personal_wiki(user)
    return Wiki.query.filter_by(owner_id=user.id, slug="wikihub").first()


def replace_acl_file(username, slug, content, message="Update ACL"):
    sync_page_to_repo(username, slug, ".wikihub/acl", content, message=message)


def delete_wiki_repos(username, slug):
    for public in (False, True):
        repo = _repo_path(username, slug, public=public)
        if os.path.isdir(repo):
            shutil.rmtree(repo)


def index_repo_pages(username, slug, wiki, reset=False):
    acl_rules = load_acl_rules(username, slug)

    if reset:
        Page.query.filter_by(wiki_id=wiki.id).delete()
        db.session.flush()

    existing_pages = {
        page.path: page
        for page in Page.query.filter_by(wiki_id=wiki.id).all()
    }
    seen_paths = set()

    for path in list_files_in_repo(username, slug):
        if not path.endswith(".md"):
            continue

        content = read_file_from_repo(username, slug, path)
        if content is None:
            continue

        frontmatter, _ = parse_markdown_document(content)
        visibility = resolve_visibility(path, acl_rules, frontmatter.get("visibility"))
        page = existing_pages.get(path)
        if page is None:
            page = Page(wiki_id=wiki.id, path=path)
            db.session.add(page)
        page.visibility = visibility
        update_page_metadata(page, content, frontmatter)
        seen_paths.add(path)

    for path, page in existing_pages.items():
        if path not in seen_paths:
            db.session.delete(page)

    db.session.flush()

    pages = Page.query.filter_by(wiki_id=wiki.id).all()
    content_by_path = {
        page.path: read_file_from_repo(username, slug, page.path) or ""
        for page in pages
    }
    for page in pages:
        refresh_wikilinks_for_page(page, content_by_path[page.path])
