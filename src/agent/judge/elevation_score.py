"""Elevation-window scorer for judge-side reading/correction grade evidence.

This module scores facade elevation windows in their natural metric coordinates:
along-facade span plus absolute z ``[sill, head]``.  It deliberately does not use
``reading_score._as_segment`` because collapsing rectangles to midlines erases
the z interval that this scorer exists to grade.

Judge-side only: it imports gt helpers and must stay under ``src.agent.judge``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import inf
from pathlib import Path
import json
from typing import Iterable, Literal

# Deliberately import Va's binding, not the similarly named DXF extraction
# binding from gt_manifest.  This adapter is a judge-side consumer of the
# reviewed frame trust root.
from src.agent.correction.facade_applicability import ElevationViewBindingV1
from src.agent.judge.score_schema import ScoreContractError

from .gt import load_gt

DEFAULT_ELEVATION_ALONG_TOL_M = 0.40
DEFAULT_SILL_TOL_M = 0.30
DEFAULT_HEAD_TOL_M = 0.30
DEFAULT_WIDTH_TOL_M = 0.40
DEFAULT_OVERLAP_ACCEPT = 0.75
DEFAULT_OVERLAP_COMPLETE = 0.95
DEFAULT_FLOOR_LINE_TOL_M = 0.30
DEFAULT_ELEVATION_OVERLAP_MIN = DEFAULT_OVERLAP_ACCEPT

FACADE_NAMES = ("North", "South", "East", "West")
FACADE_CODES = {"North": "N", "South": "S", "East": "E", "West": "W"}
_FACADE_ALIASES = {
    "north": "North",
    "n": "North",
    "south": "South",
    "s": "South",
    "east": "East",
    "e": "East",
    "west": "West",
    "w": "West",
}


@dataclass(frozen=True)
class TypedElevationObservation:
    """A normalized product rectangle before its trusted-frame projection.

    Product supplied mirror/local-x fields intentionally do not exist here.
    The only permitted coordinate transform is the reviewed Va binding.
    """

    observation_id: str
    source_input_id: str
    floor_id: str
    kind: Literal["window", "door"]
    facade_family: Literal["North", "South", "East", "West"]
    local_x_interval: tuple[float, float]
    z_interval: tuple[float, float] | None


@dataclass(frozen=True)
class ProjectedElevationObservation:
    """World-frame evidence handed to the B4b opening scorer."""

    observation_id: str
    source_input_id: str
    floor_id: str
    kind: Literal["window", "door"]
    facade_family: Literal["North", "South", "East", "West"]
    world_along_interval: tuple[float, float]
    z_interval: tuple[float, float] | None


def project_typed_elevation_observation(*, observation: TypedElevationObservation,
                                        binding: ElevationViewBindingV1,
                                        direction_semantics: Literal["building_axis", "true_azimuth", "unknown"] = "building_axis") -> ProjectedElevationObservation:
    """Project actual local-x geometry through the reviewed forward map.

    ``true_azimuth`` and ``unknown`` inputs require the reviewed external
    direction sidecar.  No product declaration can substitute for it.  This
    intentionally does *not* clip against Vg visible intervals: Va remains the
    sole applicability engine and performs that later on its public path.
    """
    if (direction_semantics in {"true_azimuth", "unknown"}
            and binding.resolution_source != "resolved_direction_sidecar"):
        raise ScoreContractError("score_direction_unresolved", "scoring.view_bindings",
                                 context={"input_id": binding.input_id,
                                          "direction_semantics": direction_semantics})
    if observation.source_input_id != binding.input_id or observation.facade_family != binding.resolved_building_direction:
        raise ScoreContractError("score_view_binding_invalid", "scoring.view_bindings",
                                 context={"observation_id": observation.observation_id})
    lo, hi = observation.local_x_interval
    if lo > hi:
        raise ScoreContractError("score_product_identity_invalid", "scoring.input_identity",
                                 context={"observation_id": observation.observation_id, "reason": "invalid_local_interval"})
    a = binding.along_origin + binding.sign * lo
    b = binding.along_origin + binding.sign * hi
    return ProjectedElevationObservation(
        observation_id=observation.observation_id, source_input_id=observation.source_input_id,
        floor_id=observation.floor_id, kind=observation.kind, facade_family=observation.facade_family,
        world_along_interval=(min(a, b), max(a, b)), z_interval=observation.z_interval,
    )


def score_typed_elevation_floor_lines(*, binding: ElevationViewBindingV1,
                                      gt_floor_zs: Iterable[float], product_zs: Iterable[float] | None,
                                      tolerance_m: float) -> "FloorLineScore":
    """Judge-only typed adapter for absolute elevation floor-line evidence.

    z is already in the building world frame; unlike along it has no mirror or
    local-x transform.  The binding still establishes that this is a trusted
    elevation source for the requested facade.
    """
    if tolerance_m < 0:
        raise ScoreContractError("score_product_identity_invalid", "scoring.input_identity",
                                 context={"reason": "negative_floor_line_tolerance"})
    return _score_floor_lines(binding.resolved_building_direction,
                              None if product_zs is None else list(product_zs), list(gt_floor_zs),
                              tol=tolerance_m)


@dataclass(frozen=True)
class ElevationBox:
    span: tuple[float, float]
    z: tuple[float, float]
    source_id: str | None = None
    original_span: tuple[float, float] | None = None

    @property
    def center(self) -> float:
        return (self.span[0] + self.span[1]) / 2.0

    @property
    def width(self) -> float:
        return self.span[1] - self.span[0]


@dataclass(frozen=True)
class GtElevationWindow:
    id: str
    facade: str
    floor: str
    span: tuple[float, float]
    z: tuple[float, float]

    @property
    def center(self) -> float:
        return (self.span[0] + self.span[1]) / 2.0

    @property
    def width(self) -> float:
        return self.span[1] - self.span[0]


@dataclass(frozen=True)
class ReadElevationWindow:
    id: str
    facade: str
    span: tuple[float, float]
    z: tuple[float, float]
    source_view: str | None = None
    floor: str | None = None

    @property
    def center(self) -> float:
        return (self.span[0] + self.span[1]) / 2.0

    @property
    def width(self) -> float:
        return self.span[1] - self.span[0]


@dataclass
class ElevationWindowMatch:
    status: str
    facade: str
    floor: str
    orientation: str
    truth: GtElevationWindow | None = None
    read: ElevationBox | None = None
    source_id: str | None = None
    deltas: dict[str, float | None] = field(default_factory=dict)
    overlap_ratio: float | None = None
    gt_coverage: float | None = None
    product_coverage: float | None = None

    @property
    def overlap_fraction(self) -> float | None:
        return self.overlap_ratio


@dataclass
class FloorLineMatch:
    facade: str
    gt_z: float
    product_z: float | None
    status: str
    delta: float | None


@dataclass
class FloorLineExtra:
    facade: str
    product_z: float
    status: str = "extra"


@dataclass
class FloorLineScore:
    facade: str
    gt_floor_lines: list[float]
    product_floor_lines: list[float]
    matches: list[FloorLineMatch] = field(default_factory=list)
    extras: list[FloorLineExtra] = field(default_factory=list)
    no_data: bool = False
    no_data_reason: str | None = None


@dataclass
class ElevationFacadeFloorScore:
    facade: str
    floor: str
    orientation: str = "aligned"
    no_data: bool = False
    gt_count: int = 0
    read_count: int = 0
    matches: list[ElevationWindowMatch] = field(default_factory=list)
    extras: list[ElevationWindowMatch] = field(default_factory=list)

    def matched_hits(self) -> tuple[int, int]:
        matched = sum(1 for m in self.matches if m.status in {"complete", "within_tol"})
        return matched, self.gt_count

    def placed_hits(self) -> tuple[int, int]:
        hit = sum(1 for m in self.matches if m.status == "complete")
        return hit, self.gt_count


@dataclass
class ElevationScoreResult:
    scores: dict[str, dict[str, ElevationFacadeFloorScore]]
    evidence: list[dict] = field(default_factory=list)
    orientation_by_facade: dict[str, str] = field(default_factory=dict)
    floor_lines: dict[str, FloorLineScore] = field(default_factory=dict)

    def summary(self) -> dict[str, int]:
        complete = within = misses = extras = no_data = gt_total = 0
        for floors in self.scores.values():
            for score in floors.values():
                gt_total += score.gt_count
                if score.no_data:
                    no_data += 1
                for match in score.matches:
                    if match.status == "complete":
                        complete += 1
                    elif match.status == "within_tol":
                        within += 1
                    elif match.status == "miss":
                        misses += 1
                extras += len(score.extras)
        return {
            "gt_total": gt_total,
            "matched_total": complete + within,
            "complete_total": complete,
            "within_tol_total": within,
            "placed_hit_total": complete,
            "z_drift_total": 0,
            "miss_total": misses,
            "extra_total": extras,
            "no_data_floor_facades": no_data,
        }


@dataclass(frozen=True)
class _Candidate:
    gt_idx: int
    read_idx: int
    status: str
    deltas: dict[str, float]
    overlap_ratio: float
    gt_coverage: float
    product_coverage: float
    cost: float
    read_box: ElevationBox


@dataclass
class _FacadeMatch:
    orientation: str
    matches: list[ElevationWindowMatch]
    extras: list[ElevationWindowMatch]
    complete: int
    within_tol: int
    matched: int
    cost: float


def _facade_name(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    return _FACADE_ALIASES.get(raw.strip().lower())


def _round_pair(vals: tuple[float, float]) -> tuple[float, float]:
    return (round(vals[0], 3), round(vals[1], 3))


def _numeric_pair(value: object) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        a = float(value[0])
        b = float(value[1])
    except (TypeError, ValueError):
        return None
    return (min(a, b), max(a, b))


def _strokes(reading: dict) -> list[dict]:
    def find(o):
        if isinstance(o, dict):
            if isinstance(o.get("strokes"), list):
                return o["strokes"]
            for v in o.values():
                r = find(v)
                if r is not None:
                    return r
        if isinstance(o, list):
            for v in o:
                r = find(v)
                if r is not None:
                    return r
        return None

    return find(reading) or []


def facade_span_limit(gt: dict, facade: str) -> float:
    fp = gt.get("footprint", {})
    if facade in {"North", "South"}:
        return float(fp["W_m"])
    return float(fp["D_m"])


def floor_bands(gt: dict) -> list[tuple[str, float, float]]:
    floors = sorted(gt.get("floors", []), key=lambda f: float(f.get("z_floor", 0.0)))
    out: list[tuple[str, float, float]] = []
    for idx, floor in enumerate(floors):
        z0 = float(floor.get("z_floor", 0.0))
        if idx + 1 < len(floors):
            z1 = float(floors[idx + 1].get("z_floor", z0))
        else:
            z1 = z0 + float(floor.get("ceiling_height", 0.0))
        out.append((str(floor.get("name")), z0, z1))
    return out


def derive_gt_floor_lines(gt: dict) -> list[float]:
    lines = {0.0}
    top = 0.0
    for floor in gt.get("floors", []):
        z0 = float(floor.get("z_floor", 0.0))
        ceiling = float(floor.get("ceiling_height", 0.0))
        if z0 > 0.0:
            lines.add(round(z0, 3))
        top = max(top, z0 + ceiling)
    lines.add(round(top, 3))
    return sorted(lines)


def _score_floor_lines(
    facade: str,
    product_lines: list[float] | None,
    gt_lines: list[float],
    *,
    tol: float,
    no_data_reason: str = "no_product_floor_line_source",
) -> FloorLineScore:
    if product_lines is None:
        return FloorLineScore(
            facade=facade,
            gt_floor_lines=list(gt_lines),
            product_floor_lines=[],
            no_data=True,
            no_data_reason=no_data_reason,
        )
    pool = sorted({round(float(z), 3) for z in product_lines})
    matches: list[FloorLineMatch] = []
    for gt_z in gt_lines:
        if not pool:
            matches.append(FloorLineMatch(facade=facade, gt_z=round(gt_z, 3), product_z=None, status="miss", delta=None))
            continue
        nearest = min(pool, key=lambda z: abs(z - gt_z))
        delta = nearest - gt_z
        if abs(delta) <= tol:
            status = "complete" if abs(delta) <= 1e-6 else "within_tol"
            matches.append(
                FloorLineMatch(
                    facade=facade,
                    gt_z=round(gt_z, 3),
                    product_z=round(nearest, 3),
                    status=status,
                    delta=round(delta, 3),
                )
            )
            pool.remove(nearest)
        else:
            matches.append(FloorLineMatch(facade=facade, gt_z=round(gt_z, 3), product_z=None, status="miss", delta=None))
    return FloorLineScore(
        facade=facade,
        gt_floor_lines=[round(float(z), 3) for z in gt_lines],
        product_floor_lines=sorted({round(float(z), 3) for z in product_lines}),
        matches=matches,
        extras=[FloorLineExtra(facade=facade, product_z=z) for z in pool],
    )


def extract_reading_floor_line_zs(view: dict) -> list[float] | None:
    if view.get("image_kind") != "elevation":
        return None
    zs: list[float] = []
    for stroke in _strokes(view):
        if stroke.get("pen") != "wall_fill":
            continue
        z = _numeric_pair((stroke.get("geometry") or {}).get("y_range_m"))
        if z is None:
            continue
        zs.extend([round(z[0], 3), round(z[1], 3)])
    if not zs:
        return None
    return sorted(set(zs))


def extract_correction_floor_line_zs(geom) -> list[float] | None:
    floors = sorted(getattr(geom, "floors", []), key=lambda f: float(getattr(f, "z_floor", 0.0)))
    if not floors:
        return None
    lines = {0.0}
    top = 0.0
    for floor in floors:
        z0 = float(getattr(floor, "z_floor", 0.0))
        ceiling = float(getattr(floor, "ceiling_height", 0.0))
        if z0 > 0.0:
            lines.add(round(z0, 3))
        top = max(top, z0 + ceiling)
    lines.add(round(top, 3))
    return sorted(lines)


def _floor_for_z(z: float, bands: list[tuple[str, float, float]]) -> str | None:
    if not bands:
        return None
    for idx, (name, z0, z1) in enumerate(bands):
        if idx == len(bands) - 1:
            if z0 <= z <= z1:
                return name
        elif z0 <= z < z1:
            return name
    return min(bands, key=lambda b: min(abs(z - b[1]), abs(z - b[2])))[0]


def _floor_index(name: str | None, bands: list[tuple[str, float, float]]) -> int | None:
    if name is None:
        return None
    for idx, (floor_name, _z0, _z1) in enumerate(bands):
        if floor_name == name:
            return idx
    return None


def derive_gt_elevation_windows(gt: dict, *, evidence: list[dict] | None = None) -> dict[str, list[GtElevationWindow]]:
    out: dict[str, list[GtElevationWindow]] = {facade: [] for facade in FACADE_NAMES}
    for entry in gt.get("windows", []):
        facade = _facade_name(entry.get("facade"))
        floor = str(entry.get("floor"))
        if facade is None:
            continue
        for idx, opening in enumerate(entry.get("openings", [])):
            try:
                x = float(opening.get("x_m"))
                width = float(opening.get("width_m"))
            except (TypeError, ValueError):
                if evidence is not None:
                    evidence.append(
                        {
                            "type": "unusable_gt_opening",
                            "facade": facade,
                            "floor": floor,
                            "index": idx,
                            "reason": "missing numeric x_m/width_m",
                        }
                    )
                continue
            sill_raw = opening.get("sill_m", entry.get("sill_m"))
            head_raw = opening.get("head_m", entry.get("head_m"))
            try:
                sill = float(sill_raw)
                head = float(head_raw)
            except (TypeError, ValueError):
                if evidence is not None:
                    evidence.append(
                        {
                            "type": "unusable_gt_opening",
                            "facade": facade,
                            "floor": floor,
                            "index": idx,
                            "reason": "missing numeric sill_m/head_m",
                        }
                    )
                continue
            out[facade].append(
                GtElevationWindow(
                    id=f"{facade}/{floor}/{idx}",
                    facade=facade,
                    floor=floor,
                    span=_round_pair((x, x + width)),
                    z=_round_pair((min(sill, head), max(sill, head))),
                )
            )
    for facade in FACADE_NAMES:
        out[facade].sort(key=lambda w: (w.floor, w.center, w.z[0], w.z[1]))
    return out


def extract_reading_elevation_windows(
    view: dict,
    *,
    view_key: str,
    gt: dict,
    evidence: list[dict],
) -> tuple[str | None, list[ReadElevationWindow]]:
    if view.get("image_kind") != "elevation":
        return None, []
    facade = _facade_name((view.get("facade") or {}).get("view_facade"))
    if facade is None:
        evidence.append(
            {"type": "unmatched_elevation_view", "view": view_key, "reason": "missing facade.view_facade"}
        )
        return None, []
    span_limit = facade_span_limit(gt, facade)
    out: list[ReadElevationWindow] = []
    for stroke in _strokes(view):
        if stroke.get("pen") != "window":
            continue
        geom = stroke.get("geometry", {})
        sid = str(stroke.get("id", ""))
        span = _numeric_pair(geom.get("x_range_m"))
        z = _numeric_pair(geom.get("y_range_m"))
        if span is None or z is None:
            evidence.append(
                {
                    "type": "unusable_elevation_window",
                    "view": view_key,
                    "facade": facade,
                    "stroke_id": sid,
                    "reason": "window stroke lacks numeric x_range_m/y_range_m",
                    "geometry_kind": geom.get("kind"),
                }
            )
            continue
        if span[1] < -DEFAULT_ELEVATION_ALONG_TOL_M or span[0] > span_limit + DEFAULT_ELEVATION_ALONG_TOL_M:
            evidence.append(
                {
                    "type": "elevation_window_span_out_of_bounds",
                    "view": view_key,
                    "facade": facade,
                    "stroke_id": sid,
                    "span": list(_round_pair(span)),
                    "span_limit_m": span_limit,
                }
            )
        out.append(
            ReadElevationWindow(
                id=sid,
                facade=facade,
                span=_round_pair(span),
                z=_round_pair(z),
                source_view=view_key,
            )
        )
    return facade, out


def _oriented_box(read: ReadElevationWindow, orientation: str, span_limit: float) -> ElevationBox:
    if orientation == "flipped":
        span = (span_limit - read.span[1], span_limit - read.span[0])
    else:
        span = read.span
    span = _round_pair((min(span), max(span)))
    return ElevationBox(span=span, z=read.z, source_id=read.id, original_span=read.span)


def _coverage(read_box: ElevationBox, truth: GtElevationWindow) -> tuple[float, float, float]:
    span_overlap = max(0.0, min(read_box.span[1], truth.span[1]) - max(read_box.span[0], truth.span[0]))
    z_overlap = max(0.0, min(read_box.z[1], truth.z[1]) - max(read_box.z[0], truth.z[0]))
    inter_area = span_overlap * z_overlap
    read_area = max(0.0, read_box.width) * max(0.0, read_box.z[1] - read_box.z[0])
    truth_area = max(0.0, truth.width) * max(0.0, truth.z[1] - truth.z[0])
    if read_area <= 0.0 or truth_area <= 0.0:
        return 0.0, 0.0, 0.0
    gt_cov = inter_area / truth_area
    product_cov = inter_area / read_area
    return min(gt_cov, product_cov), gt_cov, product_cov


def _candidate(
    *,
    gt_idx: int,
    read_idx: int,
    truth: GtElevationWindow,
    read: ReadElevationWindow,
    read_box: ElevationBox,
    along_tol: float,
    sill_tol: float,
    head_tol: float,
    width_tol: float,
    overlap_accept: float,
    overlap_complete: float,
    require_same_read_floor: bool,
) -> _Candidate | None:
    if require_same_read_floor and read.floor != truth.floor:
        return None
    overlap, gt_cov, product_cov = _coverage(read_box, truth)
    if overlap < overlap_accept:
        return None
    along_delta = read_box.center - truth.center
    sill_delta = read_box.z[0] - truth.z[0]
    head_delta = read_box.z[1] - truth.z[1]
    width_delta = read_box.width - truth.width
    status = "complete" if overlap >= overlap_complete else "within_tol"
    cost = -overlap
    return _Candidate(
        gt_idx=gt_idx,
        read_idx=read_idx,
        status=status,
        deltas={
            "along_center_m": round(along_delta, 3),
            "sill_m": round(sill_delta, 3),
            "head_m": round(head_delta, 3),
            "width_m": round(width_delta, 3),
        },
        overlap_ratio=round(overlap, 3),
        gt_coverage=round(gt_cov, 3),
        product_coverage=round(product_cov, 3),
        cost=cost,
        read_box=read_box,
    )


def _match_facade(
    *,
    facade: str,
    reads: list[ReadElevationWindow],
    truth: list[GtElevationWindow],
    bands: list[tuple[str, float, float]],
    span_limit: float,
    orientation: str,
    along_tol: float,
    sill_tol: float,
    head_tol: float,
    width_tol: float,
    overlap_accept: float,
    overlap_complete: float,
    require_same_read_floor: bool = False,
) -> _FacadeMatch:
    boxes = [_oriented_box(read, orientation, span_limit) for read in reads]
    candidates_by_gt: list[list[_Candidate]] = []
    for gi, gt_win in enumerate(truth):
        row: list[_Candidate] = []
        for ri, read in enumerate(reads):
            cand = _candidate(
                gt_idx=gi,
                read_idx=ri,
                truth=gt_win,
                read=read,
                read_box=boxes[ri],
                along_tol=along_tol,
                sill_tol=sill_tol,
                head_tol=head_tol,
                width_tol=width_tol,
                overlap_accept=overlap_accept,
                overlap_complete=overlap_complete,
                require_same_read_floor=require_same_read_floor,
            )
            if cand is not None:
                row.append(cand)
        row.sort(key=lambda c: (c.cost, reads[c.read_idx].id))
        candidates_by_gt.append(row)

    best: tuple[int, int, int, float, list[_Candidate | None], set[int]] | None = None

    def key_of(complete: int, within_tol: int, matched: int, cost: float) -> tuple[int, int, int, float]:
        return complete, within_tol, matched, -round(cost, 9)

    def search(
        gi: int,
        used: set[int],
        chosen: list[_Candidate | None],
        complete: int,
        within_tol: int,
        matched: int,
        cost: float,
    ) -> None:
        nonlocal best
        if gi == len(truth):
            if best is None or key_of(complete, within_tol, matched, cost) > key_of(best[0], best[1], best[2], best[3]):
                best = (complete, within_tol, matched, cost, list(chosen), set(used))
            return
        for cand in candidates_by_gt[gi]:
            if cand.read_idx in used:
                continue
            chosen.append(cand)
            used.add(cand.read_idx)
            search(
                gi + 1,
                used,
                chosen,
                complete + (1 if cand.status == "complete" else 0),
                within_tol + (1 if cand.status == "within_tol" else 0),
                matched + 1,
                cost + cand.cost,
            )
            used.remove(cand.read_idx)
            chosen.pop()
        chosen.append(None)
        search(gi + 1, used, chosen, complete, within_tol, matched, cost)
        chosen.pop()

    search(0, set(), [], 0, 0, 0, 0.0)
    complete, within_tol, matched, cost, chosen, used = best or (0, 0, 0, 0.0, [], set())

    matches: list[ElevationWindowMatch] = []
    for gi, gt_win in enumerate(truth):
        cand = chosen[gi] if gi < len(chosen) else None
        if cand is None:
            matches.append(
                ElevationWindowMatch(
                    status="miss",
                    facade=facade,
                    floor=gt_win.floor,
                    orientation=orientation,
                    truth=gt_win,
                    read=None,
                    source_id=None,
                    deltas={
                        "along_center_m": None,
                        "sill_m": None,
                    "head_m": None,
                    "width_m": None,
                    },
                    overlap_ratio=None,
                    gt_coverage=None,
                    product_coverage=None,
                )
            )
            continue
        matches.append(
            ElevationWindowMatch(
                status=cand.status,
                facade=facade,
                floor=gt_win.floor,
                orientation=orientation,
                truth=gt_win,
                read=cand.read_box,
                source_id=reads[cand.read_idx].id,
                deltas=cand.deltas,
                overlap_ratio=cand.overlap_ratio,
                gt_coverage=cand.gt_coverage,
                product_coverage=cand.product_coverage,
            )
        )

    extras: list[ElevationWindowMatch] = []
    for ri, read in enumerate(reads):
        if ri in used:
            continue
        floor = read.floor or _floor_for_z((read.z[0] + read.z[1]) / 2.0, bands) or "unknown"
        box = boxes[ri]
        extras.append(
            ElevationWindowMatch(
                status="extra",
                facade=facade,
                floor=floor,
                orientation=orientation,
                truth=None,
                read=box,
                source_id=read.id,
                deltas={
                    "along_center_m": None,
                    "sill_m": None,
                    "head_m": None,
                    "width_m": None,
                },
                overlap_ratio=None,
                gt_coverage=None,
                product_coverage=None,
            )
        )
    extras.sort(key=lambda m: (m.floor, m.read.center if m.read else inf, m.source_id or ""))
    return _FacadeMatch(
        orientation=orientation,
        matches=matches,
        extras=extras,
        complete=complete,
        within_tol=within_tol,
        matched=matched,
        cost=cost,
    )


def _choose_reading_orientation(
    *,
    facade: str,
    reads: list[ReadElevationWindow],
    truth: list[GtElevationWindow],
    gt: dict,
    bands: list[tuple[str, float, float]],
    along_tol: float,
    sill_tol: float,
    head_tol: float,
    width_tol: float,
    overlap_accept: float,
    overlap_complete: float,
) -> _FacadeMatch:
    span_limit = facade_span_limit(gt, facade)
    aligned = _match_facade(
        facade=facade,
        reads=reads,
        truth=truth,
        bands=bands,
        span_limit=span_limit,
        orientation="aligned",
        along_tol=along_tol,
        sill_tol=sill_tol,
        head_tol=head_tol,
        width_tol=width_tol,
        overlap_accept=overlap_accept,
        overlap_complete=overlap_complete,
    )
    flipped = _match_facade(
        facade=facade,
        reads=reads,
        truth=truth,
        bands=bands,
        span_limit=span_limit,
        orientation="flipped",
        along_tol=along_tol,
        sill_tol=sill_tol,
        head_tol=head_tol,
        width_tol=width_tol,
        overlap_accept=overlap_accept,
        overlap_complete=overlap_complete,
    )
    if (aligned.complete, aligned.within_tol, aligned.matched) > (flipped.complete, flipped.within_tol, flipped.matched):
        return aligned
    if (flipped.complete, flipped.within_tol, flipped.matched) > (aligned.complete, aligned.within_tol, aligned.matched):
        return flipped
    if reads and truth and abs(aligned.cost - flipped.cost) <= 0.05:
        aligned.orientation = "ambiguous"
        for match in aligned.matches + aligned.extras:
            match.orientation = "ambiguous"
        return aligned
    return aligned if aligned.cost <= flipped.cost else flipped


def _blank_scores(gt: dict) -> dict[str, dict[str, ElevationFacadeFloorScore]]:
    floors = [str(f.get("name")) for f in gt.get("floors", [])]
    return {
        facade: {
            floor: ElevationFacadeFloorScore(facade=facade, floor=floor)
            for floor in floors
        }
        for facade in FACADE_NAMES
    }


def _populate_facade_scores(
    scores: dict[str, dict[str, ElevationFacadeFloorScore]],
    *,
    facade: str,
    orientation: str,
    matches: Iterable[ElevationWindowMatch],
    extras: Iterable[ElevationWindowMatch],
    gt_by_floor_count: dict[str, int],
    read_by_floor_count: dict[str, int],
    no_data: bool = False,
) -> None:
    for floor, score in scores[facade].items():
        score.orientation = orientation
        score.no_data = no_data
        score.gt_count = gt_by_floor_count.get(floor, 0)
        score.read_count = read_by_floor_count.get(floor, 0)
        score.matches = []
        score.extras = []
    for match in matches:
        if match.floor in scores[facade]:
            scores[facade][match.floor].matches.append(match)
    for extra in extras:
        if extra.floor in scores[facade]:
            scores[facade][extra.floor].extras.append(extra)


def score_reading_elevation_views(
    output: dict,
    gt: dict,
    *,
    elevation_along_tol_m: float = DEFAULT_ELEVATION_ALONG_TOL_M,
    sill_tol_m: float = DEFAULT_SILL_TOL_M,
    head_tol_m: float = DEFAULT_HEAD_TOL_M,
    width_tol_m: float = DEFAULT_WIDTH_TOL_M,
    overlap_accept: float = DEFAULT_OVERLAP_ACCEPT,
    overlap_complete: float = DEFAULT_OVERLAP_COMPLETE,
    floor_line_tol_m: float = DEFAULT_FLOOR_LINE_TOL_M,
) -> ElevationScoreResult:
    evidence: list[dict] = []
    bands = floor_bands(gt)
    gt_by_facade = derive_gt_elevation_windows(gt, evidence=evidence)
    gt_floor_lines = derive_gt_floor_lines(gt)
    scores = _blank_scores(gt)
    reads_by_facade: dict[str, list[ReadElevationWindow]] = {facade: [] for facade in FACADE_NAMES}
    floor_lines_by_facade: dict[str, list[float] | None] = {facade: None for facade in FACADE_NAMES}
    seen_facades: set[str] = set()

    for view_key, view in sorted(output.items()):
        if not isinstance(view, dict) or view.get("image_kind") != "elevation":
            continue
        facade, reads = extract_reading_elevation_windows(view, view_key=view_key, gt=gt, evidence=evidence)
        if facade is None:
            continue
        seen_facades.add(facade)
        reads_by_facade[facade].extend(reads)
        line_zs = extract_reading_floor_line_zs(view)
        if line_zs is None:
            evidence.append({"type": "elevation_floor_lines_no_data", "facade": facade, "view": view_key})
        else:
            current = floor_lines_by_facade.get(facade) or []
            floor_lines_by_facade[facade] = sorted(set(current + line_zs))

    orientation_by_facade: dict[str, str] = {}
    floor_lines: dict[str, FloorLineScore] = {}
    for facade in FACADE_NAMES:
        gt_windows = gt_by_facade[facade]
        gt_counts: dict[str, int] = {}
        for win in gt_windows:
            gt_counts[win.floor] = gt_counts.get(win.floor, 0) + 1
        line_reason = (
            "missing_elevation_view"
            if facade not in seen_facades
            else "no_product_floor_line_source"
        )
        floor_lines[facade] = _score_floor_lines(
            facade,
            floor_lines_by_facade.get(facade),
            gt_floor_lines,
            tol=floor_line_tol_m,
            no_data_reason=line_reason,
        )
        if facade not in seen_facades:
            orientation_by_facade[facade] = "no_data"
            _populate_facade_scores(
                scores,
                facade=facade,
                orientation="no_data",
                matches=[],
                extras=[],
                gt_by_floor_count=gt_counts,
                read_by_floor_count={},
                no_data=True,
            )
            evidence.append({"type": "missing_elevation_view", "facade": facade})
            continue
        chosen = _choose_reading_orientation(
            facade=facade,
            reads=reads_by_facade[facade],
            truth=gt_windows,
            gt=gt,
            bands=bands,
            along_tol=elevation_along_tol_m,
            sill_tol=sill_tol_m,
            head_tol=head_tol_m,
            width_tol=width_tol_m,
            overlap_accept=overlap_accept,
            overlap_complete=overlap_complete,
        )
        orientation_by_facade[facade] = chosen.orientation
        read_counts: dict[str, int] = {}
        for match in chosen.matches:
            if match.read is not None:
                read_counts[match.floor] = read_counts.get(match.floor, 0) + 1
        for extra in chosen.extras:
            read_counts[extra.floor] = read_counts.get(extra.floor, 0) + 1
        _populate_facade_scores(
            scores,
            facade=facade,
            orientation=chosen.orientation,
            matches=chosen.matches,
            extras=chosen.extras,
            gt_by_floor_count=gt_counts,
            read_by_floor_count=read_counts,
        )
    return ElevationScoreResult(
        scores=scores,
        evidence=evidence,
        orientation_by_facade=orientation_by_facade,
        floor_lines=floor_lines,
    )


def score_reading_elevation_dir(
    reading_dir: Path | str,
    case: str,
    *,
    gt_dir: Path | str | None = None,
    elevation_along_tol_m: float = DEFAULT_ELEVATION_ALONG_TOL_M,
    sill_tol_m: float = DEFAULT_SILL_TOL_M,
    head_tol_m: float = DEFAULT_HEAD_TOL_M,
    width_tol_m: float = DEFAULT_WIDTH_TOL_M,
    overlap_accept: float = DEFAULT_OVERLAP_ACCEPT,
    overlap_complete: float = DEFAULT_OVERLAP_COMPLETE,
    floor_line_tol_m: float = DEFAULT_FLOOR_LINE_TOL_M,
) -> ElevationScoreResult:
    gt = load_gt(case, gt_dir=gt_dir) if gt_dir else load_gt(case)
    if gt is None:
        raise FileNotFoundError(f"no gt for case {case!r}")
    output = {
        p.stem: json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(Path(reading_dir).glob("*_view.json"))
    }
    return score_reading_elevation_views(
        output,
        gt,
        elevation_along_tol_m=elevation_along_tol_m,
        sill_tol_m=sill_tol_m,
        head_tol_m=head_tol_m,
        width_tol_m=width_tol_m,
        overlap_accept=overlap_accept,
        overlap_complete=overlap_complete,
        floor_line_tol_m=floor_line_tol_m,
    )


def score_correction_elevation_windows(
    geom,
    gt: dict,
    *,
    floor_map: dict[str, str],
    evidence: list[dict],
    elevation_along_tol_m: float = DEFAULT_ELEVATION_ALONG_TOL_M,
    sill_tol_m: float = DEFAULT_SILL_TOL_M,
    head_tol_m: float = DEFAULT_HEAD_TOL_M,
    width_tol_m: float = DEFAULT_WIDTH_TOL_M,
    overlap_accept: float = DEFAULT_OVERLAP_ACCEPT,
    overlap_complete: float = DEFAULT_OVERLAP_COMPLETE,
    floor_line_tol_m: float = DEFAULT_FLOOR_LINE_TOL_M,
) -> ElevationScoreResult:
    local_evidence: list[dict] = []
    bands = floor_bands(gt)
    gt_by_facade = derive_gt_elevation_windows(gt, evidence=local_evidence)
    gt_floor_lines = derive_gt_floor_lines(gt)
    product_floor_lines = extract_correction_floor_line_zs(geom)
    scores = _blank_scores(gt)
    reads_by_facade: dict[str, list[ReadElevationWindow]] = {facade: [] for facade in FACADE_NAMES}

    for win in getattr(geom, "windows", []):
        facade = _facade_name(getattr(win, "facade", None))
        mapped_floor = floor_map.get(getattr(win, "floor", ""))
        span = _numeric_pair(getattr(win, "span", None))
        z = _numeric_pair(getattr(win, "z", None))
        if facade is None or mapped_floor is None or span is None or z is None:
            local_evidence.append(
                {
                    "type": "unusable_correction_elevation_window",
                    "window_id": getattr(win, "id", None),
                    "facade": getattr(win, "facade", None),
                    "floor": getattr(win, "floor", None),
                    "reason": "missing mapped floor, facade, numeric span, or numeric z",
                }
            )
            continue
        reads_by_facade[facade].append(
            ReadElevationWindow(
                id=str(getattr(win, "id", "")),
                facade=facade,
                floor=mapped_floor,
                span=_round_pair(span),
                z=_round_pair(z),
                source_view=None,
            )
        )

    orientation_by_facade: dict[str, str] = {}
    for facade in FACADE_NAMES:
        gt_windows = gt_by_facade[facade]
        gt_counts: dict[str, int] = {}
        for gt_win in gt_windows:
            gt_counts[gt_win.floor] = gt_counts.get(gt_win.floor, 0) + 1
        span_limit = facade_span_limit(gt, facade)
        aligned = _match_facade(
            facade=facade,
            reads=reads_by_facade[facade],
            truth=gt_windows,
            bands=bands,
            span_limit=span_limit,
            orientation="aligned",
            along_tol=elevation_along_tol_m,
            sill_tol=sill_tol_m,
            head_tol=head_tol_m,
            width_tol=width_tol_m,
            overlap_accept=overlap_accept,
            overlap_complete=overlap_complete,
            require_same_read_floor=True,
        )
        flipped = _match_facade(
            facade=facade,
            reads=reads_by_facade[facade],
            truth=gt_windows,
            bands=bands,
            span_limit=span_limit,
            orientation="flipped",
            along_tol=elevation_along_tol_m,
            sill_tol=sill_tol_m,
            head_tol=head_tol_m,
            width_tol=width_tol_m,
            overlap_accept=overlap_accept,
            overlap_complete=overlap_complete,
            require_same_read_floor=True,
        )
        if (flipped.complete, flipped.within_tol, flipped.matched, -flipped.cost) > (
            aligned.complete,
            aligned.within_tol,
            aligned.matched,
            -aligned.cost,
        ):
            local_evidence.append(
                {
                    "type": "correction_mirrored_model_candidate",
                    "facade": facade,
                    "aligned": {
                        "complete_total": aligned.complete,
                        "within_tol_total": aligned.within_tol,
                        "matched_total": aligned.matched,
                        "cost": round(aligned.cost, 3),
                    },
                    "flipped": {
                        "complete_total": flipped.complete,
                        "within_tol_total": flipped.within_tol,
                        "matched_total": flipped.matched,
                        "cost": round(flipped.cost, 3),
                    },
                    "reason": "correction spans are already world-frame; flipped is reported but not excused",
                }
            )
        orientation_by_facade[facade] = "aligned"
        read_counts: dict[str, int] = {}
        for read in reads_by_facade[facade]:
            if read.floor is not None:
                read_counts[read.floor] = read_counts.get(read.floor, 0) + 1
        _populate_facade_scores(
            scores,
            facade=facade,
            orientation="aligned",
            matches=aligned.matches,
            extras=aligned.extras,
            gt_by_floor_count=gt_counts,
            read_by_floor_count=read_counts,
        )
    evidence.extend(local_evidence)
    return ElevationScoreResult(
        scores=scores,
        evidence=local_evidence,
        orientation_by_facade=orientation_by_facade,
        floor_lines={
            facade: _score_floor_lines(facade, product_floor_lines, gt_floor_lines, tol=floor_line_tol_m)
            for facade in FACADE_NAMES
        },
    )
