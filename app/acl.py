"""
.wikihub/acl parser — CODEOWNERS-pattern access control.

glob rules, most-specific wins, private by default.

precedence (most specific wins):
  1. frontmatter on the file
  2. ACL file rule matching the path (most-specific pattern first)
  3. repo default (* private, implicit)
"""

import fnmatch
import re

VALID_VISIBILITIES = {"private", "public", "public-view", "public-edit", "unlisted", "unlisted-view", "unlisted-edit"}

# normalize old names → new names
VISIBILITY_ALIASES = {"public": "public-view", "unlisted": "unlisted-view"}


def normalize_visibility(vis):
    """normalize visibility: accept old names, return canonical form."""
    if not vis:
        return vis
    return VISIBILITY_ALIASES.get(vis, vis)


# page-level visibility enum — the values a Page row / page frontmatter may hold.
# ACL-file directives additionally include the `-view`/`-edit` split and old
# aliases; those must be collapsed to this enum before being stored on a page or
# written into frontmatter (otherwise ACL tokens like `unlisted-view` leak into
# page frontmatter — see wikihub issue #15).
PAGE_VISIBILITIES = {"public", "public-edit", "private", "unlisted"}

_PAGE_VISIBILITY_MAP = {
    "public": "public",
    "public-view": "public",
    "public-edit": "public-edit",
    "private": "private",
    "unlisted": "unlisted",
    "unlisted-view": "unlisted",
    "unlisted-edit": "unlisted",
}


def normalize_page_visibility(vis):
    """Coerce any visibility token (ACL directive, old alias, or frontmatter
    value) to the page-level enum {public, public-edit, private, unlisted}.

    Returns None for unrecognized values so callers can ignore them and fall
    back to the ACL default. Used to validate/normalize frontmatter visibility
    so ACL-only tokens (e.g. `unlisted-view`) never get persisted on a page."""
    if not vis:
        return None
    return _PAGE_VISIBILITY_MAP.get(str(vis).strip().lower())


GRANT_RE = re.compile(r"^@([\w-]+):(read|edit)$")


def parse_acl(text):
    """parse a .wikihub/acl file into a list of (pattern, directive) tuples.
    directives are either a visibility string or a grant like '@user:read'.
    rules are returned in file order; resolution uses most-specific-wins."""
    rules = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        pattern = parts[0]
        for directive in parts[1:]:
            directive_lower = directive.lower()
            if directive_lower in VALID_VISIBILITIES:
                rules.append((pattern, directive_lower))
            elif GRANT_RE.match(directive):
                rules.append((pattern, directive))
            # unknown directives are logged as warnings but don't break the file
    return rules


def validate_acl(text):
    errors = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            errors.append(f"line {line_number}: ACL rule must include a pattern and at least one directive")
            continue
        for directive in parts[1:]:
            directive_lower = directive.lower()
            if directive_lower in VALID_VISIBILITIES:
                continue
            if directive.startswith("@") and not GRANT_RE.match(directive):
                errors.append(f"line {line_number}: malformed grant '{directive}'")
    return errors


def parse_serve_inline(text):
    """parse a .wikihub/serve-inline file into a list of glob patterns.

    This is the OWNER OPT-IN for serving stored .html (and other otherwise-
    download-only files) INLINE with their natural Content-Type, instead of as
    a forced `attachment` download. One glob pattern per line; '#' comments and
    blank lines ignored. Same fnmatch globbing as .wikihub/acl.

    Security: serving attacker-controlled HTML from the wiki origin is a
    stored-XSS risk. .wikihub/ files are plumbing only the wiki owner can write
    (via git push or authenticated API; they are skipped during page indexing),
    so listing a path here is an explicit, owner-authored opt-in. Inline serving
    is additionally hardened in the route with a CSP sandbox + nosniff.
    """
    patterns = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # only the first whitespace-delimited token is the pattern
        patterns.append(line.split()[0])
    return patterns


def matches_serve_inline(path, patterns):
    """True if `path` matches any opt-in glob in a parsed serve-inline list."""
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _pattern_specificity(pattern):
    """score a glob pattern by specificity. more specific = higher score.
    exact paths > deep globs > shallow globs > wildcards."""
    if "*" not in pattern and "?" not in pattern:
        return 1000 + len(pattern)  # exact path
    depth = pattern.count("/")
    wildcard_penalty = pattern.count("*") + pattern.count("?")
    return depth * 100 + len(pattern) - wildcard_penalty


def resolve_visibility(path, rules, frontmatter_visibility=None):
    """resolve visibility for a file path given ACL rules and optional frontmatter.
    frontmatter wins over ACL (most specific).
    among ACL rules, most-specific matching pattern wins."""
    if frontmatter_visibility and frontmatter_visibility in VALID_VISIBILITIES:
        return frontmatter_visibility

    matching = []
    for pattern, directive in rules:
        if directive not in VALID_VISIBILITIES:
            continue  # skip grants for visibility resolution
        if fnmatch.fnmatch(path, pattern):
            matching.append((pattern, directive))

    if not matching:
        return "private"  # repo default

    # most-specific pattern wins
    matching.sort(key=lambda x: _pattern_specificity(x[0]))
    return matching[-1][1]


def resolve_grants(path, rules):
    """resolve per-user grants for a file path.
    returns a list of (username, role) tuples."""
    grants = []
    for pattern, directive in rules:
        m = GRANT_RE.match(directive)
        if not m:
            continue
        if fnmatch.fnmatch(path, pattern):
            grants.append((m.group(1), m.group(2)))
    return grants


def can_read(path, rules, user=None, frontmatter_visibility=None):
    """check if a user can read a file."""
    # resolve_visibility may return either old (`unlisted`) or canonical
    # (`unlisted-view`) forms depending on whether the value came from an ACL
    # file, frontmatter, or a stored Page row. Normalize so both are honored —
    # otherwise anonymous link-holders 404 on ACL-defaulted pages (issue #15).
    vis = normalize_visibility(resolve_visibility(path, rules, frontmatter_visibility))

    if vis in ("public-view", "public-edit"):
        return True
    if vis in ("unlisted-view", "unlisted-edit"):
        return True  # accessible by URL

    # private — check grants
    if user:
        grants = resolve_grants(path, rules)
        for username, role in grants:
            if username == user:
                return True

    return False


def can_write(path, rules, user=None, frontmatter_visibility=None):
    """check if a user can write to a file."""
    vis = normalize_visibility(resolve_visibility(path, rules, frontmatter_visibility))

    if vis in ("public-edit", "unlisted-edit"):
        return True  # anonymous writes allowed

    # check grants
    if user:
        grants = resolve_grants(path, rules)
        for username, role in grants:
            if username == user and role == "edit":
                return True

    return False


def is_discoverable(path, rules, frontmatter_visibility=None):
    """check if a file appears in listings/search."""
    vis = normalize_visibility(resolve_visibility(path, rules, frontmatter_visibility))
    return vis in ("public-view", "public-edit")


def list_all_grants(rules):
    """extract all grants from parsed rules.
    returns a list of (pattern, username, role) tuples."""
    grants = []
    for pattern, directive in rules:
        m = GRANT_RE.match(directive)
        if m:
            grants.append((pattern, m.group(1), m.group(2)))
    return grants


def grants_for_user(rules, username):
    """return all grants for a specific user.
    returns a list of (pattern, role) tuples."""
    result = []
    for pattern, directive in rules:
        m = GRANT_RE.match(directive)
        if m and m.group(1) == username:
            result.append((pattern, m.group(2)))
    return result


def remove_grant(acl_text, pattern, username):
    """remove a specific user's grant(s) from raw ACL text for a given pattern.
    handles multi-directive lines: 'research/* @alice:read @bob:edit'
    removing alice produces 'research/* @bob:edit'.
    returns the modified ACL text."""
    out_lines = []
    for line in acl_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out_lines.append(line)
            continue
        parts = stripped.split()
        if len(parts) < 2:
            out_lines.append(line)
            continue
        line_pattern = parts[0]
        if line_pattern != pattern:
            out_lines.append(line)
            continue
        # filter out grants for this username
        kept = [line_pattern]
        for directive in parts[1:]:
            m = GRANT_RE.match(directive)
            if m and m.group(1) == username:
                continue  # drop this grant
            kept.append(directive)
        if len(kept) > 1:  # pattern + at least one directive
            out_lines.append(" ".join(kept))
        # else: line had only grants for this user, drop it entirely
    return "\n".join(out_lines) + ("\n" if acl_text.endswith("\n") else "")
