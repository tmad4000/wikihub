import re
from collections.abc import Iterable

import frontmatter
from markdown_it import MarkdownIt

from app.acl import normalize_page_visibility


# Private bands ("private sections"): owner-only regions inside a page.
#
#   <!-- private -->
#   ...only the wiki owner sees this...
#   <!-- /private -->
#
# Rules (see docs/private-sections.md and AGENTS.md "key invariants"):
# - Markers are case-insensitive and tolerate whitespace inside the comment.
# - Any extra text in the OPEN marker (e.g. ``<!-- private @alice -->``) is
#   still a plain owner-only open marker in v1 — never "less private". This
#   keeps a future per-user grant syntax (wikihub-td1) from being opened by an
#   older server.
# - Markers inside fenced/indented code blocks are literal text.
# - No nesting: a second open inside a band is ignored.
# - An unclosed band is private to the end of the file (fails closed).
# - Frontmatter is never inside a band.
PRIVATE_OPEN_RE = re.compile(r"<!--\s*private(?:\s[\s\S]*?)?-->", re.IGNORECASE)
PRIVATE_CLOSE_RE = re.compile(r"<!--\s*/private\s*-->", re.IGNORECASE)
_RAW_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(?:.*?\r?\n)??(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)", re.DOTALL)
WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(\|([^\]]+))?\]\]")

_SCAN_MD = MarkdownIt("commonmark", {"html": True})


def _normalize_frontmatter_value(key, value):
    if key == "tags":
        if isinstance(value, str):
            raw_tags = [part.strip() for part in value.split(",")]
        elif isinstance(value, Iterable):
            raw_tags = [str(part).strip() for part in value]
        else:
            raw_tags = [str(value).strip()]
        return [tag.lstrip("#") for tag in raw_tags if tag]

    if isinstance(value, str):
        return value.strip()
    return value


def parse_markdown_document(content):
    metadata = {}
    body = content

    if content.startswith("---"):
        try:
            post = frontmatter.loads(content)
            metadata = {
                str(key).strip().lower(): _normalize_frontmatter_value(str(key).strip().lower(), value)
                for key, value in post.metadata.items()
            }
            body = post.content.lstrip("\n")
        except Exception:
            pass  # malformed frontmatter — treat entire content as body

    return metadata, body


# Same pattern the editor's client-side splitFrontmatter() uses, so the server
# and the Visual editor agree on where the YAML block ends (wikihub-k7k5).
_FRONTMATTER_BLOCK_RE = re.compile(r"\A(---\r?\n[\s\S]*?\r?\n---\r?\n?)([\s\S]*)\Z")


def split_frontmatter_block(content):
    """Split raw content into (frontmatter_block_text, body) without re-serializing.

    The block keeps its exact original bytes (including the closing `---` line
    and newline) so re-attaching it reproduces the file verbatim.
    """
    content = content or ""
    match = _FRONTMATTER_BLOCK_RE.match(content)
    if not match:
        return "", content
    return match.group(1), match.group(2)


def strip_injected_frontmatter(body):
    """Drop any leading block that the frontmatter parser would read as metadata.

    Uses python-frontmatter itself (the parser behind parse_markdown_document /
    set_visibility_in_content) as the authority, so YAML comments, quoted or
    indented keys, `--- ` boundaries etc. cannot smuggle metadata past us. A body
    that merely opens with a horizontal rule (no mapping) is left alone.
    """
    for _ in range(5):
        if not body.startswith("---"):
            break
        try:
            post = frontmatter.loads(body)
        except Exception:
            break
        if not post.metadata:
            break
        body = post.content.lstrip("\n")
    return body


def replace_body_keep_frontmatter(current_content, new_body):
    """Return current_content's frontmatter block + new_body.

    Any frontmatter included in new_body is discarded: callers use this where
    the editor must not change frontmatter (suggested edits). The blank-line
    gap between the block and the body is preserved from the current file so
    proposal diffs stay minimal.
    """
    current_fm, current_body = split_frontmatter_block(current_content)
    body = strip_injected_frontmatter((new_body or "").replace("\r\n", "\n"))
    if not current_fm:
        return body
    gap = current_body[: len(current_body) - len(current_body.lstrip("\n"))]
    return current_fm + gap + body.lstrip("\n")


def split_frontmatter(content):
    if not content.startswith("---"):
        return "", content

    post = frontmatter.loads(content)
    prefix = frontmatter.dumps(frontmatter.Post("", **post.metadata))
    if prefix.endswith("\n\n"):
        prefix = prefix[:-1]
    return prefix, post.content.lstrip("\n")


def upsert_frontmatter_value(content, key, value):
    key = key.strip().lower()
    post = frontmatter.loads(content) if content.startswith("---") else frontmatter.Post(content)
    metadata = dict(post.metadata)
    if value is None:
        metadata.pop(key, None)
    else:
        metadata[key] = value
    updated = frontmatter.Post(post.content, **metadata)
    return frontmatter.dumps(updated).replace("\r\n", "\n")


def set_visibility_in_content(content, visibility):
    # Frontmatter carries page-level visibility only. Coerce ACL-file tokens and
    # old aliases (e.g. `unlisted-view`, `public`) down to the page enum so they
    # never leak into page frontmatter, and reject genuinely unknown values
    # (wikihub issue #15).
    raw = (visibility or "").strip().lower() or None
    if raw is None:
        normalized = None
    else:
        normalized = normalize_page_visibility(raw)
        if normalized is None:
            raise ValueError(f"Invalid visibility '{visibility}'")
    return upsert_frontmatter_value(content, "visibility", normalized)


def extract_wikilinks(content):
    _, body = parse_markdown_document(content)
    return [match.group(1).strip() for match in WIKILINK_RE.finditer(body)]


def page_reference_aliases(path, title=None):
    aliases = {path}
    if path.endswith(".md"):
        without_ext = path[:-3]
        aliases.add(without_ext)
        aliases.add(path.rsplit("/", 1)[-1])
        aliases.add(without_ext.rsplit("/", 1)[-1])
    if title:
        aliases.add(title.strip())
    return {alias for alias in aliases if alias}


def rewrite_wikilinks(content, old_aliases, new_path):
    if new_path.endswith(".md"):
        new_target_no_ext = new_path[:-3]
    else:
        new_target_no_ext = new_path

    def replacement(match):
        target = match.group(1).strip()
        if target not in old_aliases:
            return match.group(0)
        label = match.group(3)
        rewritten_target = new_path if target.endswith(".md") else new_target_no_ext
        if label is None:
            return f"[[{rewritten_target}]]"
        return f"[[{rewritten_target}|{label}]]"

    return WIKILINK_RE.sub(replacement, content)


_LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")


def _line_offsets(text):
    """Start offset of every line, using exactly the line breaks markdown-it
    recognises (\r\n, \r, \n). str.splitlines() also splits on \x0b, \x0c,
    \x1c-\x1e, \x85, \u2028 and \u2029, which shifted code-block ranges and let
    a crafted page hide an open marker from the scanner."""
    offsets = [0] + [match.end() for match in _LINE_BREAK_RE.finditer(text)]
    if offsets[-1] != len(text):
        offsets.append(len(text))
    return offsets


def _protected_ranges(body):
    offsets = _line_offsets(body)
    protected = []
    for token in _SCAN_MD.parse(body):
        if token.type not in {"fence", "code_block"} or not token.map:
            continue
        start_line, end_line = token.map
        start = offsets[start_line]
        end = offsets[end_line] if end_line < len(offsets) else len(body)
        protected.append((start, end))
    return protected


def _in_protected_range(index, protected):
    return any(start <= index < end for start, end in protected)


def _scan_private_markers(body):
    protected = _protected_ranges(body)
    markers = []
    for pattern, kind in ((PRIVATE_OPEN_RE, "open"), (PRIVATE_CLOSE_RE, "close")):
        for match in pattern.finditer(body):
            if _in_protected_range(match.start(), protected):
                continue
            markers.append((match.start(), match.end(), kind))
    markers.sort(key=lambda item: item[0])
    return markers


def _split_frontmatter_raw(content):
    """Split frontmatter WITHOUT re-serializing it, so band stripping never
    changes the bytes outside the bands (keeps 3-way merges clean).

    Fails closed: a leading ``---`` that is really a horizontal rule (the
    block is not a YAML mapping) or a block that contains a private marker is
    treated as body, so bands there are still found and stripped."""
    match = _RAW_FRONTMATTER_RE.match(content)
    if not match:
        return "", content
    prefix = content[:match.end()]
    if PRIVATE_OPEN_RE.search(prefix) or PRIVATE_CLOSE_RE.search(prefix):
        return "", content
    inner = _LINE_BREAK_RE.split(prefix, maxsplit=1)[1] if _LINE_BREAK_RE.search(prefix) else ""
    inner = re.sub(r"(?:^|\r?\n|\r)(?:---|\.\.\.)[ \t]*(?:\r?\n|\r)?\Z", "", inner)
    try:
        import yaml
        parsed = yaml.safe_load(inner) if inner.strip() else {}
    except Exception:
        return "", content
    if not isinstance(parsed, dict):
        return "", content
    return prefix, content[match.end():]


def has_private_bands(content):
    if not content:
        return False
    _, body = _split_frontmatter_raw(content)
    return any(kind == "open" for _, _, kind in _scan_private_markers(body))


def count_private_bands(content):
    """Number of active private bands (open markers honoured by the stripper)."""
    if not content:
        return 0
    _, body = _split_frontmatter_raw(content)
    count = 0
    in_private = False
    for _, _, kind in _scan_private_markers(body):
        if kind == "open" and not in_private:
            count += 1
            in_private = True
        elif kind == "close" and in_private:
            in_private = False
    return count


def _band_spans(body):
    """Yield (start, end) character spans of each band in ``body``, markers
    included. An unclosed band runs to the end of the body."""
    spans = []
    start = None
    for m_start, m_end, kind in _scan_private_markers(body):
        if kind == "open" and start is None:
            start = m_start
        elif kind == "close" and start is not None:
            spans.append((start, m_end))
            start = None
    if start is not None:
        spans.append((start, len(body)))
    return spans


def strip_private_bands(content):
    """Remove every private band (markers included). Frontmatter bytes are
    preserved verbatim. Unclosed band = stripped to end of file."""
    if not content:
        return content
    frontmatter_prefix, body = _split_frontmatter_raw(content)
    spans = _band_spans(body)
    if not spans:
        return content

    result = []
    cursor = 0
    for start, end in spans:
        result.append(body[cursor:start])
        cursor = end
    result.append(body[cursor:])
    return frontmatter_prefix + "".join(result)


def content_for_viewer(content, *, viewer_is_owner):
    """Return content exactly as stored for the wiki owner, band-stripped for
    everyone else (ACL grantees and public-edit editors included).

    This is THE choke point: no route may hand repo content to a non-owner
    (response, template, LLM prompt, proposal row, diff, index) without going
    through here. Raises if stripping fails — callers must never fall back to
    the raw content (fail closed)."""
    if content is None or viewer_is_owner:
        return content
    return strip_private_bands(content)


def band_wikilink_targets(content):
    """Wikilink targets that occur ONLY inside private bands (not elsewhere in
    the public part of the page)."""
    all_targets = set(extract_wikilinks(content or ""))
    if not all_targets:
        return set()
    public_targets = set(extract_wikilinks(strip_private_bands(content)))
    return all_targets - public_targets
