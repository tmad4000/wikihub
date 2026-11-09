"""Ideaflow Ecosystem delegated MCP. All data operations use existing REST ACLs."""
import base64
import hashlib
import hmac
import json
import re
import time
from urllib.parse import quote, urlencode

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import text
from werkzeug.test import EnvironBuilder
from app import db
from app.models import ExternalIdentity
from app.connector_context import connector_user

connector_bp = Blueprint("ecosystem_connector", __name__)
ISSUER = "https://id.ideaflow.app/connector"
IDENTITY_ISSUER = "https://id.ideaflow.app/api/auth"
AUDIENCE = "https://wikihub.md/mcp"
MAX_BODY = 4 * 1024 * 1024


class ConnectorError(Exception):
    def __init__(self, code, status=401):
        self.code, self.status = code, status


def _decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _verify(token, raw):
    secret = current_app.config.get("IDEAFLOW_CONNECTOR_SECRET", "")
    if not isinstance(secret, str) or len(secret) < 32:
        raise ConnectorError("app_unavailable", 503)
    if not isinstance(token, str) or len(token) > 16000:
        raise ConnectorError("invalid_assertion")
    parts = token.split(".")
    if len(parts) != 3 or not all(re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in parts):
        raise ConnectorError("invalid_assertion")
    try:
        expected = hmac.new(secret.encode(), (parts[0]+"."+parts[1]).encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _decode(parts[2])):
            raise ValueError("signature")
        header, c = json.loads(_decode(parts[0])), json.loads(_decode(parts[1]))
        now = int(time.time())
        if (not isinstance(header, dict) or not isinstance(c, dict) or header.get("alg") != "HS256" or
            header.get("typ") != "JWT" or "crit" in header or c.get("iss") != ISSUER or
            c.get("aud") != AUDIENCE or c.get("identity_issuer") != IDENTITY_ISSUER or
            not isinstance(c.get("sub"), str) or not c["sub"].strip() or len(c["sub"]) > 256 or
            type(c.get("iat")) is not int or type(c.get("exp")) is not int or
            c["iat"] > now+5 or c["exp"] <= now or not 0 < c["exp"]-c["iat"] <= 45 or
            not isinstance(c.get("jti"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", c["jti"]) or
            c.get("body_sha256") != hashlib.sha256(raw).hexdigest() or not isinstance(c.get("scope"), str)):
            raise ValueError("claims")
    except (ValueError, TypeError, KeyError):
        raise ConnectorError("invalid_assertion") from None
    scopes = set(c["scope"].split())
    if "wikihub:read" not in scopes or scopes - {"wikihub:read", "wikihub:write"}:
        raise ConnectorError("insufficient_scope", 403)
    return c, scopes


def _consume(c):
    # PostgreSQL uniqueness survives multiple Gunicorn workers and restarts.
    # Use an independent transaction, never commit a pending content mutation.
    nonce = hashlib.sha256((ISSUER+"\n"+c["jti"]).encode()).hexdigest()
    with db.engine.begin() as conn:
        conn.execute(text("delete from connector_assertion_nonces where expires_at < now()"))
        claimed = conn.execute(text("insert into connector_assertion_nonces (jti_hash, expires_at) values (:hash, to_timestamp(:expiry)) on conflict do nothing returning jti_hash"), {"hash": nonce, "expiry": c["exp"]}).first()
    if not claimed:
        raise ConnectorError("assertion_replayed")


def _tool(name, description, read=True, required=(), optional=(), destructive=False):
    fields = {key: {"type": "integer" if key in ("limit", "offset") else "string"} for key in (*required, *optional)}
    for key in ("limit", "offset"):
        if key in fields:
            fields[key].update(minimum=1 if key == "limit" else 0, maximum=100 if key == "limit" else 1000000)
    return {"name": name, "title": name.replace("wikihub_", "").replace("_", " "), "description": description,
            "inputSchema": {"type": "object", "properties": fields, "required": list(required), "additionalProperties": False},
            "annotations": {"readOnlyHint": read, "destructiveHint": destructive}}


TOOLS = [
    _tool("wikihub_whoami", "Identify your linked WikiHub account."),
    _tool("wikihub_search", "Search pages you can read; optional wiki scope owner/slug.", required=("query",), optional=("wiki", "limit", "offset")),
    _tool("wikihub_list_wikis", "List discoverable wikis through the existing directory permissions.", optional=("limit", "offset")),
    _tool("wikihub_get_wiki", "Read wiki metadata.", required=("owner", "slug")),
    _tool("wikihub_list_pages", "List readable pages in a wiki.", required=("owner", "slug")),
    _tool("wikihub_get_page", "Read page content under its ACL; owner-only private bands stay hidden from other users.", required=("owner", "slug", "path")),
    _tool("wikihub_create_wiki", "Create a wiki owned by your linked account.", False, ("slug",), ("title", "description", "template")),
    _tool("wikihub_create_page", "Create a page under existing wiki write permissions.", False, ("owner", "slug", "path", "content"), ("visibility",)),
    _tool("wikihub_update_page", "Patch content, title or visibility; non-owner edits preserve private bands.", False, ("owner", "slug", "path"), ("content", "title", "visibility")),
    _tool("wikihub_append_section", "Append content under existing edit permissions.", False, ("owner", "slug", "path", "content"), ("heading",)),
    _tool("wikihub_delete_page", "Delete a page under existing edit permissions.", False, ("owner", "slug", "path"), destructive=True),
    _tool("wikihub_set_visibility", "Change page visibility under existing permissions.", False, ("owner", "slug", "path", "visibility")),
    _tool("wikihub_share", "Grant read/edit access to a user or email; owner ACL rules apply.", False, ("owner", "slug"), ("path", "pattern", "user", "email", "level")),
    _tool("wikihub_list_grants", "List sharing grants (wiki owner only).", required=("owner", "slug")),
    _tool("wikihub_shared_with_me", "List wikis/pages explicitly shared with your linked account."),
    _tool("wikihub_fork_wiki", "Fork a permitted public wiki into your account.", False, ("owner", "slug")),
    _tool("wikihub_commit_log", "Read only the history paths visible to your account; private bands remain protected.", required=("owner", "slug"), optional=("limit",)),
    _tool("search", "Search readable WikiHub pages; compatible search alias.", required=("query",)),
    _tool("fetch", "Read a search result by owner/slug:path id.", required=("id",)),
]
BY_NAME = {tool["name"]: tool for tool in TOOLS}


def _arguments(tool, args):
    schema = tool["inputSchema"]
    if not isinstance(args, dict) or set(args)-schema["properties"].keys() or any(k not in args for k in schema["required"]):
        raise ConnectorError("invalid_arguments", 400)
    for key, value in args.items():
        spec = schema["properties"][key]
        if spec["type"] == "integer":
            if type(value) is not int or not spec["minimum"] <= value <= spec["maximum"]:
                raise ConnectorError("invalid_arguments", 400)
        elif not isinstance(value, str):
            raise ConnectorError("invalid_arguments", 400)
    return dict(args)


def _route(name, args):
    if name == "fetch":
        match = re.fullmatch(r"([^/]+)/([^:]+):(.+)", args["id"])
        if not match:
            raise ConnectorError("invalid_arguments", 400)
        args = dict(zip(("owner", "slug", "path"), match.groups()))
        name = "wikihub_get_page"
    if name in ("search", "wikihub_search"):
        query = {"q": args["query"], **{k: args[k] for k in ("limit", "offset") if k in args}}
        if args.get("wiki"):
            query.update(scope="wiki", wiki=args["wiki"])
        return "GET", "/api/v1/search?"+urlencode(query), None
    if name == "wikihub_whoami": return "GET", "/api/v1/accounts/me", None
    if name == "wikihub_shared_with_me": return "GET", "/api/v1/shared-with-me", None
    if name == "wikihub_list_wikis": return "GET", "/api/v1/wikis?"+urlencode(args), None
    if name == "wikihub_create_wiki": return "POST", "/api/v1/wikis", args
    for key in ("owner", "slug"):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", args[key]): raise ConnectorError("invalid_arguments", 400)
    base = "/api/v1/wikis/"+quote(args["owner"], safe="")+"/"+quote(args["slug"], safe="")
    if "path" in args:
        if not args["path"] or any(p in ("", ".", "..") for p in args["path"].split("/")) or "\\" in args["path"]:
            raise ConnectorError("invalid_arguments", 400)
        page = base+"/pages/"+quote(args["path"], safe="/")
    body = {k:v for k,v in args.items() if k not in ("owner", "slug", "path")}
    mapping = {
        "wikihub_get_wiki": ("GET", base, None),
        "wikihub_list_pages": ("GET", base+"/pages", None),
        "wikihub_list_grants": ("GET", base+"/grants", None),
        "wikihub_fork_wiki": ("POST", base+"/fork", {}),
        "wikihub_commit_log": ("GET", base+"/history?"+urlencode(body), None),
    }
    if name == "wikihub_share": return "POST", (page if args.get("path") else base)+"/share", body
    if name == "wikihub_create_page": return "POST", base+"/pages", {**body, "path": args["path"]}
    if name in mapping: return mapping[name]
    return {
        "wikihub_get_page": ("GET", page, None),
        "wikihub_update_page": ("PATCH", page, body),
        "wikihub_delete_page": ("DELETE", page, None),
        "wikihub_append_section": ("POST", page+"/append-section", body),
        "wikihub_set_visibility": ("POST", page+"/visibility", body),
    }[name]


def _dispatch(user, method, path, body):
    # Full Flask dispatch invokes the existing auth decorators, rate limits,
    # ACLs and content_for_viewer/private-band behavior. No service key is minted
    # or forwarded. ContextVar scope cannot be supplied by a caller header.
    app = current_app._get_current_object()
    builder = EnvironBuilder(path=path, method=method, json=body,
                             base_url="https://wikihub.md", environ_base={"REMOTE_ADDR": request.remote_addr or "unknown"})
    token = connector_user.set(user)
    try:
        with app.request_context(builder.get_environ()):
            response = app.full_dispatch_request()
            result = response.get_json(silent=True)
            return result if result is not None else {"error": "non_json_response"}, response.status_code
    finally:
        connector_user.reset(token)
        builder.close()


@connector_bp.route("/api/connector/mcp", methods=["POST"])
def connector_mcp():
    try:
        if request.content_length and request.content_length > MAX_BODY: raise ConnectorError("request_too_large", 413)
        raw = request.stream.read(MAX_BODY+1)
        if len(raw) > MAX_BODY: raise ConnectorError("request_too_large", 413)
        match = re.fullmatch(r"Bearer ([A-Za-z0-9_.-]+)", request.headers.get("Authorization", ""))
        c, scopes = _verify(match.group(1) if match else None, raw)
        try: message = json.loads(raw)
        except (ValueError, UnicodeDecodeError): raise ConnectorError("invalid_json", 400) from None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str): raise ConnectorError("invalid_request", 400)
        identity = ExternalIdentity.query.filter_by(issuer=IDENTITY_ISSUER, subject=c["sub"]).first()
        user = identity.user if identity else None
        if user is None: raise ConnectorError("account_link_required", 409)
        if user.suspended_at is not None: raise ConnectorError("permission_denied", 403)
        _consume(c)
        method = message["method"]
        if method == "initialize": result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "wikihub-ecosystem", "version": "0.1.0"}}
        elif method == "ping": result = {}
        elif method in ("notifications/initialized", "notifications/cancelled"): return "", 202
        elif method == "tools/list": result = {"tools": [t for t in TOOLS if t["annotations"]["readOnlyHint"] or "wikihub:write" in scopes]}
        elif method == "tools/call":
            params = message.get("params")
            if not isinstance(params, dict): raise ConnectorError("invalid_request", 400)
            if not isinstance(params.get("name"), str): raise ConnectorError("invalid_request", 400)
            tool = BY_NAME.get(params.get("name"))
            if not tool: raise ConnectorError("unknown_tool", 400)
            if not tool["annotations"]["readOnlyHint"] and "wikihub:write" not in scopes: raise ConnectorError("insufficient_scope", 403)
            args = _arguments(tool, params.get("arguments", {}))
            data, status = _dispatch(user, *_route(tool["name"], args))
            if status < 400 and tool["name"] == "search":
                data = {"results": [{"id": str(hit.get("wiki", ""))+":"+str(hit.get("page", hit.get("path", ""))),
                                     "title": hit.get("title", ""), "text": hit.get("excerpt", ""),
                                     "url": "https://wikihub.md/@"+quote(str(hit.get("wiki", "")), safe="/")+"/"+quote(str(hit.get("page", hit.get("path", ""))), safe="/")}
                                    for hit in data.get("results", [])]}
            elif status < 400 and tool["name"] == "fetch":
                wiki, path = args["id"].split(":", 1)
                data = {"id": args["id"], "title": data.get("title", ""), "text": data.get("content", ""),
                        "url": "https://wikihub.md/@"+quote(wiki, safe="/")+"/"+quote(path, safe="/"),
                        "metadata": {"visibility": data.get("visibility")}}
            result = {"content": [{"type": "text", "text": json.dumps(data)}], "structuredContent": data}
            if status >= 400: result["isError"] = True
        else: return jsonify({"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": -32601, "message": "Method not found"}})
        return jsonify({"jsonrpc": "2.0", "id": message.get("id"), "result": result})
    except ConnectorError as error:
        return jsonify({"error": error.code}), error.status
    except Exception:
        current_app.logger.exception("Ideaflow connector request failed")
        return jsonify({"error": "app_unavailable"}), 503
