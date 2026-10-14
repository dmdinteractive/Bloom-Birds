"""External triggers: things outside the controller that change what the sculpture does.

Four kinds of input, each optional and switched on in the editor's Triggers tab:

  OSC       UDP messages from TouchOSC, QLab, a show controller... e.g.  /bloom/flap 1
  MIDI      a USB MIDI controller plugged into the Pi (notes, CCs, program changes)
  Art-Net   DMX from a lighting console over the network (one universe, channels 1-512)
  GPIO      buttons or sensors (PIR, beam break) wired to the Pi's header

Every input turns into the same small event:  (source, address, value 0..1)
  osc      "/bloom/flap"        first numeric argument (no argument = 1)
  midi     "ch 1 note 60"       velocity/127 (note off = 0); "ch 1 cc 7" value/127
  artnet   "ch 12"              level/255
  gpio     "pin 17"             1 pressed / 0 released

A *mapping* says what an event does. "Fire" actions (play a clip, switch show, next...)
happen when the value rises past 0.5, so a button fires once per press; "level" actions
(live speed / intensity) follow the value continuously, so a fader or DMX channel rides them.

Inputs run in background threads or as asyncio UDP endpoints; every event is handed to the
asyncio loop (the same thread as the engine), so engine state is only touched from one place.
"""
from __future__ import annotations

import asyncio
import copy
import glob
import logging
import os
import select
import socket
import struct
import threading
import time
from collections import OrderedDict
from pathlib import Path

log = logging.getLogger("kinetic.triggers")

# ------------------------------------------------------------------ what a mapping can do
ACTIONS = {
    "play_clip": {"label": "Play a clip, then resume", "kind": "fire"},
    "switch":    {"label": "Switch show / playlist entry", "kind": "fire"},
    "speed":     {"label": "Live speed", "kind": "level", "lo": 0.25, "hi": 2.0},
    "depth":     {"label": "Live intensity (depth)", "kind": "level", "lo": 0.1, "hi": 1.0},
    "run":       {"label": "Run show (playlist)", "kind": "fire"},
    "next":      {"label": "Next clip", "kind": "fire"},
    "hold":      {"label": "Hold", "kind": "fire"},
    "park":      {"label": "Park (all home)", "kind": "fire"},
}
SOURCES = {"osc": "OSC", "midi": "MIDI", "artnet": "Art-Net / DMX", "gpio": "Pi GPIO"}

DEFAULTS = {
    "osc": {"enabled": False, "port": 9000},
    "midi": {"enabled": False},
    "artnet": {"enabled": False, "universe": 0},
    # wiring: "button" = switch to ground (internal pull-up); "high" / "low" = a sensor's
    # output that is active-high or active-low
    "gpio": {"enabled": False, "pins": []},
    "mappings": [],
}


# ------------------------------------------------------------------ OSC 1.0 encoding
def _osc_str(data: bytes, i: int) -> tuple[str, int]:
    end = data.index(b"\0", i)
    return data[i:end].decode("utf-8", "replace"), (end + 4) & ~3     # null-padded to 4 bytes


def parse_osc(data: bytes) -> list[tuple[str, list]]:
    """An OSC packet -> [(address, [args])]. Bundles are flattened; unknown types end the args."""
    try:
        if data.startswith(b"#bundle\0"):
            out, i = [], 16                                        # "#bundle\0" + 8-byte time tag
            while i + 4 <= len(data):
                n = struct.unpack(">i", data[i:i + 4])[0]
                out += parse_osc(data[i + 4:i + 4 + n])
                i += 4 + n
            return out
        addr, i = _osc_str(data, 0)
        if not addr.startswith("/"):
            return []
        if i >= len(data) or data[i:i + 1] != b",":
            return [(addr, [])]
        tags, i = _osc_str(data, i)
        args: list = []
        for t in tags[1:]:
            if t == "i":
                args.append(struct.unpack(">i", data[i:i + 4])[0]); i += 4
            elif t == "f":
                args.append(struct.unpack(">f", data[i:i + 4])[0]); i += 4
            elif t == "d":
                args.append(struct.unpack(">d", data[i:i + 8])[0]); i += 8
            elif t == "h":
                args.append(struct.unpack(">q", data[i:i + 8])[0]); i += 8
            elif t in "sS":
                v, i = _osc_str(data, i); args.append(v)
            elif t == "b":
                n = struct.unpack(">i", data[i:i + 4])[0]; args.append(data[i + 4:i + 4 + n]); i += 4 + ((n + 3) & ~3)
            elif t == "T":
                args.append(True)
            elif t == "F":
                args.append(False)
            elif t in "NI":
                args.append(None)
            elif t in "mcr":
                args.append(data[i:i + 4]); i += 4
            elif t == "t":
                args.append(data[i:i + 8]); i += 8
            else:
                break
        return [(addr, args)]
    except (ValueError, struct.error, IndexError):
        return []


def build_osc(address: str, *args) -> bytes:
    """Encode one OSC message (ints, floats, strings, bools). Used by the tests and the docs."""
    def s(x: str) -> bytes:
        b = x.encode() + b"\0"
        return b + b"\0" * (-len(b) % 4)
    tags, body = ",", b""
    for a in args:
        if isinstance(a, bool):
            tags += "T" if a else "F"
        elif isinstance(a, int):
            tags += "i"; body += struct.pack(">i", a)
        elif isinstance(a, float):
            tags += "f"; body += struct.pack(">f", a)
        else:
            tags += "s"; body += s(str(a))
    return s(address) + s(tags) + body


def osc_value(args: list) -> float:
    for a in args:
        if isinstance(a, bool):
            return 1.0 if a else 0.0
        if isinstance(a, (int, float)):
            return float(a)
        if isinstance(a, str):
            try:
                return float(a)
            except ValueError:
                continue
    return 1.0


# ------------------------------------------------------------------ Art-Net (ArtDmx)
ARTNET_PORT = 6454


def parse_artdmx(data: bytes) -> tuple[int, bytes] | None:
    """ArtDmx packet -> (15-bit port-address, channel levels) or None for anything else."""
    if len(data) < 18 or data[:8] != b"Art-Net\0":
        return None
    if struct.unpack("<H", data[8:10])[0] != 0x5000:              # OpDmx
        return None
    universe = struct.unpack("<H", data[14:16])[0] & 0x7FFF        # SubUni | Net << 8
    n = struct.unpack(">H", data[16:18])[0]
    return universe, data[18:18 + min(n, 512)]


def build_artdmx(universe: int, levels: bytes, seq: int = 0) -> bytes:
    levels = bytes(levels[:512])
    if len(levels) % 2:
        levels += b"\0"
    return (b"Art-Net\0" + struct.pack("<H", 0x5000) + bytes([0, 14, seq & 0xFF, 0])
            + struct.pack("<H", universe & 0x7FFF) + struct.pack(">H", len(levels)) + levels)


# ------------------------------------------------------------------ MIDI bytes
class MidiParser:
    """Raw MIDI byte stream (with running status) -> [(address, value 0..1)]."""

    def __init__(self):
        self.status = 0
        self.buf: list[int] = []

    def feed(self, data: bytes) -> list[tuple[str, float]]:
        out = []
        for b in data:
            if b >= 0xF8:                         # clock, start/stop, active sensing
                continue
            if b >= 0x80:
                self.status = b if b < 0xF0 else 0   # system messages / sysex: ignore their data
                self.buf = []
                continue
            if not self.status:
                continue
            self.buf.append(b)
            kind, ch = self.status & 0xF0, (self.status & 0x0F) + 1
            if len(self.buf) < (1 if kind in (0xC0, 0xD0) else 2):
                continue
            d, self.buf = self.buf, []
            if kind == 0x90:
                out.append((f"ch {ch} note {d[0]}", d[1] / 127))      # velocity 0 = note off
            elif kind == 0x80:
                out.append((f"ch {ch} note {d[0]}", 0.0))
            elif kind == 0xB0:
                out.append((f"ch {ch} cc {d[0]}", d[1] / 127))
            elif kind == 0xC0:                    # a program change is a momentary press
                out += [(f"ch {ch} program {d[0]}", 1.0), (f"ch {ch} program {d[0]}", 0.0)]
            elif kind == 0xE0:
                out.append((f"ch {ch} pitchbend", ((d[1] << 7) | d[0]) / 16383))
        return out


def midi_devices() -> list[dict]:
    """USB MIDI ports the Pi can read (ALSA raw MIDI: /dev/snd/midiC<card>D<device>)."""
    out = []
    for path in sorted(glob.glob("/dev/snd/midiC*D*")):
        card = Path(path).name[5:].split("D")[0]
        try:
            name = Path(f"/proc/asound/card{card}/id").read_text().strip()
        except OSError:
            name = f"card {card}"
        out.append({"path": path, "name": name})
    return out


# ------------------------------------------------------------------ the manager
class TriggerManager:
    def __init__(self, engine):
        self.engine = engine
        self.loop: asyncio.AbstractEventLoop | None = None
        self.recent: OrderedDict = OrderedDict()   # (source, address) -> {value, t}; newest last
        self.learn: str | None = None              # mapping id waiting for the next input
        self.status: dict[str, str] = {k: "off" for k in SOURCES}
        self._level: dict[str, float] = {}         # last value per mapping (edge detection)
        self._fired: dict[str, float] = {}         # last fire time per mapping (cooldown)
        self._udp: list[asyncio.DatagramTransport] = []
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._gpio: list = []
        self._dmx_prev: bytes = b""

    # ---- settings (live in config.json under "triggers")
    @property
    def cfg(self) -> dict:
        t = self.engine.cfg.data.setdefault("triggers", copy.deepcopy(DEFAULTS))
        for k, v in DEFAULTS.items():
            t.setdefault(k, copy.deepcopy(v))
        return t

    def update(self, new: dict):
        t = self.cfg
        for k in DEFAULTS:
            if k in new:
                t[k] = new[k]
        for m in t["mappings"]:
            m.setdefault("id", os.urandom(4).hex())
            m.setdefault("enabled", True)
        self.engine.cfg.save()

    # ---- start / stop inputs
    async def start(self):
        self.loop = asyncio.get_running_loop()
        await self.apply()

    async def apply(self):
        """(Re)start the inputs to match the settings."""
        self.stop()
        self._stop = threading.Event()
        self.loop = self.loop or asyncio.get_running_loop()
        c = self.cfg
        if c["osc"].get("enabled"):
            await self._start_udp("osc", int(c["osc"].get("port", 9000)), self._on_osc)
        if c["artnet"].get("enabled"):
            self._dmx_prev = b""
            await self._start_udp("artnet", ARTNET_PORT, self._on_artnet)
        if c["midi"].get("enabled"):
            self._start_midi()
        if c["gpio"].get("enabled"):
            self._start_gpio(c["gpio"].get("pins", []))

    def stop(self):
        self._stop.set()
        for tr in self._udp:
            tr.close()
        self._udp = []
        for b in self._gpio:
            try:
                b.close()
            except Exception:  # noqa: BLE001
                pass
        self._gpio = []
        for th in self._threads:
            th.join(timeout=2)
        self._threads = []
        self.status = {k: "off" for k in SOURCES}

    async def _start_udp(self, name: str, port: int, cb):
        mgr = self

        class Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                cb(data)

            def error_received(self, exc):
                log.debug("%s socket error: %s", name, exc)

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):     # share Art-Net's fixed port with other software
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.bind(("0.0.0.0", port))
            tr, _ = await self.loop.create_datagram_endpoint(Proto, sock=sock)
            mgr._udp.append(tr)
            mgr.status[name] = f"listening on UDP {port}"
        except OSError as e:
            mgr.status[name] = f"error: can't open UDP port {port} ({e.strerror or e})"
            mgr.engine.event(f"{SOURCES[name]} input: {mgr.status[name]}", "warn")

    def _on_osc(self, data: bytes):
        for addr, args in parse_osc(data):
            self.handle("osc", addr, osc_value(args), momentary=True)

    def _on_artnet(self, data: bytes):
        p = parse_artdmx(data)
        if not p or p[0] != int(self.cfg["artnet"].get("universe", 0)):
            return
        levels, prev = p[1], self._dmx_prev
        for i, v in enumerate(levels):
            if v != (prev[i] if i < len(prev) else 0):
                self.handle("artnet", f"ch {i + 1}", v / 255)
        self._dmx_prev = bytes(levels)

    # ---- MIDI: ALSA raw MIDI on the Pi (no extra libraries); mido on a Mac if installed
    def _start_midi(self):
        if os.path.isdir("/dev/snd"):
            th = threading.Thread(target=self._midi_thread, name="midi-in", daemon=True)
            th.start()
            self._threads.append(th)
            self.status["midi"] = "looking for a USB MIDI device"
            return
        try:
            import mido  # type: ignore
        except ImportError:
            self.status["midi"] = "not available on this computer (Pi only; on a Mac: pip install mido python-rtmidi)"
            return
        names = mido.get_input_names()
        for nm in names:
            port = mido.open_input(nm, callback=lambda msg, nm=nm: self._from_thread(
                lambda: [self.handle("midi", a, v) for a, v in MidiParser().feed(bytes(msg.bytes()))]))
            self._gpio.append(port)                         # closed with the GPIO handles
        self.status["midi"] = f"listening: {', '.join(names)}" if names else "no MIDI device found"

    def _midi_thread(self):
        fds: dict[str, tuple[int, MidiParser, str]] = {}
        last_scan = 0.0
        while not self._stop.is_set():
            if time.monotonic() - last_scan > 2.0:          # hot-plug: look again every 2 s
                last_scan = time.monotonic()
                devs = midi_devices()
                for d in devs:
                    if d["path"] not in fds:
                        try:
                            fds[d["path"]] = (os.open(d["path"], os.O_RDONLY | os.O_NONBLOCK), MidiParser(), d["name"])
                        except OSError as e:
                            self._set_status("midi", f"error: can't open {d['path']} ({e.strerror}); "
                                                     "is the user in the 'audio' group?")
                names = [v[2] for v in fds.values()]
                if fds:
                    self._set_status("midi", f"listening: {', '.join(names)}")
                elif not self.status["midi"].startswith("error"):
                    self._set_status("midi", "no USB MIDI device found (plug one in)")
            if not fds:
                self._stop.wait(0.5)
                continue
            ready, _, _ = select.select([v[0] for v in fds.values()], [], [], 0.5)
            for path, (fd, parser, _nm) in list(fds.items()):
                if fd not in ready:
                    continue
                try:
                    data = os.read(fd, 256)
                except OSError:
                    data = b""
                if not data:                                # unplugged
                    os.close(fd)
                    del fds[path]
                    last_scan = 0.0
                    continue
                evs = parser.feed(data)
                if evs:
                    self._from_thread(lambda evs=evs: [self.handle("midi", a, v) for a, v in evs])
        for fd, _p, _n in fds.values():
            os.close(fd)

    # ---- GPIO: gpiozero (preinstalled on Raspberry Pi OS)
    def _start_gpio(self, pins: list[dict]):
        try:
            from gpiozero import Button  # type: ignore
        except Exception:  # noqa: BLE001
            self.status["gpio"] = ("not available: gpiozero isn't installed here (on the Pi, re-run "
                                   "deploy/install_pi.sh)")
            return
        ok = []
        for p in pins:
            pin, wiring = int(p.get("pin", 0)), p.get("wiring", "button")
            try:
                if wiring == "button":
                    b = Button(pin, pull_up=True, bounce_time=0.03)
                else:
                    b = Button(pin, pull_up=None, active_state=(wiring == "high"), bounce_time=0.03)
            except Exception as e:  # noqa: BLE001
                self.engine.event(f"GPIO {pin}: {e}", "warn")
                continue
            addr = f"pin {pin}"
            b.when_pressed = lambda a=addr: self._from_thread(lambda: self.handle("gpio", a, 1.0))
            b.when_released = lambda a=addr: self._from_thread(lambda: self.handle("gpio", a, 0.0))
            self._gpio.append(b)
            ok.append(str(pin))
        self.status["gpio"] = f"watching GPIO {', '.join(ok)}" if ok else "no pins set up"

    def _set_status(self, name: str, text: str):
        self._from_thread(lambda: self.status.__setitem__(name, text))

    def _from_thread(self, fn):
        if self.loop and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(fn)

    # ---- events -> actions
    def handle(self, source: str, address: str, value: float, momentary: bool = False):
        """One input event. `momentary` (OSC): every message counts as a fresh press."""
        value = float(value)
        key = (source, address)
        self.recent.pop(key, None)
        self.recent[key] = {"value": round(value, 3), "t": time.time()}
        while len(self.recent) > 12:
            self.recent.popitem(last=False)
        if self.learn:
            m = self._mapping(self.learn)
            self.learn = None
            if m is not None:
                m["source"], m["address"] = source, address
                self.engine.cfg.save()
                self.engine.event(f"Trigger \"{m.get('name') or 'mapping'}\" learned {SOURCES[source]} {address}")
            return
        for m in self.cfg["mappings"]:
            if m.get("enabled", True) and self._matches(m, source, address):
                self._apply(m, value, momentary)

    @staticmethod
    def _matches(m: dict, source: str, address: str) -> bool:
        if m.get("source") != source:
            return False
        want, got = str(m.get("address", "")).strip().lower(), address.lower()
        if not want:
            return False
        if want == got:
            return True
        # MIDI mappings may leave the channel off ("note 60" = that note on any channel)
        return source == "midi" and got.startswith("ch ") and got.split(" ", 2)[2] == want

    def _apply(self, m: dict, value: float, momentary: bool):
        act = ACTIONS.get(m.get("action"))
        if not act:
            return
        mid = m["id"]
        if act["kind"] == "level":
            lo = float(m.get("lo", act["lo"]))
            hi = float(m.get("hi", act["hi"]))
            v = lo + max(0.0, min(1.0, value)) * (hi - lo)
            self.engine.set_playback(**{m["action"]: v}, save=False)
            return
        prev = 0.0 if momentary else self._level.get(mid, 0.0)
        self._level[mid] = value
        if not (value >= 0.5 > prev):
            return
        now = time.monotonic()
        if now - self._fired.get(mid, -1e9) < float(m.get("cooldown", 0.5)):
            return
        self._fired[mid] = now
        self.fire(m)

    def fire(self, m: dict):
        e, a = self.engine, m.get("action")
        if a == "play_clip":
            e.play_once(m.get("clip", ""), int(m.get("times", 1) or 1))
        elif a == "switch":
            e.switch_show(m.get("show") or None, max(0, int(m.get("entry", 1) or 1) - 1))
        elif a == "run":
            e.play()
        elif a == "next":
            e.next()
        elif a == "hold":
            e.hold()
        elif a == "park":
            e.park()
        elif a in ("speed", "depth"):
            self._apply(m, 1.0, True)

    def _mapping(self, mid: str) -> dict | None:
        return next((m for m in self.cfg["mappings"] if m.get("id") == mid), None)

    def test(self, mid: str):
        m = self._mapping(mid)
        if m:
            self.engine.event(f"Testing trigger \"{m.get('name') or ACTIONS.get(m.get('action'), {}).get('label', '')}\"")
            self.fire(m)

    def snapshot(self) -> dict:
        now = time.time()
        return {"status": self.status, "learn": self.learn,
                "recent": [{"source": s, "address": a, "value": r["value"], "age": round(now - r["t"], 1)}
                           for (s, a), r in reversed(self.recent.items())]}
