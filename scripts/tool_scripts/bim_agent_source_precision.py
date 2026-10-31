"""Trusted source-coordinate policy and proof for reference-line adoption.

Pixel construction keeps its own 0.1 m grid.  An adopted local ruler constrains
architectural parameters, not every world coordinate derived by intersections.
No field in a submitted plan selects this policy or waives source validation.
"""
from __future__ import annotations

import copy


REFERENCE_LINES_POLICY = "architectural_reference_lines_v1"


def reference_line_policy(manifest):
    value = manifest.get("source_precision_policy")
    if value is None:
        return False
    if value != REFERENCE_LINES_POLICY:
        raise ValueError("unknown source_precision_policy: " + str(value))
    if not manifest.get("architectural_modelling_policy"):
        raise ValueError("reference-line policy requires architectural_modelling_policy")
    return True


def validate_reference_line_adoption(toolkit, proposal, reference):
    """Validate internally saved adoption before replacing a world-vertex test.

    Missing proof retains the historical vertex test.  Invalid proof fails;
    supplying a reference never changes overlap, host or topology constraints.
    Historical references do not acquire additional requirements.
    """
    if not reference_line_policy(toolkit.manifest) or reference is None:
        return None
    from scripts.tool_scripts.bim_agent_architectural import (
        _hash, _read, _inherited_modelling, validate_build_regularization)
    from src.agent.geometry.architectural_regularization import _hash as audit_hash
    from src.agent.geometry.parametric_proposal import expand_parametric_proposal

    required = {"schema_version", "kind", "policy", "original", "effective", "adopted", "audit",
                "original_sha256", "adopted_sha256", "proposal_sha256"}
    if not isinstance(reference, dict) or required - reference.keys():
        raise ValueError("reference-line adoption requires a complete saved architectural proof")
    validate_build_regularization(toolkit, proposal, reference)
    if reference.get("schema_version") != 1 or reference.get("kind") not in {"parametric", "proposal"}:
        raise ValueError("invalid architectural adoption reference")
    original = _read(toolkit, reference["original"])
    effective = _read(toolkit, reference["effective"])
    adopted = _read(toolkit, reference["adopted"])
    audit = _read(toolkit, reference["audit"])
    if _hash(original) != reference["original_sha256"]:
        raise ValueError("architectural original declaration binding changed")
    expected = copy.deepcopy(original)
    if reference["kind"] == "proposal":
        context = _inherited_modelling(toolkit, reference.get("frame_context_parent"), original)
        context.update(copy.deepcopy(original.get("modelling", {})))
        if context:
            expected["modelling"] = context
        if context != reference.get("modelling", {}):
            raise ValueError("architectural inherited frame context changed")
    if effective != expected:
        raise ValueError("architectural effective declaration binding changed")
    if (audit.get("status") != "adopted" or audit.get("rejections")
            or audit.get("input_sha256") != audit_hash(effective)
            or audit.get("adopted_sha256") != audit_hash(adopted)):
        raise ValueError("architectural audit does not prove this adoption")
    for key, value in toolkit.manifest["architectural_modelling_policy"].items():
        if audit.get("policy", {}).get(key) != value:
            raise ValueError("architectural audit policy changed")
    expanded = expand_parametric_proposal(adopted) if reference["kind"] == "parametric" else adopted
    if expanded != proposal:
        raise ValueError("proposal is not the actual adopted declaration expansion")
    unsupported = [row for row in audit.get("not_assessed", [])
                   if row.get("scope") != "shared junction locator"]
    if unsupported:
        raise ValueError("reference-line source precision not assessed: " + str(unsupported))
    return {"policy": REFERENCE_LINES_POLICY, "status": "adopted",
            "proposal_sha256": reference["proposal_sha256"],
            "audit": copy.deepcopy(reference["audit"]),
            "meaning": "Architectural parameters adopted; derived world vertices are not quantized. "
                       "Common source validity and physical precision checks still apply."}
