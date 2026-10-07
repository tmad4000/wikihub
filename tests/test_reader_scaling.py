"""Real reader requests must not hydrate every page in a large wiki."""
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ReaderScalingTest(unittest.TestCase):
    def test_large_reader_history_and_navigation_keep_private_content_hidden(self):
        import psycopg2
        from psycopg2 import sql
        from sqlalchemy import event
        admin_url = os.environ['WORKER_TEST_DATABASE_URL']
        name = 'wikihub_reader_' + uuid.uuid4().hex[:12]
        admin = psycopg2.connect(admin_url)
        admin.autocommit = True
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        try:
            with tempfile.TemporaryDirectory(prefix='wikihub-reader-') as repos:
                os.environ.update(DATABASE_URL=urlsplit(admin_url)._replace(path='/' + name).geturl(),
                                  REPOS_DIR=repos, SECRET_KEY='reader-scale-test', SESSION_COOKIE_SECURE='0')
                from app import create_app, db
                from app.models import Page, User, Wiki, utcnow
                app = create_app()
                app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, IDEAFLOW_AUTO_SIGNIN_ENABLED=False)
                with app.test_client() as client:
                    account = client.post('/api/v1/accounts', json={'username': 'reader-scale'})
                    self.assertEqual(account.status_code, 201)
                    headers = {'Authorization': 'Bearer ' + account.json['api_key']}
                    self.assertEqual(client.post('/api/v1/wikis', json={'slug': 'large'}, headers=headers).status_code, 201)
                    for path, visibility, content in [('index.md', 'public', '# PUBLIC READER'),
                                                      ('secret.md', 'private', '# SECRET READER'),
                                                      ('by-link.md', 'unlisted', '# LINK READER')]:
                        if path == 'index.md':
                            response = client.put('/api/v1/wikis/reader-scale/large/pages/index.md', headers=headers,
                                                  json={'visibility': visibility, 'content': content})
                            self.assertEqual(response.status_code, 200, response.data)
                        else:
                            response = client.post('/api/v1/wikis/reader-scale/large/pages', headers=headers,
                                                   json={'path': path, 'visibility': visibility, 'content': content})
                            self.assertEqual(response.status_code, 201, response.data)
                    with app.app_context():
                        owner = User.query.filter_by(username='reader-scale').one()
                        wiki = Wiki.query.filter_by(owner_id=owner.id, slug='large').one()
                        wiki_id = wiki.id
                        # Exceed production's largest wiki. No per-page git sync is
                        # needed: these rows exercise metadata navigation, while the
                        # actual reader/history use the real committed pages above.
                        stamp = utcnow()
                        db.session.add_all([Page(wiki_id=wiki_id, path=f'bulk/{i:04d}.md',
                                                  title=f'PUBLIC BULK {i}', visibility='public',
                                                  updated_at=stamp) for i in range(4000)])
                        # A newer normalized plumbing row must not displace readable
                        # recent links or accidentally authorize a private-only wiki.
                        db.session.add(Page(wiki_id=wiki_id, path='x/../.wikihub/hidden.md',
                                            title='HIDDEN PLUMBING', visibility='public'))
                        locked = Wiki(owner_id=owner.id, slug='locked')
                        db.session.add(locked); db.session.flush()
                        db.session.add_all([
                            Page(wiki_id=locked.id, path='private.md', visibility='private'),
                            Page(wiki_id=locked.id, path='x/../.wikihub/hidden.md', visibility='public'),
                        ])
                        db.session.commit()
                        # Real full-text payloads make fetching unused vectors a
                        # measurable data-volume regression, including sidebar.json.
                        db.session.execute(db.text("UPDATE pages SET search_vector=to_tsvector('simple', :document) WHERE wiki_id=:wiki AND path LIKE 'bulk/%'"),
                                           {'wiki': wiki_id, 'document': ' '.join(f'fixture{i}' for i in range(250))})
                        db.session.commit()
                        db.session.remove()
                    for path in ('/@reader-scale/large', '/@reader-scale/large/history',
                                 '/@reader-scale/large/index/history', '/@reader-scale/large/by-link',
                                 '/@reader-scale/large/sidebar.json'):
                        loaded = []
                        vector_bytes = []
                        def capture(page, context):
                            loaded.append(page.path)
                            if page.__dict__.get('search_vector'):
                                vector_bytes.append(len(str(page.__dict__['search_vector']).encode()))
                        event.listen(Page, 'load', capture)
                        try:
                            response = client.get(path)
                        finally:
                            event.remove(Page, 'load', capture)
                        self.assertEqual(response.status_code, 200, path)
                        self.assertNotIn(b'SECRET READER', response.data)
                        self.assertNotIn(b'HIDDEN PLUMBING', response.data)
                        self.assertLessEqual(sum(vector_bytes), 50_000, f'{path} fetched {sum(vector_bytes)} unused vector bytes')
                        if path == '/@reader-scale/large':
                            self.assertLess(response.data.index(b'PUBLIC BULK 3999'), response.data.index(b'PUBLIC BULK 3998'))
                        # The async sidebar manifest intentionally includes all
                        # readable metadata; ordinary readers/history do not need
                        # thousands of ORM objects or large search vectors.
                        if not path.endswith('sidebar.json'):
                            self.assertLessEqual(len(loaded), 12, f'{path} hydrated {len(loaded)} pages')
                    self.assertEqual(client.get('/@reader-scale/large/secret').status_code, 403)
                    self.assertEqual(client.get('/@reader-scale/locked/history').status_code, 401)
                    with app.app_context():
                        db.session.remove()
                        db.engine.dispose()
        finally:
            with admin.cursor() as cursor:
                cursor.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(name)))
            admin.close()


if __name__ == '__main__':
    unittest.main()
