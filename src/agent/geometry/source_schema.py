"""Explicit source-only extension for simple polygon prisms and wall apertures.

The common source builder, identities, contacts and display remain unchanged.
This contract does not change drawing schemas 1/2/3 or promise curved, holed,
sloping or height-varying volumes. Every floor supplies its own footprint.
"""
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from src.agent.correction.constants import SCHEMA_VERSION_SOURCE_POLYGON
from src.agent.correction.schema import (
    Cell, CorrectedGeometry, Floor, FootprintRing, Point2, RoomRoleEvidence, WallOpening,
)


class SourcePolygonCell(Cell):
    model_config = ConfigDict(extra="forbid")
    source_refs: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    role_evidence: RoomRoleEvidence | None = None


class SourcePolygonFootprint(FootprintRing):
    vertices: list[Point2] = Field(min_length=3)


class SourcePolygonFloor(Floor):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    footprint: SourcePolygonFootprint
    cells: list[SourcePolygonCell]


class SourceWallOpening(WallOpening):
    """A rectangular aperture on an explicitly located vertical source wall."""
    kind: Literal["window", "door", "open"]

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
