"""Synthetic checks validate the pipeline, not real-world depth accuracy."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
from PIL import Image
import torch

from depth_control.dataset import DepthDataset
from depth_control.model import (ARCHITECTURE, INPUT_SIZE, DepthNetwork,
                                 NeuralDetector, checkpoint_payload,
                                 depth_regions, masked_depth_loss)
from depth_control.train import train


torch.set_num_threads(2)


class TemporaryDataset(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manifest = self.root / "labels.json"

    def tearDown(self):
        self.temporary.cleanup()

    def write_dataset(self, *, count=2, scale=0.1, depths=None, png=False):
        samples = []
        for index in range(count):
            rgb = np.full((32, 48, 3), 40 + index * 30, dtype=np.uint8)
            Image.fromarray(rgb).save(self.root / f"image{index}.png")
            depth = np.full((32, 48), 400 + index * 100, dtype=np.float32) if depths is None else depths[index]
            depth_path = self.root / f"depth{index}.{'png' if png else 'npy'}"
            if png:
                Image.fromarray(depth.astype(np.uint16)).save(depth_path)
            else:
                np.save(depth_path, depth)
            samples.append({"image": f"image{index}.png", "depth": depth_path.name})
        document = {"depth_scale_cm": scale, "samples": samples}
        self.manifest.write_text(json.dumps(document))
        return document


class DatasetTests(TemporaryDataset):
    def test_raw_uint16_png_is_converted_to_centimetres(self):
        self.write_dataset(png=True)
        data = DepthDataset(self.manifest)
        image, depth, valid = data[0]
        self.assertEqual(tuple(image.shape), (3, INPUT_SIZE, INPUT_SIZE))
        self.assertEqual(tuple(depth.shape), (1, INPUT_SIZE, INPUT_SIZE))
        self.assertTrue(bool(valid.all()))
        self.assertEqual(float(depth.mean()), 40.0)
        self.assertTrue(bool((image >= 0).all() & (image <= 1).all()))

    def test_invalid_depth_pixels_are_masked_and_nearest_resized(self):
        depth = np.full((32, 48), 100.0, dtype=np.float32)
        depth[:, :12] = 0
        depth[0, 12] = np.nan
        depth[1, 12] = np.inf
        depth[2, 12] = -1
        self.write_dataset(count=1, scale=1, depths=[depth])
        _, result, valid = DepthDataset(self.manifest)[0]
        self.assertTrue(bool(torch.isfinite(result).all()))
        self.assertFalse(bool(valid[:, :, :30].any()))
        self.assertEqual(set(result.unique().tolist()), {0.0, 100.0})

    def test_scale_is_mandatory_and_positive(self):
        document = self.write_dataset()
        for scale in (None, 0, -1, True, "0.1"):
            document["depth_scale_cm"] = scale
            self.manifest.write_text(json.dumps(document))
            with self.subTest(scale=scale), self.assertRaisesRegex(ValueError, "depth_scale_cm"):
                DepthDataset(self.manifest)

    def test_colorized_depth_is_rejected(self):
        document = self.write_dataset()
        Image.new("RGB", (48, 32)).save(self.root / "preview.png")
        document["samples"][0]["depth"] = "preview.png"
        self.manifest.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "colorization"):
            DepthDataset(self.manifest)

    def test_alignment_duplicate_and_all_invalid_checks(self):
        document = self.write_dataset()
        np.save(self.root / "depth0.npy", np.ones((2, 2), dtype=np.float32))
        with self.assertRaisesRegex(ValueError, "dimensions must match"):
            DepthDataset(self.manifest)
        document = self.write_dataset()
        document["samples"][1]["image"] = document["samples"][0]["image"]
        self.manifest.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "Duplicate RGB"):
            DepthDataset(self.manifest)
        self.write_dataset(count=1, depths=[np.zeros((32, 48), dtype=np.float32)])
        with self.assertRaisesRegex(ValueError, "no valid"):
            DepthDataset(self.manifest)

    def test_npy_object_arrays_and_relative_path_escape_rejected(self):
        document = self.write_dataset()
        np.save(self.root / "depth0.npy", np.array([[object()]], dtype=object))
        with self.assertRaises(ValueError):
            DepthDataset(self.manifest)
        document["samples"][0]["image"] = "../outside.png"
        self.manifest.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "within the manifest"):
            DepthDataset(self.manifest)


class NetworkTests(unittest.TestCase):
    def test_forward_and_masked_backward_ignore_unmeasured_depth(self):
        torch.manual_seed(5)
        model = DepthNetwork()
        result = model(torch.rand(1, 3, INPUT_SIZE, INPUT_SIZE))
        self.assertEqual(tuple(result.shape), (1, 1, INPUT_SIZE, INPUT_SIZE))
        self.assertTrue(bool(torch.isfinite(result).all() & (result > 0).all()))
        target = torch.full_like(result, 40.0)
        valid = torch.ones_like(result, dtype=torch.bool)
        valid[:, :, :, :64] = False
        target[~valid] = float("nan")
        loss = masked_depth_loss(result, target, valid)
        loss.backward()
        self.assertGreater(float(model.head.weight.grad.abs().sum()), 0)
        self.assertTrue(bool(torch.isfinite(model.head.weight.grad).all()))
        with self.assertRaisesRegex(ValueError, "measured depth"):
            masked_depth_loss(result.detach(), target, torch.zeros_like(valid))

    def test_tiny_synthetic_example_can_reduce_loss(self):
        torch.manual_seed(7)
        model = DepthNetwork()
        image = torch.full((1, 3, 32, 32), 0.5)
        target = torch.full((1, 1, 32, 32), 40.0)
        valid = torch.ones_like(target, dtype=torch.bool)
        optimizer = torch.optim.Adam(model.parameters(), lr=0.003)
        first = float(masked_depth_loss(model(image), target, valid).detach())
        for _ in range(12):
            optimizer.zero_grad()
            loss = masked_depth_loss(model(image), target, valid)
            loss.backward()
            optimizer.step()
        final = float(masked_depth_loss(model(image), target, valid).detach())
        self.assertLess(final, first * 0.7)


class RegionTests(unittest.TestCase):
    def test_closest_region_sorted_scaled_and_speckles_ignored(self):
        depth = np.full((8, 8), 120.0)
        depth[:4, :4] = 30.0
        depth[7, 7] = 2.0
        regions = depth_regions(depth, (160, 80), min_region_pixels=4)
        self.assertEqual(len(regions), 2)
        self.assertEqual(regions[0]["distance_cm"], 30.0)
        self.assertEqual(regions[0]["box"], [0.0, 0.0, 80.0, 40.0])
        self.assertEqual(regions[0]["source"], "depth_region")
        self.assertNotIn("confidence", regions[0])

    def test_disconnected_equal_depth_components_remain_separate(self):
        depth = np.zeros((6, 6))
        depth[:2, :2] = 40
        depth[4:, 4:] = 40
        depth[2, 2] = np.nan
        regions = depth_regions(depth, (6, 6), min_region_pixels=4)
        self.assertEqual(len(regions), 2)
        self.assertEqual(depth_regions(np.full((4, 4), np.inf), (4, 4)), [])


class TrainingTests(TemporaryDataset):
    def test_training_checkpoint_and_predict_reload(self):
        self.write_dataset(count=3)
        output = self.root / "artifacts" / "depth.pt"
        with contextlib.redirect_stdout(io.StringIO()):
            report = train(dataset=self.manifest, output=output, epochs=2, batch_size=2)
        self.assertTrue(output.is_file())
        self.assertEqual(report["training_samples"], 2)
        self.assertEqual(report["validation_samples"], 1)
        metrics = report["best_validation"]
        self.assertEqual(metrics["validation_valid_pixels"], INPUT_SIZE * INPUT_SIZE)
        self.assertGreaterEqual(metrics["validation_mae_cm"], 0)
        checkpoint = torch.load(output, weights_only=True)
        self.assertEqual(checkpoint["architecture"], ARCHITECTURE)
        self.assertGreater(checkpoint["trained_steps"], 0)
        detector = NeuralDetector(output)
        regions = detector.predict(Image.new("RGB", (320, 240), "gray"))
        self.assertTrue(regions)
        self.assertTrue(all(region["distance_cm"] > 0 for region in regions))
        self.assertTrue(all(0 <= region["box"][0] < region["box"][2] <= 320 for region in regions))
        self.assertTrue(all(0 <= region["box"][1] < region["box"][3] <= 240 for region in regions))
        torch.save({**checkpoint, "trained_steps": 0}, output)
        with self.assertRaisesRegex(ValueError, "training evidence"):
            NeuralDetector(output)

    def test_missing_or_untrained_checkpoints_and_single_pair_fail(self):
        with self.assertRaises(FileNotFoundError):
            NeuralDetector(self.root / "missing.pt")
        with self.assertRaisesRegex(ValueError, "untrained"):
            checkpoint_payload(DepthNetwork(), trained_steps=0)
        self.write_dataset(count=1)
        with self.assertRaisesRegex(ValueError, "at least two"):
            train(dataset=self.manifest, output=self.root / "model.pt", epochs=1)


if __name__ == "__main__":
    unittest.main()
