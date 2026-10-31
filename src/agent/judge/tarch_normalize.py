"""Tianzheng (天正) real-building DXF -> GT v3: P1 algorithm body (S0-S4).

Built strictly on top of the P0-frozen contracts in
:mod:`tarch_converter_schema`.  This module implements the **geometry stages
only**:

  S0  input preflight   proxy=0 / units explicit / view frame + unique title /
                        non-axis-parallel rejected
  S1  coordinate quantize   ``q = tau_node/10`` (a derived value); G2 conservation
                        (quantize may denoise but never merge two coords > tau_node
                        apart); zero-length (degenerate) lines bookkept as INFO;
                        an off-axis stroke is graded by the FOUR-TIER LADDER
                        (:func:`_ladder_verdict`, user-defined 2026-09-07):
                        tier 0 (minor leg <= ``q``) never enters the branch;
                        tier 1 is flattened about its ANCHOR END and itemised
                        as ``tarch_wall_axis_snapped`` (INFO); tier 2 is
                        refused AND itemised for human arbitration; tier 3
                        (angle > ``AXIS_SNAP_MAX_ANGLE_DEG``, 5.0°) is a
                        genuine diagonal, refused as before.  Both refusing
                        tiers raise ``tarch_wall_nonorthogonal`` (BLOCK)
                        whose context NAMES the tier and the reason
  S2  wall collect + jamb-cap identification  the short cross-section lines are
                        thickness evidence kind #2 (``wall_cap_or_opening_jamb``);
                        the legal thickness *range* is a sanity bound only, never
                        the *source* of a thickness (plan §2.1)
  S3  opening dual-evidence resolution   block bbox gives the along-wall span,
                        the wall's own jamb caps give the normal interval; the
                        bbox's normal extent must overlap the wall band (this is
                        what excludes a door's swing arc — D2).  Exactly one
                        solution fills; 0/many fail (plan §4 S3).
  S4  topology closure   ``polygonize_full`` over (all walls + all opening fills);
                        ``dangles/cuts/invalid`` all empty AND ``sum(face areas) ==
                        footprint area`` (plan §4 S4 / G5).

It does **not** do S5-S9 (cavities / intent binding / per-edge expand / gates
G6-G9 / persist) — that is P2.  No DXF is augmented or promoted in P1 (§0.1
方案A: convert+build always run in staging); the normalization lives in the IR.

Hard disciplines enforced here (dispatch §2 / plan §2):

  1. fail-closed — every ambiguous branch raises a BLOCK diagnostic and stops;
     there is no warn-then-continue path (the report status contract makes a
     PASS-with-BLOCK-diagnostic impossible).
  2. no baked thickness assumption — thickness comes from source geometry: a
     measured jamb cap or a source-bound pair of parallel WALL LINE faces; the
     [t_min, t_max] range is a sanity filter, not a source.  No
     ``DEFAULT_WALL_THICKNESS`` / ``MAX_WALL_PAIR_DISTANCE`` constant.
  3. non-exact-orthogonal — axis-ness is decided against ``q``, never by float
     ``==`` (D: walls deviate up to 1.31e-10 mm); a stroke that fails this may
     still be admitted by the LADDER above rather than dropped, but the
     decision is still a named, derived quantity, never a re-run of float
     ``==``.  ⭐ Until 2026-09-07 the ladder's entrance read ``tau_axis``
     (1 mm) while the downstream face-line classifier read exact equality;
     a stroke between the two (measured: exactly one in the whole corpus,
     13AF at 0.1915 mm) was refused by BOTH and vanished.  ``q`` is the real
     noise floor, and it is what the entrance reads now.
  4. no fabricated tolerance — every threshold is read from ``judge_gt.yaml`` via
     :func:`resolve_converter_tooling`; the quantization step is derived
     (``tau_node/10``), not a config key.  The ladder introduces NO new
     configuration: its deviation ceiling ``CAP`` is derived PER REQUEST from
     that request's own ``wall_thickness_range_m[0]`` (:func:`_axis_snap_cap_
     native`) and its observability grid is ``tau_node``.  ⛔ ONE declared
     exception: ``AXIS_SNAP_MAX_ANGLE_DEG`` is a plain module constant, not a
     ``judge_gt.yaml`` key — deliberately.  Its VALUE is signed (user,
     2026-09-07, superseding 2026-08-30's 1.0°), but signing a value is not
     the same act as admitting a KEY into that schema: its serialized form is
     baked into every already-signed gt.json's content hash, so a new key
     there retroactively invalidates signed answers (see the constant's own
     docstring for the empirical proof).
  5. gt isolation — this module is judge-side.  Tianzheng layer names, block-name
     prefixes and the view-frame convention are read ONLY from the request's
     :class:`TarchDialectRulesV1` / selectors, never from a module constant.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

import ezdxf
from ezdxf import bbox as ezdxf_bbox
from ezdxf.document import WRITTEN_BY_EZDXF
from ezdxf.lldxf.const import DXF12
from ezdxf.lldxf.tagwriter import TagWriter
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import polygonize_full, unary_union

from .tarch_converter_schema import (
    Affine2D, CavityIRV1, CavityReportV1, ClipBoxDxf, ConversionDiagnosticV1,
    DatumBoundNamedElevationViewIntentV3,
    ConversionReportV1, DiagnosticSeverity, EdgeBasis, GateResultV1, HumanReviewAckV1,
    OpeningCarrierRuleV1, OpeningReportV1, PlanViewIntentV1, Point2, PolygonIRV1,
    RingV1, StableId,
    SourceEntityRefV1, SourceMapEntryV1, SourceMapV1, TARCH_DIAGNOSTIC_REGISTRY,
    TarchConversionRequestV1, TarchDialectRulesV1, TarchEntitySelectorV1,
    ThicknessEvidenceV1,
    WallReportV1, WallRibbonSegmentV1, ZoneEdgeReportV1, ZoneReportV1,
    assert_staging_input, assert_staging_work_dir, compute_request_sha256,
    compute_source_map_sha256, derive_quantization_step,
    diagnostic_spec)

#: ⭐⭐⭐ SIGNED BY THE USER 2026-09-07, superseding the 1.0° signed
#: 2026-08-30 (F-143 → landed by F-147).  Verbatim: "长度越长容差应该越大一些…
#: 最大定到 5 度吧" and "按阶梯定一个方案，综合长度和角度；超出的才升级到需要
#: 人签字的仲裁".
#:
#: What it decides: it is the OUTER ENVELOPE of the ladder
#: (:func:`_ladder_verdict`).  A stroke whose off-axis angle exceeds it is a
#: GENUINE DIAGONAL (tier 3) -- refused as ``tarch_wall_nonorthogonal``
#: (BLOCK) and dropped, exactly as before.  Everything at or under it is a
#: candidate for automatic flattening (tier 1) or for human arbitration
#: (tier 2); which of the two is decided by ``CAP`` and by the anchor-end
#: rule, ⛔ neither of which is a new threshold (see
#: :func:`_axis_snap_cap_native` and :func:`_anchor_end`).
#:
#: ⭐⭐ Why an ANGLE and not a millimetre count: **the same millimetre number
#: means a ~120x different angle depending on the stroke's length**.  6 mm on
#: the 3640 mm stroke 13AD is 0.094° (obvious hand-tremor); 6 mm on a 30 mm
#: stroke is 11.310° (an unmistakable diagonal).  Measuring the ANGLE is also
#: what makes the user's "容差随长度增长" come out for free: the admissible
#: deviation is ``length · tan(angle)``, which grows with length by
#: construction -- ⛔ no second, length-dependent formula is needed.
#:
#: ⛔⛔ WHAT THE 08-30 → 09-07 CHANGE COSTS, recorded because it is real and
#: was accepted with full knowledge, ⛔ NOT because it is believed harmless:
#: 1.0° already admitted the cross-reviewer's 0.394° gently-slanted wall (two
#: 800 mm faces, each 5.5 mm out, 120 mm apart) -- both faces snapped, the
#: pair stayed 120 mm apart, and the pairing step MANUFACTURED a wall that is
#: not on the drawing.  5.0° admits a strictly WIDER band than 1.0° did, so
#: that cost is not reduced by this change; it is bounded instead by ``CAP``,
#: which refuses (to tier 2, for a human) any flattening that would move a
#: face by more than half the thinnest declared wall -- i.e. far enough to
#: change WHICH WALL the face belongs to.  On the 800 mm/5.5 mm sample the
#: deviation is 5.5 mm, well under a 30 mm CAP, so ⛔ that sample is still
#: admitted; see
#: ``tests/test_tarch_converter_p1_geometry.py``'s ``..._KNOWN_SIGNED_RISK``
#: test, which pins it at the NEW angle and is the executable form of this
#: paragraph.
#:
#: ⭐ Why this stays a module constant rather than a ``judge_gt.yaml`` key:
#: EMPIRICALLY VERIFIED (②-1b-S execution report) that adding even an
#: OPTIONAL field with a default to ``GtExtractionTolerancesV1`` flips
#: ``gt_hash_content_mismatch`` on the real SIGNED ``sm25-L_anchor/gt.json``
#: -- that schema's serialized form is baked into every already-signed
#: gt.json's ``content_sha256``.  Signing the VALUE (what the user did) is
#: not the same act as admitting the KEY into that trust root.
#:
#: ⭐ The compensating control is the LAST gate: every tier-1 admission emits
#: ``tarch_wall_axis_snapped`` and the facts transport copies ``minor_leg_mm``,
#: ``angle_deg``, the ladder's own ``deviation_limit_mm``, and WHICH END it
#: was anchored about onto the itemised ``axis_snapped_lines`` row (F-148).
AXIS_SNAP_MAX_ANGLE_DEG = 5.0

#: ⛔⛔ ``AXIS_SNAP_MAX_DEVIATION_M = 0.010`` (10 mm) WAS HERE AND IS RETIRED
#: (user, 2026-09-07: "10mm作废吧，就都按现在的推进就行").  ⛔ Do not
#: reintroduce it.  It was signed 2026-08-30 as an absolute millimetre
#: ceiling, and it is mathematically incompatible with "容差随长度增长":
#: ``10 mm / tan(5°) = 114 mm``, so on ANY stroke longer than 114 mm the 5°
#: envelope would have been dead and the millimetre number would have kept
#: deciding everything.  Its job -- "don't displace a face so far that it
#: stops belonging to its own wall" -- is now done by ``CAP``
#: (:func:`_axis_snap_cap_native`), which is DERIVED PER REQUEST from that
#: request's declared ``wall_thickness_range_m[0]`` instead of being a
#: module-level number, and which sends the over-cap stroke to a human
#: (tier 2) instead of silently dropping it.


# --------------------------------------------------------------------------- #
# Tolerance bundle — the only thresholds, all from judge_gt.yaml
# (+ AXIS_SNAP_MAX_ANGLE_DEG above, deliberately NOT from judge_gt.yaml)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Tols:
    """All seven judge tolerances (in metres) + derived helpers, none invented."""
    metres_per_unit: float
    node_join_m: float           # tau_node   (quantize base, node merge)
    axis_align_m: float          # tau_axis   (orthogonality)
    topo_area_m2: float          # tau_area   (area conservation)
    #: ⭐ Signed 2026-09-07 — see ``AXIS_SNAP_MAX_ANGLE_DEG``.
    #: ⛔ Scale-free by construction: an angle needs no ``metres_per_unit``
    #: conversion, which is precisely the property a millimetre gate lacks
    #: (hence no ``_native`` sibling property below).
    axis_snap_max_angle_deg: float = AXIS_SNAP_MAX_ANGLE_DEG

    @property
    def node_join_native(self) -> float:
        return self.node_join_m / self.metres_per_unit

    @property
    def axis_align_native(self) -> float:
        return self.axis_align_m / self.metres_per_unit

    @property
    def quant_native(self) -> float:
        # q = tau_node / 10, expressed in the DXF native unit (mm for sm24).
        return self.node_join_native / 10.0

    @property
    def ingest_grid_native(self) -> float:
        """⭐ The grid a coordinate is FINALLY stored on by the facts layer
        (``as_measured.INGEST_RESOLUTION_UNITS`` / ``UNITS_PER_METRE`` =
        10/10000 m = 1 mm), expressed in this drawing's native unit.

        ⛔ NOT a new threshold: it is ``tau_node`` itself (``node_join_m``),
        and ``q`` above is ``tau_node/10`` — so this is ``q``'s own parent,
        the coarser of the two, ⛔ not a third number.  It is used by ONE
        decision, :func:`_anchor_candidates_are_indistinguishable`, which
        asks "would a reader of the STORED document be able to tell these
        candidates apart at all".  The equality of the two definitions is a
        cross-module invariant, ⛔ not an assumption: it is locked by
        ``tests/test_tarch_converter_p1_geometry.py``'s
        ``test_gc_ingest_grid_is_tau_node_not_a_fourth_threshold``, which
        goes red the day either side moves.
        """
        return self.node_join_native


def _tols_from(tooling, metres_per_unit: float, _legacy_unused: float | None = None,
              *, axis_snap_max_angle_deg: float = AXIS_SNAP_MAX_ANGLE_DEG) -> _Tols:
    """⭐ The ladder's angle envelope is an INJECTABLE keyword parameter, on
    purpose.

    A test that needs to move exactly that gate passes it here and exercises
    the real production code path.  ⛔ It must NOT monkeypatch the
    module-level constant instead: ``from X import Y`` binds through the PARENT
    PACKAGE attribute, not ``sys.modules``, and that mistake has already
    produced a whole band of false-red "DID NOT RAISE" in this repo.  The
    object under test provides its own injection port; ⛔ no stand-in needed.

    ⭐ The ladder's OTHER limit, ``CAP``, has no keyword here on purpose: it is
    derived per request from ``wall_thickness_range_m[0]``
    (:func:`_axis_snap_cap_native`), so a test moves it by declaring a
    different thickness range on the request -- which exercises the derivation
    as well as the comparison.  ⛔ A module-level CAP would have baked the two
    in-corpus cases' 0.06 m into a constant (invariant #6).
    """
    t = tooling.tolerances
    return _Tols(metres_per_unit=metres_per_unit,
                 node_join_m=t.dxf_node_join_tolerance_m,
                 axis_align_m=t.dxf_axis_alignment_tolerance_m,
                 topo_area_m2=t.dxf_topology_area_tolerance_m2,
                 axis_snap_max_angle_deg=axis_snap_max_angle_deg)


#: Which endpoint's off-axis coordinate survives a tier-1 flattening.
#: ``"p0"``/``"p1"`` = the stroke's own first/second endpoint (the ANCHOR END,
#: chosen by :func:`_anchor_end` from the drawing's own topology);
#: ``"mid"`` = their midpoint, used ONLY when the anchor end cannot be
#: determined AND the choice is provably invisible in the stored document.
AnchorEnd = Literal["p0", "p1", "mid"]


def _snap_short_leg_to_axis(sx0: float, sy0: float, sx1: float, sy1: float,
                            *, anchor: AnchorEnd = "mid"
                            ) -> tuple[float, float, float, float, str]:
    """Zero out the SHORT leg of a (dx, dy) pair; the LONG leg's endpoints are
    never touched.  Returns ``(sx0', sy0', sx1', sy1', snapped_axis)``.

    ⛔ "Snap the short leg to zero, never move the long-leg-direction
    endpoints, never change the along-wall interval" is a fixed design
    constraint, ⛔ not a tunable.

    ``snapped_axis`` names the axis whose coordinate was collapsed to one
    shared value -- "x" if x0/x1 were made equal (the wall now runs along y),
    "y" if y0/y1 were made equal (runs along x).

    ⭐⭐⭐ WHICH VALUE SURVIVES is ``anchor``, and until 2026-09-07 it was
    unconditionally the MIDPOINT.  That was wrong, and the user named why:
    "像这种歪线大概率是一端在 cad 的吸附问题，可能吸错了，一般是一端错，
    不能取中点来改".  The error is CONCENTRATED AT ONE END, so averaging it
    over both ends corrupts the end that was right.  ⛔ Measured on the one
    real instance in the corpus: 13AD's east end reads ``100600.554`` which is
    bit-for-bit the signed drawing's value, and the midpoint moved it to
    ``100629.596`` -- 3.0 mm off, splitting BOTH of that wall's joints with
    its neighbours.

    ⭐ The old docstring justified the midpoint as "the only symmetric choice
    (no arbitrary bias toward whichever endpoint the DXF happened to list
    first)".  That reasoning weighed exactly three candidates -- first
    endpoint / second endpoint / midpoint -- and **the endpoint shared with
    another stroke was not among them**.  It is not arbitrary at all: it is
    fixed by the drawing's own topology (:func:`_anchor_end`).
    """
    dx, dy = abs(sx1 - sx0), abs(sy1 - sy0)
    if dx <= dy:
        shared_x = {"p0": sx0, "p1": sx1, "mid": (sx0 + sx1) / 2.0}[anchor]
        return shared_x, sy0, shared_x, sy1, "x"
    shared_y = {"p0": sy0, "p1": sy1, "mid": (sy0 + sy1) / 2.0}[anchor]
    return sx0, shared_y, sx1, shared_y, "y"


# --------------------------------------------------------------------------- #
# ⭐⭐⭐ The four-tier ladder (user-defined 2026-09-07).
#
#   "这种正交吸附和按毫米分辨率吸附我理解是不用签字的，直接修正就可以，
#    需要签字的是像之前上下墙体出现对齐错误的这类真『画错』问题"
#   "长度越长容差应该越大一些…最大定到 5 度吧"  (长度 = the skew stroke's OWN
#    total length, ⛔ not the deviation distance)
#   "按阶梯定一个方案，综合长度和角度；超出的才升级到需要人签字的仲裁"
#
#   tier 0  deviation <= q                      quantization erases it anyway
#                                               ⇒ ⛔ never enters the branch,
#                                                 ⛔ never recorded
#   tier 1  deviation <= min(len·tan5°, CAP)    machine flattens it about the
#           AND an anchor end is decidable      anchor end; ⛔ NO signature
#   tier 2  angle <= 5° but deviation > CAP;    refused AND itemised for a
#           or the anchor end is undecidable    HUMAN to arbitrate
#           and that choice is VISIBLE
#   tier 3  angle > 5°                          a real diagonal, refused as
#                                               before (⛔ not arbitration --
#                                               it is not "suspected drawing
#                                               error", it IS a diagonal)
#
# ⭐ Exactly THREE numbers exist in the whole ladder and ⛔ none of them is new:
#   q     = tau_node/10   (``_Tols.quant_native``, already the quantize step)
#   CAP   = half the request's own thinnest declared wall  (per request)
#   5.0°  = ``AXIS_SNAP_MAX_ANGLE_DEG``                     (user-signed)
# --------------------------------------------------------------------------- #
def _axis_snap_cap_native(request: TarchConversionRequestV1, tols: _Tols) -> float:
    """``CAP`` -- the ladder's absolute deviation ceiling, in native units.

    ⭐⭐ DERIVED PER REQUEST from that request's own declared
    ``wall_thickness_range_m[0]``: half the THINNEST wall the request says the
    drawing contains.  ⛔ Deliberately NOT a module constant: both in-corpus
    requests happen to declare ``0.06`` today, and writing ``30 mm`` here
    would bake that coincidence into the converter (invariant #6 -- ⛔ no
    baked building assumption).

    ⭐ What the semantic is, and why half a wall and not some round number:
    the flattening MOVES A FACE.  Move it by more than half the thinnest wall
    and the face can cross its own wall's centreline -- at which point the
    question is no longer "how do I straighten this line" but "WHICH WALL DOES
    THIS FACE BELONG TO", i.e. a change of design, ⛔ not a change of
    representation.  That is a decision for a human (tier 2), which is exactly
    where a stroke over this ceiling is sent.

    ⛔ It is NOT a noise floor (that is ``q``) and ⛔ NOT an envelope (that is
    the angle).  It is the point past which flattening stops being lossless in
    the only sense that matters downstream: wall membership.
    """
    return (request.wall_thickness_range_m[0] / 2.0) / tols.metres_per_unit


def _axis_snap_deviation_limit(length_native: float, cap_native: float,
                               max_angle_deg: float) -> float:
    """Tier 1's deviation ceiling for a stroke of this length:
    ``min(length · tan(max_angle), CAP)``.

    ⭐ The first limb is where the user's "长度越长容差应该越大一些" comes
    from -- and it comes for free, because measuring an ANGLE already means
    the admissible deviation scales with length.  ⛔ No separate
    length-dependent formula is needed or wanted.

    ⭐ The two limbs cross at ``CAP / tan(max_angle)`` (30 mm / tan 5° =
    343 mm with today's requests).  Below that length the ANGLE decides and
    the ceiling grows linearly; above it ``CAP`` decides and the EFFECTIVE
    angle tightens automatically (5.000° at 0.12 m, 0.472° at 3.64 m,
    0.172° at 10 m).  Both of the user's two statements -- "longer ⇒ more
    tolerance" and "on a long wall even a small angle is glaring" -- are
    therefore true at once, on different sides of that crossing.

    ⚠️ MEASURED PROPERTY, recorded so nobody reads more into the first limb
    than is there: :func:`_ladder_verdict` refuses ``angle > max_angle``
    BEFORE consulting this ceiling, and ``deviation = length·sin(angle)``, so
    for every stroke that reaches here ``deviation <= length·sin(max_angle) <
    length·tan(max_angle)`` already holds.  ⇒ once inside the envelope, the
    binding limb is ALWAYS ``CAP``.  The first limb is kept because it is the
    ladder's shape as the user defined it and because it is what makes the
    crossing point computable; it is ⛔ not dead weight, but it is ⛔ not the
    thing that refuses anything either.  The two forms differ only on
    ``(5.0000°, 5.0191°]`` -- see
    ``test_gc_ladder_limit_first_limb_is_subsumed_by_the_angle_envelope``.
    """
    return min(length_native * math.tan(math.radians(max_angle_deg)), cap_native)


def _stroke_is_already_axial(sx0: float, sy0: float, sx1: float, sy1: float,
                             q: float) -> bool:
    """Is this stroke axis-aligned as far as the pipeline is concerned?

    ⭐ Defined as "it does not enter the ladder at all", i.e. its minor leg is
    at or under the quantization step ``q``.  ⛔ Deliberately the SAME test
    the ladder's entrance uses, so the two can never disagree about what
    "already straight" means -- an orthogonal neighbour is exactly a stroke
    the ladder has no opinion about.
    """
    return min(abs(sx1 - sx0), abs(sy1 - sy0)) <= q


def _anchor_end(handle: str, p0: tuple[float, float], p1: tuple[float, float],
                axial_neighbours_at: dict[tuple[int, int], set[str]],
                q: float) -> tuple[AnchorEnd | None, str]:
    """⭐⭐⭐ WHICH END WAS DRAWN RIGHT -- from the drawing's own topology,
    ⛔ zero parameters.

    User's domain reading (2026-09-07): "像这种歪线大概率是一端在 cad 的吸附
    问题，可能吸错了，一般是一端错，不能取中点来改".  If one end failed to
    snap in the CAD editor, the OTHER end did snap -- and a snapped end is
    visible as a shared endpoint with another stroke.

    **The rule**: an end is ANCHORED iff it shares its point with another
    stroke that is ITSELF already axial.

    ⛔⛔ "itself already axial" cannot be dropped.  MEASURED: without it,
    13AD's west end also qualifies (its neighbour 13AF shares the point) and
    the rule degenerates to "both ends anchored" -- an answer, but the wrong
    one, since 13AF is part of the same crooked wall.  With it the answer is
    unique on all three real strokes.

    Returns ``(anchor, reason)`` where ``anchor`` is ``"p0"``/``"p1"`` when
    exactly one end is anchored and ``None`` when zero or both are (the
    ``reason`` string names which of those two it was -- ⛔ they are
    different situations and lead to different ladder tiers).

    ⭐ Endpoint coincidence is decided on the ``q`` grid -- the converter's own
    node identity, ⛔ not a new tolerance.
    """
    def key(point: tuple[float, float]) -> tuple[int, int]:
        return (round(point[0] / q), round(point[1] / q))

    at_p0 = axial_neighbours_at.get(key(p0), frozenset()) - {handle}
    at_p1 = axial_neighbours_at.get(key(p1), frozenset()) - {handle}
    if at_p0 and not at_p1:
        return "p0", "p0_shares_a_point_with_an_already_axial_stroke"
    if at_p1 and not at_p0:
        return "p1", "p1_shares_a_point_with_an_already_axial_stroke"
    if at_p0 and at_p1:
        return None, "both_ends_anchored"
    return None, "neither_end_anchored"


def _anchor_candidates_are_indistinguishable(sx0: float, sy0: float,
                                             sx1: float, sy1: float,
                                             q: float, ingest_grid: float
                                             ) -> tuple[bool, list[float]]:
    """⭐⭐⭐ "Would anybody be able to SEE which end we anchored?" -- ⛔ a
    zero-threshold test, ⛔ not a comparison against a tolerance.

    Takes the three candidate surviving off-axis coordinates (anchor p0 /
    anchor p1 / midpoint), pushes each through the SAME two-step chain a real
    coordinate walks -- quantize to ``q``, then snap to the facts layer's
    1 mm ingest grid -- and asks whether all three land on the SAME stored
    value.  If they do, the choice between them is not representable in the
    document at all, so ⛔ there is no reason to escalate it to a person; the
    midpoint (the symmetric choice) is taken and the stroke stays in tier 1.

    ⭐ The converse matters just as much and is why this is not a loophole:
    on a LONG stroke the same ambiguity IS visible (13AD's east anchor vs its
    midpoint differ by 3.0 mm = 30 ingest cells), and such a stroke is
    escalated.  One rule, no threshold, and it separates "a choice nobody can
    observe" from "a choice that changes the answer" by itself.

    ⛔ It is deliberately NOT written as ``|a - b| < something``: a tolerance
    would be a fourth number nobody signed, and it would be measuring a proxy
    (how far apart the candidates are) instead of the thing being guarded
    (whether the product can tell them apart).

    Returns ``(indistinguishable, stored_values)``; ``stored_values`` is
    itemised onto the record so the claim is auditable, ⛔ not just asserted.
    """
    dx, dy = abs(sx1 - sx0), abs(sy1 - sy0)
    raw = ((sx0, sx1) if dx <= dy else (sy0, sy1))
    candidates = [raw[0], raw[1], (raw[0] + raw[1]) / 2.0]
    stored = [_quantize(_quantize(value, q), ingest_grid) for value in candidates]
    return len(set(stored)) == 1, stored


def _endpoint_relocations(
        staged: list[tuple[str, str, float, float, float, float]],
        verdicts: dict[str, "_LadderVerdict | None"], q: float
        ) -> dict[tuple[tuple[int, int], str], float]:
    """⭐⭐⭐ THE OTHER HALF OF THE ANCHOR RULE: flattening a stroke about one
    end MOVES the other end, and every stroke that shared that point has to
    come along.

    ⛔ WHY THIS IS NOT OPTIONAL, measured on the corpus (sm25-L as-received,
    plan-F1, west end of the 120 mm partition).  13AD/13AE are flattened about
    their EAST ends, which drops their WEST ends by 5.81 mm.  13AF is the west
    end cap; its two endpoints ARE those two west ends.  Without this step
    13AF keeps the pre-flattening y values and the corner tears open:

        13AD.const 100600  vs  13AF.along_max 100660   ⇒ 6.0 mm gap
        13AE.const  99400  vs  13AF.along_min  99460   ⇒ 6.0 mm gap

    -- i.e. the fix for the EAST joints would have manufactured a WEST tear
    twice the size of the 3.0 mm one it set out to close.  With this step both
    corners close at 0, matching the signed drawing bit for bit.
    ⭐ Same lesson as the midpoint bug one level up: a treatment that reads one
    stroke at a time cannot see what that stroke is attached to.

    **The rule**, and the two guards that keep it from doing harm:

    1. A relocation is recorded ONLY for an endpoint a tier-1 flattening
       actually moved, keyed by that point on the ``q`` grid -- the converter's
       own node identity, ⛔ not a new tolerance.
    2. ⛔ If two flattenings would send one node to two different places ON THE
       SAME AXIS the entry is DROPPED, not guessed: an ambiguous node keeps
       today's behaviour rather than acquiring an invented one.  ⭐ Two moves
       on DIFFERENT axes are ⛔ not a conflict -- they are complementary, and
       the corpus needs exactly that: at the west corner 13AD moves the node
       in y while 13AF moves the same node in x, and each is followed by the
       other.  (Measured: keying the conflict test per NODE instead of per
       (node, axis) drops both and the corner stays 6.0 mm torn.)
    3. A neighbour follows only when the moved axis is that neighbour's ALONG
       axis (:func:`_apply_relocation`).  ⭐ That is what makes following
       structurally unable to bend anything: moving a point along a stroke's
       own direction changes its length, ⛔ never its straightness.  It is
       also why a stroke never follows its OWN proposal: a flattening moves a
       node on the stroke's CONST axis, which is never its along axis.

    Returns ``{(node_key, moved_axis): new_coordinate}``.
    """
    proposals: dict[tuple[tuple[int, int], str], set[float]] = {}
    for handle, _layer, sx0, sy0, sx1, sy1 in staged:
        verdict = verdicts.get(handle)
        if verdict is None or verdict.tier != 1:
            continue
        nx0, ny0, nx1, ny1, snapped_axis = _snap_short_leg_to_axis(
            sx0, sy0, sx1, sy1, anchor=verdict.anchor or "mid")
        for (ox, oy), (mx, my) in (((sx0, sy0), (nx0, ny0)), ((sx1, sy1), (nx1, ny1))):
            moved = mx if snapped_axis == "x" else my
            if moved == (ox if snapped_axis == "x" else oy):
                continue                      # the anchored end: it did not move
            key = ((round(ox / q), round(oy / q)), snapped_axis)
            proposals.setdefault(key, set()).add(moved)
    return {key: next(iter(values)) for key, values in proposals.items()
            if len(values) == 1}


def _apply_relocation(raw_point: tuple[float, float],
                      current: tuple[float, float], along_axis: str,
                      relocations: dict[tuple[tuple[int, int], str], float],
                      q: float) -> tuple[float, float]:
    """Move ``current`` onto a relocated node, but ⛔ ONLY along ``along_axis``.

    ⭐ The axis guard is the whole safety argument: this stroke's along axis is
    the direction it RUNS in, so shifting an endpoint there can only lengthen
    or shorten it.  Touching the other axis would tilt it -- i.e. would
    manufacture exactly the defect the ladder exists to remove -- so a node
    that moved on this stroke's CONST axis is simply not followed, and the
    stroke keeps the behaviour it had before this rule existed.
    """
    key = ((round(raw_point[0] / q), round(raw_point[1] / q)), along_axis)
    if key not in relocations:
        return current
    moved = relocations[key]
    return (moved, current[1]) if along_axis == "x" else (current[0], moved)


@dataclass(frozen=True)
class _LadderVerdict:
    """One stroke's place on the ladder.  ``tier`` is 1/2/3 (tier 0 never
    reaches here -- it does not enter the branch)."""
    tier: int
    reason: str
    anchor: AnchorEnd | None
    minor_leg: float
    major_leg: float
    length: float
    angle_deg: float
    deviation_limit: float
    cap: float
    stored_candidates: list[float] | None = None


def _ladder_verdict(handle: str, sx0: float, sy0: float, sx1: float, sy1: float,
                    *, tols: _Tols, cap_native: float,
                    axial_neighbours_at: dict[tuple[int, int], set[str]]
                    ) -> _LadderVerdict | None:
    """Grade one off-axis stroke on the four-tier ladder.  ``None`` = tier 0
    (the stroke never enters the branch -- ⛔ no record, nothing happens).

    ⭐ The order of the tests is load-bearing and is the ladder's own order:
    envelope first (is this even plausibly a drafting slip?), then ceiling
    (is straightening it still a representation change?), then the anchor
    (do we know which end to keep?).
    """
    q = tols.quant_native
    dx, dy = abs(sx1 - sx0), abs(sy1 - sy0)
    minor_leg, major_leg = min(dx, dy), max(dx, dy)
    length = math.hypot(dx, dy)
    # tier 0 -- at or under the quantization step there is nothing to decide:
    # ⭐ this ALSO covers the degenerate stroke (length < q has no direction at
    # all, and ``atan2(noise, noise)`` would invent one -- measured: 13DC's two
    # endpoints are 1.2e-10 mm apart and a naive probe called it a 3.47° slant).
    if minor_leg <= q or length <= q:
        return None
    angle_deg = math.degrees(math.atan2(minor_leg, major_leg))
    limit = _axis_snap_deviation_limit(length, cap_native,
                                       tols.axis_snap_max_angle_deg)
    common = dict(minor_leg=minor_leg, major_leg=major_leg, length=length,
                  angle_deg=angle_deg, deviation_limit=limit, cap=cap_native)
    # tier 3 -- outside the user-signed envelope: a real diagonal, ⛔ not a
    # drafting slip, ⛔ not an arbitration candidate.
    if angle_deg > tols.axis_snap_max_angle_deg:
        return _LadderVerdict(tier=3, reason="angle_beyond_envelope",
                              anchor=None, **common)
    # tier 2a -- inside the envelope but the move would exceed half the
    # thinnest declared wall ⇒ it could change wall membership ⇒ a human.
    if minor_leg > limit:
        return _LadderVerdict(tier=2, reason="deviation_over_cap",
                              anchor=None, **common)
    anchor, anchor_reason = _anchor_end(handle, (sx0, sy0), (sx1, sy1),
                                        axial_neighbours_at, q)
    if anchor is not None:
        return _LadderVerdict(tier=1, reason=anchor_reason, anchor=anchor, **common)
    # tier 2b -- BOTH ends are anchored to already-straight strokes.  Then the
    # stroke is not a slipped endpoint at all: it is a line the drawing means
    # to run between two settled points.  ⛔ Do not flatten it; a human decides.
    if anchor_reason == "both_ends_anchored":
        return _LadderVerdict(tier=2, reason="both_ends_anchored_suspected_true_slant",
                              anchor=None, **common)
    # neither end anchored -- ask whether the choice is even observable.
    indistinguishable, stored = _anchor_candidates_are_indistinguishable(
        sx0, sy0, sx1, sy1, q, tols.ingest_grid_native)
    if indistinguishable:
        return _LadderVerdict(tier=1, reason="anchor_choice_not_representable",
                              anchor="mid", stored_candidates=stored, **common)
    # tier 2c -- nobody can say which end is right AND the answer visibly
    # differs.  ⛔ Guessing here is exactly the midpoint mistake, one level up.
    return _LadderVerdict(tier=2, reason="anchor_undecidable_and_observable",
                          anchor=None, stored_candidates=stored, **common)


# --------------------------------------------------------------------------- #
# Small geometry helpers (work in DXF native units; convert to world at the edge)
# --------------------------------------------------------------------------- #
def _quantize(value: float, q: float) -> float:
    """Snap ``value`` to the ``q`` grid.  Deterministic (no banker's-rounding surprise
    on wall coords, which never sit on a .5 boundary at q = tau_node/10)."""
    return round(value / q) * q


def _overlap(lo_a: float, hi_a: float, lo_b: float, hi_b: float) -> float:
    """Positive-overlap length of two [lo,hi] intervals (0 if disjoint/touching)."""
    return min(hi_a, hi_b) - max(lo_a, lo_b)


def _to_world(point_xy: tuple[float, float], affine: Affine2D) -> Point2:
    """Apply the request's source->world affine to a DXF-native (mm) point."""
    x, y = point_xy
    return [affine.m00 * x + affine.m01 * y + affine.m02,
            affine.m10 * x + affine.m11 * y + affine.m12]


# --------------------------------------------------------------------------- #
# Diagnostic helper
# --------------------------------------------------------------------------- #
def _diag(code: str, *, handles: list[str] | None = None,
          points_dxf_mm: list[tuple[float, float]] | None = None,
          context: dict[str, Any] | None = None) -> ConversionDiagnosticV1:
    spec = diagnostic_spec(code)
    return ConversionDiagnosticV1(
        code=code, severity=spec.severity, stage=spec.stage,
        source_entity_handles=list(handles or []),
        source_points_dxf_mm=[list(p) for p in (points_dxf_mm or [])],
        context=dict(context or {}),
        action_code=spec.code)


def _add(diags: list[ConversionDiagnosticV1], d: ConversionDiagnosticV1) -> None:
    diags.append(d)


def _apply_test_neuter(gates: list[GateResultV1]) -> list[GateResultV1]:
    """Fresh-process mutation seam used only by the canonical gate suite."""
    target = os.environ.get("TARCH_NEUTER_GATE")
    return [gate.model_copy(update={"passed": True}) if gate.id == target else gate for gate in gates]


# --------------------------------------------------------------------------- #
# P1 result (the geometry artefacts of S0-S4 for one plan view)
# --------------------------------------------------------------------------- #
@dataclass
class ResolvedOpening:
    """One opening resolved by S3 dual evidence."""
    handle: str
    block_name: str
    kind: Literal["window", "door"]
    rect_dxf_mm: tuple[float, float, float, float]   # (x0, y0, x1, y1)
    axis: Literal["x", "y"]                          # along-wall axis
    cross_section_mm: tuple[float, float]            # normal interval [c1, c2]
    jamb_handles: list[str]
    classification: Literal["exterior", "interior_excluded"] = "exterior"

    def fill_edges(self) -> list[tuple[tuple[float, float], tuple[float, float]]]:
        x0, y0, x1, y1 = self.rect_dxf_mm
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        return [(corners[i], corners[(i + 1) % 4]) for i in range(4)]


@dataclass
class WallBand:
    """One thickness-homogeneous wall band, grouped from its jamb caps (S2).

    coord_mm is the band's midline representative (mean of its two face coords);
    the two real face coords are encoded in band_id and recoverable from
    thickness + cross-section.  P2 refines bands into full ribbons (both face
    tracks + joints); at P1 the cap-grouped band is the honest S2 output.
    """
    axis: Literal["x", "y"]            # along-wall (running) axis
    face_lo_mm: float                  # the two real wall-face coords (normal axis)
    face_hi_mm: float
    along_min_mm: float                # extent of the band along the running axis
    along_max_mm: float
    thickness_mm: float
    cap_handles: list[str]

    @property
    def coord_mm(self) -> float:
        return (self.face_lo_mm + self.face_hi_mm) / 2.0

    @property
    def band_id(self) -> str:
        return f"w_{self.axis}_{self.face_lo_mm:.4f}_{self.face_hi_mm:.4f}"


@dataclass
class P1PlanViewGeometry:
    """All S0-S4 outputs for one plan view, plus diagnostics and gate results."""
    view_id: str
    floor_id: str
    quant_step_native: float
    wall_lines: list[tuple[str, float, float, float, float]]   # (handle,x0,y0,x1,y1) quantized, non-degenerate
    degenerate_line_count: int
    jamb_caps_v: dict[float, set[tuple[float, float]]]         # const-x -> {(ylo,yhi)}
    jamb_caps_h: dict[float, set[tuple[float, float]]]         # const-y -> {(xlo,xhi)}
    cap_handles_v: dict[float, dict[tuple[float, float], list[str]]]
    cap_handles_h: dict[float, dict[tuple[float, float], list[str]]]
    wall_bands: list[WallBand]
    openings: list[ResolvedOpening]
    opening_fills: list[tuple[tuple[float, float], tuple[float, float]]]
    faces: list[Any]                  # shapely Polygons (native units)
    dangles: int
    cuts: int
    invalid: int
    sum_area_m2: float
    footprint_area_m2: float
    footprint_polygon: Any            # unary_union of faces (native units)
    wall_line_layers: dict[str, str] = field(default_factory=dict)  # handle -> source layer
    diagnostics: list[ConversionDiagnosticV1] = field(default_factory=list)
    gates: list[GateResultV1] = field(default_factory=list)
    #: ⛔ ②-1b-R (F-136, GLM A3): ``consumed_wall_handles`` was DELETED here --
    #: repo-wide zero write sites (only the ``default_factory=set`` default),
    #: so it was structurally always empty and its name promised a concept
    #: ("handles that fed a wall") that predates the ②-1a-R face-pairing
    #: rework and was never redefined for it.  The equivalent, ACTUALLY
    #: populated accounting already exists under names that say what they
    #: are: ``AsMeasuredConverterReadoutsV1.face_lines_excluded_as_jamb_caps``
    #: / ``face_lines_not_paired_into_a_wall`` (the paired/capped/unpaired
    #: three-bucket ledger `as_measured.py`'s view validator already
    #: enforces) plus the new S1-stage itemizations
    #: (``s1_nonorthogonal_discarded_handles`` / ``degenerate_line_handles``).
    all_wall_handles: set[str] = field(default_factory=set)

    @property
    def has_block(self) -> bool:
        return any(d.severity == DiagnosticSeverity.BLOCK for d in self.diagnostics)


# --------------------------------------------------------------------------- #
# S0 — input preflight
# --------------------------------------------------------------------------- #
def _frame_bbox(poly) -> tuple[float, float, float, float]:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return (min(xs), min(ys), max(xs), max(ys))


def _point_strictly_inside(px: float, py: float, box: tuple[float, float, float, float]) -> bool:
    return box[0] < px < box[2] and box[1] < py < box[3]


def _proxy_count(msp) -> int:
    """Count proprietary proxy entities (user did not run 'graphics export').

    Extracted so the predicate is unit-testable with duck-typed fake entities
    (ezdxf cannot synthesize a real ACAD_PROXY_ENTITY in a test)."""
    return sum(1 for e in msp if "PROXY" in e.dxftype().upper())


def s0_preflight(doc, msp, plan_view: PlanViewIntentV1, request: TarchConversionRequestV1,
                 tols: _Tols, diags: list[ConversionDiagnosticV1]) -> tuple[bool, set[str]]:
    """S0: proxy / units / view frame + unique title.  Returns (ok, wall_layer_names)."""
    ok = True
    cb = plan_view.clip_box_dxf
    clip = (float(cb.xmin), float(cb.ymin), float(cb.xmax), float(cb.ymax))
    # 1. proxy entities (user did not run 'graphics export')
    proxy_handles = [e.dxf.handle for e in msp if "PROXY" in e.dxftype().upper()]
    if proxy_handles:
        _add(diags, _diag("tarch_source_proxy_present",
                          handles=[proxy_handles[0]],
                          points_dxf_mm=[(clip[0], clip[1])],
                          context={"proxy_entity_count": len(proxy_handles)}))
        ok = False

    # 2. units: unitless source MUST carry an explicit metres_per_unit (no guessing)
    try:
        insunits = int(doc.header.get("$INSUNITS", 0))
    except Exception:  # pragma: no cover - defensive
        insunits = 0
    unit_scale = {"m": 1.0, "mm": 0.001, "cm": 0.01, "in": 0.0254, "ft": 0.3048}
    units_bad = False
    if insunits == 0 and request.native_units == "unitless":
        if request.metres_per_unit <= 0:   # guarded by PositiveFiniteFloat; future-proof
            units_bad = True
    if insunits != 0 and request.native_units != "unitless":
        declared = unit_scale.get(request.native_units)
        if declared is not None and abs(declared - request.metres_per_unit) > 1e-12:
            units_bad = True
    if units_bad:
        _add(diags, _diag("tarch_units_undeclared",
                          points_dxf_mm=[(clip[0], clip[1])],
                          context={"native_units": request.native_units,
                                   "metres_per_unit": request.metres_per_unit,
                                   "header_insunits": insunits}))
        ok = False

    # 3. view frame: the request's clip_box IS the frame (human-declared).  Verify
    #    a closed polyline frame exists at it and exactly one title sits inside.
    tau = tols.node_join_native
    frames = []
    for e in msp:
        if e.dxftype() == "LWPOLYLINE" and e.closed:
            fb = _frame_bbox(list(e.get_points()))
            if (abs(fb[0] - clip[0]) <= tau and abs(fb[1] - clip[1]) <= tau
                    and abs(fb[2] - clip[2]) <= tau and abs(fb[3] - clip[3]) <= tau):
                frames.append((e.dxf.handle, fb))
    if not frames:
        _add(diags, _diag("tarch_view_frame_missing",
                          points_dxf_mm=[(clip[0], clip[1]), (clip[2], clip[3])]))
        ok = False
    titles_inside: list[tuple[str, str]] = []
    for e in msp:
        if e.dxftype() in ("TEXT", "MTEXT"):
            ip = e.dxf.insert
            text = e.dxf.text if e.dxftype() == "TEXT" else e.text
            if _point_strictly_inside(float(ip.x), float(ip.y), clip):
                titles_inside.append((e.dxf.handle, text))
    if len(titles_inside) != 1:
        _add(diags, _diag("tarch_view_frame_ambiguous",
                          handles=[h for h, _ in titles_inside],
                          points_dxf_mm=[(clip[0], clip[1]), (clip[2], clip[3])],
                          context={"title_count": len(titles_inside),
                                   "frame_title": plan_view.frame_title}))
        ok = False
    return ok, titles_inside


# --------------------------------------------------------------------------- #
# S1 + S2 — quantize, collect walls, identify jamb caps, check orthogonality
# --------------------------------------------------------------------------- #
@dataclass
class _WallCollect:
    wall_lines: list[tuple[str, float, float, float, float]]
    degenerate: int
    caps_v: dict[float, set[tuple[float, float]]]
    caps_h: dict[float, set[tuple[float, float]]]
    cap_handles_v: dict[float, dict[tuple[float, float], list[str]]]
    cap_handles_h: dict[float, dict[tuple[float, float], list[str]]]
    all_handles: set[str]
    # G2 conservation: source coord -> set of pre-quantize source coords (per axis)
    source_x: dict[float, list[float]]
    source_y: dict[float, list[float]]
    wall_line_layers: dict[str, str] = field(default_factory=dict)


def _collect_walls(msp, plan_view: PlanViewIntentV1, request: TarchConversionRequestV1,
                   clip: tuple[float, float, float, float], tols: _Tols,
                   diags: list[ConversionDiagnosticV1]) -> _WallCollect:
    q = tols.quant_native
    wall_layers = set(plan_view.wall_selector.layers)
    wall_types = set(plan_view.wall_selector.entity_types)
    # wall thickness sanity range (native units); a FILTER, never a thickness source.
    t_min = request.wall_thickness_range_m[0] / tols.metres_per_unit
    t_max = request.wall_thickness_range_m[1] / tols.metres_per_unit
    # ⭐ the ladder's ceiling, DERIVED from this request (⛔ never a constant).
    cap_native = _axis_snap_cap_native(request, tols)

    wall_lines: list[tuple[str, float, float, float, float]] = []
    wall_line_layers: dict[str, str] = {}
    degenerate = 0
    caps_v: dict[float, set[tuple[float, float]]] = {}
    caps_h: dict[float, set[tuple[float, float]]] = {}
    cap_handles_v: dict[float, dict[tuple[float, float], list[str]]] = {}
    cap_handles_h: dict[float, dict[tuple[float, float], list[str]]] = {}
    all_handles: set[str] = set()
    source_x: dict[float, list[float]] = {}
    source_y: dict[float, list[float]] = {}

    # ⭐⭐ PASS 1 -- gather the in-frame strokes RAW.  The ladder's anchor-end
    # rule is a statement about the drawing's TOPOLOGY ("which of my two ends
    # is shared with a stroke that is itself straight"), so ⛔ it cannot be
    # answered while looking at one entity at a time.  Iteration order is
    # preserved into pass 2 so ``wall_lines`` comes out in exactly the order
    # it did before, bit for bit.
    staged: list[tuple[str, str, float, float, float, float]] = []
    for e in msp:
        if e.dxf.layer not in wall_layers:
            continue
        if e.dxftype() not in wall_types:
            # S0 entity gate: only the selector's wall primitive type (LINE) is a
            # wall this round.  Any other entity on a wall layer (arc/circle/
            # lwpolyline/proxy) is rejected, not approximated — no slope/curve.
            _add(diags, _diag("tarch_entity_unsupported",
                              handles=[e.dxf.handle],
                              context={"dxftype": e.dxftype(), "role": "wall"}))
            continue
        sx0, sy0 = float(e.dxf.start.x), float(e.dxf.start.y)
        sx1, sy1 = float(e.dxf.end.x), float(e.dxf.end.y)
        # restrict to the plan frame (both endpoints strictly inside)
        if not (_point_strictly_inside(sx0, sy0, clip) and _point_strictly_inside(sx1, sy1, clip)):
            continue
        all_handles.add(e.dxf.handle)
        staged.append((e.dxf.handle, e.dxf.layer, sx0, sy0, sx1, sy1))

    # ⭐⭐ Between the passes -- the anchor-end rule's one input: for every
    # point on the ``q`` grid, which ALREADY-AXIAL strokes touch it.  ⛔ The
    # "already axial" half is what keeps the rule from degenerating (see
    # :func:`_anchor_end`): a crooked neighbour anchors nothing.
    axial_neighbours_at: dict[tuple[int, int], set[str]] = {}
    for handle, _layer, sx0, sy0, sx1, sy1 in staged:
        if not _stroke_is_already_axial(sx0, sy0, sx1, sy1, q):
            continue
        for point in ((sx0, sy0), (sx1, sy1)):
            key = (round(point[0] / q), round(point[1] / q))
            axial_neighbours_at.setdefault(key, set()).add(handle)

    # ⭐⭐ PASS 2 -- grade every staged stroke on the ladder.
    verdicts: dict[str, _LadderVerdict | None] = {}
    for handle, _layer, sx0, sy0, sx1, sy1 in staged:
        verdicts[handle] = _ladder_verdict(handle, sx0, sy0, sx1, sy1, tols=tols,
                                           cap_native=cap_native,
                                           axial_neighbours_at=axial_neighbours_at)

    # ⭐⭐ PASS 3 -- THE JOINT FOLLOWS THE END THAT MOVED.
    relocations = _endpoint_relocations(staged, verdicts, q)

    # ⭐⭐ PASS 4 -- apply, then the unchanged S1/S2 body.
    for handle, layer, sx0, sy0, sx1, sy1 in staged:
        verdict = verdicts[handle]
        snapped: tuple[tuple[float, float], tuple[float, float], str,
                       _LadderVerdict] | None = None
        if verdict is not None and verdict.tier != 1:
            # tier 2 (arbitration) and tier 3 (real diagonal) are BOTH refused
            # and dropped -- the fail-closed outcome is the same, ⛔ but the
            # reason is not, and the record says which.  A tier-2 stroke is
            # additionally itemised for a human by the facts layer, keyed off
            # ``ladder_tier`` below.
            _add(diags, _diag("tarch_wall_nonorthogonal",
                              handles=[handle],
                              points_dxf_mm=[(sx0, sy0), (sx1, sy1)],
                              context={"ladder_tier": verdict.tier,
                                       "refused_by": [verdict.reason],
                                       "minor_leg_mm": verdict.minor_leg,
                                       "major_leg_mm": verdict.major_leg,
                                       "length_mm": verdict.length,
                                       "angle_deg": verdict.angle_deg,
                                       "deviation_limit_mm": verdict.deviation_limit,
                                       "cap_mm": verdict.cap,
                                       "axis_snap_max_angle_deg":
                                           tols.axis_snap_max_angle_deg,
                                       "anchor_candidates_stored":
                                           verdict.stored_candidates}))
            continue
        raw_p0, raw_p1 = (sx0, sy0), (sx1, sy1)
        if verdict is not None:
            sx0, sy0, sx1, sy1, snapped_axis = _snap_short_leg_to_axis(
                sx0, sy0, sx1, sy1, anchor=verdict.anchor or "mid")
            snapped = (raw_p0, raw_p1, snapped_axis, verdict)
        # ⭐ the along-axis endpoints follow any node this stroke shares with a
        # flattening that MOVED that node (see :func:`_endpoint_relocations`).
        along_axis = "x" if abs(sx1 - sx0) >= abs(sy1 - sy0) else "y"
        (sx0, sy0), (sx1, sy1) = (
            _apply_relocation(raw_p0, (sx0, sy0), along_axis, relocations, q),
            _apply_relocation(raw_p1, (sx1, sy1), along_axis, relocations, q))
        if snapped is not None:
            snapped = (raw_p0, raw_p1, snapped[2], verdict)
        x0, y0 = _quantize(sx0, q), _quantize(sy0, q)
        x1, y1 = _quantize(sx1, q), _quantize(sy1, q)
        if snapped is not None:
            # ⭐ emitted AFTER quantization so "after_p0"/"after_p1" are the
            # EXACT coordinates that end up in ``wall_lines`` -- a consumer
            # can therefore verify this record against the resulting face
            # line bit-for-bit, not against an intermediate value.
            before_p0, before_p1, snapped_axis, verdict = snapped
            _add(diags, _diag("tarch_wall_axis_snapped",
                              handles=[handle],
                              points_dxf_mm=[(x0, y0), (x1, y1)],
                              context={"before_p0": list(before_p0),
                                       "before_p1": list(before_p1),
                                       "snapped_axis": snapped_axis,
                                       "minor_leg_mm": verdict.minor_leg,
                                       # ⭐ the angle reading, alongside the
                                       # deviation, so a human reading the snap
                                       # list sees the numbers that admitted it.
                                       "angle_deg": verdict.angle_deg,
                                       # ⭐ G-c: the ladder's own readings --
                                       # WHICH TIER, what the tier-1 ceiling was
                                       # for a stroke this long, and WHICH END
                                       # the flattening was anchored about.  ⛔
                                       # Without the last one the record cannot
                                       # distinguish "we knew which end was
                                       # right" from "we averaged and hoped".
                                       "ladder_tier": 1,
                                       "length_mm": verdict.length,
                                       "deviation_limit_mm": verdict.deviation_limit,
                                       "cap_mm": verdict.cap,
                                       "anchor_end": verdict.anchor,
                                       "anchor_reason": verdict.reason,
                                       "anchor_candidates_stored":
                                           verdict.stored_candidates}))
        # G2 conservation bookkeeping (per axis, pre vs post quantize)
        for src, snap in ((sx0, x0), (sx1, x1)):
            source_x.setdefault(snap, []).append(src)
        for src, snap in ((sy0, y0), (sy1, y1)):
            source_y.setdefault(snap, []).append(src)
        # degenerate (zero-length after quantize) -> INFO, drop
        if x0 == x1 and y0 == y1:
            degenerate += 1
            _add(diags, _diag("tarch_wall_degenerate_line",
                              handles=[handle],
                              points_dxf_mm=[(x0, y0)]))
            continue
        wall_lines.append((handle, x0, y0, x1, y1))
        wall_line_layers[handle] = layer
        # S2 jamb-cap identification: short cross-section line within the sanity range.
        if x0 == x1:  # vertical segment -> cap of a horizontal wall band (normal = y)
            cap_length = abs(y1 - y0)
            if t_min <= cap_length <= t_max:
                span = (min(y0, y1), max(y0, y1))
                caps_v.setdefault(x0, set()).add(span)
                cap_handles_v.setdefault(x0, {}).setdefault(span, []).append(handle)
        elif y0 == y1:  # horizontal segment -> cap of a vertical wall band (normal = x)
            cap_length = abs(x1 - x0)
            if t_min <= cap_length <= t_max:
                span = (min(x0, x1), max(x0, x1))
                caps_h.setdefault(y0, set()).add(span)
                cap_handles_h.setdefault(y0, {}).setdefault(span, []).append(handle)

    return _WallCollect(wall_lines, degenerate, caps_v, caps_h,
                        cap_handles_v, cap_handles_h, all_handles, source_x, source_y,
                        wall_line_layers)


def _g2_conservation(collect: _WallCollect, tols: _Tols,
                     diags: list[ConversionDiagnosticV1]) -> bool:
    """G2: quantize may denoise but must not merge two coords > tau_node apart."""
    ok = True
    tau = tols.node_join_native
    for axis_name, table in (("x", collect.source_x), ("y", collect.source_y)):
        for snap, sources in table.items():
            if len(sources) < 2:
                continue
            lo, hi = min(sources), max(sources)
            if hi - lo > tau:
                _add(diags, _diag("tarch_quantization_conflict",
                                  points_dxf_mm=[(snap, snap)] if axis_name == "x" else [(snap, snap)],
                                  context={"axis": axis_name, "snap": snap,
                                           "min_source": lo, "max_source": hi,
                                           "gap": hi - lo}))
                ok = False
    return ok


def _build_wall_bands(collect: _WallCollect) -> list[WallBand]:
    """Group jamb caps into wall bands (one per unique cross-section)."""
    bands: list[WallBand] = []
    # horizontal-wall bands: caps_v groups by y cross-section; run along x.
    by_cs_v: dict[tuple[float, float], list[tuple[float, list[str]]]] = {}
    for xcoord, spans in collect.caps_v.items():
        for span in spans:
            by_cs_v.setdefault(span, []).append((xcoord, collect.cap_handles_v[xcoord][span]))
    for (ylo, yhi), members in sorted(by_cs_v.items()):
        xs = [m[0] for m in members]
        handles = sorted({h for _, hs in members for h in hs})
        bands.append(WallBand(axis="x", face_lo_mm=ylo, face_hi_mm=yhi,
                              along_min_mm=min(xs), along_max_mm=max(xs),
                              thickness_mm=yhi - ylo, cap_handles=handles))
    # vertical-wall bands: caps_h groups by x cross-section; run along y.
    by_cs_h: dict[tuple[float, float], list[tuple[float, list[str]]]] = {}
    for ycoord, spans in collect.caps_h.items():
        for span in spans:
            by_cs_h.setdefault(span, []).append((ycoord, collect.cap_handles_h[ycoord][span]))
    for (xlo, xhi), members in sorted(by_cs_h.items()):
        ys = [m[0] for m in members]
        handles = sorted({h for _, hs in members for h in hs})
        bands.append(WallBand(axis="y", face_lo_mm=xlo, face_hi_mm=xhi,
                              along_min_mm=min(ys), along_max_mm=max(ys),
                              thickness_mm=xhi - xlo, cap_handles=handles))
    bands.sort(key=lambda b: b.band_id)
    return bands


# --------------------------------------------------------------------------- #
# S3 — opening dual-evidence resolution
# --------------------------------------------------------------------------- #
def _classify_block(name: str, dialect: TarchDialectRulesV1) -> Literal["window", "door"] | None:
    is_window = name in dialect.window_block_names
    is_door = any(name.startswith(pref) for pref in dialect.door_block_prefixes)
    if is_window == is_door:  # both true is ambiguous; both false is unresolved
        return None
    return "window" if is_window else "door"


def _resolve_one(ext_x0: float, ext_y0: float, ext_x1: float, ext_y1: float,
                 caps_v: dict[float, set[tuple[float, float]]],
                 caps_h: dict[float, set[tuple[float, float]]]) -> list[tuple[Literal["x", "y"], float, float, float, float]]:
    """Dual evidence: try both axes; a solution = a jamb-cap pair at (lo,hi) with an
    identical cross-section [c1,c2] that the bbox's normal extent overlaps."""
    solutions: list[tuple[Literal["x", "y"], float, float, float, float]] = []
    # axis = x : opening spans x[lo,hi]; caps are vertical (caps_v) at x=lo and x=hi,
    #            cross-section is a y-interval; bbox normal extent is its y-range.
    for axis, lo, hi, caps, normal_lo, normal_hi in (
            ("x", ext_x0, ext_x1, caps_v, ext_y0, ext_y1),
            ("y", ext_y0, ext_y1, caps_h, ext_x0, ext_x1)):
        a_set = caps.get(lo, set())
        b_set = caps.get(hi, set())
        for a in a_set:
            for b in b_set:
                if a == b:  # identical cross-section => same wall band both ends
                    c1, c2 = a
                    if _overlap(c1, c2, normal_lo, normal_hi) > 0:
                        solutions.append((axis, lo, hi, c1, c2))
    return solutions


def s3_resolve_openings(msp, plan_view: PlanViewIntentV1, request: TarchConversionRequestV1,
                        clip: tuple[float, float, float, float], tols: _Tols,
                        collect: _WallCollect,
                        diags: list[ConversionDiagnosticV1]) -> tuple[list[ResolvedOpening], list[tuple[tuple[float, float], tuple[float, float]]]]:
    q = tols.quant_native
    opening_layers = set(plan_view.opening_selector.layers)
    opening_types = set(plan_view.opening_selector.entity_types)
    dialect = plan_view.dialect_rules

    openings: list[ResolvedOpening] = []
    fills: list[tuple[tuple[float, float], tuple[float, float]]] = []
    raw = []
    for e in msp:
        if e.dxf.layer not in opening_layers:
            continue
        if e.dxftype() not in opening_types:
            _add(diags, _diag("tarch_entity_unsupported",
                              handles=[e.dxf.handle],
                              context={"dxftype": e.dxftype(), "role": "opening"}))
            continue
        raw.append(e)
    raw.sort(key=lambda e: e.dxf.handle)

    for e in raw:
        ext = ezdxf_bbox.extents([e])
        sx0, sy0 = float(ext.extmin.x), float(ext.extmin.y)
        sx1, sy1 = float(ext.extmax.x), float(ext.extmax.y)
        cx, cy = (sx0 + sx1) / 2.0, (sy0 + sy1) / 2.0
        if not _point_strictly_inside(cx, cy, clip):
            continue
        x0, y0 = _quantize(sx0, q), _quantize(sy0, q)
        x1, y1 = _quantize(sx1, q), _quantize(sy1, q)
        kind = _classify_block(e.dxf.name, dialect)
        if kind is None:
            _add(diags, _diag("tarch_opening_kind_ambiguous",
                              handles=[e.dxf.handle],
                              context={"block_name": e.dxf.name}))
            continue
        sols = _resolve_one(x0, y0, x1, y1, collect.caps_v, collect.caps_h)
        if len(sols) == 0:
            _add(diags, _diag("tarch_opening_block_unresolved",
                              handles=[e.dxf.handle],
                              points_dxf_mm=[(x0, y0), (x1, y1)],
                              context={"block_name": e.dxf.name, "kind": kind}))
            continue
        if len(sols) > 1:
            _add(diags, _diag("tarch_opening_block_ambiguous",
                              handles=[e.dxf.handle],
                              points_dxf_mm=[(x0, y0), (x1, y1)],
                              context={"block_name": e.dxf.name, "kind": kind,
                                       "candidate_count": len(sols)}))
            continue
        axis, lo, hi, c1, c2 = sols[0]
        if axis == "x":
            rect = (lo, c1, hi, c2)
            cross = (c1, c2)
        else:
            rect = (c1, lo, c2, hi)
            cross = (c1, c2)
        jamb_handles: list[str] = []
        caps_table = collect.cap_handles_v if axis == "x" else collect.cap_handles_h
        for coord in (lo, hi):
            hs = caps_table.get(coord, {}).get(cross, [])
            jamb_handles.extend(hs)
        jamb_handles = sorted(set(jamb_handles))
        op = ResolvedOpening(handle=e.dxf.handle, block_name=e.dxf.name, kind=kind,
                             rect_dxf_mm=rect, axis=axis, cross_section_mm=cross,
                             jamb_handles=jamb_handles)
        openings.append(op)
        fills.extend(op.fill_edges())
    openings.sort(key=lambda o: o.handle)
    return openings, fills


# --------------------------------------------------------------------------- #
# S4 — topology closure + area conservation (G5)
# --------------------------------------------------------------------------- #
def s4_close_topology(wall_lines: list[tuple[str, float, float, float, float]],
                      fills: list[tuple[tuple[float, float], tuple[float, float]]],
                      tols: _Tols):
    mpu = tols.metres_per_unit
    segs: list[LineString] = []
    for _, x0, y0, x1, y1 in wall_lines:
        segs.append(LineString([(x0, y0), (x1, y1)]))
    for a, b in fills:
        segs.append(LineString([a, b]))
    polygons, cuts, dangles, invalid = polygonize_full(unary_union(segs))
    faces = list(polygons.geoms)
    dangle_geoms = list(dangles.geoms)
    cut_geoms = list(cuts.geoms)
    invalid_geoms = list(invalid.geoms)
    footprint = unary_union(faces) if faces else Polygon()
    sum_area_native = sum(g.area for g in faces)
    footprint_area_native = footprint.area
    return {"faces": faces, "dangles": dangle_geoms, "cuts": cut_geoms,
            "invalid": invalid_geoms, "n_dangles": len(dangle_geoms),
            "n_cuts": len(cut_geoms), "n_invalid": len(invalid_geoms),
            "sum_area_m2": sum_area_native * mpu * mpu,
            "footprint_area_m2": footprint_area_native * mpu * mpu,
            "footprint": footprint}


# --------------------------------------------------------------------------- #
# exterior / interior classification (D5) — needs the S4 footprint
# --------------------------------------------------------------------------- #
def _classify_openings(openings: list[ResolvedOpening], footprint: Any,
                       tols: _Tols, diags: list[ConversionDiagnosticV1]) -> None:
    """An opening is EXTERIOR iff either local wall face lies on the footprint
    exterior boundary (within tau_node).  Interior openings are INFO-excluded.

    Robust to a non-clean topology (a red fixture may leave a MultiPolygon): we
    classify against the largest constituent polygon.  Classification is moot
    when topology is broken (the report is BLOCKED anyway), but it must not crash.

    The test is deliberately local to the opening.  A single representative point
    cannot choose an outward side on a non-convex footprint: it may lie across a
    re-entrant notch from the opening and select the inward face.
    """
    from shapely.geometry import MultiPolygon, Polygon as _Poly
    if footprint.is_empty:
        return
    poly = footprint
    if isinstance(footprint, MultiPolygon):
        poly = max(footprint.geoms, key=lambda g: g.area)
    if not isinstance(poly, _Poly):
        return
    tau = tols.node_join_native
    skin = poly.exterior
    for op in openings:
        x0, y0, x1, y1 = op.rect_dxf_mm
        c1, c2 = op.cross_section_mm
        if op.axis == "x":  # wall runs along x; cross-section is y; sample at mid-x
            mid_along = (x0 + x1) / 2.0
            probes = [(mid_along, c1), (mid_along, c2)]
        else:  # wall runs along y; cross-section is x; sample at mid-y
            mid_along = (y0 + y1) / 2.0
            probes = [(c1, mid_along), (c2, mid_along)]
        distances = [skin.distance(Point(probe)) for probe in probes]
        on_skin = min(distances) <= tau
        op.classification = "exterior" if on_skin else "interior_excluded"
        if op.classification == "interior_excluded":
            probe = probes[distances.index(min(distances))]
            _add(diags, _diag("tarch_interior_opening_excluded",
                              handles=[op.handle],
                              points_dxf_mm=[probe],
                              context={"kind": op.kind, "rect": list(op.rect_dxf_mm)}))


# --------------------------------------------------------------------------- #
# S4 diagnostics — emit fail-closed codes for topology residuals (G5)
# --------------------------------------------------------------------------- #
def _emit_s4_diagnostics(s4: dict, tols: _Tols, diags: list[ConversionDiagnosticV1]) -> None:
    """Turn topology residuals into localizable BLOCK diagnostics.

    Dangling segments => a free-end / unfilled gap (``tarch_wall_free_end``); the
    converter never auto-extends (that would slice a corridor — Q5).  Non-zero
    cuts/invalid or an area mismatch => ``tarch_topology_residual``.  Each is
    localized on a real residual endpoint (never a placeholder).
    """
    if s4["n_dangles"] > 0:
        first = s4["dangles"][0]
        pt = tuple(c[:2] for c in first.coords)[0]
        _add(diags, _diag("tarch_wall_free_end",
                          points_dxf_mm=[pt],
                          context={"dangle_count": s4["n_dangles"]}))
    if s4["n_cuts"] > 0 or s4["n_invalid"] > 0:
        geoms = s4["cuts"] or s4["invalid"]
        pt = tuple(c[:2] for c in geoms[0].coords)[0] if geoms else (0.0, 0.0)
        _add(diags, _diag("tarch_topology_residual",
                          points_dxf_mm=[pt],
                          context={"cuts": s4["n_cuts"], "invalid": s4["n_invalid"]}))
    if abs(s4["sum_area_m2"] - s4["footprint_area_m2"]) > tols.topo_area_m2:
        fp = s4["footprint"]
        pt = (fp.centroid.x, fp.centroid.y) if not fp.is_empty else (0.0, 0.0)
        _add(diags, _diag("tarch_topology_residual",
                          points_dxf_mm=[pt],
                          context={"sum_area_m2": s4["sum_area_m2"],
                                   "footprint_area_m2": s4["footprint_area_m2"],
                                   "delta_m2": abs(s4["sum_area_m2"] - s4["footprint_area_m2"])}))


# --------------------------------------------------------------------------- #
# Orchestrator — run S0-S4 for one plan view
# --------------------------------------------------------------------------- #
def run_p1_plan_view(dxf_path: Path, request: TarchConversionRequestV1,
                     plan_view: PlanViewIntentV1, tooling) -> P1PlanViewGeometry:
    """Run S0-S4 on one plan view.  Pure: reads the DXF, returns geometry + diags.

    The DXF path MUST be a staging path (not a protected answer/source root);
    the staging discipline is enforced structurally (§0.1 方案A).
    """
    assert_staging_input(Path(dxf_path))
    tols = _tols_from(tooling, request.metres_per_unit)
    diags: list[ConversionDiagnosticV1] = []
    actual_sha = hashlib.sha256(Path(dxf_path).read_bytes()).hexdigest()
    request_hash_ok = request.request_sha256 == compute_request_sha256(request)
    ownership_ok = (plan_view.id in {v.id for v in request.plan_views}
                    and any(f.id == plan_view.floor_id for f in request.floors)
                    and any(v.id == plan_view.id and v.floor_id == plan_view.floor_id
                            for v in request.plan_views))
    if actual_sha != request.source_dxf_sha256 or not request_hash_ok or not ownership_ok:
        _add(diags, _diag("tarch_input_source_hash_mismatch", points_dxf_mm=[(0.0, 0.0)], context={
            "actual_source_sha256": actual_sha, "declared_source_sha256": request.source_dxf_sha256,
            "request_hash_ok": request_hash_ok, "plan_view_id": plan_view.id,
            "floor_id": plan_view.floor_id, "ownership_ok": ownership_ok}))
        empty = P1PlanViewGeometry(plan_view.id, plan_view.floor_id, tols.quant_native,
            [], 0, {}, {}, {}, {}, [], [], [], [], 0, 0, 0, 0.0, 0.0, Polygon(),
            diagnostics=diags)
        _assemble_gates(empty, False, False, tols)
        return empty
    doc = ezdxf.readfile(str(dxf_path))
    msp = doc.modelspace()
    cb = plan_view.clip_box_dxf
    clip = (float(cb.xmin), float(cb.ymin), float(cb.xmax), float(cb.ymax))

    s0_ok, _titles = s0_preflight(doc, msp, plan_view, request, tols, diags)

    collect = _collect_walls(msp, plan_view, request, clip, tols, diags)
    g2_ok = _g2_conservation(collect, tols, diags)
    bands = _build_wall_bands(collect)

    openings, fills = s3_resolve_openings(msp, plan_view, request, clip, tols, collect, diags)

    s4 = s4_close_topology(collect.wall_lines, fills, tols)
    _emit_s4_diagnostics(s4, tols, diags)
    _classify_openings(openings, s4["footprint"], tols, diags)

    result = P1PlanViewGeometry(
        view_id=plan_view.id, floor_id=plan_view.floor_id,
        quant_step_native=tols.quant_native,
        wall_lines=collect.wall_lines, wall_line_layers=collect.wall_line_layers,
        degenerate_line_count=collect.degenerate,
        jamb_caps_v=collect.caps_v, jamb_caps_h=collect.caps_h,
        cap_handles_v=collect.cap_handles_v, cap_handles_h=collect.cap_handles_h,
        wall_bands=bands, openings=openings, opening_fills=fills,
        faces=s4["faces"], dangles=s4["n_dangles"], cuts=s4["n_cuts"], invalid=s4["n_invalid"],
        sum_area_m2=s4["sum_area_m2"], footprint_area_m2=s4["footprint_area_m2"],
        footprint_polygon=s4["footprint"],
        diagnostics=diags, all_wall_handles=collect.all_handles)

    _assemble_gates(result, s0_ok, g2_ok, tols)
    return result


def _assemble_gates(result: P1PlanViewGeometry, s0_ok: bool, g2_ok: bool, tols: _Tols) -> None:
    """Emit the P1 gates.  G1/G2/G3/G5 are real pass/fail gates here; G4 (the
    14==14 outer-skin gap conservation) is an S7 check and is NOT emitted at P1
    (the exterior/interior CLASSIFICATION is done, as INFO diagnostics, but the
    conservation gate lands in P2 — emitting a stub G4=pass would be a false-lock)."""
    # G1 input preflight
    g1 = all(d.code != "tarch_source_proxy_present"
             and d.code != "tarch_units_undeclared"
             and d.code != "tarch_view_frame_missing"
             and d.code != "tarch_view_frame_ambiguous"
             and d.code != "tarch_entity_unsupported"
             and d.code != "tarch_wall_nonorthogonal" for d in result.diagnostics) and s0_ok
    result.gates.append(GateResultV1(
        id="G1", name="input preflight", passed=g1,
        evidence={"proxy_present": any(d.code == "tarch_source_proxy_present" for d in result.diagnostics),
                  "frame_ok": not any(d.code in ("tarch_view_frame_missing", "tarch_view_frame_ambiguous") for d in result.diagnostics)}))
    # G2 quantization conservation (the thickness-evidence coverage rigor — every
    # wall band fully evidenced — is P2 ribbon work; G2 at P1 is the conservation guard).
    result.gates.append(GateResultV1(
        id="G2", name="quantization conservation", passed=g2_ok,
        evidence={"quantization_conflict": any(d.code == "tarch_quantization_conflict" for d in result.diagnostics),
                  "jamb_caps_v": sum(len(v) for v in result.jamb_caps_v.values()),
                  "jamb_caps_h": sum(len(v) for v in result.jamb_caps_h.values()),
                  "wall_bands_evidenced": len(result.wall_bands)}))
    # G3 opening dual evidence
    unresolved = sum(1 for d in result.diagnostics if d.code in
                     ("tarch_opening_block_unresolved", "tarch_opening_block_ambiguous",
                      "tarch_opening_kind_ambiguous"))
    g3 = unresolved == 0
    result.gates.append(GateResultV1(
        id="G3", name="opening dual evidence", passed=g3,
        evidence={"resolved": len(result.openings), "unresolved_or_ambiguous": unresolved,
                  "exterior": sum(1 for o in result.openings if o.classification == "exterior"),
                  "interior_excluded": sum(1 for o in result.openings if o.classification == "interior_excluded")}))
    # G5 topology closure + area conservation
    area_ok = abs(result.sum_area_m2 - result.footprint_area_m2) <= tols.topo_area_m2
    g5 = result.dangles == 0 and result.cuts == 0 and result.invalid == 0 and area_ok
    result.gates.append(GateResultV1(
        id="G5", name="topology closure + area conservation", passed=g5,
        evidence={"faces": len(result.faces), "dangles": result.dangles,
                  "cuts": result.cuts, "invalid": result.invalid,
                  "sum_area_m2": result.sum_area_m2,
                  "footprint_area_m2": result.footprint_area_m2}))
    result.gates = _apply_test_neuter(result.gates)


# --------------------------------------------------------------------------- #
# Report builder — exercise the P0 contract with the P1 geometry
# --------------------------------------------------------------------------- #
#: repo root, computed the same way ``gt_schema.REPO_ROOT`` is (this file sits
#: at the same depth, ``src/agent/judge/``) -- kept local so this fingerprint
#: has no import-time dependency on any OTHER closure member.
_CONVERTER_REPO_ROOT = Path(__file__).resolve().parents[3]

#: ⭐⭐ F-D (dispatch ②-1b R4): the files that make up "the conversion
#: IMPLEMENTATION" for fingerprinting purposes.  ⛔ NOT hand-picked: this is
#: what a real closure walk from THIS file finds, by two mechanisms that are
#: each individually checked (not merely believed):
#:
#: 1. MODULE-LEVEL imports, transitively, from every ``from .X import ...`` /
#:    ``from src.agent....X import ...`` statement written at column 0 of a
#:    file already in the set.  ``tests/test_tarch_converter_reproducibility.py
#:    ::test_f_d_closure_membership_matches_a_static_import_walk`` re-derives
#:    this half mechanically and fails loudly if it and this tuple disagree --
#:    that is the "membership has an out" this fingerprint is required to have.
#:
#: 2. ONE lazy (function-body) import that a top-level-only scan cannot see:
#:    ``_run_g9_v3_preflight`` -- itself called from ``run_p2_conversion``, on
#:    the real P2 conversion path -- does ``from .gt_extraction import
#:    extract_gt_v3`` to run "the real v3 extractor" as a preflight GATE, so
#:    ``gt_extraction.py``'s bytes really can change what a conversion reports.
#:    (``gt_extraction.py``'s own top-level imports are already members of
#:    this set, so it adds no further files.)
#:
#: Two OTHER lazy imports elsewhere in this same closure were CHECKED and
#: EXCLUDED, not merely unnoticed -- each is grep-verified dead from every
#: entry point this fingerprint covers:
#:   * ``gt_schema.py``'s ``from .gt import DEFAULT_GT_DIR`` lives in
#:     ``_protected_candidate_path``, whose only caller (``write_gt_v3_candidate``)
#:     is called ONLY by the CLI ``scripts/tool_scripts/gt_from_dxf.py`` --
#:     never by anything on the conversion path.  (This mirrors the ALREADY
#:     declared blind spot for ``extractor_sha256`` in gt_raw_layer.py, which
#:     excludes that same CLI script for the same measured reason.)
#:   * ``schema.py``'s ``from src.agent.correction.window_host import
#:     WindowHostResolutionAuditV1`` runs only inside ``CorrectedGeometryV3``'s
#:     own validator, gated on a ``kind == "window_host_resolution"`` row.
#:     Nothing on the conversion path ever constructs a ``CorrectedGeometryV3``
#:     (the only ``CorrectedGeometryV3.model_validate`` call sites in the whole
#:     judge package are ``correction_score.py`` / ``score_service.py`` /
#:     ``segment_score.py``, none of which this closure imports).
CONVERTER_CLOSURE_FILES: tuple[str, ...] = tuple(sorted((
    "src/agent/judge/tarch_normalize.py",
    "src/agent/judge/tarch_converter_schema.py",
    "src/agent/judge/affine_space.py",
    "src/agent/judge/gt_manifest.py",
    "src/agent/judge/gt_schema.py",
    "src/agent/judge/gt_extraction.py",
    "src/agent/correction/facade_visibility.py",
    "src/agent/correction/footprint.py",
    "src/agent/correction/facade.py",
    "src/agent/correction/schema.py",
    "src/agent/correction/facade_convention.py",
    "src/agent/correction/claims.py",
    "src/agent/correction/constants.py",
)))


def _behavioural_source_digest(text: str) -> bytes:
    """AST-normalized digest of one file's source: comments/formatting don't move it.

    ``ast.dump(..., include_attributes=False)`` drops line/column numbers (so a
    brand-new comment LINE, which shifts every later statement's lineno, still
    doesn't move this) and comments are never AST nodes at all -- Python throws
    them away at parse time, before this function ever sees the tree.  A real
    behavioural edit (new statement, changed literal, renamed anything) always
    changes the dump.  ⚠️ NOT immune to docstring/string-literal edits -- those
    ARE ``Constant`` nodes -- only to ``#`` comments and layout, which is
    exactly dispatch's own example ("改一个字的注释").
    """
    return ast.dump(ast.parse(text), include_attributes=False).encode("utf-8")


def converter_sha256() -> str:
    """⭐⭐ F-D, widened (dispatch ②-1b R4): sha256 over the CONVERSION CLOSURE,
    AST-normalized -- ⛔ not ``sha256(tarch_normalize.py's own raw bytes)``.

    The pre-fix definition was wrong in both directions at once: a same-
    behaviour comment edit to this one file flipped it (noise), while a real
    behavioural edit to ``tarch_converter_schema.py`` / ``gt_manifest.py`` --
    files the conversion's OUTPUT actually depends on -- left it silent (the
    false negative that matters).  See ``CONVERTER_CLOSURE_FILES`` for exactly
    which files and why, and ``_behavioural_source_digest`` for why a comment
    no longer moves this.

    ⚠️ Existing signed-adjacent artefacts (``case_tests/test_baseline/gt/
    {sm24_anchor,sm25-L_anchor}/review/conversion_report.json``) recorded a
    ``converter_sha256`` under the OLD, narrow, raw-bytes definition and are
    NOT rewritten by this change (that file sits outside the human signature
    per gt_raw_layer.py's own docstring, but rewriting it is still a ``gt/``
    write this dispatch has no promotion path for).  ``gt_raw_layer.
    _fatal_fingerprints`` therefore accepts EITHER this value OR a member of
    ``KNOWN_PRE_F_D_CONVERTER_SHA256`` for an on-disk record that matches one
    -- an explicit, named exemption (dispatch ②-1b R4 "legacy" option), not a
    silent widening of what counts as a match.  New conversions (this tree,
    from here on) only ever stamp the widened value.
    """
    material = bytearray()
    for relative in CONVERTER_CLOSURE_FILES:
        path = (_CONVERTER_REPO_ROOT / relative).resolve()
        if not path.is_relative_to(_CONVERTER_REPO_ROOT) or not path.is_file():
            raise ValueError(f"converter_closure_file_missing:{relative}")
        digest = _behavioural_source_digest(path.read_text(encoding="utf-8"))
        material.extend(relative.encode("utf-8")); material.extend(b"\0")
        material.extend(digest); material.extend(b"\0")
    return hashlib.sha256(bytes(material)).hexdigest()


#: ⭐ FROZEN, not recomputed: each value is what
#: ``sha256(tarch_normalize.py's raw bytes)`` (the PRE-F-D-fix definition)
#: equalled at the commit that produced the named on-disk
#: ``conversion_report.json``, measured directly off that file
#: (2026-08-29, dispatch ②-1b R4).  A *computed* "legacy" function would be
#: self-defeating the moment this very fix edits ``tarch_normalize.py``: "hash
#: of this file's current bytes" stops equalling either on-disk value the
#: instant the file changes at all, which is exactly what adding this comment
#: does.  So the two values that already exist in the repo are pinned here
#: instead of re-derived.
#:
#: ⛔ ``sm24_anchor``'s value is DELIBERATELY NOT a member: F-132 already
#: measured (independently of this widening) that sm24's report predates the
#: current tree even under the OLD narrow definition, and the dispatch that
#: ordered this widening ("必须能看见这种漂移，⛔ 不许只在 sm25 上有牙")
#: requires that this gate keep reporting it as drifted -- exempting it here
#: would hide a real, already-known drift instead of merely tolerating a
#: definition change.  ⛔ This set must never grow silently: a NEW case
#: converted from here on always stamps the widened ``converter_sha256()``,
#: so a value can only earn a place here by having existed before this
#: dispatch AND being confirmed (by an independent measurement, not by "it
#: would be convenient") to have no other drift.
KNOWN_PRE_F_D_CONVERTER_SHA256: frozenset[str] = frozenset({
    # case_tests/test_baseline/gt/sm25-L_anchor/review/conversion_report.json,
    # commit a40d56d (this dispatch's baseline).
    "539615abee77a636f6b3432394e1abc50f0021dac54af652071cae81aec59696",
})


def build_p1_report(result: P1PlanViewGeometry, request: TarchConversionRequestV1,
                    plan_view: PlanViewIntentV1, tooling,
                    source_dxf_sha256: str) -> ConversionReportV1:
    """Build a ConversionReportV1 from the P1 geometry.

    P1 does not write an augmented normalized DXF (that is P2 S9); the
    normalization (quantization + opening fills) lives in the IR and is verified
    by gates G1-G5.  ``normalized_dxf_sha256`` is therefore bound to the *source*
    bytes at this stage and rebound to the augmented normalized.dxf in P2.
    """
    # WallBand field names retain the historical ``*_mm`` spelling, but P1 IR is
    # always in request-native units.  Reports must use the declared conversion,
    # not assume native DXF units are millimetres.
    mpu = request.metres_per_unit
    walls = [
        WallReportV1(
            band_id=b.band_id, floor_id=result.floor_id, axis=b.axis,
            coord_mm=b.coord_mm, span_mm=[b.along_min_mm, b.along_max_mm],
            segments=[WallRibbonSegmentV1(
                segment_id=b.band_id + "_s0", axis=b.axis, coord_m=b.coord_mm * mpu,
                span_m=[b.along_min_mm * mpu, b.along_max_mm * mpu],
                thickness_evidence=ThicknessEvidenceV1(
                    source_kind="wall_cap_or_opening_jamb",
                    value_m=b.thickness_mm * mpu,
                    proof_handles=b.cap_handles))],
            source_handles=b.cap_handles)
        for b in result.wall_bands]
    openings_report = [
        OpeningReportV1(
            opening_id=f"op_{o.handle.lower()}", kind=o.kind,
            classification=o.classification,
            rect_mm=list(o.rect_dxf_mm), block_handle=o.handle, block_name=o.block_name,
            jamb_handles=o.jamb_handles, geometric_witness=True)
        for o in result.openings]
    has_block = result.has_block
    status: Literal["PASS", "BLOCKED"] = "BLOCKED" if has_block else "PASS"
    return ConversionReportV1(
        report_version=1, status=status, case=request.case,
        source_dxf_sha256=source_dxf_sha256,
        normalized_dxf_sha256=(source_dxf_sha256 if status == "PASS" else None),
        request_sha256=request.request_sha256,
        judge_config_sha256=tooling.judge_config_sha256,
        vg_config_sha256=tooling.vg_config_sha256,
        converter_sha256=converter_sha256(),
        profile_version=1, quantization_step_m=derive_quantization_step(tooling),
        walls=walls, openings=openings_report,
        gates=result.gates, diagnostics=result.diagnostics,
        wall_proof_coverage={"wall_lines": len(result.wall_lines),
                             "wall_bands_evidenced": len(result.wall_bands),
                             "degenerate_lines": result.degenerate_line_count})


# =========================================================================== #
# P2 — S5..S9 (cavity -> intent -> per-edge expand -> nine gates -> persist)
#
# Geometry is carried in DXF *native* units (mm for sm24) and converted to
# world metres only at the report/manifest/overlay/seed boundary via _to_world.
#
# G8 (the reconstruction gate) is the highest-risk piece of this batch (plan §1
# G8 reinforcement, dispatch §2 #5).  It is implemented as an INDEPENDENT
# inverse: from the OUTPUT zone polygon + each edge's recorded basis+thickness
# it rebuilds the wall region and compares it to the S5-measured wall region.
# It NEVER reads S5's WallRegion or anything derived from it — rebuilding from
# ∪zones − ∪(reverse-shrunk zones), where the shrink uses only the zone output
# + recorded per-edge offsets, would otherwise collapse to the
# Footprint − Σzones tautology = a permanent false-green (plan §1).
# =========================================================================== #


@dataclass
class _ZoneEdgeRec:
    """One emitted zone-boundary edge (between consecutive zone vertices).

    ``basis`` is ``None`` for a thickness-change *step* edge (offset 0, no basis);
    wall edges carry their basis + measured thickness + outward offset.
    """
    nx: float            # outward normal x component (out of the zone)
    ny: float
    basis: EdgeBasis | None
    thickness_native: float | None
    offset_native: float   # t (outer_skin) | t/2 (wall_axis) | 0 (step)
    p1: tuple[float, float] | None = None
    p2: tuple[float, float] | None = None
    thickness_evidence: ThicknessEvidenceV1 | None = None
    # DXF handles of the SOURCE wall lines this edge derives from (the inner-face
    # wall line(s) the cavity edge lies on).  Carried so the report's source_handles
    # and the per-edge source_map ancestry are REAL handles, not a layer label.
    source_handles: list[str] = field(default_factory=list)


@dataclass
class ZoneExpansion:
    """S7 output for one zone: its polygon (native) + per-edge records + cavity."""
    cavity_id: str
    polygon: Any                # shapely Polygon (native units)
    vertices: list[tuple[float, float]]   # zone polygon verts (native, ordered, CCW)
    edges: list[_ZoneEdgeRec]   # parallel: edge vertices[i] -> vertices[i+1]
    seed_native: tuple[float, float]
    area_m2: float
    # Existing request/tooling tolerance carried with the derived geometry so
    # persistence can make topology decisions without widening its stable API.
    node_join_tolerance_native: float | None = None


@dataclass
class P2ConversionResult:
    """Everything P2 derives for one plan view: zones, gates, report, persist artefacts."""
    p1: P1PlanViewGeometry
    cavities: list[Any]              # shapely Polygons (native), area > A_room
    wall_region: Any                 # unary_union of wall-material faces (native)
    footprint: Any                   # unary_union of all faces (native)
    near_threshold_faces: list[dict] # [{area_m2, centroid_world_m}] in [0.5*A_room, 2*A_room]
    zones: list[ZoneExpansion]
    claims: list[dict]               # parallel to zones: intent binding per zone
    gates: list[GateResultV1]
    diagnostics: list[ConversionDiagnosticV1]
    manifest: Any = None             # GtExtractionManifestV1 (built in S9)
    augmented_dxf_path: Path | None = None
    conversion_report: ConversionReportV1 | None = None
    source_map: Any = None           # SourceMapV1
    overlay_path: Path | None = None
    gtv3_handles: dict | None = None                    # {layer: [handles]}
    gtv3_zone_edge_handles: dict | None = None          # {(zone_idx, edge_idx): handle}
    elevation_records: list[_ElevationRecord] = field(default_factory=list)
    elevation_audit_rows: list[dict] = field(default_factory=list)
    # The GT that G9 actually extracted.  It is the AUTHORITATIVE source for the
    # opening-side columns of the §7.4 audit table (opening id / host zone / plan-side
    # along interval); the converter must not re-derive a second version of them.
    elevation_document: Any = None   # GroundTruthV3 | None

    @property
    def has_block(self) -> bool:
        return any(d.severity == DiagnosticSeverity.BLOCK for d in self.diagnostics)


@dataclass
class MultiFloorConversionResult:
    """Document-level aggregation of the unchanged one-plan P2 geometry runs."""
    plan_results: list[P2ConversionResult]
    gates: list[GateResultV1]
    diagnostics: list[ConversionDiagnosticV1]
    manifest: Any = None
    augmented_dxf_path: Path | None = None
    conversion_report: ConversionReportV1 | None = None
    source_map: Any = None
    overlay_path: Path | None = None
    elevation_records: list[_ElevationRecord] = field(default_factory=list)
    elevation_document: Any = None

    @property
    def has_block(self) -> bool:
        return any(d.severity == DiagnosticSeverity.BLOCK for d in self.diagnostics)


def _clean_collinear(coords: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Remove collinear vertices (plan §4 S7: degenerate-redundant removal)."""
    c = list(coords)
    changed = True
    while changed:
        changed = False
        for i in range(len(c)):
            a, b, d = c[i - 1], c[i], c[(i + 1) % len(c)]
            if (a[0] == b[0] == d[0]) or (a[1] == b[1] == d[1]):
                c.pop(i); changed = True; break
    return c


def _ensure_ccw(coords: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not Polygon(coords).exterior.is_ccw:
        return coords[::-1]
    return coords


def _outward_normal(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float]:
    """Outward (right-hand) normal of a directed CCW orthogonal edge a->b."""
    if a[1] == b[1]:          # horizontal edge
        return (0.0, -1.0) if b[0] > a[0] else (0.0, 1.0)
    return (1.0, 0.0) if b[1] > a[1] else (-1.0, 0.0)


def _ray_thickness(mid: tuple[float, float], nx: float, ny: float,
                   wall_region: Any, footprint: Any, tols: _Tols) -> tuple[float, bool] | None:
    """Exact ray/boundary intersection; no native-unit march or range-derived pad."""
    minx, miny, maxx, maxy = unary_union([wall_region, footprint]).bounds
    reach = max(maxx - minx, maxy - miny) * 2.0 + tols.node_join_native
    ray = LineString([mid, (mid[0] + nx * reach, mid[1] + ny * reach)])
    hit = ray.intersection(wall_region.boundary)
    pts = []
    if hit.geom_type == "Point":
        pts = [hit]
    elif hasattr(hit, "geoms"):
        pts = [g for g in hit.geoms if g.geom_type == "Point"]
    eps = tols.node_join_native
    ds = sorted((p.x - mid[0]) * nx + (p.y - mid[1]) * ny for p in pts)
    ds = [d for d in ds if d > eps]
    if not ds:
        return None
    thickness = ds[0]
    exit_pt = Point(mid[0] + nx * thickness, mid[1] + ny * thickness)
    return thickness, footprint.exterior.distance(exit_pt) <= tols.node_join_native


def _thickness_profile(a: tuple[float, float], b: tuple[float, float],
                       nx: float, ny: float, wall_region: Any, footprint: Any,
                       tols: _Tols) -> list[tuple[tuple[float, float], tuple[float, float], float, EdgeBasis]]:
    """Per-segment thickness along edge a->b, split where thickness or basis
    changes (plan §4 S7-3).

    Returns ordered [(sub_start, sub_end, thickness, basis)] in native units.
    Uniform edges -> one segment (every sm24 edge).  A wall whose thickness
    changes mid-span -> multiple segments, each with its own measured thickness
    (real wall-pier step geometry).

    Event coordinates are projections of WallRegion boundary vertices onto the
    cavity edge.  Each open event interval is measured once at its midpoint, so
    a thickness change is located at the actual CAD coordinate, not a sample pad.
    """
    ax, ay = a
    bx, by = b

    def measure(f: float) -> tuple[float, EdgeBasis] | None:
        x, y = ax + (bx - ax) * f, ay + (by - ay) * f
        hit = _ray_thickness((x, y), nx, ny, wall_region, footprint, tols)
        if hit is None:
            return None
        t, ext = hit
        return (t, "outer_skin" if ext else "wall_axis")

    def same(p: tuple[float, EdgeBasis], q: tuple[float, EdgeBasis]) -> bool:
        return abs(p[0] - q[0]) <= tols.axis_align_native and p[1] == q[1]
    length = abs(bx - ax) if ay == by else abs(by - ay)
    events = {0.0, 1.0}
    for geom in getattr(wall_region.boundary, "geoms", [wall_region.boundary]):
        for x, y in getattr(geom, "coords", []):
            f = ((x - ax) * (bx - ax) + (y - ay) * (by - ay)) / (length * length)
            if tols.node_join_native / length < f < 1.0 - tols.node_join_native / length:
                # Only vertices in the normal wall slab can split this ray profile.
                px, py = ax + (bx - ax) * f, ay + (by - ay) * f
                if abs((x - px) * nx + (y - py) * ny) >= -tols.node_join_native:
                    events.add(round(f, 12))
    fs = sorted(events)
    out: list[tuple[tuple[float, float], tuple[float, float], float, EdgeBasis]] = []
    for f0, f1 in zip(fs, fs[1:]):
        m = measure((f0 + f1) / 2.0)
        if m is None:
            continue
        seg = ((ax + (bx - ax) * f0, ay + (by - ay) * f0),
               (ax + (bx - ax) * f1, ay + (by - ay) * f1), m[0], m[1])
        if out and same((out[-1][2], out[-1][3]), m):
            out[-1] = (out[-1][0], seg[1], out[-1][2], out[-1][3])
        else:
            out.append(seg)
    return out


def _offset_for(basis: EdgeBasis, thickness: float) -> float:
    return thickness if basis == "outer_skin" else thickness / 2.0


def _shift(pt: tuple[float, float], nx: float, ny: float, off: float) -> tuple[float, float]:
    return (pt[0] + nx * off, pt[1] + ny * off)


def _corner(v: tuple[float, float], n1: tuple[float, float], off1: float,
            n2: tuple[float, float], off2: float) -> tuple[float, float]:
    """Vertex = cavity vertex shifted by off1 along n1 and off2 along n2 (perpendicular)."""
    return (v[0] + n1[0] * off1 + n2[0] * off2, v[1] + n1[1] * off1 + n2[1] * off2)


def _edge_source_handles(a: tuple[float, float], b: tuple[float, float],
                         wall_lines, tols: _Tols) -> list[str]:
    """DXF handles of the SOURCE wall LINEs whose line the axis-aligned cavity edge
    a->b lies on.  A cavity boundary edge IS a (sub-span of a) source wall line — the
    inner face of the wall bounding that cavity — so matching axis+coord+span-overlap
    recovers the real source handle(s).  Used for honest per-edge ancestry (report
    ``source_handles`` + source_map); never a criterion.

    Wall LINEs may be drawn in either direction (x1<x0), so their span is taken as
    min/max — a segment fully inside the cavity edge is still a valid source."""
    ta = tols.axis_align_native
    out: list[str] = []
    if a[1] == b[1]:                       # horizontal cavity edge at y == a[1]
        yc, lo, hi = a[1], min(a[0], b[0]), max(a[0], b[0])
        for h, x0, y0, x1, y1 in wall_lines:
            if abs(y0 - yc) <= ta and abs(y1 - yc) <= ta:
                wlo, whi = min(x0, x1), max(x0, x1)
                if whi > lo + ta and wlo < hi - ta:        # spans overlap (non-trivial)
                    out.append(h)
    else:                                   # vertical cavity edge at x == a[0]
        xc, lo, hi = a[0], min(a[1], b[1]), max(a[1], b[1])
        for h, x0, y0, x1, y1 in wall_lines:
            if abs(x0 - xc) <= ta and abs(x1 - xc) <= ta:
                wlo, whi = min(y0, y1), max(y0, y1)
                if whi > lo + ta and wlo < hi - ta:
                    out.append(h)
    return out


# --------------------------------------------------------------------------- #
# S5 — cavity identification (area bisection) + wall region + outer skin
# --------------------------------------------------------------------------- #
def s5_identify_cavities(p1: P1PlanViewGeometry, request: TarchConversionRequestV1,
                         tols: _Tols, diags: list[ConversionDiagnosticV1],
                         affine: Affine2D) -> tuple[list[Any], Any, Any, list[dict]]:
    """Area bisection: faces > A_room are cavities; the rest union to the wall region.

    A_room is a DOMAIN PARAMETER (proposal only, never a criterion — plan §2.2);
    the criterion is the human-declared room count (G6).  Multiple disjoint exterior
    rings -> ``tarch_footprint_multiple`` (no 'take largest'); an interior ring ->
    ``tarch_profile_hole_unsupported``.  The near-threshold face list
    [0.5*A_room, 2*A_room] is computed unconditionally (承重 gate evidence, §2.2 C4).
    """
    mpu = tols.metres_per_unit
    a_room_mm2 = request.min_room_area_m2 / (mpu * mpu)
    faces = p1.faces
    cavities = [g for g in faces if g.area > a_room_mm2]
    wall_faces = [g for g in faces if g.area <= a_room_mm2]
    wall_region = unary_union(wall_faces) if wall_faces else Polygon()
    footprint = unary_union(faces) if faces else Polygon()

    # Outer-skin shape checks (plan §2.3): the simple-connected, no-hole cases S7
    # runs in are exactly those where unary_union == unbounded flood-fill outer skin
    # (plan §7b C2); the inequivalent cases (multipolygon / holes) are blocked here.
    if footprint.geom_type == "MultiPolygon" or (
            footprint.geom_type == "Polygon" and footprint.interiors):
        # localise on the first offending ring's representative point
        if footprint.geom_type == "MultiPolygon":
            rep = max(footprint.geoms, key=lambda g: g.area).representative_point()
            _add(diags, _diag("tarch_footprint_multiple",
                              points_dxf_mm=[(rep.x, rep.y)],
                              context={"component_count": len(footprint.geoms)}))
        else:
            ir = footprint.interiors[0]
            _add(diags, _diag("tarch_profile_hole_unsupported",
                              points_dxf_mm=[(ir.centroid.x, ir.centroid.y)],
                              context={"interior_ring_count": len(footprint.interiors)}))

    # Near-threshold承重 face list (always computed, not only on failure).
    lo, hi = 0.5 * a_room_mm2, 2.0 * a_room_mm2
    near: list[dict] = []
    for g in faces:
        if lo <= g.area <= hi:
            wpt = _to_world((g.centroid.x, g.centroid.y), affine)
            near.append({"area_m2": g.area * mpu * mpu, "centroid_world_m": wpt,
                         "is_cavity": g.area > a_room_mm2})
    near.sort(key=lambda d: d["area_m2"])
    return cavities, wall_region, footprint, near


# --------------------------------------------------------------------------- #
# S6 — intent binding (cavities <-> human-declared ZoneIntentSpecV1)
# --------------------------------------------------------------------------- #
def s6_bind_intent(cavities: list[Any], plan_view: PlanViewIntentV1,
                   tols: _Tols, diags: list[ConversionDiagnosticV1],
                   affine: Affine2D) -> list[dict]:
    """Bind cavities to the human-declared room list.  Coordinates come from the
    machine (cavity representative point); count+name come from the human
    (plan §4 S6 / §5.5 — the single non-mechanical step).  No auto-A_room."""
    intent = plan_view.zone_intent
    # canonical order: (minx, miny) of cavity bounds (plan §5.5)
    ordered = sorted(enumerate(cavities),
                     key=lambda kv: (round(kv[1].bounds[0], 6), round(kv[1].bounds[1], 6)))
    expected = intent.expected_count
    if len(cavities) != expected:
        reps = [_to_world((g.representative_point().x, g.representative_point().y), affine)
                for _, g in ordered]
        _add(diags, _diag("tarch_cavity_count_mismatch",
                          points_dxf_mm=[(g.representative_point().x, g.representative_point().y)
                                         for _, g in ordered[:8]],
                          context={"cavity_count": len(cavities), "expected_count": expected,
                                   "cavity_centroids_world_m": reps}))
        return []
    # void declarations remove a cavity from the zone set (no voids on sm24)
    void_pts = [(v.point_world_m[0], v.point_world_m[1]) for v in plan_view.void_intent]
    claims: list[dict] = []
    for idx, (orig_i, g) in enumerate(ordered):
        entry = intent.entries[idx]
        is_void = any(g.contains(Point(*_world_to_native(wp, affine))) for wp in void_pts)
        if is_void:
            continue
        seed_native = (g.representative_point().x, g.representative_point().y)
        claims.append({"cavity": g, "cavity_index": orig_i,
                       "zone_id": entry.zone_id, "name": entry.name or entry.zone_id,
                       "role": entry.role, "seed_native": seed_native})
    # any cavity unclaimed (no entry) would already have failed the count gate
    if intent.mode == "intent_file" and not claims:
        _add(diags, _diag("tarch_cavity_unclaimed",
                          points_dxf_mm=[(c.representative_point().x, c.representative_point().y)
                                         for c in cavities[:8]]))
    return claims


def _world_to_native(world_xy: list[float], affine: Affine2D) -> tuple[float, float]:
    """Inverse of _to_world (seed world -> native), used only for void containment."""
    # affine is source->world; for a pure scale+offset sm24 affine the inverse is exact.
    a, b, c, d, e, f = affine.m00, affine.m01, affine.m02, affine.m10, affine.m11, affine.m12
    det = a * e - b * d
    wx, wy = world_xy
    return ((e * (wx - c) - b * (wy - f)) / det, (-d * (wx - c) + a * (wy - f)) / det)


# --------------------------------------------------------------------------- #
# S7 — per-edge expand to the mixed basis box (outer skin / wall axis)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _WallFacePair:
    """A source-bound wall ribbon measured between two parallel WALL LINE faces."""
    axis: Literal["x", "y"]
    layer: str
    handle_a: str
    handle_b: str
    coord_a: float
    coord_b: float
    span_a: tuple[float, float]
    span_b: tuple[float, float]
    overlap_native: tuple[float, float]
    thickness_native: float

    @property
    def proof_handles(self) -> list[str]:
        return [self.handle_a, self.handle_b]


@dataclass(frozen=True)
class _FacePairBinding:
    """One cavity edge/subinterval bound to its source face-pair evidence."""
    evidence: ThicknessEvidenceV1
    axis: Literal["x", "y"]
    layer: str
    near_handle: str
    far_handle: str
    near_coord: float
    far_coord: float
    normal: tuple[float, float]
    thickness_native: float


def _wall_line_geometry(line) -> tuple[str, Literal["x", "y"], float, tuple[float, float]]:
    handle, x0, y0, x1, y1 = line
    if y0 == y1:
        return handle, "x", y0, (min(x0, x1), max(x0, x1))
    return handle, "y", x0, (min(y0, y1), max(y0, y1))


def _build_wall_face_pairs(wall_lines, wall_line_layers: dict[str, str],
                           wall_region: Any,
                           thickness_range_native: tuple[float, float],
                           tols: _Tols) -> list[_WallFacePair]:
    """Measure wall ribbons from parallel, same-layer source WALL LINE faces.

    The request range is only an admissibility filter.  The evidence value is the
    actual normal separation of the two source lines.  A positive along-wall
    overlap and wall-material coverage between the lines prevent nearby unrelated
    faces from being paired.
    """
    t_min, t_max = thickness_range_native
    records = [(_wall_line_geometry(line), line) for line in wall_lines]
    pairs: list[_WallFacePair] = []
    for i, ((ha, axis_a, ca, sa), _line_a) in enumerate(records):
        layer_a = wall_line_layers.get(ha)
        if layer_a is None:
            continue
        for (hb, axis_b, cb, sb), _line_b in records[i + 1:]:
            if axis_a != axis_b or wall_line_layers.get(hb) != layer_a:
                continue
            thickness = abs(cb - ca)
            if thickness < t_min or thickness > t_max:
                continue
            lo, hi = max(sa[0], sb[0]), min(sa[1], sb[1])
            if hi - lo <= tols.node_join_native:
                continue
            if axis_a == "x":
                strip = Polygon([(lo, min(ca, cb)), (hi, min(ca, cb)),
                                 (hi, max(ca, cb)), (lo, max(ca, cb))])
            else:
                strip = Polygon([(min(ca, cb), lo), (max(ca, cb), lo),
                                 (max(ca, cb), hi), (min(ca, cb), hi)])
            missing_m2 = strip.difference(wall_region).area * (
                tols.metres_per_unit * tols.metres_per_unit)
            if missing_m2 > tols.topo_area_m2:
                continue
            pairs.append(_WallFacePair(
                axis=axis_a, layer=layer_a, handle_a=ha, handle_b=hb,
                coord_a=ca, coord_b=cb, span_a=sa, span_b=sb,
                overlap_native=(lo, hi), thickness_native=thickness))
    return pairs


def _distance_to_span(value: float, span: tuple[float, float]) -> float:
    if span[0] <= value <= span[1]:
        return 0.0
    return min(abs(value - span[0]), abs(value - span[1]))


def _face_pair_binding_for_edge(a: tuple[float, float], b: tuple[float, float],
                                nx: float, ny: float,
                                sub_along: tuple[float, float],
                                source_handles: list[str],
                                pairs: list[_WallFacePair], wall_lines,
                                tols: _Tols) -> _FacePairBinding | None:
    """Bind an edge interval through its own source handles, never by value lookup."""
    axis: Literal["x", "y"] = "x" if a[1] == b[1] else "y"
    near_coord = a[1] if axis == "x" else a[0]
    normal_component = ny if axis == "x" else nx
    midpoint = (sub_along[0] + sub_along[1]) / 2.0
    candidates: list[tuple[float, _WallFacePair, str, str, float, tuple[float, float]]] = []
    for pair in pairs:
        if pair.axis != axis:
            continue
        for near_h, far_h, near_c, far_c, near_span in (
                (pair.handle_a, pair.handle_b, pair.coord_a, pair.coord_b, pair.span_a),
                (pair.handle_b, pair.handle_a, pair.coord_b, pair.coord_a, pair.span_b)):
            if near_h not in source_handles:
                continue
            if abs(near_c - near_coord) > tols.axis_align_native:
                continue
            if (far_c - near_c) * normal_component <= 0:
                continue
            candidates.append((_distance_to_span(midpoint, near_span), pair,
                               near_h, far_h, near_c, near_span))
    if not candidates:
        return None
    best_distance = min(item[0] for item in candidates)
    nearest = [item for item in candidates
               if abs(item[0] - best_distance) <= tols.node_join_native]
    reference = nearest[0]
    _, ref_pair, ref_near_h, ref_far_h, ref_near_c, _ = reference
    ref_far_c = ref_pair.coord_b if ref_near_h == ref_pair.handle_a else ref_pair.coord_a
    if any(abs(item[1].thickness_native - ref_pair.thickness_native) > tols.axis_align_native
           or abs((item[1].coord_b if item[2] == item[1].handle_a else item[1].coord_a)
                  - ref_far_c) > tols.axis_align_native
           for item in nearest[1:]):
        return None
    return _FacePairBinding(
        evidence=ThicknessEvidenceV1(
            source_kind="wall_face_pair",
            value_m=ref_pair.thickness_native * tols.metres_per_unit,
            proof_handles=[ref_near_h, ref_far_h]),
        axis=axis, layer=ref_pair.layer, near_handle=ref_near_h,
        far_handle=ref_far_h, near_coord=ref_near_c, far_coord=ref_far_c,
        normal=(nx, ny), thickness_native=ref_pair.thickness_native)


def _face_pair_binding_is_consistent(binding: _FacePairBinding, wall_lines,
                                     wall_line_layers: dict[str, str],
                                     tols: _Tols) -> bool:
    """Re-read the immutable source-face bookkeeping used to build ``binding``.

    This is a consistency check, not an independent geometric observation: both
    the binding and this re-read consume the same source WALL LINE records.  S4's
    input topology checks and G8's round-trip check are the independent defences
    against a missing or displaced opposite wall face.
    """
    by_handle = {line[0]: _wall_line_geometry(line) for line in wall_lines}
    near = by_handle.get(binding.near_handle)
    far = by_handle.get(binding.far_handle)
    if near is None or far is None:
        return False
    if (wall_line_layers.get(binding.near_handle) != binding.layer
            or wall_line_layers.get(binding.far_handle) != binding.layer):
        return False
    _, near_axis, near_coord, near_span = near
    _, far_axis, far_coord, far_span = far
    if near_axis != binding.axis or far_axis != binding.axis:
        return False
    normal_component = binding.normal[1] if binding.axis == "x" else binding.normal[0]
    predicted_far = near_coord + normal_component * binding.thickness_native
    return (abs(near_coord - binding.near_coord) <= tols.node_join_native
            and abs(far_coord - binding.far_coord) <= tols.node_join_native
            and abs(predicted_far - far_coord) <= tols.node_join_native
            and _overlap(near_span[0], near_span[1], far_span[0], far_span[1])
            > tols.node_join_native)


def _thickness_evidence_for(thickness_native: float, bands: list[WallBand],
                            tols: _Tols) -> ThicknessEvidenceV1 | None:
    """Resolve S7's measured value to an independent S2 cap/jamb proof."""
    for band in bands:
        if abs(band.thickness_mm - thickness_native) <= tols.axis_align_native and band.cap_handles:
            return ThicknessEvidenceV1(source_kind="wall_cap_or_opening_jamb",
                                       value_m=thickness_native * tols.metres_per_unit,
                                       proof_handles=band.cap_handles)
    return None


def s7_expand_zones(claims: list[dict], wall_region: Any, footprint: Any,
                    tols: _Tols, diags: list[ConversionDiagnosticV1],
                    wall_lines, wall_bands: list[WallBand] | None = None,
                    wall_line_layers: dict[str, str] | None = None,
                    thickness_range_native: tuple[float, float] | None = None,
                    ) -> list[ZoneExpansion]:
    """For each claimed cavity: clean+CCW, per-edge measure thickness + far-side
    classify (outer_skin -> offset t; wall_axis -> offset t/2), split edges on
    thickness change, rebuild the zone polygon from offset support-line corners +
    thickness-change steps (plan §4 S7).  L/T/cross/re-entrant joints need no
    special-case code; free-ends never reach here (S4 dangle gate).

    ``vertices`` and ``edges`` are parallel lists: ``edges[k]`` is the edge from
    ``vertices[k]`` to ``vertices[(k+1) % m]``.  This parallelism is what lets G8
    reverse-shrink the zone by subtracting each edge's recorded offset (the exact
    algebraic inverse of the expansion below).
    """
    mpu = tols.metres_per_unit
    face_pairs = (_build_wall_face_pairs(
        wall_lines, wall_line_layers or {}, wall_region,
        thickness_range_native, tols)
        if thickness_range_native is not None else [])
    zones: list[ZoneExpansion] = []
    for k, claim in enumerate(claims):
        g = claim["cavity"]
        c = _ensure_ccw(_clean_collinear([(p[0], p[1]) for p in list(g.exterior.coords)[:-1]]))
        n = len(c)
        subs_per_edge: list[list[tuple[tuple, tuple, float, EdgeBasis]]] = []
        proofs_per_edge: list[list[ThicknessEvidenceV1 | None]] = []
        normals: list[tuple[float, float]] = []
        edge_sources: list[list[str]] = []
        for i in range(n):
            a, b = c[i], c[(i + 1) % n]
            nx, ny = _outward_normal(a, b)
            normals.append((nx, ny))
            src = _edge_source_handles(a, b, wall_lines, tols)
            raw_profile = _thickness_profile(a, b, nx, ny, wall_region, footprint, tols)
            # A perpendicular T/cross wall can create a short ray interval through
            # the junction material.  It is not a thickness event unless it has an
            # independent S2 proof.  Collapse only such unproven junction runs into
            # their proven continuation; a genuine change must carry its own proof.
            proofs = [_thickness_evidence_for(s[2], wall_bands or [], tols)
                      for s in raw_profile]
            proven = [proof is not None for proof in proofs]
            profile = list(raw_profile)
            for j, ok in enumerate(proven):
                if ok:
                    continue
                sub = profile[j]
                sub_along = ((sub[0][0], sub[1][0]) if a[1] == b[1]
                             else (sub[0][1], sub[1][1]))
                # Evidence priority is intentional.  A source-bound face pair is
                # this interval's own thickness evidence, so it is tried before
                # donor-collapse can borrow a neighbouring cap/jamb proof.  This
                # makes local evidence replace borrowed evidence when both routes
                # are available; it is not an incidental loop-order side effect.
                binding = _face_pair_binding_for_edge(
                    a, b, nx, ny, sub_along, src, face_pairs, wall_lines, tols)
                if binding is not None:
                    if not _face_pair_binding_is_consistent(
                            binding, wall_lines, wall_line_layers or {}, tols):
                        _add(diags, _diag(
                            "tarch_wall_thickness_unevidenced",
                            handles=binding.evidence.proof_handles,
                            points_dxf_mm=[sub[0], sub[1]],
                            context={"thickness_native": binding.thickness_native,
                                     "reason": "face_pair_bookkeeping_inconsistent"}))
                    else:
                        mid = ((sub[0][0] + sub[1][0]) / 2.0,
                               (sub[0][1] + sub[1][1]) / 2.0)
                        exit_pt = Point(mid[0] + nx * binding.thickness_native,
                                        mid[1] + ny * binding.thickness_native)
                        basis: EdgeBasis = ("outer_skin"
                            if footprint.exterior.distance(exit_pt) <= tols.node_join_native
                            else "wall_axis")
                        profile[j] = (sub[0], sub[1], binding.thickness_native, basis)
                        proofs[j] = binding.evidence
                        proven[j] = True
                        continue
                left = next((q for q in range(j - 1, -1, -1) if proven[q]), None)
                right = next((q for q in range(j + 1, len(profile)) if proven[q]), None)
                donor = left if right is None else right if left is None else (
                    left if profile[left][2:] == profile[right][2:] else None)
                if donor is None:
                    _add(diags, _diag("tarch_wall_thickness_unevidenced", points_dxf_mm=[profile[j][0], profile[j][1]],
                                      context={"thickness_native": profile[j][2], "reason": "event_interval_unproven"}))
                    continue
                profile[j] = (profile[j][0], profile[j][1], profile[donor][2], profile[donor][3])
                proofs[j] = proofs[donor]
                proven[j] = True
            merged: list[tuple[tuple, tuple, float, EdgeBasis]] = []
            merged_proofs: list[ThicknessEvidenceV1 | None] = []
            for sub, proof in zip(profile, proofs):
                if merged and merged[-1][2:] == sub[2:]:
                    merged[-1] = (merged[-1][0], sub[1], sub[2], sub[3])
                    if merged_proofs[-1] is None:
                        merged_proofs[-1] = proof
                else:
                    merged.append(sub)
                    merged_proofs.append(proof)
            subs_per_edge.append(merged)
            proofs_per_edge.append(merged_proofs)
            # per-cavity-edge source ancestry (same for every sub of this cavity edge)
            edge_sources.append(src)
        offsets_per_edge = [[_offset_for(s[3], s[2]) for s in subs] for subs in subs_per_edge]

        zone_verts: list[tuple[float, float]] = []
        edges: list[_ZoneEdgeRec] = []
        for i in range(n):
            subs = subs_per_edge[i]
            offs = offsets_per_edge[i]
            nx, ny = normals[i]
            # corner_i (start of this edge's boundary); appended once (as corner_0),
            # thereafter it is the previous edge's appended end corner.
            if i == 0:
                zone_verts.append(_corner(c[0], normals[n - 1], offsets_per_edge[n - 1][-1],
                                          (nx, ny), offs[0]))
            # interior thickness-change splits -> wall sub-edge + step edge
            src = edge_sources[i]
            for j in range(len(subs) - 1):
                split_a = subs[j][1]
                proof = proofs_per_edge[i][j]
                if proof is None:
                    _add(diags, _diag("tarch_wall_thickness_unevidenced", handles=list(src),
                                      points_dxf_mm=[split_a],
                                      context={"thickness_native": subs[j][2]}))
                edges.append(_ZoneEdgeRec(nx, ny, subs[j][3], subs[j][2], offs[j],
                                          thickness_evidence=proof, source_handles=list(src)))
                zone_verts.append(_shift(split_a, nx, ny, offs[j]))
                edges.append(_ZoneEdgeRec(-ny, nx, None, None, 0.0))   # step, offset 0
                zone_verts.append(_shift(split_a, nx, ny, offs[j + 1]))
            # last sub -> end corner (start of next cavity edge)
            ni = (i + 1) % n
            end = _corner(c[ni], (nx, ny), offs[-1], normals[ni], offsets_per_edge[ni][0])
            proof = proofs_per_edge[i][-1]
            if proof is None:
                _add(diags, _diag("tarch_wall_thickness_unevidenced", handles=list(src),
                                  points_dxf_mm=[a, b],
                                  context={"thickness_native": subs[-1][2]}))
            edges.append(_ZoneEdgeRec(nx, ny, subs[-1][3], subs[-1][2], offs[-1],
                                      thickness_evidence=proof, source_handles=list(src)))
            if i != n - 1:
                zone_verts.append(end)
        zone_verts = [(round(vx, 6), round(vy, 6)) for vx, vy in zone_verts]
        poly = Polygon(zone_verts)
        if not poly.is_valid or poly.geom_type != "Polygon":
            _add(diags, _diag("tarch_edge_thickness_inconsistent", points_dxf_mm=zone_verts,
                              context={"reason": "expanded_polygon_invalid"}))
            continue
        for i, edge in enumerate(edges):
            edge.p1 = zone_verts[i]
            edge.p2 = zone_verts[(i + 1) % len(zone_verts)]
        zones.append(ZoneExpansion(
            cavity_id=f"c_{k:02d}", polygon=poly, vertices=zone_verts, edges=edges,
            seed_native=claim["seed_native"], area_m2=poly.area * mpu * mpu,
            node_join_tolerance_native=tols.node_join_native))
    return zones


# --------------------------------------------------------------------------- #
# G8 — INDEPENDENT reconstruction gate (the主保险)
# --------------------------------------------------------------------------- #
def g8_reconstruct_wall_region(zones: list[ZoneExpansion]) -> Any:
    """Rebuild the wall region from the OUTPUT zones + each edge's recorded basis+
    thickness ONLY (plan §1 G8 reinforcement, dispatch §2 #5).  Never reads S5's
    WallRegion or its derivatives.

    The normal is recomputed from persisted output ``p1 -> p2`` and offset from
    ``basis + thickness``.  In particular this function intentionally does not
    read ``nx``, ``ny`` or ``offset_native``: those are forward-pass caches and
    using either turns G8 into a tautological inverse.
    """
    zone_union = unary_union([z.polygon for z in zones]) if zones else Polygon()
    cav_recon: list[Any] = []
    for z in zones:
        e = z.edges
        m = len(e)
        if not m or any(edge.p1 is None or edge.p2 is None for edge in e):
            continue
        normals = [_outward_normal(edge.p1, edge.p2) for edge in e]
        offsets = [(_offset_for(edge.basis, edge.thickness_native)
                    if edge.basis is not None and edge.thickness_native is not None else 0.0)
                   for edge in e]
        cverts = []
        for i in range(m):
            vx, vy = e[i].p1
            pn, cn = normals[i - 1], normals[i]
            vx -= pn[0] * offsets[i - 1] + cn[0] * offsets[i]
            vy -= pn[1] * offsets[i - 1] + cn[1] * offsets[i]
            cverts.append((round(vx, 6), round(vy, 6)))
        poly = Polygon(cverts)
        if poly.is_valid and poly.geom_type == "Polygon" and not poly.is_empty:
            cav_recon.append(poly)
    cav_union = unary_union(cav_recon) if cav_recon else Polygon()
    return zone_union.difference(cav_union)


def _same_wall_consistency(zones: list[ZoneExpansion], tols: _Tols) -> tuple[bool, list[dict]]:
    """Pair back-to-back output edges by *overlap subinterval*, never whole edge.

    A long edge may face several shorter neighbour edges at T/cross junctions;
    every positive collinear overlap becomes its own evidence item.
    """
    records = []
    for zi, zone in enumerate(zones):
        for ei, edge in enumerate(zone.edges):
            if edge.basis is None or edge.thickness_native is None or edge.p1 is None or edge.p2 is None:
                continue
            nx, ny = _outward_normal(edge.p1, edge.p2)
            axis = "x" if edge.p1[1] == edge.p2[1] else "y"
            coord = edge.p1[1] if axis == "x" else edge.p1[0]
            span = sorted((edge.p1[0], edge.p2[0])) if axis == "x" else sorted((edge.p1[1], edge.p2[1]))
            records.append((zi, ei, edge, nx, ny, axis, coord, span))
    pairs: list[dict] = []
    for i, left in enumerate(records):
        for right in records[i + 1:]:
            zi, ei, e, nx, ny, axis, coord, span = left
            zj, ej, f, fx, fy, faxis, fcoord, fspan = right
            if zi == zj or axis != faxis or abs(coord - fcoord) > tols.axis_align_native:
                continue
            if abs(nx + fx) > 1e-12 or abs(ny + fy) > 1e-12:
                continue
            lo, hi = max(span[0], fspan[0]), min(span[1], fspan[1])
            if hi - lo <= tols.node_join_native:
                continue
            ok = e.basis == f.basis and abs(e.thickness_native - f.thickness_native) <= tols.axis_align_native
            pairs.append({"left": {"zone": zi, "edge": ei, "basis": e.basis, "thickness_native": e.thickness_native},
                          "right": {"zone": zj, "edge": ej, "basis": f.basis, "thickness_native": f.thickness_native},
                          "axis": axis, "coord_native": coord, "overlap_native": [lo, hi], "consistent": ok})
    return all(p["consistent"] for p in pairs), pairs


# --------------------------------------------------------------------------- #
# G4 — outer-skin gap conservation (exterior openings == outer-skin gaps)
# --------------------------------------------------------------------------- #
def _outer_skin_gap_count(p1: P1PlanViewGeometry, footprint: Any) -> int:
    """Count gaps in the RAW (unfilled) outer-skin wall lines along each exterior
    ring edge.  Each exterior opening breaks the outer skin once; the count must
    equal the exterior opening count (plan §5.2 G4 / SURVEY §3.2: 14==14)."""
    if footprint.is_empty or footprint.geom_type != "Polygon":
        return -1
    raw = list(footprint.exterior.coords)
    # The polygonized outer-skin ring carries a redundant collinear vertex at every
    # opening jamb: S4's opening fills close the wall gaps, so unary_union's exterior
    # boundary runs straight through each jamb with an extra vertex.  Counting gaps
    # per raw sub-edge would split each building side into many pieces and miscount
    # (sm24 west wall alone: 1 true edge -> ~10 jamb sub-edges -> ~10 false gaps).
    # Collapse to the true corner set first (sm24: 32 -> 4), then iterate sides.
    if raw and raw[0] == raw[-1]:
        raw = raw[:-1]
    ring = _clean_collinear([(p[0], p[1]) for p in raw])
    gaps = 0
    for a, b in zip(ring, ring[1:] + ring[:1]):
        if a[0] != b[0] and a[1] != b[1]:
            continue  # profile only supports orthogonal rings
        if a[1] == b[1]:  # horizontal ring edge at y
            yc, lo, hi = a[1], min(a[0], b[0]), max(a[0], b[0])
            segs = sorted((min(x0, x1), max(x0, x1)) for _, x0, y0, x1, y1 in p1.wall_lines
                          if y0 == yc and y1 == yc
                          and not (max(x0, x1) <= lo or min(x0, x1) >= hi))
        else:            # vertical ring edge at x
            xc, lo, hi = a[0], min(a[1], b[1]), max(a[1], b[1])
            segs = sorted((min(y0, y1), max(y0, y1)) for _, x0, y0, x1, y1 in p1.wall_lines
                          if x0 == xc and x1 == xc
                          and not (max(y0, y1) <= lo or min(y0, y1) >= hi))
        # merge spans, count uncovered gaps strictly inside (lo, hi)
        merged = []
        for s in segs:
            if merged and s[0] <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], s[1]))
            else:
                merged.append(list(s))
        cursor = lo
        for s0, s1 in merged:
            if s0 - cursor > 1.0:
                gaps += 1
            cursor = max(cursor, s1)
        if hi - cursor > 1.0:
            gaps += 1
    return gaps


# --------------------------------------------------------------------------- #
# P2 gate assembly (G4/G6/G7/G8/G9/G10) — G1/G2/G3/G5 come from P1
# --------------------------------------------------------------------------- #
def _zone_polygon_world(z: ZoneExpansion, affine: Affine2D) -> list[tuple[float, float]]:
    return [_to_world(v, affine) for v in z.vertices]


def _build_p2_gates(p1: P1PlanViewGeometry, cavities, wall_region, footprint,
                    zones: list[ZoneExpansion], claims: list[dict],
                    near_threshold: list[dict], request, plan_view, tols,
                    diags: list[ConversionDiagnosticV1]) -> list[GateResultV1]:
    gates: list[GateResultV1] = []
    affine = plan_view.world_from_source_m
    mpu = tols.metres_per_unit

    # carry P1 gates forward
    gates.extend(p1.gates)

    # G4 outer-skin gap conservation
    ext_openings = [o for o in p1.openings if o.classification == "exterior"]
    skin_gaps = _outer_skin_gap_count(p1, footprint)
    g4 = skin_gaps >= 0 and skin_gaps == len(ext_openings)
    if not g4 and skin_gaps >= 0:
        fp_pt = footprint.representative_point()
        _add(diags, _diag("tarch_opening_skin_gap_mismatch",
                          points_dxf_mm=[(fp_pt.x, fp_pt.y)],
                          context={"exterior_openings": len(ext_openings), "outer_skin_gaps": skin_gaps}))
    gates.append(GateResultV1(id="G4", name="outer-skin gap conservation", passed=g4,
        evidence={"exterior_openings": len(ext_openings), "outer_skin_gaps": skin_gaps}))

    # G6 cavity claim + count + near-threshold承重 list
    count_ok = len(cavities) == plan_view.zone_intent.expected_count
    unclaimed = len(cavities) - len(claims)
    near_pending = bool(near_threshold)
    g6 = count_ok and unclaimed <= 0 and not near_pending and not any(
        d.code == "tarch_cavity_count_mismatch" for d in diags)
    gates.append(GateResultV1(id="G6", name="cavity claim + count", passed=g6,
        evidence={"cavity_count": len(cavities),
                  "expected_count": plan_view.zone_intent.expected_count,
                  "claimed": len(claims),
                  "near_threshold_faces": near_threshold,
                  "human_confirmation_required": near_pending}))

    # G7 tiling: ∪zones ≡ footprint, no pairwise overlap (plan §4 S4/G7)
    zone_polys = [z.polygon for z in zones]
    zone_union = unary_union(zone_polys) if zone_polys else Polygon()
    symdiff_geom = zone_union.symmetric_difference(footprint)
    symdiff = symdiff_geom.area * mpu * mpu
    overlap = sum(zone_polys[i].intersection(zone_polys[j]).area
                  for i in range(len(zone_polys)) for j in range(i + 1, len(zone_polys))) * mpu * mpu
    g7 = symdiff <= tols.topo_area_m2 and overlap <= tols.topo_area_m2
    if not g7:
        loc = (symdiff_geom.representative_point() if not symdiff_geom.is_empty
               else footprint.representative_point())
        _add(diags, _diag("tarch_zone_tiling_residual",
                          points_dxf_mm=[(loc.x, loc.y)],
                          context={"symmetric_diff_m2": symdiff, "pairwise_overlap_m2": overlap}))
    gates.append(GateResultV1(id="G7", name="zone tiling", passed=g7,
        evidence={"zone_count": len(zones), "symmetric_diff_m2": symdiff,
                  "pairwise_overlap_m2": overlap}))

    # G8 INDEPENDENT reconstruction: rebuilt wall region vs measured wall region
    g8_passed = False
    g8_sd = None
    if zones and not wall_region.is_empty:
        recon = g8_reconstruct_wall_region(zones)
        g8_sd_geom = recon.symmetric_difference(wall_region)
        g8_sd = g8_sd_geom.area * mpu * mpu
        g8_passed = g8_sd <= tols.topo_area_m2
        if not g8_passed:
            loc = (g8_sd_geom.representative_point() if not g8_sd_geom.is_empty
                   else wall_region.representative_point())
            _add(diags, _diag("tarch_reconstruction_residual",
                              points_dxf_mm=[(loc.x, loc.y)],
                              context={"symmetric_diff_m2": g8_sd,
                                       "rebuilt_area_m2": recon.area * mpu * mpu,
                                       "measured_area_m2": wall_region.area * mpu * mpu}))
    walls_ok, wall_pairs = _same_wall_consistency(zones, tols)
    conflicts = [p for p in wall_pairs if not p["consistent"]]
    if conflicts:
        first = conflicts[0]
        _add(diags, _diag("tarch_edge_thickness_inconsistent",
                          context={"reason": "back_to_back_zone_edge_mismatch",
                                   "conflicts": conflicts},
                          points_dxf_mm=[(first["coord_native"], first["overlap_native"][0])]))
    g8_passed = g8_passed and walls_ok
    gates.append(GateResultV1(id="G8", name="independent wall-region reconstruction",
        passed=g8_passed, evidence={"symmetric_diff_m2": g8_sd,
                                    "same_wall_pairs": wall_pairs,
                                    "same_wall_conflict_count": len(conflicts)}))

    # G9 v3 preflight + G10 human-review overlay are emitted by the orchestrator
    # after S9 builds the augmented DXF / manifest / overlay (they need those artefacts).
    return _apply_test_neuter(gates)


# --------------------------------------------------------------------------- #
# S9 — persist: augmented DXF (appended GTV3_* layers) + manifest + report +
# source_map + human-review overlay.  §0.1 方案A: write into staging only.
# --------------------------------------------------------------------------- #
GTV3_FOOTPRINT_LAYER = "GTV3_FOOTPRINT"
GTV3_ZONE_LAYER = "GTV3_ZONE"
GTV3_OPENING_LAYER = "GTV3_OPENING"
GTV3_ELEV_OPENING_LAYER = "GTV3_ELEV_OPENING"


@dataclass(frozen=True)
class _ElevationRecord:
    view_id: str
    facade: str
    kind: Literal["window", "door"]
    rect: tuple[float, float, float, float]
    raw_handles: tuple[str, ...]
    structural_handles: tuple[str, ...]
    datum_handle: str
    datum_start: tuple[float, float]
    datum_end: tuple[float, float]
    declared_lo_endpoint: str
    generated_handle: str = ""


def elevation_block_definition_sha256(doc, name: str) -> str:
    """Canonical, order-independent fingerprint of all direct block entities.

    This deliberately includes non-structural entities: adding a CIRCLE/TEXT
    cannot bypass the request-bound exhaustive role list merely because it is
    excluded from structural extrema.
    """
    block = doc.blocks.get(name)
    def freeze(value):
        if hasattr(value, "x") and hasattr(value, "y"):
            return [float(value.x), float(value.y), float(getattr(value, "z", 0.0))]
        if isinstance(value, (list, tuple)):
            return [freeze(v) for v in value]
        if isinstance(value, dict):
            return {str(k): freeze(v) for k, v in sorted(value.items()) if k not in {"owner", "handle"}}
        return str(value) if not isinstance(value, (str, int, float, bool, type(None))) else value
    entities = []
    for entity in block:
        data = entity.dxfattribs()
        # ezdxf legitimately omits zero-valued optional defaults after a save;
        # canonicalize that container representation, not the geometry.
        if entity.dxftype() == "LWPOLYLINE":
            data.setdefault("const_width", 0.0)
        # dxfattribs omits polyline vertices; they are semantic geometry.
        if entity.dxftype() == "LWPOLYLINE":
            data["points"] = [list(map(float, p[:5])) for p in entity.get_points("xyseb")]
        entities.append({"handle": entity.dxf.handle, "type": entity.dxftype(),
                         "layer": entity.dxf.layer, "tags": freeze(data)})
    payload = {"name": name, "base_point": freeze(block.block.dxf.base_point),
               "entities": sorted(entities, key=lambda item: item["handle"])}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n").hexdigest()


def _entity_text(entity) -> str:
    return entity.plain_text() if entity.dxftype() == "MTEXT" else str(getattr(entity.dxf, "text", ""))


def _inside(entity, clip: ClipBoxDxf) -> bool:
    box = ezdxf_bbox.extents([entity], fast=True)
    cx, cy = (box.extmin.x + box.extmax.x) / 2, (box.extmin.y + box.extmax.y) / 2
    return clip.xmin < cx < clip.xmax and clip.ymin < cy < clip.ymax


def _rect_from_lines(lines, q: float) -> tuple[float, float, float, float] | None:
    """Strict four-edge rectangle check after the converter's q=tau_node/10 snap."""
    edges = []
    for line in lines:
        a = (_quantize(float(line.dxf.start.x), q), _quantize(float(line.dxf.start.y), q))
        b = (_quantize(float(line.dxf.end.x), q), _quantize(float(line.dxf.end.y), q))
        if a == b or (a[0] != b[0] and a[1] != b[1]):
            return None
        edges.append((a, b))
    if len(edges) != 4:
        return None
    degree: dict[tuple[float, float], int] = {}
    for a, b in edges:
        degree[a] = degree.get(a, 0) + 1; degree[b] = degree.get(b, 0) + 1
    if len(degree) != 4 or set(degree.values()) != {2}:
        return None
    xs, ys = [p[0] for p in degree], [p[1] for p in degree]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    expected = {tuple(sorted(((x0,y0),(x1,y0)))), tuple(sorted(((x1,y0),(x1,y1)))),
                tuple(sorted(((x0,y1),(x1,y1)))), tuple(sorted(((x0,y0),(x0,y1))))}
    return (x0, y0, x1, y1) if {tuple(sorted(edge)) for edge in edges} == expected and x0 < x1 and y0 < y1 else None


def _line_components(lines, q: float) -> list[list[Any]]:
    endpoints = [{(_quantize(float(e.dxf.start.x),q), _quantize(float(e.dxf.start.y),q)),
                  (_quantize(float(e.dxf.end.x),q), _quantize(float(e.dxf.end.y),q))} for e in lines]
    pending = set(range(len(lines))); groups = []
    while pending:
        todo = [pending.pop()]; group = []
        while todo:
            i = todo.pop(); group.append(lines[i]); neighbours = [j for j in pending if endpoints[i] & endpoints[j]]
            for j in neighbours: pending.remove(j); todo.append(j)
        groups.append(group)
    return groups


_ResolvedOpeningCarrier = tuple[
    str,
    Literal["window", "door"],
    tuple[float, float, float, float],
    tuple[str, ...],
    tuple[str, ...],
]
_OpeningCarrierResolver = Callable[
    [Any, OpeningCarrierRuleV1, Any, _Tols],
    tuple[list[_ResolvedOpeningCarrier], list[ConversionDiagnosticV1]],
]


def _resolve_connected_line_group_rect(
        view, rule: OpeningCarrierRuleV1, msp, tols: _Tols
        ) -> tuple[list[_ResolvedOpeningCarrier], list[ConversionDiagnosticV1]]:
    """Resolve only request-matched LINE components through the existing geometry path."""
    lines = [entity for entity in msp
             if entity.dxftype() == rule.match.entity_type
             and entity.dxf.layer in rule.match.layers
             and _inside(entity, view.clip_box_dxf)]
    resolved: list[_ResolvedOpeningCarrier] = []
    diagnostics: list[ConversionDiagnosticV1] = []
    for group in _line_components(lines, tols.quant_native):
        handles = tuple(sorted(entity.dxf.handle for entity in group))
        rect = _rect_from_lines(group, tols.quant_native)
        if rect is None:
            _add(diagnostics, _diag(
                "tarch_elevation_opening_component_invalid",
                handles=list(handles),
                context={"view_id": view.id, "carrier_id": rule.carrier_id}))
            continue
        resolved.append((rule.carrier_id, rule.opening_kind, rect,
                         handles, handles))
    return resolved, diagnostics


@dataclass(frozen=True)
class _CarrierPoint:
    x: float
    y: float


@dataclass(frozen=True)
class _CarrierLineDxf:
    start: _CarrierPoint
    end: _CarrierPoint


@dataclass(frozen=True)
class _CarrierLine:
    dxf: _CarrierLineDxf


def _carrier_line(start: tuple[float, float], end: tuple[float, float]) -> _CarrierLine:
    return _CarrierLine(_CarrierLineDxf(_CarrierPoint(*start), _CarrierPoint(*end)))


def _closed_polyline_rect(entity, q: float, matrix=None
                          ) -> tuple[float, float, float, float] | None:
    points = list(entity.get_points("xyseb"))
    if not entity.closed or len(points) != 4 or any(point[4] != 0 for point in points):
        return None
    coords = []
    for point in points:
        if matrix is None:
            coords.append((float(point[0]), float(point[1])))
        else:
            world = matrix.transform((point[0], point[1], 0.0))
            coords.append((float(world.x), float(world.y)))
    lines = [_carrier_line(coords[index], coords[(index + 1) % len(coords)])
             for index in range(len(coords))]
    return _rect_from_lines(lines, q)


def _resolve_closed_polyline_rect(
        view, rule: OpeningCarrierRuleV1, msp, tols: _Tols
        ) -> tuple[list[_ResolvedOpeningCarrier], list[ConversionDiagnosticV1]]:
    """Resolve each exactly matched entity as one declared closed rectangle."""
    entities = [entity for entity in msp
                if entity.dxftype() == rule.match.entity_type
                and entity.dxf.layer in rule.match.layers
                and _inside(entity, view.clip_box_dxf)]
    resolved: list[_ResolvedOpeningCarrier] = []
    diagnostics: list[ConversionDiagnosticV1] = []
    for entity in entities:
        rect = _closed_polyline_rect(entity, tols.quant_native)
        if rect is None:
            _add(diagnostics, _diag(
                "tarch_elevation_opening_component_invalid",
                handles=[entity.dxf.handle],
                context={"view_id": view.id, "carrier_id": rule.carrier_id}))
            continue
        handles = (entity.dxf.handle,)
        resolved.append((rule.carrier_id, rule.opening_kind, rect,
                         handles, handles))
    return resolved, diagnostics


def _resolve_block_entity_rect(
        view, rule: OpeningCarrierRuleV1, msp, tols: _Tols
        ) -> tuple[list[_ResolvedOpeningCarrier], list[ConversionDiagnosticV1]]:
    """Resolve request-role-bound block entities after the INSERT matrix transform."""
    inserts = [entity for entity in msp
               if entity.dxftype() == rule.match.entity_type
               and entity.dxf.layer in rule.match.layers
               and (rule.match.block_name_exact is None
                    or entity.dxf.name == rule.match.block_name_exact)
               and _inside(entity, view.clip_box_dxf)]
    resolved: list[_ResolvedOpeningCarrier] = []
    diagnostics: list[ConversionDiagnosticV1] = []
    roles = {item.entity_handle: item.role
             for item in (rule.outline.block_entity_roles or [])}
    for insert in inserts:
        block_entities = list(insert.doc.blocks.get(insert.dxf.name))
        block_handles = {entity.dxf.handle for entity in block_entities}
        fingerprint_bad = (
            rule.match.block_definition_sha256 is not None
            and elevation_block_definition_sha256(
                insert.doc, insert.dxf.name) != rule.match.block_definition_sha256)
        if fingerprint_bad or set(roles) != block_handles:
            _add(diagnostics, _diag(
                "tarch_elevation_door_block_drift",
                handles=[insert.dxf.handle],
                context={"view_id": view.id, "carrier_id": rule.carrier_id,
                         "reason": ("block_definition_sha256_mismatch"
                                    if fingerprint_bad else "block_entity_roles_drift")}))
            continue
        structural = [entity for entity in block_entities
                      if roles[entity.dxf.handle] == "structural_outline"]
        structural_handles = tuple(sorted(entity.dxf.handle for entity in structural))
        matrix = insert.matrix44()
        rect = None
        if len(structural) == 1 and structural[0].dxftype() == "LWPOLYLINE":
            rect = _closed_polyline_rect(
                structural[0], tols.quant_native, matrix)
        elif structural and all(entity.dxftype() == "LINE" for entity in structural):
            lines = []
            for entity in structural:
                start = matrix.transform(entity.dxf.start)
                end = matrix.transform(entity.dxf.end)
                lines.append(_carrier_line(
                    (float(start.x), float(start.y)),
                    (float(end.x), float(end.y))))
            rect = _rect_from_lines(lines, tols.quant_native)
        if rect is None:
            _add(diagnostics, _diag(
                "tarch_elevation_door_structure_invalid",
                handles=[insert.dxf.handle],
                context={"view_id": view.id, "carrier_id": rule.carrier_id}))
            continue
        resolved.append((rule.carrier_id, rule.opening_kind, rect,
                         (insert.dxf.handle,), structural_handles))
    return resolved, diagnostics


# One registry is the only dispatch point.  Later carrier dialects add one resolver
# entry; existing resolver branches remain untouched.
_OPENING_CARRIER_RESOLVERS: dict[str, _OpeningCarrierResolver] = {
    "block_entity_rect": _resolve_block_entity_rect,
    "closed_polyline_rect": _resolve_closed_polyline_rect,
    "connected_line_group_rect": _resolve_connected_line_group_rect,
}


def _resolve_opening_carriers(
        view, rules: list[OpeningCarrierRuleV1], msp, tols: _Tols
        ) -> tuple[list[_ResolvedOpeningCarrier], list[ConversionDiagnosticV1]]:
    """Execute only the opening carrier dialects explicitly declared by the request."""
    resolved: list[_ResolvedOpeningCarrier] = []
    diagnostics: list[ConversionDiagnosticV1] = []
    for rule in rules:
        resolver = _OPENING_CARRIER_RESOLVERS.get(rule.outline.kind)
        if resolver is None:
            raise ValueError(
                f"tarch_elevation_opening_carrier_kind_unsupported:{rule.outline.kind}")
        carriers, carrier_diagnostics = resolver(view, rule, msp, tols)
        resolved.extend(carriers)
        diagnostics.extend(carrier_diagnostics)
    return resolved, diagnostics


def _shared_elevation_dialect(request: TarchConversionRequestV1) -> TarchDialectRulesV1:
    dialect = None
    for plan_view in request.plan_views:
        if dialect is None:
            dialect = plan_view.dialect_rules
        elif plan_view.dialect_rules != dialect:
            raise ValueError("tarch_multifloor_dialect_mismatch")
    if dialect is None:  # pragma: no cover - request schema requires a plan view
        raise ValueError("tarch_multifloor_plan_floor_mismatch")
    return dialect


def _translate_legacy_opening_carrier_rules(
        request: TarchConversionRequestV1,
        view: DatumBoundNamedElevationViewIntentV3) -> list[OpeningCarrierRuleV1]:
    """Pure migration: old selectors become rules and never produce geometry."""
    if view.window_selector.entity_types != ["LINE"]:
        raise ValueError("tarch_legacy_window_selector_not_translatable")
    rules = [OpeningCarrierRuleV1(
        carrier_id=f"legacy.window.{view.id}", opening_kind="window",
        match={"entity_type": "LINE", "layers": list(view.window_selector.layers)},
        outline={"kind": "connected_line_group_rect"})]
    if view.door_selector is None:
        return rules
    if view.door_selector.entity_types != ["INSERT"]:
        raise ValueError("tarch_legacy_door_selector_not_translatable")
    dialect = _shared_elevation_dialect(request)
    if not dialect.elevation_door_block_rules:
        raise ValueError("tarch_legacy_door_rules_not_translatable")
    for index, legacy in enumerate(dialect.elevation_door_block_rules):
        rules.append(OpeningCarrierRuleV1(
            carrier_id=f"legacy.door.{index}", opening_kind="door",
            match={
                "entity_type": "INSERT",
                "layers": list(view.door_selector.layers),
                "block_name_exact": legacy.block_name_exact,
                "block_definition_sha256": legacy.block_definition_sha256,
            },
            outline={
                "kind": "block_entity_rect",
                "block_entity_roles": [role.model_dump(mode="python")
                                       for role in legacy.entity_roles],
            },
            module_union_strategy="same_band_strict_union"))
    return rules


def _opening_carrier_rules_for_view(
        request: TarchConversionRequestV1,
        view: DatumBoundNamedElevationViewIntentV3) -> list[OpeningCarrierRuleV1]:
    if request.opening_carrier_rules is None:
        return _translate_legacy_opening_carrier_rules(request, view)
    return list(request.opening_carrier_rules)


def _translate_legacy_opening_ignore_selectors(
        _request: TarchConversionRequestV1,
        _view: DatumBoundNamedElevationViewIntentV3
        ) -> list[TarchEntitySelectorV1]:
    """Pure migration for signed requests; sm24 has no ledger exemptions."""
    return []


def _opening_ignore_selectors_for_view(
        request: TarchConversionRequestV1,
        view: DatumBoundNamedElevationViewIntentV3
        ) -> list[TarchEntitySelectorV1]:
    if request.ignore_selector is None:
        return _translate_legacy_opening_ignore_selectors(request, view)
    return list(request.ignore_selector)


def _audit_opening_carrier_consumption(
        view, rules: list[OpeningCarrierRuleV1],
        ignore_selectors: list[TarchEntitySelectorV1], msp,
        carriers: list[_ResolvedOpeningCarrier]
        ) -> list[ConversionDiagnosticV1]:
    """Account for every in-frame entity on a request-declared opening layer."""
    declared_layers = {
        layer for rule in rules for layer in rule.match.layers
    }
    ledger = {
        entity.dxf.handle: entity for entity in msp
        if entity.dxf.layer in declared_layers
        and _inside(entity, view.clip_box_dxf)
    }
    consumers: dict[str, list[str]] = {handle: [] for handle in ledger}
    for carrier_id, _kind, _rect, raw_handles, _structural_handles in carriers:
        for handle in raw_handles:
            if handle in consumers:
                consumers[handle].append(carrier_id)

    diagnostics: list[ConversionDiagnosticV1] = []
    double_consumed = sorted(
        handle for handle, carrier_ids in consumers.items()
        if len(carrier_ids) > 1)
    if double_consumed:
        _add(diagnostics, _diag(
            "tarch_elevation_entity_double_consumed",
            handles=double_consumed,
            context={
                "view_id": view.id,
                "consumers": {
                    handle: sorted(consumers[handle])
                    for handle in double_consumed
                },
            }))

    def explicitly_ignored(entity) -> bool:
        return any(
            entity.dxf.layer in selector.layers
            and entity.dxftype() in selector.entity_types
            for selector in ignore_selectors)

    unconsumed = sorted(
        handle for handle, entity in ledger.items()
        if not consumers[handle] and not explicitly_ignored(entity))
    if unconsumed:
        _add(diagnostics, _diag(
            "tarch_elevation_entities_unconsumed",
            handles=unconsumed,
            context={"view_id": view.id}))
    return diagnostics


def _rects_touch_or_intersect(
        first: tuple[float, float, float, float],
        second: tuple[float, float, float, float], q: float) -> bool:
    return (first[0] <= second[2] + q and first[2] + q >= second[0]
            and first[1] <= second[3] + q and first[3] + q >= second[1])


def _same_z_band(first: tuple[float, float, float, float],
                 second: tuple[float, float, float, float], q: float) -> bool:
    return (abs(first[1] - second[1]) <= q
            and abs(first[3] - second[3]) <= q)


def _horizontal_gap(first: tuple[float, float, float, float],
                    second: tuple[float, float, float, float]) -> float:
    return max(first[0] - second[2], second[0] - first[2], 0.0)


def _merge_door_carriers(
        modules: list[_ResolvedOpeningCarrier], rules: list[OpeningCarrierRuleV1],
        tols: _Tols, view_id: str
        ) -> tuple[list[_ResolvedOpeningCarrier], list[ConversionDiagnosticV1]]:
    """Cluster by the signed policy, then use one shared rectangular-union check."""
    if not modules:
        return [], []
    policies = {
        (rule.module_union_strategy, rule.module_union_min_gap_m)
        for rule in rules if rule.opening_kind == "door"
    }
    if len(policies) != 1:
        raise ValueError("tarch_elevation_door_module_union_policy_ambiguous")
    strategy, min_gap_m = next(iter(policies))
    min_gap_native = (None if min_gap_m is None
                      else min_gap_m / tols.metres_per_unit)

    def shares_cluster(left: int, right: int) -> bool:
        first, second = modules[left][2], modules[right][2]
        if strategy == "same_band_strict_union":
            return (_same_z_band(first, second, tols.quant_native)
                    or _rects_touch_or_intersect(first, second, tols.quant_native))
        if _rects_touch_or_intersect(first, second, tols.quant_native):
            return True
        return (_same_z_band(first, second, tols.quant_native)
                and _horizontal_gap(first, second) < min_gap_native)

    merged: list[_ResolvedOpeningCarrier] = []
    diagnostics: list[ConversionDiagnosticV1] = []
    pending = set(range(len(modules)))
    while pending:
        index = pending.pop()
        cluster = [index]
        changed = True
        while changed:
            changed = False
            for candidate in list(pending):
                if any(shares_cluster(candidate, member) for member in cluster):
                    pending.remove(candidate)
                    cluster.append(candidate)
                    changed = True
        rects = [modules[item][2] for item in cluster]
        x0, y0 = min(rect[0] for rect in rects), min(rect[1] for rect in rects)
        x1, y1 = max(rect[2] for rect in rects), max(rect[3] for rect in rects)
        raw_handles = tuple(sorted(
            handle for item in cluster for handle in modules[item][3]))
        structural_handles = tuple(sorted(
            handle for item in cluster for handle in modules[item][4]))
        if (sum((rect[2] - rect[0]) * (rect[3] - rect[1]) for rect in rects)
                != (x1 - x0) * (y1 - y0)):
            _add(diagnostics, _diag(
                "tarch_elevation_door_structure_invalid",
                handles=list(raw_handles),
                context={"view_id": view_id,
                         "carrier_ids": sorted({modules[item][0]
                                                for item in cluster}),
                         "module_union_strategy": strategy}))
            continue
        merged.append((modules[index][0], "door", (x0, y0, x1, y1),
                       raw_handles, structural_handles))
    return merged, diagnostics


def _v3_elevation_records(augmented_dxf: Path, request: TarchConversionRequestV1,
                          plan_gt, tols: _Tols) -> tuple[list[_ElevationRecord], list[ConversionDiagnosticV1]]:
    """E0--E4: validate named views/datum anchors and produce normalized evidence.

    It does not infer a datum or handedness: both are consumed only from the
    signed v3 request and verified against the plan projection before opening
    geometry is touched.
    """
    doc = ezdxf.readfile(str(augmented_dxf)); msp = doc.modelspace(); diags = []; records = []
    segment_by_id = {s.id: s for floor in plan_gt.floors for s in floor.boundary_segments}
    plan_openings = []
    for opening in plan_gt.openings:
        seg = segment_by_id[opening.boundary_segment_id]
        plan_openings.append((opening, seg.facade_family))
    dialect = _shared_elevation_dialect(request)
    for view in request.elevation_views:
        if not isinstance(view, DatumBoundNamedElevationViewIntentV3):
            continue
        frame = next((e for e in msp if e.dxf.handle == view.frame_entity_handle), None)
        title = next((e for e in msp if e.dxf.handle == view.title_entity_handle), None)
        mapped = dialect.elevation_title_map.get(view.frame_title)
        if frame is None or title is None or not _inside(title, view.clip_box_dxf) or _entity_text(title).strip() != view.frame_title or mapped != view.facade_family:
            _add(diags, _diag("tarch_elevation_title_mismatch", handles=[view.frame_entity_handle], context={"view_id": view.id})); continue
        facade_segments = [s for s in segment_by_id.values() if s.facade_family == view.facade_family and s.floor_id in view.floor_ids]
        if not facade_segments:
            _add(diags, _diag("tarch_elevation_datum_invalid", handles=[view.frame_entity_handle])); continue
        plan_lo, plan_hi = min(s.world_along_interval.lo for s in facade_segments), max(s.world_along_interval.hi for s in facade_segments)
        if abs(abs(view.world_along_from_source_m.scale) - request.metres_per_unit) != 0 or abs(abs(view.world_z_from_source_m.scale) - request.metres_per_unit) != 0:
            _add(diags, _diag("tarch_elevation_z_transform_mismatch", handles=[view.frame_entity_handle])); continue
        axis = view.world_along_from_source_m.source_axis; z_axis = view.world_z_from_source_m.source_axis
        transform_along = lambda value: value * view.world_along_from_source_m.scale + view.world_along_from_source_m.offset
        # EVERY declared datum is verified, not just floor_datums[0]: two datums that
        # derive different offsets (spec §9.3) must BLOCK rather than let the first one
        # silently win.  The first datum is what the records then carry.
        datum_state: tuple | None = None
        datum_bad: ConversionDiagnosticV1 | None = None
        for candidate in view.floor_datums:
            ent = next((e for e in msp if e.dxf.handle == candidate.entity_handle), None)
            if ent is None:
                datum_bad = _diag("tarch_elevation_datum_missing", handles=[candidate.entity_handle]); break
            if ent.dxftype() != "LINE":
                datum_bad = _diag("tarch_elevation_datum_invalid", handles=[candidate.entity_handle]); break
            start, end = (float(ent.dxf.start.x), float(ent.dxf.start.y)), (float(ent.dxf.end.x), float(ent.dxf.end.y))
            a0, a1 = (start[0 if axis == "x" else 1], end[0 if axis == "x" else 1])
            z0, z1 = (start[0 if z_axis == "x" else 1], end[0 if z_axis == "x" else 1])
            floor = next(f for f in request.floors if f.id == candidate.floor_id)
            derived_offset = floor.z_floor_m - z0 * view.world_z_from_source_m.scale
            declared = a0 if candidate.world_along_lo_source_endpoint == "start" else a1
            other = a1 if candidate.world_along_lo_source_endpoint == "start" else a0
            endpoint_bad = abs(transform_along(declared) - plan_lo) > tols.node_join_m or abs(transform_along(other) - plan_hi) > tols.node_join_m
            if abs(z0 - z1) > tols.axis_align_native or abs(a0 - a1) <= tols.quant_native or abs(derived_offset - view.world_z_from_source_m.offset) > tols.node_join_m or endpoint_bad:
                datum_bad = _diag("tarch_elevation_along_direction_mismatch" if endpoint_bad else "tarch_elevation_z_transform_mismatch",
                                  handles=[candidate.entity_handle]); break
            if datum_state is None:
                datum_state = (candidate, start, end)
        if datum_bad is not None:
            _add(diags, datum_bad); continue
        datum, start, end = datum_state
        rules = _opening_carrier_rules_for_view(request, view)
        carriers, carrier_diagnostics = _resolve_opening_carriers(
            view, rules, msp, tols)
        diags.extend(carrier_diagnostics)
        diags.extend(_audit_opening_carrier_consumption(
            view, rules, _opening_ignore_selectors_for_view(request, view),
            msp, carriers))
        for _carrier_id, kind, rect, raw_handles, structural_handles in carriers:
            if kind != "window":
                continue
            records.append(_ElevationRecord(
                view.id, view.facade_family, kind, rect,
                raw_handles, structural_handles, datum.entity_handle,
                start, end, datum.world_along_lo_source_endpoint))
        doors, door_diagnostics = _merge_door_carriers(
            [carrier for carrier in carriers if carrier[1] == "door"],
            rules, tols, view.id)
        diags.extend(door_diagnostics)
        for _carrier_id, kind, rect, raw_handles, structural_handles in doors:
            records.append(_ElevationRecord(
                view.id, view.facade_family, kind, rect,
                raw_handles, structural_handles, datum.entity_handle,
                start, end, datum.world_along_lo_source_endpoint))
    return records, diags


_DETERMINISTIC_DXF_JULIAN_EPOCH = 2451544.5  # 2000-01-01T00:00:00Z
_DETERMINISTIC_DXF_GUID_NAMESPACE = uuid.UUID("cc8bd1cd-f1f1-5860-a932-aa9379bc773e")


def _deterministic_dxf_metadata(source_sha256: str, request_sha256: str) -> dict[str, str | float]:
    """Return write-time DXF metadata as a pure function of conversion inputs.

    ``ezdxf.Drawing.write()`` replaces several fields during every save.  The
    conversion artefact is hash-bound, so that normal editor metadata must not
    introduce a second, clock-dependent input into the conversion result.
    """
    seed = f"{source_sha256.lower()}:{request_sha256.lower()}"
    return {
        "$TDCREATE": _DETERMINISTIC_DXF_JULIAN_EPOCH,
        "$TDUCREATE": _DETERMINISTIC_DXF_JULIAN_EPOCH,
        "$TDUPDATE": _DETERMINISTIC_DXF_JULIAN_EPOCH,
        "$TDUUPDATE": _DETERMINISTIC_DXF_JULIAN_EPOCH,
        "$FINGERPRINTGUID": "{" + str(uuid.uuid5(_DETERMINISTIC_DXF_GUID_NAMESPACE,
                                                       f"fingerprint:{seed}")).upper() + "}",
        "$VERSIONGUID": "{" + str(uuid.uuid5(_DETERMINISTIC_DXF_GUID_NAMESPACE,
                                                   f"version:{seed}")).upper() + "}",
        "written_by_ezdxf": f"{ezdxf.__version__} @ tarch-deterministic:{seed}",
    }


def _apply_deterministic_dxf_metadata(doc, source_sha256: str, request_sha256: str) -> None:
    """Pin only converter-produced DXF save metadata after ezdxf updates it."""
    metadata = _deterministic_dxf_metadata(source_sha256, request_sha256)
    for name in ("$TDCREATE", "$TDUCREATE", "$TDUPDATE", "$TDUUPDATE",
                 "$FINGERPRINTGUID", "$VERSIONGUID"):
        doc.header[name] = metadata[name]
    doc.ezdxf_metadata()[WRITTEN_BY_EZDXF] = metadata["written_by_ezdxf"]


def _save_converter_augmented_dxf(doc, dest: Path, source_sha256: str,
                                  request_sha256: str) -> None:
    """Write a converter augmented DXF without global ezdxf state changes.

    This deliberately mirrors ezdxf's ASCII writer up to ``update_all()``, then
    replaces the four clock/GUID header values and the library write marker
    before exporting sections.  It is used exclusively by the converter's
    augmented-DXF path; DXF reading and all other ezdxf writers retain normal
    library behaviour.
    """
    doc.commit_pending_changes()
    if doc.dxfversion > DXF12:
        doc.classes.add_required_classes(doc.dxfversion)
    doc.update_all()
    _apply_deterministic_dxf_metadata(doc, source_sha256, request_sha256)
    handles = bool(doc.header.get("$HANDLING", 0)) if doc.dxfversion == DXF12 else True
    with Path(dest).open("wt", encoding=doc.output_encoding, errors="dxfreplace", newline="\n") as stream:
        doc.export_sections(TagWriter(stream, write_handles=handles, dxfversion=doc.dxfversion))


def _append_plan_geometry(doc, footprint: Any, zones: list[ZoneExpansion],
                          exterior_openings: list[ResolvedOpening],
                          ) -> tuple[dict[str, list[str]], dict[tuple[int, int], str]]:
    """Append one plan's canonical GTV3 geometry to an already-open document."""
    msp = doc.modelspace()
    handles: dict[str, list[str]] = {GTV3_FOOTPRINT_LAYER: [], GTV3_ZONE_LAYER: [], GTV3_OPENING_LAYER: []}

    # GTV3_FOOTPRINT: closed LWPOLYLINE = outer-skin exterior ring (native mm).
    # The polygonized outer-skin ring carries a redundant collinear vertex at every
    # opening jamb (S4 fills close the wall gaps, so the union exterior runs straight
    # through each jamb with a sub-mm extra vertex — see _outer_skin_gap_count).  Those
    # collapse to zero-length edges under v3's node-snap (dxf_short_edge), so drop them
    # first: only true corners remain, an identical polygon.
    if footprint.geom_type == "Polygon":
        raw = [(round(x, 6), round(y, 6)) for x, y in list(footprint.exterior.coords)[:-1]]
        ring = _clean_collinear(raw)
        e = msp.add_lwpolyline(ring, dxfattribs={"layer": GTV3_FOOTPRINT_LAYER}, close=True)
        handles[GTV3_FOOTPRINT_LAYER].append(e.dxf.handle)

    # GTV3_ZONE: one LINE per zoning edge not already carried in full by the footprint,
    # deduped by canonical coords.  Usually these are exactly the wall_axis edges.  At
    # a re-entrant outer_skin<->wall_axis transition, however, an outer_skin support
    # edge can extend past the footprint segment to the adjacent wall centreline.  It
    # must be emitted or the persisted topology has a short dangle even though G7 tiles.
    # The handle is recorded for every zone edge sharing the emitted source line.
    # Test the whole edge, not its minimum distance or GEOS's exact collinear
    # ``covers`` predicate.  The boundary band absorbs representation noise along
    # an otherwise coincident edge; any portion extending farther than the existing
    # node-join tolerance remains and must be emitted to close persisted topology.
    node_join_tolerances = {zone.node_join_tolerance_native for zone in zones}
    if len(node_join_tolerances) != 1 or None in node_join_tolerances:
        raise ValueError("persisted zones require one node-join tolerance")
    node_join_tolerance_native = next(iter(node_join_tolerances))
    footprint_boundary_band = footprint.exterior.buffer(node_join_tolerance_native)
    seen: dict[tuple[float, float, float, float], str] = {}
    zone_edge_handles: dict[tuple[int, int], str] = {}
    for z_idx, z in enumerate(zones):
        verts = z.vertices
        for i, edge in enumerate(z.edges):
            a, b = verts[i], verts[(i + 1) % len(verts)]
            if edge.basis is None:
                continue
            support_edge = LineString([a, b])
            if (edge.basis == "outer_skin"
                    and support_edge.difference(footprint_boundary_band).length
                    <= node_join_tolerance_native):
                continue
            key = (round(min(a[0], b[0]), 6), round(min(a[1], b[1]), 6),
                   round(max(a[0], b[0]), 6), round(max(a[1], b[1]), 6))
            if key in seen:
                zone_edge_handles[(z_idx, i)] = seen[key]
                continue
            e = msp.add_line((round(a[0], 6), round(a[1], 6)),
                             (round(b[0], 6), round(b[1], 6)),
                             dxfattribs={"layer": GTV3_ZONE_LAYER})
            seen[key] = e.dxf.handle
            handles[GTV3_ZONE_LAYER].append(e.dxf.handle)
            zone_edge_handles[(z_idx, i)] = e.dxf.handle

    # GTV3_OPENING: one closed LWPOLYLINE per EXTERIOR opening (clean rect, D2)
    for op in exterior_openings:
        x0, y0, x1, y1 = op.rect_dxf_mm
        e = msp.add_lwpolyline([(round(x0, 6), round(y0, 6)), (round(x1, 6), round(y0, 6)),
                                (round(x1, 6), round(y1, 6)), (round(x0, 6), round(y1, 6))],
                               dxfattribs={"layer": GTV3_OPENING_LAYER}, close=True)
        handles[GTV3_OPENING_LAYER].append(e.dxf.handle)

    return handles, zone_edge_handles


def _write_augmented_dxf(source_dxf: Path, dest: Path, footprint: Any,
                         zones: list[ZoneExpansion], exterior_openings: list[ResolvedOpening],
                         source_sha256: str, request_sha256: str,
                         ) -> tuple[dict[str, list[str]], dict[tuple[int, int], str]]:
    """Append GTV3_* layers to a copy of the source DXF, preserving every original
    handle (plan §8.1).  Returns the generated-entity handles per layer AND a
    ``(zone_index, edge_index) -> handle`` map for the GTV3_ZONE lines, so each
    emitted zoning line ties back to the exact zone edge (and its source ancestry)
    that generated it."""
    doc = ezdxf.readfile(str(source_dxf))
    for layer in (GTV3_FOOTPRINT_LAYER, GTV3_ZONE_LAYER, GTV3_OPENING_LAYER):
        if layer not in doc.layers:
            doc.layers.add(layer)
    handles, zone_edge_handles = _append_plan_geometry(
        doc, footprint, zones, exterior_openings)
    _save_converter_augmented_dxf(doc, dest, source_sha256, request_sha256)
    return handles, zone_edge_handles


@dataclass
class _PersistedPlan:
    plan_view: PlanViewIntentV1
    result: P2ConversionResult
    exterior_openings: list[ResolvedOpening]
    gtv3_handles: dict[str, list[str]]
    zone_edge_handles: dict[tuple[int, int], str]


def _write_multifloor_augmented_dxf(source_dxf: Path, dest: Path,
                                    plan_runs: list[tuple[PlanViewIntentV1, P2ConversionResult]],
                                    source_sha256: str, request_sha256: str) -> list[_PersistedPlan]:
    """Append every plan to one DXF while retaining canonical layer names."""
    doc = ezdxf.readfile(str(source_dxf))
    for layer in (GTV3_FOOTPRINT_LAYER, GTV3_ZONE_LAYER, GTV3_OPENING_LAYER):
        if layer not in doc.layers:
            doc.layers.add(layer)
    persisted: list[_PersistedPlan] = []
    for plan_view, result in plan_runs:
        exterior_openings = [opening for opening in result.p1.openings
                             if opening.classification == "exterior"]
        handles, edge_handles = _append_plan_geometry(
            doc, result.footprint, result.zones, exterior_openings)
        result.gtv3_handles = handles
        result.gtv3_zone_edge_handles = edge_handles
        persisted.append(_PersistedPlan(plan_view, result, exterior_openings,
                                        handles, edge_handles))
    _save_converter_augmented_dxf(doc, dest, source_sha256, request_sha256)
    return persisted


def _append_elevation_outlines(path: Path, records: list[_ElevationRecord], q: float,
                               source_sha256: str, request_sha256: str) -> list[_ElevationRecord]:
    """E5: append only normalized structural rectangles, then reopen and verify."""
    doc = ezdxf.readfile(str(path)); msp = doc.modelspace()
    if GTV3_ELEV_OPENING_LAYER not in doc.layers:
        doc.layers.add(GTV3_ELEV_OPENING_LAYER)
    emitted = []
    for rec in records:
        x0, y0, x1, y1 = rec.rect
        entity = msp.add_lwpolyline([(x0,y0), (x1,y0), (x1,y1), (x0,y1)], close=True,
                                    dxfattribs={"layer": GTV3_ELEV_OPENING_LAYER})
        emitted.append(_ElevationRecord(**{**rec.__dict__, "generated_handle": entity.dxf.handle}))
    _save_converter_augmented_dxf(doc, path, source_sha256, request_sha256)
    reopened = ezdxf.readfile(str(path)); rmsp = reopened.modelspace()
    for rec in emitted:
        entity = next((e for e in rmsp if e.dxf.handle == rec.generated_handle), None)
        if entity is None or entity.dxftype() != "LWPOLYLINE":
            raise ValueError("tarch_elevation_normalized_outline_drift")
        pts = list(entity.get_points("xyseb"))
        if len(pts) != 4 or any(p[4] != 0 for p in pts):
            raise ValueError("tarch_elevation_normalized_outline_drift")
        if not entity.closed:
            raise ValueError("tarch_elevation_normalized_outline_drift")
    return emitted


def _plan_footprint_for_raster(doc, handles: list[str] | None):
    candidates = [item for item in doc.modelspace()
                  if item.dxftype() == "LWPOLYLINE"
                  and item.dxf.layer == GTV3_FOOTPRINT_LAYER]
    if handles is None:
        return next(iter(candidates), None)
    wanted = set(handles)
    matched = [item for item in candidates if item.dxf.handle in wanted]
    return matched[0] if len(matched) == 1 else None


def _validate_raster_intents(request: TarchConversionRequestV1, doc, tols: _Tols,
                             diags: list[ConversionDiagnosticV1],
                             plan_footprint_handles: dict[str, list[str]] | None = None) -> None:
    """Validate signed v3 calibration facts; image bytes stay checked by overlay writer.

    ``pixel_to_source_m`` is source-metres (the manifest convention), whereas
    request control coordinates remain exact DXF-native values.  This boundary is
    explicit so a mm affine cannot silently be consumed as a metre affine.
    """
    if request.request_version != 3:
        return
    elevation_intents = {item.id: item for item in request.elevation_views if isinstance(item, DatumBoundNamedElevationViewIntentV3)}
    plan_intents = {item.id: item for item in request.plan_views}
    expected_view_ids = set(elevation_intents) | set(plan_intents)
    if {binding.view_id for binding in request.raster_overlays} != expected_view_ids or len(request.raster_overlays) != len(expected_view_ids):
        fallback = next(iter(elevation_intents.values())).frame_entity_handle if elevation_intents else request.plan_views[0].wall_selector.layers[0]
        _add(diags, _diag("tarch_raster_overlay_unbound", handles=[fallback])); return
    for binding in request.raster_overlays:
        elevation_intent = elevation_intents.get(binding.view_id)
        plan_intent = plan_intents.get(binding.view_id)
        if (elevation_intent is None and plan_intent is None) or Path(binding.source_label).name != binding.source_label:
            fallback = elevation_intent.frame_entity_handle if elevation_intent else next(iter(elevation_intents.values())).frame_entity_handle
            _add(diags, _diag("tarch_raster_overlay_unbound", handles=[fallback])); continue
        controls = {control.role: control for control in binding.calibration_controls}
        if len(controls) != len(binding.calibration_controls):
            _add(diags, _diag("tarch_raster_calibration_invalid", handles=[binding.calibration_controls[0].entity_handle])); continue
        if plan_intent is not None:
            handles = None if plan_footprint_handles is None else plan_footprint_handles.get(plan_intent.id, [])
            footprint = _plan_footprint_for_raster(doc, handles)
            if set(controls) != {"footprint_sw", "footprint_se", "footprint_nw"} or footprint is None:
                _add(diags, _diag("tarch_raster_calibration_invalid", handles=[binding.calibration_controls[0].entity_handle])); continue
            vertices = [(float(point[0]), float(point[1])) for point in footprint.get_points("xy")]
            min_x, max_x = min(point[0] for point in vertices), max(point[0] for point in vertices)
            min_y, max_y = min(point[1] for point in vertices), max(point[1] for point in vertices)
            expected = {"footprint_sw": (min_x, min_y), "footprint_se": (max_x, min_y), "footprint_nw": (min_x, max_y)}
            pix = [controls[key].pixel_point for key in ("footprint_sw", "footprint_se", "footprint_nw")]
            area2 = abs((pix[1][0]-pix[0][0])*(pix[2][1]-pix[0][1]) - (pix[1][1]-pix[0][1])*(pix[2][0]-pix[0][0]))
            def mapped(control):
                a = binding.pixel_to_source_m
                return (a.m00*control.pixel_point[0]+a.m01*control.pixel_point[1]+a.m02,
                        a.m10*control.pixel_point[0]+a.m11*control.pixel_point[1]+a.m12)
            residual_ok = all(max(abs(mapped(control)[0]-control.source_point_dxf[0]*request.metres_per_unit),
                                  abs(mapped(control)[1]-control.source_point_dxf[1]*request.metres_per_unit)) <= tols.node_join_m
                              for control in controls.values())
            def close_native(actual, expected_point): return max(abs(actual[0]-expected_point[0]), abs(actual[1]-expected_point[1])) <= tols.node_join_native
            if area2 == 0 or not residual_ok or not all(close_native(controls[role].source_point_dxf, point) for role, point in expected.items()):
                _add(diags, _diag("tarch_raster_calibration_invalid", handles=[binding.calibration_controls[0].entity_handle], context={"view_id": plan_intent.id})); continue
            continue
        intent = elevation_intent
        datum = intent.floor_datums[0]
        entity = next((item for item in doc.modelspace() if item.dxf.handle == datum.entity_handle), None)
        if set(controls) != {"datum_lo", "datum_hi", "off_datum"} or entity is None:
            _add(diags, _diag("tarch_raster_calibration_invalid", handles=[datum.entity_handle])); continue
        start, end = (float(entity.dxf.start.x), float(entity.dxf.start.y)), (float(entity.dxf.end.x), float(entity.dxf.end.y))
        expected_lo = start if datum.world_along_lo_source_endpoint == "start" else end
        expected_hi = end if datum.world_along_lo_source_endpoint == "start" else start
        def close_native(actual, expected): return max(abs(actual[0]-expected[0]), abs(actual[1]-expected[1])) <= tols.node_join_native
        pix = [controls[key].pixel_point for key in ("datum_lo", "datum_hi", "off_datum")]
        area2 = abs((pix[1][0]-pix[0][0])*(pix[2][1]-pix[0][1]) - (pix[1][1]-pix[0][1])*(pix[2][0]-pix[0][0]))
        def mapped(control):
            a = binding.pixel_to_source_m
            return (a.m00*control.pixel_point[0]+a.m01*control.pixel_point[1]+a.m02,
                    a.m10*control.pixel_point[0]+a.m11*control.pixel_point[1]+a.m12)
        residual_ok = all(max(abs(mapped(control)[0]-control.source_point_dxf[0]*request.metres_per_unit),
                              abs(mapped(control)[1]-control.source_point_dxf[1]*request.metres_per_unit)) <= tols.node_join_m
                          for control in controls.values())
        if (not close_native(controls["datum_lo"].source_point_dxf, expected_lo)
                or not close_native(controls["datum_hi"].source_point_dxf, expected_hi)
                or area2 == 0 or not residual_ok):
            _add(diags, _diag("tarch_raster_calibration_invalid", handles=[datum.entity_handle],
                              context={"view_id": intent.id, "declared_lo_endpoint": datum.world_along_lo_source_endpoint})); continue


def _build_source_map(request: TarchConversionRequestV1, plan_view: PlanViewIntentV1,
                      zones: list[ZoneExpansion], exterior_openings: list[ResolvedOpening],
                      gtv3_handles: dict[str, list[str]],
                      zone_edge_handles: dict[tuple[int, int], str],
                      footprint: Any, p1: P1PlanViewGeometry, tols: _Tols
                      ) -> SourceMapV1:
    """Per-edge ancestry (plan §6.1 / §8.1): one SourceMapEntryV1 per GENERATED
    GTV3_* entity, each naming the SOURCE DXF handles it was derived from.

      * footprint_ring  <- the outer-skin wall LINEs on the footprint exterior
      * zone_edge       <- the inner-face wall LINE(s) the cavity edge lay on
      * opening_outline <- the opening block INSERT + its jamb-cap LINEs

    Generated handles come from ``_write_augmented_dxf``; source handles are the
    real tianzheng entity handles (never a layer label)."""
    affine = plan_view.world_from_source_m
    entries: list[SourceMapEntryV1] = []

    def world(pt): return list(_to_world(pt, affine))

    # footprint <- outer-skin wall lines on the footprint exterior ring
    if footprint.geom_type == "Polygon" and gtv3_handles.get(GTV3_FOOTPRINT_LAYER):
        ring = [(p[0], p[1]) for p in list(footprint.exterior.coords)[:-1]]
        src: list[str] = []
        for a, b in zip(ring, ring[1:] + ring[:1]):
            if a[0] == b[0] or a[1] == b[1]:
                src.extend(_edge_source_handles(a, b, p1.wall_lines, tols))
        entries.append(SourceMapEntryV1(
            generated_handle=gtv3_handles[GTV3_FOOTPRINT_LAYER][0],
            view_id=plan_view.id, floor_id=plan_view.floor_id,
            semantic_role="footprint", operation="footprint_ring",
            canonical_geometry_world_m={"ring_m": [world(p) for p in ring]},
            source_entity_refs=[SourceEntityRefV1(handle=h, role="wall_side") for h in dict.fromkeys(src)]))

    # zone edges <- inner-face wall lines; one entry per emitted GTV3_ZONE line
    handle_to_sources: dict[str, list[str]] = {}
    handle_to_geom: dict[str, list] = {}
    for (z_idx, e_idx), h in zone_edge_handles.items():
        z = zones[z_idx]
        edge = z.edges[e_idx]
        a = z.vertices[e_idx]
        b = z.vertices[(e_idx + 1) % len(z.vertices)]
        handle_to_sources.setdefault(h, []).extend(edge.source_handles)
        handle_to_geom[h] = [world(a), world(b)]
    for h, sources in handle_to_sources.items():
        refs = [SourceEntityRefV1(handle=s, role="wall_side") for s in dict.fromkeys(sources)]
        proof_ids = []
        for z_idx, e_idx in zone_edge_handles:
            if zone_edge_handles[(z_idx, e_idx)] == h:
                ev = zones[z_idx].edges[e_idx].thickness_evidence
                if ev is not None:
                    proof_ids.extend(ev.proof_handles)
        entries.append(SourceMapEntryV1(
            generated_handle=h, view_id=plan_view.id, floor_id=plan_view.floor_id,
            semantic_role="zone_boundary", operation="zone_edge",
            canonical_geometry_world_m={"segment_m": handle_to_geom[h]},
            source_entity_refs=refs, proof_ids=list(dict.fromkeys(proof_ids))))

    # openings <- block INSERT + jamb caps
    for op, h in zip(exterior_openings, gtv3_handles.get(GTV3_OPENING_LAYER, [])):
        refs = [SourceEntityRefV1(handle=op.handle, role="opening_block")]
        refs.extend(SourceEntityRefV1(handle=j, role="jamb") for j in op.jamb_handles)
        x0, y0, x1, y1 = op.rect_dxf_mm
        entries.append(SourceMapEntryV1(
            generated_handle=h, view_id=plan_view.id, floor_id=plan_view.floor_id,
            semantic_role="opening", operation="opening_outline",
            canonical_geometry_world_m={"rect_m": [world((x0, y0)), world((x1, y1))]},
            source_entity_refs=refs))

    sm = SourceMapV1(map_version=1, case=request.case, entries=entries, source_map_sha256="0" * 64)
    return sm.model_copy(update={"source_map_sha256": compute_source_map_sha256(sm)})


def _outer_skin_thickness_m(zones: list[ZoneExpansion], metres_per_unit: float) -> float | None:
    """Evidence-backed exterior wall thickness for the manifest ``default_wall_thickness_m``.

    Only S7 zone edges expanded on an ``outer_skin`` basis measure the *exterior* wall,
    and each already carries its :class:`ThicknessEvidenceV1` proof (the G7/G8 evidence
    system — a measured jamb cap, never a constant).  A value is emitted only when every
    such edge is proven AND they all agree; a missing proof or any disagreement yields
    ``None``.  Fail-closed: the extractor must never receive a guessed or averaged
    thickness, and ``None`` simply leaves the downstream scorer on its existing
    degraded-but-honest path (plan §2.1 — the legal range is a sanity bound, not a source).
    """
    outer = [edge for zone in zones for edge in zone.edges if edge.basis == "outer_skin"]
    if not outer:
        return None
    if any(edge.thickness_evidence is None or not edge.thickness_native for edge in outer):
        return None
    values = {round(edge.thickness_native * metres_per_unit, 9) for edge in outer}
    if len(values) != 1:
        return None
    value = values.pop()
    return value if value > 0 else None


def _build_manifest(request: TarchConversionRequestV1, plan_view: PlanViewIntentV1,
                    zones: list[ZoneExpansion], claims: list[dict],
                    exterior_openings: list[ResolvedOpening], gtv3_handles: dict[str, list[str]],
                    augmented_dxf: Path, elevation_records: list[_ElevationRecord] | None = None):
    """Build a GtExtractionManifestV1 draft bound to the AUGMENTED DXF (plan §8.1) —
    that is the file v3 actually consumes.  Selectors pin the generated GTV3_*
    handles via only_listed (no 'layer has an extra line' silent answer drift)."""
    from .gt_manifest import (GtExtractionManifestV1, EntityLocatorV1,
                              PlanOpeningBindingV1, PlanViewBindingV1, ElevationViewBindingV1,
                              RasterOverlayBindingV1, ZoneSeedV1, compute_manifest_sha256)
    affine = plan_view.world_from_source_m          # native -> world (m00 = metres_per_unit)
    # v3's _transform pre-multiplies native by metres_per_unit, so the manifest
    # world_from_source_m maps source-METRES -> world (identity scale for a pure-scale
    # source) — NOT native -> world (which is what PlanViewIntentV1 carries).  Factor
    # the mpu scaling out of the linear part; the translation is already world-metres.
    # (Confirmed against the frozen v3 contract: tests/test_gt_extraction.py et al. use
    # m00=1.0 with metres_per_unit=0.001.)  Native->world affine is still used below for
    # the seed points, which are emitted directly in world metres.
    mpu = request.metres_per_unit
    manifest_affine = Affine2D(m00=affine.m00 / mpu, m01=affine.m01 / mpu, m02=affine.m02,
                               m10=affine.m10 / mpu, m11=affine.m11 / mpu, m12=affine.m12)
    floor = request.floors[0]
    fp_handles = sorted(gtv3_handles[GTV3_FOOTPRINT_LAYER])
    zo_handles = sorted(gtv3_handles[GTV3_ZONE_LAYER])
    # A single-zone building has no internal zoning lines -> no GTV3_ZONE handles.  The
    # selector contract requires only_listed to carry handles and all_matching to carry
    # none, so a handle-less zone set uses all_matching over the (empty) layer.
    zo_mode = "only_listed" if zo_handles else "all_matching"
    zo_min = 1 if zo_handles else 0
    seeds = []
    for z, claim in zip(zones, claims):
        wp = _to_world(z.seed_native, affine)
        seeds.append(ZoneSeedV1(zone_id=claim["zone_id"], name=claim["name"],
                                role=claim["role"], point_world_m=wp))
    plan_openings = []
    for op, h in zip(exterior_openings, sorted(gtv3_handles[GTV3_OPENING_LAYER])):
        plan_openings.append(PlanOpeningBindingV1(
            opening_id=f"op_{op.handle.lower()}", kind=op.kind,
            geometry_mode="closed_outline_bbox", span_world_axis=op.axis,
            entities=[EntityLocatorV1(handle=h)]))
    pv = {
        "kind": "plan", "id": plan_view.id, "floor_id": plan_view.floor_id,
        "clip_box_dxf": plan_view.clip_box_dxf, "world_from_source_m": manifest_affine,
        "footprint_boundary": {"entity_types": ["LWPOLYLINE"], "layers": [GTV3_FOOTPRINT_LAYER],
                               "handles": fp_handles, "handle_mode": "only_listed",
                               "min_count": 1, "max_count": 1},
        "zone_boundaries": {"entity_types": ["LINE"], "layers": [GTV3_ZONE_LAYER],
                            "handles": zo_handles, "handle_mode": zo_mode,
                            "min_count": zo_min, "max_count": None},
        "plan_openings": [p.model_dump(mode="python") for p in plan_openings],
        "zone_seeds": [s.model_dump(mode="python") for s in seeds],
        "boundary_reference": "outer_skin",
        "default_wall_thickness_m": _outer_skin_thickness_m(zones, mpu),
    }
    include_elevations = elevation_records is not None
    elevation_records = elevation_records or []
    elev_views = []
    for intent in sorted((x for x in request.elevation_views if isinstance(x, DatumBoundNamedElevationViewIntentV3)), key=lambda x: x.id) if include_elevations else []:
        evidence = []
        for rec in sorted((r for r in elevation_records if r.view_id == intent.id), key=lambda r: (r.kind, r.raw_handles)):
            evidence.append({"evidence_id": f"ev_{intent.id}_{min(rec.raw_handles).lower()}", "kind": rec.kind,
                             "geometry_mode": "closed_outline_bbox", "entities": [{"handle": rec.generated_handle}]})
        elev_views.append(ElevationViewBindingV1.model_validate({"kind": "elevation", "id": intent.id, "floor_ids": intent.floor_ids,
                           "projection_surface_key": f"ps_{intent.id}", "facade_family": intent.facade_family,
                           "view_kind": "full", "world_along_coverage": None, "direction_semantics": "building_axis", "azimuth_deg": None,
                           "clip_box_dxf": intent.clip_box_dxf,
                           "world_along_from_source_m": {"source_axis": intent.world_along_from_source_m.source_axis, "scale": intent.world_along_from_source_m.scale / mpu, "offset": intent.world_along_from_source_m.offset},
                           "world_z_from_source_m": {"source_axis": intent.world_z_from_source_m.source_axis, "scale": intent.world_z_from_source_m.scale / mpu, "offset": intent.world_z_from_source_m.offset},
                           "segment_scope_mode": "all_family_segments", "boundary_entities": [], "opening_entities": evidence}))
    rasters = []
    if request.request_version == 3 and include_elevations:
        for binding in request.raster_overlays:
            rasters.append(RasterOverlayBindingV1.model_validate({"id": binding.id, "source_label": binding.source_label, "source_sha256": binding.source_sha256,
                            "view_id": binding.view_id, "pixel_to_source_m": binding.pixel_to_source_m.model_dump(mode="python")}))
    raw = {"manifest_version": 1, "case": request.case, "source_id": request.normalized_source_id,
           "source_dxf_label": augmented_dxf.name,
           "source_dxf_sha256": hashlib.sha256(augmented_dxf.read_bytes()).hexdigest(),
           "native_units": request.native_units, "metres_per_unit": request.metres_per_unit,
           "geometry_profile": request.target_geometry_profile,
           "floors": [floor.model_dump(mode="python")], "views": [PlanViewBindingV1.model_validate(pv), *elev_views],
           "north_axis": None, "raster_overlays": rasters, "manifest_sha256": "0" * 64}
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        raw["manifest_sha256"] = compute_manifest_sha256(GtExtractionManifestV1.model_construct(**raw))
    return GtExtractionManifestV1.model_validate(raw), seeds, plan_openings


def _build_multifloor_manifest(request: TarchConversionRequestV1,
                               persisted: list[_PersistedPlan], augmented_dxf: Path,
                               elevation_records: list[_ElevationRecord] | None = None):
    """Build one handle-pinned plan binding per floor over one normalized DXF."""
    from .gt_manifest import (ElevationViewBindingV1, EntityLocatorV1,
                              GtExtractionManifestV1, PlanOpeningBindingV1,
                              PlanViewBindingV1, RasterOverlayBindingV1,
                              ZoneSeedV1, compute_manifest_sha256)

    mpu = request.metres_per_unit
    plan_bindings = []
    for item in persisted:
        plan_view = item.plan_view
        affine = plan_view.world_from_source_m
        manifest_affine = Affine2D(
            m00=affine.m00 / mpu, m01=affine.m01 / mpu, m02=affine.m02,
            m10=affine.m10 / mpu, m11=affine.m11 / mpu, m12=affine.m12)
        fp_handles = sorted(item.gtv3_handles[GTV3_FOOTPRINT_LAYER])
        zo_handles = sorted(item.gtv3_handles[GTV3_ZONE_LAYER])
        zo_mode = "only_listed" if zo_handles else "all_matching"
        seeds = []
        for zone, claim in zip(item.result.zones, item.result.claims):
            seeds.append(ZoneSeedV1(
                zone_id=claim["zone_id"], name=claim["name"], role=claim["role"],
                point_world_m=_to_world(zone.seed_native, affine)))
        plan_openings = []
        for opening, handle in zip(item.exterior_openings,
                                   sorted(item.gtv3_handles[GTV3_OPENING_LAYER])):
            plan_openings.append(PlanOpeningBindingV1(
                opening_id=f"op_{opening.handle.lower()}", kind=opening.kind,
                geometry_mode="closed_outline_bbox", span_world_axis=opening.axis,
                entities=[EntityLocatorV1(handle=handle)]))
        plan_bindings.append(PlanViewBindingV1.model_validate({
            "kind": "plan", "id": plan_view.id, "floor_id": plan_view.floor_id,
            "clip_box_dxf": plan_view.clip_box_dxf,
            "world_from_source_m": manifest_affine,
            "footprint_boundary": {
                "entity_types": ["LWPOLYLINE"], "layers": [GTV3_FOOTPRINT_LAYER],
                "handles": fp_handles, "handle_mode": "only_listed",
                "min_count": 1, "max_count": 1},
            "zone_boundaries": {
                "entity_types": ["LINE"], "layers": [GTV3_ZONE_LAYER],
                "handles": zo_handles, "handle_mode": zo_mode,
                "min_count": 1 if zo_handles else 0, "max_count": None},
            "plan_openings": [opening.model_dump(mode="python") for opening in plan_openings],
            "zone_seeds": [seed.model_dump(mode="python") for seed in seeds],
            "boundary_reference": "outer_skin",
            "default_wall_thickness_m": _outer_skin_thickness_m(item.result.zones, mpu),
        }))

    include_elevations = elevation_records is not None
    elevation_records = elevation_records or []
    elevation_bindings = []
    intents = sorted((intent for intent in request.elevation_views
                      if isinstance(intent, DatumBoundNamedElevationViewIntentV3)),
                     key=lambda intent: intent.id) if include_elevations else []
    for intent in intents:
        evidence = []
        records = sorted((record for record in elevation_records
                          if record.view_id == intent.id),
                         key=lambda record: (record.kind, record.raw_handles))
        for record in records:
            evidence.append({
                "evidence_id": f"ev_{intent.id}_{min(record.raw_handles).lower()}",
                "kind": record.kind, "geometry_mode": "closed_outline_bbox",
                "entities": [{"handle": record.generated_handle}]})
        elevation_bindings.append(ElevationViewBindingV1.model_validate({
            "kind": "elevation", "id": intent.id, "floor_ids": intent.floor_ids,
            "projection_surface_key": f"ps_{intent.id}",
            "facade_family": intent.facade_family, "view_kind": "full",
            "world_along_coverage": None, "direction_semantics": "building_axis",
            "azimuth_deg": None, "clip_box_dxf": intent.clip_box_dxf,
            "world_along_from_source_m": {
                "source_axis": intent.world_along_from_source_m.source_axis,
                "scale": intent.world_along_from_source_m.scale / mpu,
                "offset": intent.world_along_from_source_m.offset},
            "world_z_from_source_m": {
                "source_axis": intent.world_z_from_source_m.source_axis,
                "scale": intent.world_z_from_source_m.scale / mpu,
                "offset": intent.world_z_from_source_m.offset},
            "segment_scope_mode": "all_family_segments", "boundary_entities": [],
            "opening_entities": evidence,
        }))

    rasters = []
    if request.request_version == 3 and include_elevations:
        for binding in request.raster_overlays:
            rasters.append(RasterOverlayBindingV1.model_validate({
                "id": binding.id, "source_label": binding.source_label,
                "source_sha256": binding.source_sha256, "view_id": binding.view_id,
                "pixel_to_source_m": binding.pixel_to_source_m.model_dump(mode="python"),
            }))
    raw = {
        "manifest_version": 1, "case": request.case,
        "source_id": request.normalized_source_id,
        "source_dxf_label": augmented_dxf.name,
        "source_dxf_sha256": hashlib.sha256(augmented_dxf.read_bytes()).hexdigest(),
        "native_units": request.native_units,
        "metres_per_unit": request.metres_per_unit,
        "geometry_profile": request.target_geometry_profile,
        "floors": [floor.model_dump(mode="python") for floor in request.floors],
        "views": [*plan_bindings, *elevation_bindings],
        "north_axis": None, "raster_overlays": rasters,
        "manifest_sha256": "0" * 64,
    }
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        raw["manifest_sha256"] = compute_manifest_sha256(
            GtExtractionManifestV1.model_construct(**raw))
    return GtExtractionManifestV1.model_validate(raw)


# §6.5 compares two INDEPENDENT code paths that must agree exactly: the converter
# audit z (request affine, native units, `_converter_elevation_z`) and the
# authoritative GT z (manifest affine = request/mpu, then re-multiplied by mpu in
# `gt_extraction._elevation_geometry`).  Algebraically identical, so the only
# admissible difference is float re-association noise: for sm24 magnitudes (|z| <= 4 m)
# that is O(1e-15).  1e-9 m = 1 nanometre sits ~6 orders below the mm quantisation
# step and ~6 orders above the noise floor, so it can never absorb a real drift.
_PAIRING_Z_TOLERANCE_M = 1e-9


def _converter_elevation_z(rec: _ElevationRecord, intent) -> tuple[float, float]:
    """Converter-side z interval for one ledger record (request affine, native units).

    This is the single arithmetic used both for the human audit row and for the
    §6.5 postcheck, so the reviewer signs exactly the number the gate compared.
    """
    z_axis = intent.world_z_from_source_m.source_axis
    raw = (rec.rect[0], rec.rect[2]) if z_axis == "x" else (rec.rect[1], rec.rect[3])
    lo, hi = sorted(value * intent.world_z_from_source_m.scale + intent.world_z_from_source_m.offset
                    for value in raw)
    return lo, hi


def _verify_pairing_consistency(document, elevation_records: list[_ElevationRecord],
                                request: TarchConversionRequestV1) -> list[str]:
    """spec §6.5 [S]: converter pairing ledger <-> extracted GT ``opening_elevation`` refs.

    Reverse-look-up every GT elevation source ref by its generated handle and compare
    it with the converter ledger entry that produced it: view id, kind, z interval, and
    exactly one ref group per relevant pair.  Without this the converter's pre-linking
    is an unconsumed pseudo-check and the audited z may silently diverge from the
    authoritative GT z (they are two independent affine paths).

    Returns a sorted list of drift reasons; empty means consistent.
    """
    intents = {view.id: view for view in request.elevation_views
               if isinstance(view, DatumBoundNamedElevationViewIntentV3)}
    reasons: list[str] = []
    ledger: dict[str, _ElevationRecord] = {}
    for rec in elevation_records:
        if not rec.generated_handle:
            reasons.append(f"ledger_handle_missing:{rec.view_id}")
            continue
        if rec.generated_handle in ledger:
            reasons.append(f"ledger_handle_duplicate:{rec.generated_handle}")
            continue
        ledger[rec.generated_handle] = rec
    owners: dict[str, list[str]] = {}
    for opening in document.openings:
        for ref in opening.source_refs:
            if ref.role != "opening_elevation":
                continue
            owners.setdefault(ref.entity_handle, []).append(opening.id)
            rec = ledger.get(ref.entity_handle)
            if rec is None:
                reasons.append(f"gt_ref_not_in_ledger:{ref.entity_handle}")
                continue
            if ref.view_id != rec.view_id:
                reasons.append(f"view_id_mismatch:{ref.entity_handle}:{ref.view_id}!={rec.view_id}")
            if opening.kind != rec.kind:
                reasons.append(f"kind_mismatch:{ref.entity_handle}:{opening.kind}!={rec.kind}")
            intent = intents.get(rec.view_id)
            if intent is None:
                reasons.append(f"ledger_view_not_declared:{rec.view_id}")
                continue
            if opening.z_interval is None:
                reasons.append(f"gt_z_missing:{opening.id}")
                continue
            lo, hi = _converter_elevation_z(rec, intent)
            if (abs(opening.z_interval.lo - lo) > _PAIRING_Z_TOLERANCE_M
                    or abs(opening.z_interval.hi - hi) > _PAIRING_Z_TOLERANCE_M):
                reasons.append(f"z_interval_drift:{ref.entity_handle}")
    # Exactly one ref group per relevant pair: 0 = converter evidence nobody consumed,
    # >1 = one evidence claimed by several openings (or duplicated inside one).
    for handle in sorted(ledger):
        count = len(owners.get(handle, []))
        if count != 1:
            reasons.append(f"evidence_ref_group_count:{handle}:{count}")
    return sorted(set(reasons))


def _run_g9_v3_preflight(augmented_dxf: Path, manifest, tooling) -> tuple[bool, str | None, Any]:
    """G9: run the real v3 extractor (inspect + full extract, which itself performs
    validate_gt_v3 + canonical round-trip) on the augmented DXF + manifest.  Any typed
    extraction/validation failure -> fail-closed (tarch_v3_precondition).

    The extracted document is returned (third element) so the §6.5 pairing postcheck
    consumes the real GT instead of re-deriving one; it is ``None`` when G9 failed.
    """
    from pydantic import ValidationError as PydanticValidationError

    from .gt_extraction import (ExtractionError, ExtractionInputs, InspectionInputs,
                                extract_gt_v3, inspect_extraction_inputs)
    from .gt_schema import REPO_ROOT, GtValidationError, compute_gt_implementation_hashes
    try:
        hashes = compute_gt_implementation_hashes(REPO_ROOT)
        insp = inspect_extraction_inputs(InspectionInputs(augmented_dxf, manifest, tooling, hashes))
        if insp.status != "PASS":
            codes = ",".join(sorted({i.code for i in insp.issues}))
            return False, codes or insp.status, None
        document = extract_gt_v3(ExtractionInputs(augmented_dxf, manifest, tooling, hashes))
        return True, None, document
    except (ExtractionError, GtValidationError, PydanticValidationError) as exc:
        # Only the typed, expected fail-closed conditions become a BLOCK.  A genuine
        # coding bug (KeyError/AttributeError/...) must keep propagating rather than
        # be disguised as a legitimate gate result with an opaque message.
        return False, str(exc), None


def _write_overlay_svg(path: Path, p1: P1PlanViewGeometry, footprint: Any,
                       zones: list[ZoneExpansion], claims: list[dict],
                       exterior_openings: list[ResolvedOpening], affine: Affine2D) -> None:
    """Deterministic human-review overlay (G10): source wall lines + outer-skin ring
    + zone polygons (labelled, semi-transparent) + exterior opening rects.  SVG only
    (no matplotlib in this env); a PNG-composited overlay is a documented follow-up."""
    xs = [x for _, x0, y0, x1, y1 in p1.wall_lines for x in (x0, x1)]
    ys = [y for _, x0, y0, x1, y1 in p1.wall_lines for y in (y0, y1)]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    W, H = 1600.0, 1200.0
    pad = 0.02 * max(maxx - minx, maxy - miny)
    lox, loy = minx - pad, miny - pad
    span = max(maxx - minx, maxy - miny) + 2 * pad
    sx = W / span
    sy = H / span
    s = min(sx, sy)

    def X(x): return (x - lox) * s
    def Y(y): return H - (y - loy) * s
    palette = ["#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231", "#911eb4",
               "#46f0f0", "#f032e6", "#bcf60c", "#fabebe"]
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" '
             f'viewBox="0 0 {W:.0f} {H:.0f}" font-family="sans-serif" font-size="13">']
    parts.append(f'<rect width="{W:.0f}" height="{H:.0f}" fill="white"/>')
    # source wall lines
    for _, x0, y0, x1, y1 in p1.wall_lines:
        parts.append(f'<line x1="{X(x0):.1f}" y1="{Y(y0):.1f}" x2="{X(x1):.1f}" y2="{Y(y1):.1f}" '
                     f'stroke="#cccccc" stroke-width="0.8"/>')
    # zones
    for k, z in enumerate(zones):
        col = palette[k % len(palette)]
        pts = " ".join(f"{X(x):.1f},{Y(y):.1f}" for x, y in z.vertices)
        parts.append(f'<polygon points="{pts}" fill="{col}" fill-opacity="0.28" stroke="{col}" stroke-width="2"/>')
        label_pt = z.polygon.representative_point()
        cx, cy = label_pt.x, label_pt.y
        nm = claims[k]["name"] if k < len(claims) else z.cavity_id
        parts.append(f'<text x="{X(cx):.1f}" y="{Y(cy):.1f}" fill="black" text-anchor="middle" '
                     f'font-weight="bold">{nm}</text>')
    # outer-skin ring
    if footprint.geom_type == "Polygon":
        ring = " ".join(f"{X(x):.1f},{Y(y):.1f}" for x, y in footprint.exterior.coords)
        parts.append(f'<polygon points="{ring}" fill="none" stroke="black" stroke-width="2.5"/>')
    # exterior openings
    for op in exterior_openings:
        x0, y0, x1, y1 = op.rect_dxf_mm
        parts.append(f'<rect x="{X(x0):.1f}" y="{Y(y1):.1f}" width="{(x1-x0)*s:.1f}" '
                     f'height="{(y1-y0)*s:.1f}" fill="none" stroke="#0066ff" stroke-width="1.5"/>')
    parts.append("</svg>")
    Path(path).write_text("\n".join(parts), encoding="utf-8")


def _write_multifloor_overlay_svg(path: Path, persisted: list[_PersistedPlan]) -> None:
    """Deterministic source-space review overlay containing every plan frame."""
    lines = [line for item in persisted for line in item.result.p1.wall_lines]
    xs = [coord for _, x0, _y0, x1, _y1 in lines for coord in (x0, x1)]
    ys = [coord for _, _x0, y0, _x1, y1 in lines for coord in (y0, y1)]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    width, height = 1600.0, 1200.0
    pad = 0.02 * max(maxx - minx, maxy - miny)
    lox, loy = minx - pad, miny - pad
    span = max(maxx - minx, maxy - miny) + 2 * pad
    scale = min(width / span, height / span)
    x_px = lambda value: (value - lox) * scale
    y_px = lambda value: height - (value - loy) * scale
    palette = ["#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231",
               "#911eb4", "#46f0f0", "#f032e6", "#bcf60c", "#fabebe"]
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
             f'viewBox="0 0 {width:.0f} {height:.0f}" font-family="sans-serif" font-size="13">',
             f'<rect width="{width:.0f}" height="{height:.0f}" fill="white"/>']
    for item in persisted:
        for _, x0, y0, x1, y1 in item.result.p1.wall_lines:
            parts.append(f'<line x1="{x_px(x0):.1f}" y1="{y_px(y0):.1f}" '
                         f'x2="{x_px(x1):.1f}" y2="{y_px(y1):.1f}" '
                         f'stroke="#cccccc" stroke-width="0.8"/>')
        for index, zone in enumerate(item.result.zones):
            colour = palette[index % len(palette)]
            points = " ".join(f"{x_px(x):.1f},{y_px(y):.1f}" for x, y in zone.vertices)
            parts.append(f'<polygon points="{points}" fill="{colour}" fill-opacity="0.28" '
                         f'stroke="{colour}" stroke-width="2"/>')
            label = zone.polygon.representative_point()
            name = item.result.claims[index]["name"]
            parts.append(f'<text x="{x_px(label.x):.1f}" y="{y_px(label.y):.1f}" '
                         f'fill="black" text-anchor="middle" font-weight="bold">'
                         f'{item.plan_view.floor_id}:{name}</text>')
        if item.result.footprint.geom_type == "Polygon":
            ring = " ".join(f"{x_px(x):.1f},{y_px(y):.1f}"
                            for x, y in item.result.footprint.exterior.coords)
            parts.append(f'<polygon points="{ring}" fill="none" stroke="black" stroke-width="2.5"/>')
        for opening in item.exterior_openings:
            x0, y0, x1, y1 = opening.rect_dxf_mm
            parts.append(f'<rect x="{x_px(x0):.1f}" y="{y_px(y1):.1f}" '
                         f'width="{(x1-x0)*scale:.1f}" height="{(y1-y0)*scale:.1f}" '
                         f'fill="none" stroke="#0066ff" stroke-width="1.5"/>')
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def _verify_human_review_ack(work_dir: Path, request: TarchConversionRequestV1,
                             source_dxf: Path, overlay: Path) -> tuple[bool, dict]:
    """G10 accepts only a hash-bound external acknowledgement.

    Legacy requests retain the plan-overlay hash.  v3 acknowledges the canonical
    review index, whose inventory binds GT, both renders, four overlays and the
    per-opening audit table without fixing the future number of view files.
    """
    ack_path = work_dir / "review_ack.json"
    if request.request_version == 3:
        index_path = work_dir / "review_index.json"
        evidence = {"review_index_asset": index_path.name, "verification_status": "candidate", "ack_path": ack_path.name}
        if not index_path.is_file():
            evidence["review_index_status"] = "missing"
            return False, evidence
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            index_hash = index["inventory_sha256"]
        except Exception as exc:
            evidence["review_index_status"] = type(exc).__name__
            return False, evidence
        evidence["review_index_sha256"] = index_hash
        if not ack_path.is_file():
            return False, evidence
        try:
            ack = HumanReviewAckV1.model_validate_json(ack_path.read_text(encoding="utf-8"))
        except Exception as exc:
            evidence["ack_error"] = type(exc).__name__
            return False, evidence
        checks = {"source_hash": ack.source_dxf_sha256 == hashlib.sha256(source_dxf.read_bytes()).hexdigest(),
                  "request_hash": ack.request_sha256 == request.request_sha256,
                  "review_index_hash": ack.review_index_sha256 == index_hash}
        evidence.update({"verification_status": "signed" if all(checks.values()) else "hash_mismatch",
                         "reviewer": ack.reviewer, "signed_at": ack.signed_at, "ack_checks": checks,
                         "near_threshold_confirmed": ack.near_threshold_confirmed})
        return all(checks.values()), evidence
    overlay_rel = overlay.name
    evidence = {"overlay_asset": overlay_rel,
                "overlay_sha256": hashlib.sha256(overlay.read_bytes()).hexdigest(),
                "verification_status": "candidate", "ack_path": ack_path.name}
    if not ack_path.is_file():
        return False, evidence
    try:
        ack = HumanReviewAckV1.model_validate_json(ack_path.read_text(encoding="utf-8"))
    except Exception as exc:
        evidence["ack_error"] = type(exc).__name__
        return False, evidence
    checks = {"source_hash": ack.source_dxf_sha256 == hashlib.sha256(source_dxf.read_bytes()).hexdigest(),
              "request_hash": ack.request_sha256 == request.request_sha256,
              "overlay_hash": ack.overlay_sha256 == evidence["overlay_sha256"]}
    evidence.update({"verification_status": "signed" if all(checks.values()) else "hash_mismatch",
                     "reviewer": ack.reviewer, "signed_at": ack.signed_at, "ack_checks": checks})
    evidence["near_threshold_confirmed"] = ack.near_threshold_confirmed
    return all(checks.values()), evidence


def _write_diagnostic_overlay(path: Path, diagnostics: list[ConversionDiagnosticV1]) -> None:
    """Small deterministic BLOCK artefact: locations + stable diagnostic codes."""
    marks = []
    for d in diagnostics:
        for x, y in d.source_points_dxf_mm:
            marks.append((x, y, d.code))
    if marks:
        minx, maxx = min(x for x, _, _ in marks), max(x for x, _, _ in marks)
        miny, maxy = min(y for _, y, _ in marks), max(y for _, y, _ in marks)
    else:
        minx = miny = 0.0; maxx = maxy = 1.0
    span = max(maxx - minx, maxy - miny, 1.0)
    def X(x): return 40 + (x - minx) * 720 / span
    def Y(y): return 760 - (y - miny) * 720 / span
    lines = ['<svg xmlns="http://www.w3.org/2000/svg" width="800" height="800">',
             '<rect width="800" height="800" fill="white"/>']
    for x, y, code in marks:
        lines.append(f'<circle cx="{X(x):.1f}" cy="{Y(y):.1f}" r="7" fill="#d00"/>')
        lines.append(f'<text x="{X(x)+10:.1f}" y="{Y(y):.1f}" font-size="12">{code}</text>')
    if not marks:
        lines.append('<text x="20" y="40" font-size="14">BLOCK: no local geometry available</text>')
    lines.append('</svg>')
    path.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
# P2 orchestrator
# --------------------------------------------------------------------------- #
def run_p2_conversion(dxf_path: Path, request: TarchConversionRequestV1,
                      plan_view: PlanViewIntentV1, tooling,
                      work_dir: Path) -> P2ConversionResult:
    """Run S0-S9 end to end for one plan view.  Convert+build run in ``work_dir``
    (staging, §0.1 方案A); nothing is promoted into a protected answer root here."""
    assert_staging_input(Path(dxf_path))
    work_dir = Path(work_dir)
    assert_staging_work_dir(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    tols = _tols_from(tooling, request.metres_per_unit)
    affine = plan_view.world_from_source_m

    # S0-S4 (P1)
    p1 = run_p1_plan_view(dxf_path, request, plan_view, tooling)
    diags: list[ConversionDiagnosticV1] = list(p1.diagnostics)

    # A hash/ownership BLOCK is an entrance barrier: do not enter any geometry
    # stage (and therefore do not manufacture secondary topology diagnostics).
    if any(d.severity == DiagnosticSeverity.BLOCK for d in diags):
        cavities, wall_region, footprint, near, claims = [], Polygon(), Polygon(), [], []
    else:
        cavities, wall_region, footprint, near = s5_identify_cavities(p1, request, tols, diags, affine)
        claims = s6_bind_intent(cavities, plan_view, tols, diags, affine)
    # S7 expand (only if no BLOCK so far and cavities claimed)
    zones: list[ZoneExpansion] = []
    if claims and not any(d.severity == DiagnosticSeverity.BLOCK for d in diags):
        zones = s7_expand_zones(claims, wall_region, footprint, tols, diags,
                                p1.wall_lines, p1.wall_bands, p1.wall_line_layers,
                                tuple(v / request.metres_per_unit
                                      for v in request.wall_thickness_range_m))

    result = P2ConversionResult(p1=p1, cavities=cavities, wall_region=wall_region,
                                footprint=footprint, near_threshold_faces=near, zones=zones,
                                claims=claims, gates=[], diagnostics=diags)

    # G4/G6/G7/G8 (G9/G10 need S9 artefacts)
    gates = _build_p2_gates(p1, cavities, wall_region, footprint, zones, claims, near,
                            request, plan_view, tols, diags)
    blocked = any(d.severity == DiagnosticSeverity.BLOCK for d in diags)

    augmented_path: Path | None = None
    manifest = None
    overlay_path: Path | None = None
    g9_code: str | None = None
    if zones and not blocked:
        # S9 persist (staging only)
        exterior_openings = [o for o in p1.openings if o.classification == "exterior"]
        augmented_path = work_dir / "normalized.dxf"
        source_sha256 = hashlib.sha256(Path(dxf_path).read_bytes()).hexdigest()
        gtv3_handles, zone_edge_handles = _write_augmented_dxf(
            Path(dxf_path), augmented_path, footprint, zones, exterior_openings,
            source_sha256, request.request_sha256)
        manifest, seeds, plan_openings = _build_manifest(
            request, plan_view, zones, claims, exterior_openings, gtv3_handles, augmented_path)
        # v3 elevation consumes the already-certified plan projection solely to
        # verify the signed datum endpoints and make the required global pairing;
        # it never derives datum height or handedness from openings.
        elevation_records: list[_ElevationRecord] = []
        elevation_bound = False       # manifest actually carries elevation bindings
        if request.request_version == 3:
            from pydantic import ValidationError as PydanticValidationError

            from .gt_extraction import ExtractionError, ExtractionInputs, extract_gt_v3
            from .gt_schema import REPO_ROOT, GtValidationError, compute_gt_implementation_hashes
            # This pre-pass extracts against the PLAN-ONLY manifest: the elevation
            # bindings do not exist yet (they are derived from its own output), so it
            # cannot be deduplicated against the G9 run on the complete manifest
            # without inverting the gate order.  It is therefore kept, but no longer
            # allowed to crash the conversion instead of returning a blocked report.
            plan_gt = None
            try:
                plan_gt = extract_gt_v3(ExtractionInputs(augmented_path, manifest, tooling, compute_gt_implementation_hashes(REPO_ROOT)))
            except (ExtractionError, GtValidationError, PydanticValidationError) as exc:
                _add(diags, _diag("tarch_v3_precondition",
                                  points_dxf_mm=[(p1.footprint_polygon.centroid.x,
                                                  p1.footprint_polygon.centroid.y)],
                                  context={"v3_code": str(exc), "stage": "elevation_plan_prepass"}))
            if plan_gt is not None:
                elevation_records, elevation_diags = _v3_elevation_records(augmented_path, request, plan_gt, tols)
                diags.extend(elevation_diags)
                if not any(d.severity == DiagnosticSeverity.BLOCK for d in elevation_diags):
                    try:
                        elevation_records = _append_elevation_outlines(
                            augmented_path, elevation_records, tols.quant_native,
                            source_sha256, request.request_sha256)
                    except ValueError:
                        _add(diags, _diag("tarch_elevation_normalized_outline_drift", handles=[elevation_records[0].raw_handles[0]] if elevation_records else [plan_view.id]))
                    else:
                        manifest, seeds, plan_openings = _build_manifest(request, plan_view, zones, claims, exterior_openings,
                                                                           gtv3_handles, augmented_path, elevation_records)
                        elevation_bound = True
            result.elevation_records = elevation_records
        overlay_path = work_dir / "overlay_plan.svg"
        _write_overlay_svg(overlay_path, p1, footprint, zones, claims, exterior_openings, affine)
        # S9 per-edge ancestry (source_map)
        source_map = _build_source_map(request, plan_view, zones, exterior_openings, gtv3_handles,
                                       zone_edge_handles, footprint, p1, tols)
        _validate_raster_intents(request, ezdxf.readfile(str(augmented_path)), tols, diags)
        # G9 v3 preflight on the augmented bundle
        g9_ok, g9_code, g9_document = _run_g9_v3_preflight(augmented_path, manifest, tooling)
        result.elevation_document = g9_document
        pairing_drift: list[str] = []
        if not g9_ok:
            _add(diags, _diag("tarch_v3_precondition",
                              points_dxf_mm=[(p1.footprint_polygon.centroid.x,
                                              p1.footprint_polygon.centroid.y)],
                              context={"v3_code": g9_code}))
        elif elevation_bound:
            # spec §6.5 [S]: the converter pairing ledger must match the GT that was
            # actually produced.  Without it the pre-linking is never consumed and the
            # audited z can drift silently away from the authoritative GT z.
            # Guarded by `elevation_bound`: when an E0--E4 gate already BLOCKed, the
            # manifest carries no elevation bindings, so running the postcheck would
            # only manufacture a secondary diagnostic for an already-reported failure.
            pairing_drift = _verify_pairing_consistency(g9_document, elevation_records, request)
            if pairing_drift:
                g9_ok = False
                g9_code = "elevation_pairing_drift"
                _add(diags, _diag("tarch_elevation_pairing_drift",
                                  handles=sorted({part for reason in pairing_drift for part in reason.split(":")[1:2]
                                                  if part and all(c in "0123456789ABCDEF" for c in part)}),
                                  context={"reasons": pairing_drift[:8]}))
        gates.append(GateResultV1(id="G9", name="v3 extraction preflight", passed=g9_ok,
                                  evidence={"v3_code": g9_code,
                                            "pairing_drift": pairing_drift or None}))
        # G10 is deliberately not a file-exists check: candidate != signed.
        g10_ok, g10_evidence = _verify_human_review_ack(work_dir, request, Path(dxf_path), overlay_path)
        gates.append(GateResultV1(id="G10", name="human-review overlay", passed=g10_ok,
                                  evidence=g10_evidence))
        # A signed overlay acknowledgement is also the explicit human decision for
        # the near-threshold face list.  It is the only path that may clear G6's
        # otherwise fail-closed pending-review subcondition.
        if g10_ok and g10_evidence.get("near_threshold_confirmed"):
            gates = [g.model_copy(update={"passed": True,
                                           "evidence": {**g.evidence, "human_confirmation": "signed"}})
                     if g.id == "G6" and g.evidence.get("human_confirmation_required") else g
                     for g in gates]
        result.manifest = manifest
        result.augmented_dxf_path = augmented_path
        result.overlay_path = overlay_path
        result.gtv3_handles = gtv3_handles
        result.gtv3_zone_edge_handles = zone_edge_handles
        result.source_map = source_map
    else:
        overlay_path = work_dir / "overlay_diagnostics.svg"
        _write_diagnostic_overlay(overlay_path, diags)
        gates.append(GateResultV1(id="G9", name="v3 extraction preflight", passed=False,
                                  evidence={"reason": "blocked upstream; no bundle built"}))
        gates.append(GateResultV1(id="G10", name="human-review overlay", passed=False,
                                  evidence={"reason": "blocked upstream", "overlay_asset": overlay_path.name,
                                            "verification_status": "blocked"}))
        result.overlay_path = overlay_path

    # Elevation E0--E4 diagnostics are emitted after the legacy plan gates were
    # assembled.  Fold their declared registry ownership back into those gates;
    # otherwise a malformed v3 intent could leave G1/G3 nominally green.
    blocked_gates = {gate for diag in diags if diag.severity == DiagnosticSeverity.BLOCK
                     for gate in diagnostic_spec(diag.code).gates}
    gates = [gate.model_copy(update={"passed": False,
                                     "evidence": {**gate.evidence, "elevation_block": True}})
             if gate.id in blocked_gates else gate for gate in gates]
    # deterministic gate ordering G1..G10
    order = {"G1": 0, "G2": 1, "G3": 2, "G4": 3, "G5": 4, "G6": 5, "G7": 6, "G8": 7, "G9": 8, "G10": 9}
    gates.sort(key=lambda g: order[g.id])
    # Test-only mutation seam: the canonical mutation suite starts a fresh
    # process for each value and permits exactly one final gate to be neutered.
    # It is intentionally opt-in and never set by normal conversion callers.
    gates = _apply_test_neuter(gates)
    result.gates = gates
    result.diagnostics = diags
    result.conversion_report = build_p2_report(result, request, plan_view, tooling,
                                               dxf_path, augmented_path)
    return result


def _derive_multifloor_plan(dxf_path: Path, request: TarchConversionRequestV1,
                            plan_view: PlanViewIntentV1, tooling) -> P2ConversionResult:
    """Run the unchanged per-plan S0--S8 geometry without persisting a document."""
    tols = _tols_from(tooling, request.metres_per_unit)
    affine = plan_view.world_from_source_m
    p1 = run_p1_plan_view(dxf_path, request, plan_view, tooling)
    diagnostics = list(p1.diagnostics)
    if any(diag.severity == DiagnosticSeverity.BLOCK for diag in diagnostics):
        cavities, wall_region, footprint, near, claims = [], Polygon(), Polygon(), [], []
    else:
        cavities, wall_region, footprint, near = s5_identify_cavities(
            p1, request, tols, diagnostics, affine)
        claims = s6_bind_intent(cavities, plan_view, tols, diagnostics, affine)
    zones: list[ZoneExpansion] = []
    if claims and not any(diag.severity == DiagnosticSeverity.BLOCK for diag in diagnostics):
        zones = s7_expand_zones(claims, wall_region, footprint, tols, diagnostics,
                                p1.wall_lines, p1.wall_bands, p1.wall_line_layers,
                                tuple(v / request.metres_per_unit
                                      for v in request.wall_thickness_range_m))
    result = P2ConversionResult(
        p1=p1, cavities=cavities, wall_region=wall_region, footprint=footprint,
        near_threshold_faces=near, zones=zones, claims=claims, gates=[],
        diagnostics=diagnostics)
    result.gates = _build_p2_gates(
        p1, cavities, wall_region, footprint, zones, claims, near,
        request, plan_view, tols, diagnostics)
    return result


def _aggregate_plan_gates(plan_runs: list[tuple[PlanViewIntentV1, P2ConversionResult]]) -> list[GateResultV1]:
    """Fold G1--G8 with explicit per-view evidence; no gate is averaged away."""
    gates: list[GateResultV1] = []
    for gate_id in ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8"):
        rows = []
        name = ""
        for plan_view, result in plan_runs:
            gate = next(item for item in result.gates if item.id == gate_id)
            name = gate.name
            rows.append({"view_id": plan_view.id, "floor_id": plan_view.floor_id,
                         "passed": gate.passed, "evidence": gate.evidence})
        scope = "mixed_document_and_plan" if gate_id == "G1" else "per_plan"
        evidence: dict[str, Any] = {"scope": scope, "views": rows}
        if gate_id == "G6":
            near_faces = [{"view_id": row["view_id"], "floor_id": row["floor_id"],
                           "faces": row["evidence"].get("near_threshold_faces", [])}
                          for row in rows
                          if row["evidence"].get("near_threshold_faces")]
            evidence.update({
                "near_threshold_faces": near_faces,
                "human_confirmation_required": bool(near_faces),
            })
        gates.append(GateResultV1(id=gate_id, name=name,
                                  passed=all(row["passed"] for row in rows),
                                  evidence=evidence))
    return gates


def _collect_multifloor_diagnostics(
        plan_runs: list[tuple[PlanViewIntentV1, P2ConversionResult]]) -> list[ConversionDiagnosticV1]:
    """Keep document-level input failures once while retaining every plan-local fact."""
    document_codes = {"tarch_input_source_hash_mismatch", "tarch_source_proxy_present",
                      "tarch_units_undeclared"}
    seen_document_codes: set[str] = set()
    diagnostics = []
    for _view, plan in plan_runs:
        for diagnostic in plan.diagnostics:
            if diagnostic.code in document_codes:
                if diagnostic.code in seen_document_codes:
                    continue
                seen_document_codes.add(diagnostic.code)
            diagnostics.append(diagnostic)
    return diagnostics


def _validate_multifloor_request(request: TarchConversionRequestV1) -> list[PlanViewIntentV1]:
    floor_ids = [floor.id for floor in request.floors]
    plan_floor_ids = [view.floor_id for view in request.plan_views]
    if len(floor_ids) != len(set(floor_ids)) or sorted(plan_floor_ids) != sorted(floor_ids):
        raise ValueError("tarch_multifloor_plan_floor_mismatch")
    dialects = [view.dialect_rules for view in request.plan_views]
    if any(dialect != dialects[0] for dialect in dialects[1:]):
        raise ValueError("tarch_multifloor_dialect_mismatch")
    order = {floor.id: index for index, floor in enumerate(request.floors)}
    return sorted(request.plan_views, key=lambda view: order[view.floor_id])


def run_tarch_conversion(dxf_path: Path, request: TarchConversionRequestV1,
                         tooling, work_dir: Path):
    """Run one document; preserve the frozen one-plan path byte-for-byte."""
    if len(request.plan_views) == 1 and len(request.floors) == 1:
        return run_p2_conversion(dxf_path, request, request.plan_views[0], tooling, work_dir)

    assert_staging_input(Path(dxf_path))
    work_dir = Path(work_dir)
    assert_staging_work_dir(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    plan_views = _validate_multifloor_request(request)
    plan_runs = [(plan_view, _derive_multifloor_plan(dxf_path, request, plan_view, tooling))
                 for plan_view in plan_views]
    diagnostics = _collect_multifloor_diagnostics(plan_runs)
    result = MultiFloorConversionResult(
        plan_results=[plan for _view, plan in plan_runs], gates=[],
        diagnostics=diagnostics)
    source_sha256 = hashlib.sha256(Path(dxf_path).read_bytes()).hexdigest()
    augmented_path: Path | None = None
    persisted: list[_PersistedPlan] = []
    document_ready = all(plan.zones and not plan.has_block for _view, plan in plan_runs)

    if document_ready:
        augmented_path = work_dir / "normalized.dxf"
        persisted = _write_multifloor_augmented_dxf(
            Path(dxf_path), augmented_path, plan_runs,
            source_sha256, request.request_sha256)
        manifest = _build_multifloor_manifest(request, persisted, augmented_path)
        elevation_records: list[_ElevationRecord] = []
        elevation_bound = False
        if request.request_version == 3:
            from pydantic import ValidationError as PydanticValidationError
            from .gt_extraction import ExtractionError, ExtractionInputs, extract_gt_v3
            from .gt_schema import REPO_ROOT, GtValidationError, compute_gt_implementation_hashes
            plan_gt = None
            try:
                plan_gt = extract_gt_v3(ExtractionInputs(
                    augmented_path, manifest, tooling,
                    compute_gt_implementation_hashes(REPO_ROOT)))
            except (ExtractionError, GtValidationError, PydanticValidationError) as exc:
                first = plan_runs[0][1].p1.footprint_polygon.centroid
                _add(diagnostics, _diag("tarch_v3_precondition",
                                       points_dxf_mm=[(first.x, first.y)],
                                       context={"v3_code": str(exc),
                                                "stage": "elevation_plan_prepass"}))
            if plan_gt is not None:
                elevation_records, elevation_diags = _v3_elevation_records(
                    augmented_path, request, plan_gt,
                    _tols_from(tooling, request.metres_per_unit))
                diagnostics.extend(elevation_diags)
                if not any(diag.severity == DiagnosticSeverity.BLOCK
                           for diag in elevation_diags):
                    try:
                        elevation_records = _append_elevation_outlines(
                            augmented_path, elevation_records,
                            _tols_from(tooling, request.metres_per_unit).quant_native,
                            source_sha256, request.request_sha256)
                    except ValueError:
                        handles = ([elevation_records[0].raw_handles[0]]
                                   if elevation_records else [plan_views[0].id])
                        _add(diagnostics, _diag(
                            "tarch_elevation_normalized_outline_drift", handles=handles))
                    else:
                        manifest = _build_multifloor_manifest(
                            request, persisted, augmented_path, elevation_records)
                        elevation_bound = True
        result.elevation_records = elevation_records
        overlay_path = work_dir / "overlay_plan.svg"
        _write_multifloor_overlay_svg(overlay_path, persisted)

        source_entries = []
        for item in persisted:
            source_map = _build_source_map(
                request, item.plan_view, item.result.zones, item.exterior_openings,
                item.gtv3_handles, item.zone_edge_handles, item.result.footprint,
                item.result.p1, _tols_from(tooling, request.metres_per_unit))
            source_entries.extend(source_map.entries)
        source_map = SourceMapV1(
            map_version=1, case=request.case, entries=source_entries,
            source_map_sha256="0" * 64)
        source_map = source_map.model_copy(update={
            "source_map_sha256": compute_source_map_sha256(source_map)})

        footprint_handles = {item.plan_view.id:
                             item.gtv3_handles[GTV3_FOOTPRINT_LAYER]
                             for item in persisted}
        _validate_raster_intents(
            request, ezdxf.readfile(str(augmented_path)),
            _tols_from(tooling, request.metres_per_unit), diagnostics,
            footprint_handles)
        g9_ok, g9_code, g9_document = _run_g9_v3_preflight(
            augmented_path, manifest, tooling)
        result.elevation_document = g9_document
        pairing_drift: list[str] = []
        if not g9_ok:
            first = plan_runs[0][1].p1.footprint_polygon.centroid
            _add(diagnostics, _diag("tarch_v3_precondition",
                                   points_dxf_mm=[(first.x, first.y)],
                                   context={"v3_code": g9_code}))
        elif elevation_bound:
            pairing_drift = _verify_pairing_consistency(
                g9_document, elevation_records, request)
            if pairing_drift:
                g9_ok = False
                g9_code = "elevation_pairing_drift"
                _add(diagnostics, _diag(
                    "tarch_elevation_pairing_drift",
                    handles=sorted({part for reason in pairing_drift
                                    for part in reason.split(":")[1:2]
                                    if part and all(char in "0123456789ABCDEF"
                                                    for char in part)}),
                    context={"reasons": pairing_drift[:8]}))
        g9 = GateResultV1(
            id="G9", name="v3 extraction preflight", passed=g9_ok,
            evidence={"v3_code": g9_code,
                      "pairing_drift": pairing_drift or None})
        g10_ok, g10_evidence = _verify_human_review_ack(
            work_dir, request, Path(dxf_path), overlay_path)
        g10 = GateResultV1(id="G10", name="human-review overlay",
                          passed=g10_ok, evidence=g10_evidence)
        if g10_ok and g10_evidence.get("near_threshold_confirmed"):
            for _view, plan in plan_runs:
                plan.gates = [gate.model_copy(update={
                    "passed": True,
                    "evidence": {**gate.evidence, "human_confirmation": "signed"}})
                    if gate.id == "G6"
                    and gate.evidence.get("human_confirmation_required") else gate
                    for gate in plan.gates]
        result.gates = [*_aggregate_plan_gates(plan_runs), g9, g10]
        blocked_gates = {gate for diag in diagnostics
                         if diag.severity == DiagnosticSeverity.BLOCK
                         for gate in diagnostic_spec(diag.code).gates}
        result.gates = [gate.model_copy(update={
            "passed": False, "evidence": {**gate.evidence, "elevation_block": True}})
            if gate.id in blocked_gates else gate for gate in result.gates]
        result.manifest = manifest
        result.augmented_dxf_path = augmented_path
        result.overlay_path = overlay_path
        result.source_map = source_map
    else:
        overlay_path = work_dir / "overlay_diagnostics.svg"
        _write_diagnostic_overlay(overlay_path, diagnostics)
        result.gates = [*_aggregate_plan_gates(plan_runs),
                        GateResultV1(id="G9", name="v3 extraction preflight",
                                     passed=False, evidence={"reason": "blocked upstream; no bundle built"}),
                        GateResultV1(id="G10", name="human-review overlay",
                                     passed=False, evidence={"reason": "blocked upstream",
                                                            "overlay_asset": overlay_path.name,
                                                            "verification_status": "blocked"})]
        result.overlay_path = overlay_path

    order = {f"G{index}": index for index in range(1, 11)}
    result.gates.sort(key=lambda gate: order[gate.id])
    result.gates = _apply_test_neuter(result.gates)
    result.diagnostics = diagnostics
    result.conversion_report = build_multifloor_report(
        result, request, persisted, tooling, Path(dxf_path), augmented_path)
    return result


def build_p2_report(result: P2ConversionResult, request: TarchConversionRequestV1,
                    plan_view: PlanViewIntentV1, tooling, source_dxf: Path,
                    augmented_dxf: Path | None) -> ConversionReportV1:
    """Build the full ConversionReportV1 (zones + cavities + all 10 gates)."""
    affine = plan_view.world_from_source_m
    mpu = request.metres_per_unit
    p1_report = build_p1_report(result.p1, request, plan_view, tooling,
                                hashlib.sha256(Path(source_dxf).read_bytes()).hexdigest())

    # cavities (claimed_by resolved against the parallel claims list)
    claimed_indices = {c["cavity_index"] for c in result.claims}
    cavities_r: list[CavityReportV1] = []
    # order cavities the same way S6 ordered them for stable cavity_id pairing
    ordered = sorted(enumerate(result.cavities),
                     key=lambda kv: (round(kv[1].bounds[0], 6), round(kv[1].bounds[1], 6)))
    claim_by_index = {c["cavity_index"]: c for c in result.claims}
    for k, (orig_i, g) in enumerate(ordered):
        claim = claim_by_index.get(orig_i)
        cavities_r.append(CavityReportV1(
            cavity_id=f"c_{k:02d}", floor_id=plan_view.floor_id,
            area_m2=g.area * mpu * mpu,
            vertices_m=[_to_world((x, y), affine) for x, y in list(g.exterior.coords)[:-1]],
            claimed_by=(claim["zone_id"] if claim else None),
            claim_source=("intent_file" if claim else "unclaimed")))

    # zones (edges carry their recorded basis + thickness — the D7/G8 compensation record;
    # source_handles = the real source wall-line handle(s) the cavity edge lay on; the
    # derived GTV3_ZONE handle is the zoning LINE emitted for wall_axis edges and
    # the outer_skin transition extensions not carried in full by the footprint)
    zeh = result.gtv3_zone_edge_handles or {}
    zones_r: list[ZoneReportV1] = []
    for k, (z, claim) in enumerate(zip(result.zones, result.claims)):
        edges_r: list[ZoneEdgeReportV1] = []
        verts = z.vertices
        m = len(verts)
        for i, edge in enumerate(z.edges):
            if edge.basis is None:
                continue  # thickness-change step (no wall-basis record)
            a, b = verts[i], verts[(i + 1) % m]
            edges_r.append(ZoneEdgeReportV1(
                p1=_to_world(a, affine), p2=_to_world(b, affine), basis=edge.basis,
                thickness_m=(edge.thickness_native or 0.0) * mpu,
                offset_m=edge.offset_native * mpu,
                derived_handle=zeh.get((k, i)),
                source_handles=edge.source_handles,
                thickness_evidence=edge.thickness_evidence))
        if not edges_r:
            continue
        zones_r.append(ZoneReportV1(
            zone_id=claim["zone_id"], floor_id=plan_view.floor_id, name=claim["name"],
            role=claim["role"],
            role_source=("declared_absent" if claim["role"] == "unspecified" else "intent_file"),
            seed_point_world_m=_to_world(z.seed_native, affine),
            polygon_m=PolygonIRV1(exterior=RingV1(vertices=[_to_world(v, affine) for v in verts])),
            edges=edges_r))

    all_gates_passed = {g.id for g in result.gates} == {f"G{i}" for i in range(1, 11)} and all(
        g.passed for g in result.gates)
    status = "PASS" if not result.has_block and all_gates_passed else "BLOCKED"
    norm_hash = None
    if status == "PASS":
        norm_hash = hashlib.sha256(Path(augmented_dxf).read_bytes()).hexdigest() \
            if augmented_dxf is not None \
            else hashlib.sha256(Path(source_dxf).read_bytes()).hexdigest()
    audit_rows = []
    intents = {v.id: v for v in request.elevation_views if isinstance(v, DatumBoundNamedElevationViewIntentV3)}
    # Join key: the GT's opening_elevation refs carry the generated handle that this
    # ledger record produced — the same link §6.5 verifies.  Taking opening id / host
    # zone / plan interval straight from the extracted GT keeps the human table and the
    # authoritative document on ONE derivation (spec §7.4 [S] requires both the opening
    # id and the plan-side interval; without them the mirror-residual cross-check the
    # section mandates cannot be performed at all, and the table cannot be joined to the
    # overlay, which labels openings by GT id).
    opening_by_handle: dict[str, Any] = {}
    if result.elevation_document is not None:
        for opening in result.elevation_document.openings:
            for ref in opening.source_refs:
                if ref.role == "opening_elevation":
                    opening_by_handle[ref.entity_handle] = opening
    for rec in sorted(result.elevation_records, key=lambda r: (r.view_id, r.kind, r.raw_handles)):
        intent = intents[rec.view_id]; axis = intent.world_along_from_source_m.source_axis
        source_along = (rec.rect[0], rec.rect[2]) if axis == "x" else (rec.rect[1], rec.rect[3])
        opening = opening_by_handle.get(rec.generated_handle)
        audit_rows.append({"opening_id": None if opening is None else opening.id,
                           "evidence_id": f"ev_{rec.view_id}_{min(rec.raw_handles).lower()}", "view_id": rec.view_id,
                           "facade_family": rec.facade, "floor_id": intent.floor_ids[0], "kind": rec.kind,
                           "host_zone_id": None if opening is None else opening.host_zone_id,
                           "plan_world_along_interval": None if opening is None else
                               [opening.world_along_interval.lo, opening.world_along_interval.hi],
                           "elevation_source_along_interval": list(source_along),
                           "world_along_interval": sorted([x * intent.world_along_from_source_m.scale + intent.world_along_from_source_m.offset for x in source_along]),
                           # identical call the §6.5 postcheck compares against the GT
                           "z_interval": list(_converter_elevation_z(rec, intent)),
                           "datum_entity_handle": rec.datum_handle, "datum_source_start_point": list(rec.datum_start), "datum_source_end_point": list(rec.datum_end),
                           "declared_world_along_lo_source_endpoint": rec.declared_lo_endpoint,
                           "mapped_endpoint_pair": f"{rec.declared_lo_endpoint}->plan.lo", "raw_source_handles": list(rec.raw_handles), "structural_source_handles": list(rec.structural_handles)})
    return ConversionReportV1(
        report_version=1, status=status, case=request.case,
        source_dxf_sha256=hashlib.sha256(Path(source_dxf).read_bytes()).hexdigest(),
        normalized_dxf_sha256=norm_hash,
        request_sha256=request.request_sha256,
        judge_config_sha256=tooling.judge_config_sha256,
        vg_config_sha256=tooling.vg_config_sha256,
        converter_sha256=converter_sha256(),
        profile_version=1, quantization_step_m=derive_quantization_step(tooling),
        walls=p1_report.walls, openings=p1_report.openings, cavities=cavities_r, zones=zones_r,
        gates=result.gates, diagnostics=result.diagnostics,
        wall_proof_coverage=p1_report.wall_proof_coverage,
        zone_intent_coverage={"expected_count": plan_view.zone_intent.expected_count,
                              "cavity_count": len(result.cavities),
                              "near_threshold_faces": result.near_threshold_faces},
        elevation_audit_rows=audit_rows)


def build_multifloor_report(result: MultiFloorConversionResult,
                            request: TarchConversionRequestV1,
                            persisted: list[_PersistedPlan], tooling,
                            source_dxf: Path, augmented_dxf: Path | None) -> ConversionReportV1:
    """Build one report whose plan geometry and gate evidence remain floor-scoped."""
    plan_views = _validate_multifloor_request(request)
    partials = []
    for plan_view, plan_result in zip(plan_views, result.plan_results):
        partials.append((plan_view, build_p2_report(
            plan_result, request, plan_view, tooling, source_dxf, augmented_dxf)))

    all_gates_passed = ({gate.id for gate in result.gates}
                        == {f"G{index}" for index in range(1, 11)}
                        and all(gate.passed for gate in result.gates))
    status: Literal["PASS", "BLOCKED"] = (
        "PASS" if not result.has_block and all_gates_passed else "BLOCKED")
    normalized_hash = None
    if status == "PASS":
        normalized_hash = hashlib.sha256(
            (augmented_dxf or source_dxf).read_bytes()).hexdigest()

    opening_by_handle: dict[str, Any] = {}
    if result.elevation_document is not None:
        for opening in result.elevation_document.openings:
            for ref in opening.source_refs:
                if ref.role == "opening_elevation":
                    opening_by_handle[ref.entity_handle] = opening
    intents = {intent.id: intent for intent in request.elevation_views
               if isinstance(intent, DatumBoundNamedElevationViewIntentV3)}
    audit_rows = []
    for record in sorted(result.elevation_records,
                         key=lambda item: (item.view_id, item.kind, item.raw_handles)):
        intent = intents[record.view_id]
        axis = intent.world_along_from_source_m.source_axis
        source_along = ((record.rect[0], record.rect[2]) if axis == "x"
                        else (record.rect[1], record.rect[3]))
        opening = opening_by_handle.get(record.generated_handle)
        z_interval = _converter_elevation_z(record, intent)
        floor_id = None if opening is None else opening.floor_id
        if floor_id is None:
            containing = [floor.id for floor in request.floors
                          if floor.id in intent.floor_ids
                          and floor.z_floor_m <= z_interval[0] < z_interval[1]
                          <= floor.z_floor_m + floor.ceiling_height_m]
            floor_id = containing[0] if len(containing) == 1 else None
        audit_rows.append({
            "opening_id": None if opening is None else opening.id,
            "evidence_id": f"ev_{record.view_id}_{min(record.raw_handles).lower()}",
            "view_id": record.view_id, "facade_family": record.facade,
            "floor_id": floor_id, "kind": record.kind,
            "host_zone_id": None if opening is None else opening.host_zone_id,
            "plan_world_along_interval": None if opening is None else
                [opening.world_along_interval.lo, opening.world_along_interval.hi],
            "elevation_source_along_interval": list(source_along),
            "world_along_interval": sorted([
                value * intent.world_along_from_source_m.scale
                + intent.world_along_from_source_m.offset for value in source_along]),
            "z_interval": list(z_interval),
            "datum_entity_handle": record.datum_handle,
            "datum_source_start_point": list(record.datum_start),
            "datum_source_end_point": list(record.datum_end),
            "declared_world_along_lo_source_endpoint": record.declared_lo_endpoint,
            "mapped_endpoint_pair": f"{record.declared_lo_endpoint}->plan.lo",
            "raw_source_handles": list(record.raw_handles),
            "structural_source_handles": list(record.structural_handles),
        })

    return ConversionReportV1(
        report_version=1, status=status, case=request.case,
        source_dxf_sha256=hashlib.sha256(source_dxf.read_bytes()).hexdigest(),
        normalized_dxf_sha256=normalized_hash,
        request_sha256=request.request_sha256,
        judge_config_sha256=tooling.judge_config_sha256,
        vg_config_sha256=tooling.vg_config_sha256,
        converter_sha256=converter_sha256(), profile_version=1,
        quantization_step_m=derive_quantization_step(tooling),
        walls=[wall for _view, partial in partials for wall in partial.walls],
        openings=[opening for _view, partial in partials for opening in partial.openings],
        cavities=[cavity for _view, partial in partials for cavity in partial.cavities],
        zones=[zone for _view, partial in partials for zone in partial.zones],
        gates=result.gates, diagnostics=result.diagnostics,
        wall_proof_coverage={
            "scope": "per_plan",
            "views": [{"view_id": view.id, **partial.wall_proof_coverage}
                      for view, partial in partials]},
        zone_intent_coverage={
            "scope": "per_plan",
            "views": [{"view_id": view.id, **partial.zone_intent_coverage}
                      for view, partial in partials]},
        elevation_audit_rows=audit_rows)


__all__ = [
    "run_p1_plan_view", "build_p1_report", "converter_sha256",
    "KNOWN_PRE_F_D_CONVERTER_SHA256", "CONVERTER_CLOSURE_FILES",
    "P1PlanViewGeometry", "ResolvedOpening", "WallBand",
    "run_p2_conversion", "run_tarch_conversion", "build_p2_report",
    "build_multifloor_report", "P2ConversionResult", "MultiFloorConversionResult",
    "ZoneExpansion",
    "g8_reconstruct_wall_region", "s5_identify_cavities", "s6_bind_intent",
    "s7_expand_zones", "GTV3_FOOTPRINT_LAYER", "GTV3_ZONE_LAYER", "GTV3_OPENING_LAYER", "GTV3_ELEV_OPENING_LAYER", "elevation_block_definition_sha256",
]
