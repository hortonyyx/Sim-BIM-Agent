"""Audited long-context projection with lossless history and image retrieval.

The manager owns no model client and contains no building-domain imports.  It
keeps the durable history, current structured state, and the request projection
as separate things.  A caller may supply a summary callback, but that callback
must use the runtime's normal request/budget/event path.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import inspect
import json
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from src.harness_contracts import (
    ContextEventPayload,
    EventEnvelope,
    HashedBlobRef,
    SourceRef,
)
from src.harness_contracts.base import ContractModel, NonEmptyStr

from .store import EventStore, json_bytes


StateCategory = Literal[
    "user_requirement",
    "constraint",
    "evidence_reference",
    "artifact_version",
    "unresolved",
    "todo",
    "dimension",
    "object_id",
    "geometry",
    "general",
]
EpistemicStatus = Literal[
    "user_stated", "observed", "computed", "inferred", "assumed", "unresolved"
]
HistoryKind = Literal["message", "tool_result", "summary"]

_PROTECTED_CATEGORIES = frozenset(
    {"artifact_version", "dimension", "object_id", "geometry"}
)
_CHECKLIST_CATEGORIES: tuple[StateCategory, ...] = (
    "user_requirement",
    "constraint",
    "evidence_reference",
    "artifact_version",
    "unresolved",
    "todo",
    "dimension",
    "object_id",
    "geometry",
    "general",
)


class ContextPolicy(ContractModel):
    """Deterministic request projection policy."""

    # Message windows exist only for explicit historical replay. Production
    # defaults preserve an append-only prefix until this input-token threshold.
    active_window_messages: int | None = Field(default=None, ge=1)
    # None delegates pressure/retention to the runtime's model window and
    # observed request growth. Explicit thresholds remain useful for tests.
    compact_at_tokens: int | None = Field(default=None, ge=1)
    compact_to_ratio: float = Field(default=0.6, gt=0, lt=1)
    large_result_bytes: int = Field(default=8_192, ge=1)
    pinned_tags: tuple[NonEmptyStr, ...] = ()
    preserve_initial_messages: int = Field(default=2, ge=0)
    max_images: int | None = Field(default=None, ge=1)
    # Opt-in image-only projection. Initial inputs, undelivered tool results
    # and exact retrievals are always retained, even beyond this batch target.
    image_history_batches: int | None = Field(default=None, ge=1,
        exclude_if=lambda value: value is None)
    # In token mode this is a post-compaction retention target, not permission
    # to rewrite previously sent images on every request below the threshold.
    max_image_bytes: int | None = Field(default=32_000_000, ge=1)
    # Opt-in domain projection for list-valued state. Full entries, provenance
    # and revisions remain in checkpoints; only the mutable request tail is
    # narrowed to caller-selected reference/status fields. Omit the empty
    # default so existing checkpoint configuration comparisons stay identical.
    state_item_fields: dict[NonEmptyStr, tuple[NonEmptyStr, ...]] = Field(
        default_factory=dict, exclude_if=lambda value: not value)
    # Sent state is part of the request prefix for caching and signed histories.
    # Commit actually sent state snapshots before their response, then append
    # only changed state. Ordinary projections and estimators stay read-only.
    # Keep the serialized legacy default: old checkpoints omit this field.
    # Production entry enables it for new token-managed conversations.
    append_state_history: bool = Field(default=False, exclude_if=lambda value: not value)

    @field_validator("pinned_tags")
    @classmethod
    def unique_pinned_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("pinned context tags must be unique")
        return value

    @model_validator(mode="after")
    def image_history_uses_token_projection(self):
        if self.image_history_batches is not None and self.active_window_messages is not None:
            raise ValueError("image_history_batches requires token context projection")
        if self.append_state_history and self.active_window_messages is not None:
            raise ValueError("append_state_history requires token context projection")
        return self


def context_image_tool_spec() -> dict:
    """One local image registry reader; no paths or cross-task lookup."""
    return {"name": "read_context_image", "description": (
        "List saved images with action='list' to discover exact versions and their producer tools/arguments, "
        "including images from compacted history. Follow next_arguments for stable bounded pages; "
        "source_tool optionally filters by exact producer tool name. Reopen one image using its returned "
        "read arguments (view_id and sha256; action defaults to 'read'). Listing returns metadata only. "
        "Saved provenance does not assert current artifact validity or create new source evidence."),
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["read", "list"]},
            "view_id": {"type": "string", "minLength": 1},
            "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 16},
            "source_tool": {"type": "string", "minLength": 1, "maxLength": 128},
            "through_index": {"type": "integer", "minimum": 0},
            "before_index": {"type": "integer", "minimum": 0}},
            "additionalProperties": False}}


class StateEntry(ContractModel):
    """A machine-searchable current-state item, independent of summaries."""

    key: NonEmptyStr
    category: StateCategory
    value: JsonValue
    epistemic_status: EpistemicStatus
    source_refs: tuple[SourceRef, ...] = Field(min_length=1)
    revision: int = Field(default=1, ge=1)
    active: bool = True

    @model_validator(mode="after")
    def status_matches_category(self) -> "StateEntry":
        if self.category == "user_requirement" and self.epistemic_status != "user_stated":
            raise ValueError("user requirements must remain user_stated")
        if self.category == "unresolved" and self.epistemic_status != "unresolved":
            raise ValueError("unresolved entries must remain unresolved")
        return self


class HistoryRecord(ContractModel):
    history_id: NonEmptyStr
    message: dict[str, JsonValue]
    source: SourceRef
    message_blob: HashedBlobRef
    kind: HistoryKind = "message"
    tags: tuple[NonEmptyStr, ...] = ()
    image_keys: tuple[NonEmptyStr, ...] = ()
    summary_slot: NonEmptyStr | None = None
    summary_state_revisions: dict[str, int] = Field(default_factory=dict)
    content_sha256: NonEmptyStr

    @model_validator(mode="after")
    def summary_has_slot(self) -> "HistoryRecord":
        if self.kind == "summary" and self.summary_slot is None:
            raise ValueError("summary records need a summary_slot")
        if self.kind != "summary" and self.summary_slot is not None:
            raise ValueError("summary_slot is only valid for summary records")
        if self.kind != "summary" and self.summary_state_revisions:
            raise ValueError("summary state revisions are only valid for summary records")
        if any(revision < 1 for revision in self.summary_state_revisions.values()):
            raise ValueError("summary state revisions must be positive")
        return self


class ImageRecord(ContractModel):
    key: NonEmptyStr
    view_id: NonEmptyStr
    image: HashedBlobRef
    source: SourceRef
    detail: Literal["auto", "low", "high", "original"] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    tags: tuple[NonEmptyStr, ...] = ()
    registration_index: int = Field(ge=0)
    active: bool = True
    removal_event_id: NonEmptyStr | None = None


class SummaryStatement(ContractModel):
    """A summary assertion that must exactly reproduce an existing state item."""

    state_key: NonEmptyStr
    value: JsonValue
    epistemic_status: EpistemicStatus
    source_refs: tuple[SourceRef, ...] = Field(min_length=1)


class SummaryCandidate(ContractModel):
    covered_history_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    statements: tuple[SummaryStatement, ...] = Field(min_length=1)
    text: NonEmptyStr

    @field_validator("covered_history_ids")
    @classmethod
    def unique_history_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("summary history IDs must be unique")
        return value


class SummaryRequest(ContractModel):
    history: tuple[HistoryRecord, ...]
    current_state: tuple[StateEntry, ...]
    instruction: Literal[
        "Select current state statements without changing values or epistemic status. "
        "The returned text must be the canonical rendering of those statements."
    ] = (
        "Select current state statements without changing values or epistemic status. "
        "The returned text must be the canonical rendering of those statements."
    )


class StateChecklist(ContractModel):
    categories: dict[str, tuple[StateEntry, ...]]

    def entries(self, category: StateCategory) -> tuple[StateEntry, ...]:
        return self.categories[category]


class ContextProjection(ContractModel):
    messages: list[dict[str, JsonValue]]
    sources: list[SourceRef]
    checklist: StateChecklist
    decision_event_ids: tuple[NonEmptyStr, ...] = ()
    included_history_ids: tuple[NonEmptyStr, ...] = ()
    omitted_history_ids: tuple[NonEmptyStr, ...] = ()


SummaryCallback = Callable[
    [SummaryRequest], SummaryCandidate | Awaitable[SummaryCandidate]
]


@dataclass(frozen=True)
class _ProjectedRecord:
    record: HistoryRecord
    message: dict


class ContextManager:
    """Maintain lossless history and make an audited, bounded request view."""

    schema_version = "agent-runtime.context.v1"

    def __init__(
        self,
        store: EventStore,
        *,
        policy: ContextPolicy | None = None,
        summary_callback: SummaryCallback | None = None,
    ) -> None:
        self.store = store
        self.policy = policy or ContextPolicy()
        self.summary_callback = summary_callback
        self._history: list[HistoryRecord] = []
        self._state: dict[str, StateEntry] = {}
        self._images: dict[str, ImageRecord] = {}
        self._retrieval_pins: set[str] = set()
        self._archived_history_ids: set[str] = set()
        self._last_compaction: dict | None = None
        self._next_history = 0

    @property
    def history(self) -> tuple[HistoryRecord, ...]:
        return tuple(self._history)

    @property
    def state(self) -> tuple[StateEntry, ...]:
        return tuple(self._state[key] for key in sorted(self._state))

    @property
    def images(self) -> tuple[ImageRecord, ...]:
        return tuple(self._images[key] for key in sorted(self._images))

    def ingest(self, messages: list[dict], sources: list[SourceRef]) -> tuple[str, ...]:
        if len(messages) != len(sources):
            raise ValueError("every ingested message needs one source")
        return tuple(self.append(message, source) for message, source in zip(messages, sources))

    def append(
        self,
        message: dict,
        source: SourceRef,
        *,
        kind: HistoryKind = "message",
        tags: tuple[str, ...] = (),
        state_updates: tuple[StateEntry, ...] = (),
        summary_slot: str | None = None,
        summary_state_revisions: dict[str, int] | None = None,
        image_keys: tuple[str, ...] = (),
    ) -> str:
        if kind == "message" and message.get("role") == "tool":
            kind = "tool_result"
        resolved_image_keys = list(image_keys)
        for key in resolved_image_keys:
            if key not in self._images:
                raise ValueError(f"message references an unregistered image: {key}")
        registered_hashes = {self._images[key].image.sha256 for key in resolved_image_keys}
        for index, (digest, data, media_type, detail) in enumerate(_embedded_images(message)):
            # A tool may register its bytes before appending the presentation.
            # Fill missing transport detail without changing its provenance.
            if detail is not None:
                for key, image in self._images.items():
                    if image.image.sha256 == digest and image.detail is None:
                        self._images[key] = image.model_copy(update={"detail": detail})
            if digest in registered_hashes:
                continue
            existing = [
                image for image in self._images.values() if image.image.sha256 == digest
            ]
            if existing:
                for image in sorted(existing, key=lambda item: item.registration_index):
                    if image.key not in resolved_image_keys:
                        resolved_image_keys.append(image.key)
            else:
                ref = self.store.put_bytes(data, media_type)
                resolved_image_keys.append(
                    self.register_image(
                        f"{source.source_id}:image-{index}", ref, source=source, detail=detail
                    )
                )
            registered_hashes.add(digest)
        history_id = f"history-{self._next_history:06d}"
        self._next_history += 1
        copied = copy.deepcopy(message)
        record = HistoryRecord(
            history_id=history_id,
            message=copied,
            source=source,
            message_blob=self.store.put_json(copied),
            kind=kind,
            tags=tuple(tags),
            image_keys=tuple(resolved_image_keys),
            summary_slot=summary_slot,
            summary_state_revisions=dict(summary_state_revisions or {}),
            content_sha256=hashlib.sha256(json_bytes(copied)).hexdigest(),
        )
        self._history.append(record)
        for entry in state_updates:
            self.set_state(entry)
        return history_id

    def set_state(self, entry: StateEntry) -> None:
        previous = self._state.get(entry.key)
        if previous is None:
            if entry.revision != 1:
                raise ValueError("a new state entry starts at revision 1")
        elif entry == previous:
            return
        elif entry.revision != previous.revision + 1:
            raise ValueError("a changed state entry must increment revision by exactly one")
        if entry.category in _PROTECTED_CATEGORIES and all(
            self._is_summary_source(ref) for ref in entry.source_refs
        ):
            raise ValueError(
                "dimensions, object IDs, geometry and artifact versions cannot exist only in a summary"
            )
        self._state[entry.key] = entry

    def _is_summary_source(self, ref: SourceRef) -> bool:
        """Recognize summary provenance by ledger/blob identity, not a label."""

        if ref.source_kind == "generated" and ref.source_id.startswith("context-summary"):
            return True
        blob_sha = ref.blob.sha256 if isinstance(ref.blob, HashedBlobRef) else None
        for event in self.store.events:
            payload = event.payload
            if payload.event_type != "context" or payload.action != "compact":
                continue
            if ref.event_id == event.event_id:
                return True
            if blob_sha is not None and any(
                isinstance(candidate, HashedBlobRef) and candidate.sha256 == blob_sha
                for candidate in (payload.summary, payload.details)
            ):
                return True
            if ref.event_id is None or payload.details is None:
                continue
            try:
                details = json.loads(self.store.get_bytes(payload.details))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if (
                details.get("kind") == "validated_model_summary"
                and details.get("response_event_id") == ref.event_id
            ):
                return True
        return False

    def register_image(
        self,
        view_id: str,
        image: HashedBlobRef,
        *,
        source: SourceRef,
        tags: tuple[str, ...] = (),
        detail: Literal["auto", "low", "high", "original"] | None = None,
    ) -> str:
        # Verifies URI confinement, bytes and digest before accepting identity.
        self.store.get_bytes(image)
        key = self.image_key(view_id, image.sha256)
        candidate = ImageRecord(
            key=key, view_id=view_id, image=image, source=source, tags=tuple(tags), detail=detail,
            registration_index=len(self._images),
        )
        previous = self._images.get(key)
        if previous is not None:
            # The first registration is the provenance authority.  Replaying a
            # tool result or seeing the same bytes again is idempotent and must
            # not replace its source, tags, or registration order.
            if previous.detail is None and candidate.detail is not None:
                self._images[key] = previous.model_copy(update={"detail": candidate.detail})
            return key
        self._images[key] = candidate
        return key

    @staticmethod
    def image_key(view_id: str, sha256: str) -> str:
        if not view_id.strip() or len(sha256) != 64:
            raise ValueError("image identity needs a view_id and SHA-256")
        return f"{view_id}@sha256:{sha256}"

    def list_images(self, *, limit: int = 8, source_tool: str | None = None,
                    through_index: int | None = None, before_index: int | None = None) -> dict:
        """A bounded metadata view of the sole registry, without retrieval/presentation effects.

        The registration watermark freezes a page sequence while new images are
        appended. Each returned identity keeps its own original producer, even
        if another identity has identical image bytes. No validity is inferred.
        """
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError("limit must be an integer from 1 to 16")
        if source_tool is not None and (not isinstance(source_tool, str)
                or not source_tool.strip() or len(source_tool) > 128):
            raise ValueError("source_tool must be a nonempty exact tool name")
        for name, value in (("through_index", through_index), ("before_index", before_index)):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a nonnegative registration index")
        maximum = max((image.registration_index for image in self._images.values()), default=-1)
        if through_index is not None and through_index > maximum:
            raise ValueError("through_index exceeds the registered image watermark")
        if before_index is not None and (through_index is None or before_index > through_index):
            raise ValueError("before_index needs the through_index from the same page sequence")
        watermark = maximum if through_index is None else through_index
        events = {event.event_id: event for event in self.store.events}

        def producer(image):
            event = events.get(image.source.event_id)
            return event.payload if event is not None and event.payload.event_type == "tool_execution" else None

        eligible = sorted((image for image in self._images.values()
            if image.registration_index <= watermark and (source_tool is None or (
                (payload := producer(image)) is not None and payload.tool_name == source_tool))),
            key=lambda image: image.registration_index, reverse=True)
        remaining = [image for image in eligible
                     if before_index is None or image.registration_index < before_index]
        result = {"action": "list", "through_index": watermark if watermark >= 0 else None,
            "source_tool": source_tool, "total_matching": len(eligible), "images": [],
            "next_arguments": None,
            "note": "Metadata only; no image has been reopened or presented by this list. "
                    "Producer arguments describe the saved version, not current validity. "
                    "Arguments previews omit large values; registered_source identifies the full saved evidence."}
        for image in remaining[:limit]:
            payload = producer(image)
            row = {"registration_index": image.registration_index,
                "view_id": image.view_id, "sha256": image.image.sha256,
                "media_type": image.image.media_type,
                "registered_source": image.source.model_dump(mode="json"),
                "producer": None,
                "read": {"tool": "read_context_image", "arguments": {
                    "view_id": image.view_id, "sha256": image.image.sha256}}}
            if payload is not None:
                # Whole small values only: never turn a truncated string/box
                # into apparently exact provenance. Audit arguments stay intact.
                preview = {}
                for key, value in sorted(payload.full_arguments.items()):
                    if len(json_bytes({key: value})) <= 512 and len(json_bytes({**preview, key: value})) <= 1024:
                        preview[key] = copy.deepcopy(value)
                row["producer"] = {"event_id": image.source.event_id, "tool_name": payload.tool_name,
                    "call_id": payload.call_id, "arguments_preview": preview,
                    "arguments_complete": len(preview) == len(payload.full_arguments),
                    "omitted_argument_count": len(payload.full_arguments) - len(preview),
                    "arguments_sha256": hashlib.sha256(json_bytes(payload.full_arguments)).hexdigest()}
            # Keep the page itself bounded as well as the argument previews.
            # Exact identities are never shortened to manufacture a valid row.
            result["images"].append(row)
            if len(json_bytes(result)) > 22_000:
                result["images"].pop()
                if not result["images"]:
                    raise ValueError("saved image identity/provenance exceeds the directory page size bound")
                break
        if len(result["images"]) < len(remaining):
            result["next_arguments"] = {"action": "list", "limit": limit,
                "through_index": watermark, "before_index": result["images"][-1]["registration_index"],
                **({"source_tool": source_tool} if source_tool is not None else {})}
        return result

    def retrieve_image(self, view_id: str, sha256: str, *, append_to_history: bool = True) -> bytes:
        key = self.image_key(view_id, sha256)
        try:
            record = self._images[key]
        except KeyError as error:
            raise KeyError(f"unknown image identity: {key}") from error
        if record.view_id != view_id or record.image.sha256 != sha256:
            raise ValueError("image identity mismatch")
        data = self.store.get_bytes(record.image)
        if record.active and key in self._retrieval_pins:
            return data
        if not record.active:
            if record.removal_event_id is None:
                raise ValueError("removed image lacks its removal event")
            before = self._projection_snapshot()
            details = self.store.put_json(
                {"decision": "retrieve", "view_id": view_id, "sha256": sha256}
            )
            after = self.store.put_json(
                {"active_images": sorted([*self._active_image_keys(), key])}
            )
            event = self.store.append(
                self._context_payload(
                    action="retrieve_image",
                    reason="explicit exact image retrieval",
                    image=record.image,
                    removal_event_id=record.removal_event_id,
                    view_id=view_id,
                    before=before,
                    after=after,
                    details=details,
                ),
                source_refs=(record.source, self._blob_source("context-image-retrieve", details)),
            )
            self._images[key] = record.model_copy(
                update={"active": True, "removal_event_id": None}
            )
            if event.payload.image.sha256 != hashlib.sha256(data).hexdigest():
                raise ValueError("retrieved image bytes differ from the recorded image")
        # An explicit retrieval is a one-request exact pin.  It survives a
        # checkpoint and is consumed only after a valid projection is built.
        self._retrieval_pins.add(key)
        if self.policy.active_window_messages is None and append_to_history:
            # Explicit retrieval appends bytes at the end; it never reinserts
            # an old tool interaction ahead of the cached conversation prefix.
            self.append({"role": "user", "content": [
                {"type": "text", "text": f"Retrieved original image: {view_id}"},
                {"type": "image_url", "image_url": {"url":
                    f"data:{record.image.media_type};base64,{base64.b64encode(data).decode('ascii')}",
                    **({"detail": record.detail} if record.detail is not None else {})}},
            ]}, record.source, image_keys=(key,))
        return data

    def checklist(self) -> StateChecklist:
        active = [entry for entry in self.state if entry.active]
        return StateChecklist(
            categories={
                category: tuple(entry for entry in active if entry.category == category)
                for category in _CHECKLIST_CATEGORIES
            }
        )

    def full_history(self) -> tuple[list[dict], list[SourceRef]]:
        """Return exact original messages and their original event/source chain."""

        return (
            [copy.deepcopy(record.message) for record in self._history],
            [record.source for record in self._history],
        )

    restore_history = full_history

    def replay_events(self, events: list[EventEnvelope] | tuple[EventEnvelope, ...]) -> int:
        """Apply context events written after the loaded checkpoint.

        The runtime should first replay durable tool executions and its generic
        state importer, so every referenced image and current-state item exists.
        This method is idempotent for image decisions and skips summaries whose
        context event is already represented in history.
        """

        applied = 0
        represented_events = {
            record.source.event_id for record in self._history if record.source.event_id
        }
        for event in events:
            payload = event.payload
            if payload.event_type != "context":
                continue
            if payload.action in {"retain_image", "remove_image", "retrieve_image"}:
                if not isinstance(payload.image, HashedBlobRef) or payload.view_id is None:
                    raise ValueError("replayed image decision needs hashed image and view_id")
                key = self.image_key(payload.view_id, payload.image.sha256)
                record = self._images.get(key)
                if record is None:
                    raise ValueError(
                        "replay tool results/register_image before context image decisions"
                    )
                if payload.action == "remove_image":
                    self._images[key] = record.model_copy(
                        update={"active": False, "removal_event_id": event.event_id}
                    )
                elif payload.action == "retain_image":
                    self._images[key] = record.model_copy(
                        update={"active": True, "removal_event_id": None}
                    )
                else:
                    if record.removal_event_id not in {None, payload.removal_event_id}:
                        raise ValueError("replayed retrieval does not match current removal")
                    self.store.get_bytes(record.image)
                    self._images[key] = record.model_copy(
                        update={"active": True, "removal_event_id": None}
                    )
                if payload.details is not None:
                    replay_details = json.loads(self.store.get_bytes(payload.details))
                    if replay_details.get("decision") == "retrieve":
                        self._retrieval_pins.add(key)
                applied += 1
                continue
            if payload.action != "compact" or payload.details is None:
                continue
            details = json.loads(self.store.get_bytes(payload.details))
            if details.get("kind") == "deterministic_compaction":
                identities = [row["history_id"] for row in details["records"]]
                message = json.loads(self.store.get_bytes(payload.after))
                source = SourceRef(source_id=f"context-compaction-{event.event_id}",
                    source_kind="generated", locator=payload.summary.uri,
                    blob=payload.summary, event_id=event.event_id)
                self._last_compaction = {"history_ids": identities, "message": message,
                    "source": source.model_dump(mode="json"), "event_id": event.event_id}
                if self.policy.active_window_messages is None:
                    self._archived_history_ids.update(identities)
                applied += 1
                continue
            if details.get("kind") != "validated_model_summary" or event.event_id in represented_events:
                continue
            candidate = SummaryCandidate.model_validate_json(
                json.dumps(details["candidate"])
            )
            if not isinstance(payload.summary, HashedBlobRef):
                raise ValueError("replayed model summary needs a hashed attachment")
            revisions: dict[str, int] = {}
            for statement in candidate.statements:
                current = self._state.get(statement.state_key)
                exact = current is not None and (
                    current.active
                    and current.value == statement.value
                    and current.epistemic_status == statement.epistemic_status
                    and current.source_refs == statement.source_refs
                )
                revisions[statement.state_key] = (
                    current.revision if exact else (current.revision + 1 if current else 1)
                )
            source = SourceRef(
                source_id=f"context-summary-{event.event_id}",
                source_kind="generated",
                locator=payload.summary.uri,
                blob=payload.summary,
                event_id=event.event_id,
            )
            self.append(
                {"role": "system", "content": candidate.text},
                source,
                kind="summary",
                summary_slot="model-summary",
                summary_state_revisions=revisions,
            )
            represented_events.add(event.event_id)
            applied += 1
        return applied

    def build_summary_request(self, history_ids: tuple[str, ...]) -> SummaryRequest:
        selected = self._records(history_ids)
        return SummaryRequest(history=selected, current_state=tuple(
            entry for entry in self.state if entry.active
        ))

    @staticmethod
    def render_summary(statements: tuple[SummaryStatement, ...]) -> str:
        values = [statement.model_dump(mode="json") for statement in statements]
        return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def validate_summary(self, candidate: SummaryCandidate) -> SummaryCandidate:
        self._records(candidate.covered_history_ids)
        seen: set[str] = set()
        for statement in candidate.statements:
            if statement.state_key in seen:
                raise ValueError("summary cannot repeat a state key")
            seen.add(statement.state_key)
            current = self._state.get(statement.state_key)
            if current is None or not current.active:
                raise ValueError("summary statement does not reference active current state")
            if (
                statement.value != current.value
                or statement.epistemic_status != current.epistemic_status
                or statement.source_refs != current.source_refs
            ):
                raise ValueError(
                    "summary cannot change a value, its epistemic status, or its evidence"
                )
        if candidate.text != self.render_summary(candidate.statements):
            raise ValueError("summary text must be the canonical rendering of validated state")
        return candidate

    async def summarize(self, history_ids: tuple[str, ...]) -> SummaryCandidate:
        if self.summary_callback is None:
            raise ValueError("no audited summary callback was configured")
        result = self.summary_callback(self.build_summary_request(history_ids))
        candidate = await result if inspect.isawaitable(result) else result
        if not isinstance(candidate, SummaryCandidate):
            candidate = SummaryCandidate.model_validate(candidate)
        return self.validate_summary(candidate)

    def compact_with_summary(
        self,
        candidate: SummaryCandidate,
        *,
        summary_slot: str = "model-summary",
        source: SourceRef | None = None,
    ) -> str:
        candidate = self.validate_summary(candidate)
        records = self._records(candidate.covered_history_ids)
        event_ids = self._event_ids(records)
        if not event_ids:
            raise ValueError("context compaction needs prior recorded event references")
        summary_ref = self.store.put_json(candidate.model_dump(mode="json"))
        details = self.store.put_json(
            {"kind": "validated_model_summary",
             "candidate": candidate.model_dump(mode="json"),
             "response_event_id": source.event_id if source else None}
        )
        before = self._projection_snapshot()
        event = self.store.append(
            self._context_payload(
                action="compact",
                reason="validated model summary requested by the runtime",
                summary=summary_ref,
                replaced_event_ids=event_ids,
                before=before,
                after=self.store.put_json({"summary": summary_ref.model_dump(mode="json")}),
                details=details,
            ),
            source_refs=tuple([
                *(r.source for r in records),
                *((source,) if source else ()),
                self._blob_source("context-summary-details", details),
            ]),
        )
        source = SourceRef(
            source_id=f"context-summary-{event.event_id}",
            source_kind="generated",
            locator=summary_ref.uri,
            blob=summary_ref,
            event_id=event.event_id,
        )
        return self.append(
            {"role": "system", "content": candidate.text},
            source,
            kind="summary",
            summary_slot=summary_slot,
            summary_state_revisions={
                statement.state_key: self._state[statement.state_key].revision
                for statement in candidate.statements
            },
        )

    def project(
        self,
        *,
        required_tags: tuple[str, ...] = (),
        required_view_ids: tuple[str, ...] = (),
        consume_retrievals: bool = True,
        token_estimator: Callable[[list[dict]], int] | None = None,
        request_token_estimator: Callable[[list[dict], list[SourceRef]], int] | None = None,
        pressure_tokens: int | None = None,
        target_tokens: int | None = None,
        force_compact: bool = False,
        undelivered_image_event_ids: tuple[str, ...] = (),
    ) -> ContextProjection:
        if self.policy.active_window_messages is None:
            return self._project_by_tokens(required_tags=required_tags,
                required_view_ids=required_view_ids, consume_retrievals=consume_retrievals,
                token_estimator=token_estimator, request_token_estimator=request_token_estimator,
                pressure_tokens=pressure_tokens, target_tokens=target_tokens, force_compact=force_compact,
                undelivered_image_event_ids=undelivered_image_event_ids)
        required = set(required_tags) | set(self.policy.pinned_tags)
        exact_retrievals = set(self._retrieval_pins)
        selected, omitted = self._select_history(
            required, set(required_view_ids), exact_retrievals
        )
        image_decisions = self._decide_images(
            selected, required, set(required_view_ids), exact_retrievals
        )
        decision_event_ids = list(self._record_image_decisions(image_decisions))

        newest_large_result: dict[str, str] = {}
        for record in selected:
            if record.kind == "tool_result" and _message_content_size(record.message) >= self.policy.large_result_bytes:
                newest_large_result[_message_content_sha(record.message)] = record.history_id
        referenced_results = [
            record
            for record in selected
            if record.kind == "tool_result"
            and _message_content_size(record.message) >= self.policy.large_result_bytes
            and newest_large_result[_message_content_sha(record.message)] != record.history_id
        ]
        if referenced_results:
            event = self._record_result_reference_compaction(
                referenced_results, newest_large_result
            )
            decision_event_ids.append(event.event_id)

        projected: list[_ProjectedRecord] = []
        last_image_occurrence: dict[str, str] = {}
        for record in selected:
            for key in record.image_keys:
                image = self._images[key]
                if image.active:
                    last_image_occurrence[image.image.sha256] = record.history_id
        sent_image_hashes: set[str] = set()
        for record in selected:
            projected.append(
                _ProjectedRecord(
                    record,
                    self._project_message(
                        record,
                        newest_large_result,
                        last_image_occurrence,
                        sent_image_hashes,
                    ),
                )
            )

        compacted = [r for r in omitted if r.source.event_id is not None]
        if compacted:
            event, compact_message, compact_source = self._record_deterministic_compaction(compacted)
            if compact_message is not None:
                if event is not None:
                    decision_event_ids.append(event.event_id)
                synthetic = HistoryRecord(
                    history_id=f"projection-{compact_source.event_id}",
                    message=compact_message,
                    source=compact_source,
                    message_blob=self.store.put_json(compact_message),
                    kind="summary",
                    summary_slot="deterministic-compaction",
                    content_sha256=hashlib.sha256(json_bytes(compact_message)).hexdigest(),
                )
                # Appending at a group boundary cannot split assistant tool_calls
                # from any of their tool replies.
                projected.append(_ProjectedRecord(synthetic, compact_message))

        checklist = self.checklist()
        model_state = self.model_state()
        if model_state:
            state_message = {
                "role": "user",
                "content": "Current runtime state (machine generated; epistemic status is authoritative): "
                + json.dumps(
                    model_state,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
            state_ref = self.store.put_json(state_message)
            state_source = SourceRef(
                source_id="context-current-state",
                source_kind="generated",
                locator=state_ref.uri,
                blob=state_ref,
            )
            synthetic = HistoryRecord(
                history_id="projection-current-state",
                message=state_message,
                source=state_source,
                message_blob=state_ref,
                content_sha256=hashlib.sha256(json_bytes(state_message)).hexdigest(),
            )
            projected.append(_ProjectedRecord(synthetic, state_message))

        messages = [item.message for item in projected]
        _validate_projected_tool_protocol(tuple(messages))
        if consume_retrievals:
            self._retrieval_pins.difference_update(exact_retrievals)
        return ContextProjection(
            messages=messages,
            sources=[item.record.source for item in projected],
            checklist=checklist,
            decision_event_ids=tuple(decision_event_ids),
            included_history_ids=tuple(r.history_id for r in selected),
            omitted_history_ids=tuple(r.history_id for r in omitted),
        )

    def model_state(self) -> list[dict]:
        """Small mutable tail; the full source-bearing state stays in checkpoints.

        Computed child allowances are retained as operational constraints. The
        original user task is already in the immutable initial messages.
        """
        result, seen = [], set()
        for entry in sorted(self.state, key=lambda s: (s.category == "todo", s.key)):
            if not entry.active:
                continue
            value = entry.value
            fields = self.policy.state_item_fields.get(entry.key)
            if fields and isinstance(value, list) and all(isinstance(item, dict) for item in value):
                value = [{key: item[key] for key in fields if key in item} or item for item in value]
            if entry.category == "artifact_version":
                if isinstance(value, dict):
                    value = {k: value[k] for k in ("candidate", "version") if k in value}
            elif entry.category in {"unresolved", "todo"}:
                if entry.key == "current-work-review" and isinstance(value, dict):
                    value = value.get("record", {}).get("next_action")
                value = _display_issues(value, seen)
            elif entry.category == "constraint" and entry.epistemic_status == "computed":
                value = _without_audit_metadata(value)
            elif entry.key == "context-retrieval":
                pass
            else:
                continue
            if value:
                result.append({"key": entry.key, "value": value,
                               "epistemic_status": entry.epistemic_status,
                               **({"revision": entry.revision} if entry.category == "constraint" else {})})
        return result

    def _state_message(self, records):
        state = self.model_state()
        prefix = "Current runtime state (machine generated; epistemic status is authoritative): "
        if not self.policy.append_state_history:
            return {"role": "user", "content": prefix + json.dumps(
                state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))} if state else None
        latest = next((r for r in reversed(records) if "runtime-state-snapshot" in r.tags), None)
        if not state and latest is None:
            return None
        message = {"role": "user", "content": prefix
            + "Only the latest runtime state snapshot is current; older snapshots describe earlier steps. "
            + json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))}
        return None if latest is not None and latest.message == message else message

    def commit_request_state(self, request_event):
        """Persist the exact state actually sent, once, before its assistant reply.

        The request journal is the authority, including when a durable response
        is replayed after a crash. Preview/estimation and rejected/truncated
        requests cannot create phantom history snapshots.
        """
        if not self.policy.append_state_history:
            return
        request_tag = "runtime-state-request:" + request_event.event_id
        if any(request_tag in r.tags for r in self._history):
            return
        origins = {item.source.blob.sha256: item.source
            for item in request_event.payload.injected_content
            if item.source.source_id == "context-current-state" and item.source.blob is not None}
        if len(origins) > 1:
            raise ValueError("one request cannot contain multiple current runtime states")
        for source in origins.values():
            message = json.loads(self.store.get_bytes(source.blob))
            if (source.source_kind != "generated" or message.get("role") != "user"
                    or not isinstance(message.get("content"), str)):
                raise ValueError("current runtime state has invalid recorded provenance")
            recorded = source.model_copy(update={
                "source_id": "context-state-snapshot-" + request_event.event_id,
                "event_id": request_event.event_id})
            self.append(message, recorded, tags=("runtime-state-snapshot", request_tag))

    def _compaction_notice(self, identities: set[str]) -> str:
        groups = [i + 1 for i, group in enumerate(_interaction_groups(self._history))
                  if any(r.history_id in identities for r in group)]
        ranges = []
        for number in groups:
            if ranges and ranges[-1][-1] + 1 == number:
                ranges[-1].append(number)
            else:
                ranges.append([number])
        turns = ", ".join(str(r[0]) if len(r) == 1 else f"{r[0]}–{r[-1]}" for r in ranges)
        image_directory = (" Discover saved originals and diagnostics, including removed interactions, with "
            "read_context_image(action='list'); follow next_arguments, then a row's exact read arguments. "
            "The directory identifies saved versions and does not assert current validity."
            if self.policy.image_history_batches is not None else "")
        return (f"Deterministic context compaction: earlier interaction groups {turns} "
                f"({len(identities)} messages) moved out of this request; original images can be "
                "viewed again using the input names and image-viewing tools, and saved work/evidence "
                "can be inspected through the retrieval entries in the current runtime state." + image_directory)

    def _project_by_tokens(self, *, required_tags, required_view_ids, consume_retrievals, token_estimator,
                           request_token_estimator=None, pressure_tokens=None, target_tokens=None,
                           force_compact=False, undelivered_image_event_ids=()):
        from .estimation import approximate_text_tokens, _text_payload

        def estimate(messages, sources):
            if request_token_estimator is not None:
                return request_token_estimator(messages, sources)
            if token_estimator is not None:
                return token_estimator(messages)
            # Standalone/offline callers have no provider profile. Count text
            # separately from image bytes; runtime supplies its image estimator.
            text, images = _text_payload({"messages": messages})
            return approximate_text_tokens(text) + sum(
                (len(url.split(",", 1)[1]) * 3 // 4 + 3) // 4 for _, url in images)

        required = set(required_tags) | set(self.policy.pinned_tags)
        views = set(required_view_ids)
        selected = [r for r in self._history if r.history_id not in self._archived_history_ids]
        image_history_ids = (self._retained_image_history(selected, required, views,
            set(undelivered_image_event_ids)) if self.policy.image_history_batches is not None else None)
        def assemble(records, notice):
            messages = [(self._project_image_history_message(r, r.history_id in image_history_ids)
                         if image_history_ids is not None else copy.deepcopy(r.message)) for r in records]
            sources = [r.source for r in records]
            if notice:
                # Keep initial guidance/user input, then a stable compacted
                # prefix, then the surviving chronological interactions.
                initial_ids = {r.history_id for r in self._history[:self.policy.preserve_initial_messages]}
                index = sum(r.history_id in initial_ids for r in records)
                messages.insert(index, notice["message"])
                sources.insert(index, SourceRef.model_validate_json(json.dumps(notice["source"])))
            state_message = self._state_message(records)
            if state_message:
                messages.append(state_message)
                sources.append(self.store.source("context-current-state", state_message, kind="generated"))
            return messages, sources

        messages, sources = assemble(selected, self._last_compaction)
        events = []
        threshold = pressure_tokens if pressure_tokens is not None else self.policy.compact_at_tokens
        if force_compact or (threshold is not None and estimate(messages, sources) >= threshold):
            protected = {r.history_id for r in self._history[:self.policy.preserve_initial_messages]}
            protected.update(r.history_id for r in selected if required & set(r.tags) or any(
                key in self._retrieval_pins or self._images[key].view_id in views
                or required & set(self._images[key].tags) for key in r.image_keys))
            if image_history_ids is not None:
                protected.update(image_history_ids)
            groups = _interaction_groups(selected)
            if groups:
                protected.update(r.history_id for r in groups[-1])
            target = (target_tokens if target_tokens is not None else
                      int((threshold or estimate(messages, sources)) * self.policy.compact_to_ratio))
            image_bytes = {r.history_id: (sum(len(self.store.get_bytes(self._images[k].image))
                for k in r.image_keys) if image_history_ids is None or r.history_id in image_history_ids else 0)
                for r in selected}

            def fits(records):
                projected, projected_sources = assemble(records, self._last_compaction)
                image_keys = {key for r in records for key in r.image_keys
                    if image_history_ids is None or r.history_id in image_history_ids}
                return (estimate(projected, projected_sources) <= target
                    and (self.policy.max_image_bytes is None
                         or sum(image_bytes[r.history_id] for r in records) <= self.policy.max_image_bytes)
                    and (self.policy.max_images is None or len(image_keys) <= self.policy.max_images))

            removed = set()
            for group in groups:
                if fits(selected):
                    break
                if any(r.history_id in protected for r in group):
                    continue
                removed.update(r.history_id for r in group)
                selected = [r for r in selected if r.history_id not in removed]
            if removed:
                self._archived_history_ids.update(removed)
                omitted = [r for r in self._history if r.history_id in self._archived_history_ids]
                event, _, _ = self._record_deterministic_compaction(omitted)
                if event:
                    events.append(event.event_id)
                messages, sources = assemble(selected, self._last_compaction)
            # An indivisible/pinned interaction may exceed the retention target.
            # Preserve it; the actual request's model/context budget is enforced
            # by Runtime before send, never silently omit required input.

        if image_history_ids is not None:
            omitted_images = [r for r in selected if r.image_keys and r.history_id not in image_history_ids]
            if omitted_images:
                events.append(self._record_image_history_projection(omitted_images).event_id)
        selected_keys = {k for r in selected for k in r.image_keys
            if image_history_ids is None or r.history_id in image_history_ids}
        events.extend(self._record_image_decisions({key: (key in selected_keys,
            ("retained image history" if key in selected_keys else "image history projection")
            if image_history_ids is not None else
            ("retained history" if key in selected_keys else "token-threshold compaction"))
            for key in self._images}))
        _validate_projected_tool_protocol(tuple(messages))
        if consume_retrievals:
            self.acknowledge_projection()
        return ContextProjection(messages=messages, sources=sources, checklist=self.checklist(),
            decision_event_ids=tuple(events), included_history_ids=tuple(r.history_id for r in selected),
            omitted_history_ids=tuple(r.history_id for r in self._history
                                      if r.history_id in self._archived_history_ids))

    def _retained_image_history(self, selected, required_tags, required_views, undelivered_events):
        keep = {r.history_id for r in self._history[:self.policy.preserve_initial_messages]}
        # Runtime, not model text or tool metadata, sets this history tag at
        # initial ingestion. It covers initial attachments after the first N.
        keep.update(r.history_id for r in selected if "runtime-initial-input" in r.tags)
        groups = _interaction_groups(selected)
        image_batches = [group for group in groups if any(r.image_keys for r in group)
                         and any(r.message.get("role") == "tool" for r in group)]
        keep.update(r.history_id for group in image_batches[-self.policy.image_history_batches:]
                    for r in group if r.image_keys)
        # Preserve exact occurrence identities, not every historical copy of
        # matching bytes. A repeated original in an old diagnostic is archived.
        for record in selected:
            if (required_tags & set(record.tags) or any(
                    self._images[key].view_id in required_views
                    or required_tags & set(self._images[key].tags) for key in record.image_keys)):
                keep.add(record.history_id)
            if record.source.source_id == "tool-images":
                origins = json.loads(self.store.get_bytes(record.source.blob))["origin_events"]
                if undelivered_events & set(origins):
                    keep.add(record.history_id)
        # A retrieval keeps its newest occurrence only; it must not rehydrate
        # every earlier occurrence of the same image throughout the transcript.
        for key in self._retrieval_pins:
            latest = next((r for r in reversed(selected) if key in r.image_keys), None)
            if latest is not None:
                keep.add(latest.history_id)
        return keep

    def _project_image_history_message(self, record, retain):
        result = copy.deepcopy(record.message)
        if retain or not record.image_keys or not isinstance(result.get("content"), list):
            return result
        blocks = []
        for block in result["content"]:
            sha = _image_block_hash(block)
            if sha is None:
                blocks.append(block)
                continue
            images = [self._images[key] for key in record.image_keys if self._images[key].image.sha256 == sha]
            if not images:
                raise ValueError("history image has no registered exact identity")
            chosen = images[0]
            blocks.append({"type": "text", "text": json.dumps({"archived_image": {
                "view_id": chosen.view_id, "sha256": sha,
                "history_id": record.history_id, "history_source_event_id": record.source.event_id,
                "registered_source_event_id": chosen.source.event_id,
                "read": {"tool": "read_context_image", "arguments": {
                    "view_id": chosen.view_id, "sha256": sha}},
                "note": "Exact saved image bytes are omitted here; source and diagnostic status are unchanged."}},
                ensure_ascii=False, separators=(",", ":"))})
        result["content"] = blocks
        return result

    def _record_image_history_projection(self, records):
        details = self.store.put_json({"kind": "image_history_projection",
            "records": [{"history_id": r.history_id, "message_blob": r.message_blob.model_dump(mode="json"),
                         "image_keys": r.image_keys} for r in records],
            "strategy": "preserve text and current image occurrences; exact older images remain retrievable"})
        existing = next((event for event in reversed(self.store.events)
            if event.payload.event_type == "context" and event.payload.action == "compact"
            and event.payload.details == details), None)
        if existing is not None:
            return existing
        return self.store.append(self._context_payload(action="compact", summary=details,
            replaced_event_ids=self._event_ids(records), before=self._projection_snapshot(), after=details,
            details=details, reason="older image blocks replaced by exact retrievable references"),
            source_refs=tuple(r.source for r in records))

    def acknowledge_projection(self) -> tuple[str, ...]:
        """Consume exact-retrieval pins after the primary response is accepted."""

        consumed = tuple(sorted(self._retrieval_pins))
        self._retrieval_pins.clear()
        return consumed

    def dump(self) -> dict:
        history = []
        for record in self._history:
            item = record.model_dump(mode="json")
            # Checkpoints retain only a content-addressed pointer; this avoids
            # rewriting the full conversation and base64 images each turn.
            item.pop("message")
            history.append(item)
        return {
            "schema_version": self.schema_version,
            "policy": self.policy.model_dump(mode="json"),
            "history": history,
            "state": [entry.model_dump(mode="json") for entry in self.state],
            "images": [record.model_dump(mode="json") for record in self.images],
            "retrieval_pins": sorted(self._retrieval_pins),
            "archived_history_ids": sorted(self._archived_history_ids),
            "last_compaction": self._last_compaction,
            "next_history": self._next_history,
        }

    @classmethod
    def load(
        cls,
        store: EventStore,
        payload: dict,
        *,
        policy: ContextPolicy | None = None,
        summary_callback: SummaryCallback | None = None,
    ) -> "ContextManager":
        if payload.get("schema_version") != cls.schema_version:
            raise ValueError("unsupported context checkpoint schema")
        saved_policy = ContextPolicy.model_validate_json(json.dumps(payload["policy"]))
        if policy is not None and policy != saved_policy:
            raise ValueError("context policy cannot change while restoring a checkpoint")
        manager = cls(store, policy=saved_policy, summary_callback=summary_callback)
        history = []
        for item in payload["history"]:
            ref = HashedBlobRef.model_validate_json(json.dumps(item["message_blob"]))
            message = json.loads(store.get_bytes(ref))
            history.append(HistoryRecord.model_validate_json(
                json.dumps({**item, "message": message})
            ))
        manager._history = history
        manager._state = {
            entry.key: entry for entry in (
                StateEntry.model_validate_json(json.dumps(item)) for item in payload["state"]
            )
        }
        manager._images = {
            image.key: image for image in (
                ImageRecord.model_validate_json(json.dumps(item)) for item in payload["images"]
            )
        }
        manager._archived_history_ids = set(payload.get("archived_history_ids", ()))
        manager._last_compaction = payload.get("last_compaction")
        manager._retrieval_pins = set(payload.get("retrieval_pins", ()))
        manager._next_history = payload["next_history"]
        if manager._next_history < len(manager._history):
            raise ValueError("context checkpoint history counter moved backwards")
        if len(manager._state) != len(payload["state"]) or len(manager._images) != len(payload["images"]):
            raise ValueError("context checkpoint contains duplicate identities")
        history_ids = [record.history_id for record in manager._history]
        if len(history_ids) != len(set(history_ids)):
            raise ValueError("context checkpoint contains duplicate history IDs")
        if not manager._archived_history_ids <= set(history_ids):
            raise ValueError("context checkpoint archives unknown history")
        if manager.policy.active_window_messages is None and manager._archived_history_ids != set(
                (manager._last_compaction or {}).get("history_ids", ())):
            raise ValueError("context checkpoint archived history differs from its recorded compaction")
        if manager._last_compaction:
            saved = manager._last_compaction
            matching = next((e for e in store.events if e.event_id == saved["event_id"]), None)
            if (matching is None or matching.payload.event_type != "context"
                    or matching.payload.action != "compact"
                    or matching.payload.after is None
                    or json.loads(store.get_bytes(matching.payload.after)) != saved["message"]
                    or saved["source"]["blob"] != matching.payload.summary.model_dump(mode="json")
                    or saved["source"]["event_id"] != matching.event_id):
                raise ValueError("context checkpoint compaction differs from its recorded source")
            details = json.loads(store.get_bytes(matching.payload.details))
            if saved["history_ids"] != [row["history_id"] for row in details["records"]]:
                raise ValueError("context checkpoint compaction history differs from its recorded source")
        for record in manager._history:
            if (
                hashlib.sha256(json_bytes(record.message)).hexdigest() != record.content_sha256
                or record.message_blob.sha256 != record.content_sha256
            ):
                raise ValueError("context history message hash mismatch")
            if any(key not in manager._images for key in record.image_keys):
                raise ValueError("context history references a missing image")
        for image in manager._images.values():
            store.get_bytes(image.image)
            if image.active and image.removal_event_id is not None:
                raise ValueError("active checkpoint image cannot retain a removal event")
            if not image.active:
                if image.removal_event_id is None:
                    raise ValueError("removed checkpoint image lacks a removal event")
                matching = [event for event in store.events if event.event_id == image.removal_event_id]
                if (
                    len(matching) != 1
                    or matching[0].payload.event_type != "context"
                    or matching[0].payload.action != "remove_image"
                    or matching[0].payload.image != image.image
                ):
                    raise ValueError("checkpoint image removal reference is invalid")
        if not manager._retrieval_pins <= set(manager._images):
            raise ValueError("context checkpoint has retrieval pins for unknown images")
        return manager

    def _select_history(
        self,
        required_tags: set[str],
        required_view_ids: set[str],
        required_image_keys: set[str],
    ) -> tuple[list[HistoryRecord], list[HistoryRecord]]:
        keep: set[str] = {r.history_id for r in self._history[: self.policy.preserve_initial_messages]}
        keep.update(r.history_id for r in self._history[-self.policy.active_window_messages :])
        keep.update(r.history_id for r in self._history if required_tags & set(r.tags))
        keep.update(
            r.history_id
            for r in self._history
            if any(
                required_tags & set(self._images[key].tags)
                or self._images[key].view_id in required_view_ids
                or key in required_image_keys
                for key in r.image_keys
            )
        )

        # If any member is selected, expand to the whole assistant/tool/image
        # interaction group before removing obsolete standalone summaries.
        for group in _interaction_groups(self._history):
            if keep & {record.history_id for record in group}:
                keep.update(record.history_id for record in group)

        # Only the newest summary in a slot is useful in the request projection.
        newest_summary: dict[str, str] = {}
        for record in self._history:
            if record.summary_slot:
                newest_summary[record.summary_slot] = record.history_id
        for record in self._history:
            if record.summary_slot and newest_summary[record.summary_slot] != record.history_id:
                keep.discard(record.history_id)
            elif record.summary_state_revisions and any(
                key not in self._state
                or not self._state[key].active
                or self._state[key].revision != revision
                for key, revision in record.summary_state_revisions.items()
            ):
                keep.discard(record.history_id)

        selected = [r for r in self._history if r.history_id in keep]
        omitted = [r for r in self._history if r.history_id not in keep]
        return selected, omitted

    def _decide_images(
        self,
        selected: list[HistoryRecord],
        required_tags: set[str],
        required_view_ids: set[str],
        required_image_keys: set[str],
    ) -> dict[str, tuple[bool, str]]:
        selected_keys = {key for record in selected for key in record.image_keys}
        provisional: dict[str, tuple[bool, str]] = {}
        for key, image in self._images.items():
            keep = key in selected_keys and (
                key in required_image_keys
                or
                image.view_id in required_view_ids
                or bool(required_tags & set(image.tags))
                or key in {k for record in selected[-self.policy.active_window_messages :] for k in record.image_keys}
            )
            reason = (
                "explicit exact retrieval"
                if key in required_image_keys
                else "required view_id"
                if image.view_id in required_view_ids
                else "configured pinned tag"
                if required_tags & set(image.tags)
                else "active window"
                if keep
                else "outside the active request policy"
            )
            provisional[key] = (keep, reason)

        # One view_id may have multiple byte versions.  A request that names
        # only the view gets its newest registered version; exact recovery uses
        # retrieve_image(view_id, sha256).
        by_view: dict[str, list[str]] = {}
        for key, (keep, _) in provisional.items():
            if keep:
                by_view.setdefault(self._images[key].view_id, []).append(key)
        for keys in by_view.values():
            if len(keys) <= 1:
                continue
            winner = max(keys, key=lambda key: self._images[key].registration_index)
            pinned = [key for key in keys if key in required_image_keys]
            if pinned:
                winner = max(pinned, key=lambda key: self._images[key].registration_index)
            for key in keys:
                if key != winner:
                    provisional[key] = (False, f"older view version replaced by {winner}")

        # Duplicate bytes are sent once. Required view IDs win, then lexical key.
        by_hash: dict[str, list[str]] = {}
        for key, (keep, _) in provisional.items():
            if keep:
                by_hash.setdefault(self._images[key].image.sha256, []).append(key)
        for keys in by_hash.values():
            if len(keys) <= 1:
                continue
            winner = max(
                keys,
                key=lambda key: (
                    key in required_image_keys,
                    self._images[key].view_id in required_view_ids,
                    self._images[key].registration_index,
                ),
            )
            for key in keys:
                if key != winner:
                    provisional[key] = (False, f"duplicate image bytes retained as {winner}")
        kept = [key for key, (keep, _) in provisional.items() if keep]
        kept.sort(
            key=lambda key: (
                key in required_image_keys,
                self._images[key].view_id in required_view_ids,
                bool(required_tags & set(self._images[key].tags)),
                self._images[key].registration_index,
            ),
            reverse=True,
        )
        used_bytes = 0
        for index, key in enumerate(kept):
            size = len(self.store.get_bytes(self._images[key].image))
            exceeds_count = self.policy.max_images is not None and index >= self.policy.max_images
            exceeds_bytes = (
                self.policy.max_image_bytes is not None
                and used_bytes + size > self.policy.max_image_bytes
            )
            if exceeds_count or exceeds_bytes:
                if (
                    key in required_image_keys
                    or self._images[key].view_id in required_view_ids
                    or bool(required_tags & set(self._images[key].tags))
                ):
                    raise ValueError(
                        "required image exceeds the configured request image limits"
                    )
                provisional[key] = (
                    False,
                    "request image count limit" if exceeds_count else "request image byte limit",
                )
            else:
                used_bytes += size
        return provisional

    def _record_image_decisions(self, decisions: dict[str, tuple[bool, str]]) -> tuple[str, ...]:
        event_ids: list[str] = []
        for key in sorted(decisions):
            keep, reason = decisions[key]
            record = self._images[key]
            if record.active == keep:
                continue
            before = self._projection_snapshot()
            details = self.store.put_json(
                {"decision": "retain" if keep else "remove", "key": key,
                 "view_id": record.view_id, "sha256": record.image.sha256, "reason": reason}
            )
            after_active = self._active_image_keys()
            if keep:
                after_active.add(key)
                if record.active:
                    action = "retain_image"
                    fields = {}
                else:
                    if record.removal_event_id is None:
                        raise ValueError("removed image lacks its removal event")
                    self.store.get_bytes(record.image)
                    action = "retrieve_image"
                    fields = {"removal_event_id": record.removal_event_id}
            else:
                after_active.discard(key)
                action = "remove_image"
                fields = {}
            after = self.store.put_json({"active_images": sorted(after_active)})
            event = self.store.append(
                self._context_payload(
                    action=action,
                    reason=reason,
                    image=record.image,
                    view_id=record.view_id,
                    before=before,
                    after=after,
                    details=details,
                    **fields,
                ),
                source_refs=(record.source, self._blob_source("context-image-decision", details)),
            )
            event_ids.append(event.event_id)
            self._images[key] = record.model_copy(
                update={
                    "active": keep,
                    "removal_event_id": None if keep else event.event_id,
                }
            )
        return tuple(event_ids)

    def _record_deterministic_compaction(self, records: list[HistoryRecord]):
        identities = [r.history_id for r in records]
        if self._last_compaction and self._last_compaction["history_ids"] == identities:
            saved = self._last_compaction
            return None, saved["message"], SourceRef.model_validate_json(json.dumps(saved["source"]))
        event_ids = self._event_ids(tuple(records))
        if not event_ids:
            return None, None, None
        description = {
            "kind": "deterministic_compaction",
            "records": [
                {"history_id": r.history_id, "sha256": r.content_sha256, "kind": r.kind}
                for r in records
            ],
            "current_state_keys": [entry.key for entry in self.state if entry.active],
            "note": "Full messages remain in the context checkpoint and event attachments.",
        }
        summary = self.store.put_json(description)
        details = self.store.put_json(
            {"strategy": "exact duplicate/old summary/activity window", **description}
        )
        before = self._projection_snapshot()
        compact_message = {
            "role": "system",
            "content": self._compaction_notice(set(identities)),
        }
        after = self.store.put_json(compact_message)
        event = self.store.append(
            self._context_payload(
                action="compact",
                reason="deterministic projection removed repeated or inactive history",
                summary=summary,
                replaced_event_ids=event_ids,
                before=before,
                after=after,
                details=details,
            ),
            source_refs=tuple([*(r.source for r in records), self._blob_source("context-compaction-details", details)]),
        )
        source = SourceRef(
            source_id=f"context-compaction-{event.event_id}",
            source_kind="generated",
            locator=summary.uri,
            blob=summary,
            event_id=event.event_id,
        )
        self._last_compaction = {"history_ids": identities, "message": compact_message,
            "source": source.model_dump(mode="json"), "event_id": event.event_id}
        return event, compact_message, source

    def _project_message(
        self,
        record: HistoryRecord,
        newest_large_result: dict[str, str],
        last_image_occurrence: dict[str, str],
        sent_image_hashes: set[str],
    ) -> dict:
        result = copy.deepcopy(record.message)
        if record.kind == "tool_result" and _message_content_size(result) >= self.policy.large_result_bytes:
            content_sha = _message_content_sha(result)
            full_record = newest_large_result.get(content_sha)
            if full_record is not None and full_record != record.history_id:
                result["content"] = json.dumps(
                    {
                        "context_reference": {
                            "content_sha256": content_sha,
                            "full_result_history_id": full_record,
                            "reason": "exact repeated large tool result",
                        }
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
        content = result.get("content")
        if not isinstance(content, list):
            return result
        blocks = []
        for block in content:
            sha = _image_block_hash(block)
            if sha is None:
                blocks.append(block)
                continue
            matches = [
                self._images[key]
                for key in record.image_keys
                if self._images[key].image.sha256 == sha
            ]
            active = [image for image in matches if image.active]
            if (
                active
                and last_image_occurrence.get(sha) == record.history_id
                and sha not in sent_image_hashes
            ):
                blocks.append(block)
                sent_image_hashes.add(sha)
            else:
                identities = ",".join(sorted(image.key for image in matches))
                reason = (
                    "duplicate bytes already retained elsewhere in this request"
                    if active
                    else "outside active context"
                )
                blocks.append({
                    "type": "text",
                    "text": f"Image bytes omitted ({reason}); retrieve exact identity: {identities}",
                })
        result["content"] = blocks
        return result

    def _record_result_reference_compaction(
        self,
        records: list[HistoryRecord],
        newest_large_result: dict[str, str],
    ):
        replacements = [
            {
                "history_id": record.history_id,
                "event_id": record.source.event_id,
                "content_sha256": _message_content_sha(record.message),
                "full_result_history_id": newest_large_result[_message_content_sha(record.message)],
            }
            for record in records
        ]
        event_ids = self._event_ids(records)
        if not event_ids:
            raise ValueError("large-result compaction needs prior recorded event references")
        summary = self.store.put_json({"kind": "large_result_references", "replacements": replacements})
        details = self.store.put_json(
            {"strategy": "exact content hash; preserve tool envelopes", "replacements": replacements}
        )
        return self.store.append(
            self._context_payload(
                action="compact",
                reason="exact repeated large tool results replaced by hash references",
                summary=summary,
                replaced_event_ids=event_ids,
                before=self._projection_snapshot(),
                after=summary,
                details=details,
            ),
            source_refs=tuple(
                [*(record.source for record in records), self._blob_source("context-result-reference", details)]
            ),
        )

    def _records(self, history_ids: tuple[str, ...]) -> tuple[HistoryRecord, ...]:
        by_id = {record.history_id: record for record in self._history}
        try:
            return tuple(by_id[history_id] for history_id in history_ids)
        except KeyError as error:
            raise ValueError(f"unknown history ID: {error.args[0]}") from error

    @staticmethod
    def _event_ids(records: tuple[HistoryRecord, ...] | list[HistoryRecord]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(r.source.event_id for r in records if r.source.event_id))

    def _active_image_keys(self) -> set[str]:
        return {key for key, image in self._images.items() if image.active}

    def _projection_snapshot(self) -> HashedBlobRef:
        return self.store.put_json(
            {"history_ids": [record.history_id for record in self._history],
             "active_images": sorted(self._active_image_keys()),
             "state_revisions": {key: entry.revision for key, entry in sorted(self._state.items())}}
        )

    def _blob_source(self, source_id: str, blob: HashedBlobRef) -> SourceRef:
        return SourceRef(
            source_id=f"{source_id}-{blob.sha256[:12]}",
            source_kind="generated",
            locator=blob.uri,
            blob=blob,
        )

    @staticmethod
    def _context_payload(**values) -> ContextEventPayload:
        # Stage 2 adds audit detail fields while retaining stage-0 compatibility.
        supported = ContextEventPayload.model_fields
        filtered = {key: value for key, value in values.items() if key in supported}
        if values.get("action") == "retain_image" and "retain_image" not in _context_actions():
            raise RuntimeError("ContextEventPayload must support retain_image before projection")
        return ContextEventPayload(**filtered)


def _without_audit_metadata(value):
    if isinstance(value, dict):
        return {key: _without_audit_metadata(item) for key, item in value.items()
                if key not in {"file", "record", "source_refs", "blob", "locator", "basis"}
                and not any(part in key.casefold() for part in ("sha256", "hash", "blob"))}
    if isinstance(value, list):
        return [_without_audit_metadata(item) for item in value]
    return value


def _display_issues(value, seen):
    """Deduplicate complete issue values; retain original text and rich objects."""
    groups = {"unresolved", "uncertain", "conflicts", "unbuilt_openings", "unsupported",
              "report_unresolved", "report_status", "status", "basis"}
    if isinstance(value, dict) and set(value) <= groups:
        values = [item for key, item in value.items() if key not in {"status", "basis", "report_status"}]
    elif isinstance(value, list):
        values = value
    elif value:
        cleaned = _without_audit_metadata(value)
        encoded = json_bytes(cleaned)
        if cleaned and encoded not in seen:
            seen.add(encoded)
            return [cleaned]
        return []
    else:
        return []
    return [issue for item in values for issue in _display_issues(item, seen)]


def _context_actions() -> set[str]:
    annotation = ContextEventPayload.model_fields["action"].annotation
    return set(getattr(annotation, "__args__", ()))


def _embedded_images(message: dict) -> tuple[tuple[str, bytes, str, str | None], ...]:
    content = message.get("content")
    if not isinstance(content, list):
        return ()
    images = []
    for block in content:
        parsed = _image_block_data(block)
        if parsed is not None:
            images.append(parsed)
    return tuple(images)


def _image_block_hash(block) -> str | None:
    parsed = _image_block_data(block)
    return parsed[0] if parsed else None


def _image_block_data(block) -> tuple[str, bytes, str, str | None] | None:
    if not isinstance(block, dict) or block.get("type") != "image_url":
        return None
    image = block.get("image_url")
    url = image.get("url") if isinstance(image, dict) else None
    if not isinstance(url, str) or not url.startswith("data:image/") or ";base64," not in url:
        raise ValueError("context images must be captured data URLs")
    _, encoded = url.split(",", 1)
    try:
        data = base64.b64decode(encoded, validate=True)
    except ValueError as error:
        raise ValueError("invalid image data URL") from error
    media_type = url[5:].split(";", 1)[0]
    detail = image.get("detail")
    if "detail" in image and (not isinstance(detail, str) or detail not in {"auto", "low", "high", "original"}):
        raise ValueError("unsupported context image detail")
    return hashlib.sha256(data).hexdigest(), data, media_type, detail


def _message_content_sha(message: dict) -> str:
    return hashlib.sha256(json_bytes(message.get("content"))).hexdigest()


def _message_content_size(message: dict) -> int:
    return len(json_bytes(message.get("content")))


def _has_tool_images(message: dict) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "image_url" for block in content
    )


def _tool_call_ids(message: dict) -> tuple[str, ...]:
    calls = message.get("tool_calls")
    if message.get("role") != "assistant" or not isinstance(calls, list) or not calls:
        return ()
    ids = tuple(call.get("id") for call in calls if isinstance(call, dict))
    if len(ids) != len(calls) or any(not isinstance(call_id, str) or not call_id for call_id in ids):
        raise ValueError("assistant tool_calls need nonempty string IDs")
    if len(ids) != len(set(ids)):
        raise ValueError("assistant tool_call IDs must be unique")
    return ids


def _interaction_groups(records: list[HistoryRecord]) -> tuple[tuple[HistoryRecord, ...], ...]:
    """Group protocol-atomic assistant/tool/image exchanges."""

    groups: list[tuple[HistoryRecord, ...]] = []
    index = 0
    while index < len(records):
        start = records[index]
        call_ids = _tool_call_ids(start.message)
        if not call_ids:
            groups.append((start,))
            index += 1
            continue
        group = [start]
        index += 1
        while index < len(records) and records[index].message.get("role") == "tool":
            group.append(records[index])
            index += 1
        while index < len(records) and records[index].message.get("role") == "user" and _has_tool_images(records[index].message):
            group.append(records[index])
            index += 1
        groups.append(tuple(group))
    return tuple(groups)


def _validate_projected_tool_protocol(messages: tuple[dict[str, JsonValue], ...]) -> None:
    pending: set[str] | None = None
    seen: set[str] = set()
    for message in messages:
        role = message.get("role")
        calls = _tool_call_ids(message)
        if calls:
            if pending:
                raise ValueError("a new assistant tool batch started before all replies")
            pending = set(calls)
            seen = set()
            continue
        if role == "tool":
            if pending is None:
                raise ValueError("projected context contains an orphan tool reply")
            call_id = message.get("tool_call_id")
            if call_id not in pending or call_id in seen:
                raise ValueError("projected tool reply has an unknown or duplicate call ID")
            seen.add(call_id)
            if seen == pending:
                pending = None
            continue
        if pending is not None:
            raise ValueError("projected context split an assistant tool batch")
    if pending is not None:
        raise ValueError("projected context ended before all tool replies")
