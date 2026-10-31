"""②-2 module 3: the reading→correction evidence adapters (2026-08-31; B3 leg 2026-09-03).

WHAT THIS MODULE IS
-------------------
The production translation from a frozen reading artifact into the unified
``CorrectionEvidenceBundleArtifactV1`` of module 2:

* :func:`adapt_as_drawn_plan`  -- an as-drawn v2 plan product → the four
  positive wall-claim kinds + the three-status face-disposition ledger;
* :func:`adapt_legacy_reading_view` -- a legacy ``ReadingView`` (with
  ``strokes``) → one ``legacy_wall_trace`` claim per ``pen == "wall"`` stroke;
* :func:`adapt_as_drawn_elevation` -- an as-drawn v0 elevation product →
  the ``elevation_openings`` and ``floor_levels`` channels: every opening's
  ``z_range_m`` and every horizontal structure line's ``pos_m`` as
  value-plus-provenance claims (B3).

All take FROZEN BYTES (identity anchors in the bytes, design §3.2), all
return the persistable artifact (bundle + frozen source), and all refuse
loudly -- with module 2's ``EvidenceContractError``, so an adapter-time
refusal and a validator-time refusal are the same family of teeth, never two
opinions.  ⛔ This module wires NOTHING: it does not touch the vector-contract
registry, the pipeline, or any judge code; calling it is module 7's decision.

THE THREE DESIGN MANDATES, AND WHERE EACH LANDS
-----------------------------------------------
1. Every as-drawn face line falls into EXACTLY ONE disposition (§4.2).  The
   adapter builds the ledger mechanically from the producer's five accounting
   slots (selected ``pairs`` + four buckets) and duplicates module 2's closure
   validator's logic ONLY through shared functions -- the claim↔disposition
   closure itself is checked by ``validate_evidence_bundle``, which every
   adapter entry point runs on its own output before returning.
2. ``legacy`` basis is ``unknown`` unless STRUCTURED evidence says otherwise
   (§4.1/§8.3).  ⛔ No free text is ever parsed here: the real fixture pair the
   design cites carries "outer skin line" vs "centreline" only in ``note`` --
   the adapter does not read ``note`` at all.  The ONE structured path honoured
   today is a typed ``geometry.basis`` key with a value in the closed domain;
   the signed-sidecar protocol of §8.3 is NOT implemented in this module (no
   real input carries one; recorded as a wiring-day item, not silently).
3. ``pair_candidates`` is not a fifth wall (§4.3).  The adapter ⛔ never
   invents a pairing when ``pairs`` is absent: a product whose selection is
   missing is a ``PAIRS_SELECTION_ABSENT`` refusal (reperception territory),
   never a nearest-neighbour leg rebuilt from the candidate graph.

THE PIN THIS MODULE RETIRES (module 2's acceptance 9)
-----------------------------------------------------
``test_nf4_5_unselected_dangling_candidate_passes_today_module3_4_pinned``
measured that a dangling ``pair_candidates[].face_b`` that no selected pair
uses passes module 2's layer.  The module-3 half is retired HERE: the adapter
walks the WHOLE candidate graph (selected or not) and dereferences both faces
of every entry (``PAIR_CANDIDATE_REFERENCES_UNKNOWN_FACE``).  The module-4
half (the compiler recomputing the graph) stays pinned to module 4.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
Segmentation of a paired wall's unshared tails into ``single_face_fragment``
pieces.  The approved design places evidence segmentation in the provisional
compiler (its pipeline is "resolve refs → segment evidence → derive support
lines"; module 4's charter is "ref resolve, segmentation, centerline/thickness
IR", and the module-4 build step is verified by "paired-face tails survive").
A bundle claim carries ⛔ no geometry values, so "which interval is a tail" --
a computed result -- has no type slot here.  What THIS module guarantees
instead (and its tests lock) is that the pairing decision and the full
segmentation evidence reach the bundle intact: both faces of a selected pair
stay consumed by ONE claim (never re-bucketed for having unequal runs), and
each face ref carries the pixel witnesses (``runs_px`` / ``runs_m`` / ...)
the compiler will segment from.
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from src.agent.correction.evidence_contract import (
    BUNDLE_SCHEMA_VERSION,
    FLOOR_LEVEL_SELECTION_RULE,
    MIN_FLOOR_LEVELS,
    SOURCE_CONTRACT_AS_DRAWN,
    SOURCE_CONTRACT_AS_DRAWN_ELEVATION,
    SOURCE_CONTRACT_LEGACY,
    ArtifactPointerV1,
    ChannelStatusV1,
    CorrectionEvidenceBundleArtifactV1,
    CorrectionEvidenceBundleV1,
    ElevationOpeningClaimV1,
    EvidenceContractError,
    EvidenceDebtV1,
    FaceDispositionV1,
    FloorLevelClaimV1,
    FrozenSourceV1,
    LegacyWallTraceClaimV1,
    ObservationRefV1,
    OpeningClaimV1,
    PairedFacesWallClaimV1,
    SingleFaceWallClaimV1,
    SolidBandWallClaimV1,
    SourceArtifactV1,
    WallClaimV1,
    as_drawn_elevation_floor_lines,
    as_drawn_elevation_opening_index,
    as_drawn_face_index,
    finalize_bundle,
    validate_evidence_bundle,
    wall_claim_id,
)
from src.agent.correction.window_sources import source_locator
from src.agent.reading.vector_contract import (
    CONTRACT_AS_DRAWN_ELEVATION_V0,
    CONTRACT_AS_DRAWN_PLAN,
    CONTRACT_READING_VIEW_LEGACY,
    classify_vector_json,
)

#: The closed value domain of a typed legacy basis declaration (§8.3).  ⭐ A
#: ``geometry.basis`` key outside this set is a MALFORMED declaration -- loud,
#: ⛔ never a quiet fall back to ``unknown`` (silence would swallow the
#: producer's intent behind a typo).
LEGACY_BASIS_VALUES: tuple[str, ...] = ("centerline", "wall_face", "outer_skin")

#: Channels an as-drawn plan product does not carry today.  Each becomes an
#: absent channel + an explicit ``missing_channel`` debt -- ⛔ never a silent
#: hole (the whole point of ``channel_status``, design §3.3).  (B3 note:
#: ``floor_levels`` joined this list on 2026-09-03 -- the plan family has no
#: storey ladder; only an elevation product does.)
_AS_DRAWN_ABSENT_CHANNELS: tuple[str, ...] = (
    "elevation_openings",
    "floor_levels",
    "dimensions",
    "room_roles",
)

#: Channels a legacy view does not feed through THIS adapter.  A legacy view
#: may well carry window/door strokes; this module translates walls only, so
#: those channels travel as absent + debt with the unadapted stroke count
#: named in the debt's description -- the honest shape, ⛔ not a quiet drop.
_LEGACY_ABSENT_CHANNELS: tuple[str, ...] = (
    "plan_openings",
    "elevation_openings",
    "floor_levels",
    "dimensions",
    "room_roles",
)

#: Channels an elevation product does not carry: it speaks only the two
#: channels of its leg (openings' vertical half + the storey ladder).  Each
#: travels absent + ``missing_channel`` debt, same discipline as above.
_ELEVATION_ABSENT_CHANNELS: tuple[str, ...] = (
    "walls",
    "plan_openings",
    "dimensions",
    "room_roles",
)


# ── shared small helpers ──────────────────────────────────────────────────── #
def _parse_json_bytes(raw: bytes, input_id: str) -> dict:
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceContractError(
            "SOURCE_NOT_JSON", {"input_id": input_id}
        ) from exc
    if not isinstance(doc, dict):
        raise EvidenceContractError(
            "SOURCE_NOT_OBJECT", {"input_id": input_id}
        )
    return doc


def _require_contract(doc: dict, raw: bytes, input_id: str, expected: str) -> None:
    """Every detector runs; the adapter refuses anything it is not the one for.

    This mirrors the classifier's own discipline (never first-match-wins): a
    hybrid file matching two contracts is refused here exactly as it is there.
    """
    decision = classify_vector_json(doc)
    if decision.contract_id != expected:
        raise EvidenceContractError(
            "ADAPTER_CONTRACT_MISMATCH",
            {
                "input_id": input_id,
                "expected": expected,
                "detected": decision.contract_id,
                "reason": decision.reason,
            },
        )


def _pointer(input_id: str, contract: str, sha: str, pointer: str
             ) -> ArtifactPointerV1:
    return ArtifactPointerV1(
        input_id=input_id, source_contract_id=contract,
        source_output_sha256=sha, json_pointer=pointer,
    )


def _observation_ref(input_id: str, contract: str, sha: str, pointer: str,
                     oid: str, witnesses: tuple[str, ...],
                     resolution: str) -> ObservationRefV1:
    return ObservationRefV1(
        input_id=input_id, source_contract_id=contract,
        source_output_sha256=sha, json_pointer=pointer,
        observation_id=oid,
        source_locator=source_locator(
            input_id=input_id, observation_id=oid, output_sha256=sha
        ),
        pixel_witness_pointers=witnesses,
        evidence_resolution=resolution,  # type: ignore[arg-type]
    )


def _missing_face(code: str, observation_id: str, **extra) -> EvidenceContractError:
    context = {"observation_id": observation_id, **extra}
    return EvidenceContractError(code, context)


def _close_and_freeze(bundle: CorrectionEvidenceBundleV1,
                      meta: SourceArtifactV1, raw: bytes
                      ) -> CorrectionEvidenceBundleArtifactV1:
    artifact = CorrectionEvidenceBundleArtifactV1(
        bundle=bundle,
        frozen_sources=[FrozenSourceV1(artifact=meta, raw_bytes=raw)],
    )
    # ⭐ Every adapter output is validated before it leaves this module: the
    # bundle a caller receives has already passed invariants 1-8.  An adapter
    # that could only produce invalid bundles thus fails AT THE CALL, never
    # downstream where its origin would be invisible.
    validate_evidence_bundle(artifact)
    return artifact


# ── the as-drawn adapter ──────────────────────────────────────────────────── #
def adapt_as_drawn_plan(
    raw: bytes,
    *,
    input_id: str,
    floor_ref: str,
    view_type: Literal["plan", "elevation"] = "plan",
) -> CorrectionEvidenceBundleArtifactV1:
    """Translate ONE frozen as-drawn v2 plan product into a validated bundle.

    ⭐ Identity: ``input_id`` is the caller-supplied view-manifest slot (⛔ the
    adapter never mints one from a file name); ``floor_ref`` / ``view_type``
    likewise name the semantic slot the caller is binding.  Deterministic: the
    same bytes through this function produce a byte-identical bundle.
    """
    doc = _parse_json_bytes(raw, input_id)
    _require_contract(doc, raw, input_id, CONTRACT_AS_DRAWN_PLAN)

    sha = hashlib.sha256(raw).hexdigest()
    contract = SOURCE_CONTRACT_AS_DRAWN
    meta = SourceArtifactV1(
        input_id=input_id, source_contract_id=contract,
        source_output_sha256=sha, view_type=view_type, floor_ref=floor_ref,
    )
    index = as_drawn_face_index(doc)  # duplicate face ids die here, loudly

    def face_ref(fid: str) -> ObservationRefV1:
        if fid not in index:
            raise _missing_face("FACE_REFERENCES_UNKNOWN_FACE", fid)
        base = f"/observations/face_lines/{index[fid][0]}"
        return _observation_ref(
            input_id, contract, sha, base, fid,
            (f"{base}/support_cols_px", f"{base}/runs_px",
             f"{base}/edges_m", f"{base}/gaps"),
            resolution="pixel_backed",
        )

    hyp = doc["hypotheses"]
    claims: list[WallClaimV1] = []
    dispositions: list[FaceDispositionV1] = []
    debts: list[EvidenceDebtV1] = []
    openings: list[OpeningClaimV1] = []

    # -- the candidate graph: walked IN FULL, both faces dereferenced --------
    # (module 2 pinned the unselected half of NF-4 #5 to this module: a
    # candidate entry nobody selected may still dangle, and only a full walk
    # sees it.  The graph is a review aid, ⛔ never a source of pairings.)
    candidates = list(hyp.get("pair_candidates") or [])
    candidate_at: dict[frozenset, int] = {}
    for k, cand in enumerate(candidates):
        for side in ("face_a", "face_b"):
            fid = cand.get(side)
            if fid not in index:
                raise EvidenceContractError(
                    "PAIR_CANDIDATE_REFERENCES_UNKNOWN_FACE",
                    {
                        "candidate_index": k,
                        "side": side,
                        "observation_id": fid,
                        "selected": False,
                    },
                )
        candidate_at.setdefault(
            frozenset((cand["face_a"], cand["face_b"])), k
        )

    # -- selected pairs: perception's choice, translated verbatim ------------
    pairs = hyp.get("pairs")
    pairs_status = hyp.get("pairs_status")
    pairs_absent = pairs is None
    if pairs_absent or not pairs:
        # ⛔ §4.3: an absent/empty selection is NEVER rebuilt from the graph.
        # What happens next depends on whether the producer's five accounting
        # slots still cover every face line:
        #   covered   → an honest product; it travels with a
        #               ``pairs_selection_absent`` debt (a known-missing the
        #               compiler turns into reperception), or no debt at all
        #               when the producer explicitly made an empty selection;
        #   uncovered → the producer's own invariant is broken; PAIRS foot
        #               the bill loudly rather than a vague closure error.
        accounted = set()
        for bucket in ("non_wall_face_lines", "unpaired_wall_faces",
                       "solid_band_walls", "ambiguous_face_lines"):
            accounted.update((hyp.get(bucket) or {}).keys())
        unaccounted = sorted(set(index) - accounted)
        if unaccounted:
            # Two diseases, two names: ``None`` means perception never chose
            # (the producer returns exactly this shape -- pairs=None with the
            # buckets left to perception's own declarations), so the pairing
            # gap is the disease; ``[]`` claims an empty selection WAS made,
            # so faces outside every bucket are a completeness break.
            code = (
                "PAIRS_SELECTION_ABSENT" if pairs_absent
                else "FACE_WITHOUT_DISPOSITION"
            )
            context: dict = {
                "input_id": input_id,
                "unaccounted_face_lines": unaccounted,
                "candidate_count": len(candidates),
            }
            if pairs_absent:
                context.update(
                    pairs_status=pairs_status, remedy="reperception_required"
                )
            raise EvidenceContractError(code, context)
        if pairs_absent:
            debts.append(EvidenceDebtV1(
                debt_id=f"debt_pairs_absent_{input_id}",
                kind="pairs_selection_absent",
                channel="walls",
                affected_refs=(
                    _pointer(input_id, contract, sha, "/hypotheses/pairs"),
                ),
                description=(
                    "perception supplied no wall pairing "
                    f"(pairs_status={pairs_status!r}); this adapter did not "
                    "invent one from the candidate graph"
                ),
                obligation=None,
            ))
    else:
        for j, pair in enumerate(pairs):
            for side in ("face_a", "face_b"):
                if pair.get(side) not in index:
                    raise _missing_face(
                        "SELECTED_PAIR_REFERENCES_UNKNOWN_FACE",
                        pair.get(side), pair_index=j, side=side,
                    )
            key = frozenset((pair["face_a"], pair["face_b"]))
            k = candidate_at.get(key)
            if k is None:
                # §4.3: a selected pair missing from the candidate graph is an
                # input error -- never a code-invented nearest-neighbour leg.
                raise EvidenceContractError(
                    "SELECTED_PAIR_NOT_IN_CANDIDATE_GRAPH",
                    {"pair_index": j, "faces": sorted(key)},
                )
            a, b = face_ref(pair["face_a"]), face_ref(pair["face_b"])
            claim = PairedFacesWallClaimV1(
                claim_id="pending",
                hypothesis_ref=_pointer(
                    input_id, contract, sha, f"/hypotheses/pairs/{j}"
                ),
                perception_source_ref=_pointer(
                    input_id, contract, sha,
                    "/hypotheses/perception_source"
                    if "perception_source" in hyp else "/hypotheses",
                ),
                source_contract_id=contract,
                face_a_ref=a, face_b_ref=b,
                pair_candidate_ref=_pointer(
                    input_id, contract, sha,
                    f"/hypotheses/pair_candidates/{k}",
                ),
            )
            claim = claim.model_copy(update={"claim_id": wall_claim_id(claim)})
            claims.append(claim)
            for ref in (a, b):
                dispositions.append(FaceDispositionV1(
                    face_ref=ref, status="claimed_wall",
                    consuming_claim_id=claim.claim_id,
                ))

    # -- the four buckets: reading's own dispositions, translated verbatim ---
    for bucket in ("solid_band_walls", "unpaired_wall_faces"):
        for fid, reason in (hyp.get(bucket) or {}).items():
            if fid not in index:
                raise _missing_face("BUCKET_KEY_REFERENCES_UNKNOWN_FACE", fid,
                                    bucket=bucket)
            ref = face_ref(fid)
            if bucket == "solid_band_walls":
                claim = SolidBandWallClaimV1(
                    claim_id="pending",
                    hypothesis_ref=_pointer(
                        input_id, contract, sha,
                        f"/hypotheses/solid_band_walls/{fid}",
                    ),
                    perception_source_ref=_pointer(
                        input_id, contract, sha,
                        "/hypotheses/perception_source"
                        if "perception_source" in hyp else "/hypotheses",
                    ),
                    source_contract_id=contract,
                    band_face_ref=ref,
                )
            else:
                # ⚠️ counterface_state is the MECHANICAL default.  Deriving the
                # sixth state (ink present, never promoted) would require
                # parsing the bucket's free prose -- ⛔ banned for code, so the
                # honest translation stops here until the producer emits
                # structure (design §4.1: prose is audit, never a fact).
                claim = SingleFaceWallClaimV1(
                    claim_id="pending",
                    hypothesis_ref=_pointer(
                        input_id, contract, sha,
                        f"/hypotheses/unpaired_wall_faces/{fid}",
                    ),
                    perception_source_ref=_pointer(
                        input_id, contract, sha,
                        "/hypotheses/perception_source"
                        if "perception_source" in hyp else "/hypotheses",
                    ),
                    source_contract_id=contract,
                    face_ref=ref,
                    original_reason=reason,
                    counterface_state="not_in_observations",
                )
            claim = claim.model_copy(update={"claim_id": wall_claim_id(claim)})
            claims.append(claim)
            dispositions.append(FaceDispositionV1(
                face_ref=ref, status="claimed_wall",
                consuming_claim_id=claim.claim_id,
            ))

    for fid in hyp.get("non_wall_face_lines") or {}:
        if fid not in index:
            raise _missing_face("BUCKET_KEY_REFERENCES_UNKNOWN_FACE", fid,
                                bucket="non_wall_face_lines")
        dispositions.append(FaceDispositionV1(
            face_ref=face_ref(fid), status="non_wall",
            reason_ref=_pointer(
                input_id, contract, sha,
                f"/hypotheses/non_wall_face_lines/{fid}",
            ),
        ))
    for n, fid in enumerate(hyp.get("ambiguous_face_lines") or {}):
        if fid not in index:
            raise _missing_face("BUCKET_KEY_REFERENCES_UNKNOWN_FACE", fid,
                                bucket="ambiguous_face_lines")
        ref = face_ref(fid)
        dispositions.append(FaceDispositionV1(
            face_ref=ref, status="ambiguous",
            reason_ref=_pointer(
                input_id, contract, sha,
                f"/hypotheses/ambiguous_face_lines/{fid}",
            ),
        ))
        debts.append(EvidenceDebtV1(
            debt_id=f"debt_amb_{input_id}_{n}", kind="ambiguous_face",
            channel="walls", affected_refs=(ref,),
            description="reading abstained on this face line",
            obligation=None,
        ))

    # -- opening candidates: by reference only (their business protocol is
    #    module 4+'s; reference integrity -- incl. gap_index range -- is
    #    module 2's, and the closing validate below enforces it) -------------
    for i, opening in enumerate(hyp.get("opening_candidates") or []):
        ref = _observation_ref(
            input_id, contract, sha,
            f"/hypotheses/opening_candidates/{i}", opening["id"],
            (f"/hypotheses/opening_candidates/{i}/span_m",),
            resolution="pixel_backed",
        )
        openings.append(OpeningClaimV1(
            opening_id=opening["id"], source_ref=ref,
        ))

    # -- channels: present ONLY with real payload (module 2's recorded weak
    #    spot: "present yet zero claims and zero debt" must not be minter here)
    channels: list[ChannelStatusV1] = []
    walls_debt = [d.debt_id for d in debts if d.channel == "walls"]
    if claims:
        channels.append(ChannelStatusV1(
            channel="walls", state="present",
            source_input_ids=(input_id,),
        ))
    else:
        debt_id = f"debt_missing_walls_{input_id}"
        debts.append(EvidenceDebtV1(
            debt_id=debt_id, kind="missing_channel", channel="walls",
            description="no positive wall claim could be derived from this product",
            obligation=None,
        ))
        channels.append(ChannelStatusV1(
            channel="walls", state="absent",
            covered_by_debt_ids=(debt_id, *walls_debt),
        ))
    if openings:
        channels.append(ChannelStatusV1(
            channel="plan_openings", state="present",
            source_input_ids=(input_id,),
        ))
    else:
        debt_id = f"debt_missing_plan_openings_{input_id}"
        debts.append(EvidenceDebtV1(
            debt_id=debt_id, kind="missing_channel", channel="plan_openings",
            description="no opening candidate in this product",
            obligation=None,
        ))
        channels.append(ChannelStatusV1(
            channel="plan_openings", state="absent",
            covered_by_debt_ids=(debt_id,),
        ))
    for channel in _AS_DRAWN_ABSENT_CHANNELS:
        debt_id = f"debt_{channel}_{input_id}"
        debts.append(EvidenceDebtV1(
            debt_id=debt_id, kind="missing_channel", channel=channel,
            description="channel not carried by this product family",
            obligation=None,
        ))
        channels.append(ChannelStatusV1(
            channel=channel, state="absent",
            covered_by_debt_ids=(debt_id,),
        ))

    bundle = finalize_bundle(CorrectionEvidenceBundleV1(
        schema_version=BUNDLE_SCHEMA_VERSION,
        source_artifacts=[meta],
        channel_status=channels,
        wall_claims=claims,
        face_dispositions=dispositions,
        opening_claims=openings,
        evidence_debts=debts,
    ))
    return _close_and_freeze(bundle, meta, raw)


# ── the as-drawn elevation adapter (B3, 2026-09-03) ────────────────────────── #
#: ⭐ The named premise this leg stands on (dispatch sheet §五.3, guide §十三).
#: "The elevation's dimension chain spans the whole building" -- a property
#: of HOW this family of drawings is drawn, ⛔ not a theorem.  What THIS
#: adapter can check of it single-sourced: the calibration chains close
#: (zero-threshold recompute below) and the floor ladder is not degenerate
#: (the validator raises for any elevation source with fewer than
#: MIN_FLOOR_LEVELS horizontal structure lines).  The remaining half --
#: chain total length == the plan side's outer-skin span -- needs the plan
#: input and is B4's equality gate by design (see the cross-view probe:
#: zero-parameter, loud on mismatch); ⛔ this adapter must not fake it with
#: a threshold against structure-line ink coverage (measured: 0.01-0.5 px
#: jitter on the real four facades, so any such check would be a
#: nobody-signed threshold, not an equality).
ELEVATION_CHAIN_SPANS_WHOLE_BUILDING = (
    "the elevation dimension chain spans the whole building"
)


def _require_chain_closed(calibration: dict, input_id: str) -> None:
    """Zero-threshold recompute of both calibration chains (⛔ not the
    product's self-reported ``chain_closure_mm`` -- a recompute, so a
    tampered self-report cannot vouch for itself).

    Exact float equality on purpose: the three quantities are the same JSON
    literals the producer derived each other from, so any difference is a
    broken chain, never rounding.
    """
    for axis in ("x", "z"):
        chain = calibration.get(axis)
        if not isinstance(chain, dict):
            raise EvidenceContractError(
                "CALIBRATION_AXIS_MISSING",
                {"input_id": input_id, "axis": axis},
            )
        values = chain.get("values_mm")
        cum = chain.get("cum_mm")
        overall = chain.get("overall_mm")
        if (
            not isinstance(values, list) or not values
            or not isinstance(cum, list) or not cum
            or any(
                isinstance(v, bool) or not isinstance(v, (int, float))
                for v in values
            )
        ):
            raise EvidenceContractError(
                "CALIBRATION_CHAIN_MALFORMED",
                {"input_id": input_id, "axis": axis},
            )
        total = sum(values)
        if total != cum[-1] or cum[-1] != overall:
            raise EvidenceContractError(
                "CALIBRATION_CHAIN_NOT_CLOSED",
                {
                    "input_id": input_id,
                    "axis": axis,
                    "sum_values_mm": total,
                    "cum_mm_last": cum[-1],
                    "overall_mm": overall,
                },
            )


def adapt_as_drawn_elevation(
    raw: bytes,
    *,
    input_id: str,
    facade_ref: str,
    view_type: Literal["plan", "elevation"] = "elevation",
) -> CorrectionEvidenceBundleArtifactV1:
    """Translate ONE frozen as-drawn v0 elevation product into a bundle.

    The two evidences this leg exists for (design §7.2 B3 row):

    * ``openings[].z_range_m`` → :class:`ElevationOpeningClaimV1` rows on the
      ``elevation_openings`` channel -- the vertical half of every window,
      each bound carrying the pointer to the frozen byte it was read from
      plus the pixel witnesses the product recorded for it;
    * the horizontal ``structure_lines`` → :class:`FloorLevelClaimV1` rows on
      the ``floor_levels`` channel, selected by
      :data:`FLOOR_LEVEL_SELECTION_RULE` (⭐ a predicate, ⛔ not a list -- a
      different storey count selects its own ladder).

    ⭐ Identity: ``input_id`` is the caller-supplied view-manifest slot and
    ``facade_ref`` the semantic slot's second coordinate (it lands on
    ``floor_ref``; the pair ``("elevation", facade_ref)`` is the uniqueness
    slot, so the four facades of one building are four distinct sources).
    Deterministic: the same bytes through this function produce a
    byte-identical bundle.

    Refuses loudly (stable codes): ``CALIBRATION_CHAIN_NOT_CLOSED`` (either
    axis's chain does not close under recompute), ``FLOOR_LADDER_DEGENERATE``
    (fewer than :data:`MIN_FLOOR_LEVELS` horizontal structure lines -- the
    z-shape of :data:`ELEVATION_CHAIN_SPANS_WHOLE_BUILDING` failing),
    ``ELEVATION_OPENING_Z_MISSING`` (an opening without a well-formed
    ascending ``z_range_m`` pair), ``FLOOR_LINE_Z_MISSING`` (a selected
    structure line without a numeric ``pos_m``).  A windowless facade is
    ⛔ NOT an error: ``elevation_openings`` travels ``absent`` with a
    ``missing_channel`` debt, the honest zero-run shape.
    """
    doc = _parse_json_bytes(raw, input_id)
    _require_contract(doc, raw, input_id, CONTRACT_AS_DRAWN_ELEVATION_V0)

    sha = hashlib.sha256(raw).hexdigest()
    contract = SOURCE_CONTRACT_AS_DRAWN_ELEVATION
    meta = SourceArtifactV1(
        input_id=input_id, source_contract_id=contract,
        source_output_sha256=sha, view_type=view_type, floor_ref=facade_ref,
    )

    # -- premise, single-sourceable half: both chains close under recompute --
    calibration = doc.get("calibration")
    if not isinstance(calibration, dict):
        raise EvidenceContractError(
            "CALIBRATION_MISSING", {"input_id": input_id}
        )
    _require_chain_closed(calibration, input_id)

    # -- openings → elevation_opening_claims (value + byte provenance) -------
    opening_index = as_drawn_elevation_opening_index(doc)
    elev_openings: list[ElevationOpeningClaimV1] = []
    for oid, (i, node) in opening_index.items():
        base = f"/openings/{i}"
        zr = node.get("z_range_m")
        if (
            not isinstance(zr, list) or len(zr) != 2
            or isinstance(zr[0], bool) or isinstance(zr[1], bool)
            or not isinstance(zr[0], (int, float))
            or not isinstance(zr[1], (int, float))
            or not zr[0] < zr[1]
        ):
            raise EvidenceContractError(
                "ELEVATION_OPENING_Z_MISSING",
                {
                    "input_id": input_id,
                    "opening_id": oid,
                    "z_range_m": zr,
                    "expected": "a two-element ascending numeric pair",
                },
            )
        # pixel witnesses travel only when the product actually recorded
        # them; without them the z is vector/calibration-derived, and the
        # ref says so instead of promising pixels it cannot show.
        edge = node.get("edge_witnesses")
        witnesses = tuple(
            f"{base}/edge_witnesses/{key}"
            for key in ("z_low", "z_high")
            if isinstance(edge, dict) and isinstance(edge.get(key), dict)
        )
        source_ref = _observation_ref(
            input_id, contract, sha, base, oid,
            witnesses=witnesses,
            resolution="pixel_backed" if witnesses else "vector_only",
        )
        elev_openings.append(ElevationOpeningClaimV1(
            opening_id=oid,
            source_ref=source_ref,
            z_low_m=float(zr[0]),
            z_low_ref=_pointer(input_id, contract, sha, f"{base}/z_range_m/0"),
            z_high_m=float(zr[1]),
            z_high_ref=_pointer(input_id, contract, sha, f"{base}/z_range_m/1"),
        ))

    # -- horizontal structure lines → floor_level_claims (T4's rule) ---------
    floor_lines = as_drawn_elevation_floor_lines(doc)
    if len(floor_lines) < MIN_FLOOR_LEVELS:
        raise EvidenceContractError(
            "FLOOR_LADDER_DEGENERATE",
            {
                "input_id": input_id,
                "horizontal_structure_lines": sorted(floor_lines),
                "minimum": MIN_FLOOR_LEVELS,
                "rule": FLOOR_LEVEL_SELECTION_RULE,
            },
        )
    floors: list[FloorLevelClaimV1] = []
    for lid, (i, node) in floor_lines.items():
        base = f"/structure_lines/{i}"
        pos_m = node.get("pos_m")
        if isinstance(pos_m, bool) or not isinstance(pos_m, (int, float)):
            raise EvidenceContractError(
                "FLOOR_LINE_Z_MISSING",
                {
                    "input_id": input_id,
                    "structure_line_id": lid,
                    "pos_m": pos_m,
                },
            )
        witnesses = tuple(
            f"{base}/{key}"
            for key in ("pos_px", "cols_px", "runs_px")
            if key in node
        )
        floors.append(FloorLevelClaimV1(
            structure_line_id=lid,
            source_ref=_observation_ref(
                input_id, contract, sha, base, lid,
                witnesses=witnesses,
                resolution="pixel_backed" if witnesses else "vector_only",
            ),
            z_m=float(pos_m),
            z_ref=_pointer(input_id, contract, sha, f"{base}/pos_m"),
        ))

    # -- channels: the routing table says exactly what this leg carries -----
    debts: list[EvidenceDebtV1] = [
        # ⭐ R2-b (B3 rework 1, 2026-09-03): the span-equality half of the
        # named premise travels as a DEBT IN THE PRODUCT, ⛔ not as a source
        # comment -- downstream reconciles artifacts, never comments (the
        # cross-review's criterion verbatim).  ``other_known_missing`` is the
        # registered kind for exactly this shape (an accounted known-missing
        # that travels with the artifact; it blocks NO profile), and the
        # description names B4's equality gate as owner, so B4 can hold the
        # plan side accountable by ``debt_id`` when it reconciles the two
        # families.  ⛔ This debt is a claim of ownership, ⛔ never a licence:
        # it excuses no channel (channel is None on purpose).
        EvidenceDebtV1(
            debt_id=f"debt_elevation_chain_span_unchecked_{input_id}",
            kind="other_known_missing",
            channel=None,
            affected_refs=(
                _pointer(input_id, contract, sha, "/calibration"),
            ),
            description=(
                "the elevation chain's total length is NOT reconciled "
                "against the plan side's outer-skin span on this leg: that "
                "equality needs the plan input (a different product "
                "family) and is B4's zero-parameter equality gate, loud on "
                "mismatch; this adapter must not fake it with a threshold "
                "against structure-line ink coverage (measured 0.01-0.5 px "
                "jitter on the real four facades -- that would be a "
                "nobody-signed threshold, not an equality).  Named premise: "
                f"{ELEVATION_CHAIN_SPANS_WHOLE_BUILDING}.  Owner: B4."
            ),
            # ⭐ (dispatch 2026-09-04e, T4-a T1/T2) THE one obligation today:
            # the lower-case snake form of the premise constant above -- the
            # value B4's DEBT_REDEMPTION_REGISTRY keys its span row by.  The
            # debt_id prefix still carries it too, but ⛔ that prefix is no
            # longer the wiring criterion anywhere (T3).
            obligation="elevation_chain_spans_whole_building",
        ),
    ]
    channels: list[ChannelStatusV1] = []
    if elev_openings:
        channels.append(ChannelStatusV1(
            channel="elevation_openings", state="present",
            source_input_ids=(input_id,),
        ))
    else:
        debt_id = f"debt_missing_elevation_openings_{input_id}"
        debts.append(EvidenceDebtV1(
            debt_id=debt_id, kind="missing_channel",
            channel="elevation_openings",
            description="no opening in this elevation product",
            obligation=None,
        ))
        channels.append(ChannelStatusV1(
            channel="elevation_openings", state="absent",
            covered_by_debt_ids=(debt_id,),
        ))
    channels.append(ChannelStatusV1(
        channel="floor_levels", state="present",
        source_input_ids=(input_id,),
    ))
    for channel in _ELEVATION_ABSENT_CHANNELS:
        debt_id = f"debt_{channel}_{input_id}"
        debts.append(EvidenceDebtV1(
            debt_id=debt_id, kind="missing_channel", channel=channel,
            description="channel not carried by an elevation product",
            obligation=None,
        ))
        channels.append(ChannelStatusV1(
            channel=channel, state="absent",
            covered_by_debt_ids=(debt_id,),
        ))

    bundle = finalize_bundle(CorrectionEvidenceBundleV1(
        schema_version=BUNDLE_SCHEMA_VERSION,
        source_artifacts=[meta],
        channel_status=channels,
        wall_claims=[],
        face_dispositions=[],
        opening_claims=[],
        elevation_opening_claims=elev_openings,
        floor_level_claims=floors,
        evidence_debts=debts,
    ))
    return _close_and_freeze(bundle, meta, raw)


# ── the legacy adapter ────────────────────────────────────────────────────── #
def adapt_legacy_reading_view(
    raw: bytes,
    *,
    input_id: str,
    floor_ref: str,
    view_type: Literal["plan", "elevation"] = "plan",
) -> CorrectionEvidenceBundleArtifactV1:
    """Translate ONE frozen legacy ``ReadingView`` into a validated bundle.

    Every ``pen == "wall"`` stroke becomes one ``legacy_wall_trace`` claim with
    ``source_basis`` decided by STRUCTURE ONLY (§8.3):

    * a typed ``geometry.basis`` key whose value is in the closed domain →
      that basis, with ``basis_evidence_ref`` pointing at the declaring key;
    * a ``geometry.basis`` key outside the domain → ``LEGACY_BASIS_DECLARATION_
      INVALID`` (a malformed declaration is loud, ⛔ not a quiet ``unknown``);
    * no such key (every real historical product measured so far) →
      ``unknown``, basis_evidence_ref ``None``.

    ⛔ The free-text ``note`` is never read.  The two real fixtures this
    adapter must pass carry "outer skin line" and "centreline" ONLY in notes
    on the SAME ``pen == "wall"`` field -- parsing notes would return two
    different bases for the two files and quietly re-open the centreline
    assumption this whole contract exists to retire.
    """
    doc = _parse_json_bytes(raw, input_id)
    _require_contract(doc, raw, input_id, CONTRACT_READING_VIEW_LEGACY)

    sha = hashlib.sha256(raw).hexdigest()
    contract = SOURCE_CONTRACT_LEGACY
    meta = SourceArtifactV1(
        input_id=input_id, source_contract_id=contract,
        source_output_sha256=sha, view_type=view_type, floor_ref=floor_ref,
    )

    strokes = doc.get("strokes", [])
    seen: dict[str, int] = {}
    claims: list[WallClaimV1] = []
    unadapted_opening_strokes = 0
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
        pen = stroke.get("pen")
        if pen == "wall":
            geometry = stroke.get("geometry") or {}
            if "basis" in geometry:
                declared = geometry.get("basis")
                if declared not in LEGACY_BASIS_VALUES:
                    raise EvidenceContractError(
                        "LEGACY_BASIS_DECLARATION_INVALID",
                        {
                            "input_id": input_id,
                            "observation_id": sid,
                            "declared": declared,
                            "domain": list(LEGACY_BASIS_VALUES),
                        },
                    )
                basis: str = declared
                basis_ref = _pointer(
                    input_id, contract, sha, f"/strokes/{i}/geometry/basis"
                )
            else:
                basis = "unknown"
                basis_ref = None
            claim = LegacyWallTraceClaimV1(
                claim_id="pending",
                hypothesis_ref=_pointer(
                    input_id, contract, sha, f"/strokes/{i}"
                ),
                perception_source_ref=_pointer(
                    input_id, contract, sha, ""
                ),
                source_contract_id=contract,
                trace_ref=_observation_ref(
                    input_id, contract, sha, f"/strokes/{i}", sid,
                    witnesses=(), resolution="vector_only",
                ),
                source_basis=basis,  # type: ignore[arg-type]
                basis_evidence_ref=basis_ref,
            )
            claim = claim.model_copy(update={"claim_id": wall_claim_id(claim)})
            claims.append(claim)
        elif pen in ("window", "door"):
            # Counted, named in the plan_openings debt below, ⛔ not adapted:
            # an OpeningClaim's source node must be reference-shaped
            # (face_line + gap_index), which a legacy stroke is not.
            unadapted_opening_strokes += 1

    debts: list[EvidenceDebtV1] = []
    channels: list[ChannelStatusV1] = []
    if claims:
        channels.append(ChannelStatusV1(
            channel="walls", state="present",
            source_input_ids=(input_id,),
        ))
    else:
        debt_id = f"debt_missing_walls_{input_id}"
        debts.append(EvidenceDebtV1(
            debt_id=debt_id, kind="missing_channel", channel="walls",
            description="no pen=='wall' stroke in this view",
            obligation=None,
        ))
        channels.append(ChannelStatusV1(
            channel="walls", state="absent",
            covered_by_debt_ids=(debt_id,),
        ))
    for channel in _LEGACY_ABSENT_CHANNELS:
        debt_id = f"debt_{channel}_{input_id}"
        if channel == "plan_openings" and unadapted_opening_strokes:
            description = (
                f"{unadapted_opening_strokes} window/door stroke(s) present; "
                "walls-only adapter did not translate them"
            )
        else:
            description = "legacy view carries no such channel"
        debts.append(EvidenceDebtV1(
            debt_id=debt_id, kind="missing_channel", channel=channel,
            description=description,
            obligation=None,
        ))
        channels.append(ChannelStatusV1(
            channel=channel, state="absent",
            covered_by_debt_ids=(debt_id,),
        ))

    bundle = finalize_bundle(CorrectionEvidenceBundleV1(
        schema_version=BUNDLE_SCHEMA_VERSION,
        source_artifacts=[meta],
        channel_status=channels,
        wall_claims=claims,
        # §4.2 governs as-drawn face LINES; a legacy stroke is not one, so the
        # disposition ledger stays empty for a legacy source (module 2's
        # validator deliberately exempts non-as-drawn sources from it).
        face_dispositions=[],
        opening_claims=[],
        evidence_debts=debts,
    ))
    return _close_and_freeze(bundle, meta, raw)


__all__ = [
    "ELEVATION_CHAIN_SPANS_WHOLE_BUILDING",
    "LEGACY_BASIS_VALUES",
    "adapt_as_drawn_elevation",
    "adapt_as_drawn_plan",
    "adapt_legacy_reading_view",
]
