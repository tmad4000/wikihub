"""Per-path sharing must not disclose other private indexes or recent titles."""
import os
import sys
import tempfile
import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ReaderAclPrivacyTest(unittest.TestCase):
    def test_anonymous_member_partial_grantee_and_owner(self):
        import psycopg2
        from psycopg2 import sql
        from sqlalchemy import event
        admin_url = os.environ['WORKER_TEST_DATABASE_URL']
        name = 'wikihub_reader_acl_' + uuid.uuid4().hex[:12]
        admin = psycopg2.connect(admin_url)
        admin.autocommit = True
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        try:
            with tempfile.TemporaryDirectory(prefix='wikihub-reader-acl-') as repos:
                os.environ.update(DATABASE_URL=urlsplit(admin_url)._replace(path='/' + name).geturl(),
                                  REPOS_DIR=repos, SECRET_KEY='reader-acl-test', SESSION_COOKIE_SECURE='0')
                from app import create_app, db
                from app.models import Page, User, Wiki, utcnow
                app = create_app()
                app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, IDEAFLOW_AUTO_SIGNIN_ENABLED=False)
                setup = app.test_client()
                accounts = {}
                for username in ('acl-owner', 'acl-viewer', 'acl-member'):
                    response = setup.post('/api/v1/accounts', json={'username': username})
                    self.assertEqual(response.status_code, 201)
                    accounts[username] = response.json
                headers = {'Authorization': 'Bearer ' + accounts['acl-owner']['api_key']}
                self.assertEqual(setup.post('/api/v1/wikis', json={'slug': 'shared'}, headers=headers).status_code, 201)
                response = setup.put('/api/v1/wikis/acl-owner/shared/pages/index.md', headers=headers,
                                     json={'visibility': 'private', 'content': '# UNSHARED ROOT CONTENT'})
                self.assertEqual(response.status_code, 200)
                for path, visibility, title in [
                    ('README.md', 'public', 'PUBLIC FALLBACK'),
                    ('folder/index.md', 'private', 'UNSHARED FOLDER CONTENT'),
                    ('folder/allowed.md', 'private', 'SHARED LEAF'),
                    ('secret.md', 'private', 'UNSHARED SECRET TITLE'),
                    ('by-link.md', 'unlisted', 'LINK BY URL'),
                ]:
                    response = setup.post('/api/v1/wikis/acl-owner/shared/pages', headers=headers,
                                          json={'path': path, 'visibility': visibility,
                                                'content': f'---\ntitle: {title}\nvisibility: {visibility}\n---\n# {title}'})
                    self.assertEqual(response.status_code, 201, response.data)
                acl = 'folder/allowed.md @acl-viewer:read\nauthorized/* @acl-viewer:read\n'
                self.assertEqual(setup.post('/api/v1/wikis/acl-owner/shared/pages', headers=headers,
                                           json={'path': '.wikihub/acl', 'content': acl}).status_code, 201)
                with app.app_context():
                    ids = {user.username: str(user.id) for user in User.query.filter(User.username.in_(accounts)).all()}
                    wiki = Wiki.query.filter_by(owner_id=int(ids['acl-owner']), slug='shared').one()
                    newer = utcnow() + timedelta(minutes=2)
                    older = utcnow() - timedelta(days=1)
                    # More denied recent candidates than the eight-link limit:
                    # the grantee must receive older permitted links instead.
                    db.session.add_all([Page(wiki_id=wiki.id, path=f'denied/{i}.md',
                                              title=f'DENIED RECENT {i}', visibility='private',
                                              updated_at=newer) for i in range(4000)])
                    db.session.add_all([Page(wiki_id=wiki.id, path=f'authorized/{i}.md',
                                              title=f'AUTHORIZED RECENT {i}', visibility='private',
                                              updated_at=older) for i in range(8)])
                    db.session.commit(); db.session.remove()
                for actor in (None, 'acl-member', 'acl-viewer', 'acl-owner'):
                    with app.test_client() as client:
                        if actor:
                            with client.session_transaction() as session:
                                session['_user_id'] = ids[actor]; session['_fresh'] = True
                        root = client.get('/@acl-owner/shared')
                        self.assertEqual(root.status_code, 200, actor)
                        if actor == 'acl-owner':
                            self.assertIn(b'UNSHARED ROOT CONTENT', root.data)
                        else:
                            self.assertIn(b'PUBLIC FALLBACK', root.data)
                            self.assertNotIn(b'UNSHARED ROOT CONTENT', root.data)
                            self.assertNotIn(b'UNSHARED FOLDER CONTENT', root.data)
                            self.assertNotIn(b'UNSHARED SECRET TITLE', root.data)
                            self.assertNotIn(b'DENIED RECENT', root.data)
                        queries = []
                        def capture_query(conn, cursor, statement, parameters, context, executemany):
                            queries.append(statement)
                        with app.app_context():
                            engine = db.engine
                        event.listen(engine, 'before_cursor_execute', capture_query)
                        try:
                            shared = client.get('/@acl-owner/shared/folder/allowed')
                        finally:
                            event.remove(engine, 'before_cursor_execute', capture_query)
                        if actor == 'acl-viewer':
                            self.assertLess(len(queries), 30, 'Sparse grants must not repeat recent-page queries per batch')
                        folder = client.get('/@acl-owner/shared/folder/')
                        if actor in ('acl-owner', 'acl-viewer'):
                            self.assertEqual(shared.status_code, 200)
                            self.assertEqual(folder.status_code, 200)
                            self.assertIn(b'SHARED LEAF', shared.data)
                            if actor == 'acl-viewer':
                                self.assertIn(b'AUTHORIZED RECENT 7', shared.data)
                                for response in (shared, folder):
                                    self.assertNotIn(b'UNSHARED', response.data)
                                    self.assertNotIn(b'DENIED RECENT', response.data)
                        else:
                            self.assertEqual(shared.status_code, 403)
                            self.assertEqual(folder.status_code, 403)
                        self.assertEqual(client.get('/@acl-owner/shared/by-link').status_code, 200)
                        secret = client.get('/@acl-owner/shared/secret')
                        self.assertEqual(secret.status_code, 200 if actor == 'acl-owner' else 403)
                # Explicit grants to the two index paths retain normal reader
                # behavior; a grant to some other path was never sufficient.
                self.assertEqual(setup.put('/api/v1/wikis/acl-owner/shared/pages/.wikihub/acl', headers=headers,
                                          json={'content': acl + 'index.md @acl-viewer:read\nfolder/index.md @acl-viewer:read\n'}).status_code, 200)
                with app.test_client() as grantee:
                    with grantee.session_transaction() as session:
                        session['_user_id'] = ids['acl-viewer']; session['_fresh'] = True
                    self.assertIn(b'UNSHARED ROOT CONTENT', grantee.get('/@acl-owner/shared').data)
                    self.assertIn(b'UNSHARED FOLDER CONTENT', grantee.get('/@acl-owner/shared/folder/').data)
                    self.assertEqual(grantee.get('/@acl-owner/shared/secret').status_code, 403)
                with app.app_context():
                    db.session.remove(); db.engine.dispose()
        finally:
            with admin.cursor() as cursor:
                cursor.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(name)))
            admin.close()


if __name__ == '__main__':
    unittest.main()
