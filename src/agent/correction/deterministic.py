"""Deterministic correction core: canonical axis snapping + sliver guard.

Runs on `CorrectedGeometry` (correction stage output) before the modeling/MEP stages modeling. Builds a
GLOBAL canonical axis set across all floors and snaps every cell / window /
footprint boundary onto it, so:

  1. the same wall on different floors becomes byte-identical (cross-floor jitter
     gone), and
  2. no two canonical axes are closer than `min_edge_length_m`, so the interzone
     floor/ceiling split cannot produce a degenerate sub-tolerance sliver — the
     EnergyPlus input-processing segfault class is made structurally impossible.

This kills the CRASH. It does NOT guarantee CORRECTNESS: if correction stage mis-placed a
partition, snapping removes the crack but keeps the wrong layout. Geometric
correctness is the judgment layer's job (correction stage, A3 arbitration). "No crash" and
"is correct" are deliberately separate concerns.

Pipeline per axis (structural x/y): per-floor cluster (identity) ->
cross-floor reconcile (hard footprint anchors, mutual-nearest matches, ambiguity
flags) -> snap representative to grid (regularize) -> provenance-aware
sliver-merge (min-edge safety). Then a CONNECTIVITY pass pulls any cell edge
falling within `gap_close_threshold_m` inside a footprint boundary out onto it,
so an internal wall that stops short of the exterior seals the enclosure (an
unclosed enclosure forms no EnergyPlus zone). Connectivity is a coarser,
distinct operation from axis identity (A0 §4) — bigger threshold, directional,
footprint-only for now. Windows are tiered finer and are NOT placed on the
structural grid (no cross-floor identity / sliver role). Legacy windows retain
their parent-cell clamp; v3 windows only snap and floor-z clamp before B5's
source-aware host resolver owns span/topology decisions.

All tolerances come from `src/configs/correction.yaml` (basis: A0_contract.md §4),
loaded via `src.agent.correction.config` — not hardcoded here, so there is one
place to tune granularity and no A0-doc vs Python-constant drift.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from src.agent.correction.config import CoreTolerances, load_core_tolerances
from src.agent.correction.cell_geometry import (
    cell_axis_values,
    cell_has_polygon,
    cell_polygon_vertices,
    normalized_ccw_polygon,
    set_cell_polygon_vertices,
    validate_cell_polygon,
)
from src.agent.correction.envelope import AuthoritativeEnvelope, EnvelopeAxisResolution
from src.agent.correction.footprint import floor_footprint, floor_key, footprint_bbox
from src.agent.correction.geometry_validator import validate_corrected_geometry
from src.agent.correction.parse import ensure_corrected_geometry, validate_final_corrected_geometry
from src.agent.correction.schema import (
    CorrectedGeometry,
    CorrectedGeometryV3,
    DeterministicCoreStampV1,
    FootprintRing,
)
from src.agent.geometry.capability import require_supported_geometry_contract
from src.agent.geometry.capability import FEATURE_CELL_POLYGON, schema_supports, schema_version_of

if TYPE_CHECKING:
    from src.agent.correction.window_sources import VerifiedWindowResolverInputs


_EPS = 1e-9

# F-22 BLOCKER-1 (2026-08-12): version identity for the UNCONDITIONAL stamp
# `apply_deterministic_core` writes onto every schema-v3 completion (see
# `DeterministicCoreStampV1` in schema.py for the field itself, and the write
# site at the end of this function's v3 branch). `schema_version` alone
# cannot serve this role — a bug fix to this module's transform logic (e.g.
# F-17, 2026-08-09) does not bump `schema_version`, so the SAME
# `schema_version == "3"` spans both the buggy and the fixed code, and a
# judge that trusts on `schema_version` alone cannot tell a pre-fix product
# from a post-fix one (F-22 BLOCKER-1's actual reproduction).
#
# Bump this string whenever a change to this module (or envelope_transform.py
# / window_host.py, which it calls into) alters what a v3 product's
# post-transform geometry is actually GUARANTEED to mean — the same class of
# change F-17 was. `judge.correction_score._is_trusted_output_convention`
# compares a product's stamped version against this constant with EXACT
# equality; bumping it makes every artifact stamped under the OLD value stop
# being trusted, with NO historical whitelist required (user-ratified
# 2026-08-12: existing artifacts, including ones produced after a fix but
# before this stamp existed at all, are expected to need a rerun to regain a
# score — that is the intended enforcement mechanism, not a gap).
DETERMINISTIC_CORE_STAMP_VERSION = "1"

#: W#3 (wallhunt 2026-09-08b / dispatch 2026-09-08c S-A): the AS_DRAWN chain
#: leg's own stamp version — the same "which revision of the deterministic
#: kernel produced this geometry" identity, for the OTHER kernel.  The
#: as_drawn leg's geometry comes through the evidence chain's projection
#: bridge + snap + assembly + the as_drawn finalize half (see
#: ``correction.chain_replay``), ⛔ never through ``apply_deterministic_core``
#: — stamping it ``"1"`` would be a false processing claim, and leaving it
#: unstamped leaves judge-side boundary scoring permanently ``no_data``.  The
#: writer's as_drawn replay signs its proof under THIS version, and
#: ``judge.correction_score`` accepts a stamp/proof PAIR under either leg's
#: version (exact equality per leg, ⛔ no cross-leg mixing: a chain product
#: carrying a legacy proof, or vice versa, is untrusted).
AS_DRAWN_CHAIN_STAMP_VERSION = "as_drawn_chain_1"


class DeterministicCoreProofV1(BaseModel):
    """F-22 BLOCKER-1 round 2 (2026-08-13, sol re-review): externally issued
    proof that THIS accepted attempt's geometry is what the deterministic
    core actually produced -- not merely what the candidate CLAIMS
    (`DeterministicCoreStampV1`, which lives INSIDE the candidate's own
    bytes and is therefore only a `declared` fact: sol proved a candidate can
    forge a self-consistent geometry -- tampered footprint/ring/cells with
    every derived artifact (Vg, host claims, evidence) recomputed FROM the
    tampered footprint -- that still carries a byte-identical stamp).

    This object is NEVER constructed by, or reachable from, a correction
    draw. It is signed by `StageRunner.record`'s B5 write path ONLY after it
    has: (1) independently replayed `apply_deterministic_core` from the
    candidate's own embedded, re-verified raw producer bytes
    (`VerifiedWindowResolverInputs.producer_draw_canonical_bytes`), and (2)
    confirmed the replayed output's `core_owned_projection_v1` byte-matches
    the candidate's own -- see stage_runner.py's B5 branch for the exact
    equality gauntlet. It is bound into the accepted manifest record's
    `artifact_hashes["deterministic_core_proof"]` (manifest.py), the same
    tamper-evident mechanism `window_hosts`/`window_resolver_inputs` already
    use, so a judge-side reader can verify the sidecar on disk has not been
    swapped since acceptance without re-running the replay itself.

    `judge.correction_score._is_trusted_output_convention` is the ONLY
    reader that may promote a product's coordinates from `declared` to
    `trusted`, and it does so only when handed one of these AND after
    independently recomputing `core_owned_projection_v1(geom)` on the
    CURRENT geometry under test and confirming it still equals
    `core_projection_hash` -- the proof is re-verified against the artifact
    being scored, never merely trusted because a file with the right name
    exists on disk.
    """

    model_config = ConfigDict(extra="forbid")
    core_version: str
    input_hash: str
    core_projection_hash: str


def core_owned_projection_v1(geom: CorrectedGeometryV3) -> dict:
    """The subset of a v3 artifact's fields `apply_deterministic_core` fully
    determines and that a genuine core replay MUST reproduce byte-for-byte
    (F-22 BLOCKER-1 round 2).

    Deliberately EXCLUDES every field a stage AFTER the core is allowed to
    legitimately change (`finalize_correction_draw`, after `apply_
    deterministic_core` returns):
      - `facade_segments` -- materialized by `materialize_all_facade_segments`
        from the core-final footprint; already independently re-verified by
        `validate_materialized_facade_segments` (Vg rework CR1), not this.
      - each window's `span`/`room`/`facade_segment_id` -- overwritten by
        `apply_window_host_resolutions` (window_host.py) with the host-
        resolved, clamped values; those are separately cross-checked against
        `recompute_window_host_claims` in stage_runner.py's per-window audit
        loop. Only `floor_id` and `z` (untouched by host resolution) are
        core-owned for a window.
      - the `corrections` list's appended `window_host_resolution` rows --
        host resolution APPENDS to this list (never rewrites/reorders the
        core's own entries), so the caller must compare this projection's
        `corrections` as a PREFIX of the candidate's, not full equality; see
        the writer's own prefix + suffix-shape check.

    Floors are sorted by `id` and each floor's cells sorted by `id` so the
    comparison is order-independent (core mutates in place and never
    reorders either list in practice, but this keeps the projection --
    and the proof hash derived from it -- from depending on an incidental
    ordering guarantee this module does not otherwise promise).
    """
    floors = []
    for floor in sorted(geom.floors, key=lambda f: f.id):
        cells = sorted(floor.cells, key=lambda c: c.id)
        floors.append({
            "id": floor.id,
            "name": floor.name,
            "z_floor": float(floor.z_floor),
            "ceiling_height": float(floor.ceiling_height),
            "footprint": [[float(x), float(y)] for x, y in floor.footprint.vertices],
            "cells": [
                {
                    "id": cell.id,
                    "role": cell.role,
                    "x": [float(v) for v in cell.x],
                    "y": [float(v) for v in cell.y],
                    "polygon": (
                        None if cell.polygon is None
                        else [[float(x), float(y)] for x, y in cell.polygon]
                    ),
                }
                for cell in cells
            ],
        })
    windows = [
        {"id": window.id, "floor_id": window.floor_id, "z": [float(v) for v in window.z]}
        for window in sorted(geom.windows, key=lambda w: w.id)
    ]
    projection = {
        "footprint_x": [float(v) for v in geom.footprint_x],
        "footprint_y": [float(v) for v in geom.footprint_y],
        "floors": floors,
        "windows": windows,
        "conflicts": list(geom.conflicts),
        "unsupported": list(geom.unsupported),
        "corrections": list(geom.corrections),
    }
    # No later host-resolution stage owns door/passage edits. Retain their
    # complete declared geometry and evidence in replay verification, while
    # preserving existing proof hashes for artifacts without these openings.
    if geom.openings:
        projection["openings"] = [o.model_dump(mode="json") for o in sorted(geom.openings, key=lambda o: o.id)]
    return projection


def _v3_rectangular_ring(floor) -> bool:
    pts = list(floor.footprint.vertices)
    return len(pts) == 4 and len({p[0] for p in pts}) == 2 and len({p[1] for p in pts}) == 2


def _set_v3_ring_bounds(floor, axis: str, bounds: tuple[float, float]) -> None:
    """Move a rectangular v3 ring with its attached perimeter cells as one transaction."""
    lo, hi = bounds
    pts = []
    old = floor.footprint.vertices
    old_lo, old_hi = ((min(p[0] for p in old), max(p[0] for p in old)) if axis == "x"
                      else (min(p[1] for p in old), max(p[1] for p in old)))
    for x, y in old:
        if axis == "x":
            pts.append((lo if x == old_lo else hi if x == old_hi else x, y))
        else:
            pts.append((x, lo if y == old_lo else hi if y == old_hi else y))
    floor.footprint = FootprintRing(vertices=pts)


@dataclass(frozen=True)
class _IdentityCluster:
    rep: float
    raw: tuple[float, ...]


@dataclass(frozen=True)
class _AxisNode:
    idx: int
    floor: str
    rep: float
    raw: tuple[float, ...]


@dataclass
class _AxisGroup:
    canonical: float
    raw: list[float] = field(default_factory=list)
    support_by_floor: dict[str, list[float]] = field(default_factory=dict)
    node_ids: set[int] = field(default_factory=set)
    fixed_anchor: float | None = None
    footprint_raw: list[float] = field(default_factory=list)
    sliver_values: list[float] = field(default_factory=list)


def _snap_to_grid(v: float, grid: float) -> float:
    """Round `v` to the nearest multiple of `grid` (grid > 0)."""
    return round(round(v / grid) * grid, 6)


def _axis_value(v: float) -> float:
    return round(float(v), 6)


def _identity_clusters(values: list[float], tol: CoreTolerances) -> list[_IdentityCluster]:
    pts = sorted({_axis_value(v) for v in values})
    if not pts:
        return []

    clusters: list[list[float]] = [[pts[0]]]
    for v in pts[1:]:
        if v - clusters[-1][-1] <= tol.axis_jitter_tol_m + _EPS:
            clusters[-1].append(v)
        else:
            clusters.append([v])

    return [
        _IdentityCluster(rep=_axis_value(sum(c) / len(c)), raw=tuple(c))
        for c in clusters
    ]


def _build_axis_map(values: list[float], tol: CoreTolerances) -> dict[float, float]:
    """Map each input coordinate to a canonical axis value.

    1. cluster coordinates within `axis_jitter_tol_m` (same intended axis);
    2. snap each cluster representative (mean) onto `structural_snap_grid_m`;
    3. merge snapped representatives still closer than `min_edge_length_m`
       (sliver guard — the resulting break set has no gap that would split into
       a sub-tolerance strip).
    """
    grid = tol.structural_snap_grid_m
    clusters = _identity_clusters(values, tol)
    if not clusters:
        return {}

    # 2. snap each representative onto the structural grid
    reps = [_snap_to_grid(c.rep, grid) for c in clusters]

    # 3. sliver guard against the running canonical value
    groups: list[list[int]] = [[0]]
    canon: list[float] = [reps[0]]
    for i in range(1, len(reps)):
        if reps[i] - canon[-1] < tol.min_edge_length_m:
            groups[-1].append(i)
            merged = sum(reps[j] for j in groups[-1]) / len(groups[-1])
            canon[-1] = _snap_to_grid(merged, grid)
        else:
            groups.append([i])
            canon.append(reps[i])

    mapping: dict[float, float] = {}
    for gi, grp in enumerate(groups):
        cval = round(canon[gi], 6)
        for ci in grp:
            for v in clusters[ci].raw:
                mapping[v] = cval
    return mapping


def _copy_support(support: dict[str, list[float]]) -> dict[str, list[float]]:
    return {floor: list(values) for floor, values in support.items()}


def _group_from_nodes(
    nodes: list[_AxisNode],
    canonical: float,
    *,
    fixed_anchor: float | None = None,
    footprint_raw: list[float] | None = None,
) -> _AxisGroup:
    support: dict[str, list[float]] = defaultdict(list)
    raw: list[float] = []
    node_ids: set[int] = set()
    for node in nodes:
        support[node.floor].append(node.rep)
        raw.extend(node.raw)
        node_ids.add(node.idx)
    fp_raw = list(footprint_raw or [])
    raw.extend(fp_raw)
    cval = _axis_value(canonical)
    return _AxisGroup(
        canonical=cval,
        raw=raw,
        support_by_floor=dict(support),
        node_ids=node_ids,
        fixed_anchor=fixed_anchor,
        footprint_raw=fp_raw,
        sliver_values=[cval],
    )


def _merge_axis_groups(a: _AxisGroup, b: _AxisGroup, tol: CoreTolerances) -> _AxisGroup:
    fixed = [v for v in (a.fixed_anchor, b.fixed_anchor) if v is not None]
    if fixed:
        canonical = fixed[0]
    else:
        values = a.sliver_values + b.sliver_values
        canonical = _snap_to_grid(sum(values) / len(values), tol.structural_snap_grid_m)

    support = _copy_support(a.support_by_floor)
    for floor, values in b.support_by_floor.items():
        support.setdefault(floor, []).extend(values)

    return _AxisGroup(
        canonical=_axis_value(canonical),
        raw=a.raw + b.raw,
        support_by_floor=support,
        node_ids=set(a.node_ids) | set(b.node_ids),
        fixed_anchor=fixed[0] if fixed else None,
        footprint_raw=a.footprint_raw + b.footprint_raw,
        sliver_values=a.sliver_values + b.sliver_values,
    )


def _same_floor_sliver_conflict(a: _AxisGroup, b: _AxisGroup, tol: CoreTolerances) -> bool:
    for floor in set(a.support_by_floor) & set(b.support_by_floor):
        for av in a.support_by_floor[floor]:
            for bv in b.support_by_floor[floor]:
                if abs(av - bv) >= tol.min_edge_length_m - _EPS:
                    return True
    return False


def _blocked_pair_crosses(a: _AxisGroup, b: _AxisGroup, blocked_pairs: set[frozenset[int]]) -> bool:
    for aid in a.node_ids:
        for bid in b.node_ids:
            if frozenset((aid, bid)) in blocked_pairs:
                return True
    return False


def _reconcile_cross_floor(
    per_floor_axes: dict[str, list[float]],
    footprint_coords: list[float],
    tol: CoreTolerances,
    axis: str,
) -> tuple[dict[float, float], list[dict]]:
    """Return a raw-coordinate map after provenance-aware cross-floor reconcile."""
    grid = tol.structural_snap_grid_m
    audit: list[dict] = []
    ambiguous_seen: set[tuple[str, tuple[int, ...]]] = set()

    # F-133 (2026-08-28): same-floor axis-merge ledger.
    #
    # Two DIFFERENT axis values that live on the SAME floor can be collapsed
    # into one canonical axis at two independent points below, and before this
    # ledger existed BOTH were silent -- a real building step (e.g. a 240/120
    # wall-basis jog, measured at 60 mm) came out of the core as a single axis
    # with `conflicts == []` and nothing in `corrections` naming the two values
    # that were destroyed. Collapsing axes ACROSS floors is what
    # `cross_floor_align_tol_m` is *for* and is already audited by
    # `deterministic_core.cross_floor_align`; that case is deliberately NOT
    # recorded here, or the ledger drowns in its own intended behaviour.
    #
    # ⛔ This ledger is OBSERVATION ONLY. It appends audit rows and touches no
    # coordinate, no grouping decision and no tolerance -- the merge policy is
    # bit-for-bit what it was before. Telling a real same-floor step apart from
    # the model's own 5-10 mm jitter is NOT possible at this layer (correction
    # holds coordinates only -- no walls, no thickness, and the two quantities
    # overlap numerically), so the honest move today is to make the loss
    # loud rather than to guess at a threshold. See R-6 / the wall-basis
    # diagnostics under logs/experiments/2026-08-28_wall_basis_jog/.
    #
    # ⚠️ `output_precision_m` deliberately does NOT filter these rows. That
    # threshold governs "how far did one coordinate move" (a normalization
    # question); this ledger reports "two distinct axes became one" (an
    # identity question). A 10 mm step shifts each side by only 5 mm and would
    # be filtered out by an output-precision gate while still having erased a
    # real wall line -- the exact case [[absence-conflates-causes-in-observables]]
    # warns about.
    same_floor_merges: list[dict] = []

    def record_same_floor_merge(
        floor: str,
        values: list[float],
        step: str,
        tolerance_name: str,
        probe_raw: float,
    ) -> None:
        """Queue one 'N distinct axes on `floor` became one' event.

        `probe_raw` is any raw input coordinate belonging to the merged set; the
        final canonical value is only known once the whole reconcile has settled
        (a merged group can merge again), so the resolved value / deltas are
        filled in from the finished `mapping` at the end of this function.
        """
        distinct = sorted({_axis_value(v) for v in values})
        if len(distinct) < 2:
            return
        same_floor_merges.append(
            {
                "floor": floor,
                "values": distinct,
                "step": step,
                "tolerance_name": tolerance_name,
                "probe_raw": _axis_value(probe_raw),
            }
        )

    nodes: list[_AxisNode] = []
    for floor in sorted(per_floor_axes):
        for cluster in _identity_clusters(per_floor_axes[floor], tol):
            # Kill site 1: identity clustering runs PER FLOOR, so a cluster
            # holding more than one distinct value is by construction a
            # same-floor merge driven by `axis_jitter_tol_m` (< 50 mm steps).
            record_same_floor_merge(
                floor,
                list(cluster.raw),
                "identity_cluster",
                "AXIS_JITTER_TOL",
                cluster.raw[0],
            )
            nodes.append(
                _AxisNode(
                    idx=len(nodes),
                    floor=floor,
                    rep=cluster.rep,
                    raw=cluster.raw,
                )
            )
    node_by_id = {node.idx: node for node in nodes}

    def add_ambiguity(node_ids: set[int], reason: str) -> None:
        key = (reason, tuple(sorted(node_ids)))
        if key in ambiguous_seen:
            return
        ambiguous_seen.add(key)
        values = sorted({_axis_value(node_by_id[nid].rep) for nid in node_ids})
        floors = sorted({node_by_id[nid].floor for nid in node_ids})
        audit.append(
            {
                "rule_id": "deterministic_core.cross_floor_ambiguous",
                "stage": "core",
                "target": f"{axis}.axis",
                "axis": axis,
                "original_value": [round(v, 4) for v in values],
                "resolved_value": [round(v, 4) for v in values],
                "delta": 0.0,
                "tolerance_name": "CROSS_FLOOR_ALIGN_TOL",
                "reason": reason,
                "floors": floors,
            }
        )

    def block_pair(aid: int, bid: int) -> None:
        if aid != bid:
            blocked_pairs.add(frozenset((aid, bid)))

    blocked_pairs: set[frozenset[int]] = set()
    ambiguous_nodes: set[int] = set()

    # Phase B0/B footprint hard anchors. Anchors are snapped once and then never
    # averaged; competing same-floor candidates near a footprint are left apart.
    footprint = [
        (_axis_value(raw), _snap_to_grid(raw, grid))
        for raw in sorted({_axis_value(v) for v in footprint_coords})
    ]
    fp_candidates: dict[int, dict[str, list[int]]] = {
        idx: defaultdict(list) for idx in range(len(footprint))
    }
    node_fp_candidates: dict[int, list[int]] = defaultdict(list)
    for node in nodes:
        for anchor_idx, (_, anchor) in enumerate(footprint):
            if abs(node.rep - anchor) <= tol.cross_floor_align_tol_m + _EPS:
                fp_candidates[anchor_idx][node.floor].append(node.idx)
                node_fp_candidates[node.idx].append(anchor_idx)

    for node_id, anchors in node_fp_candidates.items():
        if len(anchors) >= 2:
            ambiguous_nodes.add(node_id)
            add_ambiguity({node_id}, "candidate is within tolerance of multiple footprint anchors")

    for floors in fp_candidates.values():
        for candidates in floors.values():
            if len(candidates) >= 2:
                ambiguous_nodes.update(candidates)
                for i, aid in enumerate(candidates):
                    for bid in candidates[i + 1:]:
                        block_pair(aid, bid)
                add_ambiguity(
                    set(candidates),
                    "multiple same-floor candidates compete for a footprint anchor",
                )

    footprint_assignment: dict[int, int] = {}
    for node_id, anchors in node_fp_candidates.items():
        if node_id in ambiguous_nodes or len(anchors) != 1:
            continue
        anchor_idx = anchors[0]
        floor_candidates = fp_candidates[anchor_idx][node_by_id[node_id].floor]
        if len(floor_candidates) == 1:
            footprint_assignment[node_id] = anchor_idx

    free_nodes = [
        node
        for node in nodes
        if node.idx not in footprint_assignment and node.idx not in ambiguous_nodes
    ]

    # Phase B constrained cross-floor matching. Competition is graph-level:
    # any node with two or more cross-floor candidates blocks the whole area.
    candidates_by_node: dict[int, list[int]] = {node.idx: [] for node in free_nodes}
    for i, a in enumerate(free_nodes):
        for b in free_nodes[i + 1:]:
            if a.floor == b.floor:
                continue
            if abs(a.rep - b.rep) <= tol.cross_floor_align_tol_m + _EPS:
                candidates_by_node[a.idx].append(b.idx)
                candidates_by_node[b.idx].append(a.idx)

    for node_id, candidates in candidates_by_node.items():
        if len(candidates) >= 2:
            ambiguous_nodes.add(node_id)
            for candidate in candidates:
                block_pair(node_id, candidate)
            add_ambiguity(
                {node_id, *candidates},
                "cross-floor candidate competition",
            )

    parent = {node.idx: node.idx for node in free_nodes if node.idx not in ambiguous_nodes}

    def find(node_id: int) -> int:
        while parent[node_id] != node_id:
            parent[node_id] = parent[parent[node_id]]
            node_id = parent[node_id]
        return node_id

    def union(aid: int, bid: int) -> None:
        ra, rb = find(aid), find(bid)
        if ra != rb:
            parent[rb] = ra

    accepted_edges: set[frozenset[int]] = set()
    for node_id, candidates in candidates_by_node.items():
        if node_id in ambiguous_nodes or len(candidates) != 1:
            continue
        candidate = candidates[0]
        if candidate in ambiguous_nodes:
            continue
        if candidates_by_node.get(candidate) == [node_id]:
            accepted_edges.add(frozenset((node_id, candidate)))

    for edge in sorted(accepted_edges, key=lambda e: tuple(sorted(e))):
        aid, bid = tuple(edge)
        union(aid, bid)

    components: dict[int, list[_AxisNode]] = defaultdict(list)
    for node in free_nodes:
        if node.idx in ambiguous_nodes:
            continue
        components[find(node.idx)].append(node)

    groups: list[_AxisGroup] = []
    for anchor_idx, (raw, anchor) in enumerate(footprint):
        members = [
            node_by_id[node_id]
            for node_id, assigned_anchor in footprint_assignment.items()
            if assigned_anchor == anchor_idx
        ]
        groups.append(
            _group_from_nodes(
                members,
                anchor,
                fixed_anchor=anchor,
                footprint_raw=[raw],
            )
        )

    grouped_node_ids: set[int] = set(footprint_assignment)
    for component in components.values():
        floors = [node.floor for node in component]
        reps = [node.rep for node in component]
        legal = (
            len(floors) == len(set(floors))
            and max(reps) - min(reps) <= tol.cross_floor_align_tol_m + _EPS
        )
        if not legal:
            ids = {node.idx for node in component}
            ambiguous_nodes.update(ids)
            for node in component:
                for other in component:
                    block_pair(node.idx, other.idx)
            add_ambiguity(ids, "cross-floor component violates floor uniqueness or diameter")
            continue

        canonical = _snap_to_grid(sum(reps) / len(reps), grid)
        groups.append(_group_from_nodes(component, canonical))
        grouped_node_ids.update(node.idx for node in component)

    for node in nodes:
        if node.idx not in grouped_node_ids:
            groups.append(_group_from_nodes([node], _snap_to_grid(node.rep, grid)))

    def group_sort_key(group: _AxisGroup) -> tuple[float, int, float]:
        return (
            group.canonical,
            0 if group.fixed_anchor is not None else 1,
            min(group.raw) if group.raw else group.canonical,
        )

    # Phase C provenance-aware sliver guard. Cross-floor ambiguity pairs and
    # valid same-floor Phase A separations are never silently collapsed.
    while True:
        merged_any = False
        next_groups: list[_AxisGroup] = []
        groups = sorted(groups, key=group_sort_key)
        i = 0
        while i < len(groups):
            if i + 1 >= len(groups):
                next_groups.append(groups[i])
                i += 1
                continue

            a, b = groups[i], groups[i + 1]
            if b.canonical - a.canonical < tol.min_edge_length_m - _EPS:
                conflict = (
                    _same_floor_sliver_conflict(a, b, tol)
                    or _blocked_pair_crosses(a, b, blocked_pairs)
                    or (
                        a.fixed_anchor is not None
                        and b.fixed_anchor is not None
                        and abs(a.fixed_anchor - b.fixed_anchor) > _EPS
                    )
                )
                if conflict:
                    add_ambiguity(
                        set(a.node_ids) | set(b.node_ids),
                        "provenance-aware sliver guard kept axes separate",
                    )
                    next_groups.append(a)
                    i += 1
                else:
                    # Kill site 2: the sliver guard is allowed to collapse two
                    # groups closer than `min_edge_length_m` (50-100 mm steps
                    # survive clustering but die here). Record it only when the
                    # two groups both carry support on the SAME floor -- a
                    # purely cross-floor collapse is this parameter's stated
                    # purpose and is already covered by `cross_floor_align`.
                    for shared in sorted(set(a.support_by_floor) & set(b.support_by_floor)):
                        record_same_floor_merge(
                            shared,
                            a.support_by_floor[shared] + b.support_by_floor[shared],
                            "sliver_merge",
                            "MIN_EDGE_LENGTH",
                            (a.raw or b.raw or [a.canonical])[0],
                        )
                    next_groups.append(_merge_axis_groups(a, b, tol))
                    merged_any = True
                    i += 2
            else:
                next_groups.append(a)
                i += 1

        groups = next_groups
        if not merged_any:
            break

    mapping: dict[float, float] = {}
    for group in groups:
        cval = _axis_value(group.canonical)
        for raw in group.raw:
            mapping[_axis_value(raw)] = cval

    for group in groups:
        has_cross_floor = len(group.support_by_floor) > 1
        has_footprint_attach = bool(group.footprint_raw and group.node_ids)
        if not (has_cross_floor or has_footprint_attach):
            continue
        cval = _axis_value(group.canonical)
        for raw in sorted({_axis_value(v) for v in group.raw if v not in group.footprint_raw}):
            if abs(cval - raw) <= tol.output_precision_m:
                continue
            audit.append(
                {
                    "rule_id": "deterministic_core.cross_floor_align",
                    "stage": "core",
                    "target": f"{axis}.axis[{raw:.4f}]",
                    "axis": axis,
                    "original_value": round(raw, 4),
                    "resolved_value": round(cval, 4),
                    "delta": round(cval - raw, 4),
                    "tolerance_name": "CROSS_FLOOR_ALIGN_TOL+SNAP_GRID+MIN_EDGE_LENGTH",
                }
            )

    # F-133: flush the same-floor merge ledger. `mapping` has settled, so each
    # event can now name the value it actually landed on and how far each of the
    # collapsed axes had to travel to get there.
    for event in same_floor_merges:
        resolved = _axis_value(
            mapping.get(event["probe_raw"], event["values"][0])
        )
        values = event["values"]
        deltas = [round(resolved - v, 4) for v in values]
        audit.append(
            {
                "rule_id": "deterministic_core.same_floor_axis_merge",
                "stage": "core",
                "target": (
                    f"{axis}.axis[{event['floor']}:"
                    + "|".join(f"{v:.4f}" for v in values)
                    + "]"
                ),
                "axis": axis,
                "floor": event["floor"],
                "step": event["step"],
                "original_value": [round(v, 4) for v in values],
                "resolved_value": round(resolved, 4),
                "delta": max(deltas, key=abs),
                "per_value_delta": deltas,
                "separation": round(values[-1] - values[0], 4),
                "tolerance_name": event["tolerance_name"],
                "reason": (
                    "two or more distinct axes on ONE floor were collapsed into a "
                    "single canonical axis; if they were a real building step it "
                    "is gone from the geometry (correction cannot tell a real "
                    "step from model jitter -- see R-6)"
                ),
            }
        )

    return mapping, audit


def _snap(v: float, mapping: dict[float, float]) -> float:
    return mapping.get(round(float(v), 6), float(v))


def _clamp(v: float, lo: float, hi: float) -> float:
    return min(max(v, lo), hi)


def _close_to_boundary(v: float, lo: float, hi: float, thr: float) -> float:
    """Connectivity: pull a cell edge that falls just inside a footprint boundary
    out onto it (so the enclosure seals), within `thr`. Directional — only edges
    short of `lo`/`hi` by ≤ thr move; interior partitions are untouched."""
    if 0 < v - lo <= thr:
        return lo
    if 0 < hi - v <= thr:
        return hi
    return v


def _envelope_reject_entry(axis: str, resolution: EnvelopeAxisResolution, reason: str) -> dict:
    return {
        "target": f"{axis}.footprint",
        "reason": reason,
        "regime_assumption_violated": "facade authoritative envelope reconcile",
        "rule_id": "deterministic_core.envelope_reconcile",
        "axis": axis,
        "envelope_resolution": resolution.to_dict(),
    }


def _attached_boundary_cells(geom: CorrectedGeometry, axis: str, side: str, old_value: float, attach_tol: float) -> list[tuple]:
    out = []
    for fl in geom.floors:
        for c in fl.cells:
            values = c.x if axis == "x" else c.y
            idx = 0 if side == "lo" else 1
            if abs(float(values[idx]) - old_value) <= attach_tol + _EPS:
                out.append((fl, c, idx))
    return out


def _guard_envelope_axis_move(
    geom: CorrectedGeometry,
    axis: str,
    old_bounds: tuple[float, float],
    new_bounds: tuple[float, float],
    tol: CoreTolerances,
    resolution: EnvelopeAxisResolution,
) -> dict | None:
    # boundary_attach_tol is intentionally aliased to envelope_reconcile_tol_m:
    # the same wall-thickness-scale allowance that accepts the facade envelope
    # defines which old perimeter cell edges are considered attached.
    attach_tol = tol.envelope_reconcile_tol_m
    sides = [("lo", 0, old_bounds[0], new_bounds[0]), ("hi", 1, old_bounds[1], new_bounds[1])]
    offending: list[dict] = []
    for side, idx, old_value, new_value in sides:
        if abs(new_value - old_value) <= tol.output_precision_m:
            continue
        attached = _attached_boundary_cells(geom, axis, side, old_value, attach_tol)
        if not attached:
            offending.append({"side": side, "reason": "no cell edge attached to old footprint boundary"})
            continue
        for _, cell, edge_idx in attached:
            values = list(cell.x if axis == "x" else cell.y)
            other_idx = 1 - edge_idx
            other = float(values[other_idx])
            if edge_idx == 0:
                new_extent = other - new_value
                crosses = new_value >= other - _EPS
            else:
                new_extent = new_value - other
                crosses = new_value <= other + _EPS
            if crosses or new_extent < tol.min_edge_length_m - _EPS:
                offending.append(
                    {
                        "cell_id": cell.id,
                        "side": side,
                        "old_edge": round(old_value, 6),
                        "new_edge": round(new_value, 6),
                        "nearest_interior_axis": round(other, 6),
                        "new_extent": round(new_extent, 6),
                        "reason": "move would cross nearest interior axis, invert, or shrink below min_edge_length_m",
                    }
                )
    if offending:
        return _envelope_reject_entry(
            axis,
            resolution,
            "pre-move guard rejected envelope reconcile",
        ) | {"offending_cells": offending}
    return None


def _apply_legacy_envelope_reconcile(
    geom: CorrectedGeometry,
    tol: CoreTolerances,
    authoritative_envelope: AuthoritativeEnvelope | None,
    corrections: list[dict],
    unsupported: list[dict],
    conflicts: list[dict],
) -> tuple[list[float], list[float]]:
    fx = list(map(float, geom.footprint_x))
    fy = list(map(float, geom.footprint_y))
    if authoritative_envelope is None:
        return fx, fy
    # Preserve B1's legacy safety guard exactly: polygon-cell vertices are the
    # authority, so a bbox-only envelope move must never mutate only x/y.
    if schema_version_of(geom) != "3" and any(cell_has_polygon(c) for fl in geom.floors for c in fl.cells):
        unsupported.append(
            {
                "target": "footprint",
                "reason": (
                    "authoritative envelope reconcile for polygon cells is not "
                    "implemented in schema v2 B1; refusing to move bbox-only "
                    "cell edges without moving polygon vertices"
                ),
                "regime_assumption_violated": "rectangular envelope reconcile",
            }
        )
        return fx, fy
    # B2b owns every v3 ring shape (rectangle and non-rectangle alike) as one
    # late candidate transaction.  The legacy path below remains untouched.
    if schema_version_of(geom) == "3":
        return fx, fy

    accepted_any = False
    for axis, old_bounds in (("x", (fx[0], fx[1])), ("y", (fy[0], fy[1]))):
        resolution = authoritative_envelope.axis(axis)  # type: ignore[arg-type]
        if resolution is None:
            continue
        if resolution.status == "conflict":
            conflicts.append(_envelope_reject_entry(axis, resolution, resolution.reason or "facade envelope conflict"))
            continue
        if resolution.status != "accepted":
            unsupported.append(_envelope_reject_entry(axis, resolution, resolution.reason or "facade envelope skipped"))
            continue
        if resolution.bounds is None:
            unsupported.append(_envelope_reject_entry(axis, resolution, "origin ambiguity: accepted span has no bounds"))
            continue

        new_bounds = (float(resolution.bounds[0]), float(resolution.bounds[1]))
        old_span = old_bounds[1] - old_bounds[0]
        new_span = new_bounds[1] - new_bounds[0]
        max_delta = max(
            abs(new_bounds[0] - old_bounds[0]),
            abs(new_bounds[1] - old_bounds[1]),
            abs(new_span - old_span),
        )
        if max_delta > tol.envelope_reconcile_tol_m + _EPS:
            unsupported.append(
                _envelope_reject_entry(
                    axis,
                    resolution,
                    (
                        f"authoritative envelope delta {max_delta:.3f} m exceeds "
                        f"ENVELOPE_RECONCILE_TOL {tol.envelope_reconcile_tol_m:.3f} m"
                    ),
                )
            )
            continue

        guard = _guard_envelope_axis_move(geom, axis, old_bounds, new_bounds, tol, resolution)
        if guard is not None:
            unsupported.append(guard)
            continue

        moved_cells: list[dict] = []
        for side, idx, old_value, new_value in (
            ("lo", 0, old_bounds[0], new_bounds[0]),
            ("hi", 1, old_bounds[1], new_bounds[1]),
        ):
            if abs(new_value - old_value) <= tol.output_precision_m:
                continue
            for _, cell, edge_idx in _attached_boundary_cells(
                geom, axis, side, old_value, tol.envelope_reconcile_tol_m
            ):
                values = cell.x if axis == "x" else cell.y
                before = float(values[edge_idx])
                values[edge_idx] = new_value
                moved_cells.append(
                    {
                        "cell_id": cell.id,
                        "edge": f"{axis}[{edge_idx}]",
                        "side": side,
                        "original_value": round(before, 6),
                        "resolved_value": round(new_value, 6),
                    }
                )

        if axis == "x":
            fx = [new_bounds[0], new_bounds[1]]
            geom.footprint_x = fx
        else:
            fy = [new_bounds[0], new_bounds[1]]
            geom.footprint_y = fy
        if schema_version_of(geom) == "3":
            for floor in geom.floors:
                _set_v3_ring_bounds(floor, axis, new_bounds)

        source = resolution.source
        corrections.append(
            {
                "rule_id": "deterministic_core.envelope_reconcile",
                "stage": "core",
                "target": f"{axis}.footprint",
                "axis": axis,
                "source_view": source.view if source else None,
                "source_id": source.source_id if source else None,
                "source_kind": source.source_kind if source else None,
                "candidate_class": source.candidate_class if source else None,
                "original_bounds": [round(old_bounds[0], 6), round(old_bounds[1], 6)],
                "original_span": round(old_span, 6),
                "resolved_bounds": [round(new_bounds[0], 6), round(new_bounds[1], 6)],
                "resolved_span": round(new_span, 6),
                "delta": round(new_span - old_span, 6),
                "tolerance_name": "ENVELOPE_RECONCILE_TOL",
                "tolerance_value": tol.envelope_reconcile_tol_m,
                "boundary_attach_tol_name": "ENVELOPE_RECONCILE_TOL",
                "boundary_attach_tol_value": tol.envelope_reconcile_tol_m,
                "candidate": source.to_dict() if source else None,
                "corroborating_candidates": [c.to_dict() for c in resolution.corroborating_sources],
                "moved_cells": moved_cells,
            }
        )
        accepted_any = True

    if accepted_any:
        findings = validate_corrected_geometry(geom)
        failed = [f for f in findings if not f.ok and f.check_id in {"correction.coverage", "correction.nondegenerate"}]
        if failed:
            messages = "; ".join(f"{f.check_id}: {f.message}" for f in failed)
            raise ValueError(f"envelope reconcile violated corrected-geometry invariant: {messages}")
    return fx, fy


def _apply_envelope_reconcile(
    geom: CorrectedGeometry,
    tol: CoreTolerances,
    authoritative_envelope: AuthoritativeEnvelope | None,
    verified_window_inputs: "VerifiedWindowResolverInputs | None" = None,
    *,
    annotation_basis_sink: list | None = None,
) -> CorrectedGeometry:
    """Apply an envelope reconcile without leaking mutable audit side channels.

    Legacy geometry keeps the historical B1 routine verbatim; schema v3 uses
    B2b's fresh-copy transaction.  This is intentionally a small private
    dispatcher so callers cannot accidentally commit ring/cell changes while
    retaining audit from a rejected candidate.

    ``annotation_basis_sink`` (2026-08-12, 摊 C, opt-in, default None ->
    zero behaviour change for every existing caller): when a caller passes a
    mutable list, the v3 branch extends it with
    `apply_v3_envelope_transaction`'s pure `annotation_basis` observation
    (see envelope_transform.py). This is the only way to surface that
    observation without widening this function's / `apply_deterministic_core`'s
    return type (both are consumed too widely -- including the B5 writer's
    independent replay in stage_runner.py -- to safely change their return
    shape for a purely observational, non-blocking feature). Legacy v1/v2
    never populates it (out of scope for this batch; only v3 has an
    `AuthoritativeEnvelope`-derived per-side comparison worth naming).
    """
    geom = ensure_corrected_geometry(geom)
    if schema_version_of(geom) == "3":
        if authoritative_envelope is None:
            return geom
        from src.agent.correction.envelope_transform import apply_v3_envelope_transaction
        result = apply_v3_envelope_transaction(
            geom, tol, authoritative_envelope,
            verified_window_inputs=verified_window_inputs,
        )
        if annotation_basis_sink is not None:
            annotation_basis_sink.extend(result.annotation_basis)
        return result.geom
    corrections, unsupported, conflicts = list(geom.corrections), list(geom.unsupported), list(geom.conflicts)
    fx, fy = _apply_legacy_envelope_reconcile(geom, tol, authoritative_envelope, corrections, unsupported, conflicts)
    geom.footprint_x, geom.footprint_y = fx, fy
    geom.corrections, geom.unsupported, geom.conflicts = corrections, unsupported, conflicts
    return geom


def apply_deterministic_core(
    geom: CorrectedGeometry,
    tol: CoreTolerances | None = None,
    *,
    authoritative_envelope: AuthoritativeEnvelope | None = None,
    capability_profile: str = "rectangular",
    verified_window_inputs: "VerifiedWindowResolverInputs | None" = None,
    annotation_basis_sink: list | None = None,
) -> CorrectedGeometry:
    """Snap all geometry onto a global canonical axis set; append audit entries.

    Mutates and returns `geom` (corrections / unsupported appended). `tol`
    defaults to the active `correction.yaml` (overridable for testing).
    """
    geom = ensure_corrected_geometry(geom)
    if tol is None:
        tol = load_core_tolerances()
    require_supported_geometry_contract(geom, capability_profile)

    corrections = list(geom.corrections)
    unsupported = list(geom.unsupported)
    conflicts = list(geom.conflicts)

    # ---- structural axes: room / wall / footprint x,y only (NOT windows) ----
    per_floor_x: dict[str, list[float]] = {}
    per_floor_y: dict[str, list[float]] = {}
    for fl in geom.floors:
        xs: list[float] = []
        ys: list[float] = []
        for c in fl.cells:
            if cell_has_polygon(c) and not schema_supports(geom, FEATURE_CELL_POLYGON):
                raise ValueError(
                    f"cell {c.id}: polygon requires schema_version '2' or a schema version with feature '{FEATURE_CELL_POLYGON}'"
                )
            ccw = normalized_ccw_polygon(c)
            if ccw is not None:
                # Ring encoding (winding / explicit closure) is not geometry:
                # canonicalize to an open CCW ring instead of failing the draw.
                c.polygon = ccw
                corrections.append(
                    {
                        "rule_id": "POLYGON_WINDING_CCW",
                        "stage": "core",
                        "target": f"cell:{c.id}",
                        "axis": "xy",
                        "original_value": "non_canonical_ring(cw_or_closed)",
                        "resolved_value": "ccw_open_ring",
                        "delta": 0.0,
                        "tolerance_name": "NONE(encoding-only rewrite)",
                    }
                )
            validate_cell_polygon(c, min_edge_length_m=tol.min_edge_length_m)
            xs += cell_axis_values(c, "x")
            ys += cell_axis_values(c, "y")
        # V3 footprint vertices participate in the same identity buckets as
        # cell axes.  Legacy remains byte-for-byte on its old bbox branch.
        if schema_version_of(geom) == "3":
            ring_cell = type("FloorRing", (), {
                "id": f"floor:{fl.id}", "polygon": [list(p) for p in fl.footprint.vertices],
                "x": list(geom.footprint_x), "y": list(geom.footprint_y),
            })()
            canonical = normalized_ccw_polygon(ring_cell)
            if canonical is not None:
                fl.footprint = FootprintRing(vertices=[tuple(p) for p in canonical])
                corrections.append({"rule_id": "POLYGON_WINDING_CCW", "stage": "core",
                                    "target": f"floor:{fl.id}", "axis": "xy",
                                    "original_value": "non_canonical_ring(cw_or_closed)",
                                    "resolved_value": "ccw_open_ring", "delta": 0.0,
                                    "tolerance_name": "NONE(encoding-only rewrite)"})
            ring = floor_footprint(geom, fl)
            xs.extend(point[0] for point in ring)
            ys.extend(point[1] for point in ring)
        per_floor_x[floor_key(geom, fl)] = xs
        per_floor_y[floor_key(geom, fl)] = ys

    if schema_version_of(geom) == "3":
        # Top-level bbox is a compatibility projection only; producer values are
        # auditable but cannot override floor-owned geometry.
        (xmin, xmax), (ymin, ymax) = footprint_bbox(geom)
        if list(map(float, geom.footprint_x)) != [xmin, xmax] or list(map(float, geom.footprint_y)) != [ymin, ymax]:
            corrections.append({"rule_id": "deterministic_core.v3_bbox_projection", "stage": "core",
                                "target": "footprint_x/y", "original_value": [geom.footprint_x, geom.footprint_y],
                                "resolved_value": [[xmin, xmax], [ymin, ymax]], "delta": 0.0,
                                "tolerance_name": "NONE(exact derived projection)"})
        geom.footprint_x, geom.footprint_y = [xmin, xmax], [ymin, ymax]

    xmap, x_audit = _reconcile_cross_floor(per_floor_x, geom.footprint_x, tol, "x")
    ymap, y_audit = _reconcile_cross_floor(per_floor_y, geom.footprint_y, tol, "y")
    corrections.extend(x_audit)
    corrections.extend(y_audit)

    if schema_version_of(geom) == "3":
        for fl in geom.floors:
            snapped = [(_snap(x, xmap), _snap(y, ymap)) for x, y in floor_footprint(geom, fl)]
            fl.footprint = FootprintRing(vertices=snapped)
        (xmin, xmax), (ymin, ymax) = footprint_bbox(geom)
        geom.footprint_x, geom.footprint_y = [xmin, xmax], [ymin, ymax]

    def log(target: str, axis: str, before: float, after: float, rule: str) -> None:
        if abs(after - before) > tol.output_precision_m:
            corrections.append(
                {
                    "rule_id": rule,
                    "stage": "core",
                    "target": target,
                    "axis": axis,
                    "original_value": round(before, 4),
                    "resolved_value": round(after, 4),
                    "delta": round(after - before, 4),
                    "tolerance_name": "AXIS_JITTER_TOL+SNAP_GRID+MIN_EDGE_LENGTH",
                }
            )

    fx = [_snap(geom.footprint_x[0], xmap), _snap(geom.footprint_x[1], xmap)]
    fy = [_snap(geom.footprint_y[0], ymap), _snap(geom.footprint_y[1], ymap)]
    geom.footprint_x, geom.footprint_y = fx, fy
    if schema_version_of(geom) == "3":
        # B2b runs after canonical cell/window preparation below.
        fx, fy = geom.footprint_x, geom.footprint_y
    else:
        fx, fy = _apply_legacy_envelope_reconcile(
            geom, tol, authoritative_envelope, corrections, unsupported, conflicts,
        )
    gthr = tol.gap_close_threshold_m

    # ---- z-stack continuity: close small inter-floor gaps (the z counterpart of
    # gap_close). The split-pairing kernel pairs floor/ceiling only when
    # |lower top - upper z_floor| is within its z tolerance; a small LLM z jitter
    # would otherwise silently turn the interface into Roof + exposed Floor
    # (mid-building outdoors) that no downstream gate can see. Small gaps snap to
    # the lower floor's top (audit-logged); larger ones are flagged unsupported
    # here and hard-rejected by the kernel (build_zone_volumes). ----
    floors_by_z = sorted(geom.floors, key=lambda f: f.z_floor)
    for prev, cur in zip(floors_by_z, floors_by_z[1:]):
        top = prev.z_floor + prev.ceiling_height
        gap = cur.z_floor - top
        if gap == 0:
            continue
        if abs(gap) <= gthr:
            log(f"{cur.name}.z_floor", "z", cur.z_floor, top, "deterministic_core.z_stack")
            cur.z_floor = top
        else:
            unsupported.append(
                {
                    "target": cur.name,
                    "reason": (
                        f"broken z-stack: floor starts at z={cur.z_floor} but the "
                        f"floor below ('{prev.name}') tops out at z={top} "
                        f"(gap {gap:+.3f} m > {gthr} m)"
                    ),
                    "regime_assumption_violated": "contiguous stacked floors",
                }
            )

    def axis_then_reach(cid, label, orig, amap, lo, hi):
        """Axis-identity snap, then connectivity-close to a footprint boundary."""
        snapped = _snap(orig, amap)
        reached = _close_to_boundary(snapped, lo, hi, gthr)
        axis_name = "x" if label.startswith("x") or label.endswith(".x") else "y"
        log(f"{cid}.{label}", axis_name, orig, snapped, "deterministic_core.snap")
        log(f"{cid}.{label}", axis_name, snapped, reached, "deterministic_core.gap_close")
        return reached

    for fl in geom.floors:
        for c in fl.cells:
            if cell_has_polygon(c):
                snapped_vertices: list[tuple[float, float]] = []
                for idx, (x, y) in enumerate(cell_polygon_vertices(c) or []):
                    sx = axis_then_reach(c.id, f"polygon[{idx}].x", x, xmap, fx[0], fx[1])
                    sy = axis_then_reach(c.id, f"polygon[{idx}].y", y, ymap, fy[0], fy[1])
                    snapped_vertices.append((sx, sy))
                set_cell_polygon_vertices(c, snapped_vertices)
                validate_cell_polygon(c, min_edge_length_m=tol.min_edge_length_m)
            else:
                x0 = axis_then_reach(c.id, "x[0]", c.x[0], xmap, fx[0], fx[1])
                x1 = axis_then_reach(c.id, "x[1]", c.x[1], xmap, fx[0], fx[1])
                y0 = axis_then_reach(c.id, "y[0]", c.y[0], ymap, fy[0], fy[1])
                y1 = axis_then_reach(c.id, "y[1]", c.y[1], ymap, fy[0], fy[1])
                if (x1 - x0) < tol.min_edge_length_m or (y1 - y0) < tol.min_edge_length_m:
                    unsupported.append(
                        {
                            "target": c.id,
                            "reason": "cell collapsed below min_edge_length after snap",
                            "regime_assumption_violated": "non-degenerate room cell",
                        }
                    )
                c.x, c.y = [x0, x1], [y0, y1]

    # ---- windows: legacy parent clamp; v3 pre-host snap + floor-z clamp ----
    wgrid = tol.window_snap_grid_m
    cell_by_id: dict[str, tuple] = {}
    for fl in geom.floors:
        for c in fl.cells:
            if c.id in cell_by_id:
                # A duplicate id would make this lookup (and every id-keyed map in
                # the kernel) silently resolve to the wrong floor's cell. Fail loud.
                raise ValueError(
                    f"duplicate cell id '{c.id}' (also on floor "
                    f"'{cell_by_id[c.id][0].name}') — cell ids must be globally "
                    f"unique across floors (A0 id_uniqueness)"
                )
            cell_by_id[c.id] = (fl, c)
    kept_windows = []
    for w in geom.windows:
        s0, s1 = _snap_to_grid(w.span[0], wgrid), _snap_to_grid(w.span[1], wgrid)
        z0, z1 = _snap_to_grid(w.z[0], wgrid), _snap_to_grid(w.z[1], wgrid)
        if schema_version_of(geom) == "3":
            floor = next((floor for floor in geom.floors if floor.id == w.floor_id), None)
            if floor is None:
                raise ValueError(f"window {w.id}: unknown floor_id during pre-host core")
            zlo, zhi = floor.z_floor, floor.z_floor + floor.ceiling_height
            z0, z1 = _clamp(z0, zlo, zhi), _clamp(z1, zlo, zhi)
            log(f"{w.id}.span[0]", "span", w.span[0], s0, "deterministic_core.window")
            log(f"{w.id}.span[1]", "span", w.span[1], s1, "deterministic_core.window")
            log(f"{w.id}.z[0]", "z", w.z[0], z0, "deterministic_core.window")
            log(f"{w.id}.z[1]", "z", w.z[1], z1, "deterministic_core.window")
            # B5 source-aware resolution owns plan-span clamp and all topology
            # rejection.  Pre-host core never clamps a v3 span to a room/cell
            # bbox and never drops a window before resolver totality can see it.
            w.span, w.z = [s0, s1], [z0, z1]
            kept_windows.append(w)
            continue
        clamped = tol.window_clamp_to_parent and w.room in cell_by_id
        if clamped:
            fl, c = cell_by_id[w.room]
            lo, hi = (c.x[0], c.x[1]) if w.facade.lower().startswith(("n", "s")) else (c.y[0], c.y[1])
            s0, s1 = _clamp(s0, lo, hi), _clamp(s1, lo, hi)
            zlo, zhi = fl.z_floor, fl.z_floor + fl.ceiling_height
            z0, z1 = _clamp(z0, zlo, zhi), _clamp(z1, zlo, zhi)
        # Post-clamp sanity: clamping an out-of-range window must not manufacture
        # illegal geometry. A degenerate window (zero/sub-tolerance width or
        # height) or one covering its parent cell's full facade AND full floor
        # height (subsurface area >= base surface — an EnergyPlus severe) is
        # dropped explicitly with an unsupported entry, never passed downstream.
        reason = None
        if (s1 - s0) < tol.min_edge_length_m or (z1 - z0) < tol.min_edge_length_m:
            reason = (
                f"degenerate after snap/clamp: span [{s0}, {s1}], z [{z0}, {z1}] "
                f"(width or height < {tol.min_edge_length_m} m)"
            )
        elif clamped and s0 <= lo and s1 >= hi and z0 <= zlo and z1 >= zhi:
            reason = (
                f"covers the full wall face after clamp (span [{s0}, {s1}] = cell "
                f"edge, z [{z0}, {z1}] = full floor height) — a subsurface must be "
                f"strictly smaller than its base surface"
            )
        if reason is not None:
            unsupported.append(
                {
                    "target": w.id,
                    "reason": reason,
                    "regime_assumption_violated": "window strictly inside its parent wall",
                }
            )
            continue
        log(f"{w.id}.span[0]", "span", w.span[0], s0, "deterministic_core.window")
        log(f"{w.id}.span[1]", "span", w.span[1], s1, "deterministic_core.window")
        log(f"{w.id}.z[0]", "z", w.z[0], z0, "deterministic_core.window")
        log(f"{w.id}.z[1]", "z", w.z[1], z1, "deterministic_core.window")
        w.span, w.z = [s0, s1], [z0, z1]
        kept_windows.append(w)
    geom.windows = kept_windows

    if schema_version_of(geom) == "3":
        # The B2b candidate transaction runs only after canonical ring/cell and
        # window snapping.  It receives the complete current audit state so a
        # rejection can return a fresh, unmodified geometry plus one conflict.
        geom.corrections = corrections
        geom.conflicts = conflicts
        geom.unsupported = unsupported
        if authoritative_envelope is not None:
            # Same late (post canonical cell/window) timing as B2b §5.1; the
            # dispatcher is also the only private reconciliation entry point.
            geom = _apply_envelope_reconcile(
                geom, tol, authoritative_envelope,
                verified_window_inputs=verified_window_inputs,
                annotation_basis_sink=annotation_basis_sink,
            )
        (xmin, xmax), (ymin, ymax) = footprint_bbox(geom)
        geom.footprint_x, geom.footprint_y = [xmin, xmax], [ymin, ymax]
        # F-22 BLOCKER-1: the UNCONDITIONAL "I ran, version is X" stamp — the
        # LAST statement before this function's only v3 return, so every v3
        # completion carries it regardless of which branch above did or did
        # not fire (zero intents / rejected-and-rolled-back / committed all
        # reach here the same way). A plain, unconditional assignment (never
        # "if not already set"): this also means a forged value a draw might
        # have slipped past the CORRECTION_DRAW_FORBIDDEN preflight gate on
        # (see DeterministicCoreStampV1's docstring) gets clobbered by the
        # real one on every genuine core pass, independent of that gate.
        geom.deterministic_core_stamp = DeterministicCoreStampV1(version=DETERMINISTIC_CORE_STAMP_VERSION)
        return validate_final_corrected_geometry(geom)
    geom.corrections = corrections
    geom.conflicts = conflicts
    geom.unsupported = unsupported
    return geom
