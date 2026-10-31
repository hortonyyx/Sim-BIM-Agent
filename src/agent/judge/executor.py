"""Judge executor (M3, gate ②): run a stage's rubric judge, record append-only.

The judge is the dev-期 data factory (contracts §0.3): it produces structured
verdicts that become (a) supervision labels for a small model and (b) a list of
"which judge experience can be codified into a deterministic check". This module
is model-agnostic: the actual VLM/LLM call is a pluggable ``judge_fn`` so tests
run fully fake (build plan §2.1 M3: no model wording in assertions).

Responsibilities:
  - enforce the judge registry: J0 (0_reading) / J1 (1_correction) are enabled;
    J4 (4_mep text judge) is a DISABLED stub — it returns an explicitly-disabled
    verdict, never a fake PASS, so an empty judge cannot enter the formal flow.
  - charge the global judge budget.
  - append the verdict to an append-only log (one JSON per verdict).
  - quarantine: a blocking verdict that is NOT auto-routable (unknown root /
    low confidence) is marked ``quarantined_failure`` for human triage, not
    auto-routed.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from src.agent.execution.invalidation import RunBudget
from src.agent.judge.verdict import StageVerdict, disabled_verdict

# Rubric id per stage + whether it is enabled in the formal flow.
JUDGE_REGISTRY: dict[str, tuple[str, bool]] = {
    "0_reading": ("J0", True),
    "1_correction": ("J1", True),
    "4_mep": ("J4", False),   # disabled stub — see contracts §0.4 / build plan M3
}


def rubric_for(stage: str) -> tuple[str, bool] | None:
    return JUDGE_REGISTRY.get(stage)


class JudgeOutcome:
    """Result of a judge pass + the routing decision derived from it."""

    def __init__(self, verdict: StageVerdict, *, quarantined: bool) -> None:
        self.verdict = verdict
        self.quarantined = quarantined

    @property
    def disabled(self) -> bool:
        return self.verdict.judge_disabled


def run_judge(
    stage: str,
    artifacts: dict,
    *,
    judge_fn: Callable[[str, str, dict], dict] | None,
    budget: RunBudget | None = None,
    verdict_dir: Path | None = None,
    confidence_threshold: float = 0.6,
) -> JudgeOutcome:
    """Run the stage's rubric judge.

    ``judge_fn(stage, rubric_id, artifacts) -> dict`` is the (pluggable) model
    call returning a StageVerdict-shaped dict. For a disabled rubric (J4) the
    judge_fn is NOT called; a disabled verdict is returned. Appends the verdict to
    ``verdict_dir`` (append-only) and quarantines a blocking-but-unroutable
    verdict."""
    reg = rubric_for(stage)
    if reg is None:
        # No judge defined for this stage (e.g. deterministic 2/3/5).
        v = disabled_verdict(stage, rubric_id="none")
        _append_verdict(verdict_dir, v)
        return JudgeOutcome(v, quarantined=False)

    rubric_id, enabled = reg
    if not enabled:
        v = disabled_verdict(stage, rubric_id)
        _append_verdict(verdict_dir, v)
        return JudgeOutcome(v, quarantined=False)

    if judge_fn is None:
        # Judge unavailable: do not fake a PASS — treat as disabled for this run.
        v = disabled_verdict(stage, rubric_id)
        v.notes = "judge unavailable this run"
        _append_verdict(verdict_dir, v)
        return JudgeOutcome(v, quarantined=False)

    budget = budget or RunBudget()
    budget.charge_judge()
    raw = judge_fn(stage, rubric_id, artifacts)
    verdict = StageVerdict.model_validate({**raw, "stage": stage, "rubric_id": rubric_id})
    _append_verdict(verdict_dir, verdict)

    # Quarantine: blocking but not auto-routable → human triage, not auto-route.
    quarantined = verdict.blocking and not verdict.routable(
        confidence_threshold=confidence_threshold
    )
    return JudgeOutcome(verdict, quarantined=quarantined)


def _append_verdict(verdict_dir: Path | None, verdict: StageVerdict) -> None:
    if verdict_dir is None:
        return
    d = Path(verdict_dir)
    d.mkdir(parents=True, exist_ok=True)
    # append-only: one file per verdict, monotonically numbered.
    existing = [p for p in d.glob("verdict_*.json")]
    idx = len(existing) + 1
    (d / f"verdict_{idx:03d}_{verdict.stage}.json").write_text(
        verdict.model_dump_json(indent=2), encoding="utf-8"
    )
