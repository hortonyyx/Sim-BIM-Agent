"""Explicit Chat Completions wire adapter: no SDK mutation, retry or fallback."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from src.harness_contracts import (
    AdapterRequestPayload, BlobCapture, ImageTransmission, InjectedContent,
    InlineCapture, ModelResponsePayload, ParameterAudit, ParametersNotReported,
    ParametersUnverified, PublicThinking, ResponseToolCall, ThinkingSignature,
    ThinkingSummary, ThinkingTokenCount, ThinkingUnavailable, UsageMissing,
    UsageReported,
)
from .estimation import ModelProfile, RequestTokenEstimate, estimate_chat_request
from .store import EventStore, json_bytes


@dataclass(frozen=True)
class PreparedRequest:
    body: dict
    wire_bytes: bytes
    token_estimate: RequestTokenEstimate
    event_payload: AdapterRequestPayload

    @property
    def token_reservation_estimate(self) -> int:
        return self.token_estimate.reservation_tokens

    @property
    def output_token_limit(self) -> int:
        return self.body.get("max_tokens", self.body.get("max_completion_tokens",
            self.token_estimate.output_token_limit))

    @property
    def input_token_upper_bound(self) -> int:
        return self.token_estimate.input_tokens_upper_bound

    @property
    def context_window_tokens(self) -> int | None:
        return self.token_estimate.context_window_tokens

    @property
    def estimate_source(self) -> str:
        return self.token_estimate.source


@dataclass(frozen=True)
class ParsedResponse:
    event_payload: ModelResponsePayload
    assistant_message: dict
    finish_reason: str | None
    protocol_error: str | None


def decode_image_url(url: str) -> tuple[bytes, str]:
    if not url.startswith("data:image/") or ";base64," not in url:
        raise ValueError("images must use captured data URLs, not uncaptured remote URLs")
    header, encoded = url.split(",", 1)
    return base64.b64decode(encoded, validate=True), header[5:].split(";", 1)[0]


def reasoning_history_messages(messages: list[dict], policy: str, message_sources=None) -> list[dict]:
    """Keep audited history intact; select reasoning only for the wire request.

    A chain includes successive tool-call batches for one real user turn.
    Tool pictures and runtime state carried as user messages do not end it.
    Older completed turns lose reasoning; their visible content remains intact.
    """
    if policy not in {"all", "current_tool_chain"}:
        raise ValueError(f"unsupported reasoning_history: {policy}")
    if policy == "all":
        return messages
    keep = set()
    for i in range(len(messages) - 1, -1, -1):
        message = messages[i]
        if message.get("role") == "assistant":
            if not message.get("tool_calls"):
                break
            keep.add(i)
        # Tool pictures and machine-generated state do not start a user turn.
        if message.get("role") == "user":
            if message_sources is not None:
                real_user = message_sources[i].source_kind == "user"
            else:
                content = message.get("content")
                real_user = not (isinstance(content, list) and content and all(
                    block.get("type") == "image_url" for block in content))
            if real_user:
                break
    return [{key: value for key, value in message.items()
             if i in keep or message.get("role") != "assistant"
             or key not in {"reasoning_content", "reasoning"}}
            for i, message in enumerate(messages)]


def prepare_request(*, store: EventStore, model: str, messages: list[dict],
                    message_sources: list, tools: list[dict], tool_source,
                    parameters: dict, versions, image_originals: dict | None = None,
                    model_profile: ModelProfile | None = None,
                    strict_model_profile: bool = False,
                    reasoning_history: str = "all"):
    if versions.remote_model.route_id == "deepseek" and reasoning_history != "all":
        raise ValueError("DeepSeek tool calls require reasoning_history=all")
    if versions.remote_model.route_id == "opencode-go":
        from .opencode_go import model_contract, model_profile as go_profile, prepare_request as prepare_go
        if model_contract(model)[0] != "openai_chat_completions":
            return prepare_go(store=store, model=model, messages=messages, message_sources=message_sources,
                tools=tools, tool_source=tool_source, parameters=parameters, versions=versions,
                image_originals=image_originals, strict_model_profile=strict_model_profile,
                reasoning_history=reasoning_history)
        model_profile = go_profile(model)
    if versions.remote_model.route_id == "chatgpt-subscription":
        from .responses import prepare_responses_request
        return prepare_responses_request(store=store, model=model, messages=messages,
            message_sources=message_sources, tools=tools, tool_source=tool_source,
            parameters=parameters, versions=versions, image_originals=image_originals,
            model_profile=model_profile, strict_model_profile=strict_model_profile,
            reasoning_history=reasoning_history)
    if versions.remote_model.route_id == "glm-subscription-anthropic":
        from .anthropic import prepare_anthropic_request
        return prepare_anthropic_request(store=store, model=model, messages=messages,
            message_sources=message_sources, tools=tools, tool_source=tool_source,
            parameters=parameters, versions=versions, image_originals=image_originals,
            model_profile=model_profile, strict_model_profile=strict_model_profile)
    forbidden = {"model", "messages", "tools", "stream", "n"} & parameters.keys()
    if forbidden:
        raise ValueError("request parameters cannot override model, messages, tools, stream or n")
    if len(message_sources) != len(messages):
        raise ValueError("every request message needs a recorded source")
    selected = reasoning_history_messages(messages, reasoning_history, message_sources)
    message_sources = [source if selected[i] == message else store.source(
        "reasoning-history-selection", {"policy": reasoning_history,
            "original_source": source.model_dump(mode="json"),
            "removed_fields": sorted(set(message) - set(selected[i]))})
        for i, (message, source) in enumerate(zip(messages, message_sources))]
    messages = selected
    if versions.remote_model.route_id == "opencode-go":
        from .opencode_go import chat_messages
        messages, message_sources = chat_messages(store, model, messages, message_sources)
    body = {"model": model, "messages": copy.deepcopy(messages),
            "stream": False, "n": 1, **copy.deepcopy(parameters)}
    if versions.remote_model.route_id == "deepseek":
        body.pop("n")
    if versions.remote_model.route_id in {"glm-subscription", "opencode-go"}:
        # No unverified sampling count on the Coding Plan endpoint. Capture the
        # resulting body below so the journal remains identical to wire bytes.
        body.pop("n")
        allowed = ({"max_completion_tokens"}
                   if versions.remote_model.route_id == "opencode-go" and model == "mimo-v2.6-flash"
                   else {"max_tokens", "temperature", "reasoning_effort"})
        unsupported = set(parameters) - allowed
        if unsupported:
            raise ValueError("unreviewed subscription chat parameters: " + ", ".join(sorted(unsupported)))
    if tools:
        body["tools"] = copy.deepcopy(tools)
        body.setdefault("tool_choice", "auto")
    injections = [InjectedContent(request_location=f"/messages/{i}",
        content=store.capture(message), source=source)
        for i, (message, source) in enumerate(zip(messages, message_sources))]
    if tools:
        injections.append(InjectedContent(request_location="/tools",
            content=store.capture(tools), source=tool_source))
    images = []
    for i, message in enumerate(body["messages"]):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for j, block in enumerate(content):
            if block.get("type") != "image_url":
                continue
            data, mime = decode_image_url(block["image_url"]["url"])
            sent = store.put_bytes(data, mime)
            original = (image_originals or {}).get(sent.sha256, sent)
            images.append(ImageTransmission(original=original, sent=sent,
                request_reference=f"/messages/{i}/content/{j}/image_url/url"))
    wire = json_bytes(body)
    payload = AdapterRequestPayload(adapter="chat-completions-http-v1",
        final_request_body=InlineCapture(value=body),
        wire_sha256=hashlib.sha256(wire).hexdigest(),
        injected_content=tuple(injections), images=tuple(images),
        parameters=ParameterAudit(requested=copy.deepcopy(parameters),
            provider_report=ParametersNotReported(reason="request has no provider parameter attestation"),
            effect=ParametersUnverified(reason="compatible services may ignore parameters; response is not an attestation")),
        versions=versions)
    # Validate actual bytes and every injected blob BEFORE externalizing body.
    for injection in injections:
        from src.harness_contracts.events import _resolve_json_pointer
        if store.resolve(injection.content) != _resolve_json_pointer(body, injection.request_location):
            raise ValueError("injected attachment does not match wire request")
    for transmission in images:
        from src.harness_contracts.events import _resolve_json_pointer
        raw, _ = decode_image_url(_resolve_json_pointer(body, transmission.request_reference))
        if hashlib.sha256(raw).hexdigest() != transmission.sent.sha256:
            raise ValueError("sent image hash differs from final request")
        store.get_bytes(transmission.original)
    captured = store.capture(body, force_blob=True)
    if store.capture_bytes(captured) != wire:
        raise ValueError("stored request differs from wire bytes")
    payload = payload.model_copy(update={"final_request_body": captured})
    output_limit = parameters.get("max_tokens", parameters.get("max_completion_tokens"))
    if type(output_limit) is not int or output_limit <= 0:
        raise ValueError("explicit positive output token cap required")
    token_estimate = estimate_chat_request(body, profile=model_profile,
                                           strict=strict_model_profile)
    return PreparedRequest(body, wire, token_estimate, payload)


class HttpChatAdapter:
    """Credentials are used only in the header, never serialized to the journal."""

    def __init__(self, *, base_url: str, api_key: str, transport=None):
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base URL must be HTTPS and contain no credentials/query/fragment")
        self.endpoint = base_url.rstrip("/") + "/chat/completions"
        self.failure_provider = "glm" if parsed.hostname == "open.bigmodel.cn" else None
        self._key = api_key
        self.client = httpx.AsyncClient(transport=transport, follow_redirects=False)

    async def close(self):
        await self.client.aclose()

    async def send(self, request: PreparedRequest, *, timeout: float):
        # Sending these bytes bypasses SDK extra_body merges and hidden retries.
        self.last_send_timing = {}
        started = time.monotonic()
        try:
            response = await self.client.post(self.endpoint, content=request.wire_bytes,
                headers={"Authorization": f"Bearer {self._key}",
                         "Content-Type": "application/json"}, timeout=timeout)
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


class ScriptedAdapter:
    """Offline test model. Calls exactly the same request/response audit path."""

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def send(self, request: PreparedRequest, *, timeout: float):
        self.requests.append(request.wire_bytes)
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response(request.body)
        return copy.deepcopy(response)


def reported_tokens(usage) -> int | None:
    from src.harness_contracts.usage import reported_total_tokens
    return reported_total_tokens(usage.raw_usage) if usage.kind == "reported" else None


def parse_response(raw: Any, request_event_id: str, store: EventStore,
                   *, echo_fields=("reasoning_content",)) -> ParsedResponse:
    if isinstance(raw, dict) and raw.get("object") == "response":
        from .responses import parse_responses_response
        return parse_responses_response(raw, request_event_id, store)
    if isinstance(raw, dict) and raw.get("type") == "message":
        from .anthropic import parse_anthropic_response
        return parse_anthropic_response(raw, request_event_id, store)
    captured = store.capture(raw, force_blob=True)
    usage_raw = raw.get("usage") if isinstance(raw, dict) else None
    usage = UsageReported(raw_usage=usage_raw) if isinstance(usage_raw, dict) and usage_raw else UsageMissing(reason="service omitted per-request usage")
    error, calls, visible, thinking, finish, assistant = None, [], [], [], None, {}
    try:
        if not isinstance(raw, dict) or not isinstance(raw.get("choices"), list) or len(raw["choices"]) != 1:
            raise ValueError("expected one response choice")
        choice = raw["choices"][0]
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            raise ValueError("choice and message must be objects")
        finish = choice.get("finish_reason")
        message = choice["message"]
        if message.get("role") != "assistant":
            raise ValueError("response message is not assistant")
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            visible.append(content)
        elif content is not None and not isinstance(content, str):
            raise ValueError("unsupported assistant content form")
        public = message.get("reasoning_content", message.get("reasoning"))
        if isinstance(public, str) and public.strip():
            thinking.append(PublicThinking(content=public))
        summary = message.get("reasoning_summary")
        if isinstance(summary, str) and summary.strip():
            thinking.append(ThinkingSummary(summary=summary))
        signature = message.get("thinking_signature")
        if isinstance(signature, str) and signature.strip():
            thinking.append(ThinkingSignature(signature=signature))
        if usage.kind == "reported":
            details = usage_raw.get("completion_tokens_details") or {}
            count = details.get("reasoning_tokens", usage_raw.get("reasoning_tokens"))
            if type(count) is int and count >= 0:
                thinking.append(ThinkingTokenCount(tokens=count))
        seen = set()
        # An output-limit response is an indivisible, rejected batch. Even a
        # syntactically complete call beside a partial call must not be exposed
        # as executable. The exact partial bytes remain in raw_response.
        raw_calls = [] if finish == "length" else message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise ValueError("tool_calls must be an array")
        for call in raw_calls:
            if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                raise ValueError("tool call and function must be objects")
            if call.get("type") != "function" or call.get("id") in seen:
                raise ValueError("invalid or duplicate tool call identity")
            arguments = json.loads(call["function"]["arguments"])
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be an object")
            calls.append(ResponseToolCall(call_id=call["id"],
                tool_name=call["function"]["name"], full_arguments=arguments))
            seen.add(call["id"])
        assistant = {"role": "assistant", "content": content}
        if calls:
            assistant["tool_calls"] = copy.deepcopy(message["tool_calls"])
        if set(echo_fields) & {"role", "content", "tool_calls"}:
            raise ValueError("provider echo fields cannot overwrite protocol fields")
        for field in echo_fields:
            if field in message:
                assistant[field] = copy.deepcopy(message[field])
        if finish not in {"stop", "tool_calls"}:
            error = "incomplete_response"
        elif calls and finish != "tool_calls":
            error = "inconsistent_finish_reason"
        elif (finish == "tool_calls" and not calls) or (not calls and not visible):
            error = "empty_response"
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        # Preserve the full raw object; never execute a partially parsed batch.
        error = "incomplete_response" if finish == "length" else f"malformed_response:{type(exc).__name__}"
        calls, assistant = [], {}
    if not thinking:
        thinking = [ThinkingUnavailable(reason="service exposed no reasoning content, summary, count or signature")]
    payload = ModelResponsePayload(request_event_id=request_event_id,
        visible_text=tuple(visible), tool_calls=tuple(calls), thinking=tuple(thinking),
        usage=usage, raw_response=captured)
    return ParsedResponse(payload, assistant, finish, error)


def convert_tool_result(call_id: str, raw: dict, store: EventStore, *, image_detail_supported=True):
    """Chat tool messages hold text; exact images follow all ID acknowledgements.

    Return the full presentation including image blocks, not just a summary.
    Unsupported MCP content is a visible protocol error, never silently dropped.
    """
    texts, pictures, image_refs, omitted_hints = [], [], [], []
    for part in raw.get("content", []):
        if part.get("type") == "text":
            texts.append(part["text"])
        elif part.get("type") == "image":
            data = base64.b64decode(part["data"], validate=True)
            ref = store.put_bytes(data, part["mimeType"])
            image_refs.append(ref)
            image_url = {"url": f"data:{part['mimeType']};base64,{part['data']}"}
            detail = (part.get("_meta") or {}).get("codex/imageDetail")
            if detail is not None:
                if not isinstance(detail, str) or detail not in {"auto", "low", "high", "original"}:
                    raise ValueError("unsupported MCP image detail")
                if image_detail_supported:
                    image_url["detail"] = detail
                else:
                    omitted_hints.append({"image_index": len(image_refs) - 1, "detail": detail})
            pictures += [{"type": "text", "text": f"Tool image: tool_call_id={call_id}."},
                {"type": "image_url", "image_url": image_url}]
        else:
            raise ValueError(f"unsupported MCP content type: {part.get('type')}")
    # MCP commonly supplies the same object as both JSON text and structured
    # content. Keep the original text (including any trailing time notice), and
    # supplement only fields it does not already contain. Audit metadata belongs
    # to the presentation record, not an escaped JSON wrapper sent to the model.
    texts = list(dict.fromkeys(texts))
    structured = raw.get("structuredContent")
    missing = structured
    for text in texts:
        try:
            decoded, _ = json.JSONDecoder().raw_decode(text.lstrip())
        except ValueError:
            continue
        missing = _unrepresented(missing, decoded)
    if missing is not _COVERED and structured is not None:
        texts.append(json.dumps(missing, ensure_ascii=False, separators=(",", ":")))
    if raw.get("isError"):
        texts.insert(0, "Tool error (isError=true):")
    if not texts:
        texts.append("Tool returned images below." if pictures else "Tool completed with no text result.")
    message = {"role": "tool", "tool_call_id": call_id,
               "content": "\n".join(texts)}
    presentation = {"tool_message": message, "image_blocks": pictures,
        "images": [ref.model_dump(mode="json") for ref in image_refs],
        "conversion": "MCP image blocks moved to a user message after all tool-call replies; bytes unchanged"}
    if omitted_hints:
        presentation["omitted_image_display_hints"] = omitted_hints
    return message, pictures, presentation, image_refs


_COVERED = object()


def _unrepresented(value, represented):
    """Only remove provably identical JSON values, never approximate prose."""
    if value is _COVERED:
        return value
    if json_bytes(value) == json_bytes(represented):
        return _COVERED
    if isinstance(value, dict) and isinstance(represented, dict):
        remaining = {}
        for key, item in value.items():
            rest = _unrepresented(item, represented[key]) if key in represented else item
            if rest is not _COVERED:
                remaining[key] = rest
        return remaining if remaining else _COVERED
    return value
