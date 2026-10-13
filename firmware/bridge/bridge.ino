/*
 * Kinetic Sculpture — ESP-NOW bridge firmware
 * -------------------------------------------
 * Runs on one plain ESP32-S3 (or ESP32) dev board plugged into the Raspberry Pi by USB.
 * It is a dumb relay:
 *   Pi  --USB serial-->  bridge  --ESP-NOW broadcast-->  every PD Stepper
 *   PD Stepper status  --ESP-NOW-->  bridge  --USB serial-->  Pi
 *
 * Serial framing (both directions): [0x7E][len][payload][crc8]
 *   Pi -> bridge payload  : one ESP-NOW packet, broadcast unchanged.
 *   bridge -> Pi payload  : [src mac 6][rssi int8][ESP-NOW packet], or a KsBridgeInfo heartbeat.
 *
 * Board settings (Arduino IDE): "ESP32S3 Dev Module", USB CDC On Boot: Enabled.
 * Tip: use a board with an external antenna (or place it near the sculpture) for the best link.
 */

#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <esp_arduino_version.h>
#include "ks_protocol.h"

#define FW_VERSION 2
#define ESPNOW_CHANNEL 1      // must match the nodes
#ifdef LED_BUILTIN
#define LED_PIN LED_BUILTIN   // change if your board's LED is elsewhere
#else
#define LED_PIN 2
#endif

const uint8_t BROADCAST[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
uint8_t myMac[6];

struct RxItem {
  uint8_t mac[6];
  int8_t rssi;
  uint8_t len;
  uint8_t data[250];
};
QueueHandle_t rxQueue;
volatile uint32_t rxCount = 0;
uint32_t txOk = 0, txFail = 0;
uint32_t lastActivity = 0;

void handlePacket(const uint8_t *src, int8_t rssi, const uint8_t *data, int len) {
  if (!ksHeaderOk(data, len) || len > 248) return;   // 7-byte prefix + packet must fit the 255-byte serial frame
  RxItem it;
  memcpy(it.mac, src, 6);
  it.rssi = rssi;
  it.len = (uint8_t)len;
  memcpy(it.data, data, len);
  xQueueSend(rxQueue, &it, 0);
  rxCount++;
}

#if ESP_ARDUINO_VERSION_MAJOR >= 3
void onRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  handlePacket(info->src_addr, info->rx_ctrl ? info->rx_ctrl->rssi : 0, data, len);
}
#else
void onRecv(const uint8_t *mac, const uint8_t *data, int len) { handlePacket(mac, 0, data, len); }
#endif

void writeFrame(const uint8_t *payload, int len) {
  uint8_t head[2] = {KS_SER_START, (uint8_t)len};
  uint8_t crc = ksCrc8(payload, len);
  Serial.write(head, 2);
  Serial.write(payload, len);
  Serial.write(&crc, 1);
}

// ---- serial frame parser ----
uint8_t rxBuf[256];
int rxState = 0, rxLen = 0, rxPos = 0;

void onSerialPacket(const uint8_t *p, int len) {
  if (!ksHeaderOk(p, len)) return;
  esp_err_t r = esp_now_send(BROADCAST, p, len);
  if (r == ESP_OK) txOk++; else txFail++;
  lastActivity = millis();
}

void pollSerial() {
  while (Serial.available()) {
    uint8_t b = Serial.read();
    switch (rxState) {
      case 0: if (b == KS_SER_START) rxState = 1; break;
      case 1:
        rxLen = b; rxPos = 0;
        rxState = (rxLen > 0 && rxLen <= 250) ? 2 : 0;
        break;
      case 2:
        rxBuf[rxPos++] = b;
        if (rxPos >= rxLen) rxState = 3;
        break;
      case 3:
        if (b == ksCrc8(rxBuf, rxLen)) onSerialPacket(rxBuf, rxLen);
        rxState = 0;
        break;
    }
  }
}

void setup() {
  pinMode(LED_PIN, OUTPUT);
  Serial.begin(921600);
  rxQueue = xQueueCreate(32, sizeof(RxItem));

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_ps(WIFI_PS_NONE);
  esp_wifi_set_promiscuous(true);
  esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);
  esp_wifi_set_promiscuous(false);
  esp_wifi_get_mac(WIFI_IF_STA, myMac);

  esp_now_init();
  esp_now_register_recv_cb(onRecv);
  esp_now_peer_info_t p = {};
  memcpy(p.peer_addr, BROADCAST, 6);
  p.channel = ESPNOW_CHANNEL;
  p.encrypt = false;
  esp_now_add_peer(&p);
}

void loop() {
  pollSerial();

  RxItem it;
  while (xQueueReceive(rxQueue, &it, 0) == pdTRUE) {
    uint8_t out[7 + 250];
    memcpy(out, it.mac, 6);
    out[6] = (uint8_t)it.rssi;
    memcpy(out + 7, it.data, it.len);
    writeFrame(out, 7 + it.len);
  }

  static uint32_t lastHb = 0;
  if (millis() - lastHb > 1000) {
    lastHb = millis();
    KsBridgeInfo b = {};
    b.h = {KS_MAGIC0, KS_MAGIC1, KS_PROTO_VERSION, KS_T_BRIDGE};
    memcpy(b.mac, myMac, 6);
    b.channel = ESPNOW_CHANNEL;
    b.fw = FW_VERSION;
    b.tx_ok = txOk;
    b.tx_fail = txFail;
    b.rx_count = rxCount;
    writeFrame((uint8_t *)&b, sizeof(b));
  }

  // LED: solid while the Pi is streaming, slow blink otherwise
  digitalWrite(LED_PIN, (millis() - lastActivity < 200) ? HIGH : ((millis() / 1000) % 2));
}
