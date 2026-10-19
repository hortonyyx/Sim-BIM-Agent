"""Reader observations and coordinator handoffs cannot outrun durable evidence."""

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from scripts.tool_scripts import bim_agent_replies
from scripts.tool_scripts.run_bim_agent import Toolkit
from src.agent.runtime_delivery import completed_building_handoff
from src.agent.runtime_roles.assembly_review import finalize_role_building
from src.agent.runtime_roles.session import RoleSession
from src.agent_runtime.adapter import ScriptedAdapter
from src.agent_runtime.loop import RunLimits
from src.agent_runtime.store import EventStore
from src.harness_contracts import ToolExecutionPayload

from test_agent_runtime import response
from tests.test_bim_agent_tools import _run_with_one_image
from tests.test_role_rework_handoff import _isolated_recovery_trials
from tests.test_role_session import ROOT, ROUTES, dispatch, environment  # noqa: F401
from tests.test_role_trial import example
from tests.test_runtime_r3 import LocalClient, PLAN


def test_plan_cannot_review_a_trial_in_the_same_model_response(environment, monkeypatch):
    store, make = environment
    _isolated_recovery_trials(monkeypatch, {})
    plan = example()
    # This formal reader trial must satisfy the current wall contract before
    # exercising whether its returned images reached a later model request.
    plan["wall_metadata"] = [
        {"target": target, "thickness_m": thickness, "reference_side": side,
         "evidence_status": "inferred", "basis": "Synthetic fixture wall assumption",
         "source_refs": ["north.png: synthetic fixture"]}
        for target, thickness, side in (
            ("footprint:*", 0.2, "outer_face"), ("partition:*", 0.1, "centerline"))
    ]
    adapter = ScriptedAdapter([response(
        ("trial", "trial_plan_bim", {"plan": plan}),
        ("premature", "submit_plan_reading", {"trial_id": "latest", "local_review": {
            "summary": "This response cannot have inspected the trial images it is about to create."}}),
    )])
    session = make(lambda *args: adapter)
    task = session._task(dispatch("plan", role_id="plan_reader", target="F1",
                                  budget={"model_calls": 1}))
    result = asyncio.run(session.run_reader(task))

    assert result["status"] == "failed" and result["artifact"] is None
    assert len(adapter.requests) == 1
    executions = [event.payload for event in store.all_events
                  if event.payload.event_type == "tool_execution"]
    assert next(row for row in executions if row.tool_name == "trial_plan_bim").outcome == "succeeded"
    rejected = next(row for row in executions if row.tool_name == "submit_plan_reading")
    assert rejected.outcome == "failed"
    assert "have not reached a model request" in store.resolve(rejected.raw_result)["structuredContent"]["reason"]
    assert not (session.registry.child("plan").task_directory / "bim/reader_submission.json").exists()


def test_submission_file_without_execution_receipt_is_not_a_delivery(environment):
    store, make = environment

    def interrupt_after_submission(boundary, engine):
        payload = engine.store.events[-1].payload
        if (boundary == "after_tool" and payload.event_type == "tool_invocation"
                and payload.tool_name == "submit_elevation_reading"):
            raise RuntimeError("offline interruption after submission file, before execution receipt")

    session = make(reader_fault_hook=interrupt_after_submission)
    result = asyncio.run(session.run_reader(session._task(dispatch("north"))))
    child = session.registry.child("north")
    runtime = json.loads((child.task_directory / "receipt.json").read_bytes())

    assert (child.task_directory / "bim/reader_submission.json").is_file()
    assert runtime["status"] == "failed" and runtime.get("completion") is None
    assert result["status"] == "failed" and result["artifact"] is None
    assert not [event for event in store.all_events if event.payload.event_type == "tool_execution"
                and event.payload.tool_name == "submit_elevation_reading"]
    with pytest.raises(ValueError, match="no validated artifact"):
        session.registry.read("north")


def test_projected_finish_attests_final_role_attachments(tmp_path, monkeypatch):
    run = _run_with_one_image(tmp_path / "case")
    manifest = json.loads((run / "inputs.json").read_bytes())
    manifest.update(floor_plan_images=["plan.png"], floor_scope_source="explicit")
    (run / "inputs.json").write_text(json.dumps(manifest), encoding="utf-8")
    built = Toolkit(run).build_plan("plan.png", json.dumps(PLAN))
    candidate = built["candidate"]
    original_reply = bim_agent_replies.compact_reply

    def projected_reply(run, tool, result, **kwargs):
        value = original_reply(run, tool, result, **kwargs)
        if tool == "finish_bim":
            return {key: value[key] for key in ("details_file", "details_sha256", "details_read")}
        return value

    monkeypatch.setattr(bim_agent_replies, "compact_reply", projected_reply)

    class FinishTransport:
        """Real T1/FastMCP finish; upstream reader/assembly guards are outside this test."""

        run_directory = run

        def __init__(self):
            self.client = LocalClient(run, [0])

        async def list_tools(self):
            return [tool for tool in await self.client.list_tools() if tool["name"] == "finish_bim"]

        @staticmethod
        def repeatability(name):
            return "non_idempotent_write"

        async def call_tool(self, name, arguments):
            return await self.client.call_tool(name, arguments)

    limits = RunLimits(model_calls=1, tool_calls=2, seconds=120, tokens=100_000)
    with EventStore(tmp_path / "journal", run_id="finish", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        session = RoleSession(store=store, frozen=FinishTransport(), routes=ROUTES,
                              adapter_factory=None, limits=limits, root=ROOT)
        session.assembly = SimpleNamespace(check=lambda *args, **kwargs: None, guard=lambda: None)
        session.positions = SimpleNamespace(guard=lambda *args: None, write_delivery=lambda *args: None)
        arguments = {"candidate": candidate, "architectural_review": {
            "summary": "Offline fixture: reviewed this selected source building."}}
        result = asyncio.run(session.call_tool("finish_bim", arguments))
        assert not result["isError"], result
        metadata = result["structuredContent"]
        assert metadata["candidate"] == candidate
        hashes = metadata["delivery_artifact_sha256"]
        assert len(hashes) == 6
        assert all(hashlib.sha256((run / name).read_bytes()).hexdigest() == sha
                   for name, sha in hashes.items())
        delivery = json.loads((run / "delivery.json").read_bytes())
        assert delivery["architectural_review"]["summary"] == arguments["architectural_review"]["summary"]

        captured = store.capture(result)
        store.append(ToolExecutionPayload(call_id="finish", tool_name="finish_bim", full_arguments=arguments,
            raw_result=captured, shown_result=captured, repeatability="non_idempotent_write",
            operation_key="finish", applied_write_id="finish", outcome="succeeded"))
        engine = SimpleNamespace(store=store, tools=session, role=SimpleNamespace(read_only=False),
                                 domain_completion=completed_building_handoff)
        engine.completion = completed_building_handoff(engine)
        assert engine.completion is not None
        finalization = finalize_role_building(engine, "completed")
        assert finalization["reused_handoff"] and finalization["completion_valid"]
        assert completed_building_handoff(engine) == engine.completion
