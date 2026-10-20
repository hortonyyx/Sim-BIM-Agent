"""Rebuildable, transactional indices for the append-only journal hot path.

No history is copied or scanned by prepare(). It checks a typed event against
committed indices; commit() is called only after the journal write is durable.
Recovery and independent auditing still use the complete EventLog validator.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .budget import BudgetAmounts, BudgetLedger, _effective_charge, _ensure_within_limit
from .events import (AdapterRequestPayload, AnswerRepairPayload, BudgetEventPayload,
                     BudgetOverrunPayload, EventEnvelope, NetworkRecoveryPayload, RunLifecyclePayload,
                     ToolExecutionPayload, TruncationPayload)
from .validation import EventLog, _require_available_for_reservation


DIMENSIONS = ("tokens", "money_usd", "money_cny", "seconds", "calls")


@dataclass(frozen=True)
class BudgetUpdate:
    totals: dict
    counts: dict
    token_overrun: int
    cny_overrun: Decimal


class IncrementalBudgetIndex:
    """Constant-size totals and keyed receipts, using single-pair ledger checks."""

    def __init__(self, limit: BudgetAmounts):
        self.limit = limit
        self.reservations = {}
        self.settlements = {}
        self.releases = {}
        self.reconciliations = {}
        self.overruns = set()
        self.requests = {}
        self.totals = dict.fromkeys(DIMENSIONS, 0)
        self.counts = dict.fromkeys(DIMENSIONS, 0)
        self.token_overrun = 0
        self.cny_overrun = Decimal(0)

    def _amounts(self, totals=None, counts=None):
        totals = self.totals if totals is None else totals
        counts = self.counts if counts is None else counts
        return BudgetAmounts(**{name: totals[name] if counts[name] else None
                                for name in DIMENSIONS})

    @property
    def available(self):
        committed = self._amounts()
        updates = {}
        for name in ("tokens", "money_cny"):
            limit, amount = getattr(self.limit, name), getattr(committed, name)
            if limit is not None and amount is not None and amount > limit:
                updates[name] = limit
        return self.limit.subtract(committed.model_copy(update=updates))

    def prepare(self, event):
        p = event.payload
        if isinstance(p, AdapterRequestPayload) and p.reservation_id in self.releases:
            raise ValueError("released reservation cannot dispatch a request")
        if isinstance(p, BudgetOverrunPayload):
            reservation = self.reservations.get(p.reservation_id)
            settlement = self.settlements.get(p.reservation_id)
            if reservation is None or settlement is None:
                raise ValueError("budget overrun must follow its reservation and settlement")
            if reservation.task_id != event.task_id or settlement.task_id != event.task_id:
                raise ValueError("budget overrun must remain in the reservation task")
            if p.reservation_id in self.overruns:
                raise ValueError("a settlement can have only one budget overrun event")
            r, s = reservation.payload.reservation, settlement.payload.settlement
            if (p.reserved_tokens != r.amounts.tokens or p.actual_tokens != s.effective_tokens
                    or p.overrun_tokens != s.token_overrun):
                raise ValueError("budget overrun amounts differ from reservation or settlement")
            return None
        if not isinstance(p, BudgetEventPayload):
            return None
        before, after = None, None
        token_overrun, cny_overrun = self.token_overrun, self.cny_overrun
        if p.reservation is not None:
            r = p.reservation
            if r.task_id != event.task_id:
                raise ValueError("budget reservation task must match event task")
            if r.reservation_id in self.reservations:
                raise ValueError(f"duplicate reservation_id: {r.reservation_id}")
            _require_available_for_reservation(r.amounts, self.available)
            BudgetLedger(total_limit=self.limit, reservations=(r,))
            after = r.amounts
        if p.settlement is not None:
            s = p.settlement
            original = self.reservations.get(s.reservation_id)
            if original is None:
                raise ValueError("budget must be reserved before it is settled")
            if original.task_id != event.task_id:
                raise ValueError("budget settlement task must match reservation task")
            if s.reservation_id in self.settlements:
                raise ValueError(f"reservation settled twice: {s.reservation_id}")
            if s.reservation_id in self.releases:
                raise ValueError("reservation already released or settled")
            r = original.payload.reservation
            BudgetLedger(total_limit=self.limit, reservations=(r,), settlements=(s,))
            before, after = r.amounts, _effective_charge(r, s)
            token_overrun += s.token_overrun
            cny_overrun += s.money_cny_overrun
        if p.action in {"release", "reconcile"}:
            # Imported only for new recovery actions; ordinary historical reserve
            # and settle journals do not depend on that extension's availability.
            from .validation import validate_budget_recovery_event
            record = p.release if p.action == "release" else p.reconciliation
            identifier = record.reservation_id
            original = self.reservations.get(identifier)
            settled = self.settlements.get(identifier)
            validate_budget_recovery_event(event, original, settled,
                                           self.requests.get(identifier))
            r = original.payload.reservation
            if p.action == "release":
                if identifier in self.releases:
                    raise ValueError("reservation already released or settled")
                BudgetLedger(total_limit=self.limit, reservations=(r,), releases=(record,))
                before = r.amounts
            else:
                if identifier in self.reconciliations:
                    raise ValueError("reservation reconciled twice")
                s = settled.payload.settlement
                BudgetLedger(total_limit=self.limit, reservations=(r,), settlements=(s,),
                             reconciliations=(record,))
                before, after = _effective_charge(r, s), _effective_charge(r, record.settlement)
                token_overrun += record.settlement.token_overrun - s.token_overrun
                cny_overrun += record.settlement.money_cny_overrun - s.money_cny_overrun
        totals, counts = self.totals.copy(), self.counts.copy()
        for amounts, sign in ((before, -1), (after, 1)):
            if amounts is None:
                continue
            for name in DIMENSIONS:
                value = getattr(amounts, name)
                if value is not None:
                    totals[name] += sign * value
                    counts[name] += sign
        committed = self._amounts(totals, counts)
        _ensure_within_limit(committed.model_copy(update={
            "tokens": None if committed.tokens is None else committed.tokens - token_overrun,
            "money_cny": None if committed.money_cny is None else committed.money_cny - cny_overrun,
        }), self.limit, "effective charges and outstanding reservations exceed total budget")
        return BudgetUpdate(totals, counts, token_overrun, cny_overrun)

    def commit(self, event, update):
        p = event.payload
        if isinstance(p, AdapterRequestPayload) and p.reservation_id is not None:
            self.requests[p.reservation_id] = event
        if isinstance(p, BudgetOverrunPayload):
            self.overruns.add(p.reservation_id)
        if isinstance(p, BudgetEventPayload):
            if p.reservation is not None:
                self.reservations[p.reservation.reservation_id] = event
            if p.settlement is not None:
                self.settlements[p.settlement.reservation_id] = event
            if p.action == "release":
                self.releases[p.release.reservation_id] = event
            elif p.action == "reconcile":
                self.reconciliations[p.reconciliation.reservation_id] = event
        if update is not None:
            self.totals, self.counts = update.totals, update.counts
            self.token_overrun, self.cny_overrun = update.token_overrun, update.cny_overrun


class EventValidationIndex:
    def __init__(self, *, run_id: str, root_task_id: str, budget_limit: BudgetAmounts):
        self.run_id, self.root_task_id = run_id, root_task_id
        self.budget_limit = budget_limit
        self.events_by_id = {}
        self.events_by_task = {}
        self.parents = {root_task_id: None}
        self.sequence = -1
        self.reservation_counts = {}
        self.truncated_responses = set()
        self.repair_tasks = set()
        self.repaired_invalid_responses = set()
        self.repair_results = set()
        self.completed_invocations = set()
        self.previous_writes = {}
        self.applied_ids = set()
        self.applied_operations = set()
        self.command_ids = set()
        self.network_recovery_reservations = {}
        self.network_recovery_settled = set()
        self.budget = IncrementalBudgetIndex(budget_limit)

    def prepare(self, event: EventEnvelope):
        if event.event_id in self.events_by_id:
            raise ValueError(f"duplicate event_id: {event.event_id}")
        if event.sequence <= self.sequence:
            raise ValueError("event sequence must be strictly increasing")
        if event.run_id != self.run_id:
            raise ValueError("one EventLog represents exactly one run")
        if event.occurred_at.kind != "known":
            raise ValueError("complete logs require known timestamps")
        p = event.payload
        if isinstance(p, ToolExecutionPayload) and p.capture_scope == "historical_excerpt":
            raise ValueError("complete logs cannot contain historical-excerpt tool captures")
        if isinstance(p, AdapterRequestPayload) and p.final_request_body.kind == "missing":
            raise ValueError("complete logs require the exact final adapter request")
        self._check_ancestry(event)
        # model_construct is safe here: EventEnvelope already validated the closed
        # payload contract, and the headers above preserve complete-mode rules.
        view = EventLog.model_construct(mode="complete", events=(event,),
                                       budget_limit=self.budget_limit, excerpt=None)
        view._validate_single_event_references(self.events_by_id, set())
        if isinstance(p, NetworkRecoveryPayload):
            request = self.events_by_id.get(p.request_event_id)
            if (request is None or not isinstance(request.payload, AdapterRequestPayload)
                    or request.task_id != event.task_id):
                raise ValueError("network recovery must refer to this task's prior model request")
            if p.action == "reserve":
                if p.operation_id in self.network_recovery_reservations:
                    raise ValueError("duplicate network recovery reservation")
            else:
                original = self.network_recovery_reservations.get(p.operation_id)
                if original is None or p.operation_id in self.network_recovery_settled:
                    raise ValueError("network recovery settlement needs a unique reservation")
                q = original.payload
                if (original.task_id, q.phase, q.request_event_id, q.reserved_seconds) != (
                        event.task_id, p.phase, p.request_event_id, p.reserved_seconds):
                    raise ValueError("network recovery settlement differs from reservation")
        if p.event_type == "task_control" and p.command_id in self.command_ids:
            raise ValueError("task control command_id must be unique within a run")
        if isinstance(p, TruncationPayload) and p.response_event_id in self.truncated_responses:
            raise ValueError("a truncated response can be recorded only once")
        if isinstance(p, AnswerRepairPayload):
            if p.phase == "request":
                if event.task_id in self.repair_tasks:
                    raise ValueError("a task can request only one answer repair")
                if p.invalid_response_event_id in self.repaired_invalid_responses:
                    raise ValueError("an invalid answer can request only one repair")
            elif p.repair_request_event_id in self.repair_results:
                raise ValueError("an answer repair request can have only one result")
        if isinstance(p, ToolExecutionPayload):
            if p.invocation_event_id is not None and p.invocation_event_id in self.completed_invocations:
                raise ValueError("tool invocation already has a result")
            if p.applied_write_id is not None:
                if p.applied_write_id in self.applied_ids:
                    raise ValueError(f"duplicate applied_write_id: {p.applied_write_id}")
                if p.operation_key in self.applied_operations:
                    raise ValueError(f"operation applied more than once: {p.operation_key}")
            if p.repeatability != "read_only":
                previous = self.previous_writes.get(p.operation_key)
                if previous is not None:
                    if p.retry_event_id is None:
                        raise ValueError("a repeated write operation needs a recorded retry event")
                    retry = self.events_by_id[p.retry_event_id].payload
                    if (not isinstance(retry, RunLifecyclePayload) or retry.action != "retry"
                            or retry.retry_of_event_id != previous.event_id):
                        raise ValueError("a repeated write retry must target the immediately previous attempt")
        return self.budget.prepare(event)

    def _check_ancestry(self, event):
        if event.task_id == self.root_task_id:
            if event.parent_task.kind != "root":
                raise ValueError("root task events must use root ancestry")
            return
        if event.parent_task.kind != "known":
            raise ValueError("child task events require a known parent")
        parent = event.parent_task.task_id
        if event.task_id in self.parents and self.parents[event.task_id] != parent:
            raise ValueError("task parent changed within a run")
        if parent not in self.parents:
            raise ValueError("parent task does not exist in this run")
        cursor = parent
        while cursor is not None:
            if cursor == event.task_id:
                raise ValueError("task ancestry contains a cycle")
            cursor = self.parents[cursor]

    def commit(self, event, budget_update):
        """Commit a previously prepared event after successful durable append."""
        self.events_by_id[event.event_id] = event
        self.events_by_task.setdefault(event.task_id, []).append(event)
        self.parents[event.task_id] = (None if event.parent_task.kind == "root"
                                      else event.parent_task.task_id)
        self.sequence = event.sequence
        p = event.payload
        if isinstance(p, NetworkRecoveryPayload):
            if p.action == "reserve":
                self.network_recovery_reservations[p.operation_id] = event
            else:
                self.network_recovery_settled.add(p.operation_id)
        if p.event_type == "task_control":
            self.command_ids.add(p.command_id)
        if isinstance(p, BudgetEventPayload) and p.reservation is not None:
            self.reservation_counts[event.task_id] = self.reservation_counts.get(event.task_id, 0) + 1
        if isinstance(p, TruncationPayload):
            self.truncated_responses.add(p.response_event_id)
        if isinstance(p, AnswerRepairPayload):
            if p.phase == "request":
                self.repair_tasks.add(event.task_id)
                self.repaired_invalid_responses.add(p.invalid_response_event_id)
            else:
                self.repair_results.add(p.repair_request_event_id)
        if isinstance(p, ToolExecutionPayload):
            if p.invocation_event_id is not None:
                self.completed_invocations.add(p.invocation_event_id)
            if p.repeatability != "read_only":
                self.previous_writes[p.operation_key] = event
            if p.applied_write_id is not None:
                self.applied_ids.add(p.applied_write_id)
                self.applied_operations.add(p.operation_key)
        self.budget.commit(event, budget_update)
