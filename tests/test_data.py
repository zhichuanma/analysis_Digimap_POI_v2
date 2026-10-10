from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from run import DATA_ROOT
from src.data import SourceError, SourceVerificationError, open_source, sha256, verify_sources


ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
LONG_VECTOR = b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"
LONG_SHA256 = "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"


def record(path="data/raw/abc.bin", *, size=3, digest=ABC_SHA256):
    return {
        "path": path, "size_bytes": size, "sha256": digest,
        "source_location": "synthetic-fixture:abc (not a real source)",
        "copied_at": "2026-10-10T00:00:00+00:00",
        "source_sha256_before_copy": digest, "source_sha256_after_copy": digest,
    }


class SourceTests(unittest.TestCase):
    def setUp(self):
        output_root = DATA_ROOT / "outputs"
        temp_parent = Path(os.environ.get("TMPDIR", str(output_root))).resolve()
        if not temp_parent.is_relative_to(output_root):
            raise RuntimeError("TMPDIR for source tests must be under the SPEC data root outputs/")
        temp_parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="step05-test-", dir=temp_parent)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "project"
        (self.root / "data/raw").mkdir(parents=True)
        (self.root / "cities").mkdir()
        self.source = self.root / "data/raw/abc.bin"
        self.source.write_bytes(b"abc")
        self.manifest = self.root / "data/source_manifest.json"
        self.document = {"schema_version": 1, "files": [record()]}
        self.write_manifest()

    def write_manifest(self):
        self.manifest.write_text(json.dumps(self.document, indent=2), encoding="utf-8")

    def verify(self, required_paths=("data/raw/abc.bin",)):
        return verify_sources(data_root=self.root, required_paths=required_paths)

    def failure(self, required_paths=("data/raw/abc.bin",)):
        with self.assertRaises(SourceVerificationError) as caught:
            self.verify(required_paths)
        report = caught.exception.report
        self.assertEqual(report["status"], "failed")
        self.assertEqual(json.loads(json.dumps(report)), report)
        self.assertTrue(str(caught.exception))
        return report

    def codes(self, report):
        return {e["code"] for e in report["errors"]} | {
            e["code"] for row in report["files"] for e in row["errors"]
        }

    def test_sha256_matches_independent_standard_vectors_at_different_chunk_sizes(self):
        for contents, expected in ((b"abc", ABC_SHA256), (b"", EMPTY_SHA256), (LONG_VECTOR, LONG_SHA256)):
            self.source.write_bytes(contents)
            for chunk_size in (1, 2, 7, 1048576):
                with self.subTest(length=len(contents), chunk_size=chunk_size):
                    self.assertEqual(sha256("data/raw/abc.bin", data_root=self.root, chunk_size=chunk_size), expected)

    def test_sha256_rejects_nonpositive_or_noninteger_chunks(self):
        for value in (0, -1, True, False, 1.5, "1", None):
            with self.subTest(chunk=value), self.assertRaisesRegex(ValueError, "chunk_size"):
                sha256("data/raw/abc.bin", data_root=self.root, chunk_size=value)

    def test_file_change_during_read_is_not_reported_as_verified(self):
        source = self.source

        @contextmanager
        def changing_source(relative_path, *, data_root):
            with open_source(relative_path, data_root=data_root) as stream:
                class ChangingReader:
                    changed = False

                    def fileno(self):
                        return stream.fileno()

                    def read(self, size):
                        block = stream.read(size)
                        if not self.changed:
                            source.write_bytes(b"changed-length")
                            self.changed = True
                        return block

                yield ChangingReader()

        with patch("src.data.open_source", changing_source):
            report = self.failure()
        self.assertEqual(self.codes(report), {"changed_during_read"})
        self.assertEqual(report["checked_count"], 0)
        self.assertIsNone(report["files"][0]["actual_sha256"])

    def test_open_source_is_read_only_binary_and_closes_stream(self):
        contents = b"\x00abc\r\n\xff"
        self.source.write_bytes(contents)
        with open_source("data/raw/abc.bin", data_root=self.root) as stream:
            self.assertEqual(stream.read(), contents)
            self.assertFalse(stream.writable())
            with self.assertRaises(io.UnsupportedOperation):
                stream.write(b"changed")
        self.assertTrue(stream.closed)
        self.assertEqual(self.source.read_bytes(), contents)

    def test_source_paths_must_be_relative_and_in_input_areas(self):
        for path in (str(self.source), "../outside", "data/raw/../raw/abc.bin", "outputs/a", "data/source_manifest.json", "abc.bin", "", None):
            with self.subTest(path=path), self.assertRaises(SourceError):
                with open_source(path, data_root=self.root):
                    self.fail("invalid input path was opened")

    def test_cities_and_geography_are_allowed_inputs(self):
        for name in ("cities/mapping.bin", "data/raw/geography/boundary.bin"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"abc")
            with self.subTest(path=name):
                self.assertEqual(sha256(name, data_root=self.root), ABC_SHA256)

    def test_external_and_output_symlinks_are_rejected(self):
        external = self.base / "external.bin"
        external.write_bytes(b"abc")
        outputs = self.root / "outputs"
        outputs.mkdir()
        (outputs / "a").write_bytes(b"abc")
        for name, target in (("external", external), ("output", outputs / "a")):
            (self.root / "data/raw" / name).symlink_to(target)
            with self.subTest(alias=name), self.assertRaises(SourceError):
                sha256(f"data/raw/{name}", data_root=self.root)
        (self.root / "data/raw/parent").symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(SourceError):
            sha256("data/raw/parent/external.bin", data_root=self.root)
        self.assertEqual(external.read_bytes(), b"abc")

    def test_internal_source_symlink_can_be_read(self):
        (self.root / "data/raw/alias.bin").symlink_to("abc.bin")
        self.assertEqual(sha256("data/raw/alias.bin", data_root=self.root), ABC_SHA256)

    def test_missing_directory_and_fifo_inputs_fail_without_blocking(self):
        os.mkfifo(self.root / "data/raw/pipe")
        for path, code in (("data/raw/missing", "missing_file"), ("data/raw", "not_regular_file"), ("data/raw/pipe", "not_regular_file")):
            with self.subTest(path=path), self.assertRaises(SourceError) as caught:
                sha256(path, data_root=self.root)
            self.assertEqual(caught.exception.code, code)

    def test_normal_verification_reports_expected_actual_values_and_leaves_bytes_unchanged(self):
        before_source = self.source.read_bytes()
        before_manifest = self.manifest.read_bytes()
        report = self.verify()
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["required_paths"], ["data/raw/abc.bin"])
        self.assertEqual((report["declared_count"], report["checked_count"], report["passed_count"]), (1, 1, 1))
        self.assertEqual(report["errors"], [])
        row = report["files"][0]
        self.assertEqual(row["expected_size_bytes"], 3)
        self.assertEqual(row["actual_size_bytes"], 3)
        self.assertEqual(row["expected_sha256"], ABC_SHA256)
        self.assertEqual(row["actual_sha256"], ABC_SHA256)
        self.assertEqual(row["source_sha256_before_copy"], ABC_SHA256)
        self.assertEqual(row["source_sha256_after_copy"], ABC_SHA256)
        self.assertTrue(row["required"])
        self.assertEqual(row["resolved_path"], str(self.source))
        self.assertEqual(row["status"], "passed")
        self.assertEqual(row["errors"], [])
        self.assertEqual(json.loads(json.dumps(report)), report)
        self.assertEqual(self.source.read_bytes(), before_source)
        self.assertEqual(self.manifest.read_bytes(), before_manifest)

    def test_empty_file_and_uppercase_hashes_are_valid(self):
        self.source.write_bytes(b"")
        self.document["files"] = [record(size=0, digest=EMPTY_SHA256.upper())]
        self.write_manifest()
        row = self.verify()["files"][0]
        self.assertEqual(row["actual_size_bytes"], 0)
        self.assertEqual(row["actual_sha256"], EMPTY_SHA256)
        self.assertEqual(row["expected_sha256"], EMPTY_SHA256)

    def test_equal_size_single_byte_change_fails_digest_check_only(self):
        before_manifest = self.manifest.read_bytes()
        self.source.write_bytes(b"abd")
        report = self.failure()
        self.assertEqual(self.codes(report), {"sha256_mismatch"})
        self.assertEqual(report["files"][0]["actual_size_bytes"], 3)
        self.assertNotEqual(report["files"][0]["actual_sha256"], ABC_SHA256)
        self.assertEqual(report["passed_count"], 0)
        self.assertEqual(self.manifest.read_bytes(), before_manifest)
        self.assertEqual(self.source.read_bytes(), b"abd")

    def test_truncation_and_append_fail_size_and_hash(self):
        for contents in (b"ab", b"abcd"):
            self.source.write_bytes(contents)
            with self.subTest(contents=contents):
                report = self.failure()
                self.assertEqual(self.codes(report), {"size_mismatch", "sha256_mismatch"})
                self.assertEqual(report["files"][0]["actual_size_bytes"], len(contents))

    def test_missing_manifest_is_an_explicit_failure(self):
        self.manifest.unlink()
        report = self.failure()
        self.assertEqual(self.codes(report), {"missing_manifest"})
        self.assertEqual(report["checked_count"], 0)
        self.assertFalse(self.manifest.exists())

    def test_missing_file_is_not_skipped_and_other_declared_files_are_checked(self):
        self.document["files"].append(record("data/raw/missing.bin"))
        self.write_manifest()
        report = self.failure(["data/raw/abc.bin", "data/raw/missing.bin"])
        self.assertEqual(self.codes(report), {"missing_file"})
        self.assertEqual((report["declared_count"], report["checked_count"], report["passed_count"]), (2, 1, 1))
        self.assertIsNone(report["files"][1]["actual_sha256"])
        self.assertEqual(report["files"][1]["status"], "failed")

    def test_required_file_omitted_from_manifest_cannot_pass(self):
        (self.root / "cities/mapping.bin").write_bytes(b"abc")
        report = self.failure(["data/raw/abc.bin", "cities/mapping.bin"])
        self.assertEqual(self.codes(report), {"missing_declaration"})
        self.assertEqual(report["errors"][0]["path"], "cities/mapping.bin")
        self.assertEqual(report["passed_count"], 1)
        self.assertEqual(report["status"], "failed")

    def test_every_declared_entry_is_verified_even_if_not_required(self):
        self.document["files"].append(record("cities/extra.bin"))
        self.write_manifest()
        (self.root / "cities/extra.bin").write_bytes(b"abd")
        report = self.failure()
        self.assertEqual(self.codes(report), {"sha256_mismatch"})
        self.assertEqual(report["checked_count"], 2)
        self.assertFalse(report["files"][1]["required"])

    def test_required_paths_are_nonempty_explicit_and_unique(self):
        for values in ([], (), None, "data/raw/abc.bin", {"data/raw/abc.bin"}, ["data/raw/abc.bin"] * 2, ["data/raw/abc.bin", "data/raw/./abc.bin"], ["outputs/file"], [str(self.source)]):
            with self.subTest(values=values):
                report = self.failure(values)
                self.assertEqual(self.codes(report), {"invalid_required_paths"})
                self.assertEqual(report["checked_count"], 0)

    def test_required_alias_duplicates_and_empty_manifest_are_rejected(self):
        (self.root / "data/raw/alias.bin").symlink_to("abc.bin")
        report = self.failure(["data/raw/abc.bin", "data/raw/alias.bin"])
        self.assertEqual(self.codes(report), {"invalid_required_paths"})
        self.document["files"] = []
        self.write_manifest()
        report = self.failure()
        self.assertEqual(self.codes(report), {"missing_declaration"})
        self.assertEqual(report["declared_count"], 0)

    def test_copy_evidence_mismatch_is_detected_even_when_current_copy_matches(self):
        for field in ("source_sha256_before_copy", "source_sha256_after_copy"):
            self.document["files"] = [record()]
            self.document["files"][0][field] = EMPTY_SHA256
            self.write_manifest()
            before = self.manifest.read_bytes()
            with self.subTest(field=field):
                report = self.failure()
                self.assertEqual(self.codes(report), {"copy_evidence_mismatch"})
                self.assertEqual(report["files"][0]["actual_sha256"], ABC_SHA256)
                self.assertIn(field, str(report["files"][0]["errors"]))
                self.assertEqual(self.manifest.read_bytes(), before)

    def test_missing_evidence_is_not_generated_from_current_copy(self):
        for field in record():
            self.document["files"] = [record()]
            del self.document["files"][0][field]
            self.write_manifest()
            before = self.manifest.read_bytes()
            with self.subTest(field=field):
                report = self.failure()
                self.assertEqual(self.codes(report), {"invalid_manifest"})
                self.assertEqual(report["checked_count"], 0)
                self.assertEqual(self.manifest.read_bytes(), before)

    def test_manifest_entry_field_types_and_timestamp_are_validated(self):
        changes = {
            "size_bytes": [True, -1, 3.0, "3", None],
            "sha256": ["x" * 64, "a" * 63, None, 3, " " + ABC_SHA256],
            "source_sha256_before_copy": ["bad", None],
            "source_sha256_after_copy": ["bad", None],
            "source_location": ["", "  ", None, 3],
            "copied_at": ["", "yesterday", "2026-10-10", None, 3],
        }
        for field, values in changes.items():
            for value in values:
                self.document["files"] = [record()]
                self.document["files"][0][field] = value
                self.write_manifest()
                with self.subTest(field=field, value=value):
                    report = self.failure()
                    self.assertEqual(self.codes(report), {"invalid_manifest"})
                    self.assertIn(field, report["errors"][0]["message"])

    def test_duplicate_manifest_paths_including_resolved_alias_are_rejected(self):
        (self.root / "data/raw/alias.bin").symlink_to("abc.bin")
        for name in ("data/raw/abc.bin", "data/raw/./abc.bin", "data/raw/alias.bin"):
            self.document["files"] = [record(), record(name)]
            self.write_manifest()
            with self.subTest(path=name):
                report = self.failure()
                self.assertEqual(self.codes(report), {"invalid_manifest"})
                self.assertEqual(report["checked_count"], 0)

    def test_json_syntax_duplicate_keys_and_schema_are_rejected(self):
        documents = [
            "{", "[]", "null", '{}', '{"schema_version": true, "files": []}',
            '{"schema_version": 2, "files": []}', '{"schema_version": 1, "files": {}}',
            '{"schema_version": 1, "schema_version": 1, "files": []}',
            '{"schema_version": 1, "files": [{"path":"a", "path":"b"}]}',
            '{"schema_version": 1, "files": [null]}',
            '{"schema_version": 1, "files": [], "other": NaN}',
        ]
        for document in documents:
            self.manifest.write_text(document, encoding="utf-8")
            with self.subTest(document=document):
                self.assertEqual(self.codes(self.failure()), {"invalid_manifest"})

    def test_manifest_encoding_error_is_reported(self):
        self.manifest.write_bytes(b"\xffnot-json")
        self.assertEqual(self.codes(self.failure()), {"invalid_manifest"})

    def test_manifest_paths_cannot_escape_or_move_to_input_or_output(self):
        for path in (str(self.manifest), "data/../data/source_manifest.json", "data/raw/list.json", "outputs/list.json"):
            with self.subTest(path=path), self.assertRaises(SourceVerificationError) as caught:
                verify_sources(path, data_root=self.root, required_paths=["data/raw/abc.bin"])
            self.assertEqual(self.codes(caught.exception.report), {"invalid_manifest_path"})

    def test_manifest_cannot_be_a_symlink_to_elsewhere_or_a_directory(self):
        self.manifest.unlink()
        self.manifest.symlink_to(self.source)
        self.assertEqual(self.codes(self.failure()), {"invalid_manifest_path"})
        self.manifest.unlink()
        self.manifest.mkdir()
        self.assertEqual(self.codes(self.failure()), {"not_regular_file"})

    def test_provenance_location_is_not_opened_or_changed(self):
        external = self.base / "source.bin"
        external.write_bytes(b"external content differs deliberately")
        self.document["files"][0]["source_location"] = str(external)
        self.write_manifest()
        self.assertEqual(self.verify()["status"], "passed")
        self.assertEqual(external.read_bytes(), b"external content differs deliberately")
        self.document["files"][0]["source_location"] = "unavailable-original:record-only"
        self.write_manifest()
        self.assertEqual(self.verify()["status"], "passed")

    def test_manifest_source_paths_obey_input_boundaries(self):
        (self.root / "data/raw/link").symlink_to(self.base, target_is_directory=True)
        for name in ("outputs/result", "data/raw/link/outside.bin", str(self.source), "data/raw/../raw/abc.bin"):
            self.document["files"] = [record(name)]
            self.write_manifest()
            with self.subTest(path=name):
                report = self.failure()
                self.assertEqual(self.codes(report), {"invalid_source_path"})
                self.assertEqual(report["checked_count"], 0)

    def test_failure_lists_multiple_file_problems_without_repair(self):
        self.source.write_bytes(b"abd")
        self.document["files"].append(record("cities/missing.bin"))
        self.write_manifest()
        before = self.manifest.read_bytes()
        report = self.failure(["data/raw/abc.bin", "cities/missing.bin", "data/raw/omitted.bin"])
        self.assertEqual(self.codes(report), {"sha256_mismatch", "missing_file", "missing_declaration"})
        self.assertEqual(len(report["files"]), 2)
        self.assertEqual(report["passed_count"], 0)
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_missing_manifest_and_missing_required_list_are_both_reported(self):
        self.manifest.unlink()
        report = self.failure([])
        self.assertEqual(self.codes(report), {"missing_manifest", "invalid_required_paths"})
        self.assertEqual(report["checked_count"], 0)


if __name__ == "__main__":
    unittest.main()
