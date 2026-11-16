#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

# Install dependencies if missing (local dev only; Vercel installs from requirements.txt)
python3 -c "import fastapi, uvicorn, pdfplumber, reportlab" 2>/dev/null || pip install -q -r requirements.txt

PORT="${PORT:-8000}"
exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
