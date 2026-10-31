"""A-6b: consume current tick batches, run B4 exact pairing, then four-way review.

The complete plan scope and facade availability are frozen before review. A
model may resolve identity, register uncertainty, infer dimensions for a missing
view, or return an image to step one. No distance matching or modular-size table
is used. Pure-model dimensional hypotheses remain explicitly unscoreable.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from src.agent.correction.config import load_core_tolerances
from src.agent.correction.facade_convention import world_axis
from src.agent.correction.opening_synthesis import (
    synthesize_current_openings,
)
from src.agent.correction.projection_bridge import CutLineV1
from src.agent.correction.tick_claim import (
    PLAN_V2_SCHEMA, TickClaimError, TickSession, digest, freeze, units, require_v2_plan,
)

FAMILIES = frozenset(("South", "North", "East", "West"))


@dataclass(frozen=True)
class PlanBinding:
    """Topology decision from correction; endpoints come ONLY from plan ticks."""
    opening_id: str  # source band:run identifier, never an unrelated alias
    family: str
    wall_id: str
    room_id: str
    line: CutLineV1
    floor_origin_u: int


@dataclass(frozen=True)
class FacadeInput:
    family: str
    session: TickSession | None
    expected_batch_id: str | None
    mirrored: bool | None = None
    local_x_positive: str | None = None


class PlanCandidateChoice(BaseModel):
    """Model accounting for every native face gap; no implicit deduplication.

    `bind` names a window to assemble; doors and undecided gaps can be registered
    without manufacturing a WindowV3. `same_opening` is explicit model identity,
    checked against the already selected wall, never inferred from overlap.
    """
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    candidate_id: str
    action: Literal["bind", "same_opening", "not_opening", "register"]
    target_id: str | None = None
    reason: str

    @model_validator(mode="after")
    def shape(self):
        if ((self.action == "same_opening") != (self.target_id is not None)
                or not self.reason.strip()):
            raise ValueError("PLAN_CANDIDATE_CHOICE_SHAPE")
        return self


class InferredDimensions(BaseModel):
    """Model dimensional hypothesis, not world coordinates or an evidence tier.

    This is solely category ③'s pure model output. Code retains plan position and
    adds the declared floor origin; no default/reference-size mechanism exists.
    """
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    height_mm: int
    sill_above_floor_mm: int

    @model_validator(mode="after")
    def positive_height(self):
        if self.height_mm <= 0 or self.sill_above_floor_mm < 0:
            raise ValueError("INFERENCE_DIMENSIONS_INVALID")
        return self


class OpeningChoice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    plan_opening_id: str
    action: Literal["pair", "register", "infer"]
    elevation_opening_id: str | None = None
    inferred_dimensions: InferredDimensions | None = None
    reason: str

    @model_validator(mode="after")
    def shape(self):
        if ((self.action == "pair") != (self.elevation_opening_id is not None) or
                (self.action == "infer") != (self.inferred_dimensions is not None) or
                not self.reason.strip()):
            raise ValueError("OPENING_CHOICE_SHAPE")
        return self


class SpatialResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    packet_id: str
    choices: tuple[OpeningChoice, ...]
    whole_building_review: Literal["accept", "register", "return_to_step_one"]
    reason: str
    reconsider_image_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SpatialPacket:
    packet_id: str
    record: bytes


@dataclass(frozen=True)
class OpeningOutcome:
    plan_opening_id: str
    family: str
    classification: str
    status: str
    wall_id: str
    room_id: str
    span_u: tuple[int, int] | None
    z_u: tuple[int, int] | None
    elevation_opening_id: str | None
    source: str
    score_eligible: bool
    reason: str


@dataclass(frozen=True)
class SpatialResult:
    result_id: str
    record: bytes



def _elevation_document(session: TickSession, expected: str) -> dict:
    # A-6-d1's symmetric consumer guard lives on this current-session method.
    return session.elevation_document(expected)


class OpeningReview:
    """A complete, current multi-image packet, retaining independent batch IDs.

    Plan bindings describe host/room/facade identity (model decisions), not
    numerical endpoints. B4's existing exact span and identity gates are reused.
    """
    def __init__(self, *, plan: TickSession, expected_plan_batch_id: str,
                 bindings: tuple[PlanBinding, ...], facades: tuple[FacadeInput, ...],
                 walls: tuple[CutLineV1, ...], candidate_choices: tuple[PlanCandidateChoice, ...] = (),
                 wall_compilation=None, geometry=None, floor_id: str | None = None):
        bindings, facades, walls = tuple(bindings), tuple(facades), tuple(walls)
        if (type(plan) is not TickSession or any(type(f) is not FacadeInput for f in facades)
                or any(type(b) is not PlanBinding for b in bindings)):
            raise TickClaimError("TICK_SESSION_REQUIRED")
        plan_doc = json.loads(plan.packet.source_bytes)
        self._native = plan_doc["schema"] == PLAN_V2_SCHEMA
        if not self._native and plan_doc["schema"] != "as_drawn_plan_v0":
            raise TickClaimError("PLAN_TICK_SOURCE_REQUIRED")
        self._plan, self._plan_batch = plan, expected_plan_batch_id
        self._facades, self._walls = facades, walls
        self._bindings = bindings
        self._result = None
        self._precision_u = units(Decimal(str(load_core_tolerances().output_precision_m)) * 1000)
        facts = {f.edge_id: f for f in plan.consume(expected_plan_batch_id)}
        expected_ids = {eid[:-3] for eid in facts if eid.endswith(":lo")}
        native_record = None
        self._geometry_bytes, self._floor_id = None, floor_id
        if self._native:
            expected_ids, native_record = self._native_scope(
                plan, bindings, walls, candidate_choices, wall_compilation, geometry, floor_id,
                expected_ids)
        elif candidate_choices or wall_compilation is not None or geometry is not None:
            raise TickClaimError("NATIVE_PLAN_SOURCE_REQUIRED")
        if len({b.opening_id for b in bindings}) != len(bindings) or {b.opening_id for b in bindings} != expected_ids:
            raise TickClaimError("PLAN_TOPOLOGY_COVERAGE_MISMATCH")
        if len(facades) != len(FAMILIES) or {f.family for f in facades} != FAMILIES:
            raise TickClaimError("FACADE_AVAILABILITY_MANIFEST_INCOMPLETE")
        wall_index = {w.origin_id: w for w in walls}
        if not self._native and len(wall_index) != len(walls):
            raise TickClaimError("WALL_ID_DUPLICATE")
        self._plans = {}
        for binding in bindings:
            oid = binding.opening_id
            if binding.family not in FAMILIES or type(binding.room_id) is not str or not binding.room_id or binding.wall_id not in wall_index:
                raise TickClaimError("PLAN_TOPOLOGY_INVALID", oid)
            wall = wall_index[binding.wall_id]
            if (type(binding.line) is not CutLineV1 or binding.line.origin_id != oid or binding.line.kind != "opening" or
                    binding.line.axis != facts[f"{oid}:lo"].axis or
                    binding.line.axis != world_axis(binding.family) or
                    type(binding.floor_origin_u) is not int or
                    binding.line.axis != wall.axis or binding.line.pos_m != wall.pos_m or
                    binding.line.half_thickness_m != wall.half_thickness_m):
                raise TickClaimError("PLAN_HOST_BINDING_MISMATCH", oid)
            if facts[f"{oid}:lo"].value_u >= facts[f"{oid}:hi"].value_u:
                raise TickClaimError("TICK_PLAN_INTERVAL_NOT_ORDERED", oid)
            self._plans[oid] = replace(binding.line,
                                      along_lo_m=facts[f"{oid}:lo"].value_u / 10000,
                                      along_hi_m=facts[f"{oid}:hi"].value_u / 10000)
            if self._native:
                self._require_visible(binding, (facts[f"{oid}:lo"].value_u, facts[f"{oid}:hi"].value_u))
        self._elevations, self._exact, self._availability = {}, {}, {}
        facade_records = []
        image_ids = {plan.packet.image_id}
        for facade in facades:
            if (facade.session is None) != (facade.expected_batch_id is None):
                raise TickClaimError("FACADE_BATCH_BINDING_INVALID")
            self._availability[facade.family] = facade.session is not None
            if facade.session is None:
                facade_records.append(dict(family=facade.family, availability="absent"))
                continue
            if type(facade.session) is not TickSession or facade.session.packet.image_id in image_ids:
                raise TickClaimError("IMAGE_MANIFEST_DUPLICATE")
            image_ids.add(facade.session.packet.image_id)
            doc = _elevation_document(facade.session, facade.expected_batch_id)
            if doc["facade_label"] != facade.family:
                raise TickClaimError("FACADE_SOURCE_FAMILY_MISMATCH")
            result = synthesize_current_openings(
                session=facade.session, expected_batch_id=facade.expected_batch_id, walls=walls,
                plan_openings=tuple(self._plans[b.opening_id] for b in bindings if b.family == facade.family),
                mirrored=facade.mirrored, local_x_positive=facade.local_x_positive,
                historical=not self._native)
            # B4 exact equality remains unchanged. Keep all openings, even unmatched.
            for opening in doc["openings"]:
                world = [result.along_origin_u + result.sign * units(Decimal(str(x)) * 1000)
                         for x in opening["x_range_m"]]
                z = tuple(units(Decimal(str(x)) * 1000) for x in opening["z_range_m"])
                self._elevations[(facade.family, opening["id"])] = (tuple(sorted(world)), z)
            for pair in result.pairings:
                self._exact[pair.plan_opening_id] = pair.elevation_opening_id
            facade_records.append(dict(family=facade.family, availability="present",
                                       image_id=facade.session.packet.image_id,
                                       batch_id=facade.expected_batch_id,
                                       source_sha=facade.session.packet.source_sha,
                                       b4=result.model_dump(),
                                       tick_facts=[asdict(f) for f in facade.session.consume(facade.expected_batch_id)]))
        payload = dict(schema="opening_review_v1", plan_batch=expected_plan_batch_id,
                       plan_image_id=plan.packet.image_id,
                       plan_facts=[asdict(f) for f in facts.values()],
                       output_precision_u=self._precision_u,
                       bindings=[asdict(replace(b, line=self._plans[b.opening_id])) for b in bindings], walls=[asdict(w) for w in walls],
                       facades=facade_records,
                       openings=[dict(family=k[0], opening_id=k[1], span_u=v[0], z_u=v[1])
                                 for k, v in sorted(self._elevations.items())], exact=self._exact)
        if self._native:
            payload["native_plan"] = native_record
        record = freeze(payload)
        self._packet = SpatialPacket(digest(record), record)

    def _native_scope(self, plan, bindings, walls, choices, compilation, geometry, floor_id, all_ids):
        from src.agent.correction.wall_compiler import WallCompilationV1, FixedDecisionV1, compile_wall_ir
        from src.agent.correction.projection_bridge import cut_lines_from_wall_compilation
        from src.agent.correction.schema import CorrectedGeometryV3
        from src.agent.correction.facade_visibility import materialize_floor_facade_segments, VisibilityTolerances

        doc = require_v2_plan(plan.packet.source_bytes, image_id=plan.packet.image_id)
        artifact = plan.evidence_artifact()
        if type(compilation) is not WallCompilationV1:
            raise TickClaimError("PLAN_WALL_COMPILATION_REQUIRED")
        rebuilt = compile_wall_ir(artifact, profile="strict", decisions=tuple(
            FixedDecisionV1(item_id=d.item_id, candidate_id=d.candidate_id)
            for d in compilation.applied_decisions))
        if rebuilt.model_dump() != compilation.model_dump() or rebuilt.open_items:
            raise TickClaimError("PLAN_WALL_COMPILATION_NOT_CURRENT")
        expected_walls, _ = cut_lines_from_wall_compilation(rebuilt.walls, ())
        if tuple(walls) != expected_walls:
            raise TickClaimError("PLAN_WALL_LINES_NOT_FROM_SOURCE")
        if type(geometry) is not CorrectedGeometryV3:
            raise TickClaimError("OPENING_GEOMETRY_REQUIRED")
        geom = CorrectedGeometryV3.model_validate(geometry.model_dump())
        floors = [f for f in geom.floors if f.id == floor_id]
        if len(floors) != 1:
            raise TickClaimError("OPENING_FLOOR_UNKNOWN")
        floor = floors[0]
        self._geometry_bytes = freeze(geom.model_dump(mode="json"))
        self._floor_origin_u = units(Decimal(str(floor.z_floor)) * 1000)
        self._floor_top_u = self._floor_origin_u + units(Decimal(str(floor.ceiling_height)) * 1000)
        tol = load_core_tolerances()
        self._visible_segments = materialize_floor_facade_segments(geom, floor, tolerances=VisibilityTolerances(
            depth_epsilon_m=tol.facade_visibility_depth_epsilon_m,
            endpoint_epsilon_m=tol.facade_visibility_endpoint_epsilon_m))
        choices = tuple(PlanCandidateChoice.model_validate(c.model_dump()) for c in choices)
        by_id = {c.candidate_id: c for c in choices}
        if len(by_id) != len(choices) or set(by_id) != all_ids:
            raise TickClaimError("PLAN_CANDIDATE_DECISION_COVERAGE_MISMATCH")
        primary = {c.candidate_id for c in choices if c.action == "bind"}
        owners = {r.observation_id: w.wall_id for w in rebuilt.walls for r in w.source_refs}
        openings = {o["id"]: o for o in doc["hypotheses"]["opening_candidates"]}
        # Wall ownership is derived from the source's selected pairs/four buckets
        # through the existing compiler, not from a second pairing algorithm.
        for binding in bindings:
            if binding.opening_id not in primary:
                raise TickClaimError("PLAN_TOPOLOGY_COVERAGE_MISMATCH")
            face = openings[binding.opening_id]["face_line"]
            if owners.get(face) != binding.wall_id:
                raise TickClaimError("PLAN_HOST_NOT_SELECTED_SOURCE_WALL", binding.opening_id)
            if (binding.room_id not in {c.id for c in floor.cells}
                    or binding.floor_origin_u != self._floor_origin_u):
                raise TickClaimError("PLAN_ROOM_FLOOR_BINDING_MISMATCH", binding.opening_id)
            # This output carrier is WindowV3. A declared door is registered,
            # not silently turned into a window by the assembly code.
            if (doc["hypotheses"].get("opening_types") or {}).get(binding.opening_id) != "window":
                raise TickClaimError("PLAN_WINDOW_CLASSIFICATION_REQUIRED", binding.opening_id)
        for choice in choices:
            if choice.action != "same_opening":
                continue
            face = openings[choice.candidate_id]["face_line"]
            target = openings.get(choice.target_id)
            if (choice.target_id not in primary or target is None or owners.get(face) is None
                    or owners.get(face) != owners.get(target["face_line"])):
                raise TickClaimError("PLAN_ALIAS_NOT_SAME_SELECTED_WALL", choice.candidate_id)
        return primary, dict(source_artifact=artifact.bundle.source_artifacts[0].model_dump(),
                             compilation_sha=rebuilt.content_sha256, floor_id=floor_id,
                             geometry_sha=digest(self._geometry_bytes),
                             candidate_choices=[c.model_dump() for c in choices],
                             source_hypotheses=doc["hypotheses"],
                             source_declarations=doc["declarations"])

    def _require_visible(self, binding, span_u):
        axis = world_axis(binding.family)
        lo, hi = (v / 10000 for v in span_u)
        across = 1 if axis == "x" else 0
        for segment in self._visible_segments:
            if (segment.facade_family == binding.family and segment.p1[across] == binding.line.pos_m
                    and any(v.lo <= lo < hi <= v.hi for v in segment.visible_intervals)):
                return
        raise TickClaimError("OPENING_HOST_NOT_VISIBLE", binding.opening_id)

    @property
    def packet(self) -> SpatialPacket:
        return self._packet

    def _check_current(self):
        self._plan.consume(self._plan_batch)
        for facade in self._facades:
            if facade.session is not None:
                facade.session.consume(facade.expected_batch_id)

    def submit(self, response: SpatialResponse) -> SpatialResult:
        self._check_current()
        if type(response) is not SpatialResponse or response.packet_id != self.packet.packet_id:
            raise TickClaimError("STALE_SPATIAL_RESPONSE")
        response = SpatialResponse.model_validate(response.model_dump(mode="python"))
        if self._result is not None:
            raise TickClaimError("SPATIAL_REVIEW_ALREADY_DECIDED")
        if not response.reason.strip():
            raise TickClaimError("WHOLE_BUILDING_REASON_REQUIRED")
        if response.whole_building_review == "return_to_step_one":
            sessions = {s.packet.image_id: s for s in [self._plan] + [f.session for f in self._facades if f.session]}
            ids = response.reconsider_image_ids
            if not ids or len(ids) != len(set(ids)) or not set(ids) <= set(sessions):
                raise TickClaimError("RECONSIDERATION_IMAGE_SCOPE_INVALID")
            exits = {}
            for image_id in ids:
                try:
                    sessions[image_id].reconsider(response.reason)
                except TickClaimError as exc:
                    exits[image_id] = exc.code
            raise TickClaimError("RETURN_TO_STEP_ONE_FROM_SPATIAL", {"images": ids, "exits": exits})
        if response.reconsider_image_ids:
            raise TickClaimError("RECONSIDERATION_ACTION_MISMATCH")
        choices = {c.plan_opening_id: c for c in response.choices}
        if len(choices) != len(response.choices) or set(choices) != set(self._plans):
            raise TickClaimError("SPATIAL_DECISION_COVERAGE_MISMATCH")
        outcomes, used = [], set()
        for binding in self._bindings:
            oid, family = binding.opening_id, binding.family
            choice = choices[oid]
            exists = self._availability[family]
            span = tuple(units(Decimal(str(v)) * 1000) for v in (
                self._plans[oid].along_lo_m, self._plans[oid].along_hi_m))
            classification, z, source, eligible, status = "②b", None, "pending", False, "registered_pending_user"
            if choice.action == "pair":
                key = (family, choice.elevation_opening_id)
                if not exists or key not in self._elevations:
                    raise TickClaimError("PAIR_ELEVATION_ID_UNKNOWN", key)
                if key in used:
                    raise TickClaimError("PAIR_IDENTITY_REUSED", key)
                used.add(key)
                elevation_span, z = self._elevations[key]
                classification = "①" if span == elevation_span else "②a"
                # Explicit model identity selection allows ambiguous B4 buckets to
                # resolve; it never changes B4's equality criterion itself.
                span, source, eligible, status = elevation_span, "elevation", True, "resolved"
            elif choice.action == "infer":
                if exists:
                    raise TickClaimError("INFERENCE_REQUIRES_ABSENT_FACADE")
                d = choice.inferred_dimensions
                def clean(value):
                    return int((Decimal(value) / self._precision_u).quantize(Decimal(1), rounding=ROUND_HALF_UP)) * self._precision_u
                sill = clean(binding.floor_origin_u + units(d.sill_above_floor_mm))
                z = (sill, clean(binding.floor_origin_u + units(d.sill_above_floor_mm) + units(d.height_mm)))
                if z[0] >= z[1]:
                    raise TickClaimError("REGISTER_INFERENCE_COLLAPSED_AT_OUTPUT_GRID")
                classification, source, status = "③", "inferred", "resolved_inferred"
            else:
                classification = "②b" if exists else "③"
                span = None  # registration creates no opening geometry
            if self._native and span is not None:
                self._require_visible(binding, span)
                if z is not None and not self._floor_origin_u <= z[0] < z[1] <= self._floor_top_u:
                    raise TickClaimError("OPENING_FLOOR_HEIGHT_MISMATCH", oid)
            if response.whole_building_review == "register":
                status, eligible = "registered_whole_building_review", False
            outcomes.append(OpeningOutcome(oid, family, classification, status, binding.wall_id,
                                           binding.room_id, span, z, choice.elevation_opening_id,
                                           source, eligible, choice.reason))
        record = freeze(dict(schema="opening_adjudication_v1", packet_id=self.packet.packet_id,
                             response=response.model_dump(), outcomes=[asdict(o) for o in outcomes],
                             unpaired_elevation=[list(k) for k in sorted(set(self._elevations) - used)]))
        self._result = SpatialResult(digest(record), record)
        return self._result

    def consume(self, expected_result_id: str) -> SpatialResult:
        self._check_current()
        if self._result is None or self._result.result_id != expected_result_id:
            raise TickClaimError("SPATIAL_RESULT_NOT_CURRENT")
        return self._result

    def scoreable_openings(self, expected_result_id: str) -> tuple[dict, ...]:
        """Current-batch export excludes guesses and pending decisions."""
        result = self.consume(expected_result_id)
        return tuple(r for r in json.loads(result.record)["outcomes"] if r["score_eligible"])

    def assemble_geometry(self, geometry, expected_result_id: str):
        """Only live review authority can add windows to the frozen base geometry."""
        from src.agent.correction.schema import CorrectedGeometryV3, WindowV3
        result = self.consume(expected_result_id)
        rows = self.scoreable_openings(expected_result_id)
        if not self._native or freeze(geometry.model_dump(mode="json")) != self._geometry_bytes:
            raise TickClaimError("OPENING_GEOMETRY_NOT_REVIEWED")
        geom = CorrectedGeometryV3.model_validate(geometry.model_dump())
        if any(w.floor_id == self._floor_id for w in geom.windows):
            raise TickClaimError("OPENING_GEOMETRY_ALREADY_POPULATED")
        for row in rows:
            geom.windows.append(WindowV3(
                id=f"{self._floor_id}:{row['plan_opening_id']}", floor_id=self._floor_id,
                facade=row["family"], room=row["room_id"],
                span=[v / 10000 for v in row["span_u"]], z=[v / 10000 for v in row["z_u"]]))
        geom.corrections.append(dict(kind="opening_adjudication", result_id=result.result_id,
                                     packet_id=self.packet.packet_id,
                                     outcomes=json.loads(result.record)["outcomes"],
                                     native_plan=json.loads(self.packet.record)["native_plan"]))
        return CorrectedGeometryV3.model_validate(geom.model_dump())

    def persist(self, directory: Path, expected_result_id: str):
        result = self.consume(expected_result_id)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self._plan.persist(directory / self._plan_batch, self._plan_batch)
        for facade in self._facades:
            if facade.session is not None:
                facade.session.persist(directory / facade.expected_batch_id, facade.expected_batch_id)
        for name, raw in (("spatial_packet.json", self.packet.record), ("spatial_result.json", result.record)):
            path = directory / name
            if path.exists() and path.read_bytes() != raw:
                raise TickClaimError("OPENING_ARCHIVE_IMMUTABLE", name)
            if not path.exists():
                with path.open("xb") as stream:
                    stream.write(raw)
