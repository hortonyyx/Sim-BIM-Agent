"""Read-only projection of bounded transport recovery, including crash holds."""

from __future__ import annotations

import json


def recovery_summary(store, *, limit_seconds, inherited_seconds=0.0):
    """Reservations from every task share the root's cumulative allowance."""
    operations = {}
    for event in store.all_events:
        p = event.payload
        if event.task_id == store.root_task_id and p.event_type == "run_lifecycle" and p.action == "start":
            ref = next((r.blob for r in event.source_refs if r.source_id == "runtime-config"), None)
            if ref is not None:
                config = json.loads(store.get_bytes(ref))
                inherited_seconds = config.get("reconnect_consumed_before_seconds", inherited_seconds)
                limit_seconds = config["limits"].get("model_reconnect_seconds", limit_seconds)
        if p.event_type != "network_recovery":
            continue
        if p.action == "reserve":
            if p.operation_id in operations:
                raise ValueError("duplicate network recovery reservation")
            operations[p.operation_id] = (event.task_id, p, None)
        else:
            prior = operations.get(p.operation_id)
            if (prior is None or prior[2] is not None or prior[0] != event.task_id
                    or (prior[1].phase, prior[1].request_event_id, prior[1].reserved_seconds)
                    != (p.phase, p.request_event_id, p.reserved_seconds)):
                raise ValueError("network recovery settlement has no matching reservation")
            operations[p.operation_id] = (prior[0], prior[1], p)
    charged = observed = backoff = requests = pending = 0.0
    for _, reserve, settle in operations.values():
        amount = reserve.reserved_seconds if settle is None else settle.elapsed_seconds
        charged += amount
        if settle is None:
            pending += amount
        else:
            observed += amount
            if reserve.phase == "backoff":
                backoff += amount
            else:
                requests += amount
    consumed = inherited_seconds + charged
    return {
        "limit_seconds": float(limit_seconds), "inherited_seconds": float(inherited_seconds),
        "consumed_seconds": consumed, "remaining_seconds": max(0.0, limit_seconds - consumed),
        "observed_seconds": observed, "pending_reserved_seconds": pending,
        "backoff_seconds": backoff, "reconnect_request_wait_seconds": requests,
        "scope": "all tasks in this run plus conserved source-run recovery consumption",
        "basis": ("Backoff plus token/response-header acquisition during transport recovery; "
                  "adapters without header callbacks charge the bounded complete retry request. "
                  "Established Responses stream generation is not charged as reconnect waiting. "
                  "Unsettled reservations remain fully charged; these are conservative holds, not measured downtime."),
    }


def request_outcome_timing(store, *, task_id, elapsed_seconds):
    successful = failed = backoff = 0.0
    success_count = failure_count = 0
    durations, failures = {}, set()
    for event in store.all_events:
        if event.task_id != task_id:
            continue
        p = event.payload
        if p.event_type == "network_recovery" and p.action == "settle" and p.phase == "backoff":
            backoff += p.elapsed_seconds
        failure = p.model_failure if p.event_type == "run_lifecycle" else None
        if p.event_type != "model_response" and failure is None:
            continue
        request_id = failure.request_event_id if failure else p.request_event_id
        if failure:
            failures.add(request_id)
        ref = next((r.blob for r in event.source_refs if r.source_id == "request-duration"), None)
        if ref is not None:
            durations[request_id] = json.loads(store.get_bytes(ref))["elapsed_seconds"]
    for request_id, seconds in durations.items():
        if request_id in failures:
            failed += seconds
            failure_count += 1
        else:
            successful += seconds
            success_count += 1
    return {
        "successful_request_seconds": successful, "failed_request_seconds": failed,
        "backoff_seconds": backoff, "successful_requests_measured": success_count,
        "failed_requests_measured": failure_count,
        "execution_excluding_failed_requests_and_backoff_seconds": max(0.0, elapsed_seconds - failed - backoff),
        "other_execution_seconds": max(0.0, elapsed_seconds - successful - failed - backoff),
        "scope": "current task wall time; child work may occur inside parent tool waits",
        "note": ("Failed streaming requests may include useful model generation before interruption. "
                 "Failed request time and response-header wait are not proven network downtime. "
                 "A received response is counted once; a later failure classification takes precedence. "
                 "Other execution includes tools, preparation, logging and unmeasured spans. "
                 "Do not add reconnect header time to failed/successful requests; it is a subset."),
    }
