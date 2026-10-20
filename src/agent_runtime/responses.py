"""Stateless ChatGPT subscription Responses protocol, with no credential discovery.

The local output allowance reserves budget; the subscription preview does not
accept a wire output cap. Full opaque output items are journalled and replayed.
Protocol source: https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
from collections import Counter
from contextlib import AsyncExitStack
from dataclasses import replace
from time import perf_counter

import httpx

from src.harness_contracts import (AdapterRequestPayload, ImageTransmission,
    InjectedContent, ModelResponsePayload, ParameterAudit, ParametersNotReported,
    ParametersUnverified, ResponseToolCall, ThinkingSummary, ThinkingTokenCount,
    ThinkingUnavailable, UsageMissing, UsageReported)
from .adapter import ParsedResponse, PreparedRequest, decode_image_url
from .estimation import (RequestTokenEstimate, approximate_text_tokens,
    estimate_chat_request, get_model_profile)
from .failures import (ModelServiceError, _sanitize, http_failure, service_diagnostics,
    sse_failure_classification, transport_diagnostics)
from .store import json_bytes


ENDPOINT = "https://api.openai.com/v1/responses"
TOOL_NAMESPACE = "runtime"
# Preview-unsupported fields may be audited locally, but never reach the wire.
_OMITTED = frozenset({"max_tokens", "max_completion_tokens", "max_output_tokens",
    "temperature", "top_p", "top_logprobs", "background", "conversation",
    "max_tool_calls", "metadata", "moderation", "multi_agent", "prompt",
    "prompt_cache_retention", "safety_identifier", "truncation", "user",
    "previous_response_id"})
_ALLOWED = frozenset({"reasoning_effort", "reasoning", "text", "tool_choice",
    "parallel_tool_calls", "prompt_cache_key", "service_tier"})


def _allowance(parameters):
    value = parameters.get("max_tokens", parameters.get("max_completion_tokens",
        parameters.get("max_output_tokens")))
    if type(value) is not int or value <= 0:
        raise ValueError("explicit positive local output allowance required")
    return value


def _content(content, *, assistant=False):
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "output_text" if assistant else "input_text", "text": content}]
    if not isinstance(content, list):
        raise ValueError("message content must be text or blocks")
    blocks = []
    for block in content:
        kind = block.get("type")
        if kind in {"text", "input_text", "output_text"}:
            text = block.get("text")
            if not isinstance(text, str):
                raise ValueError("invalid text block")
            blocks.append({"type": "output_text" if assistant else "input_text", "text": text})
        elif kind in {"image_url", "input_image"} and not assistant:
            image = block["image_url"]
            url = image["url"] if isinstance(image, dict) else image
            decode_image_url(url)
            detail = image.get("detail", block.get("detail", "high")) if isinstance(image, dict) else block.get("detail", "high")
            if detail not in {"auto", "low", "high", "original"}:
                raise ValueError("invalid image detail")
            blocks.append({"type": "input_image", "image_url": url, "detail": detail})
        else:
            raise ValueError("unsupported logical Responses content block")
    return blocks


def _convert_messages(messages):
    native, origins = [], []
    for index, message in enumerate(messages):
        role = message["role"]
        if role == "assistant" and "responses_output" in message:
            items = message["responses_output"]
            if not isinstance(items, list) or not all(isinstance(item, dict) and isinstance(item.get("type"), str) for item in items):
                raise ValueError("invalid recorded Responses output")
            native.extend(copy.deepcopy(items))
            origins.extend([index] * len(items))
            continue
        if role == "tool":
            item = {"type": "function_call_output", "call_id": message["tool_call_id"],
                    "output": _content(message.get("content"))}
            native.append(item)
            origins.append(index)
            continue
        if role not in {"system", "developer", "user", "assistant"}:
            raise ValueError("unsupported message role")
        blocks = _content(message.get("content"), assistant=role == "assistant")
        if blocks:
            native.append({"role": "developer" if role == "system" else role, "content": blocks})
            origins.append(index)
        if role == "assistant":
            for call in message.get("tool_calls", []):
                arguments = call["function"]["arguments"]
                if not isinstance(json.loads(arguments), dict):
                    raise ValueError("tool arguments must be an object")
                native.append({"type": "function_call", "call_id": call["id"],
                    "name": call["function"]["name"], "namespace": call.get("namespace", TOOL_NAMESPACE),
                    "arguments": arguments})
                origins.append(index)
    return native, origins


def build_responses_body(*, model, messages, tools, parameters):
    """Build the exact subscription body without a store or credentials."""
    _allowance(parameters)
    unknown = set(parameters) - _OMITTED - _ALLOWED
    if unknown:
        raise ValueError("unreviewed Responses parameters: " + ", ".join(sorted(unknown)))
    native, _ = _convert_messages(messages)
    body = {"model": model, "input": native, "store": False, "stream": True,
            "include": ["reasoning.encrypted_content"]}
    body.update({key: copy.deepcopy(value) for key, value in parameters.items() if key in _ALLOWED and key != "reasoning_effort"})
    if "reasoning_effort" in parameters:
        if "reasoning" in parameters:
            raise ValueError("reasoning and reasoning_effort cannot both be specified")
        body["reasoning"] = {"effort": parameters["reasoning_effort"]}
    if tools:
        functions, seen = [], set()
        for tool in tools:
            if tool.get("type") != "function":
                raise ValueError("only locally executed function tools are supported")
            function = tool["function"]
            name = function["name"]
            if not isinstance(name, str) or not name or name in seen:
                raise ValueError("invalid or duplicate local function name")
            seen.add(name)
            functions.append({"type": "function", "name": name,
                "description": function.get("description", ""),
                "parameters": copy.deepcopy(function["parameters"]), "strict": False})
        body["tools"] = [{"type": "namespace", "name": TOOL_NAMESPACE,
                          "description": "Locally executed runtime tools", "tools": functions}]
    return body


def _walk_images(value, pointer=""):
    if isinstance(value, dict):
        if value.get("type") == "input_image":
            yield pointer + "/image_url", value
        for key, child in value.items():
            yield from _walk_images(child, pointer + "/" + key.replace("~", "~0").replace("/", "~1"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_images(child, pointer + f"/{index}")


def _item_key(item):
    return hashlib.sha256(json_bytes(item)).hexdigest()


def _mutable_runtime_state(item):
    content = item.get("content")
    return (item.get("role") == "user" and isinstance(content, list)
            and any(isinstance(block.get("text"), str)
                    and block["text"].startswith("Current runtime state (machine generated;")
                    for block in content if isinstance(block, dict)))


class ResponsesUsageCalibration:
    """Per-role/model admission estimates backed by retained usage receipts.

    A compatible anchor's *entire* actual input is charged when all immutable
    input items are still present. Its former mutable state is not subtracted:
    charging it again is conservative. Newly added items get separate estimates.
    Removing old interactions falls back to an older compatible anchor. No
    cached tokens are subtracted, and no ciphertext is interpreted as text.
    """

    def __init__(self, model):
        self.model = model
        self.anchors = []
        self.item_tokens = {}
        self.item_estimates = {}
        self.last_input_tokens = None
        self.largest_growth = 0

    @staticmethod
    def scope(body):
        return _item_key({key: value for key, value in body.items() if key != "input"})

    def observe(self, body, raw, *, response_event_id):
        usage = raw.get("usage") or {}
        actual = usage.get("input_tokens")
        if body.get("model") != self.model or type(actual) is not int or actual < 0:
            return
        if self.last_input_tokens is not None:
            self.largest_growth = max(self.largest_growth, actual - self.last_input_tokens)
        self.last_input_tokens = actual
        attribution = (usage.get("attribution") or {}).get("items") or {}
        for item in body.get("input", []):
            value = (attribution.get(item.get("id")) or {}).get("input_tokens")
            if type(value) is int and value >= 0:
                self.item_tokens[_item_key(item)] = value
        output = raw.get("output") or []
        reasoning = (usage.get("output_tokens_details") or {}).get("reasoning_tokens",
                                                                      usage.get("output_tokens"))
        for item in output:
            value = (attribution.get(item.get("id")) or {}).get("output_tokens")
            # Without item attribution charge each opaque item the *whole*
            # reported reasoning count, rather than guessing a split.
            if value is None and item.get("type") == "reasoning":
                value = reasoning
            if type(value) is int and value >= 0:
                self.item_tokens[_item_key(item)] = value
        self.anchors.append({"scope": self.scope(body), "input_tokens": actual,
            "items": Counter(_item_key(item) for item in body.get("input", [])
                             if not _mutable_runtime_state(item)), "event": response_event_id})
        # Keep the initial anchor for post-compaction recovery, plus recent
        # receipts. These are hashes/counts, never duplicate image payloads.
        if len(self.anchors) > 32:
            self.anchors = [self.anchors[0], *self.anchors[-31:]]

    def estimate(self, body, base, *, profile=None, strict=False):
        if body.get("model") != self.model:
            return base
        items = body.get("input", [])
        keys = Counter(_item_key(item) for item in items)
        scope = self.scope(body)
        candidates = [a for a in self.anchors if a["scope"] == scope
                      and not (a["items"] - keys)]
        if not candidates:
            return base
        # Most recent compatible receipt, not a best-case minimum from another
        # role, model, request schema or materially different history.
        anchor = candidates[-1]
        covered = anchor["items"].copy()
        point = anchor["input_tokens"]
        upper = math.ceil(point * 1.10) + 256
        new_items = 0
        for item in items:
            key = _item_key(item)
            if covered[key]:
                covered[key] -= 1
                continue
            new_items += 1
            if key in self.item_tokens:
                count = self.item_tokens[key]
                point += count
                upper += math.ceil(count * 1.10) + 32
            else:
                cache_key = (key, base.output_token_limit)
                if cache_key not in self.item_estimates:
                    self.item_estimates[cache_key] = estimate_responses_request(
                        {"model": self.model, "input": [item]},
                        output_token_limit=base.output_token_limit, profile=profile, strict=strict)
                estimate = self.item_estimates[cache_key]
                point += estimate.input_tokens_estimate
                upper += estimate.input_tokens_upper_bound
        return replace(base, input_tokens_estimate=point, input_tokens_upper_bound=upper,
            fits_context=(None if base.context_window_tokens is None else
                upper + base.output_token_limit + base.reasoning_token_allowance <= base.context_window_tokens),
            source=base.source + f"; observed_input_anchor={anchor['event']}; incremental_items={new_items}",
            uncertainty=base.uncertainty + " Actual input anchor includes cached tokens; local 10%+256 "
                "history margin and separately estimated increments are not provider guarantees.")


def estimate_responses_request(body, *, output_token_limit, profile=None, strict=False):
    """Estimate actual Responses history/schema; output allowance is local only."""
    if type(output_token_limit) is not int or output_token_limit <= 0:
        raise ValueError("explicit positive local output allowance required")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError("request body needs a model")
    selected = profile or get_model_profile(model, strict=strict)
    text_body = copy.deepcopy(body)
    opaque_allowance = 0
    for item in text_body.get("input", []):
        if item.get("type") == "reasoning" and item.get("encrypted_content"):
            item["encrypted_content"] = "[opaque reasoning; token count unavailable]"
            # A standalone estimator has no receipt. Runtime calibration uses
            # observed reasoning tokens instead; never tokenize ciphertext.
            opaque_allowance += output_token_limit
    images = []
    from src.harness_contracts.events import _resolve_json_pointer
    for reference, block in _walk_images(body):
        # Delegate all model-specific image formulas to the shared estimator.
        single = estimate_chat_request({"model": model, "max_tokens": output_token_limit,
            "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {
                "url": block["image_url"], "detail": block.get("detail", "high")}}]}]}, profile=selected)
        images.append(replace(single.images[0], request_reference=reference))
        _resolve_json_pointer(text_body, reference.rsplit("/", 1)[0])["image_url"] = "[captured image]"
    text_tokens = approximate_text_tokens(json_bytes(text_body).decode("utf-8")) + selected.text_estimate_offset
    image_tokens = sum(image.tokens for image in images)
    upper = math.ceil(text_tokens * selected.text_safety_factor) + selected.text_fixed_margin + image_tokens + opaque_allowance
    allowance = selected.reasoning_token_allowance
    fits = None if selected.context_window_tokens is None else upper + output_token_limit + allowance <= selected.context_window_tokens
    return RequestTokenEstimate(model, selected.canonical_name, text_tokens, image_tokens,
        text_tokens + image_tokens + opaque_allowance, upper, output_token_limit, allowance,
        selected.context_window_tokens, fits,
        f"responses_json_v1; profile={selected.canonical_name}; text={selected.text_estimator}; image={selected.image_estimator}; safety_margin={selected.safety_margin_source}",
        f"{selected.context_uncertainty} {selected.image_uncertainty} Local output allowance is not a provider cap. {selected.reasoning_allowance_source}", tuple(images))


def estimate_responses_messages(*, model, messages, tools, parameters, strict=False, profile=None):
    return estimate_responses_request(build_responses_body(model=model, messages=messages,
        tools=tools, parameters=parameters), output_token_limit=_allowance(parameters), profile=profile, strict=strict)


def prepare_responses_request(*, store, model, messages, message_sources, tools,
        tool_source, parameters, versions, image_originals=None, model_profile=None,
        strict_model_profile=False, reasoning_history="all"):
    if len(messages) != len(message_sources):
        raise ValueError("every request message needs a recorded source")
    if reasoning_history != "all":
        raise ValueError("ChatGPT subscription requires reasoning_history=all to preserve opaque output items")
    body = build_responses_body(model=model, messages=messages, tools=tools, parameters=parameters)
    _, origins = _convert_messages(messages)
    injections = [InjectedContent(request_location=f"/input/{i}",
        content=store.capture(item), source=message_sources[origin])
        for i, (item, origin) in enumerate(zip(body["input"], origins, strict=True))]
    if tools:
        injections.append(InjectedContent(request_location="/tools", content=store.capture(body["tools"]), source=tool_source))
    images = []
    for reference, block in _walk_images(body):
        data, mime = decode_image_url(block["image_url"])
        sent = store.put_bytes(data, mime)
        original = (image_originals or {}).get(sent.sha256, sent)
        store.get_bytes(original)
        images.append(ImageTransmission(original=original, sent=sent, request_reference=reference))
    wire = json_bytes(body)
    captured = store.capture(body, force_blob=True)
    if store.capture_bytes(captured) != wire:
        raise ValueError("stored Responses request differs from wire")
    payload = AdapterRequestPayload(adapter="openai-responses-http-v1", final_request_body=captured,
        wire_sha256=hashlib.sha256(wire).hexdigest(), injected_content=tuple(injections), images=tuple(images),
        parameters=ParameterAudit(requested=copy.deepcopy(parameters),
            provider_report=ParametersNotReported(reason="service does not attest applied parameters"),
            effect=ParametersUnverified(reason="subscription preview omits unsupported wire fields; output allowance is local only")), versions=versions)
    estimate = estimate_responses_request(body, output_token_limit=_allowance(parameters), profile=model_profile, strict=strict_model_profile)
    return PreparedRequest(body, wire, estimate, payload)


def _usage(raw):
    value = raw.get("usage") if isinstance(raw, dict) else None
    # Usage is numeric accounting data, never arbitrary provider error text.
    def numeric(value):
        if isinstance(value, dict):
            return {key: clean for key, child in value.items() if (clean := numeric(child)) is not None}
        return value if type(value) is int and value >= 0 else None
    clean = numeric(value)
    return UsageReported(raw_usage=clean) if clean else UsageMissing(reason="service omitted per-request usage")


def parse_responses_response(raw, request_event_id, store):
    usage = _usage(raw)
    visible, thinking, calls, runtime_calls = [], [], [], []
    assistant, error = {}, None
    finish = "length" if raw.get("status") == "incomplete" and (raw.get("incomplete_details") or {}).get("reason") == "max_output_tokens" else "stop"
    try:
        if raw.get("object") != "response" or not isinstance(raw.get("output"), list):
            raise ValueError("invalid Responses object")
        seen = set()
        for item in raw["output"]:
            kind = item["type"]
            if kind == "message":
                if item.get("role") != "assistant" or not isinstance(item.get("content"), list):
                    raise ValueError("invalid output message")
                for block in item["content"]:
                    if block.get("type") == "output_text":
                        if not isinstance(block.get("text"), str):
                            raise ValueError("invalid output text")
                        if block["text"].strip():
                            visible.append(block["text"])
                    elif block.get("type") == "refusal" and isinstance(block.get("refusal"), str) and block["refusal"].strip():
                        visible.append(block["refusal"])
            elif kind == "reasoning":
                for summary in item.get("summary", []):
                    if summary.get("type") == "summary_text" and isinstance(summary.get("text"), str) and summary["text"].strip():
                        thinking.append(ThinkingSummary(summary=summary["text"]))
            elif kind == "function_call" and raw.get("status") == "completed":
                if item.get("status", "completed") != "completed":
                    raise ValueError("unfinished function call")
                namespace = item.get("namespace")
                if namespace not in {None, TOOL_NAMESPACE}:
                    raise ValueError("foreign function namespace")
                call = ResponseToolCall(call_id=item["call_id"], tool_name=item["name"], full_arguments=json.loads(item["arguments"]))
                if call.call_id in seen:
                    raise ValueError("duplicate function call identity")
                seen.add(call.call_id)
                calls.append(call)
                runtime_call = {"type": "function", "id": call.call_id,
                    "function": {"name": call.tool_name, "arguments": item["arguments"]}}
                if namespace is not None:
                    runtime_call["namespace"] = namespace
                runtime_calls.append(runtime_call)
            # Unknown output items remain opaque, complete and replayable.
        count = ((raw.get("usage") or {}).get("output_tokens_details") or {}).get("reasoning_tokens")
        if type(count) is int and count >= 0:
            thinking.append(ThinkingTokenCount(tokens=count))
        assistant = {"role": "assistant", "content": "\n\n".join(visible) or None,
                     "responses_output": copy.deepcopy(raw["output"])}
        if calls:
            assistant["tool_calls"] = runtime_calls
            finish = "tool_calls"
        if raw.get("status") != "completed":
            error = "incomplete_response"
        elif not visible and not calls:
            error = "empty_response"
    except (ValueError, TypeError, KeyError, AttributeError):
        error = "malformed_response"
        calls, assistant = [], {}
    if not thinking:
        thinking = [ThinkingUnavailable(reason="no public reasoning summary or count; opaque output items retained for replay")]
    payload = ModelResponsePayload(request_event_id=request_event_id, visible_text=tuple(visible), tool_calls=tuple(calls),
        thinking=tuple(thinking), usage=usage, raw_response=store.capture(raw, force_blob=True))
    return ParsedResponse(payload, assistant, finish, error)


class HttpResponsesAdapter:
    """One OAuth request per send. Token renewal belongs to the injected provider.

Only response.completed is a successful terminal object. response.incomplete
returns failure evidence for the parser/truncation journal, never executable calls.
    last_received_usage is per-request observed evidence, not a final settlement
    total when the stream fails or is cancelled without terminal usage.
"""

    def __init__(self, *, token_provider, transport=None):
        self._token_provider = token_provider
        self.client = httpx.AsyncClient(transport=transport, follow_redirects=False)
        self.last_send_timing = {}
        self.last_received_usage = UsageMissing(reason="no request usage received")

    async def close(self):
        await self.client.aclose()

    async def send_reconnecting(self, request, *, timeout, recovery_timeout, on_connected):
        """Bound only token/header acquisition; an established stream keeps its normal I/O limit."""
        return await self.send(request, timeout=timeout, recovery_timeout=recovery_timeout,
                               on_connected=on_connected)

    async def send(self, request: PreparedRequest, *, timeout: float,
                   recovery_timeout=None, on_connected=None):
        token = ""
        self.last_send_timing = {}
        self.last_received_usage = UsageMissing(reason="current request has not reported usage")
        started = perf_counter()
        phase, response_headers = "awaiting_response_headers", None
        recovery_deadline = started + recovery_timeout if recovery_timeout is not None else None
        async def acquire(awaitable):
            if recovery_deadline is None:
                return await awaitable
            return await asyncio.wait_for(awaitable, timeout=max(0.0, recovery_deadline - perf_counter()))
        def failure(category, description, raw=None, retryable=False, details=None):
            # Mid-stream observations do not establish the final total. A
            # missing terminal usage must keep the runtime's unknown hold.
            usage = _usage(raw)
            diagnostics = service_diagnostics({}, secret=token, headers=response_headers)
            diagnostics.pop("body_excerpt")
            diagnostics.update(details or {})
            if diagnostics.get("retry_hint") is not None:
                retryable = False
            error = ModelServiceError({"category": category, "retryable": retryable,
                "body_excerpt": _sanitize(description, token)[:2048],
                **diagnostics,
                "usage_received": usage.kind == "reported"}, usage)
            error.observed_usage = self.last_received_usage
            return error
        try:
            try:
                token = await acquire(self._token_provider())
            except TimeoutError:
                if recovery_deadline is not None:
                    raise
                raise failure("permission_denied", "OAuth token provider failed") from None
            except Exception:
                raise failure("permission_denied", "OAuth token provider failed") from None
            if not isinstance(token, str) or not token or token.startswith("sk-") or any(character.isspace() for character in token):
                token = ""  # Error sanitization must never call str.replace(None).
                raise failure("permission_denied", "OAuth bearer token unavailable or invalid")
            async with AsyncExitStack() as stack:
                response = await acquire(stack.enter_async_context(self.client.stream(
                    "POST", ENDPOINT, content=request.wire_bytes,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                             "Accept": "text/event-stream"}, timeout=timeout)))
                header_seconds = perf_counter() - started
                self.last_send_timing["response_headers_wait_seconds"] = header_seconds
                if on_connected is not None:
                    on_connected(header_seconds)
                response_headers = response.headers
                phase = "reading_response_stream"
                if not response.is_success:
                    await response.aread()
                    error = http_failure(response, secret=token)
                    try:
                        error.usage = _usage(response.json())
                    except ValueError:
                        error.usage = _usage({})
                    error.details["usage_received"] = error.usage.kind == "reported"
                    self.last_received_usage = error.usage
                    error.observed_usage = self.last_received_usage
                    raise error
                done_items, data = {}, []
                async def event():
                    if not data:
                        return None
                    event_raw = json.loads("\n".join(data))
                    if not isinstance(event_raw, dict):
                        raise ValueError("invalid SSE object")
                    kind = event_raw.get("type")
                    value = event_raw.get("response")
                    frame_usage = _usage(value if isinstance(value, dict) else event_raw)
                    if frame_usage.kind == "reported":
                        self.last_received_usage = frame_usage
                    if kind == "response.output_item.done":
                        index, item = event_raw["output_index"], event_raw["item"]
                        if type(index) is not int or index < 0 or not isinstance(item, dict):
                            raise ValueError("invalid output item")
                        if index in done_items and done_items[index] != item:
                            raise ValueError("conflicting output item")
                        done_items[index] = copy.deepcopy(item)
                    if kind in {"response.failed", "error"}:
                        envelope = value if isinstance(value, dict) else event_raw
                        reason = envelope.get("error") or envelope.get("incomplete_details") or (event_raw if kind == "error" else {})
                        details = service_diagnostics(reason, secret=token, headers=response_headers, envelope=envelope)
                        category, retryable = sse_failure_classification(reason, retry_hint=details["retry_hint"])
                        raise failure(category, "Responses service failure", envelope,
                            retryable=retryable, details=details)
                    if kind in {"response.completed", "response.incomplete"}:
                        expected = "completed" if kind == "response.completed" else "incomplete"
                        if not isinstance(value, dict) or value.get("object") != "response" or value.get("status") != expected or not isinstance(value.get("output"), list):
                            raise ValueError("invalid terminal response")
                        result = copy.deepcopy(value)
                        def diagnostic(value):
                            if isinstance(value, str):
                                return _sanitize(value, token)
                            if isinstance(value, dict):
                                return {key: diagnostic(child) for key, child in value.items()}
                            if isinstance(value, list):
                                return [diagnostic(child) for child in value]
                            return value
                        for key in ("error", "incomplete_details"):
                            if key in result:
                                result[key] = diagnostic(result[key])
                        # output_item.done is the complete opaque replay evidence.
                        for index, item in sorted(done_items.items()):
                            if index < len(result["output"]):
                                final = result["output"][index]
                                if any(key in final and final[key] != child for key, child in item.items()):
                                    raise ValueError("conflicting completed output")
                                result["output"][index] = {**item, **final}
                            elif index == len(result["output"]):
                                result["output"].append(item)
                            else:
                                raise ValueError("missing output item")
                        return result
                    return None
                async for line in response.aiter_lines():
                    if not line:
                        result = await event()
                        data = []
                        if result is not None:
                            return result
                    elif line.startswith("data:"):
                        data.append(line[5:].lstrip(" "))
                if data:
                    result = await event()
                    if result is not None:
                        return result
                raise failure("transport_error", "Responses stream ended without response.completed", retryable=True,
                    details={"transport_phase": phase})
        except ModelServiceError:
            raise
        except httpx.TimeoutException as exc:
            raise failure("timeout", "Responses stream timed out", retryable=True,
                details=transport_diagnostics(exc, phase=phase)) from None
        except TimeoutError as exc:
            raise failure("timeout", "Responses response-header acquisition timed out", retryable=True,
                details=transport_diagnostics(exc, phase=phase)) from None
        except httpx.TransportError as exc:
            raise failure("transport_error", "Responses stream transport interrupted", retryable=True,
                details=transport_diagnostics(exc, phase=phase)) from None
        except (ValueError, TypeError, KeyError, AttributeError):
            raise failure("invalid_response", "Invalid Responses stream protocol") from None
        finally:
            self.last_send_timing["http_round_trip_seconds"] = perf_counter() - started


def present_tools(store, body, request, response, context_event_id):
    """Record logical tool results alongside actual native Responses input."""
    from src.harness_contracts import ToolPresentationPayload
    delivered = {event.payload.tool_execution_event_id for event in store.events if event.payload.event_type == "tool_presentation"}
    results = {item["call_id"]: item for item in body["input"] if item.get("type") == "function_call_output"}
    blocks = [block for item in body["input"] for block in (item.get("content") or item.get("output") or []) if isinstance(block, dict)]
    for event in store.events:
        payload = event.payload
        if payload.event_type != "tool_execution" or event.event_id in delivered or payload.call_id not in results or payload.shown_result.kind == "missing":
            continue
        original = store.resolve(payload.shown_result)
        result = results[payload.call_id]
        output = result["output"]
        logical_message = {"role": "tool", "tool_call_id": payload.call_id,
            "content": output if isinstance(output, str) else "".join(block["text"] for block in output if block.get("type") == "input_text")}
        kept, native_images = [], []
        for logical in original.get("image_blocks", []):
            native = _content([logical])[0]
            if native in blocks:
                kept.append(logical)
                native_images.append(native)
        if (logical_message != original["tool_message"] or kept != original.get("image_blocks", [])) and context_event_id is None:
            raise ValueError("changed logical tool presentation requires context evidence")
        actual = {**original, "tool_message": logical_message, "image_blocks": kept,
            "wire_tool_result": result, "wire_image_blocks": native_images,
            "conversion": "logical tool result and images converted to Responses input; image bytes unchanged"}
        store.append(ToolPresentationPayload(tool_execution_event_id=event.event_id,
            request_event_id=request.event_id, response_event_id=response.event_id,
            shown_result=store.capture(actual, force_blob=True), context_event_id=context_event_id,
            protocol_conversion="responses_v1"))
