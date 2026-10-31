"""Audited external MCP coordination over the unchanged frozen BIM tools.

The command-line developer model remains external. Its MCP arguments and
returns are captured; its private HTTP request, context and usage are explicitly
unavailable. Only the local observation role runs inside this service.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import re
import time
from decimal import Decimal
from pathlib import Path

import jsonschema
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from src.agent.contracts import EvidencePackage, LocalizedEvidenceResult, assert_result_applicable
from src.agent.runtime_delegation import RegisteredView, run_observer, views_from_tool_return
from src.agent.runtime_entry import ROOT, paratera_credentials, prepare_inputs
from src.agent.runtime_delivery import finalize_building
from src.agent.runtime_tools import (FrozenBimTools, coordinator_role, frozen_bim_client,
    local_observer_role, write_frozen_materials, write_frozen_tool_catalog)
from src.agent_runtime.adapter import HttpChatAdapter, ScriptedAdapter
from src.agent_runtime.anthropic import HttpAnthropicAdapter
from src.agent_runtime.agent_registry import agent_version_record
from src.agent_runtime.accounting import require_cny_price_schedule, request_accounting_from_store, summarize_request_accounting
from src.agent_runtime.budget import RuntimeBudget
from src.agent_runtime.call_quota import QuotaAdapter
from src.agent_runtime.loop import RunLimits
from src.agent_runtime.output_limits import default_output_tokens, validate_output_limit
from src.agent_runtime.providers import (CHATGPT_SUBSCRIPTION, GLM_SUBSCRIPTION, GLM_SUBSCRIPTION_ANTHROPIC, SUBSCRIPTION_PROVIDERS, LIVE_PROVIDERS,
    provider_parameters, subscription_credentials, validate_provider_model)
from src.agent_runtime.store import EventStore
from src.harness_contracts import (ExternalCoordinatorMcpPayload, MissingCapture,
    RunLifecyclePayload, StateInspectionPayload, ToolExecutionPayload, ToolInvocationPayload)


def result_envelope(value, *, error=False):
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
        "structuredContent": value, "isError": error}


EXTRA_TOOLS = [
    {"name": "delegate_to_role", "description":
        "Send a bounded question and recorded view IDs to a read-only local observer. "
        "Returns directly seen / interpretations / uncertain, original-pixel locations and budget status. "
        "Use view_image first to make a crop. No automatic model fallback.",
     "inputSchema": {"type": "object", "properties": {
         "task_id": {"type": "string", "pattern": "^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$"},
         "role_id": {"const": "local_observer", "default": "local_observer"},
         "question": {"type": "string", "minLength": 1},
         "view_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "uniqueItems": True},
         "notes": {"type": "array", "items": {"type": "string"}, "default": []},
         "budget": {"type": "object", "properties": {
             "model_calls": {"type": "integer", "minimum": 1, "maximum": 6},
             "tool_calls": {"type": "integer", "minimum": 0, "maximum": 12},
             "tokens": {"type": "integer", "minimum": 1},
             "seconds": {"type": "number", "exclusiveMinimum": 0}},
             "required": ["model_calls", "tool_calls", "tokens", "seconds"], "additionalProperties": False}},
         "required": ["task_id", "question", "view_ids", "budget"], "additionalProperties": False}},
    {"name": "inspect_local_observation", "description":
        "Read the original local observation, delivered image identities and applicability to current saved BIM.",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}},
                     "required": ["task_id"], "additionalProperties": False}},
    {"name": "apply_local_observation", "description":
        "Apply a coordinator-chosen frozen write tool based on a valid local observation. "
        "Immediately rechecks saved source BIM version; stale or already used results are refused. "
        "The observation is advice, not an automatic geometry edit.",
     "inputSchema": {"type": "object", "properties": {
         "task_id": {"type": "string"}, "tool_name": {"type": "string"},
         "arguments": {"type": "object"}, "reason": {"type": "string", "minLength": 1}},
         "required": ["task_id", "tool_name", "arguments", "reason"], "additionalProperties": False}},
    {"name": "runtime_state", "description":
        "Read current saved BIM version, registered views, child statuses and the shared remaining budget.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
]


EXTRA_TOOLS.insert(1, {
    "name": "delegate_to_roles",
    "description": "Dispatch several independent local observations in one batch. "
        "Model requests run concurrently up to the configured limit (default 4); "
        "frozen tool calls are serialized. Each task keeps its own budget and result, "
        "and one failed task does not cancel its siblings. Results follow input order.",
    "inputSchema": {"type": "object", "properties": {
        "tasks": {"type": "array", "minItems": 1, "maxItems": 32,
                  "items": EXTRA_TOOLS[0]["inputSchema"]}},
        "required": ["tasks"], "additionalProperties": False},
})


class SerializedToolAccess:
    """Keep the unchanged stateful tool service behind one shared async lock.

    This does not serialize model requests. Synchronous snapshots and image
    metadata reads run on the coordinator's single event loop without yielding.
    """

    def __init__(self, tools, lock):
        self._tools, self._lock = tools, lock

    def __getattr__(self, name):
        return getattr(self._tools, name)

    async def list_tools(self):
        async with self._lock:
            return await self._tools.list_tools()

    async def call_tool(self, name, arguments):
        async with self._lock:
            return await self._tools.call_tool(name, arguments)

    @asynccontextmanager
    async def task_session(self, **kwargs):
        if not hasattr(self._tools, "task_session"):
            yield self
            return
        async with self._tools.task_session(**kwargs) as tools:
            # Keep the R1 serialization guarantee for tool execution, while
            # each observer service holds its own immutable deadline.
            yield SerializedToolAccess(tools, self._lock)


class CoordinatorSession:
    def __init__(self, *, store, tools, observer_tools, adapter_factory, model, parameters,
                 root=ROOT, route_id="scripted", guide="", limits=None,
                 max_concurrent_observers=4, low_output_limit_reason=None, start_epoch=None):
        if type(max_concurrent_observers) is not int or max_concurrent_observers < 1:
            raise ValueError("max_concurrent_observers must be a positive integer")
        self.max_concurrent_observers = max_concurrent_observers
        self.frozen_lock = asyncio.Lock()
        self.store = store
        self.tools = SerializedToolAccess(tools, self.frozen_lock)
        self.observer_tools = SerializedToolAccess(observer_tools, self.frozen_lock)
        self.adapter_factory, self.model, self.parameters = adapter_factory, model, parameters
        self.root, self.route_id, self.guide = Path(root), route_id, guide
        self.limits = limits
        self.low_output_limit_reason = low_output_limit_reason
        self.lock = asyncio.Lock()
        self.views, self.children, self.applied = {}, {}, set()
        self.catalog = []
        self.started_epoch = time.time() if start_epoch is None else start_epoch
        self.unknown_write = False
        self._restore_indexes()

    def _restore_indexes(self):
        for event in self.store.events:
            for source in event.source_refs:
                if source.source_id == "coordinator-session" and source.blob:
                    self.started_epoch = json.loads(self.store.get_bytes(source.blob))["started_epoch"]
                if source.source_id == "registered-views" and source.blob:
                    for row in json.loads(self.store.get_bytes(source.blob)):
                        view = RegisteredView.from_json(row)
                        self.views[view.view_id] = view
                if source.source_id == "delegation-outcome" and source.blob:
                    value = json.loads(self.store.get_bytes(source.blob))
                    self.children[value["package"]["task_id"]] = value
                if source.source_id == "applied-observation" and source.blob:
                    from scripts.tool_scripts.bim_agent_saved_result import read_saved_result, result_metadata
                    p = event.payload
                    if p.event_type == "tool_execution" and p.outcome == "succeeded":
                        saved = read_saved_result(result_metadata(self.store.resolve(p.raw_result)),
                            tool=p.tool_name, run=self.tools.run_directory)
                        if saved["save_effects"]["geometry_applied"]:
                            self.applied.add(json.loads(self.store.get_bytes(source.blob))["task_id"])
            if event.payload.event_type == "tool_execution" and event.payload.outcome == "unknown":
                self.unknown_write |= event.payload.repeatability != "read_only"
        complete = {e.payload.invocation_event_id for e in self.store.events
                    if e.payload.event_type == "tool_execution"}
        for event in list(self.store.events):
            if (event.task_id != self.store.task_id or event.payload.event_type != "tool_invocation"
                    or event.event_id in complete):
                continue
            p = event.payload
            failed = self.store.append(ToolExecutionPayload(call_id=p.call_id, tool_name=p.tool_name,
                full_arguments=p.full_arguments, repeatability=p.repeatability,
                operation_key=p.operation_key, outcome="unknown",
                raw_result=MissingCapture(reason="external operation interrupted after its durable intent"),
                shown_result=MissingCapture(reason="MCP return not captured"),
                invocation_event_id=event.event_id))
            if p.repeatability != "read_only":
                self._inspect_unknown(failed.event_id, p.state_before)
                self.unknown_write = True

    async def initialize(self):
        self.catalog = await self.tools.list_tools()
        self.agent_version = agent_version_record(self.root)
        from dataclasses import asdict
        from src.agent_runtime.estimation import get_model_profile
        source_files = [*sorted((self.root / "src/agent_runtime").glob("*.py")),
            self.root / "src/agent/runtime_coordinator.py",
            self.root / "src/agent/runtime_delivery.py",
            self.root / "src/agent/runtime_delegation.py"]
        configuration = {"model": self.model, "route_id": self.route_id,
            "agent_version": self.agent_version,
            "parameters": self.parameters,
            "low_output_limit_reason": self.low_output_limit_reason,
            "max_concurrent_observers": self.max_concurrent_observers,
            "frozen_tool_scheduling": "serialized_tools_with_child_scoped_services",
            "limits": self.limits.model_dump(mode="json") if self.limits else None,
            "model_profile": asdict(get_model_profile(self.model)),
            "guide_sha256": hashlib.sha256(self.guide.encode()).hexdigest(),
            "tools_sha256": hashlib.sha256(json.dumps(self.catalog, sort_keys=True).encode()).hexdigest(),
            "source_files": {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in source_files if p.is_file()}}
        configuration = json.loads(json.dumps(configuration))
        config_path = self.store.task_directory / "coordinator-config.json"
        if config_path.is_file() and json.loads(config_path.read_bytes()) != configuration:
            raise ValueError("coordinator resume configuration or implementation changed")
        if not config_path.is_file():
            self.store.write_json("coordinator-config.json", configuration)
        if not self.store.events:
            self.store.append(RunLifecyclePayload(action="start", reason="external MCP coordinator session"),
                source_refs=(self.store.source("coordinator-session", {"started_epoch": self.started_epoch,
                    "model": self.model, "route_id": self.route_id,
                    "agent_version": self.agent_version}),))
        self.schemas = {v["name"]: v["inputSchema"] for v in [*self.catalog, *EXTRA_TOOLS]}
        return self

    def _external(self, phase, name, arguments, result=None, *, sources=()):
        missing = {"kind": "missing", "reason": "未获取：external CLI model HTTP request, hidden context and provider usage are not exposed by MCP"}
        return self.store.append(ExternalCoordinatorMcpPayload(phase=phase,
            coordinator_task_id=self.store.task_id, method=name,
            request_content=self.store.capture({"method": name, "arguments": arguments}),
            result_content=(self.store.capture(result, force_blob=True) if result is not None
                else MissingCapture(reason="operation has not returned yet"))),
            source_refs=(*sources, self.store.source("external-model-request-and-usage", missing)))

    async def list_tools(self):
        self._external("operation", "tools/list", {})
        result = [*self.catalog, *EXTRA_TOOLS]
        self._external("return", "tools/list", {}, {"tools": result})
        return result

    def source_bim(self):
        run = self.tools.run_directory
        selected = None
        for event in reversed(self.store.events):
            p = event.payload
            if p.event_type == "tool_execution" and p.outcome == "succeeded":
                from scripts.tool_scripts.bim_agent_saved_result import read_saved_result, result_metadata
                selected = read_saved_result(result_metadata(self.store.resolve(p.raw_result)),
                    tool=p.tool_name, run=run)["saved_candidate"]
                if selected:
                    break
        candidates = sorted(run.glob("candidate_*/source_model.json"))
        path = run / selected / "source_model.json" if selected else None
        if path is None or not path.is_file():
            path = candidates[-1] if candidates else run / "seed/source_model.json"
        if path.is_file():
            ref = self.store.put_bytes(path.read_bytes(), "application/json")
            return {"version_id": f"{path.parent.name}:{ref.sha256}", "candidate": path.parent.name,
                "file": ref.model_dump(mode="json")}
        ref = self.store.put_bytes((run / "inputs.json").read_bytes(), "application/json")
        return {"version_id": "unbuilt:" + ref.sha256, "candidate": None,
                "inputs": ref.model_dump(mode="json")}

    def state(self):
        budget = RuntimeBudget.from_events(self.store.budget_limit, self.store.all_events)
        return {"source_bim": self.source_bim(), "views": [v.as_json() for v in self.views.values()],
            "agent_version": self.agent_version["version_id"],
            "usage_accounting": summarize_request_accounting(request_accounting_from_store(self.store, e.event_id)
                for e in self.store.all_events if e.payload.event_type == "adapter_request"),
            "children": {key: row["status"] for key, row in self.children.items()},
            "budget": budget.ledger.model_dump(mode="json"), "unknown_write": self.unknown_write,
            "external_model_request": "未获取", "external_model_usage": "未获取",
            "max_concurrent_observers": self.max_concurrent_observers,
            "frozen_tool_scheduling": "serialized_tools_with_child_scoped_services"}

    async def call_tool(self, name, arguments):
        async with self.lock:
            self._external("dispatch" if name in {"delegate_to_role", "delegate_to_roles"} else "operation", name, arguments)
            try:
                if name not in self.schemas:
                    raise ValueError("unknown coordinator tool")
                jsonschema.validate(arguments, self.schemas[name])
                if self.limits and time.time() - self.started_epoch >= self.limits.seconds:
                    value = result_envelope({"status": "time_budget_exhausted"}, error=True)
                elif name == "delegate_to_role":
                    value = result_envelope(await self.delegate(arguments))
                elif name == "delegate_to_roles":
                    value = result_envelope(await self.delegate_many(arguments["tasks"]))
                elif name == "inspect_local_observation":
                    value = result_envelope(self.inspect(arguments["task_id"]))
                elif name == "apply_local_observation":
                    value = await self.apply(arguments)
                elif name == "runtime_state":
                    value = result_envelope(self.state())
                else:
                    value = await self._execute_frozen(name, arguments)
            except (ValueError, KeyError, jsonschema.ValidationError) as exc:
                value = result_envelope({"status": "rejected", "reason": str(exc)}, error=True)
            except Exception as exc:
                value = result_envelope({"status": "coordinator_failed", "error_type": type(exc).__name__}, error=True)
            self._external("return", name, arguments, value)
            return value

    async def _execute_frozen(self, name, arguments, *, application=None):
        if self.limits and sum(e.payload.event_type == "tool_invocation" for e in self.store.all_events) >= self.limits.tool_calls:
            return result_envelope({"status": "tool_budget_exhausted"}, error=True)
        repeatability = self.tools.repeatability(name)
        write = repeatability != "read_only"
        if write and self.unknown_write:
            raise ValueError("unknown write outcome requires state inspection; no additional writes admitted")
        call_id = "coordinator-" + str(len(self.store.all_events))
        key = self.store.run_id + ":" + call_id if write else None
        before = self.store.put_json(self.tools.snapshot_state())
        invocation = self.store.append(ToolInvocationPayload(call_id=call_id, tool_name=name,
            full_arguments=arguments, repeatability=repeatability, operation_key=key, state_before=before))
        try:
            running = self.tools.call_tool(name, arguments)
            if self.limits:
                remaining = max(0.0, self.limits.seconds - (time.time() - self.started_epoch))
                raw = await asyncio.wait_for(running, timeout=remaining)
            else:
                raw = await running
        except (Exception, asyncio.CancelledError) as exc:
            execution = self.store.append(ToolExecutionPayload(call_id=call_id, tool_name=name,
                full_arguments=arguments, repeatability=repeatability, operation_key=key,
                outcome="unknown", raw_result=MissingCapture(reason="MCP transport did not return"),
                shown_result=MissingCapture(reason="no completed MCP result"), invocation_event_id=invocation.event_id))
            if write:
                self.unknown_write = True
                self._inspect_unknown(execution.event_id, before)
            return result_envelope({"status": "unknown_write_outcome" if write else "tool_transport_failed",
                                    "error_type": type(exc).__name__}, error=True)
        sources = [self.store.source("coordinator-tool-state-after", self.tools.snapshot_state())]
        from scripts.tool_scripts.bim_agent_saved_result import read_saved_result, result_metadata
        saved = read_saved_result(result_metadata(raw), tool=name, run=self.tools.run_directory)
        geometry_applied = not raw.get("isError") and saved["save_effects"]["geometry_applied"]
        if application:
            sources.append(self.store.source("observation-application-attempt", {**application, **saved}))
        if application and geometry_applied:
            sources.append(self.store.source("applied-observation", application))
        execution = self.store.append(ToolExecutionPayload(call_id=call_id, tool_name=name,
            full_arguments=arguments, repeatability=repeatability, operation_key=key,
            outcome="failed" if raw.get("isError") else "succeeded",
            # Runtime write acknowledgement includes the durable audit journal;
            # only geometry_applied can consume a localized observation.
            applied_write_id=key if write and not raw.get("isError") else None,
            raw_result=self.store.capture(raw, force_blob=True),
            shown_result=self.store.capture(raw, force_blob=True), invocation_event_id=invocation.event_id),
            source_refs=tuple(sources))
        views = views_from_tool_return(self.store, self.tools, raw, execution.event_id)
        if views:
            self._external("return", "registered_views", {"tool_event_id": execution.event_id},
                {"view_ids": [v.view_id for v in views]}, sources=(self.store.source(
                    "registered-views", [v.as_json() for v in views]),))
            for view in views:
                self.views[view.view_id] = view
        if application and geometry_applied:
            self.applied.add(application["task_id"])
        return raw

    def _inspect_unknown(self, event_id, before):
        state = self.store.put_json({"before": before.model_dump(mode="json") if before else None,
            "observed_after": self.tools.snapshot_state(),
            "reason": "saved snapshot alone does not prove write application"})
        self.store.append(StateInspectionPayload(purpose="unknown_write_recovery",
            target_event_id=event_id, persisted_state=state, conclusion="inconclusive"))

    async def delegate_many(self, tasks):
        identities = [item["task_id"] for item in tasks]
        if len(set(identities)) != len(identities) or self.store.task_id in identities:
            raise ValueError("batch task IDs must be distinct and different from coordinator")
        slots = asyncio.Semaphore(self.max_concurrent_observers)

        async def dispatch(arguments):
            async with slots:
                self._external("dispatch", "delegate_to_role", arguments)
                try:
                    outcome = await self.delegate(arguments)
                except (ValueError, KeyError) as exc:
                    outcome = {"status": "rejected", "task_id": arguments["task_id"], "reason": str(exc)}
                except Exception as exc:
                    outcome = {"status": "child_failed", "task_id": arguments["task_id"],
                               "error_type": type(exc).__name__}
                self._external("return", "delegate_to_role", arguments, outcome)
                return outcome

        results = await asyncio.gather(*(dispatch(item) for item in tasks))
        return {"status": "completed", "results": results,
                "max_concurrent_observers": self.max_concurrent_observers,
                "frozen_tool_scheduling": "serialized_tools_with_child_scoped_services"}

    def finalize(self, reason):
        """Hand off saved work when the external MCP session ends or expires."""
        receipt = {"status": reason, "agent_version": self.agent_version["version_id"],
            "started_epoch": self.started_epoch,
            "deadline_epoch": self.started_epoch + self.limits.seconds if self.limits else None,
            "finalization": finalize_building(self.tools.run_directory, reason=reason,
                elapsed_seconds=max(0.0, time.time() - self.started_epoch)),
            "external_model_completion": "not_captured"}
        self.store.write_json("receipt.json", receipt)
        self.store.append(RunLifecyclePayload(action="stop", reason=reason),
            source_refs=(self.store.source("coordinator-receipt", receipt),))
        return receipt

    async def delegate(self, arguments):
        task_id = arguments["task_id"]
        if task_id == self.store.task_id or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", task_id):
            raise ValueError("child task identity is invalid or collides with coordinator")
        child = self.store.for_task(task_id, parent_task_id=self.store.task_id)
        config_path = child.task_directory / "delegation.json"
        if config_path.is_file():
            saved = json.loads(config_path.read_bytes())
            if saved["arguments"] != arguments:
                raise ValueError("existing task ID cannot be reused for different evidence or instructions")
            if task_id in self.children:
                return {**self.children[task_id], "reused_saved_result": True}
            package = EvidencePackage.model_validate_json(json.dumps(saved["package"]))
            views = [RegisteredView.from_json(v) for v in saved["views"]]
            if package.source_model_version_id != self.source_bim()["version_id"]:
                raise ValueError("cannot resume local observation after source BIM changed")
        else:
            views = [self.views[key] for key in arguments["view_ids"]]
            package = EvidencePackage(package_id="package-" + task_id, task_id=task_id,
                role_id="local_observer", question=arguments["question"], known_evidence_ids=(),
                image_refs=tuple(v.reference for v in views),
                source_model_version_id=self.source_bim()["version_id"],
                budget_reservation_id=task_id + ":request-1")
            child.write_json("delegation.json", {"arguments": arguments, "package": package.model_dump(mode="json"),
                                                 "views": [v.as_json() for v in views]})
        limits = RunLimits(**arguments["budget"])
        if self.limits:
            limits = limits.model_copy(update={
                "max_consecutive_truncations": self.limits.max_consecutive_truncations,
                "max_total_truncations": self.limits.max_total_truncations})
            remaining = self.limits.seconds - (time.time() - self.started_epoch)
            if remaining <= 0:
                return {"status": "time_budget_exhausted", "package": package.model_dump(mode="json")}
            # A restored task must retain its limits. The root's time budget is
            # also enforced by its ledger and this enclosing operation timeout.
        try:
            adapter = self.adapter_factory(task_id)
            running = run_observer(store=child, frozen_tools=self.observer_tools, adapter=adapter,
                model=self.model, parameters=self.parameters, limits=limits, package=package, views=views,
                notes=arguments.get("notes", []), root=self.root,
                root_tool_calls=self.limits.tool_calls if self.limits else None,
                low_output_limit_reason=self.low_output_limit_reason,
                parent_deadline_epoch=self.started_epoch + self.limits.seconds if self.limits else None,
                route={"route_id": self.route_id, "model": self.model}, resume=bool(child.events))
            outcome = await asyncio.wait_for(running, timeout=remaining) if self.limits else await running
        except Exception as exc:
            # Preserve a failed child's identity and evidence as an inspectable
            # result. Outstanding reservations remain held, never refunded on
            # an unknown failure, and siblings may use only the remaining root.
            child.append(RunLifecyclePayload(action="failure", failure_stage="local_observer",
                reason="local observer failed: " + type(exc).__name__))
            outcome = {"status": "child_failed", "error_type": type(exc).__name__,
                "result": None, "runtime": None, "validation_error": None,
                "package": package.model_dump(mode="json"),
                "views": [v.as_json() for v in views], "model": self.model}
        child.write_json("observation.json", outcome)
        self._external("return", "delegation_outcome", {"task_id": task_id}, outcome,
            sources=(self.store.source("delegation-outcome", outcome),))
        self.children[task_id] = outcome
        return outcome

    def inspect(self, task_id):
        outcome = self.children[task_id]
        applicable, reason = False, outcome["status"]
        if outcome.get("result"):
            try:
                assert_result_applicable(EvidencePackage.model_validate_json(json.dumps(outcome["package"])),
                    LocalizedEvidenceResult.model_validate_json(json.dumps(outcome["result"])), self.source_bim()["version_id"])
                applicable, reason = task_id not in self.applied, "already_applied" if task_id in self.applied else "current_source_version"
            except ValueError as exc:
                reason = str(exc)
        return {**outcome, "applicable": applicable, "applicability_reason": reason}

    async def apply(self, arguments):
        task_id = arguments["task_id"]
        checked = self.inspect(task_id)
        if not checked["applicable"]:
            raise ValueError(checked["applicability_reason"])
        name, params = arguments["tool_name"], arguments["arguments"]
        if name not in {t["name"] for t in self.catalog} or self.tools.repeatability(name) == "read_only":
            raise ValueError("application needs a coordinator write tool")
        if params.get("candidate") and params["candidate"] != self.source_bim()["candidate"]:
            raise ValueError("application target is not the checked current BIM candidate")
        jsonschema.validate(params, self.schemas[name])
        return await self._execute_frozen(name, params, application={"task_id": task_id,
            "source_bim": self.source_bim(), "reason": arguments["reason"]})


async def serve(args):
    if args.provider == CHATGPT_SUBSCRIPTION:
        raise ValueError("ChatGPT subscription uses the native single_model or role_division runtime entry")
    out = args.out.resolve()
    if not out.is_relative_to(ROOT):
        raise ValueError("coordinator output must stay inside this worktree")
    validate_provider_model(args.provider, args.model)
    effective_model = args.model if args.provider in LIVE_PROVIDERS else "scripted-model"
    args.output_tokens = args.output_tokens if args.output_tokens is not None else default_output_tokens(
        effective_model, fallback=8192)
    validate_output_limit(effective_model, args.output_tokens, reason=args.low_output_limit_reason)
    parameters = provider_parameters(args.provider, output_tokens=args.output_tokens, thinking=args.thinking,
                                     reasoning_effort=args.reasoning_effort)
    if args.money_cny is not None:
        require_cny_price_schedule(effective_model, route_id=args.provider)
    limits = RunLimits(model_calls=args.model_calls, tool_calls=args.tool_calls,
                       seconds=args.seconds, tokens=args.tokens, money_cny=args.money_cny,
                       max_consecutive_truncations=args.max_consecutive_truncations,
                       max_total_truncations=args.max_total_truncations)
    if args.resume:
        run, guide = out / "bim", (out / "guide.txt").read_text()
    else:
        out.mkdir(parents=True, exist_ok=False)
        run, guide, _ = prepare_inputs(out, images=args.images, mesh=args.mesh,
            building_input=None, scope=args.scope, image_kind=args.image_kind, max_candidates=24,
            floor_plan_images=args.floor_plan_images, started_epoch=time.time(), seconds=limits.seconds)
    started_epoch = json.loads((run / "inputs.json").read_bytes()).get("started_epoch")
    adapter = None
    if args.provider in LIVE_PROVIDERS:
        from src.agent_runtime.estimation import get_model_profile
        get_model_profile(args.model, strict=True)
        base_url, key = (subscription_credentials(args.credentials_file, provider=args.provider)
            if args.provider in SUBSCRIPTION_PROVIDERS else paratera_credentials(args.credentials_file))
        adapter_type = HttpAnthropicAdapter if args.provider == GLM_SUBSCRIPTION_ANTHROPIC else HttpChatAdapter
        adapter = adapter_type(base_url=base_url, api_key=key)
        if args.quota_journal is None or not args.quota_journal.resolve().is_relative_to(ROOT):
            raise ValueError("live coordinator needs a persistent --quota-journal inside this worktree")
        quota = QuotaAdapter(adapter, args.quota_journal, limit=args.quota_limit, category="role_tests")
        factory = lambda _: quota
        model = args.model
    else:
        if args.script is None:
            raise ValueError("scripted coordinator requires explicit child response fixtures")
        fixtures = json.loads(args.script.read_bytes())
        factory = lambda task: ScriptedAdapter(fixtures[task][sum(
            e.task_id == task and e.payload.event_type == "adapter_request" for e in store.all_events):])
        model = "scripted-model"
    try:
        with EventStore(out, run_id=out.name, task_id="coordinator", budget_limit=limits.ledger_limit()) as store:
            async with frozen_bim_client(run, repository_root=ROOT) as client:
                async with frozen_bim_client(run, readonly=True, repository_root=ROOT) as observer_client:
                    tools = FrozenBimTools(client, coordinator_role(limits.ledger_limit()), run_directory=run)
                    observers = FrozenBimTools(observer_client, local_observer_role(limits.ledger_limit()), run_directory=run)
                    session = await CoordinatorSession(store=store, tools=tools, observer_tools=observers,
                        adapter_factory=factory, model=model, route_id=args.provider, guide=guide, limits=limits,
                        max_concurrent_observers=args.max_concurrent_observers,
                        low_output_limit_reason=args.low_output_limit_reason,
                        start_epoch=started_epoch,
                        parameters=parameters).initialize()
                    if not args.resume:
                        write_frozen_materials(out / "frozen", repository_root=ROOT)
                        write_frozen_tool_catalog(out / "frozen", await client.list_tools(), readonly=False)
                    server = Server("bim-runtime-coordinator", instructions=guide)

                    @server.list_tools()
                    async def list_tools():
                        return [types.Tool.model_validate(t) for t in await session.list_tools()]

                    @server.call_tool(validate_input=False)
                    async def call_tool(name, arguments):
                        return types.CallToolResult.model_validate(await session.call_tool(name, arguments))

                    async with stdio_server() as (reader, writer):
                        reason = "coordinator_disconnected"
                        try:
                            remaining = max(0.0, session.started_epoch + limits.seconds - time.time())
                            await asyncio.wait_for(server.run(reader, writer,
                                server.create_initialization_options()), timeout=remaining)
                        except asyncio.TimeoutError:
                            reason = "time_budget_exhausted"
                        finally:
                            session.finalize(reason)
    finally:
        if adapter:
            await adapter.close()


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--images", type=Path)
    p.add_argument("--floor-plan-image", dest="floor_plan_images", action="append")
    p.add_argument("--mesh", type=Path)
    p.add_argument("--image-kind", choices=("drawings", "mesh_views", "photos", "unknown"), default="drawings")
    p.add_argument("--scope", default="Local observation and coordinator operations; no whole-case run authorized.")
    p.add_argument("--provider", choices=("scripted", *(p for p in LIVE_PROVIDERS if p not in {CHATGPT_SUBSCRIPTION, "opencode-go", "deepseek"})), required=True)
    p.add_argument("--model", default="Qwen3.8-27B")
    p.add_argument("--credentials-file", type=Path)
    p.add_argument("--script", type=Path)
    p.add_argument("--quota-journal", type=Path, help="shared durable request tickets for this approved batch")
    p.add_argument("--quota-limit", type=int, default=60)
    p.add_argument("--model-calls", type=int, default=20)
    p.add_argument("--tool-calls", type=int, default=100)
    token_limit = p.add_mutually_exclusive_group()
    token_limit.add_argument("--tokens", type=int, default=300_000)
    token_limit.add_argument("--no-token-limit", dest="tokens", action="store_const", const=None)
    p.add_argument("--money-cny", type=Decimal,
                   help="shared CNY estimate ceiling for local observers; external coordinator usage is unavailable")
    p.add_argument("--seconds", type=float, default=3600)
    p.add_argument("--output-tokens", type=int, help="defaults to the reviewed model recommendation")
    p.add_argument("--low-output-limit-reason", help="explicit reason for an output cap below the recommendation")
    p.add_argument("--max-consecutive-truncations", type=int, default=2)
    p.add_argument("--max-total-truncations", type=int, default=3)
    p.add_argument("--max-concurrent-observers", type=int, default=4)
    p.add_argument("--thinking", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--reasoning-effort", choices=("low", "medium", "high", "max"))
    p.add_argument("--resume", action="store_true")
    return p


if __name__ == "__main__":
    asyncio.run(serve(parser().parse_args()))
