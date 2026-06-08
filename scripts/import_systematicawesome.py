#!/usr/bin/env python3
"""
Import the Jan-30-2026 systematicawesome.jacobcole.net scrape from
~/noos/backups/google-docs/html/ on noos-prod (synced locally to /tmp/sa-scrape/)
into Jacob's WikiHub at @jacobcole/systematicawesome.

Strategy:
  * One wiki per scrape (`systematicawesome`)
  * One page per HTML file. Filename (sans `.html`) becomes the path slug.
  * HTML cleaned (drop <style>, drop class= attrs, unwrap Google redirect URLs),
    then pandoc -> GFM markdown.
  * Internal `*.jacobcole.net` references converted to wikilinks when the target
    exists in the scrape.
  * Frontmatter records source URL, doc id, scrape date.
  * Failsafe: never overwrite an existing page silently — if the API page
    already exists with substantive content, write to `<slug>.import.YYYY-MM-DD.md`
    instead.

References ticket wikihub-q7gz.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote

import requests

SCRAPE_DIR = Path("/tmp/sa-scrape")
WIKIHUB_BASE = "https://wikihub.md"
KEY_PATH = Path.home() / ".config/wikihub/jacobcole-import-key.txt"
# Lazy-tolerant: --dry-run must work offline without the import key present.
API_KEY = KEY_PATH.read_text().strip() if KEY_PATH.exists() else ""
WIKI_OWNER = "jacobcole"
WIKI_SLUG = "systematic-awesome"
SCRAPE_DATE = "2026-01-30"

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

# Build wikilink map: filename-stem -> wikilink target (same path used as wiki page id).
def build_pages_index():
    pages = {}
    for f in SCRAPE_DIR.glob("*.html"):
        stem = f.stem
        pages[stem] = stem
    return pages



PAGES_INDEX = build_pages_index()

# Map subdomain -> page slug. Lowercase the host part before lookup.
def subdomain_to_slug(host: str) -> str | None:
    host = host.lower().strip()
    # Strip a trailing /
    if not host.endswith(".jacobcole.net"):
        return None
    sub = host[: -len(".jacobcole.net")]
    sub = sub.lower()
    # Some scrape filenames differ slightly from subdomains (e.g.
    # 'thingyoudidntknowexistedinsandiego' was scraped without the trailing 's' on 'thing').
    if sub in PAGES_INDEX:
        return sub
    # Try variants
    if sub.replace("-", "") in PAGES_INDEX:
        return sub.replace("-", "")
    return None


SOURCE_URL_RE = re.compile(
    r"https?://(?:www\.)?google\.com/url\?[^\s\"'\)]+", re.IGNORECASE
)


def unwrap_google_url(match) -> str:
    url = match.group(0)
    qm = re.search(r"[?&]q=([^&]+)", url)
    if not qm:
        return url
    return unquote(qm.group(1))


def clean_html(raw_html: str) -> str:
    # Drop <style> blocks
    html = re.sub(r"<style[^>]*>.*?</style>", "", raw_html, flags=re.DOTALL)
    # Drop class attributes
    html = re.sub(r' class="[^"]*"', "", html)
    # Replace inline data: image URIs with a placeholder note (they bloat output
    # past the 2MB page limit; images are out of scope for this import pass).
    html = re.sub(
        r'<img[^>]*src="data:image[^"]+"[^>]*>',
        "<p>[Inline image omitted — original Google Doc has embedded image; see source for original.]</p>",
        html,
    )
    # Unwrap Google redirect URLs found inside hrefs
    def fix_href(m):
        inner = m.group(1)
        if "google.com/url" in inner:
            qm = re.search(r"[?&]q=([^&\"']+)", inner)
            if qm:
                return f'href="{unquote(qm.group(1))}"'
        return m.group(0)

    html = re.sub(r'href="([^"]+)"', fix_href, html)
    # Drop empty spans/paragraphs
    html = re.sub(r"<span>\s*</span>", "", html)
    html = re.sub(r"<p[^>]*>\s*</p>", "", html)
    return html


def html_to_markdown(html: str) -> str:
    # pandoc: HTML -> GFM markdown
    res = subprocess.run(
        ["pandoc", "-f", "html", "-t", "gfm", "--wrap=preserve"],
        input=html,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if res.returncode != 0:
        raise RuntimeError(f"pandoc failed: {res.stderr[:500]}")
    return res.stdout


def wikilinkify(md: str) -> str:
    """Convert [text](http://foo.jacobcole.net) to [[foo|text]] when foo is in the scrape."""
    # Match markdown link syntax: [text](url)
    LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\)\s]+)\)")

    def replace(m):
        text = m.group(1)
        url = m.group(2)
        # Extract host
        hm = re.match(r"https?://([^/]+)", url)
        if not hm:
            return m.group(0)
        host = hm.group(1)
        slug = subdomain_to_slug(host)
        if not slug:
            return m.group(0)
        # Wikilink with alias
        if text.strip().lower() == slug.lower() or text.strip().lower() == host.lower():
            return f"[[{slug}]]"
        return f"[[{slug}|{text}]]"

    return LINK_RE.sub(replace, md)


def postprocess_markdown(md: str) -> str:
    md = wikilinkify(md)
    # Remove empty headings like '## ' or '### '
    md = re.sub(r"^#{1,6}\s*$", "", md, flags=re.MULTILINE)
    # Drop residual google.com/url naked links
    def naked_g_url(m):
        url = m.group(0)
        qm = re.search(r"[?&]q=([^&\s\"'<>]+)", url)
        if qm:
            return unquote(qm.group(1))
        return url
    md = re.sub(r"https?://(?:www\.)?google\.com/url\?[^\s\"'<>\)]+", naked_g_url, md)
    # Collapse runs of >2 blank lines
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md


def get_source_info(stem: str, results: dict) -> dict:
    """Look up source URL + docId from export-results.json for a given filename stem."""
    for entry in results.get("exported", []) + results.get("failed", []):
        fn = entry.get("filename", "")
        if fn.endswith(".html") and fn[:-5] == stem:
            return entry
    return {}


PRETTY_TITLES = {
    "adhd": "ADHD",
    "admitsphere": "AdmitSphere",
    "aieducation": "AI Education",
    "aspirations": "Aspirations",
    "autodidacts": "Autodidacts",
    "backpain": "Back Pain",
    "bookslist": "Books List",
    "bugslist": "Bugs List",
    "buoyantfitness": "Buoyant Fitness",
    "burningman": "Burning Man",
    "cheese": "Cheese",
    "chocolate": "Chocolate",
    "chronicpain": "Chronic Pain",
    "circuits": "Circuits",
    "climatechange": "Climate Change",
    "codex": "Codex",
    "commentaries": "Commentaries",
    "coronavirus": "Coronavirus",
    "covid19hackideas": "COVID-19 Hack Ideas",
    "crises": "Crises",
    "cryptoconnect": "Crypto Connect",
    "culturalendangeredspecies": "Cultural Endangered Species",
    "culturaltechnology": "Cultural Technology",
    "easilysolvableworldproblems": "Easily Solvable World Problems",
    "ethicaldilemmas": "Ethical Dilemmas",
    "existentialcrisis": "Existential Crisis",
    "favorverse": "Favorverse",
    "foodslist": "Foods List",
    "gestaltexplanation": "Gestalt Explanation",
    "globalideabank": "Global Idea Bank",
    "hackathonprojects": "Hackathon Projects",
    "healingartsgrant": "Healing Arts Grant",
    "heidegger": "Heidegger",
    "hiringblurb": "Hiring Blurb",
    "hiringlist": "Hiring List",
    "hotconnections": "Hot Connections",
    "housinglist": "Housing List",
    "hypnosis": "Hypnosis",
    "ideaflowbackground": "IdeaFlow Background",
    "ideaflowproject": "IdeaFlow Project",
    "ifiran": "If I Ran",
    "ilparty": "IL Party",
    "index": "Index",
    "infrastructure": "Infrastructure",
    "interrupt": "Interrupt",
    "kidactivities": "Kid Activities",
    "lifechange": "Life Change",
    "lifechangingthings": "Life Changing Things",
    "lists": "Lists",
    "meditation": "Meditation",
    "nvc": "NVC",
    "perfectcoordination": "Perfect Coordination",
    "philosophy": "Philosophy",
    "productfeedback": "Product Feedback",
    "products": "Products",
    "pureland": "Pureland",
    "qigongcrew": "Qigong Crew",
    "qiresearch": "Qi Research",
    "questions": "Questions",
    "quoteslist": "Quotes List",
    "salon": "Salon",
    "shadirecs": "Shadi Recs",
    "si": "SI",
    "sijointpain": "SI Joint Pain",
    "sleep": "Sleep",
    "socialsupportforregenerativefarmers": "Social Support for Regenerative Farmers",
    "stanfordclasses": "Stanford Classes",
    "startupideas": "Startup Ideas",
    "startuptrickswiki": "Startup Tricks Wiki",
    "supplements": "Supplements",
    "systematicawesome": "Systematic Awesome",
    "tea": "Tea",
    "templates": "Templates",
    "thingsyoudidntknowexisted": "Things You Didn't Know Existed",
    "thingsyoudidntknowexistedatmit": "Things You Didn't Know Existed at MIT",
    "thingsyoudidntknowexistedinhawaii": "Things You Didn't Know Existed in Hawaii",
    "thingsyoudidntknowexistedinnyc": "Things You Didn't Know Existed in NYC",
    "thingsyoudidntknowexistedinportland": "Things You Didn't Know Existed in Portland",
    "thingsyoudidntknowexistedinsantacruz": "Things You Didn't Know Existed in Santa Cruz",
    "thingsyoudidntknowexistedinsf": "Things You Didn't Know Existed in SF",
    "thingyoudidntknowexistedinsandiego": "Things You Didn't Know Existed in San Diego",
    "thoughtfulweb": "Thoughtful Web",
    "toolstacks": "Tool Stacks",
    "visioncharter": "Vision Charter",
    "vrcoralreefs": "VR Coral Reefs",
    "wall": "Wall",
    "worldgestalts": "World Gestalts",
    "worldproblems": "World Problems",
    "yogalist": "Yoga List",
}


def prettify_stem(stem: str) -> str:
    if stem in PRETTY_TITLES:
        return PRETTY_TITLES[stem]
    # Split on common boundaries
    out = stem
    # Heuristic: insert space before known words
    for word in [
        "wiki", "list", "things", "you", "didnt", "know", "existed",
        "in", "at", "of", "for", "and", "the", "atmit", "innyc", "insf",
        "insandiego", "insantacruz", "inportland", "inhawaii",
    ]:
        out = re.sub(r"(?<=[a-z])(" + word + r")(?=[a-z]|$)", r" \1", out)
    return out.replace("-", " ").strip().title()


def title_from_md(md: str, fallback: str) -> str:
    # Prefer the prettified filename — H1 in Google Docs is often blank or
    # has table-of-contents headings that don't describe the page well.
    return prettify_stem(fallback)


def build_page(stem: str, html_path: Path, results: dict) -> tuple[str, str]:
    """Returns (markdown_with_frontmatter, page_title)."""
    raw = html_path.read_text(encoding="utf-8", errors="replace")
    cleaned = clean_html(raw)
    md = html_to_markdown(cleaned)
    md = postprocess_markdown(md)
    src = get_source_info(stem, results)
    source_url = src.get("url", f"http://{stem}.jacobcole.net")
    doc_id = src.get("docId", "")
    gdoc_url = f"https://docs.google.com/document/d/{doc_id}" if doc_id else ""
    title = title_from_md(md, stem)
    fm_lines = [
        "---",
        f"title: {json.dumps(title)}",
        f"source_url: {source_url}",
    ]
    if gdoc_url:
        fm_lines.append(f"source_gdoc: {gdoc_url}")
    fm_lines.append(f"scraped_at: {SCRAPE_DATE}")
    fm_lines.append("imported_by: wikihub-q7gz")
    fm_lines.append("visibility: public")
    fm_lines.append("---")
    fm = "\n".join(fm_lines)
    footer_parts = [
        "",
        "---",
        "",
        f"*Source: <{source_url}>*",
    ]
    if gdoc_url:
        footer_parts.append(f"*Google Doc: <{gdoc_url}>*")
    footer_parts.append(f"*Scraped: {SCRAPE_DATE}*")
    footer = "\n".join(footer_parts)
    body = md.strip() + "\n" + footer + "\n"
    return fm + "\n\n" + body, title


def ensure_wiki(dry_run=False):
    """Create the destination wiki if missing."""
    if dry_run:
        # Fully offline dry-run: don't touch the network or require an API key.
        print(f"[DRY] would ensure wiki {WIKI_OWNER}/{WIKI_SLUG} exists")
        return
    url = f"{WIKIHUB_BASE}/api/v1/wikis/{WIKI_OWNER}/{WIKI_SLUG}"
    r = requests.get(url, headers=HEADERS, timeout=15)
    if r.status_code == 200:
        return
    if r.status_code == 404:
        if dry_run:
            print(f"[DRY] would create wiki {WIKI_OWNER}/{WIKI_SLUG}")
            return
        # Create
        create_url = f"{WIKIHUB_BASE}/api/v1/wikis"
        payload = {
            "slug": WIKI_SLUG,
            "title": "Systematic Awesome",
            "description": (
                "Archive of jacobcole.net subdomain Google Docs (systematicawesome universe). "
                "Imported via wikihub-q7gz from the 2026-01-30 scrape."
            ),
            "template": "freeform",
        }
        r2 = requests.post(create_url, headers=HEADERS, json=payload, timeout=15)
        if r2.status_code >= 400:
            raise RuntimeError(f"create wiki failed: {r2.status_code} {r2.text}")
        print(f"Created wiki @{WIKI_OWNER}/{WIKI_SLUG}")
        return
    r.raise_for_status()


def get_existing_page(path: str) -> dict | None:
    url = f"{WIKIHUB_BASE}/api/v1/wikis/{WIKI_OWNER}/{WIKI_SLUG}/pages/{path}"
    r = requests.get(url, headers=HEADERS, timeout=15)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def post_page(path: str, content: str) -> requests.Response:
    url = f"{WIKIHUB_BASE}/api/v1/wikis/{WIKI_OWNER}/{WIKI_SLUG}/pages"
    payload = {
        "path": path,
        "content": content,
        "visibility": "public",
    }
    return requests.post(url, headers=HEADERS, json=payload, timeout=30)


def put_page(path: str, content: str) -> requests.Response:
    url = f"{WIKIHUB_BASE}/api/v1/wikis/{WIKI_OWNER}/{WIKI_SLUG}/pages/{path}"
    payload = {
        "content": content,
        "visibility": "public",
    }
    return requests.put(url, headers=HEADERS, json=payload, timeout=30)


def main():
    dry_run = "--dry-run" in sys.argv
    only = None
    for a in sys.argv[1:]:
        if a.startswith("--only="):
            only = a.split("=", 1)[1]
    results_path = SCRAPE_DIR / "export-results.json"
    results = json.loads(results_path.read_text()) if results_path.exists() else {}

    ensure_wiki(dry_run=dry_run)

    stems = sorted(PAGES_INDEX.keys())
    if only:
        stems = [s for s in stems if s == only]
    print(f"importing {len(stems)} pages")

    # The web view route appends `.md` to URL paths and queries DB for that
    # exact path. Store pages with a `.md` suffix so they're routable.
    PATH_SUFFIX = ".md"

    counts = {"created": 0, "updated": 0, "skipped": 0, "flagged": 0, "errors": 0}
    flags = []
    for i, stem in enumerate(stems, 1):
        path = f"{stem}.md"
        try:
            content, title = build_page(stem, SCRAPE_DIR / f"{stem}.html", results)
        except Exception as e:
            print(f"[{i}/{len(stems)}] {stem}: build error: {e}")
            counts["errors"] += 1
            continue
        if dry_run:
            print(f"[{i}/{len(stems)}] {stem}: ({len(content)} bytes) title={title!r}")
            continue
        target_path = stem + PATH_SUFFIX
        existing = get_existing_page(target_path)
        write_path = target_path
        action = "created"
        if existing and existing.get("content", "").strip():
            existing_body = existing["content"]
            # If it's an older import of ours, overwrite.  Else save under date-suffix.
            if "imported_by: wikihub-q7gz" in existing_body:
                action = "updated"
            else:
                write_path = f"{stem}.import.{SCRAPE_DATE}.md"
                flags.append({
                    "stem": stem,
                    "reason": "existing non-import page; saved under .import.YYYY-MM-DD path",
                    "import_path": write_path,
                })
                counts["flagged"] += 1
                # Re-check whether the date-suffix path also already exists
                if get_existing_page(write_path):
                    action = "updated"
                else:
                    action = "created"
        # POST when creating, PUT when updating
        if action == "created":
            r = post_page(write_path, content)
        else:
            r = put_page(write_path, content)
        if r.status_code >= 400:
            print(f"[{i}/{len(stems)}] {stem} -> {write_path}: {action.upper()} failed {r.status_code} {r.text[:200]}")
            counts["errors"] += 1
        else:
            counts[action] += 1
            print(f"[{i}/{len(stems)}] {stem} -> {write_path} ({action}) [{r.status_code}]")
        time.sleep(0.2)  # ~5 req/s
    print("\n--- DONE ---")
    print(json.dumps(counts, indent=2))
    if flags:
        print("\nFlagged for review:")
        for f in flags:
            print(f"  - {f['stem']}: {f['reason']} -> {f['import_path']}")


if __name__ == "__main__":
    main()
