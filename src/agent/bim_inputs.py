"""Shared admission and immutable input manifest for both BIM runners.

Only explicitly supplied inputs are copied. This module never loads GT or
credentials, starts a model, or imports the runner that calls it.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from PIL import Image as PILImage
from scripts.tool_scripts.bim_agent_inputs import freeze_building_input, freeze_plan_input

ROOT = Path(__file__).resolve().parents[2]


# One list for both runners; hashes identify the actual implementation used.
IMPLEMENTATION_FILES = (
    'src/agent/bim_inputs.py',
    'src/agent/runtime_mesh_evidence.py',
    'src/agent/runtime_mesh_workers.py',
    'scripts/tool_scripts/bim_agent_claims.py',
    'scripts/tool_scripts/bim_agent_facade_checks.py',
    'scripts/tool_scripts/bim_agent_feedback.py',
    'scripts/tool_scripts/bim_agent_budget.py',
    'src/agent/correction/schema.py',
    'src/agent/geometry/source_model.py',
    'src/agent/roles.py',
    'src/agent/data/room_types.json',
    'src/agent/geometry/source_naming.py',
    'scripts/tool_scripts/render_geometry_viewer.py',
    'scripts/tool_scripts/run_bim_agent.py',
    'scripts/tool_scripts/bim_agent_guidance.py',
    'scripts/tool_scripts/bim_agent_continuation.py',
    'src/agent/geometry/parametric_proposal.py',
    'scripts/tool_scripts/bim_agent_inputs.py',
    'src/agent/execution/bim_claims.py',
    'src/agent/execution/bim_claim_state.py',
    'src/agent/execution/bim_height_coverage.py',
    'src/agent/geometry/component_attributes.py',
    'src/agent/geometry/reader_wall_metadata.py',
    'scripts/tool_scripts/bim_agent_mesh.py',
    'scripts/tool_scripts/bim_agent_mesh_review.py',
    'scripts/tool_scripts/bim_agent_inference.py',
    'src/agent/geometry/mesh_observation.py',
    'src/agent/geometry/mesh_bim_frame.py',
    'src/agent/execution/source_proposal.py',
    'src/agent/geometry/proposal_edits.py',
    'src/agent/geometry/opening_review.py',
    'src/agent/geometry/bim_delivery.py',
    'src/agent/geometry/source_image_overlay.py',
    'src/agent/geometry/source_floor_selection.py',
    'src/agent/geometry/source_space_relations.py',
    'src/agent/geometry/source_elevation_view.py',
    'src/agent/geometry/source_elevation_overlay.py',
    'src/agent/geometry/source_plan_view.py',
    'src/agent/geometry/source_bim.py',
    'src/agent/geometry/source_schema.py',
    'src/agent/geometry/source_visibility.py',
    'src/agent/geometry/wall_reference.py',
    'src/agent/geometry/dimension_chain.py',
    'src/agent/geometry/facade_span_comparison.py',
    'src/agent/geometry/profile_observation_binding.py',
    'src/agent/geometry/plan_feedback.py',
    'src/agent/geometry/space_trace.py',
    'src/agent/geometry/plan_partition.py',
    'src/agent/geometry/plan_assembly.py',
    'src/agent/geometry/plan_revision.py',
    'src/agent/geometry/plan_wall_support.py',
    'src/agent/geometry/plan_drawing_differences.py',
    'src/agent/execution/bim_claim_facts.py',
    'src/agent/geometry/plan_draft_view.py',
    'src/agent/geometry/pixel_region.py',
    'src/agent/geometry/pixel_region_overview.py',
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_png(path: Path) -> list[int]:
    """Validate the actual PNG payload, returning its decoded image dimensions."""
    try:
        with PILImage.open(path) as picture:
            if picture.format != "PNG":
                raise ValueError("file content is not PNG")
            size = list(picture.size)
            picture.verify()
        # PNG verify checks structure/checksums; load also checks compressed pixels.
        with PILImage.open(path) as picture:
            picture.load()
        return size
    except (OSError, SyntaxError, ValueError, PILImage.DecompressionBombError) as error:
        raise ValueError(f"input image is not a valid readable PNG: {path.name}") from error


def admitted_png_paths(directory: Path) -> list[Path]:
    """Admit only direct PNG children; linked files cannot escape this directory."""
    directory = directory.resolve()
    if not directory.is_dir():
        raise ValueError("images input must be a directory containing PNG files")
    paths = sorted(directory.glob("*.png"))
    if not paths:
        raise ValueError("images input directory must contain at least one PNG file")
    for path in paths:
        if not path.resolve().is_relative_to(directory):
            raise ValueError("input image escapes its admitted case directory")
        if not path.is_file():
            raise ValueError(f"input PNG must be a regular file: {path.name}")
        validate_png(path)
    return paths


def prepare_bim_inputs(run: Path, *, images_path: Path | None, mesh_path: Path | None,
                       building_input_path: Path | None, scope: str,
                       image_kind: str | None, max_candidates: int,
                       floor_images: list[str] | None = None,
                       started_epoch: float | None = None, seconds: float | None = None,
                       provider: str = "runtime", continuation_rounds: int = 0,
                       review_detail_enabled: bool = False, exploratory_opus: bool = False,
                       seed_path: Path | None = None, resume_plan_path: Path | None = None,
                       plan_image: str | None = None) -> dict:
    """Prepare one BIM workspace; provider changes routing only, never input scope."""
    if seconds is not None and started_epoch is None:
        raise ValueError("seconds requires started_epoch")
    if seed_path and resume_plan_path:
        raise ValueError("--resume-plan and --resume-candidate are mutually exclusive")
    if resume_plan_path:
        if not images_path or not plan_image:
            raise ValueError("--resume-plan requires --images and --plan-image")
        if Path(plan_image).name != plan_image or not plan_image.endswith(".png"):
            raise ValueError("--plan-image must be an exact admitted PNG filename")
        if not (images_path / plan_image).is_file():
            raise ValueError(f"--plan-image {plan_image!r} is not present in --images")
        if not resume_plan_path.is_file():
            raise ValueError("--resume-plan must name an existing JSON file")
    elif plan_image:
        raise ValueError("--plan-image requires --resume-plan")
    image_paths = admitted_png_paths(images_path) if images_path is not None else []
    if not image_paths and mesh_path is None:
        raise ValueError("provide PNG drawings or a native --mesh GLB")
    run = run.resolve()
    run.mkdir(parents=True, exist_ok=False)
    (run/"images").mkdir()
    images = {}
    for path in image_paths:
        target = run/"images"/path.name
        shutil.copy2(path,target)
        size = validate_png(target)
        images[path.name] = {"size":size,"sha256":digest(target)}
    mesh_input = None
    if mesh_path is not None:
        from scripts.tool_scripts.bim_agent_mesh import freeze_mesh
        mesh_input = freeze_mesh(mesh_path, run)
    building_input = (freeze_building_input(building_input_path, run, images)
                      if building_input_path is not None else None)
    plan_recovery = (freeze_plan_input(resume_plan_path, run, images, plan_image)
                     if resume_plan_path is not None else None)
    generation_mode = ("saved_plan_recovery" if plan_recovery else
                       "saved_candidate_recovery" if seed_path else "original_images_agent_experiment")
    source_input_mode = ("original_images_with_building_declaration"
                         if building_input else "original_images_only")
    if mesh_input:
        source_input_mode = 'native_mesh_with_images' if images else 'native_mesh'
        if building_input:
            source_input_mode += '_with_building_declaration'
        if not seed_path and not plan_recovery:
            generation_mode = 'native_mesh_agent_experiment'
    image_kind = (image_kind or ("unknown" if mesh_path is not None else "drawings")
                  if images else None)
    if floor_images is not None and any(image not in images for image in floor_images):
        raise ValueError("floor_plan_images must use admitted input filenames")
    # File names are only an explicit, visible scope hint, never a hidden floor count.
    floor_scope_source = "explicit_input_filenames" if floor_images is not None else "input_filename_hint"
    if floor_images is None:
        floor_images = sorted(name for name in images if re.fullmatch(r"\d+f(?:_view)?\.png", name, re.I)) if image_kind == "drawings" else []
    manifest = {"images":images,"image_kind":image_kind,"scope":scope,"provider":provider,
                             "floor_plan_images": floor_images,
                             "floor_scope_source": floor_scope_source if floor_images else "not_declared",
                             "started_epoch": started_epoch,
                             "time_budget_seconds": seconds,
                             "max_candidates":max_candidates,
                             "continuation_rounds": continuation_rounds,
                             "review_detail_enabled": review_detail_enabled,
                             "input_mode": generation_mode,
                             "exploratory_opus": exploratory_opus,
                             "source_input_mode": source_input_mode,
                             "input_contents": {
                                 "original_png_images": {"included": bool(images), "count": len(images)},
                                 "building_declaration": {"included": bool(building_input)},
                                 "saved_generated_proposal": {"included": bool(seed_path)},
                                 "ground_truth_or_evaluation": {"included": False},
                             },
                             "deadline_epoch": (started_epoch + seconds
                                                if started_epoch is not None and seconds is not None else None),
                             "implementation_sha256": {name: digest(ROOT / name) for name in IMPLEMENTATION_FILES},
                             "only_input": (
                                 "authorized original PNG images, user scope, explicitly supplied building "
                                 "declaration, and optional saved generated proposal; no GT/evaluation"
                                 if building_input else
                                 "authorized original PNG images, user scope, and optional saved generated "
                                 "proposal; no building declaration and no GT/evaluation"
                             )}
    if image_kind == "drawings" and mesh_input is None:
        from src.agent.geometry.lite_bim_regularization import DEFAULT_LITE_GRID_M
        # This is a run policy, not a claim extracted from scope or model JSON.
        # Historical manifests without it retain their original behaviour.
        manifest["lite_bim_grid_step_m"] = DEFAULT_LITE_GRID_M
    if building_input:
        manifest["building_input"] = building_input
    if plan_recovery:
        manifest["plan_recovery"] = plan_recovery
        manifest["input_contents"]["saved_pixel_plan"] = {"included": True,
                                                         "status": "unverified_not_compiled"}
        manifest["only_input"] = (
            "authorized original PNG images, user scope, optional building declaration/mesh, "
            "and one unverified saved pixel-plan declaration; no old source BIM, report, GT or evaluation"
        )
    if mesh_input:
        manifest['mesh_input'] = mesh_input
        manifest['input_contents']['original_mesh'] = {'included': True, 'sha256': mesh_input['sha256']}
        manifest['only_input'] = ('Admitted original GLB and optional original PNGs, explicit building '
                                  'declaration, user scope and optional saved proposal. Mesh views are '
                                  'generated on demand internally; no GT/evaluation or preselected camera package.')
        if plan_recovery:
            manifest['only_input'] = (
                "admitted original GLB and PNGs, user scope, optional building declaration, "
                "and one unverified saved pixel-plan declaration; no old source BIM, report, GT or evaluation"
            )
    if seed_path:
        raw = (seed_path/"proposal.json").read_bytes()
        # Only the proposal is imported, never a report that might hold evaluation.
        proposal = json.loads(raw)
        manifest["seed"] = {"candidate": "seed", "proposal_sha256":hashlib.sha256(raw).hexdigest(),
                            "source":str(seed_path.resolve()), "mode":"previous_generated_proposal_recovery"}
        from src.agent.execution.source_proposal import export_source_proposal
        report = export_source_proposal(proposal, run/"seed", provenance=manifest["seed"])
        if not (run/"seed"/"source_model.json").exists():
            raise ValueError(f"seed cannot be materialized: {report.get('error')}")
        if "lite_bim_grid_step_m" in manifest:
            from scripts.tool_scripts.bim_agent_regularization import source_grid_report
            source = json.loads((run/"seed"/"source_model.json").read_bytes())
            grid = source_grid_report(source, manifest["lite_bim_grid_step_m"])
            grid_path = run/"seed"/"lite_grid.json"
            dump(grid_path, grid)
            if grid["violations"]:
                raise ValueError("saved candidate is off the new drawing run's 0.1 m Lite BIM grid; "
                                 "admission rejected without changing the seed. See seed/lite_grid.json")
            manifest["seed"]["lite_grid"] = {
                "file": "seed/lite_grid.json", "sha256": digest(grid_path),
                "status": grid["status"], "grid_step_m": grid["grid_step_m"],
            }
    dump(run/"inputs.json", manifest)
    return manifest
