import csv
import json
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch

from pyproj import CRS
from shapely.geometry import LineString, Point, Polygon

from run import DATA_ROOT
from src.data import InputInspectionError, inspect_inputs, poi_batches, sha256


def geometry_blob(geometry, *, srs=27700, envelope=False, big_endian=False):
    # Independent fixture encoder: standard 8-byte GPKG header + optional XY
    # envelope + the geometry library's WKB. Production never uses this helper.
    flag = (0 if big_endian else 1) | (16 if geometry.is_empty else 0) | (2 if envelope else 0)
    endian = ">" if big_endian else "<"
    extra = struct.pack(endian + "4d", 0, 100, 0, 100) if envelope else b""
    return b"GP\x00" + bytes([flag]) + struct.pack(endian + "i", srs) + extra + geometry.wkb


class InspectionTests(unittest.TestCase):
    def setUp(self):
        output_root = DATA_ROOT / "outputs"
        parent = Path(os.environ.get("TMPDIR", str(output_root))).resolve()
        if not parent.is_relative_to(output_root):
            raise RuntimeError("inspection test files must stay in data-disk outputs/")
        parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="step06-test-", dir=parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "data/raw").mkdir(parents=True)
        self.poi = "data/raw/poi.gpkg"
        self.station = "data/raw/stations.csv"
        self.layer = 'POI "test"'
        self.run_number = 0
        self.make_poi()
        self.write_stations([
            ["S1", "51", "-1", "first\nsecond"],
            ["S2", "51", "-1", "same location"],
            ["S3", "52", "-2", ""], ["S3", "53", "-3", ""],
            ["", "54", "-4", ""], ["S4", "nan", "5", ""],
            ["S5", "91", "0", ""], ["001", "52", "-4", ""],
        ])

    def make_poi(self, rows=None, *, epsg=27700):
        if rows is None:
            rows = [
                (10, "A", "01010006", 10, 10, 1, geometry_blob(Point(13, 14))),
                (20, "B", "01010006", 0, 0, 2, geometry_blob(Point(0, 0))),
                (30, "A", "10540832", 0, 0, 3, geometry_blob(Point())),
                (40, None, None, 0, 0, 4, geometry_blob(LineString([(0, 0), (1, 1)]))),
                (50, "C", "0101000X", 0, 0, 99, geometry_blob(Polygon([(0, 0), (2, 2), (2, 0), (0, 2), (0, 0)]))),
                (60, "C", "99999999", 0, 0, None, b"broken"),
            ]
        path = self.root / self.poi
        if path.exists():
            path.unlink()
        with sqlite3.connect(path) as db:
            db.executescript('''
                CREATE TABLE gpkg_contents (table_name TEXT PRIMARY KEY, data_type TEXT, identifier TEXT, srs_id INTEGER);
                CREATE TABLE gpkg_geometry_columns (table_name TEXT PRIMARY KEY, column_name TEXT, geometry_type_name TEXT, srs_id INTEGER, z INTEGER, m INTEGER);
                CREATE TABLE gpkg_spatial_ref_sys (srs_name TEXT, srs_id INTEGER PRIMARY KEY, organization TEXT, organization_coordsys_id INTEGER, definition TEXT);
                CREATE TABLE gpkg_ogr_contents (table_name TEXT PRIMARY KEY, feature_count INTEGER);
            ''')
            table = '"' + self.layer.replace('"', '""') + '"'
            db.execute(f"CREATE TABLE {table} (feature_id INTEGER PRIMARY KEY, ref_no, pointx_class, feature_easting, feature_northing, pos_accuracy, geom BLOB)")
            db.execute("INSERT INTO gpkg_contents VALUES (?,?,?,?)", (self.layer, "features", self.layer, epsg))
            db.execute("INSERT INTO gpkg_geometry_columns VALUES (?,?,?,?,?,?)", (self.layer, "geom", "GEOMETRY", epsg, 2, 2))
            db.execute("INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?)", ("fixture", epsg, "EPSG", epsg, CRS.from_epsg(epsg).to_wkt()))
            db.execute("INSERT INTO gpkg_ogr_contents VALUES (?,?)", (self.layer, len(rows)))
            db.executemany(f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?)", rows)

    def write_stations(self, rows, header=None):
        with (self.root / self.station).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(header or ["StationID", "Latitude", "Longitude", "note"])
            writer.writerows(rows)

    def inspect(self, **kwargs):
        self.run_number += 1
        arguments = dict(data_root=self.root, output_dir=f"outputs/run-{self.run_number}", layer=self.layer,
                         poi_batch_size=2, station_batch_size=2,
                         accuracy_labels={1: "address", 2: "adjacent", 3: "road", 4: "locality"})
        arguments.update(kwargs)
        return inspect_inputs(self.poi, self.station, **arguments)

    def read_csv(self, report, name):
        with (Path(report["output_dir"]) / name).open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))

    def failure(self, **kwargs):
        with self.assertRaises(InputInspectionError) as caught:
            self.inspect(**kwargs)
        report = caught.exception.report
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["full_source_complete"])
        self.assertFalse(report["duplicate_list_complete"])
        self.assertFalse((Path(report["output_dir"]) / "duplicate_ref_nos.csv").exists())
        saved = json.loads((Path(report["output_dir"]) / "input_inspection.json").read_text())
        self.assertEqual(saved["status"], "failed")
        return report

    def test_full_source_duplicate_counts_include_first_rows_across_batches(self):
        report = self.inspect()
        self.assertTrue(report["full_source_complete"])
        self.assertTrue(report["duplicate_list_complete"])
        self.assertEqual(report["poi"]["source_count"], 6)
        self.assertEqual(report["poi"]["scanned_count"], 6)
        self.assertEqual(report["poi"]["ref_no"], {
            "missing_count": 1, "unique_nonmissing_count": 3, "duplicate_key_count": 2,
            "duplicate_row_count": 4, "excess_row_count": 2, "source_fid_count": 6,
        })
        rows = self.read_csv(report, "duplicate_ref_rows.csv")
        self.assertEqual([(r["raw_value"], int(r["source_fid"])) for r in rows], [("A", 10), ("A", 30), ("C", 50), ("C", 60)])

    def test_batch_sizes_preserve_all_audit_artifacts(self):
        reports = [self.inspect(poi_batch_size=n, station_batch_size=n) for n in (1, 2, 4, 20)]
        files = ["duplicate_ref_nos.csv", "duplicate_ref_rows.csv", "poi_quality_issues.csv", "coordinate_differences.csv", "classification_frequencies.csv", "native_level_frequencies.csv", "positional_accuracy_frequencies.csv", "station_quality_issues.csv"]
        for report in reports[1:]:
            for filename in files:
                self.assertEqual((Path(report["output_dir"]) / filename).read_bytes(), (Path(reports[0]["output_dir"]) / filename).read_bytes(), filename)

    def test_batches_preserve_noncontiguous_source_fids_and_raw_values(self):
        batches = list(poi_batches(self.poi, data_root=self.root, layer=self.layer, batch_size=4))
        self.assertEqual([len(b) for b in batches], [4, 2])
        self.assertEqual([r["source_fid"] for b in batches for r in b], [10, 20, 30, 40, 50, 60])
        self.assertEqual(batches[0][0]["pointx_class"], "01010006")
        self.assertIsInstance(batches[0][0]["geom"], bytes)

    def test_geometry_problems_remain_in_source_and_frequency_counts(self):
        report = self.inspect()
        self.assertEqual(report["poi"]["geometry_status"], {"valid_point": 2, "empty": 1, "non_point": 1, "invalid": 1, "decode_error": 1})
        expected = {("group", "01"): 2, ("group", "10"): 1, ("group", "99"): 1, ("group", "UNKNOWN"): 2}
        group_rows = {(r["level"], r["code"]): int(r["count"]) for r in self.read_csv(report, "native_level_frequencies.csv") if r["level"] == "group"}
        self.assertEqual(group_rows, expected)
        for level in ("group", "category", "class"):
            self.assertEqual(report["poi"]["frequencies"][level]["row_count"], 6)
        self.assertFalse(report["dictionary_checked"])
        self.assertFalse(report["selection_or_cleaning_applied"])

    def test_coordinate_difference_uses_comparable_denominator(self):
        report = self.inspect()
        self.assertEqual(report["poi"]["coordinate_difference"], {
            "comparable_count": 2, "uncomparable_count": 4, "zero_count": 1,
            "nonzero_count": 1, "mean_m": 2.5, "max_m": 5.0, "max_source_fid": 10,
        })
        self.assertEqual([float(row["delta_m"]) for row in self.read_csv(report, "coordinate_differences.csv")], [5.0, 0.0])

    def test_position_accuracy_counts_keep_unknown_and_missing(self):
        report = self.inspect()
        rows = self.read_csv(report, "positional_accuracy_frequencies.csv")
        self.assertEqual(sum(int(row["count"]) for row in rows), 6)
        self.assertEqual(report["poi"]["issue_counts"]["unknown_accuracy_code"], 1)
        self.assertEqual(report["poi"]["issue_counts"]["missing_or_invalid_accuracy"], 1)

    def test_station_checks_use_logical_rows_and_do_not_merge(self):
        report = self.inspect()
        info = report["stations"]
        self.assertEqual((info["source_count"], info["scanned_count"]), (8, 8))
        self.assertEqual(info["groups"], {
            "duplicate_station_id": {"group_count": 1, "affected_row_count": 2},
            "station_id_position_conflict": {"group_count": 1, "affected_row_count": 2},
            "co_located_different_ids": {"group_count": 1, "affected_row_count": 2},
        })
        issues = info["issue_counts"]
        self.assertEqual((issues["missing_station_id"], issues["invalid_station_coordinates"], issues["station_coordinates_out_of_range"]), (1, 1, 1))
        samples = [json.loads(row) for row in (Path(report["output_dir"]) / "station_sample.jsonl").read_text().splitlines()]
        self.assertEqual(samples[-1]["raw_fields"]["StationID"], "001")

    def test_sample_reports_cannot_be_consumed_as_global_duplicate_lists(self):
        report = self.inspect(sample_limit=2)
        self.assertEqual(report["scope"], "sample")
        self.assertTrue(report["scan_complete"])
        self.assertFalse(report["full_source_complete"])
        self.assertFalse(report["duplicate_list_complete"])
        self.assertEqual(report["poi"]["scanned_count"], 2)
        self.assertEqual(report["poi"]["ref_no"]["duplicate_key_count"], 0)
        self.assertFalse((Path(report["output_dir"]) / "duplicate_ref_nos.csv").exists())
        self.assertTrue((Path(report["output_dir"]) / "sample_duplicate_ref_nos.csv").exists())

    def test_sample_larger_than_source_stays_labelled_sample(self):
        report = self.inspect(sample_limit=100)
        self.assertEqual(report["poi"]["scanned_count"], 6)
        self.assertFalse(report["duplicate_list_complete"])

    def test_empty_source_is_explicitly_complete_and_has_no_mean(self):
        self.make_poi([])
        self.write_stations([])
        report = self.inspect()
        self.assertTrue(report["full_source_complete"])
        self.assertEqual(report["poi"]["scanned_count"], 0)
        self.assertIsNone(report["poi"]["coordinate_difference"]["mean_m"])
        self.assertFalse(report["quality_issues_present"])

    def test_raw_ref_types_and_leading_zeros_are_not_silently_coalesced(self):
        ids = ["001", "1", 1, 1.0, "", None, " ", "nan"]
        self.make_poi([(i + 10, value, "01010006", 0, 0, 1, geometry_blob(Point(0, 0))) for i, value in enumerate(ids)])
        counts = self.inspect()["poi"]["ref_no"]
        self.assertEqual(counts["missing_count"], 2)
        self.assertEqual(counts["unique_nonmissing_count"], 6)
        self.assertEqual(counts["duplicate_key_count"], 0)

    def test_classification_format_rules_preserve_raw_types_without_dictionary(self):
        values = ["01010006", 10540832, 1010006, 10540832.0, " 01010006", "１２３４５６７８", None, "", "nan"]
        self.make_poi([(i + 1, str(i), value, 0, 0, 1, geometry_blob(Point(0, 0))) for i, value in enumerate(values)])
        report = self.inspect()
        rows = self.read_csv(report, "classification_frequencies.csv")
        counts = {status: sum(int(r["count"]) for r in rows if r["format_status"] == status) for status in ("valid", "missing", "malformed")}
        self.assertEqual(counts, {"valid": 2, "missing": 2, "malformed": 5})
        self.assertEqual({r["dictionary_status"] for r in rows}, {"not_checked"})

    def test_geometry_header_endianness_and_envelope_are_supported(self):
        blobs = [geometry_blob(Point(1, 2), envelope=True), geometry_blob(Point(1, 2), big_endian=True), geometry_blob(Point(1, 2, 3))]
        self.make_poi([(i + 1, str(i), "01010006", 1, 2, 1, b) for i, b in enumerate(blobs)])
        self.assertEqual(self.inspect()["poi"]["geometry_status"], {"valid_point": 3})

    def test_null_nonfinite_wrong_srs_and_malformed_geometry_are_audited(self):
        blobs = [None, geometry_blob(Point(float("inf"), 1)), geometry_blob(Point(1, 2), srs=4326), b"GP\x00\x01\x34"]
        self.make_poi([(i + 1, str(i), "01010006", 1, 2, 1, b) for i, b in enumerate(blobs)])
        report = self.inspect()
        self.assertEqual(report["poi"]["geometry_status"], {"null": 1, "nonfinite": 1, "decode_error": 2})
        self.assertEqual(report["poi"]["frequencies"]["classification"]["row_count"], 4)

    def test_attribute_coordinate_failures_are_not_zero_differences(self):
        values = [(None, 0), ("bad", 0), (float("inf"), 0), ("", 0)]
        self.make_poi([(i + 1, str(i), "01010006", x, y, 1, geometry_blob(Point(0, 0))) for i, (x, y) in enumerate(values)])
        report = self.inspect()
        self.assertEqual(report["poi"]["coordinate_difference"]["comparable_count"], 0)
        self.assertEqual(report["poi"]["issue_counts"]["invalid_attribute_coordinates"], 4)

    def test_missing_required_poi_field_is_fatal(self):
        with sqlite3.connect(self.root / self.poi) as db:
            db.execute('ALTER TABLE "POI ""test""" RENAME COLUMN ref_no TO absent_id')
        self.assertIn("missing required fields", str(self.failure()["errors"]))

    def test_wrong_crs_is_not_reprojected_or_accepted(self):
        self.make_poi(epsg=4326)
        self.assertIn("EPSG:27700", str(self.failure()["errors"]))

    def test_missing_crs_definition_is_fatal(self):
        with sqlite3.connect(self.root / self.poi) as db:
            db.execute("DELETE FROM gpkg_spatial_ref_sys")
        self.assertIn("CRS definition", str(self.failure()["errors"]))

    def test_layer_ambiguity_and_missing_explicit_layer_fail(self):
        with sqlite3.connect(self.root / self.poi) as db:
            db.execute("INSERT INTO gpkg_contents VALUES ('extra','features','extra',27700)")
        self.assertIn("ambiguous", str(self.failure(layer=None)["errors"]))
        self.assertIn("not a registered", str(self.failure(layer="missing")["errors"]))

    def test_station_missing_or_duplicate_headers_fail(self):
        self.write_stations([], ["StationID", "Latitude"])
        self.assertIn("missing required fields", str(self.failure()["errors"]))
        self.write_stations([], ["StationID", "Latitude", "Longitude", "Longitude"])
        self.assertIn("unique header", str(self.failure()["errors"]))

    def test_short_and_long_station_rows_are_preserved_with_issues(self):
        self.write_stations([["A", "51"], ["B", "51", "0", "note", "extra"]])
        report = self.inspect()
        self.assertEqual(report["stations"]["scanned_count"], 2)
        self.assertEqual(report["stations"]["issue_counts"]["csv_field_count"], 2)

    def test_truncated_iterator_cannot_publish_complete_audit(self):
        original = poi_batches
        def truncated(*args, **kwargs):
            iterator = original(*args, **kwargs)
            try:
                yield next(iterator)
            finally:
                iterator.close()
        with patch("src.data.poi_batches", truncated):
            report = self.failure()
        self.assertEqual(report["poi"]["scanned_count"], 2)
        self.assertIn("row count mismatch", str(report["errors"]))

    def test_midstream_reader_error_is_saved_as_failed(self):
        def failed(*args, **kwargs):
            raise OSError("synthetic read failure")
            yield
        with patch("src.data.poi_batches", failed):
            self.assertIn("synthetic read failure", str(self.failure()["errors"]))

    def test_source_change_during_read_prevents_publication(self):
        changed = []
        def mutate(_):
            if not changed:
                with (self.root / self.poi).open("ab") as stream:
                    stream.write(b"changed")
                changed.append(True)
        self.assertIn("changed during reading", str(self.failure(progress=mutate)["errors"]))

    def test_original_sources_are_unchanged_by_successful_inspection(self):
        before = [sha256(p, data_root=self.root) for p in (self.poi, self.station)]
        report = self.inspect()
        self.assertTrue(report["input_bytes_unchanged"])
        self.assertEqual(before, [sha256(p, data_root=self.root) for p in (self.poi, self.station)])

    def test_bad_parameters_and_paths_are_rejected(self):
        for name in ("poi_batch_size", "station_batch_size", "sample_limit"):
            for value in (0, -1, True, 1.5, "2"):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    self.inspect(**{name: value})
        for output in ("data/raw/results", "outputs", "../outputs/run", "/tmp/output"):
            with self.subTest(output=output), self.assertRaises(ValueError):
                self.inspect(output_dir=output)
        with self.assertRaises(ValueError):
            list(poi_batches(self.poi, data_root=self.root, batch_size=False))
        with self.assertRaises(ValueError):
            list(poi_batches("../outside", data_root=self.root))

    def test_output_reuse_and_input_alias_are_rejected(self):
        report = self.inspect(output_dir="outputs/fixed")
        before = (Path(report["output_dir"]) / "input_inspection.json").read_bytes()
        with self.assertRaises(FileExistsError):
            self.inspect(output_dir="outputs/fixed")
        self.assertEqual(before, (Path(report["output_dir"]) / "input_inspection.json").read_bytes())
        (self.root / "outputs/link").symlink_to(self.root / "data/raw", target_is_directory=True)
        with self.assertRaises(ValueError):
            self.inspect(output_dir="outputs/link/new")

    def test_sidecar_dependent_gpkg_snapshot_is_rejected(self):
        (self.root / (self.poi + "-wal")).write_bytes(b"not part of verified snapshot")
        self.assertIn("sidecars", str(self.failure()["errors"]))


if __name__ == "__main__":
    unittest.main()
