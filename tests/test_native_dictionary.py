import csv
import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

import test_inspection as fixtures
from shapely.geometry import Point

from src.analysis import category_labels
from src.classification import classify, native_dictionary, merge_classification_frequencies
from src.data import (DictionaryValidationError, _artifact_fingerprint, inspect_dictionary,
                      inspect_inputs, load_inspection_frequencies, sha256, source_name_frequencies)


class NativeDictionaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.InspectionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.poi = self.fixture.poi
        self.layer = self.fixture.layer
        self.paths = [f"data/raw/{name}.txt" for name in ("groups", "categories", "classes")]
        self.groups = [["1", "01", "Official group"], ["2", "02", "Unused group"]]
        self.categories = [["1", "01", "Official category", "01"], ["2", "02", "Other category", "01"], ["3", "03", "Unused category", "02"]]
        self.classes = [["1", "0006", "Official A", "01"], ["2", "0013", "Official B", "02"]]
        self.headers = [["ID", "Group Number", "Group Description"],
                        ["ID", "Category Number", "Category Description", "Group Number Foreign Key"],
                        ["ID", "Class Number", "Classification Description", "Category Number Foreign Key"]]
        self.save_dictionaries()
        self.make_source(["01010006", "01010006", "99999999", "99999999", "99999999"])
        self.prior = "outputs/prior/full"
        self.output_no = 0
        self.audit_source()

    def save_dictionaries(self):
        for path, header, rows in zip(self.paths, self.headers, (self.groups, self.categories, self.classes)):
            with (self.root / path).open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.writer(stream, delimiter="|")
                writer.writerow(header)
                writer.writerows(rows)

    def make_source(self, codes):
        self.fixture.make_poi([(10*(i+1), str(i), code, 0, 0, 1, fixtures.geometry_blob(Point(0, 0))) for i, code in enumerate(codes)])
        with sqlite3.connect(self.root / self.poi) as db:
            table = '"' + self.layer.replace('"', '""') + '"'
            for column in ("groupname", "categoryname", "classname"):
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
            db.execute(f"UPDATE {table} SET groupname='Source group', categoryname='Source category', classname='Source A' WHERE pointx_class='01010006'")

    def manifest(self):
        required = [*self.paths, self.poi]
        entries = []
        for path in required:
            digest = sha256(path, data_root=self.root)
            entries.append({"path": path, "size_bytes": (self.root/path).stat().st_size,
                            "sha256": digest, "source_location": "synthetic fixture",
                            "copied_at": "2026-10-10T00:00:00+00:00",
                            "source_sha256_before_copy": digest, "source_sha256_after_copy": digest})
        (self.root / "data/source_manifest.json").write_text(json.dumps({"schema_version": 1, "files": entries}))
        return required

    def audit_source(self):
        # A synthetic complete prior audit and its explicit checksum evidence.
        prior_root = self.root / "outputs/prior"
        if (prior_root / "full").exists():
            import shutil
            shutil.rmtree(prior_root)
        inspect_inputs(self.poi, self.fixture.station, data_root=self.root, output_dir=self.prior, layer=self.layer)
        self.save_evidence()

    def save_evidence(self):
        parent = self.root / "outputs/prior"
        files = ["input_inspection.json", "classification_frequencies.csv", "native_level_frequencies.csv"]
        (parent / "verification.json").write_text(json.dumps({"status": "passed", "artifacts": {"full/"+name: _artifact_fingerprint(parent/"full"/name) for name in files}}))

    def dictionary(self):
        return native_dictionary(*self.paths, data_root=self.root)

    def prior_rows(self):
        return load_inspection_frequencies(self.prior, data_root=self.root, poi_path=self.poi, layer=self.layer)["rows"]

    def inspect(self, **kwargs):
        self.output_no += 1
        params = dict(data_root=self.root, inspection_dir=self.prior,
                      output_dir=f"outputs/step07-{self.output_no}", layer=self.layer,
                      required_paths=self.manifest(), batch_size=2)
        params.update(kwargs)
        return inspect_dictionary(*self.paths, self.poi, **params)

    def artifact(self, report, name):
        with (Path(report["output_dir"]) / name).open(newline="", encoding="utf-8") as stream:
            return list(csv.DictReader(stream))

    def test_complete_codes_and_unreferenced_parents_are_preserved(self):
        dictionary = self.dictionary()
        self.assertEqual(set(dictionary.groups), {"01", "02"})
        self.assertEqual(set(dictionary.categories), {"0101", "0102", "0203"})
        self.assertEqual(set(dictionary.classes), {"01010006", "01020013"})
        self.assertEqual(dictionary.classes["01010006"]["local_code"], "0006")
        self.assertEqual(dictionary.classes["01010006"]["parent_code"], "0101")
        self.assertEqual(dictionary.classes["01010006"]["source_record_no"], 1)

    def test_same_class_fragment_under_distinct_parents_remains_distinct(self):
        self.classes.append(["3", "0006", "Other A", "02"])
        self.save_dictionaries()
        self.assertIn("01020006", self.dictionary().classes)

    def test_duplicate_ids_are_not_silently_overwritten(self):
        for rows in (self.groups, self.categories, self.classes):
            with self.subTest(rows=rows):
                saved = rows[1][0]
                rows[1][0] = rows[0][0]
                self.save_dictionaries()
                with self.assertRaisesRegex(ValueError, "duplicate ID"):
                    self.dictionary()
                rows[1][0] = saved

    def test_duplicate_complete_class_code_is_fatal(self):
        self.classes.append(["3", "0006", "Duplicate", "01"])
        self.save_dictionaries()
        with self.assertRaisesRegex(ValueError, "duplicate complete class"):
            self.dictionary()

    def test_ambiguous_category_foreign_key_target_is_fatal(self):
        self.categories.append(["4", "01", "Ambiguous", "02"])
        self.save_dictionaries()
        with self.assertRaisesRegex(ValueError, "ambiguous Category Number"):
            self.dictionary()

    def test_missing_foreign_keys_are_fatal(self):
        for rows in (self.categories, self.classes):
            with self.subTest(rows=rows):
                saved = rows[0][3]
                rows[0][3] = "99"
                self.save_dictionaries()
                with self.assertRaisesRegex(ValueError, "missing .* parent"):
                    self.dictionary()
                rows[0][3] = saved

    def test_local_code_format_is_not_repaired(self):
        for value in ("6", "0006 ", "０００６", "", "00006"):
            self.classes[0][1] = value
            self.save_dictionaries()
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "local code"):
                self.dictionary()

    def test_bad_header_and_invalid_utf8_fail(self):
        path = self.root / self.paths[0]
        path.write_text("wrong|header\n")
        with self.assertRaisesRegex(ValueError, "header"):
            self.dictionary()
        path.write_bytes(b"\xff")
        with self.assertRaises(UnicodeDecodeError):
            self.dictionary()

    def test_outer_join_preserves_both_unmatched_sides_and_total(self):
        result = merge_classification_frequencies(self.dictionary(), self.prior_rows())
        actual = {r["class_code"]: (r["source_count"], r["dictionary_status"]) for r in result["classes"]}
        self.assertEqual(actual, {"01010006": (2, "matched"), "01020013": (0, "dictionary_only"), "99999999": (3, "source_only")})
        self.assertEqual(result["source_count"], 5)
        self.assertTrue(all(sum(n.values()) == 5 for n in result["level_counts"].values()))

    def test_missing_malformed_and_raw_types_survive_audit(self):
        self.make_source([None, "", "bad", 10540832, "01010006"])
        self.audit_source()
        result = merge_classification_frequencies(self.dictionary(), self.prior_rows())
        unknown = next(r for r in result["classes"] if r["class_code"] == "UNKNOWN")
        self.assertEqual(unknown["source_count"], 3)
        self.assertEqual(len(result["raw_audit"]), 5)
        self.assertEqual(result["source_count"], 5)

    def test_source_dictionary_and_code_fallback(self):
        report = self.inspect()
        rows = {(r["level"], r["code"]): r for r in self.artifact(report, "category_labels.csv")}
        self.assertEqual((rows["class", "01010006"]["label"], rows["class", "01010006"]["label_source"]), ("Source A", "source"))
        self.assertEqual((rows["class", "01020013"]["label"], rows["class", "01020013"]["label_source"]), ("Official B", "dictionary"))
        self.assertEqual((rows["class", "99999999"]["label"], rows["class", "99999999"]["label_source"]), ("99999999", "code_fallback"))
        self.assertEqual(rows["group", "02"]["source_count"], "0")

    def test_unicode_tie_order_and_whitespace_missing_are_explicit(self):
        merged = merge_classification_frequencies(self.dictionary(), self.prior_rows())
        names = []
        for level, codes in merged["level_counts"].items():
            for code, n in codes.items():
                names.extend([{"level": level, "code": code, "raw_type": "text", "raw_value": name, "count": count}
                              for name, count in ([("Z", 1), ("A", 1)] if n == 2 else [(" ", n)])])
        labels = category_labels(self.dictionary(), merged["level_counts"], names)
        known = next(r for r in labels if r["level"] == "class" and r["code"] == "01010006")
        self.assertEqual(known["label"], "A")
        self.assertTrue(known["source_name_conflict"])
        unknown = next(r for r in labels if r["level"] == "class" and r["code"] == "99999999")
        self.assertEqual(unknown["source_name_missing_count"], 3)

    def test_source_mode_beats_sort_order_and_keeps_same_name_different_codes(self):
        dictionary = self.dictionary()
        counts = {"group": {}, "category": {}, "class": {"01010006": 3, "01020013": 1}}
        names = [{"level": "class", "code": "01010006", "raw_type": "text", "raw_value": "Z", "count": 2},
                 {"level": "class", "code": "01010006", "raw_type": "text", "raw_value": "A", "count": 1},
                 {"level": "class", "code": "01020013", "raw_type": "text", "raw_value": "Z", "count": 1}]
        labels = [r for r in category_labels(dictionary, counts, names) if r["level"] == "class"]
        self.assertEqual([r["label"] for r in labels], ["Z", "Z"])

    def test_known_parent_and_unknown_class_statuses_are_independent(self):
        self.make_source(["01019999"])
        self.audit_source()
        rows = self.artifact(self.inspect(), "category_labels.csv")
        statuses = {(r["level"], r["code"]): r["dictionary_status"] for r in rows}
        self.assertEqual(statuses["group", "01"], "matched")
        self.assertEqual(statuses["category", "0101"], "matched")
        self.assertEqual(statuses["class", "01019999"], "source_only")

    def test_names_and_results_are_batch_invariant(self):
        reports = [self.inspect(batch_size=n) for n in (1, 2, 4, 20)]
        for name in ("source_name_frequencies.csv", "category_labels.csv", "classification_dictionary.csv"):
            expected = self.artifact(reports[0], name)
            for report in reports[1:]:
                self.assertEqual(self.artifact(report, name), expected)

    def test_sample_failed_and_incomplete_audits_are_rejected(self):
        path = self.root / self.prior / "input_inspection.json"
        original = json.loads(path.read_text())
        for updates in ({"scope": "sample"}, {"status": "failed"}, {"full_source_complete": False}, {"duplicate_list_complete": False}):
            path.write_text(json.dumps(dict(original, **updates)))
            self.save_evidence()
            with self.subTest(updates=updates), self.assertRaisesRegex(ValueError, "full_source"):
                self.prior_rows()

    def test_changed_frequency_artifact_is_rejected(self):
        path = self.root / self.prior / "classification_frequencies.csv"
        with path.open("a") as stream:
            stream.write("changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.prior_rows()

    def test_current_source_change_is_rejected(self):
        with (self.root / self.poi).open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            self.prior_rows()

    def test_rehashed_but_inconsistent_level_frequency_is_rejected(self):
        path = self.root / self.prior / "native_level_frequencies.csv"
        value = path.read_text().replace("class,01010006,2", "class,01010006,9")
        path.write_text(value)
        self.save_evidence()
        with self.assertRaisesRegex(ValueError, "totals disagree"):
            self.prior_rows()

    def test_failed_dictionary_report_is_saved_and_not_complete(self):
        self.classes.append(["3", "0006", "Duplicate", "01"])
        self.save_dictionaries()
        with self.assertRaises(DictionaryValidationError) as caught:
            self.inspect()
        report = caught.exception.report
        self.assertFalse(report["validation_complete"])
        self.assertEqual(report["status"], "failed")
        self.assertEqual(json.loads((Path(report["output_dir"])/"dictionary_validation.json").read_text())["status"], "failed")
        self.assertFalse((Path(report["output_dir"])/"category_labels.csv").exists())

    def test_output_reuse_and_outside_paths_are_rejected(self):
        report = self.inspect()
        relative = str(Path(report["output_dir"]).relative_to(self.root))
        with self.assertRaises(FileExistsError):
            self.inspect(output_dir=relative)
        for path in ("data/raw/new", "../outside", "/tmp/new", "outputs"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.inspect(output_dir=path)

    def test_missing_required_source_cannot_publish_complete_report(self):
        with self.assertRaises(DictionaryValidationError):
            self.inspect(required_paths=[self.poi])

    def test_source_mutation_during_scan_cannot_publish_complete_report(self):
        changed = []
        def mutate(_):
            if not changed:
                with (self.root / self.poi).open("ab") as stream:
                    stream.write(b"changed")
                changed.append(True)
        with self.assertRaises(DictionaryValidationError) as caught:
            self.inspect(progress=mutate)
        self.assertFalse(caught.exception.report["validation_complete"])

    def test_raw_frequency_and_name_totals_cannot_disagree(self):
        rows = self.prior_rows()
        rows[0]["count"] += 1
        target = "outputs/name-index.sqlite"
        with self.assertRaisesRegex(ValueError, "raw frequencies differ"):
            source_name_frequencies(self.poi, data_root=self.root, layer=self.layer,
                                    index_path=target, expected_rows=rows)
        with self.assertRaisesRegex(ValueError, "name counts disagree"):
            category_labels(self.dictionary(), {"group": {}, "category": {}, "class": {"01010006": 1}}, [])

    def test_charging_switch_and_unchanged_inputs(self):
        before = {p: sha256(p, data_root=self.root) for p in [*self.paths, self.poi]}
        report = self.inspect()
        self.assertTrue(report["input_bytes_unchanged"])
        self.assertEqual(before, {p: sha256(p, data_root=self.root) for p in before})
        dictionary = self.dictionary()
        for enabled in (False, True):
            self.assertEqual(classify("10540832", dictionary.classes, include_ev_charging=enabled).include_in_count, enabled)


if __name__ == "__main__":
    unittest.main()
