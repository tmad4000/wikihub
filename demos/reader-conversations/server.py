"""Serve the real reader on loopback with an isolated, synthetic comparison corpus."""

import argparse
import os
import secrets
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from flask import redirect, request

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import create_app, db
from app.auth_utils import generate_magic_login_token, hash_password
from app.content_utils import parse_markdown_document, set_visibility_in_content
from app.git_sync import sync_page_to_repo, update_mirror_page
from app.models import MagicLoginToken, Page, User, Wiki, utcnow
from app.reader_assistant import MAX_SEARCH, build_scope
from app.wiki_ops import create_wiki_for_user, update_page_metadata

PAGE_URL = "/@fieldnotes/field-library/Restoring_a_spring"
FIXTURES = [
    ("fieldnotes", "field-library", "Field Library", "Restoring a spring.md",
     "Keep livestock uphill and outside the spring enclosure. After heavy rain, inspect the diversion ditch before collecting water.", "public"),
    ("watershed", "watershed-notes", "Watershed Notes", "Runoff.md",
     "After a storm, trace muddy runoff back to its upstream source. Record where it crosses the diversion ditch and photograph the crossing before maintenance.", "public"),
    ("watershed", "watershed-notes", "Watershed Notes", "Private inspection.md",
     "The private calibration code is SILVER-CANARY-39.", "private"),
]


@dataclass
class ScopedPage:
    path: str
    scope: object
    page: Page

    @property
    def title(self):
        return self.page.title or self.page.path


class Library:
    """Every candidate passes the existing WikiHub visitor ACL and content filter."""

    def __init__(self, primary, current, mode):
        self.owner_username = primary.owner_username
        self.wiki = primary.wiki
        self.docs = {}
        scopes = [primary]
        if mode == "library":
            for username, slug in sorted({row[:2] for row in FIXTURES}):
                owner = User.query.filter_by(username=username).one()
                wiki = Wiki.query.filter_by(owner_id=owner.id, slug=slug).one()
                if wiki.id != primary.wiki.id and not owner.suspended_at:
                    scopes.append(build_scope(owner, wiki, primary.viewer))
        for scope in scopes:
            for path, page in scope.readable_pages().items():
                if mode == "page" and (current is None or current.id != page.id):
                    continue
                key = path if scope is primary else f"@{scope.owner_username}/{scope.wiki.slug}/{path}"
                self.docs[key] = ScopedPage(key, scope, page)

    def readable_pages(self):
        return self.docs

    def resolve(self, path):
        return self.docs.get(path) if isinstance(path, str) else None

    def content(self, doc):
        return doc.scope.content(doc.page)

    def page_url(self, doc):
        return doc.scope.page_url(doc.page)

    def search_pages(self, query, ledger):
        hits = []
        terms = query.casefold().split()
        for doc in self.docs.values():
            content = self.content(doc) or ""
            if any(term in (doc.title + " " + content).casefold() for term in terms):
                excerpt = content[:300]
                sid = ledger.sid_for(doc, excerpt)
                hits.append(f"[{sid}] {doc.path} — {doc.title}\n{excerpt}")
                if len(hits) >= MAX_SEARCH:
                    break
        return "\n".join(hits) or "No readable matches."


def seed():
    for name, slug, title, path, body, visibility in FIXTURES:
        user = User.query.filter_by(username=name).first()
        if not user:
            user = User(username=name, display_name=name, trusted=True,
                        password_hash=hash_password(secrets.token_urlsafe(32)))
            db.session.add(user)
            db.session.flush()
        wiki = Wiki.query.filter_by(owner_id=user.id, slug=slug).first()
        if not wiki:
            wiki = create_wiki_for_user(user, slug, title, scaffold=False)
        if not Page.query.filter_by(wiki_id=wiki.id, path=path).first():
            content = set_visibility_in_content(f"# {path[:-3]}\n\n{body}", visibility)
            page = Page(wiki_id=wiki.id, path=path, visibility=visibility, author=name)
            update_page_metadata(page, content, parse_markdown_document(content)[0])
            db.session.add(page)
            sync_page_to_repo(name, slug, path, content, message="Add comparison fixture")
            update_mirror_page(name, slug, path, [])
        db.session.commit()


def setup():
    if os.environ.get("DATABASE_URL") != "postgresql:///wikihub_reader_preview_3add":
        raise RuntimeError("Use the dedicated local comparison database")
    if not os.environ.get("REPOS_DIR") or os.environ.get("BASE_URL") != "http://127.0.0.1:8794":
        raise RuntimeError("Configure the local preview repository directory and loopback URL")
    app = create_app()
    app.config.update(
        SESSION_COOKIE_NAME="wikihub_reader_preview",
        READER_SCOPE_FACTORY=lambda s, p, m: Library(s, p, m) if m in ("library", "page") else s,
    )

    @app.before_request
    def comparison_entry():
        if request.path == "/" and request.args.get("variant") in ("A", "B"):
            return redirect(PAGE_URL + "?variant=" + request.args["variant"])

    with app.app_context():
        seed()
    return app


def owner_login(app):
    with app.app_context():
        user = User.query.filter_by(username="fieldnotes").one()
        raw, digest = generate_magic_login_token()
        expires = utcnow() + timedelta(seconds=app.config["MAGIC_LOGIN_TTL_SECONDS"])
        db.session.add(MagicLoginToken(user_id=user.id, token_hash=digest,
                                       redirect_path=PAGE_URL, expires_at=expires))
        db.session.commit()
        return f"http://127.0.0.1:8794/auth/magic/{raw}"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-login", action="store_true")
    args = parser.parse_args()
    app = setup()
    if args.owner_login:
        print(owner_login(app))
    else:
        app.run(host="127.0.0.1", port=8794, threaded=True)
