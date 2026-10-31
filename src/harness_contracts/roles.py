"""Pure role definitions and model bindings."""

from __future__ import annotations

from typing import Literal

from pydantic import model_validator

from .base import ContractModel, NonEmptyStr
from .budget import BudgetAmounts
from .refs import SourceRef


class ToolGrant(ContractModel):
    tool_name: NonEmptyStr
    access: Literal["read", "write"]


class InputMaterialRequirement(ContractModel):
    name: NonEmptyStr
    media_type: NonEmptyStr
    required: bool = True


class ReturnRequirement(ContractModel):
    name: NonEmptyStr
    schema_ref: NonEmptyStr


class RoleDefinition(ContractModel):
    role_id: NonEmptyStr
    responsibilities: tuple[NonEmptyStr, ...]
    tool_whitelist: tuple[ToolGrant, ...]
    input_materials: tuple[InputMaterialRequirement, ...]
    return_requirements: tuple[ReturnRequirement, ...]
    budget: BudgetAmounts
    read_only: bool = False

    @model_validator(mode="after")
    def validate_role(self) -> RoleDefinition:
        if not self.responsibilities:
            raise ValueError("a role needs at least one responsibility")
        if not self.input_materials:
            raise ValueError("a role must state its input material contract")
        if not self.return_requirements:
            raise ValueError("a role must state its return contract")
        names = [grant.tool_name for grant in self.tool_whitelist]
        if len(names) != len(set(names)):
            raise ValueError("tool whitelist contains duplicate names")
        if self.read_only and any(grant.access == "write" for grant in self.tool_whitelist):
            raise ValueError("a read-only role cannot whitelist write access")
        if not any(
            value is not None and value > 0
            for value in (
                self.budget.tokens,
                self.budget.money_usd,
                self.budget.money_cny,
                self.budget.seconds,
                self.budget.calls,
            )
        ):
            raise ValueError("a role needs at least one positive budget dimension")
        return self


class ModelRouteRef(ContractModel):
    route_id: NonEmptyStr
    model_alias: NonEmptyStr


class ValidatedScope(ContractModel):
    model: ModelRouteRef
    scope: NonEmptyStr
    evidence: tuple[SourceRef, ...]

    @model_validator(mode="after")
    def require_evidence(self) -> ValidatedScope:
        if not self.evidence:
            raise ValueError("validated scope needs evidence")
        return self


class ModelBinding(ContractModel):
    role_id: NonEmptyStr
    default_model: ModelRouteRef
    recommended_models: tuple[ModelRouteRef, ...] = ()
    validated_scopes: tuple[ValidatedScope, ...] = ()
    failure_policy: Literal["stop_and_report"] = "stop_and_report"

    @model_validator(mode="after")
    def recommendations_are_not_fallbacks(self) -> ModelBinding:
        keys = [
            (item.route_id, item.model_alias)
            for item in (self.default_model, *self.recommended_models)
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("default and recommended models must be distinct")
        declared = set(keys)
        for item in self.validated_scopes:
            key = (item.model.route_id, item.model.model_alias)
            if key not in declared:
                raise ValueError("validated scope refers to an undeclared model")
        return self


def authorize_tool_call(
    role: RoleDefinition, tool_name: str, required_access: Literal["read", "write"]
) -> ToolGrant:
    """Return the matching grant or reject the call before execution."""

    grant = next((item for item in role.tool_whitelist if item.tool_name == tool_name), None)
    if grant is None:
        raise ValueError(f"tool is not whitelisted for role {role.role_id}: {tool_name}")
    if required_access == "write" and grant.access != "write":
        raise ValueError(f"role {role.role_id} has no write grant for {tool_name}")
    return grant


def initial_model_for(binding: ModelBinding) -> ModelRouteRef:
    """Select only the declared default; recommendations require a new decision."""

    return binding.default_model
