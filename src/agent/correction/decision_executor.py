"""②-2 module 6: the three-beat decision executor (2026-09-01).

Dispatch: the module 5+6 dispatch; authority: the 2026-08-30
evidence-contract design §6.3 + §9.1 step 5 ("model output schema carries
no coordinates; first exercise the three-beat executor with FIXED
responses, then wire the model").  Module 4's own docstring reserves this
slice: "the executor loop and any profile gate beyond the ambiguous
block (module 6)".  ⭐ Nothing here talks to a model: the loop consumes
``CorrectionDecisionResponseV1`` values the caller supplies -- fixtures
today, the model through ``model_validate_json`` once module 7 lands.

The five steps of design §6.3, one loop body
--------------------------------------------
① validate the response against the packet: it must bind the CURRENT
   ``packet_hash`` (a stale hash is one of the four loud exits), every
   ``item_id`` must be an open item OF THIS PACKET, every selected
   ``candidate_id`` must be a candidate OF THAT ITEM, no item twice --
   these are ``DecisionLoopError`` raises, module 4's own decision-fault
   family; findings must cite entities/refs the packet actually indexes;
② run the ``symbolic_operation``: selected decisions are translated to
   module 4's ``FixedDecisionV1`` and the compilation is REBUILT from
   the frozen artifact -- every coordinate, thickness and intersection
   is computed by module 4's code, never read from the response;
③ the rebuilt ``WallCompilationV1`` IS the updated ``ResolvedWallV1``
   set with its source trace (module 4 re-hashes each wall);
④ re-run the deterministic consistency checks on the new provisional
   (``run_consistency_checks``; check face below);
⑤ findings are verified against the packet and carried on the outcome
   as ``pending_findings`` for the next round's candidate generation --
   see the STOP-REPORT note below for how far this goes in this module.

Success is the four-part conjunction of §6.3, each part separately
falsifiable (acceptance 5): no blocking open item · deterministic checks
pass · the model's overall ``accept`` binds THIS packet's provisional
hash -- rework B-1: a round whose decisions moved the geometry cannot
succeed on its own accept; the next packet must be built on the new
provisional and a fresh accept earned against it · no residual evidence
debt the profile's explicit policy table forbids (rework B-3: ``_DEBT_
SUCCESS_POLICY`` -- no blanket "ambiguous only", no blanket "every
missing_channel").  Decisions ACCUMULATE across rounds (rework B-2):
each round replays the whole accepted history through module 4, so a
later round cannot silently undo an earlier one.

The four loud exits keep a residual manifest (open items, debts, degraded
walls, pending findings) and the final provisional is NEVER a success
product when ``exit_reason != "success"`` -- it travels for audit with
``success=False`` stamped on the same outcome.  "No progress" has an
operational definition so it does not eat the cycle exit: a round that
executed no decision (pure reject / re-perception) is a legal rhythm and
only a STREAK of them exits; a round whose executed decisions failed to
move the provisional hash at all exits immediately (defence seat -- see
the loop body).  "Decision-hash cycle" fires when a whole decision set
repeats verbatim, which is the model stuck repeating itself.

Consistency check face (honest scope)
-------------------------------------
``collinear_wall_overlap`` and ``unshared_tail_coverage`` are the two
checks this module can compute from module 4's IR today, and the closed
``Literal`` on ``ConsistencyResultV1.check`` makes any OTHER check name
unrepresentable -- the face cannot grow silently.  Topology / enclosure /
opening-host / cross-floor checks are ⛔ NOT faked here: their inputs
(opening↔wall relations, floor pairing) do not exist in module 4's IR,
and pretending to run them would be a gate that measures nothing.  The
overlap check groups walls by EXACT (axis, constant_pos) equality -- two
collinear walls whose positions were computed independently and differ
in the last float bits are not merged by any tolerance here (zero
threshold discipline); that residue belongs to the enclosure stage that
consumes support lines.

STOP-REPORT (dispatch §一/§五), filed in the execution report
-------------------------------------------------------------
Step ⑤ "findings generate new candidates" is delivered ONLY up to
receiving, validating and carrying findings.  Generating the bounded
candidates the five kinds ask for needs operations module 4's closed
``SymbolicOperation`` / ``OpenItemV1.kind`` enums do not carry
(segmentation split/merge, topology connect/separate, opening rehost);
per the dispatch these enums are module 4's, under review, and ⛔ must
not be extended or re-implemented here.  The two kinds that DO fit the
existing IR are not special-cased either, so the finding path stays one
shape: findings ride the outcome until a candidate generator exists.
"""
from __future__ import annotations

from typing import Callable, Literal, Sequence

from pydantic import BaseModel, ConfigDict

from src.agent.correction.decision_schema import (
    ConsistencyResultV1,
    CorrectionDecisionPacketV1,
    CorrectionDecisionResponseV1,
    EntitySourceRefsV1,
    FindingV1,
    ItemDecisionV1,
)
from src.agent.correction.evidence_contract import (
    ArtifactPointerV1,
    CorrectionEvidenceBundleArtifactV1,
)
from src.agent.correction.wall_compiler import (
    COMPILATION_SCHEMA_VERSION,
    FixedDecisionV1,
    WallCompilationV1,
    compile_wall_ir,
)
from src.agent.correction.window_sources import Hex64, canonical_sha256

_CFG = ConfigDict(extra="forbid", strict=True)

#: No progress = this many CONSECUTIVE rounds executed no decision at all
#: (pure rejects / re-perceptions / empty).  One such round is a legal
#: rhythm -- the model may reject first and decide next -- and it is what
#: keeps the ``decision_hash_cycle`` exit REACHABLE: a repeated decision
#: set is caught at the head of the NEXT round, before this streak could
#: expire it as mere stalling.  Two fresh-but-empty rounds in a row mean
#: the loop is burning budget without executing anything: loud exit.
_NO_PROGRESS_STALL_LIMIT = 2


class DecisionLoopError(ValueError):
    """A loud response-validation refusal, with a stable ``code``.

    Same family as module 2/4's errors: the response names an item this
    packet does not hold, a candidate that item does not list, the same
    item twice, or a finding that cites entities/refs outside the packet.
    These are CONTENT faults of one response -- distinct from the four
    LOOP exits (stale packet among them), which terminate the whole loop
    with a residual manifest instead of raising.
    """

    def __init__(self, code: str, context: dict | None = None):
        self.code = code
        self.context = dict(context or {})
        super().__init__(f"{code}: {self.context}")


# ── the packet builder (first beat's packaging half) ------------------------- #
def build_decision_packet(
    compilation: WallCompilationV1,
    *,
    bundle: CorrectionEvidenceBundleArtifactV1,
    round_index: int,
    previous_decision_hashes: Sequence[Hex64] = (),
    solver_revision: str = COMPILATION_SCHEMA_VERSION,
) -> CorrectionDecisionPacketV1:
    """Wrap one finalized module-4 compilation into the §6.1 packet.

    ``provisional_geometry`` is the compilation's ``content_sha256`` (see
    the module-5 docstring for why the compilation is not embedded), and
    ``packet_hash`` is the canonical hash over everything else -- so a
    response binding ``packet_hash`` binds the provisional hash, the open
    items AND their candidate sets as one frozen world.

    ``bundle`` is REQUIRED (rework B-4): the entity table indexes the
    bundle's ``opening_claims`` as ``entity_kind="opening"`` entities.
    It is deliberately not an optional defaulting to an empty table --
    "forgot to pass the openings, silently indexed 0" is the exact
    disease this rework closes; the caller who forgetts gets a loud
    ``TypeError``, not a quietly opening-less packet.
    """
    if compilation.content_sha256 is None:
        raise DecisionLoopError(
            "PACKET_COMPILATION_NOT_FINALIZED",
            {"schema_version": compilation.schema_version},
        )
    entities: dict[
        str, tuple[str, tuple[ArtifactPointerV1, ...]]
    ] = {}
    for wall in compilation.walls:
        entities[wall.wall_id] = ("wall", tuple(wall.source_refs))
    for opening in bundle.bundle.opening_claims:
        # project the ObservationRefV1 onto its pointer identity: the
        # finding-citation check keys on the four ArtifactPointerV1
        # fields, so the packet carries exactly those (no revalidation
        # surprise when a dumped packet is read back).
        ref = opening.source_ref
        entities[opening.opening_id] = (
            "opening",
            (ArtifactPointerV1(
                input_id=ref.input_id,
                source_contract_id=ref.source_contract_id,
                source_output_sha256=ref.source_output_sha256,
                json_pointer=ref.json_pointer,
            ),),
        )
    for action in compilation.auto_actions:
        for entity_id in action.scope_entity_ids:
            entities.setdefault(
                entity_id, ("auto_action_scope", tuple(action.source_refs))
            )
    for item in compilation.open_items:
        entities[item.item_id] = ("open_item", tuple(item.source_refs))

    packet = CorrectionDecisionPacketV1(
        packet_hash="0" * 64,  # placeholder; finalized below
        input_bundle_hash=compilation.bundle_content_sha256,
        solver_revision=solver_revision,
        round_index=round_index,
        previous_decision_hashes=tuple(previous_decision_hashes),
        provisional_geometry=compilation.content_sha256,
        provisional_wall_summaries=tuple(compilation.walls),
        entity_to_source_refs=tuple(
            EntitySourceRefsV1(
                entity_id=entity_id,
                entity_kind=kind,
                source_refs=refs,
            )
            for entity_id, (kind, refs) in sorted(entities.items())
        ),
        auto_actions=tuple(compilation.auto_actions),
        consistency_results=run_consistency_checks(compilation),
        open_items=tuple(compilation.open_items),
    )
    content = packet.model_dump(mode="python")
    content.pop("packet_hash")
    return packet.model_copy(
        update={"packet_hash": canonical_sha256(content)}
    )


# ── step ④: the deterministic check face ------------------------------------ #
def _interval_len(pieces: tuple[tuple[float, float], ...]) -> float:
    return sum(hi - lo for lo, hi in pieces)


def run_consistency_checks(
    compilation: WallCompilationV1,
) -> tuple[ConsistencyResultV1, ...]:
    """The two checks computable from module 4's IR (see module docstring
    for the honest-scope note: no faked topology/enclosure/opening-host/
    cross-floor checks)."""
    # -- collinear_wall_overlap: two resolved walls on the SAME support
    #    line may not cover overlapping along-stretches.  Grouping is
    #    exact-equality on (axis, constant_pos): no tolerance merges two
    #    independently computed positions here.
    groups: dict[tuple[str, float], list[WallCompilationV1]] = {}
    for wall in compilation.walls:
        line = wall.resolved_centerline
        if line is None or line.constant_world_axis is None:
            continue
        groups.setdefault(
            (line.constant_world_axis, line.constant_pos_m), []
        ).append(wall)
    overlap_detail = ""
    for (axis, pos), walls in sorted(groups.items()):
        for i, first in enumerate(walls):
            for second in walls[i + 1:]:
                shared = _interval_overlap(
                    first.resolved_along_intervals,
                    second.resolved_along_intervals,
                )
                if shared:
                    overlap_detail = (
                        f"walls {first.wall_id}/{second.wall_id} overlap on "
                        f"{axis}={pos} over {shared}"
                    )
                    break
            if overlap_detail:
                break
        if overlap_detail:
            break
    overlap = ConsistencyResultV1(
        check="collinear_wall_overlap",
        passed=not overlap_detail,
        detail=overlap_detail or "no two resolved walls share along-span",
    )

    # -- unshared_tail_coverage: every single-face fragment must still be
    #    fully inside its own wall's resolved coverage -- tails survive,
    #    and nothing drifts outside the wall it evidences.
    tail_detail = ""
    for wall in compilation.walls:
        for frag in wall.unshared_tail_fragments:
            keep = _interval_overlap(
                (frag.along_interval_m,), wall.resolved_along_intervals
            )
            want = frag.along_interval_m[1] - frag.along_interval_m[0]
            if keep is None or _interval_len(keep) != want:
                tail_detail = (
                    f"fragment {frag.fragment_id} of {wall.wall_id} is not "
                    "fully covered by its wall's resolved intervals"
                )
                break
        if tail_detail:
            break
    tails = ConsistencyResultV1(
        check="unshared_tail_coverage",
        passed=not tail_detail,
        detail=tail_detail or "every unshared tail sits inside its wall",
    )
    return (overlap, tails)


def _interval_overlap(
    a: tuple[tuple[float, float], ...], b: tuple[tuple[float, float], ...]
) -> tuple[tuple[float, float], ...] | None:
    out: list[tuple[float, float]] = []
    for alo, ahi in a:
        for blo, bhi in b:
            lo, hi = max(alo, blo), min(ahi, bhi)
            if lo < hi:
                out.append((lo, hi))
    return tuple(out) if out else None


# ── the loop's own record types (design §6.3 leaves these to module 6) ------ #
class RoundRecordV1(BaseModel):
    """One executed round: which packet, which decisions, what they were
    and what the provisional became.  Determinism: the same artifact +
    the same responses replay byte-identically (canonical sort + one
    content hash over the whole outcome)."""

    model_config = _CFG

    round_index: int
    packet_hash: Hex64
    decision_hash: Hex64
    selected_item_ids: tuple[str, ...] = ()
    rejected_item_ids: tuple[str, ...] = ()
    reperception_item_ids: tuple[str, ...] = ()
    finding_ids: tuple[str, ...] = ()
    failed_checks: tuple[str, ...] = ()
    provisional_sha256: Hex64
    completion: Literal["complete", "degraded"]


class DecisionLoopOutcomeV1(BaseModel):
    """The loop's whole answer.  ⭐ ``exit_reason`` is exactly §6.3's
    four loud exits plus ``success`` -- no fifth way out was invented.

    On any non-success exit the residual manifest is populated and the
    final provisional travels FOR AUDIT ONLY: it is NOT a success
    product, and ``success=False`` on the same object says so -- there is
    no path where a degraded final compilation is handed on as finished.
    """

    model_config = _CFG

    success: bool
    exit_reason: Literal[
        "success",
        "no_progress",
        "decision_hash_cycle",
        "stale_packet",
        "round_budget_exhausted",
    ]
    rounds: tuple[RoundRecordV1, ...] = ()
    residual_open_item_ids: tuple[str, ...] = ()
    residual_debt_ids: tuple[str, ...] = ()
    degraded_wall_ids: tuple[str, ...] = ()
    pending_findings: tuple[FindingV1, ...] = ()
    final_provisional_sha256: Hex64 | None = None
    final_completion: Literal["complete", "degraded"] | None = None
    content_sha256: Hex64 | None = None


# ── response validation (§6.3 step ①) ---------------------------------------- #
def decision_hash(response: CorrectionDecisionResponseV1) -> Hex64:
    """Identity of WHAT was decided this round: the item decisions only.
    The same decision set returning later in the sequence is the
    ``decision_hash_cycle`` exit; an ``accept`` vs ``findings`` verdict
    on identical decisions is deliberately the same hash -- the cycle
    detector tracks decisions, not commentary."""
    return canonical_sha256(
        [d.model_dump(mode="python") for d in response.item_decisions]
    )


def _packet_entity_index(
    packet: CorrectionDecisionPacketV1,
) -> tuple[dict[str, str], set[tuple[str, str, str, str]]]:
    entities = {
        e.entity_id: e.entity_kind
        for e in packet.entity_to_source_refs
    }
    for item in packet.open_items:
        entities.setdefault(item.item_id, "open_item")
    refs = {
        (r.input_id, r.source_contract_id, r.source_output_sha256,
         r.json_pointer)
        for e in packet.entity_to_source_refs for r in e.source_refs
    }
    refs.update(
        (r.input_id, r.source_contract_id, r.source_output_sha256,
         r.json_pointer)
        for item in packet.open_items for r in item.source_refs
    )
    return entities, refs


#: which ids each ``requested_effect`` kind requires to be of which entity
#: kind (rework B-4: §6.2's "packet 内 opening id 与候选 wall ids" is a
#: TYPED membership, not bare presence -- a real wall id impersonating an
#: opening is the bypass the cross-review demonstrated).
_EFFECT_ENTITY_KINDS: dict[str, tuple[tuple[str, str], ...]] = {
    "review_opening_host": (
        ("opening_entity_id", "opening"),
        ("candidate_wall_entity_ids", "wall"),
    ),
}


def _effect_entities(effect) -> tuple[str, ...]:
    names = (
        "subject_entity_ids", "reference_entity_ids",
        "wall_item_entity_ids",
    )
    out = tuple(
        entity for name in names
        for entity in getattr(effect, name, ())
    )
    for name in ("opening_entity_id", "candidate_wall_entity_ids"):
        value = getattr(effect, name, None)
        if isinstance(value, str):
            out += (value,)
        else:
            out += tuple(value or ())
    return out


def _validate_response(
    packet: CorrectionDecisionPacketV1,
    response: CorrectionDecisionResponseV1,
) -> tuple[list[FixedDecisionV1], dict[str, list[str]]]:
    """Steps of §6.2's rejection list that a schema cannot express:
    items must be open IN THIS PACKET, candidates must belong to THAT
    item, no item twice, findings must cite the packet's own entities
    and refs.  Returns the module-4 bindings for the selects."""
    items = {item.item_id: item for item in packet.open_items}
    seen: set[str] = set()
    bindings: list[FixedDecisionV1] = []
    buckets: dict[str, list[str]] = {
        "selected": [], "rejected": [], "reperception": [],
    }
    for decision in response.item_decisions:
        if decision.item_id not in items:
            raise DecisionLoopError(
                "UNKNOWN_RESPONSE_ITEM",
                {"item_id": decision.item_id,
                 "available": sorted(items)},
            )
        if decision.item_id in seen:
            raise DecisionLoopError(
                "DUPLICATE_RESPONSE_ITEM", {"item_id": decision.item_id}
            )
        seen.add(decision.item_id)
        if decision.action == "select_candidate":
            candidate_ids = [
                c.candidate_id for c in items[decision.item_id].candidates
            ]
            if decision.candidate_id not in candidate_ids:
                raise DecisionLoopError(
                    "UNKNOWN_RESPONSE_CANDIDATE",
                    {"item_id": decision.item_id,
                     "candidate_id": decision.candidate_id,
                     "available": sorted(candidate_ids)},
                )
            bindings.append(FixedDecisionV1(
                item_id=decision.item_id,
                candidate_id=decision.candidate_id,
            ))
            buckets["selected"].append(decision.item_id)
        elif decision.action == "reject_all":
            buckets["rejected"].append(decision.item_id)
        else:
            buckets["reperception"].append(decision.item_id)

    entities, refs = _packet_entity_index(packet)
    for finding in response.whole_building_review.findings:
        _validate_finding(finding, entities, refs)
    return bindings, buckets


def _validate_finding(
    finding: FindingV1,
    entities: dict[str, str],
    refs: set[tuple[str, str, str, str]],
) -> None:
    cited = tuple(finding.affected_entity_ids) + _effect_entities(
        finding.requested_effect
    )
    for entity_id in cited:
        if entity_id not in entities:
            raise DecisionLoopError(
                "FINDING_ENTITY_NOT_IN_PACKET",
                {"finding_id": finding.finding_id, "entity_id": entity_id},
            )
    # typed membership: an effect kind that declares expectations for a
    # role must find entities of THAT kind in it (B-4's second example:
    # a real wall id in the ``opening_entity_id`` slot).
    for role, want_kind in _EFFECT_ENTITY_KINDS.get(
        finding.requested_effect.kind, ()
    ):
        value = getattr(finding.requested_effect, role, None)
        for entity_id in (
            (value,) if isinstance(value, str) else tuple(value or ())
        ):
            got_kind = entities.get(entity_id)
            if got_kind != want_kind:
                raise DecisionLoopError(
                    "FINDING_ENTITY_WRONG_KIND",
                    {
                        "finding_id": finding.finding_id,
                        "role": role,
                        "entity_id": entity_id,
                        "expected_kind": want_kind,
                        "actual_kind": got_kind,
                    },
                )
    for ref in tuple(finding.source_refs) + tuple(
        getattr(finding.requested_effect, "source_refs", ())
    ):
        key = (ref.input_id, ref.source_contract_id,
               ref.source_output_sha256, ref.json_pointer)
        if key not in refs:
            raise DecisionLoopError(
                "FINDING_REF_NOT_IN_PACKET",
                {"finding_id": finding.finding_id,
                 "ref": list(key)},
            )


# ── the three-beat loop (§6.3) ----------------------------------------------- #
def run_decision_loop(
    artifact: CorrectionEvidenceBundleArtifactV1,
    *,
    profile: Literal["strict", "exploratory"] = "strict",
    responses: Sequence[CorrectionDecisionResponseV1] = (),
    response_provider: "Callable[[CorrectionDecisionPacketV1], CorrectionDecisionResponseV1] | None" = None,
    round_budget: int | None = None,
    solver_revision: str = COMPILATION_SCHEMA_VERSION,
    compilation_sink: "Callable[[WallCompilationV1], None] | None" = None,
) -> DecisionLoopOutcomeV1:
    """Drive the three beats (§9.1 step 5 → module 7 wiring).

    ``compilation_sink`` (B1 wiring): when given, receives the FINAL wall
    compilation exactly once, on EVERY exit — the one choke point the
    ``outcome()`` closure is.  The outcome itself carries only hashes
    (``final_provisional_sha256``); the projection bridge needs the walls,
    and this is the seam that hands them over without changing the
    outcome's own contract.  On a non-success exit the sunk compilation is
    the audit-only provisional (``success=False`` on the same outcome says
    so) — the sink's consumer owns that discipline, not this loop.

    Two response sources, exactly one per call (both ⇒ loud, neither ⇒
    the loop consumes nothing and exits ``round_budget_exhausted``):

    * ``responses`` — the FIXED sequence (fixtures; the module-5/6 shape,
      unchanged).  The caller owns the sequence; ``round_budget`` defaults
      to its length and caps how many entries are consumed.
    * ``response_provider`` — a callable handed EACH round's freshly built
      packet and returning that round's response.  ⭐ This is the model's
      seat (module 7): the provider sees the CURRENT packet (so its
      ``packet_hash`` can bind it) before answering -- the loop still
      owns packet construction, response validation and every rebuild,
      and a provider that returns garbage dies in the SAME
      ``DecisionLoopError`` family as a bad fixture.  ``round_budget``
      has no default in this mode and must be given explicitly.

    A ``strict`` profile refused by module 4's ambiguous-debt gate
    propagates unchanged: that refusal is already loud, named and
    measured there -- this loop does not swallow it into an exit it was
    not given.  Exhausting the budget without success is the
    ``round_budget_exhausted`` exit.
    """
    if response_provider is not None and len(responses) > 0:
        raise DecisionLoopError(
            "RESPONSE_SOURCE_AMBIGUOUS",
            {
                "fixed_responses": len(responses),
                "hint": "pass either responses= or response_provider=, not both",
            },
        )
    if response_provider is not None and round_budget is None:
        raise DecisionLoopError(
            "ROUND_BUDGET_REQUIRED_WITH_PROVIDER",
            {
                "hint": "a provider is unbounded; name the round budget "
                "explicitly"
            },
        )
    if round_budget is None:
        round_budget = len(responses)
    if round_budget < 0:
        raise DecisionLoopError(
            "ROUND_BUDGET_NEGATIVE", {"round_budget": round_budget}
        )

    compilation = compile_wall_ir(artifact, profile=profile)
    debt_info = {
        debt.debt_id: (debt.kind, debt.channel)
        for debt in artifact.bundle.evidence_debts
    }
    # rework B-2: decisions ACCUMULATE across rounds.  Module 4's compile
    # applies exactly the decisions it is handed, so every round replays
    # the whole accepted history -- a round selecting its own item with
    # the history dropped would silently UNDO earlier rounds (the
    # cross-review's B-2 reading: residual re-opening an item round 0
    # had already closed).  A repeated decision for one item cannot
    # reach this list: the item is closed in the current packet, so
    # step ① refuses the response before any binding is kept.
    accumulated: list[FixedDecisionV1] = []
    previous: list[Hex64] = []
    seen_hashes: set[Hex64] = set()
    rounds: list[RoundRecordV1] = []
    pending: list[FindingV1] = []
    stall_streak = 0

    def outcome(
        exit_reason: str,
        current: WallCompilationV1,
    ) -> DecisionLoopOutcomeV1:
        if compilation_sink is not None:
            compilation_sink(current)
        return _finalize_outcome(
            success=exit_reason == "success",
            exit_reason=exit_reason,
            rounds=rounds,
            compilation=current,
            pending=pending,
        )

    for index in range(round_budget):
        packet = build_decision_packet(
            compilation,
            bundle=artifact,
            round_index=index,
            previous_decision_hashes=tuple(previous),
            solver_revision=solver_revision,
        )
        # ⭐ Module 7: the response source. Provider mode asks with the
        # CURRENT packet in hand (so the model can bind its hash); fixed
        # mode reads entry `index` -- running past the sequence breaks to
        # the same `round_budget_exhausted` exit the old
        # `enumerate(responses[:round_budget])` produced.
        if response_provider is not None:
            response = response_provider(packet)
        else:
            if index >= len(responses):
                break
            response = responses[index]
        # ① -- stale first: a response bound to any earlier world state
        # terminates the loop, loudly, with the residual manifest.
        if response.packet_hash != packet.packet_hash:
            return outcome("stale_packet", compilation)
        this_hash = decision_hash(response)
        if this_hash in seen_hashes:
            return outcome("decision_hash_cycle", compilation)
        bindings, buckets = _validate_response(packet, response)
        pending.extend(response.whole_building_review.findings)
        accumulated.extend(bindings)

        # ②③ -- run the symbolic operations: module 4 rebuilds with the
        # WHOLE accumulated decision history, and every coordinate /
        # thickness / intersection is computed from the frozen artifact,
        # never taken from the response.
        nxt = compile_wall_ir(
            artifact, profile=profile, decisions=tuple(accumulated)
        )
        # ④ -- re-run the deterministic checks on the NEW provisional.
        checks = run_consistency_checks(nxt)
        rounds.append(RoundRecordV1(
            round_index=index,
            packet_hash=packet.packet_hash,
            decision_hash=this_hash,
            selected_item_ids=tuple(sorted(buckets["selected"])),
            rejected_item_ids=tuple(sorted(buckets["rejected"])),
            reperception_item_ids=tuple(sorted(buckets["reperception"])),
            finding_ids=tuple(
                sorted(f.finding_id
                       for f in response.whole_building_review.findings)
            ),
            failed_checks=tuple(
                sorted(c.check for c in checks if not c.passed)
            ),
            provisional_sha256=nxt.content_sha256,
            completion=nxt.completion,
        ))
        previous.append(this_hash)
        seen_hashes.add(this_hash)

        # success: the §6.3 conjunction, in the order it is written.
        if _succeeded(nxt, checks, response, packet, debt_info, profile):
            return outcome("success", nxt)
        # no progress: either decisions were executed and the provisional
        # did not move (a defence seat -- module 4's accounting makes a
        # select always move the hash, so reaching this means a real
        # defect), or NO decision was executed for enough consecutive
        # rounds that the loop is only burning budget.
        moved = nxt.content_sha256 != compilation.content_sha256
        stall_streak = 0 if moved else stall_streak + 1
        if (bindings and not moved) or stall_streak >= _NO_PROGRESS_STALL_LIMIT:
            return outcome("no_progress", nxt)
        compilation = nxt

    return outcome("round_budget_exhausted", compilation)


def _succeeded(
    compilation: WallCompilationV1,
    checks: Sequence[ConsistencyResultV1],
    response: CorrectionDecisionResponseV1,
    packet: CorrectionDecisionPacketV1,
    debt_info: dict[str, tuple[str, str | None]],
    profile: str,
) -> bool:
    """The four-part conjunction of §6.3 -- written as separate ``and``
    terms so each has a fixture that falsifies it alone.

    Third term, rework B-1: the model's overall ``accept`` binds the
    provisional hash THE PACKET carried.  A round whose decisions MOVED
    the geometry therefore cannot succeed on its own accept -- the accept
    was given to the pre-execution provisional, not to the new one; the
    loop must build the next packet and earn a fresh accept that binds
    the new hash (the §6.3 wording, taken literally: 成功条件 3 binds
    ``packet.provisional_geometry``, which only equals the executed
    geometry when nothing moved this round).

    Fourth term, rework B-3: residual debt is judged by the explicit
    profile × debt-kind/channel table below -- NOT by a blanket
    "ambiguous_face only" reading (the cross-review's finding), and not
    by a blanket "every missing_channel blocks" either (its explicit
    non-ask: support-channel absences are honest known-missings)."""
    return (
        not compilation.open_items
        and all(check.passed for check in checks)
        and response.whole_building_review.verdict == "accept"
        and packet.provisional_geometry == compilation.content_sha256
        and not any(
            _debt_blocks_success(
                debt_info[debt_id][0], debt_info[debt_id][1], profile
            )
            for debt_id in compilation.residual_debt_ids
        )
    )


#: Rework B-3 -- the explicit residual-debt policy, per profile.  Key:
#: ``(debt kind, is-the-subject-channel)`` where the subject channel is
#: ``"walls"`` (the product's substance) and everything else is a
#: support channel.  Value: True = blocks success.  Both profile columns
#: are IDENTICAL BY RULING, not by oversight:
#:   * ``ambiguous_face`` blocks everywhere (§9.4: the exploratory end
#:     state is degraded, not successful);
#:   * ``pairs_selection_absent`` blocks everywhere (§6.1: reading pairs
#:     missing is ``reperception_required`` -- correction must not
#:     substitute, in any profile);
#:   * the SUBJECT channel's ``missing_channel`` / ``zero_payload_
#:     channel`` block everywhere ("no walls compiled + empty accept"
#:     must not be washed into a success -- the provisional would be
#:     empty);
#:   * support-channel absences and ``other_known_missing`` block
#:     nowhere (accounted known-missings that travel with the artifact;
#:     demanding their absence makes success structurally unreachable on
#:     every honest bundle).
#: A cell NOT in the table raises instead of guessing: an unregistered
#: combination is a policy gap to close loudly, never a silent pass.
_DEBT_SUCCESS_POLICY: dict[str, dict[tuple[str, bool], bool]] = {
    "strict": {
        ("ambiguous_face", False): True,
        ("ambiguous_face", True): True,
        ("pairs_selection_absent", False): True,
        ("pairs_selection_absent", True): True,
        ("missing_channel", True): True,
        ("missing_channel", False): False,
        ("zero_payload_channel", True): True,
        ("zero_payload_channel", False): False,
        ("other_known_missing", False): False,
        ("other_known_missing", True): False,
    },
    "exploratory": {
        ("ambiguous_face", False): True,
        ("ambiguous_face", True): True,
        ("pairs_selection_absent", False): True,
        ("pairs_selection_absent", True): True,
        ("missing_channel", True): True,
        ("missing_channel", False): False,
        ("zero_payload_channel", True): True,
        ("zero_payload_channel", False): False,
        ("other_known_missing", False): False,
        ("other_known_missing", True): False,
    },
}


def _debt_blocks_success(
    kind: str, channel: str | None, profile: str
) -> bool:
    """One cell of the policy table above; unregistered cells raise."""
    table = _DEBT_SUCCESS_POLICY.get(profile)
    if table is None:
        raise DecisionLoopError(
            "DEBT_POLICY_PROFILE_UNREGISTERED", {"profile": profile}
        )
    key = (kind, channel == "walls")
    if key not in table:
        raise DecisionLoopError(
            "DEBT_POLICY_CELL_UNREGISTERED",
            {"kind": kind, "channel": channel, "profile": profile},
        )
    return table[key]


def _finalize_outcome(
    *,
    success: bool,
    exit_reason: str,
    rounds: list[RoundRecordV1],
    compilation: WallCompilationV1,
    pending: list[FindingV1],
) -> DecisionLoopOutcomeV1:
    outcome = DecisionLoopOutcomeV1(
        success=success,
        exit_reason=exit_reason,  # type: ignore[arg-type]
        rounds=tuple(rounds),
        residual_open_item_ids=tuple(
            sorted(item.item_id for item in compilation.open_items)
        ),
        residual_debt_ids=tuple(compilation.residual_debt_ids),
        degraded_wall_ids=tuple(sorted(
            wall.wall_id for wall in compilation.walls
            if wall.resolved_centerline is None
            or wall.resolved_thickness_m is None
            or wall.output_basis is None
        )),
        pending_findings=tuple(pending),
        final_provisional_sha256=compilation.content_sha256,
        final_completion=compilation.completion,
    )
    content = outcome.model_dump(mode="python")
    content.pop("content_sha256")
    return outcome.model_copy(
        update={"content_sha256": canonical_sha256(content)}
    )


__all__ = [
    "DecisionLoopError",
    "DecisionLoopOutcomeV1",
    "RoundRecordV1",
    "build_decision_packet",
    "decision_hash",
    "run_consistency_checks",
    "run_decision_loop",
]
