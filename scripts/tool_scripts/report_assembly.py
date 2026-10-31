"""Deterministic per-run report assembly helpers.

This module builds the additive ``report/`` view for a completed or stopped run.
It deliberately reads only run artifacts and the parent case's ``case_data/``
images; it never imports or reads test_baseline/gt.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import warnings
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.agent.execution.run_meta import RUN_META_DIR
from src.agent.execution.stage_runner import STAGE_ORDER
from src.agent.execution.step_orchestrator import (
    ADVANCE_OK,
    STATE_NAME,
    TERMINAL_STOP,
    StepStatus,
)
from src.validator.checks.schema import Disposition, disposition

PENDING = {
    StepStatus.AWAITING_JUDGE,
    StepStatus.JUDGE_BLOCK,
    StepStatus.AWAITING_REREAD,
    StepStatus.AWAITING_GEOMETRY_APPROVAL,
}

RECOMMENDATION_BUCKETS = ("机制问题", "能力升级", "脚手架建议", "修法")
NO_EVIDENCE_SENTINEL = "本 run 无可证据支持的建议"

_AUDIT_KINDS = ("corrections", "conflicts", "unsupported")
_EVIDENCE_TOKEN_RE = re.compile(r"\[(E:[^\]\s]+)\]")
_AGENT_START_RE = re.compile(r"^<!-- AGENT:START ([A-Za-z0-9_.-]+) -->$")
_AGENT_END_RE = re.compile(r"^<!-- AGENT:END ([A-Za-z0-9_.-]+) -->$")
_GEN_START_RE = re.compile(r"^<!-- GEN:START ([A-Za-z0-9_.-]+) -->$")
_GEN_END_RE = re.compile(r"^<!-- GEN:END ([A-Za-z0-9_.-]+) -->$")

EXPECTED_AGENT_KEYS = ("conclusion", "focus", "diagnosis", "recommendations")
GEN_KEYS = ("model_config", "facts_card", "eyeball_index", "appendix")
VALIDATION_MANIFEST_NAME = "validation_manifest.json"


class ReportMarkerError(ValueError):
    """Raised when AGENT marker structure is ambiguous or unsafe to merge."""


def _status_values(statuses: set[StepStatus]) -> set[str]:
    return {s.value for s in statuses}


_TERMINAL_VALUES = _status_values(TERMINAL_STOP)
_ADVANCE_VALUES = _status_values(ADVANCE_OK)
_PENDING_VALUES = _status_values(PENDING)


def _rel(path: Path, start: Path) -> str:
    return os.path.relpath(path, start).replace(os.sep, "/")


def _slug(value: object) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_")
    return slug or "row"


def _copy_asset(src: Path, out_dir: Path, wanted_name: str, used: set[str]) -> dict:
    stem = Path(wanted_name).stem
    suffix = Path(wanted_name).suffix
    name = wanted_name
    idx = 2
    while name in used:
        name = f"{stem}__{idx}{suffix}"
        idx += 1
    used.add(name)
    dst = out_dir / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return {"filename": name, "path": f"report/eyeball/{name}"}


def collect_eyeball_assets(run_dir: Path) -> dict:
    """Copy real 2D visual artifacts into ``report/eyeball/``.

    The collector is intentionally explicit: it knows the current producer paths
    and records missing producers instead of relying on wishful wildcard names.
    """
    run_dir = Path(run_dir)
    out_dir = run_dir / "report" / "eyeball"
    out_dir.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    assets: list[dict] = []
    missing: list[dict] = []

    explicit = [
        ("reading_grade", run_dir / "0_reading" / "grade.png", "0_reading_grade.png"),
        ("correction_grade", run_dir / "1_correction" / "grade.png", "1_correction_grade.png"),
        ("correction_zones", run_dir / "1_correction" / "zones.png", "1_correction_zones.png"),
        ("correction_elev", run_dir / "1_correction" / "elev.png", "1_correction_elev.png"),
    ]
    # Current flow producer: per-floor role-coloured zone plans (render_all_to_dir).
    # The single-file zones.png/elev.png and per-view plan_*/roles_*/elev_* entries
    # below are legacy producers kept so reports can be regenerated on old runs.
    for src in sorted((run_dir / "1_correction").glob("zones_*.png")):
        explicit.append((f"correction_{src.stem}", src, f"1_correction_{src.name}"))
    for src in sorted((run_dir / "1_correction").glob("plan_*_render.png")):
        explicit.append((f"correction_{src.stem}", src, f"1_correction_{src.name}"))
    for src in sorted((run_dir / "1_correction").glob("roles_*.png")):
        explicit.append((f"correction_{src.stem}", src, f"1_correction_{src.name}"))
    for src in sorted((run_dir / "1_correction").glob("elev_*_render.png")):
        explicit.append((f"correction_{src.stem}", src, f"1_correction_{src.name}"))
    for producer, src, target_name in explicit:
        if src.exists():
            copied = _copy_asset(src, out_dir, target_name, used)
            copied.update({
                "producer": producer,
                "source": _rel(src, run_dir),
                "status": "copied",
            })
            assets.append(copied)
        else:
            missing.append({
                "producer": producer,
                "source": _rel(src, run_dir),
                "status": "missing",
            })

    reading_renders = sorted((run_dir / "0_reading").glob("*_render.png"))
    if reading_renders:
        for src in reading_renders:
            copied = _copy_asset(src, out_dir, f"0_reading_{src.name}", used)
            copied.update({
                "producer": "reading_render",
                "source": _rel(src, run_dir),
                "status": "copied",
            })
            assets.append(copied)
    else:
        missing.append({
            "producer": "reading_render",
            "source": "0_reading/*_render.png",
            "status": "missing",
        })

    case_data = run_dir.parent / "case_data"
    source_views = sorted(case_data.glob("*_view.png"))
    if source_views:
        for src in source_views:
            copied = _copy_asset(src, out_dir, f"case_data_{src.name}", used)
            copied.update({
                "producer": "case_data_view",
                "source": _rel(src, run_dir),
                "status": "copied",
            })
            assets.append(copied)
    else:
        missing.append({
            "producer": "case_data_view",
            "source": "../case_data/*_view.png",
            "status": "missing",
        })

    return {"dir": "report/eyeball", "assets": assets, "missing": missing}


def ensure_geometry_viewer(run_dir: Path) -> dict:
    """Verify or regenerate ``manual_review/geometry_viewer.html``.

    The viewer remains outside ``report/``. If geometry exists and the gitignored
    viewer is absent, this regenerates it from ``2_modelling/building_geometry.json``.
    """
    run_dir = Path(run_dir)
    viewer = run_dir / "manual_review" / "geometry_viewer.html"
    rel_viewer = _rel(viewer, run_dir)
    if viewer.exists():
        return {
            "available": True,
            "path": rel_viewer,
            "report_link": "../manual_review/geometry_viewer.html",
            "status": "existing",
        }

    bg = run_dir / "2_modelling" / "building_geometry.json"
    if not bg.exists():
        return {
            "available": False,
            "path": rel_viewer,
            "status": "unavailable",
            "reason": "missing 2_modelling/building_geometry.json",
        }

    try:
        from render_geometry_viewer import build_viewer_html, discover_roles

        data = json.loads(bg.read_text(encoding="utf-8"))
        viewer.parent.mkdir(parents=True, exist_ok=True)
        viewer.write_text(
            build_viewer_html(data, title=run_dir.name, roles=discover_roles(bg)),
            encoding="utf-8",
        )
    except Exception as e:  # noqa: BLE001 - viewer is useful but not load-bearing
        return {
            "available": False,
            "path": rel_viewer,
            "status": "unavailable",
            "reason": f"regeneration failed: {e}",
        }

    return {
        "available": True,
        "path": rel_viewer,
        "report_link": "../manual_review/geometry_viewer.html",
        "status": "regenerated",
    }


def _stage_entry(stage: str, info: dict) -> dict:
    return {
        "stage": stage,
        "status": str(info.get("status", "")),
        "attempts_used": info.get("attempts_used"),
        "accepted_attempt": info.get("accepted_attempt"),
        "message": info.get("message", ""),
        "route_target": info.get("route_target"),
    }


def _is_geometry_superseded(stage: str, status: str, geometry_approved: bool) -> bool:
    return (
        stage == "3_split_pairing"
        and status == StepStatus.AWAITING_GEOMETRY_APPROVAL.value
        and geometry_approved
    )


def derive_run_state(state: dict, *, geometry_approved: bool) -> dict:
    """Derive report state from the per-stage orchestration ledger.

    ``completed_clean`` scans every expected stage. The only ignored pending
    status is the real geometry approval shape: 3_split_pairing still says
    awaiting_geometry_approval while the run-level geometry_approved flag is true.
    """
    stages = state.get("stages", {}) if isinstance(state, dict) else {}
    ordered: list[dict] = []
    missing_expected: list[str] = []
    ignored_pending: list[dict] = []
    terminals: list[dict] = []
    pendings: list[dict] = []

    for stage in STAGE_ORDER:
        raw = stages.get(stage)
        if not isinstance(raw, dict):
            missing_expected.append(stage)
            continue
        entry = _stage_entry(stage, raw)
        status = entry["status"]
        ordered.append(entry)
        if status in _PENDING_VALUES:
            if _is_geometry_superseded(stage, status, geometry_approved):
                ignored_pending.append(entry)
            else:
                pendings.append(entry)
        if status in _TERMINAL_VALUES:
            terminals.append(entry)

    latest_expected = ordered[-1] if ordered else None
    latest_expected_ok = bool(latest_expected and latest_expected["status"] in _ADVANCE_VALUES)
    completed_clean = (
        not missing_expected
        and latest_expected_ok
        and not terminals
        and not pendings
    )

    if terminals:
        status = "root_stopped"
    elif pendings:
        status = "pending"
    elif completed_clean:
        status = "completed_clean"
    else:
        status = "incomplete"

    return {
        "status": status,
        "completed_clean": completed_clean,
        "root_stop": terminals[-1] if terminals else None,
        "pending": None if terminals else (pendings[-1] if pendings else None),
        "pending_candidates": pendings,
        "ignored_pending": ignored_pending,
        "missing_expected": missing_expected,
        "latest_expected": latest_expected,
        "expected": STAGE_ORDER,
        "stop_reason": state.get("stop_reason") if isinstance(state, dict) else None,
    }


def assert_unique_evidence_ids(evidence_index: list[dict]) -> None:
    counts = Counter(entry.get("id") for entry in evidence_index)
    dupes = sorted(eid for eid, count in counts.items() if eid and count > 1)
    if dupes:
        raise AssertionError(f"duplicate evidence ids: {', '.join(dupes)}")


def _add_entry(entries: list[dict], eid: str, kind: str, source: str, payload: dict) -> None:
    entries.append({"id": eid, "kind": kind, "source": source, "payload": payload})


def _gate_entries(validation_result) -> list[dict]:
    entries: list[dict] = []
    for report_key in sorted(validation_result.reports):
        rep = validation_result.reports[report_key]
        per_check: Counter[str] = Counter()
        for result in rep.results:
            disp = disposition(
                result,
                capability_profile=rep.capability_profile,
                run_profile=rep.run_profile,
            )
            if disp not in (Disposition.BLOCK, Disposition.FLAG):
                continue
            per_check[result.check_id] += 1
            suffix = "" if per_check[result.check_id] == 1 else f"#{per_check[result.check_id]}"
            eid = f"E:gate:{report_key}:{result.check_id}{suffix}"
            _add_entry(entries, eid, "gate", report_key, {
                "report_key": report_key,
                "stage": report_key.split("::")[0],
                "check_id": result.check_id,
                "status": result.status.value,
                "layer": result.layer.value,
                "disposition": disp.value,
                "message": result.message,
                "evidence": result.evidence,
            })
    return entries


def _judge_entries(run_dir: Path) -> list[dict]:
    entries: list[dict] = []
    for stage in STAGE_ORDER:
        attempts_dir = run_dir / stage / "attempts"
        if not attempts_dir.exists():
            continue
        for attempt in sorted(p for p in attempts_dir.iterdir() if p.is_dir() and p.name.isdigit()):
            judge_path = attempt / "judge.json"
            if not judge_path.exists():
                continue
            try:
                verdict = json.loads(judge_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - malformed judge sidecar is not indexable
                continue
            criteria = verdict.get("criteria", [])
            if not isinstance(criteria, list):
                continue
            for ordinal, criterion in enumerate(criteria, start=1):
                eid = f"E:judge:{stage}:{attempt.name}:c{ordinal}"
                _add_entry(entries, eid, "judge", _rel(judge_path, run_dir), {
                    "stage": stage,
                    "attempt": attempt.name,
                    "criterion_ordinal": ordinal,
                    "criterion": criterion,
                    "root_stage": verdict.get("root_stage"),
                    "rubric_id": verdict.get("rubric_id"),
                })
    return entries


def _correction_entries(run_dir: Path) -> list[dict]:
    entries: list[dict] = []
    from src.agent.execution.correction_audit import load_reportable_correction_audit

    try:
        accepted = load_reportable_correction_audit(run_dir)
    except (OSError, ValueError, json.JSONDecodeError):
        from src.agent.execution.manifest import RunManifestV2, load_run_manifest

        if isinstance(load_run_manifest(run_dir), RunManifestV2):
            raise
        return entries
    if accepted is None:
        return entries
    sidecar = accepted.path
    data = accepted.payload
    for kind in _AUDIT_KINDS:
        rows = data.get(kind, [])
        if not isinstance(rows, list):
            continue
        for ordinal, row in enumerate(rows, start=1):
            raw_id = row.get("id") if isinstance(row, dict) else None
            suffix = _slug(raw_id) if raw_id else f"r{ordinal}"
            eid = f"E:corr:{kind}:{suffix}"
            _add_entry(entries, eid, "correction", _rel(sidecar, run_dir), {
                "kind": kind,
                "ordinal": ordinal,
                "raw_id": raw_id,
                "row": row,
            })
    for ordinal, row in enumerate(accepted.window_host_rows, start=1):
        _add_entry(
            entries,
            f"E:corr:window_host:{_slug(row['window_id'])}",
            "correction",
            _rel(sidecar, run_dir),
            {"kind": "window_host", "ordinal": ordinal, "row": row},
        )
    for ordinal, row in enumerate(
        accepted.rejected_window_host_conflicts, start=1
    ):
        _add_entry(
            entries,
            f"E:corr:rejected_window_host:{row['attempt']}:{_slug(row['window_id'])}",
            "correction",
            row["audit_path"],
            {"kind": "rejected_window_host_conflict", "ordinal": ordinal, "row": row},
        )
    return entries


def _evidence_debt_entries(run_dir: Path) -> list[dict]:
    entries: list[dict] = []
    sidecar = run_dir / "1_correction" / "evidence_debt.json"
    if not sidecar.exists():
        return entries
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - malformed sidecar is not indexable
        return entries
    debts = data.get("debts") if isinstance(data, dict) else None
    if not isinstance(debts, list):
        return entries
    for ordinal, row in enumerate(debts, start=1):
        if not isinstance(row, dict):
            continue
        suffix = _slug(
            f"{row.get('view') or 'run'}_{row.get('canonical_check_id') or ordinal}_{ordinal}"
        )
        eid = f"E:debt:{suffix}"
        _add_entry(entries, eid, "evidence_debt", _rel(sidecar, run_dir), {
            "ordinal": ordinal,
            "row": row,
        })
    return entries


def build_evidence_index(
    run_dir: Path,
    validation_result,
    *,
    report_assets: dict,
    run_state: dict,
    ep: dict | None,
) -> list[dict]:
    entries: list[dict] = []
    entries.extend(_gate_entries(validation_result))
    entries.extend(_judge_entries(Path(run_dir)))
    entries.extend(_evidence_debt_entries(Path(run_dir)))
    entries.extend(_correction_entries(Path(run_dir)))

    stop = run_state.get("root_stop") or run_state.get("pending")
    if stop:
        status = stop.get("status", "unknown")
        stage = stop.get("stage", "unknown")
        _add_entry(
            entries,
            f"E:stop:{status}@{stage}",
            "stop",
            f"{RUN_META_DIR}/{STATE_NAME}",
            stop,
        )

    _add_entry(entries, "E:ep:result", "ep", "EP/EP_run/eplusout.end", {"ep": ep})
    _add_entry(entries, "E:geom:digest", "geometry", "2_modelling/building_geometry.json", {
        "geometry_digest": getattr(validation_result, "geometry_digest", None),
        "geometry_approved": getattr(validation_result, "geometry_approved", False),
    })
    for asset in report_assets.get("assets", []):
        _add_entry(entries, f"E:eyeball:{asset['filename']}", "eyeball", asset["path"], asset)

    assert_unique_evidence_ids(entries)
    return entries


def _evidence_ids(evidence_index: list[dict]) -> set[str]:
    return {entry["id"] for entry in evidence_index if "id" in entry}


def lint_report_citations(
    recommendations_text: str,
    evidence_index: list[dict],
    *,
    block_name: str = "AGENT:recommendations",
) -> list[str]:
    """Lexically validate the authored recommendation mini-format.

    The caller supplies the already-extracted AGENT recommendations block; this
    function no longer rediscovers recommendations by scanning the whole report.
    """
    known = _evidence_ids(evidence_index)
    lines = recommendations_text.splitlines()
    sections: dict[str, list[str]] = {}
    errors: list[str] = []
    in_recommendations = not any(line.strip().startswith("## ") for line in lines)
    current_bucket: str | None = None

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            in_recommendations = stripped == "## 建议"
            current_bucket = None
            continue
        if not in_recommendations:
            continue
        if stripped.startswith("### "):
            bucket = stripped[4:].strip()
            if bucket not in RECOMMENDATION_BUCKETS:
                errors.append(f"{block_name}: unknown recommendation bucket: {bucket}")
                current_bucket = None
                continue
            if bucket in sections:
                errors.append(f"{block_name}: duplicate recommendation bucket: {bucket}")
            sections.setdefault(bucket, [])
            current_bucket = bucket
            continue
        if current_bucket is not None:
            sections[current_bucket].append(line.rstrip())

    for bucket in RECOMMENDATION_BUCKETS:
        if bucket not in sections:
            errors.append(f"{block_name}: missing recommendation bucket: {bucket}")
            continue
        errors.extend(
            f"{block_name}: {error}" for error in _lint_bucket(bucket, sections[bucket], known)
        )
    return errors


def _next_nonblank(lines: list[str], start: int) -> tuple[int, str] | None:
    i = start
    while i < len(lines):
        if lines[i].strip():
            return i, lines[i]
        i += 1
    return None


def _lint_bucket(bucket: str, raw_lines: list[str], known: set[str]) -> list[str]:
    errors: list[str] = []
    content = [line for line in raw_lines if line.strip()]
    if content == [NO_EVIDENCE_SENTINEL]:
        return []
    if not content:
        return [f"{bucket}: empty bucket"]

    i = 0
    while i < len(raw_lines):
        line = raw_lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("> note:"):
            i += 1
            continue
        if not line.startswith("- action: "):
            errors.append(f"{bucket}: unsupported prose or malformed record: {stripped}")
            i += 1
            continue
        action = line.removeprefix("- action: ").strip()
        if not action:
            errors.append(f"{bucket}: empty action")

        evidence_pos = _next_nonblank(raw_lines, i + 1)
        if evidence_pos is None or not evidence_pos[1].startswith("  evidence: "):
            errors.append(f"{bucket}: action missing evidence line")
            i += 1
            continue
        evidence_line = evidence_pos[1]
        cited = _EVIDENCE_TOKEN_RE.findall(evidence_line)
        if not cited:
            errors.append(f"{bucket}: action must cite at least one evidence id")
        for eid in cited:
            if eid not in known:
                errors.append(f"{bucket}: unknown evidence id {eid}")

        owner_pos = _next_nonblank(raw_lines, evidence_pos[0] + 1)
        if owner_pos is None or not owner_pos[1].startswith("  owner: "):
            errors.append(f"{bucket}: action missing owner line")
            i = evidence_pos[0] + 1
            continue
        owner = owner_pos[1].removeprefix("  owner: ").strip()
        if not owner:
            errors.append(f"{bucket}: empty owner")
        i = owner_pos[0] + 1

    return errors


def assert_report_citations(recommendations_text: str, evidence_index: list[dict]) -> None:
    errors = lint_report_citations(recommendations_text, evidence_index)
    if errors:
        raise AssertionError("REPORT.md citation lint failed:\n- " + "\n- ".join(errors))


def _state_summary_lines(run_state: dict) -> list[str]:
    status = run_state.get("status")
    if status == "completed_clean":
        return ["- run_state: completed_clean", "- 根因停: none", "- pending: none"]
    if status == "root_stopped":
        root = run_state.get("root_stop") or {}
        lines = [
            f"- run_state: root_stopped",
            f"- 根因停: `{root.get('status')}@{root.get('stage')}`",
        ]
        if root.get("message"):
            lines.append(f"- stop message: {root['message']}")
        return lines
    if status == "pending":
        pending = run_state.get("pending") or {}
        lines = [
            "- run_state: pending",
            f"- pending: `{pending.get('status')}@{pending.get('stage')}`",
        ]
        if pending.get("message"):
            lines.append(f"- pending message: {pending['message']}")
        return lines
    return [
        f"- run_state: {status or 'unknown'}",
        f"- missing expected stages: {run_state.get('missing_expected', [])}",
    ]


def _downstream_missing_after_root(baseline: dict) -> list[dict]:
    run_state = baseline.get("run_state", {})
    root = run_state.get("root_stop") or {}
    root_stage = root.get("stage")
    if root_stage not in STAGE_ORDER:
        return []
    root_index = STAGE_ORDER.index(root_stage)
    out = []
    for item in baseline.get("blocking", []):
        stage = item.get("stage")
        if stage in STAGE_ORDER and STAGE_ORDER.index(stage) > root_index:
            msg = item.get("message", "")
            if "required artifact missing" in msg:
                out.append(item)
    return out


def _status_tldr(baseline: dict) -> str:
    state = baseline.get("run_state", {}).get("status")
    if state == "completed_clean" and baseline.get("signals", {}).get("reading_evidence_clean") is False:
        return "reading_evidence_debt"
    if state == "completed_clean":
        return "completed_clean"
    if state == "root_stopped":
        root = baseline.get("run_state", {}).get("root_stop") or {}
        return f"root_stopped: {root.get('status')}@{root.get('stage')}"
    if state == "pending":
        pending = baseline.get("run_state", {}).get("pending") or {}
        return f"pending: {pending.get('status')}@{pending.get('stage')}"
    return str(state or "unknown")


def _format_signals(signals: dict) -> list[str]:
    if not signals:
        return []
    keys = (
        "reading_syntax_valid",
        "reading_evidence_clean",
        "j0_semantic_clean",
        "pipeline_recovered",
    )
    return [f"- {key}: `{signals.get(key)}`" for key in keys]


def _marker_line(line: str) -> str:
    return line[:-1] if line.endswith("\n") else line


def extract_agent_regions(report_text: str) -> dict[str, str]:
    """Extract AGENT regions, ignoring any marker-like text inside GEN regions."""
    regions: dict[str, str] = {}
    active_key: str | None = None
    active_lines: list[str] = []
    gen_depth = 0

    for lineno, raw_line in enumerate(report_text.splitlines(keepends=True), start=1):
        line = _marker_line(raw_line)

        if _GEN_START_RE.match(line):
            if active_key is None:
                gen_depth += 1
                continue
        if _GEN_END_RE.match(line):
            if active_key is None and gen_depth:
                gen_depth -= 1
                continue
        if gen_depth:
            continue

        start = _AGENT_START_RE.match(line)
        end = _AGENT_END_RE.match(line)

        if start:
            key = start.group(1)
            if key not in EXPECTED_AGENT_KEYS:
                raise ReportMarkerError(f"unknown AGENT marker key {key!r} at line {lineno}")
            if active_key is not None:
                raise ReportMarkerError(
                    f"nested AGENT marker {key!r} at line {lineno}; "
                    f"{active_key!r} is still open"
                )
            if key in regions:
                raise ReportMarkerError(f"duplicate AGENT marker key {key!r} at line {lineno}")
            active_key = key
            active_lines = []
            continue

        if end:
            key = end.group(1)
            if active_key is None:
                raise ReportMarkerError(f"reversed/unmatched AGENT end {key!r} at line {lineno}")
            if key != active_key:
                raise ReportMarkerError(
                    f"reversed AGENT marker at line {lineno}: "
                    f"opened {active_key!r}, closed {key!r}"
                )
            regions[key] = "".join(active_lines)
            active_key = None
            active_lines = []
            continue

        if active_key is not None:
            active_lines.append(raw_line)

    if active_key is not None:
        raise ReportMarkerError(f"unclosed AGENT marker {active_key!r}")

    return regions


def _wrap_region(kind: str, key: str, body: str) -> str:
    text = body if body.endswith("\n") else body + "\n"
    return f"<!-- {kind}:START {key} -->\n{text}<!-- {kind}:END {key} -->\n"


def _format_models(models: dict) -> list[str]:
    if not models:
        return ["- models: `(未读到 run_config.yaml / llm.yaml)`"]
    lines = []
    for key, value in sorted(models.items()):
        if isinstance(value, dict):
            lines.append(
                f"- {key}: `{value.get('model_id', 'unknown')}` "
                f"(effort=`{value.get('effort', 'unknown')}`, "
                f"source=`{value.get('source', 'unknown')}`)"
            )
        else:
            lines.append(f"- {key}: `{value}`")
    return lines


def _format_reading_mode(reading_mode: dict | None) -> list[str]:
    """R4-a: render the reading-stage lane label the report shows next to the
    reading score. L-R1 requires this line to actually change when the
    declared lane changes (not decorative); L-R4 requires dev_function=true
    to render an explicit "not an official score" flag; L-R3 requires a
    legacy_unknown run to render without crashing and without impersonating
    either lane."""
    if not isinstance(reading_mode, dict) or not reading_mode:
        return ["- reading lane: `legacy_unknown`（run 未产出 reading_mode 溯源块）"]
    status = reading_mode.get("status")
    if status != "present":
        return ["- reading lane: `legacy_unknown`（run 未产出 reading_mode 溯源块）"]
    record = reading_mode.get("record") or {}
    lane = record.get("lane", "unknown")
    lines = [f"- reading lane: `{lane}`"]
    if record.get("dev_function") is True:
        lines.append(
            "- ⚠️ 本轮 reading 属 dev 期职能（tool-invention）"
            "—— **不作为正式成绩**（CLAUDE.md §1.5 #7）"
        )
    reading_agent = record.get("reading_agent")
    if isinstance(reading_agent, dict):
        lines.append(
            f"- reading-agent: `{reading_agent.get('model', 'unknown')}` "
            f"(sees_images=`{reading_agent.get('sees_images')}`, "
            f"rework_rounds=`{reading_agent.get('rework_rounds')}`)"
        )
    worker = record.get("reading_worker_agent")
    if isinstance(worker, dict):
        lines.append(
            f"- reading-worker-agent: `{worker.get('model', 'unknown')}` "
            f"(effort=`{worker.get('effort', 'unknown')}`)"
        )
    lines.append(
        f"- toolbox_version: `{record.get('toolbox_version', 'unknown')}` · "
        f"isolation_profile: `{record.get('isolation_profile', 'unknown')}`"
    )
    return lines


def _short_hash(value: object, length: int = 12) -> str:
    return str(value)[:length] if value else "null"


def _format_provenance_summary(provenance: dict) -> str:
    if not provenance:
        return "- provenance: `unavailable`"
    dirty = provenance.get("git_dirty")
    if dirty is True:
        dirty_marker = "dirty"
    elif dirty is False:
        dirty_marker = "clean"
    else:
        dirty_marker = "unknown"
    total = provenance.get("git_dirty_paths_total")
    dirty_suffix = f":{total}" if dirty is True and total is not None else ""
    parts = [
        f"git={_short_hash(provenance.get('git_sha'))}",
        f"dirty={dirty_marker}{dirty_suffix}",
        f"skills={_short_hash(provenance.get('skills_intake_hash'))}",
        f"reading={_short_hash(provenance.get('reading_src_hash'))}",
        f"correction={_short_hash(provenance.get('correction_src_hash'))}",
        f"corr_cfg={_short_hash(provenance.get('correction_config_hash'))}",
    ]
    return f"- provenance: `{' '.join(parts)}`"


def _render_model_config(baseline: dict) -> str:
    lines = [
        f"# {baseline['case']} / {baseline.get('run', '')} REPORT",
        "",
        "## 本次模型配置",
        "",
        f"- run_config.yaml: [../run_config.yaml](../run_config.yaml)",
        f"- llm.yaml: [../llm.yaml](../llm.yaml)",
        f"- recorded: `{baseline.get('recorded', '')}`",
        f"- orchestrator: `{baseline.get('orchestrator', '')}`",
        f"- 自动状态: `{_status_tldr(baseline)}`",
        _format_provenance_summary(baseline.get("provenance", {})),
        *_format_models(baseline.get("models", {})),
        *_format_reading_mode(baseline.get("reading_mode")),
        *_format_signals(baseline.get("signals", {})),
    ]
    return "\n".join(lines).rstrip() + "\n"


def _default_agent_region(key: str, baseline: dict) -> str:
    if key == "conclusion":
        return "\n".join([
            "## 一句话结论",
            "",
            f"- 自动状态: `{_status_tldr(baseline)}`",
            "<!-- AGENT-FILL: 用一句话写 pass/blocked + 本 run 最重要的一件事。 -->",
            "",
        ])
    if key == "focus":
        return "\n".join([
            "## 本轮侧重点",
            "",
            "<!-- AGENT-FILL: 说明这轮在测什么、为何重要。 -->",
            "",
        ])
    if key == "diagnosis":
        return "\n".join([
            "## 错在哪儿 + 归因",
            "",
            "<!-- AGENT-FILL: 用 evidence_index 把 gate/judge/correction/肉检事实串成因果链。 -->",
            "",
        ])
    if key == "recommendations":
        lines = ["## 建议", ""]
        for bucket in RECOMMENDATION_BUCKETS:
            lines += ["", f"### {bucket}", "", NO_EVIDENCE_SENTINEL]
        lines.append("")
        return "\n".join(lines)
    raise KeyError(key)


def _agent_region(key: str, baseline: dict, existing: dict[str, str]) -> str:
    if key in existing:
        return existing[key]
    warnings.warn(
        f"REPORT.md missing AGENT region {key!r}; using placeholder",
        RuntimeWarning,
        stacklevel=2,
    )
    return _default_agent_region(key, baseline)


def render_marked_report(
    baseline: dict,
    *,
    generated_sections: dict[str, str],
    agent_regions: dict[str, str],
) -> str:
    """Render the single marker-delimited REPORT.md."""
    sections: list[str] = []
    sections.append(_wrap_region("GEN", "model_config", _render_model_config(baseline)))
    sections.append(_wrap_region("GEN", "facts_card", generated_sections["facts_card"]))
    for key in ("conclusion", "focus", "diagnosis", "recommendations"):
        sections.append(_wrap_region("AGENT", key, _agent_region(key, baseline, agent_regions)))
    sections.append(_wrap_region("GEN", "eyeball_index", generated_sections["eyeball_index"]))
    sections.append(_wrap_region("GEN", "appendix", generated_sections["appendix"]))
    return "\n".join(section.rstrip() for section in sections).rstrip() + "\n"


def write_report_files(
    run_dir: Path,
    *,
    baseline: dict,
    generated_sections: dict[str, str],
    force_template: bool = False,
) -> None:
    report_dir = Path(run_dir) / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    report = report_dir / "REPORT.md"
    existing_regions: dict[str, str] = {}
    if report.exists():
        existing_regions = extract_agent_regions(report.read_text(encoding="utf-8"))
    if force_template:
        existing_regions = {}
    merged = render_marked_report(
        baseline,
        generated_sections=generated_sections,
        agent_regions=existing_regions,
    )
    recommendation_block = extract_agent_regions(merged)["recommendations"]
    assert_report_citations(recommendation_block, baseline["evidence_index"])
    (report_dir / "FACTS.md").unlink(missing_ok=True)
    (report_dir / "REPORT.template.md").unlink(missing_ok=True)
    report.write_text(merged, encoding="utf-8")
