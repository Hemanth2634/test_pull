"""Phone image -> trained neural depth -> human confirmation -> Wi-Fi signal."""

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
import logging
import math
import os
from pathlib import Path
import pickle
from threading import Lock
from urllib.parse import urlsplit
import warnings

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .control import THRESHOLD_CM, usable_regions
from .store import (PredictionConsumed, PredictionExpired, PredictionNotFound, PredictionStore)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
LOGGER = logging.getLogger(__name__)


@dataclass
class Settings:
    checkpoint: Path = field(default_factory=lambda: Path(os.getenv("DEPTH_CHECKPOINT", "artifacts/depth_model.pt")))
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DEPTH_DATA_DIR", "runtime")))
    esp32_url: str | None = field(default_factory=lambda: os.getenv("ESP32_URL") or None)
    prediction_ttl_seconds: float = 120.0


class WifiTransport:
    def __init__(self, base_url: str, *, client: httpx.Client | None = None):
        parsed = urlsplit(base_url)
        try:
            parsed.port
        except ValueError as error:
            raise ValueError("ESP32_URL has an invalid port.") from error
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("ESP32_URL must be an HTTP(S) origin without credentials, a path, query, or fragment.")
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=3.0, follow_redirects=False)

    def send(self, prediction_id: str, command: str):
        response = self.client.post(
            self.base_url + "/command", params={"id": prediction_id, "command": command},
        )
        response.raise_for_status()
        acknowledgement = response.json()
        if (not isinstance(acknowledgement, dict)
                or acknowledgement.get("accepted") is not True
                or acknowledgement.get("id") != prediction_id
                or acknowledgement.get("command") != command):
            raise ValueError("ESP32 acknowledgement does not match the confirmed command.")


class Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    distance_cm: float | None = Field(default=None, strict=True, gt=0, allow_inf_nan=False)


def _read_image(data: bytes):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as raw:
                if raw.format not in ("JPEG", "PNG"):
                    raise HTTPException(415, "Only JPEG and PNG frames are supported.")
                if raw.width * raw.height > MAX_IMAGE_PIXELS:
                    raise HTTPException(413, "Camera frame exceeds the 16 megapixel limit.")
                raw.load()
                return ImageOps.exif_transpose(raw).convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as error:
        raise HTTPException(400, "The request does not contain a readable camera image.") from error


def create_app(*, detector=None, settings: Settings | None = None, transport=None):
    settings = settings or Settings()
    if (not math.isfinite(settings.prediction_ttl_seconds)
            or settings.prediction_ttl_seconds <= 0):
        raise ValueError("Prediction lifetime must be a positive number of seconds.")
    application = FastAPI(title="Phone neural depth control", version="1.0")
    store = PredictionStore(settings.data_dir)
    inference_lock = Lock()
    model_error = None
    if detector is None:
        try:
            from .model import NeuralDetector
            detector = NeuralDetector(settings.checkpoint)
        except (ImportError, OSError, ValueError, RuntimeError, pickle.UnpicklingError,
                EOFError, KeyError, TypeError) as error:
            model_error = f"Trained checkpoint unavailable or invalid ({type(error).__name__})."
    if transport is None and settings.esp32_url:
        transport = WifiTransport(settings.esp32_url)
    application.state.store = store
    application.state.detector = detector
    application.state.transport = transport

    @application.exception_handler(RequestValidationError)
    async def validation_error(_request, error):
        # Avoid echoing untrusted inputs (including NaN) into JSON error output.
        detail = [{key: item[key] for key in ("loc", "msg", "type")} for item in error.errors()]
        return JSONResponse(status_code=422, content={"detail": detail})

    @application.get("/api/health")
    def health():
        return {"model_ready": detector is not None, "model_error": model_error,
                "delivery_enabled": transport is not None}

    @application.get("/api/config")
    def config():
        return {"threshold_cm": THRESHOLD_CM,
                "prediction_ttl_seconds": settings.prediction_ttl_seconds,
                "delivery_enabled": transport is not None}

    def infer(data: bytes):
        image = _read_image(data)
        try:
            with inference_lock:
                raw_regions = detector.predict(image)
            regions = usable_regions(raw_regions, image.width, image.height)
        except Exception as error:
            LOGGER.exception("Depth inference failed")
            raise HTTPException(500, "Depth inference failed; no command was emitted.") from error
        if not regions:
            return {"id": None, "objects": [], "selected": None,
                    "threshold_cm": THRESHOLD_CM, "expires_at": None}
        selected = regions[0]
        saved = store.create(image, regions, selected, settings.prediction_ttl_seconds)
        return {**saved, "objects": regions, "selected": selected, "threshold_cm": THRESHOLD_CM}

    @application.post("/api/predictions")
    async def predict(request: Request):
        if detector is None:
            raise HTTPException(503, "No trained checkpoint is loaded. Train on your dataset before inference.")
        content_type = request.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
        if content_type not in ("image/jpeg", "image/png", "application/octet-stream"):
            raise HTTPException(415, "Send a raw JPEG or PNG frame in the request body.")
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                raise HTTPException(413, "Camera frame exceeds the 5 MiB upload limit.")
            data.extend(chunk)
        if not data:
            raise HTTPException(400, "Send a camera image in the request body.")
        return await run_in_threadpool(infer, bytes(data))

    def pending_error(error):
        if isinstance(error, PredictionNotFound):
            return HTTPException(404, str(error))
        if isinstance(error, PredictionExpired):
            return HTTPException(410, str(error))
        return HTTPException(409, str(error))

    @application.post("/api/predictions/{prediction_id}/confirm")
    def confirm(prediction_id: str, confirmation: Confirmation):
        try:
            result = store.confirm(prediction_id, confirmation.distance_cm)
        except (PredictionNotFound, PredictionExpired, PredictionConsumed) as error:
            raise pending_error(error) from error
        delivered = False
        delivery_error = None
        if transport is not None:
            try:
                transport.send(prediction_id, result["command"])
                delivered = True
            except Exception as error:
                LOGGER.warning("Command delivery failed: %s", type(error).__name__)
                delivery_error = f"{type(error).__name__}: {error}"
        store.record_delivery(prediction_id, delivered, delivery_error)
        return {**result, "delivered": delivered, "delivery_error": delivery_error}

    @application.post("/api/predictions/{prediction_id}/cancel")
    def cancel(prediction_id: str):
        try:
            store.cancel(prediction_id)
        except (PredictionNotFound, PredictionExpired, PredictionConsumed) as error:
            raise pending_error(error) from error
        return {"id": prediction_id, "cancelled": True}

    web = Path(__file__).resolve().parent.parent / "web"
    application.mount("/", StaticFiles(directory=web, html=True), name="web")
    return application


app = create_app()
