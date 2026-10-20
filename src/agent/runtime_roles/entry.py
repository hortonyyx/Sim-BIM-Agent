"""Opt-in drawing role runtime, preserving the single-model entrypoint."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import AsyncExitStack
from pathlib import Path

from src.agent.runtime_delivery import completed_building_handoff
from src.agent.runtime_entry import ROOT, parser as single_parser, prepare_inputs, publish_tool_budget
from src.agent.runtime_tools import FrozenBimCatalog, FrozenBimTools, coordinator_role, frozen_bim_client, write_frozen_materials, write_frozen_tool_catalog
from src.agent_runtime.adapter import ScriptedAdapter
from .context_policy import context_policy_from_effective, effective_role_context
from src.agent_runtime.connections import resolve_connection, validate_connection_model
from src.agent_runtime.loop import Runtime, RunLimits
from src.agent_runtime.providers import CHATGPT_SUBSCRIPTION, SUBSCRIPTION_PROVIDERS, LIVE_PROVIDERS
from src.agent_runtime.run_paths import resolve_run_output
from src.agent_runtime.store import EventStore
from src.agent_runtime.versions import make_versions
from src.harness_contracts.roles import ToolGrant

from .accounting import role_accounting
from .assembly_review import finalize_role_building
from .config import load_roles
from .guidance import get_role_guide
from .session import RoleSession, update_role_context, role_parameters


def parser():
    result = single_parser()
    result.description = __doc__
    result.add_argument("--roles-json", required=True, help="explicit JSON object with all three role routes")
    result.add_argument("--max-concurrent-readers", type=int, default=8)
    result._option_string_actions["--continue-from"].help = (
        "explicit new-run continuation from a terminal drawing run; source remains read-only")
    result.add_argument("--reader-task-json",
                        help="run exactly this one reader task through the same role runtime, without a coordinator or assembly")
    return result


async def execute(args, *, adapter_factory=None, fault_hook=None, reader_fault_hook=None):
    if (getattr(args, "model_reconnect_seconds", 0)
            or getattr(args, "allow_recovery_policy_change", False)):
        raise ValueError("explicit network recovery settings currently require single_model")
    unit = json.loads(args.reader_task_json) if getattr(args, "reader_task_json", None) else None
    if unit is not None:
        import jsonschema
        from .session import TASK_SCHEMA
        jsonschema.validate(unit, TASK_SCHEMA)
        if getattr(args, "continue_from", None):
            raise ValueError("reader unit runs cannot import a whole-run continuation")
    if args.seconds is None:
        raise ValueError("--no-time-limit currently supports single_model only; role_division requires --seconds")
    if args.mesh or args.image_kind != "drawings":
        raise ValueError("role_division v1 accepts drawing images only")
    routes = load_roles(json.loads(args.roles_json), allow_scripted=args.provider == "scripted" and args.script is not None)
    primary = routes["coordinator"].model_dump(mode="json")
    if (args.provider, args.model) != (primary["provider"], primary["model"]):
        raise ValueError("coordinator route must match the explicit case route")
    if args.money_usd is not None or args.price_schedule:
        raise ValueError("role_division uses the shared route-aware CNY ledger; per-run USD price overrides are not supported")
    if args.money_cny is not None and any(role.provider in SUBSCRIPTION_PROVIDERS for role in routes.values()):
        raise ValueError("subscription roles cannot participate in a usage-priced money ceiling")
    if args.near_limit != "stop" and any(role.provider == CHATGPT_SUBSCRIPTION for role in routes.values()):
        raise ValueError("ChatGPT subscription cannot enforce a reduced output cap; use --near-limit stop")
    global_context = {"context_tokens": args.context_tokens,
        "compact_at_tokens": args.compact_at_tokens,
        "active_window_messages": args.context_window,
        "large_result_bytes": args.large_result_bytes,
        "max_images": args.max_images, "max_image_bytes": args.max_image_bytes}
    global_context = {key: value for key, value in global_context.items() if value is not None}
    role_contexts = {name: effective_role_context(name, global_defaults=global_context,
        role_overrides=route.context) for name, route in routes.items()}
    coordinator_context = role_contexts["coordinator"]
    limits = RunLimits(model_calls=args.model_calls, tool_calls=args.tool_calls, seconds=args.seconds,
        tokens=args.tokens, money_cny=args.money_cny, near_limit=args.near_limit,
        min_output_tokens=args.min_output_tokens, context_tokens=coordinator_context.context_tokens,
        max_model_retries=args.model_retries, retry_backoff_seconds=args.retry_backoff_seconds,
        max_consecutive_truncations=args.max_consecutive_truncations, max_total_truncations=args.max_total_truncations,
        summary_every=args.summary_every)
    output = resolve_run_output(args.out, repository_root=ROOT, run_root=args.run_root)
    source = None
    continuation = None
    source_path = getattr(args, "continue_from", None)
    source_digest = getattr(args, "source_events_sha256", None)
    if bool(source_path) != bool(source_digest):
        raise ValueError("continuation requires both source path and approved journal SHA-256")
    if source_path:
        from src.agent_runtime.continuation import ReadOnlyRun, continuation_allowance
        source = ReadOnlyRun(source_path)
        if output == source.directory or output.is_relative_to(source.directory) or source.directory.is_relative_to(output):
            raise ValueError("continuation needs a separate new run directory")
        limits, continuation = continuation_allowance(source, limits, expected_events_sha256=source_digest)
        prior = source.verified_role_configuration()
        for key, expected in {
            "roles": {name: route.model_dump(mode="json") for name, route in routes.items()},
            "global_context": global_context,
            "effective_context": {name: value.model_dump(mode="json") for name, value in role_contexts.items()},
            "max_concurrent_readers": args.max_concurrent_readers,
        }.items():
            if prior.get(key) != expected:
                raise ValueError("continuation must preserve role routing, context, and concurrency")
        from .continuation import verify_domain_compatibility
        verify_domain_compatibility(source, ROOT)
    elif (output / "runtime_continuation.json").is_file():
        raise ValueError("resuming a continuation requires its original --continue-from identity")
    if args.repair_tail and not args.resume:
        raise ValueError("--repair-tail requires --resume")
    if args.provider == "scripted" and args.script is None:
        raise ValueError("scripted role runs require an explicit response fixture")
    guide = get_role_guide("coordinator")
    connections = {}
    def connection_key(provider, model):
        return (provider, model if provider == "opencode-go" else None)
    if args.provider != "scripted":
        for role in routes.values():
            key = connection_key(role.provider, role.model)
            if key not in connections:
                connections[key] = resolve_connection(role.provider, args.credentials_file, model=role.model,
                    **({"chatgpt_auth_dir": getattr(args, "chatgpt_auth_dir", None)}
                       if role.provider == CHATGPT_SUBSCRIPTION else {}))
        validated_models = set()
        for role in routes.values():
            key = connection_key(role.provider, role.model)
            identity = (key, role.model)
            if identity not in validated_models:
                await validate_connection_model(connections[key], role.model)
                validated_models.add(identity)
    connection_routes = {name: (connections[connection_key(role.provider, role.model)].descriptor.model_dump(mode="json")
        if role.provider != "scripted" else {"route_id": "scripted", "billing_mode": "offline",
                                             "adapter_kind": "scripted"})
        for name, role in routes.items()}
    configuration = {"roles": {key: role.model_dump(mode="json") for key, role in routes.items()},
                     "global_context": global_context,
                     "effective_context": {key: value.model_dump(mode="json")
                                           for key, value in role_contexts.items()},
                     "connections": connection_routes,
                     "max_concurrent_readers": args.max_concurrent_readers}
    if unit is not None:
        configuration["reader_unit"] = unit
    if source is not None and source.read_json("role_configuration.json").get("connections") != connection_routes:
        raise ValueError("continuation must preserve the original provider routes")
    if args.resume:
        if json.loads((output / "role_configuration.json").read_bytes()) != configuration:
            raise ValueError("role configuration changed on resume")
        if (output / "guide.txt").read_text(encoding="utf-8") != guide:
            raise ValueError("coordinator guidance changed on resume")
        run, task = output / "bim", (output / "task.txt").read_text(encoding="utf-8")
    else:
        output.mkdir(parents=True, exist_ok=False)
        run, _, task = prepare_inputs(output, images=args.images, mesh=None, building_input=args.building_input,
            scope=args.scope, image_kind="drawings", max_candidates=args.max_candidates,
            floor_plan_images=args.floor_plan_images, started_epoch=time.time(), seconds=limits.seconds)
        (output / "guide.txt").write_text(guide, encoding="utf-8", newline="\n")
    manifest = json.loads((run / "inputs.json").read_bytes())
    parameters = role_parameters(primary)
    scripted = json.loads(args.script.read_bytes()) if args.script else None
    if scripted is not None and not isinstance(scripted, dict):
        raise ValueError("role fixture needs coordinator and tasks response lists")

    def factory(task_id, config, store):
        if adapter_factory is not None:
            return adapter_factory(task_id, config, store)
        if config["provider"] == "scripted":
            responses = scripted["coordinator"] if task_id == "coordinator" else scripted["tasks"][task_id]
            offset = sum(event.payload.event_type == "adapter_request" for event in store.events)
            return ScriptedAdapter(responses[offset:])
        connection = connections[connection_key(config["provider"], config["model"])]
        if config["provider"] == "opencode-go":
            from src.agent_runtime.opencode_go import conversation_id
            return connection.create_adapter(session_id=conversation_id(output, task_id))
        return connection.create_adapter()

    with EventStore(output, run_id=output.name, task_id="coordinator", budget_limit=limits.ledger_limit(),
                    recover_tail=args.repair_tail) as store:
        if not args.resume:
            store.write_json("role_configuration.json", configuration)
        async with AsyncExitStack() as stack:
            if unit is not None:
                client = FrozenBimCatalog(run, repository_root=ROOT)
            else:
                client = await stack.enter_async_context(frozen_bim_client(run_directory=run, repository_root=ROOT))
            base_role = coordinator_role(limits.ledger_limit())
            frozen = FrozenBimTools(client, base_role, run_directory=run)
            tools = RoleSession(store=store, frozen=frozen, routes=routes, adapter_factory=factory,
                limits=limits, root=ROOT, max_concurrent_readers=args.max_concurrent_readers,
                started_epoch=manifest["started_epoch"], reader_fault_hook=reader_fault_hook,
                role_contexts=role_contexts, connection_routes=connection_routes)
            continued_readers = None
            if source is not None:
                if args.resume:
                    saved_continuation = json.loads((output / "runtime_continuation.json").read_bytes())
                    if saved_continuation["allowance"] != continuation:
                        raise ValueError("continuation source or allowance changed on resume")
                    continued_readers = saved_continuation["readers"]
                else:
                    from .continuation import import_reader_evidence
                    continued_readers = import_reader_evidence(source, tools)
                    source.verify_unchanged()
                    body = {"allowance": continuation, "readers": continued_readers,
                            "source_file_hashes": source.evidence_hashes}
                    store.write_json("runtime_continuation.json", body)
            catalog = await tools.list_tools()
            specs = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                       "parameters": t["inputSchema"]}} for t in catalog]
            role = base_role.model_copy(update={"tool_whitelist": tuple(
                ToolGrant(tool_name=tool["name"], access="read" if tools.repeatability(tool["name"]) == "read_only" else "write")
                for tool in catalog)})
            route = {**connection_routes["coordinator"], "model": primary["model"],
                     "context": coordinator_context.model_dump(mode="json"), "roles": configuration}
            if unit is not None:
                route["parent_catalog_source"] = client.definition_source
            if continued_readers is not None:
                route["reader_continuation"] = {"manifest_sha256": hashlib.sha256(
                    (output / "reader_continuation.json").read_bytes()).hexdigest()}
            if args.script:
                route["fixture_sha256"] = hashlib.sha256(args.script.read_bytes()).hexdigest()
            versions = make_versions(store, root=ROOT, prompt=guide, tools=specs, parameters=parameters,
                route=route, code_paths=("src/agent/runtime_roles", "src/agent/runtime_tools.py", "src/agent/runtime_context.py",
                                        "src/agent/runtime_delivery.py", "src/agent/bim_inputs.py", "scripts/tool_scripts", "src/agent/geometry"))
            if args.resume:
                if source is not None and any(event.payload.event_type == "adapter_request" for event in store.events):
                    from src.agent_runtime.continuation import ReadOnlyRun
                    if ReadOnlyRun(output).verified_versions() != versions.model_dump(mode="json"):
                        raise ValueError("continuation identity changed; refusing recovery before any work")
                saved = store.latest_checkpoint()
                if saved and saved[1]["versions"] != versions.model_dump(mode="json"):
                    raise ValueError("runtime implementation or roles changed; refusing recovery before any work")
            else:
                store.write_json("versions.json", versions.model_dump(mode="json"))
                write_frozen_materials(output / "frozen", repository_root=ROOT)
                write_frozen_tool_catalog(output / "frozen", await client.list_tools(), readonly=False,
                    **({"definition_source": client.definition_source} if unit is not None else {}))
            if unit is not None:
                if args.resume and json.loads((output / "versions.json").read_bytes()) != versions.model_dump(mode="json"):
                    raise ValueError("reader unit implementation changed; refusing recovery")
                # No delegate_many: its whole-case auto-fill must never turn a
                # selected unit experiment into a six-image run.
                admitted = tools._task(unit)
                record = await tools.run_reader(admitted)
                accounting = role_accounting(store, tools.registry)
                child = tools.registry.child(unit["task_id"])
                child_receipt = child.task_directory / "receipt.json"
                runtime = json.loads(child_receipt.read_bytes()) if child_receipt.is_file() else {}
                failed_status = runtime.get("status", "failed")
                result = {"status": "completed" if record["status"] == "completed" else
                          (failed_status if failed_status != "completed" else "reader_completion_unmet"),
                    "execution_scope": "reader_unit", "whole_building_delivered": False,
                    "reader": record, "elapsed_seconds": time.time() - manifest["started_epoch"],
                    "role_accounting": accounting}
                store.write_json("role_accounting.json", accounting)
                store.write_json("role_state.json", tools.state())
                store.write_json("receipt.json", result)
                store.write_json("bim/summary.json", {
                    "runtime_mode": "role_division", "execution_scope": "reader_unit",
                    "runtime_status": result["status"], "delivery": None,
                    "reader_task_id": unit["task_id"], "receipt": "../receipt.json"})
                return result
            adapter = factory("coordinator", primary, store)
            if hasattr(adapter, "close"):
                stack.push_async_callback(adapter.close)
            engine = Runtime(store=store, adapter=adapter, tools=tools, role=role, model=primary["model"],
                parameters=parameters, versions=versions, limits=limits,
                context_policy=context_policy_from_effective("coordinator", coordinator_context),
                context_update=update_role_context, root_tool_calls=limits.tool_calls,
                start_epoch=manifest["started_epoch"], finalize_run=finalize_role_building,
                domain_completion=completed_building_handoff,
                tool_budget_update=publish_tool_budget, strict_model_profile=primary["provider"] in LIVE_PROVIDERS,
                low_output_limit_reason=primary.get("low_output_limit_reason"), fault_hook=fault_hook)
            if args.resume:
                saved = store.latest_checkpoint()
                if saved and saved[1].get("config", engine._config()) != engine._config():
                    raise ValueError("runtime configuration changed; refusing recovery before any work")
                await tools.recover_pending()
            content = task
            if manifest.get("building_input"):
                content += "\nUser-supplied declaration: " + json.dumps(manifest["building_input"], ensure_ascii=False)
            if continued_readers is not None:
                content += (
                    "\nThis is an explicitly authorized continuation in a new run, using verified reader evidence. "
                    "The previous run is immutable. Original service failures and usage remain recorded; "
                    "the available allowance already deducts prior consumption. This is not a replay of a failed request. "
                    "Inspect role_state for imported deliveries and draft handoffs. Reuse completed deliveries. "
                    "Continue an unsubmitted draft using a NEW task_id and its previous_task_id, reread and retrial it "
                    "in the new task, then submit explicitly. A passed draft is not a formal delivery. "
                    "Complete all remaining admitted floor plans, assemble the complete building, and deliver its viewer. "
                    "Imported reader summary: " + json.dumps(continued_readers, ensure_ascii=False)
                )
                from src.agent_runtime.continuation import claim_continuation
                source.verify_unchanged()
                store.write_json("continuation_claim.json", claim_continuation(source, output))
            result = await engine.run([{"role": "system", "content": guide}, {"role": "user", "content": content}], resume=args.resume)
            accounting = role_accounting(store, tools.registry)
            store.write_json("role_accounting.json", accounting)
            store.write_json("role_state.json", tools.state())
            result["role_accounting"] = accounting
            if source is not None:
                result["continuation"] = {
                    "source_run_id": source.run_id,
                    "source_events_sha256": source_digest,
                    "allowance": continuation,
                    "current_run_usage_only": True,
                    "combined_usage_requires_source_receipts": True,
                }
            store.write_json("receipt.json", result)
            from src.agent.bim_inputs import dump
            delivery = result.get("finalization", {}).get("delivery")
            dump(run / "summary.json", {
                "input_mode": manifest.get("input_mode"), "source_input_mode": manifest.get("source_input_mode"),
                "input_contents": manifest.get("input_contents"),
                "agent_response_completed": result["status"] == "completed", "runtime_status": result["status"],
                "elapsed_seconds": result.get("elapsed_seconds"),
                "delivery": ({**delivery, "report": "delivery.json", "viewer": "delivery.html"} if delivery else None),
                "receipt": "../receipt.json", "drawing_fidelity": "not_evaluated", "runtime_mode": "role_division"})
    from src.agent.runtime_behaviour import write_behaviour_report
    write_behaviour_report(output / "events.jsonl", output / "behaviour")
    return result


def main():
    args = parser().parse_args()
    result = asyncio.run(execute(args))
    print(json.dumps({"status": result["status"], "usage": result["role_accounting"]}, ensure_ascii=False))
    raise SystemExit(0 if result["status"] == "completed" else 1)


if __name__ == "__main__":
    main()
