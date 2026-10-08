"""Start an isolated local test instance with a password you choose."""
import getpass
import os
import sqlite3
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    data = root / 'test-runtime' / 'data'
    uploads = root / 'test-runtime' / 'uploads'
    os.environ['SCHOOL_DATA_DIR'] = str(data)
    os.environ['SCHOOL_UPLOAD_DIR'] = str(uploads)
    for name in ['BEHIND_PROXY', 'HTTPS_ONLY', 'SCHOOL_ADMIN_PASSWORD']:
        os.environ.pop(name, None)
    database = data / 'school.db'
    initialized = False
    if database.exists():
        try:
            with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as connection:
                initialized = bool(connection.execute('SELECT 1 FROM users LIMIT 1').fetchone())
        except sqlite3.OperationalError:
            pass
    if not initialized:
        while True:
            password = getpass.getpass('Choose a local test admin password (at least 8 characters): ')
            if len(password) >= 8:
                os.environ['SCHOOL_ADMIN_PASSWORD'] = password
                break
            print('Please use at least 8 characters.')
    from app import app
    from waitress import serve
    print('Local test server: http://127.0.0.1:8080')
    print('Staff username: admin. Press Ctrl+C to stop.')
    serve(app, host='127.0.0.1', port=8080)


if __name__ == '__main__':
    main()
