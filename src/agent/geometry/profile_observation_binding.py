"""Resolve explicitly selected pixel-profile candidates in observations/plans.

This module only binds an explicitly selected candidate coordinate.  It does
not infer drawing entities, reorder openings, or validate the facade comparison
schema; those responsibilities remain with the caller and comparison module.
"""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Callable
from typing import Any


_PROFILE_ID = re.compile(r"profile_[0-9]{3,}\Z")
_REFERENCE_FIELDS = frozenset({"profile", "candidate", "at"})
_REFERENCE_POINTS = frozenset({"peak", "start", "end"})


def resolve_plan_pixels(raw: dict, *, image: str, image_sha256: str,
                        load_profile: Callable[[str], dict]) -> tuple[dict, list]:
    """Bind pixel slots only; retain world dimensions and all other declarations.

    A midpoint is arithmetic on two caller-selected references, not recognition
    of a wall or a choice of its representative plane. Geometry validation stays
    with the existing compiler. No coordinate is snapped or inferred.
    """
    if not isinstance(raw, dict):
        raise ValueError("plan must be an object")
    resolved = copy.deepcopy(raw)
    views = {axis: dict(image=image, sha256=image_sha256, axis=axis) for axis in ("x", "y")}
    cache = {}
    bindings = []

    def slot(container, index, path, axis):
        value = container[index]
        if not isinstance(value, dict):
            return

        def reference(item, location):
            if not isinstance(item, dict):
                raise ValueError(f"{location} must be a profile candidate reference")
            return _resolve_reference(item, slot=location, view_name=axis,
                                      load_profile=load_profile, views=views, cache=cache)

        if "midpoint" in value:
            pair = value["midpoint"]
            if set(value) != {"midpoint"} or not isinstance(pair, list) or len(pair) != 2:
                raise ValueError(f"{path} midpoint requires exactly two profile references")
            ends = [reference(item, f"{path}.midpoint[{i}]") for i, item in enumerate(pair)]
            pixel = ends[0][0] / 2 + ends[1][0] / 2
            binding = dict(slot=path, operation="midpoint", resolved_pixel=pixel,
                           image=image, image_sha256=image_sha256, axis=axis,
                           endpoints=[item[1] for item in ends])
        else:
            pixel, binding = reference(value, path)
        container[index] = pixel
        bindings.append(binding)

    def point(value, path):
        if isinstance(value, list):
            for i, axis in enumerate(("x", "y")[:len(value)]):
                slot(value, i, f"{path}[{i}]", axis)

    for axis in ("x", "y"):
        anchors = resolved.get(f"{axis}_anchors")
        for i, anchor in enumerate(anchors if isinstance(anchors, list) else []):
            if isinstance(anchor, list) and anchor:
                slot(anchor, 0, f"{axis}_anchors[{i}][0]", axis)
    footprint = resolved.get("footprint_pixels")
    for i, value in enumerate(footprint if isinstance(footprint, list) else []):
        point(value, f"footprint_pixels[{i}]")
    for collection, fields in (("partitions", ("points",)),
                               ("openings", ("p1", "p2")), ("space_seeds", ("point",))):
        rows = resolved.get(collection)
        for i, row in enumerate(rows if isinstance(rows, list) else []):
            if not isinstance(row, dict):
                continue
            for field in fields:
                path = f"{collection}[{i}].{field}"
                value = row.get(field)
                if field == "points" and isinstance(value, list):
                    for j, vertex in enumerate(value):
                        point(vertex, f"{path}[{j}]")
                else:
                    point(value, path)
    return resolved, bindings


def _finite_number(value: object, label: str) -> int | float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value)):
        raise ValueError(f"{label} must be a finite number")
    return value


def _load_cached_profile(
    profile_id: str,
    *,
    load_profile: Callable[[str], dict],
    cache: dict[str, tuple[dict, str]],
) -> tuple[dict, str]:
    cached = cache.get(profile_id)
    if cached is not None:
        return cached

    loaded = load_profile(profile_id)
    if not isinstance(loaded, dict):
        raise ValueError(f"{profile_id} loader result must be an object")
    if not {"record", "sha256"}.issubset(loaded):
        raise ValueError(
            f"{profile_id} loader result must contain record and sha256")
    record = loaded["record"]
    profile_sha256 = loaded["sha256"]
    if not isinstance(record, dict):
        raise ValueError(f"{profile_id} record must be an object")
    if not isinstance(profile_sha256, str) or not profile_sha256:
        raise ValueError(f"{profile_id} sha256 must be nonempty text")
    cache[profile_id] = (record, profile_sha256)
    return record, profile_sha256


def _resolve_reference(
    reference: dict,
    *,
    slot: str,
    view_name: str,
    load_profile: Callable[[str], dict],
    views: dict,
    cache: dict[str, tuple[dict, str]],
) -> tuple[int | float, dict]:
    unknown = set(reference) - _REFERENCE_FIELDS
    if unknown:
        raise ValueError(
            f"{slot} profile reference has unknown fields: {sorted(unknown)}")
    missing = {"profile", "candidate"} - set(reference)
    if missing:
        raise ValueError(
            f"{slot} profile reference is missing fields: {sorted(missing)}")

    profile_id = reference["profile"]
    candidate_id = reference["candidate"]
    point = reference.get("at", "peak")
    if not isinstance(profile_id, str) or not _PROFILE_ID.fullmatch(profile_id):
        raise ValueError(
            f"{slot}.profile must match profile_ followed by at least three digits")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ValueError(f"{slot}.candidate must be nonempty text")
    if not isinstance(point, str) or point not in _REFERENCE_POINTS:
        raise ValueError(f"{slot}.at must be one of peak, start, or end")

    record, profile_sha256 = _load_cached_profile(
        profile_id, load_profile=load_profile, cache=cache)
    view = views.get(view_name) if isinstance(views, dict) else None
    if not isinstance(view, dict):
        raise ValueError(f"views.{view_name} must be an object")

    expected_image = view.get("image")
    expected_axis = view.get("axis")
    expected_image_sha256 = view.get("sha256")
    if not isinstance(expected_image, str) or not expected_image:
        raise ValueError(f"views.{view_name}.image must be nonempty text")
    if expected_axis not in {"x", "y"}:
        raise ValueError(f"views.{view_name}.axis must be x or y")
    if not isinstance(expected_image_sha256, str) or not expected_image_sha256:
        raise ValueError(f"views.{view_name}.sha256 must be nonempty text")
    if record.get("name") != expected_image:
        raise ValueError(
            f"{slot} profile image does not match views.{view_name}.image")
    if record.get("axis") != expected_axis:
        raise ValueError(
            f"{slot} profile axis does not match views.{view_name}.axis")
    if record.get("image_sha256") != expected_image_sha256:
        raise ValueError(
            f"{slot} profile image sha256 does not match views.{view_name}.sha256")

    candidates = record.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError(f"{profile_id}.candidates must be a list")
    matches = [candidate for candidate in candidates
               if isinstance(candidate, dict) and candidate.get("id") == candidate_id]
    if len(matches) != 1:
        raise ValueError(
            f"{slot} candidate {candidate_id!r} must exist exactly once in {profile_id}")

    candidate = matches[0]
    pixels = candidate.get("pixels")
    if not isinstance(pixels, list) or len(pixels) != 2:
        raise ValueError(f"{profile_id}.{candidate_id}.pixels must be [lo, hi]")
    start = _finite_number(pixels[0], f"{profile_id}.{candidate_id}.pixels[0]")
    end = _finite_number(pixels[1], f"{profile_id}.{candidate_id}.pixels[1]")
    peak = _finite_number(candidate.get("peak"), f"{profile_id}.{candidate_id}.peak")
    if start > end:
        raise ValueError(f"{profile_id}.{candidate_id}.pixels must be ordered lo to hi")
    if not start <= peak <= end:
        raise ValueError(f"{profile_id}.{candidate_id}.peak must lie within its pixel band")

    resolved_pixel = {"peak": peak, "start": start, "end": end}[point]
    binding = {
        "slot": slot,
        "reference": copy.deepcopy(reference),
        "resolved_pixel": resolved_pixel,
        "profile_sha256": profile_sha256,
        "image_sha256": expected_image_sha256,
        "image": expected_image,
        "axis": expected_axis,
        "candidate": {
            "id": candidate_id,
            "pixels": [start, end],
            "peak": peak,
            "selected_at": point,
        },
    }
    return resolved_pixel, binding


def resolve_observations(
    raw: dict,
    *,
    load_profile: Callable[[str], dict],
    views: dict,
) -> tuple[dict, list]:
    """Replace explicit pixel-profile references in facade coordinate slots.

    Legacy numeric coordinates and all unrelated metadata are copied unchanged.
    Profile files are supplied by ``load_profile`` so this function performs no
    filesystem access.  Each profile ID is loaded at most once per invocation.
    """
    if not isinstance(raw, dict):
        raise ValueError("observations must be an object")
    resolved = copy.deepcopy(raw)
    bindings: list[dict[str, Any]] = []
    cache: dict[str, tuple[dict, str]] = {}

    def resolve_slot(container: list, index: int, slot: str, view_name: str) -> None:
        value = container[index]
        if not isinstance(value, dict):
            return
        pixel, binding = _resolve_reference(
            value,
            slot=slot,
            view_name=view_name,
            load_profile=load_profile,
            views=views,
            cache=cache,
        )
        container[index] = pixel
        bindings.append(binding)

    for view_name in ("plan", "elevation"):
        view_observations = resolved.get(view_name)
        if not isinstance(view_observations, dict):
            continue

        anchors = view_observations.get("axis_anchors")
        if isinstance(anchors, list):
            for anchor_index, anchor in enumerate(anchors):
                if isinstance(anchor, list) and anchor:
                    resolve_slot(
                        anchor, 0,
                        f"{view_name}.axis_anchors[{anchor_index}][0]",
                        view_name,
                    )

        openings = view_observations.get("openings")
        if isinstance(openings, list):
            for opening_index, opening in enumerate(openings):
                if not isinstance(opening, dict):
                    continue
                pixels = opening.get("pixels")
                if not isinstance(pixels, list):
                    continue
                for pixel_index in range(min(2, len(pixels))):
                    resolve_slot(
                        pixels, pixel_index,
                        f"{view_name}.openings[{opening_index}].pixels[{pixel_index}]",
                        view_name,
                    )

    return resolved, bindings


def resolve_pixel_slots(values: list, *, view: dict, load_profile: Callable) -> tuple[list, list]:
    """Resolve arbitrary original-pixel slots using the same audited profile contract."""
    cache, resolved, bindings = {}, [], []
    for index, value in enumerate(values):
        if isinstance(value, dict):
            value, binding = _resolve_reference(
                value, slot=f"pixels[{index}]", view_name="image", load_profile=load_profile,
                views={"image": view}, cache=cache)
            bindings.append(binding)
        else:
            value = _finite_number(value, f"pixels[{index}]")
        resolved.append(value)
    return resolved, bindings
