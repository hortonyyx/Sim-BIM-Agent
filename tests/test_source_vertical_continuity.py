"""One deterministic review covers every declared vertical stack and exception."""
import copy
import json

import pytest

from src.agent.correction.parse import ensure_corrected_geometry
from src.agent.correction.schema import FootprintRing
from src.agent.execution.source_proposal import export_source_proposal
from src.agent.geometry.parametric_proposal import expand_parametric_proposal
from src.agent.geometry.source_bim import build_source_bim
from src.agent.geometry.source_enclosure import apply_source_enclosure
from src.agent.geometry.source_model import _digest
from src.agent.geometry.source_vertical_continuity import bind_vertical_continuity_review, review_vertical_continuity
from tests.test_parametric_continuity import continuity, storeys
from tests.test_source_enclosure import _declaration, _evidence


def source_from(proposal):
    geometry = ensure_corrected_geometry(proposal["geometry"])
    for floor in geometry.floors:
        if isinstance(floor.footprint, dict):
            floor.footprint = FootprintRing.model_validate(floor.footprint)
    return build_source_bim(geometry, capability_profile="orthogonal_polygon")


def separated():
    return {"id": "floor_compartments", "space": "stair", "repeat_ids": ["storeys"],
            "reason": "These are explicitly separate capped compartments, not one continuous core.",
            "source_refs": ["declared actual separation"]}


@pytest.mark.parametrize("role", ["shaft", "stairwell", "atrium"])
def test_all_nonspanning_role_stacks_are_aggregated_without_prose_inference(role):
    plan = storeys()
    for cell in plan["templates"]["typical"]["spaces"]:
        cell["role"] = role
        cell["source_refs"] = ["arbitrary words do not establish or waive continuity"]
    source = source_from(expand_parametric_proposal(plan))
    assert all("spanning_space_ids" not in row for row in source["floors"])
    review = source["vertical_continuity_review"]
    assert review["status"] == "decision_required"
    assert len(review["unresolved_stacks"]) == 2
    assert {tuple(row["space_ids"]) for row in review["unresolved_stacks"]} == {
        ("F1:stair", "F2:stair", "F3:stair"), ("F1:room", "F2:room", "F3:room")}
    assert all(row["interface_count"] == 2 for row in review["unresolved_stacks"])
    assert all(interface["boundary_state"] == "physical_caps"
               for row in review["unresolved_stacks"] for interface in row["interfaces"])
    assert source["validation"]["status"] == "pass"  # Common geometry does not silently impose the mesh policy.
    assert review["coverage"]["space_count"] == 6


def test_continuous_first_declaration_resolves_the_stack_without_an_extra_decision():
    plan = storeys()
    plan["continuous_spaces"] = [continuity()]
    source = source_from(expand_parametric_proposal(plan))
    assert source["vertical_continuity_review"]["status"] == "resolved"
    assert source["vertical_continuity_review"]["unresolved_stacks"] == []


def test_equipment_rooms_do_not_become_shafts_from_names_or_source_refs():
    plan = storeys()
    cell = plan["templates"]["typical"]["spaces"][0]
    cell.update(role="electrical/mechanical", source_refs=["inferred vertical duct continuity; stair; atrium"])
    source = source_from(expand_parametric_proposal(plan))
    assert source["vertical_continuity_review"]["unresolved_stacks"] == []


def test_explicit_separation_is_saved_reloaded_and_survives_local_edits(tmp_path):
    from src.agent.geometry.proposal_edits import apply_proposal_edits
    plan = storeys()
    plan["separated_spaces"] = [separated()]
    proposal = expand_parametric_proposal(plan)
    declaration = proposal["geometry"]["vertical_separations"][0]
    assert declaration["space_ids"] == ["F1:stair", "F2:stair", "F3:stair"]
    first = export_source_proposal(proposal, tmp_path / "first")
    assert first["source_geometry_ready"], first
    source = json.loads((tmp_path / "first/source_model.json").read_bytes())
    assert source["vertical_separations"] == proposal["geometry"]["vertical_separations"]
    assert source["vertical_continuity_review"]["status"] == "resolved"
    revised = apply_proposal_edits(proposal, [{"op": "set_notes", "assumptions": ["retained separation"],
                                              "unresolved": []}])
    assert revised["geometry"]["vertical_separations"] == proposal["geometry"]["vertical_separations"]
    saved = export_source_proposal(revised, tmp_path / "revised")
    assert saved["source_geometry_ready"], saved
    # A reference-preserving revision that removes a member cannot silently
    # erase the cap decision: stale explicit membership becomes a source error.
    stale = copy.deepcopy(revised)
    stale["geometry"]["vertical_separations"][0]["space_ids"][-1] = "missing:shaft"
    rejected = export_source_proposal(stale, tmp_path / "stale")
    assert not rejected["source_geometry_ready"]
    bad = json.loads((tmp_path / "stale/source_model.json").read_bytes())
    assert bad["vertical_continuity_review"]["status"] == "invalid"


@pytest.mark.parametrize("failure", ["unknown", "duplicate_member", "blank_reason", "blank_refs",
                                    "duplicate_declaration", "duplicate_interface", "isolated_member"])
def test_invalid_separations_cannot_waive_unresolved_interfaces(failure):
    plan = storeys()
    plan["separated_spaces"] = [separated()]
    if failure in ("blank_reason", "blank_refs", "duplicate_declaration"):
        if failure == "blank_reason":
            plan["separated_spaces"][0]["reason"] = " "
        elif failure == "blank_refs":
            plan["separated_spaces"][0]["source_refs"] = [" "]
        else:
            plan["separated_spaces"].append(copy.deepcopy(plan["separated_spaces"][0]))
        with pytest.raises(ValueError):
            expand_parametric_proposal(plan)
        return
    proposal = expand_parametric_proposal(plan)
    row = proposal["geometry"]["vertical_separations"][0]
    if failure == "unknown":
        row["space_ids"][-1] = "unknown"
    elif failure == "duplicate_member":
        row["space_ids"].append(row["space_ids"][0])
        with pytest.raises(ValueError):
            source_from(proposal)
        return
    elif failure == "duplicate_interface":
        proposal["geometry"]["vertical_separations"].append({**copy.deepcopy(row), "id": "duplicated_pair"})
    else:
        row["space_ids"].append("F1:room")
    source = source_from(proposal)
    assert source["vertical_continuity_review"]["status"] == "invalid"
    assert any(row["code"] == "source.vertical_separation_invalid" for row in source["validation"]["findings"])


def test_continuity_and_separation_can_share_a_terminal_member_but_not_an_interface():
    plan = storeys()
    plan["continuous_spaces"] = [{**continuity(), "instance_ids": ["F1", "F2"]}]
    plan["continuous_spaces"][0].pop("repeat_ids")
    plan["separated_spaces"] = [{**separated(), "instance_ids": ["F2", "F3"]}]
    plan["separated_spaces"][0].pop("repeat_ids")
    proposal = expand_parametric_proposal(plan)
    assert proposal["geometry"]["vertical_separations"][0]["space_ids"] == ["STAIR:stair", "F3:stair"]
    source = source_from(proposal)
    assert source["vertical_continuity_review"]["status"] == "resolved"
    plan["separated_spaces"][0]["instance_ids"] = ["F1", "F2"]
    with pytest.raises(ValueError, match="contradicts continuous"):
        expand_parametric_proposal(plan)


def _open_first_interface(source, condition="open"):
    caps = [row for row in source["boundaries"]
            if (row["space_id"], row["geometry_type"]) in {("F1:stair", "ceiling"), ("F2:stair", "floor")}]
    return apply_source_enclosure(source, _declaration(source, boundaries=[
        {"boundary_id": row["id"], "condition": condition, "scope": "whole", **_evidence()} for row in caps]))


def test_real_reciprocal_horizontal_openings_are_not_reported_as_a_closed_slab():
    source = source_from(expand_parametric_proposal(storeys()))
    result = _open_first_interface(source)
    review = result["vertical_continuity_review"]
    assert review["unresolved_stacks"][0]["space_ids"] == ["F2:stair", "F3:stair"]
    assert review["unresolved_stacks"][0]["interface_count"] == 1
    assert any(row["status"] == "open_connection" and row["space_ids"] == ["F1:stair", "F2:stair"]
               for row in review["resolved_interfaces"])
    assert result["source_model_sha256"] == _digest({k: v for k, v in result.items() if k != "source_model_sha256"})
    unknown = _open_first_interface(source, "unknown")
    assert any(row["boundary_state"] == "unverified_boundary_state"
               for stack in unknown["vertical_continuity_review"]["unresolved_stacks"] for row in stack["interfaces"])


def test_a_separation_cannot_contradict_a_real_open_interface():
    plan = storeys()
    plan["separated_spaces"] = [separated()]
    result = _open_first_interface(source_from(expand_parametric_proposal(plan)))
    assert result["vertical_continuity_review"]["status"] == "invalid"
    assert any(row["code"] == "separation_not_physical" for row in result["vertical_continuity_review"]["invalid_declarations"])


@pytest.mark.parametrize("unrelated_severe", [False, True])
def test_enclosure_retires_only_resolved_policy_findings_and_rebinds_digest(unrelated_severe):
    from src.agent.execution.source_proposal import _require_vertical_continuity
    source = source_from(expand_parametric_proposal(storeys()))
    _require_vertical_continuity(source, "explicit_stack_v1")
    assert source["validation"]["status"] == "severe"
    if unrelated_severe:
        source["validation"]["findings"].append({"code": "test.unrelated", "severity": "severe"})
    source["source_model_sha256"] = _digest({k: v for k, v in source.items() if k != "source_model_sha256"})
    caps = [row for row in source["boundaries"] if row["space_id"].endswith(":stair")
            and row["geometry_type"] in ("ceiling", "floor") and row["vertices"][0][2] in (3, 6)]
    result = apply_source_enclosure(source, _declaration(source, boundaries=[
        {"boundary_id": row["id"], "condition": "open", "scope": "whole", **_evidence()} for row in caps]))
    assert result["vertical_continuity_review"]["status"] == "resolved"
    assert not any(row["code"] == "source.vertical_continuity_decision_required"
                   for row in result["validation"]["findings"])
    assert result["validation"]["status"] == ("severe" if unrelated_severe else "pass")
    assert result["source_model_sha256"] == _digest({k: v for k, v in result.items() if k != "source_model_sha256"})


@pytest.mark.parametrize("old_findings", [[], [{"code": "test.warning", "severity": "warning"}]])
def test_binding_preserves_unexplained_severe_and_rejects_unknown_policy(old_findings):
    source = source_from(expand_parametric_proposal(storeys()))
    source["validation"].update(status="severe", findings=old_findings)
    bind_vertical_continuity_review(source)
    assert source["validation"]["status"] == "severe"
    source["vertical_continuity_policy"] = "unsupported"
    with pytest.raises(ValueError, match="unknown vertical continuity policy"):
        bind_vertical_continuity_review(source)


def test_changing_footprints_are_reported_as_outside_exact_stack_coverage():
    plan = storeys()
    plan["templates"]["different"] = copy.deepcopy(plan["templates"]["typical"])
    plan["templates"]["different"]["spaces"][0]["rect"][2] = 2.1
    plan["templates"]["different"]["spaces"][1]["rect"][0] = 2.1
    plan["templates"]["different"]["doors"] = []
    plan["repeats"][0]["exclude_ids"] = ["F2"]
    plan["instances"] = [{"id": "F2", "template": "different", "z": 3, "height": 3}]
    source = source_from(expand_parametric_proposal(plan))
    review = review_vertical_continuity(source)
    assert len(review["coverage"]["different_footprint_pairs"]) == 2
    assert not review["unresolved_stacks"]
