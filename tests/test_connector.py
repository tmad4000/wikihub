"""Real HTTP/DB/git connector flows. Run only against the dedicated test database."""
import base64
import hashlib
import hmac
import importlib.util
import json
import os
import secrets
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/wikihub_ecosystem_adapter_test")
os.environ.setdefault("REPOS_DIR", "/tmp/wikihub-ecosystem-connector-test-repos")
if not os.environ["DATABASE_URL"].endswith("/wikihub_ecosystem_adapter_test"):
    raise RuntimeError("Connector tests require the dedicated wikihub_ecosystem_adapter_test database")
SECRET = "connector-test-secret-" * 3
os.environ["IDEAFLOW_CONNECTOR_SECRET"] = SECRET
spec = importlib.util.spec_from_file_location("e2e", ROOT / "tests/test_e2e.py")
e2e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e2e)
from app import create_app, db
from app.models import ExternalIdentity, ApiKey, Page, User
from app.connector import ISSUER, IDENTITY_ISSUER, AUDIENCE


def signed(message, subject="alice-id", scope="wikihub:read", claims=None, secret=SECRET, header=None):
    raw = json.dumps(message, separators=(",", ":")).encode()
    now = int(time.time())
    claim = dict(iss=ISSUER, aud=AUDIENCE, identity_issuer=IDENTITY_ISSUER, sub=subject, iat=now, exp=now+45,
                 jti=secrets.token_urlsafe(24), scope=scope, body_sha256=hashlib.sha256(raw).hexdigest())
    claim.update(claims or {})
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    parts = [b64(json.dumps(header or {"alg": "HS256", "typ": "JWT"}).encode()), b64(json.dumps(claim).encode())]
    token = ".".join(parts)+"."+b64(hmac.new(secret.encode(), ".".join(parts).encode(), hashlib.sha256).digest())
    return raw, {"Authorization": "Bearer "+token, "Content-Type": "application/json"}


def call(client, name, args=None, **options):
    raw, headers = signed({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args or {}}}, **options)
    return client.post("/api/connector/mcp", data=raw, headers=headers)


def data(response):
    assert response.status_code == 200, response.get_json()
    value = response.get_json()["result"]
    assert not value.get("isError"), value
    return value.get("structuredContent")


def run():
    app = e2e.setup()
    client = app.test_client()
    users = {}
    for name in ("alice", "bob"):
        r = client.post("/api/v1/accounts", json={"username": name, "email": name+"@example.test"})
        assert r.status_code == 201, r.get_json()
        users[name] = r.get_json()
    with app.app_context():
        for name, user in users.items():
            db.session.add(ExternalIdentity(user_id=user["user_id"], issuer=IDENTITY_ISSUER, subject=name+"-id"))
        db.session.commit()
        key_count = ApiKey.query.count()
    owner_headers = {"Authorization": "Bearer "+users["alice"]["api_key"]}
    assert client.post("/api/v1/wikis", json={"slug": "private-test"}, headers=owner_headers).status_code == 201
    for path, visibility, content in [("secret.md", "private", "private secret"), ("public.md", "public", "visible\n<!-- private -->\nowner band secret\n<!-- /private -->\nvisible tail")]:
        assert client.post("/api/v1/wikis/alice/private-test/pages", json={"path": path, "visibility": visibility, "content": content}, headers=owner_headers).status_code == 201
    args = {"owner": "alice", "slug": "private-test", "path": "secret.md"}
    assert data(call(client, "wikihub_whoami"))["username"] == "alice"
    assert "private secret" in data(call(client, "wikihub_get_page", args))["content"]
    denied = call(client, "wikihub_get_page", args, subject="bob-id")
    assert denied.status_code == 200 and denied.get_json()["result"]["isError"]
    public = {**args, "path": "public.md"}
    assert "owner band secret" in data(call(client, "wikihub_get_page", public))["content"]
    bob_page = data(call(client, "wikihub_get_page", public, subject="bob-id"))
    assert "owner band secret" not in json.dumps(bob_page) and "<!-- private" not in json.dumps(bob_page)
    assert "visible" in bob_page["content"]
    assert "owner band secret" not in data(call(client, "fetch", {"id": "alice/private-test:public.md"}, subject="bob-id"))["text"]
    fetched = data(call(client, "fetch", {"id": "alice/private-test:secret.md"}))
    assert fetched["id"] == "alice/private-test:secret.md" and "private secret" in fetched["text"]
    hits = data(call(client, "search", {"query": "secret"}))["results"]
    assert any(hit["id"] == "alice/private-test:secret.md" for hit in hits), hits
    assert not any(hit["id"] == "alice/private-test:secret.md" for hit in data(call(client, "search", {"query": "secret"}, subject="bob-id"))["results"])
    # Caller credential headers/cookies cannot substitute for the signed subject.
    raw, headers = signed({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "wikihub_whoami"}}, subject="bob-id")
    headers["X-Api-Key"] = users["alice"]["api_key"]
    assert data(client.post("/api/connector/mcp", data=raw, headers=headers))["username"] == "bob"
    assert call(client, "wikihub_whoami", subject="alice@example.test").status_code == 409
    assert call(client, "wikihub_whoami", subject=str(users["alice"]["user_id"])).status_code == 409
    # Read/write scope gating is enforced on calls as well as tool discovery.
    raw, headers = signed({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    listed = client.post("/api/connector/mcp", data=raw, headers=headers).get_json()["result"]["tools"]
    assert listed and all(t["annotations"]["readOnlyHint"] for t in listed)
    assert not any(t["name"] == "wikihub_register_agent" for t in listed)
    write = {**args, "path": "created.md", "content": "once"}
    assert call(client, "wikihub_create_page", write).status_code == 403
    assert call(client, "wikihub_register_agent", {}, scope="wikihub:read wikihub:write").status_code == 400
    assert data(call(client, "wikihub_create_page", write, scope="wikihub:read wikihub:write"))["path"] == "created.md"
    assert call(client, "wikihub_update_page", {**args, "content": "intrusion"}, subject="bob-id", scope="wikihub:read wikihub:write").get_json()["result"]["isError"]
    assert "private secret" in data(call(client, "wikihub_get_page", args))["content"]
    assert "intrusion" not in data(call(client, "wikihub_get_page", args))["content"]
    # A fresh app instance shares the durable nonce ledger (not a process cache).
    replay_args = {**write, "path": "replay.md"}
    raw, headers = signed({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "wikihub_create_page", "arguments": replay_args}}, scope="wikihub:read wikihub:write")
    app2 = create_app()
    app2.config.update(TESTING=True, WTF_CSRF_ENABLED=False, TRUST_GATE_ENABLED=False)
    def post(target):
        with target.test_client() as c: return c.post("/api/connector/mcp", data=raw, headers=headers).status_code
    with ThreadPoolExecutor(max_workers=2) as pool: statuses = list(pool.map(post, [app, app2]))
    assert sorted(statuses) == [200, 401], statuses
    with app.app_context():
        assert Page.query.filter_by(path="replay.md").count() == 1
        assert ApiKey.query.count() == key_count
    # Signature, exact app/identity issuer, bound raw body, lifetime, scopes.
    for options in [{"secret": "wrong"}, {"claims": {"aud": "https://api.worldissuetracker.com/mcp"}}, {"claims": {"identity_issuer": "https://other.example"}}, {"claims": {"exp": int(time.time())-1}}, {"claims": {"exp": int(time.time())+90}}, {"claims": {"body_sha256": "0"*64}}, {"scope": "wikihub:write"}, {"scope": "wikihub:read noos:read"}, {"header": {"alg": "none", "typ": "JWT"}}]:
        assert call(client, "wikihub_whoami", **options).status_code in (401, 403), options
    raw, headers = signed({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert client.post("/api/connector/mcp", data=raw+b" ", headers=headers).status_code == 401
    assert call(client, "wikihub_get_page", {**args, "path": "../secret.md"}).status_code == 400
    # Context is cleared after the delegated call; regular API keys retain behavior.
    assert client.get("/api/v1/accounts/me").status_code == 401
    assert client.get("/api/v1/accounts/me", headers=owner_headers).get_json()["username"] == "alice"
    with app.app_context():
        u = db.session.get(User, users["bob"]["user_id"])
        u.suspended_at = e2e.utcnow()
        db.session.commit()
    assert call(client, "wikihub_whoami", subject="bob-id").status_code == 403
    app.config["IDEAFLOW_CONNECTOR_SECRET"] = ""
    assert call(client, "wikihub_whoami").status_code == 503
    print("PASS: linked identity, ACL/private bands, read/write scopes, durable concurrent replay, existing keys and malformed assertions")


if __name__ == "__main__":
    run()
