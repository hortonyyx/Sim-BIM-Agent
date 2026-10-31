"""Manifest-first correction audit/evidence reader for reports."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReportableCorrectionAudit:
    path: Path
    payload: dict
    window_host_rows: tuple[dict, ...] = ()
    rejected_window_host_conflicts: tuple[dict, ...] = ()


def _rejected_host_conflicts(run_dir: Path, *, accepted_attempt: int) -> tuple[dict, ...]:
    """Read typed host conflicts only from blocking, non-accepted attempts."""
    from src.agent.correction.window_host import WindowHostConflictV1
    from src.validator.checks.schema import CheckReport

    attempts = run_dir / "1_correction" / "attempts"
    rows: list[dict] = []
    if not attempts.is_dir():
        return ()
    for attempt in sorted(path for path in attempts.iterdir() if path.is_dir()):
        try:
            attempt_index = int(attempt.name)
        except ValueError:
            continue
        if attempt_index == accepted_attempt:
            continue
        audit_path = attempt / "audit.json"
        checks_path = attempt / "checks.json"
        if not audit_path.is_file() or not checks_path.is_file():
            continue
        checks = CheckReport.model_validate_json(checks_path.read_bytes())
        if not checks.blocking():
            continue
        payload = json.loads(audit_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("rejected correction audit root must be an object")
        for raw in payload.get("conflicts", ()):
            if not isinstance(raw, dict) or raw.get("kind") != "window_host_conflict":
                continue
            conflict = WindowHostConflictV1.model_validate_json(
                json.dumps(raw, separators=(",", ":"), ensure_ascii=False)
            )
            rows.append({
                "attempt": attempt_index,
                "window_id": conflict.window_id,
                "reason_code": conflict.reason_code,
                "branch": conflict.branch,
                "failed_gate_id": conflict.failed_gate_id,
                "fallback_action": conflict.fallback_action,
                "upstream_error_code": conflict.upstream_error_code,
                "audit_path": audit_path.relative_to(run_dir).as_posix(),
            })
    return tuple(sorted(rows, key=lambda row: (row["attempt"], row["window_id"], row["reason_code"])))


def load_reportable_correction_audit(run_dir: Path) -> ReportableCorrectionAudit | None:
    """Load the accepted audit and, for B5, its verified evidence sidecar.

    Historical runs without a V2 accepted pointer retain the legacy stage-root
    fallback.  A V2/B5 identity failure is never downgraded to "unreadable".
    """
    from src.agent.execution.manifest import RunManifestV2, hash_file, load_run_manifest

    run_dir = Path(run_dir)
    manifest = load_run_manifest(run_dir)
    if isinstance(manifest, RunManifestV2):
        record = manifest.accepted("1_correction")
        if record is None:
            return None
        attempt = run_dir / "1_correction" / "attempts" / f"{record.accepted_attempt:03d}"
        audit_path = attempt / "audit.json"
        claimed = record.artifact_hashes.get("audit")
        if claimed is None or not audit_path.is_file() or hash_file(audit_path) != claimed:
            raise ValueError("manifest-accepted correction audit is missing or hash-mismatched")
        payload = json.loads(audit_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("manifest-accepted correction audit root must be an object")
        if record.artifact_contract in {
            "correction_b5_v1", "correction_b5_orientation_v1",
        }:
            from src.agent.correction.window_host import WindowHostsArtifactV1
            from src.agent.output_coordinates import load_verified_accepted_correction

            verified = load_verified_accepted_correction(run_dir=run_dir, manifest=manifest)
            assert verified.raw_window_hosts_bytes is not None
            hosts = WindowHostsArtifactV1.model_validate_json(verified.raw_window_hosts_bytes)
            resolution_by_id = {row.window_id: row for row in hosts.claims.resolutions}
            evidence_by_id = {row.window_id: row for row in hosts.evidence.decisions}
            audit_rows = {
                row.get("window_id"): row
                for row in payload.get("corrections", [])
                if isinstance(row, dict) and row.get("kind") == "window_host_resolution"
            }
            if set(audit_rows) != set(resolution_by_id):
                raise ValueError("accepted B5 audit/window-host window totality mismatch")
            report_rows = []
            for window_id in sorted(resolution_by_id):
                resolution = resolution_by_id[window_id]
                audit = audit_rows[window_id]
                if audit.get("resolution_sha256") != resolution.resolution_sha256:
                    raise ValueError("accepted B5 audit resolution hash mismatch")
                evidence = evidence_by_id[window_id]
                report_rows.append({
                    "window_id": window_id,
                    "branch": resolution.branch,
                    "clamped_span": resolution.clamped_span.model_dump(mode="json"),
                    "facade_segment_id": resolution.facade_segment_id,
                    "room_id": resolution.room_id,
                    "resolution_sha256": resolution.resolution_sha256,
                    "corroboration_status": evidence.corroboration_status,
                    "evidence_sha256": evidence.evidence_sha256,
                })
            return ReportableCorrectionAudit(
                path=audit_path,
                payload=payload,
                window_host_rows=tuple(report_rows),
                rejected_window_host_conflicts=_rejected_host_conflicts(
                    run_dir, accepted_attempt=record.accepted_attempt,
                ),
            )
        return ReportableCorrectionAudit(
            path=audit_path,
            payload=payload,
            rejected_window_host_conflicts=_rejected_host_conflicts(
                run_dir, accepted_attempt=record.accepted_attempt,
            ),
        )

    sidecar = run_dir / "1_correction" / "corrections.json"
    if not sidecar.is_file():
        return None
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("legacy correction audit root must be an object")
    return ReportableCorrectionAudit(path=sidecar, payload=payload)


__all__ = ["ReportableCorrectionAudit", "load_reportable_correction_audit"]
