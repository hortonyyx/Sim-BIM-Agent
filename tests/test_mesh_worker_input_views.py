"""Exact parent crops are delivered once, with original-pixel evidence boundaries."""
import asyncio
import base64
import hashlib
import json

import pytest
from PIL import Image

from scripts.tool_scripts.run_bim_agent import Toolkit
from src.agent.runtime_mesh_evidence import (
    hydrate_mesh_worker_answer, prepare_mesh_worker_packet, verify_mesh_worker_packet,
)
from src.agent_runtime.store import EventStore
from src.harness_contracts import ToolExecutionPayload, ToolInvocationPayload
from test_agent_runtime import response
from tests.test_mesh_delivery_review import prepared
from test_mesh_workers import _inputs, _limits, _task, _wrapper, _start, _result, local_tools


def _view(store, run, *, journal=True):
    args = {"name": "sketch.png", "box": [2, 2, 6, 6], "coordinate_grid": False, "display_scale": 2}
    result = Toolkit(run, readonly=True).view(**args)
    raw = {"content": [result[0].to_image_content().model_dump(mode="json"),
                       {"type": "text", "text": result[1]}]}
    if journal:
        invocation = store.append(ToolInvocationPayload(call_id="parent-view", tool_name="view_image",
            full_arguments=args, repeatability="read_only"))
        store.append(ToolExecutionPayload(call_id="parent-view", tool_name="view_image",
            full_arguments=args, repeatability="read_only", outcome="succeeded",
            raw_result=store.capture(raw, force_blob=True), shown_result=store.capture(raw, force_blob=True),
            invocation_event_id=invocation.event_id))
    return json.loads(result[1]), raw


def _answer(name="sketch.png", box=None):
    return json.dumps({"directly_seen": [{"observation_id": "red", "statement": "Red pixels.",
        "evidence_kind": "image", "evidence_id": name,
        **({"box_pixels": box} if box else {})}], "interpretations": [], "uncertain": []})


def test_prefetch_delivers_exact_crop_first_request_reuses_and_reads_original_coordinates(tmp_path, local_tools):
    run, limits = _inputs(tmp_path), _limits()
    source = run / "images" / "sketch.png"
    with Image.open(source) as image:
        image.putpixel((0, 0), (0, 0, 255))
        image.save(source)
    manifest = json.loads((run / "inputs.json").read_bytes())
    manifest["images"]["sketch.png"]["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    (run / "inputs.json").write_text(json.dumps(manifest), encoding="utf-8")
    with EventStore(tmp_path / "audit", run_id="prefetch", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        _start(store)
        metadata, raw = _view(store, run)
        assert metadata["returned_png_sha256"] != metadata["image_sha256"]
        task = {**_task(), "image_view_ids": [metadata["view_id"]]}
        wrapper, adapters = _wrapper(store, run, limits, {"inspect": [response(text=_answer(box=[2, 2, 6, 6]))]})
        result = _result(asyncio.run(wrapper.call_tool("delegate_mesh_workers", {"tasks": [task]})))
        assert result["status"] == "completed", result
        assert result["runtime"]["model_calls"] == 1 and result["runtime"]["tool_calls"] == 0
        body = json.loads(adapters[0].requests[0])
        assert raw["content"][0]["data"] in json.dumps(body)
        assert "ORIGINAL image pixels" in json.dumps(body)
        request = next(e.payload for e in wrapper._child("inspect").events if e.payload.event_type == "adapter_request")
        assert request.images[0].sent.sha256 == metadata["returned_png_sha256"]
        assert request.images[0].original.sha256 == metadata["image_sha256"]
        seen = result["result"]["directly_seen"][0]
        assert seen["box_pixels"] == [2, 2, 6, 6]
        assert seen["evidence"]["prefetched_views"][0]["read_view_id"] == metadata["view_id"]
        repeated = _result(asyncio.run(wrapper.call_tool("delegate_mesh_workers", {"tasks": [task]})))
        assert repeated["result"] == result["result"] and len(adapters) == 1
        read = asyncio.run(wrapper.call_tool("read_mesh_worker", {"task_id": "inspect", "view_ids": [metadata["view_id"]]}))
        assert not read.get("isError"), read
        png = next(block for block in read["content"] if block["type"] == "image")
        assert png["data"] == raw["content"][0]["data"]
        digest = hashlib.sha256(base64.b64decode(png["data"])).hexdigest()
        origin = wrapper.image_origins(read)[digest]
        assert origin["box_original_pixels"] == [2, 2, 6, 6]
        assert origin["original_sha256"] == metadata["image_sha256"]
        assert origin["original_pixels_per_returned_pixel"] == metadata["original_pixels_per_returned_pixel"]
        store.validate()


def test_view_file_without_actual_parent_image_receipt_is_rejected(tmp_path):
    run, limits = _inputs(tmp_path), _limits()
    with EventStore(tmp_path / "audit", run_id="unseen", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        metadata, _ = _view(store, run, journal=False)
        with pytest.raises(ValueError, match="no successful complete"):
            prepare_mesh_worker_packet(run, tmp_path / "child", task_id="inspect", question="Inspect.",
                image_view_ids=[metadata["view_id"]], parent_store=store)
        assert not (tmp_path / "child").exists()


@pytest.mark.parametrize("case", ["outside_crop", "wrong_image", "view_id_as_image"])
def test_prefetched_crop_does_not_admit_unseen_pixels_or_other_identities(tmp_path, case):
    run, limits = _inputs(tmp_path), _limits()
    other = run / "images" / "other.png"
    Image.new("RGB", (8, 8), "blue").save(other)
    manifest = json.loads((run / "inputs.json").read_bytes())
    manifest["images"]["other.png"] = {"sha256": hashlib.sha256(other.read_bytes()).hexdigest(), "size": [8, 8]}
    (run / "inputs.json").write_text(json.dumps(manifest), encoding="utf-8")
    with EventStore(tmp_path / "audit", run_id="bounded", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        metadata, _ = _view(store, run)
        child = tmp_path / "child"
        packet = prepare_mesh_worker_packet(run, child, task_id="inspect", question="Inspect.",
            image_view_ids=[metadata["view_id"]], parent_store=store)
        child_store = store.for_task("inspect", "coordinator")
        answer = _answer(box=[0, 0, 4, 4]) if case == "outside_crop" else _answer(
            name="other.png" if case == "wrong_image" else metadata["view_id"])
        with pytest.raises(ValueError):
            hydrate_mesh_worker_answer(answer, packet, child_store, child)
        assert prepare_mesh_worker_packet(run, child, **packet["request"], parent_store=store) == packet
        (child / "worker_input_views" / (metadata["view_id"] + ".png")).write_bytes(b"changed")
        with pytest.raises(ValueError, match="changed"):
            verify_mesh_worker_packet(packet, run, child, parent_store=store)


def test_identical_crop_pixels_from_different_original_images_are_rejected(tmp_path):
    run, limits = _inputs(tmp_path), _limits()
    other = run / 'images/other.png'
    image = Image.new('RGB', (8, 8), 'red')
    image.putpixel((0, 0), (0, 0, 255))
    image.save(other)
    manifest = json.loads((run / 'inputs.json').read_bytes())
    manifest['images']['other.png'] = {'size': [8, 8], 'sha256': hashlib.sha256(other.read_bytes()).hexdigest()}
    (run / 'inputs.json').write_text(json.dumps(manifest), encoding='utf-8')
    with EventStore(tmp_path / 'audit', run_id='roi-hash-review', task_id='coordinator',
                    budget_limit=limits.ledger_limit()) as store:
        identities, returned = [], []
        toolkit = Toolkit(run, readonly=True)
        for index, name in enumerate(['sketch.png', 'other.png']):
            args = {'name': name, 'box': [2, 2, 6, 6], 'coordinate_grid': False, 'display_scale': 2}
            result = toolkit.view(**args)
            raw = {'content': [result[0].to_image_content().model_dump(mode='json'),
                               {'type': 'text', 'text': result[1]}]}
            metadata = json.loads(result[1])
            identities.append(metadata['view_id'])
            returned.append(metadata['returned_png_sha256'])
            call_id = f'parent-view-{index}'
            invocation = store.append(ToolInvocationPayload(call_id=call_id, tool_name='view_image',
                full_arguments=args, repeatability='read_only'))
            store.append(ToolExecutionPayload(call_id=call_id, tool_name='view_image', full_arguments=args,
                repeatability='read_only', outcome='succeeded', raw_result=store.capture(raw, force_blob=True),
                shown_result=store.capture(raw, force_blob=True), invocation_event_id=invocation.event_id))
        assert returned[0] == returned[1]
        assert manifest['images']['sketch.png']['sha256'] != manifest['images']['other.png']['sha256']
        with pytest.raises(ValueError, match='different source images'):
            prepare_mesh_worker_packet(run, tmp_path / 'child', task_id='inspect', question='Inspect only supplied crops',
                image_view_ids=identities, parent_store=store)
        assert not (tmp_path / 'child').exists()


def test_mesh_and_auxiliary_crop_same_bytes_cannot_alias_native_original(prepared, tmp_path):
    run = prepared[0].run
    mesh_picture = run / 'mesh_observations/mesh_001.png'
    with Image.open(mesh_picture) as mesh_image:
        assert mesh_image.mode == 'RGB'
        source = Image.new('RGB', (mesh_image.width + 2, mesh_image.height + 2), 'blue')
        source.paste(mesh_image, (1, 1))
        box = [1, 1, mesh_image.width + 1, mesh_image.height + 1]
    (run / 'images').mkdir()
    source_path = run / 'images/padded-mesh.png'
    source.save(source_path)
    manifest = json.loads((run / 'inputs.json').read_bytes())
    manifest['images'][source_path.name] = {'size': list(source.size),
        'sha256': hashlib.sha256(source_path.read_bytes()).hexdigest()}
    (run / 'inputs.json').write_text(json.dumps(manifest), encoding='utf-8')
    with EventStore(tmp_path / 'audit', run_id='roi-mixed-hash-review', task_id='coordinator',
                    budget_limit=_limits().ledger_limit()) as store:
        args = {'name': source_path.name, 'box': box, 'coordinate_grid': False, 'display_scale': 1}
        result = Toolkit(run, readonly=True).view(**args)
        raw = {'content': [result[0].to_image_content().model_dump(mode='json'),
                           {'type': 'text', 'text': result[1]}]}
        metadata = json.loads(result[1])
        assert metadata['returned_png_sha256'] == hashlib.sha256(mesh_picture.read_bytes()).hexdigest()
        assert metadata['returned_png_sha256'] != metadata['image_sha256']
        invocation = store.append(ToolInvocationPayload(call_id='parent-mixed-view', tool_name='view_image',
            full_arguments=args, repeatability='read_only'))
        store.append(ToolExecutionPayload(call_id='parent-mixed-view', tool_name='view_image', full_arguments=args,
            repeatability='read_only', outcome='succeeded', raw_result=store.capture(raw, force_blob=True),
            shown_result=store.capture(raw, force_blob=True), invocation_event_id=invocation.event_id))
        with pytest.raises(ValueError, match='identical mesh and auxiliary crop bytes'):
            prepare_mesh_worker_packet(run, tmp_path / 'child', task_id='inspect', question='Inspect supplied images',
                observation_ids=['mesh_001'], image_view_ids=[metadata['view_id']], parent_store=store)
        assert not (tmp_path / 'child').exists()
