"""Immutable runtime/domain releases; normal runs verify, never register.

Domain supplies fingerprints through a JSON subprocess protocol. This module
does not import building code. Old Agent IDs remain aliases of their original
snapshots, not retroactive full runtime/domain releases.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Any

REGISTRY_RELATIVE_PATH = Path("src/agent_runtime/agent_versions.json")
RELEASE_MANIFEST_PATH = Path("release-manifest.json")
_SCHEMA = "agent-version-registry.v2"
_CATALOG_MODES = ("coordinator", "readonly", "coordinator_mesh", "readonly_mesh")
_SKIP_PARTS = {"__pycache__", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache",
               ".venv", "node_modules", "tests", "testdata", "docs", "logs", "archive"}
_SKIP_SUFFIXES = {".pyc", ".pyo", ".tmp", ".log", ".bak"}
_SKIP_NAMES = {"readme.md", "license.md", "license", "affected_tests.py", "affected_tests_rules.yaml"}
DEFAULT_SCOPE = {
    "runtime": ["src/agent_runtime", "src/harness_contracts", "src/__init__.py",
                "src/utils/__init__.py", "src/utils/file_lock.py",
                "pyproject.toml", "uv.lock"],
    "domain": ["src/agent", "scripts/tool_scripts", "src/configs", "src/mcp",
               "src/utils", "src/validator", "src/converters", "src/runner",
               "src/converter_manager.py", "scripts/glm_code.py", "scripts/glm_code.sh"],
}


class AgentVersionMismatch(ValueError):
    """The checkout differs from the selected immutable release."""


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def product_version_id(line, record, scope):
    """Content identity for a generated product snapshot, distinct from dev IDs."""
    encoded = (json.dumps({"scope": scope, "record": record}, ensure_ascii=False,
                          sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    return f"{line}-product-v1-" + hashlib.sha256(encoded).hexdigest()[:24]


def _registry_path(root, registry_path):
    override = os.environ.get("BIM_AGENT_REGISTRY_PATH") if registry_path is None else None
    if override is not None and not Path(override).is_absolute():
        raise ValueError("BIM_AGENT_REGISTRY_PATH must be absolute")
    return (Path(registry_path) if registry_path is not None else
            Path(override) if override else Path(root) / REGISTRY_RELATIVE_PATH).resolve()


def load_agent_registry(root: Path, registry_path: Path | None = None) -> dict[str, Any]:
    path = _registry_path(root, registry_path)
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise AgentVersionMismatch(f"version registry does not exist: {path}") from error
    if registry.get("schema_version") == "agent-version-registry.v1":
        if registry.get("current_version") not in registry.get("versions", {}):
            raise AgentVersionMismatch("registry has no valid current version")
        return registry
    if registry.get("schema_version") != _SCHEMA:
        raise AgentVersionMismatch("unsupported version registry schema")
    domains = registry.get("domain_versions", {})
    if registry.get("current_domain_version") not in domains:
        raise AgentVersionMismatch("registry has no valid current domain version")
    runtime_id = registry.get("current_runtime_version")
    if runtime_id is not None and runtime_id not in registry.get("runtime_versions", {}):
        raise AgentVersionMismatch("registry has no valid current runtime version")
    projection = registry.get("product_projection")
    if projection is not None:
        if (not isinstance(projection, dict) or projection.get("schema_version") != "sim-bim-product-projection.v1"
                or not re.fullmatch(r"[a-f0-9]{40}", projection.get("source_commit", ""))):
            raise AgentVersionMismatch("invalid product projection provenance")
        if registry.get("aliases") != {}:
            raise AgentVersionMismatch("product projection cannot alias historical versions")
        for line in ("runtime", "domain"):
            identity = registry[f"current_{line}_version"]
            if set(registry[f"{line}_versions"]) != {identity}:
                raise AgentVersionMismatch("product projection must contain only its current versions")
            record = registry[f"{line}_versions"][identity]
            if (record.get("source_commit") != projection["source_commit"]
                    or record.get("product_projection") != projection
                    or identity != product_version_id(line, record, registry["scope"])):
                raise AgentVersionMismatch(f"product {line} identity differs from its immutable projection")
    # Read-only compatibility view; only canonical records are serialized.
    registry["versions"] = dict(domains)
    for alias, canonical in registry.get("aliases", {}).items():
        if canonical not in domains or alias in domains:
            raise AgentVersionMismatch(f"invalid historical alias: {alias}")
        registry["versions"][alias] = domains[canonical]
    registry["current_version"] = registry["current_domain_version"]
    return registry


def _write_registry(path, registry):
    value = {k: v for k, v in registry.items() if k not in {"versions", "current_version"}}
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8", newline="\n")
    temporary.replace(path)


def _git(root, *args):
    return subprocess.check_output(["git", "--no-optional-locks", *args], cwd=root,
                                   text=True, encoding="utf-8").strip()


def source_commit(root):
    manifest = load_release_manifest(root)
    if manifest is not None:
        return manifest["source_commit"]
    try:
        return _git(root, "rev-parse", "HEAD")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def load_release_manifest(root: Path) -> dict[str, Any] | None:
    """Read release identity without rehashing payload; release_records is the gate.

    An absent manifest retains checkout behavior. A present but invalid manifest
    never falls back to Git, including in a checkout used to inspect a release.
    """
    path = Path(root) / RELEASE_MANIFEST_PATH
    if path.is_symlink():
        raise AgentVersionMismatch("release manifest must not be a symlink")
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise AgentVersionMismatch("release manifest cannot be read") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "sim-bim-release.v1":
        raise AgentVersionMismatch("unsupported release manifest schema")
    if not isinstance(manifest.get("source_commit"), str) or not re.fullmatch(
            r"[0-9a-f]{40}", manifest["source_commit"]):
        raise AgentVersionMismatch("release manifest needs a full source_commit")
    if any(not isinstance(manifest.get(f"{line}_version"), str)
           or not manifest[f"{line}_version"] for line in ("runtime", "domain")):
        raise AgentVersionMismatch("release manifest needs runtime/domain versions")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise AgentVersionMismatch("release manifest needs payload files")
    normalized = set()
    for relative, digest in files.items():
        candidate = PurePosixPath(relative)
        if (not relative or candidate.is_absolute() or ".." in candidate.parts
                or candidate.as_posix() != relative or "\\" in relative or ":" in relative
                or relative == RELEASE_MANIFEST_PATH.as_posix()
                or relative.casefold() in normalized):
            raise AgentVersionMismatch(f"invalid release payload path: {relative}")
        normalized.add(relative.casefold())
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise AgentVersionMismatch(f"invalid release payload digest: {relative}")
    if REGISTRY_RELATIVE_PATH.as_posix() not in files:
        raise AgentVersionMismatch("release manifest must cover the version registry")
    return manifest


def _verify_release_payload(root, manifest, registry, *, registry_path):
    if _registry_path(root, registry_path) != root / REGISTRY_RELATIVE_PATH:
        raise AgentVersionMismatch("a release must use its bundled version registry")
    projection = registry.get("product_projection")
    if projection is not None:
        declared = manifest.get("projection")
        if (manifest["source_commit"] != projection["source_commit"] or not isinstance(declared, dict)
                or any(declared.get(key) != value for key, value in projection.items())):
            raise AgentVersionMismatch("release source identity differs from its product projection")
    covered = {}
    for line in ("runtime", "domain"):
        version = registry[f"current_{line}_version"]
        if manifest[f"{line}_version"] != version:
            raise AgentVersionMismatch(f"release manifest {line} version differs from registry")
        for relative, metadata in registry[f"{line}_versions"][version]["files"].items():
            if manifest["files"].get(relative) != metadata["sha256"]:
                raise AgentVersionMismatch(f"release manifest differs from registered file: {relative}")
            covered[relative] = metadata["sha256"]
    for relative, expected in manifest["files"].items():
        path = root / relative
        if not path.resolve().is_relative_to(root) or any(
                part.is_symlink() or part.is_junction()
                for part in (path, *path.parents) if part != root and part.is_relative_to(root)):
            raise AgentVersionMismatch(f"release payload contains a linked path: {relative}")
        # _verify_files already hashes registered members and checks membership.
        # Only the extra release resources need another payload hash here.
        if relative not in covered and (not path.is_file() or _sha256(path) != expected):
            raise AgentVersionMismatch(f"release payload file mismatch: {relative}")


def migrate_registry(root, registry, *, legacy_order=None):
    """Rename history without inventing missing runtime/role fingerprints."""
    if registry["schema_version"] == _SCHEMA:
        return registry
    old = registry["versions"]
    if legacy_order is None:
        # v1 sorted dictionary keys: recover registration order from Git.
        legacy_order = []
        for commit in _git(root, "log", "--reverse", "--format=%H", "--",
                           REGISTRY_RELATIVE_PATH.as_posix()).splitlines():
            historical = json.loads(_git(root, "show", f"{commit}:{REGISTRY_RELATIVE_PATH.as_posix()}"))
            for key in historical.get("versions", {}):
                if key in old and key not in legacy_order:
                    legacy_order.append(key)
    if len(legacy_order) != len(old) or set(legacy_order) != set(old):
        raise ValueError("migration needs the complete, unique historical registration order")
    aliases, domains = {}, {}
    for number, alias in enumerate(legacy_order, 1):
        match = re.search(r"(?<!\d)(20\d{6})(?!\d)", alias)
        stamp = match[1] if match else _git(root, "show", "-s", "--format=%cs", old[alias]["source_commit"]).replace("-", "")
        canonical = f"domain-v{number}-{stamp}"
        aliases[alias] = canonical
        domains[canonical] = {**old[alias], "coverage": "legacy_explicit_files",
            "legacy_version_id": alias, "mode_fingerprints": None,
            "fingerprint_status": "not_recorded_by_legacy_registry"}
    return {"schema_version": _SCHEMA, "scope": DEFAULT_SCOPE,
            "fingerprint_provider": ["{python}", "-m", "src.agent.version_fingerprints"],
            "current_runtime_version": None,
            "current_domain_version": aliases[registry["current_version"]],
            "runtime_versions": {}, "domain_versions": domains, "aliases": aliases}


def _kind(relative, line):
    if line == "runtime":
        return "runtime"
    if "guidance" in relative or Path(relative).suffix in {".md", ".txt"}:
        return "guidance"
    if Path(relative).name in {"bim_inputs.py", "bim_agent_inputs.py", "run_bim_agent.py"}:
        return "task_description"
    return "tool"


def discover_files(root, line, *, scope=None, registry_path=None):
    """Hash bytes AND membership; refuse links instead of following them."""
    root = Path(root).resolve()
    scope = DEFAULT_SCOPE if scope is None else scope
    excluded = {root / REGISTRY_RELATIVE_PATH, _registry_path(root, registry_path)}
    files = {}
    runtime_paths = [root / relative for relative in scope["runtime"]]
    for relative in scope[line]:
        base = root / relative
        if not base.resolve().is_relative_to(root):
            raise ValueError(f"version scope escapes checkout: {relative}")
        if not base.exists():
            continue
        if base.is_symlink() or base.is_junction():
            raise ValueError(f"version scope contains a symlink: {relative}")
        candidates = [base] if base.is_file() else _walk_files(base)
        for path in candidates:
            if (path in excluded or path.suffix in _SKIP_SUFFIXES or path.name.casefold() in _SKIP_NAMES
                    or any(p in _SKIP_PARTS for p in path.relative_to(root).parts)):
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError(f"version scope contains a symlink: {path}")
            if line == "domain" and any(path == p or path.is_relative_to(p) for p in runtime_paths):
                continue
            name = path.relative_to(root).as_posix()
            files[name] = {"kind": _kind(name, line), "sha256": _sha256(path)}
    return dict(sorted(files.items()))


def _walk_files(base):
    for directory, names, files in os.walk(base, followlinks=False):
        names[:] = [name for name in names if name not in _SKIP_PARTS]
        if any((Path(directory) / name).is_symlink() or (Path(directory) / name).is_junction() for name in names):
            raise ValueError(f"version scope contains a linked directory: {directory}")
        yield from (Path(directory) / name for name in files)


def _verify_files(root, record, *, line, version, registry, registry_path):
    expected = record.get("files")
    if not isinstance(expected, dict) or not expected:
        raise AgentVersionMismatch(f"version {version} has no registered files")
    if record.get("coverage") == "automatic_directories":
        actual = discover_files(root, line, scope=registry["scope"], registry_path=registry_path)
    else:
        actual = {}
        for relative, metadata in expected.items():
            path = (root / relative).resolve()
            if not path.is_relative_to(root):
                raise AgentVersionMismatch(f"registered file escapes repository: {relative}")
            if path.is_file():
                actual[relative] = {**metadata, "sha256": _sha256(path)}
    changed = sorted(name for name in expected.keys() | actual.keys()
                     if expected.get(name) != actual.get(name))
    if changed:
        raise AgentVersionMismatch(f"{line} version {version} file mismatch: {json.dumps(changed)}")


def release_records(root, *, registry_path=None, verify=True):
    root = Path(root).resolve()
    registry = load_agent_registry(root, registry_path)
    if registry["schema_version"] != _SCHEMA or not registry.get("current_runtime_version"):
        raise AgentVersionMismatch("runtime/domain registration is required before a new run")
    result = {}
    for line in ("runtime", "domain"):
        version = registry[f"current_{line}_version"]
        record = registry[f"{line}_versions"][version]
        if verify:
            _verify_files(root, record, line=line, version=version, registry=registry, registry_path=registry_path)
        result[line] = {"version_id": version, **record}
    if verify:
        manifest = load_release_manifest(root)
        if manifest is not None:
            _verify_release_payload(root, manifest, registry, registry_path=registry_path)
    return result


def agent_version_record(root: Path, version_id: str | None = None, *,
                         registry_path: Path | None = None, verify: bool = True) -> dict[str, Any]:
    root = Path(root).resolve()
    registry = load_agent_registry(root, registry_path)
    selected = version_id or registry["current_version"]
    canonical = registry.get("aliases", {}).get(selected, selected)
    record = registry["versions"].get(canonical)
    if not isinstance(record, dict):
        raise AgentVersionMismatch(f"Agent version is not registered: {selected}")
    if verify:
        if registry["schema_version"] == _SCHEMA and canonical == registry["current_domain_version"]:
            release_records(root, registry_path=registry_path)
        else:
            _verify_files(root, record, line="domain", version=canonical, registry=registry, registry_path=registry_path)
    return {"version_id": canonical, **record}


def _fingerprints(root, registry):
    command = [part.replace("{python}", sys.executable) for part in registry["fingerprint_provider"]]
    env = {**os.environ, "PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    output = subprocess.check_output(command, cwd=root, env=env, text=True,
                                     encoding="utf-8", timeout=180)
    result = json.loads(output)
    if set(result.get("tool_catalog_sha256", {})) != set(_CATALOG_MODES) or not result.get("mode_fingerprints"):
        raise ValueError("fingerprint provider did not return catalogs and mode fingerprints")
    return result


def register_versions(root, *, registry_path=None, registration_date=None, fingerprints=None,
                      legacy_order=None, alias=None):
    """Migrate if needed, then append only changed lines in one atomic write."""
    root = Path(root).resolve()
    path = _registry_path(root, registry_path)
    loaded = load_agent_registry(root, path)
    if loaded.get("product_projection"):
        raise ValueError("Generated product versions are immutable; register and rebuild in the development repository")
    registry = migrate_registry(root, loaded, legacy_order=legacy_order)
    stamp = registration_date or date.today().strftime("%Y%m%d")
    if not re.fullmatch(r"\d{8}", stamp):
        raise ValueError("registration date must be YYYYMMDD")
    datetime.strptime(stamp, "%Y%m%d")
    if alias is not None and (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", alias)
                              or alias in registry["aliases"] or alias in registry["domain_versions"]):
        raise ValueError(f"invalid or existing legacy alias: {alias}")
    snapshots = {line: discover_files(root, line, scope=registry["scope"], registry_path=path)
                 for line in ("runtime", "domain")}
    previous = registry["domain_versions"][registry["current_domain_version"]]
    domain_changed = snapshots["domain"] != previous["files"] or previous.get("coverage") != "automatic_directories"
    if fingerprints is None:
        fingerprints = _fingerprints(root, registry) if domain_changed else {
            "tool_catalog_sha256": previous["tool_catalog_sha256"], "mode_fingerprints": previous["mode_fingerprints"]}
    changed_modes = sorted(key for key in (previous.get("mode_fingerprints") or {}).keys() | fingerprints["mode_fingerprints"].keys()
                           if (previous.get("mode_fingerprints") or {}).get(key) != fingerprints["mode_fingerprints"].get(key))
    for line, before in snapshots.items():
        if before != discover_files(root, line, scope=registry["scope"], registry_path=path):
            raise ValueError("checkout changed during registration; retry after edits finish")
    changed = []
    for line in ("runtime", "domain"):
        records = registry[f"{line}_versions"]
        old = records.get(registry[f"current_{line}_version"], {})
        if (old.get("files") == snapshots[line] and old.get("coverage") == "automatic_directories"
                and (line == "runtime" or not changed_modes)):
            continue
        number = max((int(re.match(rf"{line}-v(\d+)-", key)[1]) for key in records), default=0) + 1
        version = f"{line}-v{number}-{stamp}"
        record = {"source_commit": source_commit(root), "coverage": "automatic_directories", "files": snapshots[line]}
        if line == "domain":
            record.update(fingerprints)
        records[version] = record
        registry[f"current_{line}_version"] = version
        changed.append(line)
    if alias is not None:
        registry["aliases"][alias] = registry["current_domain_version"]
    if changed or alias is not None or loaded["schema_version"] != _SCHEMA:
        _write_registry(path, registry)
    return {"runtime_version": registry["current_runtime_version"],
            "domain_version": registry["current_domain_version"], "changed_lines": changed,
            "changed_modes": changed_modes, "source_commit": source_commit(root),
            "file_counts": {line: len(files) for line, files in snapshots.items()}}


def register_agent_version(root, version_id, *, registry_path=None, catalog_hashes=None,
                           additional_files=None, make_current=True):
    """Compatibility name: version_id is now an alias, files are automatic."""
    if not make_current or additional_files or catalog_hashes is not None:
        raise ValueError("use automatic registration with complete domain fingerprints")
    result = register_versions(root, registry_path=registry_path, alias=version_id)
    return agent_version_record(root, result["domain_version"], registry_path=registry_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Register or verify runtime and domain releases")
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register", help="append changed lines; no change means no new number")
    register.add_argument("--date", help="registration date YYYYMMDD (default: local today)")
    register.add_argument("--version", help="optional legacy alias")
    register.add_argument("--legacy-order", type=Path, help="migration JSON; otherwise read Git introductions")
    verify = commands.add_parser("verify", help="verify both releases or look up a historical alias")
    verify.add_argument("--version")
    verify.add_argument("--lookup", action="store_true", help="read metadata without verifying checkout")
    for command in (register, verify):
        command.add_argument("--root", type=Path, default=Path.cwd())
        command.add_argument("--registry", type=Path)
    args = parser.parse_args(argv)
    if args.command == "register":
        result = register_versions(args.root, registry_path=args.registry, registration_date=args.date,
            legacy_order=json.loads(args.legacy_order.read_text(encoding="utf-8"))["legacy_order"] if args.legacy_order else None,
            alias=args.version)
    else:
        record = agent_version_record(args.root, args.version, registry_path=args.registry, verify=not args.lookup)
        result = {"version_id": record["version_id"], "domain_version": record["version_id"],
                  "legacy_version_id": record.get("legacy_version_id"), "source_commit": record["source_commit"]}
        if not args.lookup and not args.version:
            result["runtime_version"] = release_records(args.root, registry_path=args.registry)["runtime"]["version_id"]
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
