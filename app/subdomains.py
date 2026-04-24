"""subdomain reservations and resolution.

Every user's username implicitly claims <username>.wikihub.md as their profile
subdomain. Each wiki may optionally claim a globally-unique <subdomain>.wikihub.md.
Both share the reserved-word namespace below, so usernames and wiki subdomains
must not collide with reserved words or with each other.
"""

import re
from typing import Optional, Tuple

from app import db
from app.models import User, Wiki

# canonical host suffix. requests arriving at other hosts (the legacy
# wikihub.globalbr.ai deploy host still redirects here; *.localhost, etc.)
# fall through to the main app.
CANONICAL_SUFFIX = ".wikihub.md"
LOCAL_SUFFIX = ".wikihub.localhost"  # for local dev via caddy

# Reserved names that ALSO route to a system user's profile subdomain when hit.
# These users are auto-created by the app (see wiki_ops.ensure_official_wiki)
# and get the nicer <name>.wikihub.md URL even though users can't claim <name>
# as a wiki subdomain or username.
SYSTEM_SUBDOMAIN_USERS = frozenset({"wikihub"})


RESERVED_SUBDOMAINS = frozenset({
    # infrastructure
    "www", "api", "app", "admin", "staging", "dev", "test", "prod",
    "mail", "ftp", "ssh", "vpn", "cdn", "static", "assets", "media",
    "ns", "ns1", "ns2", "mx",
    # product surfaces
    "wikihub", "wiki", "help", "docs", "blog", "about", "status",
    "community", "explore", "discover", "trending", "popular",
    "settings", "account", "profile", "dashboard", "home",
    "login", "logout", "register", "signup", "signin", "auth",
    "new", "search", "agents", "api-docs", "support", "contact",
    # collab / future
    "collab", "realtime", "ws", "sockets", "notify", "notifications",
    "billing", "pricing", "plans", "upgrade", "payment",
    # common reserved
    "root", "null", "undefined", "test1", "test2",
    "localhost", "example", "security", "abuse", "privacy", "terms", "legal",
    # wikihub conventions
    "well-known",
})

SUBDOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def is_valid_subdomain_format(value: str) -> bool:
    """rfc-compliant dns label: 1-63 chars, alphanum + hyphen, no leading/trailing hyphen."""
    if not value or len(value) > 63:
        return False
    return bool(SUBDOMAIN_RE.match(value))


def is_reserved(value: str) -> bool:
    return value.lower() in RESERVED_SUBDOMAINS


def username_conflicts(value: str, exclude_user_id: Optional[int] = None) -> bool:
    """true if `value` is taken as a username."""
    q = User.query.filter(db.func.lower(User.username) == value.lower())
    if exclude_user_id is not None:
        q = q.filter(User.id != exclude_user_id)
    return db.session.query(q.exists()).scalar()


def wiki_subdomain_conflicts(value: str, exclude_wiki_id: Optional[int] = None) -> bool:
    """true if `value` is taken as a wiki subdomain."""
    q = Wiki.query.filter(db.func.lower(Wiki.subdomain) == value.lower())
    if exclude_wiki_id is not None:
        q = q.filter(Wiki.id != exclude_wiki_id)
    return db.session.query(q.exists()).scalar()


def validate_username(value: str, exclude_user_id: Optional[int] = None) -> Optional[str]:
    """returns an error message if invalid, else None.
    used on registration and username change."""
    if is_reserved(value):
        return f"'{value}' is a reserved subdomain name"
    if wiki_subdomain_conflicts(value):
        return f"'{value}' is already claimed as a wiki subdomain"
    return None


def validate_wiki_subdomain(value: str, exclude_wiki_id: Optional[int] = None) -> Optional[str]:
    """returns an error message if invalid, else None."""
    if not is_valid_subdomain_format(value):
        return "Subdomain must be 1-63 chars: lowercase letters, numbers, or hyphens"
    if is_reserved(value):
        return f"'{value}' is a reserved subdomain name"
    if username_conflicts(value):
        return f"'{value}' is already a username"
    if wiki_subdomain_conflicts(value, exclude_wiki_id=exclude_wiki_id):
        return f"'{value}' is already claimed by another wiki"
    return None


def resolve_host(host: str) -> Optional[Tuple[str, str]]:
    """given a request Host header, return (kind, name) if it's a recognized
    subdomain, else None.

    kind is "user" or "wiki"; name is the matching username or wiki subdomain.
    returns None for the bare apex (wikihub.md, www.wikihub.md), reserved
    subdomains, or unknown hosts — those fall through to the main app.
    """
    if not host:
        return None
    host = host.lower().split(":")[0]  # strip port
    suffix = None
    if host.endswith(CANONICAL_SUFFIX):
        suffix = CANONICAL_SUFFIX
    elif host.endswith(LOCAL_SUFFIX):
        suffix = LOCAL_SUFFIX
    else:
        return None

    label = host[: -len(suffix)]
    if not label or label == "www":
        return None
    # sub-subdomains (foo.bar.wikihub.md) not supported in MVP
    if "." in label:
        return None

    # System users (e.g. @wikihub) get their profile subdomain even though
    # the label is otherwise reserved. Claim-blocking still applies: no one
    # can register the username or claim it as a wiki subdomain.
    if label in SYSTEM_SUBDOMAIN_USERS:
        user = User.query.filter(db.func.lower(User.username) == label).first()
        if user:
            return ("user", user.username)
        return None

    if is_reserved(label):
        return None

    user = User.query.filter(db.func.lower(User.username) == label).first()
    if user:
        return ("user", user.username)
    wiki = Wiki.query.filter(db.func.lower(Wiki.subdomain) == label).first()
    if wiki:
        return ("wiki", wiki.subdomain)
    return None
