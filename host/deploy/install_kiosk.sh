#!/usr/bin/env bash
# Turn a Raspberry Pi 5 + Touch Display 2 into a dedicated operator panel.
# Requires Raspberry Pi OS *with desktop* (labwc/Wayland). Run AFTER deploy/install_pi.sh:
#
#     bash deploy/install_kiosk.sh
#
# What it does
#   - boots to the desktop and logs in automatically
#   - turns off screen blanking
#   - launches Chromium full screen on http://localhost:8080/panel once the controller is up,
#     and relaunches it if it ever closes
#
# To get out of kiosk mode for maintenance: plug in a keyboard and press Alt+F4, or SSH in and run
#     pkill -f kinetic-kiosk; pkill chromium
set -euo pipefail

PANEL_URL="${PANEL_URL:-http://localhost:8080/panel}"
BIN="$HOME/.local/bin"
mkdir -p "$BIN" "$HOME/.config/labwc"

# Chromium is named "chromium" on current Raspberry Pi OS, "chromium-browser" on older images.
if ! command -v chromium >/dev/null && ! command -v chromium-browser >/dev/null; then
  sudo apt-get update
  sudo apt-get install -y chromium || sudo apt-get install -y chromium-browser
fi
command -v curl >/dev/null || sudo apt-get install -y curl

# Desktop autologin + no screen blanking
sudo raspi-config nonint do_boot_behaviour B4
sudo raspi-config nonint do_blanking 1

cat > "$BIN/kinetic-kiosk.sh" <<EOF
#!/usr/bin/env bash
# Launched by labwc at login. Waits for the controller, then keeps the panel on screen.
URL="$PANEL_URL"
BROWSER=\$(command -v chromium || command -v chromium-browser)
until curl -fs -o /dev/null "\$URL"; do sleep 2; done
while true; do
  "\$BROWSER" --kiosk "\$URL" \\
    --noerrdialogs --disable-infobars --no-first-run \\
    --disable-session-crashed-bubble --disable-features=TranslateUI \\
    --overscroll-history-navigation=0 --disable-pinch \\
    --password-store=basic --user-data-dir="\$HOME/.config/kinetic-kiosk"
  sleep 3   # if Chromium exits or crashes, bring it back
done
EOF
chmod +x "$BIN/kinetic-kiosk.sh"

AUTOSTART="$HOME/.config/labwc/autostart"
touch "$AUTOSTART"
grep -q "kinetic-kiosk.sh" "$AUTOSTART" || echo "$BIN/kinetic-kiosk.sh &" >> "$AUTOSTART"

echo
echo "Kiosk installed. Reboot to start:  sudo reboot"
echo
echo "Screen orientation: the Touch Display 2 is a portrait panel. For the landscape operator"
echo "panel, open Control Centre > Screens on the desktop, right-click the DSI display,"
echo "choose Orientation (Left or Right, depending on how it is mounted), then Apply."
echo "Check that touches land where you tap after rotating."
