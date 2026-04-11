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

from app import create_app, db


def setup():
    app = create_app()
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

    # delete page
    r = client.delete("/api/v1/wikis/agent1/test-wiki/pages/wiki/hello.md", headers=h)
    assert r.status_code == 204


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

    r = client.post("/@folderuser/folderuser/new-folder", data={
        "folder_path": "plans/2026",
        "visibility": "public",
    }, follow_redirects=False)
    assert r.status_code == 302
    assert "/@folderuser/folderuser/plans/2026/index/edit" in r.headers["Location"]

    r = client.get("/@folderuser/folderuser/plans/2026/")
    assert r.status_code == 200
    assert b"plans/2026" in r.data or b"2026" in r.data


def test_wikipedia_urls(client, api_key):
    """Wikipedia-style URLs: underscores instead of %20, redirect %20 to underscore"""
    h = {"Authorization": f"Bearer {api_key}"}

    # create a page with spaces in the name
    r = client.post("/api/v1/wikis/agent1/test-wiki/pages", json={
        "path": "wiki/My Great Page.md",
        "content": "---\ntitle: My Great Page\nvisibility: public\n---\n\n# Hello",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201

    # access via underscore URL (Wikipedia-style)
    r = client.get("/@agent1/test-wiki/wiki/My_Great_Page")
    assert r.status_code == 200
    assert b"Hello" in r.data

    # access via %20 URL should 301 redirect to underscore URL
    r = client.get("/@agent1/test-wiki/wiki/My%20Great%20Page", follow_redirects=False)
    assert r.status_code == 301
    assert "My_Great_Page" in r.headers["Location"]

    # history via underscore URL (public, no auth needed)
    r = client.get("/@agent1/test-wiki/wiki/My_Great_Page/history")
    assert r.status_code == 200

    # create a page with literal underscores in the filename
    r = client.post("/api/v1/wikis/agent1/test-wiki/pages", json={
        "path": "wiki/kbhconvex_optimization.md",
        "content": "---\ntitle: Convex Optimization\nvisibility: public\n---\n\n# Convex Optimization",
        "visibility": "public",
    }, headers=h)
    assert r.status_code == 201

    # access via literal underscore URL — should find the underscore file, not space fallback
    r = client.get("/@agent1/test-wiki/wiki/kbhconvex_optimization")
    assert r.status_code == 200
    assert b"Convex Optimization" in r.data


def test_sharing_lifecycle(client, api_key):
    """share a private page with another user, verify access, revoke, verify no access"""
    h = {"Authorization": f"Bearer {api_key}"}

    # create a guest user
    r = client.post("/api/v1/accounts", json={"username": "guest1"})
    guest_key = r.get_json()["api_key"]
    hg = {"Authorization": f"Bearer {guest_key}"}

    # create a private page
    r = client.post("/api/v1/wikis/agent1/test-wiki/pages", json={
        "path": "sharing-test.md",
        "content": "# Secret\n\nSharing test content.",
        "visibility": "private",
    }, headers=h)
    assert r.status_code == 201

    # guest cannot read it
    r = client.get("/api/v1/wikis/agent1/test-wiki/pages/sharing-test.md", headers=hg)
    assert r.status_code in (403, 404)

    # owner shares with guest (page-level)
    r = client.post("/api/v1/wikis/agent1/test-wiki/share", json={
        "pattern": "sharing-test.md",
        "username": "guest1",
        "role": "read",
    }, headers=h)
    assert r.status_code == 200

    # guest can now read it
    r = client.get("/api/v1/wikis/agent1/test-wiki/pages/sharing-test.md", headers=hg)
    assert r.status_code == 200
    assert "Sharing test content" in r.get_json()["content"]

    # list grants
    r = client.get("/api/v1/wikis/agent1/test-wiki/grants", headers=h)
    assert r.status_code == 200
    grants = r.get_json()["grants"]
    assert any(g["username"] == "guest1" and g["role"] == "read" for g in grants)

    # page-level grants
    r = client.get("/api/v1/wikis/agent1/test-wiki/pages/sharing-test.md/grants", headers=h)
    assert r.status_code == 200
    assert any(g["username"] == "guest1" for g in r.get_json()["grants"])

    # shared-with-me from guest's perspective
    r = client.get("/api/v1/shared-with-me", headers=hg)
    assert r.status_code == 200
    shared = r.get_json()["shared"]
    assert any(s["wiki"] == "agent1/test-wiki" for s in shared)

    # owner revokes
    r = client.delete("/api/v1/wikis/agent1/test-wiki/share", json={
        "pattern": "sharing-test.md",
        "username": "guest1",
    }, headers=h)
    assert r.status_code == 200
    assert r.get_json()["revoked"] is True

    # guest can no longer read it
    r = client.get("/api/v1/wikis/agent1/test-wiki/pages/sharing-test.md", headers=hg)
    assert r.status_code in (403, 404)


def test_wiki_level_sharing(client, api_key):
    """wiki-level grant (*) gives access to all pages"""
    h = {"Authorization": f"Bearer {api_key}"}

    r = client.post("/api/v1/accounts", json={"username": "wikiguest"})
    guest_key = r.get_json()["api_key"]
    hg = {"Authorization": f"Bearer {guest_key}"}

    # create two private pages
    for name in ("wiki-share-a.md", "wiki-share-b.md"):
        r = client.post("/api/v1/wikis/agent1/test-wiki/pages", json={
            "path": name,
            "content": f"# {name}\n\nPrivate content.",
            "visibility": "private",
        }, headers=h)
        assert r.status_code == 201

    # guest can't read either
    for name in ("wiki-share-a.md", "wiki-share-b.md"):
        r = client.get(f"/api/v1/wikis/agent1/test-wiki/pages/{name}", headers=hg)
        assert r.status_code in (403, 404)

    # share entire wiki with guest
    r = client.post("/api/v1/wikis/agent1/test-wiki/share", json={
        "pattern": "*",
        "username": "wikiguest",
        "role": "read",
    }, headers=h)
    assert r.status_code == 200

    # guest can now read both
    for name in ("wiki-share-a.md", "wiki-share-b.md"):
        r = client.get(f"/api/v1/wikis/agent1/test-wiki/pages/{name}", headers=hg)
        assert r.status_code == 200

    # revoke wiki-level grant
    r = client.delete("/api/v1/wikis/agent1/test-wiki/share", json={
        "pattern": "*",
        "username": "wikiguest",
    }, headers=h)
    assert r.status_code == 200

    # guest can't read again
    for name in ("wiki-share-a.md", "wiki-share-b.md"):
        r = client.get(f"/api/v1/wikis/agent1/test-wiki/pages/{name}", headers=hg)
        assert r.status_code in (403, 404)


def test_folder_level_sharing(client, api_key):
    """folder-level grant (folder/*) gives access to folder pages only"""
    h = {"Authorization": f"Bearer {api_key}"}

    r = client.post("/api/v1/accounts", json={"username": "folderguest"})
    guest_key = r.get_json()["api_key"]
    hg = {"Authorization": f"Bearer {guest_key}"}

    # create pages in and outside folder
    client.post("/api/v1/wikis/agent1/test-wiki/pages", json={
        "path": "research/paper.md",
        "content": "# Paper\n\nResearch content.",
        "visibility": "private",
    }, headers=h)
    client.post("/api/v1/wikis/agent1/test-wiki/pages", json={
        "path": "notes.md",
        "content": "# Notes\n\nPersonal notes.",
        "visibility": "private",
    }, headers=h)

    # share research folder only
    r = client.post("/api/v1/wikis/agent1/test-wiki/share", json={
        "pattern": "research/*",
        "username": "folderguest",
        "role": "read",
    }, headers=h)
    assert r.status_code == 200

    # guest can read research/paper.md
    r = client.get("/api/v1/wikis/agent1/test-wiki/pages/research/paper.md", headers=hg)
    assert r.status_code == 200

    # guest cannot read notes.md
    r = client.get("/api/v1/wikis/agent1/test-wiki/pages/notes.md", headers=hg)
    assert r.status_code in (403, 404)


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
            ("agent surfaces", lambda: test_agent_surfaces(client)),
            ("token + settings", lambda: test_token_and_settings(client)),
            ("magic link login", lambda: test_magic_link_login(client)),
            ("ACL permissions", lambda: test_acl_permissions(client, key)),
            ("anonymous public edit", lambda: test_anonymous_public_edit(client, key)),
            ("people directory + profiles", lambda: test_people_directory_and_profiles(client, key)),
            ("new folder UI", lambda: test_new_folder_ui(client)),
            ("wikipedia-style URLs", lambda: test_wikipedia_urls(client, key)),
            ("sharing lifecycle", lambda: test_sharing_lifecycle(client, key)),
            ("wiki-level sharing", lambda: test_wiki_level_sharing(client, key)),
            ("folder-level sharing", lambda: test_folder_level_sharing(client, key)),
        ]

        passed = 1  # account creation already passed
        failed = 0
        for name, fn in test_funcs:
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
