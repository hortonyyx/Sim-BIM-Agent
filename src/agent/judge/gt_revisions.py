"""``revisions`` -- the second cut of the facts layer -- and ``as_signed``, its
mechanical derivation from :mod:`src.agent.judge.as_measured`.

    as_measured  +  revisions  =>  as_signed        (ledger §一 / §三 / §六)
                    ^^^^^^^^^   ^^^^^^^^^^^  THIS FILE.

⭐ WHAT THIS FILE IS: the shape of ONE human-signed correction (``RevisionV1``),
a ledger of them (``RevisionsLedgerV1``), and a PURE function
(:func:`derive_as_signed`) that folds a ledger onto an ``AsMeasuredV1`` to
produce ``AsSignedV1`` -- plus the reproducibility gate that keeps the two in
sync (:func:`verify_as_signed_reproduction`).

⛔ WHAT THIS FILE IS NOT: a place that writes ``revisions.json`` on anyone's
behalf.  ledger §九#3 / guide precedent (F-117): "只有签字流程能写
revisions" -- this module never touches a filesystem path, and the only way an
UNSIGNED record could reach :func:`derive_as_signed` and do anything is
already closed at the type level (see ``RevisionV1``'s own validator): a
record with ``verdict == "unsigned"`` cannot carry an ``action`` at all, so
there is no code path in :func:`derive_as_signed` that needs to notice it is
unsigned -- it structurally has nothing to apply.

## Two kinds of "action" on one record, ⛔ never conflated

``candidate_action`` -- what a DETECTOR proposes, before anyone has signed
anything.  Advisory only; :func:`derive_as_signed` never reads it.

``action`` -- what a SIGNED ``verdict == "drawing_error"`` record actually
does.  The model validator below makes the pairing structural: ``action`` may
be non-``None`` if and only if the record is signed AND its verdict is
``drawing_error``.  Naming the two differently means "a machine's guess" and
"an authorised instruction" can never be the same field silently swapped for
each other ([[observation-named-as-fact-travels-as-fact]]).

## What ``translate`` is, and is not (dispatch ①: "先只实现 translate")

A ``translate`` action names ONE integer field of ONE ``AsMeasuredFaceLineV1``
(picked out by ``(view_id, handle)``) and a signed delta in the document's own
0.1 mm units.  It is a deterministic operation ON ``as_measured`` in the
literal sense: given the same as_measured document and the same action, the
result is one line of arithmetic, re-validated by the SAME model
(``AsMeasuredFaceLineV1``) that already enforces ``along_min < along_max`` --
so a translate that produces an ill-formed line fails LOUDLY, at
:func:`derive_as_signed` time, rather than silently.

⛔ MEASURED on sm25's changed-handle batch, and worth stating because it
contradicts the ledger's own illustrative example: a "move this wall over"
translate is not what most real corrections look like.  The measured shape
has MOVED TWICE since, and each move was a real change in what the machine
diff can honestly express:

* ②-1b (2026-08-29), 5 changed DXF handles between
  ``sm25-L_t3_as_received.dxf`` and ``sm25-L_t3.dxf``
  (13AD 13AC 13AF 160A 13AE): only 2 (13AC, 160A) were a translate of an
  existing face line's own ``along_min``/``along_max`` (a ~0.2 mm endpoint
  trim); the other 3 (13AD, 13AE, 13AF) were NON-ORTHOGONAL strokes in the
  as-received drawing (up to ~5.8 mm off axis) and were reported with
  ``candidate_action=None``.
* ②-1b-S then admitted 13AD/13AE through the two USER-SIGNED axis-snap
  gates, so they became real face lines on the as-received side too.
* A-11 (2026-09-05, user 「走乙」) then snapped the ingest resolution to
  1 mm, and the honest diff changed shape again, MEASURED on this tree:
  13AC/160A's ~0.2 mm "difference" was pure representation residue and is
  ABSORBED -- those two records are gone from the ledger (exactly the
  user's ruling: 0.1 mm residue belongs to the measurement representation,
  ⛔ never to a signed ``drawing_error``) -- while 13AD and 13AE now show
  their REAL correction for the first time as a well-formed single-field
  translate (``const -30``, i.e. 3.0 mm: the signed drawing moved those
  two walls onto the position the as-received one missed it by).  13AF
  alone remains ``candidate_action=None`` -- it is skewed too far for the
  signed snap gates, so it is still absent from the as-received
  ``face_lines``, and straightening a diagonal into an axis-aligned line
  is still not expressible as "add a delta to one stored field" (①'s
  "遇到再加").  ⚠️ A-11's snap sits at the INGEST door (``as_measured``);
  it does NOT constrain a future signed translate's delta, which may
  legitimately land off the 1 mm grid.

## Propagation scope (⛔ a named limitation, not an oversight)

:func:`derive_as_signed` updates ONLY the targeted face line's named field.
It does NOT re-run wall pairing, does not touch ``walls``/``openings``, and
does not recompute ``footprint``.  A translate large enough to change which
face lines pair into a wall, or to move an opening's carrier, is out of this
unit's scope -- ②-1c owns ``AnswerCompiler`` and the dependency closures that
would need to notice such a change.  This is stated here rather than silently
producing a ``walls``/``openings`` block that quietly disagrees with the
(now-edited) ``face_lines``.
"""
from __future__ import annotations

import hashlib
import json
from typing import Iterable, Literal

from pydantic import Field, model_validator

from .as_drawn import denominator as _denominator
from .as_measured import (UNITS_PER_METRE, AsMeasuredFaceLineV1,
                          AsMeasuredV1, AsMeasuredViewV1, content_sha256,
                          refresh_boundary_edges)
from .gt_schema import DxfHandle, Hex64, HumanLabel, StableId
from .tarch_converter_schema import _StrictModel

__all__ = [
    "CURRENT_DERIVER_VERSION", "SUPPORTED_ACTION_KINDS",
    "RevisionTargetV1", "RevisionFindingV1", "TranslateActionV1", "RevisionV1",
    "RevisionsLedgerV1", "AsSignedDerivationKeyV1", "AsSignedV1",
    "AsSignedReproductionError",
    "canonical_revisions_bytes", "revisions_content_sha256",
    "canonical_as_signed_bytes", "as_signed_content_sha256",
    "derive_as_signed", "verify_as_signed_reproduction",
    "detect_translate_candidates",
]

#: ⭐ Dispatch ①: "先只实现 translate，其余遇到再加，且每加一种必须能说清
#: 『它是 as_measured 上的一个确定性操作』".  This tuple is the ONE place a
#: future action kind gets added; :class:`RevisionV1`'s discriminated
#: ``action`` field enforces it structurally (an unsupported ``kind`` fails
#: pydantic validation, named, at construction time -- it can never reach an
#: ``else: pass``).
SUPPORTED_ACTION_KINDS: tuple[str, ...] = ("translate",)

CURRENT_DERIVER_VERSION = 2


# --------------------------------------------------------------------------- #
# revisions
# --------------------------------------------------------------------------- #
class RevisionTargetV1(_StrictModel):
    """Which DXF entity a revision is about.

    ⛔ Deliberately NOT "which face line" or "which wall": as-measured may not
    yet classify the entity as a face line at all (a non-orthogonal stroke, or
    one filtered before classification -- both measured on sm25's real 5-line
    batch, see the module docstring).  A DXF handle is the one identifier that
    exists regardless of how -- or whether -- as_measured currently classifies
    the entity, so it is the one a target can always name.
    """
    kind: Literal["dxf_entity"] = "dxf_entity"
    view_id: StableId
    handle: DxfHandle


class RevisionFindingV1(_StrictModel):
    """What a detector observed, ⛔ before anyone has judged it."""
    check: HumanLabel
    magnitude_0p1mm: int | None = None
    detail: str = Field(min_length=1)


class TranslateActionV1(_StrictModel):
    """Add ``delta_0p1mm`` to one named integer field of the targeted face line.

    ``field`` is deliberately one of the face line's OWN stored scalars
    (``AsMeasuredFaceLineV1``'s fields), never a derived quantity -- so
    "recompute it and compare" is always "read the field back", never a
    second formula that could itself disagree with the first.
    """
    kind: Literal["translate"] = "translate"
    field: Literal["const", "along_min", "along_max"]
    delta_0p1mm: int

    @model_validator(mode="after")
    def _nonzero(self):
        if self.delta_0p1mm == 0:
            raise ValueError("revision_translate_delta_zero_is_not_a_correction")
        return self


class RevisionV1(_StrictModel):
    """One line of the revisions ledger (ledger §三, dispatch R1 -- fields
    copied literally: ``id / target / finding / verdict / action / reason /
    signed_by / signed_at``, plus ``candidate_action`` -- see module docstring
    for why that one field is added.)

    ⭐⭐ STRUCTURAL gate (verification #2): an ``unsigned`` record cannot carry
    a non-``None`` ``action`` at all, and only a ``drawing_error`` record may.
    This is what makes "a revision that was never signed cannot influence
    ``as_signed``" true by construction rather than by
    :func:`derive_as_signed` remembering to check.
    """
    id: StableId
    target: RevisionTargetV1
    finding: RevisionFindingV1
    verdict: Literal["drawing_error", "as_designed", "producer_defect", "unsigned"] = "unsigned"
    #: ⛔ ADVISORY ONLY.  A detector's proposal, visible on an unsigned record
    #: so a human has something concrete to judge -- never read by
    #: :func:`derive_as_signed`.
    candidate_action: TranslateActionV1 | None = None
    #: ⭐ AUTHORITATIVE.  Present if and only if ``verdict == "drawing_error"``
    #: AND the record is signed.
    action: TranslateActionV1 | None = None
    reason: str | None = None
    signed_by: str | None = None
    signed_at: str | None = None

    @model_validator(mode="after")
    def _signed_consistency(self):
        if self.verdict == "unsigned":
            if self.signed_by is not None or self.signed_at is not None:
                raise ValueError("revision_unsigned_must_not_carry_a_signature")
            if self.action is not None:
                raise ValueError(
                    "revision_unsigned_must_not_carry_an_authoritative_action")
        else:
            if not self.signed_by or not self.signed_at:
                raise ValueError("revision_signed_verdict_requires_a_signature")
            if self.verdict == "drawing_error" and self.action is None:
                raise ValueError("revision_drawing_error_requires_an_action")
            if self.verdict != "drawing_error" and self.action is not None:
                raise ValueError(
                    "revision_non_drawing_error_must_not_carry_an_action")
        return self


class RevisionsLedgerV1(_StrictModel):
    """A signed (or draft) revisions ledger, bound to exactly ONE ``as_measured``
    document by its content hash -- ⛔ a ledger cannot silently be replayed
    against a different (even a byte-identical-looking-but-not) as_measured.
    """
    schema_version: Literal[1] = 1
    case: StableId
    as_measured_content_sha256: Hex64
    revisions: list[RevisionV1] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self):
        ids = [r.id for r in self.revisions]
        if len(ids) != len(set(ids)):
            raise ValueError("revisions_id_not_unique")
        return self


def canonical_revisions_bytes(ledger: RevisionsLedgerV1) -> bytes:
    payload = ledger.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"


def revisions_content_sha256(ledger: RevisionsLedgerV1) -> str:
    return hashlib.sha256(canonical_revisions_bytes(ledger)).hexdigest()


# --------------------------------------------------------------------------- #
# as_signed -- MECHANICAL derivation, ⛔ never hand-authored
# --------------------------------------------------------------------------- #
class AsSignedDerivationKeyV1(_StrictModel):
    """Ledger §六's derivation key, literally: the as_measured content hash,
    the whole revisions ledger's hash, and the deriver's own version."""
    as_measured_content_sha256: Hex64
    revisions_content_sha256: Hex64
    deriver_version: Literal[2] = 2


class AsSignedV1(_StrictModel):
    """⭐ "同一个 schema (⛔ 别为它另发明一套字段) + 一个派生键" (ledger §1.4):
    every field ``AsMeasuredV1`` has, verbatim, plus ``derivation``.

    ⛔ NO independent trust root (ledger §七): every byte here is a pure
    function of ``as_measured`` + ``revisions``, which is exactly what
    :func:`verify_as_signed_reproduction` checks.
    """
    schema_version: Literal[1] = 1
    case: StableId
    source_dxf_label: HumanLabel
    source_dxf_sha256: Hex64
    request_sha256: Hex64
    coordinate_unit: Literal["0.1mm"] = "0.1mm"
    units_per_metre: Literal[10000] = 10000
    converter_implementation_fingerprint: Hex64
    derivation: AsSignedDerivationKeyV1
    views: list[AsMeasuredViewV1] = Field(min_length=1)


def canonical_as_signed_bytes(document: AsSignedV1) -> bytes:
    payload = document.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"


def as_signed_content_sha256(document: AsSignedV1) -> str:
    return hashlib.sha256(canonical_as_signed_bytes(document)).hexdigest()


class AsSignedReproductionError(RuntimeError):
    """⛔ Raised, never swallowed: `as_signed` must be recomputable from
    `as_measured` + `revisions`, or it says loudly why it is not."""


def _apply_translate(face: AsMeasuredFaceLineV1, action: TranslateActionV1) -> AsMeasuredFaceLineV1:
    updated = getattr(face, action.field) + action.delta_0p1mm
    # model_validate (⛔ not model_copy) so the along_min < along_max invariant
    # actually re-runs -- an ill-formed translate must fail HERE, loudly, not
    # ride out as a silently-broken face line.
    return AsMeasuredFaceLineV1.model_validate(
        {**face.model_dump(mode="json"), action.field: updated})


def _group_along_extent(faces: list[AsMeasuredFaceLineV1]) -> tuple[int, int]:
    """The union along-extent of a wall's one-sided face group (⛔ a wall's
    ``face_line_ids_lo`` / ``_hi`` are plural: a face can be several collinear
    fragments).  Mirrors how ``_pair_face_lines_into_walls`` derived the
    group's own extent in the first place -- this is the SAME formula, run
    again, ⛔ not a second opinion about what a group's extent means."""
    return min(f.along_min for f in faces), max(f.along_max for f in faces)


def _group_const_of(face: AsMeasuredFaceLineV1) -> int:
    """The group coordinate a face line's OWN (possibly just-translated)
    ``const`` would land on.  ⛔ MUST be used instead of raw ``const`` here:
    comparing a wall's ``face_lo``/``face_hi`` against a member's raw
    ``const`` flags every PRE-EXISTING split-const group as a false
    positive -- MEASURED on real, untouched sm25-L_anchor data (handle
    ``140E``, ``const=159396``, its wall's ``face_lo=159400``, a legitimate
    ~0.4 mm split already named in ``face_groups_with_a_split_const``, with
    ZERO revisions applied).  Reproducing the SAME 1 mm rounding the group
    coordinate was built from means an untouched face line always agrees,
    and disagreement means a translate moved the const far enough to change
    which millimetre bucket it falls in -- the thing this gate exists to
    catch.

    ⚠️ F-140 (GLM N-a, MEASURED): this gate's real resolution is NOT a
    constant 0.5 mm -- it is "how far the line's current ``const`` sits from
    the nearest millimetre-grid boundary", which is POSITION-DEPENDENT:
    [0.1, 0.9+] mm on real data (a line sitting exactly at a group's centre
    tolerates up to ~0.5 mm either way; a line already 0.4 mm off-centre
    (a legitimate split-const member) tolerates as little as 0.1 mm moving
    further off-centre before it crosses into the next millimetre).  This is
    the correct, EXPECTED consequence of "the check must mirror the
    producer's own rounding" (D3 quantises to 1 mm; a check built on the same
    rounding inherits the same grid) -- ⛔ it is not a bug to smooth over with
    a bigger or smaller single number.  ``test_f140_*`` below pins BOTH sides
    of this real, position-dependent boundary rather than one arbitrary delta.

    ⭐ F-140 (②-1b-S R5, GLM N-a): the rounding precision below reads
    ``_denominator.GROUP_QUANT`` as a LIVE MODULE ATTRIBUTE (⛔ not a
    module-level copy taken once at import time -- ``from ... import
    GROUP_QUANT`` would bind an independent name and go silently stale the
    instant the producer's own constant changed, which is EXACTLY the
    failure GLM measured: mutating ``denominator.GROUP_QUANT`` in isolation
    left this gate reporting SUCCESS on real sm25 data with zero revisions).
    Reading the attribute through the module object means there is
    STRUCTURALLY only one definition -- ``test_f140_the_grouping_constant_
    tracks_the_live_module_attribute`` proves the coupling is live by
    monkeypatching the producer's constant and observing this function's OWN
    output move with it, not just asserting the two happen to be equal now.
    """
    metres = face.const / UNITS_PER_METRE
    return round(round(metres, _denominator.GROUP_QUANT) * UNITS_PER_METRE)


def _verify_walls_still_match_their_face_lines(view: AsMeasuredViewV1) -> None:
    """⭐⭐ F-137 (②-1b-R B-1, ⛔ BLOCKING): a wall's own self-reported
    ``face_lo`` / ``face_hi`` / ``thickness`` / ``along_min`` / ``along_max``
    must still equal what its NAMED face lines actually say, after any
    ``translate`` has been applied to this view.

    ⛔ NOT "re-run pairing": pairing (which two lines are one wall) is
    ``denominator``'s D1-D5, decided once when ``as_measured`` was built
    (as_measured.py's own docstring, §一/§⭐⭐).  Re-pairing here would move
    that decision into the derivation step, change wall ids, and blow up
    every dependency closure that names a wall by id.  This is a narrower,
    purely NUMERIC claim: the numbers a wall carries about ITSELF must not
    silently disagree with the objects it already names by handle.

    This is precisely the check ``AsMeasuredWallV1``'s own docstring already
    promises ("stored as the INTEGER DIFFERENCE OF THE TWO STORED FACES, so
    'recompute it from the two faces and compare bit-for-bit' is a real
    check") -- MEASURED (GLM cross-review, F-137/A6) to have had no code
    doing it anywhere in the repo before this function existed.  A translate
    on ``const`` breaks ``face_lo``/``face_hi``/``thickness``; a translate on
    ``along_min``/``along_max`` breaks the wall's own along-extent -- both
    are checked here, not just the one the first-found defect happened to hit.
    """
    by_id = {face.id: face for face in view.face_lines}
    for wall in view.walls:
        lo_faces = [by_id[h] for h in wall.face_line_ids_lo]
        hi_faces = [by_id[h] for h in wall.face_line_ids_hi]
        lo_consts = {_group_const_of(f) for f in lo_faces}
        hi_consts = {_group_const_of(f) for f in hi_faces}
        if lo_consts != {wall.face_lo}:
            raise AsSignedReproductionError(
                f"as_signed_wall_face_lo_disagrees_with_its_face_lines: wall {wall.id} "
                f"reports face_lo={wall.face_lo} but face_line_ids_lo="
                f"{sorted(wall.face_line_ids_lo)} actually sit at const={sorted(lo_consts)}")
        if hi_consts != {wall.face_hi}:
            raise AsSignedReproductionError(
                f"as_signed_wall_face_hi_disagrees_with_its_face_lines: wall {wall.id} "
                f"reports face_hi={wall.face_hi} but face_line_ids_hi="
                f"{sorted(wall.face_line_ids_hi)} actually sit at const={sorted(hi_consts)}")
        if wall.thickness != wall.face_hi - wall.face_lo:
            raise AsSignedReproductionError(
                f"as_signed_wall_thickness_disagrees_with_its_faces: wall {wall.id} "
                f"reports thickness={wall.thickness} but face_hi-face_lo="
                f"{wall.face_hi - wall.face_lo}")
        lo_min, lo_max = _group_along_extent(lo_faces)
        hi_min, hi_max = _group_along_extent(hi_faces)
        expected_along_min = max(lo_min, hi_min)
        expected_along_max = min(lo_max, hi_max)
        if wall.along_min != expected_along_min or wall.along_max != expected_along_max:
            raise AsSignedReproductionError(
                f"as_signed_wall_along_extent_disagrees_with_its_face_lines: wall {wall.id} "
                f"reports along=[{wall.along_min},{wall.along_max}] but its face lines' "
                f"overlap is [{expected_along_min},{expected_along_max}]")


def _refresh_split_const_groups(view: AsMeasuredViewV1) -> AsMeasuredViewV1:
    """⭐ F-142 (②-1b-S R5, GLM complementary): a ``face_groups_with_a_split_
    const`` entry names its member handles' ``const`` values AT AS-MEASURED
    TIME.  MEASURED (GLM): translating 140E by -1 unit (0.1 mm, still inside
    its own millimetre group -- a legal, F-137-approved move) leaves the
    registry entry's ``member_consts`` reading the OLD value (159396) while
    the actual face line now reads 159395 -- the diagnostic and the geometry
    it describes have gone out of sync.

    ⛔ NOT a second grouping decision: this does not re-run D3 (which handles
    belong to which group is UNCHANGED, since a translate never moves a
    handle across ``face_line_ids_lo``/``_hi`` membership -- that would be a
    different kind of edit entirely).  It only RE-READS the same named
    handles' CURRENT ``const`` off the already-updated ``face_lines``, and
    drops the entry outright if the translate happened to move every member
    back onto the group coordinate (the entry would otherwise claim a split
    that no longer exists -- an honest reading of "no longer split" beats a
    silently-stale "still split").
    """
    r = view.converter_readouts
    if not r.face_groups_with_a_split_const:
        return view
    by_id = {f.id: f for f in view.face_lines}
    refreshed: list[dict] = []
    for group in r.face_groups_with_a_split_const:
        current = sorted({by_id[h].const for h in group["handles"] if h in by_id})
        if any(const != group["group_const"] for const in current):
            refreshed.append({**group, "member_consts": current})
        # else: the translate moved every member back onto the group
        # coordinate -- this group is no longer split, so it is dropped
        # rather than carried forward with a now-false claim.
    if refreshed == r.face_groups_with_a_split_const:
        return view          # nothing moved -- ⛔ avoid a spurious rebuild
    return AsMeasuredViewV1.model_validate({
        **view.model_dump(mode="json"),
        "converter_readouts": {**r.model_dump(mode="json"),
                               "face_groups_with_a_split_const": refreshed}})


def derive_as_signed(as_measured: AsMeasuredV1, revisions: RevisionsLedgerV1) -> AsSignedV1:
    """⭐ THE mechanical derivation (ledger §六).  Pure: no file I/O.

    Only ``verdict == "drawing_error"`` records are applied -- ``as_designed``
    / ``producer_defect`` / ``unsigned`` never touch geometry (ledger §三's
    own three-way split).  An unsigned record cannot even carry an ``action``
    (:class:`RevisionV1`'s own validator), so this loop needs no defensive
    check for that case to be correct -- it is correct because there is
    nothing on an unsigned record it COULD apply.
    """
    if revisions.as_measured_content_sha256 != content_sha256(as_measured):
        raise AsSignedReproductionError(
            "as_signed_revisions_do_not_target_this_as_measured: "
            f"ledger names {revisions.as_measured_content_sha256}, "
            f"got {content_sha256(as_measured)}")

    by_view: dict[str, AsMeasuredViewV1] = {v.view_id: v for v in as_measured.views}
    touched_views: set[str] = set()
    for revision in revisions.revisions:
        if revision.verdict != "drawing_error":
            continue
        target, action = revision.target, revision.action
        assert action is not None            # guaranteed by RevisionV1's validator
        view = by_view.get(target.view_id)
        if view is None:
            raise AsSignedReproductionError(
                f"as_signed_unknown_view:{target.view_id} (revision {revision.id})")
        new_faces: list[AsMeasuredFaceLineV1] = []
        hit = False
        for face in view.face_lines:
            if face.id == target.handle:
                hit = True
                face = _apply_translate(face, action)
            new_faces.append(face)
        if not hit:
            raise AsSignedReproductionError(
                f"as_signed_translate_target_not_found:{target.handle} "
                f"(revision {revision.id}, view {target.view_id})")
        updated_view = AsMeasuredViewV1.model_validate(
            {**view.model_dump(mode="json"),
             "face_lines": [f.model_dump(mode="json") for f in new_faces]})
        by_view[target.view_id] = _refresh_split_const_groups(updated_view)
        touched_views.add(target.view_id)

    views = [
        (refresh_boundary_edges(by_view[v.view_id])
         if v.view_id in touched_views else by_view[v.view_id])
        for v in as_measured.views]   # preserve original order
    for view in views:
        _verify_walls_still_match_their_face_lines(view)
    return AsSignedV1(
        case=as_measured.case,
        source_dxf_label=as_measured.source_dxf_label,
        source_dxf_sha256=as_measured.source_dxf_sha256,
        request_sha256=as_measured.request_sha256,
        converter_implementation_fingerprint=as_measured.converter_implementation_fingerprint,
        derivation=AsSignedDerivationKeyV1(
            as_measured_content_sha256=content_sha256(as_measured),
            revisions_content_sha256=revisions_content_sha256(revisions),
            deriver_version=CURRENT_DERIVER_VERSION),
        views=views)


_FACE_LINE_SCALAR_FIELDS: tuple[str, ...] = ("const", "along_min", "along_max")


def _index_face_lines_by_handle(doc: AsMeasuredV1) -> dict[str, tuple[str, AsMeasuredFaceLineV1]]:
    """⭐ F-141 (②-1b-S R5, GLM's N-7, "具体化"): a dict comprehension over
    ALL views silently lets a LATER view's entry overwrite an EARLIER one for
    the same handle -- ⛔ real sm25 data does not trigger this today (F1∩F2
    face-line-handle intersection measured empty), but the failure mode is
    real and dangerous: ``detect_translate_candidates`` would then compare
    against the WRONG view's line while still reporting the candidate's
    ``target.view_id`` as the intended (different) view -- "compares the
    wrong line, signs onto the right-looking view", the shape GLM's F-1
    (axis/layer) finding is the sibling of.  ⛔ Raising here, rather than
    silently keeping either the first or the last, is the only response that
    does not itself pick a winner for a data shape this function has no way
    to judge.
    """
    out: dict[str, tuple[str, AsMeasuredFaceLineV1]] = {}
    for view in doc.views:
        for face in view.face_lines:
            if face.id in out and out[face.id][0] != view.view_id:
                raise ValueError(
                    f"as_measured_face_line_handle_reused_across_views:{face.id} "
                    f"appears in both {out[face.id][0]!r} and {view.view_id!r} -- "
                    "detect_translate_candidates cannot know which view a "
                    "revision naming this handle targets")
            out[face.id] = (view.view_id, face)
    return out


def detect_translate_candidates(before: AsMeasuredV1, after: AsMeasuredV1,
                                handles: Iterable[str]) -> list[RevisionV1]:
    """⭐ R1's "机器算出的 target + finding + 候选 action" -- MACHINE-produced,
    never hand-typed verdicts (dispatch: "本单不许替用户判 verdict").

    Compares ``before`` (canonically ``as_measured``, i.e. the as-received
    drawing) against ``after`` (e.g. a same-shape document built off a later
    drawing) for each named DXF handle, and reports EXACTLY what a mechanical
    diff finds -- ⛔ never a guess:

    * present as a ``face_line`` on both sides, exactly ONE of ``const`` /
      ``along_min`` / ``along_max`` differs -> a well-formed ``translate``
      candidate (the only action kind ①'s dispatch implements);
    * present on only one side, or more than one field differs -> reported
      with ``candidate_action=None`` and a ``finding.detail`` that says
      which of those two happened -- an honest "this needs an action kind
      not yet implemented", not an approximate or wrong translate.

    Every returned record has ``verdict="unsigned"`` -- this function never
    judges, it only measures (guide precedent: "只有签字流程能写 revisions").
    """
    before_idx = _index_face_lines_by_handle(before)
    after_idx = _index_face_lines_by_handle(after)
    out: list[RevisionV1] = []
    for handle in sorted(set(handles)):
        left = before_idx.get(handle)
        right = after_idx.get(handle)
        if left is None and right is None:
            continue          # not a face line anywhere -- nothing this function can measure
        view_id = (left or right)[0]
        target = RevisionTargetV1(view_id=view_id, handle=handle)
        if left is None or right is None:
            out.append(RevisionV1(
                id=f"rev-{handle.lower()}", target=target,
                finding=RevisionFindingV1(
                    check="face_line_classification_changed",
                    detail=(
                        f"handle {handle} is "
                        f"{'absent from' if left is None else 'present in'} as_measured's "
                        f"face_lines and {'absent from' if right is None else 'present in'} "
                        "the comparison drawing's; not expressible as a translate of an "
                        "existing scalar field (e.g. a non-orthogonal stroke being "
                        "straightened into an axis-aligned face line) -- needs an action "
                        "kind ①'s '遇到再加' has not implemented yet")),
                verdict="unsigned"))
            continue
        _, before_face = left
        _, after_face = right
        # ⭐⭐ ②-1b-R (F-139/N-6, GLM ⭐ new-break finding): ``const`` on a
        # y-running line and ``const`` on an x-running line are DIFFERENT
        # physical coordinates (one is an x-intercept, the other a
        # y-intercept) -- comparing them as the same quantity is a TYPE
        # error, not a tolerance question.  MEASURED (GLM's probe): an
        # axis-swapped before/after pair with a numeric coincidence on
        # ``const`` was reported as a well-formed ``translate`` candidate,
        # with ``finding.detail`` never mentioning axis at all -- sign it and
        # ``as_signed`` ends up with a vertical line wearing a horizontal
        # line's coordinate, and every downstream gate (this module's
        # reproducibility gate included) stays green, because they all
        # verify the ARITHMETIC of the derivation, never the axis semantics
        # of what was compared.  ⛔ MUST be checked BEFORE the scalar diff --
        # comparing const/along_min/along_max across different axes is not
        # merely "no candidate", it is comparing the wrong quantities.
        # ⭐ F-141 (②-1b-S R5, GLM's F-1/N-6 "第7种同形输入·主"): the axis check
        # above stops comparing const/along across a coordinate-TYPE change,
        # but a LAYER change is the other half of the same sentence -- a
        # handle that used to be a WALL face line and is now, say, an
        # AXIS-GRID reference line is not "the same wall line, moved"; it is
        # not the same kind of entity at all, and a numeric coincidence on
        # const/along must not be signed as if it were.  MEASURED (GLM):
        # before ``layer`` was checked, a WALL->AXIS-GRID handle with a
        # coincidental small const delta was reported as a well-formed
        # ``translate`` candidate with no mention of the layer change
        # anywhere in ``finding.detail``.
        if before_face.axis != after_face.axis or before_face.layer != after_face.layer:
            changed = ([f"axis {before_face.axis!r}->{after_face.axis!r}"]
                      if before_face.axis != after_face.axis else []) + (
                      [f"layer {before_face.layer!r}->{after_face.layer!r}"]
                      if before_face.layer != after_face.layer else [])
            out.append(RevisionV1(
                id=f"rev-{handle.lower()}", target=target,
                finding=RevisionFindingV1(
                    check="face_line_identity_changed",
                    detail=(
                        f"handle {handle} changed identity between the two drawings "
                        f"({'; '.join(changed)}); const on one axis is a DIFFERENT "
                        "physical coordinate than const on the other, and a layer "
                        "change means this may no longer even be the same KIND of "
                        "line -- no scalar field of this face line can be compared "
                        "numerically across that change, so this is not expressible "
                        "as a translate; needs an action kind ①'s '遇到再加' has not "
                        "implemented yet")),
                verdict="unsigned"))
            continue
        diffs = [(field, getattr(after_face, field) - getattr(before_face, field))
                 for field in _FACE_LINE_SCALAR_FIELDS
                 if getattr(after_face, field) != getattr(before_face, field)]
        if len(diffs) == 1:
            field, delta = diffs[0]
            out.append(RevisionV1(
                id=f"rev-{handle.lower()}", target=target,
                finding=RevisionFindingV1(
                    check="face_line_field_changed", magnitude_0p1mm=abs(delta),
                    detail=f"{field} differs by {delta} (0.1mm units) between the two drawings"),
                candidate_action=TranslateActionV1(field=field, delta_0p1mm=delta),
                verdict="unsigned"))
        elif diffs:
            out.append(RevisionV1(
                id=f"rev-{handle.lower()}", target=target,
                finding=RevisionFindingV1(
                    check="face_line_multiple_fields_changed",
                    detail=f"{len(diffs)} fields differ ({diffs}); not a single-field translate"),
                verdict="unsigned"))
        # else: no field differs -- not a candidate at all, silently skipped
        # (⛔ this is "nothing to report", not "found and dismissed").
    return out


def verify_as_signed_reproduction(as_measured: AsMeasuredV1, revisions: RevisionsLedgerV1,
                                  as_signed: AsSignedV1) -> None:
    """⭐ THE reproducibility gate (ledger §六): raise, loudly and specifically,
    unless ``as_signed`` re-derives bit-for-bit from ``as_measured`` +
    ``revisions``.  ⛔ Never returns a verdict object that could be ignored --
    this is a gate, not a report."""
    fresh = derive_as_signed(as_measured, revisions)
    fresh_bytes = canonical_as_signed_bytes(fresh)
    given_bytes = canonical_as_signed_bytes(as_signed)
    if fresh_bytes != given_bytes:
        raise AsSignedReproductionError(
            "as_signed_does_not_reproduce_from_as_measured_plus_revisions: "
            f"recomputed content_sha256={hashlib.sha256(fresh_bytes).hexdigest()} != "
            f"given content_sha256={hashlib.sha256(given_bytes).hexdigest()}")
