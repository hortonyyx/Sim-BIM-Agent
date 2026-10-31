"""Read completed BIM audits into a per-run milestone report; never run a model.

This is an evidence index, not another geometry scorer or automatic acceptance
gate. Missing audits remain missing, recovery is separate from cold generation,
and observed ranges describe only explicitly grouped, comparable saved runs.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def opening_metrics(audit):
    if "reference_count" in audit:
        return {k: audit.get(k) for k in
                ("reference_count", "matched", "positions", "hosts", "door_connections")}
    if "comparisons" not in audit or "unmatched_reference" not in audit:
        return None
    rows = audit["comparisons"]
    return {"reference_count": len(rows) + len(audit["unmatched_reference"]),
            "matched": len(rows),
            "positions": sum(r.get("position_match") is True for r in rows),
            "hosts": sum(r.get("host_match") is True for r in rows),
            "door_connections": sum(r.get("connection_match") is True for r in rows)}


def read_run(root, entry):
    run = (root / entry["run"]).resolve()
    evidence, issues = {}, []

    def read(path, *, required=False):
        if not path.is_file():
            if required:
                issues.append(f"Missing {path.name}")
            return None
        raw = path.read_bytes()
        evidence[str(path.relative_to(root))] = hashlib.sha256(raw).hexdigest()
        return json.loads(raw)

    row = {"case": entry["case"], "condition": entry["condition"], "run": entry["run"],
           "notes": entry.get("notes", []), "evidence_sha256": evidence,
           "evidence_issues": issues, "quality": None, "feature_use": None}
    summary = read(run / "summary.json", required=True)
    if summary is None:
        row["status"] = "unfinished_or_missing_summary"
        return row
    row["status"] = "completed" if (summary.get("agent_response_completed") is True
        or summary.get("task_completed") is True) else "interrupted"
    row["invocations"] = summary.get("subscription_invocations")
    row["elapsed_seconds"] = summary.get("elapsed_seconds")
    row["estimated_usd_not_bill"] = summary.get("estimated_cost_usd")
    manifest = read(run / "inputs.json", required=True)
    audit = read(run / "postrun_audit.json", required=True)
    request = read(run / "agent_request.json", required=True)
    receipts = [read(p) for p in sorted(run.glob("*_receipt.json"))]
    models = sorted({r["actual_model"] for r in receipts if r.get("actual_model")})
    if not models:
        issues.append("Actual model receipts unavailable")
    row["actual_models"] = models
    row["mode"] = "unknown"
    if manifest:
        saved = manifest.get("input_contents", {}).get("saved_generated_proposal", {}).get("included")
        row["mode"] = ("recovery" if saved or "seed" in manifest or "plan_recovery" in manifest
                       else "cold" if saved is False else "unknown")
        row["conditions"] = {k: manifest.get(k) for k in
                             ("images", "scope", "provider", "implementation_sha256", "input_contents")}
        row["conditions"].update(mode=row["mode"], actual_models=models,
            max_candidates=manifest.get("max_candidates", 6),
            continuation_rounds=manifest.get("continuation_rounds", 0),
            effort=request.get("effort") if request else None,
            timeout_seconds=request.get("timeout_seconds") if request else None,
            system_prompt_sha256=digest(request.get("system_prompt")) if request else None)
        # Experimental shims may change exposed behavior without changing the
        # production manifest. Preserve their frozen fields except run provenance.
        experiment = read(run / "experiment_condition.json")
        if experiment:
            row["conditions"]["experiment"] = {k: v for k, v in experiment.items()
                                                if k not in {"producer_commit", "run"}}
        row["condition_sha256"] = digest(row["conditions"])
    if audit is None:
        return row
    selected = (summary.get("delivery") or {}).get("candidate")
    candidate = audit.get("candidate")
    row["candidate"] = candidate
    if not selected or selected != candidate:
        issues.append("Audit candidate does not match the final selected delivery")
        return row
    source = read(run / candidate / "source_model.json", required=True)
    if source is None:
        return row
    if audit.get("source_model_sha256", source.get("source_model_sha256")) != source.get("source_model_sha256"):
        issues.append("Audit source hash does not match the selected source")
        return row
    opening_path = root / entry["opening_audit"] if "opening_audit" in entry else run / "evaluation/original_openings.json"
    openings = read(opening_path, required=True)
    if openings and (openings.get("candidate", candidate) != candidate or
                     openings.get("source_model_sha256", source.get("source_model_sha256")) != source.get("source_model_sha256")):
        issues.append("Opening audit belongs to another candidate or source")
        openings = None
    browser = read(run / "browser_qa/report.json")
    quality = {
        "counts": {k: len(source[k]) for k in ("floors", "spaces", "openings", "connections")},
        "strict_partition_status": audit.get("strict_partition_status", audit.get("partition_status")),
        "space_identity_findings": audit.get("space_identity_findings"),
        "openings": opening_metrics(openings) if openings is not None else None,
        "opening_reference_sha256": (openings.get("reference_sha256", openings.get("observation_sha256"))
                                      if openings else None),
        "opening_tolerance": openings.get("tolerance") if openings else None,
        "height_mismatches_in_existing_tolerance": audit.get("height_mismatches"),
        "height_coverage": audit.get("height_coverage"),
        "source_display_replay_exact": audit.get("source_display_replay_exact",
            (audit["source_replay_exact"] and audit["display_replay_exact"])
            if "source_replay_exact" in audit and "display_replay_exact" in audit else None),
        "browser_status": browser.get("status") if browser else None,
    }
    metrics = quality["openings"]
    if metrics is not None and not (
        all(type(v) is int and v >= 0 for v in metrics.values())
        and metrics["reference_count"] > 0
        and metrics["positions"] <= metrics["matched"] <= metrics["reference_count"]
        and metrics["hosts"] <= metrics["matched"]
        and metrics["door_connections"] <= metrics["matched"]
    ):
        quality["openings"] = None
        issues.append("Opening metrics are incomplete or have an invalid denominator/count")
    row["quality"] = quality
    feature = entry.get("feature_count")
    if feature:
        value = read(run / feature["file"], required=True)
        for key in feature["path"]:
            value = value.get(key) if isinstance(value, dict) else None
        valid = type(value) is int and value >= 0
        row["feature_use"] = {"name": feature["name"], "count": value if valid else None,
            "status": ("exercised" if value else "unexercised") if valid else "unknown",
            "interpretation": "Actual reported use, not correct interpretation, benefit or causation."}
    return row


def report(root, config):
    entries = config["runs"]
    paths = [(root / e["run"]).resolve() for e in entries]
    if len(paths) != len(set(paths)):
        raise ValueError("Do not count the same run more than once")
    rows = [read_run(root, entry) for entry in entries]
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["case"], row["condition"])].append(row)
    groups = []
    for (case, condition), members in grouped.items():
        fields = set().union(*(r.get("conditions", {}).keys() for r in members))
        differences = sorted(k for k in fields
                             if len({digest(r.get("conditions", {}).get(k)) for r in members}) > 1)
        comparable = (all(r.get("condition_sha256") and not r["evidence_issues"] for r in members)
                      and len({r.get("condition_sha256") for r in members}) == 1
                      and all((r["quality"] or {}).get("openings") for r in members)
                      and len({r["quality"]["opening_reference_sha256"] for r in members}) == 1
                      and all(r["quality"]["opening_reference_sha256"] for r in members)
                      and len({digest(r["quality"]["opening_tolerance"]) for r in members}) == 1
                      and len({r["quality"]["openings"]["reference_count"] for r in members}) == 1)
        positions = [r["quality"]["openings"]["positions"] for r in members] if comparable else []
        groups.append({"case": case, "condition": condition, "sample_count": len(members),
            "statuses": [r["status"] for r in members], "comparable_saved_conditions": bool(comparable),
            "different_condition_fields": differences,
            "position_count_range": [min(positions), max(positions)] if positions and all(type(p) is int for p in positions) else None,
            "stability": "not_established", "runs": [r["run"] for r in members]})
    return {"schema_version": "bim_regression_report_v1", "model_calls": 0,
            "runs": rows, "groups": groups,
            "interpretation": "Saved audits only. No pooled success rate, automatic acceptance, or claim of a causal regression.",
            "limits": ["Missing evidence is unknown, never a pass; interrupted runs remain in the report.",
                       "Ranges cover listed observations, not a statistical reliability guarantee or sampling seed control.",
                       "Height tolerance matches and strict partition severity need original-drawing interpretation.",
                       "Recovery, changed conditions and unexercised features cannot establish cold-generation improvement.",
                       "Evidence hashes identify consumed records; this report does not rerun or independently certify their audits."]}


def markdown(data):
    lines = ["# BIM 节点质量记录", "", "逐次列出保存结果；不自动判定验收、稳定性或回退根因。缺失证据保留未知。", "",
             "| 案例 / 条件 | 运行 | 模式 / 完成状态 | 空间 / 门窗 / 连接 | 原图位置 / 宿主 / 连接 | 严格分区 | 新功能使用 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in data["runs"]:
        q = row["quality"] or {}
        counts, openings = q.get("counts"), q.get("openings")
        inventory = "/".join(str(counts[k]) for k in ("spaces", "openings", "connections")) if counts else "未知"
        matches = (f"{openings['positions']}/{openings['reference_count']}；宿主{openings['hosts']}；连接{openings['door_connections']}"
                   if openings else "未知")
        use = row["feature_use"]
        values = [f"{row['case']} / {row['condition']}", Path(row["run"]).name,
                  f"{row.get('mode', 'unknown')} / {row['status']}", inventory, matches,
                  q.get("strict_partition_status") or "未知", f"{use['status']} ({use['count']})" if use else "未单列"]
        lines.append("| " + " | ".join(str(v).replace("|", "\\|").replace("\n", " ") for v in values) + " |")
    lines += ["", "严格分区、位置、宿主和连通分别解读；宽容差高度匹配不代替逐窗语义复核。", ""]
    for group in data["groups"]:
        if group["sample_count"] > 1:
            detail = (f"已列同条件位置通过数范围 {group['position_count_range']}"
                      if group["comparable_saved_conditions"] else
                      f"不合并统计；条件差异 {group['different_condition_fields']}，另见证据缺项/参照范围")
            lines.append(f"- {group['case']} / {group['condition']}：{group['sample_count']} 次；{detail}。不据此宣称稳定。")
    lines.append("")
    for row in data["runs"]:
        for note in row["notes"] + row["evidence_issues"]:
            lines.append(f"- {Path(row['run']).name}: {note}")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--out", type=Path, required=True, help="New report directory; existing directories are refused")
    args = parser.parse_args()
    data = report(args.root.resolve(), json.loads(args.config.read_text()))
    data["config_sha256"] = hashlib.sha256(args.config.read_bytes()).hexdigest()
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / "report.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    (args.out / "README.md").write_text(markdown(data))
    print(json.dumps({"runs": len(data["runs"]), "groups": len(data["groups"]), "model_calls": 0}))


if __name__ == "__main__":
    main()
