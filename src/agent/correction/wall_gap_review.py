"""Explicit source-space decisions for breaks grouped into one compiled wall.

Wall identity alone cannot prove a physical separator across every ink gap.
Review decisions identify a gap in frozen inputs; coordinates are derived here.
No reference geometry or case-specific room grouping is consumed.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.agent.correction.projection_bridge import CutLineV1, close_collinear_gaps, cut_lines_from_wall_compilation
from src.agent.correction.wall_compiler import WallCompilationV1
from src.agent.correction.window_sources import Hex64


class WallGapDecisionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    input_id: str = Field(min_length=1)
    wall_id: str = Field(min_length=1)
    gap_index: int = Field(ge=0)
    action: Literal["continuous_space"] = "continuous_space"
    source_bytes_sha256: Hex64
    compilation_sha256: Hex64
    image_path: str = Field(min_length=1)
    image_sha256: Hex64
    review_mode: Literal["developer_assisted", "model", "user"]
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def reason_present(self):
        if not self.reason.strip():
            raise ValueError("wall_gap_review_reason_empty")
        return self


def resolve_wall_gap_decisions(
    decisions: Sequence[WallGapDecisionV1], *, input_id: str,
    raw_reading: bytes, raw_compilation: bytes,
) -> list[dict]:
    """Bind decisions to existing solid-run gaps and related source observations.

    No caller-supplied span can remove solid ink. A changed source or compilation
    invalidates the decision even when the ordinal or wall name still exists.
    """
    selected = [d for d in decisions if d.input_id == input_id]
    if not selected:
        return []
    if len({(d.wall_id, d.gap_index) for d in selected}) != len(selected):
        raise ValueError("wall_gap_review_duplicate")
    compilation = WallCompilationV1.model_validate_json(raw_compilation)
    lines, _ = cut_lines_from_wall_compilation(compilation.walls)
    _, gaps = close_collinear_gaps(lines, resolution_m=0.)
    walls = {w.wall_id: w for w in compilation.walls}
    doc = json.loads(raw_reading)
    hypotheses = doc.get("hypotheses", {})
    result = []
    for decision in sorted(selected, key=lambda d: (d.wall_id, d.gap_index)):
        if (hashlib.sha256(raw_reading).hexdigest() != decision.source_bytes_sha256
                or hashlib.sha256(raw_compilation).hexdigest() != decision.compilation_sha256):
            raise ValueError("wall_gap_review_input_drift")
        candidates = [g for g in gaps if g.half_thickness_from_origin == decision.wall_id]
        if decision.gap_index >= len(candidates):
            raise ValueError("wall_gap_review_gap_missing")
        gap = candidates[decision.gap_index]
        wall = walls[decision.wall_id]
        faces = {r.observation_id for r in wall.source_refs}
        related = [c for c in hypotheses.get("opening_candidates", [])
                   if c.get("face_line") in faces
                   and min(c["span_m"][1], gap.to_m) > max(c["span_m"][0], gap.from_m)]
        # A review of one break cannot also erase a distinct adjacent opening.
        if any(sum(min(c["span_m"][1], g.to_m) > max(c["span_m"][0], g.from_m)
                   for g in candidates) != 1 for c in related):
            raise ValueError("wall_gap_review_opening_extent_conflict")
        classifications = [{"id": c["id"], "type": hypotheses.get("opening_types", {}).get(c["id"], "unknown")}
                           for c in related]
        if any(c["type"] == "window" for c in classifications):
            raise ValueError("wall_gap_review_conflicts_with_window")
        result.append({**decision.model_dump(mode="json"), "axis": gap.axis, "pos_m": gap.pos_m,
                       "span_m": [gap.from_m, gap.to_m], "face_observation_ids": sorted(faces),
                       "opening_classifications": classifications})
    return result


def apply_wall_gap_decisions(lines: Sequence[CutLineV1], resolved: Sequence[dict]) -> tuple[CutLineV1, ...]:
    """Separate continuity groups without changing original solid-run geometry.

    Only automatic continuation within a reviewed gap and its explicitly
    reclassified opening candidates disappear. Other door/window boundaries
    keep the original closure behavior. Group IDs retain the compiled wall ID.
    """
    by_wall = {}
    omitted_openings = set()
    for row in resolved:
        by_wall.setdefault(row["wall_id"], []).append(row)
        omitted_openings.update(c["id"] for c in row["opening_classifications"])
    result = []
    for line in lines:
        if line.kind == "opening" and line.origin_id in omitted_openings:
            continue
        gaps = sorted(by_wall.get(line.origin_id, []), key=lambda r: r["span_m"])
        if gaps and line.kind == "wall":
            if any(min(line.along_hi_m, r["span_m"][1]) > max(line.along_lo_m, r["span_m"][0]) for r in gaps):
                raise ValueError("wall_gap_review_would_remove_solid_wall")
            group = sum(line.along_lo_m >= r["span_m"][1] for r in gaps)
            line = replace(line, origin_id=f"{line.origin_id}:continuity:{group}")
        result.append(line)
    return tuple(result)


def load_wall_gap_decisions(path: Path, *, image_root: Path) -> tuple[WallGapDecisionV1, ...]:
    """Check the review's source image identity when entering a new run.

    Replay later uses the frozen decision and input hashes, just as it uses
    the frozen upstream model's wall grouping; it does not redo image judgment.
    """
    rows = json.loads(path.read_bytes())
    if not isinstance(rows, list):
        raise ValueError("wall_gap_review_expected_list")
    decisions = tuple(WallGapDecisionV1.model_validate(r) for r in rows)
    for decision in decisions:
        if hashlib.sha256((image_root / decision.image_path).read_bytes()).hexdigest() != decision.image_sha256:
            raise ValueError("wall_gap_review_image_drift")
    return decisions
