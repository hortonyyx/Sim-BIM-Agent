import asyncio
import json

from src.agent.runtime_roles.readers import ReaderTools, PRETRIAL_REMINDER_THRESHOLD
from test_role_readers import Frozen, Trial


def test_observation_reminder_counts_all_looks_survives_resume_and_stops_at_trial(tmp_path, monkeypatch):
    async def scenario():
        tools = ReaderTools(Frozen(tmp_path), role_id="plan_reader", image_name="plan.png", trial=Trial())
        for name in ("inputs", "get_bim_reference"):
            await tools.call_tool(name, {})
        for index, name in enumerate(("view_image", "pixel_profile", "view_pixel_region_overview",
                                     "view_pixel_region", "map_pixels", "map_dimension_chain"), 1):
            result = await tools.call_tool(name, {})
            assert len(result["content"]) == (2 if index == PRETRIAL_REMINDER_THRESHOLD else 1)
            assert result["structuredContent"] in ({"ok": True}, {"profile_id": "profile_001"})
        resumed = ReaderTools(Frozen(tmp_path), role_id="plan_reader", image_name="plan.png", trial=Trial())
        result = await resumed.call_tool("view_pixel_profile", {})
        assert len(result["content"]) == 1  # The reminder is not repeated after resume.
        invalid = await resumed.call_tool("trial_plan_bim", {"invalid": {}})
        assert invalid["isError"] and resumed.progress()["first_trial_epoch"] is None
        monkeypatch.setattr("src.agent.runtime_roles.readers.time.time", lambda: 123.5)
        await resumed.call_tool("trial_plan_bim", {"plan": {}})
        result = await resumed.call_tool("view_image", {})
        assert len(result["content"]) == 1
        progress = resumed.progress(started_epoch=100)
        assert progress["observation_calls_before_first_trial"] == 7
        assert progress["first_trial_epoch"] == 123.5
        assert progress["first_trial_elapsed_seconds"] == 23.5
        restored = ReaderTools(Frozen(tmp_path), role_id="plan_reader", image_name="plan.png", trial=Trial())
        assert restored.progress(started_epoch=100) == progress
    asyncio.run(scenario())


def test_block_batch_is_one_observation_and_all_returned_views_remain_usable(monkeypatch):
    async def blocks(frozen, image_name):
        payload = {"evidence_previews": [{"view_id": f"block_{i}"} for i in range(6)]}
        return {"content": [{"type": "text", "text": json.dumps(payload)}], "structuredContent": payload}

    async def scenario():
        monkeypatch.setattr("src.agent.runtime_roles.plan_views.view_plan_blocks", blocks)
        frozen = Frozen()
        tools = ReaderTools(frozen, role_id="plan_reader", image_name="plan.png", trial=Trial())
        await tools.call_tool("view_plan_blocks", {})
        assert tools.progress()["by_tool"] == {"view_plan_blocks": 1}
        for i in range(6):
            await tools.call_tool("map_pixels", {"view_id": f"block_{i}"})
        assert len(frozen.calls) == 6
        elevation = ReaderTools(frozen, role_id="elevation_reader", image_name="North.png")
        assert "view_plan_blocks" not in {row["name"] for row in await elevation.list_tools()}
    asyncio.run(scenario())
