"""Bind a model's mesh handoff judgment to the current saved evidence.

Coverage is a model declaration. Current evidence does not certify independent
mesh fidelity, establish a view-count policy, or require auxiliary-image measurement.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from PIL import Image

from src.agent.geometry.mesh_bim_frame import validate_mesh_frame
from src.agent.geometry.source_model import _digest


POLICY = "mesh_delivery_review_v1"


def _path(run: Path, relative: str) -> Path:
    run = Path(run).resolve()
    path = run / relative
    if path.resolve() != path or not path.is_relative_to(run):
        raise ValueError("mesh review evidence escapes its admitted path")
    return path


def _candidate(candidate: str) -> str:
    if not isinstance(candidate, str) or not re.fullmatch(r"candidate_\d+|seed", candidate):
        raise ValueError("mesh review requires an admitted candidate ID")
    return candidate


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path) -> dict:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("mesh review evidence must contain a JSON object")
    return value


def _declaration(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != {"coverage", "summary", "overlay_ids"}:
        raise ValueError("mesh_review requires coverage, summary and overlay_ids")
    if not isinstance(value["coverage"], str) or value["coverage"] not in {"whole_building", "partial"}:
        raise ValueError("mesh_review coverage must be whole_building or partial")
    if not isinstance(value["summary"], str) or not value["summary"].strip():
        raise ValueError("mesh_review summary must be nonempty")
    ids = value["overlay_ids"]
    if (not isinstance(ids, list) or not ids or
            any(not isinstance(item, str) or not re.fullmatch(r"overlay_\d+", item) for item in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError("mesh_review needs at least one distinct overlay ID")
    return {"coverage": value["coverage"], "summary": value["summary"], "overlay_ids": list(ids)}


def _png(path: Path, expected_size=None) -> None:
    with Image.open(path) as image:
        if image.format != "PNG" or (expected_size is not None and image.size != expected_size):
            raise ValueError("mesh review evidence image format or dimensions changed")
        image.verify()


def _evidence(run, candidate, source, manifest, declaration):
    """Reconstruct the exact allowed evidence set rather than trusting stored paths."""
    candidate = _candidate(candidate)
    if not isinstance(manifest, dict) or not manifest.get("mesh_input"):
        raise ValueError("mesh_review requires an admitted original mesh")
    paths = ["inputs.json", f"{candidate}/source_model.json", f"{candidate}/report.json",
             "assets/input.glb"]
    if _json(_path(run, "inputs.json")) != manifest:
        raise ValueError("mesh input manifest changed")
    images = manifest.get("images", {})
    if not isinstance(images, dict):
        raise ValueError("mesh image inventory must be an object")
    for name, entry in images.items():
        if (not isinstance(name, str) or Path(name).name != name
                or "/" in name or "\\" in name or Path(name).suffix.lower() != ".png"):
            raise ValueError("mesh auxiliary image must use an admitted PNG filename")
        relative = f"images/{name}"
        image_path = _path(run, relative)
        if not isinstance(entry, dict) or entry.get("sha256") != _sha(image_path):
            raise ValueError("admitted auxiliary image changed")
        _png(image_path, tuple(entry["size"]))
        paths.append(relative)
    for key, filename in (("building_input", "building_input.json"),
                          ("plan_recovery", "resume_plan.json")):
        entry = manifest.get(key)
        if entry is None:
            continue
        path = _path(run, filename)
        if (not isinstance(entry, dict) or entry.get("frozen_path") != filename
                or entry.get("raw_sha256") != _sha(path)
                or entry.get("declaration") != _json(path)):
            raise ValueError(f"admitted {key} declaration changed")
        paths.append(filename)
    saved_source = _json(_path(run, paths[1]))
    if saved_source != source:
        raise ValueError("mesh review source differs from saved candidate")
    source_hash = _digest({key: value for key, value in source.items() if key != "source_model_sha256"})
    if source.get("source_model_sha256") != source_hash:
        raise ValueError("mesh review source digest mismatch")
    report = _json(_path(run, paths[2]))
    if (source.get("validation", {}).get("status") not in {"pass", "warning"}
            or report.get("source_geometry_ready") is not True
            or report.get("source_model_sha256") != source_hash):
        raise ValueError("mesh review requires current ready source geometry")
    mesh = manifest["mesh_input"]
    mesh_sha = _sha(_path(run, "assets/input.glb"))
    if mesh.get("frozen_path") != "assets/input.glb" or mesh.get("sha256") != mesh_sha:
        raise ValueError("admitted original mesh changed")
    if validate_mesh_frame(source.get("mesh_frame"))["mesh_sha256"] != mesh_sha:
        raise ValueError("source mesh_frame refers to a different mesh")
    source_file_sha = _sha(_path(run, paths[1]))
    for overlay_id in declaration["overlay_ids"]:
        overlay_file = f"mesh_overlays/{overlay_id}.json"
        image_file = f"mesh_overlays/{overlay_id}.png"
        overlay = _json(_path(run, overlay_file))
        if (overlay.get("candidate") != candidate or overlay.get("overlay_id") != overlay_id
                or overlay.get("source_model_sha256") != source_hash
                or overlay.get("source_file_sha256") != source_file_sha
                or overlay.get("mesh_sha256") != mesh_sha
                or overlay.get("mesh_frame") != source["mesh_frame"]
                or overlay.get("image_file") != image_file):
            raise ValueError("mesh overlay does not bind this saved candidate and original mesh")
        image_path = _path(run, image_file)
        if overlay.get("image_sha256") != _sha(image_path):
            raise ValueError("mesh overlay image changed")
        observation = overlay.get("observation")
        if not isinstance(observation, str) or not re.fullmatch(r"mesh_\d+", observation):
            raise ValueError("mesh overlay must refer to an admitted observation ID")
        metadata_file = f"mesh_observations/{observation}.json"
        original_file = f"mesh_observations/{observation}.png"
        metadata_path = _path(run, metadata_file)
        if overlay.get("observation_metadata_sha256") != _sha(metadata_path):
            raise ValueError("mesh observation metadata changed")
        original_path = _path(run, original_file)
        if overlay.get("observation_image_sha256") != _sha(original_path):
            raise ValueError("mesh observation image changed since overlay creation")
        metadata = _json(metadata_path)
        if metadata.get("mesh_sha256") != mesh_sha:
            raise ValueError("mesh observation refers to a different original mesh")
        resolution = metadata["resolution_px"]
        size = (resolution["width"], resolution["height"])
        _png(image_path, size)
        _png(original_path, size)
        paths += [overlay_file, image_file, metadata_file, original_file]
    return {"source_model_sha256": source_hash, "mesh_sha256": mesh_sha,
            "file_sha256": {name: _sha(_path(run, name)) for name in sorted(set(paths))}}


def _status(candidate, status, *, declaration=None, record_sha256=None, reason=None):
    declaration = declaration or {}
    result = {"status": status, "candidate": candidate,
              "complete_building": status == "current" and declaration.get("coverage") == "whole_building",
              "coverage": declaration.get("coverage"), "summary": declaration.get("summary"),
              "overlay_ids": declaration.get("overlay_ids", []), "fidelity": "not_evaluated",
              "review_file": f"{candidate}/mesh_review.json", "record_sha256": record_sha256}
    if reason is not None:
        result["reason"] = reason
    return result


def validate_mesh_review(run, candidate, source, manifest, record=None) -> dict:
    """Read-only validation for runtime completion; invalid evidence never completes."""
    if record is None:
        return _status(candidate, "missing")
    file_sha = None
    try:
        path = _path(run, f"{_candidate(candidate)}/mesh_review.json")
        file_sha = _sha(path)
        if _json(path) != record:
            raise ValueError("saved mesh review differs from the supplied record")
        if (record.get("policy") != POLICY or record.get("candidate") != candidate
                or record.get("fidelity") != "not_evaluated"
                or record.get("record_digest") != _digest({k: v for k, v in record.items() if k != "record_digest"})):
            raise ValueError("saved mesh review identity or digest changed")
        declaration = _declaration(record["declaration"])
        if record.get("evidence") != _evidence(run, candidate, source, manifest, declaration):
            raise ValueError("mesh review evidence changed")
        return _status(candidate, "current", declaration=declaration, record_sha256=file_sha)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, SyntaxError, Image.DecompressionBombError) as error:
        return _status(candidate, "stale", record_sha256=file_sha, reason=str(error))


def mesh_review_status(toolkit, candidate) -> dict:
    """Re-read current evidence; a saved declaration cannot transfer to a revision."""
    try:
        folder = _path(toolkit.run, _candidate(candidate))
        path = folder / "mesh_review.json"
        if not path.exists():
            return _status(candidate, "missing")
        record = _json(_path(toolkit.run, f"{candidate}/mesh_review.json"))
        source = _json(_path(toolkit.run, f"{candidate}/source_model.json"))
        return validate_mesh_review(toolkit.run, candidate, source, toolkit.manifest, record)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, SyntaxError, Image.DecompressionBombError) as error:
        return _status(candidate, "stale", reason=str(error))


def carry_mesh_review(toolkit, candidate, reviews_json) -> dict | None:
    """Consume the new mesh object; empty/legacy list input stays with its caller."""
    if not reviews_json:
        return None
    payload = json.loads(reviews_json)
    if isinstance(payload, list):
        return None
    if not isinstance(payload, dict) or set(payload) != {"mesh_review"}:
        raise ValueError("mesh reviews_json must contain only mesh_review")
    if not toolkit.manifest.get("mesh_input"):
        raise ValueError("mesh_review is not accepted for drawing-only runs")
    declaration = _declaration(payload["mesh_review"])
    _candidate(candidate)
    source = _json(_path(toolkit.run, f"{candidate}/source_model.json"))
    record = {"policy": POLICY, "candidate": candidate, "declaration": declaration,
              "fidelity": "not_evaluated",
              "evidence": _evidence(toolkit.run, candidate, source, toolkit.manifest, declaration)}
    record["record_digest"] = _digest(record)
    target = _path(toolkit.run, f"{candidate}/mesh_review.json")
    target.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return mesh_review_status(toolkit, candidate)
