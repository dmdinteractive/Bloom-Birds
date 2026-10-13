import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "host"))

from kinetic import protocol as P  # noqa: E402


def test_headers_identical_in_both_sketches():
    a = (ROOT / "firmware/node/ks_protocol.h").read_text()
    b = (ROOT / "firmware/bridge/ks_protocol.h").read_text()
    assert a == b, "copy firmware/node/ks_protocol.h over firmware/bridge/ks_protocol.h"


def test_constants_match_header():
    h = (ROOT / "firmware/node/ks_protocol.h").read_text()

    def enum_val(name):
        m = re.search(rf"\b{name}\s*=\s*(0x[0-9A-Fa-f]+|\d+)", h)
        return int(m.group(1), 0)

    assert enum_val("KS_T_FRAME") == P.T_FRAME
    assert enum_val("KS_T_STATUS") == P.T_STATUS
    assert enum_val("KS_C_CONFIG") == P.C_CONFIG
    assert enum_val("KS_CFG_COUNT") == P.CFG_COUNT
    for name, bit in P.S_FLAGS.items():
        key = {"overtemp": "TMC_OVERTEMP"}.get(name, name.upper())
        assert enum_val("KS_S_" + key) == bit, name


def test_struct_sizes():
    # C layout (packed): header 4 + fields
    assert P.STATUS_SIZE == 4 + 1 + 1 + 2 + 4 * 3 + 2 * 3 + 4 + 1 + 1 + 4 * 2
    assert P.CMD_SIZE == 4 + 1 + 6 + 1 + 4 * 12
    assert P.BRIDGE_SIZE == 4 + 6 + 1 + 1 + 4 * 3


def test_struct_sizes_match_compiled_header(tmp_path):
    """Compile the real C header and compare sizeof() with the Python structs."""
    import shutil
    import subprocess
    import pytest
    cxx = shutil.which("g++") or shutil.which("clang++")
    if not cxx:
        pytest.skip("no C++ compiler")
    src = tmp_path / "sz.cpp"
    src.write_text('#include <cstdio>\n#include "ks_protocol.h"\nint main(){printf("%zu %zu %zu",'
                   'sizeof(KsStatus),sizeof(KsCmd),sizeof(KsBridgeInfo));}')
    exe = tmp_path / "sz"
    subprocess.run([cxx, "-I", str(ROOT / "firmware/node"), str(src), "-o", str(exe)], check=True)
    sizes = [int(x) for x in subprocess.run([str(exe)], capture_output=True, text=True).stdout.split()]
    assert sizes == [P.STATUS_SIZE, P.CMD_SIZE, P.BRIDGE_SIZE]


def test_v2_constants_match_header():
    h = (ROOT / "firmware/node/ks_protocol.h").read_text()

    def val(name):
        return int(re.search(rf"\b{name}\s*=\s*(0x[0-9A-Fa-f]+|\d+)", h).group(1), 0)

    assert int(re.search(r"KS_PROTO_VERSION (\d+)", h).group(1)) == P.VERSION
    assert val("KS_C_SET_LIMITS") == P.C_SET_LIMITS and val("KS_C_RESTORE") == P.C_RESTORE
    for name, bit in P.I_FLAGS.items():
        assert val("KS_I_" + name.upper()) == bit, name
    assert (val("KS_M_DETECTED"), val("KS_M_WEAK"), val("KS_M_STRONG"), val("KS_M_READ_OK")) == \
        (P.M_DETECTED, P.M_WEAK, P.M_STRONG, P.M_READ_OK)


def test_old_firmware_status_is_ignored():
    st = P.Status(1, 1, 0, 0, 0, 0, 0, 0, 0, 0)
    pkt = bytearray(P.pack_status(st))
    pkt[2] = 1                       # protocol v1 node
    assert P.parse_status(bytes(pkt)) is None
    assert len(P.pack_frame(1, 1, [(0, 0)] * 16)) == 4 + 4 + 16 * 8 <= 250


def test_frame_roundtrip():
    targets = [(i * 1000 - 3, -i * 7) for i in range(10)]
    pkt = P.pack_frame(65537, P.FF_ENABLE, targets)
    seq, flags, got = P.unpack_frame(pkt)
    assert seq == 1 and flags == P.FF_ENABLE and got == targets


def test_status_roundtrip():
    st = P.Status(3, 1, P.S_FLAGS["homed"] | P.S_FLAGS["enabled"], 1234, 1300, None, 20000, 55, 9, 77)
    back = P.parse_status(P.pack_status(st))
    assert back == st and back.has("homed") and not back.has("slip")


def test_serial_framing_resyncs_after_garbage():
    d = P.SerialDeframer()
    p1, p2 = b"KS\x01\x03hello", b"KS\x01\x10world!"
    p3 = b"KS\x01\x03more"
    # garbage that even mimics a long wrapped header, then a continuous stream of traffic
    stream = b"\x00\x7e\xff junk" + P.ser_frame(p1) + b"\x7e\x05\x01\x02" + P.ser_frame(p2) + P.ser_frame(p3) * 40
    out = []
    for i in range(0, len(stream), 3):      # arrives in small chunks
        out += d.feed(stream[i:i + 3])
    assert out[:2] == [p1, p2]              # nothing lost after resync
    assert out[2:] == [p3] * 40


def test_cmd_targets_by_mac():
    pkt = P.pack_cmd(P.C_SET_ID, mac=P.mac_bytes("AA:BB:CC:00:11:22"), args=[4])
    target, mac, cmd, args = P.unpack_cmd(pkt)
    assert P.mac_str(mac) == "AA:BB:CC:00:11:22" and cmd == P.C_SET_ID and args[0] == 4
