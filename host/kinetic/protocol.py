"""Python mirror of firmware/*/ks_protocol.h.

Keep the two in sync: tests/test_protocol.py checks struct sizes against the header.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = b"KS"
VERSION = 3
MAX_MOTORS = 16
ID_UNASSIGNED = 0xFF
TARGET_ALL = 0xFF

T_FRAME, T_CMD, T_STATUS, T_BRIDGE = 1, 2, 3, 16
FF_ENABLE = 0x01
POS_VELOCITY = -(2**31)          # KsMotorTarget.pos sentinel: continuous rotation, run at vel

C_ENABLE, C_HOME, C_SET_ZERO, C_JOG, C_STOP, C_IDENTIFY, C_SET_ID, C_CONFIG, C_REBOOT, C_SET_LIMITS, C_RESTORE = range(1, 12)
CFG_COUNT = 12

S_FLAGS = {
    "enabled": 0x0001,
    "homed": 0x0002,
    "homing": 0x0004,
    "moving": 0x0008,
    "link_lost": 0x0010,
    "power_good": 0x0020,
    "limit": 0x0040,
    "home_failed": 0x0080,
    "tmc_ok": 0x0100,
    "overtemp": 0x0200,
    "slip": 0x0400,
    "configured": 0x0800,
    "streaming": 0x1000,
    "unassigned": 0x2000,
}

# KsStatus.info bits
I_FLAGS = {
    "calibrated": 0x01,
    "limits": 0x02,
    "rec_clean": 0x04,
    "restored": 0x08,
    "restored_host": 0x10,
    "pos_lost": 0x20,
    "pf_saved": 0x40,
}
# KsStatus.magnet bits
M_DETECTED, M_WEAK, M_STRONG, M_READ_OK = 0x01, 0x02, 0x04, 0x80

SER_START = 0x7E

_HDR = "<2sBB"
_STATUS = struct.Struct("<2sBB BBH iii HHH I BBii")
_BRIDGE = struct.Struct("<2sBB 6sBB III")
_CMD = struct.Struct("<2sBB B6sB" + "i" * CFG_COUNT)
_TARGET = struct.Struct("<ii")
INT32_MIN = -(2**31)


def header(ptype: int) -> bytes:
    return struct.pack(_HDR, MAGIC, VERSION, ptype)


def pack_frame(seq: int, flags: int, targets: list[tuple[int, int]]) -> bytes:
    """targets[i] = (position microsteps, velocity microsteps/s) for motor id i."""
    if len(targets) > MAX_MOTORS:
        raise ValueError("too many motors")
    body = struct.pack("<HBB", seq & 0xFFFF, flags, len(targets))
    body += b"".join(_TARGET.pack(p if p == POS_VELOCITY else _clamp32(p), _clamp32(v)) for p, v in targets)
    return header(T_FRAME) + body


def pack_cmd(cmd: int, target: int = TARGET_ALL, mac: bytes | None = None, args: list[int] | None = None) -> bytes:
    a = list(args or [])
    a += [0] * (CFG_COUNT - len(a))
    return _CMD.pack(MAGIC, VERSION, T_CMD, target, mac or bytes(6), cmd, *[_clamp32(x) for x in a[:CFG_COUNT]])


@dataclass
class Status:
    id: int
    fw: int
    flags: int
    pos: int
    target: int
    enc: int | None
    vbus_mv: int
    sg: int
    last_seq: int
    uptime_s: int
    magnet: int = 0
    info: int = 0
    lim_min: int = 0
    lim_max: int = 0

    def has(self, name: str) -> bool:
        if name in I_FLAGS:
            return bool(self.info & I_FLAGS[name])
        return bool(self.flags & S_FLAGS[name])

    def flag_names(self) -> list[str]:
        return [k for k, v in S_FLAGS.items() if self.flags & v] + \
               [k for k, v in I_FLAGS.items() if self.info & v]

    @property
    def magnet_state(self) -> str:
        """ok | none | weak | strong | unknown"""
        if not self.magnet & M_READ_OK:
            return "unknown"
        if not self.magnet & M_DETECTED:
            return "none"
        if self.magnet & M_WEAK:
            return "weak"
        if self.magnet & M_STRONG:
            return "strong"
        return "ok"


@dataclass
class BridgeInfo:
    mac: str
    channel: int
    fw: int
    tx_ok: int
    tx_fail: int
    rx_count: int


def mac_str(b: bytes) -> str:
    return ":".join(f"{x:02X}" for x in b)


def mac_bytes(s: str) -> bytes:
    return bytes(int(x, 16) for x in s.split(":"))


def parse_status(pkt: bytes) -> Status | None:
    if len(pkt) < _STATUS.size or pkt[:2] != MAGIC or pkt[3] != T_STATUS:
        return None
    if pkt[2] != VERSION:
        return None          # firmware from an older/newer protocol: ignore rather than misread
    (_m, _v, _t, sid, fw, flags, pos, target, enc, vbus, sg, seq, up, mag, info, lo, hi) = _STATUS.unpack_from(pkt)
    return Status(sid, fw, flags, pos, target, None if enc == INT32_MIN else enc, vbus, sg, seq, up,
                  mag, info, lo, hi)


def parse_bridge(pkt: bytes) -> BridgeInfo | None:
    if len(pkt) < _BRIDGE.size or pkt[:2] != MAGIC or pkt[3] != T_BRIDGE:
        return None
    (_m, _v, _t, mac, ch, fw, ok, fail, rx) = _BRIDGE.unpack_from(pkt)
    return BridgeInfo(mac_str(mac), ch, fw, ok, fail, rx)


def pack_status(st: Status) -> bytes:
    """Used by the simulator to produce realistic packets."""
    enc = INT32_MIN if st.enc is None else _clamp32(st.enc)
    return _STATUS.pack(MAGIC, VERSION, T_STATUS, st.id, st.fw, st.flags, _clamp32(st.pos),
                        _clamp32(st.target), enc, st.vbus_mv, st.sg, st.last_seq & 0xFFFF, st.uptime_s,
                        st.magnet & 0xFF, st.info & 0xFF, _clamp32(st.lim_min), _clamp32(st.lim_max))


def unpack_cmd(pkt: bytes):
    (_m, _v, _t, target, mac, cmd, *args) = _CMD.unpack_from(pkt)
    return target, mac, cmd, list(args)


def unpack_frame(pkt: bytes):
    seq, flags, count = struct.unpack_from("<HBB", pkt, 4)
    targets = [_TARGET.unpack_from(pkt, 8 + i * 8) for i in range(count)]
    return seq, flags, targets


# ---- serial framing ----
def crc8(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def ser_frame(payload: bytes) -> bytes:
    if not 0 < len(payload) <= 255:
        raise ValueError("payload size")
    return bytes([SER_START, len(payload)]) + payload + bytes([crc8(payload)])


class SerialDeframer:
    """Incremental parser for [0x7E][len][payload][crc8]."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        out = []
        while True:
            i = self.buf.find(bytes([SER_START]))
            if i < 0:
                self.buf.clear()
                return out
            del self.buf[:i]
            if len(self.buf) < 2:
                return out
            n = self.buf[1]
            if n == 0 or not self._plausible(n):
                del self.buf[:1]   # corrupted start/length byte: resync right away
                continue
            if len(self.buf) < n + 3:
                return out
            payload = bytes(self.buf[2:2 + n])
            if crc8(payload) == self.buf[2 + n]:
                out.append(payload)
                del self.buf[:n + 3]
            else:
                del self.buf[:1]   # resync


    def _plausible(self, n: int) -> bool:
        """Every real payload is a KS packet, or [mac 6][rssi][KS packet]."""
        avail = self.buf[2:2 + n]
        if len(avail) >= 2 and avail[:2] == MAGIC:
            return True
        if n < 9:
            return len(avail) < 2          # too short to be wrapped; must start with KS
        if len(avail) >= 9:
            return avail[7:9] == MAGIC
        return True                        # not enough bytes to decide yet


def _clamp32(x: float) -> int:
    x = int(round(x))
    return max(-(2**31) + 1, min(2**31 - 1, x))


STATUS_SIZE = _STATUS.size
CMD_SIZE = _CMD.size
BRIDGE_SIZE = _BRIDGE.size
