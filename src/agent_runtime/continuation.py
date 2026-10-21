"""Read-only evidence and conserved allowances for an explicit new-run continuation.

Continuation is a new, operator-selected execution, not a retry of the failed
request or a way to bypass strict same-version checkpoint recovery.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

from src.harness_contracts import BudgetAmounts, EventEnvelope, HashedBlobRef
from .budget import RuntimeBudget
from .store import EventStore


class ReadOnlyRun:
    """The read surface of EventStore, without locks, mkdir, writes, or repair."""

    def __init__(self, directory):
        self.directory = Path(directory).resolve(strict=True)
        if not self.directory.is_dir():
            raise ValueError("continuation source must be a run directory")
        self.task_directory = self.directory
        self._root = self
        self._hashes = {}
        metadata = self.read_json("journal.json")
        self.run_id = metadata["run_id"]
        self.task_id = self.root_task_id = metadata["task_id"]
        self.parent_task_id = None
        self.budget_limit = BudgetAmounts.model_validate_json(json.dumps(metadata["budget_limit"]))
        raw = self.read_bytes("events.jsonl")
        if not raw or not raw.endswith(b"\n"):
            raise ValueError("continuation needs a complete nonempty journal")
        self._all_events = tuple(EventEnvelope.model_validate_json(line) for line in raw.splitlines())
        if any(event.run_id != self.run_id for event in self._all_events):
            raise ValueError("continuation journal identity differs from metadata")
        EventStore._validate_events(self, list(self._all_events))

    def read_bytes(self, relative):
        relative = Path(relative)
        if relative.is_absolute():
            raise ValueError("continuation reads require run-relative paths")
        path = (self.directory / relative).resolve(strict=True)
        if not path.is_relative_to(self.directory) or not path.is_file():
            raise ValueError("continuation reference escapes the source run")
        raw = path.read_bytes()
        key = path.relative_to(self.directory).as_posix()
        digest = hashlib.sha256(raw).hexdigest()
        previous = self._root._hashes.setdefault(key, digest)
        if previous != digest:
            raise ValueError("continuation source changed during inspection")
        return raw

    def read_json(self, relative):
        return json.loads(self.read_bytes(relative))

    def verified_versions(self):
        """Bind the convenience sidecar to the approved root request journal."""
        saved = self.read_json("versions.json")
        durable = [event.payload.versions.model_dump(mode="json") for event in self.events
                   if event.payload.event_type == "adapter_request"]
        if not durable or any(value != saved for value in durable):
            raise ValueError("source versions differ from its durable root request identity")
        return saved

    def verified_role_configuration(self):
        versions = self.verified_versions()
        stamp = versions.get("model_route") or {}
        blob = (stamp.get("evidence") or {}).get("blob")
        if blob is None:
            raise ValueError("source has no durable role routing evidence")
        route = json.loads(self.get_bytes(blob))
        saved = self.read_json("role_configuration.json")
        if route.get("roles") != saved:
            raise ValueError("source role configuration differs from its durable request route")
        return saved

    @property
    def events(self):
        return [event for event in self._root._all_events if event.task_id == self.task_id]

    @property
    def all_events(self):
        return list(self._root._all_events)

    @property
    def evidence_hashes(self):
        return dict(self._root._hashes)

    def verify_unchanged(self):
        for name, expected in self.evidence_hashes.items():
            if hashlib.sha256(self.read_bytes(name)).hexdigest() != expected:
                raise ValueError("continuation source changed after inspection")
        return True

    def for_task(self, task_id, parent_task_id):
        if not isinstance(task_id, str) or not task_id or not isinstance(parent_task_id, str):
            raise ValueError("continuation task identities must be nonempty strings")
        child = object.__new__(type(self))
        child._root = self._root
        child.directory = self.directory
        child.task_directory = self.directory / "tasks" / hashlib.sha256(task_id.encode()).hexdigest()
        child.run_id, child.task_id = self.run_id, task_id
        child.root_task_id, child.parent_task_id = self.root_task_id, parent_task_id
        child.budget_limit = self.budget_limit
        return child

    def get_bytes(self, ref):
        ref = HashedBlobRef.model_validate(ref) if isinstance(ref, dict) else ref
        raw = self.read_bytes(ref.uri)
        if hashlib.sha256(raw).hexdigest() != ref.sha256:
            raise ValueError("continuation attachment hash mismatch")
        return raw

    def get_json_tree(self, ref):
        from .json_tree import read_json_tree
        return read_json_tree(ref, self.get_bytes)

    def resolve(self, capture):
        return EventStore.resolve(self, capture)

    def capture_bytes(self, capture):
        return EventStore.capture_bytes(self, capture)


def continuation_allowance(source: ReadOnlyRun, requested_limits, *, expected_events_sha256,
                           allow_recovery_policy_change=False):
    """Deduct old calls, tools, active time, and unknown conservative charges.

    The operator starts a new run after the old terminal event. Offline repair
    downtime is not new execution time; both already-used wall time and shared
    request-time charges continue to constrain the remaining allowance.
    """
    from .loop import RunLimits

    digest = source.evidence_hashes["events.jsonl"]
    if digest != expected_events_sha256:
        raise ValueError("continuation source journal does not match the approved identity")
    events = source.all_events
    root_stops = [event for event in source.events
                  if event.payload.event_type == "run_lifecycle" and event.payload.action == "stop"]
    if not root_stops:
        raise ValueError("continuation requires a terminal source run")
    stop = root_stops[-1]
    permitted = {"service_error", "service_unavailable", "transport_error", "timeout", "cancelled",
                 "model_retries_exhausted:transport_error", "model_retries_exhausted:timeout",
                 "model_retries_exhausted:service_unavailable", "model_reconnect_budget_exhausted"}
    if stop.payload.reason not in permitted:
        raise ValueError("source stop does not admit a new service-failure continuation")
    if stop.payload.reason == "model_reconnect_budget_exhausted":
        last_failure = next((event.payload.model_failure for event in reversed(source.events)
            if event.payload.event_type == "run_lifecycle" and event.payload.model_failure), None)
        if last_failure is None or last_failure.category not in {"transport_error", "timeout"}:
            raise ValueError("reconnect exhaustion lacks a recorded network failure")
    if any(event.sequence > stop.sequence and event.payload.event_type in {
            "adapter_request", "tool_invocation", "tool_execution"} for event in events):
        raise ValueError("source run contains work after its terminal stop")
    requests = {event.event_id for event in events if event.payload.event_type == "adapter_request"}
    responses = {event.payload.request_event_id for event in events if event.payload.event_type == "model_response"}
    failures = {event.payload.model_failure.request_event_id for event in events
                if event.payload.event_type == "run_lifecycle" and event.payload.model_failure is not None}
    if requests - responses - failures:
        raise ValueError("source has an unresolved model request; reconcile before continuation")
    invoked = {event.event_id for event in events if event.payload.event_type == "tool_invocation"}
    executed = {event.payload.invocation_event_id for event in events if event.payload.event_type == "tool_execution"}
    if invoked - executed or any(event.payload.event_type == "tool_execution"
                                and event.payload.outcome == "unknown" for event in events):
        raise ValueError("source has an unresolved tool operation; reconcile before continuation")
    receipt = source.read_json("receipt.json")
    if receipt.get("status") != stop.payload.reason:
        raise ValueError("source receipt disagrees with terminal journal")
    config_refs = [ref for event in source.events if event.payload.event_type == "run_lifecycle"
                   and event.payload.action == "start" for ref in event.source_refs
                   if ref.source_id == "runtime-config" and ref.blob is not None]
    if len(config_refs) != 1:
        raise ValueError("source has no unique durable runtime configuration")
    config = json.loads(source.get_bytes(config_refs[0].blob))
    old_limits = RunLimits.model_validate_json(json.dumps(config["limits"]))
    if old_limits.ledger_limit() != source.budget_limit:
        raise ValueError("source runtime and ledger limits disagree")
    budget = RuntimeBudget.from_events(source.budget_limit, events)
    if budget.fatal_reason is not None:
        raise ValueError("source budget has an unresolved accounting failure")
    ledger = budget.ledger
    if any(res.reservation_id not in {s.reservation_id for s in ledger.settlements}
           | {r.reservation_id for r in ledger.releases} for res in ledger.reservations):
        raise ValueError("source has outstanding budget reservations")
    summaries = [event for event in source.events if event.payload.event_type == "run_usage_summary"
                 and event.sequence > stop.sequence]
    if not summaries:
        raise ValueError("source has no durable terminal accounting summary")
    durable = source.resolve(summaries[-1].payload.raw_summary)
    elapsed = durable.get("elapsed_seconds")
    if durable.get("status") != stop.payload.reason or receipt.get("elapsed_seconds") != elapsed:
        raise ValueError("source receipt timing differs from its durable terminal summary")
    if (type(elapsed) not in {int, float} or not math.isfinite(elapsed) or elapsed < 0
            or (old_limits.seconds is not None and elapsed > old_limits.seconds)):
        raise ValueError("source elapsed time is missing or exhausted")
    available = ledger.available
    seconds = None if old_limits.seconds is None else old_limits.seconds - elapsed
    if available.seconds is not None:
        seconds = float(available.seconds) if seconds is None else min(seconds, float(available.seconds))
    values = requested_limits.model_dump()
    for name, remaining in {"model_calls": min(available.calls, old_limits.model_calls - len(requests)),
                            "tool_calls": old_limits.tool_calls - len(invoked),
                            "seconds": seconds,
                            "tokens": available.tokens, "money_usd": available.money_usd,
                            "money_cny": available.money_cny}.items():
        wanted = values[name]
        values[name] = remaining if wanted is None else wanted if remaining is None else min(wanted, remaining)
    if (values["model_calls"] <= 0 or values["tool_calls"] <= 0
            or (values["seconds"] is not None and values["seconds"] <= 0)):
        raise ValueError("source has no remaining continuation execution allowance")
    recovery_fields = ("max_model_retries", "retry_backoff_seconds", "model_reconnect_seconds")
    changed_recovery = {
        name: {"source": getattr(old_limits, name, 0), "requested": getattr(requested_limits, name, 0)}
        for name in recovery_fields
        if getattr(requested_limits, name, 0) != getattr(old_limits, name, 0)}
    if changed_recovery and not allow_recovery_policy_change:
        raise ValueError("continuation must preserve the original retry and protection policy")
    allowance_fields = {"model_calls", "tool_calls", "seconds", "tokens", "money_usd", "money_cny"}
    for name in set(type(requested_limits).model_fields) - allowance_fields - set(recovery_fields):
        if getattr(requested_limits, name) != getattr(old_limits, name):
            raise ValueError("continuation must preserve the original retry and protection policy")
    truncations = sum(event.payload.event_type == "response_truncation" for event in events)
    values["max_total_truncations"] = max(0, old_limits.max_total_truncations - truncations)
    effective = RunLimits.model_validate(values)
    # A new execution epoch is not a renewed allowance for network recovery.
    # The terminal journal summary, not an editable convenience receipt, binds
    # cumulative spent recovery time. Older journals predate this field.
    recovery = durable.get("network_recovery") or {}
    if stop.payload.reason == "model_reconnect_budget_exhausted" and not recovery:
        raise ValueError("reconnect exhaustion lacks durable recovery accounting")
    if (receipt.get("network_recovery") or {}) != recovery:
        raise ValueError("source recovery accounting differs from its durable terminal summary")
    reconnect_consumed = recovery.get("consumed_seconds", 0)
    if (type(reconnect_consumed) not in {int, float} or not math.isfinite(reconnect_consumed)
            or reconnect_consumed < 0):
        raise ValueError("source recovery consumption must be finite and nonnegative")
    summary = {
        "schema_version": "runtime-continuation.v1", "source_run": str(source.directory),
        "source_run_id": source.run_id, "source_events_sha256": digest,
        "source_terminal_event_id": stop.event_id, "source_terminal_reason": stop.payload.reason,
        "source_versions": source.verified_versions(),
        "source_limits": old_limits.model_dump(mode="json"),
        "requested_limits": requested_limits.model_dump(mode="json"),
        "effective_limits": effective.model_dump(mode="json"),
        "source_request_count": len(requests), "source_tool_invocation_count": len(invoked),
        "source_elapsed_seconds": elapsed,
        "source_truncation_count": truncations,
        "source_ledger": ledger.model_dump(mode="json"),
        "recovery_policy_change_authorized": bool(allow_recovery_policy_change),
        "recovery_policy_changes": changed_recovery,
        "reconnect_consumed_before_seconds": reconnect_consumed,
        "new_run_is_explicit_continuation": True, "replays_failed_request": False,
        "time_policy": ("New execution epoch; subtract prior active wall duration only; summed request time is observational. Offline repair downtime is not execution time."
                        if old_limits.time_budget_mode == "wall" else
                        "New execution epoch; subtract prior active wall duration and shared request-time charges. Offline repair downtime is not execution time."),
        "usage_policy": "Original receipts and unknown holds remain in source_ledger; the new ledger is limited to remaining capacity, never a replacement of original billing evidence.",
    }
    source.verify_unchanged()
    return effective, summary


def claim_continuation(source: ReadOnlyRun, destination):
    """A terminal allowance can fund one continuation, not concurrent forks.

    The claim lives beside runs, never inside the immutable source. It is made
    only after successful import/preflight and immediately before new work.
    Reopening the exact destination is allowed; a second destination is not.
    """
    destination = Path(destination).resolve()
    if destination == source.directory or destination.is_relative_to(source.directory):
        raise ValueError("continuation claim needs a separate destination")
    directory = source.directory.parent / ".continuation-claims"
    directory.mkdir(exist_ok=True)
    digest = source.evidence_hashes["events.jsonl"]
    path = directory / (digest + ".json")
    body = {"source_run": str(source.directory), "source_events_sha256": digest,
            "destination_run": str(destination), "purpose": "single explicit continuation allowance"}
    from .store import json_bytes
    raw = json_bytes(body)
    try:
        with path.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_bytes() != raw:
            raise ValueError("source allowance already belongs to another continuation; do not fork its budget") from None
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), **body}
