import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from pyproj import Transformer, datadir
from pyproj.enums import TransformDirection
from pyproj.exceptions import ProjError

from coordinate_reference import independent_wgs84_to_bng, os_forward_helmert, osgb36_projection
from run import CODE_ROOT, DATA_ROOT
from src.spatial import FIXED_COORDINATE_OPERATION, naive_neighbors, transform_coordinates


# Frozen from the independent OS-formula calculation, before production use.
REFERENCES = (
    (1.71605199, 52.65797860, 651409.9029703322, 313177.2705058373),
    (-0.1278, 51.5074, 530028.746097372, 180380.0953579962),
    (-3.1883, 55.9533, 325897.2181850026, 674001.2017913695),
    (-5.537, 50.118, 147251.70350239275, 30205.858846453008),
)
OSGB_REFERENCE = (1 + 43 / 60 + 4.5177 / 3600, 52 + 39 / 60 + 27.2531 / 3600)
OS_GRID_REFERENCE = (651409.903, 313177.270)
PROJECTION_ONLY = (
    "+proj=pipeline +step +proj=unitconvert +xy_in=deg +xy_out=rad "
    "+step +proj=tmerc +lat_0=49 +lon_0=-2 +k=0.9996012717 "
    "+x_0=400000 +y_0=-100000 +ellps=airy"
)


class CoordinateTests(unittest.TestCase):
    def convert(self, longitude, latitude):
        return transform_coordinates(longitude, latitude, FIXED_COORDINATE_OPERATION)

    def test_independent_projection_matches_official_annex_c(self):
        xy = osgb36_projection(*OSGB_REFERENCE)
        for actual, expected in zip(xy, OS_GRID_REFERENCE):
            self.assertAlmostEqual(actual, expected, delta=0.001)

    def test_independent_helmert_matches_official_annex_d(self):
        xyz = os_forward_helmert(3790644.900, -110149.210, 5111482.970)
        for actual, expected in zip(xyz, (3790269.549, -110038.064, 5111050.261)):
            self.assertAlmostEqual(actual, expected, delta=0.001)

    def test_projection_stage_matches_official_osgb36_point(self):
        xy = Transformer.from_pipeline(PROJECTION_ONLY).transform(*OSGB_REFERENCE, errcheck=True)
        for actual, expected in zip(xy, OS_GRID_REFERENCE):
            self.assertAlmostEqual(actual, expected, delta=1.0)

    def test_route_wgs84_point_matches_original_grid_target(self):
        x, y = self.convert([1.71605199], [52.65797860])
        self.assertAlmostEqual(x[0], OS_GRID_REFERENCE[0], delta=1.0)
        self.assertAlmostEqual(y[0], OS_GRID_REFERENCE[1], delta=1.0)

    def test_complete_conversion_matches_four_independent_references(self):
        for lon, lat, expected_x, expected_y in REFERENCES:
            with self.subTest(longitude=lon, latitude=lat):
                independent = independent_wgs84_to_bng(lon, lat)
                self.assertAlmostEqual(independent[0], expected_x, delta=1e-6)
                self.assertAlmostEqual(independent[1], expected_y, delta=1e-6)
                x, y = self.convert([lon], [lat])
                self.assertAlmostEqual(x[0], expected_x, delta=1.0)
                self.assertAlmostEqual(y[0], expected_y, delta=1.0)

    def test_inverse_uses_absolute_angle_tolerance_without_relative_allowance(self):
        lon, lat = [r[0] for r in REFERENCES], [r[1] for r in REFERENCES]
        x, y = self.convert(lon, lat)
        back_lon, back_lat = Transformer.from_pipeline(FIXED_COORDINATE_OPERATION).transform(
            x, y, direction=TransformDirection.INVERSE, errcheck=True,
        )
        for actual, expected in zip(back_lon + back_lat, lon + lat):
            self.assertLessEqual(abs(actual - expected), 2e-8)

    def test_longitude_first_degrees_input_easting_first_metres_output(self):
        x, y = self.convert([-0.1278], [51.5074])
        self.assertAlmostEqual(x[0], 530028.746097372, delta=1)
        self.assertAlmostEqual(y[0], 180380.0953579962, delta=1)
        swapped = self.convert([51.5074], [-0.1278])
        self.assertGreater(abs(swapped[0][0] - x[0]), 100000)

    def test_order_duplicates_generators_and_input_values_preserved(self):
        lon, lat = [-3.1883, -0.1278, -3.1883], [55.9533, 51.5074, 55.9533]
        original = (lon[:], lat[:])
        x, y = self.convert(iter(lon), iter(lat))
        self.assertIsInstance(x, list)
        self.assertIsInstance(y, list)
        self.assertEqual(len(x), 3)
        self.assertEqual(len(y), 3)
        self.assertEqual((x[0], y[0]), (x[2], y[2]))
        self.assertEqual((lon, lat), original)
        for index in range(3):
            one_x, one_y = self.convert([lon[index]], [lat[index]])
            self.assertEqual((x[index], y[index]), (one_x[0], one_y[0]))

    def test_empty_columns_do_not_construct_transformer(self):
        with patch("pyproj.Transformer.from_pipeline", side_effect=AssertionError("empty")):
            self.assertEqual(self.convert([], []), ([], []))

    def test_reject_scalar_text_and_mapping_columns(self):
        for value in (None, 3, "1", b"1", {0: 1}):
            for field in ("longitude", "latitude"):
                args = (value, [52]) if field == "longitude" else ([1], value)
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, field):
                    self.convert(*args)

    def test_reject_unparsed_text_boolean_and_other_non_numeric_values(self):
        for value in (None, "1", True, False, complex(1, 0), [], object()):
            for field in ("longitude", "latitude"):
                args = ([value], [52]) if field == "longitude" else ([1], [value])
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, field):
                    self.convert(*args)

    def test_reject_nonfinite_out_of_range_and_float_overflow(self):
        for field, limit in (("longitude", 180), ("latitude", 90)):
            for value in (math.nan, math.inf, -math.inf, limit + 0.0001, -limit - 0.0001, 10 ** 1000):
                args = ([value], [52]) if field == "longitude" else ([1], [value])
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, field):
                    self.convert(*args)

    def test_length_mismatch_rejects_instead_of_truncating(self):
        for lon, lat in (([], [52]), ([1], []), ([1, 2], [52])):
            with self.subTest(longitude=lon, latitude=lat), self.assertRaisesRegex(ValueError, "equal lengths"):
                self.convert(lon, lat)

    def test_operation_cannot_be_replaced_even_for_empty_batch(self):
        with self.assertRaisesRegex(ValueError, "coordinate_operation"):
            transform_coordinates([], [], PROJECTION_ONLY)

    def test_projection_errors_fail_batch_with_errcheck(self):
        fake = Mock()
        fake.transform.side_effect = ProjError("test domain failure")
        with patch("pyproj.Transformer.from_pipeline", return_value=fake):
            with self.assertRaisesRegex(ValueError, "coordinate transformation failed"):
                self.convert([1], [52])
        fake.transform.assert_called_once_with([1.0], [52.0], errcheck=True)

    def test_nonfinite_or_incomplete_output_fails_batch(self):
        for output in (([math.inf], [0]), ([0], [math.nan]), ([], [0]), ([0], [])):
            fake = Mock()
            fake.transform.return_value = output
            with self.subTest(output=output), patch("pyproj.Transformer.from_pipeline", return_value=fake):
                with self.assertRaisesRegex(ValueError, "non-finite or incomplete"):
                    self.convert([1], [52])

    def test_one_metre_transform_tolerance_does_not_expand_radius(self):
        self.assertEqual(naive_neighbors((0, 0), [(500, 0), (500.000001, 0)], 500), [(0, 500.0)])

    def test_offline_process_with_only_proj_database_and_empty_user_directory(self):
        parent = Path(os.environ.get("TMPDIR", str(DATA_ROOT / "outputs"))).resolve()
        if not parent.is_relative_to(DATA_ROOT / "outputs"):
            raise RuntimeError("coordinate test files must stay in data-disk outputs/")
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="step08-offline-", dir=parent) as temporary:
            base = Path(temporary)
            proj_dir, user_dir = base / "proj", base / "user"
            proj_dir.mkdir()
            user_dir.mkdir()
            shutil.copyfile(Path(datadir.get_data_dir()) / "proj.db", proj_dir / "proj.db")
            script = '''
import json, sys
from pathlib import Path
from pyproj import datadir, network
datadir.set_data_dir(sys.argv[1])
assert Path(datadir.get_data_dir()).resolve() == Path(sys.argv[1]).resolve()
assert sorted(p.name for p in Path(sys.argv[1]).iterdir()) == ['proj.db']
assert not network.is_network_enabled()
from src.spatial import FIXED_COORDINATE_OPERATION, transform_coordinates
x,y = transform_coordinates([1.71605199], [52.65797860], FIXED_COORDINATE_OPERATION)
assert not list(Path(sys.argv[2]).iterdir())
print(json.dumps({'x':x[0], 'y':y[0], 'network_enabled':network.is_network_enabled(), 'grid_files':0}))
'''
            env = {**os.environ, "PROJ_NETWORK": "OFF", "PROJ_USER_WRITABLE_DIRECTORY": str(user_dir)}
            completed = subprocess.run(
                [sys.executable, "-B", "-c", script, str(proj_dir), str(user_dir)],
                cwd=CODE_ROOT, env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(completed.stdout)
            self.assertFalse(report["network_enabled"])
            self.assertEqual(report["grid_files"], 0)
            self.assertAlmostEqual(report["x"], OS_GRID_REFERENCE[0], delta=1)
            self.assertAlmostEqual(report["y"], OS_GRID_REFERENCE[1], delta=1)


if __name__ == "__main__":
    unittest.main()
