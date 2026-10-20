from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient
import httpx
from PIL import Image

from depth_control.app import MAX_IMAGE_BYTES, Settings, WifiTransport, create_app


class Detector:
    def __init__(self, objects=None):
        self.objects = objects if objects is not None else [
            {"box": [2, 2, 18, 18], "distance_cm": 90},
            {"box": [22, 2, 38, 18], "distance_cm": 40},
        ]
        self.calls = 0

    def predict(self, image):
        self.calls += 1
        assert image.mode == "RGB"
        return self.objects


class Transport:
    def __init__(self, error=None):
        self.sent = []
        self.error = error

    def send(self, prediction_id, command):
        self.sent.append((prediction_id, command))
        if self.error:
            raise self.error


def frame_bytes():
    output = BytesIO()
    Image.new("RGB", (40, 20), "blue").save(output, format="JPEG")
    return output.getvalue()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.detector = Detector()
        self.transport = Transport()
        self.settings = Settings(checkpoint=self.directory / "missing.pt", data_dir=self.directory / "data")
        self.application = create_app(detector=self.detector, settings=self.settings, transport=self.transport)
        self.client = TestClient(self.application)
        self.addCleanup(self.client.close)

    def predict(self, client=None):
        response = (client or self.client).post("/api/predictions", content=frame_bytes(), headers={"Content-Type": "image/jpeg"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_nearest_prediction_waits_for_human_then_corrects_before_command(self):
        prediction = self.predict()
        self.assertEqual(prediction["selected"]["distance_cm"], 40)
        self.assertEqual(prediction["threshold_cm"], 50)
        self.assertEqual(self.transport.sent, [])
        response = self.client.post(f"/api/predictions/{prediction['id']}/confirm", json={"distance_cm": 70})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["command"], "ROTATE")
        self.assertTrue(response.json()["delivered"])
        self.assertEqual(self.transport.sent, [(prediction["id"], "ROTATE")])
        with sqlite3.connect(self.settings.data_dir / "feedback.sqlite3") as connection:
            row = connection.execute("SELECT status,distance_cm,command,delivered,frame_path,selected_json FROM predictions").fetchone()
        self.assertEqual(row[:4], ("confirmed", 70, "ROTATE", 1))
        self.assertTrue((self.settings.data_dir / row[4]).is_file())
        self.assertEqual(json.loads(row[5])["distance_cm"], 40)

    def test_accept_estimate_and_exact_boundary(self):
        self.detector.objects = [{"box": [0, 0, 20, 20], "distance_cm": 50}]
        prediction = self.predict()
        response = self.client.post(f"/api/predictions/{prediction['id']}/confirm", json={})
        self.assertEqual(response.json()["command"], "MOVE_SIDE")
        self.assertEqual(response.json()["distance_cm"], 50)

    def test_replay_is_rejected_after_server_app_restart(self):
        prediction = self.predict()
        path = f"/api/predictions/{prediction['id']}/confirm"
        self.assertEqual(self.client.post(path, json={}).status_code, 200)
        restarted = TestClient(create_app(detector=self.detector, settings=self.settings, transport=self.transport))
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.post(path, json={}).status_code, 409)
        self.assertEqual(len(self.transport.sent), 1)

    def test_concurrent_confirmations_emit_only_one_command(self):
        prediction = self.predict()
        path = f"/api/predictions/{prediction['id']}/confirm"
        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(lambda _: self.client.post(path, json={}).status_code, range(4)))
        self.assertEqual(sorted(statuses), [200, 409, 409, 409])
        self.assertEqual(len(self.transport.sent), 1)

    def test_expired_prediction_and_cancelled_prediction_cannot_emit(self):
        prediction = self.predict()
        self.application.state.store.clock = lambda: prediction["expires_at"] + 1
        response = self.client.post(f"/api/predictions/{prediction['id']}/confirm", json={})
        self.assertEqual(response.status_code, 410)
        self.application.state.store.clock = lambda: 1000
        another = self.predict()
        path = f"/api/predictions/{another['id']}"
        self.assertEqual(self.client.post(path + "/cancel").status_code, 200)
        self.assertEqual(self.client.post(path + "/confirm", json={}).status_code, 409)
        self.assertEqual(self.transport.sent, [])

    def test_no_regions_does_not_create_a_command_or_pending_prediction(self):
        self.detector.objects = []
        prediction = self.predict()
        self.assertIsNone(prediction["id"])
        self.assertIsNone(prediction["selected"])
        self.assertEqual(prediction["objects"], [])
        self.assertEqual(self.transport.sent, [])

    def test_missing_checkpoint_is_reported_without_fake_inference(self):
        client = TestClient(create_app(settings=self.settings))
        self.addCleanup(client.close)
        self.assertFalse(client.get("/api/health").json()["model_ready"])
        self.assertEqual(client.post("/api/predictions", content=frame_bytes()).status_code, 503)

    def test_corrupt_checkpoint_does_not_crash_the_server_or_claim_readiness(self):
        self.settings.checkpoint.write_bytes(b"not a valid checkpoint")
        client = TestClient(create_app(settings=self.settings))
        self.addCleanup(client.close)
        health = client.get("/api/health").json()
        self.assertFalse(health["model_ready"])
        self.assertIn("unavailable or invalid", health["model_error"])
        self.assertEqual(client.post("/api/predictions", content=frame_bytes()).status_code, 503)

    def test_invalid_and_oversized_images_are_rejected_before_prediction(self):
        for data, content_type, code in (
            (b"", "image/jpeg", 400),
            (b"not an image", "image/png", 400),
            (frame_bytes(), "text/plain", 415),
            (b"x" * (MAX_IMAGE_BYTES + 1), "image/jpeg", 413),
        ):
            with self.subTest(code=code, size=len(data)):
                result = self.client.post("/api/predictions", content=data, headers={"Content-Type": content_type})
                self.assertEqual(result.status_code, code, result.text)
        self.assertEqual(self.detector.calls, 0)
        self.assertEqual(self.transport.sent, [])

    def test_invalid_corrections_leave_prediction_pending(self):
        prediction = self.predict()
        path = f"/api/predictions/{prediction['id']}/confirm"
        for value in (0, -1, True, "50"):
            with self.subTest(value=value):
                self.assertEqual(self.client.post(path, json={"distance_cm": value}).status_code, 422)
        self.assertEqual(self.client.post(path, content=b'{"distance_cm":NaN}', headers={"Content-Type": "application/json"}).status_code, 422)
        self.assertEqual(self.client.post(path, json={"distance_cm": 30, "esp32_url": "http://other-host"}).status_code, 422)
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(self.client.post(path, json={}).status_code, 200)

    def test_delivery_failure_is_recorded_and_not_retried(self):
        self.transport.error = TimeoutError("receiver unavailable")
        prediction = self.predict()
        path = f"/api/predictions/{prediction['id']}/confirm"
        response = self.client.post(path, json={})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["delivered"])
        self.assertIn("TimeoutError", response.json()["delivery_error"])
        self.assertEqual(self.client.post(path, json={}).status_code, 409)
        self.assertEqual(len(self.transport.sent), 1)

    def test_unconfigured_receiver_records_symbolic_command_only(self):
        application = create_app(detector=self.detector, settings=self.settings)
        client = TestClient(application)
        self.addCleanup(client.close)
        prediction = self.predict(client)
        response = client.post(f"/api/predictions/{prediction['id']}/confirm", json={})
        self.assertEqual(response.json()["command"], "MOVE_BACK")
        self.assertFalse(response.json()["delivered"])
        self.assertIsNone(response.json()["delivery_error"])

    def test_camera_assets_and_readiness_routes_are_served(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertIn("PhoneCamera", self.client.get("/camera.js").text)
        self.assertEqual(self.client.get("/api/config").json()["threshold_cm"], 50)
        self.assertTrue(self.client.get("/api/health").json()["model_ready"])


class WifiTransportTests(unittest.TestCase):
    def test_only_a_matching_receiver_acknowledgement_counts_as_delivered(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"accepted": True, "id": request.url.params["id"], "command": request.url.params["command"]})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            transport = WifiTransport("http://192.168.1.20", client=client)
            transport.send("a" * 32, "MOVE_BACK")
        self.assertEqual(calls[0].method, "POST")
        self.assertEqual(calls[0].url.path, "/command")
        self.assertEqual(calls[0].url.params["command"], "MOVE_BACK")

        def mismatch(_request):
            return httpx.Response(200, json={"accepted": True, "id": "wrong", "command": "MOVE_BACK"})
        with httpx.Client(transport=httpx.MockTransport(mismatch)) as client:
            with self.assertRaisesRegex(ValueError, "does not match"):
                WifiTransport("http://esp32.local", client=client).send("a" * 32, "MOVE_BACK")

    def test_receiver_url_must_be_a_configured_http_origin(self):
        for value in ("file:///tmp/a", "http://user:password@host", "http://host/other", "http://host?target=x", "http://host:bad", "not-a-url"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                WifiTransport(value)


if __name__ == "__main__":
    unittest.main()
