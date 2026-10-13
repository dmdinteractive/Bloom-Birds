#!/usr/bin/env bash
# Double-click to run the automated test suite (motion math, radio protocol, and a full
# simulated power-up -> home -> play). Takes about a minute.
source "$(dirname "$0")/_common.sh"
"$PYTHON" -m pip install --quiet pytest
echo
"$PYTHON" -m pytest tests -q && echo && echo "ALL TESTS PASSED" || echo "SOME TESTS FAILED (see above)"
echo
read -r -p "Press Return to close." _
