"""Database initialization and a versioned, preserving legacy migration."""
import secrets
import sqlite3
from datetime import date, datetime

from flask import current_app, g
from werkzeug.security import generate_password_hash

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
    pw TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'editor');
CREATE TABLE IF NOT EXISTS magazines(id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL, sort INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS issues(id INTEGER PRIMARY KEY, magazine_id INTEGER NOT NULL REFERENCES magazines(id),
    title TEXT NOT NULL, issue_date TEXT NOT NULL, file TEXT NOT NULL, cover TEXT, author TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS posts(id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
    category TEXT DEFAULT 'General', pinned INTEGER DEFAULT 0, expires TEXT, created TEXT NOT NULL,
    updated TEXT, author TEXT);
CREATE TABLE IF NOT EXISTS attachments(id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL,
    file TEXT NOT NULL, name TEXT, kind TEXT);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts TEXT, user TEXT, action TEXT, detail TEXT);
'''


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(current_app.config['DATABASE'], timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys=ON')
    return g.db


def close_db(_=None):
    connection = g.pop('db', None)
    if connection is not None:
        connection.close()


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M')


def initialize():
    db = get_db()
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version > 1:
        raise RuntimeError('This database requires a newer version of the portal.')
    existing = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    if existing and version < 1:
        directory = current_app.config['DATA_DIR'] / 'migration-backups'
        directory.mkdir(exist_ok=True, mode=0o700)
        path = directory / f"school-before-v1-{datetime.now():%Y%m%d-%H%M%S-%f}.db"
        with sqlite3.connect(path) as backup:
            db.backup(backup)
            if backup.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('The migration recovery copy failed its integrity check.')
        path.chmod(0o600)
    db.executescript(SCHEMA)
    # Existing IDs, password hashes, roles, posts and files remain intact.
    with db:
        if version < 1:
            columns = {row['name'] for row in db.execute('PRAGMA table_info(users)')}
            if 'auth_id' not in columns:
                db.execute('ALTER TABLE users ADD COLUMN auth_id TEXT')
            if 'session_version' not in columns:
                db.execute('ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0')
            for row in db.execute('SELECT id FROM users WHERE auth_id IS NULL OR auth_id=""').fetchall():
                db.execute('UPDATE users SET auth_id=? WHERE id=?', (secrets.token_hex(24), row['id']))
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS users_auth_id ON users(auth_id)')
            issue_columns = {row['name'] for row in db.execute('PRAGMA table_info(issues)')}
            if 'created' not in issue_columns:
                db.execute('ALTER TABLE issues ADD COLUMN created TEXT')
            db.execute("UPDATE issues SET created=issue_date||' 00:00' WHERE created IS NULL")
            db.execute('''CREATE TABLE IF NOT EXISTS auth_sessions(
                token_hash TEXT PRIMARY KEY,
                auth_id TEXT NOT NULL REFERENCES users(auth_id) ON DELETE CASCADE,
                version INTEGER NOT NULL, expires REAL NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS posts_created ON posts(created)')
            db.execute('CREATE INDEX IF NOT EXISTS issues_magazine ON issues(magazine_id,issue_date)')
            db.execute('CREATE INDEX IF NOT EXISTS attachments_post ON attachments(post_id)')
            db.execute('PRAGMA user_version=1')
        if current_app.config['BOOTSTRAP_ADMIN'] and not db.execute('SELECT 1 FROM users').fetchone():
            password = current_app.config.get('SCHOOL_ADMIN_PASSWORD')
            if not password:
                raise RuntimeError("First run: set SCHOOL_ADMIN_PASSWORD to create the admin account.")
            if len(password) < 8:
                raise RuntimeError('SCHOOL_ADMIN_PASSWORD must contain at least 8 characters.')
            db.execute('INSERT INTO users(username,pw,role,auth_id) VALUES(?,?,?,?)',
                       ('admin', generate_password_hash(password), 'admin', secrets.token_hex(24)))
        if not db.execute('SELECT 1 FROM magazines').fetchone():
            db.executemany('INSERT INTO magazines(slug,name,sort) VALUES(?,?,?)',
                           [(f'magazine-{i}', name, i) for i, name in enumerate(
                               ['Magazine One', 'Magazine Two', 'Magazine Three'], 1)])


def import_legacy():
    """Explicit, repeatable import; unrelated startup requests do not scan files."""
    import json
    import re
    db = get_db()
    old = current_app.config['DATA_DIR'] / 'announcements.json'
    with db:
        if old.exists() and not db.execute('SELECT 1 FROM posts').fetchone():
            # Validate the entire batch before writing; rollback on a bad legacy record.
            records = [(n['title'], n['text'], datetime.strptime(n['date'], '%d %b %Y').strftime('%Y-%m-%d 00:00'))
                       for n in json.loads(old.read_text('utf-8'))]
            db.executemany("INSERT INTO posts(title,body,created,author) VALUES(?,?,?,'admin')", records)
        for magazine in db.execute('SELECT id,slug FROM magazines').fetchall():
            for file in (current_app.config['UPLOAD_DIR'] / magazine['slug']).glob('*.pdf'):
                if not db.execute('SELECT 1 FROM issues WHERE magazine_id=? AND file=?',
                                  (magazine['id'], file.name)).fetchone():
                    db.execute('''INSERT INTO issues(magazine_id,title,issue_date,file,author,created)
                        VALUES(?,?,?,?,'import',?)''', (magazine['id'], re.sub(r'[_-]+', ' ', file.stem).strip(),
                        date.fromtimestamp(file.stat().st_mtime).isoformat(), file.name,
                        datetime.fromtimestamp(file.stat().st_mtime).strftime('%Y-%m-%d %H:%M')))
