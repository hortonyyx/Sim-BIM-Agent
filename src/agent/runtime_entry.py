"""Run one role on admitted case inputs using the frozen BIM tool service.

CLI: python -m src.agent.runtime_entry --help
Real requests require an explicit live provider. Tool preparation never starts
the historical subscription runner.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import time
from decimal import Decimal
from pathlib import Path

from scripts.tool_scripts.bim_agent_guidance import build_guide
from src.agent.runtime_tools import (
    FrozenBimTools, coordinator_role, local_observer_role, frozen_bim_client,
    write_frozen_materials, write_frozen_tool_catalog,
)
from src.agent.runtime_context import update_building_context
from src.agent.runtime_delivery import completed_building_handoff, finalize_runtime_building
from src.agent_runtime.adapter import HttpChatAdapter, ScriptedAdapter
from src.agent_runtime.budget import PriceSchedule
from src.agent_runtime.accounting import require_cny_price_schedule
from src.agent_runtime.context import ContextPolicy, context_image_tool_spec
from src.agent_runtime.estimation import get_model_profile
from src.agent_runtime.output_limits import default_output_tokens, validate_output_limit
from src.agent_runtime.providers import (CHATGPT_SUBSCRIPTION, SUBSCRIPTION_PROVIDERS, LIVE_PROVIDERS,
    provider_parameters, validate_provider_model)
from src.agent_runtime.connections import paratera_credentials, resolve_connection, validate_connection_model
from src.agent_runtime.loop import RunLimits, Runtime
from src.agent_runtime.store import EventStore
from src.agent_runtime.versions import make_versions
from src.agent_runtime.run_paths import resolve_run_output


ROOT = Path(__file__).resolve().parents[2]


def runtime_model_profile(provider: str, model: str):
    """Select the compatibility or reviewed profile for this CLI route."""
    validate_provider_model(provider, model)
    strict = provider in LIVE_PROVIDERS
    return get_model_profile(model if strict else "scripted-model", strict=strict)


def prepare_inputs(output: Path, *, images: Path | None, mesh: Path | None,
                   building_input: Path | None, scope: str, image_kind: str,
                   max_candidates: int, floor_plan_images: list[str] | None = None,
                   started_epoch: float | None = None,
                   seconds: float | None = None, process_reviews: bool = False) -> tuple[Path, str, str]:
    """Freeze originals through the same admission function as Claude Code."""
    from src.agent.bim_inputs import prepare_bim_inputs
    if started_epoch is None and seconds is not None:
        raise ValueError("seconds requires started_epoch")
    run = output / "bim"
    manifest = prepare_bim_inputs(run, images_path=images, mesh_path=mesh,
        building_input_path=building_input, scope=scope, image_kind=image_kind,
        max_candidates=max_candidates, floor_images=floor_plan_images,
        started_epoch=started_epoch, seconds=seconds)
    inventory = manifest["images"]
    if process_reviews:
        if image_kind != "drawings" or mesh or not inventory:
            raise ValueError("process reviews require drawing images")
        from scripts.tool_scripts.bim_agent_process_reviews import POLICY
        manifest["drawing_review_policy"] = POLICY
        (run / "inputs.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    guide = build_guide(images=image_kind if inventory else None, mesh=bool(mesh))
    guide += ("\nSingle-model runtime completion: finish_bim selects your final handoff. "
        "After the current tool batch completes, a successful finish of a complete building "
        "with matching saved source and viewer ends this run without another model request. "
        "Complete intended edits and checks before finish_bim; partial or failed handoffs "
        "do not end the run. Tool completion is distinct from a final text answer.\n")
    if process_reviews:
        from scripts.tool_scripts.bim_agent_guidance import DRAWING_PROCESS
        guide += "\n" + DRAWING_PROCESS
    task = scope
    (output / "guide.txt").write_text(guide, encoding="utf-8")
    (output / "task.txt").write_text(task, encoding="utf-8")
    return run, guide, task


def publish_tool_budget(engine):
    """Bridge the runtime ledger to the existing, shared tool-tail formatter."""
    run = engine.tools.run_directory.resolve()
    directory = run / ".harness_tmp"
    directory.mkdir(exist_ok=True)
    value = {**engine.remaining_budget_status(), "run_directory": str(run),
             "started_epoch": engine.started_epoch}
    pending = directory / "budget_status.tmp"
    pending.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    pending.replace(directory / "budget_status.json")


async def execute(args) -> dict:
    continue_from = getattr(args, "continue_from", None)
    if continue_from and args.resume:
        raise ValueError("--continue-from starts a new run and cannot be combined with --resume")
    if bool(continue_from) != bool(getattr(args, "source_events_sha256", None)):
        raise ValueError("--continue-from and --source-events-sha256 are required together")
    if getattr(args, "allow_recovery_policy_change", False) and not continue_from:
        raise ValueError("--allow-recovery-policy-change requires --continue-from")
    mesh_workers = getattr(args, "mesh_workers", 0)
    worker_script = getattr(args, "mesh_worker_script", None)
    if (type(mesh_workers) is not int or mesh_workers < 0
            or (mesh_workers and (not (args.mesh or continue_from or args.resume) or args.role != "coordinator"))):
        raise ValueError("mesh workers require native mesh coordinator input and positive concurrency")
    if worker_script and (not mesh_workers or args.provider != "scripted"):
        raise ValueError("mesh-worker-script requires enabled mesh workers and the scripted provider")
    if mesh_workers and args.provider == "scripted" and worker_script is None:
        raise ValueError("scripted mesh workers require an explicit task-to-responses fixture")
    worker_fixture = worker_script.read_bytes() if worker_script else None
    worker_responses = json.loads(worker_fixture) if worker_fixture else None
    if worker_responses is not None and (not isinstance(worker_responses, dict)
            or any(not isinstance(key, str) or not isinstance(value, list)
                   for key, value in worker_responses.items())):
        raise ValueError("mesh-worker-script must map task IDs to response lists")
    if getattr(args, "image_history_batches", None) is not None and not args.context:
        raise ValueError("--image-history-batches requires --context")
    output = resolve_run_output(args.out, repository_root=ROOT,
                                run_root=getattr(args, "run_root", None))
    strict_model_profile = args.provider in LIVE_PROVIDERS
    # Reject an unreviewed real route before creating a run or reading credentials.
    runtime_model_profile(args.provider, args.model)
    effective_model = args.model if strict_model_profile else "scripted-model"
    args.output_tokens = args.output_tokens if args.output_tokens is not None else default_output_tokens(effective_model)
    if args.provider == "opencode-go":
        from src.agent_runtime.opencode_go import model_contract
        if model_contract(args.model)[0] == "openai_responses" and args.reasoning_history != "all":
            raise ValueError("OpenCode Go Responses requires reasoning_history=all")
    validate_output_limit(effective_model, args.output_tokens, reason=args.low_output_limit_reason)
    parameters = provider_parameters(args.provider, output_tokens=args.output_tokens,
        temperature=args.temperature, thinking=args.thinking, reasoning_effort=args.reasoning_effort,
        model=args.model)
    if args.provider in SUBSCRIPTION_PROVIDERS and (args.price_schedule or args.money_usd is not None or args.money_cny is not None):
        raise ValueError("subscription route has no usage-based money estimate; use token/time budgets")
    if args.money_cny is not None:
        require_cny_price_schedule(effective_model, route_id=args.provider)
    limits = RunLimits(model_calls=args.model_calls, tool_calls=args.tool_calls,
        seconds=args.seconds, tokens=args.tokens, money_usd=args.money_usd, money_cny=args.money_cny,
        time_budget_mode=getattr(args, "time_budget_mode", "wall_and_requests"),
        near_limit=args.near_limit, min_output_tokens=args.min_output_tokens,
        context_tokens=args.context_tokens, max_model_retries=args.model_retries,
        retry_backoff_seconds=args.retry_backoff_seconds,
        model_reconnect_seconds=getattr(args, "model_reconnect_seconds", 0),
        max_consecutive_truncations=args.max_consecutive_truncations,
        max_total_truncations=args.max_total_truncations,
        summary_every=args.summary_every)
    context_values = {"active_window_messages": args.context_window,
        "append_state_history": (args.provider == "opencode-go" and args.model == "claude-haiku-5-5"
            and args.context_window is None),
        "compact_at_tokens": args.compact_at_tokens,
        "large_result_bytes": args.large_result_bytes, "max_images": args.max_images,
        "image_history_batches": getattr(args, "image_history_batches", None),
        "max_image_bytes": args.max_image_bytes}
    context_policy = ContextPolicy(**{key: value for key, value in context_values.items()
        if value is not None}, pinned_tags=tuple(args.pin_tag)) if args.context else None
    pricing = PriceSchedule.model_validate_json(args.price_schedule.read_bytes()) if args.price_schedule else None
    if args.repair_tail and not args.resume:
        raise ValueError("--repair-tail requires --resume")
    if args.provider == "scripted" and args.script is None:
        raise ValueError("--provider scripted requires --script")
    continuation = None
    imported_source = None
    if continue_from:
        from src.agent.runtime_mesh_continuation import inspect_mesh_continuation
        continuation = inspect_mesh_continuation(continue_from, output, limits, root=ROOT,
            expected_events_sha256=args.source_events_sha256,
            allow_recovery_policy_change=getattr(args, "allow_recovery_policy_change", False))
        limits = continuation.limits
        imported_source = continuation.source
    elif args.resume and (output / "continuation_manifest.json").is_file():
        from src.agent_runtime.continuation import ReadOnlyRun
        from src.agent.runtime_mesh_continuation import continuation_lineage, tree_hashes
        saved_import = json.loads((output / "continuation_manifest.json").read_bytes())
        imported_source = ReadOnlyRun(saved_import["source_run"])
        if imported_source.evidence_hashes["events.jsonl"] != saved_import["source_events_sha256"]:
            raise ValueError("resumed continuation source journal changed")
        if (tree_hashes(imported_source.directory) != saved_import["source_tree_hashes"]
                or continuation_lineage(imported_source) != saved_import["source_execution_lineage"]):
            raise ValueError("resumed continuation source or ancestry changed")
        saved_requested = RunLimits.model_validate(saved_import["requested_limits"])
        saved_effective = RunLimits.model_validate(saved_import["effective_limits"])
        if limits not in (saved_requested, saved_effective):
            raise ValueError("runtime configuration changed; continuation resume must retain its saved limits")
        limits = saved_effective
    connection = None
    if args.provider == CHATGPT_SUBSCRIPTION:
        if args.reasoning_history != "all":
            raise ValueError("ChatGPT subscription requires --reasoning-history all")
        if args.near_limit != "stop":
            raise ValueError("ChatGPT subscription cannot enforce a reduced output cap; use --near-limit stop")
        connection = resolve_connection(args.provider, args.credentials_file,
            chatgpt_auth_dir=getattr(args, "chatgpt_auth_dir", None))
        await validate_connection_model(connection, args.model)
    if continuation:
        started_epoch = time.time()
        continuation.copy_workspace(started_epoch=started_epoch)
        run, guide, task = output / "bim", continuation.guide, continuation.task
    elif args.resume:
        run = output / "bim"
        guide, task = (output / "guide.txt").read_text(), (output / "task.txt").read_text()
    else:
        output.mkdir(parents=True, exist_ok=False)
        started_epoch = time.time()
        run, guide, task = prepare_inputs(output, images=args.images, mesh=args.mesh,
            building_input=args.building_input, scope=args.scope, image_kind=args.image_kind,
            max_candidates=args.max_candidates, floor_plan_images=args.floor_plan_images,
            started_epoch=started_epoch, seconds=limits.seconds,
            process_reviews=getattr(args, "process_reviews", False))
        if mesh_workers:
            from scripts.tool_scripts.bim_agent_guidance import MESH_WORKERS
            guide += "\n" + MESH_WORKERS
            (output / "guide.txt").write_text(guide, encoding="utf-8")
    manifest = json.loads((run / "inputs.json").read_bytes())
    started_epoch = manifest.get("started_epoch")
    role = (local_observer_role if args.role == "local_observer" else coordinator_role)(limits.ledger_limit())
    adapter = None
    try:
        with EventStore(output, run_id=output.name, task_id=args.role,
                budget_limit=limits.ledger_limit(), recover_tail=args.repair_tail) as store:
            async with frozen_bim_client(run_directory=run, readonly=role.read_only, repository_root=ROOT) as client:
                tools = FrozenBimTools(client, role, run_directory=run)
                if args.provider == "scripted":
                    fixture = args.script.read_bytes()
                    # The local fixture gives one response per request ticket. A
                    # resumed unknown ticket consumes its position, never replays it.
                    offset = sum(e.payload.event_type == "adapter_request" for e in store.events)
                    adapter = ScriptedAdapter(json.loads(fixture)[offset:])
                    route = {"route_id": "offline-scripted", "model": "scripted-model",
                        "fixture_sha256": hashlib.sha256(fixture).hexdigest()}
                else:
                    connection = connection or resolve_connection(args.provider, args.credentials_file, model=args.model)
                    if args.provider == "opencode-go":
                        from src.agent_runtime.opencode_go import conversation_id
                        adapter = connection.create_adapter(session_id=conversation_id(output, args.role))
                    else:
                        adapter = connection.create_adapter()
                    route = connection.descriptor.model_route(args.model)
                route["reasoning_history"] = args.reasoning_history
                route["context"] = ({"context_tokens": limits.context_tokens,
                    **context_policy.model_dump(mode="json")} if context_policy is not None
                    else {"enabled": False, "context_tokens": limits.context_tokens})
                if mesh_workers:
                    from src.agent.runtime_mesh_workers import MeshWorkerTools, EXTRA_TOOLS
                    from src.harness_contracts.roles import ToolGrant
                    route["mesh_workers"] = {"max_concurrent": mesh_workers,
                        "model_policy": "same_route_and_parameters_as_coordinator",
                        "fixture_sha256": hashlib.sha256(worker_fixture).hexdigest() if worker_fixture else None}

                    def worker_adapter(task_id, child):
                        if args.provider == "scripted":
                            offset = sum(e.payload.event_type == "adapter_request" for e in child.events)
                            if task_id not in worker_responses:
                                raise ValueError("no scripted responses for mesh worker " + task_id)
                            return ScriptedAdapter(worker_responses[task_id][offset:])
                        if args.provider == "opencode-go":
                            from src.agent_runtime.opencode_go import conversation_id
                            return connection.create_adapter(session_id=conversation_id(output, task_id))
                        return connection.create_adapter()

                    worker_type = MeshWorkerTools
                    if imported_source is not None:
                        from src.agent.runtime_mesh_continuation import ContinuedMeshWorkers
                        worker_type = ContinuedMeshWorkers
                    tools = worker_type(tools, store=store, root=ROOT,
                        adapter_factory=worker_adapter, model=route["model"], parameters=parameters,
                        route=route, limits=limits, start_epoch=started_epoch,
                        pricing=pricing,
                        max_concurrent=mesh_workers, reasoning_history=args.reasoning_history,
                        low_output_limit_reason=args.low_output_limit_reason)
                    if imported_source is not None:
                        from src.agent.runtime_mesh_continuation import source_worker_readers
                        tools.import_workers(source_worker_readers(imported_source, ROOT))
                    role = role.model_copy(update={"tool_whitelist": (*role.tool_whitelist,
                        *(ToolGrant(tool_name=t["name"], access="read") for t in EXTRA_TOOLS))})
                catalog = await tools.list_tools()
                if not args.resume:
                    write_frozen_materials(output / "frozen", repository_root=ROOT)
                    write_frozen_tool_catalog(output / "frozen", await client.list_tools(), readonly=role.read_only)
                version_catalog = list(catalog)
                if context_policy is not None and context_policy.image_history_batches is not None:
                    from src.harness_contracts.roles import ToolGrant
                    context_spec = context_image_tool_spec()
                    version_catalog.append(context_spec)
                    # Compare the final role before either recovery path; the
                    # Runtime enables image lookup for non-worker runs too.
                    role = role.model_copy(update={"tool_whitelist": (*role.tool_whitelist,
                        ToolGrant(tool_name=context_spec["name"], access="read"))})
                specs = [{"type": "function", "function": {"name": t["name"],
                    "description": t.get("description", ""), "parameters": t["inputSchema"]}} for t in version_catalog]
                versions = make_versions(store, root=ROOT, prompt=guide, tools=specs,
                    parameters=parameters, route=route, code_paths=("src/agent/runtime_entry.py",
                        "src/agent/runtime_delivery.py", "src/agent/bim_inputs.py",
                        "src/agent/runtime_tools.py", "scripts/tool_scripts", "src/agent/geometry",
                        "src/agent/runtime_context.py", "src/agent/runtime_behaviour.py",
                        "src/agent/runtime_mesh_workers.py", "src/agent/runtime_mesh_evidence.py",
                        "src/agent/runtime_mesh_continuation.py",
                        "src/agent/correction", "src/agent/execution"))
                # A refused resume must not replace the original run's evidence.
                # The runtime checks current versions against its saved checkpoint.
                if not args.resume:
                    store.write_json("versions.json", versions.model_dump(mode="json"))
                originals, user_content = {}, [{"type": "text", "text": task}]
                # Keep admitted user declarations in the initial requirement
                # record, so a later activity window cannot discard constraints
                # that were otherwise visible only in the inputs tool reply.
                admitted = json.loads((run / "inputs.json").read_bytes()).get("building_input")
                if admitted:
                    user_content.append({"type": "text", "text":
                        "Additional user-supplied building declaration; not independently verified: "
                        + json.dumps({key: admitted[key] for key in ("raw_sha256", "declaration",
                            "field_semantics", "conflict_policy")}, ensure_ascii=False)})
                for name in args.attach_image:
                    if Path(name).name != name:
                        raise ValueError("--attach-image needs an admitted filename")
                    image_path = run / "images" / name
                    raw = image_path.read_bytes()
                    ref = store.put_bytes(raw, "image/png")
                    originals[ref.sha256] = ref
                    user_content += [{"type": "text", "text": f"Input image: {name}"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(raw).decode()}}]
                messages = [{"role": "system", "content": guide},
                    {"role": "user", "content": user_content if len(user_content) > 1 else task}]
                engine_type = Runtime
                if continuation:
                    from src.agent.runtime_mesh_continuation import ContinuedRuntime
                    engine_type = ContinuedRuntime
                inherited_reconnect = (continuation.record["reconnect_consumed_before_seconds"]
                    if continuation else saved_import["reconnect_consumed_before_seconds"]
                    if imported_source is not None else 0)
                engine = engine_type(store=store, adapter=adapter, tools=tools, role=role,
                    model=route["model"], parameters=parameters, versions=versions, limits=limits,
                    context_policy=context_policy, pricing=pricing,
                    reasoning_history=args.reasoning_history,
                    context_update=update_building_context,
                    required_view_ids=tuple(args.keep_view_id),
                    retrieve_images=tuple(tuple(pair) for pair in args.retrieve_image),
                    start_epoch=started_epoch,
                    finalize_run=finalize_runtime_building,
                    domain_completion=completed_building_handoff if not role.read_only else None,
                    tool_budget_update=publish_tool_budget,
                    root_tool_calls=limits.tool_calls if mesh_workers else None,
                    reconnect_consumed_before_seconds=inherited_reconnect,
                    strict_model_profile=strict_model_profile)
                engine.low_output_limit_reason = args.low_output_limit_reason
                if continuation:
                    continuation.validate_runtime(engine)
                    engine.context_import = continuation.context_import
                    store.write_json("continuation_manifest.json", continuation.record)
                elif imported_source is not None:
                    from src.agent_runtime.continuation import claim_continuation
                    saved_checkpoint = store.latest_checkpoint()
                    if not saved_checkpoint:
                        raise ValueError("continuation import did not reach a checkpoint; no allowance was claimed; create a new continuation destination")
                    if (saved_checkpoint[1]["versions"] != versions.model_dump(mode="json")
                            or saved_checkpoint[1].get("config", engine._config()) != engine._config()):
                        raise ValueError("runtime configuration changed; refusing continuation resume")
                    store.write_json("continuation_claim.json", claim_continuation(imported_source, output))
                if mesh_workers and args.resume:
                    saved = store.latest_checkpoint()
                    if saved and (saved[1]["versions"] != versions.model_dump(mode="json") or
                            saved[1].get("config", engine._config()) != engine._config()):
                        raise ValueError("runtime configuration changed; refusing worker recovery before any work")
                    await tools.recover_pending()
                result = await engine.run([] if continuation else messages,
                    image_originals={} if continuation else originals, resume=args.resume)
                if imported_source is not None:
                    from src.agent.runtime_mesh_continuation import continuation_receipt, tree_hashes
                    record = continuation.record if continuation else saved_import
                    if tree_hashes(imported_source.directory) != record["source_tree_hashes"]:
                        raise ValueError("immutable continuation source tree changed")
                    result["continuation"] = continuation_receipt(record, result)
                    store.write_json("receipt.json", result)
                if mesh_workers:
                    # Runtime owns all spending and stop reasons. This domain
                    # projection only makes parent/worker call totals explicit.
                    state = tools.state()
                    counts = {}
                    for event in store.all_events:
                        row = counts.setdefault(event.task_id, {"model_requests": 0, "tool_calls": 0})
                        if event.payload.event_type == "adapter_request":
                            row["model_requests"] += 1
                        elif event.payload.event_type == "tool_invocation":
                            row["tool_calls"] += 1
                    result["mesh_workers"] = state
                    result["whole_run_counts"] = {
                        "model_requests": sum(row["model_requests"] for row in counts.values()),
                        "tool_calls": sum(row["tool_calls"] for row in counts.values()),
                        "by_task": counts,
                        "scope": "all journaled attempts, including recovery; usage is root_usage_accounting",
                    }
                    store.write_json("mesh_worker_state.json", state)
                    store.write_json("receipt.json", result)
        # A normal BIM workspace is directly scoreable by either runner's evaluator.
        # The runtime receipt remains the authority for stop reason and accounting.
        from src.agent.bim_inputs import dump
        delivery = result.get("finalization", {}).get("delivery")
        task_completed = (result["status"] == "completed" and bool(result.get("completion"))
            and result.get("finalization", {}).get("completion_valid") is True
            and isinstance(delivery, dict) and delivery.get("complete_building") is True)
        dump(run / "summary.json", {
            "input_mode": manifest.get("input_mode"), "source_input_mode": manifest.get("source_input_mode"),
            "input_contents": manifest.get("input_contents"),
            "agent_response_completed": result["status"] == "completed" and not result.get("completion"),
            "task_completed": task_completed,
            "completion_origin": "domain_tool" if task_completed else None,
            "runtime_status": result["status"], "elapsed_seconds": result.get("elapsed_seconds"),
            "delivery": ({**delivery, "report": "delivery.json", "viewer": "delivery.html"}
                         if delivery else None),
            "receipt": "../receipt.json", "drawing_fidelity": "not_evaluated"})
        from src.agent.runtime_behaviour import write_behaviour_report
        write_behaviour_report(output / "events.jsonl", output / "behaviour")
        return result
    finally:
        if isinstance(adapter, HttpChatAdapter):
            await adapter.close()


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--run-root", type=Path,
                   help="explicit absolute run-storage root; defaults to this worktree")
    p.add_argument("--images", type=Path)
    p.add_argument("--floor-plan-image", dest="floor_plan_images", action="append",
                   help="Explicit expected floor-plan filename; repeat for all floors (same as Claude Code)")
    p.add_argument("--mesh", type=Path)
    p.add_argument("--mesh-workers", type=int, default=0, metavar="MAX_CONCURRENT",
                   help="enable optional same-route read-only mesh workers under the shared run budget")
    p.add_argument("--mesh-worker-script", type=Path,
                   help="offline only: JSON mapping worker task IDs to scripted response lists")
    p.add_argument("--building-input", type=Path)
    p.add_argument("--image-kind", choices=("drawings", "mesh_views", "photos", "unknown"), default="drawings")
    p.add_argument("--process-reviews", action="store_true",
                   help="require local drawing reviews and a separate architectural judgment before complete delivery")
    p.add_argument("--scope", default="Build the supplied building at the requested detail; inspect saved results and report limitations.")
    p.add_argument("--role", choices=("coordinator", "local_observer"), default="coordinator")
    p.add_argument("--provider", choices=("scripted", *LIVE_PROVIDERS), required=True)
    p.add_argument("--script", type=Path, help="explicit offline Chat Completions response fixture")
    p.add_argument("--model", default="Qwen3.8-27B")
    p.add_argument("--reasoning-history", choices=("all", "current_tool_chain"), default="all",
                   help="OpenAI-compatible history policy; Anthropic messages are unchanged")
    p.add_argument("--credentials-file", type=Path, help="read only this provider credentials file; required for GLM subscription")
    p.add_argument("--chatgpt-auth-dir", type=Path,
                   help="independent ChatGPT subscription login directory; defaults to the app's private user directory")
    p.add_argument("--model-calls", type=int, default=6)
    p.add_argument("--tool-calls", type=int, default=12)
    time_limit = p.add_mutually_exclusive_group()
    time_limit.add_argument("--seconds", type=float, default=180.0)
    time_limit.add_argument("--no-time-limit", dest="seconds", action="store_const", const=None,
                            help="disable the overall deadline; retain finite network I/O waits and other budgets")
    p.add_argument("--time-budget-mode", choices=("wall_and_requests", "wall"), default="wall_and_requests",
                   help="wall keeps the elapsed deadline without a cumulative request-time ceiling; default also caps summed request time")
    token_limit = p.add_mutually_exclusive_group()
    token_limit.add_argument("--tokens", type=int, default=5_000_000)
    token_limit.add_argument("--no-token-limit", dest="tokens", action="store_const", const=None,
                             help="explicitly disable only the total token ceiling")
    p.add_argument("--money-cny", type=Decimal,
                   help="CNY estimate ceiling using registered Paratera rates; not a provider bill")
    p.add_argument("--money-usd", type=Decimal, help="total estimate ceiling; needs a sourced price schedule")
    p.add_argument("--price-schedule", type=Path, help="PriceSchedule JSON; estimates are not provider bills")
    p.add_argument("--near-limit", choices=("stop", "reduce_output"), default="stop")
    p.add_argument("--min-output-tokens", type=int, default=1)
    p.add_argument("--model-retries", type=int, default=2)
    p.add_argument("--retry-backoff-seconds", type=float, default=1.0)
    p.add_argument("--model-reconnect-seconds", type=float, default=0,
                   help="bounded network recovery allowance; zero retains count-only retries")
    p.add_argument("--max-consecutive-truncations", type=int, default=2,
                   help="maximum consecutive output-limit recoveries; zero disables recovery")
    p.add_argument("--max-total-truncations", type=int, default=3,
                   help="maximum output-limit recoveries across this root run, including children and summaries")
    p.add_argument("--context", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--context-tokens", type=int, help="optional explicit local capacity; omit to adapt to the model window with recoverable context compaction")
    p.add_argument("--context-window", type=int, help="explicit legacy message-window replay; production defaults to token compaction")
    p.add_argument("--compact-at-tokens", type=int,
                   help="optional explicit compaction threshold; omit for adaptive model-window pressure management")
    p.add_argument("--large-result-bytes", type=int)
    p.add_argument("--max-images", type=int, help="optional image count target at compaction; unset by default")
    p.add_argument("--image-history-batches", type=int,
                   help="retain recent tool-image batches and exact references to older images; initial and undelivered images stay visible")
    p.add_argument("--max-image-bytes", type=int)
    p.add_argument("--pin-tag", action="append", default=[])
    p.add_argument("--keep-view-id", action="append", default=[])
    p.add_argument("--retrieve-image", nargs=2, action="append", default=[], metavar=("VIEW_ID", "SHA256"))
    p.add_argument("--summary-every", type=int, default=0, help="optional constrained model summary after N tool calls; shares the root budget")
    p.add_argument("--output-tokens", type=int, help="reviewed output cap; ChatGPT subscription: local reservation allowance, not a service-enforced cap")
    p.add_argument("--low-output-limit-reason", help="explicit reason for an output cap below the recommendation")
    p.add_argument("--temperature", type=float, help="default: Paratera 0.0; subscription service default")
    p.add_argument("--thinking", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--reasoning-effort", choices=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
                   help="provider-native reasoning level; when set, omit enable_thinking")
    p.add_argument("--max-candidates", type=int, default=4)
    p.add_argument("--attach-image", action="append", default=[])
    p.add_argument("--resume", action="store_true")
    p.add_argument("--continue-from", type=Path,
                   help="continue saved mesh work in this new run without cold input preparation")
    p.add_argument("--source-events-sha256",
                   help="approved immutable events.jsonl identity of --continue-from")
    p.add_argument("--allow-recovery-policy-change", action="store_true",
                   help="explicitly permit recovery settings to change; remaining execution budgets stay conserved")
    p.add_argument("--repair-tail", action="store_true", help="explicitly preserve and repair a torn final event during resume")
    return p


def main():
    args = parser().parse_args()
    result = asyncio.run(execute(args))
    print(json.dumps({key: result[key] for key in ("status", "model_calls", "tool_calls", "reported_tokens", "billing_usd")}, ensure_ascii=False))
    raise SystemExit(0 if result["status"] in {"completed", "model_budget_exhausted", "tool_budget_exhausted", "time_budget_exhausted", "token_budget_exhausted", "money_budget_exhausted", "model_profile_context_limit_exhausted", "configured_context_limit_exhausted"} else 1)


if __name__ == "__main__":
    main()
