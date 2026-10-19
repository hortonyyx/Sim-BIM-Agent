"""Mesh workers copy bounded evidence and return verified observational identities."""
from __future__ import annotations

import base64
import hashlib
import json

import numpy as np
from PIL import Image
import pytest

from src.agent.runtime_mesh_evidence import (
    bind_mesh_worker_tool_result, hydrate_mesh_worker_answer, mesh_worker_view_blocks,
    prepare_mesh_worker_packet, verify_mesh_worker_packet,
)
from src.agent.geometry.mesh_bim_frame import render_mesh_bim_overlay
from src.agent.geometry.mesh_observation import MeshObservation
from src.agent_runtime.store import EventStore
from src.harness_contracts import BudgetAmounts, ToolExecutionPayload, ToolInvocationPayload
from tests.test_mesh_delivery_review import prepared, _admit_auxiliary_inputs


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _packet(prepared, *, candidate=False, observations=()):
    toolkit, candidate_id = prepared
    child = toolkit.run.parent / "child"
    packet = prepare_mesh_worker_packet(toolkit.run, child, task_id="worker-a", question="Inspect visible shape.",
        notes=["Coordinator hypothesis only"], candidate=candidate_id if candidate else None,
        observation_ids=observations, timing={"started_epoch": 1, "deadline_epoch": 61, "time_budget_seconds": 60})
    return toolkit.run, child, packet


def _store(child, task_id="worker-a"):
    return EventStore(child.parent / "audit", run_id="mesh-worker-test", task_id=task_id,
                      budget_limit=BudgetAmounts(calls=30, tokens=100000))


def _record(store, packet, child, name, arguments, raw, call_id="view-call"):
    raw = bind_mesh_worker_tool_result(packet, child, name, arguments, raw)
    invocation = store.append(ToolInvocationPayload(call_id=call_id, tool_name=name,
        full_arguments=arguments, repeatability="read_only"))
    store.append(ToolExecutionPayload(call_id=call_id, tool_name=name, full_arguments=arguments,
        repeatability="read_only", outcome="succeeded", raw_result=store.capture(raw, force_blob=True),
        shown_result=store.capture(raw, force_blob=True), invocation_event_id=invocation.event_id))
    return raw


def _render(child, name="mesh_001"):
    prefix = child / "mesh_observations" / name
    prefix.parent.mkdir(exist_ok=True)
    picture, metadata = MeshObservation(child / "assets/input.glb").render(prefix,
        eye=[10, 10, 10], target=[0, 0, 0], width_m=10, height_m=10, width_px=24, height_px=24)
    raw = {"content": [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(prefix.with_suffix(".png").read_bytes()).decode()},
                       {"type": "text", "text": json.dumps({**metadata, "observation": name})}]}
    return picture, metadata, raw


def _answer(kind="mesh_observation", identity="mesh_001", **extra):
    return {"directly_seen": [{"observation_id": "seen-1", "statement": "A visible exterior face.",
        "evidence_kind": kind, "evidence_id": identity, **extra}],
        "interpretations": [{"interpretation_id": "meaning-1", "statement": "May be an office volume.",
                             "based_on_observation_ids": ["seen-1"], "confidence": "low"}],
        "uncertain": [{"uncertainty_id": "unknown-1", "statement": "Room use is not observed."}]}


def test_packet_copies_only_admitted_bytes_and_one_optional_candidate(prepared):
    toolkit, candidate = prepared
    _admit_auxiliary_inputs(toolkit)
    (toolkit.run / "private_gt.json").write_text("never copy", encoding="utf-8")
    (toolkit.run / "candidate_02").mkdir()
    (toolkit.run / "candidate_02/source_model.json").write_text("never copy", encoding="utf-8")
    parent, child, packet = _packet(prepared, candidate=True, observations=["mesh_001"])
    assert packet["source_snapshot"]["candidate"] == candidate
    for relative, digest in packet["parent_file_sha256"].items():
        assert _sha(parent / relative) == _sha(child / relative) == digest
        assert (parent / relative).stat().st_ino != (child / relative).stat().st_ino
    assert set(packet["child_file_sha256"]) == {
        "inputs.json", "assets/input.glb", "images/sketch.png", "building_input.json", "resume_plan.json",
        "candidate_01/source_model.json", "candidate_01/report.json", "candidate_01/proposal.json",
        "mesh_observations/mesh_001.json", "mesh_observations/mesh_001.png", "mesh_observations/mesh_001.npz"}
    assert not (child / "private_gt.json").exists()
    assert not (child / "candidate_02").exists()
    assert not (child / "mesh_overlays").exists()
    assert not (child / "candidate_01/viewer.html").exists()
    verify_mesh_worker_packet(packet, parent, child)


def test_packet_resume_is_immutable_and_unrelated_candidate_does_not_invalidate(prepared):
    parent, child, packet = _packet(prepared, candidate=True)
    (parent / "candidate_99").mkdir()
    (parent / "candidate_99/source_model.json").write_text("unrelated", encoding="utf-8")
    verify_mesh_worker_packet(packet, parent, child)
    assert prepare_mesh_worker_packet(parent, child, **packet["request"]) == packet
    changed = {**packet["request"], "question": "Changed task"}
    with pytest.raises(ValueError, match="changed on resume"):
        prepare_mesh_worker_packet(parent, child, **changed)


def test_raw_mesh_packet_does_not_depend_on_any_candidate(prepared):
    parent, child, packet = _packet(prepared)
    (parent / "candidate_01/source_model.json").write_text("changed", encoding="utf-8")
    verify_mesh_worker_packet(packet, parent, child)
    assert packet["source_snapshot"] is None
    assert not list(child.glob("candidate_*"))


@pytest.mark.parametrize("where", ["parent", "child"])
@pytest.mark.parametrize("relative", ["assets/input.glb", "images/sketch.png", "building_input.json",
    "resume_plan.json", "candidate_01/source_model.json", "mesh_observations/mesh_001.json",
    "mesh_observations/mesh_001.png", "mesh_observations/mesh_001.npz"])
def test_changed_packet_dependency_is_rejected(prepared, where, relative):
    _admit_auxiliary_inputs(prepared[0])
    parent, child, packet = _packet(prepared, candidate=True, observations=["mesh_001"])
    path = (parent if where == "parent" else child) / relative
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises((ValueError, OSError)):
        verify_mesh_worker_packet(packet, parent, child)


@pytest.mark.parametrize("field,value", [("task_id", "../escape"), ("candidate", "../candidate_01"),
    ("observation_ids", ["../mesh_001"]), ("observation_ids", ["mesh_001", "mesh_001"]),
    ("observation_ids", "mesh_001"), ("observation_ids", [["mesh_001"]]),
    ("timing", {"deadline_epoch": float("nan")})])
def test_model_selectors_are_ids_not_paths(prepared, field, value):
    parent = prepared[0].run
    options = {"task_id": "worker-a", "question": "inspect", field: value}
    child = parent.parent / "child"
    with pytest.raises(ValueError):
        prepare_mesh_worker_packet(parent, child, **options)
    assert not child.exists()


def test_linked_mesh_cannot_escape_admitted_parent(prepared):
    parent = prepared[0].run
    asset = parent / "assets/input.glb"
    outside = parent.parent / "outside.glb"
    asset.replace(outside)
    asset.symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        _packet(prepared)


def test_pure_uncertainty_answer_does_not_require_view_or_computed_values(prepared):
    parent, child, packet = _packet(prepared)
    with _store(child) as store:
        result = hydrate_mesh_worker_answer({"uncertain": [{"uncertainty_id": "unknown", "statement": "No useful observation yet."}]},
            packet, store, child)
    assert result["directly_seen"] == [] and result["fidelity"] == "not_evaluated"
    assert result["candidate_mutation"] is False


def test_auxiliary_image_requires_actual_worker_view_even_after_coordinator_read(prepared):
    _admit_auxiliary_inputs(prepared[0])
    _, child, packet = _packet(prepared)
    with _store(child) as store:
        blocks = mesh_worker_view_blocks(["sketch.png"], packet, store, child)
        assert base64.b64decode(blocks[1]["data"]) == (child / "images/sketch.png").read_bytes()
        with pytest.raises(ValueError, match="not supplied or produced"):
            hydrate_mesh_worker_answer(_answer(kind="image", identity="sketch.png"), packet, store, child)

        # Use the actual image tool, which may crop, scale and mark coordinates.
        from scripts.tool_scripts.run_bim_agent import Toolkit
        view = Toolkit(child, readonly=True).view("sketch.png", box=[1, 1, 9, 6], display_scale=2)
        metadata = json.loads(view[1])
        raw = {"content": [view[0].to_image_content().model_dump(mode="json"),
                           {"type": "text", "text": view[1]}]}
        _record(store, packet, child, "view_image", {"name": "sketch.png", "box": [1, 1, 9, 6]}, raw)
        answer = hydrate_mesh_worker_answer(_answer(kind="image", identity="sketch.png"), packet, store, child)
        evidence = answer["directly_seen"][0]["evidence"]
        assert evidence["origin"] == "auxiliary_input_image"
        actual_view = evidence["view_receipts"][0]["view"]
        assert actual_view["returned_png_sha256"] == metadata["returned_png_sha256"]
        assert actual_view["image_sha256"] == _sha(child / "images/sketch.png")
        assert actual_view["box_original_pixels"] == [1, 1, 9, 6]
        assert actual_view["returned_png_sha256"] != actual_view["image_sha256"]
        raw["content"][0]["data"] = base64.b64encode((child / "images/sketch.png").read_bytes()).decode()
        with pytest.raises(ValueError, match="original and shown PNG"):
            bind_mesh_worker_tool_result(packet, child, "view_image", {"name": "sketch.png"}, raw)


def test_generated_mesh_view_and_measurement_are_hydrated_from_real_tool_receipts(prepared):
    parent, child, packet = _packet(prepared)
    _, _, raw = _render(child)
    with _store(child) as store:
        _record(store, packet, child, "view_mesh", {}, raw)
        measurement = MeshObservation(child / "assets/input.glb").pixel_query(
            child / "mesh_observations/mesh_001", [[12, 12], [13, 12]])
        _record(store, packet, child, "measure_mesh_pixels", {"observation": "mesh_001", "pixels": [[12, 12], [13, 12]]},
            {"structuredContent": measurement}, call_id="measure-call")
        result = hydrate_mesh_worker_answer(_answer(measurement_call_id="measure-call"), packet, store, child)
        observation = result["directly_seen"][0]
        assert observation["box_pixels"] is None
        assert observation["evidence"]["origin"] == "original_mesh_observation"
        assert observation["computed_measurement"]["result"] == measurement
        assert observation["computed_measurement"]["status"] == "computed"
        assert observation["statement_status"] == "model_reported_observation"
        blocks = mesh_worker_view_blocks(["mesh_001"], packet, store, child)
        assert base64.b64decode(blocks[1]["data"]) == (child / "mesh_observations/mesh_001.png").read_bytes()
    verify_mesh_worker_packet(packet, parent, child)


@pytest.mark.parametrize("change", ["png", "npz", "metadata"])
def test_generated_evidence_change_after_tool_return_is_rejected(prepared, change):
    _, child, packet = _packet(prepared)
    _, _, raw = _render(child)
    with _store(child) as store:
        _record(store, packet, child, "view_mesh", {}, raw)
        prefix = child / "mesh_observations/mesh_001"
        if change == "png":
            Image.new("RGB", (24, 24), "red").save(prefix.with_suffix(".png"))
        elif change == "npz":
            with np.load(prefix.with_suffix(".npz"), allow_pickle=False) as source:
                values = {name: source[name] for name in source.files}
            values["world_points"] = values["world_points"] + 1
            np.savez_compressed(prefix.with_suffix(".npz"), **values)
        else:
            path = prefix.with_suffix(".json")
            value = json.loads(path.read_bytes())
            value["camera"]["eye"][0] += 1
            _write(path, value)
        with pytest.raises(ValueError, match="changed after"):
            hydrate_mesh_worker_answer(_answer(), packet, store, child)


@pytest.mark.parametrize("answer", [_answer(measurement_call_id="invented"), _answer(box_pixels=[0, 0, 25, 24]),
    _answer(identity="mesh_999"), {"directly_seen": [], "interpretations": [], "uncertain": []}])
def test_worker_answer_cannot_invent_measurement_location_or_reference(prepared, answer):
    _, child, packet = _packet(prepared, observations=["mesh_001"])
    with _store(child) as store:
        with pytest.raises((ValueError, FileNotFoundError)):
            hydrate_mesh_worker_answer(answer, packet, store, child)


def test_overlay_is_explicitly_source_projection_and_cannot_be_claimed_as_computed_measurement(prepared):
    _, child, packet = _packet(prepared, candidate=True, observations=["mesh_001"])
    source_path = child / "candidate_01/source_model.json"
    source = json.loads(source_path.read_bytes())
    prefix = child / "mesh_observations/mesh_001"
    metadata = json.loads(prefix.with_suffix(".json").read_bytes())
    with Image.open(prefix.with_suffix(".png")) as original:
        picture, record = render_mesh_bim_overlay(original, metadata, source)
    folder = child / "mesh_overlays"
    folder.mkdir()
    picture.save(folder / "overlay_001.png")
    record.update(candidate="candidate_01", observation="mesh_001", overlay_id="overlay_001",
        image_file="mesh_overlays/overlay_001.png", image_sha256=_sha(folder / "overlay_001.png"),
        observation_metadata_sha256=_sha(prefix.with_suffix(".json")),
        observation_image_sha256=_sha(prefix.with_suffix(".png")), source_file_sha256=_sha(source_path))
    _write(folder / "overlay_001.json", record)
    raw = {"content": [{"type": "image", "mimeType": "image/png", "data": base64.b64encode((folder / "overlay_001.png").read_bytes()).decode()}],
           "structuredContent": record}
    with _store(child) as store:
        _record(store, packet, child, "overlay_mesh_candidate", {"candidate": "candidate_01", "observation": "mesh_001"}, raw)
        result = hydrate_mesh_worker_answer(_answer(kind="mesh_overlay", identity="overlay_001"), packet, store, child)
        assert result["directly_seen"][0]["evidence"]["origin"] == "source_model_projection_not_original_building"
        assert result["source_snapshot"]["candidate"] == "candidate_01"


def test_unregistered_generated_view_and_other_worker_store_are_rejected(prepared):
    _, child, packet = _packet(prepared)
    _render(child)
    with _store(child) as store:
        with pytest.raises(ValueError, match="not supplied or produced"):
            hydrate_mesh_worker_answer(_answer(), packet, store, child)
    with _store(child, task_id="worker-a") as store:
        store.task_id = "another-worker"
        with pytest.raises(ValueError, match="different worker"):
            hydrate_mesh_worker_answer({"uncertain": [{"uncertainty_id": "unknown", "statement": "unknown"}]}, packet, store, child)


def test_returned_png_must_match_saved_mesh_view(prepared):
    _, child, packet = _packet(prepared)
    _, _, raw = _render(child)
    Image.new("RGB", (24, 24), "red").save(child / "mesh_observations/mesh_001.png")
    with pytest.raises(ValueError, match="image bytes differ"):
        bind_mesh_worker_tool_result(packet, child, "view_mesh", {}, raw)
