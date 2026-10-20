# Phone camera capture

`web/index.html` is the phone depth-review page served by the Python app. Follow
[the training and server instructions](depth-control.md) to enable estimation.
It provides rear-camera preview, JPEG capture, region overlays, and explicit
human confirmation of each distance before requesting a symbolic command.

On a phone, serve the app through **HTTPS with a certificate trusted by that
phone**. Plain HTTP on an ESP32 or a laptop's LAN IP does not satisfy browser
camera permissions. A phone's localhost address refers to the phone, not the
laptop. A desktop browser on the server can use its own localhost address for
development without TLS.

Tap **Start rear camera** and allow camera access, then **Capture & estimate**.
The rear camera is preferred, with a fallback to an available camera. Captured
frames are JPEG blobs with their longest dimension limited to 640 pixels;
aspect ratio is preserved. Frames are uploaded to the Python server for
inference and stored locally there with their review record. **Stop camera**
releases the camera tracks and discards local pending review.

If the browser or operating system ends the camera stream, the page also
discards pending review and disables capture and confirmation. Start the camera
again and capture a fresh frame. A late permission response from an earlier,
cancelled start cannot restart capture or change the controls for a newer start.
A confirmation already submitted can still be delivered after the camera stops.

The reusable `PhoneCamera` class in `web/camera.js` exposes `start()`, `capture()`,
`stop()`, and `running`. Its video element must be supplied by the caller.
`capture({ maxDimension: 640, quality: 0.85 })` returns a promise for the JPEG
`Blob`, which the inference client uploads as binary image data. The class can
also be reused independently of the depth-review app.

Run the automated browser-API tests with Node.js 24:

```sh
node --test tests/*.test.mjs
```

These tests use simulated camera APIs. Real camera permission, browser behavior,
and image quality still require a phone and a trusted HTTPS connection.
