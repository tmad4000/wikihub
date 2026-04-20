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
    return app


def teardown():
    shutil.rmtree("/tmp/wikihub-test-repos", ignore_errors=True)


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
    for url in ["/llms.txt", "/AGENTS.md", "/agents", "/.well-known/mcp/server-card.json", "/.well-known/wikihub.json"]:
        r = client.get(url)
        assert r.status_code == 200, f"{url} returned {r.status_code}"


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
            ("search", lambda: test_search(client, key)),
            ("social (star + fork)", lambda: test_social(client, key)),
            ("zip upload", lambda: test_zip_upload(client, key)),
            ("agent surfaces", lambda: test_agent_surfaces(client)),
            ("ACL permissions", lambda: test_acl_permissions(client, key)),
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
        from app.models import Star, Fork, Wikilink, Page, Wiki, ApiKey, User
        for m in [Star, Fork, Wikilink, Page, Wiki, ApiKey, User]:
            m.query.delete()
        db.session.commit()

    teardown()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run_all())
