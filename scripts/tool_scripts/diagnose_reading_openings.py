"""Derive source doors from archived structured reading and wall decisions.

No drawing/model/solver calls; a new independent output directory is required.
Blocked candidates retain a viewer after full artifact and host verification.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.tool_scripts.diagnose_source_partitions import _viewer, write_json
from src.agent.correction.chain_provenance import (
    AsDrawnChainProvenanceV1, PlanWallOpeningPolicyV1, build_chain_provenance,
)
from src.agent.correction.finalize import finalize_as_drawn_chain_geometry
from src.agent.correction.parse import correction_target, ensure_corrected_geometry
from src.agent.correction.window_sources import (
    build_verified_window_inputs_as_drawn, verify_window_resolver_inputs_artifact,
)
from src.agent.execution.manifest import RunInputs, RunManifestV2, load_run_manifest, new_run_id
from src.agent.execution.stage_runner import StageRunner
from src.agent.output_coordinates import _verify_b5_bundle, load_verified_accepted_correction
from src.agent.legacy.pipeline import materialize_kernel_geometry
from src.validator.checks.correction import check_correction

DEFAULT_ARCHIVE = ROOT / "case_tests/e2e_tests/sm25-L_anchor/run_win_e2e"


def derive_candidate(old: Path, *, assumed_height_m: float = 2.1, rebuild_partitions: bool = False, connection_audit: dict | None = None,
                     wall_gap_decisions: tuple = ()):
    """Re-use verified historical inputs, deriving openings before finalization."""
    from src.agent.correction.as_drawn_openings import populate_as_drawn_openings

    old_manifest = load_run_manifest(old)
    verified = load_verified_accepted_correction(run_dir=old, manifest=old_manifest)
    original = ensure_corrected_geometry(json.loads(verified.raw_output_bytes))
    record = old_manifest.accepted("1_correction")
    attempt = old / "1_correction/attempts" / f"{record.accepted_attempt:03d}"
    old_marker = verify_window_resolver_inputs_artifact((attempt / "window_resolver_inputs.json").read_bytes())
    old_provenance = AsDrawnChainProvenanceV1.model_validate_json((attempt / "chain_provenance.json").read_bytes())
    producer = ensure_corrected_geometry(json.loads(old_marker.producer_draw_canonical_bytes))
    readings = dict(old_marker.raw_reading_artifacts)
    rows = [
        {"input_id": row.input_id, "product_filename": row.product_filename, "floor_ref": row.floor_ref,
         "compilation_bytes": row.compilation_bytes, "source_bytes_sha256": row.source_bytes_sha256}
        for row in old_provenance.floors
    ]
    endpoint_policy = "preserve_endpoint_connections_v1" if rebuild_partitions else old_provenance.endpoint_connection_policy
    if wall_gap_decisions and not rebuild_partitions:
        raise ValueError("wall gap decisions require source partition rebuild")
    if rebuild_partitions:
        from src.agent.correction.chain_replay import derive_as_drawn_chain_producer

        if connection_audit is not None:
            historical_audit = {}
            historical = derive_as_drawn_chain_producer(old_marker, old_provenance, audit=historical_audit)
            if historical.producer_draw_canonical_bytes != old_marker.producer_draw_canonical_bytes:
                raise RuntimeError("historical producer replay drifted")
            connection_audit["historical_recipe"] = historical_audit
        rebuilt_marker = derive_as_drawn_chain_producer(
            old_marker, build_chain_provenance(rows, endpoint_connection_policy=endpoint_policy,
                                              wall_gap_decisions=wall_gap_decisions),
            audit=connection_audit)
        producer = ensure_corrected_geometry(json.loads(rebuilt_marker.producer_draw_canonical_bytes))
    policy = PlanWallOpeningPolicyV1(assumed_height_m=assumed_height_m)
    producer, account = populate_as_drawn_openings(
        producer, raw_view_manifest_bytes=old_marker.raw_view_manifest_bytes,
        raw_reading_artifacts=readings,
        raw_wall_compilations={row.input_id: row.compilation_bytes for row in old_provenance.floors},
        assumed_height_m=policy.assumed_height_m,
        wall_gap_decisions=wall_gap_decisions,
    )
    marker = build_verified_window_inputs_as_drawn(
        producer_draw=producer, raw_view_manifest_bytes=old_marker.raw_view_manifest_bytes,
        raw_reading_artifacts=readings,
    )
    provenance = build_chain_provenance(
        rows, wall_opening_policy=policy, endpoint_connection_policy=endpoint_policy,
        wall_gap_decisions=wall_gap_decisions)
    result = finalize_as_drawn_chain_geometry(
        producer, verified_window_inputs=marker, target=correction_target("orthogonal_polygon"),
        chain_provenance=provenance,
    )
    return result, account, original, attempt


def record_candidate(out: Path, result):
    """Use the real writer and preserve a blocked status; never grant approval."""
    marker = result.verified_window_resolver_inputs
    (out / "0_reading").mkdir(parents=True)
    (out / "_run").mkdir()
    (out / "_run/view_manifest.json").write_bytes(marker.raw_view_manifest_bytes)
    for identity in marker.inputs.reading_artifacts:
        raw = dict(marker.raw_reading_artifacts)[identity.input_id]
        (out / "0_reading" / f"{identity.expected_output_id}.json").write_bytes(raw)
    manifest = RunManifestV2(case="structured-opening-replay", run_id=new_run_id(),
                            run_inputs=RunInputs(view_manifest_sha256=marker.inputs.view_manifest.content_sha256))
    checks = check_correction(
        result.geom, window_host_proof=result.window_host_claims, window_evidence=result.window_evidence_ledger,
        verified_window_inputs=marker, capability_profile="orthogonal_polygon", run_profile="exploratory",
    )
    record = StageRunner(out, manifest).record(stage="1_correction", stage_dir=out / "1_correction",
                                             output_obj=result, report=checks)
    manifest.save(out)
    attempt = out / "1_correction/attempts" / f"{record.attempt_index:03d}"
    return record, checks, attempt


def run(out: Path, old: Path = DEFAULT_ARCHIVE, *, rebuild_partitions: bool = False, wall_gap_decisions: tuple = ()) -> dict:
    out.mkdir(parents=True, exist_ok=False)
    connection_audit = {} if rebuild_partitions else None
    result, account, original, archived_attempt = derive_candidate(
        old, rebuild_partitions=rebuild_partitions, connection_audit=connection_audit,
        wall_gap_decisions=wall_gap_decisions)
    if connection_audit is not None:
        write_json(out / "endpoint_connections.json", connection_audit)
    payload = account.to_payload()
    accounted = [(row["input_id"], oid) for row in payload["built"] + payload["unbuilt"] + payload["reclassified"]
                 for oid in row["observation_ids"]]
    if len(accounted) != payload["observations_considered"] or len(set(accounted)) != len(accounted):
        raise RuntimeError("plan opening observations were omitted or counted twice")
    write_json(out / "opening_account.json", payload)
    record, checks, attempt = record_candidate(out, result)
    # Reverify the entire B5 candidate bundle for viewing, without fabricating
    # an accepted correction reference when its completeness check is blocked.
    proof = _verify_b5_bundle(
        raw_output_bytes=(attempt / "output.json").read_bytes(),
        raw_feature_states_bytes=(attempt / "feature_states.json").read_bytes(),
        raw_window_resolver_inputs_bytes=(attempt / "window_resolver_inputs.json").read_bytes(),
        raw_window_hosts_bytes=(attempt / "window_hosts.json").read_bytes(),
    )
    stage = out / "2_modelling"
    stage.mkdir()
    bg, issues = materialize_kernel_geometry(result.geom, stage, capability_profile="orthogonal_polygon",
                                             window_host_proof=proof)
    if bg is None:
        raise RuntimeError(f"source opening build failed: {issues}")
    data = json.loads((stage / "building_geometry.json").read_text())
    source = json.loads((stage / "source_model.json").read_text())
    data["source_model"] = source
    _viewer(out / "viewer.html", data, f"已有读图记录自动接门洞：{len(source['spaces'])} 空间、{len(bg.windows)} 窗、{len(result.geom.openings)} 门/通道；部分问题待处理")
    unchanged_rooms = [f.model_dump(mode="json") for f in original.floors] == [f.model_dump(mode="json") for f in result.geom.floors]
    # Proof hashes change with the new producer; compare window geometry and
    # geometry instead of mistaking changed proof hashes or room numbering for a move.
    def window_shape(geom):
        return sorted((w.id, w.floor_id, w.facade, tuple(w.span), tuple(w.z)) for w in geom.windows)
    unchanged_windows = window_shape(original) == window_shape(result.geom)
    unchanged_window_rooms = sorted((w.id, w.room) for w in original.windows) == sorted((w.id, w.room) for w in result.geom.windows)
    if not rebuild_partitions and not unchanged_window_rooms:
        raise RuntimeError("door-only derivation unexpectedly changed window ownership")
    if (not rebuild_partitions and not unchanged_rooms) or not unchanged_windows:
        raise RuntimeError("door derivation unexpectedly changed rooms or windows")
    if payload["unbuilt"] and record.accepted:
        raise RuntimeError("known unbuilt doors were accepted as a complete correction")
    report = {
        "manifest": {
            "mode": "assisted_wall_gap_review_replay" if wall_gap_decisions else "historical_structured_reading_and_wall_decision_replay",
            "code_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "working_tree_diff_sha256": hashlib.sha256(subprocess.check_output(["git", "diff", "HEAD"], cwd=ROOT)).hexdigest(),
            "archive": str(old.relative_to(ROOT)),
            "inputs": [{"path": str((archived_attempt / name).relative_to(ROOT)),
                        "sha256": hashlib.sha256((archived_attempt / name).read_bytes()).hexdigest()}
                       for name in ("output.json", "window_resolver_inputs.json", "chain_provenance.json")],
            "model_calls": 0, "solver_calls": 0, "manual_door_positions_or_room_groups": 0,
            "assumed_height_m": 2.1,
            "endpoint_connection_policy": result.chain_provenance.endpoint_connection_policy,
            "wall_gap_reviews": [d.model_dump(mode="json") for d in wall_gap_decisions],
        },
        "opening_account": payload,
        "correction": {"accepted": record.accepted, "checks": checks.model_dump(mode="json"),
                       "independent_replay_verification": "pass"},
        "building": {"spaces": len(source["spaces"]), "surfaces": len(bg.surfaces), "windows": len(bg.windows),
                     "source_openings": len(result.geom.openings), "derived_openings": len(bg.openings),
                     "rooms_unchanged": unchanged_rooms, "windows_unchanged": unchanged_windows,
                     "window_room_ids_unchanged": unchanged_window_rooms,
                     "source_mapping": source["validation"], "kernel_issues": issues,
                     "connections": source["connections"]},
        "not_evaluated": ["new image reading", "whole-drawing partition/opening completeness",
                          "actual door/passage heights and door operating states",
                          "EnergyPlus opening adapter/run", "human approval and browser screenshot inspection"],
    }
    if rebuild_partitions:
        from shapely.geometry import Polygon
        report["partition_changes"] = [
            {"floor_id": old_floor.id, "old_spaces": len(old_floor.cells), "new_spaces": len(new_floor.cells),
             "overlaps": [{"old_id": oc.id, "new_id": nc.id,
                           "area_m2": Polygon(oc.polygon).intersection(Polygon(nc.polygon)).area}
                          for oc in old_floor.cells for nc in new_floor.cells
                          if Polygon(oc.polygon).intersection(Polygon(nc.polygon)).area > 0]}
            for old_floor, new_floor in zip(original.floors, result.geom.floors)
        ]
    write_json(out / "report.json", report)
    built = payload["built_count"]
    pending = len(payload["unbuilt"])
    reasons = {"final_wall_host_not_unique": "找不到唯一、完整承载这处开口的墙面",
               "opening_crosses_room_boundaries": "开口范围跨到了多个房间，暂不能确定两侧关系"}
    rows = "".join(f"<li>第 {i} 组：{html.escape(reasons.get(row['reason'], '位置或来源仍需核对'))}。</li>"
                   for i, row in enumerate(payload["unbuilt"], 1))
    completeness = (f"还有 {pending} 组明确开口记录未能建入，因此校正结果未获完整通过。模型仍可查看；未建项目保留如下。"
                    if pending else "已有明确门/通道记录均已接入；这不证明原图的所有开口都已读出。")
    change_description = (
        f"保留尺寸校正前已有的墙端连接后，空间由 {sum(len(f.cells) for f in original.floors)} 个变为 {len(source['spaces'])} 个；{len(bg.windows)} 扇窗的位置和尺寸保留。新增分区来自已有墙线，整图正确性仍待独立评价。"
        if rebuild_partitions else
        f"原有 {len(source['spaces'])} 个空间、{len(bg.windows)} 扇窗均保留，房间形状和窗的位置、尺寸未改。"
    )
    reading_description = "本次没有重新看原图或调用模型；仍沿用历史读图判断和墙的处理结果。"
    if wall_gap_decisions:
        change_description = (f"根据带来源记录的 {len(wall_gap_decisions)} 处连续空间决定重建，得到 {len(source['spaces'])} 个空间；"
                              f"{len(bg.windows)} 扇窗的位置和尺寸保留。缺口判读为辅助输入，原始读图未改。")
        reading_description = (f"本次复用了原始平面图的辅助核对决定，没有重新运行读图模型；"
                               f"{len(payload['reclassified'])} 条旧门/通道记录已明确改判为连续空间，原分类与理由保存在台账。")
    page = f'''<!doctype html><html lang="zh"><meta charset="utf-8"><title>从已有读图记录自动接入门洞</title>
<style>body{{font:16px/1.8 system-ui;max-width:980px;margin:40px auto;padding:0 24px;color:#183044;background:#f6f8fa}}
section{{background:white;padding:20px;margin:20px 0}}a{{color:#0768a0}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}li{{overflow-wrap:anywhere}}</style>
<h1>从已有读图记录自动接入门洞</h1>
<p>这次自动接入了 {built} 处门/通道。{change_description}</p>
<p><a href="viewer.html">打开建筑模型</a> · <a href="opening_account.json">查看逐项门洞记录</a></p>
<section><h2>减少了什么人工操作</h2><p>直接读取已经保存的门/通道类型和位置，借用已确定的墙找到两侧房间。同一扇门在墙两面留下的记录会归并，不再逐门手工填写房间和坐标。</p>
<p>{reading_description}门高/通道高暂按 2.1 米并逐项记为假设；门的实际开闭状态未知。</p></section>
<section><h2>仍未完成</h2><p>{completeness}</p><ul>{rows}</ul>
<p>原有短边检查问题继续保留，新增开口的仿真出口尚未支持。图纸整体是否读全、房间分区是否全对、正式人工确认和浏览器截图均未在本次验收。</p></section>
<details><summary>完整报告与来源</summary><pre>{html.escape(json.dumps(report, ensure_ascii=False, indent=2))}</pre></details>
<p><a href="report.json">下载报告</a></p></html>'''
    (out / "index.html").write_text(page, encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--rebuild-partitions", action="store_true", help="Rebuild wall partitions with preserved endpoint connections")
    parser.add_argument("--wall-gap-decisions", type=Path, help="Explicit source-bound continuous-space reviews; requires --rebuild-partitions")
    args = parser.parse_args()
    from src.agent.correction.wall_gap_review import load_wall_gap_decisions
    decisions = load_wall_gap_decisions(args.wall_gap_decisions, image_root=ROOT) if args.wall_gap_decisions else ()
    report = run(args.out.resolve(), rebuild_partitions=args.rebuild_partitions, wall_gap_decisions=decisions)
    print(json.dumps({"built": report["opening_account"]["built_count"],
                      "unbuilt_groups": len(report["opening_account"]["unbuilt"]),
                      "correction_accepted": report["correction"]["accepted"],
                      "kernel_issues": report["building"]["kernel_issues"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
