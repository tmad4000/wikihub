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

VALID_VISIBILITIES = {"private", "public", "public-edit", "unlisted", "unlisted-edit"}
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
    vis = resolve_visibility(path, rules, frontmatter_visibility)

    if vis in ("public", "public-edit"):
        return True
    if vis in ("unlisted", "unlisted-edit"):
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
    vis = resolve_visibility(path, rules, frontmatter_visibility)

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
    vis = resolve_visibility(path, rules, frontmatter_visibility)
    return vis in ("public", "public-edit")
