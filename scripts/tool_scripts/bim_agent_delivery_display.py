"""Render the saved delivery report with public names; no review state changes."""
from __future__ import annotations

import html
import json

from src.agent.geometry.source_naming import (
    public_names_for_display, public_reference_map, public_reference_text,
)


def render_regularization_html(source: dict, audit: dict | None = None) -> str:
    """Present geometric changes with current public names and optional raw trace."""
    from scripts.tool_scripts.bim_agent_regularization import source_reports, report_sections

    reports = audit.get("reports", []) if audit else source_reports(source)
    if not reports:
        return ""
    names = public_names_for_display(source)
    references = public_reference_map(source, names)
    spaces = {row["id"]: row for row in source.get("spaces", [])}

    def escaped(value):
        return html.escape(public_reference_text(value, references), quote=True)

    def number(value):
        return f"{value:.4f}".rstrip("0").rstrip(".") if isinstance(value, (int, float)) else "—"

    def object_name(row):
        floor = row.get("floor_id")
        floor_name = names.get("floors", {}).get(floor, "楼层")
        axis, coordinate = row.get("axis"), row.get("to_m")
        visible = []
        object_ids = row.get("object_ids", {})
        for opening in object_ids.get("openings", row.get("opening_ids", [])):
            for identity in (opening, f"{floor}:{opening}"):
                if identity in names.get("openings", {}):
                    visible.append(names["openings"][identity])
                    break
        if axis in {"x", "y"} and isinstance(coordinate, (int, float)):
            index = 0 if axis == "x" else 1
            span = row.get("span_m")
            for boundary in source.get("boundaries", []):
                space = spaces.get(boundary.get("space_id"), {})
                if floor and space.get("floor_id") != floor:
                    continue
                points = boundary.get("vertices", [])
                if boundary.get("geometry_type") != "wall" or not points:
                    continue
                if not all(abs(point[index] - coordinate) < 1e-5 for point in points):
                    continue
                if isinstance(span, (list, tuple)) and len(span) == 2:
                    lo, hi = min(point[1 - index] for point in points), max(point[1 - index] for point in points)
                    if min(hi, max(span)) - max(lo, min(span)) <= 1e-7:
                        continue
                visible.append(names.get("boundaries", {}).get(boundary["id"], floor_name + "墙面"))
        if visible:
            return "、".join(dict.fromkeys(visible))
        if axis == "z":
            return floor_name + " · 楼面标高"
        return floor_name + " · " + ({"x": "南北向墙线", "y": "东西向墙线"}.get(axis, "平面对象"))

    rows, rejected, skipped, unmatched, reading_rows = [], [], [], [], []
    for report in (section for top in reports for section in report_sections(top)):
        for row in report.get("changes", []):
            distance = row.get("movement_m")
            label = object_name(row)
            if row.get("type") == "remove_narrow_strip_space_seeds":
                label += f" · 删除 {len(row.get('removed_space_seeds', []))} 个窄条空间标记"
            elif row.get("type") == "merge_overlapping_openings":
                label += f" · 合并 {len(row.get('removed_opening_ids', []))} 个重复门窗"
            elif row.get("type") == "move_footprint_edge":
                label += " · 跨层外轮廓边对齐"
            rows.append("<tr>" + "".join(f"<td>{escaped(value)}</td>" for value in (
                label, number(row.get("from_m")) + " → " + number(row.get("to_m")),
                number(abs(distance) * 100) if isinstance(distance, (int, float)) else "—",
                row.get("reason") or row.get("basis", "规整规则"))) + "</tr>")
        rejected.extend(report.get("rejections", []))
        skipped.extend(report.get("skipped_alignments", []))
        alignment = report.get("reading_alignment", {})
        for kind, stage in alignment.items() if isinstance(alignment, dict) else []:
            if isinstance(stage, dict):
                for item in stage.get("items", []):
                    action = item.get("action")
                    if action in {"not_moved", "checked_not_applied", "rejected"}:
                        unmatched.append({"楼层": names.get("floors", {}).get(report.get("floor_id"), "楼层"),
                                          "原因": item.get("reason", "未找到可靠对应")})
                        continue
                    floor = names.get("floors", {}).get(report.get("floor_id"), "楼层")
                    label = {"partition": "内墙", "footprint": "外轮廓", "opening": "门窗"}.get(item.get("kind"), "尺寸基准")
                    identity = str(item.get("object", "")).removeprefix("opening:")
                    for key in (identity, f"{report.get('floor_id')}:{identity}"):
                        if key in names.get("openings", {}):
                            label = names["openings"][key]
                            break
                    reading_rows.append("<tr>" + "".join(f"<td>{escaped(value)}</td>" for value in (
                        floor + " · " + label, "墨线" if kind == "ink" else "尺寸标注",
                        number(item.get("movement_m") * 100) if isinstance(item.get("movement_m"), (int, float)) else "见完整记录",
                        item.get("convention", action or "核对"))) + "</tr>")
                rejected.extend(stage.get("rejections", []))
    skipped_notes = []
    for row in skipped:
        endpoints = []
        for endpoint in (row.get("source"), row.get("target")):
            if not isinstance(endpoint, dict):
                continue
            floor = names.get("floors", {}).get(endpoint.get("floor_id"), "楼层")
            axis = {"x": "南北向墙线", "y": "东西向墙线"}.get(
                endpoint.get("axis"), "平面对象")
            endpoints.append(
                f'{floor} · {axis} {number(endpoint.get("coordinate_m"))} 米')
        distance = row.get("distance_m")
        skipped_notes.append(
            " ↔ ".join(endpoints)
            + (f'；偏差 {number(abs(distance) * 100)} 厘米'
               if isinstance(distance, (int, float)) else "")
            + "；自动对齐未安全完成，保留原值并随交付说明。"
        )
    return (
        '<section class="regularization-audit"><h2>几何规整清单</h2>'
        '<p>以下记录工具对原读数的调整；读图是否准确仍需对照原图。</p>'
        '<table><tr><th>对象</th><th>从 → 到（米）</th><th>移动（厘米）</th><th>依据</th></tr>'
        + ("".join(rows) or '<tr><td colspan="4">本稿没有几何移动。</td></tr>') + '</table>'
        + ('<h3>读图坐标调整</h3><table><tr><th>对象</th><th>依据</th><th>移动（厘米）</th><th>采用方式</th></tr>'
           + ''.join(reading_rows) + '</table>' if reading_rows else '')
        + ('<p>未采用或需返工：' + escaped(rejected) + '</p>' if rejected else '')
        + ('<h3>未对齐并保留原值</h3><ul>'
           + ''.join(f'<li>{escaped(note)}</li>' for note in skipped_notes)
           + '</ul>' if skipped_notes else '')
        + ('<p>未采用的读数调整，保持原值：' + escaped(unmatched) + '</p>' if unmatched else '')
        + '<details><summary>完整规整记录与内部编号</summary><pre style="white-space:pre-wrap;overflow-wrap:anywhere">'
        + html.escape(json.dumps(reports, ensure_ascii=False, indent=2)) + '</pre></details></section>'
    )


def render_delivery_html(result: dict, source: dict) -> str:
    candidate = result["candidate"]
    selection_origin = result["selection_origin"]
    current_claims = result["current_claim_state"]
    names = public_names_for_display(source)
    references = public_reference_map(source, names)

    def text_html(value):
        return html.escape(public_reference_text(value, references))

    def name_html(kind, identity):
        return html.escape(names.get(kind, {}).get(identity, str(identity)))

    # A separate handoff preserves the immutable candidate's original report.
    statuses = {"not_reviewed":"未回查", "partial":"仅有局部回查",
                "consistent_with_supplied_observations":"与所报观察一致",
                "observations_require_follow_up":"仍需跟进"}
    kinds = {"door":"门洞", "window":"窗", "passage":"空通道"}
    scopes = result["opening_review_scopes"]
    rows = "".join(
        f'<tr><td>{name_html("floors", s["floor_id"])}</td><td>{kinds[s["kind"]]}</td>'
        f'<td>{s["built_count"]}</td><td>{statuses[s["review_status"]]}</td></tr>'
        for s in scopes)
    facades = {"North":"北", "South":"南", "East":"东", "West":"西"}
    facade_scopes = result.get("facade_review_scopes", [])
    facade_rows = "".join(
        f'<tr><td>{name_html("floors", s["floor_id"])}</td><td>{facades[s["facade"]]}</td>'
        f'<td>{kinds[s["kind"]]}</td><td>{s["built_count"]}</td>'
        f'<td>{statuses[s["review_status"]]}</td></tr>' for s in facade_scopes)
    facade_table = (
        '<details><summary>逐立面回查范围</summary>'
        '<p>零个已建开口也需明确观察；内部及无法确定方向的开口不能靠立面回查覆盖。'
        '与所报观察一致仍不代表原图保真。</p><table>'
        '<tr><th>楼层</th><th>立面</th><th>类别</th><th>已建数量</th><th>回查状态</th></tr>'
        f'{facade_rows}</table></details>' if facade_rows else '')
    count_report = result['facade_counts']
    count_labels = {'not_counted': '尚未清点', 'conflicting_observations': '清点记录冲突',
                    'matches_observed_total': '与所报数量一致', 'count_mismatch': '数量不符'}
    count_rows = ''.join(
        f'<tr><td>{name_html("floors", scope["floor_id"])}</td><td>{facades[scope["facade"]]}</td>'
        f'<td>{kinds[kind]}</td><td>{scope[kind]["built_count"]}</td>'
        f'<td>{text_html(str(scope[kind]["observed_counts"]))}</td>'
        f'<td>{count_labels[scope[kind]["status"]]}</td></tr>'
        for scope in count_report.get('scopes', []) for kind in ('window', 'door'))
    facade_table += ('<h2>逐层逐面外墙门窗清点</h2><p>数量来自所报原图观察；相等不证明位置正确。'
        '未清点、数量不符和冲突均只报告，不阻止交付。</p><table>'
        '<tr><th>楼层</th><th>立面</th><th>类别</th><th>已建</th><th>所报原图数量</th><th>结果</th></tr>'
        + count_rows + '</table>')
    if count_report.get('status') == 'unavailable':
        facade_table += '<p>清点报告暂不可用：' + text_html(count_report['reason']) + '</p>'
    if count_report.get('unused_observations') or count_report.get('unsupported_exterior_boundaries'):
        facade_table += '<p>部分观察无法绑定当前楼层/立面或原图已变化，或外墙方向未确定；详见 delivery.json 的 facade_counts。</p>'
    heights = result['height_coverage']
    height_labels = {'located_applied': '有定位依据，已应用', 'located_confirmed': '有定位依据，已确认',
                     'assumed': '假设／声明', 'missing': '缺少逐扇定位依据'}
    height_rows = ''.join(
        f'<tr><td>{name_html("openings", row["opening_id"])}</td><td>{", ".join(name_html("floors", fid) for fid in row["floor_ids"])}</td>'
        f'<td>{text_html(str(row["facade"]))}</td><td>{text_html(str(row["absolute_z_m"]))}</td>'
        f'<td>{height_labels[row["status"]]}</td>'
        f'<td>{text_html("; ".join(e["claim_id"] + ": " + ", ".join(v["image"] for v in e["views"]) for e in row["evidence"]))}</td>'
        f'<td>{text_html(", ".join(row["issues"]))}</td></tr>'
        for row in heights.get('openings', []))
    height_table = ('<h2>逐开口高度表</h2><p>一行对应一扇外部开口。状态说明数值应用和来源定位，'
        '不证明读图正确；同一定位框包含多扇时需逐扇确认。假设、缺项与待核原因不阻止交付。</p>'
        '<table><tr><th>开口</th><th>楼层</th><th>立面</th><th>绝对高度（米）</th>'
        '<th>状态</th><th>依据与视图</th><th>待核原因</th></tr>' + height_rows + '</table>')
    if heights.get('status') == 'unavailable':
        height_table += '<p>高度表暂不可用：' + text_html(heights['reason']) + '</p>'
    view_labels = {"full_view_returned": "已返回整图", "crop_only_returned": "仅返回局部",
                   "no_direct_view_record": "无直接看图记录"}
    input_rows = ''.join(
        f'<tr><td>{text_html(row["image"])}</td><td>{view_labels[row["status"]]}</td></tr>'
        for row in result['input_view_status']['images'])
    input_table = ('<h2>本次原图直接查看记录</h2><p>仅统计本次直接看图工具的返回；'
        '其他图像工具、局部模型和前次运行不计入。返回整图不等于已检查全图或读图正确，'
        '也不等于高度已落实到模型。</p><table><tr><th>原图</th><th>返回范围</th></tr>'
        f'{input_rows}</table>' if input_rows else '')
    notes = "".join(f'<li>{text_html(s)}</li>' for s in result["generation"]["unresolved"])
    notes += "".join(f'<li>{text_html(row["id"])}：{text_html(note)}</li>'
                     for row in current_claims["claims"] if row["state"] != "retracted"
                     for note in row["unresolved"])
    notes += "".join(f'<li>继承观察（尚未复核）：{text_html(note)}</li>'
                     for row in current_claims["inherited_observations"] for note in row["unresolved"])
    claim_labels = {"applied_current": "已应用，当前仍保留", "confirmed_unchanged": "已核对，无需修改",
        "pending_application": "已采纳，尚未关联执行或确认", "partially_satisfied": "仅部分值已落实",
        "changed_since_check": "检查后对象已改变，需复核", "deferred": "暂缓",
        "retracted": "已撤回", "undecided": "待判断"}
    claim_rows = "".join(f'<tr><td>{text_html(row["id"])}</td><td>{claim_labels[row["state"]]}</td>'
                         f'<td>{text_html(row["reason"])}</td></tr>' for row in current_claims["claims"])
    superseded = "".join(f'<li>{text_html(row["before"])} → {text_html("；".join(row["after"]) or "已撤销")}；'
                         f'{text_html(row["reason"])}</li>' for row in current_claims["superseded_notes"])
    assumptions = "".join(f'<li>{text_html(s)}</li>' for s in result["assumptions"])
    counts = result["counts"]
    geometry_status = {"pass":"通过", "warning":"有警告", "severe":"有严重问题"}.get(
        (result.get("source_validation") or {}).get("status"), "未评价")
    deterministic_replay = selection_origin == "developer_selected_deterministic_trace_replay"
    selected = ("开发侧选定的局部轮廓确定性应用" if deterministic_replay else
                "模型选定" if selection_origin == "agent_selected" else
                "系统保留的最新候选，模型未显式选定")
    run_status = result["generation_status"]
    run_note = {"completed":"本次模型调用正常结束。", "in_progress":"模型调用尚未结束。",
                "interrupted":"本次模型调用未正常完成，以下保留已生成候选。"}[run_status["state"]]
    if run_status.get("handoff_saved"):
        run_note = "已选定并保存 BIM，未完成事项见下方。"
    if deterministic_replay:
        run_note = "本次由代码应用已保存的局部观察，未调用模型生成整案；不代表自主冷启动完成。"
    if run_status.get("error"):
        run_note += " " + text_html(run_status["error"])
    floor_status = result["floor_completeness"]
    mesh_review = result.get('mesh_review')
    if mesh_review is not None:
        if mesh_review.get('complete_building'):
            run_note += " 调度员已声明本稿覆盖整栋建筑，声明与当前模型及回叠证据一致；这不代表独立精度验收通过。"
        else:
            run_note += " 本稿尚无有效的整栋范围声明，保留为局部或待核查交付。"
        if mesh_review.get('summary'):
            run_note += " " + text_html(mesh_review['summary'])
    elif floor_status["complete_building"] is False:
        run_note += " 本稿不完整：尚缺楼层图 " + text_html(", ".join(floor_status["missing_candidate_images"])) + "。"
    elif floor_status["complete_building"] is True:
        run_note += " 已覆盖预期楼层图；这不证明房间、门窗或高度正确。"
    else:
        run_note += " 未声明可核对的楼层图范围，全楼完整性未判定。"
    feedback = result["source_image_feedback"]
    wall_report = result.get("wall_dimension_report") or {}
    wall_findings = wall_report.get("findings", [])
    wall_rows = "".join(
        f'<tr><td>{text_html(d["id"])}</td><td>{d["raw_length_m"]}</td>'
        f'<td>{d["representative_length_m"]}</td><td>{d["residual_m"]}</td></tr>'
        for d in wall_report.get("dimensions", []))
    finding_rows = "".join(f'<li>{text_html(f["dimension_id"])}：{text_html(f["message"])}</li>'
                           for f in wall_findings)
    calibration_rows = "".join(
        f'<li>{text_html(row["image"])} / {name_html("floors", row["floor_id"])}：'
        f'{text_html(str(warning.get("guidance", warning.get("type"))))}</li>'
        for row in feedback["current_source_projections"] for warning in row.get("calibration_warnings", []))
    host_rows = "".join(
        f'<tr><td>{text_html(row["image"])} / {name_html("floors", row["floor_id"])}</td>'
        f'<td>{text_html(point["marker"])} · {text_html(point["dimension_id"])}</td>'
        f'<td>{", ".join(name_html("boundaries", bid) for bid in point["boundary_ids"])}</td>'
        f'<td>{point["tangential_outside_distance_m"]}</td></tr>'
        for row in feedback["current_source_projections"]
        for point in (row.get("wall_evidence_projection") or {}).get("endpoints", []))
    host_html = (
        '<details><summary>尺寸证据与所引用墙段的位置对照</summary>'
        '<p>图中 W 为已引用墙段，D 的 S/E 为原记录起点/终点。下表距离表示证据点沿墙方向'
        '超出该墙段范围的长度，依赖本图标定；0 不证明归属正确，尺寸引出线也可能合理地落在墙段外。</p>'
        '<table><tr><th>原图 / 楼层</th><th>证据点</th><th>所引用墙段</th>'
        f'<th>沿墙超出范围（米）</th></tr>{host_rows}</table></details>' if host_rows else '')
    evidence_html = (
        '<h2>尺寸与标定的实际反馈</h2><p>以下为工具计算，原始数值及未处理问题不会被模型总结覆盖。'
        '尺寸残差不自动等于建模错误；同墙侧面次序冲突应先核对端点。单位：米。</p>'
        '<table><tr><th>尺寸</th><th>原标注</th><th>换算后代表面距</th><th>模型减换算值</th></tr>'
        f'{wall_rows}</table><ul>{finding_rows}{calibration_rows}</ul>{host_html}'
        if wall_rows or calibration_rows else '')
    current_projection_rows = "".join(
        f'<li>{text_html(row["image"])} / {name_html("floors", row["floor_id"])}：'
        f'<a href="{text_html(row["overlay_image"])}">当前源回叠图</a></li>'
        for row in feedback["current_source_projections"])
    old_projection_rows = "".join(
        f'<li>{text_html(row["image"])} / {name_html("floors", row["floor_id"])}：'
        f'{text_html(row["source_model_sha256"][:12])}</li>'
        for row in feedback["old_source_projections"])
    uncovered_rows = "".join(
        f'<li>{text_html(row["image"])} / {name_html("floors", row["floor_id"])}（当前源无该楼层）</li>'
        for row in feedback["registered_calibration_uncovered_floors"])
    unregistered_floor_rows = "".join(
        f'<li>{name_html("floors", floor_id)}（当前源没有任何登记图面）</li>'
        for floor_id in feedback["floors_without_registered_views"])
    projection_error_rows = "".join(
        f'<li>{text_html(str(row.get("image")))} / {name_html("floors", row.get("floor_id"))}：'
        f'{text_html(row["error"])}</li>' for row in feedback["projection_errors"])
    missing_or_failed_rows = uncovered_rows + projection_error_rows
    feedback_html = (
        '<h2>原图回叠反馈</h2><p>标定由模型提供，尚未独立验证；图像已生成不代表已审视或原图保真。</p>'
        f'<p>当前源投影 {len(feedback["current_source_projections"])} 份；旧源投影 '
        f'{len(feedback["old_source_projections"])} 份；登记但当前楼层未覆盖 '
        f'{len(feedback["registered_calibration_uncovered_floors"])} 份；当前源无登记图面楼层 '
        f'{len(feedback["floors_without_registered_views"])} 个。</p>'
        f'<ul>{current_projection_rows or "<li>当前源没有成功的回叠图。</li>"}</ul>'
        f'<details><summary>旧源投影</summary><ul>{old_projection_rows or "<li>无</li>"}</ul></details>'
        f'<details><summary>未覆盖或失败</summary><ul>{missing_or_failed_rows or "<li>无</li>"}'
        f'{unregistered_floor_rows}</ul></details>')
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<title>BIM 候选与实际检查</title><style>body{font:16px system-ui;'
        'max-width:1100px;margin:30px auto;padding:0 16px;line-height:1.6}'
        'table{border-collapse:collapse;width:100%}td,th{padding:8px;border:1px solid #ccc;text-align:left}'
        'iframe{width:100%;height:680px;border:1px solid #ccc}</style>'
        '<h1>BIM 候选与实际检查</h1><p>以下状态来自保存的源模型和回查记录。'
        '几何自洽或观察对应不等于原图保真；未核查和待处理问题见下方记录。</p>'
        f'<p>{run_note}</p>'
        f'<p>{selected}：{text_html(candidate)}。{counts["spaces"]} 个空间，'
        f'{counts["openings"]} 个已建开口，{counts["unbuilt_openings"]} 个未建开口。'
        f'几何自洽：{geometry_status}；原图保真：未评价。</p>'
        f'<p><a href="{result["viewer"]}">打开模型</a>'
        f' · <a href="{result["source_model"]}">源 BIM</a> · '
        '<a href="delivery.json">检查记录</a></p>'
        '<table><tr><th>楼层</th><th>类别</th><th>已建数量</th><th>原图观察回查</th></tr>'
        f'{rows}</table><p>{len(result["stale_reviews"])} 份旧源回查未用于当前候选。</p>'
        f'{input_table}{facade_table}{height_table}'
        f'<h2>尚未解决</h2><ul>{notes or "<li>模型未填写；仍需结合上表判断未核查范围。</li>"}</ul>'
        f'<details><summary>模型采用的假设</summary><ul>{assumptions}</ul></details>'
        '<h2>当前候选的观察结论</h2><p>以下核对实际参数和宿主是否仍保留，不代表原图判断已经验证。</p>'
        f'<table><tr><th>观察</th><th>当前状态</th><th>依据</th></tr>{claim_rows}</table>'
        f'<details><summary>已替代的说明</summary><ul>{superseded or "<li>无显式替代记录。</li>"}</ul></details>'
        '<details><summary>观察与实际应用</summary><p>采纳、执行成功与原图正确分别判断；'
        '历史应用成功不代表当前候选保留该结果。完整记录见检查记录。</p><pre>'
        + text_html(json.dumps({"adopted_unapplied": result["adopted_unapplied_claims"],
                                  "applications": result["claim_applications"]}, ensure_ascii=False, indent=2))
        + '</pre></details>'
        f'{feedback_html}'
        f'{evidence_html}'
        + render_regularization_html(source, result.get("building_precision", {}).get("regularization"))
        +
        f'<iframe title="保存的 BIM 候选" src="{result["viewer"]}"></iframe>'
        '</html>')
