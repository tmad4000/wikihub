#!/usr/bin/env python3
"""
Reinstall git hooks and hooks/wikihub.conf for every wiki repo.

Run after deploying hook changes or rotating HOOK_TOKEN / ADMIN_TOKEN, with
the same environment the app uses (it reads HOOK_TOKEN, ADMIN_TOKEN,
BASE_URL and REPOS_DIR from config):

    source .venv/bin/activate
    set -a; . /opt/wikihub-app/.env; set +a
    python3 scripts/reinstall_hooks.py            # all wikis
    python3 scripts/reinstall_hooks.py --dry-run  # list only

Existing repos keep whatever hooks they were created with until this runs.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def reinstall_all(dry_run=False):
    from app.git_backend import _install_hook, _repo_path
    from app.models import User, Wiki

    count = 0
    for wiki in Wiki.query.order_by(Wiki.id).all():
        owner = User.query.get(wiki.owner_id)
        if not owner:
            continue
        repo = _repo_path(owner.username, wiki.slug)
        if not os.path.isdir(repo):
            continue
        if not dry_run:
            _install_hook(repo, owner.username, wiki.slug)
        count += 1
    return count


def main():
    from app import create_app

    dry_run = "--dry-run" in sys.argv
    app = create_app()
    with app.app_context():
        n = reinstall_all(dry_run=dry_run)
    print(f"{'would reinstall' if dry_run else 'reinstalled'} hooks for {n} repos")


if __name__ == "__main__":
    main()
