"""Judge-only source partition evidence, independent of correction wall decisions.

Reading strokes can support physical boundaries, not define room identities:
an open doorway does not merge two source spaces. GT polygons are a separate
reference; raw boundary offsets are kept apart from proven split/merge findings.
"""
from __future__ import annotations

import hashlib
import json
import re
from itertools import combinations
from math import isfinite
from pathlib import Path

from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from src.agent.correction.parse import ensure_corrected_geometry
from src.agent.geometry.modelling import _cell_polygon
from src.agent.judge.gt_schema import GroundTruthV3, LegacyGroundTruthV2
from src.agent.judge.source_partition import compare_partitions


def floor_number(value):
    """Explicit floor labels only; never match floors by their list order."""
    match = re.fullmatch(r"(?:floor\s*|f)?(\d+)(?:\s*f|\s*floor)?(?:_view)?", str(value).lower().strip())
    return int(match[1]) if match else None


def candidate_spaces(geom):
    return [{"id": c.id, "floor_id": str(getattr(f, "id", None) or f.name),
             "polygon": list(_cell_polygon(c).exterior.coords)[:-1],
             "z_floor": float(f.z_floor), "height": float(f.ceiling_height)}
            for f in geom.floors for c in f.cells]


def _unavailable(reason):
    return {"status": "not_evaluated", "reason": reason, "findings": []}


def reference_partition(geom, document, *, tolerance_m=.02, source_spaces=None):
    """Compare typed, validated reference polygons in their recorded world frame.

    No fitting to candidate coordinates, buffering rooms, or changing answers.
    Frame/basis differences may affect the raw geometric result; separate
    explicit topological defects from boundary-offset-only discrepancies.
    """
    if document is None:
        return _unavailable("independent_partition_reference_unavailable")
    if not isinstance(document, (GroundTruthV3, LegacyGroundTruthV2)):
        raise TypeError("partition reference must be a validated GT document")
    typed = isinstance(document, GroundTruthV3)
    if (typed and document.verification.status != "human_verified") or (not typed and not document.verified):
        return _unavailable("partition_reference_not_verified")
    refs = []
    floor_rows = []
    for floor in document.floors:
        identity = floor.id if typed else floor.name
        bottom = floor.z_floor_m if typed else floor.z_floor
        height = floor.ceiling_height_m if typed else floor.ceiling_height
        floor_rows.append((identity, floor.name, bottom))
        for zone in floor.zones:
            if typed:
                if zone.polygon.interior_rings:
                    return _unavailable("reference_holes_not_supported")
                ring = zone.polygon.exterior.vertices
            else:
                x0, y0, x1, y1 = zone.rect_m
                ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
            refs.append({"id": zone.id, "floor_id": identity, "polygon": ring,
                         "z_floor": float(bottom), "height": float(height)})
    mapping, used = {}, set()
    for floor in geom.floors:
        identity = str(getattr(floor, "id", None) or floor.name)
        number = floor_number(identity) or floor_number(floor.name)
        matches = [row[0] for row in floor_rows
                   if identity in row[:2] or floor.name in row[:2]
                   or (number is not None and number in (floor_number(row[0]), floor_number(row[1])))]
        if not matches:
            matches = [row[0] for row in floor_rows if abs(row[2] - floor.z_floor) <= 1e-9]
        if len(matches) > 1 or (matches and matches[0] in used):
            return _unavailable("ambiguous_floor_correspondence")
        mapping[identity] = matches[0] if matches else f"unmatched:{identity}"
        used.update(matches)
    # A source BIM exporter can supply its actual emitted spaces. The correction
    # still supplies floor identity metadata; evaluation never feeds GT back.
    candidates = candidate_spaces(geom) if source_spaces is None else [dict(s) for s in source_spaces]
    for space in candidates:
        space["floor_id"] = mapping.get(space["floor_id"], f"unmatched:{space['floor_id']}")
    comparison = compare_partitions(refs, candidates, tolerance_m=tolerance_m)
    topology_codes = {"source_space_split", "source_spaces_merged", "extra_source_space",
                      "missing_source_space", "floor_assignment_changed", "vertical_extent_changed",
                      "candidate_spaces_overlap", "invalid_candidate_space"}
    topology = [f for f in comparison["findings"] if f["code"] in topology_codes and f["severity"] == "severe"]
    internal = []
    if comparison["status"] != "not_evaluated":
        for fid, _, _ in floor_rows:
            ref_polys = [Polygon(s["polygon"], s.get('holes', [])) for s in refs if s["floor_id"] == fid]
            cand_polys = [Polygon(s["polygon"], s.get('holes', [])) for s in candidates if s["floor_id"] == fid]
            if not ref_polys or not cand_polys or any(not p.is_valid for p in cand_polys):
                continue
            # Compare physical partitions in the footprint area common to both
            # representations. This excludes exterior reference-plane offsets,
            # not room objects; the full polygons and discrepancies remain above.
            common = unary_union(ref_polys).intersection(unary_union(cand_polys))
            ref_lines = unary_union([a.boundary.intersection(b.boundary) for a, b in combinations(ref_polys, 2)]).intersection(common)
            cand_lines = unary_union([a.boundary.intersection(b.boundary) for a, b in combinations(cand_polys, 2)]).intersection(common)
            edge_band = common.boundary.buffer(tolerance_m + 1e-9)
            missing = ref_lines.difference(cand_lines.buffer(tolerance_m + 1e-9)).difference(edge_band)
            extra = cand_lines.difference(ref_lines.buffer(tolerance_m + 1e-9)).difference(edge_band)
            row = {"floor_id": fid, "missing_length_m": missing.length, "extra_length_m": extra.length,
                   "missing_wkt": missing.wkt, "extra_wkt": extra.wkt}
            internal.append(row)
            if max(missing.length, extra.length) > 2 * tolerance_m + 1e-9:
                topology.append({"code": "internal_partition_boundary_changed", "severity": "severe",
                                 "message": "Interior source boundaries differ inside the common footprint, beyond the boundary tolerance.", **row})
    status = comparison["status"]
    if topology:
        status = "severe"
    elif status == "severe":
        status = "not_evaluated"
    return {"status": status, "basis": "verified_independent_gt_partition",
            "reference_schema_version": document.schema_version,
            "floor_mapping": mapping, "comparison": comparison,
            "topology_findings": topology,
            "internal_boundary_comparison": internal,
            "boundary_offset_review_required": any(f["code"] == "partition_boundary_changed" for f in comparison["findings"]),
            "interpretation": "Raw geometric differences retain their measured status. Boundary offsets alone require reference-plane/scale review; no candidate-fitted reference normalization is applied.",
            "reference_spaces": refs, "candidate_spaces": candidates}


def _wall_strokes(doc):
    strokes = doc.get("strokes")
    if not isinstance(strokes, list):
        return [], ["explicit_wall_strokes_unavailable"]
    walls, rejected = [], []
    for index, stroke in enumerate(strokes):
        if not isinstance(stroke, dict) or stroke.get("pen") != "wall":
            continue
        geometry = stroke.get("geometry", {})
        try:
            kind = geometry.get("kind")
            if kind == "line":
                points = [geometry["p1"], geometry["p2"]]
            elif kind == "polyline":
                points = list(geometry["points"])
                if geometry.get("closed") and points[0] != points[-1]:
                    points.append(points[0])
            else:
                raise ValueError("unsupported wall stroke geometry")
            if any(len(p) != 2 or any(not isfinite(float(v)) for v in p) for p in points):
                raise ValueError("nonfinite or non-XY wall")
            for segment, (first, second) in enumerate(zip(points, points[1:])):
                line = LineString([first, second])
                if line.length <= 0:
                    raise ValueError("zero length wall")
                walls.append({"id": stroke.get("id", str(index)), "pointer": f"/strokes/{index}/geometry",
                              "segment": segment, "line": line})
        except (TypeError, KeyError, ValueError, IndexError):
            rejected.append(f"unusable_wall_stroke:{index}")
    return walls, rejected


def reading_boundary_support(geom, readings, *, tolerance_m=.02):
    """Use upstream strokes as PARTIAL evidence for shared source boundaries.

    ``readings`` entries carry a floor_ref, input_id and exact raw bytes. The
    correction candidate is never used to construct a supposed room reference.
    No door positions are parsed from notes; unsupported spans require review.
    """
    floors, findings = [], []
    for floor in geom.floors:
        fid = str(getattr(floor, "id", None) or floor.name)
        number = floor_number(fid) or floor_number(floor.name)
        inputs = [r for r in readings if r["floor_ref"] in (fid, floor.name)
                  or (number is not None and floor_number(r["floor_ref"]) == number)]
        if len(inputs) != 1:
            floors.append({"floor_id": fid, **_unavailable("reading_floor_reference_not_unique")})
            continue
        source = inputs[0]
        raw = source["raw_bytes"]
        identity = {"input_id": source["input_id"], "sha256": hashlib.sha256(raw).hexdigest()}
        try:
            doc = json.loads(raw)
            walls, rejected = _wall_strokes(doc)
        except (ValueError, TypeError, AttributeError):
            walls, rejected = [], ["reading_not_a_valid_object"]
        if not walls or rejected:
            floors.append({"floor_id": fid, "source": identity,
                           **_unavailable(";".join(rejected) or "no_wall_strokes")})
            continue
        corridor = unary_union([w["line"].buffer(tolerance_m, cap_style=2) for w in walls]) if tolerance_m else unary_union([w["line"] for w in walls])
        pairs = []
        for left, right in combinations(floor.cells, 2):
            boundary = _cell_polygon(left).boundary.intersection(_cell_polygon(right).boundary)
            if boundary.length <= 1e-9:
                continue
            unsupported = boundary.difference(corridor)
            row = {"space_ids": [left.id, right.id], "length_m": boundary.length,
                   "unsupported_length_m": unsupported.length,
                   "boundary_wkt": boundary.wkt, "unsupported_wkt": unsupported.wkt,
                   "supporting_strokes": [{k: v for k, v in w.items() if k != "line"}
                                          for w in walls if boundary.intersection(w["line"].buffer(tolerance_m + 1e-9, cap_style=2)).length > 1e-9]}
            pairs.append(row)
            if unsupported.length > 2 * tolerance_m + 1e-9:
                findings.append({"code": "source_boundary_without_reading_wall_support", "severity": "review",
                                 "floor_id": fid, "source": identity, **row})
        floors.append({"floor_id": fid, "source": identity, "status": "review" if any(r["unsupported_length_m"] > 2*tolerance_m+1e-9 for r in pairs) else "pass",
                       "boundary_pairs": pairs, "wall_strokes": len(walls)})
    return {"status": "review" if findings else "not_evaluated" if any(f["status"] == "not_evaluated" for f in floors) else "pass",
            "scope": "partial_upstream_wall_support_only", "tolerance_m": tolerance_m,
            "floors": floors, "findings": findings,
            "not_evaluated": ["room identities", "doorway boundaries", "reading completeness",
                              "missing source walls", "exterior frame", "as-drawn face-pair semantics"]}


def partition_evidence(geometry, *, document=None, readings=(), tolerance_m=.02):
    if not isfinite(tolerance_m) or tolerance_m < 0:
        raise ValueError("tolerance_m must be finite and nonnegative")
    geom = ensure_corrected_geometry(geometry)
    reference = reference_partition(geom, document, tolerance_m=tolerance_m)
    reading = reading_boundary_support(geom, readings, tolerance_m=tolerance_m)
    return {"schema": "source_partition_evidence_v1", "reference_partition": reference,
            "reading_boundary_support": reading,
            "criterion": {"criterion": "source_partition_fidelity",
                          "suggested_status": reference["status"] if reference["status"] != "not_evaluated" else "insufficient_evidence",
                          "evidence": "Inspect source-space correspondence and unsupported internal boundaries. Counts and boundary offsets alone do not establish room correctness."},
            "not_evaluated": ["original-image reading quality", "door/opening connectivity", "false slabs"]}


def attempt_partition_evidence(run_dir, attempt_dir, *, document=None, reference_path=None):
    """Build J1 evidence from this attempt and frozen/accepted reading bytes.

    Never falls back from corrupt frozen inputs to mutable stage-root mirrors.
    Recomputed on each call; an old report cannot certify changed inputs.
    """
    from src.agent.execution.manifest import hash_bytes, load_run_manifest

    run_dir, attempt_dir = Path(run_dir), Path(attempt_dir)
    raw_output = (attempt_dir / "output.json").read_bytes()
    readings, identity = [], {"candidate_sha256": hash_bytes(raw_output)}
    manifest = load_run_manifest(run_dir)
    accepted_candidate = manifest.accepted("1_correction") if manifest else None
    candidate_is_accepted = accepted_candidate is not None and str(accepted_candidate.accepted_attempt) == attempt_dir.name.lstrip("0")
    if candidate_is_accepted and accepted_candidate.output_hash != hash_bytes(raw_output):
        raise ValueError("accepted_correction_hash_mismatch")
    identity["candidate_acceptance"] = "gate1_accepted" if candidate_is_accepted else "unaccepted_candidate"
    unavailable = None
    marker_path = attempt_dir / "window_resolver_inputs.json"
    try:
        if marker_path.exists():
            from src.agent.correction.window_sources import verify_window_resolver_inputs_artifact
            from src.agent.execution.view_manifest import ViewManifest

            marker_bytes = marker_path.read_bytes()
            marker = verify_window_resolver_inputs_artifact(marker_bytes)
            manifest = ViewManifest.model_validate_json(marker.raw_view_manifest_bytes)
            raw_readings = dict(marker.raw_reading_artifacts)
            readings = [{"input_id": row.input_id, "floor_ref": str(row.floor_ref),
                         "raw_bytes": raw_readings[row.input_id]}
                        for row in manifest.required_entries() if row.view_type == "plan"]
            identity.update(reading_basis="frozen_attempt_inputs", marker_sha256=hash_bytes(marker_bytes))
        else:
            accepted = manifest.accepted("0_reading") if manifest else None
            if accepted is None:
                unavailable = "accepted_reading_unavailable"
            else:
                path = run_dir / "0_reading/attempts" / f"{accepted.accepted_attempt:03d}" / "output.json"
                raw = path.read_bytes()
                if hash_bytes(raw) != accepted.output_hash:
                    raise ValueError("accepted_reading_hash_mismatch")
                payload = json.loads(raw)
                views = payload.get("views", payload)
                readings = [{"input_id": name, "floor_ref": name,
                             "raw_bytes": json.dumps(doc, ensure_ascii=False, sort_keys=True).encode()}
                            for name, doc in views.items() if floor_number(name) is not None and isinstance(doc, dict)]
                identity.update(reading_basis="accepted_reading_attempt", reading_path=str(path),
                                reading_sha256=hash_bytes(raw), per_view_serialization="sorted_json_from_accepted_output",
                                candidate_reading_binding="legacy_upstream_byte_binding_not_verified")
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        readings = []
        unavailable = f"reading_reference_rejected:{type(exc).__name__}:{exc}"
    result = partition_evidence(json.loads(raw_output), document=document, readings=readings)
    if unavailable:
        result["reading_boundary_support"]["input_status"] = unavailable
    if reference_path is not None:
        path = Path(reference_path)
        identity.update(reference_path=str(path), reference_sha256=hash_bytes(path.read_bytes()))
    result["identity"] = identity
    return result
