"""Describe the edit contract of the saved candidate without an extra tool call.

Parameter paths refer to the exact admitted declaration, never an ancestor plan
or a guessed reverse mapping of regularized source coordinates.
"""
from __future__ import annotations

import json

from src.agent.geometry.proposal_edits import _OPENING_FIELDS, _WINDOW_FIELDS


def _pointer(*parts):
    return "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def _aperture_paths(plan, proposal):
    """Map stable expanded IDs to their declared row and all affected IDs."""
    from src.agent.geometry.parametric_proposal import expand_parametric_instances
    instances = expand_parametric_instances(plan)
    existing = {row["id"] for key in ("openings", "windows")
                for row in proposal["geometry"].get(key, [])}
    mappings = {}

    def register(path, row, identities):
        affected = sorted(set(identities) & existing)
        for identity in affected:
            mappings[identity] = {"path": path, "declaration": row,
                                  "affected_object_ids": affected}

    for name, template in plan["templates"].items():
        users = [row for row in instances if row["template"] == name]
        for collection in ("doors", "opening_rows", "window_rows"):
            for index, row in enumerate(template.get(collection, [])):
                if collection == "doors":
                    identities = [f"{inst['id']}:door:{row['id']}" for inst in users]
                else:
                    middle = "row:" if collection == "opening_rows" else ""
                    identities = [f"{inst['id']}:{middle}{row['id']}:{span}"
                                  for inst in users for span in range(1, len(row["spans"]) + 1)]
                register(_pointer("templates", name, collection, index), row, identities)
    for index, row in enumerate(plan.get("connections", [])):
        register(_pointer("connections", index), row, [row["id"]])
    for index, row in enumerate(plan.get("core_connections", [])):
        # The admitted proposal determines which repeat/exclusion instances
        # actually produced apertures; do not guess from an ID prefix.
        identities = [f"{inst['id']}:core:{row['id']}" for inst in instances]
        register(_pointer("core_connections", index), row, identities)
    return mappings


def _targets(plan, proposal, operations):
    mappings = _aperture_paths(plan, proposal)
    result = []
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            continue
        identity = operation.get("id")
        if not isinstance(identity, str) and isinstance(operation.get("opening"), dict):
            identity = operation["opening"].get("id")
        mapping = mappings.get(identity)
        if mapping is None:
            continue
        row = mapping["declaration"]
        target = {"operation_index": index, "object_id": identity,
                  "path": mapping["path"], "current_declaration": row,
                  "affected_object_ids": mapping["affected_object_ids"],
                  "scope": "shared_declaration" if len(mapping["affected_object_ids"]) > 1 else "single_object",
                  "coordinate_contract": "Use declared plan coordinates; template/core z is relative to each instance. Do not copy adopted world coordinates."}
        changes = operation.get("changes")
        if isinstance(changes, dict):
            target["field_paths"] = {field: mapping["path"] + _pointer(field)
                                     for field in changes if field in row or field == "kind"}
        # A kind change is semantic and needs no coordinate reverse transform.
        # Do not translate host, span or Z changes without an explicit model
        # decision about template scope and local versus adopted coordinates.
        if (operation.get("op") == "update_opening" and
                isinstance(changes, dict) and "kind" in changes and
                ("kind" in row or "/doors/" in mapping["path"])):
            patch = {"op": "replace" if "kind" in row else "add",
                     "path": mapping["path"] + "/kind", "value": changes["kind"],
                     "reason": operation.get("reason"), "source_refs": operation.get("source_refs")}
            if (isinstance(patch["reason"], str) and patch["reason"].strip() and
                    isinstance(patch["source_refs"], list) and patch["source_refs"] and
                    all(isinstance(ref, str) and ref.strip() for ref in patch["source_refs"])):
                target["proposed_patch"] = patch
                target["untranslated_changes"] = sorted(set(changes) - {"kind"})
                target["apply_condition"] = (
                    "Apply only if the requested semantic change is intended for every affected object; "
                    "otherwise isolate an instance/template explicitly before changing this row. "
                    "This patch covers kind only; resolve untranslated_changes separately.")
        result.append(target)
    return result


def candidate_edit_route(toolkit, candidate, *, operations=None):
    """Return a read-only route; stale/missing bindings never authorize patches."""
    from scripts.tool_scripts.bim_agent_parametric import PATCH_ROOTS, _candidate, _current_state, _load
    result = {
        "candidate": candidate, "exact_plan": False, "patch_allowed": False,
        "recommended_tool": "revise_bim",
        "generic_contract": {
            "update_opening_fields": sorted(_OPENING_FIELDS),
            "update_window_fields": sorted(_WINDOW_FIELDS),
            "kind_update_supported": False,
            "effect_on_parametric_binding": "A generic source revision has no exact parameter binding; its ancestor plan is reference-only.",
        },
    }
    try:
        state = _current_state(toolkit, candidate)
        if state is None:
            result["origin"] = "source_proposal"
            return result
        if state.get("source_file_sha256") is None:
            result.update(origin="unbuilt_parametric", recommended_tool="patch_parametric_bim",
                          draft=state["draft"], plan_sha256=state["draft_sha256"],
                          patch_allowed=True,
                          patch_arguments={"draft": state["draft"], "expected_plan_sha256": state["draft_sha256"]})
            return result
        result.update(origin="exact_parametric", exact_plan=True, patch_allowed=True,
                      recommended_tool="patch_parametric_bim",
                      plan_sha256=state["plan_sha256"],
                      patch_arguments={"candidate": candidate, "expected_plan_sha256": state["plan_sha256"]},
                      patch_contract={"operations": ["test", "add", "replace", "remove"],
                                      "allowed_roots": sorted(PATCH_ROOTS),
                                      "required_change_fields": ["op", "path", "reason", "source_refs"],
                                      "value_required_for": ["add", "replace", "test"],
                                      "root_replacement_allowed": False},
                      next_action="For changes to this exact parameter candidate, patch the declared plan using patch_arguments. Supported generic source edits remain available.")
        if operations is not None:
            if isinstance(operations, str):
                operations = json.loads(operations)
            if not isinstance(operations, list):
                raise ValueError("operations must be a list")
            folder = _candidate(toolkit, candidate)
            plan = _load((folder / "parametric_plan.json").read_bytes())
            proposal = _load((folder / "proposal.json").read_bytes())
            result["targeted_parametric_paths"] = _targets(plan, proposal, operations)
        return result
    except (ValueError, TypeError, KeyError, OSError) as error:
        # The route is advice, never permission to skip exact binding checks.
        result.update(origin="unavailable", exact_plan=False, patch_allowed=False,
                      recommended_tool=None, error=str(error))
        result.pop("patch_arguments", None)
        result.pop("plan_sha256", None)
        return result
