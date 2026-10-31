"""Judge verdict schema v2 (M3, contracts §0.3 gate ②).

A judge verdict is a STRUCTURED CHECKLIST, not a numeric score (this project is
qualitative > quantitative). Each rubric criterion gets a status + evidence; the
verdict also carries attribution fields the orchestrator uses to route:

  - ``root_stage`` / ``root_confidence``: which stage the judge believes is the
    root cause, and how sure. ``root_stage=None`` or low confidence ⇒ NOT
    auto-routed (contracts §0.3: "unknown 不得自动路由").
  - ``retriable``: whether a blind resample could plausibly fix it.

Criterion statuses include ``not_applicable`` and ``insufficient_evidence`` so a
judge can decline to rule rather than guess (design L2). A verdict blocks the
stage when any criterion is SEVERE or FATAL, except for J0 criteria explicitly
marked correction-recoverable.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class CriterionStatus(str, Enum):
    PASS = "pass"
    MINOR = "minor"
    SEVERE = "severe"
    FATAL = "fatal"
    NOT_APPLICABLE = "not_applicable"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class Recoverability(str, Enum):
    CORRECTION_RECOVERABLE = "correction_recoverable"
    UNRECOVERABLE = "unrecoverable"
    UNKNOWN = "unknown"


class CriterionVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criterion: str
    status: CriterionStatus
    recoverability: Recoverability | None = None
    evidence: str = ""


class StageVerdict(BaseModel):
    """One judge pass over a stage's artifacts."""

    model_config = ConfigDict(extra="forbid")

    stage: str
    rubric_id: str                       # e.g. "J0" / "J1"
    criteria: list[CriterionVerdict] = Field(default_factory=list)
    root_stage: str | None = None        # None = not attributed / unknown
    root_confidence: float = 0.0         # 0..1
    retriable: bool = True
    judge_disabled: bool = False         # J4 stub: ran a disabled judge, not a PASS
    notes: str = ""

    @property
    def blocking(self) -> bool:
        return any(
            c.status in (CriterionStatus.SEVERE, CriterionStatus.FATAL)
            and not (
                self.rubric_id == "J0"
                and c.recoverability == Recoverability.CORRECTION_RECOVERABLE
            )
            for c in self.criteria
        )

    def routable(self, *, confidence_threshold: float = 0.6) -> bool:
        """Auto-routable only with an attributed root stage AND enough confidence.
        Unknown / low confidence → not auto-routed (交人)."""
        return (
            self.blocking
            and self.root_stage is not None
            and self.root_confidence >= confidence_threshold
            and not self.judge_disabled
        )


def disabled_verdict(stage: str, rubric_id: str) -> StageVerdict:
    """A judge that is intentionally disabled returns THIS — explicitly disabled,
    never a fake PASS (so an empty judge can't slip into the formal flow)."""
    return StageVerdict(
        stage=stage,
        rubric_id=rubric_id,
        criteria=[],
        judge_disabled=True,
        notes=f"{rubric_id} judge is disabled (stub); no verdict rendered",
    )
