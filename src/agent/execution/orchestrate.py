"""Agent-orchestration helpers for the dev baseline workflow (2026-06-16).

In the dev period the main conversational Agent is the orchestrator + judge②
(see AI_agent/workflow/run_case.md). Each stage is run by an isolated
executor (a separate API call or a cold-started sub-agent) so judge / cross-stage
info never pollutes a stage's input. This module gives the orchestrator two thin
primitives over the M0 audit layer:

  - ``file_stage_attempt`` — file ONE draw as an append-only attempt
    (``<stage>/attempts/NNN/{output,checks,judge}``) and move the manifest's
    accepted pointer if it passed. This is where the Agent's judge verdict is
    persisted (judge.json), realizing "attempts 全上".
  - ``summarize_gates`` — roll a set of stage CheckReports into the per-stage
    pass/flag/block counts + the flag detail list that feed _run/baseline.json
    and report/REPORT.md GEN regions.

It deliberately does NOT inject anything into a stage prompt — repair/resample
discipline lives in judge/retry.py (blind resample; judge_retry_context never
injected).
"""

from __future__ import annotations

from pathlib import Path

from src.agent.execution.stage_runner import RecordedAttempt, StageRunner
from src.validator.checks.schema import (
    CheckReport,
    CheckStatus,
    Disposition,
    disposition,
    is_evidence_check_id,
)


def file_stage_attempt(
    runner: StageRunner,
    *,
    stage: str,
    stage_dir: Path,
    output_obj,
    report: CheckReport,
    verdict=None,
    input_hashes: dict[str, str] | None = None,
    accept: bool | None = None,
) -> RecordedAttempt:
    """File one draw as an append-only attempt; persist the judge verdict beside
    it. Returns the RecordedAttempt (carries the attempt dir + accepted flag)."""
    stage_version = "2" if stage == "1_correction" and output_obj.__class__.__name__ == "FinalizeResult" else "1"
    rec = runner.record(
        stage=stage,
        stage_dir=stage_dir,
        output_obj=output_obj,
        report=report,
        input_hashes=input_hashes,
        stage_version=stage_version,
        accept=accept,
    )
    if verdict is not None:
        text = (
            verdict.model_dump_json(indent=2)
            if hasattr(verdict, "model_dump_json")
            else str(verdict)
        )
        Path(rec.attempt_dir).joinpath("judge.json").write_text(text, encoding="utf-8")
    return rec


def summarize_gates(reports: dict[str, CheckReport]) -> dict:
    """Roll stage CheckReports into a report-card summary.

    Returns ``{"gates": {stage: {pass,flag,block,na,skip}}, "flags": [...],
    "blocking": [...]}`` — the machine summary _run/baseline.json embeds and
    report/REPORT.md GEN regions render. Per-view reading keys
    (``0_reading::1f_view``) collapse onto their stage (``0_reading``)."""
    gates: dict[str, dict[str, int]] = {}
    flags: list[dict] = []
    blocking: list[dict] = []
    reading_syntax_valid = True
    reading_evidence_clean = True
    for key, rep in reports.items():
        stage = key.split("::")[0]
        agg = gates.setdefault(stage, {"pass": 0, "flag": 0, "block": 0, "na": 0, "skip": 0})
        for r in rep.results:
            d = disposition(
                r,
                capability_profile=rep.capability_profile,
                run_profile=rep.run_profile,
            )
            if stage == "0_reading":
                if is_evidence_check_id(r.check_id) and d in (Disposition.BLOCK, Disposition.FLAG):
                    reading_evidence_clean = False
                if (
                    not is_evidence_check_id(r.check_id)
                    and r.layer.value == "invariant"
                    and d == Disposition.BLOCK
                ):
                    reading_syntax_valid = False
            if d == Disposition.BLOCK:
                agg["block"] += 1
                blocking.append({"stage": stage, "check": r.check_id, "message": r.message})
            elif d == Disposition.FLAG:
                agg["flag"] += 1
                flags.append({"stage": stage, "check": r.check_id, "message": r.message,
                              "evidence": r.evidence})
            elif d == Disposition.SKIP:
                agg["skip"] += 1
            elif r.status == CheckStatus.NOT_APPLICABLE:
                agg["na"] += 1
            else:  # PASS
                agg["pass"] += 1
    return {
        "gates": gates,
        "flags": flags,
        "blocking": blocking,
        "signals": {
            "reading_syntax_valid": reading_syntax_valid,
            "reading_evidence_clean": reading_evidence_clean,
            "j0_semantic_clean": None,
            "pipeline_recovered": None,
        },
    }
