"""Strict model routing for the drawing-only role-division runtime."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_serializer, model_validator

from src.agent_runtime.estimation import get_model_profile
from src.agent_runtime.output_limits import validate_output_limit
from src.agent_runtime.providers import (
    LIVE_PROVIDERS,
    provider_parameters,
    validate_provider_model,
)


ROLE_NAMES = ("coordinator", "plan_reader", "elevation_reader")


class RoleContextOverrides(BaseModel):
    """Optional per-role overrides applied after explicit CLI-wide values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    context_tokens: int | None = Field(default=None, ge=1)
    compact_at_tokens: int | None = Field(default=None, ge=1)
    active_window_messages: int | None = Field(default=None, ge=1)
    large_result_bytes: int | None = Field(default=None, ge=1)
    max_images: int | None = Field(default=None, ge=1)
    max_image_bytes: int | None = Field(default=None, ge=1)

    @model_serializer(mode="wrap")
    def serialize_overrides(self, handler):
        # Explicit null on these fields restores automatic capacity management;
        # an omitted field still inherits the case-wide setting.
        automatic = {"context_tokens", "compact_at_tokens"}
        return {key: value for key, value in handler(self).items()
                if value is not None or (key in automatic and key in self.model_fields_set)}


class RoleConfiguration(BaseModel):
    """One explicit, reviewed chat route used by a runtime role."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    reasoning_effort: str | None = Field(default=None, min_length=1)
    output_tokens: int = Field(gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)
    enable_thinking: bool | None = Field(default=None, strict=True)
    context: RoleContextOverrides | None = None

    @model_serializer(mode="wrap")
    def serialize_route(self, handler):
        # Optional role knobs must not change existing GLM configuration bytes.
        return {key: value for key, value in handler(self).items() if value is not None}

    @model_validator(mode="after")
    def validate_reviewed_route(self, info: ValidationInfo) -> "RoleConfiguration":
        if self.enable_thinking is not None and self.reasoning_effort is not None:
            raise ValueError("choose reasoning_effort or enable_thinking, not both")
        allow_scripted = bool((info.context or {}).get("allow_scripted", False))
        if self.provider == "scripted" or self.model == "scripted-model":
            if allow_scripted and self.provider == "scripted" and self.model == "scripted-model":
                return self
            raise ValueError("scripted role routes are admitted only by an explicit offline runtime")
        if self.provider not in LIVE_PROVIDERS:
            raise ValueError(f"role provider {self.provider!r} is not a reviewed live provider")
        validate_provider_model(self.provider, self.model)
        get_model_profile(self.model, strict=True)
        provider_parameters(
            self.provider,
            model=self.model,
            output_tokens=self.output_tokens,
            reasoning_effort=self.reasoning_effort,
            temperature=self.temperature,
            thinking=True if self.enable_thinking is None else self.enable_thinking,
        )
        validate_output_limit(self.model, self.output_tokens)
        return self


def load_roles(value: object, *, allow_scripted: bool = False) -> dict[str, RoleConfiguration]:
    """Validate the complete fixed role set without defaults or inheritance."""

    if not isinstance(value, Mapping):
        raise ValueError("role_division requires an explicit roles object")
    supplied = set(value)
    required = set(ROLE_NAMES)
    missing = sorted(required - supplied)
    unexpected = sorted(supplied - required)
    if missing or unexpected:
        details = []
        if missing:
            details.append("missing roles: " + ", ".join(missing))
        if unexpected:
            details.append("unexpected roles: " + ", ".join(unexpected))
        raise ValueError("role_division identities are fixed; " + "; ".join(details))
    return {name: RoleConfiguration.model_validate(
        value[name], context={"allow_scripted": allow_scripted}) for name in ROLE_NAMES}
