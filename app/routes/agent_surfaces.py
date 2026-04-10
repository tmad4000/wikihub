"""
agent-first surface routes for wikihub.

all the cheap discovery endpoints that make wikihub agent-native:
- /llms.txt and /llms-full.txt (site-wide)
- /@user/wiki/llms.txt (per-wiki)
- /AGENTS.md
- /agents (rendered HTML)
- /.well-known/mcp/server-card.json
- /.well-known/mcp
- /.well-known/wikihub.json
"""

from flask import Response, current_app, jsonify, render_template, request

from app.models import Wiki, Page, User
from app.routes import main_bp


MCP_TOOLS = [
    {"name": "whoami", "description": "Check auth status"},
    {"name": "search", "description": "Full-text search across wikis"},
    {"name": "read_page", "description": "Read a page's content"},
    {"name": "list_pages", "description": "List pages in a wiki"},
    {"name": "create_page", "description": "Create a page"},
    {"name": "update_page", "description": "Replace or patch a page"},
    {"name": "append_section", "description": "Append a section to a page"},
    {"name": "delete_page", "description": "Delete a page"},
    {"name": "set_visibility", "description": "Set page visibility"},
    {"name": "share", "description": "Grant page read/edit access"},
    {"name": "create_wiki", "description": "Create a wiki"},
    {"name": "fork_wiki", "description": "Fork a wiki"},
    {"name": "commit_log", "description": "Read wiki history"},
]


@main_bp.route("/llms.txt")
def llms_txt():
    """site-wide LLM-readable index."""
    lines = [
        "# wikihub",
        "> GitHub for LLM wikis — a hosting platform for markdown knowledge bases.",
        "",
        "## Documentation",
        "- [Agent setup](/agents): API registration, endpoints, MCP config",
        "- [API docs](/agents#api-reference): REST API reference",
        "",
        "## API",
        "- Base: /api/v1",
        "- Auth: Bearer token (POST /api/v1/accounts to register)",
        "- MCP: /mcp",
        "",
        "## Optional",
    ]

    # list public wikis
    public_pages = Page.query.filter(
        Page.visibility.in_(["public", "public-edit"])
    ).join(Wiki).join(User, Wiki.owner_id == User.id).limit(50).all()

    seen_wikis = set()
    for p in public_pages:
        wiki_key = f"{p.wiki.owner.username}/{p.wiki.slug}"
        if wiki_key not in seen_wikis:
            lines.append(f"- [/@{wiki_key}](/@{wiki_key}): {p.wiki.title or p.wiki.slug}")
            seen_wikis.add(wiki_key)

    return Response("\n".join(lines), content_type="text/plain; charset=utf-8")


@main_bp.route("/llms-full.txt")
def llms_full_txt():
    """expanded llms.txt with all public pages."""
    lines = [
        "# wikihub — full index",
        "> All public pages across all wikis.",
        "",
    ]

    pages = Page.query.filter(
        Page.visibility.in_(["public", "public-edit"])
    ).join(Wiki).join(User, Wiki.owner_id == User.id).order_by(
        User.username, Wiki.slug, Page.path
    ).all()

    current_wiki = None
    for p in pages:
        wiki_key = f"{p.wiki.owner.username}/{p.wiki.slug}"
        if wiki_key != current_wiki:
            lines.append(f"\n## @{wiki_key}")
            current_wiki = wiki_key
        from urllib.parse import quote
        url = f"/@{wiki_key}/{quote(p.path.replace('.md', ''), safe='/')}"
        lines.append(f"- [{p.title or p.path}]({url})")

    return Response("\n".join(lines), content_type="text/plain; charset=utf-8")


@main_bp.route("/AGENTS.md")
def agents_md():
    """plain markdown version of the agents page."""
    content = """# wikihub — agent setup

## quick start

Register and get an API key in one call:

```
POST /api/v1/accounts
Content-Type: application/json

{"username": "your-name"}
```

Response: `{"user_id": 1, "username": "your-name", "api_key": "wh_..."}`

Save the API key — it's shown only once. Use as `Authorization: Bearer wh_...`.

## one-click browser sign-in

```
POST /api/v1/auth/magic-link
Authorization: Bearer wh_...
Content-Type: application/json

{"next": "/settings"}
```

Response: `{"login_url": "https://wikihub.md/auth/magic/wl_...", "expires_at": "..."}`

The link is short-lived and single-use. Open it in a browser to establish a normal web session.

## create a wiki

```
POST /api/v1/wikis
Authorization: Bearer wh_...
Content-Type: application/json

{"slug": "my-wiki", "title": "My Wiki", "template": "structured"}
```

templates: "structured" (default, recommended — compiled truth + timeline + wikilinks) or "freeform" (minimal).

## read the schema

after creating a wiki, read schema.md to learn the conventions:

```
GET /api/v1/wikis/your-name/my-wiki/pages/schema.md
Authorization: Bearer wh_...
```

schema.md describes the three-layer architecture (raw/ → wiki/ → schema.md), page format (compiled truth + timeline), wikilink conventions, and the ingest/query/lint workflow. follow it.

## add a page

put source documents in `raw/`, compiled wiki pages in `wiki/`.

```
POST /api/v1/wikis/your-name/my-wiki/pages
Authorization: Bearer wh_...
Content-Type: application/json

{"path": "wiki/hello.md", "content": "# Hello\\n\\nContent.", "visibility": "public"}
```

## MCP endpoint

```json
{
  "mcpServers": {
    "wikihub": {
      "url": "https://wikihub.md/mcp",
      "headers": {"Authorization": "Bearer wh_YOUR_KEY"}
    }
  }
}
```

## content negotiation

`Accept: text/markdown` on any page URL returns raw markdown.
Or append `.md` to the URL.

## discovery

- `/llms.txt` — site-wide index
- `/llms-full.txt` — all public pages
- `/.well-known/mcp/server-card.json` — MCP server card
- `/.well-known/wikihub.json` — bootstrap manifest
"""
    return Response(content, content_type="text/markdown; charset=utf-8")


@main_bp.route("/agents")
def agents_page():
    """rendered HTML agents page."""
    return render_template("agents.html")


@main_bp.route("/.well-known/mcp/server-card.json")
def mcp_server_card():
    """MCP server card (SEP-1649 shape)."""
    return jsonify({
        "name": "wikihub",
        "description": "GitHub for LLM wikis — read, write, and search markdown knowledge bases",
        "url": request.host_url.rstrip("/") + "/mcp",
        "transport": "streamable-http",
        "authentication": {
            "type": "bearer",
            "instructions": "POST /api/v1/accounts to register and get an API key",
        },
        "tools": MCP_TOOLS,
    })


@main_bp.route("/.well-known/mcp")
def mcp_discovery():
    """MCP discovery (SEP-1960 shape)."""
    return jsonify({
        "version": "1.0",
        "servers": [{
            "name": "wikihub",
            "url": request.host_url.rstrip("/") + "/mcp",
            "transport": "streamable-http",
        }],
    })


@main_bp.route("/.well-known/wikihub.json")
def wikihub_bootstrap():
    """site bootstrap manifest."""
    base = request.host_url.rstrip("/")
    return jsonify({
        "api_base": base + "/api/v1",
        "mcp_url": base + "/mcp",
        "signup_url": base + "/api/v1/accounts",
        "docs_url": base + "/agents",
        "llms_txt": base + "/llms.txt",
    })


@main_bp.route("/@<username>/<slug>/llms.txt")
def wiki_llms_txt(username, slug):
    owner = User.query.filter_by(username=username).first_or_404()
    wiki = Wiki.query.filter_by(owner_id=owner.id, slug=slug).first_or_404()
    pages = (
        Page.query.filter_by(wiki_id=wiki.id)
        .filter(Page.visibility.in_(["public", "public-edit"]))
        .order_by(Page.path.asc())
        .all()
    )
    lines = [
        f"# @{owner.username}/{wiki.slug}",
        f"> {wiki.title or wiki.slug}",
        "",
        "## Pages",
    ]
    for page in pages:
        page_url = f"/@{owner.username}/{wiki.slug}/{page.path.replace('.md', '')}"
        lines.append(f"- [{page.title or page.path}]({page_url})")
    return Response("\n".join(lines), content_type="text/plain; charset=utf-8")


@main_bp.route("/@<username>/<slug>/llms-full.txt")
def wiki_llms_full_txt(username, slug):
    owner = User.query.filter_by(username=username).first_or_404()
    wiki = Wiki.query.filter_by(owner_id=owner.id, slug=slug).first_or_404()
    pages = (
        Page.query.filter_by(wiki_id=wiki.id)
        .filter(Page.visibility.in_(["public", "public-edit"]))
        .order_by(Page.path.asc())
        .all()
    )
    lines = [f"# @{owner.username}/{wiki.slug} — full index", ""]
    for page in pages:
        lines.append(f"## {page.title or page.path}")
        lines.append(f"- Path: {page.path}")
        lines.append(f"- URL: /@{owner.username}/{wiki.slug}/{page.path.replace('.md', '')}")
        if page.excerpt:
            lines.append(f"- Excerpt: {page.excerpt}")
        lines.append("")
    return Response("\n".join(lines), content_type="text/plain; charset=utf-8")


def _mcp_proxy_request(method, path, json_body=None):
    headers = {}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    with current_app.test_client() as client:
        response = client.open(path, method=method, json=json_body, headers=headers)
        payload = response.get_json(silent=True)
        return response.status_code, payload


def _mcp_tool_result(name, arguments):
    if name == "whoami":
        return _mcp_proxy_request("GET", "/api/v1/accounts/me")[1]
    if name == "search":
        query = arguments.get("q", "")
        return _mcp_proxy_request("GET", f"/api/v1/search?q={query}")[1]
    if name == "read_page":
        return _mcp_proxy_request("GET", f"/api/v1/wikis/{arguments['owner']}/{arguments['slug']}/pages/{arguments['path']}")[1]
    if name == "list_pages":
        return _mcp_proxy_request("GET", f"/api/v1/wikis/{arguments['owner']}/{arguments['slug']}/pages")[1]
    if name == "create_page":
        return _mcp_proxy_request("POST", f"/api/v1/wikis/{arguments['owner']}/{arguments['slug']}/pages", arguments)[1]
    if name == "update_page":
        path = arguments.pop("path")
        return _mcp_proxy_request("PATCH", f"/api/v1/wikis/{arguments['owner']}/{arguments['slug']}/pages/{path}", arguments)[1]
    if name == "append_section":
        path = arguments.pop("path")
        return _mcp_proxy_request("POST", f"/api/v1/wikis/{arguments['owner']}/{arguments['slug']}/pages/{path}/append-section", arguments)[1]
    if name == "delete_page":
        return _mcp_proxy_request("DELETE", f"/api/v1/wikis/{arguments['owner']}/{arguments['slug']}/pages/{arguments['path']}")[1]
    if name == "set_visibility":
        path = arguments.pop("path")
        return _mcp_proxy_request("POST", f"/api/v1/wikis/{arguments['owner']}/{arguments['slug']}/pages/{path}/visibility", arguments)[1]
    if name == "share":
        path = arguments.pop("path")
        return _mcp_proxy_request("POST", f"/api/v1/wikis/{arguments['owner']}/{arguments['slug']}/pages/{path}/share", arguments)[1]
    if name == "create_wiki":
        return _mcp_proxy_request("POST", "/api/v1/wikis", arguments)[1]
    if name == "fork_wiki":
        return _mcp_proxy_request("POST", f"/api/v1/wikis/{arguments['owner']}/{arguments['slug']}/fork")[1]
    if name == "commit_log":
        return _mcp_proxy_request("GET", f"/api/v1/wikis/{arguments['owner']}/{arguments['slug']}/history")[1]
    return {"error": f"Unknown tool '{name}'"}


@main_bp.route("/mcp", methods=["GET", "POST"])
def mcp_endpoint():
    if request.method == "GET":
        return jsonify({
            "name": "wikihub",
            "transport": "streamable-http",
            "tools": MCP_TOOLS,
        })

    payload = request.get_json(silent=True) or {}
    method = payload.get("method")
    request_id = payload.get("id")

    if method == "initialize":
        return jsonify({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "serverInfo": {"name": "wikihub", "version": "1.0"},
                "capabilities": {"tools": {}},
            },
        })
    if method == "tools/list":
        return jsonify({"jsonrpc": "2.0", "id": request_id, "result": {"tools": MCP_TOOLS}})
    if method == "tools/call":
        params = payload.get("params", {})
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        result = _mcp_tool_result(tool_name, dict(arguments))
        return jsonify({"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "json", "json": result}]}})

    return jsonify({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}), 404
