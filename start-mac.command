#!/bin/sh
# Double-click to start D-Link DSL Control on macOS. Close this window to stop it.
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is not installed. Get it from https://www.python.org/downloads/ and try again."
  read -r _; exit 1
fi
exec python3 run.py "$@"
