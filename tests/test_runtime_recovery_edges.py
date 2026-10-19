"""Focused recovery tests for durable budget and summary boundaries."""

from __future__ import annotations

import asyncio
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest

from src.agent_runtime.context import (
    ContextManager,
    ContextPolicy,
    SummaryCandidate,
    SummaryStatement,
)
from src.agent_runtime.loop import RunLimits
from src.agent_runtime.store import EventStore
from src.harness_contracts import BudgetAmounts, RunLifecyclePayload

from test_agent_runtime import MESSAGES, response, runtime


class InjectedCrash(BaseException):
    pass


@pytest.mark.parametrize("corrupt", [False, True])
def test_file_capture_uses_bounded_reads_and_verifies_existing_blob(tmp_path, monkeypatch, corrupt):
    source = tmp_path / "artifact.bin"
    data = b"bounded artifact\n" * 200_000
    source.write_bytes(data)
    expected = hashlib.sha256(data).hexdigest()
    with EventStore(tmp_path / "run", run_id="stream", task_id="root",
                    budget_limit=BudgetAmounts(calls=1)) as store:
        original_read = Path.read_bytes

        def no_whole_artifact_read(path):
            if path == source or path.parent.name == "blobs":
                raise MemoryError("whole artifact allocation forbidden")
            return original_read(path)

        monkeypatch.setattr(Path, "read_bytes", no_whole_artifact_read)
        ref = store.put_file(source)
        assert ref.sha256 == expected
        target = store.directory / ref.uri
        with target.open("rb") as saved:
            assert hashlib.file_digest(saved, "sha256").hexdigest() == expected
        if corrupt:
            target.write_bytes(b"changed")
            with pytest.raises(ValueError, match="corrupted"):
                store.put_file(source)
        else:
            assert store.put_file(source) == ref
        assert not list((store.directory / "blobs").glob(".capture-*"))


def test_memory_error_during_failure_artifact_capture_saves_minimal_receipt_and_resumes(tmp_path, monkeypatch):
    engine = runtime(tmp_path, [response(("write", "save", {"value": 7}))])

    def fail_next_request(name, current):
        if name == "before_request" and current.counts["model_calls"] == 1:
            current.stage = "prepare_request"
            raise MemoryError("private allocation details")

    def fail_capture(*args, **kwargs):
        raise MemoryError("private capture details")

    engine.fault_hook = fail_next_request
    monkeypatch.setattr(engine.store, "put_file", fail_capture)
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        assert receipt["status"] == "failed"
        assert receipt["receipt_kind"] == "minimal_memory_failure"
        assert receipt["failure"]["failure_stage"] == "prepare_request"
        assert receipt["counts_snapshot"]["model_calls"] == 1
        assert receipt["accounting_complete"] is False and receipt["completion"] is None
        assert receipt == json.loads((engine.store.task_directory / "receipt.json").read_bytes())
        assert "private" not in json.dumps(receipt)
        assert (tmp_path / "tools" / "saved.json").is_file()

    resumed = runtime(tmp_path, [response(text="done")], tools=engine.tools)
    with resumed.store:
        recovered = asyncio.run(resumed.run(MESSAGES, resume=True))
        assert recovered["status"] == "completed" and recovered["model_calls"] == 2
        assert recovered["tool_calls"] == 1 and len(engine.tools.calls) == 1
        assert len(recovered["artifacts"]) == 1
        resumed.store.validate()


def test_minimal_memory_receipt_after_stop_event_is_not_completed_on_resume(tmp_path, monkeypatch):
    engine = runtime(tmp_path, [response(text="done")])
    real_capture = engine.store.capture

    def fail_receipt_capture(value, **kwargs):
        if isinstance(value, dict) and "artifact_paths" in value and "status" in value:
            raise MemoryError("receipt capture allocation")
        return real_capture(value, **kwargs)

    monkeypatch.setattr(engine.store, "capture", fail_receipt_capture)
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        assert receipt["status"] == "failed"
        assert receipt["receipt_kind"] == "minimal_memory_failure"
        assert any(e.payload.event_type == "run_lifecycle" and e.payload.reason == "completed"
                   for e in engine.store.events)
    resumed = runtime(tmp_path, [], tools=engine.tools)
    with resumed.store:
        recovered = asyncio.run(resumed.run(MESSAGES, resume=True))
        assert recovered["status"] == "completed"
        assert "receipt_kind" not in recovered and "resume_status" not in recovered
        assert recovered["model_calls"] == 1 and not resumed.adapter.requests
        resumed.store.validate()


@pytest.mark.parametrize("cache", ["forged", "missing", "corrupt", "wrong_shape"])
def test_completed_receipt_file_cannot_override_journal_answer_on_resume(tmp_path, cache):
    engine = runtime(tmp_path, [response(text="recorded answer")])
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        assert receipt["status"] == "completed"
        if cache == "missing":
            (engine.store.task_directory / "receipt.json").unlink()
        elif cache == "corrupt":
            (engine.store.task_directory / "receipt.json").write_bytes(b'{"status":')
        else:
            engine.store.write_json("receipt.json", {**receipt, "answer": "unrecorded answer"}
                                    if cache == "forged" else [])
    resumed = runtime(tmp_path, [], tools=engine.tools)
    with resumed.store:
        recovered = asyncio.run(resumed.run(MESSAGES, resume=True))
        assert recovered["status"] == "completed" and recovered["answer"] == "recorded answer"
        assert "resume_status" not in recovered and not resumed.adapter.requests
        resumed.store.validate()


def test_completed_receipt_additive_reports_do_not_rewrite_journal_or_override_canonical_fields(tmp_path):
    engine = runtime(tmp_path, [response(text="recorded answer")])
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        before = engine.store.path.read_bytes()
        engine.store.write_json("receipt.json", {**receipt, "domain_report": {"arbitrary": True},
                                                 "completion": {"unrecorded": True}})
    resumed = runtime(tmp_path, [], tools=engine.tools)
    with resumed.store:
        recovered = asyncio.run(resumed.run(MESSAGES, resume=True))
        assert recovered == {**receipt, "resume_status": "already_completed"}
        assert resumed.store.path.read_bytes() == before and not resumed.adapter.requests


@pytest.mark.parametrize("boundary", ["after_reservation", "after_retry"])
def test_resume_releases_reservation_without_durable_request(tmp_path, boundary):
    limits = RunLimits(
        model_calls=3,
        tool_calls=1,
        seconds=30.0,
        tokens=100_000,
        max_model_retries=1,
    )
    scripted = (
        [response(("write", "save", {"value": 7}))]
        if boundary == "after_reservation"
        else [ConnectionError("offline failure"), response(("write", "save", {"value": 7}))]
    )
    engine = runtime(tmp_path, scripted, limits=limits)

    def crash_at_boundary(name, _engine):
        if name == boundary:
            raise InjectedCrash(name)

    engine.fault_hook = crash_at_boundary
    with engine.store:
        with pytest.raises(InjectedCrash, match=boundary):
            asyncio.run(engine.run(MESSAGES))

    expected_sent = 0 if boundary == "after_reservation" else 1
    assert len(engine.adapter.requests) == expected_sent
    reservations = [
        event for event in engine.store.events
        if event.payload.event_type == "budget" and event.payload.action == "reserve"
    ]
    requests = [
        event for event in engine.store.events
        if event.payload.event_type == "adapter_request"
    ]
    assert len(reservations) == expected_sent + 1
    assert len(requests) == expected_sent
    if boundary == "after_retry":
        retry = next(
            event.payload for event in engine.store.events
            if event.payload.event_type == "run_lifecycle" and event.payload.action == "retry"
        )
        assert retry.retry_of_event_id == requests[0].event_id

    resumed = runtime(
        tmp_path,
        [response(("resumed", "save", {"value": 99})), response(text="done")],
        limits=limits,
        tools=engine.tools,
    )
    with resumed.store:
        receipt = asyncio.run(resumed.run(MESSAGES, resume=True))

    assert receipt["status"] == "completed"
    assert len(resumed.adapter.requests) == 2
    assert engine.tools.calls == [("save", {"value": 99})]
    assert json.loads((tmp_path / "tools" / "saved.json").read_bytes()) == {"value": 99}
    assert len(resumed.budget.ledger.releases) == 1


@pytest.mark.parametrize("mismatch", ["run", "task", "budget"])
def test_tail_recovery_rejects_identity_or_budget_change_without_touching_events(
    tmp_path, mismatch
):
    limit = BudgetAmounts(
        tokens=100_000,
        calls=4,
        seconds=Decimal("30"),
    )
    directory = tmp_path / "run"
    with EventStore(
        directory,
        run_id="original-run",
        task_id="original-task",
        budget_limit=limit,
    ) as store:
        store.append(RunLifecyclePayload(action="start", reason="test run"))

    path = directory / "events.jsonl"
    with path.open("ab") as stream:
        stream.write(b'{"incomplete":')
    before = path.read_bytes()

    run_id = "changed-run" if mismatch == "run" else "original-run"
    task_id = "changed-task" if mismatch == "task" else "original-task"
    requested_limit = (
        BudgetAmounts(tokens=99_999, calls=4, seconds=Decimal("30"))
        if mismatch == "budget"
        else limit
    )
    with pytest.raises(ValueError, match="journal limits/identity"):
        EventStore(
            directory,
            run_id=run_id,
            task_id=task_id,
            budget_limit=requested_limit,
            recover_tail=True,
        )

    assert path.read_bytes() == before


def _summary_response(body):
    request = json.loads(body["messages"][-1]["content"])
    state = request["current_state"][0]
    covered = next(
        item["history_id"]
        for item in request["history_references"]
        if item["source"]["event_id"] is not None
    )
    statement = SummaryStatement(
        state_key=state["key"],
        value=state["value"],
        epistemic_status=state["epistemic_status"],
        source_refs=tuple(state["source_refs"]),
    )
    candidate = SummaryCandidate(
        covered_history_ids=(covered,),
        statements=(statement,),
        text=ContextManager.render_summary((statement,)),
    )
    return response(text=candidate.model_dump_json())


def test_summary_response_replay_does_not_request_another_summary(tmp_path):
    limits = RunLimits(
        model_calls=4,
        tool_calls=2,
        seconds=30.0,
        tokens=100_000,
        summary_every=1,
    )
    policy = ContextPolicy(active_window_messages=16, preserve_initial_messages=2)
    engine = runtime(
        tmp_path,
        [response(("view-once", "view", {})), _summary_response],
        limits=limits,
    )
    engine.context_policy = policy

    def crash_after_summary_response(name, current):
        if (
            name == "after_response"
            and current.budget.ledger.reservations[-1].purpose == "context_summary"
        ):
            raise InjectedCrash(name)

    engine.fault_hook = crash_after_summary_response
    with engine.store:
        with pytest.raises(InjectedCrash, match="after_response"):
            asyncio.run(engine.run(MESSAGES))

    resumed = runtime(tmp_path, [response(text="Done")], limits=limits, tools=engine.tools)
    resumed.context_policy = policy
    with resumed.store:
        receipt = asyncio.run(resumed.run(MESSAGES, resume=True))
        purposes = [item.purpose for item in resumed.budget.ledger.reservations]

    assert receipt["status"] == "completed"
    assert resumed.adapter.requests and len(resumed.adapter.requests) == 1
    assert purposes == ["primary_task", "context_summary", "primary_task"]
    assert purposes.count("context_summary") == 1
    assert engine.tools.calls == [("view", {})]


def test_summary_retry_does_not_turn_following_primary_request_into_retry(tmp_path):
    limits = RunLimits(
        model_calls=5,
        tool_calls=2,
        seconds=30.0,
        tokens=100_000,
        max_model_retries=1,
        summary_every=1,
    )
    engine = runtime(
        tmp_path,
        [
            response(("view-once", "view", {})),
            ConnectionError("first summary attempt failed"),
            _summary_response,
            response(text="Done"),
        ],
        limits=limits,
    )
    engine.context_policy = ContextPolicy(
        active_window_messages=16,
        preserve_initial_messages=2,
    )

    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        reservations = [item.purpose for item in engine.budget.ledger.reservations]
        request_purposes = [
            event.payload.logical_purpose
            for event in engine.store.events
            if event.payload.event_type == "adapter_request"
        ]

    assert receipt["status"] == "completed"
    assert receipt["model_calls"] == 4
    assert receipt["retries"] == 1
    assert reservations == [
        "primary_task",
        "context_summary",
        "retry",
        "primary_task",
    ]
    assert request_purposes == [
        "primary_task",
        "context_summary",
        "context_summary",
        "primary_task",
    ]
    assert engine.budget.ledger.committed.calls == 4
