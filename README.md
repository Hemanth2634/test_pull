# test_pull
This is to test the pull changes

## ESP32 phone sensors

The [phone orientation receiver](firmware/phone_orientation_receiver/phone_orientation_receiver.ino)
streams a phone's tilt and acceleration readings to an ESP32 over WebSocket.
See [setup and browser requirements](docs/orientation.md) before uploading the sketch.
Wi-Fi credentials belong in the ignored local `wifi_config.h`, copied from the
provided example; do not commit your network password.

## Phone camera

The [camera demo](web/index.html) opens the rear camera and captures JPEG frames.
See [camera setup and tests](docs/camera.md) for secure browser access and the
reusable capture API.
