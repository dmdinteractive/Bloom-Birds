# Kinetic Sculpture Controller

Synchronized control for 8–16 PD Stepper motors that spool cable in and out. You design
the motion in a browser, then a Raspberry Pi plays it on a loop from power-up.

```
 Laptop / tablet browser                      Raspberry Pi                         ESP32 bridge
 ┌───────────────────────┐  Wi-Fi/Ethernet  ┌─────────────────────────┐   USB    ┌──────────────┐
 │ Design · Axes ·       │ ───────────────► │ run.py                  │ ───────► │ bridge.ino   │
 │ Setup (editor)        │ ◄─────────────── │  engine: clips→positions│ ◄─────── │ ESP-NOW relay│
 └───────────────────────┘   live preview   │  limiter · 50 Hz frames │  status  └──────┬───────┘
                                            └─────────────────────────┘                 │ ESP-NOW radio
                                                                                        ▼ (no router)
                                  ┌──────────┐ ┌──────────┐ ┌──────────┐     ┌──────────┐
                                  │PD Stepper│ │PD Stepper│ │PD Stepper│ ... │PD Stepper│
                                  │ node.ino │ │ node.ino │ │ node.ino │     │ node.ino │
                                  └──────────┘ └──────────┘ └──────────┘     └──────────┘
                                   each powered by USB-C PD (e.g. PoE → USB-C PD splitter)
```

## Quick install on the Raspberry Pi (from GitHub)

On the Pi (Raspberry Pi OS 64-bit with desktop), in a terminal or Raspberry Pi Connect's
remote shell:

```bash
cd ~
git clone https://github.com/dmdinteractive/Bloom-Birds.git
cd Bloom-Birds/host
bash deploy/install_pi.sh
bash deploy/install_kiosk.sh
sudo reboot
```

To update later: `cd ~/Bloom-Birds && git pull && sudo systemctl restart kinetic`.
Your settings (`host/config.json`) and shows (`host/shows/`) aren't overwritten by updates.

## How the sync works

1. **The Pi does all the thinking.** Every 1/50 s it works out where every motor should be,
   from the clip that's playing, the crossfade into the next clip, and your layout.
2. **The limiter makes it physically possible.** Before anything is sent, each motor's path
   is capped to your max speed and acceleration, and kept within its travel. If a design
   asks for too much, the motion lags smoothly instead of jerking, and the editor marks
   those spots in red.
3. **One packet moves everyone.** The Pi sends a single *frame* holding a target position
   and speed for every motor. The bridge broadcasts it over ESP-NOW, so every PD Stepper
   receives the same packet at the same moment. That shared packet is what keeps them in sync.
4. **Each PD Stepper follows smoothly.** The FastAccelStepper library generates the step
   pulses in hardware, so the radio and encoder never make the motor stutter.
5. **Failsafe.** If frames stop arriving for 0.3 s (Pi crash, radio loss, bridge unplugged),
   every motor slows to a stop and holds.
6. **Soft limits.** Each motor's top and bottom are taught in **Motor Setup** and stored on
   the PD Stepper itself. The board refuses to move past them, whatever it is sent.

Positions are in **millimetres of cable paid out**. 0 is home (fully wound in, at the top)
and the maximum is your travel. The Pi converts millimetres to motor steps using the spool
diameter, so the firmware never needs to know it.

## What's in here

| Folder | What it is |
|---|---|
| `firmware/node/` | Arduino sketch for **every PD Stepper** |
| `firmware/bridge/` | Arduino sketch for **one ESP32-S3 dev board** plugged into the Pi |
| `host/` | Python controller + web editor that runs on the Pi |
| `host/tests/` | Automated tests: motion math, protocol, full simulated power-up |

`ks_protocol.h` defines the radio messages. It must be identical in both firmware folders.
A test checks this.

---

## 1. Try it on your laptop first (no hardware)

**On a Mac:** double-click `host/mac/Start Simulator.command`. The first time, use
right-click → Open. See [`host/mac/README-mac.md`](host/mac/README-mac.md) for details, testing
with the real bridge on the Mac, and which files carry over to the Pi.

**Any computer, from a terminal:**

```bash
cd host
pip install -r requirements.txt
python3 run.py --simulate
```

Open http://localhost:8080. The simulator acts like 8 PD Steppers plus one unassigned
node. It homes them, enables output, and plays the starter show, so you can design before any
hardware exists.

## 2. Flash the firmware

In the Arduino IDE:

- **Board package:** Boards Manager → install **"esp32" by Espressif**, version 3.x.
- **Libraries** (Library Manager): **FastAccelStepper** by gin66, and **TMC2209** by
  Peter Polidoro (janelia-arduino).
- **Board settings:** *ESP32S3 Dev Module*, *USB CDC On Boot: Enabled*.

Then flash:

- **Bridge:** open `firmware/bridge/bridge.ino` and upload it to a plain ESP32-S3 dev board.
- **Nodes:** open `firmware/node/node.ino` and upload it to each PD Stepper.

**Radio channel.** Both sketches use ESP-NOW channel 1 (`ESPNOW_CHANNEL`). If the venue's
Wi-Fi is busy on channel 1, change it to 6 or 11 in **both** sketches.

## 3. Wire each motor

- **Power:** a PoE+ (802.3at) to USB-C PD splitter, such as the PoE Texas GAT-USBC-REV2,
  into the PD Stepper's USB-C port. The default request is 20 V (set in Setup).
- **Encoder magnet (required):** on the back of the motor shaft, as in the PD Stepper build
  guide. Without home switches, the encoder is how each motor knows where it is.
- **Limit switch (optional):** not needed with the default *position memory* mode. If you
  ever add one, wire it normally-open between **AUX pin 3 (AUX2)** and **AUX pin 1 (GND)** and
  choose *Limit switch* under Setup → Mechanics → Homing.

## 3a. Mechanisms

Each motor has its own mechanism, chosen in the first step of Motor Setup:

| Mechanism | Units | Size setting | Ends taught |
|---|---|---|---|
| Cable spool | mm of cable | spool diameter | top / bottom |
| Linear actuator | mm | lead (mm per motor turn) | home end / far end |
| Rotary (angle) | degrees | gear ratio | start angle / end angle |
| Continuous rotation | rev/s | gear ratio | none: the show sets its **speed** |

Motion design always works in 0–1 (0 = home end, 1 = far end), so one clip can drive spools,
slides and arms at the same time. For continuous rotation, 0–1 is a speed: forward-only motors go
from stopped (0) to full speed (1); "both directions" motors go from full reverse (0) through
stopped (0.5) to full forward (1). Per-motor top speed and acceleration are in Setup → Axis Layout.

A board whose ends are taught never free-runs: the firmware refuses speed commands while it has
soft limits, so a spool can't be spun off its cable by a wrongly set mechanism.

## 3b. Motor Setup and position memory (no home switches)

**Every new or replaced PD Stepper goes through Motor Setup once.** Open it from the touch
panel (Axes → *Motor Setup*, or tap a *New node* card), from the editor (Axes → *Motor
Setup…*), or at `http://<pi>:8080/setup`.

| Step | What happens |
|---|---|
| 1. Choose motor | Pick the board (*Identify* flashes its LEDs) and the motor position it drives. The show stops. |
| 2. Direction | It moves 10 mm "down". You answer *did it lower?* If not, the direction is reversed for that motor only. |
| 3. Encoder | It moves 20 mm and back, comparing steps with the magnet reading. A backwards encoder is fixed automatically; a missing, loose or misplaced magnet is reported. |
| 4. Teach top | Jog up (50/10/1 mm buttons) to the highest safe spot, then *Set as top*. This is 0 mm. |
| 5. Teach bottom | Jog down to the lowest safe spot, then *Set as bottom*. Limits switch on immediately. |
| 6. Test | A slow move to the middle, the bottom and back to the top, inside the limits. |
| 7. Save | The motor's travel becomes the distance you taught. Shows scale to it automatically. |

**Adding and removing motors.** A new install starts with one motor.
- **Set how many positions** with the **Motors − N +** control (editor Axes tab, panel System view).
  **+** adds an empty position: it shows as a dashed card, isn't counted as a fault, and doesn't
  hold up startup. Set it up when its board is installed. **−** removes the last position (it asks
  first if that motor is set up).
- **Add a motor** with **Add Motor…** (editor Axes tab, panel System view, or the dashed **+ Add Motor**
  card on the panel's Axes view). It opens Motor Setup with *+ New motor* selected; the new slot is
  only kept if you finish the setup.
- **Remove a motor** with the **Remove** button next to it. Its board is released. The motors after it
  move down one number, and their boards are renumbered over the radio automatically. Their setup
  data and per-motor show data (static poses, per-motor curves in the active show) move with them.
- **Remove Unused** removes every motor that hasn't been set up and has no board reporting.

**How a motor knows its position after a power cut.** The encoder reports the shaft angle
exactly, but only within one turn. It doesn't count turns.
- **Taught top:** *Set as top* stores the magnet angle at 0 mm.
- **Saved record:** each board saves its position to flash when it has been at rest for 2 s
  outside the running show. It also saves the instant its supply voltage starts to collapse,
  cutting motor current first so the leftover charge runs only the processor.
- **At power-up:** saved position + live magnet angle = exact position. The saved number only
  needs to be within half a spool turn (about 60 mm on a 40 mm spool); the encoder supplies
  the precision.
- **If only one board reboots** (the Pi kept running), the Pi sends it the last position it
  saw.

**When a motor can't know where it is**, it says so and doesn't move. It doesn't guess. This
happens when the power-loss save failed while the motor was moving. The panel shows
*Position unknown: Motor Setup > Find top*. **Find top only** is the short version of Motor
Setup: jog to the top, *Set as top*, *Save*. The taught bottom is kept.

**Limits of this approach (read before installing):**
- **The load must not move while the power is off.** If it moved by more than half a spool
  turn, the restored position would be off by whole turns and nothing could detect it. You've
  said the sculpture is light enough to stay put. Check it on the bench by cutting power with
  a piece at mid-height and measuring after a few minutes.
- **Swapping boards between motors needs Motor Setup again.** The controller warns
  (*Different board than was set up*) if a motor position is driven by a different board
  than the one that was set up there.
- **Flash wear is low.** Saves happen when a motor comes to rest outside the show and at
  power loss: a handful per day, not per movement.

## 4. Install on the Raspberry Pi

Copy the `host/` folder to the Pi, then:

```bash
cd host
bash deploy/install_pi.sh
```

The controller now starts on every boot. Plug the bridge into the Pi's USB, then open
`http://<pi-name>.local:8080` from any device on the same network.

### Operator touch panel (Pi 5 + Touch Display 2)

**Hardware:**
- Raspberry Pi 5 (4 GB is plenty)
- Raspberry Pi Touch Display 2, 7" (1280×720). Ribbon to the DSI connector, power from the GPIO header.
- Official 27 W USB-C supply
- Pi 5 Active Cooler, because the Pi will be in an enclosure
- An NVMe SSD on the M.2 HAT+ (preferred), or a high-endurance microSD card

Use **Raspberry Pi OS with desktop**, then:

```bash
cd host
bash deploy/install_pi.sh      # controller service
bash deploy/install_kiosk.sh   # boots straight into the operator panel
sudo reboot
```

**Orientation:** the Touch Display 2 is a portrait panel. To get landscape, set the
orientation once in **Control Centre → Screens**, then check that touches land where you tap.

**What the panel offers** (`http://<pi>:8080/panel`, also usable from any tablet):

| Screen | What techs can do |
|---|---|
| Run | See what's playing and the next clip. Run Show, Next, Hold, Park. Live position gauges for every axis. |
| Axes | Status cards (link, homed, enabled, supply, encoder error, signal, faults). Per axis: Identify, Home, jog ±10/±50 mm, Set Zero, Release. Assign new nodes. |
| Playlist | Turn clips on or off, change repeat counts and order, Play from here. |
| Adjust | Show speed (25–200%) and depth limit (10–100%), saved across reboots. Power-up toggles. |
| System | Address of the full editor, bridge/radio health, startup sequence, home all, event log. |

**Typing:** the panel never needs text entry. Every value is a stepper, toggle or button, so
it doesn't depend on an on-screen keyboard (Raspberry Pi OS's keyboard doesn't appear over a
full-screen browser).

**Full editor:** keeps working from a laptop at `http://<pi>:8080`. Changes made from either
screen are saved to the same show file.

**Read-only filesystem:** because techs can now change the playlist and adjustments, leave
Raspberry Pi OS's read-only overlay **off**, or their changes would be lost on reboot. All
saves are written crash-safe instead: a power cut leaves either the old file or the new one,
and an unreadable file is set aside while a working show loads.

## 5. First-time setup checklist

1. **Setup → Mechanics:** enter the spool diameter (measure with cable on it), travel,
   and max speed/acceleration. Start conservative, e.g. 80 mm/s and 150 mm/s². Choose a
   homing mode. **Apply & Send to Axes.**
2. **Setup → Axis Layout:** set where each motor hangs, seen from above. Waves, ripples and
   spins use these positions. Add motors with **Add Motor…** as you build (section 3a).
3. **Axes tab:** new PD Steppers show as *unassigned*. Click **Identify** (its LEDs
   flash), then pick which motor it is.
4. **Motor Setup for every motor** (section 3a). It sets direction, checks the encoder and
   teaches the top and bottom, one motor at a time.
5. **Power-cut test:** while the show runs, unplug one motor's PoE cable for a few seconds and
   plug it back in. It should come back *Located*, with the Axes **Enc Δ** near 0. Then cut
   power to everything (Pi included) mid-show and check the same. If motors report *Position
   unknown* after a full cut, the power-loss save isn't completing on your hardware (see
   Honest status).
6. **Motion Design tab:** build clips and a playlist. Preview while **DISABLED** (screen only),
   then press **ENABLE** to drive the real sculpture.

On power-up, the controller finds the motors, sends their settings, homes them, enables output, and
plays the playlist in a loop (turn this off under Setup → Startup).

---

## Designing motion

**Pattern clips** are generators you shape with sliders. Changes apply live while the clip plays.

| Pattern | What it does |
|---|---|
| Breathe | All motors rise and fall together |
| Traveling wave | A wave rolls across the sculpture, in any direction |
| Ripple | Rings spread out from (or into) a point |
| Spin | A wave rotates around a center; best for circular layouts |
| Chase | A single dip travels across, optionally bouncing back |
| Alternate | Odd and even motors (or 3–4 groups) move out of phase |
| Organic drift | Smooth, never-repeating movement, with a neighbor-coherence slider |
| Static pose | A fixed height for each motor |

Every pattern also has **center height**, **amplitude**, **period**, and **dwell**. Dwell
adds a pause at the top and bottom of each cycle, which reads as "breathing."

**Timeline clips** are keyframe curves:

- Double-click the graph to add a key, and drag to move it (snaps to 0.25 s; hold Alt to
  place freely).
- Select a key to set its ease: smooth, linear, ease in/out, or hold.
- **Stagger** delays the same curve across motors, turning one curve into a wave. Order it
  by motor number, left-to-right, or center-out.
- Switch to **Separate curve per motor** to hand-animate individual motors.
- **Loop seamlessly** makes the last key flow back into the first.

**Playlist:** clips play top to bottom and the list loops forever. Each clip crossfades
into the next over its *Fade in* time, so transitions are always smooth.

Shows are saved as JSON files in `host/shows/`. Use **Save as…** to keep versions.

---

## Settings reference (`host/config.json`)

Created on first run and edited from the Setup tab.

- **Mechanics:**
  - Motion: spool diameter, travel, max speed and acceleration, microsteps.
  - Driver: run and hold current (% of the TMC2209 max), USB-PD voltage, stealthChop or
    spreadCycle.
  - Homing: mode (position memory, which is the default with no switches; limit switch;
    StallGuard; or manual), speed, back-off, timeout.
- **Per motor:** name, layout X/Y, trim (fine height offset), and a travel override.
  Motor Setup also stores, per motor, the taught bottom (in full motor steps, so re-measuring
  the spool diameter never moves it), its own motor and encoder direction, and which board
  was set up there.
- **Startup:** autoplay, auto-home, frame rate, serial port.

## Honest status

- **Firmware** passes a strict compile check against the PD Stepper pin map and the
  library APIs, but it has **not been run on real hardware yet**. Expect some tuning on
  the bench: run current, StallGuard sensitivity, and possibly the tracking gains at the
  top of `node.ino`.
- **Protocol version 3.** Speed mode for continuous rotation changed the radio messages (version 2 added position memory). Re-flash **both** the
  bridge and every node. The controller names any node still running old firmware in the
  event log.
- **The power-loss save is the part most likely to need bench work.** It depends on how
  long the PD Stepper keeps running after the supply drops. That's a few tens of
  milliseconds at best, and it depends on the board's and the PoE splitter's capacitors,
  which I couldn't find published.
  - The firmware watches the supply voltage every millisecond, cuts the motor current, then
    saves. ESP32 flash storage tolerates a write interrupted by power loss, so the worst case
    is "Position unknown", never a wrong position.
  - If full-power-cut tests keep ending in "Position unknown", options are a larger capacitor
    on the PD Stepper's supply input, or a small UPS for the PoE switch.
  - Watch the node's USB serial output: it prints `rec_clean` and `restored` at each status
    line.
- **Pi software** is covered by 55 automated tests. They include:
  - a full simulated power-up → arm → play sequence;
  - power cuts with and without a successful save, and ten power cuts in a row with no
    position drift;
  - a single board rebooting mid-show;
  - limit enforcement;
  - a complete Motor Setup run on a simulated board wired backwards with its magnet flipped.
- **Spool diameter:** if cable stacks in several layers, the effective diameter grows as it
  winds, so positions drift slightly with height. A wide spool with a single layer avoids
  this.
- **Radio:** ESP-NOW broadcasts have no acknowledgement. That's fine here because a fresh
  frame arrives every 20 ms, but place the bridge within good range of all nodes, and
  check the **RSSI** column (better than −75 dBm is comfortable).
