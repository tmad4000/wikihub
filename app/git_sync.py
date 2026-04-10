"""
DB -> git sync for wikihub.

writes page content to the authoritative bare repo using git plumbing
(no working tree). does NOT fire hooks — this is the critical invariant
that prevents two-way-sync loops.

also handles public mirror regeneration: force-updates the public repo
to a single commit containing only public files with private bands stripped.

ported from listhub's git_sync.py with multi-wiki path generalization.
"""

import os
import json
import subprocess
import tempfile
from datetime import datetime, timezone

from flask import current_app

from app.content_utils import parse_markdown_document, strip_private_bands


_AUTHOR_ENV = {
    "GIT_AUTHOR_NAME": "wikihub",
    "GIT_AUTHOR_EMAIL": "sync@wikihub",
    "GIT_COMMITTER_NAME": "wikihub",
    "GIT_COMMITTER_EMAIL": "sync@wikihub",
}


def _repo_path(username, slug, public=False):
    repos_dir = current_app.config["REPOS_DIR"]
    safe_user = "".join(c for c in username if c.isalnum() or c in "-_")
    safe_slug = "".join(c for c in slug if c.isalnum() or c in "-_")
    suffix = "-public" if public else ""
    return os.path.join(repos_dir, safe_user, f"{safe_slug}{suffix}.git")


def _git(repo_path, *args, env=None, input=None):
    """run a git command in the context of a bare repo. returns stdout as string."""
    cmd = ["git", "-C", repo_path] + list(args)
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    result = subprocess.run(
        cmd, capture_output=True, text=True, env=merged_env, input=input,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )
    return result.stdout.strip()


def _git_bytes(repo_path, *args, env=None, input=None):
    """run a git command with binary input. returns stdout as bytes."""
    cmd = ["git", "-C", repo_path] + list(args)
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    result = subprocess.run(
        cmd, capture_output=True, env=merged_env, input=input,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )
    return result.stdout


def _head_commit(repo_path):
    try:
        return _git(repo_path, "rev-parse", "--verify", "HEAD")
    except subprocess.CalledProcessError:
        return None


def _head_tree(repo_path):
    try:
        return _git(repo_path, "rev-parse", "--verify", "HEAD^{tree}")
    except subprocess.CalledProcessError:
        return None


def apply_repo_changes(username, slug, changes, message, author_name="wikihub", author_email="sync@wikihub"):
    """apply one or more file writes/deletes to the authoritative repo in a single commit."""
    repo = _repo_path(username, slug)
    if not os.path.isdir(repo):
        return False

    head = _head_commit(repo)
    idx = tempfile.mktemp(prefix="wikihub-sync-", suffix=".idx")
    env = {"GIT_INDEX_FILE": idx}
    author_env = {
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": author_name,
        "GIT_COMMITTER_EMAIL": author_email,
    }

    try:
        if head:
            _git(repo, "read-tree", "HEAD", env=env)

        for change in changes:
            action = change["action"]
            path = change["path"]
            if action == "delete":
                _git(
                    repo,
                    "update-index",
                    "--index-info",
                    env=env,
                    input=f"0 {'0'*40}\t{path}\n",
                )
                continue

            content = change["content"]
            if isinstance(content, str):
                content_bytes = content.encode("utf-8")
            else:
                content_bytes = content

            blob = _git_bytes(
                repo, "hash-object", "-w", "--stdin",
                input=content_bytes, env=env,
            ).strip().decode()
            _git(repo, "update-index", "--add", "--cacheinfo", "100644", blob, path, env=env)

        new_tree = _git(repo, "write-tree", env=env)
        old_tree = _head_tree(repo) if head else None
        if new_tree == old_tree:
            return False

        commit_args = ["commit-tree", new_tree, "-m", message]
        if head:
            commit_args[2:2] = ["-p", head]

        new_commit = _git(repo, *commit_args, env={**env, **author_env})
        _git(repo, "update-ref", "refs/heads/main", new_commit)
        return True
    finally:
        if os.path.exists(idx):
            os.unlink(idx)


def sync_page_to_repo(username, slug, file_path, content, message=None, author_name="wikihub", author_email="sync@wikihub"):
    """write a single file to the authoritative repo."""
    return apply_repo_changes(
        username,
        slug,
        [{"action": "write", "path": file_path, "content": content}],
        message or f"Update {file_path}",
        author_name=author_name,
        author_email=author_email,
    )


def append_event_to_repo(username, slug, event_type, **payload):
    timestamp = datetime.now(timezone.utc).isoformat()
    event = {"type": event_type, "timestamp": timestamp, **payload}
    current = read_file_from_repo(username, slug, ".wikihub/events.jsonl", public=False) or ""
    next_content = current + json.dumps(event, sort_keys=True) + "\n"
    sync_page_to_repo(username, slug, ".wikihub/events.jsonl", next_content, message=f"Log {event_type}")


def remove_page_from_repo(username, slug, file_path):
    """remove a single file from the authoritative repo."""
    return apply_repo_changes(
        username,
        slug,
        [{"action": "delete", "path": file_path}],
        f"Remove {file_path}",
    )


def regenerate_public_mirror(username, slug, acl_rules=None):
    """regenerate the public mirror from the authoritative repo.
    strips private files (per ACL), private bands, and .wikihub/acl itself.
    force-updates to a single commit."""
    from app.acl import resolve_visibility

    auth_repo = _repo_path(username, slug)
    pub_repo = _repo_path(username, slug, public=True)

    if not os.path.isdir(auth_repo):
        return

    head = _head_commit(auth_repo)
    if not head:
        return

    # list all files in HEAD
    try:
        ls_output = _git(auth_repo, "ls-tree", "-r", "--name-only", "HEAD")
    except subprocess.CalledProcessError:
        return

    idx = tempfile.mktemp(prefix="wikihub-mirror-", suffix=".idx")
    env = {"GIT_INDEX_FILE": idx}

    try:
        for filepath in ls_output.split("\n"):
            filepath = filepath.strip()
            if not filepath:
                continue

            # skip .wikihub/acl itself
            if filepath == ".wikihub/acl":
                continue

            # read file content from authoritative repo
            try:
                content_bytes = _git_bytes(auth_repo, "cat-file", "blob", f"HEAD:{filepath}")
            except subprocess.CalledProcessError:
                continue  # skip files git can't read (encoding issues, etc)

            is_markdown = filepath.endswith(".md")

            fm_vis = None
            if is_markdown:
                content = content_bytes.decode("utf-8", errors="replace")
                frontmatter, _ = parse_markdown_document(content)
                fm_vis = frontmatter.get("visibility")

            # check visibility: frontmatter > ACL > default (private)
            vis = resolve_visibility(filepath, acl_rules or [], fm_vis)
            if vis == "private":
                continue

            # strip private bands from markdown files
            if is_markdown:
                content = strip_private_bands(content)
                blob_input = content.encode("utf-8")
            else:
                blob_input = content_bytes

            # write to public mirror index
            blob = _git_bytes(
                pub_repo, "hash-object", "-w", "--stdin",
                input=blob_input, env=env,
            ).strip().decode()

            _git(pub_repo, "update-index", "--add", "--cacheinfo",
                 "100644", blob, filepath, env=env)

        new_tree = _git(pub_repo, "write-tree", env=env)

        # single commit — no parent (linearized history)
        new_commit = _git(
            pub_repo, "commit-tree", new_tree,
            "-m", f"Public snapshot @ {head[:12]}",
            env={**env, **_AUTHOR_ENV},
        )
        _git(pub_repo, "update-ref", "refs/heads/main", new_commit)
    finally:
        if os.path.exists(idx):
            os.unlink(idx)


def read_file_from_repo(username, slug, file_path, public=False):
    """read a file's content from HEAD of a bare repo."""
    repo = _repo_path(username, slug, public=public)
    if not os.path.isdir(repo):
        return None
    try:
        content = _git_bytes(repo, "cat-file", "blob", f"HEAD:{file_path}")
        return content.decode("utf-8", errors="replace")
    except subprocess.CalledProcessError:
        return None


def read_file_bytes_from_repo(username, slug, file_path, public=False):
    """read a file as raw bytes from HEAD of a bare repo (for binary files)."""
    repo = _repo_path(username, slug, public=public)
    if not os.path.isdir(repo):
        return None
    try:
        return _git_bytes(repo, "cat-file", "blob", f"HEAD:{file_path}")
    except subprocess.CalledProcessError:
        return None


def list_files_in_repo(username, slug, public=False):
    """list all files in HEAD of a bare repo."""
    repo = _repo_path(username, slug, public=public)
    if not os.path.isdir(repo):
        return []
    try:
        output = _git(repo, "ls-tree", "-r", "--name-only", "HEAD")
        return [f.strip() for f in output.split("\n") if f.strip()]
    except subprocess.CalledProcessError:
        return []


SCHEMA_TEMPLATES = {
    "freeform": (
        "# schema\n\n"
        "this wiki has no imposed structure. pages are markdown files with optional YAML frontmatter.\n\n"
        "## frontmatter\n\n"
        "```yaml\n"
        "---\n"
        "title: Page Title\n"
        "visibility: public | private | unlisted\n"
        "tags: [topic1, topic2]\n"
        "---\n"
        "```\n\n"
        "## wikilinks\n\n"
        "link pages with `[[page-name]]` or `[[page-name|Display Text]]`. "
        "links resolve by filename — `[[linear-algebra]]` finds `wiki/courses/linear-algebra.md`.\n"
    ),
    "structured": (
        "# schema\n\n"
        "this wiki uses the LLM wiki pattern. the LLM incrementally builds and maintains a persistent, "
        "interlinked knowledge base. knowledge is compiled once and kept current, not re-derived on every query.\n\n"
        "## three layers\n\n"
        "**`raw/`** — immutable source documents. articles, transcripts, notes, data. "
        "the LLM reads from raw but never modifies it. this is the source of truth.\n\n"
        "**`wiki/`** — LLM-maintained compiled pages. summaries, entity pages, concept pages, comparisons. "
        "the LLM owns this layer entirely — creates pages, updates them when new sources arrive, "
        "maintains cross-references, keeps everything consistent. you read it; the LLM writes it.\n\n"
        "**`schema.md`** (this file) — tells the LLM how the wiki is structured, what conventions to follow, "
        "and what workflows to use. you and the LLM co-evolve this over time.\n\n"
        "## page format\n\n"
        "every wiki page has two layers:\n"
        "- **compiled truth** (above the `---` divider): current best understanding, rewritten freely when evidence changes\n"
        "- **timeline** (below the `---` divider): append-only evidence trail with dates, never edited after writing\n\n"
        "the compiled truth is a living synthesis. when new evidence arrives, rewrite it. "
        "the timeline is an immutable log — only append, never edit.\n\n"
        "## frontmatter\n\n"
        "```yaml\n"
        "---\n"
        "title: Page Title\n"
        "type: topic | person | project | idea | source-summary\n"
        "visibility: public\n"
        "tags: [area1, area2]\n"
        "sources: [raw/meeting-2026-04-10.md, raw/article-link.md]\n"
        "---\n"
        "```\n\n"
        "## wikilinks\n\n"
        "**inline wikilinks are the most important thing you do.** they are what makes this a knowledge graph "
        "instead of a folder of notes.\n\n"
        "- weave links into prose naturally: `[[mark-khrapko|Mark]] mentored him on [[hiring|hiring philosophy]]`\n"
        "- link on first mention only per section\n"
        "- use display text: `[[hiring-philosophy|hiring philosophy]]` not `[[hiring-philosophy]]`\n"
        "- cross-category links are the most valuable (person↔topic, project↔idea, experience↔reflection)\n"
        "- every page should have at least 2 outbound links\n"
        "- no 'see also' sections — if it's not worth mentioning in prose, it's not a connection\n"
        "- conservative: better to miss a link than create a wrong one\n\n"
        "## operations\n\n"
        "### ingest\n\n"
        "when processing a new source:\n"
        "1. read source → identify entities (people, topics, projects)\n"
        "2. for each entity: check if wiki page exists (read index.md first)\n"
        "3. if exists: update compiled truth, append timeline entry with date and source\n"
        "4. if new: create page with compiled truth + first timeline entry\n"
        "5. **add inline wikilinks to related pages as you write** — this is the critical step\n"
        "6. update index.md with the new page\n"
        "7. append to log.md: `## [YYYY-MM-DD] ingest | Source Title`\n\n"
        "a single source might touch 10-15 wiki pages. that's normal.\n\n"
        "### query\n\n"
        "when answering questions:\n"
        "1. read index.md to find relevant pages\n"
        "2. read those pages and synthesize an answer\n"
        "3. if the answer is valuable, file it back into the wiki as a new page — "
        "explorations should compound in the knowledge base, not disappear into chat history\n"
        "4. append to log.md: `## [YYYY-MM-DD] query | Question summary`\n\n"
        "### lint\n\n"
        "periodically health-check the wiki. look for:\n"
        "- contradictions between pages\n"
        "- stale claims that newer sources have superseded\n"
        "- orphan pages with no inbound links\n"
        "- important concepts mentioned but lacking their own page\n"
        "- missing cross-references\n"
        "- data gaps that could be filled with new sources\n\n"
        "append to log.md: `## [YYYY-MM-DD] lint | Summary of findings`\n\n"
        "## index.md\n\n"
        "the index is a catalog of every page in the wiki. each entry has a link, a one-line summary, "
        "and optionally metadata like date or source count. organized by category. "
        "the LLM reads the index first when answering queries to find relevant pages. "
        "update it on every ingest.\n\n"
        "## log.md\n\n"
        "append-only chronological record. every entry starts with `## [YYYY-MM-DD] type | title` "
        "so it's parseable with simple tools. types: `ingest`, `query`, `lint`, `update`.\n\n"
        "## source tracking\n\n"
        "every fact should be traceable. use `sources:` frontmatter to list which raw files contributed to a page. "
        "timeline entries cite their source: `**2026-04-10** (from meeting-notes.md): key insight here`\n"
    ),
}


def scaffold_wiki(username, slug, template="freeform"):
    """create the initial skeleton commit in a wiki repo.
    creates: schema.md, index.md, log.md, raw/.gitkeep, wiki/.gitkeep, .wikihub/acl"""
    repo = _repo_path(username, slug)
    if not os.path.isdir(repo):
        return

    files = {
        ".wikihub/acl": (
            "# wikihub ACL — declarative access control for this wiki.\n"
            "#\n"
            "# Rules are glob patterns. Most-specific pattern wins. Default is private.\n"
            "#\n"
            "# Visibility: private | public | public-edit | unlisted | unlisted-edit\n"
            "# Grants:     @user:read | @user:edit\n"
            "#\n"
            "# Examples:\n"
            "#   * private                      # everything private (the default)\n"
            "#   wiki/** public                 # publish the wiki/ subtree\n"
            "#   wiki/secret.md private         # override: this one stays private\n"
            "#   wiki/collab.md public-edit     # anyone can edit this page\n"
            "#   drafts/** unlisted             # accessible by URL, not indexed\n"
            "#\n"
            "\n"
            "* private\n"
        ),
        "index.md": f"# {slug}\n\ncatalog of all wiki pages. update on every ingest.\n\n## pages\n\n(none yet)\n",
        "schema.md": SCHEMA_TEMPLATES.get(template, SCHEMA_TEMPLATES["freeform"]),
        "log.md": "# log\n\nappend-only record. format: `## [YYYY-MM-DD] type | title`\n",
        "raw/.gitkeep": "",
        "wiki/.gitkeep": "",
    }

    idx = tempfile.mktemp(prefix="wikihub-scaffold-", suffix=".idx")
    env = {"GIT_INDEX_FILE": idx}

    try:
        for fpath, content in files.items():
            blob = _git_bytes(repo, "hash-object", "-w", "--stdin", input=content.encode("utf-8"), env=env).strip().decode()
            _git(repo, "update-index", "--add", "--cacheinfo", "100644", blob, fpath, env=env)

        tree = _git(repo, "write-tree", env=env)
        commit = _git(
            repo, "commit-tree", tree,
            "-m", "Initial wiki scaffold",
            env={**env, **_AUTHOR_ENV},
        )
        _git(repo, "update-ref", "refs/heads/main", commit)
    finally:
        if os.path.exists(idx):
            os.unlink(idx)
