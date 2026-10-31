"""Verified releases and invocation metadata; remote aliases stay unverified."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.harness_contracts import RemoteModelIdentity, SourceRef, VersionManifest, VersionStamp
from .agent_registry import release_records, source_commit
from .store import EventStore


def make_versions(store: EventStore, *, root: Path, prompt: str,
                  tools: list[dict], parameters: dict, route: dict,
                  code_paths: tuple[str, ...] = ()) -> VersionManifest:
    root = root.resolve()
    releases = release_records(root)
    commit = source_commit(root)
    # Working-tree file hashes also identify an uncommitted development run.
    sources = {}
    for relative in ("src/agent_runtime", "src/harness_contracts", *code_paths):
        path = root / relative
        files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
        if path == root / "src/agent_runtime":
            files = sorted((*files, path / "model_profiles.json"))
        for file in files:
            sources[file.relative_to(root).as_posix()] = hashlib.sha256(file.read_bytes()).hexdigest()
    code = store.source("code-manifest", {"commit": commit, "files": sources})

    def stamp(name, value):
        evidence = store.source(name, value)
        return VersionStamp(identifier=evidence.blob.sha256, evidence=evidence)

    agent = releases["domain"]
    agent_evidence = store.source("agent-version", agent)
    runtime_evidence = store.source("runtime-version", releases["runtime"])
    mode, models = run_model_configuration(store, route=route, parameters=parameters)
    lock = root / "uv.lock"
    if not lock.is_file():
        raise ValueError("a dependency lock is required for a versioned run")
    lock_ref = store.put_bytes(lock.read_bytes(), "text/plain")
    prompt_ref = store.put_bytes(prompt.encode("utf-8"), "text/plain")
    return VersionManifest(
        code_commit=VersionStamp(identifier=commit, evidence=code),
        dependency_lock=VersionStamp(identifier=lock_ref.sha256, evidence=SourceRef(
            source_id="dependency-lock", source_kind="runtime", locator="uv.lock", blob=lock_ref)),
        agent_version=VersionStamp(identifier=agent["version_id"], evidence=agent_evidence),
        domain_version=VersionStamp(identifier=agent["version_id"], evidence=agent_evidence),
        runtime_version=VersionStamp(identifier=releases["runtime"]["version_id"], evidence=runtime_evidence),
        mode=mode, role_models=models,
        prompt=VersionStamp(identifier=prompt_ref.sha256, evidence=SourceRef(
            source_id="system-prompt", source_kind="runtime", locator="system-prompt", blob=prompt_ref)),
        tool_definitions=stamp("tool-definitions", tools),
        inference_parameters=stamp("inference-parameters", parameters),
        model_route=stamp("model-route", route),
        remote_model=RemoteModelIdentity(route_id=route["route_id"], remote_alias=route["model"], alias_status="unverified"))


def model_configuration(route: dict, parameters: dict) -> dict:
    """Record only public routing/decoding settings, never credentials."""
    return {"route_id": route["route_id"], "model": route["model"],
            "reasoning_effort": parameters.get("reasoning_effort", parameters.get("output_config", {}).get("effort")),
            "output_tokens": parameters.get("max_tokens", parameters.get("max_completion_tokens")),
            "parameters": parameters}


def run_model_configuration(store, *, route, parameters):
    configured = route.get("roles", {}).get("roles")
    if configured:
        models = {}
        for name, config in configured.items():
            from .providers import LIVE_PROVIDERS, provider_parameters
            provider = config.get("route_id", config.get("provider"))
            if provider in LIVE_PROVIDERS:
                decoding = provider_parameters(provider, model=config["model"], output_tokens=config["output_tokens"],
                    reasoning_effort=config.get("reasoning_effort"), temperature=config.get("temperature"),
                    thinking=True if config.get("enable_thinking") is None else config["enable_thinking"])
            else:
                decoding = {"max_tokens": config["output_tokens"], **{key: config[key] for key in
                    ("reasoning_effort", "temperature", "enable_thinking") if config.get(key) is not None}}
            models[name] = model_configuration({"route_id": provider, "model": config["model"]}, decoding)
        return "role_division", models
    if not store.is_root_task:
        root_versions = store.directory / "versions.json"
        if root_versions.is_file():
            saved = json.loads(root_versions.read_bytes())
            if saved.get("role_models"):
                return saved.get("mode") or "single_model", saved["role_models"]
    return "single_model", {store.task_id: model_configuration(route, parameters)}


def version_labels(versions: VersionManifest) -> dict:
    """Compact, identical identity fields for root and child receipts."""
    return {"runtime_version": versions.runtime_version.identifier if versions.runtime_version else None,
            "domain_version": versions.domain_version.identifier if versions.domain_version else None,
            "mode": versions.mode, "role_models": versions.role_models,
            "git_commit": versions.code_commit.identifier}


def external_run_identity(root: Path, *, mode: str, role_models: dict) -> dict:
    """Verify both releases before an external client's first model request."""
    releases = release_records(root)
    return {"runtime_version": releases["runtime"]["version_id"],
            "domain_version": releases["domain"]["version_id"],
            "agent_version": releases["domain"]["version_id"],
            "mode": mode, "role_models": role_models, "git_commit": source_commit(root)}
