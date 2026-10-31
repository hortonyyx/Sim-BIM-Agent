"""Validate or start an approved whole-case configuration.

This module performs no model call for ``check`` or ``command``.  ``launch``
execs the reviewed runtime entry exactly once and is intended only after the
separate whole-case approval recorded by the project lead.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from src.agent.runtime_entry import ROOT
from src.agent_runtime.estimation import get_model_profile
from src.agent_runtime.output_limits import validate_output_limit
from src.agent_runtime.accounting import require_cny_price_schedule
from src.agent_runtime.loop import RunLimits
from src.agent_runtime.run_paths import resolve_run_output
from src.agent_runtime.providers import CHATGPT_SUBSCRIPTION, SUBSCRIPTION_PROVIDERS, LIVE_PROVIDERS, provider_parameters, validate_provider_model
from src.agent.runtime_roles.config import load_roles


ALLOWED_ENTRYPOINTS = {
    "single_model": "src.agent.runtime_entry",
    "external_coordinator_mcp": "src.agent.runtime_coordinator",
    "role_division": "src.agent.runtime_roles.entry",
}


def mesh_worker_concurrency(case: dict) -> int:
    """Optional same-route observers share the native coordinator's root budget."""
    value = case.get("mesh_workers")
    if value is None:
        return 0
    if (case.get("mode") != "single_model" or case.get("input_kind") != "mesh"
            or not isinstance(value, dict) or set(value) != {"max_concurrent"}
            or type(value["max_concurrent"]) is not int or value["max_concurrent"] <= 0):
        raise ValueError("mesh_workers requires single_model mesh input and positive max_concurrent")
    return value["max_concurrent"]


def context_arguments(case: dict) -> list[str]:
    """Omitted/null values use adaptive capacity, never a made-up large cap."""
    result = []
    for name in ("context_tokens", "compact_at_tokens"):
        value = case.get(name)
        if value is None:
            continue
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer or null for automatic context management")
        result += ["--" + name.replace("_", "-"), str(value)]
    image_batches = case.get("image_history_batches")
    if image_batches is not None:
        if type(image_batches) is not int or image_batches <= 0:
            raise ValueError("image_history_batches must be a positive integer or null")
        if case.get("mode") != "single_model":
            raise ValueError("image_history_batches currently supports single_model only")
        result += ["--image-history-batches", str(image_batches)]
    return result


def _resolve_input_path(case: dict, field: str) -> Path:
    """All explicitly named input assets use the same bounded input root."""
    configured = case.get("input_root")
    root = ROOT.resolve() if configured is None else Path(configured).expanduser()
    if not root.is_absolute():
        raise ValueError("input_root must be an absolute path")
    root = root.resolve()
    if root == Path(root.anchor):
        raise ValueError("input_root must be a dedicated directory, not a filesystem root")
    requested = case.get(field)
    if not isinstance(requested, str) or not requested.strip():
        raise ValueError(f"{field} must be a nonempty input path")
    target = (root / requested).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"{field} escapes the worktree" if configured is None
                         else f"{field} escapes its configured input_root")
    if not target.exists():
        raise ValueError(f"input does not exist: {target}")
    return target


def resolve_case_input(case: dict) -> Path:
    """Resolve one admitted case; an external input root must be explicit."""
    target = _resolve_input_path(case, "input")
    kind = case.get("input_kind")
    if kind == "images":
        from src.agent.bim_inputs import admitted_png_paths
        admitted_png_paths(target)
    elif kind == "mesh":
        if not target.is_file() or target.suffix.lower() != ".glb":
            raise ValueError("mesh input must be a regular .glb file")
    else:
        raise ValueError("input_kind must be images or mesh")
    return target


def resolve_supplemental_inputs(case: dict) -> tuple[Path | None, Path | None]:
    """Resolve explicit auxiliary PNGs and a user declaration, without discovery."""
    images = declaration = None
    if case.get("auxiliary_images") is not None:
        if case.get("mode") != "single_model" or case.get("input_kind") != "mesh":
            raise ValueError("auxiliary_images requires single_model mesh input")
        from src.agent.bim_inputs import admitted_png_paths
        images = _resolve_input_path(case, "auxiliary_images")
        admitted_png_paths(images)
    if case.get("building_input") is not None:
        if case.get("mode") not in {"single_model", "role_division"}:
            raise ValueError("building_input supports single_model and role_division only")
        from scripts.tool_scripts.bim_agent_inputs import read_building_input
        declaration = _resolve_input_path(case, "building_input")
        read_building_input(declaration)
    return images, declaration


def load_configuration(path: Path, *, low_output_limit_reason: str | None = None) -> dict:
    value = json.loads(path.read_bytes())
    if value.get("schema_version") != 1:
        raise ValueError("unsupported run configuration schema")
    if not value.get("approval_required_before_launch"):
        raise ValueError("whole-case configuration must retain its approval gate")
    cases = value.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("configuration needs at least one case")
    identities = [case.get("case_id") for case in cases]
    if len(set(identities)) != len(identities) or not all(identities):
        raise ValueError("case IDs must be present and unique")
    for case in cases:
        for name in ("input_root", "run_root"):
            if name not in case and value.get(name) is not None:
                case[name] = value[name]
        mode = case.get("mode")
        if mode not in ALLOWED_ENTRYPOINTS:
            raise ValueError(f"unsupported run mode {mode!r}")
        mesh_worker_concurrency(case)
        if type(case.get("process_reviews", False)) is not bool:
            raise ValueError("process_reviews must be a boolean")
        if case.get("process_reviews") and (mode != "single_model" or case.get("image_kind") != "drawings"
                                            or case.get("input_kind") != "images"):
            raise ValueError("process_reviews currently requires single_model drawings")
        continuation = case.get("continuation")
        if case.get("reader_task") is not None:
            if mode != "role_division" or continuation is not None:
                raise ValueError("reader_task requires role_division without whole-run continuation")
            import jsonschema
            from src.agent.runtime_roles.session import TASK_SCHEMA
            jsonschema.validate(case["reader_task"], TASK_SCHEMA)
        if continuation is not None:
            import re
            mesh_continuation = mode == "single_model" and case.get("input_kind") == "mesh"
            allowed_fields = {"source_run", "source_events_sha256"}
            if mesh_continuation:
                allowed_fields.add("allow_recovery_policy_change")
            if (mode != "role_division" and not mesh_continuation
                    or not isinstance(continuation, dict)
                    or not {"source_run", "source_events_sha256"} <= set(continuation) <= allowed_fields
                    or not isinstance(continuation["source_run"], str)
                    or not Path(continuation["source_run"]).is_absolute()
                    or not isinstance(continuation["source_events_sha256"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", continuation["source_events_sha256"]) is None
                    or type(continuation.get("allow_recovery_policy_change", False)) is not bool):
                raise ValueError("continuation needs an absolute source_run and its exact journal SHA-256 in role_division or single_model mesh")
        context_arguments(case)
        role_configurations = None
        if mode == "role_division":
            if case.get("input_kind") != "images" or case.get("image_kind") != "drawings":
                raise ValueError("role_division supports drawing images only; mesh and non-drawing inputs are not admitted")
            role_configurations = load_roles(case.get("roles"))
            concurrency = case.get("max_concurrent_readers", 8)
            if type(concurrency) is not int or concurrency <= 0:
                raise ValueError("max_concurrent_readers must be a positive integer")
        if case.get("reasoning_history", "all") not in {"all", "current_tool_chain"}:
            raise ValueError("unsupported reasoning_history")
        if mode != "single_model" and case.get("reasoning_history", "all") != "all":
            raise ValueError("reasoning_history currently supports single_model only")
        if case.get("provider") not in LIVE_PROVIDERS:
            raise ValueError("live configuration requires a reviewed provider")
        if mode == "external_coordinator_mcp" and case["provider"] == CHATGPT_SUBSCRIPTION:
            raise ValueError("ChatGPT subscription supports single_model and role_division; external_coordinator_mcp is not supported")
        if mode == "external_coordinator_mcp" and case["provider"] == "opencode-go":
            raise ValueError("OpenCode Go supports single_model and role_division only")
        if mode == "external_coordinator_mcp" and case["provider"] == "deepseek":
            raise ValueError("DeepSeek supports single_model and role_division only")
        if case["provider"] == "deepseek" and case.get("reasoning_history", "all") != "all":
            raise ValueError("DeepSeek requires reasoning_history=all")
        if case["provider"] == CHATGPT_SUBSCRIPTION and case.get("reasoning_history", "all") != "all":
            raise ValueError("ChatGPT subscription requires reasoning_history=all")
        model = case.get("model")
        validate_provider_model(case["provider"], model)
        if case["provider"] == "opencode-go":
            from src.agent_runtime.opencode_go import model_contract
            if model_contract(model)[0] == "openai_responses" and case.get("reasoning_history", "all") != "all":
                raise ValueError("OpenCode Go Responses requires reasoning_history=all")
        provider_parameters(case["provider"], model=model, output_tokens=case.get("output_tokens"),
            temperature=case.get("temperature"), thinking=case.get("thinking", True),
            reasoning_effort=case.get("reasoning_effort"))
        get_model_profile(model, strict=True)
        if low_output_limit_reason is not None:
            case["low_output_limit_reason"] = low_output_limit_reason
        validate_output_limit(model, case.get("output_tokens"), reason=case.get("low_output_limit_reason"))
        if role_configurations is not None:
            coordinator = role_configurations["coordinator"]
            coordinator_fields = {
                "provider": coordinator.provider,
                "model": coordinator.model,
                "reasoning_effort": coordinator.reasoning_effort,
                "output_tokens": coordinator.output_tokens,
            }
            mismatched = [name for name, expected in coordinator_fields.items()
                          if case.get(name) != expected]
            if mismatched:
                raise ValueError("top-level coordinator routing must match roles.coordinator: "
                                 + ", ".join(mismatched))
        if "seconds" not in case.get("limits", {}):
            raise ValueError("set seconds explicitly; null disables the single_model overall deadline")
        seconds = case["limits"]["seconds"]
        maximum_seconds = value["maximum_seconds_per_case"]
        if maximum_seconds is not None and (type(maximum_seconds) not in {int, float} or maximum_seconds <= 0):
            raise ValueError("maximum_seconds_per_case must be positive or null")
        if seconds is None:
            if mode != "single_model":
                raise ValueError("null seconds currently supports single_model only")
            if maximum_seconds is not None:
                raise ValueError("unlimited case exceeds the configuration's time ceiling")
        elif type(seconds) not in {int, float} or seconds <= 0:
            raise ValueError("each case needs positive seconds or explicit null")
        elif maximum_seconds is not None and seconds > maximum_seconds:
            raise ValueError("case exceeds the configuration's time ceiling")
        validate_budget(case)
        target = resolve_case_input(case)
        auxiliary_images, _ = resolve_supplemental_inputs(case)
        if case.get("run_root") is not None and mode not in {"single_model", "role_division"}:
            raise ValueError("explicit run_root supports single_model and role_division only")
        if case.get("run_root") is not None:
            case["run_root"] = str(Path(case["run_root"]).expanduser())
        resolve_run_output(ROOT / case["output"] if case.get("run_root") is None else Path(case["output"]), repository_root=ROOT,
                           run_root=case.get("run_root"))
        floors = case.get("floor_plan_images")
        image_directory = target if case["input_kind"] == "images" else auxiliary_images
        admitted_images = {image.name for image in image_directory.glob("*.png")} if image_directory else set()
        if floors is not None and (not isinstance(floors, list) or any(
                not isinstance(name, str) or Path(name).name != name
                or name not in admitted_images for name in floors)):
            raise ValueError("floor_plan_images must use admitted input filenames")
        providers = ({role.provider for role in role_configurations.values()}
                     if role_configurations is not None else {case["provider"]})
        if providers != {CHATGPT_SUBSCRIPTION} and (
                not isinstance(case.get("credentials_file"), str) or not case["credentials_file"].strip()):
            raise ValueError("each case requires an explicit read-only credentials_file")
        if case.get("chatgpt_auth_dir") is not None and (
                not isinstance(case["chatgpt_auth_dir"], str) or not case["chatgpt_auth_dir"].strip()):
            raise ValueError("chatgpt_auth_dir must be a nonempty directory path")
    return value


def validate_budget(case: dict) -> RunLimits:
    limits = case["limits"]
    if "seconds" not in limits:
        raise ValueError("set seconds explicitly; null disables the single_model overall deadline")
    if limits["seconds"] is None and case.get("mode") != "single_model":
        raise ValueError("null seconds currently supports single_model only")
    if "tokens" not in limits and limits.get("money_cny") is None:
        raise ValueError("set tokens explicitly (null disables it), or supply a CNY ceiling")
    if "model_reconnect_seconds" in limits and case.get("mode") != "single_model":
        raise ValueError("model_reconnect_seconds currently supports single_model only")
    if "time_budget_mode" in limits and case.get("mode") != "single_model":
        raise ValueError("time_budget_mode currently supports single_model only")
    values = {
        name: limits.get(name) for name in
        ("model_calls", "tool_calls", "seconds", "tokens", "money_cny")}
    for configured, field in (("model_retries", "max_model_retries"),
                              ("retry_backoff_seconds", "retry_backoff_seconds"),
                              ("model_reconnect_seconds", "model_reconnect_seconds"),
                              ("time_budget_mode", "time_budget_mode")):
        if configured in limits:
            values[field] = limits[configured]
    parsed = RunLimits.model_validate_json(json.dumps(values))
    if case["provider"] in SUBSCRIPTION_PROVIDERS and parsed.money_cny is not None:
        raise ValueError("subscription route cannot use a usage-priced money ceiling")
    if parsed.money_cny is not None:
        require_cny_price_schedule(case["model"], route_id=case["provider"])
    return parsed


def selected_case(configuration: dict, case_id: str) -> dict:
    matches = [case for case in configuration["cases"] if case["case_id"] == case_id]
    if not matches:
        raise ValueError(f"unknown case {case_id!r}")
    return matches[0]


def argv_for(case: dict, *, resume: bool = False) -> list[str]:
    context_args = context_arguments(case)
    budget = validate_budget(case)
    validate_output_limit(case["model"], case.get("output_tokens"), reason=case.get("low_output_limit_reason"))
    limits = case["limits"]
    output = resolve_run_output(ROOT / case["output"] if case.get("run_root") is None else Path(case["output"]), repository_root=ROOT,
                                run_root=case.get("run_root"))
    argv = [sys.executable, "-m", ALLOWED_ENTRYPOINTS[case["mode"]],
            "--out", str(output), "--provider", case["provider"],
            "--model", case["model"]]
    if case.get("credentials_file"):
        argv += ["--credentials-file", str((ROOT / Path(case["credentials_file"]).expanduser()).resolve())]
    argv += ["--scope", case["scope"], "--image-kind", case["image_kind"],
            "--model-calls", str(limits["model_calls"]),
            "--tool-calls", str(limits["tool_calls"]),
            "--output-tokens", str(case["output_tokens"])]
    argv += ["--no-time-limit"] if budget.seconds is None else ["--seconds", str(limits["seconds"])]
    if "time_budget_mode" in limits:
        argv += ["--time-budget-mode", budget.time_budget_mode]
    if case.get("process_reviews"):
        argv += ["--process-reviews"]
    if mesh_worker_concurrency(case):
        argv += ["--mesh-workers", str(mesh_worker_concurrency(case))]
    if case.get("chatgpt_auth_dir"):
        argv += ["--chatgpt-auth-dir", str(Path(case["chatgpt_auth_dir"]).expanduser().resolve())]
    if case.get("run_root") is not None:
        if case["mode"] not in {"single_model", "role_division"}:
            raise ValueError("explicit run_root supports single_model and role_division only")
        argv += ["--run-root", str(case["run_root"])]
    argv += ["--no-token-limit"] if budget.tokens is None else ["--tokens", str(budget.tokens)]
    if budget.money_cny is not None:
        argv += ["--money-cny", str(budget.money_cny)]
    source_flag = "--mesh" if case["input_kind"] == "mesh" else "--images"
    if case.get("low_output_limit_reason") is not None:
        argv += ["--low-output-limit-reason", case["low_output_limit_reason"]]
    for name in ("max_consecutive_truncations", "max_total_truncations"):
        if name in limits:
            argv += ["--" + name.replace("_", "-"), str(limits[name])]
    argv += [source_flag, str(resolve_case_input(case))]
    auxiliary_images, building_input = resolve_supplemental_inputs(case)
    if auxiliary_images is not None:
        argv += ["--images", str(auxiliary_images)]
    if building_input is not None:
        argv += ["--building-input", str(building_input)]
    for name in case.get("floor_plan_images", []):
        argv += ["--floor-plan-image", name]
    if case["mode"] == "single_model":
        argv += ["--reasoning-history", case.get("reasoning_history", "all"),
                 "--max-candidates", str(case["max_candidates"]),
                 "--model-retries", str(limits.get("model_retries", 2)),
                 "--retry-backoff-seconds", str(limits.get("retry_backoff_seconds", 1.0))]
        if "model_reconnect_seconds" in limits:
            argv += ["--model-reconnect-seconds", str(budget.model_reconnect_seconds)]
        if case.get("continuation") is not None:
            continuation = case["continuation"]
            if resume:
                saved = json.loads((output / "continuation_manifest.json").read_bytes())
                if (Path(saved["source_run"]).resolve() != Path(continuation["source_run"]).resolve()
                        or saved["source_events_sha256"] != continuation["source_events_sha256"]):
                    raise ValueError("resume continuation source differs from the configured identity")
            else:
                argv += ["--continue-from", continuation["source_run"],
                         "--source-events-sha256", continuation["source_events_sha256"]]
                if continuation.get("allow_recovery_policy_change"):
                    argv.append("--allow-recovery-policy-change")
        argv += context_args
        for name in ("temperature", "reasoning_effort"):
            if case.get(name) is not None:
                argv += ["--" + name.replace("_", "-"), str(case[name])]
    elif case["mode"] == "external_coordinator_mcp":
        if case.get("reasoning_effort") is not None:
            argv += ["--reasoning-effort", case["reasoning_effort"]]
        argv += ["--quota-journal", str(ROOT / case["quota_journal"]),
                 "--quota-limit", str(case["quota_limit"]),
                 "--max-concurrent-observers", str(case["max_concurrent_observers"])]
        argv.append("--thinking" if case.get("thinking", True) else "--no-thinking")
    else:
        roles = load_roles(case.get("roles"))
        argv += ["--roles-json", json.dumps(
                    {name: configuration.model_dump() for name, configuration in roles.items()},
                    ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                 "--max-concurrent-readers", str(case.get("max_concurrent_readers", 8)),
                 "--max-candidates", str(case["max_candidates"]),
                 "--model-retries", str(limits.get("model_retries", 2)),
                 "--retry-backoff-seconds", str(limits.get("retry_backoff_seconds", 1.0))]
        argv += context_args
        if case.get("reader_task") is not None:
            argv += ["--reader-task-json", json.dumps(case["reader_task"], ensure_ascii=False)]
        if case.get("continuation") is not None:
            argv += ["--continue-from", case["continuation"]["source_run"],
                     "--source-events-sha256", case["continuation"]["source_events_sha256"]]
    if resume:
        argv.append("--resume")
    return argv


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "command", "launch"))
    parser.add_argument("configuration", type=Path)
    parser.add_argument("--case")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--low-output-limit-reason", help="record a reason without editing a historical configuration")
    args = parser.parse_args()
    configuration = load_configuration(args.configuration.resolve(), low_output_limit_reason=args.low_output_limit_reason)
    if args.action == "check":
        print(json.dumps({"status": "ready", "batch_id": configuration["batch_id"],
                          "cases": [case["case_id"] for case in configuration["cases"]]},
                         ensure_ascii=False))
        return
    if not args.case:
        parser.error("--case is required for command or launch")
    case = selected_case(configuration, args.case)
    command = argv_for(case, resume=args.resume)
    if args.action == "command":
        # Printed commands are for the host shell; launch itself always uses argv.
        print("& " + " ".join("'" + value.replace("'", "''") + "'" for value in command)
              if os.name == "nt" else shlex.join(command))
        return
    environment = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
    if os.name == "nt":
        raise SystemExit(subprocess.call(command, env=environment))
    os.execvpe(command[0], command, environment)


if __name__ == "__main__":
    main()
