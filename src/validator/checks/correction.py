"""S1 — 1_correction deterministic check adapter (M2a, gate ①).

Wraps the A0 §7 geometry validator (agent/correction/geometry_validator.py) and
the facade translator (agent/correction/facade.py) into a CheckReport, and adds
the two checks that live at the adapter level:

  - **cross-image reconcile** (flag): the reconciled footprint width/depth must
    match the elevation facade widths, when elevation reading-views are supplied.
  - **delta/audit completeness** (block): if the deterministic core changed the
    geometry (snapped ≠ raw) or the correction relied on testdata, there must be
    a sourced ``corrections``/``conflicts`` entry — so the "0_reading was wrong"
    attribution is never silently erased (the 2f corridor-split class).
  - **window position evidence shadow** (flag; F-9 route② v2.1 §10 S2): a
    NON-blocking cross-check of the SAME model-authored ``existence`` claim
    citations the live host resolver already trusts, using the independent
    plan-authority-vs-elevation-corroborator pairing decision from
    ``correction/window_position.py``. See
    ``_window_position_evidence_shadow`` below for the full contract.

Severity→layer: coverage/nondegenerate/zstack/audit = INVARIANT (block);
window-on-wall/zone-count/reconcile/window-position-evidence-shadow = CROSS_CHECK (flag).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.agent.correction.geometry_validator import (
    GeometryFinding,
    check_window_host_resolution,
    validate_corrected_geometry,
)
from src.agent.correction.cell_geometry import cell_polygon_vertices
from src.agent.correction.config import load_core_tolerances
from src.agent.correction.envelope import (
    envelope_candidates_from_elevation_widths,
    resolve_authoritative_envelope,
)
from src.agent.correction.facade import derive_facade_frame
from src.agent.correction.schema import CorrectedGeometry
from src.agent.correction.parse import ensure_corrected_geometry
from src.agent.correction.footprint import footprint_bbox
from src.agent.geometry.capability import (
    CHECK_CAPABILITY_PROFILE_SHAPES,
    CHECK_SCHEMA_VERSION_SUPPORTED,
    SUPPORTED_SCHEMA_VERSIONS,
    allowed_shapes,
    capability_profile_allows,
    declared_shapes,
    schema_version_of,
    schema_version_supported,
)
from src.validator.checks.schema import CheckLayer, CheckReport, CheckStatus, RunProfile

if TYPE_CHECKING:
    from src.agent.execution.evidence_preflight import EvidenceDebt

# Which geometry findings are hard invariants vs soft cross-checks.
_INVARIANT_CHECKS = {
    "correction.cell_polygon_contract",
    "correction.coverage",
    "correction.nondegenerate",
    "correction.zstack_continuity",
    "correction.window_host_resolution",
}
_CROSSCHECK_CHECKS = {
    "correction.zone_count_tripwire",
    "correction.window_on_wall",
    "correction.facade_frame_cross_check",
}
_DEFERRED_RESIDUAL_CHECKS = (
    "correction.facade_area_residuals",
    "correction.wwr_residuals",
    "correction.area_residuals",
    "correction.unsupported_count_by_severity",
)


def _layer_of(check_id: str) -> CheckLayer:
    return (
        CheckLayer.INVARIANT
        if check_id in _INVARIANT_CHECKS
        else CheckLayer.CROSS_CHECK
    )


def _add_finding(rep: CheckReport, f: GeometryFinding) -> None:
    layer = _layer_of(f.check_id)
    if f.ok:
        rep.add_pass(f.check_id, layer, evidence=f.evidence)
    else:
        rep.add_fail(f.check_id, layer, f.message, evidence=f.evidence)


def check_correction(
    geom: CorrectedGeometry,
    *,
    window_host_proof=None,
    window_evidence=None,
    expected_zone_total: int | None = None,
    raw_geom: CorrectedGeometry | None = None,
    relied_on_testdata: bool = False,
    elevation_widths: dict[str, float] | None = None,
    reading_views: list[Any] | None = None,
    capability_profile: str = "rectangular",
    run_profile: RunProfile = "exploratory",
    evidence_debt: EvidenceDebt | dict | None = None,
    verified_window_inputs=None,
) -> CheckReport:
    rep = CheckReport(
        stage="1_correction",
        capability_profile=capability_profile,
        run_profile=run_profile,
    )
    try:
        geom = ensure_corrected_geometry(geom)
    except ValueError as exc:
        # A check adapter reports malformed/unknown input as a gate failure;
        # mutation/build entry points still raise at their trust boundary.
        rep.add_fail(CHECK_SCHEMA_VERSION_SUPPORTED, CheckLayer.INVARIANT, str(exc))
        return rep

    _schema_profile_compatibility(rep, geom, capability_profile)
    for f in validate_corrected_geometry(geom, expected_zone_total=expected_zone_total):
        _add_finding(rep, f)
    if str(geom.schema_version) == "3":
        _add_finding(
            rep,
            check_window_host_resolution(
                geom,
                proof=window_host_proof,
                evidence=window_evidence,
            ),
        )

    _cross_image_reconcile(rep, geom, elevation_widths)
    _facade_frame_cross_check(rep, geom, reading_views)
    _audit_completeness(rep, geom, raw_geom, relied_on_testdata)
    _evidence_debt_coverage(rep, geom, evidence_debt)
    unresolved_openings = [row for row in geom.unsupported if row.get("kind") == "as_drawn_opening_unbuilt"]
    if unresolved_openings:
        rep.add_fail(
            "correction.plan_opening_completeness", CheckLayer.INVARIANT,
            "Explicitly identified plan openings remain unbuilt; the source model is incomplete.",
            evidence={"unbuilt": unresolved_openings, "built_openings": len(geom.openings)},
        )
    _deferred_residual_placeholders(rep)
    _window_position_evidence_shadow(rep, geom, verified_window_inputs)
    return rep


def _schema_profile_compatibility(
    rep: CheckReport, geom: CorrectedGeometry, capability_profile: str
) -> None:
    version = schema_version_of(geom)
    if not schema_version_supported(geom):
        rep.add_fail(
            CHECK_SCHEMA_VERSION_SUPPORTED,
            CheckLayer.INVARIANT,
            f"unsupported correction schema_version {version!r}",
            evidence={
                "schema_version": version,
                "supported_versions": sorted(SUPPORTED_SCHEMA_VERSIONS),
            },
        )
        return
    rep.add_pass(
        CHECK_SCHEMA_VERSION_SUPPORTED,
        CheckLayer.INVARIANT,
        evidence={"schema_version": version},
    )

    data_shapes = declared_shapes(geom)
    profile_shapes = allowed_shapes(capability_profile)
    if not capability_profile_allows(geom, capability_profile):
        rep.add_fail(
            CHECK_CAPABILITY_PROFILE_SHAPES,
            CheckLayer.INVARIANT,
            "capability_profile does not allow all shapes declared by "
            "correction schema_version",
            evidence={
                "capability_profile": capability_profile,
                "declared_shapes": sorted(data_shapes),
                "allowed_shapes": sorted(profile_shapes),
            },
        )
        return
    rep.add_pass(
        CHECK_CAPABILITY_PROFILE_SHAPES,
        CheckLayer.INVARIANT,
        evidence={
            "capability_profile": capability_profile,
            "declared_shapes": sorted(data_shapes),
            "allowed_shapes": sorted(profile_shapes),
        },
    )


def check_evidence_debt_coverage(
    geom: CorrectedGeometry,
    evidence_debt: EvidenceDebt | dict | None,
    *,
    capability_profile: str = "rectangular",
    run_profile: RunProfile = "exploratory",
) -> CheckReport:
    rep = CheckReport(
        stage="1_correction",
        capability_profile=capability_profile,
        run_profile=run_profile,
    )
    _evidence_debt_coverage(rep, geom, evidence_debt)
    return rep


def _cross_image_reconcile(
    rep: CheckReport,
    geom: CorrectedGeometry,
    elevation_widths: dict[str, float] | None,
) -> None:
    """Footprint width/depth vs elevation facade widths (flag)."""
    if not elevation_widths:
        rep.add("correction.cross_image_reconcile", CheckStatus.NOT_APPLICABLE,
                CheckLayer.CROSS_CHECK, message="no elevation widths supplied")
        return
    (xmin, xmax), (ymin, ymax) = footprint_bbox(geom)
    width = abs(xmax - xmin)   # N/S facade width
    depth = abs(ymax - ymin)   # E/W facade width
    envelope = resolve_authoritative_envelope(
        envelope_candidates_from_elevation_widths(elevation_widths),
        footprint={"x": (xmin, xmax), "y": (ymin, ymax)},
        footprint_tolerance_m=0.10,
    )
    expected = {"North": width, "South": width, "East": depth, "West": depth}
    mismatches = []
    for facade, w in elevation_widths.items():
        exp = expected.get(facade)
        if exp is None:
            continue
        if abs(w - exp) > 0.10:
            mismatches.append({"facade": facade, "elevation_width": w,
                               "footprint_width": round(exp, 3)})
    if mismatches:
        rep.add_fail(
            "correction.cross_image_reconcile", CheckLayer.CROSS_CHECK,
            f"{len(mismatches)} facade width(s) disagree with the footprint",
            evidence={
                "mismatches": mismatches,
                "authoritative_envelope": envelope.to_dict(),
            })
    else:
        rep.add_pass("correction.cross_image_reconcile", CheckLayer.CROSS_CHECK,
                     evidence={
                         "facades_checked": len(elevation_widths),
                         "authoritative_envelope": envelope.to_dict(),
                     })


def _facade_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    facade = value.strip().capitalize()
    return facade if facade in {"North", "South", "East", "West"} else None


def _reading_elevation_windows(reading_views: list[Any] | None) -> tuple[list[dict], list[str]]:
    windows: list[dict] = []
    unusable: list[str] = []
    if not reading_views:
        return windows, unusable

    for view_index, view in enumerate(reading_views):
        if str(getattr(view, "image_kind", "")).lower() != "elevation":
            continue
        facade_info = getattr(view, "facade", None)
        facade = _facade_name(getattr(facade_info, "view_facade", None))
        if facade is None:
            unusable.append(getattr(view, "image_label", None) or f"view[{view_index}]")
            continue
        for stroke in getattr(view, "strokes", []):
            if str(getattr(stroke, "pen", "")).lower() != "window":
                continue
            geom = getattr(stroke, "geometry", {}) or {}
            x_range = geom.get("x_range_m") if isinstance(geom, dict) else None
            if (
                not isinstance(x_range, list)
                or len(x_range) != 2
                or x_range[0] is None
                or x_range[1] is None
            ):
                unusable.append(getattr(stroke, "id", None) or f"view[{view_index}].window")
                continue
            try:
                local_span = sorted([float(x_range[0]), float(x_range[1])])
            except (TypeError, ValueError):
                unusable.append(getattr(stroke, "id", None) or f"view[{view_index}].window")
                continue
            windows.append(
                {
                    "view": getattr(view, "image_label", None) or f"view[{view_index}]",
                    "stroke_id": getattr(stroke, "id", None),
                    "facade": facade,
                    "local_span": local_span,
                    "local_x_positive": getattr(
                        facade_info, "local_x_positive", "image_left_to_right"
                    ),
                    "mirrored": getattr(facade_info, "mirrored", "unknown"),
                }
            )
    return windows, unusable


def _footprint_bounds(geom: CorrectedGeometry) -> tuple[list[float], list[float]] | None:
    try:
        (x0, x1), (y0, y1) = footprint_bbox(geom)
        fx, fy = [x0, x1], [y0, y1]
    except (TypeError, ValueError):
        return None
    if len(fx) < 2 or len(fy) < 2 or min(fx) == max(fx) or min(fy) == max(fy):
        return None
    return fx, fy


def _window_center(span: list[float]) -> float:
    return (float(span[0]) + float(span[1])) / 2.0


def _facade_frame_cross_check(
    rep: CheckReport,
    geom: CorrectedGeometry,
    reading_views: list[Any] | None,
) -> None:
    check_id = "correction.facade_frame_cross_check"
    reading_windows, unusable = _reading_elevation_windows(reading_views)
    if not reading_views:
        rep.add(
            check_id,
            CheckStatus.NOT_APPLICABLE,
            CheckLayer.CROSS_CHECK,
            message="no elevation reading artifacts supplied",
        )
        return
    if not reading_windows:
        rep.add(
            check_id,
            CheckStatus.NOT_APPLICABLE,
            CheckLayer.CROSS_CHECK,
            message="no usable elevation reading window local-x data",
            evidence={"unusable": unusable},
        )
        return
    bounds = _footprint_bounds(geom)
    if bounds is None:
        rep.add(
            check_id,
            CheckStatus.NOT_APPLICABLE,
            CheckLayer.CROSS_CHECK,
            message="no usable footprint bounds for facade frame",
        )
        return
    if not geom.windows:
        rep.add(
            check_id,
            CheckStatus.NOT_APPLICABLE,
            CheckLayer.CROSS_CHECK,
            message="no correction windows to compare",
            evidence={"reading_windows": len(reading_windows), "unusable": unusable},
        )
        return

    fx, fy = bounds
    tol = load_core_tolerances().facade_frame_cross_check_tol_m
    predicted: list[dict] = []
    for item in reading_windows:
        frame = derive_facade_frame(
            view_facade=item["facade"],
            footprint_x=fx,
            footprint_y=fy,
            mirrored=item["mirrored"],
            local_x_positive=item["local_x_positive"],
        )
        world_span = sorted([frame.to_world_along(v) for v in item["local_span"]])
        predicted.append(
            {
                **item,
                "world_axis": frame.world_axis,
                "deterministic_world_span": [round(v, 6) for v in world_span],
                "deterministic_world_center": round(_window_center(world_span), 6),
            }
        )

    correction_by_facade: dict[str, list[dict]] = {}
    for win in geom.windows:
        facade = _facade_name(getattr(win, "facade", None))
        if facade is None or not getattr(win, "span", None):
            continue
        try:
            llm_span = [float(v) for v in win.span]
        except (TypeError, ValueError):
            continue
        correction_by_facade.setdefault(facade, []).append(
            {
                "window_id": getattr(win, "id", None),
                "facade": facade,
                "llm_world_span": [round(v, 6) for v in llm_span],
                "llm_world_center": round(_window_center(llm_span), 6),
            }
        )
    if not correction_by_facade:
        rep.add(
            check_id,
            CheckStatus.NOT_APPLICABLE,
            CheckLayer.CROSS_CHECK,
            message="no usable correction window spans to compare",
            evidence={"reading_windows": len(reading_windows), "unusable": unusable},
        )
        return

    matches: list[dict] = []
    unmatched_reading: list[dict] = []
    unmatched_correction: list[dict] = []
    mismatches: list[dict] = []
    for facade in sorted({item["facade"] for item in predicted} | set(correction_by_facade)):
        reads = [item for item in predicted if item["facade"] == facade]
        corrs = list(correction_by_facade.get(facade, []))
        used: set[int] = set()
        for read in reads:
            available = [
                (idx, corr)
                for idx, corr in enumerate(corrs)
                if idx not in used
            ]
            if not available:
                unmatched_reading.append(read)
                continue
            idx, corr = min(
                available,
                key=lambda pair: abs(
                    pair[1]["llm_world_center"] - read["deterministic_world_center"]
                ),
            )
            used.add(idx)
            delta = round(
                corr["llm_world_center"] - read["deterministic_world_center"], 6
            )
            row = {
                "facade": facade,
                "reading_view": read["view"],
                "reading_stroke_id": read["stroke_id"],
                "correction_window_id": corr["window_id"],
                "reading_local_span": read["local_span"],
                "deterministic_world_span": read["deterministic_world_span"],
                "deterministic_world_center": read["deterministic_world_center"],
                "llm_world_span": corr["llm_world_span"],
                "llm_world_center": corr["llm_world_center"],
                "delta_m": delta,
                "abs_delta_m": round(abs(delta), 6),
            }
            matches.append(row)
            if abs(delta) > tol:
                mismatches.append(row)
        for idx, corr in enumerate(corrs):
            if idx not in used:
                unmatched_correction.append(corr)

    evidence = {
        "tolerance_m": tol,
        "matches": matches,
        "unusable": unusable,
        "unmatched_reading": unmatched_reading,
        "unmatched_correction": unmatched_correction,
    }
    if mismatches or unmatched_reading or unmatched_correction:
        rep.add_fail(
            check_id,
            CheckLayer.CROSS_CHECK,
            "facade-frame deterministic placement disagrees with correction windows",
            evidence={**evidence, "mismatches": mismatches},
        )
    else:
        rep.add_pass(
            check_id,
            CheckLayer.CROSS_CHECK,
            evidence={
                "tolerance_m": tol,
                "matches_checked": len(matches),
                "unusable": unusable,
            },
        )


def _geom_signature(geom: CorrectedGeometry) -> list:
    """Coordinate signature for detecting whether the core changed geometry."""
    def cell_sig(c) -> list:
        try:
            poly = cell_polygon_vertices(c)
        except ValueError as exc:
            polygon_sig = [["INVALID", str(exc)]]
        else:
            polygon_sig = [] if poly is None else [[float(x), float(y)] for x, y in poly]
        return [c.id, *map(float, c.x), *map(float, c.y), polygon_sig]

    return [
        [fl.name, fl.z_floor, fl.ceiling_height,
         sorted(cell_sig(c) for c in fl.cells)]
        for fl in sorted(geom.floors, key=lambda f: f.name)
    ]


def _audit_completeness(
    rep: CheckReport,
    geom: CorrectedGeometry,
    raw_geom: CorrectedGeometry | None,
    relied_on_testdata: bool,
) -> None:
    """If geometry was materially changed (snapped ≠ raw) or testdata was relied
    on, there must be a sourced corrections/conflicts entry (block)."""
    changed = False
    if raw_geom is not None:
        changed = _geom_signature(geom) != _geom_signature(raw_geom)
    needs_audit = changed or relied_on_testdata
    if not needs_audit:
        rep.add("correction.audit_completeness", CheckStatus.NOT_APPLICABLE,
                CheckLayer.INVARIANT,
                message="no material change / no testdata reliance detected")
        return
    audit_entries = list(geom.corrections) + list(geom.conflicts)
    if not audit_entries:
        rep.add_fail(
            "correction.audit_completeness", CheckLayer.INVARIANT,
            "geometry was changed (or relied on testdata) but no correction/"
            "conflict was recorded — attribution to 0_reading would be erased",
            evidence={"changed": changed, "relied_on_testdata": relied_on_testdata})
        return
    # Each audit entry must be attributable — carry at least one provenance marker
    # (the A0 audit schema uses rule_id / stage / claim_type / method_profile;
    # a lighter hand-written entry may use source / reason / rule / type).
    _PROVENANCE_KEYS = {
        "source", "reason", "rule", "rule_id", "type", "claim_type", "kind",
        "what", "stage", "method_profile",
    }
    unsourced = [
        i for i, e in enumerate(audit_entries)
        if isinstance(e, dict) and not (e.keys() & _PROVENANCE_KEYS)
    ]
    if unsourced:
        rep.add_fail(
            "correction.audit_completeness", CheckLayer.INVARIANT,
            f"{len(unsourced)} correction/conflict entry(ies) lack a source/reason",
            evidence={"unsourced_indices": unsourced})
    else:
        rep.add_pass("correction.audit_completeness", CheckLayer.INVARIANT,
                     evidence={"audit_entries": len(audit_entries)})


def _coerce_debt(evidence_debt: EvidenceDebt | dict | None) -> Any | None:
    if evidence_debt is None:
        return None
    from src.agent.execution.evidence_preflight import EvidenceDebt

    if isinstance(evidence_debt, EvidenceDebt):
        return evidence_debt
    if isinstance(evidence_debt, dict):
        return EvidenceDebt.model_validate(evidence_debt)
    return None


def _audit_rows(geom: CorrectedGeometry) -> list[dict]:
    return [
        row
        for row in [*list(geom.conflicts), *list(geom.corrections)]
        if isinstance(row, dict)
    ]


def _resolved_debt_ids(rows: list[dict]) -> list[str]:
    """Only typed LLM/A3 resolution entries can settle an input debt.

    Core audit rows have no ``resolves_debt_id`` field, so deterministic cleanup
    can never accidentally wash away an evidence obligation.
    """
    from src.agent.correction.schema import DebtResolutionAuditEntry
    ids = []
    for row in rows:
        if row.get("kind") == "debt_resolution":
            ids.append(DebtResolutionAuditEntry.model_validate(row).resolves_debt_id)
    return ids


def _legacy_covered_by_audit(item, rows: list[dict]) -> bool:
    """Compatibility reader for V1/V2 artifacts; V3 is strict below."""
    def mentions(row, values):
        text = str(row)
        return any(value and value in text for value in values)
    if item.scope == "element_local":
        return bool(item.offender_ids) and all(any(mentions(row, [offender]) for row in rows)
                                               for offender in item.offender_ids)
    return any(mentions(row, [item.check_id, item.canonical_check_id, item.view]) for row in rows)


def _evidence_debt_coverage(
    rep: CheckReport,
    geom: CorrectedGeometry,
    evidence_debt: EvidenceDebt | dict | None,
) -> None:
    """A8.3b: check only that reading evidence debt was explicitly covered.

    Element-local debt is strong enough to block in golden/regression via
    ``disposition()``. View/global debt cannot be mapped to a specific cell/window,
    so it remains advisory even under those profiles.
    """
    debt = _coerce_debt(evidence_debt)
    if debt is None or not debt.debts:
        return

    rows = _audit_rows(geom)
    resolved = _resolved_debt_ids(rows)
    duplicate_ids = {value for value in resolved if resolved.count(value) > 1}
    element_missing = []
    advisory_missing = []
    for item in debt.debts:
        is_covered = (item.debt_id in resolved and item.debt_id not in duplicate_ids
                      if geom.schema_version == "3" else _legacy_covered_by_audit(item, rows))
        if is_covered:
            continue
        payload = {
            "check_id": item.check_id,
            "canonical_check_id": item.canonical_check_id,
            "view": item.view,
            "message": item.message,
        }
        if item.scope == "element_local":
            element_missing.append({**payload, "offender_ids": item.offender_ids})
        else:
            advisory_missing.append(payload)

    if duplicate_ids:
        rep.add_fail("correction.evidence_debt_coverage", CheckLayer.CROSS_CHECK,
                     "evidence debt may be resolved at most once",
                     evidence={"duplicate_resolves_debt_ids": sorted(duplicate_ids)})
    if element_missing:
        rep.add_fail(
            "correction.evidence_debt_coverage",
            CheckLayer.CROSS_CHECK,
            f"{len(element_missing)} element-local evidence debt item(s) were not "
            "covered by conflicts/corrections",
            evidence={"scope": "element_local", "missing": element_missing},
        )
    if advisory_missing:
        rep.add_fail(
            "correction.evidence_debt_coverage",
            CheckLayer.CROSS_CHECK,
            f"{len(advisory_missing)} view/global evidence debt item(s) were not "
            "mentioned in correction audit",
            evidence={"scope": "view_global", "missing": advisory_missing},
        )
    if not element_missing and not advisory_missing:
        rep.add_pass(
            "correction.evidence_debt_coverage",
            CheckLayer.CROSS_CHECK,
            evidence={"scope": "all", "debt_items": len(debt.debts)},
        )


def _deferred_residual_placeholders(rep: CheckReport) -> None:
    """A0 residual soft checks are visible placeholders until evidence improves."""
    for check_id in _DEFERRED_RESIDUAL_CHECKS:
        rep.add(
            check_id,
            CheckStatus.NOT_APPLICABLE,
            CheckLayer.CROSS_CHECK,
            message="deferred until evidence is richer",
        )


_WINDOW_POSITION_EVIDENCE_SHADOW_CHECK_ID = "correction.window_position_evidence_shadow"


def _window_position_evidence_shadow(
    rep: CheckReport,
    geom: CorrectedGeometry,
    verified_window_inputs,
) -> None:
    """F-9 route② v2.1 §10 S2: a NON-blocking cross-check that runs the
    independent plan-authority-vs-elevation-corroborator pairing decision
    (``src.agent.correction.window_position.compute_window_position_evidence_
    shadow``) alongside today's live model-authored ``window.span`` path,
    WITHOUT overriding ``span`` and WITHOUT ever contributing a BLOCK
    disposition — this check is always registered at ``CheckLayer.CROSS_CHECK``
    (which ``src/validator/checks/schema.py::disposition()`` maps to
    ``Disposition.FLAG`` on FAIL, on every ``run_profile``; only
    ``CheckStatus.ERROR`` would map to BLOCK, and this function never emits
    that status — every branch below is PASS/FAIL/NOT_APPLICABLE).

    Three, separately observable, never-``None``-collapsed states (v2.1 §9:
    "不可用 ``None`` 表示『跑过但没结果』"):

    - ``verified_window_inputs is None`` (legacy schema, or the caller never
      built one) -> this check did not run at all -> ``NOT_APPLICABLE``.
    - shadow evidence computed successfully (``status="evaluated"``,
      possibly ``window_count=0``) -> ``PASS`` iff every window's decision is
      ``"accepted"``, else ``FAIL`` (still only a FLAG-mapped fact, never a
      block).
    - the shared current-ring binding materializer could not even build this
      ring's authoritative frames (``status="binding_unavailable"``, or the
      module's own dispatcher raised a ``TypeError`` -- e.g. a caller
      substituting an advisory frame for the authoritative one, v2.1 §1.3)
      -> ``FAIL`` with the concrete error recorded as evidence, never a
      silent PASS and never an uncaught crash that would propagate past this
      non-blocking check into the real accept/reject decision.
    """
    if str(geom.schema_version) != "3" or verified_window_inputs is None:
        rep.add(
            _WINDOW_POSITION_EVIDENCE_SHADOW_CHECK_ID,
            CheckStatus.NOT_APPLICABLE,
            CheckLayer.CROSS_CHECK,
            message="no v3 verified_window_inputs supplied — shadow evidence did not run",
        )
        return

    from src.agent.correction.config import load_core_tolerances
    from src.agent.correction.window_position import compute_window_position_evidence_shadow

    try:
        shadow = compute_window_position_evidence_shadow(
            geom, verified_inputs=verified_window_inputs, tol=load_core_tolerances(),
        )
    except Exception as exc:  # noqa: BLE001 — deliberately total: a shadow
        # observer must never let ITS OWN wiring/type errors (e.g. an
        # advisory frame injected where an authoritative one belongs, v2.1
        # §1.3/§12.2's "Advisory 隔离" must-red neuter) propagate past this
        # non-blocking check and crash — or silently block — the real draw.
        rep.add_fail(
            _WINDOW_POSITION_EVIDENCE_SHADOW_CHECK_ID,
            CheckLayer.CROSS_CHECK,
            f"window position evidence shadow computation raised "
            f"{type(exc).__name__}: {exc}",
            evidence={"exception_type": type(exc).__name__, "exception_message": str(exc)},
        )
        return

    if shadow.status != "evaluated":
        rep.add_fail(
            _WINDOW_POSITION_EVIDENCE_SHADOW_CHECK_ID,
            CheckLayer.CROSS_CHECK,
            f"window position evidence shadow could not evaluate this ring "
            f"({shadow.binding_error_code})",
            evidence={
                "status": shadow.status, "binding_error_code": shadow.binding_error_code,
                "ruleset_version": shadow.ruleset_version,
                "evaluated_conditions": list(shadow.evaluated_conditions),
                "unevaluated_conditions": list(shadow.unevaluated_conditions),
            },
        )
        return

    rejected = [d for d in shadow.decisions if d.decision == "rejected"]
    evidence = {
        "producer_draw_sha256": shadow.producer_draw_sha256,
        "resolver_inputs_sha256": shadow.resolver_inputs_sha256,
        "window_count": shadow.window_count,
        "rejected_window_ids": [d.window_id for d in rejected],
        "rejected_reject_codes": sorted({d.reject_code for d in rejected if d.reject_code}),
        "max_legacy_span_delta_m": max(
            (d.legacy_span_delta_m for d in shadow.decisions if d.legacy_span_delta_m is not None),
            default=None,
        ),
        # MAJOR-B1 (`AI_agent/logs/reviews/verdict/
        # 2026-08-12_round2_blocker1_and_bcd_crossreview_sol.md`): the
        # machine-readable coverage declaration sol's finding demanded —
        # "shadow 用无修饰的 accepted/PASS 表达只覆盖部分判据的结果...把结论
        # 说得比证据强". A bare PASS status alone no longer tells a consumer
        # WHICH of v2.1 §5.3's six conditions were actually checked; these
        # three fields do. `WindowPositionEvidenceShadowReportV1`'s own
        # validator makes it structurally impossible for `shadow.all_accepted`
        # to be `True` while `unevaluated_conditions` is non-empty, so this
        # PASS branch below can never fire on an incomplete-coverage report —
        # that invariant is enforced at construction, not re-checked here.
        "ruleset_version": shadow.ruleset_version,
        "evaluated_conditions": list(shadow.evaluated_conditions),
        "unevaluated_conditions": list(shadow.unevaluated_conditions),
        "content_sha256": shadow.content_sha256,
    }
    if shadow.all_accepted:
        rep.add_pass(_WINDOW_POSITION_EVIDENCE_SHADOW_CHECK_ID, CheckLayer.CROSS_CHECK, evidence=evidence)
    else:
        rep.add_fail(
            _WINDOW_POSITION_EVIDENCE_SHADOW_CHECK_ID,
            CheckLayer.CROSS_CHECK,
            f"{len(rejected)}/{shadow.window_count} window(s) failed the independent "
            "plan-authority vs elevation-corroborator pairing decision (cross-check "
            "only — does not affect this draw's accept/reject result)",
            evidence=evidence,
        )
