"""Installation config: mechanics, motor layout and startup behaviour (config.json)."""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path


def write_atomic(path: Path, text: str):
    """Write a file so a power cut leaves either the old or the new version, never half of one."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass

HOME_MODES = {"manual": 0, "limit": 1, "stallguard": 2, "memory": 3}

DEFAULT_CONFIG = {
    "serial_port": "auto",          # "auto" finds /dev/ttyACM* or /dev/ttyUSB*
    "simulate": False,              # True = no hardware, simulated motors
    "frame_rate": 50,               # position updates per second
    "autoplay": True,               # start the playlist on power-up
    "auto_home": True,              # home every motor before autoplay (limit/stallguard modes)
    "assume_home_at_boot": False,   # manual homing only: treat power-up spot as zero
    "active_show": "default",
    "playback": {"speed": 1.0, "depth": 1.0},   # operator adjustments from the touch panel
    "mechanics": {
        "spool_diameter_mm": 40.0,  # effective diameter where the cable wraps
        "full_steps_per_rev": 200,
        "microsteps": 16,
        "travel_mm": 1000.0,        # max cable paid out below home
        "max_speed_mm_s": 150.0,
        "max_accel_mm_s2": 300.0,
        "run_current_pct": 60,
        "hold_current_pct": 30,
        "invert_dir": False,
        "invert_encoder": False,
        "spreadcycle": False,       # False = silent stealthChop (needed for StallGuard)
        # memory: no switches; each motor remembers its position (encoder + saved turn count),
        #         top and bottom are taught once per motor in Motor Setup.
        # limit | stallguard: home against a switch / hard stop at every power-up.
        # manual: set zero by hand.
        "home_mode": "memory",
        "home_speed_mm_s": 20.0,
        "home_timeout_s": 60.0,
        "home_backoff_mm": 10.0,
        "stall_threshold": 60,
        "pd_volts": 20,
    },
    "layout": "line",
    "motors": [],   # filled by default_motors()
}


def default_motors(n: int, layout: str = "line") -> list[dict]:
    motors = []
    cols = max(1, math.ceil(math.sqrt(n)))
    rows = max(1, math.ceil(n / cols))
    for i in range(n):
        if layout == "circle":
            a = 2 * math.pi * i / n
            x, y = 0.5 + 0.5 * math.cos(a), 0.5 + 0.5 * math.sin(a)
        elif layout == "grid":
            c, r = i % cols, i // cols
            x = c / (cols - 1) if cols > 1 else 0.5
            y = r / (rows - 1) if rows > 1 else 0.5
        else:
            x, y = (i / (n - 1) if n > 1 else 0.5), 0.5
        motors.append({"id": i, "name": f"M{i + 1}", "x": round(x, 4), "y": round(y, 4),
                       "trim_mm": 0.0, "travel_mm": None})
    # Per-motor setup results (written by the Motor Setup wizard):
    #   invert_dir / invert_encoder: None = use the Mechanics setting
    #   commissioned: top and bottom have been taught
    #   bottom_fs: taught bottom in FULL motor steps below the top (independent of microstepping
    #              and of the spool diameter you enter, so re-measuring the spool never moves it)
    #   node_mac: the PD Stepper that was set up for this position
    return motors


SETUP_KEYS = ("invert_dir", "invert_encoder", "commissioned", "bottom_fs", "node_mac", "commissioned_at",
              "name", "trim_mm", "travel_mm")


def relayout_motors(old: list[dict], n: int, layout: str) -> list[dict]:
    """New motor count/layout positions, keeping each surviving motor's setup (taught limits,
    direction, name...). Only the layout x/y comes from the preset."""
    by_id = {m["id"]: m for m in old}
    out = []
    for m in default_motors(n, layout):
        prev = by_id.get(m["id"])
        if prev:
            m.update({k: prev[k] for k in SETUP_KEYS if k in prev})
        out.append(m)
    return out


def is_preset_layout(motors: list[dict], layout: str) -> bool:
    """True when every motor still sits where the layout preset put it (not hand-placed)."""
    ref = default_motors(len(motors), layout)
    return all(abs(m.get("x", 0) - r["x"]) < 1e-3 and abs(m.get("y", 0) - r["y"]) < 1e-3
               for m, r in zip(motors, ref))


class Config:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = copy.deepcopy(DEFAULT_CONFIG)
        if self.path.exists():
            try:
                self.update(json.loads(self.path.read_text()))
            except ValueError:
                self.path.replace(self.path.with_suffix(".corrupt"))   # keep defaults, keep evidence
        if not self.data["motors"]:
            self.data["motors"] = default_motors(1, self.data["layout"])   # add more with "Add Motor"

    # ---------- persistence ----------
    def update(self, new: dict):
        mech = {**self.data["mechanics"], **new.get("mechanics", {})}
        self.data.update({k: v for k, v in new.items() if k != "mechanics"})
        self.data["mechanics"] = mech
        self.data["motors"] = sorted(self.data["motors"], key=lambda m: m["id"])[:16]

    def save(self):
        write_atomic(self.path, json.dumps(self.data, indent=2))

    # ---------- derived values ----------
    @property
    def mech(self) -> dict:
        return self.data["mechanics"]

    @property
    def motors(self) -> list[dict]:
        return self.data["motors"]

    @property
    def motor_count(self) -> int:
        return len(self.motors)

    @property
    def steps_per_mm(self) -> float:
        m = self.mech
        return m["full_steps_per_rev"] * m["microsteps"] / (math.pi * m["spool_diameter_mm"])

    def travel(self, i: int) -> float:
        m = self.motors[i]
        if m.get("commissioned") and m.get("bottom_fs"):
            return float(m["bottom_fs"]) * math.pi * self.mech["spool_diameter_mm"] / self.mech["full_steps_per_rev"]
        t = m.get("travel_mm")
        return float(t) if t else float(self.mech["travel_mm"])

    def bottom_usteps(self, i: int) -> int:
        """Taught bottom limit in microsteps (what the node enforces)."""
        return int(round(float(self.motors[i].get("bottom_fs") or 0) * self.mech["microsteps"]))

    def motor_flag(self, i: int, key: str) -> bool:
        v = self.motors[i].get(key) if 0 <= i < self.motor_count else None
        return bool(self.mech[key] if v is None else v)

    def trim(self, i: int) -> float:
        return float(self.motors[i].get("trim_mm") or 0.0)

    def node_config_args(self, i: int | None = None) -> list[int]:
        """Arguments for the KS_C_CONFIG command (see ks_protocol.h), for motor i
        (per-motor direction settings) or the Mechanics defaults when i is None."""
        m, spm = self.mech, self.steps_per_mm
        inv_dir = self.motor_flag(i, "invert_dir") if i is not None else m["invert_dir"]
        inv_enc = self.motor_flag(i, "invert_encoder") if i is not None else m["invert_encoder"]
        flags = (1 if inv_dir else 0) | (2 if inv_enc else 0) | (4 if m["spreadcycle"] else 0)
        return [
            int(m["run_current_pct"]),
            int(m["hold_current_pct"]),
            int(m["microsteps"]),
            int(m["max_speed_mm_s"] * spm * 1.5),    # node cap above host limit
            int(m["max_accel_mm_s2"] * spm * 2.0),   # node tracks with headroom
            flags,
            HOME_MODES.get(m["home_mode"], 1),
            int(m["home_speed_mm_s"] * spm),
            int(m["home_timeout_s"] * 1000),
            int(m["home_backoff_mm"] * spm),
            int(m["stall_threshold"]),
            int(m["pd_volts"]),
        ]
