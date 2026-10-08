"""Security and publishing regression checks using disposable databases/files."""
import io
import re
import secrets
import sqlite3
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pymupdf
from PIL import Image
from werkzeug.security import generate_password_hash

from portal import create_app, enabled
from portal.db import SCHEMA, get_db, import_legacy


class PortalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = secrets.token_urlsafe(20)
        cls.password_hash = generate_password_hash(cls.password)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = {'TESTING': True, 'DATA_DIR': self.root / 'data', 'UPLOAD_DIR': self.root / 'uploads',
                       'SECRET_KEY': secrets.token_hex(32), 'BOOTSTRAP_ADMIN': False}
        self.app = create_app(self.config)
        self.client = self.app.test_client()
        with self.app.app_context(), get_db() as db:
            db.executemany('INSERT INTO users(id,username,pw,role,auth_id) VALUES(?,?,?,?,?)',
                           [(1, 'admin', self.password_hash, 'admin', secrets.token_hex(24)),
                            (2, 'editor', self.password_hash, 'editor', secrets.token_hex(24))])

    def tearDown(self):
        self.directory.cleanup()

    def query(self, sql, values=()):
        with self.app.app_context():
            return get_db().execute(sql, values).fetchall()

    def write(self, sql, values=()):
        with self.app.app_context(), get_db() as db:
            return db.execute(sql, values).lastrowid

    def csrf(self, client=None):
        with (client or self.client).session_transaction() as session:
            return session.get('csrf')

    def login(self, username='admin', client=None):
        client = client or self.client
        client.get('/login')
        response = client.post('/login', data={'csrf': self.csrf(client), 'username': username, 'password': self.password})
        self.assertEqual(response.status_code, 302)
        return client

    def post(self, url, data=None, client=None, **kwargs):
        client = client or self.client
        return client.post(url, data={'csrf': self.csrf(client), **(data or {})}, **kwargs)

    def pdf(self):
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((50, 50), 'School review issue')
            return io.BytesIO(document.tobytes())

    def image(self):
        content = io.BytesIO()
        Image.new('RGB', (10, 10), 'purple').save(content, format='PNG')
        content.seek(0)
        return content

    def add_post(self, title='School notice', **changes):
        values = {'title': title, 'body': 'Exam information', 'category': 'Exams', 'created': '2026-10-08 09:00', **changes}
        return self.write('INSERT INTO posts(title,body,category,created,expires) VALUES(?,?,?,?,?)',
                          (values['title'], values['body'], values['category'], values['created'], values.get('expires')))

    def test_public_routes_and_real_html_service_links(self):
        for url in ['/', '/announcements', '/magazines', '/magazines/magazine-1', '/search', '/login']:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('href="http://192.168.88.21:8000"', html)
        self.assertIn('School Announcements', html)
        self.assertIn('<h1>', html)
        self.assertNotIn('Password=', html)
        self.assertEqual(self.client.get('/index.html').status_code, 301)
        self.assertEqual(self.client.get('/staff').status_code, 404)

    def test_anonymous_and_legacy_sessions_cannot_publish(self):
        self.assertEqual(self.client.get('/admin').status_code, 302)
        with self.client.session_transaction() as session:
            session['uid'] = 1
        self.assertEqual(self.client.get('/admin').status_code, 302)

    def test_login_requires_csrf(self):
        response = self.client.post('/login', data={'username': 'admin', 'password': self.password})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.query('SELECT * FROM auth_sessions'))

    def test_login_creates_csrf_before_redirect_and_rejects_missing_tokens(self):
        self.login()
        self.assertTrue(self.csrf())
        self.assertEqual(self.client.post('/admin/post', data={'title': 'Unsafe', 'body': 'Missing token'}).status_code, 400)
        with self.client.session_transaction() as session:
            session.pop('csrf')
        self.assertEqual(self.client.post('/admin/post', data={'title': 'Unsafe', 'body': 'Both tokens missing'}).status_code, 400)
        self.assertFalse(self.query('SELECT * FROM posts'))

    def test_unicode_bad_csrf_is_a_client_error(self):
        self.login()
        self.assertEqual(self.client.post('/admin/post', data={'csrf': 'غلط', 'title': 'No', 'body': 'No'}).status_code, 400)

    def test_editor_admin_boundaries(self):
        self.login('editor')
        for section in ['announcements', 'issues', 'account']:
            self.assertEqual(self.client.get('/admin?section=' + section).status_code, 200)
        for section in ['users', 'magazines', 'backup']:
            self.assertEqual(self.client.get('/admin?section=' + section).status_code, 403)
        self.assertEqual(self.client.get('/admin/backup').status_code, 403)
        self.assertEqual(self.post('/admin/user', {'username': 'intruder', 'password': self.password}).status_code, 403)

    def test_deleted_editor_cookie_cannot_inherit_reused_admin_id(self):
        editor = self.login('editor', self.app.test_client())
        old_cookie = editor.get_cookie('session').value
        self.login()
        self.assertEqual(self.post('/admin/del/user/2').status_code, 302)
        self.assertEqual(self.post('/admin/user', {'username': 'replacement', 'password': self.password, 'role': 'admin'}).status_code, 302)
        replacement = self.query('SELECT id FROM users WHERE username="replacement"')[0]
        self.assertEqual(replacement['id'], 2)  # Prove protection even when SQLite reuses the ID.
        replay = self.app.test_client()
        replay.set_cookie('session', old_cookie)
        self.assertEqual(replay.get('/admin/backup').status_code, 302)

    def test_password_change_revokes_other_sessions_and_preserves_current(self):
        other = self.login(client=self.app.test_client())
        self.login()
        response = self.post('/admin/password', {'old': self.password, 'new': secrets.token_urlsafe(20)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(other.get('/admin').status_code, 302)
        self.assertEqual(self.client.get('/admin').status_code, 200)
        self.assertEqual(len(self.query('SELECT * FROM auth_sessions')), 1)

    def test_logout_revokes_copied_cookie(self):
        self.login()
        cookie = self.client.get_cookie('session').value
        self.assertEqual(self.post('/logout').status_code, 302)
        replay = self.app.test_client()
        replay.set_cookie('session', cookie)
        self.assertEqual(replay.get('/admin').status_code, 302)

    def test_expired_server_session_is_rejected(self):
        self.login()
        self.write('UPDATE auth_sessions SET expires=0')
        self.assertEqual(self.client.get('/admin').status_code, 302)

    def test_login_throttle_and_correct_proxy_boolean_parsing(self):
        self.client.get('/login')
        for _ in range(5):
            self.post('/login', {'username': 'admin', 'password': 'wrong'})
        self.assertEqual(self.post('/login', {'username': 'admin', 'password': self.password}).status_code, 429)
        with patch.dict('os.environ', {'HTTPS_ONLY': '0', 'BEHIND_PROXY': 'false'}):
            self.assertFalse(enabled('HTTPS_ONLY'))
            self.assertFalse(enabled('BEHIND_PROXY'))

    def test_all_publishing_sections_render(self):
        self.login()
        for section in ['announcements', 'issues', 'magazines', 'account', 'users', 'backup']:
            with self.subTest(section=section):
                self.assertEqual(self.client.get('/admin?section=' + section).status_code, 200)

    def test_filter_search_state_and_escaping(self):
        self.add_post('Exam <script>alert(1)</script>')
        self.add_post('Holiday', category='Holiday')
        html = self.client.get('/announcements?q=Exam&cat=Exams').get_data(as_text=True)
        self.assertIn('name="cat" value="Exams"', html)
        self.assertIn('cat=Holiday&amp;q=Exam', html)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertNotIn('>Holiday</h2>', html)

    def test_staff_search_and_pagination_reach_older_items(self):
        self.login()
        for i in range(46):
            self.add_post(f'Archive notice {i:02d}')
        html = self.client.get('/admin?section=announcements&page=5').get_data(as_text=True)
        self.assertIn('Archive notice 00', html)
        self.assertIn('Page 5 of 5', html)
        filtered = self.client.get('/admin?section=announcements&q=notice+00').get_data(as_text=True)
        self.assertIn('Archive notice 00', filtered)
        self.assertNotIn('Archive notice 01', filtered)

    def test_extreme_pagination_is_bounded(self):
        self.add_post()
        for url in ['/announcements?page=' + '9'*300, '/magazines/magazine-1?page=' + '9'*300]:
            self.assertEqual(self.client.get(url).status_code, 200)
        self.login()
        self.assertEqual(self.client.get('/admin?page=' + '9'*300).status_code, 200)

    def test_empty_states_and_expired_announcements(self):
        identifier = self.add_post(expires='2000-01-01')
        self.assertEqual(self.client.get(f'/announcements/{identifier}').status_code, 404)
        html = self.client.get('/announcements?q=missing').get_data(as_text=True)
        self.assertIn('No matching announcements', html)
        self.assertIn('Clear search and filters', html)

    def test_school_search_covers_posts_and_magazine_titles(self):
        self.add_post('Science exam')
        self.write('INSERT INTO issues(magazine_id,title,issue_date,file,created) VALUES(1,?,?,?,?)',
                   ('Science edition', '2026-10-01', 'sample.pdf', '2026-10-01 09:00'))
        html = self.client.get('/search?q=science').get_data(as_text=True)
        self.assertIn('Science exam', html)
        self.assertIn('Science edition', html)

    def test_nonexistent_edit_does_not_create_orphan_attachment(self):
        self.login()
        response = self.post('/admin/post', {'id': '999', 'title': 'Missing', 'body': 'Missing',
                                           'files': (self.image(), 'image.png')})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(self.query('SELECT * FROM attachments'))
        self.assertFalse(list((self.root / 'uploads').rglob('*.png')))

    def test_invalid_expiry_keeps_entered_text(self):
        self.login()
        response = self.post('/admin/post', {'title': 'Keep my title', 'body': 'Keep my message', 'expires': 'not-a-date'})
        self.assertEqual(response.status_code, 400)
        html = response.get_data(as_text=True)
        self.assertIn('Keep my title', html)
        self.assertIn('Keep my message', html)
        self.assertFalse(self.query('SELECT * FROM posts'))

    def test_publish_edit_and_confirm_delete_with_valid_image(self):
        self.login()
        response = self.post('/admin/post', {'title': 'New notice', 'body': 'اردو اور انگریزی', 'files': (self.image(), 'photo.png')})
        self.assertEqual(response.status_code, 302)
        post = self.query('SELECT * FROM posts')[0]
        attachment = self.query('SELECT * FROM attachments')[0]
        with self.client.get(f"/att/{attachment['id']}") as response:
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(f"/admin?edit={post['id']}").status_code, 200)
        self.post('/admin/post', {'id': str(post['id']), 'title': 'Edited', 'body': 'Edited message'})
        self.assertEqual(self.query('SELECT title FROM posts')[0]['title'], 'Edited')
        confirmation = self.client.get(f"/admin/del/post/{post['id']}")
        self.assertIn('Delete permanently', confirmation.get_data(as_text=True))
        self.assertEqual(len(self.query('SELECT * FROM posts')), 1)  # GET does not delete.
        self.assertEqual(self.post(f"/admin/del/post/{post['id']}").status_code, 302)
        self.assertFalse(self.query('SELECT * FROM posts'))
        self.assertFalse(self.query('SELECT * FROM attachments'))
        self.assertFalse(list((self.root / 'uploads').rglob('*.png')))

    def test_invalid_attachment_is_skipped_with_feedback(self):
        self.login()
        self.post('/admin/post', {'title': 'Notice', 'body': 'Body', 'files': (io.BytesIO(b'\x89PNG\r\n\x1a\ninvalid'), 'broken.png')})
        self.assertEqual(len(self.query('SELECT * FROM posts')), 1)
        self.assertFalse(self.query('SELECT * FROM attachments'))
        self.assertIn('attachment(s) skipped', self.client.get('/admin').get_data(as_text=True))

    def test_valid_pdf_has_cover_and_safe_headers(self):
        self.login()
        response = self.post('/admin/upload', {'mag': '1', 'title': 'Valid issue', 'date': '2026-10-08', 'pdf': (self.pdf(), 'issue.pdf')})
        self.assertEqual(response.status_code, 302)
        issue = self.query('SELECT * FROM issues')[0]
        self.assertTrue(issue['cover'])
        response = self.client.get(f"/files/{issue['id']}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response.mimetype, 'application/pdf')
        response.close()
        with self.client.get(f"/cover/{issue['id']}") as response:
            self.assertEqual(response.status_code, 200)

    def test_malformed_pdf_and_date_create_no_issue_or_files(self):
        self.login()
        for date_value, content in [('not-a-date', self.pdf()), ('2026-10-08', io.BytesIO(b'%PDF-broken'))]:
            response = self.post('/admin/upload', {'mag': '1', 'date': date_value, 'pdf': (content, 'bad.pdf')})
            self.assertEqual(response.status_code, 400)
        self.assertFalse(self.query('SELECT * FROM issues'))
        self.assertFalse([p for p in (self.root / 'uploads').rglob('*') if p.is_file()])

    def test_database_failure_cleans_uploaded_pdf_and_cover(self):
        self.login()
        self.write("CREATE TRIGGER reject_issue BEFORE INSERT ON issues BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.post('/admin/upload', {'mag': '1', 'pdf': (self.pdf(), 'issue.pdf')})
        self.assertFalse([p for p in (self.root / 'uploads').rglob('*') if p.is_file()])
        self.assertFalse(self.query('SELECT * FROM issues'))

    def test_backups_are_concurrent_unique_valid_and_cleaned(self):
        self.login()
        self.post('/admin/post', {'title': 'Backed up', 'body': 'Body', 'files': (self.image(), 'photo.png')})
        cookie = self.client.get_cookie('session').value
        def fetch():
            client = self.app.test_client()
            client.set_cookie('session', cookie)
            response = client.get('/admin/backup')
            self.assertEqual(response.status_code, 200)
            content = response.get_data()
            response.close()
            with zipfile.ZipFile(io.BytesIO(content)) as bundle:
                self.assertIsNone(bundle.testzip())
                self.assertIn('school.db', bundle.namelist())
                self.assertTrue(any(name.startswith('uploads/') for name in bundle.namelist()))
                restored = self.root / ('restored-' + secrets.token_hex(8) + '.db')
                restored.write_bytes(bundle.read('school.db'))
                with sqlite3.connect(restored) as connection:
                    self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                    self.assertEqual(connection.execute('SELECT title FROM posts').fetchone()[0], 'Backed up')
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _: fetch(), range(2)))
        self.assertFalse(list((self.root / 'data').glob('portal-backup-*')))

    def test_legacy_import_keeps_same_filename_in_different_magazines(self):
        for slug in ['magazine-1', 'magazine-2']:
            folder = self.root / 'uploads' / slug
            folder.mkdir()
            (folder / 'shared.pdf').write_bytes(self.pdf().getvalue())
        with self.app.app_context():
            import_legacy()
            import_legacy()
        self.assertEqual(len(self.query('SELECT * FROM issues')), 2)

    def test_migration_preserves_legacy_data_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as legacy_root:
            root = Path(legacy_root)
            data = root / 'data'
            data.mkdir()
            with sqlite3.connect(data / 'school.db') as db:
                db.executescript(SCHEMA)
                db.execute('INSERT INTO users(id,username,pw,role) VALUES(7,?,?,?)', ('legacy', self.password_hash, 'editor'))
                db.execute("INSERT INTO posts(id,title,body,created) VALUES(8,'Existing notice','Existing message','2026-01-01 12:00')")
            config = {**self.config, 'DATA_DIR': data, 'UPLOAD_DIR': root / 'uploads'}
            migrated = create_app(config)
            with migrated.app_context():
                user = get_db().execute('SELECT * FROM users WHERE id=7').fetchone()
                self.assertEqual(user['pw'], self.password_hash)
                self.assertEqual(user['role'], 'editor')
                identity = user['auth_id']
                self.assertTrue(identity)
                self.assertEqual(get_db().execute('SELECT title FROM posts WHERE id=8').fetchone()[0], 'Existing notice')
            repeated = create_app(config)
            with repeated.app_context():
                self.assertEqual(get_db().execute('SELECT auth_id FROM users WHERE id=7').fetchone()[0], identity)
                self.assertEqual(get_db().execute('PRAGMA foreign_keys').fetchone()[0], 1)
            backups = list((data / 'migration-backups').glob('*.db'))
            self.assertEqual(len(backups), 1)
            with sqlite3.connect(backups[0]) as backup:
                self.assertEqual(backup.execute('SELECT username FROM users WHERE id=7').fetchone()[0], 'legacy')

    def test_missing_resources_and_path_traversal(self):
        for url in ['/files/999','/att/999','/images/../app.py','/images/missing.png']:
            self.assertEqual(self.client.get(url).status_code, 404)


if __name__ == '__main__':
    unittest.main()
