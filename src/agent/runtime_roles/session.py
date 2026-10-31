"""Drawing-role tasks share one ledger and return immutable, verified artifacts."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import io
import json
import math
import re
import time
from collections.abc import Mapping
from contextlib import AsyncExitStack
from pathlib import Path

import jsonschema
from PIL import Image

from src.agent.runtime_context import update_building_context
from src.agent.runtime_tools import local_observer_role
from src.agent_runtime.context import StateEntry
from src.agent_runtime.budget import RuntimeBudget
from .context_policy import context_policy_from_effective, effective_role_context
from src.agent_runtime.loop import RunLimits, Runtime
from src.agent_runtime.providers import LIVE_PROVIDERS, provider_parameters
from src.agent_runtime.store import json_bytes
from src.agent_runtime.versions import make_versions
from src.harness_contracts import HashedBlobRef, ToolExecutionPayload, ToolInvocationPayload, RunLifecyclePayload
from src.harness_contracts.roles import ToolGrant

from .accounting import role_accounting
from .artifacts import ArtifactRegistry, valid_task_id
from .coordinates import READER_COORDINATES, task_coordinates
from .feedback import reader_batch_reply
from .parameters import normalize_stringified_parameters
from .plan_reading import geometry_ready


def envelope(value, *, error=False):
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
            "structuredContent": value, "isError": error}


def role_parameters(config):
    if config["provider"] == "scripted":
        return {"max_tokens": config["output_tokens"], **{k: config[k] for k in
            ("reasoning_effort", "temperature", "enable_thinking") if config.get(k) is not None}}
    return provider_parameters(config["provider"], model=config["model"], output_tokens=config["output_tokens"],
        reasoning_effort=config.get("reasoning_effort"), temperature=config.get("temperature"),
        thinking=True if config.get("enable_thinking") is None else config["enable_thinking"])


def schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


TASK_SCHEMA = schema({
    "task_id": {"type": "string"}, "role_id": {"enum": ["plan_reader", "elevation_reader"]},
    "image": {"type": "string"}, "target": {"type": "string", "minLength": 1},
    "instructions": {"type": "string", "maxLength": 400},
    "origin": {"type": "string", "minLength": 1},
    "previous_task_id": {"type": "string"},
    "issues": {"type": "array", "items": {"type": "string"}},
    "rework_targets": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    "budget": schema({"model_calls": {"type": "integer", "minimum": 1},
                      "tool_calls": {"type": "integer", "minimum": 0},
                      "tokens": {"type": "integer", "minimum": 1},
                      "seconds": {"type": "number", "exclusiveMinimum": 0}}),
}, ("task_id", "role_id", "image", "target"))

# Readers get these per-task allowances, capped by what remains of the run; tokens
# stay under the run's own total. The coordinator no longer chooses them: in the
# 10-06 sm24 debug run it guessed 100-120k tokens, while a plan reader alone used
# ~0.9M and an elevation reader up to ~0.25M, so readers stopped after two or three
# requests. Measured use: plan reader 19-30 requests, elevation reader up to 8.
ROLE_TASK_BUDGET = {
    "plan_reader": {"model_calls": 40, "tool_calls": 120, "seconds": 3600},
    "elevation_reader": {"model_calls": 16, "tool_calls": 50, "seconds": 1800},
}
COORDINATOR_TASK_SCHEMA = schema({key: value for key, value in TASK_SCHEMA["properties"].items() if key != "budget"},
                                 (*TASK_SCHEMA["required"], "origin"))
# Internal task admission still reads older saved tasks without an origin. The
# model-facing schema requires one for every new dispatch.
COORDINATOR_ORDINARY_TOOLS = frozenset({"inputs", "view_image", "inspect_candidate", "check_openings",
    "view_elevation_candidate", "finish_bim"})
LEVEL_REFERENCE_SCHEMA = schema({"task_id": {"type": "string"}, "elevation_id": {"type": "string"}},
                                ("task_id", "elevation_id"))
HEIGHT_SCHEMA = schema({"match_id": {"oneOf": [{"type": "string"},
    {"type": "array", "items": {"type": "string"}, "minItems": 1}]}}, ("match_id",))

EXTRA_TOOLS = [
    {"name": "edit_bim", "description":
     "Edits need reasons. Separate height batch: id,sill_m,head_m,reader_evidence={task_id,sha256,opening_id} "
     "or image,bbox; reader evidence needs no new views. use(id,role,image), note(text) retain review; "
     "position(id,along_start_m,along_end_m,image) stays on its host, needs review. Optional separate "
     "position_decision: decision_id,choice=keep_plan/use_elevation/reread_plan/reread_elevation,reason,view_ids. "
     "Above 30cm inspect both original crops via role_state and decide before delivery; reread remains blocking.",
     "inputSchema": schema({"candidate": {"type": "string"}, "edits": {"type": "array", "minItems": 1,
         "maxItems": 100, "items": schema({
             "action": {"enum": ["height", "use", "position", "note", "position_decision"]}, "id": {"type": "string"},
             "decision_id": {"type": "string"},
             "choice": {"enum": ["keep_plan", "use_elevation", "reread_plan", "reread_elevation"]},
             "view_ids": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
             "reason": {"type": "string", "minLength": 1}, "image": {"type": "string"},
             "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
             "reader_evidence": schema({"task_id": {"type": "string"}, "sha256": {"type": "string"},
                 "opening_id": {"type": "string"}}, ("task_id", "sha256", "opening_id")),
             "sill_m": {"type": "number"}, "head_m": {"type": "number"}, "role": {"type": "string"},
             "basis": {"enum": ["observed", "inferred"]}, "along_start_m": {"type": "number"},
             "along_end_m": {"type": "number"}, "text": {"type": "string", "minLength": 1}},
             ("action", "reason"))}}, ("candidate", "edits"))},
    {"name": "delegate_readers", "description": "Dispatch single-image readers concurrently and wait until every task in this call ends. On the first call, admitted plan and cardinal-elevation drawings omitted from tasks are added with default targets and origin; the result lists them. Give floor/facade target and common origin. Optional instructions (at most 400 characters) contain only drawing facts or a specific rework problem; method, units and coordinates come from the reader guide and runtime. Rework uses a new task_id, previous_task_id and explicit rework_targets; the runtime preserves every unpointed plan or elevation object and its original-reading audit. Completed IDs reuse deliveries; allowances come from the role.",
     "inputSchema": schema({"tasks": {"type": "array", "items": COORDINATOR_TASK_SCHEMA, "minItems": 1, "maxItems": 32}}, ("tasks",))},
    {"name": "read_role_artifact", "description": "Read verified reader data; large fields are browsed by pointer/offset; the full artifact is retained.",
     "inputSchema": schema({"task_id": {"type": "string"}, "sha256": {"type": "string"},
                            "pointer": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}}, ("task_id",))},
    {"name": "build_from_artifact", "description": "Build a validated reader plan without retranscription. First read elevation levels: optional z_floor and ceiling_height in metres each require their *_evidence (delivered elevation task_id + elevation_id). z_floor equals its cited Z; ceiling_height equals cited top Z minus floor Z. Overrides change a compilation copy, keep evidence, and leave the reader artifact intact. Identical inputs reuse the build.",
     "inputSchema": schema({"task_id": {"type": "string"}, "sha256": {"type": "string"},
         "z_floor": {"type": "number"}, "z_floor_evidence": LEVEL_REFERENCE_SCHEMA,
         "ceiling_height": {"type": "number", "exclusiveMinimum": 0},
         "ceiling_height_evidence": LEVEL_REFERENCE_SCHEMA}, ("task_id", "sha256"))},
    {"name": "match_elevation", "description": "Match a validated elevation_reader artifact to a saved whole-building candidate. Returns matched openings, source-only/elevation-only openings and conflicts. Does not modify heights.",
     "inputSchema": schema({"task_id": {"type": "string"}, "candidate": {"type": "string"}}, ("task_id", "candidate"))},
    {"name": "apply_elevation_heights", "description": "Give match_id as one ID or a list: all safe facade heights save at most one candidate. Calls serialize on the latest usable draft; changed opening XY/hosts require a new match. Equal values only record evidence, with no new candidate. Unmatched/conflicting openings stay unresolved.",
     "inputSchema": HEIGHT_SCHEMA},
    {"name": "role_state", "description": "Read progress. position_details=true includes position readings/boxes; position_decision_id returns both original crops and saved view_ids. Pending >30cm differences block delivery until explicitly decided.", "inputSchema": schema({"position_details": {"type": "boolean"}, "position_decision_id": {"type": "string"}})},
    {"name": "review_role_assembly", "description": "Confirm actual geometry changes with reasons; audited regularization needs none. Height/use/note edits retain confirmation. Missing-floor decisions accept partial delivery; stale/unverified lineage needs repair.",
     "inputSchema": schema({"review_id": {"type": "string"}, "decisions": {"type": "array", "items": schema({
         "change_id": {"type": "string"}, "reason": {"type": "string", "minLength": 1}}, ("change_id", "reason"))}}, ("review_id", "decisions"))},
]

# The lower-level methods remain available to offline scripts, but no longer
# occupy the coordinator's tool catalog or its model-side permission grants.
INTERNAL_TOOLS = [t for t in EXTRA_TOOLS if t["name"] in {
    "build_from_artifact", "match_elevation", "apply_elevation_heights"}]
EXTRA_TOOLS = [t for t in EXTRA_TOOLS if t not in INTERNAL_TOOLS]
EXTRA_TOOLS.insert(2, {"name": "assemble_from_readers",
    "description": "Assemble floors, uses and located safe heights. Omit task_ids for latest deliveries; explicit selections persist. Audited regularization needs no review; 10-30cm differences keep plan, while >30cm differences require an explicit decision before delivery. Reassemble after reader rework; unchanged inputs reuse receipts. level_overrides cite absolute Z; ceiling_height=top Z-floor Z.",
    "inputSchema": schema({"task_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "uniqueItems": True},
        "level_overrides": {"type": "array", "items": schema({
            "floor_id": {"type": "string"}, "z_floor": {"type": "number"},
            "z_floor_evidence": LEVEL_REFERENCE_SCHEMA,
            "ceiling_height": {"type": "number", "exclusiveMinimum": 0},
            "ceiling_height_evidence": LEVEL_REFERENCE_SCHEMA}, ("floor_id",))}})})


_DEFAULT_READER_ORIGIN = (
    "Default common origin: southwest outer building corner at (0,0,0); "
    "+x East, +y North, +z Up."
)
_CARDINAL_DRAWINGS = {f"{name.casefold()}_view.png": name
                      for name in ("North", "South", "East", "West")}


def _validate_task_instructions(tasks):
    if not isinstance(tasks, list):
        return
    for index, task in enumerate(tasks):
        if not isinstance(task, dict) or not isinstance(task.get("instructions"), str):
            continue
        excess = len(task["instructions"]) - 400
        if excess > 0:
            identity = task.get("task_id") or f"tasks[{index}]"
            raise ValueError(f"reader task {identity} instructions exceed 400 characters by {excess}; "
                             "instructions are optional and should contain only drawing facts or a "
                             "specific rework problem")


def _compact_failure_result(value):
    """Project a saved tool result into a short, located rework handoff."""
    if not isinstance(value, Mapping):
        return None
    structured = value.get("structuredContent")
    if not isinstance(structured, Mapping):
        structured = value
    result = {key: copy.deepcopy(structured[key]) for key in
              ("status", "error_type", "reason", "message", "receipt_file") if key in structured}
    errors = structured.get("format_errors")
    if isinstance(errors, list):
        result["format_errors"] = [copy.deepcopy(row) for row in errors[:24] if isinstance(row, Mapping)]
        if len(errors) > len(result["format_errors"]):
            result["additional_format_error_count"] = len(errors) - len(result["format_errors"])
    hint = structured.get("repair_hint")
    if isinstance(hint, Mapping):
        kept = {key: copy.deepcopy(hint[key]) for key in (
            "path", "note", "current", "allowed_floor_bounds_m", "junction_repairs",
            "junction_repairs_remaining", "space_seeds", "missing_wall_check",
        ) if key in hint}
        if kept:
            result["repair_hint"] = kept
    action = structured.get("next_action")
    if isinstance(action, (str, Mapping)):
        result["next_action"] = copy.deepcopy(action)
    for key in ("unhosted_openings", "source_findings", "topology_issues"):
        rows = structured.get(key)
        if isinstance(rows, list) and rows:
            result[key] = copy.deepcopy(rows[:12])
            if len(rows) > 12:
                result[key + "_remaining"] = len(rows) - 12
    for key, count in structured.items():
        if key.endswith("_remaining") and isinstance(count, int) and not isinstance(count, bool):
            result[key] = count
    return result or None


def _default_floor_target(image, index):
    stem = Path(image).stem
    match = re.fullmatch(r"(?i)(?:f(\d+)|(\d+)f)(?:_view)?", stem)
    number = next((part for part in match.groups() if part is not None), None) if match else None
    return "F" + number if number is not None else f"F{index + 1}"


def _auto_task_id(role_id, image, used):
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(image).stem).strip("_") or "drawing"
    prefix = "auto_plan" if role_id == "plan_reader" else "auto_elevation"
    base = (prefix + "_" + stem)[:60]
    candidate, number = base, 2
    while candidate in used:
        suffix = f"_{number}"
        candidate = base[:64 - len(suffix)] + suffix
        number += 1
    used.add(candidate)
    return candidate


class RoleSession:
    def __init__(self, *, store, frozen, routes, adapter_factory, limits, root,
                 max_concurrent_readers=8, started_epoch=None, reader_fault_hook=None,
                 role_contexts=None, connection_routes=None):
        if type(max_concurrent_readers) is not int or max_concurrent_readers < 1:
            raise ValueError("max_concurrent_readers must be a positive integer")
        self.store, self.frozen, self.routes = store, frozen, routes
        self.adapter_factory, self.limits, self.root = adapter_factory, limits, Path(root)
        self.role_contexts = role_contexts or {name: effective_role_context(name)
                                              for name in routes}
        self.connection_routes = connection_routes or {
            name: {"route_id": (route.provider if hasattr(route, "provider") else route["provider"])}
            for name, route in routes.items()}
        self.run_directory = frozen.run_directory
        self.registry = ArtifactRegistry(store)
        self.started_epoch = time.time() if started_epoch is None else started_epoch
        self.max_concurrent_readers = max_concurrent_readers
        self.slots = asyncio.Semaphore(max_concurrent_readers)
        self.write_lock = asyncio.Lock()
        self.reader_fault_hook = reader_fault_hook
        self.manifest = json.loads((self.run_directory / "inputs.json").read_bytes())
        self.schemas = {tool["name"]: tool["inputSchema"] for tool in [*EXTRA_TOOLS, *INTERNAL_TOOLS]}
        self.ordinary_schemas = {}
        from .assembly_review import AssemblyReview, PositionReview
        self.assembly = AssemblyReview(self)
        self.positions = PositionReview(self)

    async def list_tools(self):
        ordinary = await self.frozen.list_tools()
        ordinary = [copy.deepcopy(tool) for tool in ordinary if tool["name"] in COORDINATOR_ORDINARY_TOOLS]
        from .reader_delivery import LOCAL_REVIEW_SCHEMA
        for tool in ordinary:
            if tool["name"] == "finish_bim":
                tool["inputSchema"]["properties"]["architectural_review"] = LOCAL_REVIEW_SCHEMA
                tool["description"] += " Include architectural_review.summary: judge assembled storeys, floor differences, layout/connectivity and openings; reuse the readers' local overlay checks."
        self.ordinary_schemas = {tool["name"]: tool["inputSchema"] for tool in ordinary}
        return [*ordinary, *EXTRA_TOOLS]

    def repeatability(self, name):
        if name in self.schemas:
            if name == "review_role_assembly":
                return "idempotent_write"
            return "non_idempotent_write" if name in {"build_from_artifact", "apply_elevation_heights", "assemble_from_readers", "edit_bim"} else "read_only"
        return self.frozen.repeatability(name)

    def recovery_policy(self, name):
        if name == "delegate_readers":
            return "resume_task"
        if name in self.schemas:
            return "manual"
        return getattr(self.frozen, "recovery_policy", lambda _: "manual")(name)

    def snapshot_state(self):
        value = self.frozen.snapshot_state()
        return {"bim": value, "reader_artifacts": {key: row.get("artifact") for key, row in self.registry.records.items()
                                                 if row.get("artifact")}, "assembly_review": self.assembly.current(),
                "reader_floor_sources": self.assembly._load("role_floor_sources.json", {}),
                "position_review": self.positions.summary()}

    def artifacts(self):
        return self.frozen.artifacts()

    def image_origins(self, raw):
        return self.frozen.image_origins(raw)

    def _recoverable_plan_trial_event(self, task_id):
        """Select the latest known trial outcome, without crossing an unknown write."""
        from .continuation import imported_draft_selection
        imported = imported_draft_selection(self, task_id)
        if imported is not None:
            return imported
        child = self.registry.child(task_id)
        latest = None
        rejected_calls = set()
        for event in reversed(child.events):
            payload = event.payload
            if (payload.event_type not in {"tool_invocation", "tool_execution"}
                    or payload.tool_name != "trial_plan_bim"):
                continue
            if payload.event_type == "tool_invocation" and payload.call_id in rejected_calls:
                continue
            if payload.event_type == "tool_execution" and payload.outcome == "failed":
                try:
                    raw = child.resolve(payload.raw_result)
                    structured = raw.get("structuredContent", raw) if isinstance(raw, Mapping) else {}
                except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                    structured = {}
                if (isinstance(structured, Mapping) and structured.get("status") == "rejected"
                        and structured.get("draft_unchanged") is True):
                    # Only an explicit prewrite rejection can be crossed. An
                    # unknown write or missing durable outcome below still blocks recovery.
                    rejected_calls.add(payload.call_id)
                    continue
            latest = event
            break
        if latest is None:
            return None
        payload = latest.payload
        if payload.event_type == "tool_invocation":
            return {
                "status": "unavailable", "event_id": latest.event_id,
                "reason": (
                    "The latest trial_plan_bim invocation has no durable execution outcome. Earlier drafts "
                    "and workspace-only receipts are diagnostic evidence only and are not imported as a baseline."
                ),
            }
        if payload.outcome == "unknown":
            return {
                "status": "unavailable", "event_id": latest.event_id,
                "reason": (
                    "The latest trial_plan_bim write outcome is unknown. Earlier drafts and workspace-only "
                    "receipts are diagnostic evidence only and are not imported as an editable baseline."
                ),
            }
        if payload.raw_result.kind == "missing":
            return {
                "status": "unavailable", "event_id": latest.event_id,
                "reason": "The latest trial event has no durable raw-result capture.",
            }
        try:
            raw = child.resolve(payload.raw_result)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            return {"status": "unavailable", "event_id": latest.event_id,
                    "reason": f"The latest trial result could not be hash-verified: {error}"}
        structured = raw.get("structuredContent") if isinstance(raw, Mapping) else None
        if not isinstance(structured, Mapping):
            structured = raw if isinstance(raw, Mapping) else {}
        required = ("receipt_file", "plan_sha256")
        draft_status = "passed" if payload.outcome == "succeeded" else "failed"
        if draft_status == "passed":
            required += ("compiled_numeric_plan_sha256", "candidate")
            required += (("reading_product_file", "reading_product_sha256") if structured.get("reading_geometry_ready")
                         else ("candidate_source_sha256",))
        if (structured.get("status") != draft_status
                or (draft_status == "passed" and not geometry_ready(structured))
                or any(not isinstance(structured.get(key), str) or not structured[key] for key in required)):
            return {
                "status": "unavailable", "event_id": latest.event_id,
                "reason": "The latest trial event has no matching saved declaration outcome and receipt identity.",
            }
        if structured.get("editable_draft", {}).get("editable") is False:
            return {"status": "unavailable", "event_id": latest.event_id,
                    "reason": structured["editable_draft"].get("reason", "Saved format needs a complete replacement.")}
        receipt_hash = {}
        if draft_status == "passed":
            audit = structured.get("receipt_audit")
            if audit is not None:
                if (not isinstance(audit, Mapping) or audit.get("json_pointer") != ""
                        or audit.get("receipt_file") != structured["receipt_file"]
                        or audit.get("hash_encoding") != "utf-8-json-sort_keys-compact-ensure_ascii_false"
                        or not isinstance(audit.get("sha256"), str)
                        or not re.fullmatch(r"[0-9a-f]{64}", audit["sha256"])):
                    return {"status": "unavailable", "event_id": latest.event_id,
                            "reason": "The passed trial has no valid complete receipt audit identity."}
                digest = audit["sha256"]
            else:
                # Legacy full captures can prove the entire receipt. A partial
                # projection cannot match this digest and is rejected on load.
                digest = hashlib.sha256(json.dumps(structured, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
            receipt_hash["expected_receipt_sha256"] = digest
        return {
            "status": "available", "draft_status": draft_status,
            "event_id": latest.event_id, "sequence": latest.sequence,
            "raw_result": payload.raw_result.model_dump(mode="json"),
            **{key: structured[key] for key in required},
            "compiled_numeric_plan_sha256": structured.get("compiled_numeric_plan_sha256"),
            **receipt_hash,
        }

    def _verified_recovery_trial(self, task_id, selection, expected_image_sha256):
        """Recheck event-selected products both at admission and at reader startup."""
        from .trial import PlanTrial
        record = self.registry.records[task_id]
        workspace = self.registry.child(task_id).task_directory / "bim/trial_workspace"
        prior = PlanTrial(None, image_name=record["image"], workspace=workspace,
                          receipt_directory=workspace / "trial_receipts")
        identity = {key: selection[key] for key in
                    ("receipt_file", "plan_sha256", "compiled_numeric_plan_sha256")}
        if selection.get("draft_status", selection.get("status")) == "passed":
            identity.update({key: selection[key] for key in
                             ("candidate", "candidate_source_sha256", "reading_product_file", "reading_product_sha256", "expected_receipt_sha256")
                             if key in selection})
            _, receipt = prior.verified_unsubmitted_draft(expected_image_sha256, **identity)
        else:
            _, receipt = prior.verified_failed_draft(expected_image_sha256, **identity)
        return prior, receipt

    def _failure_handoff(self, task_id):
        """Reference prior evidence and carry its last actionable failures forward."""
        record = self.registry.records[task_id]
        handoff = {
            "previous_task_id": task_id,
            "status": record["status"],
            "runtime_status": record.get("runtime_status"),
            "reason": record.get("reason"),
            "complete_record": copy.deepcopy(record.get("record_blob")),
            "tool_failures": [],
        }
        if record["status"] == "completed":
            handoff["instruction"] = (
                "Start from previous_artifact. Change only the declared rework_targets for the supplied drawing facts; "
                "the prior delivery's earlier corrected failures are not current rework evidence."
            )
            return handoff
        resolved_through = None
        if record.get("role_id") == "plan_reader":
            trial_event = self._recoverable_plan_trial_event(task_id)
            if trial_event and trial_event["status"] == "available":
                _, receipt = self._verified_recovery_trial(task_id, trial_event, record.get("input_sha256"))
                passed = trial_event["draft_status"] == "passed"
                if passed:
                    resolved_through = trial_event["sequence"]
                handoff["editable_draft"] = {
                    "status": trial_event["draft_status"], "validation_passed": passed, "editable": True,
                    "submission_accepted": False,
                    "event_id": trial_event["event_id"],
                    "raw_result": trial_event["raw_result"],
                    "plan_sha256": receipt["plan_sha256"],
                    "receipt_file": receipt["receipt_file"],
                    "compiled_numeric_plan_sha256": receipt.get("compiled_numeric_plan_sha256"),
                    **({**{key: receipt[key] for key in ("candidate", "candidate_source_sha256", "reading_product_file", "reading_product_sha256") if key in receipt},
                        "expected_receipt_sha256": trial_event["expected_receipt_sha256"],
                        "topology_decisions_required": len(receipt.get("topology_issues", []))} if passed else {}),
                    "instruction": (
                        "This hash-verified declaration passed geometry but was not submitted. Read it with "
                        "read_plan_draft(field='plan'). Preserve it unchanged unless the declared rework_targets "
                        "require a correction. Run trial_plan_bim in this task (the complete unchanged declaration "
                        "is allowed), then explicitly submit_plan_reading; the new task must establish its own "
                        "verified trial before submission. Do not invent geometric changes or repeat corrected "
                        "failures from before this passed trial. Read inherited validation with "
                        "read_plan_draft(field='report:/topology_issues') when needed."
                        if passed else
                        "This hash-verified complete failed declaration is the operations baseline in the new task. "
                        "Revise the located objects; unchanged objects and their source descriptions remain in place."
                    ),
                }
            elif trial_event and trial_event["status"] == "unavailable":
                handoff["draft_recovery"] = trial_event
        child = self.registry.child(task_id)
        known_failure = False
        for event in reversed(child.events):
            if resolved_through is not None and event.sequence <= resolved_through:
                break
            payload = event.payload
            if (payload.event_type != "tool_execution"
                    or payload.tool_name not in {"trial_plan_bim", "submit_plan_reading", "submit_elevation_reading"}
                    or payload.outcome == "succeeded"):
                continue
            row = {
                "tool": payload.tool_name,
                "outcome": payload.outcome,
                "evidence": {
                    "event_id": event.event_id,
                    "raw_result": payload.raw_result.model_dump(mode="json"),
                },
            }
            if payload.outcome != "unknown":
                try:
                    compact = _compact_failure_result(child.resolve(payload.raw_result))
                except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                    compact = None
                if compact:
                    row["feedback"] = compact
                    receipt_file = compact.get("receipt_file")
                    if isinstance(receipt_file, str):
                        row["evidence"]["receipt_file"] = receipt_file
                    known_failure = True
            handoff["tool_failures"].append(row)
            if len(handoff["tool_failures"]) >= 2 or (known_failure and payload.outcome != "unknown"):
                break
        handoff["instruction"] = handoff.get("editable_draft", {}).get("instruction") or (
            "Continue from this saved evidence. Correct the located failure while preserving all other observed "
            "walls, openings, rooms and connections; inspect the original wherever the handoff does not prove a change."
        )
        return handoff

    async def call_tool(self, name, arguments):
        if name not in self.schemas and name not in COORDINATOR_ORDINARY_TOOLS:
            return envelope({"status": "rejected", "reason": f"{name} is not a coordinator tool"}, error=True)
        if name not in self.schemas and self.frozen.repeatability(name) != "read_only":
            async with self.write_lock:
                return await self._call_tool(name, arguments)
        return await self._call_tool(name, arguments)

    async def _call_tool(self, name, arguments):
        original_arguments = arguments
        try:
            if name not in self.schemas:
                if name not in self.ordinary_schemas:
                    await self.list_tools()
                if name not in self.ordinary_schemas:
                    raise ValueError(f"{name} is not available in the coordinator catalog")
                arguments = normalize_stringified_parameters(arguments, self.ordinary_schemas[name])
                jsonschema.validate(arguments, self.ordinary_schemas[name])
                if self.frozen.repeatability(name) != "read_only" and name != "finish_bim":
                    self.assembly.guard()
                if name == "finish_bim":
                    candidate = arguments.get("candidate")
                    if candidate:
                        self.assembly.check(candidate, require_all=True)
                        self.assembly.guard()
                        self.positions.guard(candidate)
                    from .building_review import prepare_review
                    judgment = prepare_review(self, candidate, arguments.get("architectural_review"))
                    arguments = {key: value for key, value in arguments.items() if key != "architectural_review"}
                if name == "check_openings":
                    from .opening_checks import check_openings
                    result = await check_openings(self, arguments)
                else:
                    result = await self.frozen.call_tool(name, arguments)
                if name == "finish_bim" and not result.get("isError"):
                    self.positions.write_delivery(arguments["candidate"])
                    from .building_review import attach_review
                    attach_review(self, judgment)
                    # Role attachments change delivery.json/html after the frozen
                    # finish. Attest their final bytes before domain completion.
                    from scripts.tool_scripts.bim_agent_saved_result import full_result_metadata
                    final = full_result_metadata(result, self.run_directory)
                    final["delivery_artifact_sha256"] = {file: hashlib.sha256(
                        (self.run_directory / file).read_bytes()).hexdigest()
                        for file in final.get("delivery_artifact_sha256", {})}
                    result = envelope(final)
                from scripts.tool_scripts.bim_agent_saved_result import result_metadata
                meta = {**result_metadata(result), **(result.get("structuredContent") or {})}
                candidate = meta.get("candidate")
                if candidate and meta.get("source_geometry_ready"):
                    report = self.assembly.check(candidate, require_all=name == "assemble_plan_bim")
                    if report is not None:
                        result = self._with_assembly_review(result, report)
                return result
            if name == "apply_elevation_heights" and isinstance(arguments, dict):
                # The former confirm flag carried nothing beyond match_id; GLM's
                # Anthropic-compatible route sent it as the string "true" (10-06 sm24
                # debug run2), so tolerate and drop it rather than block heights.
                arguments = {key: value for key, value in arguments.items() if key != "confirm"}
            arguments = normalize_stringified_parameters(arguments, self.schemas[name])
            if name == "delegate_readers":
                _validate_task_instructions(arguments.get("tasks"))
            jsonschema.validate(arguments, self.schemas[name])
            if name == "edit_bim":
                async with self.write_lock:
                    return await self.edit_candidate(**arguments)
            if name == "delegate_readers":
                invocation = next((event for event in reversed(self.store.events)
                    if event.payload.event_type == "tool_invocation"
                    and event.payload.tool_name == name
                    and event.payload.full_arguments == original_arguments), None)
                value = await self.delegate_many(arguments["tasks"],
                    call_id=invocation.payload.call_id if invocation else None)
                first = {}
                for event in self.store.all_events:
                    payload = event.payload
                    if payload.event_type == "tool_execution" and payload.tool_name in {
                            "submit_plan_reading", "submit_elevation_reading"}:
                        first.setdefault(event.task_id, payload.outcome == "succeeded")
                reply = reader_batch_reply(value, first)
                by_id = {row["task_id"]: row for row in value["results"]}
                for row in reply["results"]:
                    handoff = by_id[row["task_id"]].get("failure_handoff")
                    if handoff:
                        row["failure_handoff"] = copy.deepcopy(handoff)
                return envelope(reply)
            if name == "read_role_artifact":
                from .feedback import reader_artifact_reply
                record = self.registry.records[arguments["task_id"]]
                self.registry._verify_record(record)
                artifact = self.registry.read(arguments["task_id"], sha256=arguments.get("sha256")) if record.get("artifact") else None
                if artifact is None and arguments.get("sha256") is not None:
                    raise ValueError("this reader has no artifact hash")
                return envelope(reader_artifact_reply(record, artifact, pointer=arguments.get("pointer"),
                                                      offset=arguments.get("offset", 0)))
            if name == "assemble_from_readers":
                from .assembly import assemble_from_readers
                async with self.write_lock:
                    return await assemble_from_readers(self, **arguments)
            if name == "build_from_artifact":
                self.assembly.guard()
                return await self.build_from_artifact(**arguments)
            if name == "match_elevation":
                return envelope(self.match(**arguments))
            if name == "apply_elevation_heights":
                self.assembly.guard()
                return await self.apply_heights(arguments["match_id"])
            if name == "review_role_assembly":
                return envelope(self.assembly.acknowledge(**arguments))
            if arguments.get("position_decision_id"):
                return await self.position_evidence(arguments["position_decision_id"])
            return envelope(self.model_state(position_details=arguments.get("position_details", False)))
        except (ValueError, KeyError, jsonschema.ValidationError) as error:
            if name == "edit_bim":
                detail = error.message if isinstance(error, jsonschema.ValidationError) else str(error)
                return envelope({"status": "rejected", "reason": " ".join(detail.split()) +
                    "; edits: height(id,sill_m,head_m,reader_evidence or image+bbox), use(id,role,image), "
                    "position(id,along_start_m,along_end_m,image), note(text), "
                    "position_decision(decision_id,choice,view_ids); each needs reason."}, error=True)
            return envelope({"status": "rejected", "reason": str(error)}, error=True)

    async def edit_candidate(self, candidate, edits):
        from .elevation import _number, _string, _bbox
        from .lineage import guard_replaced_plans, metadata
        from src.agent.roles import require_role
        from scripts.tool_scripts.bim_agent_role_heights import build_role_height_batch_entry
        guard_replaced_plans(self, candidate)
        if any(row["action"] == "position_decision" for row in edits):
            if any(row["action"] != "position_decision" for row in edits):
                raise ValueError("send position_decision in a separate batch")
            return await self.positions.decide(candidate, edits)
        source = self._source(candidate)
        proposal = json.loads((self.run_directory / candidate / "proposal.json").read_bytes())
        spaces = {row["id"]: row for row in source["spaces"]}
        openings = {row["id"]: row for row in source["openings"]}
        operations, entries, types, seen = [], [], [], set()
        notes = {field: list(proposal[field]) for field in ("assumptions", "unresolved")}
        if any(row["action"] == "height" for row in edits) and any(row["action"] != "height" for row in edits):
            raise ValueError("send height edits in a separate batch")
        for row in edits:
            action, reason = row["action"], _string(row["reason"], "reason")
            fields = {"height": {"id", "sill_m", "head_m", "image", "bbox", "basis", "reader_evidence"},
                      "use": {"id", "role", "image", "bbox", "basis"},
                      "position": {"id", "along_start_m", "along_end_m", "image", "bbox"},
                      "note": {"text"}}[action]
            if set(row) - fields - {"action", "reason"}:
                raise ValueError(f"{action} has fields for another operation")
            if action == "note":
                text = _string(row.get("text"), "text")
                if text not in notes["unresolved"]:
                    notes["unresolved"].append(text)
                continue
            identity = _string(row.get("id"), "id")
            if (action, identity) in seen:
                raise ValueError(f"duplicate {action} target {identity}")
            seen.add((action, identity))
            delivered_evidence = None
            if action == "height" and "reader_evidence" in row:
                from .height_evidence import resolve_manual_height_evidence
                delivered_evidence = resolve_manual_height_evidence(
                    self, candidate, identity, row.get("sill_m"), row.get("head_m"), row["reader_evidence"])
                for field in ("image", "bbox"):
                    if field in row and row[field] != delivered_evidence[field]:
                        raise ValueError(f"{field} disagrees with delivered reader evidence")
                row = {**row, "image": delivered_evidence["image"], "bbox": delivered_evidence["bbox"]}
            image = _string(row.get("image"), "image")
            if image not in self.manifest["images"]:
                raise ValueError("image must name an admitted original")
            refs = [f"image:{image}", reason]
            bbox = None
            if "bbox" in row:
                from .plan_review import box
                bbox = _bbox(row["bbox"], "bbox")
                box(bbox, self.manifest["images"][image]["size"])
                refs.append(f"{image}: bbox {bbox}")
            if action == "use":
                if identity not in spaces:
                    raise ValueError(f"unknown space {identity}")
                role = require_role(_string(row.get("role"), "role"))
                basis = "unknown" if role == "unknown" else row.get("basis", "inferred")
                operations.append({"op": "set_space_role", "space_id": identity, "role": role,
                    "basis": basis, "assumptions": [reason] if basis == "inferred" else [],
                    "reason": reason, "source_refs": refs})
                continue
            opening = openings.get(identity)
            if opening is None:
                raise ValueError(f"unknown opening {identity}")
            op = "update_window" if opening["kind"] == "window" else "update_opening"
            if action == "height":
                if bbox is None:
                    raise ValueError("height needs an original-image bbox")
                sill, head = _number(row.get("sill_m"), "sill_m"), _number(row.get("head_m"), "head_m")
                if sill >= head:
                    raise ValueError("height requires sill_m < head_m")
                host = next(item for item in source["boundaries"] if item["id"] == opening["host_boundary_id"])
                if sill < min(p[2] for p in host["vertices"]) or head > max(p[2] for p in host["vertices"]):
                    raise ValueError("height must fit the existing host wall")
                inferred = row.get("basis", "observed") == "inferred"
                entries.append({"claim": {"candidate": candidate,
                    "objects": [{"kind": "window" if opening["kind"] == "window" else "opening", "id": identity}],
                    "basis": "inference" if inferred else "pixels", "reason": reason,
                    "sources": [{"image": image, "box": bbox}],
                    "values": {"height": {"type": "literal", "value": [sill, head], "unit": "m"}},
                    "observation_mode": "candidate_review" if inferred else "direct",
                    "unresolved": [reason] if inferred else []},
                    "action": "apply", "reason": reason,
                    "operations": [{"op": op, "id": identity,
                        "changes": {"z": {"claim": "$claim", "value": "height"}}, "reason": reason}]})
                if delivered_evidence:
                    entries[-1]["reader_evidence"] = delivered_evidence["reader_evidence"]
                types.append("assumption" if inferred else "pixels")
            else:
                start = _number(row.get("along_start_m"), "along_start_m")
                end = _number(row.get("along_end_m"), "along_end_m")
                if start >= end:
                    raise ValueError("position requires along_start_m < along_end_m")
                points = sorted({tuple(p[:2]) for p in opening["vertices"]})
                if len(points) != 2 or (points[0][0] != points[1][0] and points[0][1] != points[1][1]):
                    raise ValueError("position requires an existing straight axis-aligned opening")
                axis = 0 if points[0][0] != points[1][0] else 1
                host = next(item for item in source["boundaries"] if item["id"] == opening["host_boundary_id"])
                low, high = min(p[axis] for p in host["vertices"]), max(p[axis] for p in host["vertices"])
                if start < low - 1e-8 or end > high + 1e-8:
                    raise ValueError("position must fit the existing host wall; it cannot move onto another host")
                if op == "update_window":
                    changes = {"span": [start, end]}
                else:
                    original = next(item for item in proposal["geometry"]["openings"] if item["id"] == identity)
                    p1, p2 = list(original["p1"]), list(original["p2"])
                    p1[axis], p2[axis] = (start, end) if p1[axis] < p2[axis] else (end, start)
                    changes = {"p1": p1, "p2": p2}
                operations.append({"op": op, "id": identity, "changes": changes,
                                   "reason": reason, "source_refs": refs})
        if notes != {field: proposal[field] for field in notes}:
            operations.append({"op": "set_notes", **notes})
        if entries:
            batch = build_role_height_batch_entry(entries, evidence_types=types)
            if all(sorted({p[2] for p in openings[e["claim"]["objects"][0]["id"]]["vertices"]}) ==
                   e["claim"]["values"]["height"]["value"] for e in entries):
                batch["entry"]["action"] = "confirm"
            name, args = "claim_transaction", {"candidate": candidate,
                "entries_json": json.dumps([batch["entry"]], ensure_ascii=False)}
        else:
            if not operations:
                return envelope({"status": "unchanged", "candidate": candidate, "source_geometry_ready": True})
            # Reject the entire batch before a write if a role or geometry operation is invalid.
            from src.agent.geometry.proposal_edits import apply_proposal_edits
            apply_proposal_edits(proposal, operations)
            name, args = "revise_bim", {"candidate": candidate, "operations_json": json.dumps(operations, ensure_ascii=False)}
        identity = "edit:" + hashlib.sha256(json_bytes({"candidate": candidate, "edits": edits})).hexdigest()
        receipt = self.store.directory / "role_operations" / (hashlib.sha256(identity.encode()).hexdigest() + ".json")
        if not receipt.is_file():
            self.assembly.guard()
        # A known write can itself trigger assembly review. Recover its durable
        # result before asking for that review; an unfinished receipt still
        # refuses repetition in _once, and every new edit remains guarded.
        result = await self._once(identity,
                                 name, args, reference={"coordinator_edits": edits})
        meta = metadata(result)
        current = (meta.get("claim_application") or {}).get("candidate") or meta.get("candidate", candidate)
        ready = meta.get("source_geometry_ready") or meta.get("status") == "completed"
        if not ready:
            raise ValueError(str(meta.get("error", meta.get("reason", "edit failed; inspect its saved receipt"))))
        review = self.assembly.check(current)
        return envelope({"status": "completed", "candidate": current, "source_geometry_ready": True,
                         "edited": len(edits), "assembly_review": review,
                         "receipt": meta.get("role_application")})

    def state(self, *, position_details=False):
        return {"readers": self.registry.state(), "max_concurrent_readers": self.max_concurrent_readers,
                "usage": role_accounting(self.store, self.registry), "assembly_review": self.assembly.current(),
                "position_review": self.positions.current() if position_details else self.positions.summary()}

    def reader_progress(self):
        """The model sees task identities and outcomes, not repeated validation audits."""
        readers = []
        for _, record in sorted(self.registry.records.items()):
            artifact = record.get("artifact") or {}
            row = {key: record.get(key) for key in ("task_id", "role_id", "target", "image", "status", "runtime_status")}
            reason = record.get("reason")
            row["reason"] = reason[:500] if isinstance(reason, str) else reason
            row["artifact"] = {key: artifact[key] for key in ("path", "sha256") if key in artifact} or None
            row["summary"] = copy.deepcopy(record.get("summary"))
            if isinstance(row["summary"], dict):
                unresolved = row["summary"].get("unresolved", [])
                row["summary"]["unresolved_count"] = len(unresolved)
                row["summary"]["unresolved"] = [str(item)[:300] for item in unresolved[:4]]
            readers.append(row)
        return readers

    def model_state(self, *, position_details=False):
        """Keep the complete audit on disk; send only actionable progress to the model."""
        state = self.state(position_details=position_details)
        usage = state["usage"]
        self.store.write_json("role_accounting.json", usage)
        audit_ref = self.store.put_json(usage).model_dump(mode="json")
        budget = RuntimeBudget.from_events(self.store.budget_limit, self.store.all_events)
        return {"readers": self.reader_progress(), "max_concurrent_readers": self.max_concurrent_readers,
                "usage": {key: usage.get(key) for key in ("requests", "provider_reported_tokens", "usage_complete")},
                "budget": {"available": budget.available.model_dump(mode="json"),
                           "fatal": budget.fatal_reason},
                "audit": {"file": "role_accounting.json", "blob": audit_ref,
                          "instruction": "Complete usage/ledger saved here. Use read_role_artifact(task_id) for one reader's details."},
                "assembly_review": state["assembly_review"], "position_review": state["position_review"]}

    async def position_evidence(self, decision_id):
        """Show the two original crops through the existing view tool in one call."""
        from .lineage import metadata
        row = self.positions.current()["items"].get(decision_id)
        if row is None:
            raise ValueError("position_decision_id must name a current position difference")
        requests = []
        for side in ("plan_evidence", "elevation_evidence"):
            evidence = row[side]
            name, box = evidence.get("image"), evidence.get("bbox")
            if name not in self.manifest["images"] or not box:
                raise ValueError("both readers need located original evidence before paired viewing")
            width, height = self.manifest["images"][name]["size"]
            crop = [max(0, math.floor(box[0])), max(0, math.floor(box[1])),
                    min(width, math.ceil(box[2])), min(height, math.ceil(box[3]))]
            requests.append((side, {"name": name, "box": crop, "display_scale": 3.0}))
        previews, images = [], []
        for side, args in requests:
            result = await self.frozen.call_tool("view_image", args)
            if result.get("isError"):
                return result
            preview = metadata(result)
            previews.append({**preview, "side": side})
            images.extend(block for block in result.get("content", []) if block.get("type") == "image")
        result = envelope({"decision_id": decision_id, "evidence_previews": previews,
                           "view_ids": [p["view_id"] for p in previews]})
        result["content"].extend(images)
        return result

    @staticmethod
    def _with_assembly_review(result, report):
        from scripts.tool_scripts.bim_agent_saved_result import result_metadata
        value = {**result_metadata(result), **(result.get("structuredContent") or {}), "assembly_review": report}
        return {**result, "structuredContent": value, "content": [
            {"type": "text", "text": json.dumps(value, ensure_ascii=False)},
            *[block for block in result.get("content", []) if block.get("type") != "text"]]}

    def _task(self, arguments):
        from .submission import canonical_target, parse_target
        arguments = normalize_stringified_parameters(arguments, TASK_SCHEMA)
        _validate_task_instructions([arguments])
        if "rework_targets" in arguments:
            if arguments.get("role_id") == "elevation_reader":
                from .elevation import normalize_elevation_rework_targets
                normalized_targets = normalize_elevation_rework_targets(arguments["rework_targets"])
            else:
                from .trial import normalize_rework_targets
                normalized_targets = normalize_rework_targets(arguments["rework_targets"])
            arguments = {**arguments,
                         "rework_targets": normalized_targets}
        jsonschema.validate(arguments, TASK_SCHEMA)
        arguments = {**arguments, "target": canonical_target(arguments["role_id"], arguments["target"])}
        valid_task_id(arguments["task_id"])
        image = arguments["image"]
        if Path(image).name != image or image not in self.manifest["images"]:
            raise ValueError("reader image must be one admitted original filename")
        path = self.run_directory / "images" / image
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != self.manifest["images"][image]["sha256"]:
            raise ValueError("reader original image changed since admission")
        task = {**arguments, "input_sha256": digest}
        if arguments.get("previous_task_id"):
            previous = self.registry.records[arguments["previous_task_id"]]
            if previous["image"] != image or previous["role_id"] != arguments["role_id"] or canonical_target(previous["role_id"], previous["target"]) != arguments["target"]:
                raise ValueError("rework must keep the same role, original image and target")
            if previous.get("input_sha256") != digest:
                raise ValueError("rework must keep the same admitted original image hash")
            if previous.get("artifact") and not arguments.get("rework_targets"):
                example = ("plan.openings:D1" if arguments["role_id"] == "plan_reader"
                           else "elevation.openings:W1.head_m")
                raise ValueError(
                    f"{arguments['role_id']} rework needs explicit rework_targets such as "
                    f"{example}; all other objects stay fixed"
                )
            task["previous_artifact"] = previous.get("artifact")
            task["previous_task_handoff"] = self._failure_handoff(arguments["previous_task_id"])
            recovered = task["previous_task_handoff"].get("editable_draft") or {}
            if recovered.get("status") == "passed" and not arguments.get("rework_targets"):
                raise ValueError("passed-draft continuation needs explicit rework_targets; unchanged retrial is allowed")
        parse_target(arguments["role_id"], arguments["target"])
        task["coordinate_contract"] = task_coordinates(arguments)
        if task["role_id"] == "elevation_reader":
            from .elevation import ELEVATION_COORDINATES
            task["coordinate_contract"]["units"] = ELEVATION_COORDINATES
        return task

    def _with_plan_floors(self, tasks, *, add_missing=False):
        """A facade target without floors names the plan floors (10-07 run4: a bare
        "South" reader invented floor "GF", which no plan floor matched)."""
        from .submission import canonical_target
        tasks = [dict(task) if isinstance(task, dict) else task for task in tasks]
        auto_added = []
        if add_missing and self.manifest.get("image_kind") == "drawings":
            explicit_origins = [task["origin"] for task in tasks if isinstance(task, dict)
                                and isinstance(task.get("origin"), str) and task["origin"]]
            default_origin = explicit_origins[0] if explicit_origins else _DEFAULT_READER_ORIGIN
            explicit_images = {(task.get("role_id"), task.get("image")) for task in tasks
                               if isinstance(task, dict)}
            explicit_targets = set()
            for task in tasks:
                if not isinstance(task, dict) or task.get("role_id") not in {
                        "plan_reader", "elevation_reader"} or not isinstance(task.get("target"), str):
                    continue
                target = canonical_target(task["role_id"], task["target"])
                explicit_targets.add((task["role_id"], target.split("/", 1)[0]))
            used_ids = {task.get("task_id") for task in tasks if isinstance(task, dict)}
            plan_images = [image for image in self.manifest.get("floor_plan_images", [])
                           if isinstance(image, str) and image in self.manifest.get("images", {})]
            for index, image in enumerate(plan_images):
                target = _default_floor_target(image, index)
                if (("plan_reader", image) in explicit_images
                        or ("plan_reader", target) in explicit_targets):
                    continue
                task = {"task_id": _auto_task_id("plan_reader", image, used_ids),
                        "role_id": "plan_reader", "image": image, "target": target,
                        "origin": default_origin}
                tasks.append(task)
                auto_added.append(task)
            for image in self.manifest.get("images", {}):
                facade = _CARDINAL_DRAWINGS.get(image.casefold()) if isinstance(image, str) else None
                if facade is None or (("elevation_reader", image) in explicit_images
                                      or ("elevation_reader", facade) in explicit_targets):
                    continue
                task = {"task_id": _auto_task_id("elevation_reader", image, used_ids),
                        "role_id": "elevation_reader", "image": image, "target": facade,
                        "origin": default_origin}
                tasks.append(task)
                auto_added.append(task)
        floors = {canonical_target("plan_reader", task["target"]) for task in tasks
                  if isinstance(task, dict) and task.get("role_id") == "plan_reader" and isinstance(task.get("target"), str)}
        floors |= {row["target"] for row in self.registry.records.values()
                   if row.get("role_id") == "plan_reader" and row.get("target")}
        if not floors:
            if add_missing:
                return tasks, [{key: task[key] for key in ("task_id", "role_id", "image", "target", "origin")}
                               for task in auto_added]
            return tasks
        result = []
        for task in tasks:
            if isinstance(task, dict) and task.get("role_id") == "elevation_reader" and isinstance(task.get("target"), str):
                target = canonical_target("elevation_reader", task["target"])
                if "/" not in target:
                    task = {**task, "target": target + "/" + ",".join(sorted(floors))}
            result.append(task)
        if add_missing:
            by_id = {task["task_id"]: task for task in result if isinstance(task, dict) and "task_id" in task}
            return result, [{key: by_id[task["task_id"]][key]
                             for key in ("task_id", "role_id", "image", "target", "origin")}
                            for task in auto_added]
        return result

    def _delegate_batch(self, tasks, call_id):
        # Bind the expanded batch to the durable parent call, before any child
        # starts. A restart must not infer "first dispatch" from partial child
        # records: those records are precisely what an interrupted batch leaves.
        requested = copy.deepcopy(tasks)
        identity = call_id if call_id is not None else "direct:" + hashlib.sha256(json_bytes(requested)).hexdigest()
        key = hashlib.sha256(json_bytes([self.store.run_id, self.store.task_id, identity])).hexdigest()
        path = self.store.directory / "role_batches" / (key + ".json")
        if path.is_file():
            record = json.loads(path.read_bytes())
            body = {key: value for key, value in record.items() if key != "record_blob"}
            if self.store.get_bytes(HashedBlobRef.model_validate(record["record_blob"])) != json_bytes(body):
                raise ValueError("reader batch metadata hash mismatch")
            if (body["run_id"], body["parent_task_id"], body["call_id"], body["requested_tasks"]) != (
                    self.store.run_id, self.store.task_id, identity, requested):
                raise ValueError("reader call identity already belongs to different tasks")
            for task in body["tasks"]:
                image = self.run_directory / "images" / task["image"]
                if hashlib.sha256(image.read_bytes()).hexdigest() != task["input_sha256"]:
                    raise ValueError("reader batch original image changed since admission")
            return body["tasks"], body["auto_added"]
        first_dispatch = not self.registry.records
        prepared = self._with_plan_floors(tasks, add_missing=first_dispatch)
        expanded, auto_added = prepared if first_dispatch else (prepared, [])
        identities = [task["task_id"] for task in expanded]
        if len(identities) != len(set(identities)):
            raise ValueError("reader task IDs must be unique within a batch")
        # Validate every dispatch before starting any model request.
        admitted = [self._task(task) for task in expanded]
        body = {"run_id": self.store.run_id, "parent_task_id": self.store.task_id,
                "call_id": identity, "requested_tasks": requested,
                "tasks": admitted, "auto_added": auto_added}
        record = {**body, "record_blob": self.store.put_json(body).model_dump(mode="json")}
        self.store.write_json("role_batches/" + path.name, record)
        return admitted, auto_added

    async def delegate_many(self, tasks, *, call_id=None):
        admitted, auto_added = self._delegate_batch(tasks, call_id)
        identities = [task["task_id"] for task in admitted]
        for task in admitted:
            self.registry.admit(task)

        async def one(task):
            async with self.slots:
                try:
                    return await self.run_reader(task)
                except (ValueError, KeyError) as error:
                    if self.registry.records.get(task["task_id"], {}).get("status") == "completed":
                        raise
                    return self.registry.save(task, status="failed", reason=str(error))
        ordered = sorted(admitted, key=lambda task: task["role_id"] != "plan_reader")
        results = await asyncio.gather(*(one(task) for task in ordered))
        results = [({**row, "failure_handoff": self._failure_handoff(row["task_id"])}
                    if row.get("status") == "failed" else row) for row in results]
        by_id = {row["task_id"]: row for row in results}
        return {"status": "completed", "results": [by_id[identity] for identity in identities],
                "auto_added": auto_added}

    async def run_reader(self, task):
        from .readers import ReaderTools
        from .guidance import get_role_guide
        from .trial import PlanTrialSession

        child = self.registry.admit(task)
        saved = self.registry.records.get(task["task_id"])
        if saved and saved["status"] == "completed":
            self.registry.read(task["task_id"])
            return {**saved, "reused_saved_result": True}
        recoverable_stop = saved and saved.get("reason") in {
            "cancelled", "token_usage_unavailable", "money_cny_usage_unavailable", "resume_pending_operation",
            "resume_uncheckpointed_budget_reservation"}
        if saved and saved["status"] not in {"running", "interrupted"} and not recoverable_stop:
            return {**saved, "reused_saved_result": True}
        deadline = self.started_epoch + self.limits.seconds
        budget = {**ROLE_TASK_BUDGET[task["role_id"]], **task.get("budget", {})}
        timing_path = child.task_directory / "reader_timing.json"
        if timing_path.is_file():
            timing = json.loads(timing_path.read_bytes())
        else:
            start = time.time()
            seconds = min(budget["seconds"], deadline - start)
            if seconds <= 0:
                return self.registry.save(task, status="failed", reason="root time budget exhausted")
            timing = {"started_epoch": start, "deadline_epoch": start + seconds, "time_budget_seconds": seconds}
            child.write_json("reader_timing.json", timing)
        allowance = {"model_calls": min(budget["model_calls"], self.limits.model_calls),
                     "tool_calls": min(budget["tool_calls"], self.limits.tool_calls),
                     "tokens": budget.get("tokens", self.limits.tokens),
                     "context_tokens": self.role_contexts[task["role_id"]].context_tokens,
                     "seconds": timing["time_budget_seconds"], "max_model_retries": self.limits.max_model_retries,
                     "retry_backoff_seconds": self.limits.retry_backoff_seconds,
                     "max_consecutive_truncations": self.limits.max_consecutive_truncations,
                     "max_total_truncations": self.limits.max_total_truncations,
                     "max_tool_recovery_retries": self.limits.max_tool_recovery_retries}
        if self.limits.tokens is not None and (allowance["tokens"] is None or allowance["tokens"] > self.limits.tokens):
            allowance["tokens"] = self.limits.tokens
        limits = RunLimits(**allowance)
        self.registry.save(task, status="running")
        config = self.routes[task["role_id"]]
        config = config.model_dump(mode="json") if hasattr(config, "model_dump") else config
        guide = get_role_guide(task["role_id"])
        raw_image = (self.run_directory / "images" / task["image"]).read_bytes()
        with Image.open(io.BytesIO(raw_image)) as image:
            size = image.size
        base_role = local_observer_role(limits.ledger_limit())
        async with AsyncExitStack() as stack:
            scoped = await stack.enter_async_context(self.frozen.task_session(
                directory=child.task_directory / "bim", root=self.root, role=base_role,
                timing=timing, images={task["image"]: (raw_image, size)}))
            trial = None
            if task["role_id"] == "plan_reader":
                trial = await stack.enter_async_context(PlanTrialSession(
                    scoped.run_directory, task["image"], root=self.root))
            previous = self.registry.read(task["previous_task_id"]) if task.get("previous_artifact") else None
            if trial is not None and task.get("previous_task_id"):
                prior_record = self.registry.records[task["previous_task_id"]]
                validation = prior_record.get("validation") or {}
                workspace = self.registry.child(task["previous_task_id"]).task_directory / "bim/trial_workspace"
                from .trial import PlanTrial, canonical_plan_sha256
                if geometry_ready(validation) and validation.get("validation_passed") is True:
                    prior = PlanTrial(None, image_name=task["image"], workspace=workspace,
                                      receipt_directory=workspace / "trial_receipts")
                    _, prior_receipt = prior.verified_plan(validation["plan_sha256"])
                    compiled_plan = prior.numeric_plan(prior_receipt)
                    if not isinstance(compiled_plan, dict):
                        raise ValueError("verified compiled rework plan is not a JSON object")
                    compiled_delivery = previous["plan"] == compiled_plan
                    if (not compiled_delivery
                            and canonical_plan_sha256(previous["plan"]) != validation["plan_sha256"]):
                        raise ValueError("rework artifact differs from its successful trial")
                    trial.inherit_reference(prior, validation["plan_sha256"], task["rework_targets"])
                    # Current deliveries contain the effective compiled plan
                    # and continue from it. Legacy deliveries contain the
                    # original declaration and retain their historical base.
                    if compiled_delivery:
                        trial.reference_plan = copy.deepcopy(compiled_plan)
                elif prior_record.get("status") == "failed":
                    editable = (task.get("previous_task_handoff") or {}).get("editable_draft")
                    if editable:
                        prior, recovered = self._verified_recovery_trial(
                            task["previous_task_id"], editable, task["input_sha256"])
                        if editable["status"] == "passed":
                            trial.inherit_reference(prior, editable["plan_sha256"], task["rework_targets"])
                            trial.reference_receipt.update({
                                "submission_accepted": False,
                                "topology_decisions_required": len(recovered.get("topology_issues", [])),
                                **{key: recovered[key] for key in
                                   ("compiled_numeric_plan_sha256", "candidate", "candidate_source_sha256", "reading_product_file", "reading_product_sha256") if key in recovered},
                            })
                        else:
                            trial.inherit_failed_reference(
                                prior, task["input_sha256"], receipt_file=editable["receipt_file"],
                                plan_sha256=editable["plan_sha256"],
                                compiled_numeric_plan_sha256=editable["compiled_numeric_plan_sha256"],
                                allowed_targets=task.get("rework_targets"),
                            )
                # The accepted review also covers warnings from trials made
                # after the selected successful geometry receipt.
                if validation:
                    trial.inherited_topology_issues = validation.get("topology_issues", [])
            from .elevation import ElevationReaderTools, ELEVATION_COORDINATES
            tool_class = ElevationReaderTools if task["role_id"] == "elevation_reader" else ReaderTools
            tool_kwargs = {"role_id": task["role_id"], "image_name": task["image"],
                           "trial": trial, "target": task["target"], "store": child}
            if task["role_id"] == "elevation_reader":
                tool_kwargs.update(previous_artifact=previous,
                                   rework_targets=task.get("rework_targets"))
            tools = tool_class(scoped, **tool_kwargs)
            catalog = await tools.list_tools()
            role = base_role.model_copy(update={"role_id": task["role_id"],
                "read_only": False, "tool_whitelist": tuple(
                ToolGrant(tool_name=tool["name"], access="read" if tools.repeatability(tool["name"]) == "read_only" else "write")
                for tool in catalog)})
            parameters = role_parameters(config)
            effective_context = self.role_contexts[task["role_id"]]
            route = {**self.connection_routes[task["role_id"]], "model": config["model"],
                     "context": effective_context.model_dump(mode="json")}
            specs = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                      "parameters": t["inputSchema"]}} for t in catalog]
            versions = make_versions(child, root=self.root, prompt=guide, tools=specs, parameters=parameters,
                                     route=route, code_paths=("src/agent/runtime_roles", "src/agent/runtime_tools.py"))
            from .reader_context import previous_artifact_context
            previous_context = previous_artifact_context(
                previous, role_id=task["role_id"], artifact_reference=task.get("previous_artifact")
            )
            from .plan_views import initial_coordinate_view
            initial_image, initial_view = initial_coordinate_view(raw_image, image_name=task["image"], directory=scoped.run_directory)
            tools._remember_references({"structuredContent": {"evidence_previews": [initial_view]}})
            content = [{"type": "text", "text": json.dumps({"task": task, "previous_artifact": previous_context,
                       "input": {"image": task["image"], "original_size": list(size), "sha256": task["input_sha256"]},
                       "initial_view": initial_view,
                       "coordinates": ELEVATION_COORDINATES if task["role_id"] == "elevation_reader"
                       else READER_COORDINATES[task["role_id"]]}, ensure_ascii=False)},
                       {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(initial_image).decode(),
                                                             "detail": "original"}}]
            adapter = self.adapter_factory(task["task_id"], config, child)
            if hasattr(adapter, "close"):
                stack.push_async_callback(adapter.close)
            from .reader_delivery import completed_reader_handoff
            engine = Runtime(store=child, adapter=adapter, tools=tools, role=role,
                model=config["model"], parameters=parameters, versions=versions, limits=limits,
                strict_model_profile=config["provider"] in LIVE_PROVIDERS,
                root_tool_calls=self.limits.tool_calls, start_epoch=timing["started_epoch"],
                domain_completion=completed_reader_handoff,
                low_output_limit_reason=config.get("low_output_limit_reason"),
                context_policy=context_policy_from_effective(task["role_id"], effective_context),
                request_timeout_seconds=self.limits.seconds / (self.max_concurrent_readers + 1),
                fault_hook=self.reader_fault_hook)
            original_ref = child.put_bytes(raw_image, "image/png")
            runtime = await engine.run([{"role": "system", "content": guide}, {"role": "user", "content": content}],
                image_originals={hashlib.sha256(initial_image).hexdigest(): original_ref}, resume=bool(child.events))
            if task["role_id"] == "plan_reader":
                runtime["plan_reader_progress"] = tools.progress(started_epoch=runtime.get("started_epoch"))
                child.write_json("receipt.json", runtime)
            submitted = tools.submission.read()
            completion = runtime.get("completion") or {}
            accepted = (runtime["status"] == "completed" and submitted is not None
                        and completion.get("artifact_sha256") == submitted["artifact_sha256"]
                        and completion.get("receipt_sha256") == tools.submission.snapshot()["receipt_sha256"])
            # A disk write without a committed tool execution is diagnostic
            # material, not an accepted reading. Recovery must settle it first.
            artifact = submitted["artifact"] if accepted else None
            status = "completed" if accepted else "failed"
            return self.registry.save(task, status=status, artifact=artifact, runtime=runtime,
                                      validation=submitted["validation"] if accepted else None,
                                      reason=runtime["status"] if artifact is None else None)

    async def _once(self, operation_id, name, arguments, *, reference):
        digest = hashlib.sha256(operation_id.encode()).hexdigest()
        path = self.store.directory / "role_operations" / (digest + ".json")
        if path.is_file():
            saved = json.loads(path.read_bytes())
            if (saved["tool"], saved["arguments"], saved["reference"]) != (name, arguments, reference):
                raise ValueError("operation identity already refers to different arguments")
            if "result" not in saved:
                raise ValueError("operation outcome unknown; cannot repeat a possible model write")
            return saved["result"]
        saved = {"operation_id": operation_id, "tool": name, "arguments": arguments, "reference": reference}
        self.store.write_json("role_operations/" + path.name, saved)
        result = await self.frozen.call_tool(name, arguments)
        # Preserve the actual expanded tool arguments as an audited blob without retransmitting a full plan.
        result = dict(result)
        from scripts.tool_scripts.bim_agent_saved_result import full_result_metadata
        metadata = {**full_result_metadata(result, self.run_directory),
                    "role_application": {"reference": reference, "expanded_parameters": self.store.put_json(saved).model_dump(mode="json")}}
        result["structuredContent"] = metadata
        result["content"] = [block for block in result.get("content", []) if block.get("type") != "text"]
        result["content"].insert(0, {"type": "text", "text": json.dumps(metadata, ensure_ascii=False)})
        self.store.write_json("role_operations/" + path.name, {**saved, "result": result})
        return result

    @staticmethod
    def _build_identity(task_id, levels):
        suffix = ":" + hashlib.sha256(json_bytes(levels)).hexdigest() if levels else ""
        return "plan:" + task_id + suffix

    async def build_from_artifact(self, task_id, sha256, **levels):
        async with self.write_lock:
            self.assembly.guard()
            return await self._build_from_artifact(task_id, sha256, **levels)

    async def _build_from_artifact(self, task_id, sha256, resolved_levels=None, elevation_readings=None, **levels):
        value = self.registry.read(task_id, sha256=sha256, role_id="plan_reader")
        record = self.registry.records[task_id]
        if not record.get("validation", {}).get("validation_passed"):
            raise ValueError("plan delivery has no successful trial receipt")
        plan = value["plan"]
        receipt = record["validation"]
        if receipt.get("compiled_numeric_plan_file"):
            from .trial import canonical_plan_sha256
            workspace = self.registry.child(task_id).task_directory / "bim/trial_workspace"
            path = (workspace / receipt["compiled_numeric_plan_file"]).resolve()
            if not path.is_relative_to(workspace.resolve()) or not path.is_file():
                raise ValueError("compiled reader plan is outside its trial or missing")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != receipt["compiled_numeric_plan_sha256"]:
                raise ValueError("compiled reader plan hash mismatch")
            compiled = json.loads(raw)
            if not isinstance(compiled, dict):
                raise ValueError("compiled reader plan is not a JSON object")
            if plan != compiled and canonical_plan_sha256(plan) != receipt["plan_sha256"]:
                raise ValueError("trial receipt does not refer to this original or verified compiled plan")
            plan = compiled
        from .levels import apply_levels, apply_resolved_levels
        if resolved_levels is None:
            plan, citations = apply_levels(self.registry, plan, **levels)
        else:
            if levels:
                raise ValueError("choose resolved levels or explicit legacy overrides")
            plan, citations = apply_resolved_levels(plan, resolved_levels), resolved_levels
            levels = {"resolved_levels": resolved_levels}
        materialization = []
        if plan.get("reading_type") == "plan":
            from .plan_materialization import materialize_plan_reading, PlanMaterializationError
            image_path = self.run_directory / "images" / record["image"]
            with Image.open(image_path) as image:
                image_size = image.size
            try:
                plan, materialization = materialize_plan_reading(plan, elevations=elevation_readings or {},
                    image_size=image_size, image_name=record["image"])
            except PlanMaterializationError as error:
                return envelope({"status": "assembly_conflict", "source_geometry_ready": False,
                    "reason": str(error), "materialization": error.decisions}, error=True)
            # Include accepted elevation identity in _once so new evidence cannot
            # accidentally reuse a previous floor build at the same levels.
            levels = {**levels, "materialization": materialization}
        reference = {"task_id": task_id, "sha256": sha256}
        if citations:
            reference["levels"] = citations
        result = await self._once(self._build_identity(task_id, levels), "build_plan_bim",
            {"image": record["image"], "plan_json": json.dumps(plan, ensure_ascii=False)},
            reference=reference)
        if materialization:
            result = copy.deepcopy(result)
            result.setdefault("structuredContent", {})["materialization"] = materialization
        meta = result.get("structuredContent") or {}
        if meta.get("source_geometry_ready") and meta.get("candidate"):
            self.assembly.bind(task_id)
            result = self._with_assembly_review(result, self.assembly.check(meta["candidate"]))
        return result

    def _source(self, candidate):
        if not isinstance(candidate, str) or Path(candidate).name != candidate or not candidate.startswith("candidate_"):
            raise ValueError("candidate must be a saved candidate identifier")
        path = (self.run_directory / candidate / "source_model.json").resolve()
        if not path.is_relative_to(self.run_directory.resolve()):
            raise ValueError("candidate escapes the BIM run")
        if not path.is_file():
            raise ValueError(f"saved candidate does not exist: {candidate}")
        return json.loads(path.read_bytes())

    def match(self, task_id, candidate, *, height_bounds=False):
        from .elevation import match_elevation
        from .lineage import guard_replaced_plans
        artifact = self.registry.read(task_id, role_id="elevation_reader")
        source = self._source(candidate)
        guard_replaced_plans(self, candidate)
        result = match_elevation(source, artifact, candidate=candidate)
        self.positions.record(task_id, candidate, result)
        self.positions.resolve_match(task_id, result)
        if height_bounds:
            # Horizontal matching alone does not prove a proposed Z fits the host.
            hosts = {row["id"]: row for row in source["boundaries"]}
            openings = {row["id"]: row for row in source["openings"]}
            safe = []
            for row in result["matches"]:
                host = hosts[openings[row["source_opening_id"]]["host_boundary_id"]]
                low, high = min(p[2] for p in host["vertices"]), max(p[2] for p in host["vertices"])
                if row["sill_m"] < low - 1e-8 or row["head_m"] > high + 1e-8:
                    result["conflicts"].append({**row, "status": "conflict", "type": "height_outside_host",
                                              "host_z_m": [low, high]})
                else:
                    safe.append(row)
            result["matches"], result["can_apply"] = safe, bool(safe)
        result.pop("match_id")  # Only the outer durable identifier is public.
        result["source_file_sha256"] = hashlib.sha256(
            (self.run_directory / candidate / "source_model.json").read_bytes()).hexdigest()
        match_id = hashlib.sha256(json_bytes({"task_id": task_id, "candidate": candidate, "result": result})).hexdigest()
        value = {"match_id": match_id, "task_id": task_id, "candidate": candidate, "result": result}
        self.store.write_json("role_matches/" + match_id + ".json", value)
        return value

    async def apply_heights(self, match_id):
        from .height_writes import apply_heights
        async with self.write_lock:
            self.assembly.guard()
            return await apply_heights(self, match_id)

    async def recover_pending(self):
        """Complete only our recoverable durable operations after a process death.

        Ordinary unknown BIM writes retain the runtime's conservative refusal.
        Reader dispatch is reconstructed from child checkpoints and immutable
        deliveries. A write can be acknowledged only from its saved receipt.
        """
        from src.agent_runtime.adapter import convert_tool_result
        completed = {event.payload.invocation_event_id for event in self.store.events
                     if event.payload.event_type == "tool_execution" and event.payload.outcome != "unknown"}
        resolved_calls = {event.payload.call_id for event in self.store.events
                          if event.payload.event_type == "tool_execution" and event.payload.outcome != "unknown"}
        latest_invocations = {event.payload.call_id: event.event_id for event in self.store.events
                              if event.payload.event_type == "tool_invocation"}
        recovered = []
        for event in list(self.store.events):
            call = event.payload
            if (call.event_type != "tool_invocation" or event.event_id in completed
                    or call.call_id in resolved_calls
                    or latest_invocations[call.call_id] != event.event_id):
                continue
            unknown = next((row for row in reversed(self.store.events)
                if row.payload.event_type == "tool_execution" and
                row.payload.invocation_event_id == event.event_id and row.payload.outcome == "unknown"), None)
            retry_source = next((source.blob for source in event.source_refs
                if source.source_id == "tool-recovery-attempt"), None)
            retry_id = json.loads(self.store.get_bytes(retry_source))["retry_event_id"] if retry_source else None
            if unknown:
                # A permission-level read label is not replay authorization.
                # Only this task-aware delegate path can recover paid children.
                if call.tool_name != "delegate_readers":
                    continue
                attempts = sum(row.payload.event_type == "tool_invocation" and
                    row.payload.call_id == call.call_id for row in self.store.events)
                if attempts > self.limits.max_tool_recovery_retries:
                    continue
                if sum(row.payload.event_type == "tool_invocation" for row in self.store.all_events) >= self.limits.tool_calls:
                    continue
                retry = self.store.append(RunLifecyclePayload(action="retry", attempt=attempts + 1,
                    retry_of_event_id=unknown.event_id, reason="recover existing reader task identities and checkpoints"))
                retry_id = retry.event_id
                event = self.store.append(ToolInvocationPayload(call_id=call.call_id,
                    tool_name=call.tool_name, full_arguments=call.full_arguments,
                    repeatability=call.repeatability, operation_key=call.operation_key,
                    state_before=self.store.put_json(self.snapshot_state())),
                    source_refs=(self.store.source("tool-recovery-attempt", {"retry_event_id": retry_id}),))
            if call.tool_name == "delegate_readers":
                raw = await self.call_tool(call.tool_name, call.full_arguments)
            elif call.tool_name in {"assemble_from_readers", "edit_bim"}:
                raw = await self.call_tool(call.tool_name, call.full_arguments)
                if raw.get("isError"):
                    # Unacknowledged inner writes keep their original refusal.
                    continue
            elif call.tool_name in {"build_from_artifact", "apply_elevation_heights"}:
                if call.tool_name == "build_from_artifact":
                    levels = {k: v for k, v in call.full_arguments.items() if k not in {"task_id", "sha256"}}
                    identity = self._build_identity(call.full_arguments["task_id"], levels)
                    path = self.store.directory / "role_operations" / (hashlib.sha256(identity.encode()).hexdigest() + ".json")
                    if not path.is_file() or "result" not in json.loads(path.read_bytes()):
                        continue
                    saved_result = json.loads(path.read_bytes())["result"]
                else:
                    from .height_writes import match_ids, load_match, saved_application
                    ids = sorted({load_match(self, identity)["match_id"] for identity in match_ids(call.full_arguments["match_id"])})
                    saved = saved_application(self, ids)
                    if saved is None:
                        continue
                    saved_result = saved["result"]
                # A process may die after the frozen build receipt was saved but
                # before its reader binding/comparison. Complete that read-only
                # bookkeeping from the receipt; _once will never rebuild it.
                raw = (await self._build_from_artifact(**call.full_arguments)
                       if call.tool_name == "build_from_artifact"
                       else saved_result)
            else:
                continue
            _, _, shown, _ = convert_tool_result(call.call_id, raw, self.store)
            self.store.append(ToolExecutionPayload(call_id=call.call_id, tool_name=call.tool_name,
                full_arguments=call.full_arguments, repeatability=call.repeatability,
                operation_key=call.operation_key, invocation_event_id=event.event_id,
                retry_event_id=retry_id,
                outcome="failed" if raw.get("isError") else "succeeded",
                applied_write_id=call.operation_key if call.repeatability != "read_only" and not raw.get("isError") else None,
                raw_result=self.store.capture(raw, force_blob=True), shown_result=self.store.capture(shown, force_blob=True),
                presentation_status="prepared"),
                source_refs=(self.store.source("tool-state-after", self.snapshot_state()),
                             self.store.source("role-operation-recovery", {"original_invocation": event.event_id,
                                 "method": "resume child checkpoints" if call.tool_name == "delegate_readers" else "read durable operation receipt"})))
            recovered.append(event.event_id)
            resolved_calls.add(call.call_id)
        return recovered


def update_role_context(engine, event, raw):
    update_building_context(engine, event, raw)
    value = engine.tools.reader_progress()
    previous = next((row for row in engine.context.state if row.key == "reader-artifacts"), None)
    engine.context.set_state(StateEntry(key="reader-artifacts", category="artifact_version", value=value,
        epistemic_status="computed", revision=1 if previous is None else previous.revision + 1,
        source_refs=(engine.store.source("reader-artifact-registry", value),)))
    positions = engine.tools.positions.summary()
    previous = next((row for row in engine.context.state if row.key == "position-decisions"), None)
    engine.context.set_state(StateEntry(key="position-decisions", category="artifact_version", value=positions,
        epistemic_status="computed", revision=1 if previous is None else previous.revision + 1,
        source_refs=(engine.store.source("position-review", positions),)))
