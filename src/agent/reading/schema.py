"""0_reading vector-JSON schema (M1: P1a dimension chain + P1b facade image-local).

The reading stage retraces each drawing into a semantic vector JSON. This module
is the canonical Pydantic model for that artifact. Two M1 schema changes over the
legacy shape (see build plan §M1):

**P1a — dimension chain + provenance.** Each ``Dimension`` gains
``chain_id / role(overall|segment|baseline) / order / value_m / text_verbatim /
anchor`` so the dimension-chain-closure check (Σ segments == overall) can run and
so the literal OCR text is preserved verbatim for the judge's "number copied
wrong" rubric (transcription truth lives here, not in a parsed float).

**P1b — facade is image-local only.** An elevation declares ``view_facade`` (which
facade, from the trusted image name/metadata), ``mirrored``, and structured
``orientation_evidence``. World axis / sign / base are NOT here — 1_correction
derives them. The legacy free-text ``facade_axis_note`` is retained for the
migration adapter (legacy.py) but is not load-bearing.

``extra="allow"`` everywhere keeps older artifacts loadable; the deterministic
linter (validator/checks/reading.py) enforces the real invariants.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Facade = Literal["North", "South", "East", "West"]
ImageKind = Literal["plan", "elevation", "section", "supplementary", "other"]


class Stroke(BaseModel):
    """One traced primitive. ``pen`` is the semantic category; ``geometry`` is a
    free dict (line: p1/p2/thickness_m; rect: x_range_m/y_range_m).

    ``line_style`` and ``visibility`` are image-local recognition attributes,
    not topology or world fields. ``visibility="hidden"`` (or
    ``line_style="dashed"``) records an observed dashed line, hidden feature,
    or projection above the cut plane. Downstream correction must not promote a
    hidden observation into an entity Window; reading records the observation
    faithfully and leaves that decision to the later-stage contract.
    """

    model_config = ConfigDict(extra="allow")
    id: str
    pen: str
    geometry: dict = Field(default_factory=dict)
    provenance: Literal["seen", "dimension_derived", "estimated", "unknown"] | None = None
    confidence: Literal["high", "medium", "low"] | None = None
    line_style: Literal["solid", "dashed", "dash_dot", "unknown"] | None = None
    visibility: Literal["visible", "hidden"] | None = None
    dimension_refs: list[str] = Field(default_factory=list)
    note: str | None = None


class DimensionRole(str, Enum):
    OVERALL = "overall"
    SEGMENT = "segment"
    BASELINE = "baseline"


class Dimension(BaseModel):
    """One dimension annotation. P1a fields are optional so legacy artifacts load;
    the legacy adapter back-fills ``value_m`` / ``text_verbatim`` from ``text``."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)
    id: str
    text: str | None = None                 # legacy display text
    text_verbatim: str | None = None        # P1a: literal OCR string (truth)
    value_m: float | None = None            # P1a: parsed numeric metres
    from_pt: list[float] | None = Field(default=None, alias="from")
    to: list[float] | None = None
    axis: str | None = None
    chain_id: str | None = None             # P1a: groups a dimension chain
    role: DimensionRole | None = None       # P1a: overall | segment | baseline
    order: int | None = None                # P1a: position within the chain
    anchor: list[float] | None = None       # P1a: pixel bbox / anchor
    note: str | None = None


class OrientationEvidence(BaseModel):
    """Structured source for the facade-orientation claim (P1b)."""

    model_config = ConfigDict(extra="allow")
    source: str            # "image_name" | "metadata" | "ocr_label" | "legacy_note"
    detail: str = ""
    confidence: Literal["high", "medium", "low", "unknown"] = "unknown"


class FacadeOrientation(BaseModel):
    """Image-LOCAL facade orientation (P1b). NO world axis/sign/base here."""

    model_config = ConfigDict(extra="allow")
    view_facade: Facade | None = None
    # Deprecated input-only audit field. ReadingViews v2 fixes x at the image's
    # left edge and readers must not emit a direction declaration.  Keep raw
    # historical values loadable, but no judge path may read this field.
    local_x_positive: str | None = None
    mirrored: Literal["true", "false", "unknown"] | bool = "unknown"
    orientation_evidence: list[OrientationEvidence] = Field(default_factory=list)


class RoomRoleObservation(BaseModel):
    """Topology-light room-role observation from visible labels or furniture."""

    model_config = ConfigDict(extra="allow")
    id: str
    anchor: list[float]
    role: str
    label_text: str
    basis: str
    confidence: float | None = None


class ReadingView(BaseModel):
    """One drawing's reading-stage output."""

    model_config = ConfigDict(extra="allow")
    image_label: str = ""
    image_kind: ImageKind | str = "plan"
    scale_origin: dict | None = None
    strokes: list[Stroke] = Field(default_factory=list)
    dimensions: list[Dimension] = Field(default_factory=list)
    ocr_texts: list = Field(default_factory=list)
    room_labels: list[RoomRoleObservation] = Field(default_factory=list)
    # uncaptured: things deliberately not traced. Must EXIST and be a list (clean
    # drawing → []); the linter only enforces "exists + list", never non-empty.
    uncaptured: list = Field(default_factory=list)
    self_check: dict | None = None
    # P1b canonical (populated for elevations; plans leave it None).
    facade: FacadeOrientation | None = None
    # Legacy free-text translation note — migration only, not load-bearing.
    facade_axis_note: str | None = None
    # Set by the migration adapter so consumers/tests can detect a migrated view.
    migrated_from_legacy: bool = False
    migration_flags: list[str] = Field(default_factory=list)
