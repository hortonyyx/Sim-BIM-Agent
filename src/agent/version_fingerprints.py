"""Offline domain-owned fingerprints of the actual model-facing surfaces.

No models or building tools are invoked: only MCP list_tools and the production
role catalog wrappers. Templates are rendered from the entrypoints' expressions
with symbolic input values; guidance and schemas are never copied here.
"""
from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import inspect
import json
from pathlib import Path
import sys
import tempfile
import textwrap
from types import SimpleNamespace

from scripts.tool_scripts import run_bim_agent as runner
from scripts.tool_scripts.bim_agent_guidance import build_guide, filter_tool_catalog
from src.agent.runtime_roles.guidance import get_role_guide
from src.agent.runtime_roles.readers import ReaderTools
from src.agent.runtime_roles.elevation import ElevationReaderTools, ELEVATION_COORDINATES
from src.agent.runtime_roles.coordinates import READER_COORDINATES
from src.agent.runtime_roles.session import RoleSession
from src.agent.runtime_mesh_workers import mesh_worker_tool_specs, mesh_worker_task_payload, WORKER_GUIDANCE
from scripts.tool_scripts.bim_agent_guidance import MESH_WORKERS
from src.agent_runtime.mcp_tools import McpToolClient
from src.agent_runtime.store import EventStore
from src.agent_runtime.loop import RunLimits

ROOT = Path(__file__).resolve().parents[2]


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _tree(function):
    return ast.parse(textwrap.dedent(inspect.getsource(function)))


def _assignment(function, name):
    matches = [node.value for node in ast.walk(_tree(function)) if isinstance(node, ast.Assign)
               and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)]
    if len(matches) != 1:
        raise ValueError(f"template assignment changed: {function.__name__}.{name}")
    return matches[0]


def _evaluate(expression, values):
    return eval(compile(ast.Expression(expression), "<domain-template>", "eval"), values)


def _execute(statement, values):
    exec(compile(ast.Module(body=[statement], type_ignores=[]), "<domain-template>", "exec"), values)


def _single_tasks():
    from src.agent.runtime_entry import execute
    result = {}
    for mode in ("cold", "seed", "plan", "mesh"):
        for declared in (False, True):
            env = {"args": SimpleNamespace(scope="<scope>", timeout="<seconds>"),
                   "plan_image": "<image>", "plan_recovery": mode == "plan",
                   "seed_path": mode == "seed", "mesh_input": mode == "mesh",
                   "building_input": declared}
            for name in ("continuation", "declaration_prompt", "prompt"):
                env[name] = _evaluate(_assignment(runner.run_experiment, name), env)
            result[f"claude_code/{mode}/declaration={declared}"] = env["prompt"]
    # The runtime's first user message, with a symbolic declaration and image.
    admitted = {key: f"<{key}>" for key in ("raw_sha256", "declaration", "field_semantics", "conflict_policy")}
    env = {"json": json, "base64": base64, "task": "<scope>", "admitted": admitted,
           "name": "<image>", "raw": b"<image-bytes>"}
    nodes = sorted(ast.walk(_tree(execute)), key=lambda n: getattr(n, "lineno", 0))
    for node in nodes:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Tuple) and
                any(isinstance(x, ast.Name) and x.id == "user_content" for x in t.elts) for t in node.targets):
            _execute(node, env)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute):
            if isinstance(node.value.func.value, ast.Name) and node.value.func.value.id == "user_content":
                _execute(node, env)
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and node.target.id == "user_content":
            _execute(node, env)
    result["runtime/plain"] = "<scope>"
    result["runtime/declaration-and-image"] = env["user_content"]
    return result


def _role_task(role):
    if role == "coordinator":
        from src.agent.runtime_roles.entry import execute
        result = {}
        for declared in (False, True):
            for continued in (False, True):
                env = {"task": "<scope>", "manifest": {"building_input": "<declaration>"},
                       "json": json, "continued_readers": {"evidence": "<verified-reader-summary>"}}
                env["content"] = _evaluate(_assignment(execute, "content"), env)
                for node in ast.walk(_tree(execute)):
                    if not (isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name)
                            and node.target.id == "content"):
                        continue
                    names = {part.id for part in ast.walk(node.value) if isinstance(part, ast.Name)}
                    if ("manifest" in names and not declared) or ("continued_readers" in names and not continued):
                        continue
                    _execute(node, env)
                result[f"declaration={declared}/continuation={continued}"] = env["content"]
        return result
    # Uses the exact JSON payload construction in run_reader, including the
    # coordinate instructions. The projected previous-artifact context remains
    # symbolic; its production projection implementation is in the domain scope.
    env = {"json": json, "base64": base64, "raw_image": b"<image-bytes>",
           "initial_image": b"<coordinate-view-bytes>", "size": ["<width>", "<height>"],
           "initial_view": "<original-pixel-coordinate-view>",
           "task": {"role_id": role, "task_id": "<task>", "image": "<image>",
                    "target": "<target>", "instructions": "<instructions>", "input_sha256": "<image-sha256>"},
           "previous_context": "<previous-artifact-context-or-null>",
           "ELEVATION_COORDINATES": ELEVATION_COORDINATES, "READER_COORDINATES": READER_COORDINATES}
    return _evaluate(_assignment(RoleSession.run_reader, "content"), env)


def _readonly_guide():
    command = _assignment(runner.subscription, "command")
    for index, node in enumerate(command.elts):
        if isinstance(node, ast.Constant) and node.value == "--system-prompt":
            return _evaluate(command.elts[index + 1], {"readonly": True})
    raise ValueError("subscription system-prompt expression changed")


def _fingerprint(tools, guidance, task):
    parts = {"tools_sha256": _hash(tools), "guidance_sha256": _hash(guidance),
             "task_template_sha256": _hash(task)}
    return {**parts, "sha256": _hash(parts)}


class _Catalog:
    """Already-probed catalog, with exactly the production visibility filter."""
    def __init__(self, tools, directory):
        self.tools, self.run_directory = tools, directory

    async def list_tools(self):
        return filter_tool_catalog(self.tools)


async def domain_fingerprints(root=ROOT):
    root = Path(root).resolve()
    catalogs, modes = {}, {}
    tasks = _single_tasks()
    with tempfile.TemporaryDirectory(prefix=".version-fingerprints-", dir=root) as temporary:
        base = Path(temporary)
        raw_catalogs = {}
        for mode in ("coordinator", "readonly", "coordinator_mesh", "readonly_mesh"):
            run = base / mode
            run.mkdir()
            manifest = {"images": {}, "scope": "fingerprint registration"}
            mesh = mode.endswith("_mesh")
            if mesh:
                manifest["mesh_input"] = {"sha256": "0" * 64, "frozen_path": "assets/placeholder.glb"}
            (run / "inputs.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
            arguments = [str(root / "scripts/tool_scripts/run_bim_agent.py"), "serve", str(run)]
            if mode.startswith("readonly"):
                arguments.append("--readonly")
            async with McpToolClient(command=sys.executable, args=arguments, cwd=root, run_directory=run) as client:
                tools = await client.list_tools()
            raw_catalogs[mode] = tools
            catalogs[mode] = _hash(tools)
            projections, guides = {}, {}
            for review in (False, True):
                for continuation in (False, True):
                    flags = {"review_detail": review, "continuation": continuation}
                    key = f"review={review}/continuation={continuation}"
                    projections[key] = filter_tool_catalog(tools, **flags)
                    for kind in (None, "drawings", "mesh_views", "photos", "unknown"):
                        guides[f"{key}/images={kind}"] = build_guide(images=kind, mesh=mesh, **flags)
            if mode.startswith("readonly"):
                guides["claude_code/local_question"] = _readonly_guide()
            else:
                # The optional Claude Code worker receives its real wrapper
                # text plus run_guide. Model selection is recorded per run.
                workers = json.loads(runner.worker_agents(run, "haiku"))
                guides["claude_code/worker"] = {name: {key: worker[key] for key in ("description", "prompt")}
                                               for name, worker in workers.items()}
            modes[f"single_model/{mode}"] = _fingerprint(projections, guides, tasks)
            if mode == "coordinator_mesh":
                modes["mesh_workers/coordinator"] = _fingerprint(
                    {key: [*value, *mesh_worker_tool_specs()] for key, value in projections.items()},
                    {"base": guides, "optional_workers": MESH_WORKERS}, tasks)
        limits = RunLimits(model_calls=1, tool_calls=1, seconds=1, tokens=1)
        modes["mesh_workers/observer"] = _fingerprint(
            filter_tool_catalog(raw_catalogs["readonly_mesh"]), WORKER_GUIDANCE,
            mesh_worker_task_payload({"request": "<frozen-task>", "inputs": "<frozen-inputs>",
                                      "source_snapshot": "<optional-source-snapshot>"}, limits))
        with EventStore(base / "roles", run_id="fingerprints", task_id="coordinator",
                        budget_limit=limits.ledger_limit()) as store:
            frozen = _Catalog(raw_catalogs["coordinator"], base / "coordinator")
            coordinator = RoleSession(store=store, frozen=frozen, routes={}, adapter_factory=None,
                                      limits=limits, root=root)
            roles = {"coordinator": coordinator,
                     "plan_reader": ReaderTools(_Catalog(raw_catalogs["readonly"], base / "readonly"),
                         role_id="plan_reader", image_name="<image>", trial=SimpleNamespace()),
                     "elevation_reader": ElevationReaderTools(_Catalog(raw_catalogs["readonly"], base / "readonly"),
                         role_id="elevation_reader", image_name="<image>", target="North")}
            for role, wrapper in roles.items():
                modes[f"role_division/{role}"] = _fingerprint(
                    await wrapper.list_tools(), get_role_guide(role), _role_task(role))
    return {"tool_catalog_sha256": catalogs, "mode_fingerprints": modes}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(domain_fingerprints()), ensure_ascii=False, sort_keys=True))
