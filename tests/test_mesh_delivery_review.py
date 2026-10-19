"""Offline mesh handoff declarations bind saved source and actual overlay evidence."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest
import trimesh

from scripts.tool_scripts.bim_agent_mesh_review import (
    carry_mesh_review, mesh_review_status, validate_mesh_review,
)
from src.agent.execution.source_proposal import export_source_proposal
from src.agent.geometry.mesh_bim_frame import render_mesh_bim_overlay
from src.agent.geometry.mesh_observation import MeshObservation


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture
def prepared(tmp_path):
    run = tmp_path / "run"
    (run / "assets").mkdir(parents=True)
    mesh = trimesh.creation.box(extents=[4, 3, 6])
    mesh.visual = trimesh.visual.TextureVisuals(uv=np.zeros((len(mesh.vertices), 2)),
        image=Image.new("RGB", (4, 4), "gray"))
    asset = run / "assets/input.glb"
    asset.write_bytes(trimesh.Scene(mesh).export(file_type="glb"))
    manifest = {"mesh_input": {"frozen_path": "assets/input.glb", "sha256": _sha(asset)},
                "images": {}, "scope": "Synthetic review contract; no fidelity assertion"}
    _write(run / "inputs.json", manifest)
    candidate = "candidate_01"
    proposal = {"geometry": {"schema_version": "2", "footprint_x": [0, 4], "footprint_y": [0, 6],
        "floors": [{"name": "F1", "z_floor": 0, "ceiling_height": 3,
                    "cells": [{"id": "room", "role": "office", "x": [0, 4], "y": [0, 6]}]}],
        "windows": [], "openings": []}, "assumptions": ["test only"], "unresolved": [],
        "mesh_frame": {"mesh_sha256": _sha(asset), "yaw_degrees": 0,
                       "translation_m": [2, 3, 1.5], "reason": "synthetic frame", "source_refs": ["fixture"]}}
    report = export_source_proposal(proposal, run / candidate)
    assert report["source_geometry_ready"]
    source_path = run / candidate / "source_model.json"
    source = json.loads(source_path.read_bytes())
    (run / "mesh_observations").mkdir()
    prefix = run / "mesh_observations/mesh_001"
    image, metadata = MeshObservation(asset).render(prefix, eye=[10, 10, 10], target=[0, 0, 0],
        width_m=10, height_m=10, width_px=24, height_px=24)
    overlay, record = render_mesh_bim_overlay(image, metadata, source)
    (run / "mesh_overlays").mkdir()
    image_path = run / "mesh_overlays/overlay_001.png"
    overlay.save(image_path)
    record.update(candidate=candidate, observation="mesh_001", overlay_id="overlay_001",
        image_file="mesh_overlays/overlay_001.png", image_sha256=_sha(image_path),
        observation_metadata_sha256=_sha(prefix.with_suffix(".json")),
        observation_image_sha256=_sha(prefix.with_suffix(".png")), source_file_sha256=_sha(source_path))
    _write(run / "mesh_overlays/overlay_001.json", record)
    return SimpleNamespace(run=run, manifest=manifest), candidate


def _review(coverage="whole_building", **updates):
    return json.dumps({"mesh_review": {"coverage": coverage, "summary": "Reviewed current exterior and inferred interior.",
                                       "overlay_ids": ["overlay_001"], **updates}})


def _admit_auxiliary_inputs(toolkit):
    from scripts.tool_scripts.bim_agent_inputs import freeze_building_input, freeze_plan_input

    (toolkit.run / "images").mkdir()
    image = toolkit.run / "images/sketch.png"
    Image.new("RGB", (12, 8), "white").save(image)
    toolkit.manifest["images"] = {"sketch.png": {"sha256": _sha(image), "size": [12, 8]}}
    for field, source_name, freeze in (
        ("building_input", "user.json", freeze_building_input),
        ("plan_recovery", "saved_plan.json", freeze_plan_input),
    ):
        source = toolkit.run.parent / source_name
        _write(source, {"path": "private/never_opened.png", "notes": "relationship sketch only"})
        toolkit.manifest[field] = freeze(source, toolkit.run, toolkit.manifest["images"],
            *( ["sketch.png"] if field == "plan_recovery" else [] ))
    _write(toolkit.run / "inputs.json", toolkit.manifest)


def test_missing_review_never_claims_completion(prepared):
    toolkit, candidate = prepared
    status = mesh_review_status(toolkit, candidate)
    assert status["status"] == "missing" and not status["complete_building"]
    assert validate_mesh_review(toolkit.run, candidate, {}, toolkit.manifest)["status"] == "missing"
    assert not (toolkit.run / candidate / "mesh_review.json").exists()


@pytest.mark.parametrize("coverage,complete", [("partial", False), ("whole_building", True)])
def test_current_declaration_and_every_evidence_hash_round_trip_read_only(prepared, coverage, complete):
    toolkit, candidate = prepared
    status = carry_mesh_review(toolkit, candidate, _review(coverage))
    assert status["status"] == "current" and status["complete_building"] is complete
    assert status["coverage"] == coverage and status["fidelity"] == "not_evaluated"
    path = toolkit.run / candidate / "mesh_review.json"
    record = json.loads(path.read_bytes())
    assert status["record_sha256"] == _sha(path)
    assert set(record["evidence"]["file_sha256"]) == {
        "inputs.json", f"{candidate}/source_model.json", f"{candidate}/report.json", "assets/input.glb",
        "mesh_overlays/overlay_001.json", "mesh_overlays/overlay_001.png",
        "mesh_observations/mesh_001.json", "mesh_observations/mesh_001.png"}
    before = {name: _sha(toolkit.run / name) for name in record["evidence"]["file_sha256"]}
    source = json.loads((toolkit.run / candidate / "source_model.json").read_bytes())
    assert validate_mesh_review(toolkit.run, candidate, source, toolkit.manifest, record) == status
    assert mesh_review_status(toolkit, candidate) == status
    assert before == {name: _sha(toolkit.run / name) for name in before}
    assert carry_mesh_review(toolkit, candidate, "") is None
    assert carry_mesh_review(toolkit, candidate, "[]") is None
    assert _sha(path) == status["record_sha256"]


@pytest.mark.parametrize("relative", ["candidate_01/source_model.json", "candidate_01/report.json",
    "mesh_overlays/overlay_001.json", "mesh_overlays/overlay_001.png", "mesh_observations/mesh_001.json",
    "mesh_observations/mesh_001.png", "assets/input.glb", "inputs.json", "candidate_01/mesh_review.json"])
def test_any_changed_bound_file_invalidates_saved_review(prepared, relative):
    toolkit, candidate = prepared
    carry_mesh_review(toolkit, candidate, _review())
    path = toolkit.run / relative
    if path.suffix == ".json":
        value = json.loads(path.read_bytes())
        value["changed_after_review"] = True
        _write(path, value)
    else:
        path.write_bytes(path.read_bytes() + b"changed")
    status = mesh_review_status(toolkit, candidate)
    assert status["status"] == "stale" and not status["complete_building"]
    assert status["fidelity"] == "not_evaluated"


def test_auxiliary_input_change_and_candidate_revision_cannot_reuse_review(prepared):
    toolkit, candidate = prepared
    carry_mesh_review(toolkit, candidate, _review())
    toolkit.manifest["building_input"] = {"declaration": {"use": "different"}}
    _write(toolkit.run / "inputs.json", toolkit.manifest)
    assert mesh_review_status(toolkit, candidate)["status"] == "stale"
    target = toolkit.run / "candidate_02"
    target.mkdir()
    for name in ("source_model.json", "report.json", "mesh_review.json"):
        (target / name).write_bytes((toolkit.run / candidate / name).read_bytes())
    assert mesh_review_status(toolkit, "candidate_02")["status"] == "stale"


@pytest.mark.parametrize("update", [{"summary": " "}, {"coverage": "all"}, {"overlay_ids": []},
    {"overlay_ids": ["overlay_001", "overlay_001"]}, {"overlay_ids": ["../outside"]}])
def test_invalid_model_declarations_are_rejected_without_saving(prepared, update):
    toolkit, candidate = prepared
    with pytest.raises(ValueError):
        carry_mesh_review(toolkit, candidate, _review(**update))
    assert mesh_review_status(toolkit, candidate)["status"] == "missing"


def test_drawing_runs_cannot_claim_a_mesh_review(prepared):
    toolkit, candidate = prepared
    toolkit.manifest.pop("mesh_input")
    with pytest.raises(ValueError, match="drawing-only"):
        carry_mesh_review(toolkit, candidate, _review())


@pytest.mark.parametrize("escape", ["candidate", "overlay_image", "observation_id", "linked_record"])
def test_evidence_paths_cannot_escape_the_run_or_admitted_filename(prepared, escape):
    toolkit, candidate = prepared
    overlay_path = toolkit.run / "mesh_overlays/overlay_001.json"
    if escape == "candidate":
        candidate = "../candidate_01"
    elif escape == "linked_record":
        outside = toolkit.run.parent / "outside.json"
        outside.write_bytes(overlay_path.read_bytes())
        overlay_path.unlink()
        overlay_path.symlink_to(outside)
    else:
        record = json.loads(overlay_path.read_bytes())
        record["image_file" if escape == "overlay_image" else "observation"] = "../outside"
        _write(overlay_path, record)
    with pytest.raises(ValueError):
        carry_mesh_review(toolkit, candidate, _review())


def test_unready_source_cannot_create_review(prepared):
    toolkit, candidate = prepared
    report_path = toolkit.run / candidate / "report.json"
    report = json.loads(report_path.read_bytes())
    report["source_geometry_ready"] = False
    _write(report_path, report)
    with pytest.raises(ValueError, match="ready source"):
        carry_mesh_review(toolkit, candidate, _review())


def test_overlay_with_different_frame_cannot_create_review(prepared):
    toolkit, candidate = prepared
    path = toolkit.run / "mesh_overlays/overlay_001.json"
    record = json.loads(path.read_bytes())
    record["mesh_frame"]["translation_m"] = [0, 0, 0]
    _write(path, record)
    with pytest.raises(ValueError, match="does not bind"):
        carry_mesh_review(toolkit, candidate, _review())


def test_malformed_original_observation_png_reports_stale(prepared):
    toolkit, candidate = prepared
    carry_mesh_review(toolkit, candidate, _review())
    (toolkit.run / "mesh_observations/mesh_001.png").write_bytes(b"broken PNG")
    assert mesh_review_status(toolkit, candidate)["status"] == "stale"


def test_mixed_inputs_freeze_all_admitted_auxiliary_evidence(prepared):
    toolkit, candidate = prepared
    _admit_auxiliary_inputs(toolkit)
    assert carry_mesh_review(toolkit, candidate, _review())["status"] == "current"
    record = json.loads((toolkit.run / candidate / "mesh_review.json").read_bytes())
    for name in ("images/sketch.png", "building_input.json", "resume_plan.json"):
        assert record["evidence"]["file_sha256"][name] == _sha(toolkit.run / name)


@pytest.mark.parametrize("relative", ["images/sketch.png", "building_input.json", "resume_plan.json"])
@pytest.mark.parametrize("review_already_saved", [False, True])
def test_changed_frozen_auxiliary_input_cannot_create_or_preserve_review(prepared, relative, review_already_saved):
    toolkit, candidate = prepared
    _admit_auxiliary_inputs(toolkit)
    if review_already_saved:
        carry_mesh_review(toolkit, candidate, _review())
    path = toolkit.run / relative
    if path.suffix == ".png":
        Image.new("RGB", (12, 8), "red").save(path)
    else:
        _write(path, {"notes": "different user information"})
    if review_already_saved:
        status = mesh_review_status(toolkit, candidate)
        assert status["status"] == "stale" and not status["complete_building"]
    else:
        with pytest.raises(ValueError, match="admitted .*changed"):
            carry_mesh_review(toolkit, candidate, _review())
        assert mesh_review_status(toolkit, candidate)["status"] == "missing"


@pytest.mark.parametrize("change", ["same_size_pixels", "missing_creation_hash"])
def test_first_finish_rejects_observation_image_not_bound_at_overlay_creation(prepared, change):
    toolkit, candidate = prepared
    if change == "same_size_pixels":
        Image.new("RGB", (24, 24), "red").save(toolkit.run / "mesh_observations/mesh_001.png")
    else:
        path = toolkit.run / "mesh_overlays/overlay_001.json"
        record = json.loads(path.read_bytes())
        record.pop("observation_image_sha256")
        _write(path, record)
    with pytest.raises(ValueError, match="image changed since overlay creation"):
        carry_mesh_review(toolkit, candidate, _review())
    assert mesh_review_status(toolkit, candidate)["status"] == "missing"


@pytest.mark.parametrize("field", ["images", "building_input", "plan_recovery"])
def test_manifest_auxiliary_paths_cannot_authorize_new_paths(prepared, field):
    toolkit, candidate = prepared
    _admit_auxiliary_inputs(toolkit)
    if field == "images":
        toolkit.manifest[field]["../outside.png"] = toolkit.manifest[field].pop("sketch.png")
    else:
        toolkit.manifest[field]["frozen_path"] = "../outside.json"
    _write(toolkit.run / "inputs.json", toolkit.manifest)
    with pytest.raises(ValueError):
        carry_mesh_review(toolkit, candidate, _review())
