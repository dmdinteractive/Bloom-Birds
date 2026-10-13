import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kinetic import motion as M  # noqa: E402
from kinetic.config import default_motors  # noqa: E402

MOTORS = default_motors(8, "line")


def test_periodic_range_and_dwell():
    for d in (0, 0.3, 0.8):
        vals = [M.periodic(u / 200, d) for u in range(400)]
        assert min(vals) >= -1 - 1e-9 and max(vals) <= 1 + 1e-9
    # with dwell, a noticeable share of samples sit exactly at the extremes
    held = sum(1 for u in range(1000) if abs(M.periodic(u / 1000, 0.5)) == 1.0)
    assert 400 < held < 600


def test_every_pattern_stays_in_range():
    for name in M.PATTERNS:
        params = {d["key"]: d["default"] for d in M.COMMON_PARAMS + M.PATTERNS[name]["params"]}
        params["values"] = [0.1 * i for i in range(8)]
        for t in (0, 1.3, 7.7, 100.0):
            vals = M.eval_pattern(name, t, MOTORS, params)
            assert len(vals) == 8
            assert all(0 <= v <= 1 for v in vals), (name, vals)


def test_wave_has_phase_offsets_and_breathe_does_not():
    p = {"center": 0.5, "amplitude": 0.3, "period": 10, "wavelengths": 1, "direction": 0}
    w = M.eval_pattern("wave", 2.0, MOTORS, p)
    b = M.eval_pattern("breathe", 2.0, MOTORS, p)
    assert len(set(round(v, 4) for v in w)) > 3
    assert len(set(round(v, 4) for v in b)) == 1


def test_patterns_are_continuous_in_time():
    for name in ("wave", "ripple", "spin", "chase", "drift", "alternate"):
        params = {d["key"]: d["default"] for d in M.COMMON_PARAMS + M.PATTERNS[name]["params"]}
        prev = M.eval_pattern(name, 0, MOTORS, params)
        for k in range(1, 600):
            cur = M.eval_pattern(name, k * 0.02, MOTORS, params)
            assert max(abs(a - b) for a, b in zip(prev, cur)) < 0.05, name
            prev = cur


def test_track_eval_eases_and_wrap():
    keys = [[0, 0.0, "linear"], [10, 1.0, "smooth"]]
    assert M.eval_track(keys, 5, 20, wrap=False) == 0.5
    assert M.eval_track(keys, 15, 20, wrap=False) == 1.0          # holds after last key
    wrapped = M.eval_track(keys, 15, 20, wrap=True)                 # flows back to first key
    assert 0.0 < wrapped < 1.0
    assert abs(M.eval_track(keys, 19.999, 20, True) - M.eval_track(keys, 0.0, 20, True)) < 1e-3
    assert M.eval_track([[0, 0.3, "hold"], [5, 0.9]], 4.9, 10, False) == 0.3


def test_timeline_stagger_delays_motors():
    clip = {"type": "timeline", "duration": 20, "mode": "shared", "stagger": 7, "wrap": False,
            "tracks": {"all": [[0, 0, "linear"], [10, 1, "linear"]]}}
    vals = M.eval_clip(clip, 7.0, MOTORS)
    assert vals[0] > vals[-1]
    assert abs(vals[0] - 0.7) < 1e-9 and abs(vals[-1] - 0.0) < 1e-9


def test_per_motor_tracks_fall_back_to_shared():
    clip = {"type": "timeline", "duration": 10, "mode": "per_motor", "wrap": False,
            "tracks": {"all": [[0, 0.2]], "3": [[0, 0.9]]}}
    vals = M.eval_clip(clip, 1, MOTORS)
    assert vals[3] == 0.9 and vals[0] == 0.2


def test_limiter_respects_speed_and_accel():
    vmax, amax, dt = 100.0, 200.0, 0.02
    lim = M.Limiter(1, vmax, amax)
    lim.reset([0.0])
    xs, vs = [], []
    for _ in range(600):
        x, v = lim.step([800.0], dt, [0.0], [1000.0])
        xs.append(x[0]); vs.append(v[0])
    assert max(abs(v) for v in vs) <= vmax + 1e-6
    acc = [abs(b - a) / dt for a, b in zip(vs, vs[1:])]
    assert max(acc) <= amax + 1e-6
    assert abs(xs[-1] - 800.0) < 0.5
    assert max(xs) < 801.0            # no meaningful overshoot


def test_limiter_tracks_feasible_motion_closely():
    lim = M.Limiter(1, 150, 300)
    lim.reset([500.0])
    worst = 0.0
    for k in range(1, 1500):
        t = k * 0.02
        d = 500 + 200 * math.sin(2 * math.pi * t / 12)   # peak 105 mm/s, 55 mm/s^2
        x, _ = lim.step([d], 0.02, [0], [1000])
        if t > 2:
            worst = max(worst, abs(x[0] - d))
    assert worst < 3.0
    assert not lim.limited[0]


def test_limiter_flags_infeasible_motion():
    lim = M.Limiter(1, 50, 100)
    lim.reset([0.0])
    flagged = False
    for k in range(500):
        d = 500 + 400 * math.sin(2 * math.pi * k * 0.02 / 2)
        lim.step([d], 0.02, [0], [1000])
        flagged |= lim.limited[0]
    assert flagged


def test_limiter_clamps_to_travel():
    lim = M.Limiter(1, 1000, 5000)
    lim.reset([10.0])
    for _ in range(200):
        x, _ = lim.step([-50.0], 0.02, [0.0], [400.0])
    assert x[0] == 0.0


def test_render_clip_shape():
    clip = {"type": "pattern", "pattern": "wave", "duration": 10, "params": {"period": 5}}
    r = M.render_clip(clip, MOTORS, [1000.0] * 8, 150, 300)
    assert len(r["t"]) == len(r["values"]) == len(r["limited"])
    assert len(r["values"][0]) == 8
