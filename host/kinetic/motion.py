"""Motion design math: pattern generators, keyframe timelines, crossfades and the
speed/acceleration limiter.

Conventions
  * A design value is 0..1 for each motor: 0 = home (top of a spool, home end of a slide,
    start angle of an arm), 1 = the far end of that motor's taught travel. For a
    continuous-rotation motor the value is a speed (see config.speed_from_norm).
  * Motors have layout coordinates x, y in 0..1 (config.json), which spatial
    patterns (wave, ripple, spin...) use.
  * Everything here is pure functions/classes so it can be unit-tested and
    rendered for the editor without touching hardware.
"""
from __future__ import annotations

import math
import random

# ------------------------------------------------------------------ helpers

def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


def smootherstep(u: float) -> float:
    u = clamp(u)
    return u * u * u * (u * (u * 6 - 15) + 10)


def periodic(u: float, dwell: float = 0.0) -> float:
    """Smooth periodic wave in [-1, 1] with optional dwell (pause) at top and bottom.

    dwell = 0 gives a pure -cos wave (starts at -1 = up).  dwell = 0.5 spends half of
    every cycle holding still at the extremes, which reads as "breathing" in kinetic art.
    """
    d = clamp(dwell, 0.0, 0.9)
    r = (1.0 - d) / 2.0          # length of each moving half
    f = u - math.floor(u)
    if f < r:
        return -math.cos(math.pi * f / r)
    f -= r
    if f < d / 2:
        return 1.0
    f -= d / 2
    if f < r:
        return math.cos(math.pi * f / r)
    return -1.0


def _normalize(vals: list[float]) -> list[float]:
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return [0.5 for _ in vals]
    return [(v - lo) / (hi - lo) for v in vals]


# ------------------------------------------------------------------ patterns
# Each pattern: fn(t, motors, params) -> list of drops (0..1).
# Parameter metadata below drives the editor sliders.

COMMON_PARAMS = [
    {"key": "center", "label": "Center height", "min": 0, "max": 1, "step": 0.01, "default": 0.5,
     "help": "Middle of the motion (0 = home, 1 = far end; continuous rotation: speed)"},
    {"key": "amplitude", "label": "Amplitude", "min": 0, "max": 0.5, "step": 0.01, "default": 0.3,
     "help": "How far each motor swings either side of the center"},
    {"key": "period", "label": "Period (s)", "min": 1, "max": 120, "step": 0.5, "default": 12,
     "help": "Seconds for one full cycle"},
    {"key": "dwell", "label": "Dwell", "min": 0, "max": 0.8, "step": 0.01, "default": 0.0,
     "help": "Fraction of each cycle spent pausing at the top and bottom"},
]

PATTERNS: dict[str, dict] = {
    "breathe": {"label": "Breathe (all together)", "params": []},
    "wave": {"label": "Traveling wave", "params": [
        {"key": "wavelengths", "label": "Waves across", "min": 0, "max": 4, "step": 0.05, "default": 1,
         "help": "How many wave peaks fit across the sculpture"},
        {"key": "direction", "label": "Direction (deg)", "min": 0, "max": 360, "step": 1, "default": 0,
         "help": "0 = left to right, 180 = right to left, 90 = front to back"},
    ]},
    "ripple": {"label": "Ripple from a point", "params": [
        {"key": "wavelengths", "label": "Rings", "min": 0, "max": 4, "step": 0.05, "default": 1},
        {"key": "origin_x", "label": "Origin X", "min": 0, "max": 1, "step": 0.01, "default": 0.5},
        {"key": "origin_y", "label": "Origin Y", "min": 0, "max": 1, "step": 0.01, "default": 0.5},
        {"key": "inward", "label": "Inward", "type": "bool", "default": False},
    ]},
    "spin": {"label": "Spin (around a center)", "params": [
        {"key": "turns", "label": "Arms", "min": 1, "max": 4, "step": 1, "default": 1},
        {"key": "origin_x", "label": "Center X", "min": 0, "max": 1, "step": 0.01, "default": 0.5},
        {"key": "origin_y", "label": "Center Y", "min": 0, "max": 1, "step": 0.01, "default": 0.5},
        {"key": "reverse", "label": "Reverse", "type": "bool", "default": False},
    ]},
    "chase": {"label": "Chase (a pulse travels across)", "params": [
        {"key": "width", "label": "Pulse width", "min": 0.05, "max": 1, "step": 0.01, "default": 0.25},
        {"key": "direction", "label": "Direction (deg)", "min": 0, "max": 360, "step": 1, "default": 0},
        {"key": "bounce", "label": "Bounce back", "type": "bool", "default": False},
    ]},
    "alternate": {"label": "Alternate (odd vs even)", "params": [
        {"key": "groups", "label": "Groups", "min": 2, "max": 4, "step": 1, "default": 2},
    ]},
    "drift": {"label": "Organic drift (smooth random)", "params": [
        {"key": "coherence", "label": "Neighbor coherence", "min": 0, "max": 1, "step": 0.01, "default": 0.6,
         "help": "1 = neighbors move alike, 0 = every motor independent"},
        {"key": "seed", "label": "Seed", "min": 0, "max": 999, "step": 1, "default": 7},
    ]},
    "pose": {"label": "Static pose", "params": []},   # uses params["values"]
}


def _param(p: dict, key: str, default):
    v = p.get(key, default)
    return default if v is None else v


def eval_pattern(name: str, t: float, motors: list[dict], p: dict) -> list[float]:
    n = len(motors)
    center = float(_param(p, "center", 0.5))
    amp = float(_param(p, "amplitude", 0.3))
    period = max(0.1, float(_param(p, "period", 12)))
    dwell = float(_param(p, "dwell", 0.0))
    cyc = t / period
    xs = [m["x"] for m in motors]
    ys = [m["y"] for m in motors]

    if name == "pose":
        vals = list(p.get("values", []))
        vals += [center] * (n - len(vals))
        return [clamp(float(v)) for v in vals[:n]]

    if name == "breathe":
        v = center + amp * periodic(cyc, dwell)
        return [clamp(v)] * n

    if name == "wave":
        th = math.radians(float(_param(p, "direction", 0)))
        s = _normalize([x * math.cos(th) + y * math.sin(th) for x, y in zip(xs, ys)])
        k = float(_param(p, "wavelengths", 1))
        return [clamp(center + amp * periodic(cyc - k * si, dwell)) for si in s]

    if name == "ripple":
        ox, oy = float(_param(p, "origin_x", 0.5)), float(_param(p, "origin_y", 0.5))
        d = [math.hypot(x - ox, y - oy) for x, y in zip(xs, ys)]
        dmax = max(d) or 1.0
        k = float(_param(p, "wavelengths", 1)) * (-1 if p.get("inward") else 1)
        return [clamp(center + amp * periodic(cyc - k * di / dmax, dwell)) for di in d]

    if name == "spin":
        ox, oy = float(_param(p, "origin_x", 0.5)), float(_param(p, "origin_y", 0.5))
        turns = int(_param(p, "turns", 1))
        sgn = -1 if p.get("reverse") else 1
        out = []
        for x, y in zip(xs, ys):
            a = math.atan2(y - oy, x - ox) / (2 * math.pi)
            out.append(clamp(center + amp * periodic(cyc - sgn * turns * a, dwell)))
        return out

    if name == "chase":
        th = math.radians(float(_param(p, "direction", 0)))
        s = _normalize([x * math.cos(th) + y * math.sin(th) for x, y in zip(xs, ys)])
        w = max(0.02, float(_param(p, "width", 0.25)))
        f = cyc - math.floor(cyc)
        if p.get("bounce"):
            f = 1 - abs(2 * f - 1)
        c = -w + f * (1 + 2 * w)        # pulse enters and fully leaves the sculpture
        out = []
        for si in s:
            bump = math.exp(-((si - c) / w) ** 2 * 3.0)
            out.append(clamp(center - amp + 2 * amp * bump))
        return out

    if name == "alternate":
        g = max(2, int(_param(p, "groups", 2)))
        return [clamp(center + amp * periodic(cyc - (i % g) / g, dwell)) for i in range(n)]

    if name == "drift":
        coh = clamp(float(_param(p, "coherence", 0.6)))
        rng = random.Random(int(_param(p, "seed", 7)))
        comps = [(rng.uniform(0.6, 1.6), rng.random(), rng.uniform(-1, 1), rng.uniform(-1, 1))
                 for _ in range(4)]
        indiv = [[(rng.uniform(0.6, 1.6), rng.random()) for _ in range(3)] for _ in range(n)]
        out = []
        for i, (x, y) in enumerate(zip(xs, ys)):
            shared = sum(math.sin(2 * math.pi * (fr * cyc + ph + 0.8 * (x * dx + y * dy)))
                         for fr, ph, dx, dy in comps) / 4
            own = sum(math.sin(2 * math.pi * (fr * cyc + ph)) for fr, ph in indiv[i]) / 3
            v = coh * shared + (1 - coh) * own
            out.append(clamp(center + amp * max(-1.0, min(1.0, v * 1.6))))
        return out

    raise ValueError(f"unknown pattern {name}")


# ------------------------------------------------------------------ keyframes

EASES = ["smooth", "linear", "ease_in", "ease_out", "hold"]


def _ease(kind: str, u: float) -> float:
    u = clamp(u)
    if kind == "linear":
        return u
    if kind == "ease_in":
        return u * u * u
    if kind == "ease_out":
        return 1 - (1 - u) ** 3
    if kind == "hold":
        return 0.0
    return smootherstep(u)


def _key_ease(k) -> str:
    return k[2] if len(k) > 2 else "smooth"


def eval_track(keys: list, t: float, duration: float, wrap: bool) -> float:
    """keys: list of [time_s, drop, ease]. The ease applies to the segment AFTER a key."""
    if not keys:
        return 0.0
    ks = sorted(keys, key=lambda k: k[0])
    if wrap and duration > 0:
        t = t % duration
        # periodic: the segment after the last key wraps around to the first key
        first, last = ks[0], ks[-1]
        ks = ([[last[0] - duration, last[1], _key_ease(last)]] + [list(k) for k in ks]
              + [[first[0] + duration, first[1], _key_ease(first)]])
    else:
        if t <= ks[0][0]:
            return float(ks[0][1])
        if t >= ks[-1][0]:
            return float(ks[-1][1])
    for a, b in zip(ks, ks[1:]):
        if a[0] <= t <= b[0]:
            span = b[0] - a[0]
            u = 0.0 if span <= 0 else (t - a[0]) / span
            e = _ease(_key_ease(a), u)
            return float(a[1]) + (float(b[1]) - float(a[1])) * e
    return float(ks[-1][1])


def eval_timeline(clip: dict, t: float, motors: list[dict]) -> list[float]:
    n = len(motors)
    dur = float(clip.get("duration", 30))
    tracks = clip.get("tracks", {})
    wrap = bool(clip.get("wrap", True))
    mode = clip.get("mode", "shared")
    stagger = float(clip.get("stagger", 0.0))
    by = clip.get("stagger_by", "index")
    if by == "x":
        order = _normalize([m["x"] for m in motors])
    elif by == "center":
        d = [abs(m["x"] - 0.5) + abs(m["y"] - 0.5) for m in motors]
        order = _normalize(d)
    else:
        order = [i / (n - 1) if n > 1 else 0.0 for i in range(n)]
    if clip.get("stagger_reverse"):
        order = [1 - o for o in order]
    out = []
    for i in range(n):
        keys = tracks.get(str(i)) if mode == "per_motor" else None
        if not keys:
            keys = tracks.get("all", [])
        out.append(clamp(eval_track(keys, t - stagger * order[i], dur, wrap)))
    return out


# ------------------------------------------------------------------ clips

def eval_clip(clip: dict, t: float, motors: list[dict]) -> list[float]:
    if clip.get("type") == "timeline":
        return eval_timeline(clip, t, motors)
    return eval_pattern(clip.get("pattern", "breathe"), t, motors, clip.get("params", {}))


def blend(a: list[float], b: list[float], w: float) -> list[float]:
    w = smootherstep(w)
    return [x + (y - x) * w for x, y in zip(a, b)]


# ------------------------------------------------------------------ limiter

class Limiter:
    """Turns any desired position stream into motion the motors can actually do.

    Per motor, it tracks (x, v) and moves toward the desired position with
    feed-forward velocity, never exceeding max speed or max acceleration and always
    leaving room to brake. If the design asks for more than that, the output lags
    and `limited[i]` is set so the editor can warn you.
    """

    def __init__(self, n: int, vmax, amax, tolerance: float = 3.0):
        """vmax / amax: one number for every motor, or a list with one per motor."""
        self.n = n
        self.vmax = vmax
        self.amax = amax
        self.tol = tolerance
        self.x = [0.0] * n
        self.v = [0.0] * n
        self.prev_desired: list[float] | None = None
        self.limited = [False] * n
        self.tol_relative = False

    def reset(self, positions: list[float]):
        self.x = list(positions)
        self.v = [0.0] * self.n
        self.prev_desired = None

    def step(self, desired: list[float], dt: float, lo: list[float], hi: list[float]) -> tuple[list[float], list[float]]:
        if self.prev_desired is None:
            self.prev_desired = list(desired)
        vmaxs = self.vmax if isinstance(self.vmax, (list, tuple)) else [self.vmax] * self.n
        amaxs = self.amax if isinstance(self.amax, (list, tuple)) else [self.amax] * self.n
        for i in range(self.n):
            vmax, amax = vmaxs[i], amaxs[i]
            d = min(max(desired[i], lo[i]), hi[i])
            ff = (d - min(max(self.prev_desired[i], lo[i]), hi[i])) / dt
            err = d - self.x[i]
            # close the error as fast as possible while always leaving room to brake
            ve = math.copysign(min(math.sqrt(1.8 * amax * abs(err)), abs(err) / dt), err)
            vt = max(-vmax, min(vmax, ff + ve))
            dv = max(-amax * dt, min(amax * dt, vt - self.v[i]))
            self.v[i] += dv
            self.x[i] += self.v[i] * dt
            self.x[i] = min(max(self.x[i], lo[i]), hi[i])
            if self.x[i] in (lo[i], hi[i]) and ((self.x[i] == lo[i] and self.v[i] < 0) or (self.x[i] == hi[i] and self.v[i] > 0)):
                self.v[i] = 0.0
            # "limited" = more than ~1% of the range (min tolerance) behind the design
            self.limited[i] = abs(d - self.x[i]) > max(self.tol * 0.01 * max(hi[i] - lo[i], 1e-9), 1e-6) \
                if self.tol_relative else abs(d - self.x[i]) > self.tol
        self.prev_desired = list(desired)
        return list(self.x), list(self.v)


def render_clip(clip: dict, motors: list[dict], travel: list[float], vmax, amax,
                samples_per_s: float = 10.0, max_samples: int = 1200) -> dict:
    """Sample a clip for the editor: desired drops plus where the limiter would kick in."""
    dur = max(0.5, float(clip.get("duration", 30)))
    n_samples = int(min(max_samples, max(20, dur * samples_per_s)))
    dt = dur / n_samples
    sim_dt = 1 / 50
    lim = Limiter(len(motors), vmax, amax, tolerance=0.5)
    lim.tol_relative = True
    first = eval_clip(clip, 0.0, motors)
    lim.reset([f * tr for f, tr in zip(first, travel)])
    zeros = [0.0] * len(motors)
    times, desired, limited_any = [], [], []
    t = 0.0
    next_sample = 0.0
    lim_flags = [False] * len(motors)
    while t <= dur + 1e-9:
        vals = eval_clip(clip, t, motors)
        lim.step([v * tr for v, tr in zip(vals, travel)], sim_dt, zeros, travel)
        lim_flags = [a or b for a, b in zip(lim_flags, lim.limited)]
        if t >= next_sample - 1e-9:
            times.append(round(t, 3))
            desired.append([round(v, 4) for v in vals])
            limited_any.append(any(lim_flags))
            lim_flags = [False] * len(motors)
            next_sample += dt
        t += sim_dt
    return {"t": times, "values": desired, "limited": limited_any}
