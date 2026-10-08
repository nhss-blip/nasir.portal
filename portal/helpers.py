import secrets
from datetime import date
from urllib.parse import urlencode

from flask import abort, request, session

CATEGORIES = ['General', 'Exams', 'Holiday', 'Event', 'Urgent']
PER_PAGE, PER_GRID, MAX_ATT = 10, 12, 6
NEW_DAYS = {'announcements': 3, 'magazines': 14}


def csrf_token():
    session.setdefault('csrf', secrets.token_hex(32))
    return session['csrf']


def check_csrf():
    expected, supplied = session.get('csrf'), request.form.get('csrf')
    if not isinstance(expected, str) or not supplied or not secrets.compare_digest(expected.encode(), supplied.encode()):
        abort(400)


def query_url(**changes):
    values = request.args.to_dict()
    values.update(changes)
    return '?' + urlencode({key: value for key, value in values.items() if value not in (None, '')})


def pagination(count, per=PER_PAGE, argument='page'):
    total = max(1, (count + per - 1) // per)
    raw = request.args.get(argument, '1')
    try:
        # Bound before conversion/offset calculation, including pathological input sizes.
        number = int(raw) if len(raw) <= 12 else total
    except (ValueError, TypeError):
        number = 1
    number = min(total, max(1, number))
    return number, total, (number - 1) * per


def valid_date(value, optional=False):
    if not value and optional:
        return None
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise ValueError('Enter a valid date in YYYY-MM-DD format.') from None
