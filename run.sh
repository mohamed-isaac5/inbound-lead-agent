#!/usr/bin/env bash
# Local launcher: creates a virtualenv on first run, then starts the app on http://127.0.0.1:8765
set -e
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv; .venv/bin/pip install -q -r requirements.txt; }
exec .venv/bin/python -m ila.api
