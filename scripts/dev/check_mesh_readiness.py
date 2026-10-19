"""Read-only preflight for an isolated, frozen, original-GLB experiment.

This command never creates a provider, opens credentials, starts a run, or
grants launch approval. The normal runtime entry remains the launch authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.geometry.mesh_observation import _validate_self_contained_glb
from src.agent.runtime_configuration import (
    argv_for, load_configuration, mesh_worker_concurrency, resolve_case_input,
    resolve_supplemental_inputs, selected_case,
)
from src.agent_runtime.agent_registry import release_records
from src.agent_runtime.run_paths import resolve_run_output


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _plain_path(path: Path) -> Path:
    """Check lexical ancestors before resolve can hide a junction or symlink."""
    if not path.is_absolute():
        raise ValueError(f"absolute path required: {path}")
    for item in (path, *path.parents):
        if not item.exists() and not item.is_symlink():
            continue
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"symlink/reparse point is not admitted: {item}")
    return path.resolve()


def _separate(a: Path, b: Path) -> bool:
    return not (a.is_relative_to(b) or b.is_relative_to(a))


def _relative(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or Path(name).is_absolute() or ".." in Path(name).parts:
        raise ValueError("artifact names must be relative paths without parent traversal")
    target = _plain_path(root / name)
    if not target.is_relative_to(root):
        raise ValueError("artifact escapes its root")
    return target


def _match(path: Path, expected: dict) -> None:
    if not path.is_file():
        raise ValueError(f"missing frozen file: {path}")
    if expected.get("bytes") is not None and path.stat().st_size != expected["bytes"]:
        raise ValueError(f"frozen file size changed: {path}")
    if sha256(path) != expected.get("sha256"):
        raise ValueError(f"frozen file hash changed: {path}")


def _files(root: Path) -> set[str]:
    # Enumerate names only first. An unexpected answer file is never opened.
    found: set[str] = set()
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in [*directories, *files]:
            _plain_path(Path(directory) / name)
        found.update((Path(directory) / name).relative_to(root).as_posix() for name in files)
    return found


def _no_credentials(value: object) -> None:
    forbidden = {"api_key", "access_token", "refresh_token", "password", "authorization",
                 "credentials_file", "chatgpt_auth_dir", "token", "secret"}
    if isinstance(value, dict):
        if any(str(key).lower() in forbidden for key in value):
            raise ValueError("readiness configuration must contain no credentials or credential locations")
        for item in value.values():
            _no_credentials(item)
    elif isinstance(value, list):
        for item in value:
            _no_credentials(item)


def check_readiness(manifest_path: Path, *, repository_root: Path = ROOT) -> dict:
    """Validate bytes and ordinary configuration, without initiating execution."""
    manifest_path = _plain_path(manifest_path.absolute())
    audit = manifest_path.parent
    repository = _plain_path(repository_root.absolute())
    manifest = json.loads(manifest_path.read_bytes())
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported readiness manifest schema")
    if manifest.get("live_launch_authorized") is not False:
        raise ValueError("preparation manifest must record live_launch_authorized=false")
    screening = manifest.get("case_screening", {})
    if (screening.get("status") != "accepted" or not screening.get("reviewer")
            or not screening.get("summary") or not screening.get("reviewed_images")):
        raise ValueError("input screening pending or rejected; an explicit visual review is required")
    for item in screening["reviewed_images"]:
        path = _relative(audit, item["path"])
        if path.suffix.lower() != ".png":
            raise ValueError("visual review evidence must name PNG files")
        _match(path, item)
    inputs = _plain_path(Path(manifest["input_root"]))
    if not inputs.is_dir() or inputs == Path(inputs.anchor):
        raise ValueError("input_root must be an existing dedicated directory")
    if not all((_separate(inputs, audit), _separate(inputs, repository), _separate(audit, repository))):
        raise ValueError("inputs, audit and repository must be separate directories")
    admitted = manifest.get("admitted_files")
    if not isinstance(admitted, list) or len(admitted) != 1 or Path(admitted[0]["path"]).suffix.lower() != ".glb":
        raise ValueError("this readiness contract admits exactly one original GLB")
    if screening.get("mesh_sha256") != admitted[0].get("sha256"):
        raise ValueError("visual screening does not bind the admitted mesh bytes")
    expected_names = {admitted[0]["path"]}
    if _files(inputs) != expected_names:
        raise ValueError("admitted input file allowlist differs; no extra files, including answers, are permitted")
    mesh = _relative(inputs, admitted[0]["path"])
    _match(mesh, admitted[0])
    _validate_self_contained_glb(mesh)

    config_ref = manifest["configuration"]
    config_path = _relative(audit, config_ref["path"])
    _match(config_path, config_ref)
    raw_config = json.loads(config_path.read_bytes())
    _no_credentials(raw_config)
    if len(raw_config.get("cases", [])) != 1:
        raise ValueError("freeze exactly one case per readiness manifest")
    configuration = load_configuration(config_path)
    case = selected_case(configuration, manifest["case_id"])
    if case["mode"] != "single_model" or case["input_kind"] != "mesh":
        raise ValueError("readiness requires native single_model mesh execution")
    if _plain_path(Path(case["input_root"])) != inputs or resolve_case_input(case) != mesh:
        raise ValueError("configuration does not select the frozen input")
    if resolve_supplemental_inputs(case) != (None, None) or case.get("floor_plan_images"):
        raise ValueError("this experiment admits no auxiliary drawings, declarations or floor plans")
    if case.get("continuation") is not None or case.get("reader_task") is not None:
        raise ValueError("first experiment cannot inherit an earlier run")
    run_root = _plain_path(Path(case["run_root"]))
    if not all(_separate(run_root, other) for other in (inputs, audit, repository)):
        raise ValueError("run_root must be separate from inputs, audit and repository")
    output = resolve_run_output(Path(case["output"]), repository_root=repository, run_root=run_root)
    _plain_path(output)
    if output.exists():
        raise ValueError("first experiment output already exists; do not reuse historical run contents")

    provenance = manifest["provenance"]
    original = _plain_path(Path(provenance["original_glb"]))
    _match(original, admitted[0])
    conversion_ref = provenance["conversion_only"]
    conversion_path = _relative(audit, conversion_ref["path"])
    _match(conversion_path, conversion_ref)
    conversion = json.loads(conversion_path.read_bytes())
    if conversion.get("output_sha256") != admitted[0]["sha256"]:
        raise ValueError("original conversion metadata does not bind the admitted GLB")
    for item in manifest.get("audit_artifacts", []):
        _match(_relative(audit, item["path"]), item)
    _match(repository / "uv.lock", manifest["dependency_lock"])
    _match(Path(__file__).resolve(), manifest["preflight_script"])
    expected_versions = manifest.get("expected_versions")
    if not expected_versions:
        raise ValueError("release seal pending; pin verified runtime and domain versions before launch")
    records = release_records(repository, verify=True)
    versions = {line: records[line]["version_id"] for line in ("runtime", "domain")}
    if versions != expected_versions:
        raise ValueError("registered runtime/domain versions differ from the reviewed preparation")
    command = argv_for(case)
    return {
        "status": "ready_for_separate_launch_approval", "no_model_calls": True,
        "live_launch_authorized": False, "case_id": case["case_id"], "mesh_sha256": sha256(mesh),
        "admitted_files": sorted(expected_names), "versions": versions,
        "model": case["model"], "provider": case["provider"],
        "reasoning_effort": case.get("reasoning_effort"),
        "mesh_workers_max_concurrent": mesh_worker_concurrency(case),
        "shared_root_limits": case["limits"], "per_response_output_tokens": case["output_tokens"],
        "run_output": str(output), "normal_runtime_argv": command,
        "scope": "File allowlist and original-asset byte identity; no semantic GT detection or BIM quality approval.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)
    try:
        report = check_readiness(args.manifest)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "not_ready", "no_model_calls": True, "reason": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
