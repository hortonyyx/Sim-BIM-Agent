"""Review explicit vertical relations without inferring them from names or prose.

An aligned stack is a question about intended continuity, not proof that every
shaft must be open. Existing reciprocal horizontal enclosure openings resolve
the question; a documented separation retains real caps. No geometry is edited.
"""
from __future__ import annotations

import copy
from itertools import combinations

from shapely.geometry import Polygon

from src.agent.correction.schema import VerticalSeparation
from src.agent.geometry.source_bim import EPS
from src.agent.geometry.source_volume import space_polygon_at_z
from src.agent.roles import require_role


STACK_ROLES = frozenset({"shaft", "stairwell", "atrium"})


def review_vertical_continuity(source: dict, *, tolerance: float = EPS) -> dict:
    spaces = {row["id"]: row for row in source.get("spaces", [])}
    roles = {sid: require_role(row.get("role")) for sid, row in spaces.items()}
    boundaries = source.get("boundaries", [])
    raw = source.get("vertical_separations", [])
    invalid, declarations, identities, claims = [], [], set(), {}
    if not isinstance(raw, list):
        invalid.append({"code": "invalid_declarations", "reason": "vertical_separations must be a list"})
        raw = []
    for index, value in enumerate(raw):
        try:
            row = VerticalSeparation.model_validate(value).model_dump(mode="json")
        except ValueError as error:
            invalid.append({"code": "invalid_declaration", "index": index, "reason": str(error)})
            continue
        if row["id"] in identities:
            invalid.append({"code": "duplicate_declaration", "id": row["id"]})
            continue
        identities.add(row["id"])
        unknown = sorted(set(row["space_ids"]) - spaces.keys())
        if unknown:
            invalid.append({"code": "unknown_members", "id": row["id"], "space_ids": unknown})
            continue
        declarations.append(row)
        for a, b in combinations(row["space_ids"], 2):
            claims.setdefault(frozenset((a, b)), []).append(row["id"])

    def cap(sid, kind, z):
        rows = [row for row in boundaries if row.get("space_id") == sid
                and row.get("geometry_type") == kind
                and row.get("vertices") and all(abs(p[2] - z) <= tolerance for p in row["vertices"])]
        if len(rows) != 1:
            return {"state": "unverified", "boundary_ids": [row["id"] for row in rows]}
        row = rows[0]
        if not Polygon([p[:2] for p in row["vertices"]], [[p[:2] for p in h] for h in row.get('holes', [])]).equals(space_polygon_at_z(spaces[sid], z)):
            return {"state": "unverified", "boundary_ids": [row["id"]]}
        state = ("physical" if row.get("kind") == "physical" and row.get("enclosure", "physical") == "physical"
                 else row.get("enclosure", row.get("kind", "unverified")))
        return {"state": state, "boundary_ids": [row["id"]]}

    def open_connection(a, b, z):
        # These records are produced only after reciprocal enclosure validation.
        for row in source.get("source_enclosure", {}).get("open_connections", []):
            points = row.get("vertices", [])
            if (set(row.get("space_ids", [])) == {a, b} and not row.get("exterior")
                    and row.get("condition") == "open" and len(points) >= 3
                    and all(abs(p[2] - z) <= tolerance for p in points)
                    and Polygon([p[:2] for p in points]).area > tolerance ** 2):
                return True
        return False

    unresolved, resolved, varying, used = [], [], [], {row["id"]: set() for row in declarations}
    checked_pairs = 0
    for a, b in combinations(spaces.values(), 2):
        lower, upper = sorted((a, b), key=lambda row: row["z_floor"])
        lid, uid = lower["id"], upper["id"]
        top = lower["z_floor"] + lower["height"]
        if abs(top - upper["z_floor"]) > tolerance:
            continue
        selected = claims.get(frozenset((lid, uid)), [])
        candidate_role = roles[lid] == roles[uid] and roles[lid] in STACK_ROLES
        if not selected and not candidate_role:
            continue
        lower_shape = space_polygon_at_z(lower, top)
        upper_shape = space_polygon_at_z(upper, upper['z_floor'])
        if lower_shape.intersection(upper_shape).area <= tolerance ** 2:
            continue
        same_shape = lower_shape.equals(upper_shape)
        if not same_shape and not selected:
            varying.append({"space_ids": [lid, uid], "z_m": top, "reason": "different_footprints"})
            continue
        checked_pairs += int(same_shape and candidate_role)
        lower_cap, upper_cap = cap(lid, "ceiling", top), cap(uid, "floor", upper["z_floor"])
        row = {"space_ids": [lid, uid], "z_m": top,
               "lower_boundary": lower_cap, "upper_boundary": upper_cap}
        is_open = open_connection(lid, uid, top)
        if selected:
            for identity in selected:
                used[identity].update((lid, uid))
            if len(selected) > 1:
                invalid.append({"code": "duplicate_interface", "space_ids": [lid, uid],
                                "declaration_ids": selected})
            if is_open or lower_cap["state"] != "physical" or upper_cap["state"] != "physical":
                invalid.append({"code": "separation_not_physical", "space_ids": [lid, uid],
                                "declaration_ids": selected})
            row.update(status="explicit_separation", declaration_ids=selected)
            resolved.append(row)
        elif is_open:
            row["status"] = "open_connection"
            resolved.append(row)
        else:
            row.update(status="decision_required",
                       boundary_state=("physical_caps" if lower_cap["state"] == upper_cap["state"] == "physical"
                                       else "unverified_boundary_state"))
            unresolved.append(row)
    for row in declarations:
        unconnected = sorted(set(row["space_ids"]) - used[row["id"]])
        if unconnected:
            invalid.append({"code": "members_without_adjacent_interface", "id": row["id"],
                            "space_ids": unconnected})

    # One result per connected unresolved stack, never one model turn per cap.
    components = []
    remaining = list(unresolved)
    while remaining:
        seed = remaining.pop(0)
        members, interfaces = set(seed["space_ids"]), [seed]
        changed = True
        while changed:
            changed = False
            for row in remaining[:]:
                if members.intersection(row["space_ids"]):
                    members.update(row["space_ids"])
                    interfaces.append(row)
                    remaining.remove(row)
                    changed = True
        components.append({"space_ids": sorted(members, key=lambda sid: (spaces[sid]["z_floor"], sid)),
                           "role": roles[next(iter(members))],
                           "interface_count": len(interfaces), "interfaces": interfaces,
                           "decision": "Declare a continuous volume or an evidenced physical separation; "
                                       "aligned geometry alone does not establish intended continuity."})
    return {
        "schema_version": "vertical_continuity_review_v1",
        "status": "invalid" if invalid else "decision_required" if components else "resolved",
        "unresolved_stacks": components, "resolved_interfaces": resolved,
        "explicit_separations": declarations, "invalid_declarations": invalid,
        "interpretation": "Resolved applies only to the exact aligned-stack scope below; "
                          "role, naming and evidence prose alone never establish continuity.",
        "coverage": {"space_count": len(spaces), "candidate_role_space_count": sum(role in STACK_ROLES for role in roles.values()),
                     "checked_aligned_interface_count": checked_pairs,
                     "different_footprint_pairs": varying,
                     "not_assessed": ["continuity across gaps, changing footprints or different roles",
                                      "stair treads, ramps, equipment service and unobserved internal openings"]},
    }


def bind_vertical_continuity_review(source: dict) -> None:
    """Recompute after geometry/enclosure changes; caller binds the final digest."""
    policy = source.get("vertical_continuity_policy")
    if policy not in (None, "explicit_stack_v1"):
        raise ValueError(f"unknown vertical continuity policy: {policy!r}")
    review = review_vertical_continuity(source)
    source["vertical_continuity_review"] = review
    validation = source.setdefault("validation", {"status": "pass", "findings": []})
    original_findings = validation.get("findings", [])
    unexplained_severe = (validation.get("status") == "severe"
                          and not any(row.get("severity") == "severe" for row in original_findings))
    findings = [row for row in original_findings if row.get("code") not in {
        "source.vertical_separation_invalid", "source.vertical_continuity_decision_required"}]
    if review["invalid_declarations"]:
        findings.append({"code": "source.vertical_separation_invalid", "severity": "severe",
                         "issues": copy.deepcopy(review["invalid_declarations"])})
    if policy == "explicit_stack_v1":
        for stack in review["unresolved_stacks"]:
            findings.append({
                "code": "source.vertical_continuity_decision_required", "severity": "severe",
                "space_ids": list(stack["space_ids"]), "role": stack["role"],
                "interface_count": stack["interface_count"],
                "reason": "Aligned adjacent spaces have unresolved vertical continuity; this does not "
                          "establish that every shaft must be continuous.",
                "repair_hint": "Resolve all stacks together: declare continuous_spaces for confirmed "
                               "continuity, or separated_spaces with member-specific separation evidence. "
                               "Preserve accepted geometry and existing apertures.",
            })
    validation["findings"] = findings
    validation["status"] = ("severe" if unexplained_severe or any(row.get("severity") == "severe" for row in findings)
                            else "warning" if any(row.get("severity") == "warning" for row in findings) else "pass")
