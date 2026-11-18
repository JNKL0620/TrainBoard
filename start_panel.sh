#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
test -f panel.json || cp panel.example.json panel.json
if test -x .venv/bin/python; then
  exec .venv/bin/python -m trainboard "$@"
fi
exec python3 -m trainboard "$@"
