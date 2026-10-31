"""Export an agent-supplied legacy geometry proposal as an inspectable source BIM.

This entry point has no correction-run authority.  It stores the proposal as it
was supplied, validates/builds a fresh source BIM candidate, and leaves both
drawing fidelity and human confirmation explicitly unevaluated.
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
from pathlib import Path
from time import perf_counter

from src.agent.correction.parse import ensure_corrected_geometry
from src.agent.geometry.source_bim import build_source_bim, source_view_geometry
from src.agent.geometry.source_model import _digest
from src.agent.geometry.source_naming import public_names_for_display, public_reference_map, public_reference_text


_PROPOSAL_FIELDS = {"geometry", "assumptions", "unresolved", "enclosure_declaration",
                    "wall_references", "wall_dimensions", "mesh_frame", "component_attributes", "wall_readings"}


def _json_bytes(value: object, *, indent: int | None = None) -> bytes:
    """Serialize only ordinary JSON values, rejecting lossy custom encoders."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=indent, allow_nan=False)
    return (text + "\n").encode("utf-8")


def _validate_proposal(proposal: dict) -> tuple[dict, list[str], list[str], dict | None]:
    if not isinstance(proposal, dict):
        raise TypeError("proposal must be an object")
    unknown = set(proposal) - _PROPOSAL_FIELDS
    missing = {"geometry", "assumptions", "unresolved"} - set(proposal)
    if unknown or missing:
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            details.append("unknown " + ", ".join(sorted(unknown)))
        raise ValueError("proposal fields: " + "; ".join(details))
    if not isinstance(proposal["geometry"], dict):
        raise TypeError("proposal.geometry must be an object")
    # Legacy Window permits extra fields, but source generation always emits
    # entries from this collection as windows. Reject a conflicting declaration
    # before that can silently turn an explicitly identified door into glazing.
    windows = proposal["geometry"].get("windows", [])
    if isinstance(windows, list):
        conflicts = [
            f"windows[{index}] id={window.get('id')!r} kind={window['kind']!r}"
            for index, window in enumerate(windows)
            if isinstance(window, dict) and "kind" in window and window["kind"] != "window"
        ]
        if conflicts:
            raise ValueError(
                "geometry.windows only accepts windows; conflicting kinds: " + "; ".join(conflicts)
                + ". Put doors/passages in geometry.openings with kind, space_id, p1, p2 and z; "
                "use other_space_id=null for an exterior opening. Preserve the declared type "
                "and observed size when correcting the proposal."
            )
    for field in ("assumptions", "unresolved"):
        if not isinstance(proposal[field], list) or any(not isinstance(item, str) for item in proposal[field]):
            raise TypeError(f"proposal.{field} must be a list of strings")
    if "mesh_frame" in proposal:
        from src.agent.geometry.mesh_bim_frame import validate_mesh_frame
        validate_mesh_frame(proposal["mesh_frame"])
    enclosure = proposal.get("enclosure_declaration")
    if enclosure is not None and not isinstance(enclosure, dict):
        raise TypeError("proposal.enclosure_declaration must be an object")
    # Preserve the original proposal for audit; absent use never means office.
    from src.agent.roles import require_role
    geometry = copy.deepcopy(proposal["geometry"])
    for floor in geometry.get("floors", []):
        for cell in floor.get("cells", []):
            cell["role"] = require_role(cell.get("role"))
    return geometry, list(proposal["assumptions"]), list(proposal["unresolved"]), enclosure


def _html_list(rows: list[str], references: dict) -> str:
    if not rows:
        return "<li>无</li>"
    return "".join(f"<li>{html.escape(public_reference_text(row, references), quote=True)}</li>" for row in rows)


def build_source_viewer_html(display: dict, *, source_geometry_ready: bool,
                             assumptions: list[str], unresolved: list[str],
                             mesh_path: Path | None = None) -> str:
    """Display a saved source and its original caveats without rebuilding it."""
    from scripts.tool_scripts.render_geometry_viewer import build_viewer_html

    title = "源 BIM 候选：几何检查通过，原图保真待评价"
    if not source_geometry_ready:
        title = "源 BIM 候选：几何检查有严重问题，仍保留查看结果"
    viewer = build_viewer_html(display, title=title, mesh_path=mesh_path)
    references = public_reference_map(display["source_model"], public_names_for_display(display["source_model"]))
    banner = (
        '<aside style="position:fixed;bottom:12px;left:12px;z-index:30;background:white;padding:10px;'
        'max-width:55vw;max-height:45vh;overflow:auto">'
        f"<strong>{html.escape(title)}</strong><br>"
        "模型提交的几何方案；原图保真尚未核对，未经人工确认。"
        "<details><summary>假设</summary><ul>" + _html_list(assumptions, references) + "</ul></details>"
        "<details><summary>未解决项</summary><ul>" + _html_list(unresolved, references) + "</ul></details>"
        '<a href="report.json">质量与来源记录</a> · <a href="proposal.json">原始方案</a> · '
        '<a href="source_model.json">源模型</a></aside>'
    )
    return viewer.replace("</body>", banner + "</body>")


def _require_vertical_continuity(source: dict, policy: str | None) -> None:
    """Promote unresolved stack decisions only for the admitted run policy.

    This is a consistency requirement, not a claim that every shaft must be
    continuous. The declared geometry or evidenced separations resolve it.
    """
    if policy is None:
        return
    if policy != "explicit_stack_v1":
        raise ValueError(f"unknown vertical continuity policy: {policy!r}")
    from src.agent.geometry.source_vertical_continuity import bind_vertical_continuity_review
    source["vertical_continuity_policy"] = policy
    bind_vertical_continuity_review(source)
    source["validation"]["scope"] += "; explicit decisions for identified vertical stacks"


def compile_source_proposal(proposal: dict, *, provenance: dict | None = None,
                            vertical_continuity_policy: str | None = None) -> tuple[dict | None, dict]:
    """Compile with the ordinary source kernel, without files, images or approval.

    The exporter below uses this exact implementation. A returned source can
    contain severe findings; callers must inspect the report. This does not
    perform run admission, architectural adoption, visual review or delivery.
    """
    started = checkpoint = perf_counter()
    timings = {}
    def stage(name):
        nonlocal checkpoint
        now = perf_counter()
        timings[name] = now - checkpoint
        checkpoint = now
    source = None
    report: dict = {
        "mode": "agent_geometry_proposal",
        "status": "not_evaluated",
        "source_geometry_ready": False,
        "model_calls": 0,
        "solver_calls": 0,
        "scope": "agent-supplied geometry to independent source BIM; no correction run or solver stage",
        "drawing_fidelity": "not_evaluated",
        "human_confirmation": "not_evaluated",
        "source_geometry_self_consistency": "not_evaluated",
        "not_evaluated": [
            "original drawing fidelity and completeness",
            "human confirmation",
            "thermal properties and EnergyPlus adaptation",
        ],
    }
    try:
        raw_proposal = _json_bytes(proposal, indent=2)
        report["proposal_sha256"] = hashlib.sha256(raw_proposal).hexdigest()
        geometry, assumptions, unresolved, enclosure = _validate_proposal(proposal)
        if provenance is not None:
            if not isinstance(provenance, dict):
                raise TypeError("provenance must be an object")
            # Check serializability before retaining it in the report so a bad
            # provenance record cannot prevent error-report persistence.
            _json_bytes(provenance)
            report["provenance"] = provenance

        report["agent_assumptions"] = assumptions
        report["unresolved"] = unresolved

        geom = ensure_corrected_geometry(geometry)
        # Legacy Floor accepts extra fields without hydrating their types. Keep
        # explicitly supplied per-floor rings usable after JSON save/reload,
        # just as in the existing typed geometry entry point.
        from src.agent.correction.schema import FootprintRing
        for floor in geom.floors:
            footprint = getattr(floor, "footprint", None)
            if isinstance(footprint, dict):
                floor.footprint = FootprintRing.model_validate(footprint)
        # Check the values the existing schema actually passes to the kernel,
        # including legacy numeric strings; the original proposal stays intact.
        from src.agent.geometry.input_scale import check_geometry_scale
        check_geometry_scale(geom.model_dump(mode="json"))
        stage("proposal_admission_seconds")
        source = build_source_bim(
            geom,
            capability_profile="simple_polygon" if geom.schema_version == "source_polygon_v1" else "orthogonal_polygon",
            enclosure_declaration=enclosure,
            wall_readings=proposal.get('wall_readings'),
        )
        _require_vertical_continuity(source, vertical_continuity_policy)
        stage("source_kernel_seconds")
        if "wall_readings" in proposal:
            from src.agent.geometry.reader_wall_metadata import bind_wall_readings
            source.update(bind_wall_readings(source, proposal["wall_readings"]))
        if "component_attributes" in proposal:
            from src.agent.geometry.component_attributes import validate_component_attributes
            explicit = validate_component_attributes(source, proposal["component_attributes"])
            replaced = {bid for row in explicit for bid in row["boundary_ids"]}
            source["component_attributes"] = [row for row in source.get("component_attributes", [])
                                              if not replaced.intersection(row["boundary_ids"])] + explicit
        if "wall_references" in proposal or "wall_dimensions" in proposal:
            from src.agent.geometry.wall_reference import resolve_wall_references, convert_wall_dimensions
            walls = resolve_wall_references(source, proposal.get("wall_references", []))
            replaced = {bid for row in walls for bid in row["boundary_ids"]}
            walls = [row for row in source.get("wall_references", [])
                     if not replaced.intersection(row["boundary_ids"])] + walls
            if len({row["id"] for row in walls}) != len(walls):
                raise ValueError("duplicate wall reference id across reading and explicit evidence")
            source["wall_references"] = walls
            source["wall_dimension_report"] = convert_wall_dimensions(walls, proposal.get("wall_dimensions", []))
            report["wall_dimension_report"] = source["wall_dimension_report"]
        # The standalone BIM must retain the same caveats as its HTML/report.
        # Keep this adapter metadata out of the legacy kernel and its outputs.
        if "mesh_frame" in proposal:
            from src.agent.geometry.mesh_bim_frame import validate_mesh_frame
            source["mesh_frame"] = validate_mesh_frame(proposal["mesh_frame"])
            report["mesh_frame"] = source["mesh_frame"]
        source["assumptions"].extend(assumptions)
        source["generation"] = {
            "method": "agent_geometry_proposal",
            "proposal_sha256": report["proposal_sha256"],
            "provenance": provenance or {},
            "unresolved": unresolved,
            "notes": geom.notes,
            # This remains in the independently exported source record so a
            # removed opening's previous geometry/source basis is auditable.
            "corrections": geom.corrections,
        }
        source["source_model_sha256"] = _digest(
            {key: value for key, value in source.items() if key != "source_model_sha256"}
        )
        validation = source["validation"]
        report["source_geometry_self_consistency"] = {
            "status": validation["status"],
            "scope": validation["scope"],
            "findings": validation["findings"],
        }
        report["source_validation"] = validation
        if vertical_continuity_policy is not None:
            report["vertical_continuity_review"] = source["vertical_continuity_review"]
        report["status"] = "severe" if validation["status"] == "severe" else "not_evaluated"
        report["source_geometry_ready"] = validation["status"] in {"pass", "warning"}
        report["counts"] = {
            name: len(source[name])
            for name in ("spaces", "boundaries", "openings", "connections", "unbuilt_openings", "unsupported")
        }
        report["source_model_sha256"] = source["source_model_sha256"]

        stage("source_metadata_seconds")
    except Exception as exc:
        source = None
        report.update(status="error", source_geometry_ready=False, error=f"{type(exc).__name__}: {exc}")
    report["timings"] = {**timings, "compile_total_seconds": perf_counter() - started}
    return source, report


def export_source_proposal(proposal: dict, out_dir: Path, *, provenance: dict | None = None,
                           mesh_path: Path | None = None,
                           vertical_continuity_policy: str | None = None) -> dict:
    """Compile and save a new inspectable candidate; never claim visual approval."""
    started = perf_counter()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=False)
    source, report = compile_source_proposal(proposal, provenance=provenance,
        vertical_continuity_policy=vertical_continuity_policy)
    timings = report.pop("timings")
    try:
        # Failed but serializable declarations remain inspectable as before.
        (out_dir / "proposal.json").write_bytes(_json_bytes(proposal, indent=2))
        if source is not None:
            checkpoint = perf_counter()
            (out_dir / "source_model.json").write_bytes(_json_bytes(source, indent=2))
            timings["source_metadata_and_save_seconds"] = (
                timings["source_metadata_seconds"] + perf_counter() - checkpoint)
            checkpoint = perf_counter()
            display = source_view_geometry(source)
            (out_dir / "display_geometry.json").write_bytes(_json_bytes(display, indent=2))
            timings["display_geometry_seconds"] = perf_counter() - checkpoint
            checkpoint = perf_counter()
            viewer = build_source_viewer_html(display,
                source_geometry_ready=report["source_geometry_ready"],
                assumptions=report["agent_assumptions"], unresolved=report["unresolved"],
                mesh_path=mesh_path)
            (out_dir / "viewer.html").write_text(viewer, encoding="utf-8", newline="\n")
            timings["viewer_seconds"] = perf_counter() - checkpoint
    except Exception as exc:
        report.update(status="error", source_geometry_ready=False, error=f"{type(exc).__name__}: {exc}")
    report["timings"] = {**timings, "export_total_seconds": perf_counter() - started}
    (out_dir / "report.json").write_bytes(_json_bytes(report, indent=2))
    return report
