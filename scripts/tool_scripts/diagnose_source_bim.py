"""Rebuild independent source BIM from archived inputs, then evaluate separately.

No image/model/solver calls. sm24 assisted input and sm25 assisted decisions are
explicitly historical interventions; independent GT is only read after export.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.agent.correction.parse import ensure_corrected_geometry
from src.agent.execution.source_bim import export_source_bim
from src.agent.judge.gt import load_gt_document
from src.agent.judge.partition_evidence import reference_partition
from src.agent.judge.source_partition import compare_partitions
from scripts.tool_scripts.diagnose_partition_evidence import overlay

CASES = {
    "sm21": ("sm21_anchor", "case_tests/e2e_tests/sm21_anchor/run_2026-07-02_sonnet_flow_e2e", None, "historical_rebuild"),
    "sm24_wrong_partition": ("sm24_anchor", "case_tests/e2e_tests/sm24_anchor/run_2026-06-24_opus_reading", None, "historical_counterexample"),
    "sm24_assisted": ("sm24_anchor", "AI_agent/logs/experiments/2026-09-09_m0_wall_openings_run01/assisted_model", None, "historical_assisted_partition_and_one_door"),
    "sm25": ("sm25-L_anchor", "AI_agent/logs/experiments/2026-09-09_m0_wall_gap_review_run01", 1, "historical_assisted_wall_gap_decisions"),
}


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n")


def exercise_source_flow(out):
    """Replay upstream inputs, then use the real CLI source-target flow."""
    from src.agent.execution.manifest import ensure_run_manifest_v2, hash_file
    from src.agent.execution.stage_runner import StageRunner
    from src.agent.execution.view_manifest import provision_view_manifest
    from src.agent.execution.run_policy_freeze import provision_run_policy
    from src.validator.checks.schema import CheckReport
    from src.validator.checks.correction import check_correction
    case=ROOT/"case_tests/e2e_tests/sm21_anchor"
    archived=case/"run_2026-07-02_sonnet_flow_e2e"
    run_dir=(out/"flow_sm21_run").resolve();run_dir.mkdir()
    view=provision_view_manifest(case,run_dir)
    manifest=ensure_run_manifest_v2(run_dir,view_manifest_sha256=view.content_sha256)
    provision_run_policy(run_dir,run_profile="exploratory",capability_profile="rectangular")
    runner=StageRunner(run_dir,manifest)
    for stage in ("0_reading","1_correction"):
        old=archived/stage/"attempts/001"
        data=json.loads((old/"output.json").read_bytes())
        checks=CheckReport.model_validate_json((old/"checks.json").read_bytes()) if stage=="0_reading" else check_correction(ensure_corrected_geometry(data))
        runner.record(stage=stage,stage_dir=run_dir/stage,output_obj=data,report=checks,
                      input_hashes={"archived_output":hash_file(old/"output.json")})
    manifest.save(run_dir)
    before={s:manifest.accepted(s).output_hash for s in ("0_reading","1_correction")}
    command=[sys.executable,"scripts/tool_scripts/run_stage.py","flow","sm21_anchor",str(run_dir),
             "--target","source-bim","--bim-out",str((out/"flow_sm21_bim").resolve()),"--judge","off"]
    result=subprocess.run(command,cwd=ROOT,capture_output=True,text=True)
    (out/"flow_stdout.txt").write_text(result.stdout)
    (out/"flow_stderr.txt").write_text(result.stderr)
    assert result.returncode==0, result.stderr+result.stdout
    from src.agent.execution.manifest import load_run_manifest
    after=load_run_manifest(run_dir)
    assert before=={s:after.accepted(s).output_hash for s in before}
    assert not any((run_dir/s).exists() for s in ("2_modelling","3_split_pairing","4_mep","5_intakeoutput"))
    assert not (run_dir/"_run/geometry_approval.json").exists()
    return {"argv":command,"returncode":result.returncode,"upstream_output_hashes_unchanged":before,
            "legacy_stages_created":False,"approval_created":False,"mode":"historical_upstream_replay"}


def run(out):
    out.mkdir(parents=True, exist_ok=False)
    reports, sections = {}, []
    for name,(case,relative,candidate,mode) in CASES.items():
        archived = ROOT/relative
        source_run = archived
        if name=="sm24_assisted":
            source_run=out/"inputs"/name
            correction=source_run/"1_correction/correction_geometry_snapped.json"
            correction.parent.mkdir(parents=True)
            shutil.copyfile(archived/"correction.json",correction)
        dest=out/name
        report=export_source_bim(source_run,dest,capability_profile="orthogonal_polygon",candidate_attempt=candidate)
        if report["status"]=="error":
            raise RuntimeError(f"{name}: {report['error']}")
        source=json.loads((dest/"source_model.json").read_bytes())
        # The original correction and independently verified GT are read only
        # on this evaluation side; generation above has no GT argument.
        input_path=source_run/next(k for k in report["input_artifacts"] if k.endswith(("output.json","snapped.json")))
        geom=ensure_corrected_geometry(json.loads(input_path.read_bytes()))
        evaluation=reference_partition(geom,load_gt_document(case),source_spaces=source["spaces"])
        # Evaluate the actual emitted source space list as well, rather than
        # assuming that a correct input proves an unchanged output.
        emitted=[{**s,"floor_id":evaluation["floor_mapping"][s["floor_id"]]} for s in source["spaces"]]
        comparison=compare_partitions(evaluation["reference_spaces"],emitted,tolerance_m=.02)
        assert comparison==evaluation["comparison"]
        evaluation["evaluated_source_model_sha256"]=source["source_model_sha256"]
        write_json(dest/"partition_evidence.json",evaluation)
        old_path=archived/("source_model.json" if name=="sm24_assisted" else "2_modelling/source_model.json")
        preserved=None
        if old_path.exists():
            old=json.loads(old_path.read_bytes())
            vertices=lambda model:{o["id"]:sorted(map(tuple,o["vertices"])) for o in model["openings"]}
            preserved=vertices(source)==vertices(old)
            assert preserved, f"{name}: opening vertices changed"
            assert source["spaces"]==old["spaces"]
        else:
            old_geometry=json.loads((archived/"2_modelling/building_geometry.json").read_bytes())
            # Old archives predate source IDs on windows; compare the full
            # multiset of vertices, separately from the source-ID unit tests.
            old_vertices=sorted(sorted(map(tuple,w["verts"])) for w in old_geometry["windows"])
            new_vertices=sorted(sorted(map(tuple,w["vertices"])) for w in source["openings"] if w["kind"]=="window")
            preserved=old_vertices==new_vertices
            assert preserved
        script_count=0
        for index,script in enumerate(re.findall(r"<script[^>]*>(.*?)</script>",(dest/"viewer.html").read_text(),re.S)):
            js=dest/f".script_{index}.js";js.write_text(script)
            try: subprocess.run(["node","--check",str(js)],check=True,capture_output=True)
            finally: js.unlink()
            script_count+=1
        report.update(mode=mode,original_input=relative,partition_status=evaluation["status"],
                      combined_quality_status="severe" if "severe" in (report["status"],evaluation["status"]) else "not_evaluated",
                      topology_findings=len(evaluation["topology_findings"]),
                      opening_vertices_preserved=preserved,viewer_scripts_checked=script_count)
        write_json(dest/"report.json",report)
        reports[name]=report
        figures="".join(overlay(evaluation["reference_spaces"],emitted,fid) for fid in sorted({s["floor_id"] for s in emitted}))
        sections.append(f'<section><h2>{name}</h2><p>{len(source["spaces"])} 个空间 / {len(source["boundaries"])} 个完整边界 / {len(source["openings"])} 个门窗开口。'
                        f'源几何检查：{source["validation"]["status"]}；独立分区评价：{evaluation["status"]}。</p>'
                        f'<a href="{name}/viewer.html">三维查看</a> · <a href="{name}/source_model.json">源 BIM</a> · '
                        f'<a href="{name}/report.json">质量与来源</a> · <a href="{name}/partition_evidence.json">独立分区证据</a>{figures}</section>')
    flow=exercise_source_flow(out)
    summary={"mode":"offline_source_bim_decoupling", "model_calls":0,"solver_calls":0,"source_flow":flow,
             "code_commit":subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
             "working_tree_diff_sha256":hashlib.sha256(subprocess.check_output(["git","diff","HEAD"],cwd=ROOT)).hexdigest(),
             "implementation_sha256": {path:hashlib.sha256((ROOT/path).read_bytes()).hexdigest() for path in (
                 "src/agent/geometry/source_bim.py", "src/agent/execution/source_bim.py",
                 "src/agent/geometry/source_model.py", "src/agent/geometry/build.py",
                 "src/agent/judge/partition_evidence.py", "scripts/tool_scripts/render_geometry_viewer.py",
                 "scripts/tool_scripts/run_stage.py", "scripts/tool_scripts/diagnose_source_bim.py")},
             "cases":reports,"not_evaluated":["new image reading","human confirmation","browser WebGL interaction","EP adapter"]}
    write_json(out/"report.json",summary)
    (out/"index.html").write_text('''<!doctype html><html lang="zh"><meta charset="utf-8"><title>独立源 BIM 生成</title>
<style>body{font:16px/1.75 system-ui;max-width:1100px;margin:32px auto;padding:0 20px;background:#f5f7fa;color:#183044}section{background:white;padding:24px;margin:24px 0}figure{display:inline-block;width:46%;margin:1%;vertical-align:top}svg{width:100%;max-height:500px}a{color:#1263ad}@media(max-width:650px){figure{width:98%}}</style>
<h1>独立源 BIM 生成</h1><p>源模型直接保存房间、完整边界、接触关系和门窗；本次生成不执行 EP 切配、物性装配或仿真。显示网格仅用于查看。</p>
<p>全部复用已有校正输入；sm24 辅助模型和 sm25 走廊决定保留人工参与标记。这不是原图冷启动。</p>
<p>sm24 的旧错分区仍作为反例，8 空间辅助模型也仍有独立分区差异；sm25 两组未建门继续阻塞。外边界参考面差异仍需核对，几何检查通过不等于原图完整性通过。</p>
<p><a href="flow_sm21_bim/viewer.html">实际 source-bim flow 输出</a>：复用已接受读图/校正，直接生成源 BIM；未进入旧 Stage 2–5，未创建人工确认。</p>
''' + "".join(sections) + '<a href="report.json">汇总与验证记录</a></html>')
    return {name:{k:r[k] for k in ("counts","status","partition_status","topology_findings","opening_vertices_preserved")} for name,r in reports.items()}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out",type=Path,required=True)
    print(json.dumps(run(parser.parse_args().out),ensure_ascii=False,indent=2))
