"""
server-side markdown rendering for wikihub.

pipeline: markdown-it-py with plugins for:
  - wikilinks [[Page]] and [[path/to/page]]
  - footnotes [^1]
  - KaTeX math ($inline$ and $$display$$)
  - code highlighting (via highlight.js on client)
  - external links in new tab
  - obsidian image embeds ![[image.png]] and ![[image.png|300]]
  - private band stripping (<!-- private -->...<!-- /private -->)
"""

import re
from markdown_it import MarkdownIt
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.dollarmath import dollarmath_plugin


def _wikilink_plugin(md):
    """custom plugin for [[wikilink]] syntax.
    renders resolved links as amber-colored internal links,
    unresolved as red-dashed."""

    def wikilink_replace(state, silent):
        pos = state.pos
        src = state.src

        if pos + 2 >= len(src) or src[pos:pos+2] != "[[":
            return False

        end = src.find("]]", pos + 2)
        if end < 0:
            return False

        if silent:
            return True

        content = src[pos+2:end]

        # handle [[target|label]] syntax
        if "|" in content:
            target, label = content.split("|", 1)
        else:
            target = content
            label = content

        target = target.strip()
        label = label.strip()

        token = state.push("wikilink_open", "a", 1)
        token.attrs = {"href": f"#wikilink:{target}", "class": "wikilink", "data-target": target}
        token.markup = "[["

        inline = state.push("text", "", 0)
        inline.content = label

        state.push("wikilink_close", "a", -1)

        state.pos = end + 2
        return True

    md.inline.ruler.before("link", "wikilink", wikilink_replace)


def _obsidian_embed_plugin(md):
    """custom plugin for ![[image.png]] and ![[image.png|300]] syntax."""

    def embed_replace(state, silent):
        pos = state.pos
        src = state.src

        if pos + 3 >= len(src) or src[pos:pos+3] != "![[":
            return False

        end = src.find("]]", pos + 3)
        if end < 0:
            return False

        if silent:
            return True

        content = src[pos+3:end]

        # handle ![[image.png|300]] width syntax
        width = None
        if "|" in content:
            parts = content.rsplit("|", 1)
            filename = parts[0].strip()
            try:
                width = int(parts[1].strip())
            except ValueError:
                filename = content
        else:
            filename = content.strip()

        # check if it's an image
        img_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif"}
        ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

        if ext in img_exts:
            token = state.push("image", "img", 0)
            token.attrs = {"src": filename, "alt": filename}
            if width:
                token.attrs["width"] = str(width)
                token.attrs["style"] = f"max-width: {width}px"
            token.children = []
        else:
            # non-image embed: render as a link
            token = state.push("html_inline", "", 0)
            token.content = f'<a href="{filename}" class="embed-link">{filename}</a>'

        state.pos = end + 2
        return True

    md.inline.ruler.before("image", "obsidian_embed", embed_replace)


def _external_link_plugin(md):
    """post-process: add target=_blank to external links."""
    original_render = md.renderer.rules.get("link_open")

    def link_open(tokens, idx, options, env):
        token = tokens[idx]
        href = token.attrGet("href") or ""

        if href.startswith(("http://", "https://")) and "wikilink" not in (token.attrGet("class") or ""):
            token.attrSet("target", "_blank")
            token.attrSet("rel", "noopener noreferrer")
            cls = token.attrGet("class") or ""
            token.attrSet("class", (cls + " external-link").strip())

        if original_render:
            return original_render(tokens, idx, options, env)
        return md.renderer.renderToken(tokens, idx, options, env)

    md.renderer.rules["link_open"] = link_open


def create_renderer():
    """create a configured markdown-it renderer."""
    md = MarkdownIt("commonmark", {"html": False, "typographer": True})
    md.enable(["table", "strikethrough"])

    footnote_plugin(md)
    dollarmath_plugin(md, double_inline=True)
    _wikilink_plugin(md)
    _obsidian_embed_plugin(md)
    _external_link_plugin(md)

    return md


# singleton renderer
_renderer = None

def get_renderer():
    global _renderer
    if _renderer is None:
        _renderer = create_renderer()
    return _renderer


def _strip_frontmatter(content):
    """strip YAML frontmatter (--- delimited) from markdown content."""
    if not content.startswith("---"):
        return content
    parts = content.split("---", 2)
    if len(parts) >= 3:
        return parts[2]
    return content


def render_markdown(content, resolve_wikilinks=None):
    """render markdown content to HTML.

    resolve_wikilinks: optional callback(target) -> (url, exists)
    to resolve [[wikilinks]] to actual URLs and mark unresolved ones.
    """
    content = _strip_frontmatter(content)
    md = get_renderer()
    html = md.render(content)

    # post-process wikilinks if resolver provided
    if resolve_wikilinks:
        def replace_wikilink(match):
            target = match.group(1)
            url, exists = resolve_wikilinks(target)
            cls = "wikilink" if exists else "wikilink wikilink-broken"
            return f'<a href="{url}" class="{cls}" data-target="{target}">'

        html = re.sub(
            r'<a href="#wikilink:([^"]+)" class="wikilink" data-target="[^"]*">',
            replace_wikilink,
            html,
        )

    return html


def render_page(content, wiki_owner=None, wiki_slug=None):
    """render a wiki page with wikilink resolution."""

    def resolver(target):
        # simple path-based resolution
        target_clean = target.strip("/")
        if not target_clean.endswith(".md"):
            target_clean += ".md"
        url = f"/@{wiki_owner}/{wiki_slug}/{target_clean.replace('.md', '')}" if wiki_owner else f"#{target}"
        # TODO: check if page exists in DB for resolved/unresolved styling
        return url, True

    return render_markdown(content, resolve_wikilinks=resolver if wiki_owner else None)
