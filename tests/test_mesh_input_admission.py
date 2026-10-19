"""Mixed mesh inputs reuse the public runtime and keep a bounded frozen inventory."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image
import numpy as np
import pytest
import trimesh

from src.agent import runtime_configuration, runtime_entry
from src.agent.bim_inputs import prepare_bim_inputs


def _case(tmp_path):
    inputs = tmp_path / "inputs"
    images = inputs / "sketches"
    images.mkdir(parents=True)
    Image.new("RGB", (12, 8), "white").save(images / "1f.png")
    mesh = trimesh.creation.box()
    mesh.visual = trimesh.visual.TextureVisuals(
        uv=np.zeros((len(mesh.vertices), 2)), image=Image.new("RGB", (4, 4), "gray"))
    (inputs / "building.glb").write_bytes(trimesh.Scene(mesh).export(file_type="glb"))
    (inputs / "declaration.json").write_bytes(
        b'{\n  "use": "user suspects hotel",\n  "plan_path": "sketches/1f.png",\n'
        b'  "unprovided_path": "../evaluation/ground_truth.json"\n}\n')
    (inputs / "ground_truth.json").write_text("never read or copy", encoding="utf-8")
    (images / "evaluation.json").write_text("never read or copy", encoding="utf-8")
    return {
        "case_id": "mesh-mixed", "mode": "single_model",
        "provider": "glm-subscription-anthropic", "model": "glm-5.3-flash",
        "credentials_file": "private.env", "reasoning_effort": "medium",
        "output_tokens": 32000, "max_candidates": 2,
        "input_root": str(inputs), "input_kind": "mesh", "input": "building.glb",
        "auxiliary_images": "sketches", "building_input": "declaration.json",
        "image_kind": "drawings", "floor_plan_images": ["1f.png"],
        "scope": "Exterior from mesh; sketch supplies relationships only.",
        "run_root": str(tmp_path / "runs"), "output": "mesh-mixed",
        "limits": {"model_calls": 2, "tool_calls": 4, "seconds": 60, "tokens": None},
    }


def _load(tmp_path, case):
    path = tmp_path / "configuration.json"
    path.write_text(json.dumps({
        "schema_version": 1, "approval_required_before_launch": True,
        "maximum_seconds_per_case": 60, "cases": [case],
    }), encoding="utf-8")
    return runtime_configuration.load_configuration(path)["cases"][0]


def test_mixed_configuration_reaches_shared_runtime_and_freezes_only_authorized_inputs(tmp_path, monkeypatch):
    case = _case(tmp_path)
    inputs = Path(case["input_root"])
    original_read = Path.read_bytes

    def guarded_read(path):
        assert path.name not in {"ground_truth.json", "evaluation.json"}
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    loaded = _load(tmp_path, case)
    command = runtime_configuration.argv_for(loaded)
    assert command[2] == "src.agent.runtime_entry"
    args = runtime_entry.parser().parse_args(command[3:])
    assert args.mesh == inputs / "building.glb"
    assert args.images == inputs / "sketches"
    assert args.building_input == inputs / "declaration.json"
    assert args.floor_plan_images == ["1f.png"]
    run, _, _ = runtime_entry.prepare_inputs(
        args.out, images=args.images, mesh=args.mesh, building_input=args.building_input,
        scope=args.scope, image_kind=args.image_kind, max_candidates=args.max_candidates,
        floor_plan_images=args.floor_plan_images)
    manifest = json.loads((run / "inputs.json").read_bytes())
    assert manifest["source_input_mode"] == "native_mesh_with_images_with_building_declaration"
    assert "lite_bim_grid_step_m" not in manifest
    assert manifest["input_contents"]["ground_truth_or_evaluation"] == {"included": False}
    assert set(manifest["images"]) == {"1f.png"}
    assert manifest["floor_plan_images"] == ["1f.png"]
    for source, frozen, expected_sha in (
        (args.mesh, run / "assets/input.glb", manifest["mesh_input"]["sha256"]),
        (args.images / "1f.png", run / "images/1f.png", manifest["images"]["1f.png"]["sha256"]),
        (args.building_input, run / "building_input.json", manifest["building_input"]["raw_sha256"]),
    ):
        assert source.read_bytes() == frozen.read_bytes()
        assert hashlib.sha256(frozen.read_bytes()).hexdigest() == expected_sha
        source.write_bytes(b"changed after admission")
        assert hashlib.sha256(frozen.read_bytes()).hexdigest() == expected_sha
    assert not list(run.rglob("*truth*"))
    assert not list(run.rglob("evaluation.json"))
    paths = manifest["building_input"]["image_path_associations"]
    assert paths[0]["authorized_image"] == "1f.png"
    assert paths[1]["status"] == "not_in_authorized_image_inventory"


@pytest.mark.parametrize("field", ["auxiliary_images", "building_input"])
@pytest.mark.parametrize("escape", ["parent", "symlink"])
def test_supplemental_inputs_cannot_escape_configured_root(tmp_path, field, escape):
    case = _case(tmp_path)
    outside = tmp_path / "outside"
    if field == "auxiliary_images":
        outside.mkdir()
        Image.new("RGB", (2, 2)).save(outside / "plan.png")
    else:
        outside.write_text("{}", encoding="utf-8")
    if escape == "parent":
        case[field] = "../outside"
    else:
        link = Path(case["input_root"]) / "linked"
        link.symlink_to(outside, target_is_directory=outside.is_dir())
        case[field] = "linked"
    with pytest.raises(ValueError, match="escapes.*input_root"):
        _load(tmp_path, case)


@pytest.mark.parametrize("invalid", ["corrupt", "renamed_jpeg", "escaping_link"])
def test_auxiliary_png_rejected_by_configuration_and_direct_admission(tmp_path, invalid):
    case = _case(tmp_path)
    images = Path(case["input_root"]) / case["auxiliary_images"]
    image = images / "1f.png"
    expected = "valid readable PNG"
    if invalid == "corrupt":
        image.write_bytes(b"broken PNG")
    elif invalid == "renamed_jpeg":
        Image.new("RGB", (2, 2)).save(image, format="JPEG")
    else:
        image.unlink()
        outside = tmp_path / "outside.png"
        Image.new("RGB", (2, 2)).save(outside)
        image.symlink_to(outside)
        expected = "escapes its admitted case"
    with pytest.raises(ValueError, match=expected):
        _load(tmp_path, case)
    with pytest.raises(ValueError, match=expected):
        prepare_bim_inputs(tmp_path / "rejected-run", images_path=images,
            mesh_path=Path(case["input_root"]) / "building.glb", building_input_path=None,
            scope="mixed", image_kind="drawings", max_candidates=1)
    assert not (tmp_path / "rejected-run").exists()


@pytest.mark.parametrize("raw", [b"[]", b"not-json", '{"use":"hotel"}'.encode("utf-16")])
def test_building_input_validated_before_configuration_can_launch(tmp_path, raw):
    case = _case(tmp_path)
    (Path(case["input_root"]) / case["building_input"]).write_bytes(raw)
    with pytest.raises(ValueError, match="building input"):
        _load(tmp_path, case)


def test_floor_scope_is_bound_to_admitted_auxiliary_filenames(tmp_path):
    case = _case(tmp_path)
    case["floor_plan_images"] = ["missing.png"]
    with pytest.raises(ValueError, match="admitted input filenames"):
        _load(tmp_path, case)


@pytest.mark.parametrize("field,mode,kind", [
    ("auxiliary_images", "single_model", "images"),
    ("auxiliary_images", "external_coordinator_mcp", "mesh"),
    ("building_input", "external_coordinator_mcp", "mesh"),
])
def test_supplemental_fields_are_not_silently_ignored_on_unsupported_routes(tmp_path, field, mode, kind):
    case = _case(tmp_path)
    case.update(mode=mode, input_kind=kind)
    for other in {"auxiliary_images", "building_input"} - {field}:
        case.pop(other)
    with pytest.raises(ValueError, match=field):
        runtime_configuration.resolve_supplemental_inputs(case)


@pytest.mark.parametrize("mode", ["single_model", "role_division"])
def test_declared_building_input_also_reaches_existing_drawing_runtime(tmp_path, mode):
    case = _case(tmp_path)
    case.update(mode=mode, input_kind="images", input="sketches")
    case.pop("auxiliary_images")
    parser = runtime_entry.parser
    if mode == "role_division":
        from src.agent.runtime_roles import entry
        parser = entry.parser
        route = {key: case[key] for key in ("provider", "model", "reasoning_effort", "output_tokens")}
        case["roles"] = {name: dict(route) for name in ("coordinator", "plan_reader", "elevation_reader")}
    args = parser().parse_args(runtime_configuration.argv_for(_load(tmp_path, case))[3:])
    assert args.mesh is None
    assert args.building_input == Path(case["input_root"]) / "declaration.json"
