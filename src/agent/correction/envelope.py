"""Facade-derived authoritative envelope extraction.

The deterministic core stays image-blind: this module consumes already-traced
0_reading vector JSON and resolves a small structured envelope object that the
core may use later. It is shared by the pipeline and correction checks so the
facade-priority rules have one implementation.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from src.agent.correction.config import CoreTolerances, load_core_tolerances
from src.agent.correction.schema import CorrectedGeometry, CorrectedGeometryV3
from src.agent.correction.facade import derive_view_projection_frame
from src.agent.correction.footprint import floor_footprint
from src.agent.reading.constants import DIMCHAIN_CLOSE_TOL_M
from src.agent.reading import ReadingView, load_reading_view

EnvelopeAxis = Literal["x", "y"]

_OVERALL_RE = re.compile(r"(overall|total|总)", re.IGNORECASE)
_NUM_RE = re.compile(r"[-+]?\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?")
_FACADE_RE = re.compile(r"\b(north|south|east|west)\b", re.IGNORECASE)


@dataclass(frozen=True)
class EnvelopeCandidate:
    axis: EnvelopeAxis
    bounds: tuple[float, float] | None
    span: float
    source_kind: str
    view: str
    source_id: str
    role: str | None = None
    note: str | None = None
    confidence: float = 0.0

    @property
    def has_overall_authority(self) -> bool:
        role = (self.role or "").strip().lower()
        note = self.note or ""
        return role == "overall" or bool(_OVERALL_RE.search(note))

    @property
    def candidate_class(self) -> str:
        if self.has_overall_authority:
            return "overall_dimension"
        if self.source_kind in {"outline", "wall_fill"}:
            return self.source_kind
        if self.bounds is None:
            return "text_only_fallback"
        return "untagged_dimension"

    def to_dict(self) -> dict:
        return {
            "axis": self.axis,
            "bounds": list(self.bounds) if self.bounds is not None else None,
            "span": round(self.span, 6),
            "source_kind": self.source_kind,
            "view": self.view,
            "source_id": self.source_id,
            "role": self.role,
            "note": self.note,
            "confidence": self.confidence,
            "candidate_class": self.candidate_class,
        }


@dataclass(frozen=True)
class EnvelopeAxisResolution:
    axis: EnvelopeAxis
    status: Literal["accepted", "skipped", "conflict"]
    bounds: tuple[float, float] | None = None
    span: float | None = None
    source: EnvelopeCandidate | None = None
    corroborating_sources: tuple[EnvelopeCandidate, ...] = ()
    candidates: tuple[EnvelopeCandidate, ...] = ()
    reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "axis": self.axis,
            "status": self.status,
            "bounds": list(self.bounds) if self.bounds is not None else None,
            "span": round(self.span, 6) if self.span is not None else None,
            "source": self.source.to_dict() if self.source else None,
            "corroborating_sources": [c.to_dict() for c in self.corroborating_sources],
            "candidates": [c.to_dict() for c in self.candidates],
            "reason": self.reason,
        }


@dataclass(frozen=True)
class WingBoundaryEvidence:
    facade: Literal["North", "South", "East", "West"]
    world_axis: EnvelopeAxis
    world_value: float
    view: str
    source_id: str
    chain_id: str
    order: int
    boundary_ref: str
    boundary_endpoint: Literal["from", "to"]
    frame_transform_hash: str


@dataclass(frozen=True)
class EnvelopeEndpointResolution:
    status: Literal["accepted", "skipped", "conflict"]
    evidence: WingBoundaryEvidence | None
    corroborating_sources: tuple[str, ...] = ()
    candidates: tuple[WingBoundaryEvidence, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class AuthoritativeEnvelope:
    axes: dict[EnvelopeAxis, EnvelopeAxisResolution] = field(default_factory=dict)
    endpoint_resolutions: tuple[EnvelopeEndpointResolution, ...] = ()

    def axis(self, axis: EnvelopeAxis) -> EnvelopeAxisResolution | None:
        return self.axes.get(axis)

    def to_dict(self) -> dict:
        out = {axis: resolution.to_dict() for axis, resolution in self.axes.items()}
        if self.endpoint_resolutions:
            out["endpoint_resolutions"] = [
                {
                    "status": r.status,
                    "evidence": None if r.evidence is None else r.evidence.__dict__,
                    "corroborating_sources": list(r.corroborating_sources),
                    "candidates": [c.__dict__ for c in r.candidates],
                    "reason": r.reason,
                }
                for r in self.endpoint_resolutions
            ]
        return out


def _axis_for_facade(facade: str | None) -> EnvelopeAxis | None:
    if facade in {"North", "South"}:
        return "x"
    if facade in {"East", "West"}:
        return "y"
    return None


def _view_facade(view: ReadingView) -> str | None:
    facade = None
    if view.facade is not None:
        facade = view.facade.view_facade
    if facade is None:
        facade = getattr(view, "view_facade", None)
    if facade is None and view.model_extra:
        facade = view.model_extra.get("view_facade")
    if facade is None:
        for text in (view.image_label, view.facade_axis_note or ""):
            m = _FACADE_RE.search(text or "")
            if m:
                facade = m.group(1)
                break
    if isinstance(facade, str):
        f = facade.strip().capitalize()
        if f in {"North", "South", "East", "West"}:
            return f
    return None


def dimension_chain_is_closed_for_endpoint(
    view: ReadingView, *, chain_id: str, axis: Literal["x", "y"], close_tol_m: float,
) -> bool:
    """The endpoint path consumes the same explicit closure rule as reading.

    It intentionally does not import the validator's private implementation:
    this is a small, pure re-expression over the public reading wire.
    """
    if close_tol_m <= 0:
        return False
    dims = [d for d in view.dimensions if d.chain_id == chain_id and (d.axis or "").lower() == axis]
    overall = [d for d in dims if getattr(d.role, "value", d.role) in {"overall", "baseline"}]
    segments = [d for d in dims if getattr(d.role, "value", d.role) == "segment"]
    if not overall or not segments or any(d.order is None for d in segments):
        return False
    try:
        values = [float(d.value_m) for d in segments]
        target = float(overall[0].value_m)
    except (TypeError, ValueError):
        return False
    return all(math.isfinite(v) for v in [*values, target]) and abs(sum(values) - target) <= close_tol_m


def _projection_extent_vertices(footprint: CorrectedGeometryV3) -> list[tuple[float, float]]:
    # All floors must be topology-identical before a transaction; endpoint
    # evidence only needs the current authoritative extent for its projection.
    return [tuple(map(float, p)) for p in floor_footprint(footprint, footprint.floors[0])]


def extract_wing_boundary_evidence_from_view(
    view: ReadingView, *, view_name: str | None, footprint: CorrectedGeometryV3,
) -> list[WingBoundaryEvidence]:
    facade = _view_facade(view)
    if facade is None or str(view.image_kind).lower() != "elevation":
        return []
    orientation = view.facade
    mirrored = getattr(orientation, "mirrored", "unknown") if orientation else "unknown"
    if not isinstance(mirrored, bool):
        if str(mirrored).lower() not in {"true", "false"}:
            return []
        mirrored = str(mirrored).lower() == "true"
    # ReadingViews v2 fixes every elevation's local x to image-left → right.
    # `local_x_positive` is an optional, non-load-bearing audit field and must
    # not affect correction projection when it is absent or stale.
    local_x_positive = "image_left_to_right"
    try:
        frame = derive_view_projection_frame(
            vertices=_projection_extent_vertices(footprint), facade_family=facade,
            mirrored=mirrored, local_x_positive=local_x_positive,
        )
    except ValueError:
        return []
    projection_vertices = _projection_extent_vertices(footprint)
    projection_extent = {
        "x": [min(p[0] for p in projection_vertices), max(p[0] for p in projection_vertices)],
        "y": [min(p[1] for p in projection_vertices), max(p[1] for p in projection_vertices)],
    }
    frame_hash = hashlib.sha256(json.dumps({
        "facade": frame.facade_family, "axis": frame.world_axis, "sign": frame.sign,
        "along_origin": frame.along_origin, "mirrored": frame.mirrored,
        "local_x_positive": frame.local_x_positive,
        "projection_extent": projection_extent,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    out: list[WingBoundaryEvidence] = []
    for dim in view.dimensions:
        extra = getattr(dim, "model_extra", None) or {}
        if (getattr(dim.role, "value", dim.role) != "segment" or not dim.chain_id
                or not isinstance(dim.order, int) or not isinstance(extra.get("boundary_ref"), str)
                or not extra["boundary_ref"] or extra.get("boundary_kind") != "wing_break"
                or extra.get("boundary_endpoint") not in {"from", "to"}):
            continue
        endpoint = extra["boundary_endpoint"]
        point = dim.from_pt if endpoint == "from" else dim.to
        if not point or len(point) < 2 or dim.value_m is None:
            continue
        try:
            local_x = float(point[0])
            value = float(dim.value_m)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(local_x) or not math.isfinite(value):
            continue
        if not dimension_chain_is_closed_for_endpoint(
            view, chain_id=dim.chain_id, axis="x", close_tol_m=DIMCHAIN_CLOSE_TOL_M,
        ):
            continue
        out.append(WingBoundaryEvidence(
            facade=facade, world_axis=frame.world_axis,
            world_value=round(frame.to_world_along(local_x), 6),
            view=view_name or facade, source_id=dim.id, chain_id=dim.chain_id,
            order=dim.order, boundary_ref=extra["boundary_ref"],
            boundary_endpoint=endpoint, frame_transform_hash=frame_hash,
        ))
    return out


def _bounds_from_points(a: list[float] | None, b: list[float] | None, idx: int) -> tuple[float, float] | None:
    if not a or not b or len(a) <= idx or len(b) <= idx:
        return None
    lo, hi = sorted((float(a[idx]), float(b[idx])))
    if hi - lo <= 0:
        return None
    return (round(lo, 6), round(hi, 6))


def _parse_text_m(text: str | None) -> float | None:
    if not text:
        return None
    m = _NUM_RE.search(text)
    if not m:
        return None
    raw = float(m.group(0).replace(",", ""))
    lower = text.lower()
    if "mm" in lower or raw >= 100.0:
        raw /= 1000.0
    return round(raw, 6) if raw > 0 else None


def _candidate_rank(c: EnvelopeCandidate) -> tuple[int, float, float]:
    if c.has_overall_authority:
        rank = 4
    elif c.source_kind in {"outline", "wall_fill"}:
        rank = 3
    elif c.bounds is not None:
        rank = 2
    else:
        rank = 1
    return (rank, c.confidence, c.span)


def _within(a: float, b: float, *, tol_m: float) -> bool:
    return abs(a - b) <= tol_m


def _bounds_agree(a: tuple[float, float] | None, b: tuple[float, float] | None, *, tol_m: float) -> bool:
    if a is None or b is None:
        return False
    return _within(a[0], b[0], tol_m=tol_m) and _within(a[1], b[1], tol_m=tol_m)


def extract_envelope_candidates_from_view(
    view: ReadingView, *, view_name: str | None = None, tol: CoreTolerances | None = None,
) -> list[EnvelopeCandidate]:
    tol = tol or load_core_tolerances()
    facade = _view_facade(view)
    axis = _axis_for_facade(facade)
    if axis is None or str(view.image_kind).lower() != "elevation":
        return []
    view_id = facade or view_name or view.image_label or "unknown"
    candidates: list[EnvelopeCandidate] = []

    for dim in view.dimensions:
        dim_axis = (dim.axis or "").strip().lower()
        if dim_axis and dim_axis not in {"x", "horizontal"}:
            continue
        bounds = _bounds_from_points(dim.from_pt, dim.to, 0)
        note = dim.note
        role = dim.role.value if hasattr(dim.role, "value") else dim.role
        if bounds is not None:
            span = round(bounds[1] - bounds[0], 6)
            confidence = 0.95 if (role == "overall" or _OVERALL_RE.search(note or "")) else 0.70
            if dim.value_m is not None and abs(float(dim.value_m) - span) > tol.envelope_candidate_agreement_tol_m:
                note = f"{note or ''}; ignored inconsistent value_m={dim.value_m}".strip("; ")
            candidates.append(
                EnvelopeCandidate(
                    axis=axis,
                    bounds=bounds,
                    span=span,
                    source_kind="dimension",
                    view=view_id,
                    source_id=dim.id,
                    role=role,
                    note=note,
                    confidence=confidence,
                )
            )
            continue
        span = _parse_text_m(dim.text_verbatim or dim.text)
        if span is None:
            continue
        confidence = 0.55 if (role == "overall" or _OVERALL_RE.search(note or "")) else 0.35
        candidates.append(
            EnvelopeCandidate(
                axis=axis,
                bounds=None,
                span=span,
                source_kind="dimension_text",
                view=view_id,
                source_id=dim.id,
                role=role,
                note=note,
                confidence=confidence,
            )
        )

    for stroke in view.strokes:
        pen = (stroke.pen or "").strip().lower()
        if pen not in {"outline", "wall_fill"}:
            continue
        geom = stroke.geometry or {}
        bounds_raw = geom.get("x_range_m")
        if not isinstance(bounds_raw, list) or len(bounds_raw) != 2:
            continue
        lo, hi = sorted((float(bounds_raw[0]), float(bounds_raw[1])))
        if hi - lo <= 0:
            continue
        candidates.append(
            EnvelopeCandidate(
                axis=axis,
                bounds=(round(lo, 6), round(hi, 6)),
                span=round(hi - lo, 6),
                source_kind=pen,
                view=view_id,
                source_id=stroke.id,
                role=None,
                note=stroke.note,
                confidence=0.85 if pen == "outline" else 0.80,
            )
        )
    return candidates


def extract_envelope_candidates_from_dir(
    vector_dir: Path | str, *, tol: CoreTolerances | None = None,
) -> list[EnvelopeCandidate]:
    tol = tol or load_core_tolerances()
    out: list[EnvelopeCandidate] = []
    for path in sorted(Path(vector_dir).glob("*_view.json")):
        view = load_reading_view(path)
        out.extend(extract_envelope_candidates_from_view(view, view_name=path.stem, tol=tol))
    return out


def _footprint_bounds(geom: CorrectedGeometry | dict[EnvelopeAxis, tuple[float, float]] | None) -> dict[EnvelopeAxis, tuple[float, float]]:
    if geom is None:
        return {}
    if isinstance(geom, CorrectedGeometry):
        from src.agent.correction.footprint import footprint_bbox
        x, y = footprint_bbox(geom)
        return {
            "x": x,
            "y": y,
        }
    return geom


def _passes_footprint_gate(
    candidate: EnvelopeCandidate,
    footprint: dict[EnvelopeAxis, tuple[float, float]],
    tolerance_m: float,
) -> bool:
    bounds = footprint.get(candidate.axis)
    if bounds is None:
        return True
    old_span = abs(bounds[1] - bounds[0])
    return abs(candidate.span - old_span) <= tolerance_m + 1e-9


def _near_matches(candidates: list[EnvelopeCandidate], candidate: EnvelopeCandidate, *, tol_m: float) -> list[EnvelopeCandidate]:
    return [
        c for c in candidates
        if c is not candidate and _within(c.span, candidate.span, tol_m=tol_m) and (
            candidate.bounds is None or c.bounds is None or _bounds_agree(candidate.bounds, c.bounds, tol_m=tol_m)
        )
    ]


def resolve_authoritative_envelope(
    candidates: list[EnvelopeCandidate],
    *,
    footprint: CorrectedGeometry | dict[EnvelopeAxis, tuple[float, float]] | None = None,
    footprint_tolerance_m: float = 0.30,
    tol: CoreTolerances | None = None,
) -> AuthoritativeEnvelope:
    tol = tol or load_core_tolerances()
    fp = _footprint_bounds(footprint)
    axes: dict[EnvelopeAxis, EnvelopeAxisResolution] = {}
    for axis in ("x", "y"):
        axis_candidates = [c for c in candidates if c.axis == axis and c.span > 0]
        if not axis_candidates:
            continue
        axis_candidates.sort(key=_candidate_rank, reverse=True)
        by_view: dict[str, list[EnvelopeCandidate]] = {}
        for c in axis_candidates:
            by_view.setdefault(c.view, []).append(c)
        best_by_view = {view: sorted(values, key=_candidate_rank, reverse=True)[0] for view, values in by_view.items()}

        authoritative_by_view = [
            c for c in best_by_view.values()
            if c.has_overall_authority or c.source_kind in {"outline", "wall_fill"}
        ]
        if len(authoritative_by_view) >= 2:
            first = authoritative_by_view[0]
            disagree = [
                c for c in authoritative_by_view[1:]
                if not _within(c.span, first.span, tol_m=tol.envelope_candidate_agreement_tol_m) or (first.bounds and c.bounds and not _bounds_agree(first.bounds, c.bounds, tol_m=tol.envelope_candidate_agreement_tol_m))
            ]
            if disagree:
                axes[axis] = EnvelopeAxisResolution(
                    axis=axis,
                    status="conflict",
                    candidates=tuple(axis_candidates),
                    reason="cross-facade authoritative envelope candidates disagree",
                )
                continue

        selected = axis_candidates[0]
        corroborating = _near_matches(axis_candidates, selected, tol_m=tol.envelope_candidate_agreement_tol_m)
        distinct_views = {c.view for c in [selected, *corroborating]}
        has_opposite_view_agreement = len(distinct_views) >= 2
        has_same_view_stroke_agreement = any(
            c.view == selected.view and c.source_kind in {"outline", "wall_fill"}
            for c in corroborating
        )
        has_overall_authority = selected.has_overall_authority
        single_view = len(by_view) == 1
        passes_gate = _passes_footprint_gate(selected, fp, footprint_tolerance_m)

        if not passes_gate:
            axes[axis] = EnvelopeAxisResolution(
                axis=axis,
                status="skipped",
                candidates=tuple(axis_candidates),
                reason=(
                    f"candidate span {selected.span:.3f} exceeds footprint tolerance "
                    f"{footprint_tolerance_m:.3f} m"
                ),
            )
            continue
        if selected.bounds is None:
            axes[axis] = EnvelopeAxisResolution(
                axis=axis,
                status="skipped",
                candidates=tuple(axis_candidates),
                reason="origin ambiguity: candidate has span but no authoritative bounds",
            )
            continue
        if single_view:
            if not has_overall_authority and not has_same_view_stroke_agreement:
                axes[axis] = EnvelopeAxisResolution(
                    axis=axis,
                    status="skipped",
                    candidates=tuple(axis_candidates),
                    reason="insufficient evidence: single facade lacks overall authority or same-view stroke agreement",
                )
                continue
        elif not (has_overall_authority or has_opposite_view_agreement or has_same_view_stroke_agreement):
            axes[axis] = EnvelopeAxisResolution(
                axis=axis,
                status="skipped",
                candidates=tuple(axis_candidates),
                reason="insufficient evidence: no corroborating facade envelope signal",
            )
            continue

        axes[axis] = EnvelopeAxisResolution(
            axis=axis,
            status="accepted",
            bounds=selected.bounds,
            span=selected.span,
            source=selected,
            corroborating_sources=tuple(corroborating),
            candidates=tuple(axis_candidates),
            reason="accepted facade authoritative envelope",
        )
    return AuthoritativeEnvelope(axes=axes)


def extract_authoritative_envelope(
    vector_dir: Path | str,
    *,
    footprint: CorrectedGeometry | dict[EnvelopeAxis, tuple[float, float]] | None = None,
    footprint_tolerance_m: float = 0.30,
    tol: CoreTolerances | None = None,
) -> AuthoritativeEnvelope:
    tol = tol or load_core_tolerances()
    candidates = extract_envelope_candidates_from_dir(vector_dir, tol=tol)
    overall = resolve_authoritative_envelope(
        candidates,
        footprint=footprint,
        footprint_tolerance_m=footprint_tolerance_m,
        tol=tol,
    )
    if not isinstance(footprint, CorrectedGeometryV3):
        return overall
    # Endpoint projection may use this call's already-accepted overall bounds,
    # but only in-memory and only for the frame extent; B2b still owns the
    # later atomic geometry mutation.
    projection_footprint = footprint.model_copy(deep=True)
    old_bbox = _footprint_bounds(footprint)
    for axis in ("x", "y"):
        resolution = overall.axis(axis)
        if resolution is None or resolution.status != "accepted" or resolution.bounds is None:
            continue
        old_lo, old_hi = old_bbox[axis]
        new_lo, new_hi = resolution.bounds
        index = 0 if axis == "x" else 1
        for floor in projection_footprint.floors:
            changed = []
            for point in floor.footprint.vertices:
                values = list(point)
                if values[index] == old_lo:
                    values[index] = new_lo
                elif values[index] == old_hi:
                    values[index] = new_hi
                changed.append(tuple(values))
            floor.footprint.vertices = changed
    evidences: list[WingBoundaryEvidence] = []
    for path in sorted(Path(vector_dir).glob("*_view.json")):
        evidences.extend(extract_wing_boundary_evidence_from_view(
            load_reading_view(path), view_name=path.stem, footprint=projection_footprint,
        ))
    by_ref: dict[str, list[WingBoundaryEvidence]] = {}
    for evidence in evidences:
        by_ref.setdefault(evidence.boundary_ref, []).append(evidence)
    resolutions: list[EnvelopeEndpointResolution] = []
    for ref, values in sorted(by_ref.items()):
        values.sort(key=lambda v: (v.world_value, v.source_id))
        first = values[0]
        if any(abs(v.world_value - first.world_value) > tol.envelope_endpoint_match_tol_m for v in values[1:]):
            resolutions.append(EnvelopeEndpointResolution("conflict", None, candidates=tuple(values), reason=f"wing boundary {ref!r} candidates disagree"))
        else:
            resolutions.append(EnvelopeEndpointResolution("accepted", first, tuple(v.source_id for v in values[1:]), tuple(values), "accepted wing_break endpoint"))
    return AuthoritativeEnvelope(axes=overall.axes, endpoint_resolutions=tuple(resolutions))


def envelope_candidates_from_elevation_widths(elevation_widths: dict[str, float]) -> list[EnvelopeCandidate]:
    candidates: list[EnvelopeCandidate] = []
    for facade_raw, width in elevation_widths.items():
        facade = facade_raw.strip().capitalize()
        axis = _axis_for_facade(facade)
        if axis is None:
            continue
        span = float(width)
        if span <= 0:
            continue
        candidates.append(
            EnvelopeCandidate(
                axis=axis,
                bounds=(0.0, round(span, 6)),
                span=round(span, 6),
                source_kind="elevation_width",
                view=facade,
                source_id=facade,
                role=None,
                note="validator elevation_widths input",
                confidence=0.60,
            )
        )
    return candidates
