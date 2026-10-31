"""Read current EventEnvelope logs and the two historical BIM-agent log forms.

Behaviour metrics mention BIM tool families, facades, claims and saved source
BIM files, so this adapter belongs to the building layer.  It only reads the
domain-neutral runtime event contract; the runtime core never imports it.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
from typing import Any

from src.harness_contracts import EventEnvelope


FACADES = ("north", "south", "east", "west")
FIRST_DRAFT_TOOLS = {"build_plan_bim", "build_bim", "build_parametric_bim"}
from scripts.tool_scripts.bim_agent_saved_result import read_saved_result


def _stamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def tool_result_data(value: Any) -> dict[str, Any]:
    """Read metadata without discarding the original response or its timing tail.

    MCP, provider presentation and CLI text are transport forms of the same
    result. T1 appends a separate time line, so decoding the entire string loses
    otherwise valid JSON. The raw text remains in the complete behaviour record.
    """
    if isinstance(value, str):
        text = value.lstrip()
        try:
            value, _ = json.JSONDecoder().raw_decode(text)
        except ValueError:
            # Claude Code truncated T1 sm25 revise_bim at step 114. Retain only
            # complete leading fields; never treat a partial object as complete.
            # candidate/source_geometry_ready precede the large evidence arrays.
            if not text.startswith("{"):
                return {}
            decoder, fields, tail = json.JSONDecoder(), {}, text[1:].lstrip()
            while tail:
                try:
                    key, end = decoder.raw_decode(tail)
                    if not isinstance(key, str) or not tail[end:].lstrip().startswith(":"):
                        break
                    tail = tail[end:].lstrip()[1:].lstrip()
                    item, end = decoder.raw_decode(tail)
                    fields[key] = item
                    tail = tail[end:].lstrip()
                    if not tail.startswith(","):
                        break
                    tail = tail[1:].lstrip()
                except ValueError:
                    break
            value = dict(fields, _partial_json=True) if fields else {}
    if not isinstance(value, dict):
        return {}
    if isinstance(value.get("structuredContent"), dict):
        return value["structuredContent"]
    if "tool_message" in value:
        return tool_result_data(value["tool_message"].get("content"))
    if isinstance(value.get("content"), list):
        for block in value["content"]:
            if isinstance(block, dict) and block.get("type") == "text":
                data = tool_result_data(block.get("text"))
                if data:
                    return data
        return {}
    return value


def _facade(value: Any) -> str | None:
    text = str(value or "").lower()
    return next((name for name in FACADES if name in text), None)


def _captured(value: Any, log_root: Path) -> Any:
    """Resolve a runtime blob beneath its run directory and verify its digest."""
    if value is None:
        return None
    data = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    if data.get("kind") == "inline":
        return data.get("value")
    if data.get("kind") == "json_references":
        from src.harness_contracts import JsonReferencedCapture
        from src.agent_runtime.store import EventStore
        reader = object.__new__(EventStore)
        reader.directory = log_root.resolve()
        return reader.resolve(JsonReferencedCapture.model_validate(data))
    if data.get("kind") == "image_references":
        from src.harness_contracts import ImageReferencedCapture
        from src.agent_runtime.image_capture import reconstruct_capture
        from src.agent_runtime.store import EventStore
        reader = object.__new__(EventStore)
        reader.directory = log_root.resolve()
        return json.loads(reconstruct_capture(ImageReferencedCapture.model_validate_json(json.dumps(data)), reader.get_bytes))
    if data.get("kind") == "blob":
        blob = data.get("blob") or {}
        uri = blob.get("uri")
        if not isinstance(uri, str):
            raise ValueError("blob capture has no relative URI")
        root = log_root.resolve()
        path = (root / uri).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"blob URI escapes event-log directory: {uri}")
        if not path.is_file():
            raise ValueError(f"blob URI does not exist below event-log directory: {uri}")
        raw = path.read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        if actual != blob.get("sha256"):
            raise ValueError(f"blob hash mismatch for {uri}: expected {blob.get('sha256')}, got {actual}")
        media_type = str(blob.get("media_type") or "")
        if media_type == "application/json" or media_type.endswith("+json"):
            try:
                return json.loads(raw)
            except (UnicodeDecodeError, ValueError) as error:
                raise ValueError(f"JSON blob cannot be decoded: {uri}") from error
        if media_type.startswith("text/"):
            try:
                return _json(raw.decode("utf-8"))
            except UnicodeDecodeError as error:
                raise ValueError(f"text blob is not UTF-8: {uri}") from error
        return {"blob": blob, "verified_bytes": len(raw)}
    return {"missing": data.get("reason")}


def _relative_times(steps: list[dict[str, Any]], origin: float | None) -> None:
    if origin is None:
        return
    for step in steps:
        absolute = step.pop("_absolute_time", None)
        step["t_call"] = None if absolute is None else round(absolute - origin, 3)


def _verify_responses_presentation(execution, presentation, visible, prepared,
                                   events_by_id, log_root: Path) -> None:
    """Verify native wire evidence separately from its logical tool result."""
    from src.agent_runtime.responses import _content

    request = events_by_id.get(presentation.request_event_id)
    if (request is None or request.payload.event_type != "adapter_request"
            or request.payload.adapter != "openai-responses-http-v1"
            or request.task_id != execution.task_id or request.sequence <= execution.sequence):
        raise ValueError("Responses presentation lacks its native request")
    sent = _captured(request.payload.final_request_body, log_root)
    if not isinstance(sent, dict) or not isinstance(sent.get("input"), list):
        raise ValueError("Responses presentation lacks native input items")
    if not isinstance(visible, dict) or not isinstance(prepared, dict):
        raise ValueError("Responses presentation lacks its logical tool result")
    tool_result = visible.get("wire_tool_result")
    logical = visible.get("tool_message")
    call_id = execution.payload.call_id
    results = [item for item in sent["input"] if isinstance(item, dict)
               and item.get("type") == "function_call_output" and item.get("call_id") == call_id]
    if (not isinstance(tool_result, dict) or results != [tool_result]
            or not isinstance(logical, dict) or logical.get("role") != "tool"
            or logical.get("tool_call_id") != call_id):
        raise ValueError("Responses tool presentation is absent from the actual request")
    output = tool_result.get("output")
    if isinstance(output, str):
        output = [{"type": "input_text", "text": output}]
    if _content(logical.get("content")) != output:
        raise ValueError("logical Responses tool result differs from native content")
    images = visible.get("image_blocks", [])
    wire_images = visible.get("wire_image_blocks")
    # image_blocks also carries the text label associating a picture with its call.
    if (not isinstance(images, list) or not isinstance(wire_images, list)
            or _content(images) != wire_images):
        raise ValueError("logical Responses images differ from native content")
    blocks = [block for item in sent["input"] if isinstance(item, dict)
              for field in ("content", "output") if isinstance(item.get(field), list)
              for block in item[field] if isinstance(block, dict)]
    # Count occurrences: one wire image cannot substantiate two reported images.
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"))
    available = Counter(canonical(block) for block in blocks)
    if Counter(canonical(image) for image in wire_images) - available:
        raise ValueError("Responses images are absent from the actual request")
    if logical != prepared.get("tool_message") or images != prepared.get("image_blocks", []):
        context = events_by_id.get(presentation.context_event_id)
        if (context is None or context.payload.event_type != "context"
                or context.task_id != execution.task_id
                or not execution.sequence < context.sequence < request.sequence):
            raise ValueError("changed logical Responses presentation lacks its prior context decision")
        # A projection can emit several decisions; its recorded context ID is
        # the last one. Validate the actual transformations in that interval,
        # rather than accepting any unrelated context event as authorization.
        decisions = [event.payload for event in events_by_id.values()
                     if event.task_id == execution.task_id
                     and execution.sequence < event.sequence <= context.sequence
                     and event.payload.event_type == "context"]
        original = prepared.get("tool_message")
        if logical != original:
            from src.agent_runtime.context import _message_content_sha
            allowed = []
            for decision in decisions:
                if decision.action != "compact" or execution.event_id not in decision.replaced_event_ids:
                    continue
                summary = _captured({"kind": "blob", "blob": decision.summary.model_dump(mode="json")}, log_root)
                if not isinstance(summary, dict) or summary.get("kind") != "large_result_references":
                    continue
                for replacement in summary.get("replacements", []):
                    if (replacement.get("event_id") == execution.event_id
                            and replacement.get("content_sha256") == _message_content_sha(original)):
                        allowed.append({**original, "content": canonical({"context_reference": {
                            "content_sha256": replacement["content_sha256"],
                            "full_result_history_id": replacement["full_result_history_id"],
                            "reason": "exact repeated large tool result"}})})
            if logical not in allowed:
                raise ValueError("changed logical Responses text is not explained by its context decisions")
        original_images = prepared.get("image_blocks", [])
        remaining = list(images)
        removed = []
        for block in original_images:
            if remaining and block == remaining[0]:
                remaining.pop(0)
            else:
                removed.append(block)
        if remaining:
            raise ValueError("changed Responses images are not a retained subset")
        from src.agent_runtime.context import _image_block_hash
        removed_hashes = [_image_block_hash(block) for block in removed]
        authorized = {decision.image.sha256 for decision in decisions
                      if decision.action == "remove_image" and decision.image.kind == "sha256"}
        if (any(sha not in authorized for sha in removed_hashes if sha is not None)
                or (removed and not any(sha is not None for sha in removed_hashes))):
            raise ValueError("changed Responses images are not explained by their context decisions")


def _read_event_log(path: Path) -> dict[str, Any]:
    events: list[EventEnvelope] = []
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if line.strip():
                try:
                    events.append(EventEnvelope.model_validate_json(line))
                except Exception as error:
                    raise ValueError(f"{path}:{number}: invalid EventEnvelope: {error}") from error
    if not events:
        raise ValueError(f"empty event log: {path}")
    if len({event.run_id for event in events}) != 1:
        raise ValueError(f"event log contains multiple run IDs: {path}")
    sequences = [event.sequence for event in events]
    if sequences != sorted(set(sequences)):
        raise ValueError(f"event sequences are duplicate or out of order: {path}")
    event_ids = [event.event_id for event in events]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError(f"event IDs are not unique: {path}")
    events_by_id = {event.event_id: event for event in events}

    presentations: dict[str, tuple[EventEnvelope, Any]] = {}
    for event in events:
        payload = event.payload
        if payload.event_type != "tool_presentation":
            continue
        target = payload.tool_execution_event_id
        if target in presentations:
            raise ValueError(f"multiple presentation events target tool execution {target}")
        presentations[target] = (event, payload)
    known_execution_ids = {
        event.event_id for event in events if event.payload.event_type == "tool_execution"
    }
    unknown_presentations = sorted(set(presentations) - known_execution_ids)
    if unknown_presentations:
        raise ValueError(f"tool presentations reference unknown executions: {unknown_presentations}")

    responses: dict[str, dict[str, Any]] = {}
    requests: list[dict[str, Any]] = []
    response_records: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    texts: list[str] = []
    usage: list[dict[str, Any]] = []
    request_models: list[str] = []
    origins = [e.occurred_at.value.timestamp() for e in events if e.occurred_at.kind == "known"]
    origin = min(origins) if origins else None
    for event in events:
        payload = event.payload
        kind = payload.event_type
        absolute = event.occurred_at.value.timestamp() if event.occurred_at.kind == "known" else None
        if kind == "adapter_request":
            final_request_body = _captured(payload.final_request_body, path.parent)
            request_model = (
                final_request_body.get("model")
                if isinstance(final_request_body, dict) else None
            ) or payload.versions.remote_model.remote_alias
            if request_model and request_model not in request_models:
                request_models.append(request_model)
            requests.append({
                "event_id": event.event_id,
                "adapter": payload.adapter,
                "model": request_model,
                "final_request_body": (payload.final_request_body.model_dump(mode="json")
                    if payload.final_request_body.kind in {"image_references", "json_references"} else final_request_body),
                "final_request_encoding": ("captured_value" if payload.final_request_body.kind in {"image_references", "json_references"}
                    else "expanded_json"),
                "wire_sha256": payload.wire_sha256,
                "injected_content": [item.model_dump(mode="json") for item in payload.injected_content],
                "images": [item.model_dump(mode="json") for item in payload.images],
                "parameters": payload.parameters.model_dump(mode="json"),
                "versions": payload.versions.model_dump(mode="json"),
            })
        elif kind == "model_response":
            row = {
                "event_id": event.event_id,
                "request_event_id": payload.request_event_id,
                "text": "\n\n".join(payload.visible_text),
                "thinking": [item.model_dump(mode="json") for item in payload.thinking],
                "usage": payload.usage.model_dump(mode="json"),
                "raw_response": _captured(payload.raw_response, path.parent),
                "tool_calls": [item.model_dump(mode="json") for item in payload.tool_calls],
            }
            response_records.append(row)
            texts.extend(payload.visible_text)
            usage.append(row["usage"])
            for call in payload.tool_calls:
                responses[call.call_id] = row
        elif kind == "tool_execution":
            response = responses.get(payload.call_id, {})
            raw = _captured(payload.raw_result, path.parent)
            prepared = _captured(payload.shown_result, path.parent)
            presentation = presentations.get(event.event_id)
            if payload.presentation_status == "sent":
                visible, delivered, presentation_id = prepared, True, None
            elif presentation is not None:
                presentation_event, presentation_payload = presentation
                visible = _captured(presentation_payload.shown_result, path.parent)
                conversion = presentation_payload.protocol_conversion
                converted = conversion in {"anthropic_messages_v1", "responses_v1"}
                if conversion == "responses_v1":
                    _verify_responses_presentation(event, presentation_payload, visible, prepared,
                                                   events_by_id, path.parent)
                elif conversion == "anthropic_messages_v1":
                    from src.agent_runtime.anthropic import native_blocks, without_cache
                    request_event = events_by_id.get(presentation_payload.request_event_id)
                    if request_event is None or request_event.payload.adapter != "anthropic-messages-http-v1":
                        raise ValueError("Messages presentation lacks its native request")
                    sent = _captured(request_event.payload.final_request_body, path.parent)
                    blocks = [b for m in sent["messages"] for b in m["content"]]
                    tool_result = visible.get("wire_tool_result")
                    if (tool_result not in blocks or tool_result.get("tool_use_id") != payload.call_id
                            or any(b not in blocks for b in visible.get("wire_image_blocks", []))):
                        raise ValueError("Messages tool presentation is absent from the actual request")
                    if (native_blocks(visible["tool_message"]["content"]) != tool_result["content"]
                            or native_blocks(visible["image_blocks"]) != [without_cache(b) for b in visible["wire_image_blocks"]]):
                        raise ValueError("logical tool presentation differs from native content")
                    if (visible["tool_message"] != prepared["tool_message"] or visible["image_blocks"] != prepared["image_blocks"]) and presentation_payload.context_event_id is None:
                        raise ValueError("changed logical Messages presentation lacks context evidence")
                if visible != prepared and presentation_payload.context_event_id is None and not converted:
                    raise ValueError(
                        f"tool presentation {presentation_event.event_id} differs from prepared result {event.event_id}"
                    )
                if visible != prepared and not converted:
                    context_event = events_by_id.get(presentation_payload.context_event_id)
                    request_event = events_by_id.get(presentation_payload.request_event_id)
                    if (context_event is None or context_event.payload.event_type != "context"
                            or request_event is None or request_event.payload.event_type != "adapter_request"
                            or context_event.sequence >= request_event.sequence):
                        raise ValueError("changed tool presentation lacks its prior context decision")
                    sent = _captured(request_event.payload.final_request_body, path.parent)
                    messages = sent.get("messages", [])
                    blocks = [b for m in messages if isinstance(m.get("content"), list)
                        for b in m["content"]]
                    if (visible.get("tool_message") not in messages
                            or any(b not in blocks for b in visible.get("image_blocks", []))):
                        raise ValueError("changed tool presentation is absent from the actual request")
                delivered, presentation_id = True, presentation_event.event_id
            else:
                visible, delivered, presentation_id = None, False, None
            steps.append({
                "index": len(steps) + 1,
                "execution_event_id": event.event_id,
                "_absolute_time": absolute,
                "tool": payload.tool_name,
                "arguments": payload.full_arguments,
                "prepared_result": prepared,
                "model_visible_result": visible,
                "delivered_to_model": delivered,
                "presentation_status": payload.presentation_status,
                "presentation_event_id": presentation_id,
                "context_event_id": presentation[1].context_event_id if presentation else None,
                "raw_result": raw,
                "outcome": payload.outcome,
                "is_error": payload.outcome == "failed",
                "model_text_before": response.get("text", ""),
                "thinking_before": response.get("thinking", []),
                "capture_scope": payload.capture_scope,
            })
    _relative_times(steps, origin)
    return {
        "source_format": "event_envelope_jsonl",
        "run": events[0].run_id,
        "invocations": [{"invocation": 1, "stream": path.name, "steps": steps}],
        "model": ", ".join(request_models) or None,
        "elapsed_seconds": (max(origins) - min(origins)) if origins else None,
        "usage": usage,
        "visible_text": texts,
        "requests": requests,
        "responses": response_records,
        "events": [event.model_dump(mode="json") for event in events],
        "event_counts": dict(Counter(event.payload.event_type for event in events)),
        "gaps": [],
        "source_files": [str(path)],
    }


def _legacy_streams(run: Path) -> list[Path]:
    compressed = sorted(p for p in run.glob("*_stream.jsonl.gz") if p.stat().st_size)
    plain = sorted(p for p in run.glob("*_stream.jsonl")
                   if p.stat().st_size and p.with_suffix(".jsonl.gz") not in compressed)
    return sorted(compressed + plain, key=lambda item: item.name)


def _read_cli_stream(path: Path, invocation: int) -> dict[str, Any]:
    opener = gzip.open(path, "rt") if path.suffix == ".gz" else path.open()
    with opener as stream:
        events = [json.loads(line) for line in stream if line.strip()]
    origin = next((_stamp(e["timestamp"]) for e in events if e.get("timestamp")), None)
    steps: list[dict[str, Any]] = []
    pending: dict[str, dict[str, Any]] = {}
    texts: list[str] = []
    thinking = 0
    usage: dict[str, dict[str, Any]] = {}
    quota: list[dict[str, Any]] = []
    init: dict[str, Any] = {}
    result: dict[str, Any] = {}
    last = origin
    for event in events:
        if event.get("timestamp"):
            last = _stamp(event["timestamp"])
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            init = {key: event.get(key) for key in
                    ("model", "claude_code_version", "apiKeySource", "permissionMode")}
        elif kind == "system" and event.get("subtype") == "thinking_tokens":
            thinking += int(event.get("estimated_tokens_delta") or 0)
        elif kind == "rate_limit_event":
            info = event.get("rate_limit_info") or {}
            windows = info.get("unifiedWindows") or {}
            quota.append({"t": None if last is None or origin is None else round(last - origin, 3),
                          "status": info.get("status"), "raw": info,
                          "five_hour": (windows.get("five_hour") or {}).get("utilization"),
                          "seven_day": (windows.get("seven_day") or {}).get("utilization")})
        elif kind == "assistant":
            message = event.get("message") or {}
            if message.get("id") and message.get("usage"):
                previous = usage.get(message["id"], {})
                current = message["usage"]
                if (current.get("output_tokens") or 0) >= (previous.get("output_tokens") or 0):
                    usage[message["id"]] = dict(current, message_id=message["id"],
                        t=None if last is None or origin is None else round(last - origin, 3))
            for block in message.get("content") or []:
                if block.get("type") == "text" and block.get("text"):
                    texts.append(block["text"])
                elif block.get("type") == "tool_use":
                    step = {
                        "index": len(steps) + 1,
                        "_absolute_time": last,
                        "tool": block["name"].replace("mcp__bim__", ""),
                        "arguments": block.get("input") or {},
                        "model_text_before": "\n\n".join(texts),
                        "thinking_tokens_before": thinking,
                    }
                    texts, thinking = [], 0
                    pending[block["id"]] = step
                    steps.append(step)
        elif kind == "user":
            content = (event.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                step = pending.pop(block.get("tool_use_id"), None)
                if step is None:
                    continue
                parts = block.get("content")
                parts = parts if isinstance(parts, list) else [{"type": "text", "text": str(parts or "")}]
                text = "\n".join(item.get("text", "") for item in parts if item.get("type") == "text")
                images = []
                for item in parts:
                    if item.get("type") != "image":
                        continue
                    source = item.get("source") or {}
                    raw = base64.b64decode(source.get("data") or "")
                    images.append({"media_type": source.get("media_type"), "bytes": len(raw),
                                   "sha256": hashlib.sha256(raw).hexdigest()})
                step.update(result_text=text, result_images=images,
                            t_result=None if last is None or origin is None else round(last - origin, 3),
                            model_visible_result=_json(text),
                            delivered_to_model=True, delivery_evidence="CLI tool_result message",
                            is_error=bool(block.get("is_error")), outcome=("failed" if block.get("is_error") else "succeeded"))
        elif kind == "result":
            result = {key: event.get(key) for key in
                      ("subtype", "is_error", "num_turns", "duration_ms", "total_cost_usd",
                       "stop_reason", "terminal_reason", "result", "usage", "modelUsage")}
    for step in pending.values():
        step.update(result_text=None, model_visible_result=None,
                    delivered_to_model=False, is_error=None, outcome="unknown", unanswered=True)
    _relative_times(steps, origin)
    return {"invocation": invocation, "stream": path.name, "steps": steps, "init": init,
            "message_usage": list(usage.values()), "quota": quota, "result": result,
            "final_text_after_last_tool": "\n\n".join(texts),
            "trailing_thinking_tokens": thinking}


def _read_cli(run: Path) -> dict[str, Any]:
    invocations = [_read_cli_stream(path, number)
                   for number, path in enumerate(_legacy_streams(run), 1)]
    receipt_path = next(iter(sorted(run.glob("*_receipt.json"))), None)
    receipt = json.loads(receipt_path.read_text()) if receipt_path else {}
    return {
        "source_format": "claude_cli_stream",
        "run": run.name,
        "invocations": invocations,
        "receipt": receipt,
        "model": receipt.get("actual_model") or ((invocations[0].get("init") or {}).get("model") if invocations else None),
        "elapsed_seconds": receipt.get("elapsed_seconds"),
        "usage": [row for inv in invocations for row in inv["message_usage"]],
        "visible_text": [step["model_text_before"] for inv in invocations for step in inv["steps"]
                         if step.get("model_text_before")] +
                        [inv["final_text_after_last_tool"] for inv in invocations if inv["final_text_after_last_tool"]],
        "gaps": [
            "CLI launch request is not the final provider request body.",
            "Tool results are the CLI-visible transformed results, not guaranteed raw MCP bytes.",
            "Thinking content is unavailable; only CLI-estimated token deltas and signatures may exist.",
        ],
        "source_files": [str(path) for path in _legacy_streams(run)] + ([str(receipt_path)] if receipt_path else []),
    }


def _read_bridge(run: Path) -> dict[str, Any]:
    tool_path = run / "tools.jsonl"
    rows = [json.loads(line) for line in tool_path.read_text().splitlines() if line.strip()]
    origin = min((float(row["time"]) for row in rows if row.get("time") is not None), default=None)
    steps = []
    for row in rows:
        data = row.get("data") or {}
        action = row.get("action", "unknown")
        # The audit stream records returned data, not original arguments.  Keep a few
        # unambiguous selectors useful for behaviour metrics and disclose the rest.
        arguments = {}
        for key in ("name", "image", "box", "candidate", "topic", "facade", "floor_id"):
            if key in data:
                arguments[key] = data[key]
        steps.append({"index": len(steps) + 1, "_absolute_time": row.get("time"),
                      "tool": action, "arguments": arguments, "model_visible_result": data,
                      "delivered_to_model": True,
                      "delivery_evidence": "legacy result-side audit; exact transport unverified",
                      "is_error": bool(data.get("error")),
                      "outcome": "failed" if data.get("error") else "succeeded",
                      "arguments_capture": "historical_result_only"})
    _relative_times(steps, origin)
    controller = run / "controller_receipt.json"
    receipt = json.loads(controller.read_text()) if controller.is_file() else {}
    dev_calls = run / "dev_calls.jsonl"
    sources = [str(tool_path)] + ([str(dev_calls)] if dev_calls.is_file() else []) + ([str(controller)] if controller.is_file() else [])
    return {
        "source_format": "legacy_bridge_audit",
        "run": run.name,
        "invocations": [{"invocation": 1, "stream": tool_path.name, "steps": steps}],
        "model": receipt.get("provider_actual_model_receipt") or receipt.get("requested_model"),
        "elapsed_seconds": receipt.get("elapsed_seconds_to_final_tool"),
        "usage": [],
        "visible_text": [],
        "gaps": [
            "Bridge tools.jsonl stores result-side audit rows, not complete call arguments or model-visible transport.",
            "Final provider request/response, visible model text, thinking and per-message usage were not archived.",
            "Requested model is retained when present; provider actual-model identity is unverified when its receipt is absent.",
        ],
        "source_files": sources,
    }


def _behaviour_reference_target(target: Path, record: dict, source_root):
    if record.get("schema_version") != 1:
        raise ValueError("unsupported behaviour reference version")
    log = (target.parent / record["event_log"]["uri"]).resolve()
    if log.name != "events.jsonl":
        raise ValueError("behaviour reference must point to events.jsonl")
    if hashlib.sha256(log.read_bytes()).hexdigest() != record["event_log"]["sha256"]:
        raise ValueError("behaviour event-log hash mismatch; regenerate the report after resume")
    evidence_root = source_root or ((target.parent / record["source_root"]).resolve()
                                   if record.get("source_root") else None)
    return log, evidence_root


def load_behaviour(path: str | Path, *, source_root: str | Path | None = None) -> dict[str, Any]:
    """Load a run directory, current ``events.jsonl``, CLI stream, or bridge audit."""
    path = Path(path)
    if path.is_dir() and (path / "events.jsonl").is_file():
        record = _read_event_log(path / "events.jsonl")
    elif path.is_file() and path.name == "events.jsonl":
        record = _read_event_log(path)
    elif path.is_dir() and _legacy_streams(path):
        record = _read_cli(path)
    elif (path.is_dir() and (path / "record.json.gz").is_file()) or path.name == "record.json.gz":
        # A historical record is an exact transformed CLI capture, not a new
        # provider trace. Saved claim/source files can be supplied separately.
        target = path / "record.json.gz" if path.is_dir() else path
        with gzip.open(target, "rt") as stream:
            record = json.load(stream)
        if record.get("source_format") == "event_envelope_reference":
            log, evidence_root = _behaviour_reference_target(target, record, source_root)
            return load_behaviour(log, source_root=evidence_root)
        record.setdefault("source_format", "claude_cli_record")
        receipt = record.get("receipt") or {}
        record.setdefault("model", receipt.get("actual_model"))
        record.setdefault("elapsed_seconds", receipt.get("elapsed_seconds"))
        record.setdefault("gaps", ["Provider request is not captured in this historical CLI record."])
        record.setdefault("source_files", [str(target)])
        for inv in record["invocations"]:
            for step in inv["steps"]:
                step.setdefault("model_visible_result", _json(step.get("result_text")))
                step.setdefault("delivered_to_model", not step.get("unanswered", False))
                step.setdefault("outcome", "unknown" if step.get("unanswered") else
                                "failed" if step.get("is_error") else "succeeded")
        record.setdefault("visible_text", [s.get("model_text_before", "") for inv in record["invocations"]
            for s in inv["steps"]] + [inv.get("final_text_after_last_tool", "") for inv in record["invocations"]])
    elif path.is_dir() and (path / "tools.jsonl").is_file():
        record = _read_bridge(path)
    elif path.is_dir() and (path / "inputs.json").is_file():
        record = {"source_format": "missing_capture", "run": path.name,
            "invocations": [], "model": None, "elapsed_seconds": None,
            "usage": [], "visible_text": [], "source_files": [],
            "gaps": ["No CLI stream or tool audit was captured. Zero captured steps is not evidence of zero executed calls."]}
    else:
        raise ValueError(f"no supported behaviour log found at {path}")
    log_root = path if path.is_dir() else path.parent
    record["source_files"] = [Path(name).resolve().relative_to(log_root.resolve()).as_posix()
        if Path(name).resolve().is_relative_to(log_root.resolve()) else name
        for name in record.get("source_files", [])]
    record["_source_root"] = str(source_root or (path if path.is_dir() else path.parent))
    for inv in record["invocations"]:
        for step in inv["steps"]:
            step["result_data"] = tool_result_data(step.get("raw_result") or
                step.get("model_visible_result") or step.get("result_text"))
    record["summary"] = summarise(record)
    record.pop("_source_root")
    return record


def _view_detail(step: dict[str, Any]) -> dict[str, Any]:
    arguments = step.get("arguments") or {}
    result = step.get("result_data") or tool_result_data(step.get("model_visible_result"))
    box = result.get("box_original_pixels") or arguments.get("box")
    size = result.get("original_size")
    scale = result.get("display_scale_actual")
    if isinstance(scale, list):
        scale = min(scale)
    # The request itself distinguishes a full view from a crop even when the
    # current runtime wraps the visible tool result for provider transport.
    # A returned full-image box is retained as corroborating result metadata.
    requested_box = arguments.get("box")
    full = requested_box is None
    if requested_box is None and size and box is not None:
        full = box == [0, 0, *size]
    return {"image": arguments.get("name") or result.get("name"), "box": box, "scale": scale,
            "full": full}


def _polygon_centroid(points: list[list[float]]) -> tuple[float, float] | None:
    if len(points) < 3:
        return None
    area2 = sum(a[0] * b[1] - b[0] * a[1]
                for a, b in zip(points, points[1:] + points[:1]))
    if abs(area2) < 1e-12:
        return None
    x = sum((a[0] + b[0]) * (a[0] * b[1] - b[0] * a[1])
            for a, b in zip(points, points[1:] + points[:1])) / (3 * area2)
    y = sum((a[1] + b[1]) * (a[0] * b[1] - b[0] * a[1])
            for a, b in zip(points, points[1:] + points[:1])) / (3 * area2)
    return x, y


def _opening_facades(run: Path, candidate: Any, identities: set[str]) -> dict[str, str]:
    path = run / str(candidate or "") / "source_model.json"
    if not candidate or not path.is_file():
        return {}
    try:
        source = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    centres = {row["id"]: _polygon_centroid(row.get("polygon") or [])
               for row in source.get("spaces", [])}
    found: dict[str, str] = {}
    for opening in source.get("openings", []):
        if (opening.get("id") not in identities or not opening.get("exterior")
                or not opening.get("space_ids")):
            continue
        centre = centres.get(opening["space_ids"][0])
        vertices = opening.get("vertices") or []
        if centre is None or not vertices:
            continue
        xs, ys = [row[0] for row in vertices], [row[1] for row in vertices]
        x, y = sum(xs) / len(xs), sum(ys) / len(ys)
        if max(xs) - min(xs) >= max(ys) - min(ys):
            found[opening["id"]] = "north" if y > centre[1] else "south"
        else:
            found[opening["id"]] = "east" if x > centre[0] else "west"
    return found


def _claim_detail(step: dict[str, Any], run: Path, saved: dict[str, dict]) -> dict[str, Any]:
    arguments = step.get("arguments") or {}
    result = step.get("result_data") or {}
    declared = _json(arguments.get("claim_json", arguments))
    declared = declared if isinstance(declared, dict) else {}
    identity = result.get("id") or result.get("claim_id")
    saved_row = saved.get(identity)
    if saved_row is None:
        # A CLI output can be truncated. Match immutable saved input fields,
        # never assign an ID merely by call order (counts also use record_claim).
        matches = [row for row in saved.values() if all(
            (row.get("claim") or {}).get(key) == declared.get(key)
            for key in ("candidate", "objects", "values", "reason"))]
        saved_row = matches[0] if len(matches) == 1 and declared.get("objects") else None
    if saved_row:
        result = saved_row
    claim = result.get("claim") or declared
    objects = [item.get("id") for item in claim.get("objects") or [] if isinstance(item, dict)]
    sources = [item.get("image") for item in result.get("sources") or [] if isinstance(item, dict)]
    source_facades = sorted({name for name in map(_facade, sources) if name})
    targets = sorted(set(_opening_facades(run, claim.get("candidate"), set(objects)).values()))
    return {"id": result.get("id"), "objects": objects, "sources": sources,
            "resolved": result.get("resolved_values"), "source_facades": source_facades,
            "target_facades": targets,
            "cross_facade": bool(targets and source_facades and set(targets) - set(source_facades)),
            "reason": claim.get("reason"), "t": step.get("t_call"),
            "evidence_capture": "saved_claim_file" if saved_row else "returned_or_requested_only",
            "saved_file": saved_row.get("_saved_file") if saved_row else None,
            "saved_sha256": saved_row.get("_saved_sha256") if saved_row else None}


def _transaction_result(step: dict[str, Any], run: Path) -> dict[str, Any]:
    """Read full entries for analysis without rewriting the model-visible reply."""
    data = step.get("result_data") or {}
    entries = data.get("entries")
    if isinstance(entries, list) and all(isinstance(row, dict) and "status" in row for row in entries):
        return data
    if "entries" not in data and not data.get("report_view"):
        return data
    relative, expected = data.get("details_file"), data.get("details_sha256")
    if not isinstance(relative, str) or not isinstance(expected, str):
        raise ValueError("claim_transaction projected entries lack a saved report reference")
    root = run.resolve()
    folder, path = (root / "tool_reports").resolve(), (root / relative).resolve()
    if not folder.is_relative_to(root) or path.parent != folder or path.suffix != ".json":
        raise ValueError("claim_transaction report must remain in the run's tool_reports directory")
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected or not path.stem.endswith("_" + actual):
        raise ValueError("claim_transaction saved report hash mismatch")
    result = json.loads(raw)
    entries = result.get("entries") if isinstance(result, dict) else None
    failed_without_entries = isinstance(result, dict) and "entries" not in result and step.get("is_error")
    if not failed_without_entries and (not isinstance(entries, list) or not all(
            isinstance(row, dict) and "status" in row for row in entries)):
        raise ValueError("claim_transaction saved report needs a complete entries list")
    for key in ("id", "status", "candidate", "saved_candidate"):
        if key in data and data[key] != result.get(key):
            raise ValueError(f"claim_transaction saved report differs from returned {key}")
    step["transaction_entries_source"] = {"file": path.relative_to(root).as_posix(), "sha256": actual}
    return result


def summarise(record: dict[str, Any]) -> dict[str, Any]:
    steps = [step for invocation in record["invocations"] for step in invocation["steps"]]
    tools = Counter(step["tool"] for step in steps)
    run = Path(record.get("_source_root", "."))
    if (run / "bim").is_dir():
        run = run / "bim"
    saved = {}
    for path in sorted((run / "claims").glob("claim_*.json")):
        raw = path.read_bytes()
        row = json.loads(raw)
        saved[row["id"]] = dict(row, _saved_file=path.relative_to(run).as_posix(),
                               _saved_sha256=hashlib.sha256(raw).hexdigest())
    transactions = [step for step in steps if step["tool"] == "claim_transaction"]
    transaction_results = {id(step): _transaction_result(step, run) for step in transactions}
    transaction_entries = [entry for data in transaction_results.values() for entry in data.get("entries", [])]
    for step in steps:
        data = transaction_results.get(id(step), step.get("result_data") or {})
        saved_bim = read_saved_result(data, tool=step["tool"], run=run)
        ready = saved_bim.get("source_geometry_ready", data.get("source_geometry_ready"))
        call_error = bool(step.get("is_error"))
        domain_failure = not call_error and (data.get("status") in {"error", "failed"}
                         or step["tool"] == "claim_transaction" and data.get("status") == "partial"
                         or ready is False or bool(data.get("error")))
        step["call_error"] = call_error
        step["domain_failure"] = domain_failure
        # A partial transaction can both fail in the domain and commit usable
        # sources. Count those commits; do not erase them with the batch status.
        step["usable_source_draft"] = (len(saved_bim["save_effects"]["created_candidates"])
                                        if not call_error else 0)
        step["saved_candidate"] = saved_bim["saved_candidate"]
    attempt = next((step.get("t_call") for step in steps if step["tool"] in FIRST_DRAFT_TOOLS), None)
    first = next((step.get("t_call") for step in steps if step["usable_source_draft"]), None)
    views = [dict(_view_detail(step), t=step.get("t_call")) for step in steps
             if step["tool"] == "view_image" and step.get("delivered_to_model", True)
             and step.get("outcome") == "succeeded"]
    before = [step for step in steps if first is None or (step.get("t_call") is not None and step["t_call"] < first)]
    views_before = [view for view in views if first is None or (view.get("t") is not None and view["t"] < first)]
    crops = [view for view in views if not view["full"]]
    overlays = Counter(_facade((step.get("arguments") or {}).get("image") or
                               (step.get("arguments") or {}).get("name") or
                               (step.get("result_data") or {}).get("facade"))
                       for step in steps if step["tool"] == "view_elevation_candidate")
    claims = [_claim_detail(step, run, saved) for step in steps
              if step["tool"] in {"record_claim", "replace_claim_sources"} and not step.get("is_error")
              and step.get("delivered_to_model", True)]
    # Entries share the actual transaction call/time; they are not extra tool
    # calls. Read saved claims just as for the compatibility single-step tools.
    for step in transactions:
        if step.get("is_error") or not step.get("delivered_to_model", True):
            continue
        for entry in transaction_results[id(step)].get("entries", []):
            if entry.get("recorded") and entry.get("claim_id") in saved:
                claims.append(_claim_detail({**step, "arguments": {},
                    "result_data": {"id": entry["claim_id"]}}, run, saved))
    claims = [claim for claim in claims if claim["objects"]]
    height_sources = Counter(name for claim in claims for name in claim["source_facades"])
    invocations = record["invocations"]
    quota = [row for inv in invocations for row in inv.get("quota", [])]
    five = [row["five_hour"] for row in quota if row.get("five_hour") is not None]
    seven = [row["seven_day"] for row in quota if row.get("seven_day") is not None]
    cli = next((inv.get("init", {}).get("claude_code_version") for inv in invocations
                if inv.get("init", {}).get("claude_code_version")), None)
    cli_results = [inv.get("result") or {} for inv in invocations]
    turns = (len(record["requests"]) if "requests" in record else
             sum(row.get("num_turns") or 0 for row in cli_results) or None)
    model_usage = {}
    for result in cli_results:
        for name, value in (result.get("modelUsage") or {}).items():
            total = model_usage.setdefault(name, Counter())
            total.update({key: number for key, number in value.items()
                          if isinstance(number, (int, float))})
    outcome_counts = {key: sum(step[key] for step in steps) for key in
                      ("call_error", "domain_failure", "usable_source_draft")}
    calls_before_attempt = [s for s in steps if attempt is None or
                            s.get("t_call") is not None and s["t_call"] < attempt]
    views_before_attempt = [v for v in views if attempt is None or
                            v.get("t") is not None and v["t"] < attempt]
    return {
        "schema_version": "bim.behaviour.v2",
        "run": record["run"], "source_format": record["source_format"], "model": record.get("model"),
        "cli": cli, "turns": turns, "invocations": len(invocations),
        "elapsed_seconds": record.get("elapsed_seconds"), "tool_calls": len(steps),
        "tool_capture_missing": record["source_format"] == "missing_capture",
        "call_errors": outcome_counts["call_error"], "domain_failures": outcome_counts["domain_failure"],
        "usable_source_drafts": outcome_counts["usable_source_draft"],
        "tool_errors": outcome_counts["call_error"], "tools": dict(tools),
        "claim_transactions": {"calls": len(transactions), "entries": len(transaction_entries),
            "status_counts": dict(Counter(entry["status"] for entry in transaction_entries)),
            "recorded_claims": sum(bool(entry.get("recorded")) for entry in transaction_entries),
            "applied_entries": sum(entry.get("status") == "applied" for entry in transaction_entries)},
        "outcome_definitions": {
            "call_errors": "outer MCP/transport call failed; not added again to domain_failures",
            "domain_failures": "normal return with status error/failed/partial, error, or source_geometry_ready=false",
            "usable_source_drafts": "committed usable sources from the shared save contract, including partial batches; selections excluded; not a fidelity verdict",
            "tool_errors": "historical alias of call_errors only",
            "first_draft_s": "first usable source, including one produced by correcting a failed draft",
            "time": "CLI t_call is dispatch time; runtime t_call is execution-event time; no invented precision"},
        "prepared_tool_results": sum("prepared_result" in step for step in steps),
        "delivered_tool_results": sum(step.get("delivered_to_model") is True for step in steps),
        "undelivered_tool_results": sum(step.get("delivered_to_model") is False for step in steps),
        "first_draft_s": first, "calls_before_first_draft": len(before),
        "first_build_attempt_s": attempt,
        "calls_before_first_build_attempt": len(calls_before_attempt),
        "full_views_before_first_build_attempt": sum(v["full"] for v in views_before_attempt),
        "crops_before_first_build_attempt": sum(not v["full"] for v in views_before_attempt),
        "pixel_tools_before_first_build_attempt": sum("pixel" in s["tool"] for s in calls_before_attempt),
        "first_assembly_s": next((s.get("t_call") for s in steps if s["tool"] == "assemble_plan_bim"
                                  and s["usable_source_draft"]), None),
        "finish_s": next((s.get("t_call") for s in reversed(steps) if s["tool"] == "finish_bim"
                          and not s["call_error"] and not s["domain_failure"]), None),
        "full_views_before_first_draft": sum(view["full"] for view in views_before),
        "crops_before_first_draft": sum(not view["full"] for view in views_before),
        "pixel_tools_before_first_draft": sum("pixel" in step["tool"] for step in before),
        "crop_scales": sorted(round(v["scale"], 2) for v in crops if isinstance(v["scale"], (int, float))),
        "elevation_crops_by_facade": dict(Counter(name for name in map(lambda row: _facade(row["image"]), crops) if name)),
        "elevation_overlays_by_facade": {key: value for key, value in overlays.items() if key},
        "height_provenance_claims": claims, "height_provenance_facades": dict(height_sources),
        "claims_from_saved_files": sum(c["evidence_capture"] == "saved_claim_file" for c in claims),
        "cross_facade_height_claims": sum(claim["cross_facade"] for claim in claims),
        "plan_builds": sum(tools[name] for name in FIRST_DRAFT_TOOLS),
        "plan_edits": tools["revise_plan_bim"] + tools["edit_plan_bim"],
        "candidate_revisions": tools["revise_bim"] + sum(entry.get("status") == "applied" for entry in transaction_entries), "gaps": record["gaps"],
        "outcomes": [{key: s.get(key) for key in ("index", "tool", "t_call", "call_error", "domain_failure",
                     "usable_source_draft", "saved_candidate")} for s in steps if s["call_error"] or s["domain_failure"] or s["usable_source_draft"]],
        "usage": record.get("usage", [row for inv in invocations for row in inv.get("message_usage", [])]),
        "model_usage": {key: dict(value) for key, value in model_usage.items()},
        "cost_usd_cli": sum(row.get("total_cost_usd") or 0 for row in cli_results) if cli else None,
        "thinking_tokens_estimated": (sum(s.get("thinking_tokens_before", 0) for s in steps)
            + sum(inv.get("trailing_thinking_tokens", 0) for inv in invocations)) if cli else None,
        "visible_text_chars": sum(len(t) for t in record.get("visible_text", [])),
        "five_hour_window": [five[0], five[-1]] if five else None,
        "seven_day_window": [seven[0], seven[-1]] if seven else None,
    }


def _short(value: Any, limit: int = 180) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = re.sub(r"\s+", " ", text or "")
    return text if len(text) <= limit else text[:limit] + f" …(+{len(text) - limit})"


def render_timeline(record: dict[str, Any]) -> str:
    summary = record["summary"]
    lines = [f"# 行为记录：{summary['run']}", "",
             f"来源格式 `{summary['source_format']}`；型号 `{summary['model'] or '未核实'}`；"
             f"{summary['tool_calls']} 次工具调用；{summary['turns']} 轮。", "",
             "| 调用报错 | 领域未成功 | 可用源稿（不等于保真通过） |",
             "|---:|---:|---:|",
             f"| {summary['call_errors']} | {summary['domain_failures']} | {summary['usable_source_drafts']} |", "",
             f"首次建模尝试 {summary['first_build_attempt_s']} 秒；首份可用源稿 {summary['first_draft_s']} 秒。", ""]
    if summary["gaps"]:
        lines += ["## 历史缺口", ""] + [f"- {gap}" for gap in summary["gaps"]] + [""]
    for invocation in record["invocations"]:
        lines += [f"## 调用 {invocation['invocation']}（{invocation['stream']}）", "",
                  "| # | 时间 s | 工具 | 参数 | 返回 | 调用报错／领域未成功／可用源稿 |",
                  "|---:|---:|---|---|---|---|"]
        for step in invocation["steps"]:
            lines.append(f"| {step['index']} | {step.get('t_call')} | {step['tool']} | "
                         f"{_short(step.get('arguments')).replace('|', '/')} | "
                         f"{_short(step.get('model_visible_result')).replace('|', '/')} | "
                         f"{int(step['call_error'])}/{int(step['domain_failure'])}/{int(step['usable_source_draft'])} |")
        lines.append("")
    return "\n".join(lines)


def write_behaviour_report(path: str | Path, out: str | Path, *, source_root: str | Path | None = None) -> dict[str, Any]:
    record = load_behaviour(path, source_root=source_root)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(record["summary"], ensure_ascii=False, indent=2) + "\n")
    (out / "timeline.md").write_text(render_timeline(record) + "\n")
    saved = record
    if record["source_format"] == "event_envelope_jsonl":
        source = Path(path)
        log = source / "events.jsonl" if source.is_dir() else source
        if log.name != "events.jsonl" or not log.is_file():
            target = source / "record.json.gz" if source.is_dir() else source
            with gzip.open(target, "rt") as stream:
                previous = json.load(stream)
            if previous.get("source_format") == "event_envelope_reference":
                log, source_root = _behaviour_reference_target(target, previous, source_root)
            else:
                # Old expanded records can be exported without their journal.
                log = None
        # The immutable event journal and its verified attachments already hold
        # the complete behaviour. Avoid serializing an expanded second copy.
        if log is not None:
            saved = {"source_format": "event_envelope_reference", "schema_version": 1,
                     "event_log": {"uri": os.path.relpath(log.resolve(), out.resolve()),
                                   "sha256": hashlib.sha256(log.read_bytes()).hexdigest()}}
            if source_root is not None:
                saved["source_root"] = os.path.relpath(Path(source_root).resolve(), out.resolve())
    with (out / "record.json.gz").open("wb") as raw_stream:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_stream, mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8") as stream:
                json.dump(saved, stream, ensure_ascii=False, indent=1)
    return record["summary"]


def main():
    """One offline command for CLI streams, runtime events and archived records."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, help="Saved BIM/claim files for an archived CLI record")
    args = parser.parse_args()
    summary = write_behaviour_report(args.run, args.out, source_root=args.source_root)
    print(json.dumps({key: summary[key] for key in (
        "run", "tool_calls", "call_errors", "domain_failures", "usable_source_drafts", "first_draft_s")},
        ensure_ascii=False))


if __name__ == "__main__":
    main()
