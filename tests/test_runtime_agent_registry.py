"""V1: immutable history, independent release lines and startup refusal."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import subprocess

import pytest

from src.agent_runtime.agent_registry import (AgentVersionMismatch, agent_version_record,
    discover_files, load_agent_registry, main, migrate_registry, register_versions, release_records)
from src.agent_runtime.store import EventStore
from src.agent_runtime.versions import make_versions, version_labels
from src.harness_contracts import BudgetAmounts, VersionManifest

ROOT = Path(__file__).resolve().parents[1]


def _scratch(tmp_path):
    root = tmp_path / "repository"
    for name in ("src/agent_runtime/loop.py", "src/agent_runtime/route_dispatch.json", "src/agent/tool.py"):
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("{}\n", encoding="utf-8")
    fingerprints = {"tool_catalog_sha256": {name: "a" * 64 for name in
        ("coordinator", "readonly", "coordinator_mesh", "readonly_mesh")},
        "mode_fingerprints": {name: {key: "b" * 64 for key in
            ("sha256", "tools_sha256", "guidance_sha256", "task_template_sha256")}
            for name in ("single_model/coordinator", "role_division/plan_reader")}}
    old = {"schema_version": "agent-version-registry.v1", "current_version": "old-20261006",
           "versions": {"old-20261006": {"source_commit": "fixture", "files":
               discover_files(root, "domain"), "tool_catalog_sha256": fingerprints["tool_catalog_sha256"]}}}
    path = root / "registry.json"
    path.write_text(json.dumps(old), encoding="utf-8")
    register_versions(root, registry_path=path, registration_date="20261007", fingerprints=fingerprints,
                      legacy_order=["old-20261006"])
    return root, path, fingerprints




def test_automatic_membership_verification_and_independent_version_lines(tmp_path, monkeypatch):
    root, path, fingerprints = _scratch(tmp_path)
    monkeypatch.setattr("src.agent_runtime.agent_registry.source_commit", lambda root: "fixture")
    original = path.read_bytes()
    noop = register_versions(root, registry_path=path, fingerprints=fingerprints)
    assert noop["changed_lines"] == noop["changed_modes"] == []
    assert path.read_bytes() == original
    for line, relative in (("runtime", "src/agent_runtime/new.json"), ("domain", "src/agent/new.py")):
        file = root / relative
        file.write_text("new\n", encoding="utf-8")
        with pytest.raises(AgentVersionMismatch):
            release_records(root, registry_path=path)
        result = register_versions(root, registry_path=path, fingerprints=fingerprints)
        assert result["changed_lines"] == [line]
        assert result["changed_modes"] == []
        release_records(root, registry_path=path)
        file.unlink()
        with pytest.raises(AgentVersionMismatch):
            release_records(root, registry_path=path)
        register_versions(root, registry_path=path, fingerprints=fingerprints)
    runtime_data = root / "src/agent_runtime/route_dispatch.json"
    runtime_data.write_text('{"limit": 3}', encoding="utf-8")
    with pytest.raises(AgentVersionMismatch):
        agent_version_record(root, registry_path=path)
    assert register_versions(root, registry_path=path, fingerprints=fingerprints)["changed_lines"] == ["runtime"]
    (root / "src/agent/__pycache__").mkdir()
    (root / "src/agent/__pycache__/tool.pyc").write_bytes(b"cache")
    (root / "src/agent/tests").mkdir()
    (root / "src/agent/tests/test_tool.py").write_text("check", encoding="utf-8")
    (root / "src/agent/README.md").write_text("documentation", encoding="utf-8")
    assert register_versions(root, registry_path=path, fingerprints=fingerprints)["changed_lines"] == []


def test_fingerprint_change_reports_only_affected_mode_and_retains_history(tmp_path):
    root, path, fingerprints = _scratch(tmp_path)
    original = load_agent_registry(root, path)
    updated = copy.deepcopy(fingerprints)
    updated["mode_fingerprints"]["role_division/plan_reader"]["guidance_sha256"] = "c" * 64
    result = register_versions(root, registry_path=path, fingerprints=updated)
    assert result["changed_lines"] == ["domain"]
    assert result["changed_modes"] == ["role_division/plan_reader"]
    after = load_agent_registry(root, path)
    for key, value in original["domain_versions"].items():
        assert after["domain_versions"][key] == value


def test_current_cli_and_manifest_include_both_lines_and_all_role_parameters(tmp_path, capsys):
    assert main(["verify", "--root", str(ROOT)]) == 0
    identity = json.loads(capsys.readouterr().out)
    routes = {name: {"provider": "offline", "model": f"model-{name}", "reasoning_effort": effort,
                    "output_tokens": 1234, "temperature": 0.0}
              for name, effort in (("coordinator", "high"), ("plan_reader", "medium"), ("elevation_reader", "low"))}
    with EventStore(tmp_path / "run", run_id="versions", task_id="coordinator",
                    budget_limit=BudgetAmounts(tokens=100, calls=1)) as store:
        args = dict(root=ROOT, prompt="test", tools=[], parameters={"max_tokens": 1234},
                    route={"route_id": "offline", "model": "test", "roles": {"roles": routes}})
        versions = make_versions(store, **args)
        assert versions.runtime_version.identifier == identity["runtime_version"]
        assert versions.domain_version.identifier == identity["domain_version"]
        assert versions.mode == "role_division"
        assert versions.role_models["elevation_reader"]["reasoning_effort"] == "low"
        assert versions.role_models["plan_reader"]["output_tokens"] == 1234
        store.write_json("versions.json", versions.model_dump(mode="json"))
        child = store.for_task("read-plan", parent_task_id="coordinator")
        args["route"] = {"route_id": "offline", "model": "model-plan_reader"}
        child_versions = make_versions(child, **args)
        assert version_labels(child_versions) == version_labels(versions)
        # Old journals remain readable; absent new fields are not fabricated.
        old = {key: value for key, value in versions.model_dump(mode="json").items()
               if key not in {"runtime_version", "domain_version", "mode", "role_models"}}
        assert VersionManifest.model_validate(old).runtime_version is None


def test_domain_provider_tracks_live_role_guidance_without_changing_single_model(monkeypatch):
    from src.agent.version_fingerprints import domain_fingerprints
    from src.agent.runtime_roles.guidance import ROLE_GUIDANCE
    before = asyncio.run(domain_fingerprints())
    assert len(before["mode_fingerprints"]) == 9
    assert {"mesh_workers/coordinator", "mesh_workers/observer"} <= set(before["mode_fingerprints"])
    registered = agent_version_record(ROOT)
    assert before["mode_fingerprints"] == registered["mode_fingerprints"]
    assert before["tool_catalog_sha256"] == registered["tool_catalog_sha256"]
    monkeypatch.setitem(ROLE_GUIDANCE, "plan_reader", ROLE_GUIDANCE["plan_reader"] + "\nChanged instruction.")
    after = asyncio.run(domain_fingerprints())
    assert [name for name in before["mode_fingerprints"]
            if before["mode_fingerprints"][name] != after["mode_fingerprints"][name]] == ["role_division/plan_reader"]


def test_registry_rejects_escape_and_incomplete_migration(tmp_path):
    root, path, _ = _scratch(tmp_path)
    registry = load_agent_registry(root, path)
    registry["domain_versions"][registry["current_domain_version"]]["files"]["../outside.py"] = {
        "kind": "tool", "sha256": "0" * 64}
    path.write_text(json.dumps(registry), encoding="utf-8")
    with pytest.raises(AgentVersionMismatch):
        release_records(root, registry_path=path)
    with pytest.raises(ValueError):
        migrate_registry(root, {"schema_version": "agent-version-registry.v1", "versions": {"one": {}}}, legacy_order=[])
