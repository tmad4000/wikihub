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
from mdit_py_plugins.anchors import anchors_plugin

from app.content_utils import parse_markdown_document


def _heading_slug(title):
    """generate a URL-friendly slug from heading text."""
    slug = re.sub(r'[^\w\s-]', '', title.lower()).strip()
    slug = re.sub(r'[-\s]+', '-', slug)
    return slug


def extract_toc(html):
    """extract table of contents entries from rendered HTML headings.
    returns list of (level, id, text) tuples for h2-h4 elements."""
    toc = []
    for match in re.finditer(r'<h([2-4])\s+id="([^"]+)"[^>]*>(.*?)</h\1>', html, re.DOTALL):
        level = int(match.group(1))
        heading_id = match.group(2)
        text = re.sub(r'<[^>]+>', '', match.group(3)).strip()
        toc.append((level, heading_id, text))
    return toc


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


def _figure_image_plugin(md):
    original_paragraph_open = md.renderer.rules.get("paragraph_open")
    original_paragraph_close = md.renderer.rules.get("paragraph_close")

    def paragraph_open(tokens, idx, options, env):
        if idx + 2 < len(tokens) and tokens[idx + 1].type == "inline":
            children = tokens[idx + 1].children or []
            if children and all(child.type == "image" for child in children):
                return "<figure>\n"
        if original_paragraph_open:
            return original_paragraph_open(tokens, idx, options, env)
        return md.renderer.renderToken(tokens, idx, options, env)

    def paragraph_close(tokens, idx, options, env):
        if idx > 0 and tokens[idx - 1].type == "inline":
            children = tokens[idx - 1].children or []
            if children and all(child.type == "image" for child in children):
                return "</figure>\n"
        if original_paragraph_close:
            return original_paragraph_close(tokens, idx, options, env)
        return md.renderer.renderToken(tokens, idx, options, env)

    md.renderer.rules["paragraph_open"] = paragraph_open
    md.renderer.rules["paragraph_close"] = paragraph_close


def _highlight_fence_plugin(md):
    def fence_renderer(tokens, idx, options, env):
        token = tokens[idx]
        parts = (token.info or "").strip().split()
        info = parts[0] if parts else ""
        lang_class = f" language-{info}" if info else ""
        content = md.utils.escapeHtml(token.content)
        return f'<pre><code class="hljs{lang_class}">{content}</code></pre>\n'

    md.renderer.rules["fence"] = fence_renderer


def create_renderer():
    """create a configured markdown-it renderer."""
    md = MarkdownIt("commonmark", {"html": False, "typographer": True})
    md.enable(["table", "strikethrough"])

    footnote_plugin(md)
    dollarmath_plugin(md, double_inline=True)
    anchors_plugin(md, permalink=False, slug_func=_heading_slug)
    _wikilink_plugin(md)
    _obsidian_embed_plugin(md)
    _external_link_plugin(md)
    _figure_image_plugin(md)
    _highlight_fence_plugin(md)

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
    _, body = parse_markdown_document(content)
    return body


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
    from app.models import Page, User, Wiki

    known_pages = {}
    title_aliases = {}
    if wiki_owner and wiki_slug:
        pages = (
            Page.query.join(Wiki, Page.wiki_id == Wiki.id)
            .join(User, Wiki.owner_id == User.id)
            .filter(User.username == wiki_owner, Wiki.slug == wiki_slug)
            .all()
        )
        known_pages = {page.path: page for page in pages}
        for page in pages:
            title_aliases[page.title] = page

    def resolver(target):
        # simple path-based resolution
        target_clean = target.strip("/")
        if not target_clean.endswith(".md"):
            target_clean += ".md"
        from urllib.parse import quote
        url = f"/@{wiki_owner}/{wiki_slug}/{quote(target_clean.replace('.md', ''), safe='/')}" if wiki_owner else f"#{target}"
        matched = known_pages.get(target_clean) or known_pages.get(target) or title_aliases.get(target)
        return url, matched is not None

    return render_markdown(content, resolve_wikilinks=resolver if wiki_owner else None)
