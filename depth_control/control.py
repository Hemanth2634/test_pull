"""Select the closest usable depth region and emit symbolic movement commands."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

THRESHOLD_CM = 50.0


def positive_distance(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Distance must be a finite positive number in centimetres.")
    distance = float(value)
    if not math.isfinite(distance) or distance <= 0:
        raise ValueError("Distance must be a finite positive number in centimetres.")
    return distance


def movement_command(distance_cm: float) -> str:
    """Use the user's exact 50 cm boundary; this function does not drive motors."""
    distance_cm = positive_distance(distance_cm)
    if distance_cm < THRESHOLD_CM:
        return "MOVE_BACK"
    if distance_cm > THRESHOLD_CM:
        return "ROTATE"
    return "MOVE_SIDE"


def usable_regions(regions: Iterable[object], width: int, height: int) -> list[dict]:
    """Validate model output and retain nearest regions before applying a cap."""
    usable = []
    for region in regions:
        if not isinstance(region, Mapping):
            continue
        try:
            distance = positive_distance(region.get("distance_cm"))
            raw_box = region.get("box")
            if not isinstance(raw_box, (list, tuple)) or len(raw_box) != 4:
                continue
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in raw_box):
                continue
            box = [float(v) for v in raw_box]
            if not all(math.isfinite(v) for v in box):
                continue
            box = [max(0.0, min(float(width), box[0])),
                   max(0.0, min(float(height), box[1])),
                   max(0.0, min(float(width), box[2])),
                   max(0.0, min(float(height), box[3]))]
            if box[2] <= box[0] or box[3] <= box[1]:
                continue
            usable.append({"box": box, "distance_cm": distance, "source": "depth_region"})
        except (ValueError, TypeError, OverflowError):
            continue
    return sorted(usable, key=lambda item: item["distance_cm"])[:256]
