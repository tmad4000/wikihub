#!/usr/bin/env python3
"""
Migrate ListHub: merge @jacobcole into @jacobreal, restructure paths, dedup.

Phases:
  1. Identify unique @jacobcole items not on @jacobreal
  2. Copy them to @jacobreal via API POST (with restructured file_paths)
  3. Restructure existing @jacobreal items (update file_path in DB + git)
  4. Merge SI joint duplicates

Run on production server: python3 migrate_listhub.py --dry-run
"""
import http.client
import json
import os
import sqlite3
import subprocess
import sys
import time

DRY_RUN = "--dry-run" in sys.argv
DB_PATH = "/home/ubuntu/listhub/listhub.db"
REPOS_DIR = "/home/ubuntu/listhub/repos"
HOST = "listhub.globalbr.ai"
ADMIN_TOKEN = "ec5107d6eb59357642976d61aafad1ad"
TARGET_USER = "jacobreal"

# ── Path restructuring rules ──────────────────────────────────────────
# Maps old file_path → new file_path for existing @jacobreal items
RESTRUCTURE_MAP = {
    # Health grouping
    "adhd.md": "health/adhd.md",
    "chronicpain/chronicpain.md": "health/chronicpain/index.md",
    "chronicpain/backpain.md": None,  # DUPLICATE of si.md — delete
    "chronicpain/sijointpain.md": None,  # DUPLICATE of si.md — delete
    "chronicpain/si.md": "health/chronicpain/si-joint.md",  # canonical
    "chronicpain/sleep.md": "health/chronicpain/sleep.md",
    "depression-resources.md": "health/depression-resources.md",
    "supplements.md": "health/supplements.md",
    "meditation.md": "health/meditation.md",
    "hypnosis.md": "health/hypnosis.md",
    "buoyantfitness.md": "health/buoyantfitness.md",
    "body-masters.md": "health/body-masters.md",
    "qigongcrew.md": "health/qigongcrew.md",
    "qiresearch.md": "health/qiresearch.md",
    "lifechange.md": "health/lifechange.md",
    "india-remote-sleep-hacking.md": "health/india-remote-sleep-hacking.md",
    "nvc.md": "health/nvc.md",

    # Ideaflow grouping
    "ideaflowbackground.md": "ideaflow/ideaflowbackground.md",
    "ideaflowproject.md": "ideaflow/ideaflowproject.md",
    # ideaflow/gestaltexplanation.md and ideaflow/ifiran.md already nested

    # Ideas grouping
    "startupideas.md": "ideas/startupideas.md",
    "startuptrickswiki.md": "ideas/startuptrickswiki.md",
    "idea-bank.md": "ideas/idea-bank.md",
    "moltbook-idea-bank.md": "ideas/moltbook-idea-bank.md",
    "covid19hackideas.md": "ideas/covid19hackideas.md",
    "confidence-gated-actions-idea.md": "ideas/confidence-gated-actions-idea.md",

    # Hiring grouping
    "hiringblurb.md": "hiring/hiringblurb.md",
    "hiringlist.md": "hiring/hiringlist.md",

    # Infrastructure → worldquestguild (per breadcrumbs)
    "infrastructure/infrastructure.md": "worldquestguild/infrastructure/index.md",
    "infrastructure/codex.md": "worldquestguild/infrastructure/codex.md",
    "infrastructure/templates.md": "worldquestguild/infrastructure/templates.md",
    "autodidacts.md": "worldquestguild/autodidacts.md",

    # Junk/meta — mark for deletion
    "codex-listhub-sync-probe-1770905898.md": None,
    "test-from-beta.md": None,
    "review-todo-imported-items.md": None,
    "ticket-default-project.md": None,
    "skill-installation-plan.md": None,
    "beads-how-to-create-ticket.md": None,
    "silence-behavior.md": None,
    "note-trigger-phrases.md": None,
    "notes-storage-preference.md": None,
    "notes-daily-log-format.md": None,
    "note-technology-better-more-organized.md": None,
}

# Path rules for @jacobcole items being moved to @jacobreal
# Only for items that DON'T already exist on @jacobreal
JACOBCOLE_PATH_MAP = {
    # Hiring
    "chief-of-staff-roadflex.md": "hiring/chief-of-staff-roadflex.md",
    "fullstack-engineer-roadflex.md": "hiring/fullstack-engineer-roadflex.md",
    "risk-lead-roadflex.md": "hiring/risk-lead-roadflex.md",
    "roadflex-culture.md": "hiring/roadflex-culture.md",
    "grant-goldman-resume.md": "hiring/grant-goldman-resume.md",

    # Health
    "arm-exercises.md": "health/arm-exercises.md",
    "mentalstatehacks.md": "health/mentalstatehacks.md",
    "supplements-connectordoc.md": "health/supplements-connectordoc.md",
    "dharma-retreat-list.md": "health/dharma-retreat-list.md",
    "spiritual-growth-hawaii.md": "health/spiritual-growth-hawaii.md",

    # Ideaflow
    "ideaflowplan.md": "ideaflow/ideaflowplan.md",
    "ideacast.md": "ideaflow/ideacast.md",
    "ideajoin.md": "ideaflow/ideajoin.md",
    "intelligence-and-gestalts.md": "ideaflow/intelligence-and-gestalts.md",

    # Ideas
    "ideas-from-the-past.md": "ideas/ideas-from-the-past.md",
    "idea-matching-algorithm.md": "ideas/idea-matching-algorithm.md",
    "fixing-the-internet.md": "ideas/fixing-the-internet.md",
    "fixingtheinternet.md": None,  # dupe of above
    "thoughtstreaming.md": "ideas/thoughtstreaming.md",

    # MIT
    "mitdoc.md": "mitdocs/mitdoc.md",
    "mitdocs-connectordoc.md": "mitdocs/mitdocs-connectordoc.md",
    "mit-classes-collab.md": "mitdocs/mit-classes-collab.md",
    "tedx-mit-outline.md": "mitdocs/tedx-mit-outline.md",

    # Things you didn't know existed (extra cities)
    "costa-rica-things.md": "thingsyoudidntknowexisted/costa-rica.md",
    "hawaiithingsyoudidntknowexisted.md": None,  # dupe — already in folder
    "thingsyoudidntknowexistedinsandiego.md": None,  # dupe — already in folder

    # Chronic pain dupes (already on jacobreal)
    "chronicpain/chronicpain.md": None,
    "chronicpain/backpain.md": None,
    "chronicpain/si.md": None,
    "chronicpain/sijointpain.md": None,
    "chronicpain/sleep.md": None,
}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_items(conn, username):
    """Get all items for a user."""
    user = conn.execute("SELECT id FROM user WHERE username = ?", (username,)).fetchone()
    if not user:
        return []
    items = conn.execute(
        "SELECT i.*, GROUP_CONCAT(t.tag) as tags_str "
        "FROM item i LEFT JOIN item_tag t ON i.id = t.item_id "
        "WHERE i.owner_id = ? GROUP BY i.id",
        (user['id'],)
    ).fetchall()
    return [dict(i) for i in items]


def api_post(path, body):
    """POST to ListHub API."""
    conn = http.client.HTTPSConnection(HOST, timeout=30)
    headers = {
        "Authorization": f"Bearer {ADMIN_TOKEN}",
        "X-ListHub-User": TARGET_USER,
        "Content-Type": "application/json",
    }
    try:
        conn.request("POST", path, json.dumps(body), headers)
        resp = conn.getresponse()
        return resp.status, resp.read().decode()
    finally:
        conn.close()


def api_delete(item_id):
    """DELETE item via API."""
    conn = http.client.HTTPSConnection(HOST, timeout=30)
    headers = {
        "Authorization": f"Bearer {ADMIN_TOKEN}",
        "X-ListHub-User": TARGET_USER,
    }
    try:
        conn.request("DELETE", f"/api/v1/items/{item_id}", None, headers)
        resp = conn.getresponse()
        return resp.status, resp.read().decode()
    finally:
        conn.close()


def git_rename_file(repo_path, old_path, new_path, content=None):
    """Rename a file in a bare git repo using plumbing commands."""
    env = dict(os.environ)
    env['GIT_DIR'] = repo_path

    # If content not provided, read from old path
    if content is None:
        try:
            result = subprocess.run(
                ['git', 'show', f'HEAD:{old_path}'],
                capture_output=True, text=True, env=env
            )
            if result.returncode != 0:
                return False
            content = result.stdout
        except Exception:
            return False

    # Create blob
    blob = subprocess.run(
        ['git', 'hash-object', '-w', '--stdin'],
        input=content, capture_output=True, text=True, env=env
    )
    blob_hash = blob.stdout.strip()

    # Read current tree
    tree_result = subprocess.run(
        ['git', 'ls-tree', '-r', 'HEAD'],
        capture_output=True, text=True, env=env
    )

    # Build new index
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.idx', delete=False) as f:
        idx_file = f.name
    env['GIT_INDEX_FILE'] = idx_file

    try:
        # Read tree into temp index
        subprocess.run(['git', 'read-tree', 'HEAD'], env=env, check=True)

        # Remove old path
        subprocess.run(
            ['git', 'update-index', '--remove', old_path],
            env=env, capture_output=True
        )

        # Add new path
        subprocess.run(
            ['git', 'update-index', '--add', '--cacheinfo',
             '100644', blob_hash, new_path],
            env=env, check=True
        )

        # Write tree
        tree = subprocess.run(
            ['git', 'write-tree'], capture_output=True, text=True, env=env
        )
        tree_hash = tree.stdout.strip()

        # Get parent commit
        parent = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], capture_output=True, text=True,
            env={k: v for k, v in env.items() if k != 'GIT_INDEX_FILE'}
        )
        parent_hash = parent.stdout.strip()

        # Create commit
        commit_env = dict(env)
        commit_env['GIT_AUTHOR_NAME'] = 'ListHub Migration'
        commit_env['GIT_AUTHOR_EMAIL'] = 'migration@listhub.globalbr.ai'
        commit_env['GIT_COMMITTER_NAME'] = 'ListHub Migration'
        commit_env['GIT_COMMITTER_EMAIL'] = 'migration@listhub.globalbr.ai'

        commit = subprocess.run(
            ['git', 'commit-tree', tree_hash, '-p', parent_hash,
             '-m', f'Restructure: {old_path} → {new_path}'],
            capture_output=True, text=True, env=commit_env
        )
        commit_hash = commit.stdout.strip()

        # Update ref
        del env['GIT_INDEX_FILE']
        subprocess.run(
            ['git', 'update-ref', 'refs/heads/main', commit_hash],
            env=env, check=True
        )
        # Also update HEAD if it's not main
        subprocess.run(
            ['git', 'update-ref', 'HEAD', commit_hash],
            env=env, capture_output=True
        )

        return True
    finally:
        if os.path.exists(idx_file):
            os.unlink(idx_file)


def git_remove_file(repo_path, file_path):
    """Remove a file from a bare git repo."""
    env = dict(os.environ)
    env['GIT_DIR'] = repo_path

    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.idx', delete=False) as f:
        idx_file = f.name
    env['GIT_INDEX_FILE'] = idx_file

    try:
        subprocess.run(['git', 'read-tree', 'HEAD'], env=env, check=True)
        subprocess.run(
            ['git', 'update-index', '--remove', file_path],
            env=env, capture_output=True
        )
        tree = subprocess.run(
            ['git', 'write-tree'], capture_output=True, text=True, env=env
        )
        tree_hash = tree.stdout.strip()

        parent = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], capture_output=True, text=True,
            env={k: v for k, v in env.items() if k != 'GIT_INDEX_FILE'}
        )
        parent_hash = parent.stdout.strip()

        commit_env = dict(env)
        commit_env['GIT_AUTHOR_NAME'] = 'ListHub Migration'
        commit_env['GIT_AUTHOR_EMAIL'] = 'migration@listhub.globalbr.ai'
        commit_env['GIT_COMMITTER_NAME'] = 'ListHub Migration'
        commit_env['GIT_COMMITTER_EMAIL'] = 'migration@listhub.globalbr.ai'

        commit = subprocess.run(
            ['git', 'commit-tree', tree_hash, '-p', parent_hash,
             '-m', f'Remove: {file_path}'],
            capture_output=True, text=True, env=commit_env
        )
        commit_hash = commit.stdout.strip()

        del env['GIT_INDEX_FILE']
        subprocess.run(
            ['git', 'update-ref', 'refs/heads/main', commit_hash],
            env=env, check=True
        )
        subprocess.run(
            ['git', 'update-ref', 'HEAD', commit_hash],
            env=env, capture_output=True
        )
        return True
    finally:
        if os.path.exists(idx_file):
            os.unlink(idx_file)


def main():
    print(f"{'DRY RUN' if DRY_RUN else 'LIVE RUN'}", file=sys.stderr)

    conn = get_db()
    jr_items = get_items(conn, "jacobreal")
    jc_items = get_items(conn, "jacobcole")

    jr_by_path = {i['file_path']: i for i in jr_items if i['file_path']}
    jr_by_slug = {i['slug']: i for i in jr_items}
    jc_by_path = {i['file_path']: i for i in jc_items if i['file_path']}

    print(f"\n@jacobreal: {len(jr_items)} items", file=sys.stderr)
    print(f"@jacobcole: {len(jc_items)} items", file=sys.stderr)

    repo_path = os.path.join(REPOS_DIR, "jacobreal.git")

    # ── PHASE 1: Identify unique @jacobcole items ──
    print("\n=== PHASE 1: Unique @jacobcole items to migrate ===", file=sys.stderr)
    to_migrate = []
    skipped_dupes = []
    skipped_junk = []

    for item in jc_items:
        fp = item['file_path'] or f"{item['slug']}.md"
        slug = item['slug']

        # Check if it's a known dupe/junk from our map
        if fp in JACOBCOLE_PATH_MAP and JACOBCOLE_PATH_MAP[fp] is None:
            skipped_junk.append(f"  SKIP (mapped None): {fp}")
            continue

        # Check if same file_path exists on jacobreal
        if fp in jr_by_path:
            skipped_dupes.append(f"  DUPE (path): {fp}")
            continue

        # Check if same slug exists on jacobreal
        if slug in jr_by_slug:
            skipped_dupes.append(f"  DUPE (slug): {slug}")
            continue

        # Determine target path
        new_path = JACOBCOLE_PATH_MAP.get(fp, fp)
        if new_path is None:
            skipped_junk.append(f"  SKIP (mapped None): {fp}")
            continue

        to_migrate.append((item, new_path))

    print(f"\n  To migrate: {len(to_migrate)}", file=sys.stderr)
    print(f"  Skipped (dupes): {len(skipped_dupes)}", file=sys.stderr)
    print(f"  Skipped (junk/mapped-None): {len(skipped_junk)}", file=sys.stderr)

    for d in skipped_dupes[:10]:
        print(d, file=sys.stderr)
    for j in skipped_junk:
        print(j, file=sys.stderr)

    print("\n  Items to migrate:", file=sys.stderr)
    for item, new_path in to_migrate:
        print(f"    {item['file_path']} → {new_path}  [{item['title']}]", file=sys.stderr)

    # ── PHASE 2: Restructure existing @jacobreal items ──
    print("\n=== PHASE 2: Restructure @jacobreal paths ===", file=sys.stderr)
    to_restructure = []
    to_delete = []

    for item in jr_items:
        fp = item['file_path'] or f"{item['slug']}.md"
        if fp in RESTRUCTURE_MAP:
            new_path = RESTRUCTURE_MAP[fp]
            if new_path is None:
                to_delete.append((item, fp))
            else:
                to_restructure.append((item, fp, new_path))

    print(f"\n  To restructure: {len(to_restructure)}", file=sys.stderr)
    for item, old, new in to_restructure:
        print(f"    {old} → {new}  [{item['title']}]", file=sys.stderr)

    print(f"\n  To delete (dupes/junk): {len(to_delete)}", file=sys.stderr)
    for item, fp in to_delete:
        print(f"    DELETE: {fp}  [{item['title']}]", file=sys.stderr)

    if DRY_RUN:
        print("\n=== DRY RUN — no changes made ===", file=sys.stderr)
        # Output JSON summary
        print(json.dumps({
            "migrate": [(i['title'], i['file_path'], np) for i, np in to_migrate],
            "restructure": [(i['title'], old, new) for i, old, new in to_restructure],
            "delete": [(i['title'], fp) for i, fp in to_delete],
            "skipped_dupes": len(skipped_dupes),
            "skipped_junk": len(skipped_junk),
        }, indent=2))
        return

    # ── EXECUTE PHASE 1: Migrate unique items ──
    print("\n=== EXECUTING PHASE 1: Migrate items ===", file=sys.stderr)
    migrated = 0
    errors = 0

    for item, new_path in to_migrate:
        body = {
            "title": item['title'],
            "slug": item['slug'],
            "content": item['content'] or "",
            "file_path": new_path,
            "item_type": item['item_type'] or "note",
            "visibility": item['visibility'] or "private",
            "tags": item['tags_str'].split(',') if item['tags_str'] else [],
        }
        status, resp = api_post("/api/v1/items/new", body)
        if 200 <= status < 300:
            migrated += 1
            print(f"  OK: {new_path}", file=sys.stderr)
        else:
            errors += 1
            print(f"  ERR {status}: {new_path} — {resp[:150]}", file=sys.stderr)
        time.sleep(0.1)

    print(f"\n  Migrated: {migrated}, Errors: {errors}", file=sys.stderr)

    # ── EXECUTE PHASE 2: Restructure paths ──
    print("\n=== EXECUTING PHASE 2: Restructure paths ===", file=sys.stderr)
    restructured = 0

    for item, old_path, new_path in to_restructure:
        # Update DB
        conn.execute(
            "UPDATE item SET file_path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_path, item['id'])
        )
        # Update git
        ok = git_rename_file(repo_path, old_path, new_path)
        if ok:
            restructured += 1
            print(f"  OK: {old_path} → {new_path}", file=sys.stderr)
        else:
            print(f"  GIT ERR: {old_path} → {new_path}", file=sys.stderr)

    conn.commit()
    print(f"\n  Restructured: {restructured}", file=sys.stderr)

    # ── EXECUTE PHASE 3: Delete dupes/junk ──
    print("\n=== EXECUTING PHASE 3: Delete dupes/junk ===", file=sys.stderr)
    deleted = 0

    for item, fp in to_delete:
        # Delete from DB
        conn.execute("DELETE FROM item_tag WHERE item_id = ?", (item['id'],))
        conn.execute("DELETE FROM item WHERE id = ?", (item['id'],))
        # Remove from git
        git_remove_file(repo_path, fp)
        deleted += 1
        print(f"  DEL: {fp}", file=sys.stderr)

    conn.commit()
    print(f"\n  Deleted: {deleted}", file=sys.stderr)

    # ── Summary ──
    print(f"\n=== DONE ===", file=sys.stderr)
    print(f"  Migrated from @jacobcole: {migrated}", file=sys.stderr)
    print(f"  Restructured paths: {restructured}", file=sys.stderr)
    print(f"  Deleted dupes/junk: {deleted}", file=sys.stderr)

    # Verify final count
    final_items = get_items(conn, "jacobreal")
    print(f"  Final @jacobreal item count: {len(final_items)}", file=sys.stderr)

    conn.close()


if __name__ == "__main__":
    main()
