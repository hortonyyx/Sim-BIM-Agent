"""Reliable editing routes must keep exact bindings and preserve source checks."""
import asyncio
import json

import pytest

from scripts.tool_scripts.bim_agent_edit_route import candidate_edit_route
from scripts.tool_scripts.bim_agent_parametric import build_parametric, inspect_parametric, patch_parametric
from src.agent.geometry.proposal_edits import apply_proposal_edits
from tests.test_bim_agent_tools import _ParametricFunctionToolkit, _parametric_file_hashes
from tests.test_parametric_proposal import plan


def kind_change(identity="F1:door:d"):
    return {"op": "update_opening", "id": identity, "changes": {"kind": "window"},
            "reason": "observed glazing instead of an entrance", "source_refs": ["view:facade"]}


def test_saved_and_inspected_candidate_expose_exact_route_without_mutation(tmp_path):
    toolkit = _ParametricFunctionToolkit(tmp_path / "run")
    first = build_parametric(toolkit, json.dumps(plan()))
    before = _parametric_file_hashes(toolkit.run)
    route = candidate_edit_route(toolkit, first["candidate"], operations=[kind_change()])
    assert route["exact_plan"] and route["patch_allowed"]
    assert route["recommended_tool"] == "patch_parametric_bim"
    assert route["patch_arguments"] == {
        "candidate": first["candidate"], "expected_plan_sha256": first["parametric"]["plan_sha256"]}
    assert "kind" not in route["generic_contract"]["update_opening_fields"]
    assert first["edit_route"]["patch_arguments"] == route["patch_arguments"]
    assert inspect_parametric(toolkit, first["candidate"])["edit_route"]["patch_arguments"] == route["patch_arguments"]
    assert route["patch_contract"]["present_roots"] == sorted(plan())
    assert "connections" in route["patch_contract"]["allowed_roots"]
    assert "connections" not in route["patch_contract"]["present_roots"]
    assert before == _parametric_file_hashes(toolkit.run)


def test_rejected_kind_edit_returns_explicit_shared_scope_and_executable_patch(tmp_path):
    toolkit = _ParametricFunctionToolkit(tmp_path / "run")
    first = build_parametric(toolkit, json.dumps(plan()))
    original = json.loads((toolkit.run / first["candidate"] / "proposal.json").read_bytes())
    with pytest.raises(ValueError, match="unsupported changes.*kind"):
        apply_proposal_edits(original, [kind_change()])
    route = candidate_edit_route(toolkit, first["candidate"], operations=[kind_change()])
    target, = route["targeted_parametric_paths"]
    assert target["path"] == "/templates/typical/doors/0"
    assert target["affected_object_ids"] == ["F1:door:d", "F2:door:d"]
    assert target["scope"] == "shared_declaration"
    assert "every affected object" in target["apply_condition"]
    patched = patch_parametric(toolkit, json.dumps([target["proposed_patch"]]),
                               candidate=route["patch_arguments"]["candidate"],
                               expected_sha=route["patch_arguments"]["expected_plan_sha256"])
    assert patched["source_geometry_ready"]
    updated = json.loads((toolkit.run / patched["candidate"] / "parametric_plan.json").read_bytes())
    assert updated["templates"]["typical"]["doors"][0]["kind"] == "window"
    assert updated["templates"]["typical"]["doors"][0]["source_refs"] == plan()["templates"]["typical"]["doors"][0]["source_refs"]
    assert json.loads((toolkit.run / first["candidate"] / "proposal.json").read_bytes()) == original


@pytest.mark.parametrize("file", ["parametric_plan.json", "proposal.json", "source_model.json", "parametric_state.json"])
def test_tampered_binding_never_emits_patch_arguments(tmp_path, file):
    toolkit = _ParametricFunctionToolkit(tmp_path / "run")
    first = build_parametric(toolkit, json.dumps(plan()))
    (toolkit.run / first["candidate"] / file).write_text("{}", encoding="utf-8")
    route = candidate_edit_route(toolkit, first["candidate"], operations=[kind_change()])
    assert not route["exact_plan"] and not route["patch_allowed"]
    assert route["recommended_tool"] is None and "patch_arguments" not in route


def test_generic_descendant_does_not_patch_ancestor_or_translate_world_coordinates(tmp_path):
    toolkit = _ParametricFunctionToolkit(tmp_path / "run")
    first = build_parametric(toolkit, json.dumps(plan()))
    original = json.loads((toolkit.run / first["candidate"] / "proposal.json").read_bytes())
    operation = {**kind_change(), "changes": {"z": [0.2, 1.8]}}
    route = candidate_edit_route(toolkit, first["candidate"], operations=[operation])
    assert "proposed_patch" not in route["targeted_parametric_paths"][0]
    updated = apply_proposal_edits(original, [operation])
    generic = toolkit.build(updated, action="revise_bim", parent=first["candidate"])
    route = candidate_edit_route(toolkit, generic["candidate"], operations=[kind_change()])
    assert not route["exact_plan"] and not route["patch_allowed"]
    assert route["recommended_tool"] == "revise_bim"
    assert "patch_arguments" not in route


def test_unbuilt_candidate_routes_to_preserved_draft_hash(tmp_path):
    class UnbuiltToolkit(_ParametricFunctionToolkit):
        def build(self, *args, **kwargs):
            result = super().build(*args, **kwargs)
            (self.run / result["candidate"] / "source_model.json").unlink()
            return result

    toolkit = UnbuiltToolkit(tmp_path / "run")
    first = build_parametric(toolkit, json.dumps(plan(), indent=2))
    route = first["edit_route"]
    assert route["origin"] == "unbuilt_parametric"
    assert not route["exact_plan"] and route["patch_allowed"]
    assert route["patch_arguments"] == {
        "draft": first["draft"], "expected_plan_sha256": first["draft_sha256"]}
    assert first["draft_sha256"] != first["parametric"]["plan_sha256"]


def test_global_connection_route_and_escaped_template_pointer(tmp_path):
    toolkit = _ParametricFunctionToolkit(tmp_path / "run")
    value = plan()
    # Template names can use JSON pointer metacharacters even though IDs cannot.
    value["templates"]["office/~wing"] = value["templates"].pop("typical")
    for row in value["instances"]:
        row["template"] = "office/~wing"
    value["connections"] = [{"id": "global_exit", "space_id": "F1:hall", "other_space_id": None,
                             "p1": [0, 1], "p2": [0, 2], "z": [0, 2], "kind": "door",
                             "state": "unknown", "source_refs": ["entry"]}]
    first = build_parametric(toolkit, json.dumps(value))
    route = candidate_edit_route(toolkit, first["candidate"], operations=[
        kind_change(), kind_change("global_exit")])
    assert route["targeted_parametric_paths"][0]["path"] == "/templates/office~1~0wing/doors/0"
    target = route["targeted_parametric_paths"][1]
    assert target["path"] == "/connections/0"
    assert target["affected_object_ids"] == ["global_exit"]
    assert target["proposed_patch"]["op"] == "replace"


def test_mcp_accepts_returned_route_arguments_and_partial_semantic_patch(tmp_path):
    from tests.test_bim_agent_tools import _run_with_one_image, _server_session, _json_result

    async def scenario():
        run = _run_with_one_image(tmp_path)
        async with _server_session(run, readonly=False) as session:
            first = _json_result(await session.call_tool("build_parametric_bim", {"plan_json": json.dumps(plan())}))
            route = first["edit_route"]
            refused = _json_result(await session.call_tool("patch_parametric_bim", {
                **route["patch_arguments"],
                "operations_json": json.dumps([{
                    "op": "replace", "path": "/connections", "value": [],
                    "reason": "explicit connection declaration", "source_refs": ["test"]}])}))
            assert refused["error_details"]["operation_index"] == 0
            assert refused["error_details"]["suggested_operation"] == "add"
            assert refused["edit_route"]["patch_arguments"] == route["patch_arguments"]
            route = refused["edit_route"]
            reply = await session.call_tool("patch_parametric_bim", {
                **route["patch_arguments"],
                "operations_json": json.dumps([{
                    "op": "replace", "path": "/unresolved", "value": ["retained uncertainty"],
                    "reason": "condensed explanation", "source_refs": ["retained evidence"]}])})
            patched = _json_result(reply)
            assert not reply.isError and patched["source_geometry_ready"], patched
        toolkit = type("ReadToolkit", (), {"run": run, "candidate_path": lambda _, c: run / c})()
        operation = {**kind_change(), "changes": {"kind": "window", "z": [0.4, 2.0]}}
        target, = candidate_edit_route(toolkit, first["candidate"], operations=[operation])["targeted_parametric_paths"]
        assert target["field_paths"]["z"] == "/templates/typical/doors/0/z"
        assert target["proposed_patch"]["path"] == "/templates/typical/doors/0/kind"
        assert target["untranslated_changes"] == ["z"]

    asyncio.run(scenario())


def test_generic_revision_preserves_derived_continuous_core_frame(tmp_path, monkeypatch):
    from scripts.tool_scripts.run_bim_agent import Toolkit
    from tests.test_bim_agent_mesh import _prepare
    from tests.test_parametric_continuity import continuity, storeys
    _, run, _ = _prepare(tmp_path, monkeypatch)
    toolkit = Toolkit(run)
    value = storeys()
    value["modelling"] = {"frames": [{"id": "local", "origin": [0.04, 0], "yaw_degrees": 0,
                                      "templates": ["typical"]}]}
    value["continuous_spaces"] = [continuity()]
    first = build_parametric(toolkit, json.dumps(value))
    assert first["source_geometry_ready"], first
    protected = _parametric_file_hashes(run / first["candidate"])
    original = json.loads((run / first["candidate"] / "proposal.json").read_bytes())
    operation = {"op": "update_opening", "id": "F1:door:landing",
                 "changes": {"z": [0.1, 2]}, "reason": "observed landing door height",
                 "source_refs": ["view:landing"]}
    revised = toolkit.revise(first["candidate"], json.dumps([operation]))
    assert revised["source_geometry_ready"], revised
    updated = json.loads((run / revised["candidate"] / "proposal.json").read_bytes())
    for before, after in zip(original["geometry"]["floors"], updated["geometry"]["floors"]):
        assert before == after
    before_by_id = {row["id"]: row for row in original["geometry"]["openings"]}
    for row in updated["geometry"]["openings"]:
        assert row["p1"] == before_by_id[row["id"]]["p1"]
        assert row["p2"] == before_by_id[row["id"]]["p2"]
    assert next(row for row in updated["geometry"]["openings"] if row["id"] == operation["id"])["z"] == [0.1, 2]
    source = json.loads((run / revised["candidate"] / "source_model.json").read_bytes())
    assert source["validation"]["status"] == "pass"
    assert len(source["connections"]) == 3
    assert _parametric_file_hashes(run / first["candidate"]) == protected


def test_large_rejected_patch_locates_absent_root_and_preserves_atomicity(tmp_path):
    from tests.test_bim_agent_tools import _parametric_function_op
    toolkit = _ParametricFunctionToolkit(tmp_path / "run")
    first = build_parametric(toolkit, json.dumps(plan()))
    operations = [_parametric_function_op("add", "/assumptions/-", f"hypothesis {i}") for i in range(42)]
    operations.append(_parametric_function_op("replace", "/connections", []))
    before = _parametric_file_hashes(toolkit.run)
    refused = patch_parametric(toolkit, json.dumps(operations), candidate=first["candidate"],
                               expected_sha=first["parametric"]["plan_sha256"])
    assert _parametric_file_hashes(toolkit.run) == before
    assert len(toolkit.calls) == 1 and refused["save_effects"]["geometry_applied"] is False
    details = refused["error_details"]
    assert details["operation_index"] == 42 and details["index_base"] == 0
    assert details["operation_count"] == 43 and details["path"] == "/connections"
    assert details["existing_parent_keys"] == sorted(plan())
    assert details["target_exists"] is False and details["suggested_operation"] == "add"
    route = refused["edit_route"]
    assert route["patch_arguments"] == first["edit_route"]["patch_arguments"]
    operations[details["operation_index"]]["op"] = details["suggested_operation"]
    fixed = patch_parametric(toolkit, json.dumps(operations),
                             candidate=route["patch_arguments"]["candidate"],
                             expected_sha=route["patch_arguments"]["expected_plan_sha256"])
    assert fixed["source_geometry_ready"]
    saved = inspect_parametric(toolkit, fixed["candidate"], full=True)["value"]
    assert saved["connections"] == []
    assert saved["assumptions"] == plan()["assumptions"] + [f"hypothesis {i}" for i in range(42)]
    assert all(_parametric_file_hashes(toolkit.run)[path] == digest for path, digest in before.items())


@pytest.mark.parametrize("path, field, expected", [
    ("/templates/typical/absent", "existing_parent_keys", sorted(plan()["templates"]["typical"])),
    ("/instances/9", "parent_length", 2),
    ("/instances/0/height/value", "parent_type", "scalar"),
])
def test_patch_context_identifies_actual_parent_without_a_new_write(tmp_path, path, field, expected):
    from tests.test_bim_agent_tools import _parametric_function_op
    toolkit = _ParametricFunctionToolkit(tmp_path / "run")
    first = build_parametric(toolkit, json.dumps(plan()))
    before = _parametric_file_hashes(toolkit.run)
    result = patch_parametric(toolkit, json.dumps([_parametric_function_op("replace", path, 1)]),
                              candidate=first["candidate"], expected_sha=first["parametric"]["plan_sha256"])
    assert result["error_details"][field] == expected
    assert result["error_details"]["path"] == path
    assert result["error_details"]["operation_index"] == 0
    assert _parametric_file_hashes(toolkit.run) == before


def test_stale_patch_hash_does_not_issue_a_replacement_write_authority(tmp_path):
    from tests.test_bim_agent_tools import _parametric_function_op
    toolkit = _ParametricFunctionToolkit(tmp_path / "run")
    first = build_parametric(toolkit, json.dumps(plan()))
    before = _parametric_file_hashes(toolkit.run)
    refused = patch_parametric(toolkit, json.dumps([_parametric_function_op("add", "/connections", [])]),
                               candidate=first["candidate"], expected_sha="0" * 64)
    assert refused["saved_candidate"] is None and "edit_route" not in refused
    assert _parametric_file_hashes(toolkit.run) == before


def test_adopted_aperture_coordinates_return_declared_hosts_and_repairable_draft(tmp_path):
    from tests.test_architectural_regularization import single, POLICY
    from tests.test_bim_agent_tools import _parametric_function_op
    toolkit = _ParametricFunctionToolkit(tmp_path / "run")
    toolkit.manifest = {"architectural_modelling_policy": POLICY}
    original = single()
    first = build_parametric(toolkit, json.dumps(original))
    before = _parametric_file_hashes(toolkit.run)
    connection = {"id": "front_window", "kind": "window", "space_id": "F1:room", "other_space_id": None,
                  "p1": [1, 0], "p2": [2, 0], "z": [1, 2], "state": "unknown", "source_refs": ["facade"]}
    rejected = patch_parametric(toolkit, json.dumps([_parametric_function_op("add", "/connections", [connection])]),
                                candidate=first["candidate"], expected_sha=first["parametric"]["plan_sha256"])
    assert rejected["saved_candidate"] is None and rejected["error_stage"] == "parametric_expansion"
    assert len(toolkit.calls) == 1
    diagnostic, = rejected["error_details"]["host_diagnostics"]
    assert diagnostic["host"] == {"template": "main", "space": "room"}
    edge = diagnostic["nearest_declared_edges"][0]
    assert edge["endpoints"] == [[.024, .024], [6.024, .024]]
    assert edge["max_endpoint_distance_m"] == pytest.approx(.024)
    assert len(diagnostic["nearest_declared_edges"]) <= 4
    route = rejected["edit_route"]
    assert route["patch_arguments"]["draft"] == rejected["draft"]
    assert route["patch_arguments"]["expected_plan_sha256"] == rejected["plan_sha256"]
    draft = json.loads((toolkit.run / rejected["draft"]).read_bytes())
    assert draft["connections"][0]["p1"] == [1, 0]  # No silent projection.
    fixed = patch_parametric(toolkit, json.dumps([
        _parametric_function_op("replace", "/connections/0/p1/1", .024),
        _parametric_function_op("replace", "/connections/0/p2/1", .024)]),
        draft=route["patch_arguments"]["draft"], expected_sha=route["patch_arguments"]["expected_plan_sha256"])
    assert fixed["source_geometry_ready"] and len(toolkit.calls) == 2
    assert all(_parametric_file_hashes(toolkit.run)[path] == digest for path, digest in before.items())
