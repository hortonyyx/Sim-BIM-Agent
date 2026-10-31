"""Persist the solver-independent BIM output of an existing correction run.

The output is a new directory, never an overwrite of accepted stage artifacts.
This is the source generation exit; it does not grant a geometry approval or
execute any thermal/EP stage. Reading/correction can feed it independently.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

from src.agent.correction.parse import ensure_corrected_geometry
from src.agent.execution.manifest import hash_file, load_run_manifest
from src.agent.geometry.source_bim import build_source_bim, source_view_geometry


def export_source_bim(run_dir: Path, out_dir: Path, *, capability_profile: str, candidate_attempt: int | None = None,
                      enclosure_input_path: Path | None = None) -> dict:
    from src.agent.execution.validation_run import _resolve_correction_source
    from src.agent.output_coordinates import _verify_b5_bundle
    from scripts.tool_scripts.render_geometry_viewer import build_viewer_html

    run_dir, out_dir = Path(run_dir).resolve(), Path(out_dir).resolve()
    if out_dir == run_dir or out_dir in run_dir.parents:
        raise ValueError("source BIM output must not replace the input run or its parents")
    out_dir.mkdir(parents=True, exist_ok=False)
    report = {"mode": "existing_correction_rebuild", "source_run": str(run_dir),
              "candidate_attempt": candidate_attempt, "capability_profile": capability_profile,
              "model_calls": 0, "solver_calls": 0, "input_artifacts": {},
              "scope": "correction to independent source BIM; not a new image reading",
              "not_evaluated": ["original image cold start", "human confirmation", "editing", "EnergyPlus"]}
    try:
        manifest = load_run_manifest(run_dir)
        rec = manifest.accepted("1_correction") if manifest else None
        attempt = None
        if candidate_attempt is not None:
            if candidate_attempt < 1:
                raise ValueError("candidate attempt must be positive")
            attempt = run_dir / "1_correction/attempts" / f"{candidate_attempt:03d}"
            raw = (attempt / "output.json").read_bytes()
            geom = ensure_corrected_geometry(json.loads(raw))
            proof = None
            if str(geom.schema_version) == "3":
                proof = _verify_b5_bundle(raw_output_bytes=raw,
                    raw_feature_states_bytes=(attempt / "feature_states.json").read_bytes(),
                    raw_window_resolver_inputs_bytes=(attempt / "window_resolver_inputs.json").read_bytes(),
                    raw_window_hosts_bytes=(attempt / "window_hosts.json").read_bytes())
            report["input_authority"] = "explicit_candidate_preview"
            report["input_trust"] = "verified B5 candidate bundle" if proof else "explicit legacy candidate"
        else:
            snapped = run_dir / "1_correction/correction_geometry_snapped.json"
            if rec:
                attempt = run_dir / "1_correction/attempts" / f"{rec.accepted_attempt:03d}"
                snapped = attempt / "output.json"
            resolved = _resolve_correction_source(run_dir, snapped)
            if resolved.geom is None:
                raise ValueError(resolved.trust_message)
            geom, proof = resolved.geom, resolved.window_host_proof
            report["input_authority"] = "accepted_correction" if rec else "legacy_stage_root"
            report["input_trust"] = resolved.trust_message
        input_path = attempt / "output.json" if attempt else run_dir / "1_correction/correction_geometry_snapped.json"
        report["input_artifacts"][input_path.relative_to(run_dir).as_posix()] = hash_file(input_path)
        blocked_input = False
        if attempt:
            for name in ("checks.json", "judge.json", "feature_states.json", "window_resolver_inputs.json", "window_hosts.json"):
                path = attempt / name
                if not path.exists():
                    continue
                report["input_artifacts"][path.relative_to(run_dir).as_posix()] = hash_file(path)
                if name == "checks.json":
                    from src.validator.checks.schema import CheckReport
                    checks = CheckReport.model_validate_json(path.read_bytes())
                    report["input_checks"] = checks.model_dump(mode="json")
                    blocked_input |= not checks.passed
                elif name == "judge.json":
                    from src.agent.judge.verdict import StageVerdict
                    verdict = StageVerdict.model_validate_json(path.read_bytes())
                    report["input_judge"] = verdict.model_dump(mode="json")
                    blocked_input |= verdict.blocking
        enclosure = None
        if enclosure_input_path is not None:
            from hashlib import sha256
            enclosure_raw = Path(enclosure_input_path).read_bytes()
            (out_dir / "enclosure_input.json").write_bytes(enclosure_raw)
            report["enclosure_input"] = {"path": str(enclosure_input_path),
                                          "sha256": sha256(enclosure_raw).hexdigest(),
                                          "mode": "explicit_source_geometry_declaration"}
            enclosure = json.loads(enclosure_raw)
            if not isinstance(enclosure, dict):
                raise ValueError("enclosure input must be a source geometry declaration object")
        source = build_source_bim(geom, capability_profile=capability_profile, window_host_proof=proof,
                                  enclosure_declaration=enclosure)
        # Preserve a geometric candidate even when old input checks or explicit
        # observations block readiness. No accepted-attempt record is invented.
        report["source_validation"] = source["validation"]
        report["status"] = "severe" if blocked_input or source["validation"]["status"] == "severe" else "not_evaluated"
        report["source_geometry_ready"] = not blocked_input and source["validation"]["status"] in {"pass", "warning"}
        report["drawing_fidelity"] = "not_evaluated"
        report["counts"] = {k:len(source[k]) for k in ("spaces", "boundaries", "openings", "connections", "unbuilt_openings", "unsupported")}
        if source["schema_version"] == "source_bim_v3":
            regions = [r for b in source["boundaries"] for r in b.get("enclosure_regions", [])]
            report["counts"].update(virtual_boundaries=sum(b["kind"] == "virtual" for b in source["boundaries"]),
                                    open_regions=sum(r["condition"] == "open" for r in regions),
                                    unknown_regions=sum(r["condition"] == "unknown" for r in regions),
                                    enclosure_open_connections=len(source["source_enclosure"]["open_connections"]))
            report["enclosure_information_complete"] = not report["counts"]["unknown_regions"]
            report["not_evaluated"].append("EP adaptation of virtual/unknown enclosure boundaries")
        report["source_model_sha256"] = source["source_model_sha256"]
        (out_dir / "source_model.json").write_text(json.dumps(source, ensure_ascii=False, indent=2)+"\n")
        display = source_view_geometry(source)
        (out_dir / "display_geometry.json").write_text(json.dumps(display, ensure_ascii=False, indent=2)+"\n")
        title = "源 BIM：有未完成项" if not report["source_geometry_ready"] else "源 BIM：几何检查通过，图纸保真待评价"
        if report["counts"].get("unknown_regions"):
            title += "；含未知围护"
        viewer = build_viewer_html(display, title=title)
        banner = ('<aside style="position:fixed;bottom:12px;left:12px;z-index:30;background:white;padding:10px;max-width:55vw">'
                  + html.escape(title) + ' · <a href="report.json">质量与输入记录</a> · <a href="source_model.json">源模型</a>'
                  '<br>从已有校正产物重建；本次未重新读图、未作人工确认。</aside>')
        (out_dir / "viewer.html").write_text(viewer.replace("</body>", banner+"</body>"))
    except Exception as exc:
        report.update(status="error", source_geometry_ready=False, error=f"{type(exc).__name__}: {exc}")
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
    return report
