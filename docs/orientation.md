# ESP32 phone orientation receiver

The ESP32 serves a phone dashboard and receives orientation and acceleration
samples over `/ws`. It prints received samples to Serial at 115200 baud.
It also accepts symbolic movement commands from the Python service over LAN;
these are logged to Serial only and do not control motors or send OS signals.

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

## Commands from the Python service

Configure the Python server to reach the ESP32 at `http://<esp32-ip>:80` on the
same LAN. The server sends `POST /command` with query parameters, for example:

```sh
curl --request POST \
  'http://<esp32-ip>:80/command?id=0123456789abcdef0123456789abcdef&command=MOVE_BACK'
```

`id` must be the prediction UUID encoded as exactly 32 lowercase hexadecimal
characters (no dashes). `command` must be exactly `MOVE_BACK`, `ROTATE`, or
`MOVE_SIDE`. No request body is needed. The phone communicates with the Python
application; it never calls the ESP32's HTTP endpoint directly from the HTTPS
application. The original ESP32 orientation dashboard and `/ws` remain available.

A successful response is HTTP 200 with:

```json
{"id":"0123456789abcdef0123456789abcdef","command":"MOVE_BACK","accepted":true}
```

Acceptance means a symbolic signal was queued. The main loop logs it as
`COMMAND id=<id> command=MOVE_BACK` at 115200 baud. Retrying the same ID and
command acknowledges success without queuing it again. Reusing an ID for a
different command returns HTTP 409. Invalid input returns HTTP 400. The queue
holds eight signals; HTTP 503 means it is full and the caller may retry the same
ID. Rejected full-queue requests are not recorded as accepted.

Duplicate suppression remembers only the latest 32 accepted IDs in RAM and
resets when the ESP32 reboots. It does not guarantee persistence across reboots
or retries after an ID leaves that history. `GET /health` returns
`{"service":"phone_orientation_receiver","status":"ok"}` for a connection
check. These LAN endpoints do not implement authentication; keep them on your
trusted network.

## Validation

The embedded JavaScript can be extracted between its `<script>` tags and checked
with `node --check`. A complete firmware compile requires the ESP32 board package
and libraries above. Uploading and verifying real sensor samples requires an
ESP32 and a phone with supported sensors.

The fixed-size command queue and validation rules can also be tested without an
ESP32, from the repository root:

```sh
g++ -std=c++11 -Wall -Wextra -Werror -pedantic tests/test_command_protocol.cpp -o /tmp/test_command_protocol
/tmp/test_command_protocol
```
