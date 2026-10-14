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


def trapezoid(u: float, ramp: float) -> float:
    """Fastest-stroke profile: position 0 -> 1 over u = 0 -> 1 with constant acceleration for
    the first `ramp` share, constant top speed in the middle, and constant braking at the end.
    For a given speed and acceleration limit this covers a stroke in the least time; the
    optimum ramp is (v/a) / (D/v + v/a) for stroke D (see stroke_fit)."""
    r = clamp(ramp, 0.02, 0.5)
    u = clamp(u)
    vpk = 1.0 / (1.0 - r)               # top speed, in strokes per stroke-time
    if u < r:
        return vpk * u * u / (2 * r)
    if u <= 1 - r:
        return vpk * (u - r / 2)
    return 1.0 - vpk * (1 - u) ** 2 / (2 * r)


def rise(u: float, shape: str = "sine", ramp: float = 0.25) -> float:
    """One stroke from -1 to +1 as u goes 0 -> 1."""
    if shape == "fast":
        return -1.0 + 2.0 * trapezoid(u, ramp)
    return -math.cos(math.pi * clamp(u))


def periodic(u: float, dwell: float = 0.0, shape: str = "sine", ramp: float = 0.25) -> float:
    """Smooth periodic wave in [-1, 1] with optional dwell (pause) at top and bottom.

    dwell = 0 gives a pure -cos wave (starts at -1 = up).  dwell = 0.5 spends half of
    every cycle holding still at the extremes, which reads as "breathing" in kinetic art.
    shape "fast" replaces each half of the sine with the fastest-stroke trapezoid.
    """
    d = clamp(dwell, 0.0, 0.9)
    r = (1.0 - d) / 2.0          # length of each moving half
    f = u - math.floor(u)
    if f < r:
        return rise(f / r, shape, ramp)
    f -= r
    if f < d / 2:
        return 1.0
    f -= d / 2
    if f < r:
        return -rise(f / r, shape, ramp)
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
    {"key": "amplitude", "label": "Stroke", "min": 0, "max": 0.5, "step": 0.005, "default": 0.3,
     "scale": 200, "unit": "%",
     "help": "Size of the movement as a share of each motor's taught length (100% = home to far end)"},
    {"key": "period", "label": "Period (s)", "min": 0.2, "max": 120, "step": 0.1, "default": 12,
     "help": "Seconds for one full cycle"},
    {"key": "dwell", "label": "Dwell", "min": 0, "max": 0.8, "step": 0.01, "default": 0.0,
     "help": "Fraction of each cycle spent pausing at the top and bottom"},
    {"key": "shape", "label": "Stroke shape", "type": "choice", "default": "sine",
     "options": {"sine": "Smooth (sine)", "fast": "Fastest stroke"},
     "only": ["breathe", "wave", "ripple", "spin", "alternate"],
     "help": "Fastest covers the full stroke in the least time the motors allow"},
    {"key": "ramp", "label": "Ramp", "min": 0.05, "max": 0.5, "step": 0.01, "default": 0.25,
     "only": ["breathe", "wave", "ripple", "spin", "alternate"], "show_if": {"shape": "fast"},
     "help": "Share of each stroke spent speeding up and slowing down; the rest is at top speed"},
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
    "pose": {"label": "Static pose", "params": [], "common": []},   # uses params["values"]
    "flap": {"label": "Flap / stroke (fast one way, slow back)", "params": [
        {"key": "stroke_share", "label": "Stroke share", "min": 0.05, "max": 0.95, "step": 0.01, "default": 0.3,
         "help": "Share of each cycle spent on the stroke toward the far end; the rest is the return. 0.5 = even"},
        {"key": "snap", "label": "Snap", "min": 1, "max": 6, "step": 0.1, "default": 2,
         "help": "1 = soft, higher = snappier start and stop of each stroke"},
        {"key": "reverse_stroke", "label": "Fast stroke toward home", "type": "bool", "default": False},
    ]},
    "pulse": {"label": "Pulse / heartbeat", "params": [
        {"key": "pulses", "label": "Pulses per cycle", "min": 1, "max": 4, "step": 1, "default": 2},
        {"key": "width", "label": "Pulse width", "min": 0.02, "max": 0.5, "step": 0.01, "default": 0.12,
         "help": "Length of each pulse as a share of the cycle"},
        {"key": "gap", "label": "Gap between pulses", "min": 0, "max": 0.5, "step": 0.01, "default": 0.06},
    ]},
    "spring": {"label": "Spring (kick and settle)", "params": [
        {"key": "bounces", "label": "Bounces per cycle", "min": 1, "max": 12, "step": 0.5, "default": 4},
        {"key": "damping", "label": "Settle", "min": 0, "max": 10, "step": 0.1, "default": 4,
         "help": "How quickly the bouncing dies away. 0 = keeps bouncing"},
    ]},
    "twitch": {"label": "Random twitches", "params": [
        {"key": "chance", "label": "Twitch chance", "min": 0, "max": 1, "step": 0.01, "default": 0.5,
         "help": "Chance that each motor twitches once in every cycle"},
        {"key": "width", "label": "Twitch length", "min": 0.02, "max": 0.5, "step": 0.01, "default": 0.15,
         "help": "Length of a twitch as a share of the cycle"},
        {"key": "together", "label": "All motors together", "type": "bool", "default": False},
        {"key": "seed", "label": "Seed", "min": 0, "max": 999, "step": 1, "default": 3},
    ]},
    "steps": {"label": "Step sequence", "common": ["period"], "params": [
        {"key": "count", "label": "Steps", "min": 2, "max": 8, "step": 1, "default": 4},
        {"key": "smooth", "label": "Smoothness", "min": 0, "max": 1, "step": 0.01, "default": 0.4,
         "help": "0 = jump between steps, 1 = glide the whole time"},
        {"key": "levels", "label": "Step", "type": "list", "count_key": "count", "default": [0.1, 0.6, 0.3, 0.9, 0.5, 0.2, 0.8, 0.4]},
    ]},
}

# Shaping that works on every pattern and timeline clip (clip["mods"]).
MOD_PARAMS = [
    {"key": "rest", "label": "Rest position", "min": 0, "max": 1, "step": 0.01, "default": None,
     "help": "Where motors outside the target group wait, and where fades start and end. Blank = the clip's center"},
    {"key": "fade_in", "label": "Fade motion in", "min": 0, "max": 120, "step": 0.5, "default": 0, "unit": "s",
     "help": "Movement grows from the rest position over this time at the start of the clip"},
    {"key": "fade_out", "label": "Fade motion out", "min": 0, "max": 120, "step": 0.5, "default": 0, "unit": "s",
     "help": "Movement shrinks back to the rest position over this time at the end of the clip"},
    {"key": "tempo_start", "label": "Tempo at start", "min": 0.25, "max": 4, "step": 0.05, "default": 1, "unit": "×"},
    {"key": "tempo_end", "label": "Tempo at end", "min": 0.25, "max": 4, "step": 0.05, "default": 1, "unit": "×",
     "help": "Speed the motion up or slow it down across the clip"},
    {"key": "stagger", "label": "Stagger", "min": 0, "max": 30, "step": 0.1, "default": 0, "unit": "s",
     "help": "Delay spread across motors (patterns; timelines have their own)"},
    {"key": "amp_var", "label": "Size variation", "min": 0, "max": 1, "step": 0.01, "default": 0,
     "help": "Each motor moves a random amount less than the others"},
    {"key": "time_var", "label": "Timing variation", "min": 0, "max": 5, "step": 0.05, "default": 0, "unit": "s",
     "help": "Each motor runs a random amount early or late"},
    {"key": "seed", "label": "Variation seed", "min": 0, "max": 999, "step": 1, "default": 11},
]
BLENDS = {"over": "Over (replace)", "add": "Add (on top)", "scale": "Scale toward home", "max": "Highest", "min": "Lowest"}


def _param(p: dict, key: str, default):
    v = p.get(key, default)
    return default if v is None else v


def eval_pattern(name: str, t: float, motors: list[dict], p: dict) -> list[float]:
    n = len(motors)
    center = float(_param(p, "center", 0.5))
    amp = float(_param(p, "amplitude", 0.3))
    period = max(0.1, float(_param(p, "period", 12)))
    dwell = float(_param(p, "dwell", 0.0))
    shape = str(_param(p, "shape", "sine"))
    ramp = float(_param(p, "ramp", 0.25))
    cyc = t / period

    def wav(u: float) -> float:
        return periodic(u, dwell, shape, ramp)
    xs = [m["x"] for m in motors]
    ys = [m["y"] for m in motors]

    if name == "pose":
        vals = list(p.get("values", []))
        vals += [center] * (n - len(vals))
        return [clamp(float(v)) for v in vals[:n]]

    if name == "breathe":
        v = center + amp * wav(cyc)
        return [clamp(v)] * n

    if name == "wave":
        th = math.radians(float(_param(p, "direction", 0)))
        s = _normalize([x * math.cos(th) + y * math.sin(th) for x, y in zip(xs, ys)])
        k = float(_param(p, "wavelengths", 1))
        return [clamp(center + amp * wav(cyc - k * si)) for si in s]

    if name == "ripple":
        ox, oy = float(_param(p, "origin_x", 0.5)), float(_param(p, "origin_y", 0.5))
        d = [math.hypot(x - ox, y - oy) for x, y in zip(xs, ys)]
        dmax = max(d) or 1.0
        k = float(_param(p, "wavelengths", 1)) * (-1 if p.get("inward") else 1)
        return [clamp(center + amp * wav(cyc - k * di / dmax)) for di in d]

    if name == "spin":
        ox, oy = float(_param(p, "origin_x", 0.5)), float(_param(p, "origin_y", 0.5))
        turns = int(_param(p, "turns", 1))
        sgn = -1 if p.get("reverse") else 1
        out = []
        for x, y in zip(xs, ys):
            a = math.atan2(y - oy, x - ox) / (2 * math.pi)
            out.append(clamp(center + amp * wav(cyc - sgn * turns * a)))
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
        return [clamp(center + amp * wav(cyc - (i % g) / g)) for i in range(n)]

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

    if name == "flap":
        share = clamp(float(_param(p, "stroke_share", 0.3)), 0.02, 0.98)
        k = max(1.0, float(_param(p, "snap", 2)))
        rev = bool(p.get("reverse_stroke"))
        v = center + amp * stroke_wave(cyc, share, k, dwell)
        if rev:
            v = center - amp * stroke_wave(cyc, share, k, dwell)
        return [clamp(v)] * n

    if name == "pulse":
        f = cyc - math.floor(cyc)
        npulse = max(1, int(_param(p, "pulses", 2)))
        w = max(0.01, float(_param(p, "width", 0.12)))
        gap = max(0.0, float(_param(p, "gap", 0.06)))
        bump = 0.0
        for k in range(npulse):
            st = k * (w + gap)
            if st <= f < st + w:
                bump = math.sin(math.pi * (f - st) / w) ** 2
        return [clamp(center - amp + 2 * amp * bump)] * n

    if name == "spring":
        f = cyc - math.floor(cyc)
        b = max(0.5, float(_param(p, "bounces", 4)))
        damp = max(0.0, float(_param(p, "damping", 4)))
        v = center + amp * math.exp(-damp * f) * math.cos(2 * math.pi * b * f)
        return [clamp(v)] * n

    if name == "twitch":
        chance = clamp(float(_param(p, "chance", 0.5)))
        w = max(0.01, float(_param(p, "width", 0.15)))
        seed = int(_param(p, "seed", 3))
        together = bool(p.get("together"))
        k = math.floor(cyc)
        f = cyc - k
        out = []
        for i in range(n):
            vi = center
            for kk in (k - 1, k):                    # a twitch may run over from the last cycle
                rng = random.Random(hash((seed, kk, 0 if together else i)) & 0xFFFFFFFF)
                if rng.random() >= chance:
                    continue
                start = rng.uniform(0, 1 - min(w, 0.99))
                size = rng.uniform(0.4, 1.0) * (1 if rng.random() < 0.5 else -1)
                u = (f + (k - kk) - start) / w
                if 0 <= u <= 1:
                    vi = center + amp * size * math.sin(math.pi * u) ** 2
            out.append(clamp(vi))
        return out

    if name == "steps":
        count = max(2, min(8, int(_param(p, "count", 4))))
        levels = list(p.get("levels") or PATTERNS["steps"]["params"][2]["default"])
        levels = (levels + [0.5] * 8)[:count]
        sm = clamp(float(_param(p, "smooth", 0.4)))
        f = (cyc - math.floor(cyc)) * count
        j = int(f) % count
        u = f - math.floor(f)
        a_, b_ = float(levels[j]), float(levels[(j + 1) % count])
        if sm <= 0 or u < 1 - sm:
            return [clamp(a_)] * n
        e = smootherstep((u - (1 - sm)) / sm)
        return [clamp(a_ + (b_ - a_) * e)] * n

    raise ValueError(f"unknown pattern {name}")


def stroke_wave(u: float, share: float, snap: float, dwell: float = 0.0) -> float:
    """Asymmetric periodic wave in [-1, 1]: rises from -1 to +1 over `share` of the moving part
    of the cycle and falls back over the rest. `snap` sharpens the start/stop of each stroke."""
    d = clamp(dwell, 0.0, 0.9)
    move = 1.0 - d
    f = u - math.floor(u)
    s1 = move * share
    s2 = move * (1 - share)

    def e(x):
        x = clamp(x)
        a, b = x ** snap, (1 - x) ** snap
        return a / (a + b) if a + b > 0 else x
    if f < s1:
        return -1 + 2 * e(f / s1)
    f -= s1
    if f < d / 2:
        return 1.0
    f -= d / 2
    if f < s2:
        return 1 - 2 * e(f / s2)
    return -1.0


# ------------------------------------------------------------------ keyframes

EASES = ["smooth", "linear", "ease_in", "ease_out", "hold", "bezier"]
DEFAULT_BEZIER = [0.4, 0.0, 0.6, 1.0]


def cubic_bezier(u: float, x1: float, y1: float, x2: float, y2: float) -> float:
    """CSS-style cubic-bezier easing: control points (x1, y1), (x2, y2) between (0,0) and (1,1).
    y may go outside 0..1 for overshoot."""
    x1, x2 = clamp(x1), clamp(x2)

    def bx(s):
        return 3 * x1 * s * (1 - s) ** 2 + 3 * x2 * s * s * (1 - s) + s ** 3

    def by(s):
        return 3 * y1 * s * (1 - s) ** 2 + 3 * y2 * s * s * (1 - s) + s ** 3
    lo, hi = 0.0, 1.0
    s = u
    for _ in range(30):                      # bisection: x(s) is monotonic for x1, x2 in 0..1
        xs = bx(s)
        if abs(xs - u) < 1e-6:
            break
        if xs < u:
            lo = s
        else:
            hi = s
        s = (lo + hi) / 2
    return by(s)


def _ease(kind: str, u: float, bz=None) -> float:
    u = clamp(u)
    if kind == "bezier":
        b = list(bz) if bz and len(bz) == 4 else DEFAULT_BEZIER
        return cubic_bezier(u, *[float(x) for x in b])
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


def _key_opts(k) -> dict:
    """Optional 4th element of a key: {"bz": [x1, y1, x2, y2]} for a custom curve."""
    return k[3] if len(k) > 3 and isinstance(k[3], dict) else {}


def eval_track(keys: list, t: float, duration: float, wrap: bool) -> float:
    """keys: list of [time_s, drop, ease]. The ease applies to the segment AFTER a key."""
    if not keys:
        return 0.0
    ks = sorted(keys, key=lambda k: k[0])
    if wrap and duration > 0:
        t = t % duration
        # periodic: the segment after the last key wraps around to the first key
        first, last = ks[0], ks[-1]
        ks = ([[last[0] - duration, last[1], _key_ease(last), _key_opts(last)]] + [list(k) for k in ks]
              + [[first[0] + duration, first[1], _key_ease(first), _key_opts(first)]])
    else:
        if t <= ks[0][0]:
            return float(ks[0][1])
        if t >= ks[-1][0]:
            return float(ks[-1][1])
    for a, b in zip(ks, ks[1:]):
        if a[0] <= t <= b[0]:
            span = b[0] - a[0]
            u = 0.0 if span <= 0 else (t - a[0]) / span
            e = _ease(_key_ease(a), u, _key_opts(a).get("bz"))
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

def group_members(target, n: int, groups: dict | None) -> set[int] | None:
    """None = every motor. Otherwise the set of motor indexes in the named group."""
    if not target or target == "all":
        return None
    members = (groups or {}).get(target)
    if members is None:
        return None
    return {int(i) for i in members if 0 <= int(i) < n}


def _order(motors: list[dict], by: str, reverse: bool) -> list[float]:
    n = len(motors)
    if by == "x":
        order = _normalize([m["x"] for m in motors])
    elif by == "center":
        order = _normalize([abs(m["x"] - 0.5) + abs(m["y"] - 0.5) for m in motors])
    else:
        order = [i / (n - 1) if n > 1 else 0.0 for i in range(n)]
    return [1 - o for o in order] if reverse else order


def _rest(clip: dict) -> float:
    r = (clip.get("mods") or {}).get("rest")
    if r is not None:
        return clamp(float(r))
    if clip.get("type") == "pattern":
        if clip.get("pattern") == "steps":
            return 0.5
        return clamp(float((clip.get("params") or {}).get("center", 0.5)))
    return 0.0


def _warp(t: float, dur: float, a: float, b: float) -> float:
    """Clip time with a tempo that ramps linearly from a to b across the clip."""
    if abs(a - 1) < 1e-9 and abs(b - 1) < 1e-9 or dur <= 0:
        return t
    tc = min(max(t, 0.0), dur)
    w = tc * (a + (b - a) * tc / (2 * dur))
    return w + (t - tc) * b if t > dur else w


def _eval_raw(clip: dict, t: float, motors: list[dict]) -> list[float]:
    if clip.get("type") == "timeline":
        return eval_timeline(clip, t, motors)
    return eval_pattern(clip.get("pattern", "breathe"), t, motors, clip.get("params", {}))


def eval_clip(clip: dict, t: float, motors: list[dict], ctx: dict | None = None) -> list[float]:
    """Evaluate any clip at time t. ctx = {"groups": {name: [motor indexes]}, "clips": {id: clip}}."""
    ctx = ctx or {}
    if clip.get("type") == "layers":
        return eval_layers(clip, t, motors, ctx)
    n = len(motors)
    mods = clip.get("mods") or {}
    dur = float(clip.get("duration", 30))
    rest = _rest(clip)
    tt = _warp(t, dur, float(mods.get("tempo_start", 1) or 1), float(mods.get("tempo_end", 1) or 1))
    if mods.get("mirror"):                     # symmetric about the center line: x -> distance from center
        motors = [{**m, "x": abs(m["x"] - 0.5) * 2} for m in motors]
    stagger = float(mods.get("stagger", 0) or 0) if clip.get("type") == "pattern" else 0.0
    tvar = float(mods.get("time_var", 0) or 0)
    avar = clamp(float(mods.get("amp_var", 0) or 0))
    seed = int(mods.get("seed", 11) or 0)
    if stagger or tvar:
        order = _order(motors, mods.get("stagger_by", "index"), bool(mods.get("stagger_reverse")))
        rng = random.Random(seed)
        offs = [stagger * order[i] + (rng.uniform(-1, 1) * tvar) for i in range(n)]
        vals = [_eval_raw(clip, tt - offs[i], motors)[i] for i in range(n)]
    else:
        vals = _eval_raw(clip, tt, motors)
    if avar:
        rng = random.Random(seed * 7919 + 1)
        scales = [1 - avar * rng.random() for _ in range(n)]
        vals = [rest + (v - rest) * k for v, k in zip(vals, scales)]
    fi, fo = float(mods.get("fade_in", 0) or 0), float(mods.get("fade_out", 0) or 0)
    env = 1.0
    if fi > 0:
        env = min(env, smootherstep(t / fi))
    if fo > 0:
        env = min(env, smootherstep((dur - t) / fo))
    if env < 1.0:
        vals = [rest + (v - rest) * env for v in vals]
    if mods.get("invert"):
        vals = [1 - v for v in vals]
    members = group_members(clip.get("target"), n, ctx.get("groups"))
    if members is not None:
        vals = [v if i in members else rest for i, v in enumerate(vals)]
    return [clamp(v) for v in vals]


def eval_layers(clip: dict, t: float, motors: list[dict], ctx: dict) -> list[float]:
    """A layered clip mixes other clips: each layer drives its target group with a blend mode."""
    n = len(motors)
    vals = [clamp(float(clip.get("base", 0.0)))] * n
    clips = ctx.get("clips") or {}
    for layer in clip.get("layers", []):
        if layer.get("enabled") is False:
            continue
        sub = clips.get(layer.get("clip"))
        if not sub or sub.get("type") == "layers":   # no nesting (and no loops)
            continue
        lv = eval_clip(sub, t - float(layer.get("offset", 0) or 0), motors, ctx)
        members = group_members(layer.get("target"), n, ctx.get("groups"))
        mix = clamp(float(layer.get("mix", 1.0)))
        mode = layer.get("blend", "over")
        rest = _rest(sub)
        out = []
        for i, (v, x) in enumerate(zip(vals, lv)):
            if members is not None and i not in members:
                out.append(v)
                continue
            if mode == "add":
                r = v + mix * (x - rest)
            elif mode == "scale":
                r = v * (1 - mix + mix * x)
            elif mode == "max":
                r = v + mix * (max(v, x) - v)
            elif mode == "min":
                r = v + mix * (min(v, x) - v)
            else:
                r = v + mix * (x - v)
            out.append(clamp(r))
        vals = out
    return vals


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
            # "limited" = more than ~1% of the range (min tolerance) behind the design, beyond
            # the one-frame lag any follower has at speed
            slack = 1.5 * abs(ff) * dt
            self.limited[i] = abs(d - self.x[i]) > max(self.tol * 0.01 * max(hi[i] - lo[i], 1e-9), 1e-6) + slack \
                if self.tol_relative else abs(d - self.x[i]) > self.tol + slack
        self.prev_desired = list(desired)
        return list(self.x), list(self.v)


def render_clip(clip: dict, motors: list[dict], travel: list[float], vmax, amax,
                samples_per_s: float = 10.0, max_samples: int = 1200, ctx: dict | None = None) -> dict:
    """Sample a clip for the editor: desired drops plus where the limiter would kick in."""
    dur = max(0.5, float(clip.get("duration", 30)))
    n_samples = int(min(max_samples, max(20, dur * samples_per_s)))
    dt = dur / n_samples
    sim_dt = 1 / 50
    lim = Limiter(len(motors), vmax, amax, tolerance=0.5)
    lim.tol_relative = True
    zeros = [0.0] * len(motors)
    # Pre-roll: clips loop, so show how the motors follow once they are moving, not the
    # catch-up from standing still at t = 0 (the show crossfades into every clip anyway).
    kind, base = time_base(clip)
    pre = 0.0
    if kind == "period":
        pre = base * max(1, math.ceil(min(30.0, 2 * base) / base))
    elif kind == "duration" and clip.get("wrap", True) is not False:
        pre = dur
    first = eval_clip(clip, -pre, motors, ctx)
    lim.reset([f * tr for f, tr in zip(first, travel)])
    t = -pre
    while t < -1e-9:
        lim.step([v * tr for v, tr in zip(eval_clip(clip, t, motors, ctx), travel)], sim_dt, zeros, travel)
        t += sim_dt
    times, desired, limited_any = [], [], []
    t = 0.0
    next_sample = 0.0
    lim_flags = [False] * len(motors)
    while t <= dur + 1e-9:
        vals = eval_clip(clip, t, motors, ctx)
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


# ------------------------------------------------------------------ fitting a design to the motors
FIT_MARGIN = 1.10      # keep 10% in hand: with less, a motor that falls behind once (e.g. after a
                       # crossfade) has too little spare speed to catch up and settles into a lag


def time_base(clip: dict) -> tuple[str | None, float]:
    """What sets the speed of a clip: ("period", s) for patterns, ("duration", s) for timelines."""
    if clip.get("type") == "pattern" and clip.get("pattern") != "pose":
        return "period", max(0.1, float((clip.get("params") or {}).get("period", 12)))
    if clip.get("type") == "timeline":
        return "duration", max(0.5, float(clip.get("duration", 30)))
    return None, 0.0


def clip_fit(clip: dict, motors: list[dict], to_units, vmax: list[float], amax: list[float],
             continuous: list[bool], ctx: dict | None = None) -> dict:
    """How much faster (or slower) than the motors allow does this clip run?

    Samples the design finely and measures, per motor, the top speed and acceleration it asks
    for (in the motor's own units: to_units(i, value) -> mm, degrees or rev/s). Because a
    pattern's motion scales with its period, running it `scale` times slower divides speed by
    `scale` and acceleration by scale²; so the smallest workable period is period * scale.
      scale > 1  the design asks for more than the motors can do: strokes would be cut short
      scale < 1  there is headroom; the clip could run 1/scale times faster
    Continuous-rotation motors: the value is a speed, so only how fast it changes counts.
    Instant jumps (hold steps, a 0-smoothness step sequence) are left out: no period fixes them.
    """
    n = len(motors)
    kind, base = time_base(clip)
    dur = max(0.5, float(clip.get("duration", 30)))
    dt = 0.01 if kind != "period" else min(0.01, base / 120)
    win = 4000 * dt
    windows = [(0.0, min(dur, win))]
    if dur > 2 * win:
        windows.append((dur - win, dur))         # tempo changes: the end may be faster
    vreq, areq, jumps = [0.0] * n, [0.0] * n, False
    for t0, t1 in windows:
        prev, prev_v = None, [None] * n
        t = t0
        while t <= t1 + 1e-9:
            vals = eval_clip(clip, t, motors, ctx)
            x = [to_units(i, vals[i]) for i in range(n)]
            if prev is not None:
                for i in range(n):
                    span = abs(to_units(i, 1.0) - to_units(i, 0.0)) or 1.0
                    dx = x[i] - prev[i]
                    if abs(dx) > 0.2 * span:          # an instant jump, not motion
                        jumps = True
                        prev_v[i] = None
                        continue
                    v = dx / dt
                    vreq[i] = max(vreq[i], abs(v))
                    if prev_v[i] is not None:
                        areq[i] = max(areq[i], abs(v - prev_v[i]) / dt)
                    prev_v[i] = v
            prev = x
            t += dt
    per, why = [], []
    for i in range(n):
        if continuous[i]:
            k = vreq[i] / max(amax[i], 1e-9)      # speed change rate vs acceleration
            why.append("acceleration")
        else:
            kv, ka = vreq[i] / max(vmax[i], 1e-9), math.sqrt(areq[i] / max(amax[i], 1e-9))
            k = max(kv, ka)
            why.append("speed" if kv >= ka else "acceleration")
        per.append(round(k, 4))
    worst = max(range(n), key=lambda i: per[i]) if n else 0
    scale = per[worst] if n else 0.0
    out = {"scale": round(scale, 4), "motor": worst, "per_motor": per, "jumps": jumps,
           "limit": why[worst] if n else "",
           "kind": kind, "base": base, "margin": FIT_MARGIN,
           "v_need": [round(v, 2) for v in vreq], "a_need": [round(a, 2) for a in areq],
           "min_base": round(base * scale * FIT_MARGIN, 2) if kind else None}
    # fastest-stroke shape: the ramp that uses top speed and acceleration fully
    p = clip.get("params") or {}
    if clip.get("type") == "pattern" and p.get("shape") == "fast" and n and not continuous[worst]:
        d = 2 * float(p.get("amplitude", 0.3)) * abs(to_units(worst, 1.0) - to_units(worst, 0.0))
        out["best_ramp"] = round(best_ramp(d, vmax[worst], amax[worst]), 3)
    return out


def best_ramp(stroke: float, vmax: float, amax: float) -> float:
    """Ramp share for the fastest trapezoid over `stroke` with these limits."""
    if stroke <= 0 or vmax <= 0 or amax <= 0:
        return 0.25
    if stroke <= vmax * vmax / amax:             # never reaches top speed: accelerate, then brake
        return 0.5
    t_ramp = vmax / amax
    return clamp(t_ramp / (stroke / vmax + t_ramp), 0.05, 0.5)
