"""Optional mesh advice tasks over the shared, domain-neutral Runtime.

The native coordinator retains all its own tools. Dispatch creates only the
requested read-only tasks; no facades, roles, or task sequence are inferred.
"""

from __future__ import annotations

import asyncio
import base64
import copy
from contextlib import AsyncExitStack
import hashlib
import inspect
import json
from pathlib import Path
import time

from jsonschema import Draft202012Validator

from src.agent.runtime_delegation import update_observer_budget
from src.agent.runtime_tools import FrozenBimTools, frozen_bim_client, local_observer_role
from src.agent_runtime.adapter import convert_tool_result
from src.agent_runtime.budget import RuntimeBudget
from src.agent_runtime.context import ContextPolicy, context_image_tool_spec
from src.agent_runtime.loop import Runtime
from src.agent_runtime.providers import LIVE_PROVIDERS
from src.agent_runtime.versions import make_versions
from src.harness_contracts import RunLifecyclePayload, ToolExecutionPayload, ToolGrant, ToolInvocationPayload


WORKER_GUIDANCE = """You are a read-only mesh worker for the BIM coordinator.
Answer the requested question using the frozen native mesh, admitted auxiliary
images and user declaration. Choose the observations and tools needed yourself.
Treat coordinator notes and user statements as assertions, not measured facts.
Keep direct observations, interpretations and uncertainty separate. Texture
appearance is not geometric proof. A source overlay checks a saved candidate
snapshot and is not independent proof of mesh agreement. Use the native mesh
frame; do not invent geographic directions. Judge auxiliary information by its
local precision; do not automatically measure hand sketches as scaled plans.
Do not edit or select the parent BIM, delegate further work, or claim delivery.
Return one JSON object matching answer_schema, without markdown. Evidence IDs
must come from supplied or actually returned views. For a computed mesh
measurement use measurement_call_id equal to your measure_mesh_pixels or
compare_mesh_source_pixels call ID for mesh_observation evidence, or your
view_mesh_section call ID for mesh_section evidence (section_NNN). Section cuts
are original surface intersections, not room plans. Comparison bands are sampled
same-ray depth residuals, not texture-window recognition or fidelity approval.
Never present freely written coordinates as tool measurements.
Before directly describing auxiliary PNG evidence, call view_image (optionally
with box). Admission alone does not establish that you have viewed an image.
You may return only uncertainty when the question cannot be established.
Respect task_limits and remaining root allowance; your task creates no budget.
Reserve a model response for the structured answer. Once the bounded question has
enough evidence, return the useful conclusion with remaining uncertainty; do not
spend the final request on another optional crop, measurement or fine-detail check.
Answer the assigned discrepancy at the coordinator's architectural scale rather
than expanding the assignment into a full building audit.
Copy worker_answer_reference fields from tool replies. For auxiliary images,
evidence_id is the admitted PNG filename, never a view_XXXX crop ID. Only use
measurement_call_id when that exact field was returned for a successful query.
Near the request limit the runtime removes tools to retain a final answer and,
when allowance remains, one correction. Answer with uncertainty if necessary.
"""

_TOKEN_BUDGET_MEANING = (
    "Total worker token budget across all requests, including input and output; "
    "each request reserves estimated input/image/reasoning tokens plus the inherited "
    "configured output allowance. This is not a per-response output cap. "
    "Null adds no child token limit beyond the shared root budget.")

_BUDGET_SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {"model_calls": {"type": "integer", "minimum": 1,
        "description": "Total requests including answer/repair. With 3+ calls, the final 2 are answer-only; "
                       "with 2 calls, the last is answer-only; 1 call can only answer supplied evidence or uncertainty. "
                       "These slots do not increase the shared root budget."},
        "tool_calls": {"type": "integer", "minimum": 0},
        "seconds": {"type": ["number", "null"], "exclusiveMinimum": 0},
        "tokens": {"type": ["integer", "null"], "minimum": 1,
            "description": _TOKEN_BUDGET_MEANING + " An explicit value must exceed the configured "
                "output allowance shown by mesh_worker_state and cover the actual request estimates."}},
    "required": ["model_calls", "tool_calls"]}
_TASK_SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {"task_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"},
        "question": {"type": "string", "minLength": 1},
        "notes": {"type": "array", "items": {"type": "string"}},
        "candidate": {"type": ["string", "null"]},
        "observation_ids": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
        "budget": _BUDGET_SCHEMA}, "required": ["task_id", "question", "budget"]}

EXTRA_TOOLS = [
    {"name": "delegate_mesh_workers", "description": "Ask any needed read-only mesh questions concurrently and wait for this batch. All workers use the coordinator's configured model and shared root budget. Only supplied tasks run; using workers is optional. Each task needs a unique stable ID and explicit model/tool allowance. Optional candidate is a frozen read-only source snapshot; observation_ids reuse parent mesh views. All admitted auxiliary inputs and declarations remain available. Reusing identical completed IDs reads their saved results; changed arguments/configuration/evidence are rejected. Null seconds/tokens add no child limit beyond root constraints. budget.tokens is a total input-plus-output allowance across requests, not an output cap. Inspect mesh_worker_state for the inherited output allowance; infeasible new tasks are rejected before input freezing and produce no observation evidence. Workers cannot modify or finish the parent BIM.",
     "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
         "tasks": {"type": "array", "minItems": 1, "items": _TASK_SCHEMA}}, "required": ["tasks"]}},
    {"name": "read_mesh_worker", "description": "Read a saved worker result and its frozen input/source basis. Default results contain compact measurement summaries. Optional measurement_call_ids return up to 3 selected full verified measurements, including from incomplete workers; IDs come from computed_measurement.tool_call_id or evidence_catalog.measurements. Optional view_ids return actual verified PNGs cited or observed by that worker (mesh_NNN, section_NNN, overlay_NNN or admitted image names). The coordinator judges whether snapshot advice applies to its current candidate; nothing is applied automatically.",
     "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
         "task_id": {"type": "string", "minLength": 1},
         "view_ids": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
         "measurement_call_ids": {"type": "array", "maxItems": 3,
             "items": {"type": "string", "minLength": 1}, "uniqueItems": True}}, "required": ["task_id"]}},
    {"name": "mesh_worker_state", "description": "Inspect mesh worker status, configured model, concurrency, inherited output allowance, minimum necessary task token budget and shared remaining allowance. The token minimum excludes actual input estimates and is not a guarantee that a request can start. Counts distinguish parent and child tasks; saved advice is not a BIM quality verdict.",
     "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}}},
]
MESH_WORKER_TOOL_NAMES = tuple(tool["name"] for tool in EXTRA_TOOLS)


def mesh_worker_tool_specs():
    return json.loads(json.dumps(EXTRA_TOOLS))


def mesh_worker_task_payload(packet, limits):
    from src.agent.runtime_mesh_evidence import MeshWorkerAnswer
    return {"packet": packet, "task_limits": limits.model_dump(mode="json"),
            "answer_reserve_calls": min(2, max(1, limits.model_calls - 1)),
            "answer_schema": MeshWorkerAnswer.model_json_schema()}


def _envelope(value, *, error=False, blocks=()):
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}, *blocks],
            "structuredContent": value, "isError": error}


def _measurement_summary(result):
    """Presentation only; validity, numeric conclusions and caveats stay visible."""
    summary = copy.deepcopy(result)
    omitted = []
    for key in ("pixel_buffer_npz", "samples", "segments", "coplanar_triangles", "point_touches"):
        if key in summary:
            value = summary.pop(key)
            omitted.append({"field": key, **({"count": len(value)} if isinstance(value, list) else {})})
    for query in summary.get("queries", ()):
        triangle = query.pop("triangle_surface_evidence", None)
        if triangle is not None:
            query["surface_summary"] = {key: triangle[key] for key in (
                "global_face_id", "triangle_normal_xyz", "plane_tilt_from_vertical_degrees",
                "horizontal_surface_trace_direction_degrees", "material_alpha_mode",
                "material_geometry_caveat") if key in triangle}
    if any("triangle_surface_evidence" in row for row in result.get("queries", ())):
        omitted.append({"field": "queries[].triangle_surface_evidence"})
    if omitted:
        summary["omitted_detail_fields"] = omitted
    return summary


def _project_worker_outcome(value):
    """Keep claim bindings; show each compact measurement only once per outcome."""
    value = copy.deepcopy(value)
    result = value.get("result")
    if isinstance(result, dict):
        measurements = {}
        for observation in result.get("directly_seen", ()):
            computed = observation.get("computed_measurement")
            if not isinstance(computed, dict) or "result" not in computed:
                continue
            detail = computed.pop("result")
            call_id = computed["tool_call_id"]
            measurements.setdefault(call_id, {**computed, "result": _measurement_summary(detail),
                "full_result_read": {"tool": "read_mesh_worker", "arguments": {
                    "task_id": value["task_id"], "measurement_call_ids": [call_id]}}})
        if measurements:
            result["measurement_summaries"] = list(measurements.values())
    value["presentation"] = {"measurement_detail": "compact; full canonical result remains journaled",
        "meaning": "Claims retain their measurement call/event/hash bindings. Summaries do not add fidelity approval.",
        "full_measurement_read": {"tool": "read_mesh_worker", "task_id": value.get("task_id"),
            "selector": "measurement_call_ids", "maximum_ids": 3}}
    return value


class _EvidenceTools:
    """Bind verified observation bytes before the ordinary runtime journals them."""

    def __init__(self, frozen, packet, store):
        self.frozen, self.packet, self.store = frozen, packet, store
        self.run_directory = frozen.run_directory

    async def list_tools(self):
        return await self.frozen.list_tools()

    def repeatability(self, name):
        return self.frozen.repeatability(name)

    def recovery_policy(self, name):
        return self.frozen.recovery_policy(name)

    async def call_tool(self, name, arguments):
        from src.agent.runtime_mesh_evidence import bind_mesh_worker_tool_result
        raw = await self.frozen.call_tool(name, arguments)
        invocation = next((event for event in reversed(self.store.events)
            if event.payload.event_type == "tool_invocation"
            and event.payload.tool_name == name and event.payload.full_arguments == arguments), None)
        return bind_mesh_worker_tool_result(self.packet, self.run_directory, name, arguments, raw,
            call_id=invocation.payload.call_id if invocation else None)

    def snapshot_state(self):
        return self.frozen.snapshot_state()

    def image_origins(self, raw):
        return self.frozen.image_origins(raw)

    def artifacts(self):
        return self.frozen.artifacts()


class MeshWorkerTools:
    """Native tool adapter; the enclosing Runtime owns parent call journaling."""

    def __init__(self, frozen, *, store, root, adapter_factory, model, parameters,
                 route, limits, start_epoch, max_concurrent=8,
                 reasoning_history="all", low_output_limit_reason=None, pricing=None):
        if type(max_concurrent) is not int or max_concurrent < 1:
            raise ValueError("max_concurrent must be a positive integer")
        self.frozen, self.store, self.root = frozen, store, Path(root).resolve()
        self.run_directory = frozen.run_directory
        self.adapter_factory, self.model = adapter_factory, model
        self.parameters = json.loads(json.dumps(parameters))
        self.route = json.loads(json.dumps(route))
        self.limits, self.start_epoch = limits, start_epoch
        self.max_concurrent, self.reasoning_history = max_concurrent, reasoning_history
        self.low_output_limit_reason = low_output_limit_reason
        self.pricing = pricing
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._locks = {}
        self._schemas = {tool["name"]: Draft202012Validator(tool["inputSchema"]) for tool in EXTRA_TOOLS}

    async def list_tools(self):
        return [*await self.frozen.list_tools(), *mesh_worker_tool_specs()]

    def repeatability(self, name):
        return "read_only" if name in self._schemas else self.frozen.repeatability(name)

    def recovery_policy(self, name):
        if name == "delegate_mesh_workers":
            return "resume_task"
        if name in self._schemas:
            return "retry_read"
        return self.frozen.recovery_policy(name)

    async def call_tool(self, name, arguments):
        if name not in self._schemas:
            return await self.frozen.call_tool(name, arguments)
        try:
            errors = list(self._schemas[name].iter_errors(arguments))
            if errors:
                raise ValueError(errors[0].message)
            if name == "delegate_mesh_workers":
                tasks = arguments["tasks"]
                ids = [task["task_id"] for task in tasks]
                if len(ids) != len(set(ids)) or self.store.task_id in ids or self.store.root_task_id in ids:
                    raise ValueError("worker task IDs must be unique and different from the coordinator")
                results = await asyncio.gather(*(self._dispatch(task) for task in tasks))
                return _envelope({"status": "completed", "results": results,
                                  "meaning": "batch ended; individual task statuses and evidence determine usefulness"})
            if name == "read_mesh_worker":
                value, blocks = self._read(arguments["task_id"], arguments.get("view_ids", ()),
                                           arguments.get("measurement_call_ids", ()))
                return _envelope(value, blocks=blocks)
            return _envelope(self.state())
        except (ValueError, FileNotFoundError) as exc:
            return _envelope({"status": "rejected", "reason": str(exc)}, error=True)

    def project_tool_result(self, name, raw):
        """Project model-visible copies only; canonical hydration/storage is unchanged."""
        if name not in {"delegate_mesh_workers", "read_mesh_worker"} or raw.get("isError"):
            return raw
        value = raw["structuredContent"]
        if name == "delegate_mesh_workers":
            value = {**value, "results": [_project_worker_outcome(row) for row in value["results"]]}
        else:
            value = _project_worker_outcome(value)
        # The envelope's first text block mirrors structuredContent. Preserve
        # subsequent PNG/reference blocks exactly, including their image origins.
        return {**raw, "structuredContent": value, "content": [
            {"type": "text", "text": json.dumps(value, ensure_ascii=False)}, *raw["content"][1:]]}

    def _configuration(self):
        return {"model": self.model, "parameters": self.parameters, "route": self.route,
            "limits": self.limits.model_dump(mode="json"), "start_epoch": self.start_epoch,
            "reasoning_history": self.reasoning_history, "max_concurrent": self.max_concurrent,
            "low_output_limit_reason": self.low_output_limit_reason,
            "pricing": self.pricing.model_dump(mode="json") if self.pricing is not None else None}

    def _token_budget_contract(self):
        # Match Runtime's configured output allowance without changing the
        # immutable configuration or lowering the requested model parameters.
        output = self.parameters.get("max_tokens", self.parameters.get("max_completion_tokens"))
        minimum = output + 1 if type(output) is int and output > 0 else None
        return {"output_token_limit": output, "minimum_task_tokens": minimum,
            "minimum_task_tokens_basis": "Necessary lower bound only: configured output allowance plus "
                "at least one input token. Actual input/image/reasoning estimates and later requests "
                "can require more; the shared runtime still checks and reserves the full request.",
            "tokens_meaning": _TOKEN_BUDGET_MEANING}

    def _new_task_budget_rejection(self, task):
        contract = self._token_budget_contract()
        tokens = task["budget"].get("tokens")
        minimum = contract["minimum_task_tokens"]
        if tokens is None or minimum is None or tokens >= minimum:
            return None
        return {"task_id": task["task_id"], "status": "rejected",
            "reason_code": "worker_token_budget_infeasible", "stage": "budget_preflight",
            "reason": f"budget.tokens={tokens} cannot cover inherited output_token_limit="
                f"{contract['output_token_limit']} plus request input; the total must be strictly greater "
                "than the output allowance. Use tokens=null to inherit root constraints or explicitly "
                "budget for the full request estimates and planned calls. No allowance was changed.",
            "budget": {"configured_task_tokens": tokens, **contract},
            "admitted": False, "result": None, "model_calls": 0, "tool_calls": 0,
            "meaning": "No worker started or observations were made; this rejection is not evidence."}

    def _child(self, task_id):
        if task_id in {self.store.task_id, self.store.root_task_id}:
            raise ValueError("worker ID cannot name the coordinator")
        return self.store.for_task(task_id, parent_task_id=self.store.task_id)

    @staticmethod
    def _anchored(child, source_id):
        found = []
        for event in (child.all_events if source_id == "mesh-worker-admission" else child.events):
            for source in event.source_refs:
                if source.source_id != source_id or source.blob is None:
                    continue
                if source_id == "mesh-worker-admission" and (event.task_id != child.parent_task_id or
                        json.loads(child.get_bytes(source.blob))["task"]["task_id"] != child.task_id):
                    continue
                found.append(source.blob)
        if not found:
            return None
        if any(ref != found[0] for ref in found):
            raise ValueError("worker immutable record has conflicting journal bindings")
        return json.loads(child.get_bytes(found[0]))

    def _record(self, child):
        record = self._anchored(child, "mesh-worker-admission")
        if record is None:
            raise ValueError("unknown mesh worker task ID")
        if record["configuration"] != self._configuration():
            raise ValueError("worker configuration changed; use the original configuration or a new run")
        from src.agent.runtime_mesh_evidence import verify_mesh_worker_packet
        verify_mesh_worker_packet(record["packet"], self.run_directory, child.task_directory / "bim")
        return record

    async def _dispatch(self, task):
        task_id = task["task_id"]
        async with self._locks.setdefault(task_id, asyncio.Lock()):
            async with self._semaphore:
                try:
                    return await self._run_task(task)
                except (ValueError, FileNotFoundError) as exc:
                    return {"task_id": task_id, "status": "rejected", "reason": str(exc)}
                except Exception as exc:
                    # The existing runtime records request failures and unknown
                    # usage; never invent a refund or expose adapter secrets.
                    return {"task_id": task_id, "status": "child_failed", "error_type": type(exc).__name__,
                            "recovery": "reuse the exact task ID to inspect or resume its journal"}

    async def _run_task(self, task):
        from src.agent.runtime_mesh_evidence import prepare_mesh_worker_packet
        child = self._child(task["task_id"])
        record = self._anchored(child, "mesh-worker-admission")
        if record is not None:
            if record["task"] != task:
                raise ValueError("task ID already has different exact arguments")
            record = self._record(child)
        else:
            if child.events:
                raise ValueError("task ID belongs to a different existing task")
            rejected = self._new_task_budget_rejection(task)
            if rejected is not None:
                return rejected
            now = time.time()
            seconds = task["budget"].get("seconds")
            deadline = now + seconds if seconds is not None else None
            if self.limits.seconds is not None:
                parent_deadline = self.start_epoch + self.limits.seconds
                deadline = min(deadline, parent_deadline) if deadline is not None else parent_deadline
            if deadline is not None and deadline <= now:
                return {"task_id": task["task_id"], "status": "root_time_budget_exhausted", "result": None}
            timing = {"started_epoch": now, "deadline_epoch": deadline,
                      "time_budget_seconds": deadline - now if deadline is not None else None}
            packet = prepare_mesh_worker_packet(self.run_directory, child.task_directory / "bim",
                task_id=task["task_id"], question=task["question"], notes=task.get("notes", ()),
                candidate=task.get("candidate"), observation_ids=task.get("observation_ids", ()), timing=timing)
            record = {"task": task, "configuration": self._configuration(), "packet": packet, "timing": timing}
            # Admission belongs to the coordinator's in-flight operation. The
            # child journal stays empty until its own Runtime starts normally.
            self.store.append(RunLifecyclePayload(action="start", reason="mesh worker scope admitted: " + child.task_id),
                              source_refs=(self.store.source("mesh-worker-admission", record),))
            child.write_json("mesh_worker.json", record)
        outcome = self._anchored(child, "mesh-worker-outcome")
        if outcome is not None:
            return self._read(task["task_id"])[0]
        budget = task["budget"]
        child_limits = self.limits.model_copy(update={"model_calls": budget["model_calls"],
            "tool_calls": budget["tool_calls"], "seconds": record["timing"]["time_budget_seconds"],
            "tokens": budget.get("tokens")})
        saved_receipts = [source.blob for event in child.events for source in event.source_refs
                          if source.source_id == "run-receipt" and source.blob is not None]
        if saved_receipts:
            receipt = json.loads(child.get_bytes(saved_receipts[-1]))
            try:
                disk_receipt = json.loads((child.task_directory / "receipt.json").read_bytes())
            except (FileNotFoundError, ValueError):
                disk_receipt = None
            # A completed journal stop can precede a failed final receipt write.
            # Let Runtime restore that suffix and finish accounting before
            # publishing a worker outcome; only an identical receipt is cached.
            if receipt.get("status") == "completed" and disk_receipt == receipt:
                return self._save_outcome(child, record, receipt)
        receipt = await self._execute_child(child, record, child_limits)
        return self._save_outcome(child, record, receipt)

    async def _execute_child(self, child, record, limits):
        from src.agent.runtime_mesh_evidence import hydrate_mesh_worker_answer, mesh_worker_view_blocks
        role = local_observer_role(limits.ledger_limit())
        child_run = child.task_directory / "bim"
        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(frozen_bim_client(
                child_run, readonly=True, repository_root=self.root))
            tools = _EvidenceTools(FrozenBimTools(client, role, run_directory=child_run), record["packet"], child)
            catalog = await tools.list_tools()
            context_values = self.route.get("context", {})
            context_policy = ContextPolicy.model_validate_json(json.dumps({key: value for key, value in context_values.items()
                if key in ContextPolicy.model_fields})) if context_values.get("enabled", True) else None
            if context_policy is not None and context_policy.image_history_batches is not None:
                context_tool = context_image_tool_spec()
                catalog = [*catalog, context_tool]
                # Match Runtime.run's role before the early resume config check.
                if not any(grant.tool_name == context_tool["name"] for grant in role.tool_whitelist):
                    role = role.model_copy(update={"tool_whitelist": (*role.tool_whitelist,
                        ToolGrant(tool_name=context_tool["name"], access="read"))})
            specs = [{"type": "function", "function": {"name": t["name"],
                "description": t.get("description", ""), "parameters": t["inputSchema"]}} for t in catalog]
            versions = make_versions(child, root=self.root, prompt=WORKER_GUIDANCE, tools=specs,
                parameters=self.parameters, route=self.route,
                code_paths=("src/agent/runtime_mesh_workers.py", "src/agent/runtime_mesh_evidence.py",
                            "src/agent/runtime_tools.py", "src/agent/bim_inputs.py", "src/agent/geometry", "scripts/tool_scripts"))
            adapter = self.adapter_factory(child.task_id, child)
            if inspect.isawaitable(adapter):
                adapter = await adapter
            if hasattr(adapter, "close"):
                stack.push_async_callback(adapter.close)
            engine = Runtime(store=child, adapter=adapter, tools=tools, role=role, model=self.model,
                parameters=self.parameters, versions=versions, limits=limits,
                pricing=self.pricing,
                root_tool_calls=self.limits.tool_calls, start_epoch=record["timing"]["started_epoch"],
                strict_model_profile=self.route["route_id"] in LIVE_PROVIDERS,
                reasoning_history=self.reasoning_history, low_output_limit_reason=self.low_output_limit_reason,
                context_policy=context_policy, context_update=update_observer_budget,
                request_timeout_seconds=(self.limits.seconds / (self.max_concurrent + 1)
                                         if self.limits.seconds is not None
                                         and self.limits.time_budget_mode == "wall_and_requests" else None),
                answer_validator=lambda answer: hydrate_mesh_worker_answer(answer, record["packet"], child, child_run),
                max_answer_repairs=1, answer_reserve_calls=min(2, max(1, limits.model_calls - 1)))
            saved = child.latest_checkpoint()
            if saved and (saved[1]["versions"] != versions.model_dump(mode="json") or
                          saved[1].get("config", engine._config()) != engine._config()):
                raise ValueError("worker implementation/configuration changed; refusing recovery before requests")
            payload = mesh_worker_task_payload(record["packet"], limits)
            content = [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]
            originals = {}
            for block in mesh_worker_view_blocks(record["task"].get("observation_ids", ()),
                    record["packet"], child, child_run):
                if block["type"] == "image":
                    raw = base64.b64decode(block["data"], validate=True)
                    originals[hashlib.sha256(raw).hexdigest()] = child.put_bytes(raw, block["mimeType"])
                    content.append({"type": "image_url", "image_url": {
                        "url": "data:" + block["mimeType"] + ";base64," + block["data"]}})
                else:
                    content.append(block)
            return await engine.run([{"role": "system", "content": WORKER_GUIDANCE},
                {"role": "user", "content": content}], image_originals=originals, resume=saved is not None)

    def _save_outcome(self, child, record, receipt):
        from src.agent.runtime_mesh_evidence import hydrate_mesh_worker_answer, mesh_worker_evidence_catalog
        result = None
        if receipt["status"] == "completed":
            result = hydrate_mesh_worker_answer(receipt["answer"], record["packet"], child, child.task_directory / "bim")
        outcome = {"task_id": child.task_id, "status": receipt["status"], "result": result,
            "runtime": {key: receipt.get(key) for key in ("status", "model_calls", "tool_calls", "reported_tokens",
                "elapsed_seconds", "budget_available", "root_budget_available")},
            "answer": receipt.get("answer"), "source_basis": record["packet"].get("source_snapshot"),
            "evidence_catalog": mesh_worker_evidence_catalog(record["packet"], child, child.task_directory / "bim"),
            "answer_delivery": self._answer_delivery(child, receipt["status"]),
            "applicability": "frozen task inputs and optional source snapshot; coordinator judges current candidate"}
        if receipt["status"] in {"cancelled", "failed", "resume_pending_operation", "tool_presentation_failed"}:
            # These stops may have a durable response awaiting replay. Their
            # normal child journal remains the recovery authority.
            return {key: value for key, value in outcome.items() if key != "answer"}
        child.append(RunLifecyclePayload(action="stop", reason=receipt["status"]),
                     source_refs=(child.source("mesh-worker-outcome", outcome),
                                  child.source("run-receipt", receipt)))
        child.write_json("mesh_worker_outcome.json", outcome)
        return {key: value for key, value in outcome.items() if key != "answer"}

    def _read(self, task_id, view_ids=(), measurement_call_ids=()):
        from src.agent.runtime_mesh_evidence import (hydrate_mesh_worker_answer, mesh_worker_view_blocks,
                                                   mesh_worker_evidence_catalog, mesh_worker_measurements)
        child = self._child(task_id)
        record = self._record(child)
        outcome = self._anchored(child, "mesh-worker-outcome")
        catalog = mesh_worker_evidence_catalog(record["packet"], child, child.task_directory / "bim")
        if outcome is None:
            # Interrupted tasks remain resumable; expose their verified evidence
            # without fabricating or freezing a completed worker outcome.
            receipts = [source.blob for event in child.events for source in event.source_refs
                        if source.source_id == "run-receipt" and source.blob is not None]
            status = json.loads(child.get_bytes(receipts[-1])).get("status", "incomplete") if receipts else "incomplete"
            outcome = {"task_id": task_id, "status": "incomplete", "result": None,
                       "answer_delivery": self._answer_delivery(child, status),
                       "source_basis": record["packet"].get("source_snapshot")}
        if "evidence_catalog" in outcome and catalog != outcome["evidence_catalog"]:
            raise ValueError("saved worker evidence catalog no longer matches its recorded evidence")
        if outcome.get("result") is not None:
            checked = hydrate_mesh_worker_answer(outcome["answer"], record["packet"], child, child.task_directory / "bim")
            if checked != outcome["result"]:
                raise ValueError("saved worker result no longer matches its recorded evidence")
        blocks = mesh_worker_view_blocks(view_ids, record["packet"], child, child.task_directory / "bim") if view_ids else []
        previews, reference = [], None
        for block in blocks:
            if block.get("type") == "text":
                reference = json.loads(block["text"])
            elif block.get("type") == "image" and reference is not None:
                previews.append({**reference, "worker_task_id": task_id,
                    "view_id": "mesh-worker:" + task_id + ":" + reference["id"],
                    "returned_png_sha256": hashlib.sha256(base64.b64decode(block["data"], validate=True)).hexdigest()})
        value = {key: value for key, value in outcome.items() if key != "answer"}
        value["evidence_catalog"] = catalog
        if previews:
            value["evidence_previews"] = previews
        if measurement_call_ids:
            value["full_measurements"] = mesh_worker_measurements(
                measurement_call_ids, record["packet"], child, child.task_directory / "bim")
        return value, blocks

    @staticmethod
    def _answer_delivery(child, status):
        repairs = [event.payload for event in child.events if event.payload.event_type == "answer_repair"]
        requested = next((row for row in reversed(repairs) if row.phase == "request"), None)
        result = next((row for row in reversed(repairs) if row.phase == "result"), None)
        error = (result.repaired_validation_error if result and not result.accepted else
                 requested.validation_error if requested else None)
        return {"status": "validated" if status == "completed" else "not_validated",
            "stop_reason": status,
            "repair_status": ("accepted" if result and result.accepted else "rejected" if result else
                              "requested_but_not_completed" if requested else "not_requested"),
            "validation_error": error[:1000] if error else None,
            "meaning": "Only a validated result is a worker conclusion; evidence may remain reusable after failure."}

    def state(self):
        tasks = {}
        for event in self.store.all_events:
            for source in event.source_refs:
                if source.source_id == "mesh-worker-admission" and source.blob:
                    value = json.loads(self.store.get_bytes(source.blob))
                    if event.task_id != self.store.task_id:
                        continue
                    task_id = value["task"]["task_id"]
                    tasks[task_id] = {"task_id": task_id, "status": "incomplete",
                        "question": value["task"]["question"], "candidate": value["task"].get("candidate"),
                        "budget": value["task"]["budget"]}
                elif source.source_id == "mesh-worker-outcome" and source.blob and event.task_id in tasks:
                    value = json.loads(self.store.get_bytes(source.blob))
                    tasks[event.task_id].update(status=value["status"], runtime=value["runtime"])
        available = RuntimeBudget.from_events(self.store.budget_limit,
            self.store.budget_projection_events()).available.model_dump(mode="json")
        remaining = max(0, self.limits.tool_calls - sum(event.payload.event_type == "tool_invocation"
            for event in self.store.all_events))
        return {"model": self.model, "route_id": self.route["route_id"], "max_concurrent": self.max_concurrent,
            "token_budget": self._token_budget_contract(),
            "answer_budget_policy": {"reserved_calls": "2 for task limits >=3, otherwise 1",
                "scope": "within the original child/root budgets; unavailable time/tokens/root calls can still stop a task"},
            "tasks": list(tasks.values()), "shared_budget_available": available, "shared_tool_calls_available": remaining,
            "scope": "worker advice only; completion and BIM quality remain separate"}

    def snapshot_state(self):
        # Worker caches/results cannot masquerade as a source BIM mutation.
        # Their immutable records are independently verified on reuse/read.
        return self.frozen.snapshot_state()

    def image_origins(self, raw):
        origins = self.frozen.image_origins(raw)
        previews = raw.get("structuredContent", {}).get("evidence_previews", ())
        for preview in previews:
            if not isinstance(preview, dict) or "worker_task_id" not in preview:
                continue
            task_id = preview["worker_task_id"]
            if preview.get("view_id") != "mesh-worker:" + task_id + ":" + preview["id"]:
                raise ValueError("worker view identity changed")
            child_run = self._child(task_id).task_directory / "bim"
            path = (child_run / preview["image_file"]).resolve()
            if not path.is_relative_to(child_run.resolve()):
                raise ValueError("worker view escaped its task")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != preview["returned_png_sha256"] or digest != preview["file_sha256"][preview["image_file"]]:
                raise ValueError("worker view bytes changed after read")
            size = preview["resolution_px"]
            origins[digest] = {"view_id": preview["view_id"], "original_path": str(path),
                "original_sha256": digest, "sent_sha256": digest, "mime_type": "image/png",
                "box_original_pixels": [0, 0, size["width"], size["height"]],
                "original_pixels_per_returned_pixel": 1, "raw_metadata": preview,
                "origin_status": "verified_worker_observation_not_independent_building_truth"}
        return origins

    def artifacts(self):
        return self.frozen.artifacts()

    async def recover_pending(self):
        """Resume only stable child identities before the parent Runtime restores."""
        resolved = {event.payload.call_id for event in self.store.events
                    if event.payload.event_type == "tool_execution" and event.payload.outcome != "unknown"}
        latest = {event.payload.call_id: event for event in self.store.events
                  if event.payload.event_type == "tool_invocation"}
        recovered = []
        for call_id, event in latest.items():
            call = event.payload
            if call_id in resolved or call.tool_name != "delegate_mesh_workers":
                continue
            unknown = next((row for row in reversed(self.store.events)
                if row.payload.event_type == "tool_execution" and row.payload.invocation_event_id == event.event_id
                and row.payload.outcome == "unknown"), None)
            retry_source = next((source.blob for source in event.source_refs
                if source.source_id == "tool-recovery-attempt"), None)
            retry_id = json.loads(self.store.get_bytes(retry_source))["retry_event_id"] if retry_source else None
            if unknown:
                attempts = sum(row.payload.event_type == "tool_invocation" and row.payload.call_id == call_id
                               for row in self.store.events)
                if attempts > self.limits.max_tool_recovery_retries or sum(
                    row.payload.event_type == "tool_invocation" for row in self.store.all_events) >= self.limits.tool_calls:
                    continue
                retry = self.store.append(RunLifecyclePayload(action="retry", attempt=attempts + 1,
                    retry_of_event_id=unknown.event_id, reason="resume exact mesh worker task identities and checkpoints"))
                retry_id = retry.event_id
                event = self.store.append(ToolInvocationPayload(call_id=call_id, tool_name=call.tool_name,
                    full_arguments=call.full_arguments, repeatability=call.repeatability,
                    operation_key=call.operation_key, state_before=self.store.put_json(self.snapshot_state())),
                    source_refs=(self.store.source("tool-recovery-attempt", {"retry_event_id": retry_id}),))
            raw = await self.call_tool(call.tool_name, call.full_arguments)
            _, _, shown, _ = convert_tool_result(call_id, self.project_tool_result(call.tool_name, raw), self.store)
            self.store.append(ToolExecutionPayload(call_id=call_id, tool_name=call.tool_name,
                full_arguments=call.full_arguments, repeatability=call.repeatability,
                operation_key=call.operation_key, invocation_event_id=event.event_id, retry_event_id=retry_id,
                outcome="failed" if raw.get("isError") else "succeeded",
                raw_result=self.store.capture(raw, force_blob=True), shown_result=self.store.capture(shown, force_blob=True),
                presentation_status="prepared"), source_refs=(self.store.source("tool-state-after", self.snapshot_state()),
                    self.store.source("mesh-worker-recovery", {"method": "resume existing child checkpoints"})))
            recovered.append(event.event_id)
        return recovered
