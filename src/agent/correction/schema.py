"""Materialized intermediate geometry for the staged correction pipeline.

correction stage (LLM correction) emits `CorrectedGeometry`; the deterministic core snaps
it; the modeling/MEP stages (LLM modeling) consumes it to produce `IntakeOutput`. Holding this
artifact explicit decouples the stages (swap a model per stage) and makes the
correction checkpoint verifiable and diffable for evaluation.

Geometry is rectangular cells in schema v1 (one cell = one room/zone footprint,
world meters). Schema v2 may use orthogonal polygon cells; `x`/`y` remain
required and are the polygon bbox projection.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal, get_args, get_origin

from pydantic import AllowInfNan, BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_serializer, model_validator

from src.agent.correction.claims import WINDOW_CLAIMS

# F-15 (2026-08-07): marker for fields that are the deterministic core's OWN
# audit trail — computed downstream from a correction draw, never legal input
# TO one. A field carrying this marker in its ``json_schema_extra`` is
# mechanically stripped from the JSON Schema shown to the correction LLM
# prompt (``vocab.producer_facing_json_schema``); validation against the full
# ``CorrectedGeometryV3`` (this module) is completely unaffected — the
# stripping only touches what the model is TOLD it may write, not what the
# core accepts/rejects. ``_producer_preflight``
# (correction/window_sources.py) remains the authoritative, unweakened door:
# a draw that fills a marked field anyway is still hard-rejected.
#
# A field marked this way NEVER gets a value from anywhere but a later,
# non-draw stage (e.g. Vg materializes `facade_segments` at finalize; the
# separate e4_orientation phase resolves `north_axis`) — it stays legitimately
# absent/None all through a draw and its preflight. Contrast with
# CORRECTION_DRAW_DERIVED below, whose fields the SCHEMA ITSELF fills in
# unconditionally the moment validation succeeds.
CORRECTION_DRAW_FORBIDDEN = "correction_draw_forbidden"

# F-16 follow-up (2026-08-08, orchestrator interface sweep §6 摊一 Step 2):
# marker for fields that are ALSO illegal input to a draw, but — unlike
# CORRECTION_DRAW_FORBIDDEN fields — the schema's own validator derives and
# fills them in from other fields of the SAME draw as soon as construction
# succeeds (WindowV3.floor <- by_id[floor_id].name), so by the time any code
# holds a validated instance the field is ALWAYS populated. That makes
# "is it populated?" meaningless as a post-construction preflight check (it
# would fire on every valid instance, derived or not) — the only point where
# "did the MODEL supply a value" is still observable is the raw draw payload,
# before ``model_validate`` ever runs. Kept as a distinct marker (rather than
# reusing CORRECTION_DRAW_FORBIDDEN for both) so that distinction is encoded
# in the schema itself and each consumer can tell, mechanically, which kind
# of field it is holding — not something a caller has to remember per field
# name. Still gets the SAME prompt-schema stripping treatment as
# CORRECTION_DRAW_FORBIDDEN (see ``vocab.producer_facing_json_schema``): the
# model must not see either kind as a fillable field.
CORRECTION_DRAW_DERIVED = "correction_draw_derived"


def _marked_field_names(model_cls: type[BaseModel], marker: str) -> tuple[str, ...]:
    names = []
    for name, field in model_cls.model_fields.items():
        extra = field.json_schema_extra
        if isinstance(extra, dict) and extra.get(marker) is True:
            names.append(name)
    return tuple(sorted(names))


def draw_forbidden_field_names(model_cls: type[BaseModel]) -> tuple[str, ...]:
    """Names of ``model_cls``'s OWN top-level fields marked
    ``CORRECTION_DRAW_FORBIDDEN`` (F-15 follow-up, 2026-08-07 orchestrator
    A3/B1).

    This is the SINGLE source both consumers must agree with — previously
    they each hardcoded their own name list and drifted: the prompt-schema
    stripper (``vocab.producer_facing_json_schema``) already read the marker
    correctly, but ``parse.parse_correction_draw``'s b2-phase draw-contract
    gate independently hardcoded ``facade_segments`` + ``north_axis`` by
    name — so when ``north_axis`` was later found to need the SAME
    protection as ``facade_segments`` (a real run filled it and was
    rejected), only one of the two lists needed updating, and it would have
    been easy to update the wrong one, or only the gate, and quietly
    re-widen the gap. Deriving both from this single function makes that
    class of drift structurally impossible: mark a field once, both
    consumers see it.

    Only inspects ``model_cls.model_fields`` directly (top-level fields of
    the model actually passed in — e.g. ``CorrectedGeometryV3``), NOT nested
    models reached through them (e.g. ``WindowV3.facade_segment_id``, a
    per-window field). Nested list-of-submodel fields (``windows``) are
    covered by the sibling function :func:`nested_draw_forbidden_fields`
    below — kept separate rather than folded into a single recursive walk
    because the two call shapes differ (a container's own fields vs. each
    item of a list field), and unifying them into one generic recursive
    function would either need to guess how deep to descend or risk
    picking up marked fields on unrelated nested models reached through
    other paths (e.g. a future marked field on ``FacadeSegment``, itself
    already gated at the container level via ``facade_segments``).

    Deliberately does NOT also match ``CORRECTION_DRAW_DERIVED`` — the two
    markers' consumers have opposite post-construction semantics (see that
    marker's docstring), and this function's only caller
    (``_producer_preflight``'s top-level check, plus the b2 gate) needs the
    "stays legitimately absent" set specifically.
    """
    return _marked_field_names(model_cls, CORRECTION_DRAW_FORBIDDEN)


def _nested_marked_fields(container_cls: type[BaseModel], marker: str) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for name, field in container_cls.model_fields.items():
        if get_origin(field.annotation) is not list:
            continue
        args = get_args(field.annotation)
        if len(args) != 1:
            continue
        item_type = args[0]
        if not (isinstance(item_type, type) and issubclass(item_type, BaseModel)):
            continue
        marked = _marked_field_names(item_type, marker)
        if marked:
            result[name] = marked
    return result


def nested_draw_forbidden_fields(container_cls: type[BaseModel]) -> dict[str, tuple[str, ...]]:
    """Map ``container_cls``'s own ``list[<submodel>]`` fields to the
    ``CORRECTION_DRAW_FORBIDDEN``-marked field names declared on each
    submodel (F-16 follow-up, 2026-08-08 orchestrator interface sweep §6).

    Companion to :func:`draw_forbidden_field_names`, one level down: where
    that function answers "which of THIS model's own fields are forbidden",
    this one answers "for each of this model's list-of-submodel fields,
    which of THAT submodel's fields are forbidden". Both read the SAME
    marker (``CORRECTION_DRAW_FORBIDDEN`` in ``json_schema_extra``), so a
    field marked once is picked up by whichever function matches its
    nesting depth — no second, hand-maintained name list at either level.

    This closes a drift that F-15's original fix (marker + top-level
    stripping/gate only) left open: ``WindowV3.facade_segment_id`` carries
    the marker (so the prompt-schema stripper already excludes it, since
    that stripper recurses through ``$defs`` and finds it regardless of
    nesting depth — see ``vocab.producer_facing_json_schema``), but the
    runtime draw-contract gates (``parse.py``'s b2 check,
    ``window_sources.py._producer_preflight``) each independently
    hardcoded the literal field name ``"facade_segment_id"`` instead of
    reading the marker — precisely the two-list drift pattern F-15 was
    meant to eliminate, just one level deeper than F-15 first looked.

    Only descends exactly one level (``list[BaseModel]`` fields whose item
    type itself declares at least one marked field); a submodel with no
    marked fields is simply absent from the returned mapping. Deliberately
    does not recurse further (e.g. into a list field's own nested list
    fields) — no current schema needs that depth, and guessing at it would
    risk snagging on unrelated future nesting.

    Intentionally excludes ``CORRECTION_DRAW_DERIVED``-marked fields (e.g.
    ``WindowV3.floor``) — see :func:`nested_draw_derived_fields` and that
    marker's docstring for why a post-construction "is it populated?" check
    (which is how ``_producer_preflight`` uses this function's result) would
    be meaningless, not merely redundant, for a field the schema itself
    always fills in.
    """
    return _nested_marked_fields(container_cls, CORRECTION_DRAW_FORBIDDEN)


def nested_draw_derived_fields(container_cls: type[BaseModel]) -> dict[str, tuple[str, ...]]:
    """Map ``container_cls``'s own ``list[<submodel>]`` fields to the
    ``CORRECTION_DRAW_DERIVED``-marked field names declared on each submodel
    (F-16, 2026-08-08 orchestrator interface sweep §6 摊一 Step 2).

    Same shape as :func:`nested_draw_forbidden_fields`, different marker.
    The split matters at the call site: this set is only safe to check
    against the RAW draw payload dict, before ``model_validate`` runs (see
    ``parse.parse_correction_draw``'s b2 gate) — a validated instance's
    fields of this kind are unconditionally populated by the model's own
    ``model_validator``, so "is it None?" can never distinguish a model-drawn
    value from a derived one after the fact.
    """
    return _nested_marked_fields(container_cls, CORRECTION_DRAW_DERIVED)


from src.agent.correction.constants import SCHEMA_VERSION_V1

# Case-insensitive aliases the facade validator accepts; anything else (e.g.
# "Northeast", a typo) is rejected so the geometry stages never silently treat
# an unknown facade as North/South.
_FACADE_ALIASES = {
    "north": "North", "n": "North",
    "south": "South", "s": "South",
    "east": "East", "e": "East",
    "west": "West", "w": "West",
}


class Cell(BaseModel):
    """One source room footprint (world meters), never a display/thermal slice.

    ``id`` is the persistent source-space identity. Public Z handles are derived
    and can change when geometry ordering changes. Preserve this id on edits.
    """

    model_config = ConfigDict(extra="allow")
    id: str
    role: str = "office"
    x: list[float]  # [min, max]
    y: list[float]  # [min, max]
    polygon: list[list[float]] | None = None  # exterior ring, CCW, not closed


class RoomRoleEvidence(BaseModel):
    """Current function assignment basis, separate from room geometry evidence."""

    model_config = ConfigDict(extra="forbid")
    role: str
    basis: Literal["observed", "inferred", "unknown"]
    source_refs: list[str] = Field(min_length=1)
    assumptions: list[str]

    @model_validator(mode="after")
    def validate_assignment(self):
        from src.agent.roles import require_role
        self.role = require_role(self.role)
        if any(not ref.strip() for ref in self.source_refs):
            raise ValueError("room role evidence requires nonblank source_refs")
        if (self.role == "unknown") != (self.basis == "unknown"):
            raise ValueError("unknown role and unknown evidence basis must be used together")
        if self.basis == "inferred" and not any(note.strip() for note in self.assumptions):
            raise ValueError("inferred room role requires an explicit assumption")
        return self


class SourceVerticalSection(BaseModel):
    """One horizontal slab of a single source space, using absolute world Z."""

    model_config = ConfigDict(extra="forbid")
    z: tuple[Annotated[float, AllowInfNan(False)], Annotated[float, AllowInfNan(False)]]
    polygon: list[list[Annotated[float, AllowInfNan(False)]]] = Field(min_length=3)
    holes: list[list[list[Annotated[float, AllowInfNan(False)]]]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_section(self):
        from src.agent.geometry.source_volume import validate_section_polygon
        if self.z[1] <= self.z[0]:
            raise ValueError("vertical section height must be positive")
        validate_section_polygon(self)
        return self


class SourceSpace(BaseModel):
    """Materialized source identity; independent of downstream zone numbering."""

    model_config = ConfigDict(extra="forbid")
    id: str
    floor_id: str
    polygon: list[list[float]]
    holes: list[list[list[float]]] = Field(default_factory=list, exclude_if=lambda value: not value)
    vertical_sections: list[SourceVerticalSection] = Field(default_factory=list, exclude_if=lambda value: not value)
    z_floor: float
    height: float
    role: str
    source_refs: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    role_evidence: RoomRoleEvidence | None = None

    @model_validator(mode="after")
    def role_matches_evidence(self):
        if self.vertical_sections:
            from src.agent.geometry.source_volume import validate_vertical_sections
            rows = validate_vertical_sections(self.vertical_sections, self.polygon, self.holes, self.z_floor, self.height)
            self.vertical_sections = [SourceVerticalSection.model_validate(row) for row in rows]
        if self.role_evidence is not None:
            from src.agent.roles import require_role
            if require_role(self.role) != self.role_evidence.role:
                raise ValueError("source room role does not match its saved role evidence")
        return self


class SourceBoundary(BaseModel):
    """A source room's oriented boundary, possibly split into several faces.

    Adjacent spaces and counterpart boundaries are derived relations, not a
    claim that adjacency is traversable. A long wall can border several rooms.
    """

    model_config = ConfigDict(extra="forbid")
    id: str
    space_id: str
    kind: Literal["physical", "virtual"]
    geometry_type: Literal["wall", "floor", "ceiling"]
    vertices: list[list[float]]
    holes: list[list[list[float]]] = Field(default_factory=list, exclude_if=lambda value: not value)
    adjacent_space_ids: list[str] = Field(default_factory=list)
    counterpart_ids: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)


class SourceOpening(BaseModel):
    """An identified opening and its source host, without door-leaf solids."""

    model_config = ConfigDict(extra="forbid")
    id: str
    kind: Literal["window", "door", "open"]
    host_boundary_id: str
    space_ids: list[str]
    exterior: bool
    vertices: list[list[float]]
    connectivity: Literal["unknown", "open", "closed"] = "unknown"
    source_refs: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class Window(BaseModel):
    """One window, positioned on a facade in world coordinates."""

    model_config = ConfigDict(extra="allow")
    id: str
    floor: str
    facade: Literal["North", "South", "East", "West"]
    span: list[float]  # along-facade world range [min, max] (x for N/S, y for E/W)
    z: list[float]  # [sill, head] world
    room: str | None = None  # cell id this window belongs to

    @field_validator("facade", mode="before")
    @classmethod
    def _normalize_facade(cls, v):
        if isinstance(v, str):
            return _FACADE_ALIASES.get(v.strip().lower(), v)
        return v


class Floor(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    z_floor: float
    ceiling_height: float
    cells: list[Cell]
    # Storey membership for existing continuous source spaces; no copies or slabs.
    spanning_space_ids: list[str] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _serialize_floor(self, handler):
        # Keep legacy proposal bytes stable, including on supported Pydantic 2.11.
        result = handler(self)
        if not self.spanning_space_ids:
            result.pop("spanning_space_ids", None)
        return result


class WallOpening(BaseModel):
    """An explicit door or empty aperture on a source room's wall (metres).

    ``other_space_id=None`` means outdoors, never an unspecified neighbour.
    The plan endpoints and world z interval define a rectangle, with no door-leaf
    solids. Door operating state may be unknown; an unfilled passage is open.
    """

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    kind: Literal["door", "open"]
    space_id: str
    other_space_id: str | None
    p1: tuple[Annotated[float, AllowInfNan(False)], Annotated[float, AllowInfNan(False)]]
    p2: tuple[Annotated[float, AllowInfNan(False)], Annotated[float, AllowInfNan(False)]]
    z: tuple[Annotated[float, AllowInfNan(False)], Annotated[float, AllowInfNan(False)]]
    state: Literal["unknown", "open", "closed"] | None = None
    source_refs: list[str] = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _opening_contract(self):
        if self.space_id == self.other_space_id:
            raise ValueError("wall opening must connect distinct spaces or outdoors")
        if self.p1 == self.p2 or not self.z[0] < self.z[1]:
            raise ValueError("wall opening requires positive width and height")
        if self.p1[0] != self.p2[0] and self.p1[1] != self.p2[1]:
            raise ValueError("only orthogonal wall openings are currently supported")
        if self.state is None:
            self.state = "open" if self.kind == "open" else "unknown"
        if self.kind == "open" and self.state != "open":
            raise ValueError("an unfilled opening cannot be declared closed or unknown")
        if not all(ref.strip() for ref in self.source_refs):
            raise ValueError("opening source references must not be blank")
        return self


class VerticalSeparation(BaseModel):
    """Explicit physical separation between selected adjacent source spaces."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    space_ids: list[str] = Field(min_length=2)
    reason: str = Field(min_length=1)
    source_refs: list[str] = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _separation_contract(self):
        if (not self.id.strip() or not self.reason.strip()
                or any(not value.strip() for value in self.space_ids + self.source_refs)):
            raise ValueError("vertical separation IDs, reason and source_refs must be nonblank")
        if len(set(self.space_ids)) != len(self.space_ids):
            raise ValueError("vertical separation space_ids must be unique")
        return self


class CorrectedGeometry(BaseModel):
    """Corrected, world-frame, centerline geometry primitives — the correction-stage output."""

    model_config = ConfigDict(extra="allow")
    schema_version: str = SCHEMA_VERSION_V1
    footprint_x: list[float]  # [min, max]
    footprint_y: list[float]  # [min, max]
    floors: list[Floor]
    windows: list[Window] = Field(default_factory=list)
    # Excluding an empty list preserves the established v1/v2/v3 byte contract.
    openings: list[WallOpening] = Field(default_factory=list, exclude_if=lambda value: not value)
    vertical_separations: list[VerticalSeparation] = Field(default_factory=list, exclude_if=lambda value: not value)
    # Audit (A0 schema, kept as flexible dicts so a stage need not over-specify).
    corrections: list[dict] = Field(default_factory=list)
    conflicts: list[dict] = Field(default_factory=list)
    unsupported: list[dict] = Field(default_factory=list)
    notes: str | None = None


# V3 is deliberately a strict subclass family.  The legacy classes above must
# remain wire-identical: historical V1/V2 artifacts retain their permissive
# extra fields and their existing serializer bytes.
FiniteFloat = Annotated[float, AllowInfNan(False)]
Hex64 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Point2 = tuple[FiniteFloat, FiniteFloat]


class FootprintRing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vertices: list[Point2] = Field(min_length=4)


class WorldInterval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lo: FiniteFloat
    hi: FiniteFloat

    @model_validator(mode="after")
    def _ordered(self):
        if self.lo >= self.hi:
            raise ValueError("world interval requires lo < hi")
        return self


class KnowledgeRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str
    dataset_version: str
    entry_id: str
    candidate_id: str
    content_sha256: Hex64


class FieldProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provenance: Literal["observed", "derived", "assumed"]
    source_ids: list[str] = Field(default_factory=list)
    method: str | None = None
    knowledge_ref: KnowledgeRef | None = None


class FacadeSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    floor_id: str
    facade_family: Literal["North", "South", "East", "West"]
    p1: Point2
    p2: Point2
    outward_normal: tuple[Literal[-1, 0, 1], Literal[-1, 0, 1]]
    world_along_interval: WorldInterval
    depth: Annotated[FiniteFloat, Field(ge=0)]
    visible_intervals: list[WorldInterval] = Field(default_factory=list)
    source_footprint_fingerprint: Hex64

    @model_validator(mode="after")
    def _valid_segment(self):
        (x1, y1), (x2, y2) = self.p1, self.p2
        nx, ny = self.outward_normal
        if (x1, y1) == (x2, y2) or (x1 != x2 and y1 != y2):
            raise ValueError("facade segment must be non-zero and axis-aligned")
        if abs(nx) + abs(ny) != 1 or (x1 != x2 and nx != 0) or (y1 != y2 and ny != 0):
            raise ValueError("facade segment normal must be unit and perpendicular")
        expected = {"North": (0, 1), "South": (0, -1), "East": (1, 0), "West": (-1, 0)}
        if (nx, ny) != expected[self.facade_family]:
            raise ValueError("facade segment normal disagrees with facade_family")
        span = sorted((x1, x2) if y1 == y2 else (y1, y2))
        if (self.world_along_interval.lo, self.world_along_interval.hi) != tuple(span):
            raise ValueError("world_along_interval must equal segment projection")
        prev = None
        for interval in self.visible_intervals:
            if interval.lo < self.world_along_interval.lo or interval.hi > self.world_along_interval.hi:
                raise ValueError("visible interval lies outside segment")
            if prev is not None and interval.lo < prev:
                raise ValueError("visible intervals must be sorted and disjoint")
            prev = interval.hi
        return self


class NorthAxisEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value_deg: FiniteFloat
    provenance: Literal["observed", "derived", "assumed"]
    source_ids: list[str] = Field(default_factory=list)
    uncertainty_deg: Annotated[FiniteFloat, Field(ge=0)] | None = None
    method: str | None = None
    frame_transform_hash: Hex64 | None = None

    @model_validator(mode="after")
    def _axis(self):
        object.__setattr__(self, "value_deg", self.value_deg % 360)
        if self.provenance == "observed" and not self.source_ids:
            raise ValueError("observed north axis requires source_ids")
        return self


class CellV3(Cell):
    model_config = ConfigDict(extra="forbid")


class FloorV3(Floor):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(frozen=True)
    footprint: FootprintRing
    cells: list[CellV3] = Field(min_length=1)


class WindowV3(Window):
    model_config = ConfigDict(extra="forbid")
    floor_id: str
    # F-16 (2026-08-08, orchestrator interface sweep §6 摊一 Step 2): base
    # `Window.floor` (str, required) is a v1/v2 leftover — v1 has no
    # `floor_id` at all, so `floor` (a name string) is its ONLY floor
    # reference and must stay exactly as-is there (untouched: this override
    # lives on WindowV3 only, base `Window`/`CorrectedGeometry` are
    # unaffected). On v3, `floor_id` is the authoritative reference and
    # `floor` becomes a pure, optional, DERIVED display echo of
    # `by_id[floor_id].name` (see `CorrectedGeometryV3._v3_integrity` below)
    # — the model no longer supplies it at all, closing off the "two
    # independent declarations of the same fact" shape that produced F-16
    # (`floors[0].id="F1"` / `name="Level 1"`, window's `floor="F1"` written
    # against the id instead of the name the old required-field contract
    # wanted). Marked CORRECTION_DRAW_DERIVED, not CORRECTION_DRAW_FORBIDDEN
    # — see that marker's docstring for why the two need to stay distinct.
    floor: str | None = Field(
        default=None, json_schema_extra={CORRECTION_DRAW_DERIVED: True}
    )
    facade_segment_id: str | None = Field(
        default=None, json_schema_extra={CORRECTION_DRAW_FORBIDDEN: True}
    )
    provenance: dict[str, FieldProvenance] | None = None

    @field_validator("provenance")
    @classmethod
    def _claims_vocab(cls, value):
        if value is not None and not set(value).issubset(WINDOW_CLAIMS):
            raise ValueError("window provenance keys must be opening-claim vocabulary")
        return value


class DebtResolutionAuditEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["debt_resolution"]
    resolves_debt_id: Hex64
    rationale: str
    source: Literal["llm_correction", "a3"]


class DeterministicCoreStampV1(BaseModel):
    """F-22 BLOCKER-1 (2026-08-12, unconditional core stamp): the
    deterministic core's own "I ran, version is X" mark.

    `deterministic.apply_deterministic_core` writes this UNCONDITIONALLY on
    every schema-v3 completion — regardless of whether any correction,
    envelope reconcile, or window-host resolution actually changed anything.
    This is deliberately a SEPARATE, always-present field, not a conditional
    `corrections[]` audit entry: `deterministic_core.envelope_atomic_transform`
    (envelope_transform.py) only appends a record when there is something to
    move (`if not intents: return ... # no record left at all`), so a legal
    v3 product whose drawing already agreed with the facade evidence (no
    intents) is byte-for-byte indistinguishable, in `corrections[]`, from one
    the core never touched. A missing conditional record can mean either "the
    core never ran" (untrustworthy) or "the core ran and had nothing to do"
    (fully trustworthy) — those two facts get compressed into the same
    blank. Only an unconditional field that is always written on completion,
    independent of what happened inside, can tell them apart.

    `version` is compared against `deterministic.DETERMINISTIC_CORE_STAMP_VERSION`
    by `judge.correction_score._is_trusted_output_convention` — the ONLY
    reader that treats it as a trust signal — with EXACT equality (same
    "declared identity, no fallback interpretation" discipline as
    `CORRECTION_OUTPUT_CONVENTION` in that module: an unrecognized version is
    untrusted, not coerced or guessed at). Deliberately a strict, separately
    named model (not a bare `dict`) even though the rest of this schema's
    audit trail uses flexible dicts (`corrections`/`conflicts`/`unsupported`):
    those lists hold heterogeneous rule-specific rows a stage need not
    over-specify; this field's whole job is a single, load-bearing identity
    check, so it gets the same strict-typing treatment as this module's other
    v3-only identity fields (`FacadeSegment`, `NorthAxisEvidence`).

    Marked `CORRECTION_DRAW_FORBIDDEN` — like `facade_segments`/`north_axis`,
    a correction draw that pre-fills this field is the model claiming a
    provenance fact only the deterministic core is allowed to assert, and is
    rejected at the raw-payload preflight (`parse.py`'s b2 gate,
    `window_sources.py`'s `_producer_preflight`) via the SAME
    `draw_forbidden_field_names` marker scan those already use — no separate
    edit needed there. Belt-and-suspenders only: `apply_deterministic_core`
    unconditionally OVERWRITES this field (plain assignment, never "if
    unset") on every real run, so even if a forged value slipped past the
    preflight it could never survive a genuine core pass.
    """

    model_config = ConfigDict(extra="forbid")
    version: str


class CorrectedGeometryV3(CorrectedGeometry):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["3"]
    floors: list[FloorV3] = Field(min_length=1)
    windows: list[WindowV3] = Field(default_factory=list)
    facade_segments: list[FacadeSegment] = Field(
        default_factory=list, json_schema_extra={CORRECTION_DRAW_FORBIDDEN: True}
    )
    # F-22 BLOCKER-1 (2026-08-12): see `DeterministicCoreStampV1` above for
    # why this exists and why it must stay a separate, unconditional field
    # rather than folding into `corrections[]`. Base `CorrectedGeometry`
    # (legacy v1/v2) deliberately does NOT get this field — this module's own
    # "V3 is deliberately a strict subclass family; the legacy classes above
    # must remain wire-identical" contract (top of file) forbids adding
    # anything to the shared base that would change a v1/v2 artifact's
    # existing serializer bytes. Legacy is never a trusted output-convention
    # candidate regardless (`_is_trusted_output_convention` gates on
    # `schema_version == "3"` first), so scoping the stamp to v3-only costs
    # nothing and keeps every legacy fixture byte-identical.
    deterministic_core_stamp: DeterministicCoreStampV1 | None = Field(
        default=None, json_schema_extra={CORRECTION_DRAW_FORBIDDEN: True}
    )
    # F-15 follow-up (2026-08-07, orchestrator A3): `north_axis` is exactly
    # the same shape of core-only field as `facade_segments` — under the b2
    # (draw) phase_contract it must ALWAYS stay unpopulated (see
    # feature_state.derive_feature_state_claims: a b2-phase v3 draw is
    # unconditionally `typed_north_axis="declared_unpopulated"`; only the
    # SEPARATE, non-LLM `e4_orientation` enrichment phase — a different call
    # site entirely, orientation.py, never a draw prompt — is allowed to
    # populate it). It was NOT marked in the original F-15 fix (facade_segments
    # / facade_segment_id only), so the b2 draw contract gate in parse.py kept
    # a second, independently-hardcoded name list — exactly the drift this
    # marker is meant to prevent. Now marked, so both the prompt-stripper
    # (vocab.producer_facing_json_schema) AND the runtime gate
    # (parse.parse_correction_draw's b2 check, via
    # schema.draw_forbidden_field_names) read the SAME single source.
    north_axis: NorthAxisEvidence | None = Field(
        default=None, json_schema_extra={CORRECTION_DRAW_FORBIDDEN: True}
    )

    @model_validator(mode="after")
    def _v3_integrity(self):
        ids = [fl.id for fl in self.floors]
        if any(not value for value in ids) or len(ids) != len(set(ids)):
            raise ValueError("v3 floor ids must be non-empty and globally unique")
        by_id = {fl.id: fl for fl in self.floors}
        for win in self.windows:
            floor = by_id.get(win.floor_id)
            if floor is None:
                raise ValueError(f"window {win.id}: unknown floor_id {win.floor_id}")
            # F-16 (2026-08-08, §6 摊一 Step 2): `floor` is derived from
            # `floor_id`, not model input (see WindowV3.floor above) — the
            # PRIMARY door rejecting a draw that supplied it is
            # parse.parse_correction_draw's raw-payload b2 gate (typed
            # WindowResolverInputError, category=model_draw_error, so a
            # rejection carries retry guidance — see that gate's comment for
            # why this pydantic-level check must NOT be the primary one: by
            # the time a WindowV3 instance exists, any exception raised here
            # is a plain ValueError wrapped into pydantic's ValidationError,
            # which is exactly the F-15-shaped mistake this project already
            # paid for once — bare ValidationError never reaches the
            # model_draw_error retry-guidance channel). This branch is the
            # schema's OWN, unconditional invariant enforcement — it runs
            # regardless of which door (if any) a caller went through, so a
            # value that already agrees with the derivation (e.g. a v1->v3
            # round-trip that copied it forward correctly) is accepted
            # as-is; only a genuine mismatch is rejected here, as a
            # defense-in-depth backstop for callers that construct
            # `CorrectedGeometryV3` directly instead of going through
            # `parse_correction_draw`.
            if win.floor is None:
                win.floor = floor.name
            elif win.floor != floor.name:
                raise ValueError(f"window {win.id}: floor must match referenced floor name")
        segment_ids = {seg.id for seg in self.facade_segments}
        if len(segment_ids) != len(self.facade_segments):
            raise ValueError("facade segment ids must be globally unique")
        for seg in self.facade_segments:
            floor = by_id.get(seg.floor_id)
            if floor is None:
                raise ValueError(f"facade segment {seg.id}: unknown floor_id")
            from src.agent.correction.footprint import floor_footprint_fingerprint
            if seg.source_footprint_fingerprint != floor_footprint_fingerprint(self, floor):
                raise ValueError(
                    f"facade segment {seg.id}: source_footprint_fingerprint does not match floor footprint"
                )
        for win in self.windows:
            if win.facade_segment_id is not None and win.facade_segment_id not in segment_ids:
                raise ValueError(f"window {win.id}: unknown facade_segment_id")
        # B2 permits different encodings during draw (core canonicalizes them),
        # but every floor must describe the same geometric footprint domain.
        def fingerprint(floor):
            pts = [(float(x), float(y)) for x, y in floor.footprint.vertices]
            if pts[0] == pts[-1]:
                pts.pop()
            forward = min(tuple(pts[i:] + pts[:i]) for i in range(len(pts)))
            rev = list(reversed(pts))
            backward = min(tuple(rev[i:] + rev[:i]) for i in range(len(rev)))
            return min(forward, backward)
        if len({fingerprint(floor) for floor in self.floors}) != 1:
            raise ValueError("v3 per-floor footprints must have identical geometry")
        for row in [*self.corrections, *self.conflicts, *self.unsupported]:
            if isinstance(row, dict) and row.get("kind") == "debt_resolution":
                DebtResolutionAuditEntry.model_validate(row)
            if isinstance(row, dict) and row.get("kind") == "window_host_resolution":
                # Local import avoids a schema -> source-wire import cycle while
                # registering the B5 strict accepted-audit shape for v3 only.
                from src.agent.correction.window_host import WindowHostResolutionAuditV1
                WindowHostResolutionAuditV1.model_validate_json(
                    json.dumps(row, separators=(",", ":"), ensure_ascii=False),
                )
        return self


# ---------------------------------------------------------------------------
# F-9 route② S0 (2026-08-11, design v2.1 §4.2/§10): raw, model-facing v3
# window-citation draw. `CorrectionDrawV3CitationV2` is an INDEPENDENT
# strict model, not a field-deleted view of `CorrectedGeometryV3` (v2.1
# §4.2: "它应是独立 strict Pydantic model，不是从 full model dump schema
# 后临时删字段"). It reuses `FloorV3`/`CellV3` unchanged — window/z/cell
# geometry are out of this batch's scope (v2.1 §0: "本批只改 Window.span
# 的 world-along authority") — but replaces `Window`/`WindowV3` with
# `WindowCitationV2`, which structurally CANNOT carry `span`, a derived
# `floor` echo, or `facade_segment_id`: the model has no field for them at
# all, so a payload that supplies one is a pydantic `extra="forbid"`
# violation (backstopped by the raw-key preflight in
# `parse.parse_raw_correction_draw`, which gives it the stable
# `producer_window_span_populated` code before pydantic's generic error —
# v2.1 §4.2).
#
# S0 ONLY defines these types and leaves them fully unwired: nothing in
# `pipeline.py`, `finalize.py`, `window_sources.py`, or any live production
# path constructs, accepts, or dispatches on `CorrectionDrawV3CitationV2`
# yet (v2.1 §10 S0: "均先不接 live production"). Wiring the live prompt/
# parser onto this type is S3/S4.
# ---------------------------------------------------------------------------

DRAW_CONTRACT_VERSION_V3_CITATION_V2 = "correction_draw_v3_window_citation_v2"


class WindowCitationV2(BaseModel):
    """Raw, model-facing v3 window: an evidence citation, not a geometry
    claim (v2.1 §4.2). No `span`, no `floor` (echoed or otherwise), no
    `facade_segment_id` — those are code-authored outputs of hydration
    (v2.1 §8), never legal draw input. `provenance` reuses the exact same
    `FieldProvenance` claim shape `WindowV3.provenance` already uses — v2.1's
    raw wire example (§4.2) is wire-identical to it; only the ENCLOSING
    window no longer carries the fields that made a citation self-referential.
    """

    model_config = ConfigDict(extra="forbid")
    id: str
    floor_id: str
    facade: Literal["North", "South", "East", "West"]
    z: list[float]  # [sill, head] world
    room: str | None = None  # cell id this window belongs to
    provenance: dict[str, FieldProvenance] | None = None

    @field_validator("facade", mode="before")
    @classmethod
    def _normalize_facade(cls, v):
        if isinstance(v, str):
            return _FACADE_ALIASES.get(v.strip().lower(), v)
        return v

    @field_validator("provenance")
    @classmethod
    def _claims_vocab(cls, value):
        if value is not None and not set(value).issubset(WINDOW_CLAIMS):
            raise ValueError("window provenance keys must be opening-claim vocabulary")
        return value


class CorrectionDrawV3CitationV2(BaseModel):
    """The raw v3 draw wire a live model may legally produce under F-9
    route② (v2.1 §4.2). Deliberately does NOT subclass `CorrectedGeometry`/
    `CorrectedGeometryV3`: v2.1 §4.2 requires an independent strict model so
    the absence of `span`/`floor`/`facade_segment_id`/`facade_segments`/
    `north_axis` is structural (a field that does not exist on this class),
    not a permissive base-class field that merely happens to be unset.
    """

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["3"]
    draw_contract_version: Literal["correction_draw_v3_window_citation_v2"]
    footprint_x: list[float]
    footprint_y: list[float]
    floors: list[FloorV3] = Field(min_length=1)
    windows: list[WindowCitationV2] = Field(default_factory=list)
    corrections: list[dict] = Field(default_factory=list)
    conflicts: list[dict] = Field(default_factory=list)
    unsupported: list[dict] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="after")
    def _raw_integrity(self):
        ids = [fl.id for fl in self.floors]
        if any(not value for value in ids) or len(ids) != len(set(ids)):
            raise ValueError("v3 floor ids must be non-empty and globally unique")
        by_id = {fl.id: fl for fl in self.floors}
        for win in self.windows:
            if win.floor_id not in by_id:
                raise ValueError(f"window {win.id}: unknown floor_id {win.floor_id}")
        # No `window_host_resolution` audit rows: that kind is Vg/host
        # resolver OUTPUT (finalize.py), never legal raw draw input — the
        # same restriction `parse.parse_correction_draw`'s b2 gate already
        # enforces for full `CorrectedGeometryV3`, restated here because
        # this raw model has no `facade_segments`/`north_axis` fields to
        # hang that check off of at the container-field level.
        for row in [*self.corrections, *self.conflicts, *self.unsupported]:
            if isinstance(row, dict) and row.get("kind") == "window_host_resolution":
                raise ValueError("raw draw must not carry window_host_resolution audit rows")
        return self
