"""②-2 module 4: the provisional wall compiler (2026-08-31).

WHAT THIS MODULE IS
-------------------
The deterministic provisional compiler of the approved ②-2 design (§5.1):

    resolve source refs → segment evidence → derive provisional wall
    support-lines → wall IR + open items

Input is ONE validated ``CorrectionEvidenceBundleArtifactV1`` (module 2's
bundle + its frozen sources; module 3's adapters are the production
builders).  Output is a :class:`WallCompilationV1`: provisional
:class:`ResolvedWallV1` records (design §5.4) carrying the segmentation, the
code-derived midlines, the THREE thickness names, and the open items with
their symbolic candidates -- the material the decision packet (module 5)
will adjudicate and the executor (module 6) will execute.  No model is
called, no prompt is read, and ⛔ nothing here wires the pipeline: this
module imports no orchestration and no judge code; flipping the as-drawn
disposition is module 7's on-purpose act.

THE EIGHT MANDATES, AND WHERE EACH LANDS
----------------------------------------
1. **Ref resolve inside frozen bytes only** (design §3.2).  Every reference
   is resolved with ``resolve_json_pointer`` against the artifact's own
   ``raw_bytes``; this module performs ⛔ no file input of any kind, so it
   cannot re-read a working directory that may have changed.
2. **Segmentation is faithful in BOTH directions** (design §4.1's fidelity
   rule, §9.1 step 4).  ``paired_faces`` compiles to a double-face wall only
   over the interval where the two faces JOINTLY cover; each unshared tail
   becomes a :class:`SingleFaceFragmentV1` that still names the claim it
   came from.  ⛔ No intersection-drop, ⛔ no union-as-double, and equal
   coverage produces ZERO fragments (an unconditional shredder fails that
   direction).
3. **The midline is derived HERE and only here** (design §5.1 / the batch
   guide's hard rule).  Reading emits no centerline field, the bundle
   carries none, the adapters translate none; the compiler computes support
   lines from the two faces / the band's own edges and ⛔ never writes any
   derived value back into the bundle or the source bytes (pure function;
   a test locks the round-trip byte-identity).
4. **The three thickness names never merge** (design §5.3).
   ``observed_face_spacing_m`` is recomputed from the two face nodes (or
   the band's own two edges) -- ⛔ never the pair node's cached
   ``spacing_m``; ``resolved_thickness_m`` stays ``None`` until a decision
   executes; ``thickness_resolution`` is the audit record whose
   ``source_values[]`` provenance kinds separate observation / declaration
   / match result.  ``matched_declared_mm`` stays a label: a SNAP
   candidate's value comes from the DECLARED callout, and the label only
   rides along as extra evidence when it names the same millimetres.
5. **``basis=unknown`` + a non-null thickness never becomes a silent
   identity** (design §5.2.1 / §4.4 #9).  The thickness is a SCALE for
   ``OFFSET_POSITIVE`` / ``OFFSET_NEGATIVE`` candidates only;
   ``IDENTITY_AS_CENTERLINE`` is ⛔ not a value of the operation enum at
   all -- the path does not exist at the type layer -- and an item that
   entered ``open_items`` is closed ONLY by an explicit decision: this
   module contains no auto rule that touches any open item, so the §6.1
   unique-candidate / Pareto rule, whatever module 6 builds of it, can
   never reach an already-opened item through here.
6. **``ambiguous`` debt is consumed, never skimmed past** (design §7.2;
   module 3's cross-review F-2).  The compiler performs the dependency
   analysis -- for every ambiguous face, how many pair candidates it
   participates in -- and then: the ``strict`` profile BLOCKS loudly,
   naming every debt and the undecided ratio, remedy
   ``wall_level_reperception``; the ``exploratory`` profile continues with
   ``completion="degraded"``, the debts as residual, and the undecided
   ratio reported on the face ledger.  ⛔ Not a docstring sentence, not a
   silent skip.
7. **The candidate graph is walked in full** (NF-4 #5's module-4 half,
   pinned by module 2).  Both faces of every ``pair_candidates`` entry --
   selected or not -- must resolve inside the frozen face index; a dangling
   ``face_b`` nobody selected dies here even if a bundle reached this layer
   through a path that skipped the adapter's own walk.
8. **Solid bands stay bands** (design §9.1 step 4 / §9.2).  A
   ``solid_band`` claim compiles to exactly one wall whose observed spacing
   is the band's own edge-to-edge width; no partner face is invented.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
Openings (their host protocol is not re-designed here -- the claims travel
on the bundle untouched); topology and ``boundary_role`` (design §5.4:
determined by the topology stage after support lines exist -- always
``None`` from this compiler); cost vectors, packets and response schemas
(module 5); the executor loop and any profile gate beyond the ambiguous
block (module 6); the signed-sidecar basis protocol (module 3 recorded the
wiring-day item).  ``output_basis`` reuses the judge side's existing
``wall_axis`` / ``outer_skin`` vocabulary by reference only (design §5.4);
this compiler emits ``wall_axis`` for every support line it derives and
emits nothing otherwise -- no signed rule produces an ``outer_skin`` output
today, so that value is never minted here.
"""
from __future__ import annotations

import json
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict

from src.agent.correction.evidence_contract import (
    SOURCE_CONTRACT_AS_DRAWN,
    ArtifactPointerV1,
    CorrectionEvidenceBundleArtifactV1,
    EvidenceContractError,
    ObservationRefV1,
    as_drawn_face_index,
    resolve_json_pointer,
    validate_evidence_bundle,
)
from src.agent.correction.window_sources import Hex64, canonical_sha256

COMPILATION_SCHEMA_VERSION = "wall_compilation_v1"

#: The one banned symbolic operation (design §4.4 #9).  It travels as an
#: EXCLUSION name on open items whose basis lacks centerline evidence; it is
#: ⛔ not a member of ``SymbolicOperation`` -- a candidate carrying it cannot
#: be constructed, which is the only lock that cannot be swapped.
IDENTITY_BAN = "IDENTITY_AS_CENTERLINE"

_CFG = ConfigDict(extra="forbid", strict=True)

SymbolicOperation = Literal[
    "KEEP_OBSERVED_WIDTH",
    "SNAP_TO_DECLARATION",
    "OFFSET_POSITIVE",
    "OFFSET_NEGATIVE",
]
ObservedBasis = Literal[
    "two_observed_faces",
    "ink_band_edges",
    "single_observed_face",
    "centerline",
    "wall_face",
    "outer_skin",
    "unknown",
]


class WallCompilerError(ValueError):
    """A loud compiler-policy refusal, with a stable ``code``.

    Two families live here: profile policy (an ``ambiguous`` debt blocking
    the strict profile) and decision faults (an unknown item/candidate, a
    duplicate decision).  Input-INTEGRITY faults reuse module 2's
    :class:`EvidenceContractError`, so an adapter-time and a compiler-time
    refusal stay one family of teeth, never two opinions.
    """

    def __init__(self, code: str, context: dict | None = None):
        self.code = code
        self.context = dict(context or {})
        super().__init__(f"{code}: {self.context}")


# ── interval arithmetic (pure; ⛔ no thresholds anywhere) ──────────────────── #
def _union(pieces: Sequence[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    out: list[tuple[float, float]] = []
    for lo, hi in sorted(pieces):
        if out and lo <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return tuple(out)


def _intersect(
    a: tuple[tuple[float, float], ...], b: tuple[tuple[float, float], ...]
) -> tuple[tuple[float, float], ...]:
    out: list[tuple[float, float]] = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if lo < hi:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return _union(out)


def _subtract(
    a: tuple[tuple[float, float], ...], b: tuple[tuple[float, float], ...]
) -> tuple[tuple[float, float], ...]:
    """``a`` minus ``b``: the stretches covered by ``a`` and not by ``b``."""
    out: list[tuple[float, float]] = []
    for lo, hi in a:
        cur = lo
        for blo, bhi in b:
            if bhi <= cur or blo >= hi:
                continue
            if blo > cur:
                out.append((cur, blo))
            cur = max(cur, bhi)
        if cur < hi:
            out.append((cur, hi))
    return _union(out)


# ── the IR (design §5.4, plus module 4's segmentation detail) ─────────────── #
class ThicknessSourceRecordV1(BaseModel):
    """One thickness NUMBER with its provenance.  ⭐ The four kinds map onto
    the acceptance's three source families: ``observed_spacing`` = the
    measured quantity (recomputed from the face/band nodes),
    ``declared_callout`` / ``declared_field`` = declarations (the drawing's
    callout list / a typed field on the source stroke), ``matched_label`` =
    a match result (the pair node's ``matched_declared_mm`` -- a label,
    ⛔ never a snap value by itself)."""

    model_config = _CFG

    provenance: Literal[
        "observed_spacing", "declared_callout", "declared_field", "matched_label"
    ]
    value_m: float
    source_refs: tuple[ArtifactPointerV1, ...]


class SymbolicCandidateV1(BaseModel):
    """One candidate the packet may adjudicate.  Coordinates appear only as
    CODE-COMPUTED previews; the operation itself is symbolic (design §6.1).
    ⭐ ``IDENTITY_AS_CENTERLINE`` is not expressible on this type."""

    model_config = _CFG

    candidate_id: str
    symbolic_operation: SymbolicOperation
    thickness_source: ThicknessSourceRecordV1 | None = None
    preview_constant_pos_m: float | None = None
    preview_thickness_m: float | None = None
    preview_delta_m: float | None = None


class SingleFaceFragmentV1(BaseModel):
    """One unshared tail of a paired wall: a stretch of ONE face the partner
    face does not cover.  ⭐ Still owned by the claim it came from (design
    §4.1's fidelity rule) -- the fragment is evidence, not a new wall."""

    model_config = _CFG

    fragment_id: str
    source_claim_id: str
    tail_of: Literal["face_a", "face_b"]
    face_ref: ObservationRefV1
    along_interval_m: tuple[float, float]


class WallCenterlineV1(BaseModel):
    """A code-derived support line, in world metres.  ``p1_m``/``p2_m`` span
    the EVIDENCE extent -- ⛔ not wall corners: corners come from
    neighbouring support-line intersections downstream (design §5.2)."""

    model_config = _CFG

    constant_world_axis: Literal["x", "y"] | None
    constant_pos_m: float | None
    p1_m: tuple[float, float]
    p2_m: tuple[float, float]


class ThicknessResolutionV1(BaseModel):
    """The audit record of an executed thickness choice (design §5.3)."""

    model_config = _CFG

    operation_id: str
    source_values: tuple[ThicknessSourceRecordV1, ...]
    decision_id: str
    delta_m: float | None


class ResolvedWallV1(BaseModel):
    """One wall's provisional IR (design §5.4).  Fields still awaiting a
    decision are ``None`` -- an open item always exists for such a wall, and
    the compilation's ``completion`` stays ``degraded`` while any is unset."""

    model_config = _CFG

    wall_id: str
    source_claim_ids: tuple[str, ...]
    source_refs: tuple[ObservationRefV1, ...]
    claim_kind: Literal[
        "paired_faces", "solid_band", "single_face", "legacy_wall_trace"
    ]
    resolved_centerline: WallCenterlineV1 | None
    resolved_along_intervals: tuple[tuple[float, float], ...]
    double_face_intervals: tuple[tuple[float, float], ...] = ()
    unshared_tail_fragments: tuple[SingleFaceFragmentV1, ...] = ()
    observed_face_spacing_m: float | None
    resolved_thickness_m: float | None
    observed_basis: ObservedBasis
    output_basis: Literal["wall_axis", "outer_skin"] | None
    boundary_role: str | None = None
    thickness_resolution: ThicknessResolutionV1 | None = None
    derivation_hash: Hex64 | None = None


class OpenItemV1(BaseModel):
    """Something only a decision or a re-perception can close.  ⭐ There is
    ⛔ no auto path: this compiler closes an item ONLY on an explicit
    :class:`FixedDecisionV1`."""

    model_config = _CFG

    item_id: str
    kind: Literal[
        "thickness_resolution",
        "axis_offset_undetermined",
        "legacy_basis_unknown",
        "legacy_trace_non_orthogonal",
    ]
    scope_entity_ids: tuple[str, ...]
    phenomenon: str
    source_refs: tuple[ArtifactPointerV1, ...]
    candidates: tuple[SymbolicCandidateV1, ...]
    why_not_auto_resolved: str
    exclusions: tuple[str, ...] = ()


class AutoActionV1(BaseModel):
    """A code action taken without any model, with its rule id on the record
    (design §6.1's auto table)."""

    model_config = _CFG

    action_id: str
    kind: Literal[
        "honor_non_wall_declaration",
        "derive_two_face_midline",
        "derive_band_midline",
        "identity_axis_from_centerline_evidence",
    ]
    scope_entity_ids: tuple[str, ...]
    source_refs: tuple[ArtifactPointerV1, ...]
    rule_id: str


class AmbiguousFaceAnalysisV1(BaseModel):
    """The dependency analysis design §7.2 orders for every ambiguous face:
    how many pair candidates it participates in (measured on the frozen
    candidate graph), i.e. exactly how much wall topology is still
    undecided on this one line."""

    model_config = _CFG

    face_ref: ObservationRefV1
    debt_id: str
    candidate_participation: int
    topology_exposure: Literal["candidate_graph", "no_candidate"]
    why_code_cannot_decide: str


class SourceUndecidedStatsV1(BaseModel):
    model_config = _CFG

    input_id: str
    disposed_face_lines: int
    ambiguous_face_lines: int
    undecided_ratio: float


class FaceUndecidedStatsV1(BaseModel):
    """The undecided ratio module 3's cross-review F-2 asked for: ``present``
    is a two-valued word that cannot say "80% of the face lines are still
    debt" -- these counts can, and they travel on every compilation."""

    model_config = _CFG

    disposed_face_lines: int
    ambiguous_face_lines: int
    undecided_ratio: float
    per_source: tuple[SourceUndecidedStatsV1, ...]


class FixedDecisionV1(BaseModel):
    """A fixed decision for the pure-code / fixed-decision fixtures of
    design §9.1 step 4.  The response SCHEMA is module 5's; this type is
    deliberately the minimal binding -- an item and one of ITS candidates."""

    model_config = _CFG

    item_id: str
    candidate_id: str


class AppliedDecisionV1(BaseModel):
    model_config = _CFG

    item_id: str
    candidate_id: str
    symbolic_operation: SymbolicOperation
    scope_entity_ids: tuple[str, ...]


class WallCompilationV1(BaseModel):
    """The compiler's whole result.  Deterministic: the same artifact plus
    the same decisions serialize byte-identically (canonical sort +
    ``content_sha256``, mirroring module 2's bundle discipline)."""

    model_config = _CFG

    schema_version: Literal["wall_compilation_v1"]
    profile: Literal["strict", "exploratory"]
    bundle_content_sha256: Hex64
    walls: list[ResolvedWallV1] = []
    open_items: list[OpenItemV1] = []
    auto_actions: list[AutoActionV1] = []
    applied_decisions: list[AppliedDecisionV1] = []
    ambiguous_analysis: list[AmbiguousFaceAnalysisV1] = []
    completion: Literal["complete", "degraded"]
    undecided: FaceUndecidedStatsV1 | None = None
    residual_debt_ids: tuple[str, ...] = ()
    content_sha256: Hex64 | None = None


# ── per-compile context (frozen bytes, one entry per source) ──────────────── #
class _Ctx:
    """Everything the per-kind compilers and the decision applier need,
    derived once from the artifact.  ⭐ No file access after construction:
    ``docs``/``shas`` come from the artifact's own frozen bytes."""

    def __init__(self, artifact: CorrectionEvidenceBundleArtifactV1):
        self.docs: dict[str, dict] = {}
        self.shas: dict[str, str] = {}
        self.contracts: dict[str, str] = {}
        for source in artifact.frozen_sources:
            meta = source.artifact
            self.docs[meta.input_id] = json.loads(
                source.raw_bytes.decode("utf-8")
            )
            self.shas[meta.input_id] = meta.source_output_sha256
            self.contracts[meta.input_id] = meta.source_contract_id
        self.callouts = {
            input_id: self._callouts(input_id) for input_id in self.docs
        }
        # wall_id -> the claim's hypothesis pointer (matched-label reads)
        self.hyp_ref: dict[str, ArtifactPointerV1] = {}
        # wall_id -> (world axis, anchor constant, along coverage) for the
        # walls whose axis is an OFFSET away (single faces, non-centerline
        # legacy traces).  The decision applier reuses exactly this anchor,
        # so a preview and an executed offset can never disagree.
        self.anchor: dict[
            str, tuple[str, float, tuple[tuple[float, float], ...]]
        ] = {}

    def _callouts(
        self, input_id: str
    ) -> tuple[tuple[float, ArtifactPointerV1], ...]:
        """The drawing's own thickness callouts as ``(value_m, pointer)``.

        A channel nobody consumes could stay untyped; THIS module consumes
        it, so its dialect is declared here: absent/``None`` → no callouts;
        anything but a list of numbers is a loud
        ``DECLARED_CALLOUTS_MALFORMED``.
        """
        node = (self.docs[input_id].get("declarations") or {}).get(
            "thickness_callouts_mm"
        )
        if node is None:
            return ()
        if not isinstance(node, list):
            raise EvidenceContractError(
                "DECLARED_CALLOUTS_MALFORMED",
                {"input_id": input_id, "got": type(node).__name__},
            )
        out: list[tuple[float, ArtifactPointerV1]] = []
        seen: set[float] = set()
        for i, value in enumerate(node):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise EvidenceContractError(
                    "DECLARED_CALLOUTS_MALFORMED",
                    {"input_id": input_id, "index": i,
                     "got": type(value).__name__},
                )
            metres = float(value) / 1000.0
            if metres in seen:
                continue
            seen.add(metres)
            out.append((metres, ArtifactPointerV1(
                input_id=input_id,
                source_contract_id=self.contracts[input_id],
                source_output_sha256=self.shas[input_id],
                json_pointer=f"/declarations/thickness_callouts_mm/{i}",
            )))
        return tuple(sorted(out, key=lambda pair: pair[0]))


# ── small resolve/validate helpers (frozen bytes only) ────────────────────── #
def _face_node(ref: ObservationRefV1, docs: dict[str, dict]) -> dict:
    node = resolve_json_pointer(docs[ref.input_id], ref.json_pointer)
    if not isinstance(node, dict) or node.get("id") != ref.observation_id:
        raise EvidenceContractError(
            "OBSERVATION_NODE_MISMATCH",
            {"input_id": ref.input_id, "pointer": ref.json_pointer,
             "ref_says": ref.observation_id},
        )
    return node


def _num(value: object, *, what: str, observation_id: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvidenceContractError(
            "FACE_GEOMETRY_MALFORMED",
            {"observation_id": observation_id, "what": what,
             "got": type(value).__name__},
        )
    return float(value)


def _runs(node: dict, *, observation_id: str) -> tuple[tuple[float, float], ...]:
    runs = node.get("runs_m")
    if not isinstance(runs, list) or not runs:
        raise EvidenceContractError(
            "FACE_GEOMETRY_MALFORMED",
            {"observation_id": observation_id, "what": "runs_m",
             "got": "empty or not a list"},
        )
    pieces: list[tuple[float, float]] = []
    for run in runs:
        if not isinstance(run, list) or len(run) != 2:
            raise EvidenceContractError(
                "FACE_GEOMETRY_MALFORMED",
                {"observation_id": observation_id, "what": "runs_m element",
                 "got": repr(run)[:60]},
            )
        lo = _num(run[0], what="runs_m lo", observation_id=observation_id)
        hi = _num(run[1], what="runs_m hi", observation_id=observation_id)
        if not lo < hi:
            raise EvidenceContractError(
                "FACE_GEOMETRY_MALFORMED",
                {"observation_id": observation_id, "what": "runs_m element",
                 "got": [lo, hi]},
            )
        pieces.append((lo, hi))
    return _union(pieces)


def _world_axis(node: dict, *, observation_id: str) -> str:
    axis = node.get("constant_world_axis")
    if axis not in ("x", "y"):
        raise EvidenceContractError(
            "FACE_GEOMETRY_MALFORMED",
            {"observation_id": observation_id,
             "what": "constant_world_axis", "got": repr(axis)},
        )
    return axis


def _support_line(
    world_axis: str, constant_pos_m: float,
    along: tuple[tuple[float, float], ...],
) -> WallCenterlineV1:
    lo, hi = along[0][0], along[-1][1]
    if world_axis == "x":
        return WallCenterlineV1(
            constant_world_axis="x", constant_pos_m=constant_pos_m,
            p1_m=(constant_pos_m, lo), p2_m=(constant_pos_m, hi),
        )
    return WallCenterlineV1(
        constant_world_axis="y", constant_pos_m=constant_pos_m,
        p1_m=(lo, constant_pos_m), p2_m=(hi, constant_pos_m),
    )


def _offset_candidates(
    wall_id: str,
    anchor_pos_m: float,
    sources: Sequence[tuple[str, float, ArtifactPointerV1]],
) -> list[SymbolicCandidateV1]:
    """``OFFSET_POSITIVE`` / ``OFFSET_NEGATIVE`` over every thickness scale.

    ⭐ The axis sits half a thickness from the observed face/trace; both
    signs are enumerated because nothing in the evidence names the wall's
    side.  ``IDENTITY_AS_CENTERLINE`` is not among the operations -- it is
    unrepresentable, not merely filtered out.
    """
    out: list[SymbolicCandidateV1] = []
    for provenance, value, pointer in sources:
        for op, sign in (("OFFSET_POSITIVE", 1.0), ("OFFSET_NEGATIVE", -1.0)):
            out.append(SymbolicCandidateV1(
                candidate_id="cand_" + canonical_sha256(
                    {"wall": wall_id, "op": op, "value": value}
                ),
                symbolic_operation=op,  # type: ignore[arg-type]
                thickness_source=ThicknessSourceRecordV1(
                    provenance=provenance,  # type: ignore[arg-type]
                    value_m=value, source_refs=(pointer,),
                ),
                preview_constant_pos_m=anchor_pos_m + sign * value / 2.0,
            ))
    return out


# ── per-kind compilation ──────────────────────────────────────────────────── #
def _thickness_candidates(
    wall_id: str, spacing: float, observed_ref, callouts
) -> list[SymbolicCandidateV1]:
    """KEEP + one SNAP per declared callout -- the keep-vs-snap choice design
    §5.2 leaves to adjudication even for a fully observed wall."""
    out = [
        SymbolicCandidateV1(
            candidate_id="cand_" + canonical_sha256(
                {"wall": wall_id, "op": "KEEP_OBSERVED_WIDTH"}
            ),
            symbolic_operation="KEEP_OBSERVED_WIDTH",
            thickness_source=ThicknessSourceRecordV1(
                provenance="observed_spacing", value_m=spacing,
                source_refs=observed_ref,
            ),
            preview_thickness_m=spacing, preview_delta_m=0.0,
        )
    ]
    out.extend(
        SymbolicCandidateV1(
            candidate_id="cand_" + canonical_sha256(
                {"wall": wall_id, "op": "SNAP_TO_DECLARATION", "value": value}
            ),
            symbolic_operation="SNAP_TO_DECLARATION",
            thickness_source=ThicknessSourceRecordV1(
                provenance="declared_callout", value_m=value,
                source_refs=(pointer,),
            ),
            preview_thickness_m=value, preview_delta_m=value - spacing,
        )
        for value, pointer in callouts
    )
    return out


_THICKNESS_WHY = (
    "keep-observed vs snap-to-declaration is a design decision "
    "(design §5.2); no signed rule exists, and this module has no auto "
    "path for open items"
)


def _compile_paired(claim, ctx: _Ctx) -> tuple[ResolvedWallV1, list, list]:
    docs = ctx.docs
    node_a = _face_node(claim.face_a_ref, docs)
    node_b = _face_node(claim.face_b_ref, docs)
    oid_a, oid_b = claim.face_a_ref.observation_id, claim.face_b_ref.observation_id
    axis_a = _world_axis(node_a, observation_id=oid_a)
    axis_b = _world_axis(node_b, observation_id=oid_b)
    if axis_a != axis_b:
        raise EvidenceContractError(
            "PAIR_CONSTANT_WORLD_AXIS_DISAGREE",
            {"claim_id": claim.claim_id, "face_a": axis_a, "face_b": axis_b},
        )
    pos_a = _num(node_a.get("pos_m"), what="pos_m", observation_id=oid_a)
    pos_b = _num(node_b.get("pos_m"), what="pos_m", observation_id=oid_b)
    cov_a = _runs(node_a, observation_id=oid_a)
    cov_b = _runs(node_b, observation_id=oid_b)
    joint = _intersect(cov_a, cov_b)
    tails_a = _subtract(cov_a, cov_b)
    tails_b = _subtract(cov_b, cov_a)
    along = _union(cov_a + cov_b)
    spacing = abs(pos_b - pos_a)  # ⭐ recomputed; the pair node's cache is unread
    wall_id = "wall_" + canonical_sha256(
        {"claim_ids": [claim.claim_id], "kind": "paired_faces"}
    )
    ctx.hyp_ref[wall_id] = claim.hypothesis_ref
    fragments = [
        SingleFaceFragmentV1(
            fragment_id="frag_" + canonical_sha256(
                {"claim": claim.claim_id, "face": oid, "interval": [lo, hi]}
            ),
            source_claim_id=claim.claim_id, tail_of=tail_of,
            face_ref=ref, along_interval_m=(lo, hi),
        )
        for ref, oid, tail_of, tails in (
            (claim.face_a_ref, oid_a, "face_a", tails_a),
            (claim.face_b_ref, oid_b, "face_b", tails_b),
        )
        for lo, hi in tails
    ]
    wall = ResolvedWallV1(
        wall_id=wall_id,
        source_claim_ids=(claim.claim_id,),
        source_refs=(claim.face_a_ref, claim.face_b_ref),
        claim_kind="paired_faces",
        resolved_centerline=_support_line(axis_a, (pos_a + pos_b) / 2.0, along),
        resolved_along_intervals=along,
        double_face_intervals=joint,
        unshared_tail_fragments=tuple(
            sorted(fragments, key=lambda f: f.fragment_id)
        ),
        observed_face_spacing_m=spacing,
        resolved_thickness_m=None,
        observed_basis="two_observed_faces",
        output_basis="wall_axis",
    )
    action = AutoActionV1(
        action_id="auto_" + canonical_sha256(
            {"kind": "derive_two_face_midline", "wall": wall_id}
        ),
        kind="derive_two_face_midline", scope_entity_ids=(wall_id,),
        source_refs=(claim.face_a_ref, claim.face_b_ref),
        rule_id="auto/midline_from_two_faces",
    )
    item = OpenItemV1(
        item_id="item_" + canonical_sha256(
            {"kind": "thickness_resolution", "scope": [wall_id]}
        ),
        kind="thickness_resolution", scope_entity_ids=(wall_id,),
        phenomenon=(
            f"observed face spacing {spacing:.6g} m (recomputed from the "
            f"two face nodes); declared callouts "
            f"{sorted(v for v, _ in ctx.callouts[claim.face_a_ref.input_id])}"
            f" m; joint coverage {len(joint)} piece(s), unshared tails "
            f"{len(fragments)}"
        ),
        source_refs=(claim.hypothesis_ref, claim.face_a_ref, claim.face_b_ref),
        candidates=tuple(_thickness_candidates(
            wall_id, spacing, (claim.face_a_ref, claim.face_b_ref),
            ctx.callouts[claim.face_a_ref.input_id],
        )),
        why_not_auto_resolved=_THICKNESS_WHY,
    )
    return wall, [item], [action]


def _compile_solid_band(claim, ctx: _Ctx) -> tuple[ResolvedWallV1, list, list]:
    node = _face_node(claim.band_face_ref, ctx.docs)
    oid = claim.band_face_ref.observation_id
    axis = _world_axis(node, observation_id=oid)
    edges = node.get("edges_m")
    if not isinstance(edges, list) or len(edges) != 2:
        raise EvidenceContractError(
            "FACE_GEOMETRY_MALFORMED",
            {"observation_id": oid, "what": "edges_m", "got": repr(edges)[:60]},
        )
    lo = _num(edges[0], what="edges_m lo", observation_id=oid)
    hi = _num(edges[1], what="edges_m hi", observation_id=oid)
    if not lo < hi:
        raise EvidenceContractError(
            "FACE_GEOMETRY_MALFORMED",
            {"observation_id": oid, "what": "edges_m", "got": [lo, hi]},
        )
    along = _runs(node, observation_id=oid)
    spacing = hi - lo  # the band's own two edges ARE the wall's two faces
    wall_id = "wall_" + canonical_sha256(
        {"claim_ids": [claim.claim_id], "kind": "solid_band"}
    )
    ctx.hyp_ref[wall_id] = claim.hypothesis_ref
    wall = ResolvedWallV1(
        wall_id=wall_id,
        source_claim_ids=(claim.claim_id,),
        source_refs=(claim.band_face_ref,),
        claim_kind="solid_band",
        resolved_centerline=_support_line(axis, (lo + hi) / 2.0, along),
        resolved_along_intervals=along,
        observed_face_spacing_m=spacing,
        resolved_thickness_m=None,
        observed_basis="ink_band_edges",
        output_basis="wall_axis",
    )
    action = AutoActionV1(
        action_id="auto_" + canonical_sha256(
            {"kind": "derive_band_midline", "wall": wall_id}
        ),
        kind="derive_band_midline", scope_entity_ids=(wall_id,),
        source_refs=(claim.band_face_ref,),
        rule_id="auto/midline_from_band_edges",
    )
    item = OpenItemV1(
        item_id="item_" + canonical_sha256(
            {"kind": "thickness_resolution", "scope": [wall_id]}
        ),
        kind="thickness_resolution", scope_entity_ids=(wall_id,),
        phenomenon=(
            f"observed band width {spacing:.6g} m (recomputed from the "
            f"band's own two edges); declared callouts "
            f"{sorted(v for v, _ in ctx.callouts[claim.band_face_ref.input_id])}"
            " m"
        ),
        source_refs=(claim.hypothesis_ref, claim.band_face_ref),
        candidates=tuple(_thickness_candidates(
            wall_id, spacing, (claim.band_face_ref,),
            ctx.callouts[claim.band_face_ref.input_id],
        )),
        why_not_auto_resolved=_THICKNESS_WHY,
    )
    return wall, [item], [action]


def _compile_single_face(claim, ctx: _Ctx) -> tuple[ResolvedWallV1, list, list]:
    node = _face_node(claim.face_ref, ctx.docs)
    oid = claim.face_ref.observation_id
    axis = _world_axis(node, observation_id=oid)
    pos = _num(node.get("pos_m"), what="pos_m", observation_id=oid)
    along = _runs(node, observation_id=oid)
    wall_id = "wall_" + canonical_sha256(
        {"claim_ids": [claim.claim_id], "kind": "single_face"}
    )
    ctx.hyp_ref[wall_id] = claim.hypothesis_ref
    ctx.anchor[wall_id] = (axis, pos, along)
    sources = [
        ("declared_callout", value, pointer)
        for value, pointer in ctx.callouts[claim.face_ref.input_id]
    ]
    candidates = _offset_candidates(wall_id, pos, sources)
    wall = ResolvedWallV1(
        wall_id=wall_id,
        source_claim_ids=(claim.claim_id,),
        source_refs=(claim.face_ref,),
        claim_kind="single_face",
        resolved_centerline=None,  # ⛔ no side/thickness basis: no silent axis
        resolved_along_intervals=along,
        observed_face_spacing_m=None,
        resolved_thickness_m=None,
        observed_basis="single_observed_face",
        output_basis=None,
    )
    if candidates:
        why = (
            "one observed face: neither the wall's side nor its thickness "
            "is in the evidence; only symbolic offsets are enumerable "
            "(design §5.2)"
        )
    else:
        why = (
            "one observed face and NO thickness scale in this source: the "
            "candidate set is empty, so the legal exits are an explicit "
            "model decision, wall-level re-perception, or a degraded "
            "profile -- never a silent axis"
        )
    item = OpenItemV1(
        item_id="item_" + canonical_sha256(
            {"kind": "axis_offset_undetermined", "scope": [wall_id]}
        ),
        kind="axis_offset_undetermined", scope_entity_ids=(wall_id,),
        phenomenon=(
            f"single observed face at {axis}={pos:.6g} m covering "
            f"{len(along)} piece(s); thickness scales available: "
            f"{sorted(v for _, v, _ in sources)}"
        ),
        source_refs=(claim.hypothesis_ref, claim.face_ref),
        candidates=tuple(candidates),
        why_not_auto_resolved=why,
        exclusions=(IDENTITY_BAN,),
    )
    return wall, [item], []


def _legacy_trace_geometry(claim, docs):
    node = _face_node(claim.trace_ref, docs)
    oid = claim.trace_ref.observation_id
    geometry = node.get("geometry")
    if not isinstance(geometry, dict):
        raise EvidenceContractError(
            "LEGACY_TRACE_GEOMETRY_MALFORMED",
            {"observation_id": oid, "what": "geometry",
             "got": type(geometry).__name__},
        )
    endpoints: list[tuple[float, float]] = []
    for key in ("p1", "p2"):
        point = geometry.get(key)
        if (
            not isinstance(point, list) or len(point) != 2
            or isinstance(point[0], bool) or isinstance(point[1], bool)
            or not isinstance(point[0], (int, float))
            or not isinstance(point[1], (int, float))
        ):
            raise EvidenceContractError(
                "LEGACY_TRACE_GEOMETRY_MALFORMED",
                {"observation_id": oid, "what": key, "got": repr(point)[:60]},
            )
        endpoints.append((float(point[0]), float(point[1])))
    thickness = geometry.get("thickness_m")
    if thickness is not None and (
        isinstance(thickness, bool) or not isinstance(thickness, (int, float))
    ):
        raise EvidenceContractError(
            "LEGACY_TRACE_GEOMETRY_MALFORMED",
            {"observation_id": oid, "what": "thickness_m",
             "got": type(thickness).__name__},
        )
    return node, endpoints, (
        None if thickness is None else float(thickness)
    )


def _compile_legacy_trace(claim, ctx: _Ctx) -> tuple[ResolvedWallV1, list, list]:
    docs = ctx.docs
    node, (p1, p2), typed_m = _legacy_trace_geometry(claim, docs)
    oid = claim.trace_ref.observation_id
    thickness_pointer = ArtifactPointerV1(
        input_id=claim.trace_ref.input_id,
        source_contract_id=claim.trace_ref.source_contract_id,
        source_output_sha256=claim.trace_ref.source_output_sha256,
        json_pointer=claim.trace_ref.json_pointer + "/geometry/thickness_m",
    )
    if p1 == p2:
        raise EvidenceContractError(
            "LEGACY_TRACE_DEGENERATE", {"observation_id": oid}
        )
    if p1[0] == p2[0]:
        world_axis, constant = "x", p1[0]
        along = ((min(p1[1], p2[1]), max(p1[1], p2[1])),)
    elif p1[1] == p2[1]:
        world_axis, constant = "y", p1[1]
        along = ((min(p1[0], p2[0]), max(p1[0], p2[0])),)
    else:
        world_axis, constant, along = None, None, ()
    wall_id = "wall_" + canonical_sha256(
        {"claim_ids": [claim.claim_id], "kind": "legacy_wall_trace"}
    )
    ctx.hyp_ref[wall_id] = claim.hypothesis_ref

    if world_axis is None:
        wall = ResolvedWallV1(
            wall_id=wall_id, source_claim_ids=(claim.claim_id,),
            source_refs=(claim.trace_ref,), claim_kind="legacy_wall_trace",
            resolved_centerline=None, resolved_along_intervals=(),
            observed_face_spacing_m=None, resolved_thickness_m=None,
            observed_basis=claim.source_basis, output_basis=None,
        )
        item = OpenItemV1(
            item_id="item_" + canonical_sha256(
                {"kind": "legacy_trace_non_orthogonal", "scope": [wall_id]}
            ),
            kind="legacy_trace_non_orthogonal", scope_entity_ids=(wall_id,),
            phenomenon=(
                f"trace endpoints {list(p1)} -> {list(p2)} share neither "
                "world axis; the orthogonal kernel cannot host it"
            ),
            source_refs=(claim.trace_ref,),
            candidates=(),
            why_not_auto_resolved=(
                "no orthogonal support line exists for this trace; the "
                "geometry needs a decision or re-perception, not a tolerance"
            ),
        )
        return wall, [item], []

    if claim.source_basis == "centerline":
        # The ONE place identity is legal: structured centerline evidence,
        # gated by module 2's basis rule, with the declaration pointer
        # travelling on the auto action.
        wall = ResolvedWallV1(
            wall_id=wall_id, source_claim_ids=(claim.claim_id,),
            source_refs=(claim.trace_ref,), claim_kind="legacy_wall_trace",
            resolved_centerline=_support_line(world_axis, constant, along),
            resolved_along_intervals=along,
            observed_face_spacing_m=None, resolved_thickness_m=None,
            observed_basis="centerline", output_basis="wall_axis",
        )
        action = AutoActionV1(
            action_id="auto_" + canonical_sha256(
                {"kind": "identity_axis_from_centerline_evidence",
                 "wall": wall_id}
            ),
            kind="identity_axis_from_centerline_evidence",
            scope_entity_ids=(wall_id,),
            source_refs=(
                claim.trace_ref,
                *([claim.basis_evidence_ref]
                  if claim.basis_evidence_ref is not None else []),
            ),
            rule_id="auto/identity_from_structured_centerline",
        )
        items: list[OpenItemV1] = []
        if typed_m is not None:
            items.append(OpenItemV1(
                item_id="item_" + canonical_sha256(
                    {"kind": "thickness_resolution", "scope": [wall_id]}
                ),
                kind="thickness_resolution", scope_entity_ids=(wall_id,),
                phenomenon=(
                    f"axis fixed by structured centerline evidence; typed "
                    f"thickness scale present: {typed_m}"
                ),
                source_refs=(claim.trace_ref, thickness_pointer),
                candidates=(
                    SymbolicCandidateV1(
                        candidate_id="cand_" + canonical_sha256(
                            {"wall": wall_id, "op": "SNAP_TO_DECLARATION",
                             "value": typed_m}
                        ),
                        symbolic_operation="SNAP_TO_DECLARATION",
                        thickness_source=ThicknessSourceRecordV1(
                            provenance="declared_field", value_m=typed_m,
                            source_refs=(thickness_pointer,),
                        ),
                        preview_thickness_m=typed_m,
                    ),
                ),
                why_not_auto_resolved=(
                    "adopting a declared thickness is a design decision; "
                    "the axis itself needed no decision"
                ),
            ))
        return wall, items, [action]

    # wall_face / outer_skin / unknown: the axis is an offset away, and the
    # side is not in the evidence.
    sources: list[tuple[str, float, ArtifactPointerV1]] = []
    if typed_m is not None:
        sources.append(("declared_field", typed_m, thickness_pointer))
    ctx.anchor[wall_id] = (world_axis, constant, along)
    candidates = _offset_candidates(wall_id, constant, sources)
    wall = ResolvedWallV1(
        wall_id=wall_id, source_claim_ids=(claim.claim_id,),
        source_refs=(claim.trace_ref,), claim_kind="legacy_wall_trace",
        resolved_centerline=None, resolved_along_intervals=along,
        observed_face_spacing_m=None, resolved_thickness_m=None,
        observed_basis=claim.source_basis, output_basis=None,
    )
    kind = (
        "legacy_basis_unknown" if claim.source_basis == "unknown"
        else "axis_offset_undetermined"
    )
    if candidates:
        why = (
            f"basis={claim.source_basis} with a typed thickness scale: the "
            "offset magnitude is known, the side is not -- identity is "
            "excluded because the trace is not evidenced as a centerline"
        )
    else:
        why = (
            f"basis={claim.source_basis} and no typed thickness: the "
            "candidate set is empty (identity excluded), so the legal exits "
            "are an explicit decision, wall-level re-perception, or a "
            "degraded profile -- never a silent axis"
        )
    item = OpenItemV1(
        item_id="item_" + canonical_sha256(
            {"kind": kind, "scope": [wall_id]}
        ),
        kind=kind,  # type: ignore[arg-type]
        scope_entity_ids=(wall_id,),
        phenomenon=(
            f"trace at {world_axis}={constant:.6g} m over "
            f"{along[0][0]:.6g}..{along[0][1]:.6g} m; thickness scales "
            f"available: {sorted(v for _, v, _ in sources)}"
        ),
        source_refs=(claim.trace_ref,),
        candidates=tuple(candidates),
        why_not_auto_resolved=why,
        exclusions=(IDENTITY_BAN,),
    )
    return wall, [item], []


# ── decision application (the ONLY closer of open items) ──────────────────── #
def _matched_labels(ctx: _Ctx, wall: ResolvedWallV1) -> list | None:
    """The hypothesis node's ``matched_declared_mm`` label list, or ``None``.

    ⭐ A label, never a value: it only ever rides ALONG a snap whose value
    came from a declaration (design §5.3).
    """
    pointer = ctx.hyp_ref.get(wall.wall_id)
    if pointer is None:
        return None
    node = resolve_json_pointer(
        ctx.docs[wall.source_refs[0].input_id], pointer.json_pointer
    )
    labels = node.get("matched_declared_mm") if isinstance(node, dict) else None
    return labels if isinstance(labels, list) else None


def _apply_decision(
    ctx: _Ctx,
    walls_by_id: dict[str, ResolvedWallV1],
    item: OpenItemV1,
    decision: FixedDecisionV1,
    candidate: SymbolicCandidateV1,
) -> ResolvedWallV1:
    wall = walls_by_id[item.scope_entity_ids[0]]
    if candidate.symbolic_operation == "KEEP_OBSERVED_WIDTH":
        observed = wall.observed_face_spacing_m
        if observed is None or candidate.thickness_source is None:
            raise WallCompilerError(
                "DECISION_CANDIDATE_NOT_APPLICABLE",
                {"item_id": item.item_id, "candidate_id": decision.candidate_id},
            )
        return wall.model_copy(update={
            "resolved_thickness_m": observed,
            "thickness_resolution": ThicknessResolutionV1(
                operation_id="KEEP_OBSERVED_WIDTH",
                source_values=(candidate.thickness_source,),
                decision_id=f"fixed:{decision.candidate_id}",
                delta_m=0.0,
            ),
        })
    if candidate.symbolic_operation == "SNAP_TO_DECLARATION":
        source = candidate.thickness_source
        if source is None:
            raise WallCompilerError(
                "DECISION_CANDIDATE_NOT_APPLICABLE",
                {"item_id": item.item_id, "candidate_id": decision.candidate_id},
            )
        source_values = [source]
        labels = _matched_labels(ctx, wall)
        if labels is not None and round(source.value_m * 1000.0, 6) in {
            round(float(v), 6) for v in labels
        }:
            source_values.append(ThicknessSourceRecordV1(
                provenance="matched_label", value_m=source.value_m,
                source_refs=(ctx.hyp_ref[wall.wall_id],),
            ))
        observed = wall.observed_face_spacing_m
        return wall.model_copy(update={
            "resolved_thickness_m": source.value_m,
            "thickness_resolution": ThicknessResolutionV1(
                operation_id="SNAP_TO_DECLARATION",
                source_values=tuple(source_values),
                decision_id=f"fixed:{decision.candidate_id}",
                delta_m=(
                    None if observed is None
                    else source.value_m - observed
                ),
            ),
        })
    # OFFSET_POSITIVE / OFFSET_NEGATIVE
    source = candidate.thickness_source
    anchor = ctx.anchor.get(wall.wall_id)
    if source is None or anchor is None:
        raise WallCompilerError(
            "DECISION_CANDIDATE_NOT_APPLICABLE",
            {"item_id": item.item_id, "candidate_id": decision.candidate_id},
        )
    axis, _, along = anchor
    return wall.model_copy(update={
        "resolved_centerline": _support_line(
            axis, candidate.preview_constant_pos_m, along
        ),
        "output_basis": "wall_axis",
        "resolved_thickness_m": source.value_m,
        "thickness_resolution": ThicknessResolutionV1(
            operation_id=candidate.symbolic_operation,
            source_values=(source,),
            decision_id=f"fixed:{decision.candidate_id}",
            delta_m=None,
        ),
    })


# ── the entry point ───────────────────────────────────────────────────────── #
def compile_wall_ir(
    artifact: CorrectionEvidenceBundleArtifactV1,
    *,
    profile: Literal["strict", "exploratory"] = "strict",
    decisions: Sequence[FixedDecisionV1] = (),
) -> WallCompilationV1:
    """Compile ONE validated evidence artifact into provisional wall IR.

    Pure and deterministic: same frozen bytes + same decisions ⇒
    byte-identical output.  ``strict`` refuses (loudly, by name) any bundle
    whose ``ambiguous`` debts are still undecided; ``exploratory`` continues
    with ``completion="degraded"`` and the undecided ratio on the record.
    """
    validate_evidence_bundle(artifact)  # idempotent entry integrity gate
    bundle = artifact.bundle
    ctx = _Ctx(artifact)

    # -- mandate 7: walk the WHOLE candidate graph of every as-drawn source,
    #    dereferencing both faces of every entry, selected or not ----------
    participation: dict[str, dict[str, int]] = {}
    for source in artifact.frozen_sources:
        meta = source.artifact
        if meta.source_contract_id != SOURCE_CONTRACT_AS_DRAWN:
            continue
        index = as_drawn_face_index(ctx.docs[meta.input_id])
        counts: dict[str, int] = {}
        candidates = (
            ctx.docs[meta.input_id].get("hypotheses") or {}
        ).get("pair_candidates") or []
        for k, cand in enumerate(candidates):
            if not isinstance(cand, dict):
                raise EvidenceContractError(
                    "PAIR_CANDIDATE_NODE_MALFORMED",
                    {"input_id": meta.input_id, "candidate_index": k},
                )
            for side in ("face_a", "face_b"):
                fid = cand.get(side)
                if fid not in index:
                    raise EvidenceContractError(
                        "PAIR_CANDIDATE_REFERENCES_UNKNOWN_FACE",
                        {
                            "input_id": meta.input_id,
                            "candidate_index": k,
                            "side": side,
                            "observation_id": fid,
                            "selected": False,
                        },
                    )
                counts[fid] = counts.get(fid, 0) + 1
        participation[meta.input_id] = counts

    # -- mandate 6: the ambiguous dependency analysis, then the profile gate
    debt_of: dict[tuple[str, str], str] = {}
    for debt in bundle.evidence_debts:
        if debt.kind != "ambiguous_face":
            continue
        for ref in debt.affected_refs:
            debt_of.setdefault((ref.input_id, ref.json_pointer), debt.debt_id)
    analysis: list[AmbiguousFaceAnalysisV1] = []
    per_source: dict[str, list[int]] = {}
    for disposition in bundle.face_dispositions:
        src = disposition.face_ref.input_id
        per_source.setdefault(src, [0, 0])
        per_source[src][0] += 1
        if disposition.status != "ambiguous":
            continue
        per_source[src][1] += 1
        ref = disposition.face_ref
        count = participation.get(src, {}).get(ref.observation_id, 0)
        analysis.append(AmbiguousFaceAnalysisV1(
            face_ref=ref,
            debt_id=debt_of[(src, ref.json_pointer)],
            candidate_participation=count,
            topology_exposure="candidate_graph" if count else "no_candidate",
            why_code_cannot_decide=(
                "reading abstained on this face line; it participates in "
                f"{count} pair candidate(s) the code must not adjudicate "
                "-- deciding wall-ness needs the image, which correction "
                "never sees"
            ),
        ))
    analysis.sort(key=lambda a: (a.face_ref.input_id, a.face_ref.observation_id))
    disposed_total = sum(v[0] for v in per_source.values())
    ambiguous_total = sum(v[1] for v in per_source.values())
    stats: FaceUndecidedStatsV1 | None = None
    if per_source:
        stats = FaceUndecidedStatsV1(
            disposed_face_lines=disposed_total,
            ambiguous_face_lines=ambiguous_total,
            undecided_ratio=(
                ambiguous_total / disposed_total if disposed_total else 0.0
            ),
            per_source=tuple(
                SourceUndecidedStatsV1(
                    input_id=input_id, disposed_face_lines=v[0],
                    ambiguous_face_lines=v[1],
                    undecided_ratio=(v[1] / v[0]) if v[0] else 0.0,
                )
                for input_id, v in sorted(per_source.items())
            ),
        )
    if profile == "strict" and analysis:
        raise WallCompilerError(
            "AMBIGUOUS_DEBT_BLOCKS_STRICT_PROFILE",
            {
                "debt_ids": sorted({a.debt_id for a in analysis}),
                "ambiguous_faces": len(analysis),
                "disposed_face_lines": disposed_total,
                "undecided_ratio": (
                    ambiguous_total / disposed_total if disposed_total else 0.0
                ),
                "participation": [
                    {
                        "observation_id": a.face_ref.observation_id,
                        "debt_id": a.debt_id,
                        "candidate_count": a.candidate_participation,
                    }
                    for a in analysis
                ],
                "remedy": "wall_level_reperception",
            },
        )

    # -- per-claim compilation ----------------------------------------------
    walls_by_id: dict[str, ResolvedWallV1] = {}
    open_items: list[OpenItemV1] = []
    auto_actions: list[AutoActionV1] = []
    for claim in bundle.wall_claims:
        if claim.kind == "paired_faces":
            wall, items, actions = _compile_paired(claim, ctx)
        elif claim.kind == "solid_band":
            wall, items, actions = _compile_solid_band(claim, ctx)
        elif claim.kind == "single_face":
            wall, items, actions = _compile_single_face(claim, ctx)
        else:
            wall, items, actions = _compile_legacy_trace(claim, ctx)
        walls_by_id[wall.wall_id] = wall
        open_items.extend(items)
        auto_actions.extend(actions)

    # -- reading's negative assertions: honoured, excluded, accounted -------
    for disposition in bundle.face_dispositions:
        if disposition.status != "non_wall":
            continue
        ref = disposition.face_ref
        auto_actions.append(AutoActionV1(
            action_id="auto_" + canonical_sha256(
                {"kind": "honor_non_wall_declaration",
                 "face": [ref.input_id, ref.observation_id]}
            ),
            kind="honor_non_wall_declaration",
            scope_entity_ids=(f"{ref.input_id}:{ref.observation_id}",),
            source_refs=(ref, disposition.reason_ref),
            rule_id="auto/honor_non_wall_declaration",
        ))

    # -- decisions: the ONLY thing that closes an open item -----------------
    items_by_id = {i.item_id: i for i in open_items}
    applied: list[AppliedDecisionV1] = []
    closed: set[str] = set()
    for decision in decisions:
        item = items_by_id.get(decision.item_id)
        if item is None:
            raise WallCompilerError(
                "UNKNOWN_DECISION_ITEM",
                {"item_id": decision.item_id,
                 "available": sorted(items_by_id)},
            )
        if decision.item_id in closed:
            raise WallCompilerError(
                "DUPLICATE_DECISION_FOR_ITEM", {"item_id": decision.item_id}
            )
        candidate = next(
            (c for c in item.candidates
             if c.candidate_id == decision.candidate_id),
            None,
        )
        if candidate is None:
            raise WallCompilerError(
                "UNKNOWN_DECISION_CANDIDATE",
                {
                    "item_id": decision.item_id,
                    "candidate_id": decision.candidate_id,
                    "available": sorted(
                        c.candidate_id for c in item.candidates
                    ),
                },
            )
        wall = _apply_decision(ctx, walls_by_id, item, decision, candidate)
        walls_by_id[wall.wall_id] = wall
        closed.add(decision.item_id)
        applied.append(AppliedDecisionV1(
            item_id=decision.item_id, candidate_id=decision.candidate_id,
            symbolic_operation=candidate.symbolic_operation,
            scope_entity_ids=item.scope_entity_ids,
        ))
    open_items = [i for i in open_items if i.item_id not in closed]

    return _finalize(
        bundle_content_sha256=bundle.content_sha256,
        profile=profile,
        walls=[walls_by_id[k] for k in sorted(walls_by_id)],
        open_items=sorted(open_items, key=lambda i: i.item_id),
        auto_actions=sorted(auto_actions, key=lambda a: a.action_id),
        applied=sorted(applied, key=lambda d: (d.item_id, d.candidate_id)),
        analysis=analysis,
        stats=stats,
        residual_debt_ids=tuple(
            sorted(d.debt_id for d in bundle.evidence_debts)
        ),
    )


# ── canonical finalize (determinism) ──────────────────────────────────────── #
def _finalize(
    *,
    bundle_content_sha256: str,
    profile: str,
    walls: list[ResolvedWallV1],
    open_items: list[OpenItemV1],
    auto_actions: list[AutoActionV1],
    applied: list[AppliedDecisionV1],
    analysis: list[AmbiguousFaceAnalysisV1],
    stats,
    residual_debt_ids: tuple[str, ...],
) -> WallCompilationV1:
    hashed_walls: list[ResolvedWallV1] = []
    for wall in walls:
        data = wall.model_dump(mode="python")
        data.pop("derivation_hash", None)
        hashed_walls.append(wall.model_copy(update={
            "derivation_hash": canonical_sha256(data)
        }))
    complete = (
        not open_items
        and not residual_debt_ids
        and all(
            w.resolved_centerline is not None
            and w.resolved_thickness_m is not None
            and w.output_basis is not None
            for w in hashed_walls
        )
    )
    compilation = WallCompilationV1(
        schema_version=COMPILATION_SCHEMA_VERSION,
        profile=profile,  # type: ignore[arg-type]
        bundle_content_sha256=bundle_content_sha256,
        walls=hashed_walls,
        open_items=open_items,
        auto_actions=auto_actions,
        applied_decisions=applied,
        ambiguous_analysis=analysis,
        completion="complete" if complete else "degraded",
        undecided=stats,
        residual_debt_ids=residual_debt_ids,
    )
    content = compilation.model_dump(mode="python")
    content.pop("content_sha256", None)
    return compilation.model_copy(
        update={"content_sha256": canonical_sha256(content)}
    )


__all__ = [
    "COMPILATION_SCHEMA_VERSION",
    "IDENTITY_BAN",
    "AmbiguousFaceAnalysisV1",
    "AppliedDecisionV1",
    "AutoActionV1",
    "FaceUndecidedStatsV1",
    "FixedDecisionV1",
    "OpenItemV1",
    "ResolvedWallV1",
    "SingleFaceFragmentV1",
    "SymbolicCandidateV1",
    "ThicknessResolutionV1",
    "ThicknessSourceRecordV1",
    "WallCenterlineV1",
    "WallCompilationV1",
    "WallCompilerError",
    "compile_wall_ir",
]
