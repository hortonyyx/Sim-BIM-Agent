"""Single-stage blind-resample harness (M3, build plan §M1 #7 / H2).

Deliberately NOT a reuse of ``_make_correction_validator`` as a generic harness.
Two distinct concerns, two thin functions:

  - ``draw_json_once`` — one LLM draw (the orchestrator's per-call primitive).
  - ``retry_stage_draw`` — resample a single stage until its CheckReport passes or
    the per-stage budget is spent (then quarantine). Cross-stage routing /
    invalidation belong to the execution orchestrator, NOT here.

The critical discipline (tested): the two entry points must NOT cross.
  - ``repair_feedback`` — explicit downstream repair instructions; INJECTED into
    the draw prompt.
  - ``judge_retry_context`` — a judge's commentary; **never injected** (would
    pollute the training data / violate "judge gives no extra process info",
    invariant 6). It is written out-of-band only; the resample stays BLIND.

So a judge_mismatch triggers a *blind* resample (feedback stays None unless a
separate repair_feedback was supplied).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from src.agent.execution.invalidation import BudgetExceeded, RunBudget
from src.validator.checks.schema import CheckReport


@dataclass
class StageDrawResult:
    accepted: bool
    draw: object | None
    report: CheckReport | None
    attempts: int
    quarantined: bool = False


def draw_json_once(call_fn: Callable[[str | None], object], feedback: str | None):
    """One draw. ``call_fn`` is the model invocation; ``feedback`` (or None) is the
    only thing injected into its prompt."""
    return call_fn(feedback)


def retry_stage_draw(
    *,
    stage: str,
    draw_fn: Callable[[str | None], object],
    validate_fn: Callable[[object], CheckReport],
    budget: RunBudget | None = None,
    repair_feedback: str | None = None,
    judge_retry_context: str | None = None,
    out_of_band_sink: Callable[[str], None] | None = None,
) -> StageDrawResult:
    """Blind-resample ``stage`` until its CheckReport passes or budget is spent.

    ``repair_feedback`` is injected; ``judge_retry_context`` is logged out-of-band
    and NEVER injected (blind resample). Returns a quarantined result when the
    per-stage draw budget is exhausted."""
    budget = budget or RunBudget()
    if judge_retry_context is not None and out_of_band_sink is not None:
        out_of_band_sink(
            f"[{stage}] judge_retry_context (OUT-OF-BAND, not injected into prompt): "
            f"{judge_retry_context}"
        )
    # ONLY repair_feedback may be injected. judge commentary stays out of the prompt.
    injected = repair_feedback
    attempts = 0
    last_report: CheckReport | None = None
    while True:
        try:
            budget.charge_draw(stage)
        except BudgetExceeded:
            return StageDrawResult(
                accepted=False, draw=None, report=last_report,
                attempts=attempts, quarantined=True,
            )
        attempts += 1
        draw = draw_json_once(draw_fn, injected)
        report = validate_fn(draw)
        last_report = report
        if report.passed:
            return StageDrawResult(
                accepted=True, draw=draw, report=report, attempts=attempts,
            )
        # Blind resample: do not carry judge commentary forward into the prompt.
