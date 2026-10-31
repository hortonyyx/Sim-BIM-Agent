"""②-2 module 2: the correction-side evidence contract (2026-08-30).

TYPE LAYER ONLY.  This module defines the unified evidence bundle the
correction stage will consume -- source refs, the four positive wall-claim
kinds, the three face-line dispositions -- plus the validator for hard
invariants 1-8 of the approved design (§4.4).  It wires NOTHING: no adapter
lives here (module 3), no compiler (module 4), no pipeline change, and the
as-drawn disposition stays exactly where it was.

Authority: the approved ②-2 design (see the verdict document of 2026-08-30),
§3.2 / §3.3 / §4.1 / §4.2 / §4.3 / §4.4, with one deliberate override -- the
design's two-valued ``counterface_state`` was superseded by cross-review
finding N-1, so this module ships THREE values (``ink_present_unpromoted``
must carry a pixel-witness pointer).  ⛔ Deriving that third value from
today's prose products is module 3's job; a regex over free text is banned
everywhere (design §4.1).

Identity discipline (design §3.2)
---------------------------------
Every reference is ``ArtifactPointerV1`` / ``ObservationRefV1``: input_id +
contract + sha256 + json_pointer into FROZEN source bytes, never a copy of a
geometry value.  ``WallClaim`` models carry ⛔ no ``pos_m`` / ``edges_m`` /
``runs_m`` / ``spacing_m``-style value fields at all -- a test derives the
forbidden-name set mechanically from the producer's own types (module 1) and
walks every field of every claim kind.  ``spacing_m`` is not even carried as
a cached audit value: with no reader there is nothing to prove it unread.

The ``src:<sha256>`` locator vocabulary and its generation principle are
REUSED from ``correction.window_sources`` (one definition, not a second
provenance scheme).

Reference integrity (NF-4, folded into this dispatch as task 3)
---------------------------------------------------------------
The cross-family review of module 1 measured five "structurally legal but
semantically false" corruptions that ALL passed: a dangling ``pairs[].face_b``,
a dangling bucket key, two face lines sharing one id, an out-of-range
``opening_candidates[].gap_index``, and a dangling ``pair_candidates[].face_b``.
This module's validator must make the FIRST THREE loud, and does:

* dangling ``face_b`` -- the selected pair's hypothesis node no longer matches
  the claim's two refs (``PAIR_HYPOTHESIS_MISMATCH``), and a ref whose
  ``observation_id`` names a face that does not exist fails dereference
  (``OBSERVATION_ID_MISMATCH`` / ``UNKNOWN_INPUT_ID``);
* dangling bucket key -- a disposition outside the face-line domain of its
  source is ``DISPOSITION_REFERENCES_UNKNOWN_FACE``; a face with no
  disposition is ``FACE_WITHOUT_DISPOSITION`` (BOTH directions are checked --
  the legacy validator/checks union absorbed one direction silently);
* duplicate face ids -- building the per-source observation index raises
  ``DUPLICATE_OBSERVATION_ID``, which is what "resolves uniquely" (design
  §4.4 #1) means operationally.

The fourth (``gap_index`` out of range) is pure reference integrity -- an
index pointing past a face line's ``gaps`` list is a dangling reference, no
prose parsing involved -- so THIS module catches it too
(``OPENING_GAP_INDEX_OUT_OF_RANGE``).

The fifth (a dangling ``pair_candidates`` entry that no selected pair uses)
is ⛔ NOT caught here and is PINNED in the test file: the bundle references
only selected pairs (design §4.3 -- ``pair_candidates`` is the candidate
graph, not wall claims), so an unselected dangling candidate is invisible to
this layer.  It belongs to module 3 (the adapter walks candidates) and
module 4 (the compiler recomputes the candidate graph and must dereference
``face_b`` there).  ⛔ Do not leave this as a silent gap between documents.

Cross-review rework (2026-08-31): two structural invariants the first
review found missing, both intra-bundle (no adapter, no prose) --
---------------------------------------------------------------
F-1 payload closure: a ``present`` channel must actually carry payload
(``walls`` -> a wall claim or a disposition; ``plan_openings`` -> an
opening claim); zero payload is legal ONLY with an explicit
``zero_payload_channel`` debt -- an honest "wired, produced nothing this
run", ⛔ never a silent empty run.  The three channels with no payload
member on this bundle can never witness a present, so for them the debt is
the only legal companion to a ``present`` row.
F-2 single sourcing: identity IS ``input_id`` (design §3.2), so every ref
on ONE claim must point into the SAME frozen source.  A wall whose two
faces live in two different products is structurally legal (``F01``/
``F02``-style ids exist in every floor's product, so hypothesis and
candidate matches still succeed) but physically impossible.

Rework-2 (2026-08-31), B-1 / B-2 -- same-family carrier swaps F-1 missed
-----------------------------------------------------------------------
B-1 source closure: F-1 asked whether a present channel has payload AT
ALL; it never asked WHERE the payload came from.  Declaring the walls
channel's source as one frozen product while the claims all live in
another therefore passed.  Now ``channel_status`` is a routing table that
must close in BOTH directions: payload sources ⊆ declared sources, and a
declared source with no payload this run needs a ``zero_payload_channel``
debt scoped to it (``affected_refs``); a channel-granularity debt only
covers the whole-channel zero run, where its statement is actually true.
B-2: ``dimensions`` / ``room_roles`` / ``elevation_openings`` have NO
payload member on this bundle, so no debt can make a ``present`` true for
them -- their only legal shape is ``absent`` + ``missing_channel``, and a
``zero_payload_channel`` debt naming one of them is refused outright.
(2026-09-03, B3: ``elevation_openings`` and the new ``floor_levels`` GAINED
payload members -- ``elevation_opening_claims`` / ``floor_level_claims`` --
so B-2's "no debt can make a present true" now covers only
``dimensions`` / ``room_roles``.)

Rework-3 (2026-09-01) -- ``absent`` while the payload rides along
-----------------------------------------------------------------
Rounds 1 and 2 both asked their question only of a ``present`` channel:
each opened with ``if status.state != "present": continue``, so declaring
``absent`` made both of them step aside while two wall claims, four
dispositions and an opening claim stayed in the bundle.  The gates were
never wrong about what they computed -- ``state`` was simply one more
carrier that could be swapped
([[gate-measures-right-but-carrier-gets-swapped]]).

The fix is ⛔ not a mirrored ``if state == "absent"`` branch (that is round
four waiting for the next carrier).  ``CHANNEL_PAYLOAD_MEMBERS`` makes
"channel -> payload member" one explicit table;
``_assert_channel_payload_closure`` walks the CHANNEL DOMAIN, measures what
the bundle carries for each channel, and requires the declaration to be
that measurement's name in BOTH directions.  ``state`` is therefore a
reconciliation result, not an independent field, and the row's very
existence is pinned too (``CHANNEL_STATUS_MISSING``) -- otherwise the same
payload leaves through a deleted row instead of a flipped value.

⭐ One judgement call is load-bearing and is stated here so it can be
attacked: a ``face_disposition`` row is the SOURCE PRODUCT's face-line
ledger, mandated for every as-drawn face line by invariant 2 whatever the
walls leg produced, so only its ``claimed_wall`` rows witness a walls
``present``.  A product whose every face line is ``non_wall`` is an honest
walls-``absent`` run with a full ledger -- that is what module 3's adapter
emits (it declares walls present only when a positive claim exists) and
what module 4's compiler consumes.  The SOURCE closure (B-1) keeps its
wider reach over every ledger row: a ``non_wall`` row from an undeclared
product is still payload from the wrong place.
"""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from src.agent.correction.window_sources import (
    Hex64,
    StableId,
    canonical_sha256,
    source_locator,
)
from src.agent.reading.vector_contract import (
    CONTRACT_AS_DRAWN_ELEVATION_V0,
    CONTRACT_AS_DRAWN_PLAN,
    CONTRACT_READING_VIEW_LEGACY,
    CONTRACT_UNKNOWN,
    classify_vector_json,
)

BUNDLE_SCHEMA_VERSION = "correction_evidence_bundle_v1"
#: The source contracts this layer knows how to dereference.  ⛔ Not a
#: registry: registration is ``reading.vector_contract``'s business (module 7).
SOURCE_CONTRACT_AS_DRAWN = CONTRACT_AS_DRAWN_PLAN
SOURCE_CONTRACT_LEGACY = CONTRACT_READING_VIEW_LEGACY
#: 2026-09-03 (B3): the as-drawn elevation product joined the dereferenceable
#: set -- its ``openings[].z_range_m`` and the floor levels carried by its
#: ``structure_lines`` became referenced evidence (claims below).
SOURCE_CONTRACT_AS_DRAWN_ELEVATION = CONTRACT_AS_DRAWN_ELEVATION_V0

SourceLocatorStr = Annotated[
    str, StringConstraints(pattern=r"^src:[0-9a-f]{64}$")
]
JsonPointerStr = Annotated[
    str, StringConstraints(pattern=r"^(/([^/~]|~0|~1)*)*$")
]

_CFG = ConfigDict(extra="forbid", strict=True)


class EvidenceContractError(ValueError):
    """A loud input-integrity rejection, with a stable ``code``.

    Everything this validator rejects is an input-integrity fault (design
    §6.1's table: ref / hash / completeness / exclusivity failures are "stop",
    never "let the model fix the schema"), so unlike
    ``window_sources.WindowResolverInputError`` there is no second category
    here -- no field of this module ever parses a model's output.
    """

    def __init__(self, code: str, context: dict | None = None):
        self.code = code
        self.context = dict(context or {})
        super().__init__(f"{code}: {self.context}")


# ── §3.2: references ──────────────────────────────────────────────────────── #
class ArtifactPointerV1(BaseModel):
    """A pointer into one frozen source artifact.  ⭐ Identity is
    ``input_id`` (a view-manifest slot), ⛔ never a file name."""

    model_config = _CFG

    input_id: StableId
    source_contract_id: StableId
    source_output_sha256: Hex64
    json_pointer: JsonPointerStr


class ObservationRefV1(ArtifactPointerV1):
    """An ``ArtifactPointerV1`` that names one observation inside the source.

    ``pixel_witness_pointers`` are json pointers into the SAME source bytes
    (e.g. at ``support_cols_px`` / ``runs_px`` / ``gaps``); they carry ⛔ no
    values.  ``native_handle`` may exist only for future sources that really
    have one -- an image-derived product must not forge one.
    """

    model_config = _CFG

    observation_id: StableId
    source_locator: SourceLocatorStr
    pixel_witness_pointers: tuple[str, ...] = ()
    native_handle: StableId | None = None
    evidence_resolution: Literal[
        "pixel_backed", "vector_only", "native_handle_backed"
    ]

    @model_validator(mode="after")
    def _identity_and_resolution_agree(self) -> "ObservationRefV1":
        expected = source_locator(
            input_id=self.input_id,
            observation_id=self.observation_id,
            output_sha256=self.source_output_sha256,
        )
        if self.source_locator != expected:
            raise ValueError(
                "source_locator does not match the canonical identity "
                f"(input_id, observation_id, source_output_sha256): expected "
                f"{expected}, got {self.source_locator}"
            )
        if self.evidence_resolution == "native_handle_backed":
            if self.native_handle is None:
                raise ValueError(
                    "native_handle_backed requires a native_handle; an "
                    "image-derived product must not claim one"
                )
        elif self.native_handle is not None:
            raise ValueError(
                "native_handle is only legal with "
                "evidence_resolution='native_handle_backed'"
            )
        if self.evidence_resolution == "pixel_backed" and not self.pixel_witness_pointers:
            raise ValueError(
                "pixel_backed is a promise: at least one pixel witness "
                "pointer is required"
            )
        return self


# ── §4.1: the four positive wall-claim kinds ──────────────────────────────── #
class WallClaimBase(BaseModel):
    """Fields every positive wall claim shares (design §4.1).

    ⛔ No geometry values anywhere on a claim: ``claim_id`` is a canonical
    hash over the normalised source refs (never an array index), and every
    other field is a reference or an enumerated judgement.
    """

    model_config = _CFG

    claim_id: StableId
    hypothesis_ref: ArtifactPointerV1
    perception_source_ref: ArtifactPointerV1
    source_contract_id: StableId


class PairedFacesWallClaimV1(WallClaimBase):
    """Two observed face lines are the two faces of ONE wall, over the
    interval where they jointly cover.  ⛔ Asserts no centerline, no nominal
    thickness, no inner/outer, and ⛔ carries no ``spacing_m`` -- the compiler
    recomputes from both faces (design §4.1: cached values must be recomputed,
    and a cache nobody reads is a cache nobody can prove unread)."""

    kind: Literal["paired_faces"] = "paired_faces"
    face_a_ref: ObservationRefV1
    face_b_ref: ObservationRefV1
    pair_candidate_ref: ArtifactPointerV1


class SolidBandWallClaimV1(WallClaimBase):
    """One observed ink band IS a wall; its own two edges are the wall's two
    faces.  The face ref's pixel witnesses must cover ``support_cols_px``,
    ``edges_m`` and ``runs_px`` (checked by the validator, not by prose)."""

    kind: Literal["solid_band"] = "solid_band"
    band_face_ref: ObservationRefV1


class SingleFaceWallClaimV1(WallClaimBase):
    """One observed face of a wall whose other face never became part of this
    claim.  ⭐ ``counterface_state`` has THREE values (N-1 superseded the
    design's two):

    * ``not_in_observations`` -- no claim about the other side at all;
    * ``observed_unclaimed`` -- the counterface IS observed but was consumed
      by a ``non_wall`` / ``ambiguous`` disposition; its node pointer and its
      disposition status must travel along;
    * ``ink_present_unpromoted`` -- the counterface's ink is on the drawing
      but the reader never promoted it to a face line (sm25 2F ``L012``);
      ⚠️ today the only carrier of that fact is free prose, which this
      layer ⛔ never parses (module 3 derives it, or the producer starts
      emitting structure).  At least one witness pointer is REQUIRED so the
      claim cannot be made without pointing at evidence.

    ``original_reason`` is the producer's prose kept verbatim for audit --
    ⛔ it is never parsed into side / thickness / basis.
    """

    kind: Literal["single_face"] = "single_face"
    face_ref: ObservationRefV1
    original_reason: str
    counterface_state: Literal[
        "not_in_observations", "observed_unclaimed", "ink_present_unpromoted"
    ]
    counterface_observation_ref: ObservationRefV1 | None = None
    counterface_disposition_status: Literal["non_wall", "ambiguous"] | None = None
    counterface_witness_pointers: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _counterface_fields_match_state(self) -> "SingleFaceWallClaimV1":
        if self.counterface_state == "not_in_observations":
            if (
                self.counterface_observation_ref is not None
                or self.counterface_disposition_status is not None
                or self.counterface_witness_pointers
            ):
                raise ValueError(
                    "counterface_state='not_in_observations' asserts nothing "
                    "about the other side; no counterface evidence may travel"
                )
            return self
        if self.counterface_state == "observed_unclaimed":
            if self.counterface_observation_ref is None:
                raise ValueError(
                    "observed_unclaimed must point at the observed-but-"
                    "unclaimed counterface node"
                )
            if self.counterface_disposition_status is None:
                raise ValueError(
                    "observed_unclaimed must carry that observation's "
                    "disposition status"
                )
            if self.counterface_witness_pointers:
                raise ValueError(
                    "ink witnesses belong to ink_present_unpromoted, not to "
                    "observed_unclaimed"
                )
            return self
        # ink_present_unpromoted
        if not self.counterface_witness_pointers:
            raise ValueError(
                "ink_present_unpromoted MUST carry at least one pixel "
                "witness pointer -- the claim is otherwise free prose"
            )
        if (
            self.counterface_observation_ref is not None
            or self.counterface_disposition_status is not None
        ):
            raise ValueError(
                "ink_present_unpromoted has no observed counterface node by "
                "definition; only witness pointers may travel"
            )
        return self


class LegacyWallTraceClaimV1(WallClaimBase):
    """A legacy ``ReadingView`` ``pen=="wall"`` stroke: wall-RELATED ink of
    ⚠️ unknown geometric role.  ``source_basis`` defaults to ``unknown``;
    a non-unknown basis REQUIRES a structured evidence pointer (design §4.1:
    ⛔ never derive basis from the free ``note``).  The full signed-sidecar
    protocol of design §8.3 is module 3's business; here the pointer merely
    has to exist and resolve."""

    kind: Literal["legacy_wall_trace"] = "legacy_wall_trace"
    trace_ref: ObservationRefV1
    source_basis: Literal["centerline", "wall_face", "outer_skin", "unknown"]
    basis_evidence_ref: ArtifactPointerV1 | None = None

    @model_validator(mode="after")
    def _basis_evidence_is_structured(self) -> "LegacyWallTraceClaimV1":
        if self.source_basis != "unknown" and self.basis_evidence_ref is None:
            raise ValueError(
                f"source_basis={self.source_basis!r} requires a structured "
                "basis_evidence_ref; free-text notes never satisfy this gate"
            )
        return self


WallClaimV1 = Annotated[
    Union[
        PairedFacesWallClaimV1,
        SolidBandWallClaimV1,
        SingleFaceWallClaimV1,
        LegacyWallTraceClaimV1,
    ],
    Field(discriminator="kind"),
]


# ── §4.2: the three face-line dispositions ────────────────────────────────── #
class FaceDispositionV1(BaseModel):
    """How ONE as-drawn face line is consumed.  ⭐ The three statuses are
    semantically distinct and ⛔ must not be merged: ``claimed_wall`` is
    consumption by exactly one wall claim, ``non_wall`` is a NEGATIVE
    semantic assertion by reading, ``ambiguous`` is an honest abstention and
    forms a known-missing evidence debt (never a silent ``non_wall``)."""

    model_config = _CFG

    face_ref: ObservationRefV1
    status: Literal["claimed_wall", "non_wall", "ambiguous"]
    consuming_claim_id: StableId | None = None
    reason_ref: ArtifactPointerV1 | None = None

    @model_validator(mode="after")
    def _status_fields_agree(self) -> "FaceDispositionV1":
        if self.status == "claimed_wall":
            if self.consuming_claim_id is None:
                raise ValueError(
                    "claimed_wall must name the one claim that consumes it"
                )
            if self.reason_ref is not None:
                raise ValueError(
                    "claimed_wall's justification travels on the claim, not here"
                )
        else:
            if self.consuming_claim_id is not None:
                raise ValueError(
                    f"{self.status} is not consumed by any wall claim"
                )
            if self.reason_ref is None:
                raise ValueError(
                    f"{self.status} must point at the source node carrying "
                    "reading's reason"
                )
        return self


# ── §3.3: bundle members ──────────────────────────────────────────────────── #
#: ⭐ 2026-09-03 (B3): ``floor_levels`` joined the domain.  An as-drawn
#: elevation product carries the storey ladder in its horizontal structure
#: lines; the plan and legacy families do not, so for them the channel
#: travels ``absent`` + ``missing_channel`` -- the routing table says where
#: each kind of evidence lives, ⛔ never a silent hole.
CHANNELS = (
    "walls",
    "plan_openings",
    "elevation_openings",
    "floor_levels",
    "dimensions",
    "room_roles",
)
ChannelName = Literal[
    "walls",
    "plan_openings",
    "elevation_openings",
    "floor_levels",
    "dimensions",
    "room_roles",
]


class ChannelStatusV1(BaseModel):
    """Per-channel availability.  ⭐ An absent channel may only travel with an
    explicit evidence debt (design §3.3: "walls wired" must never quietly
    mean "openings still scavenged from any file with ``strokes``")."""

    model_config = _CFG

    channel: ChannelName
    state: Literal["present", "absent"]
    source_input_ids: tuple[StableId, ...] = ()
    covered_by_debt_ids: tuple[StableId, ...] = ()

    @model_validator(mode="after")
    def _state_fields_agree(self) -> "ChannelStatusV1":
        if self.state == "present":
            if not self.source_input_ids:
                raise ValueError("a present channel must name its sources")
            if self.covered_by_debt_ids:
                raise ValueError("a present channel carries no debt")
        elif not self.covered_by_debt_ids:
            raise ValueError(
                "an absent channel must be covered by an explicit evidence "
                "debt -- absence without a debt is a silent hole"
            )
        return self


#: (dispatch 2026-09-04e, T4-a T1) The CLOSED obligation domain: every value
#: is a downstream obligation a debt may carry -- the promise that a named
#: handler exists and must redeem it.  The domain is exactly what today's
#: producers mint (ONE value: the B4 span-equality obligation), ⛔ no slots
#: for values nobody mints.  The value is the premise constant
#: ``ELEVATION_CHAIN_SPANS_WHOLE_BUILDING`` (defined in ``evidence_adapters``
#: -- this module cannot import it there, so it is spelled out here in
#: lower-case snake form, same name): B4's redemption registry keys its rows
#: by this domain and checks the two sides cover each other at import.
DebtObligationV1 = Literal["elevation_chain_spans_whole_building"]


class EvidenceDebtV1(BaseModel):
    """A structured, named known-missing (design §3.3).  Whether a profile may
    continue past it is module 3+/pipeline policy, ⛔ not this type's call.

    ``zero_payload_channel`` (cross-review F-1) is the explicit companion of
    a ``present`` channel that produced no payload this run -- distinct from
    ``missing_channel``, which covers an ``absent`` channel.  It is what
    keeps the payload-closure gate from killing the honest "wired, produced
    nothing" shape."""

    model_config = _CFG

    debt_id: StableId
    kind: Literal[
        "missing_channel",
        "ambiguous_face",
        "pairs_selection_absent",
        "zero_payload_channel",
        "other_known_missing",
    ]
    channel: ChannelName | None = None
    affected_refs: tuple[ArtifactPointerV1, ...] = ()
    description: str
    #: (dispatch 2026-09-04e, T4-a T1/T2) REQUIRED on every mint: the debt's
    #: downstream obligation, or ``None`` = no downstream obligation at all
    #: (the honest shape of every non-span debt today).  A non-``None`` value
    #: is a PROMISE that a handler redeems it: the handler side (B4's
    #: ``DEBT_REDEMPTION_REGISTRY``) keys its rows by exactly this domain,
    #: refuses a debt whose obligation has no row, loudly, and its
    #: import-time teeth refuse a registry key outside this domain (or a
    #: domain value with no registry row) -- so a value here cannot exist
    #: unwired.  ⚠️ The field rides inside ``_sorted_bundle``'s dump, so a
    #: freshly finalized bundle hash changes shape; that cost was measured
    #: once, 0 red (execution report 2026-09-04b, T0).
    obligation: DebtObligationV1 | None


class OpeningClaimV1(BaseModel):
    """One opening candidate, by reference only.  The full opening business
    protocol is deliberately NOT re-designed here (design §3.3); this layer
    only owns its identity and reference integrity -- including that the
    candidate's ``face_line``/``gap_index`` pair resolves inside that face's
    ``gaps`` (a dangling index is a dangling reference, NF-4 #4)."""

    model_config = _CFG

    opening_id: StableId
    source_ref: ObservationRefV1


class ElevationOpeningClaimV1(BaseModel):
    """One elevation opening's VERTICAL half: value AND byte provenance (B3).

    ⭐ This is the elevation leg's counterpart of :class:`OpeningClaimV1`,
    which is reference-only.  ``WindowV3.z`` needs the number, so the number
    travels here -- but ⛔ never as a bare value: ``z_low_m`` / ``z_high_m``
    each carry the pointer to the EXACT frozen byte they were read from
    (``/openings/<i>/z_range_m/<0|1>``), and the validator recomputes the
    equality against the frozen source.  A value that drifted from its
    carrier is a carrier swap, ⛔ not a rounding matter, and the comparison
    is exact: both sides are the same JSON literal parsed twice, so any
    difference is a lie, never float noise.

    The horizontal extent (``x_range_m``) is deliberately NOT here: B4 owns
    the cross-view pairing that needs it, and this layer only carries what a
    named consumer asked for.
    """

    model_config = _CFG

    opening_id: StableId
    source_ref: ObservationRefV1
    z_low_m: float
    z_low_ref: ArtifactPointerV1
    z_high_m: float
    z_high_ref: ArtifactPointerV1

    @model_validator(mode="after")
    def _z_direction_agrees(self) -> "ElevationOpeningClaimV1":
        if not self.z_low_m < self.z_high_m:
            raise ValueError(
                f"elevation opening {self.opening_id!r}: z_low_m "
                f"({self.z_low_m}) must be below z_high_m ({self.z_high_m})"
            )
        return self


#: ⭐ T4's decidable floor-line rule, stated once and shared by the adapter
#: (the producer) and the validator (the mirror), so they can never be two
#: opinions: a floor-level candidate is EVERY horizontal structure line of
#: the elevation (``constant_quantity == "z"``).  A rule, ⛔ not a list --
#: an elevation of any storey count and any storey heights yields exactly
#: its own horizontal lines, and no real-case reading ever appears in code.
#: Sorting the selected levels ascending gives the ladder; adjacent
#: differences are the storey-height candidates B2 derives
#: ``ceiling_height`` from.
FLOOR_LEVEL_SELECTION_RULE = "every structure line with constant_quantity == 'z'"

#: The structural minimum of a floor ladder: ground + one roof/upper level.
#: ⚠️ A COUNT, not a length constant -- it encodes "a whole-building
#: elevation has at least two levels", which is the z-direction shape of the
#: ELEVATION_CHAIN_SPANS_WHOLE_BUILDING premise (a single-level line is the
#: loud signature of a partial elevation).
MIN_FLOOR_LEVELS = 2


class FloorLevelClaimV1(BaseModel):
    """One floor level: value AND byte provenance (B3/T4).

    Selected by :data:`FLOOR_LEVEL_SELECTION_RULE` -- see its docstring for
    why that predicate and not a hand-picked list.  ``z_m`` carries the
    pointer to the frozen ``pos_m`` byte it was read from; the validator
    recomputes the equality AND re-runs the rule against the frozen source,
    so a dropped or invented level is loud, not silent.
    """

    model_config = _CFG

    structure_line_id: StableId
    source_ref: ObservationRefV1
    z_m: float
    z_ref: ArtifactPointerV1


class SourceArtifactV1(BaseModel):
    """One frozen reading artifact's identity.  ``(view_type, floor_ref)``
    is the manifest's SEMANTIC slot; two sources in one slot is
    ``DUPLICATE_SEMANTIC_INPUT`` (design §4.4 #5), never a file-order win."""

    model_config = _CFG

    input_id: StableId
    source_contract_id: StableId
    source_output_sha256: Hex64
    view_type: Literal["plan", "elevation"] | None = None
    floor_ref: str | None = None


class CorrectionEvidenceBundleV1(BaseModel):
    """The unified evidence bundle top level (design §3.3).

    ``content_sha256`` is computed by :func:`finalize_bundle` over the
    canonically sorted bundle with the hash field removed; the validator
    recomputes it (invariant 8), so the same frozen bytes must produce a
    byte-identical bundle and any tampering with ordering or content goes
    loud.  ⚠️ It is a bundle IDENTITY, ⛔ not an equality test for sub-facts
    of the bundle.
    """

    model_config = _CFG

    schema_version: Literal["correction_evidence_bundle_v1"]
    view_manifest_sha256: Hex64 | None = None
    source_artifacts: list[SourceArtifactV1] = Field(default_factory=list)
    channel_status: list[ChannelStatusV1] = Field(default_factory=list)
    wall_claims: list[WallClaimV1] = Field(default_factory=list)
    face_dispositions: list[FaceDispositionV1] = Field(default_factory=list)
    opening_claims: list[OpeningClaimV1] = Field(default_factory=list)
    elevation_opening_claims: list[ElevationOpeningClaimV1] = Field(
        default_factory=list
    )
    floor_level_claims: list[FloorLevelClaimV1] = Field(default_factory=list)
    evidence_debts: list[EvidenceDebtV1] = Field(default_factory=list)
    content_sha256: Hex64 | None = None


class FrozenSourceV1(BaseModel):
    """One source artifact together with its frozen bytes.  ⭐ References
    resolve ONLY inside these bytes -- never by re-reading a working
    directory file that may have changed (design §3.2)."""

    model_config = _CFG

    artifact: SourceArtifactV1
    raw_bytes: bytes


class CorrectionEvidenceBundleArtifactV1(BaseModel):
    """The persistable carrier: the bundle plus every frozen source."""

    model_config = _CFG

    bundle: CorrectionEvidenceBundleV1
    frozen_sources: list[FrozenSourceV1] = Field(default_factory=list)


# ── canonical identity helpers ────────────────────────────────────────────── #
def _pointer_identity(ref: ArtifactPointerV1) -> dict:
    data = {
        "input_id": ref.input_id,
        "source_contract_id": ref.source_contract_id,
        "source_output_sha256": ref.source_output_sha256,
        "json_pointer": ref.json_pointer,
    }
    observation_id = getattr(ref, "observation_id", None)
    if observation_id is not None:
        data["observation_id"] = observation_id
    return data


def wall_claim_id(
    claim: WallClaimV1,
) -> str:
    """The canonical claim id: a hash over kind + normalised refs, ⛔ never an
    array index (design §4.1).  Recomputed by the validator, so a claim
    cannot wear another claim's id."""
    refs: list[ArtifactPointerV1] = []
    kind = claim.kind
    if kind == "paired_faces":
        refs = [claim.face_a_ref, claim.face_b_ref, claim.pair_candidate_ref]
    elif kind == "solid_band":
        refs = [claim.band_face_ref]
    elif kind == "single_face":
        refs = [claim.face_ref]
    else:
        refs = [claim.trace_ref]
    return "wallc_" + canonical_sha256(
        {
            "kind": kind,
            "hypothesis_ref": _pointer_identity(claim.hypothesis_ref),
            "perception_source_ref": _pointer_identity(
                claim.perception_source_ref
            ),
            "refs": sorted(
                json.dumps(_pointer_identity(r), sort_keys=True)
                for r in refs
            ),
        }
    )


# ── canonical sorting + content hash (invariant 8) ────────────────────────── #
def _sorted_bundle(bundle: CorrectionEvidenceBundleV1) -> dict:
    """The canonical, hash-free dict of a bundle with every list sorted.

    ⚠️ ``mode="python"`` on purpose: the json-mode dump would turn every tuple
    field into a list, which this module's own ``strict=True`` models then
    refuse -- the finalize path would be dead on arrival.  Tuples serialize
    as arrays under ``json.dumps`` all the same, so the canonical hash is
    unaffected."""
    data = bundle.model_dump(mode="python")
    data.pop("content_sha256", None)
    data["source_artifacts"] = sorted(
        data["source_artifacts"], key=lambda a: a["input_id"]
    )
    data["channel_status"] = sorted(
        data["channel_status"], key=lambda c: c["channel"]
    )
    data["wall_claims"] = sorted(
        data["wall_claims"], key=lambda c: c["claim_id"]
    )
    data["face_dispositions"] = sorted(
        data["face_dispositions"],
        key=lambda d: (
            d["face_ref"]["input_id"],
            d["face_ref"]["observation_id"],
        ),
    )
    data["opening_claims"] = sorted(
        data["opening_claims"],
        key=lambda o: (o["source_ref"]["input_id"], o["opening_id"]),
    )
    data["elevation_opening_claims"] = sorted(
        data["elevation_opening_claims"],
        key=lambda o: (o["source_ref"]["input_id"], o["opening_id"]),
    )
    data["floor_level_claims"] = sorted(
        data["floor_level_claims"],
        key=lambda o: (
            o["source_ref"]["input_id"],
            o["structure_line_id"],
        ),
    )
    data["evidence_debts"] = sorted(
        data["evidence_debts"], key=lambda d: d["debt_id"]
    )
    return data


def finalize_bundle(
    bundle: CorrectionEvidenceBundleV1,
) -> CorrectionEvidenceBundleV1:
    """Return the canonically sorted bundle with ``content_sha256`` filled.

    Pure function: the input is not mutated.  Two bundles built from the same
    frozen bytes through this function serialize byte-identically.
    """
    staged = CorrectionEvidenceBundleV1.model_validate(
        _sorted_bundle(bundle)
    )
    content = _sorted_bundle(staged)
    return staged.model_copy(
        update={"content_sha256": canonical_sha256(content)}
    )


# ── frozen-source parsing (shared by the validator; module 3 may reuse) ────── #
def resolve_json_pointer(doc: object, pointer: str) -> object:
    """Resolve one RFC-6901 json pointer inside ``doc``, loudly.

    Raises ``KeyError`` for any token that does not address an existing node
    -- this is the mechanical half of "resolves uniquely" (invariant 1).
    """
    if pointer == "":
        return doc
    if not pointer.startswith("/"):
        raise KeyError(f"not a json pointer: {pointer!r}")
    node = doc
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            if not token.isdigit() or int(token) >= len(node):
                raise KeyError(f"{pointer!r}: no index {token!r}")
            node = node[int(token)]
        elif isinstance(node, dict):
            if token not in node:
                raise KeyError(f"{pointer!r}: no key {token!r}")
            node = node[token]
        else:
            raise KeyError(f"{pointer!r}: cannot descend into {type(node).__name__}")
    return node


def as_drawn_face_index(doc: dict) -> dict[str, tuple[int, dict]]:
    """Map face-line id -> (index, node) for one as-drawn product.

    ⭐ NF-4 #3: duplicate ids are LOUD here -- "resolves uniquely" means the
    index can be built at all.  (The bundle builder and the validator share
    this function, so a constructor-time refusal and a validator refusal are
    the same teeth, not two opinions.)
    """
    faces = doc.get("observations", {}).get("face_lines", [])
    index: dict[str, tuple[int, dict]] = {}
    for i, face in enumerate(faces):
        face_id = face.get("id")
        if not isinstance(face_id, str) or not face_id:
            raise EvidenceContractError(
                "FACE_LINE_WITHOUT_ID", {"index": i}
            )
        if face_id in index:
            raise EvidenceContractError(
                "DUPLICATE_OBSERVATION_ID",
                {
                    "observation_id": face_id,
                    "first_index": index[face_id][0],
                    "second_index": i,
                },
            )
        index[face_id] = (i, face)
    return index


def as_drawn_elevation_opening_index(doc: dict) -> dict[str, tuple[int, dict]]:
    """Map elevation opening id -> (index, node) for one elevation product.

    Same discipline as :func:`as_drawn_face_index`: duplicate ids are LOUD,
    and the adapter and the validator build their views through this one
    function so a constructor-time refusal and a validator refusal are the
    same teeth, never two opinions.
    """
    openings = doc.get("openings", [])
    index: dict[str, tuple[int, dict]] = {}
    for i, opening in enumerate(openings):
        oid = opening.get("id")
        if not isinstance(oid, str) or not oid:
            raise EvidenceContractError(
                "ELEVATION_OPENING_WITHOUT_ID", {"index": i}
            )
        if oid in index:
            raise EvidenceContractError(
                "DUPLICATE_OBSERVATION_ID",
                {
                    "observation_id": oid,
                    "first_index": index[oid][0],
                    "second_index": i,
                },
            )
        index[oid] = (i, opening)
    return index


def as_drawn_elevation_floor_lines(doc: dict) -> dict[str, tuple[int, dict]]:
    """Map floor-line id -> (index, node) via :data:`FLOOR_LEVEL_SELECTION_RULE`.

    THE rule (T4), stated as a predicate over the frozen product and shared
    by producer and validator: every ``structure_lines`` entry whose
    ``constant_quantity`` is ``"z"`` (a horizontal line) is a floor-level
    candidate.  ⛔ No magnitude filter, no count filter, no hand-picked list
    -- the ladder is whatever the drawing's horizontal structure lines say,
    sorted ascending by the consumer.  A vertical line (``"x"``) is an
    opening-grid line and is never a level.
    """
    lines = doc.get("structure_lines", [])
    index: dict[str, tuple[int, dict]] = {}
    for i, line in enumerate(lines):
        if line.get("constant_quantity") != "z":
            continue
        lid = line.get("id")
        if not isinstance(lid, str) or not lid:
            raise EvidenceContractError(
                "STRUCTURE_LINE_WITHOUT_ID", {"index": i}
            )
        if lid in index:
            raise EvidenceContractError(
                "DUPLICATE_OBSERVATION_ID",
                {
                    "observation_id": lid,
                    "first_index": index[lid][0],
                    "second_index": i,
                },
            )
        index[lid] = (i, line)
    return index


# ── cross-review rework (2026-08-31): F-1 / F-2 structural invariants ─────── #
#: ⭐⭐ The ONE explicit ``channel -> payload member`` table (design §3.3).
#: Every direction of the declaration/payload reconciliation below reads THIS
#: table, so ``state`` is a reconciliation RESULT and ⛔ never a field that can
#: be set independently of what the bundle actually carries.  A channel that
#: is not a key here has no payload member on this bundle type at all --
#: ``dimensions`` / ``room_roles`` today (rework-2 B-2: no debt can make a
#: ``present`` true for them; ``elevation_openings`` left that set on
#: 2026-09-03 when B3 gave it a payload member).
CHANNEL_PAYLOAD_MEMBERS: dict[str, tuple[str, ...]] = {
    "walls": ("wall_claims", "face_dispositions"),
    "plan_openings": ("opening_claims",),
    # ⭐ 2026-09-03 (B3): the elevation leg's two payload members.  Adding
    # them here is what retires B-2's "no payload carrier" exemption for
    # ``elevation_openings`` and gives ``floor_levels`` a carrier at all:
    # from now on a ``present`` row for either channel is measured against
    # these lists, and a ``zero_payload_channel`` debt may legitimately name
    # them (an honest wired-but-windowless elevation).
    "elevation_openings": ("elevation_opening_claims",),
    "floor_levels": ("floor_level_claims",),
}


def _channel_payload_rows(
    bundle: CorrectionEvidenceBundleV1, channel: str
) -> list[tuple[str, object]]:
    """EVERY row of every payload member mapped to ``channel``.

    This is the reach of the SOURCE closure (B-1): "which frozen products did
    anything in this channel's payload come from".  ⛔ Not the same question
    as "did this channel produce" -- see :func:`_payload_row_witnesses`."""
    rows: list[tuple[str, object]] = []
    for member in CHANNEL_PAYLOAD_MEMBERS.get(channel, ()):
        rows.extend((member, row) for row in getattr(bundle, member))
    return rows


def _payload_row_witnesses(member: str, row: object) -> bool:
    """Does THIS row assert that its channel PRODUCED something?

    ⭐ Not every row of a payload member does, and the difference is not
    cosmetic.  Invariant 2 mandates one disposition for EVERY as-drawn face
    line whatever the walls leg produced, so a ledger whose rows are all
    ``non_wall`` / ``ambiguous`` is an honest walls-ABSENT run with a FULL
    ledger -- exactly the shape module 3's adapter emits (it declares walls
    present only when a positive claim exists) and module 4's compiler
    consumes.  Only a ``claimed_wall`` row is the walls channel speaking,
    and it is a witness in its own right: on a non-as-drawn source the
    claim<->disposition closure above cannot see it.

    Every other payload member exists only when its channel produced, so
    every one of its rows witnesses.
    """
    if member == "face_dispositions":
        return row.status == "claimed_wall"
    return True


def _channel_witness_rows(
    bundle: CorrectionEvidenceBundleV1, channel: str
) -> list[tuple[str, object]]:
    """The rows that make ``channel``'s ``state`` say ``present``."""
    return [
        (member, row)
        for member, row in _channel_payload_rows(bundle, channel)
        if _payload_row_witnesses(member, row)
    ]


def _channel_has_payload(
    bundle: CorrectionEvidenceBundleV1, channel: str
) -> bool:
    """Does this bundle actually carry ``channel``'s payload?

    Derived from :data:`CHANNEL_PAYLOAD_MEMBERS` -- a channel with no payload
    member has nothing to witness a ``present`` with, so this is False for it
    by construction rather than by a hand-written branch."""
    return bool(_channel_witness_rows(bundle, channel))


def _assert_channel_payload_closure(
    bundle: CorrectionEvidenceBundleV1,
) -> None:
    """The declaration/payload reconciliation, over the whole CHANNEL DOMAIN.

    ⭐⭐⭐ ``state`` is not read as permission to skip anything.  For every
    channel in :data:`CHANNELS` this MEASURES what the bundle carries (via
    the one :data:`CHANNEL_PAYLOAD_MEMBERS` table) and then requires the
    declaration to be that measurement's name:

    ==================  =====================================================
    measured payload    the only legal declaration
    ==================  =====================================================
    non-empty           ``present``
    empty               ``absent``, or ``present`` + an explicit
                        ``zero_payload_channel`` debt ("wired, produced
                        nothing this run" -- F-1's honest exit, and only for
                        a channel that HAS a payload member, rework-2 B-2)
    ==================  =====================================================

    Three carriers are pinned here on purpose, because the first two rounds
    of this same defect family were never "the gate computed the wrong
    number" -- the thing it measured got swapped
    ([[gate-measures-right-but-carrier-gets-swapped]]):

    * the VALUE of ``state`` -- round 3's swap was declaring ``absent`` while
      the payload rode along, and both earlier gates opened with
      ``if status.state != "present": continue``.  There is ⛔ no such
      early-continue in here;
    * the EXISTENCE of the row -- iterating the DECLARED rows would let the
      same payload through by simply deleting the row, so this iterates the
      channel domain and a channel with no row at all is
      ``CHANNEL_STATUS_MISSING``;
    * WHICH ROWS COUNT as payload -- the disposition ledger is a function of
      the source product, not of the walls leg, so the witness predicate is
      explicit and per member (:func:`_payload_row_witnesses`) instead of
      "the member list is non-empty".
    """
    zero_payload: set[str] = set()
    for debt in bundle.evidence_debts:
        if debt.kind == "zero_payload_channel":
            if debt.channel is None:
                raise EvidenceContractError(
                    "ZERO_PAYLOAD_DEBT_WITHOUT_CHANNEL",
                    {"debt_id": debt.debt_id},
                )
            if debt.channel not in CHANNEL_PAYLOAD_MEMBERS:
                raise EvidenceContractError(
                    "ZERO_PAYLOAD_DEBT_WITHOUT_PAYLOAD_CARRIER",
                    {"debt_id": debt.debt_id, "channel": debt.channel},
                )
            zero_payload.add(debt.channel)

    rows_by_channel = {s.channel: s for s in bundle.channel_status}
    for channel in CHANNELS:
        status = rows_by_channel.get(channel)
        witnesses = _channel_witness_rows(bundle, channel)
        if status is None:
            # ⛔ Undeclared is not a third state: the routing table has to
            # cover the domain, or "absent" can be swapped for "unsaid".
            raise EvidenceContractError(
                "CHANNEL_STATUS_MISSING",
                {
                    "channel": channel,
                    "payload_rows": sorted({m for m, _ in witnesses}),
                },
            )
        if witnesses:
            if status.state != "present":
                raise EvidenceContractError(
                    "CHANNEL_DECLARED_ABSENT_WITH_PAYLOAD",
                    {
                        "channel": channel,
                        "state": status.state,
                        "payload_rows": sorted({m for m, _ in witnesses}),
                        "payload_row_count": len(witnesses),
                    },
                )
            continue
        if status.state == "present" and channel not in zero_payload:
            raise EvidenceContractError(
                "PRESENT_CHANNEL_WITHOUT_PAYLOAD",
                {"channel": channel},
            )


def _claim_source_input_ids(claim: WallClaimV1) -> set[str]:
    """F-2: every ``input_id`` ONE claim's references point into."""
    ids = {
        claim.hypothesis_ref.input_id,
        claim.perception_source_ref.input_id,
    }
    if claim.kind == "paired_faces":
        ids.update(
            {
                claim.face_a_ref.input_id,
                claim.face_b_ref.input_id,
                claim.pair_candidate_ref.input_id,
            }
        )
    elif claim.kind == "solid_band":
        ids.add(claim.band_face_ref.input_id)
    elif claim.kind == "single_face":
        ids.add(claim.face_ref.input_id)
        if claim.counterface_observation_ref is not None:
            ids.add(claim.counterface_observation_ref.input_id)
    else:
        ids.add(claim.trace_ref.input_id)
        if claim.basis_evidence_ref is not None:
            ids.add(claim.basis_evidence_ref.input_id)
    return ids


def _assert_claim_refs_single_sourced(claim: WallClaimV1) -> None:
    """F-2: identity IS ``input_id`` (design §3.2) -- one wall's faces,
    hypothesis and candidate evidence must all live in the SAME frozen
    source.  A claim spanning two inputs is structurally legal (ids like
    ``F01`` exist in every floor's product) but physically impossible."""
    claim_inputs = _claim_source_input_ids(claim)
    if len(claim_inputs) != 1:
        raise EvidenceContractError(
            "CLAIM_REFS_SPAN_MULTIPLE_INPUTS",
            {"claim_id": claim.claim_id, "input_ids": sorted(claim_inputs)},
        )


# ── rework-2 (2026-08-31) B-1: payload must come from the DECLARED source ─── #
def _payload_row_source_ids(member: str, row: object) -> set[str]:
    """Every ``input_id`` ONE payload row points into.

    ⭐ Adding a member to :data:`CHANNEL_PAYLOAD_MEMBERS` without teaching
    this function where its identity lives is loud, ⛔ not a silently empty
    source set ([[declare-the-dialect-plus-consumption-ledger]])."""
    if member == "wall_claims":
        return _claim_source_input_ids(row)
    if member == "face_dispositions":
        ids = {row.face_ref.input_id}
        if row.reason_ref is not None:
            ids.add(row.reason_ref.input_id)
        return ids
    if member == "opening_claims":
        return {row.source_ref.input_id}
    if member == "elevation_opening_claims":
        return {
            row.source_ref.input_id,
            row.z_low_ref.input_id,
            row.z_high_ref.input_id,
        }
    if member == "floor_level_claims":
        return {row.source_ref.input_id, row.z_ref.input_id}
    raise EvidenceContractError(
        "PAYLOAD_MEMBER_WITHOUT_SOURCE_RULE", {"member": member}
    )


def _channel_payload_source_ids(
    bundle: CorrectionEvidenceBundleV1, channel: str
) -> set[str]:
    """B-1: every ``input_id`` ``channel``'s payload actually lives in.

    ⭐ The reach here is EVERY row of every mapped member (the whole
    disposition ledger included), ⛔ not only the rows that witness a
    ``present``: a ``non_wall`` row smuggled in from an undeclared product is
    still payload arriving from the wrong place.  Derived from the one
    :data:`CHANNEL_PAYLOAD_MEMBERS` table, so a channel with no payload
    member carries nothing by construction."""
    ids: set[str] = set()
    for member, row in _channel_payload_rows(bundle, channel):
        ids |= _payload_row_source_ids(member, row)
    return ids


def _assert_channel_source_closure(
    bundle: CorrectionEvidenceBundleV1,
    frozen: dict[str, FrozenSourceV1],
) -> None:
    """B-1: ``channel_status`` must be a truthful routing table, not just a
    non-empty one.  Two directions, per PRESENT channel that has payload
    members:

    * forward -- every input the payload actually came from must be one of
      the channel's declared ``source_input_ids`` (payload from somewhere
      else is the "wrong-source payload" swap the rework-2 review found);
    * reverse -- a declared source that contributed no payload this run must
      be excused by a ``zero_payload_channel`` debt SCOPED to it via
      ``affected_refs``.  ⛔ A channel-granularity debt cannot excuse it: that
      debt says "this channel produced nothing", which is FALSE while other
      declared sources did produce.  (A whole-channel zero run keeps the
      F-1 global-debt exit -- there the statement is true.)

    A scoped debt's refs must name real frozen sources by their full
    identity, so the excuse cannot be minted against a made-up input.
    """
    scoped: dict[str, set[str]] = {}
    for debt in bundle.evidence_debts:
        if debt.kind != "zero_payload_channel" or debt.channel is None:
            continue
        for ref in debt.affected_refs:
            source = frozen.get(ref.input_id)
            if source is None or (
                ref.source_output_sha256
                != source.artifact.source_output_sha256
                or ref.source_contract_id != source.artifact.source_contract_id
            ):
                raise EvidenceContractError(
                    "SCOPED_ZERO_PAYLOAD_DEBT_REF_UNKNOWN",
                    {"debt_id": debt.debt_id, "input_id": ref.input_id},
                )
        if debt.affected_refs:
            scoped.setdefault(debt.channel, set()).update(
                r.input_id for r in debt.affected_refs
            )
    for status in bundle.channel_status:
        if status.channel not in CHANNEL_PAYLOAD_MEMBERS:
            continue  # payloadless channels: B-2's business, not ours
        if status.state != "present":
            continue
        declared = set(status.source_input_ids)
        payload = _channel_payload_source_ids(bundle, status.channel)
        if payload - declared:
            raise EvidenceContractError(
                "PAYLOAD_FROM_UNDECLARED_SOURCE",
                {
                    "channel": status.channel,
                    "payload_input_ids": sorted(payload),
                    "declared_input_ids": sorted(declared),
                },
            )
        if not payload:
            continue  # whole-channel zero run: F-1's exit owns that case
        unscoped = (declared - payload) - scoped.get(status.channel, set())
        if unscoped:
            raise EvidenceContractError(
                "CHANNEL_SOURCE_WITHOUT_PAYLOAD_OR_SCOPED_DEBT",
                {"channel": status.channel, "input_ids": sorted(unscoped)},
            )


# ── the validator: hard invariants 1-8 (+ NF-4 additions) ─────────────────── #
def validate_evidence_bundle(
    artifact: CorrectionEvidenceBundleArtifactV1,
) -> None:
    """Check hard invariants 1-8 of design §4.4 on ONE frozen artifact.

    Raises :class:`EvidenceContractError` with a stable ``code``.  Everything
    here is input integrity: none of it is ever a model's job to fix.
    """
    bundle = artifact.bundle
    if bundle.content_sha256 is None:
        raise EvidenceContractError("BUNDLE_NOT_FINALIZED", {})

    # -- frozen sources: identity, integrity, and contract match ----------
    frozen: dict[str, FrozenSourceV1] = {}
    docs: dict[str, dict] = {}
    for source in artifact.frozen_sources:
        meta = source.artifact
        if meta.input_id in frozen:
            raise EvidenceContractError(
                "DUPLICATE_INPUT_ID", {"input_id": meta.input_id}
            )
        digest = hashlib.sha256(source.raw_bytes).hexdigest()
        if digest != meta.source_output_sha256:
            raise EvidenceContractError(
                "SOURCE_HASH_MISMATCH",
                {"input_id": meta.input_id, "declared": meta.source_output_sha256,
                 "measured": digest},
            )
        try:
            doc = json.loads(source.raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidenceContractError(
                "SOURCE_NOT_JSON", {"input_id": meta.input_id}
            ) from exc
        decision = classify_vector_json(doc)
        if decision.contract_id != meta.source_contract_id:
            if decision.contract_id == CONTRACT_UNKNOWN and (
                "AMBIGUOUS" in (decision.reason or "")
            ):
                # invariant 6: every detector ran; one file matched two
                raise EvidenceContractError(
                    "AMBIGUOUS_CONTRACT_MATCH",
                    {"input_id": meta.input_id, "reason": decision.reason},
                )
            if decision.contract_id == CONTRACT_UNKNOWN and isinstance(
                doc.get("schema"), str
            ):
                # invariant 7: declared a schema, failed that producer's type
                # -- ⛔ never falls back to legacy
                raise EvidenceContractError(
                    "MALFORMED_DECLARED_CONTRACT",
                    {"input_id": meta.input_id, "reason": decision.reason},
                )
            raise EvidenceContractError(
                "CONTRACT_MISMATCH",
                {
                    "input_id": meta.input_id,
                    "declared": meta.source_contract_id,
                    "detected": decision.contract_id,
                },
            )
        frozen[meta.input_id] = source
        docs[meta.input_id] = doc

    declared = {a.input_id: a for a in bundle.source_artifacts}
    if set(declared) != set(frozen):
        raise EvidenceContractError(
            "SOURCE_SET_MISMATCH",
            {
                "declared_not_frozen": sorted(set(declared) - set(frozen)),
                "frozen_not_declared": sorted(set(frozen) - set(declared)),
            },
        )
    for input_id, meta in declared.items():
        source = frozen[input_id].artifact
        if (
            meta.source_output_sha256 != source.source_output_sha256
            or meta.source_contract_id != source.source_contract_id
        ):
            raise EvidenceContractError(
                "SOURCE_METADATA_MISMATCH", {"input_id": input_id}
            )

    # -- per-source observation indexes (NF-4 #3: duplicate ids are loud) --
    face_index: dict[str, dict[str, tuple[int, dict]]] = {}
    gaps_of: dict[str, dict[str, list]] = {}
    elevation_opening_index: dict[str, dict[str, tuple[int, dict]]] = {}
    floor_lines: dict[str, dict[str, tuple[int, dict]]] = {}
    for input_id, meta in declared.items():
        if meta.source_contract_id == SOURCE_CONTRACT_AS_DRAWN_ELEVATION:
            elevation_opening_index[input_id] = (
                as_drawn_elevation_opening_index(docs[input_id])
            )
            floor_lines[input_id] = as_drawn_elevation_floor_lines(
                docs[input_id]
            )
            # ⭐ The z-direction shape of ELEVATION_CHAIN_SPANS_WHOLE_BUILDING:
            # a whole-building elevation draws at least ground + one upper
            # level.  Fewer horizontal structure lines is the loud signature
            # of a partial elevation, ⛔ never something to pad silently.
            if len(floor_lines[input_id]) < MIN_FLOOR_LEVELS:
                raise EvidenceContractError(
                    "FLOOR_LADDER_DEGENERATE",
                    {
                        "input_id": input_id,
                        "horizontal_structure_lines": sorted(
                            floor_lines[input_id]
                        ),
                        "minimum": MIN_FLOOR_LEVELS,
                        "rule": FLOOR_LEVEL_SELECTION_RULE,
                    },
                )
        elif meta.source_contract_id == SOURCE_CONTRACT_AS_DRAWN:
            face_index[input_id] = as_drawn_face_index(docs[input_id])
            gaps_of[input_id] = {
                fid: node.get("gaps", []) for fid, (_, node) in face_index[input_id].items()
            }
        elif meta.source_contract_id == SOURCE_CONTRACT_LEGACY:
            strokes = docs[input_id].get("strokes", [])
            seen: dict[str, int] = {}
            for i, stroke in enumerate(strokes):
                sid = stroke.get("id")
                if not isinstance(sid, str) or not sid:
                    raise EvidenceContractError(
                        "STROKE_WITHOUT_ID", {"input_id": input_id, "index": i}
                    )
                if sid in seen:
                    raise EvidenceContractError(
                        "DUPLICATE_OBSERVATION_ID",
                        {"input_id": input_id, "observation_id": sid,
                         "first_index": seen[sid], "second_index": i},
                    )
                seen[sid] = i
        else:
            raise EvidenceContractError(
                "UNSUPPORTED_SOURCE_CONTRACT",
                {"input_id": input_id, "contract": meta.source_contract_id},
            )

    def _deref_pointer(input_id: str, sha256: str, pointer: str) -> object:
        """Invariant 1 mechanics for one pointer."""
        if input_id not in frozen:
            raise EvidenceContractError(
                "UNKNOWN_INPUT_ID", {"input_id": input_id, "pointer": pointer}
            )
        if sha256 != frozen[input_id].artifact.source_output_sha256:
            raise EvidenceContractError(
                "REF_HASH_MISMATCH",
                {"input_id": input_id, "pointer": pointer,
                 "declared": sha256,
                 "frozen": frozen[input_id].artifact.source_output_sha256},
            )
        try:
            return resolve_json_pointer(docs[input_id], pointer)
        except KeyError as exc:
            raise EvidenceContractError(
                "POINTER_UNRESOLVED",
                {"input_id": input_id, "pointer": pointer, "because": str(exc)},
            ) from exc

    def _deref_observation(ref: ObservationRefV1) -> dict:
        node = _deref_pointer(ref.input_id, ref.source_output_sha256, ref.json_pointer)
        if not isinstance(node, dict):
            raise EvidenceContractError(
                "OBSERVATION_NODE_NOT_OBJECT",
                {"input_id": ref.input_id, "pointer": ref.json_pointer},
            )
        if node.get("id") != ref.observation_id:
            raise EvidenceContractError(
                "OBSERVATION_ID_MISMATCH",
                {
                    "input_id": ref.input_id,
                    "pointer": ref.json_pointer,
                    "ref_says": ref.observation_id,
                    "node_says": node.get("id"),
                },
            )
        for witness in ref.pixel_witness_pointers:
            try:
                resolve_json_pointer(docs[ref.input_id], witness)
            except KeyError as exc:
                raise EvidenceContractError(
                    "WITNESS_POINTER_UNRESOLVED",
                    {"input_id": ref.input_id, "pointer": witness, "because": str(exc)},
                ) from exc
        return node

    def _require_bucket_key(
        claim: WallClaimV1, bucket_value: object, face_id: str
    ) -> None:
        """Invariant 4: the claim's hypothesis_ref must land on the bucket
        entry whose key IS the claimed observation id."""
        pointer = claim.hypothesis_ref.json_pointer
        if not isinstance(bucket_value, str):
            raise EvidenceContractError(
                "BUCKET_VALUE_NOT_PROSE",
                {"pointer": pointer, "got": type(bucket_value).__name__},
            )
        if pointer.rsplit("/", 1)[-1] != face_id:
            raise EvidenceContractError(
                "BUCKET_KEY_IS_NOT_THE_CLAIMED_FACE",
                {
                    "pointer": pointer,
                    "bucket_key": pointer.rsplit("/", 1)[-1],
                    "claimed_face": face_id,
                },
            )

    # -- invariant 5: one source per manifest semantic slot -----------------
    slots: dict[tuple[str, str], str] = {}
    for meta in bundle.source_artifacts:
        if meta.view_type is None or meta.floor_ref is None:
            continue
        slot = (meta.view_type, meta.floor_ref)
        if slot in slots and slots[slot] != meta.input_id:
            raise EvidenceContractError(
                "DUPLICATE_SEMANTIC_INPUT",
                {"slot": list(slot), "first": slots[slot], "second": meta.input_id},
            )
        slots[slot] = meta.input_id

    # -- invariants 1+4: every reference resolves, with the right shape ----
    for claim in bundle.wall_claims:
        expected_id = wall_claim_id(claim)
        if claim.claim_id != expected_id:
            raise EvidenceContractError(
                "CLAIM_ID_NOT_CANONICAL",
                {"claim_id": claim.claim_id, "expected": expected_id},
            )
        hyp = _deref_pointer(
            claim.hypothesis_ref.input_id,
            claim.hypothesis_ref.source_output_sha256,
            claim.hypothesis_ref.json_pointer,
        )
        _deref_pointer(
            claim.perception_source_ref.input_id,
            claim.perception_source_ref.source_output_sha256,
            claim.perception_source_ref.json_pointer,
        )
        if claim.kind == "paired_faces":
            face_a = _deref_observation(claim.face_a_ref)
            face_b = _deref_observation(claim.face_b_ref)
            cand = _deref_pointer(
                claim.pair_candidate_ref.input_id,
                claim.pair_candidate_ref.source_output_sha256,
                claim.pair_candidate_ref.json_pointer,
            )
            if not isinstance(cand, dict):
                raise EvidenceContractError(
                    "PAIR_CANDIDATE_NODE_NOT_OBJECT",
                    {"pointer": claim.pair_candidate_ref.json_pointer},
                )
            # invariant 3, checked per claim in this order: self-pair, axes,
            # hypothesis match, candidate-graph membership.
            if claim.face_a_ref.observation_id == claim.face_b_ref.observation_id:
                raise EvidenceContractError(
                    "PAIR_SELF_REFERENTIAL", {"claim_id": claim.claim_id}
                )
            if face_a.get("axis") != face_b.get("axis"):
                raise EvidenceContractError(
                    "PAIR_AXES_DISAGREE",
                    {
                        "claim_id": claim.claim_id,
                        "face_a_axis": face_a.get("axis"),
                        "face_b_axis": face_b.get("axis"),
                    },
                )
            if not isinstance(hyp, dict) or {hyp.get("face_a"), hyp.get("face_b")} != {
                claim.face_a_ref.observation_id,
                claim.face_b_ref.observation_id,
            }:
                raise EvidenceContractError(
                    "PAIR_HYPOTHESIS_MISMATCH",
                    {
                        "claim_id": claim.claim_id,
                        "pointer": claim.hypothesis_ref.json_pointer,
                        "hypothesis_faces": [
                            hyp.get("face_a") if isinstance(hyp, dict) else None,
                            hyp.get("face_b") if isinstance(hyp, dict) else None,
                        ],
                        "claim_faces": [
                            claim.face_a_ref.observation_id,
                            claim.face_b_ref.observation_id,
                        ],
                    },
                )
            if {cand.get("face_a"), cand.get("face_b")} != {
                claim.face_a_ref.observation_id,
                claim.face_b_ref.observation_id,
            }:
                raise EvidenceContractError(
                    "SELECTED_PAIR_NOT_IN_CANDIDATE_GRAPH",
                    {
                        "claim_id": claim.claim_id,
                        "pointer": claim.pair_candidate_ref.json_pointer,
                    },
                )
        elif claim.kind == "solid_band":
            _deref_observation(claim.band_face_ref)
            witnessed = {
                p.rsplit("/", 1)[-1]
                for p in claim.band_face_ref.pixel_witness_pointers
            }
            missing = {"support_cols_px", "edges_m", "runs_px"} - witnessed
            if missing:
                raise EvidenceContractError(
                    "SOLID_BAND_WITNESS_INCOMPLETE",
                    {"claim_id": claim.claim_id, "missing": sorted(missing)},
                )
            _require_bucket_key(
                claim, hyp, claim.band_face_ref.observation_id
            )
        elif claim.kind == "single_face":
            _deref_observation(claim.face_ref)
            _require_bucket_key(claim, hyp, claim.face_ref.observation_id)
            if claim.counterface_state == "observed_unclaimed":
                # existence + id match is the reference-integrity part
                _deref_observation(claim.counterface_observation_ref)
            for witness in claim.counterface_witness_pointers:
                try:
                    resolve_json_pointer(docs[claim.face_ref.input_id], witness)
                except KeyError as exc:
                    raise EvidenceContractError(
                        "COUNTERFACE_WITNESS_UNRESOLVED",
                        {"claim_id": claim.claim_id, "pointer": witness,
                         "because": str(exc)},
                    ) from exc
        else:  # legacy_wall_trace
            _deref_observation(claim.trace_ref)
            if claim.basis_evidence_ref is not None:
                _deref_pointer(
                    claim.basis_evidence_ref.input_id,
                    claim.basis_evidence_ref.source_output_sha256,
                    claim.basis_evidence_ref.json_pointer,
                )
        # F-2 (checked AFTER dereference on purpose: a ref naming a
        # never-frozen input must keep reporting UNKNOWN_INPUT_ID, the
        # sharper diagnosis -- cross-input spanning is the residual case
        # every dereference above happily survives).
        _assert_claim_refs_single_sourced(claim)

    # -- opening claims: reference integrity incl. NF-4 #4 (gap_index) -----
    opening_seen: set[tuple[str, str]] = set()
    for opening in bundle.opening_claims:
        if opening.opening_id != opening.source_ref.observation_id:
            raise EvidenceContractError(
                "OPENING_ID_MISMATCH",
                {
                    "opening_id": opening.opening_id,
                    "ref_observation_id": opening.source_ref.observation_id,
                },
            )
        key = (opening.source_ref.input_id, opening.opening_id)
        if key in opening_seen:
            raise EvidenceContractError(
                "DUPLICATE_OPENING_CLAIM", {"opening_id": opening.opening_id}
            )
        opening_seen.add(key)
        node = _deref_observation(opening.source_ref)
        face_line = node.get("face_line")
        gap_index = node.get("gap_index")
        if not isinstance(face_line, str) or not isinstance(gap_index, int):
            raise EvidenceContractError(
                "OPENING_NODE_NOT_REFERENCE_SHAPED",
                {"opening_id": opening.opening_id},
            )
        gaps = gaps_of.get(opening.source_ref.input_id, {}).get(face_line)
        if gaps is None:
            raise EvidenceContractError(
                "OPENING_FACE_LINE_UNKNOWN",
                {"opening_id": opening.opening_id, "face_line": face_line},
            )
        if not (0 <= gap_index < len(gaps)):
            raise EvidenceContractError(
                "OPENING_GAP_INDEX_OUT_OF_RANGE",
                {
                    "opening_id": opening.opening_id,
                    "face_line": face_line,
                    "gap_index": gap_index,
                    "gaps_len": len(gaps),
                },
            )

    # -- B3 (2026-09-03): elevation opening claims -- value↔byte equality ----
    # ⭐ The whole point of this block: a z that cannot be traced back to its
    # frozen byte is a bare value, and a z whose VALUE drifted from the byte
    # it names is a carrier swap.  Both are loud here, exactly (== over two
    # parses of the same JSON literal), ⛔ never a tolerance.
    elevation_opening_seen: set[tuple[str, str]] = set()
    for claim in bundle.elevation_opening_claims:
        if claim.opening_id != claim.source_ref.observation_id:
            raise EvidenceContractError(
                "ELEVATION_OPENING_ID_MISMATCH",
                {
                    "opening_id": claim.opening_id,
                    "ref_observation_id": claim.source_ref.observation_id,
                },
            )
        key = (claim.source_ref.input_id, claim.opening_id)
        if key in elevation_opening_seen:
            raise EvidenceContractError(
                "DUPLICATE_ELEVATION_OPENING_CLAIM",
                {"opening_id": claim.opening_id},
            )
        elevation_opening_seen.add(key)
        _deref_observation(claim.source_ref)
        if claim.opening_id not in elevation_opening_index.get(
            claim.source_ref.input_id, {}
        ):
            raise EvidenceContractError(
                "ELEVATION_OPENING_CLAIM_SOURCE_UNKNOWN",
                {
                    "input_id": claim.source_ref.input_id,
                    "opening_id": claim.opening_id,
                },
            )
        for value, ref, which in (
            (claim.z_low_m, claim.z_low_ref, "z_low"),
            (claim.z_high_m, claim.z_high_ref, "z_high"),
        ):
            byte = _deref_pointer(
                ref.input_id, ref.source_output_sha256, ref.json_pointer
            )
            if isinstance(byte, bool) or not isinstance(byte, (int, float)):
                raise EvidenceContractError(
                    "ELEVATION_Z_SOURCE_NOT_NUMERIC",
                    {
                        "opening_id": claim.opening_id,
                        "which": which,
                        "pointer": ref.json_pointer,
                        "got": type(byte).__name__,
                    },
                )
            if value != byte:
                raise EvidenceContractError(
                    "ELEVATION_Z_VALUE_DRIFTED_FROM_SOURCE",
                    {
                        "opening_id": claim.opening_id,
                        "which": which,
                        "pointer": ref.json_pointer,
                        "claim_value": value,
                        "frozen_byte": byte,
                    },
                )
        # F-2 for the elevation leg: one claim, one frozen source.
        claim_inputs = {
            claim.source_ref.input_id,
            claim.z_low_ref.input_id,
            claim.z_high_ref.input_id,
        }
        if len(claim_inputs) != 1:
            raise EvidenceContractError(
                "CLAIM_REFS_SPAN_MULTIPLE_INPUTS",
                {
                    "opening_id": claim.opening_id,
                    "input_ids": sorted(claim_inputs),
                },
            )

    # -- B3 (2026-09-03): floor level claims -- rule mirror + value↔byte ----
    floor_claim_seen: set[tuple[str, str]] = set()
    claimed_levels: dict[str, set[str]] = {}
    for claim in bundle.floor_level_claims:
        if claim.structure_line_id != claim.source_ref.observation_id:
            raise EvidenceContractError(
                "FLOOR_LINE_ID_MISMATCH",
                {
                    "structure_line_id": claim.structure_line_id,
                    "ref_observation_id": claim.source_ref.observation_id,
                },
            )
        key = (claim.source_ref.input_id, claim.structure_line_id)
        if key in floor_claim_seen:
            raise EvidenceContractError(
                "DUPLICATE_FLOOR_LEVEL_CLAIM",
                {"structure_line_id": claim.structure_line_id},
            )
        floor_claim_seen.add(key)
        node = _deref_observation(claim.source_ref)
        if node.get("constant_quantity") != "z":
            raise EvidenceContractError(
                "FLOOR_LINE_NOT_HORIZONTAL",
                {
                    "input_id": claim.source_ref.input_id,
                    "structure_line_id": claim.structure_line_id,
                    "constant_quantity": node.get("constant_quantity"),
                    "rule": FLOOR_LEVEL_SELECTION_RULE,
                },
            )
        byte = _deref_pointer(
            claim.z_ref.input_id,
            claim.z_ref.source_output_sha256,
            claim.z_ref.json_pointer,
        )
        if isinstance(byte, bool) or not isinstance(byte, (int, float)):
            raise EvidenceContractError(
                "FLOOR_LEVEL_SOURCE_NOT_NUMERIC",
                {
                    "structure_line_id": claim.structure_line_id,
                    "pointer": claim.z_ref.json_pointer,
                    "got": type(byte).__name__,
                },
            )
        if claim.z_m != byte:
            raise EvidenceContractError(
                "FLOOR_LEVEL_VALUE_DRIFTED_FROM_SOURCE",
                {
                    "structure_line_id": claim.structure_line_id,
                    "pointer": claim.z_ref.json_pointer,
                    "claim_value": claim.z_m,
                    "frozen_byte": byte,
                },
            )
        claim_inputs = {claim.source_ref.input_id, claim.z_ref.input_id}
        if len(claim_inputs) != 1:
            raise EvidenceContractError(
                "CLAIM_REFS_SPAN_MULTIPLE_INPUTS",
                {
                    "structure_line_id": claim.structure_line_id,
                    "input_ids": sorted(claim_inputs),
                },
            )
        claimed_levels.setdefault(claim.source_ref.input_id, set()).add(
            claim.structure_line_id
        )

    # ⭐ T4's exit-side check (recompute-gate-must-mirror-producer-definition):
    # the bundle's level set must BE what FLOOR_LEVEL_SELECTION_RULE selects
    # from the frozen source -- a dropped level and an invented one are both
    # loud.  This is the rule's enforcement, ⛔ not the adapter's promise.
    for input_id, selected in floor_lines.items():
        claimed = claimed_levels.get(input_id, set())
        if claimed != set(selected):
            raise EvidenceContractError(
                "FLOOR_LADDER_NOT_EXHAUSTIVE",
                {
                    "input_id": input_id,
                    "rule": FLOOR_LEVEL_SELECTION_RULE,
                    "selected_by_rule": sorted(selected),
                    "claimed": sorted(claimed),
                    "dropped": sorted(set(selected) - claimed),
                    "invented": sorted(claimed - set(selected)),
                },
            )

    # -- invariant 2: every as-drawn face line has exactly one disposition --
    claims_by_id: dict[str, WallClaimV1] = {c.claim_id: c for c in bundle.wall_claims}
    if len(claims_by_id) != len(bundle.wall_claims):
        raise EvidenceContractError(
            "DUPLICATE_CLAIM_ID",
            {"count": len(bundle.wall_claims)},
        )
    disposition_seen: dict[tuple[str, str], FaceDispositionV1] = {}
    for disposition in bundle.face_dispositions:
        _deref_observation(disposition.face_ref)
        key = (
            disposition.face_ref.input_id,
            disposition.face_ref.observation_id,
        )
        if key in disposition_seen:
            raise EvidenceContractError(
                "DUPLICATE_DISPOSITION",
                {"input_id": key[0], "observation_id": key[1]},
            )
        disposition_seen[key] = disposition
        if disposition.status != "claimed_wall":
            if disposition.reason_ref is not None:
                _deref_pointer(
                    disposition.reason_ref.input_id,
                    disposition.reason_ref.source_output_sha256,
                    disposition.reason_ref.json_pointer,
                )

    for input_id, index in face_index.items():
        for face_id in index:
            if (input_id, face_id) not in disposition_seen:
                raise EvidenceContractError(
                    "FACE_WITHOUT_DISPOSITION",
                    {"input_id": input_id, "observation_id": face_id},
                )
    for key in disposition_seen:
        if key[0] in face_index and key[1] not in face_index[key[0]]:
            raise EvidenceContractError(
                "DISPOSITION_REFERENCES_UNKNOWN_FACE",
                {"input_id": key[0], "observation_id": key[1]},
            )

    # claim <-> disposition closure: a claimed face is sold to exactly one
    # claim; a non-wall / ambiguous face is consumed by none (§4.2).
    sold_to: dict[tuple[str, str], set[str]] = {}
    for claim in bundle.wall_claims:
        refs: list[ObservationRefV1] = []
        if claim.kind == "paired_faces":
            refs = [claim.face_a_ref, claim.face_b_ref]
        elif claim.kind == "solid_band":
            refs = [claim.band_face_ref]
        elif claim.kind == "single_face":
            refs = [claim.face_ref]
        else:
            refs = [claim.trace_ref]
        for ref in refs:
            if ref.input_id in face_index:
                key = (ref.input_id, ref.observation_id)
                sold_to.setdefault(key, set()).add(claim.claim_id)
    for key, claim_ids in sold_to.items():
        disposition = disposition_seen.get(key)
        if disposition is None:
            raise EvidenceContractError(
                "FACE_WITHOUT_DISPOSITION",
                {"input_id": key[0], "observation_id": key[1]},
            )
        if disposition.status != "claimed_wall":
            raise EvidenceContractError(
                "FACE_CLAIMED_AND_DISPOSITIONED",
                {
                    "input_id": key[0],
                    "observation_id": key[1],
                    "status": disposition.status,
                },
            )
        if claim_ids != {disposition.consuming_claim_id}:
            raise EvidenceContractError(
                "FACE_SOLD_TO_TWO_CLAIMS",
                {
                    "input_id": key[0],
                    "observation_id": key[1],
                    "claims": sorted(claim_ids),
                },
            )
    for key, disposition in disposition_seen.items():
        if (
            disposition.status == "claimed_wall"
            and key[0] in face_index
            and key not in sold_to
        ):
            raise EvidenceContractError(
                "CLAIMED_FACE_WITH_NO_CLAIM",
                {"input_id": key[0], "observation_id": key[1],
                 "consuming_claim_id": disposition.consuming_claim_id},
            )

    # -- §4.2: an ambiguous abstention is a known-missing evidence debt ----
    debt_ids = {d.debt_id for d in bundle.evidence_debts}
    if len(debt_ids) != len(bundle.evidence_debts):
        raise EvidenceContractError("DUPLICATE_DEBT_ID", {})
    ambiguous_debts: set[tuple[str, str]] = set()
    for debt in bundle.evidence_debts:
        if debt.kind == "ambiguous_face":
            for ref in debt.affected_refs:
                ambiguous_debts.add((ref.input_id, ref.json_pointer))
        if debt.kind == "missing_channel":
            if debt.channel is None:
                raise EvidenceContractError(
                    "MISSING_CHANNEL_DEBT_WITHOUT_CHANNEL",
                    {"debt_id": debt.debt_id},
                )
    covered_channels: dict[str, list[str]] = {}
    for status in bundle.channel_status:
        if status.channel in covered_channels:
            raise EvidenceContractError(
                "DUPLICATE_CHANNEL_STATUS", {"channel": status.channel}
            )
        covered_channels[status.channel] = list(status.covered_by_debt_ids)
        for debt_id in status.covered_by_debt_ids:
            if debt_id not in debt_ids:
                raise EvidenceContractError(
                    "CHANNEL_DEBT_UNKNOWN",
                    {"channel": status.channel, "debt_id": debt_id},
                )
        for input_id in status.source_input_ids:
            if input_id not in frozen:
                raise EvidenceContractError(
                    "CHANNEL_SOURCE_UNKNOWN",
                    {"channel": status.channel, "input_id": input_id},
                )
    # F-1: present must mean payload (or an explicit zero-payload debt)
    _assert_channel_payload_closure(bundle)
    # B-1 (rework-2): that payload must come from the channel's declared
    # sources, and a declared source with no payload needs a source-scoped
    # debt -- a channel-granularity debt cannot stand in for it.
    _assert_channel_source_closure(bundle, frozen)
    for disposition in bundle.face_dispositions:
        if disposition.status != "ambiguous":
            continue
        ref = disposition.face_ref
        if (ref.input_id, ref.json_pointer) not in ambiguous_debts:
            raise EvidenceContractError(
                "AMBIGUOUS_WITHOUT_EVIDENCE_DEBT",
                {
                    "input_id": ref.input_id,
                    "observation_id": ref.observation_id,
                },
            )

    # -- single_face observed_unclaimed: counterface disposition agrees ----
    for claim in bundle.wall_claims:
        if claim.kind != "single_face":
            continue
        if claim.counterface_state != "observed_unclaimed":
            continue
        ref = claim.counterface_observation_ref
        other = disposition_seen.get((ref.input_id, ref.observation_id))
        if other is None or other.status != claim.counterface_disposition_status:
            raise EvidenceContractError(
                "COUNTERFACE_DISPOSITION_DISAGREES",
                {
                    "claim_id": claim.claim_id,
                    "counterface": ref.observation_id,
                    "claim_says": claim.counterface_disposition_status,
                    "ledger_says": None if other is None else other.status,
                },
            )

    # -- invariant 8: canonical order + content hash agree ------------------
    if canonical_sha256(_sorted_bundle(bundle)) != bundle.content_sha256:
        raise EvidenceContractError(
            "CONTENT_HASH_MISMATCH",
            {"declared": bundle.content_sha256},
        )
    _assert_canonical_order(bundle)


def _assert_canonical_order(bundle: CorrectionEvidenceBundleV1) -> None:
    """Invariant 8's ordering half, with precise codes (the hash alone would
    already catch any ordering drift -- this names it)."""
    ids = [a.input_id for a in bundle.source_artifacts]
    if ids != sorted(ids):
        raise EvidenceContractError("SOURCE_ARTIFACTS_UNORDERED", {})
    chans = [c.channel for c in bundle.channel_status]
    if chans != sorted(chans):
        raise EvidenceContractError("CHANNEL_STATUS_UNORDERED", {})
    claims = [c.claim_id for c in bundle.wall_claims]
    if claims != sorted(claims):
        raise EvidenceContractError("WALL_CLAIMS_UNORDERED", {})
    disp = [
        (d.face_ref.input_id, d.face_ref.observation_id)
        for d in bundle.face_dispositions
    ]
    if disp != sorted(disp):
        raise EvidenceContractError("FACE_DISPOSITIONS_UNORDERED", {})
    openings = [
        (o.source_ref.input_id, o.opening_id) for o in bundle.opening_claims
    ]
    if openings != sorted(openings):
        raise EvidenceContractError("OPENING_CLAIMS_UNORDERED", {})
    elev = [
        (o.source_ref.input_id, o.opening_id)
        for o in bundle.elevation_opening_claims
    ]
    if elev != sorted(elev):
        raise EvidenceContractError("ELEVATION_OPENING_CLAIMS_UNORDERED", {})
    floors = [
        (c.source_ref.input_id, c.structure_line_id)
        for c in bundle.floor_level_claims
    ]
    if floors != sorted(floors):
        raise EvidenceContractError("FLOOR_LEVEL_CLAIMS_UNORDERED", {})
    debts = [d.debt_id for d in bundle.evidence_debts]
    if debts != sorted(debts):
        raise EvidenceContractError("EVIDENCE_DEBTS_UNORDERED", {})


__all__ = [
    "BUNDLE_SCHEMA_VERSION",
    "CHANNELS",
    "CHANNEL_PAYLOAD_MEMBERS",
    "FLOOR_LEVEL_SELECTION_RULE",
    "MIN_FLOOR_LEVELS",
    "SOURCE_CONTRACT_AS_DRAWN",
    "SOURCE_CONTRACT_AS_DRAWN_ELEVATION",
    "SOURCE_CONTRACT_LEGACY",
    "ArtifactPointerV1",
    "ChannelStatusV1",
    "CorrectionEvidenceBundleArtifactV1",
    "CorrectionEvidenceBundleV1",
    "ElevationOpeningClaimV1",
    "EvidenceContractError",
    "DebtObligationV1",
    "EvidenceDebtV1",
    "FaceDispositionV1",
    "FloorLevelClaimV1",
    "FrozenSourceV1",
    "LegacyWallTraceClaimV1",
    "ObservationRefV1",
    "OpeningClaimV1",
    "PairedFacesWallClaimV1",
    "SingleFaceWallClaimV1",
    "SolidBandWallClaimV1",
    "SourceArtifactV1",
    "WallClaimV1",
    "as_drawn_elevation_floor_lines",
    "as_drawn_elevation_opening_index",
    "as_drawn_face_index",
    "finalize_bundle",
    "resolve_json_pointer",
    "validate_evidence_bundle",
    "wall_claim_id",
]
