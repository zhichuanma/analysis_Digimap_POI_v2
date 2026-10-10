"""Project path checks; source reading and verification belong to later steps."""

from pathlib import Path, PureWindowsPath


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
