"""Path containment and relative-path normalization for a snapshot root."""

from __future__ import annotations

from pathlib import Path

from .errors import ValidationError


def resolve_root(root: str | Path) -> Path:
    p = Path(root).expanduser().resolve()
    if not p.exists():
        raise ValidationError(f"snapshot root does not exist: {p}")
    if not p.is_dir():
        raise ValidationError(f"snapshot root is not a directory: {p}")
    return p


def rel_posix(root: Path, path: Path) -> str:
    """Return a POSIX-style path relative to root, rejecting escape attempts."""
    try:
        rel = path.relative_to(root)
    except ValueError as exc:
        raise ValidationError(f"path {path} is outside snapshot root {root}") from exc
    return rel.as_posix()
