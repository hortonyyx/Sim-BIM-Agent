"""Mesh scope uses the existing completion hook, with bound local evidence only."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from PIL import Image
import trimesh

from scripts.tool_scripts.bim_agent_budget import fallback_selection, saved_floor_status
from scripts.tool_scripts.run_bim_agent import Toolkit
from src.agent.execution.source_proposal import export_source_proposal
from src.agent.geometry.mesh_bim_frame import render_mesh_bim_overlay
from src.agent.runtime_delivery import completed_building_handoff, finalize_runtime_building
from tests.test_source_proposal import _proposal


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def mesh_run(tmp_path):
    run = tmp_path / "bim"
    asset = run / "assets/input.glb"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(trimesh.creation.box().export(file_type="glb"))
    _write(run / "inputs.json", {
        "images": {}, "image_kind": None, "floor_plan_images": [],
        "floor_scope_source": "not_declared", "max_candidates": 6,
        "mesh_input": {"frozen_path": "assets/input.glb", "sha256": _sha(asset)},
    })
    return run


def _candidate(run, index=1, coverage="whole_building"):
    from scripts.tool_scripts.bim_agent_mesh_review import carry_mesh_review

    candidate = f"candidate_{index:02d}"
    toolkit = Toolkit(run)
    proposal = _proposal()
    proposal["mesh_frame"] = {
        "mesh_sha256": toolkit.manifest["mesh_input"]["sha256"],
        "yaw_degrees": 0, "translation_m": [0, 0, 0],
        "reason": "synthetic unit fixture", "source_refs": ["synthetic mesh"],
    }
    report = export_source_proposal(proposal, run / candidate)
    assert report["source_geometry_ready"]
    source_path = run / candidate / "source_model.json"
    source = json.loads(source_path.read_bytes())
    observation_id, overlay_id = f"mesh_{index:03d}", f"overlay_{index:03d}"
    observation_path = run / "mesh_observations" / observation_id
    observation = {
        "mesh_sha256": toolkit.manifest["mesh_input"]["sha256"],
        "resolution_px": {"width": 40, "height": 40},
        "source_coordinate_transform": {"yaw_degrees_counterclockwise_about_positive_z": 0},
        "camera": {"target": [3, 3, 1.5], "screen_right": [1, 0, 0], "screen_up": [0, 0, 1]},
        "view_span_m": {"width": 8, "height": 5},
    }
    _write(observation_path.with_suffix(".json"), observation)
    picture = Image.new("RGB", (40, 40), "white")
    picture.save(observation_path.with_suffix(".png"))
    overlay, record = render_mesh_bim_overlay(picture, observation, source, visibility="xray")
    overlay_path = run / "mesh_overlays" / overlay_id
    overlay_path.parent.mkdir(exist_ok=True)
    overlay.save(overlay_path.with_suffix(".png"))
    record.update(
        candidate=candidate, observation=observation_id, overlay_id=overlay_id,
        image_file=f"mesh_overlays/{overlay_id}.png",
        image_sha256=_sha(overlay_path.with_suffix(".png")),
        observation_metadata_sha256=_sha(observation_path.with_suffix(".json")),
        observation_image_sha256=_sha(observation_path.with_suffix(".png")),
        source_file_sha256=_sha(source_path),
    )
    _write(overlay_path.with_suffix(".json"), record)
    if coverage is not None:
        carry_mesh_review(toolkit, candidate, json.dumps({"mesh_review": {
            "coverage": coverage, "summary": "Synthetic scope declaration; fidelity not evaluated.",
            "overlay_ids": [overlay_id],
        }}))
    return candidate


def _handoff(run, candidate):
    toolkit = Toolkit(run)
    delivery = toolkit.delivery(candidate, selection_origin="agent_selected")
    _write(run / "delivery_selection.json", {
        "candidate": candidate, "source_model_sha256": delivery["source_model_sha256"],
    })
    names = {"inputs.json", "delivery_selection.json", "delivery.json", "delivery.html",
             f"{candidate}/source_model.json", f"{candidate}/viewer.html"}
    if (run / candidate / "mesh_review.json").is_file():
        names.add(f"{candidate}/mesh_review.json")
    result = {**delivery, "delivery_artifact_sha256": {name: _sha(run / name) for name in names}}
    raw = {"structuredContent": result}
    event = SimpleNamespace(event_id="finish-1", payload=SimpleNamespace(
        event_type="tool_execution", tool_name="finish_bim", outcome="succeeded",
        repeatability="non_idempotent_write", raw_result=raw,
        full_arguments={"candidate": candidate},
    ))
    engine = SimpleNamespace(
        role=SimpleNamespace(read_only=False), tools=SimpleNamespace(run_directory=run),
        store=SimpleNamespace(events=[event], resolve=lambda value: value),
        domain_completion=completed_building_handoff, elapsed_seconds=lambda: 1.0,
    )
    return engine, result


def test_mesh_whole_scope_completes_without_claiming_drawing_floor_coverage(mesh_run):
    candidate = _candidate(mesh_run)
    engine, result = _handoff(mesh_run, candidate)
    assert result["floor_completeness"]["complete_building"] is None
    completion = completed_building_handoff(engine)
    assert completion is not None
    assert completion["scope_basis"] == "mesh_model_reported_review"
    assert completion["mesh_review"]["fidelity"] == "not_evaluated"
    assert f"{candidate}/mesh_review.json" in completion["artifact_sha256"]
    engine.completion = completion
    finalized = finalize_runtime_building(engine, "completed")
    assert finalized["reused_handoff"] and finalized["completion_valid"]
    assert finalized["delivery"]["complete_building"] is True
    assert finalized["delivery"]["scope_basis"] == "mesh_model_reported_review"
    assert all(_sha(mesh_run / name) == digest for name, digest in completion["artifact_sha256"].items())


@pytest.mark.parametrize("coverage", [None, "partial"])
def test_missing_or_partial_mesh_review_does_not_complete(mesh_run, coverage):
    engine, _ = _handoff(mesh_run, _candidate(mesh_run, coverage=coverage))
    assert completed_building_handoff(engine) is None


@pytest.mark.parametrize("relative", [
    "candidate_01/mesh_review.json", "mesh_overlays/overlay_001.png",
    "mesh_observations/mesh_001.png", "assets/input.glb",
])
def test_changed_mesh_review_or_bound_evidence_invalidates_handoff(mesh_run, relative):
    engine, _ = _handoff(mesh_run, _candidate(mesh_run))
    engine.completion = completed_building_handoff(engine)
    assert engine.completion is not None
    path = mesh_run / relative
    path.write_bytes(path.read_bytes() + b"changed")
    assert completed_building_handoff(engine) is None
    assert finalize_runtime_building(engine, "completed")["completion_valid"] is False


def test_bound_delivery_cannot_replace_the_current_mesh_review_or_geometry_check(mesh_run):
    engine, result = _handoff(mesh_run, _candidate(mesh_run))
    path = mesh_run / "delivery.json"
    delivery = json.loads(path.read_bytes())
    delivery["mesh_review"]["summary"] = "A different declaration, not the saved review."
    _write(path, delivery)
    result["delivery_artifact_sha256"]["delivery.json"] = _sha(path)
    assert completed_building_handoff(engine) is None
    delivery["mesh_review"] = result["mesh_review"]
    delivery["source_validation"]["status"] = "fail"
    _write(path, delivery)
    result["delivery_artifact_sha256"]["delivery.json"] = _sha(path)
    assert completed_building_handoff(engine) is None


def test_mesh_fallback_prefers_current_whole_review_and_leaves_floor_semantics_alone(mesh_run):
    whole = _candidate(mesh_run)
    partial = _candidate(mesh_run, 2, coverage="partial")
    toolkit = Toolkit(mesh_run)
    assert saved_floor_status(toolkit, whole)["complete_building"] is None
    assert fallback_selection(toolkit) == (whole, "latest_complete_mesh_review_fallback_not_agent_selected")
    evidence = mesh_run / "mesh_overlays/overlay_001.png"
    evidence.write_bytes(evidence.read_bytes() + b"changed")
    assert fallback_selection(toolkit) == (partial, "latest_saved_fallback_not_agent_selected")
