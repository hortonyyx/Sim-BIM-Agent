"""Run-local metadata path helpers."""

from __future__ import annotations

from pathlib import Path

RUN_META_DIR = "_run"


def run_meta_path(run_dir: Path, name: str, *, for_write: bool = False) -> Path:
    """Return ``<run>/_run/<name>`` for generated run metadata.

    ``name`` is intentionally limited to a single filename so callers do not
    smuggle stage artifacts through the metadata helper.
    """
    if Path(name).name != name:
        raise ValueError(f"run metadata name must be a filename: {name!r}")
    path = Path(run_dir) / RUN_META_DIR / name
    if for_write:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path
