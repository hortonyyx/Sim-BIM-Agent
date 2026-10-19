"""Run limits and saved floor coverage, derived only from files in one run.

Failure: GLM sm25 spent its 3000 seconds on one floor. No in-memory reminder
latches or assumed room counts: an explicit manifest floor/image scope, source
provenance and immutable successful saves drive reminders and timeout fallback.
"""
from __future__ import annotations

import json
from pathlib import Path
import time


def _read(path):
    return json.loads(path.read_bytes())


def candidate_floor_images(run, candidate, seen=None):
    seen = set() if seen is None else seen
    if candidate in seen:
        return {}
    seen.add(candidate)
    path = run / candidate / "source_model.json"
    if not path.is_file():
        return {}
    source = _read(path)
    floors = {row["id"] for row in source.get("floors", [])}
    provenance = source.get("generation", {}).get("provenance", {})
    mapping = {}
    if provenance.get("parent_candidate"):
        mapping.update(candidate_floor_images(run, provenance["parent_candidate"], seen))
    plan = provenance.get("plan_input", {})
    if plan.get("image"):
        floor = plan.get("geometry_feedback", {}).get("floor_id")
        if floor is None and len(floors) == 1:
            floor = next(iter(floors))
        mapping[plan["image"]] = [floor] if floor else []
    assembly = provenance.get("plan_assembly", {})
    if assembly.get("file"):
        assembly_path = (run / assembly["file"]).resolve()
        if assembly_path.is_relative_to(run.resolve()) and assembly_path.is_file():
            data = _read(assembly_path)
            for row in data.get("floors", []):
                mapping.setdefault(row["image"], []).append(row["floor_id"])
    return {image: sorted(set(ids) & floors) for image, ids in mapping.items() if set(ids) & floors}


def saved_floor_status(toolkit, candidate=None):
    expected = toolkit.manifest.get("floor_plan_images", [])
    saved, ready, mappings, unreadable = [], set(), {}, []
    for path in sorted(toolkit.run.glob("candidate_*/source_model.json")):
        report = path.with_name("report.json")
        try:
            if not report.is_file():
                continue
            report_data = _read(report)
            # A deadline can interrupt file writes. Never let a torn last save
            # prevent handing off an earlier complete candidate (sm25 hard stop).
            _read(path.with_name("proposal.json"))
            mapping = candidate_floor_images(toolkit.run, path.parent.name)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            unreadable.append(dict(candidate=path.parent.name, reason=str(error)))
            continue
        saved.append(path.parent.name)
        mappings[path.parent.name] = mapping
        if report_data.get("source_geometry_ready"):
            ready.add(path.parent.name)
    if (toolkit.run / "seed/source_model.json").is_file():
        try:
            _read(toolkit.run / "seed/proposal.json")
            mappings["seed"] = candidate_floor_images(toolkit.run, "seed")
            saved.insert(0, "seed")
            seed_report = toolkit.run / "seed/report.json"
            if seed_report.is_file() and _read(seed_report).get("source_geometry_ready"):
                ready.add("seed")
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            unreadable.append(dict(candidate="seed", reason=str(error)))
    covered = set().union(*(set(mappings[name]) for name in ready)) if ready else set()
    complete = [name for name in saved if name in ready and expected and set(expected) <= set(mappings[name])]
    chosen_images = set(candidate_floor_images(toolkit.run, candidate)) if candidate else set()
    return dict(expected_floor_plan_images=expected,
        floor_scope_source=toolkit.manifest.get("floor_scope_source", "not_declared"),
        saved_draft_images=sorted(covered), missing_draft_images=sorted(set(expected) - covered),
        latest_saved_candidate=saved[-1] if saved else None,
        latest_complete_candidate=complete[-1] if complete else None,
        unreadable_saved_candidates=unreadable,
        candidate=candidate, candidate_floor_images=sorted(chosen_images),
        missing_candidate_images=sorted(set(expected) - chosen_images) if candidate else [],
        candidate_source_geometry_ready=candidate in ready if candidate else None,
        complete_building=(candidate in ready and set(expected) <= chosen_images if expected and candidate else None),
        note="Floor coverage only; rooms, openings and drawing fidelity are not certified. Unspecified floor scope remains unknown.")


def time_status(toolkit, *, now=None):
    manifest = toolkit.manifest
    start, deadline = manifest.get("started_epoch"), manifest.get("deadline_epoch")
    now = time.time() if now is None else now
    elapsed = max(0, now - start) if start is not None else None
    remaining = None
    dimensions = {}
    status_path = toolkit.run / ".harness_tmp" / "budget_status.json"
    if status_path.is_file():
        status = _read(status_path)
        if (status.get("schema_version") != 1
                or status.get("run_directory") != str(toolkit.run.resolve())
                or status.get("started_epoch") != start):
            raise ValueError("runtime budget status does not belong to this run")
        dimensions = {name: dict(row) for name, row in status["dimensions"].items()}
    if start is not None and deadline is not None:
        total = deadline - start
        elapsed, remaining = max(0, now - start), max(0, deadline - now)
        old = dimensions.get("seconds", {})
        remaining = min(remaining, float(old.get("remaining", remaining)))
        dimensions["seconds"] = dict(remaining=remaining, remaining_fraction=min(
            remaining / total if total > 0 else 0, old.get("remaining_fraction", 1)))
    timing_line = ((f"已用 {elapsed / 60:.1f} 分钟" +
                    ("；整案不设时间上限" if "seconds" not in dimensions else ""))
                   if elapsed is not None and deadline is None else "")
    if not dimensions:
        return dict(active=elapsed is not None, elapsed_seconds=elapsed, remaining_seconds=remaining,
            line=(timing_line + "。" if timing_line else "已用／剩余分钟：未启用计时。"), reminders=[])
    if "seconds" in dimensions:
        remaining = float(dimensions["seconds"]["remaining"])
    tightest = min(dimensions, key=lambda name: dimensions[name]["remaining_fraction"])
    fraction = dimensions[tightest]["remaining_fraction"]
    floor_status = saved_floor_status(toolkit)
    reminders = []
    if fraction <= .5 and floor_status["missing_draft_images"] and not toolkit.readonly:
        reminders.append("最紧额度已过半，尚无草稿的楼层图：" + ", ".join(floor_status["missing_draft_images"]) +
                         "，先按已读信息保存全楼草稿、再细化")
    if fraction < .15:
        reminders.append("剩余不足15%，停止新范围探索，仅有界复核已列严重问题，来不及就交付并列未决" if not toolkit.readonly else
                         "剩余不足15%，停止新范围探索，返回已有观察与未核项")
    if remaining is not None and remaining <= 0:
        reminders.append("时间上限已到，停止执行，交最近完整全楼稿，无完整稿则交最近保存稿并标明不完整")
    labels = {"seconds": "时间", "tokens": "token", "money_usd": "金额估计USD",
              "money_cny": "金额估计CNY", "calls": "模型调用", "tool_calls": "工具调用"}
    amounts = []
    for name in labels:
        if name not in dimensions:
            continue
        value = float(dimensions[name]["remaining"])
        display = (f"{value / 60:.1f} 分钟" if name == "seconds" else
                   f"{value:.4f}" if name.startswith("money_") else f"{int(value):,}")
        amounts.append(f"{labels[name]} {display}")
    line = "保守剩余（本运行）：" + "，".join(amounts) + f"（最紧 {labels[tightest]} {fraction:.1%}）"
    if timing_line:
        line = timing_line + "；" + line
    return dict(active=True, elapsed_seconds=elapsed, remaining_seconds=remaining,
        line="；".join([line, *reminders]) + "。", reminders=reminders, floors=floor_status,
        dimensions=dimensions, tightest_dimension=tightest)


def fallback_selection(toolkit):
    status = saved_floor_status(toolkit)
    if toolkit.manifest.get("mesh_input"):
        from scripts.tool_scripts.bim_agent_mesh_review import mesh_review_status
        candidates = sorted(toolkit.run.glob("candidate_*/source_model.json"))
        seed = toolkit.run / "seed/source_model.json"
        if seed.is_file():
            candidates.insert(0, seed)
        complete = []
        for source_path in candidates:
            try:
                report = _read(source_path.with_name("report.json"))
                _read(source_path.with_name("proposal.json"))
                if not report.get("source_geometry_ready") or not source_path.with_name("viewer.html").is_file():
                    continue
                review = mesh_review_status(toolkit, source_path.parent.name)
                if (review.get("status") == "current" and review.get("coverage") == "whole_building"
                        and review.get("complete_building") is True):
                    complete.append(source_path.parent.name)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                # A partial/changed evidence save does not hide earlier usable work.
                continue
        return (complete[-1], "latest_complete_mesh_review_fallback_not_agent_selected") if complete else (
            status["latest_saved_candidate"], "latest_saved_fallback_not_agent_selected")
    full = status["latest_complete_candidate"]
    return (full or status["latest_saved_candidate"],
            "latest_complete_fallback_not_agent_selected" if full else "latest_saved_fallback_not_agent_selected")
