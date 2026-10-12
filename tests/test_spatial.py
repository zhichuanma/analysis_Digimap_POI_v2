import math
import random
import unittest

import numpy as np

from src.spatial import build_neighbor_index, exact_neighbors, naive_neighbors


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


class ExactNeighborsTests(unittest.TestCase):
    def assert_matches_naive(self, center, points, radius, index=None):
        if index is None:
            index = build_neighbor_index(points)
        expected = dict(naive_neighbors(center, points, radius))
        actual = exact_neighbors(center, index, radius)
        self.assertEqual([i for i, _ in actual], sorted(expected))
        self.assertEqual(len(actual), len(dict(actual)))
        for i, distance in actual:
            self.assertTrue(math.isclose(distance, expected[i], rel_tol=1e-12, abs_tol=1e-9))
            self.assertLessEqual(distance, radius)
        return actual

    def test_sample_a_500_and_1000(self):
        points = [(100, 0), (300, 400), (600, 0), (1100, 0)]
        index = build_neighbor_index(points)
        self.assertEqual(exact_neighbors((0, 0), index, 500), [(0, 100.0), (1, 500.0)])
        self.assertEqual(exact_neighbors((0, 0), index, 1000), [(0, 100.0), (1, 500.0), (2, 600.0)])

    def test_axis_boundaries_adjacent_floats_and_tiny_excess(self):
        for radius in (500.0, 1000.0):
            with self.subTest(radius=radius):
                points = [(math.nextafter(radius, -math.inf), 0), (radius, 0),
                          (math.nextafter(radius, math.inf), 0), (radius + 0.000001, 0),
                          (-radius, 0), (0, radius), (0, -radius)]
                result = self.assert_matches_naive((0, 0), points, radius)
                self.assertEqual([i for i, _ in result], [0, 1, 4, 5, 6])

    def test_oblique_boundaries(self):
        for radius in (500.0, 1000.0):
            x, y = radius * 0.6, radius * 0.8
            points = [(x, y), (-x, y), (x, -y), (-x, -y),
                      (x, y + 0.000001), (x, y - 0.000001),
                      (math.nextafter(x, math.inf), math.nextafter(y, math.inf))]
            result = self.assert_matches_naive((0, 0), points, radius)
            self.assertTrue({0, 1, 2, 3, 5}.issubset(dict(result)))
            self.assertNotIn(4, dict(result))
            self.assertNotIn(6, dict(result))

    def test_bng_scale_shifted_boundaries(self):
        for center in ((400000, 300000), (699500, 1299000), (-100000, 500000)):
            for radius in (500.0, 1000.0):
                x, y = center
                edge_x = x + radius
                points = [(edge_x, y), (math.nextafter(edge_x, math.inf), y),
                          (math.nextafter(edge_x, -math.inf), y),
                          (x + radius * 0.6, y + radius * 0.8)]
                result = self.assert_matches_naive(center, points, radius)
                self.assertEqual([i for i, _ in result], [0, 2, 3])

    def test_many_angles_and_adjacent_coordinates(self):
        for center in ((0.0, 0.0), (432100.0, 765400.0)):
            for radius in (500.0, 1000.0):
                points = []
                for k in range(360):
                    angle = math.tau * k / 360
                    x, y = radius * math.cos(angle), radius * math.sin(angle)
                    for scale in (math.nextafter(1.0, 0.0), 1.0, math.nextafter(1.0, math.inf)):
                        points.append((center[0] + scale * x, center[1] + scale * y))
                self.assert_matches_naive(center, points, radius)

    def test_empty_list_tuple_array_and_iterator(self):
        for points in ([], (), np.empty((0, 2)), iter(())):
            index = build_neighbor_index(points)
            self.assertEqual(len(index), 0)
            self.assertEqual(exact_neighbors((0, 0), index, 500), [])

    def test_nonempty_index_with_no_candidates(self):
        self.assertEqual(exact_neighbors((0, 0), build_neighbor_index([(2000, 0), (3000, 0)]), 500), [])

    def test_colocated_records_keep_distinct_indices(self):
        points = [(25, 40), (25, 40), (25, 40), (30, 40), (30, 40)]
        self.assertEqual(exact_neighbors((25, 40), build_neighbor_index(points), 5),
                         [(0, 0.0), (1, 0.0), (2, 0.0), (3, 5.0), (4, 5.0)])

    def test_results_follow_input_indices_not_distance(self):
        points = [(float(x), 0) for x in range(100, -101, -1)]
        random.Random(42).shuffle(points)
        result = self.assert_matches_naive((0, 0), points, 50)
        self.assertNotEqual([d for _, d in result], sorted(d for _, d in result))

    def test_iterator_consumed_once_without_losing_first_record(self):
        seen = []
        def points():
            for i, xy in enumerate(((300, 400), (0, 0), (1000, 0))):
                seen.append(i)
                yield iter(xy)
        index = build_neighbor_index(points())
        self.assertEqual(seen, [0, 1, 2])
        self.assertEqual(exact_neighbors((0, 0), index, 500), [(0, 500.0), (1, 0.0)])
        self.assertEqual(exact_neighbors((0, 0), index, 1000), [(0, 500.0), (1, 0.0), (2, 1000.0)])
        self.assertEqual(seen, [0, 1, 2])

    def test_original_list_mutation_does_not_change_snapshot(self):
        points = [[100, 0], [300, 400], [600, 0]]
        index = build_neighbor_index(points)
        self.assertEqual(points, [[100, 0], [300, 400], [600, 0]])
        points[0][0] = 9999
        points.reverse()
        points.append([0, 0])
        self.assertEqual(exact_neighbors((0, 0), index, 500), [(0, 100.0), (1, 500.0)])

    def test_numpy_view_is_copied_without_changing_source(self):
        original = np.array([[100, 0, 9], [300, 400, 8], [600, 0, 7]], dtype=float)
        snapshot = original.copy()
        index = build_neighbor_index(original[:, :2])
        np.testing.assert_array_equal(original, snapshot)
        original[:] = 9999
        self.assertEqual(exact_neighbors((0, 0), index, 500), [(0, 100.0), (1, 500.0)])

    def test_coordinate_snapshot_cannot_be_changed_through_public_fields(self):
        index = build_neighbor_index([(100, 0)])
        with self.assertRaises(AttributeError):
            index.coordinates = ((9999.0, 0.0),)
        with self.assertRaises(TypeError):
            index.coordinates[0][0] = 9999.0
        self.assertEqual(exact_neighbors((0, 0), index, 500), [(0, 100.0)])

    def test_same_index_reused_across_centers_and_radii(self):
        points = [(0, 0), (300, 400), (600, 0), (1100, 0)]
        index = build_neighbor_index(points)
        for center in ((0, 0), (600, 0), (5000, 5000), (0, 0)):
            for radius in (500, 1000):
                self.assert_matches_naive(center, points, radius, index)

    def test_invalid_point_coordinates_reject_entire_index(self):
        for bad in (None, (), (1,), (1, 2, 3), ('bad', 0), (math.nan, 0),
                    (0, math.inf), (-math.inf, 0), (10**1000, 0)):
            with self.subTest(bad=repr(bad)[:50]), self.assertRaises(ValueError):
                build_neighbor_index([(0, 0), bad])

    def test_noniterable_point_collection_rejected(self):
        for bad in (None, 2, 3.5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                build_neighbor_index(bad)

    def test_bad_query_coordinates_rejected_even_for_empty_index(self):
        for index in (build_neighbor_index([]), build_neighbor_index([(0, 0)])):
            for bad in (None, (), (1,), (1, 2, 3), ('bad', 0), (math.nan, 0),
                        (0, math.inf), (10**1000, 0)):
                with self.subTest(bad=repr(bad)[:50]), self.assertRaises(ValueError):
                    exact_neighbors(bad, index, 500)

    def test_invalid_radii_rejected_even_for_empty_index(self):
        for index in (build_neighbor_index([]), build_neighbor_index([(0, 0)])):
            for radius in (0, -1, math.nan, math.inf, -math.inf, None, 'bad', [], 10**1000):
                with self.subTest(radius=repr(radius)[:50]), self.assertRaises(ValueError):
                    exact_neighbors((0, 0), index, radius)

    def test_unbuilt_index_rejected(self):
        for index in (None, [], [(0, 0)], object()):
            with self.assertRaises(ValueError):
                exact_neighbors((0, 0), index, 500)

    def test_existing_numeric_conversion_semantics(self):
        points = [('3', '4'), (np.float64(6), np.int64(8))]
        self.assertEqual(exact_neighbors(('0', '0'), build_neighbor_index(points), '5'),
                         naive_neighbors(('0', '0'), points, '5'))

    def test_tiny_positive_radii_still_filter_candidates(self):
        for radius in (math.nextafter(0.0, math.inf), 1e-310, 1e-200):
            points = [(0, 0), (radius, 0), (math.nextafter(radius, math.inf), 0), (radius * 2, 0)]
            result = self.assert_matches_naive((0, 0), points, radius)
            self.assertEqual([i for i, _ in result], [0, 1])

    def test_finite_extreme_coordinates_keep_hypot_semantics(self):
        points = [(1e308, 0), (-1e308, 0), (0, 0), (1e308, 1e308)]
        index = build_neighbor_index(points)
        for center in ((0, 0), (1e308, 0), (-1e308, -1e308)):
            for radius in (500, 1e308, float.fromhex('0x1.fffffffffffffp+1023')):
                self.assert_matches_naive(center, points, radius, index)

    def test_fixed_seed_dense_sparse_and_duplicate_points(self):
        rng = random.Random(20261012)
        for scale in (100, 1800, 100000):
            points = [(400000 + rng.uniform(-scale, scale), 600000 + rng.uniform(-scale, scale))
                      for _ in range(300)]
            points += points[:15]
            rng.shuffle(points)
            index = build_neighbor_index(points)
            centers = [(400000, 600000), *points[:10],
                       *[(400000 + rng.uniform(-2000, 2000), 600000 + rng.uniform(-2000, 2000))
                         for _ in range(12)]]
            for center in centers:
                small = self.assert_matches_naive(center, points, 500, index)
                large = self.assert_matches_naive(center, points, 1000, index)
                self.assertTrue(set(dict(small)).issubset(dict(large)))


if __name__ == "__main__":
    unittest.main()
