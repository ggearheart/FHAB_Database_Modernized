"""WSGI entrypoint for production servers (e.g. `gunicorn wsgi:app`)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from fhab.web import create_app  # noqa: E402

app = create_app()
