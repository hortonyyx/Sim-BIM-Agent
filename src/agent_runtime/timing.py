"""Task timing projected from durable runtime events, without domain imports."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _epoch(event):
    return event.occurred_at.value.timestamp() if event.occurred_at.kind == "known" else None


def _source(store, event, name):
    ref = next((s.blob for s in event.source_refs if s.source_id == name), None)
    return json.loads(store.get_bytes(ref)) if ref else None


def _union(intervals):
    merged = []
    for start, end in sorted((a, b) for a, b in intervals if b >= a):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _seconds(intervals):
    return sum(b - a for a, b in _union(intervals))


def summarize_timing(store, *, current_task=None, started_epoch=None, ended_epoch=None,
                     status=None, pending_queue=None):
    """Wall spans and parallel work sums are deliberately separate.

    Queue durations are measured before sending. Model durations use monotonic
    observations, or historical settled observations. Tool spans in older logs
    use invocation/execution timestamps and explicitly include local overhead.
    Parent wait is the UNION of descendant lifetimes intersected with its tool
    spans; setup/teardown outside those lifetimes remains in tool time.
    """
    events = store.all_events
    tasks, tool_spans = {}, {}
    for task_id in dict.fromkeys(e.task_id for e in events):
        selected = [e for e in events if e.task_id == task_id]
        starts = [e for e in selected if e.payload.event_type == "run_lifecycle" and e.payload.action == "start"]
        stops = [e for e in selected if e.payload.event_type == "run_lifecycle" and e.payload.action == "stop"]
        receipt = _source(store, stops[-1], "run-receipt") if stops else None
        begin = _source(store, starts[0], "task-timing") if starts else None
        start = ((receipt or {}).get("started_epoch") or (begin or {}).get("started_epoch")
                 or (_epoch(starts[0]) if starts else _epoch(selected[0])))
        end = (receipt["started_epoch"] + receipt["elapsed_seconds"]
               if receipt and "started_epoch" in receipt and "elapsed_seconds" in receipt
               else _epoch(stops[-1]) if stops else None)
        task_status = stops[-1].payload.reason if stops else "running_or_interrupted"
        if task_id == current_task:
            start, end, task_status = started_epoch, ended_epoch, status
        requests = [e for e in selected if e.payload.event_type == "adapter_request"]
        durations = {}
        settlements = {}
        queues, queue_known = [], set()
        adjustments = []
        for event in selected:
            p = event.payload
            failure = p.model_failure if p.event_type == "run_lifecycle" else None
            identity = p.request_event_id if p.event_type == "model_response" else failure.request_event_id if failure else None
            duration = _source(store, event, "request-duration") if identity else None
            if duration is not None:
                durations[identity] = duration["elapsed_seconds"]
            if p.event_type == "budget" and p.action == "settle":
                settlements[p.settlement.reservation_id] = p.settlement.actual.seconds
            queue = _source(store, event, "request-dispatch")
            if queue is not None:
                if queue.get("outcome") != "not_sent" or queue not in queues:
                    queues.append(queue)
                if p.event_type == "adapter_request":
                    queue_known.add(event.event_id)
            adjustment = _source(store, event, "dispatch-adjustment")
            if adjustment:
                adjustments.append({**adjustment, "event_id": event.event_id})
        if task_id == current_task and pending_queue and pending_queue not in queues:
            queues.append(pending_queue)
        for request in requests:
            settled = settlements.get(request.payload.reservation_id)
            if request.event_id not in durations and settled is not None:
                durations[request.event_id] = float(settled)
        invocations = {e.event_id: e for e in selected if e.payload.event_type == "tool_invocation"}
        spans, measured_tool_seconds, historical = [], 0.0, False
        completed_tools = set()
        for event in selected:
            p = event.payload
            if p.event_type != "tool_execution" or p.invocation_event_id not in invocations:
                continue
            invocation = invocations[p.invocation_event_id]
            a, b = _epoch(invocation), _epoch(event)
            duration = _source(store, event, "tool-duration")
            if duration is None:
                historical = True
                if p.outcome == "unknown":
                    # A recovered intent may span process downtime. It is not
                    # a measurement of how long the original tool ran.
                    continue
            if a is not None and b is not None:
                # New records exclude snapshot/conversion work from tool time.
                seconds = duration["elapsed_seconds"] if duration is not None else max(0.0, b - a)
                if duration is not None:
                    a = duration.get("started_epoch", a)
                spans.append((a, a + seconds))
                measured_tool_seconds += seconds
                completed_tools.add(invocation.event_id)
        tool_spans[task_id] = spans
        parent = selected[0].parent_task
        tasks[task_id] = {
            "parent_task_id": parent.task_id if parent.kind == "known" else None,
            "started_epoch": start, "ended_epoch": end, "status": task_status,
            "elapsed_seconds": max(0.0, end - start) if start is not None and end is not None else None,
            "requests": len(requests), "model_seconds": sum(durations.values()),
            "model_duration_complete": len(durations) == len(requests),
            "queue_seconds": sum(row["queue_seconds"] for row in queues) if queues or not requests else None,
            "queue_duration_complete": len(queue_known) == len(requests),
            "tool_calls": len(invocations), "tool_inclusive_seconds": measured_tool_seconds,
            "tool_duration_complete": len(completed_tools) == len(invocations),
            "tool_timing_basis": "includes_historical_event_intervals" if historical else "monotonic_observation",
            "dispatch_adjustments": adjustments,
        }
    for task_id, row in tasks.items():
        descendants = []
        for other, child in tasks.items():
            parent, seen = child["parent_task_id"], set()
            while parent in tasks and parent not in seen:
                if parent == task_id:
                    if child["started_epoch"] is not None and child["ended_epoch"] is not None:
                        descendants.append((child["started_epoch"], child["ended_epoch"]))
                    break
                seen.add(parent)
                parent = tasks[parent]["parent_task_id"]
        waiting = [(max(a, c), min(b, d)) for a, b in tool_spans[task_id]
                   for c, d in descendants if max(a, c) < min(b, d)]
        row["child_wait_seconds"] = _seconds(waiting)
        row["tool_seconds"] = max(0.0, row["tool_inclusive_seconds"] - row["child_wait_seconds"])
        row["other_seconds"] = (max(0.0, row["elapsed_seconds"] - row["model_seconds"]
            - (row["queue_seconds"] or 0.0) - row["tool_inclusive_seconds"])
            if row["elapsed_seconds"] is not None else None)
    root_id = store.root_task_id
    children = [(task_id, row) for task_id, row in tasks.items()
                if task_id != root_id and row["ended_epoch"] is not None]
    latest = max(children, key=lambda item: item[1]["ended_epoch"]) if children else None
    root_end = tasks.get(root_id, {}).get("ended_epoch")
    return {
        "schema_version": 1, "root_task_id": root_id, "by_task": tasks,
        "critical_path": {
            "last_child_task_id": latest[0] if latest else None,
            "last_child_ended_epoch": latest[1]["ended_epoch"] if latest else None,
            "root_tail_seconds": max(0.0, root_end - latest[1]["ended_epoch"]) if latest and root_end is not None else None,
            "all_tasks_ended": all(row["ended_epoch"] is not None for row in tasks.values()),
        },
        "note": "Parallel task seconds must not be summed as run wall time. Child wait is the union of child lifetimes overlapping parent tool calls; setup/teardown stays in tool_seconds. Other includes preparation, context, logging, retry backoff, restart downtime and unmeasured interruption spans. Model/tool totals contain known durations only; inspect completeness flags. Missing historical queue measurements are null, not zero.",
    }


def project_saved_run(directory):
    """Read completed evidence without acquiring a writer or modifying it."""
    from .accounting import request_accounting_from_store, summarize_request_accounting
    from .store import EventStore

    class ReadOnlyStore:
        get_bytes = EventStore.get_bytes

    store = ReadOnlyStore()
    store.directory = Path(directory).resolve()
    meta = json.loads((store.directory / "journal.json").read_bytes())
    store.root_task_id = meta["task_id"]
    store.all_events = EventStore.read_events(store.directory / "events.jsonl")
    records = [request_accounting_from_store(store, e.event_id) for e in store.all_events
               if e.payload.event_type == "adapter_request"]
    return {"source": str(store.directory), "events_sha256": hashlib.sha256(
                (store.directory / "events.jsonl").read_bytes()).hexdigest(),
            "timing": summarize_timing(store),
            "usage_accounting": summarize_request_accounting(records),
            "rejected_requests": [r.receipt_dict() for r in records
                if r.accounting.usage_basis == "rejected_before_processing"],
            "note": "Read-only projection under current accounting rules. Original events, receipts and budget ledgers are unchanged; no new model call."}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(project_saved_run(args.directory), ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8", newline="\n")
