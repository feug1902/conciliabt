"""Vercel serverless entry point: exposes the FastAPI app as a Python function.

All /api/* requests are rewritten here by vercel.json. The FastAPI app serves
the API and (when the public/ dir is present) the static frontend as well.
"""
import sys
from pathlib import Path

# Make the project root importable (app package lives at ./app)
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import app  # noqa: E402

# Vercel expects a WSGI/ASGI callable named `app`
__all__ = ["app"]
