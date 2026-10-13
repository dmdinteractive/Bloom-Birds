// ks_protocol.h — Kinetic Sculpture wire protocol (ESP-NOW + USB serial)
//
// THIS FILE MUST BE IDENTICAL in firmware/node/ and firmware/bridge/.
// The Python side mirrors it in host/kinetic/protocol.py.
//
// All multi-byte values are little-endian (native on ESP32).
// Every ESP-NOW packet starts with KsHeader. Max ESP-NOW v1 payload is 250 bytes.
//
// Units on the wire are microsteps (positions) and microsteps/second (velocity).
// The Pi converts millimetres of cable to microsteps; the motors never need to
// know spool sizes.

#pragma once
#include <stdint.h>

#define KS_MAGIC0 'K'
#define KS_MAGIC1 'S'
#define KS_PROTO_VERSION 2   // v2: position memory, soft limits, setup data in status

#define KS_MAX_MOTORS 16      // frame packet holds up to 16 motors (136 bytes)
#define KS_ID_UNASSIGNED 0xFF
#define KS_TARGET_ALL 0xFF

// Packet types
enum : uint8_t {
  KS_T_FRAME = 1,   // Pi -> all nodes: synchronized position/velocity for every motor
  KS_T_CMD = 2,     // Pi -> one node (by id or MAC) or all nodes
  KS_T_STATUS = 3,  // node -> Pi
  KS_T_BRIDGE = 16  // bridge -> Pi (serial only): bridge heartbeat
};

// Frame flags
enum : uint8_t {
  KS_FF_ENABLE = 0x01,  // motors may move; if clear, nodes decelerate and hold
};

// Commands
enum : uint8_t {
  KS_C_ENABLE = 1,    // a[0]=1 energize driver, 0 = release
  KS_C_HOME = 2,      // run homing routine using stored config
  KS_C_SET_ZERO = 3,  // a[0]=position (microsteps) to assign to the current spot; marks homed
  KS_C_JOG = 4,       // a[0]=relative microsteps, a[1]=speed (microsteps/s). Ignored while streaming.
  KS_C_STOP = 5,      // decelerate to a stop, cancel homing/jog
  KS_C_IDENTIFY = 6,  // blink LEDs for 3 s
  KS_C_SET_ID = 7,    // a[0]=new motor id (0..15), or 0xFF to release. Must be addressed by MAC.
  KS_C_CONFIG = 8,    // see KsConfig index list below; saved to flash on the node
  KS_C_REBOOT = 9,
  KS_C_SET_LIMITS = 10, // a[0]=1 enable / 0 disable, a[1]=min, a[2]=max (microsteps). Saved on the node.
  KS_C_RESTORE = 11,    // a[0]=approximate position (microsteps) from the Pi's memory. Node snaps it to the
                        // exact spot using its encoder calibration. Only accepted while position is unknown.
};

// KS_C_JOG: a[2]=1 ignores the soft limits (setup wizard only, while teaching the limits).

// KS_C_CONFIG argument layout (a[0]..a[11])
enum : uint8_t {
  KS_CFG_RUN_CURRENT = 0,   // % of driver max (0-100)
  KS_CFG_HOLD_CURRENT = 1,  // % (0-100)
  KS_CFG_MICROSTEPS = 2,    // 1..256
  KS_CFG_MAX_SPEED = 3,     // microsteps/s (node-side safety cap)
  KS_CFG_MAX_ACCEL = 4,     // microsteps/s^2 (node-side tracking accel, set ~2x host limit)
  KS_CFG_FLAGS = 5,         // bit0 invert motor dir, bit1 invert encoder, bit2 spreadCycle instead of stealthChop
  KS_CFG_HOME_MODE = 6,     // 0 none (use SET_ZERO), 1 limit switch on AUX2, 2 StallGuard,
                            // 3 memory: no switch; position remembered across power cycles (encoder + saved turn count)
  KS_CFG_HOME_SPEED = 7,    // microsteps/s (always winds IN, toward negative)
  KS_CFG_HOME_TIMEOUT = 8,  // ms
  KS_CFG_HOME_BACKOFF = 9,  // microsteps to pay out after the home trigger (becomes position 0)
  KS_CFG_STALL_THRESH = 10, // StallGuard threshold 0-255 (higher = more sensitive)
  KS_CFG_PD_VOLTS = 11,     // USB-PD request: 5, 9, 12, 15 or 20
  KS_CFG_COUNT = 12
};

// Status flags (node -> Pi)
enum : uint16_t {
  KS_S_ENABLED = 0x0001,
  KS_S_HOMED = 0x0002,
  KS_S_HOMING = 0x0004,
  KS_S_MOVING = 0x0008,
  KS_S_LINK_LOST = 0x0010,   // was streaming, frames stopped arriving
  KS_S_POWER_GOOD = 0x0020,  // USB-PD contract OK
  KS_S_LIMIT = 0x0040,       // limit switch currently pressed
  KS_S_HOME_FAILED = 0x0080,
  KS_S_TMC_OK = 0x0100,      // driver answering on UART
  KS_S_TMC_OVERTEMP = 0x0200,
  KS_S_SLIP = 0x0400,        // encoder disagrees with step count
  KS_S_CONFIGURED = 0x0800,  // has a saved config
  KS_S_STREAMING = 0x1000,   // following frame stream
  KS_S_UNASSIGNED = 0x2000,  // no motor id yet
};

// KsStatus.info bits (position-memory and setup state)
enum : uint8_t {
  KS_I_CALIBRATED = 0x01,   // top has been taught: encoder angle <-> position relation is stored
  KS_I_LIMITS = 0x02,       // soft limits are set and enforced
  KS_I_REC_CLEAN = 0x04,    // saved position record is valid (motor at rest, or saved at power loss)
  KS_I_RESTORED = 0x08,     // position was recovered from the node's own memory at power-up
  KS_I_RESTORED_HOST = 0x10,// position was recovered from the Pi's memory (KS_C_RESTORE)
  KS_I_POS_LOST = 0x20,     // calibrated, but the saved position was not trustworthy at power-up
  KS_I_PF_SAVED = 0x40,     // a power-loss save happened since boot (supply dipped and came back)
};

// KsStatus.magnet bits (AS5600 STATUS register)
enum : uint8_t {
  KS_M_DETECTED = 0x01,
  KS_M_WEAK = 0x02,
  KS_M_STRONG = 0x04,
  KS_M_READ_OK = 0x80,
};

#pragma pack(push, 1)

struct KsHeader {
  uint8_t magic0;   // 'K'
  uint8_t magic1;   // 'S'
  uint8_t version;  // KS_PROTO_VERSION
  uint8_t type;     // KS_T_*
};

struct KsMotorTarget {
  int32_t pos;  // microsteps, 0 = fully wound in (home)
  int32_t vel;  // microsteps/s feed-forward
};

struct KsFrame {
  KsHeader h;
  uint16_t seq;
  uint8_t flags;  // KS_FF_*
  uint8_t count;  // number of KsMotorTarget entries; entry index == motor id
  KsMotorTarget m[KS_MAX_MOTORS];
};

struct KsCmd {
  KsHeader h;
  uint8_t target;     // motor id, or KS_TARGET_ALL. Ignored when mac is non-zero.
  uint8_t mac[6];     // all zeros = address by target id
  uint8_t cmd;        // KS_C_*
  int32_t a[KS_CFG_COUNT];
};

struct KsStatus {
  KsHeader h;
  uint8_t id;         // motor id or KS_ID_UNASSIGNED
  uint8_t fw;         // firmware version
  uint16_t flags;     // KS_S_*
  int32_t pos;        // microsteps the driver has actually been stepped to
  int32_t target;     // current target
  int32_t enc;        // encoder position converted to microsteps (0 at home)
  uint16_t vbus_mv;   // supply voltage
  uint16_t sg;        // StallGuard result
  uint16_t last_seq;  // last frame seq applied
  uint32_t uptime_s;
  uint8_t magnet;     // KS_M_*
  uint8_t info;       // KS_I_*
  int32_t lim_min;    // soft limits (microsteps), valid when info & KS_I_LIMITS
  int32_t lim_max;
};

struct KsBridgeInfo {   // serial only
  KsHeader h;
  uint8_t mac[6];
  uint8_t channel;
  uint8_t fw;
  uint32_t tx_ok;
  uint32_t tx_fail;
  uint32_t rx_count;
};

#pragma pack(pop)

// Size of a frame packet carrying n motors
static inline int ksFrameSize(int n) {
  return (int)(sizeof(KsHeader) + 4 + n * sizeof(KsMotorTarget));
}

static inline bool ksHeaderOk(const uint8_t *d, int len) {
  return len >= (int)sizeof(KsHeader) && d[0] == KS_MAGIC0 && d[1] == KS_MAGIC1 &&
         d[2] == KS_PROTO_VERSION;
}

// ---- USB serial framing between Pi and bridge ----
// [0x7E][len][payload ... len bytes][crc8]
// Pi -> bridge payload: a raw ESP-NOW packet (broadcast as-is).
// Bridge -> Pi payload: [src mac 6][rssi int8][ESP-NOW packet]   or a KsBridgeInfo.
#define KS_SER_START 0x7E

static inline uint8_t ksCrc8(const uint8_t *d, int len) {
  uint8_t crc = 0;
  for (int i = 0; i < len; i++) {
    crc ^= d[i];
    for (int b = 0; b < 8; b++) crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x07) : (uint8_t)(crc << 1);
  }
  return crc;
}
