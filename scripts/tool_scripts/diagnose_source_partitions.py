"""Offline M0 source-partition replay. Never modifies archived runs or calls models.

Run from the repository: python scripts/tool_scripts/diagnose_source_partitions.py
    --out AI_agent/logs/experiments/<new-run>
The sm24 grouping fixture isolates a user-confirmed defect; it is assisted
regression evidence, not an independently reconstructed drawing/GT answer.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from shapely.ops import unary_union

from src.agent.correction.parse import ensure_corrected_geometry
from src.agent.correction.schema import CorrectedGeometry
from src.agent.geometry.modelling import _cell_polygon
from src.agent.geometry.source_model import _ring
from src.agent.judge.source_partition import compare_partitions
from src.agent.legacy.pipeline import materialize_kernel_geometry
from scripts.tool_scripts.render_geometry_viewer import build_viewer_html


CASES = {
    "sm21": "case_tests/e2e_tests/sm21_anchor/run_2026-07-02_sonnet_flow_e2e",
    "sm24": "case_tests/e2e_tests/sm24_anchor/run_2026-06-24_opus_reading",
    "sm25": "case_tests/e2e_tests/sm25-L_anchor/run_win_e2e",
}
GROUPS = ROOT / "tests/fixtures/sm24_source_partition_groups.json"


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def spaces(geom) -> list[dict]:
    return [{"id": cell.id, "floor_id": str(getattr(floor, "id", None) or floor.name),
             "polygon": _ring(_cell_polygon(cell)), "z_floor": float(floor.z_floor),
             "height": float(floor.ceiling_height), "role": cell.role}
            for floor in geom.floors for cell in floor.cells]


def sm24_partition_reference(geom, fixture: dict):
    """Apply only declared partition identities, preserving all archived shapes.

    Coordinates come from the archived correction. This controlled intervention
    isolates erroneous source subdivision, and cannot prove the other geometry.
    """
    payload = copy.deepcopy(geom.model_dump(mode="json"))
    payload["schema_version"] = "2"
    floor = payload["floors"][0]
    by_id = {cell.id: cell for cell in geom.floors[0].cells}
    id_map = {}
    for group in fixture["groups"]:
        members = group["members"]
        polygon = unary_union([_cell_polygon(by_id[cid]) for cid in members])
        if polygon.geom_type != "Polygon" or polygon.interiors:
            raise ValueError(f"declared sm24 group is not a single-ring room: {members}")
        x0, y0, x1, y1 = polygon.bounds
        floor["cells"] = [c for c in floor["cells"] if c["id"] not in members]
        floor["cells"].append({
            "id": group["source_id"], "role": by_id[members[0]].role,
            "x": [x0, x1], "y": [y0, y1], "polygon": _ring(polygon),
            "source_partition_members": members,
        })
        id_map.update({cid: group["source_id"] for cid in members})
    for window in payload["windows"]:
        window["room"] = id_map.get(window["room"], window["room"])
    payload["corrections"].append({
        "kind": "assisted_partition_regression", "groups": fixture["groups"],
        "reason": fixture["scope"], "source": str(GROUPS.relative_to(ROOT)),
    })
    return CorrectedGeometry.model_validate(payload), id_map


def extra_internal_walls(geometry: dict, id_map: dict) -> list[dict]:
    """Locate physical wall pairs that split one declared source room."""
    cells = {m["name"]: m["cell_id"] for m in geometry["zone_meta"]}
    faces = {s["name"]: s for s in geometry["surfaces"]}
    result = []
    for surface in faces.values():
        if surface["type"] != "Wall" or surface["obc"] != "Surface":
            continue
        partner = faces.get(surface["obc_obj"])
        if partner is None or surface["name"] >= partner["name"]:
            continue
        left, right = cells[surface["zone"]], cells[partner["zone"]]
        if left != right and id_map.get(left, left) == id_map.get(right, right):
            result.append({"surface_pair": [surface["name"], partner["name"]],
                           "source_space_id": id_map[left], "cell_ids": [left, right],
                           "vertices": surface["verts"], "severity": "severe"})
    return result


def plan_svg(reference: list[dict], walls: list[dict]) -> str:
    """Geometric overlay for inspecting the isolated defect; no raster generation."""
    all_points = [p for room in reference for p in room["polygon"]]
    x0, y0 = min(p[0] for p in all_points), min(p[1] for p in all_points)
    x1, y1 = max(p[0] for p in all_points), max(p[1] for p in all_points)
    def coords(points):
        return " ".join(f"{x-x0+0.5},{y1-y+0.5}" for x, y in points)
    shapes = []
    for room in reference:
        shapes.append(f'<polygon points="{coords(room["polygon"])}" fill="#d4e6f1" stroke="#284b63" stroke-width=".035"/>')
    for wall in walls:
        line = list(dict.fromkeys(tuple(v[:2]) for v in wall["vertices"]))
        shapes.append(f'<polyline points="{coords(line)}" fill="none" stroke="#c0392b" stroke-width=".12"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {x1-x0+1} {y1-y0+1}" '
            'style="max-height:560px;width:100%" role="img" aria-label="sm24 三道错误隔墙标为红色">'
            + "".join(shapes) + '</svg>')


def _viewer(out: Path, data: dict, title: str) -> None:
    roles = {m["name"]: m.get("role", "office") for m in data.get("zone_meta", [])}
    out.write_text(build_viewer_html(data, title=title, roles=roles), encoding="utf-8")


def run(out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=False)
    manifest = {
        "mode": "historical_replay_with_assisted_partition_fixture",
        "code_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "working_tree_diff_sha256": hashlib.sha256(subprocess.check_output(["git", "diff", "HEAD"], cwd=ROOT)).hexdigest(),
        "model_calls": 0, "inputs": [],
        "human_intervention": "Two declared sm24 source-space groups from the documented user-confirmed split defect; no new drawing interpretation.",
    }
    def read(path: Path):
        raw = path.read_bytes()
        manifest["inputs"].append({"path": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(raw).hexdigest()})
        return json.loads(raw)

    fixture = read(GROUPS)
    results = {}
    svg = ""
    for case, relative in CASES.items():
        old = ROOT / relative
        dest = out / case
        dest.mkdir()
        raw = read(old / "1_correction/correction_geometry_snapped.json")
        geom = ensure_corrected_geometry(raw)
        archived = read(old / "2_modelling/building_geometry.json")
        archived_gate = read(old / "2_modelling/kernel_gate_report.json")
        write_json(dest / "archived_geometry.json", archived)
        _viewer(dest / "archived_viewer.html", archived, f"{case} 历史产物 / 尚非当前重建")
        reference = []
        result = {
            "mode": "historical_replay", "input": relative,
            "source_space_count": sum(len(f.cells) for f in geom.floors),
            "window_count": len(geom.windows), "archived_gate": archived_gate,
            "archived_viewer": f"{case}/archived_viewer.html",
            "not_evaluated": ["原图冷启动", "门洞与连通", "完整源 BIM 正确性", "本批 EnergyPlus 运行", "人工确认"],
        }
        ep = old / "EP/EP_run/eplusout.end"
        result["historical_ep"] = ep.read_text().strip() if ep.exists() else "not_run"
        if ep.exists():
            manifest["inputs"].append({"path": str(ep.relative_to(ROOT)), "sha256": hashlib.sha256(ep.read_bytes()).hexdigest()})

        # Reuse accepted v3 proof loading. No retries, model calls or fallback.
        proof = None
        try:
            if str(geom.schema_version) == "3":
                from scripts.tool_scripts.run_stage import _load_snapped_with_proof
                accepted_geom, proof = _load_snapped_with_proof(old)
                if accepted_geom.model_dump(mode="json") != geom.model_dump(mode="json"):
                    raise ValueError("accepted correction differs from archived snapped input")
                for name, blob in [("accepted_output", proof.raw_output_bytes),
                                   ("resolver_inputs", proof.raw_resolver_inputs_bytes),
                                   ("window_hosts", proof.raw_window_hosts_bytes)]:
                    manifest["inputs"].append({"kind": name, "sha256": hashlib.sha256(blob).hexdigest()})
            stage = dest / "current_rebuild"
            stage.mkdir()
            bg, issues = materialize_kernel_geometry(
                geom, stage, capability_profile="orthogonal_polygon" if str(geom.schema_version) in {"2", "3"} else "rectangular",
                window_host_proof=proof,
            )
            result["current_rebuild"] = {"status": "failed" if bg is None else "blocked" if issues else "pass", "issues": issues}
            if bg is not None:
                data = json.loads((stage / "building_geometry.json").read_text())
                data["source_model"] = json.loads((stage / "source_model.json").read_text())
                result["source_mapping"] = data["source_model"]["validation"]
                _viewer(dest / "current_viewer.html", data, f"{case} 当前重放 / {result['current_rebuild']['status']}")
                result["current_viewer"] = f"{case}/current_viewer.html"
        except Exception as exc:
            result["current_rebuild"] = {"status": "failed", "issues": [f"{type(exc).__name__}: {exc}"]}

        if case == "sm24":
            # Bind the comparison to the actual correct-reading/wrong-correction case.
            read(old / "0_reading/1f_view.json")
            corrected, id_map = sm24_partition_reference(geom, fixture)
            reference = spaces(corrected)
            write_json(dest / "assisted_full_correction.json", corrected.model_dump(mode="json"))
            extra = extra_internal_walls(archived, id_map)
            result["extra_physical_wall_pairs"] = extra
            result["reference_basis"] = fixture
            svg = plan_svg(reference, extra)
            (dest / "partition_overlay.svg").write_text(svg, encoding="utf-8")
            # Only a room-partition preview: retain all source window records in
            # the full candidate above and explicitly report why they are absent.
            preview = corrected.model_copy(deep=True)
            preview.unsupported.append({"kind": "diagnostic_windows_not_realized", "window_ids": [w.id for w in preview.windows],
                                        "reason": "Partition-only diagnostic; current legacy seam-host failure remains unresolved."})
            preview.windows = []
            preview_dir = dest / "partition_preview"
            preview_dir.mkdir()
            write_json(preview_dir / "correction.json", preview.model_dump(mode="json"))
            bg, issues = materialize_kernel_geometry(preview, preview_dir, capability_profile="orthogonal_polygon")
            if bg is None:
                raise RuntimeError(f"sm24 partition-only rebuild failed: {issues}")
            data = json.loads((preview_dir / "building_geometry.json").read_text())
            data["source_model"] = json.loads((preview_dir / "source_model.json").read_text())
            _viewer(dest / "partition_preview.html", data, "sm24 人工分组诊断 / 8 源空间 / 11 窗未建模 / 门洞连通未评价")
            result["partition_preview"] = {
                "mode": "assisted_partition_only", "viewer": "sm24/partition_preview.html",
                "space_count": len(reference), "unrealized_window_ids": [w.id for w in corrected.windows],
                "source_mapping": data["source_model"]["validation"], "kernel_issues": issues,
                "partition_comparison": compare_partitions(reference, data["source_model"]["spaces"]),
            }
        result["partition_comparison"] = compare_partitions(reference, spaces(geom))
        write_json(dest / "report.json", result)
        results[case] = result

    report = {"manifest": manifest, "cases": results}
    write_json(out / "report.json", report)
    rows = []
    for case, result in results.items():
        links = [(result["archived_viewer"], "历史查看")]
        if result.get("current_viewer"):
            links.append((result["current_viewer"], "当前重放"))
        if result.get("partition_preview"):
            links.append((result["partition_preview"]["viewer"], "8 空间分区诊断（未建窗）"))
        link_html = " · ".join(f'<a href="{html.escape(url)}">{html.escape(label)}</a>' for url, label in links)
        rows.append(f'<tr><th>{case}</th><td>{result["source_space_count"]}</td><td>{result["window_count"]}</td>'
                    f'<td>{html.escape(result["partition_comparison"]["status"])}</td>'
                    f'<td>{html.escape(result["current_rebuild"]["status"])}</td><td>{link_html}</td></tr>')
    details = "".join(f'<details><summary>{case} 具体证据与未完成项</summary><pre>{html.escape(json.dumps(result, indent=2, ensure_ascii=False))}</pre></details>'
                      for case, result in results.items())
    page = '''<!doctype html><html lang="zh"><meta charset="utf-8"><title>M0 源分区诊断</title>
<style>body{font:16px/1.7 system-ui;max-width:1120px;margin:40px auto;padding:0 24px;color:#183044;background:#f6f8fa}
table{border-collapse:collapse;width:100%;background:white}td,th{padding:12px;border:1px solid #ccd6dd;text-align:left}a{color:#0768a0}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}details{margin:16px 0}section{background:white;padding:20px;border:1px solid #ccd6dd}h1{font-size:28px}</style>
<h1>M0 · 源分区诊断</h1><p>sm24 历史校正多出三道物理隔墙，分区评价为 severe。明确分组后的 8 空间诊断保留非矩形房间。</p>
<p>本次为旧产物重放和人工分组回归，模型调用 0 次。不是原图冷启动；门洞连通、完整 BIM 正确性和人工确认尚未完成。not_evaluated 不等于通过。</p>
<table><tr><th>案例</th><th>历史源空间</th><th>历史窗</th><th>分区评价</th><th>当前重建</th><th>查看</th></tr>'''+"".join(rows)+'''</table>
<section><h2>sm24 三道额外隔墙</h2><p>红线为同一声明源空间内的历史 Wall 配对；底图只隔离已确认的切割错误，其他几何与开放口未据此验收。</p>'''+svg+'</section>'+details+'<p><a href="report.json">完整 JSON 报告与输入哈希</a></p></html>'
    (out / "index.html").write_text(page, encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="new independent run directory; must not exist")
    args = parser.parse_args()
    report = run(args.out)
    print(json.dumps({case: {"partition": value["partition_comparison"]["status"],
                             "rebuild": value["current_rebuild"]["status"]}
                      for case, value in report["cases"].items()}, ensure_ascii=False, indent=2))
    print(args.out / "index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
