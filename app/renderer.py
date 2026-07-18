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
from urllib.parse import unquote

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


# Google Docs TOC anchors look like #h.v92l0g36bhyw (internal bookmark ids).
_GDOC_TOC_ANCHOR_RE = re.compile(r'<a href="#(h\.[a-z0-9]+)">(.*?)</a>', re.IGNORECASE | re.DOTALL)
_HEADING_ID_RE = re.compile(r'<h[1-6]\s+id="([^"]+)"', re.IGNORECASE)
# TOC entries end with a tab/spaces + a page number, e.g. "Feeds        3".
_TRAILING_PAGENUM_RE = re.compile(r'[\s ]+\d+\s*$')
_URI_SCHEME_RE = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*:')


def _rewrite_gdoc_toc_anchors(html):
    """Rewrite Google-Docs-style table-of-contents anchor links to real slugs.

    Pages imported from Google Docs (frontmatter ``source_gdoc``) carry an
    auto-generated TOC whose links point to Google's internal bookmark ids
    (e.g. ``#h.v92l0g36bhyw``). Our renderer emits heading ids by slugifying
    the heading text (``#feeds``), so those bookmark anchors never resolve and
    every TOC link is dead.

    Each TOC link's visible text is the heading title followed by a page number
    (``Feeds        3``). We strip the trailing page number, slugify the text,
    and rewrite the href to the matching heading slug. Duplicate headings
    ("Misc" -> ``misc``, ``misc-1``) are consumed in document order, which is
    the order the TOC lists them. Links with no matching heading are left
    untouched. No-op when there are no such anchors.
    """
    if 'href="#h.' not in html:
        return html

    heading_ids = _HEADING_ID_RE.findall(html)
    if not heading_ids:
        return html

    # base slug -> ordered queue of concrete ids (handles duplicate headings)
    from collections import defaultdict, deque
    buckets = defaultdict(deque)
    for hid in heading_ids:
        base = re.sub(r'-\d+$', '', hid)
        buckets[base].append(hid)
    valid_ids = set(heading_ids)

    def repl(match):
        text = match.group(2)
        plain = re.sub(r'<[^>]+>', '', text)
        plain = _TRAILING_PAGENUM_RE.sub('', plain).strip()
        candidate = _heading_slug(plain)
        target = None
        queue = buckets.get(candidate)
        if queue:
            target = queue.popleft()
        elif candidate in valid_ids:
            target = candidate
        if not target:
            return match.group(0)  # leave broken anchor untouched; don't fabricate
        return f'<a href="#{target}">{text}</a>'

    return _GDOC_TOC_ANCHOR_RE.sub(repl, html)


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
        html_exts = {".html", ".htm"}
        ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

        if ext in img_exts:
            token = state.push("image", "img", 0)
            token.attrs = {"src": filename, "alt": filename}
            if width:
                token.attrs["width"] = str(width)
                token.attrs["style"] = f"max-width: {width}px"
            token.children = []
        elif ext in html_exts:
            # ![[deck.html]] / ![[deck.html|600]] — inline a sandboxed iframe of
            # the stored HTML. The embed plugin runs on the shared singleton with
            # NO wiki owner/slug context, so (mirroring the [[wikilink]] pattern)
            # we emit a placeholder here and resolve it to an <iframe> in
            # render_page(), where owner/slug + the .wikihub/serve-inline opt-in
            # are available. width (after '|') doubles as the iframe height in px.
            # Placeholder format: <!--htmlembed:HEIGHT:PATH--> (HEIGHT may be empty).
            token = state.push("html_inline", "", 0)
            token.content = f"<!--htmlembed:{width if width is not None else ''}:{filename}-->"
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

    NOTE on Cloudflare: this domain runs Cloudflare HTML Auto Minify (or
    similar transform) which actively strips <br> tags from text/html
    responses — verified empirically. The proper fix is to disable HTML
    Auto Minify and Email Address Obfuscation in the CF dashboard. The
    'no-transform' Cache-Control header (set in app/__init__.py) does NOT
    persuade CF to back off — they ignore it.

    We still emit <br> here because (a) it's correct standard HTML, (b)
    every alternative we tested (XHTML, empty span, span+ZWNJ, div+wbr)
    is also stripped by the same minifier, and (c) on any non-CF
    deployment this works perfectly. (wikihub-eiv7)
    """
    def hardbreak(tokens, idx, options, env):
        return "<br>"

    def softbreak(tokens, idx, options, env):
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

    # rewrite Google-Docs TOC anchors (#h.xxxx) to real heading slugs
    html = _rewrite_gdoc_toc_anchors(html)

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

    # Absolute-ize relative in-content links (wikihub-qmx6).
    #
    # Markdown like [topics](topics) or [notes](../raw/foo.md) renders as a
    # host-relative href ("topics", "../raw/foo.md"). On the apex form
    # (/@owner/slug/page) the browser resolves those against the page URL, but on
    # the canonical *subdomain* form (sub.wikihub.md/slug — NO trailing slash) a
    # bare "topics" resolves against the host root -> sub.wikihub.md/topics -> 404.
    # Rewrite every relative link to an absolute /@owner/slug/... path so it
    # resolves inside the wiki regardless of which host form served the page.
    if wiki_owner and wiki_slug and current_page_path:
        import posixpath
        from app.url_utils import url_path_from_page_path
        page_dir = posixpath.dirname(current_page_path)

        def resolve_relative_link(match):
            prefix = match.group(1)
            href = match.group(2)
            suffix = match.group(3)
            if _URI_SCHEME_RE.match(href) or href.startswith(("//", "/", "#")):
                return match.group(0)
            # split off ?query / #fragment so we resolve only the path part
            m = re.match(r'^([^?#]*)([?#].*)?$', href)
            path_part = m.group(1)
            tail = m.group(2) or ""
            if not path_part:
                # pure query/fragment (e.g. "?x=1", "#sec") — leave alone
                return match.group(0)
            path_part = unquote(path_part)
            directory_link = path_part.endswith("/")
            # resolve ../path relative to the current page's directory
            resolved = posixpath.normpath(posixpath.join(page_dir, path_part))
            # a link that escapes the wiki root (../../x) has no sane wiki URL
            if resolved == ".." or resolved.startswith("../") or resolved == ".":
                return match.group(0)
            if resolved.endswith(".md"):
                # prefer the canonical URL of a known page; otherwise still
                # absolute-ize so the link is host-independent.
                page = known_pages.get(resolved)
                target = page.path if page else resolved
                url = f"/@{wiki_owner}/{wiki_slug}/{url_path_from_page_path(target, strip_md=True)}"
            else:
                # already URL-form (no .md); just anchor it to the wiki root.
                url = f"/@{wiki_owner}/{wiki_slug}/{url_path_from_page_path(resolved, strip_md=False)}"
            if directory_link and not url.endswith("/"):
                url += "/"
            return f'{prefix}{url}{tail}{suffix}'

        html = re.sub(r'(href=")([^"]+)(")', resolve_relative_link, html)

    # resolve ![[file.html]] embed placeholders into sandboxed iframes (wikihub-wz2j).
    # The placeholder (<!--htmlembed:HEIGHT:PATH-->) is emitted by the embed plugin,
    # which has no wiki context. Here we have owner/slug, so we can resolve the
    # standalone serve URL and check the owner's .wikihub/serve-inline opt-in.
    html = _resolve_html_embeds(html, wiki_owner, wiki_slug, current_page_path)

    # add target=_blank to links pointing at non-markdown files (wikihub-057 part 1)
    html = _retarget_non_md_file_links(html)

    html = _prepend_frontmatter_h1(content, html)

    return html


_HTML_EMBED_RE = re.compile(r'<!--htmlembed:(\d*):(.*?)-->')

# file extensions whose links should open in a new tab (downloads / standalone
# documents that would otherwise replace the wiki page). wikihub-057 part 1.
_NEW_TAB_LINK_EXTS = {
    ".html", ".htm", ".pdf", ".svg", ".png", ".jpg", ".jpeg",
    ".gif", ".zip",
}


def build_html_embed_figure(raw_url, name, height=480):
    """Build the sandboxed <figure class="html-embed"> markup for an HTML deck.

    Shared by the ![[file.html]] inline embed (_resolve_html_embeds) and the
    sidebar-click embedded viewer (wiki_page ?view branch). The iframe src and
    the '↗ open' pop-out both point at `raw_url` (the raw .html page URL).
    Sandbox is allow-scripts allow-popups allow-popups-to-escape-sandbox with
    NO allow-same-origin, so embedded decks can run JS / open links but cannot
    touch the parent origin.
    """
    raw_url_attr = _escape(raw_url)
    name_html = _escape(name)
    return (
        f'<figure class="html-embed">'
        f'<div class="html-embed-bar"><span class="html-embed-name">{name_html}</span>'
        f'<a class="html-embed-open" href="{raw_url_attr}" target="_blank" rel="noopener noreferrer">&#8599; open</a></div>'
        f'<iframe src="{raw_url_attr}" class="html-embed-frame" loading="lazy" '
        f'sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox" '
        f'style="width:100%;height:{height}px;border:0"></iframe>'
        f'</figure>'
    )


def _resolve_html_embeds(html, wiki_owner, wiki_slug, current_page_path):
    """Replace ![[file.html]] embed placeholders with a sandboxed iframe.

    Gating: only embed an iframe when the target path is owner-opted-in via
    .wikihub/serve-inline (reusing load_serve_inline_patterns + matches_serve_inline).
    If the placeholder can't be resolved (no wiki context) or the file isn't
    allowlisted, fall back to a plain link so non-allowlisted active content is
    never iframed.
    """
    if '<!--htmlembed:' not in html:
        return html

    import posixpath
    from app.url_utils import url_path_from_page_path

    patterns = None
    if wiki_owner and wiki_slug:
        from app.wiki_ops import load_serve_inline_patterns
        patterns = load_serve_inline_patterns(wiki_owner, wiki_slug)

    def resolve(match):
        height_str = match.group(1)
        raw_path = match.group(2).strip()
        height = int(height_str) if height_str else 480

        # resolve a possibly-relative embed path against the current page's dir
        embed_path = raw_path
        if current_page_path and not raw_path.startswith("/"):
            page_dir = posixpath.dirname(current_page_path)
            embed_path = posixpath.normpath(posixpath.join(page_dir, raw_path)) if page_dir else raw_path

        basename = embed_path.rsplit("/", 1)[-1]

        # without wiki context we can't build a real URL — emit a safe label.
        if not (wiki_owner and wiki_slug):
            return f'<a href="{_escape(raw_path)}" target="_blank" rel="noopener noreferrer">{_escape(basename)}</a>'

        from app.acl import matches_serve_inline
        raw_url = f"/@{wiki_owner}/{wiki_slug}/{url_path_from_page_path(embed_path, strip_md=False)}"
        raw_url_attr = _escape(raw_url)
        name_html = _escape(basename)

        # gating: only iframe owner-opted-in files. otherwise fall back to a link.
        if not (patterns and matches_serve_inline(embed_path, patterns)):
            return (
                f'<a href="{raw_url_attr}" class="embed-link file-embed" '
                f'target="_blank" rel="noopener noreferrer">'
                f'<span class="file-icon">&#128196;</span> {name_html}</a>'
            )

        return build_html_embed_figure(raw_url, basename, height=height)

    return _HTML_EMBED_RE.sub(resolve, html)


def _retarget_non_md_file_links(html):
    """Add target=_blank rel=noopener to <a> tags whose href resolves to a
    non-markdown file (.html .pdf .svg images .zip). Links to such files would
    otherwise replace the wiki page; opening in a new tab keeps the wiki in
    place (wikihub-057 part 1). Leaves anchors, mailto:, and already-targeted
    links alone. Does NOT touch the left sidebar tree (rendered elsewhere)."""
    if "<a " not in html:
        return html

    def retarget(match):
        full = match.group(0)
        href = match.group(1)
        # skip pure anchors and non-navigational schemes
        low = href.lower()
        if low.startswith(("#", "mailto:", "javascript:", "tel:")):
            return full
        # strip query/fragment before checking the extension
        path_part = href.split("#", 1)[0].split("?", 1)[0]
        ext = "." + path_part.rsplit(".", 1)[-1].lower() if "." in path_part.rsplit("/", 1)[-1] else ""
        if ext not in _NEW_TAB_LINK_EXTS:
            return full
        if 'target=' in full:
            return full
        # inject target+rel right after the href attribute
        return full.replace(
            f'href="{href}"',
            f'href="{href}" target="_blank" rel="noopener noreferrer"',
            1,
        )

    return re.sub(r'<a [^>]*?href="([^"]+)"[^>]*>', retarget, html)
