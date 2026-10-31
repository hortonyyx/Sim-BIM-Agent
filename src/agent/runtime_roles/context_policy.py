"""Effective context configuration for role mode."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from src.agent_runtime.context import ContextPolicy


class EffectiveRoleContext(BaseModel):
    """The complete, persisted context settings used by one runtime role."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    context_tokens: int | None = Field(default=None, ge=1)
    active_window_messages: int | None = Field(default=None, ge=1)
    compact_at_tokens: int | None = Field(default=None, ge=1)
    compact_to_ratio: float = Field(gt=0, lt=1)
    large_result_bytes: int = Field(ge=1)
    max_images: int | None = Field(default=None, ge=1)
    max_image_bytes: int | None = Field(default=None, ge=1)


def effective_role_context(role_id: str, *, global_defaults: Mapping[str, object] | None = None,
                           role_overrides: object = None) -> EffectiveRoleContext:
    """Resolve role defaults, explicit CLI-wide values, then role overrides."""
    base = role_context_policy(role_id)
    values = {"context_tokens": None, **{key: value for key, value in base.model_dump().items()
        if key in EffectiveRoleContext.model_fields}}
    for source in (global_defaults or {},
                   role_overrides.model_dump(mode="json", exclude_unset=True)
                   if hasattr(role_overrides, "model_dump") else (role_overrides or {})):
        values.update({key: value for key, value in source.items()
                       if key in EffectiveRoleContext.model_fields and (value is not None
                           or key in {"context_tokens", "compact_at_tokens"})})
    return EffectiveRoleContext(**values)


def context_policy_from_effective(role_id: str, value: EffectiveRoleContext) -> ContextPolicy:
    fields = ContextPolicy.model_fields
    return role_context_policy(role_id, **{key: item for key, item in
        value.model_dump(mode="json").items() if key in fields})


def role_context_policy(role_id: str, *, base: ContextPolicy | None = None, **overrides) -> ContextPolicy:
    """Keep reader artifacts retrievable without repeating their entire ledger.

    Keep exact delivery references/status in the coordinator's mutable tail;
    role_state and read_role_artifact expose the complete records. All roles
    compact to 30%: long reader repairs also need headroom before another prefix
    rewrite. Full history, image bytes and evidence remain retrievable.
    """
    if role_id not in {"coordinator", "plan_reader", "elevation_reader"}:
        raise ValueError(f"unknown context role: {role_id}")
    values = (base or ContextPolicy(compact_at_tokens=None)).model_dump()
    values["compact_to_ratio"] = 0.3
    if role_id == "coordinator":
        values["state_item_fields"] = {**values.get("state_item_fields", {}), "reader-artifacts": (
            "task_id", "role_id", "target", "status", "artifact", "reason",
        )}
    values.update(overrides)
    return ContextPolicy(**values)
