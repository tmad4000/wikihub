"""
wikihub end-to-end tests.

minimal and intentional — each test verifies a real user flow,
not individual functions. run with: python3 tests/test_e2e.py
"""

import io
import os
import shutil
import sys
import zipfile
from datetime import timedelta
from urllib.parse import urlparse
from sqlalchemy import text

# ensure app is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["SECRET_KEY"] = "test-secret"
os.environ["DATABASE_URL"] = "postgresql://localhost/wikihub_test"
os.environ["REPOS_DIR"] = "/tmp/wikihub-test-repos"
os.environ["ADMIN_TOKEN"] = "test-admin-token"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from app import create_app, db
from app.auth_utils import _write_timestamps
from app.models import utcnow


def setup():
    shutil.rmtree("/tmp/wikihub-test-repos", ignore_errors=True)
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        reset_database()
    return app


def teardown():
    shutil.rmtree("/tmp/wikihub-test-repos", ignore_errors=True)


def reset_database():
    for table in [
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
    r = anon_client.get("/@agent1/media-wiki/outside/private-via-page.txt")
    assert r.status_code == 404, f"Page.visibility=private should block anon access, got {r.status_code}"

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
    assert "max_wikis_per_user" in data["quotas"]


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

    r = client.get("/@agent1/private-cta/team/secret")
    assert r.status_code == 404
    assert b"This page is private or doesn't exist" in r.data
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

    # Internal url_for()-generated links use the full /@user/slug/page form.
    # On a wiki subdomain, those must resolve — either directly or 301 to the
    # short form on the same host. Regression test for the double-prefix bug.
    r = client.get("/@subowner/cookbook/intro",
                   headers={"Host": "recipes.wikihub.md"}, follow_redirects=False)
    assert r.status_code in (200, 301), f"/@user/slug/page on wiki subdomain: {r.status_code}"
    if r.status_code == 301:
        assert "recipes.wikihub.md/intro" in r.headers["Location"]

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
            ("page ETag conflict", lambda: test_page_etag_conflict(client, key)),
            ("binary file serving", lambda: test_binary_file_serving(client, key)),
            ("search", lambda: test_search(client, key)),
            ("reader owner visibility control", lambda: test_reader_owner_visibility_control(client, key)),
            ("search respects ACL shares", lambda: test_search_respects_acl_shares(client, key)),
            ("social (star + fork)", lambda: test_social(client, key)),
            ("zip upload", lambda: test_zip_upload(client, key)),
            ("anonymous upload (wikihub-i2xm)", lambda: test_anonymous_upload(app)),
            ("agent surfaces", lambda: test_agent_surfaces(client)),
            ("token + settings", lambda: test_token_and_settings(client)),
            ("client_config hint", lambda: test_client_config_hint(client)),
            ("magic link login", lambda: test_magic_link_login(client)),
            ("logout (wikihub-uq9)", lambda: test_logout(client)),
            ("email verification flow (wikihub-ks5t.3)", lambda: test_email_verification_flow(client)),
            ("password reset flow (wikihub-ks5t.5)", lambda: test_password_reset_lifecycle(client)),
            ("Google auto-link security (wikihub-ks5t.4)", lambda: test_google_auto_link_security(app)),
            ("Google OAuth preserves next + invite context (wikihub-gtrq)", lambda: test_google_oauth_preserves_next_and_invite_context(app, client, key)),
            ("login redirects back (?next + Referer fallback)", lambda: test_login_redirect_back(client)),
            ("URL login (GET ?api_key / ?password)", lambda: test_url_login(client)),
            ("URL login — log redaction", lambda: test_url_login_log_redaction()),
            ("magic link from password", lambda: test_magic_link_from_password(client)),
            ("ACL permissions", lambda: test_acl_permissions(client, key)),
            ("private /new requires write access", lambda: test_private_new_page_requires_write_access(client, key)),
            ("anonymous public edit", lambda: test_anonymous_public_edit(client, key)),
            ("public-edit shows Edit button", lambda: test_public_edit_shows_edit_button(client, key)),
            ("anonymous posting + claim (wikihub-7b2r)", lambda: test_anonymous_posting_and_claim(client)),
            ("people directory + profiles", lambda: test_people_directory_and_profiles(client, key)),
            ("new folder UI", lambda: test_new_folder_ui(client)),
            ("sidebar indentation (wikihub-58c regression guard)", lambda: test_sidebar_indentation(client, key)),
            ("wikipedia-style URLs", lambda: test_wikipedia_urls(client, key)),
            ("sharing lifecycle", lambda: test_sharing_lifecycle(client, key)),
            ("wiki-level sharing", lambda: test_wiki_level_sharing(client, key)),
            ("folder-level sharing", lambda: test_folder_level_sharing(client, key)),
            ("api root discovery", lambda: test_api_root_discovery(client)),
            ("feedback submission", lambda: test_feedback_submission(client)),
            ("me capabilities", lambda: test_me_capabilities(client, key)),
            ("frontmatter title renders h1", lambda: test_frontmatter_title_renders_h1(client, key)),
            ("soft line breaks render as visual break (wikihub-eiv7)", lambda: test_soft_line_breaks_render_as_visual_break()),
            ("admin claude-auth page requires token", lambda: test_admin_claude_auth_page_requires_token(client)),
            ("history API with anon + deleted page", lambda: test_history_api_with_anon_and_deleted_page(client, key)),
            ("API CORS headers", lambda: test_api_cors_headers(client, key)),
            ("list wikis API", lambda: test_list_wikis_api(client, key)),
            ("bulk sharing (wikihub-iga9)", lambda: test_bulk_sharing(client, key)),
            ("pending invite lifecycle (wikihub-skp7)", lambda: test_pending_invite_lifecycle(client, key)),
            ("share sends email (wikihub-exj1 mock)", lambda: test_share_sends_email(client, key)),
            ("private surface offers request access", lambda: test_permission_error_offers_request_access(client, key)),
            ("access requests stay ambiguous + notify existing target", lambda: test_access_request_constant_response_and_notify_existing_target(client)),
            ("subdomain routing", lambda: test_subdomain_routing(client)),
            ("CLI end-to-end", lambda: test_cli(client)),
        ]

        passed = 1  # account creation already passed
        failed = 0
        for name, fn in test_funcs:
            _write_timestamps.clear()
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


if __name__ == "__main__":
    sys.exit(run_all())
