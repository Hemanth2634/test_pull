"""Paired, aligned RGB images and measured metric-depth maps.

Manifest: {"depth_scale_cm": 0.1, "samples": [
    {"image": "images/frame.jpg", "depth": "depth/frame.png"}
]}. The scale is REQUIRED: raw depth units * scale = centimetres.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
from PIL import Image
import torch
from torch.utils.data import Dataset

from .model import INPUT_SIZE, image_tensor


def _inside(base: Path, value: object, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Each sample needs a nonempty relative {field} path")
    relative = Path(value)
    path = (base / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(base):
        raise ValueError(f"{field} path must stay within the manifest directory")
    if not path.is_file():
        raise ValueError(f"Missing {field} file: {value}")
    return path


def read_depth(path: Path, scale_cm: float) -> np.ndarray:
    if path.suffix.lower() == ".npy":
        raw = np.load(path, allow_pickle=False)
        if raw.dtype.kind not in "uif":
            raise ValueError(f"Depth .npy must contain real numeric values: {path.name}")
    elif path.suffix.lower() == ".png":
        with Image.open(path) as image:
            if image.mode not in ("I", "I;16", "I;16L", "I;16B"):
                raise ValueError(f"Depth PNG must be single-channel 16-bit raw depth, not an RGB colorization or 8-bit preview: {path.name}")
            raw = np.array(image)
    else:
        raise ValueError("Depth maps must be 16-bit PNG or numeric .npy files")
    if raw.ndim != 2:
        raise ValueError(f"Depth map must have exactly two dimensions: {path.name}")
    with np.errstate(over="ignore", invalid="ignore"):
        depth = np.asarray(raw, dtype=np.float32) * scale_cm
    depth[~np.isfinite(depth) | (depth <= 0)] = 0
    if not bool((depth > 0).any()):
        raise ValueError(f"Depth map has no valid positive measurements: {path.name}")
    return depth


class DepthDataset(Dataset):
    """Strictly validated RGB/depth pairs; zero/invalid pixels are masked."""

    def __init__(self, manifest_path: str | Path):
        self.manifest_path = Path(manifest_path).resolve()
        with self.manifest_path.open(encoding="utf-8") as stream:
            manifest = json.load(stream)
        if not isinstance(manifest, dict):
            raise ValueError("Dataset manifest must be a JSON object")
        scale = manifest.get("depth_scale_cm")
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale <= 0:
            raise ValueError("Set depth_scale_cm explicitly to a finite positive value: raw units multiplied by this value must equal centimetres")
        self.depth_scale_cm = float(scale)
        samples = manifest.get("samples")
        if not isinstance(samples, list) or not samples:
            raise ValueError("Dataset needs a nonempty samples list")
        self.samples = []
        base = self.manifest_path.parent
        seen = set()
        for index, sample in enumerate(samples):
            if not isinstance(sample, dict):
                raise ValueError(f"Sample {index} must be an object")
            image_path = _inside(base, sample.get("image"), "image")
            depth_path = _inside(base, sample.get("depth"), "depth")
            if image_path in seen:
                raise ValueError(f"Duplicate RGB image at sample {index}; duplicates would leak across the train/validation split")
            seen.add(image_path)
            depth = read_depth(depth_path, self.depth_scale_cm)
            with Image.open(image_path) as image:
                image.load()
                if image.size != (depth.shape[1], depth.shape[0]):
                    raise ValueError(f"RGB and depth dimensions must match for sample {index}; maps must already be spatially aligned")
            self.samples.append((image_path, depth_path))
            # Also reject sparse maps whose valid pixels vanish at model resolution.
            self[index]

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, index: int) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        image_path, depth_path = self.samples[index]
        with Image.open(image_path) as image:
            rgb = image_tensor(image)
        depth = read_depth(depth_path, self.depth_scale_cm)
        resized = np.array(Image.fromarray(depth).resize((INPUT_SIZE, INPUT_SIZE), Image.Resampling.NEAREST), dtype=np.float32)
        valid = np.isfinite(resized) & (resized > 0)
        if not bool(valid.any()):
            raise ValueError(f"Depth map has no valid measurements after nearest-neighbor resizing: {depth_path.name}")
        return rgb, torch.from_numpy(resized.copy()).unsqueeze(0), torch.from_numpy(valid.copy()).unsqueeze(0)
