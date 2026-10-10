"""Project paths and read-only byte-level verification of source copies."""

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat


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
