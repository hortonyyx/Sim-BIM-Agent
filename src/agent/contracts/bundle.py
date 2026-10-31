"""One connected building contract with deterministic cross-reference checks."""
from __future__ import annotations

from typing import Literal

from pydantic import model_validator

from src.harness_contracts import BudgetLedger

from ._base import ContractModel, NonEmptyStr, unique
from .declarations import BuildingDeclaration, BuildingModelVersion, InferenceHypothesis
from .evidence import BuildingEvidenceLedger, CalculationChain, UserRequirement
from .quality import CheckResult, CoverageEntry, CoverageIssue, NormalizationProposal
from .tasks import EvidencePackage, FloorDraftContract, LocalizedEvidenceResult


class BuildingContractBundle(ContractModel):
    schema_version: Literal["building_contracts_v1"] = "building_contracts_v1"
    case_id: NonEmptyStr
    requirements: tuple[UserRequirement, ...]
    evidence: BuildingEvidenceLedger
    calculations: tuple[CalculationChain, ...]
    hypotheses: tuple[InferenceHypothesis, ...]
    declarations: tuple[BuildingDeclaration, ...]
    model_versions: tuple[BuildingModelVersion, ...]
    budget_ledger: BudgetLedger | None = None
    evidence_packages: tuple[EvidencePackage, ...] = ()
    evidence_results: tuple[LocalizedEvidenceResult, ...] = ()
    floor_drafts: tuple[FloorDraftContract, ...] = ()
    checks: tuple[CheckResult, ...]
    normalizations: tuple[NormalizationProposal, ...] = ()
    coverage_issues: tuple[CoverageIssue, ...] = ()
    coverage: tuple[CoverageEntry, ...]

    @model_validator(mode="after")
    def validate_bundle(self) -> "BuildingContractBundle":
        requirements = self._index(self.requirements, "requirement_id", "requirement")
        evidence = self._index(self.evidence.items, "evidence_id", "evidence")
        hypotheses = self._index(self.hypotheses, "hypothesis_id", "hypothesis")
        declarations = self._index(self.declarations, "declaration_id", "declaration")
        versions = self._index(self.model_versions, "model_version_id", "model version")
        packages = self._index(self.evidence_packages, "package_id", "evidence package")
        checks = self._index(self.checks, "check_id", "check")
        issues = self._index(self.coverage_issues, "issue_id", "coverage issue")
        self._index(self.calculations, "calculation_id", "calculation")
        self._index(self.coverage, "coverage_id", "coverage")
        self._index(self.normalizations, "proposal_id", "normalization")

        for item in self.evidence.items:
            self._known(item.requirement_ids, requirements, "evidence requirement")
        for chain in self.calculations:
            self._known(chain.evidence_ids, evidence, "calculation evidence")
        for hypothesis in self.hypotheses:
            self._known(hypothesis.requirement_ids, requirements, "hypothesis requirement")
            self._known(hypothesis.evidence_ids, evidence, "hypothesis evidence")
        for declaration in self.declarations:
            self._known(declaration.evidence_ids, evidence, "declaration evidence")
            self._known(declaration.requirement_ids, requirements, "declaration requirement")
            self._known(declaration.hypothesis_ids, hypotheses, "declaration hypothesis")
        for version in self.model_versions:
            self._known(version.declaration_ids, declarations, "model-version declaration")
        for package in self.evidence_packages:
            self._known(package.known_evidence_ids, evidence, "package evidence")
            self._known((package.source_model_version_id,), versions, "package model version")
            if self.budget_ledger is None:
                raise ValueError("evidence packages require the authoritative harness budget ledger")
            reservations = {
                row.reservation_id: row for row in self.budget_ledger.reservations
            }
            reservation = reservations.get(package.budget_reservation_id)
            if reservation is None:
                raise ValueError("evidence package references an unknown budget reservation")
            if reservation.purpose != "child_task":
                raise ValueError("evidence package budget reservation must be for a child_task")
            if reservation.task_id != package.task_id:
                raise ValueError("evidence package task does not match its budget reservation")
        for result in self.evidence_results:
            if result.package_id not in packages:
                raise ValueError(f"result references unknown package: {result.package_id}")
            package = packages[result.package_id]
            if result.task_id != package.task_id:
                raise ValueError("evidence result task does not match its package")
            if result.based_on_source_model_version_id != package.source_model_version_id:
                raise ValueError("evidence result source version does not match its package")
            supplied_images = set(package.image_refs)
            if any(row.location.image_ref not in supplied_images for row in result.directly_seen):
                raise ValueError("evidence result cites an image absent from its package")
        for draft in self.floor_drafts:
            self._known((draft.based_on_source_model_version_id,), versions, "floor draft model version")
            if draft.proposed_declaration.declaration_id in declarations:
                raise ValueError("a draft-only declaration cannot also be an applied declaration")
        for check in self.checks:
            self._known(check.evidence_ids, evidence, "check evidence")
            if check.saved_verification is not None:
                self._known((check.saved_verification.model_version_id,), versions, "check model version")
                version = versions[check.saved_verification.model_version_id]
                if check.saved_verification.artifact_sha256 != version.artifact_sha256:
                    raise ValueError("check verification artifact does not match its model version")
        for proposal in self.normalizations:
            self._known(proposal.check_ids, checks, "normalization check")
            self._known((proposal.before.model_version_id,), versions, "normalization before version")
            self._known((proposal.after.model_version_id,), versions, "normalization after version")
            for snapshot in (proposal.before, proposal.after):
                if snapshot.artifact_sha256 != versions[snapshot.model_version_id].artifact_sha256:
                    raise ValueError("normalization snapshot artifact does not match its model version")
        for entry in self.coverage:
            self._known((entry.requirement_id,), requirements, "coverage requirement")
            self._known((entry.choice_hypothesis_id,), hypotheses, "coverage choice")
            self._known((entry.saved_model_version_id,), versions, "coverage model version")
            self._known(entry.check_ids, checks, "coverage check")
            self._known(entry.open_issue_ids, issues, "coverage issue")
            choice = hypotheses[entry.choice_hypothesis_id]
            if choice.kind != "design_choice" or entry.requirement_id not in choice.requirement_ids:
                raise ValueError("coverage choice must be a design-choice hypothesis for its requirement")
            if entry.coarse_merge and requirements[entry.requirement_id].kind != "simplification":
                raise ValueError("a coarse merge requires an explicit user simplification requirement")
            actual = {(row.kind, row.id) for row in entry.actual_objects}
            version = versions[entry.saved_model_version_id]
            declared = {
                (ref.kind, ref.id)
                for declaration_id in version.declaration_ids
                for ref in declarations[declaration_id].declared_object_refs
            }
            if not actual <= declared:
                raise ValueError("coverage names objects absent from the saved version declarations")
            inspected: set[tuple[str, str]] = set()
            for check_id in entry.check_ids:
                check = checks[check_id]
                if check.saved_verification is not None:
                    if check.saved_verification.model_version_id != entry.saved_model_version_id:
                        raise ValueError("coverage check verified a different saved model version")
                    inspected.update((row.kind, row.id) for row in check.saved_verification.inspected_objects)
                if entry.status == "complete" and (
                    check.status != "passed" or check.saved_verification is None
                ):
                    raise ValueError("complete coverage cannot rely on an unverified or failing check")
            if entry.status == "complete" and not actual <= inspected:
                raise ValueError("coverage checks did not inspect every claimed saved object")
        return self

    @staticmethod
    def _index(rows: tuple, field: str, label: str) -> dict:
        values = [getattr(row, field) for row in rows]
        unique(values, f"{label} IDs")
        return dict(zip(values, rows))

    @staticmethod
    def _known(values: tuple[str, ...], known: dict, label: str) -> None:
        if missing := set(values) - set(known):
            raise ValueError(f"{label} references unknown IDs: {sorted(missing)}")

    def evidence_impact_index(self) -> dict[str, dict[str, tuple[str, ...]]]:
        """Return concrete evidence -> affected declarations and saved model versions."""

        declaration_hits: dict[str, set[str]] = {row.evidence_id: set() for row in self.evidence.items}
        expanded = self.evidence.expanded_object_evidence()
        for declaration in self.declarations:
            effective = set(declaration.evidence_ids)
            for ref in declaration.declared_object_refs:
                effective.update(expanded.get(f"{ref.kind}:{ref.id}", ()))
            for evidence_id in effective:
                declaration_hits[evidence_id].add(declaration.declaration_id)
        version_hits: dict[str, set[str]] = {row.evidence_id: set() for row in self.evidence.items}
        declarations = {row.declaration_id: row for row in self.declarations}
        for version in self.model_versions:
            for declaration_id in version.declaration_ids:
                declaration = declarations[declaration_id]
                effective = set(declaration.evidence_ids)
                for ref in declaration.declared_object_refs:
                    effective.update(expanded.get(f"{ref.kind}:{ref.id}", ()))
                for evidence_id in effective:
                    version_hits[evidence_id].add(version.model_version_id)
        return {
            evidence_id: {
                "declaration_ids": tuple(sorted(declaration_hits[evidence_id])),
                "model_version_ids": tuple(sorted(version_hits[evidence_id])),
            }
            for evidence_id in sorted(declaration_hits)
        }
