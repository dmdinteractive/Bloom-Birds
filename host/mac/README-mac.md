# Running on a Mac

This is the same program the Raspberry Pi runs. Nothing is a separate "Mac version", so
anything you test here behaves the same way on the Pi.

## One-time setup

1. **Python 3.9 or newer.** Check by opening Terminal and typing `python3 --version`.
   - If macOS offers to install the *Command Line Developer Tools*, accept. That includes Python.
   - Or install the "macOS 64-bit universal2 installer" from
     <https://www.python.org/downloads/macos/>.
2. **Allow the launchers to open.** The first time you open each `.command` file, macOS may say
   it is from an *unidentified developer*. **Right-click (Control-click) the file → Open → Open.**
   macOS remembers this afterwards.
   - On recent macOS versions you may instead need **System Settings → Privacy & Security →
     Open Anyway**.

The first launch takes about a minute. It creates a private Python environment in
`host/.venv-mac` and installs two libraries: aiohttp (the web server) and pyserial (USB).
It doesn't touch the rest of your Mac. To remove it, delete that folder.

## The three launchers (in `host/mac/`)

| Double-click | What it does |
|---|---|
| **Start Simulator.command** | Asks how many motors (default 5), then runs with **simulated** PD Steppers. Opens the editor and the touch panel in your browser. No hardware needed. |
| **Start with Bridge.command** | Runs with the **real** ESP-NOW bridge plugged into the Mac's USB, driving real PD Steppers. |
| **Run Tests.command** | Runs the 33 automated tests (motion math, radio protocol, simulated power-up). |

To stop, press **Ctrl+C** in the Terminal window, or close it. On real hardware, the motors
stop within 0.3 s once frames stop arriving.

Both screens are at:
- **Editor:** <http://localhost:8080>
- **Touch panel:** <http://localhost:8080/panel>. Make the browser window about 1280 × 720 to
  see it as it will look on the 7" display.

## What you can test in the simulator

The simulator acts like real PD Steppers. They report status 5 times a second, home in a few
seconds, follow every motion frame, and there's one extra *unassigned* node for practicing
assignment. You can try:

- **Motion design:** build pattern and timeline clips, preview them, arrange the playlist.
- **Operator panel:** Run/Next/Hold/Park, speed and depth adjustments, playlist on/off, jog,
  set zero, identify, assign the unassigned node (including the duplicate-ID warning).
- **Motor Setup:** the simulator includes one extra *new board*, deliberately built wrong
  (motor wired backwards, magnet flipped). Run Motor Setup on it to see the wizard catch both.
  The setup screen shows a yellow *Simulator* box with where the load really is. Use it in
  place of watching the piece.
- **Power cuts:** closing the simulator window and starting it again is a power cut where
  every motor's power-loss save works. Motors come back *Located* at the same spot and the
  show resumes.
  - The buttons **Sim: Power Cut** and **Sim: Power Cut, Save Fails** are under the editor's
    Axes tab and the panel's System view.
  - The second one shows what techs see when a motor loses its position, and how
    *Find top only* fixes it.
- **Settings:** spool diameter, travel, speed and acceleration limits. Clips that ask for too
  much show red rate-limit marks.

It can't test radio range, real motor torque, StallGuard tuning, or the limit-switch
wiring. Those need the bench.

## Which files go to the Pi

- **Shows** are saved in `host/shows/`. The simulator and the Pi use the same files, so you
  can design shows on the Mac and copy that folder to the Pi.
- **Settings** for the simulator are kept in `host/config-sim.json`, separate from the real
  `host/config.json`. Test settings never end up on the Pi by accident.
- `Start with Bridge` uses the real `config.json`. On first run it assumes 8 motors. Set the
  real count under **Setup → Axis Layout**.

## Testing with the real bridge on the Mac

1. Plug the bridge's ESP32-S3 into the Mac with a USB-C data cable.
2. The Mac names it something like `/dev/cu.usbmodem1101`, and the controller finds it
   automatically. The launcher lists the USB serial devices it can see.
3. If it isn't found, check the cable carries data (some are charge-only), and that the board
   was flashed with *USB CDC On Boot: Enabled*.
4. Keep the PD Steppers within range of the Mac.

## Problems

- **"Port 8080 is already in use":** the controller is already running in another Terminal
  window. Close it first.
- **The browser didn't open:** open <http://localhost:8080> yourself.
- **Start over with a clean simulator:** delete `host/config-sim.json` and
  `host/config-sim-nodes.json` (the simulated motors' memory).
- **Reinstall the Python environment:** delete the `host/.venv-mac` folder.
