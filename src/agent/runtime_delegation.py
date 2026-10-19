"""Building evidence packages for the domain-neutral child runtime.

Pixels come from recorded frozen-tool returns. The observer supplies only local
IDs and boxes; trusted code attaches their full run/hash/coordinate identities.
No observation is itself a BIM mutation or a claim of geometric correctness.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from src.agent.contracts import EvidencePackage, LocalizedEvidenceResult, assert_result_applicable
from src.agent.contracts.refs import CoordinateRelation, ExistingEvidenceRef, RunQualifiedEvidenceRef
from src.agent.runtime_tools import local_observer_role, authorize_tool_call
from src.agent_runtime.context import ContextPolicy, StateEntry
from src.agent_runtime.loop import RunLimits, Runtime
from src.agent_runtime.versions import make_versions
from src.harness_contracts import HashedBlobRef


OBSERVER_GUIDANCE = """You are the read-only local observer for a BIM Agent.
Answer the supplied local question using only the supplied evidence. Report
directly visible marks separately from architectural interpretation and what
cannot be determined. A rendering is not a photograph; a BIM rendering is not
proof of the real building. Do not invent measurements or copy ordinary-window
dimensions to other windows without their evidence. Do not edit the BIM.
Return one JSON object matching the provided answer schema, without markdown.
Each visible observation needs its supplied view_id and a tight ORIGINAL-image
pixel box around its evidence. Use the documented crop/scale transformation.
Keep observations concise and complete for the question. Numeric answers and
units belong in the statements. Unknown quantities belong in uncertain.
Respect task_limits across the whole task and the remaining budget updates.
Tool image selectors name/image/plan_image/elevation_image must exactly copy a
tool_image_names entry. In pixel_profile, name is an INPUT IMAGE FILENAME, not
a name for a new measurement. Measurements must stay inside a supplied crop.
"""


class ObservationAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observation_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    view_id: str = Field(pattern=r"^view_\d{4,}$")
    box_original_pixels: tuple[float, float, float, float]


class InterpretationAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interpretation_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    based_on_observation_ids: tuple[str, ...] = Field(min_length=1)
    confidence: str = Field(pattern="^(low|medium|high)$")


class UncertaintyAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uncertainty_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    related_observation_ids: tuple[str, ...] = ()


class ObserverAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    directly_seen: tuple[ObservationAnswer, ...]
    interpretations: tuple[InterpretationAnswer, ...] = ()
    uncertain: tuple[UncertaintyAnswer, ...] = ()


@dataclass(frozen=True)
class RegisteredView:
    reference: RunQualifiedEvidenceRef
    original: HashedBlobRef
    sent: HashedBlobRef
    original_size: tuple[int, int]
    sent_size: tuple[int, int]
    image_name: str
    tool_event_id: str

    @property
    def view_id(self):
        return self.reference.reference.value

    def as_json(self):
        return {"reference": self.reference.model_dump(mode="json"),
            "original": self.original.model_dump(mode="json"),
            "sent": self.sent.model_dump(mode="json"),
            "original_size": list(self.original_size), "sent_size": list(self.sent_size),
            "image_name": self.image_name, "tool_event_id": self.tool_event_id}

    @classmethod
    def from_json(cls, value):
        return cls(reference=RunQualifiedEvidenceRef.model_validate_json(json.dumps(value["reference"])),
            original=HashedBlobRef.model_validate_json(json.dumps(value["original"])),
            sent=HashedBlobRef.model_validate_json(json.dumps(value["sent"])),
            original_size=tuple(value["original_size"]), sent_size=tuple(value["sent_size"]),
            image_name=value["image_name"], tool_event_id=value["tool_event_id"])


def views_from_tool_return(store, tools, raw, event_id):
    """Register only existing view IDs with verified original and sent bytes."""
    origins = tools.image_origins(raw)
    rows = []
    for block in raw.get("content", []):
        if block.get("type") != "image":
            continue
        data = base64.b64decode(block["data"], validate=True)
        digest = hashlib.sha256(data).hexdigest()
        origin = origins.get(digest, {})
        if not origin.get("view_id") or not origin.get("original_path"):
            # Composite overlays and raw mesh renders are still logged, but do
            # not pretend to have an invertible original-image coordinate map.
            continue
        original_path = Path(origin["original_path"]).resolve()
        if not original_path.is_relative_to(tools.run_directory):
            raise ValueError("original view escapes admitted run inputs")
        original_bytes = original_path.read_bytes()
        if hashlib.sha256(original_bytes).hexdigest() != origin["original_sha256"]:
            raise ValueError("original image changed after its tool return")
        with Image.open(io.BytesIO(original_bytes)) as picture:
            width, height = picture.size
        with Image.open(io.BytesIO(data)) as picture:
            sent_width, sent_height = picture.size
        crop = tuple(float(v) for v in origin["box_original_pixels"])
        x0, y0, x1, y1 = crop
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            raise ValueError("tool crop exceeds original pixels")
        sx, sy = sent_width / (x1 - x0), sent_height / (y1 - y0)
        relation = CoordinateRelation(referenced_space="view_pixels", relation="crop_and_affine",
            crop_original_pixels=crop,
            original_to_referenced_affine=(sx, 0.0, -x0 * sx, 0.0, sy, -y0 * sy))
        rows.append(RegisteredView(reference=RunQualifiedEvidenceRef(run_id=store.run_id,
            reference=ExistingEvidenceRef(scheme="view_id", value=origin["view_id"]),
            original_sha256=origin["original_sha256"], coordinate_relation=relation),
            original=store.put_bytes(original_bytes, block["mimeType"]),
            sent=store.put_bytes(data, block["mimeType"]), original_size=(width, height),
            sent_size=(sent_width, sent_height), image_name=original_path.name, tool_event_id=event_id))
    return rows


def hydrate_observation(text, package, views):
    answer = ObserverAnswer.model_validate_json(text)
    sent = {view.view_id: view for view in views}
    observations = []
    for row in answer.directly_seen:
        if row.view_id not in sent:
            raise ValueError("localized result cites an image not supplied in this package")
        view = sent[row.view_id]
        x0, y0, x1, y1 = row.box_original_pixels
        bounds = view.reference.coordinate_relation.crop_original_pixels or (
            0.0, 0.0, float(view.original_size[0]), float(view.original_size[1]))
        if not (bounds[0] <= x0 < x1 <= bounds[2] and bounds[1] <= y0 < y1 <= bounds[3]):
            raise ValueError("localized result lies outside its delivered crop")
        observations.append({"observation_id": row.observation_id, "statement": row.statement,
            "location": {"image_ref": view.reference.model_dump(mode="json"),
                "box_original_pixels": list(row.box_original_pixels)}})
    if not observations and not answer.uncertain:
        raise ValueError("an empty answer must state what remains uncertain")
    result = LocalizedEvidenceResult.model_validate_json(json.dumps({
        "package_id": package.package_id, "task_id": package.task_id,
        "based_on_source_model_version_id": package.source_model_version_id,
        "directly_seen": observations,
        "interpretations": [v.model_dump(mode="json") for v in answer.interpretations],
        "uncertain": [v.model_dump(mode="json") for v in answer.uncertain]}))
    assert_result_applicable(package, result, package.source_model_version_id)
    return result


class EvidenceTools:
    """Narrow the existing read-only dispatcher to packet-local measurements."""

    names = frozenset({"get_bim_reference", "pixel_profile", "map_dimension_chain",
                       "compare_facade_spans", "map_pixels"})

    def __init__(self, frozen, role, views):
        self.frozen, self.role = frozen, role
        self.run_directory = frozen.run_directory
        self.views = tuple(views)
        self.images = {v.image_name for v in self.views}

    async def list_tools(self):
        return [t for t in await self.frozen.list_tools() if t["name"] in self.names]

    def repeatability(self, name):
        return self.frozen.repeatability(name)

    async def call_tool(self, name, arguments):
        authorize_tool_call(self.role, name, "read" if self.repeatability(name) == "read_only" else "write")
        if name not in self.names:
            raise ValueError("tool is outside the local evidence package")
        for key in ("name", "image", "plan_image", "elevation_image"):
            if key in arguments and (not isinstance(arguments[key], str) or arguments[key] not in self.images):
                return self._rejected("tool image is outside the local evidence package: " + key)
        if name == "pixel_profile":
            box = arguments.get("box")
            if (not isinstance(box, list) or len(box) != 4
                    or any(type(v) not in (int, float) for v in box)):
                return self._rejected("box must contain four original-pixel coordinates")
            if not any(v.image_name == arguments.get("name") and self._contains(v, box)
                       for v in self.views):
                return self._rejected("measurement lies outside the supplied crop")
        if name == "compare_facade_spans":
            # This tool can bind previously saved profiles. Admit it only for
            # complete supplied images, not an unseen remainder of a crop.
            for key in ("plan_image", "elevation_image"):
                if not any(v.image_name == arguments.get(key) and self._contains(
                    v, (0, 0, *v.original_size)) for v in self.views):
                    return self._rejected("facade comparison requires the complete supplied image: " + key)
        return await self.frozen.call_tool(name, arguments)

    @staticmethod
    def _contains(view, box):
        bounds = view.reference.coordinate_relation.crop_original_pixels or (0, 0, *view.original_size)
        x0, y0, x1, y1 = box
        return bounds[0] <= x0 < x1 <= bounds[2] and bounds[1] <= y0 < y1 <= bounds[3]

    def _rejected(self, reason):
        # A policy decision is a known result, not an unknown transport outcome.
        value = {"status": "evidence_scope_rejected", "reason": reason,
                 "tool_image_names": sorted(self.images)}
        return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                "structuredContent": value, "isError": True}

    def snapshot_state(self):
        return self.frozen.snapshot_state()

    def artifacts(self):
        # The parent owns BIM artifacts. Do not repack the whole building for
        # every read-only child; its response and image blobs are already saved.
        return []

    def image_origins(self, raw):
        return self.frozen.image_origins(raw)


def update_observer_budget(engine, event, raw):
    remaining = {"model_calls": max(0, engine.limits.model_calls - engine.counts["model_calls"]),
        "tool_calls": max(0, engine.limits.tool_calls - engine.counts["tool_calls"]),
        "tokens": engine.task_budget.available.tokens,
        # Unlimited time is null on the wire; infinity is only an internal
        # Runtime comparison value and cannot enter the JSON evidence store.
        "seconds": max(0.0, engine._remaining()) if engine.limits.seconds is not None else None,
        "scope": "remaining child allowance; root budget may be lower"}
    previous = next((entry for entry in engine.context.state
                     if entry.key == "observer-remaining-budget"), None)
    engine.context.set_state(StateEntry(key="observer-remaining-budget", category="constraint",
        revision=previous.revision + 1 if previous else 1,
        value=remaining, epistemic_status="computed",
        source_refs=(engine.store.source("observer-remaining-budget", remaining),)))


async def run_observer(*, store, frozen_tools, adapter, model, parameters, limits: RunLimits,
                       package: EvidencePackage, views, notes, root: Path, route, resume=False,
                       root_tool_calls: int | None = None, low_output_limit_reason: str | None = None,
                       parent_deadline_epoch: float | None = None):
    timing_path = store.task_directory / "observer_timing.json"
    if timing_path.is_file():
        timing = json.loads(timing_path.read_bytes())
        if parent_deadline_epoch is not None and timing["deadline_epoch"] > parent_deadline_epoch:
            raise ValueError("observer deadline exceeds the parent deadline on resume")
    else:
        start = time.time()
        deadline = min(start + limits.seconds, parent_deadline_epoch) if parent_deadline_epoch is not None else start + limits.seconds
        if deadline <= start:
            return {"status": "root_time_budget_exhausted", "result": None, "runtime": None,
                "validation_error": None, "package": package.model_dump(mode="json"),
                "views": [v.as_json() for v in views], "model": model}
        timing = {"started_epoch": start, "deadline_epoch": deadline,
                  "time_budget_seconds": min(limits.seconds, deadline - start)}
        store.write_json("observer_timing.json", timing)
    limits = limits.model_copy(update={"seconds": timing["time_budget_seconds"]})
    async with AsyncExitStack() as stack:
        # Test doubles can implement only the tool protocol. The real BIM
        # dispatcher always supplies task_session and starts a scoped service.
        if hasattr(frozen_tools, "task_session"):
            images = {view.image_name: (store.get_bytes(view.original), view.original_size) for view in views}
            frozen_tools = await stack.enter_async_context(frozen_tools.task_session(
                directory=store.task_directory / "bim", root=root,
                role=local_observer_role(limits.ledger_limit()), timing=timing, images=images))
        return await _run_observer(store=store, frozen_tools=frozen_tools, adapter=adapter,
            model=model, parameters=parameters, limits=limits, package=package, views=views,
            notes=notes, root=root, route=route, resume=resume, root_tool_calls=root_tool_calls,
            low_output_limit_reason=low_output_limit_reason, start_epoch=timing["started_epoch"])


async def _run_observer(*, store, frozen_tools, adapter, model, parameters, limits,
                        package, views, notes, root, route, resume, root_tool_calls,
                        low_output_limit_reason, start_epoch):
    role = local_observer_role(limits.ledger_limit())
    role = role.model_copy(update={"tool_whitelist": tuple(
        grant for grant in role.tool_whitelist if grant.tool_name in EvidenceTools.names)})
    tools = EvidenceTools(frozen_tools, role, views)
    catalog = await tools.list_tools()
    specs = [{"type": "function", "function": {"name": t["name"],
        "description": t.get("description", ""), "parameters": t["inputSchema"]}} for t in catalog]
    versions = make_versions(store, root=root, prompt=OBSERVER_GUIDANCE, tools=specs,
        parameters=parameters, route=route, code_paths=("src/agent/runtime_delegation.py",
            "src/agent/runtime_coordinator.py", "src/agent/runtime_tools.py", "src/agent/contracts"))
    payload = {"evidence_package": package.model_dump(mode="json"),
        "task_limits": limits.model_dump(mode="json"),
        "tool_image_names": sorted(tools.images),
        "coordinator_notes": notes, "notes_status": "coordinator-supplied assertions, not independently verified",
        "views": [v.as_json() for v in views], "answer_schema": ObserverAnswer.model_json_schema()}
    content = [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]
    originals = {}
    for view in views:
        data = store.get_bytes(view.sent)
        store.get_bytes(view.original)
        originals[view.sent.sha256] = view.original
        content += [{"type": "text", "text": "Supplied view: " + view.view_id},
            {"type": "image_url", "image_url": {"url": "data:" + view.sent.media_type + ";base64," + base64.b64encode(data).decode()}}]
    engine = Runtime(store=store, adapter=adapter, tools=tools, role=role, model=model,
        parameters=parameters, versions=versions, limits=limits,
        strict_model_profile=route["route_id"] in {"paratera", "glm-subscription"},
        root_tool_calls=root_tool_calls,
        start_epoch=start_epoch,
        low_output_limit_reason=low_output_limit_reason,
        answer_validator=lambda text: hydrate_observation(text, package, views),
        max_answer_repairs=1,
        context_update=update_observer_budget,
        context_policy=ContextPolicy(active_window_messages=16, max_images=max(1, len(views)),
                                     max_image_bytes=32_000_000))
    receipt = await engine.run([{"role": "system", "content": OBSERVER_GUIDANCE},
        {"role": "user", "content": content}], image_originals=originals, resume=resume)
    result, error = None, None
    if receipt["status"] == "completed":
        try:
            reservations = [e.payload.reservation.reservation_id for e in store.events
                if e.payload.event_type == "budget" and e.payload.action == "reserve"]
            if not reservations or reservations[0] != package.budget_reservation_id:
                raise ValueError("evidence package does not match the first actual child reservation")
            result = hydrate_observation(receipt["answer"], package, views)
        except ValueError as exc:
            error = str(exc)
    return {"status": "invalid_observation" if error else receipt["status"],
        "result": result.model_dump(mode="json") if result else None,
        "validation_error": error, "runtime": receipt,
        "package": package.model_dump(mode="json"),
        "views": [v.as_json() for v in views], "model": model}
