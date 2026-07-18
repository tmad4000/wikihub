"""
wikihub end-to-end tests.

minimal and intentional — each test verifies a real user flow,
not individual functions. run with: python3 tests/test_e2e.py
"""

import io
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import timedelta
from urllib.parse import quote, urlparse
from sqlalchemy import text

# ensure app is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["SECRET_KEY"] = "test-secret"
# DATABASE_URL / REPOS_DIR default to the shared test fixtures but may be
# overridden via env so parallel test lanes can run on isolated DB + repos dirs
# without colliding (duplicate accounts, git index races, cross-run truncation).
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/wikihub_test")
os.environ.setdefault("REPOS_DIR", "/tmp/wikihub-test-repos")
os.environ["ADMIN_TOKEN"] = "test-admin-token"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from app import create_app, db
from app.auth_utils import _ip_write_timestamps, _write_timestamps
from app.models import Page, User, Wiki, utcnow


def setup():
    shutil.rmtree(os.environ["REPOS_DIR"], ignore_errors=True)
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        reset_database()
    return app


def teardown():
    shutil.rmtree(os.environ["REPOS_DIR"], ignore_errors=True)


def reset_database():
    for table in [
        "proposal_comments",
        "proposal_page_patches",
        "proposal_revisions",
        "proposals",
        "wikilinks",
        "forks",
        "stars",
        "pages",
        "pending_invites",
        "wikis",
        "password_reset_tokens",
        "email_verification_tokens",
        "magic_login_tokens",
        "api_keys",
        "username_redirects",
        "feedback",
        "audit_log",
        "sessions",
        "users",
    ]:
        try:
            db.session.execute(text(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE'))
            db.session.commit()
        except Exception:
            db.session.rollback()


def test_agent_account_creation(client):
    """agent creates account via API, gets key, authenticates"""
    r = client.post("/api/v1/accounts", json={"username": "agent1"})
    assert r.status_code == 201
    data = r.get_json()
    assert data["username"] == "agent1"
    assert data["api_key"].startswith("wh_")

    # auth with key
    r = client.get("/api/v1/accounts/me", headers={"Authorization": f"Bearer {data['api_key']}"})
    assert r.status_code == 200
    assert r.get_json()["username"] == "agent1"

    # personal wiki is auto-created and exposed at /@username
    r = client.get("/@agent1")
    assert r.status_code == 200
    return data["api_key"]


def test_wiki_lifecycle(client, api_key):
    """create wiki, add page, read it, update it, delete it"""
    h = {"Authorization": f"Bearer {api_key}"}

    # create
    r = client.post("/api/v1/wikis", json={"slug": "test-wiki", "title": "Test"}, headers=h)
    assert r.status_code == 201

    # add page
    r = client.post("/api/v1/wikis/agent1/test-wiki/pages", json={
        "path": "wiki/hello.md",
        "content": "---\ntitle: Hello\nvisibility: public\n---\n\n# Hello\n\nWorld.",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201

    # read via API
    r = client.get("/api/v1/wikis/agent1/test-wiki/pages/wiki/hello.md", headers=h)
    assert r.status_code == 200
    assert "Hello" in r.get_json()["title"]

    # API content negotiation
    r = client.get("/api/v1/wikis/agent1/test-wiki/pages/wiki/hello.md", headers={**h, "Accept": "text/markdown"})
    assert r.status_code == 200
    assert "text/markdown" in r.content_type

    # read via web (HTML)
    r = client.get("/@agent1/test-wiki/wiki/hello")
    assert r.status_code == 200
    assert b"Hello" in r.data

    # content negotiation
    r = client.get("/@agent1/test-wiki/wiki/hello", headers={"Accept": "text/markdown"})
    assert r.status_code == 200
    assert "text/markdown" in r.content_type

    # update
    r = client.put("/api/v1/wikis/agent1/test-wiki/pages/wiki/hello.md", json={
        "content": "# Hello\n\nUpdated.",
    }, headers=h)
    assert r.status_code == 200

    # delete page — but re-add one so the wiki+mirror stay valid for later tests
    r = client.delete("/api/v1/wikis/agent1/test-wiki/pages/wiki/hello.md", headers=h)
    assert r.status_code == 204

    r = client.post("/api/v1/wikis/agent1/test-wiki/pages", json={
        "path": "wiki/index.md",
        "content": "---\ntitle: Index\nvisibility: public\n---\n\n# Test Wiki\n\nIndex page.",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201


def test_gdoc_toc_anchors_rewritten(client, api_key):
    """Google-Docs-imported pages carry a TOC linking to Google bookmark ids
    (#h.xxxx) that never resolve, because headings render with slug ids. The
    renderer must rewrite those TOC links to the real heading slugs by
    recovering the title from the link text. (wikihub-vcrq)"""
    h = {"Authorization": f"Bearer {api_key}"}

    # TOC links use Google's #h.xxxx bookmark ids; link text is "Title<tab>page#".
    # Two "Misc" sections exercise the duplicate-heading ordering.
    content = (
        "---\ntitle: TOC Doc\nvisibility: public\n"
        "source_gdoc: https://docs.google.com/document/d/abc\n---\n\n"
        "[Feeds        3](#h.v92l0g36bhyw)\n\n"
        "[Misc        4](#h.95iqrijn7rba)\n\n"
        "[Misc        7](#h.onq6k9elt3aq)\n\n"
        "### Feeds\nfeeds content\n\n"
        "### Misc\nfirst misc\n\n"
        "### Other\nother\n\n"
        "### Misc\nsecond misc\n"
    )
    r = client.post("/api/v1/wikis/agent1/test-wiki/pages", json={
        "path": "wiki/tocdoc.md",
        "content": content,
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201

    r = client.get("/@agent1/test-wiki/wiki/tocdoc")
    assert r.status_code == 200
    html = r.get_data(as_text=True)

    # no dead Google bookmark anchors remain
    assert 'href="#h.' not in html, "unresolved #h. TOC anchor still present"
    # TOC links now point at real heading slugs, duplicates ordered
    assert 'href="#feeds"' in html
    assert 'href="#misc"' in html
    assert 'href="#misc-1"' in html
    # and those slugs exist as heading ids
    assert 'id="feeds"' in html
    assert 'id="misc-1"' in html


def test_wysiwyg_editor_surface(client, api_key):
    """The editor serves the Milkdown WYSIWYG surface alongside the markdown
    textarea: bundle script, mount div, and the WYSIWYG tab. (wikihub-s2cj)

    Uses a public-edit page so the edit route is reachable anonymously (core
    product principle 2) — avoids the session-auth dance."""
    h = {"Authorization": f"Bearer {api_key}"}
    r = client.post("/api/v1/wikis/agent1/test-wiki/pages", json={
        "path": "wiki/wyz.md",
        "content": "---\ntitle: Wyz\nvisibility: public-edit\n---\n\n# Wyz\n\nHello **world**.",
        "visibility": "public-edit",
    }, headers=h)
    assert r.status_code == 201

    r = client.get("/@agent1/test-wiki/wiki/wyz/edit")
    assert r.status_code == 200, f"editor GET failed: {r.status_code} {r.get_data(as_text=True)[:200]}"
    html = r.get_data(as_text=True)
    assert "milkdown-bundle.js" in html, "Milkdown bundle not referenced in editor"
    assert 'id="milkdown-editor"' in html, "Milkdown mount div missing"
    assert ">WYSIWYG<" in html, "WYSIWYG tab missing"
    assert 'id="editor-textarea"' in html, "canonical markdown textarea missing"
    # WYSIWYG is the default (active) tab (wikihub-scay)
    assert "class=\"editor-tab active\" onclick=\"showTab('wysiwyg')\"" in html, "WYSIWYG should be the default tab"


def test_page_etag_conflict(client, api_key):
    """stale If-Match writes are rejected with 409 instead of silently overwriting."""
    h = {"Authorization": f"Bearer {api_key}"}

    r = client.post("/api/v1/wikis", json={"slug": "etag-wiki", "title": "ETag Wiki"}, headers=h)
    assert r.status_code == 201

    r = client.post("/api/v1/wikis/agent1/etag-wiki/pages", json={
        "path": "wiki/conflict.md",
        "content": "---\ntitle: Conflict\nvisibility: public\n---\n\n# Conflict\n\nBase.",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201

    r = client.get("/api/v1/wikis/agent1/etag-wiki/pages/wiki/conflict.md", headers=h)
    assert r.status_code == 200
    etag = r.headers.get("ETag")
    assert etag

    r = client.put("/api/v1/wikis/agent1/etag-wiki/pages/wiki/conflict.md", json={
        "content": "# Conflict\n\nFirst edit.",
    }, headers={**h, "If-Match": etag})
    assert r.status_code == 200

    r = client.put("/api/v1/wikis/agent1/etag-wiki/pages/wiki/conflict.md", json={
        "content": "# Conflict\n\nStale edit should fail.",
    }, headers={**h, "If-Match": etag})
    assert r.status_code == 409
    assert r.get_json()["error"] == "conflict"


def test_authenticated_bulk_writes_rate_limit(client, api_key, app):
    """authenticated bulk publishing gets a roomy quota; anonymous writes stay tight."""
    h = {"Authorization": f"Bearer {api_key}"}
    old_config = {
        "WRITE_RATE_LIMITS_IN_TESTS": app.config.get("WRITE_RATE_LIMITS_IN_TESTS"),
        "WRITE_RATE_LIMIT_AUTHENTICATED_PER_MINUTE": app.config.get("WRITE_RATE_LIMIT_AUTHENTICATED_PER_MINUTE"),
        "WRITE_RATE_LIMIT_AUTHENTICATED_IP_PER_MINUTE": app.config.get("WRITE_RATE_LIMIT_AUTHENTICATED_IP_PER_MINUTE"),
        "WRITE_RATE_LIMIT_ANONYMOUS_IP_PER_MINUTE": app.config.get("WRITE_RATE_LIMIT_ANONYMOUS_IP_PER_MINUTE"),
    }
    app.config["WRITE_RATE_LIMITS_IN_TESTS"] = True
    app.config["WRITE_RATE_LIMIT_AUTHENTICATED_PER_MINUTE"] = 12
    app.config["WRITE_RATE_LIMIT_AUTHENTICATED_IP_PER_MINUTE"] = 24
    app.config["WRITE_RATE_LIMIT_ANONYMOUS_IP_PER_MINUTE"] = 2
    _write_timestamps.clear()
    _ip_write_timestamps.clear()

    try:
        r = client.post("/api/v1/wikis", json={"slug": "bulk-rate", "title": "Bulk Rate"}, headers=h)
        assert r.status_code == 201

        for i in range(12):
            r = client.post("/api/v1/wikis/agent1/bulk-rate/pages", json={
                "path": f"bulk/page-{i}.md",
                "content": f"---\ntitle: Page {i}\nvisibility: public\n---\n\n# Page {i}\n",
                "visibility": "public",
            }, headers=h)
            assert r.status_code == 201, f"authenticated write {i} should pass, got {r.status_code}: {r.get_data(as_text=True)[:200]}"

        r = client.post("/api/v1/wikis/agent1/bulk-rate/pages", json={
            "path": "bulk/page-over.md",
            "content": "# Over user limit\n",
            "visibility": "public",
        }, headers=h)
        assert r.status_code == 429
        assert "12/min" in r.get_json()["message"]

        _write_timestamps.clear()
        _ip_write_timestamps.clear()
        r = client.post("/api/v1/wikis/agent1/bulk-rate/pages", json={
            "path": "anonymous/open.md",
            "content": "---\ntitle: Open\nvisibility: public-edit\n---\n\n# Open\n",
            "visibility": "public-edit",
        }, headers=h)
        assert r.status_code == 201
        _write_timestamps.clear()
        _ip_write_timestamps.clear()

        for i in range(2):
            r = client.put("/api/v1/wikis/agent1/bulk-rate/pages/anonymous/open.md", json={
                "content": f"# Anonymous edit {i}\n",
            })
            assert r.status_code == 200, f"anonymous write {i} should pass, got {r.status_code}: {r.get_data(as_text=True)[:200]}"

        r = client.put("/api/v1/wikis/agent1/bulk-rate/pages/anonymous/open.md", json={
            "content": "# Over anonymous limit\n",
        })
        assert r.status_code == 429
        assert "2/min" in r.get_json()["message"]
    finally:
        app.config.update(old_config)
        _write_timestamps.clear()
        _ip_write_timestamps.clear()


def test_binary_file_serving(client, api_key):
    """upload and serve binary files (images, PDFs) from wiki repos"""
    h = {"Authorization": f"Bearer {api_key}"}

    # create a wiki for binary test
    r = client.post("/api/v1/wikis", json={"slug": "media-wiki", "title": "Media"}, headers=h)
    assert r.status_code == 201

    # make wiki/** public so binary files are accessible — write ACL directly to git
    from app.git_sync import sync_page_to_repo
    sync_page_to_repo("agent1", "media-wiki", ".wikihub/acl", "* private\nwiki/** public\n")

    # add a page with an image embed
    r = client.post("/api/v1/wikis/agent1/media-wiki/pages", json={
        "path": "wiki/page-with-image.md",
        "content": "---\ntitle: Image Test\nvisibility: public\n---\n\n# Image Test\n\n![[wiki/test.png]]\n\n![[wiki/doc.pdf]]\n",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201

    # write a fake PNG (1x1 pixel) directly to the repo
    import struct
    png_data = (
        b'\x89PNG\r\n\x1a\n'  # PNG signature
        + struct.pack('>I', 13) + b'IHDR' + struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
        + struct.pack('>I', 0x1D15C187)  # CRC (pre-computed for 1x1 RGB)
        + struct.pack('>I', 12) + b'IDAT' + b'\x08\xd7c\xf8\x0f\x00\x00\x01\x01\x00\x05'
        + struct.pack('>I', 0x1A2B3C4D)  # CRC placeholder
        + struct.pack('>I', 0) + b'IEND' + struct.pack('>I', 0xAE426082)
    )
    from app.git_sync import _git_bytes, _git, _repo_path, _AUTHOR_ENV
    import tempfile
    repo = _repo_path("agent1", "media-wiki")
    idx = tempfile.mktemp(prefix="wikihub-test-bin-", suffix=".idx")
    env = {"GIT_INDEX_FILE": idx}
    try:
        # read existing tree first
        existing = _git(repo, "ls-tree", "-r", "--name-only", "HEAD").strip().split("\n")
        for f in existing:
            if f:
                blob_info = _git(repo, "ls-tree", "HEAD", f).strip().split()
                if len(blob_info) >= 3:
                    _git(repo, "update-index", "--add", "--cacheinfo", blob_info[0].split()[0] if ' ' in blob_info[0] else "100644", blob_info[2], f, env=env)
        # add binary file
        blob = _git_bytes(repo, "hash-object", "-w", "--stdin", input=png_data, env=env).strip().decode()
        _git(repo, "update-index", "--add", "--cacheinfo", "100644", blob, "wiki/test.png", env=env)
        # add fake PDF
        pdf_data = b"%PDF-1.4 fake pdf content for testing"
        blob2 = _git_bytes(repo, "hash-object", "-w", "--stdin", input=pdf_data, env=env).strip().decode()
        _git(repo, "update-index", "--add", "--cacheinfo", "100644", blob2, "wiki/doc.pdf", env=env)
        tree = _git(repo, "write-tree", env=env)
        parent = _git(repo, "rev-parse", "HEAD")
        commit = _git(repo, "commit-tree", tree, "-p", parent, "-m", "Add test binary files", env={**env, **_AUTHOR_ENV})
        _git(repo, "update-ref", "refs/heads/main", commit)
    finally:
        if os.path.exists(idx):
            os.unlink(idx)

    # regenerate public mirror
    from app.wiki_ops import load_acl_rules
    from app.git_sync import regenerate_public_mirror
    acl_rules = load_acl_rules("agent1", "media-wiki")
    regenerate_public_mirror("agent1", "media-wiki", acl_rules)

    # serve image — should return PNG with correct content type
    r = client.get("/@agent1/media-wiki/wiki/test.png")
    assert r.status_code == 200, f"Expected 200 for PNG, got {r.status_code}"
    assert "image/png" in r.content_type
    assert r.data[:4] == b'\x89PNG'

    # serve PDF — should return PDF with correct content type
    r = client.get("/@agent1/media-wiki/wiki/doc.pdf")
    assert r.status_code == 200, f"Expected 200 for PDF, got {r.status_code}"
    assert "application/pdf" in r.content_type

    # non-existent file should 404
    r = client.get("/@agent1/media-wiki/wiki/nonexistent.png")
    assert r.status_code == 404

    # rendered page should contain img tag
    r = client.get("/@agent1/media-wiki/wiki/page-with-image")
    assert r.status_code == 200
    assert b'<img' in r.data
    assert b'wiki/test.png' in r.data
    # should contain PDF file link
    assert b'file-embed' in r.data
    assert b'wiki/doc.pdf' in r.data

    # plain text files (.txt) should serve inline as text/plain
    r = client.post("/api/v1/wikis/agent1/media-wiki/pages", json={
        "path": "wiki/notes.txt", "content": "raw notes\nline two\n", "visibility": "public",
    }, headers=h)
    assert r.status_code == 201
    r = client.get("/@agent1/media-wiki/wiki/notes.txt")
    assert r.status_code == 200, f"Expected 200 for .txt, got {r.status_code}"
    assert "text/plain" in r.content_type
    assert b"raw notes" in r.data
    # browser should display inline (no Content-Disposition: attachment)
    assert "attachment" not in r.headers.get("Content-Disposition", "")

    # unknown extensions should download (Content-Disposition: attachment, octet-stream)
    r = client.post("/api/v1/wikis/agent1/media-wiki/pages", json={
        "path": "wiki/data.weirdext", "content": "arbitrary bytes here", "visibility": "public",
    }, headers=h)
    assert r.status_code == 201
    r = client.get("/@agent1/media-wiki/wiki/data.weirdext")
    assert r.status_code == 200, f"Expected 200 for unknown ext, got {r.status_code}"
    assert "application/octet-stream" in r.content_type
    assert "attachment" in r.headers.get("Content-Disposition", "")
    assert b"arbitrary bytes here" in r.data

    # potentially-XSS-y extension (.html) must NOT be served as text/html — force download
    r = client.post("/api/v1/wikis/agent1/media-wiki/pages", json={
        "path": "wiki/evil.html", "content": "<script>alert(1)</script>", "visibility": "public",
    }, headers=h)
    assert r.status_code == 201
    r = client.get("/@agent1/media-wiki/wiki/evil.html")
    assert r.status_code == 200
    assert "application/octet-stream" in r.content_type, "XSS guard: .html must not be served inline"
    assert "attachment" in r.headers.get("Content-Disposition", "")

    # wikihub-6ag: owner opt-in via .wikihub/serve-inline serves matching .html
    # INLINE as text/html, hardened with a CSP sandbox + nosniff. Files not listed
    # keep the safe attachment default above.
    r = client.post("/api/v1/wikis/agent1/media-wiki/pages", json={
        "path": "wiki/deck.html",
        "content": "<!doctype html><h1>interactive deck</h1>",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201
    # Before opt-in: deck.html still downloads (default safe behavior).
    r = client.get("/@agent1/media-wiki/wiki/deck.html")
    assert "application/octet-stream" in r.content_type
    assert "attachment" in r.headers.get("Content-Disposition", "")
    # Owner writes the opt-in allowlist (a .wikihub/ plumbing file) and republishes.
    sync_page_to_repo("agent1", "media-wiki", ".wikihub/serve-inline", "wiki/deck.html\n")
    regenerate_public_mirror("agent1", "media-wiki", load_acl_rules("agent1", "media-wiki"))
    r = client.get("/@agent1/media-wiki/wiki/deck.html")
    assert r.status_code == 200
    assert r.content_type.startswith("text/html"), f"opted-in .html should be text/html, got {r.content_type}"
    assert "inline" in r.headers.get("Content-Disposition", "")
    csp = r.headers.get("Content-Security-Policy", "")
    assert "sandbox" in csp and "allow-scripts" in csp, f"opted-in .html must be CSP-sandboxed, got {csp!r}"
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert b"interactive deck" in r.data
    # The NON-opted .html (evil.html) is unaffected by the allowlist — still downloads.
    r = client.get("/@agent1/media-wiki/wiki/evil.html")
    assert "application/octet-stream" in r.content_type
    assert "attachment" in r.headers.get("Content-Disposition", "")

    # wikihub-0idv: non-md Page row with visibility=public must grant anon access
    # even when the file-path ACL is private. (Page visibility wins over ACL, matching
    # the markdown handler's behavior.)
    # "outside/" is not covered by the "wiki/** public" ACL rule, so it defaults to private.
    r = client.post("/api/v1/wikis/agent1/media-wiki/pages", json={
        "path": "outside/public-via-page.txt", "content": "visible via Page row",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201
    anon_client = client.application.test_client()
    r = anon_client.get("/@agent1/media-wiki/outside/public-via-page.txt")
    assert r.status_code == 200, f"Page.visibility=public should grant anon access, got {r.status_code}"
    assert b"visible via Page row" in r.data
    # negative case: same directory, but Page.visibility=private → blocked
    r = client.post("/api/v1/wikis/agent1/media-wiki/pages", json={
        "path": "outside/private-via-page.txt", "content": "secret",
        "visibility": "private",
    }, headers=h)
    assert r.status_code == 201
    # wikihub-dkp8: a private non-md Page row EXISTS, so anon gets a "restricted"
    # signal (403/401), not a bare 404. Content must still not leak.
    r = anon_client.get("/@agent1/media-wiki/outside/private-via-page.txt")
    assert r.status_code in (401, 403), f"Page.visibility=private should block anon access with restricted status, got {r.status_code}"
    assert b"secret" not in r.data, "restricted response must not leak file content"

    # Non-md Page rows must survive an index_repo_pages reset (regression: wikihub-0idv).
    # Previously index_repo_pages filtered out non-md, so any operation that triggered
    # reset=True (share/ACL change/fork) would silently delete .txt/.png Page rows.
    from app.wiki_ops import index_repo_pages
    from app.models import Wiki, Page
    wiki_obj = Wiki.query.filter_by(slug="media-wiki").first()
    assert Page.query.filter_by(wiki_id=wiki_obj.id, path="wiki/notes.txt").first() is not None
    index_repo_pages("agent1", "media-wiki", wiki_obj, reset=True)
    db.session.commit()
    txt_page = Page.query.filter_by(wiki_id=wiki_obj.id, path="wiki/notes.txt").first()
    assert txt_page is not None, "non-md Page row was wiped by index_repo_pages(reset=True)"
    png_page = Page.query.filter_by(wiki_id=wiki_obj.id, path="wiki/test.png").first()
    assert png_page is not None, "non-md Page row was wiped by index_repo_pages(reset=True)"


def test_unlisted_view_acl_default_readable_by_anon(client, api_key):
    """Issue #15: a wiki whose ACL default is `unlisted-view` must be viewable
    by anonymous link-holders on the web reader, and page creation must never
    persist the ACL-only token `unlisted-view` on a page (frontmatter or DB).

    Before the fix:
    - can_read() only recognized the old `unlisted` name, so pages resolving to
      the canonical `unlisted-view` grant 404'd for anon on the web view.
    - the create path wrote resolve_visibility()'s `unlisted-view` straight into
      page frontmatter/DB, leaking an ACL token as a page visibility.
    """
    from app.acl import can_read, can_write, normalize_page_visibility
    from app.git_sync import sync_page_to_repo
    from app.models import Page

    h = {"Authorization": f"Bearer {api_key}"}
    anon = client.application.test_client()

    # unit: canonical `unlisted-view` grant is anon-readable; the page enum
    # rejects/normalizes the ACL token.
    assert can_read("deals.md", [("*", "unlisted-view")], user=None) is True
    assert can_read("deals.md", [("*", "private")], user=None) is False
    assert can_write("deals.md", [], user=None, frontmatter_visibility="unlisted-edit") is True
    assert normalize_page_visibility("unlisted-view") == "unlisted"
    assert normalize_page_visibility("unlisted-edit") == "unlisted-edit"
    assert normalize_page_visibility("public-view") == "public"
    assert normalize_page_visibility("public-edit") == "public-edit"
    assert normalize_page_visibility("bogus") is None

    # wiki with an ACL default of unlisted-view (accessible by URL, not listed)
    r = client.post("/api/v1/wikis", json={"slug": "kb15", "title": "KB15"}, headers=h)
    assert r.status_code == 201
    sync_page_to_repo("agent1", "kb15", ".wikihub/acl", "* unlisted-view\n")

    # page created without frontmatter visibility inherits the wiki default
    r = client.post("/api/v1/wikis/agent1/kb15/pages", json={
        "path": "deals.md",
        "content": "# Deals\n\nlink-holders should see this.",
    }, headers=h)
    assert r.status_code == 201

    # stored visibility is the page enum, never the ACL token; no frontmatter leak
    r = client.get("/api/v1/wikis/agent1/kb15/pages/deals.md", headers=h)
    assert r.status_code == 200
    body = r.get_json()
    assert body["visibility"] == "unlisted", body["visibility"]
    assert "unlisted-view" not in (body.get("content") or "")

    r = client.post("/api/v1/wikis/agent1/kb15/pages", json={
        "path": "bogus.md",
        "content": "---\nvisibility: bogus\n---\n\n# Bogus\n\nfalls back to ACL.",
    }, headers=h)
    assert r.status_code == 201
    r = client.get("/api/v1/wikis/agent1/kb15/pages/bogus.md", headers=h)
    assert r.status_code == 200
    body = r.get_json()
    assert body["visibility"] == "unlisted", body["visibility"]

    # anonymous link-holder can view the page on the web reader
    r = anon.get("/@agent1/kb15/deals")
    assert r.status_code == 200, f"anon reader should be 200, got {r.status_code}"
    r = anon.get("/@agent1/kb15/bogus")
    assert r.status_code == 200, f"invalid frontmatter should fall back to ACL, got {r.status_code}"

    # legacy rows persisted with the raw ACL token must also read 200 for anon
    # (exercises the can_read normalization directly)
    page = Page.query.filter_by(path="deals.md").first()
    page.visibility = "unlisted-view"
    db.session.commit()
    r = anon.get("/@agent1/kb15/deals")
    assert r.status_code == 200, f"legacy unlisted-view row should be 200 for anon, got {r.status_code}"


def test_search(client, api_key):
    """full-text search returns results"""
    h = {"Authorization": f"Bearer {api_key}"}
    r = client.get("/api/v1/search?q=hello", headers=h)
    assert r.status_code == 200
    # result count depends on what's been created/deleted above — just verify the shape
    data = r.get_json()
    assert "results" in data
    assert "total" in data


def test_reader_owner_visibility_control(client, api_key):
    """owners get a direct page-visibility control on the reader surface."""
    h = {"Authorization": f"Bearer {api_key}"}

    r = client.post("/api/v1/wikis", json={"slug": "vis-ui", "title": "Visibility UI"}, headers=h)
    assert r.status_code == 201

    r = client.post("/api/v1/wikis/agent1/vis-ui/pages", json={
        "path": "wiki/page.md",
        "content": "---\ntitle: Visibility UI\nvisibility: public\n---\n\n# Visibility UI\n",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201

    r = client.get(f"/auth/login?api_key={api_key}&next=/", follow_redirects=False)
    assert r.status_code == 302

    r = client.get("/@agent1/vis-ui/wiki/page")
    assert r.status_code == 200
    assert b'id="page-vis-trigger"' in r.data
    assert b'id="page-vis-menu"' in r.data

    r = client.post("/api/v1/wikis/agent1/vis-ui/pages/wiki/page.md/visibility", json={
        "visibility": "private",
    }, headers=h)
    assert r.status_code == 200

    r = client.get("/api/v1/wikis/agent1/vis-ui/pages/wiki/page.md", headers=h)
    assert r.status_code == 200
    assert r.get_json()["visibility"] == "private"


def test_search_respects_acl_shares(client, api_key):
    """shared private pages appear in search for grantees, but not unrelated users."""
    h = {"Authorization": f"Bearer {api_key}"}
    anon_client = client.application.test_client()

    r = client.post("/api/v1/wikis", json={"slug": "search-share", "title": "Search Share"}, headers=h)
    assert r.status_code == 201

    r = client.post("/api/v1/accounts", json={"username": "searchguest"})
    assert r.status_code == 201
    guest_key = r.get_json()["api_key"]
    hg = {"Authorization": f"Bearer {guest_key}"}

    r = client.post("/api/v1/accounts", json={"username": "outsider"})
    assert r.status_code == 201
    outsider_key = r.get_json()["api_key"]
    ho = {"Authorization": f"Bearer {outsider_key}"}

    unique_term = "zephyrsearchneedle"
    r = client.post("/api/v1/wikis/agent1/search-share/pages", json={
        "path": "roadmap/secret-plan.md",
        "content": f"# Secret Plan\n\n{unique_term} lives here.",
        "visibility": "private",
    }, headers=h)
    assert r.status_code == 201

    r = client.get(f"/api/v1/search?q={unique_term}", headers=hg)
    assert r.status_code == 200
    guest_results = r.get_json()
    assert guest_results["total"] == 0

    r = client.post("/api/v1/wikis/agent1/search-share/share", json={
        "pattern": "roadmap/*",
        "username": "searchguest",
        "role": "read",
    }, headers=h)
    assert r.status_code == 200

    r = client.get(f"/api/v1/search?q={unique_term}", headers=hg)
    assert r.status_code == 200
    guest_results = r.get_json()
    assert any(
        row["wiki"] == "agent1/search-share" and row["page"] == "roadmap/secret-plan.md"
        for row in guest_results["results"]
    ), guest_results

    r = client.get(f"/api/v1/search?q={unique_term}", headers=ho)
    assert r.status_code == 200
    outsider_results = r.get_json()
    assert not any(row["page"] == "roadmap/secret-plan.md" for row in outsider_results["results"]), outsider_results

    r = anon_client.get(f"/api/v1/search?q={unique_term}")
    assert r.status_code == 200
    anon_results = r.get_json()
    assert not any(row["page"] == "roadmap/secret-plan.md" for row in anon_results["results"]), anon_results


def test_social(client, api_key):
    """star and fork a wiki"""
    h = {"Authorization": f"Bearer {api_key}"}

    # create a second user
    r = client.post("/api/v1/accounts", json={"username": "user2"})
    key2 = r.get_json()["api_key"]
    h2 = {"Authorization": f"Bearer {key2}"}

    # star
    r = client.post("/api/v1/wikis/agent1/test-wiki/star", headers=h2)
    assert r.status_code == 201

    # fork
    r = client.post("/api/v1/wikis/agent1/test-wiki/fork", headers=h2)
    assert r.status_code == 201
    assert r.get_json()["owner"] == "user2"

    # unstar
    r = client.delete("/api/v1/wikis/agent1/test-wiki/star", headers=h2)
    assert r.status_code == 200


def test_activity_feed_filters_private_and_shows_social_events(client, api_key):
    """Wiki activity renders recent page/social events without leaking private pages."""
    h = {"Authorization": f"Bearer {api_key}"}
    r = client.post("/api/v1/wikis", json={"slug": "activity-check", "title": "Activity Check"}, headers=h)
    assert r.status_code == 201

    r = client.post("/api/v1/wikis/agent1/activity-check/pages", json={
        "path": "wiki/public-activity.md",
        "content": "---\ntitle: Public Activity\nvisibility: public\n---\n\n# Public Activity\n",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201
    r = client.post("/api/v1/wikis/agent1/activity-check/pages", json={
        "path": "secrets/private-activity.md",
        "content": "---\ntitle: Private Activity\nvisibility: private\n---\n\n# Private Activity\n\nSecret feed item.",
        "visibility": "private",
    }, headers=h)
    assert r.status_code == 201

    r = client.post("/api/v1/accounts", json={"username": "activityfan"})
    assert r.status_code == 201
    fan_key = r.get_json()["api_key"]
    fan_h = {"Authorization": f"Bearer {fan_key}"}
    r = client.post("/api/v1/wikis/agent1/activity-check/star", headers=fan_h)
    assert r.status_code == 201
    r = client.post("/api/v1/wikis/agent1/activity-check/fork", headers=fan_h)
    assert r.status_code == 201

    anon = client.application.test_client()
    anon.get("/auth/logout")
    r = anon.get("/@agent1/activity-check/activity")
    assert r.status_code == 200, f"activity feed should be public when public pages exist, got {r.status_code}"
    html = r.get_data(as_text=True)
    assert "Activity" in html
    assert "Public Activity" in html
    assert "Wiki starred" in html
    assert "Wiki forked" in html
    assert "Private Activity" not in html
    assert "secrets/private-activity.md" not in html

    owner_browser = client.application.test_client()
    r = owner_browser.post("/auth/login", data={"api_key": api_key}, follow_redirects=False)
    assert r.status_code == 302
    r = owner_browser.get("/@agent1/activity-check/activity")
    assert r.status_code == 200
    owner_html = r.get_data(as_text=True)
    assert "Private Activity" in owner_html
    assert "secrets/private-activity.md" in owner_html

    r = owner_browser.get("/@agent1/activity-check/wiki/public-activity")
    assert r.status_code == 200
    assert b"/@agent1/activity-check/activity" in r.data
    owner_browser.get("/auth/logout")


def test_curator_sidebar_only_renders_when_usable(app, client, api_key):
    """Curator launcher should not appear for anon/disabled contexts."""
    h = {"Authorization": f"Bearer {api_key}"}
    r = client.post("/api/v1/wikis", json={"slug": "curator-ui", "title": "Curator UI"}, headers=h)
    assert r.status_code == 201
    r = client.post("/api/v1/wikis/agent1/curator-ui/pages", json={
        "path": "wiki/page.md",
        "content": "---\ntitle: Curator Page\nvisibility: public\n---\n\n# Curator Page\n",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201

    original = app.config.get("CURATOR_ENABLED", True)
    try:
        app.config["CURATOR_ENABLED"] = True
        anon = client.application.test_client()
        anon.get("/auth/logout")
        r = anon.get("/@agent1/curator-ui/wiki/page")
        assert r.status_code == 200
        assert b"class=\"curator-toggle\"" not in r.data

        browser = client.application.test_client()
        r = browser.post("/auth/login", data={"api_key": api_key}, follow_redirects=False)
        assert r.status_code == 302
        r = browser.get("/@agent1/curator-ui/wiki/page")
        assert r.status_code == 200
        assert b"class=\"curator-toggle\"" in r.data
        assert b"connect Claude or add an API key" in r.data

        app.config["CURATOR_ENABLED"] = False
        r = browser.get("/@agent1/curator-ui/wiki/page")
        assert r.status_code == 200
        assert b"class=\"curator-toggle\"" not in r.data
    finally:
        app.config["CURATOR_ENABLED"] = original


def test_zip_upload(client, api_key):
    """create wiki via zip upload"""
    # login via web
    client.post("/auth/signup", data={"username": "uploader", "password": "testpass123"})

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("wiki/page1.md", "---\nvisibility: public\n---\n# Page 1\n\nContent.")
    buf.seek(0)

    r = client.post("/new", data={
        "slug": "uploaded",
        "title": "Uploaded Wiki",
        "files": (buf, "wiki.zip"),
    }, content_type="multipart/form-data", follow_redirects=False)
    assert r.status_code == 302


def test_anonymous_upload(app):
    """POST /new-anonymous mints an ephemeral account + wiki from dropped files.
    Uses a fresh test_client AND explicitly logs out to bypass flask-login's
    request-context login cache that can leak from prior login-heavy tests."""
    from flask_login import logout_user
    client = app.test_client()
    with app.test_request_context():
        logout_user()

    buf = io.BytesIO(b"---\nvisibility: public\n---\n# anon page\n\nhello anon.\n")
    r = client.post("/new-anonymous", data={
        "slug": "anontest",
        "title": "Anon Test",
        "files": (buf, "anon.md"),
    }, content_type="multipart/form-data", follow_redirects=False)
    assert r.status_code == 201, f"expected 201, got {r.status_code}: {r.get_data(as_text=True)[:240]}"
    body = r.get_json()
    assert body["api_key"].startswith("wh_"), "api_key missing or malformed"
    assert body["username"].startswith("anon-"), f"username should start with anon-, got {body['username']}"
    assert body["wiki_url"].startswith(f"/@{body['username']}/anontest"), body["wiki_url"]
    assert "client_config" in body
    r2 = client.get(body["wiki_url"])
    assert r2.status_code == 200, f"anon wiki not reachable: {r2.status_code}"


def test_agent_surfaces(client):
    """all agent discovery endpoints respond"""
    for url in ["/llms.txt", "/AGENTS.md", "/agents", "/.well-known/mcp/server-card.json", "/.well-known/wikihub.json", "/mcp"]:
        r = client.get(url)
        assert r.status_code == 200, f"{url} returned {r.status_code}"


def test_a2hs_banner_gated_to_mobile(client):
    """regression (wikihub-2q0d): the add-to-home-screen banner must be gated on
    mobile — desktop Chrome/Edge fire beforeinstallprompt too, and the banner
    used to show there. The served base template must wire the mobile check
    into the beforeinstallprompt handler before showA2HSBanner() fires."""
    r = client.get("/explore")
    assert r.status_code == 200
    html = r.data.decode()
    assert "beforeinstallprompt" in html, "A2HS script missing from base template"
    assert "isMobileDevice" in html, "mobile gate missing from A2HS script"
    handler = html.split("beforeinstallprompt", 1)[1].split("showA2HSBanner()", 1)[0]
    assert "isMobileDevice()" in handler, "showA2HSBanner() not guarded by isMobileDevice()"


def test_token_and_settings(client):
    r = client.post("/auth/signup", data={"username": "webuser", "password": "testpass123"}, follow_redirects=False)
    assert r.status_code == 302

    r = client.post("/api/v1/auth/token", json={"username": "webuser", "password": "testpass123"})
    assert r.status_code == 200
    token = r.get_json()["api_key"]
    assert token.startswith("wh_")

    r = client.post("/auth/login", data={"api_key": token}, follow_redirects=False)
    assert r.status_code == 302

    r = client.get("/settings")
    assert r.status_code == 200
    assert b"Account Control Room" in r.data
    assert b"aria-label=\"Account menu\"" in r.data
    assert b"Open profile" in r.data
    assert b"/auth/logout" in r.data


def test_magic_link_login(client):
    r = client.post("/api/v1/accounts", json={"username": "lazyagent"})
    assert r.status_code == 201
    api_key = r.get_json()["api_key"]
    h = {"Authorization": f"Bearer {api_key}"}

    r = client.post("/api/v1/auth/magic-link", json={"next": "/settings"}, headers=h)
    assert r.status_code == 201
    data = r.get_json()
    assert "/auth/magic/" in data["login_url"]

    magic_path = urlparse(data["login_url"]).path
    browser = client.application.test_client()
    r = browser.get(magic_path, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/settings")

    r = browser.get("/settings")
    assert r.status_code == 200

    other_browser = client.application.test_client()
    r = other_browser.get(magic_path, follow_redirects=False)
    assert r.status_code == 302
    assert "/auth/login" in r.headers["Location"]


def test_signin_flow_redirects_back_to_target(app, client):
    """wikihub-kvwh: sign-in CTAs must round-trip back to the private target."""
    import app.routes.auth as auth_routes
    from flask import g as _g
    from flask import redirect

    target_path = "/@signinowner/private-wiki/notes/deep-secret"
    encoded_target = quote(target_path, safe="/")
    google_next = target_path

    r = client.post("/api/v1/accounts", json={
        "username": "signinowner",
        "email": "signinowner@example.com",
        "password": "testpass12345",
    })
    assert r.status_code == 201
    api_key = r.get_json()["api_key"]
    h = {"Authorization": f"Bearer {api_key}"}

    owner = db.session.execute(text("SELECT id FROM users WHERE username = 'signinowner'")).scalar_one()
    db.session.execute(
        text(
            "UPDATE users SET email_verified_at = NOW() WHERE id = :user_id"
        ),
        {"user_id": owner},
    )
    db.session.commit()

    r = client.post("/api/v1/wikis", json={"slug": "private-wiki", "title": "Private Wiki"}, headers=h)
    assert r.status_code == 201
    r = client.post("/api/v1/wikis/signinowner/private-wiki/pages", json={
        "path": "notes/deep-secret.md",
        "content": "# Deep Secret\n\nPrivate content.",
        "visibility": "private",
    }, headers=h)
    assert r.status_code == 201

    _g.pop("_login_user", None)
    anon = app.test_client()
    r = anon.get(target_path)
    # wikihub-dkp8: existing-but-private page → restricted (403); the sign-in CTA
    # must still round-trip back to the requested target.
    assert r.status_code == 403
    assert b"This page is restricted" in r.data
    assert f'/auth/login?next={encoded_target}'.encode() in r.data

    r = anon.get("/@signinowner/private-wiki/settings")
    assert r.status_code == 401
    assert b"Sign in" in r.data

    login_path = f"/auth/login?next={encoded_target}"
    login_page = anon.get(login_path)
    assert login_page.status_code == 200
    assert f'name="next" value="{target_path}"'.encode() in login_page.data
    assert f'/auth/google?next={google_next}'.encode() in login_page.data

    _g.pop("_login_user", None)
    password_browser = app.test_client()
    r = password_browser.post("/auth/login", data={
        "username": "signinowner",
        "password": "testpass12345",
        "next": target_path,
    }, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["Location"].endswith(target_path)
    r = password_browser.get(target_path)
    assert r.status_code == 200
    assert b"Deep Secret" in r.data

    _g.pop("_login_user", None)
    api_key_browser = app.test_client()
    r = api_key_browser.post("/auth/login", data={
        "api_key": api_key,
        "next": target_path,
    }, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["Location"].endswith(target_path)
    r = api_key_browser.get(target_path)
    assert r.status_code == 200
    assert b"Deep Secret" in r.data

    class FakeGoogleClient:
        def authorize_redirect(self, redirect_uri):
            return redirect(
                f"https://accounts.google.test/o/oauth2/auth?state=signin-flow-state&redirect_uri={redirect_uri}"
            )

        def authorize_access_token(self):
            return {
                "userinfo": {
                    "sub": "google-sub-signin-flow",
                    "email": "signinowner@example.com",
                    "email_verified": True,
                    "name": "Signin Owner",
                }
            }

    try:
        original_google = auth_routes.oauth.google
        had_original = True
    except AttributeError:
        original_google = None
        had_original = False

    auth_routes.oauth.google = FakeGoogleClient()
    try:
        _g.pop("_login_user", None)
        google_browser = app.test_client()
        r = google_browser.get(login_path)
        assert r.status_code == 200
        assert f'/auth/google?next={google_next}'.encode() in r.data

        r = google_browser.get(f"/auth/google?next={google_next}", follow_redirects=False)
        assert r.status_code == 302
        assert "state=signin-flow-state" in r.headers["Location"]

        with google_browser.session_transaction() as sess:
            pending_contexts = sess.get("google_oauth_contexts", {})
            assert pending_contexts["signin-flow-state"]["next"] == target_path

        r = google_browser.get("/auth/google/callback?state=signin-flow-state&code=fake", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["Location"].endswith(target_path)
        r = google_browser.get(target_path)
        assert r.status_code == 200
        assert b"Deep Secret" in r.data
    finally:
        if had_original:
            auth_routes.oauth.google = original_google
        else:
            delattr(auth_routes.oauth, "google")

    _g.pop("_login_user", None)
    magic_browser = app.test_client()
    r = client.post("/api/v1/auth/magic-link", json={"next": target_path}, headers=h)
    assert r.status_code == 201
    magic_path = urlparse(r.get_json()["login_url"]).path
    r = magic_browser.get(magic_path, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["Location"].endswith(target_path)
    r = magic_browser.get(target_path)
    assert r.status_code == 200
    assert b"Deep Secret" in r.data


def test_logout(client):
    """/auth/logout clears the session; login-required pages then redirect to /auth/login.

    Covers wikihub-uq9 by locking in the HTTP contract — the route + UI links
    were already implemented; this test prevents silent regressions."""
    r = client.post("/api/v1/accounts", json={"username": "logoutuser"})
    assert r.status_code == 201
    api_key = r.get_json()["api_key"]

    h = {"Authorization": f"Bearer {api_key}"}
    r = client.post("/api/v1/auth/magic-link", json={"next": "/settings"}, headers=h)
    magic_path = urlparse(r.get_json()["login_url"]).path
    browser = client.application.test_client()
    r = browser.get(magic_path, follow_redirects=False)
    assert r.status_code == 302

    r = browser.get("/settings")
    assert r.status_code == 200, f"expected signed-in /settings=200, got {r.status_code}"

    r = browser.get("/auth/logout", follow_redirects=False)
    assert r.status_code == 302, f"expected logout 302, got {r.status_code}"
    assert "/auth/login" not in r.headers.get("Location", "")

    r = browser.get("/settings", follow_redirects=False)
    assert r.status_code == 302
    assert "/auth/login" in r.headers["Location"], f"expected login redirect after logout, got {r.headers.get('Location')}"


def test_google_auto_link_security(app):
    """wikihub-ks5t.4: Google OAuth must NOT auto-link to a candidate whose
    email is unverified, otherwise an attacker can claim someone else's email
    as unverified and harvest their future Google sign-in."""
    from app.models import User
    from app.routes.auth import _resolve_or_create_google_user
    from app.auth_utils import hash_password

    with app.app_context():
        # Attack setup: Alice claims victim@example.com as her email, unverified.
        alice = User(
            username="alice-attacker",
            email="victim@example.com",
            password_hash=hash_password("alice-secret-pw"),
            email_verified_at=None,
        )
        db.session.add(alice)
        db.session.commit()
        alice_id = alice.id

        # Victim signs in with Google; Google asserts email_verified=true.
        # Expected: a NEW account is created for Victim; Alice's google_id is
        # NOT set; Alice's account is unaffected.
        victim_user = _resolve_or_create_google_user(
            google_id="google-sub-victim",
            email="victim@example.com",
            email_verified=True,
            name="Victim Real",
        )
        db.session.commit()

        assert victim_user.id != alice_id, "must not auto-link into Alice's account"
        assert victim_user.google_id == "google-sub-victim"
        assert victim_user.email == "victim@example.com"
        assert victim_user.email_verified_at is not None, "new Google account should be verified"

        # Alice's account must remain password-only, no google_id linked.
        alice_after = db.session.get(User, alice_id)
        assert alice_after.google_id is None
        assert alice_after.email == "victim@example.com"
        assert alice_after.email_verified_at is None

        # --- Positive case: candidate has VERIFIED email AND Google says verified → auto-link ---
        bob = User(
            username="bob-legit",
            email="bob@example.com",
            password_hash=hash_password("bob-password"),
            email_verified_at=utcnow(),
        )
        db.session.add(bob)
        db.session.commit()
        bob_id = bob.id

        linked = _resolve_or_create_google_user(
            google_id="google-sub-bob",
            email="bob@example.com",
            email_verified=True,
            name="Bob Legit",
        )
        db.session.commit()
        assert linked.id == bob_id, "verified candidate + verified Google → auto-link expected"
        assert linked.google_id == "google-sub-bob"

        # --- Negative case: Google reports email_verified=false → no auto-link even if candidate verified ---
        carol = User(
            username="carol-verified",
            email="carol@example.com",
            password_hash=hash_password("carol-password"),
            email_verified_at=utcnow(),
        )
        db.session.add(carol)
        db.session.commit()
        carol_id = carol.id

        new_user = _resolve_or_create_google_user(
            google_id="google-sub-carol-untrusted",
            email="carol@example.com",
            email_verified=False,
            name="Carol",
        )
        db.session.commit()
        assert new_user.id != carol_id, "Google email_verified=false must not trigger auto-link"

        carol_after = db.session.get(User, carol_id)
        assert carol_after.google_id is None


def test_google_oauth_preserves_next_and_invite_context(app, client, api_key):
    """wikihub-gtrq: Google OAuth must carry next + invite token through the callback."""
    import app.routes.auth as auth_routes
    from flask import redirect
    from app.models import PendingInvite, User

    h = {"Authorization": f"Bearer {api_key}"}

    r = client.post("/api/v1/wikis", json={"slug": "oauth-invite-test", "title": "OAuth Invite Test"}, headers=h)
    assert r.status_code == 201
    r = client.post("/api/v1/wikis/agent1/oauth-invite-test/pages", json={
        "path": "secret.md", "content": "# oauth secret", "visibility": "private",
    }, headers=h)
    assert r.status_code == 201
    r = client.post("/api/v1/wikis/agent1/oauth-invite-test/share", json={
        "pattern": "*", "email": "oauth-invite@example.com", "role": "read",
    }, headers=h)
    assert r.status_code == 200

    pending = PendingInvite.query.filter_by(email="oauth-invite@example.com").first()
    assert pending and pending.token

    signup_page = client.get(
        f"/auth/signup?next=/shared&email=oauth-invite@example.com&it={pending.token}"
    )
    assert signup_page.status_code == 200
    signup_html = signup_page.get_data(as_text=True)
    assert f'/auth/google?next=/shared&amp;email=oauth-invite@example.com&amp;it={pending.token}' in signup_html

    login_page = client.get(
        f"/auth/login?next=/shared&email=oauth-invite@example.com&it={pending.token}"
    )
    assert login_page.status_code == 200
    login_html = login_page.get_data(as_text=True)
    assert f'/auth/google?next=/shared&amp;email=oauth-invite@example.com&amp;it={pending.token}' in login_html

    class FakeGoogleClient:
        def authorize_redirect(self, redirect_uri):
            return redirect(f"https://accounts.google.test/o/oauth2/auth?state=fake-google-state&redirect_uri={redirect_uri}")

        def authorize_access_token(self):
            return {
                "userinfo": {
                    "sub": "google-sub-oauth-invite",
                    "email": "oauth-invite@example.com",
                    "email_verified": False,
                    "name": "OAuth Invite User",
                }
            }

    try:
        original_google = auth_routes.oauth.google
        had_original = True
    except AttributeError:
        original_google = None
        had_original = False

    auth_routes.oauth.google = FakeGoogleClient()
    try:
        browser = app.test_client()
        r = browser.get(
            f"/auth/google?next=/shared&email=oauth-invite@example.com&it={pending.token}",
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "state=fake-google-state" in r.headers["Location"]

        with browser.session_transaction() as sess:
            pending_contexts = sess.get("google_oauth_contexts", {})
            assert pending_contexts["fake-google-state"]["next"] == "/shared"
            assert pending_contexts["fake-google-state"]["email"] == "oauth-invite@example.com"
            assert pending_contexts["fake-google-state"]["it"] == pending.token

        r = browser.get("/auth/google/callback?state=fake-google-state&code=fake", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/shared"), r.headers["Location"]

        with browser.session_transaction() as sess:
            assert "google_oauth_contexts" not in sess

        user = User.query.filter_by(google_id="google-sub-oauth-invite").first()
        assert user is not None
        assert user.email == "oauth-invite@example.com"
        assert user.email_verified_at is not None, "invite token should verify the Google-created account"
        assert PendingInvite.query.filter_by(email="oauth-invite@example.com").count() == 0

        r = browser.get("/@agent1/oauth-invite-test/secret")
        assert r.status_code == 200
        assert b"oauth secret" in r.data
    finally:
        if had_original:
            auth_routes.oauth.google = original_google
        else:
            delattr(auth_routes.oauth, "google")


def test_sidebar_json_preserves_current_path_and_acl_shares(app, client, api_key):
    """wikihub-oud7 + wikihub-aozp: async sidebar keeps current branch and ACL-shared pages."""
    import app.routes.wiki as wiki_routes

    h = {"Authorization": f"Bearer {api_key}"}

    r = client.post("/api/v1/accounts", json={"username": "sideguest", "password": "testpass12345"})
    assert r.status_code == 201

    r = client.post("/api/v1/wikis", json={"slug": "async-share", "title": "Async Share"}, headers=h)
    assert r.status_code == 201
    r = client.post("/api/v1/wikis/agent1/async-share/pages", json={
        "path": "welcome.md",
        "content": "---\ntitle: Welcome\nvisibility: public\n---\n\n# Welcome",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201
    r = client.post("/api/v1/wikis/agent1/async-share/pages", json={
        "path": "team/secret.md",
        "content": "---\ntitle: Secret\nvisibility: private\n---\n\n# Secret",
        "visibility": "private",
    }, headers=h)
    assert r.status_code == 201
    r = client.post("/api/v1/wikis/agent1/async-share/share", json={
        "pattern": "team/*", "username": "sideguest", "role": "read",
    }, headers=h)
    assert r.status_code == 200

    browser = app.test_client()
    login = browser.post(
        "/auth/login",
        data={"username": "sideguest", "password": "testpass12345"},
        follow_redirects=False,
    )
    assert login.status_code == 302

    original_threshold = wiki_routes.SIDEBAR_ASYNC_THRESHOLD
    wiki_routes.SIDEBAR_ASYNC_THRESHOLD = 1
    try:
        r = browser.get("/@agent1/async-share/sidebar.json?current=team/secret.md")
        assert r.status_code == 200, f"sidebar.json fetch failed: {r.status_code} {r.data[:200]}"
        tree = r.get_json()
    finally:
        wiki_routes.SIDEBAR_ASYNC_THRESHOLD = original_threshold

    def find_item(items, path):
        for item in items:
            if item.get("path") == path:
                return item
            found = find_item(item.get("children") or [], path)
            if found:
                return found
        return None

    welcome = find_item(tree, "welcome.md")
    assert welcome is not None, "public page should still appear in async sidebar"

    team = find_item(tree, "team")
    assert team is not None, "shared folder should appear in collaborator async sidebar"
    assert team["active"] is True, "folder containing current page should be marked active"
    assert team["ancestor_of_current"] is True, "folder should be marked as ancestor of current page"

    secret = find_item(tree, "team/secret.md")
    assert secret is not None, "ACL-shared private page should appear in collaborator async sidebar"
    assert secret["current"] is True, "current shared page should be marked current in async sidebar JSON"


def test_unlisted_page_in_sidebar_but_not_discovery(app, client, api_key):
    """wikihub #17: 'unlisted' governs DISCOVERY surfaces (search/explore/profile),
    NOT in-wiki navigation. An anonymous viewer who possesses the wiki link and can
    read a page (unlisted is readable-by-URL) must see it in the wiki's own sidebar
    page tree. Both directions are asserted:

      (A) fix     — anon sidebar of the wiki LISTS the unlisted page.
      (B) guard   — search still EXCLUDES the unlisted page (discovery surface).

    Regression: before the fix, _visible_files filtered the public-mirror sidebar
    by is_discoverable (public/public-edit only), so unlisted pages rendered by
    direct link but never appeared in nav.
    """
    h = {"Authorization": f"Bearer {api_key}"}

    # wiki creation auto-provisions a public index.md, so the wiki is reachable.
    r = client.post("/api/v1/wikis", json={"slug": "unlisted-nav", "title": "Unlisted Nav"}, headers=h)
    assert r.status_code == 201, f"setup wiki create failed: {r.status_code} {r.data[:200]}"

    # an unlisted page carrying a unique token
    r = client.post("/api/v1/wikis/agent1/unlisted-nav/pages", json={
        "path": "rules.md",
        "content": "---\ntitle: Rules\nvisibility: unlisted\n---\n\n# Rules\n\nZorptangle marker.",
        "visibility": "unlisted",
    }, headers=h)
    assert r.status_code == 201, f"setup page create failed: {r.status_code} {r.data[:200]}"

    anon = app.test_client()  # logged-out viewer

    # unlisted page is readable by URL (the can_read premise the sidebar must honor)
    r = anon.get("/@agent1/unlisted-nav/rules")
    assert r.status_code == 200, f"unlisted page must be readable by direct link, got {r.status_code}"

    # (A) unlisted page appears in the anonymous sidebar page tree
    r = anon.get("/@agent1/unlisted-nav/sidebar.json")
    assert r.status_code == 200, f"sidebar.json fetch failed: {r.status_code} {r.data[:200]}"

    def find_item(items, path):
        for item in items:
            if item.get("path") == path:
                return item
            found = find_item(item.get("children") or [], path)
            if found:
                return found
        return None

    tree = r.get_json()
    assert find_item(tree, "rules.md") is not None, \
        "unlisted page must appear in the anon sidebar of its own wiki (wikihub #17)"

    # (B) but the unlisted page stays out of discovery: anonymous search excludes it
    r = anon.get("/api/v1/search?q=Zorptangle")
    assert r.status_code == 200, f"search failed: {r.status_code} {r.data[:200]}"
    hits = r.get_json()["results"]
    assert all(hit.get("page") != "rules.md" for hit in hits), \
        "unlisted page must NOT appear in search (discovery surface stays excluded)"


def test_acl_file_updates_reindex_inherited_visibility_without_discovery_leaks(app, client, api_key):
    """GroupBrain regression: scaffolded index/log rows started private, then a
    generic `.wikihub/acl` write changed the repo default to unlisted without
    re-indexing Page.visibility. Direct-link readers saw a restricted page even
    though the ACL and public mirror allowed it.
    """
    from app.git_sync import read_file_from_repo, sync_page_to_repo
    from app.models import Page, Wiki

    h = {"Authorization": f"Bearer {api_key}"}
    anon = app.test_client()
    slug = "groupbrain-acl-regression"
    title = "GroupBrain Visibility Regression"

    r = client.post("/api/v1/wikis", json={"slug": slug, "title": title}, headers=h)
    assert r.status_code == 201, f"setup wiki create failed: {r.status_code} {r.data[:200]}"

    wiki = Wiki.query.join(User, Wiki.owner_id == User.id).filter(User.username == "agent1", Wiki.slug == slug).first()
    assert wiki is not None
    wiki_id = wiki.id

    def visibility_for(path):
        db.session.expire_all()
        page = Page.query.filter_by(wiki_id=wiki_id, path=path).first()
        return page.visibility if page else None

    def page_snapshot(path):
        db.session.expire_all()
        page = Page.query.filter_by(wiki_id=wiki_id, path=path).first()
        assert page is not None
        return {
            "id": page.id,
            "author": page.author,
            "anonymous": page.anonymous,
            "claimable": page.claimable,
            "created_at": page.created_at,
            "visibility": page.visibility,
        }

    assert visibility_for("index.md") == "private"
    assert visibility_for("log.md") == "private"
    assert Page.query.filter_by(wiki_id=wiki_id, path=".wikihub/acl").first() is None

    r = client.post(f"/api/v1/wikis/agent1/{slug}/pages", json={
        "path": ".wikihub/acl",
        "content": "* unlisted-view\n",
    }, headers=h)
    assert r.status_code == 201, f"generic ACL write failed: {r.status_code} {r.data[:200]}"
    assert r.get_json()["reindexed"] is True
    assert read_file_from_repo("agent1", slug, ".wikihub/acl") == "* unlisted-view\n"
    assert Page.query.filter_by(wiki_id=wiki_id, path=".wikihub/acl").first() is None, \
        ".wikihub/acl must stay plumbing, not an indexed page"
    assert visibility_for("index.md") == "unlisted"
    assert visibility_for("log.md") == "unlisted"
    assert visibility_for("index.md") != "unlisted-view"

    log_title = "GBVisibility Log Marker"
    r = client.put(f"/api/v1/wikis/agent1/{slug}/pages/log.md", json={
        "content": f"---\ntitle: {log_title}\n---\n\n# Log\n\ncontent-only update after ACL reindex.",
    }, headers=h)
    assert r.status_code == 200, f"log content update failed: {r.status_code} {r.data[:200]}"
    assert r.get_json()["visibility"] == "unlisted", "content-only PUT must preserve inherited unlisted visibility"

    topics_title = "GBVisibility Topics Marker"
    r = client.post(f"/api/v1/wikis/agent1/{slug}/pages", json={
        "path": "topics.md",
        "content": f"---\ntitle: {topics_title}\n---\n\n# Topics\n\ncomparison page.",
    }, headers=h)
    assert r.status_code == 201, f"topics create failed: {r.status_code} {r.data[:200]}"
    assert r.get_json()["visibility"] == "unlisted"

    rename_title = "GBVisibility Rename Marker"
    r = client.post(f"/api/v1/wikis/agent1/{slug}/pages", json={
        "path": "rename-source.md",
        "content": f"---\ntitle: {rename_title}\n---\n\n# Rename Source\n\nrename coverage.",
    }, headers=h)
    assert r.status_code == 201, f"rename source create failed: {r.status_code} {r.data[:200]}"
    r = client.patch(f"/api/v1/wikis/agent1/{slug}/pages/rename-source.md", json={
        "new_path": "rename-target.md",
    }, headers=h)
    assert r.status_code == 200, f"rename under ACL default failed: {r.status_code} {r.data[:200]}"
    assert r.get_json()["visibility"] == "unlisted"
    assert visibility_for("rename-target.md") == "unlisted"
    assert visibility_for("rename-target.md") != "unlisted-view"

    claimable_title = "GBVisibility Claimable Marker"
    r = client.post(f"/api/v1/wikis/agent1/{slug}/pages", json={
        "path": "claimable.md",
        "content": f"---\ntitle: {claimable_title}\n---\n\n# Claimable\n\nanonymous claimable page.",
        "anonymous": True,
        "claimable": True,
    }, headers=h)
    assert r.status_code == 201, f"claimable create failed: {r.status_code} {r.data[:200]}"
    claimable_before_replace = page_snapshot("claimable.md")
    assert claimable_before_replace["anonymous"] is True
    assert claimable_before_replace["claimable"] is True

    private_title = "GBVisibility Owner Private Marker"
    r = client.post(f"/api/v1/wikis/agent1/{slug}/pages", json={
        "path": "owner-control.md",
        "content": (
            f"---\ntitle: {private_title}\nvisibility: private\n---\n\n"
            "# Owner control\n\nexplicit private frontmatter remains private."
        ),
    }, headers=h)
    assert r.status_code == 201, f"private page create failed: {r.status_code} {r.data[:200]}"
    assert r.get_json()["visibility"] == "private"
    log_before_replace = page_snapshot("log.md")

    r = client.put(f"/api/v1/wikis/agent1/{slug}/pages/.wikihub/acl", json={
        "content": "* unlisted-view\n",
    }, headers=h)
    assert r.status_code == 200, f"generic ACL replace failed: {r.status_code} {r.data[:200]}"
    assert visibility_for("log.md") == "unlisted"
    assert visibility_for("topics.md") == "unlisted"
    assert visibility_for("claimable.md") == "unlisted"
    assert visibility_for("owner-control.md") == "private", "explicit private frontmatter must win over ACL"
    log_after_replace = page_snapshot("log.md")
    claimable_after_replace = page_snapshot("claimable.md")
    assert log_after_replace["id"] == log_before_replace["id"]
    assert log_after_replace["author"] == log_before_replace["author"]
    assert log_after_replace["anonymous"] == log_before_replace["anonymous"]
    assert log_after_replace["claimable"] == log_before_replace["claimable"]
    assert log_after_replace["created_at"] == log_before_replace["created_at"]
    assert claimable_after_replace["id"] == claimable_before_replace["id"]
    assert claimable_after_replace["author"] == claimable_before_replace["author"]
    assert claimable_after_replace["anonymous"] is True
    assert claimable_after_replace["claimable"] is True
    assert claimable_after_replace["created_at"] == claimable_before_replace["created_at"]
    assert read_file_from_repo("agent1", slug, ".wikihub/acl", public=True) is None
    assert read_file_from_repo("agent1", slug, ".wikihub/events.jsonl", public=True) is None

    r = anon.get(f"/@agent1/{slug}/.wikihub/acl")
    assert r.status_code == 404, f"direct ACL route must not serve plumbing, got {r.status_code}"
    r = anon.get(f"/@agent1/{slug}/.wikihub/events.jsonl")
    assert r.status_code == 404, f"direct event-log route must not serve plumbing, got {r.status_code}"

    plumbing_marker = "GBVisibility Plumbing Leak Marker"
    r = client.post(f"/api/v1/wikis/agent1/{slug}/pages", json={
        "path": ".wikihub/events.jsonl",
        "content": plumbing_marker,
    }, headers=h)
    assert r.status_code == 400, f"generic plumbing create must be rejected, got {r.status_code}"
    r = client.put(f"/api/v1/wikis/agent1/{slug}/pages/.wikihub/serve-inline", json={
        "content": "log.md\n",
    }, headers=h)
    assert r.status_code == 400, f"generic plumbing replace must be rejected, got {r.status_code}"
    r = client.patch(f"/api/v1/wikis/agent1/{slug}/pages/.wikihub/serve-inline", json={
        "content": "topics.md\n",
    }, headers=h)
    assert r.status_code == 400, f"generic plumbing patch must be rejected, got {r.status_code}"
    r = client.patch(f"/api/v1/wikis/agent1/{slug}/pages/log.md", json={
        "new_path": ".wikihub/serve-inline",
    }, headers=h)
    assert r.status_code == 400, f"renaming a page into plumbing must be rejected, got {r.status_code}"
    r = client.post(f"/api/v1/wikis/agent1/{slug}/pages/.wikihub/serve-inline/visibility", json={
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 400, f"generic plumbing visibility update must be rejected, got {r.status_code}"
    r = client.delete(f"/api/v1/wikis/agent1/{slug}/pages/.wikihub/serve-inline", headers=h)
    assert r.status_code == 400, f"generic plumbing delete must be rejected, got {r.status_code}"
    assert Page.query.filter_by(wiki_id=wiki_id, path=".wikihub/events.jsonl").first() is None
    assert Page.query.filter_by(wiki_id=wiki_id, path=".wikihub/serve-inline").first() is None
    r = client.get(f"/api/v1/wikis/agent1/{slug}/pages", headers=h)
    assert r.status_code == 200
    assert all(not page["path"].startswith(".wikihub/") for page in r.get_json()["pages"])
    r = anon.get(f"/api/v1/search?q={plumbing_marker}")
    assert r.status_code == 200
    assert r.get_json()["results"] == [], "rejected plumbing writes must not become searchable"

    import runpy
    hook = runpy.run_path(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks", "post-receive"))
    assert hook["is_wikihub_plumbing_path"](".wikihub/serve-inline.md") is True
    assert hook["is_wikihub_plumbing_path"]("wiki/serve-inline.md") is False

    admin_headers = {"Authorization": "Bearer test-admin-token"}
    admin_plumbing_marker = "GBVisibility Admin Plumbing Leak Marker"
    r = client.post("/api/v1/admin/sync-page", json={
        "username": "agent1",
        "slug": slug,
        "path": ".wikihub/serve-inline.md",
        "title": "Serve Inline Plumbing",
        "visibility": "public",
        "content": admin_plumbing_marker,
        "frontmatter": {"title": "Serve Inline Plumbing"},
    }, headers=admin_headers)
    assert r.status_code == 204, f"admin plumbing sync must be ignored, got {r.status_code}"
    assert Page.query.filter_by(wiki_id=wiki_id, path=".wikihub/serve-inline.md").first() is None
    r = client.post("/api/v1/admin/delete-page", json={
        "username": "agent1",
        "slug": slug,
        "path": ".wikihub/serve-inline.md",
    }, headers=admin_headers)
    assert r.status_code == 204, f"admin plumbing delete must be ignored, got {r.status_code}"
    r = anon.get(f"/api/v1/search?q={admin_plumbing_marker}")
    assert r.status_code == 200
    assert r.get_json()["results"] == [], "admin plumbing sync must not become searchable"

    from app.wiki_ops import refresh_wikilinks_for_page, update_page_metadata
    stale_marker = "GBVisibility Stale Plumbing Marker"
    stale_page = Page(
        wiki_id=wiki_id,
        path=".wikihub/serve-inline.md",
        title=stale_marker,
        visibility="public",
        frontmatter_json={"title": stale_marker, "tags": ["plumbing-leak"]},
        excerpt=stale_marker,
    )
    stale_content = f"---\ntitle: {stale_marker}\ntags: [plumbing-leak]\n---\n\n# {stale_marker}\n\n[[log]]"
    update_page_metadata(stale_page, stale_content)
    db.session.add(stale_page)
    db.session.flush()
    refresh_wikilinks_for_page(stale_page, stale_content)
    db.session.commit()
    assert Page.query.filter_by(wiki_id=wiki_id, path=".wikihub/serve-inline.md").first() is not None

    r = anon.get(f"/@agent1/{slug}/.wikihub/serve-inline")
    assert r.status_code == 404, f"stale plumbing web route must not render, got {r.status_code}"
    r = anon.get(f"/api/v1/wikis/agent1/{slug}/pages/.wikihub/serve-inline.md")
    assert r.status_code == 404, f"stale plumbing API read must not render, got {r.status_code}"
    r = anon.get(f"/api/wikis/agent1/{slug}/pages/.wikihub/serve-inline.md")
    assert r.status_code == 404, f"stale plumbing compat API read must not render, got {r.status_code}"
    r = anon.get(f"/@agent1/{slug}/activity")
    assert r.status_code == 200
    assert stale_marker not in r.data.decode("utf-8", errors="replace")
    r = anon.get(f"/@agent1/{slug}/graph.json")
    assert r.status_code == 200
    graph = r.get_json()
    assert all(".wikihub/" not in node.get("url", "") and node.get("title") != stale_marker for node in graph.get("nodes", []))
    r = anon.get(f"/@agent1/{slug}/tag/plumbing-leak")
    assert r.status_code == 200
    assert stale_marker not in r.data.decode("utf-8", errors="replace")
    r = anon.get(f"/@agent1/{slug}/sidebar.json")
    assert r.status_code == 200
    assert ".wikihub" not in json.dumps(r.get_json())
    r = anon.get(f"/@agent1/{slug}/llms.txt")
    assert r.status_code == 200
    assert stale_marker not in r.data.decode("utf-8", errors="replace")
    r = anon.get(f"/api/v1/search?q={stale_marker}")
    assert r.status_code == 200
    assert r.get_json()["results"] == [], "stale plumbing row must not become searchable"

    r = anon.get(f"/@agent1/{slug}/log")
    assert r.status_code == 200, f"anonymous /log direct link should be 200, got {r.status_code}"
    assert log_title.encode() in r.data
    r = anon.get(f"/@agent1/{slug}/topics")
    assert r.status_code == 200, f"anonymous /topics direct link should be 200, got {r.status_code}"
    assert topics_title.encode() in r.data

    r = anon.get(f"/@agent1/{slug}/owner-control")
    assert r.status_code in (401, 403), f"private page should be restricted, got {r.status_code}"
    assert private_title.encode() not in r.data

    for query, leaked_page in ((log_title, "log.md"), (topics_title, "topics.md"), (claimable_title, "claimable.md"), (private_title, "owner-control.md")):
        r = anon.get(f"/api/v1/search?q={query}")
        assert r.status_code == 200, f"search failed: {r.status_code} {r.data[:200]}"
        assert all(hit.get("wiki") != f"agent1/{slug}" or hit.get("page") != leaked_page for hit in r.get_json()["results"]), \
            f"{leaked_page} must not appear in anonymous search"

    for path in ("/@agent1", "/explore"):
        r = anon.get(path)
        assert r.status_code == 200, f"{path} failed: {r.status_code}"
        body = r.data.decode("utf-8", errors="replace")
        assert title not in body, f"{path} must not list an unlisted-only wiki"
        assert log_title not in body and topics_title not in body and claimable_title not in body and private_title not in body

    r = anon.get(f"/api/v1/wikis?owner=agent1&q={title}")
    assert r.status_code == 200
    assert r.get_json()["total"] == 0, "public wiki listing API must exclude unlisted-only wiki"

    other_account = client.post("/api/v1/accounts", json={"username": "aclother"}).get_json()
    other_h = {"Authorization": f"Bearer {other_account['api_key']}"}
    other_title = "GBVisibility Other Private Marker"
    r = client.post("/api/v1/wikis", json={"slug": "private-silo", "title": "Private Silo"}, headers=other_h)
    assert r.status_code == 201
    r = client.post("/api/v1/wikis/aclother/private-silo/pages", json={
        "path": "secret.md",
        "content": f"---\ntitle: {other_title}\n---\n\n# Secret\n\nprivate across another wiki.",
    }, headers=other_h)
    assert r.status_code == 201

    r = anon.get("/@aclother/private-silo/secret")
    assert r.status_code in (401, 403), f"other user's private page should be restricted, got {r.status_code}"
    assert other_title.encode() not in r.data
    r = anon.get(f"/api/v1/search?q={other_title}")
    assert r.status_code == 200
    assert r.get_json()["results"] == [], "reindexing one wiki must not leak another wiki's private page"

    hook_slug = "groupbrain-hook-acl"
    r = client.post("/api/v1/wikis", json={"slug": hook_slug, "title": "GroupBrain Hook ACL"}, headers=h)
    assert r.status_code == 201
    hook_wiki = Wiki.query.join(User, Wiki.owner_id == User.id).filter(User.username == "agent1", Wiki.slug == hook_slug).first()
    assert hook_wiki is not None
    assert Page.query.filter_by(wiki_id=hook_wiki.id, path="log.md").first().visibility == "private"

    sync_page_to_repo("agent1", hook_slug, ".wikihub/acl", "* public-view\n")
    r = client.post("/api/v1/admin/regenerate-mirror", json={
        "username": "agent1",
        "slug": hook_slug,
        "reindex": True,
    }, headers={"Authorization": "Bearer test-admin-token"})
    assert r.status_code == 200, f"admin reindex failed: {r.status_code} {r.data[:200]}"
    assert r.get_json()["reindexed"] is True
    db.session.expire_all()
    assert Page.query.filter_by(wiki_id=hook_wiki.id, path="log.md").first().visibility == "public"
    r = anon.get(f"/@agent1/{hook_slug}/log")
    assert r.status_code == 200, f"hook/admin ACL reindex should make /log anonymously readable, got {r.status_code}"

    delete_slug = "groupbrain-acl-delete"
    r = client.post("/api/v1/wikis", json={"slug": delete_slug, "title": "GroupBrain ACL Delete"}, headers=h)
    assert r.status_code == 201
    delete_wiki = Wiki.query.join(User, Wiki.owner_id == User.id).filter(User.username == "agent1", Wiki.slug == delete_slug).first()
    assert delete_wiki is not None
    r = client.post(f"/api/v1/wikis/agent1/{delete_slug}/pages", json={
        "path": ".wikihub/acl",
        "content": "* unlisted-view\n",
    }, headers=h)
    assert r.status_code == 201
    db.session.expire_all()
    assert Page.query.filter_by(wiki_id=delete_wiki.id, path="log.md").first().visibility == "unlisted"
    r = client.delete(f"/api/v1/wikis/agent1/{delete_slug}/pages/.wikihub/acl", headers=h)
    assert r.status_code == 204
    db.session.expire_all()
    assert read_file_from_repo("agent1", delete_slug, ".wikihub/acl") is None
    assert Page.query.filter_by(wiki_id=delete_wiki.id, path="log.md").first().visibility == "private"
    r = anon.get(f"/@agent1/{delete_slug}/log")
    assert r.status_code in (401, 403), f"deleting ACL should restore private default, got {r.status_code}"

    revert_slug = "groupbrain-acl-revert"
    r = client.post("/api/v1/wikis", json={"slug": revert_slug, "title": "GroupBrain ACL Revert"}, headers=h)
    assert r.status_code == 201
    revert_wiki = Wiki.query.join(User, Wiki.owner_id == User.id).filter(User.username == "agent1", Wiki.slug == revert_slug).first()
    assert revert_wiki is not None

    from app.git_backend import _repo_path
    import subprocess
    repo_path = _repo_path("agent1", revert_slug)
    private_acl_sha = subprocess.check_output([
        "git", "-C", repo_path, "log", "--format=%H", "--", ".wikihub/acl"
    ], text=True).splitlines()[-1]

    r = client.put(f"/api/v1/wikis/agent1/{revert_slug}/pages/.wikihub/acl", json={
        "content": "* unlisted-view\n",
    }, headers=h)
    assert r.status_code == 200
    db.session.expire_all()
    assert Page.query.filter_by(wiki_id=revert_wiki.id, path="log.md").first().visibility == "unlisted"
    r = anon.get(f"/@agent1/{revert_slug}/log")
    assert r.status_code == 200

    r = client.post(f"/api/v1/wikis/agent1/{revert_slug}/revert", json={
        "sha": private_acl_sha,
        "path": ".wikihub/acl",
    }, headers=h)
    assert r.status_code == 200, f"ACL revert failed: {r.status_code} {r.data[:200]}"
    assert r.get_json()["reindexed"] is True
    db.session.expire_all()
    assert Page.query.filter_by(wiki_id=revert_wiki.id, path=".wikihub/acl").first() is None
    assert Page.query.filter_by(wiki_id=revert_wiki.id, path="log.md").first().visibility == "private"
    assert read_file_from_repo("agent1", revert_slug, ".wikihub/acl") == "* private\n"
    r = anon.get(f"/@agent1/{revert_slug}/log")
    assert r.status_code in (401, 403), f"reverting ACL should refresh inherited private visibility, got {r.status_code}"


def test_email_verification_flow(client):
    """wikihub-ks5t.3: signup with email is non-blocking — account works
    immediately, and a verification link is emailed. Clicking the link sets
    email_verified_at."""
    import os
    os.environ["EMAIL_MODE"] = "mock"
    from app import email_service
    from app.models import User
    email_service.mock_clear()

    # Signup via API with email — should succeed AND queue a verify email.
    r = client.post("/api/v1/accounts", json={"username": "verifyme", "email": "verifyme@example.com"})
    assert r.status_code == 201

    user = User.query.filter_by(username="verifyme").first()
    assert user is not None
    assert user.email_verified_at is None, "should start unverified"

    # Account is immediately usable — non-blocking.
    api_key = r.get_json()["api_key"]
    r2 = client.get("/api/v1/accounts/me", headers={"Authorization": f"Bearer {api_key}"})
    assert r2.status_code == 200

    # The verify email should be in the mock outbox.
    msgs = [m for m in email_service.mock_outbox() if "Verify" in m["subject"] and m["to"] == "verifyme@example.com"]
    assert len(msgs) == 1, f"expected one verify email for verifyme, got {len(msgs)}"
    msg = msgs[0]
    # Pull the verify URL out of the email body.
    import re
    match = re.search(r"/auth/verify/(ev_[A-Za-z0-9_-]+)", msg["text"])
    assert match, f"verify URL not found in email text: {msg['text'][:200]}"
    verify_path = "/auth/verify/" + match.group(1)

    # A clean browser session clicking the link signs the user in AND verifies.
    browser = client.application.test_client()
    r3 = browser.get(verify_path, follow_redirects=False)
    assert r3.status_code == 302, f"expected redirect, got {r3.status_code}"
    assert "/auth/login" not in r3.headers.get("Location", "")

    user = User.query.filter_by(username="verifyme").first()
    assert user.email_verified_at is not None, "email_verified_at should be set after clicking link"

    # A second click on the same link is invalid (single-use) — redirects to login.
    r4 = browser.get(verify_path, follow_redirects=False)
    assert r4.status_code == 302
    assert "/auth/login" in r4.headers["Location"]

    os.environ.pop("EMAIL_MODE", None)


def test_settings_email_change_requires_reverification(client):
    """wikihub-tzgn: changing email in settings clears verification, sends a
    fresh verify link, and still preserves claimed-email conflicts."""
    import os
    import re
    from app import db, email_service
    from app.models import User

    os.environ["EMAIL_MODE"] = "mock"
    email_service.mock_clear()

    r = client.post("/api/v1/accounts", json={"username": "swapuser", "email": "old@example.com"})
    assert r.status_code == 201

    messages = [
        m for m in email_service.mock_outbox()
        if "Verify" in m["subject"] and m["to"] == "old@example.com"
    ]
    assert len(messages) == 1
    match = re.search(r"/auth/verify/(ev_[A-Za-z0-9_-]+)", messages[0]["text"])
    assert match, f"verify URL not found in email text: {messages[0]['text'][:200]}"

    browser = client.application.test_client()
    r = browser.get("/auth/verify/" + match.group(1), follow_redirects=False)
    assert r.status_code == 302

    db.session.expire_all()
    user = User.query.filter_by(username="swapuser").first()
    assert user is not None
    assert user.email == "old@example.com"
    assert user.email_verified_at is not None

    r = client.post("/api/v1/accounts", json={"username": "claimeduser", "email": "claimed@example.com"})
    assert r.status_code == 201
    email_service.mock_clear()

    r = browser.post("/claim-email", json={"email": "claimed@example.com"})
    assert r.status_code == 409
    db.session.expire_all()
    user = User.query.filter_by(username="swapuser").first()
    assert user.email == "old@example.com"
    assert user.email_verified_at is not None
    assert email_service.mock_outbox() == []

    r = browser.post("/claim-email", json={"email": "new@example.com"})
    assert r.status_code == 200
    assert r.get_json()["email"] == "new@example.com"

    db.session.expire_all()
    user = User.query.filter_by(username="swapuser").first()
    assert user.email == "new@example.com"
    assert user.email_verified_at is None, "email change should clear verification"

    messages = [
        m for m in email_service.mock_outbox()
        if "Verify" in m["subject"] and m["to"] == "new@example.com"
    ]
    assert len(messages) == 1, f"expected one verify email for new@example.com, got {len(messages)}"
    match = re.search(r"/auth/verify/(ev_[A-Za-z0-9_-]+)", messages[0]["text"])
    assert match, f"verify URL not found in email text: {messages[0]['text'][:200]}"

    r = browser.get("/auth/verify/" + match.group(1), follow_redirects=False)
    assert r.status_code == 302

    db.session.expire_all()
    user = User.query.filter_by(username="swapuser").first()
    assert user.email == "new@example.com"
    assert user.email_verified_at is not None, "new email should verify after clicking the fresh link"

    os.environ.pop("EMAIL_MODE", None)


def test_password_reset_lifecycle(client):
    """wikihub-ks5t.5: forgot-password + reset flow with single-use, expiry,
    non-enumeration, and verified-on-reset semantics."""
    import os
    import re
    from app import email_service
    from app.auth_utils import hash_one_time_token
    from app.models import PasswordResetToken, PendingInvite, User

    os.environ["EMAIL_MODE"] = "mock"
    email_service.mock_clear()

    # Seed a pending invite so reset-path verification materializes it.
    owner = client.post("/api/v1/accounts", json={"username": "resetowner"})
    assert owner.status_code == 201
    owner_key = owner.get_json()["api_key"]
    ho = {"Authorization": f"Bearer {owner_key}"}
    r = client.post("/api/v1/wikis", json={"slug": "reset-share", "title": "Reset Share"}, headers=ho)
    assert r.status_code == 201
    r = client.post("/api/v1/wikis/resetowner/reset-share/pages", json={
        "path": "secret.md", "content": "# secret", "visibility": "private",
    }, headers=ho)
    assert r.status_code == 201
    r = client.post("/api/v1/wikis/resetowner/reset-share/share", json={
        "pattern": "*", "email": "resetme@example.com", "role": "read",
    }, headers=ho)
    assert r.status_code == 200
    assert PendingInvite.query.filter_by(email="resetme@example.com").count() == 1

    # Create an account with an unverified email and password.
    r = client.post("/api/v1/accounts", json={
        "username": "resetme",
        "email": "resetme@example.com",
        "password": "oldpass12345",
    })
    assert r.status_code == 201
    reset_key = r.get_json()["api_key"]
    hr = {"Authorization": f"Bearer {reset_key}"}
    user = User.query.filter_by(username="resetme").first()
    assert user is not None
    assert user.email_verified_at is None

    r = client.get("/api/v1/wikis/resetowner/reset-share/pages/secret.md", headers=hr)
    assert r.status_code in (403, 404), "pending invite must not apply before reset-driven verification"

    # Happy path: forgot-password sends a reset email, token row is hashed in DB,
    # reset verifies the email, applies pending invites, and the new password works.
    r = client.post("/auth/forgot-password", data={"email": "resetme@example.com"})
    assert r.status_code == 200
    assert b"If that email is on an account" in r.data

    messages = [m for m in email_service.mock_outbox() if m["subject"] == "Reset your WikiHub password"]
    assert messages, "expected a password reset email"
    match = re.search(r"/auth/reset/(pr_[A-Za-z0-9_-]+)", messages[-1]["text"])
    assert match, f"reset URL not found in email body: {messages[-1]['text'][:200]}"
    raw_token = match.group(1)

    token_row = PasswordResetToken.query.filter_by(token_hash=hash_one_time_token(raw_token)).first()
    assert token_row is not None, "reset token must be hashed at rest in DB"

    r = client.get(f"/auth/reset/{raw_token}")
    assert r.status_code == 200
    assert b"Reset password for @resetme" in r.data

    browser = client.application.test_client()
    r = browser.post(
        f"/auth/reset/{raw_token}",
        data={"password": "newpass12345", "confirm_password": "newpass12345"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/@resetme")

    user = User.query.filter_by(username="resetme").first()
    assert user.email_verified_at is not None, "password reset should mark the email verified"
    token_row = db.session.get(PasswordResetToken, token_row.id)
    assert token_row.used_at is not None, "reset token should become single-use after success"
    assert PendingInvite.query.filter_by(email="resetme@example.com").count() == 0, \
        "pending invite should materialize when reset verifies the email"

    r = client.get("/api/v1/wikis/resetowner/reset-share/pages/secret.md", headers=hr)
    assert r.status_code == 200, "pending invite should unlock after reset"

    r = client.post("/api/v1/auth/token", json={"username": "resetme", "password": "newpass12345"})
    assert r.status_code == 200
    r = client.post("/api/v1/auth/token", json={"username": "resetme", "password": "oldpass12345"})
    assert r.status_code == 401

    # Used token: the same link should now be rejected.
    r = client.get(f"/auth/reset/{raw_token}")
    assert r.status_code == 400
    assert b"expired or was already used" in r.data

    # Expired token: mint a fresh token, expire it manually, verify GET and POST fail gracefully.
    r = client.post("/auth/forgot-password", data={"email": "resetme@example.com"})
    assert r.status_code == 200
    messages = [m for m in email_service.mock_outbox() if m["subject"] == "Reset your WikiHub password"]
    match = re.search(r"/auth/reset/(pr_[A-Za-z0-9_-]+)", messages[-1]["text"])
    expired_raw = match.group(1)
    expired_row = PasswordResetToken.query.filter_by(token_hash=hash_one_time_token(expired_raw)).first()
    expired_row.expires_at = utcnow() - timedelta(minutes=1)
    db.session.commit()

    r = client.get(f"/auth/reset/{expired_raw}")
    assert r.status_code == 400
    assert b"expired or was already used" in r.data
    r = client.post(
        f"/auth/reset/{expired_raw}",
        data={"password": "anotherpass123", "confirm_password": "anotherpass123"},
    )
    assert r.status_code == 400
    assert b"expired or was already used" in r.data

    # Wrong token: random string should get the same helpful failure.
    r = client.get("/auth/reset/pr_not-a-real-token")
    assert r.status_code == 400
    assert b"expired or was already used" in r.data

    # Non-enumerating: nonexistent email still gets the same success response.
    outbox_before = len(email_service.mock_outbox())
    r = client.post("/auth/forgot-password", data={"email": "nobody@example.com"})
    assert r.status_code == 200
    assert b"If that email is on an account" in r.data
    assert len(email_service.mock_outbox()) == outbox_before, "nonexistent email should not send anything"

    os.environ.pop("EMAIL_MODE", None)


def test_login_redirect_back(client):
    """Login form should redirect back to the page the user came from.

    Three layers of redirect-after-login:
    1. Explicit ?next=/foo on the login URL → land on /foo
    2. Referer header from same-origin (no ?next=) → land on the referring page
    3. No next, no Referer → land on home

    Without (2), every "Sign in" link would need to manually pass ?next=current_path,
    and any link that didn't would dump users on the homepage.
    """
    # Make a real account so the password works
    r = client.post("/api/v1/accounts", json={"username": "redirtest", "password": "testpass12345"})
    assert r.status_code == 201

    # 1. explicit ?next=
    c1 = client.application.test_client()
    r = c1.post("/auth/login?next=/explore",
                data={"username": "redirtest", "password": "testpass12345"},
                follow_redirects=False)
    assert r.status_code == 302, f"login should redirect, got {r.status_code}"
    assert r.headers["Location"].endswith("/explore"), f"expected /explore, got {r.headers['Location']}"

    # 2. Referer fallback (no ?next=)
    r = client.post("/api/v1/accounts", json={"username": "redirtest2", "password": "testpass12345"})
    assert r.status_code == 201
    c2 = client.application.test_client()
    r = c2.post("/auth/login",
                data={"username": "redirtest2", "password": "testpass12345"},
                headers={"Referer": "http://localhost/@somewiki/cool-page"},
                follow_redirects=False)
    assert r.status_code == 302
    assert "/@somewiki/cool-page" in r.headers["Location"], (
        f"Referer-based redirect failed: got {r.headers['Location']}"
    )

    # 3. No next, no Referer → home (not /auth/*)
    r = client.post("/api/v1/accounts", json={"username": "redirtest3", "password": "testpass12345"})
    assert r.status_code == 201
    c3 = client.application.test_client()
    r = c3.post("/auth/login",
                data={"username": "redirtest3", "password": "testpass12345"},
                follow_redirects=False)
    assert r.status_code == 302
    assert "/auth/" not in r.headers["Location"], f"unwanted /auth/ redirect: {r.headers['Location']}"

    # 4. Cross-origin Referer must be REJECTED (open-redirect guard)
    r = client.post("/api/v1/accounts", json={"username": "redirtest4", "password": "testpass12345"})
    assert r.status_code == 201
    c4 = client.application.test_client()
    r = c4.post("/auth/login",
                data={"username": "redirtest4", "password": "testpass12345"},
                headers={"Referer": "https://evil.example.com/phishing"},
                follow_redirects=False)
    assert r.status_code == 302
    assert "evil.example.com" not in r.headers["Location"], (
        "open-redirect risk: cross-origin Referer was honored"
    )


def test_url_login(client):
    """GET /auth/login?api_key=... and ?username=&password= create a session.

    Discouraged (URL creds leak to logs/history) but supported for
    bookmarkable auto-login on trusted devices.
    """
    from app.routes.auth import _login_attempts
    _login_attempts.clear()

    r = client.post("/api/v1/accounts", json={"username": "urllogin", "password": "urlpass12345"})
    assert r.status_code == 201
    api_key = r.get_json()["api_key"]

    # 1. GET with api_key → session + 302 to next
    c1 = client.application.test_client()
    r = c1.get(f"/auth/login?api_key={api_key}&next=/settings", follow_redirects=False)
    assert r.status_code == 302, f"expected 302, got {r.status_code}"
    assert r.headers["Location"].endswith("/settings")
    r = c1.get("/settings")
    assert r.status_code == 200, "session cookie not issued"

    # 2. GET with username+password → session + 302
    c2 = client.application.test_client()
    r = c2.get("/auth/login?username=urllogin&password=urlpass12345&next=/explore",
               follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/explore")

    # 3. GET with bad api_key → 401
    c3 = client.application.test_client()
    r = c3.get("/auth/login?api_key=wh_not_a_real_key", follow_redirects=False)
    assert r.status_code == 401

    # 4. GET with bad password → 401
    c4 = client.application.test_client()
    r = c4.get("/auth/login?username=urllogin&password=wrong", follow_redirects=False)
    assert r.status_code == 401

    # 5. Bare GET still renders the login form (no creds in query)
    c5 = client.application.test_client()
    r = c5.get("/auth/login")
    assert r.status_code == 200
    assert b"api_key" in r.data or b"API Key" in r.data

    # 6. Cross-origin next= still rejected on GET path (open-redirect guard)
    c6 = client.application.test_client()
    r = c6.get(f"/auth/login?api_key={api_key}&next=https://evil.example.com/x",
               follow_redirects=False)
    assert r.status_code == 302
    assert "evil.example.com" not in r.headers["Location"]


def test_url_login_log_redaction():
    """werkzeug access log filter scrubs api_key= and password= from URLs."""
    import logging
    from app import _RedactQueryParams

    f = _RedactQueryParams()
    rec = logging.LogRecord(
        name="werkzeug", level=logging.INFO, pathname="", lineno=0,
        msg='GET /auth/login?api_key=wh_SECRET&next=/x HTTP/1.1',
        args=(), exc_info=None,
    )
    f.filter(rec)
    assert "wh_SECRET" not in rec.msg
    assert "api_key=REDACTED" in rec.msg

    # args tuple path (werkzeug uses %s format strings)
    rec2 = logging.LogRecord(
        name="werkzeug", level=logging.INFO, pathname="", lineno=0,
        msg='%s', args=('GET /auth/login?password=hunter2 HTTP/1.1',), exc_info=None,
    )
    f.filter(rec2)
    assert "hunter2" not in rec2.args[0]
    assert "password=REDACTED" in rec2.args[0]


def test_login_post_without_referer_succeeds_with_csrf_token_wikihub_m8zi(client):
    """wikihub-m8zi: API-key login POST must work without a Referer header.

    iOS Safari (ITP, Private Browsing, in-app browsers) strips the Referer
    header on form POSTs. Flask-WTF's default WTF_CSRF_SSL_STRICT=True over
    HTTPS rejects POSTs without a same-origin Referer with a 400 "The referrer
    header is missing", breaking the login form on iPad with no user-facing
    indication of why.

    Repro on prod (before fix):
      curl -X POST https://wikihub.md/auth/login --data 'csrf_token=...&api_key=...'
      # (no Referer) -> 400 "The referrer header is missing"
      curl -X POST https://wikihub.md/auth/login --data '...' -H 'Referer: https://wikihub.md/...'
      # -> 302 success

    The fix sets WTF_CSRF_SSL_STRICT=False in app/__init__.py. CSRF token
    validation continues to run (and continues to reject token-less or
    invalid-token POSTs); SameSite=Lax session cookies (already set in
    config.py) carry the cross-site-request defense load.

    The main test fixture sets WTF_CSRF_ENABLED=False so most flows can post
    without tokens. This test re-enables CSRF (flask-wtf reads both config
    keys per-request, not at init time) and forces HTTPS via base_url so
    WTF_CSRF_SSL_STRICT's Referer-check code path actually runs.
    """
    import re as _re
    from flask import g

    app = client.application
    prev_csrf_enabled = app.config.get("WTF_CSRF_ENABLED")
    prev_ssl_strict = app.config.get("WTF_CSRF_SSL_STRICT")
    try:
        # The bug only triggers when CSRF protection is fully ON.
        app.config["WTF_CSRF_ENABLED"] = True
        # IMPORTANT: do NOT set WTF_CSRF_SSL_STRICT here — we are testing
        # whether create_app()'s default-setdefault applies. The fix is in
        # app/__init__.py: app.config.setdefault("WTF_CSRF_SSL_STRICT", False)

        # Create an account with an API key (this client has CSRF temporarily
        # back on, so we use the JSON API which is csrf-exempt for api_bp).
        r = client.post("/api/v1/accounts", json={"username": "ipad_safari_user"})
        assert r.status_code == 201, f"account create failed: {r.status_code}"
        api_key = r.get_json()["api_key"]

        # Use a fresh test client over HTTPS so cookies start clean and
        # wsgi.url_scheme=='https' (the precondition for SSL_STRICT).
        c = app.test_client()

        # The full test suite runs inside one shared `with app.app_context():`
        # block, so flask's `g` proxy persists across test_client requests.
        # flask-wtf's generate_csrf() caches the signed token on g.csrf_token
        # and short-circuits if it's already there — meaning a fresh client's
        # GET would re-render a *previous* request's token without writing
        # csrf_token to its own (empty) session. Clear it to force a fresh
        # generate-and-store cycle. (In real prod, every WSGI request gets a
        # new app_context and this is unnecessary.)
        g.pop("csrf_token", None)

        # GET the login form over HTTPS — captures session cookie + CSRF token.
        r = c.get("/auth/login", base_url="https://localhost")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        m = _re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
        assert m, "no csrf_token in login form HTML"
        csrf_token = m.group(1)

        # iOS Safari behavior: POST WITHOUT a Referer header.
        # Werkzeug test client doesn't add Referer by default.
        r = c.post(
            "/auth/login",
            data={"csrf_token": csrf_token, "api_key": api_key},
            base_url="https://localhost",
        )

        # Before the fix: 400 "The referrer header is missing".
        # After the fix: 302 (login succeeded; CSRF token still validated).
        body_preview = r.get_data(as_text=True)[:300]
        assert r.status_code == 302, (
            f"POST /auth/login without Referer returned {r.status_code} "
            f"(expected 302). Body: {body_preview}"
        )
        assert "referrer header is missing" not in body_preview.lower(), (
            f"server still requires Referer: {body_preview}"
        )

        # Confirm CSRF token validation is still wired up — a POST with
        # NO csrf_token (and no Referer) must still be rejected, otherwise
        # we've accidentally disabled CSRF entirely.
        c2 = app.test_client()
        g.pop("csrf_token", None)
        c2.get("/auth/login", base_url="https://localhost")
        r = c2.post(
            "/auth/login",
            data={"api_key": api_key},  # no csrf_token
            base_url="https://localhost",
        )
        assert r.status_code == 400, (
            f"POST without csrf_token must be rejected (CSRF still on); "
            f"got {r.status_code}"
        )
    finally:
        # Restore main-suite config so subsequent tests still pass.
        if prev_csrf_enabled is None:
            app.config.pop("WTF_CSRF_ENABLED", None)
        else:
            app.config["WTF_CSRF_ENABLED"] = prev_csrf_enabled
        if prev_ssl_strict is None:
            app.config.pop("WTF_CSRF_SSL_STRICT", None)
        else:
            app.config["WTF_CSRF_SSL_STRICT"] = prev_ssl_strict


def test_client_config_hint(client):
    """signup and token responses include client_config telling agents where to save credentials"""
    # signup
    r = client.post("/api/v1/accounts", json={"username": "cfguser"})
    assert r.status_code == 201
    data = r.get_json()
    cc = data.get("client_config")
    assert cc, "signup response missing client_config"
    assert cc["path"] == "~/.wikihub/credentials.json"
    assert cc["mode"] == "0600"
    assert cc["profile"] == "default"
    default_profile = cc["content"]["default"]
    assert default_profile["username"] == "cfguser"
    assert default_profile["api_key"] == data["api_key"]
    assert default_profile["server"].startswith("http")
    assert "api_key" in cc["read_snippets"]["shell"]
    assert cc["env_alternative"]["WIKIHUB_API_KEY"] == data["api_key"]

    # token exchange also returns client_config
    r = client.post("/auth/signup", data={"username": "cfguser2", "password": "secret-pw-123"}, follow_redirects=False)
    assert r.status_code == 302
    r = client.post("/api/v1/auth/token", json={"username": "cfguser2", "password": "secret-pw-123"})
    assert r.status_code == 200
    tdata = r.get_json()
    assert tdata["client_config"]["content"]["default"]["api_key"] == tdata["api_key"]
    assert tdata["client_config"]["content"]["default"]["username"] == "cfguser2"


def test_magic_link_from_password(client):
    """POST /api/v1/auth/magic-link should accept {username,password} as an alternative to Bearer"""
    # create account with password via API (bypasses web signup IP rate limit)
    r = client.post("/api/v1/accounts", json={
        "username": "pwmagic", "password": "correct-horse-battery-staple",
    })
    assert r.status_code == 201, r.get_json()

    # magic link from username+password (no Bearer)
    r = client.post("/api/v1/auth/magic-link", json={
        "username": "pwmagic", "password": "correct-horse-battery-staple", "next": "/settings",
    })
    assert r.status_code == 201, r.get_json()
    data = r.get_json()
    assert "/auth/magic/" in data["login_url"]

    # consume the link
    magic_path = urlparse(data["login_url"]).path
    browser = client.application.test_client()
    r = browser.get(magic_path, follow_redirects=False)
    assert r.status_code == 302, r.get_data(as_text=True)
    assert r.headers["Location"].endswith("/settings")

    # the test harness wraps every test in a single outer app_context,
    # so flask-login's cached user sticks on flask.g across requests.
    # clear it and use a fresh test_client so the "anonymous" assertions
    # below are really anonymous.
    from flask import g as _g
    _g.pop("_login_user", None)
    anon = client.application.test_client()

    # wrong password → 401
    r = anon.post("/api/v1/auth/magic-link", json={"username": "pwmagic", "password": "wrong"})
    assert r.status_code == 401

    # no auth at all → 401
    _g.pop("_login_user", None)
    r = anon.post("/api/v1/auth/magic-link", json={"next": "/settings"})
    assert r.status_code == 401


def test_anonymous_public_edit(client, api_key):
    h = {"Authorization": f"Bearer {api_key}"}
    r = client.post("/api/v1/wikis/agent1/test-wiki/pages", json={
        "path": "wiki/open.md",
        "content": "---\ntitle: Open\nvisibility: public-edit\n---\n\nOpen edit page.",
        "visibility": "public-edit",
    }, headers=h)
    assert r.status_code == 201

    r = client.put("/api/v1/wikis/agent1/test-wiki/pages/wiki/open.md", json={
        "content": "# Open\n\nEdited anonymously.",
    })
    assert r.status_code == 200

    r = client.get("/api/v1/wikis/agent1/test-wiki/pages/wiki/open.md")
    assert r.status_code == 200
    assert "Edited anonymously" in r.get_json()["content"]


def test_public_edit_shows_edit_button(client, api_key):
    """anonymous visitors on a public-edit page see the Edit button (wikihub-euw3.1).
    and on the wiki root index when index.md is public-edit (wikihub-euw3.2)."""
    h = {"Authorization": f"Bearer {api_key}"}

    # create a public-edit wiki; index.md is auto-scaffolded on create
    r = client.post("/api/v1/wikis", json={"slug": "open-wiki", "title": "Open Wiki"}, headers=h)
    assert r.status_code == 201

    # set index.md to public-edit so wiki_index renders reader.html with edit button
    r = client.put("/api/v1/wikis/agent1/open-wiki/pages/index.md", json={
        "content": "---\ntitle: Open Wiki\nvisibility: public-edit\n---\n\nRoot page.",
        "visibility": "public-edit",
    }, headers=h)
    assert r.status_code == 200, f"put index.md: {r.status_code} {r.get_data(as_text=True)[:200]}"

    r = client.post("/api/v1/wikis/agent1/open-wiki/pages", json={
        "path": "wiki/open.md",
        "content": "---\ntitle: Open\nvisibility: public-edit\n---\n\nOpen edit page.",
        "visibility": "public-edit",
    }, headers=h)
    assert r.status_code == 201

    # create a separate read-only public page for negative check
    r = client.post("/api/v1/wikis/agent1/open-wiki/pages", json={
        "path": "wiki/readonly.md",
        "content": "---\ntitle: ReadOnly\nvisibility: public\n---\n\nRead only page.",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201

    anon = client.application.test_client()

    # wiki_page: public-edit page shows Edit button for anonymous
    r = anon.get("/@agent1/open-wiki/wiki/open")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "/@agent1/open-wiki/wiki/open/edit" in body, "Edit link missing on public-edit page for anonymous user"

    # wiki_page: public (read-only) page does NOT show Edit button for anonymous
    r = anon.get("/@agent1/open-wiki/wiki/readonly")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "/@agent1/open-wiki/wiki/readonly/edit" not in body, "Edit link should not appear on read-only page for anonymous user"

    # wiki_index: root URL renders reader.html and shows Edit button when index.md is public-edit
    r = anon.get("/@agent1/open-wiki")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "/@agent1/open-wiki/index/edit" in body, "Edit link missing on wiki index for anonymous user"


def test_anonymous_posting_and_claim(client):
    """user A posts an anonymous+claimable page; user B claims it. (wikihub-7b2r)"""
    ra = client.post("/api/v1/accounts", json={"username": "anonwriter"})
    ka = ra.get_json()["api_key"]
    ha = {"Authorization": f"Bearer {ka}"}
    client.post("/api/v1/wikis", json={"slug": "rumor-mill", "title": "Rumors"}, headers=ha)
    r = client.post("/api/v1/wikis/anonwriter/rumor-mill/pages", json={
        "path": "wiki/rumor.md",
        "content": "# Rumor\n\nSomething juicy.",
        "visibility": "public",
        "anonymous": True,
        "claimable": True,
    }, headers=ha)
    assert r.status_code == 201, r.get_data(as_text=True)
    body = r.get_json()
    assert body["anonymous"] is True
    assert body["claimable"] is True
    assert body["author"] is None

    r = client.get("/api/v1/wikis/anonwriter/rumor-mill/pages/wiki/rumor.md", headers=ha)
    assert r.status_code == 200
    assert r.get_json()["author"] is None
    assert r.get_json()["anonymous"] is True

    rb = client.post("/api/v1/accounts", json={"username": "claimer"})
    kb = rb.get_json()["api_key"]
    hb = {"Authorization": f"Bearer {kb}"}
    r = client.post("/api/v1/wikis/anonwriter/rumor-mill/pages/wiki/rumor.md/claim", headers=hb)
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["anonymous"] is False
    assert body["claimable"] is False
    assert body["author"] == "claimer"

    r = client.post("/api/v1/wikis/anonwriter/rumor-mill/pages/wiki/rumor.md/claim", headers=hb)
    assert r.status_code == 409


def test_acl_permissions(client, api_key):
    """private pages are not readable without auth"""
    h = {"Authorization": f"Bearer {api_key}"}

    client.post("/api/v1/wikis/agent1/test-wiki/pages", json={
        "path": "secret.md",
        "content": "# Secret\n\nPrivate stuff.",
        "visibility": "private",
    }, headers=h)

    # unauthenticated read should fail
    r = client.get("/api/v1/wikis/agent1/test-wiki/pages/secret.md")
    assert r.status_code in (401, 403, 404)


def test_private_new_page_requires_write_access(client, api_key):
    """anonymous users cannot open or submit /new inside a private wiki."""
    h = {"Authorization": f"Bearer {api_key}"}

    r = client.post("/api/v1/wikis", json={"slug": "private-new", "title": "Private New"}, headers=h)
    assert r.status_code == 201

    r = client.get("/@agent1/private-new/new?path=notes/secret")
    assert r.status_code == 403

    r = client.post("/@agent1/private-new/new", data={
        "path": "notes/secret",
        "content": "# Secret\n\nShould not be created anonymously.",
        "visibility": "private",
    }, follow_redirects=False)
    assert r.status_code == 403

    r = client.get("/api/v1/wikis/agent1/private-new/pages/notes/secret.md", headers=h)
    assert r.status_code == 404


def test_people_directory_and_profiles(client, api_key):
    h = {"Authorization": f"Bearer {api_key}"}

    # publish agent1 personal wiki and one public project
    r = client.put("/api/v1/wikis/agent1/agent1/pages/index.md", json={
        "content": "---\ntitle: Agent One\nvisibility: public\n---\n\nBuilder of public wiki systems.",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 200

    r = client.post("/api/v1/wikis", json={"slug": "atlas", "title": "Atlas"}, headers=h)
    assert r.status_code == 201
    r = client.put("/api/v1/wikis/agent1/atlas/pages/index.md", json={
        "content": "---\ntitle: Atlas\nvisibility: public\n---\n\nPublic atlas.",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 200

    r = client.post("/api/v1/wikis", json={"slug": "vault", "title": "Vault"}, headers=h)
    assert r.status_code == 201
    r = client.put("/api/v1/wikis/agent1/vault/pages/index.md", json={
        "content": "---\ntitle: Vault\nvisibility: private\n---\n\nPrivate vault.",
        "visibility": "private",
    }, headers=h)
    assert r.status_code == 200

    # another person with a public profile + project
    r = client.post("/api/v1/accounts", json={"username": "person2"})
    assert r.status_code == 201
    key2 = r.get_json()["api_key"]
    h2 = {"Authorization": f"Bearer {key2}"}

    r = client.put("/api/v1/wikis/person2/person2/pages/index.md", json={
        "content": "---\ntitle: Person Two\nvisibility: public\n---\n\nSecond profile wiki.",
        "visibility": "public",
    }, headers=h2)
    assert r.status_code == 200
    r = client.post("/api/v1/wikis", json={"slug": "garden", "title": "Garden"}, headers=h2)
    assert r.status_code == 201
    r = client.put("/api/v1/wikis/person2/garden/pages/index.md", json={
        "content": "---\ntitle: Garden\nvisibility: public\n---\n\nPublic garden.",
        "visibility": "public",
    }, headers=h2)
    assert r.status_code == 200

    r = client.get("/explore")
    assert r.status_code == 200
    assert b"Recently Updated Wikis" in r.data
    assert b"updated " in r.data
    assert b"All people" in r.data
    assert b"@agent1" in r.data
    assert b"@person2" in r.data
    assert r.data.index(b"Garden") < r.data.index(b"Atlas")

    r = client.get("/people")
    assert r.status_code == 200
    assert b"People" in r.data
    assert b"@agent1" in r.data
    assert b"@person2" in r.data

    r = client.get("/@agent1")
    assert r.status_code == 200
    assert b"Builder of public wiki systems." in r.data
    assert b"Atlas" in r.data
    assert b"Vault" not in r.data

    r = client.get("/@person2")
    assert r.status_code == 200
    assert b"Second profile wiki." in r.data
    assert b"Garden" in r.data


def test_new_folder_ui(client):
    r = client.post("/auth/signup", data={"username": "folderuser", "password": "testpass123"}, follow_redirects=False)
    assert r.status_code == 302

    # Create top-level folder
    r = client.post("/@folderuser/folderuser/new-folder", data={
        "folder_name": "plans",
        "visibility": "public",
    }, follow_redirects=False)
    assert r.status_code == 302
    assert "/@folderuser/folderuser/plans/index/edit" in r.headers["Location"]

    # Create subfolder using parent param
    r = client.post("/@folderuser/folderuser/new-folder?parent=plans", data={
        "folder_name": "2026",
        "visibility": "public",
    }, follow_redirects=False)
    assert r.status_code == 302
    assert "/@folderuser/folderuser/plans/2026/index/edit" in r.headers["Location"]

    r = client.get("/@folderuser/folderuser/plans/2026/")
    assert r.status_code == 200
    assert b"plans/2026" in r.data or b"2026" in r.data


def test_sidebar_indentation(client, api_key):
    """REGRESSION GUARD wikihub-58c — children of folders must be visually indented.

    This bug keeps coming back: someone edits sidebar CSS or the macro in
    reader.html, accidentally drops the .sidebar-children padding or the wrapper
    div, and the left sidebar collapses into an un-nested flat list. This test
    fails loudly if that happens. DO NOT remove or relax without discussing.

    What it checks:
      1. The .sidebar-children CSS rule exists with padding-left >= 16px.
      2. A folder with a child page renders <div class="sidebar-children">
         wrapping the child row in the rendered HTML.
      3. Parent folder rows and child page rows encode explicit depth-based
         padding so the nested tree remains visually legible.
    """
    import re

    h = {"Authorization": f"Bearer {api_key}"}

    # dedicated wiki so we don't collide with other tests
    r = client.post("/api/v1/wikis", json={"slug": "indent-test", "title": "Indent Test"}, headers=h)
    assert r.status_code == 201

    # creating a page at "plans/roadmap.md" implicitly creates the 'plans' folder
    r = client.post(
        "/api/v1/wikis/agent1/indent-test/pages",
        json={
            "path": "plans/roadmap.md",
            "content": "---\ntitle: Roadmap\nvisibility: public\n---\n\n# Roadmap",
            "visibility": "public",
        },
        headers=h,
    )
    assert r.status_code == 201, f"child page create failed: {r.status_code} {r.data[:200]}"

    # fetch the reader page for the child — sidebar will expand folder
    r = client.get("/@agent1/indent-test/plans/roadmap")
    assert r.status_code == 200, f"reader fetch failed: {r.status_code}"
    html = r.data.decode()

    # 1) CSS rule exists with enough padding
    m = re.search(r"\.sidebar-children\s*\{[^}]*padding-left:\s*(\d+)px", html)
    assert m, (
        "wikihub-58c REGRESSION: .sidebar-children CSS rule missing padding-left. "
        "Child items under folders will not be indented. Restore the rule in "
        "app/templates/reader.html."
    )
    px = int(m.group(1))
    assert px >= 16, (
        f"wikihub-58bd REGRESSION: .sidebar-children padding-left is only {px}px. "
        "Nested rows need a real wrapper offset before any per-level row padding."
    )

    # 2) HTML structure: folder wraps child rows in .sidebar-children
    assert 'class="sidebar-children"' in html, (
        "wikihub-58c REGRESSION: folder macro no longer emits "
        '<div class="sidebar-children">. Child rows will render as siblings of '
        "the folder instead of nested. Check the render_sidebar macro in "
        "app/templates/reader.html."
    )

    # 3) Depth-based row padding exists for both the folder row and the child row.
    folder_pad = re.search(
        r'<a href="/@agent1/indent-test/plans/" class="sidebar-item active" style="padding-left:\s*(\d+)px;',
        html,
    )
    assert folder_pad, (
        "wikihub-ivdg REGRESSION: folder rows in the reader sidebar no longer "
        "encode explicit depth-based padding."
    )
    child_pad = re.search(
        r'data-path="plans/roadmap\.md"[^>]*style="padding-left:\s*(\d+)px;',
        html,
    )
    assert child_pad, (
        "wikihub-ivdg REGRESSION: child page rows in the reader sidebar no "
        "longer encode explicit depth-based padding."
    )
    folder_px = int(folder_pad.group(1))
    child_px = int(child_pad.group(1))
    assert child_px > folder_px, (
        f"wikihub-ivdg REGRESSION: child page padding-left ({child_px}px) must "
        f"exceed parent folder padding-left ({folder_px}px) so nesting remains visible."
    )


def test_reader_sidebar_collapse_controls(client, api_key):
    """Desktop reader renders explicit collapse/reopen controls for both sidebars."""
    h = {"Authorization": f"Bearer {api_key}"}

    r = client.post("/api/v1/wikis", json={"slug": "sidebar-controls", "title": "Sidebar Controls"}, headers=h)
    assert r.status_code == 201

    r = client.post(
        "/api/v1/wikis/agent1/sidebar-controls/pages",
        json={
            "path": "notes/overview.md",
            "content": (
                "---\n"
                "title: Overview\n"
                "visibility: public\n"
                "---\n\n"
                "# Overview\n\n"
                "## First section\nBody.\n\n"
                "## Second section\nMore.\n\n"
                "## Third section\nDone."
            ),
            "visibility": "public",
        },
        headers=h,
    )
    assert r.status_code == 201, f"page create failed: {r.status_code} {r.data[:200]}"

    r = client.get("/@agent1/sidebar-controls/notes/overview")
    assert r.status_code == 200, f"reader fetch failed: {r.status_code}"
    html = r.data.decode()

    assert 'id="left-sidebar-collapse"' in html, (
        "wikihub-adhu REGRESSION: desktop left sidebar collapse button missing "
        "from app/templates/reader.html."
    )
    assert 'id="left-sidebar-reopen"' in html, (
        "wikihub-adhu REGRESSION: desktop left sidebar reopen button missing "
        "from the reader chrome."
    )
    assert 'id="right-panel-collapse"' in html, (
        "wikihub-adhu REGRESSION: right sidebar collapse button missing on "
        "pages that render the context panel."
    )
    assert 'id="right-panel-reopen"' in html, (
        "wikihub-adhu REGRESSION: right sidebar reopen button missing from "
        "the reader actions area."
    )
    assert "wikihub-left-sidebar-collapsed" in html, (
        "wikihub-adhu REGRESSION: left sidebar collapse state key missing "
        "from reader JavaScript."
    )
    assert "wikihub-right-panel-collapsed" in html, (
        "wikihub-adhu REGRESSION: right sidebar collapse state key missing "
        "from reader JavaScript."
    )
    assert "wikihub-sidebar-folders:" in html and "agent1/sidebar-controls" in html, (
        "wikihub-oud7 REGRESSION: sidebar folder state should be namespaced by "
        "owner/wiki rather than stored in one global localStorage bucket."
    )


def test_reader_qr_code_affordance(client, api_key):
    """Reader page exposes a subtle QR-code trigger + vendored client-side QR lib (wikihub-x622)."""
    h = {"Authorization": f"Bearer {api_key}"}

    r = client.post("/api/v1/wikis", json={"slug": "qr-wiki", "title": "QR Wiki"}, headers=h)
    assert r.status_code == 201

    r = client.post(
        "/api/v1/wikis/agent1/qr-wiki/pages",
        json={
            "path": "page.md",
            "content": "---\ntitle: QR Page\nvisibility: public\n---\n\n# QR Page\n\nBody.",
            "visibility": "public",
        },
        headers=h,
    )
    assert r.status_code == 201, f"page create failed: {r.status_code} {r.data[:200]}"

    r = client.get("/@agent1/qr-wiki/page")
    assert r.status_code == 200, f"reader fetch failed: {r.status_code}"
    html = r.data.decode()

    # The subtle trigger lives in the download menu, keyboard-accessible button.
    assert 'id="qr-trigger"' in html, (
        "wikihub-x622 REGRESSION: QR-code trigger missing from the reader "
        "download menu in app/templates/reader.html."
    )
    assert 'aria-label="Show QR code for this page"' in html, (
        "wikihub-x622 REGRESSION: QR trigger lost its accessible label."
    )
    assert 'onclick="openQrModal()"' in html, (
        "wikihub-x622 REGRESSION: QR trigger no longer wired to openQrModal()."
    )
    # The vendored, offline QR library must be referenced (no CDN at runtime).
    assert "js/qrcode-generator-1.4.4.js" in html, (
        "wikihub-x622 REGRESSION: vendored qrcode-generator script reference "
        "missing from the reader page."
    )
    assert "cdn" not in html.lower().split("qrcode-generator")[0][-80:], (
        "wikihub-x622: QR library must be vendored, not loaded from a CDN."
    )
    # Popover scaffolding present so the trigger has something to open.
    assert 'id="qr-overlay"' in html and 'id="qr-canvas-wrap"' in html, (
        "wikihub-x622 REGRESSION: QR popover markup missing from reader.html."
    )

    # The vendored asset itself must actually be served.
    r = client.get("/static/js/qrcode-generator-1.4.4.js")
    assert r.status_code == 200, f"vendored QR script not served: {r.status_code}"
    body = r.data.decode()
    assert "createSvgTag" in body and "addData" in body, (
        "wikihub-x622: served QR asset does not look like qrcode-generator."
    )


def test_folder_async_sidebar_passes_current_context(client, api_key):
    """Folder views should pass explicit current-path context into sidebar.json."""
    import app.routes.wiki as wiki_routes

    h = {"Authorization": f"Bearer {api_key}"}
    r = client.post("/api/v1/wikis", json={"slug": "folder-sidebar", "title": "Folder Sidebar"}, headers=h)
    assert r.status_code == 201
    for name in ("notes/a.md", "notes/b.md"):
        r = client.post(
            "/api/v1/wikis/agent1/folder-sidebar/pages",
            json={
                "path": name,
                "content": f"---\\ntitle: {name}\\nvisibility: public\\n---\\n\\n# {name}",
                "visibility": "public",
            },
            headers=h,
        )
        assert r.status_code == 201

    original_threshold = wiki_routes.SIDEBAR_ASYNC_THRESHOLD
    wiki_routes.SIDEBAR_ASYNC_THRESHOLD = 1
    try:
        r = client.get("/@agent1/folder-sidebar/notes/")
        assert r.status_code == 200, f"folder fetch failed: {r.status_code}"
        html = r.data.decode()
    finally:
        wiki_routes.SIDEBAR_ASYNC_THRESHOLD = original_threshold

    assert 'data-url="/@agent1/folder-sidebar/sidebar.json?current=notes"' in html, (
        "wikihub-oud7 REGRESSION: folder.html async sidebar must pass the current "
        "folder path to sidebar.json so the active branch can be restored."
    )
    assert "wikihub-sidebar-folders:" in html and "agent1/folder-sidebar" in html, (
        "wikihub-oud7 REGRESSION: folder.html should namespace sidebar folder "
        "state by owner/wiki rather than sharing one global storage key."
    )


def test_relative_links_resolve_inside_wiki_on_subdomain(client, api_key):
    """wikihub-qmx6 REGRESSION: in-content relative links must resolve inside the
    wiki even on the canonical subdomain root (no trailing slash).

    Markdown like [Topics](topics) renders as a host-relative href="topics". On
    the subdomain form (agent1.wikihub.md/rel-links — NO trailing slash) a bare
    "topics" resolves against the host root -> /topics -> 404. The renderer must
    absolute-ize every relative in-content link to /@owner/slug/... so it works
    regardless of which host form (apex or subdomain) served the page.
    """
    import re as _re
    from html.parser import HTMLParser

    h = {"Authorization": f"Bearer {api_key}"}

    r = client.post("/api/v1/wikis", json={"slug": "rel-links", "title": "Rel Links"}, headers=h)
    assert r.status_code == 201

    # index page with several relative in-content links: a bare slug, a nested
    # relative path, and a relative .md link. All must become absolute.
    # (a fresh wiki auto-creates index.md, so PUT rather than POST.)
    r = client.put("/api/v1/wikis/agent1/rel-links/pages/index.md", json={
        "content": (
            "---\ntitle: Home\nvisibility: public\n---\n\n"
            "# Home\n\n"
            "- [Topics](topics)\n"
            "- [Deals](deals)\n"
            "- [Folder](folder/)\n"
            "- [Encoded](Foo%20Bar.md)\n"
            "- [Dotfile](..notes.md)\n"
            "- [Dotdir](..2026/plan)\n"
            "- [Notes](notes/scratch.md)\n"
            "- [External](https://example.com/topics)\n"
            "- [Upper External](HTTPS://example.com/upper)\n"
            "- [FTP](ftp://example.com/file)\n"
            "- [SMS](sms:+15551234567)\n"
            "- [Anchor](#home)\n"
        ),
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 200, f"index update failed: {r.status_code} {r.data[:200]}"
    for p in ("topics.md", "deals.md", "folder/index.md", "Foo Bar.md", "..notes.md", "..2026/plan.md", "notes/scratch.md"):
        r = client.post("/api/v1/wikis/agent1/rel-links/pages", json={
            "path": p,
            "content": f"---\ntitle: {p}\nvisibility: public\n---\n\n# {p}",
            "visibility": "public",
        }, headers=h)
        assert r.status_code == 201

    # Fetch the wiki ROOT via the subdomain host, no trailing slash — exactly the
    # live repro. The subdomain middleware rewrites it to /@agent1/rel-links.
    r = client.get("/rel-links", base_url="http://agent1.wikihub.md")
    assert r.status_code == 200, (
        f"subdomain wiki root should render, got {r.status_code} "
        "(subdomain middleware may not be rewriting the host)"
    )
    html = r.data.decode()

    # Extract ONLY the in-content article region — chrome links are separate.
    m = _re.search(r'<article class="article">(.*?)</article>', html, _re.DOTALL)
    assert m, "reader page missing <article class=\"article\"> content region"
    article = m.group(1)

    # Collect every href in the article body.
    hrefs = []

    class _Collect(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag == "a":
                for k, v in attrs:
                    if k == "href" and v is not None:
                        hrefs.append(v)

    _Collect().feed(article)

    # The relative page links must have been rewritten to absolute wiki paths.
    assert "/@agent1/rel-links/topics" in hrefs, f"expected absolute topics link, got {hrefs}"
    assert "/@agent1/rel-links/deals" in hrefs, f"expected absolute deals link, got {hrefs}"
    assert "/@agent1/rel-links/folder/" in hrefs, f"expected absolute folder link with trailing slash, got {hrefs}"
    assert "/@agent1/rel-links/Foo_Bar" in hrefs, f"expected decoded encoded-file link, got {hrefs}"
    assert "/@agent1/rel-links/..notes" in hrefs, f"expected dot-prefixed filename link, got {hrefs}"
    assert "/@agent1/rel-links/..2026/plan" in hrefs, f"expected dot-prefixed folder link, got {hrefs}"
    assert "/@agent1/rel-links/notes/scratch" in hrefs, f"expected absolute nested link, got {hrefs}"
    assert "HTTPS://example.com/upper" in hrefs, f"expected explicit scheme link untouched, got {hrefs}"
    assert "ftp://example.com/file" in hrefs, f"expected explicit scheme link untouched, got {hrefs}"
    assert "sms:+15551234567" in hrefs, f"expected explicit scheme link untouched, got {hrefs}"

    # Every in-content link must resolve inside the wiki: no bare host-relative
    # link (one that the browser would resolve against the host root) may survive.
    for href in hrefs:
        if _re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", href) or href.startswith("//"):
            continue  # external — fine
        if href.startswith("#"):
            continue  # in-page anchor — fine
        assert href.startswith("/@agent1/rel-links/"), (
            f"in-content link {href!r} is not absolute inside the wiki — it would "
            "resolve against the subdomain host root and 404 (wikihub-qmx6)."
        )


def test_wikipedia_urls(client, api_key):
    """Wikipedia-style URLs: underscores instead of %20, redirect %20 to underscore"""
    h = {"Authorization": f"Bearer {api_key}"}

    # use a dedicated wiki to avoid collisions
    r = client.post("/api/v1/wikis", json={"slug": "url-test", "title": "URL Test"}, headers=h)
    assert r.status_code == 201

    # create a page with spaces in the name
    r = client.post("/api/v1/wikis/agent1/url-test/pages", json={
        "path": "wiki/My Great Page.md",
        "content": "---\ntitle: My Great Page\nvisibility: public\n---\n\n# Hello",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201

    # access via underscore URL (Wikipedia-style)
    r = client.get("/@agent1/url-test/wiki/My_Great_Page")
    assert r.status_code == 200
    assert b"Hello" in r.data

    # access via %20 URL should 301 redirect to underscore URL
    r = client.get("/@agent1/url-test/wiki/My%20Great%20Page", follow_redirects=False)
    assert r.status_code == 301
    assert "My_Great_Page" in r.headers["Location"]

    # history via underscore URL (public, no auth needed)
    r = client.get("/@agent1/url-test/wiki/My_Great_Page/history")
    assert r.status_code == 200

    # create a page with literal underscores in the filename
    r = client.post("/api/v1/wikis/agent1/url-test/pages", json={
        "path": "wiki/kbhconvex_optimization.md",
        "content": "---\ntitle: Convex Optimization\nvisibility: public\n---\n\n# Convex Optimization",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201

    # access via literal underscore URL — should find the underscore file, not space fallback
    r = client.get("/@agent1/url-test/wiki/kbhconvex_optimization")
    assert r.status_code == 200
    assert b"Convex Optimization" in r.data


def test_sharing_lifecycle(client, api_key):
    """share a private page with another user, verify access, revoke, verify no access"""
    h = {"Authorization": f"Bearer {api_key}"}

    # use a dedicated wiki
    r = client.post("/api/v1/wikis", json={"slug": "share-test", "title": "Share Test"}, headers=h)
    assert r.status_code == 201

    # create a guest user
    r = client.post("/api/v1/accounts", json={"username": "guest1"})
    guest_key = r.get_json()["api_key"]
    hg = {"Authorization": f"Bearer {guest_key}"}

    # create a private page
    r = client.post("/api/v1/wikis/agent1/share-test/pages", json={
        "path": "sharing-test.md",
        "content": "# Secret\n\nSharing test content.",
        "visibility": "private",
    }, headers=h)
    assert r.status_code == 201

    # guest cannot read it
    r = client.get("/api/v1/wikis/agent1/share-test/pages/sharing-test.md", headers=hg)
    assert r.status_code in (403, 404)

    # owner shares with guest (page-level)
    r = client.post("/api/v1/wikis/agent1/share-test/share", json={
        "pattern": "sharing-test.md",
        "username": "guest1",
        "role": "read",
    }, headers=h)
    assert r.status_code == 200

    # guest can now read it
    r = client.get("/api/v1/wikis/agent1/share-test/pages/sharing-test.md", headers=hg)
    assert r.status_code == 200
    assert "Sharing test content" in r.get_json()["content"]

    # list grants
    r = client.get("/api/v1/wikis/agent1/share-test/grants", headers=h)
    assert r.status_code == 200
    grants = r.get_json()["grants"]
    assert any(g["username"] == "guest1" and g["role"] == "read" for g in grants)

    # page-level grants
    r = client.get("/api/v1/wikis/agent1/share-test/pages/sharing-test.md/grants", headers=h)
    assert r.status_code == 200
    assert any(g["username"] == "guest1" for g in r.get_json()["grants"])

    # shared-with-me from guest's perspective
    r = client.get("/api/v1/shared-with-me", headers=hg)
    assert r.status_code == 200
    shared = r.get_json()["shared"]
    assert any(s["wiki"] == "agent1/share-test" for s in shared)

    # owner revokes
    r = client.delete("/api/v1/wikis/agent1/share-test/share", json={
        "pattern": "sharing-test.md",
        "username": "guest1",
    }, headers=h)
    assert r.status_code == 200
    assert r.get_json()["revoked"] is True

    # guest can no longer read it
    r = client.get("/api/v1/wikis/agent1/share-test/pages/sharing-test.md", headers=hg)
    assert r.status_code in (403, 404)


def test_wiki_level_sharing(client, api_key):
    """wiki-level grant (*) gives access to all pages"""
    h = {"Authorization": f"Bearer {api_key}"}

    # use a dedicated wiki
    r = client.post("/api/v1/wikis", json={"slug": "wshare-test", "title": "Wiki Share Test"}, headers=h)
    assert r.status_code == 201

    r = client.post("/api/v1/accounts", json={"username": "wikiguest"})
    guest_key = r.get_json()["api_key"]
    hg = {"Authorization": f"Bearer {guest_key}"}

    # create two private pages
    for name in ("wiki-share-a.md", "wiki-share-b.md"):
        r = client.post("/api/v1/wikis/agent1/wshare-test/pages", json={
            "path": name,
            "content": f"# {name}\n\nPrivate content.",
            "visibility": "private",
        }, headers=h)
        assert r.status_code == 201

    # guest can't read either
    for name in ("wiki-share-a.md", "wiki-share-b.md"):
        r = client.get(f"/api/v1/wikis/agent1/wshare-test/pages/{name}", headers=hg)
        assert r.status_code in (403, 404)

    # share entire wiki with guest
    r = client.post("/api/v1/wikis/agent1/wshare-test/share", json={
        "pattern": "*",
        "username": "wikiguest",
        "role": "read",
    }, headers=h)
    assert r.status_code == 200

    # guest can now read both
    for name in ("wiki-share-a.md", "wiki-share-b.md"):
        r = client.get(f"/api/v1/wikis/agent1/wshare-test/pages/{name}", headers=hg)
        assert r.status_code == 200

    # revoke wiki-level grant
    r = client.delete("/api/v1/wikis/agent1/wshare-test/share", json={
        "pattern": "*",
        "username": "wikiguest",
    }, headers=h)
    assert r.status_code == 200

    # guest can't read again
    for name in ("wiki-share-a.md", "wiki-share-b.md"):
        r = client.get(f"/api/v1/wikis/agent1/wshare-test/pages/{name}", headers=hg)
        assert r.status_code in (403, 404)


def test_folder_level_sharing(client, api_key):
    """folder-level grant (folder/*) gives access to folder pages only"""
    h = {"Authorization": f"Bearer {api_key}"}

    # use a dedicated wiki
    r = client.post("/api/v1/wikis", json={"slug": "fshare-test", "title": "Folder Share Test"}, headers=h)
    assert r.status_code == 201

    r = client.post("/api/v1/accounts", json={"username": "folderguest"})
    guest_key = r.get_json()["api_key"]
    hg = {"Authorization": f"Bearer {guest_key}"}

    # create pages in and outside folder
    client.post("/api/v1/wikis/agent1/fshare-test/pages", json={
        "path": "research/paper.md",
        "content": "# Paper\n\nResearch content.",
        "visibility": "private",
    }, headers=h)
    client.post("/api/v1/wikis/agent1/fshare-test/pages", json={
        "path": "notes.md",
        "content": "# Notes\n\nPersonal notes.",
        "visibility": "private",
    }, headers=h)

    # share research folder only
    r = client.post("/api/v1/wikis/agent1/fshare-test/share", json={
        "pattern": "research/*",
        "username": "folderguest",
        "role": "read",
    }, headers=h)
    assert r.status_code == 200

    # guest can read research/paper.md
    r = client.get("/api/v1/wikis/agent1/fshare-test/pages/research/paper.md", headers=hg)
    assert r.status_code == 200

    # guest cannot read notes.md
    r = client.get("/api/v1/wikis/agent1/fshare-test/pages/notes.md", headers=hg)
    assert r.status_code in (403, 404)


def test_me_capabilities(client, api_key):
    """GET /api/v1/me/capabilities returns a full capability snapshot."""
    # auth required — flask_login caches current_user on `g`, which persists
    # across requests within the wrapping app_context() in this test harness.
    # Clearing it (and using a fresh client with no session cookie) gives us
    # a true unauthenticated request.
    from flask import g
    g.pop("_login_user", None)
    anon = client.application.test_client()
    r = anon.get("/api/v1/me/capabilities")
    assert r.status_code == 401

    # authenticated happy path
    h = {"Authorization": f"Bearer {api_key}"}
    r = client.get("/api/v1/me/capabilities", headers=h)
    assert r.status_code == 200
    data = r.get_json()
    assert data["username"] == "agent1"
    assert "user_id" in data
    assert isinstance(data["wikis"], list)
    # agent1 has at least their personal wiki by now
    assert any(w["role"] == "owner" for w in data["wikis"])
    rl = data["rate_limits"]
    assert "writes_per_minute" in rl
    assert "feedback_per_minute" in rl
    assert rl["writes_per_minute"]["limit"] >= 1
    assert "reset_at" in rl["writes_per_minute"]
    assert data["features"]["git_push"] is True
    assert data["quotas"]["max_wikis_per_user"] == client.application.config["MAX_WIKIS_PER_USER"]

    user = User.query.filter_by(username="agent1").one()
    user.wiki_limit = 321
    db.session.commit()
    r = client.get("/api/v1/me/capabilities", headers=h)
    assert r.status_code == 200
    data = r.get_json()
    assert data["quotas"]["max_wikis_per_user"] == 321
    user.wiki_limit = None
    db.session.commit()


def test_feedback_submission(client):
    """POST /api/v1/feedback accepts valid submissions and rejects bad ones."""
    # use a fresh client to avoid stale session cookies from prior tests
    # leaking into the anonymous feedback path
    anon = client.application.test_client()

    # happy path — anonymous bug report
    r = anon.post("/api/v1/feedback", json={
        "kind": "bug",
        "subject": "Something is broken",
        "body": "Here is a full description.",
        "context": {"page_url": "https://example.com/x", "wiki": "@alice/foo"},
    })
    assert r.status_code == 201, f"expected 201, got {r.status_code}: {r.data!r}"
    data = r.get_json()
    assert data["id"].startswith("fb_")
    assert data["status"] == "received"
    assert "received_at" in data

    # bad kind
    r = anon.post("/api/v1/feedback", json={
        "kind": "nonsense",
        "subject": "x",
        "body": "x",
    })
    assert r.status_code == 400
    err = r.get_json()
    assert err["error"] == "bad_request"
    assert err.get("field") == "kind"

    # oversized body
    r = anon.post("/api/v1/feedback", json={
        "kind": "comment",
        "subject": "x",
        "body": "A" * 10_001,
    })
    assert r.status_code == 400
    err = r.get_json()
    assert err.get("field") == "body"

    # missing subject
    r = anon.post("/api/v1/feedback", json={
        "kind": "praise",
        "body": "nice!",
    })
    assert r.status_code == 400


def test_api_root_discovery(client):
    """GET /api returns a discovery document pointing at v1."""
    r = client.get("/api")
    assert r.status_code == 200, f"/api returned {r.status_code}"
    assert "application/json" in r.content_type
    assert "public" in r.headers.get("Cache-Control", "")
    assert "max-age=300" in r.headers.get("Cache-Control", "")
    data = r.get_json()
    assert data["name"] == "wikihub"
    assert data["current_version"] == "v1"
    assert data["versions"]["v1"]["base"] == "/api/v1"
    assert data["versions"]["v1"]["capabilities"] == "/api/v1/me/capabilities"
    assert data["feedback"] == "/api/v1/feedback"
    assert data["request_id_header"] == "X-Request-ID"
    assert data["deprecated_versions"] == []

    # /api/ (trailing slash) also works
    r = client.get("/api/")
    assert r.status_code == 200

    # HEAD should respond too
    r = client.head("/api")
    assert r.status_code == 200


def test_frontmatter_title_renders_h1(client, api_key):
    """pages with only frontmatter title (no # heading) get an <h1> in rendered
    output — otherwise the page has a browser tab title but no visible heading
    (wikihub-3jb). Tests the renderer directly so it doesn't depend on
    pre-existing wiki-state from earlier tests."""
    from app.renderer import render_page
    import re as _re

    # frontmatter-only title, no body heading — should get an h1 prepended
    html = render_page("---\ntitle: Bayes Theorem\nvisibility: public\n---\n\nBody text.")
    assert _re.search(r'<h1[^>]*>\s*Bayes Theorem\s*</h1>', html), \
        f"frontmatter title should render as h1 when body has no heading; got: {html!r}"

    # body already has matching h1 — should NOT duplicate
    html = render_page("---\ntitle: With Heading\n---\n\n# With Heading\n\nBody.")
    h1_tags = _re.findall(r'<h1[^>]*>\s*With Heading\s*</h1>', html)
    assert len(h1_tags) == 1, f"expected exactly one h1 for matching title, got {len(h1_tags)}: {html!r}"

    # no frontmatter title, no body heading — no h1 should be added
    html = render_page("Just body text.")
    assert "<h1" not in html

    # frontmatter title + only h2 in body — h1 is still prepended
    html = render_page("---\ntitle: Doc Title\n---\n\n## Only H2\n\nText.")
    assert _re.search(r'<h1[^>]*>\s*Doc Title\s*</h1>', html)


def test_html_embed_inline_iframe(client, api_key):
    """![[file.html]] renders a sandboxed iframe ONLY when the owner opts the
    path in via .wikihub/serve-inline; otherwise it falls back to a plain link.
    Also covers wikihub-057 part 1: links to non-md files get target=_blank.
    (wikihub-wz2j)"""
    from app.renderer import render_page
    from app.git_sync import sync_page_to_repo, regenerate_public_mirror
    from app.wiki_ops import load_acl_rules
    import re as _re

    h = {"Authorization": f"Bearer {api_key}"}

    r = client.post("/api/v1/wikis", json={"slug": "embed-wiki", "title": "Embeds"}, headers=h)
    assert r.status_code == 201

    sync_page_to_repo("agent1", "embed-wiki", ".wikihub/acl", "* private\nwiki/** public\n")

    # store two decks; opt only the first one in
    for name in ("deck.html", "other.html"):
        r = client.post("/api/v1/wikis/agent1/embed-wiki/pages", json={
            "path": f"wiki/{name}",
            "content": "<!doctype html><h1>deck</h1>",
            "visibility": "public",
        }, headers=h)
        assert r.status_code == 201
    sync_page_to_repo("agent1", "embed-wiki", ".wikihub/serve-inline", "wiki/deck.html\n")
    regenerate_public_mirror("agent1", "embed-wiki", load_acl_rules("agent1", "embed-wiki"))

    # NOTE: render_page resolves the embed against the current page's directory,
    # so a page living at wiki/index.md embedding ![[deck.html]] resolves to
    # wiki/deck.html — exactly the opted-in path.
    md = (
        "---\ntitle: Slides\nvisibility: public\n---\n\n"
        "# Slides\n\n"
        "![[deck.html|600]]\n\n"
        "![[other.html]]\n"
    )
    html = render_page(md, wiki_owner="agent1", wiki_slug="embed-wiki",
                       current_page_path="wiki/index.md")

    # opted-in deck.html -> sandboxed iframe pointing at the standalone serve URL
    raw_url = "/@agent1/embed-wiki/wiki/deck.html"
    assert '<figure class="html-embed">' in html, f"expected html-embed figure; got: {html!r}"
    iframe_m = _re.search(r'<iframe [^>]*src="([^"]+)"[^>]*>', html)
    assert iframe_m, f"expected an iframe; got: {html!r}"
    assert iframe_m.group(1) == raw_url, f"iframe src wrong: {iframe_m.group(1)!r}"
    sandbox_m = _re.search(r'<iframe [^>]*sandbox="([^"]+)"', html)
    assert sandbox_m, "iframe must declare a sandbox"
    sandbox = sandbox_m.group(1)
    assert "allow-scripts" in sandbox and "allow-popups-to-escape-sandbox" in sandbox
    assert "allow-same-origin" not in sandbox, "null-origin isolation: no allow-same-origin"
    assert "height:600px" in html, "the |600 height hint should set iframe height"
    assert f'class="html-embed-open" href="{raw_url}" target="_blank"' in html, \
        "pop-out '↗ open' link should target a new tab"

    # NOT opted-in other.html -> plain link, NO iframe for it
    other_url = "/@agent1/embed-wiki/wiki/other.html"
    assert f'href="{other_url}"' in html
    # only one iframe total (deck), none for other.html
    assert html.count("<iframe") == 1, f"non-allowlisted html must not be iframed; got: {html!r}"

    # wikihub-057 part 1: the fallback link to other.html opens in a new tab
    link_m = _re.search(r'<a [^>]*href="' + _re.escape(other_url) + r'"[^>]*>', html)
    assert link_m and 'target="_blank"' in link_m.group(0), \
        f"non-md file link should open in new tab; got: {link_m.group(0) if link_m else None!r}"

    # render with NO wiki context (singleton path) must never produce an iframe
    html2 = render_page(md)
    assert "<iframe" not in html2, "no wiki context -> no iframe (safe fallback)"


def test_html_viewer_embedded_in_reader_chrome(client, api_key):
    """wikihub-ntpc: clicking an HTML deck in the sidebar opens an embedded
    VIEWER (deck inside reader chrome via a sandboxed iframe + pop-out) rather
    than the bare standalone deck. Additive: the raw .html URL (no ?view) is
    unchanged (wikihub-6ag), and the sidebar tree points .html entries at ?view=1.
    """
    import re as _re
    from app.git_sync import sync_page_to_repo, regenerate_public_mirror
    from app.wiki_ops import load_acl_rules
    from app.routes.wiki import _build_sidebar_tree
    from app.models import Wiki, User

    h = {"Authorization": f"Bearer {api_key}"}

    r = client.post("/api/v1/wikis", json={"slug": "viewer-wiki", "title": "Viewer"}, headers=h)
    assert r.status_code == 201
    sync_page_to_repo("agent1", "viewer-wiki", ".wikihub/acl", "* private\nwiki/** public\n")

    r = client.post("/api/v1/wikis/agent1/viewer-wiki/pages", json={
        "path": "wiki/deck.html",
        "content": "<!doctype html><h1>interactive deck</h1>",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201

    # opt the deck in for inline serving, then publish the mirror
    sync_page_to_repo("agent1", "viewer-wiki", ".wikihub/serve-inline", "wiki/deck.html\n")
    regenerate_public_mirror("agent1", "viewer-wiki", load_acl_rules("agent1", "viewer-wiki"))

    raw_url = "/@agent1/viewer-wiki/wiki/deck.html"

    # (c) plain .html (no ?view) still serves the bare deck inline (6ag UNCHANGED)
    r = client.get(raw_url)
    assert r.status_code == 200
    assert r.content_type.startswith("text/html"), f"raw .html should serve inline, got {r.content_type}"
    csp = r.headers.get("Content-Security-Policy", "")
    assert "sandbox" in csp and "allow-scripts" in csp, "raw serve must keep CSP sandbox"
    assert b"interactive deck" in r.data, "raw serve must return the actual deck body"

    # (a) ?view=1 on an opted-in deck renders reader chrome + a sandboxed iframe
    #     whose src is the raw .html URL (no ?view).
    r = client.get(raw_url + "?view=1")
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    # reader chrome present (the breadcrumb owner link is unique to reader/folder shells)
    assert "@agent1" in body and 'class="breadcrumb"' in body, "viewer must render full reader chrome"
    # the deck body itself must NOT be inlined (it lives behind the sandboxed iframe)
    assert "interactive deck" not in body, "viewer must embed via iframe, not inline the deck body"
    iframe_m = _re.search(r'<iframe [^>]*src="([^"]+)"[^>]*>', body)
    assert iframe_m, f"viewer must contain an iframe; got: {body[:400]!r}"
    assert iframe_m.group(1) == raw_url, f"iframe src must be the raw .html URL (no ?view); got {iframe_m.group(1)!r}"
    sandbox_m = _re.search(r'<iframe [^>]*sandbox="([^"]+)"', body)
    assert sandbox_m, "viewer iframe must declare a sandbox"
    sandbox = sandbox_m.group(1)
    assert "allow-scripts" in sandbox and "allow-popups-to-escape-sandbox" in sandbox
    assert "allow-same-origin" not in sandbox, "null-origin isolation: no allow-same-origin"
    # pop-out '↗ open' targets the raw deck in a new tab
    assert f'class="html-embed-open" href="{raw_url}" target="_blank"' in body, \
        "viewer must offer an '↗ open full' pop-out to the raw deck"

    # (b) the sidebar tree gives the .html entry a URL ending in ?view=1
    owner = User.query.filter_by(username="agent1").first()
    wiki = Wiki.query.filter_by(owner_id=owner.id, slug="viewer-wiki").first()
    tree = _build_sidebar_tree("agent1", "viewer-wiki", wiki, public=False)

    def find_item(items, path):
        for item in items:
            if item.get("path") == path:
                return item
            found = find_item(item.get("children") or [], path)
            if found:
                return found
        return None

    deck = find_item(tree, "wiki/deck.html")
    assert deck is not None, "deck.html should appear in the sidebar tree"
    assert deck["url"].endswith("?view=1"), f"sidebar .html URL should point at the viewer; got {deck['url']!r}"
    assert deck["url"] == raw_url + "?view=1"

    # a non-opted-in .html falls through to raw serving even with ?view=1
    r = client.post("/api/v1/wikis/agent1/viewer-wiki/pages", json={
        "path": "wiki/other.html",
        "content": "<!doctype html><h1>not opted in</h1>",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201
    r = client.get("/@agent1/viewer-wiki/wiki/other.html?view=1")
    assert r.status_code == 200
    # not opted-in -> default safe download (no reader chrome, no iframe)
    assert "application/octet-stream" in r.content_type, \
        "non-opted-in .html?view must fall through to the safe default serving"


def test_soft_line_breaks_render_as_visual_break():
    """single newlines inside a paragraph must produce a visual line break
    (wikihub-eiv7). strict commonmark would collapse them to spaces, which
    surprises users writing one-line-per-thought (Obsidian/chat style).
    Reported when a supplement list on /jacobcole/health/Sleep rendered as
    one wall of text.

    We emit a structural span (display:block) rather than <br> because
    Cloudflare's HTML transforms on this zone strip <br> tags. Either form
    satisfies the user-visible contract; this test passes for either,
    and fails if breaks=true is reverted (no break element at all)."""
    from app.renderer import render_markdown

    src = "Line one.\nLine two.\nLine three."
    html = render_markdown(src)
    # accept any form of line-break element; the renderer emits <br> by default,
    # but a future CF-bypass workaround might use a different element.
    break_count = html.count("<br") + html.count('class="md-line-break"')
    assert break_count >= 2, \
        f"expected at least 2 line-break elements between 3 lines; got: {html!r}"
    assert "Line one." in html and "Line two." in html and "Line three." in html

    # blank-line-separated paragraphs still produce separate <p> blocks (no regression)
    html = render_markdown("Para one.\n\nPara two.")
    assert html.count("<p>") == 2, f"expected two <p> blocks, got: {html!r}"


def test_admin_claude_auth_page_requires_token(client):
    """anonymous GET to /api/v1/admin/claude-auth must not expose admin HTML
    (wikihub-6q3). Return 404 to avoid leaking that the route exists."""
    anon = client.application.test_client()
    r = anon.get("/api/v1/admin/claude-auth")
    assert r.status_code == 404, f"anonymous admin page should 404, got {r.status_code}"

    # with the right admin token, the page loads
    r = anon.get("/api/v1/admin/claude-auth?token=test-admin-token")
    assert r.status_code == 200


def test_history_api_with_anon_and_deleted_page(client, api_key):
    """regression: GET /api/v1/wikis/<owner>/<slug>/history must not 500 when
    the repo has multiple commits including an anonymous one and a page that
    no longer exists in HEAD (wikihub-855)."""
    h = {"Authorization": f"Bearer {api_key}"}

    # dedicated wiki
    r = client.post("/api/v1/wikis", json={"slug": "history-bug", "title": "History Bug"}, headers=h)
    assert r.status_code == 201

    # commit 1 (authored): create a public-edit page so anon can edit it
    r = client.post("/api/v1/wikis/agent1/history-bug/pages", json={
        "path": "wiki/open.md",
        "content": "---\ntitle: Open\nvisibility: public-edit\n---\n\nInitial.",
        "visibility": "public-edit",
    }, headers=h)
    assert r.status_code == 201

    # commit 2 (anonymous): edit the same page without auth
    r = client.put("/api/v1/wikis/agent1/history-bug/pages/wiki/open.md", json={
        "content": "# Open\n\nEdited anonymously.",
    })
    assert r.status_code == 200

    # commit 3 (authored): create a public page that we'll later delete
    r = client.post("/api/v1/wikis/agent1/history-bug/pages", json={
        "path": "wiki/doomed.md",
        "content": "---\ntitle: Doomed\nvisibility: public\n---\n\nWill be gone.",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201

    # commit 4 (authored): delete the doomed page — it no longer exists in HEAD
    r = client.delete("/api/v1/wikis/agent1/history-bug/pages/wiki/doomed.md", headers=h)
    assert r.status_code == 204

    # now hit the history endpoint — the bug: this used to 500
    r = client.get("/api/v1/wikis/agent1/history-bug/history", headers=h)
    assert r.status_code == 200, f"history returned {r.status_code}: {r.data!r}"
    data = r.get_json()
    assert "commits" in data
    assert "total" in data
    assert len(data["commits"]) >= 3, f"expected multiple commits, got {len(data['commits'])}"

    # every commit must have the expected shape — this is what was crashing
    for c in data["commits"]:
        assert isinstance(c.get("sha"), str) and len(c["sha"]) == 40
        assert isinstance(c.get("author"), str) and c["author"]
        assert isinstance(c.get("date"), str)
        assert isinstance(c.get("message"), str)
        assert isinstance(c.get("files_changed"), list)

    # anonymous client (no auth) must also be able to read public history
    anon = client.application.test_client()
    r = anon.get("/api/v1/wikis/agent1/history-bug/history")
    assert r.status_code == 200, f"anon history returned {r.status_code}: {r.data!r}"

    # filter by a specific file must also survive the same shape
    r = client.get("/api/v1/wikis/agent1/history-bug/history?path=wiki/open.md", headers=h)
    assert r.status_code == 200


def test_api_cors_headers(client, api_key):
    """CORS headers on API responses (wikihub-gzj).

    - OPTIONS preflight returns the expected Allow-* headers
    - GET on a public API endpoint returns Access-Control-Allow-Origin
    """
    # OPTIONS preflight — ask about all three headers we care about
    r = client.open(
        "/api/v1/wikis",
        method="OPTIONS",
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization,Content-Type,X-Request-ID",
        },
    )
    assert r.status_code in (200, 204), f"preflight returned {r.status_code}"
    allow_origin = r.headers.get("Access-Control-Allow-Origin")
    assert allow_origin in ("*", "https://example.com"), \
        f"preflight Access-Control-Allow-Origin missing/wrong: {allow_origin!r}"
    allow_methods = (r.headers.get("Access-Control-Allow-Methods") or "").upper()
    for m in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
        assert m in allow_methods, f"preflight missing method {m}: {allow_methods!r}"
    allow_headers = (r.headers.get("Access-Control-Allow-Headers") or "").lower()
    for hname in ("authorization", "content-type", "x-request-id"):
        assert hname in allow_headers, f"preflight missing header {hname}: {allow_headers!r}"

    # GET on a public API endpoint — /api is public discovery
    r = client.get("/api", headers={"Origin": "https://example.com"})
    assert r.status_code == 200
    assert r.headers.get("Access-Control-Allow-Origin") in ("*", "https://example.com"), \
        f"GET /api did not return Access-Control-Allow-Origin: {dict(r.headers)!r}"

    # a GET to a list endpoint should also include CORS headers
    r = client.get("/api/v1/wikis", headers={"Origin": "https://example.com"})
    assert r.status_code == 200
    assert r.headers.get("Access-Control-Allow-Origin") in ("*", "https://example.com")

    # exposed headers are advertised via Access-Control-Expose-Headers
    expose = (r.headers.get("Access-Control-Expose-Headers") or "").lower()
    for hname in ("x-request-id", "x-ratelimit-remaining", "x-ratelimit-reset"):
        assert hname in expose, f"expose-headers missing {hname}: {expose!r}"


def test_list_wikis_api(client, api_key):
    """GET /api/v1/wikis (wikihub-bh4):
    - anonymous sees only public wikis
    - authed user also sees their own private wikis
    - owner filter scopes to one user
    - pagination works
    """
    # set up an alice user with a mix of public and private wikis
    r = client.post("/api/v1/accounts", json={"username": "alice"})
    assert r.status_code == 201
    alice_key = r.get_json()["api_key"]
    ha = {"Authorization": f"Bearer {alice_key}"}

    r = client.post("/api/v1/wikis", json={"slug": "alice-pub", "title": "Alice Pub"}, headers=ha)
    assert r.status_code == 201
    r = client.put("/api/v1/wikis/alice/alice-pub/pages/index.md", json={
        "content": "---\ntitle: Alice Pub\nvisibility: public\n---\n\nPublic alice.",
        "visibility": "public",
    }, headers=ha)
    assert r.status_code == 200

    r = client.post("/api/v1/wikis", json={"slug": "alice-vault", "title": "Alice Vault"}, headers=ha)
    assert r.status_code == 201
    r = client.put("/api/v1/wikis/alice/alice-vault/pages/index.md", json={
        "content": "---\ntitle: Alice Vault\nvisibility: private\n---\n\nSecret alice.",
        "visibility": "private",
    }, headers=ha)
    assert r.status_code == 200

    # --- anonymous: only public wikis ---
    anon = client.application.test_client()
    r = anon.get("/api/v1/wikis")
    assert r.status_code == 200
    data = r.get_json()
    assert "wikis" in data and "total" in data and "limit" in data and "offset" in data
    owners_slugs = {(w["owner"], w["name"]) for w in data["wikis"]}
    assert ("alice", "alice-pub") in owners_slugs
    assert ("alice", "alice-vault") not in owners_slugs, "anonymous must not see private wikis"

    # --- authed alice: also sees her own vault ---
    r = client.get("/api/v1/wikis", headers=ha)
    assert r.status_code == 200
    data = r.get_json()
    owners_slugs = {(w["owner"], w["name"]) for w in data["wikis"]}
    assert ("alice", "alice-pub") in owners_slugs
    assert ("alice", "alice-vault") in owners_slugs, "authed owner should see her private wiki"

    # --- owner filter ---
    r = anon.get("/api/v1/wikis?owner=alice")
    assert r.status_code == 200
    data = r.get_json()
    assert len(data["wikis"]) >= 1
    assert all(w["owner"] == "alice" for w in data["wikis"])

    # --- pagination ---
    r = anon.get("/api/v1/wikis?limit=1&offset=0")
    assert r.status_code == 200
    data = r.get_json()
    assert data["limit"] == 1
    assert data["offset"] == 0
    assert len(data["wikis"]) <= 1
    assert data["total"] >= 1

    # limit clamps to max 200 when exceeded
    r = anon.get("/api/v1/wikis?limit=9999")
    assert r.status_code == 200
    assert r.get_json()["limit"] == 200

    # slug is the @owner/name form
    r = anon.get("/api/v1/wikis?owner=alice")
    assert r.status_code == 200
    for w in r.get_json()["wikis"]:
        assert w["slug"] == f"@{w['owner']}/{w['name']}"
def test_bulk_sharing(client, api_key):
    """bulk share grants access to multiple users in one call; idempotent; reports failures."""
    h = {"Authorization": f"Bearer {api_key}"}

    r = client.post("/api/v1/wikis", json={"slug": "bulk-share-test", "title": "Bulk Share"}, headers=h)
    assert r.status_code == 201

    # create three guest accounts — two with emails, one without
    client.post("/api/v1/accounts", json={"username": "bulka", "email": "bulka@example.com"})
    client.post("/api/v1/accounts", json={"username": "bulkb", "email": "bulkb@example.com"})
    r = client.post("/api/v1/accounts", json={"username": "bulkc"})
    guest_c_key = r.get_json()["api_key"]
    hc = {"Authorization": f"Bearer {guest_c_key}"}

    # a private page everyone should be able to read once granted
    r = client.post("/api/v1/wikis/agent1/bulk-share-test/pages", json={
        "path": "secret.md", "content": "# secret", "visibility": "private",
    }, headers=h)
    assert r.status_code == 201

    # first bulk: mix of username + email + one nonexistent
    r = client.post("/api/v1/wikis/agent1/bulk-share-test/share/bulk", json={
        "grants": [
            {"username": "bulka", "role": "read"},
            {"email": "bulkb@example.com", "role": "read"},
            {"username": "bulkc", "role": "edit"},
            {"username": "nobody-xyz", "role": "read"},
        ],
        "pattern": "*",
    }, headers=h)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert len(body["added"]) == 3, body
    assert len(body["failed"]) == 1, body
    assert body["failed"][0]["input"] == "nobody-xyz"
    added_users = {g["username"] for g in body["added"]}
    assert added_users == {"bulka", "bulkb", "bulkc"}

    # all three can now read the private page
    for u, k in [("bulkc", guest_c_key)]:
        r = client.get("/api/v1/wikis/agent1/bulk-share-test/pages/secret.md",
                       headers={"Authorization": f"Bearer {k}"})
        assert r.status_code == 200, f"{u} should have read access"

    # second bulk: re-adding same grants should all show up as skipped (idempotent)
    r = client.post("/api/v1/wikis/agent1/bulk-share-test/share/bulk", json={
        "grants": [
            {"username": "bulka", "role": "read"},
            {"username": "bulkb", "role": "read"},
        ],
        "pattern": "*",
    }, headers=h)
    assert r.status_code == 200
    body = r.get_json()
    assert len(body["added"]) == 0
    assert len(body["skipped"]) == 2

    # bad role rejected as failed, other entries still added
    r = client.post("/api/v1/wikis/agent1/bulk-share-test/share/bulk", json={
        "grants": [
            {"username": "bulka", "role": "admin"},
            {"username": "bulkb", "role": "edit"},  # new role → new line, still "added"
        ],
        "pattern": "docs/*",
    }, headers=h)
    assert r.status_code == 200
    body = r.get_json()
    assert any(f["input"] == "bulka" and "role" in f["error"] for f in body["failed"]), body
    assert any(g["username"] == "bulkb" and g["pattern"] == "docs/*" for g in body["added"]), body

    # empty grants array rejected
    r = client.post("/api/v1/wikis/agent1/bulk-share-test/share/bulk", json={"grants": []}, headers=h)
    assert r.status_code == 400

    # non-owner forbidden
    r = client.post("/api/v1/wikis/agent1/bulk-share-test/share/bulk", json={
        "grants": [{"username": "bulka", "role": "read"}], "pattern": "*",
    }, headers=hc)
    assert r.status_code == 403


def test_suggested_edit_proposal_flow(client, api_key):
    """wikihub-b6lc: users can suggest edits, then owners accept/reject them."""
    h = {"Authorization": f"Bearer {api_key}"}

    r = client.post("/api/v1/accounts", json={"username": "suggestor", "password": "testpass12345"})
    assert r.status_code == 201

    r = client.post("/api/v1/wikis", json={"slug": "suggest-test", "title": "Suggest Test"}, headers=h)
    assert r.status_code == 201
    original = "---\ntitle: Public Draft\nvisibility: public-edit\n---\n\n# Public Draft\n\nOriginal line."
    r = client.post("/api/v1/wikis/agent1/suggest-test/pages", json={
        "path": "draft.md",
        "content": original,
        "visibility": "public-edit",
    }, headers=h)
    assert r.status_code == 201

    browser = client
    r = browser.post(
        "/auth/login",
        data={"username": "suggestor", "password": "testpass12345"},
        follow_redirects=False,
    )
    assert r.status_code == 302

    r = browser.get("/@agent1/suggest-test/draft")
    assert r.status_code == 200
    assert b"/-/suggest/draft" in r.data
    assert b"/@agent1/suggest-test/draft/edit" in r.data, "public-edit should still allow direct edits"

    proposed = "---\ntitle: Public Draft\nvisibility: private\n---\n\n# Public Draft\n\nSuggested replacement."
    r = browser.post("/@agent1/suggest-test/-/suggest/draft", data={
        "title": "Tighten draft",
        "note": "Cleaner phrasing.",
        "content": proposed,
    }, follow_redirects=False)
    assert r.status_code in (302, 303), r.get_data(as_text=True)[:200]
    proposal_path = urlparse(r.headers["Location"]).path
    assert "/-/proposals/" in proposal_path

    # The live page is not overwritten until the owner accepts the proposal.
    r = client.get("/api/v1/wikis/agent1/suggest-test/pages/draft.md", headers=h)
    assert r.status_code == 200
    assert "Original line." in r.get_json()["content"]
    assert "Suggested replacement." not in r.get_json()["content"]

    r = browser.get(f"/auth/login?api_key={api_key}&next={proposal_path}", follow_redirects=False)
    assert r.status_code == 302
    r = browser.get(proposal_path)
    assert r.status_code == 200
    assert b"Tighten draft" in r.data
    assert b"Cleaner phrasing." in r.data
    assert b"Suggested replacement." in r.data

    r = browser.post(proposal_path + "/accept", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert r.headers["Location"].endswith("/@agent1/suggest-test/draft")

    r = client.get("/api/v1/wikis/agent1/suggest-test/pages/draft.md", headers=h)
    assert r.status_code == 200
    accepted = r.get_json()["content"]
    assert "Suggested replacement." in accepted
    assert "visibility: public-edit" in accepted
    assert "visibility: private" not in accepted, "suggestions must not change visibility"

    # Rejection path: another suggestion is stored but never applied.
    r = browser.post("/@agent1/suggest-test/-/suggest/draft", data={
        "title": "Rejected draft",
        "note": "Do not merge this.",
        "content": accepted.replace("Suggested replacement.", "Rejected replacement."),
    }, follow_redirects=False)
    assert r.status_code in (302, 303)
    reject_path = urlparse(r.headers["Location"]).path
    r = browser.get(f"/auth/login?api_key={api_key}&next={reject_path}", follow_redirects=False)
    assert r.status_code == 302
    r = browser.post(reject_path + "/reject", follow_redirects=False)
    assert r.status_code in (302, 303)

    r = client.get("/api/v1/wikis/agent1/suggest-test/pages/draft.md", headers=h)
    assert r.status_code == 200
    assert "Rejected replacement." not in r.get_json()["content"]


def test_proposal_comments_and_revision_flow(client, api_key):
    """wikihub-7cus: owners request changes, authors resubmit, owner accepts latest."""
    h = {"Authorization": f"Bearer {api_key}"}

    r = client.post("/api/v1/accounts", json={"username": "reviewer2", "password": "testpass12345"})
    assert r.status_code == 201

    r = client.post("/api/v1/wikis", json={"slug": "review-flow", "title": "Review Flow"}, headers=h)
    assert r.status_code == 201
    original = "---\ntitle: Review Draft\nvisibility: public-edit\n---\n\n# Review Draft\n\nOriginal review line."
    r = client.post("/api/v1/wikis/agent1/review-flow/pages", json={
        "path": "draft.md",
        "content": original,
        "visibility": "public-edit",
    }, headers=h)
    assert r.status_code == 201

    browser = client
    r = browser.post(
        "/auth/login",
        data={"username": "reviewer2", "password": "testpass12345"},
        follow_redirects=False,
    )
    assert r.status_code == 302

    r = browser.post("/@agent1/review-flow/-/suggest/draft", data={
        "title": "Reviewable suggestion",
        "note": "First pass.",
        "content": original.replace("Original review line.", "First suggested line."),
    }, follow_redirects=False)
    assert r.status_code in (302, 303)
    proposal_path = urlparse(r.headers["Location"]).path

    r = browser.get(f"/auth/login?api_key={api_key}&next={proposal_path}", follow_redirects=False)
    assert r.status_code == 302
    r = browser.post(proposal_path + "/request-changes", data={
        "body": "Please include the source note.",
    }, follow_redirects=False)
    assert r.status_code in (302, 303)

    r = browser.get(proposal_path)
    assert r.status_code == 200
    assert b"changes_requested" in r.data
    assert b"Please include the source note." in r.data

    r = browser.post(
        "/auth/login",
        data={"username": "reviewer2", "password": "testpass12345"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    r = browser.get(proposal_path)
    assert r.status_code == 200
    assert b"Submit revision" in r.data
    r = browser.post(proposal_path + "/comment", data={"body": "Revision incoming."}, follow_redirects=False)
    assert r.status_code in (302, 303)
    second = original.replace("Original review line.", "Second suggested line with source.")
    r = browser.post(proposal_path + "/resubmit", data={
        "note": "Added the source note.",
        "content": second,
    }, follow_redirects=False)
    assert r.status_code in (302, 303)

    r = client.get("/api/v1/wikis/agent1/review-flow/pages/draft.md", headers=h)
    assert r.status_code == 200
    assert "Original review line." in r.get_json()["content"]
    assert "Second suggested line" not in r.get_json()["content"]

    r = browser.get(f"/auth/login?api_key={api_key}&next={proposal_path}", follow_redirects=False)
    assert r.status_code == 302
    r = browser.get(proposal_path)
    assert r.status_code == 200
    assert b"pending" in r.data
    assert b"Added the source note." in r.data
    assert b"Second suggested line with source." in r.data
    assert b"Revision incoming." in r.data

    r = browser.post(proposal_path + "/accept", follow_redirects=False)
    assert r.status_code in (302, 303)

    r = client.get("/api/v1/wikis/agent1/review-flow/pages/draft.md", headers=h)
    assert r.status_code == 200
    content = r.get_json()["content"]
    assert "Second suggested line with source." in content
    assert "First suggested line." not in content


def test_pending_invite_lifecycle(client, api_key):
    """share by email before the user exists → PendingInvite stashed → user signs up
    via Google (auto-verified email) → invite materializes as a real ACL grant and
    the user can read the private page."""
    from app.models import PendingInvite, User, utcnow
    from app import db
    from app.wiki_ops import materialize_pending_invites_for
    h = {"Authorization": f"Bearer {api_key}"}

    r = client.post("/api/v1/wikis", json={"slug": "invite-test", "title": "Invite Test"}, headers=h)
    assert r.status_code == 201
    r = client.post("/api/v1/wikis/agent1/invite-test/pages", json={
        "path": "secret.md", "content": "# secret", "visibility": "private",
    }, headers=h)
    assert r.status_code == 201

    # share to an email that has no account yet
    r = client.post("/api/v1/wikis/agent1/invite-test/share", json={
        "pattern": "*", "email": "future-user@example.com", "role": "edit",
    }, headers=h)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body.get("invited") == "future-user@example.com"
    assert body.get("role") == "edit"

    # PendingInvite row created — and it has a random token
    pending = PendingInvite.query.filter_by(email="future-user@example.com").all()
    assert len(pending) == 1
    assert pending[0].pattern == "*" and pending[0].role == "edit"
    assert pending[0].token and len(pending[0].token) >= 32, "invite token must be set (wikihub-yjsv)"

    # /grants exposes both granted and pending
    r = client.get("/api/v1/wikis/agent1/invite-test/grants", headers=h)
    assert r.status_code == 200
    g = r.get_json()
    assert len(g["pending"]) == 1 and g["pending"][0]["email"] == "future-user@example.com"

    # bulk share to the same email is idempotent
    r = client.post("/api/v1/wikis/agent1/invite-test/share/bulk", json={
        "grants": [{"email": "future-user@example.com", "role": "edit"}],
        "pattern": "*",
    }, headers=h)
    assert r.status_code == 200
    assert len(PendingInvite.query.filter_by(email="future-user@example.com").all()) == 1

    # user signs up with password — no email verification yet, so invite should NOT apply
    r = client.post("/api/v1/accounts", json={
        "username": "future1",
        "email": "future-user@example.com",
        "password": "testpass12345",
    })
    assert r.status_code == 201
    future_key = r.get_json()["api_key"]
    hf = {"Authorization": f"Bearer {future_key}"}

    r = client.get("/api/v1/wikis/agent1/invite-test/pages/secret.md", headers=hf)
    assert r.status_code in (403, 404), "unverified email must NOT unlock pending invite"
    assert len(PendingInvite.query.filter_by(email="future-user@example.com").all()) == 1, \
        "pending invite should still be waiting"

    # simulate email verification (Google OAuth path in prod, direct flag here)
    future = User.query.filter_by(username="future1").first()
    future.email_verified_at = utcnow()
    db.session.commit()
    applied = materialize_pending_invites_for(future)
    db.session.commit()
    assert len(applied) == 1

    # PendingInvite row consumed
    assert PendingInvite.query.filter_by(email="future-user@example.com").count() == 0

    # user now has edit access
    r = client.get("/api/v1/wikis/agent1/invite-test/pages/secret.md", headers=hf)
    assert r.status_code == 200

    # /grants no longer shows the pending entry but shows the materialized grant
    r = client.get("/api/v1/wikis/agent1/invite-test/grants", headers=h)
    assert r.status_code == 200
    g = r.get_json()
    assert not g["pending"]
    assert any(row["username"] == "future1" and row["role"] == "edit" for row in g["grants"])

    # revoke a fresh pending invite by email (before signup) via DELETE
    r = client.post("/api/v1/wikis/agent1/invite-test/share", json={
        "pattern": "*", "email": "another@example.com", "role": "read",
    }, headers=h)
    assert r.status_code == 200
    r = client.delete("/api/v1/wikis/agent1/invite-test/share", json={
        "pattern": "*", "email": "another@example.com",
    }, headers=h)
    assert r.status_code == 200
    assert r.get_json()["revoked"] is True
    assert PendingInvite.query.filter_by(email="another@example.com").count() == 0

    # --- wikihub-yjsv: one-click invite verification via the form signup path ---
    # create a fresh invite, simulate the user clicking the invite link and
    # submitting the signup form WITH the token. Their email should be
    # auto-verified and the invite materialized — no separate verify round-trip.
    r = client.post("/api/v1/wikis/agent1/invite-test/share", json={
        "pattern": "*", "email": "oneclick@example.com", "role": "edit",
    }, headers=h)
    assert r.status_code == 200
    row = PendingInvite.query.filter_by(email="oneclick@example.com").first()
    assert row and row.token, "invite token must be present"
    invite_token = row.token

    # form signup carrying the invite token (hidden input)
    r = client.post("/auth/signup", data={
        "username": "oneclick",
        "email": "oneclick@example.com",
        "password": "testpass12345",
        "it": invite_token,
    }, follow_redirects=False)
    assert r.status_code in (302, 303), r.get_data(as_text=True)[:200]
    user = User.query.filter_by(username="oneclick").first()
    assert user and user.email_verified_at is not None, "valid token must auto-verify"
    assert PendingInvite.query.filter_by(email="oneclick@example.com").count() == 0, \
        "invite should materialize on token-backed signup"

    # negative: signup WITHOUT the token must NOT auto-verify
    r = client.post("/api/v1/wikis/agent1/invite-test/share", json={
        "pattern": "*", "email": "notoken@example.com", "role": "read",
    }, headers=h)
    assert r.status_code == 200
    r = client.post("/auth/signup", data={
        "username": "notoken",
        "email": "notoken@example.com",
        "password": "testpass12345",
        # deliberately no 'it' field
    }, follow_redirects=False)
    assert r.status_code in (302, 303)
    notoken = User.query.filter_by(username="notoken").first()
    assert notoken and notoken.email_verified_at is None, \
        "tokenless signup must NOT auto-verify (yjsv security invariant)"
    assert PendingInvite.query.filter_by(email="notoken@example.com").count() == 1, \
        "tokenless signup leaves the invite pending for the real verify flow"

    # negative: wrong token must NOT auto-verify either
    r = client.post("/api/v1/wikis/agent1/invite-test/share", json={
        "pattern": "*", "email": "badtoken@example.com", "role": "read",
    }, headers=h)
    assert r.status_code == 200
    r = client.post("/auth/signup", data={
        "username": "badtoken",
        "email": "badtoken@example.com",
        "password": "testpass12345",
        "it": "obviously-wrong-token",
    }, follow_redirects=False)
    assert r.status_code in (302, 303)
    bad = User.query.filter_by(username="badtoken").first()
    assert bad and bad.email_verified_at is None, "wrong token must NOT auto-verify"


def test_share_sends_email(client, api_key):
    """share endpoints emit share-invite emails via email_service (mock mode)."""
    import os
    os.environ["EMAIL_MODE"] = "mock"
    from app import email_service
    email_service.mock_clear()

    h = {"Authorization": f"Bearer {api_key}"}
    client.post("/api/v1/wikis", json={"slug": "email-test", "title": "Email Share Test"}, headers=h)
    client.post("/api/v1/accounts", json={"username": "notify1", "email": "notify1@example.com"})

    # existing user: should get an 'X shared a wiki' email
    r = client.post("/api/v1/wikis/agent1/email-test/share", json={
        "pattern": "*", "username": "notify1", "role": "read",
    }, headers=h)
    assert r.status_code == 200

    # pending email: should get a 'sign up to get access' email
    r = client.post("/api/v1/wikis/agent1/email-test/share", json={
        "pattern": "*", "email": "future2@example.com", "role": "edit",
    }, headers=h)
    assert r.status_code == 200

    # filter out the email-verification email triggered by creating notify1
    # with an email (wikihub-ks5t.3); this test only cares about share emails.
    outbox = [m for m in email_service.mock_outbox() if "Verify" not in m["subject"]]
    assert len(outbox) == 2, outbox
    tos = {m["to"] for m in outbox}
    assert tos == {"notify1@example.com", "future2@example.com"}

    # pending email template should link to signup
    pending_email = next(m for m in outbox if m["to"] == "future2@example.com")
    assert "signup" in pending_email["text"].lower() or "create your" in pending_email["html"].lower()

    os.environ.pop("EMAIL_MODE", None)


def test_permission_error_offers_request_access(client, api_key):
    h = {"Authorization": f"Bearer {api_key}"}

    r = client.post("/api/v1/wikis", json={"slug": "private-cta", "title": "Private CTA"}, headers=h)
    assert r.status_code == 201

    r = client.post("/api/v1/wikis/agent1/private-cta/pages", json={
        "path": "team/secret.md",
        "content": "# Secret\n\nPrivate body.",
        "visibility": "private",
    }, headers=h)
    assert r.status_code == 201

    # wikihub-dkp8: existing-but-private page → restricted (403), and the
    # restricted screen still offers the request-access affordance.
    r = client.get("/@agent1/private-cta/team/secret")
    assert r.status_code == 403
    assert b"This page is restricted" in r.data
    assert b"Request access" in r.data
    assert b"/api/v1/access-requests" in r.data


def test_access_request_constant_response_and_notify_existing_target(client):
    import os
    from app.routes.api import _access_request_timestamps

    os.environ["EMAIL_MODE"] = "mock"
    from app import email_service
    email_service.mock_clear()
    _write_timestamps.clear()
    _access_request_timestamps.clear()

    r = client.post("/api/v1/accounts", json={"username": "ownerreq", "email": "ownerreq@example.com"})
    assert r.status_code == 201
    key = r.get_json()["api_key"]
    h = {"Authorization": f"Bearer {key}"}

    r = client.post("/api/v1/wikis", json={"slug": "access-req", "title": "Access Request Wiki"}, headers=h)
    assert r.status_code == 201

    r = client.post("/api/v1/wikis/ownerreq/access-req/pages", json={
        "path": "private/page.md",
        "content": "# Hidden\n\nNope.",
        "visibility": "private",
    }, headers=h)
    assert r.status_code == 201

    r = client.post("/api/v1/access-requests", json={
        "path": "/@ownerreq/access-req/private/page",
        "email": "asker@example.com",
        "note": "Need this for collaboration.",
    })
    assert r.status_code == 202
    data = r.get_json()
    assert data["ok"] is True
    assert "owner has been notified" in data["message"]

    shareish = [m for m in email_service.mock_outbox() if "Access request" in m["subject"]]
    assert len(shareish) == 1, shareish
    assert shareish[0]["to"] == "ownerreq@example.com"
    assert "/@ownerreq/access-req/private/page" in shareish[0]["text"]
    assert "asker@example.com" in shareish[0]["text"]

    before = len(email_service.mock_outbox())
    r = client.post("/api/v1/access-requests", json={
        "path": "/@ownerreq/access-req/does/not/exist",
        "email": "asker@example.com",
        "note": "Need this too.",
    })
    assert r.status_code == 202
    data = r.get_json()
    assert data["ok"] is True
    assert "owner has been notified" in data["message"]
    assert len(email_service.mock_outbox()) == before, email_service.mock_outbox()

    os.environ.pop("EMAIL_MODE", None)


def test_subdomain_routing(client):
    """users get profile subdomains; wikis can claim custom subdomains.
    requests with a matching Host header route to the canonical path."""
    # Reserved username is rejected on signup
    r = client.post("/api/v1/accounts", json={"username": "www"})
    assert r.status_code == 409, "reserved username should be rejected"

    r = client.post("/api/v1/accounts", json={"username": "staging"})
    assert r.status_code == 409, "reserved username should be rejected"

    # Regular user creation works
    r = client.post("/api/v1/accounts", json={"username": "subowner"})
    assert r.status_code == 201
    key = r.get_json()["api_key"]
    h = {"Authorization": f"Bearer {key}"}

    # Create a wiki
    r = client.post("/api/v1/wikis", json={"owner": "subowner", "slug": "cookbook"}, headers=h)
    assert r.status_code == 201

    # Claim a subdomain on the wiki
    r = client.patch("/api/v1/wikis/subowner/cookbook",
                     json={"subdomain": "recipes"}, headers=h)
    assert r.status_code == 200, f"subdomain PATCH failed: {r.get_data(as_text=True)}"
    assert r.get_json()["subdomain"] == "recipes"

    # Reserved subdomain rejected
    r = client.patch("/api/v1/wikis/subowner/cookbook",
                     json={"subdomain": "admin"}, headers=h)
    assert r.status_code == 400

    # Username conflict rejected
    r = client.patch("/api/v1/wikis/subowner/cookbook",
                     json={"subdomain": "subowner"}, headers=h)
    assert r.status_code == 400

    # Access user profile via user subdomain (profile pages are public)
    r = client.get("/", headers={"Host": "subowner.wikihub.md"})
    assert r.status_code == 200, f"user subdomain failed: {r.status_code}"
    assert b"subowner" in r.data.lower()

    # Wiki subdomain routes to the wiki. The wiki has no public content yet,
    # but the route is reached (4xx/2xx, not a raw 500 or mis-routed response).
    # Verify by creating a public page first.
    r = client.post("/api/v1/wikis/subowner/cookbook/pages",
                    json={"path": "intro.md", "content": "# Intro\nPublic page.",
                          "visibility": "public", "message": "add intro"}, headers=h)
    assert r.status_code in (200, 201)
    r = client.get("/intro", headers={"Host": "recipes.wikihub.md"})
    assert r.status_code == 200, f"wiki subdomain page failed: {r.status_code}"

    r = client.post("/api/v1/wikis/subowner/cookbook/pages",
                    json={"path": "activity/menu.md", "content": "# Activity Menu\nWeekly prep.",
                          "visibility": "public", "message": "add activity page"}, headers=h)
    assert r.status_code in (200, 201)

    # Apex /@user/<slug> 301s to wiki subdomain
    r = client.get("/@subowner/cookbook",
                   headers={"Host": "wikihub.md"}, follow_redirects=False)
    assert r.status_code == 301
    assert "recipes.wikihub.md" in r.headers["Location"]

    # Apex /@user 301s to user profile subdomain
    r = client.get("/@subowner",
                   headers={"Host": "wikihub.md"}, follow_redirects=False)
    assert r.status_code == 301
    assert "subowner.wikihub.md" in r.headers["Location"]

    # Global routes (api, auth, static) still work on subdomains
    r = client.get("/api/v1/ping", headers={"Host": "subowner.wikihub.md"})
    # /api/v1/ping doesn't exist, but the important thing is it doesn't get
    # rewritten into /@subowner/api/v1/ping (which would 404 differently)
    assert r.status_code in (404, 200)

    for host in ("subowner.wikihub.md", "recipes.wikihub.md"):
        r = client.get("/activity", headers={"Host": host})
        assert r.status_code == 200, f"/activity should stay global on {host}: {r.status_code}"
        assert b"Recent page creations and updates across public wikis" in r.data
        r = client.get("/activity.rss", headers={"Host": host})
        assert r.status_code == 200, f"/activity.rss should stay global on {host}: {r.status_code}"
        assert r.mimetype == "application/rss+xml"
        assert b"WikiHub" in r.data
        assert b"recent activity" in r.data

    r = client.get("/activity/menu", headers={"Host": "recipes.wikihub.md"})
    assert r.status_code == 200, f"/activity/<page> should rewrite to wiki page on wiki subdomain: {r.status_code}"
    assert b"Activity Menu" in r.data
    assert b"Recent page creations and updates across public wikis" not in r.data

    # Internal url_for()-generated links use the full /@user/slug/page form.
    # On a wiki subdomain, those must resolve — either directly or 301 to the
    # short form on the same host. Regression test for the double-prefix bug.
    r = client.get("/@subowner/cookbook/intro",
                   headers={"Host": "recipes.wikihub.md"}, follow_redirects=False)
    assert r.status_code in (200, 301), f"/@user/slug/page on wiki subdomain: {r.status_code}"
    if r.status_code == 301:
        assert "recipes.wikihub.md/intro" in r.headers["Location"]

    r = client.get("/@subowner/cookbook/activity",
                   headers={"Host": "recipes.wikihub.md"}, follow_redirects=False)
    assert r.status_code == 200, f"per-wiki activity on wiki subdomain should not redirect: {r.status_code}"
    assert b"Recent visible edits" in r.data
    assert b"Recent page creations and updates across public wikis" not in r.data

    r = client.get("/@subowner/cookbook/activity",
                   headers={"Host": "wikihub.md"}, follow_redirects=False)
    assert r.status_code == 200, f"per-wiki activity on apex should not canonicalize to global activity: {r.status_code}"
    assert b"Recent visible edits" in r.data
    assert b"Recent page creations and updates across public wikis" not in r.data

    r = client.get("/@subowner/cookbook/activity.rss",
                   headers={"Host": "recipes.wikihub.md"}, follow_redirects=False)
    assert r.status_code == 200, f"per-wiki RSS on wiki subdomain should not redirect: {r.status_code}"
    assert r.mimetype == "application/rss+xml"
    assert b"cookbook" in r.data
    assert b"WikiHub" not in r.data

    r = client.get("/@subowner/cookbook/activity.rss",
                   headers={"Host": "wikihub.md"}, follow_redirects=False)
    assert r.status_code == 200, f"per-wiki RSS on apex should not canonicalize to global RSS: {r.status_code}"
    assert r.mimetype == "application/rss+xml"
    assert b"cookbook" in r.data
    assert b"WikiHub" not in r.data

    r = client.get("/@subowner/cookbook/activity/menu",
                   headers={"Host": "wikihub.md"}, follow_redirects=False)
    assert r.status_code == 301, f"activity folder page on apex should canonicalize to wiki subdomain: {r.status_code}"
    assert "recipes.wikihub.md/activity/menu" in r.headers["Location"]

    # Same regression check for user subdomain
    r = client.get("/@subowner/cookbook/intro",
                   headers={"Host": "subowner.wikihub.md"}, follow_redirects=False)
    assert r.status_code in (200, 301), f"/@user/slug/page on user subdomain: {r.status_code}"

    # Clearing the subdomain
    r = client.patch("/api/v1/wikis/subowner/cookbook",
                     json={"subdomain": None}, headers=h)
    assert r.status_code == 200
    assert r.get_json()["subdomain"] is None

    # System user @wikihub gets a special subdomain override: wikihub.wikihub.md
    # resolves to /@wikihub even though "wikihub" is a reserved label.
    # (ensure the user exists — truncated by reset_database at test start)
    from app.wiki_ops import ensure_official_wiki
    ensure_official_wiki()
    db.session.commit()

    r = client.get("/", headers={"Host": "wikihub.wikihub.md"})
    assert r.status_code == 200, f"system subdomain failed: {r.status_code}"

    # Apex /@wikihub 301s to wikihub.wikihub.md
    r = client.get("/@wikihub",
                   headers={"Host": "wikihub.md"}, follow_redirects=False)
    assert r.status_code == 301, f"expected 301, got {r.status_code}"
    assert "wikihub.wikihub.md" in r.headers["Location"]


def test_cli(client):
    """CLI end-to-end: credential handling + every subcommand against a
    real app via a requests→flask-test-client shim."""
    import io
    import json as _json
    import os as _os
    import tempfile
    from contextlib import redirect_stdout, redirect_stderr
    from unittest.mock import patch

    # make sure the CLI package is importable (installed editable or via path)
    sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "cli"))
    from wikihub_cli.__main__ import main
    import wikihub_cli.__main__ as wh

    class FakeResp:
        def __init__(self, fresp):
            self.status_code = fresp.status_code
            self.text = fresp.get_data(as_text=True)
            self._headers = dict(fresp.headers)

        def json(self):
            return _json.loads(self.text)

    def fake_request(method, url, headers=None, json=None, params=None, timeout=None, **_kw):
        parsed = urlparse(url)
        path = parsed.path
        if parsed.query:
            path = f"{path}?{parsed.query}"
        kwargs = {"headers": headers or {}}
        if json is not None:
            kwargs["json"] = json
        if params:
            kwargs["query_string"] = params
        fresp = client.open(path, method=method, **kwargs)
        return FakeResp(fresp)

    # isolate the credentials file to a temp dir
    tmp_home = tempfile.mkdtemp(prefix="wh-cli-test-")
    orig_path = wh.CREDENTIALS_PATH
    wh.CREDENTIALS_PATH = type(orig_path)(tmp_home) / ".wikihub" / "credentials.json"

    def run_cli(*args) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with patch("wikihub_cli.__main__.requests.request", side_effect=fake_request), \
             redirect_stdout(out), redirect_stderr(err):
            rc = main(["--server", "http://localhost"] + list(args))
        return rc, out.getvalue(), err.getvalue()

    try:
        # signup
        rc, out, err = run_cli("signup", "--username", "cliuser", "--password", "testpass12345")
        assert rc == 0, f"signup failed: {err}"
        assert "signed up as cliuser" in out
        assert wh.CREDENTIALS_PATH.exists(), "credentials file not written"
        creds = _json.loads(wh.CREDENTIALS_PATH.read_text())
        assert creds["default"]["username"] == "cliuser"
        assert creds["default"]["api_key"].startswith("wh_")

        # whoami
        rc, out, err = run_cli("whoami")
        assert rc == 0, err
        assert "cliuser" in out

        # new wiki
        rc, out, err = run_cli("new", "notes", "--title", "CLI Notes")
        assert rc == 0, err
        assert "cliuser/notes" in out

        # write (inline content)
        rc, out, err = run_cli("write", "cliuser/notes/hello.md", "--content", "# hello from cli\n")
        assert rc == 0, err
        assert "created" in out

        # read
        rc, out, err = run_cli("read", "cliuser/notes/hello.md")
        assert rc == 0, err
        assert "hello from cli" in out

        # write (update existing)
        rc, out, err = run_cli("write", "cliuser/notes/hello.md", "--content", "# v2\n")
        assert rc == 0, err
        assert "updated" in out

        # ls
        rc, out, err = run_cli("ls", "cliuser/notes")
        assert rc == 0, err
        assert "hello.md" in out

        # search
        rc, out, err = run_cli("search", "hello", "--wiki", "cliuser/notes")
        assert rc == 0, err
        assert "result(s)" in out

        # publish from local file
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as tf:
            tf.write("# published\n")
            tmpfile = tf.name
        rc, out, err = run_cli("publish", tmpfile, "--to", "cliuser/notes/pub.md")
        _os.unlink(tmpfile)
        assert rc == 0, err

        # rm
        rc, out, err = run_cli("rm", "cliuser/notes/pub.md")
        assert rc == 0, err
        assert "deleted" in out

        # share: create two teammates, bulk add by username + email, list, revoke
        client.post("/api/v1/accounts", json={"username": "climate1", "email": "climate1@example.com"})
        client.post("/api/v1/accounts", json={"username": "climate2"})

        rc, out, err = run_cli("share", "add", "cliuser/notes", "climate1@example.com", "climate2", "--role", "edit")
        assert rc == 0, err
        assert "added" in out and "climate1" in out and "climate2" in out, out

        # re-running is idempotent → both reported as skipped
        rc, out, err = run_cli("share", "add", "cliuser/notes", "climate1", "climate2", "--role", "edit")
        assert rc == 0, err
        assert "skipped" in out, out

        # unknown user → nonzero exit, message on stderr
        rc, out, err = run_cli("share", "add", "cliuser/notes", "nobody-xyz", "--role", "read")
        assert rc != 0
        assert "nobody-xyz" in err

        # ls shows both
        rc, out, err = run_cli("share", "ls", "cliuser/notes")
        assert rc == 0, err
        assert "climate1" in out and "climate2" in out

        # rm one of them
        rc, out, err = run_cli("share", "rm", "cliuser/notes", "climate1")
        assert rc == 0, err
        assert "revoked" in out and "climate1" in out

        rc, out, err = run_cli("share", "ls", "cliuser/notes")
        assert rc == 0
        assert "climate2" in out
        assert "climate1" not in out

        # mcp-config
        rc, out, err = run_cli("mcp-config")
        assert rc == 0, err
        cfg = _json.loads(out)
        assert cfg["mcpServers"]["wikihub"]["url"].endswith("/mcp")
        assert "Authorization" in cfg["mcpServers"]["wikihub"]["headers"]

        # ----- gh-style multi-account auth (wikihub-0gj2) -----
        # cliuser is still signed in (default profile, active).
        # add a second account via `auth login --signup`; it should get auto-named.
        rc, out, err = run_cli("auth", "login", "--signup", "--username", "cliuser2", "--password", "testpass12345")
        assert rc == 0, err
        assert "added profile" in out and "now active" in out, out
        # credentials.json should have _active pointing at the new profile, plus both profiles
        creds_multi = _json.loads(wh.CREDENTIALS_PATH.read_text())
        assert "default" in creds_multi and creds_multi["_active"] != "default", creds_multi
        new_profile = creds_multi["_active"]
        assert new_profile.startswith("cliuser2@"), new_profile

        # whoami (with no --profile) follows _active and returns the second account
        rc, out, err = run_cli("whoami")
        assert rc == 0, err
        assert "cliuser2" in out

        # auth status lists both, marks the active one
        rc, out, err = run_cli("auth", "status")
        assert rc == 0, err
        assert "default" in out and new_profile in out
        active_line = next(l for l in out.splitlines() if l.startswith("*"))
        assert new_profile in active_line, f"active marker on wrong line: {active_line!r}"

        # switch back to default
        rc, out, err = run_cli("auth", "switch", "default")
        assert rc == 0, err
        rc, out, err = run_cli("whoami")
        assert rc == 0 and "cliuser" in out and "cliuser2" not in out, out

        # --profile NAME still overrides _active for a single invocation
        rc, out, err = run_cli("--profile", new_profile, "whoami")
        assert rc == 0 and "cliuser2" in out, out

        # auth logout (no arg) removes the active profile; active falls back to the other
        rc, out, err = run_cli("auth", "logout")
        assert rc == 0, err
        assert "removed profile 'default'" in out, out
        creds_after_alogout = _json.loads(wh.CREDENTIALS_PATH.read_text())
        assert "default" not in creds_after_alogout
        assert creds_after_alogout.get("_active") == new_profile

        # auth switch to unknown profile fails
        rc, out, err = run_cli("auth", "switch", "nope")
        assert rc != 0
        assert "no profile named 'nope'" in err

        # clean up the second profile via auth logout with explicit name
        rc, out, err = run_cli("auth", "logout", new_profile)
        assert rc == 0, err
        creds_final = _json.loads(wh.CREDENTIALS_PATH.read_text())
        assert not [k for k in creds_final.keys() if k != "_active"]

        # logout (back-compat path) when "default" already removed — should say "no profile"
        rc, out, err = run_cli("logout")
        assert rc == 0, err
        assert "no profile 'default' found" in out

        # re-signup so the legacy assertion below has something to remove
        rc, out, err = run_cli("signup", "--username", "cliuser3", "--password", "testpass12345")
        assert rc == 0, err

        # legacy back-compat: bare `wikihub logout` still removes "default"
        rc, out, err = run_cli("logout")
        assert rc == 0, err
        assert "removed profile" in out
        creds_after = _json.loads(wh.CREDENTIALS_PATH.read_text())
        assert "default" not in creds_after

        # unauthenticated command after logout
        rc, out, err = run_cli("whoami")
        assert rc != 0
        assert "not authenticated" in err
    finally:
        wh.CREDENTIALS_PATH = orig_path
        shutil.rmtree(tmp_home, ignore_errors=True)


def _make_curator_session(app, work_dir, owner, wiki_slug, username, user_id):
    """Build a session dict shaped like the live agent_chat sessions, for
    direct tool-layer testing without needing an Anthropic API key."""
    import time as _time
    from app.routes.agent_chat import _clone_wiki
    repos_dir = app.config["REPOS_DIR"]
    clone_path = _clone_wiki(repos_dir, owner, wiki_slug, work_dir)
    return {
        "conversation_id": "test-session",
        "work_dir": work_dir,
        "clone_path": clone_path,
        "messages": [],
        "system_prompt": "",
        "last_used": _time.time(),
        "base_url": "http://localhost",
        "auth_token": None,
        "owner": owner,
        "wiki_slug": wiki_slug,
        "username": username,
        "user_id": user_id,
    }


def test_agent_chat_blocks_cross_user_private_read(client, api_key):
    """user A creates a private page; user B's chat tools cannot read it.

    Exercises the tool layer end-to-end (read_file, search_content, list_files)
    with a session built as user B but pointed at user A's wiki. (wikihub-7w40)
    """
    import tempfile, shutil as _sh
    from app.models import User
    from app.routes.agent_chat import _execute_tool

    h = {"Authorization": f"Bearer {api_key}"}

    # Owner agent1 creates a private wiki + secret page.
    r = client.post("/api/v1/wikis", json={"slug": "curator-priv-a", "title": "Priv A"}, headers=h)
    assert r.status_code == 201
    secret_marker = "curatorzephyrtoken1234"
    r = client.post("/api/v1/wikis/agent1/curator-priv-a/pages", json={
        "path": "secret/plan.md",
        "content": f"# Plan\n\n{secret_marker} lives here. Top secret.",
        "visibility": "private",
    }, headers=h)
    assert r.status_code == 201

    # Create a second user, agent_b. We will directly build a curator session
    # bound to agent_b but whose work_dir cloned agent1's wiki — simulating the
    # state right after a malicious cross-wiki context request.
    r = client.post("/api/v1/accounts", json={"username": "agent_b"})
    assert r.status_code == 201
    agent_b = User.query.filter_by(username="agent_b").first()

    work_dir = tempfile.mkdtemp(prefix="curator-test-")
    try:
        sess = _make_curator_session(
            client.application, work_dir,
            owner="agent1", wiki_slug="curator-priv-a",
            username="agent_b", user_id=agent_b.id,
        )

        # 1. Direct read of private path must be refused.
        out = _execute_tool("read_file", {"path": "agent1/curator-priv-a/secret/plan.md"}, sess)
        assert secret_marker not in out, f"LEAK: agent_b read agent1's private file: {out!r}"
        assert "no access" in out or "not found" in out, out

        # 2. Search must not surface the secret marker even when grepping the clone.
        # ("No matches found for: <q>" echoes the query — that's fine; we want
        # to ensure the marker doesn't appear in a hit line.)
        out = _execute_tool("search_content", {"query": secret_marker}, sess)
        assert "No matches found" in out, f"LEAK via search: {out!r}"
        assert "secret/plan.md" not in out, f"LEAK via search (path leak): {out!r}"

        # 3. list_files must not reveal the private file's existence.
        out = _execute_tool("list_files", {"directory": "agent1/curator-priv-a/secret"}, sess)
        assert "plan.md" not in out, f"LEAK via list_files: {out!r}"

        # 4. Cross-wiki path traversal — agent_b session is bound to
        #    curator-priv-a; trying to read a different wiki/path is refused.
        out = _execute_tool("read_file", {"path": "agent1/some-other-wiki/page.md"}, sess)
        assert secret_marker not in out
        assert "no access" in out or "not found" in out, out

        # 5. write_file is also gated — agent_b cannot write to agent1's private file.
        out = _execute_tool("write_file", {
            "path": "agent1/curator-priv-a/secret/plan.md",
            "content": "pwned",
        }, sess)
        assert "no access" in out or "not found" in out, out
    finally:
        _sh.rmtree(work_dir, ignore_errors=True)


def test_agent_chat_anon_session_blocked(client):
    """anonymous chat (no Bearer token) is rejected at the HTTP layer."""
    anon = client.application.test_client()
    r = anon.post("/api/v1/agent/chat", json={"message": "hi"})
    assert r.status_code == 401, f"anon chat should be 401, got {r.status_code}"


def test_agent_chat_session_locked_to_creator(client, api_key):
    """A conversation_id minted by user A cannot be reused by user B
    (wikihub-7w40 — defense against session-id sharing/leaking)."""
    import tempfile
    from app.models import User
    from app.routes.agent_chat import _sessions, _sessions_lock

    # Pre-seed a fake session belonging to agent1.
    r = client.post("/api/v1/wikis", json={"slug": "curator-locked", "title": "Locked"},
                    headers={"Authorization": f"Bearer {api_key}"})
    assert r.status_code == 201
    agent1 = User.query.filter_by(username="agent1").first()

    work_dir = tempfile.mkdtemp(prefix="curator-test-")
    sess = _make_curator_session(client.application, work_dir,
                                 owner="agent1", wiki_slug="curator-locked",
                                 username="agent1", user_id=agent1.id)
    fake_cid = "test-locked-cid"
    sess["conversation_id"] = fake_cid
    with _sessions_lock:
        _sessions[fake_cid] = sess

    # User B tries to use agent1's session.
    r = client.post("/api/v1/accounts", json={"username": "agent_session_thief"})
    thief_key = r.get_json()["api_key"]

    r = client.post("/api/v1/agent/chat",
                    json={"message": "leak it", "conversation_id": fake_cid},
                    headers={"Authorization": f"Bearer {thief_key}"})
    assert r.status_code == 403, f"session theft should 403, got {r.status_code}"

    with _sessions_lock:
        _sessions.pop(fake_cid, None)


def test_agent_chat_search_filters_private_pages(client, api_key):
    """search_content tool must not include lines from pages the user can't read,
    even when the working dir holds the full clone. (wikihub-7w40)"""
    import tempfile, shutil as _sh
    from app.models import User
    from app.routes.agent_chat import _execute_tool

    h = {"Authorization": f"Bearer {api_key}"}
    r = client.post("/api/v1/wikis", json={"slug": "curator-search", "title": "S"}, headers=h)
    assert r.status_code == 201

    public_marker = "publicfoo9999"
    private_marker = "privatesecret9999"
    client.post("/api/v1/wikis/agent1/curator-search/pages", json={
        "path": "public-note.md",
        "content": f"# Public\n\n{public_marker}",
        "visibility": "public",
    }, headers=h)
    client.post("/api/v1/wikis/agent1/curator-search/pages", json={
        "path": "private-note.md",
        "content": f"# Priv\n\n{private_marker}",
        "visibility": "private",
    }, headers=h)

    r = client.post("/api/v1/accounts", json={"username": "search_outsider"})
    outsider = User.query.filter_by(username="search_outsider").first()

    work_dir = tempfile.mkdtemp(prefix="curator-test-")
    try:
        sess = _make_curator_session(client.application, work_dir,
                                     owner="agent1", wiki_slug="curator-search",
                                     username="search_outsider", user_id=outsider.id)

        out_pub = _execute_tool("search_content", {"query": public_marker}, sess)
        assert public_marker in out_pub, f"public marker should be searchable, got: {out_pub!r}"

        out_priv = _execute_tool("search_content", {"query": private_marker}, sess)
        # the query echoes in "No matches found for: <q>" which is fine.
        # what matters: no hit line referencing the private file.
        assert "No matches found" in out_priv, f"unexpected hit: {out_priv!r}"
        assert "private-note.md" not in out_priv, f"LEAK: private path in search: {out_priv!r}"

        out_list = _execute_tool("list_files", {"directory": "agent1/curator-search"}, sess)
        assert "public-note.md" in out_list
        assert "private-note.md" not in out_list, f"LEAK: private path in list_files: {out_list!r}"
    finally:
        _sh.rmtree(work_dir, ignore_errors=True)


def test_agent_chat_resists_prompt_injection_for_acl_bypass(client, api_key):
    """Prompt-injected tool calls with adversarial paths must be refused at
    the tool layer regardless of the input string. The model's compliance is
    not a security boundary; the tool refuses on its own. (wikihub-7w40)"""
    import tempfile, shutil as _sh
    from app.models import User
    from app.routes.agent_chat import _execute_tool

    h = {"Authorization": f"Bearer {api_key}"}
    r = client.post("/api/v1/wikis", json={"slug": "curator-inject", "title": "X"}, headers=h)
    assert r.status_code == 201
    secret = "INJECTSECRET777"
    client.post("/api/v1/wikis/agent1/curator-inject/pages", json={
        "path": "vault.md",
        "content": f"# Vault\n\n{secret}",
        "visibility": "private",
    }, headers=h)

    r = client.post("/api/v1/accounts", json={"username": "inject_user"})
    inject_user = User.query.filter_by(username="inject_user").first()

    # Bind the session to a different (innocuous) wiki so the request looks
    # benign on the surface.
    r = client.post("/api/v1/wikis", json={"slug": "decoy", "title": "D"},
                    headers={"Authorization": f"Bearer {r.get_json()['api_key']}" if False else api_key})
    # Use agent1's decoy as the bound wiki for inject_user (simulating any
    # public wiki they're viewing).
    work_dir = tempfile.mkdtemp(prefix="curator-test-")
    try:
        sess = _make_curator_session(client.application, work_dir,
                                     owner="agent1", wiki_slug="decoy",
                                     username="inject_user", user_id=inject_user.id)
        adversarial_paths = [
            "agent1/curator-inject/vault.md",         # cross-wiki
            "../agent1/curator-inject/vault.md",      # path traversal
            "agent1/decoy/../curator-inject/vault.md",  # tricky traversal
            "/etc/passwd",                            # absolute path
        ]
        for p in adversarial_paths:
            out = _execute_tool("read_file", {"path": p}, sess)
            assert secret not in out, f"LEAK via path {p!r}: {out!r}"
    finally:
        _sh.rmtree(work_dir, ignore_errors=True)


def test_agent_chat_disabled_returns_503(app, client):
    """When CURATOR_ENABLED is false, /agent/chat returns 503 even before auth."""
    orig = app.config.get("CURATOR_ENABLED", True)
    app.config["CURATOR_ENABLED"] = False
    try:
        r = client.post("/api/v1/agent/chat", json={"message": "hi"})
        assert r.status_code == 503, f"expected 503 when disabled, got {r.status_code}"
    finally:
        app.config["CURATOR_ENABLED"] = orig


def test_backlinks_api(client, api_key):
    """wikihub-yqe6: backlinks API + ?include=backlinks + forward-ref fallback.

    Covers four scenarios:
      1. POST source page that wikilinks to existing target → backlink shows.
      2. GET .../pages/<target>/backlinks returns the source.
      3. GET .../pages/<target>?include=backlinks embeds the same list.
      4. Forward ref: source links to a target that doesn't exist yet; create
         the target later — backlink appears via alias fallback.
    """
    h = {"Authorization": f"Bearer {api_key}"}

    # Fresh wiki for this test (avoid cross-talk with other tests' agent1 wikis).
    r = client.post("/api/v1/wikis", json={"slug": "backlinks-test", "title": "BL"}, headers=h)
    assert r.status_code == 201, r.get_json()

    # Target page (Body Masters)
    r = client.post("/api/v1/wikis/agent1/backlinks-test/pages", json={
        "path": "health/Body Masters.md",
        "content": "---\ntitle: Body Masters\nvisibility: public\n---\n\n# Body Masters\n\nA roster.",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201, r.get_json()

    # Source page that wikilinks to it.
    r = client.post("/api/v1/wikis/agent1/backlinks-test/pages", json={
        "path": "health/Meditation Masters.md",
        "content": "---\ntitle: Meditation Masters\nvisibility: public\n---\n\n# Meditation Masters\n\nSee also [[health/Body Masters]].",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201

    # 1+2: backlinks endpoint returns Meditation Masters as a backlink to Body Masters.
    r = client.get("/api/v1/wikis/agent1/backlinks-test/pages/health/Body Masters.md/backlinks", headers=h)
    assert r.status_code == 200, r.get_json()
    payload = r.get_json()
    assert payload["target"]["path"] == "health/Body Masters.md"
    paths = [b["path"] for b in payload["backlinks"]]
    assert "health/Meditation Masters.md" in paths, paths
    assert payload["total"] == 1

    # 3: ?include=backlinks embeds the same list on the page-read response.
    r = client.get("/api/v1/wikis/agent1/backlinks-test/pages/health/Body Masters.md?include=backlinks", headers=h)
    assert r.status_code == 200
    payload = r.get_json()
    assert "backlinks" in payload, payload.keys()
    paths = [b["path"] for b in payload["backlinks"]]
    assert "health/Meditation Masters.md" in paths

    # Without include= the field must NOT appear (keeps the default response shape clean).
    r = client.get("/api/v1/wikis/agent1/backlinks-test/pages/health/Body Masters.md", headers=h)
    assert r.status_code == 200
    assert "backlinks" not in r.get_json()

    # 4: forward-ref fallback — page links to a target that doesn't exist yet.
    r = client.post("/api/v1/wikis/agent1/backlinks-test/pages", json={
        "path": "health/India Remote Sleep Hacking.md",
        "content": "---\ntitle: India Remote Sleep Hacking\nvisibility: public\n---\n\nSee [[health/Sleep]].",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201

    # Sleep doesn't exist yet — its backlinks endpoint should 404.
    r = client.get("/api/v1/wikis/agent1/backlinks-test/pages/health/Sleep.md/backlinks", headers=h)
    assert r.status_code == 404

    # Create Sleep AFTER the link was made.
    r = client.post("/api/v1/wikis/agent1/backlinks-test/pages", json={
        "path": "health/Sleep.md",
        "content": "---\ntitle: Sleep\nvisibility: public\n---\n\n# Sleep",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201

    # Now Sleep should see India Remote Sleep Hacking as a backlink — even
    # though India was created first and never re-saved.
    r = client.get("/api/v1/wikis/agent1/backlinks-test/pages/health/Sleep.md/backlinks", headers=h)
    assert r.status_code == 200
    paths = [b["path"] for b in r.get_json()["backlinks"]]
    assert "health/India Remote Sleep Hacking.md" in paths, (
        f"forward-ref fallback failed: expected India Remote Sleep Hacking in backlinks of Sleep, got {paths}"
    )


def test_highlight_js_script_url_is_canonical():
    """wikihub-1rx9: base.html must reference the canonical @highlightjs/cdn-assets
    package, not the bare highlight.js npm package.

    Before the fix, base.html had:
        <script src=".../npm/highlight.js@11.9.0/highlight.min.js"></script>
    That path 404s on JSDelivr (the highlight.js npm package has no root-level
    highlight.min.js — the build artifacts live under @highlightjs/cdn-assets).
    JSDelivr returns text/plain 404 → Chrome ORB blocks → code blocks never
    received client-side syntax highlighting.

    Fix: switch the <script> src to
        https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11.9.0/highlight.min.js
    which returns HTTP 200 with content-type: application/javascript.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    base_path = os.path.join(repo_root, "app", "templates", "base.html")
    with open(base_path) as f:
        base = f.read()

    # The good URL must be present.
    assert "@highlightjs/cdn-assets@11.9.0/highlight.min.js" in base, (
        "base.html must load highlight.js from @highlightjs/cdn-assets@11.9.0/"
        "highlight.min.js (the canonical CDN package). The bare "
        "highlight.js@11.9.0/highlight.min.js path 404s on JSDelivr."
    )

    # The bad URL pattern — bare "highlight.js@<ver>/highlight.min.js" as a
    # <script src=...> — must NOT appear. Note: the CSS theme URLs at
    # highlight.js@11.9.0/styles/*.min.css ARE valid (verified 200) and are
    # intentionally left alone.
    import re
    bad_script = re.search(
        r'<script[^>]+src=["\'][^"\']*\bhighlight\.js@[\d.]+/highlight\.min\.js["\']',
        base,
    )
    assert not bad_script, (
        f"base.html still loads the broken highlight.js script URL: "
        f"{bad_script.group(0)!r}. Switch to @highlightjs/cdn-assets."
    )


def test_nginx_serves_service_worker_allowed_header():
    """wikihub-o1ib: nginx must add `Service-Worker-Allowed: /` for /static/sw.js.

    Before the fix, the SW registration call in base.html:
        navigator.serviceWorker.register('/static/sw.js', { scope: '/' })
    was rejected by browsers with:
        The path of the provided scope ('/') is not under the max scope
        allowed ('/static/'). ... use the Service-Worker-Allowed HTTP header
        to allow the scope.

    Fix: in deploy/nginx/wikihub.conf, add a `location = /static/sw.js` block
    in each Flask-proxying server block that proxies upstream AND sets
    `add_header Service-Worker-Allowed "/" always;`. Don't move the SW file
    (would break paths). Don't reduce scope to /static/ (defeats the SW).
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    conf_path = os.path.join(repo_root, "deploy", "nginx", "wikihub.conf")
    with open(conf_path) as f:
        conf = f.read()

    # There are two Flask-proxying server blocks for wikihub.md (443 + 80
    # fallback). Both must have a location handling /static/sw.js with the
    # Service-Worker-Allowed header.
    import re
    # Find non-commented `Service-Worker-Allowed` directives.
    allowed_lines = [
        line.strip()
        for line in conf.splitlines()
        if not line.lstrip().startswith("#")
        and "Service-Worker-Allowed" in line
    ]
    assert allowed_lines, (
        "deploy/nginx/wikihub.conf does not set Service-Worker-Allowed for "
        "/static/sw.js. The SW registration in base.html uses scope='/' which "
        "browsers reject without this header."
    )
    # And the header value must allow scope '/'.
    assert any('"/"' in line or "'/'" in line for line in allowed_lines), (
        f"Service-Worker-Allowed must be set to '/' to permit the SW's scope; "
        f"got: {allowed_lines!r}"
    )

    # The header must live within a location matching /static/sw.js.
    sw_loc = re.search(
        r'location\s*=?\s*/static/sw\.js\s*\{([^}]*)\}',
        conf,
        re.DOTALL,
    )
    assert sw_loc, (
        "deploy/nginx/wikihub.conf must have a `location = /static/sw.js` "
        "block where the Service-Worker-Allowed header is added."
    )


def test_nginx_does_not_intercept_flask_errors(client):
    """wikihub-fg1p: nginx must NOT proxy_intercept_errors on the main location.

    Regression guard. Before the fix, the conf had `proxy_intercept_errors on;`
    in the wikihub.md server blocks alongside `error_page 404 = @welcome_redirect;`
    which meant nginx swallowed every Flask 4xx and returned welcome.html with
    HTTP 200 — masking permission_error.html and breaking the sign-in flow for
    logged-out users hitting private wiki URLs.

    The fix removes the global `error_page 404 = @welcome_redirect;` from the
    Flask-proxying server blocks. Rate-limit 429 from nginx itself remains
    routed to the welcome page (it's served by nginx before Flask sees it).
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    conf_path = os.path.join(repo_root, "deploy", "nginx", "wikihub.conf")
    with open(conf_path) as f:
        conf = f.read()

    # The bad pattern: error_page 404 routes to welcome — applied to the whole
    # server block (i.e. NOT inside a location-block that's static-only).
    # Strict: no error_page 404 line at all in the file (the static welcome
    # fallback is reachable directly via /welcome.html and via 429-only).
    bad_lines = [
        line.strip()
        for line in conf.splitlines()
        if not line.lstrip().startswith("#")
        and "error_page" in line and "404" in line and "@welcome_redirect" in line
    ]
    assert not bad_lines, (
        f"deploy/nginx/wikihub.conf still intercepts Flask 404s: {bad_lines!r}. "
        "Remove 'error_page 404 = @welcome_redirect;' so Flask's permission_error.html "
        "is returned with its real status code."
    )

    # proxy_intercept_errors must not be `on` at server scope where the Flask
    # app is proxied. We allow it inside specific static-only locations.
    # Heuristic: count active (non-comment) `proxy_intercept_errors on;` lines.
    intercept_on_lines = [
        line.strip()
        for line in conf.splitlines()
        if not line.lstrip().startswith("#")
        and "proxy_intercept_errors on;" in line
    ]
    assert not intercept_on_lines, (
        f"deploy/nginx/wikihub.conf still has 'proxy_intercept_errors on;' "
        f"at server scope: {intercept_on_lines!r}. This swallows Flask 4xx responses."
    )


def test_welcome_html_has_sign_in_link():
    """wikihub-46ke: deploy/static/welcome.html must offer a Sign in path.

    Before the fix, welcome.html had only an email capture form and links to
    /explore, /roadmap, /AGENTS.md — but no /auth/login or /auth/signup link.
    A logged-out user hitting the 404 fallback dead-ended with no path back to
    their account.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    welcome_path = os.path.join(repo_root, "deploy", "static", "welcome.html")
    with open(welcome_path) as f:
        html = f.read()

    assert "/auth/login" in html, (
        "welcome.html is missing a Sign in link (/auth/login). Logged-out users "
        "hitting a 404 dead-end with no path back to their account."
    )
    # Ensure there's at least one visible CTA that says Sign in (not just a footer link).
    lower = html.lower()
    assert "sign in" in lower, "welcome.html must include a visible 'Sign in' label"


def test_search_trigger_visible_on_mobile():
    """wikihub-31s3 + wikihub-n6l7: global search must be reachable at mobile
    widths AND for anonymous visitors on the marketing landing page.

    Before wikihub-31s3, base.html hid the search-trigger below 640px.
    Before wikihub-n6l7, landing.html still hid it for anonymous users
    behind an `{% if current_user.is_authenticated %}` CSS guard, so the
    marketing landing showed no search button at all to logged-out visitors.

    Both templates' default .search-trigger rule must NOT set display:none.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    import re

    for tmpl_path in (
        os.path.join(repo_root, "app", "templates", "base.html"),
        os.path.join(repo_root, "app", "templates", "landing.html"),
    ):
        with open(tmpl_path) as f:
            src = f.read()
        match = re.search(r"\.search-trigger\s*\{([^}]*)\}", src, re.DOTALL)
        tmpl = os.path.basename(tmpl_path)
        assert match, f"{tmpl}: expected a .search-trigger {{}} CSS rule"
        default_block = match.group(1)
        disp_match = re.search(r"display\s*:\s*([a-z\-]+)", default_block)
        assert disp_match, (
            f"{tmpl}: .search-trigger has no display property in default block: "
            f"{default_block!r}"
        )
        assert disp_match.group(1) != "none", (
            f"{tmpl}: .search-trigger has display:none by default — search "
            f"button is hidden. Set display:inline-flex so search is reachable "
            f"on phones and to anonymous visitors on the landing page."
        )
        # landing.html must not gate the trigger visibility behind an auth check.
        if tmpl == "landing.html":
            # Stronger check: no inline-flex override gated on current_user
            assert not re.search(
                r"\{%\s*if current_user[^%]*%\}\s*\.search-trigger\s*\{\s*display:\s*inline-flex",
                src,
            ), (
                "landing.html: .search-trigger is gated behind an auth check. "
                "Remove the {% if current_user.is_authenticated %} wrap so "
                "anonymous visitors see the search button too (wikihub-n6l7)."
            )
            # ALSO: the search modal + overlay + search.js script must NOT be
            # behind an auth gate — otherwise the visible button is wired to a
            # non-existent global and clicks silently no-op (mobile bug
            # 2026-05-19). The previous structure was:
            #   {% if current_user.is_authenticated %}
            #     <style>.search-modal{...}</style>
            #     <div id="search-modal">...</div>
            #     <script src=".../search.js"></script>
            #   {% endif %}
            # which left anon visitors with a click-no-op trigger.
            assert not re.search(
                r"\{%\s*if current_user[^%]*%\}[\s\S]{0,200}id=[\"']search-modal[\"']",
                src,
            ), (
                "landing.html: the #search-modal markup is wrapped in an auth "
                "{% if %} gate. Anonymous visitors will see the search button "
                "(visible at all viewports) but clicks will silently no-op "
                "because window.wikihubSearch is never initialised. Remove "
                "the auth wrap around the modal+script (data-username on the "
                "modal element can stay conditional inline)."
            )
            assert not re.search(
                r"\{%\s*if current_user[^%]*%\}[\s\S]{0,1000}static/js/search\.js",
                src,
            ), (
                "landing.html: search.js include is behind an auth gate. "
                "Remove the {% if %} wrap — search.js must load for everyone "
                "so window.wikihubSearch is defined for the visible button."
            )


def test_search_modal_mobile_ux_fixes_wikihub_zlgt():
    """wikihub-zlgt: mobile search modal UX — close button, autofocus,
    body-scroll lock, system back-button, subdomain scope detection,
    empty-state hint, and scope-aware placeholder.

    Each assertion below maps to one of the 7 acceptance criteria in the
    ticket. These are static-source checks (no JS runner in this repo) — we
    assert the templates and search.js contain the wiring that delivers each
    behavior. If someone reintroduces the bug by ripping the wiring out,
    this test fails.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    import re

    # The modal markup must be the shared partial so close-button and scope
    # container can't drift between base.html and landing.html.
    partial_path = os.path.join(repo_root, "app", "templates", "_search_modal.html")
    assert os.path.exists(partial_path), (
        "_search_modal.html partial missing — was the markup re-inlined? "
        "Both base.html and landing.html should `{% include '_search_modal.html' %}`."
    )
    with open(partial_path) as f:
        partial = f.read()

    # (1) Visible × close button, 44x44 touch target, with id search-close
    assert 'id="search-close"' in partial, (
        "search modal partial missing #search-close button (wikihub-zlgt fix #1). "
        "Mobile users have no way out of the fullscreen modal without Esc."
    )
    assert 'aria-label' in partial, "search-close button must have aria-label"

    # (3 prep) Scope container present in partial (was missing from landing.html before)
    assert 'id="search-scope"' in partial, (
        "search-scope container missing from partial — scope pill won't render."
    )

    for tmpl_name in ("base.html", "landing.html"):
        tmpl_path = os.path.join(repo_root, "app", "templates", tmpl_name)
        with open(tmpl_path) as f:
            src = f.read()
        # Both templates must use the partial (not duplicate the markup)
        assert "_search_modal.html" in src, (
            f"{tmpl_name}: should `{{% include '_search_modal.html' %}}` rather "
            f"than inline the search modal markup (wikihub-zlgt extraction). "
            f"Otherwise base/landing drift."
        )
        # CSS for .search-close must define a 44x44 touch target in each template
        close_css = re.search(
            r"\.search-close\s*\{([^}]*)\}", src, re.DOTALL
        )
        assert close_css, (
            f"{tmpl_name}: missing .search-close CSS rule. Need a 44x44 "
            f"touch target so the close button is tap-friendly on mobile."
        )
        block = close_css.group(1)
        assert "44px" in block, (
            f"{tmpl_name}: .search-close must specify 44px (width/height) for "
            f"the mobile touch-target requirement (wikihub-zlgt)."
        )

    # Now check search.js wiring for the JS-side fixes.
    js_path = os.path.join(repo_root, "app", "static", "js", "search.js")
    with open(js_path) as f:
        js = f.read()

    # (2) Input autofocus on open — call .focus() on the input
    assert "input.focus()" in js, (
        "search.js: must call input.focus() on open() so mobile keyboard "
        "raises immediately (wikihub-zlgt fix #2)."
    )

    # (3) Body scroll lock + restore
    assert "document.body.style.overflow = 'hidden'" in js, (
        "search.js: must set document.body.style.overflow='hidden' on open() "
        "to lock background scrolling on mobile (wikihub-zlgt fix #3)."
    )
    assert "document.body.style.overflow = ''" in js, (
        "search.js: must restore document.body.style.overflow='' on close() "
        "so the page becomes scrollable again (wikihub-zlgt fix #3)."
    )

    # (4) history.pushState + popstate handler so system back-button closes modal
    assert "history.pushState" in js, (
        "search.js: must history.pushState() on open() so the system back "
        "button closes the modal instead of leaving the site (wikihub-zlgt fix #4)."
    )
    assert "popstate" in js, (
        "search.js: must register a 'popstate' listener that closes the "
        "modal when the user presses back (wikihub-zlgt fix #4)."
    )
    assert "wikihubSearch" in js, "popstate state marker missing"

    # (5) detectScope() must recognise subdomain URL form (jacobcole.wikihub.md/...)
    # Look for a regex that matches the subdomain host pattern.
    assert re.search(r"wikihub\\\.md", js) or re.search(r"wikihub\.md", js), (
        "search.js: detectScope() must include a host-side check for "
        "<slug>.wikihub.md subdomain URLs (wikihub-zlgt fix #5). Without "
        "this, the scope pill never shows on Jacob's canonical "
        "jacobcole.wikihub.md/... pages."
    )
    assert "window.location.host" in js, (
        "search.js: detectScope() should read window.location.host to detect "
        "subdomain wikis (wikihub-zlgt fix #5)."
    )

    # (6) Empty state — renderEmptyState() or equivalent must exist and be
    #     invoked on open() when there's no query.
    assert "search-empty" in js, (
        "search.js: must render a 'search-empty' state when no query is "
        "entered (wikihub-zlgt fix #6)."
    )
    assert re.search(r"Type to search", js), (
        "search.js: empty state should include a 'Type to search…' hint "
        "(wikihub-zlgt fix #6)."
    )

    # (7) Scope-aware placeholder text
    assert "'Search this wiki…'" in js, (
        "search.js: input placeholder should change to 'Search this wiki…' "
        "when currentScope is set (wikihub-zlgt fix #7)."
    )
    assert "'Search wikihub…'" in js, (
        "search.js: default placeholder should be 'Search wikihub…' when no "
        "scope is set (wikihub-zlgt fix #7)."
    )


def test_search_detect_scope_matches_subdomain_url_form_wikihub_zlgt():
    """wikihub-zlgt fix #5: detectScope() regex must match the subdomain
    URL shape (jacobcole.wikihub.md/jacobcole/health/Sleep) as scope=wiki,
    owner=jacobcole, slug=jacobcole — not just the /@owner/slug form.

    We can't run JS in this test suite, so we mechanically simulate the
    regex from search.js against the canonical URL the ticket calls out.
    If detectScope's regex changes, mirror the change here.
    """
    import re

    # Sourced from app/static/js/search.js detectScope() — keep in sync.
    host = "jacobcole.wikihub.md"
    path = "/jacobcole/health/Sleep"

    sub_match = re.match(r"^([\w-]+)\.wikihub\.md", host, re.IGNORECASE)
    assert sub_match, "subdomain regex must match jacobcole.wikihub.md"
    slug = sub_match.group(1)
    assert slug.lower() != "www", "www should not be treated as a wiki slug"

    path_match = re.match(r"^/([\w-]+)", path)
    assert path_match, "path regex must extract first path segment as owner"
    owner = path_match.group(1)

    assert (owner, slug) == ("jacobcole", "jacobcole"), (
        f"expected scope owner=jacobcole, slug=jacobcole — got "
        f"owner={owner!r}, slug={slug!r}. detectScope() in search.js is "
        f"broken for the canonical subdomain URL form (wikihub-zlgt fix #5)."
    )

    # The legacy /@owner/slug form must still resolve.
    legacy_path = "/@jacobcole/health/Sleep"
    legacy_match = re.match(r"^/@([\w-]+)/([\w-]+)", legacy_path)
    assert legacy_match, "legacy /@owner/slug regex must still match"
    assert legacy_match.group(1) == "jacobcole"
    assert legacy_match.group(2) == "health"


def test_unauth_private_page_renders_permission_error_with_sign_in(client):
    """wikihub-ffqt: GET of a private wiki page while logged out must return
    the permission_error.html template with a visible Sign in link.

    Before the fix, prod nginx intercepted Flask's 404 and served welcome.html
    with HTTP 200 — user saw 'You found WikiHub' marketing copy with no
    sign-in path. Flask already returns the right template; this test guards
    the app-layer behavior. The nginx-layer fix is covered separately.
    """
    # Need a fresh authenticated user to set up the private wiki+page.
    r = client.post("/api/v1/accounts", json={"username": "ffqtowner"})
    assert r.status_code == 201, r.get_json()
    key = r.get_json()["api_key"]
    h = {"Authorization": f"Bearer {key}"}

    r = client.post("/api/v1/wikis", json={"slug": "ffqt-wiki", "title": "FFQT"}, headers=h)
    assert r.status_code == 201

    r = client.post("/api/v1/wikis/ffqtowner/ffqt-wiki/pages", json={
        "path": "secret/summary.md",
        "content": "# Secret\n\nPrivate.",
        "visibility": "private",
    }, headers=h)
    assert r.status_code == 201, r.get_json()

    # Unauth GET — no Authorization header, fresh test client. We logout first
    # to clear any flask-login session leaked from earlier shared-client tests
    # (Flask-Login's current_user proxy can carry over within the same app context).
    anon = client.application.test_client()
    anon.get("/auth/logout", follow_redirects=False)
    r = anon.get("/@ffqtowner/ffqt-wiki/secret/summary")
    # wikihub-dkp8: the page EXISTS but is private → distinct "restricted"
    # screen with 403 (existence acknowledged), NOT the ambiguous 404.
    assert r.status_code == 403, (
        f"unauth private page returned {r.status_code} (expected 403 restricted). "
        "If 200, the app is leaking private content to anonymous users."
    )
    body = r.data.decode("utf-8", errors="replace")
    # Must render the restricted variant of permission_error (recognizable heading).
    assert "This page is restricted" in body, (
        "expected restricted permission_error.html template, got something else. "
        "If welcome.html is returned, the nginx intercept or a Flask routing "
        "bug is hiding the real error template."
    )
    # Must NOT leak page content/title.
    assert "Secret" not in body and "Private." not in body, "restricted screen leaked page content/title"
    # Must contain a Sign in link with next= pointing back at the requested path.
    assert "/auth/login" in body, "permission_error.html missing /auth/login link"
    assert "Sign in" in body, "permission_error.html missing 'Sign in' CTA text"


def test_restricted_vs_not_found_distinction(client):
    """wikihub-dkp8: an existing-but-private page must read as "restricted"
    (403 / 401), distinct from a genuinely-missing path (404), across the web
    route, the API, and the owner-subdomain routing form. Truly-missing stays
    404; unlisted and granted access stay 200. Content is never leaked.
    """
    # Owner sets up a private page, an unlisted page, and a public page.
    r = client.post("/api/v1/accounts", json={"username": "dkp8owner"})
    assert r.status_code == 201
    key = r.get_json()["api_key"]
    h = {"Authorization": f"Bearer {key}"}

    r = client.post("/api/v1/wikis", json={"slug": "dkp8-wiki", "title": "DKP8"}, headers=h)
    assert r.status_code == 201

    # Give the wiki a user-subdomain-reachable owner + a page under a folder.
    r = client.post("/api/v1/wikis/dkp8owner/dkp8-wiki/pages", json={
        "path": "vault/secret.md",
        "content": "# TopSecret\n\nZZSECRETMARKER body.",
        "visibility": "private",
    }, headers=h)
    assert r.status_code == 201

    r = client.post("/api/v1/wikis/dkp8owner/dkp8-wiki/pages", json={
        "path": "unlisted-note.md",
        "content": "# Unlisted\n\nReachable by link.",
        "visibility": "unlisted",
    }, headers=h)
    assert r.status_code == 201

    anon = client.application.test_client()
    anon.get("/auth/logout")

    # --- Case 1: anon on an existing private page → 403 restricted screen ---
    r = anon.get("/@dkp8owner/dkp8-wiki/vault/secret")
    assert r.status_code == 403, f"anon private page expected 403 restricted, got {r.status_code}"
    body = r.data.decode("utf-8", errors="replace")
    assert "This page is restricted" in body, "expected the restricted screen heading"
    assert "ZZSECRETMARKER" not in body and "TopSecret" not in body, "restricted screen leaked content/title"

    # --- Case 2: anon on a truly-missing path → 404 (not restricted) ---
    r = anon.get("/@dkp8owner/dkp8-wiki/no/such/page")
    assert r.status_code == 404, f"missing page expected 404, got {r.status_code}"
    body = r.data.decode("utf-8", errors="replace")
    assert "This page is restricted" not in body, "missing page must NOT show the restricted screen"

    # --- Case 4: unlisted page stays readable by direct link (200) ---
    r = anon.get("/@dkp8owner/dkp8-wiki/unlisted-note")
    assert r.status_code == 200, f"unlisted page expected 200, got {r.status_code}"
    assert b"Reachable by link" in r.data

    # --- Case 6a (subdomain form): anon on the private page via owner subdomain → 403 ---
    # A user subdomain (dkp8owner.wikihub.md) rewrites /<slug>/<path> → /@dkp8owner/<slug>/<path>.
    r = anon.get("/dkp8-wiki/vault/secret", headers={"Host": "dkp8owner.wikihub.md"})
    assert r.status_code == 403, f"subdomain-form private page expected 403, got {r.status_code}"
    assert "This page is restricted" in r.data.decode("utf-8", errors="replace")
    # subdomain-form missing path still 404
    r = anon.get("/dkp8-wiki/vault/does-not-exist", headers={"Host": "dkp8owner.wikihub.md"})
    assert r.status_code == 404, f"subdomain-form missing page expected 404, got {r.status_code}"

    # --- Case 5 (API surface, mirrors wikihub_get_page): 403 vs 401 vs 404 ---
    # Authenticated non-grantee → 403 forbidden (page exists, no access).
    r = client.post("/api/v1/accounts", json={"username": "dkp8stranger"})
    stranger_key = r.get_json()["api_key"]
    hs = {"Authorization": f"Bearer {stranger_key}"}
    r = client.get("/api/v1/wikis/dkp8owner/dkp8-wiki/pages/vault/secret.md", headers=hs)
    assert r.status_code == 403, f"authed non-grantee API read expected 403, got {r.status_code}"
    jb = r.get_json()
    assert jb.get("error") == "forbidden"
    assert "ZZSECRETMARKER" not in (jb.get("content") or ""), "API restricted response leaked content"

    # Anonymous API read of the existing private page → 401 + WWW-Authenticate.
    anon_api = client.application.test_client()
    r = anon_api.get("/api/v1/wikis/dkp8owner/dkp8-wiki/pages/vault/secret.md")
    assert r.status_code == 401, f"anon API read of private page expected 401, got {r.status_code}"
    assert "WWW-Authenticate" in r.headers
    assert r.get_json().get("error") == "authentication_required"

    # API read of a truly-missing page → 404 not_found.
    r = client.get("/api/v1/wikis/dkp8owner/dkp8-wiki/pages/nope/missing.md", headers=hs)
    assert r.status_code == 404, f"missing page API read expected 404, got {r.status_code}"
    assert r.get_json().get("error") == "not_found"

    # --- Case 3: a granted user reads it (200) via both API and web session ---
    r = client.post("/api/v1/wikis/dkp8owner/dkp8-wiki/share", json={
        "pattern": "vault/secret.md",
        "username": "dkp8stranger",
        "role": "read",
    }, headers=h)
    assert r.status_code == 200
    r = client.get("/api/v1/wikis/dkp8owner/dkp8-wiki/pages/vault/secret.md", headers=hs)
    assert r.status_code == 200, f"granted user API read expected 200, got {r.status_code}"
    assert "ZZSECRETMARKER" in r.get_json()["content"]


def test_mobile_hamburger_exposes_hidden_nav_links():
    """wikihub-pz27: mobile nav must include a hamburger button (.nav-menu-toggle)
    that surfaces People / For Agents / My Wiki / Sign in below 1024px.

    Before the fix, _nav.html had class 'nav-hide-mobile' on those links
    with no fallback — they vanished entirely on phones (<640px) and on
    iPad portrait (768px).
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    nav_path = os.path.join(repo_root, "app", "templates", "_nav.html")
    base_path = os.path.join(repo_root, "app", "templates", "base.html")
    with open(nav_path) as f:
        nav = f.read()
    with open(base_path) as f:
        base = f.read()
    # 1. nav template must include a menu-toggle button
    assert "nav-menu-toggle" in nav, (
        "_nav.html missing .nav-menu-toggle button. Hidden links have no fallback "
        "on mobile/iPad portrait viewports."
    )
    # 2. CSS must show the toggle below 1024px and hide it above
    assert "nav-menu-toggle" in base, "base.html missing CSS for .nav-menu-toggle"
    # 3. A nav-mobile-menu container must exist
    assert "nav-mobile-menu" in nav, (
        "_nav.html missing .nav-mobile-menu container for the slide-out list"
    )
    # 4. nav-hide-mobile breakpoint must include iPad-portrait viewports (<= 1024px)
    import re
    # find @media (max-width: Npx) {... nav-hide-mobile ... display: none ...}
    matches = re.findall(
        r"@media\s*\(\s*max-width:\s*(\d+)px\s*\)[^{}]*\{[^}]*\.nav-hide-mobile[^}]*display\s*:\s*none",
        base,
        re.DOTALL,
    )
    assert matches, "base.html: expected a .nav-hide-mobile display:none rule under @media max-width"
    widths = [int(w) for w in matches]
    assert max(widths) >= 1024, (
        f"nav-hide-mobile breakpoint is {max(widths)}px — should be >= 1024 so "
        "iPad portrait (768px) also gets the hamburger fallback."
    )


def test_error_page_ipad_alignment_fix():
    """wikihub-dw8u: permission_error.html and error.html (404) must not use the
    old `min-height: calc(100vh - 56px)` + `justify-content: center` trick that
    floated content in the middle of iPad-portrait viewports and pushed the
    footer 600+px below content.

    The corrected layout anchors content near the top with a clamp-based
    padding-top so it breathes on desktop without leaving a wall of whitespace
    on tablet. Buttons must wrap and be centered with consistent min-width so
    the primary CTA doesn't look smaller than the secondaries on iPad.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    import re as _re
    # Strip /* ... */ CSS/JS comments so prose mentioning the old anti-pattern
    # in rationale comments doesn't false-positive.
    _strip_comments = lambda s: _re.sub(r"/\*.*?\*/", "", s, flags=_re.DOTALL)
    for name in ("permission_error.html", "error.html"):
        path = os.path.join(repo_root, "app", "templates", name)
        with open(path) as f:
            css = _strip_comments(f.read())
        # Old bad pattern must be gone.
        assert "calc(100vh - 56px)" not in css, (
            f"{name}: regression — `min-height: calc(100vh - 56px)` is back. "
            "On iPad portrait this vertically-centers content and leaves a "
            "wall of whitespace above and below. Use top-anchored padding."
        )
        # Extract just the .error-page block to check it doesn't vertical-center.
        ep_match = _re.search(r"\.error-page\s*\{([^}]*)\}", css)
        assert ep_match, f"{name}: could not find .error-page block"
        ep_block = ep_match.group(1)
        assert "justify-content: center" not in ep_block, (
            f"{name}: regression — `.error-page` has `justify-content: center`. "
            "Top-anchor with padding-top instead so the content sits near the top "
            "of tall viewports (iPad portrait) and doesn't strand the footer 600px below."
        )
        # New correct pattern must be present on .error-page: clamp-based padding.
        assert "clamp(" in ep_block and "padding:" in ep_block, (
            f"{name}: .error-page missing clamp-based padding. Required so content "
            "anchors near top instead of mid-viewport on iPad portrait."
        )
        # Button row must wrap, center, and have min-width so primary is not undersized.
        ea_match = _re.search(r"\.error-actions\s*\{([^}]*)\}", css)
        assert ea_match, f"{name}: could not find .error-actions block"
        ea_block = ea_match.group(1)
        assert "flex-wrap: wrap" in ea_block, (
            f"{name}: .error-actions must use `flex-wrap: wrap` so the row "
            "doesn't overflow narrow phone widths."
        )
        assert "justify-content: center" in ea_block, (
            f"{name}: .error-actions must `justify-content: center` so the row is "
            "horizontally centered when wrapped."
        )
        btn_match = _re.search(r"\.btn\s*\{([^}]*)\}", css)
        assert btn_match, f"{name}: could not find .btn block"
        btn_block = btn_match.group(1)
        assert "min-width:" in btn_block, (
            f"{name}: .btn must declare `min-width` so the primary CTA "
            "doesn't look visually undersized next to longer-text secondaries."
        )


def test_md_request_for_private_page_returns_json_4xx_not_landing(client):
    """wikihub-3rjt: Accept: text/markdown for an unauthenticated private page
    must return a 4xx (401/403/404) with non-HTML content type. Agents must
    NOT receive 200 + HTML landing as the markdown body of the page.
    """
    r = client.post("/api/v1/accounts", json={"username": "rjtowner"})
    assert r.status_code == 201
    key = r.get_json()["api_key"]
    h = {"Authorization": f"Bearer {key}"}

    r = client.post("/api/v1/wikis", json={"slug": "rjt-wiki", "title": "RJT"}, headers=h)
    assert r.status_code == 201
    r = client.post("/api/v1/wikis/rjtowner/rjt-wiki/pages", json={
        "path": "secret/notes.md",
        "content": "# Secret",
        "visibility": "private",
    }, headers=h)
    assert r.status_code == 201

    anon = client.application.test_client()
    anon.get("/auth/logout")
    # Request .md via Accept: text/markdown
    r = anon.get(
        "/@rjtowner/rjt-wiki/secret/notes.md",
        headers={"Accept": "text/markdown"},
    )
    assert r.status_code in (401, 403, 404), (
        f"unauth .md GET for private page returned {r.status_code} (expected 4xx)"
    )
    ctype = r.headers.get("Content-Type", "")
    assert "text/html" not in ctype, (
        f"unauth .md GET returned Content-Type {ctype!r} — agents will parse "
        "HTML landing as markdown. Expected text/plain, text/markdown, or application/json."
    )
    # If 401, must include WWW-Authenticate header
    if r.status_code == 401:
        assert "WWW-Authenticate" in r.headers, (
            "401 must include WWW-Authenticate header for agent client compliance"
        )


def test_api_wikis_endpoint_returns_401_with_www_authenticate_for_private(client):
    """wikihub-uonp: /api/wikis/<owner>/<slug> for an unauthenticated request
    to a private wiki must return 401 with WWW-Authenticate: Bearer header and
    a JSON error body with a sign_in_url hint.

    Owner with valid auth should get 200.
    """
    r = client.post("/api/v1/accounts", json={"username": "uonpowner"})
    assert r.status_code == 201
    key = r.get_json()["api_key"]
    h = {"Authorization": f"Bearer {key}"}

    r = client.post("/api/v1/wikis", json={"slug": "uonp-wiki", "title": "UONP", "visibility": "private"}, headers=h)
    assert r.status_code == 201

    # Owner authed — should get 200
    r = client.get("/api/wikis/uonpowner/uonp-wiki", headers=h)
    assert r.status_code == 200, (
        f"authed owner GET /api/wikis/<owner>/<slug> returned {r.status_code} "
        f"(expected 200). Body: {r.data[:200]!r}"
    )

    # Anon — should get 401 with WWW-Authenticate
    anon = client.application.test_client()
    anon.get("/auth/logout")
    r = anon.get("/api/wikis/uonpowner/uonp-wiki")
    assert r.status_code == 401, (
        f"anon GET /api/wikis/<owner>/<slug> for private wiki returned {r.status_code} "
        f"(expected 401)"
    )
    assert "WWW-Authenticate" in r.headers, (
        "401 must include WWW-Authenticate header"
    )
    assert "Bearer" in r.headers["WWW-Authenticate"], (
        f"WWW-Authenticate should announce Bearer scheme, got: {r.headers['WWW-Authenticate']!r}"
    )
    import json as _json
    body = _json.loads(r.data.decode("utf-8"))
    assert body.get("error") == "authentication_required", (
        f"401 body missing/wrong 'error' field: {body!r}"
    )
    assert "sign_in_url" in body, "401 body missing sign_in_url hint"


def test_logged_out_search_returns_only_public(client):
    """wikihub-7dml: logged-out search must NOT return private content."""
    r = client.post("/api/v1/accounts", json={"username": "scopeowner"})
    assert r.status_code == 201
    key = r.get_json()["api_key"]
    h = {"Authorization": f"Bearer {key}"}

    r = client.post("/api/v1/wikis", json={"slug": "scope-pub", "title": "Scope Pub", "visibility": "public"}, headers=h)
    assert r.status_code == 201
    r = client.post("/api/v1/wikis/scopeowner/scope-pub/pages", json={
        "path": "public-zzunique-marker.md",
        "content": "# Public\n\nThis is a public page with a unique marker: zzpubmarker.",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201

    r = client.post("/api/v1/wikis", json={"slug": "scope-priv", "title": "Scope Priv", "visibility": "private"}, headers=h)
    assert r.status_code == 201
    r = client.post("/api/v1/wikis/scopeowner/scope-priv/pages", json={
        "path": "private-zzsecret-marker.md",
        "content": "# Private\n\nSecret marker: zzprivmarker.",
        "visibility": "private",
    }, headers=h)
    assert r.status_code == 201

    anon = client.application.test_client()
    anon.get("/auth/logout")
    # logged-out global search
    r = anon.get("/api/search?q=zzprivmarker")
    if r.status_code == 200:
        import json as _json
        body = _json.loads(r.data.decode("utf-8"))
        results = body.get("results", body) if isinstance(body, dict) else body
        result_str = repr(results)
        assert "zzprivmarker" not in result_str, (
            "logged-out search leaked private page content marker zzprivmarker"
        )
        assert "scope-priv" not in result_str, (
            "logged-out search leaked private wiki title"
        )


def test_history_route_acl_gated_for_private_wiki(client, api_key):
    """wikihub-8888.1: web /history and /commit must not leak private content.

    Setup: a wiki with one PRIVATE page only. Anonymous viewers should NOT
    see commit metadata, filenames, SHAs, or diffs via the web routes.
    Owner of the wiki must still see history (don't over-restrict).

    Test order is anon-first to avoid flask-login's app-context-cached
    current_user from polluting fresh test_clients (see test_anonymous_upload).
    """
    import re
    h = {"Authorization": f"Bearer {api_key}"}
    r = client.post("/api/v1/wikis", json={"slug": "acl-hist-priv", "title": "Hist Priv"}, headers=h)
    assert r.status_code == 201
    r = client.post("/api/v1/wikis/agent1/acl-hist-priv/pages", json={
        "path": "secret.md",
        "content": "---\ntitle: Top Secret\nvisibility: private\n---\n\nsuper-secret-marker-zz9876",
        "visibility": "private",
    }, headers=h)
    assert r.status_code == 201

    # Clear any leaked login from prior tests.
    from flask_login import logout_user
    app = client.application
    with app.test_request_context():
        logout_user()

    # Anonymous: must NOT get 200 with private metadata.
    anon = app.test_client()
    r = anon.get("/@agent1/acl-hist-priv/history")
    assert r.status_code in (401, 403, 404), (
        f"anon got {r.status_code} on private-only wiki history — expected 4xx"
    )
    anon_body = r.data.decode("utf-8", errors="replace")
    assert "secret.md" not in anon_body, "anon history leaked private filename"

    # Now exercise the owner branch.
    owner = app.test_client()
    r = owner.get(f"/auth/login?api_key={api_key}&next=/", follow_redirects=False)
    assert r.status_code == 302
    r = owner.get("/@agent1/acl-hist-priv/history")
    assert r.status_code == 200, f"owner history got {r.status_code}"
    body = r.data.decode("utf-8", errors="replace")
    assert "secret.md" in body, "owner should see private filename in their own history"

    # Re-derive a real sha from the owner-visible page for the commit test.
    sha_match = re.search(r"\b[0-9a-f]{40}\b", body)
    if sha_match:
        sha = sha_match.group(0)
        r = owner.get(f"/@agent1/acl-hist-priv/commit/{sha}")
        assert r.status_code == 200, f"owner commit view returned {r.status_code}"

        # Clear leaked owner login again, then verify anon can't see /commit.
        with app.test_request_context():
            logout_user()
        anon2 = app.test_client()
        r = anon2.get(f"/@agent1/acl-hist-priv/commit/{sha}")
        assert r.status_code in (401, 403, 404), (
            f"anon got {r.status_code} on private commit — expected 4xx"
        )
        leak_body = r.data.decode("utf-8", errors="replace")
        assert "super-secret-marker-zz9876" not in leak_body, (
            "anon /commit leaked private page contents"
        )
        assert "secret.md" not in leak_body, "anon /commit leaked private filename"


def test_commit_diff_renders_when_sidebar_is_async(client, api_key):
    """wikihub-8vwd: /commit/<sha> must not 500 when the wiki is large enough
    that _sidebar_for_wiki returns None (client-side sidebar.json mode).

    Before the fix, diff.html iterated sidebar_items directly, so a None value
    raised 'NoneType object is not iterable' and the page 500'd.
    """
    import re
    import app.routes.wiki as wiki_routes

    h = {"Authorization": f"Bearer {api_key}"}
    r = client.post("/api/v1/wikis", json={"slug": "diff-async-sidebar", "title": "Diff Async"}, headers=h)
    assert r.status_code == 201
    r = client.post("/api/v1/wikis/agent1/diff-async-sidebar/pages", json={
        "path": "note.md",
        "content": "---\ntitle: Note\nvisibility: public\n---\n\nhello world",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201

    app = client.application
    owner = app.test_client()
    r = owner.get(f"/auth/login?api_key={api_key}&next=/", follow_redirects=False)
    assert r.status_code == 302
    body = owner.get("/@agent1/diff-async-sidebar/history").data.decode("utf-8", errors="replace")
    sha_match = re.search(r"\b[0-9a-f]{40}\b", body)
    assert sha_match, "expected a commit SHA in history output"
    sha = sha_match.group(0)

    # Force the async-sidebar path so _sidebar_for_wiki returns None.
    original_threshold = wiki_routes.SIDEBAR_ASYNC_THRESHOLD
    wiki_routes.SIDEBAR_ASYNC_THRESHOLD = 0
    try:
        r = owner.get(f"/@agent1/diff-async-sidebar/commit/{sha}")
    finally:
        wiki_routes.SIDEBAR_ASYNC_THRESHOLD = original_threshold

    assert r.status_code == 200, (
        f"wikihub-8vwd REGRESSION: /commit returned {r.status_code} when "
        "sidebar_items is None (async-sidebar mode) — diff.html must tolerate None"
    )


def test_graph_route_filters_private_pages_for_anon(client, api_key):
    """wikihub-8888.2: graph endpoints must not expose private page titles/edges to anon."""
    import json as _json
    from flask_login import logout_user
    app = client.application
    h = {"Authorization": f"Bearer {api_key}"}
    r = client.post("/api/v1/wikis", json={"slug": "graph-mix", "title": "Graph Mix"}, headers=h)
    assert r.status_code == 201

    # Public page links to private page
    r = client.post("/api/v1/wikis/agent1/graph-mix/pages", json={
        "path": "public-hub.md",
        "content": "---\ntitle: Public Hub\nvisibility: public\n---\n\nSee [[private-deets]] for details.",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201
    r = client.post("/api/v1/wikis/agent1/graph-mix/pages", json={
        "path": "private-deets.md",
        "content": "---\ntitle: Private Deets\nvisibility: private\n---\n\nsecrets",
        "visibility": "private",
    }, headers=h)
    assert r.status_code == 201

    # Populate Wikilink rows manually — the e2e POST endpoint stores page
    # content but doesn't run the wikilink extractor (that's normally a
    # git_sync hook). The graph endpoint filters orphans, so without a
    # wikilink every node disappears even for the owner. Insert the
    # `public-hub -> private-deets` edge that the page body declares.
    from app.models import db as _db, Page as _Page, Wikilink as _Wikilink, Wiki as _Wiki, User as _User
    with app.app_context():
        _owner = _User.query.filter_by(username="agent1").first()
        _wiki = _Wiki.query.filter_by(owner_id=_owner.id, slug="graph-mix").first()
        _hub = _Page.query.filter_by(wiki_id=_wiki.id, path="public-hub.md").first()
        _priv = _Page.query.filter_by(wiki_id=_wiki.id, path="private-deets.md").first()
        _db.session.add(_Wikilink(source_page_id=_hub.id, target_path="private-deets", target_page_id=_priv.id))
        _db.session.commit()

    # Anon first to avoid login leakage.
    with app.test_request_context():
        logout_user()
    anon = app.test_client()
    r = anon.get("/@agent1/graph-mix/graph.json")
    assert r.status_code == 200, f"anon got {r.status_code}"
    data = _json.loads(r.data.decode("utf-8"))
    nodes = data.get("nodes", [])
    titles = [n.get("title", "") for n in nodes]
    paths = [n.get("url", "") for n in nodes]
    assert "Private Deets" not in titles, f"graph leaked private title: {titles}"
    assert not any("private-deets" in p for p in paths), f"graph leaked private path: {paths}"

    # anon page-level graph of the private page itself: 4xx
    r = anon.get("/@agent1/graph-mix/private-deets/graph.json")
    assert r.status_code in (401, 403, 404), (
        f"anon page-graph on private page returned {r.status_code}"
    )

    # Now exercise the owner branch.
    owner = app.test_client()
    r = owner.get(f"/auth/login?api_key={api_key}&next=/", follow_redirects=False)
    assert r.status_code == 302
    r = owner.get("/@agent1/graph-mix/graph.json")
    assert r.status_code == 200
    odata = _json.loads(r.data.decode("utf-8"))
    otitles = [n.get("title", "") for n in odata.get("nodes", [])]
    assert "Private Deets" in otitles, f"owner missing private node from graph: {otitles}"


def test_tag_index_filters_private_pages_for_anon(client, api_key):
    """wikihub-8888.3: tag index must not expose private tagged pages to anon."""
    from flask_login import logout_user
    app = client.application
    h = {"Authorization": f"Bearer {api_key}"}
    r = client.post("/api/v1/wikis", json={"slug": "tag-mix", "title": "Tag Mix"}, headers=h)
    assert r.status_code == 201

    r = client.post("/api/v1/wikis/agent1/tag-mix/pages", json={
        "path": "open.md",
        "content": "---\ntitle: Open Page\nvisibility: public\ntags: [shared-tag]\n---\n\nopen content",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201
    r = client.post("/api/v1/wikis/agent1/tag-mix/pages", json={
        "path": "hidden.md",
        "content": "---\ntitle: Hidden Page\nvisibility: private\ntags: [shared-tag]\n---\n\nhidden content",
        "visibility": "private",
    }, headers=h)
    assert r.status_code == 201

    # Anon first to avoid login leakage.
    with app.test_request_context():
        logout_user()
    anon = app.test_client()
    r = anon.get("/@agent1/tag-mix/tag/shared-tag")
    assert r.status_code == 200, f"anon got {r.status_code}"
    body = r.data.decode("utf-8", errors="replace")
    assert "Open Page" in body, "public page missing from anon tag index"
    assert "Hidden Page" not in body, "tag index leaked private page title to anon"
    assert "hidden.md" not in body, "tag index leaked private page path to anon"

    # Now exercise the owner branch.
    owner = app.test_client()
    r = owner.get(f"/auth/login?api_key={api_key}&next=/", follow_redirects=False)
    assert r.status_code == 302
    r = owner.get("/@agent1/tag-mix/tag/shared-tag")
    assert r.status_code == 200
    obody = r.data.decode("utf-8", errors="replace")
    assert "Open Page" in obody and "Hidden Page" in obody, "owner missing pages from tag index"


def test_owner_can_render_deep_nested_page_no_500(client, api_key):
    """Regression: logged-in owner GETting a deep-nested page path renders 200.

    The reader view does Proposal.query.filter_by(...).count() for the owner.
    If that query path is broken (e.g. by a schema/grant/import regression),
    EVERY non-root wiki page 500s for the owner. We had exactly this in prod
    when the proposals migration left tables owned by `postgres` instead of
    the app role, so SELECT was denied. This test exercises the Proposal
    codepath under the owner-session in the same way the prod request does.
    """
    h = {"Authorization": f"Bearer {api_key}"}

    r = client.post("/api/v1/wikis", json={"slug": "deep-render", "title": "Deep Render"}, headers=h)
    assert r.status_code == 201

    # public to keep ACL out of the picture
    deep_path = "2026-05-29/nvc/nvc_guide.md"
    r = client.post("/api/v1/wikis/agent1/deep-render/pages", json={
        "path": deep_path,
        "content": "---\ntitle: NVC Guide\nvisibility: public\n---\n\n# NVC Guide\n\nNested page body.",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201

    # log in as owner via the URL api_key path (same as prod owner session)
    r = client.get(f"/auth/login?api_key={api_key}&next=/", follow_redirects=False)
    assert r.status_code == 302

    # Hit the nested page as owner. This is what blew up in prod (proposals
    # SELECT permission denied) — even at the route level we must get 200.
    r = client.get("/@agent1/deep-render/2026-05-29/nvc/nvc_guide")
    assert r.status_code == 200, (
        f"owner GET of deep nested page must return 200, got {r.status_code}. "
        f"Body: {r.get_data(as_text=True)[:300]}"
    )
    assert b"NVC Guide" in r.data

    # Also exercise the SUMMARY-style root-of-folder path that prod 500'd on.
    r = client.post("/api/v1/wikis/agent1/deep-render/pages", json={
        "path": "2026-05-29/SUMMARY.md",
        "content": "---\ntitle: Summary\nvisibility: public\n---\n\n# Summary\n",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201
    r = client.get("/@agent1/deep-render/2026-05-29/SUMMARY")
    assert r.status_code == 200


def test_500_page_has_reference_and_retry(app, client):
    """Regression: the 500 error page must surface a correlation id, a try-again
    link to the same URL, and a Report-this link with the reference in it.

    Without this, prod 500s like the proposals-grant outage are debuggable
    only by SSH+log-tail. This test invokes the 500 errorhandler directly
    (Flask doesn't allow late route registration after the first request)
    and asserts the rendered body carries the reference + retry affordances.
    """
    from werkzeug.exceptions import InternalServerError

    # Need TESTING off so the errorhandler isn't bypassed; need PROPAGATE off
    # too. Restore at the end.
    prev_testing = app.config.get("TESTING")
    prev_propagate = app.config.get("PROPAGATE_EXCEPTIONS")
    app.config["TESTING"] = False
    app.config["PROPAGATE_EXCEPTIONS"] = False
    try:
        # Drive the registered errorhandler under a real request context for
        # a deep nested path with a query string — mirrors prod 500 shape.
        with app.test_request_context("/@jacobcole/otter-highlights/2026-05-29/SUMMARY?x=1"):
            handler = app.error_handler_spec[None][500].get(InternalServerError) \
                      or list(app.error_handler_spec[None][500].values())[0]
            resp = handler(InternalServerError())
            body = resp[0] if isinstance(resp, tuple) else resp.get_data(as_text=True)
            if hasattr(body, "decode"):
                body = body.decode("utf-8")
        import re as _re
        m = _re.search(r"Reference:\s*<strong>([0-9a-f]{8})</strong>", body)
        assert m, f"500 page missing 8-hex Reference. Body: {body[:500]}"
        ref = m.group(1)
        assert "/@jacobcole/otter-highlights/2026-05-29/SUMMARY" in body, \
            "500 page missing retry link to original URL"
        assert (f"ref%3D{ref}" in body) or (f"ref={ref}" in body), \
            "500 page Report link must include reference"
    finally:
        app.config["TESTING"] = prev_testing
        app.config["PROPAGATE_EXCEPTIONS"] = prev_propagate


def test_visibility_toggle_for_underscore_filename(client, api_key):
    """wikihub-vbug: clicking the visibility indicator on a page whose
    filename contains an underscore (e.g. nvc_tutorial.md) must succeed.

    Before the fix the URL→path normalization converted `_` to space
    unconditionally, so /pages/.../nvc_tutorial/visibility looked up
    `nvc tutorial.md` (which did not exist) and returned 404 "Page not
    found", while the page was clearly visible in the sidebar.
    """
    h = {"Authorization": f"Bearer {api_key}"}

    r = client.post("/api/v1/wikis", json={"slug": "vbug-wiki", "title": "VBug"}, headers=h)
    assert r.status_code == 201, r.get_data(as_text=True)

    # Page filename literally contains an underscore.
    r = client.post("/api/v1/wikis/agent1/vbug-wiki/pages", json={
        "path": "nvc/nvc_tutorial.md",
        "content": "---\ntitle: NVC Tutorial\nvisibility: private\n---\n\n# NVC Tutorial\n",
        "visibility": "private",
    }, headers=h)
    assert r.status_code == 201, r.get_data(as_text=True)

    # Visibility toggle from the reader UI uses the clean URL path
    # (no .md, underscore preserved). This MUST resolve.
    r = client.post(
        "/api/v1/wikis/agent1/vbug-wiki/pages/nvc/nvc_tutorial/visibility",
        json={"visibility": "public"}, headers=h,
    )
    assert r.status_code == 200, (
        f"visibility POST returned {r.status_code}: {r.get_data(as_text=True)}"
    )
    body = r.get_json()
    assert body["visibility"] == "public"
    assert body["path"] == "nvc/nvc_tutorial.md"

    # Read back via the same clean URL — must succeed too.
    r = client.get("/api/v1/wikis/agent1/vbug-wiki/pages/nvc/nvc_tutorial", headers=h)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["visibility"] == "public"

    # Toggle back to private — and confirm the visibility actually changed
    # (i.e. our fix didn't silently fail).
    r = client.post(
        "/api/v1/wikis/agent1/vbug-wiki/pages/nvc/nvc_tutorial/visibility",
        json={"visibility": "private"}, headers=h,
    )
    assert r.status_code == 200
    assert r.get_json()["visibility"] == "private"


def test_page_lookup_consistent_across_endpoints_for_underscore_path(client, api_key):
    """wikihub-wkmg + wikihub-vbug: every endpoint that takes a <path:page_path>
    must resolve the same DB row for the same URL path. The CLI's
    `wikihub write` does a GET to decide POST-vs-PUT — if GET returns 404 for a
    path that POST then 409's on, the page is permanently stuck.

    Cover GET, PUT, PATCH, DELETE, visibility POST, /pages POST (409 case)
    for a page whose filename contains an underscore.
    """
    h = {"Authorization": f"Bearer {api_key}"}

    r = client.post("/api/v1/wikis", json={"slug": "wkmg-wiki", "title": "WKMG"}, headers=h)
    assert r.status_code == 201

    r = client.post("/api/v1/wikis/agent1/wkmg-wiki/pages", json={
        "path": "nvc/nvc_tutorial.md",
        "content": "---\ntitle: First\nvisibility: private\n---\n\n# First\n",
        "visibility": "private",
    }, headers=h)
    assert r.status_code == 201

    # Listing shows the page.
    r = client.get("/api/v1/wikis/agent1/wkmg-wiki/pages", headers=h)
    assert r.status_code == 200
    paths = [p["path"] for p in r.get_json()["pages"]]
    assert "nvc/nvc_tutorial.md" in paths

    # The CLI hits this URL — must NOT 404.
    base = "/api/v1/wikis/agent1/wkmg-wiki/pages/nvc/nvc_tutorial"

    r = client.get(base, headers=h)
    assert r.status_code == 200, f"GET clean-URL must resolve: {r.get_data(as_text=True)}"

    # PUT (replace_page) — same URL must resolve.
    r = client.put(base, json={"content": "# Updated via PUT\n"}, headers=h)
    assert r.status_code == 200, f"PUT must resolve: {r.get_data(as_text=True)}"

    # PATCH (patch_page).
    r = client.patch(base, json={"content": "# Updated via PATCH\n"}, headers=h)
    assert r.status_code == 200, f"PATCH must resolve: {r.get_data(as_text=True)}"

    # Visibility toggle.
    r = client.post(base + "/visibility", json={"visibility": "public"}, headers=h)
    assert r.status_code == 200

    # DELETE.
    r = client.delete(base, headers=h)
    assert r.status_code == 204, f"DELETE must resolve: {r.get_data(as_text=True)}"

    # And after delete, GET must 404.
    r = client.get(base, headers=h)
    assert r.status_code == 404


def test_wiki_limit_effective_resolution_and_429(client, app):
    """wikihub-20ct: per-user wiki cap.

    - effective_wiki_limit() returns the config default when User.wiki_limit
      is NULL, and the override when set.
    - hitting the cap returns 429 too_many with the EFFECTIVE limit in the
      message (not the raw constant).
    - a per-user override lifts the cap for that user only.
    """
    from app.models import User

    r = client.post("/api/v1/accounts", json={"username": "limituser"})
    assert r.status_code == 201
    key = r.get_json()["api_key"]
    h = {"Authorization": f"Bearer {key}"}

    # 1) resolution: NULL override -> config default; set override wins.
    with app.app_context():
        u = User.query.filter_by(username="limituser").first()
        assert u.wiki_limit is None
        assert u.effective_wiki_limit() == app.config["MAX_WIKIS_PER_USER"]
        u.wiki_limit = 3
        db.session.commit()
        assert u.effective_wiki_limit() == 3

    # limituser already has a personal wiki (count=1). With override=3, two
    # more should succeed, the third create should 429.
    r = client.post("/api/v1/wikis", json={"slug": "w-a"}, headers=h)
    assert r.status_code == 201, r.get_data(as_text=True)
    r = client.post("/api/v1/wikis", json={"slug": "w-b"}, headers=h)
    assert r.status_code == 201, r.get_data(as_text=True)

    r = client.post("/api/v1/wikis", json={"slug": "w-c"}, headers=h)
    assert r.status_code == 429, f"expected 429 at cap: {r.get_data(as_text=True)}"
    body = r.get_json()
    assert body["error"] == "too_many"
    # message shows the effective limit (3), not the config default.
    assert "3" in body["message"]
    assert str(app.config["MAX_WIKIS_PER_USER"]) not in body["message"] \
        or app.config["MAX_WIKIS_PER_USER"] == 3

    r = client.post("/api/v1/accounts", json={"username": "forksource"})
    assert r.status_code == 201
    r = client.post("/api/v1/wikis/forksource/forksource/fork", headers=h)
    assert r.status_code == 429, f"fork should respect cap: {r.get_data(as_text=True)}"
    body = r.get_json()
    assert body["error"] == "too_many"
    assert "3" in body["message"]

    # 2) raising the override immediately unblocks creation for this user.
    with app.app_context():
        u = User.query.filter_by(username="limituser").first()
        u.wiki_limit = 100000
        db.session.commit()
    r = client.post("/api/v1/wikis", json={"slug": "w-c"}, headers=h)
    assert r.status_code == 201, f"override should unblock: {r.get_data(as_text=True)}"

    r = client.post("/api/v1/wikis/forksource/forksource/fork", headers=h)
    assert r.status_code == 201, f"override should unblock fork: {r.get_data(as_text=True)}"


def test_set_wiki_limit_script_imports_from_repo_root():
    """wikihub-20ct: documented helper invocation can import app."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run(
        [sys.executable, "scripts/set_wiki_limit.py"],
        cwd=repo_root,
        text=True,
        capture_output=True,
    )
    assert r.returncode == 2
    assert "Usage (from the app root" in r.stdout
    assert "ModuleNotFoundError" not in r.stderr


def test_side_peek_fragment_endpoint(client, api_key):
    """wikihub-9k18: ?fragment=1 returns the rendered article body as JSON so
    the side-peek panel can show a same-wiki link without navigating away."""
    h = {"Authorization": f"Bearer {api_key}"}
    client.post("/api/v1/wikis", json={"slug": "peek-wiki", "title": "Peek"}, headers=h)
    r = client.post("/api/v1/wikis/agent1/peek-wiki/pages", json={
        "path": "wiki/target.md",
        "content": "---\ntitle: Peek Target\nvisibility: public\n---\n\n# Peek Target\n\nBody **here**.",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201, r.get_json()

    r = client.get("/@agent1/peek-wiki/wiki/target?fragment=1")
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    assert "application/json" in r.content_type
    data = r.get_json()
    assert data["title"] == "Peek Target"
    assert "Body" in data["html"] and "<strong>here</strong>" in data["html"]
    # Fragment must be body-only: no full-page chrome (nav/sidebar/reader shell).
    assert "reader-layout" not in data["html"]
    assert "<html" not in data["html"].lower()
    # Canonical full-page URL is provided for "open as full page" / copy-link.
    assert data["url"] == "/@agent1/peek-wiki/wiki/target"


def test_side_peek_fragment_endpoint_preserves_md_redirect(client, api_key):
    """nsv-9wm: .md page links keep ?fragment=1 through the clean-URL redirect."""
    h = {"Authorization": f"Bearer {api_key}"}
    client.post("/api/v1/wikis", json={"slug": "peek-md", "title": "PeekMD"}, headers=h)
    r = client.post("/api/v1/wikis/agent1/peek-md/pages", json={
        "path": "wiki/target.md",
        "content": "---\ntitle: Markdown Link Target\nvisibility: public\n---\n\n# Markdown Link Target\n\nBody **here**.",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201, r.get_json()

    r = client.get("/@agent1/peek-md/wiki/target.md?fragment=1", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/@agent1/peek-md/wiki/target?fragment=1")

    r = client.get("/@agent1/peek-md/wiki/target.md?fragment=1", follow_redirects=True)
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    assert "application/json" in r.content_type
    data = r.get_json()
    assert data["title"] == "Markdown Link Target"
    assert "<strong>here</strong>" in data["html"]
    assert data["url"] == "/@agent1/peek-md/wiki/target"


def test_side_peek_fragment_respects_acl_for_anon(client, api_key):
    """wikihub-9k18: the fragment endpoint reuses the full page-route ACL, so a
    private page must NOT be readable via ?fragment=1 by an anonymous viewer."""
    h = {"Authorization": f"Bearer {api_key}"}
    client.post("/api/v1/wikis", json={"slug": "peek-acl", "title": "PeekACL"}, headers=h)
    r = client.post("/api/v1/wikis/agent1/peek-acl/pages", json={
        "path": "secret/plan.md",
        "content": "---\ntitle: Secret Plan\nvisibility: private\n---\n\n# Secret Plan\n\ntop secret",
        "visibility": "private",
    }, headers=h)
    assert r.status_code == 201, r.get_json()

    anon = client.application.test_client()
    anon.get("/auth/logout", follow_redirects=False)
    r = anon.get("/@agent1/peek-acl/secret/plan?fragment=1")
    assert r.status_code in (401, 403, 404), (
        f"private page leaked via fragment endpoint (got {r.status_code})"
    )
    body = r.get_data(as_text=True)
    assert "top secret" not in body, "fragment endpoint leaked private content to anon"


def test_side_peek_preserves_same_page_anchor_clicks():
    """nsv-7lr: in-page anchors should keep the browser's native scroll behavior."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const sidepeekPath = path.join(process.cwd(), "app/static/js/sidepeek.js");
const source = fs.readFileSync(sidepeekPath, "utf8");
let articleClickHandler = null;
let domReadyHandler = null;

const article = {
  addEventListener(type, fn) {
    if (type === "click") articleClickHandler = fn;
  }
};

const location = {
  href: "http://example.test/@agent1/peek-wiki/wiki/source",
  origin: "http://example.test",
  pathname: "/@agent1/peek-wiki/wiki/source",
  search: ""
};

const context = {
  URL,
  URLSearchParams,
  location,
  window: {
    __wikihubPeek: { base: "/@agent1/peek-wiki" },
    innerWidth: 1024,
    addEventListener() {},
    location
  },
  document: {
    body: { appendChild() {}, classList: { add() {}, remove() {} } },
    querySelector(selector) {
      return selector.indexOf(".article") >= 0 ? article : null;
    },
    addEventListener(type, fn) {
      if (type === "DOMContentLoaded") domReadyHandler = fn;
    }
  },
  history: { state: null, pushState() {}, replaceState() {}, back() {} },
  navigator: {},
  setTimeout() {}
};

vm.runInNewContext(source, context, { filename: sidepeekPath });
if (!domReadyHandler) throw new Error("DOMContentLoaded handler was not registered");
domReadyHandler();
if (!articleClickHandler) throw new Error("article click handler was not registered");

function makeAnchor(rawHref) {
  return {
    href: new URL(rawHref, location.href).href,
    target: "",
    classList: { contains() { return false; } },
    hasAttribute() { return false; },
    getAttribute(name) { return name === "href" ? rawHref : null; },
    closest(selector) {
      if (selector === "a") return this;
      if (selector === ".article, .peek-body") return article;
      return null;
    }
  };
}

function dispatch(rawHref) {
  let prevented = false;
  const anchor = makeAnchor(rawHref);
  articleClickHandler({
    defaultPrevented: false,
    button: 0,
    metaKey: false,
    ctrlKey: false,
    shiftKey: false,
    altKey: false,
    target: anchor,
    preventDefault() { prevented = true; }
  });
  if (prevented) throw new Error(rawHref + " was intercepted");
}

dispatch("#footnote-1");
dispatch("/@agent1/peek-wiki/wiki/source#toc");
"""
    r = subprocess.run(
        ["node", "-e", script],
        cwd=repo_root,
        text=True,
        capture_output=True,
    )
    assert r.returncode == 0, r.stderr or r.stdout


def test_side_peek_scrolls_peek_self_anchors():
    """nsv-7ki/nsv-oxc: peek links resolve and navigate inside the panel."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

(async () => {
  const sidepeekPath = path.join(process.cwd(), "app/static/js/sidepeek.js");
  const source = fs.readFileSync(sidepeekPath, "utf8");
  let articleClickHandler = null;
  let peekClickHandler = null;
  let domReadyHandler = null;
  let scrolledTo = [];
  let fetchedUrls = [];
  let pushedStates = [];
  let replacedStates = [];

  const article = {
    addEventListener(type, fn) {
      if (type === "click") articleClickHandler = fn;
    }
  };

  const targets = {
    "h.foo": { id: "h.foo", scrollIntoView() { scrolledTo.push("h.foo"); } },
    "2026": { id: "2026", scrollIntoView() { scrolledTo.push("2026"); } }
  };

  const bodyEl = {
    scrollTop: 0,
    classList: { add() {}, remove() {} },
    addEventListener(type, fn) {
      if (type === "click") peekClickHandler = fn;
    },
    set innerHTML(_html) {},
    querySelector(selector) {
      if (selector === "#escaped:h.foo") return targets["h.foo"];
      if (selector === "#escaped:2026") return targets["2026"];
      if (selector === "#h.foo" || selector === "#2026") {
        throw new Error("raw hash selector used: " + selector);
      }
      return null;
    },
    querySelectorAll() {
      return Object.values(targets);
    }
  };

  const noopEl = {
    classList: { add() {}, remove() {} },
    setAttribute() {},
    removeAttribute() {},
    addEventListener() {},
    querySelector() { return noopEl; },
    set href(_value) {}
  };

  const overlay = {
    offsetWidth: 1,
    classList: { add() {}, remove() {} },
    setAttribute() {},
    removeAttribute() {},
    addEventListener() {},
    querySelector(selector) {
      if (selector === "#peek-body") return bodyEl;
      return noopEl;
    },
    set innerHTML(_html) {}
  };

  const location = {
    href: "http://example.test/@agent1/peek-wiki/wiki/source",
    origin: "http://example.test",
    pathname: "/@agent1/peek-wiki/wiki/source",
    search: ""
  };

  const context = {
    URL,
    URLSearchParams,
    location,
    window: {
      __wikihubPeek: { base: "/@agent1/peek-wiki" },
      innerWidth: 1024,
      addEventListener() {},
      location,
      CSS: { escape(value) { return "escaped:" + value; } }
    },
    document: {
      body: {
        appendChild() {},
        classList: { add() {}, remove() {} }
      },
      createElement() { return overlay; },
      querySelector(selector) {
        return selector.indexOf(".article") >= 0 ? article : null;
      },
      addEventListener(type, fn) {
        if (type === "DOMContentLoaded") domReadyHandler = fn;
      }
    },
    history: {
      state: null,
      pushState(state, _title, url) {
        this.state = state;
        pushedStates.push({ state, url });
      },
      replaceState(state, _title, url) {
        this.state = state;
        replacedStates.push({ state, url });
      },
      back() {}
    },
    navigator: {},
    fetch() {
      fetchedUrls.push(arguments[0]);
      return Promise.resolve({
        ok: true,
        headers: { get() { return "application/json"; } },
        json() {
          return Promise.resolve({
            title: "Target",
            html: "<h2 id=\"h.foo\">H</h2><h2 id=\"2026\">Y</h2>",
            url: "/@agent1/peek-wiki/wiki/folder/target"
          });
        }
      });
    },
    setTimeout
  };

  vm.runInNewContext(source, context, { filename: sidepeekPath });
  domReadyHandler();

  function makeAnchor(rawHref, inPeek) {
    return {
      href: new URL(rawHref, location.href).href,
      target: "",
      classList: { contains() { return false; } },
      hasAttribute() { return false; },
      getAttribute(name) { return name === "href" ? rawHref : null; },
      closest(selector) {
        if (selector === "a") return this;
        if (selector === ".article, .peek-body") return inPeek ? bodyEl : article;
        if (selector === ".peek-body") return inPeek ? bodyEl : null;
        return null;
      }
    };
  }

  function dispatch(handler, rawHref, inPeek) {
    let prevented = false;
    const anchor = makeAnchor(rawHref, inPeek);
    handler({
      defaultPrevented: false,
      button: 0,
      metaKey: false,
      ctrlKey: false,
      shiftKey: false,
      altKey: false,
      target: anchor,
      preventDefault() { prevented = true; }
    });
    return prevented;
  }

  if (!dispatch(articleClickHandler, "/@agent1/peek-wiki/wiki/folder/target#h.foo", false)) {
    throw new Error("cross-page peek link was not intercepted");
  }
  await new Promise((resolve) => setTimeout(resolve, 0));
  if (scrolledTo.join(",") !== "h.foo") {
    throw new Error("peek did not scroll escaped load hash; got " + scrolledTo.join(","));
  }
  if (!peekClickHandler) throw new Error("peek click handler was not registered");
  if (pushedStates.length !== 1) {
    throw new Error("initial peek should push one history entry; got " + pushedStates.length);
  }

  if (!dispatch(peekClickHandler, "/@agent1/peek-wiki/wiki/folder/target#2026", true)) {
    throw new Error("peek self-anchor was not intercepted");
  }
  if (scrolledTo.join(",") !== "h.foo,2026") {
    throw new Error("peek self-anchor did not scroll inside panel; got " + scrolledTo.join(","));
  }

  if (!dispatch(peekClickHandler, "next", true)) {
    throw new Error("peek relative link was not intercepted");
  }
  await new Promise((resolve) => setTimeout(resolve, 0));
  if (!fetchedUrls[1] || fetchedUrls[1].indexOf("/@agent1/peek-wiki/wiki/folder/next?fragment=1") < 0) {
    throw new Error("peek relative link resolved to wrong fetch URL: " + fetchedUrls.join(","));
  }
  if (pushedStates.length !== 1) {
    throw new Error("in-panel navigation pushed a new history entry");
  }
  if (replacedStates.length !== 1) {
    throw new Error("in-panel navigation should replace history once; got " + replacedStates.length);
  }
})().catch((err) => {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
"""
    r = subprocess.run(
        ["node", "-e", script],
        cwd=repo_root,
        text=True,
        capture_output=True,
    )
    assert r.returncode == 0, r.stderr or r.stdout


def test_side_peek_ignores_stale_fetch_responses():
    """nsv-7nv: older side-peek fetches must not overwrite newer clicks."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

(async () => {
  const sidepeekPath = path.join(process.cwd(), "app/static/js/sidepeek.js");
  const source = fs.readFileSync(sidepeekPath, "utf8");
  let articleClickHandler = null;
  let domReadyHandler = null;
  let renderedHtml = "";
  const requests = [];
  const pushedUrls = [];

  const article = {
    addEventListener(type, fn) {
      if (type === "click") articleClickHandler = fn;
    }
  };

  const bodyEl = {
    scrollTop: 0,
    classList: { add() {}, remove() {} },
    addEventListener() {},
    set innerHTML(value) { renderedHtml = value; },
    querySelector() { return null; },
    querySelectorAll() { return []; }
  };

  const titleEl = { textContent: "" };
  const noopEl = {
    classList: { add() {}, remove() {} },
    setAttribute() {},
    removeAttribute() {},
    addEventListener() {},
    querySelector() { return noopEl; },
    set href(_value) {}
  };

  const overlay = {
    offsetWidth: 1,
    classList: { add() {}, remove() {} },
    setAttribute() {},
    removeAttribute() {},
    addEventListener() {},
    querySelector(selector) {
      if (selector === "#peek-body") return bodyEl;
      if (selector === "#peek-title") return titleEl;
      return noopEl;
    },
    set innerHTML(_html) {}
  };

  const location = {
    href: "http://example.test/@agent1/peek-wiki/wiki/source",
    origin: "http://example.test",
    pathname: "/@agent1/peek-wiki/wiki/source",
    search: ""
  };

  const context = {
    URL,
    URLSearchParams,
    location,
    window: {
      __wikihubPeek: { base: "/@agent1/peek-wiki" },
      innerWidth: 1024,
      addEventListener() {},
      location
    },
    document: {
      body: {
        appendChild() {},
        classList: { add() {}, remove() {} }
      },
      createElement() { return overlay; },
      querySelector(selector) {
        return selector.indexOf(".article") >= 0 ? article : null;
      },
      addEventListener(type, fn) {
        if (type === "DOMContentLoaded") domReadyHandler = fn;
      }
    },
    history: {
      state: null,
      pushState(state, _title, url) {
        this.state = state;
        pushedUrls.push(url);
      },
      replaceState() {},
      back() {}
    },
    navigator: {},
    fetch(url) {
      let resolveResponse;
      const promise = new Promise((resolve) => { resolveResponse = resolve; });
      requests.push({
        url,
        resolve(data) {
          resolveResponse({
            ok: true,
            headers: { get() { return "application/json"; } },
            json() { return Promise.resolve(data); }
          });
        }
      });
      return promise;
    },
    setTimeout
  };

  vm.runInNewContext(source, context, { filename: sidepeekPath });
  domReadyHandler();

  function makeAnchor(rawHref) {
    return {
      href: new URL(rawHref, location.href).href,
      target: "",
      classList: { contains() { return false; } },
      hasAttribute() { return false; },
      getAttribute(name) { return name === "href" ? rawHref : null; },
      closest(selector) {
        if (selector === "a") return this;
        if (selector === ".article, .peek-body") return article;
        if (selector === ".peek-body") return null;
        return null;
      }
    };
  }

  function dispatch(rawHref) {
    const anchor = makeAnchor(rawHref);
    articleClickHandler({
      defaultPrevented: false,
      button: 0,
      metaKey: false,
      ctrlKey: false,
      shiftKey: false,
      altKey: false,
      target: anchor,
      preventDefault() {}
    });
  }

  dispatch("/@agent1/peek-wiki/wiki/slow");
  dispatch("/@agent1/peek-wiki/wiki/fast");
  if (requests.length !== 2) {
    throw new Error("expected two side-peek fetches, got " + requests.length);
  }

  requests[1].resolve({
    title: "Fast",
    html: "<p>fast</p>",
    url: "/@agent1/peek-wiki/wiki/fast"
  });
  await new Promise((resolve) => setTimeout(resolve, 0));
  requests[0].resolve({
    title: "Slow",
    html: "<p>slow</p>",
    url: "/@agent1/peek-wiki/wiki/slow"
  });
  await new Promise((resolve) => setTimeout(resolve, 0));

  if (renderedHtml !== "<p>fast</p>") {
    throw new Error("stale fetch overwrote latest render: " + renderedHtml);
  }
  if (titleEl.textContent !== "Fast") {
    throw new Error("stale fetch overwrote latest title: " + titleEl.textContent);
  }
  if (pushedUrls.length !== 1 || pushedUrls[0] !== "/@agent1/peek-wiki/wiki/source?peek=wiki%2Ffast") {
    throw new Error("history was not updated only for latest fetch: " + pushedUrls.join(","));
  }
})().catch((err) => {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
"""
    r = subprocess.run(
        ["node", "-e", script],
        cwd=repo_root,
        text=True,
        capture_output=True,
    )
    assert r.returncode == 0, r.stderr or r.stdout


def test_page_meta_liveness_endpoint(client, api_key):
    """Live-reload indicator: GET ?meta=1 returns content_hash for cheap polling,
    updates the hash when content changes, and enforces the same ACL as a full read
    (no leak to anon on private pages)."""
    from flask_login import logout_user
    app = client.application
    h = {"Authorization": f"Bearer {api_key}"}
    r = client.post("/api/v1/wikis", json={"slug": "live-mix", "title": "Live"}, headers=h)
    assert r.status_code == 201

    # A public page + a private page.
    r = client.post("/api/v1/wikis/agent1/live-mix/pages", json={
        "path": "note.md",
        "content": "# Note\n\nv1",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201
    r = client.post("/api/v1/wikis/agent1/live-mix/pages", json={
        "path": "secret.md",
        "content": "# Secret\n\nhidden",
        "visibility": "private",
    }, headers=h)
    assert r.status_code == 201

    meta_url = "/api/v1/wikis/agent1/live-mix/pages/note.md?meta=1"

    # meta=1 returns hash but NOT the body content.
    r = client.get(meta_url, headers=h)
    assert r.status_code == 200, f"meta poll got {r.status_code}"
    data = r.get_json()
    assert data.get("content_hash"), "meta response missing content_hash"
    assert "content" not in data, "meta response must not include full content"
    first_hash = data["content_hash"]
    assert r.headers.get("ETag") == f'"{first_hash}"'

    # After an edit, the hash changes — this is what the reader poll detects.
    r = client.put("/api/v1/wikis/agent1/live-mix/pages/note.md", json={
        "content": "# Note\n\nv2 updated",
    }, headers=h)
    assert r.status_code == 200
    r = client.get(meta_url, headers=h)
    assert r.status_code == 200
    assert r.get_json()["content_hash"] != first_hash, "hash must change after edit"

    # ACL: anon must NOT be able to poll a private page's hash. wikihub-dkp8
    # intentionally distinguishes existing-but-restricted API reads from
    # missing pages, so this is 401/403 instead of the old ambiguous 404.
    with app.test_request_context():
        logout_user()
    anon = app.test_client()
    r = anon.get("/api/v1/wikis/agent1/live-mix/pages/secret.md?meta=1")
    assert r.status_code in (401, 403), f"anon meta poll on private page expected restricted status, got {r.status_code}"
    assert "content_hash" not in (r.get_json() or {}), "meta leaked private hash to anon"

    # But anon CAN poll a public page (the KB / unlisted use case).
    r = anon.get(meta_url)
    assert r.status_code == 200, f"anon meta poll on public page got {r.status_code}"
    assert r.get_json().get("content_hash"), "anon missing hash on public page"


def test_reader_has_live_update_indicator(client, api_key):
    """The reader page must ship the polling script + non-modal update pill."""
    h = {"Authorization": f"Bearer {api_key}"}
    client.post("/api/v1/wikis", json={"slug": "live-ui", "title": "Live UI"}, headers=h)
    client.post("/api/v1/wikis/agent1/live-ui/pages", json={
        "path": "watch.md", "content": "# Watch\n\nbody", "visibility": "public",
    }, headers=h)
    r = client.get("/@agent1/live-ui/watch")
    assert r.status_code == 200
    body = r.data.decode("utf-8", errors="replace")
    assert 'id="page-update-pill"' in body, "reader missing update pill element"
    assert "?meta=1" in body, "reader missing meta liveness poll"


def test_pinned_pages_sort_to_top(app, client, api_key):
    """wikihub-o9fh: a page with frontmatter `pinned: true` floats to the top of
    the wiki sidebar, above alphabetically-earlier non-pinned pages, and carries a
    `pinned` flag in the sidebar tree/JSON.
    """
    import app.routes.wiki as wiki_routes

    h = {"Authorization": f"Bearer {api_key}"}
    r = client.post("/api/v1/wikis", json={"slug": "pins", "title": "Pins"}, headers=h)
    assert r.status_code == 201
    # alphabetically-first page, NOT pinned
    r = client.post("/api/v1/wikis/agent1/pins/pages", json={
        "path": "aaa.md",
        "content": "---\ntitle: Aaa\nvisibility: public\n---\n\n# Aaa",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201
    # alphabetically-last page, PINNED — must still sort first
    r = client.post("/api/v1/wikis/agent1/pins/pages", json={
        "path": "zzz.md",
        "content": "---\ntitle: Zzz\npinned: true\nvisibility: public\n---\n\n# Zzz",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201

    # exercise the sidebar tree (same builder feeds reader.html + sidebar.json)
    r = client.get("/@agent1/pins/sidebar.json")
    assert r.status_code == 200, f"sidebar.json: {r.status_code} {r.data[:200]}"
    tree = r.get_json()
    paths = [item.get("path") for item in tree]
    assert paths[0] == "zzz.md", f"pinned page must sort first, got order {paths}"
    assert tree[0].get("pinned") is True, "pinned page must carry pinned=True"
    assert "aaa.md" in paths, "non-pinned page still listed"
    zzz_idx = paths.index("zzz.md")
    aaa_idx = paths.index("aaa.md")
    assert zzz_idx < aaa_idx, "pinned page must precede non-pinned"

    # reader HTML marks the pinned page for the top-section divider styling
    r = client.get("/@agent1/pins/zzz")
    assert r.status_code == 200
    body = r.data.decode("utf-8", errors="replace")
    assert "sidebar-pinned" in body, "reader sidebar should mark pinned pages"
    assert "#async-sidebar > .sidebar-pinned + :not(.sidebar-pinned)" in body, "reader async sidebar keeps pinned divider"

    r = client.get("/@agent1/pins")
    assert r.status_code == 200
    body = r.data.decode("utf-8", errors="replace")
    assert "#async-sidebar > .sidebar-pinned + :not(.sidebar-pinned)" in body, "folder async sidebar keeps pinned divider"


def test_pinned_unreadable_hidden(app, client, api_key):
    """wikihub-o9fh: a pinned page the viewer can't read simply doesn't appear —
    pinning never overrides read permissions.
    """
    h = {"Authorization": f"Bearer {api_key}"}
    r = client.post("/api/v1/wikis", json={"slug": "pinsec", "title": "PinSec"}, headers=h)
    assert r.status_code == 201
    r = client.post("/api/v1/wikis/agent1/pinsec/pages", json={
        "path": "public.md",
        "content": "---\ntitle: Public\nvisibility: public\n---\n\n# Public",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201
    # pinned BUT private — an anonymous viewer must not see it
    r = client.post("/api/v1/wikis/agent1/pinsec/pages", json={
        "path": "secret.md",
        "content": "---\ntitle: Secret\npinned: true\nvisibility: private\n---\n\n# Secret",
        "visibility": "private",
    }, headers=h)
    assert r.status_code == 201

    anon = app.test_client()
    r = anon.get("/@agent1/pinsec/sidebar.json")
    assert r.status_code == 200
    paths = [item.get("path") for item in r.get_json()]
    assert "public.md" in paths, "public page visible to anon"
    assert "secret.md" not in paths, "pinned-but-private page must be hidden from anon"


def test_empty_sidebar_copy(app, client, api_key):
    """wikihub-l3z2: a wiki with no visible pages shows explicit copy, not blankness."""
    # fresh owner with a password so we can drive a logged-in browser session
    r = client.post("/api/v1/accounts", json={"username": "hollowowner", "password": "testpass12345"})
    assert r.status_code == 201
    oh = {"Authorization": f"Bearer {r.get_json()['api_key']}"}
    r = client.post("/api/v1/wikis", json={"slug": "hollow", "title": "Hollow"}, headers=oh)
    assert r.status_code == 201

    browser = app.test_client()
    login = browser.post("/auth/login", data={"username": "hollowowner", "password": "testpass12345"}, follow_redirects=False)
    assert login.status_code == 302
    # owner views the brand-new empty wiki (folder.html render, empty sidebar)
    r = browser.get("/@hollowowner/hollow")
    assert r.status_code == 200, f"owner empty-wiki view: {r.status_code}"
    body = r.data.decode("utf-8", errors="replace")
    assert "No listed pages visible to you." in body, "empty sidebar must show explicit copy"


def test_empty_profile_copy_no_leak(app, client, api_key):
    """wikihub-l3z2: an anonymous view of a profile with no visible content shows
    unconditional copy, and that copy is IDENTICAL whether or not the account has
    unlisted content (no information leak). Zero-case wiki count reads 'No public wikis'.
    """
    # fresh account: personal wiki auto-created, no pages
    r = client.post("/api/v1/accounts", json={"username": "hushacct", "password": "testpass12345"})
    assert r.status_code == 201
    owner_key = r.get_json()["api_key"]

    anon = app.test_client()
    before = anon.get("/@hushacct")
    assert before.status_code == 200
    before_body = before.data.decode("utf-8", errors="replace")
    assert "No public pages here" in before_body, "empty profile must show explicit copy"
    assert "No public wikis" in before_body, "zero-case count reads 'No public wikis'"

    # add UNLISTED content — must NOT change the anonymous profile surface
    oh = {"Authorization": f"Bearer {owner_key}"}
    r = client.post("/api/v1/wikis/hushacct/hushacct/pages", json={
        "path": "hidden.md",
        "content": "---\ntitle: Hidden\nvisibility: unlisted\n---\n\n# Hidden secret plans",
        "visibility": "unlisted",
    }, headers=oh)
    assert r.status_code == 201

    after = anon.get("/@hushacct")
    assert after.status_code == 200
    after_body = after.data.decode("utf-8", errors="replace")
    assert "No public pages here" in after_body, "copy unchanged with unlisted content (no leak)"
    assert "No public wikis" in after_body, "count wording unchanged with unlisted content"
    assert "Hidden" not in after_body, "unlisted page title must not leak on profile"
    assert "/hushacct/hidden" not in after_body, "unlisted page path must not leak on profile"


def test_nav_header_nowrap(client):
    """wikihub-gnat: top-bar nav labels must not wrap at phone widths. Assert the
    responsive guards (nowrap on nav links + narrow breakpoint) are served.
    """
    r = client.get("/")
    assert r.status_code == 200
    body = r.data.decode("utf-8", errors="replace")
    assert ".nav-link {" in body and "white-space: nowrap" in body, "nav links must be nowrap"
    assert "@media (max-width: 480px)" in body, "narrow-width nav breakpoint must exist"


def run_all():
    app = setup()

    with app.app_context():
        client = app.test_client()
        tests = [
            ("agent account creation", lambda: test_agent_account_creation(client)),
        ]

        # run account creation first to get key
        print("Running tests...\n")
        key = None
        try:
            key = test_agent_account_creation(client)
            print("  PASS  agent account creation")
        except AssertionError as e:
            print(f"  FAIL  agent account creation: {e}")
            return 1

        test_funcs = [
            ("wiki lifecycle", lambda: test_wiki_lifecycle(client, key)),
            ("gdoc TOC anchors rewritten (wikihub-vcrq)", lambda: test_gdoc_toc_anchors_rewritten(client, key)),
            ("WYSIWYG editor surface (wikihub-s2cj)", lambda: test_wysiwyg_editor_surface(client, key)),
            ("page ETag conflict", lambda: test_page_etag_conflict(client, key)),
            ("authenticated bulk write rate limits", lambda: test_authenticated_bulk_writes_rate_limit(client, key, app)),
            ("binary file serving", lambda: test_binary_file_serving(client, key)),
            ("unlisted-view ACL default readable by anon (issue #15)", lambda: test_unlisted_view_acl_default_readable_by_anon(client, key)),
            ("search", lambda: test_search(client, key)),
            ("reader owner visibility control", lambda: test_reader_owner_visibility_control(client, key)),
            ("search respects ACL shares", lambda: test_search_respects_acl_shares(client, key)),
            ("social (star + fork)", lambda: test_social(client, key)),
            ("activity feed filters private and shows social events", lambda: test_activity_feed_filters_private_and_shows_social_events(client, key)),
            ("curator sidebar only renders when usable", lambda: test_curator_sidebar_only_renders_when_usable(app, client, key)),
            ("zip upload", lambda: test_zip_upload(client, key)),
            ("anonymous upload (wikihub-i2xm)", lambda: test_anonymous_upload(app)),
            ("agent surfaces", lambda: test_agent_surfaces(client)),
            ("A2HS banner mobile-only (wikihub-2q0d)", lambda: test_a2hs_banner_gated_to_mobile(client)),
            ("token + settings", lambda: test_token_and_settings(client)),
            ("client_config hint", lambda: test_client_config_hint(client)),
            ("magic link login", lambda: test_magic_link_login(client)),
            ("sign-in flow redirects back to target (wikihub-kvwh)", lambda: test_signin_flow_redirects_back_to_target(app, client)),
            ("logout (wikihub-uq9)", lambda: test_logout(client)),
            ("unlisted page in sidebar but not discovery (wikihub #17)", lambda: test_unlisted_page_in_sidebar_but_not_discovery(app, client, key)),
            ("ACL file changes reindex inherited visibility without discovery leaks", lambda: test_acl_file_updates_reindex_inherited_visibility_without_discovery_leaks(app, client, key)),
            ("email verification flow (wikihub-ks5t.3)", lambda: test_email_verification_flow(client)),
            ("password reset flow (wikihub-ks5t.5)", lambda: test_password_reset_lifecycle(client)),
            ("Google auto-link security (wikihub-ks5t.4)", lambda: test_google_auto_link_security(app)),
            ("Google OAuth preserves next + invite context (wikihub-gtrq)", lambda: test_google_oauth_preserves_next_and_invite_context(app, client, key)),
            ("login redirects back (?next + Referer fallback)", lambda: test_login_redirect_back(client)),
            ("URL login (GET ?api_key / ?password)", lambda: test_url_login(client)),
            ("URL login — log redaction", lambda: test_url_login_log_redaction()),
            ("login POST without Referer succeeds with CSRF token (wikihub-m8zi)", lambda: test_login_post_without_referer_succeeds_with_csrf_token_wikihub_m8zi(client)),
            ("magic link from password", lambda: test_magic_link_from_password(client)),
            ("ACL permissions", lambda: test_acl_permissions(client, key)),
            ("private /new requires write access", lambda: test_private_new_page_requires_write_access(client, key)),
            ("anonymous public edit", lambda: test_anonymous_public_edit(client, key)),
            ("public-edit shows Edit button", lambda: test_public_edit_shows_edit_button(client, key)),
            ("anonymous posting + claim (wikihub-7b2r)", lambda: test_anonymous_posting_and_claim(client)),
            ("people directory + profiles", lambda: test_people_directory_and_profiles(client, key)),
            ("new folder UI", lambda: test_new_folder_ui(client)),
            ("sidebar indentation (wikihub-58c regression guard)", lambda: test_sidebar_indentation(client, key)),
            ("QR code affordance on reader page (wikihub-x622)", lambda: test_reader_qr_code_affordance(client, key)),
            ("relative links resolve inside wiki on subdomain (wikihub-qmx6)", lambda: test_relative_links_resolve_inside_wiki_on_subdomain(client, key)),
            ("wikipedia-style URLs", lambda: test_wikipedia_urls(client, key)),
            ("sharing lifecycle", lambda: test_sharing_lifecycle(client, key)),
            ("wiki-level sharing", lambda: test_wiki_level_sharing(client, key)),
            ("folder-level sharing", lambda: test_folder_level_sharing(client, key)),
            ("api root discovery", lambda: test_api_root_discovery(client)),
            ("feedback submission", lambda: test_feedback_submission(client)),
            ("me capabilities", lambda: test_me_capabilities(client, key)),
            ("frontmatter title renders h1", lambda: test_frontmatter_title_renders_h1(client, key)),
            ("![[file.html]] inline iframe + new-tab links (wikihub-wz2j)", lambda: test_html_embed_inline_iframe(client, key)),
            ("HTML viewer embedded in reader chrome (wikihub-ntpc)", lambda: test_html_viewer_embedded_in_reader_chrome(client, key)),
            ("soft line breaks render as visual break (wikihub-eiv7)", lambda: test_soft_line_breaks_render_as_visual_break()),
            ("admin claude-auth page requires token", lambda: test_admin_claude_auth_page_requires_token(client)),
            ("history API with anon + deleted page", lambda: test_history_api_with_anon_and_deleted_page(client, key)),
            ("API CORS headers", lambda: test_api_cors_headers(client, key)),
            ("list wikis API", lambda: test_list_wikis_api(client, key)),
            ("bulk sharing (wikihub-iga9)", lambda: test_bulk_sharing(client, key)),
            ("suggested edit proposal flow (wikihub-b6lc)", lambda: test_suggested_edit_proposal_flow(client, key)),
            ("proposal comments + revisions (wikihub-7cus)", lambda: test_proposal_comments_and_revision_flow(client, key)),
            ("pending invite lifecycle (wikihub-skp7)", lambda: test_pending_invite_lifecycle(client, key)),
            ("share sends email (wikihub-exj1 mock)", lambda: test_share_sends_email(client, key)),
            ("private surface offers request access", lambda: test_permission_error_offers_request_access(client, key)),
            ("access requests stay ambiguous + notify existing target", lambda: test_access_request_constant_response_and_notify_existing_target(client)),
            ("subdomain routing", lambda: test_subdomain_routing(client)),
            ("agent chat blocks cross-user private read (wikihub-7w40)", lambda: test_agent_chat_blocks_cross_user_private_read(client, key)),
            ("agent chat anon session blocked (wikihub-7w40)", lambda: test_agent_chat_anon_session_blocked(client)),
            ("agent chat session locked to creator (wikihub-7w40)", lambda: test_agent_chat_session_locked_to_creator(client, key)),
            ("agent chat search filters private pages (wikihub-7w40)", lambda: test_agent_chat_search_filters_private_pages(client, key)),
            ("agent chat resists prompt-injection ACL bypass (wikihub-7w40)", lambda: test_agent_chat_resists_prompt_injection_for_acl_bypass(client, key)),
            ("agent chat disabled returns 503 (wikihub-7w40)", lambda: test_agent_chat_disabled_returns_503(app, client)),
            ("backlinks API + forward-ref fallback (wikihub-yqe6)", lambda: test_backlinks_api(client, key)),
            ("highlight.js script URL is canonical (wikihub-1rx9)", lambda: test_highlight_js_script_url_is_canonical()),
            ("nginx serves Service-Worker-Allowed header (wikihub-o1ib)", lambda: test_nginx_serves_service_worker_allowed_header()),
            ("nginx does not intercept Flask errors (wikihub-fg1p)", lambda: test_nginx_does_not_intercept_flask_errors(client)),
            ("welcome.html has Sign in link (wikihub-46ke)", lambda: test_welcome_html_has_sign_in_link()),
            ("search trigger visible on mobile (wikihub-31s3)", lambda: test_search_trigger_visible_on_mobile()),
            ("search modal mobile UX fixes (wikihub-zlgt)", lambda: test_search_modal_mobile_ux_fixes_wikihub_zlgt()),
            ("search detectScope subdomain URL form (wikihub-zlgt)", lambda: test_search_detect_scope_matches_subdomain_url_form_wikihub_zlgt()),
            ("unauth private page renders permission_error with Sign in (wikihub-ffqt)", lambda: test_unauth_private_page_renders_permission_error_with_sign_in(client)),
            ("restricted (403) vs not-found (404) distinction (wikihub-dkp8)", lambda: test_restricted_vs_not_found_distinction(client)),
            ("mobile hamburger exposes hidden nav (wikihub-pz27)", lambda: test_mobile_hamburger_exposes_hidden_nav_links()),
            ("error pages iPad alignment fix (wikihub-dw8u)", lambda: test_error_page_ipad_alignment_fix()),
            ("md request for private page returns 4xx (wikihub-3rjt)", lambda: test_md_request_for_private_page_returns_json_4xx_not_landing(client)),
            ("/api/wikis 401+WWW-Authenticate (wikihub-uonp)", lambda: test_api_wikis_endpoint_returns_401_with_www_authenticate_for_private(client)),
            ("logged-out search returns only public (wikihub-7dml)", lambda: test_logged_out_search_returns_only_public(client)),
            ("history/commit ACL-gated for private wiki (wikihub-8888.1)", lambda: test_history_route_acl_gated_for_private_wiki(client, key)),
            ("commit diff renders with async sidebar (wikihub-8vwd)", lambda: test_commit_diff_renders_when_sidebar_is_async(client, key)),
            ("graph filters private pages for anon (wikihub-8888.2)", lambda: test_graph_route_filters_private_pages_for_anon(client, key)),
            ("tag index filters private pages for anon (wikihub-8888.3)", lambda: test_tag_index_filters_private_pages_for_anon(client, key)),
            ("page meta liveness endpoint (live-reload)", lambda: test_page_meta_liveness_endpoint(client, key)),
            ("reader has live update indicator (live-reload)", lambda: test_reader_has_live_update_indicator(client, key)),
            ("owner renders deep nested page (proposals-grant regression)", lambda: test_owner_can_render_deep_nested_page_no_500(client, key)),
            ("500 page has reference + retry link", lambda: test_500_page_has_reference_and_retry(app, client)),
            ("visibility toggle resolves underscore filename (wikihub-vbug)", lambda: test_visibility_toggle_for_underscore_filename(client, key)),
            ("page lookup consistent across endpoints for underscore path (wikihub-wkmg+vbug)", lambda: test_page_lookup_consistent_across_endpoints_for_underscore_path(client, key)),
            ("side peek fragment endpoint (wikihub-9k18)", lambda: test_side_peek_fragment_endpoint(client, key)),
            ("side peek .md fragment redirect (nsv-9wm)", lambda: test_side_peek_fragment_endpoint_preserves_md_redirect(client, key)),
            ("side peek fragment respects ACL for anon (wikihub-9k18)", lambda: test_side_peek_fragment_respects_acl_for_anon(client, key)),
            ("side peek preserves same-page anchors (nsv-7lr)", lambda: test_side_peek_preserves_same_page_anchor_clicks()),
            ("side peek scrolls peek self anchors (nsv-7ki)", lambda: test_side_peek_scrolls_peek_self_anchors()),
            ("side peek ignores stale fetch responses (nsv-7nv)", lambda: test_side_peek_ignores_stale_fetch_responses()),
            ("CLI end-to-end", lambda: test_cli(client)),
            ("pinned pages sort to top (wikihub-o9fh)", lambda: test_pinned_pages_sort_to_top(app, client, key)),
            ("pinned-but-unreadable hidden (wikihub-o9fh)", lambda: test_pinned_unreadable_hidden(app, client, key)),
            ("empty sidebar copy (wikihub-l3z2)", lambda: test_empty_sidebar_copy(app, client, key)),
            ("empty profile copy + no leak (wikihub-l3z2)", lambda: test_empty_profile_copy_no_leak(app, client, key)),
            ("nav header nowrap at narrow widths (wikihub-gnat)", lambda: test_nav_header_nowrap(client)),
            ("per-user wiki limit + 429 (wikihub-20ct)", lambda: test_wiki_limit_effective_resolution_and_429(client, app)),
            ("set_wiki_limit imports from app root (wikihub-20ct)", lambda: test_set_wiki_limit_script_imports_from_repo_root()),
            ("global activity excludes non-public content", lambda: test_global_activity_excludes_non_public(client, key)),
            ("activity RSS well-formed + correct links (both hosts)", lambda: test_activity_rss_is_well_formed_with_correct_links(client, key)),
            ("private-only wiki RSS does not leak metadata", lambda: test_wiki_activity_rss_private_only_wiki_does_not_leak_metadata(client, key)),
            ("per-wiki RSS finds older unlisted pages after private pages", lambda: test_wiki_activity_rss_keeps_older_unlisted_after_private_pages(client, key)),
            ("global activity rows avoid nested anchors", lambda: test_global_activity_rows_do_not_nest_anchors(client, key)),
            ("global activity pagination", lambda: test_activity_pagination(client, key)),
        ]

        passed = 1  # account creation already passed
        failed = 0
        for name, fn in test_funcs:
            _write_timestamps.clear()
            _ip_write_timestamps.clear()
            try:
                fn()
                print(f"  PASS  {name}")
                passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}")
                failed += 1
            except Exception as e:
                print(f"  ERROR {name}: {e}")
                failed += 1

        print(f"\n{passed} passed, {failed} failed")

        # cleanup DB
        reset_database()

    teardown()
    return 1 if failed else 0


def _seed_activity_fixtures(client, key):
    """Create a public wiki + an unlisted wiki that also holds a private page.

    Returns the header dict. Used by the activity-feed tests below.
    """
    h = {"Authorization": f"Bearer {key}"}
    # public wiki with a public page
    client.post("/api/v1/wikis", json={"slug": "pubwiki", "title": "Public Wiki"}, headers=h)
    client.post("/api/v1/wikis/agent1/pubwiki/pages", json={
        "path": "wiki/public-note.md",
        "content": "---\ntitle: Public Note\nvisibility: public\n---\n\n# Public Note\n",
        "visibility": "public",
    }, headers=h)
    # unlisted wiki: one unlisted page (reachable by link) + one private page (secret)
    client.post("/api/v1/wikis", json={"slug": "secretwiki", "title": "Secret Wiki"}, headers=h)
    client.post("/api/v1/wikis/agent1/secretwiki/pages", json={
        "path": "wiki/unlisted-note.md",
        "content": "---\ntitle: Unlisted Note\nvisibility: unlisted\n---\n\n# Unlisted Note\n",
        "visibility": "unlisted",
    }, headers=h)
    client.post("/api/v1/wikis/agent1/secretwiki/pages", json={
        "path": "wiki/private-note.md",
        "content": "---\ntitle: Private Note\nvisibility: private\n---\n\n# Private Note\n",
        "visibility": "private",
    }, headers=h)
    return h


def test_global_activity_excludes_non_public(client, key):
    """Global /activity page + /activity.rss show public pages only; unlisted and
    private never leak into the site-wide surface."""
    _seed_activity_fixtures(client, key)

    # HTML page (anonymous request)
    r = client.get("/activity")
    assert r.status_code == 200, f"/activity should render, got {r.status_code}"
    body = r.get_data(as_text=True)
    assert "Public Note" in body, "public page must appear in global activity"
    assert "Unlisted Note" not in body, "unlisted page must NOT appear in global activity"
    assert "Private Note" not in body, "private page must NOT appear in global activity"

    # RSS feed (anonymous request)
    r = client.get("/activity.rss")
    assert r.status_code == 200
    assert r.mimetype == "application/rss+xml", f"unexpected mimetype {r.mimetype}"
    rss = r.get_data(as_text=True)
    assert "Public Note" in rss
    assert "Unlisted Note" not in rss
    assert "Private Note" not in rss


def test_activity_rss_is_well_formed_with_correct_links(client, key):
    """Both the global and per-wiki RSS feeds parse as XML and carry absolute
    links matching the request host (tested for two host forms)."""
    from xml.etree import ElementTree

    _seed_activity_fixtures(client, key)
    # flask_login caches current_user on `g`, which persists across requests
    # inside this harness's wrapping app_context(). Clear it AND use a fresh
    # cookieless client so "anonymous" surfaces are checked as a true anon
    # request (else the owner would see their own private pages). Same idiom as
    # test_me_capabilities.
    from flask import g as _flask_g
    _flask_g.pop("_login_user", None)
    anon = client.application.test_client()

    # two host forms to prove absolute links track the request host. Avoid the
    # production apex (wikihub.md), which triggers canonical-redirect middleware
    # on /@owner/slug paths — irrelevant to feed-link correctness.
    for host in ("localhost", "example.test"):
        # global feed
        r = anon.get("/activity.rss", headers={"Host": host})
        assert r.status_code == 200
        root = ElementTree.fromstring(r.get_data())  # raises on malformed XML
        assert root.tag == "rss"
        channel = root.find("channel")
        assert channel is not None
        items = channel.findall("item")
        assert items, "global RSS should have at least one item"
        # global feed carries only public pages; the private note never leaks
        all_links = " ".join(it.find("link").text for it in items)
        assert "/@agent1/pubwiki/" in all_links, f"public page missing from global RSS: {all_links[:200]}"
        assert "secretwiki" not in all_links, "unlisted/private wiki must not leak into global RSS"
        self_link = channel.find("{http://www.w3.org/2005/Atom}link")
        assert self_link is not None and host in self_link.get("href")

        # per-wiki feed for the unlisted wiki (anonymous request)
        r = anon.get("/@agent1/secretwiki/activity.rss", headers={"Host": host})
        assert r.status_code == 200
        assert r.mimetype == "application/rss+xml"
        root = ElementTree.fromstring(r.get_data())
        titles = [it.find("title").text for it in root.find("channel").findall("item")]
        joined = " ".join(titles)
        # unlisted pages are reachable-by-link, so they belong in the wiki feed…
        assert "Unlisted Note" in joined, "unlisted page should appear in per-wiki RSS"
        # …but private pages must never appear for an anonymous request
        assert "Private Note" not in joined, f"private page must NOT appear in anon per-wiki RSS; got titles={titles!r}"
        for it in root.find("channel").findall("item"):
            assert it.find("link").text.startswith(f"http://{host}/@agent1/secretwiki/")


def test_wiki_activity_rss_private_only_wiki_does_not_leak_metadata(client, key):
    h = {"Authorization": f"Bearer {key}"}
    client.post("/api/v1/wikis", json={"slug": "private-rss", "title": "Private RSS"}, headers=h)
    client.post("/api/v1/wikis/agent1/private-rss/pages", json={
        "path": "wiki/private-only.md",
        "content": "---\ntitle: Private Only\nvisibility: private\n---\n\n# Private Only\n",
        "visibility": "private",
    }, headers=h)

    from flask import g as _flask_g
    _flask_g.pop("_login_user", None)
    anon = client.application.test_client()

    r = anon.get("/@agent1/private-rss/activity.rss")
    assert r.status_code == 401
    body = r.get_data(as_text=True)
    assert "Private RSS" not in body
    assert "Private Only" not in body
    assert r.mimetype != "application/rss+xml"


def test_wiki_activity_rss_keeps_older_unlisted_after_private_pages(client, key):
    """Anonymous per-wiki RSS keeps reachable unlisted pages even when newer
    private pages would otherwise fill the candidate window."""
    from datetime import timedelta
    from flask import g as _flask_g
    from xml.etree import ElementTree

    h = {"Authorization": f"Bearer {key}"}
    client.post("/api/v1/wikis", json={"slug": "rss-sparse", "title": "RSS Sparse"}, headers=h)
    client.post("/api/v1/wikis/agent1/rss-sparse/pages", json={
        "path": "wiki/older-unlisted.md",
        "content": "---\ntitle: Older Unlisted\nvisibility: unlisted\n---\n\n# Older Unlisted\n",
        "visibility": "unlisted",
    }, headers=h)

    owner = User.query.filter_by(username="agent1").first()
    wiki = Wiki.query.filter_by(owner_id=owner.id, slug="rss-sparse").first()
    base = utcnow()
    visible_page = Page.query.filter_by(wiki_id=wiki.id, path="wiki/older-unlisted.md").first()
    visible_page.updated_at = base
    visible_page.created_at = base
    for i in range(305):
        db.session.add(Page(
            wiki_id=wiki.id,
            path=f"secrets/private-{i:03d}.md",
            title=f"Private {i:03d}",
            visibility="private",
            created_at=base + timedelta(seconds=i + 1),
            updated_at=base + timedelta(seconds=i + 1),
        ))
    db.session.commit()

    _flask_g.pop("_login_user", None)
    anon = client.application.test_client()
    r = anon.get("/@agent1/rss-sparse/activity.rss")
    assert r.status_code == 200
    titles = [
        it.find("title").text
        for it in ElementTree.fromstring(r.get_data()).find("channel").findall("item")
    ]
    joined = " ".join(titles)
    assert "Older Unlisted" in joined, f"older readable page missing from RSS: {titles!r}"
    assert "Private 304" not in joined, "anonymous RSS must not expose private pages"


def test_global_activity_rows_do_not_nest_anchors(client, key):
    """Global activity rows render page and wiki links as sibling anchors."""
    from html.parser import HTMLParser

    _seed_activity_fixtures(client, key)
    r = client.get("/activity")
    assert r.status_code == 200

    class AnchorNestingParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.anchor_depth = 0
            self.nested = False
            self.row_count = 0
            self.page_links = 0
            self.wiki_links = 0

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            classes = attrs.get("class", "").split()
            href = attrs.get("href", "")
            if "gactivity-row" in classes:
                self.row_count += 1
                assert tag != "a", "global activity row must not be an anchor"
            if tag == "a":
                if self.anchor_depth:
                    self.nested = True
                self.anchor_depth += 1
                if "gactivity-title" in classes:
                    self.page_links += 1
                if href.startswith("/@agent1/pubwiki"):
                    self.wiki_links += 1

        def handle_endtag(self, tag):
            if tag == "a":
                self.anchor_depth -= 1

    parser = AnchorNestingParser()
    parser.feed(r.get_data(as_text=True))
    assert parser.row_count > 0, "expected global activity rows"
    assert parser.page_links > 0, "page title should remain a primary link"
    assert parser.wiki_links > 0, "wiki link should remain present"
    assert not parser.nested, "global activity must not contain nested anchors"


def test_activity_pagination(client, key):
    """Global activity paginates; page 2 shows different entries than page 1."""
    h = {"Authorization": f"Bearer {key}"}
    client.post("/api/v1/wikis", json={"slug": "pagewiki", "title": "Page Wiki"}, headers=h)
    # create > one page of activity (per_page=40)
    for i in range(45):
        client.post("/api/v1/wikis/agent1/pagewiki/pages", json={
            "path": f"wiki/note-{i:03d}.md",
            "content": f"---\ntitle: Note {i:03d}\nvisibility: public\n---\n\n# Note {i:03d}\n",
            "visibility": "public",
        }, headers=h)

    r1 = client.get("/activity?page=1")
    assert r1.status_code == 200
    b1 = r1.get_data(as_text=True)
    assert "Page 1 of" in b1
    assert "Older" in b1  # has_next → link present

    r2 = client.get("/activity?page=2")
    assert r2.status_code == 200
    b2 = r2.get_data(as_text=True)
    assert "Page 2 of" in b2
    # the two pages should not be identical (different slice of entries)
    assert b1 != b2, "page 2 should differ from page 1"


if __name__ == "__main__":
    sys.exit(run_all())
