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


def setup():
    shutil.rmtree("/tmp/wikihub-test-repos", ignore_errors=True)
    app = create_app()
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
        "wikis",
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
    # auth required
    r = client.get("/api/v1/me/capabilities")
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
            ("public-edit shows Edit button", lambda: test_public_edit_shows_edit_button(client, key)),
            ("people directory + profiles", lambda: test_people_directory_and_profiles(client, key)),
            ("new folder UI", lambda: test_new_folder_ui(client)),
            ("wikipedia-style URLs", lambda: test_wikipedia_urls(client, key)),
            ("sharing lifecycle", lambda: test_sharing_lifecycle(client, key)),
            ("wiki-level sharing", lambda: test_wiki_level_sharing(client, key)),
            ("folder-level sharing", lambda: test_folder_level_sharing(client, key)),
            ("api root discovery", lambda: test_api_root_discovery(client)),
            ("feedback submission", lambda: test_feedback_submission(client)),
            ("me capabilities", lambda: test_me_capabilities(client, key)),
            ("frontmatter title renders h1", lambda: test_frontmatter_title_renders_h1(client, key)),
            ("admin claude-auth page requires token", lambda: test_admin_claude_auth_page_requires_token(client)),
            ("history API with anon + deleted page", lambda: test_history_api_with_anon_and_deleted_page(client, key)),
            ("list wikis API", lambda: test_list_wikis_api(client, key)),
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
