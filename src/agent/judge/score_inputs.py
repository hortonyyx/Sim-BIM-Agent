"""Judge-owned score bindings and completeness overlay materialization (B4b A)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from src.agent.correction import facade_convention
from src.agent.correction.facade_applicability import ElevationViewBindingV1
from src.agent.execution.view_manifest import (
    CompletenessAssertion,
    Coverage,
    DatasetSourceRef,
    OpeningEvidence,
    RequiredViewEntry,
    UserSourceRef,
    ViewManifest,
    compute_content_hash,
)
from src.agent.judge.gt_schema import GroundTruthV3

from .score_schema import (
    CLAIM_ORDER,
    ElevationScoreViewBindingV1,
    JudgeCompletenessOverlayV1,
    JudgeScoreViewBindingsV1,
    PlanScoreViewBindingV1,
    ReadingFilteredComponentBasisV1,
    ScoreContractError,
    canonical_sha256,
)

# F-9 route② S1 (2026-08-11): _AXIS/_BASE_SIGN merged into
# `facade_convention` (single, gt-free source; judge is a permitted
# consumer, see that module's docstring and design v2.1 §11).
_FRAME_KEYS = {
    "schema", "input_id", "resolved_building_direction", "source_footprint_fingerprint",
    "world_axis", "sign", "along_origin", "mirrored", "local_x_positive",
}


def frame_transform_preimage(binding: ElevationScoreViewBindingV1) -> dict[str, Any]:
    return {
        "schema": "view_projection_binding_v1", "input_id": binding.input_id,
        "resolved_building_direction": binding.resolved_building_direction,
        "source_footprint_fingerprint": binding.source_footprint_fingerprint,
        "world_axis": binding.world_axis, "sign": binding.sign, "along_origin": binding.along_origin,
        "mirrored": binding.mirrored, "local_x_positive": binding.local_x_positive,
    }


def frame_transform_sha256_from_preimage(preimage: dict[str, Any]) -> str:
    if set(preimage) != _FRAME_KEYS:
        raise ScoreContractError("score_view_binding_invalid", "scoring.view_bindings", context={"preimage_keys": sorted(preimage)})
    # Revalidate the exact nine fields independently of Va's private helper.
    try:
        probe = ElevationScoreViewBindingV1(
            kind="elevation", floor_ids=("frame-proof",), facade_family=preimage["resolved_building_direction"],
            gt_source_view_ids=("frame-proof",), resolution_source="resolved_direction_sidecar",
            orientation_output_hash="0" * 64, adapter_version="frame-proof",
            frame_transform_sha256="0" * 64, **{k: v for k, v in preimage.items() if k != "schema"},
        )
    except ValidationError as exc:
        raise ScoreContractError("score_view_binding_invalid", "scoring.view_bindings") from exc
    return canonical_sha256(frame_transform_preimage(probe))


def frame_transform_sha256(binding: ElevationScoreViewBindingV1) -> str:
    return frame_transform_sha256_from_preimage(frame_transform_preimage(binding))


def _load_json(path: Path | str) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8")
        json.loads(text)  # retain stable JSON-decode handling before strict parsing
        return text
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScoreContractError("score_view_binding_invalid", "scoring.view_bindings") from exc


def load_score_view_bindings(path: Path | str, *, expected_case_id: str, expected_gt_content_sha256: str,
                             expected_case_metadata_sha256: str, expected_base_view_manifest_sha256: str) -> JudgeScoreViewBindingsV1:
    try:
        result = JudgeScoreViewBindingsV1.model_validate_json(_load_json(path))
    except (ValidationError, ScoreContractError) as exc:
        raise ScoreContractError("score_view_binding_invalid", "scoring.view_bindings") from exc
    if (result.case_id, result.gt_content_sha256, result.case_metadata_sha256, result.base_view_manifest_sha256) != (
        expected_case_id, expected_gt_content_sha256, expected_case_metadata_sha256, expected_base_view_manifest_sha256,
    ):
        raise ScoreContractError("score_view_binding_invalid", "scoring.view_bindings", context={"identity": "mismatch"})
    return result


def validate_score_view_bindings(*, bindings: JudgeScoreViewBindingsV1, base: ViewManifest,
                                 input_ids: set[str] | None = None) -> None:
    required = {entry.input_id: entry for entry in base.required_entries() if entry.view_type in {"plan", "elevation"}}
    if input_ids is not None:
        if not input_ids or not input_ids <= set(required):
            raise ScoreContractError("score_view_binding_invalid", "scoring.view_bindings", context={"scope": "invalid"})
        required = {input_id: entry for input_id, entry in required.items() if input_id in input_ids}
    declared = {binding.input_id: binding for binding in bindings.bindings}
    if set(declared) != set(required):
        raise ScoreContractError("score_view_binding_invalid", "scoring.view_bindings", context={"required": sorted(required), "declared": sorted(declared)})
    for input_id, binding in declared.items():
        entry = required[input_id]
        if (entry.view_type == "plan") != isinstance(binding, PlanScoreViewBindingV1):
            raise ScoreContractError("score_view_binding_invalid", "scoring.view_bindings", context={"input_id": input_id})
        if isinstance(binding, ElevationScoreViewBindingV1):
            if binding.facade_family != binding.resolved_building_direction or binding.world_axis != facade_convention.world_axis(binding.resolved_building_direction):
                raise ScoreContractError("score_direction_unresolved", "scoring.view_bindings", context={"input_id": input_id})
            expected_sign = facade_convention.resolve_sign(
                binding.resolved_building_direction, mirrored=binding.mirrored, local_x_positive=binding.local_x_positive,
            )
            if binding.sign != expected_sign or binding.frame_transform_sha256 != frame_transform_sha256(binding):
                raise ScoreContractError("score_direction_unresolved", "scoring.view_bindings", context={"input_id": input_id})
            if binding.resolution_source == "manifest_building_axis":
                if entry.direction_semantics != "building_axis" or entry.building_view_direction != binding.resolved_building_direction:
                    raise ScoreContractError("score_direction_unresolved", "scoring.view_bindings", context={"input_id": input_id})
            elif entry.direction_semantics not in {"true_azimuth", "unknown"}:
                raise ScoreContractError("score_direction_unresolved", "scoring.view_bindings", context={"input_id": input_id})


def select_score_view_bindings(*, bindings: JudgeScoreViewBindingsV1,
                               input_ids: set[str]) -> JudgeScoreViewBindingsV1:
    """Derive a hash-valid consumer subset; never modify judge-owned source JSON."""
    selected = tuple(binding for binding in bindings.bindings if binding.input_id in input_ids)
    if {binding.input_id for binding in selected} != input_ids:
        raise ScoreContractError("score_view_binding_invalid", "scoring.view_bindings", context={"scope": "unknown_input"})
    payload = bindings.model_dump(mode="python", exclude={"content_sha256"})
    payload["bindings"] = tuple(binding.model_dump(mode="python") for binding in selected)
    payload["content_sha256"] = canonical_sha256(payload)
    return JudgeScoreViewBindingsV1.model_validate(payload)


def source_view_to_gt_view_ids(
    score_bindings: JudgeScoreViewBindingsV1,
) -> dict[str, tuple[str, ...]]:
    """Return the reviewed input-id -> GT-view bridge for matchers."""
    return {
        item.input_id: tuple(item.gt_source_view_ids)
        for item in score_bindings.bindings
    }


def validate_score_view_bindings_against_gt(*, bindings: JudgeScoreViewBindingsV1, base: ViewManifest,
                                            gt: GroundTruthV3,
                                            input_ids: set[str] | None = None) -> None:
    """Validate the §6.3 GT-side floor/facade/source-ref trust root.

    The frozen file loader has no GT parameter, so the future typed service
    calls this companion validator after both independently strict inputs load.
    """
    validate_score_view_bindings(bindings=bindings, base=base, input_ids=input_ids)
    views = {view.id: view for source in gt.sources for view in source.views}
    floors = {floor.id: floor for floor in gt.floors}
    ordered_floor_ids = [floor.id for floor in gt.floors]
    entries = {entry.input_id: entry for entry in base.required_entries()}
    segment_by_id = {segment.id: segment for floor in gt.floors for segment in floor.boundary_segments}
    for binding in bindings.bindings:
        entry = entries[binding.input_id]
        expected_floor_ids = (binding.floor_id,) if isinstance(binding, PlanScoreViewBindingV1) else binding.floor_ids
        if any(floor_id not in floors for floor_id in expected_floor_ids):
            raise ScoreContractError("score_view_binding_invalid", "scoring.view_bindings", context={"input_id": binding.input_id, "reason": "unknown_gt_floor"})
        if isinstance(binding, PlanScoreViewBindingV1):
            # B-M floor_ref is a 1-based position; its target must be this typed GT floor.
            if entry.floor_ref is None or entry.floor_ref < 1 or entry.floor_ref > len(ordered_floor_ids) or ordered_floor_ids[entry.floor_ref - 1] != binding.floor_id:
                raise ScoreContractError("score_view_binding_invalid", "scoring.view_bindings", context={"input_id": binding.input_id, "reason": "floor_mismatch"})
        for source_view_id in binding.gt_source_view_ids:
            source_view = views.get(source_view_id)
            if source_view is None or source_view.kind != binding.kind or tuple(source_view.floor_ids) != tuple(expected_floor_ids):
                raise ScoreContractError("score_view_binding_invalid", "scoring.view_bindings", context={"input_id": binding.input_id, "source_view_id": source_view_id})
            if isinstance(binding, ElevationScoreViewBindingV1) and source_view.facade_family != binding.facade_family:
                raise ScoreContractError("score_view_binding_invalid", "scoring.view_bindings", context={"input_id": binding.input_id, "reason": "facade_mismatch"})
            relevant_refs = []
            for floor_id in expected_floor_ids:
                floor = floors[floor_id]
                relevant_refs.extend(ref for zone in floor.zones for ref in zone.source_refs)
                relevant_refs.extend(ref for segment in floor.boundary_segments
                                     if not isinstance(binding, ElevationScoreViewBindingV1) or segment.facade_family == binding.facade_family
                                     for ref in segment.source_refs)
            relevant_refs.extend(ref for opening in gt.openings
                                 if opening.floor_id in expected_floor_ids and (
                                     not isinstance(binding, ElevationScoreViewBindingV1)
                                     or segment_by_id[opening.boundary_segment_id].facade_family == binding.facade_family
                                 ) for ref in opening.source_refs)
            if not any(ref.view_id == source_view_id for ref in relevant_refs):
                raise ScoreContractError("score_view_binding_invalid", "scoring.view_bindings", context={"input_id": binding.input_id, "source_view_id": source_view_id, "reason": "unreferenced_gt_view"})


def load_completeness_overlay(path: Path | str | None, *, expected_case_id: str,
                              expected_gt_content_sha256: str, expected_base_view_manifest_sha256: str) -> JudgeCompletenessOverlayV1 | None:
    if path is None:
        return None
    try:
        result = JudgeCompletenessOverlayV1.model_validate_json(_load_json(path))
    except (ValidationError, ScoreContractError) as exc:
        raise ScoreContractError("score_completeness_input_invalid", "scoring.completeness") from exc
    if (result.case_id, result.gt_content_sha256, result.base_view_manifest_sha256) != (
        expected_case_id, expected_gt_content_sha256, expected_base_view_manifest_sha256,
    ):
        raise ScoreContractError("score_completeness_input_invalid", "scoring.completeness", context={"identity": "mismatch"})
    return result


def resolve_dataset_declaration(*, declaration, registry_root: Path | str) -> Path:
    """Require exactly one read-only registry declaration matching its full body."""
    if declaration.source != "dataset_ref":
        raise ScoreContractError("score_completeness_input_invalid", "scoring.completeness")
    body = declaration.body
    root = Path(registry_root)
    candidate_root = root / body.dataset_id / body.dataset_version
    matches: list[Path] = []
    try:
        paths = tuple(candidate_root.rglob("*.json"))
    except OSError as exc:
        raise ScoreContractError("score_completeness_input_invalid", "scoring.completeness") from exc
    for path in paths:
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            if parsed.get("source") != "dataset_ref":
                continue
            if parsed.get("body") == body.model_dump(mode="json") and parsed.get("body_sha256") == declaration.body_sha256:
                matches.append(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
    if len(matches) != 1:
        raise ScoreContractError("score_completeness_input_invalid", "scoring.completeness", context={"dataset_id": body.dataset_id, "match_count": len(matches)})
    return matches[0]


def _evidence_for_declaration(declaration, entry: RequiredViewEntry) -> OpeningEvidence:
    body = declaration.body
    claims = tuple(body.negative_claims)
    if not claims or claims != tuple(claim for claim in CLAIM_ORDER if claim in claims):
        raise ScoreContractError("score_completeness_input_invalid", "scoring.completeness", context={"input_id": body.input_id})
    allowed = set(entry.opening_evidence.potentially_observable_claims)
    if not set(claims).issubset(allowed):
        raise ScoreContractError("score_completeness_input_invalid", "scoring.completeness", context={"input_id": body.input_id, "claims": list(claims)})
    if entry.view_type == "plan":
        if body.coverage.kind != "full_floor":
            raise ScoreContractError("score_completeness_input_invalid", "scoring.completeness")
        coverage = Coverage(frame="plan_floor_region", region="full_floor", completeness_assertion_id=body.assertion_id)
    else:
        if body.coverage.kind != "full_facade":
            raise ScoreContractError("score_completeness_input_invalid", "scoring.completeness")
        coverage = Coverage(frame="elevation_local_along", region="full_facade", completeness_assertion_id=body.assertion_id)
    if declaration.source == "user":
        source_ref = UserSourceRef(source="user", content_sha256=declaration.body_sha256)
    else:
        source_ref = DatasetSourceRef(source="dataset_ref", dataset_id=body.dataset_id,
            dataset_version=body.dataset_version, contract_id=body.contract_id, content_sha256=declaration.body_sha256)
    return OpeningEvidence(
        potentially_observable_claims=entry.opening_evidence.potentially_observable_claims,
        negative_evidence_capable_claims=sorted(claims), coverage=coverage,
        completeness_assertion=CompletenessAssertion(assertion_id=body.assertion_id, source_ref=source_ref),
    )


def build_effective_view_manifest(*, base: ViewManifest, overlay: JudgeCompletenessOverlayV1 | None) -> ViewManifest:
    """Return an in-memory projection; neither base nor RunManifest is mutated."""
    if overlay is None:
        return base
    if overlay.base_view_manifest_sha256 != base.content_sha256 or overlay.case_id != base.case_id:
        raise ScoreContractError("score_completeness_input_invalid", "scoring.completeness", context={"identity": "mismatch"})
    entries = []
    by_input = {item.body.input_id: item for item in overlay.declarations}
    for entry in base.entries:
        declaration = by_input.pop(entry.input_id, None)
        if declaration is None:
            entries.append(entry.model_dump(mode="json"))
            continue
        if not isinstance(entry, RequiredViewEntry):
            raise ScoreContractError("score_completeness_input_invalid", "scoring.completeness", context={"input_id": entry.input_id})
        evidence = _evidence_for_declaration(declaration, entry)
        current = entry.opening_evidence
        if current.completeness_assertion is not None:
            if current.model_dump(mode="json") != evidence.model_dump(mode="json"):
                raise ScoreContractError("score_completeness_input_invalid", "scoring.completeness", context={"input_id": entry.input_id, "reason": "base_conflict"})
            entries.append(entry.model_dump(mode="json"))  # exact idempotent repetition
            continue
        payload = entry.model_dump(mode="json")
        payload["opening_evidence"] = evidence.model_dump(mode="json")
        entries.append(payload)
    if by_input:
        raise ScoreContractError("score_completeness_input_invalid", "scoring.completeness", context={"unknown_inputs": sorted(by_input)})
    payload = base.model_dump(mode="json")
    payload["entries"] = entries
    payload.pop("content_sha256")
    payload["content_sha256"] = canonical_sha256(payload)
    try:
        return ViewManifest.model_validate(payload)
    except ValidationError as exc:
        raise ScoreContractError("score_completeness_input_invalid", "scoring.completeness") from exc


def build_reading_score_manifest(
    *,
    effective: ViewManifest,
    trusted_capability_dispositions: tuple[
        ReadingFilteredComponentBasisV1, ...
    ],
    input_ids: set[str] | None = None,
) -> ViewManifest:
    """Remove only trusted-input-filtered claims from the in-memory Va input.

    Attempt bytes are intentionally absent from this boundary.  A malformed or
    empty attempt source keeps both its positive denominator and any reviewed
    negative-evidence capability; only a strict trusted-input disposition may
    edit this score-only manifest.
    """
    component_claims = {
        "plan_segments": frozenset(),
        "plan_openings": frozenset({"existence", "along", "width"}),
        "elevation_opening_xy": frozenset(
            {"existence", "along", "width"}
        ),
        "elevation_opening_z": frozenset({"sill", "head"}),
    }
    removals: dict[str, set[str]] = {}
    for raw in trusted_capability_dispositions:
        try:
            item = (
                raw
                if isinstance(raw, ReadingFilteredComponentBasisV1)
                else ReadingFilteredComponentBasisV1.model_validate(raw)
            )
        except ValidationError as exc:
            raise ScoreContractError(
                "score_view_manifest_invalid",
                "scoring.applicability",
            ) from exc
        removals.setdefault(item.source_input_id, set()).update(
            component_claims[item.component]
        )

    entries: list[dict[str, Any]] = []
    for entry in effective.entries:
        payload = entry.model_dump(mode="json")
        removed = removals.get(entry.input_id, set())
        if not removed or not isinstance(entry, RequiredViewEntry):
            entries.append(payload)
            continue
        evidence = payload["opening_evidence"]
        evidence["potentially_observable_claims"] = sorted(
            set(evidence["potentially_observable_claims"]) - removed
        )
        evidence["negative_evidence_capable_claims"] = sorted(
            set(evidence["negative_evidence_capable_claims"]) - removed
        )
        if not evidence["negative_evidence_capable_claims"]:
            evidence["coverage"] = None
            evidence["completeness_assertion"] = None
        entries.append(payload)

    payload = effective.model_dump(mode="json")
    payload["entries"] = entries
    if input_ids is not None:
        required = {
            entry.input_id
            for entry in effective.required_entries()
        }
        if not input_ids or not input_ids <= required:
            raise ScoreContractError(
                "score_view_binding_invalid",
                "scoring.view_bindings",
                context={"scope": "invalid"},
            )
        payload["entries"] = [
            entry
            for entry in entries
            if entry.get("kind") != "required_view"
            or entry["input_id"] in input_ids
        ]
    payload.pop("content_sha256")
    payload["content_sha256"] = canonical_sha256(payload)
    try:
        return ViewManifest.model_validate(payload)
    except ValidationError as exc:
        raise ScoreContractError(
            "score_view_manifest_invalid",
            "scoring.applicability",
        ) from exc


def materialize_va_elevation_bindings(*, score_bindings: JudgeScoreViewBindingsV1,
                                      effective_manifest: ViewManifest) -> tuple[ElevationViewBindingV1, ...]:
    validate_score_view_bindings(
        bindings=score_bindings,
        base=effective_manifest,
        input_ids={binding.input_id for binding in score_bindings.bindings},
    )
    output = []
    for binding in score_bindings.bindings:
        if isinstance(binding, ElevationScoreViewBindingV1):
            output.append(ElevationViewBindingV1(
                input_id=binding.input_id, resolved_building_direction=binding.resolved_building_direction,
                resolution_source=binding.resolution_source, view_manifest_sha256=effective_manifest.content_sha256,
                orientation_output_hash=binding.orientation_output_hash, adapter_version=binding.adapter_version,
                source_footprint_fingerprint=binding.source_footprint_fingerprint, world_axis=binding.world_axis,
                sign=binding.sign, along_origin=binding.along_origin, mirrored=binding.mirrored,
                local_x_positive=binding.local_x_positive, frame_transform_sha256=frame_transform_sha256(binding),
            ))
    return tuple(output)
