#!/usr/bin/env bash
# One-command start: sets up the virtualenv on first run, then launches.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "First run — setting up a local Python environment…"
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements.txt
  echo "Done."
fi

exec ./.venv/bin/python run.py "$@"
