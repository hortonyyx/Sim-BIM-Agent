"""D1c draft/edit boundaries, whole-plan feedback and preservation contracts."""

import asyncio
import copy
import hashlib
import json

import jsonschema
import pytest
from PIL import Image

from src.agent.geometry.plan_partition import compile_plan_partition
from src.agent.runtime_roles.guidance import PLAN_READER_GUIDANCE
from src.agent.runtime_roles.plan_format import PLAN_EXAMPLE, READER_PLAN_EXAMPLE, plan_format_errors
from src.agent.runtime_roles.plan_review import (
    loose_partition_ends, revise_operations, topology_issues, unhosted_openings, validate_wall_reference,
)
from src.agent.runtime_roles.readers import ReaderTools
from src.agent.runtime_roles.submission import WALL_REFERENCE_EXAMPLE
from src.agent.runtime_roles.trial import PlanTrial
from tests.test_role_readers import Frozen, plan
from tests.test_role_submission import operation
from tests.test_role_trial import Tools


def test_reference_example_is_complete_compilable_and_present_in_reader_guidance():
    assert plan_format_errors(PLAN_EXAMPLE) == []
    proposal, _ = compile_plan_partition(PLAN_EXAMPLE, image_size=(12, 8), image_name="plan.png")
    assert proposal
    for stale in ("Even a failed trial is the next rework base", "base_plan_sha256", "finishing budget below"):
        assert stale not in PLAN_READER_GUIDANCE
    normalized_guidance = " ".join(PLAN_READER_GUIDANCE.split())
    assert "send one complete plan to trial_plan_bim so code can place it" in normalized_guidance
    assert "Use observed original pixels for ALL actual walls, openings and room seeds" in normalized_guidance
    assert "Original annotations remain evidence" in normalized_guidance


def test_reader_example_is_the_reference_plan_at_image_scale():
    assert plan_format_errors(READER_PLAN_EXAMPLE) == []
    proposal, _ = compile_plan_partition(READER_PLAN_EXAMPLE, image_size=(600, 420), image_name="plan.png")
    assert proposal
    reference, _ = compile_plan_partition(PLAN_EXAMPLE, image_size=(12, 8), image_name="plan.png")
    actual, expected = proposal["geometry"], reference["geometry"]
    assert actual["footprint_x"] == expected["footprint_x"]
    assert actual["footprint_y"] == expected["footprint_y"]
    for floor, old in zip(actual["floors"], expected["floors"], strict=True):
        assert floor["name"] == old["name"]
        assert "z_floor" not in floor and "ceiling_height" not in floor
        for cell, prior in zip(floor["cells"], old["cells"], strict=True):
            assert {key: cell[key] for key in ("id", "polygon", "x", "y", "role")} == {
                key: prior[key] for key in ("id", "polygon", "x", "y", "role")}
    for collection in ("windows", "openings"):
        assert [{k: v for k, v in row.items() if k not in {"z", "source_refs"}}
                for row in actual[collection]] == [
                    {k: v for k, v in row.items() if k not in {"z", "source_refs"}}
                    for row in expected[collection]]
        assert all("z" not in row for row in actual[collection])
    assert json.dumps(READER_PLAN_EXAMPLE, ensure_ascii=False, separators=(",", ":")) in PLAN_READER_GUIDANCE
    assert all(value > 20 for row in READER_PLAN_EXAMPLE["footprint_pixels"] for value in row)


def test_world_metre_points_and_invented_room_uses_are_named_before_compiling():
    # 10-06 probe run2: anchors and footprint in pixels, dividers and openings in metres.
    value = plan()
    value["x_anchors"] = [[247, 0], [613, 10]]
    value["y_anchors"] = [[878, 0], [150, 20]]
    value["footprint_pixels"] = [[247, 150], [613, 150], [613, 878], [247, 878]]
    value["partitions"] = [{"id": "west", "points": [[4.18, 19.75], [4.18, 0.25]], "source_refs": ["plan.png: wall"]}]
    value["openings"] = [{"id": "D_main", "kind": "door", "p1": [0.54, 20], "p2": [2.14, 20],
                          "z": [0, 2.1], "source_refs": ["plan.png: door"]}]
    value["space_seeds"] = [{"id": "hall", "point": [400, 500], "role": "open_office_meeting"},
                            {"id": "office", "point": [300, 300], "role": "office"}]
    errors = {row["path"]: row["message"] for row in plan_format_errors(value)}
    assert "never world metres" in errors["plan.partitions[0]"]
    assert "never world metres" in errors["plan.openings[0]"]
    assert "not a room_types code" in errors["plan.space_seeds[0].role"]
    assert not any(path.startswith("plan.space_seeds[1]") for path in errors)
    pixels = copy.deepcopy(value)
    pixels["partitions"][0]["points"] = [[400, 150], [400, 878]]
    pixels["openings"][0].update(p1=[267, 150], p2=[327, 150])
    pixels["space_seeds"][0]["role"] = "Office"
    assert plan_format_errors(pixels) == []


def test_all_rows_and_nested_formats_reported_in_one_response_without_compiler_call(tmp_path):
    async def scenario():
        value = plan()
        del value["ceiling_height"]
        value["units"] = "mm"
        value["partitions"] = [{"id": "P1", "p1": [1, 1], "p2": [2, 2]},
                               {"id": "P2", "points": [[1], [False, 3]], "source_refs": []}]
        value["space_seeds"] = [{"id": "S1", "seed": [1, 2]}, {"id": "S1", "point": [1, "x"]}]
        value["openings"] *= 2
        value["openings"][0]["room"] = "left"
        (tmp_path / "images").mkdir()
        image = tmp_path / "images/plan.png"
        Image.new("RGB", (120, 120), "white").save(image)
        (tmp_path / "inputs.json").write_text(json.dumps({"images": {"plan.png": {
            "size": [120, 120], "sha256": hashlib.sha256(image.read_bytes()).hexdigest()}}}))
        tools = Tools()
        trial = PlanTrial(tools, image_name="plan.png", workspace=tmp_path)
        reader = ReaderTools(Frozen(), role_id="plan_reader", image_name="plan.png", trial=trial)
        envelope = await reader.call_tool("trial_plan_bim", {"plan": value})
        receipt = envelope["structuredContent"]
        assert receipt["error_type"] == "plan_format" and tools.calls == []
        assert receipt["repair_hint"]["example"] == READER_PLAN_EXAMPLE
        errors = receipt["format_errors"]
        assert any(row["path"] == "plan" and "units" in row["message"] for row in errors)
        assert not any("ceiling_height" in row["message"] for row in errors)
        for collection in ("partitions", "space_seeds", "openings"):
            for index in (0, 1):
                assert any(row["path"].startswith(f"plan.{collection}[{index}]") for row in errors)
        assert all(row["message"] in envelope["content"][-1]["text"] for row in errors)
        assert trial.baseline() == (None, None)
        repaired = plan()
        repaired["wall_metadata"] = copy.deepcopy(READER_PLAN_EXAMPLE["wall_metadata"])
        outcome = await reader.call_tool("trial_plan_bim", {"plan": repaired})
        assert outcome["structuredContent"]["status"] == "passed", outcome
    asyncio.run(scenario())


def test_all_alias_conflicts_and_bad_quantities_are_listed_together():
    value = plan()
    value["partitions"][0]["pixels"] = value["partitions"][0]["points"]
    value["space_seeds"][0]["pixels"] = value["space_seeds"][0]["point"]
    value["z_floor"] = {"value": "0", "unit": "feet"}
    value["openings"][0]["z"] = [float("nan"), True]
    errors = plan_format_errors(value)
    assert sum("both pixels" in row["message"] for row in errors) == 2
    assert {row["path"] for row in errors} >= {"plan.z_floor", "plan.openings[0].z[0]", "plan.openings[0].z[1]"}


def test_failed_revision_remains_latest_editable_base_without_invalidating_last_success():
    async def scenario():
        tools = Tools(ready=False)
        trial = PlanTrial(tools, image_name="plan.png")
        first = await trial.run(plan())
        assert first["source_geometry_ready"] is False
        assert trial.baseline() == (plan(), first["plan_sha256"])
        with pytest.raises(ValueError):
            trial.verified_plan(first["plan_sha256"])
        second_plan = plan()
        second_plan["basis"] += " revised calibration"
        second = await trial.run(second_plan)
        assert second["base_plan_sha256"] == first["plan_sha256"]
        fixed_draft = await trial.run(operations=[operation(p2=[10, 41])])
        assert fixed_draft["base_plan_sha256"] == second["plan_sha256"]
        assert fixed_draft["status"] == "failed"
        assert trial.baseline()[1] == fixed_draft["plan_sha256"]
        tools.ready = True
        third_plan = plan()
        third_plan["basis"] += " final calibration"
        good = await trial.run(third_plan)
        tools.ready = False
        bad = await trial.run(operations=[operation(p2=[10, 42])])
        assert bad["status"] == "failed" and bad["phase"] == "operations"
        assert trial.baseline()[1] == bad["plan_sha256"]
        assert trial.verified_plan(good["plan_sha256"])[0] == third_plan
        assert trial.read_draft()["last_passed_plan_sha256"] == good["plan_sha256"]
        tools.ready = True
        repaired = await trial.run(operations=[operation(p2=[10, 41])])
        assert repaired["base_plan_sha256"] == bad["plan_sha256"]
        assert repaired["changes"][0]["before"]["p2"] == [10, 42]
        assert repaired["plan_revision"]["unchanged_ids"]["partitions"] == ["P1"]
    asyncio.run(scenario())


def test_all_shared_operation_kinds_and_failed_batch_leave_input_unchanged():
    value = plan()
    original = copy.deepcopy(value)
    common = {"reason": "located correction", "source_refs": ["plan.png: original"], "bbox": [0, 0, 100, 100]}
    operations = [
        {**common, "op": "update", "collection": "openings", "id": "W1", "changes": {"p2": [10, 41]}},
        {**common, "op": "add", "collection": "space_seeds", "value": {"id": "right", "point": [80, 40]}},
        {**common, "op": "remove", "collection": "partitions", "id": "P1"},
        {**common, "op": "set", "field": "basis", "value": "revised observation"},
    ]
    changed, audit = revise_operations(value, operations)
    assert len(audit["actual_changes"]) == 4 and value == original
    assert changed["partitions"] == [] and changed["space_seeds"][0] == value["space_seeds"][0]
    with pytest.raises(ValueError, match="does not exist"):
        revise_operations(value, [operations[0], {**operations[2], "id": "missing"}])
    assert value == original


def test_role_tool_catalog_removes_manual_bookkeeping_and_accepts_only_one_mode():
    async def scenario():
        reader = ReaderTools(Frozen(), role_id="plan_reader", image_name="plan.png", trial=PlanTrial(Tools(), image_name="plan.png"))
        schema = next(tool["inputSchema"] for tool in await reader.list_tools() if tool["name"] == "trial_plan_bim")
        assert set(schema["properties"]) == {"plan", "operations"}
        jsonschema.validate({"operations": [operation(p2=[10, 41])]}, schema)
        for arguments in ({"plan": plan(), "base_plan_sha256": "a" * 64},
                          {"plan": plan(), "changes": []}, {"plan": plan(), "operations": []}):
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate(arguments, schema)
            assert (await reader.call_tool("trial_plan_bim", arguments))["isError"]
        assert (await reader.call_tool("trial_plan_bim", {"operations": [operation(p2=[10, 41])]}))["isError"]
    asyncio.run(scenario())


def test_cross_task_operations_only_touch_named_targets_and_continue_from_new_baseline():
    async def scenario():
        prior = PlanTrial(Tools(), image_name="plan.png")
        good = await prior.run(plan())
        current = PlanTrial(Tools(), image_name="plan.png")
        current.inherit_reference(prior, good["plan_sha256"], ["plan.openings:W1"])
        with pytest.raises(ValueError, match="outside the coordinator"):
            await current.run(operations=[{**operation(), "collection": "partitions", "id": "P1",
                                          "changes": {"points": [[61, 10], [61, 110]]}}])
        one = await current.run(operations=[operation(p2=[10, 41])])
        two = await current.run(operations=[operation(p2=[10, 42])])
        assert one["base_plan_sha256"] == good["plan_sha256"]
        assert two["base_plan_sha256"] == one["plan_sha256"]
        assert current.load_plan(two)["partitions"] == plan()["partitions"]
        assert current.load_plan(two)["space_seeds"] == plan()["space_seeds"]
        current.inherit_reference(prior, good["plan_sha256"], ["plan.openings"])
        assert current.allowed_rework_targets == ["plan.openings"]
        failed = PlanTrial(Tools(False), image_name="plan.png")
        failed_receipt = await failed.run(plan())
        with pytest.raises(ValueError, match="successful isolated trial"):
            current.inherit_reference(failed, failed_receipt["plan_sha256"], ["plan.openings:W1"])
    asyncio.run(scenario())


def test_failed_noop_revision_does_not_invalidate_prior_success_with_same_hash():
    async def scenario():
        tools = Tools()
        trial = PlanTrial(tools, image_name="plan.png")
        good = await trial.run(plan())
        tools.ready = False
        failed = await trial.run(operations=[operation(p2=[10, 40])])
        assert failed["plan_sha256"] == good["plan_sha256"] and failed["changes"] == []
        assert trial.verified_plan(good["plan_sha256"])[1]["validation_passed"]
        assert trial.require_success(plan()) == good
    asyncio.run(scenario())


def test_invalid_operation_type_is_a_correctable_prewrite_rejection():
    async def scenario():
        tools = Tools()
        trial = PlanTrial(tools, image_name="plan.png")
        good = await trial.run(plan())
        reader = ReaderTools(Frozen(), role_id="plan_reader", image_name="plan.png", trial=trial)
        bad = await reader.call_tool("trial_plan_bim", {"operations": [{**operation(p2=[10, 41]), "op": []}]})
        assert bad["isError"] and bad["structuredContent"]["status"] == "rejected"
        assert len(tools.calls) == 1 and trial.baseline()[1] == good["plan_sha256"]
    asyncio.run(scenario())


def test_topology_warnings_from_a_trial_without_geometry_do_not_bind():
    # 10-06 probe run2: 14 warnings from a draft with dividers in metres blocked submission.
    row = {"type": "unsupported_open_separator", "divider": "P1", "opening": "D1", "x_px": 60,
           "y_px": [10, 110], "look_box": [55, 8, 65, 112]}
    failed = {"plan_sha256": "a" * 64, "source_geometry_ready": False, "drawing_differences": {"items": [row]}}
    built = {"plan_sha256": "b" * 64, "source_geometry_ready": True, "drawing_differences": {"items": [row]}}
    assert topology_issues([failed]) == []
    assert [issue["plan_sha256"] for issue in topology_issues([failed, built])] == ["b" * 64]


def test_host_failure_lists_every_unhosted_opening_with_its_nearest_line():
    value = plan()
    value["openings"] += [
        {"id": "D_in", "kind": "door", "p1": [60, 60], "p2": [60, 70], "z": [0, 2.1], "source_refs": ["plan.png: door"]},
        {"id": "D_top", "kind": "door", "p1": [20, 16], "p2": [40, 16], "z": [0, 2.1], "source_refs": ["plan.png: door"]},
        {"id": "W2", "kind": "window", "p1": [13, 70], "p2": [13, 90], "z": [1, 2], "source_refs": ["plan.png: window"]},
    ]
    rows = {row["opening"]: row["nearest_parallel_line"] for row in unhosted_openings(value)}
    assert rows == {
        "D_top": {"line": "footprint", "line_at_px": 10, "offset_px": 6, "spans_opening": True},
        "W2": {"line": "footprint", "line_at_px": 10, "offset_px": 3, "spans_opening": True},
    }

    async def scenario():
        trial = PlanTrial(Tools(ready=False), image_name="plan.png")
        receipt = await trial.run(value)
        assert receipt["status"] == "failed"
        assert [row["opening"] for row in receipt["unhosted_openings"]] == ["D_top", "W2"]
    asyncio.run(scenario())


class _FailingTools(Tools):
    """A compiler that fails with a chosen error, or compiles but fails its source self-check."""

    def __init__(self, workspace, *, error=None, findings=None):
        super().__init__(ready=False, workspace=workspace)
        self.error, self.findings = error, findings

    async def call_tool(self, name, arguments):
        result = await super().call_tool(name, arguments)
        body = result["structuredContent"]
        if self.error is not None:
            body["error"] = self.error
        if self.findings is not None:
            candidate = self.workspace / "candidate_01"
            candidate.mkdir(exist_ok=True)
            (candidate / "report.json").write_text(json.dumps({"source_geometry_self_consistency": {
                "status": "severe", "findings": self.findings}}), encoding="utf-8", newline="\n")
            body.pop("error", None)
            body.update(candidate="candidate_01", status="severe")
        return result


def test_failed_trial_names_loose_divider_ends_and_source_self_check_findings(tmp_path):
    value = plan()
    value["partitions"].append({"id": "P2", "points": [[60, 40], [100, 40]], "source_refs": ["plan.png: wall"]})
    loose = [{"partition": "P2", "end": [100, 40], "nearest_line": "footprint", "gap_px": 10.0}]
    assert loose_partition_ends(value) == loose
    assert loose_partition_ends(plan()) == []
    workspace = tmp_path / "trial"
    (workspace / "images").mkdir(parents=True)
    (workspace / "images" / "plan.png").write_bytes(b"one-original")
    overlap = {"code": "source.opening_overlap", "opening_ids": ["W1", "D1"],
               "boundary_id": "space/left/wall/2", "severity": "severe", "area_m2": 1.9}

    async def scenario():
        dangling = PlanTrial(_FailingTools(workspace, error="polygonize produced dangles: [...]"),
                             image_name="plan.png", workspace=workspace)
        receipt = await dangling.run(value)
        assert receipt["status"] == "failed" and receipt["loose_partition_ends"] == loose
        checked = PlanTrial(_FailingTools(workspace, findings=[overlap]), image_name="plan.png", workspace=workspace)
        receipt = await checked.run(plan())
        assert receipt["reason"] == "source self-check failed: source.opening_overlap W1/D1 on space/left/wall/2"
        assert receipt["source_findings"] == [overlap]
    asyncio.run(scenario())


def test_wall_reference_allows_distinct_lines_but_rejects_contradiction_per_category():
    assert validate_wall_reference(WALL_REFERENCE_EXAMPLE) == WALL_REFERENCE_EXAMPLE
    invalid = copy.deepcopy(WALL_REFERENCE_EXAMPLE)
    invalid["partitions"]["dimension_basis"] = "outer_face"
    with pytest.raises(ValueError, match="partitions.*same declared"):
        validate_wall_reference(invalid)
    with pytest.raises(ValueError, match="separate perimeter"):
        validate_wall_reference(WALL_REFERENCE_EXAMPLE["perimeter"])
