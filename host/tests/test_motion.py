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


# ---------------------------------------------------------------- shapes, shaping, layers, groups

from kinetic.config import default_motors as _dm  # noqa: E402


def _pat(name, **params):
    base = {"center": 0.5, "amplitude": 0.4, "period": 2.0}
    return {"type": "pattern", "pattern": name, "duration": 20, "params": {**base, **params}}


def test_flap_is_fast_one_way_slow_the_other():
    ms = _dm(1)
    c = _pat("flap", stroke_share=0.25)
    # reaches the far end at 25% of the cycle, home again at 100%
    assert abs(M.eval_clip(c, 0.5, ms)[0] - 0.9) < 1e-6
    assert abs(M.eval_clip(c, 0.0, ms)[0] - 0.1) < 1e-6
    rise = M.eval_clip(c, 0.25, ms)[0] - M.eval_clip(c, 0.0, ms)[0]
    fall = M.eval_clip(c, 0.5, ms)[0] - M.eval_clip(c, 0.75, ms)[0]
    assert rise > 2 * fall


def test_every_new_pattern_stays_in_range():
    ms = _dm(5)
    for name in ("flap", "pulse", "spring", "twitch", "steps"):
        c = _pat(name, chance=1.0)
        for k in range(200):
            vals = M.eval_clip(c, k * 0.05, ms)
            assert len(vals) == 5 and all(0.0 <= v <= 1.0 for v in vals), name


def test_twitch_is_repeatable_and_only_sometimes_moves():
    ms = _dm(3)
    c = _pat("twitch", chance=1.0, width=0.3, seed=5)
    a = [M.eval_clip(c, k * 0.1, ms) for k in range(60)]
    b = [M.eval_clip(c, k * 0.1, ms) for k in range(60)]
    assert a == b
    assert any(v[0] != 0.5 for v in a) and any(v[0] == 0.5 for v in a)


def test_steps_holds_levels():
    ms = _dm(1)
    c = _pat("steps", count=4, smooth=0.0, levels=[0.1, 0.6, 0.3, 0.9])
    assert [round(M.eval_clip(c, t, ms)[0], 3) for t in (0.1, 0.6, 1.1, 1.6)] == [0.1, 0.6, 0.3, 0.9]


def test_target_group_and_rest():
    ms = _dm(4)
    c = {**_pat("breathe"), "target": "wing", "mods": {"rest": 0.2}}
    vals = M.eval_clip(c, 0.5, ms, {"groups": {"wing": [1, 3]}})
    assert vals[0] == vals[2] == 0.2 and vals[1] == vals[3] != 0.2


def test_fade_in_and_out_envelope():
    ms = _dm(1)
    c = {**_pat("breathe"), "duration": 10, "mods": {"fade_in": 2, "fade_out": 2, "rest": 0.5}}
    assert M.eval_clip(c, 0.0, ms)[0] == 0.5
    assert M.eval_clip(c, 10.0, ms)[0] == 0.5
    assert abs(M.eval_clip(c, 5.0, ms)[0] - M.eval_clip(_pat("breathe"), 5.0, ms)[0]) < 1e-9


def test_variation_makes_motors_differ_but_stays_repeatable():
    ms = _dm(4)
    c = {**_pat("breathe"), "mods": {"amp_var": 0.6, "time_var": 0.5, "seed": 2}}
    v1 = M.eval_clip(c, 0.7, ms)
    assert len(set(round(v, 4) for v in v1)) > 1
    assert v1 == M.eval_clip(c, 0.7, ms)


def test_invert_and_tempo_ramp():
    ms = _dm(1)
    c = _pat("breathe")
    assert abs(M.eval_clip({**c, "mods": {"invert": True}}, 0.3, ms)[0] - (1 - M.eval_clip(c, 0.3, ms)[0])) < 1e-9
    fast = {**c, "duration": 10, "mods": {"tempo_start": 2, "tempo_end": 2}}
    assert abs(M.eval_clip(fast, 0.25, ms)[0] - M.eval_clip(c, 0.5, ms)[0]) < 1e-9


def test_layers_blend_modes_and_targets():
    ms = _dm(2)
    slow = {"id": "s", **_pat("pose", center=0.4), "params": {"values": [0.4, 0.4]}}
    flutter = {"id": "f", **_pat("pose"), "params": {"values": [0.7, 0.7], "center": 0.5}}
    clips = {"s": slow, "f": flutter}
    ctx = {"clips": clips, "groups": {"one": [1]}}
    lay = {"type": "layers", "duration": 10, "base": 0.0, "layers": [
        {"clip": "s", "blend": "over", "mix": 1.0},
        {"clip": "f", "blend": "add", "mix": 1.0, "target": "one"}]}
    v = M.eval_clip(lay, 1.0, ms, ctx)
    assert abs(v[0] - 0.4) < 1e-9 and abs(v[1] - 0.6) < 1e-9      # 0.4 + (0.7 - 0.5)
    lay["layers"][1]["blend"] = "scale"
    v = M.eval_clip(lay, 1.0, ms, ctx)
    assert abs(v[1] - 0.28) < 1e-9                                   # 0.4 * 0.7
    lay["layers"].append({"clip": "missing"})                        # dangling references are ignored
    assert len(M.eval_clip(lay, 1.0, ms, ctx)) == 2


def test_cubic_bezier_matches_known_points():
    # linear control points give a straight line; ease-in-out is symmetric about the middle
    for u in (0.0, 0.25, 0.5, 0.75, 1.0):
        assert abs(M.cubic_bezier(u, 0.25, 0.25, 0.75, 0.75) - u) < 1e-4
    assert abs(M.cubic_bezier(0.5, 0.42, 0.0, 0.58, 1.0) - 0.5) < 1e-4
    assert M.cubic_bezier(0.2, 0.42, 0.0, 0.58, 1.0) < 0.2            # slow start
    assert M.cubic_bezier(0.6, 0.3, 1.6, 0.7, 1.0) > 1.0              # overshoot


def test_bezier_key_in_track_and_wrap():
    keys = [[0, 0.0, "bezier", {"bz": [0.0, 1.0, 0.0, 1.0]}], [10, 1.0, "smooth"]]
    assert M.eval_track(keys, 2.0, 10, False) > 0.6                  # fast early rise
    keys_w = [[0, 0.0, "smooth"], [5, 1.0, "bezier", {"bz": [1.0, 0.0, 1.0, 0.0]}]]
    assert M.eval_track(keys_w, 7.0, 10, True) > 0.9                 # wrap segment uses the last key's curve
