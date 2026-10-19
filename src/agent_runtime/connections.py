"""Resolve live connections once for both transport and public run identity."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict

from .providers import (
    CHATGPT_SUBSCRIPTION,
    CHATGPT_BASE_URL,
    DEEPSEEK,
    GLM_SUBSCRIPTION_ANTHROPIC,
    OPENCODE_GO,
    SUBSCRIPTION_PROVIDERS,
    subscription_credentials,
)


_SENSITIVE_QUERY_NAMES = frozenset({
    "access_key", "access_token", "api_key", "apikey", "auth", "authorization",
    "credential", "key", "password", "secret", "signature", "token",
})


class ConnectionDescriptor(BaseModel):
    """Public connection identity safe to persist in version evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    route_id: str
    base_url: str
    billing_mode: str
    adapter_kind: str

    def model_route(self, model: str) -> dict:
        return {"route_id": self.route_id, "model": model,
                **self.model_dump(mode="json", exclude={"route_id"})}


def _public_base_url(value: str) -> str:
    """Validate the exact URL that will be given to an HTTP adapter.

    Userinfo and sensitive query parameters must never become version evidence.
    The adapters already reject every query and fragment, so use the same public
    value for transport instead of recording a sanitized URL that was not sent.
    """
    parsed = urlsplit(value)
    if parsed.username or parsed.password:
        raise ValueError("base URL must not contain userinfo or credentials")
    sensitive = sorted({name for name, _ in parse_qsl(parsed.query, keep_blank_values=True)
                        if name.casefold() in _SENSITIVE_QUERY_NAMES})
    if sensitive:
        raise ValueError("base URL contains sensitive query parameters: " + ", ".join(sensitive))
    if parsed.scheme != "https" or parsed.query or parsed.fragment:
        raise ValueError("base URL must be HTTPS and contain no query/fragment")
    return value


def paratera_credentials(path: Path | None) -> tuple[str, str]:
    """Read only the two permitted keys, without sourcing/exporting an env file."""
    values = {}
    if path:
        from dotenv import dotenv_values
        private = dotenv_values(path, interpolate=False)
        values = {name: private.get(name) for name in ("PARATERA_BASE_URL", "PARATERA_API_KEY")}
    base_url = values.get("PARATERA_BASE_URL") or os.environ.get(
        "PARATERA_BASE_URL", "https://llmapi.paratera.com/v1")
    key = values.get("PARATERA_API_KEY") or os.environ.get("PARATERA_API_KEY")
    if not key:
        raise ValueError("PARATERA_API_KEY was not supplied")
    return _public_base_url(base_url), key


def deepseek_credentials(path: Path | None) -> tuple[str, str]:
    """Use only the explicitly selected file and the official DeepSeek endpoint."""
    if path is None or not Path(path).is_file():
        raise ValueError("DeepSeek requires an explicit existing credentials file")
    from dotenv import dotenv_values
    private = dotenv_values(path, interpolate=False)
    base_url, key = private.get("DEEPSEEK_BASE_URL"), private.get("DEEPSEEK_API_KEY")
    if base_url not in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}:
        raise ValueError("DEEPSEEK_BASE_URL must be the reviewed official HTTPS endpoint")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("DEEPSEEK_API_KEY is missing from the specified credentials file")
    return base_url, key


@dataclass(frozen=True)
class ResolvedConnection:
    """A private credential paired with its serializable public identity."""

    descriptor: ConnectionDescriptor
    api_key: str = field(default="", repr=False)
    subscription: object | None = field(default=None, repr=False, compare=False)

    def create_adapter(self, *, session_id=None):
        if self.descriptor.route_id == OPENCODE_GO:
            from .opencode_go import HttpGoAdapter
            return HttpGoAdapter(base_url=self.descriptor.base_url, api_key=self.api_key,
                protocol=self.descriptor.adapter_kind, session_id=session_id)
        if self.descriptor.route_id == CHATGPT_SUBSCRIPTION:
            from .responses import HttpResponsesAdapter
            return HttpResponsesAdapter(token_provider=self.subscription.get_access_token)
        from .adapter import HttpChatAdapter
        from .anthropic import HttpAnthropicAdapter
        adapter_type = (HttpAnthropicAdapter
                        if self.descriptor.adapter_kind == "anthropic_messages"
                        else HttpChatAdapter)
        return adapter_type(base_url=self.descriptor.base_url, api_key=self.api_key)


def resolve_connection(provider: str, credentials_file: Path | None,
                       *, chatgpt_auth_dir: Path | None = None, model=None) -> ResolvedConnection:
    """Resolve one reviewed live route without exposing its credential."""
    if provider == OPENCODE_GO:
        from .opencode_go import credentials, inference_base_url, model_contract
        protocol = model_contract(model)[0]
        _, key = credentials(credentials_file)
        return ResolvedConnection(descriptor=ConnectionDescriptor(route_id=provider,
            base_url=inference_base_url(protocol), billing_mode="subscription", adapter_kind=protocol), api_key=key)
    if provider == CHATGPT_SUBSCRIPTION:
        from .openai_subscription import SubscriptionCredentials
        return ResolvedConnection(descriptor=ConnectionDescriptor(
            route_id=provider, base_url=CHATGPT_BASE_URL,
            billing_mode="subscription", adapter_kind="openai_responses"),
            subscription=SubscriptionCredentials(chatgpt_auth_dir))
    if provider in SUBSCRIPTION_PROVIDERS:
        base_url, key = subscription_credentials(credentials_file, provider=provider)
        billing_mode = "subscription"
    elif provider == "paratera":
        base_url, key = paratera_credentials(credentials_file)
        billing_mode = "metered"
    elif provider == DEEPSEEK:
        from .providers import validate_provider_model
        validate_provider_model(provider, model)
        base_url, key = deepseek_credentials(credentials_file)
        billing_mode = "metered"
    else:
        raise ValueError(f"no live connection resolver for provider {provider!r}")
    base_url = _public_base_url(base_url)
    descriptor = ConnectionDescriptor(
        route_id=provider,
        base_url=base_url,
        billing_mode=billing_mode,
        adapter_kind=("anthropic_messages"
                      if provider == GLM_SUBSCRIPTION_ANTHROPIC
                      else "openai_chat_completions"),
    )
    return ResolvedConnection(descriptor=descriptor, api_key=key)


async def validate_connection_model(connection: ResolvedConnection, model: str) -> None:
    """Check the selected subscription's actual catalog before any model request."""
    if connection.descriptor.route_id != CHATGPT_SUBSCRIPTION:
        return
    models = await connection.subscription.list_models()
    if model not in {item.slug for item in models}:
        raise ValueError("selected model is not available in this ChatGPT subscription catalog; run the models command")
