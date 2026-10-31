"""GLM Coding Plan Messages transport, aligned with the 10-03 CLI capture.

The context manager keeps protocol-neutral logical tool messages. This adapter
records their converted native blocks and source locations before sending.
Returned thinking blocks (including opaque signatures) are replayed unchanged.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import time

from src.harness_contracts import (AdapterRequestPayload, ImageTransmission,
    InjectedContent, ModelResponsePayload, ParameterAudit, ParametersNotReported,
    ParametersUnverified, PublicThinking, ResponseToolCall, ThinkingSignature,
    ThinkingTokenCount, ThinkingUnavailable, UsageMissing, UsageReported)
from .adapter import HttpChatAdapter, ParsedResponse, PreparedRequest, decode_image_url
from .estimation import estimate_chat_request
from .store import json_bytes


# Exact beta header from request_02; client identity/telemetry headers are not
# impersonated. Compatibility of these flags is tested on this specific route.
CAPTURED_BETAS = (
    "claude-code-20250219,interleaved-thinking-2025-05-14,thinking-token-count-2026-05-13,"
    "context-management-2025-06-27,prompt-caching-scope-2026-01-05,"
    "mid-conversation-system-2026-04-07,mid-conversation-tool-changes-2026-07-01,"
    "advisor-tool-2026-03-01,effort-2025-11-24"
)


class HttpAnthropicAdapter(HttpChatAdapter):
    def __init__(self, *, base_url, api_key, transport=None):
        super().__init__(base_url=base_url, api_key=api_key, transport=transport)
        self.endpoint = base_url.rstrip("/") + "/v1/messages?beta=true"

    async def send(self, request: PreparedRequest, *, timeout: float):
        self.last_send_timing = {}
        started = time.monotonic()
        try:
            response = await self.client.post(self.endpoint, content=request.wire_bytes,
                headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
                    "anthropic-version": "2023-06-01", "anthropic-beta": CAPTURED_BETAS}, timeout=timeout)
        finally:
            self.last_send_timing["http_round_trip_seconds"] = time.monotonic() - started
        if not response.is_success:
            from .failures import http_failure
            raise http_failure(response, secret=self._key, provider=self.failure_provider)
        started = time.monotonic()
        try:
            return response.json()
        finally:
            self.last_send_timing["http_json_decode_seconds"] = time.monotonic() - started


def native_blocks(content):
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        raise ValueError("message content must be text or blocks")
    blocks = []
    for block in content:
        if block.get("type") == "image_url":
            url = block["image_url"]["url"]
            _, mime = decode_image_url(url)
            blocks.append({"type": "image", "source": {"type": "base64",
                "media_type": mime, "data": url.split(",", 1)[1]}})
        elif block.get("type") == "text":
            blocks.append(copy.deepcopy(block))
        else:
            raise ValueError("unsupported logical content block")
    return blocks


def convert_messages(messages, message_sources, *, native_system_only=False):
    if len(messages) != len(message_sources):
        raise ValueError("every request message needs a recorded source")
    system, system_sources, converted, sources = [], [], [], []
    for message, source in zip(messages, message_sources, strict=True):
        role = message["role"]
        if role == "system" and (not converted or native_system_only):
            blocks = native_blocks(message["content"])
            system.extend(blocks)
            system_sources.extend([source] * len(blocks))
            continue
        if role == "assistant":
            if "anthropic_content" in message:
                blocks = copy.deepcopy(message["anthropic_content"])
            else:
                blocks = native_blocks(message.get("content"))
                for call in message.get("tool_calls", []):
                    blocks.append({"type": "tool_use", "id": call["id"],
                        "name": call["function"]["name"], "input": json.loads(call["function"]["arguments"])})
        elif role == "tool":
            role = "user"
            text = message["content"]
            blocks = [{"type": "tool_result", "tool_use_id": message["tool_call_id"],
                "content": native_blocks(text)}]
            if isinstance(text, str) and text.startswith("Tool error (isError=true):"):
                blocks[0]["is_error"] = True
        elif role in {"user", "system"}:
            # Mid-conversation system messages are a captured GLM extension;
            # preserve their role, with the matching beta header above.
            blocks = native_blocks(message.get("content"))
        else:
            raise ValueError("unsupported message role")
        if not blocks:
            raise ValueError("empty Messages content is not supported")
        if converted and converted[-1]["role"] == role and role == "user":
            converted[-1]["content"].extend(blocks)
            sources[-1].extend([source] * len(blocks))
        else:
            converted.append({"role": role, "content": blocks})
            sources.append([source] * len(blocks))
    # The captured CLI marks its two substantive system blocks and the last
    # block of the latest message. Our own single guide is one substantive block.
    for block in system[-2:]:
        block["cache_control"] = {"type": "ephemeral"}
    if converted:
        for block in reversed(converted[-1]["content"]):
            if block["type"] not in {"thinking", "redacted_thinking"}:
                block["cache_control"] = {"type": "ephemeral"}
                break
    return system, system_sources, converted, sources


def prepare_anthropic_request(*, store, model, messages, message_sources, tools,
        tool_source, parameters, versions, image_originals=None, model_profile=None,
        strict_model_profile=False, native_system_only=False, check_thinking_bindings=False):
    unsupported = set(parameters) - {"max_tokens", "thinking", "output_config", "context_management"}
    if unsupported:
        raise ValueError("unreviewed Anthropic parameters: " + ", ".join(sorted(unsupported)))
    system, system_sources, native, sources = convert_messages(messages, message_sources,
                                                               native_system_only=native_system_only)
    body = {"model": model, "system": system, "messages": native, "stream": False,
            **copy.deepcopy(parameters)}
    if tools:
        body["tools"] = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                          "input_schema": copy.deepcopy(t["function"]["parameters"])} for t in tools]
    binding_audit = None
    if check_thinking_bindings:
        from .anthropic_binding import project_bound_thinking
        sources, binding_audit = project_bound_thinking(store, body, sources)
        native = body["messages"]
    injections = []
    for i, (block, source) in enumerate(zip(system, system_sources, strict=True)):
        injections.append(InjectedContent(request_location=f"/system/{i}", content=store.capture(block), source=source))
    for i, (message, block_sources) in enumerate(zip(native, sources, strict=True)):
        for j, (block, source) in enumerate(zip(message["content"], block_sources, strict=True)):
            injections.append(InjectedContent(request_location=f"/messages/{i}/content/{j}",
                                              content=store.capture(block), source=source))
    if tools:
        injections.append(InjectedContent(request_location="/tools", content=store.capture(body["tools"]), source=tool_source))
    if binding_audit is not None:
        injections.append(InjectedContent(request_location="/messages", content=store.capture(native), source=binding_audit))
    images = []
    def visit(value, pointer):
        if isinstance(value, dict):
            if value.get("type") == "image":
                source = value["source"]
                if source["type"] != "base64":
                    raise ValueError("only archived base64 images are supported")
                sent = store.put_bytes(base64.b64decode(source["data"], validate=True), source["media_type"])
                original = (image_originals or {}).get(sent.sha256, sent)
                store.get_bytes(original)
                images.append(ImageTransmission(original=original, sent=sent, request_reference=pointer + "/source/data"))
            for key, child in value.items():
                visit(child, pointer + "/" + key.replace("~", "~0").replace("/", "~1"))
        elif isinstance(value, list):
            for i, child in enumerate(value):
                visit(child, pointer + f"/{i}")
    visit(body, "")
    wire = json_bytes(body)
    captured = store.capture(body, force_blob=True)
    if store.capture_bytes(captured) != wire:
        raise ValueError("stored Messages request differs from wire")
    from src.harness_contracts.events import _resolve_json_pointer
    for injection in injections:
        if store.resolve(injection.content) != _resolve_json_pointer(body, injection.request_location):
            raise ValueError("Messages injection differs from wire")
    payload = AdapterRequestPayload(adapter="anthropic-messages-http-v1", final_request_body=captured,
        wire_sha256=hashlib.sha256(wire).hexdigest(), injected_content=tuple(injections), images=tuple(images),
        parameters=ParameterAudit(requested=copy.deepcopy(parameters),
            provider_report=ParametersNotReported(reason="service does not attest applied parameters"),
            effect=ParametersUnverified(reason="matches captured CLI settings; server effect is not attested")), versions=versions)
    estimate = estimate_chat_request(body, profile=model_profile, strict=strict_model_profile)
    return PreparedRequest(body, wire, estimate, payload)


def parse_anthropic_response(raw, request_event_id, store):
    usage_raw = raw.get("usage")
    usage = UsageReported(raw_usage=usage_raw) if isinstance(usage_raw, dict) and usage_raw else UsageMissing(reason="service omitted per-request usage")
    visible, public, signatures, calls, thinking = [], [], [], [], []
    finish = {"max_tokens": "length", "tool_use": "tool_calls", "end_turn": "stop", "stop_sequence": "stop"}.get(raw.get("stop_reason"), raw.get("stop_reason"))
    error, assistant = None, {}
    try:
        if raw.get("type") != "message" or raw.get("role") != "assistant" or not isinstance(raw.get("content"), list):
            raise ValueError("invalid Messages response")
        seen = set()
        for block in raw["content"]:
            kind = block["type"]
            if kind == "text":
                if not isinstance(block.get("text"), str):
                    raise ValueError("invalid text block")
                if block["text"].strip():
                    visible.append(block["text"])
            elif kind == "thinking":
                if block.get("thinking"):
                    public.append(block["thinking"])
                if block.get("signature"):
                    signatures.append(block["signature"])
            elif kind == "redacted_thinking":
                if not isinstance(block.get("data"), str):
                    raise ValueError("invalid opaque thinking block")
            elif kind == "tool_use":
                if finish == "length":
                    continue  # The entire truncated batch is never executable.
                call = ResponseToolCall(call_id=block["id"], tool_name=block["name"], full_arguments=block["input"])
                if call.call_id in seen:
                    raise ValueError("duplicate tool call identity")
                seen.add(call.call_id)
                calls.append(call)
            else:
                raise ValueError("unsupported Messages response block")
        if public:
            thinking.append(PublicThinking(content="\n\n".join(public)))
        if signatures:
            thinking.append(ThinkingSignature(signature=signatures[0] if len(signatures) == 1 else json.dumps(signatures)))
        count = (usage_raw or {}).get("thinking_tokens")
        if type(count) is int and count >= 0:
            thinking.append(ThinkingTokenCount(tokens=count))
        assistant = {"role": "assistant", "content": "\n\n".join(visible) or None,
                     "anthropic_content": copy.deepcopy(raw["content"])}
        if calls:
            assistant["tool_calls"] = [{"type": "function", "id": c.call_id,
                "function": {"name": c.tool_name, "arguments": json.dumps(c.full_arguments, ensure_ascii=False)}} for c in calls]
        if finish not in {"stop", "tool_calls"}:
            error = "incomplete_response"
        elif calls and finish != "tool_calls":
            error = "inconsistent_finish_reason"
        elif (finish == "tool_calls" and not calls) or (not calls and not visible):
            error = "empty_response"
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        error = "incomplete_response" if finish == "length" else f"malformed_response:{type(exc).__name__}"
        calls, assistant = [], {}
    if not thinking:
        thinking = [ThinkingUnavailable(reason="service supplied no public thinking content, count or signature; opaque blocks remain in raw response")]
    payload = ModelResponsePayload(request_event_id=request_event_id, visible_text=tuple(visible), tool_calls=tuple(calls),
        thinking=tuple(thinking), usage=usage, raw_response=store.capture(raw, force_blob=True))
    return ParsedResponse(payload, assistant, finish, error)


def without_cache(block):
    return {k: v for k, v in block.items() if k != "cache_control"}


def present_tools(store, body, request, response, context_event_id):
    """Record both the logical tool view and its actual native wire blocks."""
    from src.harness_contracts import ToolPresentationPayload
    delivered = {e.payload.tool_execution_event_id for e in store.events if e.payload.event_type == "tool_presentation"}
    blocks = [b for m in body["messages"] for b in m["content"]]
    results = {b["tool_use_id"]: b for b in blocks if b["type"] == "tool_result"}
    for event in store.events:
        p = event.payload
        if p.event_type != "tool_execution" or event.event_id in delivered or p.call_id not in results or p.shown_result.kind == "missing":
            continue
        original = store.resolve(p.shown_result)
        kept, native_images = [], []
        for logical in original.get("image_blocks", []):
            native = native_blocks([logical])[0]
            match = next((b for b in blocks if without_cache(b) == native), None)
            if match is not None:
                kept.append(logical)
                native_images.append(match)
        result = results[p.call_id]
        logical_message = {"role": "tool", "tool_call_id": p.call_id,
            "content": "".join(b["text"] for b in result["content"] if b["type"] == "text")}
        if (logical_message != original["tool_message"] or kept != original.get("image_blocks", [])) and context_event_id is None:
            raise ValueError("changed logical tool presentation requires context evidence")
        actual = {**original, "tool_message": logical_message, "image_blocks": kept, "wire_tool_result": result,
            "wire_image_blocks": native_images,
            "conversion": "logical tool acknowledgement and following images converted to native Messages user blocks; image bytes unchanged"}
        store.append(ToolPresentationPayload(tool_execution_event_id=event.event_id,
            request_event_id=request.event_id, response_event_id=response.event_id,
            shown_result=store.capture(actual, force_blob=True), context_event_id=context_event_id,
            protocol_conversion="anthropic_messages_v1"))
