/*
 * Kinetic Sculpture — PD Stepper motor node firmware
 * ---------------------------------------------------
 * Runs on every PD Stepper (ESP32-S3 + TMC2209 + AS5600).
 *
 * What it does
 *  - Listens for ESP-NOW broadcasts from the bridge (plugged into the Pi).
 *  - FRAME packets carry the target position + velocity for EVERY motor, 50 times
 *    a second. Each node picks out its own entry (by motor id), so all motors get
 *    the same packet at the same instant — that is what keeps them in sync.
 *  - FastAccelStepper generates the step pulses in hardware, so motion stays smooth
 *    while the radio and encoder are serviced.
 *  - Knows where it is without a home switch ("memory" home mode):
 *      * Teaching the top position stores an encoder CALIBRATION: the magnet angle at
 *        position 0. From then on the encoder angle pins the position exactly within
 *        one spool turn.
 *      * The turn count comes from a small POSITION RECORD saved in flash when the motor
 *        comes to rest, and again the instant the supply voltage starts to collapse.
 *      * At power-up: saved turn count + live encoder angle = exact position.
 *        If the record is not trustworthy, the node reports "position lost" and will
 *        not move to show commands until a tech re-teaches the top.
 *  - Enforces soft limits (top/bottom taught in the setup wizard) on everything it does.
 *  - Can also home against a limit switch (AUX2) or by StallGuard, reads the encoder to
 *    detect slipping, and reports status back ~5x per second.
 *  - Failsafe: if frames stop arriving for 300 ms the motor decelerates and holds.
 *
 * Board settings (Arduino IDE): "ESP32S3 Dev Module", USB CDC On Boot: Enabled.
 * Libraries: FastAccelStepper (gin66), TMC2209 (janelia-arduino).
 *
 * Buttons (local setup, only when the show is not streaming):
 *   SW1 hold = wind in, SW3 hold = pay out, SW2 hold 2 s = set current spot as zero (home).
 */

#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <esp_arduino_version.h>
#include <Wire.h>
#include <Preferences.h>
#include <TMC2209.h>
#include <FastAccelStepper.h>
#include "ks_protocol.h"

#define FW_VERSION 2
#define ESPNOW_CHANNEL 1   // must match the bridge
#define FULL_STEPS_PER_REV 200  // 1.8 degree motor

// ---------------- PD Stepper pin map ----------------
#define TMC_EN 21   // low = enabled
#define STEP_PIN 5
#define DIR_PIN 6
#define MS1 1
#define MS2 2
#define TMC_TX 17
#define TMC_RX 18
#define DIAG 16     // StallGuard output
#define PG 15       // USB-PD power good (LOW = good)
#define CFG1 38
#define CFG2 48
#define CFG3 47
#define VBUS 4
#define LED1 10
#define LED2 12
#define SW1 35
#define SW2 36
#define SW3 37
#define AUX1 14     // free (reserved for a wired GO line)
#define AUX2 13     // limit switch input, switch to GND
#define I2C_SDA 8
#define I2C_SCL 9
#define AS5600_ADDR 0x36
const float VBUS_DIV_RATIO = 0.1189427313f;  // 20k / 2.7k divider

// ---------------- tuning ----------------
const uint32_t LINK_TIMEOUT_MS = 300;   // no frames for this long -> stop
const float TRACK_KP = 4.0f;            // 1/s, how hard to close position error
const float TRACK_LOOKAHEAD_S = 0.02f;  // aim one frame ahead
const uint32_t STATUS_PERIOD_MS = 200;
const uint32_t STALL_IGNORE_MS = 200;   // ignore DIAG while accelerating at homing start
const uint32_t REST_SAVE_MS = 2000;     // save the position record after this long at rest
const float POWER_FAIL_RATIO = 0.75f;   // supply below 75% of its normal level = power is going

// ---------------- state ----------------
Preferences prefs;
TMC2209 driver;
FastAccelStepperEngine engine;
FastAccelStepper *stepper = nullptr;

uint8_t myId = KS_ID_UNASSIGNED;
uint8_t myMac[6];
uint8_t bridgeMac[6];
bool bridgeKnown = false;
const uint8_t BROADCAST[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

int32_t cfg[KS_CFG_COUNT] = {
  60,     // run current %
  30,     // hold current %
  16,     // microsteps
  20000,  // max speed microsteps/s
  60000,  // max accel microsteps/s^2
  0,      // flags
  1,      // home mode: limit switch
  1600,   // home speed
  30000,  // home timeout ms
  400,    // backoff microsteps
  60,     // stall threshold
  20      // PD volts
};
bool cfgSaved = false;

enum Mode { M_IDLE, M_STREAM, M_HOMING, M_JOG, M_BUTTON };
Mode mode = M_IDLE;

// frame mailbox (written in the WiFi task, read in loop)
portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
volatile bool frameNew = false;
volatile uint32_t lastFrameMs = 0;
KsMotorTarget frameTarget;
uint8_t frameFlags = 0;
uint16_t frameSeq = 0;
bool linkLost = false;
bool everStreamed = false;

QueueHandle_t cmdQueue;

// homing
enum HomePhase { H_CLEAR, H_SEEK, H_BACKOFF };
HomePhase homePhase;
uint32_t homeStartMs = 0, seekStartMs = 0;
bool homed = false, homeFailed = false;

// encoder
bool encOk = false;
int32_t encTotal = 0, encZero = 0;
int prevRaw = -1;
bool slip = false;
uint32_t slipSinceMs = 0;

// driver / power
bool tmcOk = false, tmcOverTemp = false;
uint16_t sgResult = 0;
uint16_t vbusMv = 0;
bool powerGood = false;
int32_t lastTarget = 0;

uint32_t identifyUntil = 0;
uint32_t sw2DownMs = 0;

// position memory (home mode 3). Values are in microsteps at the microstep setting `ms`
// they were saved with, and rescaled if microstepping changes later.
const uint32_t CAL_MAGIC = 0x4B53434C;  // "KSCL"
const uint32_t REC_MAGIC = 0x4B535052;  // "KSPR"
const uint32_t LIM_MAGIC = 0x4B534C4D;  // "KSLM"
struct PosCal { uint32_t magic; uint16_t raw; uint16_t ms; int32_t pos; };   // angle at a known position
struct PosRec { uint32_t magic; int32_t pos; uint16_t ms; uint8_t clean; uint8_t rsv; };
struct LimRec { uint32_t magic; int32_t lo; int32_t hi; uint16_t ms; uint8_t on; uint8_t rsv; };
PosCal cal = {};
PosRec rec = {};
LimRec lim = {};
bool calibrated = false;
bool restored = false, restoredHost = false, posLost = false, pfSaved = false;
uint32_t restSinceMs = 0;
uint8_t magnetBits = 0;
// power-loss detection
bool pfArmed = false, pfLatched = false;
float vbusAvgMv = 0;
uint32_t pfGoodSinceMs = 0;

// ---------------- helpers ----------------
int32_t stepsPerRev() { return FULL_STEPS_PER_REV * cfg[KS_CFG_MICROSTEPS]; }
int encSign() { return (cfg[KS_CFG_FLAGS] & 0x02) ? -1 : 1; }
bool memoryMode() { return cfg[KS_CFG_HOME_MODE] == 3; }
// convert a value saved at microstep setting `fromMs` into the current setting
int32_t rescale(int32_t v, uint16_t fromMs) {
  if (!fromMs || fromMs == cfg[KS_CFG_MICROSTEPS]) return v;
  return (int32_t)((int64_t)v * cfg[KS_CFG_MICROSTEPS] / fromMs);
}
bool limitsOn() { return lim.magic == LIM_MAGIC && lim.on; }
int32_t limLo() { return rescale(lim.lo, lim.ms); }
int32_t limHi() { return rescale(lim.hi, lim.ms); }
bool magnetOk() { return (magnetBits & KS_M_READ_OK) && (magnetBits & KS_M_DETECTED); }

bool limitPressed() { return digitalRead(AUX2) == LOW; }

void setPdVoltage(int v) {
  //                5V 9V 12V 15V 20V
  // CFG1           1  0  0   0   0
  // CFG2           -  0  0   1   1
  // CFG3           -  0  1   1   0
  if (v == 5) { digitalWrite(CFG1, HIGH); return; }
  digitalWrite(CFG1, LOW);
  digitalWrite(CFG2, (v == 15 || v == 20) ? HIGH : LOW);
  digitalWrite(CFG3, (v == 12 || v == 15) ? HIGH : LOW);
}

void enableMotor(bool on) {
  if (on && powerGood) {
    driver.enable();
    stepper->enableOutputs();
  } else {
    stepper->forceStop();
    stepper->disableOutputs();
    driver.disable();
    if (mode != M_IDLE) mode = M_IDLE;
  }
}

bool motorEnabled() { return digitalRead(TMC_EN) == LOW; }

void applyConfig() {
  setPdVoltage(cfg[KS_CFG_PD_VOLTS]);
  driver.setMicrostepsPerStep(cfg[KS_CFG_MICROSTEPS]);
  driver.setRunCurrent(constrain(cfg[KS_CFG_RUN_CURRENT], 0, 100));
  driver.setHoldCurrent(constrain(cfg[KS_CFG_HOLD_CURRENT], 0, 100));
  if (cfg[KS_CFG_FLAGS] & 0x04) driver.disableStealthChop(); else driver.enableStealthChop();
  if (cfg[KS_CFG_FLAGS] & 0x01) driver.enableInverseMotorDirection(); else driver.disableInverseMotorDirection();
  driver.setStallGuardThreshold(constrain(cfg[KS_CFG_STALL_THRESH], 0, 255));
  driver.setCoolStepDurationThreshold(0xFFFFF);  // StallGuard/DIAG active at all speeds
  stepper->setAcceleration(cfg[KS_CFG_MAX_ACCEL]);
}

void saveConfig() {
  prefs.putBytes("cfg", cfg, sizeof(cfg));
  cfgSaved = true;
}

void loadPrefs() {
  prefs.begin("ks", false);
  myId = prefs.getUChar("id", KS_ID_UNASSIGNED);
  if (prefs.getBytesLength("cfg") == sizeof(cfg)) {
    prefs.getBytes("cfg", cfg, sizeof(cfg));
    cfgSaved = true;
  }
  if (prefs.getBytesLength("cal") == sizeof(cal)) prefs.getBytes("cal", &cal, sizeof(cal));
  if (prefs.getBytesLength("rec") == sizeof(rec)) prefs.getBytes("rec", &rec, sizeof(rec));
  if (prefs.getBytesLength("lim") == sizeof(lim)) prefs.getBytes("lim", &lim, sizeof(lim));
  calibrated = cal.magic == CAL_MAGIC && cal.ms > 0;
  if (rec.magic != REC_MAGIC) rec = {};
}

// ---------------- position memory ----------------
// Tell the step generator AND the encoder bookkeeping that the shaft is at position p.
void anchorPosition(int32_t p) {
  stepper->setCurrentPosition(p);
  encZero = encTotal - encSign() * (int32_t)((int64_t)p * 4096 / stepsPerRev());
  lastTarget = p;
  homed = true; homeFailed = false; slip = false; slipSinceMs = 0;
}

int32_t encoderSteps();

// Best estimate of where the shaft really is: the encoder once we are homed.
int32_t bestPosition() {
  return (homed && encOk && magnetOk()) ? encoderSteps() : stepper->getCurrentPosition();
}

// The calibration fixes the position modulo one turn. Return the position consistent
// with the current magnet angle that lies closest to `approx`.
int32_t phasePosition(int32_t approx) {
  int32_t spr = stepsPerRev();
  int d = (prevRaw - cal.raw) & 0x0FFF;                       // 0..4095 counts past the taught angle
  int64_t base = (int64_t)rescale(cal.pos, cal.ms) + (int64_t)encSign() * d * spr / 4096;
  int64_t k = llround((double)((int64_t)approx - base) / spr);
  return (int32_t)(base + k * spr);
}

void writeRec(int32_t pos, bool clean) {
  rec.magic = REC_MAGIC;
  rec.pos = pos;
  rec.ms = cfg[KS_CFG_MICROSTEPS];
  rec.clean = clean ? 1 : 0;
  prefs.putBytes("rec", &rec, sizeof(rec));
}

void saveCalibration(int32_t pos) {
  if (!(encOk && magnetOk() && prevRaw >= 0)) return;   // no usable encoder: can't calibrate
  cal.magic = CAL_MAGIC;
  cal.raw = (uint16_t)prevRaw;
  cal.ms = cfg[KS_CFG_MICROSTEPS];
  cal.pos = pos;
  prefs.putBytes("cal", &cal, sizeof(cal));
  calibrated = true;
}

void forgetCalibration() {
  calibrated = false;
  cal = {};
  rec = {};
  prefs.remove("cal");
  prefs.remove("rec");
  homed = false; restored = false; restoredHost = false; posLost = false;
}

void setLimits(bool on, int32_t lo, int32_t hi) {
  lim.magic = LIM_MAGIC;
  lim.on = on ? 1 : 0;
  lim.lo = min(lo, hi);
  lim.hi = max(lo, hi);
  lim.ms = cfg[KS_CFG_MICROSTEPS];
  prefs.putBytes("lim", &lim, sizeof(lim));
}

int32_t clampToLimits(int32_t p) {
  if (!limitsOn() || !homed) return p;
  return constrain(p, limLo(), limHi());
}

void readEncoder();
void readMagnet();

// Called once in setup(): recover the position saved before the power went off.
void restoreAtBoot() {
  for (int i = 0; i < 10 && (prevRaw < 0 || !encOk); i++) { readEncoder(); delay(2); }
  readMagnet();
  if (!memoryMode() || !calibrated) return;
  if (!encOk || !magnetOk()) { posLost = true; return; }
  if (rec.magic != REC_MAGIC || !rec.clean) { posLost = true; return; }   // was moving when power went
  int32_t p = phasePosition(rescale(rec.pos, rec.ms));
  int32_t spr = stepsPerRev();
  if (limitsOn() && (p < limLo() - spr / 2 || p > limHi() + spr / 2)) { posLost = true; return; }
  anchorPosition(p);
  restored = true;
}

// Keep the saved record honest: "dirty" as soon as the motor moves, "clean" with the exact
// position once it has been at rest for a moment (outside of the running show).
void updatePositionMemory(uint32_t now) {
  if (!homed || !calibrated) return;
  if (stepper->isRunning()) {
    restSinceMs = 0;
    if (rec.magic != REC_MAGIC || rec.clean) writeRec(bestPosition(), false);
    return;
  }
  if (!restSinceMs) restSinceMs = now ? now : 1;
  if (!rec.clean && mode != M_STREAM && now - restSinceMs > REST_SAVE_MS) writeRec(bestPosition(), true);
}

// Power is going away: stop the motor (so what is left in the capacitors runs only the
// processor) and save the exact position. NVS writes are designed to survive an
// interrupted write, so the worst case is the previous (dirty) record -> "position lost".
void checkPowerFail(uint32_t now) {
  static uint32_t lastUs = 0;
  uint32_t us = micros();
  if (us - lastUs < 1000) return;
  lastUs = us;
  float mv = analogReadMilliVolts(VBUS) / VBUS_DIV_RATIO;
  bool pgBad = digitalRead(PG) == HIGH;
  if (!pfArmed) {          // wait for a steady supply before watching for its loss
    if (!pgBad && mv > 4000) {
      vbusAvgMv = vbusAvgMv <= 0 ? mv : vbusAvgMv * 0.98f + mv * 0.02f;
      if (!pfGoodSinceMs) pfGoodSinceMs = now ? now : 1;
      if (now - pfGoodSinceMs > 1500) pfArmed = true;
    } else {
      pfGoodSinceMs = 0;
    }
    return;
  }
  bool low = pgBad || mv < vbusAvgMv * POWER_FAIL_RATIO;
  if (!low) {
    vbusAvgMv = vbusAvgMv * 0.995f + mv * 0.005f;
    if (pfLatched) {                     // supply came back (a dip, not a cut)
      if (!pfGoodSinceMs) pfGoodSinceMs = now ? now : 1;
      if (now - pfGoodSinceMs > 500) pfLatched = false;
    }
    return;
  }
  pfGoodSinceMs = 0;
  if (pfLatched) return;
  pfLatched = true;
  digitalWrite(TMC_EN, HIGH);            // cut motor current immediately
  stepper->forceStop();
  if (mode != M_IDLE) mode = M_IDLE;
  if (homed && calibrated) {
    writeRec(bestPosition(), true);
    pfSaved = true;
  }
}

void addPeer(const uint8_t *mac) {
  if (esp_now_is_peer_exist(mac)) return;
  esp_now_peer_info_t p = {};
  memcpy(p.peer_addr, mac, 6);
  p.channel = ESPNOW_CHANNEL;
  p.encrypt = false;
  esp_now_add_peer(&p);
}

// ---------------- ESP-NOW receive (runs in WiFi task: keep it short) ----------------
void handlePacket(const uint8_t *src, const uint8_t *data, int len) {
  if (!ksHeaderOk(data, len)) return;
  uint8_t type = data[3];

  if (type == KS_T_FRAME && len >= ksFrameSize(1)) {
    const KsFrame *f = (const KsFrame *)data;
    if (myId == KS_ID_UNASSIGNED || myId >= f->count || len < ksFrameSize(f->count)) return;
    portENTER_CRITICAL(&mux);
    frameTarget = f->m[myId];
    frameFlags = f->flags;
    frameSeq = f->seq;
    frameNew = true;
    lastFrameMs = millis();
    portEXIT_CRITICAL(&mux);
    if (!bridgeKnown) { memcpy(bridgeMac, src, 6); bridgeKnown = true; }
  } else if (type == KS_T_CMD && len >= (int)sizeof(KsCmd)) {
    const KsCmd *c = (const KsCmd *)data;
    bool macSet = false;
    for (int i = 0; i < 6; i++) if (c->mac[i]) macSet = true;
    bool forMe = macSet ? (memcmp(c->mac, myMac, 6) == 0)
                        : (c->target == KS_TARGET_ALL || (myId != KS_ID_UNASSIGNED && c->target == myId));
    if (!forMe) return;
    if (!bridgeKnown) { memcpy(bridgeMac, src, 6); bridgeKnown = true; }
    xQueueSend(cmdQueue, c, 0);
  }
}

#if ESP_ARDUINO_VERSION_MAJOR >= 3
void onRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) { handlePacket(info->src_addr, data, len); }
#else
void onRecv(const uint8_t *mac, const uint8_t *data, int len) { handlePacket(mac, data, len); }
#endif

// ---------------- homing ----------------
void startHoming() {
  if (!powerGood) { homeFailed = true; return; }
  homeFailed = false;
  if (cfg[KS_CFG_HOME_MODE] == 0) { homeFailed = true; return; }  // manual mode: use SET_ZERO
  if (memoryMode()) {
    // No switch to find: "home" means wind up to the taught top, if we know where we are.
    if (!homed) { homeFailed = true; return; }
    enableMotor(true);
    stepper->setSpeedInHz(cfg[KS_CFG_HOME_SPEED]);
    stepper->setAcceleration(cfg[KS_CFG_MAX_ACCEL]);
    stepper->moveTo(limitsOn() ? limLo() : 0);
    mode = M_JOG;
    return;
  }
  homed = false; slip = false;
  enableMotor(true);
  mode = M_HOMING;
  homeStartMs = millis();
  stepper->setSpeedInHz(cfg[KS_CFG_HOME_SPEED]);
  stepper->setAcceleration(cfg[KS_CFG_MAX_ACCEL]);
  if (cfg[KS_CFG_HOME_MODE] == 1 && limitPressed()) {
    homePhase = H_CLEAR;       // already on the switch: pay out until released
    stepper->runForward();
  } else {
    homePhase = H_SEEK;
    seekStartMs = millis();
    stepper->runBackward();    // wind in
  }
}

void finishHome(bool ok) {
  if (ok) {
    stepper->setCurrentPosition(0);
    encZero = encTotal;
    homed = true;
    slip = false;
    lastTarget = 0;
  } else {
    stepper->forceStop();
    homeFailed = true;
  }
  mode = M_IDLE;
}

void updateHoming() {
  if (millis() - homeStartMs > (uint32_t)cfg[KS_CFG_HOME_TIMEOUT]) { finishHome(false); return; }
  switch (homePhase) {
    case H_CLEAR:
      if (!limitPressed()) {
        stepper->forceStop();
        homePhase = H_SEEK;
        seekStartMs = millis();
        stepper->runBackward();
      }
      break;
    case H_SEEK: {
      bool hit = false;
      if (cfg[KS_CFG_HOME_MODE] == 1) hit = limitPressed();
      else hit = (millis() - seekStartMs > STALL_IGNORE_MS) && digitalRead(DIAG) == HIGH;
      if (hit) {
        stepper->forceStop();
        stepper->setCurrentPosition(0);
        homePhase = H_BACKOFF;
        stepper->moveTo(cfg[KS_CFG_HOME_BACKOFF]);
      }
      break;
    }
    case H_BACKOFF:
      if (!stepper->isRunning()) finishHome(true);
      break;
  }
}

// ---------------- commands ----------------
void handleCmd(const KsCmd &c) {
  switch (c.cmd) {
    case KS_C_ENABLE:
      enableMotor(c.a[0] != 0);
      break;
    case KS_C_HOME:
      startHoming();
      break;
    case KS_C_SET_ZERO:
      // "This spot is position a[0]" (the setup wizard sends 0 at the taught top).
      // Also stores the encoder calibration so the position survives power cycles.
      if (!stepper->isRunning()) {
        anchorPosition(c.a[0]);
        saveCalibration(c.a[0]);
        if (calibrated) writeRec(c.a[0], true);
        restored = false; restoredHost = false; posLost = false;
      }
      break;
    case KS_C_JOG:
      if (mode == M_IDLE || mode == M_JOG) {
        enableMotor(true);
        stepper->setSpeedInHz(max(1, (int)c.a[1]));
        stepper->setAcceleration(cfg[KS_CFG_MAX_ACCEL]);
        if (limitsOn() && homed && c.a[2] != 1) {
          int32_t base = stepper->isRunning() ? stepper->targetPos() : stepper->getCurrentPosition();
          stepper->moveTo(clampToLimits(base + c.a[0]));
        } else {
          stepper->move(c.a[0]);
        }
        mode = M_JOG;
      }
      break;
    case KS_C_SET_LIMITS:
      setLimits(c.a[0] != 0, c.a[1], c.a[2]);
      break;
    case KS_C_RESTORE:
      // The Pi remembers roughly where we were (e.g. only this node rebooted).
      if (!homed && calibrated && encOk && magnetOk() && !stepper->isRunning()) {
        readEncoder();
        int32_t p = phasePosition(c.a[0]);
        anchorPosition(p);
        writeRec(p, true);
        restoredHost = true; posLost = false;
      }
      break;
    case KS_C_STOP:
      stepper->stopMove();
      mode = M_IDLE;
      break;
    case KS_C_IDENTIFY:
      identifyUntil = millis() + 3000;
      break;
    case KS_C_SET_ID:
      if (c.a[0] == KS_ID_UNASSIGNED || (c.a[0] >= 0 && c.a[0] < KS_MAX_MOTORS)) {   // 0xFF = release
        myId = (uint8_t)c.a[0];
        prefs.putUChar("id", myId);
        identifyUntil = millis() + 1500;
      }
      break;
    case KS_C_CONFIG:
      if (stepper->isRunning()) break;   // never reconfigure a moving motor
      {
        int32_t oldMs = cfg[KS_CFG_MICROSTEPS];
        int32_t oldFlags = cfg[KS_CFG_FLAGS];
        memcpy(cfg, c.a, sizeof(cfg));
        // flipping motor or encoder direction changes how angle maps to position:
        // the taught top no longer applies
        if ((oldFlags ^ cfg[KS_CFG_FLAGS]) & 0x03) forgetCalibration();
        cfg[KS_CFG_MICROSTEPS] = constrain(cfg[KS_CFG_MICROSTEPS], 1, 256);
        if (cfg[KS_CFG_MICROSTEPS] != oldMs) {   // keep the same physical position in new units
          int32_t p = stepper->getCurrentPosition();
          stepper->setCurrentPosition((int32_t)((int64_t)p * cfg[KS_CFG_MICROSTEPS] / oldMs));
          lastTarget = (int32_t)((int64_t)lastTarget * cfg[KS_CFG_MICROSTEPS] / oldMs);
        }
      }
      applyConfig();
      saveConfig();
      break;
    case KS_C_REBOOT:
      delay(50);
      ESP.restart();
      break;
  }
}

// ---------------- streaming ----------------
void applyFrame() {
  KsMotorTarget t;
  uint8_t flags;
  portENTER_CRITICAL(&mux);
  t = frameTarget;
  flags = frameFlags;
  frameNew = false;
  portEXIT_CRITICAL(&mux);

  if (mode == M_HOMING || mode == M_BUTTON) return;   // local operations win
  if (!(flags & KS_FF_ENABLE) || !homed || !powerGood) {
    if (mode == M_STREAM) { stepper->stopMove(); mode = M_IDLE; }
    return;
  }
  if (mode == M_JOG) stepper->stopMove();
  if (mode != M_STREAM) { enableMotor(true); mode = M_STREAM; }
  everStreamed = true;
  linkLost = false;

  int32_t pos = stepper->getCurrentPosition();
  float err = (float)(t.pos - pos);
  float speed = fabsf((float)t.vel) + fabsf(err) * TRACK_KP;
  speed = constrain(speed, 20.0f, (float)cfg[KS_CFG_MAX_SPEED]);
  int32_t aim = clampToLimits(t.pos + (int32_t)(t.vel * TRACK_LOOKAHEAD_S));
  t.pos = clampToLimits(t.pos);
  stepper->setSpeedInHz((uint32_t)speed);
  stepper->setAcceleration(cfg[KS_CFG_MAX_ACCEL]);
  stepper->moveTo(aim);
  lastTarget = t.pos;
}

// ---------------- sensors ----------------
void readEncoder() {
  Wire.beginTransmission(AS5600_ADDR);
  Wire.write(0x0C);
  if (Wire.endTransmission(false) != 0) { encOk = false; return; }
  if (Wire.requestFrom(AS5600_ADDR, 2) < 2) { encOk = false; return; }
  int raw = ((Wire.read() << 8) | Wire.read()) & 0x0FFF;
  encOk = true;
  if (prevRaw < 0) { prevRaw = raw; return; }
  int d = raw - prevRaw;
  if (d > 2048) d -= 4096;
  if (d < -2048) d += 4096;
  encTotal += d;
  prevRaw = raw;
}

void readMagnet() {
  Wire.beginTransmission(AS5600_ADDR);
  Wire.write(0x0B);                       // STATUS: MD (bit5), ML (bit4), MH (bit3)
  if (Wire.endTransmission(false) != 0 || Wire.requestFrom(AS5600_ADDR, 1) < 1) { magnetBits = 0; return; }
  uint8_t st = Wire.read();
  magnetBits = KS_M_READ_OK | ((st & 0x20) ? KS_M_DETECTED : 0) | ((st & 0x10) ? KS_M_WEAK : 0) |
               ((st & 0x08) ? KS_M_STRONG : 0);
}

int32_t encoderSteps() {
  int32_t counts = encTotal - encZero;
  if (cfg[KS_CFG_FLAGS] & 0x02) counts = -counts;
  return (int32_t)((int64_t)counts * stepsPerRev() / 4096);
}

void checkSlip() {
  if (!homed || !encOk) { slipSinceMs = 0; return; }
  int32_t diff = abs(encoderSteps() - stepper->getCurrentPosition());
  if (diff > stepsPerRev() / 4) {
    if (!slipSinceMs) slipSinceMs = millis();
    if (millis() - slipSinceMs > 500) slip = true;  // latched until re-homed
  } else {
    slipSinceMs = 0;
  }
}

void readDriver() {
  tmcOk = driver.isSetupAndCommunicating();
  if (tmcOk) {
    TMC2209::Status s = driver.getStatus();
    tmcOverTemp = s.over_temperature_warning || s.over_temperature_shutdown;
    sgResult = driver.getStallGuardResult();
  }
  uint32_t mv = 0;
  for (int i = 0; i < 4; i++) mv += analogReadMilliVolts(VBUS);
  vbusMv = (uint16_t)((mv / 4.0f) / VBUS_DIV_RATIO);
}

void sendStatus() {
  KsStatus s = {};
  s.h = {KS_MAGIC0, KS_MAGIC1, KS_PROTO_VERSION, KS_T_STATUS};
  s.id = myId;
  s.fw = FW_VERSION;
  uint16_t f = 0;
  if (motorEnabled()) f |= KS_S_ENABLED;
  if (homed) f |= KS_S_HOMED;
  if (mode == M_HOMING) f |= KS_S_HOMING;
  if (stepper->isRunning()) f |= KS_S_MOVING;
  if (linkLost) f |= KS_S_LINK_LOST;
  if (powerGood) f |= KS_S_POWER_GOOD;
  if (limitPressed()) f |= KS_S_LIMIT;
  if (homeFailed) f |= KS_S_HOME_FAILED;
  if (tmcOk) f |= KS_S_TMC_OK;
  if (tmcOverTemp) f |= KS_S_TMC_OVERTEMP;
  if (slip) f |= KS_S_SLIP;
  if (cfgSaved) f |= KS_S_CONFIGURED;
  if (mode == M_STREAM) f |= KS_S_STREAMING;
  if (myId == KS_ID_UNASSIGNED) f |= KS_S_UNASSIGNED;
  s.flags = f;
  s.pos = stepper->getCurrentPosition();
  s.target = lastTarget;
  s.enc = encOk ? encoderSteps() : INT32_MIN;
  s.vbus_mv = vbusMv;
  s.sg = sgResult;
  s.last_seq = frameSeq;
  s.uptime_s = millis() / 1000;
  s.magnet = magnetBits;
  uint8_t info = 0;
  if (calibrated) info |= KS_I_CALIBRATED;
  if (limitsOn()) info |= KS_I_LIMITS;
  if (rec.magic == REC_MAGIC && rec.clean) info |= KS_I_REC_CLEAN;
  if (restored) info |= KS_I_RESTORED;
  if (restoredHost) info |= KS_I_RESTORED_HOST;
  if (posLost && !homed) info |= KS_I_POS_LOST;
  if (pfSaved) info |= KS_I_PF_SAVED;
  s.info = info;
  s.lim_min = limitsOn() ? limLo() : 0;
  s.lim_max = limitsOn() ? limHi() : 0;
  esp_now_send(bridgeKnown ? bridgeMac : BROADCAST, (uint8_t *)&s, sizeof(s));
}

// ---------------- local buttons ----------------
void updateButtons() {
  if (mode == M_STREAM || mode == M_HOMING) return;
  bool in = digitalRead(SW1) == LOW, out = digitalRead(SW3) == LOW, zero = digitalRead(SW2) == LOW;
  if ((in || out) && mode != M_BUTTON) {
    enableMotor(true);
    stepper->setSpeedInHz(cfg[KS_CFG_HOME_SPEED]);
    stepper->setAcceleration(cfg[KS_CFG_MAX_ACCEL]);
    if (limitsOn() && homed) stepper->moveTo(in ? limLo() : limHi());   // never past the taught limits
    else if (in) stepper->runBackward(); else stepper->runForward();
    mode = M_BUTTON;
  } else if (!in && !out && mode == M_BUTTON) {
    stepper->stopMove();
    mode = M_IDLE;
  }
  if (zero) {
    if (!sw2DownMs) sw2DownMs = millis();
    if (millis() - sw2DownMs > 2000 && !stepper->isRunning()) {
      KsCmd c = {};
      c.cmd = KS_C_SET_ZERO;
      handleCmd(c);
      identifyUntil = millis() + 800;
      sw2DownMs = 0xFFFFFFFF;  // wait for release
    }
  } else {
    sw2DownMs = 0;
  }
}

void updateLeds() {
  uint32_t now = millis();
  if (now < identifyUntil) {
    bool on = (now / 80) % 2;
    digitalWrite(LED1, on); digitalWrite(LED2, !on);
    return;
  }
  bool led1;
  if (mode == M_STREAM) led1 = true;
  else if (linkLost) led1 = (now / 100) % 2;
  else led1 = (now / 1000) % 2;
  digitalWrite(LED1, led1);
  digitalWrite(LED2, homed ? HIGH : (homeFailed ? (now / 250) % 2 : LOW));
}

// ---------------- USB serial (bench setup) ----------------
void handleSerial() {
  if (!Serial.available()) return;
  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line.startsWith("id ")) {
    int id = line.substring(3).toInt();
    if (id >= 0 && id < KS_MAX_MOTORS) { myId = id; prefs.putUChar("id", myId); }
  }
  Serial.printf("KS node fw%d id=%d mac=%02X:%02X:%02X:%02X:%02X:%02X homed=%d pos=%ld enc=%ld vbus=%umV tmc=%d "
                "magnet=%02X calibrated=%d restored=%d lost=%d rec_clean=%d limits=%d[%ld..%ld]\n",
                FW_VERSION, myId, myMac[0], myMac[1], myMac[2], myMac[3], myMac[4], myMac[5], homed,
                (long)stepper->getCurrentPosition(), (long)encoderSteps(), vbusMv, tmcOk, magnetBits,
                calibrated, restored, posLost, rec.clean, limitsOn(), (long)limLo(), (long)limHi());
}

// ---------------- setup / loop ----------------
void setup() {
  pinMode(PG, INPUT);
  pinMode(CFG1, OUTPUT); pinMode(CFG2, OUTPUT); pinMode(CFG3, OUTPUT);
  pinMode(LED1, OUTPUT); pinMode(LED2, OUTPUT);
  pinMode(SW1, INPUT); pinMode(SW2, INPUT); pinMode(SW3, INPUT);
  pinMode(AUX2, INPUT_PULLUP);
  pinMode(DIAG, INPUT);
  pinMode(MS1, OUTPUT); pinMode(MS2, OUTPUT);
  digitalWrite(MS1, LOW); digitalWrite(MS2, LOW);   // TMC2209 UART address 0
  pinMode(TMC_EN, OUTPUT);
  digitalWrite(TMC_EN, HIGH);                        // start released

  loadPrefs();
  setPdVoltage(cfg[KS_CFG_PD_VOLTS]);

  delay(200);
  Serial.begin(115200);
  Serial.setTimeout(20);

  Wire.begin(I2C_SDA, I2C_SCL);
  Wire.setClock(400000);
  analogSetPinAttenuation(VBUS, ADC_11db);

  driver.setup(Serial2, 115200, TMC2209::SERIAL_ADDRESS_0, TMC_RX, TMC_TX);
  driver.enableAutomaticCurrentScaling();
  driver.setStandstillMode(TMC2209::NORMAL);

  engine.init();
  stepper = engine.stepperConnectToPin(STEP_PIN);
  stepper->setDirectionPin(DIR_PIN);
  stepper->setEnablePin(TMC_EN, true);   // low-active enable
  stepper->setAutoEnable(false);
  stepper->disableOutputs();
  applyConfig();
  driver.disable();

  restoreAtBoot();

  cmdQueue = xQueueCreate(8, sizeof(KsCmd));

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_ps(WIFI_PS_NONE);   // no power-save: lowest, most consistent latency
  esp_wifi_set_promiscuous(true);
  esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);
  esp_wifi_set_promiscuous(false);
  esp_wifi_get_mac(WIFI_IF_STA, myMac);
  if (esp_now_init() != ESP_OK) Serial.println("ESP-NOW init failed");
  esp_now_register_recv_cb(onRecv);
  addPeer(BROADCAST);

  Serial.printf("KS node ready. id=%d\n", myId);
}

void loop() {
  static uint32_t lastEnc = 0, lastStatus = 0, lastDrv = 0;
  uint32_t now = millis();

  powerGood = digitalRead(PG) == LOW && !pfLatched;
  checkPowerFail(now);
  if (!powerGood && motorEnabled()) enableMotor(false);

  KsCmd c;
  while (xQueueReceive(cmdQueue, &c, 0) == pdTRUE) handleCmd(c);

  if (bridgeKnown) addPeer(bridgeMac);

  if (frameNew) applyFrame();
  if (mode == M_STREAM && now - lastFrameMs > LINK_TIMEOUT_MS) {
    stepper->stopMove();        // decelerate and hold position
    mode = M_IDLE;
    linkLost = true;
  }
  if (mode == M_HOMING) updateHoming();
  if (mode == M_JOG && !stepper->isRunning()) mode = M_IDLE;

  if (now - lastEnc >= 10) { lastEnc = now; readEncoder(); checkSlip(); }
  if (now - lastDrv >= 500) { lastDrv = now; readDriver(); readMagnet(); }
  updatePositionMemory(now);
  // stagger status packets by id so nodes don't all transmit at once
  if (now - lastStatus >= STATUS_PERIOD_MS + (myId == KS_ID_UNASSIGNED ? 37 : myId * 3)) {
    lastStatus = now;
    sendStatus();
  }

  updateButtons();
  updateLeds();
  handleSerial();
}
