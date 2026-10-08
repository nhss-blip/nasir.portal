import secrets
import sqlite3
from datetime import date
from pathlib import Path

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .auth import guard, log, me, sign_in
from .db import get_db, now
from .helpers import CATEGORIES, MAX_ATT, PER_PAGE, pagination, valid_date
from .storage import backup_archive, make_cover, remove_files, save_upload

bp = Blueprint('staff', __name__)
SECTIONS = {'announcements': 'Announcements', 'issues': 'Magazine issues', 'magazines': 'Magazines',
            'account': 'My account', 'users': 'Staff accounts', 'backup': 'Backup & activity'}


def back(message, section='announcements', category='success'):
    flash(message, category)
    return redirect(url_for('staff.admin', section=section))


def render_admin(section, **context):
    return render_template('admin/' + section + '.html', title='Publishing', section=section,
                           sections=SECTIONS, today=date.today().isoformat(), **context)


@bp.get('/admin')
def admin():
    user = guard()
    db = get_db()
    section = request.args.get('section', 'announcements')
    if section not in SECTIONS:
        abort(404)
    if section in {'magazines', 'users', 'backup'} and user['role'] != 'admin':
        abort(403)
    query = request.args.get('q', '').strip()[:200]
    if section == 'announcements':
        edit = None
        if request.args.get('edit'):
            edit = db.execute('SELECT * FROM posts WHERE id=?', (request.args.get('edit', type=int),)).fetchone()
            if not edit:
                abort(404)
        attachments = db.execute('SELECT * FROM attachments WHERE post_id=?', (edit['id'],)).fetchall() if edit else []
        predicate, values = ('WHERE title LIKE ? OR body LIKE ?', [f'%{query}%', f'%{query}%']) if query else ('', [])
        count = db.execute(f'SELECT count(*) FROM posts {predicate}', values).fetchone()[0]
        number, total, offset = pagination(count)
        posts = db.execute(f'SELECT * FROM posts {predicate} ORDER BY pinned DESC,created DESC,id DESC LIMIT ? OFFSET ?',
                           values + [PER_PAGE, offset]).fetchall()
        return render_admin(section, edit=edit, edit_attachments=attachments, posts=posts,
                            q=query, count=count, pg=number, pages=total, draft=edit or {})
    if section == 'issues':
        predicate, values = ('WHERE i.title LIKE ? OR m.name LIKE ?', [f'%{query}%', f'%{query}%']) if query else ('', [])
        count = db.execute(f'SELECT count(*) FROM issues i JOIN magazines m ON m.id=i.magazine_id {predicate}', values).fetchone()[0]
        number, total, offset = pagination(count)
        issues = db.execute(f'''SELECT i.*,m.name AS magazine_name FROM issues i JOIN magazines m ON m.id=i.magazine_id
            {predicate} ORDER BY i.issue_date DESC,i.id DESC LIMIT ? OFFSET ?''', values + [PER_PAGE, offset]).fetchall()
        return render_admin(section, magazines=db.execute('SELECT * FROM magazines ORDER BY sort,id').fetchall(),
                            issues=issues, q=query, count=count, pg=number, pages=total, draft={})
    if section == 'magazines':
        return render_admin(section, magazines=db.execute('SELECT * FROM magazines ORDER BY sort,id').fetchall())
    if section == 'users':
        return render_admin(section, users=db.execute('SELECT id,username,role FROM users ORDER BY id').fetchall())
    if section == 'backup':
        return render_admin(section, audit=db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 30').fetchall())
    return render_admin(section)


def post_error(message, draft, edit, attachments, status=400):
    return render_admin('announcements', draft=draft, edit=edit, edit_attachments=attachments,
                        posts=[], q='', count=0, pg=1, pages=1, form_error=message), status


@bp.post('/admin/post')
def save_post():
    guard()
    form, db = request.form, get_db()
    title, body = form.get('title', '').strip()[:150], form.get('body', '').strip()[:5000]
    identifier = form.get('id', type=int)
    edit = None
    if form.get('id'):
        edit = db.execute('SELECT * FROM posts WHERE id=?', (identifier,)).fetchone()
        if not edit:
            abort(404)
    attachments = db.execute('SELECT * FROM attachments WHERE post_id=?', (identifier,)).fetchall() if edit else []
    if not title or not body:
        return post_error('Enter a title and a message.', form, edit, attachments)
    try:
        expiry = valid_date(form.get('expires'), optional=True)
    except ValueError as error:
        return post_error(str(error), form, edit, attachments)
    category = form.get('category') if form.get('category') in CATEGORIES else 'General'
    values = (title, body, category, int(bool(form.get('pinned'))), expiry)
    created, removed, skipped = [], [], 0
    folder = current_app.config['UPLOAD_DIR'] / '_announcements'
    with current_app.extensions['storage_lock']:
        try:
            with db:
                if edit:
                    db.execute('UPDATE posts SET title=?,body=?,category=?,pinned=?,expires=?,updated=? WHERE id=?',
                               values + (now(), identifier))
                    selected = set(form.getlist('remove'))
                    for attachment in attachments:
                        if str(attachment['id']) in selected:
                            db.execute('DELETE FROM attachments WHERE id=? AND post_id=?', (attachment['id'], identifier))
                            removed.append(folder / attachment['file'])
                else:
                    identifier = db.execute('INSERT INTO posts(title,body,category,pinned,expires,created,author) VALUES(?,?,?,?,?,?,?)',
                                            values + (now(), me()['username'])).lastrowid
                count = db.execute('SELECT count(*) FROM attachments WHERE post_id=?', (identifier,)).fetchone()[0]
                for upload in request.files.getlist('files'):
                    if not upload or not upload.filename:
                        continue
                    if count >= MAX_ATT:
                        skipped += 1
                        continue
                    try:
                        path, extension = save_upload(upload, folder)
                    except ValueError:
                        skipped += 1
                        continue
                    created.append(path)
                    db.execute('INSERT INTO attachments(post_id,file,name,kind) VALUES(?,?,?,?)',
                               (identifier, path.name, Path(upload.filename).name[:100], 'pdf' if extension == 'pdf' else 'img'))
                    count += 1
                log('edit post' if edit else 'post', title)
        except Exception:
            remove_files(created)
            raise
        remove_files(removed)
    suffix = f' {skipped} attachment(s) skipped: choose valid JPG, PNG, GIF, WebP or PDF files, up to {MAX_ATT} per announcement.' if skipped else ''
    return back('Announcement saved.' + suffix, category='warning' if skipped else 'success')


@bp.post('/admin/upload')
def upload():
    guard()
    db, form = get_db(), request.form
    file = request.files.get('pdf')
    magazine = db.execute('SELECT * FROM magazines WHERE id=?', (form.get('mag', type=int),)).fetchone()
    try:
        if not magazine or not file or not file.filename:
            raise ValueError('Choose a magazine and a PDF.')
        issue_date = valid_date(form.get('date') or date.today().isoformat())
    except ValueError as error:
        return render_admin('issues', magazines=db.execute('SELECT * FROM magazines ORDER BY sort,id').fetchall(),
                            issues=[], q='', count=0, pg=1, pages=1, draft=form, form_error=str(error)), 400
    title = form.get('title', '').strip()[:120] or Path(file.filename).stem.replace('_', ' ').replace('-', ' ')[:120]
    created = []
    with current_app.extensions['storage_lock']:
        try:
            path, _ = save_upload(file, current_app.config['UPLOAD_DIR'] / magazine['slug'], pdf_only=True)
            created.append(path)
            cover = make_cover(path)
            if cover:
                created.append(cover)
            with db:
                db.execute('INSERT INTO issues(magazine_id,title,issue_date,file,cover,author,created) VALUES(?,?,?,?,?,?,?)',
                           (magazine['id'], title, issue_date, path.name, cover.name if cover else None, me()['username'], now()))
                log('upload', f"{magazine['name']}: {title}")
        except ValueError as error:
            remove_files(created)
            return render_admin('issues', magazines=db.execute('SELECT * FROM magazines ORDER BY sort,id').fetchall(),
                                issues=[], q='', count=0, pg=1, pages=1, draft=form, form_error=str(error)), 400
        except Exception:
            remove_files(created)
            raise
    return back('Magazine issue uploaded.', 'issues')


@bp.route('/admin/del/<kind>/<int:i>', methods=['GET', 'POST'])
def delete(kind, i):
    guard(kind == 'user')
    db = get_db()
    tables = {'post': ('posts', 'title', 'announcements'), 'issue': ('issues', 'title', 'issues'), 'user': ('users', 'username', 'users')}
    if kind not in tables or (kind == 'user' and i == me()['id']):
        abort(404)
    table, field, section = tables[kind]
    item = db.execute(f'SELECT * FROM {table} WHERE id=?', (i,)).fetchone()
    if not item:
        abort(404)
    if request.method == 'GET':
        return render_template('admin/delete.html', title='Confirm deletion', section=section, sections=SECTIONS,
                               kind=kind, item=item, item_name=item[field])
    removed = []
    with current_app.extensions['storage_lock']:
        with db:
            if kind == 'post':
                removed = [current_app.config['UPLOAD_DIR'] / '_announcements' / row['file']
                           for row in db.execute('SELECT file FROM attachments WHERE post_id=?', (i,))]
                db.execute('DELETE FROM attachments WHERE post_id=?', (i,))
            elif kind == 'issue':
                magazine = db.execute('SELECT slug FROM magazines WHERE id=?', (item['magazine_id'],)).fetchone()
                if magazine:
                    removed = [current_app.config['UPLOAD_DIR'] / magazine['slug'] / item[name]
                               for name in ('file', 'cover') if item[name]]
            db.execute(f'DELETE FROM {table} WHERE id=?', (i,))
            log('delete ' + kind, item[field])
        remove_files(removed)
    return back('Item deleted.', section)


@bp.post('/admin/mag')
def magazine():
    import re
    guard(True)
    db = get_db()
    name = request.form.get('name', '').strip()[:60]
    if not name:
        return back('Enter a magazine name.', 'magazines', 'error')
    identifier = request.form.get('id', type=int)
    with db:
        if request.form.get('id'):
            if not db.execute('SELECT 1 FROM magazines WHERE id=?', (identifier,)).fetchone():
                abort(404)
            db.execute('UPDATE magazines SET name=? WHERE id=?', (name, identifier))
        else:
            slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or 'magazine'
            if db.execute('SELECT 1 FROM magazines WHERE slug=?', (slug,)).fetchone():
                slug += '-' + secrets.token_hex(4)
            db.execute('INSERT INTO magazines(slug,name,sort) VALUES(?,?,(SELECT coalesce(max(sort),0)+1 FROM magazines))', (slug, name))
        log('magazine', name)
    return back('Magazine saved.', 'magazines')


@bp.post('/admin/user')
def add_user():
    import re
    guard(True)
    db = get_db()
    username, password = request.form.get('username', '').strip().lower(), request.form.get('password', '')
    if not re.fullmatch(r'[a-z0-9._-]{1,60}', username) or len(password) < 8:
        return back('Use a username with letters, numbers, dots, underscores or hyphens, and a password of at least 8 characters.', 'users', 'error')
    try:
        with db:
            db.execute('INSERT INTO users(username,pw,role,auth_id) VALUES(?,?,?,?)',
                       (username, generate_password_hash(password), 'admin' if request.form.get('role') == 'admin' else 'editor', secrets.token_hex(24)))
            log('add user', username)
    except sqlite3.IntegrityError:
        return back('That username already exists.', 'users', 'error')
    return back('Staff account added.', 'users')


@bp.post('/admin/password')
def password():
    user = guard()
    db = get_db()
    if not check_password_hash(user['pw'], request.form.get('old', '')) or len(request.form.get('new', '')) < 8:
        return back('Check your current password and use at least 8 characters for the new password.', 'account', 'error')
    with db:
        db.execute('UPDATE users SET pw=?,session_version=session_version+1 WHERE auth_id=?',
                   (generate_password_hash(request.form['new']), user['auth_id']))
        db.execute('DELETE FROM auth_sessions WHERE auth_id=?', (user['auth_id'],))
        log('password changed')
        updated = db.execute('SELECT * FROM users WHERE auth_id=?', (user['auth_id'],)).fetchone()
        sign_in(updated)
    return back('Password changed. Other sessions have been signed out.', 'account')


@bp.get('/admin/backup')
def backup():
    guard(True)
    with current_app.extensions['storage_lock']:
        temporary, archive = backup_archive()
        try:
            with get_db():
                log('backup')
            response = send_file(archive, as_attachment=True, download_name=f'school-backup-{date.today()}.zip')
        except Exception:
            temporary.cleanup()
            raise
    response.direct_passthrough = False
    response.call_on_close(temporary.cleanup)
    return response
