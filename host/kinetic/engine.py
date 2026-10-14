"""Playback engine: turns the show (clips + playlist) into a synchronized 50 Hz stream
of motor positions, and manages the motors (discovery, config, homing, safety).

Flow every tick:
    playlist/solo/hold source -> drops (0..1) -> crossfade -> millimetres -> Limiter
    -> microsteps -> one broadcast FRAME for all motors.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from . import motion as M
from . import protocol as P
from .config import KINDS, Config, default_motors, is_preset_layout, relayout_motors, write_atomic

log = logging.getLogger("kinetic.engine")


def new_id() -> str:
    return uuid.uuid4().hex[:8]


DEFAULT_SHOW = {
    "name": "default",
    "playlist": [],
    "clips": [],
}


def starter_show() -> dict:
    """A small show so the sculpture does something on first boot."""
    c1 = {"id": new_id(), "name": "Slow breath", "type": "pattern", "pattern": "breathe", "duration": 40,
          "fade": 5, "params": {"center": 0.4, "amplitude": 0.25, "period": 16, "dwell": 0.2}}
    c2 = {"id": new_id(), "name": "Rolling wave", "type": "pattern", "pattern": "wave", "duration": 60,
          "fade": 6, "params": {"center": 0.5, "amplitude": 0.3, "period": 14, "dwell": 0.0,
                                "wavelengths": 1.0, "direction": 0}}
    c3 = {"id": new_id(), "name": "Cascade drop", "type": "timeline", "duration": 24, "fade": 5,
          "mode": "shared", "stagger": 6, "stagger_by": "index", "wrap": True,
          "tracks": {"all": [[0, 0.2, "smooth"], [8, 0.7, "smooth"], [12, 0.7, "smooth"], [20, 0.2, "smooth"]]}}
    return {"name": "default", "clips": [c1, c2, c3],
            "playlist": [{"clip": c1["id"], "repeat": 1}, {"clip": c2["id"], "repeat": 1},
                         {"clip": c3["id"], "repeat": 2}]}


class ClipSource:
    def __init__(self, engine: "Engine", clip_id: str, t0: float):
        self.engine, self.clip_id, self.t0 = engine, clip_id, t0

    def eval(self, now: float) -> list[float]:
        clip = self.engine.clip(self.clip_id)
        if clip is None:
            return self.engine.last_norm
        return M.eval_clip(clip, now - self.t0, self.engine.cfg.motors, self.engine.eval_ctx())


class HoldSource:
    def __init__(self, values: list[float]):
        self.values = list(values)

    def eval(self, now: float) -> list[float]:
        return self.values


class Engine:
    def __init__(self, cfg: Config, transport, shows_dir: Path):
        self.cfg = cfg
        self.tx = transport
        self.shows_dir = Path(shows_dir)
        self.shows_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.nodes: dict[str, dict] = {}
        self.bridge: dict | None = None
        self.events: deque = deque(maxlen=60)
        self.show: dict = {}
        self.load_show(cfg.data.get("active_show", "default"))

        n = cfg.motor_count
        self.limiter = self._new_limiter(n)
        self.last_norm = [0.0] * n
        self.out_mm = [0.0] * n
        self.out_v = [0.0] * n
        self.desired_mm = [0.0] * n
        self.armed = False
        self.mode = "idle"            # idle | playlist | solo | hold | park
        self.source = HoldSource([0.0] * n)
        self.fade_from = None         # (source, start, duration)
        self.entry_index = 0
        self.entry_start = 0.0
        self.solo_clip: str | None = None
        self.seq = 0
        self.disarm_frames = 0
        self.startup = "waiting"
        self._started = time.monotonic()
        # Show clock: advances at the operator's speed setting, so slowing the show
        # down or speeding it up never makes positions jump.
        self.vclock = 0.0
        # Position memory on the Pi side: last known good position of every node (by MAC),
        # so a node that reboots on its own (while the Pi keeps running) can be told where it was.
        self.last_known: dict[str, tuple[int, float]] = {}   # mac -> (microsteps, monotonic time)
        self._restore_sent: dict[str, float] = {}
        self._old_fw_warned: set[str] = set()
        # Motor Setup wizard state (one motor at a time), shared by the editor and the panel
        self.wizard: dict | None = None
        self._wiz_task: asyncio.Task | None = None

        transport.on_packet = self._on_packet
        transport.on_bridge = self._on_bridge

    # ================================================================ events
    def event(self, msg: str, level: str = "info"):
        self.events.append({"t": time.time(), "msg": msg, "level": level})
        getattr(log, "warning" if level == "warn" else "info")(msg)

    # ================================================================ nodes
    def _on_packet(self, mac: str, rssi: int, pkt: bytes):
        st = P.parse_status(pkt)
        if not st:
            if len(pkt) >= 4 and pkt[:2] == P.MAGIC and pkt[3] == P.T_STATUS and pkt[2] != P.VERSION \
                    and mac not in self._old_fw_warned:
                self._old_fw_warned.add(mac)
                self.event(f"Node {mac} runs firmware for protocol v{pkt[2]} (this controller needs v{P.VERSION}). "
                           "Re-flash it with the current node.ino.", "warn")
            return
        with self.lock:
            prev = self.nodes.get(mac)
            if prev and st.uptime_s + 2 < prev["status"].uptime_s:
                self.event(f"{self.mname(st.id)} ({mac}) rebooted", "warn")
            if prev and not prev["status"].has("slip") and st.has("slip"):
                self.event(f"{self.mname(st.id)} encoder disagrees with steps (slipping?)", "warn")
            if prev and not prev["status"].has("home_failed") and st.has("home_failed"):
                self.event(f"{self.mname(st.id)} homing failed", "warn")
            if prev is None:
                self.event(f"Found motor node {mac} (id {'unassigned' if st.id == P.ID_UNASSIGNED else st.id})")
            if prev and not prev["status"].has("pos_lost") and st.has("pos_lost") and st.id != P.ID_UNASSIGNED:
                self.event(f"{self.mname(st.id)} does not know its position (it was moving when power was lost)", "warn")
            if prev and not prev["status"].has("restored") and st.has("restored"):
                self.event(f"{self.mname(st.id)} recovered its position from memory")
            now = time.monotonic()
            self.nodes[mac] = {"mac": mac, "rssi": rssi, "status": st, "seen": now}
            if st.has("homed"):
                self.last_known[mac] = (st.enc if st.enc is not None else st.pos, now)
        self._maybe_restore(mac, st)

    # Only this node rebooted (the Pi kept running): give it its last known position. The node
    # snaps that to the exact spot with its encoder, so it only has to be within half a turn.
    RESTORE_MAX_AGE_S = 120.0

    def _maybe_restore(self, mac: str, st: P.Status):
        if st.has("homed") or st.has("homing") or not st.has("calibrated") or st.id == P.ID_UNASSIGNED:
            return
        if 0 <= st.id < self.cfg.motor_count and self.cfg.is_continuous(st.id):
            return
        if self.cfg.mech["home_mode"] != "memory" or (self.wizard and self.wizard.get("mac") == mac):
            return
        known = self.last_known.get(mac)
        now = time.monotonic()
        if not known or now - known[1] > self.RESTORE_MAX_AGE_S or now - self._restore_sent.get(mac, 0) < 2.0:
            return
        self._restore_sent[mac] = now
        self.send_cmd(P.C_RESTORE, mac=mac, args=[int(known[0])])
        self.event(f"{self.mname(st.id)} rebooted; restoring its position from the controller's memory")

    def _on_bridge(self, info: P.BridgeInfo):
        self.bridge = {"mac": info.mac, "channel": info.channel, "fw": info.fw, "tx_ok": info.tx_ok,
                       "tx_fail": info.tx_fail, "rx": info.rx_count, "seen": time.monotonic()}

    @staticmethod
    def mname(i: int) -> str:
        return "unassigned node" if i == P.ID_UNASSIGNED else f"M{i + 1}"

    def online_nodes(self) -> list[dict]:
        now = time.monotonic()
        with self.lock:
            return [n for n in self.nodes.values() if now - n["seen"] < 2.0]

    def node_by_id(self, i: int) -> dict | None:
        best = None
        for n in self.online_nodes():
            if n["status"].id == i and (best is None or n["seen"] > best["seen"]):
                best = n
        return best

    def actual_mm(self) -> list[float | None]:
        """Where each motor reports it is, in its own units (None for continuous rotation,
        where only the commanded speed is shown)."""
        out = []
        for i in range(self.cfg.motor_count):
            n = self.node_by_id(i)
            out.append(n["status"].pos / self.cfg.spu(i) if n and not self.cfg.is_continuous(i) else None)
        return out

    def _new_limiter(self, n: int) -> M.Limiter:
        lim = M.Limiter(n, [self.cfg.vmax(i) for i in range(n)], [self.cfg.amax(i) for i in range(n)], tolerance=0.5)
        lim.tol_relative = True
        return lim

    # ---- node commands
    def send_cmd(self, cmd: int, target: int = P.TARGET_ALL, mac: str | None = None, args=None):
        self.tx.send(P.pack_cmd(cmd, target, P.mac_bytes(mac) if mac else None, args))

    def push_config(self, target: int = P.TARGET_ALL):
        """Send settings to one motor id, or to every configured motor id. Always addressed per
        motor, because direction settings can differ per motor (and a node that sees its
        direction settings change forgets its taught top)."""
        ids = range(self.cfg.motor_count) if target == P.TARGET_ALL else [target]
        for i in ids:
            self.send_cmd(P.C_CONFIG, i, args=self.cfg.node_config_args(i))
        self.event("Sent motor settings to " + ("all motors" if target == P.TARGET_ALL else f"M{target + 1}"))

    def push_limits(self, target: int = P.TARGET_ALL):
        """Re-send the taught limits (the nodes keep their own copy; this keeps them in step
        with the config, e.g. after microstepping changes)."""
        ids = range(self.cfg.motor_count) if target == P.TARGET_ALL else [target]
        for i in ids:
            if not self.cfg.motors[i].get("commissioned"):
                continue
            if self.cfg.is_continuous(i):
                self.send_cmd(P.C_SET_LIMITS, i, args=[0, 0, 0])      # free to spin
            elif self.cfg.motors[i].get("bottom_fs"):
                self.send_cmd(P.C_SET_LIMITS, i, args=[1, 0, self.cfg.bottom_usteps(i)])

    def home(self, target: int = P.TARGET_ALL):
        if self.armed:
            self.arm(False)
        self.send_cmd(P.C_HOME, target)
        self.event("Homing " + ("all motors" if target == P.TARGET_ALL else f"M{target + 1}"))

    def set_zero(self, target: int = P.TARGET_ALL):
        self.send_cmd(P.C_SET_ZERO, target, args=[0])
        self.event("Set current position as home (zero) on " + ("all motors" if target == P.TARGET_ALL else f"M{target + 1}"))

    def jog(self, target: int, dist: float, free: bool = False, speed: float | None = None):
        """Relative move in the motor's own units (mm, degrees, revolutions).
        free=True ignores the soft limits (only while teaching them)."""
        if self.armed:
            self.event("Disarm before jogging", "warn")
            return
        if target == P.TARGET_ALL:
            for i in range(self.cfg.motor_count):
                self.jog(i, dist, free, speed)
            return
        spu = self.cfg.spu(target)
        v = speed if speed else self.cfg.jog_speed(target)
        self.send_cmd(P.C_JOG, target, args=[int(dist * spu), int(v * spu), 1 if free else 0])

    def identify(self, mac: str):
        self.send_cmd(P.C_IDENTIFY, mac=mac)

    def assign(self, mac: str, new_id: int):
        self.send_cmd(P.C_SET_ID, mac=mac, args=[int(new_id)])
        self.event(f"Assigned node {mac} as {self.mname(new_id)}")
        if 0 <= new_id < self.cfg.motor_count and self.cfg.mech["home_mode"] == "memory" \
                and self.cfg.motors[new_id].get("node_mac") not in (None, mac) and not self.wizard:
            self.event(f"Run Motor Setup for {self.mname(new_id)}: this board has not been set up for that position", "warn")

    def enable(self, target: int, on: bool):
        self.send_cmd(P.C_ENABLE, target, args=[1 if on else 0])

    def stop_nodes(self):
        self.arm(False)
        self.send_cmd(P.C_STOP)

    # ================================================================ show management
    def eval_ctx(self) -> dict:
        """What clips need besides time: motor groups and the clip library (for layered clips)."""
        groups = {g["name"]: g.get("motors", []) for g in self.cfg.data.get("groups", []) if g.get("name")}
        return {"groups": groups, "clips": {c["id"]: c for c in self.show.get("clips", [])}}

    def clip(self, clip_id: str) -> dict | None:
        for c in self.show.get("clips", []):
            if c["id"] == clip_id:
                return c
        return None

    def show_path(self, name: str) -> Path:
        safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_ ").strip() or "show"
        return self.shows_dir / f"{safe}.json"

    def list_shows(self) -> list[str]:
        return sorted(p.stem for p in self.shows_dir.glob("*.json"))

    def load_show(self, name: str):
        p = self.show_path(name)
        loaded = None
        if p.exists():
            try:
                loaded = json.loads(p.read_text())
            except (OSError, ValueError) as e:
                bad = p.with_suffix(".corrupt")
                p.replace(bad)
                self.event(f"Show file {p.name} was unreadable ({e}); moved to {bad.name}", "warn")
        if loaded is not None:
            self.show = loaded
        else:
            self.show = starter_show() if name == "default" else copy.deepcopy(DEFAULT_SHOW)
            self.show["name"] = name
            write_atomic(p, json.dumps(self.show, indent=2))
        self.show.setdefault("clips", [])
        self.show.setdefault("playlist", [])
        self.cfg.data["active_show"] = self.show.get("name", name)

    def save_show(self, show: dict, name: str | None = None):
        show = copy.deepcopy(show)
        if name:
            show["name"] = name
        ids = {c["id"] for c in show.get("clips", [])}
        show["playlist"] = [e for e in show.get("playlist", []) if e.get("clip") in ids]
        self.show = show
        write_atomic(self.show_path(show["name"]), json.dumps(show, indent=2))
        if self.solo_clip and self.solo_clip not in ids:
            self.hold()

    # ================================================================ playback control
    def _switch(self, source, fade: float):
        now = self.vclock
        # keep the old motion running underneath a true crossfade; if a fade is already
        # in progress, freeze its current look instead of nesting fades
        current = HoldSource(self.last_norm) if self.fade_from else self.source
        self.fade_from = (current, now, max(0.0, fade)) if fade > 0 else None
        self.source = source

    def _enabled_index(self, start: int) -> int | None:
        """First playlist entry at or after `start` (wrapping) that is switched on."""
        pl = self.show["playlist"]
        for k in range(len(pl)):
            i = (start + k) % len(pl)
            if pl[i].get("enabled", True) and self.clip(pl[i]["clip"]):
                return i
        return None

    def play(self, index: int = 0, force: bool = False):
        """Run the playlist from `index`. force=True plays that entry even if switched off."""
        pl = self.show["playlist"]
        if not pl:
            self.event("Playlist is empty", "warn")
            return
        i = index % len(pl) if force else self._enabled_index(index)
        if i is None:
            self.event("Every playlist entry is switched off", "warn")
            self.hold()
            return
        self.mode = "playlist"
        self.solo_clip = None
        self.entry_index = i
        self._start_entry(first=True)

    def play_entry(self, index: int):
        self.play(index, force=True)

    def _start_entry(self, first: bool = False):
        pl = self.show["playlist"]
        entry = pl[self.entry_index % len(pl)]
        clip = self.clip(entry["clip"])
        now = self.vclock
        self.entry_start = now
        fade = float(clip.get("fade", 3)) if clip else 2.0
        if first:
            fade = max(fade, 3.0)
        self._switch(ClipSource(self, entry["clip"], now), fade)
        if clip:
            self.event(f"Playing \"{clip.get('name', 'clip')}\"")

    def next(self):
        if self.mode == "playlist":
            i = self._enabled_index(self.entry_index + 1)
            if i is not None:
                self.entry_index = i
                self._start_entry()

    def solo(self, clip_id: str):
        clip = self.clip(clip_id)
        if not clip:
            log.warning("solo: unknown clip %s (have %s)", clip_id, [c["id"] for c in self.show["clips"]])
            return
        self.mode = "solo"
        self.solo_clip = clip_id
        self._switch(ClipSource(self, clip_id, self.vclock), max(1.5, float(clip.get("fade", 2))))
        self.event(f"Previewing \"{clip.get('name', 'clip')}\" on loop")

    def hold(self):
        self.mode = "hold"
        self.solo_clip = None
        self._switch(HoldSource(self.last_norm), 0)

    def park(self):
        """Wind everything up to home, at the motors' max speed/accel."""
        self.mode = "park"
        self.solo_clip = None
        self._switch(HoldSource([0.0] * self.cfg.motor_count), 0)
        self.event("Parking: winding all motors up")

    def arm(self, on: bool):
        if on and self.wizard:
            self.event("Finish or cancel Motor Setup before enabling the show", "warn")
            return
        if on and not self.armed:
            act = self.actual_mm()
            start = [a if a is not None else x for a, x in zip(act, self.out_mm)]
            self.limiter.reset(start)
            self.out_mm = list(start)
            self.last_norm = [self._mm_to_norm(i, v) for i, v in enumerate(start)]
            # blend from where the motors really are into whatever is playing
            self.fade_from = (HoldSource(self.last_norm), self.vclock, 3.0)
            self.armed = True
            self.event("ARMED: motors follow the show")
        elif not on and self.armed:
            self.armed = False
            self.disarm_frames = 25
            self.event("Disarmed: motors hold position")

    # ================================================================ tick
    def _mm_to_norm(self, i: int, mm: float) -> float:
        if self.cfg.is_continuous(i):           # a speed: invert speed_from_norm
            vmax = self.cfg.vmax(i) or 1.0
            return M.clamp((mm / vmax + 1) / 2 if self.cfg.bidirectional(i) else mm / vmax)
        tr = self.cfg.travel(i)
        return M.clamp((mm - self.cfg.trim(i)) / tr) if tr > 0 else 0.0

    def _norm_to_mm(self, i: int, v: float) -> float:
        return min(max(v * self.cfg.travel(i) + self.cfg.trim(i), 0.0), self.cfg.travel(i))

    def resize(self):
        """Call after motor count / mechanics change."""
        n = self.cfg.motor_count
        self.limiter = self._new_limiter(n)
        self.limiter.reset([0.0] * n)
        self.last_norm = [0.0] * n
        self.out_mm = [0.0] * n
        self.out_v = [0.0] * n
        self.desired_mm = [0.0] * n
        self.fade_from = None
        if self.mode in ("hold", "park", "idle"):
            self.source = HoldSource([0.0] * n)

    @property
    def playback(self) -> dict:
        pb = self.cfg.data.get("playback") or {}
        return {"speed": M.clamp(float(pb.get("speed", 1.0)), 0.25, 2.0),
                "depth": M.clamp(float(pb.get("depth", 1.0)), 0.1, 1.0)}

    def set_playback(self, speed: float | None = None, depth: float | None = None):
        pb = self.playback
        if speed is not None:
            pb["speed"] = round(M.clamp(float(speed), 0.25, 2.0), 3)
        if depth is not None:
            pb["depth"] = round(M.clamp(float(depth), 0.1, 1.0), 3)
        self.cfg.data["playback"] = pb
        self.cfg.save()
        self.event(f"Playback set to speed {pb['speed'] * 100:.0f}%, depth {pb['depth'] * 100:.0f}%")

    def tick(self, dt: float):
        pb = self.playback
        self.vclock += dt * pb["speed"]
        now = self.vclock
        n = self.cfg.motor_count
        self.limiter.vmax = [self.cfg.vmax(i) for i in range(n)]
        self.limiter.amax = [self.cfg.amax(i) for i in range(n)]

        if self.mode == "playlist" and self.show["playlist"]:
            pl = self.show["playlist"]
            entry = pl[self.entry_index % len(pl)]
            clip = self.clip(entry["clip"])
            length = (float(clip.get("duration", 30)) if clip else 5.0) * max(1, int(entry.get("repeat", 1)))
            if now - self.entry_start >= length:
                i = self._enabled_index(self.entry_index + 1)
                if i is None:
                    self.event("Every playlist entry is switched off; holding", "warn")
                    self.hold()
                else:
                    self.entry_index = i
                    self._start_entry()

        norm = self.source.eval(now)
        if len(norm) != n:
            norm = (list(norm) + [0.0] * n)[:n]
        if self.fade_from:
            src, start, dur = self.fade_from
            w = (now - start) / dur if dur > 0 else 1.0
            if w >= 1.0:
                self.fade_from = None
            else:
                prev = src.eval(now)
                prev = (list(prev) + [0.0] * n)[:n]
                norm = M.blend(prev, norm, w)
        self.last_norm = norm
        depth = pb["depth"]
        cont = [self.cfg.is_continuous(i) for i in range(n)]
        # positions: the depth limit compresses everything toward home.
        # continuous rotation: the design value is a speed, and depth scales the speed.
        self.desired_mm = [self.cfg.speed_from_norm(i, v) * depth if cont[i] else self._norm_to_mm(i, v * depth)
                           for i, v in enumerate(norm)]
        hi = [self.cfg.travel(i) for i in range(n)]
        lo = [-hi[i] if cont[i] else 0.0 for i in range(n)]
        out_x, out_v = self.limiter.step(self.desired_mm, dt, [0.0] * n, hi)
        for i in range(n):
            if cont[i]:                         # ramp the speed at the motor's acceleration limit
                cur = self.out_mm[i] if i < len(self.out_mm) else 0.0
                a = self.cfg.amax(i) * dt
                tgt = min(max(self.desired_mm[i], lo[i]), hi[i])
                out_x[i] = cur + max(-a, min(a, tgt - cur))
                out_v[i] = out_x[i]             # speed in units/s
                self.limiter.x[i], self.limiter.v[i] = 0.0, 0.0
                self.limiter.limited[i] = False
        self.out_mm, self.out_v = out_x, out_v

        if self.armed:
            self._send_frame(P.FF_ENABLE)
        elif self.disarm_frames > 0:
            self.disarm_frames -= 1
            self._send_frame(0)

    def _send_frame(self, flags: int):
        targets = []
        for i, (x, v) in enumerate(zip(self.out_mm, self.out_v)):
            spu = self.cfg.spu(i)
            if self.cfg.is_continuous(i):
                targets.append((P.POS_VELOCITY, v * spu))   # run at a speed
            else:
                targets.append((x * spu, v * spu))
        self.seq = (self.seq + 1) & 0xFFFF
        self.tx.send(P.pack_frame(self.seq, flags, targets))

    async def run(self):
        period = 1.0 / float(self.cfg.data.get("frame_rate", 50))
        nxt = time.monotonic()
        last = nxt
        while True:
            now = time.monotonic()
            try:
                self.tick(min(0.1, now - last) or period)
            except Exception:  # noqa: BLE001 - never let a bad clip kill the loop
                log.exception("tick failed")
                self.hold()
            last = now
            nxt += period
            delay = nxt - time.monotonic()
            if delay < -0.5:
                nxt = time.monotonic()
            await asyncio.sleep(max(0.0, delay))

    # ================================================================ startup
    async def autostart(self):
        cfg = self.cfg
        self.startup = "waiting for bridge"
        while not self.tx.connected:
            await asyncio.sleep(0.5)
        self.event(f"Bridge connected ({self.tx.port_name})")
        self.startup = "waiting for motors"
        # wait for the motors that have been set up; empty positions (no board yet) don't hold things up
        want = {i for i, m in enumerate(cfg.motors) if m.get("commissioned")} or set(range(cfg.motor_count))
        t0 = time.monotonic()
        while time.monotonic() - t0 < 15:
            have = {n["status"].id for n in self.online_nodes()}
            if want <= have:
                break
            await asyncio.sleep(0.5)
        have = {n["status"].id for n in self.online_nodes()}
        missing = sorted(want - have)
        if missing:
            self.event(f"Motors not found: {', '.join(str(m) for m in missing)}", "warn")
        self.push_config()
        await asyncio.sleep(0.8)

        mode = cfg.mech["home_mode"]
        if mode == "memory":
            # Nothing to drive into: each motor recovered its position at power-up.
            self.startup = "checking positions"
            self.push_limits()
            await asyncio.sleep(1.0)
            for i in sorted(have):
                if i == P.ID_UNASSIGNED:
                    continue
                n = self.node_by_id(i)
                if not n:
                    continue
                st = n["status"]
                setup_mac = cfg.motors[i].get("node_mac")
                if cfg.motors[i].get("commissioned") and setup_mac and setup_mac != n["mac"]:
                    self.event(f"M{i + 1} is now driven by a different board ({n['mac']}) than the one that was set up "
                               f"({setup_mac}). Run Motor Setup for M{i + 1}.", "warn")
                if st.has("homed") or cfg.is_continuous(i):
                    continue
                if not st.has("calibrated"):
                    self.event(f"M{i + 1} has not been set up yet: Setup > Motor Setup", "warn")
                else:
                    self.event(f"M{i + 1} position unknown: run Motor Setup > Find top", "warn")
        elif mode != "manual" and cfg.data.get("auto_home", True):
            self.startup = "homing"
            self.home()
            await asyncio.sleep(1.0)
            deadline = time.monotonic() + float(cfg.mech["home_timeout_s"]) + 10
            while time.monotonic() < deadline:
                sts = [self.node_by_id(i) for i in have]
                if all(s and (s["status"].has("homed") or s["status"].has("home_failed")) for s in sts):
                    break
                await asyncio.sleep(0.5)
        elif mode == "manual" and cfg.data.get("assume_home_at_boot"):
            self.set_zero()
            await asyncio.sleep(0.5)

        # ready = knows its position, or spins continuously (speed mode needs no position)
        homed = [i for i in range(cfg.motor_count) if (s := self.node_by_id(i))
                 and (s["status"].has("homed") or (cfg.is_continuous(i) and cfg.motors[i].get("commissioned")))]
        if len(homed) < len(want):
            self.event(f"Only {len(homed)}/{len(want)} set-up motors know their position; the rest will not move", "warn")
        if cfg.data.get("autoplay", True) and homed:
            self.startup = "running"
            self.arm(True)
            self.play()
        else:
            self.startup = "ready (autoplay off)" if homed else "not homed"


    # ================================================================ adding / removing motors
    #
    # Motor numbers are positions in the radio frame (M1 = slot 0 ...), so removing a motor from
    # the middle renumbers the ones after it: their boards are told their new number over the
    # radio, and their setup data and per-motor show data move with them.

    def add_motor(self) -> int:
        n = self.cfg.motor_count
        if n >= P.MAX_MOTORS:
            raise ValueError(f"The radio frame holds at most {P.MAX_MOTORS} motors")
        self.arm(False)
        layout = self.cfg.data.get("layout", "line")
        if is_preset_layout(self.cfg.motors, layout):
            motors = relayout_motors(self.cfg.motors, n + 1, layout)
        else:                                   # hand-placed: keep them, put the new one in the middle
            motors = self.cfg.motors + [default_motors(1, layout)[0] | {"id": n, "name": f"M{n + 1}"}]
        self.cfg.data["motors"] = motors
        self.cfg.save()
        self.resize()
        self._shift_show(insert_at=n)
        self.event(f"Added motor M{n + 1}")
        return n

    def remove_motors(self, ids: list[int]):
        ids = sorted({int(i) for i in ids if 0 <= int(i) < self.cfg.motor_count}, reverse=True)
        if not ids:
            return
        if len(ids) >= self.cfg.motor_count:
            raise ValueError("Keep at least one motor")
        if self.wizard and self.wizard["motor"] in ids:
            self.wiz_cancel()
        self.arm(False)
        layout = self.cfg.data.get("layout", "line")
        preset = is_preset_layout(self.cfg.motors, layout)
        online = self.online_nodes()
        names = [m["name"] for m in self.cfg.motors]
        for i in ids:                           # highest first, so lower numbers stay valid
            for nd in online:
                sid = nd["status"].id
                if sid == i:
                    self.send_cmd(P.C_SET_ID, mac=nd["mac"], args=[P.ID_UNASSIGNED])
            self.cfg.motors.pop(i)
            self._shift_show(remove_at=i)
        # groups: drop removed motors, shift the numbers after them
        removed_set = set(ids)
        for g in self.cfg.data.get("groups", []):
            g["motors"] = sorted({j - sum(1 for r in removed_set if r < j) for j in g.get("motors", []) if j not in removed_set})
        # renumber the rest, and tell each remaining board its (possibly new) number
        old_ids = [m["id"] for m in self.cfg.motors]
        for new_id, m in enumerate(self.cfg.motors):
            old = m["id"]
            m["id"] = new_id
            if re.fullmatch(r"M\d+", m.get("name", "")):
                m["name"] = f"M{new_id + 1}"
            if old != new_id:
                for nd in online:
                    if nd["status"].id == old:
                        self.send_cmd(P.C_SET_ID, mac=nd["mac"], args=[new_id])
        if preset:
            self.cfg.data["motors"] = relayout_motors(self.cfg.motors, len(self.cfg.motors), layout)
        self.cfg.save()
        self.resize()
        removed = ", ".join(names[i] for i in sorted(ids))
        moved = sum(1 for o, m in zip(old_ids, self.cfg.motors) if o != m["id"])
        self.event(f"Removed {removed}" + (f"; {moved} motor(s) after it were renumbered" if moved else ""))

    def set_motor_count(self, n: int, force: bool = False):
        """Grow by adding empty slots at the end, or shrink by removing from the end.
        Removing a motor that is set up or has a board reporting needs force=True."""
        n = max(1, min(P.MAX_MOTORS, int(n)))
        cur = self.cfg.motor_count
        if n > cur:
            for _ in range(n - cur):
                self.add_motor()
        elif n < cur:
            drop = list(range(n, cur))
            seen = {nd["status"].id for nd in self.online_nodes()}
            busy = [self.cfg.motors[i]["name"] for i in drop if self.cfg.motors[i].get("commissioned") or i in seen]
            if busy and not force:
                raise ValueError(f"{', '.join(busy)} is set up or has a board; confirm to remove")
            self.remove_motors(drop)

    def remove_unused(self):
        """Remove every motor that hasn't been through Motor Setup and has no board reporting."""
        seen = {nd["status"].id for nd in self.online_nodes()}
        unused = [i for i, m in enumerate(self.cfg.motors) if not m.get("commissioned") and i not in seen]
        if len(unused) >= self.cfg.motor_count:
            unused = unused[1:]                 # always keep one
        self.remove_motors(unused)
        return len(unused)

    def _shift_show(self, remove_at: int | None = None, insert_at: int | None = None):
        """Keep per-motor show data (static poses, per-motor timeline curves) attached to the
        right motor when motors are removed or added."""
        changed = False
        for c in self.show.get("clips", []):
            vals = (c.get("params") or {}).get("values")
            if isinstance(vals, list):
                if remove_at is not None and remove_at < len(vals):
                    vals.pop(remove_at)
                    changed = True
                elif insert_at is not None and insert_at <= len(vals):
                    vals.insert(insert_at, vals[-1] if vals else 0.0)
                    changed = True
            tracks = c.get("tracks")
            if c.get("mode") == "per_motor" and isinstance(tracks, dict):
                new = {}
                for k, v in tracks.items():
                    if not k.isdigit():
                        new[k] = v
                        continue
                    j = int(k)
                    if remove_at is not None:
                        if j == remove_at:
                            continue
                        j = j - 1 if j > remove_at else j
                    new[str(j)] = v
                if insert_at is not None and str(insert_at) not in new and tracks:
                    new[str(insert_at)] = copy.deepcopy(tracks.get(str(insert_at - 1)) or next(iter(tracks.values())))
                c["tracks"] = new
                changed = True
        if changed:
            self.save_show(self.show)

    # ================================================================ Motor Setup wizard
    #
    # Steps (the UI walks through them; each step is a command below):
    #   start -> direction -> encoder -> top -> bottom -> test -> finish
    # "recover" mode is the short version for a motor that lost its position:
    #   start(recover) -> top -> finish   (keeps the taught bottom)
    #
    # The whole sculpture is disarmed while the wizard runs, so show frames never fight the jogs.

    def _wiz_node(self) -> dict | None:
        w = self.wizard
        if not w:
            return None
        with self.lock:
            n = self.nodes.get(w["mac"])
        return n if n and time.monotonic() - n["seen"] < 2.0 else None

    def _wiz_set(self, **kw):
        if self.wizard is not None:
            self.wizard.update(kw)

    # ---- mechanism-aware helpers (distances are in the motor's own units)
    TEST_STEP = {"spool": 10.0, "linear": 5.0, "rotary": 15.0, "continuous": 0.25}
    JOG_STEPS = {"spool": [50, 10, 1], "linear": [20, 5, 0.5], "rotary": [45, 10, 1], "continuous": [1, 0.25, 0.05]}
    SIZE_PRESETS = {"spool": [20, 25, 30, 40, 50, 60, 80, 100], "linear": [1, 2, 4, 5, 8, 10, 12, 20, 40],
                    "rotary": [1, 2, 3, 4, 5, 10, 14, 19, 27, 50, 100], "continuous": [1, 2, 3, 4, 5, 10, 14, 19, 27, 50, 100]}

    def _wiz_steps(self, w: dict) -> list[str]:
        if w["recover"]:
            return ["top", "finish"]
        if self.cfg.is_continuous(w["motor"]):
            return ["mechanism", "direction", "encoder", "test", "finish"]
        return ["mechanism", "direction", "encoder", "top", "bottom", "test", "finish"]

    def _wiz_next(self, after: str) -> str:
        steps = self._wiz_steps(self.wizard)
        return steps[min(steps.index(after) + 1, len(steps) - 1)] if after in steps else steps[-1]

    def wiz_start(self, mac: str, motor: int, recover: bool = False):
        added = False
        if motor == self.cfg.motor_count:       # "New motor" chosen in the wizard
            motor = self.add_motor()
            added = True
            recover = False
        if not 0 <= motor < self.cfg.motor_count:
            raise ValueError("motor out of range")
        with self.lock:
            n = self.nodes.get(mac)
        if not n:
            raise ValueError("node not found")
        self.arm(False)
        self.send_cmd(P.C_STOP)
        for other in self.online_nodes():          # one PD Stepper per motor position
            if other["mac"] != mac and other["status"].id == motor:
                self.send_cmd(P.C_SET_ID, mac=other["mac"], args=[P.ID_UNASSIGNED])
                self.event(f"Node {other['mac']} released from M{motor + 1}")
        if n["status"].id != motor:
            self.assign(mac, motor)
        m = self.cfg.motors[motor]
        recover = bool(recover and m.get("commissioned") and m.get("bottom_fs") and not self.cfg.is_continuous(motor))
        self.wizard = {"mac": mac, "motor": motor, "recover": recover,
                       "step": "top" if recover else "mechanism", "busy": False, "msg": "", "error": "",
                       "dir_tested": False, "encoder_ok": False, "top_set": False, "bottom_mm": None,
                       "test_done": False, "was_commissioned": bool(m.get("commissioned")), "added": added}
        self._wiz_later(self._wiz_prepare())
        self.event(("Motor Setup (find home)" if recover else "Motor Setup") + f" started for M{motor + 1}")

    async def _wiz_prepare(self):
        w = self.wizard
        i = w["motor"]
        await self._wait_for(lambda: (n := self._wiz_node()) and n["status"].id == i, 5.0)
        self.push_config(i)
        await asyncio.sleep(0.3)
        if not w["recover"]:
            self.send_cmd(P.C_SET_LIMITS, i, args=[0, 0, 0])   # free to move while teaching
        self.enable(i, True)

    def _wiz_later(self, coro):
        if self._wiz_task and not self._wiz_task.done():
            self._wiz_task.cancel()

        async def runner():
            self._wiz_set(busy=True, error="")
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - report in the wizard instead of crashing
                log.exception("wizard step failed")
                self._wiz_set(error=str(e))
            finally:
                self._wiz_set(busy=False)
        try:
            self._wiz_task = asyncio.get_running_loop().create_task(runner())
        except RuntimeError:          # no loop (unit tests calling directly)
            asyncio.run(runner())

    async def _wait_for(self, cond, timeout: float) -> bool:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if cond():
                return True
            await asyncio.sleep(0.1)
        return False

    async def _wiz_move_and_wait(self, dist: float, free: bool, speed: float | None = None, timeout: float = 30):
        i = self.wizard["motor"]
        self.jog(i, dist, free=free, speed=speed)
        await asyncio.sleep(0.5)
        ok = await self._wait_for(lambda: (n := self._wiz_node()) and not n["status"].has("moving"), timeout)
        await asyncio.sleep(0.3)
        return ok

    def wiz_mechanism(self, kind: str, size: float | None = None, bidirectional: bool | None = None):
        """Step 1: what this motor drives. Changing the type discards limits taught for the old one."""
        w = self.wizard
        if not w or w["busy"] or kind not in KINDS:
            return
        i = w["motor"]
        m = self.cfg.motors[i]
        if self.cfg.kind(i) != kind:
            m.pop("bottom_fs", None)
            m["travel_mm"] = None
            m.pop("max_speed", None)
            m.pop("max_accel", None)
        m["kind"] = kind
        if size:
            m["size"] = float(size)
        elif kind != "spool":
            m["size"] = KINDS[kind]["size_default"]
        else:
            m.pop("size", None)
        if bidirectional is not None:
            m["bidirectional"] = bool(bidirectional)
        self.cfg.save()
        self.push_config(i)
        self.enable(i, True)
        self._wiz_set(step=self._wiz_next("mechanism"), msg=f"{KINDS[kind]['label']} selected.", error="")
        self.event(f"M{i + 1}: mechanism set to {KINDS[kind]['label']}")

    def wiz_jog(self, dist: float):
        w = self.wizard
        if not w or w["busy"]:
            return
        # While teaching, the limits are off; in recover mode only the home is re-taught, so
        # the motor may also need to go past the old (unknown) zero.
        self.jog(w["motor"], dist, free=True)

    def wiz_direction_test(self):
        w = self.wizard
        if not w:
            return
        i = w["motor"]
        d, unit, info = self.TEST_STEP[self.cfg.kind(i)], self.cfg.unit(i), self.cfg.kind_info(i)

        async def go():
            self._wiz_set(msg=f"Moving {d:g} {unit}. Watch which way it goes.")
            await self._wiz_move_and_wait(d, free=True)
            self._wiz_set(dir_tested=True, msg=f"Did it {info['positive']}?")
        self._wiz_later(go())

    def wiz_direction_answer(self, correct: bool):
        w = self.wizard
        if not w or not w["dir_tested"]:
            return
        i = w["motor"]
        d, unit = self.TEST_STEP[self.cfg.kind(i)], self.cfg.unit(i)

        async def go():
            if correct:
                self._wiz_set(msg=f"Direction is correct. Returning {d:g} {unit}.")
                await self._wiz_move_and_wait(-d, free=True)
            else:
                m = self.cfg.motors[i]
                # flipping the motor also flips how the encoder relates to steps
                m["invert_dir"] = not self.cfg.motor_flag(i, "invert_dir")
                m["invert_encoder"] = not self.cfg.motor_flag(i, "invert_encoder")
                self.cfg.save()
                self.push_config(i)
                await asyncio.sleep(0.5)
                self.enable(i, True)
                self.event(f"M{i + 1}: direction reversed")
                self._wiz_set(msg=f"Direction reversed. Returning {d:g} {unit}.")
                await self._wiz_move_and_wait(d, free=True)    # it went the other way: come back in the new sense
            self._wiz_set(step="encoder", msg="")
        self._wiz_later(go())

    def wiz_encoder_check(self):
        w = self.wizard
        if not w:
            return
        i = w["motor"]
        spu, unit = self.cfg.spu(i), self.cfg.unit(i)

        async def go():
            n = self._wiz_node()
            if not n:
                raise RuntimeError("Motor is not reporting. Check power and the radio link.")
            mag = n["status"].magnet_state
            if mag in ("none", "unknown"):
                raise RuntimeError("No encoder magnet detected. Check the magnet on the back of the motor shaft.")
            dist = 2 * self.TEST_STEP[self.cfg.kind(i)]
            for attempt in range(2):
                self._wiz_set(msg=f"Checking the encoder: moving {dist:g} {unit} and back.")
                st0 = self._wiz_node()["status"]
                await self._wiz_move_and_wait(dist, free=True)
                st1 = self._wiz_node()["status"]
                dpos, denc = st1.pos - st0.pos, (st1.enc or 0) - (st0.enc or 0)
                await self._wiz_move_and_wait(-dist, free=True)
                if abs(dpos) < dist * spu * 0.5:
                    raise RuntimeError("The motor did not move. Check the motor wiring and supply voltage.")
                ratio = denc / dpos
                if abs(ratio - 1) < 0.15:
                    note = " (magnet is weak: move it closer)" if mag == "weak" else \
                           " (magnet is very strong: move it a little further)" if mag == "strong" else ""
                    self._wiz_set(encoder_ok=True, step=self._wiz_next("encoder"), msg="Encoder OK" + note)
                    return
                if abs(ratio + 1) < 0.15 and attempt == 0:
                    m = self.cfg.motors[i]
                    m["invert_encoder"] = not self.cfg.motor_flag(i, "invert_encoder")
                    self.cfg.save()
                    self.push_config(i)
                    await asyncio.sleep(0.5)
                    self.enable(i, True)
                    self.event(f"M{i + 1}: encoder direction reversed")
                    continue
                if abs(ratio) < 0.15:
                    raise RuntimeError("The encoder did not see the shaft turn. The magnet may be loose or missing.")
                raise RuntimeError(f"Encoder and motor disagree (ratio {ratio:.2f}). "
                                   "The motor may be slipping, or the magnet is off-center.")
            raise RuntimeError("Encoder check failed twice.")
        self._wiz_later(go())

    def wiz_set_top(self):
        """Teach the home end: top of a spool, home end of a slide, start angle of an arm."""
        w = self.wizard
        if not w:
            return
        i = w["motor"]
        home = self.cfg.kind_info(i)["home"]

        async def go():
            self.send_cmd(P.C_SET_ZERO, i, args=[0])
            ok = await self._wait_for(lambda: (n := self._wiz_node()) and n["status"].has("homed")
                                      and n["status"].has("calibrated") and abs(n["status"].pos) < 2, 3.0)
            if not ok:
                raise RuntimeError(f"The motor did not confirm the {home} (is the encoder magnet detected?).")
            if w["recover"]:
                self.push_limits(i)
                self._wiz_set(top_set=True, step="finish", msg=f"{home.capitalize()} set. Limits restored.")
            else:
                self._wiz_set(top_set=True, step="bottom", msg=f"{home.capitalize()} set. This is 0 {self.cfg.unit(i)}.")
            self.event(f"M{i + 1}: {home} taught")
        self._wiz_later(go())

    def wiz_set_bottom(self):
        """Teach the far end: bottom of a spool, far end of a slide, end angle of an arm."""
        w = self.wizard
        n = self._wiz_node()
        if not w or not n:
            return
        i = w["motor"]
        st = n["status"]
        ms = self.cfg.mech["microsteps"]
        unit, far, home = self.cfg.unit(i), self.cfg.kind_info(i)["far"], self.cfg.kind_info(i)["home"]
        pos = st.enc if st.enc is not None else st.pos
        dist = pos / self.cfg.spu(i)
        need = 2 * self.TEST_STEP[self.cfg.kind(i)]
        if dist < need:
            self._wiz_set(error=f"The {far} must be at least {need:g} {unit} past the {home}.")
            return
        w["bottom_fs"] = pos / ms
        w["bottom_mm"] = round(dist, 1)
        self.send_cmd(P.C_SET_LIMITS, i, args=[1, 0, int(pos)])
        self._wiz_set(step="test", error="", msg=f"{far.capitalize()} set at {dist:.0f} {unit}. Limits are now active.")
        self.event(f"M{i + 1}: {far} taught at {dist:.0f} {unit}")

    def wiz_test(self):
        w = self.wizard
        if not w:
            return
        i = w["motor"]
        unit = self.cfg.unit(i)
        if self.cfg.is_continuous(i):
            spd = self.cfg.vmax(i) * 0.25

            async def spin():
                self._wiz_set(msg=f"Spin test: forward at {spd:.2f} {unit} for 3 s, then back.")
                await self._wiz_move_and_wait(spd * 3, free=True, speed=spd, timeout=20)
                await self._wiz_move_and_wait(-spd * 3, free=True, speed=spd, timeout=20)
                self._wiz_set(test_done=True, step="finish", msg="Spin test complete.")
            self._wiz_later(spin())
            return
        if not w.get("bottom_fs"):
            return
        bottom = w["bottom_mm"]
        info = self.cfg.kind_info(i)
        spd = max(self.cfg.vmax(i) / 6, min(self.cfg.vmax(i) / 3, self.cfg.vmax(i)))

        async def go():
            for label, goal in (("middle", bottom / 2), (info["far"], bottom), (info["home"], 0.0)):
                n = self._wiz_node()
                if not n:
                    raise RuntimeError("Motor stopped reporting during the test.")
                here = n["status"].pos / self.cfg.spu(i)
                self._wiz_set(msg=f"Test move: going to the {label} ({goal:.0f} {unit})")
                await self._wiz_move_and_wait(goal - here, free=False, speed=spd,
                                              timeout=abs(goal - here) / spd + 15)
            self._wiz_set(test_done=True, step="finish", msg="Test complete. Check it stopped at the right places.")
        self._wiz_later(go())

    def wiz_goto(self, step: str):
        """Go back to an earlier step (e.g. re-teach the far end after the test)."""
        w = self.wizard
        if not w or w["busy"]:
            return
        steps = self._wiz_steps(w)
        if step not in steps:
            return
        if step in ("mechanism", "direction", "encoder", "top", "bottom") and not w["recover"]:
            self.send_cmd(P.C_SET_LIMITS, w["motor"], args=[0, 0, 0])   # free again while teaching
            w["test_done"] = False
        if step in ("mechanism", "direction"):
            w["dir_tested"] = False
        self._wiz_set(step=step, msg="", error="")

    def wiz_finish(self):
        w = self.wizard
        if not w:
            return
        i = w["motor"]
        m = self.cfg.motors[i]
        cont = self.cfg.is_continuous(i)
        if not w["recover"] and not cont:
            if not (w["top_set"] and w.get("bottom_fs")):
                self._wiz_set(error="Teach both ends first.")
                return
            m["bottom_fs"] = round(w["bottom_fs"], 2)
            m["travel_mm"] = None
        if cont:
            # continuous rotation has no limits; position memory isn't needed to run
            self.send_cmd(P.C_SET_LIMITS, i, args=[0, 0, 0])
            self.send_cmd(P.C_SET_ZERO, i, args=[0])
            m.pop("bottom_fs", None)
        # pin this motor's directions so later edits to the Mechanics defaults can't flip it
        # (a node that sees its direction change forgets its taught top)
        m["invert_dir"] = self.cfg.motor_flag(i, "invert_dir")
        m["invert_encoder"] = self.cfg.motor_flag(i, "invert_encoder")
        m["commissioned"] = True
        m["node_mac"] = w["mac"]
        m["commissioned_at"] = time.strftime("%Y-%m-%d %H:%M")
        self.cfg.save()
        self.push_limits(i)
        unit = self.cfg.unit(i)
        self.event(f"M{i + 1} set up as {self.cfg.kind_info(i)['label'].lower()}" +
                   ("" if w["recover"] or cont else f": travel {self.cfg.travel(i):.0f} {unit}"))
        self.wizard = None

    def wiz_cancel(self):
        w = self.wizard
        if not w:
            return
        if self._wiz_task and not self._wiz_task.done():
            self._wiz_task.cancel()
        i = w["motor"]
        self.send_cmd(P.C_STOP, i)
        if self.cfg.motors[i].get("commissioned"):
            self.push_limits(i)                    # put the previous limits back
        self.event(f"Motor Setup cancelled for M{i + 1}")
        self.wizard = None
        if w.get("added") and i == self.cfg.motor_count - 1 and self.cfg.motor_count > 1 \
                and not self.cfg.motors[i].get("commissioned"):
            self.remove_motors([i])                # the new slot was never set up: take it away again

    def wizard_snapshot(self) -> dict | None:
        w = self.wizard
        if not w:
            return None
        i = w["motor"]
        out = {k: v for k, v in w.items() if k != "bottom_fs"}
        kind = self.cfg.kind(i)
        info = KINDS[kind]
        spu = self.cfg.spu(i)
        out.update({"steps": self._wiz_steps(w), "kind": kind, "unit": info["unit"], "home": info["home"],
                    "far": info["far"], "positive": info["positive"], "jog_steps": self.JOG_STEPS[kind],
                    "size": self.cfg.size(i), "bidirectional": self.cfg.bidirectional(i),
                    "test_step": self.TEST_STEP[kind]})
        n = self._wiz_node()
        if n:
            st = n["status"]
            out["node"] = {"online": True, "pos_mm": round(st.pos / spu, 1),
                           "enc_mm": None if st.enc is None else round(st.enc / spu, 1),
                           "moving": st.has("moving"), "homed": st.has("homed"),
                           "calibrated": st.has("calibrated"), "limits": st.has("limits"),
                           "magnet": st.magnet_state, "id": st.id}
        else:
            out["node"] = {"online": False}
        truth = getattr(self.tx, "truth", None)
        if truth:
            t = truth(w["mac"])
            if t is not None:
                out["sim_drop_mm"] = round(t / spu, 1)
        return out

    # ================================================================ UI snapshot
    def snapshot(self) -> dict:
        now = time.monotonic()
        vnow = self.vclock
        nodes = []
        with self.lock:
            items = list(self.nodes.values())
        for n in sorted(items, key=lambda n: (n["status"].id, n["mac"])):
            st = n["status"]
            # each board reports in microsteps; show it in its motor's units
            spm = self.cfg.spu(st.id) if 0 <= st.id < self.cfg.motor_count else self.cfg.steps_per_mm
            enc_mm = None if st.enc is None else st.enc / spm
            nodes.append({
                "mac": n["mac"], "id": None if st.id == P.ID_UNASSIGNED else st.id,
                "online": now - n["seen"] < 2.0, "rssi": n["rssi"], "flags": st.flag_names(),
                "pos_mm": round(st.pos / spm, 1), "target_mm": round(st.target / spm, 1),
                "enc_mm": None if enc_mm is None else round(enc_mm, 1),
                "err_mm": None if enc_mm is None else round(enc_mm - st.pos / spm, 1),
                "vbus": round(st.vbus_mv / 1000, 2), "sg": st.sg, "uptime": st.uptime_s, "fw": st.fw,
                "magnet": st.magnet_state,
                "lim_mm": [round(st.lim_min / spm, 1), round(st.lim_max / spm, 1)] if st.has("limits") else None,
            })
        clip_id, clip_t = None, 0.0
        if isinstance(self.source, ClipSource):
            clip_id, clip_t = self.source.clip_id, vnow - self.source.t0
        bridge = None
        if self.bridge:
            bridge = {**self.bridge, "online": now - self.bridge["seen"] < 3.0}
        return {
            "type": "tick",
            "mode": self.mode, "armed": self.armed, "startup": self.startup,
            "clip": clip_id, "clip_t": round(clip_t, 2), "entry": self.entry_index,
            "entry_t": round(vnow - self.entry_start, 2) if self.mode == "playlist" else 0,
            "playback": self.playback,
            "norm": [round(v, 4) for v in self.last_norm],
            "out_mm": [round(v, 1) for v in self.out_mm],
            "desired_mm": [round(v, 1) for v in self.desired_mm],
            "limited": list(self.limiter.limited),
            "actual_mm": [None if a is None else round(a, 1) for a in self.actual_mm()],
            "nodes": nodes, "bridge": bridge, "link": self.tx.connected, "port": self.tx.port_name,
            "fading": self.fade_from is not None,
            "travel": [round(self.cfg.travel(i), 1) for i in range(self.cfg.motor_count)],
            "commissioned": [bool(m.get("commissioned")) for m in self.cfg.motors],
            "setup_macs": [m.get("node_mac") for m in self.cfg.motors],
            "kinds": [self.cfg.kind(i) for i in range(self.cfg.motor_count)],
            "units": [self.cfg.unit(i) for i in range(self.cfg.motor_count)],
            "ends": [[self.cfg.kind_info(i)["home"], self.cfg.kind_info(i)["far"]] for i in range(self.cfg.motor_count)],
            "home_mode": self.cfg.mech["home_mode"],
            "wizard": self.wizard_snapshot(),
            "sim": bool(getattr(self.tx, "truth", None)),
        }
