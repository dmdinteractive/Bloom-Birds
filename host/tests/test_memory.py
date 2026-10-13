"""Position memory (no home switches) and the Motor Setup wizard, against the simulated nodes.

The simulator keeps the *physical* position of each load separate from what the node believes,
so these tests check the real thing: after a power cut, does the node's idea of its position
match where the load physically is?
"""
import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kinetic import protocol as P  # noqa: E402
from kinetic.config import Config, relayout_motors  # noqa: E402
from kinetic.engine import Engine  # noqa: E402
from kinetic.transport import SimTransport  # noqa: E402


@pytest.fixture
def rig(tmp_path):
    cfg = Config(tmp_path / "config.json")
    cfg.data["motors"] = relayout_motors(cfg.motors, 4, "line")
    travel_steps = int(cfg.mech["travel_mm"] * cfg.steps_per_mm)
    tx = SimTransport(cfg.motor_count, cfg.node_config_args(), unassigned=1,
                      state_path=tmp_path / "nodes.json", travel_steps=travel_steps)
    eng = Engine(cfg, tx, tmp_path / "shows")
    tx.start()
    yield cfg, tx, eng
    tx.stop()


def node(tx, i):
    return next(n for n in tx.nodes if n.id == i)


def believed_vs_true(cfg, tx, i):
    """(where the node thinks it is, where the load really is) in mm."""
    n = node(tx, i)
    spm = cfg.steps_per_mm
    return n.encoder_steps() / spm, n.phys_fs * n.ms / spm


async def run_show(eng, seconds):
    runner = asyncio.create_task(eng.run())
    await asyncio.wait_for(eng.autostart(), 30)
    await asyncio.sleep(seconds)
    return runner


def test_simulated_motors_start_set_up_and_play(rig):
    cfg, tx, eng = rig

    async def go():
        runner = await run_show(eng, 3)
        runner.cancel()
    asyncio.run(go())
    assert eng.armed and eng.mode == "playlist"
    for i in range(cfg.motor_count):
        st = eng.node_by_id(i)["status"]
        assert st.has("homed") and st.has("restored") and st.has("calibrated") and st.has("limits")


def test_power_cut_mid_show_with_good_save_restores_exact_position(rig):
    cfg, tx, eng = rig

    async def go():
        runner = await run_show(eng, 6)          # motors are moving
        tx.power_cut(save_works=True)
        await asyncio.sleep(0.5)
        runner.cancel()
    asyncio.run(go())
    for i in range(cfg.motor_count):
        n = node(tx, i)
        assert n.homed and n.restored
        believed, true = believed_vs_true(cfg, tx, i)
        assert abs(believed - true) < 0.25, (i, believed, true)   # encoder count = 0.03 mm
        assert true > 1.0          # really was somewhere below the top, not trivially 0


def test_repeated_power_cuts_do_not_drift(rig):
    """Ten power cuts in a row while moving: the error must not grow, because every restore is
    re-derived from the absolute magnet angle and the taught calibration."""
    cfg, tx, eng = rig
    errors = []

    async def go():
        runner = await run_show(eng, 3)
        for _ in range(10):
            await asyncio.sleep(1.3)
            tx.power_cut(save_works=True)
            await asyncio.sleep(0.5)
            for i in range(cfg.motor_count):
                b, t = believed_vs_true(cfg, tx, i)
                errors.append(abs(b - t))
            eng.arm(False)
            eng.arm(True)                       # resume the show after each cut
        runner.cancel()
    asyncio.run(go())
    assert max(errors) < 0.25, max(errors)
    assert max(errors[-4:]) < 0.25


def test_power_cut_while_moving_with_failed_save_reports_position_lost(rig):
    cfg, tx, eng = rig

    async def go():
        runner = await run_show(eng, 6)
        runner.cancel()
        tx.power_cut(save_works=False)
    asyncio.run(go())
    # a full power cut restarts the Pi too, so it has no memory of the positions either
    eng2 = Engine(cfg, tx, eng.shows_dir)

    async def go2():
        runner = asyncio.create_task(eng2.run())
        await asyncio.sleep(0.8)
        runner.cancel()
    asyncio.run(go2())
    for i in range(cfg.motor_count):
        st = eng2.node_by_id(i)["status"]
        assert not st.has("homed") and st.has("pos_lost")
    # and the controller never moves a motor that doesn't know where it is
    assert all(not node(tx, i).streaming for i in range(cfg.motor_count))


def test_single_node_reboot_is_restored_by_the_pi(rig):
    cfg, tx, eng = rig

    async def go():
        runner = await run_show(eng, 6)
        tx.reboot_node(node(tx, 2).mac)          # watchdog-style reset, no power-loss save
        await asyncio.sleep(1.5)
        runner.cancel()
    asyncio.run(go())
    n = node(tx, 2)
    assert n.homed and n.restored_host
    believed, true = believed_vs_true(cfg, tx, 2)
    assert abs(believed - true) < 0.25


def test_soft_limits_clamp_show_and_jogs(rig):
    cfg, tx, eng = rig
    spm = cfg.steps_per_mm
    hi = node(tx, 0).lim_hi()

    async def go():
        runner = asyncio.create_task(eng.run())
        await asyncio.sleep(0.5)
        eng.jog(0, cfg.mech["travel_mm"] + 300, speed_mm_s=2000)   # asks for 300 mm past the bottom
        await asyncio.sleep(9)                                       # node caps speed at 225 mm/s
        runner.cancel()
    asyncio.run(go())
    n = node(tx, 0)
    assert n.steps <= hi + 1
    assert abs(n.steps / spm - cfg.mech["travel_mm"]) < 1


def test_quitting_and_restarting_the_simulator_is_a_power_cycle(rig, tmp_path):
    cfg, tx, eng = rig

    async def go():
        runner = await run_show(eng, 5)
        runner.cancel()
    asyncio.run(go())
    before = [believed_vs_true(cfg, tx, i)[1] for i in range(cfg.motor_count)]
    tx.stop()                                     # writes the state file
    tx2 = SimTransport(cfg.motor_count, cfg.node_config_args(), unassigned=1, state_path=tmp_path / "nodes.json")
    assert not tx2.fresh
    for i in range(cfg.motor_count):
        n = node(tx2, i)
        assert n.homed and n.restored
        assert abs(n.encoder_steps() / cfg.steps_per_mm - before[i]) < 0.25


def test_relayout_keeps_taught_limits(rig):
    cfg, tx, eng = rig
    cfg.motors[1].update({"commissioned": True, "bottom_fs": 1234.0, "invert_dir": True})
    new = relayout_motors(cfg.motors, 6, "circle")
    assert len(new) == 6 and new[1]["bottom_fs"] == 1234.0 and new[1]["invert_dir"] is True
    assert "commissioned" not in new[5]


# ---------------------------------------------------------------- the setup wizard

def test_setup_wizard_on_a_miswired_board(rig):
    """The extra simulated board has its motor wired backwards AND its magnet flipped.
    Walk the wizard like a tech would, then power-cut and check it recovers."""
    cfg, tx, eng = rig
    spm = cfg.steps_per_mm
    fresh = next(n for n in tx.nodes if n.id == P.ID_UNASSIGNED)
    slot = 3                                  # replace motor 4's board with the new one

    def true_mm():
        return fresh.phys_fs * fresh.ms / spm

    async def idle():
        await asyncio.sleep(0.4)
        for _ in range(200):
            if not eng.wizard or not eng.wizard["busy"]:
                return
            await asyncio.sleep(0.1)
        raise AssertionError("wizard step hung")

    async def go():
        runner = asyncio.create_task(eng.run())
        await asyncio.sleep(0.6)
        eng.wiz_start(fresh.mac, slot)
        await idle()
        assert fresh.id == slot
        old = next(n for n in tx.nodes if n.mac == "5E:00:00:00:00:03")
        assert old.id == P.ID_UNASSIGNED          # previous board released from the slot

        # 1. direction: the tech watches the load
        before = true_mm()
        eng.wiz_direction_test()
        await idle()
        lowered = true_mm() > before
        assert lowered is False                   # miswired: it wound up
        eng.wiz_direction_answer(lowered)
        await idle()
        assert eng.wizard["step"] == "encoder"
        assert abs(true_mm() - before) < 0.5      # back where it started

        # 2. encoder check fixes the flipped magnet by itself
        eng.wiz_encoder_check()
        await idle()
        assert eng.wizard["error"] == "", eng.wizard["error"]
        assert eng.wizard["encoder_ok"] and eng.wizard["step"] == "top"

        # 3. teach top: jog up until the load is at the physical top
        eng.wiz_jog(-true_mm())
        await asyncio.sleep(true_mm() / 40 + 3)
        assert abs(true_mm()) < 0.5
        eng.wiz_set_top()
        await idle()
        assert eng.wizard["step"] == "bottom"

        # 4. teach bottom 500 mm down
        eng.wiz_jog(500)
        await asyncio.sleep(500 / 40 + 3)
        eng.wiz_set_bottom()
        await idle()
        assert eng.wizard["step"] == "test" and abs(eng.wizard["bottom_mm"] - 500) < 1

        # limits are live: a free jog is not allowed now, a normal one stops at the bottom
        eng.jog(slot, 200, speed_mm_s=200)
        await asyncio.sleep(3)
        assert true_mm() <= 500.5

        # 5. test moves, 6. finish
        eng.wiz_test()
        await idle()
        assert eng.wizard["test_done"] and abs(true_mm()) < 0.5
        eng.wiz_finish()
        assert eng.wizard is None

        # it plays, and survives a power cut mid-show
        eng.arm(True)
        eng.play()
        await asyncio.sleep(5)
        tx.power_cut(save_works=True)
        await asyncio.sleep(0.6)
        runner.cancel()

    asyncio.run(go())
    m = cfg.motors[slot]
    assert m["commissioned"] and m["node_mac"] == fresh.mac
    assert abs(cfg.travel(slot) - 500) < 1
    assert fresh.homed and fresh.restored
    assert abs(fresh.encoder_steps() / spm - true_mm()) < 0.25
    # the saved config makes the miswired motor behave: + = lower, encoder agrees
    assert fresh._dir_sign() == 1
    # its directions are pinned: changing the Mechanics defaults later doesn't touch it
    cfg.mech["invert_dir"] = not cfg.mech["invert_dir"]
    assert cfg.node_config_args(slot)[5] & 3 == 3


def test_recover_mode_reteaches_top_and_keeps_bottom(rig):
    cfg, tx, eng = rig
    cfg.motors[0].update({"commissioned": True, "bottom_fs": 4000.0, "travel_mm": None})
    n0 = node(tx, 0)

    async def go():
        runner = asyncio.create_task(eng.run())
        await asyncio.sleep(0.5)
        eng.jog(0, 600)                       # cut the power while it is still moving
        await asyncio.sleep(2)
        assert node(tx, 0).jog_target is not None
        eng.last_known.clear()                # full power cut: the Pi restarted too
        tx.power_cut(save_works=False)        # ...and the node's power-loss save failed
        await asyncio.sleep(0.6)
        assert eng.node_by_id(0)["status"].has("pos_lost")
        eng.wiz_start(n0.mac, 0, recover=True)
        await asyncio.sleep(1.0)
        assert eng.wizard["step"] == "top"
        drop = n0.phys_fs * n0.ms / cfg.steps_per_mm
        eng.wiz_jog(-drop)
        await asyncio.sleep(drop / 40 + 3)
        eng.wiz_set_top()
        await asyncio.sleep(1.5)
        assert eng.wizard["step"] == "finish"
        eng.wiz_finish()
        await asyncio.sleep(0.6)
        runner.cancel()
    asyncio.run(go())
    st = eng.node_by_id(0)["status"]
    assert st.has("homed") and st.has("limits")
    assert abs(st.lim_max - cfg.bottom_usteps(0)) <= 1
