"""Auditable compact-plan drafts and explicit patches, using the shared builder.

An exact compact plan is a separate claim from source validity or visual quality.
Generic source revisions deliberately do not inherit that claim.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re

from scripts.tool_scripts.bim_agent_saved_result import saved_result
from src.agent.geometry.parametric_proposal import expand_parametric_proposal


PATCH_ROOTS = frozenset({"templates", "instances", "repeats", "connections",
    "core_connections", "continuous_spaces", "assumptions", "unresolved", "schema_version",
    "mesh_frame", "boundary_curves", "modelling", "primitive_frames"})
_DRAFT = re.compile(r"draft_[0-9]+\.json\Z")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _load(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"nonfinite JSON: {value}")))


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path, value):
    path.write_text(_json(value) + "\n", encoding="utf-8", newline="\n")


def _inside(toolkit, path):
    root = Path(toolkit.run).resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("parametric reference escapes this run")
    return resolved


def _relative(toolkit, path):
    return _inside(toolkit, path).relative_to(Path(toolkit.run).resolve()).as_posix()


def _candidate(toolkit, candidate):
    if not isinstance(candidate, str) or not re.fullmatch(r"candidate_[0-9]+|seed", candidate):
        raise ValueError("invalid candidate ID")
    return _inside(toolkit, toolkit.candidate_path(candidate))


def _draft(toolkit, draft):
    if not isinstance(draft, str):
        raise ValueError("draft must identify a saved parametric draft")
    name = draft.removeprefix("parametric_drafts/")
    if not _DRAFT.fullmatch(name):
        raise ValueError("draft must be draft_NNN.json or parametric_drafts/draft_NNN.json")
    path = _inside(toolkit, Path(toolkit.run) / "parametric_drafts" / name)
    if not path.is_file():
        raise ValueError("unknown parametric draft")
    return path


def _new_path(toolkit, folder, prefix):
    directory = _inside(toolkit, Path(toolkit.run) / folder)
    directory.mkdir(exist_ok=True)
    expression = re.compile(rf"{prefix}_([0-9]+)\.json\Z")
    number = max((int(m.group(1)) for p in directory.iterdir()
                  if (m := expression.fullmatch(p.name))), default=0) + 1
    return directory / f"{prefix}_{number:03d}.json"


def _draft_state(toolkit, path):
    state_path = _inside(toolkit, path.with_suffix(".state.json"))
    state = _load(state_path.read_bytes()) if state_path.is_file() else {}
    if not isinstance(state, dict):
        raise ValueError("parametric draft state must be an object")
    actual = _sha(path)
    if state.get("plan_sha256", actual) != actual:
        raise ValueError("saved draft bytes changed after admission")
    patch = state.get("patch")
    if patch:
        if not isinstance(patch, dict):
            raise ValueError("parametric patch reference must be an object")
        reference = patch.get("file", "")
        if not re.fullmatch(r"parametric_patches/patch_[0-9]+\.json", reference):
            raise ValueError("invalid parametric patch reference")
        patch_path = _inside(toolkit, Path(toolkit.run) / reference)
        if not patch_path.is_file() or _sha(patch_path) != patch.get("sha256"):
            raise ValueError("saved patch audit bytes changed")
    return {**state, "draft": _relative(toolkit, path), "plan_sha256": actual}


def _current_state(toolkit, candidate):
    folder = _candidate(toolkit, candidate)
    state_path = _inside(toolkit, folder / "parametric_state.json")
    if not state_path.is_file():
        return None
    state = _load(state_path.read_bytes())
    if not isinstance(state, dict):
        raise ValueError("parametric candidate state must be an object")
    if state.get("candidate") != candidate or state.get("schema_version") != 1:
        raise ValueError("parametric candidate binding is invalid")
    for name, field in (("parametric_plan.json", "plan_sha256"),
                        ("proposal.json", "proposal_sha256"),
                        ("source_model.json", "source_file_sha256")):
        path = _inside(toolkit, folder / name)
        actual = _sha(path) if path.is_file() else None
        if actual != state.get(field):
            raise ValueError(f"parametric candidate binding is stale: {name} changed")
    draft = _draft(toolkit, state["draft"])
    draft_state = _draft_state(toolkit, draft)
    if draft_state["plan_sha256"] != state["draft_sha256"]:
        raise ValueError("parametric candidate draft binding is stale")
    if (draft_state.get("candidate_binding_file") != f"{candidate}/parametric_state.json" or
            draft_state.get("candidate_binding_sha256") != _sha(state_path)):
        raise ValueError("parametric candidate state changed after binding")
    if _load(draft.read_bytes()) != _load((folder / "parametric_plan.json").read_bytes()):
        raise ValueError("candidate plan does not match its admitted draft")
    if state.get("architectural_regularization") is not None:
        from scripts.tool_scripts.bim_agent_architectural import validate_parametric_regularization
        validate_parametric_regularization(toolkit, state)
    return state


def _parent(toolkit, folder):
    # The shared builder stores this relation in both source generation and report.
    for name in ("source_model.json", "report.json"):
        path = _inside(toolkit, folder / name)
        if not path.is_file():
            continue
        value = _load(path.read_bytes())
        if not isinstance(value, dict):
            raise ValueError("candidate lineage record must be an object")
        provenance = (value.get("generation", {}).get("provenance", {})
                      if name == "source_model.json" else value.get("provenance", {}))
        parent = provenance.get("parent_candidate")
        if parent:
            return parent
    return None


def _lineage(toolkit, candidate):
    seen = set()
    cursor = candidate
    chain = []
    while cursor and cursor not in seen and len(chain) < 100:
        seen.add(cursor)
        folder = _candidate(toolkit, cursor)
        chain.append(cursor)
        plan = _inside(toolkit, folder / "parametric_plan.json")
        if plan.is_file():
            return {"candidate": cursor, "plan_file": _relative(toolkit, plan),
                    "plan_sha256": _sha(plan), "candidate_chain": chain,
                    "reference_only": True}
        cursor = _parent(toolkit, folder)
    return None


def _base(toolkit, candidate, draft):
    if bool(candidate) == bool(draft):
        raise ValueError("select exactly one candidate or draft")
    if draft:
        path = _draft(toolkit, draft)
        state = _draft_state(toolkit, path)
        return _load(path.read_bytes()), state, None
    state = _current_state(toolkit, candidate)
    if state is None:
        raise ValueError("candidate has no exact bound parametric plan; inspect stale lineage, "
                         "then submit an explicit new plan preserving any source-only changes")
    if state.get("source_file_sha256") is None:
        raise ValueError(f"candidate has no saved source; patch its preserved draft {state['draft']} instead")
    folder = _candidate(toolkit, candidate)
    return _load((folder / "parametric_plan.json").read_bytes()), state, candidate


def _failure(toolkit, action, error, *, state=None, stage="parametric_input"):
    result = saved_result({"status": "error", "error": str(error), "error_stage": stage,
                           "remaining_seconds": toolkit.remaining_seconds()},
                          audit_written=bool(state))
    if state:
        result.update(draft=state["draft"], plan_sha256=state["plan_sha256"], parametric=state)
    toolkit.log(action, result)
    return result


def _feedback_parent(toolkit, state, candidate):
    """Resolve feedback ancestry through immutable patch audits, never draft hints.

    A failed draft can still be repaired if its former rendered baseline changed.
    Such a baseline is only an optimization: return a miss instead of trusting it
    or weakening the exact plan used for the patch itself.
    """
    try:
        cursor, seen = state, set()
        for _ in range(100):
            if candidate:
                current = _current_state(toolkit, candidate)
                if current is None or current.get("source_file_sha256") is None:
                    raise ValueError("feedback ancestor has no exact saved source")
                for key in ("plan_sha256", "proposal_sha256", "source_file_sha256"):
                    if cursor.get(key) != current.get(key):
                        raise ValueError(f"feedback ancestor binding changed: {key}")
                return {"candidate": candidate, **{key: current[key] for key in
                    ("plan_sha256", "proposal_sha256", "source_file_sha256")}}, None
            draft = cursor["draft"]
            if draft in seen:
                raise ValueError("cyclic parametric feedback ancestry")
            seen.add(draft)
            # An explicitly selected built draft has its own most recent source.
            # Verify its reciprocal binding instead of skipping to an older patch
            # ancestor (which could hide a reversal of the last geometry edit).
            if cursor.get("candidate") and cursor.get("candidate_binding_file"):
                current = _current_state(toolkit, cursor["candidate"])
                if (current is None or current.get("draft") != draft or
                        current.get("draft_sha256") != cursor["plan_sha256"]):
                    raise ValueError("feedback draft candidate binding changed")
                if current.get("source_file_sha256") is not None:
                    candidate, cursor = cursor["candidate"], current
                    continue
            patch = cursor.get("patch")
            if not patch:
                return None, None
            audit = _load(_inside(toolkit, Path(toolkit.run) / patch["file"]).read_bytes())
            if audit.get("new_draft_sha256") != cursor["plan_sha256"]:
                raise ValueError("feedback patch does not bind the repaired draft")
            base = audit["base"]
            if base.get("candidate"):
                candidate, cursor = base["candidate"], base
            else:
                cursor = _draft_state(toolkit, _draft(toolkit, base["draft"]))
                if cursor["plan_sha256"] != base["plan_sha256"]:
                    raise ValueError("feedback ancestor draft bytes changed")
        raise ValueError("parametric feedback ancestry exceeds 100 drafts")
    except (ValueError, TypeError, KeyError, OSError) as error:
        return None, str(error)


def _build(toolkit, plan_json, *, action, base=None, patch=None, parent=None,
           feedback_parent=None, feedback_parent_error=None):
    if getattr(toolkit, "readonly", False):
        return _failure(toolkit, action, "parametric writes are unavailable in readonly mode")
    if not isinstance(plan_json, str):
        return _failure(toolkit, action, "plan_json must be a JSON string")
    path = _new_path(toolkit, "parametric_drafts", "draft")
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(plan_json)
    state = {"schema_version": 1, "status": "draft", "draft": _relative(toolkit, path),
             "plan_sha256": _sha(path)}
    if base:
        state["base"] = base
    if patch:
        state["patch"] = patch
    if feedback_parent:
        state["feedback_parent"] = feedback_parent
    if feedback_parent_error:
        state["feedback_parent_unavailable"] = feedback_parent_error
    _write(path.with_suffix(".state.json"), state)
    try:
        plan = _load(plan_json)
        from scripts.tool_scripts.bim_agent_architectural import prepare_parametric_plan
        compiled_plan, regularization = prepare_parametric_plan(toolkit, plan, draft_path=path)
        if regularization is not None:
            state["architectural_regularization"] = regularization
        expansion_audit = {}
        proposal = expand_parametric_proposal(compiled_plan, expansion_audit=expansion_audit)
        if expansion_audit:
            state["expansion_audit"] = expansion_audit
    except (ValueError, TypeError, KeyError) as error:
        state.update(status="invalid_plan", error=str(error))
        if isinstance(getattr(error, "audit", None), dict):
            state["regularization_failure"] = error.audit
        if isinstance(getattr(error, "audit_reference", None), dict):
            state["regularization_failure_reference"] = error.audit_reference
        _write(path.with_suffix(".state.json"), state)
        return _failure(toolkit, action, error, state=state, stage="parametric_expansion")
    try:
        options = {}
        if feedback_parent is not None:
            options["feedback_parent"] = feedback_parent
        if regularization is not None:
            options["architectural_regularization"] = regularization
        result = toolkit.build(proposal, action=action, parent=parent, **options)
    except (ValueError, TypeError, KeyError) as error:
        state.update(status="build_rejected", error=str(error))
        _write(path.with_suffix(".state.json"), state)
        return _failure(toolkit, action, error, state=state, stage="parametric_build")
    candidate = result.get("candidate")
    if candidate:
        folder = _candidate(toolkit, candidate)
        _write(folder / "parametric_plan.json", plan)
        binding = {"schema_version": 1, "status": "current", "candidate": candidate,
            "draft": state["draft"], "draft_sha256": state["plan_sha256"],
            "plan_sha256": _sha(folder / "parametric_plan.json"),
            "proposal_sha256": _sha(folder / "proposal.json"),
            "source_file_sha256": (_sha(folder / "source_model.json")
                                   if (folder / "source_model.json").is_file() else None),
            "base": base, "patch": patch, "fidelity": "not_evaluated"}
        if feedback_parent is not None:
            binding["feedback_parent"] = feedback_parent
        if feedback_parent_error:
            binding["feedback_parent_unavailable"] = feedback_parent_error
        if regularization is not None:
            binding["architectural_regularization"] = regularization
        if expansion_audit:
            binding["expansion_audit"] = expansion_audit
        if binding["source_file_sha256"] is None:
            binding["status"] = "unbuilt"
        _write(folder / "parametric_state.json", binding)
        report_path = _inside(toolkit, folder / "report.json")
        if report_path.is_file():
            report = _load(report_path.read_bytes())
            report["parametric"] = binding
            _write(report_path, report)
        state.update(status="built", candidate=candidate,
                     candidate_binding_file=f"{candidate}/parametric_state.json",
                     candidate_binding_sha256=_sha(folder / "parametric_state.json"))
        result["parametric"] = binding
    else:
        state["status"] = "build_rejected"
        result["parametric"] = state
    _write(path.with_suffix(".state.json"), state)
    result.update(draft=state["draft"], draft_sha256=state["plan_sha256"])
    # Inventories are saved by the shared builder; defaults must not re-expand them.
    result.pop("opening_inventory", None)
    toolkit.log("parametric_draft_saved", {"action": action, **state})
    return result


def build_parametric(toolkit, plan_json: str, *, action="build_parametric_bim") -> dict:
    return _build(toolkit, plan_json, action=action)


def _tokens(pointer):
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise ValueError("pointer must be a JSON Pointer starting with /, or empty for root")
    if not pointer:
        return []
    result = []
    for token in pointer[1:].split("/"):
        if re.search(r"~(?![01])", token):
            raise ValueError("invalid JSON Pointer escape")
        result.append(token.replace("~1", "/").replace("~0", "~"))
    return result


def _index(token, length, *, add=False):
    if add and token == "-":
        return length
    if not re.fullmatch(r"0|[1-9][0-9]*", token):
        raise ValueError("array pointer requires a canonical nonnegative index")
    index = int(token)
    if index >= length + int(add):
        raise ValueError("array pointer is out of range")
    return index


def _select(value, tokens):
    for token in tokens:
        if isinstance(value, dict):
            if token not in value:
                raise ValueError(f"pointer member does not exist: {token}")
            value = value[token]
        elif isinstance(value, list):
            value = value[_index(token, len(value))]
        else:
            raise ValueError("pointer traverses a scalar")
    return value


def _summary(plan):
    if not isinstance(plan, dict):
        return {"type": type(plan).__name__}
    result = {"top_level_keys": list(plan), "templates": {},
              "counts": {k: len(v) for k, v in plan.items() if isinstance(v, (list, dict))}}
    for key, value in plan.get("templates", {}).items() if isinstance(plan.get("templates"), dict) else []:
        result["templates"][key] = {k: len(v) for k, v in value.items()
                                    if isinstance(v, list)} if isinstance(value, dict) else {"invalid": True}
    if isinstance(plan.get("instances"), list):
        result["instances"] = [{k: row.get(k) for k in ("id", "template", "z", "height")}
                               for row in plan["instances"] if isinstance(row, dict)]
    try:
        from src.agent.geometry.parametric_proposal import expand_parametric_instances
        result["expanded_instance_count"] = len(expand_parametric_instances(plan))
    except (ValueError, TypeError, KeyError) as error:
        result["expansion_error"] = str(error)
    return result


def _selection(value, pointer, max_chars, offset):
    encoded = _json(value)
    result = {"pointer": pointer, "serialized_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
              "total_chars": len(encoded), "offset": offset}
    if offset > len(encoded):
        raise ValueError("offset is beyond selected JSON text")
    if offset == 0 and len(encoded) <= max_chars:
        result["value"] = value
    else:
        end = min(offset + max_chars, len(encoded))
        result.update(serialized_json=encoded[offset:end], next_offset=end if end < len(encoded) else None,
                      fragment=True)
    keys = list(value) if isinstance(value, dict) else range(len(value)) if isinstance(value, list) else []
    children = []
    for key in list(keys)[:100]:
        child = pointer + "/" + str(key).replace("~", "~0").replace("/", "~1")
        if sum(map(len, children)) + len(child) > max_chars:
            break
        children.append(child)
    result["children"] = children
    result["children_truncated"] = len(keys) > len(children)
    return result


def inspect_parametric(toolkit, candidate: str = "", *, draft: str = "",
                       pointer: str | None = None, full: bool = False,
                       max_chars: int = 12000, offset: int = 0) -> dict:
    try:
        if type(max_chars) is not int or not 1 <= max_chars <= 64000 or type(offset) is not int or offset < 0:
            raise ValueError("max_chars must be 1..64000 and offset a nonnegative integer")
        if bool(candidate) == bool(draft):
            raise ValueError("select exactly one candidate or draft")
        candidate_state = _current_state(toolkit, candidate) if candidate else None
        if candidate_state and candidate_state.get("source_file_sha256") is None:
            return {"status": "unbuilt", "candidate": candidate, "exact_plan": False,
                    "patch_allowed": False, "draft": candidate_state["draft"],
                    "draft_sha256": candidate_state["draft_sha256"],
                    "reason": "No source was saved; inspect and patch the preserved draft instead."}
        if candidate and candidate_state is None:
            reference = _lineage(toolkit, candidate)
            result = {"status": "stale_lineage", "candidate": candidate, "exact_plan": False,
                "nearest_parametric": reference,
                "patch_allowed": False,
                "reason": "No exact bound compact plan for this source. Ancestor plans are read-only "
                          "references; do not overwrite source-only edits. Submit an explicit new plan "
                          "that accounts for those edits before patching."}
            if reference and (pointer is not None or full):
                reference_plan = _load(_inside(toolkit, Path(toolkit.run) / reference["plan_file"]).read_bytes())
                selected_pointer = pointer if pointer is not None else ""
                result["reference_selection"] = {
                    "reference_only": True, "candidate": reference["candidate"],
                    **_selection(_select(reference_plan, _tokens(selected_pointer)),
                                 selected_pointer, max_chars, offset)}
            elif offset:
                raise ValueError("offset requires full=true or a pointer with an available reference")
            toolkit.log("inspect_parametric_plan", {"status": "stale_lineage", "candidate": candidate})
            return result
        plan, state, _ = _base(toolkit, candidate, draft)
        result = {"status": "current" if candidate else "draft", "exact_plan": bool(candidate),
                  "candidate": candidate or None, "draft": state["draft"],
                  "plan_sha256": state["plan_sha256"], "patch_allowed": True,
                  "fidelity": "not_evaluated"}
        result["lineage"] = {key: state[key] for key in
                             ("base", "patch", "proposal_sha256", "source_file_sha256",
                              "feedback_parent", "feedback_parent_unavailable",
                              "architectural_regularization") if key in state}
        if pointer is not None or full:
            pointer = pointer if pointer is not None else ""
            result.update(_selection(_select(plan, _tokens(pointer)), pointer, max_chars, offset))
        else:
            if offset:
                raise ValueError("offset requires full=true or a pointer")
            summary = _summary(plan)
            if len(_json(summary)) > max_chars:
                summary = {"counts": summary.get("counts", {}), "summary_truncated": True,
                           "expanded_instance_count": summary.get("expanded_instance_count"),
                           "hint": "Read /templates, /instances, /repeats or other nodes with pointer and offset."}
            result["summary"] = summary
            result["read"] = {"pointer": "/templates", "full": False, "max_chars": max_chars}
        toolkit.log("inspect_parametric_plan", {k: result[k] for k in ("status", "candidate", "draft", "plan_sha256")})
        return result
    except (ValueError, TypeError, KeyError, OSError) as error:
        return {"status": "stale" if candidate else "error", "candidate": candidate or None,
                "error": str(error), "exact_plan": False, "patch_allowed": False}


def _apply(plan, operations):
    if not isinstance(operations, list) or not operations or len(operations) > 1000:
        raise ValueError("operations must be a nonempty array of at most 1000 explicit patches")
    value = copy.deepcopy(plan)
    for operation in operations:
        if not isinstance(operation, dict):
            raise ValueError("each patch must be an object")
        op = operation.get("op")
        required = {"op", "path", "value"} if op in {"add", "replace", "test"} else {"op", "path"}
        if op != "test":
            required |= {"reason", "source_refs"}
        if op not in {"add", "replace", "remove", "test"} or set(operation) != required:
            raise ValueError("patch supports add/replace/remove with reason and source_refs; "
                             "add/replace/test require value, remove forbids value")
        if op != "test" and (not isinstance(operation["reason"], str) or not operation["reason"].strip()
                or not isinstance(operation["source_refs"], list) or not operation["source_refs"]
                or any(not isinstance(item, str) or not item.strip() for item in operation["source_refs"])):
            raise ValueError("each change needs a nonblank reason and nonempty source_refs")
        tokens = _tokens(operation["path"])
        if not tokens or tokens[0] not in PATCH_ROOTS:
            raise ValueError("patch path must start at an explicit parametric field; root replacement is forbidden")
        parent = _select(value, tokens[:-1])
        key = tokens[-1]
        if op == "test":
            actual = _select(value, tokens)
            # Canonical JSON comparison distinguishes true from 1.
            if _json(actual) != _json(operation["value"]):
                raise ValueError(f"patch test failed: {operation['path']}")
            continue
        if isinstance(parent, dict):
            if op != "add" and key not in parent:
                raise ValueError(f"patch target does not exist: {operation['path']}")
            if op == "remove":
                del parent[key]
            else:
                parent[key] = copy.deepcopy(operation["value"])
        elif isinstance(parent, list):
            index = _index(key, len(parent), add=op == "add")
            if op == "add":
                parent.insert(index, copy.deepcopy(operation["value"]))
            elif op == "remove":
                parent.pop(index)
            else:
                parent[index] = copy.deepcopy(operation["value"])
        else:
            raise ValueError("patch target parent is a scalar")
    return value


def patch_parametric(toolkit, operations_json: str, *, candidate: str = "", draft: str = "",
                     expected_sha: str, action="patch_parametric_bim") -> dict:
    if getattr(toolkit, "readonly", False):
        return _failure(toolkit, action, "parametric writes are unavailable in readonly mode")
    try:
        plan, state, parent = _base(toolkit, candidate, draft)
        if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
            raise ValueError("expected_sha must be the exact inspected plan_sha256")
        if expected_sha != state["plan_sha256"]:
            raise ValueError("stale expected_sha; inspect the intended base before patching")
        operations = _load(operations_json)
        updated = _apply(plan, operations)
        encoded = _json(updated)
        feedback_parent, feedback_error = _feedback_parent(toolkit, state, parent)
    except (ValueError, TypeError, KeyError, OSError) as error:
        return _failure(toolkit, action, error)
    base = {"candidate": candidate or None, "draft": state["draft"],
            "plan_sha256": expected_sha,
            "source_file_sha256": state.get("source_file_sha256"),
            "proposal_sha256": state.get("proposal_sha256")}
    record = {"schema_version": 1, "base": base, "operations": operations,
              "new_draft_sha256": hashlib.sha256(encoded.encode()).hexdigest()}
    path = _new_path(toolkit, "parametric_patches", "patch")
    _write(path, record)
    patch = {"file": _relative(toolkit, path), "sha256": _sha(path)}
    return _build(toolkit, encoded, action=action, base=base, patch=patch, parent=parent,
                  feedback_parent=feedback_parent, feedback_parent_error=feedback_error)
