"""External triggers: the input decoders, the mapping rules, and what the engine does."""
import asyncio
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kinetic import triggers as T  # noqa: E402
from kinetic.config import Config, default_motors  # noqa: E402
from kinetic.engine import Engine  # noqa: E402
from kinetic.transport import SimTransport  # noqa: E402


@pytest.fixture
def eng(tmp_path):
    cfg = Config(tmp_path / "config.json")
    cfg.data["motors"] = default_motors(2)
    e = Engine(cfg, SimTransport(2, cfg.node_config_args()), tmp_path / "shows")
    e.triggers = T.TriggerManager(e)
    return e


def run(e, seconds, dt=0.02):
    for _ in range(int(seconds / dt)):
        e.tick(dt)


def mapping(e, **m):
    m.setdefault("id", f"m{len(e.triggers.cfg['mappings'])}")
    e.triggers.cfg["mappings"].append(m)
    return m


# ---------------------------------------------------------------- decoders
def test_osc_roundtrip_and_bundles():
    pkt = T.build_osc("/bloom/flap", 1, 0.5, "x", True)
    assert T.parse_osc(pkt) == [("/bloom/flap", [1, 0.5, "x", True])]
    assert T.parse_osc(T.build_osc("/go")) == [("/go", [])]
    a, b = T.build_osc("/a", 1), T.build_osc("/b", 0.25)
    bundle = b"#bundle\0" + b"\0" * 8 + len(a).to_bytes(4, "big") + a + len(b).to_bytes(4, "big") + b
    assert T.parse_osc(bundle) == [("/a", [1]), ("/b", [0.25])]
    assert T.parse_osc(b"garbage") == []
    assert T.osc_value([]) == 1.0 and T.osc_value([0]) == 0.0 and T.osc_value([False]) == 0.0


def test_artdmx_parse():
    pkt = T.build_artdmx(0x0103, bytes([0, 255, 128]))      # net 1, sub-uni 3
    uni, levels = T.parse_artdmx(pkt)
    assert uni == 0x0103 and levels[:3] == bytes([0, 255, 128])
    assert T.parse_artdmx(b"Art-Net\0" + b"\x00\x20" + b"\0" * 10) is None   # OpPoll, not DMX


def test_midi_parser_running_status_and_noise():
    p = T.MidiParser()
    # note on ch1 60 vel 127, running-status note 60 vel 0 (= off), clock byte in the middle,
    # cc 7 on ch 2, program change 5 on ch 1
    evs = p.feed(bytes([0x90, 60, 127, 0xF8, 60, 0, 0xB1, 7, 64, 0xC0, 5]))
    assert evs == [("ch 1 note 60", 1.0), ("ch 1 note 60", 0.0), ("ch 2 cc 7", 64 / 127),
                   ("ch 1 program 5", 1.0), ("ch 1 program 5", 0.0)]
    assert p.feed(bytes([0xF0, 1, 2, 3, 0xF7])) == []           # sysex ignored
    assert p.feed(bytes([0x80, 61])) == [] and p.feed(bytes([10])) == [("ch 1 note 61", 0.0)]  # split read


# ---------------------------------------------------------------- mapping rules
def test_button_fires_once_per_press_with_cooldown(eng):
    mapping(eng, source="gpio", address="pin 17", action="hold", cooldown=0.2)
    eng.play()
    eng.triggers.handle("gpio", "pin 17", 1.0)
    assert eng.mode == "hold"
    eng.play()
    eng.triggers.handle("gpio", "pin 17", 1.0)            # still held down: no new press
    assert eng.mode == "playlist"
    eng.triggers.handle("gpio", "pin 17", 0.0)
    eng.triggers.handle("gpio", "pin 17", 1.0)            # pressed again inside the cooldown
    assert eng.mode == "playlist"


def test_midi_mapping_without_channel_matches_any_channel(eng):
    mapping(eng, source="midi", address="note 60", action="park")
    eng.triggers.handle("midi", "ch 10 note 60", 0.8)
    assert eng.mode == "park"


def test_fader_rides_live_speed_without_saving(eng, tmp_path):
    mapping(eng, source="artnet", address="ch 5", action="speed", lo=0.5, hi=1.5)
    before = (tmp_path / "config.json").stat().st_mtime_ns if (tmp_path / "config.json").exists() else 0
    eng.triggers.handle("artnet", "ch 5", 1.0)
    assert eng.playback["speed"] == 1.5
    eng.triggers.handle("artnet", "ch 5", 0.0)
    assert eng.playback["speed"] == 0.5
    after = (tmp_path / "config.json").stat().st_mtime_ns if (tmp_path / "config.json").exists() else 0
    assert before == after                                 # no disk write per fader move


def test_learn_takes_the_next_input(eng):
    m = mapping(eng, source="osc", address="", action="next")
    eng.triggers.learn = m["id"]
    eng.triggers.handle("midi", "ch 1 cc 20", 1.0)
    assert m["source"] == "midi" and m["address"] == "ch 1 cc 20" and eng.triggers.learn is None
    assert eng.triggers.snapshot()["recent"][0]["address"] == "ch 1 cc 20"


# ---------------------------------------------------------------- engine actions
def test_play_clip_then_resume_playlist_where_it_left_off(eng):
    show = eng.show
    eng.play()
    run(eng, 4.0)
    entry, elapsed = eng.entry_index, eng.vclock - eng.entry_start
    trig = show["clips"][-1]
    mapping(eng, source="osc", address="/flap", action="play_clip", clip=trig["id"], cooldown=0)
    eng.triggers.handle("osc", "/flap", 1.0, momentary=True)
    assert eng.mode == "trigger" and eng.source.clip_id == trig["id"]
    run(eng, float(trig["duration"]) / 2)
    eng.triggers.handle("osc", "/flap", 1.0, momentary=True)   # again mid-way: restarts the clip...
    assert eng.interrupt["resume"]["entry"] == entry            # ...but keeps the first resume point
    run(eng, float(trig["duration"]) + 0.1)
    assert eng.mode == "playlist" and eng.entry_index == entry
    assert abs((eng.vclock - eng.entry_start) - elapsed) < 0.15  # picked up at the same moment


def test_operator_command_cancels_a_triggered_clip(eng):
    eng.play()
    eng.play_once(eng.show["clips"][0]["id"])
    eng.hold()
    assert eng.interrupt is None
    run(eng, 200)
    assert eng.mode == "hold"


def test_switch_show_and_entry(eng):
    eng.save_show({**eng.show, "name": "Evening"})          # a second show (copy of the first)
    eng.load_show("default")
    rev = eng.show_rev
    mapping(eng, source="gpio", address="pin 5", action="switch", show="Evening", entry=2)
    eng.triggers.handle("gpio", "pin 5", 1.0)
    assert eng.show["name"] == "Evening" and eng.show_rev == rev + 1
    assert eng.mode == "playlist" and eng.entry_index == 1
    eng.triggers.handle("gpio", "pin 5", 0.0)
    m = mapping(eng, source="gpio", address="pin 6", action="switch", show="Nope")
    eng.triggers.fire(m)
    assert eng.show["name"] == "Evening"                    # unknown show: stays, with a warning
    assert "no show" in eng.events[-1]["msg"]


def test_osc_over_real_udp_and_artnet_universe_filter(eng):
    free = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    tm = eng.triggers
    tm.cfg["osc"] = {"enabled": True, "port": port}
    tm.cfg["artnet"] = {"enabled": True, "universe": 0}
    mapping(eng, source="osc", address="/bloom/depth", action="depth")
    mapping(eng, source="artnet", address="ch 2", action="park")

    async def go():
        await tm.start()
        assert tm.status["osc"].startswith("listening")
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(T.build_osc("/bloom/depth", 0.0), ("127.0.0.1", port))
        # Art-Net's port is fixed (6454) and may be shared with other software on this machine,
        # which would get the packet instead, so feed it straight to the receiver
        tm._on_artnet(T.build_artdmx(1, bytes([0, 255])))      # another universe: ignored
        assert eng.mode != "park"
        tm._on_artnet(T.build_artdmx(0, bytes([0, 255])))
        await asyncio.sleep(0.3)
        s.close()
        tm.stop()

    asyncio.run(go())
    assert eng.playback["depth"] == 0.1
    assert eng.mode == "park"
