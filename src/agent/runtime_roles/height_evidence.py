"""Carry reader coordinates into the existing located-height report.

No BIM coordinates are fitted here. Level bands and image scale come from the
immutable reading; source regions get the *existing matcher*'s horizontal
tolerance and the level bands' vertical uncertainty, not a box reverse-fitted
to the candidate. The original boxes and every derivation remain in the receipt.
"""

from __future__ import annotations

import hashlib
import json
import math
from itertools import combinations
from pathlib import Path

from src.agent_runtime.store import json_bytes

IMAGE_EVIDENCE = {"annotation", "annotation_and_pixels", "pixels", "visual_estimate"}


def _review_id(record):
    return hashlib.sha256(json_bytes(record)).hexdigest()


def _reader_image(session, artifact, task_id):
    name = artifact["image"]
    info = session.manifest["images"][name]
    task = session.registry.task(task_id)
    raw = (session.run_directory / "images" / name).read_bytes()
    if (hashlib.sha256(raw).hexdigest() != info["sha256"]
            or task["input_sha256"] != info["sha256"]):
        raise ValueError("height evidence original changed since reader admission")
    return name, info


def _persist_reader_evidence(session, *, task_id, artifact, bindings, location=None):
    """Save a hash-addressed receipt for reader rows already verified by registry."""
    record_ref = session.registry.records[task_id]["artifact"]
    name, info = _reader_image(session, artifact, task_id)
    record = {
        "schema_version": "role_height_artifact_evidence_v2",
        "image": name,
        "image_sha256": info["sha256"],
        "facade": artifact["orientation"],
        "reader_task_id": task_id,
        "reader_artifact_sha256": record_ref["sha256"],
        "normalized_artifact_sha256": artifact["artifact_sha256"],
        "bindings": sorted(bindings, key=lambda row: (
            row["source_opening_id"], row["artifact_opening_id"])),
        "basis": (
            "Immutable delivered elevation-reader artifact row, exact original-image bbox, "
            "and a one-to-one current source-opening match."
        ),
    }
    if location is not None:
        record["role_height_evidence"] = location
        if location.get("status") == "prepared":
            record["transform"] = location["transform"]
    identity = _review_id(record)
    folder = session.run_directory / "elevation_reviews"
    folder.mkdir(exist_ok=True)
    existing = [json.loads(path.read_bytes()) for path in sorted(folder.glob("review_*.json"))]
    prior = next((row for row in existing if row.get("role_evidence_id") == identity), None)
    if prior is not None:
        if {key: value for key, value in prior.items() if key != "role_evidence_id"} != record:
            raise ValueError("saved role height evidence changed")
    else:
        index = len(existing) + 1
        while True:
            try:
                with (folder / f"review_{index:04d}.json").open(
                    "x", encoding="utf-8", newline="\n"
                ) as stream:
                    stream.write(json.dumps(
                        {**record, "role_evidence_id": identity},
                        ensure_ascii=False, indent=2,
                    ) + "\n")
                break
            except FileExistsError:
                index += 1
    return identity


def _binding(artifact, matched):
    by_id = {row["id"]: row for row in artifact["openings"]}
    row = by_id[matched["artifact_opening_id"]]
    return {
        "artifact_opening_id": row["id"],
        "source_opening_id": matched["source_opening_id"],
        "kind": "window" if row["kind"] == "window" else "opening",
        "floor_id": row["floor_id"],
        "image": artifact["image"],
        "original_bbox": row["bbox"],
        "height_m": [row["sill_m"], row["head_m"]],
        "evidence_type": row["evidence_type"],
    }


def _entry_target(entry):
    objects = entry.get("claim", {}).get("objects", [])
    if len(objects) != 1:
        raise ValueError("reader height entry must target exactly one opening")
    return objects[0]


def _attach_reference(entry, *, evidence_id, task_id, artifact_sha256, binding):
    source = entry["claim"]["sources"][0]
    source.update(image=binding["image"], box=list(binding["original_bbox"]))
    entry["reader_evidence"] = {
        "evidence_id": evidence_id,
        "task_id": task_id,
        "artifact_sha256": artifact_sha256,
        "artifact_opening_id": binding["artifact_opening_id"],
    }


def resolve_manual_height_evidence(
    session, candidate, opening_id, sill, head, reader_evidence,
):
    """Resolve an explicit manual edit to one immutable delivered reader row.

    ``reader_evidence`` is exactly ``{task_id, sha256, opening_id}``.  The row,
    height, current BIM object and one-to-one elevation match must all agree.
    The caller can then use the returned source and ``reader_evidence`` member in
    the ordinary atomic role-height entry.
    """
    required = {"task_id", "sha256", "opening_id"}
    if not isinstance(reader_evidence, dict) or set(reader_evidence) != required:
        raise ValueError(f"reader_evidence must contain exactly {sorted(required)}")
    if any(not isinstance(reader_evidence[key], str) or not reader_evidence[key]
           for key in required):
        raise ValueError("reader_evidence values must be nonempty strings")
    task_id = reader_evidence["task_id"]
    artifact_ref = reader_evidence["sha256"]
    artifact = session.registry.read(task_id, sha256=artifact_ref, role_id="elevation_reader")
    from .elevation import match_elevation, validate_elevation_artifact
    artifact = validate_elevation_artifact(artifact)
    rows = [row for row in artifact["openings"] if row["id"] == reader_evidence["opening_id"]]
    if len(rows) != 1:
        raise ValueError("reader_evidence.opening_id must name exactly one delivered opening")
    row = rows[0]
    if row["evidence_type"] not in IMAGE_EVIDENCE:
        raise ValueError("reader opening has no original-image height evidence")
    if [float(sill), float(head)] != [row["sill_m"], row["head_m"]]:
        raise ValueError("manual height must equal the cited reader opening height")
    result = match_elevation(session._source(candidate), artifact, candidate=candidate)
    identity_pairs = [*result["matches"], *[
        row for row in result["conflicts"]
        if row.get("type") == "position_or_width_conflict" and not row.get("ambiguous")
    ]]
    matches = [matched for matched in identity_pairs
               if matched["artifact_opening_id"] == row["id"]
               and matched["source_opening_id"] == opening_id]
    if len(matches) != 1:
        raise ValueError("reader opening is not uniquely matched to the manual height target")
    binding = _binding(artifact, matches[0])
    evidence_id = _persist_reader_evidence(
        session, task_id=task_id, artifact=artifact, bindings=[binding]
    )
    return {
        "image": binding["image"],
        "bbox": list(binding["original_bbox"]),
        "reader_evidence": {
            "evidence_id": evidence_id,
            "task_id": task_id,
            "artifact_sha256": artifact_ref,
            "artifact_opening_id": binding["artifact_opening_id"],
        },
    }


def reader_height_reviews(run):
    """Return only internally consistent receipts created by the role runtime."""
    result = {}
    folder = Path(run) / "elevation_reviews"
    for path in sorted(folder.glob("review_*.json")):
        try:
            row = json.loads(path.read_bytes())
            identity = row.pop("role_evidence_id")
            if row.get("schema_version") != "role_height_artifact_evidence_v2":
                continue
            if identity != _review_id(row):
                continue
            if not isinstance(row.get("bindings"), list):
                continue
            result[identity] = {**row, "role_evidence_id": identity}
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
    return result


def verified_reader_height_reference(run, reference, *, target, value, source):
    """Resolve one persisted reference only when every saved identity agrees."""
    if not isinstance(reference, dict):
        return None
    reviews = reader_height_reviews(run)
    review = reviews.get(reference.get("evidence_id"))
    if review is None:
        return None
    if (reference.get("task_id") != review.get("reader_task_id")
            or reference.get("artifact_sha256") != review.get("reader_artifact_sha256")):
        return None
    matches = [row for row in review["bindings"]
               if row.get("artifact_opening_id") == reference.get("artifact_opening_id")
               and row.get("source_opening_id") == target.get("id")
               and row.get("kind") == target.get("kind")
               and row.get("height_m") == value
               and row.get("image") == source.get("image")
               and row.get("original_bbox") == source.get("box")
               and row.get("evidence_type") in IMAGE_EVIDENCE]
    if len(matches) != 1:
        return None
    return {
        **reference,
        "image": matches[0]["image"],
        "box": matches[0]["original_bbox"],
        "height_m": matches[0]["height_m"],
        "verification": "persisted_delivered_reader_binding",
    }


def delivered_reader_height_index(run, source, candidate):
    """Reconstruct legacy exact bindings from real adjacent reader deliveries.

    This is deliberately strict: the original image, artifact bytes, exact row
    bbox/value and a fresh one-to-one match to the current source must agree.
    Duplicate exact rows are omitted instead of guessed.
    """
    run = Path(run).resolve()
    root = run.parent
    tasks = root / "tasks"
    if not tasks.is_dir():
        return {}
    from .elevation import match_elevation, validate_elevation_artifact

    found = {}
    for record_path in sorted(tasks.glob("*/reader_record.json")):
        try:
            record = json.loads(record_path.read_bytes())
            ref = record["artifact"]
            if (record.get("role_id") != "elevation_reader"
                    or record.get("status") != "completed" or not isinstance(ref, dict)):
                continue
            artifact_path = (root / ref["path"]).resolve()
            if (not artifact_path.is_relative_to(tasks.resolve())
                    or artifact_path.parent != record_path.parent.resolve()):
                continue
            raw = artifact_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
                continue
            artifact = validate_elevation_artifact(json.loads(raw))
            image_path = run / "images" / artifact["image"]
            if (not image_path.is_file()
                    or hashlib.sha256(image_path.read_bytes()).hexdigest() != record["input_sha256"]):
                continue
            report = match_elevation(source, artifact, candidate=candidate)
            by_id = {row["id"]: row for row in artifact["openings"]}
            identity_pairs = [*report["matches"], *[
                row for row in report["conflicts"]
                if row.get("type") == "position_or_width_conflict" and not row.get("ambiguous")
            ]]
            for matched in identity_pairs:
                row = by_id[matched["artifact_opening_id"]]
                if row["evidence_type"] not in IMAGE_EVIDENCE:
                    continue
                kind = "window" if row["kind"] == "window" else "opening"
                key = (
                    kind, matched["source_opening_id"],
                    tuple([row["sill_m"], row["head_m"]]),
                    artifact["image"], tuple(row["bbox"]),
                )
                found.setdefault(key, []).append({
                    "task_id": record["task_id"],
                    "artifact_sha256": ref["sha256"],
                    "artifact_opening_id": row["id"],
                    "image": artifact["image"],
                    "box": row["bbox"],
                    "height_m": [row["sill_m"], row["head_m"]],
                    "verification": "adjacent_delivered_artifact_exact_match",
                })
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
    return {key: rows[0] for key, rows in found.items() if len(rows) == 1}


def derive_z_calibration(
    elevations,
    *,
    max_residual_m=0.10,
    max_half_band_uncertainty_m=0.35,
):
    """Derive image-Y to absolute-Z from saved level lines or horizontal bands.

    This consumes only reader image evidence.  It never fits a candidate BIM,
    GT, or opening heights.  Wide horizontal bands provide their vertical
    centre only when no explicit line_y_px exists; the greatest Z span wins after requiring a
    negative pixel/Z slope and bounded half-band uncertainty.  Every other
    saved horizontal level remains an independent residual check.
    """

    points = []
    for row in elevations or ():
        box = row.get("bbox") if isinstance(row, dict) else None
        value = row.get("value_m") if isinstance(row, dict) else None
        evidence_type = row.get("evidence_type") if isinstance(row, dict) else None
        if (evidence_type not in IMAGE_EVIDENCE
                or not isinstance(box, list) or len(box) != 4
                or not isinstance(value, (int, float)) or isinstance(value, bool)):
            continue
        left, top, right, bottom = (float(item) for item in box)
        if not all(math.isfinite(item) for item in (left, top, right, bottom, float(value))):
            continue
        band_height = bottom - top
        line_y = row.get("line_y_px")
        if line_y is not None:
            if isinstance(line_y, bool) or not isinstance(line_y, (int, float)) or not math.isfinite(line_y):
                continue
        if band_height <= 0 or right <= left or (line_y is None and right - left < 4 * band_height):
            continue
        points.append({
            "id": row.get("id"),
            "kind": row.get("kind"),
            "bbox": [left, top, right, bottom],
            "y_px": float(line_y) if line_y is not None else (top + bottom) / 2,
            "z_m": float(value),
            "half_band_px": 0.0 if line_y is not None else band_height / 2,
            "position_source": "line_y_px" if line_y is not None else "horizontal_band_centre",
        })

    choices = []
    for first, second in combinations(points, 2):
        pixel_delta = second["y_px"] - first["y_px"]
        world_delta = second["z_m"] - first["z_m"]
        if pixel_delta == 0 or world_delta == 0 or pixel_delta * world_delta >= 0:
            continue
        slope = world_delta / pixel_delta
        uncertainty = max(first["half_band_px"], second["half_band_px"]) * abs(slope)
        if uncertainty > max_half_band_uncertainty_m + 1e-9:
            continue
        choices.append((abs(world_delta), abs(pixel_delta), first, second, slope, uncertainty))

    evidence = {
        "status": "missing_two_horizontal_level_bands",
        "method": "furthest_absolute_Z_pair_using_explicit_lines_or_horizontal_bands",
        "points": points,
        "max_abs_residual_m": None,
    }
    if not choices:
        return evidence

    _, _, first, second, slope, uncertainty = max(
        choices, key=lambda item: (item[0], item[1])
    )
    if first["y_px"] > second["y_px"]:
        first, second = second, first
    calibration = {
        "pixel_start": first["y_px"],
        "pixel_end": second["y_px"],
        "world_start_m": first["z_m"],
        "world_end_m": second["z_m"],
    }
    slope = (
        calibration["world_end_m"] - calibration["world_start_m"]
    ) / (calibration["pixel_end"] - calibration["pixel_start"])
    offset = calibration["world_start_m"] - slope * calibration["pixel_start"]
    residuals = []
    for point in points:
        predicted = slope * point["y_px"] + offset
        residuals.append({
            **point,
            "predicted_z_m": predicted,
            "residual_m": predicted - point["z_m"],
        })
    max_residual = max(abs(row["residual_m"]) for row in residuals)
    evidence.update(
        status="derived_from_saved_level_lines",
        calibration=calibration,
        selected=[first, second],
        points=residuals,
        max_abs_residual_m=max_residual,
        selected_max_half_band_uncertainty_m=uncertainty,
    )
    if max_residual > max_residual_m + 1e-9:
        evidence.update(
            status="rejected_inconsistent_saved_level_lines",
            reason="saved horizontal level residual exceeds the accepted limit",
        )
        evidence.pop("calibration", None)
    return evidence


def height_locations(artifact, match, size):
    calibration = artifact["x_calibration"]
    hs = (calibration["world_end_m"] - calibration["world_start_m"]) / (
        calibration["pixel_end"] - calibration["pixel_start"])
    ho = calibration["world_start_m"] - hs * calibration["pixel_start"]
    tolerance = match["tolerances_m"]

    def inside(box):
        return 0 <= box[0] < box[2] <= size[0] and 0 <= box[1] < box[3] <= size[1]

    bands = [row for row in artifact["elevations"] if row["evidence_type"] in IMAGE_EVIDENCE
             and inside(row["bbox"]) and row["bbox"][2] - row["bbox"][0] >= 4 * (row["bbox"][3] - row["bbox"][1])
             and (row["bbox"][3] - row["bbox"][1]) / 2 * abs(hs) <= tolerance["position"]]
    pairs = [(a, b) for a, b in combinations(bands, 2) if a["value_m"] != b["value_m"]
             and (a["bbox"][3] < b["bbox"][1] or b["bbox"][3] < a["bbox"][1])]
    derived = derive_z_calibration(artifact.get("elevations", ()))
    explicit = artifact.get("z_calibration")
    result = {"artifact_sha256": artifact["artifact_sha256"], "x_calibration": calibration,
              "z_calibration_evidence": derived,
              "status": "missing_vertical_frame", "openings": {}}
    aligned_by_id = {
        row.get("id"): row.get("effective_bbox_px", row.get("aligned_bbox_px"))
        for row in artifact.get("ink_alignment", {}).get("openings", ())
        if isinstance(row, dict)
    }

    def opening_box(row):
        candidate = aligned_by_id.get(row["id"])
        if (isinstance(candidate, list) and len(candidate) == 4
                and all(isinstance(value, (int, float)) for value in candidate)):
            return candidate
        candidate = row.get("opening_bbox")
        if isinstance(candidate, list) and len(candidate) == 4:
            return candidate
        # Read compatibility for immutable pre-Q2 submissions.
        return row["bbox"]

    if explicit is not None:
        anchors = []
        zs = (explicit["world_end_m"] - explicit["world_start_m"]) / (
            explicit["pixel_end"] - explicit["pixel_start"])
        zo = explicit["world_start_m"] - zs * explicit["pixel_start"]

        def uncertainty(z):
            return 2.0

        vertical_method = "explicit_z_calibration"
    else:
        if not bands:
            return result
        anchors = list(max(pairs, key=lambda pair: abs(pair[1]["value_m"] - pair[0]["value_m"]))) if pairs else [min(
            bands, key=lambda row: (row["bbox"][3] - row["bbox"][1], row["id"]))]
        ys = [(row["bbox"][1] + row["bbox"][3]) / 2 for row in anchors]
        zs = ((anchors[1]["value_m"] - anchors[0]["value_m"]) / (ys[1] - ys[0])
              if len(anchors) == 2 else -abs(hs))
        zo = anchors[0]["value_m"] - zs * ys[0]

        def uncertainty(z):
            half = [(row["bbox"][3] - row["bbox"][1]) / 2 for row in anchors]
            if len(anchors) == 1:
                return half[0]
            fraction = (z - anchors[0]["value_m"]) / (
                anchors[1]["value_m"] - anchors[0]["value_m"])
            return abs(1 - fraction) * half[0] + abs(fraction) * half[1]

        vertical_method = ("two_observed_level_bands" if len(anchors) == 2
                           else "horizontal_scale_corroborated_by_opening_regions")

    if zs >= 0:
        result["status"] = "inconsistent_vertical_frame"
        return result

    def vertical_fits(row):
        region = opening_box(row)
        pad = max(uncertainty(row["sill_m"]), uncertainty(row["head_m"]))
        return (region[1] - pad - 2 <= (row["head_m"] - zo) / zs
                and (row["sill_m"] - zo) / zs <= region[3] + pad + 2)

    observed = [row for row in artifact["openings"] if row["evidence_type"] in IMAGE_EVIDENCE]
    if explicit is None and len(anchors) == 1 and (
            len(observed) < 2 or not all(vertical_fits(row) for row in observed)):
        result["status"] = "uncorroborated_vertical_scale"
        return result
    if explicit is None and any(not row["bbox"][1] - uncertainty(row["value_m"]) - 2
            <= (row["value_m"] - zo) / zs
            <= row["bbox"][3] + uncertainty(row["value_m"]) + 2 for row in bands):
        result["status"] = "conflicting_level_bands"
        return result
    result.update(status="prepared", vertical_method=vertical_method,
        level_anchors=anchors, z_calibration=explicit,
        transform={"horizontal_metres_per_pixel": hs, "horizontal_offset_m": ho,
                   "z_metres_per_pixel": zs, "z_offset_m": zo})
    by_id = {row["id"]: row for row in artifact["openings"]}
    for matched in match["matches"]:
        row = by_id[matched["artifact_opening_id"]]
        original = row["bbox"]
        located = opening_box(row)
        item = {"source_opening_id": matched["source_opening_id"], "original_bbox": original,
                "aligned_opening_bbox": located,
                "status": "unlocated_reader_region"}
        result["openings"][row["id"]] = item
        if row["evidence_type"] not in IMAGE_EVIDENCE:
            item["status"] = "non_image_evidence"
            continue
        if not inside(located) or not vertical_fits(row):
            continue
        # These are context crops in the unchanged original, not new observations.
        hp = (tolerance["position"] + tolerance["width"] / 2) / abs(hs)
        vp = max(uncertainty(row["sill_m"]), uncertainty(row["head_m"]))
        box = [max(0, math.floor(located[0] - hp)), max(0, math.floor(located[1] - vp)),
               min(size[0], math.ceil(located[2] + hp)), min(size[1], math.ceil(located[3] + vp))]
        if box == [0, 0, *size] or any(other["id"] != row["id"]
                and box[0] <= opening_box(other)[0] and box[1] <= opening_box(other)[1]
                and box[2] >= opening_box(other)[2] and box[3] >= opening_box(other)[3]
                for other in artifact["openings"]):
            item["status"] = "non_unique_reader_region"
            continue
        item.update(status="prepared", source_box=box,
                    padding_pixels={"horizontal": hp, "vertical": vp},
                    horizontal_tolerances_m=tolerance)
    return result


def carry_height_evidence(session, saved_match, artifact, entries):
    """Persist artifact/object bindings before the single atomic height write.

    A usable original opening bbox does not depend on a separate view or a full
    image-to-Z calibration.  Calibration is still retained when available for
    compatibility and visual projection, but the delivered reader row and the
    one-to-one match are the authoritative location binding.
    """
    from .elevation import validate_elevation_artifact

    artifact = validate_elevation_artifact(artifact)
    name, info = _reader_image(session, artifact, saved_match["task_id"])
    location = height_locations(artifact, saved_match["result"], info["size"])
    matched_by_source = {
        matched["source_opening_id"]: matched
        for matched in saved_match["result"]["matches"]
    }
    bindings = []
    entry_bindings = []
    for entry in entries:
        target = _entry_target(entry)
        matched = matched_by_source.get(target["id"])
        if matched is None:
            raise ValueError("height entry has no one-to-one saved reader match")
        binding = _binding(artifact, matched)
        if binding["kind"] != target["kind"]:
            raise ValueError("height entry kind disagrees with its reader match")
        bindings.append(binding)
        entry_bindings.append((entry, binding))
    identity = _persist_reader_evidence(
        session, task_id=saved_match["task_id"], artifact=artifact,
        bindings=bindings, location=location,
    )
    artifact_ref = session.registry.records[saved_match["task_id"]]["artifact"]["sha256"]
    for entry, binding in entry_bindings:
        if binding["evidence_type"] not in IMAGE_EVIDENCE:
            continue
        _attach_reference(
            entry, evidence_id=identity, task_id=saved_match["task_id"],
            artifact_sha256=artifact_ref, binding=binding,
        )
    return {**location, "calibration_id": identity, "artifact_binding_count": len(bindings)}
