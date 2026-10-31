"""Stepwise judge-in-the-loop orchestrator (backlog #1, 2026-06-19).

Turns the dev-baseline run from "run the whole chain, then judge after the fact"
into a per-stage BLOCKING loop the main Agent drives turn by turn (the ideal in
AI_agent/workflow/run_case.md §2):

    for each stage 0..5:
        draw (executor) ──► gate① (deterministic check)
                              │ block + stochastic/manual → blind resample ≤ budget
                              │ block + deterministic      → fail-closed (code defect)
                              ▼
                          gate① pass
                              │ judge stage (J0/J1 enabled) → STOP: AWAITING_JUDGE
                              │ source checkpoint (after 2) → STOP: AWAITING_GEOMETRY_APPROVAL
                              │ else (2/3/5, 4_mep J4-disabled) → advance
        Agent submits StageVerdict:
            non-blocking                      → JUDGE_PASS (advance)
            blocking + routable + stochastic  → JUDGE_BLOCK (blind resample, same budget)
            blocking + manual (0_reading)     → AWAITING_REREAD if runner-enabled, else human
            blocking + unattributed/low-conf  → JUDGE_BLOCK_HUMAN (交人, stop)

Disciplines this module mechanically keeps:
  - A judge-driven resample is a BLIND resample — judge commentary is NEVER fed
    into a draw prompt (invariant 6, mirrors judge/retry.py). The draw loop here
    always injects ``None``.
  - Budget is DISK-DERIVED: the per-stage cap counts ``attempts/NNN`` on disk, so
    gate①-block resamples and judge-block resamples draw from the *same* well of
    ``per_stage_draws`` across separate CLI invocations. NOTE this counts *semantic*
    draws — one outer draw = one filed attempt facing gate①. A stochastic executor
    (run_correction/run_mep) may itself retry ≤3× internally for transport /
    malformed-JSON robustness (``_call_json_llm``); that lower layer is NOT the
    gate①/judge resample and never masks a gate①-wrong draw (well-formed-but-wrong
    always reaches the outer loop and is counted), so "反复不过就停" stays intact —
    the only cost is one semantic draw can be >1 LLM call. (Archiving inner rejects
    is a deferred audit refinement — review 2026-06-19 High-2.)
  - The geometry checkpoint is a *calling policy* (ConfirmationPolicy), wired here
    as a blocking human gate: stages 3–5 refuse until the source digest is
    approved (approval.py binds the digest so an approved checkpoint is reused,
    not silently regenerated).

This module owns only the decision logic + the small ``orchestration_state.json``
ledger; the real stage executors / renderers / the judge (the Agent) are injected,
so the core is unit-testable with fakes (no LLM / EP / geometry).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from src.agent.execution.manifest import next_attempt_index
from src.agent.execution.orchestrate import file_stage_attempt
from src.agent.execution.policy import RunPolicy
from src.agent.execution.run_meta import run_meta_path
from src.agent.execution.stage_runner import (
    STAGE_REGISTRY,
    Capability,
    StageRunner,
    stage_spec,
)
from src.agent.judge.verdict import CriterionStatus, Recoverability, StageVerdict
from src.validator.checks.schema import CheckReport

STATE_NAME = "orchestration_state.json"
GEOMETRY_CHECKPOINT_STAGE = "2_modelling"
GEOMETRY_GATED_STAGE = "3_split_pairing"
GEOMETRY_GATED_STAGES = {"3_split_pairing", "4_mep", "5_intakeoutput"}


class StepStatus(str, Enum):
    # --- gate① / advance ---
    AWAITING_JUDGE = "awaiting_judge"                 # gate① passed, judge stage → Agent judges
    DETERMINISTIC_PASS = "deterministic_pass"         # gate① passed, no enabled judge → advance
    AWAITING_GEOMETRY_APPROVAL = "awaiting_geometry_approval"  # human geometry confirm gate
    AWAITING_HUMAN_REVIEW = "awaiting_human_review"   # flow-level human review checkpoint
    AWAITING_REREAD = "awaiting_reread"               # manual reading blocked, runner protocol available
    # --- judge verdict outcomes ---
    JUDGE_PASS = "judge_pass"                         # verdict non-blocking → advance
    JUDGE_BLOCK = "judge_block"                       # verdict blocking, routable → resample
    # --- terminal stops ---
    QUARANTINED = "quarantined"                       # per-stage draw budget spent
    DETERMINISTIC_DEFECT = "deterministic_defect"     # deterministic gate① block (code bug)
    HUMAN_REDRAW_REQUIRED = "human_redraw_required"   # manual stage blocked (0_reading)
    JUDGE_BLOCK_HUMAN = "judge_block_human"           # judge blocked, unattributed → 交人


# Statuses that end the run until a human acts (the orchestrator stops + reports).
TERMINAL_STOP = {
    StepStatus.QUARANTINED,
    StepStatus.DETERMINISTIC_DEFECT,
    StepStatus.HUMAN_REDRAW_REQUIRED,
    StepStatus.JUDGE_BLOCK_HUMAN,
}
# Statuses where gate① passed and the stage is effectively done for advancing.
ADVANCE_OK = {StepStatus.DETERMINISTIC_PASS, StepStatus.JUDGE_PASS}


@dataclass
class StageOutcome:
    stage: str
    status: StepStatus
    attempts_used: int
    accepted_attempt: int | None = None
    report: CheckReport | None = None
    packet: dict | None = None
    message: str = ""
    # When a judge attributes the failure to a (possibly upstream) root stage, the
    # stage a resample / human-redraw should target — not necessarily ``stage``.
    route_target: str | None = None
    recoverable_criteria_count: int = 0

    @property
    def terminal_stop(self) -> bool:
        return self.status in TERMINAL_STOP

    @property
    def can_advance(self) -> bool:
        return self.status in ADVANCE_OK

    def summary(self) -> dict:
        d: dict = {
            "stage": self.stage,
            "status": self.status.value,
            "attempts_used": self.attempts_used,
            "accepted_attempt": self.accepted_attempt,
            "message": self.message,
        }
        if self.route_target is not None:
            d["route_target"] = self.route_target
        if self.recoverable_criteria_count:
            d["recoverable_criteria_count"] = self.recoverable_criteria_count
        if self.report is not None:
            d["gate1"] = _report_summary(self.report)
        return d


# --------------------------------------------------------------------------- #
# small disk helpers
# --------------------------------------------------------------------------- #
def _existing_attempts(stage_dir: Path) -> int:
    return next_attempt_index(stage_dir) - 1


def _attempt_dir(stage_dir: Path, idx: int) -> Path:
    return stage_dir / "attempts" / f"{idx:03d}"


def _sibling_stage_dir_for(stage: str, stage_dir: Path) -> Callable[[str], Path]:
    parent = Path(stage_dir).parent

    def resolve(target: str) -> Path:
        if target == stage:
            return Path(stage_dir)
        return parent / target

    return resolve


def _report_summary(rep: CheckReport) -> dict:
    return {
        "passed": rep.passed,
        "block": len(rep.blocking()),
        "flag": len(rep.flagged()),
    }


def _load_report(stage_dir: Path, idx: int) -> CheckReport | None:
    p = _attempt_dir(stage_dir, idx) / "checks.json"
    if not p.exists():
        return None
    return CheckReport.model_validate_json(p.read_text(encoding="utf-8"))


def _load_verdict(stage_dir: Path, idx: int) -> StageVerdict | None:
    p = _attempt_dir(stage_dir, idx) / "judge.json"
    if not p.exists():
        return None
    try:
        return StageVerdict.model_validate_json(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a malformed verdict is treated as absent
        return None


def _append_verdict_log(verdict_dir: Path | None, verdict: StageVerdict) -> None:
    """Append-only verdict log (mirror judge.executor._append_verdict)."""
    if verdict_dir is None:
        return
    d = Path(verdict_dir)
    d.mkdir(parents=True, exist_ok=True)
    idx = len([p for p in d.glob("verdict_*.json")]) + 1
    (d / f"verdict_{idx:03d}_{verdict.stage}.json").write_text(
        verdict.model_dump_json(indent=2), encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# run a single stage (draw + gate① + classify)
# --------------------------------------------------------------------------- #
def run_one_stage(
    *,
    stage: str,
    runner: StageRunner,
    stage_dir: Path,
    policy: RunPolicy,
    draw_fn: Callable[[str | None], tuple[object, CheckReport]],
    geometry_approved: Callable[[], bool] | None = None,
    packet_fn: Callable[[Path, CheckReport], dict] | None = None,
    stage_dir_for: Callable[[str], Path] | None = None,
    force_draw: bool = False,
) -> StageOutcome:
    """Run one stage to its next decision point.

    ``draw_fn(feedback) -> (output_obj, gate①_report)`` is the injected executor +
    its deterministic check. The loop injects ``None`` (blind). For a stochastic /
    manual stage a gate①-block triggers a blind resample up to the per-stage draw
    budget; for a deterministic stage a gate①-block is fail-closed (code defect).
    """
    stage_dir = Path(stage_dir)
    spec = stage_spec(stage)
    cap = spec.capability
    approved = geometry_approved or (lambda: False)
    stage_dir_for = stage_dir_for or _sibling_stage_dir_for(stage, stage_dir)

    # Guard direct downstream entry as well as sequential flow/resume.
    if stage in GEOMETRY_GATED_STAGES and policy.confirmation_blocks(approved()):
        return StageOutcome(
            stage, StepStatus.AWAITING_GEOMETRY_APPROVAL, _existing_attempts(stage_dir),
            message="source geometry not approved — confirm Stage 2 before downstream processing",
        )

    accepted = runner.manifest.accepted(stage)
    if accepted is not None and not force_draw:
        # already gate①-accepted: re-emit the post-gate① decision without redrawing
        report = _load_report(stage_dir, accepted.accepted_attempt)
        return _post_gate1(stage, stage_dir, accepted.accepted_attempt, report,
                           policy, approved, _existing_attempts(stage_dir), packet_fn,
                           stage_dir_for)

    cap_draws = policy.budget.per_stage_draws
    existing = _existing_attempts(stage_dir)
    last_report: CheckReport | None = None
    attempt_idx: int | None = None
    while True:
        if existing >= cap_draws:
            return StageOutcome(
                stage, StepStatus.QUARANTINED, existing, attempt_idx, last_report,
                message=f"per-stage draw budget ({cap_draws}) spent — quarantine for human triage",
            )
        out, report = draw_fn(None)  # blind: never inject judge/feedback in the loop
        existing += 1
        rec = file_stage_attempt(
            runner, stage=stage, stage_dir=stage_dir, output_obj=out, report=report,
        )
        last_report = report
        attempt_idx = rec.attempt_index
        if report.passed:
            break
        if cap == Capability.DETERMINISTIC:
            return StageOutcome(
                stage, StepStatus.DETERMINISTIC_DEFECT, existing, attempt_idx, report,
                message="deterministic gate① blocked — code defect, fail-closed (no resample)",
            )
        if cap == Capability.MANUAL:
            if policy.reading_runner_available:
                if existing < cap_draws:
                    return StageOutcome(
                        stage, StepStatus.AWAITING_REREAD, existing, attempt_idx, report,
                        route_target=stage,
                        message=(
                            "0_reading gate① blocked — blind sub-agent re-read available "
                            f"(attempts {existing}/{cap_draws})"
                        ),
                    )
                return StageOutcome(
                    stage, StepStatus.QUARANTINED, existing, attempt_idx, report,
                    route_target=stage,
                    message=(
                        "0_reading gate① blocked and re-read budget "
                        f"({cap_draws}) is spent — quarantine for human triage"
                    ),
                )
            return StageOutcome(
                stage, StepStatus.HUMAN_REDRAW_REQUIRED, existing, attempt_idx, report,
                message="0_reading gate① blocked — manual stage needs a human re-trace",
            )
        # stochastic → loop and blind-resample

    return _post_gate1(stage, stage_dir, attempt_idx, last_report, policy,
                       approved, existing, packet_fn, stage_dir_for)


def _post_gate1(
    stage: str, stage_dir: Path, attempt_idx: int,
    report: CheckReport | None, policy: RunPolicy,
    approved: Callable[[], bool], attempts: int,
    packet_fn: Callable[[Path, CheckReport], dict] | None,
    stage_dir_for: Callable[[str], Path],
) -> StageOutcome:
    """gate① passed — decide: judge / geometry-gate / advance."""
    # Source review precedes downstream serialization and uses no Stage 3 output.
    if stage == GEOMETRY_CHECKPOINT_STAGE and policy.confirmation_blocks(approved()):
        return StageOutcome(
            stage, StepStatus.AWAITING_GEOMETRY_APPROVAL, attempts, attempt_idx, report,
            message="geometry built + gate① passed — awaiting human geometry confirmation",
        )

    # lazy import: judge.executor imports from src.agent.execution, so a top-level
    # import here would close an execution<->judge package import cycle.
    from src.agent.judge.executor import rubric_for

    reg = rubric_for(stage)
    if reg is not None and reg[1] and policy.judge_enabled:  # an ENABLED judge (J0 / J1)
        existing_verdict = _load_verdict(stage_dir, attempt_idx)
        if existing_verdict is not None:
            return _verdict_outcome(
                stage, attempt_idx, report, existing_verdict, attempts,
                policy=policy, stage_dir_for=stage_dir_for)
        packet = None
        if packet_fn is not None and report is not None:
            packet = packet_fn(_attempt_dir(stage_dir, attempt_idx), report)
        return StageOutcome(
            stage, StepStatus.AWAITING_JUDGE, attempts, attempt_idx, report, packet=packet,
            message=f"gate① passed — awaiting judge {reg[0]} (you, multimodal)",
        )

    return StageOutcome(
        stage, StepStatus.DETERMINISTIC_PASS, attempts, attempt_idx, report,
        message="gate① passed — no enabled judge for this stage → advance",
    )


# --------------------------------------------------------------------------- #
# submit a judge verdict
# --------------------------------------------------------------------------- #
def submit_verdict(
    *,
    stage: str,
    stage_dir: Path,
    attempt_index: int,
    verdict: StageVerdict,
    verdict_dir: Path | None = None,
    policy: RunPolicy | None = None,
    stage_dir_for: Callable[[str], Path] | None = None,
    confidence_threshold: float = 0.6,
) -> StageOutcome:
    """Persist the Agent's judge verdict beside the accepted attempt + classify it.

    Writes ``attempts/NNN/judge.json`` (per-attempt) and appends to the run's
    append-only verdict log. The verdict is the OUTPUT of the judge — it is never
    fed back into a draw prompt (blind resample discipline)."""
    stage_dir = Path(stage_dir)
    adir = _attempt_dir(stage_dir, attempt_index)
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "judge.json").write_text(verdict.model_dump_json(indent=2), encoding="utf-8")
    _append_verdict_log(verdict_dir, verdict)
    policy = policy or RunPolicy()
    stage_dir_for = stage_dir_for or _sibling_stage_dir_for(stage, stage_dir)
    return _verdict_outcome(
        stage, attempt_index, _load_report(stage_dir, attempt_index), verdict,
        _existing_attempts(stage_dir), confidence_threshold,
        policy=policy, stage_dir_for=stage_dir_for,
    )


def _verdict_outcome(
    stage: str, attempt_index: int, report: CheckReport | None,
    verdict: StageVerdict, attempts: int, confidence_threshold: float = 0.6,
    *,
    policy: RunPolicy | None = None,
    stage_dir_for: Callable[[str], Path] | None = None,
) -> StageOutcome:
    """Classify a judge verdict by its *attributed root stage* (not mechanically the
    judged stage): a J1 verdict whose root is 0_reading must route to a human
    re-trace, not a 1_correction resample (contracts §0.3 upstream_input_failure)."""
    recoverable_count = _recoverable_severe_count(verdict)
    policy = policy or RunPolicy()
    stage_dir_for = stage_dir_for or (lambda target: Path(target))
    if not verdict.blocking:
        if recoverable_count:
            return StageOutcome(
                stage, StepStatus.JUDGE_PASS, attempts, attempt_index, report,
                message=(
                    "judge verdict pass-through "
                    f"({recoverable_count} severe/fatal correction-recoverable "
                    "J0 criterion/criteria) → advance to correction"
                ),
                recoverable_criteria_count=recoverable_count,
            )
        return StageOutcome(
            stage, StepStatus.JUDGE_PASS, attempts, attempt_index, report,
            message="judge verdict non-blocking (pass/minor) → advance",
        )
    if not verdict.routable(confidence_threshold=confidence_threshold):
        return StageOutcome(
            stage, StepStatus.JUDGE_BLOCK_HUMAN, attempts, attempt_index, report,
            message="judge blocked but root unattributed / low confidence → human triage",
        )
    # routable: route to the judge-attributed root stage, applying ITS capability.
    target = verdict.root_stage
    if target not in STAGE_REGISTRY:
        return StageOutcome(
            stage, StepStatus.JUDGE_BLOCK_HUMAN, attempts, attempt_index, report,
            route_target=target,
            message=f"judge blocked, attributed root '{target}' is not a known stage → human triage",
        )
    target_cap = stage_spec(target).capability
    if target_cap == Capability.MANUAL:
        if policy.reading_runner_available:
            cap_draws = policy.budget.per_stage_draws
            target_attempts = _existing_attempts(Path(stage_dir_for(target)))
            if target_attempts < cap_draws:
                return StageOutcome(
                    stage, StepStatus.AWAITING_REREAD, target_attempts, attempt_index, report,
                    route_target=target,
                    message=(
                        f"judge blocked, root='{target}' (manual) — blind sub-agent "
                        f"re-read available (attempts {target_attempts}/{cap_draws})"
                    ),
                )
            return StageOutcome(
                stage, StepStatus.QUARANTINED, target_attempts, attempt_index, report,
                route_target=target,
                message=(
                    f"judge blocked, root='{target}' (manual) but re-read budget "
                    f"({cap_draws}) is spent — quarantine for human triage"
                ),
            )
        return StageOutcome(
            stage, StepStatus.HUMAN_REDRAW_REQUIRED, attempts, attempt_index, report,
            route_target=target,
            message=f"judge blocked, root='{target}' (manual) → human re-trace required",
        )
    if target_cap == Capability.DETERMINISTIC:
        return StageOutcome(
            stage, StepStatus.JUDGE_BLOCK_HUMAN, attempts, attempt_index, report,
            route_target=target,
            message=f"judge blocked, root='{target}' (deterministic) → code defect, human triage",
        )
    return StageOutcome(
        stage, StepStatus.JUDGE_BLOCK, attempts, attempt_index, report,
        route_target=target,
        message=f"judge blocked (routable, root='{target}') → blind resample {target}",
    )


def _recoverable_severe_count(verdict: StageVerdict) -> int:
    if verdict.rubric_id != "J0":
        return 0
    return sum(
        1
        for c in verdict.criteria
        if c.status in (CriterionStatus.SEVERE, CriterionStatus.FATAL)
        and c.recoverability == Recoverability.CORRECTION_RECOVERABLE
    )


# --------------------------------------------------------------------------- #
# geometry approval (human confirmation gate)
# --------------------------------------------------------------------------- #
def approve_geometry(
    run_dir: Path,
    *,
    actor: str,
    timestamp: str,
    policy: str = "required",
    note: str = "",
    case_dir: Path | None = None,
    expected_digest: str | None = None,
):
    """Record the human geometry confirmation, binding it to the current checkpoint
    digest. Returns the saved GeometryApproval, or None if the geometry is not
    present / not consistent (nothing valid to approve)."""
    from src.agent.execution.approval import GeometryApproval
    from src.agent.execution.run_policy_freeze import (
        effective_run_policy,
        resolve_frozen_run_policy,
    )
    from src.agent.execution.source_checkpoint import SCHEMA, current_source_review
    from src.agent.execution.manifest import hash_obj

    run_dir = Path(run_dir)
    # R1-5 (裁定 §1.3): judge the geometry confirmation on the run's FROZEN
    # policy, not RunPolicy() defaults (laxest exploratory/rectangular/optional).
    frozen = resolve_frozen_run_policy(run_dir)
    effective = effective_run_policy(run_dir)
    if expected_digest is None:
        return None
    review = current_source_review(run_dir, policy=effective, expected_digest=expected_digest)
    if review is None:
        return None
    appr = GeometryApproval(
        digest=review["digest"], actor=actor, policy=policy,
        timestamp=timestamp, note=note,
        run_policy_source=frozen.source,
        run_policy_legacy_defaulted=frozen.legacy_defaulted,
        run_profile=effective.run_profile,
        capability_profile=effective.capability_profile,
        checkpoint_schema=SCHEMA, review_sha256=hash_obj(review),
    )
    appr.save(run_dir)
    return appr


def geometry_is_approved(run_dir: Path, *, case_dir: Path | None = None) -> bool:
    """True iff a stored approval matches the current geometry checkpoint digest."""
    from src.agent.execution.run_policy_freeze import effective_run_policy
    from src.agent.execution.source_checkpoint import SCHEMA, current_source_review
    from src.agent.execution.approval import GeometryApproval
    from src.agent.execution.manifest import hash_obj

    # R1-5 (裁定 §1.3): judge on the run's FROZEN policy, not RunPolicy() defaults.
    review = current_source_review(Path(run_dir), policy=effective_run_policy(run_dir))
    try:
        approval = GeometryApproval.load(run_dir)
    except (ValueError, OSError):
        return False
    return bool(review and approval and approval.checkpoint_schema == SCHEMA
                and approval.digest == review["digest"] and approval.review_sha256 == hash_obj(review))


# --------------------------------------------------------------------------- #
# orchestration state ledger (feeds record_baseline's stop-reason)
# --------------------------------------------------------------------------- #
def load_state(run_dir: Path) -> dict:
    p = run_meta_path(run_dir, STATE_NAME)
    if not p.exists():
        return {"stages": {}, "stop_reason": None, "updated": ""}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"stages": {}, "stop_reason": None, "updated": ""}


def update_state(run_dir: Path, outcome: StageOutcome, *, timestamp: str = "") -> dict:
    """Merge one stage outcome into the run's orchestration ledger + set stop_reason.

    The orchestration is sequential, so stop_reason is derived from the LATEST
    outcome: a terminal-stop / geometry-wait sets it; any forward move (advance,
    judge pass, awaiting judge) clears it — so e.g. a successful 4_mep advance after
    geometry approval does NOT leave a stale ``awaiting_geometry_approval@3...``
    behind (record_baseline would otherwise report a stopped run)."""
    state = load_state(run_dir)
    state.setdefault("stages", {})[outcome.stage] = outcome.summary()
    state["updated"] = timestamp
    if outcome.terminal_stop:
        state["stop_reason"] = f"{outcome.status.value}@{outcome.stage}"
    elif outcome.status in (
        StepStatus.AWAITING_GEOMETRY_APPROVAL,
        StepStatus.AWAITING_HUMAN_REVIEW,
        StepStatus.AWAITING_REREAD,
    ):
        state["stop_reason"] = f"{outcome.status.value}@{outcome.stage}"
    else:
        state["stop_reason"] = None
    run_meta_path(run_dir, STATE_NAME, for_write=True).write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return state


def mark_geometry_approved(run_dir: Path, *, timestamp: str = "") -> dict:
    """Reflect a recorded geometry approval in the ledger: set ``geometry_approved``
    and clear a pending geometry-confirmation stop_reason (so the run is not still
    reported as stopped before 4_mep runs)."""
    state = load_state(run_dir)
    state["geometry_approved"] = True
    state["updated"] = timestamp
    sr = state.get("stop_reason")
    if sr and sr.startswith("awaiting_geometry_approval"):
        state["stop_reason"] = None
    run_meta_path(run_dir, STATE_NAME, for_write=True).write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return state


def mark_review_approved(run_dir: Path, stage: str, *, timestamp: str = "") -> dict:
    """Reflect a durable human review approval in the ledger.

    The hash-bound review record remains the authority for resume. This only
    clears the visible pending stop_reason when it matches the approved stage.
    """
    state = load_state(run_dir)
    state.setdefault("human_review_approved", {})[stage] = True
    state["updated"] = timestamp
    sr = state.get("stop_reason")
    if sr == f"{StepStatus.AWAITING_HUMAN_REVIEW.value}@{stage}":
        state["stop_reason"] = None
    run_meta_path(run_dir, STATE_NAME, for_write=True).write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return state


__all__ = [
    "StepStatus",
    "StageOutcome",
    "TERMINAL_STOP",
    "ADVANCE_OK",
    "run_one_stage",
    "submit_verdict",
    "approve_geometry",
    "geometry_is_approved",
    "load_state",
    "update_state",
    "mark_geometry_approved",
    "mark_review_approved",
    "STATE_NAME",
    "GEOMETRY_CHECKPOINT_STAGE",
    "GEOMETRY_GATED_STAGE",
]
