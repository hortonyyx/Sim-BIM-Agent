"""Explicit role bindings for configurable chat services (no model calls).

Capabilities describe the selected deployment, not a model-name heuristic.
Specialized OCR/embedding protocols must use their own adapters.
"""

from __future__ import annotations

import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


class _StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ChatService(_StrictConfig):
    provider: str = Field(min_length=1)
    base_url: str | None = None
    api_key_env: str = Field(min_length=1)


class ChatProfile(_StrictConfig):
    service: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    temperature: float = Field(ge=0)
    max_tokens: int = Field(gt=0)
    extra_body: dict[str, Any] | None = None
    # Missing/None is unknown, never implied support.
    capabilities: dict[str, StrictBool | None] = Field(default_factory=dict)


class RoleBinding(_StrictConfig):
    model: str = Field(min_length=1)
    requires: list[str] = Field(default_factory=list)


class ModelRoutes(_StrictConfig):
    schema_version: Literal[1]
    services: dict[str, ChatService]
    models: dict[str, ChatProfile]
    roles: dict[str, RoleBinding]
    default_role: str | None = None

    @model_validator(mode="after")
    def check_bindings(self) -> ModelRoutes:
        if self.default_role is not None and self.default_role not in self.roles:
            raise ValueError(f"default_role {self.default_role!r} is not configured")
        for name, profile in self.models.items():
            if profile.service not in self.services:
                raise ValueError(f"model profile {name!r} has an unknown service")
        for role, binding in self.roles.items():
            if binding.model not in self.models:
                raise ValueError(f"role {role!r} has an unknown model profile")
            profile = self.models[binding.model]
            missing = [key for key in binding.requires
                       if profile.capabilities.get(key) is not True]
            if missing:
                raise ValueError(f"role {role!r} needs unconfirmed capabilities: {missing}")
        return self

    def chat_config(self, role: str | None) -> dict[str, Any]:
        """Return the existing LLMConfig contract; resolve only this key.

        Explicit unknown roles never fall back to the default. Credentials are
        looked up only here, keeping unrelated services usable without keys.
        """
        selected = role if role is not None else self.default_role
        if selected not in self.roles:
            raise ValueError(f"chat role {selected!r} is not configured")
        profile = self.models[self.roles[selected].model]
        service = self.services[profile.service]
        key = os.environ.get(service.api_key_env)
        if not key or not key.strip():
            raise ValueError(f"missing credential environment variable {service.api_key_env}")
        return dict(provider=service.provider, base_url=service.base_url,
                    api_key=key, model_name=profile.model_name,
                    temperature=profile.temperature, max_tokens=profile.max_tokens,
                    extra_body=profile.extra_body)
