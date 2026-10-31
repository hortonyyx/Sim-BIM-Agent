"""Failure classification → routing decision (M2a/M3 seam, contracts §0.3).

A stage failure is classified by *the stage's capability* before any routing
decision — never "always bounce upstream" (Codex design H1). Given a blocking
CheckReport, :func:`route_stage_failure` returns the action the orchestrator must
take:

  capability      blocking failure → action
  ─────────────   ─────────────────────────────────────────────────────────────
  manual          ``human_redraw_required`` by default (0_reading); when
                  RunPolicy.reading_runner_available is enabled, the step
                  orchestrator can return ``awaiting_reread`` so the main Agent
                  runs the blind cold-start sub-agent protocol
  stochastic      ``blind_resample``         (1_correction / 4_mep: same input,
                  different sampling; judge text never injected into the prompt)
  deterministic   ``fail_closed``            (core / 2 / 3 / 5: a code defect to
                  raise; NEVER bounce upstream or swap the sample to mask it)

A clean report routes to ``proceed``. The orchestrator consults the budget
(invalidation.RunBudget) before acting on ``blind_resample``.
"""

from __future__ import annotations

from enum import Enum

from src.agent.execution.stage_runner import Capability, stage_spec
from src.validator.checks.schema import CheckReport


class RouteAction(str, Enum):
    PROCEED = "proceed"
    HUMAN_REDRAW_REQUIRED = "human_redraw_required"
    BLIND_RESAMPLE = "blind_resample"
    FAIL_CLOSED = "fail_closed"


def route_stage_failure(stage: str, report: CheckReport) -> RouteAction:
    """Classify a stage's CheckReport into a routing action."""
    if report.passed:  # no blocking results
        return RouteAction.PROCEED
    cap = stage_spec(stage).capability
    if cap == Capability.MANUAL:
        return RouteAction.HUMAN_REDRAW_REQUIRED
    if cap == Capability.STOCHASTIC:
        return RouteAction.BLIND_RESAMPLE
    # deterministic: fail-closed, code defect — caller raises.
    return RouteAction.FAIL_CLOSED
