"""Maintenance commands run explicitly rather than during server startup."""
import argparse

from . import create_app
from .db import get_db, import_legacy
from .storage import make_cover, validate_pdf


def main():
    parser = argparse.ArgumentParser(description='School portal maintenance')
    parser.add_argument('command', choices=['import-legacy', 'backfill-covers'])
    args = parser.parse_args()
    app = create_app()
    with app.app_context():
        if args.command == 'import-legacy':
            import_legacy()
            print('Legacy import completed.')
        else:
            count = 0
            db = get_db()
            for issue in db.execute('''SELECT i.id,i.file,m.slug FROM issues i JOIN magazines m ON m.id=i.magazine_id
                WHERE i.cover IS NULL''').fetchall():
                path = app.config['UPLOAD_DIR'] / issue['slug'] / issue['file']
                try:
                    validate_pdf(path)
                except ValueError:
                    print(f"Skipped unreadable issue {issue['id']}.")
                    continue
                with app.extensions['storage_lock']:
                    cover = make_cover(path)
                    if cover:
                        with db:
                            db.execute('UPDATE issues SET cover=? WHERE id=?', (cover.name, issue['id']))
                        count += 1
            print(f'Created {count} cover(s).')


if __name__ == '__main__':
    main()
