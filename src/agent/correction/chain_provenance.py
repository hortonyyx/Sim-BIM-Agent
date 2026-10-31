"""The as_drawn evidence-chain leg's writer-replay provenance carrier (W#3).

WHY THIS MODULE EXISTS (wallhunt 2026-09-08b, dispatch 2026-09-08c S-A):
``StageRunner.record``'s B5 anti-tamper lock replays the LEGACY deterministic
core (``extract_authoritative_envelope`` + ``apply_deterministic_core``) from
the marker's embedded bytes and rejects any candidate whose core-owned
projection drifts.  An as_drawn-chain candidate dies at that replay BY
CONSTRUCTION — its geometry came through the projection bridge, snap and
assembly, not the legacy envelope core — so the wall was
``writer_core_projection_drift`` on every genuine as_drawn write.

The fix is NOT to weaken the lock: it is a SECOND replay of equal strength,
one per leg, dispatched by an explicit marker this carrier provides.  Equal
strength means the same property F-22 BLOCKER-1 round 2 established for the
legacy leg — the accepted geometry must be reproducible, field for field,
from FROZEN UPSTREAM BYTES through the real production functions:

  elevation bytes  -> ``derive_floor_ladder``                (z rungs)
  plan bytes       -> ``adapt_as_drawn_plan``                (evidence)
  compilation bytes-> ``cut_lines_from_wall_compilation`` +
                      ``project_cut_lines``                  (per-floor geom)
  plan bytes       -> ``read_plan_calibration_declaration`` +
                      ``snap_footprints_to_reference``       (cross-floor snap)
  ladder + snapped -> ``assemble_multifloor_geometry``       (the producer)
  plan + producer  -> window population, then optional
                      ``populate_as_drawn_openings``          (explicit recipe)
  producer         -> ``build_verified_window_inputs_as_drawn`` +
                      ``finalize_as_drawn_chain_geometry``   (the final geom)

The one link the deterministic replay cannot re-derive is the decision loop's
model beat — exactly as the legacy replay cannot re-derive the model's draw.
Each leg therefore carries that model product as frozen embedded bytes: the
legacy leg inside ``VerifiedWindowResolverInputs.producer_draw_canonical_
bytes``; THIS leg inside ``AsDrawnFloorCompilationV1.compilation_bytes``, the
final ``WallCompilationV1`` the loop settled on (whose ``content_sha256`` the
loop's own outcome record and the projection envelope both already bind to).

Every OTHER byte the replay consumes comes from the marker's embedded
artifacts (view manifest + all reading products), cross-checked per floor by
``source_bytes_sha256`` — the same anchor ``chain_source_record.json`` files
on disk during the chain run.  ⛔ The writer never reads the run directory:
everything it replays from travels inside the candidate's own carrier, so the
lock stays a property of the attempt, not of files a caller could rewrite
afterwards.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from typing import Annotated

from src.agent.correction.window_sources import Hex64, canonical_json_bytes, canonical_sha256
from src.agent.correction.wall_gap_review import WallGapDecisionV1


def _without_content_hash(model: BaseModel) -> dict:
    data = model.model_dump(mode="json")
    data.pop("content_sha256", None)
    return data

StableName = Annotated[str, StringConstraints(min_length=1, max_length=256)]

_CFG = ConfigDict(extra="forbid", frozen=True, strict=True)


class AsDrawnFloorCompilationV1(BaseModel):
    """One plan product's frozen chain inputs for the writer-side replay.

    ``input_id`` is the manifest slot id — the key that indexes the marker's
    ``raw_reading_artifacts`` for this floor's plan bytes.  ``product_
    filename`` is the file the chain actually froze and consumed (the
    manifest's ``expected_output_id`` name); the replay re-adapts those bytes
    under ``Path(product_filename).stem`` exactly as ``run_correction_
    evidence_chain`` did, because the evidence bundle's identity — and with it
    the compilation's ``bundle_content_sha256`` binding — is derived from that
    stem, ⛔ not from the manifest slot id.  ``floor_ref`` is the flow-level
    semantic slot (storey ordering anchor: production wires the chains
    ground-up sorted by it, and the replay must zip its floors to the ladder
    in the same order).
    """

    model_config = _CFG

    input_id: StableName
    product_filename: StableName
    floor_ref: StableName
    compilation_bytes: bytes
    #: sha256 of ``compilation_bytes`` (byte-level self-integrity)
    compilation_sha256: Hex64
    #: sha256 of the plan product bytes the chain froze — must equal the
    #: marker's embedded bytes for ``input_id`` at replay time
    source_bytes_sha256: Hex64


class PlanWallOpeningPolicyV1(BaseModel):
    """Explicit derivation recipe; absent on historical window-only runs."""

    model_config = _CFG
    version: Literal["plan_wall_openings_v1"] = "plan_wall_openings_v1"
    assumed_height_m: float = Field(default=2.1, gt=0, allow_inf_nan=False)


class AsDrawnChainProvenanceV1(BaseModel):
    """The as_drawn leg's replay anchor, ground-up, one entry per storey."""

    model_config = _CFG

    schema_version: Literal["as_drawn_chain_provenance_v1"]
    floors: tuple[AsDrawnFloorCompilationV1, ...]
    # Absent means the historical position-only snap and alignment.
    endpoint_connection_policy: Literal["preserve_endpoint_connections_v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    wall_opening_policy: PlanWallOpeningPolicyV1 | None = Field(default=None, exclude_if=lambda value: value is None)
    wall_gap_decisions: tuple[WallGapDecisionV1, ...] = Field(default=(), exclude_if=lambda value: not value)
    content_sha256: Hex64

    def storey_floor_refs(self) -> tuple[str, ...]:
        return tuple(entry.floor_ref for entry in self.floors)

    @property
    def replay_input_hash(self) -> str:
        """Hash frozen model products and the explicit derivation recipe.

        The legacy proof's ``input_hash`` is the sha256 of the replayed
        producer bytes; this is the same role for this leg — one hash over
        every floor's frozen compilation bytes, in ground-up order, plus
        the opening recipe when present. Historical hashes stay unchanged.
        """
        digest = hashlib.sha256()
        for entry in self.floors:
            digest.update(entry.compilation_bytes)
        if self.wall_opening_policy is not None:
            digest.update(canonical_json_bytes(self.wall_opening_policy.model_dump(mode="json")))
        if self.endpoint_connection_policy is not None:
            digest.update(canonical_json_bytes(self.endpoint_connection_policy))
        if self.wall_gap_decisions:
            digest.update(canonical_json_bytes([d.model_dump(mode="json") for d in self.wall_gap_decisions]))
        return digest.hexdigest()

    @model_validator(mode="after")
    def _canonical_and_hashed(self):
        if not self.floors:
            raise ValueError("as_drawn chain provenance carries no storeys")
        refs = self.storey_floor_refs()
        if any(d.input_id not in {f.input_id for f in self.floors} for d in self.wall_gap_decisions):
            raise ValueError("wall_gap_review_unknown_plan")
        if len(set(refs)) != len(refs):
            raise ValueError("floor_ref values must be unique per storey")
        if tuple(sorted(refs)) != refs:
            raise ValueError(
                "floors must be listed ground-up sorted by floor_ref — "
                "production wires the chains in that order and the writer "
                "replay zips them to the ladder the same way"
            )
        for entry in self.floors:
            if (
                hashlib.sha256(entry.compilation_bytes).hexdigest()
                != entry.compilation_sha256
            ):
                raise ValueError(
                    f"compilation_sha256 mismatch for floor_ref {entry.floor_ref}"
                )
        if self.content_sha256 != canonical_sha256(_without_content_hash(self)):
            raise ValueError(
                "content_sha256 does not match canonical chain provenance"
            )
        return self


def build_chain_provenance(
    floors: "list[dict]",
    *, wall_opening_policy: PlanWallOpeningPolicyV1 | None = None,
    endpoint_connection_policy: Literal["preserve_endpoint_connections_v1"] | None = None,
    wall_gap_decisions: tuple[WallGapDecisionV1, ...] = (),
) -> AsDrawnChainProvenanceV1:
    """Assemble + self-hash a provenance carrier from per-floor fields.

    ``floors`` entries carry ``input_id`` / ``product_filename`` /
    ``floor_ref`` / ``compilation_bytes`` (raw bytes) / ``source_bytes_sha256``
    — the flow wiring reads the frozen compilations off the chain run dirs and
    hands them here; the ``compilation_sha256`` and the carrier's canonical
    ``content_sha256`` are computed HERE, once, by the only constructor, so a
    hand-built (never-hashed) carrier cannot exist.
    """
    entries = [
        AsDrawnFloorCompilationV1(
            input_id=item["input_id"],
            product_filename=item["product_filename"],
            floor_ref=item["floor_ref"],
            compilation_bytes=item["compilation_bytes"],
            compilation_sha256=hashlib.sha256(
                item["compilation_bytes"]
            ).hexdigest(),
            source_bytes_sha256=item["source_bytes_sha256"],
        )
        for item in sorted(floors, key=lambda item: item["floor_ref"])
    ]
    staged = AsDrawnChainProvenanceV1.model_construct(
        schema_version="as_drawn_chain_provenance_v1",
        floors=tuple(entries),
        wall_opening_policy=wall_opening_policy,
        endpoint_connection_policy=endpoint_connection_policy,
        wall_gap_decisions=wall_gap_decisions,
        content_sha256="0" * 64,
    )
    return AsDrawnChainProvenanceV1(
        schema_version="as_drawn_chain_provenance_v1",
        floors=tuple(entries),
        wall_opening_policy=wall_opening_policy,
        endpoint_connection_policy=endpoint_connection_policy,
        wall_gap_decisions=wall_gap_decisions,
        content_sha256=canonical_sha256(_without_content_hash(staged)),
    )


__all__ = [
    "AsDrawnChainProvenanceV1",
    "AsDrawnFloorCompilationV1",
    "PlanWallOpeningPolicyV1",
    "build_chain_provenance",
]
