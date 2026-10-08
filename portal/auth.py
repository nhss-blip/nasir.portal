import hashlib
import secrets
import threading
import time

from flask import Blueprint, abort, current_app, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from .db import get_db, now
from .helpers import check_csrf, csrf_token

bp = Blueprint('auth', __name__)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def me():
    if 'me' not in g:
        g.me = None
        identity, token, version = session.get('auth_id'), session.get('session_token'), session.get('version')
        if isinstance(identity, str) and isinstance(token, str) and isinstance(version, int):
            g.me = get_db().execute('''SELECT u.* FROM users u JOIN auth_sessions s ON s.auth_id=u.auth_id
                WHERE u.auth_id=? AND u.session_version=? AND s.version=? AND s.token_hash=? AND s.expires>?''',
                (identity, version, version, token_hash(token), time.time())).fetchone()
    return g.me


def guard(admin=False):
    user = me()
    if user is None:
        abort(redirect(url_for('auth.login')))
    if admin and user['role'] != 'admin':
        abort(403)
    if request.method == 'POST':
        check_csrf()
    return user


def log(action, detail='', username=None):
    user = me()
    get_db().execute('INSERT INTO audit(ts,user,action,detail) VALUES(?,?,?,?)',
                     (now(), username or (user['username'] if user else '-'), action, detail[:200]))


def sign_in(user):
    session.clear()
    session.permanent = True
    session['auth_id'], session['version'] = user['auth_id'], user['session_version']
    session['session_token'] = secrets.token_urlsafe(32)
    csrf_token()
    duration = current_app.config['PERMANENT_SESSION_LIFETIME'].total_seconds()
    db = get_db()
    db.execute('DELETE FROM auth_sessions WHERE expires<=?', (time.time(),))
    db.execute('INSERT INTO auth_sessions(token_hash,auth_id,version,expires) VALUES(?,?,?,?)',
               (token_hash(session['session_token']), user['auth_id'], user['session_version'], time.time() + duration))
    g.me = user


def revoke_current():
    token = session.get('session_token')
    if token:
        get_db().execute('DELETE FROM auth_sessions WHERE token_hash=?', (token_hash(token),))


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET' and me():
        return redirect(url_for('staff.admin'))
    error = ''
    status = 200
    if request.method == 'POST':
        check_csrf()
        # Per-instance state, bounded lifetime, and serialized updates for Waitress threads.
        state = current_app.extensions.setdefault('login_throttle', {'failures': {}, 'lock': threading.Lock()})
        ip = request.remote_addr or 'unknown'
        with state['lock']:
            timestamp = time.time()
            failures = state['failures']
            for address, (count, since) in list(failures.items()):
                if timestamp - since >= 600:
                    del failures[address]
            count, since = failures.get(ip, (0, timestamp))
            if count >= 5:
                error, status = 'Too many attempts. Try again in 10 minutes.', 429
            else:
                user = get_db().execute('SELECT * FROM users WHERE username=?',
                                        (request.form.get('username', '').strip().lower(),)).fetchone()
                if user and check_password_hash(user['pw'], request.form.get('password', '')):
                    with get_db():
                        revoke_current()
                        sign_in(user)
                        log('login')
                    failures.pop(ip, None)
                    return redirect(url_for('staff.admin'))
                failures[ip] = (count + 1, since)
                error = 'Wrong username or password.'
    return render_template('login.html', title='Staff sign-in', error=error,
                           username=request.form.get('username', '')), status


@bp.post('/logout')
def logout():
    guard()
    with get_db():
        log('logout')
        revoke_current()
    session.clear()
    g.me = None
    return redirect(url_for('public.home'))
