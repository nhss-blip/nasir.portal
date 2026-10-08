"""Application factory for the school portal."""
import os
import secrets
from datetime import timedelta
from pathlib import Path
from threading import RLock

from flask import Flask, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

ROOT = Path(__file__).resolve().parent.parent


def enabled(name):
    return os.environ.get(name, '').strip().lower() in {'1', 'true', 'yes', 'on'}


def create_app(config=None):
    app = Flask(__name__, template_folder=str(ROOT / 'templates'),
                static_folder=str(ROOT / 'static'))
    app.config.from_mapping(
        DATA_DIR=Path(os.environ.get('SCHOOL_DATA_DIR', ROOT / 'data')),
        UPLOAD_DIR=Path(os.environ.get('SCHOOL_UPLOAD_DIR', ROOT / 'uploads')),
        IMAGE_DIR=ROOT / 'images', BOOTSTRAP_ADMIN=True,
        SCHOOL_ADMIN_PASSWORD=os.environ.get('SCHOOL_ADMIN_PASSWORD'),
        MAX_CONTENT_LENGTH=80 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=enabled('HTTPS_ONLY'),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    )
    if config:
        app.config.update(config)
    for name in ['DATA_DIR', 'UPLOAD_DIR', 'IMAGE_DIR']:
        app.config[name] = Path(app.config[name])
    app.config['DATA_DIR'].mkdir(parents=True, exist_ok=True)
    app.config['UPLOAD_DIR'].mkdir(parents=True, exist_ok=True)
    (app.config['UPLOAD_DIR'] / '_announcements').mkdir(exist_ok=True)
    app.config['DATABASE'] = app.config['DATA_DIR'] / 'school.db'
    if not app.config.get('SECRET_KEY'):
        key = app.config['DATA_DIR'] / 'secret.key'
        if not key.exists():
            try:
                with key.open('x') as stream:
                    stream.write(secrets.token_hex(32))
                key.chmod(0o600)
            except FileExistsError:
                pass
        app.config['SECRET_KEY'] = key.read_text()
    if enabled('BEHIND_PROXY'):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    app.extensions['storage_lock'] = RLock()

    from . import auth, db, public, staff
    app.teardown_appcontext(db.close_db)
    app.register_blueprint(auth.bp)
    app.register_blueprint(public.bp)
    app.register_blueprint(staff.bp)

    @app.template_filter('content_language')
    def content_language(text):
        # Publishing fields support English/Urdu; help readers select the right voice.
        return 'ur' if any('\u0600' <= character <= '\u06ff' for character in (text or '')) else 'en'

    @app.context_processor
    def common_context():
        from .helpers import CATEGORIES, csrf_token, query_url
        logo = 'logo.webp' if (app.config['IMAGE_DIR'] / 'logo.webp').exists() else 'logo.png'
        return dict(user=auth.me(), tok=csrf_token(), cats=CATEGORIES,
                    query_url=query_url, logo=logo)

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        if response.mimetype == 'text/html':
            response.headers['Content-Security-Policy'] = (
                "default-src 'self'; img-src 'self'; object-src 'none'; base-uri 'self'; "
                "form-action 'self' https://www.google.com; frame-ancestors 'self'"
            )
        if request.path.startswith(('/admin', '/login', '/logout')):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def error_page(error):
        messages = {
            400: 'Please reload the form and try again.',
            403: 'This action requires administrator access.',
            404: 'This page or item could not be found.',
            413: 'The upload is too large. The total request limit is 80 MB.',
        }
        return render_template('error.html', title=f'Error {error.code}',
                               message=messages[error.code]), error.code

    with app.app_context():
        db.initialize()
    return app
