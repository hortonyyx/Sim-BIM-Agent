"""Per-role accounting is a projection of the same complete root journal."""

from __future__ import annotations

import json

from src.agent_runtime.accounting import request_accounting_from_store, summarize_request_accounting
from src.agent_runtime.budget import RuntimeBudget


def role_accounting(store, registry):
    roles = {store.task_id: "coordinator"}
    for path in (store.directory / "tasks").glob("*/reader_task.json"):
        task = json.loads(path.read_bytes())
        roles[task["task_id"]] = task["role_id"]
    records = [request_accounting_from_store(store, event.event_id)
               for event in store.all_events if event.payload.event_type == "adapter_request"]
    total = summarize_request_accounting(records)
    total["by_role"] = {}
    for role in ("coordinator", "plan_reader", "elevation_reader"):
        events = [event for event in store.all_events if roles.get(event.task_id) == role]
        summary = summarize_request_accounting(row for row in records if roles.get(row.task_id) == role)
        seconds = [float(event.payload.settlement.actual.seconds) for event in events
                   if event.payload.event_type == "budget" and event.payload.action == "settle"
                   and event.payload.settlement.actual.seconds is not None]
        repair = [event.payload for event in events if event.payload.event_type == "answer_repair"]
        deliveries = [row for row in registry.records.values() if row["role_id"] == role]
        failed_tasks = {event.task_id for event in events if event.payload.event_type == "answer_repair"}
        submissions = [event for event in events if event.payload.event_type == "tool_execution"
                       and event.payload.tool_name in {"submit_plan_reading", "submit_elevation_reading"}]
        rejected = [event for event in submissions if event.payload.outcome != "succeeded"]
        failed_tasks.update(event.task_id for event in rejected)
        completed = [row for row in deliveries if row["status"] == "completed"]
        failures = [event.payload.model_failure for event in events
                    if event.payload.event_type == "run_lifecycle" and event.payload.model_failure]
        # Receipts include tools, validation, restart downtime and service failures.
        # Summed concurrent task time is distinct from elapsed wall span.
        receipts = []
        for task_id, task_role in roles.items():
            if task_role != role:
                continue
            path = (store.directory if task_id == store.task_id else registry.child(task_id).task_directory) / "receipt.json"
            if path.is_file():
                receipts.append({**json.loads(path.read_bytes()), "task_id": task_id})
        intervals = [(receipt["started_epoch"], receipt["started_epoch"] + receipt["elapsed_seconds"])
                     for receipt in receipts if receipt.get("started_epoch") is not None and receipt.get("elapsed_seconds") is not None]
        repair_requests = sum(row.phase == "request" for row in repair)
        repair_results = [row for row in repair if row.phase == "result"]
        repair_failures = sum(row.accepted is False for row in repair_results)
        summary.update({"request_seconds": sum(seconds),
                        "request_duration_complete": len(seconds) == summary["requests"],
                        "task_elapsed_seconds_sum": sum(end - start for start, end in intervals),
                        "task_wall_span_seconds": max(end for _, end in intervals) - min(start for start, _ in intervals) if intervals else None,
                        "tool_calls": sum(event.payload.event_type == "tool_invocation" for event in events),
                        "format_errors": repair_requests + repair_failures,
                        "submission_attempts": len(submissions),
                        "submission_rejections": len(rejected),
                        "submission_repaired_tasks": sum(row["task_id"] in {event.task_id for event in rejected}
                                                         for row in completed),
                        "repair_requests": repair_requests,
                        "repair_failures": repair_failures,
                        "repair_incomplete": repair_requests - len(repair_results),
                        "repair_successes": sum(row.phase == "result" and row.accepted for row in repair),
                        "delivered_tasks": len(deliveries), "validated_tasks": len(completed),
                        "first_pass_tasks": sum(row["task_id"] not in failed_tasks for row in completed),
                        "first_pass_rate": (sum(row["task_id"] not in failed_tasks for row in completed) / len(deliveries)
                                            if deliveries else None),
                        "after_repair_rate": len(completed) / len(deliveries) if deliveries else None,
                        "failures": [{"category": row.category, "retryable": row.retryable,
                                      "http_status": row.http_status} for row in failures],
                        "delivery_failures": [{"task_id": row["task_id"], "reason": row["reason"]}
                                              for row in deliveries if row["status"] == "failed"]})
        total["by_role"][role] = summary
        if role == "plan_reader":
            summary["before_first_trial"] = [
                {"task_id": receipt["task_id"], **receipt["plan_reader_progress"]}
                for receipt in receipts if "plan_reader_progress" in receipt]
    total["ledger"] = RuntimeBudget.from_events(store.budget_limit, store.all_events).ledger.model_dump(mode="json")
    total["timing_note"] = ("Request seconds sum settled observed durations, including service failures. "
        "Unknown/late unsettled durations are marked incomplete; task elapsed includes tools and restart downtime. "
        "Concurrent task elapsed sums and wall spans are separate; root receipt gives full-run wall time.")
    return total
