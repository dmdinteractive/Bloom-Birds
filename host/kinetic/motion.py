"""Motion: the moves a show is made of, and the limiter that keeps them physically possible.

A show is a short list of moves that loops. Each move is described in 0..1 per bird
(0 = home end, 1 = far end of its taught travel); the engine turns that into millimetres
(or degrees) for each motor.

Moves so far:
    flap   a gentle sine between a top and a bottom height, with an optional delay from
           one bird to the next.
"""
from __future__ import annotations

import math

FIT_MARGIN = 1.10      # keep 10% in hand below the motor's limits when working out the fastest flap


def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


def smootherstep(u: float) -> float:
    u = clamp(u)
    return u * u * u * (u * (u * 6 - 15) + 10)


def blend(a: list[float], b: list[float], w: float) -> list[float]:
    w = smootherstep(w)
    return [x + (y - x) * w for x, y in zip(a, b)]


# ------------------------------------------------------------------ moves

# The knobs each move has. The screens build their controls from this list, so adding a move
# later means adding an entry here and a function in MOVE_FUNCS.
MOVES = {
    "flap": {
        "label": "Gentle flap",
        "help": "Each bird glides smoothly between a top and a bottom height, over and over.",
        "params": [
            {"key": "top", "label": "Top", "unit": "%", "min": 0.0, "max": 1.0, "step": 0.05, "default": 0.2,
             "help": "Highest point of the flap, as a share of the bird's travel (0% = home end)"},
            {"key": "bottom", "label": "Bottom", "unit": "%", "min": 0.0, "max": 1.0, "step": 0.05, "default": 0.6,
             "help": "Lowest point of the flap (100% = far end)"},
            {"key": "period", "label": "Seconds per flap", "unit": "s", "min": 0.2, "max": 120.0, "step": 0.5,
             "default": 12.0, "help": "Time for one full down-and-up. Smaller is faster."},
            {"key": "offset", "label": "Delay between birds", "unit": "%", "min": 0.0, "max": 1.0, "step": 0.05,
             "default": 0.0, "help": "How far each bird trails the one before it, as a share of one flap "
                                     "(0% = all together)"},
        ],
    },
}


def default_move(kind: str = "flap") -> dict:
    return {"type": kind, **{p["key"]: p["default"] for p in MOVES[kind]["params"]}}


def clean_move(move: dict) -> dict:
    """A move with every knob present and inside its range (unknown keys are dropped)."""
    kind = move.get("type") if move.get("type") in MOVES else "flap"
    out = {"type": kind}
    for p in MOVES[kind]["params"]:
        try:
            v = float(move.get(p["key"], p["default"]))
        except (TypeError, ValueError):
            v = p["default"]
        out[p["key"]] = round(clamp(v, p["min"], p["max"]), 4)
    return out


def flap(move: dict, phase: float, n: int, order: list[int] | None = None) -> list[float]:
    """Sine between top and bottom. `phase` counts flaps (1.0 = one full down-and-up) and the
    move starts at the top at phase 0. `order[i]` is bird i's place in line for the delay
    (birds that aren't set up don't take a place)."""
    top, bottom = move["top"], move["bottom"]
    out = []
    for i in range(n):
        k = order[i] if order else i
        u = phase - move.get("offset", 0.0) * k
        out.append(top + (bottom - top) * (1 - math.cos(2 * math.pi * u)) / 2)
    return out


MOVE_FUNCS = {"flap": flap}


def eval_move(move: dict, phase: float, n: int, order: list[int] | None = None) -> list[float]:
    return MOVE_FUNCS[move.get("type", "flap")](move, phase, n, order)


def fastest_period(stroke: float, vmax: float, amax: float) -> float:
    """Shortest sine period (seconds) over `stroke` units that stays inside a motor's speed and
    acceleration limits. A sine of stroke d and period T peaks at speed pi*d/T and
    acceleration 2*pi^2*d/T^2."""
    if stroke <= 0 or vmax <= 0 or amax <= 0:
        return 0.0
    by_speed = math.pi * stroke / vmax
    by_accel = math.sqrt(2 * math.pi ** 2 * stroke / amax)
    return max(by_speed, by_accel) * FIT_MARGIN


def move_fit(move: dict, travel: list[float], vmax: list[float], amax: list[float],
             active: list[bool]) -> dict:
    """How a move sits against each bird's limits: the fastest period each can follow, which
    limit sets it, and the peak speed and acceleration the current period asks for."""
    share = abs(move["bottom"] - move["top"])
    period = max(1e-3, move["period"])
    birds, worst = [], 0.0
    for i, tr in enumerate(travel):
        d = share * tr
        t_v = math.pi * d / vmax[i] if vmax[i] > 0 else 0.0
        t_a = math.sqrt(2 * math.pi ** 2 * d / amax[i]) if amax[i] > 0 else 0.0
        fastest = max(t_v, t_a) * FIT_MARGIN
        birds.append({"stroke": round(d, 1), "fastest": round(fastest, 2),
                      "limit": "speed" if t_v >= t_a else "acceleration",
                      "peak_v": round(math.pi * d / period, 1),
                      "peak_a": round(2 * math.pi ** 2 * d / period ** 2, 1),
                      "ok": period >= fastest - 1e-6})
        if active[i]:
            worst = max(worst, fastest)
    # round up, so a period set from this number is never a hair too fast
    return {"birds": birds, "fastest": math.ceil(worst * 10) / 10, "ok": period >= worst - 1e-6}


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
