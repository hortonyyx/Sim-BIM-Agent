"""Immutable elevation readings and adopted-value overlays for one model.

This reuses the elevation reader's validation, ink policy, grid adoption and
matcher. A saved reading is model-supplied evidence, not independent validation
of the drawing or permission to change a source BIM.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import re
from collections import Counter
from pathlib import Path

from PIL import Image

from src.agent.runtime_roles.elevation import (
    expand_elevation_submission, match_elevation, validate_elevation_artifact,
)
from src.agent.runtime_roles.elevation_ink import align_elevation_artifact
from src.agent.runtime_roles.height_evidence import derive_z_calibration
from src.agent.runtime_roles.elevation_views import _check_image_coordinates, _adopted_geometry, _overlay


SCHEMA = "drawing_elevation_reading_v1"
TABLE_LIMIT = 40
_READING_ID = re.compile(r"reading_[0-9]{4,}")
_NOTICE = "Model-supplied reading; not independently verified against the original drawing."


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _hash(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _reject_constant(value):
    raise ValueError(f"reading JSON contains non-finite constant {value}")


def normalize_submission(submitted):
    """Reorder equivalent representations, never guess a coordinate direction."""
    if not isinstance(submitted, dict):
        raise ValueError("reading_json must contain an object")
    row = copy.deepcopy(submitted)
    changes = []
    for name in ("x_calibration", "z_calibration"):
        cal = row.get(name)
        if not isinstance(cal, dict):
            continue
        start, end = cal.get("pixel_start"), cal.get("pixel_end")
        if (isinstance(start, (float, int)) and isinstance(end, (float, int))
                and start > end):
            cal["pixel_start"], cal["pixel_end"] = end, start
            prefix = "distance" if "distance_start_m" in cal else "world"
            a, b = prefix + "_start_m", prefix + "_end_m"
            if a in cal and b in cal:
                cal[a], cal[b] = cal[b], cal[a]
            changes.append({"field": name, "action": "reordered_paired_anchors"})
    openings = row.get("openings")
    if isinstance(openings, list) and all(isinstance(item, dict) for item in openings):
        def centre(item):
            span = item.get("x_px")
            if span is None:
                bounds = item.get("opening_bbox", [])
                span = [bounds[0], bounds[2]] if len(bounds) == 4 else []
            if len(span) != 2 or any(not isinstance(v, (float, int)) for v in span):
                raise ValueError("each opening needs x_px or opening_bbox in original pixels")
            return sum(span) / 2
        ordered = sorted(openings, key=lambda item: (str(item.get("floor_id", "")), centre(item)))
        if ordered != openings:
            changes.append({"field": "openings", "action": "sorted_by_floor_and_image_x"})
            row["openings"] = ordered
        if "counts" not in row:
            floors = {item.get("floor_id") for item in openings}
            floors.update(item.get("floor_id") for item in row.get("elevations", [])
                          if isinstance(item, dict) and item.get("kind") == "floor")
            counts = {floor: Counter(item.get("kind") for item in openings
                                    if item.get("floor_id") == floor) for floor in floors if floor}
            row["counts"] = [{"floor_id": floor, "window_count": counts[floor]["window"],
                              "door_count": counts[floor]["door"]} for floor in sorted(counts)]
            changes.append({"field": "counts", "action": "derived_from_declared_openings_and_floors"})
    return row, changes


def _directory(toolkit, *, create=False):
    root = toolkit.run.resolve()
    folder = root / "drawing_readings"
    if folder.resolve().parent != root:
        raise ValueError("drawing_readings must stay inside this run")
    if create:
        folder.mkdir(exist_ok=True)
    return folder


def prepare_elevation_reading(toolkit, image, facade, reading_json, *, replaces_reading_id=""):
    """Return [unchanged original, adopted-value overlay] and a short saved receipt."""
    if not isinstance(reading_json, str):
        raise ValueError("reading_json must be a JSON object encoded as a string")
    if not isinstance(facade, str) or facade.title() not in {"North", "South", "East", "West"}:
        raise ValueError("facade must be North, South, East or West")
    facade = facade.title()
    image_path = toolkit.image_path(image)
    name = image_path.name
    if replaces_reading_id:
        previous = load_reading(toolkit, replaces_reading_id)
        if previous["image"] != name:
            raise ValueError("replaces_reading_id must belong to this same original image")
    image_sha = hashlib.sha256(image_path.read_bytes()).hexdigest()
    with Image.open(image_path) as opened:
        original = opened.convert("RGB")
    submitted = json.loads(reading_json, parse_constant=_reject_constant)
    _bytes(submitted)  # Reject overflow to infinity anywhere in the submission.
    normalized, normalization = normalize_submission(submitted)
    expanded = expand_elevation_submission(normalized, facade)
    if expanded.get("image", name) != name:
        raise ValueError("reading image must be the admitted original filename")
    artifact = validate_elevation_artifact(expanded, image_name=name, apply_regularization=False)
    _check_image_coordinates(artifact, original.size)
    z_evidence = derive_z_calibration(artifact["elevations"])
    if artifact.get("z_calibration") is None:
        if z_evidence.get("calibration") is None:
            raise ValueError("z_calibration or two consistent located horizontal levels are required for an adopted-value overlay")
        artifact.pop("artifact_sha256", None)
        artifact["z_calibration"] = z_evidence["calibration"]
        artifact = validate_elevation_artifact(artifact, image_name=name, apply_regularization=False)
        _check_image_coordinates(artifact, original.size)
    alignment = align_elevation_artifact(original, artifact)
    artifact = copy.deepcopy(artifact)
    artifact.pop("artifact_sha256", None)
    by_id = {row["id"]: row["aligned_values"] for row in alignment["openings"]}
    for row in artifact["openings"]:
        for field in ("x_px", "width_m", "sill_m", "head_m"):
            if by_id[row["id"]].get(field) is not None:
                row[field] = copy.deepcopy(by_id[row["id"]][field])
    artifact["ink_alignment"] = alignment
    artifact = validate_elevation_artifact(artifact, image_name=name)
    openings, levels, issues = _adopted_geometry(artifact, original.size)
    overlay = _overlay(original, artifact, openings, levels)
    stream = io.BytesIO()
    overlay.save(stream, format="PNG")
    overlay_bytes = stream.getvalue()
    folder = _directory(toolkit, create=True)
    index = 1 + max((int(path.stem.removeprefix("reading_")) for path in folder.glob("reading_*.json")
        if _READING_ID.fullmatch(path.stem)), default=0)
    while True:
        reading_id = f"reading_{index:04d}"
        record_path, overlay_path = folder / f"{reading_id}.json", folder / f"{reading_id}_overlay.png"
        if overlay_path.exists():
            index += 1
            continue
        record = {"schema_version": SCHEMA, "reading_id": reading_id, "image": name,
            "image_sha256": image_sha, "image_size_px": list(original.size), "facade": facade,
            "submitted_json": reading_json, "submitted_sha256": hashlib.sha256(reading_json.encode()).hexdigest(),
            "submitted": submitted, "normalization": normalization,
            "replaces_reading_id": replaces_reading_id or None,
            "artifact": artifact, "artifact_sha256": artifact["artifact_sha256"],
            "z_calibration_evidence": z_evidence, "adopted_openings": openings, "levels": levels,
            "issues": issues, "independently_verified": False, "verification_notice": _NOTICE,
            "overlay_file": overlay_path.relative_to(toolkit.run).as_posix(),
            "overlay_sha256": hashlib.sha256(overlay_bytes).hexdigest(),
            "overlay_basis": "adopted width about calibrated x_px centre; adopted absolute sill/head/level Z"}
        record["record_sha256"] = _hash(record)
        try:
            with record_path.open("xb") as saved:
                with overlay_path.open("xb") as picture:
                    picture.write(overlay_bytes)
                saved.write(_bytes(record) + b"\n")
            break
        except FileExistsError:
            index += 1
    metadata = {key: record[key] for key in ("reading_id", "image", "image_sha256", "facade",
        "artifact_sha256", "overlay_file", "independently_verified", "verification_notice", "overlay_basis", "replaces_reading_id")}
    metadata.update(reading_file=record_path.relative_to(toolkit.run).as_posix(),
        reading_file_sha256=hashlib.sha256(record_path.read_bytes()).hexdigest(),
        normalization=normalization, counts=artifact["counts"], adopted_openings=openings[:TABLE_LIMIT], levels=levels[:TABLE_LIMIT],
        issues=issues[:TABLE_LIMIT], opening_count=len(openings), level_count=len(levels), issue_count=len(issues),
        omitted_rows={"adopted_openings": max(0, len(openings) - TABLE_LIMIT),
            "levels": max(0, len(levels) - TABLE_LIMIT), "issues": max(0, len(issues) - TABLE_LIMIT)})
    from scripts.tool_scripts.bim_agent_replies import compact_reply
    metadata = compact_reply(toolkit.run, "prepare_elevation_reading", metadata, full_result=record, force=True)
    toolkit.log("prepare_elevation_reading", metadata)
    return [original, overlay], metadata


def load_reading(toolkit, reading_id):
    """Load and verify one immutable reading, its adopted artifact and original."""
    if not isinstance(reading_id, str) or not _READING_ID.fullmatch(reading_id):
        raise ValueError("choose a reading_id returned by prepare_elevation_reading")
    folder = _directory(toolkit)
    path = folder / f"{reading_id}.json"
    if path.resolve().parent != folder.resolve() or not path.is_file():
        raise ValueError("saved drawing reading is missing or outside this run")
    record = json.loads(path.read_bytes(), parse_constant=_reject_constant)
    if (record.get("schema_version") != SCHEMA or record.get("reading_id") != reading_id
            or record.get("record_sha256") != _hash({k: v for k, v in record.items() if k != "record_sha256"})):
        raise ValueError("saved drawing reading hash or identity mismatch")
    original = toolkit.image_path(record["image"])
    if hashlib.sha256(original.read_bytes()).hexdigest() != record["image_sha256"]:
        raise ValueError("saved drawing reading original image hash mismatch")
    artifact = validate_elevation_artifact(record["artifact"], image_name=record["image"])
    if artifact["artifact_sha256"] != record["artifact_sha256"] or artifact["orientation"] != record["facade"]:
        raise ValueError("saved drawing reading artifact hash or facade mismatch")
    submitted_json = record["submitted_json"]
    if (hashlib.sha256(submitted_json.encode()).hexdigest() != record["submitted_sha256"]
            or json.loads(submitted_json, parse_constant=_reject_constant) != record["submitted"]):
        raise ValueError("saved drawing reading original submission hash mismatch")
    overlay = toolkit.run / record["overlay_file"]
    if (overlay.resolve().parent != folder.resolve()
            or overlay.resolve() != (folder / f"{reading_id}_overlay.png").resolve()
            or hashlib.sha256(overlay.read_bytes()).hexdigest() != record["overlay_sha256"]):
        raise ValueError("saved drawing reading overlay hash mismatch")
    return record


def compare_reading(toolkit, reading_id, candidate):
    """Persist an existing-matcher comparison and return bounded diagnostics."""
    record = load_reading(toolkit, reading_id)
    source_path = toolkit.candidate_path(candidate) / "source_model.json"
    if source_path.resolve().parent.parent != toolkit.run.resolve():
        raise ValueError("candidate source must stay inside this run")
    source = json.loads(source_path.read_bytes(), parse_constant=_reject_constant)
    if source.get("source_model_sha256") != _hash({k: v for k, v in source.items() if k != "source_model_sha256"}):
        raise ValueError("candidate source model hash mismatch")
    match = match_elevation(source, record["artifact"], candidate=candidate)
    heights, issues = [], []
    source_floors = {row.get("floor_id") for row in source.get("spaces", [])}
    for floor in sorted({row["floor_id"] for row in record["artifact"]["counts"]} - source_floors):
        issues.append({"type": "missing_floor_in_bim", "floor_id": floor})
    floors = {row["id"]: row for row in source.get("floors", [])}
    for level in record["artifact"]["elevations"]:
        if level["kind"] != "floor":
            continue
        floor = floors.get(level["floor_id"])
        if floor is None:
            issues.append({"type": "missing_floor_level_in_bim", "floor_id": level["floor_id"]})
        elif abs(floor["z_floor"] - level["value_m"]) > 1e-6:
            issues.append({"type": "floor_level_difference", "floor_id": level["floor_id"],
                "adopted_level_m": level["value_m"], "source_level_m": floor["z_floor"]})
    for field, kind in (("elevation_only", "missing_in_bim"), ("source_only", "extra_in_bim"), ("conflicts", "match_conflict")):
        issues.extend({"type": kind, "detail": row} for row in match[field])
    by_id = {row["id"]: row for row in source["openings"]}
    for row in match["matches"]:
        vertices = by_id[row["source_opening_id"]]["vertices"]
        sill, head = min(p[2] for p in vertices), max(p[2] for p in vertices)
        differences = {"artifact_opening_id": row["artifact_opening_id"],
            "source_opening_id": row["source_opening_id"], "floor_id": row["floor_id"],
            "adopted_sill_m": row["sill_m"], "adopted_head_m": row["head_m"],
            "source_sill_m": sill, "source_head_m": head,
            "sill_difference_m": row["sill_m"] - sill, "head_difference_m": row["head_m"] - head}
        heights.append(differences)
        if max(abs(differences[key]) for key in ("sill_difference_m", "head_difference_m")) > 1e-6:
            issues.append({"type": "height_difference", **differences})
    for row in match["position_comparisons"]:
        if row["max_difference_m"] > 0.30 + 1e-9:
            issues.append({"type": "horizontal_difference_over_30cm", **{key: row[key] for key in (
                "artifact_opening_id", "source_opening_id", "floor_id", "max_difference_m", "bucket")}})
    for row in match["counts"]:
        if not row["matches"]:
            issues.append({"type": "count_difference", **row})
    current_consistent = not issues and len(match["matches"]) == len(record["artifact"]["openings"])
    result = {"schema_version": "drawing_elevation_comparison_v1", "reading_id": reading_id,
        "image": record["image"], "facade": record["facade"], "artifact_sha256": record["artifact_sha256"],
        "candidate": candidate, "source_model_sha256": source["source_model_sha256"],
        "current_consistent": current_consistent, "match": match, "height_differences": heights,
        "issues": issues, "independently_verified": False, "verification_notice": _NOTICE,
        "consistency_scope": "saved reading against current source; horizontal differences 10-30cm retain the plan and remain in match.position_comparisons"}
    raw = _bytes(result) + b"\n"
    digest = hashlib.sha256(raw).hexdigest()
    path = _directory(toolkit) / f"{reading_id}_comparison_{digest[:24]}.json"
    try:
        with path.open("xb") as stream:
            stream.write(raw)
    except FileExistsError:
        if path.read_bytes() != raw:
            raise ValueError("saved drawing comparison changed")
    summary = {**{key: result[key] for key in ("reading_id", "image", "facade", "artifact_sha256", "candidate",
        "source_model_sha256", "current_consistent", "independently_verified", "verification_notice", "consistency_scope")},
        "comparison_file": path.relative_to(toolkit.run).as_posix(), "comparison_sha256": digest,
        "matched_count": len(match["matches"]), "counts": match["counts"], "issues": issues[:TABLE_LIMIT],
        "issue_count": len(issues), "omitted_issue_count": max(0, len(issues) - TABLE_LIMIT),
        "height_difference_count": sum(row["type"] == "height_difference" for row in issues)}
    from scripts.tool_scripts.bim_agent_replies import compact_reply
    return compact_reply(toolkit.run, "compare_elevation_reading", summary, full_result=result, force=True)
