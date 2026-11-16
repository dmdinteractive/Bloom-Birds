"""End-to-end: engine + simulated PD Steppers, no hardware."""
import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kinetic import motion as M  # noqa: E402
from kinetic import protocol as P  # noqa: E402
from kinetic.config import Config, default_motors  # noqa: E402
from kinetic.engine import Engine  # noqa: E402
from kinetic.transport import SimTransport  # noqa: E402


class Recorder(SimTransport):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.frames = []

    def send(self, payload):
        if payload[3] == P.T_FRAME:
            self.frames.append(P.unpack_frame(payload))
        super().send(payload)


@pytest.fixture
def setup(tmp_path):
    cfg = Config(tmp_path / "config.json")
    cfg.data["motors"] = default_motors(8)
    cfg.data["mechanics"]["home_timeout_s"] = 5
    tx = Recorder(cfg.motor_count, cfg.node_config_args())
    eng = Engine(cfg, tx, tmp_path / "shows")
    tx.start()
    yield cfg, tx, eng
    tx.stop()


async def run_for(eng, seconds):
    task = asyncio.create_task(eng.run())
    await asyncio.sleep(seconds)
    task.cancel()


def test_autostart_homes_arms_and_plays(setup):
    cfg, tx, eng = setup

    async def go():
        runner = asyncio.create_task(eng.run())
        await asyncio.wait_for(eng.autostart(), timeout=30)
        await asyncio.sleep(3)
        runner.cancel()

    asyncio.run(go())
    assert eng.armed and eng.mode == "play"
    assert all(eng.node_by_id(i)["status"].has("homed") for i in range(cfg.motor_count))
    enabled = [f for f in tx.frames if f[1] & P.FF_ENABLE]
    assert len(enabled) > 50
    # every frame carries a target for every motor -> they move from the same packet
    assert all(len(f[2]) == cfg.motor_count for f in enabled)
    # targets stay within travel
    spm = cfg.steps_per_mm
    for _, _, targets in enabled:
        for pos, _ in targets:
            assert -1 <= pos / spm <= cfg.mech["travel_mm"] + 1


def test_no_frames_when_disarmed(setup):
    cfg, tx, eng = setup
    eng.play()
    asyncio.run(run_for(eng, 0.6))
    assert tx.frames == []           # preview only: nothing sent to motors


def test_disarm_sends_stop_frames_then_silence(setup):
    cfg, tx, eng = setup
    eng.armed = True
    eng.play()
    asyncio.run(run_for(eng, 0.3))
    eng.arm(False)
    tx.frames.clear()
    asyncio.run(run_for(eng, 1.2))
    assert tx.frames and all(not (f[1] & P.FF_ENABLE) for f in tx.frames)
    assert len(tx.frames) <= 26


def test_starting_and_holding_never_jump(setup):
    cfg, tx, eng = setup
    eng.play()
    samples = []
    for k in range(400):
        if k == 150:
            eng.hold()
        if k == 250:
            eng.play()
        eng.tick(0.02)
        samples.append(list(eng.out_mm))
    jumps = [max(abs(a - b) for a, b in zip(p, q)) for p, q in zip(samples, samples[1:])]
    # at 150 mm/s max speed and 20 ms ticks nothing may jump more than ~3 mm per tick
    assert max(jumps) < 4.0


def test_live_edit_changes_playing_move(setup):
    cfg, tx, eng = setup
    eng.play()
    eng.fade_from = None
    eng.set_move(top=0.5, bottom=0.5)
    eng.tick(0.02)
    assert all(abs(v - 0.5) < 1e-9 for v in eng.last_norm)


# ---------------------------------------------------------------- the flap move

def test_flap_is_a_sine_between_top_and_bottom():
    m = M.clean_move({"type": "flap", "top": 0.2, "bottom": 0.6, "period": 4, "offset": 0.25})
    assert M.flap(m, 0.0, 1) == [0.2]                                  # starts at the top
    assert abs(M.flap(m, 0.5, 1)[0] - 0.6) < 1e-9                      # half a flap later: bottom
    assert abs(M.flap(m, 0.25, 1)[0] - 0.4) < 1e-9                     # a quarter: midway
    a, b = M.flap(m, 0.25, 2)
    assert abs(b - 0.2) < 1e-9                                         # second bird trails by a quarter flap


def test_clean_move_clamps_and_fills_in():
    m = M.clean_move({"top": -3, "period": 0, "junk": 1})
    assert m["type"] == "flap" and m["top"] == 0.0 and m["period"] == 0.2 and "junk" not in m
    assert m["bottom"] == 0.6


def test_fastest_period_follows_the_motor_limits():
    # stroke 400 mm, 150 mm/s, 300 mm/s^2: speed sets it (pi * 400 / 150 = 8.38 s, plus margin)
    fit = M.move_fit(M.clean_move({"top": 0.2, "bottom": 0.6, "period": 8}), [1000.0], [150.0], [300.0], [True])
    assert fit["birds"][0]["limit"] == "speed" and abs(fit["birds"][0]["fastest"] - 8.38 * M.FIT_MARGIN) < 0.02
    assert not fit["ok"] and fit["fastest"] >= fit["birds"][0]["fastest"]
    # a faster motor allows a faster flap
    assert M.move_fit(M.clean_move({"top": 0.2, "bottom": 0.6, "period": 8}), [1000.0], [600.0], [3000.0], [True])["ok"]


def test_flap_at_the_fastest_period_is_followed_without_lag(setup):
    cfg, tx, eng = setup
    eng.set_move(period=eng.fit()["fastest"])
    eng.play()
    limited = False
    for k in range(3000):
        eng.tick(0.02)
        if k > 400:                      # after easing in
            limited = limited or any(eng.limiter.limited)
    assert not limited
    assert eng.snapshot()["fit"]["ok"]


def test_too_fast_a_flap_is_flagged_and_stays_within_limits(setup):
    cfg, tx, eng = setup
    eng.set_move(period=1.0)
    assert not eng.fit()["ok"]
    eng.play()
    vmax = cfg.vmax(0)
    seen = False
    for _ in range(600):
        eng.tick(0.02)
        seen = seen or any(eng.limiter.limited)
        assert all(abs(v) <= vmax + 1e-6 for v in eng.out_v)
    assert seen


def test_changing_pace_does_not_jump(setup):
    cfg, tx, eng = setup
    eng.play()
    for _ in range(300):
        eng.tick(0.02)
    before = list(eng.out_mm)
    eng.set_move(period=20.0)
    eng.tick(0.02)
    assert max(abs(a - b) for a, b in zip(before, eng.out_mm)) < 4.0


def test_only_set_up_birds_take_a_place_in_line(setup):
    cfg, tx, eng = setup
    for i, m in enumerate(cfg.motors):
        m["commissioned"] = i in (0, 3)
    assert eng.bird_order()[0] == 0 and eng.bird_order()[3] == 1


def test_move_is_saved_and_reloaded(setup, tmp_path):
    cfg, tx, eng = setup
    eng.set_move(top=0.1, bottom=0.9, period=12, offset=0.2)
    again = Engine(cfg, SimTransport(cfg.motor_count, cfg.node_config_args()), eng.shows_dir)
    assert again.move() == {"type": "flap", "top": 0.1, "bottom": 0.9, "period": 12.0, "offset": 0.2}


def test_old_format_show_is_kept_aside(tmp_path):
    shows = tmp_path / "shows"
    shows.mkdir()
    (shows / "default.json").write_text('{"name": "default", "clips": [], "playlist": []}')
    cfg = Config(tmp_path / "config.json")
    eng = Engine(cfg, SimTransport(cfg.motor_count, cfg.node_config_args()), shows)
    assert eng.move()["type"] == "flap"
    assert (shows / "default.old-format.json").exists()


def test_corrupt_show_file_recovers(tmp_path):
    shows = tmp_path / "shows"
    shows.mkdir()
    (shows / "default.json").write_text('{"name": "default", "clips": [')   # truncated by a power cut
    cfg = Config(tmp_path / "config.json")
    eng = Engine(cfg, SimTransport(cfg.motor_count, cfg.node_config_args()), shows)
    assert eng.move()["type"] == "flap"            # fell back to a working show
    assert (shows / "default.corrupt").exists()    # evidence kept


def test_atomic_write_leaves_no_temp(tmp_path):
    from kinetic.config import write_atomic
    p = tmp_path / "x.json"
    write_atomic(p, "one")
    write_atomic(p, "two")
    assert p.read_text() == "two" and not (tmp_path / "x.json.tmp").exists()
