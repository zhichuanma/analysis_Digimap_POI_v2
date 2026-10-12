"""Step 11 v3 decisions, native Groups and authenticated evidence failures."""

from collections import Counter
from contextlib import closing
import copy
import csv
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import pyarrow.parquet as pq
from pyproj import CRS
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box
import yaml

from run import CODE_ROOT, DATA_ROOT
from src.classification import NativeDictionary
from src.data import (POICleaningContext, POICleaningError, POI_FIELDS, _artifact_fingerprint,
                      _fingerprint, _raw_key, clean_poi_batch, inspect_dictionary,
                      load_poi_cleaning_context, poi_cleaning_summary, poi_parquet_schema,
                      prepare_poi_sample)
from src.poi_research import FUNCTION_GROUPS, RULE_FIELDS, load_research_rules
from test_inspection import geometry_blob
import test_native_dictionary as native_tests


def configured_dictionary():
    """Unit fixture from reviewed config; real official coverage checked separately."""
    path = CODE_ROOT / "config/poi_research_rules_v3.csv"
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    groups, categories, classes = {}, {}, {}
    for r in rows:
        if r["rule_origin"] != "step07_dictionary":
            continue
        code = r["class_code"]
        groups[code[:2]] = {"dictionary_name": r["group_name"]}
        categories[code[:4]] = {"dictionary_name": r["category_name"]}
        classes[code] = {"dictionary_name": r["class_name"]}
    meta = yaml.safe_load((CODE_ROOT / "config/poi_research_rules_v3.yaml").read_text())
    dictionary = NativeDictionary(groups, categories, classes)
    rules = load_research_rules("config/poi_research_rules_v3.yaml", code_root=CODE_ROOT,
                                dictionary=dictionary, dictionary_inputs=meta["dictionary_inputs"])
    return dictionary, rules, meta


def record(fid=1, identity="001", code="09470819", xy=(2, 2), **changes):
    row = {"source_fid": fid, "ref_no": identity, "pointx_class": code,
           "feature_easting": xy[0], "feature_northing": xy[1], "pos_accuracy": 1,
           "geom": geometry_blob(Point(xy))}
    row.update(changes)
    return row


class POICleaningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dictionary, cls.rules, _ = configured_dictionary()

    def setUp(self):
        schema = {"crs": {"definition": CRS.from_epsg(27700).to_wkt()}, "coordinate_units": "metre",
                  "field_mapping": dict(POI_FIELDS), "geometry": {"column_name": "geom", "srs_id": 27700}}
        self.context = POICleaningContext("data/raw/synthetic.gpkg", "synthetic", schema,
                            {_raw_key("R"): 2}, {3: _raw_key("R"), 9: _raw_key("R")}, box(0, 0, 10, 10),
                            self.dictionary, self.rules, {str(i): str(i) for i in range(1, 5)}, {})

    def clean(self, rows, **kwargs):
        return clean_poi_batch(rows, context=self.context, **kwargs)

    def twelve(self):
        return [record(1, "P01", xy=(7, 7)), record(2, "P02", xy=(7, 7)), record(3, "R"),
                record(4, None), record(5, "P05", geom=geometry_blob(Polygon([(0,0),(2,2),(2,0),(0,2),(0,0)]))),
                record(6, "P06", geom=geometry_blob(Point())),
                record(7, "P07", geom=geometry_blob(LineString([(0,0),(1,1)]))),
                record(8, "P08", xy=(11, 5)), record(9, "R"),
                record(10, "P10", "99999999", (6, 8), feature_easting=3, feature_northing=4, pos_accuracy=3),
                record(11, "P11", "10540832"), record(12, "P12", xy=(0, 5))]

    def test_12_hand_cases_default_base_four_eight_research_three(self):
        result = self.clean(self.twelve())
        self.assertEqual([r["source_fid"] for r in result["base_pois"]], [1, 2, 10, 12])
        self.assertEqual([r["source_fid"] for r in result["analysis_pois"]], [1, 2, 12])
        self.assertEqual((result["summary"]["base_eligible_count"], result["summary"]["base_excluded_count"]), (4, 8))
        self.assertEqual(result["audit"][9]["delta_m"], 5)

    def test_12_hand_cases_switch_base_five_seven_research_stays_three(self):
        result = self.clean(self.twelve(), include_ev_charging=True)
        self.assertEqual([r["source_fid"] for r in result["base_pois"]], [1, 2, 10, 11, 12])
        self.assertEqual((result["summary"]["base_eligible_count"], result["summary"]["base_excluded_count"]), (5, 7))
        self.assertEqual([r["source_fid"] for r in result["analysis_pois"]], [1, 2, 12])

    def test_retail_restaurant_office_industry_logistics_and_parking(self):
        cases = {"09470819": "retail", "01020043": "food_beverage", "02100167": "office_services",
                 "07410531": "industry_logistics", "02150221": "industry_logistics",
                 "10540736": "transport_parking", "10540735": "transport_parking", "10540737": "transport_parking"}
        result = self.clean([record(i, str(i), code) for i, code in enumerate(cases, 20)])
        self.assertEqual([r["poi_function_group"] for r in result["analysis_pois"]], list(cases.values()))
        self.assertTrue(all(r["analysis_include"] for r in result["audit"]))

    def test_legacy_function_annotations_do_not_define_composition(self):
        codes = ["01010006", "01020043", "09470819", "03170248", "02100167", "06340458", "07410531", "10540736"]
        result = self.clean([record(i, str(i), code) for i, code in enumerate(codes, 20)])
        self.assertEqual([r["poi_function_group"] for r in result["analysis_pois"]], list(FUNCTION_GROUPS))
        self.assertEqual(result["summary"]["native_group_counts"],
                         {"01": 2, "02": 1, "03": 1, "04": 0, "05": 0, "06": 1, "07": 1, "09": 1, "10": 1})
        self.assertNotIn("functional_group_counts", result["summary"])

    def test_passenger_rail_and_confirmed_mixed_class_and_entrances_included(self):
        cases = [("10570761", True), ("10570756", True), ("10570738", True), ("10570794", True)]
        rows = self.clean([record(i, str(i), code) for i, (code, _) in enumerate(cases, 20)])["audit"]
        self.assertEqual([r["analysis_include"] for r in rows], [v for _, v in cases])
        self.assertEqual(rows[2]["research_decision"], "include")
        self.assertIn("用户最新人工复核", rows[2]["classification_reason"])
        self.assertTrue(rows[2]["functional_group_pending"])
        self.assertFalse(rows[2]["classification_review"])
        self.assertEqual(rows[3]["research_decision"], "include")

    def test_confirmed_electrical_and_bus_stops_include_while_structures_exclude(self):
        codes = ["06340433", "10540730", "10540740", "03580804", "03190259", "10590732", "10550747"]
        result = self.clean([record(i, str(i), code) for i, code in enumerate(codes, 20)])
        self.assertEqual(len(result["base_pois"]), 7)
        self.assertEqual([r["class_code"] for r in result["analysis_pois"]], ["06340433", "10590732"])
        self.assertTrue(all(r["analysis_include"] or r["analysis_exclusion_reason"] for r in result["audit"]))

    def test_same_native_group_can_have_different_class_decisions(self):
        codes = ["06340458", "06340433", "07410531", "07410538", "07410534", "07380501", "07380507", "10530728", "03190257"]
        rows = self.clean([record(i, str(i), code) for i, code in enumerate(codes, 20)])["audit"]
        self.assertEqual([r["research_decision"] for r in rows], ["include", "include", "include", "exclude", "include", "include", "include", "include", "exclude"])

    def test_all_602_rules_with_both_ev_switches_and_eight_pending_groups(self):
        records = [record(i, str(i), code) for i, code in enumerate(self.rules.rows, 1000)]
        pending = ["02030047", "06340433", "06340437", "06340438", "06340443", "06340802", "10560754", "10570738"]
        for enabled in (False, True):
            with self.subTest(include_ev_charging=enabled):
                result = self.clean(records, include_ev_charging=enabled)
                self.assertEqual(len(result["audit"]), 602)
                self.assertEqual(len(result["base_pois"]), 602 if enabled else 601)
                self.assertEqual(len(result["analysis_pois"]), 585)
                self.assertEqual(Counter(r["research_decision"] for r in result["audit"]), {"include": 585, "exclude": 17})
                self.assertEqual([r["class_code"] for r in result["analysis_pois"] if r["functional_group_pending"]], pending)
                self.assertEqual(result["summary"]["functional_group_pending_count"], 8)
                self.assertEqual(sum(result["summary"]["native_group_counts"].values()), 585)
                self.assertTrue(all(not r["classification_review"] for r in result["analysis_pois"]))
                self.assertTrue(all(r["poi_function_group"] is None for r in result["analysis_pois"] if r["functional_group_pending"]))

    def test_each_native_group_uses_official_code_and_name(self):
        codes = ["01010006", "02090141", "03170240", "04240304", "05260316", "06340802", "07410534", "09470819", "10570738"]
        result = self.clean([record(i, str(i), code) for i, code in enumerate(codes, 20)])
        self.assertEqual([r["group_code"] for r in result["analysis_pois"]], ["01", "02", "03", "04", "05", "06", "07", "09", "10"])
        for row in result["analysis_pois"]:
            self.assertEqual(row["group_dictionary_name"], self.dictionary.groups[row["group_code"]]["dictionary_name"])
            self.assertEqual(row["category_dictionary_name"], self.dictionary.categories[row["category_code"]]["dictionary_name"])
            self.assertEqual(row["class_dictionary_name"], self.dictionary.classes[row["class_code"]]["dictionary_name"])
        self.assertEqual(set(result["summary"]["native_group_counts"].values()), {1})

    def test_all_24_final_user_decisions_are_applied_by_class_code(self):
        included = ["02030047", "06340433", "06340437", "06340438", "06340443", "10560754", "10570738"]
        excluded = ["03160237", "03190257", "03190259", "03580804", "03580805", "03580806", "03580807", "07410538", "10540730", "10540733", "10540734", "10540740", "10540743", "10540832", "10550747", "10560751", "10560755"]
        cases = dict.fromkeys(included, "include") | dict.fromkeys(excluded, "exclude")
        audit = self.clean([record(i, str(i), c) for i, c in enumerate(reversed(cases), 20)])["audit"]
        self.assertEqual({r["class_code"]: r["research_decision"] for r in audit}, cases)

    def test_colocated_convenience_and_other_venues_retained_ev_always_excluded(self):
        codes = ["02090138", "02090141", "02090811", "09470819", "01010006", "10540736", "10540735", "10540832"]
        for enabled in (False, True):
            result = self.clean([record(i, str(i), code, (2, 2)) for i, code in enumerate(codes, 20)], include_ev_charging=enabled)
            self.assertEqual([r["class_code"] for r in result["analysis_pois"]], codes[:-1])
            self.assertEqual(result["summary"]["native_group_counts"]["02"], 3)

    def test_missing_class_rule_preserves_known_parent_labels_without_guessing(self):
        row = self.clean([record(code="01019999")])["audit"][0]
        self.assertEqual(row["group_dictionary_name"], "ACCOMMODATION, EATING AND DRINKING")
        self.assertEqual(row["category_dictionary_name"], "ACCOMMODATION")
        self.assertIsNone(row["class_dictionary_name"])
        self.assertTrue(row["base_eligible"])
        self.assertFalse(row["analysis_include"])
        self.assertEqual(row["research_decision"], "review")

    def test_unknown_class_retains_base_but_reviews_without_other(self):
        row = self.clean([record(code="99999999")])["audit"][0]
        self.assertTrue(row["base_eligible"] and row["classification_review"])
        self.assertFalse(row["analysis_include"])
        self.assertIsNone(row["poi_function_group"])
        self.assertEqual((row["group_code"], row["category_code"]), ("99", "9999"))

    def test_missing_and_malformed_classification_fully_audited(self):
        values = [None, "", "bad", " 01010006", 1010006, 10540832.0, "１２３４５６７８"]
        result = self.clean([record(i, str(i), value) for i, value in enumerate(values, 20)])
        self.assertEqual(len(result["base_pois"]), 7)
        self.assertEqual(result["analysis_pois"], [])
        self.assertEqual(result["summary"]["classification_anomaly_count"], 7)
        self.assertEqual(result["summary"]["classification_anomaly_proportion"], 1)
        self.assertTrue(all(r["classification_review"] and r["analysis_exclusion_reason"] for r in result["audit"]))

    def test_ev_class_never_enters_research_with_either_switch(self):
        for enabled in (False, True):
            row = self.clean([record(code="10540832")], include_ev_charging=enabled)["audit"][0]
            self.assertEqual(row["base_eligible"], enabled)
            self.assertFalse(row["analysis_include"])
            self.assertEqual(row["research_decision"], "exclude")
            self.assertEqual(row["classification_status"], "unknown_dictionary")
            self.assertTrue(row["classification_review"])

    def test_multi_reason_record_has_one_audit_and_all_independent_checks(self):
        row = record(3, "R", "10540832", geom=b"broken", pos_accuracy=4)
        result = self.clean([row])
        self.assertEqual(len(result["audit"]), 1)
        self.assertEqual(set(result["audit"][0]["base_exclusion_reasons"]), {"global_duplicate_ref_no", "geometry_decode_error", "charging_facility_switch_excluded"})
        self.assertTrue(result["audit"][0]["classification_reason"])
        self.assertEqual(result["summary"]["multiple_base_exclusion_reason_count"], 1)

    def test_category_prefix_zeros_raw_class_and_id_types_preserved(self):
        inputs = [record(i, value, "01010006") for i, value in enumerate(["001", "1", 1, 1.0, " ID "], 20)]
        before = copy.deepcopy(inputs)
        rows = self.clean(inputs)["analysis_pois"]
        self.assertEqual([r["raw_ref_no"]["type"] for r in rows], ["text", "text", "integer", "real", "text"])
        self.assertEqual([r["raw_ref_no"]["value"] for r in rows], ["001", "1", "1", "1.0", " ID "])
        self.assertEqual({r["category_number"] for r in rows}, {"01"})
        self.assertEqual(inputs, before)

    def test_invalid_ids_excluded_without_normalization(self):
        values = [None, "", "  \t", float("inf"), float("nan"), b"ID", True]
        result = self.clean([record(i, value) for i, value in enumerate(values, 20)])
        self.assertEqual(result["base_pois"], [])
        self.assertEqual(len(result["audit"]), 7)
        self.assertTrue(all(r["base_exclusion_reasons"] for r in result["audit"]))

    def test_same_coordinates_unique_ids_remain_separate_outside_pua(self):
        result = self.clean([record(1, "one", xy=(9, 9), in_target_pua=False), record(2, "two", xy=(9, 9), in_target_pua=False)])
        self.assertEqual(len(result["base_pois"]), 2)
        self.assertEqual(len(result["analysis_pois"]), 2)

    def test_global_duplicates_outside_coverage_still_block_inside_row(self):
        result = self.clean([record(3, "R"), record(9, "R", xy=(11, 11))])
        self.assertEqual(result["base_pois"], [])
        self.assertIn("outside_data_coverage", result["audit"][1]["base_exclusion_reasons"])
        self.assertIn("global_duplicate_ref_no", result["audit"][0]["base_exclusion_reasons"])

    def test_batch_sizes_rowwise_and_reversed_order_have_identical_audit(self):
        rows = self.twelve()
        expected = self.clean(rows)["audit"]
        for n in (1, 2, 6, 100):
            actual = [r for start in range(0, len(rows), n) for r in self.clean(rows[start:start+n])["audit"]]
            self.assertEqual(actual, expected)
        self.assertEqual(sorted(self.clean(list(reversed(rows)))["audit"], key=lambda r: r["source_fid"]), expected)

    def test_null_empty_nonpoint_invalid_nonfinite_and_broken_geometry(self):
        blobs = [None, geometry_blob(Point()), geometry_blob(LineString([(0,0),(1,1)])),
                 geometry_blob(Polygon([(0,0),(2,2),(2,0),(0,2),(0,0)])), geometry_blob(Point(float("inf"),1)), b"broken"]
        rows = self.clean([record(i, str(i), geom=b) for i, b in enumerate(blobs, 20)])["audit"]
        self.assertEqual([r["geometry"]["status"] for r in rows], ["null", "empty", "non_point", "invalid", "nonfinite", "decode_error"])
        self.assertTrue(all(r["within_data_coverage"] is None and not r["base_eligible"] for r in rows))

    def test_header_crs_failure_is_row_exclusion_layer_crs_failure_rejected(self):
        row = self.clean([record(geom=geometry_blob(Point(1, 1), srs=4326))])["audit"][0]
        self.assertEqual(row["geometry"]["status"], "decode_error")
        schema = copy.deepcopy(self.context.schema)
        schema["crs"]["definition"] = CRS.from_epsg(4326).to_wkt()
        with self.assertRaisesRegex(ValueError, "CRS"):
            clean_poi_batch([record()], context=replace(self.context, schema=schema))

    def test_holes_multipart_and_boundaries_use_covers_without_tolerance(self):
        polygon = Polygon([(0,0),(10,0),(10,10),(0,10)], holes=[[(4,4),(6,4),(6,6),(4,6)]])
        self.context = replace(self.context, coverage=MultiPolygon([polygon, box(20,0,30,10)]))
        points = [(1,1), (0,5), (4,5), (5,5), (25,5), (15,5), (30.000001,5)]
        result = self.clean([record(i, str(i), xy=p) for i, p in enumerate(points, 20)])
        self.assertEqual([r["within_data_coverage"] for r in result["audit"]], [True, True, True, False, True, False, False])

    def test_empty_coverage_and_invalid_coverage(self):
        self.context = replace(self.context, coverage=Polygon())
        self.assertEqual(self.clean([record()])["base_pois"], [])
        for geom in (Point(2,2), Polygon([(0,0),(2,2),(2,0),(0,2),(0,0)])):
            with self.assertRaisesRegex(ValueError, "coverage"):
                clean_poi_batch([record()], context=replace(self.context, coverage=geom))

    def test_accuracy_three_four_and_attribute_difference_review_only(self):
        result = self.clean([record(20, "a", xy=(6,8), feature_easting=3, feature_northing=4, pos_accuracy=3), record(21,"b",pos_accuracy=4)])
        self.assertEqual(len(result["analysis_pois"]), 2)
        self.assertEqual((result["audit"][0]["dx"], result["audit"][0]["dy"], result["audit"][0]["delta_m"]), (3,4,5))
        self.assertTrue(all(r["position_accuracy_review"] for r in result["audit"]))

    def test_missing_attribute_coordinates_and_wrong_crs_have_no_fake_delta(self):
        rows = self.clean([record(20,"a",feature_easting=None), record(21,"b",attribute_crs="EPSG:4326"), record(22,"c",attribute_crs="bad")])["audit"]
        self.assertTrue(all(r["base_eligible"] and r["delta_m"] is None and r["dx"] is None for r in rows))
        self.assertEqual(rows[1]["quality_reasons"][0]["reason"], "attribute_coordinate_crs_mismatch")

    def test_geometric_xy_used_and_z_m_audited_without_spatial_merge(self):
        row = self.clean([record(geom=geometry_blob(Point(2,3,100)), feature_easting=7)])["audit"][0]
        self.assertEqual((row["x"],row["y"]), (2,3))
        self.assertTrue(row["base_eligible"] and row["geometry"]["has_z"])

    def test_temporal_fields_preserved_without_inferred_history(self):
        row = self.clean([record(supply_date="01-SEP-2026", topo_toid_version=3, qualifier_type="type", qualifier_data="unknown")])["audit"][0]
        self.assertEqual(row["raw_fields"]["supply_date"]["value"], "01-SEP-2026")
        self.assertTrue(row["time_audit"]["historical_existence_not_inferred"])
        self.assertEqual(row["time_audit"]["lifecycle_status"], "no_verified_lifecycle_field")

    def test_count_conservation_reasons_and_repeated_runs(self):
        result = self.clean(self.twelve())
        self.assertEqual(result, self.clean(self.twelve()))
        summary = result["summary"]
        self.assertEqual(summary["input_count"], summary["base_eligible_count"] + summary["base_excluded_count"])
        self.assertEqual(summary["input_count"], summary["analysis_included_count"] + summary["analysis_excluded_count"])
        self.assertEqual(sum(summary["native_group_counts"].values()), summary["analysis_included_count"])
        self.assertTrue(all(r["analysis_include"] or r["analysis_exclusion_reason"] for r in result["audit"]))
        self.assertEqual(len({r["source_fid"] for r in result["audit"]}), 12)

    def test_malformed_batch_and_mismatched_duplicate_fid_explicitly_rejected(self):
        for rows in ([record(), record()], [{"source_fid":1}], [record(3,"wrong")], [record(20,"R")]):
            with self.assertRaises(ValueError):
                self.clean(rows)
        with self.assertRaises(ValueError):
            self.clean([record()], include_ev_charging="true")

    def test_empty_batch_has_zero_counts_and_all_native_groups(self):
        result = self.clean([])
        self.assertEqual(result["audit"], [])
        self.assertEqual(result["summary"]["input_count"], 0)
        self.assertEqual(set(result["summary"]["native_group_counts"]), set(self.dictionary.groups))
        self.assertEqual(set(result["summary"]["native_group_counts"].values()), {0})
        self.assertIsNone(result["summary"]["classification_review_proportion"])


class ResearchRuleTests(unittest.TestCase):
    def setUp(self):
        self.dictionary, self.rules, self.meta = configured_dictionary()
        parent = Path(os.environ.get("TMPDIR", DATA_ROOT / "outputs"))
        temp = tempfile.TemporaryDirectory(prefix="step11-rules-", dir=parent)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "config").mkdir()
        self.path = self.root / self.meta["rules_file"]
        self.path.write_bytes((CODE_ROOT / self.meta["rules_file"]).read_bytes())
        self.config = self.root / "config/poi_research_rules_v3.yaml"
        self.save_meta()

    def save_meta(self):
        self.config.write_text(yaml.safe_dump(self.meta, sort_keys=False))

    def edit(self, action):
        with self.path.open(newline="") as f:
            rows = list(csv.DictReader(f))
        action(rows)
        with self.path.open("w", newline="") as f:
            w = csv.DictWriter(f,fieldnames=RULE_FIELDS);w.writeheader();w.writerows(rows)
        self.meta["rules_fingerprint"] = _artifact_fingerprint(self.path)
        self.save_meta()

    def load(self):
        return load_research_rules("config/poi_research_rules_v3.yaml", code_root=self.root, dictionary=self.dictionary, dictionary_inputs=self.meta["dictionary_inputs"])

    def test_complete_601_class_coverage_and_explicit_ev_exception(self):
        self.assertEqual(self.rules.coverage["dictionary_class_count"], 601)
        self.assertEqual(self.rules.coverage["decisions"], {"include":585,"exclude":16,"review":0})
        self.assertEqual(self.rules.coverage["all_rule_decisions"], {"include":585,"exclude":17,"review":0})
        self.assertEqual(sum(self.rules.coverage["native_group_classes"].values()), 585)
        self.assertEqual(sum(self.rules.coverage["functional_group_classes"].values()), 577)
        self.assertEqual(len(self.rules.coverage["included_without_function_group"]), 8)
        self.assertEqual(len(self.rules.coverage["policy_exceptions"]), 1)

    def test_incomplete_rules_detected_even_when_checksum_resealed(self):
        self.edit(lambda rows: rows.pop(0))
        with self.assertRaisesRegex(ValueError,"coverage incomplete"):
            self.load()

    def test_wrong_row_version_rejected(self):
        self.edit(lambda rows: rows[0].update(rule_version="poi-research-v2.0.0"))
        with self.assertRaisesRegex(ValueError,"version mismatch"):
            self.load()

    def test_valid_new_version_visible_in_rows_and_parquet_metadata(self):
        self.meta["rule_version"] = "poi-research-v2.0.0"
        self.edit(lambda rows: [r.update(rule_version=self.meta["rule_version"]) for r in rows])
        rules = self.load()
        self.assertEqual(rules.version,"poi-research-v2.0.0")
        self.assertEqual(poi_parquet_schema(rules.version).metadata[b"classification_rule_version"],b"poi-research-v2.0.0")

    def test_duplicate_rule_and_unknown_rule_rejected(self):
        self.edit(lambda rows: rows.append(dict(rows[0])))
        with self.assertRaisesRegex(ValueError,"duplicate"):
            self.load()

    def test_include_group_and_noninclude_group_contract(self):
        for group in ("Other","retail,office_services"):
            self.edit(lambda rows: next(r for r in rows if r["decision"]=="include").update(poi_function_group=group))
            with self.assertRaisesRegex(ValueError,"invalid audit function group"):
                self.load()
        self.edit(lambda rows: next(r for r in rows if r["decision"]=="include").update(poi_function_group=""))
        self.assertEqual(self.load().rows["01010002"]["decision"], "include")
        self.edit(lambda rows: next(r for r in rows if r["decision"]=="exclude").update(poi_function_group="retail"))
        with self.assertRaisesRegex(ValueError,"invalid audit function group"):
            self.load()

    def test_v3_rule_table_is_bound_without_modifying_historical_v1(self):
        old_meta = yaml.safe_load((CODE_ROOT / "config/poi_research_rules_v1.yaml").read_text())
        old = load_research_rules("config/poi_research_rules_v1.yaml", code_root=CODE_ROOT,
                                  dictionary=self.dictionary, dictionary_inputs=old_meta["dictionary_inputs"])
        self.assertEqual(old.version, "poi-research-v1.0.0")
        self.assertEqual(old.rows["02030047"]["decision"], "review")
        self.assertEqual(self.rules.rows["02030047"]["decision"], "include")

    def test_unsupported_composition_and_unmatched_policies_rejected(self):
        for field, value in (("composition_mode", "functional_groups"), ("unmatched_class_policy", "include")):
            original = self.meta[field]
            self.meta[field] = value
            self.save_meta()
            with self.assertRaisesRegex(ValueError, "native_group"):
                self.load()
            self.meta[field] = original

    def test_official_name_parent_and_dictionary_fingerprint_mismatch(self):
        self.edit(lambda rows: rows[0].update(class_name="invented"))
        with self.assertRaisesRegex(ValueError,"name/origin"):
            self.load()
        self.meta["dictionary_class_count"] = 600
        self.save_meta()
        with self.assertRaisesRegex(ValueError,"fingerprint/count"):
            self.load()

    def test_rule_table_unsealed_mutation_rejected(self):
        with self.path.open("a") as f:
            f.write("changed")
        with self.assertRaisesRegex(ValueError,"checksum"):
            self.load()

    def test_ev_inclusion_rule_forbidden(self):
        self.edit(lambda rows: next(r for r in rows if r["class_code"]=="10540832").update(decision="include",poi_function_group="transport_parking"))
        with self.assertRaisesRegex(ValueError,"EV charging"):
            self.load()

    def test_missing_reason_and_duplicate_yaml_keys_rejected(self):
        self.edit(lambda rows: rows[0].update(decision_reason=""))
        with self.assertRaisesRegex(ValueError,"reason"):
            self.load()
        with self.config.open("a") as f:
            f.write("\nschema_version: 1\n")
        with self.assertRaises(yaml.YAMLError):
            self.load()


class POIEvidenceTests(unittest.TestCase):
    """Small synthetic authenticated artifacts; never relax production checks."""
    def setUp(self):
        fixture = native_tests.NativeDictionaryTests()
        fixture.setUp()
        self.fixture = fixture
        self.addCleanup(fixture.doCleanups)
        self.root, self.poi, self.layer = fixture.root, fixture.poi, fixture.layer
        self.accuracy = "data/raw/accuracy.txt"
        (self.root/self.accuracy).write_text('Positional Accuracy Code|Description\n1|address\n2|adjacent\n3|road\n4|locality\n')
        self.scope_dir, self.coverage_dir, self.dictionary_dir = "outputs/step09/full", "outputs/step10/full", "outputs/step07/full"
        self.paths = [*fixture.paths,self.poi,fixture.fixture.station,self.accuracy]
        entries=[]
        for name in self.paths:
            fp=_artifact_fingerprint(self.root/name)
            entries.append(dict(path=name,**fp,source_location="synthetic",copied_at="2026-10-11T00:00:00+00:00",source_sha256_before_copy=fp["sha256"],source_sha256_after_copy=fp["sha256"]))
        (self.root/"data/source_manifest.json").write_text(json.dumps(dict(schema_version=1,files=entries)))
        self.seal("outputs/prior/full")
        dictionary_report=inspect_dictionary(*fixture.paths,self.poi,data_root=self.root,inspection_dir=fixture.prior,output_dir=self.dictionary_dir,layer=self.layer,required_paths=self.paths)
        self.seal(self.dictionary_dir)
        current={p:_fingerprint(p,self.root,1048576) for p in self.paths}
        base=dict(status="completed",validation_complete=True,input_bytes_unchanged=True,input_fingerprints_before=current,input_fingerprints_after=current)
        directory=self.root/self.scope_dir;directory.mkdir(parents=True)
        self.write(directory/"pua_scope_report.json",dict(base,scope_complete=True))
        self.write(directory/"scope_definition.json",dict(target_puas=["Synthetic City"]))
        (directory/"study_area.wkb").write_bytes(box(0,0,1,1).wkb)
        self.seal(self.scope_dir)
        evidence=json.loads((directory.parent/"verification.json").read_text())
        upstream=dict(scope_dir=self.scope_dir,verification=_artifact_fingerprint(directory.parent/"verification.json"),artifacts={p.name:_artifact_fingerprint(p) for p in directory.iterdir()})
        directory=self.root/self.coverage_dir;directory.mkdir(parents=True)
        self.write(directory/"station_preparation_report.json",dict(base,scope="full_source",full_source_identity_checked=True))
        inspection=json.loads((self.root/fixture.prior/"input_inspection.json").read_text())
        keys={k:inspection["poi"][k] for k in ("layers","layer","fields","field_mapping","fid_column","geometry","crs","crs_epsg","coordinate_units","source_count","cached_feature_count","cached_count_matches","extent_is_coverage_boundary")}
        self.write(directory/"coverage_definition.json",dict(crs="EPSG:27700",units="metre",predicate="covers",target_puas=["Synthetic City"],upstream_scope=upstream,input_fingerprints=current,order=dict(poi_path=self.poi,poi_layer=self.layer,poi_metadata=keys,bounds=[0,0,10,10])))
        for name in ("gb_proxy.wkb","data_coverage_proxy.wkb"):
            (directory/name).write_bytes(box(0,0,10,10).wkb)
        self.seal(self.coverage_dir)
        code=self.root/"code";code.mkdir();(code/"config").mkdir()
        version="poi-research-v3.0.0";rows=[]
        dictionary=fixture.dictionary()
        for code_value,r in dictionary.classes.items():
            rows.append(dict(class_code=code_value,group_number=code_value[:2],category_number=code_value[2:4],category_code=code_value[:4],group_name=dictionary.groups[code_value[:2]]["dictionary_name"],category_name=dictionary.categories[code_value[:4]]["dictionary_name"],class_name=r["dictionary_name"],decision="include",poi_function_group="",decision_reason="Synthetic included Class with pending audit group",rule_version=version,rule_origin="step07_dictionary"))
        rows.append(dict(class_code="10540832",group_number="10",category_number="54",category_code="1054",group_name="",category_name="",class_name="",decision="exclude",poi_function_group="",decision_reason="User EV exclusion",rule_version=version,rule_origin="user_policy_exception"))
        path=code/"config/rules.csv"
        with path.open("w",newline="") as f:
            writer=csv.DictWriter(f,fieldnames=RULE_FIELDS);writer.writeheader();writer.writerows(rows)
        meta=dict(schema_version=2,rule_version=version,rules_file="config/rules.csv",rules_fingerprint=_artifact_fingerprint(path),dictionary_inputs={p:_artifact_fingerprint(self.root/p) for p in fixture.paths},dictionary_class_count=2,policy_exception_codes=["10540832"],functional_groups={g:g for g in FUNCTION_GROUPS},subtype_policy="synthetic",time_policy="no historical inference",composition_mode="native_group",unmatched_class_policy="review")
        (code/"config/poi_research_rules_v3.yaml").write_text(yaml.safe_dump(meta))
        self.code_root=code
        self.options=dict(layer=self.layer,inspection_dir=fixture.prior,dictionary_dir=self.dictionary_dir,coverage_dir=self.coverage_dir,scope_dir=self.scope_dir,group_path=fixture.paths[0],category_path=fixture.paths[1],class_path=fixture.paths[2],accuracy_path=self.accuracy,required_paths=self.paths)

    def write(self,path,value):
        path.write_text(json.dumps(value))

    def seal(self,relative):
        directory=self.root/relative
        artifacts={str(p.relative_to(directory.parent)):_artifact_fingerprint(p) for p in directory.iterdir() if p.is_file()}
        self.write(directory.parent/"verification.json",dict(status="passed",artifacts=artifacts))

    def load(self,**changes):
        return load_poi_cleaning_context(self.poi,data_root=self.root,code_root=self.code_root,**dict(self.options,**changes))

    def mutate_json(self,relative,updates,reseal=True):
        path=self.root/relative;value=json.loads(path.read_text());value.update(updates);self.write(path,value)
        if reseal:self.seal(str(path.parent.relative_to(self.root)))

    def sample(self,**changes):
        args=dict(data_root=self.root,code_root=self.code_root,output_dir="outputs/sample",context_options=self.options,limit=5,batch_size=2)
        args.update(changes)
        return prepare_poi_sample(self.poi,**args)

    def test_verified_context_and_sample_outputs_roundtrip(self):
        self.assertEqual(len(self.load().duplicate_keys),0)
        report=self.sample()
        self.assertEqual(report["status"],"completed")
        self.assertEqual(report["summary"]["input_count"],5)
        self.assertEqual(report["summary"]["base_eligible_count"],5)
        self.assertEqual(report["summary"]["analysis_included_count"],2)
        self.assertEqual(report["summary"]["classification_review_count"],3)
        output=Path(report["output_dir"])
        table = pq.read_table(output/"research_pois.parquet")
        self.assertEqual(table.schema,poi_parquet_schema("poi-research-v3.0.0"))
        self.assertEqual(table.schema.metadata[b"schema_version"], b"2")
        self.assertEqual(table.schema.metadata[b"composition_mode"], b"native_group")
        self.assertEqual(table["poi_function_group"].to_pylist(), [None, None])
        self.assertEqual(table["functional_group_pending"].to_pylist(), [True, True])
        self.assertEqual(report["summary"]["functional_group_pending_count"], 2)
        self.assertEqual(sum(report["summary"]["native_group_counts"].values()), 2)
        for row in table.to_pylist():
            self.assertEqual(row["group_dictionary_name"], report["summary"]["native_group_names"][row["group_code"]])
        self.assertEqual(len((output/"poi_audit.jsonl").read_text().splitlines()),5)
        self.assertEqual(_artifact_fingerprint(output/"research_rules_snapshot.csv"),report["rules"]["rules_fingerprint"])
        self.assertEqual(_artifact_fingerprint(output/"research_rules_snapshot.yaml"),report["rules"]["config_fingerprint"])
        self.assertFalse(report["full_source_cleaning_run"])

    def test_sample_or_incomplete_duplicate_evidence_refused_when_resealed(self):
        path=self.fixture.prior+"/input_inspection.json"
        self.mutate_json(path,dict(scope="sample"))
        with self.assertRaisesRegex(ValueError,"full_source"):
            self.load()

    def test_duplicate_evidence_artifact_tamper_refused(self):
        path=self.root/self.fixture.prior/"duplicate_ref_nos.csv"
        with path.open("a") as f:f.write("text,A,2\n")
        with self.assertRaisesRegex(ValueError,"checksum"):
            self.load()

    def test_resealed_duplicate_key_without_all_rows_refused(self):
        path=self.root/self.fixture.prior/"duplicate_ref_nos.csv"
        with path.open("a") as f:f.write("text,A,2\n")
        self.seal(self.fixture.prior)
        with self.assertRaisesRegex(ValueError,"omits"):
            self.load()

    def test_failed_dictionary_evidence_refused(self):
        self.mutate_json(self.dictionary_dir+"/dictionary_validation.json",dict(status="failed"))
        with self.assertRaisesRegex(ValueError,"step07"):
            self.load()

    def test_wrong_coverage_crs_and_pua_binding_refused(self):
        self.mutate_json(self.coverage_dir+"/coverage_definition.json",dict(crs="EPSG:4326"))
        with self.assertRaisesRegex(ValueError,"CRS"):
            self.load()

    def test_wrong_coverage_intersection_refused(self):
        path=self.root/self.coverage_dir/"data_coverage_proxy.wkb"
        path.write_bytes(box(0,0,9,9).wkb);self.seal(self.coverage_dir)
        with self.assertRaisesRegex(ValueError,"intersection"):
            self.load()

    def test_raw_input_manifest_change_and_incomplete_required_list_refused(self):
        with (self.root/self.poi).open("ab") as f:f.write(b"changed")
        with self.assertRaises(ValueError):self.load()
        with self.assertRaisesRegex(ValueError,"required_paths"):
            self.load(required_paths=[self.poi])

    def test_wrong_layer_and_resealed_stale_input_fingerprint_refused(self):
        with self.assertRaisesRegex(ValueError,"source/layer"):
            self.load(layer="wrong")
        path=self.root/self.fixture.prior/"input_inspection.json"
        r=json.loads(path.read_text());r["input_fingerprints_before"][self.poi]["sha256"]="0"*64;r["input_fingerprints_after"][self.poi]["sha256"]="0"*64
        self.write(path,r);self.seal(self.fixture.prior)
        with self.assertRaisesRegex(ValueError,"fingerprint"):
            self.load()

    def test_failed_sample_saved_and_no_success_publication(self):
        with patch("src.data.clean_poi_batch",side_effect=ValueError("synthetic failure")):
            with self.assertRaises(POICleaningError) as error:self.sample()
        report=error.exception.report
        self.assertEqual(report["status"],"failed")
        self.assertFalse(report["validation_complete"])
        self.assertFalse((Path(report["output_dir"])/"research_pois.parquet").exists())

    def test_existing_output_and_full_source_request_refused(self):
        report=self.sample()
        with self.assertRaises(FileExistsError):self.sample()
        for limit in (None,101,False):
            with self.assertRaises(ValueError):self.sample(output_dir="outputs/forbidden",limit=limit)
        self.assertFalse((self.root/"outputs/forbidden").exists())

    def test_source_mutation_during_sample_detected_before_publication(self):
        original=clean_poi_batch
        def mutate(*args,**kwargs):
            result=original(*args,**kwargs)
            with (self.root/self.poi).open("ab") as f:f.write(b"changed")
            return result
        with patch("src.data.clean_poi_batch",mutate):
            with self.assertRaises(POICleaningError):self.sample()
        self.assertFalse((self.root/"outputs/sample/research_pois.parquet").exists())

    def test_empty_sample_schema_and_count_conservation(self):
        directory=self.root/self.coverage_dir
        value=json.loads((directory/"coverage_definition.json").read_text())
        value["order"]["bounds"]=[5,5,10,10]
        self.write(directory/"coverage_definition.json",value)
        (directory/"data_coverage_proxy.wkb").write_bytes(box(5,5,10,10).wkb)
        self.seal(self.coverage_dir)
        report=self.sample()
        self.assertEqual(report["summary"]["base_eligible_count"],0)
        self.assertEqual(report["summary"]["input_count"],5)
        for name in ("base_eligible_pois.parquet","research_pois.parquet"):
            table=pq.read_table(Path(report["output_dir"])/name)
            self.assertEqual(table.num_rows,0)
            self.assertEqual(table.schema,poi_parquet_schema("poi-research-v3.0.0"))

    def test_truncated_reader_cannot_publish_sample(self):
        with patch("src.data.poi_batches",return_value=iter([])):
            with self.assertRaises(POICleaningError) as error:self.sample()
        self.assertIn("count does not reconcile",str(error.exception))

    def test_repeated_runs_batch_sizes_and_version_change_visible_in_artifacts(self):
        first=self.sample(batch_size=1)
        second=self.sample(output_dir="outputs/repeated",batch_size=5)
        self.assertEqual(first["summary"],second["summary"])
        for name in ("base_eligible_pois.parquet","research_pois.parquet"):
            self.assertEqual(pq.read_table(Path(first["output_dir"])/name).to_pylist(),pq.read_table(Path(second["output_dir"])/name).to_pylist())
        self.assertEqual((Path(first["output_dir"])/"poi_audit.jsonl").read_bytes(),(Path(second["output_dir"])/"poi_audit.jsonl").read_bytes())
        path=self.code_root/"config/rules.csv"
        with path.open(newline="") as f:rows=list(csv.DictReader(f))
        for row in rows:row["rule_version"]="poi-research-v3.0.1"
        with path.open("w",newline="") as f:
            w=csv.DictWriter(f,fieldnames=RULE_FIELDS);w.writeheader();w.writerows(rows)
        path_meta=self.code_root/"config/poi_research_rules_v3.yaml"
        meta=yaml.safe_load(path_meta.read_text());meta["rule_version"]="poi-research-v3.0.1";meta["rules_fingerprint"]=_artifact_fingerprint(path)
        path_meta.write_text(yaml.safe_dump(meta))
        third=self.sample(output_dir="outputs/version3patch")
        self.assertEqual(third["classification_rule_version"],"poi-research-v3.0.1")
        self.assertEqual(pq.read_table(Path(third["output_dir"])/"research_pois.parquet").schema.metadata[b"classification_rule_version"],b"poi-research-v3.0.1")
        self.assertEqual({r["classification_rule_version"] for r in pq.read_table(Path(third["output_dir"])/"research_pois.parquet").to_pylist()},{"poi-research-v3.0.1"})

    def test_path_escape_and_input_alias_rejected(self):
        for target in ("outputs","../outside","data/raw/output"):
            with self.assertRaises(ValueError):self.sample(output_dir=target)
        (self.root/"outputs/link").symlink_to(self.root/"data/raw",target_is_directory=True)
        with self.assertRaises(ValueError):self.sample(output_dir="outputs/link/result")


if __name__ == "__main__":
    unittest.main()
