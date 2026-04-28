#include "esp_camera.h"
#include <WiFi.h>
#include <WebServer.h>

// ================== WIFI ==================
const char* ssid = "total";
const char* password = "123456789";

// ================== CAMERA PINS (AI THINKER) ==================
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27

#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

WebServer server(80);

// ================== CAMERA INIT ==================
void startCamera() {
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;

  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;

  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;

  config.pin_sscb_sda = SIOD_GPIO_NUM;
  config.pin_sscb_scl = SIOC_GPIO_NUM;

  config.pin_pwdn  = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;

  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;

  // 🔥 STABLE SETTINGS
  if (psramFound()) {
    config.frame_size = FRAMESIZE_QVGA;   // 320x240 (stable)
    config.jpeg_quality = 12;             // balanced quality/load
    config.fb_count = 1;                  // avoid FB-OVF on long runs
  } else {
    config.frame_size = FRAMESIZE_QVGA;
    config.jpeg_quality = 14;
    config.fb_count = 1;
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("❌ Camera init failed: 0x%x\n", err);
    return;
  }

  // 🔥 Sensor tuning
  sensor_t * s = esp_camera_sensor_get();
  s->set_framesize(s, FRAMESIZE_QVGA);
  s->set_quality(s, psramFound() ? 12 : 14);
  s->set_brightness(s, 1);
  s->set_contrast(s, 1);
  s->set_saturation(s, 0);
  s->set_sharpness(s, 1);
  s->set_dcw(s, 1);                 // enable downsize path for stability
  s->set_gain_ctrl(s, 1);
  s->set_gainceiling(s, GAINCEILING_8X);
  s->set_exposure_ctrl(s, 1);
  s->set_ae_level(s, 1);
  s->set_aec_value(s, 400);

  Serial.println("✅ Camera initialized");
}

// ================== STREAM ==================
void handleStream() {
  WiFiClient client = server.client();

  Serial.println("📡 Client connected");

  String response =
    "HTTP/1.1 200 OK\r\n"
    "Content-Type: multipart/x-mixed-replace; boundary=frame\r\n"
    "Access-Control-Allow-Origin: *\r\n\r\n";

  client.print(response);

  while (client.connected()) {

    camera_fb_t * fb = esp_camera_fb_get();
    if (!fb) {
      Serial.println("⚠ Capture failed");
      delay(30);
      continue;
    }

    client.print("--frame\r\n");
    client.print("Content-Type: image/jpeg\r\n");
    client.printf("Content-Length: %u\r\n\r\n", fb->len);

    client.write(fb->buf, fb->len);
    client.print("\r\n");

    esp_camera_fb_return(fb);

    delay(100);   // ~10fps max, easier on buffers
    yield();      // ✅ keep this

    // 🔥 CRITICAL FIX
    if (!client.connected()) break;
  }

  client.stop();   // 🔥 VERY IMPORTANT
  Serial.println("❌ Client disconnected");
}

// ================== SETUP ==================
void setup() {
  Serial.begin(115200);
  Serial.println("\n🚀 Booting...");

  WiFi.mode(WIFI_STA);
  WiFi.disconnect(true);
  delay(1000);

  WiFi.begin(ssid, password);
  Serial.print("🔌 Connecting");

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  Serial.println("\n✅ WiFi Connected");
  Serial.print("📡 IP Address: ");
  Serial.println(WiFi.localIP());

  startCamera();

  server.on("/stream", HTTP_GET, handleStream);

  server.begin();
  Serial.println("🚀 Server started");
}

// ================== LOOP ==================
void loop() {
  server.handleClient();
}
