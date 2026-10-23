"""Offline stdio checks for the small BIM-agent MCP surface."""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import pytest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from PIL import Image

from scripts.tool_scripts.run_bim_agent import (Toolkit, cost_receipt_summary, digest,
                                                prepare_detail_observation,
                                                review_detail_observation,
                                                terminate_subscription)


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "scripts/tool_scripts/run_bim_agent.py"


def _run_with_one_image(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    images = run / "images"
    images.mkdir(parents=True)
    path = images / "plan.png"
    Image.new("RGB", (12, 8), "white").save(path)
    run.joinpath("inputs.json").write_text(json.dumps({
        "images": {"plan.png": {"size": [12, 8], "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}},
        "scope": "synthetic two-room test input",
        "only_input": "original image bytes and user scope; no GT/history",
    }), encoding="utf-8")
    return run


def _add_image(run: Path, name: str, color: str = "white") -> None:
    path = run / "images" / name
    Image.new("RGB", (12, 8), color).save(path)
    manifest = json.loads((run / "inputs.json").read_text())
    manifest["images"][name] = {"size": [12, 8], "sha256": digest(path)}
    (run / "inputs.json").write_text(json.dumps(manifest), encoding="utf-8")


@asynccontextmanager
async def _server_session(run: Path, *, readonly: bool):
    args = [str(SERVER), "serve", str(run)]
    if readonly:
        args.append("--readonly")
    parameters = StdioServerParameters(command=sys.executable, args=args, cwd=str(ROOT),
                                      env={"PYTHONUTF8": "1"})
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as session:
            await session.initialize()
            yield session


def _json_result(result):
    assert not result.isError
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


def _error_text(result) -> str:
    assert result.isError
    return "\n".join(getattr(row, "text", "") for row in result.content)


async def _profile_full_report(session, summary):
    """C3-T moves auxiliary intervals to the hash-checked, paged report."""
    pieces, offset = [], 0
    while True:
        page = _json_result(await session.call_tool('read_candidate_items', {
            'candidate': '', 'collection': 'report', 'report_file': summary['details_file'],
            'offset': offset, 'limit': 12000}))
        pieces.append(page['text'])
        if page['next_offset'] is None:
            break
        offset = page['next_offset']
    raw = ''.join(pieces)
    assert hashlib.sha256(raw.encode()).hexdigest() == summary['details_sha256']
    full = json.loads(raw)
    assert full['candidates'] == summary['candidates']
    assert full.get('empty_filter_diagnostics') == summary.get('empty_filter_diagnostics')
    assert summary['cross_axis_profile']['run_count'] == len(full['cross_axis_profile']['runs'])
    assert summary['threshold_excluded_support']['interval_count'] == len(full['threshold_excluded_support']['intervals'])
    assert 'evidence_note' not in summary
    return full


def _full_delivery_result(run, result):
    """Large receipts explicitly point to the complete persisted report."""
    reply = _json_result(result)
    if not reply.get("response_compacted"):
        return reply
    assert reply["full_delivery_report"] == "delivery.json"
    full = json.loads((run / reply["full_delivery_report"]).read_text())
    assert (full["candidate"], full["source_model_sha256"]) == (reply["candidate"], reply["source_model_sha256"])
    return full


def test_assemble_plans_stdio_keeps_both_floors_and_binds_originals(tmp_path):
    async def scenario():
        run = _run_with_one_image(tmp_path)
        _add_image(run, 'upstairs.png')
        plan = dict(floor_id='local', z_floor=0, ceiling_height=3,
            x_anchors=[[0,0],[11,11]], y_anchors=[[0,7],[7,0]], basis='synthetic shared frame',
            footprint_pixels=[[1,1],[10,1],[10,6],[1,6]],
            partitions=[dict(id='wall',points=[[5,1],[5,6]],source_refs=['synthetic'])],
            openings=[dict(id='door',kind='door',p1=[5,3],p2=[5,4],z=[0,2],source_refs=['synthetic'])],
            assumptions=[],unresolved=[])
        async with _server_session(run, readonly=False) as session:
            rows = []
            for i, image in enumerate(['plan.png', 'upstairs.png'], 1):
                result = _json_result(await session.call_tool('build_plan_bim',
                    dict(image=image, plan_json=json.dumps(plan))))
                assert result['source_geometry_ready']
                rows.append(dict(draft_id=f'draft_{i:03d}',
                    expected_plan_sha256=result['plan_input']['plan_sha256'],
                    floor_id=f'F{i}', z_floor=3*(i-1), evidence='synthetic storey annotation'))
            stale = [dict(r) for r in rows]
            stale[1]['expected_plan_sha256'] = '0'*64
            assert (await session.call_tool('assemble_plan_bim', {'floors_json':json.dumps(stale)})).isError
            result = _json_result(await session.call_tool('assemble_plan_bim', {'floors_json':json.dumps(rows)}))
            assert result['source_geometry_ready']
            candidate = result['candidate']
            source = json.loads((run/candidate/'source_model.json').read_text())
            assert {f['id'] for f in source['floors']} == {'F1', 'F2'}
            assert len(source['spaces']) == 4 and len(source['connections']) == 2
            doors = {o['id']:o for o in source['openings']}
            assert {v[2] for v in doors['F2:door']['vertices']} == {3,5}
            binding = source['generation']['provenance']['plan_assembly']
            assert digest(run/binding['file']) == binding['sha256']
            mapped = {r['floor_id'] for r in result['source_image_projections']}
            assert {'F1','F2'} <= mapped
            assert {r['floor_id'] for r in result['source_plan_views']} == {'F1','F2'}
            queried = _json_result(await session.call_tool('check_source_space_relation',
                dict(candidate=candidate,image='upstairs.png',floor_id='F2', observations_json=json.dumps([
                    dict(id='rooms',points=[[3,2],[8,2]],expected='separate_spaces',evidence='synthetic')]))))
            assert queried['conflict_count'] == 0
            assert queried['observations'][0]['direct_connections'][0]['opening_id'] == 'F2:door'
            # Both bound image bytes and hashes are checked before an assembly.
            Image.new('RGB',(12,8),'black').save(run/'images/upstairs.png')
            assert (await session.call_tool('assemble_plan_bim', {'floors_json':json.dumps(rows)})).isError
        async with _server_session(run, readonly=True) as session:
            assert 'assemble_plan_bim' not in {t.name for t in (await session.list_tools()).tools}
    asyncio.run(scenario())


def test_space_relation_stdio_uses_current_source_and_registered_calibration(tmp_path):
    async def scenario():
        run = _run_with_one_image(tmp_path)
        plan = dict(floor_id='F1', z_floor=0, ceiling_height=3,
            x_anchors=[[0,0],[11,11]], y_anchors=[[0,7],[7,0]], basis='synthetic',
            footprint_pixels=[[1,1],[10,1],[10,6],[1,6]],
            partitions=[dict(id='wall',points=[[5,1],[5,6]],source_refs=['synthetic'])],
            openings=[dict(id='door',kind='door',p1=[5,3],p2=[5,4],z=[0,2],source_refs=['synthetic'])],
            assumptions=[],unresolved=[])
        observations = [dict(id='hall',points=[[3,2],[8,2]],expected='same_space',evidence='synthetic observation')]
        async with _server_session(run, readonly=False) as session:
            first = _json_result(await session.call_tool('build_plan_bim', dict(image='plan.png',plan_json=json.dumps(plan))))
            assert first['source_geometry_ready']
            candidate = first['candidate']
            path = run/candidate/'source_model.json'
            unchanged = path.read_bytes()
            args = dict(candidate=candidate,image='plan.png',floor_id='F1',observations_json=json.dumps(observations))
            checked = _json_result(await session.call_tool('check_source_space_relation', args))
            assert checked['conflict_count'] == 1
            assert checked['observations'][0]['direct_connections'][0]['opening_id'] == 'door'
            assert checked['calibration_sha256'] == digest(run/checked['calibration_file'])
            assert json.loads((run/checked['review_file']).read_text()) == checked
            assert path.read_bytes() == unchanged
            first_delivery = _full_delivery_result(run, await session.call_tool('finish_bim', {'candidate':candidate}))
            assert first_delivery['space_relation_review']['conflict_count'] == 1
            observations[0]['expected'] = 'separate_spaces'
            await session.call_tool('check_source_space_relation', {**args,'observations_json':json.dumps(observations)})
            second_delivery = _full_delivery_result(run, await session.call_tool('finish_bim', {'candidate':candidate}))
            assert second_delivery['space_relation_review']['sample_count'] == 1
            assert second_delivery['space_relation_review']['status'] == 'consistent_with_supplied_samples'
            plan['partitions'] = []
            plan['openings'] = []
            second = _json_result(await session.call_tool('build_plan_bim', dict(image='plan.png',plan_json=json.dumps(plan))))
            assert second['source_geometry_ready']
            stale = _full_delivery_result(run, await session.call_tool('finish_bim', {'candidate':second['candidate']}))
            assert stale['space_relation_review']['status'] == 'not_reviewed'
            assert stale['space_relation_review']['stale_review_count'] == 2
            observations[0]['expected'] = 'same_space'
            repaired = _json_result(await session.call_tool('check_source_space_relation', {
                **args,'candidate':second['candidate'],'observations_json':json.dumps(observations)}))
            assert repaired['observations'][0]['actual_relation'] == 'same_space'
            invalid = await session.call_tool('check_source_space_relation', {**args,'image':'../plan.png'})
            assert invalid.isError
        async with _server_session(run, readonly=True) as session:
            assert 'check_source_space_relation' not in {t.name for t in (await session.list_tools()).tools}
    asyncio.run(scenario())


def test_plan_wall_support_stdio_reads_saved_paths_and_preserves_source(tmp_path):
    async def scenario():
        from PIL import ImageDraw
        run = _run_with_one_image(tmp_path)
        picture = Image.new('RGB', (80, 80), 'black')
        draw = ImageDraw.Draw(picture)
        for a, b in ((5, 19), (30, 44), (60, 70)):
            draw.line((20, a, 20, b), fill=(128, 128, 128))
        picture.save(run/'images/plan.png')
        manifest = json.loads((run/'inputs.json').read_text())
        manifest['images']['plan.png'] = dict(size=[80, 80], sha256=digest(run/'images/plan.png'))
        (run/'inputs.json').write_text(json.dumps(manifest))
        plan = dict(floor_id='F1', z_floor=0, ceiling_height=3,
            x_anchors=[[0, 0], [79, 7.9]], y_anchors=[[0, 7.9], [79, 0]],
            basis='synthetic', footprint_pixels=[[5,5],[70,5],[70,70],[5,70]],
            partitions=[dict(id='wall', points=[[20,5],[20,65]], source_refs=['synthetic'])],
            openings=[dict(id='door', kind='door', p1=[20,20], p2=[20,29], z=[0,2.1],
                           source_refs=['synthetic'])], assumptions=[], unresolved=[])
        async with _server_session(run, readonly=False) as session:
            failed = _json_result(await session.call_tool('build_plan_bim',
                dict(image='plan.png', plan_json=json.dumps(plan))))
            assert not failed['source_geometry_ready']
            args = dict(draft_id='draft_001', rgb=[128]*3, tolerance=0, radius_pixels=1)
            first = await session.call_tool('view_plan_wall_support', args)
            assert not first.isError
            data = json.loads(first.content[1].text)
            assert not data['proposed_space_adjacency_available']
            assert data['review_intervals'][0]['span_pixels'] == [45,59]
            plan['partitions'][0]['points'][1][1] = 70
            built = _json_result(await session.call_tool('build_plan_bim',
                dict(image='plan.png', plan_json=json.dumps(plan))))
            source = run/built['candidate']/'source_model.json'
            before = source.read_bytes()
            second = await session.call_tool('view_plan_wall_support', {**args, 'draft_id':'draft_002'})
            assert not second.isError
            data = json.loads(second.content[1].text)
            assert len(data['review_intervals'][0]['proposed_adjacent_space_ids']) == 2
            assert data['segments'][0]['declared_opening_sample_count'] == 10
            assert json.loads((run/data['support_record']).read_text()) == data
            assert digest(run/data['support_image']) == data['support_image_sha256']
            with Image.open(io.BytesIO(base64.b64decode(second.content[0].data))) as returned:
                assert returned.crop((0,0,80,80)).tobytes() == picture.tobytes()
            assert source.read_bytes() == before
            bad = await session.call_tool('view_plan_wall_support', {**args, 'draft_id':'../draft_001'})
            assert bad.isError
        async with _server_session(run, readonly=True) as session:
            assert 'view_plan_wall_support' not in {t.name for t in (await session.list_tools()).tools}
    asyncio.run(scenario())


@pytest.mark.parametrize("readonly", [True, False])
def test_facade_comparison_stdio_binds_originals_without_building(tmp_path, readonly):
    async def scenario():
        run = _run_with_one_image(tmp_path)
        _add_image(run, "east.png")
        observations = {
            "plan": {"axis_anchors": [[0, 0], [8, 8]],
                     "openings": [{"id": "p", "pixels": [1, 2], "kind": "door"}]},
            "elevation": {"axis_anchors": [[0, 0], [12, 8]],
                          "openings": [{"id": "e", "pixels": [9, 10.5]}]},
        }
        args = {"plan_image": "plan.png", "elevation_image": "east.png",
                "observations_json": json.dumps(observations)}
        async with _server_session(run, readonly=readonly) as session:
            first = _json_result(await session.call_tool("compare_facade_spans", args))
            assert first["direction_separation"]["lower_residual_direction"] == "elevation_reverse"
            assert first["original_images"]["plan"]["sha256"] == digest(run / "images/plan.png")
            assert first["observations"]["plan"]["openings"][0]["kind"] == "door"
            assert json.loads((run / first["record"]).read_text()) == first
            assert first["schema_version"] == "facade_span_direction_probe_v2"
            assert first["direction_separation"]["absolute_fit_status"] == "not_evaluated"
            second = _json_result(await session.call_tool("compare_facade_spans", args))
            assert second["record"] != first["record"]
            for literal in ("NaN", "Infinity", "-Infinity", "1e999"):
                invalid = json.dumps(observations).replace(
                    '"kind": "door"', f'"kind": "door", "confidence": {literal}')
                rejected = await session.call_tool(
                    "compare_facade_spans", {**args, "observations_json": invalid})
                assert "finite" in _error_text(rejected)
            rejected = await session.call_tool("compare_facade_spans", {**args, "plan_image": "../plan.png"})
            assert "Available filenames" in _error_text(rejected)
            observations["plan"]["axis_anchors"][1][0] = 9
            rejected = await session.call_tool("compare_facade_spans", {
                **args, "observations_json": json.dumps(observations)})
            assert "outside original image bounds" in _error_text(rejected)
            (run / "images/plan.png").write_bytes(b"changed")
            assert "input image changed" in _error_text(await session.call_tool("compare_facade_spans", args))
        assert len(list((run / "facade_comparisons").glob("*.json"))) == 2
        assert not list(run.glob("candidate*"))
    asyncio.run(scenario())


@pytest.mark.parametrize("readonly", [True, False])
def test_facade_comparison_adopts_actual_profile_candidates(tmp_path, readonly):
    async def scenario():
        run = _run_with_one_image(tmp_path)
        _add_image(run, "east.png")
        manifest = json.loads((run / "inputs.json").read_text())
        for name in ("plan.png", "east.png"):
            path = run / "images" / name
            pic = Image.open(path).convert("RGB")
            for x in range(12):
                for y in range(8):
                    if name == "plan.png" and y in (0, 7):
                        pic.putpixel((x, y), (0, 255, 0))
                    elif ((name == "plan.png" and y in (1, 2)) or
                          (name == "east.png" and x in (9, 10))):
                        pic.putpixel((x, y), (0, 255, 255))
            pic.save(path)
            manifest["images"][name]["sha256"] = digest(path)
        (run / "inputs.json").write_text(json.dumps(manifest))
        async with _server_session(run, readonly=readonly) as session:
            profiles = []
            for name, axis, rgb in (("plan.png", "y", [0, 255, 255]),
                                    ("plan.png", "y", [0, 255, 0]),
                                    ("east.png", "x", [0, 255, 255])):
                result = await session.call_tool("view_pixel_profile", {
                    "name": name, "box": [0, 0, 12, 8], "axis": axis,
                    "rgb": rgb, "tolerance": 0, "min_fraction": 0.5})
                assert not result.isError
                profiles.append(json.loads(result.content[1].text))

            def ref(index, candidate="C01", at="peak"):
                return {"profile": profiles[index]["profile_id"], "candidate": candidate, "at": at}

            raw = {"plan": {"axis_anchors": [[ref(1), 0], [ref(1, "C02"), 7]],
                            "openings": [{"id": "p", "pixels": [ref(0, at="start"), ref(0, at="end")]}]},
                   "elevation": {"axis_anchors": [[0, 0], [12, 7]],
                                 "openings": [{"id": "e", "pixels": [ref(2, at="start"), ref(2, at="end")]}]}}
            args = {"plan_image": "plan.png", "elevation_image": "east.png",
                    "observations_json": json.dumps(raw)}
            result = _json_result(await session.call_tool("compare_facade_spans", args))
            assert result["submitted_observations"] == raw
            assert result["observations"]["plan"]["axis_anchors"] == [[0, 0], [7, 7]]
            assert result["observations"]["plan"]["openings"][0]["pixels"] == [1, 2]
            assert result["observations"]["elevation"]["openings"][0]["pixels"] == [9, 10]
            assert len(result["measurement_bindings"]) == 6
            assert json.loads((run / result["record"]).read_text()) == result
            raw["plan"]["openings"][0]["pixels"][0] = ref(2)
            rejected = await session.call_tool("compare_facade_spans", {**args, "observations_json": json.dumps(raw)})
            assert rejected.isError
            raw["plan"]["openings"][0]["pixels"][0] = {**ref(0), "offset": 1}
            assert (await session.call_tool("compare_facade_spans", {**args, "observations_json": json.dumps(raw)})).isError
        assert len(list((run / "facade_comparisons").glob("*.json"))) == 1
        assert not list(run.glob("candidate*"))
    asyncio.run(scenario())


def _two_room_proposal() -> str:
    return json.dumps({
        "geometry": {
            "schema_version": "2", "footprint_x": [0, 6], "footprint_y": [0, 4],
            "floors": [{"name": "F1", "z_floor": 0, "ceiling_height": 3, "cells": [
                {"id": "left", "role": "office", "x": [0, 3], "y": [0, 4]},
                {"id": "right", "role": "corridor", "x": [3, 6], "y": [0, 4]},
            ]}],
            "windows": [],
            "openings": [{"id": "door", "kind": "door", "space_id": "left", "other_space_id": "right",
                          "p1": [3, 1], "p2": [3, 2], "z": [0, 2.1], "source_refs": ["synthetic"]}],
        },
        "assumptions": ["synthetic fixture"],
        "unresolved": [],
    })


def _two_floor_proposal() -> str:
    proposal = json.loads(_two_room_proposal())
    proposal["geometry"]["floors"].append({
        "name": "F2", "z_floor": 3, "ceiling_height": 3, "cells": [
            {"id": "upper_left", "role": "office", "x": [0, 3], "y": [0, 4]},
            {"id": "upper_right", "role": "corridor", "x": [3, 6], "y": [0, 4]},
        ],
    })
    return json.dumps(proposal)


def test_on_demand_reference_build_example_and_readonly_access(tmp_path):
    async def scenario():
        run = _run_with_one_image(tmp_path)
        async with _server_session(run, readonly=True) as session:
            reference = _json_result(await session.call_tool("get_bim_reference", {"topic": "geometry"}))
            # The documented example must remain executable through the real API.
            text = reference["reference"]
            proposal, _ = json.JSONDecoder().raw_decode(text[text.index("{"):])
            assert "build_bim" not in {t.name for t in (await session.list_tools()).tools}
            rejected = await session.call_tool("get_bim_reference", {"topic": "../inputs.json"})
            assert rejected.isError
        async with _server_session(run, readonly=False) as session:
            for topic in ("edits", "wall_dimensions", "opening_review", "room_types", "naming"):
                result = _json_result(await session.call_tool("get_bim_reference", {"topic": topic}))
                assert result["topic"] == topic and result["reference"]
            saved = _json_result(await session.call_tool("build_bim", {"proposal_json": json.dumps(proposal)}))
            assert saved["source_geometry_ready"]
            assert (run / saved["candidate"] / "source_model.json").is_file()
    asyncio.run(scenario())


def test_readonly_stdio_inventory_hash_and_tool_boundary(tmp_path):
    async def scenario():
        run = _run_with_one_image(tmp_path)
        async with _server_session(run, readonly=True) as session:
            tools = {tool.name for tool in (await session.list_tools()).tools}
            assert {"inputs", "view_image", "pixel_profile", "view_pixel_profile",
                    "view_pixel_region_overview", "map_pixels", "map_dimension_chain"} <= tools
            assert "build_bim" not in tools and "review_detail" not in tools
            assert "revise_bim" not in tools and "inspect_candidate" not in tools
            assert "check_openings" not in tools
            assert "finish_bim" not in tools and "overlay_candidate" not in tools
            assert "view_elevation_candidate" not in tools

            inventory = _json_result(await session.call_tool("inputs", {}))
            chain = _json_result(await session.call_tool("map_dimension_chain", {
                "lengths": [540, 4800, 2520, 1600, 540], "origin_m": 10,
                "direction": -1, "expected_total": 10000}))
            assert chain["segments"][3]["span_m"] == [0.54, 2.14]
            assert chain["closure_error_m"] == 0
            assert set(inventory["images"]) == {"plan.png"}
            viewed = await session.call_tool("view_image", {"name":"plan.png", "box":[2, 3, 12, 8]})
            assert viewed.content[0].type == "image"
            metadata = json.loads(viewed.content[1].text)
            assert metadata["box_original_pixels"] == [2, 3, 12, 8]
            assert metadata["returned_size"] == [10, 5]
            assert metadata["original_pixels_per_returned_pixel"] == [1, 1]
            assert inventory["images"]["plan.png"]["sha256"] == hashlib.sha256(
                (run / "images/plan.png").read_bytes()).hexdigest()
            assert (await session.call_tool("view_image", {"name": "plan.png"})).content[0].type == "image"
            for name in ("../outside.png", "ground_truth.png"):
                assert "Available filenames" in _error_text(await session.call_tool("view_image", {"name": name}))

            Image.new("RGB", (12, 8), "black").save(run / "images/plan.png")
            assert "input image changed" in _error_text(await session.call_tool("view_image", {"name": "plan.png"}))

    asyncio.run(scenario())


def test_view_image_opt_in_display_scale_preserves_original_pixels_and_caps_output(tmp_path):
    async def scenario():
        run = _run_with_one_image(tmp_path)
        pattern = Image.new("RGB", (5, 4))
        pattern.putdata([
            (x * 40, y * 60, (x + y) * 20)
            for y in range(pattern.height) for x in range(pattern.width)
        ])
        pattern_path = run / "images" / "pattern.png"
        pattern.save(pattern_path)
        manifest = json.loads((run / "inputs.json").read_text())
        manifest["images"]["pattern.png"] = {
            "size": list(pattern.size), "sha256": digest(pattern_path)}
        (run / "inputs.json").write_text(json.dumps(manifest), encoding="utf-8")
        before = digest(pattern_path)
        async with _server_session(run, readonly=True) as session:
            default = await session.call_tool("view_image", {
                "name": "pattern.png", "box": [1, 1, 3, 3], "coordinate_grid": False})
            default_meta = json.loads(default.content[1].text)
            assert default_meta["returned_size"] == [2, 2]
            assert default_meta["display_scale_requested"] == 1.0
            assert default_meta["display_scale_actual"] == [1.0, 1.0]

            scaled = await session.call_tool("view_image", {
                "name": "pattern.png", "box": [1, 1, 3, 3], "coordinate_grid": False,
                "display_scale": 4})
            scaled_meta = json.loads(scaled.content[1].text)
            returned = Image.open(io.BytesIO(base64.b64decode(scaled.content[0].data))).convert("RGB")
            expected = pattern.crop((1, 1, 3, 3)).resize((8, 8), Image.Resampling.NEAREST)
            assert returned.tobytes() == expected.tobytes()
            assert scaled_meta["returned_size"] == [8, 8]
            assert scaled_meta["display_scale_requested"] == 4.0
            assert scaled_meta["display_scale_actual"] == [4.0, 4.0]
            assert scaled_meta["box_original_pixels"] == [1, 1, 3, 3]
            assert scaled_meta["original_pixels_per_returned_pixel"] == [0.25, 0.25]
            assert "magnification_note" not in scaled_meta

        large = Image.new("RGB", (400, 100), "white")
        large_path = run / "images" / "large.png"
        large.save(large_path)
        manifest = json.loads((run / "inputs.json").read_text())
        manifest["images"]["large.png"] = {"size": list(large.size), "sha256": digest(large_path)}
        (run / "inputs.json").write_text(json.dumps(manifest), encoding="utf-8")
        async with _server_session(run, readonly=True) as session:
            capped = await session.call_tool("view_image", {
                "name": "large.png", "coordinate_grid": False, "display_scale": 8})
            capped_meta = json.loads(capped.content[1].text)
            assert capped_meta["returned_size"] == [1600, 400]
            assert capped_meta["display_scale_actual"] == [4.0, 4.0]
            assert "enlarged only 4.00x" in capped_meta["magnification_note"]
        assert digest(pattern_path) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("axis", ["x", "y"])
@pytest.mark.parametrize("readonly", [True, False])
def test_pixel_profile_preserves_equal_and_weaker_peaks_inside_connected_ink(tmp_path, axis, readonly):
    async def scenario():
        run = _run_with_one_image(tmp_path)
        path = run / "images/plan.png"
        pic = Image.new("RGB", (18, 18), "white")
        # A connected line extends past two equally supported bands. Extent,
        # first argmax, and the complete set of maxima are different facts.
        for along in range(4, 14):
            for across in range(5, 10) if along in (6, 7, 11) else (7,):
                xy = (along, across) if axis == "x" else (across, along)
                pic.putpixel(xy, (0, 0, 0))
        # A weaker local peak must also survive, rather than assuming both
        # dimension ticks always have exactly the same antialiased support.
        for across in (5, 6):
            pic.putpixel((9, across) if axis == "x" else (across, 9), (0, 0, 0))
        pic.save(path)
        before = digest(path)
        manifest = json.loads((run / "inputs.json").read_text())
        manifest["images"]["plan.png"].update(size=list(pic.size), sha256=before)
        (run / "inputs.json").write_text(json.dumps(manifest))
        box = [3, 4, 15, 11] if axis == "x" else [4, 3, 11, 15]
        async with _server_session(run, readonly=readonly) as session:
            reply = await session.call_tool("pixel_profile", dict(
                name="plan.png", box=box, axis=axis, rgb=[0, 0, 0], tolerance=0))
            result = _json_result(reply)
            assert result["runs"] == [{"pixels": [4, 13], "peak": 6, "max_count": 5,
                                       "support_peaks": [{"pixels": [6, 7], "count": 5},
                                                         {"pixels": [9, 9], "count": 3},
                                                         {"pixels": [11, 11], "count": 5}]}]
            assert result["matching_pixels"] == 24
            assert result["support_length"] == 7
            assert result["name"] == "plan.png" and result["box_original_pixels"] == box
            assert "not measured dimension" in result["evidence_note"]
            assert "including weaker peaks" in result["evidence_note"]
        assert digest(path) == before
        record = json.loads((run / "tools.jsonl").read_text().splitlines()[-1])
        assert record["data"]["result"] == result and record["readonly"] is readonly

    asyncio.run(scenario())


def test_view_pixel_profile_filters_sparse_strokes_and_reports_unbridged_peak_support(tmp_path):
    async def scenario():
        run = _run_with_one_image(tmp_path)
        path = run / "images" / "plan.png"
        pic = Image.new("RGB", (12, 8), "white")
        # Sparse marks touch consecutive x coordinates but never reach the
        # requested fraction. They must not become a candidate band.
        for x in range(1, 6):
            pic.putpixel((x, x % pic.height), (0, 0, 0))
        # Candidate x=8 has two exact y intervals separated by a real gap.
        for y in (1, 2, 4, 5, 6):
            pic.putpixel((8, y), (0, 0, 0))
        for y in (1, 2, 4):
            pic.putpixel((9, y), (0, 0, 0))
        for x in (*range(2, 8), 10):
            pic.putpixel((x, 7), (0, 0, 0))
        pic.save(path)
        manifest = json.loads((run / "inputs.json").read_text())
        manifest["images"]["plan.png"]["sha256"] = digest(path)
        (run / "inputs.json").write_text(json.dumps(manifest), encoding="utf-8")

        async with _server_session(run, readonly=True) as session:
            viewed = await session.call_tool("view_pixel_profile", {
                "name": "plan.png", "box": [0, 0, 12, 8], "axis": "x",
                "rgb": [0, 0, 0], "tolerance": 0, "min_fraction": 0.375})
            assert viewed.content[0].type == "image"
            result = json.loads(viewed.content[1].text)
            assert result["minimum_count"] == 3
            result = await _profile_full_report(session, result)
            assert result["candidates"] == [{
                "id": "C01", "pixels": [8, 9], "peak": 8, "max_count": 5,
                "max_fraction": 0.625,
                "support_intervals_at_peak": [[1, 2], [4, 6]],
            }]
            excluded = result["threshold_excluded_support"]
            assert excluded["intervals"] == [
                {"pixels": [1, 7], "min_count": 1, "max_count": 2},
                {"pixels": [10, 10], "min_count": 1, "max_count": 1},
            ]
            assert excluded["coordinate_count"] == 8 and excluded["matching_pixels"] == 12
            assert excluded["minimum_count"] == 3 and excluded["support_length"] == 8
            assert result["box_original_pixels"] == [0, 0, 12, 8]
            assert (run / result["profile_image"]).is_file()
            assert (run / result["profile_record"]).is_file()
            assert digest(run / result["profile_image"]) == result["profile_image_sha256"]
            assert json.loads((run / result["profile_record"]).read_text()) == result
            assert "do not prove" in result["evidence_note"]

            horizontal = await session.call_tool("view_pixel_profile", {
                "name": "plan.png", "box": [2, 1, 11, 8], "axis": "y",
                "rgb": [0, 0, 0], "tolerance": 0, "min_fraction": 0.5})
            horizontal_result = json.loads(horizontal.content[1].text)
            assert horizontal_result["minimum_count"] == 5
            assert horizontal_result["candidates"] == [{
                "id": "C01", "pixels": [7, 7], "peak": 7, "max_count": 7,
                "max_fraction": 0.777778,
                "support_intervals_at_peak": [[2, 7], [10, 10]],
            }]
            assert horizontal_result["box_original_pixels"] == [2, 1, 11, 8]
            assert horizontal_result["panel_layout"]["mask_panel_combined_pixels"][0] == 12
        records = [json.loads(line) for line in (run / "tools.jsonl").read_text().splitlines()]
        assert records[-1]["action"] == "view_pixel_profile"
        assert records[-1]["readonly"] is True

    asyncio.run(scenario())


@pytest.mark.parametrize("readonly", [True, False])
def test_profile_cross_axis_separates_long_traces_from_junction_peak(tmp_path, readonly):
    async def scenario():
        run = _run_with_one_image(tmp_path)
        path = run / "images/plan.png"
        pic = Image.new("RGB", (24, 20), "black")
        # Two long horizontal strokes meet one vertical stroke. Its peak
        # spans the crop height but is not the thickness of the long strokes.
        for x in range(4, 20):
            for y in (8, 11):
                pic.putpixel((x, y), (128, 128, 128))
        for y in range(5, 15):
            pic.putpixel((19, y), (128, 128, 128))
        pic.save(path)
        before = digest(path)
        manifest = json.loads((run / "inputs.json").read_text())
        manifest["images"]["plan.png"].update(size=list(pic.size), sha256=before)
        (run / "inputs.json").write_text(json.dumps(manifest))
        common = dict(name="plan.png", box=[4, 5, 20, 15], rgb=[128, 128, 128],
                      tolerance=0, min_fraction=.2)
        async with _server_session(run, readonly=readonly) as session:
            data = json.loads((await session.call_tool("view_pixel_profile", {**common, "axis": "x"})).content[1].text)
            data = await _profile_full_report(session, data)
            assert data["candidates"][0]["support_intervals_at_peak"] == [[5, 14]]
            cross = data["cross_axis_profile"]
            assert cross["axis"] == "y" and cross["minimum_count"] == 4
            assert [row["pixels"] for row in cross["runs"]] == [[8, 8], [11, 11]]
            assert all(row["max_count"] == 16 and row["support_intervals_at_peak"] == [[4, 19]]
                       for row in cross["runs"])
            # The long one-pixel vertical trace is hidden by the y threshold,
            # even though the horizontal junctions make the result nonempty.
            assert "empty_filter_diagnostics" not in data
            assert data["threshold_excluded_support"]["intervals"] == []
            assert cross["threshold_excluded_support"]["intervals"] == [
                {"pixels": [5, 7], "min_count": 1, "max_count": 1},
                {"pixels": [9, 10], "min_count": 1, "max_count": 1},
                {"pixels": [12, 14], "min_count": 1, "max_count": 1},
            ]
            context = data["crop_context"]
            assert context["edge_support_intervals"] == {
                "left": [[8, 8], [11, 11]], "right": [[5, 14]],
                "top": [[19, 19]], "bottom": [[19, 19]]}
            assert context["suggested_view_box"] == [0, 0, 24, 20]
            other = json.loads((await session.call_tool("view_pixel_profile", {**common, "axis": "y"})).content[1].text)
            other = await _profile_full_report(session, other)
            assert [{k: v for k, v in row.items() if k != "id"} for row in other["candidates"]] == cross["runs"]
            assert other["threshold_excluded_support"] == cross["threshold_excluded_support"]
            assert other["cross_axis_profile"]["threshold_excluded_support"] == data["threshold_excluded_support"]
            assert other["cross_axis_profile"]["runs"] == [
                {k: v for k, v in row.items() if k != "id"} for row in data["candidates"]]
            assert json.loads((run / data["profile_record"]).read_text()) == data
        assert digest(path) == before
    asyncio.run(scenario())


@pytest.mark.parametrize("readonly", [True, False])
def test_empty_profile_explains_color_mismatch_and_filtered_support(tmp_path, readonly):
    async def scenario():
        run = _run_with_one_image(tmp_path)
        path = run / "images" / "plan.png"
        pic = Image.new("RGB", (12, 8), "black")
        for x in range(2, 10):
            pic.putpixel((x, 3), (128, 128, 128))
        pic.save(path)
        before = digest(path)
        manifest = json.loads((run / "inputs.json").read_text())
        manifest["images"]["plan.png"]["sha256"] = before
        (run / "inputs.json").write_text(json.dumps(manifest))
        common = dict(name="plan.png", box=[0, 0, 12, 8], axis="y", tolerance=0)
        async with _server_session(run, readonly=readonly) as session:
            wrong = await session.call_tool("view_pixel_profile", {
                **common, "rgb": [255, 255, 255], "min_fraction": .5})
            data = json.loads(wrong.content[1].text)
            data = await _profile_full_report(session, data)
            diagnostic = data["empty_filter_diagnostics"]
            assert data["candidates"] == [] and data["matching_pixels"] == 0
            assert data["threshold_excluded_support"]["intervals"] == []
            assert diagnostic["reason"] == "no_pixels_match_requested_color"
            assert diagnostic["nearest_observed_color"]["rgb"] == [128, 128, 128]
            assert diagnostic["frequent_observed_colors"] == [
                {"rgb": [0, 0, 0], "pixel_count": 88},
                {"rgb": [128, 128, 128], "pixel_count": 8}]
            assert json.loads((run / data["profile_record"]).read_text()) == data
            strict = await session.call_tool("view_pixel_profile", {
                **common, "rgb": diagnostic["nearest_observed_color"]["rgb"], "min_fraction": 1})
            data = json.loads(strict.content[1].text)
            data = await _profile_full_report(session, data)
            diagnostic = data["empty_filter_diagnostics"]
            assert data["candidates"] == [] and data["matching_pixels"] == 8
            assert diagnostic["reason"] == "matching_pixels_below_support_threshold"
            assert diagnostic["maximum_support_count"] == 8 and diagnostic["minimum_count"] == 12
            assert data["threshold_excluded_support"]["intervals"] == [
                {"pixels": [3, 3], "min_count": 8, "max_count": 8}]
            good = await session.call_tool("view_pixel_profile", {
                **common, "rgb": [128, 128, 128], "min_fraction": .5})
            data = json.loads(good.content[1].text)
            assert "empty_filter_diagnostics" not in data
            assert data["candidates"][0]["pixels"] == [3, 3]
            assert data["candidates"][0]["support_intervals_at_peak"] == [[2, 9]]
            # A one-pixel minimum excludes nothing, and does not reclassify ink.
            unfiltered = await session.call_tool("view_pixel_profile", {
                **common, "rgb": [128, 128, 128], "min_fraction": .01})
            unfiltered_data = json.loads(unfiltered.content[1].text)
            unfiltered_data = await _profile_full_report(session, unfiltered_data)
            assert unfiltered_data["minimum_count"] == 1
            assert unfiltered_data["threshold_excluded_support"]["intervals"] == []
            assert unfiltered_data["cross_axis_profile"]["threshold_excluded_support"]["intervals"] == []
            plain = _json_result(await session.call_tool("pixel_profile", {
                **common, "rgb": [255, 255, 255]}))
            assert plain["runs"] == []
            assert plain["empty_filter_diagnostics"]["reason"] == "no_pixels_match_requested_color"
        assert digest(path) == before

    asyncio.run(scenario())


def test_source_elevation_stdio_returns_actual_source_and_changed_height(tmp_path):
    import base64
    import io
    from PIL import ImageChops

    async def scenario():
        run = _run_with_one_image(tmp_path)
        proposal = json.loads(_two_floor_proposal())
        proposal["geometry"]["windows"] = [{
            "id":"south_window", "floor":"F1", "facade":"South", "room":"left",
            "span":[1,2], "z":[0.4,2.2], "source_refs":["synthetic"],
        }]
        async with _server_session(run, readonly=False) as session:
            built = _json_result(await session.call_tool("build_bim", {"proposal_json":json.dumps(proposal)}))
            first = await session.call_tool("view_elevation_candidate", {"candidate":built["candidate"], "facade":"South"})
            metadata = _json_result(first)
            actual = Image.open(io.BytesIO(base64.b64decode(first.content[0].data))).convert("RGB")
            saved = Image.open(run / metadata["elevation_image"]).convert("RGB")
            assert actual.size == saved.size and ImageChops.difference(actual, saved).getbbox() is None
            source = json.loads((run / built["candidate"] / "source_model.json").read_text())
            assert metadata["source_model_sha256"] == source["source_model_sha256"]
            assert "South" == metadata["facade"]
            protected = digest(run / built["candidate"] / "source_model.json")
            paired = await session.call_tool("view_elevation_candidate", {
                "candidate": built["candidate"], "facade": "South", "image": "plan.png"})
            pair_meta = _json_result(paired)
            # The name does not control the orientation: caller pairing stays unverified.
            assert pair_meta["comparison"]["image_facade_binding"] == "caller_selected_not_verified"
            assert pair_meta["drawing_fidelity"] == "not_evaluated"
            assert [item.type for item in paired.content] == ["image", "image", "text", "text"]
            original = Image.open(io.BytesIO(base64.b64decode(paired.content[0].data)))
            assert original.size == (12, 8) and original.getpixel((0, 0)) == (255, 255, 255)
            assert paired.content[1].data == first.content[0].data
            assert pair_meta["original_view"]["box_original_pixels"] == [0, 0, 12, 8]
            assert not pair_meta["original_view"]["coordinate_grid"]["shown"]
            reference = Toolkit(run).read_image_view(pair_meta["original_view"]["view_id"])
            assert reference["record"]["returned_png_sha256"] == hashlib.sha256(base64.b64decode(paired.content[0].data)).hexdigest()
            assert json.loads((run / pair_meta["review_file"]).read_text()) == pair_meta
            assert digest(run / built["candidate"] / "source_model.json") == protected
            assert pair_meta["opening_heights"][0]["above_floor_m"] == [0.4, 2.2]
            missing = await session.call_tool("view_elevation_candidate", {
                "candidate": built["candidate"], "facade": "South", "image": "invented.png"})
            assert "Available filenames" in _error_text(missing)
            revised = _json_result(await session.call_tool("revise_bim", {
                "candidate":built["candidate"], "operations_json":json.dumps([{
                    "op":"update_window", "id":"south_window", "changes":{"z":[1,2.6]},
                    "reason":"synthetic height correction", "source_refs":["synthetic"],
                }])}))
            second = await session.call_tool("view_elevation_candidate", {"candidate":revised["candidate"], "facade":"South"})
            updated = _json_result(second)
            assert updated["source_model_sha256"] != metadata["source_model_sha256"]
            assert first.content[0].data != second.content[0].data
            assert updated["opening_heights"][0]["absolute_z_m"] == [1, 2.6]
            assert json.loads((run / pair_meta["review_file"]).read_text()) == pair_meta
            assert "facade" in _error_text(await session.call_tool("view_elevation_candidate", {
                "candidate":revised["candidate"], "facade":"../South"}))

    asyncio.run(scenario())


def test_calibrated_elevation_stdio_preserves_inputs_and_reuses_original_frame(tmp_path):
    from PIL import ImageChops

    async def scenario():
        run = _run_with_one_image(tmp_path)
        image_path = run / "images/plan.png"
        Image.new("RGB", (1800, 1800), "black").save(image_path)
        manifest = json.loads((run / "inputs.json").read_text())
        manifest["images"]["plan.png"] = {"size": [1800, 1800], "sha256": digest(image_path)}
        (run / "inputs.json").write_text(json.dumps(manifest))
        proposal = json.loads(_two_floor_proposal())
        proposal["geometry"]["windows"] = [{
            "id": "east_window", "floor": "F1", "facade": "East", "room": "right",
            "span": [1, 2], "z": [1, 2.6], "source_refs": ["synthetic"],
        }]
        frame = dict(facade="East", image="plan.png", horizontal_anchors=[[100, 0], [1100, 4]],
                     z_anchors=[[1600, 0], [100, 6]], basis="Synthetic exterior width and total height")
        async with _server_session(run, readonly=False) as session:
            built = _json_result(await session.call_tool("build_bim", {"proposal_json": json.dumps(proposal)}))
            source_path = run / built["candidate"] / "source_model.json"
            protected = {p: digest(p) for p in (source_path, image_path)}
            first = await session.call_tool("view_elevation_candidate", {"candidate": built["candidate"], **frame})
            metadata = _json_result(first)
            assert [item.type for item in first.content] == ["image", "image", "text", "text"]
            assert metadata["horizontal_axis"] == "y" and metadata["mode"] == "source_elevation_overlay"
            assert min(p[1] for p in metadata["projected_openings"][0]["pixel_vertices"]) == 950
            actual = Image.open(io.BytesIO(base64.b64decode(first.content[1].data))).convert("RGB")
            saved_path = run / metadata["elevation_image"]
            saved_hash = digest(saved_path)
            saved = Image.open(saved_path).convert("RGB")
            saved.thumbnail((1600, 1600))
            assert actual.size == (1600, 1600) and ImageChops.difference(actual, saved).getbbox() is None
            assert metadata["original_view"]["returned_size"] == metadata["returned_size"]
            assert all(digest(p) == h for p, h in protected.items())
            bad = await session.call_tool("view_elevation_candidate", {
                "candidate": built["candidate"], "facade": "East", "horizontal_anchors": [[0, 0], [1, 1]]})
            assert "requires image" in _error_text(bad)
            revised = _json_result(await session.call_tool("revise_bim", {
                "candidate": built["candidate"], "operations_json": json.dumps([{
                    "op": "update_window", "id": "east_window", "changes": {"z": [1, 2.8]},
                    "reason": "synthetic changed head", "source_refs": ["synthetic"],
                }])}))
            second = _json_result(await session.call_tool("view_elevation_candidate", {
                "candidate": revised["candidate"], **frame}))
            assert min(p[1] for p in second["projected_openings"][0]["pixel_vertices"]) == 900
            assert second["anchors"] == metadata["anchors"]
            assert second["source_model_sha256"] != metadata["source_model_sha256"]
            assert second["elevation_image"] != metadata["elevation_image"]
            assert digest(saved_path) == saved_hash
            assert json.loads((run / metadata["review_file"]).read_text()) == metadata
            assert all(digest(p) == h for p, h in protected.items())

    asyncio.run(scenario())


def test_room_use_stdio_updates_function_and_retains_physical_source(tmp_path):
    async def scenario():
        run = _run_with_one_image(tmp_path)
        async with _server_session(run, readonly=False) as session:
            built = _json_result(await session.call_tool("build_bim", {"proposal_json": _two_floor_proposal()}))
            original = json.loads((run / built["candidate"] / "source_model.json").read_text())
            assert built["room_use_review"]["summary"]["recorded_count"] == 0
            assert built["room_use_review"]["summary"]["unrecorded_count"] == len(original["spaces"])
            assert "next_action" not in built["room_use_review"]
            revised = _json_result(await session.call_tool("revise_bim", {
                "candidate": built["candidate"], "operations_json": json.dumps([{
                    "op": "set_space_role", "space_id": "left", "role": "conference/meeting/multipurpose",
                    "basis": "inferred", "assumptions": ["No room label; furniture-based interpretation"],
                    "source_refs": ["synthetic.png: table at [10,20,30,40]"], "reason": "Review function",
                }])}))
            updated = json.loads((run / revised["candidate"] / "source_model.json").read_text())
            room = next(s for s in updated["spaces"] if s["id"] == "left")
            assert room["role_evidence"]["basis"] == "inferred"
            assert revised["room_use_review"]["summary"]["recorded_count"] == 1
            assert revised["room_use_review"]["summary"]["inferred_count"] == 1
            assert "left" not in revised["room_use_review"]["unrecorded_space_ids"]
            assert "next_action" not in revised["room_use_review"]
            assert "Conference-Meeting-Multipurpose" in updated["public_names"]["spaces"]["left"]
            for key in ("floors", "boundaries", "openings", "connections", "opening_hosts"):
                assert updated[key] == original[key]
            assert original == json.loads((run / built["candidate"] / "source_model.json").read_text())
            finished = _json_result(await session.call_tool("finish_bim", {"candidate": revised["candidate"]}))
            inspected = _json_result(await session.call_tool("inspect_candidate", {"candidate": revised["candidate"]}))
            assert inspected["room_use_review"] == finished["room_use_review"]
            assert finished["room_use_review"]["use_decision"]["status"] == "no_issue_identified"
            assert finished["room_use_review"]["use_decision"]["space_count"] == 0
            assert finished["room_use_review"]["delivery_blocked"] is False
            assert {k: v for k, v in finished["room_use_review"].items() if k != "next_action"} == revised["room_use_review"]

    asyncio.run(scenario())


def test_wall_reference_stdio_calculation_persistence_and_feedback(tmp_path):
    from tests.test_wall_reference import reference, dimension

    async def scenario():
        run = _run_with_one_image(tmp_path)
        async with _server_session(run, readonly=False) as session:
            built = _json_result(await session.call_tool("build_bim", {"proposal_json": _two_room_proposal()}))
            candidate = built["candidate"]
            refs = [reference("west", "space/left/wall/3", [-0.12, 0.12]),
                    reference("shared", "space/left/wall/1", [-0.12, 0.12])]
            d = dimension()
            d["start"]["pixel"], d["end"]["pixel"] = [1, 2], [6, 2]
            checked = _json_result(await session.call_tool("check_wall_dimensions", {
                "candidate": candidate, "references_json": json.dumps(refs), "dimensions_json": json.dumps([d]),
                "include_inventory": True}))
            assert checked["dimension_report"]["dimensions"][0]["residual_m"] == 0
            assert checked["boundary_inventory"]
            await session.call_tool("overlay_candidate", {"candidate": candidate, "image": "plan.png",
                "floor_id": "F1", "x_anchors": [[1, 0], [10, 6]], "y_anchors": [[6, 0], [1, 4]], "basis": "synthetic"})
            result = await session.call_tool("revise_bim", {"candidate": candidate, "operations_json": json.dumps([
                {"op": "set_wall_references", "wall_references": refs, "wall_dimensions": [d], "reason": "synthetic"}])})
            assert not result.isError
            assert any(c.type == "image" for c in result.content)
            revised = _json_result(result)
            source = json.loads((run / revised["candidate"] / "source_model.json").read_text())
            assert len(source["wall_references"]) == 2
            assert source["wall_dimension_report"]["dimensions"][0]["raw_length_m"] == 2.76
            projection = revised["source_image_projections"][0]
            evidence = projection["wall_evidence_projection"]
            assert evidence["scope"]["image_name"] == "plan.png"
            assert [p["pixel"] for p in evidence["endpoints"]] == [[1, 2], [6, 2]]
            assert {p["wall_id"] for p in evidence["endpoints"]} == {"west", "shared"}
            delivered = _full_delivery_result(run, await session.call_tool("finish_bim", {"candidate": revised["candidate"]}))
            assert delivered["source_image_feedback"]["current_source_projections"][0]["wall_evidence_projection"] == evidence
            assert "尺寸证据与所引用墙段的位置对照" in (run / "delivery.html").read_text()
            d["end"]["pixel"] = [12, 2]
            assert "outside original image" in _error_text(await session.call_tool("check_wall_dimensions", {
                "candidate": candidate, "references_json": json.dumps(refs), "dimensions_json": json.dumps([d])}))

    asyncio.run(scenario())


def test_original_coordinate_grid_keeps_crop_and_thumbnail_frame():
    from scripts.tool_scripts.run_bim_agent import coordinate_grid_view
    pic = Image.new("RGB", (800, 400), "white")
    raw = pic.tobytes()
    annotated, meta = coordinate_grid_view(pic, [400, 600, 2000, 1400])
    assert annotated.size == pic.size
    assert pic.tobytes() == raw
    assert annotated.tobytes() != raw
    assert {"original_pixel": 1000, "display_pixel": 300} in meta["ticks"]["x"]
    assert {"original_pixel": 1200, "display_pixel": 300} in meta["ticks"]["y"]


def test_upper_floor_opening_diagnostic_reaches_agent_and_preserves_declared_door(tmp_path):
    async def scenario():
        run = _run_with_one_image(tmp_path)
        proposal = json.loads(_two_floor_proposal())
        proposal["geometry"]["openings"].append({
            "id": "upper_door", "kind": "door", "space_id": "upper_left",
            "other_space_id": "upper_right", "p1": [3, 1], "p2": [3, 2],
            "z": [0, 2.1], "source_refs": ["synthetic upper floor aperture"],
        })
        async with _server_session(run, readonly=False) as session:
            built = _json_result(await session.call_tool("build_bim", {"proposal_json": json.dumps(proposal)}))
            assert built["counts"]["unbuilt_openings"] == 1
            inspected = _json_result(await session.call_tool("inspect_candidate", {"candidate": built["candidate"]}))
            diagnostic = inspected["source_validation"]["findings"][0]["height_host_diagnostic"]
            assert diagnostic["actual_opening_z_bounds_m"] == [0, 2.1]
            assert {tuple(row["expected_z_bounds_m"]) for row in diagnostic["declared_space_bounds"]} == {(3, 6)}
            revised = _json_result(await session.call_tool("revise_bim", {
                "candidate": built["candidate"], "operations_json": json.dumps([{
                    "op": "update_opening", "id": "upper_door", "changes": {"z": [3, 5.1]},
                    "reason": "synthetic correction to absolute floor coordinates",
                    "source_refs": ["synthetic upper floor aperture"],
                }]),
            }))
            assert revised["source_geometry_ready"]
            assert revised["counts"]["unbuilt_openings"] == 0
            assert revised["counts"]["connections"] == 2
            source = json.loads((run / revised["candidate"] / "source_model.json").read_text())
            assert {o["id"] for o in source["openings"]} == {"door", "upper_door"}

    asyncio.run(scenario())


def test_delivery_keeps_numeric_contradiction_and_calibration_warning_with_empty_notes(tmp_path):
    from tests.test_wall_reference import reference, dimension
    from src.agent.geometry.proposal_edits import apply_proposal_edits
    run = _run_with_one_image(tmp_path)
    toolkit = Toolkit(run)
    proposal = json.loads(_two_room_proposal())
    proposal["assumptions"], proposal["unresolved"] = [], []
    proposal["wall_references"] = [reference("west", "space/left/wall/3", [0, 0.24])]
    d = dimension(value=240)
    d["end"]["wall_id"] = "west"
    d["start"]["pixel"], d["end"]["pixel"] = [1, 2], [2, 2]
    proposal["wall_dimensions"] = [d]
    built = toolkit.build(proposal)
    assert built["source_geometry_ready"]
    toolkit.project_overlay(built["candidate"], "plan.png", "F1", [[0,0],[10,6]], [[0,4],[7,0]],
                            "synthetic inconsistent scales", trigger_action="overlay_candidate")
    result = toolkit.delivery(built["candidate"], selection_origin="agent_selected")
    assert result["generation"]["unresolved"] == []
    assert result["wall_dimension_report"]["findings"][0]["code"] == "same_wall_endpoint_order"
    assert result["source_image_feedback"]["current_source_projections"][0]["calibration_warnings"]
    page = (run / "delivery.html").read_text()
    assert "-0.48" in page and ("cross-axis" in page.lower() or "disagree" in page)
    # A new candidate replaces only the evidence, not the source objects.
    d["start"]["side"], d["end"]["side"] = "negative", "positive"
    updated = apply_proposal_edits(proposal, [{"op":"set_wall_references", "wall_references":proposal["wall_references"],
                                             "wall_dimensions":[d], "reason":"synthetic side correction"}])
    new = toolkit.build(updated)
    current = toolkit.delivery(new["candidate"], selection_origin="agent_selected")
    assert current["wall_dimension_report"]["findings"] == []
    assert current["source_image_feedback"]["current_source_projections"] == []
    assert len(current["source_image_feedback"]["old_source_projections"]) == 1


def test_detail_review_uses_isolated_image_only_workspace_and_parent_receipt(tmp_path):
    run = _run_with_one_image(tmp_path)
    _add_image(run, "unselected.png", "black")
    manifest = json.loads((run / "inputs.json").read_text())
    manifest.update(scope="PARENT_SCOPE_MUST_NOT_LEAK", seed={"secret":"PARENT_SEED_MUST_NOT_LEAK"},
                    historical_candidate="PARENT_HISTORY_MUST_NOT_LEAK")
    (run / "inputs.json").write_text(json.dumps(manifest), encoding="utf-8")
    (run / "candidate_01").mkdir()
    (run / "candidate_01" / "proposal.json").write_text("PARENT_CANDIDATE_MUST_NOT_LEAK")
    calls = []

    def fake_subscription(mcp_run, prompt, **kwargs):
        calls.append((mcp_run, prompt, kwargs))
        assert kwargs["model"] == "haiku" and kwargs["readonly"]
        assert kwargs["name"] == "detail_01"
        assert kwargs["log_run"] == run
        assert kwargs["receipt_context"]["observation_source"]["run"] == "detail_01"
        return {"actual_model":"offline-haiku", "returncode":0,
                "result":{"is_error":False, "result":"observed mark", "total_cost_usd":0.75}}

    response = review_detail_observation(Toolkit(run), "What mark is visible at [1,2,3,4]?", ["plan.png"],
                                         invoke=fake_subscription)
    child, prompt, _ = calls[0]
    assert child == run / "detail_01"
    assert prompt == "Images: ['plan.png']\nQuestion: What mark is visible at [1,2,3,4]?"
    assert response["completed"] and response["observation_source"]["input_sha256"] == digest(child / "inputs.json")
    assert response["observation_source"]["images"]["plan.png"] == digest(child / "images/plan.png")
    assert set(json.loads((child / "inputs.json").read_text())["images"]) == {"plan.png"}
    assert (child / "question.txt").read_text() == "What mark is visible at [1,2,3,4]?"
    assert not (child / "images/unselected.png").exists()
    child_text = "\n".join(path.read_text(errors="ignore") for path in child.rglob("*") if path.is_file())
    for secret in ("PARENT_SCOPE_MUST_NOT_LEAK", "PARENT_SEED_MUST_NOT_LEAK",
                   "PARENT_HISTORY_MUST_NOT_LEAK", "PARENT_CANDIDATE_MUST_NOT_LEAK"):
        assert secret not in child_text

    async def readonly_boundary():
        async with _server_session(child, readonly=True) as session:
            tools = {tool.name for tool in (await session.list_tools()).tools}
            assert "review_detail" not in tools and "build_bim" not in tools
            inventory = _json_result(await session.call_tool("inputs", {}))
            assert set(inventory["images"]) == {"plan.png"}
            assert inventory["images"]["plan.png"]["sha256"] == digest(child / "images/plan.png")

    asyncio.run(readonly_boundary())
    # subscription writes detail receipts in the parent, so the root summary sees
    # this invocation once and does not recurse into the isolated child workspace.
    (run / "agent_receipt.json").write_text(json.dumps({"result":{"total_cost_usd":0.5}}))
    (run / "detail_01_receipt.json").write_text(json.dumps({"result":{"total_cost_usd":0.75}}))
    receipts, costs = cost_receipt_summary(run)
    assert len(receipts) == 2 and costs == {"estimated_cost_usd":1.25,
                                             "cost_receipts_complete":True,
                                             "reported_partial_cost_usd":1.25}


def test_detail_review_refuses_budget_exhaustion_or_too_little_time_without_calling(tmp_path):
    run = _run_with_one_image(tmp_path)
    manifest = json.loads((run / "inputs.json").read_text())
    manifest["deadline_epoch"] = time.time() + 50
    (run / "inputs.json").write_text(json.dumps(manifest), encoding="utf-8")
    invoked = False

    def fake_subscription(*args, **kwargs):
        nonlocal invoked
        invoked = True
        return {}

    response = review_detail_observation(Toolkit(run), "Check the visible mark.", ["plan.png"],
                                         invoke=fake_subscription)
    assert response["completed"] is False and "insufficient remaining budget" in response["error"]
    assert not invoked and not (run / "detail_01").exists()

    manifest.pop("deadline_epoch")
    (run / "inputs.json").write_text(json.dumps(manifest), encoding="utf-8")
    for number in (1, 2):
        (run / f"detail_{number:02d}_request.json").write_text("{}")
    response = review_detail_observation(Toolkit(run), "Check the visible mark.", ["plan.png"],
                                         invoke=fake_subscription)
    assert response == {"error":"local review budget exhausted", "completed":False}
    assert not invoked


def test_detail_review_passes_bounded_budget_into_hashed_readonly_manifest(tmp_path):
    run = _run_with_one_image(tmp_path)
    parent_deadline = time.time() + 180
    manifest = json.loads((run / "inputs.json").read_text())
    manifest["deadline_epoch"] = parent_deadline
    (run / "inputs.json").write_text(json.dumps(manifest), encoding="utf-8")
    calls = []

    def fake_subscription(child, prompt, **kwargs):
        calls.append((child, prompt, kwargs))
        child_manifest = json.loads((child / "inputs.json").read_text())
        assert abs(child_manifest["deadline_epoch"] - (
            parent_deadline - 45)) < 0.001
        assert kwargs["timeout"] <= 135
        return {"actual_model": "offline-haiku", "returncode": 0,
                "result": {"is_error": False, "result": "one observed mark"}}

    response = review_detail_observation(Toolkit(run), "Check this one mark.", ["plan.png"],
                                         invoke=fake_subscription)
    child, _, kwargs = calls[0]
    assert response["observation_source"]["input_sha256"] == digest(child / "inputs.json")
    assert kwargs["receipt_context"]["observation_source"] == response["observation_source"]

    async def readonly_time_is_visible():
        async with _server_session(child, readonly=True) as session:
            inventory = _json_result(await session.call_tool("inputs", {}))
            assert inventory["deadline_epoch"] == json.loads(
                (child / "inputs.json").read_text())["deadline_epoch"]
            assert isinstance(inventory["remaining_seconds"], int)
            viewed = await session.call_tool("view_image", {"name": "plan.png"})
            assert isinstance(json.loads(viewed.content[1].text)["remaining_seconds"], int)

    asyncio.run(readonly_time_is_visible())

    legacy_child, legacy_hash = prepare_detail_observation(
        Toolkit(run), "Legacy direct setup remains untimed.", ["plan.png"], "detail_02")
    assert "deadline_epoch" not in json.loads((legacy_child / "inputs.json").read_text())
    assert legacy_hash == digest(legacy_child / "inputs.json")

    long_run = _run_with_one_image(tmp_path / "long")
    long_manifest = json.loads((long_run / "inputs.json").read_text())
    long_manifest["deadline_epoch"] = time.time() + 900
    (long_run / "inputs.json").write_text(json.dumps(long_manifest), encoding="utf-8")
    long_calls = []

    def long_subscription(child, prompt, **kwargs):
        long_calls.append((child, kwargs))
        return {"returncode": 0, "result": {"is_error": False, "result": "mark"}}

    review_detail_observation(Toolkit(long_run), "Check this one mark.", ["plan.png"],
                              invoke=long_subscription)
    long_child, long_kwargs = long_calls[0]
    visible_deadline = json.loads((long_child / "inputs.json").read_text())["deadline_epoch"]
    assert 0 < visible_deadline - time.time() <= 240
    assert 0 < long_kwargs["timeout"] <= 240


def test_detail_review_keeps_partial_text_but_marks_timeout_or_error_unfinished(tmp_path):
    run = _run_with_one_image(tmp_path)

    def interrupted_subscription(*args, **kwargs):
        return {"timed_out":True, "returncode":1,
                "result":{"is_error":False, "result":"partial observation"}}

    response = review_detail_observation(Toolkit(run), "Check the visible mark.", ["plan.png"],
                                         invoke=interrupted_subscription)
    assert response["result"] == "partial observation"
    assert response["timed_out"] and response["completed"] is False
    assert response["is_error"] is False

    empty_run = _run_with_one_image(tmp_path / "empty")
    response = review_detail_observation(Toolkit(empty_run), "Check the visible mark.", ["plan.png"],
                                         invoke=lambda *args, **kwargs: {"returncode":1})
    assert response["result"] == "No completed answer"
    assert response["is_error"] and response["completed"] is False


def test_detail_review_requested_time_limits_worker_without_spending_parent_reserve(tmp_path):
    import pytest

    run = _run_with_one_image(tmp_path)
    manifest = json.loads((run / "inputs.json").read_text())
    manifest["deadline_epoch"] = time.time() + 600
    (run / "inputs.json").write_text(json.dumps(manifest))
    calls = []

    def local_worker(child, prompt, **kwargs):
        calls.append(kwargs)
        deadline = json.loads((child / "inputs.json").read_text())["deadline_epoch"]
        assert 0 < deadline - time.time() <= 60
        assert 0 < kwargs["timeout"] <= 60
        return {"returncode": 0, "result": {"is_error": False, "result": "local observation"}}

    response = review_detail_observation(Toolkit(run), "Inspect one local mark.", ["plan.png"],
                                        timeout_seconds=60, invoke=local_worker)
    assert response["completed"] and len(calls) == 1
    for invalid in [True, 0, 14, 241, float("nan"), float("inf")]:
        with pytest.raises(ValueError, match="timeout_seconds"):
            review_detail_observation(Toolkit(run), "No invocation.", ["plan.png"],
                                      timeout_seconds=invalid, invoke=local_worker)
    assert len(calls) == 1

    async def tool_schema_exposes_budget():
        async with _server_session(run, readonly=False) as session:
            tool = next(t for t in (await session.list_tools()).tools if t.name == "review_detail")
            assert tool.inputSchema["properties"]["timeout_seconds"]["default"] == 120
    asyncio.run(tool_schema_exposes_budget())


def test_normal_stdio_builds_candidate_and_returns_plan_image(tmp_path):
    from src.agent.geometry.source_plan_view import render_source_plan

    def check_automatic_plan(run, call):
        result = _json_result(call)
        assert result["source_plan_errors"] == []
        assert len(result["source_plan_views"]) == 1
        metadata = result["source_plan_views"][0]
        source = json.loads((run / result["candidate"] / "source_model.json").read_text())
        replay, evidence = render_source_plan(source, "F1")
        assert all(metadata[key] == value for key, value in evidence.items())
        images = [block for block in call.content if block.type == "image"]
        assert len(images) == 1
        actual = Image.open(io.BytesIO(base64.b64decode(images[0].data)))
        assert actual.tobytes() == replay.tobytes()
        assert Image.open(run / metadata["plan_image"]).tobytes() == replay.tobytes()
        return result, actual.tobytes()

    async def scenario():
        run = _run_with_one_image(tmp_path)
        async with _server_session(run, readonly=False) as session:
            tools = {tool.name for tool in (await session.list_tools()).tools}
            assert "build_bim" in tools and "view_candidate" in tools
            built, initial_pixels = check_automatic_plan(run, await session.call_tool(
                "build_bim", {"proposal_json": _two_room_proposal()}))
            assert built["candidate"] == "candidate_01"
            assert built["source_geometry_ready"]
            assert built["source_image_projections"] == [] and built["projection_errors"] == []
            viewed = await session.call_tool("view_candidate", {"candidate": built["candidate"], "floor_id": "F1"})
            assert not viewed.isError
            assert viewed.content[0].type == "image"
            assert viewed.content[0].mimeType == "image/png"
            assert (run / "candidate_01" / "source_model.json").exists()
            original = (run / "candidate_01" / "proposal.json").read_bytes()
            revised, revised_pixels = check_automatic_plan(run, await session.call_tool("revise_bim", {
                "candidate": "candidate_01",
                "operations_json": json.dumps([{"op":"reflect", "axis":"x", "reason":"synthetic frame reflection"}]),
            }))
            assert revised_pixels != initial_pixels
            assert Image.open(run / built["source_plan_views"][0]["plan_image"]).tobytes() == initial_pixels
            assert revised["candidate"] == "candidate_02"
            assert revised["source_geometry_ready"]
            assert (run / "candidate_01" / "proposal.json").read_bytes() == original
            inspected = _json_result(await session.call_tool("inspect_candidate", {"candidate":"candidate_02"}))
            assert inspected["proposal"]["geometry"]["floors"][0]["cells"][0]["x"] == [3, 6]
            review = {"floor_id":"F1", "kind":"door", "image":"plan.png", "coverage":"complete",
                      "marks":[{"mark_id":"m1", "box":[1,1,6,7], "opening_ids":["door"],
                                "space_ids":["left","right"], "basis":"visible", "note":"synthetic aperture"}]}
            before = (run / "candidate_02/source_model.json").read_bytes()
            checked = _json_result(await session.call_tool("check_openings", {
                "candidate":"candidate_02", "review_json":json.dumps(review)}))
            assert checked["drawing_fidelity"] == "not_evaluated"
            saved = json.loads((run / checked["review_file"]).read_text())
            assert saved["observations"] == review
            assert (run / "candidate_02/source_model.json").read_bytes() == before
            assert not checked["findings"]
            assert "opening_reviews/review_001.json" == checked["review_file"]
            empty_north = _json_result(await session.call_tool("check_openings", {
                "candidate": "candidate_02", "review_json": json.dumps({
                    "floor_id": "F1", "kind": "window", "image": "plan.png",
                    "facade": "North", "coverage": "complete", "marks": []})}))
            assert empty_north["review_scope"]["facade"] == "North"
            assert empty_north["model_opening_ids"] == []

            overlaid = await session.call_tool("overlay_candidate", {
                "candidate":"candidate_02", "image":"plan.png", "floor_id":"F1",
                "x_anchors":[[0,0],[11,6]], "y_anchors":[[7,0],[0,4]],
                "basis":"synthetic image extent represents the building footprint",
                "box":[1,1,11,7]})
            assert not overlaid.isError
            assert overlaid.content[0].type == "image"
            overlay_info = json.loads(overlaid.content[1].text)
            assert overlay_info["box_original_pixels"] == [1,1,11,7]
            assert overlay_info["returned_size"] == [10,6]
            assert overlay_info["drawing_fidelity"] == "not_evaluated"
            assert (run / overlay_info["overlay_image"]).is_file()
            assert (run / "candidate_02/source_model.json").read_bytes() == before

            finished = _full_delivery_result(run, await session.call_tool("finish_bim", {"candidate":"candidate_02"}))
            assert finished["candidate"] == "candidate_02"
            assert finished["selection_origin"] == "agent_selected"
            assert finished["drawing_fidelity"] == "not_evaluated"
            assert (run / "delivery.html").is_file()
            assert "逐立面回查范围" in (run / "delivery.html").read_text()
            north_windows = next(s for s in finished["facade_review_scopes"]
                                 if s["floor_id"] == "F1" and s["facade"] == "North" and s["kind"] == "window")
            assert north_windows["review_status"] == "consistent_with_supplied_observations"
            all_windows = next(s for s in finished["opening_review_scopes"]
                               if s["floor_id"] == "F1" and s["kind"] == "window")
            assert all_windows["review_status"] == "partial"
            assert json.loads((run / "delivery.json").read_text())["source_model_sha256"] == checked["source_model_sha256"]
            assert json.loads((run / "delivery_selection.json").read_text())["candidate"] == "candidate_02"

            # Move the shared wall through the public MCP path. Both room sides
            # and the door on that wall move, while prior candidates/reviews stay.
            moved = _json_result(await session.call_tool("revise_bim", {
                "candidate": "candidate_02", "operations_json": json.dumps([{
                    "op": "move_shared_wall", "space_ids": ["left", "right"],
                    "coordinate_m": 3.5, "reason": "synthetic observed partition",
                    "source_refs": ["plan.png: synthetic wall observation"]}])}))
            assert moved["candidate"] == "candidate_03" and moved["source_geometry_ready"]
            assert (run / "candidate_02/source_model.json").read_bytes() == before
            moved_proposal = json.loads((run / "candidate_03/proposal.json").read_text())
            rooms = {cell["id"]: cell for cell in moved_proposal["geometry"]["floors"][0]["cells"]}
            assert rooms["left"]["x"] == [3.5, 6] and rooms["right"]["x"] == [0, 3.5]
            assert moved_proposal["geometry"]["openings"][0]["p1"][0] == 3.5
            assert moved_proposal["geometry"]["openings"][0]["p2"][0] == 3.5
            moved_delivery = _full_delivery_result(run, await session.call_tool("finish_bim", {"candidate":"candidate_03"}))
            assert moved_delivery["source_model_sha256"] != finished["source_model_sha256"]
            assert len(moved_delivery["stale_reviews"]) == 2
            assert all(scope["review_status"] == "not_reviewed"
                       for scope in moved_delivery["opening_review_scopes"])

    asyncio.run(scenario())


def test_registered_source_overlay_feedback_reuses_only_explicit_image_floor_calibrations(tmp_path):
    from src.agent.execution.source_proposal import export_source_proposal

    async def scenario():
        run = _run_with_one_image(tmp_path)
        _add_image(run, "upper.png", "black")
        proposal_json = _two_floor_proposal()
        export_source_proposal(json.loads(proposal_json), run / "seed")
        async with _server_session(run, readonly=False) as session:
            # A model chooses this frame explicitly on a recovered seed.  Nothing
            # registers the other image/floor until the model calls this tool.
            seed_overlay = await session.call_tool("overlay_candidate", {
                "candidate": "seed", "image": "plan.png", "floor_id": "F1",
                "x_anchors": [[0, 0], [11, 6]], "y_anchors": [[7, 0], [0, 4]],
                "basis": "synthetic seed frame"})
            assert not seed_overlay.isError and seed_overlay.content[0].type == "image"
            seed_info = json.loads(seed_overlay.content[1].text)
            assert seed_info["registered_calibration"]["calibration_id"] == "calibration_001"
            seed_overlay_path = run / seed_info["overlay_image"]
            seed_overlay_bytes = seed_overlay_path.read_bytes()
            seed_overlay_metadata = (seed_overlay_path.with_suffix(".json")).read_bytes()

            # build_bim retains its structured JSON and now returns the actual
            # automatic overlay image with the source-bound projection metadata.
            first_call = await session.call_tool("build_bim", {"proposal_json": proposal_json})
            first = _json_result(first_call)
            assert {row["floor_id"] for row in first["source_plan_views"]} == {"F1", "F2"}
            assert len([block for block in first_call.content if block.type == "image"]) == 3
            assert first["source_image_projections"], first["projection_errors"]
            assert first["projection_errors"] == []
            assert first_call.content[0].type == "image"
            first_projection = first["source_image_projections"]
            assert len(first_projection) == 1
            assert first_projection[0]["automatic_projection"] is True
            assert first_projection[0]["trigger_action"] == "build_bim"
            assert first_projection[0]["anchors"] == {"x": [[0.0, 0.0], [11.0, 6.0]],
                                                        "y": [[7.0, 0.0], [0.0, 4.0]]}
            assert first_projection[0]["reused_calibration"]["registered_by_candidate"] == "seed"
            assert first_projection[0]["source_model_sha256"] == json.loads(
                (run / "candidate_01/source_model.json").read_text())["source_model_sha256"]
            first_projection_bytes = (run / first_projection[0]["overlay_image"]).read_bytes()

            upper_overlay = await session.call_tool("overlay_candidate", {
                "candidate": "candidate_01", "image": "upper.png", "floor_id": "F2",
                "x_anchors": [[0, 0], [11, 6]], "y_anchors": [[7, 0], [0, 4]],
                "basis": "synthetic upper floor frame"})
            upper_info = json.loads(upper_overlay.content[1].text)
            assert upper_info["registered_calibration"]["calibration_id"] == "calibration_002"

            # A changed wall creates fresh source-bound views.  The old seed and
            # first-candidate projection artifacts stay byte-for-byte unchanged.
            second = _json_result(await session.call_tool("revise_bim", {
                "candidate": "candidate_01", "operations_json": json.dumps([{
                    "op": "move_shared_wall", "space_ids": ["left", "right"],
                    "coordinate_m": 3.5, "reason": "synthetic overlay correction",
                    "source_refs": ["plan.png: synthetic observation"]}])}))
            assert len(second["source_image_projections"]) == 2
            second_by_floor = {row["floor_id"]: row for row in second["source_image_projections"]}
            assert second_by_floor["F1"]["reused_calibration"]["calibration_id"] == "calibration_001"
            assert second_by_floor["F2"]["reused_calibration"]["calibration_id"] == "calibration_002"
            assert (run / second_by_floor["F1"]["overlay_image"]).read_bytes() != first_projection_bytes
            assert seed_overlay_path.read_bytes() == seed_overlay_bytes
            assert seed_overlay_path.with_suffix(".json").read_bytes() == seed_overlay_metadata

            # Recalibrating one exact image/floor supersedes only that pair.
            replaced = await session.call_tool("overlay_candidate", {
                "candidate": "candidate_02", "image": "plan.png", "floor_id": "F1",
                "x_anchors": [[0, 0], [10, 6]], "y_anchors": [[7, 0], [0, 4]],
                "basis": "synthetic corrected F1 frame"})
            assert json.loads(replaced.content[1].text)["registered_calibration"]["calibration_id"] == "calibration_003"
            third = _json_result(await session.call_tool("revise_bim", {
                "candidate": "candidate_02", "operations_json": json.dumps([{
                    "op": "reflect", "axis": "x", "reason": "synthetic new source"}])}))
            third_by_floor = {row["floor_id"]: row for row in third["source_image_projections"]}
            assert third_by_floor["F1"]["reused_calibration"]["calibration_id"] == "calibration_003"
            assert third_by_floor["F1"]["anchors"]["x"] == [[0.0, 0.0], [10.0, 6.0]]
            assert third_by_floor["F2"]["reused_calibration"]["calibration_id"] == "calibration_002"

            # Hash validation happens through Toolkit.image_path before a failed
            # explicit projection can affect the saved source candidate.
            before_failure = (run / "candidate_03/source_model.json").read_bytes()
            Image.new("RGB", (12, 8), "red").save(run / "images/plan.png")
            failed = await session.call_tool("overlay_candidate", {
                "candidate": "candidate_03", "image": "plan.png", "floor_id": "F1",
                "x_anchors": [[0, 0], [11, 6]], "y_anchors": [[7, 0], [0, 4]],
                "basis": "must reject changed input"})
            assert "input image changed" in _error_text(failed)
            assert (run / "candidate_03/source_model.json").read_bytes() == before_failure

            # Automatic reuse reports the same input failure, but still returns
            # the candidate and the unaffected F2 image instead of hiding feedback.
            fourth_call = await session.call_tool("build_bim", {"proposal_json": proposal_json})
            fourth = _json_result(fourth_call)
            assert fourth["candidate"] == "candidate_04"
            assert len(fourth["source_image_projections"]) == 1
            assert fourth["source_image_projections"][0]["floor_id"] == "F2"
            assert len(fourth["projection_errors"]) == 1
            assert fourth["projection_errors"][0]["floor_id"] == "F1"
            assert "input image changed" in fourth["projection_errors"][0]["error"]
            assert fourth_call.content[0].type == "image"
            delivery = _full_delivery_result(run, await session.call_tool("finish_bim", {"candidate": "candidate_04"}))
            feedback = delivery["source_image_feedback"]
            assert feedback["current_source_projections"][0]["floor_id"] == "F2"
            assert feedback["old_source_projections"]
            assert feedback["floors_without_registered_views"] == []
            assert feedback["calibration_independently_verified"] is False
            assert delivery["drawing_fidelity"] == "not_evaluated"

    asyncio.run(scenario())


@pytest.mark.parametrize("effort", [None, "low", "medium"])
def test_recovery_imports_only_proposal_and_rebuilds_production_checks(tmp_path, monkeypatch, effort):
    from scripts.tool_scripts import run_bim_agent as runner
    from src.agent.execution.source_proposal import export_source_proposal
    source = tmp_path / "old_candidate"
    proposal = json.loads(_two_room_proposal())
    export_source_proposal(proposal, source)
    old_report = source / "report.json"
    # An old directory may later acquire evaluator output. It must not enter
    # a generating model's new recovery workspace.
    old_report.write_text(json.dumps({"independent_evaluation":"DO_NOT_EXPOSE"}))
    images = tmp_path / "images"
    images.mkdir()
    Image.new("RGB", (12, 8), "white").save(images / "plan.png")

    def offline_subscription(run, prompt, **kwargs):
        assert kwargs["effort"] == effort
        manifest = json.loads((run / "inputs.json").read_text())
        assert manifest["input_mode"] == "saved_candidate_recovery"
        assert json.loads((run / "seed/proposal.json").read_text()) == proposal
        assert "DO_NOT_EXPOSE" not in (run / "seed/report.json").read_text()
        assert json.loads((run / "seed/report.json").read_text())["source_geometry_ready"]
        receipt = {"elapsed_seconds": 0, "result":{"is_error":False,"total_cost_usd":0}}
        runner.dump(run / "agent_receipt.json", receipt)
        return receipt

    monkeypatch.setattr(runner, "subscription", offline_subscription)
    args = SimpleNamespace(images=images, out=tmp_path / "recovery", scope="synthetic recovery",
                           timeout=30, resume_candidate=source)
    if effort is not None:
        args.effort = effort
    runner.run_experiment(args)
    assert json.loads(old_report.read_text())["independent_evaluation"] == "DO_NOT_EXPOSE"
    delivery = json.loads((args.out / "delivery.json").read_text())
    assert delivery["candidate"] == "seed"
    assert delivery["selection_origin"] == "latest_saved_fallback_not_agent_selected"
    assert delivery["drawing_fidelity"] == "not_evaluated"
    assert "DO_NOT_EXPOSE" not in json.dumps(delivery)


def _ended_or_zombie(pid: int) -> bool:
    """A zombie has stopped executing; init will reap an orphan shortly."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            assert ctypes.get_last_error() == 87, "could not query child process"
            return True
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        try:
            code = wintypes.DWORD()
            assert kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value != 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                           text=True, check=False).stdout.strip()
    return state.startswith("Z")


def test_interrupted_run_keeps_candidate_and_labels_delivery_incomplete(tmp_path, monkeypatch):
    from scripts.tool_scripts import run_bim_agent as runner
    images = tmp_path / "images"
    images.mkdir()
    Image.new("RGB", (12, 8), "white").save(images / "plan.png")

    def interrupted_subscription(run, prompt, **kwargs):
        runner.Toolkit(run).build(json.loads(_two_room_proposal()))
        receipt = {"elapsed_seconds": 2, "returncode": 1,
                   "result":{"is_error":True,"total_cost_usd":0,
                             "result":"You've hit your session limit"}}
        runner.dump(run / "agent_receipt.json", receipt)
        return receipt

    monkeypatch.setattr(runner, "subscription", interrupted_subscription)
    args = SimpleNamespace(images=images, out=tmp_path / "interrupted", scope="synthetic run",
                           timeout=30, resume_candidate=None)
    runner.run_experiment(args)
    delivery = json.loads((args.out / "delivery.json").read_text())
    summary = json.loads((args.out / "summary.json").read_text())
    assert summary["has_viewable_candidate"] and not summary["agent_response_completed"]
    assert delivery["candidate"] == "candidate_01"
    assert delivery["generation_status"]["state"] == "interrupted"
    assert delivery["generation_status"]["error"] == "You've hit your session limit"
    assert "未正常完成" in (args.out / "delivery.html").read_text()
    assert (args.out / "candidate_01/viewer.html").exists()
    assert delivery["drawing_fidelity"] == "not_evaluated"


def test_terminate_subscription_stops_parent_and_nested_session_child(tmp_path):
    child_pid_path = tmp_path / "nested-child.pid"
    parent_code = """
from pathlib import Path
import subprocess
import sys
import time

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
Path(sys.argv[1]).write_text(str(child.pid), encoding="utf-8")
time.sleep(60)
"""
    parent = subprocess.Popen([sys.executable, "-c", parent_code, str(child_pid_path)],
                              start_new_session=True)
    child_pid = None
    try:
        deadline = time.monotonic() + 5
        while not child_pid_path.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert child_pid_path.exists(), "nested session did not start"
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))

        terminate_subscription(parent)
        assert parent.poll() is not None
        deadline = time.monotonic() + 3
        while not _ended_or_zombie(child_pid) and time.monotonic() < deadline:
            time.sleep(.02)
        assert _ended_or_zombie(child_pid), "nested sleep process survived termination"
    finally:
        for pid in (parent.pid, child_pid):
            if pid is None or _ended_or_zombie(pid):
                continue
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=15)
                continue
            try:
                os.killpg(os.getpgid(pid), 9)
            except ProcessLookupError:
                pass


def test_parametric_tool_stdio_builds_repeated_spaces_and_preserves_compact_plan(tmp_path):
    run = _run_with_one_image(tmp_path)
    compact = {
        'templates': {'t': {'footprint': [[0,0],[6,0],[6,4],[0,4]],
            'spaces': [{'id':'room','role':'office','rect':[0,0,6,4],
                        'source_refs':['synthetic hypothesis']}],
            'window_rows': [{'id':'w','facade':'West','plane':0,'spans':[[1,2]],
                             'z':[1,2],'source_refs':['synthetic observation']}]}},
        'instances':[{'id':'A','template':'t','z':0,'height':3},
                     {'id':'B','template':'t','z':3,'height':3}],
        'assumptions':['test'], 'unresolved':['interiors unknown']}
    async def exercise():
        async with _server_session(run, readonly=False) as session:
            result=await session.call_tool('build_parametric_bim', {'plan_json':json.dumps(compact)})
            payload=_json_result(result)
            assert payload['source_geometry_ready'],payload
            assert payload['counts']['spaces']==2
            assert len([c for c in result.content if c.type=='image'])==1
            restored=await session.call_tool('inspect_parametric_plan', {'candidate':payload['candidate'], 'full':True})
            restored_data = _json_result(restored)
            assert restored_data['value']==compact
            assert payload['parametric']['plan_sha256'] == restored_data['plan_sha256']
            assert payload['draft'] and len(payload['draft_sha256']) == 64
            revised = _json_result(await session.call_tool('patch_parametric_bim', {
                'candidate': payload['candidate'], 'expected_plan_sha256': restored_data['plan_sha256'],
                'operations_json': json.dumps([{'op': 'replace', 'path': '/instances/1/height',
                    'value': 3.2, 'reason': 'Explicit synthetic upper height correction',
                    'source_refs': ['synthetic height observation']}])}))
            assert revised['saved_candidate'] == 'candidate_02', revised
            assert revised['parametric']['base']['plan_sha256'] == restored_data['plan_sha256']
            assert revised['draft'] != payload['draft'] and revised['draft_sha256'] != payload['draft_sha256']
            report = json.loads((run / revised['candidate'] / 'report.json').read_bytes())
            assert report['parametric'] == revised['parametric']
            assert report['parametric']['source_file_sha256'] == digest(run / revised['candidate'] / 'source_model.json')
            current = _json_result(await session.call_tool('inspect_parametric_plan', {
                'candidate': revised['candidate'], 'pointer': '/instances/1/height'}))
            assert current['status'] == 'current' and current['value'] == 3.2
            failed=await session.call_tool('build_parametric_bim', {'plan_json':'{"oops":1}'})
            assert 'missing fields' in _json_result(failed)['error']
        async with _server_session(run, readonly=True) as session:
            names={t.name for t in (await session.list_tools()).tools}
            assert 'build_parametric_bim' not in names
            assert 'inspect_parametric_plan' not in names
            assert 'patch_parametric_bim' not in names
    asyncio.run(exercise())


def test_source_build_feedback_and_finish_preserve_same_level_annex_connection(tmp_path):
    run=_run_with_one_image(tmp_path)
    proposal=json.loads(_two_room_proposal())
    geometry=proposal['geometry']
    floor=geometry['floors'][0]
    left,right=floor['cells']
    floor['cells']=[left]
    floor['footprint']={'vertices':[[0,0],[3,0],[3,4],[0,4]]}
    geometry['floors'].append({'name':'ANNEX','z_floor':0,'ceiling_height':3,
        'cells':[right],'footprint':{'vertices':[[3,0],[6,0],[6,4],[3,4]]}})
    async def exercise():
        async with _server_session(run,readonly=False) as session:
            result=_json_result(await session.call_tool('build_bim',{'proposal_json':json.dumps(proposal)}))
            assert result['source_geometry_ready'],result
            assert len(result['source_plan_views'])==2
            summary_openings = [dict(zip(f['columns'], row))
                for f in result['opening_inventory']['floors'] for row in f['rows']]
            assert any(len(o.get('floor_ids') or []) == 2 for o in summary_openings)
            complete = json.loads((run / result['details_file']).read_text())
            assert any(len(o.get('floor_ids', [])) == 2
                for f in complete['opening_inventory']['floors'] for o in f['openings'])
            finished=_json_result(await session.call_tool('finish_bim',{'candidate':result['candidate']}))
            assert finished['viewer_exists']
    asyncio.run(exercise())


# The compact-plan state/patch contract is tested without a model or MCP process.
class _ParametricFunctionToolkit:
    def __init__(self, run):
        self.run = run
        self.run.mkdir()
        self.readonly = False
        self.reject = False
        self.calls = []
        self.logs = []

    def remaining_seconds(self):
        return None

    def log(self, action, data):
        self.logs.append((action, data))

    def candidate_path(self, candidate):
        path = self.run / candidate
        if not path.is_dir():
            raise ValueError('unknown candidate')
        return path

    def build(self, proposal, *, action, parent, feedback_parent=None, feedback_parent_error=None,
              architectural_regularization=None):
        from scripts.tool_scripts.bim_agent_saved_result import saved_result
        self.calls.append(dict(proposal=proposal, action=action, parent=parent,
                               feedback_parent=feedback_parent,
                               feedback_parent_error=feedback_parent_error,
                               architectural_regularization=architectural_regularization))
        if self.reject:
            return saved_result({'error': 'synthetic source constraint',
                                 'error_stage': 'source_hard_constraints'}, audit_written=True)
        candidate = f'candidate_{len(list(self.run.glob("candidate_*"))) + 1:02d}'
        folder = self.run / candidate
        folder.mkdir()
        (folder / 'proposal.json').write_text(json.dumps(proposal), encoding='utf-8')
        source = {'generation': {'provenance': {'parent_candidate': parent}},
                  'geometry': proposal['geometry']}
        (folder / 'source_model.json').write_text(json.dumps(source), encoding='utf-8')
        return saved_result({'candidate': candidate, 'source_geometry_ready': True,
                             'opening_inventory': {'large': 'kept in actual shared builder reports'}},
                            candidate=candidate, created=[candidate], geometry_applied=True, audit_written=True)


def _parametric_function_plan():
    return {'templates': {'office': {'footprint': [[0,0],[6,0],[6,4],[0,4]],
             'spaces': [{'id': 'room', 'role': 'office', 'rect': [0,0,6,4],
                         'source_refs': ['synthetic declared hypothesis']}]}},
            'instances': [{'id': 'A', 'template': 'office', 'z': 0, 'height': 3}],
            'assumptions': ['test'], 'unresolved': ['actual interiors unknown']}


def _parametric_function_op(op, path, value=None):
    result = {'op': op, 'path': path}
    if op != 'test':
        result.update(reason='explicit synthetic correction', source_refs=['test observation'])
    if op != 'remove':
        result['value'] = value
    return result


def _parametric_file_hashes(path):
    return {p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob('*') if p.is_file()}


def test_parametric_function_build_inspect_and_patch_preserve_exact_base(tmp_path):
    from scripts.tool_scripts.bim_agent_parametric import build_parametric, inspect_parametric, patch_parametric
    toolkit = _ParametricFunctionToolkit(tmp_path / 'run')
    plan = _parametric_function_plan()
    raw = json.dumps(plan, indent=2)
    first = build_parametric(toolkit, raw)
    assert (toolkit.run / first['draft']).read_text(encoding='utf-8') == raw
    assert 'opening_inventory' not in first
    state = inspect_parametric(toolkit, first['candidate'])
    assert state['status'] == 'current' and state['patch_allowed']
    assert state['summary']['expanded_instance_count'] == 1
    assert 'value' not in state
    assert inspect_parametric(toolkit, first['candidate'], pointer='/templates/office')['value'] == plan['templates']['office']
    original_hashes = _parametric_file_hashes(toolkit.run)
    second = patch_parametric(toolkit, json.dumps([
        _parametric_function_op('test', '/instances/0/height', 3),
        _parametric_function_op('replace', '/instances/0/height', 3.5),
        _parametric_function_op('add', '/assumptions/-', 'new explicit assumption')]),
        candidate=first['candidate'], expected_sha=state['plan_sha256'])
    assert second['saved_candidate'] == 'candidate_02'
    assert toolkit.calls[-1]['parent'] == first['candidate']
    assert second['parametric']['base']['source_file_sha256'] == first['parametric']['source_file_sha256']
    current = inspect_parametric(toolkit, second['candidate'], full=True)
    expected = _parametric_function_plan()
    expected['instances'][0]['height'] = 3.5
    expected['assumptions'].append('new explicit assumption')
    assert current['value'] == expected
    assert all(_parametric_file_hashes(toolkit.run)[name] == sha for name, sha in original_hashes.items())
    patch = json.loads((toolkit.run / second['parametric']['patch']['file']).read_bytes())
    assert patch['base']['plan_sha256'] == state['plan_sha256']
    assert patch['new_draft_sha256'] == second['draft_sha256']


@pytest.mark.parametrize('reject_stage', ['expansion', 'source'])
def test_parametric_function_failed_draft_can_be_patched(tmp_path, reject_stage):
    from scripts.tool_scripts.bim_agent_parametric import build_parametric, inspect_parametric, patch_parametric
    toolkit = _ParametricFunctionToolkit(tmp_path / 'run')
    plan = _parametric_function_plan()
    if reject_stage == 'expansion':
        del plan['unresolved']
    else:
        toolkit.reject = True
    first = build_parametric(toolkit, json.dumps(plan))
    assert first['saved_candidate'] is None and first['error']
    base = inspect_parametric(toolkit, draft=first['draft'])
    toolkit.reject = False
    op = (_parametric_function_op('add', '/unresolved', ['unknown']) if reject_stage == 'expansion'
          else _parametric_function_op('replace', '/instances/0/height', 3.5))
    second = patch_parametric(toolkit, json.dumps([op]), draft=first['draft'], expected_sha=base['plan_sha256'])
    assert second['saved_candidate'] == 'candidate_01'
    assert toolkit.calls[-1]['parent'] is None
    assert json.loads((toolkit.run / first['draft']).read_bytes()) == plan


@pytest.mark.parametrize('failure', ['stale_sha', 'test', 'traversal', 'unknown_root', 'extra_field',
                                   'leading_zero', 'scalar', 'missing', 'missing_reason', 'empty_refs'])
def test_parametric_function_rejected_patch_is_atomic(tmp_path, failure):
    from scripts.tool_scripts.bim_agent_parametric import build_parametric, inspect_parametric, patch_parametric
    toolkit = _ParametricFunctionToolkit(tmp_path / 'run')
    first = build_parametric(toolkit, json.dumps(_parametric_function_plan()))
    state = inspect_parametric(toolkit, first['candidate'])
    sha = state['plan_sha256']
    op = _parametric_function_op('replace', '/instances/0/height', 4)
    if failure == 'stale_sha': sha = '0' * 64
    elif failure == 'test': op = _parametric_function_op('test', '/instances/0/height', True)
    elif failure == 'traversal': op['path'] = '/../proposal.json'
    elif failure == 'unknown_root': op['path'] = '/source_model'
    elif failure == 'extra_field': op['unknown'] = 'no'
    elif failure == 'leading_zero': op['path'] = '/instances/00/height'
    elif failure == 'scalar': op['path'] = '/instances/0/height/x'
    elif failure == 'missing': op['path'] = '/instances/0/missing'
    elif failure == 'missing_reason': del op['reason']
    elif failure == 'empty_refs': op['source_refs'] = []
    before = _parametric_file_hashes(toolkit.run)
    result = patch_parametric(toolkit, json.dumps([
        _parametric_function_op('add', '/assumptions/-', 'must not apply'), op]),
        candidate=first['candidate'], expected_sha=sha)
    assert result['saved_candidate'] is None and result['error']
    assert len(toolkit.calls) == 1
    assert _parametric_file_hashes(toolkit.run) == before


def test_parametric_function_generic_revision_reports_stale_without_overwriting(tmp_path):
    from scripts.tool_scripts.bim_agent_parametric import build_parametric, inspect_parametric, patch_parametric
    toolkit = _ParametricFunctionToolkit(tmp_path / 'run')
    first = build_parametric(toolkit, json.dumps(_parametric_function_plan()))
    generic = toolkit.build({'geometry': {'changed_only_in_source': True}}, action='revise_bim', parent=first['candidate'])
    before = _parametric_file_hashes(toolkit.run)
    inspected = inspect_parametric(toolkit, generic['candidate'], full=True)
    assert inspected['status'] == 'stale_lineage' and not inspected['exact_plan']
    assert inspected['nearest_parametric']['candidate'] == first['candidate']
    assert inspected['nearest_parametric']['reference_only'] and 'value' not in inspected
    assert inspected['reference_selection']['reference_only']
    assert inspected['reference_selection']['value'] == _parametric_function_plan()
    result = patch_parametric(toolkit, json.dumps([_parametric_function_op('replace', '/instances/0/height', 4)]),
        candidate=generic['candidate'], expected_sha=first['parametric']['plan_sha256'])
    assert 'no exact bound' in result['error']
    assert _parametric_file_hashes(toolkit.run) == before


@pytest.mark.parametrize('artifact', ['source_model.json', 'proposal.json', 'parametric_plan.json',
                                    'parametric_state.json', 'draft', 'patch'])
def test_parametric_function_binding_detects_changed_bytes(tmp_path, artifact):
    from scripts.tool_scripts.bim_agent_parametric import build_parametric, inspect_parametric, patch_parametric
    toolkit = _ParametricFunctionToolkit(tmp_path / 'run')
    first = build_parametric(toolkit, json.dumps(_parametric_function_plan()))
    second = patch_parametric(toolkit, json.dumps([_parametric_function_op('replace', '/instances/0/height', 4)]),
        candidate=first['candidate'], expected_sha=first['parametric']['plan_sha256'])
    path = (toolkit.run / second['draft'] if artifact == 'draft' else
            toolkit.run / second['parametric']['patch']['file'] if artifact == 'patch' else
            toolkit.run / second['candidate'] / artifact)
    path.write_bytes(path.read_bytes() + b' ')
    state = inspect_parametric(toolkit, second['candidate'])
    assert state['status'] == 'stale' and not state['patch_allowed']
    count = len(toolkit.calls)
    result = patch_parametric(toolkit, json.dumps([_parametric_function_op('replace', '/instances/0/height', 5)]),
        candidate=second['candidate'], expected_sha=second['parametric']['plan_sha256'])
    assert result['error'] and len(toolkit.calls) == count


def test_parametric_function_large_node_can_be_read_completely_in_pages(tmp_path):
    from scripts.tool_scripts.bim_agent_parametric import build_parametric, inspect_parametric
    toolkit = _ParametricFunctionToolkit(tmp_path / 'run')
    plan = _parametric_function_plan()
    plan['assumptions'] = ['较长的观察依据' * 100]
    first = build_parametric(toolkit, json.dumps(plan))
    offset, parts, hashes = 0, [], set()
    while True:
        read = inspect_parametric(toolkit, first['candidate'], pointer='/assumptions', max_chars=71, offset=offset)
        assert read['fragment'] and len(read['serialized_json']) <= 71
        parts.append(read['serialized_json'])
        hashes.add(read['serialized_sha256'])
        offset = read['next_offset']
        if offset is None:
            break
    assert len(hashes) == 1
    assert json.loads(''.join(parts)) == plan['assumptions']
    assert inspect_parametric(toolkit, first['candidate'], max_chars=20)['summary']['summary_truncated']


def test_parametric_function_remove_escaped_pointer_and_readonly(tmp_path):
    from scripts.tool_scripts.bim_agent_parametric import build_parametric, inspect_parametric, patch_parametric
    toolkit = _ParametricFunctionToolkit(tmp_path / 'run')
    plan = _parametric_function_plan()
    plan['templates']['office/~example'] = plan['templates'].pop('office')
    plan['instances'][0]['template'] = 'office/~example'
    first = build_parametric(toolkit, json.dumps(plan))
    assert inspect_parametric(toolkit, first['candidate'], pointer='/templates/office~1~0example')['value']['spaces']
    result = patch_parametric(toolkit, json.dumps([_parametric_function_op('remove', '/assumptions/0')]),
        candidate=first['candidate'], expected_sha=first['parametric']['plan_sha256'])
    assert inspect_parametric(toolkit, result['candidate'], full=True)['value']['assumptions'] == []
    before = _parametric_file_hashes(toolkit.run)
    toolkit.readonly = True
    assert build_parametric(toolkit, json.dumps(plan))['error']
    assert patch_parametric(toolkit, '[]', candidate=first['candidate'],
                            expected_sha=first['parametric']['plan_sha256'])['error']
    assert _parametric_file_hashes(toolkit.run) == before


@pytest.mark.parametrize('raw', ['{"templates":{},"templates":{}}', '{"instances":[NaN]}', '{broken'])
def test_parametric_function_invalid_json_is_preserved_but_not_patchable(tmp_path, raw):
    from scripts.tool_scripts.bim_agent_parametric import build_parametric, inspect_parametric
    toolkit = _ParametricFunctionToolkit(tmp_path / 'run')
    result = build_parametric(toolkit, raw)
    assert result['error'] and result['saved_candidate'] is None
    assert (toolkit.run / result['draft']).read_text(encoding='utf-8') == raw
    assert not inspect_parametric(toolkit, draft=result['draft'])['patch_allowed']
    assert not toolkit.calls


def test_parametric_function_repeat_and_core_connection_patch_uses_shared_expansion(tmp_path):
    from scripts.tool_scripts.bim_agent_parametric import build_parametric, inspect_parametric, patch_parametric
    toolkit = _ParametricFunctionToolkit(tmp_path / 'run')
    plan = {'templates': {
        'core': {'footprint': [[0,0],[2,0],[2,4],[0,4]],
                 'spaces': [{'id': 'lift', 'role': 'shaft', 'rect': [0,0,2,4], 'source_refs': ['declared shaft']}]},
        'office': {'footprint': [[0,0],[6,0],[6,4],[0,4]],
                   'spaces': [{'id': 'room', 'role': 'office', 'rect': [2,0,6,4], 'source_refs': ['declared office']}]}
        }, 'instances': [{'id': 'CORE', 'template': 'core', 'z': 0, 'height': 6}],
        'repeats': [{'id': 'offices', 'template': 'office', 'id_prefix': 'F', 'start': 1,
                     'count': 2, 'z': 0, 'height': 3, 'spanning_space_ids': ['CORE:lift'],
                     'source_refs': ['explicitly repeated storeys']}],
        'core_connections': [{'id': 'lift', 'core_space_id': 'CORE:lift', 'space': 'room',
            'repeat_ids': ['offices'], 'p1': [2,1], 'p2': [2,2], 'z': [0,2.1],
            'source_refs': ['explicit service set']}],
        'assumptions': ['synthetic'], 'unresolved': ['interiors unknown']}
    first = build_parametric(toolkit, json.dumps(plan))
    assert first['saved_candidate'] == 'candidate_01', first
    assert len(toolkit.calls[0]['proposal']['geometry']['openings']) == 2
    result = patch_parametric(toolkit, json.dumps([
        _parametric_function_op('replace', '/repeats/0/count', 12),
        _parametric_function_op('replace', '/instances/0/height', 36)]),
        candidate=first['candidate'], expected_sha=first['parametric']['plan_sha256'])
    assert result['saved_candidate'] == 'candidate_02', result
    assert inspect_parametric(toolkit, result['candidate'])['summary']['expanded_instance_count'] == 13
    assert len(toolkit.calls[-1]['proposal']['geometry']['openings']) == 12
    saved_plan = inspect_parametric(toolkit, result['candidate'], full=True)['value']
    assert len(saved_plan['instances']) == 1 and len(saved_plan['repeats']) == 1
    assert len(saved_plan['core_connections']) == 1


def test_parametric_function_legacy_plan_is_readable_but_not_an_exact_adoption(tmp_path):
    from scripts.tool_scripts.bim_agent_parametric import build_parametric, inspect_parametric, patch_parametric
    toolkit = _ParametricFunctionToolkit(tmp_path / 'run')
    first = build_parametric(toolkit, json.dumps(_parametric_function_plan()))
    (toolkit.run / first['candidate'] / 'parametric_state.json').unlink()
    state = inspect_parametric(toolkit, first['candidate'], pointer='/instances')
    assert state['status'] == 'stale_lineage' and not state['patch_allowed']
    assert state['reference_selection']['value'] == _parametric_function_plan()['instances']
    rejected = patch_parametric(toolkit, json.dumps([_parametric_function_op('replace', '/instances/0/height', 4)]),
        candidate=first['candidate'], expected_sha=first['parametric']['plan_sha256'])
    assert rejected['error'] and len(toolkit.calls) == 1


def test_parametric_function_failed_patch_keeps_a_repairable_draft_and_patch_audit(tmp_path):
    from scripts.tool_scripts.bim_agent_parametric import build_parametric, inspect_parametric, patch_parametric
    toolkit = _ParametricFunctionToolkit(tmp_path / 'run')
    first = build_parametric(toolkit, json.dumps(_parametric_function_plan()))
    failed = patch_parametric(toolkit, json.dumps([_parametric_function_op('remove', '/unresolved')]),
        candidate=first['candidate'], expected_sha=first['parametric']['plan_sha256'])
    assert failed['saved_candidate'] is None and 'missing fields' in failed['error']
    assert failed['parametric']['base']['candidate'] == first['candidate']
    assert (toolkit.run / failed['parametric']['patch']['file']).is_file()
    draft = inspect_parametric(toolkit, draft=failed['draft'])
    fixed = patch_parametric(toolkit, json.dumps([_parametric_function_op('add', '/unresolved', ['explicit unknown'])]),
        draft=failed['draft'], expected_sha=draft['plan_sha256'])
    assert fixed['saved_candidate'] == 'candidate_02'
    assert fixed['parametric']['base']['draft'] == failed['draft']


def test_parametric_function_unbuilt_candidate_requires_its_draft_for_repair(tmp_path):
    from scripts.tool_scripts.bim_agent_parametric import build_parametric, inspect_parametric, patch_parametric
    toolkit = _ParametricFunctionToolkit(tmp_path / 'run')
    original = toolkit.build
    def without_source(proposal, *, action, parent):
        result = original(proposal, action=action, parent=parent)
        (toolkit.run / result['candidate'] / 'source_model.json').unlink()
        result['saved_candidate'] = None
        result['source_geometry_ready'] = False
        return result
    toolkit.build = without_source
    first = build_parametric(toolkit, json.dumps(_parametric_function_plan()))
    inspected = inspect_parametric(toolkit, first['candidate'])
    assert inspected['status'] == 'unbuilt' and not inspected['patch_allowed']
    operations = json.dumps([_parametric_function_op('replace', '/instances/0/height', 4)])
    rejected = patch_parametric(toolkit, operations, candidate=first['candidate'],
                                expected_sha=first['parametric']['plan_sha256'])
    assert 'no saved source' in rejected['error']
    toolkit.build = original
    repaired = patch_parametric(toolkit, operations, draft=first['draft'], expected_sha=first['draft_sha256'])
    assert repaired['saved_candidate'] == 'candidate_02' and toolkit.calls[-1]['parent'] is None


def test_parametric_function_real_toolkit_failed_export_repairs_preserved_draft(tmp_path):
    from scripts.tool_scripts.bim_agent_parametric import build_parametric, inspect_parametric, patch_parametric
    run = _run_with_one_image(tmp_path)
    toolkit = Toolkit(run)
    plan = _parametric_function_plan()
    plan['templates']['office']['spaces'][0]['role'] = 'invalid_unregistered_role'
    failed = build_parametric(toolkit, json.dumps(plan))
    assert failed['candidate'] == 'candidate_01' and failed['saved_candidate'] is None
    assert not (run / failed['candidate'] / 'source_model.json').exists()
    assert inspect_parametric(toolkit, failed['candidate'])['status'] == 'unbuilt'
    original = _parametric_file_hashes(run / failed['candidate'])
    repaired = patch_parametric(toolkit, json.dumps([
        _parametric_function_op('replace', '/templates/office/spaces/0/role', 'office')]),
        draft=failed['draft'], expected_sha=failed['draft_sha256'])
    assert repaired['saved_candidate'] == 'candidate_02' and repaired['source_geometry_ready']
    assert inspect_parametric(toolkit, repaired['candidate'])['status'] == 'current'
    report = json.loads((run / repaired['candidate'] / 'report.json').read_bytes())
    assert report['parametric']['base']['draft'] == failed['draft']
    assert report['parametric']['patch']['sha256'] == digest(run / repaired['parametric']['patch']['file'])
    assert (run / repaired['candidate'] / 'source_model.json').is_file()
    assert _parametric_file_hashes(run / failed['candidate']) == original


@pytest.mark.parametrize('artifact', ['candidate', 'draft'])
def test_parametric_function_corrupt_state_is_a_structured_refusal(tmp_path, artifact):
    from scripts.tool_scripts.bim_agent_parametric import build_parametric, inspect_parametric
    toolkit = _ParametricFunctionToolkit(tmp_path / 'run')
    first = build_parametric(toolkit, json.dumps(_parametric_function_plan()))
    path = ((toolkit.run / first['candidate'] / 'parametric_state.json') if artifact == 'candidate'
            else (toolkit.run / first['draft']).with_suffix('.state.json'))
    path.write_text('[]', encoding='utf-8')
    state = inspect_parametric(toolkit, first['candidate'])
    assert state['status'] == 'stale' and not state['patch_allowed'] and 'object' in state['error']


def test_parametric_function_failed_draft_chain_recovers_only_verified_feedback_parent(tmp_path):
    from scripts.tool_scripts.bim_agent_parametric import build_parametric, patch_parametric
    toolkit = _ParametricFunctionToolkit(tmp_path / 'run')
    first = build_parametric(toolkit, json.dumps(_parametric_function_plan()))
    failed = patch_parametric(toolkit, json.dumps([_parametric_function_op('remove', '/unresolved')]),
        candidate=first['candidate'], expected_sha=first['parametric']['plan_sha256'])
    failed_again = patch_parametric(toolkit, json.dumps([_parametric_function_op('remove', '/assumptions')]),
        draft=failed['draft'], expected_sha=failed['plan_sha256'])
    assert failed_again['saved_candidate'] is None
    # An advisory state hint cannot replace the immutable patch ancestry.
    path = (toolkit.run / failed_again['draft']).with_suffix('.state.json')
    state = json.loads(path.read_bytes())
    state['feedback_parent'] = {'candidate': 'candidate_999', 'source_file_sha256': '0'*64}
    path.write_text(json.dumps(state), encoding='utf-8')
    fixed = patch_parametric(toolkit, json.dumps([
        _parametric_function_op('add', '/unresolved', ['unknown']),
        _parametric_function_op('add', '/assumptions', ['explicit'])]),
        draft=failed_again['draft'], expected_sha=failed_again['plan_sha256'])
    assert fixed['saved_candidate'] == 'candidate_02', fixed
    assert toolkit.calls[-1]['parent'] is None  # No fabricated source lineage.
    reference = toolkit.calls[-1]['feedback_parent']
    assert reference == {'candidate': first['candidate'], **{key: first['parametric'][key]
        for key in ('plan_sha256', 'proposal_sha256', 'source_file_sha256')}}
    assert fixed['parametric']['feedback_parent'] == reference


@pytest.mark.parametrize('changed', ['source_model.json', 'parametric_plan.json', 'proposal.json'])
def test_parametric_function_stale_feedback_ancestor_falls_back_without_blocking_draft_repair(tmp_path, changed):
    from scripts.tool_scripts.bim_agent_parametric import build_parametric, patch_parametric
    toolkit = _ParametricFunctionToolkit(tmp_path / 'run')
    first = build_parametric(toolkit, json.dumps(_parametric_function_plan()))
    failed = patch_parametric(toolkit, json.dumps([_parametric_function_op('remove', '/unresolved')]),
        candidate=first['candidate'], expected_sha=first['parametric']['plan_sha256'])
    original = toolkit.run / first['candidate'] / changed
    original.write_bytes(original.read_bytes()+b' ')
    fixed = patch_parametric(toolkit, json.dumps([_parametric_function_op('add', '/unresolved', ['unknown'])]),
        draft=failed['draft'], expected_sha=failed['plan_sha256'])
    assert fixed['saved_candidate'] == 'candidate_02', fixed
    assert toolkit.calls[-1]['feedback_parent'] is None and toolkit.calls[-1]['parent'] is None
    assert 'changed' in fixed['parametric']['feedback_parent_unavailable']
    assert toolkit.calls[-1]['feedback_parent_error'] == fixed['parametric']['feedback_parent_unavailable']


def test_parametric_function_built_draft_uses_its_own_bound_source_not_older_ancestor(tmp_path):
    from scripts.tool_scripts.bim_agent_parametric import build_parametric, patch_parametric
    toolkit = _ParametricFunctionToolkit(tmp_path / 'run')
    first = build_parametric(toolkit, json.dumps(_parametric_function_plan()))
    second = patch_parametric(toolkit, json.dumps([
        _parametric_function_op('replace', '/instances/0/height', 4)]),
        candidate=first['candidate'], expected_sha=first['parametric']['plan_sha256'])
    reverted = patch_parametric(toolkit, json.dumps([
        _parametric_function_op('replace', '/instances/0/height', 3)]),
        draft=second['draft'], expected_sha=second['draft_sha256'])
    assert reverted['saved_candidate'] == 'candidate_03'
    assert toolkit.calls[-1]['parent'] is None
    assert toolkit.calls[-1]['feedback_parent']['candidate'] == second['candidate']


@pytest.mark.parametrize('yaw', [0, 17])
def test_mesh_generic_metadata_revisions_keep_adopted_local_frame_and_current_role(tmp_path, monkeypatch, yaw):
    import math
    from scripts.tool_scripts.bim_agent_parametric import build_parametric, inspect_parametric
    from tests.test_bim_agent_mesh import _prepare
    _, run, _ = _prepare(tmp_path, monkeypatch)
    toolkit = Toolkit(run)
    value = _parametric_function_plan()
    angle = math.radians(yaw)
    def world(x, y):
        return [.04+math.cos(angle)*x-math.sin(angle)*y,
                .04+math.sin(angle)*x+math.cos(angle)*y]
    ring = [world(0, 0), world(6.04, 0), world(6.04, 4.04), world(0, 4.04)]
    value['schema_version'] = 'source_polygon_v1'
    value['templates']['office']['footprint'] = ring
    room = value['templates']['office']['spaces'][0]
    room.pop('rect')
    room['polygon'] = ring
    value['modelling'] = {'frames': [{'id': 'wing', 'origin': [.04, .04],
        'yaw_degrees': yaw, 'templates': ['office']}]}
    first = build_parametric(toolkit, json.dumps(value))
    assert first['source_geometry_ready'], first
    initial = json.loads((run/first['candidate']/'source_model.json').read_bytes())
    second = toolkit.revise(first['candidate'], json.dumps([{'op': 'set_space_role',
        'space_id': 'A:room', 'role': 'meeting', 'basis': 'inferred', 'assumptions': ['Use is inferred'],
        'reason': 'explicit use hypothesis', 'source_refs': ['fixture']}]))
    assert second['source_geometry_ready'], second
    third = toolkit.revise(second['candidate'], json.dumps([{'op': 'set_notes',
        'assumptions': ['text-only follow-up'], 'unresolved': ['interiors unknown']}]))
    assert third['source_geometry_ready'], third
    for built in (second, third):
        current = json.loads((run/built['candidate']/'source_model.json').read_bytes())
        for actual, expected in zip(current['spaces'][0]['polygon'], initial['spaces'][0]['polygon']):
            assert actual == pytest.approx(expected, abs=1e-9, rel=0)
        assert current['spaces'][0]['role'] == 'conference/meeting/multipurpose'
        assert not inspect_parametric(toolkit, built['candidate'])['exact_plan']
