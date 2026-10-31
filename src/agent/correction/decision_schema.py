"""②-2 module 5: the decision packet and the decision-response schemas.

Dispatch: the 2026-09-01 module 5+6 dispatch; authority: the 2026-08-30
evidence-contract design §6.1 / §6.2 (cross-family approved).  Module 4's
own docstring reserves exactly this slice: "cost vectors, packets and
response schemas (module 5)".

Shapes
------
``CorrectionDecisionPacketV1`` (code → model) and
``CorrectionDecisionResponseV1`` (model → code) carry the field lists of
design §6.1 / §6.2 verbatim; the open-item / auto-action / wall-summary
shapes are module 4's own types, re-used by import, never re-declared.
The module-4 seam ``FixedDecisionV1`` documents ("the response SCHEMA is
module 5's; this type is deliberately the minimal binding") is exactly
the ``select_candidate`` action below.

Two sub-structures §6.1 names without giving fields (``entity_to_source_
refs[]``, ``consistency_results[]``) get the smallest shapes that carry
the named meaning; the choice is recorded in the execution report, not
silently frozen here.

``provisional_geometry`` is the provisional compilation's content hash
(module 4's ``WallCompilationV1.content_sha256``), ⛔ not an embedded copy:
an embedded compilation would duplicate ``open_items`` / ``auto_actions``
that §6.1 also lists at the top level, and two copies can disagree.  One
hash anchor plus the flat lists leaves exactly one copy of everything.

Coordinates are structurally absent from the RESPONSE side
---------------------------------------------------------
The response type tree contains NO numeric field of any kind: item
decisions are (id, enum, id, reason) tuples, findings reference packet
entities by identifier and carry a discriminated ``requested_effect``
whose five kinds are a CLOSED value domain (design §6.2's table verbatim),
each with its own strict schema.  This is not a naming discipline: a
field holding a number cannot be constructed on this side, so "the model
outputs coordinates" fails at the type layer, whatever the field is
called.  ``extra="forbid"`` everywhere closes the last gap -- an ``x``
the schema never listed is rejected before any executor code runs, not
by a lexical scan for suspicious names.

The STRING half of that proof (module 7 v3 rework, B-2)
-------------------------------------------------------
The v2 cross-review pushed an integer coordinate -- ``"wall endpoint is
at (12, 34)"`` -- through ``reason_code``: the type had no numeric field,
but free text is itself a numeric CHANNEL, and the runtime regex guarding
it only knew decimal pairs and lowercase ``x=/y=``.  The rework ruling:
⛔ do NOT complete the regex (a lexical guard over unbounded free text is
never completable -- the same seam six times in this repo); move the
defence to the TYPE.  So every string field the model may MINT itself is
now a ``CodeToken`` (SCREAMING_SNAKE_CASE over ``[A-Z_]`` ONLY -- letters
and underscores, ⛔ no digits at all):

* ``ItemDecisionV1.reason_code`` and the reperception effect's
  ``reason_code`` (the B-2 channel itself),
* ``FindingV1.finding_id`` / ``kind`` / ``rationale`` (``finding_id`` is
  model-minted -- no packet closure exists to bound it -- and prose
  ``rationale`` / free ``kind`` were unchecked channels riding into the
  outcome's ``pending_findings``).

The grammar is a WHITELIST of legal form, so it is complete by
construction: the charset has no digit (kills every numeral, any base,
any notation -- including notations nobody has tried yet, which is the
whole point), no lowercase letter (kills ``x=``/``y=``), and no
whitespace, comma, semicolon, parenthesis, bracket, equals sign or
decimal point (kills every pair / axis / tuple notation); a token must
start with a letter.  Digits are banned outright rather than
pattern-matched because a token grammar that admits numerals
(``X_12_Y_34``) reopens the pair-in-disguise channel the rework exists
to close -- and nothing downstream parses a numeral out of these five
fields (entity references ride their own id channels), so the ban costs
no consumer anything.  With all five minted fields closed, NO free-text
field remains on the response side: there is nothing left for a
coordinate to ride in.

The strings the model ECHOES (``item_id``, ``candidate_id``, entity id
tuples, ``source_refs``) stay plain ``str`` on purpose: their value
domain is the PACKET's own index, minted by code, and membership is
enforced by the executor (``UNKNOWN_RESPONSE_ITEM`` /
``UNKNOWN_RESPONSE_CANDIDATE`` / ``FINDING_ENTITY_NOT_IN_PACKET`` /
``FINDING_REF_NOT_IN_PACKET``) -- a typed vocabulary supplied by the
packet, which is a different author than the response.  A coordinate
string is not a packet id and dies there; ``packet_hash`` is a ``Hex64``
pattern.  So EVERY string channel on the response side is now either a
``CodeToken``, an executor-checked packet echo, or a hex pattern -- none
is "the rest, as before".

``assert_response_payload_carries_no_coordinates`` (below) keeps its
walk but is DEMOTED to the beat's pre-construction diagnostic: a hit
names the offending JSON path so the model seat's format-retry gets a
pointed message instead of a bare schema error.  The DEFENCE is the
closed type; the walk is advisory and is not asked to be complete.

The entity/reference closure ("all entities and refs must already be in
the packet", §6.2) is cross-object, so it cannot live in a schema: the
executor owns it.  Acceptance 3's fabricated-candidate fixture proves it
on the item path; the entity-index check proves it on the finding path.
"""
from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from src.agent.correction.evidence_contract import ArtifactPointerV1
from src.agent.correction.wall_compiler import (
    AutoActionV1,
    OpenItemV1,
    ResolvedWallV1,
)
from src.agent.correction.window_sources import Hex64

_CFG = ConfigDict(extra="forbid", strict=True)


# ── module 7 v3 (rework B-2): the code-token channel ------------------------- #
#: The ONLY string shape the model may MINT itself on the response side.
#: SCREAMING_SNAKE_CASE over [A-Z_] — letters and underscores ONLY, 1..96
#: chars, starting with a letter.  ⭐ This is a whitelist of legal FORM,
#: not a detector of coordinate forms (see the module docstring): the
#: charset carries no digit, no lowercase letter, no whitespace and no
#: separator, so every coordinate notation — pair, axis assignment,
#: bracketed tuple, pair-in-underscores disguise, in any base — is
#: UNREPRESENTABLE here, including notations nobody has tried yet.  The
#: defence is complete by construction and cannot fall behind the way
#: the v2 runtime regex did (lexical guards over unbounded free text are
#: never completable; this is not free text).
CodeToken = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Z][A-Z_]*$", min_length=1, max_length=96),
]


# ── packet sub-structures §6.1 names without fields ------------------------- #
class EntitySourceRefsV1(BaseModel):
    """One packet entity, its kind and the frozen references that ground it.

    Minimal carrier of the §6.1 ``entity_to_source_refs[]`` slot: the
    model looks an entity up here to cite evidence, the executor checks
    finding citations against exactly this index.

    ``entity_kind`` is a CLOSED domain (rework B-4): the packet's entity
    table does not just list ids, it says WHAT each id is.  A ``review_
    opening_host`` finding must cite an entity whose kind really is
    ``opening``, and every candidate wall id one whose kind really is
    ``wall`` -- membership alone let a real wall id impersonate an
    opening, which is exactly the bypass the cross-review caught.
    """

    model_config = _CFG

    entity_id: str
    entity_kind: Literal[
        "wall", "opening", "open_item", "auto_action_scope"
    ]
    source_refs: tuple[ArtifactPointerV1, ...] = ()


class ConsistencyResultV1(BaseModel):
    """One deterministic check over the provisional compilation (§6.3
    step 4), as it travels on the NEXT packet.  ``check`` is a closed
    enum: growing the check face means growing this Literal, which is a
    visible diff -- an unnamed free-text check name cannot appear."""

    model_config = _CFG

    check: Literal["collinear_wall_overlap", "unshared_tail_coverage"]
    passed: bool
    detail: str


# ── §6.1 first beat: the packet (code → model) ------------------------------- #
class CorrectionDecisionPacketV1(BaseModel):
    """Everything the model may see for one adjudication round.  Hashed
    canonically: ``packet_hash`` is ``canonical_sha256`` over the packet
    with the hash field removed, so a response binding this hash binds
    every field of this packet (module 2's discipline, applied here)."""

    model_config = _CFG

    packet_hash: Hex64
    input_bundle_hash: Hex64
    solver_revision: str
    round_index: int
    previous_decision_hashes: tuple[Hex64, ...] = ()
    provisional_geometry: Hex64
    provisional_wall_summaries: tuple[ResolvedWallV1, ...] = ()
    entity_to_source_refs: tuple[EntitySourceRefsV1, ...] = ()
    auto_actions: tuple[AutoActionV1, ...] = ()
    consistency_results: tuple[ConsistencyResultV1, ...] = ()
    open_items: tuple[OpenItemV1, ...] = ()

    @model_validator(mode="after")
    def _round_agrees_with_history(self) -> "CorrectionDecisionPacketV1":
        if self.round_index < 0:
            raise ValueError("round_index counts from 0")
        if len(self.previous_decision_hashes) != self.round_index:
            raise ValueError(
                f"round_index={self.round_index} but "
                f"{len(self.previous_decision_hashes)} previous decision "
                "hash(es): exactly one decision hash per elapsed round"
            )
        return self


# ── §6.2 second beat: the response (model → code) ---------------------------- #
class ItemDecisionV1(BaseModel):
    """One decision on one packet item.  ``candidate_id`` rides along
    ONLY on ``select_candidate`` -- the other two actions decide WITHOUT
    a candidate (reject every listed one / hand the item back to reading),
    and a candidate id arriving with them is a malformed response.
    ``reason_code`` is a ``CodeToken`` (v3, B-2): the model MINTS it, so
    it is the one field on this object whose shape the type itself must
    bound -- ids are echoed (executor-checked against the packet)."""

    model_config = _CFG

    item_id: str
    action: Literal["select_candidate", "reject_all", "request_reperception"]
    candidate_id: str | None = None
    reason_code: CodeToken

    @model_validator(mode="after")
    def _candidate_only_with_select(self) -> "ItemDecisionV1":
        if self.action == "select_candidate":
            if self.candidate_id is None:
                raise ValueError(
                    "select_candidate must name the chosen candidate"
                )
        elif self.candidate_id is not None:
            raise ValueError(
                f"{self.action} carries candidate_id={self.candidate_id!r}: "
                "only select_candidate may name a candidate"
            )
        return self


# ── the five requested_effect kinds (design §6.2's table, verbatim) ---------- #
class ReviewAlignmentEffectV1(BaseModel):
    """``review_alignment``: subject/reference entity ids from the packet;
    relation restricted to collinear/parallel/perpendicular.  The model
    names WHO to align and WHICH relation -- never a target line."""

    model_config = _CFG

    kind: Literal["review_alignment"]
    subject_entity_ids: tuple[str, ...]
    reference_entity_ids: tuple[str, ...]
    relation: Literal["collinear", "parallel", "perpendicular"]


class ReviewSegmentationEffectV1(BaseModel):
    """``review_segmentation``: packet subject ids; relation restricted to
    split_required/merge_review_required.  Candidates are enumerated by
    the compiler from source intervals, not described here."""

    model_config = _CFG

    kind: Literal["review_segmentation"]
    subject_entity_ids: tuple[str, ...]
    relation: Literal["split_required", "merge_review_required"]


class ReviewTopologyRelationEffectV1(BaseModel):
    """``review_topology_relation``: packet subject/reference ids; relation
    restricted to connect/separate."""

    model_config = _CFG

    kind: Literal["review_topology_relation"]
    subject_entity_ids: tuple[str, ...]
    reference_entity_ids: tuple[str, ...]
    relation: Literal["connect", "separate"]


class ReviewOpeningHostEffectV1(BaseModel):
    """``review_opening_host``: one packet opening id plus the candidate
    wall ids to rehost among.  The finding itself changes no host."""

    model_config = _CFG

    kind: Literal["review_opening_host"]
    opening_entity_id: str
    candidate_wall_entity_ids: tuple[str, ...]


class RequestWallReperceptionEffectV1(BaseModel):
    """``request_wall_reperception``: packet wall/item ids, the source
    refs being re-read and a reason code (design §7.3's wall-level
    directed request).  Generates no geometry."""

    model_config = _CFG

    kind: Literal["request_wall_reperception"]
    wall_item_entity_ids: tuple[str, ...]
    source_refs: tuple[ArtifactPointerV1, ...] = ()
    reason_code: CodeToken


RequestedEffectV1 = Annotated[
    Union[
        ReviewAlignmentEffectV1,
        ReviewSegmentationEffectV1,
        ReviewTopologyRelationEffectV1,
        ReviewOpeningHostEffectV1,
        RequestWallReperceptionEffectV1,
    ],
    Field(discriminator="kind"),
]


class FindingV1(BaseModel):
    """One whole-building finding.  It is a REQUEST for the next round's
    candidate generation, ⛔ not an executable instruction: the executor
    verifies it against the packet's entity/ref index and carries it to
    the next round; the bounded candidates it asks for are generated by
    code (compiler / opening resolver), never described here (§6.2).
    ``finding_id`` / ``kind`` / ``rationale`` are ``CodeToken`` (v3, B-2):
    all three are model-MINTED with no packet closure to bound them, and
    they ride into the outcome's ``pending_findings`` -- so their shape
    is bounded by the TYPE (no digit, no separator, no lowercase), which
    is what makes a coordinate unrepresentable in prose channels too."""

    model_config = _CFG

    finding_id: CodeToken
    kind: CodeToken
    affected_entity_ids: tuple[str, ...] = ()
    source_refs: tuple[ArtifactPointerV1, ...] = ()
    requested_effect: RequestedEffectV1
    rationale: CodeToken


class WholeBuildingReviewV1(BaseModel):
    """The model's overall verdict on the SAME provisional geometry the
    packet hashed (§6.3's success condition 3 binds through packet_hash).
    ``findings`` may name problems the code never listed -- that channel
    exists precisely so the model never has to smuggle them into item
    decisions."""

    model_config = _CFG

    verdict: Literal["accept", "findings"]
    findings: tuple[FindingV1, ...] = ()

    @model_validator(mode="after")
    def _verdict_agrees_with_findings(self) -> "WholeBuildingReviewV1":
        if self.verdict == "accept" and self.findings:
            raise ValueError(
                "verdict=accept carries findings: acceptance is unconditioned"
            )
        return self


class CorrectionDecisionResponseV1(BaseModel):
    """The model's whole answer for one packet.  ⭐ There is no coordinate
    anywhere on this type or anything reachable from it -- see the module
    docstring; the structural proof is a test that walks this type's
    field tree and fails on ANY numeric field."""

    model_config = _CFG

    packet_hash: Hex64
    item_decisions: tuple[ItemDecisionV1, ...] = ()
    whole_building_review: WholeBuildingReviewV1


# ── module 7: the beat's pre-construction diagnostic (v3: DEMOTED) --------- #
#: A decimal coordinate PAIR (or an x=/y= axis assignment) inside ONE string
#: leaf.  ⭐ v3 DEMOTION (rework B-2): since every model-minted string field
#: became a ``CodeToken``, this walk is NOT the defence any more — the closed
#: type is.  It stays as the beat's DIAGNOSTIC: a hit names the JSON path so
#: the model seat's format-retry gets a pointed message instead of a bare
#: pattern error.  ⛔ It is deliberately NOT being "completed" to catch more
#: coordinate forms (integer pairs, brackets, uppercase axes all pass it and
#: die at construction instead) — completing it is the road the rework
#: ruling closed.
_COORDINATE_PAIR_IN_STRING_RE = re.compile(
    r"[xy]=\s*-?\d"                    # an axis assignment, e.g. x=12.3
    r"|-?\d+\.\d+\s*[,;]\s*-?\d+\.\d+"  # two decimals: 12.34, 56.78
)


class CoordinateSmuggledInResponse(ValueError):
    """A model response payload carries a coordinate the type layer cannot
    see (module 7's model seat).  Raised by
    ``assert_response_payload_carries_no_coordinates`` BEFORE the payload
    is constructed into ``CorrectionDecisionResponseV1``."""


def assert_response_payload_carries_no_coordinates(payload: Any) -> None:
    """Pre-construction diagnostic for the beat (module 7; v3 demoted).

    The DEFENCE against coordinates in the response is the closed type:
    no numeric field anywhere on the tree, ``extra="forbid"`` everywhere,
    and — since the v3 rework — every model-minted string field is a
    ``CodeToken``, so no free-text channel exists at all.  This walk runs
    on the RAW parsed payload (pre-construction) purely to give the model
    seat's format-retry a POINTED message instead of a bare schema error:

    * any numeric leaf (``int``/``float``/``bool`` — the response type has
      no numeric or boolean field at all, so a number here is an extra
      channel or a mis-placed field);
    * any string leaf matching a decimal coordinate PAIR or an x=/y= axis
      assignment (see the regex above — one dimension in prose is fine).

    ⭐ Keep this a pure payload check: it never constructs, never reads the
    packet, and never decides anything about the geometry.  ⛔ Do not grow
    the regex: forms it misses (integer pairs, brackets, uppercase axes)
    are already unrepresentable at the type layer, and enumerating them
    here would rebuild the never-completable lexical guard the rework
    moved away from.
    """
    def _walk(node: Any, path: str) -> None:
        if isinstance(node, bool) or isinstance(node, (int, float)):
            raise CoordinateSmuggledInResponse(
                f"numeric leaf at {path or '<root>'}: {node!r} — the "
                "decision response type has no numeric field at all"
            )
        if isinstance(node, str):
            hit = _COORDINATE_PAIR_IN_STRING_RE.search(node)
            if hit:
                raise CoordinateSmuggledInResponse(
                    f"coordinate-like pair in string at {path}: {hit.group(0)!r} "
                    f"(full value {node!r}) — coordinates may not travel "
                    "inside response strings"
                )
            return
        if isinstance(node, dict):
            for key, value in node.items():
                _walk(value, f"{path}.{key}" if path else str(key))
            return
        if isinstance(node, (list, tuple)):
            for i, value in enumerate(node):
                _walk(value, f"{path}[{i}]")
            return
        # None / actual strings fell through above; anything else (a model
        # seat should never produce it) is left to the strict type layer.

    _walk(payload, "")


__all__ = [
    "CodeToken",
    "ConsistencyResultV1",
    "CoordinateSmuggledInResponse",
    "CorrectionDecisionPacketV1",
    "CorrectionDecisionResponseV1",
    "EntitySourceRefsV1",
    "FindingV1",
    "ItemDecisionV1",
    "RequestedEffectV1",
    "RequestWallReperceptionEffectV1",
    "ReviewAlignmentEffectV1",
    "ReviewOpeningHostEffectV1",
    "ReviewSegmentationEffectV1",
    "ReviewTopologyRelationEffectV1",
    "WholeBuildingReviewV1",
]
