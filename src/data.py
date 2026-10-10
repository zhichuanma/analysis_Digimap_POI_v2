"""Project paths and read-only byte-level verification of source copies."""

from contextlib import contextmanager
from contextlib import ExitStack
from collections import Counter
import csv
from datetime import datetime
import hashlib
import io
from itertools import islice
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import sqlite3
import struct


def _resolve(path: Path) -> Path:
    """Resolve existing links, allowing new targets but rejecting link loops."""
    try:
        return path.resolve(strict=True)
    except FileNotFoundError:
        return path.resolve(strict=False)


def project_path(relative_path: str, *, root: Path, field: str = "path") -> Path:
    """Resolve a POSIX relative path inside an explicitly trusted absolute root.

    Check the original text before normalization. Neither the current working
    directory nor expansion of '~' or environment variables participates.
    Missing targets are allowed; existing parent symlinks still constrain them.
    No directories or files are created.
    """
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError(f"{field}: must be a non-empty relative path string")
    if "\x00" in relative_path or "\\" in relative_path:
        raise ValueError(f"{field}: must use POSIX path syntax without NUL bytes")
    path = Path(relative_path)
    if path.is_absolute() or PureWindowsPath(relative_path).drive:
        raise ValueError(f"{field}: absolute or drive-qualified paths are forbidden")
    if ".." in path.parts:
        raise ValueError(f"{field}: '..' path components are forbidden")

    root = Path(root)
    if not root.is_absolute():
        raise ValueError(f"{field}: root must be an absolute trusted path")
    try:
        resolved_root = _resolve(root)
        resolved = _resolve(resolved_root / path)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{field}: cannot resolve path: {exc}") from exc
    if not resolved.is_relative_to(resolved_root):
        raise ValueError(f"{field}: resolved path escapes root {resolved_root}")
    return resolved


class SourceError(ValueError):
    """An input could not be read under the source-file contract."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class SourceVerificationError(ValueError):
    """A failed verification; ``report`` is JSON-serializable evidence."""

    def __init__(self, report):
        self.report = report
        errors = report["errors"] + [
            error for row in report["files"] for error in row["errors"]
        ]
        super().__init__("Source verification failed: " + "; ".join(
            error["message"] for error in errors
        ))


def _source_path(relative_path, data_root):
    try:
        root = project_path(".", root=data_root, field="data_root")
        resolved = project_path(relative_path, root=root, field="source_path")
    except (TypeError, ValueError) as exc:
        raise SourceError("invalid_source_path", str(exc)) from exc
    areas = (root / "data/raw", root / "cities")
    lexical = root / relative_path
    if not all(any(path.is_relative_to(area) for area in areas) for path in (lexical, resolved)):
        raise SourceError(
            "invalid_source_path", f"{relative_path}: inputs must stay in data/raw/ or cities/"
        )
    return resolved


@contextmanager
def _open_regular(path):
    # O_NONBLOCK prevents FIFO input from hanging before the regular-file check.
    # The final component was resolved already; do not follow a replacement link.
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError as exc:
        raise SourceError("missing_file", f"{path}: file is missing") from exc
    except OSError as exc:
        raise SourceError("unreadable_file", f"{path}: cannot open read-only: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SourceError("not_regular_file", f"{path}: must be a regular file")
        stream = os.fdopen(descriptor, "rb")
        descriptor = None
        with stream:
            yield stream
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextmanager
def open_source(relative_path, *, data_root):
    """Yield a read-only binary stream from data/raw/ or cities/ in data_root.

    No write mode, permission change, directory creation or provenance-source
    access is provided. A source's content is not verified merely by opening it.
    """
    path = _source_path(relative_path, data_root)
    with _open_regular(path) as stream:
        yield stream


def _file_state(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _fingerprint(relative_path, data_root, chunk_size):
    if type(chunk_size) is not int or chunk_size <= 0:
        raise ValueError("chunk_size: must be a positive integer, not a bool")
    path = _source_path(relative_path, data_root)
    digest = hashlib.sha256()
    size = 0
    try:
        with open_source(relative_path, data_root=data_root) as stream:
            before = os.fstat(stream.fileno())
            while block := stream.read(chunk_size):
                size += len(block)
                digest.update(block)
            after = os.fstat(stream.fileno())
            if _file_state(before) != _file_state(after) or _file_state(after) != _file_state(path.stat()):
                raise SourceError("changed_during_read", f"{relative_path}: changed during reading")
            if size != after.st_size:
                raise SourceError("changed_during_read", f"{relative_path}: inconsistent byte count")
    except OSError as exc:
        raise SourceError("unreadable_file", f"{relative_path}: cannot finish reading: {exc}") from exc
    return {"resolved_path": str(path), "size_bytes": size, "sha256": digest.hexdigest()}


def sha256(relative_path, *, data_root, chunk_size=1048576):
    """Hash all original bytes in bounded chunks; return a lowercase digest."""
    return _fingerprint(relative_path, data_root, chunk_size)["sha256"]


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError(f"non-standard JSON constant: {value}")


def _required_paths(values, data_root):
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError("required_paths: must be an independent non-empty list or tuple")
    normalized = []
    resolved = set()
    for value in values:
        path = _source_path(value, data_root)
        name = Path(value).as_posix()
        if name in normalized or path in resolved:
            raise ValueError(f"required_paths: duplicate path or alias: {value}")
        normalized.append(name)
        resolved.add(path)
    return normalized


def _manifest_entries(document, data_root):
    if not isinstance(document, dict) or set(document) != {"schema_version", "files"}:
        raise ValueError("manifest: expected schema_version and files")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("manifest.schema_version: must be integer 1")
    if not isinstance(document["files"], list):
        raise ValueError("manifest.files: must be a list")
    fields = {
        "path", "size_bytes", "sha256", "source_location", "copied_at",
        "source_sha256_before_copy", "source_sha256_after_copy",
    }
    entries = []
    names, resolved = set(), set()
    for index, entry in enumerate(document["files"]):
        label = f"manifest.files[{index}]"
        if not isinstance(entry, dict) or not fields.issubset(entry):
            raise ValueError(f"{label}: missing required source/copy evidence fields")
        path = _source_path(entry["path"], data_root)
        name = Path(entry["path"]).as_posix()
        if name in names or path in resolved:
            raise ValueError(f"{label}.path: duplicate path or alias: {entry['path']}")
        names.add(name)
        resolved.add(path)
        if type(entry["size_bytes"]) is not int or entry["size_bytes"] < 0:
            raise ValueError(f"{label}.size_bytes: must be a non-negative integer, not a bool")
        for field in ("sha256", "source_sha256_before_copy", "source_sha256_after_copy"):
            if not isinstance(entry[field], str) or not re.fullmatch(r"[0-9a-fA-F]{64}", entry[field]):
                raise ValueError(f"{label}.{field}: must contain exactly 64 hexadecimal digits")
        if not isinstance(entry["source_location"], str) or not entry["source_location"].strip():
            raise ValueError(f"{label}.source_location: must be non-empty provenance text")
        try:
            copied_at = datetime.fromisoformat(entry["copied_at"])
            if copied_at.tzinfo is None:
                raise ValueError("missing timezone")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}.copied_at: must be an ISO 8601 timestamp with timezone") from exc
        entries.append({**entry, "path": name, "resolved_path": str(path)})
    return entries


def _issue(code, message, path=None):
    return {"code": code, "path": path, "message": message}


def verify_sources(manifest_path="data/source_manifest.json", *, data_root, required_paths):
    """Verify all declared copies against a separately supplied required list.

    Return a JSON-serializable report, or raise SourceVerificationError carrying
    the failed report. Only a new-project manifest and new-project copies are
    read. Historical source hashes are compared, never recreated or repaired.
    The caller, not this function, may persist the report in outputs/<run-id>/.
    """
    report = {
        "status": "failed", "manifest_path": None, "data_root": None,
        "required_paths": [], "declared_count": 0, "checked_count": 0,
        "passed_count": 0, "files": [], "errors": [],
    }
    try:
        root = project_path(".", root=data_root, field="data_root")
        report["data_root"] = str(root)
        path = project_path(manifest_path, root=root, field="manifest_path")
        if Path(manifest_path) != Path("data/source_manifest.json") or path != root / "data/source_manifest.json":
            raise ValueError("manifest_path: must stay at data/source_manifest.json")
        report["manifest_path"] = str(path)
    except (TypeError, ValueError) as exc:
        report["errors"].append(_issue("invalid_manifest_path", str(exc)))
        raise SourceVerificationError(report) from exc

    # Check these independent prerequisites separately so a missing required
    # list does not conceal a missing manifest, or vice versa.
    entries = []
    try:
        with _open_regular(path) as stream:
            document = json.load(
                stream, object_pairs_hook=_unique_json_object, parse_constant=_reject_json_constant
            )
        if isinstance(document, dict) and isinstance(document.get("files"), list):
            report["declared_count"] = len(document["files"])
        entries = _manifest_entries(document, root)
    except SourceError as exc:
        code = "missing_manifest" if exc.code == "missing_file" else exc.code
        report["errors"].append(_issue(code, str(exc), "data/source_manifest.json"))
    except (OSError, ValueError) as exc:
        report["errors"].append(_issue("invalid_manifest", str(exc), "data/source_manifest.json"))
    try:
        report["required_paths"] = _required_paths(required_paths, root)
    except (TypeError, ValueError) as exc:
        report["errors"].append(_issue("invalid_required_paths", str(exc)))
    if report["errors"]:
        raise SourceVerificationError(report)

    required = set(report["required_paths"])
    declared = {entry["path"] for entry in entries}
    for missing in sorted(required - declared):
        report["errors"].append(_issue("missing_declaration", f"{missing}: required but not declared", missing))

    for entry in entries:
        name = entry["path"]
        row = {
            "path": name, "resolved_path": entry["resolved_path"], "required": name in required,
            "expected_size_bytes": entry["size_bytes"], "actual_size_bytes": None,
            "expected_sha256": entry["sha256"].lower(), "actual_sha256": None,
            "source_location": entry["source_location"], "copied_at": entry["copied_at"],
            "source_sha256_before_copy": entry["source_sha256_before_copy"].lower(),
            "source_sha256_after_copy": entry["source_sha256_after_copy"].lower(),
            "status": "failed", "errors": [],
        }
        for field in ("source_sha256_before_copy", "source_sha256_after_copy"):
            if row[field] != row["expected_sha256"]:
                row["errors"].append(_issue(
                    "copy_evidence_mismatch", f"{name}: {field} differs from declared copy sha256", name
                ))
        try:
            actual = _fingerprint(name, root, 1048576)
            report["checked_count"] += 1
            row.update(resolved_path=actual["resolved_path"], actual_size_bytes=actual["size_bytes"], actual_sha256=actual["sha256"])
            if row["actual_size_bytes"] != row["expected_size_bytes"]:
                row["errors"].append(_issue("size_mismatch", f"{name}: byte count differs from manifest", name))
            if row["actual_sha256"] != row["expected_sha256"]:
                row["errors"].append(_issue("sha256_mismatch", f"{name}: SHA-256 differs from manifest", name))
        except SourceError as exc:
            row["errors"].append(_issue(exc.code, str(exc), name))
        if not row["errors"]:
            row["status"] = "passed"
            report["passed_count"] += 1
        report["files"].append(row)

    if report["errors"] or report["passed_count"] != report["declared_count"]:
        raise SourceVerificationError(report)
    report["status"] = "passed"
    return report


# Step 06 audits source records. None of these functions selects or cleans POIs.
POI_FIELDS = {
    "ref_no": "ref_no", "raw_code": "pointx_class",
    "easting": "feature_easting", "northing": "feature_northing",
    "accuracy": "pos_accuracy",
}
STATION_FIELDS = {"id": "StationID", "latitude": "Latitude", "longitude": "Longitude"}


class InputInspectionError(ValueError):
    """A failed/incomplete inspection; partial artifacts are never complete lists."""

    def __init__(self, report):
        self.report = report
        super().__init__("; ".join(report["errors"]))


def _positive_integer(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name}: must be a positive integer, not a bool")


def _quoted(name):
    return '"' + name.replace('"', '""') + '"'


def _field_mapping(value, default):
    fields = dict(default) if value is None else value
    if not isinstance(fields, dict) or fields.keys() != default.keys():
        raise ValueError(f"field mapping must specify {sorted(default)}")
    if any(not isinstance(v, str) or not v or '\x00' in v for v in fields.values()):
        raise ValueError("field mapping values must be non-empty column names")
    if len(set(fields.values())) != len(fields):
        raise ValueError("field mapping must use distinct columns")
    return dict(fields)


@contextmanager
def _gpkg_connection(relative_path, data_root):
    path = _source_path(relative_path, data_root)
    # A source snapshot must not depend on unmanifested SQLite sidecar files.
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal", "-shm")):
        raise ValueError("GPKG has SQLite sidecars; a standalone stable snapshot is required")
    with _open_regular(path) as stream:
        before = _file_state(os.fstat(stream.fileno()))
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("BEGIN")
            yield connection
        finally:
            connection.close()
            if before != _file_state(path.stat()) or before != _file_state(os.fstat(stream.fileno())):
                raise ValueError("GPKG changed during reading")


def _gpkg_schema(connection, layer, fields):
    from pyproj import CRS

    contents = [dict(row) for row in connection.execute("SELECT * FROM gpkg_contents")]
    layers = [row["table_name"] for row in contents if row["data_type"] == "features"]
    if layer is None:
        if len(layers) != 1:
            raise ValueError(f"POI layer is ambiguous; available feature layers: {layers}")
        layer = layers[0]
    if not isinstance(layer, str) or layer not in layers:
        raise ValueError(f"POI layer {layer!r} is not a registered feature layer: {layers}")
    columns = [dict(row) for row in connection.execute(f"PRAGMA table_info({_quoted(layer)})")]
    names = {column["name"] for column in columns}
    missing = set(fields.values()) - names
    if missing:
        raise ValueError(f"POI missing required fields: {sorted(missing)}")
    keys = [column for column in columns if column["pk"]]
    if len(keys) != 1 or keys[0]["type"].upper() != "INTEGER":
        raise ValueError("POI requires one INTEGER feature primary key")
    if "source_fid" in names and keys[0]["name"] != "source_fid":
        raise ValueError("POI source_fid column conflicts with the feature primary key")
    geometry_rows = list(connection.execute("SELECT * FROM gpkg_geometry_columns WHERE table_name=?", (layer,)))
    if len(geometry_rows) != 1 or geometry_rows[0]["column_name"] not in names:
        raise ValueError("POI requires exactly one declared geometry column")
    geometry = dict(geometry_rows[0])
    if geometry["column_name"] in fields.values() or keys[0]["name"] in fields.values():
        raise ValueError("POI attributes cannot alias geometry or feature primary key")
    selected = next(row for row in contents if row["table_name"] == layer)
    if geometry["srs_id"] != selected["srs_id"]:
        raise ValueError("GPKG content and geometry CRS IDs disagree")
    srs_rows = list(connection.execute("SELECT * FROM gpkg_spatial_ref_sys WHERE srs_id=?", (geometry["srs_id"],)))
    if len(srs_rows) != 1:
        raise ValueError("POI CRS definition is missing or ambiguous")
    srs = dict(srs_rows[0])
    crs = CRS.from_wkt(srs["definition"])
    if not crs.equals(CRS.from_epsg(27700)) or not crs.is_projected or any(axis.unit_conversion_factor != 1 for axis in crs.axis_info):
        raise ValueError("POI CRS must be equivalent to EPSG:27700 with metre units")
    count = connection.execute(f"SELECT COUNT(*) FROM {_quoted(layer)}").fetchone()[0]
    cached_count = None
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gpkg_ogr_contents'").fetchone():
        cached = connection.execute("SELECT feature_count FROM gpkg_ogr_contents WHERE table_name=?", (layer,)).fetchone()
        cached_count = cached[0] if cached else None
    return {
        "layers": contents, "layer": layer, "fields": columns, "field_mapping": fields,
        "fid_column": keys[0]["name"], "geometry": geometry, "crs": srs,
        "crs_epsg": crs.to_epsg(), "coordinate_units": "metre",
        "source_count": count, "cached_feature_count": cached_count,
        "cached_count_matches": cached_count == count if cached_count is not None else None,
        "extent_is_coverage_boundary": False,
    }


def poi_batches(relative_path, *, data_root, layer=None, batch_size=10000, fields=None, limit=None):
    """Yield raw GPKG dictionaries in source-FID order, without dropping rows.

    source_fid is the real feature primary key. Geometry remains a GPKG blob;
    malformed geometry must not prevent auditing that row's ID and attributes.
    limit is explicitly a local sample, never a global duplicate audit.
    """
    _positive_integer(batch_size, "batch_size")
    if limit is not None:
        _positive_integer(limit, "limit")
    mapping = _field_mapping(fields, POI_FIELDS)
    with _gpkg_connection(relative_path, data_root) as connection:
        schema = _gpkg_schema(connection, layer, mapping)
        statement = f"SELECT * FROM {_quoted(schema['layer'])} ORDER BY {_quoted(schema['fid_column'])}"
        cursor = connection.execute(statement + (" LIMIT ?" if limit is not None else ""), (limit,) if limit is not None else ())
        while rows := cursor.fetchmany(batch_size):
            batch = []
            for row in rows:
                record = dict(row)
                fid = record[schema["fid_column"]]
                if type(fid) is not int:
                    raise ValueError("feature primary key is not an integer")
                record["source_fid"] = fid
                batch.append(record)
            yield batch


def _raw(value):
    """Lossless typed scalar representation for SQL keys and CSV/JSON output."""
    if value is None:
        return "null", ""
    if isinstance(value, str):
        return "text", value
    if isinstance(value, bytes):
        return "blob", value.hex()
    if type(value) is int:
        return "integer", str(value)
    if type(value) is float:
        return "real", repr(value)
    raise ValueError(f"unsupported raw scalar type: {type(value).__name__}")


def _raw_key(value):
    return json.dumps(_raw(value), ensure_ascii=False, separators=(",", ":"))


def _missing_kind(value):
    if value is None:
        return "null"
    if isinstance(value, str):
        if value == "":
            return "empty_string"
        if not value.strip():
            return "whitespace"
    if isinstance(value, float) and not math.isfinite(value):
        return "nonfinite"
    return "present"


def _classification_parts(value):
    # Same format semantics as classify(); dictionary membership is NOT checked.
    if value is None or (isinstance(value, float) and math.isnan(value)) or value == "":
        return "missing", "UNKNOWN", "UNKNOWN", "UNKNOWN"
    code = str(value)
    if re.fullmatch(r"[0-9]{8}", code) is None:
        return "malformed", "UNKNOWN", "UNKNOWN", "UNKNOWN"
    return "valid", code[:2], code[:4], code


def _number(value):
    if value is None or isinstance(value, (bytes, bool)) or (isinstance(value, str) and not value.strip()):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _geometry_check(blob, srs_id):
    from shapely import from_wkb
    from shapely.errors import GEOSException

    result = {"status": "null", "type": None, "xy": None, "detail": "", "has_z": False, "has_m": False}
    if blob is None:
        return result
    try:
        if not isinstance(blob, bytes) or len(blob) < 8 or blob[:3] != b"GP\x00":
            raise ValueError("not a version-0 GeoPackage geometry")
        flags = blob[3]
        envelope = (flags >> 1) & 7
        if flags & 0xE0 or envelope not in (0, 1, 2, 3, 4):
            raise ValueError("unsupported GeoPackage flags/envelope")
        header_srs = struct.unpack(("<" if flags & 1 else ">") + "i", blob[4:8])[0]
        if header_srs != srs_id:
            raise ValueError("geometry header CRS differs from layer CRS")
        offset = 8 + (0, 32, 48, 48, 64)[envelope]
        if len(blob) <= offset:
            raise ValueError("truncated GeoPackage geometry")
        geometry = from_wkb(blob[offset:])
        result.update(type=geometry.geom_type, has_z=bool(geometry.has_z), has_m=bool(geometry.has_m))
        if bool(flags & 0x10) != bool(geometry.is_empty):
            raise ValueError("GeoPackage empty flag disagrees with WKB")
        if geometry.is_empty:
            result["status"] = "empty"
        elif geometry.geom_type == "Point" and not all(math.isfinite(v) for v in (geometry.x, geometry.y)):
            result["status"] = "nonfinite"
        elif not geometry.is_valid:
            result["status"] = "invalid"
        elif geometry.geom_type != "Point":
            result["status"] = "non_point"
        else:
            result.update(status="valid_point", xy=(geometry.x, geometry.y))
    except (ValueError, TypeError, GEOSException, struct.error) as error:
        result.update(status="decode_error", detail=str(error), xy=None)
    return result


def _json_write(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _csv_writer(stack, path, header):
    stream = stack.enter_context(path.open("x", encoding="utf-8", newline=""))
    writer = csv.writer(stream)
    writer.writerow(header)
    return writer


def _audit_database(path):
    db = sqlite3.connect(path)
    db.execute("PRAGMA cache_size=-8192")
    db.executescript('''
        CREATE TABLE ref_rows (source_fid INTEGER PRIMARY KEY, ref_key TEXT);
        CREATE INDEX ref_key_index ON ref_rows(ref_key);
        CREATE TABLE frequencies (kind TEXT, raw_key TEXT, raw_type TEXT, raw_value TEXT,
            format_status TEXT, group_code TEXT, category_code TEXT, class_code TEXT,
            n INTEGER NOT NULL, PRIMARY KEY(kind, raw_key));
        CREATE TABLE station_rows (record_no INTEGER PRIMARY KEY, station_id TEXT, xy_key TEXT);
        CREATE INDEX station_id_index ON station_rows(station_id);
        CREATE INDEX station_xy_index ON station_rows(xy_key);
    ''')
    return db


def _inspect_pois(path, root, output, db, report, fields, batch_size, limit, known_accuracy, progress):
    info = report["poi"]
    missing = {name: Counter() for name in fields.values()}
    geometry_status, geometry_types, issues = Counter(), Counter(), Counter()
    compared, unequal, mean, maximum, maximum_fid = 0, 0, 0.0, None, None
    with ExitStack() as stack:
        issue_writer = _csv_writer(stack, output / "poi_quality_issues.csv", ["source_fid", "reason", "detail"])
        difference_writer = _csv_writer(stack, output / "coordinate_differences.csv", ["source_fid", "x_geometry", "y_geometry", "feature_easting", "feature_northing", "dx", "dy", "delta_m"])
        samples = stack.enter_context((output / "poi_sample.jsonl").open("x", encoding="utf-8"))
        for batch in poi_batches(path, data_root=root, layer=info["layer"], fields=fields, batch_size=batch_size, limit=limit):
            refs, frequency = [], Counter()
            frequency_values = {}
            for row in batch:
                fid = row["source_fid"]
                for name in fields.values():
                    missing[name][_missing_kind(row[name])] += 1
                ref = row[fields["ref_no"]]
                kind = _missing_kind(ref)
                absent = kind in ("null", "empty_string", "nonfinite")
                refs.append((fid, None if absent else _raw_key(ref)))
                reasons = []
                if absent:
                    reasons.append(("missing_ref_no", kind))
                elif kind == "whitespace":
                    reasons.append(("whitespace_ref_no", _raw_key(ref)))
                for category, field in (("classification", "raw_code"), ("accuracy", "accuracy")):
                    raw_value = row[fields[field]]
                    key = _raw_key(raw_value)
                    frequency[category, key] += 1
                    frequency_values[category, key] = raw_value
                code_status = _classification_parts(row[fields["raw_code"]])[0]
                if code_status != "valid":
                    reasons.append((code_status + "_classification", _raw_key(row[fields["raw_code"]])))
                accuracy = row[fields["accuracy"]]
                if _missing_kind(accuracy) != "present":
                    reasons.append(("missing_or_invalid_accuracy", _raw_key(accuracy)))
                elif known_accuracy is not None and _raw_key(accuracy) not in known_accuracy:
                    reasons.append(("unknown_accuracy_code", _raw_key(accuracy)))
                geo = _geometry_check(row[info["geometry"]["column_name"]], info["geometry"]["srs_id"])
                geometry_status[geo["status"]] += 1
                geometry_types[geo["type"] or "UNREADABLE_OR_NULL"] += 1
                if geo["status"] != "valid_point":
                    reasons.append(("geometry_" + geo["status"], geo["detail"]))
                easting, northing = _number(row[fields["easting"]]), _number(row[fields["northing"]])
                if easting is None or northing is None:
                    reasons.append(("invalid_attribute_coordinates", "finite easting and northing required"))
                delta = None
                if geo["xy"] is not None and easting is not None and northing is not None:
                    x, y = geo["xy"]
                    dx, dy = x - easting, y - northing
                    value = math.hypot(dx, dy)
                    if math.isfinite(value):
                        delta = value
                        compared += 1
                        mean += (delta - mean) / compared
                        if maximum is None or delta > maximum:
                            maximum, maximum_fid = delta, fid
                        difference_writer.writerow([fid, x, y, easting, northing, dx, dy, delta])
                        if delta != 0:
                            unequal += 1
                            reasons.append(("geometry_attribute_difference", repr(delta)))
                    else:
                        reasons.append(("coordinate_difference_overflow", "non-finite difference"))
                for reason, detail in reasons:
                    issues[reason] += 1
                    issue_writer.writerow([fid, reason, detail])
                if info["scanned_count"] < 20:
                    sample = {"scope": "sample", "source_fid": fid, "source_layer": info["layer"],
                              "raw_fields": {name: {"type": _raw(value)[0], "value": _raw(value)[1]} for name, value in row.items() if name != info["geometry"]["column_name"]},
                              "geometry": geo, "delta_m": delta, "issues": reasons}
                    samples.write(json.dumps(sample, ensure_ascii=False, allow_nan=False) + "\n")
                info["scanned_count"] += 1
            db.executemany("INSERT INTO ref_rows VALUES (?,?)", refs)
            entries = []
            for (category, key), count in frequency.items():
                value = frequency_values[category, key]
                raw_type, raw_value = _raw(value)
                parts = _classification_parts(value) if category == "classification" else (None, None, None, None)
                entries.append((category, key, raw_type, raw_value, *parts, count))
            db.executemany("INSERT INTO frequencies VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(kind,raw_key) DO UPDATE SET n=n+excluded.n", entries)
            db.commit()
            if progress is not None:
                progress({"stage": "poi", "scanned_count": info["scanned_count"], "source_count": info["source_count"]})
    info.update(missing_by_field={k: dict(v) for k, v in missing.items()}, geometry_status=dict(geometry_status), geometry_types=dict(geometry_types), issue_counts=dict(issues))
    info["coordinate_difference"] = {"comparable_count": compared, "uncomparable_count": info["scanned_count"] - compared, "zero_count": compared - unequal, "nonzero_count": unequal, "mean_m": mean if compared else None, "max_m": maximum, "max_source_fid": maximum_fid}
    expected = info["source_count"] if limit is None else min(info["source_count"], limit)
    info["expected_scan_count"] = expected
    if info["scanned_count"] != expected:
        raise ValueError(f"POI row count mismatch: expected {expected}, scanned {info['scanned_count']}")
    if any(sum(counts.values()) != expected for counts in missing.values()):
        raise ValueError("POI field missingness counts do not reconcile")


def _station_reader(stream, fields):
    text = io.TextIOWrapper(stream, encoding="utf-8-sig", newline="")
    reader = csv.reader(text, strict=True)
    header = next(reader, None)
    if not header or len(set(header)) != len(header) or any(not name for name in header):
        raise ValueError("station CSV requires a non-empty unique header")
    if set(fields.values()) - set(header):
        raise ValueError(f"station CSV missing required fields: {sorted(set(fields.values()) - set(header))}")
    return text, reader, header


def _inspect_stations(path, root, output, db, report, fields, batch_size, limit):
    info = report["stations"]
    missing = {name: Counter() for name in fields.values()}
    issues = Counter()
    with ExitStack() as stack:
        writer = _csv_writer(stack, output / "station_quality_issues.csv", ["source_record_no", "StationID", "reason", "detail"])
        sample = stack.enter_context((output / "station_sample.jsonl").open("x", encoding="utf-8"))
        stream = stack.enter_context(open_source(path, data_root=root))
        text, reader, header = _station_reader(stream, fields)
        stack.callback(text.detach)
        info.update(header=header, field_mapping=fields)
        iterator = islice(reader, limit) if limit is not None else reader
        for batch in iter(lambda: list(islice(iterator, batch_size)), []):
            entries = []
            for values in batch:
                info["scanned_count"] += 1
                record_no = info["scanned_count"]
                row = {name: values[i] if i < len(values) else None for i, name in enumerate(header)}
                reasons = []
                if len(values) != len(header):
                    reasons.append(("csv_field_count", f"expected {len(header)}, got {len(values)}"))
                for name in fields.values():
                    missing[name][_missing_kind(row[name])] += 1
                station_id = row[fields["id"]]
                if station_id is None or station_id == "":
                    reasons.append(("missing_station_id", "null or empty ID"))
                    station_id = None
                elif not station_id.strip():
                    reasons.append(("whitespace_station_id", repr(station_id)))
                lat, lon = _number(row[fields["latitude"]]), _number(row[fields["longitude"]])
                xy_key = None
                if lat is None or lon is None:
                    reasons.append(("invalid_station_coordinates", "finite numeric latitude/longitude required"))
                elif not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    reasons.append(("station_coordinates_out_of_range", f"longitude={lon}, latitude={lat}"))
                else:
                    # Signed zero has the same exact numerical location.
                    xy_key = json.dumps([lon or 0.0, lat or 0.0], separators=(",", ":"))
                entries.append((record_no, station_id, xy_key))
                for reason, detail in reasons:
                    writer.writerow([record_no, station_id, reason, detail])
                    issues[reason] += 1
                if record_no <= 20:
                    sample.write(json.dumps({"scope": "sample", "source_record_no": record_no, "raw_fields": row, "issues": reasons}, ensure_ascii=False) + "\n")
            db.executemany("INSERT INTO station_rows VALUES (?,?,?)", entries)
            db.commit()
        group_checks = [
            ("duplicate_station_id", "station_id", "station_id IS NOT NULL", "COUNT(*) > 1"),
            ("station_id_position_conflict", "station_id", "station_id IS NOT NULL AND xy_key IS NOT NULL", "COUNT(DISTINCT xy_key) > 1"),
            ("co_located_different_ids", "xy_key", "station_id IS NOT NULL AND xy_key IS NOT NULL", "COUNT(DISTINCT station_id) > 1"),
        ]
        groups = {}
        for reason, key, where, having in group_checks:
            group_count, affected = 0, 0
            query = f"SELECT {key} FROM station_rows WHERE {where} GROUP BY {key} HAVING {having} ORDER BY {key}"
            for (value,) in db.execute(query):
                group_count += 1
                for record_no, station_id in db.execute(f"SELECT record_no,station_id FROM station_rows WHERE {key}=? AND station_id IS NOT NULL ORDER BY record_no", (value,)):
                    writer.writerow([record_no, station_id, reason, value])
                    affected += 1
                    issues[reason] += 1
            groups[reason] = {"group_count": group_count, "affected_row_count": affected}
    info.update(missing_by_field={k: dict(v) for k, v in missing.items()}, issue_counts=dict(issues), groups=groups, source_count=None)
    if limit is None:
        # Independent logical-record count: quoted newlines are not records.
        with open_source(path, data_root=root) as stream:
            text = io.TextIOWrapper(stream, encoding="utf-8-sig", newline="")
            try:
                reader = csv.reader(text, strict=True)
                next(reader)
                info["source_count"] = sum(1 for _ in reader)
            finally:
                text.detach()
        if info["source_count"] != info["scanned_count"]:
            raise ValueError("station logical record counts do not reconcile")


def _export_frequencies(db, output, count, known_accuracy):
    summaries = {}
    with ExitStack() as stack:
        for kind, filename in (("classification", "classification_frequencies.csv"), ("accuracy", "positional_accuracy_frequencies.csv")):
            writer = _csv_writer(stack, output / filename, ["raw_type", "raw_value", "format_status", "group_code", "category_code", "class_code", "count", "dictionary_status", "accuracy_description"])
            total, distinct = 0, 0
            for key, raw_type, raw_value, status, group, category, class_code, n in db.execute("SELECT raw_key,raw_type,raw_value,format_status,group_code,category_code,class_code,n FROM frequencies WHERE kind=? ORDER BY raw_key", (kind,)):
                writer.writerow([raw_type, raw_value, status, group, category, class_code, n, "not_checked" if kind == "classification" else "", (known_accuracy or {}).get(key, "") if kind == "accuracy" else ""])
                total += n
                distinct += 1
            if total != count:
                raise ValueError(f"{kind} frequency count {total} differs from scanned count {count}")
            summaries[kind] = {"row_count": total, "distinct_raw_values": distinct}
        writer = _csv_writer(stack, output / "native_level_frequencies.csv", ["level", "code", "count"])
        for level in ("group", "category", "class"):
            counts = db.execute(f"SELECT {level}_code,SUM(n) FROM frequencies WHERE kind='classification' GROUP BY {level}_code ORDER BY {level}_code")
            total, distinct = 0, 0
            for code, n in counts:
                writer.writerow([level, code, n])
                total += n
                distinct += 1
            if total != count:
                raise ValueError(f"{level} counts do not reconcile")
            summaries[level] = {"row_count": total, "distinct_codes_including_unknown": distinct}
    return summaries


def _export_duplicates(db, output, source, layer, sample):
    db.execute("CREATE TABLE duplicate_refs (ref_key TEXT PRIMARY KEY, n INTEGER)")
    db.execute("INSERT INTO duplicate_refs SELECT ref_key,COUNT(*) FROM ref_rows WHERE ref_key IS NOT NULL GROUP BY ref_key HAVING COUNT(*)>1")
    db.commit()
    prefix = "sample_" if sample else ""
    key_count, involved = 0, 0
    with ExitStack() as stack:
        keys = _csv_writer(stack, output / (prefix + "duplicate_ref_nos.csv"), ["raw_type", "raw_value", "count"])
        rows = _csv_writer(stack, output / (prefix + "duplicate_ref_rows.csv"), ["source_path", "source_layer", "source_fid", "raw_type", "raw_value", "count"])
        for key, n in db.execute("SELECT ref_key,n FROM duplicate_refs ORDER BY ref_key"):
            raw_type, raw_value = json.loads(key)
            keys.writerow([raw_type, raw_value, n])
            key_count += 1
            involved += n
            actual = 0
            for (fid,) in db.execute("SELECT source_fid FROM ref_rows WHERE ref_key=? ORDER BY source_fid", (key,)):
                rows.writerow([source, layer, fid, raw_type, raw_value, n])
                actual += 1
            if actual != n:
                raise ValueError("duplicate source rows do not reconcile")
    missing = db.execute("SELECT COUNT(*) FROM ref_rows WHERE ref_key IS NULL").fetchone()[0]
    unique = db.execute("SELECT COUNT(DISTINCT ref_key) FROM ref_rows").fetchone()[0]
    total = db.execute("SELECT COUNT(*) FROM ref_rows").fetchone()[0]
    if missing + unique + involved - key_count != total:
        raise ValueError("ref_no counts do not reconcile")
    return {"missing_count": missing, "unique_nonmissing_count": unique, "duplicate_key_count": key_count, "duplicate_row_count": involved, "excess_row_count": involved - key_count, "source_fid_count": total}


def inspect_inputs(poi_path, station_path, *, data_root, output_dir, layer=None,
                   poi_batch_size=10000, station_batch_size=1000, sample_limit=None,
                   poi_fields=None, station_fields=None, accuracy_labels=None, progress=None):
    """Audit raw inputs into a NEW outputs/<run-id> directory and return a report.

    Caller verifies the source manifest first. This function additionally hashes
    its two inputs before/after reading. sample_limit never produces a complete
    duplicate list. Errors retain a failed report and raise InputInspectionError.
    """
    _positive_integer(poi_batch_size, "poi_batch_size")
    _positive_integer(station_batch_size, "station_batch_size")
    if sample_limit is not None:
        _positive_integer(sample_limit, "sample_limit")
    fields = _field_mapping(poi_fields, POI_FIELDS)
    stations = _field_mapping(station_fields, STATION_FIELDS)
    labels = None
    if accuracy_labels is not None:
        if not isinstance(accuracy_labels, dict) or any(not isinstance(v, str) for v in accuracy_labels.values()):
            raise ValueError("accuracy_labels must map raw codes to descriptions")
        labels = {_raw_key(k): v for k, v in accuracy_labels.items()}
    root = project_path(".", root=data_root)
    output = project_path(output_dir, root=root, field="output_dir")
    if not Path(output_dir).is_relative_to("outputs") or output == root / "outputs" or not output.is_relative_to(root / "outputs"):
        raise ValueError("output_dir must be a new directory strictly inside outputs/")
    for source in (poi_path, station_path):
        _source_path(source, root)
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "schema_version": 1, "status": "running", "scope": "sample" if sample_limit is not None else "full_source",
        "sample_limit": sample_limit, "scan_complete": False, "full_source_complete": False,
        "duplicate_list_complete": False, "errors": [], "output_dir": str(output),
        "poi": {"path": poi_path, "scanned_count": 0}, "stations": {"path": station_path, "scanned_count": 0},
        "batch_sizes": {"poi": poi_batch_size, "stations": station_batch_size},
        "dictionary_checked": False, "selection_or_cleaning_applied": False,
        "accuracy_lookup_supplied": labels is not None,
    }
    _json_write(output / "input_inspection.json", report)
    db = None
    try:
        before = {source: _fingerprint(source, root, 1048576) for source in (poi_path, station_path)}
        states = {source: _file_state(_source_path(source, root).stat()) for source in before}
        report["input_fingerprints_before"] = before
        with _gpkg_connection(poi_path, root) as connection:
            report["poi"].update(_gpkg_schema(connection, layer, fields))
        db = _audit_database(output / "audit_index.sqlite")
        _inspect_pois(poi_path, root, output, db, report, fields, poi_batch_size, sample_limit, labels, progress)
        _inspect_stations(station_path, root, output, db, report, stations, station_batch_size, sample_limit)
        report["poi"]["frequencies"] = _export_frequencies(db, output, report["poi"]["scanned_count"], labels)
        after = {source: _fingerprint(source, root, 1048576) for source in before}
        report["input_fingerprints_after"] = after
        report["input_bytes_unchanged"] = before == after
        if before != after or any(states[source] != _file_state(_source_path(source, root).stat()) for source in before):
            raise ValueError("input changed during inspection")
        report["poi"]["ref_no"] = _export_duplicates(db, output, poi_path, report["poi"]["layer"], sample_limit is not None)
        if report["poi"]["ref_no"]["source_fid_count"] != report["poi"]["scanned_count"]:
            raise ValueError("source FID count does not reconcile")
        report.update(status="completed", scan_complete=True, full_source_complete=sample_limit is None, duplicate_list_complete=sample_limit is None)
        report["quality_issues_present"] = bool(report["poi"]["issue_counts"] or report["stations"]["issue_counts"] or report["poi"]["ref_no"]["duplicate_key_count"])
    except Exception as error:
        report.update(status="failed", scan_complete=False, full_source_complete=False, duplicate_list_complete=False)
        report["errors"].append(f"{type(error).__name__}: {error}")
        _json_write(output / "input_inspection.json", report)
        raise InputInspectionError(report) from error
    finally:
        if db is not None:
            db.close()
    _json_write(output / "input_inspection.json", report)
    return report
