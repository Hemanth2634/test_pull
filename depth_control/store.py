"""Persist pending frames and consume human confirmation once using SQLite."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .control import movement_command, positive_distance


class PredictionNotFound(Exception):
    pass


class PredictionConsumed(Exception):
    pass


class PredictionExpired(Exception):
    pass


class PredictionStore:
    def __init__(self, directory: Path | str, *, clock=time.time):
        self.directory = Path(directory)
        self.frames = self.directory / "frames"
        self.frames.mkdir(parents=True, exist_ok=True)
        self.database = self.directory / "feedback.sqlite3"
        self.clock = clock
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    frame_path TEXT NOT NULL,
                    objects_json TEXT NOT NULL,
                    selected_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    distance_cm REAL,
                    command TEXT,
                    confirmed_at REAL,
                    delivered INTEGER NOT NULL DEFAULT 0,
                    delivery_error TEXT
                )
            """)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database, timeout=10.0)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def create(self, image, objects: list[dict], selected: dict, ttl: float) -> dict:
        prediction_id = uuid.uuid4().hex
        relative_frame = f"frames/{prediction_id}.jpg"
        frame = self.directory / relative_frame
        temporary = frame.with_suffix(".tmp")
        now = self.clock()
        try:
            image.save(temporary, format="JPEG", quality=90)
            temporary.replace(frame)
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO predictions (id,created_at,expires_at,frame_path,objects_json,selected_json) "
                    "VALUES (?,?,?,?,?,?)",
                    (prediction_id, now, now + ttl, relative_frame,
                     json.dumps(objects, allow_nan=False), json.dumps(selected, allow_nan=False)),
                )
        except Exception:
            temporary.unlink(missing_ok=True)
            frame.unlink(missing_ok=True)
            raise
        return {"id": prediction_id, "expires_at": now + ttl}

    def _pending(self, connection, prediction_id: str):
        row = connection.execute("SELECT * FROM predictions WHERE id=?", (prediction_id,)).fetchone()
        if row is None:
            raise PredictionNotFound("Prediction not found.")
        if row["status"] != "pending":
            raise PredictionConsumed("This prediction was already confirmed or cancelled.")
        if row["expires_at"] <= self.clock():
            raise PredictionExpired("Prediction expired. Capture a new frame before confirming.")
        return row

    def confirm(self, prediction_id: str, corrected_cm: float | None) -> dict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._pending(connection, prediction_id)
            selected = json.loads(row["selected_json"])
            distance = positive_distance(selected["distance_cm"] if corrected_cm is None else corrected_cm)
            command = movement_command(distance)
            connection.execute(
                "UPDATE predictions SET status='confirmed',distance_cm=?,command=?,confirmed_at=? WHERE id=?",
                (distance, command, self.clock(), prediction_id),
            )
        return {"id": prediction_id, "distance_cm": distance, "command": command}

    def cancel(self, prediction_id: str):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._pending(connection, prediction_id)
            connection.execute("UPDATE predictions SET status='cancelled' WHERE id=?", (prediction_id,))

    def record_delivery(self, prediction_id: str, delivered: bool, error: str | None):
        with self._connect() as connection:
            connection.execute(
                "UPDATE predictions SET delivered=?,delivery_error=? WHERE id=? AND status='confirmed'",
                (int(delivered), error, prediction_id),
            )
