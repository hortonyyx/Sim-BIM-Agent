"""Run policy — the calling-strategy knobs that gate behaviour without changing
what the validators report (M0 CLI policy).

Kept separate from the validators (fact ≠ policy). A caller (CLI / batch / CI)
constructs a :class:`RunPolicy` and the orchestrator consults it to decide:
  - whether the user geometry-confirmation gate blocks (``confirmation_policy``),
  - whether the dev-期 LLM/VLM judge layer runs at all (``judge_enabled``),
  - the draw/judge budget (mapped onto invalidation.RunBudget),
  - the validation scope (``--intake-from`` ⇒ ``downstream_only``: skip 0–4
    validation, only validate the supplied IntakeOutput downstream).
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from src.agent.execution.invalidation import RunBudget


class ConfirmationPolicy(str, Enum):
    REQUIRED = "required"   # unapproved geometry checkpoint BLOCKS
    OPTIONAL = "optional"   # viewer produced, approval recorded if given, never blocks
    DISABLED = "disabled"   # no gate at all (batch / CI / downstream_only)


class ValidationScope(str, Enum):
    FULL = "full"                       # validate every stage 0–5
    DOWNSTREAM_ONLY = "downstream_only"  # --intake-from: only the supplied IntakeOutput


RunProfile = Literal["exploratory", "dev", "golden", "regression"]


class RunPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_policy: ConfirmationPolicy = ConfirmationPolicy.OPTIONAL
    judge_enabled: bool = False  # dev-期 scaffold; off by default (M2 det. first)
    validation_scope: ValidationScope = ValidationScope.FULL
    capability_profile: str = "rectangular"
    run_profile: RunProfile = "exploratory"
    # Whether a full-scope run REQUIRES an EnergyPlus run (EP/EP_run/eplusout.end).
    # Default False = geometry/MEP-only validation (pre-EP) is a first-class mode;
    # a missing .end is then explicitly skipped, NOT silently passed. Set True to
    # make the EP baseline a required, blocking artifact.
    require_ep: bool = False
    # Default False preserves the current manual 0_reading stop. When True, the
    # orchestrator may return awaiting_reread; the main Agent owns the actual
    # cold-start sub-agent protocol and artifact handoff.
    reading_runner_available: bool = False
    budget: RunBudget = RunBudget()

    def confirmation_blocks(self, approved: bool) -> bool:
        """Does an unapproved geometry checkpoint block under this policy?"""
        if self.confirmation_policy == ConfirmationPolicy.REQUIRED:
            return not approved
        return False  # optional / disabled never block
