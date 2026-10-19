import json

from scripts.tool_scripts import bim_agent_continuation
from scripts.tool_scripts.bim_agent_guidance import (
    CORE, DELIVERY, DRAWING_METHOD, FINISHING, IMAGE_KINDS, MESH_CORE, MESH_GUIDE, MESH_METHOD, MESH_VIEWS, PHOTOS, REFERENCES, TOOLS,
    build_guide)
from scripts.tool_scripts.run_bim_agent import run_guide


def test_system_prompt_carries_only_the_methods_for_the_declared_inputs():
    drawings = build_guide(images="drawings")
    assert drawings.startswith(CORE) and DRAWING_METHOD in drawings
    assert all(text not in drawings for text in (MESH_VIEWS, PHOTOS, IMAGE_KINDS))
    photos = build_guide(images="photos")
    assert PHOTOS in photos and DRAWING_METHOD not in photos
    unknown = build_guide(images="unknown")
    assert all(text in unknown for text in (IMAGE_KINDS, DRAWING_METHOD, MESH_VIEWS, PHOTOS))
    assert "none that is not drawn" in DRAWING_METHOD and "not drawn" not in CORE
    assert all(build_guide(images=kind).endswith(TOOLS + "\n" + DELIVERY)
               for kind in ("drawings", "photos", "unknown"))


def test_mesh_inputs_keep_the_partial_inference_method_and_filter_capabilities():
    # Mesh inputs select their own priorities/frame and method, including when
    # auxiliary images are labelled drawings. The shared tools/delivery remain.
    assert "get_bim_reference('partial_inference')" in MESH_GUIDE and DRAWING_METHOD not in MESH_GUIDE
    assert all(build_guide(images=kind, mesh=mesh, review_detail=True, continuation=True) == MESH_GUIDE
               for kind, mesh in (("mesh_views", False), ("unknown", True), (None, True), ("drawings", True)))
    disabled = build_guide(mesh=True)
    assert disabled.startswith(MESH_CORE) and CORE not in disabled and MESH_METHOD in disabled
    assert disabled.endswith(TOOLS + "\n" + DELIVERY)
    assert "review_detail" not in disabled and "does not reset it" not in disabled
    assert "get_bim_reference('partial_inference')" in disabled


def test_run_guide_follows_the_manifest_and_legacy_runs_keep_their_old_meaning(tmp_path):
    def guide(**manifest):
        (tmp_path / "inputs.json").write_text(json.dumps(manifest))
        return run_guide(tmp_path)
    assert guide(images={"a.png": {}}, image_kind="photos") == build_guide(images="photos")
    assert guide(images={"a.png": {}}) == build_guide(images="drawings")
    assert guide(images={"a.png": {}}, mesh_input={"sha256": "x"}) == build_guide(images="unknown", mesh=True)
    assert guide(images={}, mesh_input={"sha256": "x"}) == build_guide(mesh=True)


def test_reconstruction_reference_is_the_single_drawing_method():
    assert REFERENCES["reconstruction"] is DRAWING_METHOD


def test_finishing_rule_has_one_source_for_delivery_and_continuation():
    assert FINISHING in DELIVERY


def test_room_use_and_note_formats_are_given_where_delivery_needs_them():
    assert '"op":"set_space_role"' in DELIVERY and '"op":"replace_note"' in DELIVERY
    assert "Thickness" not in REFERENCES["claims"] and "replace_note" not in REFERENCES["claims"]
    assert "set_component_thickness" in REFERENCES["wall_dimensions"]
    assert '"op":"replace_note"' in REFERENCES["edits"]
