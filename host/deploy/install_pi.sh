#!/usr/bin/env bash
# Install the kinetic sculpture controller on a Raspberry Pi (Raspberry Pi OS Bookworm).
# Run from the host/ folder:   bash deploy/install_pi.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(whoami)"

echo "Installing into $DIR for user $USER_NAME"
sudo apt-get update
# python3-gpiozero + python3-lgpio: buttons and sensors on the GPIO header (external triggers)
sudo apt-get install -y python3-venv python3-gpiozero python3-lgpio

# --system-site-packages lets the private environment use the GPIO libraries installed above
# (re-running this script on an older install switches the existing environment over)
python3 -m venv --system-site-packages "$DIR/.venv"
"$DIR/.venv/bin/pip" install --upgrade pip
"$DIR/.venv/bin/pip" install -r "$DIR/requirements.txt"

# serial port access for the USB bridge
sudo usermod -aG dialout "$USER_NAME"
# USB MIDI controllers (audio) and the GPIO header (gpio), for external triggers
sudo usermod -aG audio,gpio "$USER_NAME"

sed -e "s#__DIR__#$DIR#g" -e "s#__USER__#$USER_NAME#g" "$DIR/deploy/kinetic.service" | sudo tee /etc/systemd/system/kinetic.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable kinetic.service
sudo systemctl restart kinetic.service

echo
echo "Done. The controller starts on every boot."
echo "Open http://$(hostname).local:8080 from a laptop on the same network."
echo "Logs:  journalctl -u kinetic -f"
echo "(Log out and back in once so the serial-port permission applies to manual runs.)"
