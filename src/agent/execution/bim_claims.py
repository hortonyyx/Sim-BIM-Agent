"""Located observations whose values can be resolved into existing proposal edits.

Claims express model interpretations, not verified drawing truth. Decisions and
successful geometry application are deliberately separate. No geometry kernel
or universal image-to-building representation is introduced here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Annotated, Literal

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from src.agent.geometry.dimension_chain import map_dimension_chain
from src.agent.geometry.profile_observation_binding import resolve_pixel_slots


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode()


def sha(value):
    return hashlib.sha256(json_bytes(value)).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ObjectRef(StrictModel):
    kind: Literal["window", "opening", "space", "boundary"]
    id: str = Field(min_length=1)


class ImageRef(StrictModel):
    image: str = Field(min_length=1)
    # A whole drawing is a valid source; precise crop coordinates are optional.
    box: list[float] | None = Field(default=None, min_length=4, max_length=4)


class ViewedImageRef(StrictModel):
    view_id: str = Field(pattern=r"^view_\d{4,}$")


class LiteralValue(StrictModel):
    type: Literal["literal"]
    value: float | list[float]
    unit: Literal["m"]


class AxisValue(StrictModel):
    type: Literal["image_axis"]
    image: str
    axis: Literal["x", "y"]
    pixels: list[float | dict] = Field(min_length=1, max_length=2)
    anchors: list[list] = Field(min_length=2, max_length=2)
    reduction: Literal["midpoint"] | None = None


class ChainValue(StrictModel):
    type: Literal["dimension_chain"]
    lengths: list[float] = Field(min_length=1)
    unit: Literal["mm", "m"]
    origin_m: float
    direction: Literal[-1, 1]
    segment: int = Field(ge=0)


class Claim(StrictModel):
    candidate: str
    objects: list[ObjectRef] = Field(min_length=1)
    basis: Literal["annotation_and_pixels", "pixels", "visual_estimate", "inference", "declared"]
    reason: str = Field(min_length=1)
    sources: list[ImageRef | ViewedImageRef]
    values: dict[str, Annotated[LiteralValue | AxisValue | ChainValue, Field(discriminator="type")]]
    # Omitted preserves the legacy all-values/all-objects contract. An explicit
    # empty list marks supporting evidence, never an applicable parameter.
    value_targets: dict[str, list[ObjectRef]] | None = None
    observation_mode: Literal["direct", "candidate_review"] = "direct"
    unresolved: list[str] = Field(default_factory=list)


def geometry_state(proposal):
    """Source proposal geometry/semantics only, excluding notes and evidence metadata."""
    excluded = {"corrections", "notes", "source_refs", "assumptions"}
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k not in excluded}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value
    return clean(proposal["geometry"])


def objects(proposal, source):
    geometry = geometry_state(proposal)
    result = {}
    for kind in ("window", "opening"):
        result.update({(kind, row["id"]): row for row in geometry.get(kind+"s", [])})
    for floor in geometry["floors"]:
        result.update({("space", row["id"]): {"floor": floor["name"], **row}
                       for row in floor["cells"]})
    result.update({("boundary", row["id"]): row for row in source["boundaries"]})
    return result


def value_targets(claim, field):
    mapping = claim.get("value_targets")
    return claim["objects"] if mapping is None else mapping[field]


def parameter_slots(operation):
    """Only supported numerical slots; paths are audit labels, not JSON pointers."""
    name = operation.get("op")
    if name in {"update_window", "update_opening"}:
        targets = [(name.removeprefix("update_"), operation.get("id"))]
        for field in operation.get("changes", {}):
            yield operation["changes"], field, field, targets, field in {"z", "span", "p1", "p2"}
    elif name == "add_opening":
        opening = operation.get("opening", {})
        # Observe the existing host spaces before the new object exists.
        targets = [("space", opening[key]) for key in ("space_id", "other_space_id") if opening.get(key) is not None]
        for field in ("p1", "p2", "z"):
            if field in opening:
                yield opening, field, field, targets, True
    elif name in {"move_shared_wall", "set_component_thickness"}:
        field = "coordinate_m" if name == "move_shared_wall" else "thickness_m"
        targets = ([("space", identity) for identity in operation.get("space_ids", [])]
                   if name == "move_shared_wall" else [("boundary", operation.get("boundary_id"))])
        yield operation, field, field, targets, True
    elif name == "reshape_spaces":
        for i, row in enumerate(operation.get("spaces", [])):
            for j, point in enumerate(row.get("polygon", [])):
                for axis in range(len(point)):
                    yield point, axis, f"spaces.{i}.polygon.{j}.{axis}", [("space", row.get("id"))], True


class ClaimStore:
    def __init__(self, toolkit):
        self.toolkit = toolkit
        self.run = toolkit.run
        self.folder = self.run / "claims"

    def candidate(self, name):
        path = self.toolkit.candidate_path(name)
        return (json.loads((path / "proposal.json").read_bytes()),
                json.loads((path / "source_model.json").read_bytes()))

    def _write(self, prefix, value):
        self.folder.mkdir(exist_ok=True)
        index = len(list(self.folder.glob(f"{prefix}_*.json"))) + 1
        name = f"{prefix}_{index:04d}"
        with (self.folder / f"{name}.json").open("xb") as stream:
            stream.write(json_bytes({"id": name, **value}))
        return {"id": name, **value}

    def _profile(self, name):
        if not re.fullmatch(r"profile_\d{3,}", name):
            raise ValueError("invalid profile ID")
        path = self.run / "pixel_profiles" / f"{name}.json"
        raw = path.read_bytes()
        return {"record": json.loads(raw), "sha256": hashlib.sha256(raw).hexdigest()}

    def _sources(self, claim):
        sources = []
        if claim["basis"] in {"annotation_and_pixels", "pixels", "visual_estimate"} and not claim["sources"]:
            raise ValueError("image-based claims require a located source")
        for source in claim["sources"]:
            view_binding = {}
            if "view_id" in source:
                viewed = self.toolkit.read_image_view(source["view_id"])
                metadata = viewed["record"]
                view_binding = {"view_id": source["view_id"], "view_sha256": viewed["sha256"]}
                source = {"image": metadata["name"], "box": metadata["box_original_pixels"]}
            path = self.toolkit.image_path(source["image"])
            with Image.open(path) as picture:
                width, height = picture.size
            box = source["box"] if source["box"] is not None else [0, 0, width, height]
            left, top, right, bottom = box
            if not (0 <= left < right <= width and 0 <= top < bottom <= height):
                raise ValueError("source box outside original image")
            sources.append({**source, **view_binding, "box": box, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                            "size": [width, height]})
        return sources

    def _resolve_values(self, claim, sources):
        resolved, computations = {}, {}
        for field, spec in claim["values"].items():
            if spec["type"] == "literal":
                resolved[field] = spec["value"]
                computations[field] = {"method": "explicit_metric_value", "basis": claim["basis"]}
            elif spec["type"] == "dimension_chain":
                if not sources and claim["basis"] not in {"inference", "declared"}:
                    raise ValueError("a dimension chain needs located annotation evidence")
                chain = map_dimension_chain(spec["lengths"], unit=spec["unit"],
                    origin_m=spec["origin_m"], direction=spec["direction"])
                if spec["segment"] >= len(chain["segments"]):
                    raise ValueError("dimension segment outside chain")
                resolved[field] = chain["segments"][spec["segment"]]["span_m"]
                computations[field] = {"method": "dimension_chain", "chain": chain,
                                       "segment": spec["segment"]}
            else:
                matches = [row for row in sources if row["image"] == spec["image"]]
                if not matches:
                    raise ValueError("image_axis requires the image in sources")
                source = matches[0]
                if any(len(anchor) != 2 for anchor in spec["anchors"]):
                    raise ValueError("anchors require two [pixel, world_metres] pairs")
                pixels, bindings = resolve_pixel_slots(
                    [a[0] for a in spec["anchors"]] + spec["pixels"],
                    view={"image": source["image"], "axis": spec["axis"], "sha256": source["sha256"]},
                    load_profile=self._profile)
                bound = source["size"][0 if spec["axis"] == "x" else 1]
                if any(not 0 <= point <= bound for point in pixels):
                    raise ValueError("pixel coordinate outside original image")
                world = [LiteralValue(type="literal", value=a[1], unit="m").value for a in spec["anchors"]]
                if any(isinstance(v, list) for v in world) or pixels[0] == pixels[1] or world[0] == world[1]:
                    raise ValueError("distinct scalar anchors required")
                scale = (world[1]-world[0]) / (pixels[1]-pixels[0])
                values = [round(world[0] + (point-pixels[0])*scale, 9) for point in pixels[2:]]
                resolved[field] = values[0] if len(values) == 1 else sorted(values)
                if spec.get("reduction") == "midpoint":
                    if len(values) != 2:
                        raise ValueError("midpoint requires two measured faces")
                    resolved[field] = round(sum(values) / 2, 9)
                computations[field] = {"method": "image_axis", "axis": spec["axis"],
                    "pixels": pixels, "metre_anchors": world, "metres_per_pixel": scale,
                    "measurement_bindings": bindings, "calibration": "caller_interpreted_not_verified"}
                if spec.get("reduction"):
                    computations[field]["reduction"] = spec["reduction"]
        json_bytes(resolved)
        return resolved, computations

    def _prepare(self, data):
        claim = Claim.model_validate(data).model_dump()
        json_bytes(claim)
        if not claim["reason"].strip() or not claim["values"] or any(not k.strip() for k in claim["values"]):
            raise ValueError("nonempty reason and named values required")
        proposal, source = self.candidate(claim["candidate"])
        inventory = objects(proposal, source)
        for ref in claim["objects"]:
            if (ref["kind"], ref["id"]) not in inventory:
                same_id = [{"kind": kind, "id": identity}
                           for kind, identity in sorted(inventory) if identity == ref["id"]]
                detail = ("The same exact id exists as " + json.dumps(same_id) +
                          ". Use the matching kind/id explicitly; no automatic rebinding."
                          if same_id else
                          "Inspect the current candidate's exact object IDs before retrying.")
                raise ValueError(
                    "claim object does not exist in candidate " + repr(claim["candidate"]) +
                    ": " + json.dumps(ref) + ". " + detail +
                    " Claim kinds use proposal collections: window for geometry.windows; "
                    "opening for geometry.openings (doors/passages), even though the source "
                    "BIM lists both in openings.")
        mapping = claim["value_targets"]
        if mapping is not None:
            if set(mapping) != set(claim["values"]):
                raise ValueError("value_targets must map every value, including supporting values with []")
            declared = {(ref["kind"], ref["id"]) for ref in claim["objects"]}
            for refs in mapping.values():
                if any((ref["kind"], ref["id"]) not in declared for ref in refs):
                    raise ValueError("value_targets must refer to declared claim objects")
            if not any(mapping.values()):
                raise ValueError("claim requires at least one application value target")
        sources = self._sources(claim)
        values, computations = self._resolve_values(claim, sources)
        return {"claim": claim, "parent_proposal_sha256": sha(proposal),
            "sources": sources, "resolved_values": values, "computations": computations,
            "verification": "not_independently_verified"}

    def record(self, data):
        return self._write("claim", self._prepare(data))

    def replace_sources(self, identity, view_ids, reason):
        """Replace references on the same candidate; never silently change values."""
        if (not isinstance(view_ids, list) or not view_ids or not isinstance(reason, str)
                or not reason.strip()):
            raise ValueError("nonempty view_ids and replacement reason required")
        old = self.read(identity)
        proposal, _ = self.candidate(old["claim"]["candidate"])
        if sha(proposal) != old["parent_proposal_sha256"]:
            raise ValueError("claim parent changed; record a new observation")
        data = copy.deepcopy(old["claim"])
        data.update(sources=[{"view_id": value} for value in view_ids], reason=reason)
        prepared = self._prepare(data)
        if prepared["resolved_values"] != old["resolved_values"]:
            raise ValueError("source replacement changed values; record a new observation")
        # Validate before either append: an invalid view must not retract good evidence.
        row = self._write("claim", {**prepared, "replaces_claim": identity,
            "replaced_claim_sha256": sha(old)})
        decision = self.decide(identity, "retracted", f"Sources replaced by {row['id']}: {reason}")
        return {**row, "superseded_decision": decision,
                "next_action": "Inspect the returned sources, decide whether to adopt the new claim, "
                               "then confirm/apply it. Previous confirmations are not transferred."}

    def read(self, identity):
        if not re.fullmatch(r"claim_\d{4,}", identity):
            raise ValueError("invalid claim ID")
        return json.loads((self.folder / f"{identity}.json").read_text())

    def decide(self, identity, disposition, reason):
        self.read(identity)
        if disposition not in {"adopted", "deferred", "retracted"} or not reason.strip():
            raise ValueError("choose adopted/deferred/retracted with a reason")
        return self._write("decision", {"claim_id": identity, "disposition": disposition, "reason": reason})

    def decisions(self):
        result = {}
        for path in sorted(self.folder.glob("decision_*.json")):
            row = json.loads(path.read_text())
            result[row["claim_id"]] = row
        return result

    def resolve_operations(self, candidate, operations):
        """Bind values to supported edit parameters, leaving other edits explicit/unbound."""
        proposal, source = self.candidate(candidate)
        decisions, bindings, snapshots = self.decisions(), [], {}
        resolved = copy.deepcopy(operations)
        if not isinstance(resolved, list) or not resolved:
            raise ValueError("operations must be a nonempty list")
        for index, operation in enumerate(resolved):
            if not isinstance(operation, dict):
                raise ValueError("operation must be an object")
            for container, field, path, targets, supported in parameter_slots(operation):
                value = container[field]
                if not isinstance(value, dict) or "claim" not in value:
                    continue
                if not supported:
                    raise ValueError("unsupported claim parameter")
                if set(value) != {"claim", "value"}:
                    raise ValueError("parameter reference requires only claim and value")
                row = self.read(value["claim"])
                decision = decisions.get(row["id"])
                if not decision or decision["disposition"] != "adopted":
                    raise ValueError("claim must be explicitly adopted before application")
                if row["parent_proposal_sha256"] != sha(proposal):
                    raise ValueError("claim is stale for this parent proposal; record a new claim")
                if value["value"] not in row["resolved_values"]:
                    raise ValueError("unknown claim value")
                claimed = {(r["kind"], r["id"]) for r in value_targets(row["claim"], value["value"])}
                if not targets or not set(targets) <= claimed:
                    raise ValueError("claim does not refer to the operation targets")
                sources = self._sources(row["claim"])
                values, computations = self._resolve_values(row["claim"], sources)
                if sources != row["sources"] or values != row["resolved_values"] or computations != row["computations"]:
                    raise ValueError("claim evidence changed since recording")
                if value["value"] not in values:
                    raise ValueError("unknown claim value")
                parameter = values[value["value"]]
                if field in {"p1", "p2"} and row["claim"]["values"][value["value"]]["type"] != "literal":
                    raise ValueError("point coordinates require an explicit literal [x, y]; axis intervals are not points")
                if field in {"coordinate_m", "thickness_m"} or operation["op"] == "reshape_spaces":
                    if isinstance(parameter, list):
                        raise ValueError("parameter requires a scalar value")
                elif not isinstance(parameter, list) or len(parameter) != 2:
                    raise ValueError("parameter requires two coordinates")
                container[field] = copy.deepcopy(parameter)
                reference = f"claim:{row['id']}:{sha(row)}"
                if reference not in operation.setdefault("source_refs", []):
                    operation["source_refs"].append(reference)
                snapshots[row["id"]] = {"record": row, "decision": decision}
                bindings.append({"operation_index": index, "parameter": path, "targets": [list(target) for target in targets],
                    "claim_id": row["id"], "value_field": value["value"],
                    "resolved_value": parameter, "computation": computations[value["value"]]})
        return resolved, {"parent_candidate": candidate, "parent_proposal_sha256": sha(proposal),
                          "bindings": bindings, "claims": snapshots}

    def status(self, candidate=None):
        decisions = self.decisions()
        claims = []
        applications = [json.loads(path.read_text()) for path in sorted(self.folder.glob("application_*.json"))]
        for path in sorted(self.folder.glob("claim_*.json")):
            row = json.loads(path.read_text())
            if candidate is not None and row["claim"]["candidate"] != candidate:
                continue
            uses = [app for app in applications if row["id"] in app.get("claim_ids", [])]
            claims.append({**row, "decision": decisions.get(row["id"]), "applications": uses,
                           "applied_anywhere": any(app["status"] == "applied" for app in uses)})
        return {"claims": claims, "applications": applications,
                "drawing_fidelity": "not_evaluated",
                "note": "Adopted, applied, and faithful to the drawing are separate conclusions."}
