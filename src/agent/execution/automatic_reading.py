"""One-draw Claude-subscription entrance for the isolated reading stage.

The stage itself remains the existing clean-room reader and merge gate.  This
module only makes its lifecycle callable by the source-BIM flow: it refuses to
overwrite a run, starts one explicitly selected subscription model, and records
the result in the run's controller-owned evidence directory.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agent.execution.isolation import (
    build_isolation_workspace,
    merge_isolated_output,
    reader_invocations_path,
    spawn_command,
)
from src.agent.execution.manifest import RunManifestV2, load_run_manifest


_STAGE = "0_reading"
_MODEL_IDS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _report_path(run_dir: Path) -> Path:
    path = run_dir / "_run" / "automatic_reading.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_report(run_dir: Path, report: dict[str, Any]) -> None:
    _report_path(run_dir).write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _result(
    run_dir: Path, *, status: str, model: str, persist: bool = True, **extra: Any
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": "automatic_reading_v1",
        "status": status,
        "requested_model": model,
        "started_at": _now(),
        **extra,
    }
    if persist:
        _write_report(run_dir, result)
    return result


def _last_invocation(staging_root: Path) -> dict[str, Any] | None:
    path = reader_invocations_path(staging_root)
    if not path.is_file():
        return None
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    return json.loads(lines[-1]) if lines else None


def _report_reference(run_dir: Path) -> str:
    return str(_report_path(run_dir))


def _existing_reading_state(run_dir: Path) -> tuple[str | None, Path | None]:
    """Return an accepted/retry-blocking state before starting any reader."""
    manifest = load_run_manifest(run_dir)
    if isinstance(manifest, RunManifestV2) and manifest.accepted(_STAGE) is not None:
        record = manifest.accepted(_STAGE)
        assert record is not None
        return "accepted", run_dir / _STAGE / "attempts" / f"{record.accepted_attempt:03d}"

    stage_dir = run_dir / _STAGE
    if stage_dir.exists() and any(stage_dir.iterdir()):
        return "unaccepted", None
    return None, None


def run_automatic_reading(
    case_dir: Path,
    run_dir: Path,
    *,
    model: str,
    timeout_seconds: int = 900,
) -> dict[str, Any]:
    """Run exactly one isolated subscription reading draw and merge its result.

    ``model`` is deliberately limited to the two approved product tiers.  A
    caller cannot silently pick a provider alias or an API model.  The function
    never retries: an existing accepted product is reused, while any non-empty
    unaccepted stage is returned as ``blocked`` for explicit operator action.
    """
    case_dir = Path(case_dir).resolve()
    run_dir = Path(run_dir).resolve()
    requested = str(model).strip().lower()
    resolved_model = _MODEL_IDS.get(requested)
    if resolved_model is None:
        return _result(
            run_dir,
            status="error",
            model=str(model),
            error="reading model must be exactly one of: haiku, sonnet",
        )
    if timeout_seconds <= 0:
        return _result(
            run_dir,
            status="error",
            model=requested,
            resolved_model=resolved_model,
            error="timeout_seconds must be positive",
        )

    existing, attempt_dir = _existing_reading_state(run_dir)
    if existing == "accepted":
        return _result(
            run_dir,
            status="reused",
            model=requested,
            persist=False,
            resolved_model=resolved_model,
            attempt_dir=str(attempt_dir),
            original_report=_report_reference(run_dir),
        )
    if existing == "unaccepted":
        return _result(
            run_dir,
            status="blocked",
            model=requested,
            persist=not _report_path(run_dir).exists(),
            resolved_model=resolved_model,
            error="0_reading already contains unaccepted artifacts; refusing an implicit retry",
            original_report=_report_reference(run_dir),
        )
    if _report_path(run_dir).exists():
        return _result(
            run_dir,
            status="blocked",
            model=requested,
            persist=False,
            resolved_model=resolved_model,
            error="a previous automatic-reading report exists; refusing an implicit second draw",
            original_report=_report_reference(run_dir),
        )

    report = _result(
        run_dir,
        status="started",
        model=requested,
        resolved_model=resolved_model,
        timeout_seconds=timeout_seconds,
    )
    try:
        workspace = build_isolation_workspace(
            case_dir,
            run_dir=run_dir,
            pilot_review_gate=False,
        )
        report["staging_root"] = str(workspace.staging_root)
        report["started_at"] = _now()
        _write_report(run_dir, report)
        spawn_command(
            workspace.staging_root,
            model=resolved_model,
            execute=True,
            timeout_seconds=timeout_seconds,
            subscription_only=True,
        )
        invocation = _last_invocation(workspace.staging_root)
        if invocation is None:
            raise RuntimeError("reader launcher produced no invocation audit record")
        redacted_argv = invocation.get("argv_redacted")
        report["launch_audit"] = {
            "invocation_path": str(reader_invocations_path(workspace.staging_root)),
            "argv_redacted": redacted_argv,
            "stdout_path": invocation.get("stdout_path"),
            "stderr_path": invocation.get("stderr_path"),
            "response_usage": invocation.get("response_usage"),
        }
        attempt_dir = merge_isolated_output(workspace.staging_root, run_dir, accept=True)
        manifest = load_run_manifest(run_dir)
        accepted = (
            isinstance(manifest, RunManifestV2)
            and manifest.accepted(_STAGE) is not None
            and manifest.accepted(_STAGE).accepted_attempt == int(attempt_dir.name)
        )
        report.update(
            status="accepted" if accepted else "blocked",
            attempt_dir=str(attempt_dir),
            finished_at=_now(),
        )
        if not accepted:
            report["error"] = "reading checks filed the attempt but blocked acceptance"
        _write_report(run_dir, report)
        return report
    except subprocess.TimeoutExpired as exc:
        if report.get("staging_root"):
            invocation = _last_invocation(Path(report["staging_root"]))
            if invocation is not None:
                report["launch_audit"] = {
                    "invocation_path": str(reader_invocations_path(Path(report["staging_root"]))),
                    "stdout_path": invocation.get("stdout_path"),
                    "stderr_path": invocation.get("stderr_path"),
                }
        report.update(
            status="error",
            timed_out=True,
            error=f"reader timed out after {timeout_seconds}s",
            stdout_tail=str(exc.output or "")[-2000:],
            stderr_tail=str(exc.stderr or "")[-2000:],
            finished_at=_now(),
        )
        _write_report(run_dir, report)
        return report
    except Exception as exc:  # The report is the operation's durable handoff.
        if report.get("staging_root"):
            invocation = _last_invocation(Path(report["staging_root"]))
            if invocation is not None:
                report["launch_audit"] = {
                    "invocation_path": str(reader_invocations_path(Path(report["staging_root"]))),
                    "stdout_path": invocation.get("stdout_path"),
                    "stderr_path": invocation.get("stderr_path"),
                    "response_is_error": invocation.get("response_is_error"),
                    "response_error": invocation.get("response_error"),
                    "response_subtype": invocation.get("response_subtype"),
                    "response_usage": invocation.get("response_usage"),
                }
        report.update(
            status="error",
            error=f"{type(exc).__name__}: {exc}",
            finished_at=_now(),
        )
        _write_report(run_dir, report)
        return report
