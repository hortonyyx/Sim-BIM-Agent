"""Offline Stage 2 confirmation/resume with archived inputs and explicit auto actor."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.tool_scripts.run_stage import (
    _auto_start_stage, _draw_modelling, _draw_split_pairing, _render_geometry_viewer, _source_review_digest,
)
from src.agent.correction.parse import ensure_corrected_geometry
from src.agent.execution.manifest import ensure_run_manifest_v2, hash_file
from src.agent.execution.policy import ConfirmationPolicy, RunPolicy
from src.agent.execution.run_policy_freeze import provision_run_policy
from src.agent.execution.stage_runner import StageRunner
from src.agent.execution.step_orchestrator import approve_geometry, geometry_is_approved, run_one_stage, StepStatus
from src.agent.execution.view_manifest import provision_view_manifest
from src.validator.checks.correction import check_correction
from src.validator.checks.schema import CheckReport

CASE = ROOT / "case_tests/e2e_tests/sm21_anchor"
ARCHIVE = CASE / "run_2026-07-02_sonnet_flow_e2e"
SM25 = ROOT / "AI_agent/logs/experiments/2026-09-09_m0_wall_gap_review_run01"


def stage_source(run: Path):
    """Import archived reading/correction into a new run, then build current Stage 2."""
    run.mkdir(parents=True, exist_ok=False)
    vm = provision_view_manifest(CASE, run)
    manifest = ensure_run_manifest_v2(run, view_manifest_sha256=vm.content_sha256)
    provision_run_policy(run, run_profile="exploratory", capability_profile="rectangular")
    runner = StageRunner(run, manifest)
    policy = RunPolicy(confirmation_policy=ConfirmationPolicy.REQUIRED)
    old_reading = ARCHIVE / "0_reading/attempts/001"
    runner.record(stage="0_reading", stage_dir=run / "0_reading",
                  output_obj=json.loads((old_reading / "output.json").read_bytes()),
                  report=CheckReport.model_validate_json((old_reading / "checks.json").read_bytes()),
                  input_hashes={"archived_reading_output": hash_file(old_reading / "output.json")})
    original = ARCHIVE / "1_correction/attempts/001/output.json"
    geom = ensure_corrected_geometry(json.loads(original.read_bytes()))
    correction_report = check_correction(geom)
    assert correction_report.passed
    runner.record(stage="1_correction", stage_dir=run / "1_correction", output_obj=geom, report=correction_report,
                  input_hashes={"archived_correction_output": hash_file(original)})
    manifest.save(run)
    outcome = run_one_stage(stage="2_modelling", runner=runner, stage_dir=run / "2_modelling", policy=policy,
                            draw_fn=lambda _: _draw_modelling(run, policy), geometry_approved=lambda: geometry_is_approved(run))
    manifest.save(run)
    assert outcome.status == StepStatus.AWAITING_GEOMETRY_APPROVAL
    viewer = _render_geometry_viewer(run, CASE)
    assert viewer and Path(viewer).is_file()
    assert not (run / "3_split_pairing").exists()
    return runner, policy, outcome


def run(out: Path):
    out.mkdir(parents=True, exist_ok=False)
    positive = out / "sm21"
    runner, policy, paused = stage_source(positive)
    digest = _source_review_digest(positive)
    calls = []
    refused = run_one_stage(stage="3_split_pairing", runner=runner, stage_dir=positive / "3_split_pairing", policy=policy,
                            draw_fn=lambda _: calls.append("unexpected"), geometry_approved=lambda: geometry_is_approved(positive))
    assert refused.status == StepStatus.AWAITING_GEOMETRY_APPROVAL and not calls
    before = {s: runner.manifest.accepted(s).output_hash for s in ("0_reading", "1_correction", "2_modelling")}
    # This is a labelled automated flow test, never the user's approval.
    approval = approve_geometry(positive, actor="diagnostic:auto", timestamp="", policy="auto",
                                note="Offline confirmation/resume exercise, not human approval", expected_digest=digest)
    assert approval and geometry_is_approved(positive)
    start = _auto_start_stage(manifest=runner.manifest, run_dir=positive, case_dir=CASE,
                              policy=policy, review_switches=set(), to_stage="3_split_pairing")
    assert start == "3_split_pairing"
    resumed = run_one_stage(stage=start, runner=runner, stage_dir=positive / start, policy=policy,
                            draw_fn=lambda _: _draw_split_pairing(positive, policy), geometry_approved=lambda: geometry_is_approved(positive))
    runner.manifest.save(positive)
    assert resumed.status == StepStatus.DETERMINISTIC_PASS
    assert before == {s: runner.manifest.accepted(s).output_hash for s in before}
    assert geometry_is_approved(positive)
    negative = out / "sm25_preview"
    negative.mkdir()
    for relative in ("_run/run_manifest.json", "2_modelling/building_geometry.json",
                     "2_modelling/source_model.json", "2_modelling/kernel_gate_report.json"):
        dest = negative / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SM25 / relative, dest)
    provision_run_policy(negative, run_profile="exploratory", capability_profile="orthogonal_polygon")
    viewer = _render_geometry_viewer(negative, ROOT / "case_tests/e2e_tests/sm25-L_anchor")
    assert viewer and Path(viewer).is_file()
    assert approve_geometry(negative, actor="diagnostic:auto", timestamp="", policy="auto",
                            expected_digest=_source_review_digest(negative)) is None
    assert not (negative / "_run/geometry_approval.json").exists()
    def source_counts(run_dir):
        source = json.loads((run_dir / "2_modelling/source_model.json").read_bytes())
        return {"spaces": len(source["spaces"]), "unbuilt_or_unsupported": len(source["unsupported"])}
    report = {"mode": "historical_input_source_checkpoint_exercise", "model_calls": 0, "solver_calls": 0,
              "code_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "sm21": {"pause": paused.status.value, "checkpoint_stage": paused.stage, "digest": digest,
                       "refused_before_approval": not calls, "actor": approval.actor, "resume_stage": start,
                       "resume_result": resumed.status.value, "upstream_output_hashes_unchanged": before,
                       "source_counts": source_counts(positive)},
              "sm25": {"mode": "unaccepted_candidate_preview", "approval_refused": True,
                       "digest": _source_review_digest(negative), "source": str(SM25.relative_to(ROOT)),
                       "source_counts": source_counts(negative)},
              "not_evaluated": ["new image reading", "human approval", "persistent editing", "EnergyPlus", "browser interaction"]}
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (out / "index.html").write_text('''<!doctype html><html lang="zh"><meta charset="utf-8"><title>Stage 2 源模型确认与恢复</title>
<style>body{font:16px/1.8 system-ui;max-width:900px;margin:40px auto;padding:20px}section{padding:20px;background:#f5f7fa;margin:20px 0}</style>
<h1>Stage 2 源模型确认与恢复</h1><section><h2>sm21：有效源版本</h2>
<p>当前代码重建后停在 Stage 2；未确认时拒绝进入 Stage 3。离线测试以 diagnostic:auto 记录模拟确认，随后直接恢复到 Stage 3，上游产物不重抽。这不是用户人工确认。</p>
<a href="sm21/manual_review/geometry_viewer.html">查看源模型与版本</a></section>
<section><h2>sm25：未接受候选仍可查看</h2><p>29 个空间的候选保留未建门和短边问题，只生成带状态的查看版本，拒绝确认继续。</p>
<a href="sm25_preview/manual_review/geometry_viewer.html">查看带未完成项的候选</a></section>
<p>原图冷启动、持久编辑及仿真出口未在本程完成。</p><a href="report.json">验证报告</a></html>''', encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    print(json.dumps(run(parser.parse_args().out.resolve()), ensure_ascii=False))
