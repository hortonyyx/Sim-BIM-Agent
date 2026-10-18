"""Bounded trial views; complete evidence remains in the verified receipt."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _child(pointer, key):
    return pointer + "/" + str(key).replace("~", "~0").replace("/", "~1")


def reference(receipt, pointer, value):
    raw = _bytes(value)
    result = {"json_pointer": pointer, "sha256": hashlib.sha256(raw).hexdigest(),
              "hash_encoding": "utf-8-json-sort_keys-compact-ensure_ascii_false",
              "bytes": len(raw), "read": {"tool": "read_plan_draft", "arguments": {
                  "trial_id": receipt.get("trial_id", "latest"), "field": "report:" + pointer}}}
    if receipt.get("receipt_file"):
        result["receipt_file"] = receipt["receipt_file"]
    if isinstance(value, (list, dict)):
        result["item_count"] = len(value)
    return result


def _preview(receipt, pointer, value, *, budget=1800, depth=2, reference_for=None):
    if len(_bytes(value)) <= budget:
        return copy.deepcopy(value)
    result = {"full_record": (reference_for(pointer, value) if reference_for else
                              reference(receipt, pointer, value)), "truncated": True}
    if depth == 0:
        return result
    if isinstance(value, Mapping):
        # Keep verdicts and locations ahead of potentially repeated audit ledgers.
        preferred = ("status", "id", "issue_id", "type", "code", "path", "field", "objects",
                     "divider", "opening", "opening_id", "look_box", "bbox", "end_offsets_m",
                     "declared", "gap", "reason", "fix",
                     "instruction", "total", "counts", "scope", "not_checked", "coverage",
                     "kind", "check", "summary", "grid_step_m", "findings", "items", "rejections")
        keys = [key for key in preferred if key in value]
        result["summary"] = {key: _preview(receipt, _child(pointer, key), value[key], budget=350,
                                           depth=depth - 1, reference_for=reference_for)
                             for key in keys[:12]}
    elif isinstance(value, list):
        result["items"] = [_preview(receipt, _child(pointer, i), row, budget=350,
                                     depth=depth - 1, reference_for=reference_for)
                           for i, row in enumerate(value[:4])]
        result["remaining"] = max(0, len(value) - 4)
    elif isinstance(value, str):
        result["preview"] = value[:300]
    return result


def report_summary(receipt, field):
    """Keep verdicts and located findings, with all omitted ledger data retrievable."""
    value, pointer = receipt[field], _child("", field)
    if not isinstance(value, Mapping):
        return _preview(receipt, pointer, value)
    keep = ("schema", "status", "total", "counts", "items", "coverage", "scope", "meaning",
            "not_checked", "reason", "wall_placement", "corridor_space_ids", "findings", "hint")
    # A floor's located differences are decision evidence, not a repeated audit
    # ledger. Keep a modest list together so offsets need no per-opening reads.
    result = {key: _preview(receipt, _child(pointer, key), value[key],
                          budget=8000 if field == "drawing_differences" and key == "items" else 1800)
            for key in keep if key in value}
    if field == 'drawing_differences' and isinstance(result.get('items'), list):
        # Explain each heuristic once; keep each location and offset together.
        guidance = {}
        for row in result['items']:
            if not isinstance(row, dict) or not isinstance(row.get('check'), str):
                continue
            check = row.pop('check')
            key = next((key for key, text in guidance.items() if text == check), None)
            key = key or f'g{len(guidance) + 1}'
            guidance[key] = check
            row['guidance'] = key
        if guidance:
            result['guidance'] = guidance
    return result


def bound_receipt_projection(receipt, visible, *, max_bytes=24_000):
    """Bound both receipt outcomes once, using references to the full original.

    Domain-specific summaries remain useful, but no summary path is allowed to
    bypass this final boundary. Small values retain their existing shape.
    """
    def size(value):
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))

    root = reference(receipt, "", receipt)

    def preview(value, pointer, *, budget=3000, depth=4, original=True):
        if size(value) <= budget:
            return copy.deepcopy(value)
        result = {"full_record": reference(receipt, pointer, value) if original else root,
                  "truncated": True}
        if depth == 0:
            return result
        if isinstance(value, Mapping):
            preferred = ("status", "id", "issue_id", "type", "code", "path", "field", "objects",
                         "divider", "opening_id", "bbox", "look_box", "reason", "fix", "instruction",
                         "repair_hint", "loose_partition_ends", "unhosted_openings",
                         "summary", "total", "counts", "items", "findings", "rejections")
            result["summary"] = {}
            for key in preferred:
                if key not in value:
                    continue
                child = preview(value[key], _child(pointer, key), depth=depth - 1,
                                budget=min(1800, max(0, budget - size(result) - 64)), original=original)
                result["summary"][key] = child
                if size(result) > budget:
                    result["summary"].pop(key)
        elif isinstance(value, list):
            result.update(items=[], remaining=len(value))
            for index, item in enumerate(value[:4]):
                child = preview(item, _child(pointer, index), depth=depth - 1,
                                budget=min(1200, max(0, budget - size(result) - 64)), original=original)
                result["items"].append(child)
                if size(result) > budget:
                    result["items"].pop()
                    break
                result["remaining"] -= 1
        elif isinstance(value, str):
            result["preview"] = value[:300]
            while size(result) > budget and result["preview"]:
                result["preview"] = result["preview"][:len(result["preview"]) // 2]
        return result

    projected = {}
    for field, value in visible.items():
        budget = (10_000 if field == "drawing_differences" else
                  6000 if field == "literal_geometry_diagnostic" else 3000)
        if size(value) <= budget:
            projected[field] = value
            continue
        # Re-project the original field, not an already summarized value: every
        # JSON pointer and digest must resolve against the saved full receipt.
        projected[field] = preview(receipt.get(field, value), _child("", field),
                                   budget=budget, original=field in receipt)
    if size(projected) <= max_bytes:
        return projected
    # A large number of individually small fields can exceed the total budget.
    # Keep the outcome, repair/submission instructions and precise evidence first.
    priority = ("status", "trial_id", "error_type", "source_geometry_ready", "plan_sha256",
                "receipt_file", "receipt_audit", "next_action", "submission", "reason", "message",
                "editable_draft", "repair_hint", "format_errors", "regularization_failure",
                "literal_geometry_diagnostic",
                "unhosted_openings", "source_findings", "topology_issues", "building_precision", "wall_metadata_summary",
                "drawing_differences", "audit_refs", "report_reading")
    result = {"receipt_audit": root, "omitted_field_count": 0}
    for field in dict.fromkeys((*priority, *projected)):
        if field not in projected or field == "receipt_audit":
            continue
        result[field] = projected[field]
        if size(result) > max_bytes:
            result.pop(field)
            result["omitted_field_count"] += 1
    return result


def passed_receipt(receipt):
    """Return decisions and changes; keep repeated bookkeeping in the full receipt."""
    visible = {}
    audit_only = {'original_plan_sha256', 'image', 'reading_product_file', 'reading_product_sha256',
        'candidate', 'draft', 'compiled_plan_sha256', 'input_plan_file', 'aligned_numeric_input_file',
        'aligned_numeric_input_sha256', 'unaligned_numeric_input_file', 'unaligned_numeric_input_sha256',
        'compiled_numeric_plan_file', 'compiled_numeric_plan_sha256', 'field_aliases',
        'length_bindings', 'measurement_bindings', 'measurement_profiles', 'reading_alignment',
        'alignment', 'overlay', 'base_plan_sha256', 'plan_revision', 'operations', 'phase',
        'topology_dividers', 'editable_draft'}
    for field, value in receipt.items():
        if field in audit_only:
            continue
        if field == "returned_images":
            visible[field] = [{key: image[key] for key in ("file", "sha256") if key in image}
                              for image in value if isinstance(image, Mapping)]
            continue
        pointer = _child("", field)
        if field in {"building_precision", "drawing_differences", "corridor_review"} and isinstance(value, Mapping):
            # A precision report can carry the complete regularization ledger three
            # times. Keep the audit verdict and located findings, not that ledger.
            visible[field] = report_summary(receipt, field)
        elif field == 'changes' and isinstance(value, list) and len(_bytes(value)) > 1800:
            visible[field] = {'count': len(value), 'items': [
                {key: row[key] for key in ('item', 'path', 'field', 'id') if key in row}
                for row in value], 'full_record': reference(receipt, pointer, value)}
        else:
            visible[field] = _preview(receipt, pointer, value)
    issues = receipt.get("topology_issues", [])
    visible["submission"] = {"status": "not_submitted", "topology_decisions_required": len(issues),
        "instruction": "Trial passed is a compiled floor draft, not a reader delivery or drawing-fidelity approval."}
    visible["next_action"] = {
        "tool": "submit_plan_reading", "arguments": {"trial_id": receipt.get("trial_id", "latest")},
        "instruction": ("Inspect the line overlay and thickness-bearing clean plan. Resolve every topology_issues entry "
                        "with a located topology_decision (read truncated lists via their reference); include "
                        "north_arrow if orientation_submission requires it, then submit this passed trial. "
                        "Use operations only if the drawing supports a correction; do not repeat the plan or audit ledger.")}
    # This digest binds every original field, including decision obligations.
    # It belongs only to the projection, so the durable receipt is not recursive.
    visible["receipt_audit"] = reference(receipt, "", receipt)
    visible["report_reading"] = ("read_plan_draft(field='report:/building_precision') browses the full receipt. "
        "Large containers return small child references; append /key or /index to inspect one. "
        "Use object_id='page:8' for the next page. No rebuilding occurs.")
    return bound_receipt_projection(receipt, visible)


def bounded_json_view(document, *, pointer="", offset=0, reference_for):
    """Browse verified JSON without returning large nested values in a page.

    The caller verifies the document and provides its own immutable references.
    ``reference_for(pointer, value)`` must return a small JSON reference. Returned
    ``next_offset`` is a child index, or a character offset for long text.
    """
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("report page offset must be a nonnegative integer")
    if not isinstance(pointer, str):
        raise ValueError("report JSON pointer must be text")
    if pointer and not pointer.startswith("/"):
        raise ValueError("report field must use a JSON pointer, e.g. report:/building_precision")
    value = document
    for raw in pointer.split("/")[1:] if pointer else []:
        if re.search(r"~(?![01])", raw):
            raise ValueError("invalid JSON pointer escape")
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, Mapping) and key in value:
            value = value[key]
        elif isinstance(value, list) and key.isdecimal() and str(int(key)) == key and int(key) < len(value):
            value = value[int(key)]
        else:
            raise ValueError("report JSON pointer does not identify a saved value")
    result = {"reference": reference_for(pointer, value)}
    if len(_bytes(value)) <= 3000 and offset == 0:
        result["value"] = copy.deepcopy(value)
    elif isinstance(value, (Mapping, list)):
        keys = list(value) if isinstance(value, Mapping) else list(range(len(value)))
        if offset >= len(keys) and offset != 0:
            raise ValueError("report page offset is beyond the saved value")
        entries = []
        for key in keys[offset:offset + 8]:
            child = _child(pointer, key)
            # Preserve a small row intact rather than replacing its useful
            # measurements with a larger reference and incomplete summary.
            preview = _preview(None, child, value[key], budget=1200, reference_for=reference_for)
            # A preview may itself contain several nested references. Bound the
            # complete entry, not just each original leaf, before returning it.
            if len(_bytes(preview)) > 1200:
                preview = _preview(None, child, value[key], budget=350, depth=0,
                                   reference_for=reference_for)
            entries.append({"key": key, "value": preview})
        result["entries"] = entries
        result["total"] = len(keys)
        if offset + 8 < len(keys):
            result["next_offset"] = offset + 8
    elif isinstance(value, str):
        if offset >= len(value) and offset != 0:
            raise ValueError("report page offset is beyond the saved text")
        result["text"] = value[offset:offset + 600]
        if offset + 600 < len(value):
            result["next_offset"] = offset + 600
    else:
        raise ValueError("report page requires a container or text")
    return result


def _page_offset(page):
    if page is not None:
        if not isinstance(page, str) or not page.startswith("page:") or not page[5:].isdecimal():
            raise ValueError("object_id must be page:<nonnegative offset> when browsing a JSON value")
        return int(page[5:])
    return 0


def read_plan(plan, identity, *, field=None, object_id=None, read_trial_id=None,
              read_tool="read_plan_draft", read_arguments=None):
    """Project a verified declaration; full geometry and audit stay external.

    Existing field/object lookups and JSON pointers share the same bounded
    browser. References always name the selected trial, never a mutable file.
    """
    target = read_trial_id or identity.get("trial_id", "latest")
    arguments = dict(read_arguments) if read_arguments is not None else {"trial_id": target}

    def make_reference(pointer, value):
        result = reference(identity, pointer, value)
        result["read"] = {"tool": read_tool, "arguments": {**arguments, "field": "plan:" + pointer}}
        result["plan_sha256"] = identity.get("plan_sha256")
        if identity.get("input_plan_file"):
            result["input_plan_file"] = identity["input_plan_file"]
        return result

    if field is None:
        if object_id is not None:
            raise ValueError("object_id requires a field or JSON pointer")
        fields = sorted(plan)
        result = {"fields": fields[:32], "reference": make_reference("", plan),
                  "object_ids": {}, "object_counts": {}}
        if len(fields) > 32:
            result["fields_remaining"] = len(fields) - 32
        for name in ("partitions", "openings", "space_seeds"):
            rows = plan.get(name)
            if isinstance(rows, list):
                result["object_counts"][name] = len(rows)
                result["object_ids"][name] = [
                    _preview(None, _child(_child(_child("", name), i), "id"), row.get("id"),
                             budget=100, depth=0, reference_for=make_reference)
                    for i, row in enumerate(rows[:12]) if isinstance(row, Mapping)]
        result["draft_reading"] = (
            "Object IDs show at most the first 12 rows per collection. Read a field or object_id; "
            "large values return entries and next pages. Use field='plan:/partitions/0' for a JSON pointer. "
            "Full saved geometry and audit records remain unchanged; reads do not compile.")
        return result
    if not isinstance(field, str):
        raise ValueError("field must name a saved field, plan, or plan:/JSON/pointer")
    if field.startswith("plan:"):
        pointer = field.removeprefix("plan:")
    elif field == "plan":
        pointer = ""
    elif field in plan:
        pointer = _child("", field)
        if object_id is not None and isinstance(plan[field], list):
            matches = [i for i, row in enumerate(plan[field])
                       if isinstance(row, Mapping) and row.get("id") == object_id]
            if len(matches) > 1:
                raise ValueError("object_id must identify exactly one saved object")
            if matches:
                pointer = _child(pointer, matches[0])
                object_id = None
            elif not isinstance(object_id, str) or not object_id.startswith("page:"):
                raise ValueError("object_id must identify exactly one saved object")
    else:
        raise ValueError(f"Saved declaration has no field {field!r}")
    result = bounded_json_view(plan, pointer=pointer, offset=_page_offset(object_id),
                               reference_for=make_reference)
    if "next_offset" in result:
        result["next"] = {**arguments, "field": "plan:" + pointer,
                          "object_id": f"page:{result.pop('next_offset')}"}
    if field == "plan" and "value" in result:
        result["plan"] = result.pop("value")
    return result


def read_report(receipt, pointer, *, page=None, read_trial_id=None):
    """Read only the selected in-memory receipt; never open a model-supplied path."""
    offset = _page_offset(page)
    target = read_trial_id or receipt.get("trial_id", "latest")

    def make_reference(path, value):
        result = reference(receipt, path, value)
        result["read"]["arguments"]["trial_id"] = target
        return result

    result = {"trial_id": receipt.get("trial_id"), "status": "reported",
              "trial_status": receipt.get("status"), **bounded_json_view(receipt, pointer=pointer,
                  offset=offset, reference_for=make_reference)}
    if "next_offset" in result:
        result["next"] = {"trial_id": target, "field": "report:" + pointer,
                          "object_id": f"page:{result.pop('next_offset')}"}
    return result
