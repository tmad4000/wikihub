"""
wikihub MCP server.

exposes wiki content to AI agents via model context protocol.
agents can list wikis, search pages, read page content, and list pages.

usage:
  python3 mcp_server.py                          # default: http://localhost:5100
  python3 mcp_server.py --base-url https://wikihub.globalbr.ai
  WIKIHUB_API_KEY=wh_xxx python3 mcp_server.py   # authenticated access

add to claude code:
  claude mcp add wikihub -- python3 /path/to/mcp_server.py
"""

import argparse
import os
import urllib.request
import urllib.parse
import json

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("wikihub")

BASE_URL = os.environ.get("WIKIHUB_BASE_URL", "http://localhost:5100")
API_KEY = os.environ.get("WIKIHUB_API_KEY", "")


def _api(path, accept="application/json"):
    url = f"{BASE_URL}/api/v1{path}"
    req = urllib.request.Request(url)
    req.add_header("Accept", accept)
    if API_KEY:
        req.add_header("Authorization", f"Bearer {API_KEY}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "message": e.read().decode()[:200]}
    except Exception as e:
        return {"error": str(e)}


def _fetch_text(url):
    """fetch raw text from a URL (for markdown content negotiation)."""
    full = f"{BASE_URL}{url}"
    req = urllib.request.Request(full)
    req.add_header("Accept", "text/markdown")
    if API_KEY:
        req.add_header("Authorization", f"Bearer {API_KEY}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode()
    except Exception as e:
        return f"error: {e}"


@mcp.tool()
def list_pages(owner: str, wiki: str) -> str:
    """list all pages in a wiki. returns page paths, titles, and visibility."""
    result = _api(f"/wikis/{owner}/{wiki}/pages")
    if "error" in result:
        return json.dumps(result)
    pages = result if isinstance(result, list) else result.get("pages", [])
    lines = [f"# Pages in @{owner}/{wiki}\n"]
    for p in pages:
        title = p.get("title") or p.get("path", "?")
        vis = p.get("visibility", "?")
        path = p.get("path", "?")
        lines.append(f"- [{title}]({path}) [{vis}]")
    return "\n".join(lines) if lines else "no pages found"


@mcp.tool()
def read_page(owner: str, wiki: str, page_path: str) -> str:
    """read a wiki page's markdown content. page_path is like 'wiki/agents' (no .md extension)."""
    content = _fetch_text(f"/@{owner}/{wiki}/{page_path}.md")
    return content


@mcp.tool()
def search_wiki(query: str, owner: str = "", wiki: str = "") -> str:
    """search across wikis by text query. optionally scope to a specific owner/wiki.
    returns matching pages with excerpts."""
    params = {"q": query}
    if owner:
        params["owner"] = owner
    if wiki:
        params["wiki"] = wiki
    qs = urllib.parse.urlencode(params)
    result = _api(f"/search?{qs}")
    if "error" in result:
        return json.dumps(result)
    results = result if isinstance(result, list) else result.get("results", [])
    lines = [f"# Search results for '{query}'\n"]
    for r in results:
        title = r.get("title", r.get("page", "?"))
        page = r.get("page", r.get("path", ""))
        wiki_ref = r.get("wiki", "")  # "owner/slug" format
        excerpt = r.get("excerpt", "")
        url = f"/@{wiki_ref}/{page.replace('.md', '')}" if wiki_ref else page
        lines.append(f"## {title}")
        lines.append(f"  url: {url}")
        if excerpt:
            lines.append(f"  {excerpt}")
        lines.append("")
    return "\n".join(lines) if len(lines) > 1 else "no results found"


@mcp.tool()
def get_wiki_info(owner: str, wiki: str) -> str:
    """get metadata about a wiki: title, description, page count, star count."""
    result = _api(f"/wikis/{owner}/{wiki}")
    if "error" in result:
        return json.dumps(result)
    lines = [
        f"# @{owner}/{wiki}",
        f"title: {result.get('title', wiki)}",
        f"description: {result.get('description', '(none)')}",
        f"stars: {result.get('star_count', 0)}",
        f"forks: {result.get('fork_count', 0)}",
    ]
    return "\n".join(lines)


@mcp.tool()
def read_llms_txt(owner: str, wiki: str) -> str:
    """read a wiki's llms.txt — a structured index of all pages optimized for LLM consumption."""
    return _fetch_text(f"/@{owner}/{wiki}/llms.txt")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WikiHub MCP Server")
    parser.add_argument("--base-url", default=BASE_URL, help="WikiHub base URL")
    args = parser.parse_args()
    BASE_URL = args.base_url
    mcp.run()
