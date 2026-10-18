"""Current node contracts, with real saved views and the shared runtime entry."""

import asyncio
import copy
import hashlib
import json

import pytest
from PIL import Image, ImageDraw

from reader_delivery_fixtures import REVIEW, elevation_responses
from src.agent.runtime_roles.elevation import ElevationReaderTools
from src.agent.runtime_roles.entry import execute, parser
from src.agent.runtime_roles.guidance import COMPACT_ELEVATION_EXAMPLE
from src.agent.runtime_roles.submission import ReaderSubmission
from src.agent_runtime.adapter import ScriptedAdapter
from tests.test_role_readers import Frozen
from tests.test_role_submission import PassedTrial
from test_agent_runtime import versions


def elevation_image():
    image = Image.new("RGB", (1000, 800), "white")
    ImageDraw.Draw(image).rectangle((200, 380, 300, 567), outline="black")
    return image


def tools_at(root):
    (root / "images").mkdir(parents=True)
    image = root / "images/North.png"
    elevation_image().save(image)
    (root / "inputs.json").write_text(json.dumps({"images": {"North.png": {
        "sha256": hashlib.sha256(image.read_bytes()).hexdigest()}}}), encoding="utf-8")
    return ElevationReaderTools(Frozen(root), role_id="elevation_reader", image_name="North.png", target="North/F1")


def test_prepared_elevation_is_not_delivered_and_only_latest_view_can_be_accepted(tmp_path):
    async def scenario():
        tools = tools_at(tmp_path)
        first = await tools.call_tool("trial_elevation_reading", COMPACT_ELEVATION_EXAMPLE)
        assert not first["isError"], first
        assert tools.submission.read() is None
        assert {row["kind"] for row in first["structuredContent"]["views"]} == {"elevation", "overlay"}
        assert len([row for row in first["content"] if row["type"] == "image"]) == 2
        changed = copy.deepcopy(COMPACT_ELEVATION_EXAMPLE)
        changed["openings"][0]["opening_bbox"][1] += 25
        second = await tools.call_tool("trial_elevation_reading", changed)
        assert not second["isError"], second
        stale = await tools.call_tool("submit_elevation_reading", {
            "trial_id": first["structuredContent"]["trial_id"], "local_review": REVIEW})
        assert stale["isError"]
        current = {"trial_id": second["structuredContent"]["trial_id"], "local_review": REVIEW}
        assert (await tools.call_tool("submit_elevation_reading", {**current, "local_review": {"summary": " "}}))["isError"]
        accepted = await tools.call_tool("submit_elevation_reading", current)
        assert not accepted["isError"], accepted
        saved = tools.submission.read()
        assert saved["validation"]["local_review"]["artifact_sha256"] == saved["artifact_sha256"]
        assert saved["validation"]["local_review"]["independently_verified"] is False
        view = tmp_path / second["structuredContent"]["views"][1]["file"]
        view.write_bytes(b"tampered")
        with pytest.raises(ValueError, match="view hash"):
            tools.submission.read()
    asyncio.run(scenario())


def test_plan_requires_local_review_and_saved_result_views():
    trial = PassedTrial()
    submission = ReaderSubmission(role_id="plan_reader", image_name="plan.png", trial=trial)
    with pytest.raises(ValueError, match="local_review"):
        submission.submit({"trial_id": "latest"})
    trial.receipt["returned_images"] = []
    with pytest.raises(ValueError, match="saved result views"):
        submission.submit({"trial_id": "latest", "local_review": REVIEW})
    assert submission.read() is None


def test_elevation_overlay_uses_gray_original_and_retains_exact_adopted_geometry(tmp_path):
    async def scenario():
        tools = tools_at(tmp_path)
        path = tmp_path / 'images/North.png'
        image = elevation_image()
        image.putpixel((20, 60), (0, 200, 200))
        image.save(path)
        inputs = json.loads((tmp_path / 'inputs.json').read_bytes())
        inputs['images']['North.png']['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        (tmp_path / 'inputs.json').write_text(json.dumps(inputs), encoding='utf-8')
        tools = ElevationReaderTools(Frozen(tmp_path), role_id='elevation_reader', image_name='North.png', target='North/F1')
        result = await tools.call_tool('trial_elevation_reading', COMPACT_ELEVATION_EXAMPLE)
        assert not result['isError'], result
        views = result['structuredContent']['views']
        with Image.open(tmp_path / next(v['file'] for v in views if v['kind'] == 'overlay')) as overlay:
            r, g, b = overlay.getpixel((20, 60))
            assert r == g == b and r > 0
            assert overlay.size == image.size
        assert image.getpixel((20, 60)) == (0, 200, 200)
    asyncio.run(scenario())


def test_reader_unit_uses_exact_task_with_no_coordinator_or_auto_filled_siblings(tmp_path, monkeypatch):
    monkeypatch.setattr("src.agent.runtime_roles.entry.make_versions", lambda *a, **k: versions())
    monkeypatch.setattr("src.agent.runtime_roles.session.make_versions", lambda *a, **k: versions())
    images = tmp_path / "images"
    images.mkdir()
    for name in ("North.png", "South.png", "1f_view.png"):
        (elevation_image() if name == "North.png" else
         Image.new("RGB", (1000, 800), "white")).save(images / name)
    marker = tmp_path / "script.json"
    marker.write_text("{}", encoding="utf-8")
    routes = {role: {"provider": "scripted", "model": "scripted-model", "output_tokens": 4000}
              for role in ("coordinator", "plan_reader", "elevation_reader")}
    task = {"task_id": "north-unit", "role_id": "elevation_reader", "image": "North.png", "target": "North/F1"}
    args = parser().parse_args(["--out", str(tmp_path / "out"), "--run-root", str(tmp_path),
        "--images", str(images), "--provider", "scripted", "--model", "scripted-model",
        "--script", str(marker), "--roles-json", json.dumps(routes), "--seconds", "120",
        "--model-calls", "2", "--tool-calls", "4", "--tokens", "100000000",
        "--reader-task-json", json.dumps(task)])
    dispatched = []
    def factory(task_id, config, store):
        dispatched.append(task_id)
        return ScriptedAdapter(elevation_responses(COMPACT_ELEVATION_EXAMPLE))
    result = asyncio.run(execute(args, adapter_factory=factory))
    assert result["status"] == "completed", result
    assert dispatched == ["north-unit"]
    assert result["execution_scope"] == "reader_unit" and not result["whole_building_delivered"]
    assert result["reader"]["runtime_status"] == "completed"
    assert result["role_accounting"]["requests"] == 2
    summary = json.loads((tmp_path / "out/bim/summary.json").read_bytes())
    assert summary["delivery"] is None
