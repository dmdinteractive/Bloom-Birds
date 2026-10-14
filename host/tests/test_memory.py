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
        eng.jog(0, cfg.mech["travel_mm"] + 300, speed=2000)   # asks for 300 mm past the bottom
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

    async def idle(limit=20):
        await asyncio.sleep(0.4)
        for _ in range(int(limit * 10)):
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

        # 0. mechanism: it drives a cable spool
        assert eng.wizard["step"] == "mechanism"
        eng.wiz_mechanism("spool")
        await idle()
        assert eng.wizard["step"] == "direction"

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
        eng.jog(slot, 200, speed=200)
        await asyncio.sleep(3)
        assert true_mm() <= 500.5

        # 5. test moves, 6. finish
        eng.wiz_test()
        await idle(60)                            # slow, careful moves (a third of top speed)
        assert eng.wizard["test_done"] and abs(true_mm()) < 0.5
        assert eng.wizard["step"] == "speed"      # the speed test is offered next (optional)
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


# ---------------------------------------------------------------- adding / removing motors

def _run(eng, seconds):
    async def go():
        t = asyncio.create_task(eng.run())
        await asyncio.sleep(seconds)
        t.cancel()
    asyncio.run(go())


def test_new_install_starts_with_one_motor(tmp_path):
    assert Config(tmp_path / "c.json").motor_count == 1


def test_remove_middle_motor_renumbers_boards_and_data(rig):
    cfg, tx, eng = rig
    cfg.motors[2].update({"commissioned": True, "bottom_fs": 2222.0, "node_mac": "5E:00:00:00:00:02"})
    pose = {"id": "pz", "name": "pose", "type": "pattern", "pattern": "pose", "duration": 5, "fade": 0,
            "params": {"values": [0.0, 0.1, 0.2, 0.3]}}
    eng.save_show({**eng.show, "clips": eng.show["clips"] + [pose]})
    _run(eng, 0.6)
    eng.remove_motors([1])
    _run(eng, 0.6)
    assert cfg.motor_count == 3 and [m["id"] for m in cfg.motors] == [0, 1, 2]
    assert [m["name"] for m in cfg.motors] == ["M1", "M2", "M3"]
    # the board that was M3 is now M2 and carries its setup with it
    assert next(n for n in tx.nodes if n.mac == "5E:00:00:00:00:02").id == 1
    assert cfg.motors[1]["bottom_fs"] == 2222.0
    assert next(n for n in tx.nodes if n.mac == "5E:00:00:00:00:01").id == P.ID_UNASSIGNED
    assert next(n for n in tx.nodes if n.mac == "5E:00:00:00:00:03").id == 2
    assert eng.clip("pz")["params"]["values"] == [0.0, 0.2, 0.3]


def test_cannot_remove_the_last_motor(rig):
    cfg, tx, eng = rig
    with pytest.raises(ValueError):
        eng.remove_motors(list(range(cfg.motor_count)))


def test_add_motor_and_remove_unused(rig):
    cfg, tx, eng = rig
    _run(eng, 0.6)
    eng.add_motor()
    eng.add_motor()
    assert cfg.motor_count == 6 and cfg.motors[5]["name"] == "M6"
    _run(eng, 0.6)
    assert eng.remove_unused() == 2          # M5, M6: not set up, no board reporting
    assert cfg.motor_count == 4


def test_wizard_new_slot_adds_motor_and_cancel_takes_it_away(rig):
    cfg, tx, eng = rig
    fresh = next(n for n in tx.nodes if n.id == P.ID_UNASSIGNED)

    async def go():
        t = asyncio.create_task(eng.run())
        await asyncio.sleep(0.6)
        eng.wiz_start(fresh.mac, cfg.motor_count)        # "New motor"
        await asyncio.sleep(1.0)
        assert cfg.motor_count == 5 and fresh.id == 4 and eng.wizard["added"]
        eng.wiz_cancel()
        await asyncio.sleep(0.6)
        t.cancel()
    asyncio.run(go())
    assert cfg.motor_count == 4
    assert fresh.id == P.ID_UNASSIGNED


def test_set_motor_count(rig):
    cfg, tx, eng = rig
    _run(eng, 0.6)
    eng.set_motor_count(6)
    assert cfg.motor_count == 6
    eng.set_motor_count(4)                    # removes the two empty slots, no confirmation needed
    assert cfg.motor_count == 4
    with pytest.raises(ValueError):
        eng.set_motor_count(2)                # M3, M4 have boards reporting
    eng.set_motor_count(2, force=True)
    assert cfg.motor_count == 2


# ---------------------------------------------------------------- other mechanisms

def test_rotary_motor_moves_in_degrees(rig):
    cfg, tx, eng = rig
    cfg.motors[0].update({"kind": "rotary", "size": 5.0})          # 5:1 gearbox
    spu = cfg.spu(0)
    assert abs(spu - 3200 / 72) < 1e-6                             # 72 output degrees per motor turn
    cfg.motors[0]["travel_mm"] = 180.0                             # 0..180 degrees
    eng.send_cmd(P.C_SET_LIMITS, 0, args=[0, 0, 0])
    pose = {"id": "rz", "name": "pose", "type": "pattern", "pattern": "pose", "duration": 30, "fade": 0,
            "params": {"values": [0.5] * cfg.motor_count}}
    eng.save_show({**eng.show, "clips": eng.show["clips"] + [pose]})
    eng.push_config(0)

    async def go():
        t = asyncio.create_task(eng.run())
        await asyncio.sleep(0.6)
        eng.arm(True)
        eng.solo("rz")
        await asyncio.sleep(9)
        t.cancel()
    asyncio.run(go())
    assert abs(node(tx, 0).steps / spu - 90.0) < 1.0               # half of 180 degrees


def test_continuous_motor_runs_at_speed_and_respects_limits(rig):
    cfg, tx, eng = rig
    cfg.motors[0].update({"kind": "continuous", "size": 1.0, "max_speed": 1.0, "max_accel": 2.0})
    cfg.motors[1].update({"kind": "continuous", "size": 1.0, "max_speed": 1.0, "max_accel": 2.0})
    eng.send_cmd(P.C_SET_LIMITS, 0, args=[0, 0, 0])               # M1: set up as continuous (no limits)
    # M2's board still has taught limits -> it must refuse to free-run
    pose = {"id": "cz", "name": "spin", "type": "pattern", "pattern": "pose", "duration": 30, "fade": 0,
            "params": {"values": [0.5] * cfg.motor_count}}
    eng.save_show({**eng.show, "clips": eng.show["clips"] + [pose]})
    eng.push_config(0)
    eng.push_config(1)
    samples = []

    async def go():
        t = asyncio.create_task(eng.run())
        await asyncio.sleep(0.6)
        eng.arm(True)
        eng.solo("cz")
        await asyncio.sleep(4)
        s0, m2a = node(tx, 0).steps, node(tx, 1).steps
        await asyncio.sleep(2)
        samples.extend([(node(tx, 0).steps - s0) / 2, node(tx, 1).steps - m2a])
        t.cancel()
    asyncio.run(go())
    rate, m2_moved = samples
    assert abs(rate / cfg.spu(0) - 0.5) < 0.05                    # 0.5 rev/s = half of max
    assert abs(m2_moved) < cfg.spu(1) * 0.05                       # limited board ignored velocity mode
    assert abs(eng.out_mm[0] - 0.5) < 0.01                         # screens show the speed


def test_wizard_for_continuous_motor_skips_limits(rig):
    cfg, tx, eng = rig
    fresh = next(n for n in tx.nodes if n.id == P.ID_UNASSIGNED)

    async def idle():
        await asyncio.sleep(0.4)
        for _ in range(300):
            if not eng.wizard or not eng.wizard["busy"]:
                return
            await asyncio.sleep(0.1)
        raise AssertionError("wizard step hung")

    async def go():
        t = asyncio.create_task(eng.run())
        await asyncio.sleep(0.6)
        eng.wiz_start(fresh.mac, cfg.motor_count)
        await idle()
        eng.wiz_mechanism("continuous", 1.0, True)
        await idle()
        assert eng.wizard_snapshot()["steps"] == ["mechanism", "direction", "encoder", "test", "speed", "finish"]
        before = fresh.phys_fs
        eng.wiz_direction_test()
        await idle()
        eng.wiz_direction_answer(fresh.phys_fs > before)
        await idle()
        eng.wiz_encoder_check()
        await idle()
        assert eng.wizard["error"] == "" and eng.wizard["step"] == "test"
        eng.wiz_test()
        await idle()
        assert eng.wizard["test_done"]
        eng.wiz_finish()
        await asyncio.sleep(0.6)
        t.cancel()
    asyncio.run(go())
    m = cfg.motors[-1]
    assert m["kind"] == "continuous" and m["commissioned"] and m["bidirectional"]
    assert "bottom_fs" not in m and not fresh.limits_on()


def test_groups_follow_renumbering(rig):
    cfg, tx, eng = rig
    cfg.data["groups"] = [{"name": "left", "motors": [0, 1]}, {"name": "right", "motors": [2, 3]}]
    _run(eng, 0.6)
    eng.remove_motors([1])
    assert cfg.data["groups"] == [{"name": "left", "motors": [0]}, {"name": "right", "motors": [1, 2]}]
    assert eng.eval_ctx()["groups"] == {"left": [0], "right": [1, 2]}


# ---------------------------------------------------------------- the speed test
def test_speed_test_finds_limits_below_the_stall_and_keeps_position(rig):
    """The simulated motors stall above 600 RPM / 40 rev/s² (like a real stepper with a load).
    The speed test must stop below that, suggest limits with a margin, and leave the motor's
    idea of its position matching where the load really is."""
    cfg, tx, eng = rig
    i = 1
    n1 = node(tx, i)
    full = 250 / (3.141592653589793 * cfg.mech["spool_diameter_mm"]) * cfg.mech["full_steps_per_rev"]
    cfg.motors[i].update({"commissioned": True, "bottom_fs": round(full, 2), "travel_mm": None,
                          "max_speed": 300.0, "max_accel": 1500.0, "node_mac": n1.mac})
    n1.lim = {"on": True, "lo": 0, "hi": int(full * n1.ms), "ms": n1.ms}
    upr = cfg.unit_per_rev(i)
    stall_v = n1.stall_fs / cfg.mech["full_steps_per_rev"] * upr          # mm/s
    stall_a = n1.stall_acc_fs / cfg.mech["full_steps_per_rev"] * upr      # mm/s²

    async def go():
        runner = asyncio.create_task(eng.run())
        await asyncio.sleep(0.6)
        eng.wiz_start(n1.mac, i, speed_only=True)
        await asyncio.sleep(1.0)
        assert eng.wizard["step"] == "speed"
        eng.wiz_speed_test()
        for _ in range(1200):
            await asyncio.sleep(0.1)
            if not eng.wizard["busy"]:
                break
        r = eng.wizard["speed"]
        assert eng.wizard["error"] == "", eng.wizard["error"]
        eng.wiz_speed_save(True)
        eng.wiz_finish()
        await asyncio.sleep(0.6)
        runner.cancel()
        return r

    r = asyncio.run(go())
    assert r["v_ok"] and r["a_ok"]
    assert 300 < r["v_ok"] <= stall_v and r["a_ok"] <= stall_a * 1.05
    assert any(not lv["ok"] for lv in r["levels"])                 # it did find the edge
    assert cfg.motors[i]["max_speed"] == r["v_save"] < r["v_ok"]
    assert cfg.motors[i]["max_accel"] == r["a_save"] < r["a_ok"]
    # after missing steps on purpose, the position was re-synced to the encoder
    believed, true = believed_vs_true(cfg, tx, i)
    assert abs(believed - true) < 0.5, (believed, true)
    assert abs(n1.steps / n1.ms - n1.phys_fs * (1 if not n1.wiring_rev else -1)) < 2   # steps agree with the load
    assert n1.lim_lo() <= n1.steps <= n1.lim_hi()
