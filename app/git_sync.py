"""
DB -> git sync for wikihub.

writes page content from the database to the authoritative bare repo
using git plumbing (no working tree). does NOT fire hooks — this is
the critical invariant that prevents two-way-sync loops.

also handles public mirror regeneration: force-updates the public repo
to a single commit containing only public files with private bands stripped.

ported from listhub's git_sync.py with multi-wiki path generalization.
"""

import os
import re
import subprocess
import tempfile

from flask import current_app


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


def sync_page_to_repo(username, slug, file_path, content):
    """incremental sync: write a single page to the authoritative repo.
    call after creating or updating a page via the web/API."""
    repo = _repo_path(username, slug)
    if not os.path.isdir(repo):
        return

    head = _head_commit(repo)
    idx = tempfile.mktemp(prefix="wikihub-sync-", suffix=".idx")
    env = {"GIT_INDEX_FILE": idx}

    try:
        if head:
            _git(repo, "read-tree", "HEAD", env=env)

        blob = _git_bytes(
            repo, "hash-object", "-w", "--stdin",
            input=content.encode("utf-8"), env=env,
        ).strip().decode()

        _git(repo, "update-index", "--add", "--cacheinfo",
             "100644", blob, file_path, env=env)

        new_tree = _git(repo, "write-tree", env=env)
        old_tree = _head_tree(repo) if head else None
        if new_tree == old_tree:
            return

        commit_args = ["commit-tree", new_tree, "-m", f"Update {file_path}"]
        if head:
            commit_args[2:2] = ["-p", head]

        new_commit = _git(repo, *commit_args, env={**env, **_AUTHOR_ENV})
        _git(repo, "update-ref", "refs/heads/main", new_commit)
    finally:
        if os.path.exists(idx):
            os.unlink(idx)


def remove_page_from_repo(username, slug, file_path):
    """remove a single file from the authoritative repo."""
    repo = _repo_path(username, slug)
    if not os.path.isdir(repo):
        return

    head = _head_commit(repo)
    if not head:
        return

    idx = tempfile.mktemp(prefix="wikihub-sync-", suffix=".idx")
    env = {"GIT_INDEX_FILE": idx}

    try:
        _git(repo, "read-tree", "HEAD", env=env)
        _git(repo, "update-index", "--index-info", env=env,
             input=f"0 {'0'*40}\t{file_path}\n")

        new_tree = _git(repo, "write-tree", env=env)
        old_tree = _head_tree(repo)
        if new_tree == old_tree:
            return

        new_commit = _git(
            repo, "commit-tree", new_tree, "-p", head,
            "-m", f"Remove {file_path}",
            env={**env, **_AUTHOR_ENV},
        )
        _git(repo, "update-ref", "refs/heads/main", new_commit)
    finally:
        if os.path.exists(idx):
            os.unlink(idx)


# --- private band stripping ---

PRIVATE_OPEN = re.compile(r"<!--\s*private\s*-->", re.IGNORECASE)
PRIVATE_CLOSE = re.compile(r"<!--\s*/private\s*-->", re.IGNORECASE)


def strip_private_bands(content):
    """strip <!-- private -->...<!-- /private --> bands from markdown content.
    unclosed bands fail closed (everything after the opener is private)."""
    result = []
    in_private = False
    for line in content.split("\n"):
        if not in_private and PRIVATE_OPEN.search(line):
            in_private = True
            continue
        if in_private and PRIVATE_CLOSE.search(line):
            in_private = False
            continue
        if not in_private:
            result.append(line)
    return "\n".join(result)


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

            # check visibility via ACL
            if acl_rules:
                vis = resolve_visibility(filepath, acl_rules)
                if vis == "private":
                    continue

            # read file content from authoritative repo
            content_bytes = _git_bytes(auth_repo, "cat-file", "blob", f"HEAD:{filepath}")
            content = content_bytes.decode("utf-8", errors="replace")

            # strip private bands from markdown files
            if filepath.endswith(".md"):
                content = strip_private_bands(content)

            # write to public mirror index
            blob = _git_bytes(
                pub_repo, "hash-object", "-w", "--stdin",
                input=content.encode("utf-8"), env=env,
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


def scaffold_wiki(username, slug):
    """create the initial Karpathy skeleton commit in a wiki repo.
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
        "index.md": f"# {slug}\n\nwelcome to your wiki.\n",
        "schema.md": "# schema\n\ndescribe the structure of your knowledge base here.\n",
        "log.md": "# log\n\nchronological updates and decisions.\n",
        "raw/.gitkeep": "",
        "wiki/.gitkeep": "",
    }

    idx = tempfile.mktemp(prefix="wikihub-scaffold-", suffix=".idx")
    env = {"GIT_INDEX_FILE": idx}

    try:
        for fpath, content in files.items():
            blob = _git_bytes(
                repo, "hash-object", "-w", "--stdin",
                input=content.encode("utf-8"), env=env,
            ).strip().decode()
            _git(repo, "update-index", "--add", "--cacheinfo",
                 "100644", blob, fpath, env=env)

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
