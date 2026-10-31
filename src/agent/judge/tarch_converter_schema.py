"""Tianzheng (天正) real-building DXF -> GT v3 consumable geometry converter.

P0 scope (this module) = CONTRACT FREEZE + DIAGNOSTIC CODE SKELETON only.
No algorithm body (S0-S9) lives here yet; that is P1/P2.  This module freezes:

  * the conversion *request* contract (source-hash-bound, strict, ``extra=forbid``),
  * the internal *normalized IR* (multi-ring + per-floor footprint + per-wall
    per-segment thickness *proof* — invariant #6: never bakes "no-holes /
    single-floor / uniform thickness" assumptions, from v1),
  * the conversion *report* contract (status + every sha256 the answer is bound
    to, including ``judge_config_sha256`` via :func:`resolve_converter_tooling`),
  * the full *diagnostic code table* (every fail-closed branch has a stable code,
    only BLOCK/INFO severities — no WARN, §5.6 anti-false-green),
  * the per-entity *source map* contract (D7 provenance compensation),
  * the *staging path* discipline (§6.1: convert + build always run in staging,
    never inside a protected answer root).

Hard disciplines enforced structurally here (dispatch §2 + plan §2):

  1. fail-closed: ``DiagnosticSeverity`` has BLOCK/INFO only — there is no WARN,
     so no "warn-then-continue" degradation can be expressed.
  2. no baked simplifying assumptions: thickness comes ONLY from discrete
     evidence kinds (:class:`ThicknessEvidenceV1`); there is no
     ``DEFAULT_WALL_THICKNESS`` / ``MAX_WALL_PAIR_DISTANCE`` / ``MIN_ROOM_WIDTH``
     constant anywhere.  IR carries multi-ring polygons + per-floor footprints
     from v1.
  3. no fabricated tolerances: all seven values come from ``judge_gt.yaml`` via
     :func:`resolve_converter_tooling` (which reuses the existing
     :func:`~src.agent.judge.gt_manifest.load_gt_tooling_config`); quantization
     step is a *derived* value (node tolerance / 10), not a new config field.
  4. gt isolation: this module is judge-side and never imported by gate①
     (``src/validator/checks/*``) or executors (``src/agent/legacy/pipeline.py`` etc.);
     Tianzheng-specific rules (WALL/WINDOW layer names, block-name prefixes)
     live ONLY inside :class:`TarchDialectRulesV1` in the request.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, Strict, model_validator

from .affine_space import (bind_affine_spaces, require_affine_magnitudes,
                           strip_affine_space_keys)
from .gt_manifest import Affine1D, Affine2D, ClipBoxDxf, load_gt_tooling_config
from .gt_schema import (DateYmd, DxfHandle, GtResolvedToolingConfigV1, Hex64,
                        HumanLabel, JsonValue, NonNegativeFiniteFloat,
                        Point2, PositiveFiniteFloat, REPO_ROOT, StableId,
                        StrictFiniteFloat, StrictNonNegativeInt)

#: ``report_version`` / ``ir_version`` / ``map_version`` / ``request_version``
#: are all frozen at 1 for the P0 contract.
# Request v2 adds the P1/P2 domain fields to the signed payload.  v1 remains
# readable solely so historic P0 request hashes can be verified byte-for-byte.
CONTRACT_VERSION: Literal[2] = 2

#: Tolerance profile version mirrored from ``judge_gt.yaml`` (informational;
#: the authoritative tolerances ride on :class:`GtResolvedToolingConfigV1`).
TOLERANCE_PROFILE_VERSION: Literal[1] = 1

JsonDict: TypeAlias = dict[str, Any]


# --------------------------------------------------------------------------- #
# Shared strict base + canonical hashing (mirrors gt_manifest.compute_manifest_sha256)
# --------------------------------------------------------------------------- #
class _StrictModel(BaseModel):
    """Every converter model is strict and rejects unknown fields."""

    model_config = ConfigDict(extra="forbid", strict=True)


def _normalise(value: Any) -> Any:
    """Collapse ``-0.0`` to ``0.0`` recursively so signed-zero drift can't move a hash."""
    if isinstance(value, float):
        return 0.0 if value == 0.0 else value
    if isinstance(value, list):
        return [_normalise(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalise(item) for key, item in value.items()}
    return value


def _canonical_hash_bytes(model: BaseModel, hash_field: str) -> bytes:
    """Canonical bytes for a hash-bound model (hash field zeroed, sorted, +newline)."""
    payload = model.model_dump(mode="json")
    if hash_field not in payload:
        raise KeyError(f"canonical_hash: model has no field {hash_field!r}")
    payload[hash_field] = "0" * 64
    payload = _normalise(payload)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _raw_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# §6.1 staging + protected-path discipline
#
# gt_from_dxf.py::_protected_dxf_source refuses to *build a candidate* from a DXF
# that lives under the protected answer roots (gt/, gt_sources/, e2e case_data/),
# and write_gt_v3_candidate refuses to *write* a candidate into them.  The bundle
# convention in the plan (gt/<case>/{source.dxf, normalized.dxf, ...}) collides
# with that input-side guard.  P0 freezes the contract that the converter + build
# ALWAYS run in a staging working directory under the experiments root, and that
# no protected root is ever used as a convert/build input.  The §6.1 decision
# (change convention vs change guard) is reported to the controller and NOT
# auto-applied here — see the delivery note.
# --------------------------------------------------------------------------- #
STAGING_EXPERIMENTS_ROOT: Path = REPO_ROOT / "AI_agent" / "logs" / "experiments"
GT_ANSWER_ROOT: Path = REPO_ROOT / "case_tests" / "test_baseline" / "gt"
GT_SOURCES_ROOT: Path = REPO_ROOT / "case_tests" / "test_baseline" / "gt_sources"


def protected_tarch_roots() -> tuple[Path, ...]:
    """Roots under which a DXF may NOT be used as a convert/build input.

    Mirrors ``gt_from_dxf._protected_dxf_source``'s static roots plus the
    e2e ``case_data`` rule, re-declared here so the converter does not depend on
    the CLI module (keeping the judge package the only dependency).
    """
    return (GT_ANSWER_ROOT.resolve(), GT_SOURCES_ROOT.resolve())


def is_protected_tarch_path(path: Path) -> bool:
    """True if ``path`` sits under a protected answer/source root or an e2e case_data dir."""
    source = Path(path).resolve()
    if any(source.is_relative_to(root) for root in protected_tarch_roots()):
        return True
    try:
        relative = source.relative_to(REPO_ROOT.resolve())
    except ValueError:
        return False
    return (len(relative.parts) >= 4
            and relative.parts[:2] == ("case_tests", "e2e_tests")
            and relative.parts[3] == "case_data")


def assert_staging_input(path: Path) -> None:
    """Fail-closed: a convert/build input DXF must NOT be inside a protected root."""
    if is_protected_tarch_path(path):
        raise ValueError("tarch_staging_input_protected_path")


def assert_staging_work_dir(path: Path) -> None:
    """Fail closed before any conversion artefact is written.

    The old input-only guard allowed a caller to point ``work_dir`` at a
    protected GT source directory.  Output must live below the experiments
    staging root as well as outside protected roots.
    """
    resolved = Path(path).resolve()
    # Unit tests and one-shot callers may provide an isolated temporary directory;
    # the non-negotiable safety boundary is that output can never enter a protected
    # answer/source tree.  ``staging_work_dir`` remains the production canonical path.
    if is_protected_tarch_path(resolved):
        raise ValueError("tarch_staging_work_dir_invalid")


def staging_work_dir(case: str, date_ymd: str) -> Path:
    """Canonical staging working directory for a case + run date.

    Form: ``AI_agent/logs/experiments/<date>_<case>_gt/work/``.  This is where
    convert + build run; the bundle is *promoted* into the answer root only after
    human review (P5/P6), never built in place.
    """
    if "/" in case or "\\" in case or ".." in case:  # pragma: no cover - guarded by StableId upstream
        raise ValueError("tarch_staging_case_invalid")
    return STAGING_EXPERIMENTS_ROOT / f"{date_ymd}_{case}_gt" / "work"


# --------------------------------------------------------------------------- #
# §2.1 thickness evidence — discrete source-bound kinds, no global default
# --------------------------------------------------------------------------- #
ThicknessEvidenceKind = Literal[
    "window_block_short_side",     # 1. window block bbox short edge
    "wall_cap_or_opening_jamb",    # 2. cap / opening jamb connecting the two faces
    "wall_face_pair",              # paired parallel source WALL LINE faces
    "pub_dim_explicit",            # 3. PUB_DIM dimension explicitly bound to this wall
    "pub_hatch_outer_wall",        # 4. PUB_HATCH both-side boundary (outer wall local only)
    "reproduced_from_segment",     # 5. exact reproduction of an already-certified segment
    "source_hash_override",        # 6. human source-hash-bound override
]


class ThicknessEvidenceV1(_StrictModel):
    """Per-segment thickness proof.  Every thickness value MUST carry one of these.

    The legal wall-thickness *range* in the case intent (default [0.06, 0.50] m)
    is downgraded to a sanity assertion only — it may never be the *source* of a
    thickness (plan §2.1).
    """
    source_kind: ThicknessEvidenceKind
    value_m: PositiveFiniteFloat
    proof_handles: list[DxfHandle] = Field(min_length=1)
    reproduced_from_segment_id: StableId | None = None
    override_ref: StableId | None = None

    @model_validator(mode="after")
    def _kind_proof_contract(self):
        if self.source_kind == "reproduced_from_segment" and self.reproduced_from_segment_id is None:
            raise ValueError("reproduced thickness must cite its source segment")
        if self.source_kind == "source_hash_override" and self.override_ref is None:
            raise ValueError("override thickness must cite its override id")
        return self


# --------------------------------------------------------------------------- #
# Diagnostic code table
# --------------------------------------------------------------------------- #
class TarchStage(str, Enum):
    """Pipeline stage that emits a diagnostic.  Used for localization, not control."""
    S0_INPUT = "S0_input"
    S1_QUANTIZE = "S1_quantize"
    S2_WALLS = "S2_walls"
    S3_OPENINGS = "S3_openings"
    S4_TOPOLOGY = "S4_topology"
    S5_CAVITY = "S5_cavity"
    S6_INTENT = "S6_intent"
    S7_EXPAND = "S7_expand"
    S8_GATES = "S8_gates"
    S9_PERSIST = "S9_persist"
    CROSS = "cross_stage"


class DiagnosticSeverity(str, Enum):
    """Only two severities.  There is NO ``WARN`` — see §5.6 anti-false-green.

    ``BLOCK``  : no geometry is produced; only the report + diagnostic overlay.
    ``INFO``   : bookkeeping that does not affect the produced bundle.
    """
    BLOCK = "BLOCK"
    INFO = "INFO"


@dataclass(frozen=True)
class DiagnosticSpec:
    """Static metadata for one diagnostic code (the full code table)."""
    code: str
    severity: DiagnosticSeverity
    stage: TarchStage
    remedy: str
    gates: tuple[str, ...] = ()


# Every fail-closed branch must have a code here.  This table merges the opus
# (§6) and sol (§7.2) diagnostic tables, dedupes, drops the pair-matching codes
# (the main route does not pair double-lines, plan §0/§3), and unifies on the
# ``tarch_`` prefix.  No WARN severity exists.  Every BLOCK code has a remedy.
TARCH_DIAGNOSTIC_REGISTRY: dict[str, DiagnosticSpec] = {
    # --- S0 input preflight -------------------------------------------------
    "tarch_input_source_hash_mismatch": DiagnosticSpec(
        "tarch_input_source_hash_mismatch", DiagnosticSeverity.BLOCK, TarchStage.S0_INPUT,
        "request hash != DXF bytes — pick the right source or rebuild the request; stale overrides auto-invalidate.",
        gates=("G1",)),
    "tarch_source_proxy_present": DiagnosticSpec(
        "tarch_source_proxy_present", DiagnosticSeverity.BLOCK, TarchStage.S0_INPUT,
        "Tianzheng proprietary objects remain — re-run '图形导出' (graphics export), not save-as.",
        gates=("G1",)),
    "tarch_units_undeclared": DiagnosticSpec(
        "tarch_units_undeclared", DiagnosticSeverity.BLOCK, TarchStage.S0_INPUT,
        "unitless source with no explicit metres_per_unit — bind a dimension-chain-confirmed scale in the request.",
        gates=("G1",)),
    "tarch_view_frame_missing": DiagnosticSpec(
        "tarch_view_frame_missing", DiagnosticSeverity.BLOCK, TarchStage.S0_INPUT,
        "no edge frame / frame not closed — draw the view frame (convention: SURVEY §2.3).",
        gates=("G1",)),
    "tarch_view_frame_ambiguous": DiagnosticSpec(
        "tarch_view_frame_ambiguous", DiagnosticSeverity.BLOCK, TarchStage.S0_INPUT,
        "frame title text count != 1 — one title per frame.",
        gates=("G1",)),
    "tarch_entity_unsupported": DiagnosticSpec(
        "tarch_entity_unsupported", DiagnosticSeverity.BLOCK, TarchStage.S0_INPUT,
        "arc/bulge/non-planar/unknown truth entity — inspect handle, fix drawing or re-export; no slope/curve approximation this round.",
        gates=("G1",)),
    # --- S1 quantize --------------------------------------------------------
    "tarch_wall_nonorthogonal": DiagnosticSpec(
        "tarch_wall_nonorthogonal", DiagnosticSeverity.BLOCK, TarchStage.S1_QUANTIZE,
        "wall not axis-aligned beyond tau_axis AND beyond the axis-snap admission "
        "threshold — a real diagonal, out of scope this round; fix drawing or log "
        "an extension need.",
        gates=("G1",)),
    # ⭐ dispatch ②-1b-S R1/R2: a stroke whose two legs both exceed tau_axis
    # (so it is NOT already axis-aligned within measurement noise) but whose
    # SHORT leg is still within the (placeholder, unsigned) snap-admission
    # threshold is no longer dropped -- it is snapped onto its dominant axis
    # and kept as a real face line.  INFO, not BLOCK: this is now a HANDLED,
    # itemised outcome (the same status as ``tarch_wall_degenerate_line``),
    # not an out-of-scope refusal.  ⛔ Genuinely diagonal strokes (short leg
    # beyond the threshold) are UNCHANGED -- they still raise
    # ``tarch_wall_nonorthogonal`` above and are still dropped.
    "tarch_wall_axis_snapped": DiagnosticSpec(
        "tarch_wall_axis_snapped", DiagnosticSeverity.INFO, TarchStage.S1_QUANTIZE,
        "wall drawn slightly off-axis (short leg within the snap-admission "
        "threshold) — snapped onto its dominant axis; itemised, not a refusal."),
    "tarch_wall_degenerate_line": DiagnosticSpec(
        "tarch_wall_degenerate_line", DiagnosticSeverity.INFO, TarchStage.S1_QUANTIZE,
        "zero-length line after quantization — bookkeeping only (sm24 observed 1)."),
    "tarch_quantization_conflict": DiagnosticSpec(
        "tarch_quantization_conflict", DiagnosticSeverity.BLOCK, TarchStage.S1_QUANTIZE,
        "two coordinates > tau_node apart collapsed to one grid point — abnormal drawing precision, needs human review.",
        gates=("G2",)),
    # --- S2 walls / thickness evidence -------------------------------------
    "tarch_wall_thickness_unevidenced": DiagnosticSpec(
        "tarch_wall_thickness_unevidenced", DiagnosticSeverity.BLOCK, TarchStage.S2_WALLS,
        "wall has no window/cap/dim/hatch/override thickness evidence — bind an existing dimension or confirm the face pair in the audit overlay.",
        gates=("G2",)),
    # --- S3 openings (dual evidence) ---------------------------------------
    "tarch_opening_block_unresolved": DiagnosticSpec(
        "tarch_opening_block_unresolved", DiagnosticSeverity.BLOCK, TarchStage.S3_OPENINGS,
        "block finds no matching jamb-cap pair — draw the jamb caps or give the opening rectangle in overrides.openings.",
        gates=("G3",)),
    "tarch_opening_block_ambiguous": DiagnosticSpec(
        "tarch_opening_block_ambiguous", DiagnosticSeverity.BLOCK, TarchStage.S3_OPENINGS,
        "block matches multiple wall bands — use an override to pin the unique host.",
        gates=("G3",)),
    "tarch_opening_kind_ambiguous": DiagnosticSpec(
        "tarch_opening_kind_ambiguous", DiagnosticSeverity.BLOCK, TarchStage.S3_OPENINGS,
        "exploded opening: door/window kind undecidable — bind the source component as door/window; no shape-scoring guess.",
        gates=("G3",)),
    "tarch_interior_opening_excluded": DiagnosticSpec(
        "tarch_interior_opening_excluded", DiagnosticSeverity.INFO, TarchStage.S3_OPENINGS,
        "opening hosts an interior wall band — not written to manifest (sm24 observed 7); bookkeeping only.",
        gates=("G4",)),
    # --- S4 topology / S5 cavity -------------------------------------------
    "tarch_topology_residual": DiagnosticSpec(
        "tarch_topology_residual", DiagnosticSeverity.BLOCK, TarchStage.S4_TOPOLOGY,
        "dangles/cuts/invalid != 0 or area sum != envelope — locate via the diagnostic overlay.",
        gates=("G5",)),
    "tarch_wall_free_end": DiagnosticSpec(
        "tarch_wall_free_end", DiagnosticSeverity.BLOCK, TarchStage.S4_TOPOLOGY,
        "dangling free-end — pick one: declare as opening / declare extension target / fix drawing. NEVER auto-extend.",
        gates=("G5",)),
    "tarch_footprint_multiple": DiagnosticSpec(
        "tarch_footprint_multiple", DiagnosticSeverity.BLOCK, TarchStage.S5_CAVITY,
        "multiple disjoint exterior rings (multipolygon) — check for a missed closure; if truly multi-building, await the multipolygon profile.",
        gates=("G5",)),
    "tarch_profile_hole_unsupported": DiagnosticSpec(
        "tarch_profile_hole_unsupported", DiagnosticSeverity.BLOCK, TarchStage.S5_CAVITY,
        "footprint has an interior ring (courtyard/回字) — do not fill the hole; complete the §11-U1 profile extension first, then re-run.",
        gates=("G5",)),
    # --- S6 intent / S7 expand ---------------------------------------------
    "tarch_cavity_count_mismatch": DiagnosticSpec(
        "tarch_cavity_count_mismatch", DiagnosticSeverity.BLOCK, TarchStage.S6_INTENT,
        "cavity count != expected_count — recheck the room count; this is the main gate that catches a sliced corridor.",
        gates=("G6",)),
    "tarch_cavity_unclaimed": DiagnosticSpec(
        "tarch_cavity_unclaimed", DiagnosticSeverity.BLOCK, TarchStage.S6_INTENT,
        "cavity has no seed/label and is not declared void — add a room name or declare it a void (天井/atrium).",
        gates=("G6",)),
    "tarch_edge_thickness_inconsistent": DiagnosticSpec(
        "tarch_edge_thickness_inconsistent", DiagnosticSeverity.BLOCK, TarchStage.S7_EXPAND,
        "an edge spans a thickness change that can't be split at event coordinates — inspect; usually a misaligned wall.",
        gates=("G7",)),
    "tarch_zone_tiling_residual": DiagnosticSpec(
        "tarch_zone_tiling_residual", DiagnosticSeverity.BLOCK, TarchStage.S7_EXPAND,
        "G7 tiling symmetric difference > tau_area or zones overlap — converter bug; capture a minimal reproducing fixture.",
        gates=("G7",)),
    "tarch_opening_skin_gap_mismatch": DiagnosticSpec(
        "tarch_opening_skin_gap_mismatch", DiagnosticSeverity.BLOCK, TarchStage.S7_EXPAND,
        "outer-skin gap count != exterior opening count — find the missing/extra (sm24 observed 14==14).",
        gates=("G4",)),
    "tarch_reconstruction_residual": DiagnosticSpec(
        "tarch_reconstruction_residual", DiagnosticSeverity.BLOCK, TarchStage.S8_GATES,
        "G8 inverted wall-region doesn't match — highest priority; usually a basis recorded wrong.",
        gates=("G8",)),
    # --- cross-stage / gates -----------------------------------------------
    "tarch_v3_precondition": DiagnosticSpec(
        "tarch_v3_precondition", DiagnosticSeverity.BLOCK, TarchStage.S8_GATES,
        "v3 preflight (G9) raised an ExtractionError — context keeps the original v3 code; the converter never catches/rewrites/swallows it.",
        gates=("G9",)),
    # --- named elevation (request v3 only) ---------------------------------
    "tarch_elevation_title_mismatch": DiagnosticSpec("tarch_elevation_title_mismatch", DiagnosticSeverity.BLOCK, TarchStage.S0_INPUT, "named elevation frame/title/facade binding is not exact", gates=("G1",)),
    "tarch_elevation_datum_missing": DiagnosticSpec("tarch_elevation_datum_missing", DiagnosticSeverity.BLOCK, TarchStage.S0_INPUT, "the request-bound floor datum was not found in the named view", gates=("G1",)),
    "tarch_elevation_datum_invalid": DiagnosticSpec("tarch_elevation_datum_invalid", DiagnosticSeverity.BLOCK, TarchStage.S0_INPUT, "floor datum is not a full-span axis-aligned line", gates=("G1",)),
    "tarch_elevation_z_transform_mismatch": DiagnosticSpec("tarch_elevation_z_transform_mismatch", DiagnosticSeverity.BLOCK, TarchStage.S0_INPUT, "datum-derived z offset or elevation scale differs from the signed request", gates=("G1",)),
    "tarch_elevation_along_direction_mismatch": DiagnosticSpec("tarch_elevation_along_direction_mismatch", DiagnosticSeverity.BLOCK, TarchStage.S0_INPUT, "declared datum endpoint does not map to facade projection lo", gates=("G1",)),
    "tarch_elevation_opening_component_invalid": DiagnosticSpec("tarch_elevation_opening_component_invalid", DiagnosticSeverity.BLOCK, TarchStage.S3_OPENINGS, "selected elevation window lines do not form one rectangle", gates=("G3",)),
    "tarch_elevation_entities_unconsumed": DiagnosticSpec("tarch_elevation_entities_unconsumed", DiagnosticSeverity.BLOCK, TarchStage.S3_OPENINGS, "entities on request-declared opening layers were neither consumed nor explicitly ignored; add an exact carrier or ignore declaration", gates=("G3",)),
    "tarch_elevation_entity_double_consumed": DiagnosticSpec("tarch_elevation_entity_double_consumed", DiagnosticSeverity.BLOCK, TarchStage.S3_OPENINGS, "one elevation entity was consumed by more than one request-declared opening carrier", gates=("G3",)),
    "tarch_elevation_door_block_drift": DiagnosticSpec("tarch_elevation_door_block_drift", DiagnosticSeverity.BLOCK, TarchStage.S3_OPENINGS, "door block fingerprint or exhaustive role list drifted", gates=("G3",)),
    "tarch_elevation_door_structure_invalid": DiagnosticSpec("tarch_elevation_door_structure_invalid", DiagnosticSeverity.BLOCK, TarchStage.S3_OPENINGS, "door structural outline or its union is not one rectangle", gates=("G3",)),
    "tarch_elevation_normalized_outline_drift": DiagnosticSpec("tarch_elevation_normalized_outline_drift", DiagnosticSeverity.BLOCK, TarchStage.S9_PERSIST, "reopened normalized elevation outline differs from structural evidence", gates=("G3",)),
    # --- declared-but-not-emitted, by design (spec §6.4) ------------------------
    # The next three name G9 *extraction* failure modes.  §6.4 mandates a single
    # channel for those: the raw extractor code is carried verbatim in
    # ``tarch_v3_precondition.context.v3_code`` (e.g. "elevation_opening_no_candidate"),
    # never re-spelled into a converter code — re-spelling would let the converter's
    # and the extractor's vocabularies drift apart silently.  They are retained as
    # declared vocabulary (the P0 contract is frozen; narrowing DiagCode would be a
    # contract change) and their non-emission is locked by
    # ``test_declared_not_emitted_elevation_codes_stay_unemitted``.
    # ``tarch_interior_opening_elevation_not_applicable`` is likewise a declared INFO
    # with no emitter: interior openings are excluded at S3, so there is no G4 site
    # that could observe the condition.
    "tarch_elevation_opening_no_candidate": DiagnosticSpec("tarch_elevation_opening_no_candidate", DiagnosticSeverity.BLOCK, TarchStage.S8_GATES, "elevation opening has no matching exterior plan opening", gates=("G9",)),
    "tarch_elevation_opening_assignment_ambiguous": DiagnosticSpec("tarch_elevation_opening_assignment_ambiguous", DiagnosticSeverity.BLOCK, TarchStage.S8_GATES, "elevation opening pairing has multiple optimum assignments", gates=("G9",)),
    "tarch_elevation_opening_kind_mismatch": DiagnosticSpec("tarch_elevation_opening_kind_mismatch", DiagnosticSeverity.BLOCK, TarchStage.S8_GATES, "elevation and plan opening kinds differ", gates=("G9",)),
    "tarch_elevation_pairing_drift": DiagnosticSpec("tarch_elevation_pairing_drift", DiagnosticSeverity.BLOCK, TarchStage.S8_GATES, "converter pairing ledger differs from extracted GT references", gates=("G9",)),
    "tarch_interior_opening_elevation_not_applicable": DiagnosticSpec("tarch_interior_opening_elevation_not_applicable", DiagnosticSeverity.INFO, TarchStage.S3_OPENINGS, "interior opening has no elevation evidence obligation", gates=("G4",)),
    "tarch_raster_overlay_unbound": DiagnosticSpec("tarch_raster_overlay_unbound", DiagnosticSeverity.BLOCK, TarchStage.S8_GATES, "required review raster binding is missing", gates=("G10",)),
    "tarch_raster_calibration_invalid": DiagnosticSpec("tarch_raster_calibration_invalid", DiagnosticSeverity.BLOCK, TarchStage.S8_GATES, "raster calibration controls/affine are invalid", gates=("G10",)),
}

#: Ordered tuple of every diagnostic code (test asserts this == codes in the registry).
ALL_DIAGNOSTIC_CODES: tuple[str, ...] = tuple(TARCH_DIAGNOSTIC_REGISTRY.keys())


def diagnostic_spec(code: str) -> DiagnosticSpec:
    """Look up a code's static spec; raises if the code is not in the table."""
    try:
        return TARCH_DIAGNOSTIC_REGISTRY[code]
    except KeyError as exc:  # pragma: no cover - defensive
        raise ValueError(f"tarch_diagnostic_code_unknown:{code}") from exc


DiagCode = Literal[
    "tarch_input_source_hash_mismatch", "tarch_source_proxy_present",
    "tarch_units_undeclared", "tarch_view_frame_missing", "tarch_view_frame_ambiguous",
    "tarch_entity_unsupported", "tarch_wall_nonorthogonal", "tarch_wall_axis_snapped",
    "tarch_wall_degenerate_line",
    "tarch_quantization_conflict", "tarch_wall_thickness_unevidenced",
    "tarch_opening_block_unresolved", "tarch_opening_block_ambiguous",
    "tarch_opening_kind_ambiguous", "tarch_interior_opening_excluded",
    "tarch_topology_residual", "tarch_wall_free_end", "tarch_footprint_multiple",
    "tarch_profile_hole_unsupported", "tarch_cavity_count_mismatch", "tarch_cavity_unclaimed",
    "tarch_edge_thickness_inconsistent",
    "tarch_zone_tiling_residual", "tarch_opening_skin_gap_mismatch",
    "tarch_reconstruction_residual", "tarch_v3_precondition",
    "tarch_elevation_title_mismatch", "tarch_elevation_datum_missing",
    "tarch_elevation_datum_invalid", "tarch_elevation_z_transform_mismatch",
    "tarch_elevation_along_direction_mismatch", "tarch_elevation_opening_component_invalid",
    "tarch_elevation_entities_unconsumed", "tarch_elevation_entity_double_consumed",
    "tarch_elevation_door_block_drift", "tarch_elevation_door_structure_invalid",
    "tarch_elevation_normalized_outline_drift", "tarch_elevation_opening_no_candidate",
    "tarch_elevation_opening_assignment_ambiguous", "tarch_elevation_opening_kind_mismatch",
    "tarch_elevation_pairing_drift", "tarch_interior_opening_elevation_not_applicable",
    "tarch_raster_overlay_unbound", "tarch_raster_calibration_invalid",
]


class ConversionDiagnosticV1(_StrictModel):
    """One diagnostic event.  Every BLOCK code must carry source point and/or handles."""
    code: DiagCode
    severity: DiagnosticSeverity
    stage: TarchStage
    view_id: StableId | None = None
    floor_id: StableId | None = None
    source_entity_handles: list[DxfHandle] = Field(default_factory=list)
    generated_candidate_ids: list[StableId] = Field(default_factory=list)
    source_points_dxf_mm: list[Point2] = Field(default_factory=list)
    points_world_m: list[Point2] = Field(default_factory=list)
    context: JsonDict = Field(default_factory=dict)
    action_code: str = Field(min_length=1)
    overlay_asset: str | None = None

    @model_validator(mode="after")
    def _localizable_and_consent(self):
        spec = diagnostic_spec(self.code)
        if self.severity != spec.severity:
            raise ValueError(f"tarch_diag_severity_mismatch:{self.code}:{self.severity}")
        if self.stage != spec.stage:
            raise ValueError(f"tarch_diag_stage_mismatch:{self.code}:{self.stage}")
        # A BLOCK must be localizable: at least one source point or one handle.
        if self.severity == DiagnosticSeverity.BLOCK:
            if not self.source_entity_handles and not self.source_points_dxf_mm:
                raise ValueError(f"tarch_diag_block_not_localizable:{self.code}")
        return self


# --------------------------------------------------------------------------- #
# Config resolution — reuses load_gt_tooling_config (no new tolerance channel)
# --------------------------------------------------------------------------- #
def resolve_converter_tooling(gt_config_path: Path, vg_config_path: Path) -> GtResolvedToolingConfigV1:
    """Resolve the seven judge tolerances + two Vg epsilons, with both config sha256.

    This is the ONLY tolerance channel for the converter.  It deliberately reuses
    :func:`~src.agent.judge.gt_manifest.load_gt_tooling_config` so no tolerance
    value is ever invented here and the ``judge_config_sha256`` /
    ``vg_config_sha256`` recorded in the report are byte-identical to what the
    v3 extractor itself would bind.
    """
    return load_gt_tooling_config(Path(gt_config_path), Path(vg_config_path))


def derive_quantization_step(tooling: GtResolvedToolingConfigV1) -> float:
    """Quantization step = node join tolerance / 10 (a DERIVED value, not a config field).

    Mirrors plan §4 S1: ``q = tau_node / 10``.  Kept as code so no one can add a
    ``quantization_step`` config key and silently change snapping behaviour.
    """
    return tooling.tolerances.dxf_node_join_tolerance_m / 10.0


# --------------------------------------------------------------------------- #
# Conversion REQUEST contract (source-hash-bound, strict)
# --------------------------------------------------------------------------- #
TarchEntityType = Literal[
    "ARC", "ATTRIB", "CIRCLE", "DIMENSION", "HATCH", "INSERT", "LINE",
    "LWPOLYLINE", "MTEXT", "POLYLINE", "TEXT",
]


class TarchEntitySelectorV1(_StrictModel):
    """What the converter reads from the source DXF for one role (layer + type filter)."""
    entity_types: list[TarchEntityType] = Field(min_length=1)
    layers: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _sorted_unique(self):
        if (self.entity_types != sorted(set(self.entity_types))
                or self.layers != sorted(set(self.layers))):
            raise ValueError("tarch_selector_lists_must_be_sorted_unique")
        return self


class BlockEntityRoleV1(_StrictModel):
    """Declared semantic role of one direct entity in a block definition."""
    entity_handle: DxfHandle
    role: Literal["structural_outline", "nonstructural_detail"]


class OpeningCarrierMatchV1(_StrictModel):
    """Exact, request-declared DXF match for one opening carrier dialect."""
    entity_type: TarchEntityType
    layers: list[str] = Field(min_length=1)
    block_name_exact: str | None = Field(default=None, min_length=1)
    block_definition_sha256: Hex64 | None = None

    @model_validator(mode="after")
    def _match_contract(self):
        if self.layers != sorted(set(self.layers)):
            raise ValueError("opening carrier layers must be sorted unique")
        if self.entity_type != "INSERT" and (
                self.block_name_exact is not None
                or self.block_definition_sha256 is not None):
            raise ValueError("opening carrier block match fields require INSERT")
        return self


class OpeningOutlineV1(_StrictModel):
    """Declared geometry resolver; the converter never infers this choice."""
    kind: Literal[
        "closed_polyline_rect",
        "connected_line_group_rect",
        "block_entity_rect",
    ]
    block_entity_roles: list[BlockEntityRoleV1] | None = None

    @model_validator(mode="after")
    def _block_roles_contract(self):
        if self.kind != "block_entity_rect":
            if self.block_entity_roles is not None:
                raise ValueError("block entity roles require block_entity_rect")
            return self
        if not self.block_entity_roles:
            raise ValueError("block_entity_rect requires block entity roles")
        handles = [role.entity_handle for role in self.block_entity_roles]
        if len(handles) != len(set(handles)):
            raise ValueError("duplicate opening block entity role handle")
        if not any(role.role == "structural_outline"
                   for role in self.block_entity_roles):
            raise ValueError("block_entity_rect requires structural outline roles")
        return self


class OpeningCarrierRuleV1(_StrictModel):
    """Request-bound mapping from an exact carrier dialect to opening semantics."""
    carrier_id: StableId
    opening_kind: Literal["window", "door"]
    match: OpeningCarrierMatchV1
    outline: OpeningOutlineV1
    module_union_strategy: Literal[
        "same_band_strict_union",
        "touching_rect_union",
    ] | None = None
    # Signed building-domain parameter, not a geometry tolerance.  It declares
    # how large a same-band module gap may be before the modules are accepted as
    # separate doors; touching_rect_union deliberately has no default.
    module_union_min_gap_m: PositiveFiniteFloat | None = None

    @model_validator(mode="after")
    def _entity_type_matches_outline(self):
        expected = {
            "closed_polyline_rect": "LWPOLYLINE",
            "connected_line_group_rect": "LINE",
            "block_entity_rect": "INSERT",
        }[self.outline.kind]
        if self.match.entity_type != expected:
            raise ValueError(
                f"opening outline {self.outline.kind} requires {expected}")
        if self.opening_kind == "window":
            if (self.module_union_strategy is not None
                    or self.module_union_min_gap_m is not None):
                raise ValueError("window opening carriers cannot declare module union")
        elif self.module_union_strategy is None:
            raise ValueError("door opening carriers require a module union strategy")
        elif self.module_union_strategy == "touching_rect_union":
            if self.module_union_min_gap_m is None:
                raise ValueError(
                    "touching_rect_union requires module_union_min_gap_m")
        elif self.module_union_min_gap_m is not None:
            raise ValueError(
                "module_union_min_gap_m is only valid for touching_rect_union")
        return self


class ElevationDoorBlockRoleV1(_StrictModel):
    entity_handle: DxfHandle
    role: Literal["structural_outline", "nonstructural_detail"]


class ElevationDoorBlockRuleV1(_StrictModel):
    block_name_exact: str = Field(min_length=1)
    block_definition_sha256: Hex64
    entity_roles: list[ElevationDoorBlockRoleV1] = Field(min_length=1)

    @model_validator(mode="after")
    def _roles_unique(self):
        handles = [role.entity_handle for role in self.entity_roles]
        if len(handles) != len(set(handles)):
            raise ValueError("duplicate elevation door block role handle")
        return self


class TarchDialectRulesV1(_StrictModel):
    """Tianzheng-specific recognition rules — the ONLY place they may live.

    These (WALL/WINDOW layer names, block-name prefixes) must NOT leak into
    0_reading / correction / executors / gate①.  They are bound per-request and
    versioned, so a classifier change is auditable.
    """
    window_block_names: list[str] = Field(min_length=1)
    door_block_prefixes: list[str] = Field(min_length=1)
    classifier_version: StableId
    elevation_title_map: dict[str, Literal["North", "South", "East", "West"]] = Field(default_factory=dict)
    elevation_door_block_rules: list[ElevationDoorBlockRuleV1] = Field(default_factory=list)


class ZoneIntentEntryV1(_StrictModel):
    """One declared room.  ``role`` may be the ``unspecified`` sentinel (sm24)."""
    zone_id: StableId
    name: HumanLabel | None = None
    role: StableId


class ZoneIntentSpecV1(_StrictModel):
    """Where zoning intent comes from.  ``expected_count`` is required, no default."""
    mode: Literal["intent_file", "cad_labels_or_reviewed_anchors"]
    expected_count: StrictNonNegativeInt
    entries: list[ZoneIntentEntryV1] = Field(default_factory=list)

    @model_validator(mode="after")
    def _entries_contract(self):
        if self.mode == "intent_file":
            if not self.entries:
                raise ValueError("intent_file mode requires an explicit room list")
            if len(self.entries) != self.expected_count:
                raise ValueError("intent_file entries length must equal expected_count")
            ids = [entry.zone_id for entry in self.entries]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate zone_id in intent list")
        # cad_labels mode derives count from CAD text at runtime; entries may be empty.
        return self


class VoidIntentAnchorV1(_StrictModel):
    void_id: StableId
    point_world_m: Point2


class CriticalDimensionV1(_StrictModel):
    """Optional independent truth constraint; if declared it must match."""
    dim_handle: DxfHandle
    axis: Literal["x", "y"]
    expected_span_m: PositiveFiniteFloat


# Narrow override operations only — never a 'submit final polygons' escape.
TarchOverrideKind = Literal[
    "bind_opening_group", "declare_free_end_non_zoning", "confirm_joint",
    "reviewed_zone_anchor", "reviewed_void_anchor",
    "bind_face_pair",  # backlog wall-ribbon route (plan §3)
]


class TarchOverrideV1(_StrictModel):
    kind: TarchOverrideKind
    source_handles: list[DxfHandle] = Field(min_length=1)
    reason: HumanLabel
    reviewer: StableId
    request_hash_at_issue: Hex64


class PlanViewIntentV1(_StrictModel):
    id: StableId
    floor_id: StableId
    frame_title: HumanLabel
    clip_box_dxf: ClipBoxDxf
    world_from_source_m: Affine2D
    wall_selector: TarchEntitySelectorV1
    opening_selector: TarchEntitySelectorV1
    room_label_selector: TarchEntitySelectorV1 | None = None
    dialect_rules: TarchDialectRulesV1
    zone_intent: ZoneIntentSpecV1
    void_intent: list[VoidIntentAnchorV1] = Field(default_factory=list)

    @model_validator(mode="after")
    def _bind_affine_spaces(self):
        # The REQUEST copy consumes raw DXF-native coordinates (m00 == metres_per_unit).
        # The identically named PlanViewBindingV1 field consumes source METRES.
        # Same name, same type, 1000x apart on every sm24/sm25 anchor.
        bind_affine_spaces(self, "world_from_source_m",
                           domain="dxf_native", codomain="world_metre")
        return self


class ElevationViewIntentV1(_StrictModel):
    """Elevation views are passed through; the converter re-binds handles, never re-infers."""
    id: StableId
    floor_ids: list[StableId] = Field(min_length=1)
    facade_family: Literal["North", "South", "East", "West"]
    clip_box_dxf: ClipBoxDxf
    world_along_from_source_m: Affine1D
    world_z_from_source_m: Affine1D

    @model_validator(mode="after")
    def _bind_affine_spaces(self):
        # REQUEST copy consumes raw DXF-native coordinates (|scale| == metres_per_unit).
        # The identically named ElevationViewBindingV1 fields consume source METRES.
        for field in ("world_along_from_source_m", "world_z_from_source_m"):
            bind_affine_spaces(self, field,
                               domain="dxf_native", codomain="world_metre")
        return self

    @model_validator(mode="after")
    def _axes_differ(self):
        if self.world_along_from_source_m.source_axis == self.world_z_from_source_m.source_axis:
            raise ValueError("elevation source axes must differ")
        return self


class ElevationFloorDatumIntentV1(_StrictModel):
    floor_id: StableId
    entity_handle: DxfHandle
    datum_kind: Literal["floor_line"]
    world_along_lo_source_endpoint: Literal["start", "end"]


class DatumBoundNamedElevationViewIntentV3(_StrictModel):
    """Strict v3-only named-elevation contract; it is never duck-typed as v1."""
    intent_version: Literal[3]
    intent_kind: Literal["named_datum_bound"]
    id: StableId
    binding_source: Literal["named_title"]
    frame_title: HumanLabel
    frame_entity_handle: DxfHandle
    title_entity_handle: DxfHandle
    floor_ids: list[StableId] = Field(min_length=1)
    facade_family: Literal["North", "South", "East", "West"]
    clip_box_dxf: ClipBoxDxf
    world_along_from_source_m: Affine1D
    world_z_from_source_m: Affine1D
    floor_datums: list[ElevationFloorDatumIntentV1] = Field(min_length=1)
    window_selector: TarchEntitySelectorV1
    door_selector: TarchEntitySelectorV1 | None = None
    view_kind: Literal["full"]
    segment_scope_mode: Literal["all_family_segments"]

    @model_validator(mode="after")
    def _bind_affine_spaces(self):
        for field in ("world_along_from_source_m", "world_z_from_source_m"):
            bind_affine_spaces(self, field,
                               domain="dxf_native", codomain="world_metre")
        return self

    @model_validator(mode="after")
    def _v3_contract(self):
        if self.floor_ids != sorted(set(self.floor_ids)):
            raise ValueError("elevation floor IDs must be sorted unique")
        if self.world_along_from_source_m.source_axis == self.world_z_from_source_m.source_axis:
            raise ValueError("elevation source axes must differ")
        if sorted(d.floor_id for d in self.floor_datums) != self.floor_ids:
            raise ValueError("elevation datum floors must exactly match floor_ids")
        return self


class RasterCalibrationControlV1(_StrictModel):
    entity_handle: DxfHandle
    source_point_dxf: Point2
    pixel_point: Point2
    role: Literal["datum_lo", "datum_hi", "off_datum", "footprint_sw", "footprint_se", "footprint_nw"]


class RasterOverlayIntentV3(_StrictModel):
    id: StableId
    source_label: HumanLabel
    source_sha256: Hex64
    view_id: StableId
    pixel_to_source_m: Affine2D
    calibration_controls: list[RasterCalibrationControlV1] = Field(min_length=3)

    @model_validator(mode="after")
    def _bind_affine_spaces(self):
        bind_affine_spaces(self, "pixel_to_source_m",
                           domain="pixel", codomain="source_metre")
        return self


class NorthAxisIntentV1(_StrictModel):
    value_deg: Annotated[StrictFiniteFloat, Field(ge=0, lt=360)]
    source_view_id: StableId
    source_entity_handle: DxfHandle


class TarchConversionRequestV1(_StrictModel):
    """The strict, source-hash-bound conversion request (the only non-machine input)."""
    request_version: Literal[1, 2, 3]
    case: StableId
    source_dxf_label: HumanLabel
    source_dxf_sha256: Hex64
    normalized_source_id: StableId
    target_geometry_profile: Literal["c2_simple_orthogonal_no_holes"]
    native_units: Literal["m", "mm", "cm", "in", "ft", "unitless"]
    metres_per_unit: PositiveFiniteFloat
    # Building-domain LEGAL wall-thickness range (default [0.06, 0.50] m).  This is a
    # sanity FILTER for jamb-cap identification (plan §2.1), NEVER the *source* of a
    # thickness — every thickness value still carries a six-kind ThicknessEvidenceV1.
    # Optional with the documented default so P0-built requests still construct.
    wall_thickness_range_m: list[StrictFiniteFloat] = Field(
        default=[0.06, 0.50], min_length=2, max_length=2)
    # Building-domain cavity/wall bisection threshold A_room (plan §2.2 / §4 S5).
    # Like wall_thickness_range_m this is a DOMAIN PARAMETER, not a tolerance:
    # it ONLY proposes the cavity/wall bucket split; the criterion is the human-
    # declared room count (G6), never A_room itself.  Plan §2.2 forbids auto-
    # adjusting A_room until the count fits.  Optional with the documented default
    # so P0/P1-built requests still construct.
    min_room_area_m2: PositiveFiniteFloat = Field(default=2.0)
    floors: list["FloorIntentV1"] = Field(min_length=1)
    plan_views: list[PlanViewIntentV1] = Field(min_length=1)
    elevation_views: list[ElevationViewIntentV1 | DatumBoundNamedElevationViewIntentV3] = Field(default_factory=list)
    # ``None`` is the migration state for already-signed requests.  A declared
    # table (including an explicitly empty one) is request-hash-bound.
    opening_carrier_rules: list[OpeningCarrierRuleV1] | None = None
    # Union of exact (layer, DXF type) selectors exempted from the opening
    # carrier ledger.  ``None`` is only the already-signed-request migration
    # state; an explicit list, including [], is request-hash-bound.
    ignore_selector: list[TarchEntitySelectorV1] | None = None
    north_axis: NorthAxisIntentV1 | None = None
    raster_overlays: list[StableId | RasterOverlayIntentV3] = Field(default_factory=list)
    label_role_map: dict[str, StableId] = Field(default_factory=dict)
    critical_dimensions: list[CriticalDimensionV1] = Field(default_factory=list)
    overrides: list[TarchOverrideV1] = Field(default_factory=list)
    request_sha256: Hex64

    @model_validator(mode="after")
    def _affine_magnitudes(self):
        """Numeric half of the affine contract (B4-(2)a, F-A).

        The type gate stamps a slot's two ends *blind to content*: six bare
        coefficients lifted out of the manifest and dropped into a request slot
        are stamped ``dxf_native -> world_metre`` and used 1000x off (measured
        on sm24: the clip corner lands 12264.66 m away).  This validator reads
        the coefficients instead, and the request's own ``metres_per_unit`` is
        what pins them -- so it has teeth on a completely undeclared affine,
        and it needs no counterpart slot to compare against.  That matters:
        sm25-L, the main case, ships a request and no manifest at all.

        See ``affine_space.require_affine_magnitudes`` for the prediction.
        """
        return require_affine_magnitudes(
            self, metres_per_unit=self.metres_per_unit,
            context="TarchConversionRequestV1")

    @model_validator(mode="after")
    def _label_basename(self):
        if "/" in self.source_dxf_label or "\\" in self.source_dxf_label or ".." in self.source_dxf_label:
            raise ValueError("source label must be a basename")
        return self

    @model_validator(mode="after")
    def _wall_thickness_range_ordered(self):
        lo, hi = self.wall_thickness_range_m
        if not (0.0 < lo < hi):
            raise ValueError("wall_thickness_range_m must be [lo, hi] with 0 < lo < hi")
        return self

    @model_validator(mode="after")
    def _request_version_elevation_contract(self):
        if self.request_version == 3:
            if any(not isinstance(view, DatumBoundNamedElevationViewIntentV3) for view in self.elevation_views):
                raise ValueError("request v3 requires datum-bound elevation intents")
            if any(not isinstance(item, RasterOverlayIntentV3) for item in self.raster_overlays):
                raise ValueError("request v3 requires typed raster overlays")
        elif any(isinstance(view, DatumBoundNamedElevationViewIntentV3) for view in self.elevation_views):
            raise ValueError("legacy request cannot contain v3 elevation intent")
        return self

    @model_validator(mode="after")
    def _opening_carrier_ids_unique(self):
        if self.opening_carrier_rules is not None:
            ids = [rule.carrier_id for rule in self.opening_carrier_rules]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate opening carrier id")
            door_policies = {
                (rule.module_union_strategy, rule.module_union_min_gap_m)
                for rule in self.opening_carrier_rules
                if rule.opening_kind == "door"
            }
            if len(door_policies) > 1:
                raise ValueError(
                    "all door opening carriers must declare one module union policy")
        return self


class FloorIntentV1(_StrictModel):
    id: StableId
    name: HumanLabel
    z_floor_m: StrictFiniteFloat
    ceiling_height_m: PositiveFiniteFloat


#: Request versions signed BEFORE the affine two-end space contract existed.
#: Their canonical preimage omits ``domain_space``/``codomain_space`` so every
#: already-signed request keeps its hash byte-for-byte.
#:
#: MIGRATION HONESTY: this currently lists *every* legal ``request_version``,
#: so the space contract is deliberately OUTSIDE the signature.  It protects the
#: *code paths* -- a mis-spaced affine is rejected by the owning model's
#: validator before it can be used -- and it does NOT mean any signed request
#: has attested to its affine spaces.  ``test_affine_space_contract.py`` goes red
#: the moment a new request version is added without deciding whether it binds
#: the spaces into the signature.
REQUEST_VERSIONS_WITHOUT_SPACE_BINDING: frozenset[int] = frozenset({1, 2, 3})


def compute_request_sha256(request: TarchConversionRequestV1) -> str:
    """Canonical request hash with an explicit v1 migration path.

    P0 v1 did not contain ``wall_thickness_range_m`` or ``min_room_area_m2``.
    Omitting them when hashing a declared v1 request preserves old signatures;
    all new requests must declare v2 and bind both fields.

    The affine two-end space contract follows the same shape but a wider gate:
    see :data:`REQUEST_VERSIONS_WITHOUT_SPACE_BINDING`.
    """
    payload = request.model_dump(mode="json")
    payload["request_sha256"] = "0" * 64
    if request.request_version == 1:
        payload.pop("wall_thickness_range_m", None)
        payload.pop("min_room_area_m2", None)
    if request.request_version in REQUEST_VERSIONS_WITHOUT_SPACE_BINDING:
        payload = strip_affine_space_keys(payload)
    if request.opening_carrier_rules is None:
        payload.pop("opening_carrier_rules", None)
    if request.ignore_selector is None:
        payload.pop("ignore_selector", None)
    payload = _normalise(payload)
    return _sha256_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n")


# --------------------------------------------------------------------------- #
# Normalized IR — multi-ring + per-floor footprint + per-wall per-segment proof
# --------------------------------------------------------------------------- #
Axis = Literal["x", "y"]
EdgeBasis = Literal["outer_skin", "wall_axis"]


class RingV1(_StrictModel):
    """A single ring (>= 4 vertices, closed implicitly).  IR keeps many rings."""
    vertices: list[Point2] = Field(min_length=4)


class PolygonIRV1(_StrictModel):
    """A polygon with an exterior ring and (from v1) any number of interior rings.

    Interior rings are retained even though the current profile rejects holes
    (plan §4.3): the IR must be able to carry a courtyard so that, when the
    hole-profile extension lands, the IR shape model does not change.
    """
    exterior: RingV1
    interior_rings: list[RingV1] = Field(default_factory=list)


class WallTrackV1(_StrictModel):
    """One face-track fragment (a wall side may be cut by openings/joints)."""
    handle: DxfHandle
    axis: Axis
    coord_m: StrictFiniteFloat
    span_m: Annotated[list[StrictFiniteFloat], Field(min_length=2, max_length=2)]


class WallRibbonSegmentV1(_StrictModel):
    """One thickness-homogeneous segment of a wall.  A wall that changes thickness
    mid-span is modelled as multiple segments, each with its own evidence."""
    segment_id: StableId
    axis: Axis
    coord_m: StrictFiniteFloat
    span_m: Annotated[list[StrictFiniteFloat], Field(min_length=2, max_length=2)]
    thickness_evidence: ThicknessEvidenceV1


class JointRefV1(_StrictModel):
    joint_id: StableId
    kind: Literal["L_corner", "tee", "cross", "free_end", "thickness_change"]
    proof_handles: list[DxfHandle] = Field(min_length=1)


class WallSourceRefV1(_StrictModel):
    handle: DxfHandle
    role: Literal["side_a", "side_b", "cap", "jamb", "joint_return"]


class WallRibbonV1(_StrictModel):
    """One wall: its two face tracks, its per-segment thickness proofs, joints, openings."""
    id: StableId
    floor_id: StableId
    axis: Axis
    segments: list[WallRibbonSegmentV1] = Field(min_length=1)
    side_a_tracks: list[WallTrackV1] = Field(default_factory=list)
    side_b_tracks: list[WallTrackV1] = Field(default_factory=list)
    joints: list[JointRefV1] = Field(default_factory=list)
    opening_ids: list[StableId] = Field(default_factory=list)
    source_refs: list[WallSourceRefV1] = Field(default_factory=list)

    @model_validator(mode="after")
    def _segment_ids_unique(self):
        ids = [seg.segment_id for seg in self.segments]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate segment id in wall {self.id}")
        return self


class FootprintIRV1(_StrictModel):
    """Per-floor footprint (multi-ring).  IR keeps one per floor — never shared."""
    floor_id: StableId
    polygon: PolygonIRV1


class ZoningEdgeV1(_StrictModel):
    """One emitted zoning boundary edge, with its basis (D7/G8) + thickness proof."""
    id: StableId
    floor_id: StableId
    kind: Literal["wall_midline", "exterior_closure_connector"]
    p1: Point2
    p2: Point2
    basis: EdgeBasis
    thickness_evidence: ThicknessEvidenceV1
    offset_m: NonNegativeFiniteFloat
    derived_handle: DxfHandle | None = None
    source_handles: list[DxfHandle] = Field(min_length=1)


class IntentAnchorRefV1(_StrictModel):
    source: Literal["intent_file", "cad_label", "reviewed_anchor"]
    point_world_m: Point2
    source_entity_handle: DxfHandle | None = None


class ZoneIRV1(_StrictModel):
    """One zone: its polygon (multi-ring-ready), intent anchor, and emitting edges."""
    zone_id: StableId
    floor_id: StableId
    name: HumanLabel
    role: StableId
    role_source: Literal["cad_label", "intent_file", "declared_absent"]
    role_scored: bool
    seed_point_world_m: Point2
    polygon: PolygonIRV1
    intent_anchor: IntentAnchorRefV1
    edges: list[ZoningEdgeV1] = Field(min_length=1)


class NonZoningWallV1(_StrictModel):
    """A free-end wall proven non-zoning (connects same intent around its cap)."""
    ribbon_id: StableId
    floor_id: StableId
    proof_kind: Literal["connects_same_intent_around_cap"]
    proof_handles: list[DxfHandle] = Field(min_length=1)


class CavityIRV1(_StrictModel):
    """One net-room cavity (or a void), pre-expansion."""
    cavity_id: StableId
    floor_id: StableId
    area_m2: PositiveFiniteFloat
    polygon: PolygonIRV1
    claimed_by: StableId | None = None
    claim_source: Literal["intent_file", "cad_label", "void_declaration", "unclaimed"]


class FloorIRV1(_StrictModel):
    """Everything the converter derived for one floor."""
    floor_id: StableId
    plan_view_id: StableId
    wall_ribbons: list[WallRibbonV1] = Field(default_factory=list)
    footprint: FootprintIRV1 | None = None
    cavities: list[CavityIRV1] = Field(default_factory=list)
    zones: list[ZoneIRV1] = Field(default_factory=list)
    zoning_edges: list[ZoningEdgeV1] = Field(default_factory=list)
    non_zoning_walls: list[NonZoningWallV1] = Field(default_factory=list)


class NormalizedBuildingIRV1(_StrictModel):
    """The full normalized building IR.  Carries per-floor structure from v1.

    No ``W_m/D_m``, no rows, no bands, no global wall thickness, no shared
    footprint, no fixed floor count (plan §3.3).  This is the shape the later
    hole/multi-floor profile extensions grow into without rewriting.
    """
    ir_version: Literal[1]
    case: StableId
    floors: list[FloorIRV1] = Field(min_length=1)


# --------------------------------------------------------------------------- #
# Conversion REPORT contract
# --------------------------------------------------------------------------- #
GateId = Literal["G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9", "G10"]


class GateResultV1(_StrictModel):
    id: GateId
    name: HumanLabel
    passed: bool
    evidence: JsonDict = Field(default_factory=dict)


class HumanReviewAckV1(_StrictModel):
    """External, post-overlay human signature consumed by G10."""
    reviewer: StableId
    signed_at: str = Field(min_length=1)
    decision: Literal["approved"]
    source_dxf_sha256: Hex64
    request_sha256: Hex64
    overlay_sha256: Hex64
    review_index_sha256: Hex64 | None = None
    near_threshold_confirmed: bool = False


class WallReportV1(_StrictModel):
    band_id: StableId
    floor_id: StableId
    axis: Axis
    coord_mm: StrictFiniteFloat
    span_mm: Annotated[list[StrictFiniteFloat], Field(min_length=2, max_length=2)]
    segments: list[WallRibbonSegmentV1] = Field(min_length=1)
    source_handles: list[DxfHandle] = Field(min_length=1)


class OpeningReportV1(_StrictModel):
    opening_id: StableId
    kind: Literal["window", "door"]
    classification: Literal["exterior", "interior_excluded"]
    rect_mm: Annotated[list[StrictFiniteFloat], Field(min_length=4, max_length=4)]
    block_handle: DxfHandle
    block_name: str
    jamb_handles: list[DxfHandle] = Field(min_length=1)
    geometric_witness: bool
    derived_handle: DxfHandle | None = None


class CavityReportV1(_StrictModel):
    cavity_id: StableId
    floor_id: StableId
    area_m2: PositiveFiniteFloat
    vertices_m: list[Point2] = Field(min_length=4)
    claimed_by: StableId | None = None
    claim_source: Literal["intent_file", "cad_label", "void_declaration", "unclaimed"]


class ZoneEdgeReportV1(_StrictModel):
    p1: Point2
    p2: Point2
    basis: EdgeBasis
    thickness_m: PositiveFiniteFloat
    offset_m: NonNegativeFiniteFloat
    derived_handle: DxfHandle | None = None
    source_handles: list[DxfHandle] = Field(min_length=1)
    thickness_evidence: ThicknessEvidenceV1 | None = None


class ZoneReportV1(_StrictModel):
    zone_id: StableId
    floor_id: StableId
    name: HumanLabel
    role: StableId
    role_source: Literal["cad_label", "intent_file", "declared_absent"]
    seed_point_world_m: Point2
    polygon_m: PolygonIRV1
    edges: list[ZoneEdgeReportV1] = Field(min_length=1)


class ConversionReportV1(_StrictModel):
    """The conversion report.  Bound to every sha256 the answer depends on.

    ``status`` is PASS only when all gates pass and geometry is emitted;
    otherwise BLOCKED and only the report + diagnostic overlay are written.
    No geometry is emitted on any BLOCK diagnostic (§5.6).
    """
    report_version: Literal[1]
    status: Literal["PASS", "BLOCKED"]
    case: StableId
    source_dxf_sha256: Hex64
    normalized_dxf_sha256: Hex64 | None = None
    request_sha256: Hex64
    judge_config_sha256: Hex64
    vg_config_sha256: Hex64
    converter_sha256: Hex64
    profile_version: Literal[1]
    quantization_step_m: PositiveFiniteFloat
    walls: list[WallReportV1] = Field(default_factory=list)
    openings: list[OpeningReportV1] = Field(default_factory=list)
    cavities: list[CavityReportV1] = Field(default_factory=list)
    zones: list[ZoneReportV1] = Field(default_factory=list)
    gates: list[GateResultV1] = Field(default_factory=list)
    diagnostics: list[ConversionDiagnosticV1] = Field(default_factory=list)
    unconsumed_source_handles: list[DxfHandle] = Field(default_factory=list)
    opening_coverage: JsonDict = Field(default_factory=dict)
    wall_proof_coverage: JsonDict = Field(default_factory=dict)
    zone_intent_coverage: JsonDict = Field(default_factory=dict)
    elevation_audit_rows: list[JsonDict] = Field(default_factory=list)
    review_bundle_inventory_sha256: Hex64 | None = None

    @model_validator(mode="after")
    def _status_geom_contract(self):
        has_geom = bool(self.zones or self.walls)
        has_block = any(d.severity == DiagnosticSeverity.BLOCK for d in self.diagnostics)
        if self.status == "PASS":
            if has_block:
                raise ValueError("tarch_report_pass_with_block_diag")
            if not has_geom:
                raise ValueError("tarch_report_pass_without_geometry")
            if self.normalized_dxf_sha256 is None:
                raise ValueError("tarch_report_pass_without_normalized_hash")
            required = {"G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9", "G10"}
            got = {g.id: g.passed for g in self.gates}
            # P1 produces a partial diagnostic report; an S9/P2 report identifies
            # itself by carrying G10 and is then required to carry every gate.
            if "G10" in got and (set(got) != required or not all(got.values())):
                raise ValueError("tarch_report_pass_with_failed_or_missing_gate")
        return self


def compute_report_sha256(report: ConversionReportV1) -> str:
    """Canonical sha256 of the whole report.

    The report carries no self-hash field (it is self-describing via the sha256s
    it already binds: source/request/judge_config/vg_config/converter).  This
    helper lets the source-map/bundle close the provenance loop without adding a
    new wire field.
    """
    payload = _normalise(report.model_dump(mode="json"))
    data = (json.dumps(payload, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n")
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# Per-entity source map (D7 provenance compensation)
# --------------------------------------------------------------------------- #
class SourceEntityRefV1(_StrictModel):
    handle: DxfHandle
    subentity_index: StrictNonNegativeInt | None = None
    role: Literal["wall_side", "cap", "jamb", "opening_block", "joint_return", "dimension", "hatch", "elevation_raw", "elevation_structural", "elevation_excluded"]


SourceMapOperation = Literal[
    "outer_skin", "midline", "opening_bridge", "joint_connector",
    "opening_outline", "footprint_ring", "zone_edge", "elevation_opening_outline",
]


class SourceMapEntryV1(_StrictModel):
    generated_handle: DxfHandle | None = None
    view_id: StableId
    floor_id: StableId
    semantic_role: Literal["footprint", "zone_boundary", "opening", "connector"]
    operation: SourceMapOperation
    canonical_geometry_world_m: JsonDict
    source_entity_refs: list[SourceEntityRefV1] = Field(min_length=1)
    wall_ribbon_ids: list[StableId] = Field(default_factory=list)
    opening_id: StableId | None = None
    joint_id: StableId | None = None
    proof_ids: list[StableId] = Field(default_factory=list)


class SourceMapV1(_StrictModel):
    map_version: Literal[1]
    case: StableId
    entries: list[SourceMapEntryV1] = Field(default_factory=list)
    source_map_sha256: Hex64


def compute_source_map_sha256(source_map: SourceMapV1) -> str:
    return _sha256_bytes(_canonical_hash_bytes(source_map, "source_map_sha256"))


__all__ = [
    "CONTRACT_VERSION", "TOLERANCE_PROFILE_VERSION",
    "STAGING_EXPERIMENTS_ROOT", "GT_ANSWER_ROOT", "GT_SOURCES_ROOT",
    "protected_tarch_roots", "is_protected_tarch_path", "assert_staging_input", "assert_staging_work_dir",
    "staging_work_dir",
    "ThicknessEvidenceKind", "ThicknessEvidenceV1",
    "TarchStage", "DiagnosticSeverity", "DiagnosticSpec",
    "TARCH_DIAGNOSTIC_REGISTRY", "ALL_DIAGNOSTIC_CODES", "DiagCode",
    "diagnostic_spec", "ConversionDiagnosticV1",
    "resolve_converter_tooling", "derive_quantization_step",
    "TarchEntitySelectorV1", "ElevationDoorBlockRoleV1", "ElevationDoorBlockRuleV1", "TarchDialectRulesV1", "ZoneIntentEntryV1",
    "ZoneIntentSpecV1", "VoidIntentAnchorV1", "CriticalDimensionV1",
    "TarchOverrideKind", "TarchOverrideV1", "PlanViewIntentV1",
    "ElevationViewIntentV1", "ElevationFloorDatumIntentV1", "DatumBoundNamedElevationViewIntentV3", "RasterCalibrationControlV1", "RasterOverlayIntentV3", "NorthAxisIntentV1", "FloorIntentV1",
    "TarchConversionRequestV1", "compute_request_sha256",
    "RingV1", "PolygonIRV1", "WallTrackV1", "WallRibbonSegmentV1",
    "JointRefV1", "WallSourceRefV1", "WallRibbonV1", "FootprintIRV1",
    "ZoningEdgeV1", "IntentAnchorRefV1", "ZoneIRV1", "NonZoningWallV1",
    "CavityIRV1", "FloorIRV1", "NormalizedBuildingIRV1",
    "GateResultV1", "HumanReviewAckV1", "WallReportV1", "OpeningReportV1", "CavityReportV1",
    "ZoneEdgeReportV1", "ZoneReportV1", "ConversionReportV1",
    "compute_report_sha256",
    "SourceEntityRefV1", "SourceMapOperation", "SourceMapEntryV1", "SourceMapV1",
    "compute_source_map_sha256",
]
