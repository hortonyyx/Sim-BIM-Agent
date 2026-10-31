"""Record a case as a self-contained baseline: _run/baseline.json + report/.

The new (2026-06-16) baseline scheme: a clean anchor case carries its own machine
score-card (_run/baseline.json) + a curated report/ folder. This tool runs the
deterministic gate① over the on-disk run (validate_case), rolls it up
(summarize_gates), folds in the model config (llm.yaml) + EP end-state, and
writes _run/baseline.json plus the single human-facing report/REPORT.md.

It does NOT run the pipeline or any LLM — it records an already-produced run. The
judge② verdicts (the Agent's, in <stage>/attempts/NNN/judge.json) are summarized
if present. Timestamp/orchestrator are passed in (no Date.now in tooling here).

Usage:
    python scripts/tool_scripts/record_baseline.py <case> <run> \
        --base-dir case_tests/e2e_tests --date 2026-06-16 --orchestrator opus-4.8
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import warnings
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.agent.execution import load_state, summarize_gates, validate_case
from src.agent.execution.approval import APPROVAL_NAME
from src.agent.execution.manifest import MANIFEST_NAME
from src.agent.execution.reading_mode import provision_reading_mode, resolve_reading_mode
from src.agent.execution.run_config import RunConfig, load_run_config
from src.agent.execution.run_meta import RUN_META_DIR, run_meta_path
from src.agent.execution.stage_runner import STAGE_ORDER
from src.agent.execution.step_orchestrator import STATE_NAME
from src.agent.judge.verdict import StageVerdict

from report_assembly import (
    VALIDATION_MANIFEST_NAME,
    build_evidence_index,
    collect_eyeball_assets,
    derive_run_state,
    ensure_geometry_viewer,
    write_report_files,
)

BASELINE_NAME = "baseline.json"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
_GIT_DIRTY_PATHS_CAP = 50


def _hash_directory_contents(root: Path) -> str:
    """Deterministic sha256 over sorted relative paths and file bytes."""
    root = Path(root)
    h = hashlib.sha256()
    files = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if "__pycache__" in rel.parts or path.suffix == ".pyc":
            continue
        files.append(path)
    for path in sorted(files, key=lambda p: p.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix().encode("utf-8")
        h.update(rel)
        h.update(b"\0")
        h.update(path.read_bytes())
    return h.hexdigest()


def _hash_file_contents(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _git_output(args: list[str]) -> str:
    return subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _collect_git_provenance() -> tuple[dict, str | None]:
    try:
        git_sha = _git_output(["rev-parse", "HEAD"]).strip()
        status = _git_output(["status", "--porcelain", "--untracked-files=all"])
    except Exception as e:  # noqa: BLE001 — provenance is best-effort metadata
        return (
            {
                "git_sha": None,
                "git_dirty": None,
                "git_dirty_paths": None,
                "git_dirty_paths_total": None,
                "git_dirty_paths_cap": _GIT_DIRTY_PATHS_CAP,
            },
            f"git provenance unavailable: {e}",
        )

    dirty_paths = []
    for line in status.splitlines():
        path = line[3:] if len(line) > 3 else line
        dirty_paths.append(path.split(" -> ", 1)[-1])
    return (
        {
            "git_sha": git_sha,
            "git_dirty": bool(dirty_paths),
            "git_dirty_paths": dirty_paths[:_GIT_DIRTY_PATHS_CAP],
            "git_dirty_paths_total": len(dirty_paths),
            "git_dirty_paths_cap": _GIT_DIRTY_PATHS_CAP,
        },
        None,
    )


def _collect_provenance() -> dict:
    from src.agent.legacy.resources import INTAKE_RESOURCE_RELATIVE

    # Future golden exact re-record comparisons must normalize/exclude
    # environment-dependent provenance fields such as git dirtiness.
    provenance, git_error = _collect_git_provenance()
    errors = [git_error] if git_error else []
    targets = {
        # Historical report key; hash the relocated resources actually in use.
        "skills_intake_hash": PROJECT_ROOT / INTAKE_RESOURCE_RELATIVE,
        "reading_src_hash": PROJECT_ROOT / "src" / "agent" / "reading",
        "correction_src_hash": PROJECT_ROOT / "src" / "agent" / "correction",
    }
    for key, path in targets.items():
        try:
            provenance[key] = _hash_directory_contents(path)
        except Exception as e:  # noqa: BLE001 — provenance is best-effort metadata
            provenance[key] = None
            errors.append(f"{key} unavailable: {e}")
    try:
        provenance["correction_config_hash"] = _hash_file_contents(
            PROJECT_ROOT / "src" / "configs" / "correction.yaml"
        )
    except Exception as e:  # noqa: BLE001 — provenance is best-effort metadata
        provenance["correction_config_hash"] = None
        errors.append(f"correction_config_hash unavailable: {e}")
    if errors:
        provenance["collection_error"] = "; ".join(errors)
    return provenance


def _load_llm_yaml(run_dir: Path) -> dict:
    p = run_dir / "llm.yaml"
    if not p.exists():
        return {}
    try:
        from omegaconf import OmegaConf

        cfg = OmegaConf.to_container(OmegaConf.load(p), resolve=False)
    except Exception:  # noqa: BLE001 — config read is best-effort metadata
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _model_entry(model_id: object = None, *, effort: object = None, source: str) -> dict:
    return {
        "model_id": str(model_id) if model_id not in (None, "") else "unknown",
        "effort": str(effort) if effort not in (None, "") else "unknown",
        "source": source,
    }


def _model_from_run_config(cfg: RunConfig, role: str) -> dict | None:
    raw = cfg.models.get(role)
    if raw in (None, ""):
        return None
    source = f"run_config.yaml:models.{role}"
    if isinstance(raw, dict):
        model_id = raw.get("model_id") or raw.get("model_name") or raw.get("model")
        effort = raw.get("effort") or raw.get("reasoning_effort")
        return _model_entry(model_id, effort=effort, source=source)
    return _model_entry(raw, source=source)


def _model_from_llm_section(llm: dict, section: str, *, source: str) -> dict | None:
    sec = llm.get(section)
    if not isinstance(sec, dict) or not sec.get("model_name"):
        return None
    return _model_entry(
        sec.get("model_name"),
        effort=sec.get("reasoning_effort") or sec.get("effort"),
        source=source,
    )


def _models_from_llm_yaml(run_dir: Path, *, orchestrator: str | None = None) -> dict:
    """Structured model provenance for baseline.models.

    ``run_config.yaml`` is authoritative for reading/orchestrator and overrides
    legacy ``llm.yaml`` where both exist. Missing metadata is explicit
    ``unknown`` rather than silent omission.
    """
    run_cfg = load_run_config(run_dir)
    llm = _load_llm_yaml(run_dir)
    if not run_cfg.present:
        warnings.warn(
            f"{run_dir / 'run_config.yaml'} missing; baseline.models will use "
            "llm.yaml fallbacks plus unknown placeholders",
            RuntimeWarning,
            stacklevel=2,
        )

    models: dict[str, dict] = {}
    for role in ("reading", "correction", "mep", "default", "orchestrator"):
        from_cfg = _model_from_run_config(run_cfg, role)
        if from_cfg is not None:
            models[role] = from_cfg
            continue
        if role == "correction":
            models[role] = (
                _model_from_llm_section(
                    llm, "intake_correction", source="llm.yaml:intake_correction"
                )
                or _model_entry(source="unknown")
            )
        elif role == "mep":
            models[role] = (
                _model_from_llm_section(llm, "intake_mep", source="llm.yaml:intake_mep")
                or _model_from_llm_section(
                    llm,
                    "intake_correction",
                    source="llm.yaml:intake_correction(fallback)",
                )
                or _model_entry(source="unknown")
            )
        elif role == "default":
            models[role] = (
                _model_from_llm_section(llm, "default", source="llm.yaml:default")
                or _model_entry(source="unknown")
            )
        elif role == "orchestrator" and orchestrator:
            models[role] = _model_entry(
                orchestrator,
                source="record_baseline.orchestrator_arg",
            )
        else:
            models[role] = _model_entry(source="unknown")
    return models


def _ep_end(case_dir: Path) -> dict | None:
    run = case_dir / "EP" / "EP_run"
    if not (run / "eplusout.end").exists():
        return None
    from src.runner.runner import read_ep_end

    end = read_ep_end(run)
    if end is None:
        return None
    return {"completed": end["completed"], "severe": end["severe"],
            "warnings": end["warnings"]}


def _geometry_counts(case_dir: Path) -> dict:
    bg = case_dir / "2_modelling" / "building_geometry.json"
    if not bg.exists():
        return {}
    d = json.loads(bg.read_text())
    return {"zones": len(d.get("zones", [])),
            "surfaces": len(d.get("surfaces", [])),
            "windows": len(d.get("windows", []))}


def _draws_and_verdicts(case_dir: Path) -> tuple[dict, list]:
    """Count append-only attempts per stage + collect judge verdict summaries."""
    draws: dict[str, int] = {}
    verdicts: list[dict] = []
    for stage in ("0_reading", "1_correction", "2_modelling", "3_split_pairing",
                  "4_mep", "5_intakeoutput"):
        adir = case_dir / stage / "attempts"
        if not adir.exists():
            continue
        attempts = [p for p in adir.iterdir() if p.is_dir() and p.name.isdigit()]
        draws[stage] = len(attempts)
        for ap in sorted(attempts):
            jf = ap / "judge.json"
            if jf.exists():
                try:
                    v = json.loads(jf.read_text())
                    blocking = _verdict_blocking(v)
                except Exception:  # noqa: BLE001 — best-effort verdict metadata
                    pass
                else:
                    verdicts.append({"stage": stage, "attempt": int(ap.name),
                                     "rubric_id": v.get("rubric_id"),
                                     "blocking": blocking,
                                     "root_stage": v.get("root_stage")})
    return draws, verdicts


def _verdict_blocking(v: dict) -> bool:
    return StageVerdict.model_validate(v).blocking


_AUDIT_KINDS = ("corrections", "conflicts", "unsupported")
_CORRECTION_ROW_KEYS = (
    "id",
    "rule_id",
    "target",
    "source_ids",
    "original_value",
    "resolved_value",
    "delta",
    "changes_topology",
)
_CORRECTION_ROW_CAP = 20


def _empty_corrections_summary(
    sidecar_path: str,
    *,
    present: bool,
    parse_status: str,
    parse_error: str | None = None,
) -> dict:
    summary = {
        "sidecar_path": sidecar_path,
        "present": present,
        "parse_status": parse_status,
        "counts": {kind: 0 for kind in _AUDIT_KINDS},
        "by_rule_id": {},
        "by_stage": {},
        "corrections": {"total": 0, "shown": 0, "cap": _CORRECTION_ROW_CAP, "rows": []},
        "conflicts": [],
        "unsupported": [],
    }
    if parse_error:
        summary["parse_error"] = parse_error
    return summary


def _as_list(obj) -> list:
    return obj if isinstance(obj, list) else []


def _string_count_value(entry: dict, key: str) -> str | None:
    value = entry.get(key)
    if value is None or value == "":
        return None
    return str(value)


def _count_audit_fields(entries: list) -> tuple[dict, dict]:
    by_rule_id: Counter[str] = Counter()
    by_stage: Counter[str] = Counter()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rule_id = _string_count_value(entry, "rule_id")
        stage = _string_count_value(entry, "stage")
        if rule_id:
            by_rule_id[rule_id] += 1
        if stage:
            by_stage[stage] += 1
    return dict(sorted(by_rule_id.items())), dict(sorted(by_stage.items()))


def _summarize_correction_rows(corrections: list, cap: int = _CORRECTION_ROW_CAP) -> dict:
    rows = []
    for entry in corrections[:cap]:
        if isinstance(entry, dict):
            row = {key: entry.get(key) for key in _CORRECTION_ROW_KEYS if key in entry}
        else:
            row = {"value": entry}
        rows.append(row)
    return {"total": len(corrections), "shown": len(rows), "cap": cap, "rows": rows}


def _corrections_summary(run_dir: Path) -> dict:
    """Best-effort read of 1_correction/corrections.json for attribution reporting.

    The sidecar is intentionally heterogeneous: LLM A0 entries are rich dicts,
    deterministic-core entries are narrower, and legacy/bad entries should not make
    baseline recording fail.
    """
    from src.agent.execution.correction_audit import load_reportable_correction_audit

    try:
        accepted = load_reportable_correction_audit(run_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        from src.agent.execution.manifest import RunManifestV2, load_run_manifest

        # A manifest-bound/B5 identity failure is load-bearing and must abort
        # report generation; legacy root summaries remain best-effort.
        if isinstance(load_run_manifest(run_dir), RunManifestV2):
            raise
        return _empty_corrections_summary(
            "1_correction/corrections.json",
            present=True,
            parse_status="malformed_json",
            parse_error=str(exc),
        )
    if accepted is None:
        rel = "1_correction/corrections.json"
        return _empty_corrections_summary(rel, present=False, parse_status="missing")
    sidecar = accepted.path
    rel = sidecar.relative_to(run_dir).as_posix()
    data = accepted.payload

    corrections = _as_list(data.get("corrections", []))
    conflicts = _as_list(data.get("conflicts", []))
    unsupported = _as_list(data.get("unsupported", []))
    by_rule_id, by_stage = _count_audit_fields(corrections + conflicts + unsupported)
    return {
        "sidecar_path": rel,
        "present": True,
        "parse_status": "ok",
        "counts": {
            "corrections": len(corrections),
            "conflicts": len(conflicts),
            "unsupported": len(unsupported),
        },
        "by_rule_id": by_rule_id,
        "by_stage": by_stage,
        "corrections": _summarize_correction_rows(corrections),
        "conflicts": conflicts,
        "unsupported": unsupported,
        "window_hosts": list(accepted.window_host_rows),
        "rejected_window_host_conflicts": list(
            accepted.rejected_window_host_conflicts
        ),
    }


def _annotation_basis_summary(run_dir: Path) -> dict:
    """Best-effort read of 1_correction/annotation_basis.json (2026-08-12,
    摊 C: "让标注法这个观测量可见" -- see AI_agent/archive/2026-09-08_pre_takeover/plan.md〇-C). Pure
    observation, advisory-only: a missing sidecar (legacy v1/v2 run, or a v3
    run whose facade envelope never accepted an axis) is normal, not an
    error, and must never make report generation fail or change any
    gate①/judge② outcome."""
    path = run_dir / "1_correction" / "annotation_basis.json"
    rel = "1_correction/annotation_basis.json"
    if not path.exists():
        return {"sidecar_path": rel, "present": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "sidecar_path": rel, "present": True,
            "parse_status": "malformed_json", "parse_error": str(exc),
        }
    if not isinstance(data, dict):
        return {
            "sidecar_path": rel, "present": True,
            "parse_status": "malformed_json", "parse_error": "sidecar is not a JSON object",
        }
    return {"sidecar_path": rel, "present": True, "parse_status": "ok", **data}


def _j0_semantic_clean(verdicts: list[dict]) -> bool | None:
    j0 = [
        v for v in verdicts
        if v.get("stage") == "0_reading" or v.get("rubric_id") == "J0"
    ]
    if not j0:
        return None
    return not any(v.get("blocking") for v in j0)


def _pipeline_recovered(corrections_summary: dict) -> bool | None:
    if corrections_summary.get("parse_status") != "ok":
        return None
    counts = corrections_summary.get("counts", {})
    if counts.get("conflicts", 0) or counts.get("unsupported", 0):
        return False
    if counts.get("corrections", 0):
        return True
    return None


def _eyeball_checklist(
    report_assets: dict,
    viewer: dict,
    summary: dict,
    corrections_summary: dict | None = None,
) -> list[str]:
    """The 🔍 L-肉眼 list: perceptual items deterministic + judge can't fully
    settle. Always-on items + one per surfaced flag."""
    items = []
    for asset in report_assets.get("assets", []):
        items.append(
            f"2D 肉检 `{asset['path']}`（from `{asset['source']}` / {asset['producer']}）")
    for missing in report_assets.get("missing", []):
        items.append(
            f"missing eyeball producer `{missing['source']}` ({missing['producer']})")
    if viewer.get("available"):
        items.append(
            f"3D 几何 `{viewer['path']}`（{viewer['status']}；浏览器打开 orbit / 半透明 / "
            "截面 / 爆炸 / 量距，确认无误后 `approve-geometry`）")
    else:
        items.append(f"3D 几何 viewer unavailable: {viewer.get('reason', 'unknown')}")
    for f in summary.get("flags", []):
        items.append(
            f"flag [{f['stage']}::{f['check']}] —— {f['message']}（对应渲染件人工核一眼）")
    if corrections_summary:
        c = corrections_summary.get("counts", {})
        conflicts = c.get("conflicts", 0)
        unsupported = c.get("unsupported", 0)
        if conflicts or unsupported:
            items.append(
                "audit-derived [corrections_summary] —— "
                f"`corrections.json` 有 {conflicts} conflicts / {unsupported} unsupported，"
                "人核看错↔改错归因")
    return items


def record_baseline(
    run_dir: Path,
    *,
    date: str,
    orchestrator: str,
    require_ep: bool = False,
    run_profile: str = "exploratory",
    force_template: bool = False,
    require_reading_mode: bool = False,
) -> dict:
    """Record a self-contained RUN (``<case>/run_<note>/``) as a baseline. The
    case (materials) is the run's parent; products + llm.yaml live in the run.

    ``require_reading_mode`` (R4-a, L-R2): when True, this record fail-closes
    unless ``run_config.yaml`` declares a ``reading_mode:`` section (freezing
    it into ``_run/reading_mode.json`` on first record). Defaults to False so
    every pre-existing direct caller of this function is unaffected; the
    ``run_stage.py flow --record`` CLI path is the one caller that passes
    True — see src/agent/execution/reading_mode.py's module docstring for the
    full placement rationale."""
    run_dir = Path(run_dir)
    case = run_dir.parent.name
    if require_reading_mode:
        # L-R2: fail-closed FIRST, before any gate①/evidence machinery runs —
        # a run recorded through the official ritual without a declared
        # reading_mode must refuse before doing any other work. Raises
        # reading_mode_not_declared / reading_mode_invalid / reading_mode_drift
        # (see src/agent/execution/reading_mode.py).
        provision_reading_mode(run_dir, declared=load_run_config(run_dir).reading_mode)
    # R1-5 (裁定 §1.3): consume the FROZEN run policy, not a self-rolled RunPolicy
    # — the geometry gate + capability profile judge on the run's declared tier,
    # not RunPolicy() defaults (laxest exploratory/rectangular/optional).  The
    # legacy CLI parameters deliberately do NOT recreate a requested strict
    # tier: a run without a frozen record is read-only legacy-defaulted.
    from src.agent.execution.run_policy_freeze import (
        effective_run_policy,
        resolve_frozen_run_policy,
    )

    frozen = resolve_frozen_run_policy(run_dir)
    # r2-4 (ruling 2026-08-04 §2): require_ep is a per-invocation operational
    # knob (from the caller's --with-ep / --require-ep), NOT read from the frozen
    # record's context. The frozen record supplies only the tier (run_profile /
    # capability_profile) via effective_run_policy.
    policy = effective_run_policy(run_dir, require_ep=require_ep)
    # r2c-1 (ruling 2026-08-04 §1): the tier (run_profile / capability_profile)
    # written into the baseline header must come from the SAME policy that was
    # fed to validate_case (effective_run_policy), NOT from ``frozen`` directly.
    # frozen and effective agree in the well-formed case (effective derives its
    # tier from the frozen record), so this is behaviour-preserving — but it
    # makes the header WITNESS what validate_case actually judged on. Only the
    # provenance fields (source / legacy_defaulted / policy_hash) stay on frozen
    # (mirrors step_orchestrator.approve_geometry's split).
    res = validate_case(run_dir, policy=policy, write_reports=False)
    summary = summarize_gates(res.reports)
    counts = _geometry_counts(run_dir)
    draws, verdicts = _draws_and_verdicts(run_dir)
    state = load_state(run_dir)  # stepwise orchestration ledger (stop_reason etc.)
    corr_summary = _corrections_summary(run_dir)
    annotation_basis_summary = _annotation_basis_summary(run_dir)  # 摊 C, 2026-08-12
    signals = {
        **summary.get("signals", {}),
        "j0_semantic_clean": _j0_semantic_clean(verdicts),
        "pipeline_recovered": _pipeline_recovered(corr_summary),
    }
    ep = _ep_end(run_dir)
    report_assets = collect_eyeball_assets(run_dir)
    viewer = ensure_geometry_viewer(run_dir)
    run_state = derive_run_state(state, geometry_approved=res.geometry_approved)
    evidence_index = build_evidence_index(
        run_dir, res, report_assets=report_assets, run_state=run_state, ep=ep)
    # L-R3: read-only, never fails — a run with no frozen reading_mode.json
    # (predates this feature, or never opted in) resolves to legacy_unknown
    # rather than being judged non-compliant or coerced into either lane.
    reading_mode_resolution = resolve_reading_mode(run_dir)
    baseline = {
        "case": case,
        "run": run_dir.name,
        "recorded": date,
        "orchestrator": orchestrator,
        "models": _models_from_llm_yaml(run_dir, orchestrator=orchestrator),
        "reading_mode": reading_mode_resolution.model_dump(mode="json"),
        "geometry": counts,
        "geometry_digest": res.geometry_digest,
        "geometry_approved": res.geometry_approved,
        "run_policy": {
            "source": frozen.source,
            "legacy_defaulted": frozen.legacy_defaulted,
            "run_profile": policy.run_profile,
            "capability_profile": policy.capability_profile,
            "policy_hash": frozen.policy_hash,
        },
        "gates": summary["gates"],
        "signals": signals,
        "flags": summary["flags"],
        "blocking": summary["blocking"],
        "judge_verdicts": verdicts,
        "draws": draws,
        "orchestration": state.get("stages", {}),
        "stop_reason": state.get("stop_reason"),
        "ep": ep,
        "corrections_summary": corr_summary,
        "annotation_basis_summary": annotation_basis_summary,
        "evidence_index": evidence_index,
        "provenance": _collect_provenance(),
        "blocked": res.blocked,
    }
    report_context = {
        **baseline,
        "run_state": run_state,
        "report_assets": report_assets,
        "viewer": viewer,
    }
    run_meta_path(run_dir, BASELINE_NAME, for_write=True).write_text(
        json.dumps(baseline, indent=2, ensure_ascii=False), encoding="utf-8")
    write_report_files(
        run_dir,
        baseline=report_context,
        generated_sections={
            "facts_card": _render_facts_card(report_context),
            "eyeball_index": _render_eyeball_index(
                report_context,
                _eyeball_checklist(report_assets, viewer, summary, corr_summary),
            ),
            "appendix": _render_appendix(report_context),
        },
        force_template=force_template,
    )
    return report_context


def _audit_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _render_corrections_audit(summary: dict) -> list[str]:
    lines = ["", "## 校正审计（看错↔改错归因）"]
    path = summary.get("sidecar_path", "1_correction/corrections.json")
    status = summary.get("parse_status", "unknown")
    if not summary.get("present"):
        lines += ["", f"- （无 corrections.json）`{path}`"]
        return lines
    if status != "ok":
        msg = summary.get("parse_error", "")
        lines += ["", f"- `corrections.json` 读取状态: {status}" + (f" — {msg}" if msg else "")]
        return lines

    counts = summary.get("counts", {})
    conflicts = summary.get("conflicts", [])
    unsupported = summary.get("unsupported", [])
    corrections = summary.get("corrections", {})
    rows = corrections.get("rows", [])

    if conflicts:
        lines += ["", "### conflicts[]"]
        lines += [f"- {_audit_json(entry)}" for entry in conflicts]
    if unsupported:
        lines += ["", "### unsupported[]"]
        lines += [f"- {_audit_json(entry)}" for entry in unsupported]
    if rows:
        lines += ["", f"### corrections[]（显示 {corrections.get('shown', len(rows))}/"
                  f"{corrections.get('total', len(rows))}，cap={corrections.get('cap', '?')}）"]
        lines += [f"- {_audit_json(row)}" for row in rows]
    elif not (conflicts or unsupported):
        lines += ["", "- `corrections.json` 存在，但 audit 为空。"]

    lines += [
        "",
        "### count summary",
        f"- sidecar: `{path}` ({status})",
        "- counts: "
        f"corrections={counts.get('corrections', 0)}, "
        f"conflicts={counts.get('conflicts', 0)}, "
        f"unsupported={counts.get('unsupported', 0)}",
        f"- by_rule_id: {_audit_json(summary.get('by_rule_id', {}))}",
        f"- by_stage: {_audit_json(summary.get('by_stage', {}))}",
    ]
    return lines


def _render_evidence_index(index: list[dict]) -> list[str]:
    lines = ["", "### evidence_index"]
    if not index:
        return lines + ["", "- （empty）"]
    for entry in index:
        payload = entry.get("payload", {})
        if entry.get("kind") == "judge":
            criterion = payload.get("criterion", {})
            desc = criterion.get("criterion") if isinstance(criterion, dict) else ""
        elif entry.get("kind") == "gate":
            desc = payload.get("message", "")
        elif entry.get("kind") == "evidence_debt":
            row = payload.get("row", {})
            desc = row.get("check_id") if isinstance(row, dict) else ""
        elif entry.get("kind") == "correction":
            desc = payload.get("raw_id") or f"row {payload.get('ordinal')}"
        else:
            desc = entry.get("source", "")
        lines.append(
            f"- `{entry.get('id')}` ({entry.get('kind')}; source `{entry.get('source')}`)"
            + (f" — {desc}" if desc else "")
        )
    return lines


def _render_report_assets(b: dict) -> list[str]:
    lines = ["", "## report/eyeball assets"]
    assets = b.get("report_assets", {})
    copied = assets.get("assets", [])
    missing = assets.get("missing", [])
    if copied:
        lines += ["", "### copied"]
        lines += [
            f"- `{a['path']}` <= `{a['source']}` ({a['producer']})"
            for a in copied
        ]
    if missing:
        lines += ["", "### missing producers"]
        lines += [
            f"- `{m['source']}` ({m['producer']})"
            for m in missing
        ]
    viewer = b.get("viewer", {})
    lines += ["", "### 3D viewer"]
    if viewer.get("available"):
        lines.append(f"- `{viewer['path']}` ({viewer['status']})")
    else:
        lines.append(f"- unavailable: {viewer.get('reason', 'unknown')}")
    return lines


def _render_run_state(b: dict) -> list[str]:
    state = b.get("run_state", {})
    lines = ["", "### run_state", "", f"- status: `{state.get('status', 'unknown')}`"]
    if state.get("completed_clean"):
        lines.append("- completed_clean: true")
    if state.get("root_stop"):
        root = state["root_stop"]
        lines.append(f"- root_stop: `{root.get('status')}@{root.get('stage')}`")
        if root.get("message"):
            lines.append(f"- root message: {root['message']}")
    if state.get("pending"):
        pending = state["pending"]
        lines.append(f"- pending: `{pending.get('status')}@{pending.get('stage')}`")
        if pending.get("message"):
            lines.append(f"- pending message: {pending['message']}")
    if state.get("ignored_pending"):
        ignored = [
            f"{x.get('status')}@{x.get('stage')}" for x in state.get("ignored_pending", [])
        ]
        lines.append(f"- ignored_pending: {ignored}")
    if state.get("missing_expected"):
        lines.append(f"- missing_expected: {state['missing_expected']}")
    return lines


def _downstream_missing_after_root(b: dict) -> list[dict]:
    run_state = b.get("run_state", {})
    root = run_state.get("root_stop") or {}
    root_stage = root.get("stage")
    if root_stage not in STAGE_ORDER:
        return []
    root_index = STAGE_ORDER.index(root_stage)
    out = []
    for item in b.get("blocking", []):
        stage = item.get("stage")
        if stage in STAGE_ORDER and STAGE_ORDER.index(stage) > root_index:
            msg = item.get("message", "")
            if "required artifact missing" in msg:
                out.append(item)
    return out


def _render_corrections_audit_summary(summary: dict) -> list[str]:
    lines = ["", "### 校正审计摘要"]
    path = summary.get("sidecar_path", "1_correction/corrections.json")
    status = summary.get("parse_status", "unknown")
    if not summary.get("present"):
        return lines + ["", f"- sidecar: `{path}` (missing)"]
    if status != "ok":
        msg = summary.get("parse_error", "")
        return lines + [
            "",
            f"- sidecar: `{path}` ({status})" + (f" — {msg}" if msg else ""),
        ]
    counts = summary.get("counts", {})
    return lines + [
        "",
        f"- sidecar: `{path}` ({status})",
        "- counts: "
        f"corrections={counts.get('corrections', 0)}, "
        f"conflicts={counts.get('conflicts', 0)}, "
        f"unsupported={counts.get('unsupported', 0)}",
        f"- by_rule_id: {_audit_json(summary.get('by_rule_id', {}))}",
        f"- by_stage: {_audit_json(summary.get('by_stage', {}))}",
    ]


def _render_annotation_basis_summary(summary: dict) -> list[str]:
    """2026-08-12 (摊 C): render the "which annotation basis" pure
    observation into the human-facing report — one line + one interpretation
    rule, so a future run whose facades are axis-line-dimensioned (rather
    than this batch's outer-skin convention) is visible on sight instead of
    silently folding into the same 4.8%-area drift this batch registered but
    never surfaced. Pure reporting: never blocks, never affects `verdict`/
    `blocking`/`flags` elsewhere in this report."""
    lines = ["", "### 标注法观测（纯观测,不阻断,见 plan.md〇-C）"]
    path = summary.get("sidecar_path", "1_correction/annotation_basis.json")
    if not summary.get("present"):
        return lines + ["", f"- sidecar: `{path}` (missing — legacy v1/v2 run, or no accepted facade-envelope axis)"]
    if summary.get("parse_status") != "ok":
        msg = summary.get("parse_error", "")
        return lines + [
            "",
            f"- sidecar: `{path}` ({summary.get('parse_status')})" + (f" — {msg}" if msg else ""),
        ]
    return lines + [
        "",
        f"- sidecar: `{path}` (ok)",
        f"- {summary.get('summary_line', '（无 summary_line）')}",
        f"- 解读规则: {summary.get('interpretation_rule', '（无 interpretation_rule）')}",
    ]


def _render_facts_card(b: dict) -> str:
    g = b["geometry"]
    ep = b["ep"]
    stop = b.get("stop_reason")
    run_status = b.get("run_state", {}).get("status")
    signals = b.get("signals", {})
    if run_status == "root_stopped":
        root = b.get("run_state", {}).get("root_stop") or {}
        verdict = f"❌ STOPPED ({root.get('status')}@{root.get('stage')})"
    elif run_status == "pending":
        pending = b.get("run_state", {}).get("pending") or {}
        verdict = f"⏸ PENDING ({pending.get('status')}@{pending.get('stage')})"
    elif b["blocked"] or stop:
        verdict = f"❌ STOPPED ({stop})" if stop else "❌ BLOCKED"
    elif signals.get("reading_evidence_clean") is False:
        verdict = "⚠️ reading evidence debt"
    elif run_status == "completed_clean":
        verdict = "✅ clean"
    else:
        verdict = f"⚠️ {run_status or 'state_unknown'}"
    lines = [
        "## 事实卡",
        "",
        f"**结论**: {verdict}"
        + (f" / EP {'Completed' if ep and ep['completed'] else 'NOT completed'}, "
           f"{ep['severe']} severe, {ep['warnings']} warn" if ep else " / EP 未跑")
        + (f" / {g.get('zones','?')}区·{g.get('surfaces','?')}面·{g.get('windows','?')}窗"
           if g else ""),
        "",
        f"**数字权威**: [../{RUN_META_DIR}/{BASELINE_NAME}](../{RUN_META_DIR}/{BASELINE_NAME}) "
        "+ 本 REPORT 的 GEN 区；AGENT 区为主控叙事/建议，citation linter 只约束建议证据 id。",
        "",
        "### 逐段 gate①",
        "",
        "| 段 | pass | flag | block | n/a |",
        "|---|---|---|---|---|",
    ]
    for stage, agg in b["gates"].items():
        lines.append(f"| {stage} | {agg['pass']} | {agg['flag']} | {agg['block']} | {agg['na']} |")
    lines += [
        "",
        "### 证据信号",
        "",
        "| signal | value |",
        "|---|---|",
    ]
    for key in (
        "reading_syntax_valid",
        "reading_evidence_clean",
        "j0_semantic_clean",
        "pipeline_recovered",
    ):
        lines.append(f"| {key} | `{signals.get(key)}` |")
    if b.get("orchestration"):
        lines += ["", "### 逐段编排状态（judge-in-the-loop）", "",
                  "| 段 | status | 抽样 |", "|---|---|---|"]
        for stage, st in b["orchestration"].items():
            lines.append(f"| {stage} | {st.get('status','?')} | {st.get('attempts_used','?')} |")
    if b["draws"]:
        lines += ["", f"**抽样次数（attempts/ 落盘）**: {b['draws']}"]
    if b["judge_verdicts"]:
        nblk = sum(1 for v in b["judge_verdicts"] if v.get("blocking"))
        lines += ["", f"**judge② verdicts**: {len(b['judge_verdicts'])} 条"
                  f"（{nblk} 条 blocking；见各 attempts/NNN/judge.json）"]
    lines += _render_run_state(b)
    if b["blocking"]:
        lines += ["", "### blocking"]
        lines += [f"- [{x['stage']}::{x['check']}] {x['message']}" for x in b["blocking"]]
    if b["flags"]:
        lines += ["", "### flags（不阻塞、供归因）"]
        lines += [f"- [{x['stage']}::{x['check']}] {x['message']}" for x in b["flags"]]
    consequential = _downstream_missing_after_root(b)
    if consequential:
        lines += ["", "### 连带缺失下游件", ""]
        lines += [f"- `{x.get('stage')}`: {x.get('message')}" for x in consequential]
    lines += _render_corrections_audit_summary(b.get("corrections_summary", {}))
    lines += _render_annotation_basis_summary(b.get("annotation_basis_summary", {}))
    return "\n".join(lines) + "\n"


def _render_eyeball_index(b: dict, eyeball: list[str]) -> str:
    lines = ["## 肉视检验索引", ""]
    lines += [f"{i+1}. {it}" for i, it in enumerate(eyeball)]
    assets = b.get("report_assets", {})
    viewer = b.get("viewer", {})
    lines += ["", "### report/eyeball"]
    if assets.get("assets"):
        lines += [
            f"- [{a['filename']}](eyeball/{a['filename']}) — {a['producer']} from `{a['source']}`"
            for a in assets["assets"]
        ]
    else:
        lines.append("- 2D eyeball assets unavailable; missing producers are listed above.")
    if assets.get("missing"):
        lines += ["", "### missing producers"]
        lines += [f"- `{m['source']}` ({m['producer']})" for m in assets["missing"]]
    lines += ["", "### manual_review viewer"]
    if viewer.get("available"):
        lines.append(f"- [3D geometry viewer]({viewer['report_link']}) — `{viewer['status']}`")
    else:
        lines.append(f"- 3D geometry viewer unavailable — {viewer.get('reason', 'unknown')}")
    return "\n".join(lines) + "\n"


def _render_appendix(b: dict) -> str:
    lines = [
        "## 附录指针",
        "",
        f"- numeric authority: [../{RUN_META_DIR}/{BASELINE_NAME}](../{RUN_META_DIR}/{BASELINE_NAME})",
        f"- run manifest: [../{RUN_META_DIR}/{MANIFEST_NAME}](../{RUN_META_DIR}/{MANIFEST_NAME})",
        "- validation summary: "
        f"[../{RUN_META_DIR}/{VALIDATION_MANIFEST_NAME}](../{RUN_META_DIR}/{VALIDATION_MANIFEST_NAME})",
        f"- geometry approval: [../{RUN_META_DIR}/{APPROVAL_NAME}](../{RUN_META_DIR}/{APPROVAL_NAME})",
        f"- orchestration ledger: [../{RUN_META_DIR}/{STATE_NAME}](../{RUN_META_DIR}/{STATE_NAME})",
        "- raw reading outputs: [../0_reading/](../0_reading/)",
        "- reading evidence debt: [../1_correction/evidence_debt.json](../1_correction/evidence_debt.json)",
        "- correction audit: [../1_correction/corrections.json](../1_correction/corrections.json)",
        "- judge verdict log: [../verdicts/](../verdicts/)",
        f"- geometry_digest: `{b['geometry_digest']}`",
    ]
    lines += _render_evidence_index(b.get("evidence_index", []))
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("case", help="case name under <base-dir>/")
    ap.add_argument("run", help="run folder name under <case>/ (e.g. run_2026-06-16_opus)")
    ap.add_argument("--base-dir", default="case_tests/e2e_tests")
    ap.add_argument("--date", required=True, help="ISO date of the run (no Date.now in tooling)")
    ap.add_argument("--orchestrator", required=True, help="main Agent model id, e.g. opus-4.8")
    ap.add_argument("--require-ep", action="store_true")
    ap.add_argument(
        "--run-profile",
        choices=("exploratory", "dev", "golden", "regression"),
        default="exploratory",
        help="evidence gate policy: exploratory/dev flag, golden/regression block",
    )
    ap.add_argument("--force-template", action="store_true",
                    help="overwrite report/REPORT.md from the generated skeleton")
    args = ap.parse_args()
    run_dir = Path(args.base_dir) / args.case / args.run
    b = record_baseline(run_dir, date=args.date, orchestrator=args.orchestrator,
                        require_ep=args.require_ep, run_profile=args.run_profile,
                        force_template=args.force_template)
    print(
        f"wrote {run_meta_path(run_dir, BASELINE_NAME)} + {run_dir/'report'/'REPORT.md'}  "
        f"(blocked={b['blocked']}, run_state={b['run_state']['status']})"
    )


if __name__ == "__main__":
    main()
