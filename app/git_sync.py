"""
DB -> git sync for wikihub.

writes page content to the authoritative bare repo using git plumbing
(no working tree). does NOT fire hooks — this is the critical invariant
that prevents two-way-sync loops.

also handles public mirror regeneration: force-updates the public repo
to a single commit containing only public files with private bands stripped.

ported from listhub's git_sync.py with multi-wiki path generalization.
"""

import contextlib
import fcntl
import os
import json
import subprocess
import tempfile
from datetime import datetime, timezone

from flask import current_app

from app.content_utils import content_for_viewer, parse_markdown_document, strip_private_bands


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


_REF_UPDATE_ATTEMPTS = 5


class RefUpdateConflict(RuntimeError):
    """refs/heads/main moved under us on every retry (wikihub-xkmk)."""


@contextlib.contextmanager
def _repo_write_lock(repo_path):
    """Serialize WikiHub's own writers to one bare repo across threads and
    gunicorn workers (wikihub-xkmk). A git push does not take this lock; the
    compare-and-swap in `_cas_update_main` covers that case."""
    lock_path = os.path.join(repo_path, "wikihub-write.lock")
    with open(lock_path, "a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _cas_update_main(repo_path, new_commit, expected_old):
    """Move refs/heads/main to `new_commit` only if it still points at
    `expected_old` (None = ref must not exist yet). Returns False when another
    writer got there first, so the caller can rebuild on the new head instead
    of silently discarding that writer's commit."""
    try:
        _git(repo_path, "update-ref", "refs/heads/main", new_commit, expected_old or "")
        return True
    except subprocess.CalledProcessError:
        current = _head_commit(repo_path)
        if current == new_commit:
            return True
        if current != expected_old:
            return False
        raise


def apply_repo_changes(username, slug, changes, message, author_name="wikihub", author_email="sync@wikihub"):
    """apply one or more file writes/deletes to the authoritative repo in a single commit.

    Concurrency (wikihub-xkmk): writers hold a per-repo lock and move the ref
    with a compare-and-swap; if a push moved main meanwhile, the change set is
    re-applied on the new head rather than overwriting it.
    """
    repo = _repo_path(username, slug)
    if not os.path.isdir(repo):
        return False

    with _repo_write_lock(repo):
        for _attempt in range(_REF_UPDATE_ATTEMPTS):
            result = _apply_repo_changes_once(repo, changes, message, author_name, author_email)
            if result is not None:
                return result
    raise RefUpdateConflict(f"refs/heads/main kept moving in {username}/{slug}; gave up after {_REF_UPDATE_ATTEMPTS} attempts")


def _apply_repo_changes_once(repo, changes, message, author_name, author_email):
    """One read-tree → commit → CAS attempt. None means the ref moved; retry."""
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
        if not _cas_update_main(repo, new_commit, head):
            return None
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
    strips private files (per ACL), private bands, and .wikihub/* plumbing.
    force-updates to a single commit (under the mirror's write lock, with a
    compare-and-swap so a concurrent incremental update is not lost)."""
    auth_repo = _repo_path(username, slug)
    pub_repo = _repo_path(username, slug, public=True)

    if not os.path.isdir(auth_repo) or not os.path.isdir(pub_repo):
        return

    with _repo_write_lock(pub_repo):
        for _attempt in range(_REF_UPDATE_ATTEMPTS):
            if _regenerate_public_mirror_once(auth_repo, pub_repo, acl_rules):
                return
    raise RefUpdateConflict(f"public mirror ref kept moving in {username}/{slug}")


def _regenerate_public_mirror_once(auth_repo, pub_repo, acl_rules):
    """Build one snapshot; False means the mirror ref moved and we must retry."""
    from app.acl import resolve_visibility

    mirror_head = _head_commit(pub_repo)
    head = _head_commit(auth_repo)
    if not head:
        return True

    # list all regular files in HEAD (symlinks are never mirrored)
    try:
        mirror_paths = [path for mode, path in list_tree_entries(auth_repo, head) if mode != GIT_SYMLINK_MODE]
    except subprocess.CalledProcessError:
        return True

    idx = tempfile.mktemp(prefix="wikihub-mirror-", suffix=".idx")
    env = {"GIT_INDEX_FILE": idx}

    try:
        for filepath in mirror_paths:
            filepath = filepath.strip()
            if not filepath:
                continue

            # skip WikiHub plumbing files regardless of ACL defaults
            if filepath.startswith(".wikihub/"):
                continue

            # read file content from the same authoritative snapshot
            try:
                content_bytes = _git_bytes(auth_repo, "cat-file", "blob", f"{head}:{filepath}")
            except subprocess.CalledProcessError as e:
                print(f"WARNING: mirror skipped {filepath}: git cat-file failed: {e}")
                continue

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
                try:
                    content = strip_private_bands(content)
                except Exception as e:
                    # Fail CLOSED: never publish unstripped content. The file
                    # is simply absent from the mirror until stripping works.
                    print(f"WARNING: mirror skipped {filepath}: private-band stripping failed: {e}")
                    continue
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
        return _cas_update_main(pub_repo, new_commit, mirror_head)
    finally:
        if os.path.exists(idx):
            os.unlink(idx)


def update_mirror_page(username, slug, filepath, acl_rules=None, deleted=False):
    """incrementally update a single file in the public mirror.
    loads the existing mirror tree, adds/updates/removes just the one file,
    then writes a new commit. O(1) git ops instead of O(N files).
    falls back to full rebuild on any error."""
    try:
        _update_mirror_page_impl(username, slug, filepath, acl_rules, deleted)
    except Exception as e:
        print(f"WARNING: incremental mirror update failed for {username}/{slug}/{filepath}, falling back to full rebuild: {e}")
        regenerate_public_mirror(username, slug, acl_rules)


def _update_mirror_page_impl(username, slug, filepath, acl_rules=None, deleted=False):
    auth_repo = _repo_path(username, slug)
    pub_repo = _repo_path(username, slug, public=True)

    if not os.path.isdir(auth_repo) or not os.path.isdir(pub_repo):
        return

    with _repo_write_lock(pub_repo):
        _update_mirror_page_locked(auth_repo, pub_repo, filepath, acl_rules, deleted)


def _update_mirror_page_locked(auth_repo, pub_repo, filepath, acl_rules=None, deleted=False):
    from app.acl import resolve_visibility

    head = _head_commit(auth_repo)
    if not head:
        return

    idx = tempfile.mktemp(prefix="wikihub-mirror-inc-", suffix=".idx")
    env = {"GIT_INDEX_FILE": idx}

    try:
        # load current mirror state
        mirror_head = _head_commit(pub_repo)
        mirror_tree = _head_tree(pub_repo)
        if mirror_tree:
            _git(pub_repo, "read-tree", mirror_tree, env=env)

        if not deleted and not filepath.startswith(".wikihub/"):
            # symlinks are never mirrored (their target text would be published)
            try:
                entry = _git(auth_repo, "ls-tree", "HEAD", "--", filepath).strip()
            except subprocess.CalledProcessError:
                entry = ""
            if entry.startswith(GIT_SYMLINK_MODE + " "):
                deleted = True

        if deleted or filepath.startswith(".wikihub/"):
            # remove from mirror (ignore if file wasn't in mirror)
            try:
                _git(
                    pub_repo, "update-index", "--index-info", env=env,
                    input=f"0 {'0'*40}\t{filepath}\n",
                )
            except subprocess.CalledProcessError:
                pass
        else:
            # read file from auth repo
            try:
                content_bytes = _git_bytes(auth_repo, "cat-file", "blob", f"HEAD:{filepath}")
            except subprocess.CalledProcessError:
                return

            is_markdown = filepath.endswith(".md")
            fm_vis = None
            if is_markdown:
                content = content_bytes.decode("utf-8", errors="replace")
                frontmatter, _ = parse_markdown_document(content)
                fm_vis = frontmatter.get("visibility")

            vis = resolve_visibility(filepath, acl_rules or [], fm_vis)
            if vis == "private":
                # remove from mirror if it was there
                try:
                    _git(
                        pub_repo, "update-index", "--index-info", env=env,
                        input=f"0 {'0'*40}\t{filepath}\n",
                    )
                except subprocess.CalledProcessError:
                    pass
            else:
                if is_markdown:
                    # No try/except: a stripping failure must not publish the
                    # raw file. The exception propagates to update_mirror_page,
                    # whose full rebuild skips the file (fail closed).
                    content = strip_private_bands(content)
                    blob_input = content.encode("utf-8")
                else:
                    blob_input = content_bytes

                blob = _git_bytes(
                    pub_repo, "hash-object", "-w", "--stdin",
                    input=blob_input, env=env,
                ).strip().decode()
                _git(pub_repo, "update-index", "--add", "--cacheinfo",
                     "100644", blob, filepath, env=env)

        new_tree = _git(pub_repo, "write-tree", env=env)
        new_commit = _git(
            pub_repo, "commit-tree", new_tree,
            "-m", f"Update {filepath} @ {head[:12]}",
            env={**env, **_AUTHOR_ENV},
        )
        if not _cas_update_main(pub_repo, new_commit, mirror_head):
            # Another mirror writer moved the ref; update_mirror_page falls
            # back to a full rebuild, which reflects both changes.
            raise RefUpdateConflict(f"public mirror moved while updating {filepath}")
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


def read_file_for_viewer(username, slug, file_path, *, viewer_is_owner, public=False):
    """read_file_from_repo + content_for_viewer: the only safe way to hand
    repo markdown to a (possibly) non-owner. Band-stripped unless the viewer
    owns the wiki; raises (never falls back to raw) if stripping fails."""
    return content_for_viewer(
        read_file_from_repo(username, slug, file_path, public=public),
        viewer_is_owner=viewer_is_owner,
    )


def read_file_bytes_from_repo(username, slug, file_path, public=False):
    """read a file as raw bytes from HEAD of a bare repo (for binary files)."""
    repo = _repo_path(username, slug, public=public)
    if not os.path.isdir(repo):
        return None
    try:
        return _git_bytes(repo, "cat-file", "blob", f"HEAD:{file_path}")
    except subprocess.CalledProcessError:
        return None


GIT_SYMLINK_MODE = "120000"


def list_tree_entries(repo_path, rev="HEAD"):
    """Return [(mode, path)] for every blob-like entry in `rev` (recursive).
    Raises CalledProcessError if the rev is not readable."""
    output = _git_bytes(repo_path, "ls-tree", "-r", "-z", rev)
    entries = []
    for record in output.split(b"\0"):
        if not record:
            continue
        meta, _, path = record.partition(b"\t")
        mode = meta.split(b" ", 1)[0].decode("ascii", errors="replace")
        entries.append((mode, path.decode("utf-8", errors="surrogateescape")))
    return entries


def symlink_paths_in_tree(repo_path, rev):
    """Paths in `rev` stored as symbolic links (git mode 120000). WikiHub
    content is plain files only; pushes containing symlinks are rejected by
    the pre-receive hook and any that exist are skipped when indexing."""
    return [path for mode, path in list_tree_entries(repo_path, rev) if mode == GIT_SYMLINK_MODE]


def list_files_in_repo(username, slug, public=False):
    """list all regular files in HEAD of a bare repo (symlinks excluded)."""
    repo = _repo_path(username, slug, public=public)
    if not os.path.isdir(repo):
        return []
    try:
        return [path for mode, path in list_tree_entries(repo) if mode != GIT_SYMLINK_MODE and path.strip()]
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
        "visibility: public | public-edit | private | unlisted | unlisted-edit\n"
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
            "# Visibility: private | public-view | public-edit | unlisted-view | unlisted-edit\n"
            "# Aliases:    public -> public-view, unlisted -> unlisted-view\n"
            "# Frontmatter stores page visibility: public | public-edit | private | unlisted | unlisted-edit\n"
            "# Grants:     @user:read | @user:edit\n"
            "#\n"
            "# Examples:\n"
            "#   * private                      # everything private (the default)\n"
            "#   wiki/** public-view            # publish the wiki/ subtree\n"
            "#   wiki/secret.md private         # override: this one stays private\n"
            "#   wiki/collab.md public-edit     # anyone can edit this page\n"
            "#   drafts/** unlisted-view        # accessible by URL, not indexed\n"
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
