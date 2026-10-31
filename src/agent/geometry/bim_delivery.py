"""Truthful, source-bound delivery summaries for viewable source BIMs.

The summary deliberately reports what has and has not been reviewed.  It does
not inspect drawings, infer missing geometry, or turn a self-consistent source
model into a drawing-fidelity verdict.
"""
from __future__ import annotations

import copy
from typing import Any

from .opening_review import facade_inventory, opening_inventory


_KINDS = ("door", "passage", "window")
_CONSISTENT = "consistent_with_supplied_observations"
_FOLLOW_UP = "observations_require_follow_up"


def _as_list(value: Any, field: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"source {field} must be a list when present")
    return copy.deepcopy(value)


def _review_ref(review: dict, index: int, *, state: str, reason: str | None = None) -> dict:
    """Keep the useful, stable identity of a review without copying its body."""
    scope = review.get("review_scope")
    image = review.get("image")
    findings = review.get("findings", [])
    reference = review.get("review_file")
    if not isinstance(reference, str) or not reference:
        reference = f"reviews[{index}]"
    result = {
        "review_ref": reference,
        "review_index": index,
        "state": state,
        "source_model_sha256": review.get("source_model_sha256"),
        "floor_id": scope.get("floor_id") if isinstance(scope, dict) else None,
        "kind": scope.get("kind") if isinstance(scope, dict) else None,
        "coverage": scope.get("coverage") if isinstance(scope, dict) else None,
        "image": copy.deepcopy(image) if isinstance(image, dict) else None,
        "conclusion": review.get("conclusion"),
        "finding_codes": sorted(
            row["code"] for row in findings
            if isinstance(row, dict) and isinstance(row.get("code"), str)
        ),
    }
    if isinstance(scope, dict) and isinstance(scope.get("facade"), str):
        result["facade"] = scope["facade"]
    if "location_check" in review:
        result["location_check"] = copy.deepcopy(review["location_check"])
    if reason is not None:
        result["stale_reason"] = reason
    return result


def _image_identity(ref: dict) -> tuple[str, str] | None:
    image = ref.get("image")
    if not isinstance(image, dict):
        return None
    sha256 = image.get("sha256")
    if isinstance(sha256, str) and sha256:
        return ("sha256", sha256)
    name = image.get("name")
    if isinstance(name, str) and name:
        return ("name", name)
    return None


def _scope_status(effective_complete: list[dict], partial: list[dict]) -> str:
    """Return only statuses with an explicit meaning to delivery consumers."""
    # A partial result is useful but cannot establish coverage by itself.
    if not effective_complete:
        return "partial" if partial else "not_reviewed"
    # A current partial from another image cannot erase a complete coverage
    # claim.  Its findings are nevertheless retained in scope findings below.
    if any(ref.get("conclusion") != _CONSISTENT or ref["finding_codes"]
           for ref in effective_complete):
        return _FOLLOW_UP
    # ``partial_review_not_complete`` records a deliberately limited scope;
    # it does not contradict a complete, consistent review already available
    # for this source scope.  Any other partial finding remains a risk.
    if any(set(ref["finding_codes"]) - {"partial_review_not_complete"}
           for ref in partial):
        return _FOLLOW_UP
    return _CONSISTENT


def _effective_scope_refs(refs: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Apply replacement only within one exact scope and one identical image."""
    complete = [ref for ref in refs if ref["coverage"] == "complete"]
    partial = [ref for ref in refs if ref["coverage"] == "partial"]
    latest_complete_by_image: dict[tuple[str, str] | tuple[str, int], dict] = {}
    for ref in complete:
        image_key = _image_identity(ref) or ("review_index", ref["review_index"])
        latest_complete_by_image[image_key] = ref
    effective_complete = sorted(latest_complete_by_image.values(), key=lambda row: row["review_index"])
    effective_by_image = {
        _image_identity(ref) or ("review_index", ref["review_index"]): ref
        for ref in effective_complete
    }
    superseded = []
    for ref in refs:
        image_key = _image_identity(ref) or ("review_index", ref["review_index"])
        replacement = effective_by_image.get(image_key)
        if replacement is None or ref is replacement:
            continue
        if ref["coverage"] == "complete" or ref["review_index"] < replacement["review_index"]:
            superseded.append(ref)
    return effective_complete, [ref for ref in partial if ref not in superseded], superseded


def summarize_delivery(source: dict, reviews: list[dict]) -> dict:
    """Summarize a source BIM and only reviews bound to that exact source.

    ``reviews`` is ordered: a later list item is a later submitted review.  A
    later *complete* review of the same source, floor/kind scope, and image
    explicitly supersedes earlier records for that same image.  Complete
    reviews from other images remain independently effective; partial reviews
    never supersede a complete review and never establish full coverage.

    This is intentionally a delivery description, not a global pass/fail gate:
    a geometrically valid source with no image review remains viewable and is
    reported as ``not_reviewed``.
    """
    if not isinstance(reviews, list) or any(not isinstance(review, dict) for review in reviews):
        raise ValueError("reviews must be a list of review objects")

    inventory = opening_inventory(source)
    facade_index = facade_inventory(source)
    source_hash = inventory["source_model_sha256"]
    current_reviews: list[dict] = []
    stale_reviews: list[dict] = []
    for index, review in enumerate(reviews):
        if review.get("stale_reason"):
            stale_reviews.append(_review_ref(review, index, state="stale", reason=review["stale_reason"]))
        elif review.get("source_model_sha256") != source_hash:
            reason = ("source_model_sha256_missing" if "source_model_sha256" not in review
                      else "source_model_sha256_mismatch")
            stale_reviews.append(_review_ref(review, index, state="stale", reason=reason))
        else:
            current_reviews.append(_review_ref(review, index, state="current"))

    # Only a valid scope can influence the status of source opening scopes.
    scope_refs: dict[tuple[str, str, str | None], list[dict]] = {}
    for ref in current_reviews:
        key = (ref["floor_id"], ref["kind"], ref.get("facade"))
        if key[0] is not None and key[1] in _KINDS:
            scope_refs.setdefault(key, []).append(ref)

    opening_review_scopes = []
    facade_review_scopes = []
    facade_floor_rows = {row["floor_id"]: row for row in facade_index["floors"]}
    for floor in inventory["floors"]:
        floor_id = floor["floor_id"]
        counts = floor["counts"]
        facade_floor = facade_floor_rows[floor_id]
        available_facades = [row["facade"] for row in facade_floor["facades"]]
        for kind in _KINDS:
            # A review with no facade is the original plan-style whole
            # floor/kind claim.  It keeps its old behavior exactly; facade
            # reviews are assembled separately below.
            refs = scope_refs.get((floor_id, kind, None), [])
            effective_complete, effective_partial, superseded = _effective_scope_refs(refs)
            facade_rows = []
            for facade in available_facades:
                facade_refs = scope_refs.get((floor_id, kind, facade), [])
                facade_complete, facade_partial, facade_superseded = _effective_scope_refs(facade_refs)
                built_ids = [opening_id for opening_id in floor["opening_ids"]
                             if (facade_index["opening_classifications"][opening_id].get("facade") == facade and
                                 next(row["kind"] for row in floor["openings"] if row["id"] == opening_id) == kind)]
                row = {
                    "floor_id": floor_id, "kind": kind, "facade": facade,
                    "exterior_boundary_ids": next(item["exterior_boundary_ids"] for item in facade_floor["facades"]
                                                   if item["facade"] == facade),
                    "built_count": len(built_ids), "built_opening_ids": built_ids,
                    "review_status": _scope_status(facade_complete, facade_partial),
                    "current_review_refs": [ref["review_ref"] for ref in facade_refs],
                    "effective_complete_review_refs": [ref["review_ref"] for ref in facade_complete],
                    "partial_review_refs": [ref["review_ref"] for ref in facade_partial],
                    "superseded_review_refs": [ref["review_ref"] for ref in facade_superseded],
                    "finding_codes": sorted({code for ref in [*facade_complete, *facade_partial]
                                               for code in ref["finding_codes"]}),
                }
                facade_rows.append(row)
                facade_review_scopes.append(row)
            unavailable_facade_refs = [ref for key, candidates in scope_refs.items()
                                       if key[:2] == (floor_id, kind) and key[2] is not None and key[2] not in available_facades
                                       for ref in candidates]
            non_facade_openings = [row for row in facade_floor["non_facade_openings"] if row["kind"] == kind]
            unsupported_exterior_boundaries = facade_floor["unsupported_exterior_boundaries"]
            facade_has_current = any(row["current_review_refs"] for row in facade_rows) or unavailable_facade_refs
            facade_findings = {code for row in facade_rows for code in row["finding_codes"]}
            facade_findings.update(code for ref in unavailable_facade_refs for code in ref["finding_codes"])
            facade_statuses = [row["review_status"] for row in facade_rows]
            if effective_complete:
                review_status = _scope_status(effective_complete, effective_partial)
                # A material contradiction in an independent facade image is
                # retained instead of being hidden by a plan review.
                if (any(status == _FOLLOW_UP for status in facade_statuses) or
                        any(code != "partial_review_not_complete" for code in facade_findings)):
                    review_status = _FOLLOW_UP
            elif any(status == _FOLLOW_UP for status in facade_statuses) or unavailable_facade_refs:
                review_status = _FOLLOW_UP
            elif effective_partial:
                # Preserve the original plan-review meaning: a partial record
                # is useful evidence, but it cannot establish consistency.
                review_status = "partial"
            elif (facade_rows and all(status == _CONSISTENT for status in facade_statuses) and
                  not non_facade_openings and not unsupported_exterior_boundaries):
                # Every exterior direction is required, even directions with
                # no built opening.  An empty complete review is the evidence
                # that a missing whole facade has not been silently accepted.
                review_status = _CONSISTENT
            elif facade_has_current:
                review_status = "partial"
            else:
                review_status = "not_reviewed"
            finding_codes = sorted({code for ref in [*effective_complete, *effective_partial]
                                    for code in ref["finding_codes"]} | facade_findings)
            all_refs = [*refs, *[ref for row in facade_rows for ref in
                                  scope_refs.get((floor_id, kind, row["facade"]), [])], *unavailable_facade_refs]
            opening_review_scopes.append({
                "floor_id": floor_id,
                "kind": kind,
                "built_count": counts[kind],
                "built_opening_ids": [
                    opening_id for opening_id in floor["opening_ids"]
                    if next(row["kind"] for row in floor["openings"] if row["id"] == opening_id) == kind
                ],
                "review_status": review_status,
                "current_review_refs": [ref["review_ref"] for ref in all_refs],
                "effective_complete_review_refs": [ref["review_ref"] for ref in effective_complete],
                "partial_review_refs": [ref["review_ref"] for ref in effective_partial],
                "superseded_review_refs": [ref["review_ref"] for ref in superseded],
                "finding_codes": finding_codes,
                "facade_coverage": {
                    "required_facades": available_facades,
                    "facade_scope_refs": [row["current_review_refs"] for row in facade_rows],
                    "non_facade_openings": copy.deepcopy(non_facade_openings),
                    "unsupported_exterior_boundaries": copy.deepcopy(unsupported_exterior_boundaries),
                    "unavailable_facade_review_refs": [ref["review_ref"] for ref in unavailable_facade_refs],
                },
            })

    generation = source.get("generation", {})
    if generation is None:
        generation = {}
    if not isinstance(generation, dict):
        raise ValueError("source generation must be an object when present")
    counts = {
        name: len(_as_list(source.get(name), name))
        for name in ("spaces", "boundaries", "openings", "connections", "unbuilt_openings", "unsupported")
    }
    return {
        "schema_version": "bim_delivery_v1",
        "source_model_sha256": source_hash,
        "source_validation": copy.deepcopy(source.get("validation")),
        "wall_dimension_report": copy.deepcopy(source.get("wall_dimension_report")),
        "counts": counts,
        "unbuilt_openings": _as_list(source.get("unbuilt_openings"), "unbuilt_openings"),
        "assumptions": _as_list(source.get("assumptions"), "assumptions"),
        "generation": {"unresolved": _as_list(generation.get("unresolved"), "generation.unresolved")},
        "opening_inventory": inventory,
        "facade_inventory": facade_index,
        "opening_review_scopes": opening_review_scopes,
        "facade_review_scopes": facade_review_scopes,
        "current_reviews": current_reviews,
        "stale_reviews": stale_reviews,
        "drawing_fidelity": "not_evaluated",
    }
