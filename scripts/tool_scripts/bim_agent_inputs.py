"""Freeze user building declarations for the standalone BIM-agent experiment.

This module deliberately does not interpret the declaration as a BIM schema.
It preserves the supplied JSON and only links declared image paths to images
that the caller has already admitted to the experiment's image inventory.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterator


FROZEN_BUILDING_INPUT = "building_input.json"
FROZEN_PLAN_INPUT = "resume_plan.json"


def _json_pointer_part(value: object) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _declared_image_paths(value: object, pointer: str = "") -> Iterator[tuple[str, str]]:
    """Yield path-valued declaration fields without opening their targets."""
    if isinstance(value, dict):
        for key, child in value.items():
            child_pointer = f"{pointer}/{_json_pointer_part(key)}"
            if isinstance(child, str) and "path" in str(key).casefold():
                yield child_pointer, child
            else:
                yield from _declared_image_paths(child, child_pointer)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _declared_image_paths(child, f"{pointer}/{index}")


def _inventory_associations(declaration: dict, images: dict) -> list[dict]:
    """Associate declarations by exact basename with the admitted inventory.

    The declared value is treated only as text.  In particular, this function
    never resolves it relative to the JSON file and never reads that path.
    """
    rows = []
    for pointer, declared_path in _declared_image_paths(declaration):
        basename = declared_path.replace("\\", "/").rsplit("/", 1)[-1]
        image = images.get(basename)
        row = {
            "json_pointer": pointer,
            "declared_path": declared_path,
            "declared_basename": basename,
        }
        if image is None:
            row.update({
                "status": "not_in_authorized_image_inventory",
                "authorized_image": None,
            })
        else:
            row.update({
                "status": "associated_by_exact_basename",
                "authorized_image": basename,
                "authorized_image_sha256": image["sha256"],
            })
        rows.append(row)
    return rows


def read_building_input(source: Path) -> tuple[bytes, dict]:
    """Read only the explicitly admitted UTF-8 declaration, never its path fields."""
    if not source.is_file():
        raise ValueError("building input must name an existing JSON file")
    raw = source.read_bytes()
    try:
        declaration = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"building input must be valid UTF-8 JSON: {error}") from error
    if not isinstance(declaration, dict):
        raise ValueError("building input must be a JSON object")
    return raw, declaration


def freeze_building_input(source: Path, run: Path, images: dict) -> dict:
    """Copy one JSON declaration byte-for-byte and describe its provenance."""
    source = source.resolve()
    raw, declaration = read_building_input(source)

    frozen = run / FROZEN_BUILDING_INPUT
    frozen.write_bytes(raw)
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    associations = _inventory_associations(declaration, images)
    return {
        "source_path": str(source),
        "frozen_path": FROZEN_BUILDING_INPUT,
        "raw_sha256": raw_sha256,
        "raw_size_bytes": len(raw),
        "declaration": declaration,
        "image_path_associations": associations,
        "image_path_policy": (
            "Declared paths are provenance labels only. They are associated by exact basename "
            "with the already authorized PNG inventory and are never opened as file paths."
        ),
        "field_semantics": {
            "thermal_zones": (
                "User declaration for downstream simulation zoning; it is not a measured or "
                "authoritative count of physical source spaces."
            ),
            "other_fields": (
                "User-supplied building declarations retain their stated meaning and are not "
                "ground truth or independently verified observations."
            ),
        },
        "conflict_policy": (
            "The agent must compare declarations with supplied mesh, image and other evidence, preserve the "
            "conflict or uncertainty, and decide its modelling effect explicitly."
        ),
    }


def freeze_plan_input(source: Path, run: Path, images: dict, image_name: str) -> dict:
    """Freeze one unverified pixel-plan JSON, bound to an admitted original image."""
    if image_name not in images:
        raise ValueError(f"--plan-image {image_name!r} is not in this run's PNG image inventory")
    source = source.resolve()
    raw = source.read_bytes()
    try:
        declaration = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"--resume-plan must be valid UTF-8 JSON: {error}") from error
    if not isinstance(declaration, dict):
        raise ValueError("--resume-plan must contain a JSON object")
    (run / FROZEN_PLAN_INPUT).write_bytes(raw)
    return {
        "source_path": str(source),
        "frozen_path": FROZEN_PLAN_INPUT,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_size_bytes": len(raw),
        "declaration": declaration,
        "image": image_name,
        "image_sha256": images[image_name]["sha256"],
        "status": "unverified_pixel_plan_declaration_not_compiled",
    }
