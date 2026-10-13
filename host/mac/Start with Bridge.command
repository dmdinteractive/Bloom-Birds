#!/usr/bin/env bash
# Double-click to run the controller on this Mac with the REAL ESP-NOW bridge plugged into
# a USB port, driving real PD Steppers. Same software the Raspberry Pi will run.
source "$(dirname "$0")/_common.sh"

echo
echo "USB serial devices found:"
ls /dev/cu.usbmodem* /dev/cu.usbserial* /dev/cu.wchusbserial* /dev/cu.SLAB_USBtoUART* 2>/dev/null \
  || echo "  (none - plug the bridge in; the controller will keep looking for it)"
echo
echo "Starting with hardware. Motors will home and the show will play automatically"
echo "unless that is switched off under Setup > Startup."
echo "  Editor:      http://localhost:8080"
echo "  Touch panel: http://localhost:8080/panel"
echo "Press Ctrl+C or close this window to stop (motors stop within 0.3 s)."
echo
exec "$PYTHON" run.py --local --open --config config.json --shows shows
