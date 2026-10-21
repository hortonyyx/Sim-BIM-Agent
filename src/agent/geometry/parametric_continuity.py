"""Join explicitly selected, identical storey spaces without redrawing geometry.

The declaration supplies continuity, member selection and evidence. This module
only removes artificial intermediate caps and rebinds existing apertures; it
does not infer stair/lift semantics, service floors or new openings.
"""
from __future__ import annotations

import copy

from shapely.geometry import Polygon


def declared_continuity_members(plan, instances=None, groups=None):
    """Resolve only explicit instance/repeat membership, for all compiler paths."""
    from src.agent.geometry.parametric_proposal import _instance_records, fields, identifier, strings
    declarations = plan.get("continuous_spaces", [])
    if not isinstance(declarations, list) or len(declarations) > 100:
        raise ValueError("continuous_spaces must be a list of at most 100 declarations")
    if not declarations:
        return []
    if instances is None:
        instances, groups = _instance_records(plan)
    instance_map = {row["id"]: row for row in instances}
    generated, claimed, result = set(instance_map), set(), []
    for declaration in declarations:
        fields(declaration, ("id", "space", "source_refs"),
               ("instance_ids", "repeat_ids", "assumptions"))
        target_floor = identifier(declaration["id"])
        local = identifier(declaration["space"])
        if target_floor in generated:
            raise ValueError(f"{target_floor}: continuous instance ID already exists")
        generated.add(target_floor)
        selected = strings(declaration.get("instance_ids", []))
        repeat_ids = strings(declaration.get("repeat_ids", []))
        for repeat_id in repeat_ids:
            if repeat_id not in groups:
                raise ValueError(f"{target_floor}: unknown repeat {repeat_id}")
            selected.extend(groups[repeat_id])
        if (len(selected) < 2 or len(set(selected)) != len(selected)
                or len(set(repeat_ids)) != len(repeat_ids)):
            raise ValueError(f"{target_floor}: select at least two unique explicit member instances")
        refs = strings(declaration["source_refs"], True)
        assumptions = strings(declaration.get("assumptions", []))
        for fid in selected:
            if fid not in instance_map:
                raise ValueError(f"{target_floor}: unknown member instance {fid}")
        member_ids = [f"{fid}:{local}" for fid in selected]
        if claimed.intersection(member_ids):
            raise ValueError(f"{target_floor}: member is already selected by another continuous space")
        claimed.update(member_ids)
        result.append(dict(id=target_floor, space_id=f"{target_floor}:{local}",
                           member_space_ids=member_ids, source_refs=refs, assumptions=assumptions))
    return result


def apply_continuous_spaces(plan, instances, groups, floors, windows, openings):
    """Update freshly expanded records and return an auditable member mapping."""
    from src.agent.geometry.source_bim import EPS
    declarations = declared_continuity_members(plan, instances, groups)
    if not declarations:
        return []
    instance_map = {row["id"]: row for row in instances}
    cells = {cell["id"]: (floor, cell) for floor in floors for cell in floor["cells"]}
    already_spanning = {sid for floor in floors for sid in floor.get("spanning_space_ids", [])}
    mapping, audit, additions = {}, [], []
    for declaration in declarations:
        target_floor, target_space = declaration["id"], declaration["space_id"]
        refs, assumptions = declaration["source_refs"], declaration["assumptions"]
        members = []
        for sid in declaration["member_space_ids"]:
            if sid not in cells:
                raise ValueError(f"{target_floor}: unknown local member {sid}")
            if sid in already_spanning:
                raise ValueError(f"{target_floor}: member {sid} is already spanning")
            members.append(cells[sid])
        members.sort(key=lambda row: row[0]["z_floor"])
        reference = members[0][1]
        polygon = Polygon(reference["polygon"], reference.get('holes', []))
        for index, (floor, cell) in enumerate(members):
            if cell["role"] != reference["role"] or cell.get("role_evidence") != reference.get("role_evidence"):
                raise ValueError(f"{target_floor}: member roles and role evidence must agree")
            if not polygon.equals(Polygon(cell["polygon"], cell.get('holes', []))):
                raise ValueError(f"{target_floor}: member footprints differ; no shape adoption is performed")
            if index:
                previous = members[index - 1][0]
                # Use the source kernel's numerical touching predicate, not an
                # architectural gap-closing distance. Other spaces stay intact.
                previous_top = previous["z_floor"] + previous["ceiling_height"]
                if abs(previous_top - floor["z_floor"]) > EPS:
                    raise ValueError(f"{target_floor}: member heights contain a gap or overlap")
        member_ids = [cell["id"] for _, cell in members]
        mapping.update(dict.fromkeys(member_ids, target_space))
        merged = copy.deepcopy(reference)
        merged["id"] = target_space
        merged["source_refs"] = list(dict.fromkeys(
            [ref for _, cell in members for ref in cell["source_refs"]] + refs))
        merged["assumptions"] = list(dict.fromkeys(
            [item for _, cell in members for item in cell.get("assumptions", [])] + assumptions))
        base = members[0][0]["z_floor"]
        last = members[-1][0]
        top = last["z_floor"] + last["ceiling_height"]
        height = top - base
        additions.append(dict(name=target_floor, z_floor=base, ceiling_height=height,
                              footprint={"vertices": copy.deepcopy(merged["polygon"]),
                                         **({'holes': copy.deepcopy(merged['holes'])} if merged.get('holes') else {})}, cells=[merged]))
        audit.append(dict(id=target_floor, space_id=target_space, member_space_ids=member_ids,
                          z=[base, top], source_refs=refs, assumptions=assumptions))

    # Validate before mutating: an opening between two members cannot silently
    # disappear or become an invalid self-connection after the join.
    for opening in openings:
        a, b = opening.get("space_id"), opening.get("other_space_id")
        if a in mapping and b is not None and mapping.get(a, a) == mapping.get(b, b):
            raise ValueError(f"{opening.get('id')}: continuous join would create a self-connection")

    for floor in floors:
        removed = [cell["id"] for cell in floor["cells"] if cell["id"] in mapping]
        if removed:
            floor["cells"] = [cell for cell in floor["cells"] if cell["id"] not in mapping]
            spanning = floor.setdefault("spanning_space_ids", [])
            spanning.extend(mapping[sid] for sid in removed)
            instance_map[floor["name"]]["spanning_space_ids"] = list(spanning)
    for window in windows:
        if window.get("room") in mapping:
            window["room"] = mapping[window["room"]]
            window["floor"] = window["room"].split(":", 1)[0]
    for opening in openings:
        for key in ("space_id", "other_space_id"):
            if opening.get(key) in mapping:
                opening[key] = mapping[opening[key]]
    floors.extend(additions)
    return audit


def expand_separated_spaces(plan, instances, groups, floors, continuity):
    """Retain only explicitly declared cap interfaces; never infer an exception."""
    from src.agent.geometry.parametric_proposal import fields, identifier, strings
    from src.agent.correction.schema import VerticalSeparation
    declarations = plan.get("separated_spaces", [])
    if not isinstance(declarations, list) or len(declarations) > 100:
        raise ValueError("separated_spaces must be a list of at most 100 declarations")
    if not declarations:
        return []
    instances_by_id = {row["id"]: row for row in instances}
    cells = {cell["id"] for floor in floors for cell in floor["cells"]}
    merged = {sid: row["space_id"] for row in continuity for sid in row["member_space_ids"]}
    identities, result = set(), []
    for declaration in declarations:
        fields(declaration, ("id", "space", "reason", "source_refs"),
               ("instance_ids", "repeat_ids", "assumptions"))
        identity, local = identifier(declaration["id"]), identifier(declaration["space"])
        if identity in identities:
            raise ValueError(f"{identity}: duplicate separated_spaces declaration")
        identities.add(identity)
        selected = strings(declaration.get("instance_ids", []))
        repeats = strings(declaration.get("repeat_ids", []))
        for rid in repeats:
            if rid not in groups:
                raise ValueError(f"{identity}: unknown separation repeat {rid}")
            selected.extend(groups[rid])
        if len(selected) < 2 or len(set(selected)) != len(selected) or len(set(repeats)) != len(repeats):
            raise ValueError(f"{identity}: separation members must contain at least two unique instances")
        if any(fid not in instances_by_id for fid in selected):
            raise ValueError(f"{identity}: unknown separation instance")
        raw_members = [f"{fid}:{local}" for fid in selected]
        members = [merged.get(sid, sid) for sid in raw_members]
        if len(set(members)) != len(members):
            raise ValueError(f"{identity}: separation contradicts continuous_spaces within the same volume")
        if any(sid not in cells for sid in members):
            raise ValueError(f"{identity}: unknown separation space")
        record = VerticalSeparation.model_validate({
            "id": identity, "space_ids": members, "reason": declaration["reason"],
            "source_refs": strings(declaration["source_refs"], True),
            "assumptions": strings(declaration.get("assumptions", [])),
        }).model_dump(mode="json")
        result.append(record)
    return result
