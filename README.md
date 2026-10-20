# test_pull
This is to test the pull changes

## ESP32 phone sensors

The [phone orientation receiver](firmware/phone_orientation_receiver/phone_orientation_receiver.ino)
streams a phone's tilt and acceleration readings to an ESP32 over WebSocket.
See [setup and browser requirements](docs/orientation.md) before uploading the sketch.
Wi-Fi credentials belong in the ignored local `wifi_config.h`, copied from the
provided example; do not commit your network password.

## Phone camera

The [phone camera page](web/index.html) opens the rear camera and captures JPEG frames.
See [camera setup and tests](docs/camera.md) for secure browser access and the
reusable capture API.

## Depth estimation and symbolic commands

The Python workflow trains a new dense-depth neural network from RGB images and
per-pixel depth maps. It selects the closest depth region, waits for a human to
confirm or correct the distance, then emits `MOVE_BACK` below 50 cm, `ROTATE`
above 50 cm, or `MOVE_SIDE` at exactly 50 cm. Commands can be delivered to the
ESP32 over Wi-Fi; they do not control motors.

See [dataset, training, server, and phone instructions](docs/depth-control.md).
No trained weights or dataset are included. The server reports inference as
unavailable until a trained checkpoint is supplied.
