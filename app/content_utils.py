import re
from collections.abc import Iterable

import frontmatter
from markdown_it import MarkdownIt

from app.acl import VALID_VISIBILITIES, normalize_visibility


PRIVATE_OPEN_RE = re.compile(r"<!--\s*private\s*-->", re.IGNORECASE)
PRIVATE_CLOSE_RE = re.compile(r"<!--\s*/private\s*-->", re.IGNORECASE)
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
    normalized = (visibility or "").strip().lower() or None
    if normalized and normalized not in VALID_VISIBILITIES:
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


def _line_offsets(text):
    offsets = [0]
    total = 0
    for line in text.splitlines(keepends=True):
        total += len(line)
        offsets.append(total)
    if not text.endswith("\n"):
        offsets[-1] = len(text)
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


def has_private_bands(content):
    _, body = split_frontmatter(content)
    return any(kind == "open" for _, _, kind in _scan_private_markers(body))


def strip_private_bands(content):
    frontmatter_prefix, body = split_frontmatter(content)
    markers = _scan_private_markers(body)
    if not markers:
        return content

    result = []
    cursor = 0
    in_private = False

    for start, end, kind in markers:
        if kind == "open" and not in_private:
            result.append(body[cursor:start])
            cursor = end
            in_private = True
        elif kind == "close" and in_private:
            cursor = end
            in_private = False

    if not in_private:
        result.append(body[cursor:])

    stripped_body = "".join(result)
    if frontmatter_prefix:
        separator = "\n" if stripped_body and not stripped_body.startswith("\n") else ""
        return f"{frontmatter_prefix}{separator}{stripped_body}"
    return stripped_body
