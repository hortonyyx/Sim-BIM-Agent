"""Explicit source-only extension for simple polygon prisms and wall apertures.

The common source builder, identities, contacts and display remain unchanged.
This contract does not change drawing schemas 1/2/3. Circular curves are explicit
tolerance-bound polygon approximations; a floor footprint may exclude holes,
and source spaces may have explicitly declared interior rings.
"""
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from src.agent.correction.constants import SCHEMA_VERSION_SOURCE_POLYGON
from src.agent.correction.schema import (
    Cell, CorrectedGeometry, Floor, FootprintRing, Point2, RoomRoleEvidence, WallOpening, SourceVerticalSection,
)
from src.agent.geometry.source_curves import (
    BoundaryCurve, CurveSegment, CurveUse, expand_source_polygon_geometry,
)


class SourcePolygonCell(Cell):
    model_config = ConfigDict(extra="forbid")
    holes: list[list[Point2]] = Field(default_factory=list, exclude_if=lambda value: not value)
    vertical_sections: list[SourceVerticalSection] = Field(default_factory=list, exclude_if=lambda value: not value)
    source_refs: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    role_evidence: RoomRoleEvidence | None = None
    boundary_path: list[CurveUse] | None = None


class SourcePolygonFootprint(FootprintRing):
    vertices: list[Point2] = Field(min_length=3)
    holes: list[list[Point2]] = Field(default_factory=list)
    boundary_path: list[CurveUse] | None = None


class SourcePolygonFloor(Floor):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    footprint: SourcePolygonFootprint
    cells: list[SourcePolygonCell]

    @model_validator(mode="after")
    def validate_sectioned_spaces(self):
        from src.agent.geometry.source_volume import validate_vertical_sections
        for cell in self.cells:
            if cell.vertical_sections:
                polygon = cell.polygon or [[cell.x[0], cell.y[0]], [cell.x[1], cell.y[0]],
                                           [cell.x[1], cell.y[1]], [cell.x[0], cell.y[1]]]
                rows = validate_vertical_sections(cell.vertical_sections, polygon, cell.holes,
                                                  self.z_floor, self.ceiling_height)
                cell.vertical_sections = [SourceVerticalSection.model_validate(row) for row in rows]
        return self


class SourceWallOpening(WallOpening):
    """A rectangular aperture on an explicitly located vertical source wall."""
    kind: Literal["window", "door", "open"]
    curve_segment: CurveSegment | None = None

    @model_validator(mode="after")
    def _opening_contract(self):
        if self.space_id == self.other_space_id:
            raise ValueError("wall opening must connect distinct spaces or outdoors")
        if self.p1 == self.p2 or not self.z[0] < self.z[1]:
            raise ValueError("wall opening requires positive width and height")
        if self.kind == "window" and self.other_space_id is not None:
            raise ValueError("source polygon windows currently require an exterior host")
        if self.state is None:
            self.state = "open" if self.kind == "open" else "unknown"
        if self.kind == "open" and self.state != "open":
            raise ValueError("an unfilled opening cannot be declared closed or unknown")
        if not all(ref.strip() for ref in self.source_refs):
            raise ValueError("opening source references must not be blank")
        return self


class SourcePolygonGeometry(CorrectedGeometry):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["source_polygon_v1"] = SCHEMA_VERSION_SOURCE_POLYGON
    floors: list[SourcePolygonFloor]
    openings: list[SourceWallOpening] = Field(default_factory=list)
    boundary_curves: dict[str, BoundaryCurve] = Field(default_factory=dict)
    curve_discretization: dict = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def expand_declared_curves(cls, value):
        return expand_source_polygon_geometry(value)
