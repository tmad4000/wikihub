"""wikihub CLI entry point.

thin wrapper over the REST API at {server}/api/v1. authentication uses
the credentials file at ~/.wikihub/credentials.json (documented in
app/credentials_hint.py) or the env vars WIKIHUB_SERVER / WIKIHUB_USERNAME
/ WIKIHUB_API_KEY.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests

from wikihub_cli import __version__

DEFAULT_SERVER = "https://wikihub.md"
CREDENTIALS_PATH = Path.home() / ".wikihub" / "credentials.json"
DEFAULT_PROFILE = "default"


# ---------- credential handling ----------

def load_profile(profile: str = DEFAULT_PROFILE) -> dict[str, str]:
    """Read a profile from credentials.json. Env vars override the file."""
    data: dict[str, Any] = {}
    if CREDENTIALS_PATH.exists():
        try:
            data = json.loads(CREDENTIALS_PATH.read_text())
        except json.JSONDecodeError:
            data = {}
    out = dict(data.get(profile) or {})
    for key, env in (("server", "WIKIHUB_SERVER"), ("username", "WIKIHUB_USERNAME"), ("api_key", "WIKIHUB_API_KEY")):
        if os.environ.get(env):
            out[key] = os.environ[env]
    return out


def save_profile(profile: str, server: str, username: str, api_key: str) -> None:
    """Write (merge) a profile into credentials.json, mode 0600."""
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if CREDENTIALS_PATH.exists():
        try:
            data = json.loads(CREDENTIALS_PATH.read_text())
        except json.JSONDecodeError:
            data = {}
    data[profile] = {"server": server, "username": username, "api_key": api_key}
    CREDENTIALS_PATH.write_text(json.dumps(data, indent=2))
    os.chmod(CREDENTIALS_PATH, 0o600)


def delete_profile(profile: str) -> bool:
    if not CREDENTIALS_PATH.exists():
        return False
    try:
        data = json.loads(CREDENTIALS_PATH.read_text())
    except json.JSONDecodeError:
        return False
    if profile not in data:
        return False
    del data[profile]
    CREDENTIALS_PATH.write_text(json.dumps(data, indent=2))
    os.chmod(CREDENTIALS_PATH, 0o600)
    return True


# ---------- HTTP helpers ----------

class ClientError(Exception):
    pass


def get_client(args: argparse.Namespace, require_auth: bool = True) -> tuple[str, dict[str, str]]:
    """Resolve (server, headers) from args+profile+env."""
    prof = load_profile(getattr(args, "profile", DEFAULT_PROFILE))
    server = (getattr(args, "server", None) or prof.get("server") or os.environ.get("WIKIHUB_SERVER") or DEFAULT_SERVER).rstrip("/")
    headers = {"Accept": "application/json"}
    api_key = getattr(args, "api_key", None) or prof.get("api_key") or os.environ.get("WIKIHUB_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    elif require_auth:
        raise ClientError(
            "not authenticated — run `wikihub login` or `wikihub signup`, "
            "or set WIKIHUB_API_KEY"
        )
    return server, headers


def api_request(method: str, url: str, headers: dict[str, str], **kwargs) -> requests.Response:
    try:
        return requests.request(method, url, headers=headers, timeout=30, **kwargs)
    except requests.RequestException as e:
        raise ClientError(f"network error: {e}")


def raise_for_api_error(resp: requests.Response) -> None:
    if resp.status_code >= 400:
        try:
            body = resp.json()
            msg = body.get("message") or body.get("error") or resp.text
        except Exception:
            msg = resp.text
        raise ClientError(f"{resp.status_code}: {msg}")


# ---------- command implementations ----------

def cmd_signup(args: argparse.Namespace) -> int:
    server = (args.server or DEFAULT_SERVER).rstrip("/")
    payload: dict[str, Any] = {}
    if args.username:
        payload["username"] = args.username
    if args.password:
        payload["password"] = args.password
    if args.email:
        payload["email"] = args.email
    if args.display_name:
        payload["display_name"] = args.display_name
    resp = api_request("POST", f"{server}/api/v1/accounts", {"Accept": "application/json"}, json=payload)
    raise_for_api_error(resp)
    body = resp.json()
    username = body.get("username")
    api_key = body.get("api_key")
    if not (username and api_key):
        raise ClientError(f"unexpected signup response: {body}")
    save_profile(args.profile, server, username, api_key)
    print(f"signed up as {username} on {server}")
    print(f"credentials saved to {CREDENTIALS_PATH} (profile: {args.profile})")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    server = (args.server or DEFAULT_SERVER).rstrip("/")
    if args.api_key:
        # verify key works, then save
        headers = {"Authorization": f"Bearer {args.api_key}", "Accept": "application/json"}
        resp = api_request("GET", f"{server}/api/v1/accounts/me", headers)
        raise_for_api_error(resp)
        me = resp.json()
        username = me.get("username")
        if not username:
            raise ClientError(f"unexpected whoami response: {me}")
        save_profile(args.profile, server, username, args.api_key)
        print(f"logged in as {username}")
        return 0
    if not (args.username and args.password):
        raise ClientError("provide --api-key, or --username and --password")
    resp = api_request(
        "POST", f"{server}/api/v1/auth/token",
        {"Accept": "application/json"},
        json={"username": args.username, "password": args.password},
    )
    raise_for_api_error(resp)
    body = resp.json()
    api_key = body.get("api_key")
    if not api_key:
        raise ClientError(f"unexpected token response: {body}")
    save_profile(args.profile, server, args.username, api_key)
    print(f"logged in as {args.username}")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    if delete_profile(args.profile):
        print(f"removed profile '{args.profile}' from {CREDENTIALS_PATH}")
    else:
        print(f"no profile '{args.profile}' found")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    server, headers = get_client(args)
    resp = api_request("GET", f"{server}/api/v1/accounts/me", headers)
    raise_for_api_error(resp)
    me = resp.json()
    if args.json:
        print(json.dumps(me, indent=2))
    else:
        print(f"{me.get('username')} ({me.get('display_name') or '—'})")
        if me.get("email"):
            print(f"  email: {me['email']}")
        print(f"  server: {server}")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    server, headers = get_client(args)
    payload = {"slug": args.slug}
    if args.title:
        payload["title"] = args.title
    if args.description:
        payload["description"] = args.description
    if args.template:
        payload["template"] = args.template
    resp = api_request("POST", f"{server}/api/v1/wikis", headers, json=payload)
    raise_for_api_error(resp)
    body = resp.json()
    owner = body.get("owner")
    slug = body.get("slug")
    print(f"created wiki: {owner}/{slug}")
    print(f"  web: {server}/@{owner}/{slug}")
    print(f"  git: {server}/@{owner}/{slug}.git")
    return 0


def _parse_wiki_spec(spec: str) -> tuple[str, str]:
    """Accept 'owner/slug' or '@owner/slug'."""
    s = spec.lstrip("@")
    if "/" not in s:
        raise ClientError(f"expected owner/slug, got: {spec}")
    owner, slug = s.split("/", 1)
    return owner, slug


def _parse_page_spec(spec: str) -> tuple[str, str, str]:
    """Accept 'owner/slug/path/to/page' or '@owner/slug/path'."""
    s = spec.lstrip("@")
    parts = s.split("/", 2)
    if len(parts) < 3:
        raise ClientError(f"expected owner/slug/path, got: {spec}")
    owner, slug, path = parts
    return owner, slug, path


def cmd_ls(args: argparse.Namespace) -> int:
    server, headers = get_client(args, require_auth=False)
    owner, slug = _parse_wiki_spec(args.wiki)
    resp = api_request("GET", f"{server}/api/v1/wikis/{owner}/{slug}/pages", headers)
    raise_for_api_error(resp)
    body = resp.json()
    pages = body.get("pages") or body.get("results") or body if isinstance(body, list) else body.get("pages", [])
    if args.json:
        print(json.dumps(body, indent=2))
        return 0
    for p in pages:
        path = p.get("path", "")
        title = p.get("title") or path
        vis = p.get("visibility", "public")
        print(f"{vis:<14} {path:<40} {title}")
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    server, headers = get_client(args, require_auth=False)
    owner, slug, path = _parse_page_spec(args.page)
    h = dict(headers)
    h["Accept"] = "text/markdown"
    resp = api_request("GET", f"{server}/api/v1/wikis/{owner}/{slug}/pages/{path}", h)
    raise_for_api_error(resp)
    sys.stdout.write(resp.text)
    if resp.text and not resp.text.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def _read_content(args: argparse.Namespace) -> str:
    if args.file:
        return Path(args.file).read_text()
    if args.content is not None:
        return args.content
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise ClientError("provide --file, --content, or pipe content to stdin")


def cmd_write(args: argparse.Namespace) -> int:
    server, headers = get_client(args)
    owner, slug, path = _parse_page_spec(args.page)
    content = _read_content(args)
    url = f"{server}/api/v1/wikis/{owner}/{slug}/pages/{path}"

    # check existence — if exists, PUT; otherwise POST to /pages
    check = api_request("GET", url, headers)
    exists = check.status_code == 200
    if exists:
        payload = {"content": content}
        if args.visibility:
            payload["visibility"] = args.visibility
        resp = api_request("PUT", url, headers, json=payload)
    else:
        payload = {"path": path, "content": content}
        if args.visibility:
            payload["visibility"] = args.visibility
        resp = api_request("POST", f"{server}/api/v1/wikis/{owner}/{slug}/pages", headers, json=payload)
    raise_for_api_error(resp)
    body = resp.json()
    verb = "updated" if exists else "created"
    print(f"{verb} {owner}/{slug}/{body.get('path', path)}")
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    # alias: wikihub publish <file> --to <owner/slug/path>
    if not args.to:
        # derive page path from filename basename
        raise ClientError("--to <owner/slug/path> is required")
    args.file = args.local_file
    args.content = None
    args.page = args.to
    return cmd_write(args)


def cmd_rm(args: argparse.Namespace) -> int:
    server, headers = get_client(args)
    owner, slug, path = _parse_page_spec(args.page)
    resp = api_request(
        "DELETE", f"{server}/api/v1/wikis/{owner}/{slug}/pages/{path}", headers,
    )
    raise_for_api_error(resp)
    print(f"deleted {owner}/{slug}/{path}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    server, headers = get_client(args, require_auth=False)
    params: dict[str, Any] = {"q": args.query, "limit": args.limit}
    if args.wiki:
        owner, slug = _parse_wiki_spec(args.wiki)
        params["scope"] = "wiki"
        params["wiki"] = f"{owner}/{slug}"
    resp = api_request("GET", f"{server}/api/v1/search", headers, params=params)
    raise_for_api_error(resp)
    body = resp.json()
    if args.json:
        print(json.dumps(body, indent=2))
        return 0
    results = body.get("results", [])
    total = body.get("total", len(results))
    print(f"{total} result(s)")
    for r in results:
        wiki = r.get("wiki", "")
        page = r.get("page", "")
        title = r.get("title") or page
        print(f"  {wiki}/{page}  — {title}")
    return 0


def cmd_mcp_config(args: argparse.Namespace) -> int:
    server, headers = get_client(args, require_auth=False)
    # Try to pull credentials for the Authorization header
    prof = load_profile(args.profile)
    api_key = prof.get("api_key") or os.environ.get("WIKIHUB_API_KEY")
    entry: dict[str, Any] = {"url": f"{server}/mcp"}
    if api_key:
        entry["headers"] = {"Authorization": f"Bearer {api_key}"}
    cfg = {"mcpServers": {"wikihub": entry}}
    print(json.dumps(cfg, indent=2))
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    print(f"wikihub-cli {__version__}")
    return 0


# ---------- argparse wiring ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wikihub", description="WikiHub CLI")
    p.add_argument("--server", help=f"server URL (default: {DEFAULT_SERVER} or credentials file)")
    p.add_argument("--profile", default=DEFAULT_PROFILE, help="credentials profile (default: default)")
    p.add_argument("--api-key", help="override API key (else: credentials file or WIKIHUB_API_KEY)")
    p.add_argument("--json", action="store_true", help="emit JSON where applicable")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("signup", help="create a new account and save credentials")
    s.add_argument("--username")
    s.add_argument("--password")
    s.add_argument("--email")
    s.add_argument("--display-name")
    s.set_defaults(func=cmd_signup)

    s = sub.add_parser("login", help="log in with username/password or save an existing API key")
    s.add_argument("--username")
    s.add_argument("--password")
    s.add_argument("--save-api-key", dest="api_key", help="save this existing key after verifying")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("logout", help="remove a profile from credentials")
    s.set_defaults(func=cmd_logout)

    s = sub.add_parser("whoami", help="print the authenticated account")
    s.set_defaults(func=cmd_whoami)

    s = sub.add_parser("new", help="create a new wiki")
    s.add_argument("slug")
    s.add_argument("--title")
    s.add_argument("--description")
    s.add_argument("--template", choices=["freeform", "structured"])
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("ls", help="list pages in a wiki")
    s.add_argument("wiki", help="owner/slug")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("read", help="read a page's markdown to stdout")
    s.add_argument("page", help="owner/slug/path")
    s.set_defaults(func=cmd_read)

    s = sub.add_parser("write", help="create or update a page")
    s.add_argument("page", help="owner/slug/path")
    s.add_argument("--file", help="read content from file")
    s.add_argument("--content", help="content inline")
    s.add_argument("--visibility", choices=["public", "private", "unlisted", "public-edit", "unlisted-edit"])
    s.set_defaults(func=cmd_write)

    s = sub.add_parser("publish", help="publish a local markdown file to a page (alias of write)")
    s.add_argument("local_file", help="path to local markdown file")
    s.add_argument("--to", required=True, help="owner/slug/path destination")
    s.add_argument("--visibility", choices=["public", "private", "unlisted", "public-edit", "unlisted-edit"])
    s.set_defaults(func=cmd_publish)

    s = sub.add_parser("rm", help="delete a page")
    s.add_argument("page", help="owner/slug/path")
    s.set_defaults(func=cmd_rm)

    s = sub.add_parser("search", help="full-text search")
    s.add_argument("query")
    s.add_argument("--wiki", help="scope to owner/slug")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("mcp-config", help="print mcpServers JSON to wire WikiHub's MCP endpoint into an agent")
    s.set_defaults(func=cmd_mcp_config)

    s = sub.add_parser("version", help="print CLI version")
    s.set_defaults(func=cmd_version)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ClientError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
