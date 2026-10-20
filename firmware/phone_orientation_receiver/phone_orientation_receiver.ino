/*
  ESP32 Phone Orientation Receiver
  - ESP32 serves a web page to the phone
  - Phone streams deviceorientation + devicemotion over a WebSocket
  - ESP32 parses the data and prints it to Serial

  Libraries (Arduino Library Manager):
    - "ESPAsyncWebServer" by ESP32Async
    - "AsyncTCP"          by ESP32Async
*/

#include <WiFi.h>
#include <AsyncTCP.h>
#include <ESPAsyncWebServer.h>

// Keep local Wi-Fi credentials out of version control.
#include "wifi_config.h"

// ---------- Sensor state ----------
struct SensorData {
  float alpha, beta, gamma;   // degrees
  float ax, ay, az;           // m/s^2, including gravity
  uint32_t seq;               // packet counter from phone
  uint32_t receivedAt;        // millis() when last packet arrived
};

volatile bool newData = false;
SensorData sensor = {};
portMUX_TYPE sensorMux = portMUX_INITIALIZER_UNLOCKED;
uint32_t packetCount = 0;

AsyncWebServer server(80);
AsyncWebSocket ws("/ws");

// ---------- Web page ----------
const char PAGE[] PROGMEM = R"rawliteral(
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>ESP32 Orientation</title>
<style>
  body{margin:0;padding:16px;background:#0f1115;color:#e8eaed;font:15px/1.4 system-ui,sans-serif}
  h1{font-size:18px;margin:0 0 12px}
  .card{background:#1a1d24;border-radius:10px;padding:12px;margin-bottom:12px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:6px 12px}
  .k{color:#8a8f98}.v{font-family:ui-monospace,monospace;text-align:right}
  .ok{color:#3ecf8e}.bad{color:#ff5c5c}
  button{width:100%;padding:12px;border:0;border-radius:8px;background:#5b9cff;color:#fff;font-size:16px;margin-bottom:12px}
  .stage{height:180px;display:flex;align-items:center;justify-content:center;perspective:600px}
  .phone{width:75px;height:140px;border-radius:12px;background:linear-gradient(#5b9cff,#2a4f99);
         border:3px solid #cfd6e4;display:flex;justify-content:center;padding-top:6px;font-size:11px;font-weight:600}
  #log{font-family:ui-monospace,monospace;font-size:12px;max-height:120px;overflow:auto;white-space:pre-wrap;color:#8a8f98}
</style>
</head>
<body>
<h1>ESP32 Orientation Link</h1>
<div class="card grid">
  <span class="k">Secure context</span><span class="v" id="secure"></span>
  <span class="k">WebSocket</span><span class="v bad" id="wsStatus">connecting</span>
  <span class="k">Sent to ESP32</span><span class="v" id="sent">0</span>
  <span class="k">Send rate</span><span class="v" id="rate">-</span>
</div>
<button id="start">Start sensors</button>
<div class="card stage"><div class="phone" id="phone">TOP</div></div>
<div class="card grid">
  <span class="k">alpha</span><span class="v" id="alpha">-</span>
  <span class="k">beta</span><span class="v" id="beta">-</span>
  <span class="k">gamma</span><span class="v" id="gamma">-</span>
  <span class="k">accel x/y/z</span><span class="v" id="acc">-</span>
</div>
<div class="card"><div class="k">Log</div><div id="log"></div></div>

<script>
const $ = id => document.getElementById(id);
const log = m => { $("log").textContent = `[${new Date().toLocaleTimeString()}] ${m}\n` + $("log").textContent; };
const SEND_INTERVAL_MS = 50;   // 20 Hz to the ESP32

$("secure").textContent = isSecureContext ? "yes" : "NO - sensors blocked";
$("secure").className = "v " + (isSecureContext ? "ok" : "bad");

let ws, seq = 0, sent = 0, lastSent = 0, lastSendTime = 0;
let o = {a:0,b:0,g:0}, acc = {x:0,y:0,z:0};

function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`);
  ws.onopen  = () => { $("wsStatus").textContent = "connected"; $("wsStatus").className = "v ok"; log("WebSocket connected"); };
  ws.onclose = () => { $("wsStatus").textContent = "reconnecting"; $("wsStatus").className = "v bad"; setTimeout(connect, 1000); };
  ws.onerror = () => ws.close();
  ws.onmessage = e => log("ESP32: " + e.data);
}
connect();

setInterval(() => { $("rate").textContent = (sent - lastSent) + " Hz"; lastSent = sent; }, 1000);

function send() {
  const now = performance.now();
  if (now - lastSendTime < SEND_INTERVAL_MS) return;
  if (!ws || ws.readyState !== WebSocket.OPEN || ws.bufferedAmount > 1024) return;
  lastSendTime = now;
  // CSV: seq,alpha,beta,gamma,ax,ay,az
  ws.send([seq++, o.a, o.b, o.g, acc.x, acc.y, acc.z].map(n => typeof n === "number" && n % 1 ? n.toFixed(2) : n).join(","));
  $("sent").textContent = ++sent;
}

function onOrientation(e) {
  o = { a: e.alpha ?? 0, b: e.beta ?? 0, g: e.gamma ?? 0 };
  $("alpha").textContent = o.a.toFixed(1) + "°";
  $("beta").textContent  = o.b.toFixed(1) + "°";
  $("gamma").textContent = o.g.toFixed(1) + "°";
  $("phone").style.transform = `rotateX(${o.b}deg) rotateY(${-o.g}deg) rotateZ(${-o.a}deg)`;
  send();
}

function onMotion(e) {
  const g = e.accelerationIncludingGravity || {};
  acc = { x: g.x ?? 0, y: g.y ?? 0, z: g.z ?? 0 };
  $("acc").textContent = `${acc.x.toFixed(1)} / ${acc.y.toFixed(1)} / ${acc.z.toFixed(1)}`;
}

async function start() {
  try {
    if (typeof window.DeviceOrientationEvent?.requestPermission === "function")
      log("Orientation permission: " + await DeviceOrientationEvent.requestPermission());
    if (typeof window.DeviceMotionEvent?.requestPermission === "function")
      log("Motion permission: " + await DeviceMotionEvent.requestPermission());
  } catch (err) { log("Permission error: " + err); }

  // Keep the screen awake so the stream doesn't stop
  try { if ("wakeLock" in navigator) { await navigator.wakeLock.request("screen"); log("Screen wake lock on"); } }
  catch (err) { log("Wake lock failed: " + err.message); }

  addEventListener("deviceorientation", onOrientation);
  addEventListener("devicemotion", onMotion);
  $("start").textContent = "Streaming to ESP32"; $("start").disabled = true;
  log("Sensors started");
}
$("start").addEventListener("click", start);
</script>
</body>
</html>
)rawliteral";

// ---------- WebSocket handling ----------
void handleMessage(const char* msg) {
  SensorData d;
  int n = sscanf(msg, "%lu,%f,%f,%f,%f,%f,%f",
                 (unsigned long*)&d.seq, &d.alpha, &d.beta, &d.gamma,
                 &d.ax, &d.ay, &d.az);
  if (n != 7) {
    Serial.printf("Bad packet (%d fields): %s\n", n, msg);
    return;
  }
  d.receivedAt = millis();

  portENTER_CRITICAL(&sensorMux);
  sensor = d;
  packetCount++;
  newData = true;
  portEXIT_CRITICAL(&sensorMux);
}

void onWsEvent(AsyncWebSocket* server, AsyncWebSocketClient* client,
               AwsEventType type, void* arg, uint8_t* data, size_t len) {
  switch (type) {
    case WS_EVT_CONNECT:
      Serial.printf("Client #%u connected from %s\n", client->id(),
                    client->remoteIP().toString().c_str());
      client->text("hello from ESP32");
      break;

    case WS_EVT_DISCONNECT:
      Serial.printf("Client #%u disconnected\n", client->id());
      break;

    case WS_EVT_DATA: {
      AwsFrameInfo* info = (AwsFrameInfo*)arg;
      // Only handle complete, single-frame text messages
      if (info->final && info->index == 0 && info->len == len && info->opcode == WS_TEXT) {
        char buf[128];
        size_t n = min(len, sizeof(buf) - 1);
        memcpy(buf, data, n);
        buf[n] = '\0';
        handleMessage(buf);
      }
      break;
    }

    default:
      break;
  }
}

// ---------- Setup / loop ----------
void setup() {
  Serial.begin(115200);
  delay(200);

#if USE_AP
  WiFi.mode(WIFI_AP);
  WiFi.softAP(AP_SSID, AP_PASS);
  Serial.printf("\nAP started: %s  (password: %s)\n", AP_SSID, AP_PASS);
  Serial.printf("Open http://%s on the phone\n", WiFi.softAPIP().toString().c_str());
#else
  WiFi.mode(WIFI_STA);
  WiFi.begin(STA_SSID, STA_PASS);
  Serial.print("\nConnecting to Wi-Fi");
  while (WiFi.status() != WL_CONNECTED) { delay(300); Serial.print("."); }
  Serial.printf("\nOpen http://%s on the phone\n", WiFi.localIP().toString().c_str());
#endif

  ws.onEvent(onWsEvent);
  server.addHandler(&ws);

  server.on("/", HTTP_GET, [](AsyncWebServerRequest* req) {
    req->send(200, "text/html", PAGE);
  });

  server.begin();
}

void loop() {
  static uint32_t lastPrint = 0, lastCleanup = 0, lastCount = 0;

  // Print at 5 Hz so Serial doesn't choke
  if (millis() - lastPrint >= 200) {
    lastPrint = millis();

    SensorData d;
    bool fresh;
    uint32_t count;
    portENTER_CRITICAL(&sensorMux);
    d = sensor; fresh = newData; newData = false; count = packetCount;
    portEXIT_CRITICAL(&sensorMux);

    if (fresh) {
      Serial.printf("#%lu  a=%7.1f  b=%7.1f  g=%7.1f  |  acc=%6.2f %6.2f %6.2f  |  %lu pkt/s\n",
                    (unsigned long)d.seq, d.alpha, d.beta, d.gamma,
                    d.ax, d.ay, d.az, (unsigned long)(count - lastCount) * 5);
      lastCount = count;

      // ---- Use the data here, e.g. drive a servo from tilt ----
      // int angle = constrain(map((int)d.gamma, -90, 90, 0, 180), 0, 180);
      // myServo.write(angle);
    } else if (sensor.receivedAt && millis() - sensor.receivedAt > 2000) {
      Serial.println("No data for 2 s (phone screen off or disconnected?)");
      sensor.receivedAt = 0;
    }
  }

  if (millis() - lastCleanup > 1000) {
    lastCleanup = millis();
    ws.cleanupClients();
  }
}