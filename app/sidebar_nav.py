"""Custom sidebar order for a wiki: `.wikihub/sidebar.md` (wikihub-hnfm).

Pure module (no DB, no git). `_build_sidebar_tree` builds the automatic,
viewer-filtered tree first; `apply_nav` then rearranges THAT tree according to
the parsed file. Because every page/folder in the result comes from the already
visibility-filtered tree, an entry the viewer cannot read (or a stale target)
is simply dropped together with its label, and a group left with no visible
rows is dropped together with its heading — nothing in the file leaks.

Format (plain Markdown, agent-writable in one page write):

    # Start here                 <- H1-H6 = group heading
    - [[index|Overview]]         <- wikilink, optional |label
    - guides/                    <- folder: its subtree (auto order inside)
      - [[guides/advanced]]      <- nested bullet = placed first inside the folder
    # Reference
    - [Glossary](glossary.md)    <- markdown link, path relative to the wiki root
    - ...                        <- "rest": every visible page/folder not listed
    - [Site](https://example.com)  <- external link (http/https only)

Pages that are not listed are never hidden: they land where `...` sits, or at
the end under a divider ("Other pages" when the file has groups). Unlisted
`pinned: true` pages still float to the very top. Every row of a custom tree
carries a sequential `order` so the client "Custom" sort reproduces the server
order exactly (wikihub-9c8j).
"""

import posixpath
import re
from urllib.parse import unquote

SIDEBAR_FILE_PATH = ".wikihub/sidebar.md"
MAX_DEPTH = 4

# Linear-time patterns only (review: a lazy group followed by \s*…$ was
# O(N^2)/O(N^3) on long whitespace runs, and this file is parsed on every
# anonymous sidebar build). Lines are rstripped before matching and trailing
# '#'s are trimmed in Python.
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*)$")
_LIST_RE = re.compile(r"^([ \t]*)[-*+][ \t]+(.*)$")
MAX_FILE_BYTES = 64 * 1024
MAX_LINE_CHARS = 500
MAX_ENTRIES = 2000
_WIKILINK_RE = re.compile(r"^\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_MDLINK_RE = re.compile(r"^\[([^\]]*)\]\(\s*<?([^)\s>]+)>?\s*\)")
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_REST_TOKENS = {"...", "…"}


def parse_sidebar_md(text):
    """Parse sidebar.md into nodes: group / entry / external / rest."""
    top = []
    container = top
    stack = []  # (level, node) for the list items of the current container
    text = (text or "")
    if len(text.encode("utf-8", "replace")) > MAX_FILE_BYTES:
        text = text.encode("utf-8", "replace")[:MAX_FILE_BYTES].decode("utf-8", "ignore")
    entries = 0
    for raw in text.splitlines():
        raw = raw.rstrip()
        if not raw or len(raw) > MAX_LINE_CHARS:
            continue
        entries += 1
        if entries > MAX_ENTRIES:
            break
        heading = _HEADING_RE.match(raw)
        if heading:
            label = heading.group(2).rstrip("#").rstrip()
            if not label:
                continue
            group = {"type": "group", "label": label, "children": []}
            top.append(group)
            container = group["children"]
            stack = []
            continue
        item = _LIST_RE.match(raw)
        if not item:
            continue
        indent = len(item.group(1).replace("\t", "  "))
        level = min(indent // 2, MAX_DEPTH - 1)
        node = _parse_entry(item.group(2).strip())
        if node is None:
            continue
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            stack[-1][1].setdefault("children", []).append(node)
        else:
            container.append(node)
        stack.append((level, node))
    return top


def _parse_entry(body):
    body = body.strip()
    if body in _REST_TOKENS:
        return {"type": "rest"}
    m = _WIKILINK_RE.match(body)
    if m:
        target = m.group(1).strip()
        label = (m.group(2) or "").strip() or None
        if target.startswith("@"):
            # Cross-wiki refs ([[@user/wiki]]) are reserved for phase 2
            # (wikihub-v02); keep the file valid by ignoring them for now.
            return {"type": "ignored", "children": []}
        return {"type": "entry", "target": target, "label": label, "children": []}
    m = _MDLINK_RE.match(body)
    if m:
        label = m.group(1).strip() or None
        href = m.group(2).strip()
        if _SCHEME_RE.match(href):
            if href.lower().startswith(("http://", "https://")):
                return {"type": "external", "url": href, "label": label or href, "children": []}
            return {"type": "ignored", "children": []}
        return {"type": "entry", "target": href, "label": label, "children": []}
    if body.startswith("[") or not body:
        return {"type": "ignored", "children": []}
    return {"type": "entry", "target": body, "label": None, "children": []}


def _index_tree(items, pages, folders, basenames):
    for item in items:
        if item["kind"] == "folder":
            folders[item["path"]] = item
            _index_tree(item.get("children") or [], pages, folders, basenames)
        elif item["kind"] == "page":
            pages[item["path"]] = item
            stem = posixpath.basename(item["path"]).lower()
            if stem.endswith(".md"):
                stem = stem[:-3]
            basenames.setdefault(stem, []).append(item)


def _resolve(target, pages, folders, basenames):
    t = unquote(target).strip()
    t = t.split("#", 1)[0]
    is_folder = t.endswith("/")
    t = posixpath.normpath("/" + t.strip()).lstrip("/") if t.strip("/") else ""
    if not t or t.startswith(".."):
        return None
    variants = [t]
    if "_" in t:
        variants.append(t.replace("_", " "))
    for v in variants:
        if is_folder:
            if v in folders:
                return folders[v]
            continue
        for candidate in (v, v if v.endswith(".md") else v + ".md"):
            if candidate in pages:
                return pages[candidate]
        if v in folders:
            return folders[v]
    if is_folder or "/" in t:
        return None
    stem = t.lower()
    if stem.endswith(".md"):
        stem = stem[:-3]
    matches = basenames.get(stem) or basenames.get(stem.replace("_", " ")) or []
    return matches[0] if len(matches) == 1 else None


def _collect_explicit(nodes, pages, folders, basenames, out):
    for node in nodes:
        if node["type"] == "entry":
            item = _resolve(node["target"], pages, folders, basenames)
            if item is not None:
                node["_item"] = item
                out.add((item["kind"], item["path"]))
        _collect_explicit(node.get("children") or [], pages, folders, basenames, out)


def _prune(items, exclude):
    """Copy `items` without rows in `exclude`; drop folders left empty."""
    result = []
    for item in items:
        if (item["kind"], item["path"]) in exclude:
            continue
        if item["kind"] == "folder":
            children = _prune(item.get("children") or [], exclude)
            if not children:
                continue
            item = dict(item, children=children)
        result.append(dict(item))
    return result


def _has_rows(items):
    return any(item["kind"] in {"page", "folder", "external"} for item in items)


def apply_nav(nodes, tree):
    """Rearrange the visible auto `tree` by parsed sidebar `nodes`.

    Returns (items, applied). `applied` is False when the file contributed
    nothing renderable for this viewer (the caller then keeps today's tree,
    including no Custom sort button).
    """
    pages, folders, basenames = {}, {}, {}
    _index_tree(tree, pages, folders, basenames)
    explicit = set()
    _collect_explicit(nodes, pages, folders, basenames, explicit)
    has_external = _any_type(nodes, "external")
    if not explicit and not has_external:
        return tree, False

    rest = _prune(tree, explicit)
    for item in rest:
        item["rest"] = True
    pinned_top = [item for item in rest if item.get("pinned")]
    rest = [item for item in rest if not item.get("pinned")]

    placed = set()
    state = {"rest_placed": False}

    def build(children):
        out = []
        for node in children:
            kind = node["type"]
            if kind == "rest":
                if not state["rest_placed"]:
                    state["rest_placed"] = True
                    out.extend(rest)
                continue
            if kind == "group":
                group_children = build(node["children"])
                if _has_rows(group_children):
                    out.append({"kind": "group", "name": node["label"], "path": "", "children": group_children})
                continue
            if kind == "external":
                out.append({"kind": "external", "name": node["label"], "path": "", "url": node["url"], "children": []})
                out.extend(build(node.get("children") or []))
                continue
            item = node.get("_item")
            key = (item["kind"], item["path"]) if item else None
            if item is None or key in placed:
                # Stale / invisible / duplicate target: drop the row (and its
                # label) but keep any nested entries that do resolve.
                out.extend(build(node.get("children") or []))
                continue
            placed.add(key)
            row = dict(item)
            if node.get("label"):
                row["name"] = node["label"]
            if item["kind"] == "folder":
                nested = build(node.get("children") or [])
                remaining = _prune(item.get("children") or [], explicit)
                row["children"] = nested + remaining
                out.append(row)
            else:
                row["children"] = []
                out.append(row)
                # Pages have no children in the tree: nested entries under a
                # page follow it at the same level.
                out.extend(build(node.get("children") or []))
        return out

    items = pinned_top + build(nodes)
    if not state["rest_placed"] and rest:
        has_groups = any(item["kind"] == "group" for item in items)
        items.append({"kind": "divider", "name": "Other pages" if has_groups else "", "path": "", "children": []})
        items.extend(rest)
    _assign_order(items)
    return items, True


def _any_type(nodes, wanted):
    return any(node["type"] == wanted or _any_type(node.get("children") or [], wanted) for node in nodes)


def _assign_order(items):
    for index, item in enumerate(items):
        item["order"] = index
        if item.get("children"):
            _assign_order(item["children"])


def default_sidebar_md(tree):
    """A starting `.wikihub/sidebar.md` that lists the current tree, so the
    owner's first edit is a reorder rather than a blank page."""
    lines = [
        "<!-- Sidebar order for this wiki. '# Heading' starts a group; '- [[page]]' or",
        "     '- [[page|Label]]' lists a page; '- folder/' inserts a folder; indent two",
        "     spaces to nest; '- ...' marks where every unlisted page goes. -->",
        "",
    ]

    def walk(items, depth):
        for item in items:
            pad = "  " * depth
            if item["kind"] == "folder":
                lines.append(f"{pad}- {item['path']}/")
            elif item["kind"] == "page":
                target = item["path"][:-3] if item["path"].endswith(".md") else item["path"]
                lines.append(f"{pad}- [[{target}]]")
    walk(tree, 0)
    lines.append("- ...")
    return "\n".join(lines) + "\n"
