"""Typed event payloads for the model/tool harness boundary."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from .base import ContractModel, EventTimestamp, NonEmptyStr, ParentTaskRef, Sha256
from .budget import BudgetAmounts, BudgetReconciliation, BudgetRelease, BudgetReservation, BudgetSettlement, UsageEvidence
from .refs import BlobRef, HashedBlobRef, ImageTransmission, SourceRef


class InlineCapture(ContractModel):
    kind: Literal["inline"] = "inline"
    value: JsonValue


class BlobCapture(ContractModel):
    kind: Literal["blob"] = "blob"
    blob: BlobRef


class JsonReferencedCapture(ContractModel):
    kind: Literal["json_references"] = "json_references"
    blob: HashedBlobRef
    wire_sha256: Sha256


class EncodedImageReference(ContractModel):
    location: str  # JSON pointer, including the empty pointer for a root string
    image: HashedBlobRef
    prefix: str = ""
    # Usually absent: retain unusual but valid base64 spellings exactly.
    base64_text: HashedBlobRef | None = None


class ImageReferencedCapture(ContractModel):
    kind: Literal["image_references"] = "image_references"
    blob: HashedBlobRef  # canonical JSON template; image string slots are null
    images: tuple[EncodedImageReference, ...] = Field(min_length=1)
    wire_sha256: Sha256
    serialization: Literal["json_utf8_sorted_compact_v1"] = "json_utf8_sorted_compact_v1"


class MissingCapture(ContractModel):
    kind: Literal["missing"] = "missing"
    reason: NonEmptyStr


CapturedValue = Annotated[
    InlineCapture | BlobCapture | JsonReferencedCapture | ImageReferencedCapture | MissingCapture,
    Field(discriminator="kind"),
]


class InjectedContent(ContractModel):
    request_location: NonEmptyStr
    content: CapturedValue
    source: SourceRef


class PublicThinking(ContractModel):
    kind: Literal["public_content"] = "public_content"
    content: NonEmptyStr


class ThinkingSummary(ContractModel):
    kind: Literal["summary"] = "summary"
    summary: NonEmptyStr


class ThinkingTokenCount(ContractModel):
    kind: Literal["reported_token_count"] = "reported_token_count"
    tokens: int = Field(ge=0)


class ThinkingSignature(ContractModel):
    kind: Literal["signature"] = "signature"
    signature: NonEmptyStr


class ThinkingUnavailable(ContractModel):
    kind: Literal["unavailable"] = "unavailable"
    reason: NonEmptyStr


ThinkingEvidence = Annotated[
    PublicThinking
    | ThinkingSummary
    | ThinkingTokenCount
    | ThinkingSignature
    | ThinkingUnavailable,
    Field(discriminator="kind"),
]


class ParametersReported(ContractModel):
    kind: Literal["reported"] = "reported"
    values: dict[str, JsonValue]


class ParametersNotReported(ContractModel):
    kind: Literal["not_reported"] = "not_reported"
    reason: NonEmptyStr


ParameterReport = Annotated[
    ParametersReported | ParametersNotReported,
    Field(discriminator="kind"),
]


class ParametersVerified(ContractModel):
    kind: Literal["verified"] = "verified"
    evidence: SourceRef


class ParametersUnverified(ContractModel):
    kind: Literal["unverified"] = "unverified"
    reason: NonEmptyStr


ParameterEffect = Annotated[
    ParametersVerified | ParametersUnverified,
    Field(discriminator="kind"),
]


class ParameterAudit(ContractModel):
    requested: dict[str, JsonValue]
    provider_report: ParameterReport
    effect: ParameterEffect

    @model_validator(mode="after")
    def unreported_parameters_cannot_be_verified(self) -> ParameterAudit:
        if self.provider_report.kind == "not_reported" and self.effect.kind == "verified":
            raise ValueError("unreported parameters cannot be marked verified")
        return self


class VersionStamp(ContractModel):
    identifier: NonEmptyStr
    evidence: SourceRef | None = None


class RemoteModelIdentity(ContractModel):
    route_id: NonEmptyStr
    remote_alias: NonEmptyStr
    fixed_revision: NonEmptyStr | None = None
    alias_status: Literal["verified_fixed", "unverified"]
    evidence: SourceRef | None = None

    @model_validator(mode="after")
    def verified_alias_needs_evidence(self) -> RemoteModelIdentity:
        if self.alias_status == "verified_fixed" and (
            self.fixed_revision is None or self.evidence is None
        ):
            raise ValueError("a verified remote alias needs a revision and evidence")
        return self


class VersionManifest(ContractModel):
    code_commit: VersionStamp
    dependency_lock: VersionStamp
    agent_version: VersionStamp | None = None
    runtime_version: VersionStamp | None = None
    domain_version: VersionStamp | None = None
    mode: NonEmptyStr | None = None
    role_models: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)
    prompt: VersionStamp
    tool_definitions: VersionStamp
    inference_parameters: VersionStamp
    model_route: VersionStamp
    remote_model: RemoteModelIdentity


class AdapterRequestPayload(ContractModel):
    event_type: Literal["adapter_request"] = "adapter_request"
    reservation_id: NonEmptyStr | None = None
    logical_purpose: Literal["primary_task", "context_summary"] | None = None
    adapter: NonEmptyStr
    final_request_body: CapturedValue
    # Optional only for journals written before A1-R. New adapters always set it.
    wire_sha256: Sha256 | None = None
    injected_content: tuple[InjectedContent, ...] = ()
    images: tuple[ImageTransmission, ...] = ()
    parameters: ParameterAudit
    versions: VersionManifest

    @model_validator(mode="after")
    def references_match_final_body(self) -> AdapterRequestPayload:
        if self.final_request_body.kind != "inline":
            if self.final_request_body.kind == "missing" and (
                self.injected_content or self.images
            ):
                raise ValueError(
                    "a missing final request cannot claim injected content or image locations"
                )
            return self
        body = self.final_request_body.value
        if not isinstance(body, dict):
            raise ValueError("an inline final request body must be a JSON object")
        for item in self.injected_content:
            actual = _resolve_json_pointer(body, item.request_location)
            if item.content.kind == "inline" and actual != item.content.value:
                raise ValueError(
                    f"injected content differs from final request at {item.request_location}"
                )
        for item in self.images:
            _resolve_json_pointer(body, item.request_reference)
        return self


class ResponseToolCall(ContractModel):
    call_id: NonEmptyStr
    tool_name: NonEmptyStr
    full_arguments: dict[str, JsonValue]


class ModelResponsePayload(ContractModel):
    event_type: Literal["model_response"] = "model_response"
    request_event_id: NonEmptyStr
    visible_text: tuple[NonEmptyStr, ...] = ()
    tool_calls: tuple[ResponseToolCall, ...] = ()
    thinking: tuple[ThinkingEvidence, ...]
    usage: UsageEvidence
    raw_response: CapturedValue

    @model_validator(mode="after")
    def distinguish_thinking_forms(self) -> ModelResponsePayload:
        kinds = [item.kind for item in self.thinking]
        if not kinds:
            raise ValueError("thinking must use an available form or explicit unavailable")
        if len(kinds) != len(set(kinds)):
            raise ValueError("each thinking evidence form may occur at most once")
        if "unavailable" in kinds and len(kinds) != 1:
            raise ValueError("unavailable thinking cannot be combined with available forms")
        call_ids = [item.call_id for item in self.tool_calls]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("response tool call IDs must be unique")
        return self


class TruncationPayload(ContractModel):
    """A discarded output-limit response, with a bounded recovery decision."""

    event_type: Literal["response_truncation"] = "response_truncation"
    request_event_id: NonEmptyStr
    response_event_id: NonEmptyStr
    finish_reason: Literal["length"] = "length"
    thinking_characters: int = Field(ge=0)
    visible_characters: int = Field(ge=0)
    reported_reasoning_tokens: int | None = Field(default=None, ge=0)
    has_tool_calls: bool
    tool_call_count: int | None = Field(default=None, ge=0)
    consecutive_count: int = Field(ge=1)
    total_count: int = Field(ge=1)
    max_consecutive_recoveries: int = Field(ge=0)
    max_total_recoveries: int = Field(ge=0)
    action: Literal["continue", "stop"]
    reason: NonEmptyStr


class AnswerRepairPayload(ContractModel):
    """One bounded request to repair a rejected final answer, and its result."""

    event_type: Literal["answer_repair"] = "answer_repair"
    phase: Literal["request", "result"]
    attempt: int = Field(ge=1)
    invalid_response_event_id: NonEmptyStr
    original_answer: CapturedValue
    validation_error: NonEmptyStr
    repair_request_event_id: NonEmptyStr | None = None
    repaired_response_event_id: NonEmptyStr | None = None
    repaired_answer: CapturedValue | None = None
    accepted: bool | None = None
    repaired_validation_error: NonEmptyStr | None = None

    @model_validator(mode="after")
    def phase_fields_match(self) -> AnswerRepairPayload:
        result_fields = (
            self.repair_request_event_id,
            self.repaired_response_event_id,
            self.repaired_answer,
            self.accepted,
        )
        if self.phase == "request":
            if any(value is not None for value in result_fields) or self.repaired_validation_error is not None:
                raise ValueError("an answer repair request cannot claim a repair result")
            return self
        if any(value is None for value in result_fields):
            raise ValueError("an answer repair result needs its request, response, answer, and verdict")
        if self.accepted and self.repaired_validation_error is not None:
            raise ValueError("an accepted repair cannot retain a validation error")
        if not self.accepted and self.repaired_validation_error is None:
            raise ValueError("a rejected repair needs its validation error")
        return self


class ToolFailureDetails(ContractModel):
    """Sanitized local exception evidence captured after durable tool intent."""

    exception_type: NonEmptyStr
    stage: NonEmptyStr
    diagnostic: NonEmptyStr

    @field_validator("diagnostic")
    @classmethod
    def bounded_diagnostic(cls, value):
        if len(value.encode("utf-8", errors="backslashreplace")) > 2048:
            raise ValueError("tool failure diagnostic exceeds 2 KB")
        return value


class ToolExecutionPayload(ContractModel):
    event_type: Literal["tool_execution"] = "tool_execution"
    call_id: NonEmptyStr
    tool_name: NonEmptyStr
    full_arguments: dict[str, JsonValue]
    raw_result: CapturedValue
    shown_result: CapturedValue
    repeatability: Literal["read_only", "idempotent_write", "non_idempotent_write"]
    outcome: Literal["succeeded", "failed", "unknown"]
    capture_scope: Literal["complete", "historical_excerpt"] = "complete"
    operation_key: NonEmptyStr | None = None
    applied_write_id: NonEmptyStr | None = None
    retry_event_id: NonEmptyStr | None = None
    invocation_event_id: NonEmptyStr | None = None
    presentation_status: Literal["sent", "prepared", "historical_unverified"] = "sent"
    failure: ToolFailureDetails | None = None

    @model_validator(mode="after")
    def validate_execution_result(self) -> ToolExecutionPayload:
        is_write = self.repeatability != "read_only"
        if is_write and self.operation_key is None:
            raise ValueError("write operations need a stable operation_key")
        if not is_write and self.applied_write_id is not None:
            raise ValueError("read-only tools cannot report an applied write")
        if self.outcome == "succeeded":
            if self.failure is not None:
                raise ValueError("a successful execution cannot carry failure details")
            if self.capture_scope == "complete" and (
                self.raw_result.kind == "missing" or self.shown_result.kind == "missing"
            ):
                raise ValueError(
                    "a complete successful execution needs raw and model-visible results"
                )
            if is_write and self.applied_write_id is None:
                raise ValueError("successful writes need an applied_write_id")
        elif self.applied_write_id is not None:
            raise ValueError("only a successful write can have applied_write_id")
        if self.outcome == "unknown" and self.raw_result.kind != "missing":
            raise ValueError("an unknown outcome must not claim a raw result")
        return self


class ToolInvocationPayload(ContractModel):
    """Durable intent written before contacting a possibly mutating tool."""

    event_type: Literal["tool_invocation"] = "tool_invocation"
    call_id: NonEmptyStr
    tool_name: NonEmptyStr
    full_arguments: dict[str, JsonValue]
    repeatability: Literal["read_only", "idempotent_write", "non_idempotent_write"]
    operation_key: NonEmptyStr | None = None
    state_before: HashedBlobRef | None = None

    @model_validator(mode="after")
    def writes_need_identity(self) -> ToolInvocationPayload:
        if self.repeatability != "read_only" and self.operation_key is None:
            raise ValueError("write invocation needs operation_key")
        return self


class ToolPresentationPayload(ContractModel):
    """A service accepted a request containing this previously prepared result."""

    event_type: Literal["tool_presentation"] = "tool_presentation"
    tool_execution_event_id: NonEmptyStr
    request_event_id: NonEmptyStr
    response_event_id: NonEmptyStr
    shown_result: CapturedValue
    context_event_id: NonEmptyStr | None = None
    protocol_conversion: Literal["anthropic_messages_v1", "responses_v1"] | None = None


class StateInspectionPayload(ContractModel):
    event_type: Literal["state_inspection"] = "state_inspection"
    purpose: Literal["unknown_write_recovery", "resume"]
    target_event_id: NonEmptyStr
    persisted_state: HashedBlobRef
    conclusion: Literal["not_applied", "already_applied", "inconclusive", "safe_to_resume"]


class ModelFailureDetails(ContractModel):
    """Sanitized service diagnostics, independent of the retry decision."""

    request_event_id: NonEmptyStr
    category: NonEmptyStr
    retryable: bool
    http_status: int | None = Field(default=None, ge=100, le=599)
    service_error_type: str | None = None
    service_error_code: str | None = Field(default=None, max_length=256)
    retry_hint: str | None = Field(default=None, max_length=256)
    transport_phase: str | None = Field(default=None, max_length=64)
    exception_chain: tuple[str, ...] = Field(default=(), max_length=8)
    os_error_codes: tuple[int, ...] = Field(default=(), max_length=8)
    body_excerpt: str | None = None
    request_id: str | None = None
    usage_received: bool

    @field_validator("exception_chain")
    @classmethod
    def bounded_exception_types(cls, value):
        if any(len(item) > 256 for item in value):
            raise ValueError("model failure exception type exceeds 256 characters")
        return value

    @field_validator("body_excerpt")
    @classmethod
    def bounded_excerpt(cls, value):
        if value is not None and len(value.encode("utf-8")) > 2048:
            raise ValueError("model failure excerpt exceeds 2 KB")
        return value


class RunLifecyclePayload(ContractModel):
    event_type: Literal["run_lifecycle"] = "run_lifecycle"
    action: Literal["start", "stop", "retry", "cancel", "timeout", "resume", "failure"]
    reason: NonEmptyStr
    retry_of_event_id: NonEmptyStr | None = None
    attempt: int | None = Field(default=None, ge=2)
    state_inspection_event_id: NonEmptyStr | None = None
    checkpoint: HashedBlobRef | None = None
    failure_stage: NonEmptyStr | None = None
    model_failure: ModelFailureDetails | None = None
    partial_artifacts: tuple[BlobRef, ...] = ()

    @model_validator(mode="after")
    def validate_action_fields(self) -> RunLifecyclePayload:
        if self.model_failure is not None and self.action not in {"failure", "timeout", "cancel"}:
            raise ValueError("model failure details require a failure, timeout or cancel event")
        if self.action == "retry":
            if self.retry_of_event_id is None or self.attempt is None:
                raise ValueError("retry needs retry_of_event_id and attempt")
        elif self.retry_of_event_id is not None or self.attempt is not None:
            raise ValueError("retry fields are only valid for retry")
        if self.action == "resume":
            if self.checkpoint is None or self.state_inspection_event_id is None:
                raise ValueError("resume needs a checkpoint and prior state inspection")
        elif self.checkpoint is not None:
            raise ValueError("checkpoint is only valid for resume")
        if self.action == "failure" and self.failure_stage is None:
            raise ValueError("failure needs failure_stage")
        if self.action != "failure" and self.failure_stage is not None:
            raise ValueError("failure_stage is only valid for failure")
        return self


class TaskControlPayload(ContractModel):
    """A durable acknowledgement of an operator command at a safe boundary."""

    event_type: Literal["task_control"] = "task_control"
    command_id: NonEmptyStr
    action: Literal["pause", "resume", "message"]
    target_task_id: NonEmptyStr
    command: HashedBlobRef
    message: NonEmptyStr | None = None

    @model_validator(mode="after")
    def message_matches_action(self) -> TaskControlPayload:
        if (self.action == "message") != (self.message is not None):
            raise ValueError("only a message control requires message text")
        return self


class BudgetEventPayload(ContractModel):
    event_type: Literal["budget"] = "budget"
    action: Literal["reserve", "settle", "release", "reconcile"]
    reservation: BudgetReservation | None = None
    settlement: BudgetSettlement | None = None
    release: BudgetRelease | None = None
    reconciliation: BudgetReconciliation | None = None

    @model_validator(mode="after")
    def exactly_one_budget_record(self) -> BudgetEventPayload:
        records = {"reserve": self.reservation, "settle": self.settlement,
                   "release": self.release, "reconcile": self.reconciliation}
        if records[self.action] is None or sum(value is not None for value in records.values()) != 1:
            raise ValueError(f"{self.action} event needs only its matching budget record")
        return self


class BudgetWaitPayload(ContractModel):
    """Observable wait for a live root time reservation to settle."""

    event_type: Literal["budget_wait"] = "budget_wait"
    phase: Literal["begin", "end"]
    wait_id: NonEmptyStr
    reason: NonEmptyStr
    active_hold_ids: tuple[NonEmptyStr, ...]
    root_available: BudgetAmounts
    task_available: BudgetAmounts
    wall_remaining_seconds: Decimal | None = Field(ge=0)
    elapsed_seconds: Decimal | None = Field(default=None, ge=0)
    outcome: Literal["capacity_changed", "wall_deadline_exhausted", "cancelled"] | None = None

    @model_validator(mode="after")
    def phase_fields_match(self) -> BudgetWaitPayload:
        if self.phase == "begin":
            if self.elapsed_seconds is not None or self.outcome is not None:
                raise ValueError("budget wait begin cannot claim an outcome")
        elif self.elapsed_seconds is None or self.outcome is None:
            raise ValueError("budget wait end needs elapsed_seconds and outcome")
        return self


class BudgetOverrunPayload(ContractModel):
    """Auditable explanation for a reported token use above its reservation."""

    event_type: Literal["budget_overrun"] = "budget_overrun"
    model: NonEmptyStr
    reservation_id: NonEmptyStr
    reserved_tokens: int = Field(ge=0)
    actual_tokens: int = Field(ge=0)
    overrun_tokens: int = Field(gt=0)
    reasoning_token_allowance: int = Field(ge=0)
    completion_over_max_tokens: int | None
    within_profile_allowance: bool | None
    root_limit_exceeded: bool
    task_limit_exceeded: bool

    @model_validator(mode="after")
    def overrun_matches_amounts(self) -> BudgetOverrunPayload:
        if self.overrun_tokens != self.actual_tokens - self.reserved_tokens:
            raise ValueError("overrun_tokens must equal actual_tokens minus reserved_tokens")
        return self


class ContextEventPayload(ContractModel):
    event_type: Literal["context"] = "context"
    action: Literal["compact", "retain_image", "remove_image", "retrieve_image"]
    reason: NonEmptyStr
    summary: BlobRef | None = None
    replaced_event_ids: tuple[NonEmptyStr, ...] = ()
    image: BlobRef | None = None
    removal_event_id: NonEmptyStr | None = None
    view_id: NonEmptyStr | None = None
    before: HashedBlobRef | None = None
    after: HashedBlobRef | None = None
    details: HashedBlobRef | None = None

    @model_validator(mode="after")
    def validate_context_action(self) -> ContextEventPayload:
        if self.action == "compact":
            if self.summary is None or not self.replaced_event_ids:
                raise ValueError("compression needs a summary and replaced event IDs")
            if self.image is not None or self.removal_event_id is not None:
                raise ValueError("image fields are not valid for compression")
        elif self.action in {"remove_image", "retain_image"}:
            if self.image is None or self.summary is not None or self.removal_event_id:
                raise ValueError("image removal needs only the image reference")
        elif self.image is None or self.removal_event_id is None or self.summary is not None:
            raise ValueError("image retrieval needs image and removal_event_id")
        return self


class CheckpointPayload(ContractModel):
    """Durable recovery state; the JSON pointer file is only an acceleration."""

    event_type: Literal["checkpoint"] = "checkpoint"
    state: HashedBlobRef
    after_event_id: NonEmptyStr


class NetworkRecoveryPayload(ContractModel):
    """Durable recovery allowance; unfinished reservations remain fully charged."""

    event_type: Literal["network_recovery"] = "network_recovery"
    action: Literal["reserve", "settle"]
    operation_id: NonEmptyStr
    phase: Literal["backoff", "response_headers", "adapter_request"]
    request_event_id: NonEmptyStr
    reserved_seconds: float = Field(ge=0, allow_inf_nan=False)
    elapsed_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def settlement_has_observed_duration(self):
        if (self.action == "settle") != (self.elapsed_seconds is not None):
            raise ValueError("only recovery settlement carries elapsed duration")
        return self


class ExternalCoordinatorMcpPayload(ContractModel):
    event_type: Literal["external_coordinator_mcp"] = "external_coordinator_mcp"
    phase: Literal["dispatch", "operation", "return"]
    coordinator_task_id: NonEmptyStr
    method: NonEmptyStr
    request_content: CapturedValue
    result_content: CapturedValue


class RunAggregateUsagePayload(ContractModel):
    """Run-level receipt data that must not replace per-message usage."""

    event_type: Literal["run_usage_summary"] = "run_usage_summary"
    scope: Literal["run_aggregate"] = "run_aggregate"
    usage: UsageEvidence
    raw_summary: CapturedValue
    notes: tuple[NonEmptyStr, ...] = ()


EventPayload = Annotated[
    AdapterRequestPayload
    | ModelResponsePayload
    | TruncationPayload
    | AnswerRepairPayload
    | ToolExecutionPayload
    | ToolInvocationPayload
    | ToolPresentationPayload
    | StateInspectionPayload
    | RunLifecyclePayload
    | TaskControlPayload
    | BudgetEventPayload
    | BudgetWaitPayload
    | BudgetOverrunPayload
    | ContextEventPayload
    | CheckpointPayload
    | NetworkRecoveryPayload
    | ExternalCoordinatorMcpPayload
    | RunAggregateUsagePayload,
    Field(discriminator="event_type"),
]


class EventEnvelope(ContractModel):
    schema_version: Literal["harness.event.v1"] = "harness.event.v1"
    event_id: NonEmptyStr
    run_id: NonEmptyStr
    task_id: NonEmptyStr
    parent_task: ParentTaskRef
    sequence: int = Field(ge=0)
    occurred_at: EventTimestamp
    source_refs: tuple[SourceRef, ...] = ()
    payload: EventPayload

    @model_validator(mode="after")
    def task_cannot_parent_itself(self) -> EventEnvelope:
        if self.parent_task.kind == "known" and self.parent_task.task_id == self.task_id:
            raise ValueError("task cannot be its own parent")
        return self


def _resolve_json_pointer(document: JsonValue, pointer: str) -> JsonValue:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise ValueError(f"request reference must be a JSON pointer: {pointer}")
    current = document
    for raw_part in pointer[1:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as error:
                raise ValueError(f"request reference does not exist: {pointer}") from error
        else:
            raise ValueError(f"request reference does not exist: {pointer}")
    return current
