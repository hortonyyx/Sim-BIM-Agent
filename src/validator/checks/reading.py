"""S0 — 0_reading deterministic structure linter (M2a, gate ①).

Per contracts §1 0_reading and the pen library, this is a *per-image* linter: it
asks only "is this one drawing structurally well-formed and self-consistent",
never topology / cross-image placement (those belong to 1_correction). The one
world-placement datum it does look at is the plan's own ``scale_origin`` — a
per-image declaration of how that drawing's local frame sits in the single world
frame, required by guide.md §1 and consumed by gate②; it assigns no rooms,
surfaces or facades, so the no-topology discipline is intact.

Layers (§0.2):
  - INVARIANT (block): unique stroke/dimension ids, legal pen × kind for the
    image_kind, finite numerics, non-degenerate line/rect, parseable dimensions,
    axis-endpoint consistency, image-local facade fields present on elevations,
    uncaptured present as a list (NOT required non-empty — clean drawing → []).
  - CROSS_CHECK (flag): single-image dimension-chain closure (Σ segments ==
    overall), low-confidence internal stroke↔dimension consistency, out-of-bounds.
  - CROSS_CHECK, profile-gated to BLOCK under golden/regression: plan
    ``scale_origin`` usable (see :func:`_plan_scale_origin`).

Returns a :class:`CheckReport`; policy (block vs flag) is applied by the report.
"""

from __future__ import annotations

import json
import math

from src.agent.reading.legacy import parse_value_m, reading_raw_metadata
from src.agent.reading.schema import ReadingView
from src.agent.reading.constants import DIMCHAIN_CLOSE_TOL_M
from src.agent.roles import CANONICAL_ROLES, normalize
from src.validator.checks.schema import (
    CALIBRATION_AXES_CHECK_ID,
    DIMENSION_ENDPOINT_BOUNDS_CHECK_ID,
    PLAN_FRAME_CHECK_ID,
    CheckLayer,
    CheckReport,
    CheckStatus,
    RunProfile,
)

# Legal pen sets by image kind (pen_library.md §2).
_PLAN_PENS = {"wall", "window"}
_ELEVATION_PENS = {"wall_fill", "window", "outline"}
_GEOMETRY_KINDS = {"line", "rect", "polyline"}
# Topology / world fields a reading stroke must NOT carry (those are 1_correction's).
_FORBIDDEN_STROKE_KEYS = {
    "zone",
    "adjacent_zone",
    "adjacent_surface",
    "obc",
    "world_z",
    "is_exterior",
    "parent_wall_id",
    "parent_window_ids",
    "rooms",
}
_ROOM_LABEL_BASES = {"label", "furniture", "ocr"}
_MIN_EXTENT = 0.05  # m — below this a line/rect is degenerate
_OUTPUT_PRECISION_M = 0.01  # A0 OUTPUT_PRECISION scale
_PROVENANCE_PENS = {"wall", "window", "wall_fill", "outline"}
_WINDOW_JAMB_TOLERANCE_M = 0.20
# M-3 (r1 / F-4 ③): OCR/annotation anchors are tolerated up to this far OUTSIDE
# the trusted structural extent (a label sitting just past a wall is legitimate).
# A PIXEL anchor like [360, 450] on a ~10 m plan is orders of magnitude outside,
# so any reasonable margin still surfaces it — that is the bad-data signal O-4's
# canvas fix removed and this check restores. Kept generous vs the renderer's
# MARGIN_M (render_vector_to_png) so a label flagged here is one a reviewer would
# NOT see in the render.
_OCR_ANCHOR_MARGIN_M = 2.0
# B-1 (r3 batchC dispatch §1 BLOCKER, route (b) — unit-anomaly, no external
# root): factor applied to the median absolute deviation (MAD) of a view's own
# stroke geometry to build a plausible-magnitude fence for OCR anchors /
# dimension endpoints. "An order of magnitude" (r3 dispatch's own wording) ==
# 10x. See _structural_metric_reference for the full worked justification
# (why this resists the three r1/r2 self-inflation tricks and does not
# falsely accuse legitimate large buildings).
_UNIT_ANOMALY_MAD_FACTOR = 10.0
# Absolute floor (metres) on the fence half-width, so a view with a tiny or
# degenerate structural extent (e.g. a single short wall segment, MAD ~= 0)
# does not become hair-trigger — no legitimate architectural annotation sits
# farther than this from the drawing's own traced geometry.
_UNIT_ANOMALY_FLOOR_M = 5.0


def _finite(*vals) -> bool:
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in vals)


def _legal_pens(image_kind: str) -> set[str] | None:
    k = (image_kind or "").lower()
    if k == "plan":
        return _PLAN_PENS
    if k == "elevation":
        return _ELEVATION_PENS
    return None  # section/supplementary/other: pen set not constrained here


def _uncaptured_list(view: ReadingView) -> list | None:
    """Effective uncaptured entries, pooled across the canonical top-level
    ``uncaptured`` and the tolerated legacy carriers (top-level
    ``uncaptured_visual_elements`` extra, and the old nested
    ``self_check.uncaptured_visual_elements``). Returns ``None`` only when no
    carrier is present as a list at all."""
    found = False
    pooled: list = []
    if isinstance(view.uncaptured, list):
        found = True
        pooled.extend(view.uncaptured)
    extra = getattr(view, "__pydantic_extra__", None) or {}
    legacy = extra.get("uncaptured_visual_elements")
    if isinstance(legacy, list):
        found = True
        pooled.extend(legacy)
    if isinstance(view.self_check, dict):
        nested = view.self_check.get("uncaptured_visual_elements")
        if isinstance(nested, list):
            found = True
            pooled.extend(nested)
    return pooled if found else None


def check_reading_view(
    view: ReadingView,
    *,
    capability_profile: str = "rectangular",
    run_profile: RunProfile = "exploratory",
    view_metadata: dict | None = None,
    dimensioned: bool | None = None,
    dimensioned_state: str | None = None,
) -> CheckReport:
    rep = CheckReport(
        stage="0_reading",
        capability_profile=capability_profile,
        run_profile=run_profile,
    )
    meta = _view_metadata(
        view, view_metadata, dimensioned=dimensioned, dimensioned_state=dimensioned_state,
    )

    # ---- INVARIANT: unique ids ----
    _unique_ids(rep, "stroke", [s.id for s in view.strokes])
    _unique_ids(rep, "dimension", [d.id for d in view.dimensions])

    # ---- INVARIANT: legal pen × kind ----
    _pen_kind(rep, view)

    # ---- INVARIANT: no topology/world fields on strokes ----
    _no_topology_fields(rep, view)

    # ---- INVARIANT: finite + non-degenerate geometry ----
    _geometry_wellformed(rep, view)

    # ---- INVARIANT: dimensions parseable + axis-endpoint consistent ----
    _dimensions_wellformed(rep, view)

    # ---- INVARIANT: elevation facade image-local fields present ----
    _facade_fields(rep, view)

    # ---- profile-gated: plan views declare a usable local→world frame ----
    _plan_scale_origin(rep, view)

    # ---- INVARIANT: uncaptured present as a list (not required non-empty) ----
    unc = _uncaptured_list(view)
    if unc is None:
        rep.add_fail(
            "reading.uncaptured_present", CheckLayer.INVARIANT,
            "uncaptured field missing or not a list",
        )
    else:
        rep.add_pass("reading.uncaptured_present", CheckLayer.INVARIANT,
                     evidence={"count": len(unc)})

    _raw_field_presence(rep, meta)

    # ---- CROSS_CHECK: dimensioned source views carry dimension evidence ----
    _dimensioned_view_evidence(rep, view, meta)

    # ---- INVARIANT: topology-light room-role observations, only if present ----
    _room_labels_wellformed(rep, view)

    # ---- CROSS_CHECK (profile-gated BLOCK): OCR/annotation anchors in bounds ----
    # B-1 (r3 batchC dispatch §1 BLOCKER): the r2 X-2 mechanism (a "trusted"
    # bound resolved from the case_data source image's real PIXEL size) was
    # dimensionally wrong — reading coordinates are METRES — so it was retired.
    # See _structural_metric_reference for the replacement: a dimensionally
    # consistent, dependency-free unit-anomaly check computed entirely from
    # this view's own stroke geometry (never from case_dir/manifest, so there
    # is no more "trusted vs fallback" split — one mechanism, always).
    _ocr_anchors_in_bounds(rep, view)

    # ---- CROSS_CHECK (profile-gated BLOCK): dimension endpoints in bounds ----
    _dimension_endpoints_in_bounds(rep, view)

    # ---- CROSS_CHECK: dimension-chain closure ----
    _chain_closure(rep, view, meta)

    # ---- CROSS_CHECK: dimension-derived strokes cite real dimensions ----
    _dimension_derived_refs(rep, view, meta)

    # ---- CROSS_CHECK: stroke provenance + internal stroke↔dimension consistency ----
    _stroke_dimension_consistency(rep, view, meta)

    # ---- CROSS_CHECK: healed door openings leave a trace in uncaptured ----
    _door_heal_traced(rep, view)

    return rep


_DIMENSIONED_STATES = ("declared_true", "declared_false", "unknown", "legacy_default")


def _view_metadata(
    view: ReadingView,
    view_metadata: dict | None,
    *,
    dimensioned: bool | None,
    dimensioned_state: str | None = None,
) -> dict:
    meta = reading_raw_metadata(view)
    if view_metadata:
        meta.update(view_metadata)
    if dimensioned is not None:
        meta["dimensioned"] = dimensioned
    # S-3: the 4-state dimensioned_state is the authoritative applicability signal
    # carried to checks.json evidence (declared_true / declared_false / unknown /
    # legacy_default). Derive it from the legacy bool (explicit param, else the
    # view_metadata bool) when a structured state was not passed, but never fold
    # it back to a bool downstream — the whole point is keeping
    # unknown ≠ declared_false ≠ legacy_default visible end-to-end.
    if dimensioned_state is None:
        prior = meta.get("dimensioned_state")
        if isinstance(prior, str) and prior in _DIMENSIONED_STATES:
            dimensioned_state = prior
        else:
            dimensioned_state = "declared_true" if meta.get("dimensioned") is True else "legacy_default"
    meta["dimensioned_state"] = dimensioned_state
    # Legacy bool consumers (_chain_closure etc.) still see a bool: only an
    # affirmative declared_true counts as "dimensioned".
    meta["dimensioned"] = dimensioned_state == "declared_true"
    meta.setdefault("legacy_migrated", bool(getattr(view, "migrated_from_legacy", False)))
    return meta


def _evidence_meta(meta: dict) -> dict:
    return {
        "dimensioned": bool(meta.get("dimensioned")),
        "dimensioned_state": meta.get("dimensioned_state", "legacy_default"),
        "raw_has_dimensions": meta.get("raw_has_dimensions"),
        "raw_has_uncaptured": meta.get("raw_has_uncaptured"),
        "legacy_migrated": bool(meta.get("legacy_migrated")),
    }


def _unique_ids(rep: CheckReport, kind: str, ids: list[str]) -> None:
    seen, dupes = set(), []
    for i in ids:
        if i in seen:
            dupes.append(i)
        seen.add(i)
    if dupes:
        rep.add_fail(
            f"reading.{kind}_ids_unique", CheckLayer.INVARIANT,
            f"duplicate {kind} id(s): {sorted(set(dupes))}",
            evidence={"duplicates": sorted(set(dupes))},
        )
    else:
        rep.add_pass(f"reading.{kind}_ids_unique", CheckLayer.INVARIANT)


def _room_labels_wellformed(rep: CheckReport, view: ReadingView) -> None:
    labels = view.room_labels
    if not labels:
        return

    _unique_ids(rep, "room_label", [label.id for label in labels])

    bad_roles = []
    for label in labels:
        role = normalize(label.role)
        if role not in CANONICAL_ROLES:
            bad_roles.append({"id": label.id, "role": label.role, "normalized": role})
    if bad_roles:
        rep.add_fail(
            "reading.room_label_roles_valid", CheckLayer.INVARIANT,
            f"{len(bad_roles)} room label role(s) outside canonical vocabulary",
            evidence={
                "offenders": bad_roles,
                "canonical_roles": sorted(CANONICAL_ROLES),
            },
        )
    else:
        rep.add_pass("reading.room_label_roles_valid", CheckLayer.INVARIANT)

    bad_basis = [
        {"id": label.id, "basis": label.basis}
        for label in labels
        if label.basis not in _ROOM_LABEL_BASES
    ]
    if bad_basis:
        rep.add_fail(
            "reading.room_label_basis_valid", CheckLayer.INVARIANT,
            f"{len(bad_basis)} room label(s) with illegal basis",
            evidence={"offenders": bad_basis, "allowed": sorted(_ROOM_LABEL_BASES)},
        )
    else:
        rep.add_pass("reading.room_label_basis_valid", CheckLayer.INVARIANT)

    bounds = _image_bounds(view)
    bad_anchors = []
    if bounds is None:
        bad_anchors = [{"id": label.id, "anchor": label.anchor, "reason": "no image bounds"} for label in labels]
    else:
        xmin, xmax, ymin, ymax = bounds
        for label in labels:
            anchor = label.anchor
            if len(anchor) != 2 or not _finite(*anchor):
                bad_anchors.append(
                    {"id": label.id, "anchor": anchor, "reason": "not two finite numbers"}
                )
                continue
            x, y = anchor
            if not (xmin <= x <= xmax and ymin <= y <= ymax):
                bad_anchors.append(
                    {
                        "id": label.id,
                        "anchor": anchor,
                        "reason": "outside image bounds",
                    }
                )
    if bad_anchors:
        rep.add_fail(
            "reading.room_label_anchors_in_bounds", CheckLayer.CROSS_CHECK,
            f"{len(bad_anchors)} room label anchor(s) invalid or out of bounds",
            evidence={"offenders": bad_anchors, "bounds": bounds},
        )
    else:
        rep.add_pass(
            "reading.room_label_anchors_in_bounds", CheckLayer.CROSS_CHECK,
            evidence={"bounds": bounds},
        )


def _ocr_anchors_in_bounds(rep: CheckReport, view: ReadingView) -> None:
    """M-3 (r1 / F-4 ③) + B-1 (r3 batchC dispatch §1 BLOCKER): surface OCR/
    annotation anchors whose magnitude is anomalous relative to this view's own
    structural (wall/outline) geometry. O-4 stopped letting OCR anchors expand
    the rendered canvas (the ~3.3e8-px root cause) — but that also deleted the
    ONLY signal of a pixel anchor, so bad data was then silently masked (worse
    than before). This check restores a machine-readable signal. A pixel
    anchor like ``[360, 450]`` on a ~10 m plan is the textbook bad-data shape.
    ⛔ Never clamped, never silently dropped — only surfaced. Empty
    ``ocr_texts`` passes cleanly.

    Reference comes from :func:`_structural_metric_reference` (route (b) of
    B-1 — unit-anomaly, no external root; see that function's docstring for
    why the earlier px-as-metre ``trusted_image_bounds`` mechanism, r2's X-2,
    was retired instead of repaired).

    ⚠️ 2026-08-04 r4 (batch C dispatch §1, user-ratified downgrade): this check
    is ADVISORY ONLY (FLAG, never BLOCK, on every ``run_profile`` — see
    ``schema.disposition`` / ``_OCR_ANCHOR_BLOCK_PROFILES``), not because the
    bad-data shape stopped mattering, but because the median/MAD fence in
    :func:`_structural_metric_reference` is known to misjudge in BOTH
    directions: it can be fooled (a structure written entirely in pixel space
    has no internal outlier to catch — false NEGATIVE), and it can wrongly
    accuse ordinary, legitimate drawings (an everyday 10x8 m closed-polyline
    room or a 60x4 m elongated building — false POSITIVE; see this module's
    test suite for the worked repro of both shapes). Do not "fix" this by
    tightening/loosening the MAD factor or margin — that only trades one
    failure mode for the other on a heuristic that cannot see units. The real
    fix is R1.5: reading only ever writes pixel anchors + a reference to the
    dimension that calibrates them, and metric conversion happens exactly once
    in code — which makes "pixel written where a metre was expected"
    constructionally impossible instead of something to detect after the
    fact."""
    ocr_texts = view.ocr_texts or []
    if not ocr_texts:
        rep.add_pass("reading.ocr_anchors_in_bounds", CheckLayer.CROSS_CHECK)
        return
    bounds = _structural_metric_reference(view)
    bad: list[dict] = []
    if bounds is None:
        bad = [{"index": i, "anchor": _ocr_anchor_repr(t), "reason": "no structural geometry to judge against"}
               for i, t in enumerate(ocr_texts)]
    else:
        xmin, xmax, ymin, ymax = bounds
        for i, t in enumerate(ocr_texts):
            if not isinstance(t, dict):
                bad.append({"index": i, "reason": "ocr entry is not an object"})
                continue
            anchor = t.get("anchor")
            if not (isinstance(anchor, list) and len(anchor) == 2 and _finite(*anchor)):
                bad.append({"index": i, "anchor": anchor, "reason": "anchor not two finite numbers"})
                continue
            x, y = anchor
            if not (xmin - _OCR_ANCHOR_MARGIN_M <= x <= xmax + _OCR_ANCHOR_MARGIN_M
                    and ymin - _OCR_ANCHOR_MARGIN_M <= y <= ymax + _OCR_ANCHOR_MARGIN_M):
                bad.append({"index": i, "anchor": anchor, "text": t.get("text", ""),
                            "reason": "coordinate magnitude anomalous relative to structural geometry"})
    if bad:
        rep.add_fail(
            "reading.ocr_anchors_in_bounds", CheckLayer.CROSS_CHECK,
            f"{len(bad)} ocr anchor(s) invalid or magnitude-anomalous",
            evidence={"offenders": bad, "structural_reference": bounds, "margin_m": _OCR_ANCHOR_MARGIN_M},
        )
    else:
        rep.add_pass("reading.ocr_anchors_in_bounds", CheckLayer.CROSS_CHECK,
                     evidence={"structural_reference": bounds})


def _ocr_anchor_repr(t) -> object:
    """Best-effort anchor for an ocr entry that may not be a dict (e.g. None)."""
    return t.get("anchor") if isinstance(t, dict) else t


def _dimension_endpoints_in_bounds(rep: CheckReport, view: ReadingView) -> None:
    """X-1 (r2 batchC dispatch §1) + B-1 (r3 batchC dispatch §1 BLOCKER):
    surface dimension endpoints (``from``/``to``) whose magnitude is anomalous
    relative to this view's own structural (wall/outline) geometry — the same
    bad-data shape M-3 (``_ocr_anchors_in_bounds``) already catches for OCR
    anchors. N-3's adaptive canvas scale (render_vector_to_png) stopped
    RAISING when a dimension endpoint is written in pixel space (it downscales
    instead of blowing up to ~3.3e8 px) — that deleted the last machine-
    readable signal of this failure mode, since gate① never checked dimension
    endpoints directly; it only ever inherited the renderer's crash. This
    restores a machine-readable signal, independent of the renderer. ⛔ Never
    clamped, never silently dropped — only surfaced. Empty ``dimensions``
    passes cleanly.

    Reference comes from :func:`_structural_metric_reference`, which is
    computed from STROKES ONLY — a bad dimension endpoint can never inflate
    the very reference it is checked against (dimensions never enter that
    function's point set at all, so there is no ``exclude_dimensions`` flag to
    thread through any more).

    ⚠️ 2026-08-04 r4 (batch C dispatch §1, user-ratified downgrade): ADVISORY
    ONLY (FLAG, never BLOCK, on every ``run_profile``) — same fence, same
    known false-negative (uniformly pixel-space structure) / false-positive
    (ordinary closed-polyline room, elongated building) failure modes as
    ``_ocr_anchors_in_bounds`` above; see that function's docstring for the
    full account. Structural fix is R1.5, not a tighter heuristic here."""
    dims = view.dimensions or []
    if not dims:
        rep.add_pass(DIMENSION_ENDPOINT_BOUNDS_CHECK_ID, CheckLayer.CROSS_CHECK)
        return
    bounds = _structural_metric_reference(view)
    bad: list[dict] = []
    if bounds is None:
        for d in dims:
            for field, pt in (("from", d.from_pt), ("to", d.to)):
                if pt is not None:
                    bad.append({"id": d.id, "field": field, "point": pt, "reason": "no structural geometry to judge against"})
    else:
        xmin, xmax, ymin, ymax = bounds
        for d in dims:
            for field, pt in (("from", d.from_pt), ("to", d.to)):
                if pt is None:
                    continue
                if not (isinstance(pt, list) and len(pt) == 2 and _finite(*pt)):
                    bad.append({"id": d.id, "field": field, "point": pt, "reason": "point not two finite numbers"})
                    continue
                x, y = pt
                if not (xmin - _OCR_ANCHOR_MARGIN_M <= x <= xmax + _OCR_ANCHOR_MARGIN_M
                        and ymin - _OCR_ANCHOR_MARGIN_M <= y <= ymax + _OCR_ANCHOR_MARGIN_M):
                    bad.append({"id": d.id, "field": field, "point": pt,
                                "reason": "coordinate magnitude anomalous relative to structural geometry"})
    if bad:
        rep.add_fail(
            DIMENSION_ENDPOINT_BOUNDS_CHECK_ID, CheckLayer.CROSS_CHECK,
            f"{len(bad)} dimension endpoint(s) invalid or magnitude-anomalous",
            evidence={"offenders": bad, "structural_reference": bounds, "margin_m": _OCR_ANCHOR_MARGIN_M},
        )
    else:
        rep.add_pass(DIMENSION_ENDPOINT_BOUNDS_CHECK_ID, CheckLayer.CROSS_CHECK,
                     evidence={"structural_reference": bounds})


def _structural_metric_reference(view: ReadingView) -> tuple[float, float, float, float] | None:
    """B-1 (r3 batchC dispatch §1 BLOCKER, route (b) — unit-anomaly, no
    external root): the dimensionally-safe replacement for the retired r2 X-2
    mechanism (``resolve_view_pixel_bounds`` + ``trusted_image_bounds``),
    which compared a METRE reading coordinate against a PIXEL bound (the
    case_data source image's real width/height in px). A ~10-20 m building's
    source image is 790-3000 px wide, so virtually any garbage metre-scale
    value up to that pixel count — including the exact repro payload
    ``[360, 450]`` — sailed straight through: the regression gate blocked
    nothing on real case_data images, only on the retired test suite's 2x2 px
    synthetic fixture. See sol's 2026-08-04 batch C r2 review §2 for the full
    repro.

    Route (a) — deriving a metre bound from a pixel-per-metre ratio (e.g. the
    view's own ``scale_origin`` / dimension calibration) — was rejected: that
    ratio is itself written by the party being checked (``scale_origin`` is a
    plain product-declared dict, see ``_plan_scale_origin``; no other
    pixel-per-metre source exists anywhere in this repository — grepped for
    print/paper-scale case metadata, found none), which fails
    decision_log.md §5.14's "the judged party cannot write the basis" test
    exactly the way the original bug did.

    Route (b), implemented here, asks a dimensionally-consistent question
    entirely within metre space: is this coordinate's magnitude sane relative
    to the SAME view's own already-established structural geometry? This is
    an established pattern in this file already (``_chain_closure``,
    ``_stroke_dimension_consistency`` are both internal cross-consistency
    checks over the same submission, not comparisons to a frozen external
    fact) — it does not need decision_log §5.14's "second, pre-existing,
    unwritable record" because it is not freezing a value at all; it is a
    fixed, gate①-owned ALGORITHM (which the checked party cannot write —
    it lives in this module, not in the submission) applied to the party's
    own data. What the party CANNOT do is defeat it by writing one bad
    coordinate: to slip a pixel-scale value past this fence, the whole
    submission's stroke geometry would have to be uniformly pixel-scale too,
    at which point ``_chain_closure``/``_dimension_derived_refs`` and the
    plan/elevation dimension-chain checks would very likely also fire on the
    same submission (the injected value would no longer be an isolated
    anomaly, it would be systemic).

    Uses STROKES ONLY (line/rect/polyline geometry) — never OCR anchors, never
    dimension endpoints, never the product-declared ``image_bounds`` escape
    hatch (``_explicit_image_bounds`` is not consulted here) — because those
    are exactly the fields these two checks judge; consulting them would let
    the party being checked inflate its own yardstick. This is the same
    concern the r1/r2 "three inflation tricks" test targeted, and it is still
    resisted: dropping the product-declared ``image_bounds`` field and
    dimension endpoints from the point set entirely defeats two of the three
    tricks by construction (they are never read); the third (an added stray
    stroke reaching toward the bad anchor) is resisted by using MEDIAN +
    MEDIAN ABSOLUTE DEVIATION (MAD), not raw min/max, as the base statistic —
    a robust statistic barely moves when one extra point is injected among
    several genuine ones (a raw min/max bounding box has no such property: one
    extra point can move a max arbitrarily far). Worked example (batch C r3
    execution log has the full numeric trace): a 2-stroke base building
    (points (0,0),(10,0),(0,8),(10,8)) plus one injected stroke reaching to
    (400,500) still yields median_x=5, MAD_x=5 — unchanged from the
    non-injected case — because a single outlier among several points does
    not move the median.

    Returns ``None`` when the view has no stroke geometry at all (degenerate/
    empty submission) — callers then fail closed (every OCR anchor / dimension
    endpoint is flagged "no structural geometry to judge against"), the same
    fail-closed posture the pre-B-1 code had for its own "no bounds" case.

    ⚠️ 2026-08-04 r4 (batch C dispatch §1, user-ratified downgrade — KNOWN
    LIMITATIONS, written on purpose so this does not quietly become "the
    check that claims to guard something it can't"):
      - False NEGATIVE: if the whole view's stroke geometry is itself written
        in pixel space (not just one bad coordinate), the median/MAD fence is
        computed FROM that pixel-scale geometry, so a pixel-scale OCR anchor
        or dimension endpoint is no longer an outlier relative to it and
        passes clean. The worked-example resistance to the "stray stroke"
        trick above only holds when the REST of the geometry is genuinely
        metre-scale.
      - False POSITIVE: the median can degenerate on ordinary, everyday
        shapes. A closed polyline that repeats its start point as its last
        point (a common "closed room outline" encoding) pulls the median
        toward that repeated corner instead of the room's centre — e.g. a
        legitimate 10x8 m room ((0,0),(10,0),(10,8),(0,8),(0,0)) yields
        median_x=0 (not 5) and a collapsed MAD, fencing METRE-SCALE ANNOTATIONS
        INSIDE THE SAME ROOM outside the tolerated band. The same collapse
        hits a 60x4 m elongated building. Both shapes are exercised as real
        locks in tests/test_checks_reading_correction.py.
      - Route (a) (a pixel-per-metre ratio from ``scale_origin``) was already
        rejected (see the paragraph above) because the judged party writes
        that ratio; route (b) here is dependency-free but, as the two failure
        modes above show, is not a reliable arbiter either — it is a
        heuristic, not a proof.
      ⇒ Both consumers (``_ocr_anchors_in_bounds`` /
      ``_dimension_endpoints_in_bounds``) treat this fence as ADVISORY ONLY
      (disposition FLAG, never BLOCK — see schema.py) as of r4. Do not re-BLOCK
      on this fence without first fixing the mechanism itself. The intended
      real fix is R1.5 (reading writes pixel anchors + a referenced dimension
      only; metric conversion happens exactly once, in code — eliminating the
      "is this coordinate secretly in the wrong unit" question by
      construction instead of guessing at it statistically after the fact)."""
    xs: list[float] = []
    ys: list[float] = []

    def add_pt(pt) -> None:
        if isinstance(pt, list) and len(pt) >= 2 and _finite(pt[0], pt[1]):
            xs.append(float(pt[0]))
            ys.append(float(pt[1]))

    for stroke in view.strokes:
        g = stroke.geometry or {}
        kind = g.get("kind")
        if kind == "line":
            add_pt(g.get("p1"))
            add_pt(g.get("p2"))
        elif kind == "rect":
            xr, yr = g.get("x_range_m"), g.get("y_range_m")
            if isinstance(xr, list) and isinstance(yr, list) and len(xr) >= 2 and len(yr) >= 2:
                add_pt([xr[0], yr[0]])
                add_pt([xr[1], yr[1]])
        elif kind == "polyline":
            for pt in g.get("points", []):
                add_pt(pt)

    if not xs or not ys:
        return None

    def _median(vals: list[float]) -> float:
        s = sorted(vals)
        n = len(s)
        mid = n // 2
        return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0

    def _half_width(vals: list[float], center: float) -> float:
        mad = _median([abs(v - center) for v in vals])
        return max(mad * _UNIT_ANOMALY_MAD_FACTOR, _UNIT_ANOMALY_FLOOR_M)

    mx, my = _median(xs), _median(ys)
    hx, hy = _half_width(xs, mx), _half_width(ys, my)
    return mx - hx, mx + hx, my - hy, my + hy


def _image_bounds(view: ReadingView) -> tuple[float, float, float, float] | None:
    """Product-derivable bounds: declared ``image_bounds`` extra field, else
    bounds derived from the view's own strokes + dimension endpoints combined.

    B-1 (r3 batchC dispatch §1 BLOCKER): the r2 X-2 ``trusted_bounds``
    parameter this function used to accept was retired — it compared a METRE
    reading coordinate against the case_data source image's real PIXEL size, a
    dimensional mismatch that let the exact repro payload ``[360, 450]``
    through on every real (790-3000 px) case_data image. The two callers that
    needed a dimensionally-safe, self-inflation-resistant reference
    (``_ocr_anchors_in_bounds`` / ``_dimension_endpoints_in_bounds``) now use
    :func:`_structural_metric_reference` instead (route (b) — internal
    unit-anomaly, no external root; see its docstring for the full
    justification). This function is unchanged from its pre-X-2 form and is
    now used only by ``reading.room_label_anchors_in_bounds`` (a different,
    lower-stakes check, not in scope of B-1 — it was never fed
    ``trusted_bounds`` even under r2's X-2, so its behaviour here is
    unaffected by this fix)."""
    explicit = _explicit_image_bounds(view)
    if explicit is not None:
        return explicit

    xs: list[float] = []
    ys: list[float] = []

    def add_pt(pt) -> None:
        if isinstance(pt, list) and len(pt) >= 2 and _finite(pt[0], pt[1]):
            xs.append(float(pt[0]))
            ys.append(float(pt[1]))

    for stroke in view.strokes:
        g = stroke.geometry or {}
        kind = g.get("kind")
        if kind == "line":
            add_pt(g.get("p1"))
            add_pt(g.get("p2"))
        elif kind == "rect":
            xr, yr = g.get("x_range_m"), g.get("y_range_m")
            if isinstance(xr, list) and isinstance(yr, list) and len(xr) >= 2 and len(yr) >= 2:
                add_pt([xr[0], yr[0]])
                add_pt([xr[1], yr[1]])
        elif kind == "polyline":
            for pt in g.get("points", []):
                add_pt(pt)
    for dim in view.dimensions:
        add_pt(dim.from_pt)
        add_pt(dim.to)

    if not xs or not ys:
        return None
    return min(xs), max(xs), min(ys), max(ys)


def _explicit_image_bounds(view: ReadingView) -> tuple[float, float, float, float] | None:
    extra = getattr(view, "__pydantic_extra__", None) or {}
    bounds = extra.get("image_bounds")
    if isinstance(bounds, dict):
        xr, yr = bounds.get("x"), bounds.get("y")
        if (
            isinstance(xr, list)
            and isinstance(yr, list)
            and len(xr) >= 2
            and len(yr) >= 2
            and _finite(xr[0], xr[1], yr[0], yr[1])
        ):
            return float(xr[0]), float(xr[1]), float(yr[0]), float(yr[1])
    if (
        isinstance(bounds, list)
        and len(bounds) == 2
        and all(isinstance(pt, list) and len(pt) >= 2 for pt in bounds)
        and _finite(bounds[0][0], bounds[0][1], bounds[1][0], bounds[1][1])
    ):
        xs = [float(bounds[0][0]), float(bounds[1][0])]
        ys = [float(bounds[0][1]), float(bounds[1][1])]
        return min(xs), max(xs), min(ys), max(ys)

    size = extra.get("image_size") or extra.get("image_dimensions")
    if isinstance(size, list) and len(size) >= 2 and _finite(size[0], size[1]):
        return 0.0, float(size[0]), 0.0, float(size[1])
    width, height = extra.get("image_width"), extra.get("image_height")
    if _finite(width, height):
        return 0.0, float(width), 0.0, float(height)
    return None


def _pen_kind(rep: CheckReport, view: ReadingView) -> None:
    legal = _legal_pens(view.image_kind)
    bad: list[dict] = []
    for s in view.strokes:
        if legal is not None and s.pen not in legal:
            bad.append({"id": s.id, "pen": s.pen, "reason": "illegal pen for image_kind"})
            continue
        kind = (s.geometry or {}).get("kind")
        if kind not in _GEOMETRY_KINDS:
            reason = "missing geometry kind" if kind is None else "unknown geometry kind"
            bad.append({"id": s.id, "kind": kind, "reason": reason})
    if bad:
        rep.add_fail(
            "reading.pen_kind_valid", CheckLayer.INVARIANT,
            f"{len(bad)} stroke(s) with illegal pen/kind for image_kind="
            f"'{view.image_kind}'",
            evidence={"offenders": bad, "legal_pens": sorted(legal) if legal else None},
        )
    else:
        rep.add_pass("reading.pen_kind_valid", CheckLayer.INVARIANT)


def _no_topology_fields(rep: CheckReport, view: ReadingView) -> None:
    bad = []
    for s in view.strokes:
        extra = getattr(s, "__pydantic_extra__", None) or {}
        hit = _FORBIDDEN_STROKE_KEYS & set(extra)
        hit |= _FORBIDDEN_STROKE_KEYS & set(s.geometry or {})
        if hit:
            bad.append({"id": s.id, "fields": sorted(hit)})
    if bad:
        rep.add_fail(
            "reading.no_topology_fields", CheckLayer.INVARIANT,
            "stroke(s) carry topology/world fields (belong to 1_correction)",
            evidence={"offenders": bad},
        )
    else:
        rep.add_pass("reading.no_topology_fields", CheckLayer.INVARIANT)


def _geometry_wellformed(rep: CheckReport, view: ReadingView) -> None:
    bad = []
    for s in view.strokes:
        g = s.geometry or {}
        kind = g.get("kind")
        if kind == "line":
            p1, p2 = g.get("p1"), g.get("p2")
            if not (p1 and p2 and _finite(*p1, *p2)):
                bad.append({"id": s.id, "reason": "non-finite line endpoints"})
            elif math.dist(p1[:2], p2[:2]) < _MIN_EXTENT:
                bad.append({"id": s.id, "reason": "degenerate line (zero length)"})
        elif kind == "rect":
            xr, yr = g.get("x_range_m"), g.get("y_range_m")
            if not (xr and yr and _finite(*xr, *yr)):
                bad.append({"id": s.id, "reason": "non-finite rect range"})
            elif abs(xr[1] - xr[0]) < _MIN_EXTENT or abs(yr[1] - yr[0]) < _MIN_EXTENT:
                # EITHER collapsed axis makes a rectangle degenerate (zero area).
                bad.append({"id": s.id, "reason": "degenerate rect (collapsed axis)"})
        elif kind == "polyline":
            pts = g.get("points")
            if not (
                isinstance(pts, list)
                and len(pts) >= 2
                and all(isinstance(pt, list) and len(pt) >= 2 and _finite(*pt[:2]) for pt in pts)
            ):
                bad.append({"id": s.id, "reason": "malformed polyline points"})
            elif sum(math.dist(pts[i - 1][:2], pts[i][:2]) for i in range(1, len(pts))) < _MIN_EXTENT:
                bad.append({"id": s.id, "reason": "degenerate polyline (zero length)"})
    if bad:
        rep.add_fail(
            "reading.nondegenerate_geometry", CheckLayer.INVARIANT,
            f"{len(bad)} stroke(s) with non-finite or degenerate geometry",
            evidence={"offenders": bad},
        )
    else:
        rep.add_pass("reading.nondegenerate_geometry", CheckLayer.INVARIANT)


def _dimensions_wellformed(rep: CheckReport, view: ReadingView) -> None:
    unparseable, axis_bad = [], []
    for d in view.dimensions:
        val = d.value_m if d.value_m is not None else parse_value_m(d.text_verbatim or d.text)
        if val is None:
            unparseable.append(d.id)
        # axis-endpoint consistency: a dimension's axis must match the axis along
        # which its endpoints actually differ.
        if d.axis and d.from_pt and d.to:
            # A present-but-malformed endpoint (fewer than 2 components, or a
            # non-finite one) is an OFFENDER, not a crash: the schema types
            # from/to as plain ``list[float]``, so a reader that emits
            # ``"to": [11.36]`` loads fine and used to raise IndexError two
            # lines below — a gate that dies instead of reporting. Its sibling
            # ``_strokes_nondegenerate`` already guards ``len(pt) >= 2``; this
            # is the same guard on the dimension path. Endpoints that are
            # ABSENT stay skipped, exactly as before.
            if not (len(d.from_pt) >= 2 and len(d.to) >= 2
                    and _finite(*d.from_pt[:2], *d.to[:2])):
                axis_bad.append({
                    "id": d.id, "axis": d.axis, "reason": "malformed endpoints",
                    "from": list(d.from_pt), "to": list(d.to),
                })
                continue
            dx = abs(d.to[0] - d.from_pt[0])
            dy = abs(d.to[1] - d.from_pt[1])
            if d.axis == "x" and dx < dy:
                axis_bad.append({"id": d.id, "axis": "x", "dx": dx, "dy": dy})
            elif d.axis == "y" and dy < dx:
                axis_bad.append({"id": d.id, "axis": "y", "dx": dx, "dy": dy})
    if unparseable:
        rep.add_fail(
            "reading.dimension_parseable", CheckLayer.INVARIANT,
            f"{len(unparseable)} dimension(s) with no parseable value",
            evidence={"ids": unparseable},
        )
    else:
        rep.add_pass("reading.dimension_parseable", CheckLayer.INVARIANT)
    if axis_bad:
        rep.add_fail(
            "reading.axis_endpoint_consistent", CheckLayer.INVARIANT,
            f"{len(axis_bad)} dimension(s) with malformed endpoints or an axis "
            f"that disagrees with them",
            evidence={"offenders": axis_bad},
        )
    else:
        rep.add_pass("reading.axis_endpoint_consistent", CheckLayer.INVARIANT)


def _raw_field_presence(rep: CheckReport, meta: dict) -> None:
    evidence = _evidence_meta(meta)
    raw_has_uncaptured = meta.get("raw_has_uncaptured")
    if raw_has_uncaptured is False:
        rep.add_fail(
            "reading.raw_field_presence",
            CheckLayer.CROSS_CHECK,
            "raw reading JSON omitted uncaptured before schema/default migration",
            evidence=evidence,
        )
    else:
        rep.add_pass("reading.raw_field_presence", CheckLayer.CROSS_CHECK, evidence=evidence)


def _dimensioned_view_evidence(rep: CheckReport, view: ReadingView, meta: dict) -> None:
    evidence = _evidence_meta(meta)
    state = meta.get("dimensioned_state", "legacy_default")
    if state != "declared_true":
        # declared_false / unknown / legacy_default are all N/A for the dimension
        # evidence checks, but the 4-state reason stays machine-visible in
        # evidence.dimensioned_state (ruling追加约束 #1: never fold back to a
        # bool — unknown ≠ declared_false ≠ legacy_default end-to-end).
        reason = {
            "declared_false": "view is declared not dimensioned",
            "unknown": "view dimensioned applicability is unknown (not declared)",
            "legacy_default": "view is not declared dimensioned (legacy default)",
        }.get(state, "view is not declared dimensioned")
        rep.add(
            "reading.dimensions_present",
            CheckStatus.NOT_APPLICABLE,
            CheckLayer.CROSS_CHECK,
            message=reason,
            evidence=evidence,
        )
        rep.add(
            "reading.dimension_p1a_fields",
            CheckStatus.NOT_APPLICABLE,
            CheckLayer.CROSS_CHECK,
            message=reason,
            evidence=evidence,
        )
        return

    if not view.dimensions:
        rep.add_fail(
            "reading.dimensions_present",
            CheckLayer.CROSS_CHECK,
            "dimensioned view has empty dimensions[]",
            evidence={**evidence, "dimension_count": 0},
        )
        rep.add(
            "reading.dimension_p1a_fields",
            CheckStatus.NOT_APPLICABLE,
            CheckLayer.CROSS_CHECK,
            message="no dimensions to inspect",
            evidence={**evidence, "dimension_count": 0},
        )
        return
    rep.add_pass(
        "reading.dimensions_present",
        CheckLayer.CROSS_CHECK,
        evidence={**evidence, "dimension_count": len(view.dimensions)},
    )

    if meta.get("legacy_migrated"):
        rep.add(
            "reading.dimension_p1a_fields",
            CheckStatus.NOT_APPLICABLE,
            CheckLayer.CROSS_CHECK,
            message="legacy-migrated dimensions are grandfathered",
            evidence={**evidence, "dimension_count": len(view.dimensions)},
        )
        return

    offenders = []
    for d in view.dimensions:
        missing = []
        if not d.text_verbatim:
            missing.append("text_verbatim")
        if d.value_m is None:
            missing.append("value_m")
        if not d.chain_id:
            missing.append("chain_id")
        if d.role is None:
            missing.append("role")
        if d.order is None:
            missing.append("order")
        if missing:
            offenders.append({"id": d.id, "missing": missing})
    if offenders:
        rep.add_fail(
            "reading.dimension_p1a_fields",
            CheckLayer.CROSS_CHECK,
            f"{len(offenders)} dimension(s) missing required P1a fields",
            evidence={**evidence, "offenders": offenders},
        )
    else:
        rep.add_pass(
            "reading.dimension_p1a_fields",
            CheckLayer.CROSS_CHECK,
            evidence={**evidence, "dimension_count": len(view.dimensions)},
        )


def _door_heal_traced(rep: CheckReport, view: ReadingView) -> None:
    """Flag a healed door opening (a wall stroke whose note says it healed a door)
    that leaves no matching trace in ``uncaptured`` — the old "acknowledged skip
    vs silent loss" discipline (guide §2.1 guardrail 4). Advisory: a missing audit
    note does not corrupt geometry, so this surfaces to gate② rather than blocks.
    Only fires when a heal happened, so a clean drawing with ``uncaptured=[]`` is
    untouched."""
    # Match the guide §2.1 convention phrase "healed door opening at <position>"
    # (precise — avoids false-flagging a note like "door not healed").
    healed = [
        s for s in view.strokes
        if isinstance(getattr(s, "note", None), str) and "healed door" in s.note.lower()
    ]
    if not healed:
        rep.add("reading.door_heal_traced", CheckStatus.NOT_APPLICABLE,
                CheckLayer.CROSS_CHECK, message="no healed door openings")
        return
    pooled = _uncaptured_list(view) or []
    heal_traces = [e for e in pooled if "heal" in str(e).lower()]
    if not heal_traces:
        rep.add_fail(
            "reading.door_heal_traced", CheckLayer.CROSS_CHECK,
            f"{len(healed)} wall stroke(s) healed a door opening but `uncaptured` "
            "records no heal trace (silent-loss risk; see guide §2.1)",
            evidence={
                "healed_stroke_ids": [s.id for s in healed],
                "uncaptured_entries": len(pooled),
            },
        )
    else:
        rep.add_pass(
            "reading.door_heal_traced", CheckLayer.CROSS_CHECK,
            evidence={
                "healed_stroke_ids": [s.id for s in healed],
                "heal_traces": len(heal_traces),
            },
        )


def _facade_fields(rep: CheckReport, view: ReadingView) -> None:
    if (view.image_kind or "").lower() != "elevation":
        rep.add("reading.facade_fields", CheckStatus.NOT_APPLICABLE, CheckLayer.INVARIANT,
                message="not an elevation")
        return
    f = view.facade
    if f is None or f.view_facade is None:
        rep.add_fail(
            "reading.facade_fields", CheckLayer.INVARIANT,
            "elevation missing image-local facade orientation (view_facade)",
        )
    else:
        rep.add_pass("reading.facade_fields", CheckLayer.INVARIANT,
                     evidence={"view_facade": f.view_facade, "mirrored": str(f.mirrored)})


def _strict_number(value) -> bool:
    """A value gate② can actually use as a coordinate: a real finite number.

    Mirrors the consumer's own strictness — ``bool`` is an ``int`` subclass in
    Python but is not a coordinate, and ``"0.0"`` / ``None`` / ``NaN`` are not
    either. A present-but-unusable value must be treated exactly like a missing
    one, otherwise the gate passes an artifact the scorer will still reject.
    """
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _plan_scale_origin(rep: CheckReport, view: ReadingView) -> None:
    """Plan views must declare a usable ``scale_origin`` (guide.md §1/§2/§6).

    gate② rebuilds the plan-local → world frame from ``scale_origin.world_x_m``
    + ``world_y_m`` and from nothing else; when either is absent or non-numeric
    it emits ``plan_frame_unavailable`` and the entire plan channel scores zero
    no matter how accurate the tracing was — a silent zero that reads exactly
    like a bad drawing. So this refuses such a reading outright under the
    acceptance profiles (``golden`` / ``regression``) while ``exploratory`` /
    ``dev`` only flag it, keeping historical artifacts that predate the
    instruction contract replayable (policy lives in
    :func:`src.validator.checks.schema.disposition`).

    The free-prose ``note`` field is deliberately not evidence of anything: it
    is never read by the consumer.
    """
    if (view.image_kind or "").lower() != "plan":
        rep.add(
            PLAN_FRAME_CHECK_ID, CheckStatus.NOT_APPLICABLE, CheckLayer.CROSS_CHECK,
            message="not a plan",
        )
        return

    origin = view.scale_origin
    if not isinstance(origin, dict):
        rep.add_fail(
            PLAN_FRAME_CHECK_ID, CheckLayer.CROSS_CHECK,
            "plan view declares no scale_origin object; its local→world frame "
            "cannot be rebuilt and the plan channel would score zero",
            evidence={
                "scale_origin_present": origin is not None,
                "scale_origin_type": type(origin).__name__,
                "unusable_fields": ["world_x_m", "world_y_m"],
            },
        )
        return

    unusable = [
        key for key in ("world_x_m", "world_y_m")
        if not _strict_number(origin.get(key))
    ]
    if unusable:
        rep.add_fail(
            PLAN_FRAME_CHECK_ID, CheckLayer.CROSS_CHECK,
            f"plan scale_origin has no usable {' + '.join(unusable)}; its "
            "local→world frame cannot be rebuilt and the plan channel would "
            "score zero",
            evidence={
                "scale_origin_present": True,
                "scale_origin_type": "dict",
                "unusable_fields": unusable,
                "values": {key: repr(origin.get(key)) for key in unusable},
            },
        )
        return

    rep.add_pass(
        PLAN_FRAME_CHECK_ID, CheckLayer.CROSS_CHECK,
        evidence={
            "world_x_m": float(origin["world_x_m"]),
            "world_y_m": float(origin["world_y_m"]),
        },
    )


def _chain_closure(rep: CheckReport, view: ReadingView, meta: dict) -> None:
    """Σ segment values == overall/baseline value, per (chain_id, axis).

    Limitation: arithmetic closure catches many but not all misreads; a
    self-consistent wrong read still closes (see
    tests/test_checks_reading_correction.py::test_self_consistent_wrong_dimension_passes_linter).
    """
    evidence = _evidence_meta(meta)
    missing_chain_id = []
    missing_axis = []
    chains: dict[tuple[str, str], dict] = {}
    for d in view.dimensions:
        if not d.chain_id:
            if meta.get("dimensioned"):
                missing_chain_id.append(d.id)
            continue
        if d.axis not in ("x", "y"):
            if meta.get("dimensioned"):
                missing_axis.append(d.id)
            continue
        c = chains.setdefault(
            (d.chain_id, d.axis),
            {"overall": [], "segments": [], "dimension_ids": []},
        )
        c["dimension_ids"].append(d.id)
        val = d.value_m if d.value_m is not None else parse_value_m(d.text_verbatim or d.text)
        if val is None:
            continue
        if d.role and d.role.value in ("overall", "baseline"):
            c["overall"].append({"id": d.id, "value": val, "role": d.role.value})
        elif d.role and d.role.value == "segment":
            c["segments"].append({"id": d.id, "value": val, "order": d.order})
    if not chains:
        if meta.get("dimensioned") and view.dimensions:
            rep.add_fail(
                "reading.dimension_chain_closure",
                CheckLayer.CROSS_CHECK,
                "dimensioned view has no complete chain_id+axis dimension groups",
                evidence={
                    **evidence,
                    "missing_chain_id": missing_chain_id,
                    "missing_axis": missing_axis,
                },
            )
            return
        rep.add("reading.dimension_chain_closure", CheckStatus.NOT_APPLICABLE,
                CheckLayer.CROSS_CHECK, message="no chain_id-tagged dimensions",
                evidence=evidence)
        return
    mismatches = []
    incomplete = []
    for (cid, axis), c in chains.items():
        if not c["overall"] or not c["segments"]:
            incomplete.append({
                "chain_id": cid,
                "axis": axis,
                "overall_count": len(c["overall"]),
                "segment_count": len(c["segments"]),
                "dimension_ids": c["dimension_ids"],
            })
            continue
        unordered = [seg["id"] for seg in c["segments"] if seg["order"] is None]
        if unordered:
            incomplete.append({
                "chain_id": cid,
                "axis": axis,
                "reason": "segment missing order",
                "segment_ids": unordered,
            })
            continue
        seg_sum = sum(seg["value"] for seg in c["segments"])
        overall = c["overall"][0]
        if abs(seg_sum - overall["value"]) > DIMCHAIN_CLOSE_TOL_M:
            mismatches.append(
                {
                    "chain_id": cid,
                    "axis": axis,
                    "overall": overall["value"],
                    "overall_id": overall["id"],
                    "segment_sum": seg_sum,
                    "segment_ids": [seg["id"] for seg in c["segments"]],
                }
            )
    if missing_chain_id or missing_axis or incomplete or mismatches:
        rep.add_fail(
            "reading.dimension_chain_closure", CheckLayer.CROSS_CHECK,
            "dimension chain evidence is missing, incomplete, or non-closing",
            evidence={
                **evidence,
                "missing_chain_id": missing_chain_id,
                "missing_axis": missing_axis,
                "incomplete": incomplete,
                "mismatches": mismatches,
            },
        )
    else:
        rep.add_pass("reading.dimension_chain_closure", CheckLayer.CROSS_CHECK,
                     evidence={**evidence, "chains_checked": len(chains)})


def _dimension_derived_refs(rep: CheckReport, view: ReadingView, meta: dict) -> None:
    dim_ids = {d.id for d in view.dimensions}
    offenders = []
    checked = 0
    for s in view.strokes:
        if getattr(s, "provenance", None) != "dimension_derived":
            continue
        checked += 1
        refs = list(getattr(s, "dimension_refs", []) or [])
        missing_refs = [ref for ref in refs if ref not in dim_ids]
        if not refs or missing_refs:
            offenders.append({
                "stroke_id": s.id,
                "dimension_refs": refs,
                "missing_refs": missing_refs,
            })
    evidence = {**_evidence_meta(meta), "strokes_checked": checked}
    if offenders:
        rep.add_fail(
            "reading.dimension_derived_refs",
            CheckLayer.CROSS_CHECK,
            f"{len(offenders)} dimension_derived stroke(s) have empty or unresolved dimension_refs",
            evidence={**evidence, "offenders": offenders},
        )
    elif checked:
        rep.add_pass("reading.dimension_derived_refs", CheckLayer.CROSS_CHECK, evidence=evidence)
    else:
        rep.add(
            "reading.dimension_derived_refs",
            CheckStatus.NOT_APPLICABLE,
            CheckLayer.CROSS_CHECK,
            message="no dimension_derived strokes",
            evidence=evidence,
        )


def _stroke_dimension_consistency(rep: CheckReport, view: ReadingView, meta: dict) -> None:
    """Report provenance coverage and flag plan-wall coordinates that sit exactly
    on dimension-chain cumulative positions. This is advisory only: a real wall can
    be dimensioned, so J0 must verify the rendered/source image evidence."""
    structural = [s for s in view.strokes if s.pen in _PROVENANCE_PENS]
    with_provenance = [
        s for s in structural if getattr(s, "provenance", None) is not None
    ]
    if not structural or not with_provenance:
        mode = "legacy"
    elif len(with_provenance) == len(structural):
        mode = "full"
    else:
        mode = "partial"
    provenance_evidence = {
        **_evidence_meta(meta),
        "provenance_mode": mode,
        "structural_strokes": len(structural),
        "with_provenance": len(with_provenance),
    }
    if mode in ("legacy", "partial"):
        rep.add_fail(
            "reading.stroke_provenance_coverage",
            CheckLayer.CROSS_CHECK,
            "structural strokes lack full provenance coverage",
            evidence=provenance_evidence,
        )
    else:
        rep.add_pass(
            "reading.stroke_provenance_coverage",
            CheckLayer.CROSS_CHECK,
            evidence=provenance_evidence,
        )

    if (view.image_kind or "").lower() != "plan":
        rep.add(
            "reading.partition_on_window_jamb",
            CheckStatus.NOT_APPLICABLE,
            CheckLayer.CROSS_CHECK,
            message="not a plan",
        )
        rep.add(
            "reading.stroke_dimension_consistency",
            CheckStatus.NOT_APPLICABLE,
            CheckLayer.CROSS_CHECK,
            message="not a plan",
        )
        return

    dim_positions = _dimension_chain_positions(view)
    bounds = _wall_bounds(view)
    _partition_on_window_jamb(rep, view, dim_positions, bounds)

    if not dim_positions.get("x") and not dim_positions.get("y"):
        rep.add(
            "reading.stroke_dimension_consistency",
            CheckStatus.NOT_APPLICABLE,
            CheckLayer.CROSS_CHECK,
            message="no chain_id-tagged x/y dimension cumulative positions",
        )
        return

    offenders = []
    for stroke in view.strokes:
        if stroke.pen != "wall":
            continue
        if getattr(stroke, "provenance", None) == "dimension_derived":
            continue
        axis_line = _wall_axis_line(stroke)
        if axis_line is None:
            continue
        axis, coord, p1, p2 = axis_line
        if _is_perimeter_axis(axis, coord, bounds):
            continue
        matches = [
            pos for pos in dim_positions.get(axis, [])
            if abs(pos["coord"] - coord) <= _OUTPUT_PRECISION_M
        ]
        if not matches:
            continue
        joined = _wall_join_evidence(view, stroke, p1, p2)
        offenders.append({
            "stroke_id": stroke.id,
            "axis": axis,
            "coord_m": coord,
            "matching_dimension_ids": sorted({
                dim_id for pos in matches for dim_id in pos["dimension_ids"]
            }),
            "matching_positions": matches,
            "joins_walls": joined,
            "bounds_rooms_inferable": joined["both_endpoints_join"],
        })

    if offenders:
        rep.add_fail(
            "reading.stroke_dimension_consistency",
            CheckLayer.CROSS_CHECK,
            f"{len(offenders)} plan wall stroke coordinate(s) coincide with "
            "dimension-chain cumulative positions; verify each is a real "
            "room-bounding wall",
            evidence={"tolerance_m": _OUTPUT_PRECISION_M, "offenders": offenders},
        )
    else:
        rep.add_pass(
            "reading.stroke_dimension_consistency",
            CheckLayer.CROSS_CHECK,
            evidence={
                "tolerance_m": _OUTPUT_PRECISION_M,
                "axes_with_chains": sorted(k for k, v in dim_positions.items() if v),
            },
        )


def _dimension_chain_positions(view: ReadingView) -> dict[str, list[dict]]:
    chains: dict[tuple[str, str], list] = {}
    for d in view.dimensions:
        if d.chain_id and d.axis in ("x", "y"):
            chains.setdefault((d.chain_id, d.axis), []).append(d)

    positions: dict[str, list[dict]] = {"x": [], "y": []}
    seen: set[tuple[str, float, tuple[str, ...]]] = set()
    for (chain_id, axis), dims in chains.items():
        ordered = sorted(dims, key=lambda d: (d.order is None, d.order or 0, d.id))
        running = 0.0
        running_ids: list[str] = []
        for d in ordered:
            # Prefer explicit endpoints: they are already cumulative coordinates in
            # the reading frame and avoid guessing a datum.
            endpoint_coords = []
            if d.from_pt and len(d.from_pt) >= 2 and _finite(*d.from_pt[:2]):
                endpoint_coords.append(float(d.from_pt[0 if axis == "x" else 1]))
            if d.to and len(d.to) >= 2 and _finite(*d.to[:2]):
                endpoint_coords.append(float(d.to[0 if axis == "x" else 1]))
            for coord in endpoint_coords:
                _add_dim_position(positions, seen, axis, coord, [d.id], chain_id)

            val = d.value_m if d.value_m is not None else parse_value_m(d.text_verbatim or d.text)
            if val is None:
                continue
            role = d.role.value if d.role is not None else None
            if role == "segment":
                running += float(val)
                running_ids.append(d.id)
                _add_dim_position(positions, seen, axis, running, running_ids, chain_id)
    return positions


def _add_dim_position(
    positions: dict[str, list[dict]],
    seen: set[tuple[str, float, tuple[str, ...]]],
    axis: str,
    coord: float,
    dimension_ids: list[str],
    chain_id: str,
) -> None:
    rounded = round(float(coord), 6)
    ids = tuple(sorted(dimension_ids))
    key = (axis, rounded, ids)
    if key in seen:
        return
    seen.add(key)
    positions[axis].append({
        "coord": rounded,
        "dimension_ids": list(ids),
        "chain_id": chain_id,
    })


def _partition_on_window_jamb(
    rep: CheckReport,
    view: ReadingView,
    dim_positions: dict[str, list[dict]],
    bounds: tuple[float, float, float, float] | None,
) -> None:
    jambs = _window_jamb_positions(view)
    if not jambs.get("x") and not jambs.get("y"):
        rep.add(
            "reading.partition_on_window_jamb",
            CheckStatus.NOT_APPLICABLE,
            CheckLayer.CROSS_CHECK,
            message="no window jamb geometry",
            evidence={"tolerance_m": _WINDOW_JAMB_TOLERANCE_M},
        )
        return

    offenders = []
    for stroke in view.strokes:
        if stroke.pen != "wall":
            continue
        axis_line = _wall_axis_line(stroke)
        if axis_line is None:
            continue
        axis, coord, p1, p2 = axis_line
        if _is_perimeter_axis(axis, coord, bounds):
            continue

        matching_jambs = [
            jamb for jamb in jambs.get(axis, [])
            if abs(jamb["coord_m"] - coord) <= _WINDOW_JAMB_TOLERANCE_M
        ]
        if not matching_jambs:
            continue

        matching_dimensions = [
            pos for pos in dim_positions.get(axis, [])
            if abs(pos["coord"] - coord) <= _WINDOW_JAMB_TOLERANCE_M
        ]
        joins = _wall_join_evidence(view, stroke, p1, p2)

        offenders.append({
            "stroke_id": stroke.id,
            "axis": axis,
            "coord_m": coord,
            "window_jambs": matching_jambs,
            "matching_dimension_positions": matching_dimensions,
            "joins_walls": joins,
        })

    if offenders:
        rep.add_fail(
            "reading.partition_on_window_jamb",
            CheckLayer.CROSS_CHECK,
            f"{len(offenders)} interior wall stroke coordinate(s) coincide with "
            "window jambs; verify each is a real partition rather than a traced jamb",
            evidence={
                "tolerance_m": _WINDOW_JAMB_TOLERANCE_M,
                "offenders": offenders,
            },
        )
    else:
        rep.add_pass(
            "reading.partition_on_window_jamb",
            CheckLayer.CROSS_CHECK,
            evidence={
                "tolerance_m": _WINDOW_JAMB_TOLERANCE_M,
                "window_jamb_count": sum(len(v) for v in jambs.values()),
            },
        )


def _window_jamb_positions(view: ReadingView) -> dict[str, list[dict]]:
    positions: dict[str, list[dict]] = {"x": [], "y": []}
    seen: set[tuple[str, float, str]] = set()

    def add(axis: str, coord: float, stroke_id: str, source: str) -> None:
        rounded = round(float(coord), 6)
        key = (axis, rounded, stroke_id)
        if key in seen:
            return
        seen.add(key)
        positions[axis].append({
            "window_id": stroke_id,
            "coord_m": rounded,
            "source": source,
        })

    for stroke in view.strokes:
        if stroke.pen != "window":
            continue
        g = stroke.geometry or {}
        kind = g.get("kind")
        if kind == "rect":
            xr, yr = g.get("x_range_m"), g.get("y_range_m")
            if isinstance(xr, list) and len(xr) >= 2 and _finite(*xr[:2]):
                add("x", float(xr[0]), stroke.id, "x_range_m[0]")
                add("x", float(xr[1]), stroke.id, "x_range_m[1]")
            if isinstance(yr, list) and len(yr) >= 2 and _finite(*yr[:2]):
                add("y", float(yr[0]), stroke.id, "y_range_m[0]")
                add("y", float(yr[1]), stroke.id, "y_range_m[1]")
        elif kind == "line":
            p1, p2 = g.get("p1"), g.get("p2")
            if not (
                isinstance(p1, list)
                and isinstance(p2, list)
                and len(p1) >= 2
                and len(p2) >= 2
                and _finite(*p1[:2], *p2[:2])
            ):
                continue
            x1, y1 = float(p1[0]), float(p1[1])
            x2, y2 = float(p2[0]), float(p2[1])
            if abs(y1 - y2) <= _OUTPUT_PRECISION_M:
                add("x", x1, stroke.id, "p1.x")
                add("x", x2, stroke.id, "p2.x")
            elif abs(x1 - x2) <= _OUTPUT_PRECISION_M:
                add("y", y1, stroke.id, "p1.y")
                add("y", y2, stroke.id, "p2.y")
    return positions


def _wall_axis_line(stroke) -> tuple[str, float, list[float], list[float]] | None:
    g = stroke.geometry or {}
    if g.get("kind") != "line":
        return None
    p1, p2 = g.get("p1"), g.get("p2")
    if not (isinstance(p1, list) and isinstance(p2, list) and len(p1) >= 2 and len(p2) >= 2):
        return None
    if not _finite(*p1[:2], *p2[:2]):
        return None
    x1, y1 = float(p1[0]), float(p1[1])
    x2, y2 = float(p2[0]), float(p2[1])
    if abs(x1 - x2) <= _OUTPUT_PRECISION_M:
        return "x", round((x1 + x2) / 2, 6), [x1, y1], [x2, y2]
    if abs(y1 - y2) <= _OUTPUT_PRECISION_M:
        return "y", round((y1 + y2) / 2, 6), [x1, y1], [x2, y2]
    return None


def _wall_bounds(view: ReadingView) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for stroke in view.strokes:
        if stroke.pen != "wall":
            continue
        g = stroke.geometry or {}
        if g.get("kind") != "line":
            continue
        p1, p2 = g.get("p1"), g.get("p2")
        if (
            isinstance(p1, list) and isinstance(p2, list)
            and len(p1) >= 2 and len(p2) >= 2
            and _finite(*p1[:2], *p2[:2])
        ):
            xs.extend([float(p1[0]), float(p2[0])])
            ys.extend([float(p1[1]), float(p2[1])])
    if not xs or not ys:
        return None
    return min(xs), max(xs), min(ys), max(ys)


def _is_perimeter_axis(
    axis: str, coord: float, bounds: tuple[float, float, float, float] | None
) -> bool:
    if bounds is None:
        return False
    xmin, xmax, ymin, ymax = bounds
    extremes = (xmin, xmax) if axis == "x" else (ymin, ymax)
    return any(abs(coord - edge) <= _OUTPUT_PRECISION_M for edge in extremes)


def _wall_join_evidence(view: ReadingView, stroke, p1: list[float], p2: list[float]) -> dict:
    joins = [
        _point_joins_other_wall(view, stroke.id, p1),
        _point_joins_other_wall(view, stroke.id, p2),
    ]
    return {
        "p1": joins[0],
        "p2": joins[1],
        "both_endpoints_join": all(joins),
    }


def _point_joins_other_wall(view: ReadingView, own_id: str, pt: list[float]) -> bool:
    for other in view.strokes:
        if other.id == own_id or other.pen != "wall":
            continue
        g = other.geometry or {}
        if g.get("kind") != "line":
            continue
        p1, p2 = g.get("p1"), g.get("p2")
        if not (
            isinstance(p1, list) and isinstance(p2, list)
            and len(p1) >= 2 and len(p2) >= 2
            and _finite(*p1[:2], *p2[:2])
        ):
            continue
        if _point_on_segment(pt, [float(p1[0]), float(p1[1])], [float(p2[0]), float(p2[1])]):
            return True
    return False


def _point_on_segment(pt: list[float], a: list[float], b: list[float]) -> bool:
    px, py = pt
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    seg_len2 = dx * dx + dy * dy
    if seg_len2 <= 0:
        return False
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len2))
    closest = [ax + t * dx, ay + t * dy]
    return math.dist(pt, closest) <= _OUTPUT_PRECISION_M


# ---------------------------------------------------------------------------
# 2026-08-18 · the machine consumer for the cross-axis calibration signal.
#
# Unlike everything above, this one does NOT read the ReadingView — it reads the
# CV evidence sidecars on disk. That is deliberate and is the whole point of the
# check; the rationale (and the measured failure that motivated it) lives on
# CALIBRATION_AXES_CHECK_ID in schema.py. In one line: the reader's own account
# of its calibration was measured to be wrong, so the gate reads the tool's
# output instead of the reader's claim.
# ---------------------------------------------------------------------------

_CALIBRATOR_GLOB = "**/*px_m_calibrator*.json"


def check_calibration_evidence(rep: CheckReport, cv_evidence_root) -> CheckReport:
    """Append the cross-axis calibration verdict to ``rep``, from evidence files.

    ``cv_evidence_root`` is the directory the CV wrapper writes sidecars under
    (staging ``out/cv``); a caller that has no such directory should not call
    this at all rather than pass a bogus path.

    ⚠️ Scope, stated rather than implied: this answers "did any calibration this
    reader ran disagree with itself across axes", NOT "did the reader derive its
    coordinates from a disagreeing calibration". Answering the second needs the
    reading to cite the calibration it used (a `candidate_id` reference), which
    is the R1.5 direction and is not implemented — so a reader that runs a bad
    calibration, then a good one, and genuinely uses the good one still trips
    this. That over-triggering is the deliberate choice: under-triggering here
    reproduces exactly the silence this check exists to end, and the evidence
    for "which one was actually used" does not exist on disk today.
    """
    from pathlib import Path

    root = Path(cv_evidence_root)
    # ⛔ Never put the absolute path in evidence. Check FACTS must be comparable
    # across runs that differ only in policy (L-11 pins exactly that), and an
    # absolute staging path embeds a per-run tmp directory, which would make two
    # byte-identical products produce different fact rows. The trailing two
    # components keep the row debuggable without making it run-specific.
    root_label = "/".join(root.parts[-2:]) if len(root.parts) >= 2 else root.name
    if not root.is_dir():
        # ⛔ NOT "clean". Absence of evidence conflates "no calibration was run"
        # with "the directory moved", and this check must not launder either into
        # a pass — it says so in the message rather than emitting PASS.
        rep.add(
            CALIBRATION_AXES_CHECK_ID, CheckStatus.NOT_APPLICABLE, CheckLayer.CROSS_CHECK,
            message=(
                "no CV evidence directory — this check cannot speak to whether the "
                "reading was calibrated at all, and its silence is not evidence of a "
                "clean calibration"
            ),
            evidence={"cv_evidence_root": root_label, "exists": False},
        )
        return rep

    sidecars = sorted(root.glob(_CALIBRATOR_GLOB))
    if not sidecars:
        rep.add(
            CALIBRATION_AXES_CHECK_ID, CheckStatus.NOT_APPLICABLE, CheckLayer.CROSS_CHECK,
            message=(
                "no px_m_calibrator evidence present — the reading declares no "
                "pixel→metre ruler this check can audit; absence is not agreement"
            ),
            evidence={"cv_evidence_root": root_label, "calibrator_sidecars": 0},
        )
        return rep

    offenders: list[dict] = []
    unreadable: list[str] = []
    audited = 0
    for path in sidecars:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            unreadable.append(f"{path.name}: {type(exc).__name__}")
            continue
        for result in data.get("results") or []:
            if not isinstance(result, dict):
                continue
            audited += 1
            metric = result.get("metric") if isinstance(result.get("metric"), dict) else {}
            # Read the flag from BOTH levels: the tool writes it on the result and
            # mirrors it into `metric`, and a checker that trusted only one would
            # go quiet the day the other one is the surviving spelling.
            disagreed = bool(
                result.get("axis_calibration_disagreement")
                or metric.get("axis_calibration_disagreement")
            )
            if not disagreed:
                continue
            offenders.append(
                {
                    "sidecar": path.name,
                    "candidate_id": result.get("candidate_id"),
                    "axis_px_per_m": result.get("axis_px_per_m"),
                    "axis_relative_deviation": result.get("axis_relative_deviation"),
                    "axis_relative_deviation_limit": result.get("axis_relative_deviation_limit"),
                    "confidence": metric.get("confidence"),
                }
            )

    if unreadable:
        # Fail-closed: an unparseable calibration sidecar means we do not know the
        # ruler is consistent, which is not the same as knowing it is.
        rep.add(
            CALIBRATION_AXES_CHECK_ID, CheckStatus.ERROR, CheckLayer.CROSS_CHECK,
            message=f"calibration evidence could not be read: {'; '.join(unreadable)}",
            evidence={"unreadable": unreadable, "calibrator_sidecars": len(sidecars)},
        )
        return rep

    if offenders:
        rep.add_fail(
            CALIBRATION_AXES_CHECK_ID, CheckLayer.CROSS_CHECK,
            (
                f"{len(offenders)} calibration result(s) report the two axes disagreeing "
                "beyond the tool's own limit; the pixel→metre ruler is inconsistent with "
                "itself, so geometry derived from it is wrong in at least one direction. "
                "The tool returns success in this case by design (2026-08-17 legal exit) "
                "— this check is what makes that outcome visible to anything other than "
                "a reader choosing to mention it"
            ),
            evidence={
                "offenders": offenders,
                "calibrator_sidecars": len(sidecars),
                "calibration_results_audited": audited,
            },
        )
        return rep

    rep.add(
        CALIBRATION_AXES_CHECK_ID, CheckStatus.PASS, CheckLayer.CROSS_CHECK,
        message=f"all {audited} calibration result(s) agree across axes",
        evidence={"calibrator_sidecars": len(sidecars), "calibration_results_audited": audited},
    )
    return rep
