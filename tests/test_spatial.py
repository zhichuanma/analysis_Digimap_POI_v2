import math
import unittest

from src.spatial import naive_neighbors


class NaiveNeighborsTests(unittest.TestCase):
    def test_sample_a_at_500_metres(self) -> None:
        poi_xy = [(100, 0), (300, 400), (600, 0), (1100, 0)]

        result = naive_neighbors((0, 0), poi_xy, 500)

        self.assertEqual([index for index, _ in result], [0, 1])
        self.assertEqual([distance for _, distance in result], [100.0, 500.0])

    def test_sample_a_at_1000_metres(self) -> None:
        poi_xy = [(100, 0), (300, 400), (600, 0), (1100, 0)]

        result = naive_neighbors((0, 0), poi_xy, 1000)

        self.assertEqual([index for index, _ in result], [0, 1, 2])
        self.assertTrue(math.isclose(result[1][1], 500.0))

    def test_empty_poi_input_returns_no_neighbors(self) -> None:
        self.assertEqual(naive_neighbors((0, 0), [], 500), [])

    def test_boundary_is_included_and_tiny_over_boundary_is_excluded(self) -> None:
        poi_xy = [(500, 0), (500.000001, 0), (1000, 0)]

        at_500 = naive_neighbors((0, 0), poi_xy, 500)
        at_1000 = naive_neighbors((0, 0), poi_xy, 1000)

        self.assertEqual([index for index, _ in at_500], [0])
        self.assertEqual([index for index, _ in at_1000], [0, 1, 2])

    def test_same_coordinate_pois_keep_distinct_input_indices(self) -> None:
        poi_xy = [(25, 40), (25, 40), (80, 80)]

        result = naive_neighbors((25, 40), poi_xy, 1)

        self.assertEqual(result, [(0, 0.0), (1, 0.0)])

    def test_non_positive_radius_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            naive_neighbors((0, 0), [], 0)


if __name__ == "__main__":
    unittest.main()
