"""Links between the Pi and the motors.

SerialTransport  - real hardware: USB serial to the ESP-NOW bridge.
SimTransport     - no hardware: simulated PD Steppers that behave like the firmware,
                   so the editor, homing flow and playback can be tested at a desk.
Both call `on_packet(mac, rssi, payload)` for every packet from a node and
`on_bridge(BridgeInfo)` for bridge heartbeats.
"""
from __future__ import annotations

import glob
import json
import logging
import math
import os
import threading
import time
from typing import Callable

from . import protocol as P

log = logging.getLogger("kinetic.transport")

PacketCb = Callable[[str, int, bytes], None]
BridgeCb = Callable[[P.BridgeInfo], None]


class Transport:
    def __init__(self):
        self.on_packet: PacketCb = lambda mac, rssi, pkt: None
        self.on_bridge: BridgeCb = lambda info: None
        self.connected = False
        self.port_name = ""

    def start(self): ...
    def stop(self): ...
    def send(self, payload: bytes): ...


class SerialTransport(Transport):
    def __init__(self, port: str = "auto", baud: int = 921600):
        super().__init__()
        self.want_port = port
        self.baud = baud
        self._ser = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_bridge_seen = 0.0

    def start(self):
        self._thread = threading.Thread(target=self._run, name="serial", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _find_port(self) -> str | None:
        if self.want_port and self.want_port != "auto":
            return self.want_port
        # Linux / Raspberry Pi first, then macOS names. On a Mac an ESP32-S3 using native USB
        # shows up as /dev/cu.usbmodem*; boards with a USB-serial chip as cu.usbserial*,
        # cu.wchusbserial* (CH340) or cu.SLAB_USBtoUART* (CP210x).
        ports = []
        for pat in ("/dev/ttyACM*", "/dev/ttyUSB*", "/dev/cu.usbmodem*", "/dev/cu.usbserial*",
                    "/dev/cu.wchusbserial*", "/dev/cu.SLAB_USBtoUART*"):
            ports += sorted(glob.glob(pat))
        return ports[0] if ports else None

    def _run(self):
        import serial  # pyserial
        deframer = P.SerialDeframer()
        while not self._stop.is_set():
            if self._ser is None:
                port = self._find_port()
                if not port:
                    self.connected = False
                    time.sleep(1.0)
                    continue
                try:
                    self._ser = serial.Serial(port, self.baud, timeout=0.05)
                    self.port_name = port
                    log.info("opened %s", port)
                except Exception as e:  # noqa: BLE001
                    log.warning("cannot open %s: %s", port, e)
                    time.sleep(2.0)
                    continue
            try:
                data = self._ser.read(512)
            except Exception as e:  # noqa: BLE001
                log.warning("serial lost: %s", e)
                self._close()
                continue
            for payload in deframer.feed(data):
                self._dispatch(payload)
            self.connected = time.monotonic() - self.last_bridge_seen < 3.0

    def _dispatch(self, payload: bytes):
        if len(payload) >= 4 and payload[:2] == P.MAGIC and payload[3] == P.T_BRIDGE:
            info = P.parse_bridge(payload)
            if info:
                self.last_bridge_seen = time.monotonic()
                self.on_bridge(info)
            return
        if len(payload) > 7:
            mac = P.mac_str(payload[:6])
            rssi = payload[6] - 256 if payload[6] > 127 else payload[6]
            self.on_packet(mac, rssi, payload[7:])

    def _close(self):
        with self._lock:
            try:
                if self._ser:
                    self._ser.close()
            finally:
                self._ser = None
                self.connected = False

    def send(self, payload: bytes):
        with self._lock:
            if not self._ser:
                return
            try:
                self._ser.write(P.ser_frame(payload))
            except Exception as e:  # noqa: BLE001
                log.warning("write failed: %s", e)


# ---------------------------------------------------------------- simulator

FULL_STEPS = 200
CNT = 4096          # AS5600 counts per turn


class _SimNode:
    """Behaves like node.ino (fw 2): steps, a magnetic encoder that only knows the angle within
    one turn, a taught calibration, a saved position record, soft limits and power cycles.

    "Physical" truth (where the load really is) is kept separately from what the node believes,
    so the setup wizard and power-cut recovery are tested for real. Each node can be built with
    its motor wired backwards and/or its magnet reversed, like a real build might be.
    """

    def __init__(self, mac: str, sid: int, cfg: list[int], wiring_rev=False, magnet_rev=False, mount=0.0):
        self.mac = mac
        self.id = sid
        self.cfg = list(cfg)
        # hardware truth
        self.wiring_rev = wiring_rev
        self.magnet_rev = magnet_rev
        self.mount = mount          # magnet angle (counts) at the physical top
        self.phys_fs = 0.0          # where the load really is, in full steps below the top
        # what the motor + load can really do: above these the rotor slips and stops following
        # the steps (and stays stalled until the step rate drops), like a real stepper
        self.stall_fs = 2000.0      # full steps/s (600 RPM)
        self.stall_acc_fs = 8000.0  # full steps/s² (40 rev/s²)
        self.stalled = False
        self.jog_speed = 0.0        # microsteps/s of the current jog (the firmware's setSpeedInHz)
        # non-volatile memory (flash)
        self.cal: dict | None = None       # {"raw", "ms", "pos"}
        self.rec: dict | None = None       # {"pos", "ms", "clean"}
        self.lim: dict | None = None       # {"on", "lo", "hi", "ms"}
        self.nv_dirty = True
        self.boot()

    # ---- helpers mirroring the firmware
    @property
    def ms(self) -> int:
        return max(1, int(self.cfg[2]))

    @property
    def spr(self) -> int:
        return FULL_STEPS * self.ms

    def _rescale(self, v: float, from_ms: int) -> float:
        return v if not from_ms or from_ms == self.ms else v * self.ms / from_ms

    def _dir_sign(self) -> int:
        return -1 if (self.wiring_rev ^ bool(self.cfg[5] & 1)) else 1

    def _enc_sign(self) -> int:
        return -1 if self.cfg[5] & 2 else 1

    def _unwrapped(self) -> float:
        return self.mount + (-1 if self.magnet_rev else 1) * self.phys_fs * CNT / FULL_STEPS

    def raw(self) -> int:
        return int(math.floor(self._unwrapped())) % CNT

    def enc_total(self) -> int:
        return int(math.floor(self._unwrapped())) - self.enc_boot

    def encoder_steps(self) -> int:
        return int(self._enc_sign() * (self.enc_total() - self.enc_zero) * self.spr / CNT)

    def memory_mode(self) -> bool:
        return self.cfg[6] == 3

    def limits_on(self) -> bool:
        return bool(self.lim and self.lim["on"])

    def lim_lo(self) -> float:
        return self._rescale(self.lim["lo"], self.lim["ms"]) if self.lim else 0

    def lim_hi(self) -> float:
        return self._rescale(self.lim["hi"], self.lim["ms"]) if self.lim else 0

    def clamp(self, p: float) -> float:
        if not self.limits_on() or not self.homed:
            return p
        return min(max(p, self.lim_lo()), self.lim_hi())

    def anchor(self, p: float):
        self.steps = float(p)
        self.enc_zero = self.enc_total() - self._enc_sign() * int(p * CNT / self.spr)
        self.target = self.steps
        self.jog_target = None
        self.homed, self.home_failed = True, False

    def best_pos(self) -> int:
        return self.encoder_steps() if self.homed else int(self.steps)

    def phase_position(self, approx: float) -> int:
        d = (self.raw() - self.cal["raw"]) % CNT
        base = self._rescale(self.cal["pos"], self.cal["ms"]) + self._enc_sign() * d * self.spr / CNT
        k = round((approx - base) / self.spr)
        return int(base + k * self.spr)

    def write_rec(self, pos: float, clean: bool):
        self.rec = {"pos": int(pos), "ms": self.ms, "clean": clean}
        self.nv_dirty = True

    # ---- power-up
    def boot(self):
        self.steps = 0.0            # FastAccelStepper starts counting at 0
        self.vel = 0.0
        self.target = 0.0
        self.ff = 0.0
        self.enabled = False
        self.homed = False
        self.homing_until = 0.0
        self.home_failed = False
        self.streaming = False
        self.last_frame = 0.0
        self.link_lost = False
        self.last_seq = 0
        self.jog_target: float | None = None
        self.vel_mode = False           # continuous rotation: run at target_speed
        self.target_speed = 0.0
        self.boot_t = time.monotonic()
        self.identify_until = 0.0
        self.enc_boot = int(math.floor(self._unwrapped()))
        self.enc_zero = 0
        self.restored = self.restored_host = self.pos_lost = False
        self.rest_since = 0.0
        if self.memory_mode() and self.cal:
            if self.rec and self.rec["clean"]:
                p = self.phase_position(self._rescale(self.rec["pos"], self.rec["ms"]))
                if self.limits_on() and not (self.lim_lo() - self.spr / 2 <= p <= self.lim_hi() + self.spr / 2):
                    self.pos_lost = True
                else:
                    self.anchor(p)
                    self.restored = True
            else:
                self.pos_lost = True

    def power_fail_save(self):
        if self.homed and self.cal:
            self.write_rec(self.best_pos(), True)

    # ---- motion
    def step(self, dt: float, now: float):
        amax = max(1.0, self.cfg[4])
        vmax = max(1.0, self.cfg[3])
        if self.jog_target is not None and not self.streaming and self.jog_speed > 0:
            vmax = self.jog_speed          # firmware: jog speed comes with the command, not the cap
        before = self.steps
        v_before = self.vel
        if self.homing_until:       # limit-switch / StallGuard style homing (not memory mode)
            if now >= self.homing_until:
                self.homing_until = 0.0
                self.phys_fs = 0.0
                self.anchor(0)
                self.vel = 0.0
            else:
                self.vel = -self.cfg[7]
                self.steps += self.vel * dt
                self.phys_fs = max(0.0, self.phys_fs + self._dir_sign() * self.vel * dt / self.ms)
            return
        if self.streaming and now - self.last_frame > 0.3:
            self.streaming = False
            self.link_lost = True
            self.target = self.steps
            self.ff = 0.0
            if self.vel_mode:                   # coast to a stop, then hold
                self.vel_mode = False
                self.jog_target = self.steps + math.copysign(self.vel * self.vel / (2 * amax), self.vel)
        goal = self.target if (self.streaming or self.jog_target is None) else self.jog_target
        if not self.enabled:
            self.vel = 0.0
        elif self.streaming and self.vel_mode:      # continuous rotation, like the firmware's runForward
            dv = max(-amax * dt, min(amax * dt, max(-vmax, min(vmax, self.target_speed)) - self.vel))
            self.vel += dv
            self.steps += self.vel * dt
            self.target = self.steps
        else:
            err = goal - self.steps
            if self.streaming or self.jog_target is None:
                want = self.ff + err * 8.0
            else:   # point-to-point move: brake in time, like FastAccelStepper's ramp
                want = math.copysign(min(math.sqrt(2 * amax * abs(err)) * 0.95, abs(err) / max(dt, 1e-3)), err)
            want = max(-vmax, min(vmax, want))
            dv = max(-amax * dt, min(amax * dt, want - self.vel))
            self.vel += dv
            self.steps += self.vel * dt
            if self.jog_target is not None and abs(self.jog_target - self.steps) < 1:
                self.steps = self.jog_target
                self.target = self.jog_target      # hold here afterwards
                self.jog_target = None
                self.vel = 0.0
        if self.stall_fs:
            vfs = abs(self.vel) / self.ms
            afs = abs(self.vel - v_before) / max(dt, 1e-6) / self.ms
            if vfs > self.stall_fs or afs > self.stall_acc_fs * 1.02:
                self.stalled = True
            elif self.stalled and vfs < 0.3 * self.stall_fs:
                self.stalled = False
        if not self.stalled:
            self.phys_fs += self._dir_sign() * (self.steps - before) / self.ms
        # position record: dirty while moving, clean after 2 s at rest outside the show
        moving = abs(self.vel) > 1
        if self.homed and self.cal:
            if moving:
                self.rest_since = 0.0
                if not self.rec or self.rec["clean"]:
                    self.write_rec(self.best_pos(), False)
            else:
                self.rest_since = self.rest_since or now
                if self.rec and not self.rec["clean"] and not self.streaming and now - self.rest_since > 2.0:
                    self.write_rec(self.best_pos(), True)

    def status(self, now: float) -> bytes:
        f = P.S_FLAGS
        flags = f["power_good"] | f["tmc_ok"] | f["configured"]
        if self.enabled: flags |= f["enabled"]
        if self.homed: flags |= f["homed"]
        if self.homing_until: flags |= f["homing"]
        if abs(self.vel) > 1 or self.jog_target is not None: flags |= f["moving"]
        if self.link_lost: flags |= f["link_lost"]
        if self.home_failed: flags |= f["home_failed"]
        if self.streaming: flags |= f["streaming"]
        if self.id == P.ID_UNASSIGNED: flags |= f["unassigned"]
        I = P.I_FLAGS
        info = 0
        if self.cal: info |= I["calibrated"]
        if self.limits_on(): info |= I["limits"]
        if self.rec and self.rec["clean"]: info |= I["rec_clean"]
        if self.restored: info |= I["restored"]
        if self.restored_host: info |= I["restored_host"]
        if self.pos_lost and not self.homed: info |= I["pos_lost"]
        st = P.Status(self.id, 2, flags, int(self.steps), int(self.target), self.encoder_steps(),
                      20100, 180, self.last_seq, int(now - self.boot_t),
                      P.M_READ_OK | P.M_DETECTED, info,
                      int(self.lim_lo()) if self.limits_on() else 0, int(self.lim_hi()) if self.limits_on() else 0)
        return P.pack_status(st)

    # ---- persistence (the simulator remembers its nodes between runs, like real flash)
    def to_json(self, pf_works: bool) -> dict:
        rec = self.rec
        if pf_works and self.homed and self.cal:     # quitting the simulator = power cut with a good save
            rec = {"pos": self.best_pos(), "ms": self.ms, "clean": True}
        return {"mac": self.mac, "id": self.id, "cfg": self.cfg, "cal": self.cal, "rec": rec, "lim": self.lim,
                "hw": {"wiring_rev": self.wiring_rev, "magnet_rev": self.magnet_rev, "mount": self.mount,
                       "phys_fs": self.phys_fs}}

    @classmethod
    def from_json(cls, d: dict) -> "_SimNode":
        hw = d["hw"]
        nd = cls.__new__(cls)
        nd.mac, nd.id, nd.cfg = d["mac"], d["id"], list(d["cfg"])
        nd.wiring_rev, nd.magnet_rev, nd.mount = hw["wiring_rev"], hw["magnet_rev"], hw["mount"]
        nd.phys_fs = hw["phys_fs"]
        nd.cal, nd.rec, nd.lim = d.get("cal"), d.get("rec"), d.get("lim")
        nd.nv_dirty = False
        nd.boot()
        return nd


class SimTransport(Transport):
    """Pretends to be a bridge plus N PD Steppers.

    state_path: if given, the simulated nodes' flash and physical positions are kept in this
    file, so stopping and restarting the simulator behaves like a power cycle.
    """

    def __init__(self, n_motors: int, node_cfg: list[int], unassigned: int = 0,
                 state_path=None, travel_steps: int = 0):
        super().__init__()
        self.state_path = state_path
        self.pf_works = True
        self.fresh = True
        self.nodes: list[_SimNode] = []
        if state_path and os.path.exists(state_path):
            try:
                with open(state_path) as f:
                    data = json.load(f)
                self.nodes = [_SimNode.from_json(d) for d in data["nodes"]]
                self.pf_works = data.get("pf_works", True)
                self.fresh = False
            except (OSError, ValueError, KeyError):
                self.nodes = []
        if not self.nodes:
            for i in range(n_motors + unassigned):
                mac = f"5E:00:00:00:00:{i:02X}"   # locally-administered fake MACs
                if i < n_motors:
                    # already set up: taught top at the physical top, limits = travel
                    nd = _SimNode(mac, i, node_cfg, mount=(i * 997.3) % CNT)
                    nd.cfg[6] = 3
                    nd.cal = {"raw": nd.raw(), "ms": nd.ms, "pos": 0}
                    nd.rec = {"pos": 0, "ms": nd.ms, "clean": True}
                    if travel_steps:
                        nd.lim = {"on": True, "lo": 0, "hi": int(travel_steps), "ms": nd.ms}
                    nd.boot()
                else:
                    # a brand-new board straight out of the box, built "wrong" on purpose so the
                    # setup wizard has something to fix: motor wired backwards, magnet flipped
                    nd = _SimNode(mac, P.ID_UNASSIGNED, node_cfg, wiring_rev=True, magnet_rev=True,
                                  mount=1234.0)
                    nd.phys_fs = 300.0
                    nd.boot()
                self.nodes.append(nd)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.connected = True
        self.port_name = "simulator"
        self.tx = 0

    def start(self):
        threading.Thread(target=self._run, name="sim", daemon=True).start()

    def stop(self):
        self._stop.set()
        self.save_state()

    def save_state(self):
        if not self.state_path:
            return
        with self._lock:
            data = {"pf_works": self.pf_works, "nodes": [nd.to_json(self.pf_works) for nd in self.nodes]}
        tmp = str(self.state_path) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.state_path)

    # ---- simulator-only test hooks
    def truth(self, mac: str) -> float | None:
        """Where the load really is (microsteps below the physical top), for the wizard's hint."""
        for nd in self.nodes:
            if nd.mac == mac:
                return nd.phys_fs * nd.ms
        return None

    def power_cut(self, save_works: bool = True):
        with self._lock:
            for nd in self.nodes:
                if save_works:
                    nd.power_fail_save()
                nd.boot()

    def reboot_node(self, mac: str):
        with self._lock:
            for nd in self.nodes:
                if nd.mac == mac:
                    nd.boot()

    def _run(self):
        last = time.monotonic()
        next_status = last
        next_bridge = last
        next_save = last + 1.0
        while not self._stop.is_set():
            time.sleep(0.005)
            now = time.monotonic()
            dt, last = now - last, now
            with self._lock:
                for nd in self.nodes:
                    nd.step(dt, now)
                statuses = [(nd.mac, nd.status(now)) for nd in self.nodes] if now >= next_status else []
            if statuses:
                next_status = now + 0.2
                for mac, pkt in statuses:
                    self.on_packet(mac, -48, pkt)
            if now >= next_bridge:
                next_bridge = now + 1.0
                self.on_bridge(P.BridgeInfo("5E:00:00:00:FF:01", 1, 2, self.tx, 0, 0))
            if self.state_path and now >= next_save:
                next_save = now + 1.0
                try:
                    self.save_state()
                except OSError:
                    pass

    def send(self, payload: bytes):
        if len(payload) < 4 or payload[:2] != P.MAGIC:
            return
        self.tx += 1
        now = time.monotonic()
        with self._lock:
            if payload[3] == P.T_FRAME:
                seq, flags, targets = P.unpack_frame(payload)
                for nd in self.nodes:
                    if nd.id >= len(targets) or nd.homing_until:
                        continue
                    if targets[nd.id][0] == P.POS_VELOCITY:
                        # velocity mode: no position needed, but never on a motor with limits
                        if not (flags & P.FF_ENABLE) or nd.limits_on():
                            if nd.streaming:
                                nd.streaming = False
                                nd.vel_mode = False
                                nd.target = nd.steps
                            continue
                        nd.enabled = nd.streaming = nd.vel_mode = True
                        nd.link_lost = False
                        nd.jog_target = None
                        nd.target_speed = float(targets[nd.id][1])
                        nd.last_frame = now
                        nd.last_seq = seq
                        continue
                    nd.vel_mode = False
                    if not (flags & P.FF_ENABLE) or not nd.homed:
                        if nd.streaming:
                            nd.streaming = False
                            nd.target = nd.steps
                            nd.ff = 0.0
                        continue
                    nd.enabled = True
                    nd.streaming = True
                    nd.link_lost = False
                    nd.jog_target = None
                    nd.target = nd.clamp(float(targets[nd.id][0]))
                    nd.ff = float(targets[nd.id][1])
                    nd.last_frame = now
                    nd.last_seq = seq
            elif payload[3] == P.T_CMD:
                target, mac, cmd, a = P.unpack_cmd(payload)
                mac_s = P.mac_str(mac) if any(mac) else None
                for nd in self.nodes:
                    if mac_s:
                        if nd.mac != mac_s:
                            continue
                    elif target != P.TARGET_ALL and target != nd.id:
                        continue
                    self._cmd(nd, cmd, a, now)

    def _cmd(self, nd: _SimNode, cmd: int, a: list[int], now: float):
        if cmd == P.C_ENABLE:
            nd.enabled = bool(a[0])
            if not nd.enabled:
                nd.streaming = False
        elif cmd == P.C_HOME:
            nd.home_failed = False
            if nd.memory_mode():
                if not nd.homed:
                    nd.home_failed = True
                    return
                nd.enabled = True
                nd.jog_target = nd.lim_lo() if nd.limits_on() else 0.0
                return
            if nd.cfg[6] == 0:
                nd.home_failed = True
                return
            nd.enabled = True
            nd.homed = False
            nd.streaming = False
            speed = max(1, nd.cfg[7])
            nd.homing_until = now + min(8.0, abs(nd.phys_fs * nd.ms) / speed + 0.8)
        elif cmd == P.C_SET_ZERO:
            if abs(nd.vel) > 1:
                return
            nd.anchor(a[0])
            nd.cal = {"raw": nd.raw(), "ms": nd.ms, "pos": int(a[0])}
            nd.write_rec(a[0], True)
            nd.restored = nd.restored_host = nd.pos_lost = False
        elif cmd == P.C_JOG and not nd.streaming:
            nd.enabled = True
            nd.jog_speed = float(max(1, a[1]))
            if nd.limits_on() and nd.homed and a[2] != 1:
                base = nd.jog_target if nd.jog_target is not None else nd.steps
                nd.jog_target = nd.clamp(base + a[0])
            else:
                nd.jog_target = (nd.jog_target if nd.jog_target is not None else nd.steps) + a[0]
        elif cmd == P.C_STOP:
            nd.jog_target = None
            nd.target = nd.steps
            nd.homing_until = 0.0
        elif cmd == P.C_SET_ID:
            if a[0] == P.ID_UNASSIGNED or 0 <= a[0] < P.MAX_MOTORS:
                nd.id = a[0]
                nd.identify_until = now + 1.5
        elif cmd == P.C_CONFIG:
            if abs(nd.vel) > 1:
                return
            old_ms, old_flags = nd.ms, nd.cfg[5]
            nd.cfg = list(a)
            nd.cfg[2] = max(1, min(256, nd.cfg[2]))
            if nd.ms != old_ms:
                nd.steps = nd.steps * nd.ms / old_ms
                nd.target = nd.steps
            if (old_flags ^ nd.cfg[5]) & 3:
                nd.cal = None
                nd.rec = None
                nd.homed = nd.restored = nd.restored_host = nd.pos_lost = False
            nd.nv_dirty = True
        elif cmd == P.C_SET_LIMITS:
            nd.lim = {"on": bool(a[0]), "lo": min(a[1], a[2]), "hi": max(a[1], a[2]), "ms": nd.ms}
            nd.nv_dirty = True
        elif cmd == P.C_RESTORE:
            if not nd.homed and nd.cal and abs(nd.vel) < 1:
                p = nd.phase_position(a[0])
                nd.anchor(p)
                nd.write_rec(p, True)
                nd.restored_host, nd.pos_lost = True, False
        elif cmd == P.C_IDENTIFY:
            nd.identify_until = now + 3
