"""Materialize explicit plan door/passage observations after floor assembly.

This is a pure, offline adapter. Selected face pairs establish duplicate
observations; compiled wall references establish the support line; final room
edges establish both spaces. Prose and room names supply no geometry.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
from typing import Mapping

from shapely.geometry import LineString, Polygon
from shapely.geometry.polygon import orient

from src.agent.correction.cell_geometry import cell_polygon
from src.agent.correction.evidence_contract import resolve_json_pointer
from src.agent.correction.footprint import floor_footprint, floor_key
from src.agent.correction.schema import CorrectedGeometry, WallOpening
from src.agent.correction.wall_compiler import WallCompilationV1
from src.agent.correction.window_sources import canonical_sha256, source_locator
from src.agent.execution.view_manifest import ViewManifest
from src.agent.reading.as_drawn.schema import SCHEMA, AsDrawnPlanV2

# Only the existing micrometre geometric partition roundoff, not a drawing
# tolerance. Normal-axis following separately uses the compiled wall thickness.
_EPS = 2e-6
_KINDS = {"door": "door", "passage": "open", "open": "open"}


@dataclass
class AsDrawnOpeningAccount:
    schema: str = "as_drawn_opening_account_v1"
    observations_considered: int = 0
    built_count: int = 0
    built: list[dict] = field(default_factory=list)
    unbuilt: list[dict] = field(default_factory=list)
    folded: list[dict] = field(default_factory=list)
    reclassified: list[dict] = field(default_factory=list)
    host_moves: list[dict] = field(default_factory=list)
    assumed_height_m: float = 2.1
    views: list[dict] = field(default_factory=list)

    def to_payload(self) -> dict:
        return asdict(self)


class _Unresolved(ValueError):
    def __init__(self, reason: str, **details):
        super().__init__(reason)
        self.reason, self.details = reason, details


def _covers(intervals, lo, hi):
    cursor = lo
    for a, b in sorted(intervals):
        if b < cursor - _EPS:
            continue
        if a > cursor + _EPS:
            return False
        cursor = max(cursor, b)
        if cursor >= hi - _EPS:
            return True
    return False


def _edges(poly):
    """(fixed axis, position, along interval, interior sign), from ring winding."""
    polygon = orient(poly, sign=1)
    for ring in [polygon.exterior, *polygon.interiors]:
        points = list(ring.coords)
        for a, b in zip(points, points[1:]):
            if abs(a[0] - b[0]) <= _EPS:
                yield "x", a[0], tuple(sorted((a[1], b[1]))), -1 if b[1] > a[1] else 1
            elif abs(a[1] - b[1]) <= _EPS:
                yield "y", a[1], tuple(sorted((a[0], b[0]))), 1 if b[0] > a[0] else -1


def _points(axis, pos, span):
    return ((pos, span[0]), (pos, span[1])) if axis == "x" else ((span[0], pos), (span[1], pos))


def _host(geom, floor, axis, pos, span, half_thickness):
    supports = []
    for cell in floor.cells:
        for edge_axis, edge_pos, interval, sign in _edges(cell_polygon(cell)):
            if (edge_axis == axis and abs(edge_pos - pos) <= half_thickness + _EPS
                    and min(interval[1], span[1]) > max(interval[0], span[0]) + _EPS):
                supports.append((edge_pos, cell.id, interval, sign))
    groups = []
    for item in sorted(supports):
        if groups and abs(item[0] - groups[-1][0][0]) <= _EPS:
            groups[-1].append(item)
        else:
            groups.append([item])
    if len(groups) != 1:
        raise _Unresolved("final_wall_host_not_unique", candidate_positions=[g[0][0] for g in groups])
    group = groups[0]
    by_room = {}
    for _, room, interval, sign in group:
        by_room.setdefault(room, []).append((interval, sign))
    if len(by_room) > 2 or any(not _covers([i for i, _ in rows], *span) for rows in by_room.values()):
        raise _Unresolved("opening_crosses_room_boundaries", candidate_space_ids=sorted(by_room))
    signs = [{sign for _, sign in rows} for rows in by_room.values()]
    if any(len(s) != 1 for s in signs) or (len(signs) == 2 and signs[0] == signs[1]):
        raise _Unresolved("room_sides_inconsistent", candidate_space_ids=sorted(by_room))
    final_pos = group[0][0]
    p1, p2 = _points(axis, final_pos, span)
    if len(by_room) == 1:
        boundary = Polygon(floor_footprint(geom, floor)).boundary
        if not boundary.buffer(_EPS, cap_style=2).covers(LineString([p1, p2])):
            raise _Unresolved("missing_interior_neighbor", candidate_space_ids=sorted(by_room))
    rooms = sorted(by_room)
    return final_pos, rooms[0], rooms[1] if len(rooms) == 2 else None


def _compiled_owners(raw, *, doc, input_id, expected_output_id, output_sha):
    compilation = WallCompilationV1.model_validate_json(raw)
    payload = compilation.model_dump(mode="python")
    digest = payload.pop("content_sha256")
    if canonical_sha256(payload) != digest:
        raise ValueError(f"{input_id}: wall compilation content hash mismatch")
    owners = {}
    for wall in compilation.walls:
        payload = wall.model_dump(mode="python")
        if canonical_sha256({k: v for k, v in payload.items() if k != "derivation_hash"}) != wall.derivation_hash:
            raise ValueError(f"{input_id}: compiled wall derivation hash mismatch")
        for ref in wall.source_refs:
            # The floor executor historically names source bytes by product
            # filename; the manifest may give that slot a different input ID.
            if (ref.input_id not in {input_id, expected_output_id} or ref.source_contract_id != "as_drawn_plan"
                    or ref.source_output_sha256 != output_sha):
                raise ValueError(f"{input_id}: compiled wall source does not match reading bytes")
            node = resolve_json_pointer(doc, ref.json_pointer)
            if not isinstance(node, dict) or node.get("id") != ref.observation_id:
                raise ValueError(f"{input_id}: compiled wall source pointer does not match face")
            owners.setdefault(ref.observation_id, {})[wall.wall_id] = wall
    return owners


def _observations(doc, owners):
    faces = {r["id"]: r for r in doc["observations"]["face_lines"]}
    hypotheses = doc["hypotheses"]
    types = hypotheses.get("opening_types") or {}
    candidates = hypotheses.get("opening_candidates") or []
    if len({r["id"] for r in candidates}) != len(candidates):
        raise ValueError("duplicate opening observation id")
    result = {}
    for candidate in candidates:
        row = {"candidate": candidate, "kind": _KINDS.get(types.get(candidate["id"])),
               "reading_type": types.get(candidate["id"]), "wall": None, "problem": None}
        face = faces.get(candidate["face_line"])
        index = candidate["gap_index"]
        span = candidate["span_m"]
        if (face is None or index < 0 or index >= len(face["gaps"])
                or any(not math.isfinite(v) for v in span) or span[0] >= span[1]
                or any(abs(a - b) > 1e-9 for a, b in zip(span, face["gaps"][index]["span_m"]))):
            row["problem"] = "opening_gap_reference_invalid"
        else:
            walls = list(owners.get(face["id"], {}).values())
            if len(walls) != 1:
                row["problem"] = "compiled_wall_host_not_unique"
            else:
                wall = walls[0]
                row["wall"] = wall
                center = wall.resolved_centerline
                if (center is None or center.constant_world_axis not in {"x", "y"}
                        or center.constant_pos_m is None or wall.resolved_thickness_m is None
                        or wall.resolved_thickness_m <= 0):
                    row["problem"] = "compiled_wall_unresolved"
                elif center.constant_world_axis != face["constant_world_axis"]:
                    row["problem"] = "compiled_wall_axis_mismatch"
                # Compiled intervals describe solid ink runs; an opening is
                # precisely a gap between them. Check the support's extent,
                # not solid-run coverage, after verifying the indexed gap.
                elif not wall.resolved_along_intervals or not _covers(
                    [(min(a for a, _ in wall.resolved_along_intervals),
                      max(b for _, b in wall.resolved_along_intervals))], *span
                ):
                    row["problem"] = "opening_outside_compiled_wall"
        result[candidate["id"]] = row
    # A positive type entry without its candidate is also a missing object.
    for oid, typ in sorted(types.items()):
        if typ in _KINDS and oid not in result:
            result[oid] = {"candidate": {"id": oid}, "kind": _KINDS[typ], "reading_type": typ,
                           "wall": None, "problem": "opening_candidate_missing"}
    return result


def _groups(doc, rows):
    pairs = {frozenset((p["face_a"], p["face_b"])) for p in (doc["hypotheses"].get("pairs") or [])}
    adjacency = {oid: set() for oid in rows}
    candidates = sorted(rows)
    for i, oid in enumerate(candidates):
        a = rows[oid]
        if a["problem"]:
            continue
        for other in candidates[i + 1:]:
            b = rows[other]
            if b["problem"] or a["wall"].wall_id != b["wall"].wall_id:
                continue
            ca, cb = a["candidate"], b["candidate"]
            if ca["face_line"] == cb["face_line"] or frozenset((ca["face_line"], cb["face_line"])) not in pairs:
                continue
            tolerance = a["wall"].resolved_thickness_m / 2
            if all(abs(x - y) <= tolerance + _EPS for x, y in zip(ca["span_m"], cb["span_m"])):
                adjacency[oid].add(other)
                adjacency[other].add(oid)
    visited = set()
    for oid in candidates:
        if oid in visited:
            continue
        component, todo = set(), [oid]
        while todo:
            current = todo.pop()
            if current not in component:
                component.add(current)
                todo.extend(adjacency[current] - component)
        visited.update(component)
        positives = sorted(o for o in component if rows[o]["kind"])
        if not positives:
            continue
        problem = next((rows[o]["problem"] for o in positives if rows[o]["problem"]), None)
        if len(component) > 2:
            problem = "paired_observation_ambiguous"
        elif len({rows[o]["kind"] for o in component}) != 1:
            problem = "paired_observation_type_conflict"
        yield positives, sorted(component), problem


def _rectangle(opening):
    axis = "x" if opening.p1[0] == opening.p2[0] else "y"
    fixed, along = (0, 1) if axis == "x" else (1, 0)
    return axis, opening.p1[fixed], tuple(sorted((opening.p1[along], opening.p2[along]))), opening.z


def _overlaps(a, b):
    return (a[0] == b[0] and abs(a[1] - b[1]) <= _EPS
            and min(a[2][1], b[2][1]) > max(a[2][0], b[2][0]) + _EPS
            and min(a[3][1], b[3][1]) > max(a[3][0], b[3][0]) + _EPS)


def _window_rectangles(geom, floor):
    segments = {s.id: s for s in getattr(geom, "facade_segments", [])}
    cells = {c.id: c for c in floor.cells}
    for window in geom.windows:
        if (getattr(window, "floor_id", None) != floor_key(geom, floor)
                if geom.schema_version == "3" else window.floor != floor.name):
            continue
        axis = "x" if window.facade in {"East", "West"} else "y"
        segment = segments.get(getattr(window, "facade_segment_id", None))
        if segment is not None:
            yield window.id, (axis, segment.p1[0 if axis == "x" else 1], window.span, window.z)
            continue
        # Legacy windows have no segment ID. Room boundary orientation supplies
        # their physical facade; do not invent a room-centre offset probe.
        wanted_sign = -1 if window.facade in {"East", "North"} else 1
        for cell in ([cells[window.room]] if window.room in cells else floor.cells):
            for edge_axis, pos, interval, sign in _edges(cell_polygon(cell)):
                if edge_axis == axis and sign == wanted_sign and _covers([interval], *window.span):
                    yield window.id, (axis, pos, window.span, window.z)


def populate_as_drawn_openings(
    geom: CorrectedGeometry, *, raw_view_manifest_bytes: bytes,
    raw_reading_artifacts: Mapping[str, bytes], raw_wall_compilations: Mapping[str, bytes],
    assumed_height_m: float = 2.1,
    wall_gap_decisions: tuple = (),
) -> tuple[CorrectedGeometry, AsDrawnOpeningAccount]:
    """Return an immutable-input enrichment plus complete positive-observation ledger.

    ``built`` counts physical source openings, while ``folded`` names extra
    face observations. ``unbuilt`` records all positive IDs in each rejected
    group and is also persisted in ``geometry.unsupported``. Explicit reviewed
    continuous-space gaps retain positive observations in ``reclassified``
    and source correction records, rather than inventing internal openings.
    No source room,
    window, existing opening or observed along-span is removed or resized.
    """
    if not math.isfinite(assumed_height_m) or assumed_height_m <= 0:
        raise ValueError("assumed opening height must be finite and positive")
    result = geom.model_copy(deep=True)
    account = AsDrawnOpeningAccount(assumed_height_m=float(assumed_height_m))
    manifest = ViewManifest.model_validate_json(raw_view_manifest_bytes)
    floors = sorted(geom.floors, key=lambda f: f.z_floor)
    proposals = []

    def reject(row, reason, **details):
        failure = {**row, "reason": reason, **details}
        account.unbuilt.append(failure)
        record = {"kind": "as_drawn_opening_unbuilt", **failure}
        if record not in result.unsupported:
            result.unsupported.append(record)

    for entry in sorted(manifest.required_entries(), key=lambda e: e.input_id):
        if entry.view_type != "plan":
            continue
        raw = raw_reading_artifacts.get(entry.input_id)
        if raw is None:
            raise ValueError(f"{entry.input_id}: required plan reading is absent")
        doc = json.loads(raw)
        if doc.get("schema") != SCHEMA:
            account.views.append({"input_id": entry.input_id, "status": "no_structured_opening_channel"})
            continue
        AsDrawnPlanV2.model_validate_json(raw)
        positives = {oid for oid, kind in (doc["hypotheses"].get("opening_types") or {}).items() if kind in _KINDS}
        account.observations_considered += len(positives)
        account.views.append({"input_id": entry.input_id, "status": "structured", "observations_considered": len(positives)})
        if not positives:
            continue
        base = {"input_id": entry.input_id, "observation_ids": sorted(positives)}
        if entry.floor_ref is None or entry.floor_ref > len(floors):
            reject(base, "plan_floor_missing")
            continue
        floor = floors[entry.floor_ref - 1]
        output_sha = hashlib.sha256(raw).hexdigest()
        compilation_raw = raw_wall_compilations.get(entry.input_id)
        if compilation_raw is None:
            reject(base, "wall_compilation_missing")
            continue
        owners = _compiled_owners(compilation_raw, doc=doc, input_id=entry.input_id,
                                  expected_output_id=entry.expected_output_id, output_sha=output_sha)
        from src.agent.correction.wall_gap_review import resolve_wall_gap_decisions

        reviews = resolve_wall_gap_decisions(
            wall_gap_decisions, input_id=entry.input_id, raw_reading=raw, raw_compilation=compilation_raw)
        reclassified_ids = set()
        for review in reviews:
            ids = sorted(c["id"] for c in review["opening_classifications"] if c["id"] in positives)
            if not ids:
                continue
            # Reclassification applies to fresh source reconstruction, never
            # silently deletes an existing stored opening during enrichment.
            old_id = "plan_opening:" + canonical_sha256({"input_id": entry.input_id, "observation_ids": ids})[:24]
            if any(o.id == old_id for o in geom.openings):
                raise ValueError("wall_gap_review_requires_source_rebuild")
            row = {"input_id": entry.input_id, "observation_ids": ids,
                   "reason": "reviewed_continuous_source_space", "review": review}
            account.reclassified.append(row)
            correction = {"kind": "wall_gap_opening_reclassification", **row}
            if correction not in result.corrections:
                result.corrections.append(correction)
            reclassified_ids.update(ids)
        rows = _observations(doc, owners)
        rows = {oid: row for oid, row in rows.items() if oid not in reclassified_ids}
        for positive_ids, all_ids, problem in _groups(doc, rows):
            row = {"input_id": entry.input_id, "observation_ids": positive_ids,
                   "floor_id": floor_key(geom, floor)}
            if problem:
                reject(row, problem, related_observation_ids=all_ids)
                continue
            anchor = positive_ids[0]
            observation = rows[anchor]
            wall = observation["wall"]
            axis, pos = wall.resolved_centerline.constant_world_axis, wall.resolved_centerline.constant_pos_m
            span = tuple(observation["candidate"]["span_m"])
            row.update({"wall_id": wall.wall_id, "anchor_observation_id": anchor, "span_m": list(span),
                        "compiled_half_thickness_m": wall.resolved_thickness_m / 2})
            try:
                if assumed_height_m > floor.ceiling_height + _EPS:
                    raise _Unresolved("assumed_height_exceeds_room_height")
                final_pos, room, neighbor = _host(geom, floor, axis, pos, span, wall.resolved_thickness_m / 2)
                # Choosing one measured span as the representative must not
                # conceal that its opposite face crosses into another room.
                for duplicate_id in positive_ids[1:]:
                    duplicate_span = rows[duplicate_id]["candidate"]["span_m"]
                    duplicate_pos, duplicate_room, duplicate_neighbor = _host(
                        geom, floor, axis, pos, duplicate_span, wall.resolved_thickness_m / 2
                    )
                    if (abs(duplicate_pos - final_pos) > _EPS
                            or (duplicate_room, duplicate_neighbor) != (room, neighbor)):
                        raise _Unresolved("paired_observation_room_conflict", conflicting_observation_id=duplicate_id)
            except _Unresolved as exc:
                reject(row, exc.reason, **exc.details)
                continue
            p1, p2 = _points(axis, final_pos, span)
            refs = [source_locator(input_id=entry.input_id, observation_id=oid, output_sha256=output_sha) for oid in positive_ids]
            oid = "plan_opening:" + canonical_sha256({"input_id": entry.input_id, "observation_ids": positive_ids})[:24]
            assumptions = [f"Height assumed {assumed_height_m:g} m above floor; no measured door/opening height in the plan opening channel."]
            if final_pos != pos:
                assumptions.append(f"Wall position follows final room boundary: {axis}={pos:g} to {final_pos:g} m; bounded by compiled half-thickness {wall.resolved_thickness_m / 2:g} m; along-span unchanged.")
            opening = WallOpening(id=oid, kind=observation["kind"], space_id=room, other_space_id=neighbor,
                                  p1=p1, p2=p2, z=(floor.z_floor, floor.z_floor + assumed_height_m),
                                  source_refs=refs, assumptions=assumptions)
            proposals.append((opening, row, floor, pos, final_pos, axis))

    # Resolve the entire proposal set before accepting any member, so order
    # cannot choose a winner among overlapping source observations.
    for opening, row, floor, pos, final_pos, axis in proposals:
        rectangle = _rectangle(opening)
        existing_id = [o for o in geom.openings if o.id == opening.id]
        if existing_id and (len(existing_id) != 1 or existing_id[0] != opening):
            reject(row, "existing_opening_identity_conflict", opening_id=opening.id)
            continue
        overlaps = sorted({o.id for o in geom.openings if o.id != opening.id and _overlaps(rectangle, _rectangle(o))}
                          | {o.id for o, *_ in proposals if o.id != opening.id and _overlaps(rectangle, _rectangle(o))})
        if overlaps:
            reject(row, "opening_overlap", conflicting_opening_ids=overlaps)
            continue
        window_ids = sorted({wid for wid, rect in _window_rectangles(geom, floor) if _overlaps(rectangle, rect)})
        if window_ids:
            reject(row, "window_overlap", conflicting_window_ids=window_ids)
            continue
        if not existing_id:
            result.openings.append(opening)
        account.built.append({**row, "opening_id": opening.id, "kind": opening.kind,
                              "space_id": opening.space_id, "other_space_id": opening.other_space_id,
                              "p1": list(opening.p1), "p2": list(opening.p2), "z": list(opening.z),
                              "source_refs": opening.source_refs, "assumptions": opening.assumptions})
        for oid in row["observation_ids"][1:]:
            account.folded.append({"input_id": row["input_id"], "observation_id": oid,
                                   "anchor_observation_id": row["anchor_observation_id"], "opening_id": opening.id})
        if final_pos != pos:
            account.host_moves.append({"input_id": row["input_id"], "opening_id": opening.id,
                                       "axis": axis, "compiled_pos_m": pos, "final_pos_m": final_pos,
                                       "delta_m": final_pos - pos, "wall_id": row["wall_id"],
                                       "max_follow_distance_m": row["compiled_half_thickness_m"]})
    account.built_count = len(account.built)
    return result, account
