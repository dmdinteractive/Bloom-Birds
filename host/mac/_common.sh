#!/usr/bin/env bash
# Shared setup for the Mac launchers. Not meant to be opened directly.
# Finds Python 3.9+, creates a private Python environment in host/.venv-mac the first
# time, and installs the two libraries the controller needs (aiohttp, pyserial).
set -euo pipefail

HOST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$HOST_DIR/.venv-mac"
cd "$HOST_DIR"

find_python() {
  for p in python3.13 python3.12 python3.11 python3.10 python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
    if command -v "$p" >/dev/null 2>&1 && \
       "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
      command -v "$p"; return 0
    fi
  done
  return 1
}

if [ ! -x "$VENV/bin/python" ]; then
  echo "First run: setting up Python (takes about a minute, needs internet)..."
  if ! PY="$(find_python)"; then
    echo
    echo "Python 3.9 or newer was not found."
    echo "Install it from https://www.python.org/downloads/macos/ (the 'macOS 64-bit universal2"
    echo "installer'), or if macOS offers to install the Command Line Developer Tools, accept that."
    echo "Then double-click this launcher again."
    read -r -p "Press Return to close." _
    exit 1
  fi
  echo "Using $PY ($("$PY" --version))"
  "$PY" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
fi
# Cheap when already installed; picks up any new requirements after an update.
"$VENV/bin/python" -m pip install --quiet -r requirements.txt
PYTHON="$VENV/bin/python"
