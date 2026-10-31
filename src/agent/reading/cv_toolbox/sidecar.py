"""Append-only sidecar writer for reading-stage CV evidence."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

from PIL import Image

_SIDECAR_NAME_RE = re.compile(r"^\d{3}_[A-Za-z0-9_-]+(\.json)?$")


def sha256_short(path: str | Path, length: int = 12) -> str:
    h = sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:length]


def evidence_dir(out_dir: str | Path, source_image: str | Path) -> Path:
    return Path(out_dir) / "cv_evidence" / Path(source_image).stem


def allocate_sidecar_path(
    out_dir: str | Path,
    source_image: str | Path,
    tool: str,
    sidecar_name: str = "auto",
) -> Path:
    """Allocate an append-only sidecar path.

    `auto` picks the first unused NNN slot. Explicit names are accepted as
    `NNN_tool` or `NNN_tool.json` and raise if already present.
    """

    root = evidence_dir(out_dir, source_image)
    root.mkdir(parents=True, exist_ok=True)

    if sidecar_name == "auto":
        for n in range(1, 1000):
            candidate = root / f"{n:03d}_{tool}.json"
            if not candidate.exists():
                return candidate
        raise RuntimeError(f"no free sidecar slot under {root}")

    if not _SIDECAR_NAME_RE.fullmatch(sidecar_name):
        raise ValueError("sidecar_name must match NNN_tool or NNN_tool.json")
    name = sidecar_name if sidecar_name.endswith(".json") else f"{sidecar_name}.json"
    path = root / name
    if path.exists():
        raise FileExistsError(f"sidecar already exists: {path}")
    return path


def write_sidecar(
    sidecar_path: str | Path,
    source_image: str | Path,
    tool_payload: dict[str, Any],
    *,
    crop_chain: list[dict[str, Any]] | None = None,
    overlay_path: str | Path | None = None,
) -> Path:
    """Write a cv_schema=1 sidecar without overwriting existing evidence."""

    path = Path(sidecar_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    source_hash = sha256_short(source_image)
    # F-51: report the SOURCE image's true pixel size. The reader (VLM) works on
    # a possibly downsampled frame, so its self-reported pixel coordinates may
    # live in a different frame than these sidecars (which read the original
    # file). width_px/height_px give it the anchor to detect that mismatch.
    # Deliberately the source image's size — never the post-crop size, even when
    # crop_chain is non-empty.
    with Image.open(source_image) as _source_img:
        source_width_px, source_height_px = _source_img.size
    payload = deepcopy(tool_payload)
    for idx, result in enumerate(payload.get("results", []), start=1):
        result.setdefault("candidate_id", f"{Path(source_image).stem}:{payload.get('tool', 'cv_tool')}:{idx:03d}")
        result.setdefault("coord_space", "source_px")
        provenance = result.setdefault("provenance", {})
        # Future J0 can cite this sidecar path/candidate_id as evidence, but J0
        # does not consume cv_evidence in this batch.
        provenance.setdefault("tool", payload.get("tool", ""))
        provenance.setdefault("tool_version", payload.get("tool_version", ""))
        provenance.setdefault("recipe_id", payload.get("recipe_id", ""))
        provenance["source_image_sha256"] = source_hash
        provenance.setdefault("crop_chain_id", "root" if not crop_chain else "crop_chain:0")

    sidecar = {
        "cv_schema": "1",
        "source_image": {
            "name": Path(source_image).name,
            "sha256": source_hash,
            "width_px": int(source_width_px),
            "height_px": int(source_height_px),
        },
        # Phase B anchor_px will be lifted from per-candidate fields here later;
        # the reading schema is intentionally untouched in this batch.
        "crop_chain": crop_chain or [],
        "overlay_path": str(overlay_path) if overlay_path is not None else None,
        **payload,
    }
    # Future attempts collection may archive cv_evidence beside output/checks;
    # current behavior is a flat-stage audit sidecar only.
    with path.open("x", encoding="utf-8") as fh:
        fh.write(json.dumps(sidecar, indent=2, sort_keys=True) + "\n")
    return path
