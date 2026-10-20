"""Immutable mesh worker inputs and hash-bound observational answers.

The worker receives copies of admitted evidence, never a writable parent BIM.
File identities and computed measurements are supplied by trusted tool receipts,
not by model-authored hashes or filenames.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from scripts.tool_scripts.bim_agent_saved_result import result_metadata
from src.agent.bim_inputs import validate_png
from src.agent.geometry.mesh_bim_frame import validate_mesh_frame
from src.agent.geometry.source_model import _digest
from src.agent_runtime.store import json_bytes


POLICY = "mesh_worker_packet_v1"


def _path(root, relative):
    root = Path(root).resolve()
    relative_path = Path(relative)
    path = root / relative_path
    if (relative_path.is_absolute() or ".." in relative_path.parts
            or path.resolve() != path or not path.is_relative_to(root)):
        raise ValueError("mesh worker evidence escapes its admitted path")
    return path


def _sha(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _json(path):
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("mesh evidence must contain a JSON object")
    return value


def _id(value, pattern, label):
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError(f"invalid {label}")
    return value


def _image_name(value):
    if (not isinstance(value, str) or Path(value).name != value or "/" in value or "\\" in value
            or Path(value).suffix.lower() != ".png"):
        raise ValueError("auxiliary image must be an admitted PNG filename")
    return value


def _snapshot(run, candidate):
    if candidate is None:
        return None, []
    _id(candidate, r"candidate_\d+|seed", "candidate ID")
    files = [f"{candidate}/{name}" for name in ("source_model.json", "report.json", "proposal.json")]
    source, report = (_json(_path(run, name)) for name in files[:2])
    source_hash = _digest({k: v for k, v in source.items() if k != "source_model_sha256"})
    if source.get("source_model_sha256") != source_hash or report.get("source_model_sha256") != source_hash:
        raise ValueError("worker source snapshot digest or report binding changed")
    proposal_sha = _sha(_path(run, files[2]))
    if (report.get("proposal_sha256") != proposal_sha
            or source.get("generation", {}).get("proposal_sha256") != proposal_sha):
        raise ValueError("worker source snapshot proposal binding changed")
    frame = validate_mesh_frame(source.get("mesh_frame"))
    return {"candidate": candidate, "source_model_sha256": source_hash,
            "source_file_sha256": _sha(_path(run, files[0])), "mesh_frame": frame,
            "source_geometry_ready": report.get("source_geometry_ready") is True}, files


def _observation(run, observation, mesh_sha):
    _id(observation, r"mesh_\d+", "mesh observation ID")
    names = [f"mesh_observations/{observation}{suffix}" for suffix in (".json", ".png", ".npz")]
    metadata = _json(_path(run, names[0]))
    if metadata.get("mesh_sha256") != mesh_sha:
        raise ValueError("mesh observation belongs to a different original asset")
    size = validate_png(_path(run, names[1]))
    if metadata.get("resolution_px") != {"width": size[0], "height": size[1]}:
        raise ValueError("mesh observation image dimensions changed")
    for key, relative in (("image_sha256", names[1]), ("pixel_buffer_sha256", names[2])):
        if key in metadata and metadata[key] != _sha(_path(run, relative)):
            raise ValueError("mesh observation creation-time image or buffer changed")
    # Never follow metadata.artifacts paths. Queries use this local, derived prefix.
    import numpy as np
    with np.load(_path(run, names[2]), allow_pickle=False) as data:
        if (str(data["mesh_sha256"].item()) != mesh_sha
                or list(data["image_shape"]) != [size[1], size[0]]):
            raise ValueError("mesh observation pixel buffer identity or dimensions changed")
    return {"kind": "mesh_observation", "id": observation,
            "origin": "original_mesh_observation", "image_file": names[1],
            "resolution_px": metadata["resolution_px"],
            "file_sha256": {name: _sha(_path(run, name)) for name in names}}, names


def _section(run, identity, mesh_sha, snapshot):
    _id(identity, r"section_\d+", "mesh section ID")
    names = [f"mesh_sections/{identity}{suffix}" for suffix in (".json", ".png")]
    record = _json(_path(run, names[0]))
    size = validate_png(_path(run, names[1]))
    if (record.get("mesh_sha256") != mesh_sha or record.get("section_id") != identity
            or record.get("image_file") != names[1] or record.get("image_sha256") != _sha(_path(run, names[1]))
            or record.get("resolution_px") != {"width": size[0], "height": size[1]}):
        raise ValueError("mesh section asset, image or dimensions changed")
    if record.get("candidate") is not None and (snapshot is None or any(
            record.get(key) != snapshot[key] for key in ("candidate", "source_model_sha256", "source_file_sha256", "mesh_frame"))):
        raise ValueError("source-coordinate section requires the matching candidate snapshot")
    return {"kind": "mesh_section", "id": identity, "origin": "original_mesh_plane_intersection",
        "image_file": names[1], "resolution_px": record["resolution_px"],
        "coordinate_frame": record["coordinate_frame"], "height_m": record["height_m"],
        "source_snapshot": snapshot if record.get("candidate") is not None else None,
        "file_sha256": {name: _sha(_path(run, name)) for name in names}}, names


def _parent_inputs(parent_run, candidate, observations):
    manifest = _json(_path(parent_run, "inputs.json"))
    mesh = manifest.get("mesh_input")
    if not isinstance(mesh, dict) or mesh.get("frozen_path") != "assets/input.glb":
        raise ValueError("mesh worker requires an admitted original GLB")
    paths = ["assets/input.glb"]
    if _sha(_path(parent_run, paths[0])) != mesh.get("sha256"):
        raise ValueError("admitted original mesh changed")
    images = manifest.get("images", {})
    if not isinstance(images, dict):
        raise ValueError("admitted image inventory must be an object")
    for name, entry in images.items():
        relative = f"images/{_image_name(name)}"
        path = _path(parent_run, relative)
        if (_sha(path) != entry.get("sha256") or validate_png(path) != entry.get("size")):
            raise ValueError("admitted auxiliary image changed")
        paths.append(relative)
    for key, filename in (("building_input", "building_input.json"), ("plan_recovery", "resume_plan.json")):
        entry = manifest.get(key)
        if entry is None:
            continue
        path = _path(parent_run, filename)
        if (entry.get("frozen_path") != filename or entry.get("raw_sha256") != _sha(path)
                or entry.get("declaration") != _json(path)):
            raise ValueError(f"admitted {key} changed")
        paths.append(filename)
    snapshot, snapshot_files = _snapshot(parent_run, candidate)
    if snapshot and snapshot["mesh_frame"]["mesh_sha256"] != mesh["sha256"]:
        raise ValueError("source snapshot belongs to a different original mesh")
    paths += snapshot_files
    evidence = {}
    for observation in observations:
        reference, files = (_section(parent_run, observation, mesh["sha256"], snapshot)
                            if observation.startswith("section_") else _observation(parent_run, observation, mesh["sha256"]))
        evidence[observation] = reference
        paths += files
    return manifest, snapshot, evidence, {name: _sha(_path(parent_run, name)) for name in paths}


def _request(task_id, question, notes, candidate, observation_ids, timing):
    _id(task_id, r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", "worker task ID")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("worker question must be nonempty")
    if not isinstance(notes, (tuple, list)) or any(not isinstance(note, str) for note in notes):
        raise ValueError("worker notes must be strings")
    if not isinstance(observation_ids, (tuple, list)):
        raise ValueError("worker observation IDs must be a list")
    observations = list(observation_ids)
    for observation in observations:
        _id(observation, r"(?:mesh|section)_\d+", "mesh observation or section ID")
    if len(set(observations)) != len(observations):
        raise ValueError("worker observations must be distinct")
    if candidate is not None:
        _id(candidate, r"candidate_\d+|seed", "candidate ID")
    timing = {} if timing is None else dict(timing)
    if set(timing) - {"started_epoch", "deadline_epoch", "time_budget_seconds"}:
        raise ValueError("unknown mesh worker timing field")
    if any((value is None and key == "started_epoch") or (value is not None and (
           not isinstance(value, (int, float)) or isinstance(value, bool)
           or not math.isfinite(value) or value < 0)) for key, value in timing.items()):
        raise ValueError("mesh worker timing must contain finite nonnegative numbers")
    return {"task_id": task_id, "question": question, "notes": list(notes), "candidate": candidate,
            "observation_ids": observations, "timing": timing}


def prepare_mesh_worker_packet(parent_run, child_run, *, task_id, question, notes=(),
                               candidate=None, observation_ids=(), timing=None):
    """Copy only admitted assets and one optional source snapshot; resume is immutable."""
    parent_run, child_run = Path(parent_run).resolve(), Path(child_run).resolve()
    if child_run == parent_run or parent_run.is_relative_to(child_run):
        raise ValueError("worker workspace must be separate from its parent")
    request = _request(task_id, question, notes, candidate, observation_ids, timing)
    packet_path = _path(child_run, "mesh_worker_packet.json")
    if packet_path.exists():
        packet = _json(packet_path)
        if packet.get("request") != request:
            raise ValueError("worker task input or timing changed on resume")
        verify_mesh_worker_packet(packet, parent_run, child_run)
        return packet
    if child_run.exists() and any(child_run.iterdir()):
        raise ValueError("new mesh worker workspace must be empty")
    manifest, snapshot, observations, files = _parent_inputs(parent_run, candidate, request["observation_ids"])
    parent_manifest_sha = _sha(_path(parent_run, "inputs.json"))
    child_run.mkdir(parents=True, exist_ok=True)
    for relative, digest in files.items():
        target = _path(child_run, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_path(parent_run, relative), target)
        if _sha(target) != digest:
            raise ValueError("worker input changed while copying")
    child_manifest = {key: copy.deepcopy(manifest[key]) for key in
        ("images", "image_kind", "mesh_input", "building_input", "plan_recovery",
         "architectural_modelling_policy", "vertical_continuity_policy") if key in manifest}
    child_manifest.update(input_mode="runtime_mesh_worker_observation", scope=question,
        floor_plan_images=[], floor_scope_source="not_declared", **request["timing"])
    _path(child_run, "inputs.json").write_bytes(json_bytes(child_manifest))
    packet = {"policy": POLICY, "request": request, "parent_run": str(parent_run),
              "child_run": str(child_run), "input_manifest_sha256": parent_manifest_sha,
              "mesh_sha256": manifest["mesh_input"]["sha256"], "source_snapshot": snapshot,
              "parent_file_sha256": files, "child_file_sha256": {**files,
                  "inputs.json": _sha(_path(child_run, "inputs.json"))},
              "observations": observations, "image_names": sorted(manifest.get("images", {})),
              "fidelity": "not_evaluated"}
    packet["packet_sha256"] = _digest(packet)
    packet_path.write_bytes(json_bytes(packet))
    verify_mesh_worker_packet(packet, parent_run, child_run)
    return packet


def _verify_child(packet, child_run):
    if (packet.get("policy") != POLICY or packet.get("packet_sha256") != _digest(
            {k: v for k, v in packet.items() if k != "packet_sha256"})
            or packet.get("child_run") != str(Path(child_run).resolve())
            or _json(_path(child_run, "mesh_worker_packet.json")) != packet):
        raise ValueError("mesh worker packet identity or digest changed")
    for relative, digest in packet["child_file_sha256"].items():
        if _sha(_path(child_run, relative)) != digest:
            raise ValueError("frozen mesh worker input changed: " + relative)


def verify_mesh_worker_packet(packet, parent_run, child_run):
    """Verify current admitted inputs and the selected candidate, not other candidates."""
    _verify_child(packet, child_run)
    if (packet["parent_run"] != str(Path(parent_run).resolve())
            or _sha(_path(parent_run, "inputs.json")) != packet["input_manifest_sha256"]):
        raise ValueError("parent mesh input manifest changed")
    request = packet["request"]
    _, snapshot, observations, files = _parent_inputs(parent_run, request["candidate"], request["observation_ids"])
    if files != packet["parent_file_sha256"] or snapshot != packet["source_snapshot"] or observations != packet["observations"]:
        raise ValueError("parent mesh worker evidence changed")


def _evidence(packet, child_run, kind, identity):
    _verify_child(packet, child_run)
    if kind == "image":
        _image_name(identity)
        if identity not in packet["image_names"]:
            raise ValueError("image is outside the worker packet")
        relative = f"images/{identity}"
        size = validate_png(_path(child_run, relative))
        return {"kind": kind, "id": identity, "origin": "auxiliary_input_image",
                "image_file": relative, "resolution_px": {"width": size[0], "height": size[1]},
                "file_sha256": {relative: _sha(_path(child_run, relative))}}
    if kind == "mesh_observation":
        return _observation(child_run, identity, packet["mesh_sha256"])[0]
    if kind == "mesh_section":
        return _section(child_run, identity, packet["mesh_sha256"], packet["source_snapshot"])[0]
    if kind != "mesh_overlay":
        raise ValueError("unsupported mesh worker evidence kind")
    _id(identity, r"overlay_\d+", "mesh overlay ID")
    snapshot = packet["source_snapshot"]
    if snapshot is None:
        raise ValueError("source overlay requires a supplied candidate snapshot")
    relative = f"mesh_overlays/{identity}.json"
    record = _json(_path(child_run, relative))
    image_file = f"mesh_overlays/{identity}.png"
    original = _observation(child_run, record["observation"], packet["mesh_sha256"])[0]
    if (record.get("candidate") != snapshot["candidate"] or record.get("overlay_id") != identity
            or record.get("source_model_sha256") != snapshot["source_model_sha256"]
            or record.get("source_file_sha256") != snapshot["source_file_sha256"]
            or record.get("mesh_frame") != snapshot["mesh_frame"] or record.get("mesh_sha256") != packet["mesh_sha256"]
            or record.get("image_file") != image_file or record.get("image_sha256") != _sha(_path(child_run, image_file))
            or record.get("observation_image_sha256") != original["file_sha256"][original["image_file"]]
            or record.get("observation_metadata_sha256") != original["file_sha256"][f"mesh_observations/{record['observation']}.json"]):
        raise ValueError("worker overlay no longer binds its source and original observation")
    if validate_png(_path(child_run, image_file)) != [original["resolution_px"]["width"], original["resolution_px"]["height"]]:
        raise ValueError("worker overlay image dimensions changed")
    return {"kind": kind, "id": identity, "origin": "source_model_projection_not_original_building",
            "image_file": image_file, "resolution_px": original["resolution_px"],
            "source_snapshot": snapshot, "file_sha256": {**original["file_sha256"],
                relative: _sha(_path(child_run, relative)), image_file: _sha(_path(child_run, image_file))}}


def _comparison_reference(packet, child_run, metadata):
    identity = _id(metadata.get("evidence_id"), r"correspondence_\d+", "mesh comparison ID")
    relative = f"mesh_evidence/{identity}.json"
    path = _path(child_run, relative)
    record = _json(path)
    snapshot = packet["source_snapshot"]
    if (snapshot is None or metadata.get("evidence_file") != relative
            or metadata.get("evidence_sha256") != _sha(path)
            or record.get("evidence_id") != identity or record.get("mesh_sha256") != packet["mesh_sha256"]
            or any(record.get(key) != snapshot[key] for key in ("candidate", "source_model_sha256", "source_file_sha256", "mesh_frame"))):
        raise ValueError("worker comparison no longer binds its source or result bytes")
    original = _observation(child_run, record["observation"], packet["mesh_sha256"])[0]
    hashes = {kind: original["file_sha256"][f"mesh_observations/{record['observation']}{suffix}"]
              for kind, suffix in (("metadata", ".json"), ("image", ".png"), ("pixel_buffer", ".npz"))}
    if record.get("observation_file_sha256") != hashes or metadata.get("observation") != record["observation"]:
        raise ValueError("worker comparison observation bytes changed")
    report_file = metadata.get("details_file")
    if (not isinstance(report_file, str) or not re.fullmatch(
            r"tool_reports/compare_mesh_source_pixels_[0-9a-f]{64}\.json", report_file)
            or metadata.get("details_sha256") != _sha(_path(child_run, report_file))
            or metadata["details_sha256"] != _sha(path)):
        raise ValueError("worker comparison complete report bytes changed")
    return {"kind": "mesh_correspondence", "id": identity, "source_snapshot": snapshot,
            "file_sha256": {**original["file_sha256"], relative: _sha(path), report_file: metadata["details_sha256"]}}


def bind_mesh_worker_tool_result(packet, child_run, tool_name, arguments, raw):
    """Freeze file hashes in the normal tool receipt, immediately after execution."""
    _verify_child(packet, child_run)
    if raw.get("isError"):
        return raw
    metadata = result_metadata(raw)
    kind = identity = None
    if tool_name == "view_mesh":
        kind, identity = "mesh_observation", metadata.get("observation")
    elif tool_name in {"view_mesh_observation", "measure_mesh_pixels", "compare_mesh_source_pixels"}:
        kind, identity = "mesh_observation", arguments.get("observation")
    elif tool_name == "view_mesh_section":
        kind, identity = "mesh_section", metadata.get("section_id")
    elif tool_name == "overlay_mesh_candidate":
        kind, identity = "mesh_overlay", metadata.get("overlay_id")
    elif tool_name == "view_image":
        kind, identity = "image", metadata.get("name") or arguments.get("name")
    if kind is None:
        return raw
    reference = _evidence(packet, child_run, kind, identity)
    if tool_name in {"view_mesh", "view_mesh_observation", "overlay_mesh_candidate", "view_mesh_section", "view_image"}:
        shown = [hashlib.sha256(base64.b64decode(block["data"], validate=True)).hexdigest()
                 for block in raw.get("content", []) if block.get("type") == "image"]
        if tool_name == "view_image":
            if (metadata.get("image_sha256") != reference["file_sha256"][reference["image_file"]]
                    or metadata.get("returned_png_sha256") not in shown):
                raise ValueError("auxiliary image tool return does not bind its original and shown PNG")
        elif reference["file_sha256"][reference["image_file"]] not in shown:
            raise ValueError("mesh tool image bytes differ from its saved observation")
    if tool_name in {"measure_mesh_pixels", "compare_mesh_source_pixels"} and metadata.get("mesh_sha256") != packet["mesh_sha256"]:
        raise ValueError("worker measurement belongs to another mesh")
    result = dict(raw)
    result["structuredContent"] = {**metadata, "mesh_worker_evidence": reference}
    if tool_name == "compare_mesh_source_pixels":
        result["structuredContent"]["mesh_worker_computed_evidence"] = _comparison_reference(packet, child_run, metadata)
    return result


class _AnswerModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MeshDirectObservation(_AnswerModel):
    observation_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    evidence_kind: Literal["mesh_observation", "mesh_overlay", "mesh_section", "image"]
    evidence_id: str = Field(min_length=1)
    box_pixels: tuple[float, float, float, float] | None = None
    measurement_call_id: str | None = Field(default=None, description=(
        "Actual successful measure_mesh_pixels or compare_mesh_source_pixels call ID for mesh_observation; "
        "or actual view_mesh_section call ID for mesh_section. Trusted receipts supply the computed values."))


class MeshInterpretation(_AnswerModel):
    interpretation_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    based_on_observation_ids: tuple[str, ...] = Field(min_length=1)
    confidence: Literal["low", "medium", "high"]


class MeshUncertainty(_AnswerModel):
    uncertainty_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    related_observation_ids: tuple[str, ...] = ()


class MeshWorkerAnswer(_AnswerModel):
    directly_seen: tuple[MeshDirectObservation, ...] = ()
    interpretations: tuple[MeshInterpretation, ...] = ()
    uncertain: tuple[MeshUncertainty, ...] = ()


def _receipts(store):
    result = {}
    for event in store.events:
        payload = event.payload
        if (payload.event_type == "tool_execution" and payload.outcome == "succeeded"
                and payload.capture_scope == "complete"):
            raw = store.resolve(payload.raw_result)
            if isinstance(raw, dict) and not raw.get("isError"):
                metadata = result_metadata(raw)
                if isinstance(metadata.get("mesh_worker_evidence"), dict):
                    result[payload.call_id] = (event, raw, metadata)
    return result


def _verified_reference(packet, store, child_run, kind, identity, *, allow_admitted_image=False):
    if store.task_id != packet["request"]["task_id"]:
        raise ValueError("mesh evidence belongs to a different worker task")
    actual = _evidence(packet, child_run, kind, identity)
    image_views = []
    for event, raw, metadata in _receipts(store).values():
        bound = metadata["mesh_worker_evidence"]
        if bound.get("kind") == kind and bound.get("id") == identity:
            if bound != actual:
                raise ValueError("worker evidence changed after its tool return")
            # A numeric query is not an image delivery. It may support a computed
            # measurement below, but cannot itself prove the worker saw a view.
            if event.payload.tool_name in {"measure_mesh_pixels", "compare_mesh_source_pixels"}:
                continue
            receipt = {"tool_execution_event_id": event.event_id,
                       "tool_call_id": event.payload.call_id,
                       "tool_result_sha256": hashlib.sha256(json_bytes(raw)).hexdigest()}
            if kind == "image":
                image_views.append({**receipt, "view": {key: metadata[key] for key in (
                    "name", "image_sha256", "returned_png_sha256", "view_id", "original_size",
                    "returned_size", "box_original_pixels", "coordinate_grid",
                    "original_pixels_per_returned_pixel") if key in metadata}})
            else:
                return {**actual, **receipt}
    if image_views:
        return {**actual, "view_receipts": image_views}
    if ((kind == "image" and allow_admitted_image)
            or (kind in {"mesh_observation", "mesh_section"} and identity in packet["observations"])):
        return actual
    raise ValueError("worker answer cites evidence not supplied or produced by its tools")


def hydrate_mesh_worker_answer(answer, packet, store, child_run):
    """Attach trusted references; free-text numeric statements remain model statements."""
    _verify_child(packet, child_run)
    if store.task_id != packet["request"]["task_id"]:
        raise ValueError("mesh answer belongs to a different worker task")
    value = MeshWorkerAnswer.model_validate_json(answer) if isinstance(answer, str) else MeshWorkerAnswer.model_validate(answer)
    seen_ids = [row.observation_id for row in value.directly_seen]
    for values in (seen_ids, [r.interpretation_id for r in value.interpretations], [r.uncertainty_id for r in value.uncertain]):
        if len(set(values)) != len(values):
            raise ValueError("mesh worker answer IDs must be unique within each section")
    if not seen_ids and not value.uncertain:
        raise ValueError("an empty mesh worker answer must explain uncertainty")
    observations = []
    receipts = _receipts(store)
    for row in value.directly_seen:
        reference = _verified_reference(packet, store, child_run, row.evidence_kind, row.evidence_id)
        if row.box_pixels is not None:
            x0, y0, x1, y1 = row.box_pixels
            resolution = reference["resolution_px"]
            if not (0 <= x0 < x1 <= resolution["width"] and 0 <= y0 < y1 <= resolution["height"]):
                raise ValueError("worker observation box exceeds its evidence image")
        output = {"observation_id": row.observation_id, "statement": row.statement,
                  "statement_status": "model_reported_observation", "evidence": reference,
                  "box_pixels": list(row.box_pixels) if row.box_pixels is not None else None}
        if row.measurement_call_id is not None:
            if row.measurement_call_id not in receipts:
                raise ValueError("worker answer cites no successful measurement call")
            event, raw, metadata = receipts[row.measurement_call_id]
            name = event.payload.tool_name
            matching = ((name in {"measure_mesh_pixels", "compare_mesh_source_pixels"}
                         and row.evidence_kind == "mesh_observation"
                         and event.payload.full_arguments.get("observation") == row.evidence_id)
                        or (name == "view_mesh_section" and row.evidence_kind == "mesh_section"
                            and metadata.get("section_id") == row.evidence_id))
            if (not matching or metadata["mesh_worker_evidence"] != _evidence(packet, child_run, row.evidence_kind, row.evidence_id)):
                raise ValueError("worker computed evidence must cite the actual matching mesh measurement")
            if name == "compare_mesh_source_pixels" and metadata.get("mesh_worker_computed_evidence") != _comparison_reference(packet, child_run, metadata):
                raise ValueError("worker comparison changed after its tool return")
            output["computed_measurement"] = {"status": "computed", "tool_call_id": event.payload.call_id,
                "tool_execution_event_id": event.event_id, "tool_result_sha256": hashlib.sha256(json_bytes(raw)).hexdigest(),
                "result": {k: v for k, v in metadata.items() if k != "mesh_worker_evidence"}}
        observations.append(output)
    for row in (*value.interpretations, *value.uncertain):
        related = row.based_on_observation_ids if isinstance(row, MeshInterpretation) else row.related_observation_ids
        if set(related) - set(seen_ids):
            raise ValueError("mesh interpretation or uncertainty cites an unknown observation")
    return {"task_id": packet["request"]["task_id"], "packet_sha256": packet["packet_sha256"],
            "run_id": store.run_id, "input_manifest_sha256": packet["input_manifest_sha256"],
            "mesh_sha256": packet["mesh_sha256"], "source_snapshot": packet["source_snapshot"],
            "directly_seen": observations, "interpretations": [r.model_dump(mode="json") for r in value.interpretations],
            "uncertain": [r.model_dump(mode="json") for r in value.uncertain],
            "fidelity": "not_evaluated", "candidate_mutation": False}


def mesh_worker_view_blocks(view_ids, packet, store, child_run):
    """Return exact admitted or tool-bound PNGs without resolving caller paths."""
    blocks = []
    for identity in view_ids:
        kind = ("mesh_observation" if re.fullmatch(r"mesh_\d+", identity) else
                "mesh_section" if re.fullmatch(r"section_\d+", identity) else
                "mesh_overlay" if re.fullmatch(r"overlay_\d+", identity) else "image")
        reference = _verified_reference(packet, store, child_run, kind, identity, allow_admitted_image=True)
        raw = _path(child_run, reference["image_file"]).read_bytes()
        blocks += [{"type": "text", "text": json.dumps(reference, ensure_ascii=False)},
                   {"type": "image", "mimeType": "image/png", "data": base64.b64encode(raw).decode()}]
    return blocks
