from datetime import datetime, timedelta

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, send_from_directory, url_for

from .db import get_db
from .helpers import CATEGORIES, NEW_DAYS, PER_GRID, PER_PAGE, pagination
from .services import SERVICES
from .storage import MIME

bp = Blueprint('public', __name__)
VISIBLE_POSTS = "(expires IS NULL OR expires='' OR expires>=date('now','localtime'))"


@bp.get('/index.html')
def legacy_home():
    return redirect(url_for('public.home'), code=301)


@bp.get('/')
def home():
    return render_template('home.html', title='School services', services=SERVICES,
        posts=get_db().execute(f'SELECT * FROM posts WHERE {VISIBLE_POSTS} ORDER BY pinned DESC,created DESC,id DESC LIMIT 3').fetchall())


@bp.get('/images/<path:f>')
def images(f):
    return send_from_directory(current_app.config['IMAGE_DIR'], f)


def serve_issue(identifier, column, mime):
    # Column names are fixed internal callers, never request input.
    row = get_db().execute(f'''SELECT i.{column} AS file,m.slug FROM issues i
        JOIN magazines m ON m.id=i.magazine_id WHERE i.id=?''', (identifier,)).fetchone()
    if not row or not row['file']:
        abort(404)
    return send_from_directory(current_app.config['UPLOAD_DIR'] / row['slug'], row['file'], mimetype=mime)


@bp.get('/files/<int:i>')
def pdf(i):
    return serve_issue(i, 'file', 'application/pdf')


@bp.get('/cover/<int:i>')
def cover(i):
    return serve_issue(i, 'cover', 'image/jpeg')


@bp.get('/att/<int:i>')
def attachment(i):
    row = get_db().execute('SELECT file FROM attachments WHERE id=?', (i,)).fetchone()
    if not row:
        abort(404)
    return send_from_directory(current_app.config['UPLOAD_DIR'] / '_announcements', row['file'],
                               mimetype=MIME[row['file'].rsplit('.', 1)[1]])


@bp.get('/api/new')
def api_new():
    cutoff = lambda kind: (datetime.now() - timedelta(days=NEW_DAYS[kind])).strftime('%Y-%m-%d %H:%M')
    posts = get_db().execute(f'SELECT 1 FROM posts WHERE created>=? AND {VISIBLE_POSTS} LIMIT 1', (cutoff('announcements'),)).fetchone()
    issues = get_db().execute('''SELECT 1 FROM issues i JOIN magazines m ON m.id=i.magazine_id
        WHERE m.hidden=0 AND i.created>=? LIMIT 1''', (cutoff('magazines'),)).fetchone()
    response = jsonify({'/announcements': bool(posts), '/magazines': bool(issues)})
    response.headers['Cache-Control'] = 'no-store'
    return response


def attachments_for(posts):
    attachments = {}
    identifiers = [post['id'] for post in posts]
    if identifiers:
        placeholders = ','.join('?' for _ in identifiers)
        for row in get_db().execute(f'SELECT * FROM attachments WHERE post_id IN ({placeholders}) ORDER BY id', identifiers):
            attachments.setdefault(row['post_id'], []).append(row)
    return attachments


@bp.get('/announcements')
def announcements():
    query = request.args.get('q', '').strip()[:200]
    category = request.args.get('cat', '')
    if category not in CATEGORIES:
        category = ''
    where, values = [VISIBLE_POSTS], []
    if query:
        where.append('(title LIKE ? OR body LIKE ?)')
        values.extend([f'%{query}%', f'%{query}%'])
    if category:
        where.append('category=?')
        values.append(category)
    predicate = ' AND '.join(where)
    count = get_db().execute(f'SELECT count(*) FROM posts WHERE {predicate}', values).fetchone()[0]
    number, total, offset = pagination(count)
    posts = get_db().execute(f'''SELECT * FROM posts WHERE {predicate}
        ORDER BY pinned DESC,created DESC,id DESC LIMIT ? OFFSET ?''', values + [PER_PAGE, offset]).fetchall()
    return render_template('announcements.html', title='Announcements', posts=posts,
        attachments=attachments_for(posts), q=query, cat=category, count=count, pg=number, pages=total)


@bp.get('/announcements/<int:identifier>')
def announcement(identifier):
    post = get_db().execute(f'SELECT * FROM posts WHERE id=? AND {VISIBLE_POSTS}', (identifier,)).fetchone()
    if not post:
        abort(404)
    return render_template('announcement.html', title=post['title'], post=post, attachments=attachments_for([post]))


@bp.get('/magazines')
@bp.get('/magazines/<slug>')
def magazines(slug=None):
    db = get_db()
    if slug:
        magazine = db.execute('SELECT * FROM magazines WHERE slug=?', (slug,)).fetchone()
        if not magazine:
            abort(404)
        count = db.execute('SELECT count(*) FROM issues WHERE magazine_id=?', (magazine['id'],)).fetchone()[0]
        number, total, offset = pagination(count, PER_GRID)
        issues = db.execute('SELECT * FROM issues WHERE magazine_id=? ORDER BY issue_date DESC,id DESC LIMIT ? OFFSET ?',
                            (magazine['id'], PER_GRID, offset)).fetchall()
        return render_template('issues.html', title=magazine['name'], magazine=magazine, issues=issues,
                               pg=number, pages=total)
    blocks = [(magazine, db.execute('SELECT * FROM issues WHERE magazine_id=? ORDER BY issue_date DESC,id DESC LIMIT 4',
                                    (magazine['id'],)).fetchall())
              for magazine in db.execute('SELECT * FROM magazines WHERE hidden=0 ORDER BY sort,id').fetchall()]
    return render_template('magazines.html', title='Magazines', blocks=blocks)


@bp.get('/search')
def search():
    query = request.args.get('q', '').strip()[:200]
    posts, issues = [], []
    if query:
        posts = get_db().execute(f'''SELECT * FROM posts WHERE {VISIBLE_POSTS} AND (title LIKE ? OR body LIKE ?)
            ORDER BY pinned DESC,created DESC,id DESC LIMIT 20''', (f'%{query}%', f'%{query}%')).fetchall()
        issues = get_db().execute('''SELECT i.*,m.name AS magazine_name FROM issues i JOIN magazines m ON m.id=i.magazine_id
            WHERE m.hidden=0 AND (i.title LIKE ? OR m.name LIKE ?) ORDER BY issue_date DESC,i.id DESC LIMIT 20''',
            (f'%{query}%', f'%{query}%')).fetchall()
    return render_template('search.html', title='Search school content', q=query, posts=posts, issues=issues)
