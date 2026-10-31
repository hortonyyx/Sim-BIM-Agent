"""Durable geometry approval records and the legacy audit digest.

Current source confirmations use source_checkpoint.py: accepted Stage 1/2,
source identity, displayed geometry, checks and immutable review artifacts.
The Stage 2 orchestrator requires this schema before downstream resume.

geometry_checkpoint_digest() retains the historical 2+3 serialization recipe
for auditing old runs. Such a legacy approval cannot authorize the new source
checkpoint. Confirmation remains a calling policy (required/optional/disabled);
automated confirmations record their actor and policy explicitly.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from src.agent.execution.manifest import combined_digest, hash_obj, hash_text
from src.agent.execution.run_meta import run_meta_path

APPROVAL_NAME = "geometry_approval.json"


def geometry_checkpoint_digest(
    *,
    building_geometry: dict,
    geometry_specs: str,
    kernel_check_report: dict,
    stage_version: str = "1",
    check_version: str = "1",
) -> str:
    """Deterministic digest of the four things an approval binds to. Order of the
    parts does not matter (combined_digest sorts)."""
    return combined_digest(
        [
            hash_obj(building_geometry),
            hash_text(geometry_specs),
            hash_obj(kernel_check_report),
            hash_text(f"stage={stage_version};check={check_version}"),
        ]
    )


class GeometryApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    digest: str
    actor: str                 # who approved (operator id / "ci" / "auto")
    policy: str = "required"   # the confirmation_policy in force at approval time
    timestamp: str = ""        # ISO; stamped by caller (scripts pass it in)
    note: str = ""
    # R1-5: make an approval's policy provenance inspectable.  Old approvals
    # predate the frozen-policy wire and therefore truthfully default to legacy.
    run_policy_source: str = "legacy_defaulted"
    run_policy_legacy_defaulted: bool = True
    run_profile: str = "exploratory"
    capability_profile: str = "rectangular"
    checkpoint_schema: str = "legacy_geometry_checkpoint"
    review_sha256: str | None = None

    # ---- io ----
    @classmethod
    def load(cls, case_dir: Path) -> "GeometryApproval | None":
        p = run_meta_path(case_dir, APPROVAL_NAME)
        if not p.exists():
            return None
        return cls.model_validate_json(p.read_text(encoding="utf-8"))

    def save(self, case_dir: Path) -> Path:
        p = run_meta_path(case_dir, APPROVAL_NAME, for_write=True)
        p.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return p


def is_approved(case_dir: Path, current_digest: str) -> bool:
    """True iff a stored approval matches the current geometry digest. A drifted
    checkpoint (digest mismatch) is treated as unapproved — fail-closed."""
    appr = GeometryApproval.load(case_dir)
    return appr is not None and appr.digest == current_digest
