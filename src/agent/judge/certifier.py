"""Proof-carrying identity diagnostics and request-level arbitration.

Detectors in the segment scorer report source-labelled facts here.  They do
not choose red versus unsupported: only ``certify_and_arbitrate_request`` owns
that request-level decision.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import logging
from typing import Callable, Iterable, Iterator, Mapping

from src.agent.judge.score_schema import ScoreContractError


_logger = logging.getLogger(__name__)


class ProofStatus(str, Enum):
    """Closed proof result returned by every registered evaluator."""

    CERTIFIED_CONFLICT = "CERTIFIED_CONFLICT"
    CONTINGENT = "CONTINGENT"
    DISPROVED = "DISPROVED"
    UNPROVEN = "UNPROVEN"


_FACT_RANK = {
    "source": 0,
    "edge": 1,
    "support_cut": 2,
    "atom_owner": 3,
    "diagnostic": 4,
}


@dataclass(frozen=True, order=True)
class FactNode:
    fact_id: tuple[object, ...]
    phase: str
    operands: tuple[tuple[object, ...], ...] = ()
    enclosure: tuple[float, float] | None = None


@dataclass
class FiniteFactGraph:
    """Finite, forward-ranked fact DAG used by capability closure."""

    nodes: dict[tuple[object, ...], FactNode] = field(default_factory=dict)
    direct_dependents: dict[
        tuple[object, ...], set[tuple[object, ...]]
    ] = field(default_factory=dict)

    def add(self, node: FactNode) -> None:
        if node.phase not in _FACT_RANK:
            raise ValueError(f"unknown fact phase: {node.phase}")
        existing = self.nodes.get(node.fact_id)
        if existing is not None and existing != node:
            raise ValueError(f"fact id collision: {node.fact_id!r}")
        for operand in node.operands:
            parent = self.nodes.get(operand)
            if parent is None:
                raise ValueError(f"missing fact operand: {operand!r}")
            if _FACT_RANK[parent.phase] >= _FACT_RANK[node.phase]:
                raise ValueError(
                    f"non-forward fact arc: {parent.phase} -> {node.phase}"
                )
            self.direct_dependents.setdefault(operand, set()).add(node.fact_id)
        self.nodes[node.fact_id] = node

    def dependency_closure(
        self, seeds: Iterable[tuple[object, ...]]
    ) -> tuple[
        tuple[tuple[object, ...], ...],
        tuple[tuple[tuple[object, ...], tuple[object, ...]], ...],
    ]:
        dependent = set(seeds)
        queue = sorted(dependent, key=repr)
        arcs: set[tuple[tuple[object, ...], tuple[object, ...]]] = set()
        while queue:
            parent = queue.pop(0)
            for child in sorted(
                self.direct_dependents.get(parent, ()), key=repr
            ):
                if child not in dependent:
                    dependent.add(child)
                    arcs.add((parent, child))
                    queue.append(child)
                    queue.sort(key=repr)
        return (
            tuple(sorted(dependent, key=repr)),
            tuple(sorted(arcs, key=repr)),
        )


@dataclass(frozen=True)
class CapabilityEnvelope:
    capability_id: str
    kind: str
    source_edge_id: tuple[object, ...]
    source_vertex_ids: tuple[tuple[object, ...], ...]
    seed_coordinate_keys: tuple[object, ...]
    dependent_fact_ids: tuple[tuple[object, ...], ...]
    dependency_arcs: tuple[
        tuple[tuple[object, ...], tuple[object, ...]], ...
    ]
    fixed_invariants: tuple[tuple[object, ...], ...]
    complete: bool
    side: str = ""
    floor_id: str = ""
    reason: str = "near_orthogonal_advisory_unpaired"
    edge_hex: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConflictWitness:
    predicate: str
    predicate_schema_version: str
    source_edge_ids: tuple[tuple[object, ...], ...] = ()
    source_vertex_ids: tuple[tuple[object, ...], ...] = ()
    owner_ids: tuple[object, ...] = ()
    locus: tuple[tuple[float, float], ...] = ()
    required_fact_ids: tuple[tuple[object, ...], ...] = ()
    fixed_core_fact_ids: tuple[tuple[object, ...], ...] = ()
    direction: str | None = None
    expected_reverse_slots: tuple[tuple[object, ...], ...] = ()


@dataclass(frozen=True)
class JudgeDiagnostic:
    diagnostic_id: str
    requested_code: str
    gate_id: str
    reason: str
    floor_id: str
    witness: ConflictWitness | None
    caused_by: tuple[str, ...] = ()
    side: str = ""
    context: Mapping[str, object] = field(default_factory=dict)
    precertified: bool = False


@dataclass(frozen=True)
class _ExactErrorContextDiagnostic(JudgeDiagnostic):
    """Internal admission diagnostic; no public policy bit exists to set."""


def _with_exact_error_context(
    diagnostic: JudgeDiagnostic,
) -> JudgeDiagnostic:
    """Convert only the audited admission bridge to the internal subtype."""
    return _ExactErrorContextDiagnostic(
        diagnostic_id=diagnostic.diagnostic_id,
        requested_code=diagnostic.requested_code,
        gate_id=diagnostic.gate_id,
        reason=diagnostic.reason,
        floor_id=diagnostic.floor_id,
        witness=diagnostic.witness,
        caused_by=diagnostic.caused_by,
        side=diagnostic.side,
        context=diagnostic.context,
        precertified=diagnostic.precertified,
    )


@dataclass
class AnalysisCollector:
    diagnostics: list[JudgeDiagnostic] = field(default_factory=list)
    capabilities: list[CapabilityEnvelope] = field(default_factory=list)

    def extend(
        self,
        diagnostics: Iterable[JudgeDiagnostic],
        capabilities: Iterable[CapabilityEnvelope],
    ) -> None:
        self.diagnostics.extend(diagnostics)
        self.capabilities.extend(capabilities)


class _CollectedIdentityDiagnostic(RuntimeError):
    """Abort one detector/floor after recording a pure contract diagnostic."""


_ACTIVE_COLLECTOR: ContextVar[AnalysisCollector | None] = ContextVar(
    "judge_identity_analysis_collector", default=None
)


@contextmanager
def collecting_into(collector: AnalysisCollector) -> Iterator[None]:
    token = _ACTIVE_COLLECTOR.set(collector)
    try:
        yield
    finally:
        _ACTIVE_COLLECTOR.reset(token)


def is_collected_identity_abort(exc: BaseException) -> bool:
    return isinstance(exc, _CollectedIdentityDiagnostic)


def canonical_diagnostic_id(
    *,
    side: str,
    floor_id: str,
    reason: str,
    witness: ConflictWitness | None,
) -> str:
    payload = repr((side, floor_id, reason, witness)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def report_identity_diagnostic(diagnostic: JudgeDiagnostic) -> None:
    """Record in an active request, or arbitrate a direct compatibility call."""
    collector = _ACTIVE_COLLECTOR.get()
    if collector is not None:
        collector.diagnostics.append(diagnostic)
        raise _CollectedIdentityDiagnostic(diagnostic.diagnostic_id)
    certify_and_arbitrate_request(
        diagnostics=(diagnostic,),
        capabilities=(),
        evaluator_registry=DEFAULT_EVALUATOR_REGISTRY,
        request_key=("compat", diagnostic.side, diagnostic.floor_id),
        identity_code=diagnostic.requested_code,
    )


Evaluator = Callable[
    [ConflictWitness, tuple[CapabilityEnvelope, ...]], ProofStatus | str
]


def _dependent_facts(
    capabilities: tuple[CapabilityEnvelope, ...],
) -> set[tuple[object, ...]]:
    return {
        fact
        for capability in capabilities
        for fact in capability.dependent_fact_ids
    }


def _common_evaluator(
    witness: ConflictWitness,
    capabilities: tuple[CapabilityEnvelope, ...],
    *,
    minimum_edges: int,
    minimum_owners: int = 0,
    needs_positive_locus: bool = False,
) -> ProofStatus:
    if len(witness.source_edge_ids) < minimum_edges:
        return ProofStatus.UNPROVEN
    if len(witness.owner_ids) < minimum_owners:
        return ProofStatus.UNPROVEN
    if needs_positive_locus:
        if len(witness.locus) != 2 or witness.locus[0] == witness.locus[1]:
            return ProofStatus.UNPROVEN
    dependent = _dependent_facts(capabilities)
    if witness.fixed_core_fact_ids and not (
        set(witness.fixed_core_fact_ids) & dependent
    ):
        return ProofStatus.CERTIFIED_CONFLICT
    required = set(witness.required_fact_ids or witness.source_edge_ids)
    if required & dependent:
        return ProofStatus.CONTINGENT
    return ProofStatus.CERTIFIED_CONFLICT


def evaluate_owner_multiplicity(
    witness: ConflictWitness, capabilities: tuple[CapabilityEnvelope, ...]
) -> ProofStatus:
    if len(witness.source_edge_ids) < 2 or len(witness.owner_ids) < 2:
        return ProofStatus.UNPROVEN
    if len(witness.locus) != 2 or witness.locus[0] == witness.locus[1]:
        return ProofStatus.UNPROVEN
    dependent = _dependent_facts(capabilities)
    fixed_owners = {
        owner
        for edge, owner in zip(
            witness.source_edge_ids, witness.owner_ids, strict=False
        )
        if edge not in dependent
    }
    if len(fixed_owners) >= 2:
        return ProofStatus.CERTIFIED_CONFLICT
    return ProofStatus.CONTINGENT


def evaluate_missing_reverse_owner(
    witness: ConflictWitness, capabilities: tuple[CapabilityEnvelope, ...]
) -> ProofStatus:
    if not witness.expected_reverse_slots:
        return ProofStatus.UNPROVEN
    return _common_evaluator(witness, capabilities, minimum_edges=1)


def evaluate_exterior_interior_conflict(
    witness: ConflictWitness, capabilities: tuple[CapabilityEnvelope, ...]
) -> ProofStatus:
    return _common_evaluator(
        witness,
        capabilities,
        minimum_edges=2,
        minimum_owners=2,
        needs_positive_locus=True,
    )


def evaluate_ring_identity_conflict(
    witness: ConflictWitness, capabilities: tuple[CapabilityEnvelope, ...]
) -> ProofStatus:
    if len(witness.source_vertex_ids) < 2:
        return ProofStatus.UNPROVEN
    return _common_evaluator(witness, capabilities, minimum_edges=0)


def evaluate_segment_merge_conflict(
    witness: ConflictWitness, capabilities: tuple[CapabilityEnvelope, ...]
) -> ProofStatus:
    if len(witness.source_vertex_ids) < 2:
        return ProofStatus.UNPROVEN
    return _common_evaluator(witness, capabilities, minimum_edges=0)


DEFAULT_EVALUATOR_REGISTRY: Mapping[tuple[str, str], Evaluator] = {
    ("owner_multiplicity", "1"): evaluate_owner_multiplicity,
    ("missing_reverse_owner", "1"): evaluate_missing_reverse_owner,
    ("exterior_interior_conflict", "1"): evaluate_exterior_interior_conflict,
    ("ring_identity_conflict", "1"): evaluate_ring_identity_conflict,
    ("segment_merge_conflict", "1"): evaluate_segment_merge_conflict,
}


def _coerce_status(value: ProofStatus | str) -> ProofStatus:
    try:
        return value if isinstance(value, ProofStatus) else ProofStatus(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"registered evaluator returned invalid proof status: {value!r}") from exc


def _root_diagnostics(
    certified: list[JudgeDiagnostic],
) -> list[JudgeDiagnostic]:
    ids = {item.diagnostic_id for item in certified}
    derivative_ids = {
        item.diagnostic_id
        for item in certified
        if any(parent in ids for parent in item.caused_by)
    }
    return [item for item in certified if item.diagnostic_id not in derivative_ids]


def _diagnostic_sort_key(item: JudgeDiagnostic) -> tuple[object, ...]:
    locus = () if item.witness is None else item.witness.locus
    return item.side, item.floor_id, repr(locus), item.diagnostic_id


def _error_context(
    diagnostic: JudgeDiagnostic,
    status: ProofStatus,
    capabilities: tuple[CapabilityEnvelope, ...],
) -> dict[str, object]:
    witness = diagnostic.witness
    assert witness is not None
    if isinstance(diagnostic, _ExactErrorContextDiagnostic):
        return {"reason": diagnostic.reason}
    dependent = _dependent_facts(capabilities)
    fixed_edges = tuple(
        edge for edge in witness.source_edge_ids if edge not in dependent
    )
    if witness.predicate == "owner_multiplicity":
        fixed_owners = tuple(
            owner
            for edge, owner in zip(
                witness.source_edge_ids, witness.owner_ids, strict=False
            )
            if edge not in dependent
        )
        fixed_edges = fixed_edges[:2]
        fixed_owners = fixed_owners[:2]
    else:
        fixed_owners = tuple(witness.owner_ids)
    depends = tuple(
        sorted(
            (
                capability.capability_id
                for capability in capabilities
                if set(witness.required_fact_ids or witness.source_edge_ids)
                & set(capability.dependent_fact_ids)
            )
        )
    )
    context = dict(diagnostic.context)
    context.update({
        "reason": diagnostic.reason,
        "floor_id": diagnostic.floor_id,
        "authority": "scoring_identity",
        "proof_status": status.value,
        "predicate": witness.predicate,
        "predicate_schema_version": witness.predicate_schema_version,
        "owner_ids": fixed_owners,
        "source_edge_ids": fixed_edges,
        "source_vertex_ids": tuple(witness.source_vertex_ids),
        "depends_on_capability_ids": depends,
        "diagnostic_id": diagnostic.diagnostic_id,
        "caused_by": tuple(diagnostic.caused_by),
    })
    return context


def _capability_context(
    capability: CapabilityEnvelope | None,
    *,
    reason: str,
    resolution: str,
    diagnostic_audit: tuple[tuple[object, ...], ...] = (),
) -> dict[str, object]:
    context: dict[str, object] = {
        "reason": reason,
        "resolution": resolution,
        "diagnostic_audit": diagnostic_audit,
    }
    if capability is not None:
        context.update(
            {
                "floor_id": capability.floor_id,
                "capability_id": capability.capability_id,
                "source_edge_id": capability.source_edge_id,
                "source_vertex_ids": capability.source_vertex_ids,
                "seed_coordinate_keys": capability.seed_coordinate_keys,
                "dependent_fact_ids": capability.dependent_fact_ids,
                "dependency_arcs": capability.dependency_arcs,
                "fixed_invariants": capability.fixed_invariants,
                "complete": capability.complete,
                "edge_hex": capability.edge_hex,
            }
        )
    return context


def certify_and_arbitrate_request(
    diagnostics: Iterable[JudgeDiagnostic],
    capabilities: Iterable[CapabilityEnvelope],
    evaluator_registry: Mapping[tuple[str, str], Evaluator],
    request_key: object,
    identity_code: str,
) -> None:
    """Certify every claim, then make the sole request-level severity decision."""
    ordered_diagnostics = tuple(sorted(diagnostics, key=_diagnostic_sort_key))
    ordered_capabilities = tuple(
        sorted(capabilities, key=lambda item: item.capability_id)
    )
    seen: dict[str, JudgeDiagnostic] = {}
    unique: list[JudgeDiagnostic] = []
    for item in ordered_diagnostics:
        prior = seen.get(item.diagnostic_id)
        if prior is not None:
            if prior != item:
                raise ValueError(
                    f"diagnostic id collision with different evidence: {item.diagnostic_id}"
                )
            continue
        seen[item.diagnostic_id] = item
        unique.append(item)

    certified: list[tuple[JudgeDiagnostic, ProofStatus]] = []
    uncertain: list[JudgeDiagnostic] = []
    missing: list[tuple[JudgeDiagnostic, str, str]] = []
    proof_audit: list[tuple[object, ...]] = []
    final_outcome = "scored"
    try:
        for diagnostic in unique:
            witness = diagnostic.witness
            if witness is None:
                uncertain.append(diagnostic)
                proof_audit.append(
                    (
                        diagnostic.diagnostic_id,
                        None,
                        None,
                        ProofStatus.UNPROVEN.value,
                        "missing_witness",
                    )
                )
                continue
            if diagnostic.precertified:
                certified.append(
                    (diagnostic, ProofStatus.CERTIFIED_CONFLICT)
                )
                proof_audit.append(
                    (
                        diagnostic.diagnostic_id,
                        witness.predicate,
                        witness.predicate_schema_version,
                        ProofStatus.CERTIFIED_CONFLICT.value,
                        (),
                    )
                )
                continue
            key = (witness.predicate, witness.predicate_schema_version)
            evaluator = evaluator_registry.get(key)
            if evaluator is None:
                missing.append((diagnostic, key[0], key[1]))
                uncertain.append(diagnostic)
                proof_audit.append(
                    (
                        diagnostic.diagnostic_id,
                        key[0],
                        key[1],
                        ProofStatus.UNPROVEN.value,
                        "missing_predicate_evaluator",
                    )
                )
                _logger.info(
                    "judge_certifier_missing_evaluator",
                    extra={
                        "event": "judge_certifier_missing_evaluator",
                        "request_key": request_key,
                        "side": diagnostic.side,
                        "floor_id": diagnostic.floor_id,
                        "diagnostic_id": diagnostic.diagnostic_id,
                        "predicate": key[0],
                        "predicate_schema_version": key[1],
                        "requested_code": diagnostic.requested_code,
                        "resolution": "diagnostic_evidence_incomplete",
                    },
                )
                continue
            status = _coerce_status(
                evaluator(witness, ordered_capabilities)
            )
            dependencies = tuple(
                sorted(
                    capability.capability_id
                    for capability in ordered_capabilities
                    if set(
                        witness.required_fact_ids
                        or witness.source_edge_ids
                    )
                    & set(capability.dependent_fact_ids)
                )
            )
            proof_audit.append(
                (
                    diagnostic.diagnostic_id,
                    witness.predicate,
                    witness.predicate_schema_version,
                    status.value,
                    dependencies,
                )
            )
            if status is ProofStatus.CERTIFIED_CONFLICT:
                certified.append((diagnostic, status))
            elif status in (ProofStatus.CONTINGENT, ProofStatus.UNPROVEN):
                uncertain.append(diagnostic)

        if certified:
            final_outcome = "red"
            roots = _root_diagnostics([item for item, _ in certified])
            selected = min(roots or [item for item, _ in certified], key=_diagnostic_sort_key)
            status = next(
                proof for item, proof in certified if item.diagnostic_id == selected.diagnostic_id
            )
            raise ScoreContractError(
                selected.requested_code,
                selected.gate_id,
                context=_error_context(selected, status, ordered_capabilities),
            )

        if uncertain or ordered_capabilities:
            final_outcome = "na"
            if any(item.witness is None for item in uncertain):
                reason = "diagnostic_evidence_incomplete"
                resolution = "missing_witness"
                capability = None
            elif missing:
                reason = "diagnostic_evidence_incomplete"
                resolution = "missing_predicate_evaluator"
                capability = None
            else:
                capability = ordered_capabilities[0] if ordered_capabilities else None
                reason = (
                    capability.reason
                    if capability is not None
                    else "diagnostic_evidence_incomplete"
                )
                resolution = (
                    "capability_contingent"
                    if capability is not None
                    else "unproven_witness"
                )
            raise ScoreContractError(
                "score_unsupported_combination",
                "scoring.capability",
                context=_capability_context(
                    capability,
                    reason=reason,
                    resolution=resolution,
                    diagnostic_audit=tuple(
                        sorted(proof_audit, key=repr)
                    ),
                ),
            )
    finally:
        histogram_counts: dict[tuple[str, str], int] = {}
        for _, predicate, version in missing:
            histogram_counts[(predicate, version)] = (
                histogram_counts.get((predicate, version), 0) + 1
            )
        _logger.info(
            "judge_certifier_missing_evaluator_summary",
            extra={
                "event": "judge_certifier_missing_evaluator_summary",
                "request_key": request_key,
                "missing_predicate_evaluator_count": len(missing),
                "predicate_histogram": tuple(
                    (predicate, version, count)
                    for (predicate, version), count in sorted(
                        histogram_counts.items()
                    )
                ),
                "diagnostic_ids": tuple(
                    sorted(item.diagnostic_id for item, _, _ in missing)
                ),
                "final_outcome": final_outcome,
            },
        )
