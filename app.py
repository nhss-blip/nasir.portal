"""Waitress-compatible entry point: python -m waitress --port=8080 app:app."""
import os
from portal import create_app

app = create_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
