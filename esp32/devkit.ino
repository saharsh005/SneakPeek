/*
  SneakPeek — ESP32 DevKit V1 Firmware
  =====================================
  Responsibilities:
    1. Read PIR (HC-SR501)   → GPIO14  — digital, sustained motion detection
    2. Read MQ-2 smoke       → GPIO35  — analog, gas/smoke PPM estimation
    3. Read LDR              → GPIO34  — analog, ambient light (day/night)
    4. Receive JPEG frames from ESP32-CAM over UART2 (GPIO16/17)
    5. Forward everything to the laptop over USB serial (UART0) in the
       exact JSON+binary protocol that receiver.py expects

  Serial protocol to laptop (UART0 @ 115200):
    Sensor packet (every 500ms, newline-delimited JSON):
        {"type":"sensor","motion":1,"smoke_ppm":145.2,"ldr":3100}

    Frame packet (on motion trigger, after receiving JPEG from CAM):
        {"type":"frame","size":18432}
        <raw 18432 bytes of JPEG>

  UART2 protocol from ESP32-CAM (GPIO16 RX, GPIO17 TX @ 115200):
    CAM sends when commanded:
        4-byte big-endian size header: e.g. 0x00 0x00 0x48 0x00 = 18432
        followed by raw JPEG bytes

  Pin assignments:
    GPIO14  → PIR OUT
    GPIO34  → LDR midpoint (analog, input-only)
    GPIO35  → MQ-2 AO (analog, input-only)
    GPIO16  → UART2 RX (receives from CAM TX/UOT)
    GPIO17  → UART2 TX (sends capture command to CAM RX/UOR)
*/

#include <Arduino.h>
#include <ArduinoJson.h>        // Install via Library Manager: "ArduinoJson" by Benoit Blanchon
#include <WiFi.h>
#include <HTTPClient.h>

// ── Pin definitions ───────────────────────────────────────────────────
#define PIN_PIR       14
#define PIN_LDR       34
#define PIN_MQ2       35
#define PIN_CAM_RX    16        // UART2 RX — wire to CAM UOT (TX)
#define PIN_CAM_TX    17        // UART2 TX — wire to CAM UOR (RX)

// ── Thresholds (mirror config.json values) ────────────────────────────
#define SMOKE_PPM_DANGER     300.0f   // above this = smoke alert
#define LDR_NIGHT_MAX        400      // below this = night (0=dark,4095=bright)
#define MOTION_SUSTAIN_MS    2000     // ms of continuous motion before capture
#define SENSOR_INTERVAL_MS   500      // how often to send sensor JSON to laptop
#define MAX_FRAME_SIZE       60000    // safety cap on JPEG size (bytes)
#define CAM_CAPTURE_CMD      'C'      // single byte sent to CAM to trigger capture
#define CAM_TIMEOUT_MS       5000     // max wait for CAM to respond
#define HTTP_POST_TIMEOUT_MS 3000

// Wi-Fi + backend endpoint (set to the machine running python main.py)
const char* WIFI_SSID = "total";
const char* WIFI_PASS = "123456789";
const char* SENSOR_API_URL = "http://10.224.162.73:5000/api/sensor/event";

// ── UART2 for camera communication ────────────────────────────────────
HardwareSerial CamSerial(2);

// ── State ─────────────────────────────────────────────────────────────
bool     motionActive       = false;
uint32_t motionStartMs      = 0;
bool     motionSustained    = false;
uint32_t lastSensorSendMs   = 0;
bool     cameraReady        = false;
uint32_t lastWifiLogMs      = 0;

// ── MQ-2 PPM estimation ───────────────────────────────────────────────
// The MQ-2 outputs an analogue voltage proportional to gas concentration.
// We use a simplified curve fit: not calibrated in true PPM but gives a
// consistent relative reading (0–4095 ADC → ~0–600 "ppm equivalent").
// Real calibration requires clean-air baseline — see comments in readSmokePPM().
float readSmokePPM() {
  int raw = analogRead(PIN_MQ2);
  // Simplified linear map — replace with proper Rs/R0 curve for calibration
  // At clean air: raw ≈ 200-400. At smoke: raw > 1000+
  float voltage = (raw / 4095.0f) * 3.3f;
  float ppm     = voltage * 200.0f;   // rough approximation
  return ppm;
}

// ── Sensor JSON → laptop ──────────────────────────────────────────────
void sendSensorPacket(bool motion, float smokePpm, int ldr) {
  StaticJsonDocument<128> doc;
  doc["type"]      = "sensor";
  doc["motion"]    = motion ? 1 : 0;
  doc["smoke_ppm"] = round(smokePpm * 10.0f) / 10.0f;  // 1 decimal place
  doc["ldr"]       = ldr;
  doc["is_night"]  = (ldr < LDR_NIGHT_MAX) ? 1 : 0;
  serializeJson(doc, Serial);
  Serial.println();   // newline terminates the JSON line for receiver.py readline()

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println(F("[HTTP] Skip POST: WiFi disconnected"));
    return;
  }

  String payload;
  serializeJson(doc, payload);

  HTTPClient http;
  WiFiClient client;
  http.setTimeout(HTTP_POST_TIMEOUT_MS);
  if (!http.begin(client, SENSOR_API_URL)) {
    Serial.println(F("[HTTP] begin() failed"));
    return;
  }
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(payload);
  if (code > 0) {
    Serial.printf("[HTTP] POST %s => %d\n", SENSOR_API_URL, code);
  } else {
    Serial.printf("[HTTP] FAIL code=%d WiFi=%d RSSI=%d URL=%s\n",
                  code, WiFi.status(), WiFi.RSSI(), SENSOR_API_URL);
  }
  http.end();
}

void ensureWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  uint32_t now = millis();
  if (now - lastWifiLogMs > 2000) {
    Serial.printf("[WiFi] reconnecting... status=%d\n", WiFi.status());
    lastWifiLogMs = now;
  }
  WiFi.disconnect(false, false);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  uint32_t deadline = millis() + 4000;
  while (WiFi.status() != WL_CONNECTED && millis() < deadline) {
    delay(200);
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[WiFi] connected IP=%s RSSI=%d\n",
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
  }
}

// ── Request and forward a JPEG frame from the CAM ────────────────────
void captureAndForwardFrame() {
  // 1. Flush any stale bytes in CAM buffer
  while (CamSerial.available()) CamSerial.read();

  // 2. Send capture command to CAM
  CamSerial.write(CAM_CAPTURE_CMD);
  CamSerial.flush();

  // 3. Wait for 4-byte big-endian size header from CAM
  uint32_t deadline = millis() + CAM_TIMEOUT_MS;
  while (CamSerial.available() < 4) {
    if (millis() > deadline) {
      Serial.println(F("{\"type\":\"error\",\"msg\":\"CAM timeout\"}"));
      return;
    }
    delay(10);
  }

  uint32_t frameSize = 0;
  frameSize |= ((uint32_t)CamSerial.read()) << 24;
  frameSize |= ((uint32_t)CamSerial.read()) << 16;
  frameSize |= ((uint32_t)CamSerial.read()) << 8;
  frameSize |= ((uint32_t)CamSerial.read());

  if (frameSize == 0 || frameSize > MAX_FRAME_SIZE) {
    StaticJsonDocument<64> err;
    err["type"] = "error";
    err["msg"]  = "bad frame size";
    err["size"] = frameSize;
    serializeJson(err, Serial);
    Serial.println();
    return;
  }

  // 4. Send frame header JSON to laptop — receiver.py reads this first
  StaticJsonDocument<64> hdr;
  hdr["type"] = "frame";
  hdr["size"] = frameSize;
  serializeJson(hdr, Serial);
  Serial.println();

  // 5. Stream raw JPEG bytes from CAM → USB serial to laptop
  // Read in 256-byte chunks to avoid stack overflow
  uint8_t  buf[256];
  uint32_t remaining = frameSize;
  deadline = millis() + CAM_TIMEOUT_MS;

  while (remaining > 0) {
    if (millis() > deadline) {
      // Partial frame — receiver.py will discard it on size mismatch
      break;
    }
    uint32_t toRead  = min((uint32_t)sizeof(buf), remaining);
    uint32_t got     = CamSerial.readBytes(buf, toRead);
    if (got > 0) {
      Serial.write(buf, got);
      remaining -= got;
    }
  }
  Serial.flush();
}

// ─────────────────────────────────────────────────────────────────────
void setup() {
  // UART0 → laptop (USB serial)
  Serial.begin(115200);
  while (!Serial) delay(10);

  // UART2 → ESP32-CAM
  CamSerial.begin(115200, SERIAL_8N1, PIN_CAM_RX, PIN_CAM_TX);

  // Pins
  pinMode(PIN_PIR, INPUT);
  // GPIO34 and GPIO35 are input-only ADC pins — no pinMode needed

  // MQ-2 warm-up: needs ~20s after power-on for stable readings
  // We don't block here — first readings will be slightly inaccurate
  // but it normalises quickly
  analogReadResolution(12);   // 12-bit ADC → 0–4095

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("[WiFi] connecting to %s ...\n", WIFI_SSID);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) {
    delay(300);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[WiFi] connected IP=%s RSSI=%d\n",
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
  } else {
    Serial.printf("[WiFi] connect failed status=%d\n", WiFi.status());
  }

  Serial.println(F("{\"type\":\"boot\",\"msg\":\"SneakPeek DevKit ready\"}"));
}

// ─────────────────────────────────────────────────────────────────────
void loop() {
  ensureWiFi();
  uint32_t now = millis();

  // ── Read sensors ──────────────────────────────────────────────────
  bool  pirRaw   = digitalRead(PIN_PIR) == HIGH;
  float smokePpm = readSmokePPM();
  int   ldrRaw   = analogRead(PIN_LDR);
  bool  isNight  = ldrRaw < LDR_NIGHT_MAX;

  Serial.printf("[SENSOR] motion=%d smoke_ppm=%.1f ldr=%d night=%d\n",
                pirRaw ? 1 : 0, smokePpm, ldrRaw, isNight ? 1 : 0);

  // ── PIR sustained motion logic ────────────────────────────────────
  // The sensor itself determines what counts as real presence,
  // not the AI. Short blips are ignored here on the hardware level.
  if (pirRaw) {
    if (!motionActive) {
      // Rising edge — start timer
      motionActive    = true;
      motionStartMs   = now;
      motionSustained = false;
    } else if (!motionSustained && (now - motionStartMs >= MOTION_SUSTAIN_MS)) {
      // Motion has been continuous for long enough — trigger capture
      motionSustained = true;
      captureAndForwardFrame();
    }
  } else {
    // Motion ended — reset
    motionActive    = false;
    motionSustained = false;
    motionStartMs   = 0;
  }

  // ── Smoke bypass — send alert flag immediately ────────────────────
  // MQ-2 acts autonomously. If smoke is above threshold, we send a
  // sensor packet immediately (not waiting for next 500ms interval)
  // so receiver.py can fire a smoke alert without waiting for motion.
  if (smokePpm >= SMOKE_PPM_DANGER) {
    sendSensorPacket(pirRaw, smokePpm, ldrRaw);
    // Also trigger a frame capture regardless of motion
    // (smoke + camera = better evidence)
    captureAndForwardFrame();
    delay(200);   // small debounce
    return;
  }

  // ── Regular sensor heartbeat every 500ms ─────────────────────────
  if (now - lastSensorSendMs >= SENSOR_INTERVAL_MS) {
    sendSensorPacket(pirRaw, smokePpm, ldrRaw);
    lastSensorSendMs = now;
  }

  delay(50);  // ~20Hz polling loop
}
