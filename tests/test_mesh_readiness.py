from __future__ import annotations

import json
from pathlib import Path
import socket
import struct

import pytest

from scripts.dev import check_mesh_readiness as readiness


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _ref(path: Path, root: Path) -> dict:
    return {"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size,
            "sha256": readiness.sha256(path)}


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    audit, inputs, originals = (tmp_path / name for name in ("audit", "inputs", "originals"))
    for folder in (audit, inputs, originals):
        folder.mkdir()
    document = json.dumps({"asset": {"version": "2.0"}}).encode()
    document += b" " * (-len(document) % 4)
    glb = struct.pack("<4sIIII", b"glTF", 2, 20 + len(document), len(document), 0x4E4F534A) + document
    source, mesh = originals / "input.glb", inputs / "building.glb"
    source.write_bytes(glb)
    mesh.write_bytes(glb)
    conversion = audit / "conversion_only.json"
    _write(conversion, {"output_sha256": readiness.sha256(mesh)})
    from PIL import Image
    screenshot = audit / "reviewed.png"
    Image.new("RGB", (4, 4), "white").save(screenshot)
    config = json.loads((readiness.ROOT / "product/config.mesh.example.json").read_bytes())
    config.update(input_root=str(inputs), run_root=str(tmp_path / "runs"))
    case = config["cases"][0]
    case.update(case_id="frozen", input="building.glb", output="first-run")
    case.pop("auxiliary_images")
    case.pop("building_input")
    configuration = audit / "configuration.json"
    _write(configuration, config)
    manifest = {
        "schema_version": 1, "live_launch_authorized": False, "case_id": "frozen",
        "input_root": str(inputs), "admitted_files": [_ref(mesh, inputs)],
        "configuration": _ref(configuration, audit),
        "provenance": {"original_glb": str(source), "conversion_only": _ref(conversion, audit)},
        "case_screening": {"status": "accepted", "reviewer": "offline test fixture",
                           "mesh_sha256": readiness.sha256(mesh),
                           "summary": "synthetic review binding only", "reviewed_images": [_ref(screenshot, audit)]},
        "dependency_lock": {"sha256": readiness.sha256(readiness.ROOT / "uv.lock")},
        "preflight_script": {"sha256": readiness.sha256(Path(readiness.__file__))},
        "expected_versions": {"runtime": "runtime-test", "domain": "domain-test"},
    }
    path = audit / "readiness.json"
    _write(path, manifest)
    calls = []

    def releases(root, *, verify):
        calls.append((root, verify))
        return {line: {"version_id": value} for line, value in manifest["expected_versions"].items()}

    monkeypatch.setattr(readiness, "release_records", releases)
    # Any accidental network use is a hard failure, not a skipped integration.
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("preflight attempted network"))
    return path, manifest, config, calls


def test_preflight_uses_normal_entry_and_shared_limits_without_launch(prepared):
    path, _, _, calls = prepared
    report = readiness.check_readiness(path)
    assert report["status"] == "ready_for_separate_launch_approval"
    assert report["no_model_calls"] and not report["live_launch_authorized"]
    assert report["normal_runtime_argv"][1:3] == ["-m", "src.agent.runtime_entry"]
    assert report["mesh_workers_max_concurrent"] == 8
    assert report["shared_root_limits"]["model_calls"] == 48
    assert report["shared_root_limits"]["tool_calls"] == 240
    assert report["shared_root_limits"]["seconds"] is None
    assert report["shared_root_limits"]["tokens"] is None
    assert calls == [(readiness.ROOT, True)]
    assert not Path(report["run_output"]).exists()


def test_extra_answer_is_rejected_before_its_bytes_are_read(prepared, monkeypatch):
    path, manifest, _, _ = prepared
    answer = Path(manifest["input_root"]) / "historical_GT_answer.json"
    answer.write_text("do not inspect this content", encoding="utf-8")
    read = Path.open

    def guarded(self, *args, **kwargs):
        assert self != answer, "unexpected input contents were read"
        return read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    with pytest.raises(ValueError, match="allowlist"):
        readiness.check_readiness(path)


@pytest.mark.parametrize("target", ["mesh", "configuration", "review_image", "original", "conversion"])
def test_frozen_artifact_changes_fail(prepared, target):
    path, manifest, _, _ = prepared
    targets = {"mesh": Path(manifest["input_root"]) / "building.glb",
               "configuration": path.parent / "configuration.json",
               "review_image": path.parent / "reviewed.png",
               "original": Path(manifest["provenance"]["original_glb"]),
               "conversion": path.parent / "conversion_only.json"}
    with targets[target].open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError, match="(size|hash) changed"):
        readiness.check_readiness(path)


@pytest.mark.parametrize("status", ["pending", "rejected"])
def test_visual_screening_is_not_inferred_from_valid_bytes(prepared, status):
    path, manifest, _, _ = prepared
    manifest["case_screening"]["status"] = status
    _write(path, manifest)
    with pytest.raises(ValueError, match="screening pending or rejected"):
        readiness.check_readiness(path)


def test_visual_screening_cannot_be_reused_for_another_mesh(prepared):
    path, manifest, _, _ = prepared
    manifest["case_screening"]["mesh_sha256"] = "0" * 64
    _write(path, manifest)
    with pytest.raises(ValueError, match="does not bind"):
        readiness.check_readiness(path)


@pytest.mark.parametrize("change,reason", [
    ("credentials", "no credentials"), ("bad_model", "model"),
    ("existing_output", "already exists"), ("input_escape", "escapes"),
    ("registry", "versions differ"), ("unsealed", "seal pending"),
    ("lock", "hash changed"), ("script", "hash changed"),
])
def test_preflight_rejects_bad_configuration_or_release(prepared, monkeypatch, change, reason):
    path, manifest, config, _ = prepared
    case = config["cases"][0]
    if change == "credentials":
        case["api_key"] = "fixture-not-a-secret"
    elif change == "bad_model":
        case["model"] = "not-a-model"
    elif change == "existing_output":
        (Path(config["run_root"]) / case["output"]).mkdir(parents=True)
    elif change == "input_escape":
        case["input"] = "../originals/input.glb"
    elif change == "registry":
        monkeypatch.setattr(readiness, "release_records", lambda *a, **k: {
            "runtime": {"version_id": "other"}, "domain": {"version_id": "other"}})
    elif change == "unsealed":
        manifest["expected_versions"] = None
    elif change == "lock":
        manifest["dependency_lock"]["sha256"] = "0" * 64
    elif change == "script":
        manifest["preflight_script"]["sha256"] = "0" * 64
    _write(path.parent / "configuration.json", config)
    manifest["configuration"] = _ref(path.parent / "configuration.json", path.parent)
    _write(path, manifest)
    with pytest.raises(ValueError, match=reason):
        readiness.check_readiness(path)


def test_symlink_input_root_is_rejected(prepared):
    path, manifest, _, _ = prepared
    link = path.parent.parent / "input-link"
    try:
        link.symlink_to(Path(manifest["input_root"]), target_is_directory=True)
    except OSError:
        pytest.skip("host does not permit creation of test symlinks")
    manifest["input_root"] = str(link)
    _write(path, manifest)
    with pytest.raises(ValueError, match="symlink/reparse"):
        readiness.check_readiness(path)
