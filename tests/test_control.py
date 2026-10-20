import math
import unittest

from depth_control.control import movement_command, usable_regions


class CommandTests(unittest.TestCase):
    def test_exact_fifty_centimetre_boundary(self):
        self.assertEqual(movement_command(49.999), "MOVE_BACK")
        self.assertEqual(movement_command(50), "MOVE_SIDE")
        self.assertEqual(movement_command(50.001), "ROTATE")

    def test_invalid_distances_never_become_commands(self):
        for value in (0, -1, math.nan, math.inf, -math.inf, True, "50", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                movement_command(value)

    def test_closest_valid_region_wins_even_without_confidence(self):
        objects = [
            {"box": [0, 0, 20, 20], "distance_cm": 200, "confidence": 0.99},
            {"box": [20, 20, 40, 40], "distance_cm": 30, "source": "depth_region"},
            {"box": [0, 0, 10, 10], "distance_cm": float("nan")},
            {"box": [4, 4, 4, 4], "distance_cm": 1},
            {"box": [0, 0, float("inf"), 20], "distance_cm": 1},
        ]
        regions = usable_regions(objects, 100, 100)
        self.assertEqual([item["distance_cm"] for item in regions], [30, 200])
        self.assertEqual(regions[0]["box"], [20, 20, 40, 40])

    def test_out_of_frame_boxes_are_clipped_and_empty_boxes_removed(self):
        regions = usable_regions([
            {"box": [-10, -10, 20, 20], "distance_cm": 20},
            {"box": [200, 200, 300, 300], "distance_cm": 10},
        ], 100, 100)
        self.assertEqual(regions, [{"box": [0, 0, 20, 20], "distance_cm": 20, "source": "depth_region"}])


if __name__ == "__main__":
    unittest.main()
