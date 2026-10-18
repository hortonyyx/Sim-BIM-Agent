"""Validated single-image reader artifacts and least-privilege tool wrappers."""

from __future__ import annotations

from collections.abc import Mapping
import copy
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any

from src.agent.geometry.plan_input import normalize_plan_fields, plan_error_hint

from .parameters import normalize_stringified_parameters


PLAN_READER_TOOL_NAMES = (
    "inputs",
    "view_image",
    "view_plan_blocks",
    "pixel_profile",
    "view_pixel_profile",
    "view_pixel_region_overview",
    "view_pixel_region",
    "map_pixels",
    "map_dimension_chain",
    "get_bim_reference",
    "trial_plan_bim",
    "read_plan_draft",
    "submit_plan_reading",
)
ELEVATION_READER_TOOL_NAMES = tuple(
    name for name in PLAN_READER_TOOL_NAMES if name not in {"view_plan_blocks", "trial_plan_bim", "read_plan_draft", "submit_plan_reading"}
) + ("trial_elevation_reading", "submit_elevation_reading",)
READER_TOOL_NAMES = {
    "plan_reader": PLAN_READER_TOOL_NAMES,
    "elevation_reader": ELEVATION_READER_TOOL_NAMES,
}

_IMAGE_KEYS = {"image", "name", "plan_image", "floor_plan_image", "elevation_image"}
_REFERENCE_KEYS = {"view_id", "profile", "profile_id", "overview_id", "region_id"}

# sm24/sm21 traces had 20/32/35 looks before any trial. Six allows the whole
# image, the block batch and a few targeted looks, without forbidding further reads.
PRETRIAL_REMINDER_THRESHOLD = 6
PLAN_OBSERVATION_TOOLS = frozenset(PLAN_READER_TOOL_NAMES) - {
    "inputs", "get_bim_reference", "trial_plan_bim", "read_plan_draft", "submit_plan_reading",
}


def _minimum(path: str, example: object, problem: str) -> ValueError:
    rendered = json.dumps(example, ensure_ascii=False, separators=(",", ":"))
    return ValueError(f"{path}: {problem}. Minimum correct example: {rendered}")


def _decode_object(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise _minimum(
                "artifact",
                {"plan": {}, "evidence": [], "unresolved": []},
                f"invalid JSON at line {error.lineno} column {error.colno}",
            ) from error
    if not isinstance(value, Mapping):
        raise _minimum(
            "artifact",
            {"plan": {}, "evidence": [], "unresolved": []},
            "expected one JSON object",
        )
    return dict(value)


def _finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _point(value: object, path: str) -> list[object]:
    if not isinstance(value, list) or len(value) != 2:
        raise _minimum(path, [10, 20], "expected a two-number original-pixel point")
    if not all(_finite_number(part) or isinstance(part, Mapping) for part in value):
        raise _minimum(path, [10, 20], "each coordinate must be finite or a saved profile reference")
    return copy.deepcopy(value)


def _nonempty_text(value: object, path: str, example: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _minimum(path, example, "expected non-empty text")
    return value.strip()


def _string_list(value: object, path: str) -> list[str]:
    if not isinstance(value, list):
        raise _minimum(path, [], "expected a JSON list of strings")
    result = []
    for index, row in enumerate(value):
        result.append(_nonempty_text(row, f"{path}[{index}]", "state the unresolved item"))
    return result


def _validate_anchor(value: object, path: str) -> list[list[object]]:
    if not isinstance(value, list) or len(value) != 2:
        raise _minimum(path, [[10, 0.0], [110, 10.0]], "expected two [pixel, world_metres] anchors")
    result = []
    for index, row in enumerate(value):
        if not isinstance(row, list) or len(row) != 2:
            raise _minimum(f"{path}[{index}]", [10, 0.0], "expected [pixel, world_metres]")
        pixel, world = row
        if not (_finite_number(pixel) or isinstance(pixel, Mapping)) or not (
            _finite_number(world) or isinstance(world, Mapping)
        ):
            raise _minimum(f"{path}[{index}]", [10, 0.0], "anchor values must be finite or explicit references/quantities")
        result.append(copy.deepcopy(row))
    return result


def _validate_plan(plan_value: object) -> dict[str, Any]:
    if not isinstance(plan_value, Mapping):
        raise _minimum("artifact.plan", plan_error_hint({}, "plan_json")["example"], "expected the build_plan_bim plan object")
    try:
        plan, _ = normalize_plan_fields(dict(plan_value))
    except ValueError as error:
        raise ValueError(f"artifact.plan: {error}. Minimum correct example: " + json.dumps(
            plan_error_hint(plan_value, str(error)), ensure_ascii=False, separators=(",", ":")
        )) from error
    required = {
        "floor_id", "z_floor", "ceiling_height", "x_anchors", "y_anchors", "basis",
        "footprint_pixels", "partitions", "openings", "assumptions", "unresolved",
    }
    if plan.get("reading_type") == "plan":
        required -= {"z_floor", "ceiling_height"}
    missing = sorted(required - set(plan))
    if missing:
        field = missing[0]
        hint = plan_error_hint(plan, f"plan.{field}")
        raise ValueError(
            f"artifact.plan.{field}: missing required build_plan_bim field. Minimum correct example: "
            + json.dumps(hint.get("example", {field: None}), ensure_ascii=False, separators=(",", ":"))
        )
    _nonempty_text(plan["floor_id"], "artifact.plan.floor_id", "F1")
    _nonempty_text(plan["basis"], "artifact.plan.basis", "observed dimension anchors and wall reference planes")
    for field in ("z_floor", "ceiling_height"):
        if field not in plan and plan.get("reading_type") == "plan":
            continue
        if not (_finite_number(plan[field]) or isinstance(plan[field], Mapping)):
            raise _minimum(f"artifact.plan.{field}", 0.0 if field == "z_floor" else 3.0,
                           "expected finite metres or an explicit tagged quantity")
    plan["x_anchors"] = _validate_anchor(plan["x_anchors"], "artifact.plan.x_anchors")
    plan["y_anchors"] = _validate_anchor(plan["y_anchors"], "artifact.plan.y_anchors")
    ring = plan["footprint_pixels"]
    if not isinstance(ring, list) or len(ring) < 4:
        raise _minimum("artifact.plan.footprint_pixels", [[10, 10], [110, 10], [110, 110], [10, 110]],
                       "expected at least four original-pixel vertices")
    plan["footprint_pixels"] = [_point(row, f"artifact.plan.footprint_pixels[{index}]")
                                for index, row in enumerate(ring)]
    for collection in ("partitions", "openings", "space_seeds"):
        rows = plan.get(collection, [])
        if not isinstance(rows, list):
            raise _minimum(f"artifact.plan.{collection}", [], "expected a JSON list")
        seen: set[str] = set()
        for index, row in enumerate(rows):
            path = f"artifact.plan.{collection}[{index}]"
            if not isinstance(row, Mapping):
                hint = plan_error_hint(plan, f"plan.{collection}[{index}]")
                raise _minimum(path, hint.get("example", {}), "expected an item object")
            row = dict(row)
            identity = _nonempty_text(row.get("id"), path + ".id", f"{collection[:-1]}-id")
            if identity in seen:
                raise _minimum(path + ".id", f"unique-{identity}", f"duplicate id {identity!r}")
            seen.add(identity)
            if collection == "partitions":
                points = row.get("points")
                if not isinstance(points, list) or len(points) < 2:
                    raise _minimum(path + ".points", [[10, 20], [40, 20]], "expected at least two pixel points")
                row["points"] = [_point(point, f"{path}.points[{number}]") for number, point in enumerate(points)]
            elif collection == "openings":
                if row.get("kind") not in {"window", "door", "open"}:
                    raise _minimum(path + ".kind", "window", "expected window, door, or open")
                row["p1"] = _point(row.get("p1"), path + ".p1")
                row["p2"] = _point(row.get("p2"), path + ".p2")
                z = row.get("z")
                if not (z is None and plan.get("reading_type") == "plan") and (not isinstance(z, list) or len(z) != 2 or not all(
                    _finite_number(part) or isinstance(part, Mapping) for part in z
                )):
                    raise _minimum(path + ".z", [1.0, 2.4], "expected two absolute world heights")
            else:
                row["point"] = _point(row.get("point"), path + ".point")
            if collection != "space_seeds":
                refs = row.get("source_refs")
                if not (refs is None and plan.get("reading_type") == "plan") and (not isinstance(refs, list) or not refs or not all(isinstance(ref, str) and ref.strip() for ref in refs)):
                    raise _minimum(path + ".source_refs", ["plan.png: observed mark"],
                                   "expected at least one source description")
            rows[index] = row
        plan[collection] = rows
    if not isinstance(plan["assumptions"], list) or not all(
        isinstance(row, str) and row.strip() for row in plan["assumptions"]
    ):
        raise _minimum("artifact.plan.assumptions", [], "expected a list of explicit assumption strings")
    plan["unresolved"] = _string_list(plan["unresolved"], "artifact.plan.unresolved")
    return copy.deepcopy(plan)


def _evidence_targets(plan: Mapping[str, Any]) -> set[str]:
    targets = {"plan.x_anchors", "plan.y_anchors", "plan.footprint_pixels"}
    for collection in ("partitions", "openings", "space_seeds"):
        targets.update(f"plan.{collection}:{row['id']}" for row in plan.get(collection, []))
    return targets


def pixel_box(points, *, image_size=None, padding=2):
    """Locate declared pixels, not a new visual observation or dimension reading."""
    if not points or any(not isinstance(point, (list, tuple)) or len(point) != 2
                         or not all(_finite_number(value) for value in point) for point in points):
        raise ValueError("automatic evidence requires resolved original-image pixels")
    xs, ys = zip(*points)
    if min(xs) < 0 or min(ys) < 0 or (image_size and (
            max(xs) > image_size[0] or max(ys) > image_size[1])):
        raise ValueError("declared evidence pixels lie outside the original image")
    bounds = [max(0, min(xs) - padding), max(0, min(ys) - padding),
              max(xs) + padding, max(ys) + padding]
    if image_size:
        bounds[2], bounds[3] = min(image_size[0], bounds[2]), min(image_size[1], bounds[3])
    from .plan_review import box
    return box(bounds, image_size)


def automatic_plan_evidence(plan, *, image_name, image_size=None):
    """Keep the existing artifact contract, with boxes from the verified numeric plan.

    Anchors have only one pixel axis. Their boxes are coordinate bands across the
    footprint, not invented locations of printed dimensions. Original references,
    assumptions and unresolved statements remain in the immutable plan.
    """
    ring = plan["footprint_pixels"]
    xs, ys = zip(*ring)
    rows = []

    def add(item, points, basis):
        rows.append({"item": item, "source": image_name,
                     "bbox": pixel_box(points, image_size=image_size), "basis": basis})

    for axis, other in (("x", ys), ("y", xs)):
        positions = [row[0] for row in plan[f"{axis}_anchors"]]
        points = ([[value, bound] if axis == "x" else [bound, value]
                   for value in positions for bound in (min(other), max(other))])
        add(f"plan.{axis}_anchors", points,
            f"Derived {axis}-anchor coordinate band; the perpendicular dimension-text location is not supplied. "
            "Calibration interpretation remains in plan.basis; this box is not verification of the annotation.")
    add("plan.footprint_pixels", ring,
        "Derived footprint extent from trial pixels; observation and assumptions remain in plan.basis/assumptions.")
    for collection in ("partitions", "space_seeds", "openings"):
        for row in plan.get(collection, []):
            points = (row["points"] if collection == "partitions" else
                      [row["point"]] if collection == "space_seeds" else [row["p1"], row["p2"]])
            add(f"plan.{collection}:{row['id']}", points,
                "Derived location of the trial declaration, not an observation verdict. "
                + "Source descriptions: " + "; ".join(row.get("source_refs", []))
                + (". Assumptions: " + "; ".join(row["assumptions"]) if row.get("assumptions") else ""))
    return rows


def validate_plan_artifact(value: object, *, image_name: str | None = None) -> dict[str, Any]:
    """Return the normalized plan-reader artifact or a repair-oriented ValueError.

    The outer contract stays deliberately flat.  Geometry correctness belongs to
    the isolated trial compiler; this gate checks the existing plan shape and
    makes every declared item traceable to a localized box on the one task image.
    """

    artifact = _decode_object(value)
    if set(artifact) != {"plan", "evidence", "unresolved"}:
        missing = sorted({"plan", "evidence", "unresolved"} - set(artifact))
        extra = sorted(set(artifact) - {"plan", "evidence", "unresolved"})
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unexpected " + ", ".join(extra))
        raise _minimum("artifact", {"plan": {}, "evidence": [], "unresolved": []}, "; ".join(detail))
    plan = _validate_plan(artifact["plan"])
    unresolved = _string_list(artifact["unresolved"], "artifact.unresolved")
    if unresolved != plan["unresolved"]:
        raise _minimum(
            "artifact.unresolved",
            plan["unresolved"],
            "must exactly match plan.unresolved so trial and delivered artifact describe the same draft",
        )
    rows = artifact["evidence"]
    if not isinstance(rows, list):
        raise _minimum("artifact.evidence", [], "expected a JSON list")
    evidence = []
    targets = _evidence_targets(plan)
    covered: set[str] = set()
    for index, raw in enumerate(rows):
        path = f"artifact.evidence[{index}]"
        if not isinstance(raw, Mapping):
            raise _minimum(path, {"item": "plan.openings:W1", "source": image_name or "plan.png",
                                  "bbox": [10, 20, 30, 40]}, "expected an evidence object")
        extra = set(raw) - {"item", "source", "bbox", "basis"}
        if extra:
            raise _minimum(path, {"item": "plan.openings:W1", "source": image_name or "plan.png",
                                  "bbox": [10, 20, 30, 40]}, "unexpected fields " + ", ".join(sorted(extra)))
        item = _nonempty_text(raw.get("item"), path + ".item", "plan.openings:W1")
        if item not in targets:
            raise _minimum(path + ".item", sorted(targets)[0],
                           f"unknown item {item!r}; expected one of {sorted(targets)}")
        source = _nonempty_text(raw.get("source"), path + ".source", image_name or "plan.png")
        if image_name is not None and source != image_name:
            raise _minimum(path + ".source", image_name,
                           "must name the reader task's one original image")
        bbox = raw.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4 or not all(_finite_number(value) for value in bbox):
            raise _minimum(path + ".bbox", [10, 20, 30, 40], "expected four finite original-pixel bounds")
        if bbox[0] < 0 or bbox[1] < 0 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            raise _minimum(path + ".bbox", [10, 20, 30, 40],
                           "expected non-negative [left,top,right,bottom] with positive area")
        row = {"item": item, "source": source, "bbox": list(bbox)}
        if "basis" in raw:
            row["basis"] = _nonempty_text(raw["basis"], path + ".basis", "observed wall line and dimension")
        evidence.append(row)
        covered.add(item)
    missing_evidence = sorted(targets - covered)
    if missing_evidence:
        item = missing_evidence[0]
        raise _minimum("artifact.evidence", {"item": item, "source": image_name or "plan.png",
                                              "bbox": [10, 20, 30, 40]},
                       f"missing localized evidence for {item}")
    return {"plan": plan, "evidence": evidence, "unresolved": unresolved}


def _walk(value: object):
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key), child
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _structured(result: object) -> object:
    if isinstance(result, Mapping):
        structured = result.get("structuredContent")
        if structured is not None:
            return structured
        for block in result.get("content", []) if isinstance(result.get("content"), list) else []:
            if isinstance(block, Mapping) and block.get("type") == "text" and isinstance(block.get("text"), str):
                try:
                    return json.loads(block["text"])
                except json.JSONDecodeError:
                    continue
    return None


class ReaderTools:
    """Expose one reader's frozen read tools and a plan-only isolated trial."""

    @staticmethod
    def original_image_detail(result):
        """Keep reader coordinates in the returned pixel frame; bytes stay exact."""
        return {**result, "content": [
            {**part, "_meta": {**(part.get("_meta") or {}), "codex/imageDetail": "original"}}
            if part.get("type") == "image" else part
            for part in result.get("content", [])]}

    def __init__(self, frozen, *, role_id: str, image_name: str, trial=None, target=None, store=None):
        if role_id not in READER_TOOL_NAMES:
            raise ValueError(f"reader role must be one of {sorted(READER_TOOL_NAMES)}")
        if role_id == "plan_reader" and trial is None:
            raise ValueError("plan_reader requires an isolated plan trial")
        if role_id == "elevation_reader" and trial is not None:
            raise ValueError("elevation_reader cannot receive a plan trial")
        self.frozen = frozen
        self.store = store
        self.role_id = role_id
        self.image_name = _nonempty_text(image_name, "image_name", "plan.png")
        self.trial = trial
        self._catalog: dict[str, dict[str, Any]] | None = None
        self._issued_references: set[str] = set()
        self._progress = {"observation_calls_before_first_trial": 0, "by_tool": {},
                          "first_trial_epoch": None}
        run_directory = getattr(frozen, "run_directory", None)
        self._scope_directory = Path(run_directory).resolve() if run_directory is not None else None
        self._reference_file = (self._scope_directory / "reader_issued_references.json"
                                if self._scope_directory is not None else None)
        self._image_sha256 = self._admitted_image_sha256()
        self._load_references()
        if trial is not None and hasattr(trial, "inherited_reference_ids"):
            inherited = trial.inherited_reference_ids()
            if inherited - self._issued_references:
                self._issued_references.update(inherited)
                self._save_references()
        from .submission import ReaderSubmission
        self.submission = ReaderSubmission(role_id=role_id, image_name=image_name,
            directory=self._scope_directory, trial=trial, target=target)

    def _admitted_image_sha256(self) -> str | None:
        if self._scope_directory is None:
            return None
        manifest = self._scope_directory / "inputs.json"
        if not manifest.is_file():
            return None
        value = json.loads(manifest.read_bytes())
        row = value.get("images", {}).get(self.image_name, {})
        digest = row.get("sha256")
        return digest if isinstance(digest, str) else None

    def _load_references(self) -> None:
        if self._reference_file is None or not self._reference_file.is_file():
            return
        value = json.loads(self._reference_file.read_bytes())
        expected = {
            "schema": "reader_issued_references_v1",
            "role_id": self.role_id,
            "image": self.image_name,
            "image_sha256": self._image_sha256,
        }
        if not isinstance(value, dict) or any(value.get(key) != item for key, item in expected.items()):
            raise ValueError("saved reader reference scope does not match this role/image")
        references = value.get("references")
        if not isinstance(references, list) or any(not isinstance(row, str) or not row for row in references):
            raise ValueError("saved reader reference sidecar is malformed")
        if references != sorted(set(references)):
            raise ValueError("saved reader references must be unique and sorted")
        self._issued_references.update(references)
        progress = value.get("plan_reader_progress")
        if self.role_id == "plan_reader" and progress is not None:
            if (not isinstance(progress, dict)
                    or type(progress.get("observation_calls_before_first_trial")) is not int
                    or progress["observation_calls_before_first_trial"] < 0
                    or not isinstance(progress.get("by_tool"), dict)
                    or any(key not in PLAN_OBSERVATION_TOOLS or type(count) is not int or count < 0
                           for key, count in progress["by_tool"].items())
                    or sum(progress["by_tool"].values()) != progress["observation_calls_before_first_trial"]
                    or (progress.get("first_trial_epoch") is not None
                        and not _finite_number(progress["first_trial_epoch"]))):
                raise ValueError("saved plan reader progress is malformed")
            self._progress = copy.deepcopy(progress)

    def _save_references(self) -> None:
        if self._reference_file is None:
            return
        value = {
            "schema": "reader_issued_references_v1",
            "role_id": self.role_id,
            "image": self.image_name,
            "image_sha256": self._image_sha256,
            "references": sorted(self._issued_references),
        }
        if self.role_id == "plan_reader":
            value["plan_reader_progress"] = self._progress
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        temporary = self._reference_file.with_suffix(".tmp")
        temporary.write_text(raw, encoding="utf-8", newline="\n")
        temporary.replace(self._reference_file)

    async def list_tools(self) -> list[dict[str, Any]]:
        source = await self.frozen.list_tools()
        by_name = {tool.get("name"): tool for tool in source}
        wanted = READER_TOOL_NAMES[self.role_id]
        # The frozen wrapper intentionally hides historical ``pixel_profile``;
        # the reader contract retains it as a numeric-only alias for the current
        # view_pixel_profile(include_image=false) operation.
        if "pixel_profile" in wanted and "pixel_profile" not in by_name and "view_pixel_profile" in by_name:
            alias = copy.deepcopy(by_name["view_pixel_profile"])
            alias["name"] = "pixel_profile"
            alias["description"] = (
                "Return the current single-image pixel profile without an image; "
                "equivalent to view_pixel_profile(include_image=false)."
            )
            schema = alias.get("inputSchema")
            if isinstance(schema, dict) and isinstance(schema.get("properties"), dict):
                schema["properties"].pop("include_image", None)
            by_name["pixel_profile"] = alias
        from .submission import SUBMISSION_TOOLS, OPERATION_SCHEMA
        local = {"view_plan_blocks", "trial_plan_bim", "read_plan_draft", "submit_plan_reading", "submit_elevation_reading", "trial_elevation_reading"}
        missing = [name for name in wanted if name not in local and name not in by_name]
        if missing:
            raise ValueError(f"frozen reader catalog missing tools: {missing}")
        tools = [copy.deepcopy(by_name[name]) for name in wanted if name not in local]
        from .guidance import reader_references
        references = reader_references(self.role_id)
        reference = next(row for row in tools if row["name"] == "get_bim_reference")
        reference["description"] = (
            "Read this drawing role's contract when a detail needs clarification; "
            "the main workflow and example are already in the initial guide.")
        reference["inputSchema"] = {"type": "object", "additionalProperties": False,
            "properties": {"topic": {"type": "string", "enum": list(references)}},
            "required": ["topic"]}
        if self.role_id == "plan_reader":
            from .plan_views import PLAN_VIEWS_TOOL
            tools.append(copy.deepcopy(PLAN_VIEWS_TOOL))
            tools.append({"name": "read_plan_draft", "description": (
                "Read a saved declaration without rebuilding it, including failed drafts and inherited drafts. "
                "No arguments returns fields, object IDs, editability, operations base and last passed hash. "
                "Use field plus optional object_id for a local read; all reads are bounded. "
                "field='plan' browses the declaration; field='plan:/partitions/0' follows a saved JSON pointer. "
                "Use field='report:/building_precision' (JSON pointer) to browse the verified trial receipt; "
                "large fields return child references, and object_id='page:8' requests the next small page. "
                "trial_id defaults to latest; reference selects the inherited draft even after a new trial. "
                "Follow returned read/next arguments for more data. An editable draft is not necessarily submit-ready."),
                "inputSchema": {"type": "object", "properties": {
                    "trial_id": {"type": "string", "pattern": "^(latest|reference|trial_[0-9]{3,})$"},
                    "field": {"type": "string"}, "object_id": {"type": "string"}},
                    "additionalProperties": False}})
            tools.append({
                "name": "trial_plan_bim",
                "description": (
                    "Validate, regularize and render a 2D plan reading and original overlay. No source BIM is made. "
                    "Send one complete plan with reading_type='plan', floor_id, x_anchors, y_anchors, basis, "
                    "footprint_pixels, partitions, openings, space_seeds, assumptions, unresolved and wall_metadata. "
                    "Every wall needs positive thickness_m; use footprint:* and partition:* inferred defaults "
                    "plus exact exceptions. Exterior defaults inward from its outer face; partitions default "
                    "symmetric about the selected line. A one-sided exception uses expand_direction=up/down/left/right "
                    "in the original image; code resolves world sides. Batch defaults always adopt inferred evidence. "
                    "Omit unobserved z_floor/ceiling_height/opening z; assembly resolves them. "
                    "dimension_chains rows use id, axis, segments_mm, tick_pixels and source_refs; total_mm and "
                    "start_world_m are optional. tick_pixels accepts axis numbers or [x,y] points and has one more "
                    "item than segments_mm. Code extracts the stated axis and accumulates "
                    "shared chain nodes and adopts the configured Lite grid; do not hand-round or distribute residuals. "
                    "Reuse exact original-pixel coordinates at shared junctions and on opening hosts. "
                    "Dimensions bind once against original ticks; nearby ink does not overwrite geometry. "
                    "Original dimensions remain auditable. Failed returns named fields/objects and "
                    "repair actions. Operations use the remembered draft and preserve unpointed objects. Never omit an "
                    "observed wall/opening or change room topology to pass. Only passed trials can be submitted."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {"plan": {"type": "object"},
                                   "operations": {"type": "array", "items": OPERATION_SCHEMA,
                                                  "minItems": 1, "maxItems": 100}},
                    "oneOf": [{"required": ["plan"]}, {"required": ["operations"]}],
                    "additionalProperties": False,
                },
            })
        tools.append(copy.deepcopy(SUBMISSION_TOOLS[self.role_id]))
        self._catalog = {tool["name"]: tool for tool in tools}
        return tools

    def repeatability(self, name: str):
        if name in {"view_plan_blocks", "read_plan_draft"} and self.role_id == "plan_reader":
            return "read_only"
        if name == "trial_plan_bim":
            return "non_idempotent_write"
        if name in {"submit_plan_reading", "submit_elevation_reading"} and name in READER_TOOL_NAMES[self.role_id]:
            return "idempotent_write"
        if name not in READER_TOOL_NAMES[self.role_id]:
            raise ValueError(f"tool {name!r} is outside {self.role_id}")
        return self.frozen.repeatability(name)

    def recovery_policy(self, name: str) -> str:
        if name in {"view_plan_blocks", "read_plan_draft"} and self.role_id == "plan_reader":
            return "retry_read"
        if self.repeatability(name) != "read_only":
            return "manual"
        return getattr(self.frozen, "recovery_policy", lambda _: "manual")(name)

    def can_resume_reads(self, expected, observed, names):
        if any(self.recovery_policy(name) != "retry_read" for name in names):
            return False
        if any(expected.get(key) != observed.get(key) for key in ("frozen", "trial", "submission")):
            return False
        before, after = expected.get("reader_scope", {}), observed.get("reader_scope", {})
        if any(before.get(key) != after.get(key) for key in ("role_id", "image", "image_sha256")):
            return False
        # Observation progress and newly issued same-image reference caches can
        # be rebuilt. Existing references and protected BIM/trial state cannot.
        return set(before.get("references", ())) <= set(after.get("references", ()))

    def _enforce_single_image(self, arguments: Mapping[str, Any], *, plan_object=False) -> None:
        for key, value in _walk(arguments):
            # A plan's nested object name is not an image-selector parameter.
            # Actual image/profile selectors remain bound to the admitted input.
            if key in _IMAGE_KEYS and not (plan_object and key == "name") and isinstance(value, str) and value != self.image_name:
                raise ValueError(
                    f"{self.role_id} can use only image {self.image_name!r}; {key} referenced {value!r}"
                )
            if key == "images" and isinstance(value, list) and any(name != self.image_name for name in value):
                raise ValueError(f"{self.role_id} images must contain only {self.image_name!r}")
            if key in _REFERENCE_KEYS and isinstance(value, str) and value not in self._issued_references:
                raise ValueError(
                    f"{self.role_id} reference {value!r} was not returned by this single-image task"
                )

    def _remember_references(self, result: object) -> None:
        payload = _structured(result)
        if payload is None:
            return
        before = set(self._issued_references)
        for key, value in _walk(payload):
            if key in _REFERENCE_KEYS and isinstance(value, str):
                self._issued_references.add(value)
        if self._issued_references != before:
            self._save_references()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._catalog is None:
            await self.list_tools()
        if name not in self._catalog:
            raise ValueError(f"tool {name!r} is outside {self.role_id}")
        try:
            if not isinstance(arguments, Mapping):
                raise _minimum(name, {}, "tool arguments must be an object")
            arguments = normalize_stringified_parameters(
                arguments, self._catalog[name]["inputSchema"]
            )
            if name == "trial_plan_bim" and not (
                (set(arguments) == {"plan"} and isinstance(arguments["plan"], Mapping))
                or (set(arguments) == {"operations"} and isinstance(arguments["operations"], list))
            ):
                raise _minimum("trial_plan_bim", {"plan": {}},
                               "requires one complete plan or operations against a remembered plan; never both")
            self._enforce_single_image(arguments, plan_object=name == "trial_plan_bim")
            if name == "view_plan_blocks" and arguments:
                raise ValueError("view_plan_blocks takes no arguments; it uses this task's admitted image")
        except ValueError as error:
            if name not in {"trial_plan_bim", "submit_plan_reading", "submit_elevation_reading"}:
                raise
            # This admission check ran before any tool write. Return a known
            # rejection so the conservative write-outcome guard need not stop.
            reason = str(error)
            if "Minimum correct example" not in reason:
                reason += ". Minimum correct example: " + json.dumps({"image": self.image_name,
                    "reference": "use only a profile/view returned by this task"})
            value = {"status": "rejected", "reason": reason, "draft_unchanged": True,
                     "next_action": "Correct the named field and retry within this task budget; no trial was executed."}
            return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                    "structuredContent": value, "isError": True}
        if name == "read_plan_draft":
            try:
                result = self.trial.read_draft(**arguments)
                error = False
            except (ValueError, TypeError) as exc:
                result, error = {"status": "rejected", "reason": str(exc)}, True
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                    "structuredContent": result, "isError": error}
        if name in {"submit_plan_reading", "submit_elevation_reading"}:
            try:
                observation_reuse = None
                if name == "submit_plan_reading":
                    passed = [row for row in self.trial.receipts if row.get("status") == "passed"]
                    if passed:
                        from .reader_delivery import require_observed_plan_trial
                        receipt = passed[-1]
                        observation_reuse = require_observed_plan_trial(self.store, self.trial, receipt)
                result = self.submission.submit(dict(arguments))
                if observation_reuse is not None:
                    result["observation_reuse"] = observation_reuse
                return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                        "structuredContent": result, "isError": False}
            except (ValueError, KeyError) as error:
                result = {"status": "rejected", "reason": str(error),
                          "next_action": "Fix this item and call the submission tool again within the same task budget."}
                return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                        "structuredContent": result, "isError": True}
        if name == "trial_plan_bim":
            if self._progress["first_trial_epoch"] is None:
                self._progress["first_trial_epoch"] = time.time()
                self._save_references()
            try:
                if "plan" in arguments:
                    plan = dict(arguments["plan"])
                    baseline = self.trial.baseline()[0] if hasattr(self.trial, "baseline") else None
                    # Recovery must retain its admitted declaration contract.
                    # Inserting the new marker into a legacy rework would change
                    # an immutable field outside its permitted repair targets.
                    if baseline is None or baseline.get("reading_type") == "plan":
                        plan.setdefault("reading_type", "plan")
                    # The task fixes the floor ID; operations cannot edit it, so a
                    # different spelling would block delivery (10-07 sm24 run4).
                    if self.submission.target_identity is not None:
                        plan["floor_id"] = self.submission.target_identity
                    result = await self.trial.call(plan)
                else:
                    result = await self.trial.call(operations=arguments["operations"])
            except ValueError as error:
                from .trial import DraftRevisionRejected
                value = {"status": "rejected", "reason": str(error),
                         "next_action": "Correct the cited object or field in a complete plan, then retry; no trial was accepted."}
                if isinstance(error, DraftRevisionRejected):
                    value["draft_unchanged"] = True
                return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                        "structuredContent": value, "isError": True}
        else:
            if self.role_id == "plan_reader" and name in PLAN_OBSERVATION_TOOLS and self._progress["first_trial_epoch"] is None:
                self._progress["observation_calls_before_first_trial"] += 1
                counts = self._progress["by_tool"]
                counts[name] = counts.get(name, 0) + 1
                self._save_references()
            delegated_name = name
            delegated_arguments = dict(arguments)
            if name == "pixel_profile":
                delegated_name = "view_pixel_profile"
                delegated_arguments["include_image"] = False
            if name == "get_bim_reference":
                from .guidance import reader_references
                references = reader_references(self.role_id)
                topic = arguments.get("topic")
                if topic in references:
                    result = {"content": [{"type": "text", "text": references[topic]}], "isError": False}
                else:
                    result = {"content": [{"type": "text", "text": json.dumps({
                        "status": "unknown_reader_topic", "available_topics": list(references)},
                        ensure_ascii=False)}], "isError": True}
            elif name == "view_plan_blocks":
                from .plan_views import view_plan_blocks
                result = await view_plan_blocks(self.frozen, self.image_name)
            else:
                result = await self.frozen.call_tool(delegated_name, delegated_arguments)
        if name != "trial_plan_bim" and not (isinstance(result, Mapping) and result.get("isError") is True):
            self._remember_references(result)
        count = self._progress["observation_calls_before_first_trial"]
        if (self.role_id == "plan_reader" and name in PLAN_OBSERVATION_TOOLS
                and self._progress["first_trial_epoch"] is None and count == PRETRIAL_REMINDER_THRESHOLD):
            result = dict(result)
            result["content"] = [*result.get("content", []), {"type": "text", "text":
                f"You have made {count} observation calls without a trial; try the whole floor now, then use trial differences and its overlay for details."}]
        return self.original_image_detail(result)

    def progress(self, *, started_epoch=None):
        value = copy.deepcopy(self._progress)
        first = value["first_trial_epoch"]
        value.update(reminder_threshold=PRETRIAL_REMINDER_THRESHOLD,
                     first_trial_elapsed_seconds=(max(0, first - started_epoch)
                         if first is not None and started_epoch is not None else None))
        return value

    def snapshot_state(self):
        frozen = self.frozen.snapshot_state()
        references = {
            "role_id": self.role_id,
            "image": self.image_name,
            "image_sha256": self._image_sha256,
            "references": sorted(self._issued_references),
            "sidecar_sha256": (hashlib.sha256(self._reference_file.read_bytes()).hexdigest()
                               if self._reference_file is not None and self._reference_file.is_file() else None),
        }
        return {
            "frozen": frozen,
            "reader_scope": references,
            "trial": self.trial.durable_snapshot() if self.trial is not None else None,
            "submission": self.submission.snapshot(),
        }

    def artifacts(self):
        paths = list(self.frozen.artifacts())
        if self._reference_file is not None and self._reference_file.is_file():
            paths.append(self._reference_file)
        if self.trial is not None:
            paths.extend(self.trial.artifacts())
        if self.submission.path and self.submission.path.is_file():
            paths.append(self.submission.path)
        return sorted(set(paths))

    def image_origins(self, raw_result):
        origins = {}
        try:
            origins.update(self.frozen.image_origins(raw_result))
        except (AttributeError, TypeError, ValueError):
            pass
        if self.trial is not None:
            origins.update(self.trial.image_origins(raw_result))
        return origins
