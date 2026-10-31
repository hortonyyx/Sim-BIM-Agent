"""``AsMeasuredV1`` -- the first of the facts layer's three cuts.

    as_measured  +  revisions  =>  as_signed        (architecture/gt_revision_ledger.md §一)
    ^^^^^^^^^^^ THIS FILE.  ⛔ ``revisions`` / ``as_signed`` are ②-1b, not here.

⭐ WHAT IT IS: what the machine measured off the **un-retouched** drawing.
⛔ WHAT IT IS NOT: an answer.  Nothing here is projected, expanded, or given a
modelling ``basis``; ⛔ this file does not know what an outer skin is.

## Projection choices it deliberately does NOT store

1. ``basis`` -- axis vs outer-skin is a CHOICE MADE WHEN COMPILING AN ANSWER
   (guide §十.6b), not something a drawing can be measured for.  ⇒ ②-1c.
2. **expanded endpoints** -- F-122 measured that the S7 zone-edge report's
   per-edge endpoints are the answer edges AFTER S7 pushed them out by t or t/2
   (``offset_m == (t if outer_skin else t/2)``, 136/136; all 272 endpoints
   already 0.060-0.339 m outside their claimed cavity).  Copying them in would
   be a semantic reverse-migration, and any compiler then validated against
   them would only replay the producer's own choice.
``boundary_condition`` is now a first-class, cavity-side edge fact (②-1d).
It is derived before any answer-profile offset is selected and independently
rechecked by the answer compiler; it is not copied from stored ``basis``.

⭐ Consequently the source of every field here is **P1 (S0-S4)**, i.e.
``run_p1_plan_view``, and ⛔ never S5-S7.  ``test_as_measured_facts_layer.py``
greps this module for the S7 names and fails if one appears.

## Junctions: the layer stores LINES, ⛔ not corner points

Guide §十.6c, measured on sm25: all 54 orthogonal junctions agree -- a wall line
stops at the NEAR FACE of the wall it runs into, so the corner square is claimed
by neither.  A facts layer that stored per-edge corner points would freeze one
particular junction resolution into the record and turn F-134 from "solved in
the S7 expansion step" into "unsolved".  So a wall is stored as
``(two face lines, thickness, along-wall interval)`` and the along-wall interval
is for the denominator and for locating openings -- ⛔ NOT for deciding corners.

## ⭐⭐ A wall is TWO PAIRED FACE LINES -- ⛔ not a converter ``wall_band``

②-1a took ``walls`` straight off ``P1PlanViewGeometry.wall_bands``.  MEASURED
(②-1a-R, 2026-08-29) that was the WRONG SOURCE: ``WallBand``'s own docstring
says it is "grouped from its **jamb caps**" -- the little strokes that cross a
wall at a door or a window -- ⛔ not from the two faces of a wall.  Signed
drawing, ``plan-F1``:

    from ``wall_bands``   45 "walls", thickness {100:1, 120:5, 240:7, 296:1,
                              300:16, 304:1, 356:1, 360:11, 364:1, 500:1} mm
    from face pairing     55 walls,   thickness {120: 28, 240: 27} mm

⛔ The drawing has no 300 mm wall.  Those 16 were a door/window JAMB paired
with the face of a real 120 mm partition 300 mm away, so the reported
"thickness" was the distance from an opening frame to the next wall.  That is
33 walls that do not exist, about to be written into the standard answer that
BOTH reading and correction are then graded against.

⭐ So ``walls`` is now derived by ``denominator.face_line_targets`` -- THE SAME
D1-D5 pass the scoreable denominator runs, ⛔ never a second implementation of
"what a wall face is" -- and the pairing rule is: same axis, overlapping along
the wall, NEAREST opposite face.  ⛔ There is no thickness threshold anywhere in
it: filtering candidate pairs by declared thickness is precisely the mechanism
that silently deleted sm24's entire 120 mm partition family (batch guide §一).

⛔⛔ THE TRAP THAT MAKES THIS NON-OBVIOUS: ``face_lines`` holds 225 strokes on
signed ``plan-F1`` and only 110 of them are pairable.  Pairing all 225 puts the
very same ghosts back, because the other 115 ARE the jamb caps and stubs.  The
exclusion is D2's, REUSED.

⚠️ AXIS CONVENTION -- written down because BOTH seats mis-read it while
diagnosing this: ``denominator``'s ``axis`` names the axis the CONSTANT
coordinate sits on, this module's ``axis`` names the axis a line RUNS ALONG.
They are OPPOSITE.  :func:`_pair_face_lines_into_walls` flips it exactly once,
deliberately ([[cross-representation-mutation-must-be-equivalent]]: a field
NAMED one thing and HOLDING another is how that whole family of errors runs).

⭐ Nothing is deleted by the change: the 45 bands are still carried, verbatim,
as ``converter_readouts.jamb_cap_bands`` -- under a name that says what they
are grouped from.

## TWO different quantities: storage UNIT (0.1 mm ints) · ingest RESOLUTION (1 mm)

User 2026-08-29: coordinates are **stored** as integers in units of 0.1 mm.
⛔ That half is not a tolerance and ⛔ not a snapping pass -- it is the
*storage type*.  Floats cannot be compared bit-for-bit after a round trip, and
F-98's family of "the two rulers disagree in the 12th decimal" problems is a
property of the representation, not of the geometry.  Every geometric number
below is ``int``; ⛔ there is no float in the document outside
``converter_readouts``, where the converter's own records ride out VERBATIM
(see below).  ``test_as_measured_facts_layer.py`` asserts exactly that.

User 2026-09-05 (A-11, 「走乙」): **separately** from the storage type, the
**ingest resolution** is 1 mm (:data:`INGEST_RESOLUTION_UNITS`) -- geometric
coordinates are snapped to the nearest 1 mm grid point by :func:`_geom_units`
BEFORE they enter the document.  That half IS a snap, deliberately: the
converter's own quantisation leaves ±0.1 mm representation residue off the
integer-millimetre grid (MEASURED on sm25 as-received: 74 of 2812 geometric
integers), and the user has ruled that this residue belongs to the
**measurement representation**, ⛔ not to ``drawing_error`` -- it must never
reach the ``revisions`` ledger as a signed correction.  The two statements do
not contradict each other: the storage unit says how fine a grid the document
*can* express; the ingest resolution says how fine the values that actually
enter it *are*.  ⛔ The snap owns ONLY the coordinate paths itemised in
``_geom_units``' docstring -- verbatim converter readouts, the raw pre-snap
``before_*`` observations, and non-coordinate quantities (lengths, areas,
counts) are explicitly outside it, and :func:`scan_ingest_resolution_violations`
is the exit scan that re-checks the whole serialised document against that
boundary.

## ⛔ Nothing is silently re-measured from the drawing

The measured geometry is either copied from ``P1PlanViewGeometry`` or is a unit
conversion of something copied from it.  Boundary conditions are a named
topological derivation over those stored integers, with an independent
compiler-side recomputation.  The 2026-08-29 lesson (the converter had already
computed the readouts and the consumer dropped them on the floor) is why
``dangles`` / ``cuts`` / ``invalid`` / ``diagnostics`` / ``gates`` are carried
through untouched, ⛔ never recomputed.

⭐ And nothing is dropped silently either ([[absence-conflates-causes-in-observables]]).
``wall_lines`` can legitimately contain a stroke that is neither horizontal nor
vertical (measured: 1 in as-received ``plan-F1`` -- F-129).  It cannot be a
"face line at a constant coordinate", so it is itemised in
``converter_readouts.non_orthogonal_lines`` and a model validator enforces the
ledger identity

    wall_lines_total == len(face_lines) + non_orthogonal + degenerate

so a stroke can never leave the record by simply not being appended to a list.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from .gt_manifest import Affine2D, load_gt_tooling_config
from .gt_schema import (REPO_ROOT, DxfHandle, Hex64, HumanLabel, StableId,
                        StrictFiniteFloat, StrictNonNegativeInt)
from .tarch_converter_schema import (JsonDict, TarchConversionRequestV1,
                                     _StrictModel, compute_request_sha256)
from .as_drawn.denominator import MERGE_M, face_line_targets
from .tarch_normalize import P1PlanViewGeometry, converter_sha256, run_p1_plan_view

GT_CFG = REPO_ROOT / "src/configs/judge_gt.yaml"
VG_CFG = REPO_ROOT / "src/configs/correction.yaml"

#: Storage unit.  1 world metre == 10000 units.  ⛔ Declared, never inferred.
UNITS_PER_METRE = 10_000
UNIT_LABEL = "0.1mm"

#: ⭐ A-11 (user 2026-09-05, 「走乙」): the INGEST RESOLUTION, expressed in the
#: document's own storage units -- 10 units == 1 mm.  This is the ONE
#: declaration point for "gt 入库分辨率 = 1 mm" on the facts path; every
#: snapping decision reads it through :func:`snap_to_ingest_resolution` /
#: :func:`_geom_units`, ⛔ never a second literal.
#:
#: ⚠️ NOT to be confused with ``as_drawn/denominator.GROUP_QUANT`` (``= 3``,
#: the ``round(const, 3)`` of the D3 grouping KEY).  That is a different
#: quantity with a different unit (decimal metres of a grouping decision);
#: they are not unified and must not be, absent an argument that they should
#: always move together (dispatch A-11 §二).
INGEST_RESOLUTION_UNITS: int = 10
INGEST_RESOLUTION_LABEL: str = "1mm"


class AsMeasuredUnavailable(RuntimeError):
    """⛔ Raised instead of returning a facts document that is quietly partial.

    Same shape as ``DenominatorUnavailable`` (F-126): there is no object to keep
    using, and the reason is named.  ⛔ An empty/blocked conversion must never be
    indistinguishable from "this drawing genuinely has nothing on it".
    """

    def __init__(self, reason: str, *, view_id: str, detail: str,
                 blocking_codes: list[str] | None = None) -> None:
        self.reason = reason
        self.view_id = view_id
        self.detail = detail
        self.blocking_codes = list(blocking_codes or [])
        super().__init__(f"as_measured_unavailable[{reason}] view={view_id}: {detail}"
                         + (f" (BLOCK: {', '.join(self.blocking_codes)})"
                            if self.blocking_codes else ""))


# --------------------------------------------------------------------------- #
# unit conversion -- the ONLY place metres become storage integers
# --------------------------------------------------------------------------- #
def to_units(metres: float) -> int:
    """World metres -> 0.1 mm integer.  ⛔ One implementation, used everywhere.

    ⚠️ A-11: this stays the PURE representation change (metres -> storage
    integers), ⛔ with no snap inside -- a value already in 0.1 mm integers
    must survive it bit-for-bit, which is exactly what
    ``test_r2_to_units_is_the_declared_scale`` pins.  The 1 mm ingest
    resolution is a SECOND, separate step (:func:`_geom_units`).

    ``round`` is banker's rounding: deterministic, and the half-way case is
    0.05 mm -- three orders of magnitude below anything the converter's own
    quantisation leaves behind.
    """
    return int(round(float(metres) * UNITS_PER_METRE))


def snap_to_ingest_resolution(units: int) -> int:
    """0.1 mm integer -> the nearest 1 mm grid point (A-11).

    Pure integer arithmetic (``divmod``, no float division), so there is no
    representation surprise at any magnitude the document can hold; the
    tie-break at exactly 0.5 mm is banker's, matching :func:`to_units`' own
    documented convention.

    ⭐ IDENTITY, load-bearing (A-11 acceptance #2): a value already on the
    grid is returned UNCHANGED -- snapping may move representation residue,
    ⛔ never an on-grid value.
    """
    quotient, remainder = divmod(units, INGEST_RESOLUTION_UNITS)
    twice = 2 * remainder
    if twice > INGEST_RESOLUTION_UNITS or (
            twice == INGEST_RESOLUTION_UNITS and quotient % 2):
        quotient += 1
    return quotient * INGEST_RESOLUTION_UNITS


def _geom_units(metres: float) -> int:
    """⭐ A-11: metres -> a storage integer that is ALSO on the 1 mm ingest
    grid.  This is the ONE door every geometric coordinate walks through on
    its way into the document; :func:`to_units` underneath stays the pure
    representation change.

    ⛔⛔ THE EXPLICIT EXTENSION OF "geometric coordinate" (A-11 boundary #1:
    itemised here as call site -> document field, ⛔ not inferred from field
    names).  Every call site of THIS function:

      ``_face_line_records``       ``face_lines[*].const / .along_min / .along_max``
      ``_jamb_cap_band_records``   (band face consts, used only as the
                                   ``by_const`` lookup key against
                                   ``face_lines[*].const`` -- not emitted)
      ``_pair_face_lines_into_walls``
                                   ``walls[*].face_lo / .face_hi /
                                   .along_min / .along_max``
      ``_split_const_groups``      ``converter_readouts.
                                   face_groups_with_a_split_const[*].
                                   group_const / .member_consts``
      ``_opening_records``         ``openings[*].along_min / .along_max /
                                   .cross_lo / .cross_hi`` (and the same
                                   consts inside
                                   ``unresolved_opening_carriers[*]``)
      ``_footprint_record``        ``footprint.rings[*].points``
      ``_axis_snap_records``       ``axis_snapped_lines[*].after_p0 /
                                   .after_p1``

    Everything geometric derived downstream (wall ``thickness``; boundary
    ``cavity_const`` / ``span_lo`` / ``span_hi`` / ``p1`` / ``p2``; evidence
    ``raw_face_const`` / ``opposite_face_const`` / ``exit_point`` /
    ``footprint_edge_points``; ring-loss span consts) is computed FROM these
    stored integers and inherits the grid -- :func:`scan_ingest_resolution_
    violations` is the exit scan that re-checks all of it on the serialised
    document.

    ⛔ Explicitly NOT coordinates (unchanged by the snap -- the boundary is a
    list, not a vibe; each entry also names where it is exempted in the scan):

      ``axis_snapped_lines[*].before_p0 / .before_p1`` -- the RAW pre-snap
        observation, the very evidence a human reviews on that row
        (quantising it would falsify the record of how skew the stroke was);
      ``axis_snapped_lines[*].minor_leg_units`` -- a LENGTH observation;
      ``non_orthogonal_lines[*].p0 / .p1`` -- ⭐ SAME REASON, added
        2026-09-07 (A-11-d3).  This row's whole claim is "this stroke is
        neither horizontal nor vertical", and the 1 mm grid was ERASING the
        evidence for it: 13AF went in as ``p0=[52400, 100660]`` /
        ``p1=[52400, 99460]`` -- two IDENTICAL x, i.e. a row asserting
        skewness while displaying a perfectly vertical line (raw ezdxf:
        52400.742 -> 52398.827, a 0.1915 mm lean).  ⛔ The original A-11
        argument was made for ``axis_snapped_lines[*].before_*`` and stops
        one field-family short of its own scope: BOTH are the raw pre-
        treatment observation, ⛔ neither is a position anything is placed
        at.  These strokes are dropped from ``face_lines`` by construction,
        so no downstream geometry is computed from them;
      ``axis_snap_arbitrations[*].deviation_units / .deviation_limit_units /
        .cap_units`` -- LENGTH observations (how far the flattening would
        have moved the face, and the ceilings it was measured against),
        ⛔ not positions;
      ``converter_readouts.diagnostics`` / ``.gates`` / ``.jamb_cap_bands``
        -- the converter's own VERBATIM records (module rule: ⛔ nothing in
        there is recomputed);
      counts, indices, winding sides (``side`` / ``outward_normal``), areas
        (``area_units2``), angles -- none of them is a position.
    """
    return snap_to_ingest_resolution(to_units(metres))


#: ⭐ A-11 exit scan: the NON-coordinate integer paths of a serialised facts
#: document, EXPLICITLY exempted from the 1 mm grid check, each with its
#: reason.  ⛔ The scan (:func:`scan_ingest_resolution_violations`) checks
#: EVERY integer in the document by default -- a future field is born
#: CHECKED, and joining this table is a conscious act that must state what
#: the quantity is.  This table IS the "configuration quantity" boundary of
#: dispatch A-11 (#1/#3): what is not a coordinate gets listed, ⛔ nothing
#: gets to be non-grid by being overlooked.
#:
#: Patterns are ``.``-separated path segments against the serialised
#: document (``model_dump(mode="json")``); a ``*`` segment matches any list
#: index; matching is exact otherwise.
INGEST_NON_COORDINATE_PATHS: dict[str, str] = {
    "schema_version":
        "document schema metadata",
    "units_per_metre":
        "the storage-unit declaration (10000) -- a unit, not a coordinate",
    "derivation.deriver_version":
        "as_signed deriver version metadata",
    "views[*].footprint.rings[*].polygon_index":
        "a ring index into the footprint polygon list",
    "views[*].boundary_edges[*].sequence":
        "an edge's ordinal within its cavity",
    "views[*].boundary_edges[*].side":
        "a winding direction (-1/+1)",
    "views[*].boundary_edges[*].evidence.outward_normal[*]":
        "a direction pair (-1/+1 components)",
    "views[*].boundary_edges[*].evidence.exit_point[*]":
        "the ray-exit WITNESS, deliberately pushed one 0.1 mm unit off the "
        "face it exits (``_boundary_exit_const``'s ``farthest + outward``) "
        "so the covers test is not decided on a boundary -- snapping it "
        "would change what the classifier saw, and the schema already "
        "declares it 'a classifier witness only'",
    "views[*].boundary_ring_losses[*].area_units2":
        "an AREA (square units), not a position",
    "views[*].boundary_ring_losses[*].owner_count":
        "a count of owning wall groups",
    "views[*].boundary_ring_losses[*].span.side":
        "a winding direction (-1/+1)",
    "views[*].converter_readouts.axis_snapped_lines[*].before_p0":
        "RAW pre-snap observation -- evidence, quantising it falsifies it",
    "views[*].converter_readouts.axis_snapped_lines[*].before_p1":
        "RAW pre-snap observation -- evidence, quantising it falsifies it",
    "views[*].converter_readouts.axis_snapped_lines[*].minor_leg_units":
        "a LENGTH observation (how skew the stroke was), not a position",
    "views[*].converter_readouts.non_orthogonal_lines[*].p0":
        "RAW pre-snap observation -- evidence, quantising it falsifies it",
    "views[*].converter_readouts.non_orthogonal_lines[*].p1":
        "RAW pre-snap observation -- evidence, quantising it falsifies it",
    "views[*].converter_readouts.axis_snap_arbitrations[*].p0":
        "RAW pre-decision observation -- evidence, quantising it falsifies it",
    "views[*].converter_readouts.axis_snap_arbitrations[*].p1":
        "RAW pre-decision observation -- evidence, quantising it falsifies it",
    "views[*].converter_readouts.axis_snap_arbitrations[*].deviation_units":
        "a LENGTH observation (how far the flattening would move the face), "
        "not a position",
    "views[*].converter_readouts.axis_snap_arbitrations[*].deviation_limit_units":
        "a LENGTH observation (this stroke's tier-1 ceiling), not a position",
    "views[*].converter_readouts.axis_snap_arbitrations[*].cap_units":
        "a LENGTH observation (half the thinnest declared wall), not a position",
    "views[*].converter_readouts.axis_snap_arbitrations[*].ladder_tier":
        "the ladder tier this stroke landed on (2), not a position",
    "views[*].converter_readouts.dangles":
        "converter readout count",
    "views[*].converter_readouts.cuts":
        "converter readout count",
    "views[*].converter_readouts.invalid":
        "converter readout count",
    "views[*].converter_readouts.degenerate_line_count":
        "converter readout count",
    "views[*].converter_readouts.wall_lines_total":
        "converter readout count",
    "views[*].converter_readouts.degenerate_in_wall_lines":
        "recomputed ledger count",
    "views[*].converter_readouts.degenerate_line_handles":
        "exempt SUBTREE -- handles are strings, listed for the boundary's sake",
    "views[*].converter_readouts.diagnostics":
        "exempt SUBTREE -- the converter's VERBATIM record (may contain "
        "arbitrary integers in its own frames)",
    "views[*].converter_readouts.gates":
        "exempt SUBTREE -- the converter's VERBATIM record",
    "views[*].converter_readouts.jamb_cap_bands":
        "exempt SUBTREE -- the converter's VERBATIM record, in its native "
        "millimetre floats",
}


def _iter_int_leaves(node: Any, prefix: tuple[str, ...] = ()):
    """Yield ``(path, value)`` for every ``int`` leaf of a JSON-able tree.

    ``bool`` is excluded explicitly (it is an ``int`` subclass in Python, and
    a flag is never a coordinate); floats/strings/None are not ``int`` and
    drop out on their own.  List indices are emitted as ``*`` so paths are
    position-independent by construction.
    """
    if isinstance(node, bool):
        return
    if isinstance(node, int):
        yield prefix, node
    elif isinstance(node, dict):
        for key in sorted(node):
            yield from _iter_int_leaves(node[key], prefix + (str(key),))
    elif isinstance(node, list):
        for item in node:
            yield from _iter_int_leaves(item, prefix + ("*",))


def _path_matches(path: tuple[str, ...], pattern: str) -> bool:
    """PREFIX match: the pattern's segments must match the path's first
    ``len(segments)`` segments.  That is what makes an exempt SUBTREE
    (``...converter_readouts.diagnostics[*]``) cover its arbitrarily deep
    ``context`` -- while every leaf-shaped pattern in the table
    (``side``, ``area_units2``, ...) matches only itself, because no such
    leaf is ever a prefix of another field's path."""
    segments: list[str] = []
    for segment in pattern.split("."):
        if segment.endswith("[*]"):       # "views[*]" == the literal "views"
            segments.append(segment[:-3])  # then one wildcard index segment
            segments.append("*")
        else:
            segments.append(segment)
    if len(path) < len(segments):
        return False
    return all(seg == "*" or seg == part
               for part, seg in zip(path, segments))


def scan_ingest_resolution_violations(payload: dict) -> list[str]:
    """⭐ A-11 exit scan: every integer of a serialised facts document
    (``as_measured`` OR ``as_signed`` -- same shape) must lie on the 1 mm
    ingest grid, unless its path is explicitly exempted in
    :data:`INGEST_NON_COORDINATE_PATHS`.

    Returns the offending paths (``path = value``); an empty list is
    compliance.  ⛔ This is an EXIT check, not an entry narrowing: it does
    not care HOW a value got into the document (builder, revision, hand
    edit), only whether what is there NOW is on the grid.
    """
    out: list[str] = []
    for path, value in _iter_int_leaves(payload):
        if any(_path_matches(path, pattern)
               for pattern in INGEST_NON_COORDINATE_PATHS):
            continue
        if value % INGEST_RESOLUTION_UNITS:
            out.append(".".join(path) + f" = {value}")
    return out


def _axis_aligned(affine: Affine2D, view_id: str) -> tuple[float, float, float, float]:
    """Return (sx, tx, sy, ty) for an axis-preserving source->world affine.

    ⛔ Loud, not assumed: a constant-coordinate face line only *exists* if the
    transform maps the x axis to the x axis.  Measured on sm25-L, both plan
    views are pure scale+translate (``m01 == m10 == 0.0``, ``m00 == m11 ==
    0.001``) -- but a request is free to declare a rotation, and this layer
    would silently mislabel every axis if it did.
    """
    if affine.m01 != 0.0 or affine.m10 != 0.0:
        raise AsMeasuredUnavailable(
            "non_axis_aligned_affine", view_id=view_id,
            detail=(f"world_from_source_m has m01={affine.m01} m10={affine.m10}; "
                    "a constant-coordinate face line is not well defined under "
                    "a rotating/shearing transform"))
    return affine.m00, affine.m02, affine.m11, affine.m12


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #
class AsMeasuredFaceLineV1(_StrictModel):
    """One drawn wall FACE line, exactly as the converter collected it.

    ``axis`` is the direction the line RUNS ALONG; the constant coordinate is on
    the other axis.  ⛔ There is no "wall side" here -- which of a wall's two
    faces this is, is stated by the wall that references it, not by the line.
    """
    id: DxfHandle                       # the DXF handle; unique within a view
    layer: HumanLabel
    axis: Literal["x", "y"]
    const: int                          # 0.1 mm, on the axis normal to ``axis``
    along_min: int                      # 0.1 mm
    along_max: int                      # 0.1 mm

    @model_validator(mode="after")
    def _ordered(self):
        if self.along_min >= self.along_max:
            raise ValueError("as_measured_face_line_degenerate")
        return self


class AsMeasuredWallV1(_StrictModel):
    """One wall = TWO PAIRED FACE LINES + the stretch over which both are drawn.

    ⭐ ``thickness`` is DERIVED (``face_hi - face_lo``) and stored anyway, so a
    consumer never has to re-derive it -- but it is stored as the INTEGER
    DIFFERENCE OF THE TWO STORED FACES, so "recompute it from the two faces and
    compare bit-for-bit" is a real check and not a re-run of a float formula.

    ⛔⛔ ``face_line_ids_lo`` / ``_hi`` MAY NOT BE EMPTY.  In ②-1a they could be,
    because ``walls`` came from the converter's jamb-cap ``wall_bands`` and a
    band's "second face" was often a coordinate with no stroke on it at all.
    That is exactly how 33 walls the drawing does not contain got into the
    record, so the impossibility is now STRUCTURAL rather than a test that some
    later edit could forget to run: a wall that cannot name the ink on both of
    its faces is not a wall this layer will construct.

    ``face_lo`` / ``face_hi`` are the D3 GROUP coordinate of each face -- the
    producer's own answer to "which strokes are one drawn face line", quantised
    at 1 mm by ``denominator.GROUP_QUANT``, then stored in the document's 0.1 mm
    unit.  ⛔ Not a new snap and ⛔ not a tolerance: the strokes' own exact
    0.1 mm coordinates are untouched on the ``face_lines`` this record names by
    handle, and where the two differ the group is listed in
    ``converter_readouts.face_groups_with_a_split_const`` (MEASURED: 2 groups on
    signed ``plan-F1``, 0 on the other three views) rather than absorbed
    silently ([[absence-conflates-causes-in-observables]]).
    """
    id: StableId
    axis: Literal["x", "y"]             # along-wall (running) axis
    face_lo: int                        # 0.1 mm, normal axis
    face_hi: int                        # 0.1 mm, normal axis
    thickness: int                      # 0.1 mm == face_hi - face_lo
    along_min: int
    along_max: int
    #: handles of the face lines lying on ``face_lo`` / ``face_hi``.  A face can
    #: be drawn as several collinear fragments, hence lists.  ⛔ NEVER empty.
    face_line_ids_lo: list[DxfHandle] = Field(min_length=1)
    face_line_ids_hi: list[DxfHandle] = Field(min_length=1)

    @model_validator(mode="after")
    def _thickness_is_the_difference(self):
        if self.thickness != self.face_hi - self.face_lo:
            raise ValueError("as_measured_thickness_not_face_difference")
        if self.thickness <= 0:
            raise ValueError("as_measured_thickness_not_positive")
        if self.along_min >= self.along_max:
            raise ValueError("as_measured_wall_along_interval_degenerate")
        return self


class AsMeasuredOpeningV1(_StrictModel):
    """One opening, with the wall it sits in named by reference."""
    id: DxfHandle
    block_name: HumanLabel
    kind: Literal["window", "door"]
    axis: Literal["x", "y"]             # along-wall axis
    along_min: int
    along_max: int
    cross_lo: int                       # normal-axis interval, 0.1 mm
    cross_hi: int
    #: The wall RUNS this opening is a gap in.  ⛔ PLURAL since ②-1a-R, and the
    #: plural is the honest shape, not a convenience: a wall is now a paired
    #: pair of drawn face-line RUNS, and D4 refuses to merge a run across an
    #: opening -- so an opening lies BETWEEN two runs of one wall rather than
    #: inside one.  MEASURED on all four views (signed/as-received x F1/F2):
    #: every single opening has EXACTLY 2 touching runs, 0 that it strictly
    #: overlaps, and those 2 always share ONE face pair -- i.e. the WALL is
    #: unambiguous and only "which of its two runs" is not, which is a question
    #: with no answer rather than one this layer may invent.
    #: ⛔ Naming one of the two would be [[observation-named-as-fact-travels-as-fact]]:
    #: a consumer would read "the opening is inside that run", which is false.
    #: ⛔ Empty is a stated fact, not an omission --
    #: ``converter_readouts.unresolved_opening_carriers`` says why.
    carrier_wall_ids: list[StableId] = Field(default_factory=list)
    jamb_handles: list[DxfHandle] = Field(default_factory=list)
    classification: Literal["exterior", "interior_excluded"] = "exterior"

    @model_validator(mode="after")
    def _ordered(self):
        if self.along_min > self.along_max or self.cross_lo > self.cross_hi:
            raise ValueError("as_measured_opening_interval_unordered")
        return self


class AsMeasuredRingV1(_StrictModel):
    """One ring of the measured footprint, in 0.1 mm integers."""
    polygon_index: StrictNonNegativeInt
    kind: Literal["exterior", "interior"]
    points: list[list[int]] = Field(min_length=4)

    @model_validator(mode="after")
    def _pairs(self):
        if any(len(p) != 2 for p in self.points):
            raise ValueError("as_measured_ring_point_not_a_pair")
        return self


class AsMeasuredFootprintV1(_StrictModel):
    geom_type: HumanLabel
    is_empty: bool
    rings: list[AsMeasuredRingV1] = Field(default_factory=list)


class BoundaryConditionEvidenceV1(_StrictModel):
    """Projection-free evidence for one cavity-side boundary decision.

    Coordinates use the facts document's 0.1 mm integer grid.  ``exit_point``
    is a classifier witness only: no output-profile support line or expanded
    corner is stored here.  Near/far handles make the two wall faces explicit
    instead of asking a consumer to recover them by list position.
    """

    method: Literal["facts_geometry_ray_exit_v1"] = "facts_geometry_ray_exit_v1"
    raw_face_const: int
    opposite_face_const: int
    thickness_units: StrictNonNegativeInt
    outward_normal: list[int] = Field(min_length=2, max_length=2)
    exit_point: list[int] = Field(min_length=2, max_length=2)
    footprint_ring_id: StableId
    footprint_edge_id: StableId | None = None
    footprint_edge_points: list[list[int]] | None = None
    adjacent_cavity_id: StableId | None = None
    cavity_side_face_line_ids: list[DxfHandle] = Field(min_length=1)
    far_side_face_line_ids: list[DxfHandle] = Field(min_length=1)

    @model_validator(mode="after")
    def _positive_thickness_and_point_pairs(self):
        if self.thickness_units <= 0:
            raise ValueError("as_measured_boundary_thickness_not_positive")
        if (self.footprint_edge_points is not None
                and (len(self.footprint_edge_points) != 2
                     or any(len(point) != 2 for point in self.footprint_edge_points))):
            raise ValueError("as_measured_boundary_footprint_edge_not_two_points")
        return self


class AsMeasuredBoundaryEdgeV1(_StrictModel):
    """One logical cavity-side edge before any profile chooses an offset."""

    id: StableId
    cavity_id: StableId
    sequence: StrictNonNegativeInt
    axis: Literal["x", "y"]
    cavity_const: int
    span_lo: int
    span_hi: int
    side: Literal[-1, 1]
    p1: list[int] = Field(min_length=2, max_length=2)
    p2: list[int] = Field(min_length=2, max_length=2)
    wall_ids: list[StableId] = Field(min_length=1)
    face_line_handles: list[DxfHandle] = Field(min_length=1)
    boundary_condition: Literal[
        "exterior", "interzone", "unclaimed_void", "unknown"]
    evidence: BoundaryConditionEvidenceV1

    @model_validator(mode="after")
    def _geometry_and_evidence_agree(self):
        if self.span_lo >= self.span_hi or self.p1 == self.p2:
            raise ValueError("as_measured_boundary_edge_degenerate")
        if any(len(point) != 2 for point in (self.p1, self.p2)):
            raise ValueError("as_measured_boundary_edge_point_not_a_pair")
        expected_normal = ([self.side * -1, 0] if self.axis == "y"
                           else [0, self.side * -1])
        if self.evidence.outward_normal != expected_normal:
            raise ValueError("as_measured_boundary_outward_normal_disagrees_with_side")
        return self


class AsMeasuredBoundaryFailureSpanV1(_StrictModel):
    """The exact ring segment that prevented a cavity from yielding edges."""

    axis: Literal["x", "y", "non_axis"]
    const: int | None = None
    lo: int | None = None
    hi: int | None = None
    side: Literal[-1, 1] | None = None
    p1: list[int] = Field(min_length=2, max_length=2)
    p2: list[int] = Field(min_length=2, max_length=2)
    nearest_same_axis_wall_face_const: int | None = None
    span_to_nearest_same_axis_wall_face_delta: int | None = None

    @model_validator(mode="after")
    def _axis_fields_are_explicit(self):
        scalars = (self.const, self.lo, self.hi, self.side)
        if self.axis == "non_axis":
            if any(value is not None for value in scalars):
                raise ValueError("as_measured_non_axis_failure_has_axis_fields")
        elif any(value is None for value in scalars):
            raise ValueError("as_measured_axis_failure_missing_axis_fields")
        elif self.lo >= self.hi:
            raise ValueError("as_measured_boundary_failure_span_degenerate")
        nearest = self.nearest_same_axis_wall_face_const
        delta = self.span_to_nearest_same_axis_wall_face_delta
        if (nearest is None) != (delta is None):
            raise ValueError("as_measured_boundary_failure_nearest_face_unpaired")
        if nearest is not None:
            if self.const is None or delta != self.const - nearest:
                raise ValueError("as_measured_boundary_failure_nearest_face_delta")
        return self


class AsMeasuredBoundaryRingLossV1(_StrictModel):
    """Stored readout for an above-threshold cavity that yielded no edges.

    The value is still a pure derivation of the other measured geometry.  It is
    persisted here for the narrower reason that the as-measured draft must
    acknowledge its own gaps: a missing logical ring must not disappear as an
    unobservable absence.  This is not a rule that derived values in general
    belong in the facts layer.
    """

    cavity_id: StableId
    area_units2: StrictNonNegativeInt
    span: AsMeasuredBoundaryFailureSpanV1
    reason: Literal[
        "non_axis_segment", "owner_count", "classify_illogical", "merged_lt_3",
        "endcap_const_not_a_measured_parallel_face",
        "adjacent_support_lines_parallel", "intersection_ring_invalid",
        "merged_span_has_no_supporting_witness"]
    owner_count: StrictNonNegativeInt | None = None

    @model_validator(mode="after")
    def _owner_count_matches_reason(self):
        if (self.reason == "owner_count") != (self.owner_count is not None):
            raise ValueError("as_measured_boundary_loss_owner_count_mismatch")
        if self.area_units2 <= 0:
            raise ValueError("as_measured_boundary_loss_area_not_positive")
        return self


class AsMeasuredNonOrthogonalLineV1(_StrictModel):
    """A collected stroke that is neither horizontal nor vertical.

    ⭐ It exists (measured: 1 in as-received ``plan-F1`` before 2026-09-07; 0
    after the ladder landed -- that stroke, 13AF, is now a real face line) and
    it cannot be a constant-coordinate face line.  ⛔ So it is NAMED here
    rather than dropped: "the record has no such wall" and "the record
    silently refused this wall" must not look the same
    ([[absence-conflates-causes-in-observables]]).

    ⭐⭐ ``p0``/``p1`` are RAW (``to_units``), ⛔ NOT on the 1 mm ingest grid --
    A-11-d3, fixed 2026-09-07.  Quantising them falsified the row: 13AF was
    stored with both endpoints at x=52400 while the drawing has 52400.742 and
    52398.827, i.e. the row asserted "not axis-aligned" and displayed a
    perfectly vertical line.  Same exemption and same wording as
    ``AsMeasuredAxisSnapV1.before_p0``/``before_p1``; the reason was already
    written down for that family and simply had not been applied to this one.
    """
    id: DxfHandle
    layer: HumanLabel
    #: ⛔ RAW 0.1 mm world, ⛔ NOT ingest-grid -- see the class docstring.
    p0: list[int] = Field(min_length=2, max_length=2)
    p1: list[int] = Field(min_length=2, max_length=2)


class AsMeasuredAxisSnapArbitrationV1(_StrictModel):
    """⭐⭐⭐ One stroke the ladder REFUSED TO DECIDE (tier 2) -- it is inside
    the 5° envelope, so it may well be a drafting slip, but flattening it is
    ⛔ not the machine's call.  This row is what a human arbitrates.

    ⭐ Why a separate list from ``non_orthogonal_lines`` and from
    ``s1_nonorthogonal_discarded_handles``: those two say a stroke did not
    become a face line.  ⛔ They do not say WHY, and "a genuine 45° diagonal"
    (tier 3) and "a 0.4° wall we could have straightened but must not" (tier 2)
    are completely different findings that were previously the same absence.

    ``reason`` is the ladder's own verdict word, verbatim from the converter:

      ``deviation_over_cap``          inside 5°, but flattening would move the
                                      face by more than half the thinnest
                                      declared wall ⇒ it could change WHICH
                                      wall this face belongs to
      ``both_ends_anchored_...``      BOTH ends sit on already-straight
                                      strokes ⇒ the drawing means this line to
                                      run between two settled points ⇒ suspect
                                      a REAL slant, ⛔ do not flatten
      ``anchor_undecidable_and_observable``
                                      neither end is anchored AND the three
                                      candidate answers do NOT collapse to one
                                      stored value ⇒ the choice is visible in
                                      the product, so a person makes it

    ⛔ ``deviation_units`` / ``deviation_limit_units`` / ``cap_units`` are
    LENGTH observations, not positions -- exempt from the ingest grid for the
    same reason ``minor_leg_units`` is (see ``_geom_units``).
    """
    id: DxfHandle
    layer: HumanLabel
    ladder_tier: int = Field(ge=2, le=2)
    reason: HumanLabel
    p0: list[int] = Field(min_length=2, max_length=2)   # RAW 0.1 mm world
    p1: list[int] = Field(min_length=2, max_length=2)
    deviation_units: StrictNonNegativeInt      # how far the flattening would move it
    deviation_limit_units: StrictNonNegativeInt   # this stroke's tier-1 ceiling
    cap_units: StrictNonNegativeInt            # half the thinnest declared wall
    angle_deg: StrictFiniteFloat = Field(ge=0.0, le=90.0)


class AsMeasuredAxisSnapV1(_StrictModel):
    """One S1 stroke ADMITTED by snapping, ⛔ not one that was already exact.

    ⭐⭐ dispatch ②-1b-S R1/R2 (thresholds SIGNED BY THE USER 2026-08-30,
    F-143/F-147): a stroke whose two legs BOTH exceed ``tau_axis`` (so it
    would, before ②-1b-S, have been dropped as ``tarch_wall_nonorthogonal``)
    but which passes BOTH signed admission gates -- SHORT leg within
    ``tarch_normalize.AXIS_SNAP_MAX_DEVIATION_M`` (10 mm) AND off-axis angle
    within ``tarch_normalize.AXIS_SNAP_MAX_ANGLE_DEG`` (1.0°) -- is now
    kept: the short leg is snapped to zero and the line becomes a real face
    line.  ⭐ GLM's exact demand (dispatch §二 R2): "被吸附过" and "本来就是正
    的" must never look the same on the record -- this is the record that
    keeps them apart.  A face line with a handle also listed here was NOT
    drawn exactly on axis; one that is not listed here either was, or was
    excluded/dropped for an unrelated reason.

    ``before_p0``/``before_p1`` are the RAW (un-snapped, un-quantized)
    endpoints; ``after_p0``/``after_p1`` are what the resulting face line's
    two endpoints became (one coordinate now shared -- the value this
    document's ``AsMeasuredFaceLineV1.const`` for this handle equals, up to
    quantization).  ``minor_leg_units`` is how much the short leg was worth
    (0.1 mm world) and ``angle_deg`` is the converter's independently
    transported off-axis angle.  Both signed admission readings therefore
    live on the itemised row the human reviews (F-148), not only in a separate
    diagnostics blob.
    """
    id: DxfHandle
    layer: HumanLabel
    snapped_axis: Literal["x", "y"]      # which coordinate was collapsed to one value
    before_p0: list[int] = Field(min_length=2, max_length=2)   # 0.1 mm world, PRE-snap
    before_p1: list[int] = Field(min_length=2, max_length=2)
    after_p0: list[int] = Field(min_length=2, max_length=2)    # 0.1 mm world, POST-snap
    after_p1: list[int] = Field(min_length=2, max_length=2)
    minor_leg_units: StrictNonNegativeInt   # 0.1 mm world -- the skew this line actually had
    angle_deg: StrictFiniteFloat = Field(ge=0.0, le=90.0)


class AsMeasuredConverterReadoutsV1(_StrictModel):
    """The converter's OWN verdicts, carried VERBATIM.

    ⛔ Not one geometric value in here is recomputed.  2026-08-29's lesson was
    that the converter had already produced these and the consumer threw them
    away; the fix is transport, ⛔ not a second opinion.
    """
    dangles: StrictNonNegativeInt
    cuts: StrictNonNegativeInt
    invalid: StrictNonNegativeInt
    #: ⭐ ②-1b-R (F-136/A3, GLM): the S1 zero-length discards, ITEMIZED by
    #: handle -- ⛔ not just this count.  Measured on signed ``plan-F1``:
    #: ``["13DC"]`` (``degenerate_line_count == 1``).  Verbatim from the
    #: ``tarch_wall_degenerate_line`` diagnostic, ⛔ never recomputed --
    #: ``degenerate_in_wall_lines`` below is recomputed FROM ``geo.wall_lines``,
    #: which structurally cannot contain a zero-length stroke (the converter
    #: already dropped it before appending), so that field is a real but
    #: always-empty check; THIS field and ``degenerate_line_count`` are the
    #: converter's own S1-stage readout and are what actually accounts for
    #: a handle leaving the record as "zero length".
    degenerate_line_count: StrictNonNegativeInt
    degenerate_line_handles: list[DxfHandle] = Field(default_factory=list)
    #: ⭐ ②-1b-R (F-136/A3, GLM): the S1 non-orthogonal discards, ITEMIZED --
    #: strokes whose BOTH legs exceed ``tau_axis`` and are therefore dropped
    #: before ever becoming a ``geo.wall_lines`` entry (⛔ a DIFFERENT
    #: population from ``non_orthogonal_lines`` below, which are strokes that
    #: DID reach ``geo.wall_lines`` and are skew there -- one is a continuous-
    #: quantity tolerance on the ORIGINAL drawing, the other is a discrete
    #: quantization-grid property; see the module docstring's "two rulers"
    #: note).  Measured on signed ``plan-F1``: ``["13AD", "13AE"]``.
    s1_nonorthogonal_discarded_handles: list[DxfHandle] = Field(default_factory=list)
    #: identity partners for the face-line ledger (see the document validator)
    wall_lines_total: StrictNonNegativeInt
    degenerate_in_wall_lines: StrictNonNegativeInt
    all_wall_handles: list[DxfHandle] = Field(default_factory=list)
    non_orthogonal_lines: list[AsMeasuredNonOrthogonalLineV1] = Field(default_factory=list)
    #: ⭐⭐ dispatch ②-1b-S R1/R2: strokes ADMITTED by the two USER-SIGNED
    #: snap gates (2026-08-30, F-143/F-147: deviation ≤ 10 mm AND angle ≤
    #: 1.0°) -- ⛔ a DIFFERENT population from
    #: ``s1_nonorthogonal_discarded_handles`` above (those are strokes that
    #: were STILL refused, unchanged) and from ``non_orthogonal_lines`` above
    #: (a different mechanism entirely -- post-quantization skew on a stroke
    #: that never failed the S1 "both legs > tau_axis" test at all).  See
    #: ``AsMeasuredAxisSnapV1``.
    axis_snapped_lines: list[AsMeasuredAxisSnapV1] = Field(default_factory=list)
    #: ⭐⭐⭐ G-c (2026-09-07): strokes the ladder sent to a HUMAN (tier 2) --
    #: inside the 5° envelope, so ⛔ not "a diagonal, out of scope", but the
    #: machine ⛔ may not straighten them by itself.  ⛔ A DIFFERENT population
    #: again from all three lists above.  MEASURED on both in-corpus cases:
    #: EMPTY -- every real skew stroke lands on tier 1 and is fixed without a
    #: signature, which is exactly the outcome the user asked for ("正交吸附和
    #: 按毫米分辨率吸附不用签字，直接修正就可以").  ⛔ An empty list is
    #: therefore NOT evidence the tier has no teeth; the synthetic fixtures in
    #: ``tests/test_tarch_converter_p1_geometry.py`` are, because the corpus
    #: contains ZERO real slants to exercise it with.
    axis_snap_arbitrations: list[AsMeasuredAxisSnapArbitrationV1] = Field(
        default_factory=list)
    unresolved_opening_carriers: list[JsonDict] = Field(default_factory=list)
    #: ⛔ ②-1a-R: the converter's ``wall_bands``, carried VERBATIM and under a
    #: name that says what they are grouped from -- JAMB CAPS (S2), ⛔ NOT wall
    #: faces.  ②-1a stored these as ``walls``; that is the defect this unit
    #: undoes.  They stay in the record because they ARE a converter readout,
    #: and throwing a readout on the floor is the failure F-A had just closed.
    jamb_cap_bands: list[JsonDict] = Field(default_factory=list)
    #: bands (⛔ not walls) one of whose two faces carries no collected stroke.
    jamb_cap_bands_missing_a_face_line: list[StableId] = Field(default_factory=list)
    #: ⭐ the consumption ledger for ``face_lines``: every collected face line
    #: sits in EXACTLY ONE of {referenced by a wall, excluded as a jamb cap,
    #: pairable but unpaired}, enforced by the view validator.  ⛔ Without it a
    #: stroke can leave the record by simply never being appended to a list --
    #: which is how 115 of signed ``plan-F1``'s 225 strokes would vanish.
    face_lines_excluded_as_jamb_caps: list[DxfHandle] = Field(default_factory=list)
    face_lines_not_paired_into_a_wall: list[DxfHandle] = Field(default_factory=list)
    #: D3 groups whose member strokes do not all sit ON the group coordinate
    #: (the 1 mm grouping absorbs at most 0.5 mm).  MEASURED: 2 on signed
    #: ``plan-F1``, 0 elsewhere.  ⛔ Named rather than absorbed silently.
    face_groups_with_a_split_const: list[JsonDict] = Field(default_factory=list)
    #: ⭐ the ONLY subtree in the whole document allowed to contain floats: these
    #: are the converter's own records in the converter's own frames.
    diagnostics: list[JsonDict] = Field(default_factory=list)
    gates: list[JsonDict] = Field(default_factory=list)

    @model_validator(mode="after")
    def _axis_snap_ledger_has_teeth(self):
        """⭐ dispatch ②-1b-S R3's "delete an item from the snap list -> the
        identity must go red" requirement, self-proving.  ``diagnostics``
        above is a VERBATIM, independently-populated carry of every converter
        diagnostic (``_readout_records``); ``axis_snapped_lines`` is built by
        a SEPARATE pass over that same population (``_axis_snap_records``).
        If the two ever disagree in COUNT, one of them lost or invented an
        entry relative to the converter's own record -- which is exactly what
        deleting one entry from ``axis_snapped_lines`` by hand does.
        """
        snap_diags = [d for d in self.diagnostics
                      if d.get("code") == "tarch_wall_axis_snapped"]
        diag_count = len(snap_diags)
        if len(self.axis_snapped_lines) != diag_count:
            raise ValueError(
                f"as_measured_axis_snapped_ledger_broken: "
                f"{len(self.axis_snapped_lines)} axis_snapped_lines != "
                f"{diag_count} tarch_wall_axis_snapped diagnostics")
        diag_angles = {
            str(handle): float(diagnostic.get("context", {}).get("angle_deg"))
            for diagnostic in snap_diags
            for handle in diagnostic.get("handles", [])}
        row_ids = [row.id for row in self.axis_snapped_lines]
        if len(row_ids) != len(set(row_ids)) or set(row_ids) != set(diag_angles):
            raise ValueError(
                "as_measured_axis_snapped_handles_disagree_with_diagnostics: "
                f"rows={sorted(row_ids)} diagnostics={sorted(diag_angles)}")
        for row in self.axis_snapped_lines:
            if row.angle_deg != diag_angles[row.id]:
                raise ValueError(
                    "as_measured_axis_snapped_angle_disagrees_with_diagnostic: "
                    f"{row.id} row={row.angle_deg} diagnostic={diag_angles[row.id]}")
        return self

    @model_validator(mode="after")
    def _arbitration_ledger_has_teeth(self):
        """⭐ The same self-proof, for the tier-2 list: deleting a row from
        ``axis_snap_arbitrations`` must go RED.

        ``diagnostics`` is a VERBATIM, independently-populated carry of the
        converter's own records; this list is built by a SEPARATE pass over
        that same population.  ⛔ Without this the arbitration list could be
        quietly emptied and the document would still validate -- which is the
        precise shape of "改判据顺手把红掉的锁拆了".
        """
        tier2 = [d for d in self.diagnostics
                 if d.get("code") == "tarch_wall_nonorthogonal"
                 and (d.get("context") or {}).get("ladder_tier") == 2]
        expected = {h for d in tier2 for h in d.get("handles", [])}
        rows = [row.id for row in self.axis_snap_arbitrations]
        if len(rows) != len(set(rows)) or set(rows) != expected:
            raise ValueError(
                "as_measured_axis_snap_arbitration_ledger_broken: "
                f"rows={sorted(rows)} tier2_diagnostics={sorted(expected)}")
        by_handle = {h: d for d in tier2 for h in d.get("handles", [])}
        for row in self.axis_snap_arbitrations:
            context = by_handle[row.id].get("context") or {}
            if row.angle_deg != float(context.get("angle_deg")):
                raise ValueError(
                    "as_measured_axis_snap_arbitration_angle_disagrees_with_diagnostic: "
                    f"{row.id} row={row.angle_deg} diagnostic={context.get('angle_deg')}")
            if [row.reason] != list(context.get("refused_by") or []):
                raise ValueError(
                    "as_measured_axis_snap_arbitration_reason_disagrees_with_diagnostic: "
                    f"{row.id} row={row.reason} diagnostic={context.get('refused_by')}")
        return self


class AsMeasuredViewV1(_StrictModel):
    view_id: StableId
    floor_id: StableId
    face_lines: list[AsMeasuredFaceLineV1] = Field(default_factory=list)
    walls: list[AsMeasuredWallV1] = Field(default_factory=list)
    openings: list[AsMeasuredOpeningV1] = Field(default_factory=list)
    footprint: AsMeasuredFootprintV1
    #: ②-1d: logical cavity-side edges, classified before profile projection.
    #: Empty remains schema-valid so an older/stripped facts document can be
    #: independently recomputed by AnswerCompiler (dispatch acceptance #5).
    boundary_edges: list[AsMeasuredBoundaryEdgeV1] = Field(default_factory=list)
    #: F-153: cavities above the request threshold that could not yield a
    #: logical ring.  This is generated in the same pass as ``boundary_edges``
    #: so a cavity is represented by exactly one of the two outcomes.
    boundary_ring_losses: list[AsMeasuredBoundaryRingLossV1] = Field(
        default_factory=list)
    converter_readouts: AsMeasuredConverterReadoutsV1

    @model_validator(mode="after")
    def _ledger_identity(self):
        """⛔ Every collected stroke is accounted for in exactly one bucket.

        This is the "consumption ledger" shape, not a range check: an
        unforeseen stroke stops being a silent omission and becomes a named
        red ([[declare-the-dialect-plus-consumption-ledger]]).
        """
        r = self.converter_readouts
        ids = [f.id for f in self.face_lines]
        if len(ids) != len(set(ids)):
            raise ValueError("as_measured_face_line_id_not_unique")
        known = set(ids)
        # ⭐ dispatch ②-1b-S R2, checked FIRST and deliberately ahead of the
        # wide S1 ledger below: a handle "被吸附过" must be provably a real
        # face line now (it made it INTO the answer, ⛔ not itemised and then
        # also dropped) and must never ALSO claim to be S1-discarded (admitted
        # and refused are mutually exclusive outcomes for the same stroke --
        # and double-booking one always ALSO breaks the wide identity below,
        # but this is the more specific diagnosis of WHY).
        snapped_ids = {s.id for s in r.axis_snapped_lines}
        dangling_snapped = sorted(snapped_ids - known)
        if dangling_snapped:
            raise ValueError(
                f"as_measured_axis_snapped_not_a_face_line:{dangling_snapped}")
        both_snapped_and_discarded = sorted(
            snapped_ids & set(r.s1_nonorthogonal_discarded_handles))
        if both_snapped_and_discarded:
            raise ValueError(
                f"as_measured_axis_snapped_also_discarded:{both_snapped_and_discarded}")
        total = (len(self.face_lines) + len(r.non_orthogonal_lines)
                 + r.degenerate_in_wall_lines)
        if total != r.wall_lines_total:
            raise ValueError(
                f"as_measured_wall_line_ledger_broken: "
                f"{r.wall_lines_total} collected != {len(self.face_lines)} face_lines "
                f"+ {len(r.non_orthogonal_lines)} non_orthogonal "
                f"+ {r.degenerate_in_wall_lines} degenerate")
        # ⭐⭐ ②-1b-R (F-136/A3, GLM): the identity above only accounts for what
        # made it INTO ``geo.wall_lines`` -- ⛔ it says nothing about a handle
        # that never got that far.  MEASURED on signed plan-F1: 226 collected
        # handles, only 223 in ``geo.wall_lines`` (3 left silently: 13AD/13AE
        # were S1 non-orthogonal discards, 13DC was an S1 zero-length
        # discard) -- the OLD identity was 222+1+0==223, green, while those 3
        # left the record with no bucket at all.  This is the WIDER identity,
        # over ``all_wall_handles`` -- the true universe of collected
        # handles -- rather than ``wall_lines_total``, an intermediate count:
        s1_total = (r.wall_lines_total + len(r.s1_nonorthogonal_discarded_handles)
                   + r.degenerate_line_count)
        if len(r.all_wall_handles) != s1_total:
            raise ValueError(
                f"as_measured_s1_handle_ledger_broken: "
                f"{len(r.all_wall_handles)} all_wall_handles != {r.wall_lines_total} wall_lines_total "
                f"+ {len(r.s1_nonorthogonal_discarded_handles)} s1_nonorthogonal_discarded "
                f"+ {r.degenerate_line_count} degenerate_line_count")
        if len(r.degenerate_line_handles) != r.degenerate_line_count:
            raise ValueError(
                f"as_measured_degenerate_line_handles_count_mismatch: "
                f"{len(r.degenerate_line_handles)} handles listed != "
                f"degenerate_line_count={r.degenerate_line_count}")
        for wall in self.walls:
            for ref in (*wall.face_line_ids_lo, *wall.face_line_ids_hi):
                if ref not in known:
                    raise ValueError(f"as_measured_dangling_face_line_ref:{ref}")
        wall_ids = {w.id for w in self.walls}
        if len(wall_ids) != len(self.walls):
            raise ValueError("as_measured_wall_id_not_unique")
        for opening in self.openings:
            for ref in opening.carrier_wall_ids:
                if ref not in wall_ids:
                    raise ValueError(f"as_measured_dangling_carrier_ref:{ref}")
        # ⭐⭐ ②-1a-R: the SECOND consumption ledger, and the one that keeps the
        # 225-vs-110 trap from ever being silent again.  A wall is built from
        # PAIRED face lines, so most collected strokes are deliberately NOT in a
        # wall -- and "deliberately excluded" must not look like "quietly lost".
        paired = {ref for wall in self.walls
                  for ref in (*wall.face_line_ids_lo, *wall.face_line_ids_hi)}
        capped = set(r.face_lines_excluded_as_jamb_caps)
        loose = set(r.face_lines_not_paired_into_a_wall)
        for name_a, a, name_b, b in (("wall", paired, "jamb_cap", capped),
                                     ("wall", paired, "unpaired", loose),
                                     ("jamb_cap", capped, "unpaired", loose)):
            both = sorted(a & b)
            if both:
                raise ValueError(
                    f"as_measured_face_line_in_two_buckets[{name_a}+{name_b}]:{both}")
        accounted = paired | capped | loose
        if accounted != known:
            raise ValueError(
                "as_measured_face_line_consumption_ledger_broken: "
                f"unaccounted={sorted(known - accounted)} "
                f"not_a_face_line={sorted(accounted - known)}")
        boundary_ids = [edge.id for edge in self.boundary_edges]
        if len(boundary_ids) != len(set(boundary_ids)):
            raise ValueError("as_measured_boundary_edge_id_not_unique")
        loss_cavity_ids = [loss.cavity_id for loss in self.boundary_ring_losses]
        if len(loss_cavity_ids) != len(set(loss_cavity_ids)):
            raise ValueError("as_measured_boundary_ring_loss_cavity_not_unique")
        both_edges_and_loss = sorted(
            {edge.cavity_id for edge in self.boundary_edges}
            & set(loss_cavity_ids))
        if both_edges_and_loss:
            raise ValueError(
                "as_measured_boundary_cavity_has_edges_and_loss:"
                f"{both_edges_and_loss}")
        for edge in self.boundary_edges:
            dangling_walls = sorted(set(edge.wall_ids) - wall_ids)
            dangling_faces = sorted(set(edge.face_line_handles) - known)
            if dangling_walls:
                raise ValueError(
                    f"as_measured_boundary_edge_dangling_walls:{edge.id}:{dangling_walls}")
            if dangling_faces:
                raise ValueError(
                    f"as_measured_boundary_edge_dangling_faces:{edge.id}:{dangling_faces}")
        return self


class AsMeasuredV1(_StrictModel):
    """The facts-layer document for one drawing.  ⛔ Never edited after writing.

    Trust root (ledger §七): the source DXF hash + the request hash + the
    converter IMPLEMENTATION fingerprint (sol's B1, filled in ②-1b R3 -- see
    ``converter_implementation_fingerprint``'s own docstring for exactly what
    kind of anchor this is and is not).
    """
    schema_version: Literal[1] = 1
    case: StableId
    #: ⭐ names the drawing this was measured off -- for sm25 that is the
    #: AS-RECEIVED file, not the signed one (ledger §十 / guide §十.3).
    source_dxf_label: HumanLabel
    source_dxf_sha256: Hex64
    request_sha256: Hex64
    coordinate_unit: Literal["0.1mm"] = "0.1mm"
    units_per_metre: Literal[10000] = 10000
    #: ⭐ ②-1b R3 (B1): the widened conversion-CLOSURE fingerprint (F-D, R4;
    #: see ``tarch_normalize.converter_sha256`` / ``CONVERTER_CLOSURE_FILES``).
    #:
    #: ⚠️ "谁签谁" (dispatch B1's own bar -- not "there is a hash", but "who
    #: signs what"), stated plainly rather than implied by the field existing:
    #:   INPUT   (source_dxf_sha256 / request_sha256, above) -- content-
    #:           addressed, exactly as they already were before this unit;
    #:           NOT yet human-signed for the as-received drawing (that
    #:           happens in the signing flow, ledger §五, which this dispatch
    #:           does not run -- R1's revisions stay ``unsigned``).
    #:   IMPL    this field -- ALSO content-addressed (13-file closure,
    #:           AST-normalized), ⛔ NOT human- or cryptographically signed:
    #:           this repo's commits carry no GPG signature (checked
    #:           2026-08-29: ``git config commit.gpgsign`` unset, no
    #:           signature on HEAD) and no existing signed carrier
    #:           (``HumanReviewAckV1``) covers an implementation fingerprint
    #:           at all -- inventing one here would mean re-signing
    #:           infrastructure this dispatch has no event to drive (no human
    #:           signs anything in this unit) and no license to add (⛔ "不
    #:           许改任何已签字件的哈希").  So "external anchor" here means
    #:           exactly what it already means for source_dxf_sha256 /
    #:           request_sha256: a value computed by a NAMED, AUDITABLE,
    #:           reproducible method (CONVERTER_CLOSURE_FILES has an out --
    #:           see its own docstring and
    #:           tests/test_tarch_converter_reproducibility.py's provenance
    #:           tests) rather than an opaque, unscoped self-hash.  Turning
    #:           this into a HUMAN-authorized value is future work with a
    #:           named hook, not a claim made here (ledger §七, §八).
    #:   FACTS   this document's own ``content_sha256`` (canonical_bytes/
    #:           content_sha256 below) covers every byte above, including
    #:           this field.
    converter_implementation_fingerprint: Hex64
    views: list[AsMeasuredViewV1] = Field(min_length=1)

    @model_validator(mode="after")
    def _views_unique_and_sorted(self):
        ids = [v.view_id for v in self.views]
        if ids != sorted(ids):
            raise ValueError("as_measured_views_not_sorted")
        if len(ids) != len(set(ids)):
            raise ValueError("as_measured_view_id_not_unique")
        return self


# --------------------------------------------------------------------------- #
# canonical bytes -- the object the reproducibility gate hashes
# --------------------------------------------------------------------------- #
def canonical_bytes(document: AsMeasuredV1) -> bytes:
    """⭐ ONE canonical serialisation, mirroring ``compute_request_sha256``.

    ``sort_keys`` removes any dependence on field insertion order; the lists
    below were already put in a total order by the builder, because a *sorted
    dump of an unsorted list* is still unsorted.
    """
    payload = document.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"


def content_sha256(document: AsMeasuredV1) -> str:
    return hashlib.sha256(canonical_bytes(document)).hexdigest()


# --------------------------------------------------------------------------- #
# R1 -- the as-received request is a PURE SOURCE SWAP of the signed one
# --------------------------------------------------------------------------- #
#: The only keys allowed to differ between ``request.json`` (signed, points at
#: the retouched drawing) and ``request_as_measured.json`` (points at the
#: as-received drawing).  ⭐ Each one differs *because the source file differs*:
#:   source_dxf_label / source_dxf_sha256  -- which file
#:   normalized_source_id                  -- the id of what normalising THAT file yields
#:   request_sha256                        -- the signature over the above
REQUEST_AS_MEASURED_ALLOWED_DELTA = ("source_dxf_label", "source_dxf_sha256",
                                     "normalized_source_id", "request_sha256")


#: Suffix appended to ``normalized_source_id``.  Normalising a DIFFERENT source
#: file yields a DIFFERENT normalised artefact, so it needs a different id;
#: ⛔ sharing the signed id would make two distinct artefacts collide.
AS_RECEIVED_ID_SUFFIX = ".as-received"


def request_file_bytes(raw: dict) -> bytes:
    """The repo's own request-file formatting, recovered rather than invented.

    ⭐ VERIFIED, not assumed: ``json.loads`` of the shipped signed
    ``request.json`` re-dumped through this function is byte-identical to the
    file on disk (25909 == 25909).  That is what lets the derived request be
    checked with ``==`` on bytes instead of "looks equivalent", and it is why
    key order is PRESERVED rather than sorted -- the derived file then diffs
    against the signed one in exactly the four places it is allowed to differ.
    """
    return (json.dumps(raw, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def derive_as_measured_request(signed_request: Path, as_received_dxf: Path) -> bytes:
    """⭐ The as-received request is DERIVED, ⛔ never authored.

    R1 asked for a request that lets the converter run on the as-received
    drawing ⛔ without changing a single byte of the signed one.  Two roads were
    open; this is road 甲 (a second file), with road 甲's own risk -- two request
    files drifting apart, which is F-130's shape -- closed structurally instead
    of by discipline:

      the second file is not maintained, it is COMPUTED from the first, and
      ``test_as_measured_facts_layer.py`` recomputes it and compares BYTES.

    ⛔ Road 乙 (one request declaring two sources) was rejected for a concrete
    reason, not a preference: every extra key lands in ``model_dump`` and
    therefore in ``compute_request_sha256``, so it would either break all three
    signed hashes or need yet another entry in the
    ``REQUEST_VERSIONS_WITHOUT_SPACE_BINDING``-style strip list -- i.e. a second
    field that the signatures deliberately do not cover, in the trust root, to
    solve a problem a separate file solves with no signature surface at all.
    """
    raw = json.loads(Path(signed_request).read_text(encoding="utf-8"))
    as_received_dxf = Path(as_received_dxf)
    raw["source_dxf_label"] = as_received_dxf.name
    raw["source_dxf_sha256"] = hashlib.sha256(as_received_dxf.read_bytes()).hexdigest()
    raw["normalized_source_id"] = raw["normalized_source_id"] + AS_RECEIVED_ID_SUFFIX
    raw["request_sha256"] = "0" * 64
    raw["request_sha256"] = compute_request_sha256(
        TarchConversionRequestV1.model_validate(raw))
    return request_file_bytes(raw)


def assert_request_is_pure_source_swap(signed_request: Path,
                                       as_measured_request: Path) -> dict[str, tuple[Any, Any]]:
    """⛔ Two request files must not be able to drift apart (F-130's shape).

    Adding a second request is the cheap way to run the converter on the
    as-received drawing WITHOUT touching a signed file's bytes.  Its cost is
    that the two can then be edited independently and quietly disagree about
    clip boxes, selectors, dialect rules or the affine -- at which point
    "measured off the original drawing" and "the answer" stop being comparable
    and nothing says so.

    ⭐ So the second file is not a second opinion: it is the first file with a
    NAMED, ENUMERATED delta, and this function is the gate that keeps it that
    way.  Returns ``{key: (signed_value, as_measured_value)}`` for the delta.
    """
    signed = json.loads(Path(signed_request).read_text(encoding="utf-8"))
    other = json.loads(Path(as_measured_request).read_text(encoding="utf-8"))
    if set(signed) != set(other):
        raise ValueError(
            "as_measured_request_key_set_differs: "
            f"only_signed={sorted(set(signed) - set(other))} "
            f"only_as_measured={sorted(set(other) - set(signed))}")
    delta: dict[str, tuple[Any, Any]] = {}
    disagreements = []
    for key in sorted(signed):
        if signed[key] == other[key]:
            continue
        if key in REQUEST_AS_MEASURED_ALLOWED_DELTA:
            delta[key] = (signed[key], other[key])
        else:
            disagreements.append(key)
    if disagreements:
        raise ValueError(f"as_measured_request_drifted_on:{sorted(disagreements)}")
    missing = [k for k in REQUEST_AS_MEASURED_ALLOWED_DELTA if k not in delta]
    if missing:
        raise ValueError(
            "as_measured_request_is_not_a_source_swap: these keys were expected "
            f"to differ and do not: {missing}")
    return delta


# --------------------------------------------------------------------------- #
# builder
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# ordering seams -- ⭐ the ONLY thing standing between this layer and the
# string-hash seed, so each one is a named, patchable object rather than an
# inline ``.sort(key=lambda ...)``.
#
# ⛔ Not a stylistic choice: a lock that says "reversing the input changed
# nothing" cannot tell a builder that SORTS from a builder that IGNORES its
# input.  Neutering a named key to a constant makes the sort a no-op (Python's
# sort is stable) and the same lock then goes red -- which is what proves the
# sort, and not luck, is holding the reproducibility gate up
# ([[gate-with-only-negative-assertions-is-unobservable]]).
# --------------------------------------------------------------------------- #
def _face_line_sort_key(face: "AsMeasuredFaceLineV1"):
    return (face.axis, face.const, face.along_min, face.along_max, face.id)


def _wall_sort_key(wall: "AsMeasuredWallV1"):
    """⚠️ ②-1a-R moved what this seam holds up, so it is stated rather than
    assumed ([[moving-a-gate-to-a-new-measurement-point]]).

    In ②-1a the walls came straight off ``geo.wall_bands`` in the upstream's
    iteration order, so THIS sort was what made a reversed input produce
    identical bytes.  It no longer is: walls are now built from
    ``face_line_targets``, which sorts its own groups, so reversing the input
    leaves the wall order alone even with this key neutered.  ⭐ What the key
    still uniquely holds up is the DOCUMENTED TOTAL ORDER of ``walls`` (the
    pairing emits them in the denominator's axis convention, i.e. the flipped
    one), and that is the direction its neuter test now measures.
    """
    return (wall.axis, wall.face_lo, wall.face_hi, wall.along_min, wall.along_max, wall.id)


def _band_sort_key(band):
    """Total order for the converter's own ``wall_bands`` readout.

    ⭐ A named, patchable seam like the other four: ``geo.wall_bands`` arrives
    as a list and the reproducibility probe reverses it, so without this sort
    the document's bytes would follow the upstream's iteration order.
    """
    return (band.band_id, band.axis, band.face_lo_mm, band.face_hi_mm)


def _opening_sort_key(opening: "AsMeasuredOpeningV1"):
    return (opening.axis, opening.cross_lo, opening.cross_hi,
            opening.along_min, opening.along_max, opening.id)


def _sorted_handles(values) -> list[str]:
    """Total order for every handle collection.

    ⭐ This is the one that the seed actually attacks: ``all_wall_handles`` is a
    ``set[str]``, and ``str`` is the type whose hash Python randomises per
    process.  ⛔ Iterating it directly would make the document's bytes a
    function of ``PYTHONHASHSEED``.
    """
    return sorted(values)


def _face_line_records(geo: P1PlanViewGeometry, sx: float, tx: float,
                       sy: float, ty: float) -> tuple[list[AsMeasuredFaceLineV1],
                                                      list[AsMeasuredNonOrthogonalLineV1],
                                                      int]:
    faces: list[AsMeasuredFaceLineV1] = []
    skew: list[AsMeasuredNonOrthogonalLineV1] = []
    degenerate = 0
    for handle, x0, y0, x1, y1 in geo.wall_lines:
        layer = geo.wall_line_layers.get(handle, "")
        wx0, wx1 = _geom_units(sx * x0 + tx), _geom_units(sx * x1 + tx)
        wy0, wy1 = _geom_units(sy * y0 + ty), _geom_units(sy * y1 + ty)
        if x0 != x1 and y0 != y1:
            # ⛔ RAW (``to_units`` only), ⛔ NOT ``_geom_units``: A-11-d3.  This
            # row's ONLY claim is "neither horizontal nor vertical", and the
            # 1 mm ingest grid was collapsing exactly the coordinate pair that
            # carries it -- 13AF stored both x as 52400 while the drawing has
            # 52400.742 / 52398.827.  Same exemption, same reason, same wording
            # as ``axis_snapped_lines[*].before_*``; see ``_geom_units``.
            skew.append(AsMeasuredNonOrthogonalLineV1(
                id=handle, layer=layer,
                p0=[to_units(sx * x0 + tx), to_units(sy * y0 + ty)],
                p1=[to_units(sx * x1 + tx), to_units(sy * y1 + ty)]))
            continue
        if x0 == x1 and y0 == y1:
            degenerate += 1
            continue
        if x0 == x1:                       # runs along y, constant x
            faces.append(AsMeasuredFaceLineV1(
                id=handle, layer=layer, axis="y", const=wx0,
                along_min=min(wy0, wy1), along_max=max(wy0, wy1)))
        else:                              # runs along x, constant y
            faces.append(AsMeasuredFaceLineV1(
                id=handle, layer=layer, axis="x", const=wy0,
                along_min=min(wx0, wx1), along_max=max(wx0, wx1)))
    faces.sort(key=_face_line_sort_key)
    skew.sort(key=lambda s: (s.p0, s.p1, s.id))
    return faces, skew, degenerate


def _jamb_cap_band_records(geo: P1PlanViewGeometry, faces: list[AsMeasuredFaceLineV1],
                           sx: float, tx: float, sy: float,
                           ty: float) -> tuple[list[dict], list[str]]:
    """The converter's ``wall_bands``, VERBATIM -- ⛔ under a truthful name.

    ⛔ These are NOT walls, and ②-1a's mistake was to store them as such.  The
    converter groups them from JAMB CAPS: a stroke counts as a cap when its
    LENGTH lands inside ``wall_thickness_range_m`` = [0.06, 0.50] m, so a door
    frame and a real partition face 0.30 m away become one "300 mm wall".

    ⭐ They are still carried, in the converter's own native millimetres (this
    subtree is the document's only float-bearing one), because they are a real
    readout of a real producer -- what changes is the NAME and the fact that
    nothing downstream may mistake them for the wall list.

    The second return value keeps ②-1a's measurement alive: bands one of whose
    two faces has no collected stroke at all (MEASURED 9 / 7 / 11 / 7 across the
    four views) -- the evidence that band grouping is not face pairing.
    """
    by_const: dict[tuple[str, int], list[str]] = {}
    for face in faces:
        by_const.setdefault((face.axis, face.const), []).append(face.id)
    records: list[dict] = []
    missing: list[str] = []
    for band in sorted(geo.wall_bands, key=_band_sort_key):
        if band.axis == "x":               # runs along x, faces are y coords
            lo = _geom_units(sy * band.face_lo_mm + ty)
            hi = _geom_units(sy * band.face_hi_mm + ty)
        else:                              # runs along y, faces are x coords
            lo = _geom_units(sx * band.face_lo_mm + tx)
            hi = _geom_units(sx * band.face_hi_mm + tx)
        lo, hi = (lo, hi) if lo <= hi else (hi, lo)
        if not by_const.get((band.axis, lo)) or not by_const.get((band.axis, hi)):
            missing.append(band.band_id)
        records.append({
            "band_id": band.band_id, "axis": band.axis,
            "face_lo_mm": float(band.face_lo_mm), "face_hi_mm": float(band.face_hi_mm),
            "along_min_mm": float(band.along_min_mm),
            "along_max_mm": float(band.along_max_mm),
            "thickness_mm": float(band.thickness_mm),
            "cap_handles": _sorted_handles(set(band.cap_handles))})
    return records, _sorted_handles(set(missing))


def _pair_face_lines_into_walls(
        targets: list[dict], known: set[str]) -> tuple[list[AsMeasuredWallV1], list[str]]:
    """⭐ Two face lines make a wall: same axis, overlapping, NEAREST opposite.

    ⛔ There is NO thickness threshold in this function, by design.  "Keep the
    pairs whose gap looks like a declared wall" is the rule that silently
    deleted sm24's whole 120 mm partition family; the structural substitute is
    that each face may be consumed once, by its nearest overlapping opposite.

    ⚠️⚠️ ``targets`` speak the DENOMINATOR's axis convention -- ``axis`` names
    the axis the CONSTANT coordinate sits on -- while every ``axis`` in this
    module names the axis a line RUNS ALONG.  The flip happens HERE, once, and
    nowhere else ([[cross-representation-mutation-must-be-equivalent]]).

    ``known`` filters the references down to strokes this layer actually stored
    as ``face_lines``; a collected stroke that is degenerate never becomes one,
    and a reference to it would dangle.
    """
    used: set[int] = set()
    walls: list[AsMeasuredWallV1] = []
    for i, a in enumerate(targets):
        if i in used:
            continue
        best: tuple[float, int, dict] | None = None
        for j in range(i + 1, len(targets)):
            if j in used or targets[j]["axis"] != a["axis"]:
                continue
            b = targets[j]
            overlap = min(a["hi_m"], b["hi_m"]) - max(a["lo_m"], b["lo_m"])
            gap = abs(a["const_m"] - b["const_m"])
            if overlap <= 0 or gap < 1e-9:
                continue
            if best is None or gap < best[0]:
                best = (gap, j, b)
        if best is None:
            continue
        _gap, j, b = best
        used |= {i, j}
        lo_t, hi_t = (a, b) if a["const_m"] <= b["const_m"] else (b, a)
        face_lo, face_hi = _geom_units(lo_t["const_m"]), _geom_units(hi_t["const_m"])
        along_min = _geom_units(max(a["lo_m"], b["lo_m"]))
        along_max = _geom_units(min(a["hi_m"], b["hi_m"]))
        run_axis = "y" if a["axis"] == "x" else "x"          # ⚠️ the one flip
        walls.append(AsMeasuredWallV1(
            id=f"w_{run_axis}_{face_lo}_{face_hi}_{along_min}_{along_max}",
            axis=run_axis, face_lo=face_lo, face_hi=face_hi,
            thickness=face_hi - face_lo,
            along_min=along_min, along_max=along_max,
            face_line_ids_lo=sorted(h for h in lo_t["handles"] if h in known),
            face_line_ids_hi=sorted(h for h in hi_t["handles"] if h in known)))
    unpaired = _sorted_handles({h for i, tgt in enumerate(targets) if i not in used
                                for h in tgt["handles"] if h in known})
    walls.sort(key=_wall_sort_key)
    return walls, unpaired


def _split_const_groups(targets: list[dict],
                        by_id: dict[str, AsMeasuredFaceLineV1]) -> list[dict]:
    """D3 groups whose members do not all sit on the group coordinate.

    ⛔ Not a defect report and ⛔ not a filter: the 1 mm grouping is the
    producer's own answer to "which strokes are one face line", and a wall's
    ``face_lo``/``face_hi`` are that group coordinate.  This names the (small,
    MEASURED: 2 on signed ``plan-F1``) set where the group coordinate and a
    member stroke's own coordinate differ, so the 0.1 mm is visible in the
    record instead of being absorbed by a rounding nobody can see.
    """
    out: list[dict] = []
    for tgt in targets:
        group_const = _geom_units(tgt["const_m"])
        members = sorted({by_id[h].const for h in tgt["handles"] if h in by_id})
        if any(const != group_const for const in members):
            out.append({"axis": "y" if tgt["axis"] == "x" else "x",
                        "group_const": group_const, "member_consts": members,
                        "handles": _sorted_handles(h for h in tgt["handles"]
                                                   if h in by_id)})
    out.sort(key=lambda g: (g["axis"], g["group_const"], g["handles"]))
    return out


# --------------------------------------------------------------------------- #
# ②-1d boundary-condition facts -- deliberately independent of AnswerCompiler
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _BoundaryWallGroup:
    axis: Literal["x", "y"]
    face_lo: int
    face_hi: int
    runs: tuple[AsMeasuredWallV1, ...]
    openings: tuple[AsMeasuredOpeningV1, ...]

    @property
    def key(self) -> tuple[str, int, int]:
        return self.axis, self.face_lo, self.face_hi

    @property
    def wall_ids(self) -> list[str]:
        return sorted(wall.id for wall in self.runs)

    @property
    def face_line_handles(self) -> list[str]:
        return sorted({handle for wall in self.runs
                       for handle in (*wall.face_line_ids_lo,
                                      *wall.face_line_ids_hi)})

    def coverage(self) -> list[tuple[int, int]]:
        return ([(wall.along_min, wall.along_max) for wall in self.runs]
                + [(opening.along_min, opening.along_max)
                   for opening in self.openings])

    def handles(self, side: Literal["lo", "hi"]) -> list[str]:
        return sorted({handle for wall in self.runs
                       for handle in (wall.face_line_ids_lo if side == "lo"
                                      else wall.face_line_ids_hi)})


#: Placeholder condition carried by an end-cap span.  An end-cap never becomes
#: an edge record, so it never needs -- and must never invent -- one of the four
#: real ``boundary_condition`` values.
_ENDCAP_NON_CONDITION = "not_an_edge"


@dataclass
class _BoundarySpan:
    axis: Literal["x", "y"]
    cavity_const: int
    lo: int
    hi: int
    side: Literal[-1, 1]
    p1: tuple[int, int]
    p2: tuple[int, int]
    group: _BoundaryWallGroup
    boundary_condition: str
    kind: Literal["faced", "endcap"] = "faced"
    #: True when this span is a cell produced by partitioning an illogical
    #: parent at measured geometry transitions.  Such a cell owns a witness
    #: that is valid only inside itself, so re-merging it would hand the
    #: merged record a NEW single witness -- the exact thing the partition
    #: exists to prevent.  ⇒ a subdivided cell never merges.
    subdivided: bool = False


@dataclass
class _BoundaryDerivation:
    edges: list[AsMeasuredBoundaryEdgeV1]
    losses: list[AsMeasuredBoundaryRingLossV1]


def _boundary_opaque_id(prefix: str, *parts: object) -> str:
    payload = "|".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(payload).hexdigest()[:16]}"


def _boundary_wall_groups(view: AsMeasuredViewV1) -> dict[
        tuple[str, int, int], _BoundaryWallGroup]:
    grouped: dict[tuple[str, int, int], list[AsMeasuredWallV1]] = {}
    for wall in view.walls:
        grouped.setdefault((wall.axis, wall.face_lo, wall.face_hi), []).append(wall)
    by_wall_id = {wall.id: wall for wall in view.walls}
    opening_groups: dict[tuple[str, int, int], list[AsMeasuredOpeningV1]] = {}
    for opening in view.openings:
        keys = {
            (by_wall_id[wall_id].axis, by_wall_id[wall_id].face_lo,
             by_wall_id[wall_id].face_hi)
            for wall_id in opening.carrier_wall_ids if wall_id in by_wall_id}
        geometric_key = (opening.axis, opening.cross_lo, opening.cross_hi)
        if geometric_key in grouped:
            keys.add(geometric_key)
        for key in keys:
            opening_groups.setdefault(key, []).append(opening)
    result = {}
    for key, runs in grouped.items():
        result[key] = _BoundaryWallGroup(
            axis=key[0], face_lo=key[1], face_hi=key[2],
            runs=tuple(sorted(
                runs, key=lambda wall: (wall.along_min, wall.along_max, wall.id))),
            openings=tuple(sorted(
                {opening.id: opening
                 for opening in opening_groups.get(key, [])}.values(),
                key=lambda opening: (
                    opening.along_min, opening.along_max, opening.id))))
    return result


def _boundary_band_rectangle(axis: str, face_lo: int, face_hi: int,
                             along_lo: int, along_hi: int) -> Polygon:
    if axis == "x":
        return Polygon([(along_lo, face_lo), (along_hi, face_lo),
                        (along_hi, face_hi), (along_lo, face_hi)])
    return Polygon([(face_lo, along_lo), (face_lo, along_hi),
                    (face_hi, along_hi), (face_hi, along_lo)])


def _boundary_wall_region(view: AsMeasuredViewV1) -> Any:
    rectangles = [_boundary_band_rectangle(
        wall.axis, wall.face_lo, wall.face_hi, wall.along_min, wall.along_max)
        for wall in view.walls]
    rectangles.extend(_boundary_band_rectangle(
        opening.axis, opening.cross_lo, opening.cross_hi,
        opening.along_min, opening.along_max) for opening in view.openings)
    return unary_union(rectangles) if rectangles else Polygon()


def _boundary_footprint(view: AsMeasuredViewV1
                        ) -> tuple[Polygon, list[tuple[str, list[list[int]]]]]:
    exterior = [ring for ring in view.footprint.rings if ring.kind == "exterior"]
    if len(exterior) != 1:
        return Polygon(), []
    ext = exterior[0]
    holes = [ring.points for ring in view.footprint.rings
             if ring.kind == "interior" and ring.polygon_index == ext.polygon_index]
    polygon = Polygon(ext.points, holes=holes)
    if polygon.is_empty or not polygon.is_valid:
        return Polygon(), []
    ring_id = f"footprint:{view.view_id}:ring:{ext.polygon_index}"
    return polygon, [(ring_id, ext.points)]


def _boundary_cavity_id(view_id: str, cavity: Polygon) -> str:
    bounds = tuple(round(value, 6) for value in cavity.bounds)
    return _boundary_opaque_id("cavity", view_id, *bounds, round(cavity.area, 3))


def _boundary_owners(groups: dict[tuple[str, int, int], _BoundaryWallGroup],
                     axis: str, const: int, lo: int,
                     hi: int) -> list[_BoundaryWallGroup]:
    found = []
    for group in groups.values():
        if group.axis != axis or const not in (group.face_lo, group.face_hi):
            continue
        if any(min(hi, end) - max(lo, start) > 0
               for start, end in group.coverage()):
            found.append(group)
    return sorted(found, key=lambda group: (group.key, group.wall_ids))


def _boundary_endcap_groups(
        groups: dict[tuple[str, int, int], _BoundaryWallGroup],
        axis: str, const: int, lo: int, hi: int) -> list[_BoundaryWallGroup]:
    """Bands carried on their *run end line* rather than on one of their faces.

    A cavity ring segment that crosses a perpendicular band's full thickness at
    that band's ``along_min``/``along_max`` is the band's END CAP.  Both of the
    band's face lines bound it, so it is measured geometry -- but it is not a
    wall FACE, which is why ``_boundary_owners`` correctly reports nobody for
    it.  Naming it here is what lets an end-capped cavity keep its ring instead
    of being written off wholesale as ``owner_count=0``.
    """
    found = []
    for group in groups.values():
        if group.axis == axis:
            continue
        if min(hi, group.face_hi) - max(lo, group.face_lo) <= 0:
            continue
        if any(const in (wall.along_min, wall.along_max) for wall in group.runs):
            found.append(group)
    return sorted(found, key=lambda group: (group.key, group.wall_ids))


def _boundary_parallel_measured_faces(
        groups: dict[tuple[str, int, int], _BoundaryWallGroup],
        axis: str, const: int) -> list[tuple[str, int, int]]:
    """Bands PARALLEL to this segment that measured a face at exactly ``const``.

    Admissibility rule for an end cap: the wall really has to stop *on* another
    wall's measured face.  ⛔ No tolerance and no snapping -- ``const`` either is
    one of the measured face constants or it is not.  A wall whose end lands one
    integer unit away from the face it appears to touch has not been measured as
    touching it, and the cavity it half-bounds is not a room this layer can
    honestly close.
    """
    return sorted(group.key for group in groups.values()
                  if group.axis == axis and const in (group.face_lo, group.face_hi))


def _boundary_nearest_same_axis_face(
        groups: dict[tuple[str, int, int], _BoundaryWallGroup], axis: str,
        const: int, lo: int, hi: int) -> int | None:
    """Report the nearest overlapping same-axis wall face without matching it.

    The result is evidence only.  It never changes owner lookup: a face one
    integer unit away remains a different face just as a face 1000 units away
    does.  Requiring exact along-span overlap keeps the clue tied to this ring
    segment rather than to an unrelated parallel wall elsewhere in the view.
    """
    candidates: list[tuple[int, int, tuple[str, ...]]] = []
    for group in groups.values():
        if group.axis != axis:
            continue
        if not any(min(hi, end) - max(lo, start) > 0
                   for start, end in group.coverage()):
            continue
        for face_const in (group.face_lo, group.face_hi):
            candidates.append((
                abs(const - face_const), face_const, tuple(group.wall_ids)))
    if not candidates:
        return None
    return min(candidates)[1]


def _boundary_exit_const(span: _BoundarySpan, raw_far: int) -> int:
    outward = -span.side
    stored_far = span.group.face_hi if outward > 0 else span.group.face_lo
    farthest = max(raw_far, stored_far) if outward > 0 else min(raw_far, stored_far)
    return farthest + outward


def _boundary_transition_points(
        span: _BoundarySpan, raw_far: int, footprint: Polygon,
        wall_region: Any, cavities: list[Polygon]) -> list[int]:
    """Partition a ray-exit span at every geometry membership transition.

    The classifier may still use a witness point *inside* each resulting cell,
    but no chosen point decides which side of a wall/cavity/footprint boundary
    the whole unsplit span represents.  All cut coordinates come from the
    measured geometries themselves; no sampling interval or tolerance exists.
    """
    exit_const = _boundary_exit_const(span, raw_far)
    if span.axis == "y":
        probe = LineString([(exit_const, span.lo), (exit_const, span.hi)])
        along_index = 1
    else:
        probe = LineString([(span.lo, exit_const), (span.hi, exit_const)])
        along_index = 0
    cuts = {span.lo, span.hi}

    def collect(geometry: Any) -> None:
        intersection = probe.intersection(geometry)

        def visit(part: Any) -> None:
            if part.is_empty:
                return
            if hasattr(part, "geoms"):
                for child in part.geoms:
                    visit(child)
                return
            if hasattr(part, "coords"):
                for point in part.coords:
                    value = int(round(point[along_index]))
                    if span.lo < value < span.hi:
                        cuts.add(value)

        visit(intersection)

    for geometry in (footprint, wall_region, *cavities):
        collect(geometry)
    return sorted(cuts)


def _boundary_subspan(span: _BoundarySpan, lo: int, hi: int) -> _BoundarySpan:
    if span.axis == "y":
        low_point, high_point = ((span.cavity_const, lo),
                                 (span.cavity_const, hi))
        forward = span.p1[1] < span.p2[1]
    else:
        low_point, high_point = ((lo, span.cavity_const),
                                 (hi, span.cavity_const))
        forward = span.p1[0] < span.p2[0]
    p1, p2 = ((low_point, high_point) if forward
              else (high_point, low_point))
    return _BoundarySpan(
        axis=span.axis, cavity_const=span.cavity_const, lo=lo, hi=hi,
        side=span.side, p1=p1, p2=p2, group=span.group,
        boundary_condition=span.boundary_condition, kind=span.kind)


def _boundary_failure_span(
        *, p1: tuple[int, int], p2: tuple[int, int],
        axis: Literal["x", "y"] | None = None, const: int | None = None,
        lo: int | None = None, hi: int | None = None,
        side: Literal[-1, 1] | None = None,
        nearest_same_axis_wall_face_const: int | None = None,
        ) -> AsMeasuredBoundaryFailureSpanV1:
    delta = (None if const is None or nearest_same_axis_wall_face_const is None
             else const - nearest_same_axis_wall_face_const)
    return AsMeasuredBoundaryFailureSpanV1(
        axis=axis or "non_axis", const=const, lo=lo, hi=hi, side=side,
        p1=list(p1), p2=list(p2),
        nearest_same_axis_wall_face_const=(
            nearest_same_axis_wall_face_const),
        span_to_nearest_same_axis_wall_face_delta=delta)


def _boundary_ray_witness(
        axis: str, start_const: int, exit_const: int, mid_along: int,
        ring_records: list[tuple[str, list[list[int]]]],
        ) -> tuple[str, str, list[list[int]]] | None:
    low, high = sorted((start_const, exit_const))
    for ring_id, points in ring_records:
        ring = points[:-1] if len(points) > 1 and points[0] == points[-1] else points
        for index, (a, b) in enumerate(zip(ring, ring[1:] + ring[:1])):
            if axis == "y" and a[0] == b[0]:
                const, along_lo, along_hi = a[0], min(a[1], b[1]), max(a[1], b[1])
            elif axis == "x" and a[1] == b[1]:
                const, along_lo, along_hi = a[1], min(a[0], b[0]), max(a[0], b[0])
            else:
                continue
            if low <= const <= high and along_lo <= mid_along <= along_hi:
                return ring_id, f"{ring_id}:edge:{index}", [list(a), list(b)]
    return None


def _classify_boundary_fact(
        span: _BoundarySpan, raw_near: int, raw_far: int,
        footprint: Polygon, ring_records: list[tuple[str, list[list[int]]]],
        wall_region: Any, cavities: list[Polygon],
        cavity_ids: dict[int, str],
        ) -> tuple[str, BoundaryConditionEvidenceV1, bool]:
    """Facts-side classifier, independent of ``answer_compiler._classify_boundary``.

    The boolean says whether the span is a logical pre-projection edge.  A ray
    still inside wall material is a junction fragment, not a boundary record;
    an outside ray without an axis-aligned footprint witness is a genuine
    ``unknown`` record and remains representable (R3's diagonal-façade supply).
    """
    outward = -span.side
    mid_along = (span.lo + span.hi) // 2
    exit_const = _boundary_exit_const(span, raw_far)
    exit_point = ([exit_const, mid_along] if span.axis == "y"
                  else [mid_along, exit_const])
    point = Point(exit_point)
    outside = not footprint.covers(point) and not wall_region.covers(point)
    footprint_ring_id = ring_records[0][0]
    footprint_edge_id = None
    footprint_edge_points = None
    adjacent = None
    logical_edge = True
    if outside:
        witness = _boundary_ray_witness(
            span.axis, raw_near, exit_const, mid_along, ring_records)
        if witness is None:
            condition = "unknown"
        else:
            footprint_ring_id, footprint_edge_id, footprint_edge_points = witness
            condition = "exterior"
    else:
        adjacent_cavities = [cavity for cavity in cavities if cavity.covers(point)]
        if len(adjacent_cavities) == 1:
            condition = "interzone"
            adjacent = cavity_ids[id(adjacent_cavities[0])]
        elif footprint.covers(point) and not wall_region.covers(point):
            condition = "unclaimed_void"
        else:
            condition = "unknown"
            logical_edge = False

    near_side: Literal["lo", "hi"] = "lo" if span.side < 0 else "hi"
    far_side: Literal["lo", "hi"] = "hi" if near_side == "lo" else "lo"
    near_handles = span.group.handles(near_side)
    far_handles = span.group.handles(far_side)
    evidence = BoundaryConditionEvidenceV1(
        raw_face_const=raw_near,
        opposite_face_const=raw_far,
        thickness_units=abs(raw_far - raw_near),
        outward_normal=([outward, 0] if span.axis == "y" else [0, outward]),
        exit_point=exit_point,
        footprint_ring_id=footprint_ring_id,
        footprint_edge_id=footprint_edge_id,
        footprint_edge_points=footprint_edge_points,
        adjacent_cavity_id=adjacent,
        cavity_side_face_line_ids=near_handles,
        far_side_face_line_ids=far_handles)
    return condition, evidence, logical_edge


def _boundary_spans_mergeable(left: _BoundarySpan,
                              right: _BoundarySpan) -> bool:
    if left.subdivided or right.subdivided:
        return False
    return (left.axis == right.axis
            and left.cavity_const == right.cavity_const
            and left.side == right.side
            and left.kind == right.kind
            and left.group.key == right.group.key
            and left.boundary_condition == right.boundary_condition
            and left.p2 == right.p1)


def _merge_boundary_spans(spans: list[_BoundarySpan]) -> list[_BoundarySpan]:
    if not spans:
        return []
    start = 0
    for index, span in enumerate(spans):
        if not _boundary_spans_mergeable(spans[index - 1], span):
            start = index
            break
    rotated = spans[start:] + spans[:start]
    merged: list[_BoundarySpan] = []
    for span in rotated:
        if merged and _boundary_spans_mergeable(merged[-1], span):
            previous = merged[-1]
            merged[-1] = _BoundarySpan(
                axis=previous.axis, cavity_const=previous.cavity_const,
                lo=min(previous.lo, span.lo), hi=max(previous.hi, span.hi),
                side=previous.side, p1=previous.p1, p2=span.p2,
                group=previous.group,
                boundary_condition=previous.boundary_condition,
                kind=previous.kind)
        else:
            merged.append(span)
    return merged


def _boundary_support_lines(
        merged: list[_BoundarySpan]) -> list[list[_BoundarySpan]]:
    """Group the edge-bearing spans into the SUPPORT LINES the ring turns on.

    One support line legitimately carries more than one edge record: the
    classifier refuses to judge a junction fragment (a stretch whose exit ray
    is still inside wall material) and the record is cut there rather than
    claiming a condition it could not see.  Such a cut does not turn the ring,
    so it must never become a corner.

    End caps carry no record at all and are dropped here.  What closes the ring
    across an end cap is that the two faced lines it separates meet at their own
    intersection -- which is exactly why an end cap has to be recognised (so the
    cavity is not written off) without ever becoming an edge.
    """
    faced = [span for span in merged if span.kind == "faced"]
    if not faced:
        return []
    start = 0
    for index, span in enumerate(faced):
        previous = faced[index - 1]
        if (previous.axis, previous.cavity_const) != (span.axis,
                                                      span.cavity_const):
            start = index
            break
    rotated = faced[start:] + faced[:start]
    lines: list[list[_BoundarySpan]] = []
    for span in rotated:
        if lines and (lines[-1][0].axis, lines[-1][0].cavity_const) == (
                span.axis, span.cavity_const):
            lines[-1].append(span)
        else:
            lines.append([span])
    return lines


def _boundary_ring_corners(
        lines: list[list[_BoundarySpan]]
        ) -> tuple[list[tuple[int, int]], int | None]:
    """Corner ``i`` = the one intersection of support lines ``i-1`` and ``i``.

    This mirrors the answer side's own junction rule (``_support_vertices`` in
    the answer compiler): two adjacent support lines meet in exactly one point
    when -- and only when -- they run on different axes.  Chaining the measured
    span END POINTS instead is what manufactured self-crossing rings: a span's
    end point is where THIS wall's face stops being measurable, which is not
    where the ring turns.  Returns ``(corners, bad_index)``, where ``bad_index``
    names the first junction whose two support lines are parallel.
    """
    corners: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        previous = lines[index - 1]
        if previous[0].axis == line[0].axis:
            return [], index
        if previous[0].axis == "y":
            corners.append((previous[0].cavity_const, line[0].cavity_const))
        else:
            corners.append((line[0].cavity_const, previous[0].cavity_const))
    return corners, None


def _derive_boundary_facts(view: AsMeasuredViewV1, *, min_room_area_m2: float
                           ) -> _BoundaryDerivation:
    """Derive edges and the named readout for cavities that yield no edges."""
    footprint, ring_records = _boundary_footprint(view)
    if footprint.is_empty or not ring_records:
        return _BoundaryDerivation(edges=[], losses=[])
    wall_region = _boundary_wall_region(view)
    geometry = footprint.difference(wall_region)
    threshold = float(min_room_area_m2) * UNITS_PER_METRE * UNITS_PER_METRE
    cavities = [part for part in getattr(geometry, "geoms", [geometry])
                if (part.geom_type == "Polygon" and not part.is_empty
                    and part.area > threshold)]
    cavities.sort(key=lambda cavity: tuple(round(value, 6)
                                            for value in cavity.bounds))
    cavity_ids = {id(cavity): _boundary_cavity_id(view.view_id, cavity)
                  for cavity in cavities}
    groups = _boundary_wall_groups(view)
    face_by_id = {face.id: face for face in view.face_lines}
    records: list[AsMeasuredBoundaryEdgeV1] = []
    losses: list[AsMeasuredBoundaryRingLossV1] = []

    for cavity in cavities:
        cavity_id = cavity_ids[id(cavity)]
        ring = [(int(round(x)), int(round(y)))
                for x, y in list(cavity.exterior.coords)[:-1]]
        # ⭐ Which side of a ring segment the cavity lies on is a LOCAL fact and
        # is read from the ring's own winding.  ⛔ Comparing against one global
        # representative point only works for a convex cavity: on the L-shaped
        # sm25 corridor it put 7 of 38 segments on the wrong side, which swapped
        # near/far face readings, inverted the outward normal, and made the exit
        # ray land back inside the cavity -- producing edges that named THEIR
        # OWN cavity as the room next door.
        counter_clockwise = cavity.exterior.is_ccw
        spans: list[_BoundarySpan] = []
        local_failures: list[AsMeasuredBoundaryFailureSpanV1] = []
        fatal_loss: AsMeasuredBoundaryRingLossV1 | None = None
        for a, b in zip(ring, ring[1:] + ring[:1]):
            if a[0] == b[0] and a[1] != b[1]:
                axis: Literal["x", "y"] = "y"
                cavity_const, lo, hi = a[0], min(a[1], b[1]), max(a[1], b[1])
                side: Literal[-1, 1] = (
                    -1 if (b[1] > a[1]) == counter_clockwise else 1)
            elif a[1] == b[1] and a[0] != b[0]:
                axis = "x"
                cavity_const, lo, hi = a[1], min(a[0], b[0]), max(a[0], b[0])
                side = 1 if (b[0] > a[0]) == counter_clockwise else -1
            else:
                fatal_loss = AsMeasuredBoundaryRingLossV1(
                    cavity_id=cavity_id, area_units2=int(round(cavity.area)),
                    span=_boundary_failure_span(p1=a, p2=b),
                    reason="non_axis_segment")
                break
            owners = _boundary_owners(groups, axis, cavity_const, lo, hi)
            end_caps = ([] if len(owners) == 1
                        else _boundary_endcap_groups(
                            groups, axis, cavity_const, lo, hi))
            if len(owners) != 1 and not (not owners and len(end_caps) == 1):
                nearest_face = _boundary_nearest_same_axis_face(
                    groups, axis, cavity_const, lo, hi)
                failure_span = _boundary_failure_span(
                    p1=a, p2=b, axis=axis, const=cavity_const,
                    lo=lo, hi=hi, side=side,
                    nearest_same_axis_wall_face_const=nearest_face)
                fatal_loss = AsMeasuredBoundaryRingLossV1(
                    cavity_id=cavity_id, area_units2=int(round(cavity.area)),
                    span=failure_span, reason="owner_count",
                    owner_count=len(owners))
                break
            if not owners:
                # An end cap constructs a corner and nothing else.  ⛔ It never
                # becomes an edge record: the three things a
                # ``BoundaryConditionEvidenceV1`` needs -- a paired parallel
                # face, a positive ``thickness_units``, and face lines on both
                # sides -- do not exist for it, so any edge record here would
                # have to invent them.  Admissible only when the wall really
                # stops ON a measured face of a wall parallel to the segment.
                if not _boundary_parallel_measured_faces(
                        groups, axis, cavity_const):
                    nearest_face = _boundary_nearest_same_axis_face(
                        groups, axis, cavity_const, lo, hi)
                    fatal_loss = AsMeasuredBoundaryRingLossV1(
                        cavity_id=cavity_id,
                        area_units2=int(round(cavity.area)),
                        span=_boundary_failure_span(
                            p1=a, p2=b, axis=axis, const=cavity_const,
                            lo=lo, hi=hi, side=side,
                            nearest_same_axis_wall_face_const=nearest_face),
                        reason="endcap_const_not_a_measured_parallel_face")
                    break
                spans.append(_BoundarySpan(
                    axis=axis, cavity_const=cavity_const, lo=lo, hi=hi,
                    side=side, p1=a, p2=b, group=end_caps[0],
                    boundary_condition=_ENDCAP_NON_CONDITION, kind="endcap"))
                continue
            group = owners[0]
            near_side: Literal["lo", "hi"] = "lo" if side < 0 else "hi"
            far_side: Literal["lo", "hi"] = "hi" if near_side == "lo" else "lo"
            near_handles, far_handles = group.handles(near_side), group.handles(far_side)
            raw_near = round(sum(face_by_id[handle].const for handle in near_handles)
                             / len(near_handles))
            raw_far = round(sum(face_by_id[handle].const for handle in far_handles)
                            / len(far_handles))
            candidate = _BoundarySpan(
                axis=axis, cavity_const=cavity_const, lo=lo, hi=hi,
                side=side, p1=a, p2=b, group=group,
                boundary_condition="unknown")
            condition, _evidence, logical = _classify_boundary_fact(
                candidate, raw_near, raw_far, footprint, ring_records,
                wall_region, cavities, cavity_ids)
            if logical:
                candidate.boundary_condition = condition
                spans.append(candidate)
                continue

            # A single witness may reveal that a span crosses a junction, but
            # it must not decide the fate of the entire span or cavity.  Only
            # then partition at measured geometry transitions and reclassify
            # every resulting cell.  Already-logical spans retain their
            # established identity and classification granularity.
            cuts = _boundary_transition_points(
                candidate, raw_far, footprint, wall_region, cavities)
            cells = list(zip(cuts, cuts[1:]))
            # ⭐ The cut coordinates are sorted ascending, but the ring may
            # traverse this span descending.  Emitting cells in coordinate
            # order would scramble the ring sequence for every subdivided span.
            along = 1 if candidate.axis == "y" else 0
            if candidate.p1[along] > candidate.p2[along]:
                cells.reverse()
            for child_lo, child_hi in cells:
                child = _boundary_subspan(candidate, child_lo, child_hi)
                child.subdivided = True
                condition, _evidence, logical = _classify_boundary_fact(
                    child, raw_near, raw_far, footprint, ring_records,
                    wall_region, cavities, cavity_ids)
                if not logical:
                    local_failures.append(_boundary_failure_span(
                        p1=child.p1, p2=child.p2, axis=child.axis,
                        const=child.cavity_const, lo=child.lo, hi=child.hi,
                        side=child.side))
                    continue
                child.boundary_condition = condition
                spans.append(child)
        if fatal_loss is not None:
            losses.append(fatal_loss)
            continue

        merged = _merge_boundary_spans(spans)
        lines = _boundary_support_lines(merged)
        supports = [span for line in lines for span in line]
        if len(lines) < 3:
            if local_failures:
                losses.append(AsMeasuredBoundaryRingLossV1(
                    cavity_id=cavity_id, area_units2=int(round(cavity.area)),
                    span=local_failures[0], reason="classify_illogical"))
            elif merged:
                witness = merged[0]
                losses.append(AsMeasuredBoundaryRingLossV1(
                    cavity_id=cavity_id, area_units2=int(round(cavity.area)),
                    span=_boundary_failure_span(
                        p1=witness.p1, p2=witness.p2, axis=witness.axis,
                        const=witness.cavity_const, lo=witness.lo, hi=witness.hi,
                        side=witness.side),
                    reason="merged_lt_3"))
            else:
                raise ValueError(
                    f"as_measured_boundary_empty_without_failure:{cavity_id}")
            continue
        corners, parallel_index = _boundary_ring_corners(lines)
        if parallel_index is not None:
            witness = lines[parallel_index][0]
            losses.append(AsMeasuredBoundaryRingLossV1(
                cavity_id=cavity_id, area_units2=int(round(cavity.area)),
                span=_boundary_failure_span(
                    p1=witness.p1, p2=witness.p2, axis=witness.axis,
                    const=witness.cavity_const, lo=witness.lo, hi=witness.hi,
                    side=witness.side),
                reason="adjacent_support_lines_parallel"))
            continue
        placed: list[tuple[tuple[int, int], tuple[int, int]]] = []
        for line_index, line in enumerate(lines):
            for position, span in enumerate(line):
                placed.append((
                    corners[line_index] if position == 0 else span.p1,
                    corners[(line_index + 1) % len(corners)]
                    if position == len(line) - 1 else span.p2))
        corner_ring = Polygon(corners)
        if (len(set(corners)) != len(corners) or corner_ring.is_empty
                or not corner_ring.is_valid or corner_ring.area <= 0
                or any(p1 == p2 for p1, p2 in placed)):
            witness = supports[0]
            losses.append(AsMeasuredBoundaryRingLossV1(
                cavity_id=cavity_id, area_units2=int(round(cavity.area)),
                span=_boundary_failure_span(
                    p1=witness.p1, p2=witness.p2, axis=witness.axis,
                    const=witness.cavity_const, lo=witness.lo, hi=witness.hi,
                    side=witness.side),
                reason="intersection_ring_invalid"))
            continue
        cavity_records: list[AsMeasuredBoundaryEdgeV1] = []
        merge_witness: _BoundarySpan | None = None
        for sequence, span in enumerate(supports):
            near_side = "lo" if span.side < 0 else "hi"
            far_side = "hi" if near_side == "lo" else "lo"
            near_handles = span.group.handles(near_side)
            far_handles = span.group.handles(far_side)
            raw_near = round(sum(face_by_id[handle].const for handle in near_handles)
                             / len(near_handles))
            raw_far = round(sum(face_by_id[handle].const for handle in far_handles)
                            / len(far_handles))
            condition, evidence, logical = _classify_boundary_fact(
                span, raw_near, raw_far, footprint, ring_records,
                wall_region, cavities, cavity_ids)
            if not logical:
                # ⭐ Two contiguous spans that each classified logically can
                # merge into one whose own witness lands on the junction
                # between them.  The merged record then has no witness that
                # supports it, so it must not be written.  ⛔ This is a
                # property of THIS cavity: it is named here and the cavity is
                # dropped, rather than aborting the whole document.
                merge_witness = span
                break
            cavity_records.append(AsMeasuredBoundaryEdgeV1(
                id=_boundary_opaque_id(
                    "boundary-edge", cavity_id, sequence, span.axis,
                    span.cavity_const, span.lo, span.hi, span.side,
                    *span.group.wall_ids),
                cavity_id=cavity_id, sequence=sequence, axis=span.axis,
                cavity_const=span.cavity_const, span_lo=span.lo, span_hi=span.hi,
                side=span.side,
                p1=list(placed[sequence][0]), p2=list(placed[sequence][1]),
                wall_ids=span.group.wall_ids,
                face_line_handles=span.group.face_line_handles,
                boundary_condition=condition, evidence=evidence))
        if merge_witness is not None:
            losses.append(AsMeasuredBoundaryRingLossV1(
                cavity_id=cavity_id, area_units2=int(round(cavity.area)),
                span=_boundary_failure_span(
                    p1=merge_witness.p1, p2=merge_witness.p2,
                    axis=merge_witness.axis, const=merge_witness.cavity_const,
                    lo=merge_witness.lo, hi=merge_witness.hi,
                    side=merge_witness.side),
                reason="merged_span_has_no_supporting_witness"))
            continue
        records.extend(cavity_records)
    return _BoundaryDerivation(edges=records, losses=losses)


def derive_boundary_edges(view: AsMeasuredViewV1, *, min_room_area_m2: float
                          ) -> list[AsMeasuredBoundaryEdgeV1]:
    """Derive logical boundary facts without selecting an output profile."""
    return _derive_boundary_facts(
        view, min_room_area_m2=min_room_area_m2).edges


def derive_boundary_ring_losses(
        view: AsMeasuredViewV1, *, min_room_area_m2: float
        ) -> list[AsMeasuredBoundaryRingLossV1]:
    """Return the named readout for above-threshold cavities with no edges."""
    return _derive_boundary_facts(
        view, min_room_area_m2=min_room_area_m2).losses


def refresh_boundary_edges(view: AsMeasuredViewV1) -> AsMeasuredViewV1:
    """Refresh boundary evidence after an authorised face-line translation.

    Revision actions cannot re-pair walls or invent cavities.  The stored
    projection-free edge geometry therefore remains the identity anchor, while
    raw/opposite face readings, exit witnesses, and the classification itself
    are recomputed from the translated face lines.  This keeps ``as_signed`` a
    truthful derivation rather than carrying stale ``as_measured`` evidence.
    """
    if not view.boundary_edges:
        return view
    footprint, ring_records = _boundary_footprint(view)
    if footprint.is_empty or not ring_records:
        return view
    wall_region = _boundary_wall_region(view)
    by_cavity: dict[str, list[AsMeasuredBoundaryEdgeV1]] = {}
    for edge in view.boundary_edges:
        by_cavity.setdefault(edge.cavity_id, []).append(edge)
    cavities = []
    cavity_ids: dict[int, str] = {}
    for cavity_id, edges in sorted(by_cavity.items()):
        ordered = sorted(edges, key=lambda edge: edge.sequence)
        cavity = Polygon([edge.p1 for edge in ordered])
        if cavity.is_empty or not cavity.is_valid or cavity.area <= 0:
            raise ValueError(f"as_measured_boundary_refresh_invalid_cavity:{cavity_id}")
        cavities.append(cavity)
        cavity_ids[id(cavity)] = cavity_id
    groups = _boundary_wall_groups(view)
    groups_by_walls = {tuple(group.wall_ids): group for group in groups.values()}
    face_by_id = {face.id: face for face in view.face_lines}
    refreshed = []
    for edge in view.boundary_edges:
        group = groups_by_walls.get(tuple(edge.wall_ids))
        if group is None:
            raise ValueError(
                f"as_measured_boundary_refresh_wall_group_missing:{edge.id}")
        span = _BoundarySpan(
            axis=edge.axis, cavity_const=edge.cavity_const,
            lo=edge.span_lo, hi=edge.span_hi, side=edge.side,
            p1=tuple(edge.p1), p2=tuple(edge.p2), group=group,
            boundary_condition=edge.boundary_condition)
        near_side: Literal["lo", "hi"] = "lo" if edge.side < 0 else "hi"
        far_side: Literal["lo", "hi"] = "hi" if near_side == "lo" else "lo"
        near_handles, far_handles = group.handles(near_side), group.handles(far_side)
        raw_near = round(sum(face_by_id[handle].const for handle in near_handles)
                         / len(near_handles))
        raw_far = round(sum(face_by_id[handle].const for handle in far_handles)
                        / len(far_handles))
        condition, evidence, _logical = _classify_boundary_fact(
            span, raw_near, raw_far, footprint, ring_records,
            wall_region, cavities, cavity_ids)
        refreshed.append(AsMeasuredBoundaryEdgeV1.model_validate({
            **edge.model_dump(mode="json"),
            "boundary_condition": condition,
            "evidence": evidence.model_dump(mode="json"),
        }))
    return AsMeasuredViewV1.model_validate({
        **view.model_dump(mode="json"),
        "boundary_edges": [edge.model_dump(mode="json") for edge in refreshed],
    })


def _opening_records(geo: P1PlanViewGeometry, walls: list[AsMeasuredWallV1],
                     sx: float, tx: float, sy: float,
                     ty: float) -> tuple[list[AsMeasuredOpeningV1], list[dict]]:
    by_face: dict[tuple[str, int, int], list[AsMeasuredWallV1]] = {}
    for wall in walls:
        by_face.setdefault((wall.axis, wall.face_lo, wall.face_hi), []).append(wall)
    records: list[AsMeasuredOpeningV1] = []
    unresolved: list[dict] = []
    for op in geo.openings:
        x0, y0, x1, y1 = op.rect_dxf_mm
        wx = sorted((_geom_units(sx * x0 + tx), _geom_units(sx * x1 + tx)))
        wy = sorted((_geom_units(sy * y0 + ty), _geom_units(sy * y1 + ty)))
        c0, c1 = op.cross_section_mm
        if op.axis == "x":
            along_min, along_max = wx
            cross = sorted((_geom_units(sy * c0 + ty), _geom_units(sy * c1 + ty)))
        else:
            along_min, along_max = wy
            cross = sorted((_geom_units(sx * c0 + tx), _geom_units(sx * c1 + tx)))
        candidates = [w for w in by_face.get((op.axis, cross[0], cross[1]), [])
                      if not (along_max < w.along_min or along_min > w.along_max)]
        carriers = _sorted_handles(w.id for w in candidates)
        if not candidates:
            unresolved.append({"opening_id": op.handle, "axis": op.axis,
                               "cross_lo": cross[0], "cross_hi": cross[1],
                               "candidate_wall_ids": [],
                               "reason": "no_wall_with_this_face_pair"})
        records.append(AsMeasuredOpeningV1(
            id=op.handle, block_name=op.block_name, kind=op.kind, axis=op.axis,
            along_min=along_min, along_max=along_max,
            cross_lo=cross[0], cross_hi=cross[1], carrier_wall_ids=carriers,
            jamb_handles=_sorted_handles(set(op.jamb_handles)),
            classification=op.classification))
    records.sort(key=_opening_sort_key)
    unresolved.sort(key=lambda u: u["opening_id"])
    return records, unresolved


def _footprint_record(geo: P1PlanViewGeometry, sx: float, tx: float,
                      sy: float, ty: float) -> AsMeasuredFootprintV1:
    poly = geo.footprint_polygon
    rings: list[AsMeasuredRingV1] = []
    if poly is not None and not poly.is_empty:
        parts = list(getattr(poly, "geoms", [poly]))
        for index, part in enumerate(parts):
            exterior = getattr(part, "exterior", None)
            if exterior is None:
                continue
            rings.append(AsMeasuredRingV1(
                polygon_index=index, kind="exterior",
                points=[[_geom_units(sx * x + tx), _geom_units(sy * y + ty)]
                        for x, y in exterior.coords]))
            for hole in part.interiors:
                rings.append(AsMeasuredRingV1(
                    polygon_index=index, kind="interior",
                    points=[[_geom_units(sx * x + tx), _geom_units(sy * y + ty)]
                            for x, y in hole.coords]))
    return AsMeasuredFootprintV1(
        geom_type=str(poly.geom_type) if poly is not None else "None",
        is_empty=bool(poly is None or poly.is_empty), rings=rings)


def _handles_with_diagnostic_code(geo: P1PlanViewGeometry, code: str) -> list[str]:
    """⭐ ②-1b-R (F-136/A3): itemize a diagnostic population by HANDLE, ⛔ not
    just its count -- the converter already names exactly which strokes it
    dropped and why (``d.code`` / ``d.source_entity_handles``); this is
    transport of that readout, not a re-derivation of it.
    """
    out: list[str] = []
    for d in geo.diagnostics:
        if str(getattr(d.code, "value", d.code)) == code:
            out.extend(str(h) for h in d.source_entity_handles)
    return out


def _axis_snap_records(geo: P1PlanViewGeometry, sx: float, tx: float,
                       sy: float, ty: float) -> list[AsMeasuredAxisSnapV1]:
    """⭐⭐ dispatch ②-1b-S R2: itemize the ``tarch_wall_axis_snapped``
    diagnostics into the "snap list" GLM required -- ⛔ transport of the
    converter's own before/after/axis/magnitude readout, nothing recomputed
    except the world-frame affine + 0.1 mm unit conversion every other field
    in this document already goes through.
    """
    records: list[AsMeasuredAxisSnapV1] = []
    for d in geo.diagnostics:
        if str(getattr(d.code, "value", d.code)) != "tarch_wall_axis_snapped":
            continue
        handle = str(d.source_entity_handles[0])
        layer = geo.wall_line_layers.get(handle, "")
        ctx = d.context
        bx0, by0 = ctx["before_p0"]
        bx1, by1 = ctx["before_p1"]
        snapped_axis = ctx["snapped_axis"]
        minor_leg_mm = float(ctx["minor_leg_mm"])
        (ax0, ay0), (ax1, ay1) = d.source_points_dxf_mm
        scale = sx if snapped_axis == "x" else sy
        records.append(AsMeasuredAxisSnapV1(
            id=handle, layer=layer, snapped_axis=snapped_axis,
            # ⛔ before_* / minor_leg_units stay RAW (to_units only): they are
            # the pre-snap OBSERVATION this row exists to carry -- the A-11
            # coordinate extension explicitly excludes them (see _geom_units).
            before_p0=[to_units(sx * bx0 + tx), to_units(sy * by0 + ty)],
            before_p1=[to_units(sx * bx1 + tx), to_units(sy * by1 + ty)],
            after_p0=[_geom_units(sx * ax0 + tx), _geom_units(sy * ay0 + ty)],
            after_p1=[_geom_units(sx * ax1 + tx), _geom_units(sy * ay1 + ty)],
            minor_leg_units=to_units(abs(scale) * minor_leg_mm),
            angle_deg=float(ctx["angle_deg"])))
    records.sort(key=lambda r: r.id)
    return records


def _axis_snap_arbitration_records(geo: P1PlanViewGeometry, sx: float, tx: float,
                                   sy: float, ty: float
                                   ) -> list[AsMeasuredAxisSnapArbitrationV1]:
    """⭐ Itemize the ladder's TIER-2 refusals -- the strokes a human, ⛔ not
    the machine, has to decide about.

    Built by a SEPARATE pass over ``geo.diagnostics`` (the same population
    ``_readout_records`` carries verbatim), which is what gives
    ``_arbitration_ledger_has_teeth`` something independent to compare against.

    ⛔ Tier 3 (``angle_beyond_envelope``) is deliberately NOT here: a real
    diagonal is not a suspected drafting error and there is nothing for a
    person to arbitrate.  Both tiers still raise the same fail-closed
    ``tarch_wall_nonorthogonal`` BLOCK and both are still dropped; ⭐ the tier
    is what tells them apart, and it is on the diagnostic's own context.
    """
    records: list[AsMeasuredAxisSnapArbitrationV1] = []
    for d in geo.diagnostics:
        if str(getattr(d.code, "value", d.code)) != "tarch_wall_nonorthogonal":
            continue
        ctx = d.context or {}
        if ctx.get("ladder_tier") != 2:
            continue
        handle = str(d.source_entity_handles[0])
        (px0, py0), (px1, py1) = d.source_points_dxf_mm
        # the deviation is measured on the SNAPPED axis, so it scales with that
        # axis' own factor -- the same rule ``_axis_snap_records`` uses.
        scale = abs(sx) if abs(px1 - px0) <= abs(py1 - py0) else abs(sy)
        records.append(AsMeasuredAxisSnapArbitrationV1(
            id=handle, layer=geo.wall_line_layers.get(handle, ""),
            ladder_tier=2, reason=str((ctx.get("refused_by") or [""])[0]),
            # ⛔ RAW, like every other pre-treatment observation in this file.
            p0=[to_units(sx * px0 + tx), to_units(sy * py0 + ty)],
            p1=[to_units(sx * px1 + tx), to_units(sy * py1 + ty)],
            deviation_units=to_units(scale * float(ctx["minor_leg_mm"])),
            deviation_limit_units=to_units(scale * float(ctx["deviation_limit_mm"])),
            cap_units=to_units(scale * float(ctx["cap_mm"])),
            angle_deg=float(ctx["angle_deg"])))
    records.sort(key=lambda r: r.id)
    return records


def _readout_records(geo: P1PlanViewGeometry) -> tuple[list[dict], list[dict]]:
    """Diagnostics + gates, VERBATIM.  ⛔ Nothing here is recomputed or filtered.

    ⭐ Not filtered by severity either: F-B (2026-08-29) measured that dropping
    BLOCK codes on a *successful* path passed the entire suite, because the only
    fixture with that combination was never inspected.
    """
    diagnostics = [{
        "code": str(getattr(d.code, "value", d.code)),
        "severity": str(getattr(d.severity, "value", d.severity)),
        "stage": str(getattr(d.stage, "value", d.stage)),
        "action_code": d.action_code,
        "handles": [str(h) for h in d.source_entity_handles],
        "points_dxf_mm": [[float(p[0]), float(p[1])] for p in d.source_points_dxf_mm],
        "context": d.context,
    } for d in geo.diagnostics]
    gates = [{"id": g.id, "name": g.name, "passed": bool(g.passed)} for g in geo.gates]
    return diagnostics, gates


#: ⭐ The identity gate, ⛔ NOT a new policy: this is GLM's "C-身份" criterion from
#: the F-126b cross-review, reused verbatim -- *no BLOCK whose stage is S0_input*.
#: S0 is the stage that asks "is the thing being measured the thing that was
#: declared"; a BLOCK there means the ruler never got to the drawing at all.
S0_INPUT_STAGE = "S0_input"


def _refuse_if_the_ruler_never_measured(geo: P1PlanViewGeometry) -> None:
    """⛔ Refuse loudly when the conversion never happened -- and ONLY then.

    ⚠️ The tempting rule ("refuse on any BLOCK") is wrong here and would defeat
    the whole layer.  MEASURED on the as-received sm25 drawing: it raises
    ``tarch_wall_nonorthogonal`` (BLOCK, S1) and ``tarch_wall_free_end`` (BLOCK,
    S4) precisely BECAUSE it is the un-retouched drawing -- those BLOCKs are the
    drawing's warts, which is the thing this layer exists to record.  Refusing
    on them would mean the facts layer can only ever measure drawings that have
    already been cleaned up, i.e. exactly the "bake the retouching into history"
    outcome the ledger §十 rejects.

    ⭐ So the split is the one the F-126b review already argued and accepted:
    a ``S0_input`` BLOCK (or no geometry at all) means *the measurement did not
    happen*; a content BLOCK means *the measurement happened and found
    something*.  The latter rides out in ``converter_readouts`` -- ⛔ not
    filtered, not summarised away (F-B).
    """
    s0_blocks = sorted({str(getattr(d.code, "value", d.code)) for d in geo.diagnostics
                        if str(getattr(d.severity, "value", d.severity)) == "BLOCK"
                        and str(getattr(d.stage, "value", d.stage)) == S0_INPUT_STAGE})
    if s0_blocks:
        raise AsMeasuredUnavailable(
            "upstream_identity_block", view_id=geo.view_id, blocking_codes=s0_blocks,
            detail="an S0_input BLOCK means the converter never measured this "
                   "drawing; recording that as a facts layer would store an "
                   "absence as a measurement")
    if not geo.wall_lines:
        raise AsMeasuredUnavailable(
            "no_geometry", view_id=geo.view_id,
            detail="the conversion ran without an S0 block and still collected "
                   "zero wall lines; that is a different failure from a refused "
                   "input and must not share its exit")


def build_view(geo: P1PlanViewGeometry, affine: Affine2D, *,
               t_max_m: float, merge_m: float = MERGE_M,
               min_room_area_m2: float | None = None) -> AsMeasuredViewV1:
    """⭐ Pure: P1 geometry in, facts document out.  ⛔ Reads no file.

    ``t_max_m`` is the request's widest DECLARED wall thickness.  ⛔ It is not a
    pairing threshold -- it bounds the denominator's D2 jamb-cap test only, and
    it has to be passed in because this function is pure and the range lives on
    the request.
    """
    _refuse_if_the_ruler_never_measured(geo)
    sx, tx, sy, ty = _axis_aligned(affine, geo.view_id)
    faces, skew, degenerate = _face_line_records(geo, sx, tx, sy, ty)
    by_id = {face.id: face for face in faces}
    # ⭐ THE SAME D1-D5 pass the scoreable denominator runs -- ⛔ not a second
    # implementation, and ⛔ not the raw ``face_lines`` (225 of them on signed
    # plan-F1, of which only 110 are pairable; pairing all 225 puts the ghost
    # walls straight back).
    drawn = face_line_targets(geo, affine, t_max_m=t_max_m, merge_m=merge_m)
    walls, unpaired = _pair_face_lines_into_walls(drawn["targets"], set(by_id))
    caps = _sorted_handles({a["handle"] for a in drawn["allowed_not_required"]
                            if a["handle"] in by_id})
    split_const = _split_const_groups(drawn["targets"], by_id)
    bands, faceless = _jamb_cap_band_records(geo, faces, sx, tx, sy, ty)
    openings, unresolved = _opening_records(geo, walls, sx, tx, sy, ty)
    diagnostics, gates = _readout_records(geo)
    axis_snapped = _axis_snap_records(geo, sx, tx, sy, ty)
    arbitrations = _axis_snap_arbitration_records(geo, sx, tx, sy, ty)
    view = AsMeasuredViewV1(
        view_id=geo.view_id, floor_id=geo.floor_id,
        face_lines=faces, walls=walls, openings=openings,
        footprint=_footprint_record(geo, sx, tx, sy, ty),
        converter_readouts=AsMeasuredConverterReadoutsV1(
            dangles=geo.dangles, cuts=geo.cuts, invalid=geo.invalid,
            degenerate_line_count=geo.degenerate_line_count,
            degenerate_line_handles=_sorted_handles(
                _handles_with_diagnostic_code(geo, "tarch_wall_degenerate_line")),
            s1_nonorthogonal_discarded_handles=_sorted_handles(
                _handles_with_diagnostic_code(geo, "tarch_wall_nonorthogonal")),
            wall_lines_total=len(geo.wall_lines),
            degenerate_in_wall_lines=degenerate,
            all_wall_handles=_sorted_handles(geo.all_wall_handles),
            non_orthogonal_lines=skew,
            axis_snap_arbitrations=arbitrations,
            axis_snapped_lines=axis_snapped,
            unresolved_opening_carriers=unresolved,
            jamb_cap_bands=bands,
            jamb_cap_bands_missing_a_face_line=faceless,
            face_lines_excluded_as_jamb_caps=caps,
            face_lines_not_paired_into_a_wall=unpaired,
            face_groups_with_a_split_const=split_const,
            diagnostics=diagnostics, gates=gates))
    if min_room_area_m2 is None:
        return view
    boundary = _derive_boundary_facts(
        view, min_room_area_m2=min_room_area_m2)
    return AsMeasuredViewV1.model_validate({
        **view.model_dump(mode="json"),
        "boundary_edges": [edge.model_dump(mode="json")
                           for edge in boundary.edges],
        "boundary_ring_losses": [loss.model_dump(mode="json")
                                 for loss in boundary.losses],
    })


def build_as_measured(dxf: Path, request_path: Path, *,
                      view_ids: list[str] | None = None) -> AsMeasuredV1:
    """Measure ``dxf`` under ``request_path`` and return the facts document.

    ⛔ The DXF is staged into a temp dir before the converter sees it: answer
    roots are read-only inputs and ``run_p1_plan_view`` enforces that
    structurally.  ⛔ Nothing is written to disk by this function.
    """
    dxf = Path(dxf)
    request = TarchConversionRequestV1.model_validate_json(
        Path(request_path).read_text(encoding="utf-8"))
    wanted = sorted(view_ids) if view_ids else sorted(v.id for v in request.plan_views)
    tooling = load_gt_tooling_config(GT_CFG, VG_CFG)
    views: list[AsMeasuredViewV1] = []
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / dxf.name
        shutil.copy2(dxf, staged)
        for view_id in wanted:
            view = next(v for v in request.plan_views if v.id == view_id)
            geo = run_p1_plan_view(staged, request, view, tooling)
            views.append(build_view(
                geo, view.world_from_source_m,
                t_max_m=max(float(t) for t in request.wall_thickness_range_m),
                min_room_area_m2=float(request.min_room_area_m2)))
    return AsMeasuredV1(
        case=request.case,
        source_dxf_label=dxf.name,
        source_dxf_sha256=hashlib.sha256(dxf.read_bytes()).hexdigest(),
        request_sha256=compute_request_sha256(request),
        converter_implementation_fingerprint=converter_sha256(),
        views=views)


__all__ = [
    "UNITS_PER_METRE", "UNIT_LABEL", "REQUEST_AS_MEASURED_ALLOWED_DELTA",
    "INGEST_RESOLUTION_UNITS", "INGEST_RESOLUTION_LABEL",
    "INGEST_NON_COORDINATE_PATHS",
    "AsMeasuredUnavailable", "AsMeasuredV1", "AsMeasuredViewV1",
    "AsMeasuredFaceLineV1", "AsMeasuredWallV1", "AsMeasuredOpeningV1",
    "AsMeasuredRingV1", "AsMeasuredFootprintV1",
    "BoundaryConditionEvidenceV1", "AsMeasuredBoundaryEdgeV1",
    "AsMeasuredBoundaryFailureSpanV1", "AsMeasuredBoundaryRingLossV1",
    "AsMeasuredNonOrthogonalLineV1", "AsMeasuredAxisSnapV1",
    "AsMeasuredAxisSnapArbitrationV1", "AsMeasuredConverterReadoutsV1",
    "assert_request_is_pure_source_swap", "derive_as_measured_request",
    "request_file_bytes", "AS_RECEIVED_ID_SUFFIX",
    "build_as_measured", "build_view", "derive_boundary_edges",
    "derive_boundary_ring_losses",
    "refresh_boundary_edges",
    "S0_INPUT_STAGE",
    "canonical_bytes", "content_sha256", "to_units",
    "snap_to_ingest_resolution", "scan_ingest_resolution_violations",
]
