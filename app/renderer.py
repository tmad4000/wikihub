"""
server-side markdown rendering for wikihub.

pipeline: markdown-it-py with plugins for:
  - wikilinks [[Page]] and [[path/to/page]]
  - footnotes [^1]
  - KaTeX math ($...$, $$...$$, \\(...\\), \\[...\\], \\begin{equation})
  - \\qty command expansion (physics package compat)
  - code highlighting (via highlight.js on client)
  - external links in new tab
  - obsidian image embeds ![[image.png]] and ![[image.png|300]]
  - private band stripping (<!-- private -->...<!-- /private -->)
"""

import re

from markdown_it import MarkdownIt
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.amsmath import amsmath_plugin
from mdit_py_plugins.anchors import anchors_plugin

from app.content_utils import parse_markdown_document
from markupsafe import escape as _escape


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

        # check file type for appropriate embed rendering
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
            # non-image files (PDFs, etc.): render as a styled file link
            # matches Obsidian behavior — opens in new tab, not inline
            display_name = filename.rsplit("/", 1)[-1]
            token = state.push("html_inline", "", 0)
            token.content = (
                f'<a href="{filename}" class="embed-link file-embed" target="_blank">'
                f'<span class="file-icon">&#128196;</span> {display_name}</a>'
            )

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


def _hardbreak_no_newline_plugin(md):
    """Render hard line breaks as bare '<br>' with no trailing newline.

    Cloudflare's HTML Auto Minify strips '<br>\\n' inside paragraphs (treating
    it as redundant inline whitespace), so rendered pages lose all soft-break
    line breaks. Emitting '<br>' with no newline survives the minifier and
    produces identical visual output (wikihub-eiv7).
    """
    def hardbreak(tokens, idx, options, env):
        return "<br>"

    def softbreak(tokens, idx, options, env):
        # only relevant when breaks=true; mirrors hardbreak in that mode
        if options.get("breaks"):
            return "<br>"
        return "\n"

    md.renderer.rules["hardbreak"] = hardbreak
    md.renderer.rules["softbreak"] = softbreak


def _highlight_fence_plugin(md):
    def fence_renderer(tokens, idx, options, env):
        token = tokens[idx]
        parts = (token.info or "").strip().split()
        info = parts[0] if parts else ""
        lang_class = f" language-{info}" if info else ""
        content = md.utils.escapeHtml(token.content)
        return f'<pre><code class="hljs{lang_class}">{content}</code></pre>\n'

    md.renderer.rules["fence"] = fence_renderer


def _expand_qty(text):
    """expand \\qty(...), \\qty[...], \\qty{...} to \\left...\\right."""
    i = 0
    result = []
    while i < len(text):
        if text[i:i+4] == '\\qty' and (i + 4 >= len(text) or not text[i+4].isalpha()):
            j = i + 4
            while j < len(text) and text[j] == ' ':
                j += 1
            if j < len(text) and text[j] in '([{':
                opener = text[j]
                closer = {'(': ')', '[': ']', '{': '}'}[opener]
                left = {'(': '\\left(', '[': '\\left[', '{': '\\left\\{'}[opener]
                right = {')': '\\right)', ']': '\\right]', '}': '\\right\\}'}[closer]
                depth = 1
                k = j + 1
                while k < len(text) and depth > 0:
                    if text[k] == '\\':
                        k += 2
                        continue
                    if text[k] == opener:
                        depth += 1
                    elif text[k] == closer:
                        depth -= 1
                    k += 1
                if depth == 0:
                    result.append(left)
                    result.append(_expand_qty(text[j+1:k-1]))
                    result.append(right)
                    i = k
                    continue
        result.append(text[i])
        i += 1
    return ''.join(result)


def _preprocess_latex_math(text):
    """normalize LaTeX math delimiters for markdown-it-py.

    markdown-it escapes \\( to (, destroying the math delimiter.
    this converts \\(...\\) → $...$ and \\[...\\] → $$...$$ before parsing,
    and expands \\qty commands to \\left/\\right pairs.
    """
    # protect code blocks from processing
    protected = []
    def protect(m):
        protected.append(m.group(0))
        return f'\x00PROT{len(protected)-1}\x00'
    text = re.sub(r'```[\s\S]*?```|`[^`]+`', protect, text)

    # \\( ... \\) → $ ... $  (inline math)
    text = re.sub(r'\\\((.+?)\\\)', r'$\1$', text)
    # \\[ ... \\] → $$ ... $$  (display math)
    text = re.sub(r'\\\[(.+?)\\\]', r'$$\1$$', text, flags=re.DOTALL)
    # expand \qty commands
    text = _expand_qty(text)

    # restore protected regions
    for i, p in enumerate(protected):
        text = text.replace(f'\x00PROT{i}\x00', p)
    return text


def create_renderer():
    """create a configured markdown-it renderer."""
    # breaks=True renders single newlines inside a paragraph as <br>, matching
    # Obsidian / GitHub-comment behavior. Strict CommonMark would collapse them
    # to spaces, which surprises users writing one-line-per-thought (wikihub-eiv7).
    # xhtmlOut=False emits HTML5 <br> instead of XHTML <br />, because Cloudflare's
    # HTML Auto Minify strips the latter entirely (verified empirically 2026-04-28).
    md = MarkdownIt(
        "commonmark",
        {"html": False, "typographer": True, "breaks": True, "xhtmlOut": False},
    )
    md.enable(["table", "strikethrough"])

    footnote_plugin(md)
    dollarmath_plugin(md, double_inline=True)
    amsmath_plugin(md)
    anchors_plugin(md, permalink=False, max_level=4, slug_func=_heading_slug)
    _wikilink_plugin(md)
    _obsidian_embed_plugin(md)
    _external_link_plugin(md)
    _figure_image_plugin(md)
    _highlight_fence_plugin(md)
    _hardbreak_no_newline_plugin(md)

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
    content = _preprocess_latex_math(content)
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


_LEADING_H1_RE = re.compile(r'\s*<h1(?:\s[^>]*)?>', re.IGNORECASE)


def _prepend_frontmatter_h1(content, html):
    """If frontmatter has a title but the rendered HTML does not start with an h1,
    prepend an <h1> containing the frontmatter title so the rendered page has a
    visible heading. If the body already opens with an h1 (e.g. markdown `# Foo`),
    leave it alone to avoid duplicates."""
    metadata, _ = parse_markdown_document(content)
    fm_title = metadata.get("title")
    if not fm_title or not isinstance(fm_title, str):
        return html
    fm_title = fm_title.strip()
    if not fm_title:
        return html
    if _LEADING_H1_RE.match(html or ""):
        return html
    slug = _heading_slug(fm_title)
    return f'<h1 id="{_escape(slug)}">{_escape(fm_title)}</h1>\n{html}'


def render_page(content, wiki_owner=None, wiki_slug=None, current_page_path=None):
    """render a wiki page with wikilink resolution.
    current_page_path: the page's path in the repo (e.g. 'wiki/courses/cs224n.md')
    used to resolve relative markdown links like ../raw/foo.md"""
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
        basename_pages = {}
        for page in pages:
            if page.title:
                title_aliases[page.title.lower()] = page
            # basename lookup: "page-name" and "page-name.md" both resolve
            bname = page.path.rsplit("/", 1)[-1]
            basename_pages.setdefault(bname.lower(), page)
            if bname.endswith(".md"):
                basename_pages.setdefault(bname[:-3].lower(), page)

    def resolver(target):
        target_clean = target.strip("/")
        if not target_clean.endswith(".md"):
            target_clean += ".md"
        from app.url_utils import url_path_from_page_path
        # try: exact path → raw target → basename (case-insensitive) → title (case-insensitive)
        matched = (
            known_pages.get(target_clean)
            or known_pages.get(target)
            or basename_pages.get(target_clean.lower())
            or basename_pages.get(target.strip("/").lower())
            or title_aliases.get(target.lower())
        )
        if matched:
            url = f"/@{wiki_owner}/{wiki_slug}/{url_path_from_page_path(matched.path, strip_md=True)}"
        else:
            url = f"/@{wiki_owner}/{wiki_slug}/{url_path_from_page_path(target_clean, strip_md=True)}" if wiki_owner else f"#{target}"
        return url, matched is not None

    html = render_markdown(content, resolve_wikilinks=resolver if wiki_owner else None)

    # resolve relative markdown links (e.g. ../raw/foo.md) against current page path
    if wiki_owner and wiki_slug and current_page_path:
        import posixpath
        from app.url_utils import url_path_from_page_path
        page_dir = posixpath.dirname(current_page_path)

        def resolve_relative_link(match):
            prefix = match.group(1)
            href = match.group(2)
            suffix = match.group(3)
            # only resolve relative .md links (not external URLs, anchors, or absolute paths)
            if href.startswith(("http://", "https://", "/", "#", "mailto:")):
                return match.group(0)
            if not href.endswith(".md"):
                return match.group(0)
            # resolve ../path relative to current page's directory
            resolved = posixpath.normpath(posixpath.join(page_dir, href))
            # check if this resolves to a known page
            page = known_pages.get(resolved)
            if page:
                url = f"/@{wiki_owner}/{wiki_slug}/{url_path_from_page_path(page.path, strip_md=True)}"
                return f'{prefix}{url}{suffix}'
            return match.group(0)

        html = re.sub(r'(href=")([^"]+)(")', resolve_relative_link, html)

    html = _prepend_frontmatter_h1(content, html)

    return html
