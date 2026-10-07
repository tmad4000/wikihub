"""Real PostgreSQL regression: directory queries must not grow with account count."""
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

class DirectoryScalingTest(unittest.TestCase):
    def test_bounded_queries_and_profile_visibility(self):
        import psycopg2
        from psycopg2 import sql
        from sqlalchemy import event
        from flask_login import login_user
        admin_url = os.environ['WORKER_TEST_DATABASE_URL']
        name = 'wikihub_directory_' + uuid.uuid4().hex[:12]
        admin = psycopg2.connect(admin_url)
        admin.autocommit = True
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        try:
            parts = urlsplit(admin_url)
            with tempfile.TemporaryDirectory(prefix='wikihub-directory-') as repos:
                os.environ.update(DATABASE_URL=urlunsplit(parts._replace(path='/' + name)),
                                  REPOS_DIR=repos, SECRET_KEY='directory-test', SESSION_COOKIE_SECURE='0')
                from app import create_app, db
                from app.models import User, Wiki, Page
                from app.routes.main import _people_directory
                app = create_app()
                app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, IDEAFLOW_AUTO_SIGNIN_ENABLED=False)
                with app.app_context():
                    # More accounts than production's 106, plus both public and private profiles.
                    for i in range(120):
                        user = User(username=f'directory-{i:03d}')
                        db.session.add(user); db.session.flush()
                        personal = Wiki(owner_id=user.id, slug=user.username)
                        project = Wiki(owner_id=user.id, slug='public-project')
                        secret = Wiki(owner_id=user.id, slug='secret-project')
                        db.session.add_all([personal, project, secret]); db.session.flush()
                        db.session.add_all([
                            Page(wiki_id=personal.id, path='index.md', visibility='private', excerpt=f'PRIVATE-{i}'),
                            Page(wiki_id=personal.id, path='README.md', visibility='public-view', excerpt=f'PUBLIC-{i}'),
                            Page(wiki_id=project.id, path='notes.md', visibility='public', excerpt='project'),
                            Page(wiki_id=secret.id, path='notes.md', visibility='private', excerpt='hidden'),
                        ])
                    db.session.commit(); db.session.remove()
                    engine = db.engine
                for own in (False, True):
                    with app.test_request_context('/people'):
                        if own:
                            login_user(User.query.filter_by(username='directory-000').one())
                        statements = []
                        def capture(conn, cursor, statement, parameters, context, executemany):
                            if statement.lstrip().upper().startswith('SELECT'):
                                statements.append(statement)
                        event.listen(engine, 'before_cursor_execute', capture)
                        try:
                            cards = _people_directory()
                        finally:
                            event.remove(engine, 'before_cursor_execute', capture)
                        self.assertLessEqual(len(statements), 7, f'{len(statements)} queries for 120 accounts')
                        self.assertGreaterEqual(len(cards), 120)
                        indexed = {card['user'].username: card for card in cards}
                        for i in range(120):
                            card = indexed[f'directory-{i:03d}']
                            owner = own and i == 0
                            self.assertEqual(card['project_count'], 2 if owner else 1)
                            self.assertEqual(card['visible_wiki_count'], 3 if owner else 2)
                            self.assertEqual(card['profile_excerpt'], f'PRIVATE-{i}' if owner else f'PUBLIC-{i}')
                            self.assertEqual([w.slug for w in card['latest_wikis']].count('secret-project'), int(owner))
                        self.assertEqual(_people_directory(limit=6), cards[:6])
                with app.test_client() as client:
                    response = client.get('/explore')
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn(b'PRIVATE-', response.data)
                with app.app_context():
                    db.session.remove(); db.engine.dispose()
        finally:
            with admin.cursor() as cursor:
                cursor.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(name)))
            admin.close()

if __name__ == '__main__':
    unittest.main()
