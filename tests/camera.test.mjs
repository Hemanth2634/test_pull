import assert from "node:assert/strict";
import { test } from "node:test";
import { PhoneCamera } from "../web/camera.js";

function fixture(overrides = {}) {
  const tracks = [{ stops: 0, stop() { this.stops += 1; } }];
  const stream = { getTracks: () => tracks };
  const requests = [];
  const drawings = [];
  const encodings = [];
  const blob = new Blob(["camera-image"], { type: "image/jpeg" });
  const video = {
    videoWidth: 1920,
    videoHeight: 1080,
    srcObject: null,
    pauses: 0,
    async play() {},
    pause() { this.pauses += 1; },
  };
  const canvas = {
    getContext: () => ({ drawImage: (...args) => drawings.push(args) }),
    toBlob(callback, type, quality) {
      encodings.push({ type, quality });
      callback(blob);
    },
  };
  const camera = new PhoneCamera(video, {
    secureContext: true,
    mediaDevices: {
      async getUserMedia(constraints) {
        requests.push(constraints);
        return stream;
      },
    },
    createCanvas: () => canvas,
    ...overrides,
  });
  return { camera, video, stream, tracks, requests, canvas, drawings, encodings, blob };
}

test("insecure contexts get an actionable HTTPS error", async () => {
  const { camera, requests } = fixture({ secureContext: false });
  await assert.rejects(camera.start(), /requires HTTPS or localhost/);
  assert.equal(requests.length, 0);
  assert.equal(camera.running, false);
});

test("missing camera APIs get an actionable browser error", async () => {
  const { camera } = fixture({ mediaDevices: undefined });
  await assert.rejects(camera.start(), /unavailable in this browser/);
});

test("permission refusal leaves the camera stopped and explains recovery", async () => {
  const { camera } = fixture({
    mediaDevices: {
      async getUserMedia() {
        const error = new Error("refused");
        error.name = "NotAllowedError";
        throw error;
      },
    },
  });
  await assert.rejects(camera.start(), /Allow camera access in your browser settings/);
  assert.equal(camera.running, false);
});

test("start prefers the rear camera without microphone access", async () => {
  const { camera, requests, video, stream } = fixture();
  await camera.start();
  assert.deepEqual(requests, [{ audio: false, video: { facingMode: { ideal: "environment" } } }]);
  assert.equal(video.srcObject, stream);
  assert.equal(video.playsInline, true);
  assert.equal(video.muted, true);
  assert.equal(camera.running, true);
});

test("capture encodes a JPEG frame scaled with its aspect ratio intact", async () => {
  const { camera, canvas, drawings, encodings, video, blob } = fixture();
  await camera.start();
  assert.equal(await camera.capture(), blob);
  assert.equal(canvas.width, 640);
  assert.equal(canvas.height, 360);
  assert.deepEqual(drawings, [[video, 0, 0, 640, 360]]);
  assert.deepEqual(encodings, [{ type: "image/jpeg", quality: 0.85 }]);
});

test("portrait capture scales its longest dimension and does not upscale", async () => {
  const { camera, canvas, video } = fixture();
  await camera.start({ facingMode: "user" });
  video.videoWidth = 1080;
  video.videoHeight = 1920;
  await camera.capture();
  assert.equal(canvas.width, 360);
  assert.equal(canvas.height, 640);
  video.videoWidth = 100;
  video.videoHeight = 50;
  await camera.capture();
  assert.equal(canvas.width, 100);
  assert.equal(canvas.height, 50);
});

test("capture requires a running camera and a decoded video frame", async () => {
  const { camera, video } = fixture();
  await assert.rejects(camera.capture(), /Start the camera/);
  await camera.start();
  video.videoWidth = 0;
  await assert.rejects(camera.capture(), /has not produced a frame/);
});

test("invalid encoding settings and missing encoder output fail explicitly", async () => {
  const { camera, canvas } = fixture();
  await camera.start();
  await assert.rejects(camera.capture({ maxDimension: 0 }), /positive integer/);
  await assert.rejects(camera.capture({ quality: NaN }), /between 0 and 1/);
  canvas.toBlob = callback => callback(null);
  await assert.rejects(camera.capture(), /could not encode/);
});

test("stop releases tracks and clears the video; repeated stops are harmless", async () => {
  const { camera, tracks, video } = fixture();
  await camera.start();
  camera.stop();
  camera.stop();
  assert.equal(tracks[0].stops, 1);
  assert.equal(video.srcObject, null);
  assert.equal(video.pauses, 1);
  assert.equal(camera.running, false);
});

test("a playback failure releases the acquired camera", async () => {
  const { camera, tracks, video } = fixture();
  video.play = async () => { throw new Error("playback failed"); };
  await assert.rejects(camera.start(), /playback failed/);
  assert.equal(tracks[0].stops, 1);
  assert.equal(video.srcObject, null);
  assert.equal(camera.running, false);
});

test("stopping during the permission prompt releases a late camera stream", async () => {
  let grantPermission;
  const pending = new Promise(resolve => { grantPermission = resolve; });
  const { camera, stream, tracks, video } = fixture({
    mediaDevices: { getUserMedia: () => pending },
  });
  const starting = camera.start();
  camera.stop();
  grantPermission(stream);
  await assert.rejects(starting, { name: "AbortError" });
  assert.equal(tracks[0].stops, 1);
  assert.equal(video.srcObject, null);
  assert.equal(camera.running, false);
});
