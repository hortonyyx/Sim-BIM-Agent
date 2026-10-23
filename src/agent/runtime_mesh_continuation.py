"""Explicit new-run mesh continuation from immutable, fully journaled work.

Only recovery implementation upgrades are admitted. Building logic, prompt,
model, tool surface and decoding remain bound to the original execution.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

from src.agent_runtime.continuation import ReadOnlyRun, continuation_allowance
from src.agent_runtime.context_continuation import ImportedContext
from src.agent_runtime.loop import RunLimits, Runtime
from src.agent_runtime.store import EventStore
from src.agent.runtime_mesh_workers import MeshWorkerTools

# Reviewed compatibility boundary, not a general skip-version-check switch.
RECOVERY_CODE_PATHS = frozenset({
    "src/agent/runtime_entry.py", "src/agent/runtime_configuration.py",
    "src/agent/runtime_mesh_continuation.py", "src/agent_runtime/loop.py",
    "src/agent_runtime/continuation.py", "src/agent_runtime/context_continuation.py",
    "src/agent_runtime/failures.py", "src/agent_runtime/responses.py",
    "src/agent_runtime/network_recovery.py",
    "src/harness_contracts/events.py", "src/harness_contracts/__init__.py",
    "src/harness_contracts/validation.py",
    "src/harness_contracts/incremental.py",
})


def file_hash(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def tree_hashes(path):
    result = {}
    for item in sorted(Path(path).rglob("*")):
        if item.is_symlink() or item.is_junction():
            raise ValueError("continuation source must not contain linked paths")
        if item.is_file():
            result[item.relative_to(path).as_posix()] = file_hash(item)
    return result


def _version_value(source, versions, name):
    return json.loads(source.get_bytes(versions[name]["evidence"]["blob"]))


def continuation_parent(source):
    """Bind an ancestry sidecar to its durable context-import event."""
    if not (source.directory / "continuation_manifest.json").exists():
        return None
    manifest = source.read_json("continuation_manifest.json")
    anchors = [source.resolve(event.payload.result_content) for event in source.events
        if event.payload.event_type == "external_coordinator_mcp"
        and event.payload.method == "continuation.import_context"]
    if len(anchors) != 1:
        raise ValueError("continuation ancestry has no unique durable import")
    anchor = anchors[0]
    if (anchor["source_run"] != manifest["source_run"]
            or anchor["source_journal"]["sha256"] != manifest["source_events_sha256"]):
        raise ValueError("continuation ancestry sidecar differs from its journal")
    parent = ReadOnlyRun(manifest["source_run"])
    if parent.evidence_hashes["events.jsonl"] != manifest["source_events_sha256"]:
        raise ValueError("continuation ancestry journal changed")
    return parent


def continuation_lineage(source):
    """Count each prior execution once; offline repair time is separate."""
    rows, seen = [], set()
    while source is not None:
        digest = source.evidence_hashes["events.jsonl"]
        if digest in seen:
            raise ValueError("cyclic continuation ancestry")
        seen.add(digest)
        summaries = [event for event in source.events if event.payload.event_type == "run_usage_summary"]
        if not summaries:
            raise ValueError("continuation ancestry lacks durable accounting")
        summary = source.resolve(summaries[-1].payload.raw_summary)
        if source.read_json("receipt.json").get("elapsed_seconds") != summary["elapsed_seconds"]:
            raise ValueError("continuation ancestry elapsed time differs from journal")
        rows.append({"source_run": str(source.directory), "source_events_sha256": digest,
            "elapsed_seconds": summary["elapsed_seconds"],
            "model_requests": sum(e.payload.event_type == "adapter_request" for e in source.all_events),
            "tool_calls": sum(e.payload.event_type == "tool_invocation" for e in source.all_events),
            "network_recovery": summary.get("network_recovery")})
        source = continuation_parent(source)
    return rows


def continuation_receipt(record, result):
    rows = record["source_execution_lineage"]
    source_seconds = sum(row["elapsed_seconds"] for row in rows)
    return {"source_run": record["source_run"], "source_execution_lineage": rows,
        "source_elapsed_seconds": record["source_elapsed_seconds"],
        "source_cumulative_execution_seconds": source_seconds,
        "new_execution_seconds": result.get("elapsed_seconds"),
        "combined_execution_seconds": source_seconds + result.get("elapsed_seconds", 0),
        "source_request_count": sum(row["model_requests"] for row in rows),
        "source_tool_invocation_count": sum(row["tool_calls"] for row in rows),
        "source_tree_unchanged": True, "cold_start": False, "usage_policy": record["usage_policy"]}


class ContinuedRuntime(Runtime):
    """Install imported history once at the fresh-run initial checkpoint."""

    def _checkpoint(self):
        if not getattr(self, "_imported_context_ready", False):
            self.context_import.initialize(self)
            self.store.write_json("context-import.json", self.context_import.manifest)
            self._imported_context_ready = True
        result = super()._checkpoint()
        if not getattr(self, "_continuation_claimed", False):
            from src.agent_runtime.continuation import claim_continuation
            claim = claim_continuation(self.context_import.source, self.store.directory)
            self.store.write_json("continuation_claim.json", claim)
            self._continuation_claimed = True
        return result


class _SourceWorkers(MeshWorkerTools):
    def _configuration(self):
        # Preserve the exact historic schema rather than adding new defaults
        # before validating an old immutable admission record.
        return self.source_configuration


class ContinuedMeshWorkers(MeshWorkerTools):
    """Read old worker outcomes; never restart or relabel exhausted workers."""

    def import_workers(self, readers):
        self.imported_readers, self.imported_rows = {}, []
        for reader in readers:
            for event in reader.store.events:
                for ref in event.source_refs:
                    if ref.source_id != "mesh-worker-admission" or ref.blob is None:
                        continue
                    record = json.loads(reader.store.get_bytes(ref.blob))
                    task_id = record["task"]["task_id"]
                    if task_id in self.imported_readers:
                        raise ValueError("duplicate worker identity across continuation ancestry")
                    value, _ = reader._read(task_id)
                    self.imported_readers[task_id] = reader
                    self.imported_rows.append({"task_id": task_id,
                        "status": value["status"], "question": record["task"]["question"],
                        "candidate": record["task"].get("candidate"), "budget": record["task"]["budget"],
                        "runtime": value.get("runtime"), "result_available": value.get("result") is not None,
                        "imported_from_run": str(reader.store.directory)})
        return self

    def _read(self, task_id, view_ids=(), measurement_call_ids=()):
        reader = self.imported_readers.get(task_id)
        return (reader._read(task_id, view_ids, measurement_call_ids) if reader
                else super()._read(task_id, view_ids, measurement_call_ids))

    async def _dispatch(self, task):
        reader = self.imported_readers.get(task["task_id"])
        if reader is not None:
            if reader._record(reader._child(task["task_id"]))["task"] != task:
                return {"task_id": task["task_id"], "status": "rejected",
                    "reason": "Imported task requires its exact original arguments; allowance is not renewed."}
            return reader._read(task["task_id"])[0]
        return await super()._dispatch(task)

    def state(self):
        value = super().state()
        value["tasks"] = [*self.imported_rows, *value["tasks"]]
        return value

    def image_origins(self, raw):
        previews = raw.get("structuredContent", {}).get("evidence_previews", ())
        imported = {p.get("worker_task_id") for p in previews if p.get("worker_task_id") in self.imported_readers}
        if imported:
            if len(imported) != 1 or any(p.get("worker_task_id") not in imported for p in previews):
                raise ValueError("mixed worker preview origins are not supported")
            return self.imported_readers[next(iter(imported))].image_origins(raw)
        return super().image_origins(raw)


@dataclass
class MeshContinuation:
    source: ReadOnlyRun
    destination: Path
    root: Path
    limits: RunLimits
    record: dict
    source_hashes: dict
    context_import: ImportedContext
    versions: dict
    guide: str
    task: str

    def verify_unchanged(self):
        self.source.verify_unchanged()
        if tree_hashes(self.source.directory) != self.source_hashes:
            raise ValueError("immutable continuation source tree changed")

    def copy_workspace(self, *, started_epoch):
        self.verify_unchanged()
        self.destination.mkdir(parents=True, exist_ok=False)
        copied, excluded = {}, []
        for name, digest in self.source_hashes.items():
            if not name.startswith(("bim/", "tasks/", "imported-tasks/")):
                continue
            if (name.startswith("bim/.harness_tmp/") or name in {
                    "bim/summary.json", "bim/delivery.json", "bim/delivery.html"}):
                excluded.append(name)
                continue
            relative = "imported-tasks/" + name[6:] if name.startswith("tasks/") else name
            target = self.destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise ValueError("imported worker artifact identity collision")
            shutil.copy2(self.source.directory / name, target)
            if file_hash(target) != digest:
                raise ValueError("continuation copy hash mismatch")
            copied[relative] = digest
        for name, value in (("guide.txt", self.guide), ("task.txt", self.task)):
            (self.destination / name).write_text(value, encoding="utf-8")
        # Only execution timing is rebased. Geometry, assumptions, observations
        # and model conversation remain byte-for-byte prior accepted work.
        manifest_path = self.destination / "bim" / "inputs.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["started_epoch"] = started_epoch
        manifest["time_budget_seconds"] = self.limits.seconds
        manifest["deadline_epoch"] = started_epoch + self.limits.seconds if self.limits.seconds is not None else None
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.record.update(copied_files=copied, excluded_failed_packaging=excluded,
            input_manifest_timing_rebase={"source_sha256": copied["bim/inputs.json"],
                "destination_sha256": file_hash(manifest_path), "started_epoch": started_epoch,
                "time_budget_seconds": self.limits.seconds}, source_tree_hashes=self.source_hashes)
        self.verify_unchanged()

    def validate_runtime(self, engine):
        """Compare preserved contracts before a claim or any model call."""
        current = engine.versions.model_dump(mode="json")
        changed = []
        for name in ("dependency_lock", "prompt", "tool_definitions", "inference_parameters"):
            if current[name]["identifier"] != self.versions[name]["identifier"]:
                raise ValueError("continuation incompatible " + name)
        if current["remote_model"] != self.versions["remote_model"]:
            raise ValueError("continuation cannot change provider or model")
        source_route = _version_value(self.source, self.versions, "model_route")
        target_route = json.loads(engine.store.get_bytes(engine.versions.model_route.evidence.blob))
        if source_route.get("route_id") == target_route.get("route_id") == "offline-scripted":
            source_route.pop("fixture_sha256", None)
            target_route.pop("fixture_sha256", None)
        if source_route != target_route:
            raise ValueError("continuation cannot change model route, workers or context projection")
        before = _version_value(self.source, self.versions, "code_commit")["files"]
        after = json.loads(engine.store.get_bytes(engine.versions.code_commit.evidence.blob))["files"]
        for name in sorted(set(before) | set(after)):
            if before.get(name) != after.get(name):
                if name not in RECOVERY_CODE_PATHS:
                    raise ValueError("continuation incompatible product code change: " + name)
                changed.append(name)
        previous = copy.deepcopy(self.context_import.saved["config"])
        present = engine._config()
        previous["limits"] = present["limits"]
        previous["role"]["budget"] = present["role"]["budget"]
        previous["root_tool_calls"] = present["root_tool_calls"]
        previous["versions"] = present["versions"]
        previous["reconnect_consumed_before_seconds"] = present.get("reconnect_consumed_before_seconds", 0)
        if "reconnect_consumed_before_seconds" not in present:
            previous.pop("reconnect_consumed_before_seconds")
        if previous != present:
            fields = [k for k in set(previous) | set(present) if previous.get(k) != present.get(k)]
            raise ValueError("continuation incompatible runtime configuration: " + ", ".join(sorted(fields)))
        self.record["compatibility"] = {"status": "pass", "changed_recovery_code_paths": changed,
            "source_versions": self.versions, "destination_versions": current,
            "preserved": ["tool_definitions", "prompt", "model", "provider", "inference_parameters",
                "dependency_lock", "geometry_and_delivery_code", "context_policy", "worker_configuration"]}
        self.verify_unchanged()

    def worker_readers(self):
        return source_worker_readers(self.source, self.root)


def source_worker_readers(source, root, *, seen=None):
    """Keep all ancestor results reachable without granting them fresh work."""
    seen = set() if seen is None else seen
    if source.directory in seen:
        raise ValueError("cyclic continuation ancestry")
    seen.add(source.directory)
    versions = source.verified_versions()
    route = _version_value(source, versions, "model_route")
    saved = EventStore.latest_checkpoint(source)[1]
    readers = []
    configurations = []
    for event in source.events:
        for ref in event.source_refs:
            if ref.source_id == "mesh-worker-admission" and ref.blob:
                record = json.loads(source.get_bytes(ref.blob))
                config = record["configuration"]
                if (config["model"] != saved["config"]["model"]
                        or config["parameters"] != saved["config"]["parameters"]
                        or config["route"] != route
                        or RunLimits.model_validate(config["limits"]) != RunLimits.model_validate(saved["config"]["limits"])):
                    raise ValueError("worker admission differs from original route and limits")
                configurations.append(config)
    if configurations:
        if any(config != configurations[0] for config in configurations):
            raise ValueError("source worker configurations disagree")
        config = configurations[0]
        reader = _SourceWorkers(SimpleNamespace(run_directory=source.directory / "bim", image_origins=lambda raw: {}),
            store=source, root=root, adapter_factory=None, model=config["model"],
            parameters=config["parameters"], route=config["route"], limits=RunLimits.model_validate(config["limits"]),
            start_epoch=config["start_epoch"], max_concurrent=config["max_concurrent"],
            reasoning_history=config["reasoning_history"])
        reader.source_configuration = config
        readers.append(reader)
    ancestor = continuation_parent(source)
    if ancestor is not None:
        readers.extend(source_worker_readers(ancestor, root, seen=seen))
    return readers


def inspect_mesh_continuation(source_path, destination, requested_limits, *, root,
                              expected_events_sha256, allow_recovery_policy_change=False):
    source = ReadOnlyRun(source_path)
    destination, root = Path(destination).resolve(), Path(root).resolve()
    if (destination.exists() or destination == source.directory
            or destination.is_relative_to(source.directory) or source.directory.is_relative_to(destination)):
        raise ValueError("continuation needs a separate new destination")
    source_hashes = tree_hashes(source.directory)
    limits, record = continuation_allowance(source, requested_limits,
        expected_events_sha256=expected_events_sha256,
        allow_recovery_policy_change=allow_recovery_policy_change)
    versions = source.verified_versions()
    if versions.get("mode") != "single_model" or source.task_id != "coordinator":
        raise ValueError("mesh continuation requires an original single-model coordinator")
    manifest = source.read_json("bim/inputs.json")
    if manifest.get("input_mode") != "native_mesh_agent_experiment":
        raise ValueError("continuation source is not admitted native mesh input")
    imported = ImportedContext(source)
    if imported.saved["versions"] != versions:
        raise ValueError("source checkpoint versions differ from journal")
    configs = [ref for event in source.events if event.payload.event_type == "run_lifecycle"
        and event.payload.action == "start" for ref in event.source_refs if ref.source_id == "runtime-config"]
    if len(configs) != 1 or json.loads(source.get_bytes(configs[0].blob)) != imported.saved["config"]:
        raise ValueError("source checkpoint configuration differs from journal")
    if source.read_json("receipt.json").get("completion") is not None:
        raise ValueError("completed BIM cannot be imported as unfinished work")
    from src.agent.runtime_tools import FrozenBimTools
    observed = FrozenBimTools.snapshot_state(SimpleNamespace(run_directory=source.directory / "bim"))
    observed_files = {k: v for k, v in observed["files"].items() if k not in {"delivery.json", "summary.json"}}
    expected_files = {k: v for k, v in imported.saved["tool_state"]["files"].items()
                      if k not in {"delivery.json", "summary.json"}}
    if observed_files != expected_files:
        raise ValueError("source BIM file set or bytes differ from durable checkpoint")
    if not any(name.startswith("bim/candidate_") and name.endswith("/source_model.json") for name in source_hashes):
        raise ValueError("mesh continuation needs a saved source candidate")
    guide = source.get_bytes(versions["prompt"]["evidence"]["blob"]).decode("utf-8")
    if (source.read_bytes("guide.txt").decode("utf-8").replace("\r\n", "\n") != guide
            or hashlib.sha256(guide.encode()).hexdigest() != versions["prompt"]["identifier"]):
        raise ValueError("source guide differs from original model prompt")
    task = source.read_bytes("task.txt").decode("utf-8").replace("\r\n", "\n")
    record.update(kind="native_mesh_exact_continuation", cold_start=False,
        manual_geometry_edits=False, added_modelling_instruction=False,
        source_checkpoint=imported.checkpoint_ref.model_dump(mode="json"),
        source_checkpoint_sequence=imported.checkpoint_sequence,
        source_usage_accounting=source.read_json("receipt.json").get("root_usage_accounting"),
        source_usage_unknown_must_remain_unknown=True,
        source_execution_lineage=continuation_lineage(source))
    result = MeshContinuation(source, destination, root, limits, record, source_hashes, imported, versions, guide, task)
    result.verify_unchanged()
    return result
