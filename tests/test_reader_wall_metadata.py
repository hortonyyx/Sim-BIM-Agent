"""Wall evidence survives 2D/3D assembly without moving or borrowing walls."""
import asyncio
import copy
import hashlib
import json

import pytest
from PIL import Image

from scripts.tool_scripts.render_geometry_viewer import build_viewer_html, wall_properties
from src.agent.correction.parse import ensure_corrected_geometry
from src.agent.execution.source_proposal import export_source_proposal
from src.agent.geometry.component_attributes import thickness_record, validate_component_attributes
from src.agent.geometry.plan_assembly import assemble_plan_proposals
from src.agent.geometry.plan_partition import compile_plan_partition
from src.agent.geometry.reader_wall_metadata import (
    bind_wall_metadata, bind_wall_readings, validate_wall_metadata, wall_reading_input, require_wall_thickness,
)
from src.agent.geometry.source_bim import build_source_bim, source_view_geometry
from src.agent.geometry.source_model import _digest
from tests.test_plan_partition import _plan


def _known(target="partition:P_vertical", thickness=.2):
    return dict(target=target, thickness_m=thickness, reference_side="negative_face",
                evidence_status="observed", basis="200 mm dimension at the selected wall",
                source_refs=["plan:wall-dimension"])


@pytest.fixture
def example():
    plan = _plan()
    proposal, metadata = compile_plan_partition(plan, image_size=(100, 100), image_name="plan.png")
    source = build_source_bim(ensure_corrected_geometry(proposal["geometry"]),
                              capability_profile="orthogonal_polygon")
    plan["wall_metadata"] = [_known()]
    return plan, proposal, metadata, source


def test_missing_thickness_is_explicit_unknown_and_known_needs_evidence():
    plan = _plan()
    before = copy.deepcopy(plan)
    rows = validate_wall_metadata(plan)
    assert plan == before
    assert len(rows) == 6  # Four authored footprint sides and two partitions.
    assert all(r["thickness_m"] is None and r["evidence_status"] == "unknown" for r in rows)
    assert all(r["reference_side"] == "unknown" and r["source_refs"] == [] for r in rows)
    plan["wall_metadata"] = [_known()]
    assert next(r for r in validate_wall_metadata(plan) if r["target"] == "partition:P_vertical") == _known()


@pytest.mark.parametrize("change", [
    {"thickness_m": 0}, {"thickness_m": -1}, {"thickness_m": True}, {"thickness_m": float("nan")},
    {"thickness_m": None}, {"evidence_status": "unknown"}, {"source_refs": []},
    {"basis": ""}, {"reference_side": "left"}, {"target": "partition:missing"},
])
def test_invalid_thickness_or_unlocatable_wall_is_rejected(change):
    plan = _plan()
    plan["wall_metadata"] = [{**_known(), **change}]
    with pytest.raises(ValueError):
        validate_wall_metadata(plan)


def test_segment_override_inherits_remaining_whole_partition_values():
    plan = _plan()
    plan["wall_metadata"] = [_known("partition:P_vertical:0")]
    rows = validate_wall_metadata(plan)
    assert next(r for r in rows if r["target"] == "partition:P_vertical:1")["thickness_m"] is None
    assert all(r["target"] != "partition:P_vertical" for r in rows)
    plan["wall_metadata"].append(_known())
    rows = validate_wall_metadata(plan)
    assert all(r["thickness_m"] == .2 for r in rows if r["target"].startswith("partition:P_vertical"))


def test_t_junction_binds_each_partial_side_without_changing_geometry(example):
    plan, _, metadata, source = example
    before = copy.deepcopy((plan, metadata, source))
    patch = bind_wall_metadata(source, plan, metadata)
    assert (plan, metadata, source) == before
    known = patch["component_attributes"]
    assert len(known) == 3  # One long side and two separate room sides.
    assert all(r["thickness_m"] == .2 and r["host_scope"] == "boundary" for r in known)
    assert all(r["geometry_effect"] == "none" for r in known)
    assert all(r["offsets_m"] is None for r in patch["wall_references"])
    assert validate_component_attributes(source, known) == known
    boundaries = {r["id"]: r for r in source["boundaries"]}
    assert all({p[0] for p in boundaries[r["boundary_ids"][0]]["vertices"]} == {5.0} for r in known)
    changed = copy.deepcopy(source)
    target = next(b for b in changed["boundaries"] if b["id"] == known[0]["boundary_ids"][0])
    target["vertices"][0][2] += .1
    with pytest.raises(ValueError, match="host changed"):
        validate_component_attributes(changed, known)
    # Existing component-wide edits still reject partial counterpart spreading.
    with pytest.raises(ValueError, match="partial contacts"):
        thickness_record(source, known[0]["boundary_ids"][0], .2, basis="test", source_refs=["test"])


def test_authored_footprint_edge_and_world_side_do_not_follow_ring_orientation(example):
    plan, _, metadata, source = example
    plan["wall_metadata"] = [_known("footprint:0", .3)]
    patch = bind_wall_metadata(source, plan, metadata)
    boundaries = {r["id"]: r for r in source["boundaries"]}
    assert patch["component_attributes"]
    assert all({p[1] for p in boundaries[r["boundary_ids"][0]]["vertices"]} == {8.9}
               for r in patch["component_attributes"])
    plan["wall_metadata"] = [_known()]
    baseline = bind_wall_metadata(source, plan)
    plan["partitions"][0]["points"].reverse()
    assert bind_wall_metadata(source, plan) == baseline


@pytest.mark.parametrize("x_sign", [-1, 1])
@pytest.mark.parametrize("y_sign", [-1, 1])
@pytest.mark.parametrize("reverse_ring", [False, True])
@pytest.mark.parametrize("reverse_anchors", [False, True])
@pytest.mark.parametrize("alias", ["outer_face", "inner_face"])
def test_perimeter_face_aliases_follow_world_interior_on_concave_edges(
        x_sign, y_sign, reverse_ring, reverse_anchors, alias):
    # Expectations are explicit world-space outward normals of an L footprint;
    # its reentrant vertical face is at x=4, not at the bounding box's max x=7.
    world_ring = [[1, 1], [7, 1], [7, 3], [4, 3], [4, 6], [1, 6]]
    outer_sides = ["negative_face", "positive_face", "positive_face",
                   "positive_face", "positive_face", "negative_face"]
    expected = {frozenset((tuple(a), tuple(b))): side
                for a, b, side in zip(world_ring, world_ring[1:] + world_ring[:1], outer_sides)}
    if reverse_ring:
        world_ring.reverse()
    plan = _plan()
    plan.update(
        x_anchors=[[50, 0], [50 + x_sign * 40, 8]],
        y_anchors=[[50, 0], [50 + y_sign * 40, 8]],
        footprint_pixels=[[50 + x_sign * x * 5, 50 + y_sign * y * 5] for x, y in world_ring],
        partitions=[], openings=[], space_seeds=[],
        wall_metadata=[dict(_known(f"footprint:{i}"), reference_side=alias,
                            evidence_status="inferred") for i in range(len(world_ring))])
    if reverse_anchors:
        plan["x_anchors"].reverse()
        plan["y_anchors"].reverse()
    before = copy.deepcopy(plan)

    rows = validate_wall_metadata(plan)

    assert plan == before
    for i, (a, b, row) in enumerate(zip(world_ring, world_ring[1:] + world_ring[:1], rows)):
        side = expected[frozenset((tuple(a), tuple(b)))]
        if alias == "inner_face":
            side = "positive_face" if side == "negative_face" else "negative_face"
        assert row == {**plan["wall_metadata"][i], "reference_side": side}


@pytest.mark.parametrize("target", ["partition:P_vertical", "partition:P_vertical:0"])
@pytest.mark.parametrize("alias", ["outer_face", "inner_face"])
def test_partition_face_aliases_are_rejected_without_unique_inside(target, alias):
    plan = _plan()
    plan["wall_metadata"] = [dict(_known(target), reference_side=alias)]
    with pytest.raises(ValueError, match="partition walls have no unique building interior side"):
        validate_wall_metadata(plan)


@pytest.mark.parametrize("ring,target", [
    ([[10, 10], [90, 90], [10, 90], [90, 10]], "footprint:0"),  # Self intersection.
    ([[10, 10], [50, 10], [90, 10]], "footprint:0"),  # Zero area.
    ([[10, 10], [90, 10], [90, 10], [90, 90], [10, 90]], "footprint:1"),  # Zero edge.
    ([[10, 10], [90, 20], [90, 90], [10, 90]], "footprint:0"),  # Oblique normal.
])
def test_ambiguous_or_nonorthogonal_perimeter_aliases_are_not_guessed(ring, target):
    plan = _plan()
    plan["footprint_pixels"] = ring
    plan["wall_metadata"] = [dict(_known(target), reference_side="outer_face")]
    with pytest.raises(ValueError, match="simple nonzero polygon|nonzero orthogonal segments"):
        validate_wall_metadata(plan)


def test_perimeter_alias_retains_raw_observation_beside_canonical_product_and_source(example, tmp_path):
    from src.agent.runtime_roles.plan_format import plan_format_errors
    from src.agent.runtime_roles.plan_reading import build_reading
    plan, _, _, source = example
    # The existing fixture has a repeated closing vertex and downward pixel Y.
    plan["wall_metadata"] = [dict(_known("footprint:0", .3), reference_side="outer_face"),
        dict(_known("footprint:*", .24), reference_side="outer_face"), _known("partition:*", .12)]
    before = copy.deepcopy(plan)
    assert plan_format_errors(plan) == []
    (tmp_path / "images").mkdir()
    Image.new("RGB", (100, 100), "white").save(tmp_path / "images/plan.png")

    result = build_reading(plan, workspace=tmp_path, image_name="plan.png", number=1)
    saved = json.loads((tmp_path / "reading_001/plan.json").read_text(encoding="utf-8"))
    product = json.loads((tmp_path / "reading_001/reading.json").read_text(encoding="utf-8"))
    reading = wall_reading_input(plan, product["mapping"])
    patch = bind_wall_readings(source, [reading])

    assert result["structuredContent"]["status"] == "passed"
    assert plan == before and saved == require_wall_thickness(before)
    assert product["mapping"]["wall_metadata"] == require_wall_thickness(before)["wall_metadata"]
    adopted = next(row for row in product["walls"] if row["target"] == "footprint:0")
    assert adopted == {**before["wall_metadata"][0], "reference_side": "positive_face"}
    assert next(row for row in reading["walls"] if row["target"] == "footprint:0")["reference_side"] == "positive_face"
    known = [row for row in patch["wall_references"] if row["thickness_m"] == .3]
    assert known and all(row["reference_side"] == "positive_face" for row in known)
    assert all(row["thickness_m"] == .3 and row["evidence_status"] == "observed" for row in known)
    assert all(row["reference_basis"] == before["wall_metadata"][0]["basis"] for row in known)
    assert all(row["source_refs"][:1] == before["wall_metadata"][0]["source_refs"] for row in known)


@pytest.mark.parametrize("declaration", ["quantity", "profile", "both"])
@pytest.mark.parametrize("alias", ["outer_face", "inner_face"])
def test_face_alias_waits_for_actual_trial_quantity_and_profile_resolution(tmp_path, declaration, alias):
    from src.agent.runtime_roles.plan_format import READER_PLAN_EXAMPLE, plan_format_errors
    from src.agent.runtime_roles.trial import PlanTrial
    plan = copy.deepcopy(READER_PLAN_EXAMPLE)
    plan["wall_metadata"].insert(0, dict(_known("footprint:0"), reference_side=alias))
    numeric = copy.deepcopy(plan)
    expected_side = next(row["reference_side"] for row in validate_wall_metadata(numeric)
                         if row["target"] == "footprint:0")
    (tmp_path / "images").mkdir()
    image = tmp_path / "images/plan.png"
    Image.new("RGB", (800, 600), "white").save(image)
    profiles = tmp_path / "pixel_profiles"
    profiles.mkdir()
    if declaration in {"quantity", "both"}:
        plan["x_anchors"][0][1] = {"value": numeric["x_anchors"][0][1] * 1000, "unit": "mm"}
    if declaration in {"profile", "both"}:
        pixels = [numeric["x_anchors"][0][0], numeric["footprint_pixels"][0][0]]
        profile = {"name": "plan.png", "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                   "axis": "x", "candidates": [
                       {"id": f"C0{i+1}", "pixels": [pixel, pixel], "peak": pixel}
                       for i, pixel in enumerate(pixels)]}
        (profiles / "profile_001.json").write_text(json.dumps(profile), encoding="utf-8")
        plan["x_anchors"][0][0] = {"profile": "profile_001", "candidate": "C01"}
        plan["footprint_pixels"][0][0] = {"profile": "profile_001", "candidate": "C02"}
    before = copy.deepcopy(plan)
    assert plan_format_errors(plan) == []
    # Deferral is exclusive to declaration preflight; a saved-product caller
    # cannot silently accept unresolved geometry by using the default API.
    with pytest.raises(ValueError, match="must be finite"):
        validate_wall_metadata(plan)
    trial = PlanTrial(None, image_name="plan.png", workspace=tmp_path,
                      receipt_directory=tmp_path / "trial_receipts", profile_directory=profiles)

    receipt = asyncio.run(trial.run(plan))

    assert receipt["status"] == "passed", receipt.get("reason")
    assert plan == before
    submitted = json.loads((tmp_path / "trial_receipts/trial_001_plan.json").read_bytes())
    resolved = json.loads((tmp_path / receipt["unaligned_numeric_input_file"]).read_bytes())
    product = json.loads((tmp_path / receipt["reading_product_file"]).read_bytes())
    assert submitted == before
    for field in ("x_anchors", "y_anchors", "footprint_pixels", "wall_metadata"):
        assert resolved[field] == numeric[field]
    assert bool(receipt["length_bindings"]) == (declaration in {"quantity", "both"})
    assert bool(receipt["measurement_bindings"]) == (declaration in {"profile", "both"})
    assert product["mapping"]["wall_metadata"] == require_wall_thickness(numeric)["wall_metadata"]
    adopted = next(row for row in product["walls"] if row["target"] == "footprint:0")
    assert adopted == {**numeric["wall_metadata"][0], "reference_side": expected_side}


def test_partial_thickness_and_nearby_wall_cannot_be_silently_spread(example):
    plan, _, metadata, source = example
    plan["wall_metadata"] = [_known("partition:P_vertical:0")]
    with pytest.raises(ValueError, match="different wall metadata"):
        bind_wall_metadata(source, plan, metadata)
    plan["wall_metadata"] = [_known()]
    shifted = copy.deepcopy(source)
    for wall in shifted["boundaries"]:
        if wall["geometry_type"] == "wall" and {p[0] for p in wall["vertices"]} == {5.0}:
            for point in wall["vertices"]:
                point[0] += .01
    with pytest.raises(ValueError, match="not fully covered"):
        bind_wall_metadata(shifted, plan, metadata)


def test_floor_assembly_rebinds_duplicate_local_ids_and_retains_values_on_reload(example, tmp_path):
    plan, proposal, metadata, _ = example
    lower, upper = copy.deepcopy(proposal), copy.deepcopy(proposal)
    lower["wall_readings"] = [wall_reading_input(plan, metadata)]
    plan["wall_metadata"][0]["thickness_m"] = .3
    upper["wall_readings"] = [wall_reading_input(plan, metadata)]
    inputs = [dict(proposal=lower, floor_id="L1", z_floor=0),
              dict(proposal=upper, floor_id="L2", z_floor=3.2)]
    before = copy.deepcopy(inputs)
    combined = assemble_plan_proposals(inputs)
    assert inputs == before
    path = tmp_path / "assembled"
    report = export_source_proposal(combined, path)
    assert report["source_geometry_ready"], report
    source = json.loads((path / "source_model.json").read_text(encoding="utf-8"))
    display = source_view_geometry(source)  # Includes source digest validation.
    spaces = {r["id"]: r for r in source["spaces"]}
    boundaries = {r["id"]: r for r in source["boundaries"]}
    for record in source["component_attributes"]:
        floors = {spaces[boundaries[bid]["space_id"]]["floor_id"] for bid in record["boundary_ids"]}
        assert floors in ({"L1"}, {"L2"})
        assert record["thickness_m"] == (.2 if floors == {"L1"} else .3)
    assert len(source["component_attributes"]) == 6
    assert display["source_model"]["component_attributes"] == source["component_attributes"]
    reloaded = json.loads((path / "proposal.json").read_text(encoding="utf-8"))
    assert bind_wall_readings(source, reloaded["wall_readings"])["component_attributes"] == source["component_attributes"]


def test_viewer_shows_known_unknown_and_conflicting_values_with_basis(example):
    plan, _, metadata, source = example
    source.update(bind_wall_metadata(source, plan, metadata))
    source["source_model_sha256"] = _digest({k: v for k, v in source.items() if k != "source_model_sha256"})
    properties = wall_properties(source)
    known = next(k for k, v in properties.items() if v["thickness_m"] == .2)
    unknown = next(v for v in properties.values() if v["thickness_m"] is None)
    assert properties[known]["basis"] == [_known()["basis"]]
    assert properties[known]["reference_side"] == "negative_face"
    assert properties[known]["evidence_status"] == "observed"
    assert unknown["evidence_status"] == "unknown"
    page = build_viewer_html(source_view_geometry(source))
    start = page.index("window.GEO = ") + len("window.GEO = ")
    embedded = json.loads(page[start:page.index(";</script>", start)])
    assert embedded["wall_properties"] == properties
    for record in source["component_attributes"]:
        if known in record["boundary_ids"]:
            record["thickness_m"] = .25
    assert wall_properties(source)[known]["conflict"]
    assert wall_properties(source)[known]["thickness_m"] is None


def test_tool_build_and_assembly_keep_thickness_without_model_calls(tmp_path):
    from scripts.tool_scripts.run_bim_agent import Toolkit
    run = tmp_path / "run"
    (run / "images").mkdir(parents=True)
    image = run / "images/plan.png"
    Image.new("RGB", (100, 100), "white").save(image)
    (run / "inputs.json").write_text(json.dumps({
        "images": {"plan.png": {"size": [100, 100], "sha256": hashlib.sha256(image.read_bytes()).hexdigest()}},
        "scope": "synthetic wall metadata", "only_input": "synthetic image"}), encoding="utf-8")
    toolkit = Toolkit(run)
    plan = _plan()
    plan["wall_metadata"] = [_known()]
    first = toolkit.build_plan("plan.png", json.dumps(plan))
    assert first["source_geometry_ready"], first
    plan["wall_metadata"][0]["thickness_m"] = .3
    second = toolkit.build_plan("plan.png", json.dumps(plan))
    assert second["source_geometry_ready"], second
    floors = []
    for result, fid, z in ((first, "L1", 0), (second, "L2", 3.2)):
        draft = result["plan_input"]
        source = json.loads((run / result["candidate"] / "source_model.json").read_text(encoding="utf-8"))
        assert source["component_attributes"]
        floors.append(dict(draft_id=draft["plan_file"].split("/")[-2],
                           expected_plan_sha256=draft["plan_sha256"], floor_id=fid, z_floor=z,
                           evidence="synthetic separate floor identity"))
    assembled = toolkit.assemble_plans(json.dumps(floors))
    assert assembled["source_geometry_ready"], assembled
    source = json.loads((run / assembled["candidate"] / "source_model.json").read_text(encoding="utf-8"))
    assert sorted(r["thickness_m"] for r in source["component_attributes"]) == [.2, .2, .2, .3, .3, .3]
