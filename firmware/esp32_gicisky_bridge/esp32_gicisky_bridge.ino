// ===========================================================================
// Gicisky 10.2" — ESP32 MQTT -> BLE bridge (production)   [PubSubClient build]
//
// v2 RELIABILITY PATCH:
//   * FIX client-object LEAK on hs02/hs03/bad_notif error paths -- NimBLE has
//     only 3 client slots, so 3 leaked failures wedged the BLE stack until
//     reboot (the "only the laptop + power-cycle revives it" failure mode).
//   * DIRECT CONNECT by MAC when the scan misses -- a panel stuck mid-session
//     often does not advertise but still accepts a direct connection (this is
//     what Windows/bleak does, and why the laptop could revive stuck panels).
//   * LAPTOP-STYLE forgiving protocol: proceed past a failed 01-ack if the
//     panel is talking, and tolerate odd notifications in the part loop
//     instead of aborting -- completing a transfer is what heals a wedged panel.
//   * Longer supervision timeout (6 s; NimBLE default ~2.5 s dropped the link
//     on brief panel MCU stalls that Windows rides out) + Data Length Extension
//     so 244-byte parts go out in one LL packet instead of ~10 fragments.
//   * Notification RING BUFFER (was single slot that could drop a status notif
//     arriving right behind a part request).
//   * MQTT restart command (gicisky/<PANEL>/cmd/restart) + auto-restart if
//     client slots are ever exhausted -- no more walking to the device.
//
// v3 SPEED RESTORE (per-format timing):
//   * both formats use the forced 11-15 ms interval and answer part requests
//     immediately. (A slower interval for compression2 was tried for a wrong
//     hypothesis; the real QuickLZ non-refresh cause was the 6-bit vendor
//     hash, fixed in compression.py.)
//
// Receives a payload over MQTT in small CHUNKS (PubSubClient can't do one
// 162 KB message, so Home Assistant splits it), reassembles to LittleFS,
// VERIFIES the md5 (never pushes corrupt data -> corruption locks the panel),
// and pushes it to the panel over BLE using the PROVEN flow:
//   patient connect+discovery retry  (panel needs recovery time after a commit)
//   -> request fast connection interval (9-12 = 11.25-15 ms, like the phone)
//   -> parts: per-column WRITE-WITH-RESPONSE, compression2 write-without-response
//   -> DONE_HOLD_MS hold -> clean disconnect -> recovery interval before next commit.
//
// MQTT contract (matches the gicisky_102 HA integration, mqtt_bridge.py):
//   sub  gicisky/<PANEL>/cmd/push   each msg = [4B seq LE][2B nchunks LE][chunk bytes]
//        the reassembled stream is [4B 'GCSK'][1B ver][4B len LE][16B md5][payload]
//        ver 1 = per-column, ver 2 = compression2/QuickLZ
//   pub  gicisky/<PANEL>/result     retained JSON {ok,md5_ok,done,parts,mean_ms,
//                                   total_s,tried_s,fmt,reason}
//   pub  gicisky/<PANEL>/battery    retained JSON {batt_pct,batt_v,rssi,fw}
//   pub  gicisky/<PANEL>/status     "online" (retained) on connect, LWT "offline"
//
// Libraries (Arduino IDE Library Manager — both single packages, no extra deps):
//   NimBLE-Arduino  1.4.x   (pin 1.4.3 if 2.x won't compile)
//   PubSubClient    (by Nick O'Leary)
//   LittleFS, WiFi, MD5Builder — built into the ESP32 Arduino core
// Board: ESP32 / ESP32-S3 / ESP32-C3 (NOT S2). Partition scheme must include LittleFS.
// ===========================================================================

#include <WiFi.h>
#include <esp_wifi.h>
#include <LittleFS.h>
#include <MD5Builder.h>
#include <PubSubClient.h>
#include <NimBLEDevice.h>
#include <esp_gap_ble_api.h>   // for explicit per-connection TX power

// ----------------------------- configuration --------------------------------
#include "secrets.h"  // Copy secrets.example.h to secrets.h and configure credentials.

static char TOPIC_PUSH[48], TOPIC_RESULT[48], TOPIC_STATUS[48], TOPIC_BATTERY[48], TOPIC_RESTART[48];

// ----------------------------- BLE protocol ---------------------------------
static NimBLEUUID SVC_UUID("0000fef0-0000-1000-8000-00805f9b34fb");
static NimBLEUUID CMD_UUID("0000fef1-0000-1000-8000-00805f9b34fb");
static NimBLEUUID IMG_UUID("0000fef2-0000-1000-8000-00805f9b34fb");
static const size_t   PART_SIZE   = 240;
static const uint32_t NOTIF_TO_MS = 10000;
// PER-FORMAT connection params (1.25 ms units; timeout 10 ms units):
//   per-column: the forced 11-15 ms interval is PROVEN -- it committed cleanly
//     dozens of times in the fast bridge.
//   compression2: kept as a separate pair so it can be tuned independently;
//     currently the same as per-column (see the note on QLZ_CI_* below).
// Supervision timeout 6 s for BOTH: costs zero speed, only drop tolerance
// (NimBLE's ~2.5 s default dropped the link on brief panel MCU stalls that
// Windows rides out).
static const uint16_t RAW_CI_MIN = 9,  RAW_CI_MAX = 12;   // per-column
static const uint16_t QLZ_CI_MIN = 9,  QLZ_CI_MAX = 12;   // compression2 (was 24/40;
                                                          // slow interval was for a WRONG
                                                          // hypothesis -- real cause was a
                                                          // 6-bit vendor hash, fixed in
                                                          // compression.py. Now == per-column)
static const uint16_t CI_LAT = 0, CI_TMO = 600;
static const uint32_t READY_TRIES   = 24;
static const uint32_t READY_WAIT_MS = 4000;
static const uint32_t RECOVERY_MS   = 30000;
static const uint32_t WAKE_DELAY_MS = 3000;       // settle time after connect before
                                                  // the handshake, so a COLD/sleeping
                                                  // panel's display controller wakes
                                                  // (mature stacks are slow here = wake it;
                                                  //  NimBLE is fast = can beat a cold panel)
static const float    BATT_MIN_V    = 2.2f;       // 0x008B advertisement battery scale
static const float    BATT_MAX_V    = 3.2f;       // (V = adv_byte/10; pct from min..max)
static const uint32_t DONE_HOLD_MS  = 5000;       // stay connected after DONE so the panel
                                                  // can DECOMPRESS (compression2) + REFRESH
                                                  // before we disconnect (the working laptop
                                                  // sender used 3 s; raw/per-column need less)
static const uint32_t PACE_QLZ_MS   = 0;          // compression2: no artificial pace. The
                                                  // transfer is request-driven (we wait for
                                                  // each part request via waitNotif), which
                                                  // is the same natural rhythm the PC/mobile
                                                  // app use. Raise to 5-25 ONLY if a fast WNR
                                                  // burst is seen to congest the link.
static const uint32_t REPLY_DELAY_MS = 0;         // per-column: reply to part requests
                                                  // immediately -- write-WITH-response is
                                                  // self-pacing and committed cleanly dozens
                                                  // of times unthrottled in the fast bridge.
                                                  // Raise (25-80) only if raw noise returns.
static const uint8_t  PUSH_TRIES    = 3;          // whole-transfer retries: a COMPLETED retry
                                                  // also self-heals a panel left mid-session
                                                  // by an earlier aborted push (this used to
                                                  // need the laptop + power-cycle ritual)

// ----------------------------- framing / files ------------------------------
static const size_t HEADER_SIZE = 25;            // 'GCSK'(4)+ver(1)+len(4)+md5(16)
static const char*  PAYLOAD_FILE = "/push.bin";
static const size_t MQTT_BUF = 1400;             // PubSubClient buffer (chunk + overhead)

// ----------------------------- globals --------------------------------------
WiFiClient    wifiClient;
PubSubClient  mqtt(wifiClient);
volatile bool g_verifyReady = false;             // a complete stream waits on disk
uint32_t      g_lastCommitMs = 0;
float         g_battV = -1.0f;                   // panel battery voltage (from advertisement)
int           g_battPct = -1;                    // panel battery percent (-1 = unknown)
uint16_t      g_fw = 0;                           // panel firmware (advertisement)
int           g_rssi = 0;                         // panel advertisement RSSI (dBm)
bool          g_lastCompress2 = false;            // format of the last push (for result)
const char*   g_lastReason = "idle";              // where the last push ended/failed

// chunk reassembly state
File          g_rxFile;
bool          g_rxActive = false;
uint16_t      g_rxChunks = 0;
uint32_t      g_rxCount  = 0;
uint32_t      g_rxLastMs = 0;

// BLE notification plumbing -- small RING BUFFER (was: single slot). If the
// panel fires two notifications back-to-back (e.g. the last part request
// immediately followed by the final 05xx status) the single slot lost the
// first one, surfacing as rare "unexpected notif" aborts. 4 deep is plenty:
// the protocol is request-driven, so >2 in flight never really happens.
#define NOTIF_Q 4
static uint8_t          g_q[NOTIF_Q][32];
static volatile size_t  g_qLen[NOTIF_Q];
static volatile uint8_t g_qHead = 0, g_qTail = 0;   // head = write, tail = read
static uint8_t          g_snap[32];
static size_t           g_snapLen = 0;
static portMUX_TYPE     g_mux = portMUX_INITIALIZER_UNLOCKED;
static volatile bool    g_restartReq = false;        // MQTT-commanded reboot

static void notifyCB(NimBLERemoteCharacteristic*, uint8_t* data, size_t len, bool) {
  portENTER_CRITICAL(&g_mux);
  if (len > 32) len = 32;
  uint8_t next = (uint8_t)((g_qHead + 1) % NOTIF_Q);
  if (next == g_qTail) g_qTail = (uint8_t)((g_qTail + 1) % NOTIF_Q);  // full -> drop oldest
  memcpy(g_q[g_qHead], data, len);
  g_qLen[g_qHead] = len;
  g_qHead = next;
  portEXIT_CRITICAL(&g_mux);
}
static void notifFlush() {
  portENTER_CRITICAL(&g_mux);
  g_qTail = g_qHead;
  portEXIT_CRITICAL(&g_mux);
}
static bool waitNotif(uint32_t timeoutMs) {
  uint32_t t0 = millis();
  while (g_qTail == g_qHead) { if (millis() - t0 > timeoutMs) return false; delay(1); }
  portENTER_CRITICAL(&g_mux);
  g_snapLen = g_qLen[g_qTail];
  memcpy(g_snap, g_q[g_qTail], g_snapLen);
  g_qTail = (uint8_t)((g_qTail + 1) % NOTIF_Q);
  portEXIT_CRITICAL(&g_mux);
  return true;
}

// Release the NimBLE client on EVERY exit path. The old code LEAKED the client
// object on the hs02/hs03/bad_notif paths; NimBLE has only 3 client slots
// (CONFIG_BT_NIMBLE_MAX_CONNECTIONS), so after 3 leaked failures createClient()
// returns nullptr and the bridge is wedged until reboot.
static void bleDrop(NimBLEClient*& client) {
  if (!client) return;
  if (client->isConnected()) { client->disconnect(); delay(100); }
  NimBLEDevice::deleteClient(client);
  client = nullptr;
}

// BLE address type for DIRECT (scan-less) connects. Cached from the last scan
// hit; if never seen, derived from the MAC's top two bits (11 = random static,
// which FF:FF:.. panels are; otherwise public).
static int g_addrType = -1;
static uint8_t addrTypeFromMac(const char* mac) {
  unsigned hi = (unsigned)strtoul(mac, nullptr, 16);
  return ((hi & 0xC0) == 0xC0) ? BLE_ADDR_RANDOM : BLE_ADDR_PUBLIC;
}

// ----------------------------- md5 verify -----------------------------------
static bool verifyPayloadMd5(const uint8_t expect[16], uint32_t payloadLen) {
  File f = LittleFS.open(PAYLOAD_FILE, "r");
  if (!f) return false;
  if (!f.seek(HEADER_SIZE)) { f.close(); return false; }
  MD5Builder md5; md5.begin();
  uint8_t buf[512]; uint32_t remaining = payloadLen;
  while (remaining) {
    size_t want = remaining < sizeof(buf) ? remaining : sizeof(buf);
    size_t n = f.read(buf, want);
    if (n == 0) { f.close(); return false; }
    md5.add(buf, n); remaining -= n;
  }
  f.close();
  md5.calculate();
  uint8_t got[16]; md5.getBytes(got);
  return memcmp(got, expect, 16) == 0;
}

// ----------------------------- result publish -------------------------------
static void publishResult(bool ok, uint8_t done, uint32_t parts,
                          uint32_t mean_us, uint32_t total_ms, bool md5_ok,
                          uint32_t tried_ms) {
  char buf[240];
  int n = snprintf(buf, sizeof(buf),
      "{\"ok\":%s,\"md5_ok\":%s,\"done\":\"05%02x\",\"parts\":%u,"
      "\"mean_ms\":%.1f,\"total_s\":%.1f,\"tried_s\":%.1f,\"fmt\":\"%s\",\"reason\":\"%s\"}",
      ok ? "true" : "false", md5_ok ? "true" : "false", done, parts,
      mean_us / 1000.0, total_ms / 1000.0, tried_ms / 1000.0,
      g_lastCompress2 ? "compression2" : "per-column", g_lastReason);
  mqtt.publish(TOPIC_RESULT, (const uint8_t*)buf, n, true);   // RETAINED: persists last push
  Serial.printf("result -> %s\n", buf);
}

// Device sensors (battery %, voltage, RSSI, firmware) on a RETAINED topic, so HA
// always keeps the LAST GOOD reading -- a failed push (which may not re-read the
// advertisement) never blanks it, and it survives ESP/HA restarts. Published only
// when we actually have a valid reading; otherwise the retained value persists.
static void publishBattery() {
  if (g_battPct < 0) return;                     // never read yet -> keep retained value
  char buf[160];
  int n = snprintf(buf, sizeof(buf),
      "{\"batt_pct\":%d,\"batt_v\":%.1f,\"rssi\":%d,\"fw\":\"0x%04X\"}",
      g_battPct, g_battV, g_rssi, g_fw);
  mqtt.publish(TOPIC_BATTERY, (const uint8_t*)buf, n, true);   // RETAINED
  Serial.printf("battery -> %s\n", buf);
}

// ----------------------------- BLE push (proven) ----------------------------
static bool blePush(uint32_t payloadLen, uint32_t &out_total_ms,
                    uint32_t &out_mean_us, uint32_t &out_parts, uint8_t &out_done) {
  out_total_ms = 0; out_mean_us = 0; out_parts = 0; out_done = 0;
  const uint32_t nparts = (payloadLen + PART_SIZE - 1) / PART_SIZE;

  File f = LittleFS.open(PAYLOAD_FILE, "r");
  if (!f) { Serial.println("cannot open payload file"); g_lastReason = "no_file"; return false; }

  // Frame ver byte (offset 4) selects the payload FORMAT: 1 = per-column (normal),
  // 2 = compression2 (QuickLZ). This only changes the 02 SIZE command below; the
  // part loop is identical for both.
  uint8_t verByte = 1; f.seek(4); f.read(&verByte, 1);
  const bool compress2 = (verByte == 2);
  g_lastCompress2 = compress2;
  Serial.printf("payload format: %s (ver %u)\n",
                compress2 ? "compression2/QuickLZ" : "per-column", verByte);

  NimBLEClient* client = nullptr;
  NimBLERemoteCharacteristic* cmd = nullptr;
  NimBLERemoteCharacteristic* img = nullptr;

  for (uint32_t attempt = 1; ; attempt++) {
    NimBLEScan* scan = NimBLEDevice::getScan();
    scan->setActiveScan(true); scan->setInterval(45); scan->setWindow(15);
    NimBLEScanResults res = scan->start(6, false);
    NimBLEAdvertisedDevice* panel = nullptr;
    for (int i = 0; i < res.getCount(); i++) {
      NimBLEAdvertisedDevice d = res.getDevice(i);
      if (strcasecmp(d.getAddress().toString().c_str(), TARGET_MAC) == 0) {
        panel = new NimBLEAdvertisedDevice(d);
        g_addrType = d.getAddress().getType();   // remember for direct connects
        // Battery + firmware are in the advertisement (manufacturer data, company
        // 0x5053): payload = company(2) + [id_lo, batt, fw_hi, fw_lo, id_hi].
        // RSSI comes from the advertisement directly. No connection needed.
        g_rssi = d.getRSSI();
        std::string md = d.getManufacturerData();
        if (md.size() >= 4 && (uint8_t)md[0] == 0x53 && (uint8_t)md[1] == 0x50) {
          g_battV = (uint8_t)md[3] / 10.0f;
          float pct = (g_battV - BATT_MIN_V) * 100.0f / (BATT_MAX_V - BATT_MIN_V);
          if (pct < 0) pct = 0; if (pct > 100) pct = 100;
          g_battPct = (int)(pct + 0.5f);
          if (md.size() >= 6) g_fw = ((uint16_t)(uint8_t)md[4] << 8) | (uint8_t)md[5];
          Serial.printf("panel battery: %.1f V (%d%%), fw 0x%04X, rssi %d\n",
                        g_battV, g_battPct, g_fw, g_rssi);
        }
        break;
      }
    }
    bool ready = false;
    client = NimBLEDevice::createClient();
    if (!client) {
      // Should be impossible now that every path releases its client, but if
      // the slots are ever exhausted anyway, a self-restart beats a dead bridge.
      Serial.println("createClient FAILED (no free client slots) -> restarting ESP");
      f.close();
      delay(200);
      ESP.restart();
    }
    if (compress2) client->setConnectionParams(QLZ_CI_MIN, QLZ_CI_MAX, CI_LAT, CI_TMO);
    else           client->setConnectionParams(RAW_CI_MIN, RAW_CI_MAX, CI_LAT, CI_TMO);
    client->setConnectTimeout(10);
    bool connected = false;
    if (panel) {
      connected = client->connect(panel);
      delete panel; panel = nullptr;
    } else {
      // Scan miss. A panel stuck mid-session often does NOT advertise (it still
      // thinks it is connected to the previous central) -- but it usually still
      // accepts a DIRECT connection by address. Windows/bleak connects by MAC
      // without requiring a scan hit, which is why the laptop could revive
      // panels this bridge could not even find.
      uint8_t t = (g_addrType >= 0) ? (uint8_t)g_addrType : addrTypeFromMac(TARGET_MAC);
      Serial.printf("scan miss -> direct connect to %s (addr type %u)\n", TARGET_MAC, t);
      connected = client->connect(NimBLEAddress(std::string(TARGET_MAC), t));
    }
    if (connected) {
      if (compress2) client->updateConnParams(QLZ_CI_MIN, QLZ_CI_MAX, CI_LAT, CI_TMO);
      else           client->updateConnParams(RAW_CI_MIN, RAW_CI_MAX, CI_LAT, CI_TMO);
      // Data Length Extension: compression2 parts are 244-byte write commands;
      // without DLE each one fragments into ~10 x 27-byte LL packets (long
      // airtime = wide corruption window). Windows negotiates DLE automatically
      // -- do the same. Harmless no-op if the panel's radio doesn't support it.
      // (NimBLE-Arduino 2.x alternative: client->setDataLen(251);)
      ble_gap_set_data_len(client->getConnId(), 251, 2120);
      delay(300);
      struct ble_gap_conn_desc desc;
      if (ble_gap_conn_find(client->getConnId(), &desc) == 0)
        Serial.printf("[try %u] connected MTU=%d  interval %.2f ms  sup.timeout %u ms\n",
                      attempt, client->getMTU(), desc.conn_itvl * 1.25,
                      (unsigned)desc.supervision_timeout * 10);
      NimBLERemoteService* svc = client->getService(SVC_UUID);
      if (svc) {
        cmd = svc->getCharacteristic(CMD_UUID);
        img = svc->getCharacteristic(IMG_UUID);
        if (cmd && img && cmd->subscribe(true, notifyCB)) ready = true;
      }
    }
    if (ready) { Serial.printf("panel READY on attempt %u\n", attempt); break; }
    bleDrop(client);
    if (attempt >= READY_TRIES) { Serial.println("panel never became ready"); g_lastReason = "no_panel"; f.close(); return false; }
    delay(READY_WAIT_MS);
  }

  // Let a COLD/sleeping panel's display controller fully wake before we start.
  // Mature stacks (laptop/phone) are slow between connect and first write, which
  // happens to wake the panel; NimBLE is fast and can beat a cold panel -> the
  // first commit corrupts. This settle delay mimics that warm-up.
  Serial.printf("settling %u ms (cold-panel wake)...\n", WAKE_DELAY_MS);
  delay(WAKE_DELAY_MS);

  // Flush stale notifications NOW, right before the handshake (was: before the
  // settle delay). A panel abandoned mid-transfer by an earlier aborted push can
  // immediately resume its old-session part requests on reconnect; if one of
  // those lands during the settle delay and gets consumed as the 0x01 reply,
  // the whole handshake runs one step out of sync and the panel digs deeper
  // into its stuck state ("strange things" -> power-cycle ritual).
  notifFlush();

  // compression2 parts go out as 244-byte write commands, which require
  // MTU >= 247. We request 247 at init; verify the panel granted it.
  if (compress2 && client->getMTU() < 247)
    Serial.printf("WARNING: MTU=%d < 247 -- compression2 write commands will not fit!\n",
                  client->getMTU());

  // Handshake 0x01 with RESYNC: writing 0x01 resets the panel's state machine,
  // so if the reply is a stale 05xx part request instead of an 01xx ack, just
  // drain it and issue 0x01 again -- this recovers a panel stuck mid-session.
  bool hs01 = false, gotAnyNotif = false;
  for (uint8_t hsTry = 1; hsTry <= 3 && !hs01; hsTry++) {
    uint8_t c01 = 0x01;
    cmd->writeValue(&c01, 1, false);
    if (!waitNotif(NOTIF_TO_MS)) break;
    gotAnyNotif = true;
    if (g_snapLen >= 1 && g_snap[0] == 0x01) hs01 = true;
    else {
      Serial.printf("stale notif %02x.. during 01 handshake -> resync (%u/3)\n",
                    g_snapLen ? g_snap[0] : 0, hsTry);
      delay(300);
      notifFlush();
    }
  }
  if (!hs01) {
    if (!gotAnyNotif) {   // panel truly silent -> nothing more to do this attempt
      Serial.println("timeout 01 (panel silent)"); g_lastReason = "hs01";
      bleDrop(client); f.close(); return false;
    }
    // Panel answers but only with stale 05xx requests: PROCEED ANYWAY, exactly
    // like the Windows/bleak script -- it never validates the 01/02 acks, and
    // it is the sender that heals stuck panels. 0x02 re-arms size/format and
    // the part loop then serves whatever the panel asks; COMPLETING a transfer
    // is what actually resets a wedged panel.
    Serial.println("01 ack never seen but panel is talking -> proceeding laptop-style");
  }
  // SIZE command signals the FORMAT to the panel (Wireshark-confirmed):
  //   compression2 (QuickLZ): 6 bytes  [02][4B len LE][01]
  //   per-column   (normal) : 8 bytes  [02][4B len LE][00 00 00]
  uint8_t c02[8]; c02[0] = 0x02;
  c02[1] = payloadLen & 0xFF; c02[2] = (payloadLen >> 8) & 0xFF;
  c02[3] = (payloadLen >> 16) & 0xFF; c02[4] = (payloadLen >> 24) & 0xFF;
  size_t c02len;
  if (compress2) { c02[5] = 0x01; c02len = 6; }
  else           { c02[5] = c02[6] = c02[7] = 0x00; c02len = 8; }
  cmd->writeValue(c02, c02len, false);
  if (!waitNotif(NOTIF_TO_MS)) { Serial.println("timeout 02"); g_lastReason = "hs02"; bleDrop(client); f.close(); return false; }
  uint8_t c03 = 0x03;
  cmd->writeValue(&c03, 1, false);
  if (!waitNotif(NOTIF_TO_MS)) { Serial.println("timeout 03"); g_lastReason = "hs03"; bleDrop(client); f.close(); return false; }

  uint8_t pkt[4 + PART_SIZE];
  uint64_t rtt_sum = 0; uint32_t rtt_cnt = 0;
  uint32_t notif_timeouts = 0;
  uint32_t weird_notifs = 0;
  uint32_t t0 = millis();
  while (true) {
    if (g_snapLen >= 2 && g_snap[0] == 0x05 && g_snap[1] != 0x00) {
      out_done = g_snap[1];
      out_total_ms = millis() - t0; out_parts = rtt_cnt;
      out_mean_us = rtt_cnt ? (uint32_t)(rtt_sum / rtt_cnt) : 0;
      Serial.printf("DONE 05%02x  parts=%u  total=%.1fs  mean=%.1fms\n",
                    out_done, rtt_cnt, out_total_ms / 1000.0, out_mean_us / 1000.0);
      const bool done_ok = out_done == 0x08 || out_done == 0x03;   // both are success codes
      g_lastReason = done_ok ? "ok" : "done_code";
      delay(DONE_HOLD_MS);   // let the panel decompress + refresh before disconnect
      bleDrop(client); f.close();
      return done_ok;
    }
    if (g_snapLen < 6 || g_snap[0] != 0x05) {
      // Do NOT abort here like before -- the laptop script tolerates anything
      // and just keeps serving, which is precisely how it un-wedges panels.
      // Log it, skip it, wait for the next (real) part request.
      if (++weird_notifs <= 5) {
        Serial.printf("odd notif (len %u, %02x..) -> ignoring (%u/5)\n",
                      (unsigned)g_snapLen, g_snapLen ? g_snap[0] : 0, weird_notifs);
        if (!waitNotif(NOTIF_TO_MS)) {
          Serial.println("silence after odd notif"); g_lastReason = "bad_notif";
          bleDrop(client); f.close(); return false;
        }
        continue;
      }
      Serial.println("too many odd notifs"); g_lastReason = "bad_notif";
      bleDrop(client); f.close(); return false;
    }
    uint32_t part = (uint32_t)g_snap[2] | ((uint32_t)g_snap[3] << 8) |
                    ((uint32_t)g_snap[4] << 16) | ((uint32_t)g_snap[5] << 24);
    uint32_t off = part * PART_SIZE;
    if (off >= payloadLen) {
      // Panel asked one part PAST the end = it has every byte. For compression2
      // it now DECOMPRESSES and commits -- do NOT slam the connection shut at
      // exactly that moment (this is why compress2 pushes transferred fine but
      // never refreshed: per-column ends via the 05xx done branch above and got
      // DONE_HOLD; this exit disconnected instantly). Wait for a possible final
      // 05xx status, then hold the link for the commit, like the done branch.
      Serial.printf("part %u past end (payload %u) -> waiting for final status\n",
                    part, payloadLen);
      if (waitNotif(5000) && g_snapLen >= 2 && g_snap[0] == 0x05) {
        out_done = g_snap[1];
        Serial.printf("final status 05%02x\n", out_done);
      } else {
        Serial.println("no final status notif (panel may signal done only via past-end)");
      }
      out_total_ms = millis() - t0; out_parts = rtt_cnt;
      const bool past_ok = out_done ? (out_done == 0x08 || out_done == 0x03) : true;
      g_lastReason = past_ok ? "ok" : "done_code";
      delay(DONE_HOLD_MS);   // commit grace: PC/HA give this implicitly via slow teardown
      bleDrop(client); f.close();
      return past_ok;
    }
    uint32_t len = payloadLen - off; if (len > PART_SIZE) len = PART_SIZE;
    // Give the panel breathing room between its request and our part -- the
    // Windows stack does this by accident and is the only always-clean sender.
    // Applies to BOTH formats (per-column showed noise too when answered fast).
    if (REPLY_DELAY_MS) delay(REPLY_DELAY_MS);
    pkt[0] = part & 0xFF; pkt[1] = (part >> 8) & 0xFF;
    pkt[2] = (part >> 16) & 0xFF; pkt[3] = (part >> 24) & 0xFF;
    f.seek(HEADER_SIZE + off);
    f.read(pkt + 4, len);
    // WRITE TYPE IS FORMAT-SPECIFIC (field-verified, do not "fix" this again):
    //   compression2 -> write WITHOUT response. Every sender that ever made this
    //     format refresh (PC standalone, HA) used write commands. A with-response
    //     push transfers cleanly and even returns DONE 0508, but the panel NEVER
    //     refreshes -- its streaming decompressor apparently only runs on the
    //     write-command path. Needs MTU >= 247 (we request 247; check the log).
    //   per-column -> write WITH response (proven on this bridge): ATT-acked,
    //     works at any MTU, failures visible.
    // Both paths check writeValue's return and retry (NimBLE returns false on a
    // congested/oversized write command too).
    bool wrote = false;
    for (uint8_t wtry = 1; wtry <= 3 && !wrote; wtry++) {
      wrote = img->writeValue(pkt, 4 + len, !compress2);
      if (!wrote) { Serial.printf("write failed part %u (try %u/3)\n", part, wtry); delay(50); }
    }
    if (!wrote) {
      Serial.printf("giving up: part %u would not write\n", part);
      g_lastReason = "write"; out_parts = rtt_cnt; out_total_ms = millis() - t0;
      bleDrop(client); f.close(); return false;
    }
    if (compress2 && PACE_QLZ_MS) delay(PACE_QLZ_MS);   // WNR needs pacing; with-response self-paces

    uint32_t t_send = micros();
    if ((part % 64) == 0) Serial.printf("part %u/%u\n", part, nparts);
    if (!waitNotif(NOTIF_TO_MS)) {
      // The protocol is request-driven and resumable: a lost notification does
      // not have to kill the push. g_snap still holds the request for THIS
      // part, so 'continue' re-parses it and retransmits the same part.
      if (++notif_timeouts <= 3) {
        Serial.printf("notif timeout at part %u -> resending (%u/3)\n", part, notif_timeouts);
        continue;
      }
      Serial.printf("timeout after part %u\n", part); g_lastReason = "transfer"; out_parts = rtt_cnt; out_total_ms = millis() - t0; bleDrop(client); f.close(); return false;
    }
    notif_timeouts = 0;
    rtt_sum += (micros() - t_send); rtt_cnt++;
  }
}

// ----------------------------- chunk reassembly (MQTT callback) -------------
static void onMqttMessage(char* topic, byte* payload, unsigned int length) {
  if (strcmp(topic, TOPIC_RESTART) == 0) {   // remote un-wedge: reboot the ESP
    g_restartReq = true;
    return;
  }
  if (strcmp(topic, TOPIC_PUSH) != 0) return;
  if (length < 6) return;                          // need [seq:4][nchunks:2]
  uint32_t seq    = (uint32_t)payload[0] | ((uint32_t)payload[1] << 8) |
                    ((uint32_t)payload[2] << 16) | ((uint32_t)payload[3] << 24);
  uint16_t nchunks = (uint16_t)payload[4] | ((uint16_t)payload[5] << 8);
  const uint8_t* data = payload + 6;
  unsigned int dlen = length - 6;

  if (seq == 0) {                                  // first chunk -> (re)start
    g_rxFile = LittleFS.open(PAYLOAD_FILE, "w");
    g_rxActive = (bool)g_rxFile;
    g_rxChunks = nchunks; g_rxCount = 0;
    if (!g_rxActive) { Serial.println("ERR: open for write failed"); return; }
    Serial.printf("receiving %u chunks...\n", nchunks);
  }
  if (!g_rxActive) return;                          // ignore stray chunk w/o a begin
  if (seq != g_rxCount) {                           // out-of-order -> abort (md5 would fail)
    Serial.printf("ERR: chunk out of order (got %u, expected %u)\n", seq, g_rxCount);
    g_rxFile.close(); g_rxActive = false; return;
  }
  g_rxFile.write(data, dlen);
  g_rxCount++; g_rxLastMs = millis();
  if (g_rxCount >= g_rxChunks) {
    g_rxFile.close(); g_rxActive = false;
    Serial.println("all chunks received; will verify in loop");
    g_verifyReady = true;
  }
}

// ----------------------------- MQTT connect ---------------------------------
static void mqttEnsureConnected() {
  if (mqtt.connected()) return;
  Serial.print("MQTT connecting...");
  String cid = "gicisky-" + String(PANEL_ID) + "-" + String((uint32_t)ESP.getEfuseMac(), HEX);
  if (mqtt.connect(cid.c_str(), MQTT_USER, MQTT_PASS, TOPIC_STATUS, 1, true, "offline")) {
    Serial.println(" ok");
    mqtt.subscribe(TOPIC_PUSH, 1);
    mqtt.subscribe(TOPIC_RESTART, 1);
    mqtt.publish(TOPIC_STATUS, (const uint8_t*)"online", 6, true);
    Serial.printf("subscribed: %s\n", TOPIC_PUSH);
  } else {
    Serial.printf(" failed rc=%d (retry)\n", mqtt.state());
  }
}

// ----------------------------- setup / loop ---------------------------------
void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n=== Gicisky ESP32 MQTT->BLE bridge (PubSubClient) ===");

  snprintf(TOPIC_PUSH,   sizeof(TOPIC_PUSH),   "gicisky/%s/cmd/push", PANEL_ID);
  snprintf(TOPIC_RESULT, sizeof(TOPIC_RESULT), "gicisky/%s/result",   PANEL_ID);
  snprintf(TOPIC_STATUS, sizeof(TOPIC_STATUS), "gicisky/%s/status",   PANEL_ID);
  snprintf(TOPIC_BATTERY,sizeof(TOPIC_BATTERY),"gicisky/%s/battery",  PANEL_ID);
  snprintf(TOPIC_RESTART,sizeof(TOPIC_RESTART),"gicisky/%s/cmd/restart", PANEL_ID);

  if (!LittleFS.begin(true)) Serial.println("LittleFS mount failed!");

  NimBLEDevice::init("");
  NimBLEDevice::setMTU(247);
  NimBLEDevice::setPower(ESP_PWR_LVL_P9);          // DEFAULT TX power = max (+9 dBm)
  // Belt-and-suspenders: pin the CONNECTION TX power (where the 240-byte parts
  // flow) and scan to max too, in case DEFAULT doesn't cover every handle.
  // NOTE: this boosts only ESP->panel. The panel->ESP direction (its notifs/ACKs)
  // is set by the battery panel and cannot be raised here.
  for (int h = ESP_BLE_PWR_TYPE_CONN_HDL0; h <= ESP_BLE_PWR_TYPE_CONN_HDL8; h++)
    esp_ble_tx_power_set((esp_ble_power_type_t)h, ESP_PWR_LVL_P9);
  esp_ble_tx_power_set(ESP_BLE_PWR_TYPE_SCAN, ESP_PWR_LVL_P9);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("WiFi connecting to %s", WIFI_SSID);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) { delay(300); Serial.print("."); }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) Serial.printf("WiFi OK, IP %s\n", WiFi.localIP().toString().c_str());
  else Serial.println("WiFi FAILED (will keep retrying)");

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setBufferSize(MQTT_BUF);
  mqtt.setKeepAlive(120);
  mqtt.setCallback(onMqttMessage);
  mqttEnsureConnected();
}

void loop() {
  if (g_restartReq) {
    Serial.println("restart requested over MQTT");
    if (mqtt.connected()) {
      mqtt.publish(TOPIC_STATUS, (const uint8_t*)"restarting", 10, true);
      mqtt.loop();
    }
    delay(300);
    ESP.restart();
  }
  if (WiFi.status() != WL_CONNECTED) { WiFi.begin(WIFI_SSID, WIFI_PASS); delay(500); }
  mqttEnsureConnected();
  mqtt.loop();

  // incomplete transfer timeout (lost chunk)
  if (g_rxActive && millis() - g_rxLastMs > 8000) {
    Serial.println("ERR: chunk stream timed out (incomplete) -> reset");
    g_rxFile.close(); g_rxActive = false;
    g_lastReason = "rx_timeout";
    publishResult(false, 0, 0, 0, 0, false, 0);
  }

  if (g_verifyReady) {
    g_verifyReady = false;
    File f = LittleFS.open(PAYLOAD_FILE, "r");
    bool ok_hdr = f && f.size() >= HEADER_SIZE;
    uint8_t hdr[HEADER_SIZE]; uint32_t plen = 0; bool md5ok = false;
    if (ok_hdr) {
      f.read(hdr, HEADER_SIZE); f.close();
      if (memcmp(hdr, "GCSK", 4) == 0 && (hdr[4] == 1 || hdr[4] == 2)) {
        g_lastCompress2 = (hdr[4] == 2);
        plen = (uint32_t)hdr[5] | ((uint32_t)hdr[6] << 8) | ((uint32_t)hdr[7] << 16) | ((uint32_t)hdr[8] << 24);
        md5ok = verifyPayloadMd5(hdr + 9, plen);
      }
    } else if (f) f.close();

    if (!md5ok) {
      Serial.println("ERR: md5 mismatch / bad header -> refusing to push");
      g_lastReason = "md5";
      publishResult(false, 0, 0, 0, 0, false, 0);
      return;
    }
    Serial.printf("md5 OK, payload %u bytes  (free heap %u, min ever %u)\n",
                  plen, ESP.getFreeHeap(), ESP.getMinFreeHeap());

    uint32_t since = millis() - g_lastCommitMs;
    if (g_lastCommitMs != 0 && since < RECOVERY_MS) {
      uint32_t wait = RECOVERY_MS - since;
      Serial.printf("waiting %u ms recovery...\n", wait);
      uint32_t w0 = millis();
      while (millis() - w0 < wait) { mqtt.loop(); delay(50); }
    }

    Serial.println("starting BLE push...");
    // WiFi/BLE coexistence on the shared radio intermittently corrupts the BLE
    // transfer (the noise). The payload is already fully on LittleFS, so WiFi is
    // not needed during the push -> disconnect WiFi to give BLE the radio alone
    // (matching the clean standalone test), then reconnect to report the result.
    Serial.println("quiescing WiFi for the BLE push...");
    WiFi.disconnect(true);
    esp_wifi_stop();
    delay(200);

    // Auto-retry the WHOLE transfer on a DETECTABLE failure (write gave up, notif
    // timeout, connection lost, or a non-0508 done code). A clean retry beats
    // pushing a half-delivered image -> protects against the panel locking up.
    // (Note: this cannot detect a transfer that completed 0508 but with on-air
    //  noise -- that's a link-quality problem; see CI_*/antenna.)
    uint32_t total_ms = 0, mean_us = 0, parts = 0; uint8_t done = 0;
    bool ok = false;
    uint32_t pushStart = millis();                 // measure how long we TRY (incl. retries)
    for (uint8_t attempt = 1; attempt <= PUSH_TRIES && !ok; attempt++) {
      if (attempt > 1) {
        Serial.printf("push attempt %u failed -> waiting %u ms before retry\n",
                      attempt - 1, RECOVERY_MS);
        uint32_t w0 = millis();
        while (millis() - w0 < RECOVERY_MS) delay(50);   // WiFi stays off here
      }
      ok = blePush(plen, total_ms, mean_us, parts, done);
    }
    g_lastCommitMs = millis();
    uint32_t tried_ms = g_lastCommitMs - pushStart;

    // bring WiFi + MQTT back to report the result
    Serial.println("restoring WiFi...");
    esp_wifi_start();
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    uint32_t w0 = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - w0 < 15000) delay(200);
    mqttEnsureConnected();
    publishResult(ok, done, parts, mean_us, total_ms, true, tried_ms);
    publishBattery();   // retained device sensors (battery/voltage/rssi/fw)
  }

  delay(10);
}
