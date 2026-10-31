"""Invalidation DAG + resume + global draw/judge budget (M0).

When a stage's accepted output changes, everything downstream of it is stale and
its manifest pointer must be dropped — otherwise a resume would reuse a cached
attempt computed against an input that no longer exists. The full DAG
(施工方案 M0, re-verify must-fix):

    0_reading      → 1,2,3,4,5
    1_correction   → 2,3,4,5
    2_modelling    → 3,4,5
    3_split_pairing→ 4,5
    4_mep          → 5

Resume reuses already-accepted attempts whose recorded input hashes still match;
the first stage whose inputs drifted (or that was invalidated) re-runs, and
everything after it re-runs too. Crucially this means an approved geometry
checkpoint is NOT silently regenerated (approval.py binds the digest).

The budget guards against runaway routing: per-stage draw cap + a global
draw/judge cap + a simple route-cycle detector.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from src.agent.execution.manifest import RunManifest
from src.agent.execution.stage_runner import STAGE_ORDER, STAGE_REGISTRY

# Direct dependents (transitive closure computed below).
_DIRECT_DEPENDENTS: dict[str, list[str]] = {}
for _s, _spec in STAGE_REGISTRY.items():
    for _dep in _spec.depends_on:
        _DIRECT_DEPENDENTS.setdefault(_dep, []).append(_s)


def downstream_of(stage: str) -> list[str]:
    """All stages transitively downstream of ``stage`` (pipeline order)."""
    seen: set[str] = set()
    frontier = list(_DIRECT_DEPENDENTS.get(stage, []))
    while frontier:
        cur = frontier.pop()
        if cur in seen:
            continue
        seen.add(cur)
        frontier.extend(_DIRECT_DEPENDENTS.get(cur, []))
    return [s for s in STAGE_ORDER if s in seen]


def invalidate(manifest: RunManifest, stage: str) -> list[str]:
    """Drop the manifest pointers for every stage downstream of ``stage``.

    Returns the list of stages whose accepted pointer was removed. The attempt
    *directories* are left on disk (append-only); only the acceptance pointer is
    cleared, so a resume will re-run them.
    """
    dropped = []
    for s in downstream_of(stage):
        if s in manifest.stages:
            del manifest.stages[s]
            dropped.append(s)
    return dropped


def stages_to_run(
    manifest: RunManifest, current_input_hashes: dict[str, dict[str, str]]
) -> list[str]:
    """Decide which stages a resume must re-run.

    ``current_input_hashes[stage]`` is the *current* hash of each input that
    stage consumes. A stage re-runs if: it has no accepted record, its recorded
    input hashes differ from current, or any upstream stage re-runs (downstream
    contagion). Returns stages in pipeline order.
    """
    rerun: set[str] = set()
    for stage in STAGE_ORDER:
        rec = manifest.accepted(stage)
        spec = STAGE_REGISTRY[stage]
        # Upstream contagion: if any dependency re-runs, this re-runs.
        if any(dep in rerun for dep in spec.depends_on):
            rerun.add(stage)
            continue
        if rec is None:
            rerun.add(stage)
            continue
        cur = current_input_hashes.get(stage, {})
        if rec.input_hashes != cur:
            rerun.add(stage)
    return [s for s in STAGE_ORDER if s in rerun]


# --------------------------------------------------------------------------- #
# budget + cycle detection
# --------------------------------------------------------------------------- #
class BudgetExceeded(RuntimeError):
    pass


class RouteCycleDetected(RuntimeError):
    pass


class RunBudget(BaseModel):
    """Per-stage draw cap + global draw/judge caps + route-cycle detection."""

    model_config = ConfigDict(extra="forbid")

    per_stage_draws: int = 3
    global_draws: int = 30
    global_judges: int = 30
    # Max times the orchestrator may route back to the *same* stage before we
    # call it a cycle and quarantine.
    max_routes_to_stage: int = 5

    draws_used: dict[str, int] = Field(default_factory=dict)
    judges_used: int = 0
    routes_to_stage: dict[str, int] = Field(default_factory=dict)

    @property
    def total_draws(self) -> int:
        return sum(self.draws_used.values())

    def charge_draw(self, stage: str) -> None:
        n = self.draws_used.get(stage, 0) + 1
        if n > self.per_stage_draws:
            raise BudgetExceeded(
                f"stage '{stage}' exceeded per-stage draw budget "
                f"({self.per_stage_draws}); quarantine as quarantined_failure"
            )
        if self.total_draws + 1 > self.global_draws:
            raise BudgetExceeded(
                f"run exceeded global draw budget ({self.global_draws})"
            )
        self.draws_used[stage] = n

    def charge_judge(self) -> None:
        if self.judges_used + 1 > self.global_judges:
            raise BudgetExceeded(
                f"run exceeded global judge budget ({self.global_judges})"
            )
        self.judges_used += 1

    def charge_route(self, stage: str) -> None:
        n = self.routes_to_stage.get(stage, 0) + 1
        if n > self.max_routes_to_stage:
            raise RouteCycleDetected(
                f"routed to stage '{stage}' {n} times "
                f"(> {self.max_routes_to_stage}); aborting to avoid a route cycle"
            )
        self.routes_to_stage[stage] = n
