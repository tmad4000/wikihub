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
# top-level keys in credentials.json that are NOT profiles
_META_KEYS = {"_active"}


# ---------- credential handling ----------

def _load_creds() -> dict[str, Any]:
    """Read the whole credentials file. Returns {} on missing/invalid."""
    if not CREDENTIALS_PATH.exists():
        return {}
    try:
        return json.loads(CREDENTIALS_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def _write_creds(data: dict[str, Any]) -> None:
    """Write the credentials file and lock it to 0600."""
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_PATH.write_text(json.dumps(data, indent=2))
    os.chmod(CREDENTIALS_PATH, 0o600)


def list_profiles() -> list[str]:
    """Return all profile names in credentials.json (excludes _active and other meta keys)."""
    return [k for k in _load_creds().keys() if k not in _META_KEYS]


def get_active_profile() -> str | None:
    """Return the name of the currently active profile, or None."""
    data = _load_creds()
    active = data.get("_active")
    if isinstance(active, str) and active in data and active not in _META_KEYS:
        return active
    return None


def set_active_profile(name: str) -> None:
    """Set the active profile. Assumes name already exists; caller should check."""
    data = _load_creds()
    data["_active"] = name
    _write_creds(data)


def clear_active_profile() -> None:
    """Remove the _active marker (e.g. after removing the active profile and no fallback)."""
    data = _load_creds()
    if "_active" in data:
        del data["_active"]
        _write_creds(data)


def resolve_profile_name(explicit: str | None) -> str:
    """Resolve which profile to use when an explicit --profile wasn't passed.

    Precedence: explicit arg > _active in credentials.json > "default".
    """
    if explicit:
        return explicit
    return get_active_profile() or DEFAULT_PROFILE


def load_profile(profile: str = DEFAULT_PROFILE) -> dict[str, str]:
    """Read a profile from credentials.json. Env vars override the file."""
    data = _load_creds()
    raw = data.get(profile)
    out: dict[str, str] = dict(raw) if isinstance(raw, dict) else {}
    for key, env in (("server", "WIKIHUB_SERVER"), ("username", "WIKIHUB_USERNAME"), ("api_key", "WIKIHUB_API_KEY")):
        if os.environ.get(env):
            out[key] = os.environ[env]
    return out


def save_profile(profile: str, server: str, username: str, api_key: str) -> None:
    """Write (merge) a profile into credentials.json, mode 0600."""
    if profile in _META_KEYS:
        raise ClientError(f"'{profile}' is a reserved name and cannot be used as a profile")
    data = _load_creds()
    data[profile] = {"server": server, "username": username, "api_key": api_key}
    _write_creds(data)


def delete_profile(profile: str) -> bool:
    """Remove a profile. If it was active, clear or reassign _active.

    Returns True if the profile was removed, False if it didn't exist.
    """
    data = _load_creds()
    if profile in _META_KEYS or profile not in data:
        return False
    was_active = data.get("_active") == profile
    del data[profile]
    if was_active:
        remaining = [k for k in data.keys() if k not in _META_KEYS]
        if remaining:
            # reassign to the first remaining profile (prefer "default" if present)
            data["_active"] = "default" if "default" in remaining else remaining[0]
        else:
            data.pop("_active", None)
    _write_creds(data)
    return True


# ---------- HTTP helpers ----------

class ClientError(Exception):
    pass


def get_client(args: argparse.Namespace, require_auth: bool = True) -> tuple[str, dict[str, str]]:
    """Resolve (server, headers) from args+profile+env.

    Profile resolution precedence:
      1. explicit --profile NAME (args.profile when set)
      2. WIKIHUB_API_KEY env (via load_profile env overrides)
      3. _active in credentials.json
      4. "default"
    """
    prof = load_profile(resolve_profile_name(getattr(args, "profile", None)))
    server = (getattr(args, "server", None) or prof.get("server") or os.environ.get("WIKIHUB_SERVER") or DEFAULT_SERVER).rstrip("/")
    headers = {"Accept": "application/json"}
    api_key = getattr(args, "api_key", None) or prof.get("api_key") or os.environ.get("WIKIHUB_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    elif require_auth:
        raise ClientError(
            "not authenticated — run `wikihub auth login` or `wikihub signup`, "
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

def _server_host(server: str) -> str:
    """Strip scheme/port/path so the hostname is safe to use in a profile name."""
    s = server
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    # drop any path + port
    s = s.split("/", 1)[0]
    s = s.split(":", 1)[0]
    return s or "wikihub"


def _suggest_profile_name(username: str, server: str) -> str:
    """Pick a unique profile name for a new account.

    Preference order: "default" (if unused) → "<username>@<host>" → "<username>@<host>-2", ...
    """
    existing = set(list_profiles())
    if DEFAULT_PROFILE not in existing:
        return DEFAULT_PROFILE
    base = f"{username}@{_server_host(server)}"
    if base not in existing:
        return base
    i = 2
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


def _do_signup(server: str, *, username: str | None, password: str | None,
               email: str | None, display_name: str | None) -> tuple[str, str]:
    """Call POST /api/v1/accounts, return (username, api_key)."""
    payload: dict[str, Any] = {}
    if username:
        payload["username"] = username
    if password:
        payload["password"] = password
    if email:
        payload["email"] = email
    if display_name:
        payload["display_name"] = display_name
    resp = api_request("POST", f"{server}/api/v1/accounts", {"Accept": "application/json"}, json=payload)
    raise_for_api_error(resp)
    body = resp.json()
    if not (body.get("username") and body.get("api_key")):
        raise ClientError(f"unexpected signup response: {body}")
    return body["username"], body["api_key"]


def _do_login(server: str, *, username: str | None, password: str | None,
              api_key: str | None) -> tuple[str, str]:
    """Authenticate against the server. Returns (username, api_key).

    If api_key is given, it's verified via /accounts/me. Otherwise
    username+password exchanges for a token.
    """
    if api_key:
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        resp = api_request("GET", f"{server}/api/v1/accounts/me", headers)
        raise_for_api_error(resp)
        me = resp.json()
        if not me.get("username"):
            raise ClientError(f"unexpected whoami response: {me}")
        return me["username"], api_key
    if not (username and password):
        raise ClientError("provide --api-key, or --username and --password")
    resp = api_request(
        "POST", f"{server}/api/v1/auth/token",
        {"Accept": "application/json"},
        json={"username": username, "password": password},
    )
    raise_for_api_error(resp)
    body = resp.json()
    if not body.get("api_key"):
        raise ClientError(f"unexpected token response: {body}")
    return username, body["api_key"]


def cmd_signup(args: argparse.Namespace) -> int:
    server = (args.server or DEFAULT_SERVER).rstrip("/")
    username, api_key = _do_signup(
        server,
        username=args.username, password=args.password,
        email=args.email, display_name=args.display_name,
    )
    # Back-compat: top-level `wikihub signup` without explicit --profile writes
    # to "default" (possibly overwriting). `wikihub auth login` uses smarter logic.
    profile = args.profile or DEFAULT_PROFILE
    save_profile(profile, server, username, api_key)
    # Make newly signed-up account the active one.
    set_active_profile(profile)
    print(f"signed up as {username} on {server}")
    print(f"credentials saved to {CREDENTIALS_PATH} (profile: {profile})")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    server = (args.server or DEFAULT_SERVER).rstrip("/")
    username, api_key = _do_login(
        server,
        username=args.username, password=args.password,
        api_key=args.api_key,
    )
    # Back-compat: top-level `wikihub login` without --profile writes to "default".
    profile = args.profile or DEFAULT_PROFILE
    save_profile(profile, server, username, api_key)
    set_active_profile(profile)
    print(f"logged in as {username}")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    # Back-compat: `wikihub logout` removes "default" when no --profile given.
    # `wikihub auth logout` has richer semantics (see cmd_auth_logout).
    profile = args.profile or DEFAULT_PROFILE
    if delete_profile(profile):
        print(f"removed profile '{profile}' from {CREDENTIALS_PATH}")
    else:
        print(f"no profile '{profile}' found")
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
        active = get_active_profile() or DEFAULT_PROFILE
        print(f"  profile: {active}")
    return 0


# ---------- gh-style multi-account auth commands ----------

def cmd_auth_login(args: argparse.Namespace) -> int:
    """Add a new account without overwriting existing ones.

    Behaviour:
      - If --profile is given explicitly, that name is used.
      - Otherwise we pick a name automatically:
          * "default" if no profile exists yet
          * "<username>@<host>" if "default" is taken
          * "<username>@<host>-N" if that's also taken
      - Newly-added profile is made the active one.
    """
    server = (args.server or DEFAULT_SERVER).rstrip("/")
    if args.signup:
        username, api_key = _do_signup(
            server,
            username=args.username, password=args.password,
            email=args.email, display_name=args.display_name,
        )
        verb = "signed up"
    else:
        username, api_key = _do_login(
            server,
            username=args.username, password=args.password,
            api_key=args.api_key,
        )
        verb = "logged in"

    # Resolve profile name: explicit --profile > auto-named
    profile = args.profile or _suggest_profile_name(username, server)
    existing = list_profiles()
    overwriting = profile in existing
    save_profile(profile, server, username, api_key)
    set_active_profile(profile)

    print(f"{verb} as {username} on {server}")
    if overwriting:
        print(f"updated profile '{profile}' (now active)")
    else:
        print(f"added profile '{profile}' (now active)")
    print(f"credentials: {CREDENTIALS_PATH}")
    return 0


def cmd_auth_switch(args: argparse.Namespace) -> int:
    """Set the active profile."""
    target = args.target_profile
    profiles = list_profiles()
    if target not in profiles:
        if profiles:
            raise ClientError(
                f"no profile named '{target}' (known: {', '.join(sorted(profiles))})"
            )
        raise ClientError(f"no profiles configured — run `wikihub auth login` first")
    set_active_profile(target)
    print(f"switched to profile '{target}'")
    return 0


def cmd_auth_status(args: argparse.Namespace) -> int:
    """List all profiles, marking the active one with '*'."""
    data = _load_creds()
    profiles = [k for k in data.keys() if k not in _META_KEYS]
    active = get_active_profile()

    if args.json:
        out = {
            "active": active,
            "credentials_path": str(CREDENTIALS_PATH),
            "profiles": {
                name: {
                    "server": (data[name].get("server") if isinstance(data.get(name), dict) else None),
                    "username": (data[name].get("username") if isinstance(data.get(name), dict) else None),
                    "active": name == active,
                }
                for name in profiles
            },
        }
        print(json.dumps(out, indent=2))
        return 0

    if not profiles:
        print("no profiles — run `wikihub auth login` to add one")
        print(f"credentials: {CREDENTIALS_PATH}")
        return 0

    # column widths
    name_w = max(len(n) for n in profiles)
    name_w = max(name_w, len("profile"))
    user_w = max(
        (len(data[n].get("username", "") or "")
         for n in profiles if isinstance(data.get(n), dict)),
        default=0,
    )
    user_w = max(user_w, len("username"))
    print(f"  {'profile':<{name_w}}  {'username':<{user_w}}  server")
    for name in sorted(profiles):
        prof = data.get(name) if isinstance(data.get(name), dict) else {}
        marker = "*" if name == active else " "
        user = prof.get("username", "") or "—"
        server = prof.get("server", "") or "—"
        print(f"{marker} {name:<{name_w}}  {user:<{user_w}}  {server}")
    print()
    print(f"credentials: {CREDENTIALS_PATH}")
    if not active and profiles:
        print("no active profile — run `wikihub auth switch <profile>` to pick one")
    return 0


def cmd_auth_logout(args: argparse.Namespace) -> int:
    """Remove a profile. Without an argument, removes the active profile."""
    target = args.target_profile
    if not target:
        target = get_active_profile()
        if not target:
            # fall back to "default" so `wikihub auth logout` on a single-account
            # install "just works" even without _active set.
            if DEFAULT_PROFILE in list_profiles():
                target = DEFAULT_PROFILE
            else:
                raise ClientError("no active profile — pass a profile name or `wikihub auth switch` first")

    if not delete_profile(target):
        print(f"no profile '{target}' found")
        return 0
    print(f"removed profile '{target}'")
    new_active = get_active_profile()
    if new_active and new_active != target:
        print(f"active profile is now '{new_active}'")
    elif not new_active and list_profiles():
        print("no active profile — run `wikihub auth switch <profile>`")
    return 0


def cmd_auth_list(args: argparse.Namespace) -> int:
    """Alias of `auth status` that only emits profile names (one per line).

    Useful for shell completion and scripting. Active profile is marked with '*'.
    """
    profiles = sorted(list_profiles())
    if args.json:
        print(json.dumps(profiles, indent=2))
        return 0
    active = get_active_profile()
    for name in profiles:
        marker = "* " if name == active else "  "
        print(f"{marker}{name}")
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


def _id_to_grant_field(identifier: str) -> dict[str, str]:
    """an identifier is an email if it contains '@' and '.', else a username."""
    s = identifier.strip().lstrip("@").lower()
    if "@" in s and "." in s.split("@", 1)[1]:
        return {"email": s}
    return {"username": s}


def cmd_share_add(args: argparse.Namespace) -> int:
    server, headers = get_client(args)
    owner, slug = _parse_wiki_spec(args.wiki)
    grants = [{**_id_to_grant_field(u), "role": args.role, "pattern": args.pattern} for u in args.users]
    resp = api_request(
        "POST", f"{server}/api/v1/wikis/{owner}/{slug}/share/bulk", headers,
        json={"grants": grants},
    )
    raise_for_api_error(resp)
    body = resp.json()
    if args.json:
        print(json.dumps(body, indent=2))
        return 0
    for g in body.get("added", []):
        print(f"added   @{g['username']}:{g['role']} → {g['pattern']}")
    for g in body.get("skipped", []):
        print(f"skipped @{g['username']}:{g['role']} → {g['pattern']} (already granted)")
    for g in body.get("failed", []):
        print(f"failed  {g.get('input', '?')} — {g.get('error', 'unknown')}", file=sys.stderr)
    return 0 if not body.get("failed") else 2


def cmd_share_ls(args: argparse.Namespace) -> int:
    server, headers = get_client(args)
    owner, slug = _parse_wiki_spec(args.wiki)
    resp = api_request("GET", f"{server}/api/v1/wikis/{owner}/{slug}/grants", headers)
    raise_for_api_error(resp)
    body = resp.json()
    if args.json:
        print(json.dumps(body, indent=2))
        return 0
    grants = body.get("grants", [])
    if not grants:
        print("(no grants)")
        return 0
    for g in grants:
        print(f"{g['pattern']:<20} @{g['username']}:{g['role']}")
    return 0


def cmd_share_rm(args: argparse.Namespace) -> int:
    server, headers = get_client(args)
    owner, slug = _parse_wiki_spec(args.wiki)
    exit_code = 0
    for identifier in args.users:
        field = _id_to_grant_field(identifier)
        username = field.get("username")
        if not username and "email" in field:
            # resolve email → username via user search (email prefix matches)
            r = api_request("GET", f"{server}/api/v1/users/search", headers, params={"q": field["email"]})
            if r.status_code == 200:
                matches = r.json().get("users", [])
                if matches:
                    username = matches[0]["username"]
        if not username:
            print(f"failed  {identifier} — could not resolve to a username", file=sys.stderr)
            exit_code = 2
            continue
        resp = api_request(
            "DELETE", f"{server}/api/v1/wikis/{owner}/{slug}/share", headers,
            json={"pattern": args.pattern, "username": username},
        )
        if resp.status_code >= 400:
            try:
                msg = resp.json().get("message", resp.text)
            except Exception:
                msg = resp.text
            print(f"failed  @{username} — {msg}", file=sys.stderr)
            exit_code = 2
            continue
        body = resp.json()
        if body.get("revoked"):
            print(f"revoked @{username} → {args.pattern}")
        else:
            print(f"skipped @{username} → {args.pattern} (no matching grant)")
    return exit_code


def cmd_mcp_config(args: argparse.Namespace) -> int:
    server, headers = get_client(args, require_auth=False)
    # Try to pull credentials for the Authorization header
    prof = load_profile(resolve_profile_name(args.profile))
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
    # Default is None so get_client() / resolve_profile_name() can fall back
    # to the _active profile when --profile is not explicitly passed.
    p.add_argument("--profile", default=None, help="credentials profile (default: active, else 'default')")
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

    # gh-style multi-account auth group
    auth = sub.add_parser(
        "auth",
        help="manage multiple accounts (gh-style)",
        description=(
            "gh-style multi-account auth. Use `auth login` to add accounts without "
            "overwriting existing ones; `auth switch` to change the active profile."
        ),
    )
    auth_sub = auth.add_subparsers(dest="auth_cmd", required=True)

    al = auth_sub.add_parser(
        "login",
        help="add a new account (or update an existing one); become active",
        description=(
            "Authenticate and save credentials. If --profile is omitted, a name "
            "is chosen automatically ('default' if unused, else '<username>@<host>')."
        ),
    )
    al.add_argument("--username")
    al.add_argument("--password")
    al.add_argument("--save-api-key", dest="api_key", help="save this existing key after verifying")
    al.add_argument("--signup", action="store_true", help="create a new account instead of logging in")
    al.add_argument("--email", help="(with --signup) email for new account")
    al.add_argument("--display-name", help="(with --signup) display name for new account")
    al.set_defaults(func=cmd_auth_login)

    asw = auth_sub.add_parser("switch", help="set the active profile")
    asw.add_argument("target_profile", metavar="profile", help="profile name to make active")
    asw.set_defaults(func=cmd_auth_switch)

    ast = auth_sub.add_parser("status", help="list all profiles and mark the active one")
    ast.set_defaults(func=cmd_auth_status)

    als = auth_sub.add_parser("list", help="list profile names (one per line; active marked with *)")
    als.set_defaults(func=cmd_auth_list)

    alo = auth_sub.add_parser("logout", help="remove a profile (default: the active one)")
    alo.add_argument("target_profile", nargs="?", metavar="profile",
                     help="profile name to remove (default: active profile)")
    alo.set_defaults(func=cmd_auth_logout)

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

    s = sub.add_parser("share", help="manage collaborators on a wiki")
    share_sub = s.add_subparsers(dest="share_cmd", required=True)

    sa = share_sub.add_parser("add", help="grant one or more users access to a wiki")
    sa.add_argument("wiki", help="owner/slug")
    sa.add_argument("users", nargs="+", help="usernames or emails")
    sa.add_argument("--role", choices=["read", "edit"], default="read")
    sa.add_argument("--pattern", default="*", help="path pattern (default: '*' = whole wiki)")
    sa.set_defaults(func=cmd_share_add)

    sl = share_sub.add_parser("ls", help="list current grants on a wiki")
    sl.add_argument("wiki", help="owner/slug")
    sl.set_defaults(func=cmd_share_ls)

    sr = share_sub.add_parser("rm", help="revoke one or more users from a wiki")
    sr.add_argument("wiki", help="owner/slug")
    sr.add_argument("users", nargs="+", help="usernames or emails")
    sr.add_argument("--pattern", default="*", help="path pattern to revoke (default: '*')")
    sr.set_defaults(func=cmd_share_rm)

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
