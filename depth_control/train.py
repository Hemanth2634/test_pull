"""Train a new dense-depth CNN from measured paired RGB/depth data."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import random
import tempfile

import numpy as np
import torch
from torch.utils.data import DataLoader, Subset

from .dataset import DepthDataset
from .model import DepthNetwork, checkpoint_payload, masked_depth_loss


def evaluate(model: DepthNetwork, loader: DataLoader, device: torch.device) -> dict:
    model.eval()
    absolute_error = 0.0
    valid_pixels = 0
    with torch.inference_mode():
        for images, targets, valid in loader:
            images, targets, valid = images.to(device), targets.to(device), valid.to(device)
            prediction = model(images)
            absolute_error += float(torch.abs(prediction[valid] - targets[valid]).sum())
            valid_pixels += int(valid.sum())
    if not valid_pixels:
        raise ValueError("Validation did not contain measured depth pixels")
    return {"mae_cm": absolute_error / valid_pixels, "valid_pixels": valid_pixels}


def save_checkpoint(path: Path, payload: dict) -> None:
    """Replace the checkpoint atomically, preserving previous valid output on error."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            torch.save(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def train(*, dataset: str | Path, output: str | Path, epochs: int = 20,
          batch_size: int = 4, seed: int = 7, learning_rate: float = 1e-3,
          validation_fraction: float = 0.2, depth_normalization_cm: float = 100.0,
          device: str = "cpu", threads: int = 2) -> dict:
    if epochs < 1 or batch_size < 1 or threads < 1:
        raise ValueError("epochs, batch_size and threads must be positive")
    if not math.isfinite(learning_rate) or learning_rate <= 0:
        raise ValueError("learning_rate must be finite and positive")
    if not 0 < validation_fraction < 1:
        raise ValueError("validation_fraction must be between zero and one")
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.set_num_threads(threads)
    data = DepthDataset(dataset)
    if len(data) < 2:
        raise ValueError("Training needs at least two distinct RGB/depth pairs for a held-out validation split")
    order = list(range(len(data)))
    random.Random(seed).shuffle(order)
    val_count = min(len(data) - 1, max(1, round(len(data) * validation_fraction)))
    validation_indices, training_indices = order[:val_count], order[val_count:]
    generator = torch.Generator().manual_seed(seed)
    train_loader = DataLoader(Subset(data, training_indices), batch_size=batch_size,
                              shuffle=True, generator=generator, num_workers=0)
    validation_loader = DataLoader(Subset(data, validation_indices), batch_size=batch_size, num_workers=0)
    target_device = torch.device(device)
    model = DepthNetwork(depth_normalization_cm).to(target_device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=1e-4)
    trained_steps = 0
    history = []
    best_mae = math.inf
    best_metrics = None
    for epoch in range(1, epochs + 1):
        model.train()
        loss_sum = 0.0
        batches = 0
        for images, targets, valid in train_loader:
            images, targets, valid = images.to(target_device), targets.to(target_device), valid.to(target_device)
            optimizer.zero_grad(set_to_none=True)
            loss = masked_depth_loss(model(images), targets, valid)
            if not bool(torch.isfinite(loss)):
                raise ValueError("Training produced a nonfinite loss; no checkpoint saved for this epoch")
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=5.0)
            optimizer.step()
            loss_sum += float(loss.detach())
            batches += 1
            trained_steps += 1
        metrics = evaluate(model, validation_loader, target_device)
        if not math.isfinite(metrics["mae_cm"]):
            raise ValueError("Validation produced nonfinite depth values")
        record = {"epoch": epoch, "train_log_depth_loss": loss_sum / batches,
                  "validation_mae_cm": metrics["mae_cm"],
                  "validation_valid_pixels": metrics["valid_pixels"]}
        history.append(record)
        print(json.dumps(record), flush=True)
        if metrics["mae_cm"] < best_mae:
            best_mae = metrics["mae_cm"]
            best_metrics = record
            # CPU tensors keep the checkpoint portable to CPU-only servers.
            state = {name: value.detach().cpu().clone() for name, value in model.state_dict().items()}
            payload = checkpoint_payload(model, trained_steps=trained_steps,
                                         depth_scale_cm=data.depth_scale_cm,
                                         seed=seed, train_samples=len(training_indices),
                                         validation_samples=len(validation_indices),
                                         training_indices=training_indices,
                                         validation_indices=validation_indices,
                                         validation_metrics=record)
            payload["model_state"] = state
            save_checkpoint(Path(output), payload)
    return {"checkpoint": str(output), "best_validation": best_metrics, "history": history,
            "training_samples": len(training_indices), "validation_samples": len(validation_indices)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, help="Paired RGB/depth JSON manifest")
    parser.add_argument("--output", required=True, help="Destination .pt checkpoint")
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--learning-rate", type=float, default=1e-3)
    parser.add_argument("--validation-fraction", type=float, default=0.2)
    parser.add_argument("--depth-normalization-cm", type=float, default=100.0)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--threads", type=int, default=2)
    arguments = parser.parse_args(argv)
    try:
        result = train(**vars(arguments))
    except (ValueError, OSError, RuntimeError) as error:
        parser.exit(2, f"Training failed: {error}\n")
    print(json.dumps({"saved_checkpoint": result["checkpoint"], "best_validation": result["best_validation"]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
