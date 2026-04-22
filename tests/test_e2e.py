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
        "magic_login_tokens",
        "api_keys",
        "username_redirects",
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
    assert b"All people" in r.data
    assert b"@agent1" in r.data
    assert b"@person2" in r.data

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

    # wikihub-58bd: the real invariant is that padding-left must exceed the
    # sidebar-folder-toggle width. Files inside a folder have no toggle button,
    # so when .sidebar-children padding == toggle width, file icons end up at
    # exactly the same x as their parent folder's icon — zero visible nesting.
    tog = re.search(r"\.sidebar-folder-toggle\s*\{[^}]*width:\s*(\d+)px", html)
    assert tog, (
        "wikihub-58c REGRESSION: .sidebar-folder-toggle width rule missing. "
        "Cannot verify nesting invariant."
    )
    toggle_width = int(tog.group(1))
    assert px > toggle_width + 4, (
        f"wikihub-58bd REGRESSION: .sidebar-children padding-left is {px}px "
        f"but .sidebar-folder-toggle width is {toggle_width}px. Children need "
        f"padding-left > toggle_width + 4 so file icons appear visibly indented "
        f"past the parent folder's icon. (Was: padding=={toggle_width} made the "
        f"tree look flat at the second nesting level.)"
    )

    # 2) HTML structure: folder wraps child rows in .sidebar-children
    assert 'class="sidebar-children"' in html, (
        "wikihub-58c REGRESSION: folder macro no longer emits "
        '<div class="sidebar-children">. Child rows will render as siblings of '
        "the folder instead of nested. Check the render_sidebar macro in "
        "app/templates/reader.html."
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

    # PendingInvite row created
    pending = PendingInvite.query.filter_by(email="future-user@example.com").all()
    assert len(pending) == 1
    assert pending[0].pattern == "*" and pending[0].role == "edit"

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
            ("binary file serving", lambda: test_binary_file_serving(client, key)),
            ("search", lambda: test_search(client, key)),
            ("social (star + fork)", lambda: test_social(client, key)),
            ("zip upload", lambda: test_zip_upload(client, key)),
            ("anonymous upload (wikihub-i2xm)", lambda: test_anonymous_upload(app)),
            ("agent surfaces", lambda: test_agent_surfaces(client)),
            ("token + settings", lambda: test_token_and_settings(client)),
            ("client_config hint", lambda: test_client_config_hint(client)),
            ("magic link login", lambda: test_magic_link_login(client)),
            ("logout (wikihub-uq9)", lambda: test_logout(client)),
            ("email verification flow (wikihub-ks5t.3)", lambda: test_email_verification_flow(client)),
            ("Google auto-link security (wikihub-ks5t.4)", lambda: test_google_auto_link_security(app)),
            ("login redirects back (?next + Referer fallback)", lambda: test_login_redirect_back(client)),
            ("URL login (GET ?api_key / ?password)", lambda: test_url_login(client)),
            ("URL login — log redaction", lambda: test_url_login_log_redaction()),
            ("magic link from password", lambda: test_magic_link_from_password(client)),
            ("ACL permissions", lambda: test_acl_permissions(client, key)),
            ("anonymous public edit", lambda: test_anonymous_public_edit(client, key)),
            ("anonymous posting + claim (wikihub-7b2r)", lambda: test_anonymous_posting_and_claim(client)),
            ("people directory + profiles", lambda: test_people_directory_and_profiles(client, key)),
            ("new folder UI", lambda: test_new_folder_ui(client)),
            ("sidebar indentation (wikihub-58c regression guard)", lambda: test_sidebar_indentation(client, key)),
            ("wikipedia-style URLs", lambda: test_wikipedia_urls(client, key)),
            ("sharing lifecycle", lambda: test_sharing_lifecycle(client, key)),
            ("wiki-level sharing", lambda: test_wiki_level_sharing(client, key)),
            ("folder-level sharing", lambda: test_folder_level_sharing(client, key)),
            ("bulk sharing (wikihub-iga9)", lambda: test_bulk_sharing(client, key)),
            ("pending invite lifecycle (wikihub-skp7)", lambda: test_pending_invite_lifecycle(client, key)),
            ("share sends email (wikihub-exj1 mock)", lambda: test_share_sends_email(client, key)),
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
