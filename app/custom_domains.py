"""Validation and DNS ownership checks for externally mapped wiki domains."""

import ipaddress
import re
from urllib.parse import urlparse

import requests

from app import db
from app.models import CustomDomain


_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
VERIFICATION_PREFIX = "wikihub-verification="
ROUTABLE_STATUSES = frozenset({"active"})


def normalize_custom_hostname(value):
    """Return a normalized ASCII hostname or raise ValueError."""
    hostname = str(value or "").strip().lower().rstrip(".")
    if not hostname or len(hostname) > 253:
        raise ValueError("Enter a valid hostname (for example, docs.example.com)")
    if "://" in hostname or "/" in hostname or "@" in hostname:
        raise ValueError("Enter a hostname only, without https:// or a path")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError("IP addresses cannot be used as custom domains")
    labels = hostname.split(".")
    if len(labels) < 2 or any(not _LABEL_RE.fullmatch(label) for label in labels):
        raise ValueError("Custom domains must be valid lowercase DNS hostnames")
    if hostname == "wikihub.md" or hostname.endswith(".wikihub.md"):
        raise ValueError("Use the built-in .wikihub.md subdomain setting instead")
    if labels[-1] in {"localhost", "local", "test", "invalid", "example"}:
        raise ValueError("That hostname cannot be used for a public custom domain")
    return hostname


def custom_hostname_conflicts(hostname, exclude_domain_id=None):
    query = CustomDomain.query.filter(db.func.lower(CustomDomain.hostname) == hostname.lower())
    if exclude_domain_id is not None:
        query = query.filter(CustomDomain.id != exclude_domain_id)
    return db.session.query(query.exists()).scalar()


def verification_name(hostname):
    return f"_wikihub.{hostname}"


def verification_value(token):
    return f"{VERIFICATION_PREFIX}{token}"


def _txt_answers(name, timeout=8):
    """Resolve TXT records through DNS-over-HTTPS.

    A fixed HTTPS endpoint avoids shelling out to platform-specific DNS tools.
    The queried name is already constrained by normalize_custom_hostname().
    """
    response = requests.get(
        "https://cloudflare-dns.com/dns-query",
        params={"name": name, "type": "TXT"},
        headers={"Accept": "application/dns-json"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    answers = payload.get("Answer") or []
    values = []
    for answer in answers:
        if int(answer.get("type", 0)) != 16:
            continue
        value = str(answer.get("data", "")).strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        values.append(value.replace('" "', ""))
    return values


def verify_dns_challenge(domain):
    expected = verification_value(domain.verification_token)
    return expected in _txt_answers(verification_name(domain.hostname))


def resolve_custom_host(host):
    """Return the routable CustomDomain for a request Host header, if any."""
    if not host:
        return None
    hostname = host.lower().split(":", 1)[0].rstrip(".")
    try:
        hostname = normalize_custom_hostname(hostname)
    except ValueError:
        return None
    return CustomDomain.query.filter(
        db.func.lower(CustomDomain.hostname) == hostname,
        CustomDomain.status.in_(ROUTABLE_STATUSES),
        CustomDomain.tls_status == "active",
    ).first()


def is_allowed_sheet_source(url):
    """Only accept HTTPS Google Sheets URLs; no generic remote fetches."""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "docs.google.com"
        and parsed.username is None
        and parsed.password is None
        and parsed.path.startswith("/spreadsheets/d/")
    )
