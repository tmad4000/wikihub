#!/usr/bin/env python3 -u
"""
Migrate Jeremy Nixon's Thinking Index to WikiHub.

Parses the index at https://jeremynixon.github.io/thinking/2020/04/22/index.html
and creates wiki pages for each category and entry.
"""

import re
import sys
import time
import requests
from dataclasses import dataclass, field

WIKIHUB_BASE = "https://wikihub.globalbr.ai"
API_KEY = "wh_PSINkt8oJZuM9CbuRlXjRho6vJZ-PFIyTTsHy8ji2eY"
WIKI_OWNER = "jeremynixon"
WIKI_SLUG = "thinking"
INDEX_URL = "https://jeremynixon.github.io/thinking/2020/04/22/index.html"

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}


def log(msg):
    print(msg, flush=True)


@dataclass
class Entry:
    title: str
    url: str


@dataclass
class Category:
    name: str
    slug: str
    entries: list = field(default_factory=list)


def clean_google_redirect_url(url: str) -> str:
    """Clean up Google redirect tracking params from URLs."""
    url = re.sub(r'&sa=D&ust=\d+&usg=[A-Za-z0-9_-]+$', '', url)
    url = url.replace('&amp;', '&')
    url = url.replace('%3D', '=')
    return url


def slugify(text: str) -> str:
    """Convert text to a URL-friendly slug."""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_]+', '-', text)
    text = re.sub(r'-+', '-', text)
    text = text.strip('-')
    return text[:80]


def parse_index(html: str) -> list[Category]:
    """Parse the index HTML and extract categories with their entries."""
    categories = []

    body_match = re.search(
        r'class="post-content e-content"[^>]*>(.*?)</article>',
        html, re.DOTALL
    )
    if not body_match:
        raise ValueError("Could not find post content in HTML")

    content = body_match.group(1)
    parts = re.split(r'<h2[^>]*>', content)

    for part in parts[1:]:
        h2_match = re.match(r'([^<]+)</h2>', part)
        if not h2_match:
            continue

        cat_name = h2_match.group(1).strip()
        cat_slug = slugify(cat_name)
        category = Category(name=cat_name, slug=cat_slug)

        links = re.findall(
            r'<a\s+href="([^"]+)"[^>]*>([^<]+)</a>',
            part
        )

        for url, title in links:
            clean_url = clean_google_redirect_url(url)
            entry = Entry(title=title.strip(), url=clean_url)
            category.entries.append(entry)

        if category.entries:
            categories.append(category)

    return categories


def create_page(slug: str, content: str) -> bool:
    """Create a page in the wiki via the API."""
    url = f"{WIKIHUB_BASE}/api/v1/wikis/{WIKI_OWNER}/{WIKI_SLUG}/pages"
    path = f"{slug}.md"
    payload = {"path": path, "content": content}

    try:
        resp = requests.post(url, headers=HEADERS, json=payload, timeout=15)
        if resp.status_code in (200, 201):
            log(f"  + {slug}")
            return True
        elif resp.status_code == 409:
            log(f"  = {slug} (exists)")
            return True
        else:
            log(f"  ! {slug} ({resp.status_code}: {resp.text[:100]})")
            return False
    except requests.exceptions.RequestException as e:
        log(f"  ! {slug} (error: {e})")
        return False


def build_category_page(category: Category) -> str:
    lines = [
        "---",
        f"title: {category.name}",
        "visibility: public",
        "---",
        "",
        f"# {category.name}",
        "",
    ]
    for entry in category.entries:
        lines.append(f"- [{entry.title}]({entry.url})")
    lines.append("")
    return "\n".join(lines)


def build_index_page(categories: list[Category]) -> str:
    lines = [
        "---",
        "title: The Index",
        "visibility: public",
        "---",
        "",
        "# The Index",
        "",
        "![The Index](https://github.com/JeremyNixon/JeremyNixon.github.io/raw/master/_site/images/great_library.jpg)",
        "",
        "By [@jvnixon](https://twitter.com/JvNixon)",
        "",
        "A collection of essays, notes, and resources on thinking, learning, and problem-solving.",
        "",
        "Originally published at [jeremynixon.github.io](https://jeremynixon.github.io/thinking/2020/04/22/index.html).",
        "",
        "---",
        "",
    ]

    for category in categories:
        lines.append(f"## {category.name}")
        lines.append("")
        for entry in category.entries:
            entry_slug = slugify(entry.title)
            lines.append(f"- [[{entry_slug}|{entry.title}]]")
        lines.append("")

    return "\n".join(lines)


def build_entry_page(entry: Entry, category_name: str, category_slug: str) -> str:
    lines = [
        "---",
        f'title: "{entry.title}"',
        "visibility: public",
        "---",
        "",
        f"# {entry.title}",
        "",
        f"Category: [[{category_slug}|{category_name}]]",
        "",
        f"[Read the original document]({entry.url})",
        "",
    ]
    return "\n".join(lines)


def main():
    log("Fetching index page...")
    resp = requests.get(INDEX_URL, timeout=30)
    resp.raise_for_status()

    log("Parsing categories and entries...")
    categories = parse_index(resp.text)

    total_entries = sum(len(c.entries) for c in categories)
    log(f"Found {len(categories)} categories with {total_entries} entries total")

    seen_slugs = {}
    created = 0
    skipped = 0
    failed = 0

    log("\n--- Creating entry pages ---")
    for category in categories:
        log(f"\n[{category.name}] ({len(category.entries)} entries)")
        for entry in category.entries:
            entry_slug = slugify(entry.title)

            if entry_slug in seen_slugs:
                if seen_slugs[entry_slug] == entry.url:
                    skipped += 1
                    continue
                entry_slug = f"{entry_slug}-{category.slug}"

            seen_slugs[entry_slug] = entry.url
            content = build_entry_page(entry, category.name, category.slug)
            if create_page(entry_slug, content):
                created += 1
            else:
                failed += 1
            time.sleep(0.15)

    log("\n--- Creating category pages ---")
    for category in categories:
        content = build_category_page(category)
        if create_page(category.slug, content):
            created += 1
        else:
            failed += 1
        time.sleep(0.15)

    log("\n--- Updating index page ---")
    index_content = build_index_page(categories)
    # Update existing index page
    url = f"{WIKIHUB_BASE}/api/v1/wikis/{WIKI_OWNER}/{WIKI_SLUG}/pages/index.md"
    try:
        resp = requests.put(url, headers=HEADERS, json={"content": index_content}, timeout=15)
        if resp.status_code in (200, 201):
            log("  + index (updated)")
            created += 1
        else:
            log(f"  ! index ({resp.status_code}: {resp.text[:100]})")
            failed += 1
    except Exception as e:
        log(f"  ! index (error: {e})")
        failed += 1

    log(f"\nDone! Created: {created}, Skipped: {skipped}, Failed: {failed}")
    log(f"Wiki: {WIKIHUB_BASE}/@{WIKI_OWNER}/{WIKI_SLUG}")


if __name__ == "__main__":
    main()
