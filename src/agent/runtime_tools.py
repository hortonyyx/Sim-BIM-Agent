"""Building policy for the frozen 5bb10538 BIM MCP service.

This module treats the service's readonly registration as the source of truth
for local observation, then inspects each coordinator-only implementation.
Inspection tools may append logs or regenerate views while remaining read-only
with respect to source BIM and workflow decisions.  Persistent claims, reviews,
audits, selections, and candidates are conservatively non-idempotent writes.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import asynccontextmanager
import base64
import copy
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Literal, Protocol

from scripts.tool_scripts.bim_agent_guidance import REFERENCES, build_guide, filter_tool_catalog, tool_capabilities
from src.agent_runtime.agent_registry import AgentVersionMismatch, agent_version_record
from src.agent_runtime.mcp_tools import McpToolClient
from src.harness_contracts.budget import BudgetAmounts
from src.harness_contracts.roles import (
    InputMaterialRequirement,
    ReturnRequirement,
    RoleDefinition,
    ToolGrant,
    authorize_tool_call,
)


FROZEN_BASELINE_COMMIT = "5bb10538"
Repeatability = Literal["read_only", "idempotent_write", "non_idempotent_write"]

MESH_OBSERVER_TOOL_NAMES = (
    "inspect_mesh", "inspect_mesh_directions", "view_mesh", "measure_mesh_pixels",
    "view_mesh_observation", "overlay_mesh_candidate",
    "view_mesh_section", "compare_mesh_source_pixels",
)
MESH_COORDINATOR_ONLY_TOOL_NAMES = (
    "set_candidate_mesh_frame",
)
LOCAL_OBSERVER_TOOL_NAMES = (
    "get_bim_reference", "inputs", "view_image", "pixel_profile",
    "view_pixel_profile", "view_pixel_region_overview", "view_pixel_region",
    "preview_space_trace", "view_space_trace", "select_space_trace",
    "map_dimension_chain", "compare_facade_spans", "map_pixels", "read_candidate_items",
)
COORDINATOR_ONLY_TOOL_NAMES = (
    "record_inference", "inspect_inference", "audit_inference_candidate",
    "inspect_plan_draft", "revise_plan_bim", "view_plan_wall_support",
    "claim_transaction", "record_claim", "replace_claim_sources", "view_claim_evidence", "decide_claim",
    "claim_status", "confirm_claims", "check_wall_dimensions", "inspect_candidate",
    "check_source_space_relation", "check_openings",
    "record_work_review", "finish_bim", "overlay_candidate", "revise_bim",
    "review_detail", "build_plan_bim", "assemble_plan_bim", "build_parametric_bim",
    "inspect_parametric_plan", "patch_parametric_bim", "build_bim", "view_elevation_candidate", "view_candidate",
    "prepare_elevation_reading", "apply_elevation_readings",
)
COORDINATOR_TOOL_NAMES = LOCAL_OBSERVER_TOOL_NAMES + COORDINATOR_ONLY_TOOL_NAMES
MESH_LOCAL_OBSERVER_TOOL_NAMES = MESH_OBSERVER_TOOL_NAMES + LOCAL_OBSERVER_TOOL_NAMES
MESH_COORDINATOR_TOOL_NAMES = (
    MESH_OBSERVER_TOOL_NAMES + MESH_COORDINATOR_ONLY_TOOL_NAMES + COORDINATOR_TOOL_NAMES
)

# These coordinator-only tools inspect saved state.  Their implementations can
# append tools.jsonl or create replaceable render/measurement caches, but they do
# not alter source BIM, select workflow state, or create a review consumed by a
# later delivery check.  This is the same boundary used by the frozen server's
# --readonly surface: observational sidecars are disclosed, not misrepresented
# as a completely write-free filesystem operation.
COORDINATOR_INSPECTION_TOOL_NAMES = (
    "inspect_inference", "inspect_plan_draft", "view_plan_wall_support",
    "view_claim_evidence", "claim_status", "check_wall_dimensions",
    "inspect_candidate", "inspect_parametric_plan",
    "view_candidate",
)
NON_IDEMPOTENT_WRITE_TOOL_NAMES = tuple(
    name for name in COORDINATOR_ONLY_TOOL_NAMES
    if name not in COORDINATOR_INSPECTION_TOOL_NAMES
)
FROZEN_TOOL_REPEATABILITY: Mapping[str, Repeatability] = {
    **{name: "read_only" for name in LOCAL_OBSERVER_TOOL_NAMES},
    **{name: "read_only" for name in MESH_OBSERVER_TOOL_NAMES},
    **{name: "read_only" for name in COORDINATOR_INSPECTION_TOOL_NAMES},
    "set_candidate_mesh_frame": "non_idempotent_write",
    **{name: "non_idempotent_write" for name in NON_IDEMPOTENT_WRITE_TOOL_NAMES},
}

# This one tool does not look remote at its call site, but its implementation
# starts a Haiku subprocess.  Phase 1 has no delegation/runtime model calls.
PHASE1_FORBIDDEN_TOOLS = frozenset({"review_detail"})


class ToolCatalogMismatch(ValueError):
    """The running service does not match the selected Agent version."""


class ToolAccessDenied(PermissionError):
    """A role, phase rule, or repeatability policy rejected a call before MCP."""


class ToolClient(Protocol):
    async def list_tools(self) -> list[dict[str, Any]]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class FrozenBimCatalog:
    """Non-executable parent catalog for a standalone reader unit.

    Uses the same definitions as the stdio server, but owns no transport. The
    child still opens its isolated readonly MCP through ``task_session``.
    """

    def __init__(self, run_directory: Path, *, repository_root: Path):
        root = Path(repository_root).resolve()
        try:
            agent_version_record(root, verify=True)
        except AgentVersionMismatch as error:
            raise ToolCatalogMismatch(str(error)) from error
        from scripts.tool_scripts import run_bim_agent
        if Path(run_bim_agent.__file__).resolve() != root / "scripts/tool_scripts/run_bim_agent.py":
            raise ToolCatalogMismatch("parent catalog builder belongs to a different repository")
        self._server = run_bim_agent.build_tool_server(Path(run_directory).resolve())

    @property
    def definition_source(self) -> dict[str, Any]:
        return {"kind": "in_process_definition_factory",
                "factory": "scripts.tool_scripts.run_bim_agent.build_tool_server",
                "stdio_server_started": False, "tool_execution_allowed": False}

    async def list_tools(self) -> list[dict[str, Any]]:
        return [tool.model_dump(mode="json", exclude_none=True)
                for tool in await self._server.list_tools()]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raise ToolAccessDenied("reader_unit parent catalog cannot execute tools")


def frozen_bim_client(
    run_directory: Path,
    readonly: bool = False,
    repository_root: Path | None = None,
    *, enabled_only: bool = False,
) -> McpToolClient:
    """Create the stage-1 building server transport from generic MCP pieces."""

    root = (Path(repository_root) if repository_root is not None
            else Path(__file__).resolve().parents[2]).resolve()
    try:
        agent_version_record(root, verify=True)
    except AgentVersionMismatch as error:
        raise ToolCatalogMismatch(str(error)) from error
    server = root / "scripts" / "tool_scripts" / "run_bim_agent.py"
    if not server.is_file():
        raise FileNotFoundError(f"frozen BIM MCP server does not exist: {server}")
    run = Path(run_directory).resolve()
    args = [str(server), "serve", str(run)]
    if readonly:
        args.append("--readonly")
    if enabled_only:
        args.append("--enabled-only")
    return McpToolClient(
        command=sys.executable,
        args=args,
        cwd=root,
        run_directory=run,
    )


def repeatability_for(tool_name: str) -> Repeatability:
    try:
        return FROZEN_TOOL_REPEATABILITY[tool_name]
    except KeyError as error:
        raise ToolCatalogMismatch(f"tool is not in the registered Agent catalog: {tool_name}") from error


def _catalog_mode(*, readonly: bool, mesh: bool) -> str:
    return ("readonly" if readonly else "coordinator") + ("_mesh" if mesh else "")


def _catalog_names(*, readonly: bool, mesh: bool) -> tuple[str, ...]:
    if mesh:
        return MESH_LOCAL_OBSERVER_TOOL_NAMES if readonly else MESH_COORDINATOR_TOOL_NAMES
    return LOCAL_OBSERVER_TOOL_NAMES if readonly else COORDINATOR_TOOL_NAMES


def validate_frozen_catalog(
    tools: list[dict[str, Any]], *, readonly: bool, mesh: bool | None = None
) -> str:
    """Require the exact definitions exposed by the frozen server mode."""

    names = [tool.get("name") for tool in tools]
    if any(not isinstance(name, str) or not name for name in names):
        raise ToolCatalogMismatch("MCP catalog contains a missing or invalid tool name")
    if len(names) != len(set(names)):
        raise ToolCatalogMismatch("MCP catalog contains duplicate tool names")
    actual = set(names)
    if mesh is None:
        variants = [candidate for candidate in (False, True)
                    if actual == set(_catalog_names(readonly=readonly, mesh=candidate))]
        if len(variants) != 1:
            base = set(_catalog_names(readonly=readonly, mesh=False))
            mesh_names = set(_catalog_names(readonly=readonly, mesh=True))
            raise ToolCatalogMismatch(
                "frozen MCP catalog name set matches neither admitted-input variant; "
                f"base_missing={sorted(base - actual)}, base_extra={sorted(actual - base)}, "
                f"mesh_missing={sorted(mesh_names - actual)}, mesh_extra={sorted(actual - mesh_names)}"
            )
        mesh = variants[0]
    expected = set(_catalog_names(readonly=readonly, mesh=mesh))
    if actual != expected:
        raise ToolCatalogMismatch(
            f"frozen MCP catalog mismatch; missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )
    mode = _catalog_mode(readonly=readonly, mesh=mesh)
    root = Path(__file__).resolve().parents[2]
    try:
        record = agent_version_record(root, verify=True)
    except AgentVersionMismatch as error:
        raise ToolCatalogMismatch(str(error)) from error
    actual_hash = hashlib.sha256(_canonical_json_bytes(tools)).hexdigest()
    expected_hash = record["tool_catalog_sha256"][mode]
    if actual_hash != expected_hash:
        raise ToolCatalogMismatch(
            f"Agent {record['version_id']} MCP {mode} definitions changed: {actual_hash}"
        )
    return mode


def coordinator_role(budget: BudgetAmounts) -> RoleDefinition:
    """Return the coordinator whitelist for either frozen input variant."""

    return RoleDefinition(
        role_id="coordinator",
        responsibilities=("inspect evidence, decide building changes, and save selected BIM output",),
        tool_whitelist=tuple(
            ToolGrant(tool_name=name, access="read" if repeatability_for(name) == "read_only" else "write")
            for name in MESH_COORDINATOR_TOOL_NAMES
        ),
        input_materials=(
            InputMaterialRequirement(name="admitted_run_inputs", media_type="application/json"),
            InputMaterialRequirement(name="frozen_prompt_material", media_type="text/plain"),
        ),
        return_requirements=(
            ReturnRequirement(name="saved_candidate_or_stop_reason", schema_ref="stage1/run-result"),
        ),
        budget=budget,
    )


def local_observer_role(budget: BudgetAmounts) -> RoleDefinition:
    """Return the read-only whitelist for either frozen input variant."""

    return RoleDefinition(
        role_id="local_observer",
        responsibilities=("inspect admitted evidence and report a bounded local observation",),
        tool_whitelist=tuple(
            ToolGrant(tool_name=name, access="read") for name in MESH_LOCAL_OBSERVER_TOOL_NAMES
        ),
        input_materials=(
            InputMaterialRequirement(name="admitted_images_or_views", media_type="image/*"),
            InputMaterialRequirement(name="localized_question", media_type="text/plain"),
        ),
        return_requirements=(
            ReturnRequirement(name="observation", schema_ref="stage0/localized-observation"),
        ),
        budget=budget,
        read_only=True,
    )


class FrozenBimTools:
    """Apply frozen catalog, role grants, and phase-one no-delegation rules."""

    def __init__(
        self,
        client: ToolClient,
        role: RoleDefinition,
        *,
        run_directory: Path,
        phase: int = 1,
    ) -> None:
        self.client = client
        self.role = role
        self.run_directory = Path(run_directory).resolve()
        self.phase = phase
        self._catalog_checked = False
        manifest_path = self.run_directory / "inputs.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.mesh = bool(manifest.get("mesh_input"))
        self.capabilities = tool_capabilities(manifest)
        if phase == 1:
            self.capabilities["review_detail"] = False

    @asynccontextmanager
    async def task_session(self, *, directory, root, role, timing, images):
        """Give a read-only child its own T1 clock and measurement sidecars.

        Copies only the originals supplied in the evidence package. The
        EvidenceTools wrapper still enforces the supplied crop boundaries.
        Parent inputs and concurrently running siblings are never rewritten.
        """
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "images").mkdir(exist_ok=True)
        parent = json.loads((self.run_directory / "inputs.json").read_bytes())
        inventory = {}
        for name, (raw, size) in images.items():
            if Path(name).name != name:
                raise ValueError("observer image must be an admitted filename")
            target = directory / "images" / name
            if target.exists() and target.read_bytes() != raw:
                raise ValueError("observer original image changed on resume")
            if not target.exists():
                target.write_bytes(raw)
            inventory[name] = {"size": list(size), "sha256": hashlib.sha256(raw).hexdigest()}
        manifest = {"images": inventory, "image_kind": parent.get("image_kind"),
            "input_mode": "runtime_local_observation", "scope": "supplied local evidence only",
            "floor_plan_images": [], "floor_scope_source": "not_declared", **timing}
        path = directory / "inputs.json"
        if path.exists() and json.loads(path.read_bytes()) != manifest:
            raise ValueError("observer inputs or time budget changed on resume")
        if not path.exists():
            path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        async with frozen_bim_client(directory, readonly=True, repository_root=root) as client:
            yield FrozenBimTools(client, role, run_directory=directory)

    async def list_tools(self) -> list[dict[str, Any]]:
        tools = await self.client.list_tools()
        validate_frozen_catalog(tools, readonly=self.role.read_only, mesh=self.mesh)
        self._catalog_checked = True
        return filter_tool_catalog(tools, **self.capabilities)

    def repeatability(self, name: str) -> Repeatability:
        """Expose the frozen retry classification to the runtime loop."""

        return repeatability_for(name)

    def recovery_policy(self, name: str) -> str:
        # This is deliberately independent of the broader read-only permission
        # class: some tools create workflow decisions or launch other work.
        repeatable_observations = {"get_bim_reference", "inputs", "view_image", "pixel_profile",
            "view_pixel_profile", "view_pixel_region_overview", "view_pixel_region",
            "map_pixels", "read_candidate_items"}
        return "retry_read" if name in repeatable_observations else "manual"

    def project_tool_result(self, name: str, raw: dict) -> dict:
        """Show measurement numbers once while journaling the complete raw reply."""
        if name != "measure_mesh_pixels" or raw.get("isError"):
            return raw
        metadata = _result_metadata(raw)
        if (metadata.get("error") or not isinstance(metadata.get("queries"), list)
                or not metadata.get("details_file") or not metadata.get("details_sha256")):
            # Old replies without a readable full-report reference stay whole.
            return raw
        from scripts.tool_scripts.bim_agent_replies import measurement_summary
        shown = copy.deepcopy(raw)
        if isinstance(shown.get("structuredContent"), dict):
            shown["structuredContent"] = measurement_summary(shown["structuredContent"])
        for block in shown.get("content", ()):
            if block.get("type") != "text":
                continue
            try:
                value = json.loads(block["text"])
            except (ValueError, TypeError, KeyError):
                continue
            if isinstance(value, dict) and isinstance(value.get("queries"), list):
                block["text"] = json.dumps(measurement_summary(value), ensure_ascii=False)
        return shown

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self._catalog_checked:
            await self.list_tools()
        repeatability = repeatability_for(name)
        required_access: Literal["read", "write"] = "read" if repeatability == "read_only" else "write"
        authorize_tool_call(self.role, name, required_access)
        if not filter_tool_catalog([{"name": name}], **self.capabilities):
            # This is a known policy refusal before the MCP server is called.
            # Return it through the ordinary tool-result envelope so the loop
            # records a failed call rather than an unknown write outcome.
            blocked = {
                "status": "blocked",
                "failure_stage": "before_execution",
                "tool_name": name,
                "reason": f"{name} is disabled for this run",
            }
            return {
                "content": [{"type": "text", "text": json.dumps(blocked, ensure_ascii=False)}],
                "isError": True,
                "structuredContent": blocked,
            }
        if self.role.read_only and repeatability != "read_only":
            # authorize_tool_call already rejects this for valid RoleDefinition,
            # but keep the policy obvious at the executor boundary.
            raise ToolAccessDenied(f"read-only role cannot execute write tool: {name}")
        # The loop snapshots state before a write and compares it after a
        # transport exception.  This executor deliberately does no inspection
        # or retry by itself: recovery needs the loop's event/stop context.
        return await self.client.call_tool(name, arguments)

    def artifacts(self) -> list[Path]:
        """List durable run artifacts used for recovery and stop reports.

        Unlike the state snapshot, this includes viewer HTML, source/proposal
        files, reports, evidence images, and observational sidecars so a stop
        receipt does not hide a useful partial result.  Only transport scratch,
        bytecode, and the separately captured legacy tool log are excluded.
        """

        if not self.run_directory.is_dir():
            raise FileNotFoundError(f"run directory does not exist: {self.run_directory}")
        return [
            path for path in sorted(self.run_directory.rglob("*"))
            if path.is_file() and _is_run_artifact(path, self.run_directory)
        ]

    def snapshot_state(self) -> dict[str, Any]:
        """Hash durable files only, for unknown-write recovery comparison.

        The snapshot is a sorted mapping, not a claim that an unchanged file set
        proves a lost write did not happen.  It intentionally excludes generated
        views and logs because their counters/timestamps are not source state.
        """

        if not self.run_directory.is_dir():
            raise FileNotFoundError(f"run directory does not exist: {self.run_directory}")
        files: dict[str, str] = {}
        for path in sorted(self.run_directory.rglob("*")):
            if not path.is_file() or not _is_durable_artifact(path, self.run_directory):
                continue
            files[path.relative_to(self.run_directory).as_posix()] = _sha256(path)
        canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return {"files": files, "snapshot_sha256": hashlib.sha256(canonical).hexdigest()}

    def image_origins(self, raw_result: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        """Map returned MCP image bytes to source-input metadata when available.

        The key is the SHA-256 of exact image bytes shown to the model.  A crop
        or resized view stays a distinct sent hash but retains its original input
        path, source hash, crop coordinates, and pixel scale from the raw tool
        metadata.  For a direct original image, original and sent hashes match.
        """

        if not isinstance(raw_result, Mapping):
            raise TypeError("raw_result must be the JSON MCP result object")
        metadata = _result_metadata(raw_result)
        # Claim results return real image views in evidence_previews rather than
        # at the response root. Match each preview to the exact returned PNG so
        # its original view_id remains usable after context eviction.
        previews = metadata.get("evidence_previews", [])
        if not isinstance(previews, list):
            previews = []
        result: dict[str, dict[str, Any]] = {}
        content = raw_result.get("content", [])
        if not isinstance(content, list):
            return result
        for block in content:
            if not isinstance(block, Mapping) or block.get("type") != "image":
                continue
            data = block.get("data")
            if not isinstance(data, str):
                continue
            try:
                sent = base64.b64decode(data, validate=True)
            except ValueError as error:
                raise ValueError("MCP image content is not valid base64") from error
            sent_sha = hashlib.sha256(sent).hexdigest()
            image_metadata = next((preview for preview in previews
                if isinstance(preview, dict) and preview.get("returned_png_sha256") == sent_sha), metadata)
            source_name = image_metadata.get("name") or image_metadata.get("image")
            source_hash = image_metadata.get("image_sha256")
            source_path = self.run_directory / "images" / source_name if isinstance(source_name, str) else None
            origin: dict[str, Any] = {
                "sent_sha256": sent_sha,
                "mime_type": block.get("mimeType"),
                "raw_metadata": image_metadata,
            }
            if source_path is not None and source_path.is_file() and isinstance(source_hash, str):
                actual_source_sha = _sha256(source_path)
                if actual_source_sha == source_hash:
                    origin.update(
                        original_path=str(source_path),
                        original_sha256=source_hash,
                        box_original_pixels=image_metadata.get("box_original_pixels"),
                        original_pixels_per_returned_pixel=image_metadata.get("original_pixels_per_returned_pixel"),
                        view_id=image_metadata.get("view_id"),
                    )
                    if sent_sha == source_hash:
                        origin["transport"] = "original_bytes"
                    else:
                        origin["transport"] = "derived_view_with_original_coordinates"
                else:
                    origin["origin_status"] = "metadata_source_hash_no_longer_matches_run_input"
            else:
                origin["origin_status"] = "tool_return_has_no_verified_original-input mapping"
            result[sent_sha] = origin
        return result


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _is_durable_artifact(path: Path, run_directory: Path) -> bool:
    """Exclude known logs and regenerated image views from recovery snapshots."""

    relative = path.relative_to(run_directory)
    excluded_directories = {
        ".harness_tmp", "__pycache__", "bridge", "mesh_observations", "mesh_overlays",
        "overlay_calibrations", "source_image_projections", "pixel_profiles", "space_traces",
        "facade_comparisons", "space_relation_reviews", "plan_wall_support", "image_views",
        "pixel_region_overviews", "pixel_regions",
    }
    if any(part in excluded_directories for part in relative.parts):
        return False
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".html", ".log", ".jsonl"}:
        # Input image bytes are immutable evidence, so they are the exception.
        return relative.parts[:1] == ("images",)
    return True


def _is_run_artifact(path: Path, run_directory: Path) -> bool:
    relative = path.relative_to(run_directory)
    if any(part in {".harness_tmp", "__pycache__"} for part in relative.parts):
        return False
    return path.name != "tools.jsonl" and path.suffix.lower() != ".pyc"


def _result_metadata(raw_result: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the ordinary text JSON metadata returned alongside an MCP image."""
    from scripts.tool_scripts.bim_agent_saved_result import result_metadata
    return result_metadata(raw_result)


def write_frozen_materials(output_directory: Path, *, repository_root: Path) -> dict[str, Any]:
    """Write schemas, both frozen system prompts, and byte-hash evidence.

    The caller supplies live schemas obtained from ``run_bim_agent.py serve`` to
    :func:`write_frozen_tool_catalog`; keeping server execution separate makes
    this function deterministic and free of model/network activity.
    """

    root = Path(repository_root).resolve()
    output = Path(output_directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    try:
        version = agent_version_record(root, verify=True)
    except AgentVersionMismatch as error:
        raise ToolCatalogMismatch(str(error)) from error
    source_hashes = {
        relative: metadata["sha256"] for relative, metadata in version["files"].items()
    }
    prompts = {
        "drawing_system_prompt.txt": build_guide(images="drawings", mesh=False),
        "mesh_system_prompt.txt": build_guide(images="mesh_views", mesh=True),
    }
    for name, text in prompts.items():
        (output / name).write_text(text, encoding="utf-8")
    reference_directory = output / "references"
    reference_directory.mkdir(exist_ok=True)
    reference_hashes: dict[str, str] = {}
    for topic, text in sorted(REFERENCES.items()):
        path = reference_directory / f"{topic}.txt"
        path.write_text(text, encoding="utf-8")
        reference_hashes[topic] = _sha256(path)
    material = {
        "agent_version": version["version_id"],
        "baseline_commit": version["source_commit"],
        "source_sha256": source_hashes,
        "prompts": {
            name: {"sha256": _sha256(output / name), "byte_count": (output / name).stat().st_size}
            for name in prompts
        },
        "references": {
            topic: {
                "path": f"references/{topic}.txt",
                "sha256": digest,
                "byte_count": (reference_directory / f"{topic}.txt").stat().st_size,
            }
            for topic, digest in reference_hashes.items()
        },
    }
    (output / "material_manifest.json").write_text(
        json.dumps(material, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return material


def write_frozen_tool_catalog(output_directory: Path, tools: list[dict[str, Any]], *, readonly: bool,
                              definition_source: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Persist exact definitions plus separate policy and byte evidence."""

    mode = validate_frozen_catalog(tools, readonly=readonly)
    version = agent_version_record(Path(__file__).resolve().parents[2], verify=True)
    output = Path(output_directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Copy through JSON to prevent later mutation of the live client result.
    definitions = json.loads(json.dumps(tools, ensure_ascii=False, allow_nan=False))
    definition_hashes = {
        tool["name"]: hashlib.sha256(_canonical_json_bytes(tool)).hexdigest()
        for tool in definitions
    }
    policy = {
        tool["name"]: {
            "repeatability": repeatability_for(tool["name"]),
            "phase1_available": tool["name"] not in PHASE1_FORBIDDEN_TOOLS,
        }
        for tool in definitions
    }
    catalog = {
        "baseline_commit": version["source_commit"],
        "server_mode": mode,
        "definition_encoding": "UTF-8 canonical JSON (sorted keys, compact separators)",
        "definitions_sha256": hashlib.sha256(_canonical_json_bytes(definitions)).hexdigest(),
        "definition_sha256_by_name": definition_hashes,
        "tools": definitions,
        "policy_by_name": policy,
    }
    if definition_source is not None:
        catalog["definition_source"] = json.loads(json.dumps(definition_source, allow_nan=False))
    target_names = {
        "coordinator": "coordinator_tools.json",
        "readonly": "local_observer_tools.json",
        "coordinator_mesh": "coordinator_mesh_tools.json",
        "readonly_mesh": "local_observer_mesh_tools.json",
    }
    target = output / target_names[mode]
    target.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    receipt = {
        "path": str(target),
        "sha256": _sha256(target),
        "tool_count": len(definitions),
        "definitions_sha256": catalog["definitions_sha256"],
    }
    manifest_path = output / "tool_catalog_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {
        "baseline_commit": version["source_commit"],
        "catalogs": {},
    }
    manifest["catalogs"][catalog["server_mode"]] = {
        "path": target.name,
        "sha256": receipt["sha256"],
        "tool_count": receipt["tool_count"],
        "definitions_sha256": receipt["definitions_sha256"],
    }
    if definition_source is not None:
        manifest["catalogs"][catalog["server_mode"]]["definition_source"] = catalog["definition_source"]
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return receipt
