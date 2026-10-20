# Phone camera capture

`web/index.html` demonstrates rear-camera preview, JPEG frame capture, and image
download. Frames stay in the browser; this capture demo does not estimate depth
or issue movement commands.

Serve the `web` directory with a static HTTP server. For a desktop test on the
same computer, run `python3 -m http.server 8080 --directory web` and open the
server using the browser's localhost address. On a phone, serve the same files
through **HTTPS with a certificate trusted by that phone**. Plain HTTP on an
ESP32 or a laptop's LAN IP does not satisfy browser camera permissions. A phone's
localhost address refers to the phone, not the laptop.

Tap **Start camera** and allow camera access, then **Capture frame**. The rear
camera is preferred, with a fallback to an available camera. Captured frames are
JPEG blobs with their longest dimension limited to 640 pixels; aspect ratio is
preserved. **Stop camera** releases the camera tracks.

The reusable `PhoneCamera` class in `web/camera.js` exposes `start()`, `capture()`,
`stop()`, and `running`. Its video element must be supplied by the caller.
`capture({ maxDimension: 640, quality: 0.85 })` returns a promise for the JPEG
`Blob`, which a later inference client can upload as binary image data.

Run the automated browser-API tests with Node.js 24:

```sh
node --test tests/camera.test.mjs
```

These tests use simulated camera APIs. Real camera permission, browser behavior,
and image quality still require a phone and a trusted HTTPS connection.
