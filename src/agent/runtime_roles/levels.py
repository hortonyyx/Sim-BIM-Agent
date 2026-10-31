"""Apply explicitly cited elevation levels to a copy of a reader plan."""

from __future__ import annotations

import copy
import json
import math
import re

from src.agent.geometry.plan_feedback import resolve_plan_lengths


LEVEL_TOLERANCE_M = 0.02


def decision(kind, reason, action, **details):
    """Stable, located issue IDs, independent of candidate numbering."""
    import hashlib
    from src.agent_runtime.store import json_bytes
    row = {"kind": kind, "reason": reason, "action": action, **details}
    return {"id": hashlib.sha256(json_bytes(row)).hexdigest()[:16], **row}


def resolve_levels(registry, plans, elevations, overrides=()):
    """Choose actual cited values; never average or hide missing/conflicting levels."""
    plans = {floor: resolve_plan_lengths(plan)[0] for floor, plan in plans.items()}
    readings = [{"task_id": task_id, "elevation_id": row["id"],
                 "artifact_sha256": registry.records[task_id]["artifact"]["sha256"],
                 "image": artifact["image"], "level": row}
                for task_id, artifact in elevations.items() for row in artifact["elevations"]]
    changes = {}
    for row in overrides:
        floor = row["floor_id"]
        if floor not in plans or floor in changes:
            raise ValueError("level_overrides need distinct selected floor IDs")
        changes[floor] = {key: value for key, value in row.items() if key != "floor_id"}
    issues = []

    def pick(rows, floor, field, fallback, rule):
        rows = sorted(rows, key=lambda r: (r["task_id"], r["elevation_id"]))
        missing = sorted(set(elevations) - {r["task_id"] for r in rows})
        consistent = rows and not missing and max(r["level"]["value_m"] for r in rows) - min(
            r["level"]["value_m"] for r in rows) <= LEVEL_TOLERANCE_M + 1e-9
        value = rows[0]["level"]["value_m"] if consistent else fallback
        return {"value_m": value, "status": "elevation" if consistent else ("plan_assumption" if fallback is not None else "unresolved"),
                "rule": rule, "references": rows, "missing_task_ids": missing}

    # Explicit floor marks determine stack order where present. Natural floor IDs
    # break equal/local-zero plan bases deterministically (F2 before F10).
    def floor_order(floor):
        rows = [r["level"]["value_m"] for r in readings
                if r["level"]["kind"] == "floor" and r["level"].get("floor_id") == floor]
        natural = tuple((0, int(x)) if x.isdigit() else (1, x.casefold())
                        for x in re.split(r"(\d+)", floor))
        consistent = rows and max(rows) - min(rows) <= LEVEL_TOLERANCE_M + 1e-9
        return min(rows) if consistent else plans[floor].get("z_floor", 0), natural

    floors = sorted(plans, key=floor_order)
    resolved = {}
    for index, floor in enumerate(floors):
        rows = [r for r in readings if r["level"]["kind"] == "floor"
                and r["level"].get("floor_id") == floor]
        rule = "floor_mark"
        if index == 0:
            missing = set(elevations) - {r["task_id"] for r in rows}
            ground = [r for r in readings if r["task_id"] in missing and r["level"]["kind"] == "ground"
                      and r["level"].get("floor_id") in (None, floor)]
            rule = "lowest_floor_ground" if not rows else ("floor_mark_or_ground" if ground else rule)
            rows += ground
        resolved[floor] = {"z_floor": pick(rows, floor, "z_floor", plans[floor].get("z_floor"), rule)}
        override = changes.get(floor, {})
        if "z_floor" in override or "z_floor_evidence" in override:
            _, citations = apply_levels(registry, plans[floor], **{
                key: value for key, value in override.items() if key.startswith("z_floor")})
            ref = citations["z_floor"]
            resolved[floor]["z_floor"] = {"value_m": ref["value_m"], "status": "override",
                "rule": "explicit_cited_floor", "references": [ref]}
    for index, floor in enumerate(floors):
        plan, base = plans[floor], resolved[floor]["z_floor"]
        if index + 1 < len(floors):
            top = copy.deepcopy(resolved[floors[index + 1]]["z_floor"])
            top["rule"] = "next_floor:" + floors[index + 1]
        else:
            # Reader labels differ (run4: roof/eave/other for the same top).
            # Compare each facade's highest top-like mark, retaining equal marks.
            # Ground and intermediate floor marks cannot stand in for a roof.
            rows = []
            for task_id in sorted(elevations):
                candidates = [r for r in readings if r["task_id"] == task_id
                    and r["level"]["kind"] in {"eave", "roof", "other"}
                    and r["level"].get("floor_id") in (None, floor)]
                if candidates:
                    highest = max(r["level"]["value_m"] for r in candidates)
                    rows.extend(r for r in candidates if r["level"]["value_m"] == highest)
            top = pick(rows, floor, "ceiling_height", (base["value_m"] + plan["ceiling_height"] if base["value_m"] is not None and "ceiling_height" in plan else None),
                       "top_floor_highest_per_facade:eave|roof|other")
        height = (top["value_m"] - base["value_m"] if top["value_m"] is not None and base["value_m"] is not None else None)
        valid = base["status"] in {"elevation", "override"} and top["status"] in {"elevation", "override"} and height is not None and height > 0
        resolved[floor]["ceiling_height"] = {
            "value_m": height if valid else plan.get("ceiling_height"),
            "status": "elevation" if valid else ("plan_assumption" if "ceiling_height" in plan else "unresolved"), "rule": top["rule"],
            "references": top["references"], "floor_references": base["references"],
            "missing_task_ids": top.get("missing_task_ids", [])}
        override = changes.get(floor, {})
        if "ceiling_height" in override or "ceiling_height_evidence" in override:
            _, citations = apply_levels(registry, {**plan, "z_floor": base["value_m"]}, **{
                key: value for key, value in override.items() if key.startswith("ceiling_height")})
            ref = citations["ceiling_height"]
            resolved[floor]["ceiling_height"] = {"value_m": ref["value_m"], "status": "override",
                "rule": "explicit_cited_top_minus_floor", "references": [ref], "floor_references": base["references"]}
        for field, row in resolved[floor].items():
            if row["status"] in {"plan_assumption", "unresolved"}:
                issues.append(decision("level_unresolved", "Missing, inconsistent or nonpositive elevation levels; an explicit value is required when the plan has no observed height.",
                    "Inspect the cited levels; re-dispatch the elevation reader or call assemble_from_readers with a cited level_overrides value.",
                    floor_id=floor, field=field, fallback_m=row["value_m"], references=row["references"],
                    floor_references=row.get("floor_references", []), missing_task_ids=row.get("missing_task_ids", [])))
    return resolved, issues


def apply_resolved_levels(plan, resolution):
    updated, _ = resolve_plan_lengths(plan)
    if any(resolution[field]["value_m"] is None for field in ("z_floor", "ceiling_height")):
        raise ValueError("assembly requires resolved floor base and top; 2D readings contain no fallback heights")
    delta = resolution["z_floor"]["value_m"] - updated.get("z_floor", resolution["z_floor"]["value_m"])
    for field in ("z_floor", "ceiling_height"):
        updated[field] = resolution[field]["value_m"]
    # Legacy BIM drafts use provisional relative heights. Explicit Z in a 2D
    # reading is an absolute observation and must survive floor resolution.
    # Never clip an opening to fit a shorter storey.
    if delta and updated.get("reading_type") != "plan":
        for row in updated.get("openings", []):
            if "z" in row:
                row["z"] = [z + delta for z in row["z"]]
    text = "Role level resolution (absolute metres): " + json.dumps(
        resolution, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    updated["basis"] = updated.get("basis", "") + "\n" + text
    updated.setdefault("assumptions", []).append(text)
    return updated


def apply_levels(registry, plan, *, z_floor=None, z_floor_evidence=None,
                 ceiling_height=None, ceiling_height_evidence=None):
    updated, citations = copy.deepcopy(plan), {}
    for field, value, reference in (("z_floor", z_floor, z_floor_evidence),
                                     ("ceiling_height", ceiling_height, ceiling_height_evidence)):
        if value is None and reference is None:
            continue
        if (isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value)
                or not isinstance(reference, dict) or set(reference) != {"task_id", "elevation_id"}):
            raise ValueError(f"{field} needs finite metres and {field}_evidence with task_id + elevation_id")
        artifact = registry.read(reference["task_id"], role_id="elevation_reader")
        level = next((row for row in artifact["elevations"] if row["id"] == reference["elevation_id"]), None)
        if level is None:
            raise ValueError(f"{field}_evidence refers to an unknown elevation_id")
        if field == "z_floor" and level.get("floor_id") not in (None, plan["floor_id"]):
            raise ValueError(f"{field}_evidence belongs to a different floor")
        expected = level["value_m"] if field == "z_floor" else level["value_m"] - updated["z_floor"]
        if not math.isclose(value, expected, rel_tol=0, abs_tol=1e-8):
            raise ValueError(f"{field}={value} disagrees with cited absolute level; expected {expected} m "
                             "(ceiling_height = cited top Z minus floor Z)")
        if field == "ceiling_height" and value <= 0:
            raise ValueError("ceiling_height must be positive")
        citations[field] = {**reference, "value_m": value, "previous_value_m": plan.get(field),
            "artifact_sha256": registry.records[reference["task_id"]]["artifact"]["sha256"],
            "image": artifact["image"], "level": copy.deepcopy(level)}
        updated[field] = value
    if citations:
        # Basis survives in compilation metadata; the assumptions/evidence list
        # also travels into the source candidate, including later assemblies.
        # This resolution explicitly supersedes, not erases, the reader guess.
        resolved = "Role elevation levels override plan assumptions: " + json.dumps(
            citations, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        updated["basis"] = updated.get("basis", "") + "\n" + resolved
        updated.setdefault("assumptions", []).append(resolved)
    return updated, citations
