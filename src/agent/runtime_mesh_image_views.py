"""Prefetch exact previously returned auxiliary views into a worker packet."""
from __future__ import annotations

import base64
import hashlib
import json


def bind_parent_receipts(store, views):
    """Require actual successful parent image delivery, not just a view file."""
    from scripts.tool_scripts.bim_agent_saved_result import result_metadata
    from src.agent_runtime.store import json_bytes
    if not views:
        return
    if store is None:
        raise ValueError("prefetched image views require their parent tool receipts")
    found = {}
    for event in store.events:
        payload = event.payload
        if (payload.event_type != "tool_execution" or payload.tool_name != "view_image"
                or payload.outcome != "succeeded" or payload.capture_scope != "complete"):
            continue
        raw = store.resolve(payload.raw_result)
        if not isinstance(raw, dict) or raw.get("isError"):
            continue
        metadata = result_metadata(raw)
        identity = metadata.get("view_id")
        if identity not in views:
            continue
        view = views[identity]
        shown = {hashlib.sha256(base64.b64decode(block["data"], validate=True)).hexdigest()
                 for block in raw.get("content", ()) if block.get("type") == "image"}
        if (metadata.get("name") != view["evidence_id"]
                or metadata.get("view_record_sha256") != view["source_view_sha256"]
                or metadata.get("returned_png_sha256") != view["view"]["returned_png_sha256"]
                or view["view"]["returned_png_sha256"] not in shown):
            raise ValueError("parent view receipt does not match the frozen auxiliary crop")
        found[identity] = {"tool_execution_event_id": event.event_id, "tool_call_id": payload.call_id,
                           "tool_result_sha256": hashlib.sha256(json_bytes(raw)).hexdigest()}
    if set(found) != set(views):
        raise ValueError("auxiliary crop has no successful complete parent view_image receipt")
    for identity, receipt in found.items():
        views[identity]["parent_view_receipt"] = receipt


def prepare_image_views(parent_run, identities, manifest):
    from src.agent.input_image_view import render_input_view
    from src.agent.runtime_mesh_evidence import _id, _image_name, _json, _path, _sha
    views, parent_files, pictures = {}, {}, {}
    for identity in identities:
        _id(identity, r"view_\d+", "auxiliary view ID")
        record_file = f"image_views/{identity}.json"
        record = _json(_path(parent_run, record_file))
        name = _image_name(record.get("name"))
        original = f"images/{name}"
        admitted = manifest.get("images", {}).get(name)
        if (not isinstance(admitted, dict) or record.get("view_id") != identity
                or record.get("image_sha256") != admitted.get("sha256")
                or record.get("original_size") != admitted.get("size")):
            raise ValueError("auxiliary view is not bound to this admitted image")
        region = record.get("box_original_pixels")
        grid = record.get("coordinate_grid", {}).get("shown")
        if (not isinstance(region, list) or len(region) != 4
                or any(type(v) is not int for v in region) or type(grid) is not bool):
            raise ValueError("auxiliary view needs integer original-pixel bounds and a grid contract")
        png, rendered = render_input_view(_path(parent_run, original), name=name, box=region,
            coordinate_grid=grid, display_scale=record.get("display_scale_requested", 1.0))
        digest = hashlib.sha256(png).hexdigest()
        if any(v["view"]["returned_png_sha256"] == digest
               and v["source_image_sha256"] != admitted["sha256"] for v in views.values()):
            raise ValueError("identical crop bytes from different source images need separate worker tasks")
        if (digest != record.get("returned_png_sha256") or any(
                rendered[key] != record.get(key) for key in (
                    "image_sha256", "original_size", "returned_size", "box_original_pixels",
                    "coordinate_grid", "original_pixels_per_returned_pixel"))):
            raise ValueError("auxiliary view cannot reproduce its original returned PNG/coordinates")
        picture_file = f"worker_input_views/{identity}.png"
        record_sha = _sha(_path(parent_run, record_file))
        views[identity] = {"id": identity, "kind": "image", "evidence_id": name,
            "origin": "prefetched_auxiliary_view", "image_file": picture_file,
            "source_image_file": original, "source_image_sha256": admitted["sha256"],
            "source_view_file": record_file, "source_view_sha256": record_sha,
            "resolution_px": {"width": rendered["returned_size"][0], "height": rendered["returned_size"][1]},
            "file_sha256": {picture_file: digest, original: admitted["sha256"], record_file: record_sha},
            "view": {**rendered, "returned_png_sha256": digest},
            "answer_reference": {"evidence_kind": "image", "evidence_id": name,
                "box_pixels_coordinate_system": "ORIGINAL image pixels, within a supplied crop; not crop-local pixels"},
            "delivery": "initial worker request image; not a worker tool call or measurement"}
        parent_files[record_file] = record_sha
        pictures[picture_file] = png
    return views, parent_files, pictures


def prefetched_image_reference(packet, identity):
    """A crop has a read ID; model claims continue to cite the admitted PNG name."""
    views = [value for value in packet.get("input_views", {}).values() if value["evidence_id"] == identity]
    return [{"read_view_id": value["id"], "origin": value["origin"],
             "image_file": value["image_file"], "file_sha256": value["file_sha256"],
             "view": value["view"], "delivery": value["delivery"]} for value in views]


def input_view_blocks(identity, packet, child_run):
    from src.agent.runtime_mesh_evidence import _path, _verify_child
    _verify_child(packet, child_run)
    view = packet.get("input_views", {}).get(identity)
    if view is None:
        raise ValueError("auxiliary view was not supplied to this worker")
    raw = _path(child_run, view["image_file"]).read_bytes()
    return [{"type": "text", "text": json.dumps(view, ensure_ascii=False)},
            {"type": "image", "mimeType": "image/png", "data": base64.b64encode(raw).decode()}]
