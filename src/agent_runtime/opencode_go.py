"""OpenCode Go subscription routing; no metered fallback or credential discovery.

Endpoints: https://opencode.ai/v2/docs/console/go/
Model limits/effort metadata: https://models.dev/api.json (2026-10-14 snapshot).
Catalog exposure is not local protocol/model-profile admission.
"""
from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
from pathlib import Path
import uuid

from src.harness_contracts import (AdapterRequestPayload, ImageTransmission, InjectedContent,
    ParameterAudit, ParametersNotReported, ParametersUnverified)
from .adapter import HttpChatAdapter, PreparedRequest, decode_image_url
from .estimation import estimate_chat_request, get_model_profile
from .store import json_bytes

PROVIDER = "opencode-go"
# Subscription/catalog scope retained in explicit credential files. Inference
# bases are protocol-specific; the catalog URL is not a chat/messages base.
BASE_URL = "https://opencode.ai/inference/go/v1"
PROTOCOL_BASE_URLS = {
    "openai_chat_completions": "https://opencode.ai/inference/go/openai/v1",
    "anthropic_messages": "https://opencode.ai/inference/go/anthropic/v1",
    "openai_responses": "https://opencode.ai/inference/go/openai/v1",
}
# Admitted IDs have model profiles and explicit protocol contracts.
# Other catalog entries, including experimental IDs, fail before a model request.
MODELS = {
    "glm-5.3-flash": ("openai_chat_completions", 1_000_000, ("low", "high", "max")),
    "mimo-v2.6-flash": ("openai_chat_completions", 1_048_576, ()),
    "kimi-k3": ("openai_chat_completions", 1_048_576, ("max",)),
    "minimax-m3": ("anthropic_messages", 1_000_000, ()),
    "qwen3.8-max": ("anthropic_messages", 1_000_000, ("low", "medium", "xhigh")),
    "claude-haiku-5-5": ("anthropic_messages", 1_000_000, ("low", "medium", "high", "xhigh", "max")),
    "gpt-6-luna": ("openai_responses", 1_050_000, ("none", "low", "medium", "high", "xhigh", "max")),
}


def model_contract(model):
    if not isinstance(model, str) or model not in MODELS:
        raise ValueError("OpenCode Go model has no admitted protocol/profile; use one of: " + ", ".join(MODELS))
    return MODELS[model]


def inference_base_url(protocol):
    if protocol not in PROTOCOL_BASE_URLS:
        raise ValueError("unreviewed OpenCode Go transport protocol")
    return PROTOCOL_BASE_URLS[protocol]


def credentials(path):
    if path is None:
        raise ValueError("OpenCode Go requires an explicit credentials file")
    from dotenv import dotenv_values
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError("OpenCode Go credentials file does not exist")
    private = dotenv_values(path, interpolate=False)
    if private.get("OPENCODE_GO_BASE_URL") != BASE_URL:
        raise ValueError("OPENCODE_GO_BASE_URL must be the reviewed Go subscription endpoint")
    key = private.get("OPENCODE_GO_API_KEY")
    if not isinstance(key, str) or not key or any(c.isspace() for c in key):
        raise ValueError("OPENCODE_GO_API_KEY is missing or malformed")
    return BASE_URL, key


def conversation_id(run_directory, task_id):
    """Stable on resume; distinct for concurrent reader contexts; no local path disclosure."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "sim-bim-agent:" +
        str(Path(run_directory).resolve()) + ":" + str(task_id)))


def dns_bootstrap(path):
    """Optional public DNS bootstrap in the same explicit private connection file."""
    from dotenv import dotenv_values
    from .go_direct import DEFAULT_DNS_BOOTSTRAP, public_ipv4
    value = dotenv_values(Path(path).resolve(), interpolate=False).get("OPENCODE_GO_DNS_BOOTSTRAP_IPV4")
    return public_ipv4(value if value is not None else DEFAULT_DNS_BOOTSTRAP)


def parameters(model, *, output_tokens, temperature=None, thinking=True, reasoning_effort=None):
    protocol, _, efforts = model_contract(model)
    if thinking is not True:
        raise ValueError("OpenCode Go uses service reasoning defaults; disabling thinking is not admitted")
    if reasoning_effort is not None and reasoning_effort not in efforts:
        raise ValueError("unreviewed OpenCode Go reasoning_effort for " + model)
    if temperature is not None and (protocol != "openai_chat_completions" or model in {"kimi-k3", "mimo-v2.6-flash"}):
        raise ValueError("OpenCode Go model uses service sampling defaults")
    output_key = "max_completion_tokens" if model == "mimo-v2.6-flash" else "max_tokens"
    result = {output_key: output_tokens}
    if temperature is not None:
        result["temperature"] = temperature
    if reasoning_effort is not None:
        if protocol == "anthropic_messages":
            result["output_config"] = {"effort": reasoning_effort}
        else:
            result["reasoning_effort"] = reasoning_effort
    return result


def chat_messages(store, model, messages, sources):
    """Use MiMo's documented image URL shape without changing pixels/history.

    https://mimo.mi.com/docs/en-US/quick-start/usage-guide/multimodal-understanding/image-understanding
    The local 'original' display hint is not a documented MiMo request field.
    Keep each original source in the projection record; capture projected messages
    before computing wire bytes, estimates and image transmission hashes.
    """
    if model != "mimo-v2.6-flash":
        return messages, sources
    projected, projected_sources = copy.deepcopy(messages), list(sources)
    for i, message in enumerate(projected):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        removed = []
        for j, block in enumerate(content):
            if block.get("type") == "image_url" and "detail" in block["image_url"]:
                removed.append({"path": f"/content/{j}/image_url/detail",
                                "value": block["image_url"].pop("detail")})
        if removed:
            projected_sources[i] = store.source("go-mimo-image-projection", {
                "original_source": sources[i].model_dump(mode="json"),
                "removed_display_hints": removed, "image_bytes_changed": False})
    return projected, projected_sources


def model_profile(model):
    """Endpoint-scoped reservations; do not reuse Paratera calibration as Go evidence."""
    _, cap, _ = model_contract(model)
    selected = get_model_profile(model, strict=True)
    return replace(selected, context_window_tokens=cap, endpoint_context_window_tokens=cap,
        context_source=(selected.context_source if model == "mimo-v2.6-flash"
                        else "https://models.dev/api.json (opencode-go, 2026-10-14)"),
        context_uncertainty="Provider catalog capacity, not an independently saturated endpoint test.",
        text_estimate_offset=0, text_safety_factor=2.0, text_fixed_margin=32,
        safety_margin_source="Local uncalibrated Go 2x text margin; not provider billing evidence.",
        profile_status="provider_catalog_with_unverified_endpoint_image_reservation",
        reasoning_allowance_source="Local output reservation policy; not a validated Go output bound.",
        image_uncertainty="Upstream image sizing or local proxy only; OpenCode Go preprocessing and image billing are not independently calibrated.")


class HttpGoAdapter(HttpChatAdapter):
    def __init__(self, *, base_url, api_key, protocol, session_id, transport=None, dns_bootstrap_ipv4=None):
        if base_url != inference_base_url(protocol):
            raise ValueError("unreviewed OpenCode Go transport route")
        uuid.UUID(session_id)
        if transport is None:
            from .go_direct import direct_transport, DEFAULT_DNS_BOOTSTRAP
            transport = direct_transport(dns_bootstrap_ipv4 or DEFAULT_DNS_BOOTSTRAP)
        super().__init__(base_url=base_url, api_key=api_key, transport=transport, trust_env=False)
        path = {"openai_chat_completions": "/chat/completions",
                "anthropic_messages": "/messages", "openai_responses": "/responses"}[protocol]
        self.endpoint = base_url + path
        self.client.headers.update({"User-Agent": "Sim-BIM-Agent/work-model",
                                    "x-opencode-session": session_id})
        if protocol == "anthropic_messages":
            self.client.headers["anthropic-version"] = "2023-06-01"


def responses_body(*, model, messages, tools, parameters):
    from .responses import build_responses_body
    body = build_responses_body(model=model, messages=messages, tools=tools, parameters=parameters)
    body.update(stream=False, max_output_tokens=parameters["max_tokens"])
    if "tools" in body:
        body["tools"] = body["tools"][0]["tools"]
    for item in body["input"]:
        if item.get("type") == "function_call" and item.get("namespace") == "runtime":
            item.pop("namespace")
    return body


def _messages_tools(tools):
    """Project two-field root presence alternatives; keep local schemas intact.

    Messages rejects root combinators. This narrow wire projection handles only
    two distinct, declared properties, each in a required-only branch. Execution
    still validates the original schema; nested/value constraints are untouched.
    """
    projected = copy.deepcopy(tools)
    for tool in projected:
        schema = tool["function"]["parameters"]
        properties = schema.get("properties")
        if schema.get("type") != "object" or not isinstance(properties, dict):
            continue
        for key in ("oneOf", "anyOf"):
            branches = schema.get(key)
            if (isinstance(branches, list) and len(branches) == 2
                    and all(isinstance(branch, dict) and set(branch) == {"required"}
                            and isinstance(branch["required"], list) and len(branch["required"]) == 1
                            and isinstance(branch["required"][0], str)
                            and branch["required"][0] in properties for branch in branches)
                    and branches[0]["required"] != branches[1]["required"]):
                del schema[key]
    return projected


def estimate_messages(*, model, messages, tools, parameters):
    protocol = model_contract(model)[0]
    if protocol == "openai_responses":
        from .responses import estimate_responses_request
        return estimate_responses_request(responses_body(model=model, messages=messages,
            tools=tools, parameters=parameters), output_token_limit=parameters["max_tokens"],
            profile=model_profile(model), strict=True)
    if protocol == "anthropic_messages":
        from .anthropic import convert_messages
        system, _, native, _ = convert_messages(messages, [None] * len(messages), native_system_only=True)
        body = {"model": model, "system": system, "messages": native, **parameters}
        if tools:
            body["tools"] = [{"name": tool["function"]["name"],
                "description": tool["function"].get("description", ""),
                "input_schema": tool["function"]["parameters"]} for tool in _messages_tools(tools)]
        return estimate_chat_request(body, profile=model_profile(model), strict=True)
    return estimate_chat_request({"model": model, "messages": messages,
        "tools": tools, **parameters}, profile=model_profile(model), strict=True)


def prepare_request(**kwargs):
    protocol = model_contract(kwargs["model"])[0]
    kwargs["model_profile"] = model_profile(kwargs["model"])
    if protocol == "anthropic_messages":
        from .anthropic import prepare_anthropic_request
        kwargs.pop("reasoning_history", None)
        kwargs["tools"] = _messages_tools(kwargs["tools"])
        prepared = prepare_anthropic_request(**kwargs, native_system_only=True,
            check_thinking_bindings=kwargs["model"] == "claude-haiku-5-5")
        audit = prepared.event_payload.parameters.model_copy(update={"effect": ParametersUnverified(
            reason="OpenCode Go Messages mapping; applied server values are not attested")})
        return replace(prepared, event_payload=prepared.event_payload.model_copy(update={"parameters": audit}))
    if protocol == "openai_chat_completions":
        # The shared formatter handles Go's chat route without entering this native path.
        from .adapter import prepare_request as prepare_chat
        return prepare_chat(**kwargs)
    if kwargs.get("reasoning_history", "all") != "all":
        raise ValueError("OpenCode Go Responses requires reasoning_history=all")
    from .responses import _convert_messages, _walk_images, estimate_responses_request
    store, messages, sources = kwargs["store"], kwargs["messages"], kwargs["message_sources"]
    if len(messages) != len(sources):
        raise ValueError("every request message needs a recorded source")
    body = responses_body(model=kwargs["model"], messages=messages, tools=kwargs["tools"],
                          parameters=kwargs["parameters"])
    _, origins = _convert_messages(messages)
    injections = [InjectedContent(request_location=f"/input/{i}", content=store.capture(item),
                                  source=sources[origin])
                  for i, (item, origin) in enumerate(zip(body["input"], origins, strict=True))]
    if kwargs["tools"]:
        injections.append(InjectedContent(request_location="/tools", content=store.capture(body["tools"]),
                                          source=kwargs["tool_source"]))
    images = []
    for reference, block in _walk_images(body):
        data, mime = decode_image_url(block["image_url"])
        sent = store.put_bytes(data, mime)
        original = (kwargs.get("image_originals") or {}).get(sent.sha256, sent)
        store.get_bytes(original)
        images.append(ImageTransmission(original=original, sent=sent, request_reference=reference))
    wire, captured = json_bytes(body), store.capture(body, force_blob=True)
    if store.capture_bytes(captured) != wire:
        raise ValueError("stored OpenCode Go request differs from wire")
    payload = AdapterRequestPayload(adapter="openai-responses-http-v1", final_request_body=captured,
        wire_sha256=hashlib.sha256(wire).hexdigest(), injected_content=tuple(injections), images=tuple(images),
        parameters=ParameterAudit(requested=copy.deepcopy(kwargs["parameters"]),
            provider_report=ParametersNotReported(reason="Go does not attest applied parameters"),
            effect=ParametersUnverified(reason="Explicit output cap transmitted; not independently attested")),
        versions=kwargs["versions"])
    estimate = estimate_responses_request(body, output_token_limit=kwargs["parameters"]["max_tokens"],
                                          profile=kwargs["model_profile"], strict=True)
    return PreparedRequest(body, wire, estimate, payload)
