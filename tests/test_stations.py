"""Step-10 independent hand cases and full-source file/evidence contracts."""

from collections import Counter
from contextlib import closing
import csv
import json
from pathlib import Path
import sqlite3
import struct
import unittest
from unittest.mock import patch

import pyarrow.parquet as pq
from pyproj import CRS
from shapely.geometry import Point, box

import test_geography as geo_tests
from src.data import (StationPreparationError, load_station_scope, prepare_station_files,
                      read_order_extent, station_parquet_schemas)
from src.geography import coverage_geometries
from src.spatial import FIXED_COORDINATE_OPERATION, naive_neighbors, prepare_stations, station_radius_base


def source(number, identity, xy=(700, 2000), *, lon=None, lat=52, inside=True, puas=None):
    names = (["City"] if inside else []) if puas is None else puas
    return {"source_record_no": number, "raw_id": identity,
            "raw_longitude": str(lon if lon is not None else (-1 if xy else "")),
            "raw_latitude": str(lat if xy else ""), "raw_fields": {}, "raw_values": [],
            "xy": list(xy) if xy else None, "in_target_pua": inside if xy else None,
            "lad_codes": ["E00000001"] if xy else [], "pua_names": names if xy else [],
            "target_pua_names": names if xy else [], "audit_reasons": [], "prior_station_issues": [],
            "coordinate_reasons": [] if xy else ["missing_station_coordinates"], "transformation_error": None}


class StationTests(unittest.TestCase):
    def setUp(self):
        self.coverage = coverage_geometries([
            {"lad_code": "E00000001", "geometry": box(0, 0, 4000, 4000)},
            {"lad_code": "N00000001", "geometry": box(-4000, 0, -2000, 4000)}], crs=27700,
            order_bounds=(-10000, -10000, 10000, 10000))

    def prepare(self, rows, **kwargs):
        return prepare_stations(rows, self.coverage, target_puas=["City", "Other"], **kwargs)

    def test_same_location_unique_ids_merge_and_radius_700(self):
        result = self.prepare([source(1, "01"), source(2, "02")])
        self.assertEqual(len(result["stations"]), 1)
        station = result["stations"][0]
        self.assertEqual(station["source_record_nos"], [1, 2])
        self.assertEqual(station["raw_ids"], ["01", "02"])
        self.assertTrue(station["selected"] and station["query_eligible"])
        self.assertEqual(station["gb_boundary_distance_m"], 700)
        self.assertEqual([r["coverage_complete_proxy"] for r in station_radius_base(result["stations"])], [True, False])
        self.assertEqual([r["station_key"] for r in result["audit"]], [station["station_key"]] * 2)

    def test_full_source_id_conflict_with_outside_counterpart(self):
        result = self.prepare([source(1, "dup"), source(2, "dup", (3500, 2000), lon=-2, inside=False)])
        self.assertEqual(len(result["stations"]), 2)
        selected = next(r for r in result["stations"] if r["selected"])
        self.assertFalse(selected["query_eligible"])
        self.assertEqual(selected["main_status"], "station_id_position_conflict")
        self.assertEqual(result["audit"][0]["id_source_record_nos"], [1, 2])
        self.assertEqual(len(station_radius_base(result["stations"])), 2)

    def test_duplicate_same_id_same_location_and_missing_block_group(self):
        for identities in (("one", "one"), ("one", ""), ("one", None), ("one", "  ")):
            with self.subTest(identities=identities):
                result = self.prepare([source(1, identities[0]), source(2, identities[1])])
                self.assertEqual(len(result["stations"]), 1)
                self.assertFalse(result["stations"][0]["query_eligible"])
                self.assertTrue(all(not r["coverage_complete_proxy"] for r in station_radius_base(result["stations"])))

    def test_normalization_collision_and_numeric_strings_stay_distinct(self):
        result = self.prepare([source(1, " A "), source(2, "A")])
        self.assertIn("station_id_normalization_collision", result["audit"][0]["identity_reasons"])
        self.assertFalse(result["stations"][0]["identity_eligible"])
        result = self.prepare([source(i, value) for i, value in enumerate(("001", "1", "1.0", "nan"), 1)])
        self.assertTrue(result["stations"][0]["query_eligible"])
        self.assertEqual(result["stations"][0]["normalized_ids"], ["001", "1", "1.0", "nan"])

    def test_unlocated_records_do_not_merge_and_keep_missing_state(self):
        result = self.prepare([source(1, "a", None), source(2, "b", None)])
        self.assertEqual(len(result["stations"]), 2)
        for row in result["stations"]:
            self.assertIsNone(row["in_target_pua"])
            self.assertIsNone(row["gb_boundary_distance_m"])
            self.assertFalse(row["query_eligible"] or row["selected"])
        self.assertEqual(station_radius_base(result["stations"]), [])

    def test_duplicate_id_with_unlocated_counterpart_blocks_known_location(self):
        result = self.prepare([source(1, "a"), source(2, "a", None)])
        self.assertFalse(any(r["query_eligible"] for r in result["stations"]))

    def test_coordinate_precision_signed_zero_and_order_independence(self):
        rows = [source(1, "a", lon=0), source(2, "b", lon=-0.0), source(3, "c", (701, 2000), lon=1e-12)]
        result = self.prepare(rows)
        self.assertEqual(len(result["stations"]), 2)
        self.assertEqual(result, self.prepare(reversed(rows)))

    def test_contradictory_geography_or_coordinate_blocks_preparation(self):
        for change in ({"in_target_pua": False, "pua_names": [], "target_pua_names": []}, {"xy": [701, 2000]}, {"lad_codes": []}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "disagree"):
                self.prepare([source(1, "a"), {**source(2, "b"), **change}])
        with self.assertRaises(ValueError):
            self.prepare([source(1, "a"), source(1, "b")])

    def test_ni_and_gb_outside_remain_separate_from_selection(self):
        result = self.prepare([source(1, "ni", (-3000, 2000), lon=-6, inside=False),
                               source(2, "outside", (-100, 2000), lon=-3, inside=False)])
        self.assertEqual({s["spatial_status"] for s in result["stations"]}, {"NI", "outside_GB_proxy"})
        self.assertFalse(any(s["query_eligible"] for s in result["stations"]))
        with self.assertRaisesRegex(ValueError, "outside same-source GB"):
            self.prepare([source(1, "bad", (-100, 2000), inside=True)])

    def test_boundary_and_exact_thresholds_no_tolerance(self):
        for x, complete in ((0, False), (500, True), (499.999999, False)):
            with self.subTest(x=x):
                result = self.prepare([source(1, "one", (x, 2000))])
                self.assertTrue(result["stations"][0]["query_eligible"])
                self.assertEqual(station_radius_base(result["stations"])[0]["coverage_complete_proxy"], complete)

    def test_order_outside_boundary_and_threshold(self):
        self.coverage = coverage_geometries([{"lad_code": "E00000001", "geometry": box(0, 0, 4000, 4000)}],
                                            crs=27700, order_bounds=(1000, 0, 4000, 4000))
        for x, eligible, margin, complete in ((999.999999, False, -0.000001, False), (1000, True, 0, False), (1500, True, 500, True)):
            with self.subTest(x=x):
                result = self.prepare([source(1, "one", (x, 2000))])
                row = result["stations"][0]
                self.assertEqual(row["query_eligible"], eligible)
                self.assertAlmostEqual(row["order_clearance_m"], margin, places=10)
                self.assertEqual(station_radius_base(result["stations"])[0]["coverage_complete_proxy"], complete)

    def test_poi_across_pua_is_inside_coverage_and_radius(self):
        self.coverage = coverage_geometries([{"lad_code": "E00000001", "geometry": box(-2000, -2000, 4000, 6000)}],
                                            crs=27700, order_bounds=(-2000, -2000, 4000, 6000))
        study = box(0, 0, 1000, 4000)
        xy, poi = (900, 2000), (1100, 2000)
        self.assertTrue(study.covers(Point(xy)))
        self.assertFalse(study.covers(Point(poi)))
        self.assertTrue(self.coverage["data_coverage"].covers(Point(poi)))
        self.assertEqual(naive_neighbors(xy, [poi], 500), [(0, 200.0)])
        result = self.prepare([source(1, "one", xy)])
        self.assertEqual([r["coverage_complete_proxy"] for r in station_radius_base(result["stations"])], [True, True])
        self.assertFalse(self.coverage["data_coverage"].covers(Point(4001, 2000)))
        self.assertEqual(naive_neighbors((3900, 2000), [(4001, 2000)], 500), [(0, 101.0)])

    def test_multiple_pua_memberships_do_not_duplicate_entities_or_base(self):
        result = self.prepare([source(1, "one", puas=["City", "Other"])])
        self.assertEqual(result["stations"][0]["target_pua_names"], ["City", "Other"])
        self.assertEqual(len(station_radius_base(result["stations"])), 2)

    def test_sample_stable_only_selected_within_gb_and_full_identity(self):
        rows = [source(i, str(i), (500 + 100 * i, 2000), lon=-1 - i / 100) for i in range(1, 8)]
        rows += [source(8, "border", (0, 2000), lon=-2), source(9, "1", (3000, 2000), lon=-3, inside=False)]
        a = self.prepare(rows, sample_size=3, random_seed=17)
        b = self.prepare(reversed(rows), sample_size=3, random_seed=17)
        self.assertEqual(a, b)
        self.assertEqual(len(a["sample_keys"]), 3)
        self.assertTrue(all(s["spatial_status"] == "within_GB_proxy" and "1" not in s["normalized_ids"] for s in a["stations"] if s["selected"]))
        self.assertEqual(len(a["audit"]), 9)

    def test_empty_zero_selected_and_sample_arguments(self):
        self.assertEqual(self.prepare([])["stations"], [])
        self.assertEqual(station_radius_base(self.prepare([source(1, "a", inside=False)])["stations"]), [])
        for kwargs in ({"sample_size": 1}, {"sample_size": False, "random_seed": 1}, {"random_seed": 1}):
            with self.assertRaises(ValueError):
                self.prepare([], **kwargs)


class StationFileTests(unittest.TestCase):
    def setUp(self):
        self.fixture = geo_tests.GeographyFileTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        with closing(sqlite3.connect(self.root / self.fixture.lad_path)) as db:
            db.execute('UPDATE "LAD ""test""" SET code=CASE code WHEN \'A\' THEN \'E00000001\' ELSE \'S00000001\' END')
            db.commit()
        self.poi = "data/raw/poi_test.gpkg"
        self.order = "data/raw/order.txt"
        self.ref = "data/raw/product.txt"
        (self.root / self.ref).write_text("National Grid easting/northing in metres; synthetic GB product")
        self.review = {"crs": "EPSG:27700", "units": "metre", "axis_order": ["easting", "northing"],
                       "references": [{"source_path": self.ref, "locator": "line 1", "quote": "National Grid easting/northing in metres"}]}
        with closing(sqlite3.connect(self.root / self.poi)) as db:
            db.executescript('''
                CREATE TABLE gpkg_contents (table_name TEXT, data_type TEXT, srs_id INTEGER);
                CREATE TABLE gpkg_geometry_columns (table_name TEXT, column_name TEXT, geometry_type_name TEXT, srs_id INTEGER, z INTEGER, m INTEGER);
                CREATE TABLE gpkg_spatial_ref_sys (srs_id INTEGER, definition TEXT);
                CREATE TABLE poi (id INTEGER PRIMARY KEY, geom BLOB, ref_no INTEGER, pointx_class TEXT, feature_easting REAL, feature_northing REAL, pos_accuracy INTEGER);
            ''')
            db.execute("INSERT INTO gpkg_contents VALUES ('poi','features',27700)")
            db.execute("INSERT INTO gpkg_geometry_columns VALUES ('poi','geom','POINT',27700,0,0)")
            db.execute("INSERT INTO gpkg_spatial_ref_sys VALUES (?,?)", (27700, CRS.from_epsg(27700).to_wkt()))
            db.commit()
        self.order_text = ("Folder: poi_test\nExtent: 0, 0 - 700000, 1300000\nDate Ordered: synthetic\n"
                           "Product: Points of Interest\nVersion: synthetic\nFormat: GeoPackage\nArea: 910000 square kilometres\n"
                           "Filename: poi_test/poi_test.gpkg\nExtent: 0, 0 - 700000, 1300000\n")
        (self.root / self.order).write_text(self.order_text)
        self.refresh_manifest()
        self.step09 = self.fixture.run_scope()
        self.scope_dir = str(Path(self.step09["output_dir"]).relative_to(self.root))
        self.seal_scope()
        self.counter = 0

    def refresh_manifest(self):
        self.fixture.write_manifest()
        path = self.root / "data/source_manifest.json"
        manifest = json.loads(path.read_text())
        for name in (self.poi, self.order, self.ref):
            fp = geo_tests.fingerprint(self.root / name)
            manifest["files"].append({"path": name, **fp, "source_location": "synthetic",
                                      "copied_at": "2026-10-11T00:00:00+00:00", "source_sha256_before_copy": fp["sha256"],
                                      "source_sha256_after_copy": fp["sha256"]})
        path.write_text(json.dumps(manifest))
        self.paths = [r["path"] for r in manifest["files"]]

    def seal_scope(self):
        output = Path(self.step09["output_dir"])
        files = {str(p.relative_to(output.parent)): geo_tests.fingerprint(p) for p in output.iterdir() if p.is_file()}
        (output.parent / "verification.json").write_text(json.dumps({"status": "passed", "artifacts": files}))

    def run_prepare(self, **changes):
        self.counter += 1
        args = dict(data_root=self.root, output_dir=f"outputs/stations-{self.counter}", scope_dir=self.scope_dir,
                    inspection_dir=self.fixture.prior, coordinate_operation=FIXED_COORDINATE_OPERATION,
                    required_paths=self.paths, order_review=self.review, poi_layer="poi")
        args.update(changes)
        return prepare_station_files(self.fixture.station, self.fixture.lad_path, self.order, self.poi, **args)

    def failure(self, **changes):
        with self.assertRaises(StationPreparationError) as caught:
            self.run_prepare(**changes)
        report = caught.exception.report
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["validation_complete"])
        self.assertEqual(json.loads((Path(report["output_dir"]) / "station_preparation_report.json").read_text()), report)
        return report

    def test_end_to_end_audit_all_rows_group_blocked_and_roundtrip(self):
        report = self.run_prepare()
        self.assertEqual(report["source_record_count"], 5)
        self.assertEqual(report["entity_record_count"], 3)
        self.assertEqual(report["selected_entity_count"], 1)
        self.assertEqual(report["query_eligible_count"], 0)
        self.assertEqual(report["coverage_counts"], {"500": {"complete": 0, "incomplete": 1}, "1000": {"complete": 0, "incomplete": 1}})
        output = Path(report["output_dir"])
        stations = pq.read_table(output / "stations.parquet").to_pylist()
        selected = next(r for r in stations if r["selected"])
        self.assertEqual(selected["raw_ids"], ["01", "02", ""])
        with (output / "station_audit.csv").open(newline="") as stream:
            audit = list(csv.DictReader(stream))
        self.assertEqual(len(audit), 5)
        self.assertEqual(json.loads(audit[0]["raw_fields"])["note"], "quoted\nnewline")
        self.assertEqual(pq.read_table(output / "stations.parquet").schema, station_parquet_schemas()[0])

    def test_valid_group_queryable_and_empty_input_schema(self):
        for rows, entities in (([["01", "52", "-1", "a"], ["02", "52", "-1", "b"]], 1), ([], 0)):
            self.fixture.write_stations(rows, issues=[])
            self.refresh_manifest()
            self.step09 = self.fixture.run_scope()
            self.scope_dir = str(Path(self.step09["output_dir"]).relative_to(self.root))
            self.seal_scope()
            report = self.run_prepare()
            self.assertEqual(report["selected_entity_count"], entities)
            self.assertEqual(report["query_eligible_count"], entities)
            self.assertEqual(report["coverage_counts"]["1000"]["complete"], entities)
            self.assertEqual(pq.read_table(Path(report["output_dir"]) / "station_radius_base.parquet").schema, station_parquet_schemas()[1])
            self.assertEqual(set(report["pua_entity_counts"]), {"City", "Other"})

    def test_order_explicit_identity_crs_and_malformed_extent(self):
        valid = read_order_extent(self.order, self.poi, data_root=self.root, poi_layer="poi", review=self.review)
        self.assertEqual(valid["bounds"], [0, 0, 700000, 1300000])
        self.assertFalse(valid["poi_metadata"]["extent_is_coverage_boundary"])
        variants = [self.order_text.replace("Filename: poi_test/poi_test.gpkg", "Filename: other/other.gpkg"),
                    self.order_text + "Extent: 0, 0 - 1, 1\n", self.order_text.replace("0, 0 - 700000, 1300000", "nan, 0 - 700000, 1300000"),
                    self.order_text.replace("910000 square", "100 square"), self.order_text.replace("700000, 1300000", "1, 1", 1)]
        for text in variants:
            (self.root / self.order).write_text(text)
            with self.assertRaises(ValueError):
                read_order_extent(self.order, self.poi, data_root=self.root, poi_layer="poi", review=self.review)
        (self.root / self.order).write_text(self.order_text)
        with self.assertRaises(ValueError):
            read_order_extent(self.order, self.poi, data_root=self.root, poi_layer="poi", review={})
        with closing(sqlite3.connect(self.root / self.poi)) as db:
            db.execute("UPDATE gpkg_spatial_ref_sys SET definition=?", (CRS.from_epsg(4326).to_wkt(),))
            db.commit()
        with self.assertRaises(ValueError):
            read_order_extent(self.order, self.poi, data_root=self.root, poi_layer="poi", review=self.review)

    def test_tampered_scope_and_failed_scope_cannot_be_consumed(self):
        path = Path(self.step09["output_dir"]) / "station_pua_audit.csv"
        with path.open("a") as stream:
            stream.write("tampered")
        self.assertIn("checksum", self.failure()["errors"][0])

    def test_failed_scope_with_resealed_evidence_is_rejected(self):
        path = Path(self.step09["output_dir"]) / "pua_scope_report.json"
        report = json.loads(path.read_text())
        report["status"] = "failed"
        path.write_text(json.dumps(report))
        self.seal_scope()
        self.assertIn("completed", self.failure()["errors"][0])

    def test_source_mutation_and_missing_required_sources_fail(self):
        self.failure(required_paths=[self.fixture.station])
        with (self.root / self.fixture.station).open("a") as stream:
            stream.write("new,52,-1,new\n")
        self.failure()

    def test_mutation_during_preparation_detected_and_failed_state_saved(self):
        from src.spatial import prepare_stations as original
        def mutated(*args, **kwargs):
            result = original(*args, **kwargs)
            with (self.root / self.order).open("a") as stream:
                stream.write("changed\n")
            return result
        with patch("src.spatial.prepare_stations", mutated):
            self.failure()

    def test_output_directory_overwrite_escape_and_symlink_refused(self):
        report = self.run_prepare()
        original = (Path(report["output_dir"]) / "station_preparation_report.json").read_bytes()
        for target in ("outputs/stations-1", "../escape", "data/raw/result", "outputs"):
            with self.assertRaises((ValueError, FileExistsError)):
                self.run_prepare(output_dir=target)
        (self.root / "outputs/link").symlink_to(self.root / "data/raw", target_is_directory=True)
        with self.assertRaises(ValueError):
            self.run_prepare(output_dir="outputs/link/new")
        self.assertEqual((Path(report["output_dir"]) / "station_preparation_report.json").read_bytes(), original)

    def test_scope_loader_checks_full_source_and_pipeline(self):
        with self.assertRaises(ValueError):
            load_station_scope(self.scope_dir, data_root=self.root, station_path=self.fixture.station, coordinate_operation="EPSG:27700")
        loaded = load_station_scope(self.scope_dir, data_root=self.root, station_path=self.fixture.station,
                                    coordinate_operation=FIXED_COORDINATE_OPERATION)
        self.assertEqual(len(loaded["rows"]), 5)
        self.assertEqual([r["in_target_pua"] for r in loaded["rows"]], [True, True, False, None, True])


if __name__ == "__main__":
    unittest.main()
