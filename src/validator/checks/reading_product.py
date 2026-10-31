"""Reading gate dispatch by the product contract, before legacy defaults apply.

The as-drawn branch checks the registered producer shape and its native rulers.
It does not claim to run the legacy stroke lint or the image-dependent ink
checks: their applicability is recorded explicitly. Pixel truth still needs
the as-drawn image checks / judge.
"""
from __future__ import annotations

import math

from src.agent.reading import parse_reading_view
from src.agent.reading.constants import DIMCHAIN_CLOSE_TOL_M
from src.agent.reading.vector_contract import (
    CONTRACT_AS_DRAWN_ELEVATION_V0,
    CONTRACT_AS_DRAWN_PLAN,
    CONTRACT_STAGE_CHECK_REPORT,
    classify_vector_json,
)
from src.validator.checks.reading import check_reading_view
from src.validator.checks.schema import CheckLayer, CheckReport, CheckStatus, RunProfile

# Every legacy per-view check except the two native dimension checks below.
# Keep the roster visible in reports: an empty legacy default was often a
# vacuous PASS as well as the four misleading FLAGs seen in wallhunt W#1.
LEGACY_FIELD_CHECKS = {
    CheckLayer.INVARIANT: (
        "stroke_ids_unique", "dimension_ids_unique", "pen_kind_valid",
        "no_topology_fields", "nondegenerate_geometry", "dimension_parseable",
        "axis_endpoint_consistent", "facade_fields", "uncaptured_present",
        "room_label_roles_valid", "room_label_basis_valid",
    ),
    CheckLayer.CROSS_CHECK: (
        "plan_scale_origin_usable", "raw_field_presence", "dimension_p1a_fields",
        "room_label_anchors_in_bounds", "ocr_anchors_in_bounds",
        "dimension_endpoints_in_bounds", "dimension_derived_refs",
        "stroke_provenance_coverage", "stroke_dimension_consistency",
        "partition_on_window_jamb", "door_heal_traced",
    ),
}


def check_reading_product(
    raw: object,
    *,
    capability_profile: str = "rectangular",
    run_profile: RunProfile = "exploratory",
    dimensioned_state: str = "legacy_default",
) -> CheckReport:
    rep = CheckReport(stage="0_reading", capability_profile=capability_profile,
                      run_profile=run_profile)
    if not isinstance(raw, dict):
        rep.add_fail("reading.view_payload_shape", CheckLayer.INVARIANT,
                     "produced view is not a JSON object")
        return rep
    decision = classify_vector_json(raw)
    # Undeclared historical inputs retain the old migration and diagnostics.
    # A declared, malformed/unknown/ambiguous contract NEVER falls back here.
    if "schema" not in raw and decision.contract_id != CONTRACT_STAGE_CHECK_REPORT:
        try:
            view = parse_reading_view(raw)
        except Exception as exc:  # malformed products must leave a gate report
            rep.add_fail("reading.view_payload_valid", CheckLayer.INVARIANT,
                         f"produced view failed schema validation: {exc}")
            return rep
        return check_reading_view(view, capability_profile=capability_profile,
                                  run_profile=run_profile,
                                  dimensioned_state=dimensioned_state)
    if decision.contract_id not in {CONTRACT_AS_DRAWN_PLAN, CONTRACT_AS_DRAWN_ELEVATION_V0}:
        rep.add_fail("reading.view_payload_valid", CheckLayer.INVARIANT,
                     "unsupported, malformed or ambiguous reading product contract",
                     evidence={"contract": decision.contract_id, "reason": decision.reason})
        return rep

    is_plan = decision.contract_id == CONTRACT_AS_DRAWN_PLAN
    evidence = {"contract": decision.contract_id,
                "image_kind": "plan" if is_plan else "elevation"}
    rep.add_pass("reading.product_contract", CheckLayer.INVARIANT, evidence=evidence)
    for layer, checks in LEGACY_FIELD_CHECKS.items():
        for check in checks:
            rep.add(f"reading.{check}", CheckStatus.NOT_APPLICABLE, layer,
                    message="legacy field check does not apply to this product contract",
                    evidence=evidence)
    calibration = raw.get("observations", {}).get("calibration") if is_plan else raw.get("calibration")
    _check_calibration(rep, calibration, axes=("x", "y") if is_plan else ("x", "z"),
                       path="observations.calibration" if is_plan else "calibration",
                       dimensioned_state=dimensioned_state, evidence=evidence)
    return rep


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _numbers(value: object, *, minimum: int) -> bool:
    return isinstance(value, list) and len(value) >= minimum and all(_number(v) for v in value)


def _check_calibration(rep, calibration, *, axes, path, dimensioned_state, evidence):
    cal = calibration if isinstance(calibration, dict) else {}
    evidence = {**evidence, "carrier": path, "dimensioned_state": dimensioned_state}
    frame_errors, chain_errors, counts = [], [], {}
    if not _number(cal.get("mm_per_px")) or cal["mm_per_px"] <= 0:
        frame_errors.append(f"{path}.mm_per_px must be positive and finite")
    zero = cal.get("world_zero_px")
    if not _numbers(zero, minimum=2) or len(zero) != 2:
        frame_errors.append(f"{path}.world_zero_px must contain two finite numbers")
    closure = {}
    for axis in axes:
        chain = cal.get(axis)
        if not isinstance(chain, dict):
            frame_errors.append(f"{path}.{axis} is missing or is not an object")
            counts[axis] = 0
            chain_errors.append(f"{axis}: no dimension chain")
            continue
        scale = chain.get("mm_per_px")
        if not _number(scale) or scale <= 0 or not _number(chain.get("origin_px")):
            frame_errors.append(f"{path}.{axis} has no usable pixel ruler")
        cum, values, overall = chain.get("cum_mm"), chain.get("values_mm"), chain.get("overall_mm")
        counts[axis] = len(cum) if _numbers(cum, minimum=2) else 0
        if (not counts[axis] or not _numbers(values, minimum=1)
                or not _number(overall) or overall <= 0):
            chain_errors.append(f"{axis}: invalid cumulative/segment/overall dimensions")
            continue
        if (len(cum) != len(values) + 1 or cum[0] != 0
                or any(b <= a for a, b in zip(cum, cum[1:]))
                or any(v <= 0 for v in values)):
            chain_errors.append(f"{axis}: inconsistent cumulative dimension sequence")
            continue
        # Recompute from the native dimensions; never trust chain_closure_mm.
        errors = [abs(sum(values) - overall), abs(cum[-1] - overall)]
        errors.extend(abs(b - a - v) for a, b, v in zip(cum, cum[1:], values))
        closure[axis] = max(errors)
        if closure[axis] > DIMCHAIN_CLOSE_TOL_M * 1000:
            chain_errors.append(f"{axis}: dimension chain does not close")
    if frame_errors:
        rep.add_fail("reading.as_drawn.calibration_usable", CheckLayer.INVARIANT,
                     "; ".join(frame_errors), evidence=evidence)
    else:
        rep.add_pass("reading.as_drawn.calibration_usable", CheckLayer.INVARIANT,
                     evidence=evidence)
    dimension_evidence = {**evidence, "tick_counts": counts}
    if dimensioned_state != "declared_true":
        rep.add("reading.dimensions_present", CheckStatus.NOT_APPLICABLE, CheckLayer.CROSS_CHECK,
                message=f"dimension applicability is {dimensioned_state}", evidence=dimension_evidence)
    elif not all(counts.values()):
        rep.add_fail("reading.dimensions_present", CheckLayer.CROSS_CHECK,
                     "dimensioned view lacks native cumulative dimension ticks", evidence=dimension_evidence)
    else:
        rep.add_pass("reading.dimensions_present", CheckLayer.CROSS_CHECK, evidence=dimension_evidence)
    closure_evidence = {**evidence, "recomputed_max_error_mm": closure,
                        "tolerance_mm": DIMCHAIN_CLOSE_TOL_M * 1000}
    if chain_errors:
        rep.add_fail("reading.dimension_chain_closure", CheckLayer.CROSS_CHECK,
                     "; ".join(chain_errors), evidence=closure_evidence)
    else:
        rep.add_pass("reading.dimension_chain_closure", CheckLayer.CROSS_CHECK, evidence=closure_evidence)
