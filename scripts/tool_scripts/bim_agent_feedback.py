"""Thin MCP input/feedback handling; no geometry edits or implicit observations.

T1: GLM sm21/24/25 used measurement labels as image names, sm24 exceeded
the zoom limit and skipped claim adoption, sm25 omitted edit fields. Historical
run53–58/98–100 also need actionable object, crop and note repair messages.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent
from pydantic import Field


ImageFilename = Annotated[str, Field(description="Filename from inputs(), never a measurement label; unique case/extension aliases accepted.")]


def resolve_image_name(images, name):
    """Exact names win; only a unique case/extension alias is safe to resolve."""
    if isinstance(name, str) and name in images:
        return name
    matches = []
    if isinstance(name, str) and name and Path(name).name == name and "\\" not in name:
        matches = [key for key in images if key.casefold() == name.casefold()
                   or (not Path(name).suffix and Path(key).stem.casefold() == name.casefold())]
    if len(matches) == 1:
        return matches[0]
    problem = "ambiguous" if matches else "unknown"
    raise ValueError(f"{problem} input image filename {name!r}; here fill the input image filename, "
                     "not a measurement label. Available filenames: " + json.dumps(sorted(images)))


_NAMED_IMAGE_TOOLS = {"view_image", "pixel_profile", "view_pixel_profile",
                      "view_pixel_region_overview", "view_pixel_region", "preview_space_trace"}


def normalize_images(toolkit, tool, arguments):
    notes = []
    def image(value):
        resolved = resolve_image_name(toolkit.manifest["images"], value)
        if resolved != value:
            notes.append(f"Input image {value!r}: used actual filename {resolved!r}.")
        return resolved
    def visit(value):
        if isinstance(value, list):
            return [visit(row) for row in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if key in {"image", "plan_image", "floor_plan_image", "elevation_image"} and isinstance(item, str) and item:
                result[key] = image(item)
            elif key == "images" and isinstance(item, list):
                result[key] = [image(name) for name in item]
            else:
                result[key] = visit(item)
        return result
    result = visit(copy.deepcopy(arguments))
    if tool in _NAMED_IMAGE_TOOLS and "name" in result:
        result["name"] = image(result["name"])
    for key, value in list(result.items()):
        if key.endswith("_json") and isinstance(value, str):
            try:
                decoded = json.loads(value)
            except ValueError:
                continue  # The tool reports its own format error.
            resolved = visit(decoded)
            if resolved != decoded:
                result[key] = json.dumps(resolved, ensure_ascii=False)
    return result, list(dict.fromkeys(notes))


def repair_hint(toolkit, tool, arguments, message):
    """Explain the rejected operation without guessing geometry or evidence."""
    if "reviews_json:" in message or "drawing_process:" in message:
        return 'Next: read get_bim_reference(topic="drawing_process") for the review and delivery format.'
    if "Run deadline reached" in message:
        return ""
    if "input image filename" in message or "Claim kinds use proposal collections" in message:
        return ""  # Already includes exact available choices and the next step.
    if "crop outside" in message:
        name = arguments.get("name")
        size = toolkit.manifest["images"].get(name, {}).get("size")
        return (f"Use ORIGINAL pixels for {name!r}, size {size}; omit box for the whole image, "
                "or use 0 <= left < right <= width and 0 <= top < bottom <= height.")
    if "explicitly adopted" in message:
        return ('Use claim_transaction with claim_id, your adoption reason and action=confirm '
                'for unchanged values, or action=apply to revise them; see the claims reference.')
    if "refer to the operation targets" in message:
        return ('Read claim_status(candidate) for exact objects/value_targets; use update_window for kind=window '
                'and update_opening for kind=opening. Record a new claim if its actual target differs.')
    if "differs from current geometry" in message:
        return "Use claim_transaction action=apply, or action=decide with disposition=deferred and a reason."
    if "stale for" in message:
        return "Read the reported changed targets/views/values; record new evidence for those targets. Unchanged targets can be handled separately in claim_transaction."
    if tool == "revise_plan_bim":
        try:
            ops = json.loads(arguments.get("operations_json", "[]"))
            if not isinstance(ops, list):
                return 'operations_json needs a JSON list of operation objects (需要列表), e.g. [{"op":"update", ...}], not {"operations":[...]}.'
            if any(not isinstance(row, dict) for row in ops):
                return "Each operations_json list element must be an operation object (列表元素须为对象)."
            index = int(re.search(r"operation (\d+)", message)[1]) if re.search(r"operation (\d+)", message) else 0
            op = ops[index]
        except (ValueError, TypeError, IndexError):
            op = {}
        kind = op.get("op", "update")
        example = dict(op=kind, reason="explain the observed correction", source_refs=["input filename: observed region"])
        if kind == "set":
            example.update(field=op.get("field", "ceiling_height"), value=op.get("value", 3.0))
        else:
            example["collection"] = op.get("collection", "partitions")
            if kind in {"update", "remove"}:
                example["id"] = op.get("id", "existing-wall-id")
            if kind == "update":
                example["changes"] = op.get("changes", {"points": op.get("points", [[10, 20], [100, 20]])})
            elif kind == "add":
                example["value"] = op.get("value", {"id": "new-wall-id", "points": [[10, 20], [100, 20]]})
        return "Minimum operation format (replace evidence placeholders): " + json.dumps([example])
    if "replace_note" in message:
        return ('Read inspect_candidate(candidate, include_geometry=false) for current notes; old must match an '
                'exact saved note. One operation replaces all identical copies in that field. Minimum: [{"op":"replace_note","field":"unresolved",'
                '"old":"exact current note","replacement":[],"reason":"why resolved",'
                '"source_refs":["input filename: observed evidence"]}].')
    if "set_notes" in message:
        return 'Minimum: [{"op":"set_notes","assumptions":["complete intended assumptions"],"unresolved":[]}]; omit reason/source_refs for set_notes.'
    if "requires an explicit assumption" in message:
        return ('Minimum: {"op":"set_space_role","space_id":"existing-id","role":"corridor",'
                '"basis":"inferred","assumptions":["explain the inferred use"],"reason":"why",'
                '"source_refs":["input filename: region"]}.')
    if "room_types catalog" in message:
        return "Next: get_bim_reference(topic=\"room_types\") and choose a listed role; inferred uses also need assumptions."
    if "expected exactly one existing id" in message:
        return "Next: inspect_candidate(candidate) for exact IDs; update_window edits windows and update_opening edits doors/passages."
    if tool == "check_openings":
        return 'Next: read get_bim_reference(topic="opening_review") for the review format.'
    if tool == "check_wall_dimensions":
        return 'Next: read get_bim_reference(topic="wall_dimensions") for the explicit wall reference and position format.'
    if tool == "compare_facade_spans":
        return 'Next: read get_bim_reference(topic="facade_correspondence") for independently declared axis anchors and opening spans.'
    if tool in {"prepare_elevation_reading", "apply_elevation_readings"}:
        return 'Next: read get_bim_reference(topic="drawing_process") for the reading, review and application format.'
    return "Next: read get_bim_reference(topic=" + json.dumps(
        "claims" if "claim" in tool else "edits" if "revise" in tool else "plan_partition") + ") for the operation format."


def plan_feedback_error(error, **plans):
    """Name a malformed declaration without filling in any geometry values."""
    from src.agent.geometry.plan_input import plan_error_hint
    missing = error.args[0] if isinstance(error, KeyError) and error.args else None
    for phase, plan in plans.items():
        if not isinstance(plan, dict):
            continue
        rows = plan.get('openings', [])
        for i, row in enumerate(rows if isinstance(rows, list) else []):
            path, field = f'plan.openings[{i}]', None
            if isinstance(row, dict):
                if missing in {'p1', 'p2', 'z', 'id', 'kind'} and missing not in row:
                    field = missing
                elif missing is None:
                    for k in ('p1', 'p2', 'z'):
                        value = row.get(k)
                        if (not isinstance(value, list) or len(value) != 2 or
                                any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value)):
                            field = k
                            break
                if field is None:
                    continue
                identity = row.get('id', f'index {i}')
                issue = f"missing field {field!r}" if field not in row else f"invalid field {field!r}; needs two finite numbers"
                reason = f"{phase} {path} ({identity!r}): {issue}"
            else:
                identity = f'index {i}'
                reason = f"{phase} {path}: opening must be an object"
            return dict(status='unavailable', reason=reason, object_id=identity,
                field=field, declaration=phase, error_type=type(error).__name__,
                repair_hint=plan_error_hint(plan, path))
        if missing in plan or missing is None:
            continue
        if missing in {'x_anchors', 'y_anchors', 'footprint_pixels', 'openings', 'floor_id', 'z_floor', 'ceiling_height'}:
            reason = f"{phase} plan: missing field {missing!r}"
            return dict(status='unavailable', reason=reason, field=missing, declaration=phase,
                error_type=type(error).__name__, repair_hint=plan_error_hint(plan, 'plan.' + missing))
    plan = next(iter(plans.values()), None)
    return dict(status='unavailable', reason=f'{type(error).__name__}: {error}',
        repair_hint=plan_error_hint(plan, str(error)))


class FeedbackMCP(FastMCP):
    """One response boundary shared by successful, image and failed tools."""
    def __init__(self, toolkit, *args, **kwargs):
        self.toolkit = toolkit
        super().__init__(*args, **kwargs)

    async def list_tools(self):
        # Pydantic's generated labels repeat parameter names on every request.
        # Only schema annotations are removed: a property/default named title
        # remains valid data, and validation requirements are unchanged.
        def without_titles(schema):
            if isinstance(schema, list):
                return [without_titles(v) for v in schema]
            if not isinstance(schema, dict):
                return schema
            result = {}
            for key, value in schema.items():
                if key == "title":
                    continue
                if key in {"properties", "$defs", "definitions", "patternProperties", "dependentSchemas"}:
                    result[key] = {name: without_titles(v) for name, v in value.items()}
                elif key in {"default", "examples", "enum", "const"}:
                    result[key] = value
                else:
                    result[key] = without_titles(value)
            return result
        return [tool.model_copy(update={"inputSchema": without_titles(tool.inputSchema)})
                for tool in await super().list_tools()]

    async def call_tool(self, name, arguments):
        notes = []
        try:
            from scripts.tool_scripts.bim_agent_budget import time_status
            before = time_status(self.toolkit)
            if before.get("remaining_seconds") is not None and before["remaining_seconds"] <= 0:
                raise ValueError("Run deadline reached; no further tool actions. Saved output will be handed off.")
            normalized, notes = normalize_images(self.toolkit, name, arguments)
            result = await super().call_tool(name, normalized)
        except Exception as error:
            message = str(error)
            # C2: T1 sm25 calls 63/65 lost the actual rejection when the hint
            # itself raised KeyError. An explanatory failure must never hide it.
            try:
                hint = repair_hint(self.toolkit, name, arguments, message)
            except Exception:
                hint = ""
            from scripts.tool_scripts.bim_agent_replies import compact_reply
            failure = compact_reply(self.toolkit.run, name, {
                'status': 'error', 'tool_name': name, 'error_type': type(error).__name__,
                'error': message, 'repair_hint': hint}, force=True)
            result = CallToolResult(isError=True, content=[TextContent(type="text",
                text=json.dumps(failure, ensure_ascii=False))], structuredContent=failure)
        notes.append(time_status(self.toolkit)["line"])
        if notes:
            extra = [TextContent(type="text", text="\n".join(notes))]
            if isinstance(result, CallToolResult):
                result.content.extend(extra)
            elif isinstance(result, tuple):
                result = (list(result[0]) + extra, result[1])
            elif isinstance(result, dict):
                result = (extra + [TextContent(type="text", text=json.dumps(result))], result)
            else:
                result = list(result) + extra
        return result
