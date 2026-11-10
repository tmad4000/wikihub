#!/usr/bin/env python3
"""A2 targeted rehearsal against a new, local-only disposable PostgreSQL database."""
import importlib.util
import os
from pathlib import Path
import sys
import subprocess
import tempfile
from urllib.parse import urlsplit
import uuid

import psycopg2
from psycopg2 import sql

ROOT = Path(__file__).resolve().parents[1]
admin_url = os.environ.get('WORKER_TEST_DATABASE_URL', 'postgresql://localhost/postgres')
parts = urlsplit(admin_url)
if (parts.scheme != 'postgresql' or parts.hostname not in ('localhost', '127.0.0.1', '::1')
        or parts.path != '/postgres' or parts.query or parts.fragment):
    sys.exit('Refusing: use a postgresql loopback /postgres URL without query or fragment options')
# libpq environment routing must not override the validated loopback destination.
for variable in ('PGHOSTADDR', 'PGSERVICE', 'PGSERVICEFILE'):
    os.environ.pop(variable, None)
name = 'wikihub_a2_' + uuid.uuid4().hex[:12]
admin = psycopg2.connect(admin_url, connect_timeout=5)
admin.autocommit = True
with admin.cursor() as cursor:
    cursor.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
try:
    (ROOT / 'evidence').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='a2-repos-', dir=ROOT / 'evidence') as repos:
        os.environ.update(DATABASE_URL=parts._replace(path='/' + name).geturl(), REPOS_DIR=repos)
        spec = importlib.util.spec_from_file_location('wikihub_e2e', ROOT / 'tests/test_e2e.py')
        tests = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tests)
        app = tests.setup()
        functions = [
            ('rehearsal rejects URI routing overrides', lambda c, k: tests.test_activity_rehearsal_rejects_routing_options()),
            ('public proposal after 105 unlisted candidates', lambda c, k: tests.test_wiki_activity_proposals_survive_hidden_candidates(app, c, k)),
            ('all-viewer unlisted exclusion + direct access', lambda c, k: tests.test_wiki_activity_excludes_unlisted_for_all_viewers(app, c, k)),
            ('HTML private ACL + social events', tests.test_activity_feed_filters_private_and_shows_social_events),
            ('global activity visibility', tests.test_global_activity_excludes_non_public),
            ('RSS XML + host links', tests.test_activity_rss_is_well_formed_with_correct_links),
            ('private-only RSS metadata denial', tests.test_wiki_activity_rss_private_only_wiki_does_not_leak_metadata),
            ('older public entry after 305 hidden rows', tests.test_wiki_activity_rss_keeps_older_public_after_hidden_pages),
            ('unlisted reader sidebar still works', lambda c, k: tests.test_unlisted_page_in_sidebar_but_not_discovery(app, c, k)),
        ]
        with app.app_context():
            try:
                for label, function in functions:
                    tests.reset_database()
                    tests.db.session.remove()
                    tests._write_timestamps.clear(); tests._ip_write_timestamps.clear()
                    from flask import g
                    g.pop('_login_user', None)
                    client = app.test_client()
                    key = tests.test_agent_account_creation(client)
                    function(client, key)
                    print('PASS ' + label, flush=True)
                # Rehearse the production runbook's backup format on disposable data only.
                dump = str(Path(repos) / 'a2-backup.dump')
                subprocess.run(['pg_dump', '--format=custom', '--file', dump, os.environ['DATABASE_URL']], check=True)
                subprocess.run(['pg_restore', '--list', dump], check=True, stdout=subprocess.DEVNULL)
                print('PASS PostgreSQL custom backup archive readable', flush=True)
                print(f'{len(functions)} targeted checks plus backup archive check passed; disposable database only', flush=True)
            finally:
                tests.db.session.remove(); tests.db.engine.dispose()
finally:
    with admin.cursor() as cursor:
        cursor.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(name)))
    admin.close()
