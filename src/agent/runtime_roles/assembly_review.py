"""Compare assembly facts against accepted reader trials, with scoped review receipts."""

from __future__ import annotations

import hashlib
import json
import math

from src.agent_runtime.store import json_bytes

from .lineage import candidate_readers, current_plan_deliveries
from .plan_reading import product_identity, comparison_facts


def _identity(value, floor):
    return value.removeprefix(floor + ":") if isinstance(value, str) else value


def _ring(points):
    rows = [tuple(round(float(v), 8) for v in point[:2]) for point in points]
    if rows and rows[-1] == rows[0]:
        rows.pop()
    return min(tuple(rows[i:] + rows[:i]) for i in range(len(rows))) if rows else ()


def floor_facts(source, floor):
    spaces = {row["id"]: row for row in source["spaces"] if row["floor_id"] == floor}
    rooms = {_identity(key, floor): _ring(row["polygon"]) for key, row in spaces.items()}
    openings = {}
    for row in source["openings"]:
        if not set(row.get("space_ids", [])) & set(spaces):
            continue
        openings[_identity(row["id"], floor)] = {"kind": row["kind"],
            "xy": sorted({tuple(round(float(v), 8) for v in point[:2]) for point in row["vertices"]}),
            "space_ids": sorted(_identity(key, floor) for key in row["space_ids"]),
            "exterior": row.get("exterior")}
    adjacent = sorted({tuple(sorted(_identity(key, floor) for key in row["space_ids"]))
                       for row in source.get("boundary_relations", [])
                       if len(row["space_ids"]) == 2 and set(row["space_ids"]) <= set(spaces)})
    return {"room_count": len(rooms), "opening_count": len(openings),
            "rooms": rooms, "openings": openings, "adjacency": adjacent}


def compare_floor(expected, actual, floor):
    before, after = floor_facts(expected, floor), floor_facts(actual, floor)
    changes = []
    for key in ("room_count", "opening_count", "adjacency"):
        if before[key] != after[key]:
            changes.append({"floor_id": floor, "item": key, "before": before[key], "after": after[key]})
    for key in ("rooms", "openings"):
        for identity in sorted(set(before[key]) | set(after[key])):
            if before[key].get(identity) != after[key].get(identity):
                changes.append({"floor_id": floor, "item": key + ":" + identity,
                                "before": before[key].get(identity), "after": after[key].get(identity)})
    for row in changes:
        row["change_id"] = hashlib.sha256(json_bytes(row)).hexdigest()[:20]
    return changes


_REGULARIZATION_COORDINATE_TOLERANCE_M = 2e-6
_VIEW_COVERAGE_MARGIN_PX = 2


def _review_identity(facts):
    """Bind decisions only to inputs and findings that can change safety."""
    material = {key: facts[key] for key in ("bindings", "changes", "blockers")}
    return hashlib.sha256(json_bytes(material)).hexdigest()


def _regularization_opening_adjustments(row):
    """Validate explicit crossing-opening movements from the hashed audit."""
    crossing_values = row.get("adjusted_crossing_opening_ids", [])
    raw = row.get("opening_adjustments", [])
    if (not isinstance(crossing_values, list) or not isinstance(raw, list)
            or any(not isinstance(identity, str) for identity in crossing_values)
            or len(set(crossing_values)) != len(crossing_values)):
        return None
    crossing = set(crossing_values)
    adjustments = {}
    for value in raw:
        if not isinstance(value, dict) or not isinstance(value.get("opening_id"), str):
            return None
        identity, mode = value["opening_id"], value.get("mode")
        if identity in adjustments or identity not in crossing or mode not in {
                "translate_preserve_width", "open_passage_boundary_follow"}:
            return None
        ends = {}
        for stage in ("before", "after"):
            item = value.get(stage)
            if (not isinstance(item, dict)
                    or set(item) != {"p1", "p2", "world_p1", "world_p2", "width_m"}):
                return None
            if (isinstance(item["width_m"], bool)
                    or not isinstance(item["width_m"], (int, float))
                    or not math.isfinite(float(item["width_m"])) or item["width_m"] < 0):
                return None
            for key in ("p1", "p2", "world_p1", "world_p2"):
                point = item[key]
                if (not isinstance(point, (list, tuple)) or len(point) < 2
                        or any(isinstance(number, bool) or not isinstance(number, (int, float))
                               or not math.isfinite(float(number)) for number in point[:2])):
                    return None
            ends[stage] = item
        deltas = value.get("jamb_deltas_m")
        width_change = value.get("width_change_m")
        if (not isinstance(deltas, dict) or set(deltas) != {"p1", "p2"}
                or any(isinstance(number, bool) or not isinstance(number, (int, float))
                       or not math.isfinite(float(number)) for number in deltas.values())
                or isinstance(width_change, bool) or not isinstance(width_change, (int, float))
                or not math.isfinite(float(width_change))
                or not math.isclose(float(ends["after"]["width_m"])
                                    - float(ends["before"]["width_m"]), float(width_change),
                                    abs_tol=_REGULARIZATION_COORDINATE_TOLERANCE_M)):
            return None
        axis_index = 0 if row.get("axis") == "x" else 1
        for key in ("p1", "p2"):
            before_world = ends["before"]["world_" + key]
            after_world = ends["after"]["world_" + key]
            if (not math.isclose(float(after_world[axis_index]) - float(before_world[axis_index]),
                                 float(deltas[key]),
                                 abs_tol=_REGULARIZATION_COORDINATE_TOLERANCE_M)
                    or not math.isclose(float(after_world[1 - axis_index]),
                                        float(before_world[1 - axis_index]),
                                        abs_tol=_REGULARIZATION_COORDINATE_TOLERANCE_M)):
                return None
        for stage in ("before", "after"):
            world_width = math.dist(ends[stage]["world_p1"][:2], ends[stage]["world_p2"][:2])
            if not math.isclose(world_width, float(ends[stage]["width_m"]),
                                abs_tol=_REGULARIZATION_COORDINATE_TOLERANCE_M):
                return None
        if mode == "translate_preserve_width":
            if (value.get("inference") != {"basis": "entity_opening_width_preserved"}
                    or not math.isclose(float(deltas["p1"]), float(deltas["p2"]),
                                        abs_tol=_REGULARIZATION_COORDINATE_TOLERANCE_M)
                    or float(width_change) != 0.0
                    or not math.isclose(float(ends["before"]["width_m"]),
                                        float(ends["after"]["width_m"]),
                                        abs_tol=_REGULARIZATION_COORDINATE_TOLERANCE_M)):
                return None
        else:
            inference = value.get("inference")
            boundary_ids = value.get("boundary_ids")
            before_offsets = value.get("boundary_offsets_before_m")
            after_offsets = value.get("boundary_offsets_after_m")
            if (inference != {
                    "basis": "structured_complete_open_passage_compatibility",
                    "state": "open", "structured_gap_support": True,
                    "complete_between_terminal_boundaries": True,
                    "hard_width_reference": False,
                    } or not isinstance(boundary_ids, dict) or set(boundary_ids) != {"p1", "p2"}
                    or any(not isinstance(item, str) for item in boundary_ids.values())
                    or not isinstance(before_offsets, dict) or set(before_offsets) != {"p1", "p2"}
                    or not isinstance(after_offsets, dict) or set(after_offsets) != {"p1", "p2"}
                    or any(isinstance(number, bool) or not isinstance(number, (int, float))
                           or not math.isfinite(float(number))
                           for number in [*before_offsets.values(), *after_offsets.values()])
                    or any(not math.isclose(float(before_offsets[key]), float(after_offsets[key]),
                                            abs_tol=_REGULARIZATION_COORDINATE_TOLERANCE_M)
                           for key in ("p1", "p2"))):
                return None
        adjustments[identity] = {
            "mode": mode,
            "before_world": sorted(tuple(float(number) for number in ends["before"][key][:2])
                                   for key in ("world_p1", "world_p2")),
            "after_world": sorted(tuple(float(number) for number in ends["after"][key][:2])
                                  for key in ("world_p1", "world_p2")),
        }
    return adjustments if set(adjustments) == crossing else None


def _valid_lite_grid_change(row):
    """Accept a grid audit only when its step, displacement and topology agree."""
    fields = ("from_m", "to_m", "movement_m", "grid_step_m")
    if any(isinstance(row.get(key), bool) or not isinstance(row.get(key), (int, float))
           or not math.isfinite(float(row[key])) for key in fields):
        return False
    before, after, movement, step = (float(row[key]) for key in fields)
    tolerance = _REGULARIZATION_COORDINATE_TOLERANCE_M
    return (step > 0
            and abs(movement) <= step / 2 + tolerance
            and math.isclose(after - before, movement, abs_tol=tolerance, rel_tol=0)
            and abs(after - round(after / step) * step) <= tolerance
            and row.get("before_relationships") is not None
            and row["before_relationships"] == row.get("after_relationships")
            and not row.get("blocked_collapses"))


def _regularization_moves(report, floor):
    """Return audited coordinate substitutions for one floor, or no exemptions.

    Only the deterministic plan-stack rule is eligible. Unknown change types,
    malformed distances, collapses, or an observed topology change make the
    whole report ineligible instead of weakening the reader-fact gate.
    """
    if (not isinstance(report, dict)
            or report.get("schema") not in {"plan_regularization_report_v1",
                                             "plan_stack_regularization_report_v1"}
            or report.get("rule_version") != "plan_regularization_v1"
            or report.get("status") != "pass"):
        return []
    coordinate_types = {"move_wall_line", "move_footprint_edge", "lite_grid_coordinate",
                        "merge_duplicate_wall_into_fixed_footprint",
                        "retain_openings_on_merged_wall"}
    moves = []
    for row in report.get("changes", []):
        if isinstance(row, dict) and row.get("type") in {"lite_grid_coordinate", "lite_grid_height"}:
            if not _valid_lite_grid_change(row):
                return []
            if row["type"] == "lite_grid_height":
                # floor_facts compares XY; validated Z changes do not waive any XY edit.
                continue
        if not isinstance(row, dict) or row.get("type") not in coordinate_types:
            return []
        axis, before, after = row.get("axis"), row.get("from_m"), row.get("to_m")
        distance = row.get("movement_m")
        span = row.get("span_m")
        if (axis not in {"x", "y"} or any(isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value)) for value in (before, after, distance))
                or not isinstance(span, (list, tuple)) or len(span) != 2
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(float(value)) for value in span)
                or not math.isclose(abs(float(after) - float(before)), abs(float(distance)),
                                    abs_tol=_REGULARIZATION_COORDINATE_TOLERANCE_M)
                or row.get("blocked_collapses")
                or ("before_relationships" in row and row.get("before_relationships") != row.get("after_relationships"))):
            return []
        if (row["type"] == "retain_openings_on_merged_wall"
                and (float(before) != float(after) or float(distance) != 0.0)):
            return []
        adjustments = _regularization_opening_adjustments(row)
        if adjustments is None:
            return []
        if row.get("floor_id") == floor:
            object_ids = row.get("object_ids") if isinstance(row.get("object_ids"), dict) else {}
            openings = {*row.get("opening_ids", []), *row.get("moved_opening_ids", []),
                        *object_ids.get("openings", [])}
            moves.append({"axis": axis, "from_m": float(before), "to_m": float(after),
                          "span_m": sorted(float(value) for value in span),
                          "opening_ids": openings,
                          "crossing_opening_ids": set(row.get("adjusted_crossing_opening_ids", [])),
                          "opening_adjustments": adjustments,
                          "collapse_backtrack": row["type"] == "merge_duplicate_wall_into_fixed_footprint",
                          "type": row["type"]})
    return moves


def _regularized_points(points, moves):
    result = []
    for point in points:
        moved = list(point)
        for row in moves:
            index = 0 if row["axis"] == "x" else 1
            along = moved[1 - index]
            if (math.isclose(moved[index], row["from_m"],
                             abs_tol=_REGULARIZATION_COORDINATE_TOLERANCE_M)
                    and row["span_m"][0] - _REGULARIZATION_COORDINATE_TOLERANCE_M <= along
                    <= row["span_m"][1] + _REGULARIZATION_COORDINATE_TOLERANCE_M):
                moved[index] = row["to_m"]
        result.append(tuple(moved))
    return result


def _points_close(left, right):
    return len(left) == len(right) and all(
        len(a) == len(b) and all(math.isclose(x, y, abs_tol=_REGULARIZATION_COORDINATE_TOLERANCE_M)
                                 for x, y in zip(a, b, strict=True))
        for a, b in zip(left, right, strict=True))


def _audited_zero_width_backtrack(left, middle, right, moves):
    """Recognize only the exact zero-width excursion created by a strip merge."""
    for row in moves:
        if not row.get("collapse_backtrack"):
            continue
        index = 0 if row["axis"] == "x" else 1
        if not all(math.isclose(point[index], row["to_m"],
                                abs_tol=_REGULARIZATION_COORDINATE_TOLERANCE_M)
                   for point in (left, middle, right)):
            continue
        along = [point[1 - index] for point in (left, middle, right)]
        lo, hi = row["span_m"]
        if ((math.isclose(along[1], lo, abs_tol=_REGULARIZATION_COORDINATE_TOLERANCE_M)
             and any(math.isclose(value, hi, abs_tol=_REGULARIZATION_COORDINATE_TOLERANCE_M)
                     for value in (along[0], along[2])))
                or (math.isclose(along[1], hi, abs_tol=_REGULARIZATION_COORDINATE_TOLERANCE_M)
                    and any(math.isclose(value, lo, abs_tol=_REGULARIZATION_COORDINATE_TOLERANCE_M)
                            for value in (along[0], along[2])))):
            return True
    return False


def _simplified_ring(points, *, backtrack_moves=()):
    """Canonicalize a ring while ignoring duplicate/collinear tessellation points."""
    rows = [tuple(float(value) for value in point[:2]) for point in points]
    compact = []
    for point in rows:
        if not compact or not _points_close([compact[-1]], [point]):
            compact.append(point)
    if len(compact) > 1 and _points_close([compact[0]], [compact[-1]]):
        compact.pop()
    changed = True
    while changed and len(compact) > 3:
        changed = False
        for index, middle in enumerate(compact):
            left, right = compact[index - 1], compact[(index + 1) % len(compact)]
            cross = ((middle[0] - left[0]) * (right[1] - left[1])
                     - (middle[1] - left[1]) * (right[0] - left[0]))
            baseline = math.hypot(right[0] - left[0], right[1] - left[1])
            between = all(min(a, c) - _REGULARIZATION_COORDINATE_TOLERANCE_M <= b
                          <= max(a, c) + _REGULARIZATION_COORDINATE_TOLERANCE_M
                          for a, b, c in zip(left, middle, right, strict=True))
            audited_backtrack = (not between and _audited_zero_width_backtrack(
                left, middle, right, backtrack_moves
            ))
            if (abs(cross) <= _REGULARIZATION_COORDINATE_TOLERANCE_M * baseline
                    and (between or audited_backtrack)):
                compact.pop(index)
                changed = True
                break
    return _ring(compact)


def _regularized_ring(points, moves):
    """Apply audits in order, collapsing a merge excursion before later moves."""
    result = [tuple(point) for point in points]
    applied = []
    for row in moves:
        result = _regularized_points(result, [row])
        applied.append(row)
        compact = []
        for point in result:
            if not compact or not _points_close([compact[-1]], [point]):
                compact.append(point)
        if len(compact) > 1 and _points_close([compact[0]], [compact[-1]]):
            compact.pop()
        changed = True
        while changed and len(compact) > 3:
            changed = False
            for index, middle in enumerate(compact):
                left, right = compact[index - 1], compact[(index + 1) % len(compact)]
                cross = ((middle[0] - left[0]) * (right[1] - left[1])
                         - (middle[1] - left[1]) * (right[0] - left[0]))
                baseline = math.hypot(right[0] - left[0], right[1] - left[1])
                if (abs(cross) <= _REGULARIZATION_COORDINATE_TOLERANCE_M * baseline
                        and _audited_zero_width_backtrack(left, middle, right, applied)):
                    compact.pop(index)
                    changed = True
                    break
        result = compact
    return _simplified_ring(result)


def _regularized_opening_options(points, moves, identity):
    """Replay only explicit audited opening movements, preserving their order."""
    options = [list(tuple(point) for point in points)]
    for row in moves:
        adjustment = row["opening_adjustments"].get(identity)
        if adjustment is None:
            moved = [_regularized_points(option, [row]) for option in options]
        else:
            moved = []
            for option in options:
                if _points_close(sorted(option), adjustment["before_world"]):
                    moved.append(adjustment["after_world"])
        unique = {}
        for option in moved:
            unique[tuple(sorted(option))] = option
        options = list(unique.values())
    return [sorted(option) for option in options]


def accepted_regularization_changes(changes, report, floor):
    """Separate exact, audited deterministic movement from reader geometry edits."""
    moves = _regularization_moves(report, floor)
    if not moves:
        return [], changes
    accepted, remaining = [], []
    moved_openings = set().union(*(row["opening_ids"] for row in moves))
    for change in changes:
        before, after = change.get("before"), change.get("after")
        item = change.get("item", "")
        allowed = False
        if item.startswith("rooms:") and before is not None and after is not None:
            transformed = _regularized_ring(before, moves)
            allowed = _points_close(transformed, _simplified_ring(after))
        elif item.startswith("openings:") and before is not None and after is not None:
            identity = item.split(":", 1)[1]
            unchanged = {key: before.get(key) for key in ("kind", "space_ids", "exterior")} == {
                key: after.get(key) for key in ("kind", "space_ids", "exterior")}
            transformed = _regularized_opening_options(before.get("xy", []), moves, identity)
            allowed = (identity in moved_openings and unchanged
                       and any(_points_close(option, after.get("xy", [])) for option in transformed))
        if allowed:
            accepted.append({**change, "classification": "deterministic_regularization"})
        else:
            remaining.append(change)
    return accepted, remaining


class AssemblyReview:
    def __init__(self, session):
        self.session, self.store = session, session.store

    def _load(self, name, default):
        path = self.store.directory / name
        return json.loads(path.read_bytes()) if path.is_file() else default

    def bind(self, task_id):
        value = self.session.registry.read(task_id, role_id="plan_reader")
        record = self.session.registry.records[task_id]
        _, digest = product_identity(record.get("validation", {}))
        if not digest:
            raise ValueError("reader trial is missing its validated product hash")
        floors = self._load("role_floor_sources.json", {})
        floors[value["plan"]["floor_id"]] = {"task_id": task_id,
            "artifact_sha256": record["artifact"]["sha256"],
            "source_sha256": digest}
        self.store.write_json("role_floor_sources.json", floors)

    def trial_facts(self, task_id, binding):
        receipt = self.session.registry.records[task_id]["validation"]
        workspace = self.session.registry.child(task_id).task_directory / "bim/trial_workspace"
        filename, digest = product_identity(receipt)
        path = (workspace / filename).resolve()
        if not path.is_relative_to(workspace.resolve()) or not path.is_file():
            raise ValueError("accepted trial product is missing or outside its task")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest or digest != binding["source_sha256"]:
            raise ValueError("accepted trial source hash changed")
        product = json.loads(raw)
        return comparison_facts(product) if receipt.get("reading_product_file") else product

    @staticmethod
    def _identified(row, field="change_id"):
        row[field] = hashlib.sha256(json_bytes(row)).hexdigest()[:20]
        return row

    def _delivery_coverage(self, candidate, actual_floors):
        current = current_plan_deliveries(self.session)
        used = candidate_readers(self.session, candidate)
        used_by_target = {}
        blockers = []
        for task_id in sorted(used):
            row = self.session.registry.records.get(task_id) or {}
            if row.get("role_id") != "plan_reader":
                continue
            target = row.get("target")
            used_by_target.setdefault(target, []).append(task_id)
            expected = current.get(target)
            if expected and task_id != expected["task_id"]:
                blockers.append(self._identified({
                    "item": "stale_plan_delivery",
                    "target": target,
                    "floor_id": expected["floor_id"],
                    "used_task_id": task_id,
                    "current_task_id": expected["task_id"],
                }, "blocker_id"))

        changes = []
        for target, expected in current.items():
            floor = expected["floor_id"]
            if floor not in actual_floors:
                changes.append(self._identified({
                    "floor_id": floor,
                    "item": "missing_required_floor",
                    "target": target,
                    "before": {
                        "task_id": expected["task_id"],
                        "artifact_sha256": expected["artifact_sha256"],
                    },
                    "after": None,
                }))
            elif not used_by_target.get(target):
                blockers.append(self._identified({
                    "item": "unverified_plan_lineage",
                    "target": target,
                    "floor_id": floor,
                    "current_task_id": expected["task_id"],
                    "used_task_ids": used_by_target.get(target, []),
                }, "blocker_id"))

        scope = [current[target] for target in sorted(current)]
        return scope, sorted(used), changes, blockers

    def _regularization_report(self, candidate):
        """Find the hashed assembly receipt inherited by this candidate."""
        seen = set()
        while candidate and candidate not in seen:
            seen.add(candidate)
            source = self.session._source(candidate)
            provenance = source.get("generation", {}).get("provenance", {})
            assembly = provenance.get("plan_assembly")
            plan_input = provenance.get("plan_input")
            reference = assembly if isinstance(assembly, dict) else None
            direct_report = False
            if reference is None and isinstance(plan_input, dict):
                reference = plan_input.get("regularization_report")
                direct_report = True
            if isinstance(reference, dict) and isinstance(reference.get("file"), str):
                path = (self.session.run_directory / reference["file"]).resolve()
                if (not path.is_relative_to(self.session.run_directory.resolve()) or not path.is_file()
                        or hashlib.sha256(path.read_bytes()).hexdigest() != reference.get("sha256")):
                    raise ValueError("regularization receipt is missing, outside the run, or changed")
                receipt = json.loads(path.read_bytes())
                return receipt if direct_report else self._assembly_regularization(receipt)
            candidate = provenance.get("parent_candidate")
        return None

    def _assembly_regularization(self, receipt):
        """Merge hash-bound single-floor audits before the stack audit."""
        stack = receipt.get("regularization") if isinstance(receipt, dict) else None
        floors = receipt.get("floors") if isinstance(receipt, dict) else None
        if floors is None:
            return stack
        if not isinstance(floors, list) or not isinstance(stack, dict):
            raise ValueError("plan assembly regularization receipt is malformed")
        draft_root = (self.session.run_directory / "plan_drafts").resolve()
        reports = []
        for row in floors:
            if not isinstance(row, dict) or not all(
                    isinstance(row.get(key), str)
                    for key in ("draft_id", "expected_plan_sha256", "floor_id")):
                raise ValueError("plan assembly floor receipt is malformed")
            plan_path = (draft_root / row["draft_id"] / "plan.json").resolve()
            if not plan_path.is_relative_to(draft_root) or not plan_path.is_file():
                raise ValueError("plan assembly floor plan is missing or outside the run")
            raw = plan_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != row["expected_plan_sha256"]:
                raise ValueError("plan assembly floor plan changed")
            plan = json.loads(raw)
            if plan.get("floor_id") != row["floor_id"]:
                raise ValueError("plan assembly floor identity changed")
            if isinstance(plan.get("regularization"), dict):
                reports.append(plan["regularization"])
        floor_reports = stack.get("floor_reports", {})
        if floor_reports is not None and not isinstance(floor_reports, dict):
            raise ValueError("plan assembly floor reports are malformed")
        for row in floors:
            report = (floor_reports or {}).get(row["floor_id"])
            if isinstance(report, dict):
                reports.append(report)
        reports.append(stack)
        if any(report.get("schema") not in {"plan_regularization_report_v1",
                                               "plan_stack_regularization_report_v1"}
               or report.get("rule_version") != "plan_regularization_v1"
               or report.get("status") != "pass"
               or not isinstance(report.get("changes", []), list)
               for report in reports):
            return None
        return {"schema": "plan_stack_regularization_report_v1",
                "rule_version": "plan_regularization_v1", "status": "pass",
                "changes": [change for report in reports for change in report.get("changes", [])]}

    def check(self, candidate, *, require_all=False):
        bindings = self._load("role_floor_sources.json", {})
        if not bindings and not require_all:
            return None
        source = self.session._source(candidate)
        actual_floors = {row["floor_id"] for row in source["spaces"]}
        changes, regularization_changes, checked, blockers = [], [], [], []
        regularization = self._regularization_report(candidate)
        for floor, binding in bindings.items():
            if floor not in actual_floors and not require_all:
                continue
            task_id = binding["task_id"]
            self.session.registry.read(task_id, sha256=binding["artifact_sha256"], role_id="plan_reader")
            floor_changes = compare_floor(self.trial_facts(task_id, binding), source, floor)
            accepted, floor_changes = accepted_regularization_changes(floor_changes, regularization, floor)
            regularization_changes.extend(accepted)
            changes.extend(floor_changes)
            checked.append(floor)
        if require_all:
            for floor in sorted(actual_floors - set(bindings)):
                row = {"floor_id": floor, "item": "unexpected_floor", "before": None,
                       "after": floor_facts(source, floor)}
                changes.append({**row, "change_id": hashlib.sha256(json_bytes(row)).hexdigest()[:20]})
            delivery_scope, used_plan_tasks, missing, blockers = self._delivery_coverage(
                candidate, actual_floors
            )
            changes.extend(missing)
        else:
            delivery_scope, used_plan_tasks = [], []
        facts = {"candidate": candidate, "require_all": require_all, "source_sha256": hashlib.sha256(
            (self.session.run_directory / candidate / "source_model.json").read_bytes()).hexdigest(),
            "bindings": bindings, "checked_floors": checked, "changes": changes,
            "regularization_changes": regularization_changes,
            "delivery_scope": delivery_scope, "used_plan_tasks": used_plan_tasks,
            "blockers": blockers}
        # Candidate hashes, checked floors, accepted regularization, and caller
        # scope are diagnostic. Missing floors and lineage remain material
        # because they are represented by changes/blockers.
        identity = _review_identity(facts)
        old = self._load("role_assembly_reviews/" + identity + ".json", {})
        status = "blocked" if blockers else ("needs_review" if changes else "unchanged")
        report = {**facts, "review_id": identity, "status": status}
        if not blockers and old.get("decisions") and self._valid_decisions(changes, old["decisions"]):
            report.update(status="reviewed", decisions=old["decisions"])
        self.store.write_json("role_assembly_reviews/" + identity + ".json", report)
        self.store.write_json("role_assembly_current.json", {"review_id": identity})
        return report

    def current(self):
        current = self._load("role_assembly_current.json", {})
        return self._load("role_assembly_reviews/" + current.get("review_id", "missing") + ".json", None)

    @staticmethod
    def _valid_decisions(changes, decisions):
        if not isinstance(decisions, list) or any(
                not isinstance(row, dict) or set(row) != {"change_id", "reason"}
                or not isinstance(row["reason"], str) or not row["reason"].strip() for row in decisions):
            return False
        expected = {row["change_id"] for row in changes}
        supplied = {row["change_id"] for row in decisions}
        return len(supplied) == len(decisions) and supplied == expected

    def guard(self):
        report = self.current()
        if report and report["status"] == "blocked":
            raise ValueError("assembly cannot be delivered because its plan lineage is stale or unverified: "
                             + json.dumps(report["blockers"], ensure_ascii=False, sort_keys=True))
        if report and report["status"] == "needs_review":
            raise ValueError("assembly changed accepted reader facts; inspect changes and call review_role_assembly "
                             "with a reason for each change before continuing: " + report["review_id"])

    def acknowledge(self, review_id, decisions):
        report = self.current()
        if not report or report["review_id"] != review_id:
            raise ValueError("review_id must refer to the current assembly comparison")
        if report.get("blockers"):
            raise ValueError("stale or unverified plan lineage cannot be accepted as partial delivery")
        refreshed = self.check(report["candidate"], require_all=report["require_all"])
        if refreshed["review_id"] != review_id:
            raise ValueError("assembly source or reader references changed; review the new comparison")
        if not self._valid_decisions(report["changes"], decisions):
            raise ValueError("provide exactly one nonempty reason for each assembly change_id")
        report.update(status="reviewed", decisions=decisions)
        self.store.write_json("role_assembly_reviews/" + review_id + ".json", report)
        return report


class PositionReview:
    """Durable, evidence-bound plan/elevation choices; no geometry averaging."""

    def __init__(self, session):
        self.session = session

    def current(self):
        return self.session.assembly._load("role_position_review.json", {"items": {}})

    def _save(self, value):
        for key, row in value["items"].items():
            self.session.store.write_json("role_position_items/" + key + ".json", row)
        self.session.store.write_json("role_position_review.json", value)

    def bind(self, candidate, references, *, assembly_id, position_revision=None):
        scope = {"task_ids": [row["task_id"] for row in references], "assembly_id": assembly_id,
                 "assembled_position_revision": position_revision}
        # finish_bim selects a candidate, so the latest explicit assembly of
        # that candidate is active. Replaying an older assembly reactivates its
        # own scope instead of consulting globally newer facade deliveries.
        self.session.store.write_json("role_position_scopes/" + assembly_id + "/" + candidate + ".json", scope)
        self.session.store.write_json("role_position_scopes/" + candidate + ".json", scope)
        state = self.current()
        state["items"] = {key: row for key, row in state["items"].items()
                          if row["elevation_task_id"] in scope["task_ids"]}
        self._save(state)

    def _scope_record(self, candidate):
        seen = set()
        while candidate and candidate not in seen:
            seen.add(candidate)
            self.session._source(candidate)
            scope = self.session.assembly._load("role_position_scopes/" + candidate + ".json", None)
            if scope is not None:
                return scope
            path = self.session.run_directory / candidate / "report.json"
            report = json.loads(path.read_bytes()) if path.is_file() else {}
            candidate = (report.get("provenance") or {}).get("parent_candidate")
        return None

    def _scope(self, candidate):
        record = self._scope_record(candidate)
        return record["task_ids"] if record else None

    def revision(self):
        decisions = {key: row.get("decision") for key, row in self.current()["items"].items()}
        return hashlib.sha256(json_bytes(decisions)).hexdigest()

    @staticmethod
    def _opening(source, identity):
        opening = next((row for row in source["openings"] if row["id"] == identity), None)
        if opening is None:
            raise ValueError("position review opening no longer exists: " + identity)
        return opening

    @staticmethod
    def _span(opening, axis):
        index = 0 if axis == "x" else 1
        return [min(p[index] for p in opening["vertices"]), max(p[index] for p in opening["vertices"])]

    def _plan(self, comparison):
        floor, identity = comparison["floor_id"], comparison["source_opening_id"]
        binding = self.session.assembly._load("role_floor_sources.json", {}).get(floor)
        if not binding:
            return {"task_id": None, "image": None, "bbox": None,
                    "span_m": comparison["plan_span_m"], "basis": "candidate; reader evidence unavailable"}
        task = binding["task_id"]
        artifact = self.session.registry.read(task, sha256=binding["artifact_sha256"], role_id="plan_reader")
        receipt = self.session.registry.records[task]["validation"]
        seed_id = identity.removeprefix(floor + ":")
        source = self.session.assembly.trial_facts(task, binding)
        opening = next((row for row in source["openings"] if row["id"] in {identity, seed_id}), None)
        evidence = [row for row in artifact.get("evidence", []) if row.get("item") == "plan.openings:" + seed_id]
        located = next((row for row in evidence if row.get("bbox")), {})
        return {"task_id": task, "artifact_sha256": binding["artifact_sha256"],
                "source_sha256": binding["source_sha256"], "candidate": receipt["candidate"],
                "image": self.session.registry.records[task]["image"], "bbox": located.get("bbox"),
                "span_m": self._span(opening, comparison["world_axis"]) if opening else None,
                "basis": "accepted plan trial" if opening else "opening absent from accepted plan trial",
                "evidence": evidence}

    def record(self, task_id, candidate, result):
        from .elevation import compare_opening_positions, attach_ink_review
        source = self.session._source(candidate)
        artifact_sha = self.session.registry.records[task_id]["artifact"]["sha256"]
        state = self.current()
        state["items"] = {key: row for key, row in state["items"].items() if row["elevation_task_id"] != task_id}
        for comparison in result.get("position_comparisons", []):
            plan = self._plan(comparison)
            independent = (compare_opening_positions(plan["span_m"], comparison["elevation_span_m"])
                if plan["span_m"] is not None else {
                    "plan_span_m": None, "plan_width_m": None, "differences_m": None,
                    "max_difference_m": None, "status": "reread", "bucket": "unavailable",
                    "requires_both_views": False})
            row = {**comparison, **independent, "candidate_span_m": comparison["plan_span_m"], "plan_evidence": plan,
                   "elevation_task_id": task_id, "elevation_artifact_sha256": artifact_sha}
            attach_ink_review(row, comparison.get("ink_inconsistencies", []))
            # A height-only candidate change must not invalidate a choice. The
            # two independent observations, including their evidence, do bind it.
            identity = hashlib.sha256(json_bytes({key: row[key] for key in (
                "source_opening_id", "artifact_opening_id", "floor_id", "world_axis", "plan_evidence",
                "elevation_task_id", "elevation_artifact_sha256", "elevation_span_m")})).hexdigest()[:24]
            row["decision_id"] = identity
            row["candidate"] = candidate
            opening = self._opening(source, row["source_opening_id"])
            row["host_boundary_id"] = opening["host_boundary_id"]
            row["space_ids"] = opening["space_ids"]
            previous = state["items"].get(identity) or self.session.assembly._load(
                "role_position_items/" + identity + ".json", {})
            if previous.get("decision"):
                row["decision"] = previous["decision"]
                row["status"] = previous["status"]
                row["host_boundary_id"] = previous["host_boundary_id"]
                row["space_ids"] = previous["space_ids"]
            elif row["bucket"] == "10_30cm":
                row["decision"] = {
                    "choice": "keep_plan",
                    "reason": "10-30 cm plan/elevation difference: retain the accepted plan position by policy.",
                    "views": [],
                    "plan_evidence": row["plan_evidence"],
                    "elevation_evidence": row["elevation_evidence"],
                    "automatic": True,
                }
                row["status"] = "decided"
            for key, old in list(state["items"].items()):
                if (old["orientation"], old["source_opening_id"]) == (row["orientation"], row["source_opening_id"]):
                    del state["items"][key]
            state["items"][identity] = row
        self._save(state)

    def refresh(self, candidate):
        from .assembly import select_deliveries
        from .elevation import match_elevation
        # Preserve compatibility with partial/offline sessions without a plan
        # delivery, but check any rows already established in those sessions.
        try:
            _, elevations, _ = select_deliveries(self.session, self._scope(candidate))
        except ValueError as error:
            if "no validated plan delivery" not in str(error):
                raise
            elevations = {}
        if elevations:
            state = self.current()
            state["items"] = {key: row for key, row in state["items"].items()
                              if row["elevation_task_id"] in elevations}
            self._save(state)
            source = self.session._source(candidate)
            for task_id, artifact in elevations.items():
                self.record(task_id, candidate, match_elevation(source, artifact, candidate=candidate))
        return self.current()

    def resolve_match(self, task_id, result):
        """An explicit position choice establishes identity, never waives Z bounds."""
        choices = {row["source_opening_id"]: row for row in self.current()["items"].values()
                   if row["elevation_task_id"] == task_id and row["status"] == "decided"}
        remaining, resolved = [], []
        for conflict in result["conflicts"]:
            row = choices.get(conflict.get("source_opening_id"))
            if (row and conflict.get("type") == "position_or_width_conflict" and not conflict.get("ambiguous")
                    and row["artifact_opening_id"] == conflict["artifact_opening_id"]):
                accepted = {k: v for k, v in conflict.items() if k != "type"}
                accepted.update(status="matched", position_decision_id=row["decision_id"])
                result["matches"].append(accepted)
                resolved.append({"decision_id": row["decision_id"], "opening": row["source_opening_id"]})
            else:
                remaining.append(conflict)
        result["conflicts"] = remaining
        if resolved:
            result["resolved_position_conflicts"] = resolved
        result["can_apply"] = bool(result["matches"])

    def summary(self):
        rows = sorted(self.current()["items"].values(), key=lambda row: (
            -max(row["max_difference_m"] if row["max_difference_m"] is not None else float("inf"),
                 row.get("evidence_max_difference_m", 0)), row["decision_id"]))
        pending = [{"decision_id": row["decision_id"], "opening": row["source_opening_id"],
            "facade": row["orientation"], "difference_cm": (
                round(row["max_difference_m"] * 100, 1) if row["max_difference_m"] is not None else None),
            "ink_difference_cm": round(row.get("ink_max_difference_m", 0) * 100, 1),
            "ink_issue_count": len(row.get("ink_inconsistencies", [])),
            "numeric_width_conflict": bool(row.get("width_span_conflict")),
            "both_views_required": row["requires_both_views"], "status": row["status"]}
            for row in rows if row["status"] in {"pending", "reread"}]
        return {"pending": pending,
                "delivery_blocking": list(pending),
                "delivery_defaults": [],
                "receipt_file": "role_position_review.json"}

    def guard(self, candidate):
        state = self.refresh(candidate)
        scope = self._scope_record(candidate)
        if scope and scope.get("assembled_position_revision") != self.revision():
            raise ValueError("position decisions changed; re-call assemble_from_readers before delivery")
        source = self.session._source(candidate)
        for row in state["items"].values():
            opening = self._opening(source, row["source_opening_id"])
            choice = row.get("decision", {}).get("choice")
            expected = row["elevation_span_m"] if choice == "use_elevation" else row["plan_span_m"]
            if (row["status"] in {"pending", "reread"}
                    or any(abs(a - b) > 1e-7 for a, b in zip(self._span(opening, row["world_axis"]), expected))):
                raise ValueError("position decision required before delivery: " + row["decision_id"])
            if opening["host_boundary_id"] != row["host_boundary_id"] or opening["space_ids"] != row["space_ids"]:
                raise ValueError("position decision cannot change host or connectivity")

    def _views(self, row, view_ids):
        """Views must be persisted original-image views covering both evidence boxes."""
        views = []
        from pathlib import Path
        for identity in view_ids:
            if Path(identity).name != identity or not identity.startswith("view_"):
                raise ValueError("view_ids must name saved original-image views")
            path = self.session.run_directory / "image_views" / (identity.removesuffix(".json") + ".json")
            if not path.is_file():
                raise ValueError("original view does not exist: " + identity)
            view = json.loads(path.read_bytes())
            views.append(view)
        if row["requires_both_views"]:
            for side in ("plan_evidence", "elevation_evidence"):
                evidence = row[side]
                image, box = evidence.get("image"), evidence.get("bbox")
                if not image or not box:
                    raise ValueError("over 30 cm requires located evidence from both readers; re-read missing evidence")
                manifest = self.session.manifest["images"][image]
                if hashlib.sha256((self.session.run_directory / "images" / image).read_bytes()).hexdigest() != manifest["sha256"]:
                    raise ValueError("position evidence image changed")
                if not any(v.get("name") == image and v.get("image_sha256") == manifest["sha256"]
                           and (b := v.get("box_original_pixels"))
                           and b[0] <= box[0] + _VIEW_COVERAGE_MARGIN_PX
                           and b[1] <= box[1] + _VIEW_COVERAGE_MARGIN_PX
                           and b[2] >= box[2] - _VIEW_COVERAGE_MARGIN_PX
                           and b[3] >= box[3] - _VIEW_COVERAGE_MARGIN_PX for v in views):
                    raise ValueError("over 30 cm: view_image must cover both reader evidence boxes; supply their view_ids")
        return views

    async def decide(self, candidate, edits):
        from .session import envelope
        from .lineage import metadata
        request = {"candidate": candidate, "edits": edits}
        identity = hashlib.sha256(json_bytes(request)).hexdigest()
        path = "role_position_actions/" + identity + ".json"
        prior = self.session.assembly._load(path, None)
        if prior and prior.get("result"):
            return envelope(prior["result"])
        state = self.refresh(candidate)
        prepared, corrections, seen = [], [], set()
        source = self.session._source(candidate)
        for edit in edits:
            if set(edit) - {"action", "decision_id", "choice", "reason", "view_ids"}:
                raise ValueError("position_decision accepts decision_id, choice, reason and view_ids only")
            key = edit.get("decision_id")
            if key in seen or key not in state["items"]:
                raise ValueError("each position decision_id must be current and appear once")
            seen.add(key)
            row = state["items"][key]
            choice, reason = edit.get("choice"), edit["reason"].strip()
            if not reason or choice not in {"keep_plan", "use_elevation", "reread_plan", "reread_elevation"}:
                raise ValueError("position decision requires a choice and a concrete reason")
            if row.get("decision") and not row["decision"].get("automatic") and not prior:
                raise ValueError("position decision is already recorded; reassemble changed reader evidence")
            if row["plan_span_m"] is None and not choice.startswith("reread_"):
                raise ValueError("independent plan reading is unavailable; request a reader re-read with the missing opening")
            if (choice == "use_elevation" and row["elevation_evidence"]["evidence_type"] != "pixels"
                    and abs(row.get("reader_width_m", row["elevation_width_m"]) - row["elevation_width_m"]) > 1e-7):
                raise ValueError("retained numeric width conflicts with the pixel interval; keep plan or re-read before using elevation")
            # Requesting better evidence is allowed before views exist. Selecting
            # either side above 30 cm is allowed only after both were viewed.
            views = [] if choice.startswith("reread_") else self._views(row, edit.get("view_ids", []))
            opening = self._opening(source, row["source_opening_id"])
            if row["plan_span_m"] is not None and any(abs(a - b) > 1e-7
                    for a, b in zip(self._span(opening, row["world_axis"]), row["plan_span_m"])):
                raise ValueError("candidate position changed; reassemble before deciding")
            decision = {"choice": choice, "reason": reason, "views": views,
                        "plan_evidence": row["plan_evidence"], "elevation_evidence": row["elevation_evidence"]}
            if choice.startswith("reread_"):
                task_id = row["plan_evidence"].get("task_id") if choice == "reread_plan" else row["elevation_task_id"]
                decision["rework"] = {"previous_task_id": task_id, "issues": [reason]}
                if choice == "reread_plan":
                    decision["rework"]["rework_targets"] = ["plan.openings:" + row["source_opening_id"].removeprefix(row["floor_id"] + ":")]
            prepared.append((key, decision))
            if choice == "use_elevation":
                corrections.append({"action": "position", "id": row["source_opening_id"],
                    "along_start_m": row["elevation_span_m"][0], "along_end_m": row["elevation_span_m"][1],
                    "image": row["elevation_evidence"]["image"], "bbox": row["elevation_evidence"]["bbox"], "reason": reason})
        self.session.store.write_json(path, {"request": request})
        current = candidate
        if corrections:
            result = await self.session.edit_candidate(candidate, corrections)
            current = metadata(result)["candidate"]
        for key, decision in prepared:
            state["items"][key].update(decision=decision,
                status="reread" if decision["choice"].startswith("reread_") else "decided")
        self._save(state)
        result = {"status": "completed", "candidate": current, "source_geometry_ready": True,
                  "position_review": self.summary(), "decisions": [{"decision_id": k, **d} for k, d in prepared],
                  "next_action": "Re-call assemble_from_readers to refresh matches and safe heights; re-read choices need a corrected reader delivery first."}
        self.session.store.write_json(path, {"request": request, "result": result})
        return envelope(result)

    def write_delivery(self, candidate):
        """Role-only report attachment, also after fallback regenerates delivery."""
        import html
        self.guard(candidate)
        state = json.loads(json_bytes(self.current()))
        root = self.session.run_directory
        state["assembly_scope"] = self._scope_record(candidate)
        path = root / "delivery.json"
        if path.is_file():
            value = json.loads(path.read_bytes())
            value["position_review"] = state
            path.write_bytes(json_bytes(value))
        report = root / "position_review.json"
        report.write_bytes(json_bytes(state))
        page = root / "delivery.html"
        if page.is_file():
            text = page.read_text(encoding="utf-8")
            begin, end = "<!-- role-position-start -->", "<!-- role-position-end -->"
            if begin in text:
                text = text[:text.index(begin)] + text[text.index(end) + len(end):]
            section = begin + '<section><h2>平面与立面位置裁决</h2><pre>' + html.escape(
                json.dumps(state, ensure_ascii=False, indent=2)) + "</pre></section>" + end
            text = text.replace("</body>", section + "</body>") if "</body>" in text else text + section
            page.write_text(text, encoding="utf-8", newline="\n")


def finalize_role_building(engine, reason):
    """The timeout/stop fallback cannot silently deliver an unreviewed assembly."""
    from scripts.tool_scripts.bim_agent_budget import fallback_selection
    from scripts.tool_scripts.run_bim_agent import Toolkit
    from src.agent.runtime_delivery import finalize_runtime_building

    try:
        selection = engine.tools.run_directory / "delivery_selection.json"
        forced_fallback = reason.endswith(("time_budget_exhausted", "money_budget_exhausted")) or reason == "money_cny_usage_unavailable"
        if selection.is_file() and not forced_fallback:
            chosen = json.loads(selection.read_bytes())["candidate"]
        else:
            chosen, _ = fallback_selection(Toolkit(engine.tools.run_directory))
        if chosen:
            report = engine.tools.assembly.check(chosen, require_all=True)
            try:
                engine.tools.assembly.guard()
                engine.tools.positions.guard(chosen)
            except ValueError as error:
                if report and report["status"] == "needs_review":
                    return {"status": "assembly_review_required", "delivery": None,
                            "assembly_review": report}
                return {"status": "assembly_delivery_blocked", "delivery": None,
                        "assembly_review": report, "reason": str(error)}
            from .building_review import verify_review
            try:
                architectural_review = verify_review(engine.tools, chosen)
            except ValueError as error:
                return {"status": "architectural_review_required", "delivery": None, "reason": str(error)}
    except (ValueError, OSError, KeyError) as error:
        return {"status": "assembly_review_failed", "delivery": None, "reason": str(error)}
    result = finalize_runtime_building(engine, reason)
    if chosen and result.get("status") == "delivered":
        engine.tools.positions.write_delivery(chosen)
        from .building_review import attach_review
        attach_review(engine.tools, architectural_review)
    return result
