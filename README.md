# Bloom Birds Controller

Synchronized control for 1–16 PD Stepper motors. A Raspberry Pi plays one looping move (a
gentle sine flap) from power-up. You set four knobs; the motors' own measured limits decide
how fast it can go.

> **Branch `simple-shows`.** This is the simplified version. The earlier clip, timeline and
> trigger editor is on `main`. Motor Setup, the firmware and the radio protocol are unchanged,
> so nothing needs re-flashing to switch between the two.

```
 Laptop / tablet browser                      Raspberry Pi                         ESP32 bridge
 ┌───────────────────────┐  Wi-Fi/Ethernet  ┌─────────────────────────┐   USB    ┌──────────────┐
 │ Moves screen          │ ───────────────► │ run.py                  │ ───────► │ bridge.ino   │
 │ (knobs, birds, speed) │ ◄─────────────── │  engine: move→positions  │ ◄─────── │ ESP-NOW relay│
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
git clone -b simple-shows https://github.com/dmdinteractive/Bloom-Birds.git
cd Bloom-Birds/host
bash deploy/install_pi.sh
bash deploy/install_kiosk.sh
sudo reboot
```

To update later: `cd ~/Bloom-Birds && git pull && sudo systemctl restart kinetic`.

Already installed from `main`? Switch that install over with:
`cd ~/Bloom-Birds && git fetch origin simple-shows && git checkout simple-shows && sudo systemctl restart kinetic`.
Your motor setup (`host/config.json`) is kept. The old show file is kept as
`host/shows/default.old-format.json` and a new flap show is started.
Your settings (`host/config.json`) and shows (`host/shows/`) aren't overwritten by updates.

## How the sync works

1. **The Pi does all the thinking.** Every 1/50 s it works out where every motor should be,
   from the move that's playing.
2. **The limiter makes it physically possible.** Before anything is sent, each motor's path
   is capped to that motor's measured top speed and acceleration, and kept within its travel.
   If the flap is set faster than that, the motion lags smoothly instead of jerking, and both
   screens say so in plain words.
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
| `host/` | Python controller + web screens that run on the Pi |
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

Open http://localhost:8080. The simulator acts like PD Steppers plus one unassigned
node (`--axes 1` for one bird, `--axes 5` for five). It enables output and plays the flap, so you
can try the knobs before any hardware is connected.

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

The show always works in 0–1 (0 = home end, 1 = far end), so one move can drive spools,
slides and arms at the same time. For continuous rotation, 0–1 is a speed: forward-only motors go
from stopped (0) to full speed (1); "both directions" motors go from full reverse (0) through
stopped (0.5) to full forward (1). Per-motor top speed and acceleration come from the speed test.

A board whose ends are taught never free-runs: the firmware refuses speed commands while it has
soft limits, so a spool can't be spun off its cable by a wrongly set mechanism.

## 3b. Motor Setup and position memory (no home switches)

**Every new or replaced PD Stepper goes through Motor Setup once.** Open it from the touch
panel (Axes → *Motor Setup*, or tap a *New node* card), from the Moves screen (Birds → *Motor
Setup…*), or at `http://<pi>:8080/setup`.

| Step | What happens |
|---|---|
| 1. Choose motor | Pick the board (*Identify* flashes its LEDs) and the motor position it drives. The show stops. |
| 2. Direction | It moves 10 mm "down". You answer *did it lower?* If not, the direction is reversed for that motor only. |
| 3. Encoder | It moves 20 mm and back, comparing steps with the magnet reading. A backwards encoder is fixed automatically; a missing, loose or misplaced magnet is reported. |
| 4. Teach top | Jog up (50/10/1 mm buttons) to the highest safe spot, then *Set as top*. This is 0 mm. |
| 5. Teach bottom | Jog down to the lowest safe spot, then *Set as bottom*. Limits switch on immediately. |
| 6. Test | A slow move to the middle, the bottom and back to the top, inside the limits. |
| 7. Speed test (optional) | Measures how fast this motor can move **with its real load**. See below. |
| 8. Save | The motor's travel becomes the distance you taught. Shows scale to it automatically. |

**Speed test.** The default limits (150 mm/s and 300 mm/s² on a spool, about 72 RPM on a 40 mm
spool) are deliberately cautious. The speed test finds what each motor can really do:
- It runs back and forth over the middle 60% of the taught travel (continuous motors: 10 turns
  at a time), starting at half the current limit and going 30% faster each run.
- After every run it compares where the driver stepped to with where the encoder says the shaft
  went. A gap of more than about 1/20 of a turn means the motor slipped, and the test stops
  there. If a run slips while it was also ramping harder than any clean run, it retries that
  speed once with the gentlest ramp the stroke allows, so a steep ramp isn't mistaken for a
  speed limit.
- Then it raises the acceleration at 80% of the top speed found, until that slips too.
- It suggests **70% of the clean top speed** and **60% of the clean acceleration**, and shows
  every run in a table. *Use* saves them for that motor; *Keep current* doesn't.
- After a slip it re-syncs the motor's position to the encoder, so the taught limits stay true.
  The test never runs above 1200 RPM or outside the taught limits.
- **Run it with the real load attached**, and again after changing the load, run current or
  supply voltage. A slipped run makes a buzz or clunk; that's expected and harmless.
- For a motor that's already set up, choose it in Motor Setup and press **Speed test only**.

**Adding and removing motors.** A new install starts with one motor.
- **Set how many positions** with the **Motors − N +** control (panel System view).
  **+** adds an empty position: it shows as a dashed card, isn't counted as a fault, and doesn't
  hold up startup. Set it up when its board is installed. **−** removes the last position (it asks
  first if that motor is set up).
- **Add a motor** with **Add a bird…** on the Moves screen, or **Add Motor…** on the panel (System view, or the
  dashed **+ Add Motor** card on the Axes view). It opens Motor Setup with *+ New motor* selected; the new slot is
  only kept if you finish the setup.
- **Remove a motor** with the **Remove** button next to it. Its board is released. The motors after it
  move down one number, and their boards are renumbered over the radio automatically. Their setup
  data moves with them.
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
| Run | See what's playing. Run Show, Hold, Park. Live position gauges for every axis; an amber outline marks a motor being slowed to its limit. |
| Axes | Status cards (link, homed, enabled, supply, encoder error, signal, faults). Per axis: Identify, Home, jog ±10/±50 mm, Set Zero, Release. Assign new nodes. |
| Flap | The four knobs (top, bottom, seconds per flap, delay between birds) as − / + buttons, *Set to fastest the motors can do*, and the power-up toggles. |
| System | Address of the Moves screen, bridge/radio health, startup sequence, home all, event log. |

**Typing:** the panel never needs text entry. Every value is a stepper, toggle or button, so
it doesn't depend on an on-screen keyboard (Raspberry Pi OS's keyboard doesn't appear over a
full-screen browser).

**Moves screen:** works from a laptop or tablet at `http://<pi>:8080`. Changes made from either
screen are saved to the same show file.

**Read-only filesystem:** because techs can change the flap from the panel, leave
Raspberry Pi OS's read-only overlay **off**, or their changes would be lost on reboot. All
saves are written crash-safe instead: a power cut leaves either the old file or the new one,
and an unreadable file is set aside while a working show loads.

## 5. First-time setup checklist

1. **Motor Setup for every bird** (section 3b). It sets direction, checks the encoder, teaches
   the two ends, and offers the **speed test**. Start with one bird; add the rest with
   **Add a bird…** when they are ready. The show runs with whichever birds are set up.
2. **Run the speed test** with the bird's real load attached. This is what sets how fast the
   show may go (see *Speed* below).
3. **Power-cut test:** while the show runs, unplug one motor's PoE cable for a few seconds and
   plug it back in. It should come back *Located*. Then cut power to everything (Pi included)
   mid-show and check the same. If motors report *Position unknown* after a full cut, the
   power-loss save isn't completing on your hardware (see Honest status).
4. **Moves screen:** set the flap's knobs. With the motors **DISABLED** it plays on screen only;
   press **ENABLE** to drive the real birds.

On power-up, the controller finds the motors, sends their settings, checks they know their
positions, enables output, and loops the flap (turn this off with *Run show at power-up*).

---

## The show: one move, four knobs

The show is a single move that loops: **Gentle flap**. Each bird glides along a sine curve
between a top and a bottom height.

| Knob | What it does |
|---|---|
| **Top** | Highest point, as a share of that bird's taught travel (0% = home end). |
| **Bottom** | Lowest point (100% = far end). |
| **Seconds per flap** | Time for one full down-and-up. Smaller is faster. |
| **Delay between birds** | How far each bird trails the one before it, as a share of one flap. 0% = all together; 20% with five birds makes one continuous wave. |

Change them on the **Moves screen** (`http://<pi>:8080`, sliders and number boxes) or on the
touch panel's **Flap** view (− / + buttons). Changes take effect at once, without a jump, and
are saved to `host/shows/default.json`.

**One bird or five.** Only birds that have been through Motor Setup take a place in line for
the delay, so the same show runs with one bird on the bench or all five in the space.

**Run / Hold / Park.** *Run* eases from wherever the birds are into the flap over three
seconds. *Hold* stops them where they are. *Park* moves them all to their home end.

### Speed: the hardware decides

There is no speed number to type in. How fast the flap may go comes from each motor:

1. **The speed test** (Motor Setup → *Speed test*, or *Setup / speed test* next to a bird) runs
   the motor back and forth with its real load, faster each time, until the encoder shows it
   missed steps. It saves limits a margin below the last clean run (70% of the speed, 60% of the
   acceleration).
2. The result says **which limit it hit**: *the motor is the limit* (it slipped), or *it never
   slipped* (the test stops at 1,200 motor RPM). It also lists the run current, supply voltage
   and driver mode it was tested with.
3. From those limits the controller works out the **fastest flap** each bird can follow for the
   stroke you set: a sine of stroke *d* and period *T* peaks at speed π·d/T and acceleration
   2π²·d/T². Both screens show that number and have a **Set to fastest** button.
4. If you set the flap faster anyway, the birds lag smoothly, their bars get an amber outline,
   and the screen says *Too fast for the motors*.

A motor that hasn't been tested uses a cautious default (150 mm/s and 300 mm/s² for a spool,
about 72 motor RPM on a 40 mm spool). The Birds table shows which motors are still on the
default.

**To get more speed out of the same motor**, change these under Moves screen → Settings, then
run the speed test again:
- **Run current** (default 60%). More current is more torque, and more heat.
- **Supply voltage** (default 20 V, the most the PD Stepper requests).
- **Driver mode.** *Auto* is quiet when slow and strong when fast. *Always quiet* is weak at speed.

If the motor is the limit at 100% current and 20 V, software can't go further. A shorter stroke,
a larger spool (more cable per turn, less torque) or a different motor or driver are the options.

### Adding another move later

Moves live in `host/kinetic/motion.py`: one entry in `MOVES` (its name and knobs) and one
function in `MOVE_FUNCS` (heights for every bird at a point in the cycle). The screens build
their controls from that list.

---

## Settings reference (`host/config.json`)

Created on first run. The Moves screen's Settings box edits the ones that affect speed; the rest
can be edited in the file (restart the controller afterwards).

- **Mechanics:**
  - Motion: spool diameter, travel, max speed and acceleration, microsteps.
  - Driver: run and hold current (% of the TMC2209 max), USB-PD voltage, and **Driver
    mode**:
    - *Auto* (default): quiet stealthChop at low speed, switching to spreadCycle above the
      **switch speed** (motor RPM, default 150) for more torque.
    - *Always silent* (stealthChop): quietest, but less torque at speed.
    - *Always strong* (spreadCycle): audible.

    StallGuard homing needs stealthChop. Auto switching needs node firmware 4; older nodes
    stay in stealthChop.
  - Homing: mode (position memory, which is the default with no switches; limit switch;
    StallGuard; or manual), speed, back-off, timeout.
- **Per motor:** name, trim (fine height offset), a travel override, and max
  speed / acceleration (set by the speed test).
  Motor Setup also stores, per motor, the taught bottom (in full motor steps, so re-measuring
  the spool diameter never moves it), its own motor and encoder direction, and which board
  was set up there.
- **Startup:** autoplay, auto-home, frame rate, serial port.

## Honest status

- **This branch has only been run against the simulator.** The firmware is unchanged from
  `main`, which has run on the real PD Steppers. The flap, the fastest-flap calculation and the
  screens are new and have not driven real motors yet.
- **Protocol version 3.** Speed mode for continuous rotation changed the radio messages (version 2 added position memory). Re-flash **both** the
  bridge and every node. The controller names any node still running old firmware in the
  event log.
- **Node firmware 4** adds automatic stealthChop/spreadCycle switching (TMC2209 `TPWMTHRS`
  register; see the [TMC2209 datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/TMC2209_datasheet_rev1.08.pdf)).
  The radio protocol is unchanged, so only the **nodes** need re-flashing, not the bridge.
- **Speed test:** checked against simulated motors that slip above a set speed and
  acceleration. On real hardware, how quickly the encoder can follow a fast shaft hasn't been
  measured. If it can't keep up, the test reads that as a slip and stops early, which is the
  safe direction.
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
- **Pi software** is covered by 46 automated tests. They include:
  - a full simulated power-up → arm → play sequence;
  - power cuts with and without a successful save, and ten power cuts in a row with no
    position drift;
  - a single board rebooting mid-show;
  - limit enforcement;
  - a complete Motor Setup run on a simulated board wired backwards with its magnet flipped.
  - the speed test finding a simulated motor's slip limits and keeping its position;
  - the fastest-flap calculation against hand-worked physics, and a flap at that pace being
    followed with no lag;
  - changing the pace or stopping and starting mid-flap without a jump.
- **Spool diameter:** if cable stacks in several layers, the effective diameter grows as it
  winds, so positions drift slightly with height. A wide spool with a single layer avoids
  this.
- **Radio:** ESP-NOW broadcasts have no acknowledgement. That's fine here because a fresh
  frame arrives every 20 ms, but place the bridge within good range of all nodes, and
  check the **RSSI** column (better than −75 dBm is comfortable).
