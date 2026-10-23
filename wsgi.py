"""WSGI entry point for hosting (Vercel, Waitress, Gunicorn): `wsgi:app`.

Start-up errors (for example a missing SECRET_KEY) are shown as a plain page instead of a crash.
Local development keeps using `flask --app app run`.
"""
from app import safe_create_app

app = safe_create_app()
