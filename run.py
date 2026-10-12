"""Run, verify and review the reproducible five-stage Digimap POI analysis."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from src.data import project_path
from src.spatial import RADII_M, validate_coordinate_operation


CODE_ROOT = Path(__file__).resolve().parent
DATA_ROOT = (Path.home() / "Work" / "analysis_Digimap_POI_v2").resolve()
CONFIG_FIELDS = {
    "radii_m", "classification_mode", "include_ev_charging",
    "poi_batch_size", "station_batch_size", "coordinate_operation", "paths",
}
PATH_LAYOUT = {
    "raw_dir": "data/raw",
    "source_manifest": "data/source_manifest.json",
    "cities_dir": "cities",
    "geography_dir": "data/raw/geography",
    "output_dir": "outputs",
}


class _UniqueKeyLoader(yaml.SafeLoader):
    """Keep SafeLoader's types and prohibit silent duplicate-key replacement."""

    def construct_mapping(self, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise yaml.constructor.ConstructorError(
                    None, None, "configuration keys must be strings", key_node.start_mark
                )
            if key in mapping:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key: {key}", key_node.start_mark
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _check_fields(value: Any, expected: set[str], field: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field}: must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field}: keys must be strings")
    missing = expected - value.keys()
    unknown = value.keys() - expected
    if missing or unknown:
        raise ValueError(
            f"{field}: missing fields {sorted(missing)}; unknown fields {sorted(unknown)}"
        )


def _overlap(first: Path, second: Path) -> bool:
    return first.is_relative_to(second) or second.is_relative_to(first)


def validate_config(
    config: Mapping[str, Any], *, code_root: Path = CODE_ROOT, data_root: Path = DATA_ROOT,
) -> dict[str, Any]:
    """Return checked parameters and paths without modifying input or filesystem.

    Roots are deployment settings, never YAML/CLI parameters. Explicit root
    arguments allow isolated synthetic tests. Input existence, source hashes,
    coordinate transformation and analysis are not checked here.
    """
    _check_fields(config, CONFIG_FIELDS, "config")
    coordinate_operation = validate_coordinate_operation(config["coordinate_operation"])
    radii = config["radii_m"]
    if not isinstance(radii, list) or not radii:
        raise ValueError("radii_m: must be a non-empty list")
    for radius in radii:
        # Integers are finite without conversion, including unusually large ones.
        if (
            type(radius) not in (int, float) or radius <= 0
            or (isinstance(radius, float) and not math.isfinite(radius))
        ):
            raise ValueError("radii_m: must contain finite positive numbers, not bools")
    if any(first >= second for first, second in zip(radii, radii[1:])):
        raise ValueError("radii_m: must be strictly increasing and unique")
    if tuple(radii) != RADII_M:
        raise ValueError("radii_m: SPEC requires exactly [500, 1000]")
    if config["classification_mode"] != "native":
        raise ValueError("classification_mode: must be 'native'")
    if type(config["include_ev_charging"]) is not bool:
        raise ValueError("include_ev_charging: must be a bool")
    for field in ("poi_batch_size", "station_batch_size"):
        if type(config[field]) is not int or config[field] <= 0:
            raise ValueError(f"{field}: must be a positive integer, not a bool")

    _check_fields(config["paths"], set(PATH_LAYOUT), "paths")
    code_root = project_path(".", root=code_root, field="code_root")
    data_root = project_path(".", root=data_root, field="data_root")
    if _overlap(code_root, data_root):
        raise ValueError("data_root: must be separate from code_root")
    paths = {
        name: project_path(value, root=data_root, field=f"paths.{name}")
        for name, value in config["paths"].items()
    }
    output = paths["output_dir"]
    if output == data_root or _overlap(output, code_root):
        raise ValueError("paths.output_dir: must not overlap either project root")
    for name, path in paths.items():
        if name != "output_dir" and _overlap(output, path):
            raise ValueError(f"paths.output_dir: overlaps input paths.{name}")

    # Keep each path in its SPEC-assigned area after resolving symlinks. Inputs
    # may select subdirectories; the manifest remains at its agreed location.
    for name, relative_area in PATH_LAYOUT.items():
        area = data_root / relative_area
        if name == "source_manifest":
            valid = paths[name] == area
        else:
            valid = paths[name].is_relative_to(area)
        if not valid:
            raise ValueError(f"paths.{name}: must stay in the SPEC location {relative_area}")
    if not paths["geography_dir"].is_relative_to(paths["raw_dir"]):
        raise ValueError("paths.geography_dir: must be inside paths.raw_dir")

    # Reserve the entire input areas, even if a config selects a narrower subset.
    for name in ("raw_dir", "source_manifest", "cities_dir", "geography_dir"):
        area = project_path(PATH_LAYOUT[name], root=data_root, field=f"paths.{name}")
        if _overlap(output, area):
            raise ValueError(f"paths.output_dir: overlaps reserved input area {name}")

    return {
        "radii_m": tuple(radii),
        "classification_mode": config["classification_mode"],
        "include_ev_charging": config["include_ev_charging"],
        "poi_batch_size": config["poi_batch_size"],
        "station_batch_size": config["station_batch_size"],
        "coordinate_operation": coordinate_operation,
        "code_root": code_root,
        "data_root": data_root,
        "paths": paths,
    }


def load_config(
    config_path: str = "config.yaml", *, code_root: Path = CODE_ROOT,
    data_root: Path = DATA_ROOT,
) -> dict[str, Any]:
    """Load a configuration relative to run.py's root, then validate it."""
    path = project_path(config_path, root=code_root, field="config_path")
    try:
        with path.open(encoding="utf-8") as stream:
            config = yaml.load(stream, Loader=_UniqueKeyLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"config_path: cannot load {path}: {exc}") from exc
    checked = validate_config(config, code_root=code_root, data_root=data_root)
    return {"config_path": path, **checked}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-config", action="store_true")
    mode.add_argument("--stage", choices=('all','inspect','prepare','match','summarize','report','verify','notebook','portability','test'))
    parser.add_argument("--config", default="config.yaml", help="path relative to run.py")
    parser.add_argument('--study-config', default='config/study.json')
    parser.add_argument('--run-id')
    parser.add_argument('--sample-stations', type=int)
    parser.add_argument('--seed', type=int)
    parser.add_argument('--primary-radius', type=int, choices=(500,1000))
    parser.add_argument('--threads', type=int)
    parser.add_argument('--memory-limit')
    parser.add_argument('--test-pattern', default='test_*.py')
    args = parser.parse_args(argv)
    if not args.check_config and args.stage != 'test' and not args.run_id:
        parser.error('--run-id is required for analysis and review')
    try:
        data_root = Path(os.environ.get('DIGIMAP_DATA_ROOT', str(DATA_ROOT))).expanduser().resolve()
        checked = load_config(args.config, code_root=CODE_ROOT, data_root=data_root)
        if args.check_config:
            report = {'status': 'config_valid', 'inputs_verified': False, 'config': checked}
        else:
            os.environ['PROJ_NETWORK'] = 'OFF'
            from src.pipeline import run_pipeline, verify_run
            from src.validation import read_json, write_json
            from src.maintenance import check_directory, notebook, portability, run_tests
            stage = args.stage or 'all'
            if args.run_id and not __import__('re').fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}', args.run_id):
                raise ValueError('invalid run-id')
            run_dir = checked['paths']['output_dir'] / (args.run_id or 'unused')
            if stage == 'verify':
                report = verify_run(run_dir, code_root=CODE_ROOT, data_root=data_root)
                out = check_directory(run_dir, 'verify')
                write_json(out / 'verification.json', report)
            elif stage in ('notebook', 'portability'):
                report = (notebook if stage == 'notebook' else portability)(run_dir, code_root=CODE_ROOT, data_root=data_root)
            elif stage == 'test':
                report = run_tests(code_root=CODE_ROOT, data_root=data_root, run_id=args.run_id, pattern=args.test_pattern)
            else:
                study_path = project_path(args.study_config, root=CODE_ROOT)
                study = read_json(study_path)
                checked['study_config_path'] = study_path
                report = run_pipeline(checked, study, run_id=args.run_id, stage=stage,
                    options={k: getattr(args,k) for k in ('sample_stations','seed','primary_radius','threads','memory_limit')})
    except (ValueError, RuntimeError, OSError, ImportError) as exc:
        print(f"Run/configuration error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, default=str, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if report.get('status') == 'failed' else 0


if __name__ == "__main__":
    raise SystemExit(main())
