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

from flask import Response, jsonify, render_template, request

from app.models import Wiki, Page, User
from app.routes import main_bp


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

## create a wiki

```
POST /api/v1/wikis
Authorization: Bearer wh_...
Content-Type: application/json

{"slug": "my-wiki", "title": "My Wiki"}
```

## add a page

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
        "tools": [
            {"name": "whoami", "description": "check auth status"},
            {"name": "create_wiki", "description": "create a new wiki"},
            {"name": "create_page", "description": "add a page to a wiki"},
            {"name": "read_page", "description": "read a page's content"},
            {"name": "update_page", "description": "update a page"},
            {"name": "delete_page", "description": "remove a page"},
            {"name": "search", "description": "full-text search across wikis"},
            {"name": "list_pages", "description": "list pages in a wiki"},
            {"name": "set_visibility", "description": "change page visibility"},
            {"name": "fork_wiki", "description": "fork a wiki"},
        ],
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
