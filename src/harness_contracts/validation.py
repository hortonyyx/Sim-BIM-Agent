"""Cross-event checks that cannot live on an individual payload."""

from __future__ import annotations

from typing import Literal

from pydantic import model_validator

from .base import ContractModel, NonEmptyStr
from .budget import BudgetAmounts, BudgetLedger
from .events import (
    AdapterRequestPayload,
    AnswerRepairPayload,
    TruncationPayload,
    BudgetEventPayload,
    BudgetOverrunPayload,
    ContextEventPayload,
    CheckpointPayload,
    EventEnvelope,
    ModelResponsePayload,
    NetworkRecoveryPayload,
    RunLifecyclePayload,
    TaskControlPayload,
    StateInspectionPayload,
    ToolExecutionPayload,
    ToolInvocationPayload,
    ToolPresentationPayload,
)
from .refs import SourceRef


class ExcerptDisclosure(ContractModel):
    reason: NonEmptyStr
    omitted_event_types: tuple[NonEmptyStr, ...]
    missing_event_ids: tuple[NonEmptyStr, ...] = ()
    source_records: tuple[SourceRef, ...]


class EventLog(ContractModel):
    mode: Literal["complete", "excerpt"]
    events: tuple[EventEnvelope, ...]
    budget_limit: BudgetAmounts | None = None
    excerpt: ExcerptDisclosure | None = None

    @model_validator(mode="after")
    def validate_event_log(self) -> EventLog:
        if not self.events:
            raise ValueError("event log cannot be empty")
        if self.mode == "excerpt":
            if self.excerpt is None or not self.excerpt.source_records:
                raise ValueError("excerpt mode needs disclosure and source records")
        elif self.excerpt is not None:
            raise ValueError("complete logs cannot carry excerpt disclosure")

        event_by_id: dict[str, EventEnvelope] = {}
        run_ids: set[str] = set()
        previous_sequence = -1
        for event in self.events:
            if event.event_id in event_by_id:
                raise ValueError(f"duplicate event_id: {event.event_id}")
            if event.sequence <= previous_sequence:
                raise ValueError("event sequence must be strictly increasing")
            previous_sequence = event.sequence
            event_by_id[event.event_id] = event
            run_ids.add(event.run_id)
            if self.mode == "complete":
                if event.occurred_at.kind == "historical_missing":
                    raise ValueError("complete logs require known timestamps")
                if event.parent_task.kind == "historical_missing":
                    raise ValueError("complete logs require known task ancestry")
                if (
                    isinstance(event.payload, ToolExecutionPayload)
                    and event.payload.capture_scope == "historical_excerpt"
                ):
                    raise ValueError(
                        "complete logs cannot contain historical-excerpt tool captures"
                    )
                if (
                    isinstance(event.payload, AdapterRequestPayload)
                    and event.payload.final_request_body.kind == "missing"
                ):
                    raise ValueError("complete logs require the exact final adapter request")
        if len(run_ids) != 1:
            raise ValueError("one EventLog represents exactly one run")

        missing_ids = set(self.excerpt.missing_event_ids if self.excerpt else ())
        self._validate_response_requests(event_by_id, missing_ids)
        self._validate_answer_repairs(event_by_id, missing_ids)
        self._validate_truncations(event_by_id, missing_ids)
        self._validate_inspections(event_by_id, missing_ids)
        self._validate_recovery(event_by_id, missing_ids)
        self._validate_network_recovery(event_by_id, missing_ids)
        self._validate_context(event_by_id, missing_ids)
        self._validate_task_controls(event_by_id, missing_ids)
        for event in self.events:
            if isinstance(event.payload, CheckpointPayload):
                _require_prior_event(event.payload.after_event_id, event, event_by_id,
                                     missing_ids, "checkpoint boundary")
        self._validate_budget_events()
        self._validate_duplicate_writes()
        self._validate_invocations(event_by_id, missing_ids)
        self._validate_presentations(event_by_id, missing_ids)
        return self

    def _validate_network_recovery(self, event_by_id, missing_ids):
        operations = {}
        settled = set()
        for event in self.events:
            p = event.payload
            if not isinstance(p, NetworkRecoveryPayload):
                continue
            request = _require_prior_event(p.request_event_id, event, event_by_id,
                                           missing_ids, "network recovery request")
            if request is not None and (not isinstance(request.payload, AdapterRequestPayload)
                                        or request.task_id != event.task_id):
                raise ValueError("network recovery must refer to this task's model request")
            if p.action == "reserve":
                if p.operation_id in operations:
                    raise ValueError("duplicate network recovery reservation")
                operations[p.operation_id] = event
            else:
                original = operations.get(p.operation_id)
                if original is None or p.operation_id in settled:
                    raise ValueError("network recovery settlement needs a unique reservation")
                q = original.payload
                if (original.task_id, q.phase, q.request_event_id, q.reserved_seconds) != (
                        event.task_id, p.phase, p.request_event_id, p.reserved_seconds):
                    raise ValueError("network recovery settlement differs from reservation")
                settled.add(p.operation_id)

    def _validate_task_controls(self, event_by_id, missing_ids) -> None:
        acknowledged = set()
        for event in self.events:
            p = event.payload
            if not isinstance(p, TaskControlPayload):
                continue
            if p.target_task_id != event.task_id:
                raise ValueError("task control must be acknowledged by its target task")
            if p.command_id in acknowledged:
                raise ValueError("task control already acknowledged")
            acknowledged.add(p.command_id)

    def _validate_single_event_references(self, event_by_id, missing_ids) -> None:
        """Shared reference checks for an already typed new event.

        Incremental callers provide a one-event view and the committed index.
        Cross-event uniqueness and budgets belong to their incremental state;
        complete validation above still independently replays the whole log.
        New reference-bearing payloads must be checked in both paths.
        """
        self._validate_response_requests(event_by_id, missing_ids)
        self._validate_answer_repairs(event_by_id, missing_ids)
        self._validate_truncations(event_by_id, missing_ids)
        self._validate_inspections(event_by_id, missing_ids)
        self._validate_recovery(event_by_id, missing_ids)
        self._validate_context(event_by_id, missing_ids)
        self._validate_task_controls(event_by_id, missing_ids)
        for event in self.events:
            if isinstance(event.payload, CheckpointPayload):
                _require_prior_event(event.payload.after_event_id, event, event_by_id,
                                     missing_ids, "checkpoint boundary")
        self._validate_invocations(event_by_id, missing_ids)
        self._validate_presentations(event_by_id, missing_ids)

    def _validate_truncations(self, event_by_id, missing_ids) -> None:
        recorded = set()
        for event in self.events:
            p = event.payload
            if not isinstance(p, TruncationPayload):
                continue
            request = _require_prior_event(p.request_event_id, event,
                event_by_id, missing_ids, "truncated request")
            response = _require_prior_event(p.response_event_id, event,
                event_by_id, missing_ids, "truncated response")
            if request is not None and not isinstance(request.payload, AdapterRequestPayload):
                raise ValueError("truncation must reference an adapter request")
            if response is not None:
                if not isinstance(response.payload, ModelResponsePayload):
                    raise ValueError("truncation must reference a model response")
                if response.payload.request_event_id != p.request_event_id:
                    raise ValueError("truncation request and response do not match")
                if response.payload.tool_calls:
                    raise ValueError("truncated calls must not be exposed as executable")
            if any(e is not None and e.task_id != event.task_id for e in (request, response)):
                raise ValueError("truncation must remain in its original task")
            if p.response_event_id in recorded:
                raise ValueError("a truncated response can be recorded only once")
            recorded.add(p.response_event_id)
            if p.action == "continue" and (p.consecutive_count > p.max_consecutive_recoveries
                    or p.total_count > p.max_total_recoveries):
                raise ValueError("truncation recovery exceeds its configured bound")

    def _validate_answer_repairs(self, event_by_id, missing_ids) -> None:
        requested_invalid_responses: set[str] = set()
        requesting_tasks: set[str] = set()
        results_by_request: set[str] = set()
        for event in self.events:
            payload = event.payload
            if not isinstance(payload, AnswerRepairPayload):
                continue
            invalid = _require_prior_event(payload.invalid_response_event_id, event,
                event_by_id, missing_ids, "invalid answer response")
            if invalid is not None and not isinstance(invalid.payload, ModelResponsePayload):
                raise ValueError("invalid_response_event_id must reference a model response")
            if invalid is not None and invalid.task_id != event.task_id:
                raise ValueError("answer repair must remain in the same task as the invalid response")
            if payload.phase == "request":
                if payload.attempt != 1:
                    raise ValueError("only one bounded answer repair is supported")
                if event.task_id in requesting_tasks:
                    raise ValueError("a task can request only one answer repair")
                if payload.invalid_response_event_id in requested_invalid_responses:
                    raise ValueError("an invalid answer can request only one repair")
                requesting_tasks.add(event.task_id)
                requested_invalid_responses.add(payload.invalid_response_event_id)
                continue
            request = _require_prior_event(payload.repair_request_event_id, event,
                event_by_id, missing_ids, "answer repair request")
            repaired = _require_prior_event(payload.repaired_response_event_id, event,
                event_by_id, missing_ids, "repaired answer response")
            if request is not None and (
                not isinstance(request.payload, AnswerRepairPayload)
                or request.payload.phase != "request"
            ):
                raise ValueError("repair_request_event_id must reference an answer repair request")
            if repaired is not None and not isinstance(repaired.payload, ModelResponsePayload):
                raise ValueError("repaired_response_event_id must reference a model response")
            if request is not None and repaired is not None:
                if request.task_id != event.task_id or repaired.task_id != event.task_id:
                    raise ValueError("answer repair request and response must stay in one task")
                if repaired.sequence <= request.sequence:
                    raise ValueError("a repaired answer response must follow its repair request")
                adapter_request = event_by_id.get(repaired.payload.request_event_id)
                if adapter_request is not None:
                    if adapter_request.task_id != event.task_id:
                        raise ValueError("the repaired model request must stay in the repair task")
                    if adapter_request.sequence <= request.sequence:
                        raise ValueError("the repaired model request must follow the repair request event")
            if payload.repair_request_event_id in results_by_request:
                raise ValueError("an answer repair request can have only one result")
            results_by_request.add(payload.repair_request_event_id)
            if request is not None:
                prior = request.payload
                for field in ("attempt", "invalid_response_event_id", "original_answer", "validation_error"):
                    if getattr(payload, field) != getattr(prior, field):
                        raise ValueError(f"answer repair result differs from its request: {field}")

    def _validate_presentations(self, event_by_id, missing_ids) -> None:
        for event in self.events:
            p = event.payload
            if not isinstance(p, ToolPresentationPayload):
                continue
            execution = _require_prior_event(p.tool_execution_event_id, event, event_by_id,
                missing_ids, "presented tool result")
            request = _require_prior_event(p.request_event_id, event, event_by_id,
                missing_ids, "presentation request")
            response = _require_prior_event(p.response_event_id, event, event_by_id,
                missing_ids, "presentation acknowledgement")
            if execution is None or not isinstance(execution.payload, ToolExecutionPayload):
                raise ValueError("presentation must reference a tool execution")
            if request is None or not isinstance(request.payload, AdapterRequestPayload):
                raise ValueError("presentation must reference an adapter request")
            if response is None or not isinstance(response.payload, ModelResponsePayload):
                raise ValueError("presentation must reference a model response")
            if response.payload.request_event_id != request.event_id or execution.sequence >= request.sequence:
                raise ValueError("presentation must follow tool execution and its accepted request")
            if p.shown_result.kind == "missing":
                raise ValueError("presentation differs from prepared tool result")
            expected_adapter = {"anthropic_messages_v1": "anthropic-messages-http-v1",
                                "responses_v1": "openai-responses-http-v1"}.get(p.protocol_conversion)
            converted = expected_adapter is not None
            if converted and (request.payload.adapter != expected_adapter or not any(
                    injection.source.event_id == execution.event_id for injection in request.payload.injected_content)):
                raise ValueError("protocol conversion needs its native request and tool-result source")
            if (p.shown_result != execution.payload.shown_result and not converted) or p.context_event_id is not None:
                context = _require_prior_event(p.context_event_id, event, event_by_id,
                    missing_ids, "tool context projection")
                if (context is None or not isinstance(context.payload, ContextEventPayload)
                        or context.sequence >= request.sequence):
                    raise ValueError("changed tool presentation requires a prior context projection")

    def _validate_invocations(self, event_by_id, missing_ids) -> None:
        completed: set[str] = set()
        for event in self.events:
            payload = event.payload
            if not isinstance(payload, ToolExecutionPayload) or payload.invocation_event_id is None:
                continue
            invocation = _require_prior_event(payload.invocation_event_id, event,
                event_by_id, missing_ids, "tool invocation")
            if invocation is None or not isinstance(invocation.payload, ToolInvocationPayload):
                raise ValueError("execution must reference a prior tool invocation")
            if invocation.event_id in completed:
                raise ValueError("tool invocation already has a result")
            completed.add(invocation.event_id)
            for field in ("call_id", "tool_name", "full_arguments", "repeatability", "operation_key"):
                if getattr(payload, field) != getattr(invocation.payload, field):
                    raise ValueError(f"execution differs from tool invocation: {field}")

    def _validate_inspections(
        self, event_by_id: dict[str, EventEnvelope], missing_ids: set[str]
    ) -> None:
        for event in self.events:
            payload = event.payload
            if not isinstance(payload, StateInspectionPayload):
                continue
            _require_prior_event(
                payload.target_event_id,
                event,
                event_by_id,
                missing_ids,
                "state inspection target",
            )

    def _validate_response_requests(
        self, event_by_id: dict[str, EventEnvelope], missing_ids: set[str]
    ) -> None:
        for event in self.events:
            payload = event.payload
            if not isinstance(payload, ModelResponsePayload):
                continue
            request = _require_prior_event(
                payload.request_event_id,
                event,
                event_by_id,
                missing_ids,
                "model request",
            )
            if request is not None and not isinstance(
                request.payload, AdapterRequestPayload
            ):
                raise ValueError("request_event_id must reference an adapter request")

    def _validate_recovery(
        self, event_by_id: dict[str, EventEnvelope], missing_ids: set[str]
    ) -> None:
        for event in self.events:
            payload = event.payload
            if not isinstance(payload, RunLifecyclePayload):
                continue
            if payload.model_failure is not None:
                request = _require_prior_event(payload.model_failure.request_event_id,
                    event, event_by_id, missing_ids, "model failure request")
                if request is not None and not isinstance(request.payload, AdapterRequestPayload):
                    raise ValueError("model failure must reference an adapter request")
            if payload.action == "retry":
                original = _require_prior_event(
                    payload.retry_of_event_id,
                    event,
                    event_by_id,
                    missing_ids,
                    "retry target",
                )
                if original is None:
                    continue
                tool = original.payload
                if isinstance(tool, AdapterRequestPayload):
                    continue
                if not isinstance(tool, ToolExecutionPayload):
                    raise ValueError("retry target must be a model request or tool execution")
                if tool.outcome == "succeeded":
                    raise ValueError("a successful tool execution cannot be retried")
                if tool.outcome != "unknown":
                    continue
                if tool.repeatability == "read_only":
                    continue
                inspection = _require_prior_event(
                    payload.state_inspection_event_id,
                    event,
                    event_by_id,
                    missing_ids,
                    "unknown-write state inspection",
                )
                if inspection is None:
                    raise ValueError("unknown write retry needs a recorded state inspection")
                check = inspection.payload
                if not isinstance(check, StateInspectionPayload):
                    raise ValueError("state_inspection_event_id must reference an inspection")
                if (
                    check.purpose != "unknown_write_recovery"
                    or check.target_event_id != original.event_id
                    or check.conclusion != "not_applied"
                ):
                    raise ValueError(
                        "unknown write retry requires persisted state proving not_applied"
                    )
            elif payload.action == "resume":
                inspection = _require_prior_event(
                    payload.state_inspection_event_id,
                    event,
                    event_by_id,
                    missing_ids,
                    "resume state inspection",
                )
                if inspection is None or not isinstance(
                    inspection.payload, StateInspectionPayload
                ):
                    raise ValueError("resume needs a recorded prior state inspection")
                check = inspection.payload
                if check.purpose != "resume" or check.conclusion != "safe_to_resume":
                    raise ValueError("resume inspection must conclude safe_to_resume")
                if payload.checkpoint != check.persisted_state:
                    raise ValueError("resume checkpoint must match the inspected persisted state")

        for event in self.events:
            tool = event.payload
            if not isinstance(tool, ToolExecutionPayload) or tool.retry_event_id is None:
                continue
            retry_event = _require_prior_event(
                tool.retry_event_id,
                event,
                event_by_id,
                missing_ids,
                "tool retry",
            )
            if retry_event is None or not isinstance(
                retry_event.payload, RunLifecyclePayload
            ):
                raise ValueError("retry_event_id must reference a retry lifecycle event")
            retry = retry_event.payload
            if retry.action != "retry":
                raise ValueError("retry_event_id must reference a retry action")
            original = event_by_id.get(retry.retry_of_event_id)
            if original is not None and isinstance(
                original.payload, ToolExecutionPayload
            ):
                if original.payload.operation_key != tool.operation_key:
                    raise ValueError("retried tool must preserve the operation_key")
                if original.payload.tool_name != tool.tool_name:
                    raise ValueError("retried tool must preserve the tool name")
                if original.payload.full_arguments != tool.full_arguments:
                    raise ValueError("retried tool must preserve the full arguments")

        previous_write_by_key: dict[str, EventEnvelope] = {}
        for event in self.events:
            tool = event.payload
            if (
                not isinstance(tool, ToolExecutionPayload)
                or tool.repeatability == "read_only"
            ):
                continue
            operation_key = tool.operation_key
            assert operation_key is not None
            previous = previous_write_by_key.get(operation_key)
            if previous is not None:
                if tool.retry_event_id is None:
                    raise ValueError(
                        "a repeated write operation needs a recorded retry event"
                    )
                retry_event = event_by_id[tool.retry_event_id]
                retry = retry_event.payload
                if (
                    not isinstance(retry, RunLifecyclePayload)
                    or retry.action != "retry"
                    or retry.retry_of_event_id != previous.event_id
                ):
                    raise ValueError(
                        "a repeated write retry must target the immediately previous attempt"
                    )
            previous_write_by_key[operation_key] = event

    def _validate_context(
        self, event_by_id: dict[str, EventEnvelope], missing_ids: set[str]
    ) -> None:
        for event in self.events:
            payload = event.payload
            if not isinstance(payload, ContextEventPayload):
                continue
            if payload.action == "compact":
                for replaced_id in payload.replaced_event_ids:
                    _require_prior_event(
                        replaced_id,
                        event,
                        event_by_id,
                        missing_ids,
                        "compressed event",
                    )
                continue
            if payload.action != "retrieve_image":
                continue
            removal = _require_prior_event(
                payload.removal_event_id,
                event,
                event_by_id,
                missing_ids,
                "image removal",
            )
            if removal is None:
                continue
            previous = removal.payload
            if not isinstance(previous, ContextEventPayload) or previous.action != "remove_image":
                raise ValueError("removal_event_id must reference an image removal")
            if previous.image != payload.image:
                raise ValueError("retrieved image must match the removed image reference")

    def _validate_budget_events(self) -> None:
        reservations = []
        settlements = []
        releases = []
        reconciliations = []
        reservation_events: dict[str, EventEnvelope] = {}
        settlement_events: dict[str, EventEnvelope] = {}
        overrun_reservations: set[str] = set()
        requests_by_reservation: dict[str, EventEnvelope] = {}
        for event in self.events:
            payload = event.payload
            if isinstance(payload, AdapterRequestPayload) and payload.reservation_id:
                if any(r.reservation_id == payload.reservation_id for r in releases):
                    raise ValueError("released reservation cannot be dispatched")
                requests_by_reservation[payload.reservation_id] = event
            if isinstance(payload, BudgetOverrunPayload):
                reservation_event = reservation_events.get(payload.reservation_id)
                settlement_event = settlement_events.get(payload.reservation_id)
                if reservation_event is None or settlement_event is None:
                    raise ValueError(
                        "budget overrun must follow its reservation and settlement"
                    )
                if (
                    reservation_event.task_id != event.task_id
                    or settlement_event.task_id != event.task_id
                ):
                    raise ValueError(
                        "budget overrun must remain in the reservation task"
                    )
                if payload.reservation_id in overrun_reservations:
                    raise ValueError("a settlement can have only one budget overrun event")
                overrun_reservations.add(payload.reservation_id)
                reservation = reservation_event.payload.reservation
                settlement = settlement_event.payload.settlement
                assert reservation is not None and settlement is not None
                if (
                    payload.reserved_tokens != reservation.amounts.tokens
                    or payload.actual_tokens != settlement.effective_tokens
                    or payload.overrun_tokens != settlement.token_overrun
                ):
                    raise ValueError(
                        "budget overrun amounts differ from reservation or settlement"
                    )
                continue
            if not isinstance(payload, BudgetEventPayload):
                continue
            if self.budget_limit is None:
                raise ValueError("budget events require the run's total budget limit")
            if payload.reservation is not None:
                if payload.reservation.task_id != event.task_id:
                    raise ValueError("budget reservation task must match event task")
                current = BudgetLedger(
                    total_limit=self.budget_limit,
                    reservations=tuple(reservations),
                    settlements=tuple(settlements),
                    releases=tuple(releases), reconciliations=tuple(reconciliations),
                )
                _require_available_for_reservation(
                    payload.reservation.amounts, current.available
                )
                reservations.append(payload.reservation)
                reservation_events[payload.reservation.reservation_id] = event
            if payload.settlement is not None:
                reservation_event = reservation_events.get(
                    payload.settlement.reservation_id
                )
                if reservation_event is None:
                    raise ValueError("budget must be reserved before it is settled")
                if reservation_event.task_id != event.task_id:
                    raise ValueError("budget settlement task must match reservation task")
                settlements.append(payload.settlement)
                settlement_events[payload.settlement.reservation_id] = event
            if payload.action in {"release", "reconcile"}:
                record = payload.release if payload.action == "release" else payload.reconciliation
                assert record is not None
                validate_budget_recovery_event(event,
                    reservation_events.get(record.reservation_id),
                    settlement_events.get(record.reservation_id),
                    requests_by_reservation.get(record.reservation_id))
                if payload.release is not None:
                    releases.append(payload.release)
                else:
                    reconciliations.append(payload.reconciliation)
            # Validate every journal prefix. A later settlement may release a
            # conservative hold, but it cannot erase evidence that an earlier
            # reservation was admitted over the configured total.
            BudgetLedger(
                total_limit=self.budget_limit,
                reservations=tuple(reservations),
                settlements=tuple(settlements),
                releases=tuple(releases), reconciliations=tuple(reconciliations),
            )

    def _validate_duplicate_writes(self) -> None:
        applied_ids: set[str] = set()
        applied_operations: set[str] = set()
        for event in self.events:
            payload = event.payload
            if not isinstance(payload, ToolExecutionPayload):
                continue
            if payload.applied_write_id is None:
                continue
            if payload.applied_write_id in applied_ids:
                raise ValueError(f"duplicate applied_write_id: {payload.applied_write_id}")
            applied_ids.add(payload.applied_write_id)
            if payload.operation_key in applied_operations:
                raise ValueError(
                    f"operation applied more than once: {payload.operation_key}"
                )
            applied_operations.add(payload.operation_key)  # type: ignore[arg-type]


def validate_budget_recovery_event(
    event: EventEnvelope,
    reservation_event: EventEnvelope | None,
    settlement_event: EventEnvelope | None,
    request_event: EventEnvelope | None,
) -> None:
    """Shared full/incremental cross-event checks; caller supplies prior indexes."""
    payload = event.payload
    if not isinstance(payload, BudgetEventPayload) or payload.action not in {"release", "reconcile"}:
        raise ValueError("expected a budget recovery event")
    if reservation_event is None or reservation_event.sequence >= event.sequence:
        raise ValueError("budget recovery requires a prior reservation")
    if reservation_event.task_id != event.task_id:
        raise ValueError("budget recovery must remain in the reservation task")
    if payload.action == "release":
        if request_event is not None or settlement_event is not None:
            raise ValueError("only an unsent, unsettled reservation can be released")
        return
    receipt = payload.reconciliation
    assert receipt is not None
    if (settlement_event is None or settlement_event.event_id != receipt.settlement_event_id
            or settlement_event.sequence >= event.sequence):
        raise ValueError("reconciliation must bind its prior original settlement")
    if (request_event is None or request_event.event_id != receipt.request_event_id
            or request_event.sequence >= settlement_event.sequence):
        raise ValueError("reconciliation must bind the reservation's prior request")
    if settlement_event.task_id != event.task_id or request_event.task_id != event.task_id:
        raise ValueError("reconciliation references must remain in the reservation task")
    if receipt.evidence not in event.source_refs:
        raise ValueError("reconciliation evidence must also be an event source")


def validate_event_log(events: tuple[EventEnvelope, ...], **kwargs: object) -> EventLog:
    """Convenience helper for callers that already accumulated event envelopes."""

    return EventLog(events=events, **kwargs)


def _require_available_for_reservation(
    requested: BudgetAmounts, available: BudgetAmounts
) -> None:
    """Reject a new hold against the actual capacity remaining at that event."""

    for name in ("tokens", "money_usd", "money_cny", "seconds", "calls"):
        amount = getattr(requested, name)
        remaining = getattr(available, name)
        if name == "money_cny" and remaining is not None and amount is None:
            raise ValueError("CNY-limited request requires a money reservation")
        if amount is not None and remaining is not None and amount > remaining:
            raise ValueError(f"budget reservation exceeds available budget ({name})")


def _require_prior_event(
    referenced_id: str | None,
    current: EventEnvelope,
    event_by_id: dict[str, EventEnvelope],
    missing_ids: set[str],
    label: str,
) -> EventEnvelope | None:
    if referenced_id is None:
        raise ValueError(f"{label} reference is required")
    referenced = event_by_id.get(referenced_id)
    if referenced is None:
        if referenced_id in missing_ids:
            return None
        raise ValueError(f"{label} event does not exist: {referenced_id}")
    if referenced.sequence >= current.sequence:
        raise ValueError(f"{label} must precede the referencing event")
    return referenced
