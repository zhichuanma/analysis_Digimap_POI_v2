"""Step 09: explicit PUA/LAD scope and lossless source-row geography selection.

No entity merging, coverage, POI cleaning or radius matching is performed here.
"""

from collections import Counter, defaultdict
from itertools import islice
import csv
import json
import math
from pathlib import Path

from shapely import STRtree, from_wkb, get_coordinates, prepare, union_all
from shapely.geometry import Point
from shapely.validation import explain_validity
from pyproj import CRS
from pyproj.exceptions import CRSError

from src.data import (
    _artifact_fingerprint, _fingerprint, _json_write, _number, _positive_integer,
    load_station_inspection, project_path, read_lad_geometries,
    read_pua_definitions, station_rows, verify_sources,
)
from src.spatial import transform_coordinates, validate_coordinate_operation


class GeographyError(ValueError):
    """Partial artifacts are diagnostic only; consult the failed report."""

    def __init__(self, report):
        self.report = report
        super().__init__("; ".join(report["errors"]))


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _targets(values):
    if isinstance(values, (str, bytes)):
        raise ValueError("target_puas must be an explicit nonempty list")
    targets = list(values)
    if not targets or any(not _text(v) for v in targets) or len(targets) != len(set(targets)):
        raise ValueError("target_puas must contain unique nonempty names")
    return sorted(targets)


def resolve_pua_lad(definitions, lads, *, target_puas, crosswalk):
    """Resolve by source code, reviewed crosswalk, or unique exact name, in that order.

    Crosswalk entries contain lad_code, lad_name and evidence. Names are never
    stripped/fuzzed. Unresolved definitions (even outside targets) block success.
    """
    targets = _targets(target_puas)
    codes, names = {}, defaultdict(list)
    for lad in lads:
        code, name = lad["lad_code"], lad["lad_name"]
        if not _text(code) or not _text(name) or code in codes:
            raise ValueError("LAD codes/names missing or duplicate LAD code")
        codes[code] = lad
        names[name].append(code)
    for raw_name, entry in crosswalk.items():
        if (not _text(raw_name) or not all(_text(entry.get(k)) for k in ("lad_code", "lad_name", "evidence"))
                or entry["lad_code"] not in codes or codes[entry["lad_code"]]["lad_name"] != entry["lad_name"]):
            raise ValueError(f"unverified/conflicting crosswalk entry: {raw_name!r}")
    records, issues, members = [], [], defaultdict(set)
    source_pairs, order_names, name_orders, member_orders = set(), {}, {}, {}
    source_names = set()
    for source in definitions:
        pua, name = source["pua_name"], source["local_authority"]
        raw = source["raw_fields"]
        row = {**source, "lad_code": None, "lad_name": None, "lad_source_fid": None,
               "status": "unresolved", "method": None, "evidence": None}
        reasons = []
        if not _text(pua) or not _text(name):
            reasons.append("missing_mapping_key")
        else:
            source_names.add(pua)
            explicit = source.get("lad_code")
            entry = crosswalk.get(name)
            if explicit not in (None, ""):
                code, method = explicit, "source_code"
                if entry and entry["lad_code"] != code:
                    reasons.append("conflicting_code_crosswalk")
                if code in codes and codes[code]["lad_name"] != name and not entry:
                    reasons.append("unreviewed_code_name_difference")
            elif entry:
                code, method = entry["lad_code"], "reviewed_crosswalk"
            else:
                matches = names.get(name, [])
                code, method = (matches[0] if len(matches) == 1 else None), "exact_name"
                if len(matches) > 1:
                    reasons.append("ambiguous_lad_name")
            if code not in codes:
                reasons.append("unmatched_lad")
            else:
                row.update(lad_code=code, lad_name=codes[code]["lad_name"],
                           lad_source_fid=codes[code]["source_fid"], method=method,
                           evidence=entry if entry else {"basis": "unique exact source name"})
            # Optional source ordinals must not silently describe two definitions.
            order = raw.get("pua_order")
            if order is not None:
                order = str(order)
                if order_names.get(order, pua) != pua or name_orders.get(pua, order) != order:
                    reasons.append("conflicting_pua_identity")
                order_names[order], name_orders[pua] = pua, order
            member_order = raw.get("la_order_within_pua")
            if member_order is not None:
                key = pua, str(member_order)
                if member_orders.get(key, name) != name:
                    reasons.append("conflicting_member_order")
                member_orders[key] = name
        if reasons:
            for reason in reasons:
                issues.append({"pua_name": pua, "source_row": source["source_row"],
                               "reason": reason, "severity": "error", "detail": name})
        else:
            pair = pua, row["lad_code"]
            row["status"] = "duplicate" if pair in source_pairs else "matched"
            if pair in source_pairs:
                issues.append({"pua_name": pua, "source_row": source["source_row"],
                               "reason": "duplicate_relationship", "severity": "audit", "detail": row["lad_code"]})
            members[pua].add(row["lad_code"])
            source_pairs.add(pair)
            if row["lad_name"] != name:
                issues.append({"pua_name": pua, "source_row": source["source_row"],
                               "reason": "reviewed_name_difference", "severity": "audit", "detail": row["evidence"]})
            old_code = (crosswalk.get(name) or {}).get("source_lad_code")
            if old_code and old_code != row["lad_code"]:
                issues.append({"pua_name": pua, "source_row": source["source_row"],
                               "reason": "reviewed_code_change", "severity": "audit", "detail": row["evidence"]})
        records.append(row)
    for pua in sorted(set(targets) - source_names):
        issues.append({"pua_name": pua, "source_row": None, "reason": "missing_target_pua", "severity": "error", "detail": pua})
    reverse = defaultdict(list)
    for pua, values in sorted(members.items()):
        for code in sorted(values):
            reverse[code].append(pua)
    for code, puas in reverse.items():
        if len(puas) > 1:
            issues.append({"pua_name": puas, "source_row": None, "reason": "lad_in_multiple_puas", "severity": "audit", "detail": code})
    return {"records": records, "issues": issues, "target_puas": targets,
            "members": {pua: sorted(values) for pua, values in sorted(members.items())},
            "complete": not any(row["severity"] == "error" for row in issues)}


def _polygon(geometry, label):
    if (geometry is None or geometry.geom_type not in {"Polygon", "MultiPolygon"}
            or geometry.is_empty or not geometry.is_valid or geometry.has_z or geometry.has_m
            or any(not math.isfinite(v) for xy in get_coordinates(geometry) for v in xy)):
        detail = explain_validity(geometry) if geometry is not None else "missing"
        raise ValueError(f"{label}: requires nonempty valid finite 2D polygon ({detail})")
    return geometry


def build_pua_scope(mapping, lads, *, crs):
    """Union complete whole-LAD members, preserving holes and multipart topology."""
    try:
        parsed = CRS.from_user_input(crs)
    except CRSError as error:
        raise ValueError("scope CRS is missing or invalid") from error
    if not parsed.equals(CRS.from_epsg(27700)) or any(a.unit_conversion_factor != 1 for a in parsed.axis_info):
        raise ValueError("scope CRS must be verified EPSG:27700 with metre units")
    if mapping.get("complete") is not True:
        raise ValueError("PUA mapping is incomplete/conflicting")
    targets = _targets(mapping["target_puas"])
    by_code = {}
    for row in lads:
        code = row["lad_code"]
        if code in by_code:
            raise ValueError("duplicate LAD code")
        by_code[code] = _polygon(row["geometry"], code)
    geometries = {}
    for pua, members in mapping["members"].items():
        if not members or set(members) - set(by_code):
            raise ValueError(f"{pua}: missing LAD members")
        geometries[pua] = _polygon(union_all([by_code[c] for c in sorted(set(members))]), pua)
    if set(targets) - set(geometries):
        raise ValueError("missing target PUA geometry")
    study = _polygon(union_all([geometries[p] for p in targets]), "target union")
    return {"target_puas": targets, "members": mapping["members"], "geometries": geometries,
            "lad_geometries": by_code, "study_area": study, "crs": "EPSG:27700", "predicate": "covers"}


def select_station_rows(rows, scope):
    """Yield each projected source row once; membership ambiguity is not unknown."""
    lad_keys, pua_keys = sorted(scope["lad_geometries"]), sorted(scope["geometries"])
    lad_tree = STRtree([scope["lad_geometries"][key] for key in lad_keys])
    pua_tree = STRtree([scope["geometries"][key] for key in pua_keys])
    prepare(scope["study_area"])
    seen = set()
    for source in rows:
        number = source["source_record_no"]
        if type(number) is not int or number <= 0 or number in seen:
            raise ValueError("source_record_no must be unique positive logical record numbers")
        seen.add(number)
        row = {**source, "lad_codes": [], "pua_names": [], "target_pua_names": [],
               "in_target_pua": None, "audit_reasons": list(source.get("coordinate_reasons", []))}
        xy = source.get("xy")
        if xy is None or len(xy) != 2 or any(_number(v) is None for v in xy):
            if not row["audit_reasons"]:
                row["audit_reasons"].append("invalid_projected_coordinates")
        elif row["audit_reasons"]:
            pass  # Malformed rows cannot gain geography eligibility through partial fields.
        else:
            point = Point(xy)
            row["lad_codes"] = sorted(lad_keys[i] for i in lad_tree.query(point, predicate="covered_by"))
            row["pua_names"] = sorted(pua_keys[i] for i in pua_tree.query(point, predicate="covered_by"))
            row["target_pua_names"] = sorted(set(row["pua_names"]) & set(scope["target_puas"]))
            inside = bool(scope["study_area"].covers(point))
            # Independent member predicates guard union/index consistency for each point.
            expected = sorted(p for p, members in scope["members"].items() if set(members) & set(row["lad_codes"]))
            if row["pua_names"] != expected or inside != bool(row["target_pua_names"]):
                raise ValueError(f"union/member membership disagreement at source row {number}")
            row["in_target_pua"] = inside
            row["audit_reasons"].append("inside_target_pua" if inside else "outside_target_pua")
            if len(row["lad_codes"]) > 1:
                row["audit_reasons"].append("multiple_lad_memberships")
            if len(row["pua_names"]) > 1:
                row["audit_reasons"].append("multiple_pua_memberships")
            if inside and not scope["study_area"].contains(point):
                row["audit_reasons"].append("on_target_boundary")
        yield row


def _project_rows(rows, operation, batch_size):
    operation = validate_coordinate_operation(operation)
    # Fail infrastructure/configuration before handling row-specific failures.
    transform_coordinates([-1.0], [52.0], operation)
    iterator = iter(rows)
    while batch := list(islice(iterator, batch_size)):
        valid = []
        for row in batch:
            row["xy"] = None
            raw_lon, raw_lat = row["raw_longitude"], row["raw_latitude"]
            lon, lat = _number(raw_lon), _number(raw_lat)
            if raw_lon in (None, "") or raw_lat in (None, ""):
                row["coordinate_reasons"].append("missing_station_coordinates")
            elif lon is None or lat is None:
                row["coordinate_reasons"].append("invalid_station_coordinates")
            elif not (-180 <= lon <= 180 and -90 <= lat <= 90):
                row["coordinate_reasons"].append("station_coordinates_out_of_range")
            elif not row["coordinate_reasons"]:
                valid.append((row, lon, lat))
        if valid:
            try:
                x, y = transform_coordinates([v[1] for v in valid], [v[2] for v in valid], operation)
                for (row, _, _), px, py in zip(valid, x, y, strict=True):
                    row["xy"] = (px, py)
            except ValueError as error:
                if not str(error).startswith("coordinate transformation failed:"):
                    raise
                transform_coordinates([-1.0], [52.0], operation)
                for row, lon, lat in valid:
                    try:
                        x, y = transform_coordinates([lon], [lat], operation)
                        row["xy"] = (x[0], y[0])
                    except ValueError as single_error:
                        if not str(single_error).startswith("coordinate transformation failed:"):
                            raise
                        row["coordinate_reasons"].append("coordinate_transformation_failed")
                        row["transformation_error"] = str(single_error)
        yield from batch


def _cell(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) if isinstance(value, (dict, list, tuple)) else value


def _write_table(path, records, fields):
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in records:
            writer.writerow({key: _cell(row.get(key)) for key in fields})


def prepare_pua_scope(mapping_path, lad_path, station_path, *, data_root, output_dir,
                      target_puas, mapping_sheet, lad_layer, code_field, name_field,
                      crosswalk, version_review, inspection_dir, coordinate_operation,
                      required_paths, comparison_csv=None, station_batch_size=1000):
    """Run step 09 in a new output directory; manifest and upstream audit are mandatory.

    version_review documents the reviewed whole-LAD interpretation and source /
    boundary versions. Crosswalk is an explicit reviewed input, saved in full.
    """
    _positive_integer(station_batch_size, "station_batch_size")
    root = project_path(".", root=data_root)
    output = project_path(output_dir, root=root)
    if (not Path(output_dir).is_relative_to("outputs") or output == root / "outputs"
            or not output.is_relative_to(root / "outputs")):
        raise ValueError("output_dir must be a new directory strictly inside outputs/")
    output.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": 1, "status": "running", "errors": [], "output_dir": str(output),
              "mapping_complete": False, "scope_complete": False, "scan_complete": False,
              "validation_complete": False, "station_entities_merged": False,
              "final_query_or_coverage_evaluated": False}
    _json_write(output / "pua_scope_report.json", report)
    try:
        targets = _targets(target_puas)
        operation = validate_coordinate_operation(coordinate_operation)
        paths = list(required_paths)
        used = {mapping_path, lad_path, station_path} | ({comparison_csv} if comparison_csv else set())
        if not used <= set(paths):
            raise ValueError("required_paths must include all geography/definition/station inputs")
        if (version_review.get("whole_lad_members") is not True
                or not all(_text(version_review.get(k)) for k in ("definition_version", "boundary_version", "evidence"))):
            raise ValueError("explicit reviewed whole-LAD definition and boundary versions required")
        report["sources_before"] = verify_sources(data_root=root, required_paths=paths)
        before = {p: _fingerprint(p, root, 1048576) for p in paths}
        report["input_fingerprints_before"] = before
        prior = load_station_inspection(inspection_dir, data_root=root, station_path=station_path)
        report["prior_station_audit"] = {k: v for k, v in prior.items() if k != "issues"}
        definitions = read_pua_definitions(mapping_path, data_root=root, sheet=mapping_sheet)
        if comparison_csv:
            comparison = read_pua_definitions(comparison_csv, data_root=root)
            canonical = lambda rows: Counter(json.dumps({k: "" if v is None else str(v) for k, v in r["raw_fields"].items()}, sort_keys=True) for r in rows)
            if canonical(definitions) != canonical(comparison):
                raise ValueError("PUA workbook and comparison CSV disagree")
            report["definition_csv_matches"] = True
        lads, metadata = read_lad_geometries(lad_path, data_root=root, layer=lad_layer,
                                            code_field=code_field, name_field=name_field)
        report["lad_metadata"] = metadata
        definition = {"target_puas": targets, "mapping_path": mapping_path, "mapping_sheet": mapping_sheet,
                      "mapping_fields": ["pua_name", "local_authority", "lad_code (optional)"],
                      "lad_path": lad_path, "lad_layer": lad_layer, "lad_code_field": code_field,
                      "lad_name_field": name_field, "version_review": version_review,
                      "crosswalk": crosswalk, "predicate": "covers", "crs": "EPSG:27700",
                      "coordinate_operation": operation, "required_paths": paths,
                      "input_fingerprints": before, "station_batch_size": station_batch_size,
                      "multiple_memberships": "retain all; one candidate per source row"}
        _json_write(output / "scope_definition.json", definition)
        mapping = resolve_pua_lad(definitions, lads, target_puas=targets, crosswalk=crosswalk)
        _write_table(output / "pua_lad_mapping.csv", mapping["records"],
                     ["source_path", "source_sheet", "source_row", "raw_fields", "pua_name", "local_authority",
                      "lad_code", "lad_name", "lad_source_fid", "status", "method", "evidence"])
        _write_table(output / "pua_lad_issues.csv", mapping["issues"],
                     ["pua_name", "source_row", "reason", "severity", "detail"])
        report.update(mapping_complete=mapping["complete"], mapping_rows=len(definitions),
                      mapping_issue_counts=dict(Counter(r["reason"] for r in mapping["issues"])),
                      target_puas=targets, pua_count=len(mapping["members"]),
                      member_lad_count=len({c for cs in mapping["members"].values() for c in cs}))
        scope = build_pua_scope(mapping, lads, crs=metadata["crs"]["definition"])
        _write_table(output / "pua_geometries.csv",
                     ({"pua_name": p, "lad_codes": scope["members"][p], "is_target": p in targets,
                       "crs": scope["crs"], "geometry_wkb_hex": g.wkb_hex} for p, g in scope["geometries"].items()),
                     ["pua_name", "lad_codes", "is_target", "crs", "geometry_wkb_hex"])
        with (output / "study_area.wkb").open("xb") as stream:
            stream.write(scope["study_area"].wkb)
        report["scope_complete"] = True
        fields = ["source_record_no", "raw_id", "raw_longitude", "raw_latitude", "raw_fields", "raw_values",
                  "xy", "lad_codes", "pua_names", "target_pua_names", "in_target_pua", "audit_reasons",
                  "prior_station_issues", "transformation_error"]
        counts, pua_counts, unique_counts, reasons = Counter(), Counter(), Counter(), Counter()
        expected_keys, candidate_keys, scanned = [], [], 0
        with (output / "station_pua_audit.csv").open("x", encoding="utf-8", newline="") as full, \
                (output / "stations_in_target_pua.csv").open("x", encoding="utf-8", newline="") as selected:
            writers = [csv.DictWriter(s, fieldnames=fields) for s in (full, selected)]
            for writer in writers:
                writer.writeheader()
            projected = _project_rows(station_rows(station_path, data_root=root), operation, station_batch_size)
            for row in select_station_rows(projected, scope):
                scanned += 1
                row["prior_station_issues"] = prior["issues"].get(row["source_record_no"], [])
                counts[{True: "inside", False: "outside", None: "unknown"}[row["in_target_pua"]]] += 1
                reasons.update(row["audit_reasons"])
                pua_counts.update(row["pua_names"])
                if len(row["pua_names"]) == 1:
                    unique_counts.update(row["pua_names"])
                exported = {key: _cell(row.get(key)) for key in fields}
                writers[0].writerow(exported)
                if row["in_target_pua"] is True:
                    expected_keys.append(row["source_record_no"])
                    writers[1].writerow(exported)
        # Read-back checks validate written artifacts, not just in-memory counts.
        with (output / "station_pua_audit.csv").open(encoding="utf-8", newline="") as stream:
            audit = list(csv.DictReader(stream, strict=True))
        with (output / "stations_in_target_pua.csv").open(encoding="utf-8", newline="") as stream:
            candidate = list(csv.DictReader(stream, strict=True))
        candidate_keys = [int(r["source_record_no"]) for r in candidate]
        logical_count = sum(1 for _ in station_rows(station_path, data_root=root))
        if (logical_count != scanned or scanned != prior["source_count"] or scanned != sum(counts.values())
                or [int(r["source_record_no"]) for r in audit] != list(range(1, scanned + 1))
                or candidate_keys != expected_keys
                or candidate != [r for r in audit if r["in_target_pua"] == "True"]):
            raise ValueError("station source/audit/candidate counts or rowsets disagree")
        if not from_wkb((output / "study_area.wkb").read_bytes()).equals(scope["study_area"]):
            raise ValueError("study geometry read-back differs")
        field_limit = csv.field_size_limit()
        try:
            # Real multipart WKB can exceed CSV's 128 KiB default. Bound the
            # temporary limit by the actual geometries and restore caller state.
            csv.field_size_limit(max(field_limit, max(len(g.wkb_hex) for g in scope["geometries"].values())))
            with (output / "pua_geometries.csv").open(encoding="utf-8", newline="") as stream:
                for row in csv.DictReader(stream):
                    if row["crs"] != scope["crs"] or not from_wkb(row["geometry_wkb_hex"]).equals(scope["geometries"][row["pua_name"]]):
                        raise ValueError("PUA geometry read-back differs")
        finally:
            csv.field_size_limit(field_limit)
        after = {p: _fingerprint(p, root, 1048576) for p in paths}
        if after != before or load_station_inspection(inspection_dir, data_root=root, station_path=station_path) != prior:
            raise ValueError("source or upstream audit changed during geography selection")
        report.update(input_fingerprints_after=after, input_bytes_unchanged=True,
                      source_record_count=scanned, station_counts={k: counts[k] for k in ("inside", "outside", "unknown")},
                      station_reason_counts=dict(reasons), candidate_count=len(candidate),
                      pua_station_counts={p: {"source_rows": pua_counts[p], "unique_pua_rows": unique_counts[p]} for p in scope["geometries"]},
                      zero_station_target_puas=[p for p in targets if not pua_counts[p]],
                      status="completed", scan_complete=True, validation_complete=True)
        report["artifacts"] = {p.name: _artifact_fingerprint(p) for p in sorted(output.iterdir()) if p.name != "pua_scope_report.json"}
    except Exception as error:
        report.update(status="failed", scope_complete=False, scan_complete=False, validation_complete=False)
        report["errors"].append(f"{type(error).__name__}: {error}")
        _json_write(output / "pua_scope_report.json", report)
        raise GeographyError(report) from error
    _json_write(output / "pua_scope_report.json", report)
    return report
