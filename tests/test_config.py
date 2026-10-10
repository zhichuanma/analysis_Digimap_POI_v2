import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from run import CODE_ROOT, DATA_ROOT, load_config, validate_config
from src.data import project_path
from src.spatial import FIXED_COORDINATE_OPERATION


def sample_config():
    return {
        "radii_m": [500, 1000],
        "classification_mode": "native",
        "include_ev_charging": False,
        "poi_batch_size": 10000,
        "station_batch_size": 1000,
        "coordinate_operation": FIXED_COORDINATE_OPERATION,
        "paths": {
            "raw_dir": "data/raw",
            "source_manifest": "data/source_manifest.json",
            "cities_dir": "cities",
            "geography_dir": "data/raw/geography",
            "output_dir": "outputs",
        },
    }


class ConfigTests(unittest.TestCase):
    def setUp(self):
        output_root = DATA_ROOT / "outputs"
        temp_parent = Path(os.environ.get("TMPDIR", str(output_root))).resolve()
        if not temp_parent.is_relative_to(output_root):
            raise RuntimeError("TMPDIR for these tests must be under the SPEC data root outputs/")
        temp_parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="step04-test-", dir=temp_parent)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.code_root = self.base / "code"
        self.data_root = self.base / "data"
        self.code_root.mkdir()
        self.data_root.mkdir()
        self.config = sample_config()

    def validate(self, config=None):
        return validate_config(
            self.config if config is None else config,
            code_root=self.code_root, data_root=self.data_root,
        )

    def load_text(self, text):
        (self.code_root / "config.yaml").write_text(text, encoding="utf-8")
        return load_config(code_root=self.code_root, data_root=self.data_root)

    def cli(self, *args, cwd=None):
        return subprocess.run(
            [sys.executable, "-B", str(CODE_ROOT / "run.py"), *args],
            cwd=cwd or self.base, capture_output=True, text=True, check=False,
        )

    def test_valid_config_preserves_parameters_and_resolves_expected_paths(self):
        original = copy.deepcopy(self.config)
        result = self.validate()
        self.assertEqual(result["radii_m"], (500, 1000))
        self.assertEqual(result["classification_mode"], "native")
        self.assertIs(result["include_ev_charging"], False)
        self.assertEqual(result["poi_batch_size"], 10000)
        self.assertEqual(result["station_batch_size"], 1000)
        self.assertEqual(result["coordinate_operation"], FIXED_COORDINATE_OPERATION)
        self.assertEqual(result["code_root"], self.code_root)
        self.assertEqual(result["data_root"], self.data_root)
        self.assertEqual(result["paths"], {
            "raw_dir": self.data_root / "data/raw",
            "source_manifest": self.data_root / "data/source_manifest.json",
            "cities_dir": self.data_root / "cities",
            "geography_dir": self.data_root / "data/raw/geography",
            "output_dir": self.data_root / "outputs",
        })
        self.assertEqual(self.config, original)
        self.assertEqual(list(self.data_root.iterdir()), [])

    def test_allowed_switch_batches_and_subdirectories_are_preserved(self):
        self.config.update(include_ev_charging=True, poi_batch_size=1, station_batch_size=7)
        self.config["paths"].update(
            cities_dir="cities/version1", geography_dir="data/raw/geography/version1",
            output_dir="outputs/synthetic-run",
        )
        result = self.validate()
        self.assertIs(result["include_ev_charging"], True)
        self.assertEqual(result["poi_batch_size"], 1)
        self.assertEqual(result["station_batch_size"], 7)
        self.assertEqual(result["paths"]["output_dir"], self.data_root / "outputs/synthetic-run")
        self.assertEqual(list(self.data_root.iterdir()), [])

    def test_radii_reject_invalid_types_values_order_duplicates_and_changed_scope(self):
        invalid = [
            None, "500,1000", [], (500, 1000), [True, 1000], ["500", 1000],
            [0, 1000], [-500, 1000], [float("nan"), 1000], [500, float("inf")],
            [500, float("-inf")], [1000, 500], [500, 500], [250, 500],
            [500], [500, 1000, 1500], [500, 10 ** 1000],
        ]
        for value in invalid:
            with self.subTest(radii=value), self.assertRaisesRegex(ValueError, "radii_m"):
                self.validate({**self.config, "radii_m": value})

    def test_classification_mode_must_be_native(self):
        for value in ("research", "Native", "", None, False, 1, []):
            with self.subTest(mode=value), self.assertRaisesRegex(ValueError, "classification_mode"):
                self.validate({**self.config, "classification_mode": value})

    def test_coordinate_operation_rejects_other_parameters_operations_and_grids(self):
        for value in (None, True, 1, [], "", "+proj=longlat", "EPSG:27700",
                      FIXED_COORDINATE_OPERATION.replace("+x=446.448", "+x=0"),
                      FIXED_COORDINATE_OPERATION + " +step +proj=hgridshift +grids=@null"):
            with self.subTest(operation=value), self.assertRaisesRegex(ValueError, "coordinate_operation"):
                self.validate({**self.config, "coordinate_operation": value})

    def test_coordinate_operation_normalizes_only_whitespace_without_transforming(self):
        self.config["coordinate_operation"] = "\n  " + FIXED_COORDINATE_OPERATION.replace(" ", "\n  ")
        with patch("pyproj.Transformer.from_pipeline", side_effect=AssertionError("must not transform")):
            result = self.validate()
        self.assertEqual(result["coordinate_operation"], FIXED_COORDINATE_OPERATION)

    def test_charging_switch_must_be_bool(self):
        for value in ("false", "true", 0, 1, None, []):
            with self.subTest(switch=value), self.assertRaisesRegex(ValueError, "include_ev_charging"):
                self.validate({**self.config, "include_ev_charging": value})

    def test_batches_must_be_positive_integers(self):
        for field in ("poi_batch_size", "station_batch_size"):
            for value in (0, -1, 1.5, 1000.0, "1000", True, False, None):
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, field):
                    self.validate({**self.config, field: value})

    def test_schema_rejects_missing_unknown_and_nonstring_keys(self):
        for field in self.config:
            reduced = {key: value for key, value in self.config.items() if key != field}
            with self.subTest(missing=field), self.assertRaisesRegex(ValueError, field):
                self.validate(reduced)
        for field in self.config["paths"]:
            altered = copy.deepcopy(self.config)
            del altered["paths"][field]
            with self.subTest(missing_path=field), self.assertRaisesRegex(ValueError, field):
                self.validate(altered)
        for extra in ({"typo": 1}, {"data_root": "/tmp"}, {1: "bad"}):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, "config"):
                self.validate({**self.config, **extra})
        self.config["paths"]["ouput_dir"] = "outputs"
        with self.assertRaisesRegex(ValueError, "ouput_dir"):
            self.validate()

    def test_config_and_paths_require_mappings(self):
        for value in (None, [], "native", False):
            with self.subTest(config=value), self.assertRaisesRegex(ValueError, "config"):
                validate_config(value, code_root=self.code_root, data_root=self.data_root)
            with self.subTest(paths=value), self.assertRaisesRegex(ValueError, "paths"):
                self.validate({**self.config, "paths": value})

    def test_paths_reject_absolute_parent_components_and_invalid_text(self):
        invalid = [
            str(self.data_root / "data/raw"), "/etc/passwd", "../outside",
            "data/../data/raw", "data/raw/..", "", "   ", None, 1, True,
            "data/\x00raw", r"C:\data\raw", "C:/data/raw", "C:relative",
            r"data\..\raw",
        ]
        for value in invalid:
            with self.subTest(path=value), self.assertRaisesRegex(ValueError, "test_path"):
                project_path(value, root=self.data_root, field="test_path")
        self.config["paths"]["raw_dir"] = str(self.data_root / "data/raw")
        with self.assertRaisesRegex(ValueError, "paths.raw_dir"):
            self.validate()

    def test_roots_are_absolute_and_code_and_data_are_separate(self):
        with self.assertRaisesRegex(ValueError, "root must be an absolute"):
            project_path("file", root=Path("relative-root"))
        for data_root in (self.code_root, self.base, self.code_root / "nested"):
            with self.subTest(root=data_root), self.assertRaisesRegex(ValueError, "data_root"):
                validate_config(self.config, code_root=self.code_root, data_root=data_root)

    def test_missing_targets_resolve_without_creation_or_expansion(self):
        result = project_path("data/raw/new/file", root=self.data_root)
        self.assertEqual(result, self.data_root / "data/raw/new/file")
        self.assertFalse(result.exists())
        self.assertEqual(project_path("~/$VARIABLE", root=self.data_root), self.data_root / "~/$VARIABLE")
        self.assertEqual(list(self.data_root.iterdir()), [])

    def test_symlink_within_root_resolves_to_same_file(self):
        target = self.data_root / "file.txt"
        target.write_text("synthetic", encoding="utf-8")
        (self.data_root / "alias.txt").symlink_to(target)
        self.assertEqual(project_path("alias.txt", root=self.data_root), target)

    def test_file_symlink_outside_root_is_rejected(self):
        external = self.base / "external.txt"
        external.write_text("synthetic", encoding="utf-8")
        (self.data_root / "external.txt").symlink_to(external)
        with self.assertRaisesRegex(ValueError, "escapes root"):
            project_path("external.txt", root=self.data_root)
        self.assertEqual(external.read_text(encoding="utf-8"), "synthetic")

    def test_parent_symlinks_cannot_escape_even_for_missing_targets(self):
        # A sibling with the root's string prefix must still be rejected.
        external = self.base / "data-other"
        external.mkdir()
        (self.data_root / "alias").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "escapes root"):
            project_path("alias/new/file.txt", root=self.data_root)
        (self.data_root / "dangling").symlink_to(self.base / "not-created", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "escapes root"):
            project_path("dangling/new.txt", root=self.data_root)
        self.assertEqual(list(external.iterdir()), [])

    def test_symlink_loop_is_rejected(self):
        (self.data_root / "loop").symlink_to("loop")
        with self.assertRaisesRegex(ValueError, "cannot resolve path"):
            project_path("loop/new.txt", root=self.data_root)

    def test_output_cannot_use_roots_or_overlap_inputs(self):
        for value in (".", "data", "data/raw", "data/raw/result", "cities", "data/source_manifest.json"):
            with self.subTest(output=value), self.assertRaisesRegex(ValueError, "paths.output_dir"):
                altered = copy.deepcopy(self.config)
                altered["paths"]["output_dir"] = value
                self.validate(altered)
        (self.data_root / "code-alias").symlink_to(self.code_root, target_is_directory=True)
        self.config["paths"]["output_dir"] = "code-alias"
        with self.assertRaisesRegex(ValueError, "paths.output_dir"):
            self.validate()

    def test_output_symlink_aliases_cannot_overlap_inputs_or_root(self):
        (self.data_root / "outputs").mkdir()
        for index, target in enumerate((".", "data", "data/raw", "cities", "data/raw/geography")):
            alias = f"outputs/alias{index}"
            (self.data_root / alias).symlink_to(self.data_root / target, target_is_directory=True)
            self.config["paths"]["output_dir"] = alias
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "paths.output_dir"):
                self.validate()

    def test_output_root_symlink_into_raw_is_rejected(self):
        (self.data_root / "data/raw").mkdir(parents=True)
        (self.data_root / "outputs").symlink_to(self.data_root / "data/raw", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "paths.output_dir"):
            self.validate()

    def test_input_alias_into_output_is_rejected(self):
        (self.data_root / "data").mkdir()
        (self.data_root / "outputs").mkdir()
        (self.data_root / "data/raw").symlink_to(self.data_root / "outputs", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "overlaps input"):
            self.validate()

    def test_directory_roles_follow_spec_and_geography_stays_with_raw(self):
        invalid = {
            "raw_dir": "other-inputs", "source_manifest": "data/other.json",
            "cities_dir": "other-cities", "geography_dir": "data/raw/other-geography",
            "output_dir": "other-outputs",
        }
        for field, value in invalid.items():
            altered = copy.deepcopy(self.config)
            altered["paths"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, f"paths.{field}"):
                self.validate(altered)
        self.config["paths"]["raw_dir"] = "data/raw/subset"
        with self.assertRaisesRegex(ValueError, "paths.geography_dir"):
            self.validate()

    def test_loads_yaml_and_rejects_duplicate_keys_at_both_levels(self):
        document = yaml.safe_dump(self.config)
        result = self.load_text(document)
        self.assertEqual(result["config_path"], self.code_root / "config.yaml")
        self.assertEqual(result["radii_m"], (500, 1000))
        with self.assertRaisesRegex(ValueError, "duplicate key: radii_m"):
            self.load_text(document + "radii_m: [500, 1000]\n")
        duplicated_path = document.replace("paths:\n", "paths:\n  raw_dir: data/raw\n")
        with self.assertRaisesRegex(ValueError, "duplicate key: raw_dir"):
            self.load_text(duplicated_path)

    def test_yaml_rejects_unsafe_tags_syntax_errors_and_invalid_root(self):
        for document in ("!!python/tuple [1, 2]", "radii_m: [", "", "[]", "native", "1: value"):
            with self.subTest(document=document), self.assertRaises(ValueError):
                self.load_text(document)

    def test_config_file_paths_are_confined_to_code_root(self):
        (self.code_root / "config.yaml").write_text(yaml.safe_dump(self.config), encoding="utf-8")
        outside = self.base / "external.yaml"
        outside.write_text(yaml.safe_dump(self.config), encoding="utf-8")
        (self.code_root / "external.yaml").symlink_to(outside)
        (self.code_root / "external-dir").symlink_to(self.base, target_is_directory=True)
        for path in (
            str(self.code_root / "config.yaml"), "sub/../config.yaml", "../external.yaml",
            "external.yaml", "external-dir/external.yaml", "missing.yaml", ".",
        ):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "config_path"):
                load_config(path, code_root=self.code_root, data_root=self.data_root)

    def test_internal_config_alias_is_allowed(self):
        (self.code_root / "config.yaml").write_text(yaml.safe_dump(self.config), encoding="utf-8")
        (self.code_root / "alias.yaml").symlink_to("config.yaml")
        result = load_config("alias.yaml", code_root=self.code_root, data_root=self.data_root)
        self.assertEqual(result["config_path"], self.code_root / "config.yaml")

    def test_cli_has_identical_results_from_different_working_directories(self):
        args = ("--check-config", "--config", "config.yaml")
        first = self.cli(*args, cwd=CODE_ROOT)
        second = self.cli(*args, cwd=self.base)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(first.stderr, "")
        self.assertEqual(second.stderr, "")
        report = json.loads(first.stdout)
        self.assertEqual(report["status"], "config_valid")
        self.assertIs(report["inputs_verified"], False)
        self.assertEqual(report["config"]["radii_m"], [500, 1000])
        self.assertEqual(report["config"]["config_path"], str(CODE_ROOT / "config.yaml"))
        self.assertEqual(report["config"]["paths"]["raw_dir"], str(DATA_ROOT / "data/raw"))
        self.assertEqual(report["config"]["paths"]["output_dir"], str(DATA_ROOT / "outputs"))

    def test_cli_rejects_bad_config_path_and_unimplemented_analysis_arguments(self):
        for args in (
            ("--check-config", "--config", str(CODE_ROOT / "config.yaml")),
            ("--check-config", "--config", "sub/../config.yaml"),
            ("--check-config", "--config", f"{self.base.name}-missing.yaml"),
            ("--check-config", "--stage", "all"), (),
        ):
            result = self.cli(*args)
            with self.subTest(args=args):
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr)
                self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
