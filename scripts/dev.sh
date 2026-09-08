#!/usr/bin/env bash
# Start the Dream Job backend and frontend for local development.
#
# The Vite dev server proxies /api to the Python backend (see
# frontend/vite.config.js), so both halves share an origin and the session
# cookie works without CORS exceptions.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ ! -f .env ]; then
  echo "No .env found. Copy .env.example to .env and fill it in:"
  echo "  cp .env.example .env"
  echo "  python3 -m dreamjob.security.crypto --generate-key   # for the two key values"
  exit 1
fi

cleanup() { kill 0 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo "==> Applying database migrations"
PYTHONPATH=backend python3 -m dreamjob.db.migrator

echo "==> Backend on http://127.0.0.1:8000"
PYTHONPATH=backend python3 -m uvicorn dreamjob.main:app --reload --host 127.0.0.1 --port 8000 &

if [ -d frontend/node_modules ]; then
  echo "==> Frontend on http://127.0.0.1:5173"
  (cd frontend && npm run dev) &
else
  echo "==> Frontend dependencies missing; run: cd frontend && npm install"
fi

wait
