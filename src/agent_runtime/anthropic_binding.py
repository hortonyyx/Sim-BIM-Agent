"""Project bound thinking only when its recorded native prefix is unchanged.

This is an opt-in Go/Claude wire conversion. Durable responses and logical
history are never rewritten. A new EventStore reconstructs all bindings from
the request/response journal, including after recovery.
"""
from __future__ import annotations

import copy
import hashlib

from .store import json_bytes

_THINKING = frozenset({"thinking", "redacted_thinking"})


def _block_value(block):
    # Cache breakpoints are request metadata, not conversation content. Do not
    # recursively remove this key from user tool arguments or JSON payloads.
    value = copy.deepcopy(block)
    value.pop("cache_control", None)
    if value.get("type") == "tool_result" and isinstance(value.get("content"), list):
        value["content"] = [_block_value(child) for child in value["content"]]
    return value


def _prefix_hash(body, messages):
    value = {"model": body["model"], "system": [_block_value(b) for b in body.get("system", [])],
             "tools": [{k: v for k, v in tool.items() if k != "cache_control"}
                       for tool in body.get("tools", [])],
             "messages": [{"role": m["role"], "content": [_block_value(b) for b in m["content"]]}
                          for m in messages]}
    return hashlib.sha256(json_bytes(value)).hexdigest()


def project_bound_thinking(store, body, sources):
    """Return native block sources and an optional recorded conversion audit.

    An immutable model_response links each signature to the actual request
    that produced it. Unknown origins fail closed by omitting entire thinking
    blocks. Corrupt archived evidence still raises before any network request.
    Removing an earlier thinking block may invalidate later bindings too.
    """
    if not any(b.get("type") in _THINKING for m in body["messages"] for b in m["content"]):
        return sources, None
    events = {event.event_id: event for event in store.all_events}
    projected, projected_sources, changes = [], [], []
    for index, (message, origins) in enumerate(zip(body["messages"], sources, strict=True)):
        blocks = message["content"]
        thinking = [i for i, b in enumerate(blocks) if b.get("type") in _THINKING]
        reason, expected, current = None, None, None
        response_event, request_event = None, None
        if message["role"] == "assistant" and thinking:
            ids = {origins[i].event_id for i in thinking}
            response_event = events.get(next(iter(ids))) if len(ids) == 1 else None
            if (response_event is None or response_event.payload.event_type != "model_response"
                    or response_event.task_id != store.task_id):
                reason = "unverified_response_origin"
            else:
                request_event = events.get(response_event.payload.request_event_id)
                if (request_event is None or request_event.payload.event_type != "adapter_request"
                        or request_event.task_id != store.task_id
                        or request_event.payload.adapter != "anthropic-messages-http-v1"):
                    reason = "unverified_request_origin"
                else:
                    original = store.resolve(request_event.payload.final_request_body)
                    expected = _prefix_hash(original, original["messages"])
                    current = _prefix_hash(body, projected)
                    original_response = store.resolve(response_event.payload.raw_response)
                    original_thinking = [_block_value(b) for b in original_response["content"]
                                         if b.get("type") in _THINKING]
                    if [_block_value(blocks[i]) for i in thinking] != original_thinking:
                        reason = "thinking_content_changed"
                    elif expected != current:
                        reason = "conversation_prefix_changed"
            if reason:
                changes.append({"message_index": index, "reason": reason,
                    "response_event_id": response_event.event_id if response_event else None,
                    "request_event_id": request_event.event_id if request_event else None,
                    "expected_prefix_sha256": expected, "current_prefix_sha256": current,
                    "removed_blocks": [{"index": i, "type": blocks[i]["type"],
                        "sha256": hashlib.sha256(json_bytes(blocks[i])).hexdigest()} for i in thinking],
                    "original_sources": [origins[i].model_dump(mode="json") for i in thinking]})
                keep = [i for i in range(len(blocks)) if i not in thinking]
                message = {**message, "content": [blocks[i] for i in keep]}
                origins = [origins[i] for i in keep]
        if not message["content"]:
            continue
        if projected and message["role"] == "user" and projected[-1]["role"] == "user":
            projected[-1]["content"].extend(message["content"])
            projected_sources[-1].extend(origins)
        else:
            projected.append(copy.deepcopy(message))
            projected_sources.append(list(origins))
    body["messages"] = projected
    if not changes:
        return projected_sources, None
    audit = store.source("go-messages-thinking-binding", {
        "policy": "omit_invalid_bound_thinking_v1", "durable_history_unchanged": True,
        "changes": changes}, kind="generated")
    return projected_sources, audit
