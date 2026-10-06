#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
exec .venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1 --log-level debug --no-access-log
