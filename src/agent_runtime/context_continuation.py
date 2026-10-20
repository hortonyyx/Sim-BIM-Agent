"""Exact context import into an explicitly selected new execution.

This is separate from same-version checkpoint recovery.
Call ImportedContext(source).initialize(engine) immediately before the fresh
runtime's first checkpoint, after run([]) has emitted its new start event.
Budgets, timing, retry policy, tools and authorization remain the caller's job.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from pathlib import Path
from urllib.parse import quote

from pydantic import TypeAdapter

from src.agent_runtime.context import ContextManager
from src.agent_runtime.continuation import ReadOnlyRun
from src.agent_runtime.store import EventStore, json_bytes
from src.harness_contracts import (
    ContextEventPayload,
    ExternalCoordinatorMcpPayload,
    HashedBlobRef,
    SourceRef,
)
from src.harness_contracts.events import CapturedValue


def _refs(value):
    """Find explicit hashed references, never interpret arbitrary strings."""
    if isinstance(value, dict):
        if value.get("kind") == "sha256" and {"uri", "sha256", "media_type"} <= value.keys():
            yield HashedBlobRef.model_validate(value)
        else:
            for item in value.values():
                yield from _refs(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _refs(item)


class ImportedContext:
    """Validated, immutable source checkpoint; initialize only one fresh engine."""

    def __init__(self, source: ReadOnlyRun):
        if not isinstance(source, ReadOnlyRun):
            raise TypeError("source must be ReadOnlyRun, never a writable EventStore")
        self.source = source
        found = EventStore.latest_checkpoint(source)
        if found is None:
            raise ValueError("continuation source has no durable checkpoint")
        self.checkpoint_ref, saved, self.checkpoint_sequence = found
        self.saved = copy.deepcopy(saved)
        if not saved.get("context"):
            raise ValueError("exact continuation requires a saved ContextManager")
        for key in ("pending_response_id", "pending_picture_events", "completion",
                    "answer_repair_request_id", "answer_repair_response_id",
                    "answer_repair_original", "answer_repair_error"):
            if saved.get(key):
                raise ValueError(f"cannot import unresolved checkpoint field: {key}")
        if saved.get("terminal_reason") or saved.get("answer"):
            raise ValueError("cannot import terminal or completed context as fresh work")
        pictures = saved.get("pending_pictures")
        if pictures:
            pending = (json.loads(source.get_bytes(HashedBlobRef.model_validate(pictures)))
                       if pictures.get("kind") == "sha256"
                       else source.resolve(TypeAdapter(CapturedValue).validate_python(pictures)))
            if pending:
                raise ValueError("cannot import undelivered pending pictures")
        self.events_by_id = {e.event_id: e for e in source.all_events}
        suffix = [e for e in source.events if e.sequence > self.checkpoint_sequence]
        if any(e.payload.event_type in {"model_response", "tool_invocation",
                                       "tool_execution", "tool_presentation", "context",
                                       "task_control"} for e in suffix):
            raise ValueError("source has accepted work after checkpoint; reconcile first")
        requests = {e.event_id for e in source.all_events if e.payload.event_type == "adapter_request"}
        responses = {e.payload.request_event_id for e in source.all_events
                     if e.payload.event_type == "model_response"}
        failures = {e.payload.model_failure.request_event_id for e in source.all_events
                    if e.payload.event_type == "run_lifecycle" and e.payload.model_failure is not None}
        if requests - responses - failures:
            raise ValueError("source has an unresolved model request")
        for key in ("redispatch_of", "retry_of"):
            previous_intent = saved.get(key)
            if previous_intent and (previous_intent not in failures or previous_intent in responses):
                raise ValueError("cannot discard unresolved or accepted prior request intent: " + key)
        invocations = {e.event_id for e in source.all_events
                       if e.payload.event_type == "tool_invocation"}
        executions = {e.payload.invocation_event_id for e in source.all_events
                      if e.payload.event_type == "tool_execution"}
        if invocations - executions or any(
            e.payload.event_type == "tool_execution" and e.payload.outcome == "unknown"
            for e in source.all_events
        ):
            raise ValueError("source has unresolved tool effects")
        # Known rejected/partial terminal-tool attempts are ordinary completed
        # interactions and must remain in history. Actual completion is blocked
        # by the saved completion/answer fields; unknown effects are rejected
        # above. Imported tool IDs prevent replaying any prior attempt.
        stops = [e for e in source.events
                 if e.payload.event_type == "run_lifecycle" and e.payload.action == "stop"]
        if not stops or stops[-1].payload.reason not in {
                "service_error", "service_unavailable", "transport_error", "timeout", "cancelled",
                "model_retries_exhausted:transport_error", "model_retries_exhausted:timeout",
                "model_retries_exhausted:service_unavailable", "model_reconnect_budget_exhausted"}:
            raise ValueError("context continuation requires an admitted service-failure stop")
        last_response = next((event for event in reversed(source.events)
            if event.payload.event_type == "model_response"), None)
        if last_response and any(event.payload.event_type == "response_truncation"
                and event.payload.response_event_id == last_response.event_id for event in source.events):
            # A new journal would reset the consecutive-truncation counter.
            # Until that counter has an import contract, keep this boundary on
            # strict same-run recovery rather than renewing protection.
            raise ValueError("source is still recovering a truncated response; use same-version resume")
        context = saved["context"]
        self.original_context = ContextManager.load(source, context)
        self.messages, self.sources = self.original_context.full_history()
        self.image_originals = {
            k: HashedBlobRef.model_validate(v) for k, v in saved["image_originals"].items()
        }
        for ref in self.image_originals.values():
            source.get_bytes(ref)
        self.old_used_ids = set(saved["used_ids"])
        if len(self.old_used_ids) != len(saved["used_ids"]):
            raise ValueError("source has duplicate used tool call IDs")
        call_ids = [call["id"] for message in self.messages for call in message.get("tool_calls", [])]
        result_ids = [message["tool_call_id"] for message in self.messages if message.get("role") == "tool"]
        if (Counter(call_ids) != Counter(result_ids)
                or any(n != 1 for n in Counter(call_ids).values())
                or set(call_ids) != self.old_used_ids):
            raise ValueError("source history has unmatched, duplicate or unaccounted tool interactions")
        self.counts = {
            "history": len(self.original_context.history),
            "history_kinds": dict(Counter(r.kind for r in self.original_context.history)),
            "images": len(self.original_context.images),
            "active_images": sum(i.active for i in self.original_context.images),
            "state": len(self.original_context.state),
            "image_originals": len(self.image_originals),
            "retrieval_pins": len(context["retrieval_pins"]),
            "initial_tagged_history": sum(
                "runtime-initial-input" in r.tags for r in self.original_context.history
            ),
            "old_used_ids": len(self.old_used_ids),
        }
        self.initialized = False
        self.new_context = None
        self.manifest = None
        self.source.verify_unchanged()

    def initialize(self, engine):
        """Install exact history before a fresh runtime's initial checkpoint.

        No old counters, failed requests, retry identifiers, elapsed time or
        terminal state are imported. The caller conserves the old budget in the
        independently validated new limits. This callback performs no model call.
        """
        if self.initialized:
            if self.new_context is engine.context:
                return self.new_context
            raise ValueError("context import may initialize only one engine")
        store = engine.store
        if not isinstance(store, EventStore):
            raise TypeError("destination must be EventStore")
        destination = store.directory.resolve()
        if destination == self.source.directory or destination.is_relative_to(self.source.directory):
            raise ValueError("context import destination must be outside immutable source")
        events = store.events
        if len(events) != 1 or events[0].payload.event_type != "run_lifecycle" or events[0].payload.action != "start":
            raise ValueError("initialize only after fresh run start and before first checkpoint")
        if engine.messages or engine.sources or (engine.context and engine.context.history):
            raise ValueError("fresh runtime must run([]); do not retag historical messages as initial input")
        if engine.context_policy != self.original_context.policy:
            raise ValueError("context projection policy must remain exactly unchanged")
        if engine.model != self.saved["config"]["model"]:
            raise ValueError("context usage calibration requires the original parent model")
        self.source.verify_unchanged()
        copied = {}

        def copy_blob(ref):
            ref = HashedBlobRef.model_validate(ref) if isinstance(ref, dict) else ref
            key = (ref.uri, ref.sha256, ref.media_type)
            if key in copied:
                return copied[key]
            raw = self.source.get_bytes(ref)
            new = store.put_bytes(raw, ref.media_type)
            if new.sha256 != ref.sha256 or store.get_bytes(new) != raw:
                raise ValueError("copied attachment differs from immutable source")
            # All product blob references are content-addressed with this URI.
            # Reject relocation rather than mutating exact historical JSON bytes.
            if new.uri != ref.uri:
                raise ValueError("source blob URI is not portable content-addressed storage")
            copied[key] = new
            if "json" in ref.media_type:
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    value = None
                for child in _refs(value):
                    copy_blob(child)
            return new

        for ref in _refs(self.saved["context"]):
            copy_blob(ref)
        for ref in self.image_originals.values():
            copy_blob(ref)
        # Keep the full original checkpoint and journal as external evidence;
        # importing them as blobs does not replay their events in the new run.
        checkpoint = copy_blob(self.checkpoint_ref)
        journal = store.put_bytes(self.source.read_bytes("events.jsonl"), "application/x-ndjson")
        if journal.sha256 != self.source.evidence_hashes["events.jsonl"]:
            raise ValueError("source journal hash changed")
        source_locator = (
            f"continuation-source:{quote(self.source.run_id, safe='')}"
            f"@sha256:{journal.sha256}"
        )
        # Runtime's estimator calibrates opaque Responses reasoning from actual
        # parent receipts. A new journal otherwise loses this read-only evidence
        # and can trigger unnecessary compaction before the first new request.
        # This imports estimates only: never billing, counts or failed usage.
        from src.agent_runtime.responses import ResponsesUsageCalibration
        calibration = ResponsesUsageCalibration(engine.model)
        parent_requests = {}
        observed = []
        ignored = []
        for event in self.source.events:  # Role-local, deliberately not all_events.
            event_payload = event.payload
            if event_payload.event_type == "adapter_request":
                parent_requests[event.event_id] = event_payload.final_request_body
            elif event_payload.event_type == "model_response":
                capture = parent_requests.get(event_payload.request_event_id)
                if capture is None:
                    raise ValueError("parent response lacks its original request for calibration")
                body = self.source.resolve(capture)
                raw = self.source.resolve(event_payload.raw_response)
                actual = (raw.get("usage") or {}).get("input_tokens")
                if body.get("model") != engine.model:
                    raise ValueError("parent calibration response uses another model")
                if type(actual) is not int or actual < 0:
                    ignored.append(event.event_id)  # Unknown is never replaced by zero.
                    continue
                for ref in _refs(capture.model_dump(mode="json")):
                    copy_blob(ref)
                for ref in _refs(event_payload.raw_response.model_dump(mode="json")):
                    copy_blob(ref)
                external_id = f"{source_locator}/event/{event.event_id}"
                calibration.observe(body, raw, response_event_id=external_id)
                observed.append({
                    "source_request_event_id": event_payload.request_event_id,
                    "source_response_event_id": event.event_id,
                    "calibration_response_event_id": external_id,
                    "actual_input_tokens": actual,
                })
        calibration_record = {
            "scope": "parent Responses usage estimates only; no budget or request replay",
            "model": engine.model,
            "source_response_records": observed,
            "ignored_missing_input_usage_response_ids": ignored,
            "anchor_count": len(calibration.anchors),
            "largest_growth": calibration.largest_growth,
            "new_journal_cursor": 0,
            "new_journal_requests": {},
        }
        import_record = {
            "kind": "exact_external_context_import",
            "source_run": str(self.source.directory),
            "source_run_id": self.source.run_id,
            "source_task_id": self.source.task_id,
            "source_journal": journal.model_dump(mode="json"),
            "source_checkpoint": checkpoint.model_dump(mode="json"),
            "source_checkpoint_sequence": self.checkpoint_sequence,
            "counts": self.counts,
            "usage_calibration": calibration_record,
            "preserves": "Exact message bytes including reasoning; history kinds/tags/order, image identities/status/pins, state values/revisions, used call IDs.",
            "excludes": "No independent evaluation, authored summary, old billing/reset, pending response replay, terminal tool replay or fresh model work.",
        }
        manifest_blob = store.put_json(import_record)
        evidence = SourceRef(source_id="continuation-source-checkpoint", source_kind="history",
                             locator=source_locator, blob=manifest_blob)
        anchor = store.append(
            ExternalCoordinatorMcpPayload(
                phase="operation", coordinator_task_id="operator-continuation-import",
                method="continuation.import_context",
                request_content=store.capture({"source_run_id": self.source.run_id,
                                               "checkpoint_sha256": checkpoint.sha256}),
                result_content=store.capture(import_record),
            ),
            source_refs=(evidence,),
        )

        def rebind(value):
            original = SourceRef.model_validate(value)
            old_id = original.event_id
            if old_id is not None and old_id not in self.events_by_id:
                raise ValueError("context source refers to an unknown original event")
            blob = original.blob
            if blob is not None:
                if not isinstance(blob, HashedBlobRef):
                    raise ValueError("exact import requires hashed source attachments")
                blob = copy_blob(blob)
            elif old_id is not None:
                # A pure event source gets immutable event bytes as well as
                # the namespace locator; it remains independently resolvable.
                event_document = self.events_by_id[old_id].model_dump(mode="json")
                for ref in _refs(event_document):
                    copy_blob(ref)
                blob = store.put_bytes(json_bytes(event_document), "application/json")
            rebound = original.model_dump(mode="json")
            rebound.update(
                locator=f"{source_locator}/event/{quote(old_id or 'none', safe='')}?locator={quote(original.locator, safe='')}",
                blob=blob.model_dump(mode="json") if blob is not None else None,
                event_id=anchor.event_id if old_id is not None else None,
            )
            return SourceRef.model_validate(rebound).model_dump(mode="json")

        payload = copy.deepcopy(self.saved["context"])
        for row in payload["history"]:
            row["source"] = rebind(row["source"])
        for row in payload["state"]:
            row["source_refs"] = [rebind(ref) for ref in row["source_refs"]]
        # A compacted context remains compacted, with its exact summary and
        # history selection. Rebind the durable compaction event; do not ask a
        # model to summarize it again or silently reactivate archived history.
        compact = payload.get("last_compaction")
        if compact:
            original_event = self.events_by_id[compact["event_id"]]
            original_payload = original_event.payload
            for ref in _refs(original_payload.model_dump(mode="json")):
                copy_blob(ref)
            imported = store.append(original_payload, source_refs=(evidence,))
            compact["event_id"] = imported.event_id
            compact["source"] = rebind(compact["source"])
            compact["source"]["event_id"] = imported.event_id
        removal_map = {}
        for row in payload["images"]:
            row["source"] = rebind(row["source"])
            old_id = row.get("removal_event_id")
            if not row["active"]:
                old_event = self.events_by_id[old_id]
                original_payload = old_event.payload
                if (original_payload.event_type != "context"
                        or original_payload.action != "remove_image"
                        or original_payload.image.model_dump(mode="json") != row["image"]):
                    raise ValueError("inactive source image lacks exact recorded removal")
                if old_id not in removal_map:
                    for ref in _refs(original_payload.model_dump(mode="json")):
                        copy_blob(ref)
                    details = store.put_json({
                        "kind": "imported_image_removal_state",
                        "source_locator": f"{source_locator}/event/{old_id}",
                        "original_event": old_event.model_dump(mode="json"),
                        "context_import": manifest_blob.model_dump(mode="json"),
                        "meaning": "Preserved prior inactive state; no new visibility judgement.",
                    })
                    imported = store.append(ContextEventPayload(
                        action="remove_image",
                        reason="exact continuation import preserves original image inactive state",
                        image=HashedBlobRef.model_validate(row["image"]), view_id=row["view_id"],
                        before=original_payload.before, after=original_payload.after, details=details,
                    ), source_refs=(evidence, SourceRef.model_validate(row["source"])))
                    removal_map[old_id] = imported.event_id
                row["removal_event_id"] = removal_map[old_id]
        context = ContextManager.load(store, payload, policy=engine.context_policy)
        messages, sources = context.full_history()
        if json_bytes(messages) != json_bytes(self.messages):
            raise ValueError("exact imported history changed content, images or reasoning")
        if [r.kind for r in context.history] != [r.kind for r in self.original_context.history]:
            raise ValueError("history kinds changed during import")
        if [r.tags for r in context.history] != [r.tags for r in self.original_context.history]:
            raise ValueError("history tags changed during import")
        if context.dump()["retrieval_pins"] != self.saved["context"]["retrieval_pins"]:
            raise ValueError("image retrieval pins changed during import")
        engine.context = context
        engine.messages, engine.sources = messages, sources
        engine.originals = dict(self.image_originals)
        engine.used_ids = set(self.old_used_ids)
        engine.incomplete_response_count = self.saved.get("incomplete_response_count", 0)
        engine._context_calibration = calibration
        engine._context_calibration_cursor = 0
        engine._context_calibration_requests = {}
        self.new_context = context
        self.initialized = True
        self.manifest = {
            **import_record,
            "import_event_id": anchor.event_id,
            "manifest_blob": manifest_blob.model_dump(mode="json"),
            "copied_blob_count": len(copied),
            "copied_blob_unique_sha_count": len({r.sha256 for r in copied.values()}),
            "image_removal_event_map": removal_map,
            "source_evidence_hashes": self.source.evidence_hashes,
        }
        self.source.verify_unchanged()
        return context
