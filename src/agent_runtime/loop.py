"""Audited single-role runtime with bounded context and durable recovery."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import math
import time
import weakref
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Literal
from time import perf_counter

from pydantic import Field

from src.harness_contracts import (
    AnswerRepairPayload, BudgetAmounts, BudgetEventPayload, BudgetOverrunPayload, BudgetWaitPayload, CheckpointPayload, HashedBlobRef,
    EstimatedCostUpperBound, MissingCapture, NetworkRecoveryPayload, RunAggregateUsagePayload, RunLifecyclePayload, SourceRef,
    StateInspectionPayload, ToolExecutionPayload, ToolFailureDetails, ToolInvocationPayload,
    ToolPresentationPayload, UsageMissing, UsageReported, authorize_tool_call,
    TruncationPayload, TaskControlPayload,
)
from src.harness_contracts.base import ContractModel
from .adapter import (convert_tool_result, parse_response, prepare_request,
                      reasoning_history_messages, reported_tokens)
from .accounting import (account_request_usage, bills_images_separately,
    get_cny_price_schedule, require_cny_price_schedule, request_accounting_from_store, summarize_request_accounting)
from .budget import PriceSchedule, RequestEstimate, RuntimeBudget
from .budget_projection import load_budget_projection
from .estimation import get_model_profile, estimate_chat_request
from .failures import (ModelServiceError, classify_failure, is_unprocessed_rejection,
                       rejected_request_usage, safe_exception_details)
from .dispatch import get_dispatcher, route_policies
from src.harness_contracts.events import ModelFailureDetails
from .output_limits import validate_output_limit
from .store import EventStore


NO_DEADLINE_IO_TIMEOUT_SECONDS = 300.0
MAX_DOMAIN_COMPLETION_CONTINUATIONS = 2


class _BudgetCapacitySignal:
    """Event-loop local notification for released shared root reservations."""

    def __init__(self):
        self.revision = 0
        self.active_reservations: set[str] = set()
        self.waiters: deque[tuple[asyncio.Future, int]] = deque()

    def start(self, reservation_id: str) -> None:
        self.active_reservations.add(reservation_id)

    def complete(self, reservation_id: str) -> None:
        if reservation_id not in self.active_reservations:
            return
        self.active_reservations.discard(reservation_id)
        self.revision += 1
        self._wake_head()

    def _wake_head(self) -> None:
        if not self.waiters:
            return
        waiter, observed_revision = self.waiters[0]
        if self.revision != observed_revision and not waiter.done():
            waiter.set_result(None)

    async def wait_for_change(self, revision: int, *, timeout: float | None) -> bool:
        if self.revision != revision and not self.waiters:
            return True
        waiter = asyncio.get_running_loop().create_future()
        entry = (waiter, revision)
        self.waiters.append(entry)
        try:
            self._wake_head()
            try:
                await asyncio.wait_for(waiter, timeout=timeout)
            except TimeoutError:
                return False
            return True
        finally:
            if entry in self.waiters:
                self.waiters.remove(entry)
            self._wake_head()


_BUDGET_CAPACITY_SIGNALS = weakref.WeakKeyDictionary()
_RECOVERY_CAPACITY_SIGNALS = weakref.WeakKeyDictionary()


def _shared_budget_capacity_signal(store: EventStore) -> _BudgetCapacitySignal:
    root = store._root
    signal = _BUDGET_CAPACITY_SIGNALS.get(root)
    if signal is None:
        signal = _BudgetCapacitySignal()
        _BUDGET_CAPACITY_SIGNALS[root] = signal
    return signal


def _shared_recovery_capacity_signal(store):
    root = store._root
    if root not in _RECOVERY_CAPACITY_SIGNALS:
        _RECOVERY_CAPACITY_SIGNALS[root] = _BudgetCapacitySignal()
    return _RECOVERY_CAPACITY_SIGNALS[root]


class RunLimits(ContractModel):
    model_calls: int = Field(ge=1)
    tool_calls: int = Field(ge=0)
    seconds: float | None = Field(gt=0)
    # Omit the legacy default from frozen JSON so old checkpoints and worker
    # configurations without this field remain byte-shape compatible.
    time_budget_mode: Literal["wall_and_requests", "wall"] = Field(
        default="wall_and_requests", exclude_if=lambda mode: mode == "wall_and_requests")
    tokens: int | None = Field(ge=1)
    money_usd: Decimal | None = Field(default=None, ge=0)
    money_cny: Decimal | None = Field(default=None, ge=0)
    near_limit: Literal["stop", "reduce_output"] = "stop"
    min_output_tokens: int = Field(default=1, ge=1)
    context_tokens: int | None = Field(default=None, ge=1)
    max_model_retries: int = Field(default=2, ge=0)
    retry_backoff_seconds: float = Field(default=1.0, ge=0)
    model_reconnect_seconds: float = Field(default=0.0, ge=0, allow_inf_nan=False, strict=True)
    max_consecutive_truncations: int = Field(default=2, ge=0)
    max_total_truncations: int = Field(default=3, ge=0)
    summary_every: int = Field(default=0, ge=0)
    max_tool_recovery_retries: int = Field(default=1, ge=0)

    def ledger_limit(self):
        return BudgetAmounts(tokens=self.tokens, calls=self.model_calls,
            seconds=(Decimal(str(self.seconds))
                     if self.seconds is not None and self.time_budget_mode == "wall_and_requests" else None),
            money_usd=self.money_usd, money_cny=self.money_cny)


@dataclass
class Runtime:
    store: EventStore
    adapter: object
    tools: object
    role: object
    model: str
    parameters: dict
    versions: object
    limits: RunLimits
    echo_fields: tuple[str, ...] = ("reasoning_content",)
    reasoning_history: Literal["all", "current_tool_chain"] = "all"
    context_policy: object | None = None
    pricing: PriceSchedule | None = None
    context_update: object | None = None
    fault_hook: object | None = None
    required_context_tags: tuple[str, ...] = ()
    required_view_ids: tuple[str, ...] = ()
    retrieve_images: tuple[tuple[str, str], ...] = ()
    strict_model_profile: bool = False
    root_tool_calls: int | None = None
    answer_validator: object | None = None
    max_answer_repairs: int = 0
    low_output_limit_reason: str | None = None
    # An entrypoint may start the clock before preparing its tool service.
    # Resume still restores the original persisted start; it never buys time.
    start_epoch: float | None = None
    finalize_run: object | None = None
    # Optional required domain attestation, checked between complete tool batches.
    # Text alone cannot complete an attested task or replace its completion record.
    domain_completion: object | None = None
    tool_budget_update: object | None = None
    # Parallel roles reserve a bounded slice per in-flight request so the first
    # reader cannot hold the entire shared time budget. None preserves the
    # established single-model deadline and exact request path.
    request_timeout_seconds: float | None = None
    reconnect_consumed_before_seconds: float = 0.0
    _phase_seconds: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _control_applied: set[str] = field(default_factory=set, init=False, repr=False)
    _control_paused: bool = field(default=False, init=False, repr=False)

    async def run(self, messages: list[dict], *, message_sources=None,
                  image_originals=None, resume=False) -> dict:
        if self.versions.remote_model.route_id == "deepseek" and self.reasoning_history != "all":
            raise ValueError("DeepSeek tool calls require reasoning_history=all")
        if self.versions.remote_model.route_id == "opencode-go":
            from .opencode_go import model_contract
            if model_contract(self.model)[0] == "openai_responses" and self.reasoning_history != "all":
                raise ValueError("OpenCode Go Responses requires reasoning_history=all")
            if self.pricing is not None or self.limits.money_usd is not None or self.limits.money_cny is not None:
                raise ValueError("OpenCode Go subscription does not use a metered API money budget")
        if self.versions.remote_model.route_id == "chatgpt-subscription":
            if self.reasoning_history != "all":
                raise ValueError("ChatGPT subscription requires reasoning_history=all")
            if self.limits.near_limit != "stop":
                raise ValueError("ChatGPT subscription output allowance is local only; reduce_output is unsupported")
            if self.pricing is not None or self.limits.money_usd is not None or self.limits.money_cny is not None:
                raise ValueError("ChatGPT subscription does not use a metered API money budget")
        if self.request_timeout_seconds is not None and self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if (not math.isfinite(self.reconnect_consumed_before_seconds)
                or self.reconnect_consumed_before_seconds < 0):
            raise ValueError("inherited reconnect consumption must be finite and nonnegative")
        validate_output_limit(self.model, self.parameters.get("max_tokens",
            self.parameters.get("max_completion_tokens")), reason=self.low_output_limit_reason)
        self.started = time.monotonic()
        now = time.time()
        self.started_epoch = now if self.start_epoch is None else self.start_epoch
        self.elapsed_before = max(0.0, now - self.started_epoch)
        self.messages, self.sources = [], []
        self.originals = dict(image_originals or {})
        self.used_ids = set()
        self.pending_response_id = None
        self.pending_pictures, self.pending_picture_events = [], []
        self.answer = None
        self.completion = None
        self.incomplete_response_count = 0
        self.stage = "initialization"
        self.terminal_reason = None
        self.retry_of = None
        self.redispatch_of = None
        self._queue_interruption = None
        self.answer_repair_request_id = None
        self.answer_repair_response_id = None
        self.answer_repair_original = None
        self.answer_repair_error = None
        self.last_summary_at = 0
        self.context = None
        self._control_applied = set()
        self._control_paused = False
        self._phase_seconds = {}
        self._context_calibration = None
        self._context_calibration_cursor = 0
        self._context_calibration_requests = {}
        self._budget_capacity = _shared_budget_capacity_signal(self.store)
        self._recovery_capacity = _shared_recovery_capacity_signal(self.store)
        # Checkpoints reuse the last observed tool state. Every actual tool
        # still gets fresh before/after snapshots, and resume re-reads disk.
        self._tool_state = None
        if self.max_answer_repairs not in (0, 1):
            raise ValueError("max_answer_repairs must be zero or one")
        if self.context_policy is not None:
            from .context import ContextManager
            self.context = ContextManager(self.store, policy=self.context_policy)
        if self.store.is_root_task and self.store.budget_limit != self.limits.ledger_limit():
            raise ValueError("runtime limits differ from persisted budget")
        for name, value in self.limits.ledger_limit().model_dump().items():
            cap = getattr(self.role.budget, name)
            if cap is not None and (value is None or value > cap):
                raise ValueError(f"runtime exceeds role budget: {name}")
        self._load_budget()
        self._refresh_counts()
        specs = await self.tools.list_tools()
        if self.context and self.context.policy.image_history_batches is not None:
            from .context import context_image_tool_spec
            from src.harness_contracts import ToolGrant
            spec = context_image_tool_spec()
            if any(row["name"] == spec["name"] for row in specs):
                raise ValueError("tool catalog collides with runtime context image reader")
            specs = [*specs, spec]
            if not any(grant.tool_name == spec["name"] for grant in self.role.tool_whitelist):
                self.role = self.role.model_copy(update={"tool_whitelist": (*self.role.tool_whitelist,
                    ToolGrant(tool_name=spec["name"], access="read"))})
        self.specs = [{"type": "function", "function": {
            "name": t["name"], "description": t.get("description", ""),
            "parameters": t["inputSchema"]}} for t in specs]
        self.spec_by_name = {t["name"]: t for t in specs}
        self.tool_source = self.store.source("mcp-tool-definitions", self.specs)
        if self.store.events:
            if not resume:
                raise ValueError("existing journal requires explicit resume")
            reason = self._restore()
            if reason == "already_completed":
                return {**self._completed_receipt,
                        "resume_status": reason}
            if reason:
                return self._stop(reason)
        else:
            self.store.append(RunLifecyclePayload(action="start", reason="single-role run started"),
                source_refs=(self.store.source("runtime-config", self._config()),
                    self.store.source("task-timing", {"started_epoch": self.started_epoch}),))
            sources = list(message_sources or [self.store.source(f"initial-message-{i}", m,
                kind="user" if m.get("role") == "user" else "runtime")
                for i, m in enumerate(messages)])
            for message, source in zip(messages, sources, strict=True):
                self._append_message(message, source, tags=("runtime-initial-input",)
                    if self.context and self.context.policy.image_history_batches is not None else ())
            if self.context:
                from .context import StateEntry
                for i, (message, source) in enumerate(zip(messages, sources, strict=True)):
                    if message.get("role") == "user":
                        # Keep the full user input, rather than an inferred paraphrase.
                        self.context.set_state(StateEntry(key=f"user-input-{i}",
                            category="user_requirement", value=(message["content"] if isinstance(message.get("content"), str)
                                else [b for b in message.get("content", []) if b.get("type") != "image_url"]),
                            epistemic_status="user_stated", source_refs=(source,)))
                        self.context.set_state(StateEntry(key=f"user-constraints-{i}",
                            category="constraint", value={"user_input_key": f"user-input-{i}",
                                "basis": "Keep constraints in the complete original user statement; no semantic extraction or expansion."},
                            epistemic_status="user_stated", source_refs=(source,)))
            self._checkpoint()
        try:
            for view_id, digest in self.retrieve_images:
                if self.context is None:
                    raise ValueError("image retrieval requires context management")
                self.context.retrieve_image(view_id, digest)
                self._checkpoint()
            while True:
                if self.completion is not None and self.terminal_reason == "completed":
                    return self._stop("completed")
                if not self.pending_response_id and not self.terminal_reason and self.domain_completion:
                    completion = self.domain_completion(self)
                    if completion is not None:
                        if not isinstance(completion, dict) or not completion:
                            raise ValueError("domain completion must be a nonempty evidence record")
                        self.completion = completion
                        self.terminal_reason = "completed"
                        self._checkpoint()
                        return self._stop("completed")
                # A complete tool interaction stays intact. Operator messages
                # enter immediately before the next model request, never
                # between an assistant tool call and its tool result.
                if not self.pending_response_id:
                    reason = await self._control_boundary()
                    if reason:
                        return self._stop(reason)
                if self.terminal_reason:
                    return self._stop(self.terminal_reason)
                if self.pending_response_id:
                    reason = await self._execute_pending()
                    if reason:
                        return self._stop(reason)
                    continue
                budget_revision = self._budget_capacity.revision
                reason = self._budget_stop()
                if reason:
                    reason = await self._wait_for_transient_root_time_reservation(
                        reason, revision=budget_revision
                    )
                    if reason is None:
                        continue
                    return self._stop(reason)
                # Deterministic projection always precedes any optional paid summary.
                projection = self._project()
                if (self.context and self.limits.summary_every
                        and self.answer_repair_request_id is None
                        and self.counts["tool_calls"] - self.last_summary_at >= self.limits.summary_every):
                    reason = await self._summarize()
                    if reason:
                        return self._stop(reason)
                    projection = self._project()
                if self.context:
                    # Make selection/retrieval decisions recoverable before sending.
                    self._checkpoint()
                self._fault("before_request")
                result, reason = await self._model_call(
                    projection[0], projection[1], purpose="retry" if self.retry_of else "primary_task",
                    tools=[] if self.answer_repair_request_id else self.specs,
                    context_event_id=projection[2])
                if reason:
                    return self._stop(reason)
                self._accept_response(result)
                self.retry_of = self.redispatch_of = None
                self._checkpoint()
        except (Exception, asyncio.CancelledError) as exc:
            return self._exception_stop(exc)

    @contextmanager
    def _measure(self, phase):
        # Observation timing stays separate from the injected admission clock.
        started = perf_counter()
        try:
            yield
        finally:
            self._phase_seconds[phase] = self._phase_seconds.get(phase, 0.0) + perf_counter() - started

    def _apply_control(self, event):
        payload = event.payload
        if payload.command_id in self._control_applied:
            return
        command = json.loads(self.store.get_bytes(payload.command))
        if (command["run_id"] != self.store.run_id or command["target_task_id"] != self.store.task_id
                or command["command_id"] != payload.command_id or command["action"] != payload.action
                or command.get("text") != payload.message):
            raise ValueError("control command differs from its recorded acknowledgement")
        if payload.action == "message":
            source = SourceRef(source_id=payload.command_id, source_kind="user",
                locator=f"operator:{command['source']}", event_id=event.event_id, blob=payload.command)
            self._append_message({"role": "user", "content": payload.message}, source)
            if self.context:
                from .context import StateEntry
                self.context.set_state(StateEntry(key=f"operator-input-{payload.command_id}",
                    category="user_requirement", value=payload.message, epistemic_status="user_stated",
                    source_refs=(source,)))
            # A correction arriving during a final response still gets one
            # ordinary budgeted turn before the run has been closed.
            if self.terminal_reason == "completed":
                self.terminal_reason, self.answer = None, None
        else:
            self._control_paused = payload.action == "pause"
        self._control_applied.add(payload.command_id)

    async def _control_boundary(self):
        from .control import read_commands
        while True:
            changed = False
            for command in read_commands(self.store.directory):
                if command.run_id != self.store.run_id:
                    raise ValueError("control inbox contains another run's command")
                if command.target_task_id != self.store.task_id or command.command_id in self._control_applied:
                    continue
                blob = self.store.put_json(command.model_dump(mode="json"))
                event = self.store.append(TaskControlPayload(command_id=command.command_id,
                    action=command.action, target_task_id=self.store.task_id,
                    command=blob, message=command.text), source_refs=(SourceRef(
                        source_id=command.command_id, source_kind="user",
                        locator=f"operator:{command.source}", blob=blob),))
                self._fault("after_control_event")
                self._apply_control(event)
                changed = True
            if changed:
                self._checkpoint()
            if not self._control_paused:
                return None
            self.stage = "paused"
            remaining = self._remaining()
            if remaining <= 0:
                return self._scoped_budget_reason("time", task=True)
            # Pause does not cancel remote work or buy more wall-clock time.
            await asyncio.sleep(min(0.1, remaining))

    def _effective_echo_fields(self):
        # Resolve before both initial capture and resume comparisons, including
        # callers that inspect _config() before run(). Other routes stay intact.
        if self.versions.remote_model.route_id == "opencode-go" and self.model == "kimi-k3":
            from .opencode_go import KIMI_CHAT_REASONING_FIELDS
            return tuple(dict.fromkeys((*self.echo_fields, *KIMI_CHAT_REASONING_FIELDS)))
        return self.echo_fields

    def _config(self):
        model_profile = asdict(self._effective_model_profile())
        # The persisted JSON decodes tuples as lists. Normalize here so resume
        # compares the live profile with the same shape that was saved.
        model_profile["aliases"] = list(model_profile["aliases"])
        return {"model": self.model, "parameters": self.parameters,
            "model_profile": model_profile,
            "strict_model_profile": self.strict_model_profile,
            "root_tool_calls": self.root_tool_calls,
            "role": self.role.model_dump(mode="json"),
            "versions": self.versions.model_dump(mode="json"),
            "limits": self.limits.model_dump(mode="json"),
            "context_policy": self.context_policy.model_dump(mode="json") if self.context_policy else None,
            "pricing": self.pricing.model_dump(mode="json") if self.pricing else None,
            "echo_fields": list(self._effective_echo_fields()),
            "reasoning_history": self.reasoning_history,
            **({"route_dispatch": asdict(route_policies()[self.versions.remote_model.route_id])}
               if self.versions.remote_model.route_id in route_policies() else {}),
            "tool_budget_update_enabled": self.tool_budget_update is not None,
            **({"domain_completion_enabled": True,
                "domain_completion_continuation_limit": MAX_DOMAIN_COMPLETION_CONTINUATIONS}
               if self.domain_completion else {}),
            "answer_validation_enabled": self.answer_validator is not None,
            "max_answer_repairs": self.max_answer_repairs,
            "low_output_limit_reason": self.low_output_limit_reason,
            "required_context_tags": list(self.required_context_tags),
            "required_view_ids": list(self.required_view_ids),
            **({"model_io_idle_timeout_seconds": NO_DEADLINE_IO_TIMEOUT_SECONDS}
               if self.limits.seconds is None else {}),
            **({"request_timeout_seconds": self.request_timeout_seconds}
               if self.request_timeout_seconds is not None else {}),
            **({"reconnect_consumed_before_seconds": self.reconnect_consumed_before_seconds}
               if self.reconnect_consumed_before_seconds else {})}

    def _load_budget(self):
        self.cny_pricing = (require_cny_price_schedule(self.model,
            route_id=self.versions.remote_model.route_id)
            if self.store.budget_limit.money_cny is not None or self.limits.money_cny is not None else None)
        profile_minimum = get_model_profile(self.model).recommended_min_output_tokens or 1
        minimum = self.limits.min_output_tokens
        if self.low_output_limit_reason is None:
            minimum = max(minimum, profile_minimum)
        self.budget = load_budget_projection(self.store, self.store.budget_limit,
            near_limit_policy=self.limits.near_limit, min_output_tokens=minimum,
            pricing=self.pricing, cny_pricing=self.cny_pricing)
        self.task_budget = load_budget_projection(self.store, self.limits.ledger_limit(), task_id=self.store.task_id,
            near_limit_policy=self.limits.near_limit, min_output_tokens=minimum,
            pricing=self.pricing, cny_pricing=self.cny_pricing)

    def _refresh_counts(self):
        requests = [e for e in self.store.events if e.payload.event_type == "adapter_request"]
        usages = {e.payload.request_event_id: e.payload.usage for e in self.store.events
                  if e.payload.event_type == "model_response"}
        settlements = {s.reservation_id: s for s in self.task_budget.ledger.effective_settlements}
        for request in requests:
            settlement = settlements.get(request.payload.reservation_id)
            if settlement is not None:
                # HTTP failures can carry a usage receipt without a successful
                # chat response; account for each actual request exactly once.
                usages[request.event_id] = settlement.usage
        self.counts = {"model_calls": len(requests),
            "tool_calls": sum(e.payload.event_type == "tool_invocation" for e in self.store.events),
            "reported_tokens": sum(reported_tokens(usage) or 0 for usage in usages.values()),
            "reserved_tokens": self.task_budget.ledger.committed.tokens or 0,
            "usage_complete": len(requests) == len(usages) and all(reported_tokens(usage) is not None for usage in usages.values())}

    def remaining_budget_status(self):
        """Read the existing ledger conservatively, without enlarging any cap.

        Root reservations include children, summaries, unknown calls and retries;
        private usage of an external coordinating model is not observable here.
        """
        self._load_budget()
        dimensions = {}
        for name in ("seconds", "tokens", "money_usd", "money_cny", "calls"):
            caps = [(getattr(b.total_limit, name), getattr(b.available, name))
                    for b in (self.budget, self.task_budget)
                    if getattr(b.total_limit, name) is not None]
            if name == "seconds" and self.limits.time_budget_mode == "wall" and self.limits.seconds is not None:
                # Wall-only runs still publish their deadline to tools/models;
                # the unbounded request-time ledger is observation, not a cap.
                caps.append((Decimal(str(self.limits.seconds)), Decimal(str(self._remaining()))))
            if not caps:
                continue
            remaining = min(value for _, value in caps)
            if name == "seconds":
                if self.limits.seconds is not None:
                    remaining = min(remaining, Decimal(str(self._remaining())))
                caps = [(limit, min(value, remaining)) for limit, value in caps]
            dimensions[name] = {"remaining": str(max(0, remaining)),
                "remaining_fraction": float(min(
                    max(0, value) / limit if limit > 0 else 0 for limit, value in caps))}
        tool_caps = [(self.limits.tool_calls, self.counts["tool_calls"])]
        if self.root_tool_calls is not None:
            tool_caps.append((self.root_tool_calls, sum(
                e.payload.event_type == "tool_invocation" for e in self.store.all_events)))
        dimensions["tool_calls"] = {"remaining": str(min(max(0, limit - used) for limit, used in tool_caps)),
            "remaining_fraction": min(max(0, limit - used) / limit if limit else 0 for limit, used in tool_caps)}
        return {"schema_version": 1, "scope": "runtime_managed_requests_only",
                "external_coordinator_usage": "unavailable", "dimensions": dimensions}

    def _project(self, *, force_compact=False, target_tokens=None):
        with self._measure("context_projection"):
            return self._project_unmeasured(force_compact=force_compact, target_tokens=target_tokens)

    def _project_unmeasured(self, *, force_compact=False, target_tokens=None):
        if self.context is None:
            return self.messages, self.sources, None
        pressure, target = self._context_pressure()
        p = self.context.project(required_tags=self.required_context_tags,
            required_view_ids=self.required_view_ids, consume_retrievals=False,
            request_token_estimator=self._estimate_context_tokens,
            pressure_tokens=pressure, target_tokens=target if target_tokens is None else target_tokens,
            force_compact=force_compact,
            undelivered_image_event_ids=self._undelivered_image_events())
        last = p.decision_event_ids[-1] if p.decision_event_ids else next((
            e.event_id for e in reversed(self.store.events) if e.payload.event_type == "context"), None)
        return list(p.messages), list(p.sources), last

    def _undelivered_image_events(self):
        if self.context.policy.image_history_batches is None:
            return ()
        def image_hashes(capture):
            if capture.kind == "missing":
                return ()
            if hasattr(capture, "images"):
                return tuple(sorted(ref.image.sha256 for ref in capture.images
                                    if ref.location.startswith("/image_blocks/")))
            from .adapter import decode_image_url
            import hashlib
            value = self.store.resolve(capture)
            return tuple(sorted(hashlib.sha256(decode_image_url(block["image_url"]["url"])[0]).hexdigest()
                for block in value.get("image_blocks", []) if block.get("type") == "image_url"))

        presentations = {}
        for event in self.store.events:
            if event.payload.event_type == "tool_presentation":
                presentations.setdefault(event.payload.tool_execution_event_id, []).append(event.payload.shown_result)
        return tuple(event.event_id for event in self.store.events
            if event.payload.event_type == "tool_execution" and event.payload.outcome != "unknown"
            and (expected := image_hashes(event.payload.shown_result))
            and not any(image_hashes(shown) == expected for shown in presentations.get(event.event_id, ())))

    def _is_context_image_tool(self, name):
        return (name == "read_context_image" and self.context is not None
                and self.context.policy.image_history_batches is not None)

    def _tool_repeatability(self, name):
        return "read_only" if self._is_context_image_tool(name) else self.tools.repeatability(name)

    async def _call_tool(self, name, arguments):
        if not self._is_context_image_tool(name):
            return await self.tools.call_tool(name, arguments)
        try:
            action = arguments.get("action", "read")
            if action == "list":
                if set(arguments) - {"action", "limit", "source_tool", "through_index", "before_index"}:
                    raise ValueError("list accepts only limit, source_tool, through_index and before_index")
                metadata = self.context.list_images(**{key: value for key, value in arguments.items()
                                                       if key != "action"})
                return {"content": [{"type": "text", "text": json.dumps(metadata, ensure_ascii=False)}],
                    "structuredContent": metadata, "isError": False}
            if (action != "read" or set(arguments) - {"action", "view_id", "sha256"}
                    or not all(isinstance(arguments.get(key), str) for key in ("view_id", "sha256"))):
                raise ValueError("read requires view_id and sha256 strings, with no list arguments")
            view_id, sha256 = arguments["view_id"], arguments["sha256"]
            raw = self.context.retrieve_image(view_id, sha256, append_to_history=False)
            image = next(row for row in self.context.images if row.key == self.context.image_key(view_id, sha256))
            metadata = {"view_id": view_id, "sha256": sha256,
                "registered_source": image.source.model_dump(mode="json"),
                "note": "Exact previously registered bytes; reopening does not create or validate a new observation."}
            return {"content": [{"type": "text", "text": json.dumps(metadata, ensure_ascii=False)},
                {"type": "image", "mimeType": image.image.media_type,
                 "data": base64.b64encode(raw).decode("ascii"),
                 **({"_meta": {"codex/imageDetail": image.detail}} if image.detail is not None else {})}],
                "structuredContent": metadata, "isError": False}
        except (KeyError, ValueError, TypeError) as error:
            return {"content": [{"type": "text", "text":
                ("Image directory request failed: " + str(error) if arguments.get("action") == "list" else
                 "Exact image lookup failed: use view_id and sha256 from this task's archived_image reference "
                 "or discover saved identities with read_context_image(action='list').")}],
                "isError": True}

    def _refresh_context_calibration(self):
        route = self.versions.remote_model.route_id
        from .opencode_go import model_contract
        if route != "chatgpt-subscription" and not (route == "opencode-go" and model_contract(self.model)[0] == "openai_responses"):
            return None
        if self._context_calibration is None:
            from .responses import ResponsesUsageCalibration
            self._context_calibration = ResponsesUsageCalibration(self.model)
        # The role-local event projection is the persistence mechanism. Resume
        # rebuilds from the same receipts; sibling models never calibrate us.
        for event in self.store.events:
            if event.sequence <= self._context_calibration_cursor:
                continue
            payload = event.payload
            if payload.event_type == "adapter_request":
                self._context_calibration_requests[event.event_id] = payload.final_request_body
            elif (payload.event_type == "model_response"
                  and payload.request_event_id in self._context_calibration_requests):
                body = self.store.resolve(self._context_calibration_requests[payload.request_event_id])
                raw = self.store.resolve(payload.raw_response)
                self._context_calibration.observe(body, raw, response_event_id=event.event_id)
            self._context_calibration_cursor = event.sequence
        return self._context_calibration

    def _effective_model_profile(self):
        if self.versions.remote_model.route_id == "opencode-go":
            from .opencode_go import model_profile
            return model_profile(self.model)
        return get_model_profile(self.model, strict=self.strict_model_profile)

    def _context_pressure(self):
        profile = self._effective_model_profile()
        windows = [n for n in (profile.context_window_tokens, self.limits.context_tokens) if n is not None]
        window = min(windows) if windows else None
        explicit = self.context.policy.compact_at_tokens
        output = self.parameters.get("max_tokens", self.parameters.get("max_completion_tokens", 1))
        calibration = self._refresh_context_calibration()
        growth = calibration.largest_growth if calibration else 0
        # Leave room for another output and a recently observed large input
        # increment. Window-relative uncertainty replaces a universal cutoff.
        headroom = max(output, growth, (window or 0) // 50)
        automatic = max(1, window - headroom) if window else None
        pressure = (min(explicit, automatic) if explicit is not None and automatic is not None
                    else explicit if explicit is not None else automatic)
        target = (int(pressure * self.context.policy.compact_to_ratio) if explicit is not None
                  else max(output + 1, pressure - 2 * headroom) if pressure is not None else None)
        return pressure, target

    def _calibrate_prepared(self, prepared):
        calibration = self._refresh_context_calibration()
        if calibration is None:
            return prepared
        return replace(prepared, token_estimate=calibration.estimate(
            prepared.body, prepared.token_estimate, strict=self.strict_model_profile))

    def _estimate_context_tokens(self, messages, sources):
        if self.versions.remote_model.route_id == "opencode-go":
            from .opencode_go import estimate_messages, responses_body
            estimate = estimate_messages(model=self.model, messages=messages,
                tools=self.specs, parameters=self.parameters)
            calibration = self._refresh_context_calibration()
            if calibration is not None:
                body = responses_body(model=self.model, messages=messages,
                    tools=self.specs, parameters=self.parameters)
                estimate = calibration.estimate(body, estimate, strict=self.strict_model_profile)
            return estimate.reservation_tokens
        if self.versions.remote_model.route_id == "chatgpt-subscription":
            from .responses import build_responses_body, estimate_responses_request
            body = build_responses_body(model=self.model, messages=messages,
                tools=self.specs, parameters=self.parameters)
            estimate = estimate_responses_request(body, output_token_limit=self.parameters.get(
                "max_tokens", self.parameters.get("max_completion_tokens")),
                strict=self.strict_model_profile)
            calibration = self._refresh_context_calibration()
            return calibration.estimate(body, estimate, strict=self.strict_model_profile).reservation_tokens
        return estimate_chat_request({"model": self.model,
                "messages": (messages if self.versions.remote_model.route_id == "glm-subscription-anthropic"
                    else reasoning_history_messages(messages, self.reasoning_history, sources)),
                "tools": self.specs, **self.parameters},
                strict=self.strict_model_profile).reservation_tokens

    def _compact_before_retry(self, messages, sources):
        if self.context is None:
            return None
        before = self._estimate_context_tokens(messages, sources)
        projection = self._project(force_compact=True, target_tokens=max(1, int(before * 0.6)))
        after = self._estimate_context_tokens(projection[0], projection[1])
        self._checkpoint()
        return projection if after < before else None

    def _context_recovery_stop(self, *, reason, prepared=None):
        evidence = {"reason": reason, "recoverable": True,
            "next_action": ("A provider retry hint blocks automatic resend. Inspect the saved failure and preserve completed tools before explicitly planning any new request."
                if reason == "provider_retry_hint_veto" else
                "Resume from the saved task state with a smaller required working set or corrected window evidence; do not replay completed tools."),
            "checkpoint": str(self.store.task_directory / "checkpoint.json"),
            "configured_context_limit": self.limits.context_tokens,
            "estimate": asdict(prepared.token_estimate) if prepared is not None else None}
        self.store.append(RunLifecyclePayload(action="failure", failure_stage="context_management",
            reason="context_recovery_required"), source_refs=(self.store.source("context-recovery", evidence),))
        self._checkpoint()
        return "context_recovery_required"

    async def _model_call(self, messages, sources, *, purpose, tools, context_event_id=None):
        parameters = dict(self.parameters)
        degradation = None
        logical_purpose = "context_summary" if purpose == "context_summary" else "primary_task"
        local_context_recoveries = 0
        while True:
            if self._retry_uses_reconnect() and not await self._wait_for_recovery_capacity(0.0):
                return None, self._recovery_stop_reason()
            async with self._dispatch_slot() as lease:
                if (self._retry_uses_reconnect() and self._recovery_summary()["remaining_seconds"] <= 0):
                    continue
                self.stage = "prepare_request"
                with self._measure("request_prepare"):
                    prepared = prepare_request(store=self.store, model=self.model,
                        messages=messages, message_sources=sources, tools=tools,
                        tool_source=self.tool_source, parameters=parameters, versions=self.versions,
                        image_originals=self.originals,
                        reasoning_history=self.reasoning_history,
                        strict_model_profile=self.strict_model_profile)
                    prepared = self._calibrate_prepared(prepared)
                profile_limit = prepared.context_window_tokens
                configured_limit = self.limits.context_tokens
                available_limits = tuple(limit for limit in (profile_limit, configured_limit)
                                         if limit is not None)
                effective_context_limit = min(available_limits) if available_limits else None
                if (effective_context_limit is not None
                        and prepared.token_reservation_estimate > effective_context_limit):
                    projection = (self._compact_before_retry(messages, sources)
                        if local_context_recoveries < 2 and logical_purpose == "primary_task" else None)
                    if projection is not None:
                        local_context_recoveries += 1
                        messages, sources, context_event_id = projection
                        continue
                    return None, self._context_recovery_stop(reason="required_context_cannot_fit_after_compaction",
                                                             prepared=prepared)
                budget_revision = self._budget_capacity.revision
                self._load_budget()
                remaining = self._remaining()
                if remaining <= 0:
                    return None, self._scoped_budget_reason("time", task=True)
                # Wall-clock deadline covers tools, summaries, retries, and restart downtime.
                finite_seconds = [value for value in (
                    self._wall_remaining_amount(),
                    self.budget.available.seconds,
                    self.task_budget.available.seconds,
                ) if value is not None]
                seconds = min(finite_seconds) if finite_seconds else None
                if seconds is not None and self.request_timeout_seconds is not None:
                    seconds = min(seconds, Decimal(str(self.request_timeout_seconds)))
                if seconds is not None and seconds <= 0:
                    reason = self._scoped_budget_reason(
                        "time", task=(self.task_budget.available.seconds is not None
                                      and self.task_budget.available.seconds <= 0)
                    )
                    reason = await self._wait_for_transient_root_time_reservation(
                        reason, revision=budget_revision, lease=lease
                    )
                    if reason is None:
                        continue
                    return None, reason
                budget_purpose = (
                    "child_task"
                    if purpose == "primary_task" and not self.store.is_root_task
                    else purpose
                )
                # The calibrated total includes images, but a usage receipt
                # need not provide their split. Preserve the original proxy in
                # token_estimate; its budget subset cannot exceed the total.
                image_budget_subset = min(prepared.token_estimate.image_tokens,
                                          prepared.input_token_upper_bound)
                estimate = RequestEstimate.for_model_call(purpose=budget_purpose, task_id=self.store.task_id,
                    input_token_upper_bound=prepared.input_token_upper_bound,
                    image_input_tokens_estimate=image_budget_subset,
                    additional_image_tokens_estimate=(prepared.token_estimate.image_tokens
                        if bills_images_separately(get_cny_price_schedule(self.model,
                            route_id=self.versions.remote_model.route_id)) else 0),
                    output_token_limit=prepared.output_token_limit,
                    seconds=None if self.limits.time_budget_mode == "wall" else seconds,
                    reasoning_token_allowance=prepared.token_estimate.reasoning_token_allowance,
                    estimate_source=prepared.estimate_source,
                    pricing=self.pricing, cny_pricing=self.cny_pricing)
                reservation_id = self.store.next_reservation_id()
                task_decision = self.task_budget.reserve(reservation_id, estimate)
                if task_decision.action == "stop":
                    return None, self._budget_reason(
                        task_decision,
                        scope=None if self.store.is_root_task else "child",
                    )
                effective = task_decision.effective_estimate
                decision = self.budget.reserve(reservation_id, effective)
                if decision.action == "stop":
                    reason = self._budget_reason(
                        decision,
                        scope="root" if not self.store.is_root_task else None,
                    )
                    reason = await self._wait_for_transient_root_time_reservation(
                        reason, revision=budget_revision, lease=lease
                    )
                    if reason is None:
                        continue
                    return None, reason
                if task_decision.action == "reduce_output" or decision.action == "reduce_output":
                    reduction = (
                        decision
                        if decision.action == "reduce_output"
                        else task_decision
                    )
                    degradation = reduction.model_dump(mode="json")
                    key = "max_tokens" if "max_tokens" in parameters else "max_completion_tokens"
                    parameters[key] = reduction.output_token_limit
                    # No event or request has been emitted yet. Re-estimate exact wire bytes.
                    self._load_budget()
                    continue
                reservation = decision.reservation
                self.store.append(BudgetEventPayload(action="reserve", reservation=reservation),
                    source_refs=(self.store.source("request-budget-decision", {
                        **decision.model_dump(mode="json"), "original_output_limit": self.parameters.get("max_tokens", self.parameters.get("max_completion_tokens")),
                        "actual_output_limit": prepared.output_token_limit,
                        **({"output_limit_enforcement": "local_reservation_only",
                            "service_output_cap": None,
                            "note": "ChatGPT subscription rejects output cap parameters; actual usage can exceed the reservation."}
                           if self.versions.remote_model.route_id == "chatgpt-subscription" else {}),
                        "near_limit_action": "reduce_output" if degradation else "allow",
                        "degradation": degradation,
                        "token_estimate": asdict(prepared.token_estimate),
                        "image_budget_estimate": {
                            "raw_image_proxy_tokens": prepared.token_estimate.image_tokens,
                            "included_image_subset_tokens": image_budget_subset,
                            "basis": "Image proxy capped by the whole-input upper bound; an estimated subset, not provider-reported image usage."},
                        "cny_reservation": ({
                            "price_schedule": {k: str(v) if isinstance(v, Decimal) else v
                                               for k, v in asdict(self.cny_pricing).items()},
                            "assumed_cache_read_tokens": 0,
                            "note": "No future cache hit assumed; full prompt plus separate images and output/reasoning allowance. Estimate, not a bill."
                        } if self.cny_pricing else None),
                        "context_limits": {"model_profile": profile_limit,
                            "configured": configured_limit,
                            "effective": effective_context_limit}}),))
                self._fault("after_reservation")
                if self.retry_of:
                    self.store.append(RunLifecyclePayload(action="retry", reason="explicit bounded model retry",
                        retry_of_event_id=self.retry_of, attempt=self._request_retry_count(self.retry_of) + 2))
                    self._fault("after_retry")
                self.stage = "model_request"
                request = self.store.append(prepared.event_payload.model_copy(update={
                    "reservation_id": reservation.reservation_id, "logical_purpose": logical_purpose}),
                    source_refs=(self.store.source("request-dispatch", lease.evidence if lease else {
                        "route_id": self.versions.remote_model.route_id,
                        "queue_seconds": 0.0, "mode": "unconfigured"}),))
                if lease:
                    lease.recorded = True
                self._queue_interruption = None
                self._refresh_counts()
                self._fault("after_request")
                sent_at = time.monotonic()
                deadlines = [value for value in (
                    self._wall_timeout(), float(seconds) if seconds is not None else None,
                    self.request_timeout_seconds) if value is not None]
                request_timeout = min(deadlines) if deadlines else None
                # Without a run deadline, protect individual network I/O waits,
                # not the total duration of an actively streaming response.
                io_timeout = (request_timeout if self.limits.seconds is not None
                              else min(NO_DEADLINE_IO_TIMEOUT_SECONDS, request_timeout)
                              if request_timeout is not None else NO_DEADLINE_IO_TIMEOUT_SECONDS)
                self._budget_capacity.start(reservation.reservation_id)
                recovery = None
                try:
                    recovery = self._reserve_request_recovery(request)
                    raw = await asyncio.wait_for(
                        self._send_with_recovery(prepared, recovery, timeout=io_timeout),
                        timeout=request_timeout,
                    )
                    adapter_elapsed = time.monotonic() - sent_at
                    self._phase_seconds["adapter_send"] = self._phase_seconds.get("adapter_send", 0.0) + adapter_elapsed
                except (Exception, asyncio.CancelledError) as exc:
                    failure = classify_failure(exc, request.event_id)
                    elapsed = time.monotonic() - sent_at
                    self._phase_seconds["adapter_send"] = self._phase_seconds.get("adapter_send", 0.0) + elapsed
                    redispatch = (lease is not None and failure.category == "temporary_rate_limit"
                        and failure.retryable and failure.retry_hint is None)
                    adjustment = lease.rejected() if redispatch else None
                    if lease:
                        lease.release()
                    self._record_model_failure(failure, elapsed=elapsed, dispatch_adjustment=adjustment)
                    usage = exc.usage if isinstance(exc, ModelServiceError) else UsageMissing(reason="request ended without a service usage receipt")
                    try:
                        settlement_stop = self._settle(reservation.reservation_id, usage,
                                     seconds=elapsed)
                    finally:
                        self._budget_capacity.complete(reservation.reservation_id)
                    await self._yield_budget_waiters()
                    self._refresh_counts()
                    if settlement_stop:
                        return None, settlement_stop
                    if failure.category == "context_length_exceeded":
                        # Context repair is a separate, normally non-transient
                        # retry path. Provider retry vetoes still take precedence.
                        if failure.retry_hint is not None:
                            return None, self._context_recovery_stop(
                                reason="provider_retry_hint_veto", prepared=prepared)
                        rejected = sum(e.payload.event_type == "run_lifecycle"
                            and e.payload.model_failure is not None
                            and e.payload.model_failure.category == "context_length_exceeded"
                            for e in self.store.events)
                        projection = (self._compact_before_retry(messages, sources)
                            if rejected == 1 and logical_purpose == "primary_task"
                            and self.answer_repair_request_id is None else None)
                        if projection is None:
                            return None, self._context_recovery_stop(reason="provider_context_rejected_without_further_safe_reduction",
                                                                     prepared=prepared)
                        messages, sources, context_event_id = projection
                        self.retry_of, purpose = request.event_id, "retry"
                        self._checkpoint()
                        continue
                    if redispatch and await self._retry_budget_available():
                        self.redispatch_of, self.retry_of = request.event_id, None
                        purpose = logical_purpose
                        continue
                    self.redispatch_of = None
                    if await self._allow_model_retry(failure):
                        self.retry_of, purpose = request.event_id, "retry"
                        continue
                    return None, self._failure_stop_reason(failure)
                except BaseException:
                    # Process-style fault injection and interpreter shutdown do
                    # not leave peers waiting on an operation no longer running.
                    self._budget_capacity.complete(reservation.reservation_id)
                    if recovery is not None:
                        self._recovery_capacity.complete(recovery.operation_id)
                    raise
                if lease:
                    lease.release()
                try:
                    self.stage = "model_response"
                    parse_started = perf_counter()
                    with self._measure("response_parse_capture"):
                        parsed = parse_response(raw, request.event_id, self.store, echo_fields=self._effective_echo_fields())
                    parse_elapsed = perf_counter() - parse_started
                    elapsed = time.monotonic() - sent_at
                    adjustment = lease.succeeded() if lease else None
                    self.redispatch_of = None
                    response = self.store.append(parsed.event_payload,
                        source_refs=(self.store.source("request-duration", {"elapsed_seconds": elapsed,
                            "basis": "local monotonic wall time from send through parsed response",
                            "phases": {"adapter_send_seconds": adapter_elapsed,
                                "response_parse_capture_seconds": parse_elapsed,
                                **getattr(self.adapter, "last_send_timing", {})}}),
                            *((self.store.source("dispatch-adjustment", adjustment),) if adjustment else ())))
                    reason = self._settle(reservation.reservation_id, parsed.event_payload.usage,
                                          seconds=elapsed)
                finally:
                    self._budget_capacity.complete(reservation.reservation_id)
                await self._yield_budget_waiters()
                self._refresh_counts()
                self._present_tools(prepared.body, sources, request, response, context_event_id)
                truncation = None
                if parsed.finish_reason == "length":
                    blocked = reason
                    if reported_tokens(parsed.event_payload.usage) is None:
                        blocked = blocked or "token_usage_unavailable"
                    if self._remaining() <= 0:
                        blocked = blocked or self._scoped_budget_reason("time", task=True)
                    truncation = self._record_truncation(response, blocked=blocked)
                self._fault("after_response")
                if reason:
                    return None, reason
                if truncation is not None:
                    if truncation.payload.action == "stop":
                        return None, truncation.payload.reason
                    message = self._truncation_prompt(purpose=logical_purpose)
                    source = self._event_source(truncation)
                    self.retry_of = None
                    if logical_purpose == "primary_task":
                        self._append_truncation_prompt(truncation)
                        messages, sources, context_event_id = self._project()
                    else:
                        messages, sources = [*messages, message], [*sources, source]
                    self._checkpoint()
                    # This is another paid request under the same task's remaining
                    # budget and deadline, not a free protocol or transport retry.
                    purpose = logical_purpose
                    continue
                if parsed.protocol_error:
                    if parsed.protocol_error == "empty_response" and reported_tokens(parsed.event_payload.usage) == 0:
                        failure = ModelFailureDetails(request_event_id=request.event_id,
                            category="empty_response", retryable=True, usage_received=True,
                            service_error_type="empty_response", request_id=str(raw.get("id")) if raw.get("id") else None)
                        self._record_model_failure(failure)
                        if await self._allow_model_retry(failure):
                            self.retry_of, purpose = request.event_id, "retry"
                            continue
                        return None, self._failure_stop_reason(failure)
                    return None, parsed.protocol_error
                if reported_tokens(parsed.event_payload.usage) is None:
                    return None, "token_usage_unavailable"
                if self._remaining() <= 0:
                    return None, "time_budget_exhausted"
                return response, None

    def _truncation_prompt(self, *, purpose="primary_task"):
        continuation = (
            "Continue from the last confirmed state by issuing the next complete tool call. "
            "The task still requires verified domain completion; a status report does not finish it."
            if self.domain_completion and purpose == "primary_task" else
            "Continue the requested task from the last confirmed state in smaller complete steps, "
            "using tools when needed. Keep the next response within the output limit.")
        return {"role": "user", "content": (
            "The previous response exceeded the output limit and was discarded. "
            "Its thinking was not retained and none of its tool calls ran. "
            + continuation
        )}

    def _append_truncation_prompt(self, event):
        if not any(source.event_id == event.event_id for source in self.sources):
            self._append_message(self._truncation_prompt(), self._event_source(event))

    def _record_truncation(self, response, *, blocked=None):
        existing = next((e for e in reversed(self.store.events)
            if e.payload.event_type == "response_truncation"
            and e.payload.response_event_id == response.event_id), None)
        if existing is not None:
            request = next(e for e in self.store.events if e.event_id == response.payload.request_event_id)
            receipt = next((r for r in self.task_budget.ledger.reconciliations
                if r.reservation_id == request.payload.reservation_id), None)
            if (receipt is not None and blocked is None and existing.payload.action == "stop"
                    and existing.payload.reason in {"money_cny_usage_unavailable", "token_usage_unavailable"}
                    and existing.payload.consecutive_count <= self.limits.max_consecutive_truncations
                    and existing.payload.total_count <= self.limits.max_total_truncations):
                # This is an effective projection of the old stop, backed by
                # the appended reconciliation. Keep its single journal record.
                return existing.model_copy(update={"payload": existing.payload.model_copy(update={
                    "action": "continue",
                    "reason": "late usage reconciled; discard truncated output and request a concise continuation"})})
            return existing
        total, consecutive = 0, 0
        for event in self.store.all_events:
            if event.sequence > response.sequence:
                break
            if event.payload.event_type != "model_response":
                continue
            raw = self.store.resolve(event.payload.raw_response)
            choices = raw.get("choices", []) if isinstance(raw, dict) else []
            truncated = (len(choices) == 1 and isinstance(choices[0], dict)
                and choices[0].get("finish_reason") == "length")
            if isinstance(raw, dict) and raw.get("type") == "message":
                truncated = raw.get("stop_reason") == "max_tokens"
            if isinstance(raw, dict) and raw.get("object") == "response":
                truncated = (raw.get("status") == "incomplete" and
                    (raw.get("incomplete_details") or {}).get("reason") == "max_output_tokens")
            total += int(truncated)
            if event.task_id == self.store.task_id:
                consecutive = consecutive + 1 if truncated else 0
        raw = self.store.resolve(response.payload.raw_response)
        if raw.get("object") == "response":
            blocks = raw.get("output", [])
            thinking = ""  # Opaque encrypted reasoning is never public thought.
            visible = "".join(part.get("text", "") for block in blocks
                if block.get("type") == "message" for part in block.get("content", [])
                if part.get("type") == "output_text")
            calls = [block for block in blocks if block.get("type") == "function_call"]
        elif raw.get("type") == "message":
            blocks = raw.get("content", [])
            thinking = "".join(b.get("thinking", "") for b in blocks if b.get("type") == "thinking" and isinstance(b.get("thinking", ""), str))
            visible = "".join(b.get("text", "") for b in blocks if b.get("type") == "text" and isinstance(b.get("text", ""), str))
            calls = [b for b in blocks if b.get("type") == "tool_use"]
        else:
            message = raw["choices"][0].get("message")
            message = message if isinstance(message, dict) else {}
            thinking = message.get("reasoning_content", message.get("reasoning", ""))
            visible, calls = message.get("content"), message.get("tool_calls")
        usage = response.payload.usage
        details = usage.raw_usage if usage.kind == "reported" else {}
        completion_details = details.get("completion_tokens_details") or details.get("output_tokens_details") or {}
        count = completion_details.get("reasoning_tokens", details.get("reasoning_tokens", details.get("thinking_tokens")))
        exceeded = (consecutive > self.limits.max_consecutive_truncations
            or total > self.limits.max_total_truncations)
        reason = "incomplete_response" if exceeded else blocked
        return self.store.append(TruncationPayload(
            request_event_id=response.payload.request_event_id,
            response_event_id=response.event_id,
            thinking_characters=len(thinking) if isinstance(thinking, str) else 0,
            visible_characters=len(visible) if isinstance(visible, str) else 0,
            reported_reasoning_tokens=count if type(count) is int and count >= 0 else None,
            has_tool_calls=bool(calls), tool_call_count=len(calls) if isinstance(calls, list) else None,
            consecutive_count=consecutive, total_count=total,
            max_consecutive_recoveries=self.limits.max_consecutive_truncations,
            max_total_recoveries=self.limits.max_total_truncations,
            action="stop" if reason else "continue",
            reason=reason or "discard truncated output and request a concise continuation"))

    def _settle(self, reservation_id, usage, *, seconds):
        # Other children may reserve or settle root budget while this request is
        # in flight. Rebuild both ledgers from the shared durable journal.
        self._load_budget()
        usage = self._effective_budget_usage(reservation_id, usage)
        if any(s.reservation_id == reservation_id for s in self.budget.ledger.settlements):
            self._record_token_overrun(reservation_id, usage)
            return self._recorded_settlement_stop(reservation_id) or self._settled_token_stop()
        charged_seconds = Decimal(str(max(0.0, seconds))) if seconds is not None else None
        accounting = self._request_accounting(reservation_id, usage)
        image_charge = {"image_tokens_estimate": accounting.image_tokens_estimate,
            "additional_image_tokens": accounting.additional_image_tokens,
            "reported_usage_includes_image_tokens": accounting.reported_usage_includes_image_tokens}
        reservation = next(r for r in self.budget.ledger.reservations if r.reservation_id == reservation_id)
        if accounting.usage_basis == "rejected_before_processing" and reservation.amounts.money_usd is not None:
            image_charge["cost"] = EstimatedCostUpperBound(usd=Decimal(0),
                reason="Known unprocessed rejection; no charge inferred from the request estimate")
        if reservation.amounts.money_cny is not None:
            image_charge["estimated_cost_cny"] = (accounting.estimated_cost_cny
                if reported_tokens(usage) is not None else None)
        sources = (self.store.source("request-usage-accounting", accounting.receipt_dict()),)
        actual = BudgetAmounts(tokens=reported_tokens(usage), calls=1, seconds=charged_seconds)
        task_decision = self.task_budget.settle(reservation_id,
            actual=actual, usage=usage, **image_charge)
        # Even when the task cannot afford the observed charge, record the full
        # amount in the root ledger. A child's limit is not a root-wide failure.
        if task_decision.settlement is not None:
            root_decision = self.budget.settle(reservation_id, actual=actual, usage=usage, **image_charge)
            if root_decision.settlement is not None:
                self.store.append(BudgetEventPayload(action="settle", settlement=root_decision.settlement), source_refs=sources)
                self._record_token_overrun(reservation_id, usage)
                return self._settled_token_stop()
            decision = root_decision
        elif task_decision.action == "stop":
            decision = task_decision
        else:
            decision = self.budget.settle(reservation_id,
                actual=actual, usage=usage, **image_charge)
        if decision.action == "stop":
            if "seconds" in decision.exceeded_dimensions:
                task_seconds, root_seconds = self.task_budget.available.seconds, self.budget.available.seconds
                task_limited = task_seconds is not None and (root_seconds is None or task_seconds <= root_seconds)
                stop_reason = self._scoped_budget_reason("time", task=task_limited)
            else:
                stop_reason = "token_reservation_exceeded" if "tokens" in decision.exceeded_dimensions else decision.reason
            self.store.append(RunLifecyclePayload(action="failure", failure_stage="budget_settlement",
                reason=decision.reason), source_refs=(self.store.source("budget-violation", {
                    "decision": decision.model_dump(mode="json"), "observed": actual.model_dump(mode="json"),
                    "usage": usage.model_dump(mode="json"), "stop_reason": stop_reason}),))
            if (decision.exceeded_dimensions == ("seconds",)
                    and actual.tokens is not None
                    and (accounting.budget_charge_tokens > (decision.reservation.amounts.tokens or 0)
                         or image_charge.get("estimated_cost_cny") is not None)):
                # A late response still incurred its full token charge. The
                # observed duration remains above in the violation evidence;
                # leave time unsettled (retain its hold), and keep the time stop.
                token_charge = actual.model_copy(update={"seconds": None})
                task_charge = self.task_budget.settle(reservation_id, actual=token_charge, usage=usage, **image_charge)
                root_charge = self.budget.settle(reservation_id, actual=token_charge, usage=usage, **image_charge)
                if task_charge.settlement is not None and root_charge.settlement is not None:
                    self.store.append(BudgetEventPayload(action="settle", settlement=root_charge.settlement), source_refs=sources)
                    self._record_token_overrun(reservation_id, usage)
            return stop_reason
        self.store.append(BudgetEventPayload(action="settle", settlement=decision.settlement), source_refs=sources)
        return None

    def _request_accounting(self, reservation_id, usage):
        request = next((e for e in self.store.events if e.payload.event_type == "adapter_request"
            and e.payload.reservation_id == reservation_id), None)
        if request is None:
            # Legacy offline settlement fixtures can predate request captures.
            return account_request_usage(usage, image_tokens_estimate=0, pricing=None)
        return request_accounting_from_store(self.store, request.event_id, usage=usage).accounting

    def _effective_budget_usage(self, reservation_id, fallback):
        """Project a late receipt without rewriting the original response."""
        settlement = next((s for s in self.task_budget.ledger.effective_settlements
            if s.reservation_id == reservation_id), None)
        return settlement.usage if settlement is not None else fallback

    def _recorded_settlement_stop(self, reservation_id):
        for event in reversed(self.store.events):
            if (event.payload.event_type != "run_lifecycle"
                    or event.payload.failure_stage != "budget_settlement"):
                continue
            for source in event.source_refs:
                if source.source_id == "budget-violation" and source.blob:
                    saved = json.loads(self.store.get_bytes(source.blob))
                    reservation = saved.get("decision", {}).get("reservation") or {}
                    if reservation.get("reservation_id") == reservation_id:
                        if (any(r.reservation_id == reservation_id for r in self.task_budget.ledger.reconciliations)
                                and saved.get("stop_reason") in {"money_cny_usage_unavailable",
                                    "token_usage_unavailable", "unsettled_reported_usage"}):
                            continue
                        return saved.get("stop_reason")
        return None

    def _settled_token_stop(self):
        for budget, task in ((self.budget, False), (self.task_budget, True)):
            if budget.fatal_reason is not None:
                if budget.fatal_reason in {"token_budget_exhausted", "money_budget_exhausted"}:
                    dimension = "token" if budget.fatal_reason == "token_budget_exhausted" else "money"
                    return self._scoped_budget_reason(dimension, task=task)
                return budget.fatal_reason
            if (budget.total_limit.money_cny is not None
                    and (budget.ledger.committed.money_cny or 0) >= budget.total_limit.money_cny):
                return self._scoped_budget_reason("money", task=task)
        root_tokens = self.budget.ledger.committed.tokens or 0
        task_tokens = self.task_budget.ledger.committed.tokens or 0
        if self.budget.total_limit.tokens is not None and root_tokens > self.budget.total_limit.tokens:
            return self._scoped_budget_reason("token")
        if self.task_budget.total_limit.tokens is not None and task_tokens > self.task_budget.total_limit.tokens:
            return self._scoped_budget_reason("token", task=True)
        return None

    def _record_token_overrun(self, reservation_id, usage):
        # The reconciliation itself carries the complete overrun. The older
        # budget_overrun event protocol references only the original settlement.
        if any(r.reservation_id == reservation_id for r in self.budget.ledger.reconciliations):
            return
        reservation = next(r for r in self.budget.ledger.reservations if r.reservation_id == reservation_id)
        accounting = self._request_accounting(reservation_id, usage)
        actual_tokens = accounting.budget_charge_tokens
        reserved_tokens = reservation.amounts.tokens
        if actual_tokens is None or reserved_tokens is None or actual_tokens <= reserved_tokens:
            return
        if any(e.payload.event_type == "budget_overrun" and e.payload.reservation_id == reservation_id
               for e in self.store.events):
            return
        # Read the original request's profile margin, not a later configuration.
        evidence = next((e for e in self.store.events if e.payload.event_type == "budget"
                         and e.payload.reservation is not None
                         and e.payload.reservation.reservation_id == reservation_id), None)
        estimate = {}
        if evidence is not None:
            ref = next((s.blob for s in evidence.source_refs if s.source_id == "request-budget-decision"), None)
            if ref:
                estimate = json.loads(self.store.get_bytes(ref)).get("token_estimate", {})
        allowance = estimate.get("reasoning_token_allowance", 0)
        request = next((e for e in self.store.events if e.payload.event_type == "adapter_request"
                        and e.payload.reservation_id == reservation_id), None)
        body = self.store.resolve(request.payload.final_request_body) if request else {}
        limit = body.get("max_tokens", body.get("max_completion_tokens"))
        completion = usage.raw_usage.get("completion_tokens", usage.raw_usage.get("output_tokens"))
        completion_over = max(0, completion - limit) if type(completion) is int and type(limit) is int else None
        self.store.append(BudgetOverrunPayload(model=body.get("model", self.model),
            reservation_id=reservation_id, reserved_tokens=reserved_tokens, actual_tokens=actual_tokens,
            overrun_tokens=actual_tokens - reserved_tokens, reasoning_token_allowance=allowance,
            completion_over_max_tokens=completion_over,
            within_profile_allowance=(completion_over <= allowance if completion_over is not None else None),
            root_limit_exceeded=(self.budget.total_limit.tokens is not None and
                (self.budget.ledger.committed.tokens or 0) > self.budget.total_limit.tokens),
            task_limit_exceeded=(self.task_budget.total_limit.tokens is not None and
                (self.task_budget.ledger.committed.tokens or 0) > self.task_budget.total_limit.tokens)),
            source_refs=(self.store.source("request-usage-accounting", accounting.receipt_dict()),))

    def _present_tools(self, body, sources, request, response, context_event_id):
        if request.payload.adapter == "openai-responses-http-v1":
            from .responses import present_tools
            return present_tools(self.store, body, request, response, context_event_id)
        if request.payload.adapter == "anthropic-messages-http-v1":
            from .anthropic import present_tools
            return present_tools(self.store, body, request, response, context_event_id)
        delivered = {e.payload.tool_execution_event_id for e in self.store.events if e.payload.event_type == "tool_presentation"}
        events = {e.event_id: e for e in self.store.events}
        for message, source in zip(body["messages"], sources, strict=True):
            event = events.get(source.event_id)
            if (message.get("role") != "tool" or event is None or event.payload.event_type != "tool_execution"
                    or event.event_id in delivered or event.payload.shown_result.kind == "missing"):
                continue
            original = self.store.resolve(event.payload.shown_result)
            original_blocks = original.get("image_blocks", [])
            shown_blocks = []
            all_blocks = [b for m in body["messages"] if isinstance(m.get("content"), list) for b in m["content"]]
            for block in original_blocks:
                if block in all_blocks:
                    shown_blocks.append(block)
            actual = {**original, "tool_message": message, "image_blocks": shown_blocks}
            capture = event.payload.shown_result if actual == original else self.store.capture(actual, force_blob=True)
            self.store.append(ToolPresentationPayload(tool_execution_event_id=event.event_id,
                request_event_id=request.event_id, response_event_id=response.event_id,
                shown_result=capture, context_event_id=context_event_id))
            delivered.add(event.event_id)

    def _accept_response(self, event):
        if any(source.event_id == event.event_id for source in self.sources):
            return
        if self.context:
            request = next(e for e in self.store.events if e.event_id == event.payload.request_event_id)
            self.context.commit_request_state(request)
        parsed = parse_response(self.store.resolve(event.payload.raw_response), event.payload.request_event_id,
                                self.store, echo_fields=self._effective_echo_fields())
        if parsed.protocol_error:
            self.terminal_reason = parsed.protocol_error
            return
        calls = parsed.event_payload.tool_calls
        if self.answer_repair_request_id is not None and calls:
            self._append_message(parsed.assistant_message, self._event_source(event))
            answer = "\n".join(parsed.event_payload.visible_text)
            self._record_answer_repair_result(event, answer, accepted=False,
                error="the single answer repair response must not invoke tools")
            self.answer = None
            self.terminal_reason = "answer_validation_failed"
            return
        invoked = {e.payload.call_id for e in self.store.events if e.payload.event_type == "tool_invocation"}
        additional = sum(c.call_id not in invoked for c in calls)
        if self.counts["tool_calls"] + additional > self.limits.tool_calls:
            self.terminal_reason = "tool_budget_exhausted"
            return
        if self.root_tool_calls is not None:
            root_used = sum(e.payload.event_type == "tool_invocation" for e in self.store.all_events)
            if root_used + additional > self.root_tool_calls:
                self.terminal_reason = self._scoped_budget_reason("tool")
                return
        try:
            for call in calls:
                if call.call_id in self.used_ids or call.tool_name not in self.spec_by_name:
                    raise ValueError("unknown tool or reused call identity")
                repeatability = self._tool_repeatability(call.tool_name)
                authorize_tool_call(self.role, call.tool_name, "read" if repeatability == "read_only" else "write")
        except ValueError:
            self.terminal_reason = "tool_authorization_failed"
            return
        if self.context:
            self.context.acknowledge_projection()
        self._append_message(parsed.assistant_message, self._event_source(event))
        if calls:
            self.incomplete_response_count = 0
            self.pending_response_id = event.event_id
        else:
            if self.domain_completion:
                # The model's natural-language stopping point is not the domain
                # completion authority. Preserve it as interim history and make
                # a bounded, ordinary budgeted attempt to continue the work.
                self.answer = None
                self.incomplete_response_count += 1
                if self.incomplete_response_count > MAX_DOMAIN_COMPLETION_CONTINUATIONS:
                    self.terminal_reason = "domain_completion_unmet"
                    return
                self._append_message(self._domain_continuation_prompt(), self._event_source(event))
                return
            answer = "\n".join(parsed.event_payload.visible_text)
            if self.answer_validator is None:
                self.answer = answer
                self.terminal_reason = "completed"
                return
            try:
                self.answer_validator(answer)
            except ValueError as exc:
                error = str(exc).strip() or type(exc).__name__
                self._reject_or_request_answer_repair(event, answer, error)
                return
            if self.answer_repair_request_id is not None:
                self._record_answer_repair_result(event, answer, accepted=True)
            self.answer = answer
            self.terminal_reason = "completed"

    @staticmethod
    def _domain_continuation_prompt():
        return {"role": "user", "content": (
            "The task's required completion has not been verified. Your previous text is an interim "
            "status report, not a completed task. Continue with the available tools toward the "
            "completion requirement in the task guidance, using the existing remaining budget. "
            "Repeated text-only replies without tool progress will stop this run as incomplete."
        )}

    def _reject_or_request_answer_repair(self, event, answer, error):
        if self.answer_repair_request_id is not None:
            self._record_answer_repair_result(event, answer, accepted=False, error=error)
            self.answer = None
            self.terminal_reason = "answer_validation_failed"
            return
        if self.max_answer_repairs < 1:
            self.answer = None
            self.terminal_reason = "answer_validation_failed"
            return
        existing = next((candidate for candidate in self.store.events
            if candidate.payload.event_type == "answer_repair"
            and candidate.payload.phase == "request"
            and candidate.payload.invalid_response_event_id == event.event_id), None)
        repair = existing or self.store.append(AnswerRepairPayload(
            phase="request", attempt=1, invalid_response_event_id=event.event_id,
            original_answer=self.store.capture(answer), validation_error=error))
        self.answer_repair_request_id = repair.event_id
        self.answer_repair_response_id = event.event_id
        self.answer_repair_original = answer
        self.answer_repair_error = error
        message = {"role": "user", "content": (
            "The previous final answer failed the unchanged response validator. "
            "Correct only its JSON syntax, schema, or reference errors. Return the "
            "complete replacement JSON object without markdown. This is the single "
            f"allowed repair. Validation error: {error}"
        )}
        self._append_message(message, self._event_source(repair))

    def _record_answer_repair_result(self, event, answer, *, accepted, error=None):
        existing = next((candidate for candidate in self.store.events
            if candidate.payload.event_type == "answer_repair"
            and candidate.payload.phase == "result"
            and candidate.payload.repair_request_event_id == self.answer_repair_request_id), None)
        if existing is None:
            self.store.append(AnswerRepairPayload(
                phase="result", attempt=1,
                invalid_response_event_id=self.answer_repair_response_id,
                original_answer=self.store.capture(self.answer_repair_original),
                validation_error=self.answer_repair_error,
                repair_request_event_id=self.answer_repair_request_id,
                repaired_response_event_id=event.event_id,
                repaired_answer=self.store.capture(answer), accepted=accepted,
                repaired_validation_error=error))

    async def _execute_pending(self):
        event = next(e for e in self.store.events if e.event_id == self.pending_response_id)
        for call in event.payload.tool_calls:
            if call.call_id in self.used_ids:
                continue
            if self._remaining() <= 0:
                return "time_budget_exhausted"
            if self.counts["tool_calls"] >= self.limits.tool_calls:
                return "tool_budget_exhausted"
            # A pending response may survive a process interruption while
            # another task consumes the root allowance before it resumes.
            if self.root_tool_calls is not None and sum(
                    e.payload.event_type == "tool_invocation" for e in self.store.all_events
            ) >= self.root_tool_calls:
                return self._scoped_budget_reason("tool")
            self.stage = f"tool:{call.tool_name}"
            self._fault("before_tool")
            repeatability = self._tool_repeatability(call.tool_name)
            unknown = self._unresolved_tool_calls().get(call.call_id)
            retry_event = None
            if unknown is not None:
                if self._tool_recovery_policy(call.tool_name) != "retry_read":
                    return "resume_pending_operation"
                attempts = sum(e.payload.event_type == "tool_invocation" and
                    e.payload.call_id == call.call_id for e in self.store.events)
                if attempts > self.limits.max_tool_recovery_retries:
                    return "tool_recovery_retries_exhausted"
                retry_event = self.store.append(RunLifecyclePayload(action="retry",
                    reason="explicit repeatable-read recovery; original outcome remains unknown",
                    retry_of_event_id=unknown.event_id, attempt=attempts + 1))
            write = repeatability != "read_only"
            # Provider call IDs are scoped to a conversation. Concurrent readers
            # may return the same ID; their writes must not collide in the root journal.
            key = json.dumps([self.store.run_id, self.store.task_id, call.call_id],
                             separators=(",", ":")) if write else None
            self._tool_state = copy.deepcopy(self.tools.snapshot_state())
            before = self.store.put_json(self._tool_state)
            intent = self.store.append(ToolInvocationPayload(call_id=call.call_id,
                tool_name=call.tool_name, full_arguments=call.full_arguments,
                repeatability=repeatability, operation_key=key, state_before=before),
                source_refs=((self.store.source("tool-recovery-attempt", {
                    "retry_event_id": retry_event.event_id}),) if retry_event else ()))
            self._refresh_counts()
            if self.tool_budget_update is not None:
                self.tool_budget_update(self)
            tool_started = time.monotonic()
            tool_epoch = time.time()
            try:
                raw = await asyncio.wait_for(self._call_tool(call.tool_name, call.full_arguments), timeout=self._wall_timeout())
            except (Exception, asyncio.CancelledError) as exc:
                failed = self._unknown_execution(intent, elapsed=time.monotonic() - tool_started,
                                                 started_epoch=tool_epoch, exception=exc)
                if write:
                    self._inspect_unknown(failed.event_id, before)
                return "unknown_write_outcome" if write else self._exception_reason(exc)
            tool_elapsed = time.monotonic() - tool_started
            duration = self.store.source("tool-duration", {"elapsed_seconds": tool_elapsed,
                "started_epoch": tool_epoch,
                "basis": "local monotonic wall time awaiting tool, including any child tasks"})
            self._fault("after_tool")
            try:
                # Prepare the accepted wire shape before recording the result,
                # so a display-hint projection cannot look like a missing image.
                image_detail_supported = not (self.versions.remote_model.route_id == "opencode-go"
                    and self.model == "mimo-v2.6-flash")
                _, _, shown, _ = convert_tool_result(call.call_id, raw, self.store,
                    image_detail_supported=image_detail_supported,
                    original_image_detail=("high" if self.versions.remote_model.route_id == "paratera"
                                           else "original"))
            except Exception:
                self.store.append(ToolExecutionPayload(call_id=call.call_id,
                    tool_name=call.tool_name, full_arguments=call.full_arguments,
                    raw_result=self.store.capture(raw, force_blob=True),
                    shown_result=MissingCapture(reason="MCP presentation conversion failed"),
                    repeatability=repeatability, operation_key=key, outcome="failed",
                    invocation_event_id=intent.event_id, presentation_status="prepared",
                    retry_event_id=retry_event.event_id if retry_event else None), source_refs=(duration,))
                return "tool_presentation_failed"
            self._tool_state = copy.deepcopy(self.tools.snapshot_state())
            execution = self.store.append(ToolExecutionPayload(call_id=call.call_id,
                tool_name=call.tool_name, full_arguments=call.full_arguments,
                raw_result=self.store.capture(raw, force_blob=True),
                shown_result=self.store.capture(shown, force_blob=True),
                repeatability=repeatability, operation_key=key,
                outcome="failed" if raw.get("isError") else "succeeded",
                applied_write_id=key if write and not raw.get("isError") else None,
                invocation_event_id=intent.event_id, presentation_status="prepared",
                retry_event_id=retry_event.event_id if retry_event else None),
                # The retry lifecycle is kept separately from read/write access.
                source_refs=(self.store.source("tool-state-after", self._tool_state), duration))
            self._fault("after_execution")
            self._accept_execution(execution)
            self._checkpoint()
        self._finish_tool_batch()
        self._checkpoint()
        return None

    def _accept_execution(self, event):
        p = event.payload
        if p.call_id in self.used_ids:
            return
        if p.outcome == "unknown":
            # Retain the original evidence. Recovery policy is checked below;
            # an unknown result is never presented as a completed tool reply.
            return
        if p.shown_result.kind == "missing":
            self.terminal_reason = "resume_pending_operation" if p.outcome == "unknown" else "tool_presentation_failed"
            return
        raw = self.store.resolve(p.raw_result)
        shown = self.store.resolve(p.shown_result)
        self._append_message(shown["tool_message"], self._event_source(event), kind="tool_result")
        self.pending_pictures.extend(shown["image_blocks"])
        if shown["image_blocks"]:
            self.pending_picture_events.append(event.event_id)
        context_read = self._is_context_image_tool(p.tool_name)
        origins = {} if context_read else getattr(self.tools, "image_origins", lambda _: {})(raw)
        if context_read and p.outcome == "succeeded" and p.full_arguments.get("action", "read") == "read":
            # Replay must restore the exact pin even if the process stopped
            # after durable execution but before the context checkpoint.
            self.context.retrieve_image(p.full_arguments["view_id"], p.full_arguments["sha256"],
                                        append_to_history=False)
        image_refs = shown.get("images")
        if image_refs is None:  # Saved pre-C1 presentations remain resumable.
            image_refs = json.loads(shown["tool_message"]["content"]).get("images", [])
        image_details = [block["image_url"].get("detail") for block in shown["image_blocks"]
                         if block.get("type") == "image_url"]
        for index, ref_dict in enumerate(image_refs):
            ref = HashedBlobRef.model_validate_json(json.dumps(ref_dict))
            origin = origins.get(ref.sha256, {})
            path = origin.get("original_path") if isinstance(origin, dict) else origin
            if not context_read or ref.sha256 not in self.originals:
                self.originals[ref.sha256] = self.store.put_bytes(Path(path).read_bytes(), ref.media_type) if path else ref
            if self.context:
                view_id = (p.full_arguments["view_id"] if context_read
                           else origin.get("view_id") if isinstance(origin, dict) else None)
                self.context.register_image(view_id or f"{p.call_id}:{ref.sha256}", ref,
                    source=self._event_source(event), tags=tuple(origin.get("tags", ())) if isinstance(origin, dict) else (),
                    detail=image_details[index] if index < len(image_details) else None)
        self.used_ids.add(p.call_id)
        if self.context_update and self.context and not context_read:
            self.context_update(self, event, raw)

    def _finish_tool_batch(self):
        if self.pending_pictures:
            self._append_message({"role": "user", "content": self.pending_pictures},
                SourceRef(source_id="tool-images", source_kind="tool",
                    locator=",".join(self.pending_picture_events), event_id=self.pending_picture_events[-1],
                    blob=self.store.put_json({"origin_events": self.pending_picture_events,
                        "conversion": "MCP images after all tool replies, exact image bytes"})))
        self.pending_pictures, self.pending_picture_events = [], []
        self.pending_response_id = None

    def _append_message(self, message, source, *, kind="message", tags=()):
        self.messages.append(message)
        self.sources.append(source)
        if self.context:
            self.context.append(message, source, kind=kind, tags=tags)

    def _unknown_execution(self, invocation, *, elapsed=None, started_epoch=None,
                           exception: BaseException | None = None):
        p = invocation.payload
        failure = (ToolFailureDetails(**safe_exception_details(
            exception, stage=f"tool:{p.tool_name}"
        )) if exception is not None else None)
        retry = next((s.blob for s in invocation.source_refs if s.source_id == "tool-recovery-attempt"), None)
        retry_id = json.loads(self.store.get_bytes(retry))["retry_event_id"] if retry else None
        return self.store.append(ToolExecutionPayload(call_id=p.call_id, tool_name=p.tool_name,
            full_arguments=p.full_arguments, repeatability=p.repeatability, operation_key=p.operation_key,
            outcome="unknown", raw_result=MissingCapture(reason="interrupted after durable intent; result not captured"),
            shown_result=MissingCapture(reason="no result was presented"),
            invocation_event_id=invocation.event_id, presentation_status="prepared", failure=failure,
            retry_event_id=retry_id),
            source_refs=((self.store.source("tool-duration", {"elapsed_seconds": elapsed,
                **({"started_epoch": started_epoch} if started_epoch is not None else {})}),)
                         if elapsed is not None else ()))

    def _inspect_unknown(self, event_id, before):
        state = self.store.put_json({"before": before.model_dump(mode="json") if before else None,
            "observed_after": self.tools.snapshot_state(),
            "reason": "snapshot alone cannot prove completion or absence of a late write"})
        self.store.append(StateInspectionPayload(purpose="unknown_write_recovery",
            target_event_id=event_id, persisted_state=state, conclusion="inconclusive"))

    def _tool_recovery_policy(self, name):
        if self._is_context_image_tool(name):
            return "retry_read"
        policy = getattr(self.tools, "recovery_policy", lambda _: "manual")(name)
        if policy not in {"retry_read", "resume_task", "manual"}:
            raise ValueError("unsupported tool recovery policy")
        if policy == "retry_read" and self._tool_repeatability(name) != "read_only":
            raise ValueError("repeatable-read recovery cannot authorize a write")
        return policy

    def _unresolved_tool_calls(self):
        latest = {}
        for event in self.store.events:
            if event.payload.event_type == "tool_execution":
                latest[event.payload.call_id] = event
        return {key: event for key, event in latest.items() if event.payload.outcome == "unknown"}

    def _checkpoint(self):
        with self._measure("checkpoint"):
            return self._checkpoint_unmeasured()

    def _checkpoint_unmeasured(self):
        if self._tool_state is None:
            self._tool_state = copy.deepcopy(self.tools.snapshot_state())
        snapshot = {"messages": self.messages if self.context is None else None,
            "sources": [s.model_dump(mode="json") for s in self.sources] if self.context is None else None,
            "context": self.context.dump() if self.context else None,
            "image_originals": {k: v.model_dump(mode="json") for k, v in self.originals.items()},
            "counts": self.counts, "used_ids": sorted(self.used_ids), "answer": self.answer,
            "completion": self.completion,
            "incomplete_response_count": self.incomplete_response_count,
            "pending_response_id": self.pending_response_id,
            "pending_pictures": self.store.capture(self.pending_pictures).model_dump(mode="json"),
            "pending_picture_events": self.pending_picture_events,
            "terminal_reason": self.terminal_reason, "retry_of": self.retry_of,
            "redispatch_of": self.redispatch_of,
            "answer_repair_request_id": self.answer_repair_request_id,
            "answer_repair_response_id": self.answer_repair_response_id,
            "answer_repair_original": self.answer_repair_original,
            "answer_repair_error": self.answer_repair_error,
            "last_summary_at": self.last_summary_at,
            "control_applied": sorted(self._control_applied), "control_paused": self._control_paused,
            "elapsed_seconds": self.elapsed_seconds(), "started_epoch": self.started_epoch,
            "tool_state": self._tool_state, "config": self._config(),
            "versions": self.versions.model_dump(mode="json"), "last_event_id": self.store.events[-1].event_id}
        ref = self.store.put_json_tree(snapshot)
        self.store.append(CheckpointPayload(state=ref, after_event_id=snapshot["last_event_id"]))
        self.store.write_json("checkpoint.json", ref.model_dump(mode="json"))
        self._fault("after_checkpoint")

    def _restore(self):
        found = self.store.latest_checkpoint()
        if found is None:
            return "resume_checkpoint_missing"
        ref, saved, sequence = found
        self.started_epoch = saved.get("started_epoch", time.time() - saved["elapsed_seconds"])
        self.elapsed_before = max(saved["elapsed_seconds"], time.time() - self.started_epoch)
        self.started = time.monotonic()
        # Resume may not change prompts, tools, policy, budgets, route, or code.
        if saved.get("config", self._config()) != self._config():
            return "resume_configuration_changed"
        if saved["versions"] != self.versions.model_dump(mode="json"):
            return "resume_state_changed"
        suffix = [e for e in self.store.events if e.sequence > sequence]
        if any(e.payload.event_type == "run_lifecycle" and e.payload.reason == "completed" for e in suffix):
            try:
                receipt = json.loads((self.store.task_directory / "receipt.json").read_bytes())
            except (FileNotFoundError, ValueError):
                receipt = None
            anchors = [source.blob for event in suffix for source in event.source_refs
                       if source.source_id == "run-receipt" and source.blob is not None]
            recorded = json.loads(self.store.get_bytes(anchors[-1])) if anchors else None
            # Callers may add domain reports to the materialized receipt. Every
            # journal-owned field must still match, and only the anchored fields
            # are returned; the caller can reconstruct its own annotations.
            if (isinstance(receipt, dict) and isinstance(recorded, dict)
                    and recorded.get("status") == "completed"
                    and all(key in receipt and receipt[key] == value for key, value in recorded.items())):
                completed = recorded.get("completion")
                if ((self.domain_completion and not completed)
                        or (completed and (not self.domain_completion or self.domain_completion(self) != completed))):
                    return "resume_state_changed"
                self._completed_receipt = recorded
                return "already_completed"
        if saved.get("context"):
            from .context import ContextManager
            self.context = ContextManager.load(self.store, saved["context"], policy=self.context_policy)
            self.messages, self.sources = self.context.full_history()
        else:
            self.messages = saved["messages"]
            self.sources = [SourceRef.model_validate_json(json.dumps(s)) for s in saved["sources"]]
        self.originals = {k: HashedBlobRef.model_validate_json(json.dumps(v)) for k, v in saved["image_originals"].items()}
        self.used_ids = set(saved["used_ids"])
        self.answer, self.terminal_reason = saved.get("answer"), saved.get("terminal_reason")
        self.completion = saved.get("completion")
        self.incomplete_response_count = saved.get("incomplete_response_count", 0)
        self.pending_response_id = saved.get("pending_response_id")
        pictures = saved.get("pending_pictures")
        if pictures and pictures.get("kind") == "sha256":  # historical checkpoint
            self.pending_pictures = json.loads(self.store.get_bytes(HashedBlobRef.model_validate(pictures)))
        elif pictures:
            from pydantic import TypeAdapter
            from src.harness_contracts.events import CapturedValue
            self.pending_pictures = self.store.resolve(TypeAdapter(CapturedValue).validate_json(json.dumps(pictures)))
        else:
            self.pending_pictures = []
        self.pending_picture_events = saved.get("pending_picture_events", [])
        self.retry_of = saved.get("retry_of")
        self.redispatch_of = saved.get("redispatch_of")
        self.answer_repair_request_id = saved.get("answer_repair_request_id")
        self.answer_repair_response_id = saved.get("answer_repair_response_id")
        self.answer_repair_original = saved.get("answer_repair_original")
        self.answer_repair_error = saved.get("answer_repair_error")
        self.last_summary_at = saved.get("last_summary_at", 0)
        self._control_applied = set(saved.get("control_applied", []))
        self._control_paused = saved.get("control_paused", False)
        requested_reservations = {e.payload.reservation_id for e in self.store.events
            if e.payload.event_type == "adapter_request" and e.payload.reservation_id}
        released = {r.reservation_id for r in self.task_budget.ledger.releases}
        settled = {s.reservation_id for s in self.task_budget.ledger.settlements}
        for reservation in self.task_budget.ledger.reservations:
            if reservation.reservation_id not in requested_reservations | released | settled:
                from .budget_recovery import release_unsent_reservation
                release_unsent_reservation(self.store, reservation.reservation_id,
                    reason="resume_unsent_reservation")
        self._load_budget()
        self._refresh_counts()
        if (self.terminal_reason in {"money_cny_usage_unavailable", "token_usage_unavailable",
                "unsettled_reported_usage"} and self.counts["usage_complete"]
                and self._settled_token_stop() is None):
            self.terminal_reason = None
        # A checkpoint may have captured a blocked response without accepting
        # it into model history. Replay that response after its late receipt,
        # while already accepted/pending responses remain exactly once.
        reconciled_requests = {r.request_event_id for r in self.task_budget.ledger.reconciliations}
        accepted_response_ids = {source.event_id for source in self.sources}
        suffix = [e for e in self.store.events if e.payload.event_type == "model_response"
            and e.sequence <= sequence and e.payload.request_event_id in reconciled_requests
            and e.event_id not in accepted_response_ids and e.event_id != self.pending_response_id] + suffix
        expected_state = saved["tool_state"]
        committed_summaries = set()
        for event in suffix:
            if event.payload.event_type == "context" and event.payload.details:
                details = json.loads(self.store.get_bytes(event.payload.details))
                if details.get("kind") == "validated_model_summary":
                    committed_summaries.add(details.get("response_event_id"))
        # First reconstruct all durable results, then inspect the real saved state.
        for event in suffix:
            p = event.payload
            if p.event_type == "model_response":
                # A durable response finishes the old admission attempt, even
                # when the process died before saving the next checkpoint.
                self.redispatch_of = None
                request = next(e for e in self.store.events if e.event_id == p.request_event_id)
                reservation_id = request.payload.reservation_id
                if reservation_id:
                    usage = self._effective_budget_usage(reservation_id, p.usage)
                    timing = next((s.blob for s in event.source_refs if s.source_id == "request-duration"), None)
                    elapsed = json.loads(self.store.get_bytes(timing))["elapsed_seconds"] if timing else None
                    reason = self._settle(reservation_id, usage, seconds=elapsed)
                    parsed = parse_response(self.store.resolve(p.raw_response), p.request_event_id,
                        self.store, echo_fields=self._effective_echo_fields())
                    if parsed.finish_reason == "length":
                        truncation = self._record_truncation(event, blocked=reason or (
                            "token_usage_unavailable" if reported_tokens(usage) is None else None))
                        if reason:
                            return reason
                        if truncation.payload.action == "stop":
                            return truncation.payload.reason
                        if request.payload.logical_purpose != "context_summary":
                            self._append_truncation_prompt(truncation)
                        self.retry_of = None
                        continue
                    if reason:
                        return reason
                    if parsed.protocol_error == "empty_response" and reported_tokens(usage) == 0:
                        # Rejected empty replies never become assistant history,
                        # including when a crash happens before retry/stop.
                        if not any(e.payload.event_type == "run_lifecycle" and e.payload.model_failure
                                   and e.payload.model_failure.request_event_id == p.request_event_id
                                   for e in self.store.events):
                            self._record_model_failure(ModelFailureDetails(request_event_id=p.request_event_id,
                                category="empty_response", retryable=True, usage_received=True,
                                service_error_type="empty_response"))
                        continue
                    if reported_tokens(usage) is None:
                        return "token_usage_unavailable"
                    reservation = next(r for r in self.budget.ledger.reservations if r.reservation_id == reservation_id)
                    if request.payload.logical_purpose == "context_summary" or reservation.purpose == "context_summary":
                        if event.event_id in committed_summaries:
                            self.last_summary_at = self.counts["tool_calls"]
                        else:
                            try:
                                self._accept_summary(event)
                            except ValueError:
                                return "summary_validation_failed"
                        continue
                self._accept_response(event)
            elif p.event_type == "tool_execution":
                self._accept_execution(event)
                after = next((s.blob for s in event.source_refs if s.source_id == "tool-state-after"), None)
                if after:
                    expected_state = json.loads(self.store.get_bytes(after))
            elif p.event_type == "context" and self.context:
                self.context.replay_events([event])
            elif p.event_type == "task_control":
                self._apply_control(event)
        if self.context:
            self.messages, self.sources = self.context.full_history()
        complete = {e.payload.invocation_event_id for e in self.store.events if e.payload.event_type == "tool_execution"}
        for event in list(self.store.events):
            p = event.payload
            if p.event_type == "tool_invocation" and event.event_id not in complete:
                result = self._unknown_execution(event)
                if p.repeatability != "read_only":
                    self._inspect_unknown(result.event_id, p.state_before)
        unresolved = self._unresolved_tool_calls()
        for event in unresolved.values():
            if self._tool_recovery_policy(event.payload.tool_name) != "retry_read":
                if event.payload.repeatability != "read_only":
                    self._inspect_unknown(event.event_id, None)
                return "resume_pending_operation"
            attempts = sum(e.payload.event_type == "tool_invocation" and
                e.payload.call_id == event.payload.call_id for e in self.store.events)
            if attempts > self.limits.max_tool_recovery_retries:
                return "tool_recovery_retries_exhausted"
        observed_state = self.tools.snapshot_state()
        safe = expected_state == observed_state
        if not safe and unresolved:
            # Only the domain adapter can attest to harmless observation-cache
            # changes. Its protected source/artifact identity must still match.
            inspect_reads = getattr(self.tools, "can_resume_reads", None)
            safe = bool(inspect_reads and inspect_reads(expected_state, observed_state,
                tuple(event.payload.tool_name for event in unresolved.values())))
        inspected = self.store.append(StateInspectionPayload(purpose="resume",
            target_event_id=saved["last_event_id"], persisted_state=ref,
            conclusion="safe_to_resume" if safe else "inconclusive"))
        if not safe:
            return "resume_state_changed"
        self._tool_state = copy.deepcopy(observed_state)
        if self.completion and (not self.domain_completion or self.domain_completion(self) != self.completion):
            return "resume_state_changed"
        failures = {e.payload.model_failure.request_event_id: e.payload.model_failure
                    for e in self.store.events if e.payload.event_type == "run_lifecycle" and e.payload.model_failure}
        responded = {e.payload.request_event_id for e in self.store.events if e.payload.event_type == "model_response"
                     and e.payload.request_event_id not in failures}
        retried = {e.payload.retry_of_event_id for e in self.store.events
            if e.payload.event_type == "run_lifecycle" and e.payload.action == "retry"}
        retried.update(self._redispatch_parent(e) for e in self.store.events
                       if e.payload.event_type == "adapter_request" and self._redispatch_parent(e))
        dangling = [e for e in self.store.events if e.payload.event_type == "adapter_request"
                    and e.event_id not in responded and e.event_id not in retried]
        if dangling:
            request = dangling[-1]
            failure = failures.get(request.event_id)
            context_retry_ready = (failure is not None and failure.category == "context_length_exceeded"
                and failure.retry_hint is None
                and self.retry_of == request.event_id
                and sum(f.category == "context_length_exceeded" for f in failures.values()) == 1)
            if failure and (failure.retry_hint is not None or (not failure.retryable and not context_retry_ready)):
                return ("context_recovery_required" if failure.category == "context_length_exceeded"
                        else failure.category)
            if request.payload.reservation_id:
                rejected = failure and is_unprocessed_rejection(failure)
                failure_event = next((e for e in self.store.events
                    if e.payload.event_type == "run_lifecycle" and e.payload.model_failure
                    and e.payload.model_failure.request_event_id == request.event_id), None)
                timing = next((s.blob for s in failure_event.source_refs
                    if s.source_id == "request-duration"), None) if failure_event else None
                elapsed = json.loads(self.store.get_bytes(timing))["elapsed_seconds"] if timing else None
                self._settle(request.payload.reservation_id,
                    rejected_request_usage() if rejected else UsageMissing(reason="process interrupted with request outcome unknown"),
                    seconds=elapsed)
            if context_retry_ready:
                # A checkpoint after a strictly smaller projection authorized
                # exactly this context retry before the process interruption.
                self.redispatch_of, self.retry_of = None, request.event_id
            elif (failure and failure.category == "temporary_rate_limit"
                    and failure.retryable and failure.retry_hint is None
                    and self.versions.remote_model.route_id in route_policies()):
                # Resume explicit rate-limit refusals through admission, without
                # consuming transport-failure retries. Usage may still be
                # unknown if a provider receipt was lost before settlement.
                self.redispatch_of, self.retry_of = request.event_id, None
            elif self.answer_repair_request_id is not None:
                return "resume_request_outcome_unknown"
            elif self._uses_reconnect(failure) and request.payload.logical_purpose != "context_summary":
                if (self._recovery_summary()["remaining_seconds"] <= 0
                        and not self._recovery_capacity.active_reservations):
                    return "model_reconnect_budget_exhausted"
                self.redispatch_of, self.retry_of = None, request.event_id
            elif self._request_retry_count(request.event_id) >= self.limits.max_model_retries:
                return self._failure_stop_reason(failure) if failure else "resume_request_outcome_unknown"
            elif request.payload.logical_purpose == "context_summary":
                return "resume_request_outcome_unknown"
            else:
                self.redispatch_of, self.retry_of = None, request.event_id
        self._refresh_counts()
        reason = self._settled_token_stop()
        if reason:
            return reason
        self.store.append(RunLifecyclePayload(action="resume", reason="durable results replayed and saved state rechecked",
            state_inspection_event_id=inspected.event_id, checkpoint=ref))
        self._checkpoint()
        return None

    async def _summarize(self):
        # A constrained summary may rearrange existing state; it cannot invent facts.
        from .context import SummaryCandidate
        projection = self.context.project(required_tags=self.required_context_tags,
            required_view_ids=self.required_view_ids, consume_retrievals=False)
        history_ids = projection.omitted_history_ids or tuple(r.history_id for r in self.context.history
            if r.source.event_id and r.kind != "summary")
        if not history_ids:
            self.last_summary_at = self.counts["tool_calls"]
            return None
        request = self.context.build_summary_request(history_ids)
        content = {"instruction": request.instruction,
            "response_schema": SummaryCandidate.model_json_schema(),
            "covered_history_ids": list(history_ids),
            "history_references": [{"history_id": r.history_id,
                "message_blob": r.message_blob.model_dump(mode="json"), "source": r.source.model_dump(mode="json")}
                for r in request.history],
            "current_state": [s.model_dump(mode="json") for s in request.current_state],
            "text_encoding": "JSON array of statements, UTF-8, sorted keys, no whitespace separators; values and epistemic status must be exact"}
        messages = [{"role": "system", "content": "Select useful existing state entries for a compact index. Return only the required JSON object; do not add or reinterpret facts."},
                    {"role": "user", "content": json.dumps(content, ensure_ascii=False)}]
        sources = [self.store.source(f"context-summary-request-{i}", m) for i, m in enumerate(messages)]
        # If interrupted after a truncated summary, reconstruct only the short
        # recovery instruction. The discarded reasoning never enters history.
        summary_requests = {e.event_id for e in self.store.events
            if e.payload.event_type == "adapter_request" and e.payload.logical_purpose == "context_summary"}
        previous = next((e for e in reversed(self.store.events)
            if e.payload.event_type == "model_response" and e.payload.request_event_id in summary_requests), None)
        recovery = next((e for e in reversed(self.store.events)
            if e.payload.event_type == "response_truncation" and previous is not None
            and e.payload.response_event_id == previous.event_id), None)
        if recovery is not None and recovery.payload.action == "continue":
            messages.append(self._truncation_prompt(purpose="context_summary"))
            sources.append(self._event_source(recovery))
        response, reason = await self._model_call(messages, sources,
            purpose="context_summary", tools=[])
        if reason:
            return reason
        try:
            self._accept_summary(response)
        except ValueError:
            return "summary_validation_failed"
        self.retry_of = None
        self._checkpoint()
        return None

    def _accept_summary(self, response):
        from .context import SummaryCandidate
        if response.payload.tool_calls:
            raise ValueError("context summaries cannot invoke tools")
        text = "\n".join(response.payload.visible_text)
        candidate = SummaryCandidate.model_validate_json(text)
        self.context.compact_with_summary(candidate, source=self._event_source(response))
        self.last_summary_at = self.counts["tool_calls"]

    def _remaining(self):
        # Infinity is only an internal comparison identity, never a wire value.
        return (float("inf") if self.limits.seconds is None
                else max(0.0, self.limits.seconds - self.elapsed_seconds()))

    def elapsed_seconds(self):
        return self.elapsed_before + max(0.0, time.monotonic() - self.started)

    def _wall_timeout(self):
        return None if self.limits.seconds is None else self._remaining()

    def _wall_remaining_amount(self):
        remaining = self._wall_timeout()
        return Decimal(str(remaining)) if remaining is not None else None

    def _budget_stop(self):
        self._load_budget()
        settled_stop = self._settled_token_stop()
        if settled_stop:
            return settled_stop
        if self._remaining() <= 0:
            return self._scoped_budget_reason("time", task=True)
        if self.counts["model_calls"] >= self.limits.model_calls:
            return self._scoped_budget_reason("model", task=True)
        if self.task_budget.available.calls <= 0:
            return self._scoped_budget_reason("model", task=True)
        if self.task_budget.available.tokens is not None and self.task_budget.available.tokens <= 0:
            return self._scoped_budget_reason("token", task=True)
        if (self.task_budget.available.money_usd is not None
                and self.task_budget.available.money_usd <= 0):
            return self._scoped_budget_reason("money", task=True)
        if self.task_budget.available.money_cny is not None and self.task_budget.available.money_cny <= 0:
            return self._scoped_budget_reason("money", task=True)
        if self.task_budget.available.seconds is not None and self.task_budget.available.seconds <= 0:
            return self._scoped_budget_reason("time", task=True)
        if self.budget.available.calls <= 0:
            return self._scoped_budget_reason("model")
        if self.budget.available.tokens is not None and self.budget.available.tokens <= 0:
            return self._scoped_budget_reason("token")
        if self.budget.available.money_usd is not None and self.budget.available.money_usd <= 0:
            return self._scoped_budget_reason("money")
        if self.budget.available.money_cny is not None and self.budget.available.money_cny <= 0:
            return self._scoped_budget_reason("money")
        if self.budget.available.seconds is not None and self.budget.available.seconds <= 0:
            return self._scoped_budget_reason("time")
        return None

    def _can_wait_for_root_time_reservation(self, reason):
        """True only when a live sibling hold can still release root seconds."""

        root_reason = self._scoped_budget_reason("time")
        if reason != root_reason or self._remaining() <= 0:
            return False
        if self.task_budget.available.seconds is not None and self.task_budget.available.seconds <= 0:
            return False
        ledger = self.budget.ledger
        if ledger.available.seconds is None or ledger.available.seconds > 0:
            return False
        limit = ledger.total_limit.seconds
        charged = ledger.charged.seconds or Decimal(0)
        if limit is None or charged >= limit:
            return False
        return bool(self._active_root_time_hold_ids())

    def _active_root_time_hold_ids(self):
        settled = {item.reservation_id for item in self.budget.ledger.settlements} | {
            item.reservation_id for item in self.budget.ledger.releases}
        return tuple(sorted(
            reservation.reservation_id
            for reservation in self.budget.ledger.reservations
            if reservation.reservation_id not in settled
            and reservation.reservation_id in self._budget_capacity.active_reservations
            and (reservation.amounts.seconds or 0) > 0
        ))

    async def _wait_for_transient_root_time_reservation(
        self, reason, *, revision, lease=None
    ):
        """Wait once for a live hold, returning None when admission should retry."""

        if not self._can_wait_for_root_time_reservation(reason):
            return reason
        if lease:
            lease.release()
        wait_id = f"{self.store.task_id}:budget-wait-{1 + sum(
            event.payload.event_type == 'budget_wait' and event.payload.phase == 'begin'
            for event in self.store.events
        )}"
        started = time.monotonic()
        self.store.append(BudgetWaitPayload(
            phase="begin", wait_id=wait_id, reason=reason,
            active_hold_ids=self._active_root_time_hold_ids(),
            root_available=self.budget.available,
            task_available=self.task_budget.available,
            wall_remaining_seconds=self._wall_remaining_amount(),
        ))
        try:
            changed = await self._budget_capacity.wait_for_change(
                revision, timeout=self._wall_timeout()
            )
        except asyncio.CancelledError:
            self._load_budget()
            self.store.append(BudgetWaitPayload(
                phase="end", wait_id=wait_id, reason=reason,
                active_hold_ids=self._active_root_time_hold_ids(),
                root_available=self.budget.available,
                task_available=self.task_budget.available,
                wall_remaining_seconds=self._wall_remaining_amount(),
                elapsed_seconds=Decimal(str(max(0.0, time.monotonic() - started))),
                outcome="cancelled",
            ))
            raise
        self._load_budget()
        self.store.append(BudgetWaitPayload(
            phase="end", wait_id=wait_id, reason=reason,
            active_hold_ids=self._active_root_time_hold_ids(),
            root_available=self.budget.available,
            task_available=self.task_budget.available,
            wall_remaining_seconds=self._wall_remaining_amount(),
            elapsed_seconds=Decimal(str(max(0.0, time.monotonic() - started))),
            outcome="capacity_changed" if changed else "wall_deadline_exhausted",
        ))
        return None if changed else self._scoped_budget_reason("time", task=True)

    async def _retry_budget_available(self):
        """Retry/redispatch admission shares the same transient-hold wait path."""

        while True:
            revision = self._budget_capacity.revision
            reason = self._budget_stop()
            if reason is None:
                return True
            reason = await self._wait_for_transient_root_time_reservation(
                reason, revision=revision
            )
            if reason is not None:
                return False

    async def _yield_budget_waiters(self):
        # A just-settled task must not synchronously reserve the released
        # seconds again before older waiters have a chance to re-enter.
        if self._budget_capacity.waiters:
            await asyncio.sleep(0)

    def _budget_reason(self, decision, *, scope=None):
        names = {"calls": "model", "tokens": "token", "seconds": "time", "money_usd": "money", "money_cny": "money"}
        if decision.exceeded_dimensions:
            reason = names[decision.exceeded_dimensions[0]] + "_budget_exhausted"
            return f"{scope}_{reason}" if scope else reason
        return decision.reason

    def _scoped_budget_reason(self, dimension, *, task=False):
        reason = f"{dimension}_budget_exhausted"
        if self.store.is_root_task:
            return reason
        return f"{'child' if task else 'root'}_{reason}"

    def _retry_count(self):
        return sum(e.payload.event_type == "run_lifecycle" and e.payload.action == "retry" for e in self.store.events)

    def _request_retry_count(self, request_id):
        """Count the current failed-request chain, not unrelated prior retries."""
        count = 0
        while request_id:
            request = next(e for e in self.store.events if e.event_id == request_id)
            parent = self._redispatch_parent(request)
            if parent:
                request_id = parent
                continue
            prior = [e for e in self.store.events if e.sequence < request.sequence
                     and (e.payload.event_type == "adapter_request" or
                          e.payload.event_type == "run_lifecycle" and e.payload.action == "retry"
                          and any(target.event_id == e.payload.retry_of_event_id and
                                  target.payload.event_type == "adapter_request" for target in self.store.events))]
            if not prior or prior[-1].payload.event_type != "run_lifecycle":
                break
            count += 1
            request_id = prior[-1].payload.retry_of_event_id
        return count

    def _redispatch_parent(self, request):
        ref = next((s.blob for s in request.source_refs if s.source_id == "request-dispatch"), None)
        return json.loads(self.store.get_bytes(ref)).get("redispatch_of") if ref else None

    @asynccontextmanager
    async def _dispatch_slot(self):
        gate = get_dispatcher(self.versions.remote_model.route_id)
        if gate is None:
            yield None
            return
        self.stage = "request_queue"
        started, epoch = time.monotonic(), time.time()
        previous = self._queue_interruption or {}
        try:
            lease = await gate.acquire(timeout=self._wall_timeout(), redispatch_of=self.redispatch_of)
        except (TimeoutError, asyncio.CancelledError):
            self._queue_interruption = {"route_id": gate.route_id,
                "started_epoch": previous.get("started_epoch", epoch),
                "ended_epoch": time.time(),
                "queue_seconds": previous.get("queue_seconds", 0.0) + time.monotonic() - started,
                "outcome": "not_sent", "redispatch_of": self.redispatch_of}
            raise
        if previous:
            # Output-budget reduction can re-prepare before any send. Keep the
            # already observed queue wait when acquiring a fresh permit.
            lease.evidence["queue_seconds"] += previous["queue_seconds"]
            lease.evidence["started_epoch"] = previous["started_epoch"]
        try:
            yield lease
        finally:
            if not getattr(lease, "recorded", False):
                self._queue_interruption = {**lease.evidence, "outcome": "not_sent"}
            lease.release()

    def _record_model_failure(self, failure, *, elapsed=None, dispatch_adjustment=None):
        sources = []
        if self.versions.remote_model.route_id == "chatgpt-subscription" and not failure.usage_received:
            observed = getattr(self.adapter, "last_received_usage", None)
            if isinstance(observed, UsageReported):
                sources.append(self.store.source("observed-stream-usage", {
                    "usage": observed.model_dump(mode="json"), "completeness": "unknown",
                    "note": "Observed before terminal failure; final usage is unknown. This evidence does not settle or release the token hold."}))
        if elapsed is not None:
            sources.append(self.store.source("request-duration", {"elapsed_seconds": elapsed,
                "basis": "local monotonic wall time from send through failure",
                "phases": {"adapter_send_seconds": elapsed,
                    **getattr(self.adapter, "last_send_timing", {})}}))
        if dispatch_adjustment is not None:
            sources.append(self.store.source("dispatch-adjustment", dispatch_adjustment))
        self.store.append(RunLifecyclePayload(action="failure", failure_stage="model_request",
            reason=failure.category, model_failure=failure), source_refs=tuple(sources))

    async def _allow_model_retry(self, failure):
        attempt = self._request_retry_count(failure.request_event_id)
        reconnect = self._uses_reconnect(failure)
        if (not failure.retryable or failure.retry_hint is not None or self.answer_repair_request_id is not None
                or (not reconnect and attempt >= self.limits.max_model_retries)):
            return False
        if not await self._retry_budget_available():
            return False
        delay = self.limits.retry_backoff_seconds * 2 ** min(attempt, 30)
        if reconnect:
            delay = min(30.0, delay)
            if not await self._wait_for_recovery_capacity(delay):
                return False
        if delay >= self._remaining():
            return False
        if reconnect:
            operation = self._reserve_recovery("backoff", failure.request_event_id, delay)
            started = perf_counter()
            self._fault("after_reconnect_backoff_reservation")
            try:
                await asyncio.sleep(delay)
            finally:
                self._settle_recovery(operation, perf_counter() - started)
        else:
            await asyncio.sleep(delay)
        return await self._retry_budget_available()

    def _uses_reconnect(self, failure):
        return (failure is not None and self.limits.model_reconnect_seconds > 0
                and failure.retryable and failure.retry_hint is None
                and failure.category in {"transport_error", "timeout"}
                and self.answer_repair_request_id is None)

    def _retry_uses_reconnect(self):
        failure = next((e.payload.model_failure for e in reversed(self.store.events)
                        if e.payload.event_type == "run_lifecycle" and e.payload.model_failure
                        and e.payload.model_failure.request_event_id == self.retry_of), None)
        return self._uses_reconnect(failure)

    def _recovery_summary(self):
        from .network_recovery import recovery_summary
        return recovery_summary(self.store, limit_seconds=self.limits.model_reconnect_seconds,
                                inherited_seconds=self.reconnect_consumed_before_seconds)

    def _recovery_stop_reason(self):
        return (self._scoped_budget_reason("time", task=True) if self._remaining() <= 0
                else "model_reconnect_budget_exhausted")

    async def _wait_for_recovery_capacity(self, minimum):
        # Another live request temporarily reserves the remaining recovery
        # allowance until headers arrive. A crash hold has no live signal and
        # remains charged; do not wait forever or mistake it for free capacity.
        while True:
            revision = self._recovery_capacity.revision
            if self._recovery_summary()["remaining_seconds"] > minimum:
                return True
            if not self._recovery_capacity.active_reservations or self._remaining() <= 0:
                return False
            await self._recovery_capacity.wait_for_change(
                revision, timeout=min(60.0, self._remaining()))

    def _reserve_recovery(self, phase, request_id, seconds):
        operation = NetworkRecoveryPayload(action="reserve",
            operation_id=f"network-recovery-{len(self.store.all_events) + 1}",
            phase=phase, request_event_id=request_id, reserved_seconds=seconds)
        self.store.append(operation)
        self._recovery_capacity.start(operation.operation_id)
        return operation

    def _settle_recovery(self, operation, seconds):
        self.store.append(operation.model_copy(update={"action": "settle", "elapsed_seconds": max(0.0, seconds)}))
        self._recovery_capacity.complete(operation.operation_id)

    def _reserve_request_recovery(self, request):
        if not self._retry_uses_reconnect():
            return None
        phase = "response_headers" if callable(getattr(self.adapter, "send_reconnecting", None)) else "adapter_request"
        return self._reserve_recovery(phase, request.event_id, self._recovery_summary()["remaining_seconds"])

    async def _send_with_recovery(self, prepared, operation, *, timeout):
        if operation is None:
            return await self.adapter.send(prepared, timeout=timeout)
        started, settled = perf_counter(), False
        def connected(seconds):
            nonlocal settled
            if not settled:
                self._settle_recovery(operation, seconds)
                settled = True
        try:
            if operation.phase == "response_headers":
                return await self.adapter.send_reconnecting(prepared, timeout=timeout,
                    recovery_timeout=operation.reserved_seconds, on_connected=connected)
            # Adapters without phase callbacks cannot distinguish connection
            # from generation: bound and label their complete retry attempt.
            return await asyncio.wait_for(self.adapter.send(prepared, timeout=timeout),
                                          timeout=operation.reserved_seconds)
        finally:
            if not settled:
                connected(perf_counter() - started)

    def _failure_stop_reason(self, failure):
        budget = self._budget_stop()
        if budget and self._can_wait_for_root_time_reservation(budget):
            # A transient sibling hold must not replace the actual terminal
            # failure when no further retry/redispatch will be attempted.
            budget = None
        if budget:
            return budget
        if self._uses_reconnect(failure):
            delay = min(30.0, self.limits.retry_backoff_seconds
                        * 2 ** min(self._request_retry_count(failure.request_event_id), 30))
            if delay >= self._remaining():
                return self._scoped_budget_reason("time", task=True)
            return "model_reconnect_budget_exhausted"
        if failure.retryable and self._request_retry_count(failure.request_event_id) >= self.limits.max_model_retries:
            return "model_retries_exhausted:" + failure.category
        if failure.retryable and self.limits.retry_backoff_seconds * 2 ** self._request_retry_count(
                failure.request_event_id) >= self._remaining():
            return self._scoped_budget_reason("time", task=True)
        return failure.category

    def _fault(self, name):
        if self.fault_hook:
            self.fault_hook(name, self)

    def _event_source(self, event):
        return SourceRef(source_id=event.event_id, source_kind="tool" if event.payload.event_type == "tool_execution" else "runtime",
            locator=f"events.jsonl:{event.sequence + 1}", event_id=event.event_id)

    def _exception_reason(self, exc):
        timeout = isinstance(exc, (TimeoutError, asyncio.TimeoutError))
        cancel = isinstance(exc, asyncio.CancelledError)
        reason = ((self._scoped_budget_reason("time", task=True)
                   if self.limits.seconds is not None else "operation_timeout")
                  if timeout else "cancelled" if cancel else "failed")
        action = "timeout" if timeout else "cancel" if cancel else "failure"
        fields = {"failure_stage": self.stage} if action == "failure" else {}
        self.store.append(RunLifecyclePayload(action=action,
            reason=f"{reason}; error_type={type(exc).__name__}", **fields),
            source_refs=((self.store.source("request-dispatch", self._queue_interruption),)
                         if getattr(self, "_queue_interruption", None) else ()))
        return reason

    def _exception_stop(self, exc):
        try:
            return self._stop(self._exception_reason(exc))
        except MemoryError:
            # Do not repeat finalization, collect artifacts, or reconstruct usage
            # after memory pressure has already broken normal failure handling.
            # This small best-effort receipt is not a completed journal or bill.
            receipt = {"status": "failed", "receipt_kind": "minimal_memory_failure",
                "run_id": self.store.run_id, "task_id": self.store.task_id,
                "failure": {"error_type": type(exc).__name__, "failure_stage": self.stage,
                    "stop_error_type": "MemoryError"},
                "answer": None, "completion": None, "artifacts": [], "artifact_paths": [],
                "accounting_complete": False, "counts_snapshot": dict(self.counts),
                "elapsed_seconds": self.elapsed_seconds(),
                "budget_scope": "root" if self.store.is_root_task else "child",
                "note": "Normal stop failed in memory allocation. Counts are an unreconciled snapshot; journal and saved artifacts remain the recovery authority."}
            self.store.write_json("receipt.json", receipt)
            return receipt

    def _stop(self, reason):
        self._load_budget()
        self._refresh_counts()
        if reason == "completed" and self.domain_completion and self.completion is None:
            reason = "domain_completion_unmet"
        # Domain finalization reuses already saved artifacts; it must run before
        # their hashes and the final receipt are recorded. No new model request.
        finalization = (self.finalize_run(self, reason) if self.finalize_run
                        and not reason.startswith("resume_") else None)
        if (self.completion is not None and isinstance(finalization, dict)
                and finalization.get("completion_valid") is False):
            # A domain may detect stale completion evidence during its last
            # handoff check. Retain that evidence, but never report success.
            reason = "completion_state_changed"
        artifacts, paths = [], []
        for path in self.tools.artifacts():
            if path.is_file():
                artifacts.append(self.store.put_file(path))
                paths.append(str(path))
        costs = [s.cost for s in self.task_budget.ledger.effective_settlements]
        estimated = self.task_budget.ledger.committed.money_usd
        accounting_records = [request_accounting_from_store(self.store, e.event_id)
            for e in self.store.all_events if e.payload.event_type == "adapter_request"]
        task_accounting = summarize_request_accounting(
            row for row in accounting_records if row.task_id == self.store.task_id)
        from .timing import summarize_timing
        # A timeout/cleanup may finish beyond the deadline; timing must retain
        # the observed wall duration instead of clipping it to the time limit.
        elapsed = self.elapsed_seconds()
        timing = summarize_timing(self.store, current_task=self.store.task_id,
            started_epoch=self.started_epoch, ended_epoch=self.started_epoch + elapsed,
            status=reason, pending_queue=getattr(self, "_queue_interruption", None))
        from .network_recovery import request_outcome_timing
        timing["request_outcomes"] = request_outcome_timing(
            self.store, task_id=self.store.task_id, elapsed_seconds=elapsed)
        from .versions import version_labels
        receipt = {"status": reason, "answer": self.answer,
            **({"completion": self.completion} if self.completion is not None else {}),
            **({"consecutive_incomplete_responses": self.incomplete_response_count}
               if self.domain_completion else {}),
            **version_labels(self.versions),
            **({"finalization": finalization} if finalization is not None else {}),
            "started_epoch": self.started_epoch,
            "deadline_epoch": (self.started_epoch + self.limits.seconds if self.limits.seconds is not None else None),
            "agent_version": self.versions.agent_version.identifier if self.versions.agent_version else None,
            "output_limit_policy": validate_output_limit(self.model,
                self.parameters.get("max_tokens", self.parameters.get("max_completion_tokens")),
                reason=self.low_output_limit_reason),
            **({"subscription_output_policy": {
                "local_reservation_tokens": self.parameters["max_tokens"],
                "service_output_cap": None, "enforcement": "local_reservation_only",
                "note": "Requests are bounded by runtime call/time budgets. Output allowance is not a server cap; actual usage is settled, and exceeding the run token budget stops further work."}}
               if self.versions.remote_model.route_id == "chatgpt-subscription" else {}),
            **self.counts, "elapsed_seconds": elapsed, "timing": timing,
            "network_recovery": self._recovery_summary(),
            "runtime_processing": {"process_phase_seconds": dict(self._phase_seconds),
                "shared_journal": getattr(self.store, "timing_totals", {}),
                "scope": "current process only; phases can contain journal work and must not be summed as exclusive wall time"},
            "limits": self.limits.model_dump(mode="json"), "retries": self._retry_count(), "fallback": False,
            "truncations": sum(e.payload.event_type == "response_truncation" for e in self.store.events),
            "root_truncations": sum(e.payload.event_type == "response_truncation" for e in self.store.all_events),
            "billing_usd": str(sum(c.usd for c in costs)) if costs and all(c.kind == "reported" for c in costs) else None,
            "estimated_money_upper_bound_usd": str(estimated) if estimated is not None else None,
            "artifacts": [r.model_dump(mode="json") for r in artifacts], "artifact_paths": paths,
            "token_accounting": "provider-reported usage plus separately estimated images when omitted, and outstanding/unknown holds; estimates are not bills",
            "image_tokens_estimate": task_accounting["image_tokens_estimate"],
            "estimated_cost_cny": task_accounting["estimated_cost_cny"],
            "committed_estimate_cny": (str(self.budget.ledger.committed.money_cny)
                if self.budget.ledger.committed.money_cny is not None else None),
            "cny_ceiling_note": "CNY ceiling uses registered rates and conservative holds, not provider bills. Missing usage retains its hold and stops further requests.",
            "usage_accounting": task_accounting,
            "root_usage_accounting": summarize_request_accounting(accounting_records),
            "request_usage_accounting": [row.receipt_dict() for row in accounting_records
                if row.task_id == self.store.task_id],
            "budget": self.budget.ledger.model_dump(mode="json"),
            "root_budget_available": self.budget.available.model_dump(mode="json"),
            "task_budget": self.task_budget.ledger.model_dump(mode="json"),
            "task_budget_available": self.task_budget.available.model_dump(mode="json"),
            "budget_scope": "root" if self.store.is_root_task else "child"}
        self.store.append(RunLifecyclePayload(action="stop", reason=reason, partial_artifacts=tuple(artifacts)),
            source_refs=(self.store.source("run-receipt", receipt),
                *((self.store.source("request-dispatch", self._queue_interruption),)
                  if getattr(self, "_queue_interruption", None) else ())))
        usage = UsageReported(raw_usage={"total_tokens": self.counts["reported_tokens"]}) if self.counts["usage_complete"] and self.counts["model_calls"] else UsageMissing(reason="one or more requests lacked complete usage, or none were sent")
        self.store.append(RunAggregateUsagePayload(usage=usage,
            raw_summary=self.store.capture(receipt), notes=("Locally summed usage; no aggregate provider bill was supplied.",)))
        self.store.write_json("receipt.json", receipt)
        self.store.validate()
        return receipt
