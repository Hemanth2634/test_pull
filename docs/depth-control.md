# Camera depth estimation with human confirmation

The phone captures an RGB frame and sends it to a Python server. A small U-Net
trained **from scratch on your RGB/depth pairs** predicts a dense metric depth
map. Connected regions of similar depth provide candidate obstacles. The server
selects the region with the smallest predicted median distance, shows that estimate, and
waits for a human to confirm or correct it in centimetres. Each confirmation
emits one symbolic command. A correction applies to that selected, reviewed
region; it does not silently select a different region. The distance rule is:

| Confirmed distance | Command |
| --- | --- |
| Less than 50 cm | `MOVE_BACK` |
| Greater than 50 cm | `ROTATE` |
| Exactly 50 cm | `MOVE_SIDE` |

There is no tolerance band. Confirming an unchanged estimate uses its full
precision; typing `50` explicitly selects `MOVE_SIDE`. No operating-system
signals or motor control are implemented.

The network predicts depth, not object classes. Depth regions can split one
object or join nearby objects; they are not semantic object detections. Small
regions are filtered to reduce single-pixel noise. Quality and centimetre
accuracy depend on the supplied data, camera, and held-out evaluation. A
checkpoint must be trained before real inference works; the server returns a
clear unavailable response if no valid checkpoint exists.

## Install

Use Python 3.12 and Node.js 24 for the camera tests. From the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --index-url https://download.pytorch.org/whl/cpu torch==2.9.1
.venv/bin/python -m pip install -r requirements.txt
```

The first command installs a CPU wheel without CUDA dependencies. No pretrained
weights or Hugging Face downloads are used. A CUDA installation can use the
matching official PyTorch distribution instead.

## Provide RGB images and per-pixel ground truth

Copy `examples/dataset.json` into your dataset directory as `labels.json` and
replace its example paths. Images and maps are **not included** in this example.
All paths are relative to the manifest. Every pair must have the same pixel
dimensions and be spatially aligned.

Depth maps must be single-channel 16-bit PNGs or two-dimensional numeric `.npy`
arrays. Colourized depth screenshots are not metric ground truth. Zero,
negative, and nonfinite samples are invalid pixels and are excluded from the
loss and metrics; each sample must contain valid measured depths.

`depth_scale_cm` is required and converts raw depth values to centimetres:

| Raw map unit | `depth_scale_cm` |
| --- | --- |
| Millimetres | `0.1` |
| Centimetres | `1.0` |
| Metres | `100.0` |

Provide varied indoor/outdoor scenes matching the intended camera and operating
conditions, including multiple obstacles and examples around 50 cm. At least
two distinct images are needed for a train/validation split; that minimum only
checks the pipeline and is not enough to establish useful model quality.
Near-duplicate frames across the split can make validation misleading. Keep a
separate set of scenes for final real-world evaluation.

## Train

```sh
.venv/bin/python -m depth_control.train \
  --dataset data/labels.json \
  --output artifacts/depth_model.pt \
  --epochs 100 --batch-size 8 --threads 2 --seed 42
```

The trainer uses a seeded held-out split, masked depth loss, and reports
validation mean absolute error in **centimetres** and valid-pixel counts. It
saves the checkpoint with the best validation MAE. Training for a fixed number
of epochs does not establish adequate accuracy; inspect validation results
before using the estimates. The model uses a 128-by-128 input, so very small
obstacles can disappear during resizing.

## Run the phone/server/ESP32 workflow

1. Follow [the firmware instructions](orientation.md), upload the sketch, and
   connect the ESP32, Python host, and phone to a network where the Python host
   can reach the ESP32's printed IP address. The original tilt dashboard remains
   available on the ESP32.
2. Set `ESP32_URL` to the receiver's base address, for example
   `http://192.168.1.20`. The Python server makes the Wi-Fi request; the phone
   never makes a mixed HTTP request to the ESP32 from its HTTPS camera page.
3. Serve the Python app using HTTPS and a certificate trusted by the phone:

   ```sh
   ESP32_URL=http://192.168.1.20 \
   DEPTH_CHECKPOINT=artifacts/depth_model.pt \
   DEPTH_DATA_DIR=runtime \
   .venv/bin/uvicorn depth_control.app:app --host 0.0.0.0 --port 8443 \
     --ssl-certfile certs/server.pem --ssl-keyfile certs/server-key.pem
   ```

   Supply your own trusted certificate covering the server hostname or LAN IP.
   A development CA such as `mkcert` can issue a LAN certificate, but its CA must
   also be trusted by the phone. An untrusted browser certificate warning does
   not establish camera permission. Certificates and keys belong in the ignored
   `certs/` directory. For server-only testing on the host, omit the TLS flags
   and use the host browser's localhost address.
4. Open the server on the phone, allow camera access, start the camera, and tap
   **Capture and estimate**. The frozen frame shows candidate regions and the
   selected closest region. Confirm the estimate or enter a corrected positive
   distance in centimetres. Discard the frame to avoid emitting a command.
5. On confirmation, the Python server records the feedback and sends one
   request to the ESP32's `/command` endpoint. The firmware prints the symbolic
   command to Serial. It does not actuate hardware. The UI distinguishes an
   emitted command from successful ESP32 acknowledgement.

If `ESP32_URL` is unset, the server still records and returns symbolic commands,
with `delivered: false`. This is useful while developing without an ESP32.
Pending estimates expire after 120 seconds. Repeated or concurrent confirmation
of one estimate cannot generate additional commands. A failed or interrupted
delivery is not automatically retried; capture a new frame rather than replaying
an old confirmation.

The prototype has no user authentication and is intended for a local development
network.

## Human feedback and retraining

`runtime/` contains prediction frames and a SQLite record of estimates,
confirmations, corrections, and delivery outcomes. These files are ignored by
Git. A scalar correction controls the current command and is retained for
review. It does **not** create a full per-pixel ground-truth map or silently
retrain the network. Retrain using reviewed RGB/depth pairs through the same
training command.

## Validate

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/*.test.mjs
g++ -std=c++11 -Wall -Wextra -pedantic tests/test_command_protocol.cpp -o /tmp/test-command-protocol
/tmp/test-command-protocol
```

Automated tests cover dense-depth data loading and model computation, nearest
region selection, the 50 cm boundaries, human confirmation and replay handling,
Wi-Fi acknowledgement handling, and browser camera lifecycle. Synthetic tests
validate the pipeline only. Real depth quality requires the provided dataset;
phone-camera and ESP32 behavior require testing on those devices.
