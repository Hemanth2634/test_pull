"""A small dense metric-depth network trained from scratch.

Depth regions are geometric obstacle candidates, not semantic object detections.
No pretrained or randomly initialized network is used by ``NeuralDetector``.
"""
from __future__ import annotations

from collections import deque
import math
from pathlib import Path

import numpy as np
from PIL import Image
import torch
from torch import nn
from torch.nn import functional as F

INPUT_SIZE = 128
ARCHITECTURE = "tiny-unet-depth-v1"
DEFAULT_DEPTH_NORMALIZATION_CM = 100.0


def _block(in_channels: int, out_channels: int) -> nn.Sequential:
    return nn.Sequential(
        nn.Conv2d(in_channels, out_channels, 3, padding=1), nn.SiLU(),
        nn.Conv2d(out_channels, out_channels, 3, padding=1), nn.SiLU(),
    )


class DepthNetwork(nn.Module):
    """Compact U-Net producing positive depth in centimetres at each pixel."""

    def __init__(self, depth_normalization_cm: float = DEFAULT_DEPTH_NORMALIZATION_CM):
        super().__init__()
        if not math.isfinite(depth_normalization_cm) or depth_normalization_cm <= 0:
            raise ValueError("depth_normalization_cm must be finite and positive")
        self.depth_normalization_cm = float(depth_normalization_cm)
        self.enc1 = _block(3, 12)
        self.enc2 = _block(12, 24)
        self.enc3 = _block(24, 48)
        self.bottleneck = _block(48, 64)
        self.dec3 = _block(64 + 48, 48)
        self.dec2 = _block(48 + 24, 24)
        self.dec1 = _block(24 + 12, 12)
        self.head = nn.Conv2d(12, 1, 1)

    def forward(self, images: torch.Tensor) -> torch.Tensor:
        if images.ndim != 4 or images.shape[1] != 3:
            raise ValueError("images must have shape [batch, 3, height, width]")
        a = self.enc1(images)
        b = self.enc2(F.max_pool2d(a, 2))
        c = self.enc3(F.max_pool2d(b, 2))
        x = self.bottleneck(F.max_pool2d(c, 2))
        x = self.dec3(torch.cat((F.interpolate(x, size=c.shape[-2:], mode="bilinear", align_corners=False), c), dim=1))
        x = self.dec2(torch.cat((F.interpolate(x, size=b.shape[-2:], mode="bilinear", align_corners=False), b), dim=1))
        x = self.dec1(torch.cat((F.interpolate(x, size=a.shape[-2:], mode="bilinear", align_corners=False), a), dim=1))
        return (F.softplus(self.head(x)) + 1e-6) * self.depth_normalization_cm


def image_tensor(image: Image.Image, input_size: int = INPUT_SIZE) -> torch.Tensor:
    image = image.convert("RGB").resize((input_size, input_size), Image.Resampling.BILINEAR)
    pixels = np.asarray(image, dtype=np.float32).copy() / 255.0
    return torch.from_numpy(pixels).permute(2, 0, 1)


def masked_depth_loss(predicted_cm: torch.Tensor, target_cm: torch.Tensor,
                      valid: torch.Tensor) -> torch.Tensor:
    """Smooth L1 on log depth, ignoring unmeasured pixels entirely."""
    valid = valid.bool() & torch.isfinite(target_cm) & (target_cm > 0)
    if not bool(valid.any()):
        raise ValueError("A training batch must contain measured depth pixels")
    return F.smooth_l1_loss(torch.log1p(predicted_cm[valid]), torch.log1p(target_cm[valid]))


def depth_regions(depth_cm: np.ndarray, original_size: tuple[int, int], *,
                  min_region_pixels: int = 32, relative_tolerance: float = 0.15,
                  max_regions: int = 64) -> list[dict]:
    """Group connected pixels within logarithmic depth bands.

    Bounds are mapped back to the original RGB image. Small regions are ignored.
    A wall, floor, or several touching objects can form one region; these are not
    object identities or calibrated model-confidence scores.
    """
    depth = np.asarray(depth_cm, dtype=np.float64)
    if depth.ndim != 2 or min_region_pixels < 1 or max_regions < 1:
        raise ValueError("Expected a 2D map and positive region limits")
    if not math.isfinite(relative_tolerance) or relative_tolerance <= 0:
        raise ValueError("relative_tolerance must be finite and positive")
    width, height = original_size
    if width < 1 or height < 1:
        raise ValueError("Original image dimensions must be positive")
    valid = np.isfinite(depth) & (depth > 0)
    bands = np.zeros(depth.shape, dtype=np.int64)
    bands[valid] = np.floor(np.log(depth[valid]) / math.log1p(relative_tolerance)).astype(np.int64)
    seen = ~valid
    map_h, map_w = depth.shape
    results = []
    for y in range(map_h):
        for x in range(map_w):
            if seen[y, x]:
                continue
            band = bands[y, x]
            seen[y, x] = True
            queue = deque([(y, x)])
            values = []
            min_x = max_x = x
            min_y = max_y = y
            while queue:
                cy, cx = queue.popleft()
                values.append(depth[cy, cx])
                min_x, max_x = min(min_x, cx), max(max_x, cx)
                min_y, max_y = min(min_y, cy), max(max_y, cy)
                for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                    if 0 <= ny < map_h and 0 <= nx < map_w and not seen[ny, nx] and bands[ny, nx] == band:
                        seen[ny, nx] = True
                        queue.append((ny, nx))
            if len(values) < min_region_pixels:
                continue
            distance = float(np.median(values))
            results.append({
                "box": [min_x * width / map_w, min_y * height / map_h,
                        (max_x + 1) * width / map_w, (max_y + 1) * height / map_h],
                "distance_cm": distance,
                "source": "depth_region",
            })
    results.sort(key=lambda item: item["distance_cm"])
    return results[:max_regions]


def checkpoint_payload(model: DepthNetwork, *, trained_steps: int, **metadata) -> dict:
    if trained_steps < 1:
        raise ValueError("Cannot mark an untrained model as a trained checkpoint")
    return {
        **metadata,
        "architecture": ARCHITECTURE,
        "input_size": INPUT_SIZE,
        "depth_normalization_cm": model.depth_normalization_cm,
        "trained_steps": int(trained_steps),
        "model_state": model.state_dict(),
    }


class NeuralDetector:
    """Load a trained metric-depth model and return geometric obstacle regions."""

    def __init__(self, checkpoint_path: str | Path, *, min_region_pixels: int = 32,
                 region_relative_tolerance: float = 0.15, device: str = "cpu"):
        self.device = torch.device(device)
        self.checkpoint_path = Path(checkpoint_path)
        if not self.checkpoint_path.is_file():
            raise FileNotFoundError(f"No trained depth checkpoint at {self.checkpoint_path}. Train using your labeled RGB/depth dataset first.")
        checkpoint = torch.load(self.checkpoint_path, map_location="cpu", weights_only=True)
        if not isinstance(checkpoint, dict) or checkpoint.get("architecture") != ARCHITECTURE:
            raise ValueError("Unsupported depth checkpoint architecture")
        if checkpoint.get("input_size") != INPUT_SIZE or not isinstance(checkpoint.get("trained_steps"), int) or checkpoint["trained_steps"] < 1:
            raise ValueError("Checkpoint lacks compatible input size or training evidence")
        self.model = DepthNetwork(checkpoint.get("depth_normalization_cm", float("nan")))
        self.model.load_state_dict(checkpoint["model_state"], strict=True)
        if any(not bool(torch.isfinite(value).all()) for value in self.model.state_dict().values()):
            raise ValueError("Checkpoint contains nonfinite model parameters")
        self.model.to(self.device).eval()
        if min_region_pixels < 1 or not math.isfinite(region_relative_tolerance) or region_relative_tolerance <= 0:
            raise ValueError("Invalid depth-region settings")
        self.min_region_pixels = min_region_pixels
        self.region_relative_tolerance = region_relative_tolerance

    def predict(self, image: Image.Image) -> list[dict]:
        tensor = image_tensor(image).unsqueeze(0).to(self.device)
        with torch.inference_mode():
            prediction = self.model(tensor)[0, 0].cpu().numpy()
        return depth_regions(prediction, image.size,
                             min_region_pixels=self.min_region_pixels,
                             relative_tolerance=self.region_relative_tolerance)
