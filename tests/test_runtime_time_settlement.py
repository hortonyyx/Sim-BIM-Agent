"""Settlement overruns keep evidence and report the exhausted time scope."""

from __future__ import annotations

from decimal import Decimal
import json
import time

from src.agent_runtime.adapter import ScriptedAdapter
from src.agent_runtime.loop import RunLimits, Runtime
from src.agent_runtime.store import EventStore
from src.harness_contracts import (
    BudgetAmounts,
    BudgetEventPayload,
    BudgetReservation,
    RunLifecyclePayload,
    UsageMissing,
    UsageReported,
)

from test_agent_runtime import Tools, role, versions


def _runtime_with_reservation(
    tmp_path,
    *,
    root_seconds: float,
    child_seconds: float,
    reservation_tokens: int = 50,
    reservation_seconds: str = "0.900",
):
    root_limits = RunLimits(
        model_calls=2,
        tool_calls=0,
        seconds=root_seconds,
        tokens=200,
    )
    child_limits = RunLimits(
        model_calls=1,
        tool_calls=0,
        seconds=child_seconds,
        tokens=100,
    )
    root = EventStore(
        tmp_path / "run",
        run_id="settlement-scope",
        task_id="coordinator",
        budget_limit=root_limits.ledger_limit(),
    )
    root.append(RunLifecyclePayload(action="start", reason="test root started"))
    child = root.for_task("observer", parent_task_id="coordinator")
    reservation = BudgetReservation(
        reservation_id="observer:request-1",
        purpose="child_task",
        task_id="observer",
        amounts=BudgetAmounts(
            tokens=reservation_tokens,
            calls=1,
            seconds=Decimal(reservation_seconds),
        ),
    )
    child.append(BudgetEventPayload(action="reserve", reservation=reservation))
    engine = Runtime(
        store=child,
        adapter=ScriptedAdapter([]),
        tools=Tools(tmp_path / "tools"),
        role=role(child_limits, readonly=True),
        model="test",
        parameters={"max_tokens": 16, "temperature": 0.0},
        versions=versions(),
        limits=child_limits,
    )
    engine.started = time.monotonic()
    engine.started_epoch = time.time()
    engine.elapsed_before = 0.0
    engine.answer = None
    engine.completion = None
    engine._load_budget()
    engine._refresh_counts()
    return root, engine, reservation


def _violation(engine):
    event = next(
        event
        for event in engine.store.events
        if event.payload.event_type == "run_lifecycle"
        and event.payload.failure_stage == "budget_settlement"
    )
    source = next(
        source
        for source in event.source_refs
        if source.source_id == "budget-violation"
    )
    return json.loads(engine.store.get_bytes(source.blob))


def test_child_cleanup_time_overrun_keeps_unknown_hold_and_scoped_receipt(tmp_path):
    root, engine, reservation = _runtime_with_reservation(
        tmp_path,
        root_seconds=20.0,
        child_seconds=1.0,
    )
    with root:
        reason = engine._settle(
            reservation.reservation_id,
            UsageMissing(reason="cancelled request returned no usage receipt"),
            seconds=0.995,
        )

        assert reason == "child_time_budget_exhausted"
        evidence = _violation(engine)
        assert evidence["decision"]["reason"] == "actual_usage_exceeds_reservation"
        assert evidence["decision"]["exceeded_dimensions"] == ["seconds"]
        assert evidence["observed"] == {
            "tokens": None,
            "money_usd": None,
            "money_cny": None,
            "seconds": "0.995",
            "calls": 1,
        }
        assert evidence["usage"]["kind"] == "missing"
        assert Decimal(evidence["observed"]["seconds"]) - reservation.amounts.seconds == Decimal("0.095")

        receipt = engine._stop(reason)
        assert receipt["status"] == "child_time_budget_exhausted"
        assert receipt["task_budget"]["settlements"] == []
        assert receipt["budget"]["settlements"] == []
        assert receipt["task_budget_available"]["tokens"] == 50
        assert receipt["root_budget_available"]["tokens"] == 150
        assert not [
            event
            for event in engine.store.events
            if event.payload.event_type == "budget"
            and event.payload.action == "settle"
        ]


def test_root_time_cap_wins_when_root_remaining_time_is_tighter(tmp_path):
    root, engine, reservation = _runtime_with_reservation(
        tmp_path,
        root_seconds=1.0,
        child_seconds=10.0,
    )
    with root:
        reason = engine._settle(
            reservation.reservation_id,
            UsageMissing(reason="cancelled request returned no usage receipt"),
            seconds=0.995,
        )

        assert reason == "root_time_budget_exhausted"
        assert _violation(engine)["decision"]["exceeded_dimensions"] == ["seconds"]
        receipt = engine._stop(reason)
        assert receipt["status"] == "root_time_budget_exhausted"
        assert receipt["budget"]["settlements"] == []


def test_token_overrun_settles_when_root_and_child_totals_still_fit(tmp_path):
    root, engine, reservation = _runtime_with_reservation(
        tmp_path,
        root_seconds=20.0,
        child_seconds=10.0,
    )
    with root:
        reason = engine._settle(
            reservation.reservation_id,
            UsageReported(raw_usage={"total_tokens": 51}),
            seconds=0.500,
        )

        assert reason is None
        settlement = engine.task_budget.ledger.settlements[0]
        assert settlement.actual.tokens == 51
        assert settlement.token_overrun == 1
        assert engine.task_budget.ledger.charged.tokens == 51
        assert engine.task_budget.available.tokens == 49
        assert engine.budget.ledger.charged.tokens == 51
        assert engine.budget.available.tokens == 149
        assert not [event for event in engine.store.events
                    if event.payload.event_type == "run_lifecycle"
                    and event.payload.failure_stage == "budget_settlement"]
