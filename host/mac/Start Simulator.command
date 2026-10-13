#!/usr/bin/env bash
# Double-click to run the controller on this Mac with SIMULATED motors (no hardware needed).
# The editor and the touch panel open in your browser. Close this window (or press Ctrl+C)
# to stop.
source "$(dirname "$0")/_common.sh"

echo
read -r -p "How many motors should the simulator have? [5] " AXES
AXES="${AXES:-5}"
case "$AXES" in ''|*[!0-9]*) AXES=5 ;; esac

echo
echo "Starting the simulator with $AXES motors..."
echo "  Editor:      http://localhost:8080"
echo "  Touch panel: http://localhost:8080/panel"
echo "Leave this window open while testing. Press Ctrl+C or close it to stop."
echo
exec "$PYTHON" run.py --simulate --axes "$AXES" --local --open \
     --config config-sim.json --shows shows
