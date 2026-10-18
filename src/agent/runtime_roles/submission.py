"""Explicit reader submission tools and their small durable delivery receipts."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import jsonschema
from PIL import Image

from .reader_delivery import LOCAL_REVIEW_SCHEMA, local_review, verify_views
from .plan_review import box, opening_hosts, topology_issues, validate_topology, validate_wall_reference


def obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


TEXT = {"type": "string", "minLength": 1}
NUMBER = {"type": "number"}
BOX = {"type": "array", "items": NUMBER, "minItems": 4, "maxItems": 4}
PAIR = {"type": "array", "items": NUMBER, "minItems": 2, "maxItems": 2}
STRINGS = {"type": "array", "items": TEXT}
EVIDENCE_TYPE = {"enum": ["annotation", "pixels", "annotation_and_pixels", "visual_estimate", "assumption", "declared"]}
WALL_LINE_SCHEMA = obj({"convention": {"enum": ["centerline", "inner_face", "outer_face", "explicit_face"]},
                        "dimension_basis": TEXT, "basis": TEXT, "bbox": BOX},
                       ("convention", "basis"))
WALL_REFERENCE_EXAMPLE = {
    "perimeter": {"convention": "outer_face", "dimension_basis": "outer_face",
                  "basis": "overall dimensions end on the observed outer faces", "bbox": [0, 0, 10, 10]},
    "partitions": {"convention": "centerline", "dimension_basis": "centerline",
                   "basis": "internal dimension anchors converted to observed divider midplanes", "bbox": [0, 0, 10, 10]},
}
OPERATION_SCHEMA = {"oneOf": [
    obj({"op": {"const": op}, "reason": TEXT, "source_refs": {**STRINGS, "minItems": 1}, "bbox": BOX, **fields},
        ("op", "reason", "source_refs", "bbox", *fields))
    for op, fields in {
        "update": {"collection": {"enum": ["partitions", "openings", "space_seeds"]}, "id": TEXT,
                   "changes": {"type": "object", "minProperties": 1}},
        "add": {"collection": {"enum": ["partitions", "openings", "space_seeds"]}, "value": {"type": "object"}},
        "remove": {"collection": {"enum": ["partitions", "openings", "space_seeds"]}, "id": TEXT},
        "set": {"field": TEXT, "value": {}},
    }.items()
]}
PLAN_SCHEMA = obj({
    "local_review": LOCAL_REVIEW_SCHEMA,
    "trial_id": {"type": "string", "pattern": "^(latest|trial_[0-9]{3,})$"},
    "plan_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
    "notes": {"type": "array", "items": obj({"item": TEXT,
        "kind": {"enum": ["assumption", "inferred", "unresolved"]}, "basis": TEXT}, ("item", "kind", "basis"))},
    "unresolved": STRINGS,
    "north_arrow": obj({"bbox": BOX, "basis": TEXT,
        "world_north_toward": {"enum": ["image_top", "image_bottom"]},
        "world_east_toward": {"enum": ["image_left", "image_right"]}},
        ("bbox", "basis", "world_north_toward", "world_east_toward")),
    "wall_reference": obj({"perimeter": WALL_LINE_SCHEMA, "partitions": WALL_LINE_SCHEMA}),
    "topology_decisions": {"type": "array", "items": obj({"issue_id": TEXT,
        "decision": {"enum": ["retain_opening", "continuous_space"]}, "basis": TEXT, "bbox": BOX},
        ("issue_id", "decision", "basis", "bbox"))},
})
PLAN_SCHEMA["required"] = ["local_review"]
PLAN_SCHEMA["anyOf"] = [{"required": ["trial_id"]}, {"required": ["plan_sha256"]}]

ELEVATION_SCHEMA = obj({
    "orientation": {"enum": ["North", "South", "East", "West"]},
    "view_direction": {"enum": ["North", "South", "East", "West"]},
    "x_calibration": obj({"pixel_start": NUMBER, "pixel_end": NUMBER, "world_start_m": NUMBER,
                          "world_end_m": NUMBER, "world_axis": {"enum": ["x", "y"]}},
                         ("pixel_start", "pixel_end", "world_start_m", "world_end_m")),
    "elevations": {"type": "array", "items": obj({"id": TEXT,
        "kind": {"enum": ["ground", "floor", "eave", "roof", "other"]}, "value_m": NUMBER,
        "floor_id": TEXT, "evidence_type": EVIDENCE_TYPE, "bbox": BOX},
        ("id", "kind", "value_m", "evidence_type", "bbox"))},
    "openings": {"type": "array", "items": obj({"id": TEXT, "floor_id": TEXT,
        "kind": {"enum": ["window", "door"]}, "x_px": PAIR, "width_m": NUMBER,
        "sill_m": NUMBER, "head_m": NUMBER, "evidence_type": EVIDENCE_TYPE, "bbox": BOX},
        ("id", "floor_id", "kind", "x_px", "width_m", "sill_m", "head_m", "evidence_type", "bbox"))},
    "counts": {"type": "array", "items": obj({"floor_id": TEXT,
        "window_count": {"type": "integer", "minimum": 0}, "door_count": {"type": "integer", "minimum": 0}},
        ("floor_id", "window_count", "door_count"))},
    "unresolved": STRINGS,
}, ("orientation", "view_direction", "x_calibration", "elevations", "openings", "counts", "unresolved"))

SUBMISSION_TOOLS = {
    "plan_reader": {"name": "submit_plan_reading",
        "description": "After inspecting the actual plan and original overlay, submit the latest passed trial with local_review.summary and trial_id=latest (or its ID/hash). This ends the reader task. Pixel evidence is generated automatically. Optional notes identify inferred/assumed/unresolved items; use plan.<field>, exact plan object paths, or trial.<field> for top-level fields actually present in this verified successful trial. Short field/object aliases must be unique across both namespaces. Verified 2D readings also allow omitted top-level plan contract fields, such as ceiling_height or z_floor; notes never populate them. Unknown fields or object IDs and ambiguous aliases are rejected. For 2D readings, thickness and expansion are already recorded in the passed plan; do not duplicate wall_reference. Topology warnings need decisions; mirrored axes still need north_arrow. Omit other empty fields.",
        "inputSchema": PLAN_SCHEMA},
    "elevation_reader": {"name": "submit_elevation_reading",
        "description": "Submit this facade's batched readings. x_px, calibration pixel_start/pixel_end and boxes use original pixels; world calibration, width_m and absolute value_m/sill_m/head_m use metres. The tool checks fields, counts, ordering and evidence, adopts levels and opening width/sill/head on the 0.1 m Lite grid, and retains original readings in regularization metadata; fix pointed errors and resubmit.",
        "inputSchema": ELEVATION_SCHEMA},
}


_ROLE_PREFIXES = {"plan_reader": ("plan", "floor"), "elevation_reader": ("elevation", "facade")}
_FLOOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")


def canonical_target(role_id, target):
    """One spelling per target. 10-07 sm24 run4: "plan/F1" and "elevation/North" were
    rejected, then "plan F1" was admitted as a floor ID that no reader plan could match."""
    if not isinstance(target, str):
        return target
    text = target.strip()
    prefixed = re.match(r"(?i)^(plan|floor|elevation|facade)\s*[/:\s]\s*(.+)$", text)
    if prefixed and prefixed.group(1).casefold() in _ROLE_PREFIXES.get(role_id, ()):
        text = prefixed.group(2).strip()
    if role_id == "elevation_reader":
        facade, slash, floors = text.partition("/")
        text = facade.strip().title()
        if slash:
            text += "/" + ",".join(part.strip().upper() for part in floors.split(","))
        return text
    return text.upper()


def parse_target(role_id, target):
    if target is None:
        return None, set()
    if not isinstance(target, str) or not target.strip():
        raise ValueError("reader target must be a nonempty floor ID or facade[/floor IDs]")
    target = canonical_target(role_id, target)
    if role_id == "plan_reader":
        if not _FLOOR_ID.fullmatch(target):
            raise ValueError(f"plan target must be one floor ID such as F1 (letters, digits, _ . -), not {target!r}")
        return target, set()
    parts = target.split("/", 1)
    facade = parts[0].strip().title()
    if facade not in {"North", "South", "East", "West"}:
        raise ValueError("elevation target must be North, South, East or West, optionally /F1 or /F1,F2")
    floors = {part.strip() for part in parts[1].split(",")} if len(parts) == 2 else set()
    if "" in floors:
        raise ValueError("elevation target floor IDs cannot be empty")
    return facade, floors


def _bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n").encode()


def _wall_defaults(plan, evidence, overrides, *, image_size=None):
    by_item = {row["item"]: row for row in evidence}
    result = {}
    for category, convention in (("perimeter", "outer_face"), ("partitions", "centerline")):
        # A default is a modeling convention, never a claim about a drawn label.
        result[category] = {
            "convention": convention, "dimension_basis": convention,
            "basis": f"Default modeling convention: {convention}; automatic box locates declared wall pixels, "
                     "not a measured dimension chain. Check plan.basis and assumptions for calibration.",
            "bbox": by_item["plan.footprint_pixels"]["bbox"],
        }
        if category == "partitions" and plan.get("partitions"):
            from .readers import pixel_box
            result[category]["bbox"] = pixel_box(
                [point for row in plan["partitions"] for point in row["points"]], image_size=image_size)
        if category in overrides:
            override = overrides[category]
            result[category].update(override)
            result[category]["dimension_basis"] = override.get("dimension_basis", override["convention"])
    return validate_wall_reference(result, image_size=image_size)


def _normalize_note_items(notes, targets):
    """Keep canonical paths; resolve aliases only when they identify one item."""

    aliases = {}

    def add(alias, target):
        aliases.setdefault(alias, set()).add(target)

    for target in sorted(targets):
        add(target, target)
        if target.startswith("trial."):
            add(target.removeprefix("trial."), target)
            continue
        short = target.removeprefix("plan.")
        add(short, target)
        if ":" in short:
            collection, identity = short.split(":", 1)
            singular = collection[:-1] if collection.endswith("s") else collection
            for alias in (
                identity,
                f"{collection}.{identity}",
                f"plan.{collection}.{identity}",
                f"{singular}:{identity}",
                f"{singular}.{identity}",
                f"plan.{singular}:{identity}",
            ):
                add(alias, target)
        else:
            add(short.removesuffix("_pixels"), target)

    result = []
    for index, raw in enumerate(notes):
        note = dict(raw)
        supplied = note["item"].strip()
        matches = {supplied} if supplied in targets else aliases.get(supplied, set())
        if len(matches) == 1:
            note["item"] = next(iter(matches))
        elif len(matches) > 1:
            raise ValueError(
                f"notes[{index}].item {supplied!r} is ambiguous; use one of "
                f"{sorted(matches)}"
            )
        else:
            raise ValueError(
                f"notes[{index}].item {supplied!r} is not a declared plan object or verified trial field; "
                f"use its exact item from {sorted(targets)}"
            )
        result.append(note)
    return result


class ReaderSubmission:
    def __init__(self, *, role_id, image_name, directory=None, trial=None, target=None):
        self.role_id, self.image_name, self.trial, self.target = role_id, image_name, trial, target
        self.target_identity, self.target_floors = parse_target(role_id, target)
        self.directory = Path(directory) if directory is not None else None
        self.path = self.directory / "reader_submission.json" if self.directory is not None else None
        self.value = None
        self.image_size = None
        if self.directory and (self.directory / "images" / image_name).is_file():
            with Image.open(self.directory / "images" / image_name) as image:
                self.image_size = image.size
        if self.path and self.path.is_file():
            self.value = json.loads(self.path.read_bytes())
            self.read()

    def read(self):
        if self.path and self.path.is_file():
            saved = json.loads(self.path.read_bytes())
            if self.value is not None and saved != self.value:
                raise ValueError("reader submission changed after acceptance")
            self.value = saved
        if self.value is None:
            return None
        value = self.value
        if value["role_id"] != self.role_id or value["image"] != self.image_name or value.get("target") != self.target:
            raise ValueError("reader submission scope changed")
        if hashlib.sha256(_bytes(value["artifact"])).hexdigest() != value["artifact_sha256"]:
            raise ValueError("reader submission artifact hash mismatch")
        if self.trial is not None:
            from .plan_reading import product_identity
            plan, receipt = self.trial.verified_plan(value["validation"]["plan_sha256"])
            compiled_plan = self.trial.numeric_plan(receipt)
            if (compiled_plan != value["artifact"]["plan"]
                    or product_identity(receipt) != product_identity(value["validation"])):
                raise ValueError("reader submission does not match its immutable successful trial")
        review = value["validation"].get("local_review")
        if review is not None:
            if review["artifact_sha256"] != value["artifact_sha256"]:
                raise ValueError("local review no longer matches the reader artifact")
            workspace = getattr(self.trial, "workspace", None)
            if workspace is not None:
                verify_views(workspace, review["views"])
        return value

    def submit(self, arguments):
        try:
            return self._submit(arguments)
        except (ValueError, KeyError, TypeError) as error:
            if "Minimum correct example" in str(error):
                raise ValueError(str(error)) from error
            from .guidance import ELEVATION_EXAMPLE
            example = ELEVATION_EXAMPLE if self.role_id == "elevation_reader" else {
                "trial_id": "latest", "local_review": {"summary": "Checked the actual plan/overlay; uncertain items are retained in unresolved."}}
            suffix = (" Include a decision for each reported topology issue; mirrored axes need north_arrow."
                      if self.role_id == "plan_reader" else "")
            raise ValueError(f"{SUBMISSION_TOOLS[self.role_id]['name']}: {error}. Minimum correct example: "
                             + json.dumps(example, separators=(",", ":")) + suffix) from error

    def _submit(self, arguments):
        from .readers import automatic_plan_evidence, validate_plan_artifact
        from .elevation import validate_elevation_artifact
        from .guidance import ELEVATION_EXAMPLE

        tool = SUBMISSION_TOOLS[self.role_id]
        # Old recorded calls may still supply located evidence. Keep and validate
        # it, but do not advertise redundant handwriting in the new tool schema.
        arguments = dict(arguments)
        legacy_evidence = arguments.pop("evidence", None) if self.role_id == "plan_reader" else None
        try:
            jsonschema.validate(arguments, tool["inputSchema"])
        except jsonschema.ValidationError as error:
            path = ".".join(str(part) for part in error.absolute_path) or "arguments"
            example = ELEVATION_EXAMPLE if self.role_id == "elevation_reader" else {
                "trial_id": "latest", "local_review": {"summary": "Checked the actual plan/overlay; uncertain items are retained in unresolved."}}
            raise ValueError(f"{tool['name']}.{path}: {error.message}. Minimum correct example: "
                             + json.dumps(example, separators=(",", ":"))) from error
        if self.role_id == "plan_reader":
            from .coordinates import validate_north_arrow
            passed = [(index, row) for index, row in enumerate(self.trial.receipts, 1)
                      if row.get("status") == "passed" and row.get("reading_geometry_ready", row.get("source_geometry_ready")) is not False]
            if not passed:
                raise ValueError("submission requires a successful isolated trial in this task")
            index, latest = passed[-1]
            latest_id = latest.get("trial_id", f"trial_{index:03d}")
            if arguments.get("trial_id", "latest") not in {"latest", latest_id}:
                raise ValueError(f"submit the latest successful trial {latest_id}")
            digest = arguments.get("plan_sha256", latest["plan_sha256"])
            if digest != latest["plan_sha256"]:
                raise ValueError(f"submit the latest successful trial {latest_id} or its plan_sha256")
            plan, validation = self.trial.verified_plan(digest)
            if self.target_identity is not None and plan.get("floor_id") != self.target_identity:
                raise ValueError(f"plan.floor_id {plan.get('floor_id')!r} differs from the task floor "
                                 f"{self.target_identity!r}; submit a trial made after this task started")
            numeric = self.trial.numeric_plan(validation)
            generated = automatic_plan_evidence(numeric, image_name=self.image_name, image_size=self.image_size)
            evidence = generated
            if legacy_evidence is not None:
                if not isinstance(legacy_evidence, list):
                    raise ValueError("legacy evidence must be a list")
                supplied = {row.get("item") for row in legacy_evidence if isinstance(row, dict)}
                evidence = [*legacy_evidence, *(row for row in generated if row["item"] not in supplied)]
            artifact = validate_plan_artifact({"plan": numeric, "evidence": evidence,
                                               "unresolved": numeric["unresolved"]}, image_name=self.image_name)
            note_targets = {
                row["item"] for row in artifact["evidence"]
            } | {f"plan.{field}" for field in set(plan) | set(numeric)}
            # This is the verified successful trial, before submission adds
            # notes/review/decisions. Notes cannot create missing report fields.
            note_targets.update(f"trial.{field}" for field in validation)
            if numeric.get("reading_type") == "plan" and validation.get("reading_geometry_ready") is True:
                from .plan_format import PLAN_FORMAT
                # An unknown optional property remains addressable through the
                # 2D contract, without inserting a value or pixel observation.
                note_targets.update(f"plan.{field}" for field in PLAN_FORMAT["properties"])
            notes = _normalize_note_items(arguments.get("notes", []), note_targets)
            # Field/collection notes stay in validation; an assumed height has
            # no pixel evidence to manufacture and must not change the trial.
            for note in notes:
                located = [row for row in artifact["evidence"] if row["item"] == note["item"]]
                for row in located:
                    row["basis"] = row.get("basis", "") + f" [{note['kind']}] {note['basis']}"
            # The delivery is the exact numeric plan that the successful trial
            # compiled, including deterministic reader alignment and its audit.
            artifact["plan"] = numeric
            artifact["unresolved"] = list(dict.fromkeys([*numeric["unresolved"], *arguments.get("unresolved", []),
                *(f"{note['item']}: {note['basis']}" for note in notes if note["kind"] == "unresolved")]))
            issues = list({row["issue_id"]: row for row in [*self.trial.inherited_topology_issues,
                           *topology_issues(self.trial.receipts)]}.values())
            validation = {**validation, "validation_passed": True,
                "wall_reference": _wall_defaults(numeric, generated, arguments.get("wall_reference", {}),
                                                  image_size=self.image_size),
                "evidence_origin": {"method": "verified_trial_pixels", "plan_sha256": digest,
                    "numeric_plan_sha256": validation.get("compiled_numeric_plan_sha256"),
                    "legacy_evidence_items": [row["item"] for row in legacy_evidence or []]},
                "notes": notes,
                "opening_hosts": opening_hosts(numeric),
                "topology_issues": issues,
                "topology_decisions": validate_topology(issues, arguments.get("topology_decisions", []),
                                                       numeric, image_size=self.image_size)}
            north_arrow = validate_north_arrow(numeric, arguments.get("north_arrow"),
                                               image_size=self.image_size)
            if north_arrow is not None:
                validation["north_arrow"] = north_arrow
            views = validation.get("returned_images", [])
            workspace = getattr(self.trial, "workspace", None)
            if workspace is not None:
                verify_views(workspace, views)
            validation["local_review"] = local_review(arguments["local_review"],
                artifact_sha256=hashlib.sha256(_bytes(artifact)).hexdigest(), views=views,
                image_sha256=(views[0].get("origin", {}).get("original_sha256") if views else None))
            boxes = [row["bbox"] for row in artifact["evidence"]]
        else:
            try:
                artifact = validate_elevation_artifact(arguments, image_name=self.image_name)
            except ValueError as error:
                raise ValueError(f"{error}. Minimum correct example: " + json.dumps(ELEVATION_EXAMPLE, separators=(",", ":"))) from error
            validation = {"validation_passed": True}
            boxes = [row["bbox"] for row in artifact["elevations"] + artifact["openings"]]
            if self.target_identity is not None and artifact["orientation"] != self.target_identity:
                raise ValueError(f"orientation must match task.target {self.target_identity}")
            floors = {row["floor_id"] for key in ("openings", "elevations", "counts")
                      for row in artifact[key] if row.get("floor_id") is not None}
            if self.target_floors and (not floors <= self.target_floors or
                    {row["floor_id"] for row in artifact["counts"]} != self.target_floors):
                raise ValueError(f"floor_id/counts must cover exactly task.target floors {sorted(self.target_floors)}")
        for evidence_box in boxes:
            box(evidence_box, self.image_size)
        value = {"role_id": self.role_id, "image": self.image_name, "target": self.target, "artifact": artifact,
                 "artifact_sha256": hashlib.sha256(_bytes(artifact)).hexdigest(), "validation": validation}
        previous = self.read()
        if previous is not None and previous != value:
            raise ValueError("this task already has an accepted submission; rework uses a new task_id")
        if previous is None and self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_bytes(_bytes(value))
            temporary.replace(self.path)
        self.value = value
        return {"status": "accepted", "artifact_sha256": value["artifact_sha256"],
                "plan_sha256": validation.get("plan_sha256"), "unresolved": artifact["unresolved"],
                "next_action": "Reviewed submission saved; reader task complete."}

    def snapshot(self):
        value = self.read()
        return None if value is None else {"artifact_sha256": value["artifact_sha256"],
            "receipt_sha256": hashlib.sha256(_bytes(value)).hexdigest()}
