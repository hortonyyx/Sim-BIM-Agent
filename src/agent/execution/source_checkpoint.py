"""Stage 2 source-model review, independent of downstream serialization.

The displayed snapshot binds source identity, derived geometry and current
checks. A preview can exist without being eligible for approval. Historical
Stage 3 approval digests remain separate and cannot authorize this checkpoint.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.agent.execution.manifest import hash_file, hash_obj, load_run_manifest
from src.agent.execution.policy import RunPolicy
from src.agent.execution.run_meta import run_meta_path

REVIEW_NAME = "source_geometry_review.json"
SCHEMA = "source_geometry_checkpoint_v1"


def inspect_source_checkpoint(run_dir: Path, *, policy: RunPolicy, geometry_path: Path | None = None) -> dict:
    """Rebuild from trusted correction, check Stage 2 and retain preview failures."""
    from src.agent.execution.validation_run import _resolve_correction_source
    from src.agent.geometry import build_geometry
    from src.agent.geometry.source_model import _digest, materialize_source_model
    from src.agent.geometry.specs import building_geometry_dict
    from src.validator.checks.correction import check_correction
    from src.validator.checks.kernel import check_kernel
    from src.validator.checks.schema import CheckReport

    run_dir = Path(run_dir)
    problems, checks, records = [], {}, {}
    manifest = load_run_manifest(run_dir)
    for stage in ("1_correction", "2_modelling"):
        rec = manifest.accepted(stage) if manifest else None
        records[stage] = rec.model_dump(mode="json") if rec else None
        if rec is None:
            problems.append(f"{stage}: no accepted source version")
            continue
        attempt = run_dir / stage / "attempts" / f"{rec.accepted_attempt:03d}"
        output = attempt / "output.json"
        actual = hash_file(output)
        if actual != rec.output_hash:
            problems.append(f"{stage}: accepted output changed")
        if not rec.check_passed:
            problems.append(f"{stage}: manifest does not record passed checks")
        check_path = attempt / "checks.json"
        if check_path.exists():
            expected_checks = getattr(rec, "artifact_hashes", {}).get("checks")
            if expected_checks is not None and hash_file(check_path) != expected_checks:
                problems.append(f"{stage}: accepted checks changed")
            rep = CheckReport.model_validate_json(check_path.read_bytes())
            checks[f"{stage}:accepted"] = rep.model_dump(mode="json")
            if not rep.passed:
                problems.append(f"{stage}: accepted checks are blocking")
        else:
            problems.append(f"{stage}: accepted checks missing")
        judge_path = attempt / "judge.json"
        if judge_path.exists():
            from src.agent.judge.verdict import StageVerdict
            verdict = StageVerdict.model_validate_json(judge_path.read_bytes())
            checks[f"{stage}:judge"] = verdict.model_dump(mode="json")
            if verdict.blocking:
                problems.append(f"{stage}: judge has unresolved blocking findings")

    rec2 = manifest.accepted("2_modelling") if manifest else None
    path = geometry_path or (run_dir / "2_modelling/attempts" / f"{rec2.accepted_attempt:03d}" / "output.json"
                             if rec2 else run_dir / "2_modelling/building_geometry.json")
    data = json.loads(path.read_bytes())
    source = None
    source_path = run_dir / "2_modelling/source_model.json"
    if source_path.exists():
        candidate = json.loads(source_path.read_bytes())
        payload = {k: v for k, v in candidate.items() if k != "source_model_sha256"}
        if candidate.get("source_model_sha256") != _digest(payload) or candidate.get("derived_geometry_sha256") != _digest(data):
            problems.append("source model does not match the displayed geometry")
        else:
            source = candidate
    from src.agent.execution.manifest import RunManifestV2
    rec1 = manifest.accepted("1_correction") if manifest else None
    correction_path = run_dir / "1_correction/correction_geometry_snapped.json"
    if rec1 is not None and not isinstance(manifest, RunManifestV2):
        correction_path = run_dir / "1_correction/attempts" / f"{rec1.accepted_attempt:03d}" / "output.json"
    correction = _resolve_correction_source(run_dir, correction_path)
    if correction.geom is None:
        problems.append(correction.trust_message)
    else:
        crep = check_correction(correction.geom, capability_profile=policy.capability_profile,
                                run_profile=policy.run_profile, window_host_proof=correction.window_host_proof,
                                window_evidence=correction.window_evidence)
        checks["1_correction:current"] = crep.model_dump(mode="json")
        if not crep.passed:
            problems.append("current correction checks are blocking")
        bg = build_geometry(correction.geom, capability_profile=policy.capability_profile,
                            window_host_proof=correction.window_host_proof)
        rebuilt = building_geometry_dict(bg)
        if data != rebuilt:
            problems.append("displayed geometry differs from the source rebuild")
        rebuilt_source = materialize_source_model(correction.geom, bg)
        if source is not None and source != rebuilt_source:
            problems.append("source sidecar differs from the source rebuild")
        if data == rebuilt:
            source = rebuilt_source
        krep = check_kernel(bg, capability_profile=policy.capability_profile, run_profile=policy.run_profile,
                            window_host_proof=correction.window_host_proof)
        checks["2_modelling:current"] = krep.model_dump(mode="json")
        if not krep.passed:
            problems.append("current geometry checks are blocking")
    if source is None:
        problems.append("source model unavailable")
    elif source["validation"]["status"] != "pass":
        problems.append("source-to-derived mapping has severe findings")
    if source is not None and source.get("unsupported"):
        problems.append("source model contains unresolved unbuilt/unsupported observations")
    if rec2 is not None:
        accepted_data = json.loads((run_dir / "2_modelling/attempts" / f"{rec2.accepted_attempt:03d}" / "output.json").read_bytes())
        if data != accepted_data:
            problems.append("displayed candidate is not the accepted Stage 2 geometry")
    # The saved solver diagnostics also remain visible for unaccepted previews.
    gate = run_dir / "2_modelling/kernel_gate_report.json"
    if gate.exists():
        checks["saved_kernel_diagnostics"] = json.loads(gate.read_bytes())
    basis = {"schema": SCHEMA, "run_id": getattr(manifest, "run_id", None), "accepted_versions": records,
             "geometry_sha256": hash_obj(data), "source_model_sha256": hash_obj(source),
             "checks": checks, "problems": problems,
             "run_profile": policy.run_profile, "capability_profile": policy.capability_profile}
    return {"digest": hash_obj(basis), "basis": basis, "approval_ready": not problems,
            "geometry": data, "source_model": source}


def save_source_review(run_dir: Path, state: dict, viewer_html: str) -> Path:
    """Publish immutable review bytes plus a pointer; never approve as a side effect."""
    from src.agent.execution.manifest import hash_text

    run_dir = Path(run_dir)
    # Include the actual HTML bytes in the revision path so renderer changes do
    # not overwrite an older review of the same source/checkpoint.
    html_hash = hash_text(viewer_html)
    folder = run_dir / "manual_review" / f"{state['digest']}-{html_hash[:12]}"
    folder.mkdir(parents=True, exist_ok=True)
    outputs = {"geometry_viewer.html": viewer_html,
               "source_model.json": json.dumps(state["source_model"], ensure_ascii=False, indent=2),
               "building_geometry.json": json.dumps(state["geometry"], ensure_ascii=False, indent=2),
               "checkpoint.json": json.dumps(state["basis"], ensure_ascii=False, indent=2)}
    for name, content in outputs.items():
        path = folder / name
        if path.exists() and path.read_text() != content:
            raise ValueError("immutable source review changed")
        path.write_text(content, encoding="utf-8")
    record = {"schema": SCHEMA, "digest": state["digest"], "approval_ready": state["approval_ready"],
              "viewer": (folder / "geometry_viewer.html").relative_to(run_dir).as_posix(),
              "artifacts": {(folder / name).relative_to(run_dir).as_posix(): hash_file(folder / name) for name in outputs}}
    run_meta_path(run_dir, REVIEW_NAME, for_write=True).write_text(json.dumps(record, indent=2), encoding="utf-8")
    return folder / "geometry_viewer.html"


def current_source_review(run_dir: Path, *, policy: RunPolicy, expected_digest: str | None = None) -> dict | None:
    """Check displayed bytes and recompute against current authoritative inputs."""
    try:
        run_dir = Path(run_dir)
        record = json.loads(run_meta_path(run_dir, REVIEW_NAME).read_bytes())
        if record["schema"] != SCHEMA or (expected_digest is not None and record["digest"] != expected_digest):
            return None
        if not record["approval_ready"]:
            return None
        viewer = Path(record["viewer"])
        expected_files = {viewer.with_name(name).as_posix() for name in (
            "geometry_viewer.html", "source_model.json", "building_geometry.json", "checkpoint.json")}
        if set(record["artifacts"]) != expected_files or viewer.name != "geometry_viewer.html":
            return None
        for relative, digest in record["artifacts"].items():
            path = (run_dir / relative).resolve()
            if not path.is_relative_to((run_dir / "manual_review").resolve()) or hash_file(path) != digest:
                return None
        state = inspect_source_checkpoint(run_dir, policy=policy)
        if state["digest"] != record["digest"] or not state["approval_ready"]:
            return None
        folder = (run_dir / viewer).parent
        if (json.loads((folder / "checkpoint.json").read_bytes()) != state["basis"]
                or json.loads((folder / "source_model.json").read_bytes()) != state["source_model"]
                or json.loads((folder / "building_geometry.json").read_bytes()) != state["geometry"]):
            return None
        return record
    except (OSError, ValueError, KeyError, TypeError):
        return None
