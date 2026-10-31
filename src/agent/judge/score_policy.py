"""Map gt scorer results into machine-readable judge evidence.

These suggestions are evidence for gate②, not an automatic verdict.  They are
kept out of ``StageVerdict`` on purpose: the judge still owns the authoritative
checklist decision.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Iterable, Literal

from .reading_score import (
    DEFAULT_WALL_TOL_M,
    DEFAULT_WIN_CENTRE_TOL_M,
    FloorScore,
)
from .elevation_score import (
    DEFAULT_ELEVATION_ALONG_TOL_M,
    DEFAULT_OVERLAP_ACCEPT,
    DEFAULT_OVERLAP_COMPLETE,
    DEFAULT_HEAD_TOL_M,
    DEFAULT_SILL_TOL_M,
    DEFAULT_WIDTH_TOL_M,
    ElevationScoreResult,
)
from .score_schema import NonNegativeFloat, NonNegativeInt, StrictWire, StrictBool, StableId

EXTRA_MINOR_MAX = 2
WINDOW_MINOR_RATIO = 0.80


class V3Criterion(StrictWire):
    """Denominator-aware machine criterion for the typed C2 score path."""

    criterion_id: StableId
    eligible: StrictBool
    denominator_units: NonNegativeFloat
    passing_units: NonNegativeFloat
    failing_units: NonNegativeFloat
    na_reasons: dict[StableId, NonNegativeInt]
    verdict: Literal["pass", "fail", "not_applicable", "insufficient_evidence"]


class V3PolicyVerdict(StrictWire):
    """Typed policy result; callers keep REJECTED outside StageVerdict."""

    verdict: Literal["pass", "fail", "not_applicable", "rejected"]
    criteria: tuple[V3Criterion, ...]
    rejection_code: str | None = None


def _criterion_from_rows(*, criterion_id: str, rows: Iterable[object], max_failing_units: float = 0.0) -> V3Criterion:
    """Construct a conserving criterion from ClaimScoreRowV8-like inputs."""
    rows = tuple(rows)
    def amounts(row) -> tuple[float, float, float]:
        pieces = getattr(row, "outcome_slices", ())
        if pieces:
            denominator = float(getattr(row, "eligible_units", sum(piece.units for piece in pieces)))
            return denominator, sum(piece.units for piece in pieces if piece.result in {"complete", "within_tolerance"}), sum(piece.units for piece in pieces if piece.result in {"miss", "conflict"})
        denominator = float(getattr(row, "eligible_units", 1.0))
        result = getattr(row, "result", getattr(row, "status", "not_applicable"))
        if result == "not_applicable":
            return 0.0, 0.0, 0.0
        return denominator, (denominator if result in {"complete", "within_tolerance"} else 0.0), (denominator if result in {"miss", "conflict", "extra"} else 0.0)
    values = tuple(amounts(row) for row in rows)
    denominator = sum(item[0] for item in values)
    passing = sum(item[1] for item in values)
    failing = sum(item[2] for item in values)
    reasons: dict[str, int] = {}
    for row in rows:
        reason = getattr(row, "na_reason", None)
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
    if abs((passing + failing) - denominator) > 1e-9:
        raise ValueError("score_denominator_nonconserving")
    if denominator == 0:
        return V3Criterion(criterion_id=criterion_id, eligible=False, denominator_units=0.0,
                           passing_units=0.0, failing_units=0.0, na_reasons=reasons,
                           verdict="not_applicable")
    return V3Criterion(criterion_id=criterion_id, eligible=True, denominator_units=denominator,
                       passing_units=passing, failing_units=failing, na_reasons=reasons,
                       verdict="fail" if failing > max_failing_units else "pass")


def _segment_criterion(*, criterion_id: str, rows: Iterable[object], passing: frozenset[str],
                       failing: frozenset[str], max_failing_units: float = 0.0) -> V3Criterion:
    """W4: a length-denominator criterion over SegmentScore rows.

    ``eligible_units`` is the length (m) a row contributes (overlap for a match,
    uncovered target for a miss, uncovered observation for an extra, double-
    covered target for a duplicate).  Passing/failing buckets are explicit per
    criterion so the same row can be passing in one account (a duplicate counts
    as target-covered for walls_complete) and failing in another (the same
    duplicate fails no_duplicate_wall_strokes) without charging the same gap
    twice.  Conservation (passing + failing == denominator) is enforced.
    """
    materialized = tuple(rows)
    def units(row) -> float:
        return float(getattr(row, "eligible_units", 1.0))
    def status_of(row) -> str:
        return getattr(row, "status", getattr(row, "result", "not_applicable"))
    denominator = sum(units(row) for row in materialized)
    passing_units = sum(units(row) for row in materialized if status_of(row) in passing)
    failing_units = sum(units(row) for row in materialized if status_of(row) in failing)
    if abs((passing_units + failing_units) - denominator) > 1e-9:
        raise ValueError("score_denominator_nonconserving")
    if denominator == 0:
        return V3Criterion(criterion_id=criterion_id, eligible=False, denominator_units=0.0,
                           passing_units=0.0, failing_units=0.0, na_reasons={}, verdict="not_applicable")
    return V3Criterion(criterion_id=criterion_id, eligible=True, denominator_units=denominator,
                       passing_units=passing_units, failing_units=failing_units, na_reasons={},
                       verdict="fail" if failing_units > max_failing_units else "pass")


def c2_v3_score_policy(*, claim_rows: Iterable[object],
                       segment_rows: Iterable[object] = (), floor_line_rows: Iterable[object] = (),
                       opening_source_rows: Iterable[object] = (),
                       identity_valid: bool = True, totality_valid: bool = True,
                       max_failing_units: float = 0.0) -> V3PolicyVerdict:
    """Apply §9.2 without changing the legacy advisory policy.

    Identity and conservation are terminal machine rejections.  They do not
    become a misleading StageVerdict aggregation.  The generic row shape keeps
    this helper judge-only and lets Phase D assemble its sidecar independently.
    """
    if not identity_valid:
        return V3PolicyVerdict(verdict="rejected", criteria=(), rejection_code="score_gt_identity_invalid")
    if not totality_valid:
        return V3PolicyVerdict(verdict="rejected", criteria=(), rejection_code="score_denominator_nonconserving")
    claims = tuple(claim_rows)
    segments = tuple(segment_rows)
    source_rows = tuple(opening_source_rows)
    def boundary_row(row: object) -> bool:
        """SegmentScore/SegmentScoreRow adapters expose topology on target/obs."""
        target_exterior = getattr(row, "target_exterior", None)
        if target_exterior is not None:
            return bool(target_exterior)
        segment = getattr(row, "target", None) or getattr(row, "observation", None)
        return bool(getattr(segment, "exterior", False))
    # PlanSegment.exterior is the Phase-B topology discriminator: exterior
    # boundary and interior wall rows must not silently share a denominator.
    wall_rows = tuple(row for row in segments if not boundary_row(row))
    boundary_rows = tuple(row for row in segments if boundary_row(row))
    by_claim = {name: tuple(row for row in claims if row.claim == name)
                for name in ("existence", "host", "along", "width", "sill", "head", "appearance")}
    # W4 (R-3): walls split into three accounts -- miss / extra / duplicate.
    # A duplicate counts as target-covered (passing) for walls_complete and as a
    # failure only for no_duplicate_wall_strokes, so the same covered length is
    # never charged twice.  walls_complete's denominator is answer length only
    # (target rows): product over-draw feeds no_extra_walls, never walls, so a
    # product that draws an extra wall cannot inflate its own pass denominator.
    wall_target_rows = tuple(
        row
        for row in wall_rows
        if (
            getattr(row, "target", None) is not None
            or getattr(row, "target_id", None) is not None
        )
    )
    wall_extra_rows = tuple(row for row in wall_rows
                            if getattr(row, "target", None) is None
                            and getattr(row, "target_id", None) is None
                            and (
                                getattr(row, "observation", None) is not None
                                or getattr(row, "observation_id", None) is not None
                            ))
    wall_duplicate_rows = tuple(row for row in wall_rows if getattr(row, "status", None) == "duplicate")
    if source_rows:
        plan_geometry_rows = tuple(
            row
            for row in source_rows
            if row.channel == "plan" and row.claim in {"along", "width"}
        )
        elevation_geometry_rows = tuple(
            row
            for row in source_rows
            if row.channel == "elevation"
            and row.claim in {"along", "width", "sill", "head"}
        )
    else:
        # Preserve correction-v3's public criterion bytes exactly.
        plan_geometry_rows = by_claim["along"] + by_claim["width"]
        elevation_geometry_rows = by_claim["sill"] + by_claim["head"]
    # The segment/floor-line inputs use the same narrow row protocol.  Empty
    # inputs become explicit NA instead of an invented zero score.
    criteria = (
        _segment_criterion(criterion_id="walls_complete", rows=wall_target_rows,
            passing=frozenset({"complete", "within_tolerance", "duplicate"}), failing=frozenset({"miss"}),
            max_failing_units=max_failing_units),
        _segment_criterion(criterion_id="no_extra_walls", rows=wall_extra_rows,
            passing=frozenset(), failing=frozenset({"extra"}), max_failing_units=max_failing_units),
        _segment_criterion(criterion_id="no_duplicate_wall_strokes", rows=wall_duplicate_rows,
            passing=frozenset(), failing=frozenset({"duplicate"}), max_failing_units=max_failing_units),
        _criterion_from_rows(criterion_id="boundary_complete", rows=boundary_rows, max_failing_units=max_failing_units),
        _criterion_from_rows(criterion_id="windows_placed", rows=by_claim["existence"], max_failing_units=max_failing_units),
        _criterion_from_rows(criterion_id="window_plan_geometry", rows=plan_geometry_rows, max_failing_units=max_failing_units),
        _criterion_from_rows(criterion_id="window_elevation_geometry", rows=elevation_geometry_rows, max_failing_units=max_failing_units),
        _criterion_from_rows(criterion_id="floor_lines_complete", rows=floor_line_rows, max_failing_units=max_failing_units),
        # Phase C has no oversplit/negative-evidence aggregate scorer yet;
        # retain explicit NA rather than fabricate a zero-denominator pass.
        _criterion_from_rows(criterion_id="no_oversplit", rows=(), max_failing_units=max_failing_units),
        _criterion_from_rows(criterion_id="negative_evidence_complete", rows=(), max_failing_units=max_failing_units),
    )
    eligible = tuple(item for item in criteria if item.eligible)
    if not eligible:
        return V3PolicyVerdict(verdict="not_applicable", criteria=criteria)
    return V3PolicyVerdict(verdict="fail" if any(item.verdict == "fail" for item in eligible) else "pass",
                           criteria=criteria)


def reading_score_criteria(
    scores: Mapping[str, FloorScore],
    *,
    wall_tol_m: float = DEFAULT_WALL_TOL_M,
    window_centre_tol_m: float = DEFAULT_WIN_CENTRE_TOL_M,
    elevation=None,
    elevation_along_tol_m: float = DEFAULT_ELEVATION_ALONG_TOL_M,
    sill_tol_m: float = DEFAULT_SILL_TOL_M,
    head_tol_m: float = DEFAULT_HEAD_TOL_M,
    width_tol_m: float = DEFAULT_WIDTH_TOL_M,
    overlap_accept: float = DEFAULT_OVERLAP_ACCEPT,
    overlap_complete: float = DEFAULT_OVERLAP_COMPLETE,
    extra_evidence: list[dict] | None = None,
) -> list[dict]:
    """Return suggested criterion evidence derived from FloorScore objects."""
    total_wall_hits = total_walls = total_windows_hit = total_windows = 0
    total_boundary_hits = total_boundary = 0
    extra_walls = extra_windows = 0
    no_data_boundary_floors = 0
    max_wall_offset = 0.0
    floors: list[dict] = []

    for key, score in scores.items():
        wh, wt = score.wall_hits()
        winh, wint = score.window_hits()
        bh, bt = score.boundary_hits()
        ew = len(score.extra_vwalls) + len(score.extra_hwalls)
        exwin = sum(len(v) for v in score.extra_windows.values())
        wall_status_counts = _status_counts(score.vwalls + score.hwalls + score.extra_vwalls + score.extra_hwalls)
        window_status_counts = _status_counts(
            [m for matches in score.windows.values() for m in matches]
            + [m for matches in score.extra_windows.values() for m in matches]
        )
        total_wall_hits += wh
        total_walls += wt
        total_windows_hit += winh
        total_windows += wint
        total_boundary_hits += bh
        total_boundary += bt
        if score.boundary is None:
            no_data_boundary_floors += 1
        extra_walls += ew
        extra_windows += exwin
        max_wall_offset = max(max_wall_offset, score.max_wall_offset())
        floors.append(
            {
                "key": key,
                "floor": score.floor,
                "wall_hits": wh,
                "wall_total": wt,
                "boundary_hits": bh,
                "boundary_total": bt,
                "boundary_no_data": score.boundary is None,
                "window_hits": winh,
                "window_total": wint,
                "extra_walls": ew,
                "extra_windows": exwin,
                "wall_status_counts": wall_status_counts,
                "window_status_counts": window_status_counts,
                "max_wall_offset_m": score.max_wall_offset(),
            }
        )

    missed_walls = max(0, total_walls - total_wall_hits)
    missed_boundary = max(0, total_boundary - total_boundary_hits)
    missed_windows = max(0, total_windows - total_windows_hit)
    window_ratio = 1.0 if total_windows == 0 else total_windows_hit / total_windows

    # F-1b: "no paper was graded" must never read as pass.  This guards only the
    # mechanical fact that ``scores`` is empty -- e.g. a reading envelope the
    # legacy seam failed to unwrap, or a product whose views map to no gt floor.
    # It does NOT fire when scores is non-empty but a floor legitimately lacks
    # windows: that is the "GT has no windows" case, handled by the
    # ``total_windows == 0 -> pass`` branch below, which stays unchanged.
    no_scored_floors = not scores

    if no_scored_floors:
        wall_status = window_status = boundary_status = oversplit_status = "severe"
    else:
        if missed_walls:
            wall_status = "severe"
        elif extra_walls:
            wall_status = "minor" if extra_walls <= EXTRA_MINOR_MAX else "severe"
        else:
            wall_status = "pass"

        # MINOR-A1 (2026-08-12 sol re-review, §3.1 tail): a floor with
        # `score.boundary is None` (F-22 BLOCKER-1 refusal -- no trusted
        # output convention) contributes 0/0 to both `total_boundary` and
        # `total_boundary_hits`, so the naive `missed = total - hits` formula
        # reads 0 misses on a product that was actively REFUSED, not one that
        # was checked and found complete. `walls_complete`/
        # `score_evidence_completeness` already correctly read `severe` for
        # the same refusal (see `_extract_correction_boundary`'s
        # `unsupported_output_convention` evidence entry) -- this criterion
        # must say the same thing, not the opposite, when any floor has no
        # boundary data at all.
        boundary_status = "severe" if (missed_boundary or no_data_boundary_floors) else "pass"

        if total_windows == 0 or missed_windows == 0:
            window_status = "pass"
        elif window_ratio >= WINDOW_MINOR_RATIO:
            window_status = "minor"
        else:
            window_status = "severe"

        oversplit_count = extra_walls
        if oversplit_count == 0:
            oversplit_status = "pass"
        elif oversplit_count <= EXTRA_MINOR_MAX:
            oversplit_status = "minor"
        else:
            oversplit_status = "severe"

    no_data_evidence = (
        "no_data: no reading views were scored against gt floors; "
        "scores empty so wall/window/boundary/oversplit denominators are all zero"
    )

    criteria = [
        {
            "criterion": "walls_complete",
            "suggested_status": wall_status,
            "evidence": no_data_evidence if no_scored_floors else (
                f"wall_hits={total_wall_hits}/{total_walls}; "
                f"missed={missed_walls}; extra={extra_walls}; "
                f"max_offset_m={round(max_wall_offset, 3)}; "
                f"wall_tol_m={wall_tol_m}"
            ),
            "floors": floors,
        },
        {
            "criterion": "windows_placed",
            "suggested_status": window_status,
            "evidence": no_data_evidence if no_scored_floors else (
                f"window_hits={total_windows_hit}/{total_windows}; "
                f"missed={missed_windows}; extra={extra_windows}; "
                f"hit_ratio={round(window_ratio, 3)}; "
                f"centre_tol_m={window_centre_tol_m}"
            ),
            "floors": floors,
        },
        {
            "criterion": "boundary_complete",
            "suggested_status": boundary_status,
            "evidence": no_data_evidence if no_scored_floors else (
                f"boundary_hits={total_boundary_hits}/{total_boundary}; "
                f"missed={missed_boundary}; "
                f"no_data_floors={no_data_boundary_floors}; "
                f"wall_tol_m={wall_tol_m}"
            ),
            "floors": floors,
        },
        {
            "criterion": "no_oversplit",
            "suggested_status": oversplit_status,
            "evidence": no_data_evidence if no_scored_floors else (
                f"extra_vwalls+extra_hwalls={oversplit_count}; "
                f"minor_threshold={EXTRA_MINOR_MAX}"
            ),
            "floors": floors,
        },
    ]
    if elevation is not None:
        criteria.append(
            elevation_windows_placed_criterion(
                elevation,
                elevation_along_tol_m=elevation_along_tol_m,
                sill_tol_m=sill_tol_m,
                head_tol_m=head_tol_m,
                width_tol_m=width_tol_m,
                overlap_accept=overlap_accept,
                overlap_complete=overlap_complete,
            )
        )
    if extra_evidence:
        criteria.append(
            {
                "criterion": "score_evidence_completeness",
                "suggested_status": "severe",
                "evidence": "some correction floors/windows could not be matched to gt floors",
                "details": extra_evidence,
            }
        )
    return criteria


def elevation_windows_placed_criterion(
    elevation,
    *,
    elevation_along_tol_m: float = DEFAULT_ELEVATION_ALONG_TOL_M,
    sill_tol_m: float = DEFAULT_SILL_TOL_M,
    head_tol_m: float = DEFAULT_HEAD_TOL_M,
    width_tol_m: float = DEFAULT_WIDTH_TOL_M,
    overlap_accept: float = DEFAULT_OVERLAP_ACCEPT,
    overlap_complete: float = DEFAULT_OVERLAP_COMPLETE,
) -> dict:
    """Advisory status from elevation section only.

    Misses, z-drifts, extras, and no-data facade/floor cells all count against
    accurate elevation placement.  This remains score evidence only and is never
    a ``StageVerdict`` field.
    """

    if isinstance(elevation, ElevationScoreResult):
        summary = elevation.summary()
        floors = []
        for facade, by_floor in elevation.scores.items():
            for floor, score in by_floor.items():
                placed, gt_total = score.placed_hits()
                matched, _ = score.matched_hits()
                complete = sum(1 for m in score.matches if m.status == "complete")
                within = sum(1 for m in score.matches if m.status == "within_tol")
                floors.append(
                    {
                        "facade": facade,
                        "floor": floor,
                        "orientation": score.orientation,
                        "no_data": score.no_data,
                        "gt_count": score.gt_count,
                        "read_count": score.read_count,
                        "matched_total": matched,
                        "placed_hit_total": placed,
                        "complete_total": complete,
                        "within_tol_total": within,
                        "miss_total": sum(1 for m in score.matches if m.status == "miss"),
                        "extra_total": len(score.extras),
                    }
                )
    else:
        summary = dict(elevation.get("summary", {})) if isinstance(elevation, Mapping) else {}
        floors = []
        if isinstance(elevation, Mapping):
            for facade, facade_data in (elevation.get("facades") or {}).items():
                for floor, score in (facade_data.get("floors") or {}).items():
                    matches = score.get("matches") or []
                    floors.append(
                        {
                            "facade": facade,
                            "floor": floor,
                            "orientation": score.get("orientation"),
                            "no_data": bool(score.get("no_data")),
                            "gt_count": int(score.get("gt_count", 0)),
                            "read_count": int(score.get("read_count", 0)),
                            "matched_total": int(score.get("matched_total", 0)),
                            "placed_hit_total": int(score.get("placed_hit_total", 0)),
                            "complete_total": int(score.get("complete_total", 0)),
                            "within_tol_total": int(score.get("within_tol_total", 0)),
                            "miss_total": sum(1 for m in matches if m.get("status") == "miss"),
                            "extra_total": len(score.get("extras") or []),
                        }
                    )

    gt_total = int(summary.get("gt_total", 0))
    complete = int(summary.get("complete_total", summary.get("placed_hit_total", 0)))
    within = int(summary.get("within_tol_total", 0))
    placed = complete + within
    misses = int(summary.get("miss_total", 0))
    extras = int(summary.get("extra_total", 0))
    no_data = int(summary.get("no_data_floor_facades", 0))
    failures = max(0, gt_total - placed) + extras + no_data
    ratio = 1.0 if gt_total == 0 else placed / gt_total

    if failures == 0:
        status = "pass"
    elif ratio >= WINDOW_MINOR_RATIO and no_data == 0:
        status = "minor"
    else:
        status = "severe"

    return {
        "criterion": "elevation_windows_placed",
        "suggested_status": status,
        "evidence": (
            f"elevation_placed={placed}/{gt_total}; "
            f"complete={complete}; within_tol={within}; missed={misses}; extra={extras}; "
            f"no_data_floor_facades={no_data}; hit_ratio={round(ratio, 3)}; "
            f"along_tol_m={elevation_along_tol_m}; sill_tol_m={sill_tol_m}; "
            f"head_tol_m={head_tol_m}; width_tol_m={width_tol_m}; "
            f"overlap_accept={overlap_accept}; overlap_complete={overlap_complete}"
        ),
        "floors": floors,
    }


def _status_counts(records: list[object]) -> dict[str, int]:
    out = {"complete": 0, "within_tol": 0, "miss": 0, "extra": 0}
    for record in records:
        status = getattr(record, "status", None)
        if status in out:
            out[status] += 1
    return out
