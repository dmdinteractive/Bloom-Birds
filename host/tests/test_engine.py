"""End-to-end: engine + simulated PD Steppers, no hardware."""
import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kinetic import protocol as P  # noqa: E402
from kinetic.config import Config  # noqa: E402
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
    assert eng.armed and eng.mode == "playlist"
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


def test_crossfade_is_continuous(setup):
    cfg, tx, eng = setup
    eng.play()
    samples = []

    async def go():
        t0 = time.monotonic()
        while time.monotonic() - t0 < 2.0:
            eng.tick(0.02)
            samples.append(list(eng.out_mm))
            if abs(time.monotonic() - t0 - 1.0) < 0.011:
                eng.next()
            await asyncio.sleep(0.02)

    asyncio.run(go())
    jumps = [max(abs(a - b) for a, b in zip(p, q)) for p, q in zip(samples, samples[1:])]
    # at 150 mm/s max speed and 20 ms ticks nothing may jump more than ~3 mm per tick
    assert max(jumps) < 4.0


def test_live_edit_changes_playing_clip(setup):
    cfg, tx, eng = setup
    clip = eng.show["clips"][0]
    eng.solo(clip["id"])
    eng.tick(0.02)
    show = dict(eng.show)
    show["clips"] = [dict(c) for c in eng.show["clips"]]
    show["clips"][0] = {**clip, "pattern": "pose", "params": {"values": [1.0] * 8}}
    eng.save_show(show)
    eng.fade_from = None
    eng.tick(0.02)
    assert eng.last_norm == [1.0] * 8


# ---------------------------------------------------------------- operator panel features

def test_speed_scales_show_clock(setup):
    cfg, tx, eng = setup
    eng.set_playback(speed=0.5)
    eng.play()
    for _ in range(100):          # 2 s of wall time
        eng.tick(0.02)
    assert abs(eng.snapshot()["clip_t"] - 1.0) < 0.05     # show advanced 1 s
    eng.set_playback(speed=2.0)
    t_before = eng.snapshot()["clip_t"]
    for _ in range(50):           # 1 s of wall time
        eng.tick(0.02)
    assert abs(eng.snapshot()["clip_t"] - t_before - 2.0) < 0.05


def test_speed_change_does_not_jump_positions(setup):
    cfg, tx, eng = setup
    eng.play()
    for _ in range(300):
        eng.tick(0.02)
    before = list(eng.out_mm)
    eng.set_playback(speed=1.8)
    eng.tick(0.02)
    assert max(abs(a - b) for a, b in zip(before, eng.out_mm)) < 4.0


def test_depth_limit_compresses_toward_home(setup):
    cfg, tx, eng = setup
    clip = {"id": "pose1", "name": "p", "type": "pattern", "pattern": "pose", "duration": 10, "fade": 0,
            "params": {"values": [1.0] * cfg.motor_count}}
    eng.save_show({**eng.show, "clips": eng.show["clips"] + [clip]})
    eng.solo("pose1")
    eng.fade_from = None
    eng.set_playback(depth=0.5)
    eng.tick(0.02)
    assert all(abs(d - 0.5 * cfg.travel(i)) < 1e-6 for i, d in enumerate(eng.desired_mm))


def test_disabled_entries_are_skipped(setup):
    cfg, tx, eng = setup
    show = eng.show
    show["playlist"][0]["enabled"] = False
    eng.save_show(show)
    eng.play()
    assert eng.entry_index == 1
    eng.next()
    assert eng.entry_index == 2
    eng.next()
    assert eng.entry_index == 1          # wraps past the disabled first entry


def test_all_disabled_holds(setup):
    cfg, tx, eng = setup
    show = eng.show
    for e in show["playlist"]:
        e["enabled"] = False
    eng.save_show(show)
    eng.play()
    assert eng.mode == "hold"


def test_play_entry_starts_at_index(setup):
    cfg, tx, eng = setup
    eng.play_entry(2)
    assert eng.mode == "playlist" and eng.entry_index == 2
    assert eng.source.clip_id == eng.show["playlist"][2]["clip"]


def test_playback_settings_persist(setup, tmp_path):
    cfg, tx, eng = setup
    eng.set_playback(speed=0.8, depth=0.6)
    from kinetic.config import Config as C
    again = C(cfg.path)
    assert again.data["playback"] == {"speed": 0.8, "depth": 0.6}


def test_corrupt_show_file_recovers(tmp_path):
    shows = tmp_path / "shows"
    shows.mkdir()
    (shows / "default.json").write_text('{"name": "default", "clips": [')   # truncated by a power cut
    cfg = Config(tmp_path / "config.json")
    eng = Engine(cfg, SimTransport(cfg.motor_count, cfg.node_config_args()), shows)
    assert eng.show["clips"]                       # fell back to a working show
    assert (shows / "default.corrupt").exists()    # evidence kept


def test_atomic_write_leaves_no_temp(tmp_path):
    from kinetic.config import write_atomic
    p = tmp_path / "x.json"
    write_atomic(p, "one")
    write_atomic(p, "two")
    assert p.read_text() == "two" and not (tmp_path / "x.json.tmp").exists()
