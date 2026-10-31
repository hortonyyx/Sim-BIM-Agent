"""Correction trust boundary and the separate draw/final ring contracts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from pydantic import BaseModel

from src.agent.correction.cell_geometry import normalized_ccw_polygon, validate_cell_polygon
from src.agent.correction.config import load_core_tolerances
from src.agent.correction.schema import (
    DRAW_CONTRACT_VERSION_V3_CITATION_V2,
    CorrectedGeometry,
    CorrectedGeometryV3,
    CorrectionDrawV3CitationV2,
)

# E4-output-contract spec v2 §3.2bis writer contract: the frozen phase
# vocabulary. `b2` = draw/Vg finalize (north_axis must stay None);
# `e4_orientation` = the deterministic orientation-enrichment augment
# (BO-CR10: narrowed from bare `str` to the contract Literal).
PhaseContract = Literal["b2", "e4_orientation"]


@dataclass(frozen=True)
class CorrectionTarget:
    schema_version: str
    schema_model: type[CorrectedGeometry]
    capability_profile: str
    phase_contract: PhaseContract = "b2"
    # F-9 route② S0 (2026-08-11, design v2.1 §4.3): v3-under-route② targets
    # additionally split "what the model is allowed to write" (`draw_model`)
    # from "the fully-hydrated contract the deterministic core produces"
    # (`full_model`) -- today's single `schema_model` conflates the two for
    # v1/v2/legacy-v3 targets, where the model IS allowed to author `span`.
    # `correction_target()` below (the live factory `run_correction`/
    # `pipeline.py` actually call) NEVER sets these fields -- S0 leaves the
    # live path on the existing `schema_model`-only contract; only the new,
    # unwired `correction_target_v2()` populates them (v2.1 §10 S0: "均先不
    # 接 live production").
    draw_model: type[BaseModel] | None = None
    full_model: type[BaseModel] | None = None
    draw_contract_version: str | None = None
    full_schema_version: str | None = None
    # Explicit legacy artifact loader registry (v2.1 §4.1: "禁止按『有没有
    # span』猜版本"; §10 S0: "显式 loader registry"). Keyed by the wire's own
    # `artifact_version`/`draw_contract_version` string, never inferred from
    # field presence. `None` for every profile that has no legacy artifact
    # axis (v1/v2/rectangular).
    legacy_artifact_loader_registry: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.phase_contract not in ("b2", "e4_orientation"):
            raise ValueError(
                f"unknown correction phase_contract {self.phase_contract!r}; "
                "frozen vocabulary is 'b2' | 'e4_orientation'"
            )
        if (self.draw_model is None) != (self.draw_contract_version is None):
            raise ValueError("draw_model and draw_contract_version must be set together or not at all")
        if (self.full_model is None) != (self.full_schema_version is None):
            raise ValueError("full_model and full_schema_version must be set together or not at all")


def correction_target(capability_profile: str) -> CorrectionTarget:
    if capability_profile == "rectangular":
        return CorrectionTarget("1", CorrectedGeometry, capability_profile)
    if capability_profile == "orthogonal_polygon":
        return CorrectionTarget("3", CorrectedGeometryV3, capability_profile)
    raise ValueError(f"unknown correction capability profile {capability_profile!r}")


def correction_target_v2(capability_profile: str = "orthogonal_polygon") -> CorrectionTarget:
    """F-9 route② S0 (design v2.1 §4.3/§10 S0): the raw/full-split target for
    the future window-citation live path.

    NOT called by `correction_target()`, `pipeline.py`, or any live
    production entry point (v2.1 §10 S0: "均先不接 live production") --
    this is schema/serialize/reload/hash-testable scaffolding only. `S3`/`S4`
    are the batches that make `run_correction` actually construct a
    `CorrectionTarget` through this function.
    """
    if capability_profile != "orthogonal_polygon":
        raise ValueError(
            f"unknown correction capability profile {capability_profile!r} for route② target"
        )
    from src.agent.correction.window_position import WINDOW_RESOLVER_ARTIFACT_LOADER_REGISTRY

    return CorrectionTarget(
        "3", CorrectedGeometryV3, capability_profile,
        draw_model=CorrectionDrawV3CitationV2, full_model=CorrectedGeometryV3,
        draw_contract_version=DRAW_CONTRACT_VERSION_V3_CITATION_V2, full_schema_version="3",
        legacy_artifact_loader_registry=WINDOW_RESOLVER_ARTIFACT_LOADER_REGISTRY,
    )


def ensure_corrected_geometry(value: dict | CorrectedGeometry) -> CorrectedGeometry:
    # Capability's registry is deliberately consulted at runtime so test/feature
    # profiles can register a future read-only legacy schema while still keeping
    # genuinely unknown versions fail-closed.
    from src.agent.geometry.capability import SUPPORTED_SCHEMA_VERSIONS
    from src.agent.correction.constants import SCHEMA_VERSION_SOURCE_POLYGON
    version = str(value.get("schema_version", "1") if isinstance(value, dict) else value.schema_version)
    if version == SCHEMA_VERSION_SOURCE_POLYGON:
        from src.agent.geometry.source_schema import SourcePolygonGeometry
        return SourcePolygonGeometry.model_validate(value if isinstance(value, dict) else value.model_dump())
    if isinstance(value, dict):
        version = str(value.get("schema_version", "1"))
        if version == "3":
            return CorrectedGeometryV3.model_validate(value)
        if version in SUPPORTED_SCHEMA_VERSIONS:
            return CorrectedGeometry.model_validate(value)
        raise ValueError(f"unsupported correction schema_version {version!r}")
    version = str(getattr(value, "schema_version", "1"))
    if version == "3":
        return CorrectedGeometryV3.model_validate(value.model_dump())
    if version in SUPPORTED_SCHEMA_VERSIONS:
        return value
    raise ValueError(f"unsupported correction schema_version {version!r}")


def _ring_checks(geom: CorrectedGeometry, *, canonical: bool) -> None:
    if geom.schema_version != "3":
        return
    tol = load_core_tolerances()
    for floor in geom.floors:
        ring = floor.footprint.vertices
        # Reuse the established robust orthogonal/simple/min-edge checker via a
        # temporary cell-shaped object; its input contract is identical.
        class _Ring:
            polygon = [list(p) for p in ring]
            x = [min(p[0] for p in ring), max(p[0] for p in ring)]
            y = [min(p[1] for p in ring), max(p[1] for p in ring)]
            id = "floor_footprint"
        validate_cell_polygon(_Ring(), min_edge_length_m=tol.min_edge_length_m,
                              require_bbox_match=False, allow_cw=not canonical,
                              allow_closed=not canonical)


def parse_correction_draw(payload: dict, target: CorrectionTarget) -> CorrectedGeometry:
    # B5 producer draw is intentionally unmounted.  Check the raw payload
    # before strict V3 model validation so a prefilled reference receives its
    # stable source-trust code instead of an incidental "unknown segment".
    #
    # F-2c/F-7 rework r1 (MAJOR ③) — path honesty, NOT a classification site:
    # these two raises are reached only when `parse_correction_draw` is handed a
    # *dict*, which in production happens inside the inner-retry validator
    # (`_schema_only_correction_validator` / `_make_correction_validator`, the
    # validator `_draw_correction` wires) and `run_correction`'s own parse — all
    # of which wrap any exception into a RuntimeError. So they trigger the inner
    # BLIND retry (up to 3 chances for the model to stop prefilling) and NEVER
    # reach `run_stage._draw_correction`'s `model_draw_error`→archive classifier.
    # The `category` below is required by the type and records fault attribution
    # (a prefill IS the model's fault); it is not consumed by the outer
    # classifier at this site. The same prefill condition is independently
    # caught on the live post-draw path by `_producer_preflight`
    # (window_sources.py), which IS routed as model_draw_error → archive +
    # resample — so classification coverage does not depend on these raises.
    # Their value here is a STABLE, named rejection code in the inner-retry
    # channel (without them the V3 schema validator still rejects a prefilled
    # segment id, but as a generic ValidationError, not this stable code).
    if target.schema_version == "3" and isinstance(payload, dict):
        # F-16 follow-up (2026-08-08, orchestrator interface sweep §6 摊一
        # Step 1): the forbidden field NAMES for each nested list field are
        # derived from the schema's own CORRECTION_DRAW_FORBIDDEN marker
        # (schema.nested_draw_forbidden_fields) instead of being hardcoded
        # here — this used to literally spell out "facade_segment_id",
        # independently of the marker vocab.producer_facing_json_schema
        # already reads for the SAME field, the exact two-list drift F-15's
        # top-level fix (draw_forbidden_field_names, below) already closed
        # one level up but left open here, one level down.
        from src.agent.correction.schema import nested_draw_forbidden_fields
        for list_field, marked_names in nested_draw_forbidden_fields(target.schema_model).items():
            items = payload.get(list_field)
            if isinstance(items, list) and any(
                isinstance(item, dict) and any(item.get(name) is not None for name in marked_names)
                for item in items
            ):
                from src.agent.correction.window_sources import WindowResolverInputError
                raise WindowResolverInputError("producer_segment_ref_prefilled", category="model_draw_error")
        # F-16 (2026-08-08, §6 摊一 Step 2): `WindowV3.floor` is a SEPARATE
        # marker (CORRECTION_DRAW_DERIVED, not CORRECTION_DRAW_FORBIDDEN) —
        # the schema derives it from `floor_id` unconditionally the moment
        # `model_validate` succeeds, so this raw-payload dict is the ONLY
        # place "did the model supply floor" is still observable at all (see
        # `schema.nested_draw_derived_fields`'s docstring — a validated
        # instance can never tell a model-drawn value from a derived one
        # apart after the fact). Checked here, not folded into the loop
        # above, because it needs its OWN error code/guidance text
        # (`producer_window_floor_populated`) — reusing
        # `producer_segment_ref_prefilled`'s message would tell the model to
        # remove `facade_segments`/`facade_segment_id`, the wrong field.
        from src.agent.correction.schema import nested_draw_derived_fields
        for list_field, marked_names in nested_draw_derived_fields(target.schema_model).items():
            items = payload.get(list_field)
            if isinstance(items, list) and any(
                isinstance(item, dict) and any(item.get(name) is not None for name in marked_names)
                for item in items
            ):
                from src.agent.correction.window_sources import WindowResolverInputError
                raise WindowResolverInputError("producer_window_floor_populated", category="model_draw_error")
        audit_rows = [payload.get(name) for name in ("corrections", "conflicts", "unsupported")]
        if any(isinstance(rows, list) and any(isinstance(item, dict) and item.get("kind") == "window_host_resolution" for item in rows) for rows in audit_rows):
            from src.agent.correction.window_sources import WindowResolverInputError
            raise WindowResolverInputError("producer_resolver_audit_prefilled", category="model_draw_error")
    geom = ensure_corrected_geometry(payload)
    if geom.schema_version != target.schema_version:
        raise ValueError("correction draw schema_version does not match selected target")
    if target.phase_contract == "b2" and geom.schema_version == "3":
        assert isinstance(geom, CorrectedGeometryV3)
        # F-15 follow-up (2026-08-07, orchestrator A3): the forbidden field
        # NAMES are derived from the schema's own CORRECTION_DRAW_FORBIDDEN
        # marker (schema.draw_forbidden_field_names) instead of being
        # hardcoded here a second time — `facade_segments` and `north_axis`
        # used to each be named literally in this `if`, independently of the
        # marker vocab.producer_facing_json_schema already read, and the two
        # lists drifted (north_axis was marked-forbidden-in-practice by this
        # gate but not yet marked in the schema, so the prompt kept showing
        # it — a real run hit exactly that gap). This `if` is still scoped to
        # phase_contract == "b2" (the draw phase) exactly as before: the
        # SEPARATE e4_orientation phase (orientation.py, a deterministic
        # post-draw enrichment, never an LLM prompt) legitimately populates
        # north_axis and never reaches this branch.
        from src.agent.correction.schema import draw_forbidden_field_names
        populated = [
            name for name in draw_forbidden_field_names(CorrectedGeometryV3)
            if getattr(geom, name)
        ]
        if populated:
            # F-15 follow-up: same typed exception class + category as the
            # OTHER model_draw_error doors in this file (facade_segment_id /
            # window_host_resolution, above) and in window_sources.py's
            # `_producer_preflight` — not a plain ValueError — so this
            # rejection gets the SAME retry-guidance treatment
            # (vocab._MODEL_DRAW_ERROR_GUIDANCE) instead of silently falling
            # through to a blind retry (F-15 A1 pattern: a real run burned 2
            # of its 3 attempts on this exact message, un-guided, before this
            # fix). The message text keeps the original prefix
            # ("b2 draw contract requires empty facade_segments and null
            # north_axis") so the pre-existing
            # test_v3_producer_prefilled_facade_segments_still_rejected regex
            # match still passes unchanged.
            from src.agent.correction.window_sources import WindowResolverInputError
            raise WindowResolverInputError(
                "producer_b2_forbidden_field_populated",
                {
                    "message": (
                        "b2 draw contract requires empty facade_segments and "
                        "null north_axis"
                    ),
                    "fields": populated,
                },
                category="model_draw_error",
            )
    _ring_checks(geom, canonical=False)
    return geom


def validate_final_corrected_geometry(geom: CorrectedGeometry) -> CorrectedGeometry:
    result = ensure_corrected_geometry(geom)
    _ring_checks(result, canonical=True)
    return result


def parse_raw_correction_draw(payload: dict) -> CorrectionDrawV3CitationV2:
    """F-9 route② S0 (design v2.1 §4.2/§10 S0): parse a raw model-facing v3
    window-citation draw.

    NOT called by `_draw_correction`, `run_correction`, or any live parse
    path (v2.1 §10 S0: "均先不接 live production") — S3/S4 wire this in.
    Exists now so the raw-key preflight and the strict `extra="forbid"`
    schema itself are independently schema/serialize/reload-testable ahead
    of that wiring.

    Mirrors the existing `parse_correction_draw` raw-payload-dict preflight
    pattern (same file, above): a raw-key check runs BEFORE
    `CorrectionDrawV3CitationV2.model_validate`, so a live model that writes
    `span` on a window gets the STABLE `producer_window_span_populated`
    code (v2.1 §4.2) instead of a generic, un-actionable pydantic
    `ValidationError` from the `extra="forbid"` schema violation that would
    otherwise reject it anyway (the raw model itself has no `span` field at
    all — the preflight is purely about giving the rejection a stable,
    retry-guidable code, not about whether it gets rejected).
    """
    if not isinstance(payload, dict):
        raise TypeError("parse_raw_correction_draw requires a raw dict payload")
    windows = payload.get("windows")
    if isinstance(windows, list):
        offenders = [
            item.get("id") for item in windows
            if isinstance(item, dict) and "span" in item
        ]
        if offenders:
            from src.agent.correction.window_sources import WindowResolverInputError
            raise WindowResolverInputError(
                "producer_window_span_populated",
                {"window_ids": offenders},
                category="model_draw_error",
            )
    return CorrectionDrawV3CitationV2.model_validate(payload)
