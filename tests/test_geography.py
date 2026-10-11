import csv
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook
from pyproj import CRS
from shapely.geometry import MultiPolygon, Point, Polygon, box

from run import DATA_ROOT
from src.data import read_lad_geometries, read_pua_definitions, station_rows
from src.geography import (GeographyError, _project_rows, build_pua_scope,
                           prepare_pua_scope, resolve_pua_lad, select_station_rows)
from src.spatial import FIXED_COORDINATE_OPERATION


def fingerprint(path, resolved=False):
    data = path.read_bytes()
    result = {"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    if resolved:
        result["resolved_path"] = str(path.resolve())
    return result


def lad(code, geom, name=None):
    return {"lad_code": code, "lad_name": name or code, "source_fid": len(code), "geometry": geom}


def definition(pua, member, number=2, **raw):
    return {"pua_name": pua, "local_authority": member, "source_row": number,
            "source_path": "cities/test.csv", "source_sheet": None,
            "raw_fields": {"pua_name": pua, "local_authority": member, **raw}}


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.lads = [lad("A", box(0, 0, 1000, 1000)), lad("B", box(1000, 0, 2000, 1000))]
        self.defs = [definition("City", "A"), definition("City", "B", 3)]

    def mapping(self, defs=None, lads=None, targets=None, crosswalk=None):
        return resolve_pua_lad(self.defs if defs is None else defs, self.lads if lads is None else lads,
                               target_puas=["City"] if targets is None else targets,
                               crosswalk={} if crosswalk is None else crosswalk)

    def scope(self, defs=None, lads=None, targets=None):
        return build_pua_scope(self.mapping(defs, lads, targets), self.lads if lads is None else lads, crs="EPSG:27700")

    def select(self, points, scope=None):
        return list(select_station_rows(({"source_record_no": i, "xy": p} for i, p in enumerate(points, 1)), scope or self.scope()))

    def test_five_hand_calculated_rows_and_internal_boundary(self):
        rows = self.select([(500, 500), (1000, 500), (0, 500), (2000.000001, 500), None])
        self.assertEqual([r["in_target_pua"] for r in rows], [True, True, True, False, None])
        self.assertEqual(rows[1]["lad_codes"], ["A", "B"])
        self.assertEqual(rows[1]["pua_names"], ["City"])
        self.assertNotIn("on_target_boundary", rows[1]["audit_reasons"])
        self.assertIn("on_target_boundary", rows[2]["audit_reasons"])
        self.assertEqual(sum(r["in_target_pua"] is True for r in rows), 3)

    def test_hole_interior_excluded_but_hole_boundary_included(self):
        g = Polygon([(0, 0), (1000, 0), (1000, 1000), (0, 1000)],
                    [[(400, 400), (600, 400), (600, 600), (400, 600)]])
        scope = self.scope([definition("City", "A")], [lad("A", g)])
        rows = self.select([(500, 500), (400, 500), (399.999999, 500)], scope)
        self.assertEqual([r["in_target_pua"] for r in rows], [False, True, True])

    def test_multipart_preserved_no_hull(self):
        g = MultiPolygon([box(0, 0, 10, 10), box(20, 0, 30, 10)])
        scope = self.scope([definition("City", "A")], [lad("A", g)])
        self.assertTrue(scope["study_area"].equals(g))
        self.assertEqual([r["in_target_pua"] for r in self.select([(5, 5), (15, 5), (25, 5)], scope)], [True, False, True])

    def test_shared_pua_boundary_single_candidate_and_all_memberships(self):
        scope = self.scope([definition("City", "A"), definition("Other", "B", 3)], targets=["City"])
        rows = self.select([(1000, 500), (1500, 500)], scope)
        self.assertEqual(rows[0]["pua_names"], ["City", "Other"])
        self.assertEqual(rows[0]["target_pua_names"], ["City"])
        self.assertEqual([r["in_target_pua"] for r in rows], [True, False])

    def test_missing_target_and_missing_member_never_shrink_scope(self):
        for defs, targets in [(self.defs, ["Absent"]), (self.defs + [definition("City", "Absent", 4)], ["City"])]:
            mapping = self.mapping(defs, targets=targets)
            self.assertFalse(mapping["complete"])
            with self.assertRaisesRegex(ValueError, "incomplete"):
                build_pua_scope(mapping, self.lads, crs=27700)

    def test_identical_duplicate_audited_and_geometry_once(self):
        mapping = self.mapping(self.defs + [definition("City", "A", 4)])
        self.assertTrue(mapping["complete"])
        self.assertEqual(mapping["members"]["City"], ["A", "B"])
        self.assertEqual(mapping["records"][-1]["status"], "duplicate")

    def test_conflicting_source_ordinals_block_scope(self):
        for defs in ([definition("City", "A", pua_order=1), definition("Other", "B", pua_order=1)],
                     [definition("City", "A", la_order_within_pua=1), definition("City", "B", la_order_within_pua=1)]):
            self.assertFalse(self.mapping(defs)["complete"])

    def test_explicit_code_is_not_overridden_by_name(self):
        row = definition("City", "B")
        row["lad_code"] = "A"
        self.assertFalse(self.mapping([row])["complete"])

    def test_reviewed_alias_and_version_change_preserved(self):
        entry = {"lad_code": "A", "lad_name": "A", "source_lad_code": "OLD",
                 "evidence": "independent synthetic correspondence"}
        result = self.mapping([definition("City", "Old A")], crosswalk={"Old A": entry})
        self.assertTrue(result["complete"])
        self.assertEqual(result["records"][0]["evidence"], entry)
        self.assertEqual({r["reason"] for r in result["issues"]}, {"reviewed_name_difference", "reviewed_code_change"})
        with self.assertRaisesRegex(ValueError, "crosswalk"):
            self.mapping(crosswalk={"Old A": {**entry, "lad_name": "Wrong"}})

    def test_names_not_trimmed_or_fuzzy_matched(self):
        for member in ("A ", "a", "AA", ""):
            self.assertFalse(self.mapping([definition("City", member)])["complete"])

    def test_duplicate_or_ambiguous_lad_identifiers_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate LAD"):
            self.mapping(lads=self.lads + [self.lads[0]])
        ambiguous = [lad("A", box(0, 0, 1, 1), "Name"), lad("B", box(1, 0, 2, 1), "Name")]
        self.assertFalse(self.mapping([definition("City", "Name")], lads=ambiguous)["complete"])

    def test_empty_duplicate_or_implicit_targets_rejected(self):
        for targets in ([], ["City", "City"], "City", [""]):
            with self.assertRaises(ValueError):
                self.mapping(targets=targets)

    def test_bad_geometry_and_crs_never_repaired(self):
        for g in (Polygon(), Point(0, 0), Polygon([(0, 0), (2, 2), (2, 0), (0, 2)]), None):
            with self.assertRaises(ValueError):
                self.scope([definition("City", "A")], [lad("A", g)])
        for crs in (None, "EPSG:4326", "EPSG:2263"):
            with self.assertRaises(ValueError):
                build_pua_scope(self.mapping(), self.lads, crs=crs)

    def test_mapping_order_and_same_coordinate_rows(self):
        scope = self.scope(list(reversed(self.defs)), list(reversed(self.lads)))
        expected = self.select([(500, 500), (500, 500), (2001, 500)])
        self.assertEqual(self.select([(500, 500), (500, 500), (2001, 500)], scope), expected)
        self.assertEqual(len([r for r in expected if r["in_target_pua"]]), 2)

    def test_one_lad_in_multiple_puas_audited_not_assigned_arbitrarily(self):
        defs = [definition("City", "A"), definition("Other", "A", 3)]
        mapping = self.mapping(defs)
        self.assertTrue(mapping["complete"])
        self.assertIn("lad_in_multiple_puas", [r["reason"] for r in mapping["issues"]])
        rows = self.select([(500, 500)], self.scope(defs))
        self.assertEqual(rows[0]["pua_names"], ["City", "Other"])

    def test_repeated_source_row_key_rejected(self):
        with self.assertRaisesRegex(ValueError, "unique"):
            list(select_station_rows([{"source_record_no": 1, "xy": (1, 1)}] * 2, self.scope()))


class GeographyFileTests(unittest.TestCase):
    def setUp(self):
        parent = Path(os.environ.get("TMPDIR", str(DATA_ROOT / "outputs"))).resolve()
        if not parent.is_relative_to(DATA_ROOT / "outputs"):
            raise RuntimeError("step09 test fixtures must stay in data-disk outputs")
        parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="step09-test-", dir=parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "cities").mkdir()
        (self.root / "data/raw").mkdir(parents=True)
        self.mapping, self.lad_path, self.station = "cities/pua.xlsx", "data/raw/lad.gpkg", "data/raw/stations.csv"
        self.csv = "cities/pua.csv"
        self.layer = 'LAD "test"'
        self.prior = "outputs/step06/full"
        self.number = 0
        self.write_mapping()
        self.write_lads()
        self.write_stations()
        self.write_manifest()

    def write_mapping(self, member="A"):
        book = Workbook()
        sheet = book.active
        sheet.title = "GB_mapping"
        values = [["pua_name", "local_authority"], ["City", member], ["Other", "B"]]
        for row in values:
            sheet.append(row)
        book.save(self.root / self.mapping)
        book.close()
        with (self.root / self.csv).open("w", encoding="utf-8-sig", newline="") as stream:
            csv.writer(stream).writerows(values)

    def write_lads(self, *, geometry=None, crs=27700, blob_srs=None):
        path = self.root / self.lad_path
        if path.exists():
            path.unlink()
        with closing(sqlite3.connect(path)) as db:
            db.executescript('''
                CREATE TABLE gpkg_contents (table_name TEXT, data_type TEXT, srs_id INTEGER);
                CREATE TABLE gpkg_geometry_columns (table_name TEXT, column_name TEXT, geometry_type_name TEXT, srs_id INTEGER, z INTEGER, m INTEGER);
                CREATE TABLE gpkg_spatial_ref_sys (srs_id INTEGER, definition TEXT);
                CREATE TABLE "LAD ""test""" (FID INTEGER PRIMARY KEY, code TEXT, name TEXT, geom BLOB);
            ''')
            db.execute("INSERT INTO gpkg_contents VALUES (?,?,?)", (self.layer, "features", crs))
            db.execute("INSERT INTO gpkg_geometry_columns VALUES (?,?,?,?,?,?)", (self.layer, "geom", "MULTIPOLYGON", crs, 0, 0))
            db.execute("INSERT INTO gpkg_spatial_ref_sys VALUES (?,?)", (crs, CRS.from_epsg(crs).to_wkt()))
            shapes = [geometry if geometry is not None else MultiPolygon([box(400000, 200000, 500000, 300000)]), MultiPolygon([box(600000, 200000, 700000, 300000)])]
            for fid, code, geom in zip((10, 30), ("A", "B"), shapes):
                blob = b"GP\x00" + bytes([1 | (16 if geom.is_empty else 0)]) + struct.pack("<i", crs if blob_srs is None else blob_srs) + geom.wkb
                db.execute('INSERT INTO "LAD ""test""" VALUES (?,?,?,?)', (fid, code, code, blob))
            db.commit()

    def write_stations(self, rows=None, issues=None):
        if rows is None:
            rows = [["01", "52", "-1", "quoted\nnewline"], ["02", "52", "-1", "same coordinates"],
                    ["03", "55", "-4", "outside"], ["04", "x", "-1", "invalid"], ["", "52", "-1", "missing ID"]]
        with (self.root / self.station).open("w", encoding="utf-8-sig", newline="") as stream:
            csv.writer(stream).writerows([["StationID", "Latitude", "Longitude", "note"], *rows])
        if issues is None:
            issues = [[1, "01", "co_located_different_ids", "[-1.0,52.0]"], [2, "02", "co_located_different_ids", "[-1.0,52.0]"],
                      [4, "04", "invalid_station_coordinates", "invalid"], [5, "", "missing_station_id", "missing"]] if rows else []
        directory = self.root / self.prior
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "station_quality_issues.csv").open("w", newline="") as stream:
            csv.writer(stream).writerows([["source_record_no", "StationID", "reason", "detail"], *issues])
        counts = {}
        for row in issues:
            counts[row[2]] = counts.get(row[2], 0) + 1
        fp = {self.station: fingerprint(self.root / self.station, True)}
        report = {"status": "completed", "scope": "full_source", "scan_complete": True,
                  "full_source_complete": True, "duplicate_list_complete": True, "input_bytes_unchanged": True,
                  "input_fingerprints_before": fp, "input_fingerprints_after": fp,
                  "stations": {"path": self.station, "field_mapping": {"id": "StationID", "latitude": "Latitude", "longitude": "Longitude"},
                               "source_count": len(rows), "scanned_count": len(rows), "issue_counts": counts, "groups": {}}}
        (directory / "input_inspection.json").write_text(json.dumps(report))
        self.write_evidence()

    def write_evidence(self):
        directory = self.root / self.prior
        (directory.parent / "verification.json").write_text(json.dumps({"status": "passed", "artifacts": {
            "full/" + name: fingerprint(directory / name) for name in ("input_inspection.json", "station_quality_issues.csv")}}))

    def write_manifest(self):
        entries = []
        for name in (self.mapping, self.csv, self.lad_path, self.station):
            fp = fingerprint(self.root / name)
            entries.append({"path": name, **fp, "source_location": "independent synthetic fixture",
                            "copied_at": "2026-10-11T00:00:00+00:00", "source_sha256_before_copy": fp["sha256"],
                            "source_sha256_after_copy": fp["sha256"]})
        (self.root / "data/source_manifest.json").write_text(json.dumps({"schema_version": 1, "files": entries}))

    def run_scope(self, **changes):
        self.number += 1
        args = dict(data_root=self.root, output_dir=f"outputs/run-{self.number}", target_puas=["City", "Other"],
                    mapping_sheet="GB_mapping", lad_layer=self.layer, code_field="code", name_field="name",
                    crosswalk={}, version_review={"whole_lad_members": True, "definition_version": "synthetic 2022",
                                                  "boundary_version": "synthetic 2025", "evidence": "synthetic whole-LAD fixture"},
                    inspection_dir=self.prior, coordinate_operation=FIXED_COORDINATE_OPERATION,
                    required_paths=[self.mapping, self.csv, self.lad_path, self.station], comparison_csv=self.csv, station_batch_size=2)
        args.update(changes)
        return prepare_pua_scope(self.mapping, self.lad_path, self.station, **args)

    def failure(self, **changes):
        with self.assertRaises(GeographyError) as caught:
            self.run_scope(**changes)
        report = caught.exception.report
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["scope_complete"])
        self.assertFalse(report["scan_complete"])
        self.assertFalse(report["validation_complete"])
        self.assertEqual(json.loads((Path(report["output_dir"]) / "pua_scope_report.json").read_text()), report)
        return report

    def table(self, report, name):
        with (Path(report["output_dir"]) / name).open(newline="") as stream:
            return list(csv.DictReader(stream))

    def test_real_io_five_rows_three_candidates_and_zero_city(self):
        before = {p: fingerprint(self.root / p) for p in (self.mapping, self.csv, self.lad_path, self.station)}
        report = self.run_scope()
        self.assertEqual(report["station_counts"], {"inside": 3, "outside": 1, "unknown": 1})
        self.assertEqual(report["candidate_count"], 3)
        self.assertEqual(report["zero_station_target_puas"], ["Other"])
        rows = self.table(report, "station_pua_audit.csv")
        self.assertEqual([r["source_record_no"] for r in rows], ["1", "2", "3", "4", "5"])
        self.assertEqual(json.loads(rows[0]["raw_fields"])["note"], "quoted\nnewline")
        self.assertEqual(rows[0]["raw_id"], "01")
        self.assertEqual(rows[4]["in_target_pua"], "True")
        self.assertIn("co_located_different_ids", rows[0]["prior_station_issues"])
        self.assertFalse(report["station_entities_merged"])
        self.assertEqual(before, {p: fingerprint(self.root / p) for p in before})

    def test_batch_sizes_produce_same_audits(self):
        reports = [self.run_scope(station_batch_size=n) for n in (1, 3, 100)]
        for name in ("station_pua_audit.csv", "stations_in_target_pua.csv", "pua_geometries.csv"):
            self.assertEqual(len({(Path(r["output_dir"]) / name).read_bytes() for r in reports}), 1)

    def test_large_polygon_wkb_roundtrip_restores_csv_field_limit(self):
        limit = csv.field_size_limit()
        geometry = MultiPolygon([Point(450000, 250000).buffer(10000, quad_segs=4096)])
        self.assertGreater(len(geometry.wkb_hex), limit)
        self.write_lads(geometry=geometry)
        self.write_manifest()
        self.assertTrue(self.run_scope()["validation_complete"])
        self.assertEqual(csv.field_size_limit(), limit)

    def test_empty_station_source_keeps_schema_and_all_zero_puas(self):
        self.write_stations([])
        self.write_manifest()
        report = self.run_scope()
        self.assertEqual(report["station_counts"], {"inside": 0, "outside": 0, "unknown": 0})
        self.assertEqual(report["zero_station_target_puas"], ["City", "Other"])
        self.assertEqual(self.table(report, "station_pua_audit.csv"), [])
        self.assertIn("in_target_pua", (Path(report["output_dir"]) / "stations_in_target_pua.csv").read_text())

    def test_missing_member_creates_mapping_issues_and_no_candidates(self):
        self.write_mapping("Unknown")
        self.write_manifest()
        report = self.failure()
        self.assertFalse(report["scope_complete"])
        self.assertFalse((Path(report["output_dir"]) / "stations_in_target_pua.csv").exists())
        self.assertIn("unmatched_lad", (Path(report["output_dir"]) / "pua_lad_issues.csv").read_text())

    def test_output_refuses_overwrite_escape_and_input_area(self):
        report = self.run_scope()
        before = (Path(report["output_dir"]) / "pua_scope_report.json").read_bytes()
        for output in ("outputs/run-1", "../outside", "data/raw/bad", "outputs"):
            with self.assertRaises((ValueError, FileExistsError)):
                self.run_scope(output_dir=output)
        (self.root / "outputs/link").symlink_to(self.root / "data/raw", target_is_directory=True)
        with self.assertRaises(ValueError):
            self.run_scope(output_dir="outputs/link/new")
        self.assertEqual((Path(report["output_dir"]) / "pua_scope_report.json").read_bytes(), before)

    def test_tampered_source_or_prior_artifact_fails(self):
        (self.root / self.csv).write_text("changed")
        self.failure()
        self.write_mapping()
        self.write_manifest()
        with (self.root / self.prior / "station_quality_issues.csv").open("a") as stream:
            stream.write("1,01,changed,changed\n")
        report = self.failure()
        self.assertIn("checksum mismatch", report["errors"][0])

    def test_incomplete_prior_audit_and_wrong_source_fingerprint_fail(self):
        path = self.root / self.prior / "input_inspection.json"
        original = json.loads(path.read_text())
        for changes in ({"scope": "sample"}, {"status": "failed"}, {"input_fingerprints_after": {}}):
            path.write_text(json.dumps({**original, **changes}))
            self.write_evidence()
            self.failure()

    def test_source_changes_during_processing_detected(self):
        original = select_station_rows
        def changed(rows, scope):
            yield from original(rows, scope)
            with (self.root / self.csv).open("a") as stream:
                stream.write("changed\n")
        with patch("src.geography.select_station_rows", changed):
            self.failure()

    def test_xlsx_requires_sheet_and_rejects_formula_and_name_changes(self):
        with self.assertRaisesRegex(ValueError, "sheet"):
            read_pua_definitions(self.mapping, data_root=self.root)
        self.write_mapping("=1+1")
        with self.assertRaisesRegex(ValueError, "formulas"):
            read_pua_definitions(self.mapping, data_root=self.root, sheet="GB_mapping")
        self.write_mapping()
        (self.root / self.csv).write_text("pua_name,local_authority\nCity,A \nOther,B\n")
        self.write_manifest()
        self.assertIn("disagree", self.failure()["errors"][0])

    def test_polygon_reader_fids_crs_invalid_and_header_checks(self):
        rows, metadata = read_lad_geometries(self.lad_path, data_root=self.root, layer=self.layer, code_field="code", name_field="name")
        self.assertEqual([r["source_fid"] for r in rows], [10, 30])
        self.assertEqual(metadata["crs_epsg"], 27700)
        for args in ({"crs": 4326}, {"blob_srs": 4326}, {"geometry": MultiPolygon([])},
                     {"geometry": MultiPolygon([Polygon([(0, 0), (2, 2), (2, 0), (0, 2)])])}, {"geometry": Point(0, 0)}):
            self.write_lads(**args)
            with self.assertRaises(ValueError):
                read_lad_geometries(self.lad_path, data_root=self.root, layer=self.layer, code_field="code", name_field="name")

    def test_malformed_rows_missing_invalid_range_coordinates_are_distinct_unknown(self):
        self.write_stations([["01", "", "-1", "missing"], ["02", "nan", "-1", "invalid"],
                             ["03", "91", "-1", "range"], ["04", "52", "-1", "extra", "field"]], issues=[])
        rows = list(_project_rows(station_rows(self.station, data_root=self.root), FIXED_COORDINATE_OPERATION, 3))
        self.assertEqual([r["coordinate_reasons"] for r in rows], [["missing_station_coordinates"], ["invalid_station_coordinates"],
                                                                  ["station_coordinates_out_of_range"], ["csv_field_count"]])
        self.assertTrue(all(r["xy"] is None for r in rows))
        self.assertEqual(rows[-1]["raw_values"][-1], "field")

    def test_single_projection_failure_unknown_infrastructure_failure_fatal(self):
        from src.spatial import transform_coordinates
        def failure(lon, lat, operation):
            if -4 in lon:
                raise ValueError("coordinate transformation failed: synthetic domain failure")
            return transform_coordinates(lon, lat, operation)
        with patch("src.geography.transform_coordinates", failure):
            report = self.run_scope()
        self.assertEqual(report["station_counts"], {"inside": 3, "outside": 0, "unknown": 2})
        self.assertEqual(report["station_reason_counts"]["coordinate_transformation_failed"], 1)
        with patch("src.geography.transform_coordinates", side_effect=RuntimeError("PROJ unavailable")):
            self.assertIn("PROJ unavailable", self.failure()["errors"][0])

    def test_full_source_conflict_survives_outside_counterpart(self):
        self.write_stations([["conflict", "52", "-1", "inside"], ["conflict", "55", "-4", "outside"]],
                            [[1, "conflict", "station_id_position_conflict", "all-source"], [2, "conflict", "station_id_position_conflict", "all-source"]])
        self.write_manifest()
        report = self.run_scope()
        candidate = self.table(report, "stations_in_target_pua.csv")
        self.assertEqual(len(candidate), 1)
        self.assertIn("station_id_position_conflict", candidate[0]["prior_station_issues"])
        self.assertEqual(len(self.table(report, "station_pua_audit.csv")), 2)

    def test_required_paths_and_whole_lad_review_mandatory(self):
        self.failure(required_paths=[self.mapping])
        self.failure(version_review={"whole_lad_members": False})
        self.failure(target_puas=[])
        self.failure(coordinate_operation="EPSG:27700")


class CoverageTests(unittest.TestCase):
    def coverage(self, shapes=None, bounds=(-10000, -10000, 10000, 10000), crs=27700):
        from src.geography import coverage_geometries
        if shapes is None:
            shapes = [lad("E00000001", box(0, 0, 2000, 4000)), lad("S00000001", box(2000, 0, 4000, 4000)),
                      lad("N00000001", box(-4000, 0, -2000, 2000))]
        return coverage_geometries(shapes, crs=crs, order_bounds=bounds)

    def test_all_gb_lads_union_and_internal_line_distance(self):
        from src.geography import boundary_distances
        cov = self.coverage()
        self.assertTrue(cov["gb"].equals(box(0, 0, 4000, 4000)))
        self.assertEqual(cov["members"], {"gb": ["E00000001", "S00000001"], "ni": ["N00000001"]})
        self.assertEqual(boundary_distances([(700, 2000), (2000, 2000), (0, 2000), (-100, 2000), None], cov["gb"]),
                         [700, 2000, 0, 100, None])

    def test_hole_rings_and_multipart_gap(self):
        from src.geography import boundary_distances
        shape = Polygon([(0, 0), (4000, 0), (4000, 4000), (0, 4000)],
                        [[(1500, 1500), (2500, 1500), (2500, 2500), (1500, 2500)]])
        geometry = MultiPolygon([shape, box(6000, 0, 8000, 2000)])
        positions = [(2000, 2000), (1500, 2000), (1000, 2000), (5000, 1000), (7000, 1000)]
        values = boundary_distances(positions, geometry)
        self.assertEqual(values, [500, 0, 500, 1000, 1000])
        self.assertEqual(values, [Point(p).distance(geometry.boundary) for p in positions])
        self.assertFalse(geometry.covers(Point(2000, 2000)))

    def test_order_intersection_and_empty_ni_or_coverage(self):
        cov = self.coverage([lad("W00000001", box(0, 0, 4000, 4000))], bounds=(1000, 1000, 6000, 6000))
        self.assertTrue(cov["data_coverage"].equals(box(1000, 1000, 4000, 4000)))
        self.assertTrue(cov["ni"].is_empty)
        self.assertTrue(self.coverage(bounds=(7000, 7000, 8000, 8000))["data_coverage"].is_empty)

    def test_invalid_codes_duplicate_crs_and_geometry_fail(self):
        for shapes in ([], [lad("X00000001", box(0, 0, 1, 1))], [lad("E1", box(0, 0, 1, 1))],
                       [lad("E00000001", Point(0, 0))], [lad("E00000001", Polygon())],
                       [lad("E00000001", box(0, 0, 1, 1))] * 2):
            with self.subTest(shapes=shapes), self.assertRaises(ValueError):
                self.coverage(shapes)
        for crs in (None, 4326, 2263):
            with self.subTest(crs=crs), self.assertRaises(ValueError):
                self.coverage(crs=crs)

    def test_bounds_and_coordinate_errors(self):
        from src.geography import boundary_distances
        for bounds in ((0, 0, 0, 1), (1, 0, 0, 1), (0, 0, float("inf"), 1), (False, 0, 1, 1), (0, 1), "0,0,1,1"):
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                self.coverage(bounds=bounds)
        for xy in ([(1, float("nan"))], [(1, 2, 3)], [(True, 1)]):
            with self.assertRaises(ValueError):
                boundary_distances(xy, box(0, 0, 1, 1))
        self.assertEqual(boundary_distances([], box(0, 0, 1, 1)), [])


if __name__ == "__main__":
    unittest.main()
