# ESP32 phone orientation receiver

The ESP32 serves a phone dashboard and receives orientation and acceleration
samples over `/ws`. It prints received samples to Serial at 115200 baud.

1. Install the ESP32 board support package in the Arduino IDE and select your
   ESP32 board and USB port.
2. Install **AsyncTCP** and **ESPAsyncWebServer**, both maintained by
   **ESP32Async**, using the Arduino Library Manager. `WiFi.h` comes with the
   ESP32 board support package.
3. Copy `firmware/phone_orientation_receiver/wifi_config.example.h` to
   `firmware/phone_orientation_receiver/wifi_config.h`. Edit only the local copy:
   set `STA_SSID` and `STA_PASS` for your router, or set `USE_AP` to `1` and choose
   an access point name and password of at least eight characters. The local
   configuration is ignored by Git; keep credentials out of committed files.
4. Open `firmware/phone_orientation_receiver/phone_orientation_receiver.ino`,
   compile, and upload it. Open Serial Monitor at **115200 baud**.
5. Connect the phone to the same router, or to the ESP32 access point when using
   `USE_AP=1`. Open the address printed to Serial; the default access point
   address is `http://192.168.4.1`.

## Browser sensor access

Modern mobile browsers generally require a **secure context** for motion and
orientation sensors. The ESP32 serves plain HTTP, so its direct IP address may
load the dashboard while the browser blocks sensors. Use a trusted HTTPS reverse
proxy that can reach the ESP32, forwarding both `/` and the `/ws` WebSocket
upgrade. The dashboard automatically uses `wss:` when loaded over HTTPS. The
proxy and any certificates are separate infrastructure, not supplied by the
sketch. An untrusted certificate may not provide a secure context.

Check that the dashboard reports `Secure context: yes`, then tap **Start
sensors** and allow motion/orientation access when prompted. With a connected
WebSocket, rotating the phone should update the dashboard and produce sensor
samples in Serial Monitor. Keep the page visible; wake lock support depends on
the browser.

## Validation

The embedded JavaScript can be extracted between its `<script>` tags and checked
with `node --check`. A complete firmware compile requires the ESP32 board package
and libraries above. Uploading and verifying real sensor samples requires an
ESP32 and a phone with supported sensors.
