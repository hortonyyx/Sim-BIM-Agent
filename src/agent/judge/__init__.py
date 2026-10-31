"""Judge harness (M3): verdict schema v2, single-stage blind resample, executor.

The judge is dev-期 scaffolding / a data factory — once a small model is trained
and enough error classes are codified into deterministic checks, the top-tier
judge is retired (contracts §0.3). It never injects commentary into a stage
prompt (invariant 6); retries are blind resamples.
"""

from __future__ import annotations

from src.agent.judge.executor import (
    JUDGE_REGISTRY,
    JudgeOutcome,
    rubric_for,
    run_judge,
)
from src.agent.judge.retry import StageDrawResult, draw_json_once, retry_stage_draw
from src.agent.judge.verdict import (
    CriterionStatus,
    CriterionVerdict,
    Recoverability,
    StageVerdict,
    disabled_verdict,
)

__all__ = [
    "JUDGE_REGISTRY",
    "JudgeOutcome",
    "rubric_for",
    "run_judge",
    "StageDrawResult",
    "draw_json_once",
    "retry_stage_draw",
    "CriterionStatus",
    "CriterionVerdict",
    "Recoverability",
    "StageVerdict",
    "disabled_verdict",
]
