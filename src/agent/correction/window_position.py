"""F-9 route② S0/S2 (2026-08-11, design v2.1 §4.4/§5.3/§6.3/§7.3/§8/§9/§10):
raw projection context, resolver artifact V2, position-decision/evidence
artifacts, the explicit legacy/new artifact-version loader registry, and the
typed §9 error-code vocabulary (S0) — plus (S2) the authoritative frame V2,
the unified projection dispatcher, and the shadow pairing decision that runs
alongside today's live model-authored `window.span` path.

S0's types are new, additive scaffolding — nothing under the "§10 S0" banner
below is called by any live production path (v2.1 §10 S0: "均先不接 live
production"). S2's types/functions ARE wired into live production (see
`src/validator/checks/correction.py::_window_position_evidence_shadow`), but
strictly as a non-blocking CROSS_CHECK observer: v2.1 §10 S2 "不得覆盖
span、不得 block" — see the "§10 S2" section below for the full contract.

Kept as its own file (not folded into `window_sources.py`, already 1200+
lines) per v2.1 §11's file-responsibility table: "projector、pairing、
hydration artifact → 建议新 correction/window_position.py，避免继续膨胀
window_sources.py".
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Annotated, Callable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, Strict, model_validator

from src.agent.correction import facade_convention
from src.agent.correction.footprint import floor_footprint_fingerprint
from src.agent.correction.schema import CorrectedGeometryV3, CorrectionDrawV3CitationV2
from src.agent.correction.window_sources import (
    ElevationSourceWindowV1,
    FiniteFloat,
    Hex64,
    PlanSourceWindowV1,
    RawReadingArtifactV1,
    SourceIntervalV1,
    SourceLocator,
    StableId,
    VerifiedWindowResolverInputs,
    WindowDirectionBindingError,
    WindowResolverInputsArtifactV1,
    canonical_sha256,
    materialize_current_ring_va_elevation_bindings,
)

_CFG = ConfigDict(extra="forbid", frozen=True, strict=True)


def _without_key(model: BaseModel, key: str) -> dict:
    data = model.model_dump(mode="json")
    data.pop(key, None)
    return data


# ---------------------------------------------------------------------------
# §9 typed error vocabulary. A WIDER category type than
# `window_sources.WindowSourceErrorCategory` (which only has
# "model_draw_error"/"input_integrity_error") — v2.1 §9 needs three more
# buckets ("upstream evidence block" / "invariant" / "deterministic
# transform") that the existing type was never meant to cover. Defined as
# its own class (not by widening the existing Literal in place) so this S0
# batch cannot change the runtime behaviour of any existing
# `WindowResolverInputError` call site — none of them are touched.
# ---------------------------------------------------------------------------

PositionEvidenceErrorCategory = Literal[
    "model_draw_error", "upstream_evidence_block", "input_integrity_error",
    "invariant", "deterministic_transform",
]

# v2.1 §9 table, restated as a mechanical code -> category map so a caller
# never has to hand-pick the category string (and risk misclassifying a
# model mistake as an invariant, or vice versa -- exactly the "category is a
# required keyword" discipline `window_sources.WindowResolverInputError`
# already established, extended to the wider vocabulary this design adds).
POSITION_EVIDENCE_ERROR_CATEGORY: dict[str, PositionEvidenceErrorCategory] = {
    "producer_window_span_populated": "model_draw_error",
    "position_evidence_authority_invalid": "model_draw_error",
    "position_evidence_pair_mismatch": "model_draw_error",
    "position_evidence_insufficient": "upstream_evidence_block",
    "projection_datum_unresolved": "upstream_evidence_block",
    "projection_scope_unresolved": "upstream_evidence_block",
    "projection_scope_ambiguous": "upstream_evidence_block",
    "source_identity_invalid": "input_integrity_error",
    "position_hydration_identity_drift": "invariant",
    "position_evidence_post_transform_mismatch": "deterministic_transform",
    "unknown_artifact_version": "input_integrity_error",
}


class WindowPositionEvidenceError(ValueError):
    """Typed, stable route② position-evidence rejection (v2.1 §9).

    Sibling of `window_sources.WindowResolverInputError`, not a replacement
    for it — S0 does not touch any existing `WindowResolverInputError` call
    site. `category` is a required keyword for the same reason that class
    requires it: a raise site that forgets to classify itself fails loudly
    instead of silently defaulting into the wrong retry/archive bucket.
    """

    def __init__(self, code: str, context: Mapping[str, object] | None = None, *, category: PositionEvidenceErrorCategory):
        self.code = code
        self.category = category
        self.context = dict(context or {})
        super().__init__(f"{code}: {self.context}")


def position_evidence_error(code: str, context: Mapping[str, object] | None = None) -> WindowPositionEvidenceError:
    """Construct a `WindowPositionEvidenceError` with its category looked up
    from `POSITION_EVIDENCE_ERROR_CATEGORY` (v2.1 §9 table), instead of
    letting each call site hand-pick a category string.
    """
    category = POSITION_EVIDENCE_ERROR_CATEGORY.get(code)
    if category is None:
        raise KeyError(f"{code!r} is not in the v2.1 §9 error vocabulary; add it to POSITION_EVIDENCE_ERROR_CATEGORY")
    return WindowPositionEvidenceError(code, context, category=category)


# ---------------------------------------------------------------------------
# §4.4 raw projection context: window-independent facade-ring facts a raw
# draw's OWN floor/cell geometry can produce before any window is hydrated.
# ---------------------------------------------------------------------------

class FloorRingFactV1(BaseModel):
    model_config = _CFG
    floor_id: StableId
    footprint_fingerprint: Hex64
    z_floor: FiniteFloat
    ceiling_height: FiniteFloat


class RawProjectionContextV1(BaseModel):
    """v2.1 §4.4 raw projection context.

    REWORKED 2026-08-11 per sol MAJOR-1 (`AI_agent/logs/reviews/verdict/
    2026-08-11_f22_f9s0s1_crossreview_sol.md`): `raw_draw_sha256` used to be
    an opaque caller-supplied string with no cross-check against any actual
    bytes elsewhere in the eventual artifact, so a context could legally
    claim a `raw_draw_sha256` that disagreed with the raw draw bytes it was
    actually paired with in `WindowResolverInputsArtifactV2` (sol's repro:
    `raw_hash_mismatch_accepted == True`). `WindowResolverInputsArtifactV2`
    below now cross-validates this field against its own
    `raw_draw_canonical_bytes`, and a new `view_datum_sha256` field
    similarly cross-validates against `direction_datum_sidecar_bytes` (v2.1
    §5.79-582: "V2 artifact 内嵌 direction/datum sidecar bytes"). The full
    per-scope `datum_geometry_sha256`/`z_datum_mode`/`ElevationProjection
    ScopeV1` machinery (v2.1 §7.1-7.3) is real algorithm work that requires
    live authentication (S2) to produce meaningfully — S0's job is only to
    make the WIRE capable of expressing and hash-binding an identity, which
    `view_datum_sha256` now does; it is not yet populated by any live scope
    resolver.
    """

    model_config = _CFG
    raw_draw_sha256: Hex64
    resolver_hash: Hex64
    view_datum_sha256: Hex64
    floor_rings: tuple[FloorRingFactV1, ...] = Field(min_length=1)
    content_sha256: Hex64

    @model_validator(mode="after")
    def _hash(self):
        ids = [row.floor_id for row in self.floor_rings]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("floor_rings must be sorted by floor_id and unique")
        if self.content_sha256 != canonical_sha256(_without_key(self, "content_sha256")):
            raise ValueError("content_sha256 does not match canonical raw projection context")
        return self


def materialize_raw_projection_context(
    raw: CorrectionDrawV3CitationV2, *, raw_draw_sha256: str, resolver_hash: str, view_datum_sha256: str,
) -> RawProjectionContextV1:
    """Build the window-independent facade-ring context straight from a raw
    draw's own floor/cell geometry (v2.1 §4.4).

    Deliberately takes no window-shaped input and refuses anything but a
    `CorrectionDrawV3CitationV2` instance (v2.1 §4.4 requires this to run
    BEFORE hydration; the type check is a structural guard against a caller
    handing it a full `CorrectedGeometryV3` instead — §12.2 "Raw projection
    context" lock: "给 context builder 传 full model...时，structure/path
    锁红"). Its implementation only reads `raw.floors`, so it cannot depend
    on any window having been hydrated even by accident.

    `raw_draw_sha256` and `view_datum_sha256` are caller-supplied here (this
    function has no bytes of its own to hash) but MUST agree with the
    eventual `WindowResolverInputsArtifactV2.raw_draw_canonical_bytes` /
    `direction_datum_sidecar_bytes` -- that cross-field agreement is
    enforced when the two are assembled together into the artifact, not
    here (see `WindowResolverInputsArtifactV2._canonical_and_hashed`).
    """
    if not isinstance(raw, CorrectionDrawV3CitationV2):
        raise TypeError(
            "materialize_raw_projection_context requires a raw "
            "CorrectionDrawV3CitationV2, not a full/hydrated model"
        )
    rows = tuple(sorted(
        (
            FloorRingFactV1(
                floor_id=floor.id,
                footprint_fingerprint=floor_footprint_fingerprint(raw, floor),
                z_floor=floor.z_floor, ceiling_height=floor.ceiling_height,
            )
            for floor in raw.floors
        ),
        key=lambda row: row.floor_id,
    ))
    payload = {
        "raw_draw_sha256": raw_draw_sha256, "resolver_hash": resolver_hash,
        "view_datum_sha256": view_datum_sha256,
        "floor_rings": [row.model_dump(mode="json") for row in rows],
    }
    return RawProjectionContextV1(
        raw_draw_sha256=raw_draw_sha256, resolver_hash=resolver_hash, view_datum_sha256=view_datum_sha256,
        floor_rings=rows, content_sha256=canonical_sha256(payload),
    )


# ---------------------------------------------------------------------------
# §4.1/§8.5 resolver-input artifact V2 -- the new-run wire, distinct from
# the historical `window_resolver_inputs_v1` producer artifact (still owned
# by `window_sources.WindowResolverInputsArtifactV1`, untouched by S0).
# ---------------------------------------------------------------------------

class WindowResolverInputsArtifactV2(BaseModel):
    """v2.1 §4.1 table row "新 v3 raw draw" / §8.5 attempt-bundle member.

    Distinguished from the historical `window_resolver_inputs_v1` artifact
    by its OWN `artifact_version` field, never by inspecting whether a
    window carries `span` (v2.1 §4.1: "禁止按『有没有 span』猜版本" — see
    `load_window_resolver_artifact` below, which dispatches on this field).

    REWORKED 2026-08-11 per sol MAJOR-1: the ONLY thing the validator used
    to check was this outer envelope's own `content_sha256` — it never
    verified that `raw_projection_context.raw_draw_sha256` actually equals
    `sha256(raw_draw_canonical_bytes)`. Because the outer hash is computed
    OVER both fields together, a hostile/buggy writer could pair a raw draw
    with a context built from DIFFERENT raw bytes, recompute the outer hash
    over that mismatched pair, and the result would still self-validate —
    "外层再自哈希不能替代内层身份绑定" (sol). Now enforced explicitly, plus
    the matching `direction_datum_sidecar_bytes` <-> `view_datum_sha256`
    cross-check (v2.1 §579-582).
    """

    model_config = _CFG
    artifact_version: Literal["window_resolver_inputs_v2"]
    raw_draw_canonical_bytes: bytes
    raw_view_manifest_bytes: bytes
    raw_reading_artifacts: tuple[RawReadingArtifactV1, ...]
    direction_datum_sidecar_bytes: bytes
    raw_projection_context: RawProjectionContextV1
    content_sha256: Hex64

    @model_validator(mode="after")
    def _canonical_and_hashed(self):
        ids = tuple(row.input_id for row in self.raw_reading_artifacts)
        if ids != tuple(sorted(ids)) or len(ids) != len(set(ids)):
            raise ValueError("raw_reading_artifacts must have unique canonical input_id order")
        actual_raw_hash = hashlib.sha256(self.raw_draw_canonical_bytes).hexdigest()
        if actual_raw_hash != self.raw_projection_context.raw_draw_sha256:
            raise ValueError(
                "raw_projection_context.raw_draw_sha256 does not match "
                "sha256(raw_draw_canonical_bytes) -- context is bound to different raw bytes"
            )
        actual_datum_hash = hashlib.sha256(self.direction_datum_sidecar_bytes).hexdigest()
        if actual_datum_hash != self.raw_projection_context.view_datum_sha256:
            raise ValueError(
                "raw_projection_context.view_datum_sha256 does not match "
                "sha256(direction_datum_sidecar_bytes) -- context is bound to a different datum sidecar"
            )
        if self.content_sha256 != canonical_sha256(_without_key(self, "content_sha256")):
            raise ValueError("content_sha256 does not match resolver-input artifact v2")
        return self


# ---------------------------------------------------------------------------
# §5.3/§8.1 per-window position decision + the evidence-artifact wrapper.
# ---------------------------------------------------------------------------

def _canonical_window_key(plan_locator: str) -> str:
    """v2.1 §5.3: "必须另存 canonical_window_key = hash(plan_authority_locator)".
    A pure function (not a method) so both `build_window_position_decision`
    and the model's own validator compute it identically.
    """
    return canonical_sha256({"schema": "canonical_window_key_v1", "plan_authority_locator": plan_locator})


class WindowPositionDecisionV1(BaseModel):
    """v2.1 §5.3/§8.1 per-window position decision.

    REWORKED 2026-08-11 per sol MAJOR-2. Two independent gaps closed:

    1. Preimage was missing every identity field that binds a decision to a
       SPECIFIC raw draw / resolver artifact / projection context (v2.1
       §560-575 lists them explicitly). Without them, the exact same
       decision object could be embedded in two `WindowPositionEvidenceArt
       ifactV1` wrappers declaring two different `(raw_draw_sha256,
       resolver_hash)` pairs and self-hash validly in both — sol's repro:
       `evidence_binding a b ... == evidence_binding c d ...` (identical
       `decision_sha256` under two different, both-plausible-looking
       outer bindings). Now `raw_draw_sha256`/`resolver_hash`/
       `raw_projection_context_sha256` are IN the decision's own preimage,
       and the wrapper cross-validates its own binding against them (see
       `WindowPositionEvidenceArtifactV1._hash` below) — a decision can no
       longer be silently re-parented onto a different raw/resolver/context
       triple.
    2. `accepted` had no invariants connecting it to the OTHER fields on the
       same decision — sol's three hostile constructions (`accepted_
       without_elevation`, `accepted_wrong_derived_span`,
       `accepted_distance_over_tolerance`) all passed. Now enforced: an
       accepted decision must have >=1 elevation corroborator, every
       distance <= tolerance_value, and `derived_span == plan_world_interval`
       (v2.1 §5.4: "derived_span = sorted(plan_authority.world_..._interval)").
    """

    model_config = _CFG
    raw_draw_sha256: Hex64
    resolver_hash: Hex64
    raw_projection_context_sha256: Hex64
    canonical_window_key: Hex64
    window_id: StableId
    plan_locator: SourceLocator
    elevation_locators: tuple[SourceLocator, ...]
    plan_world_interval: SourceIntervalV1
    elevation_projected_intervals: tuple[SourceIntervalV1, ...]
    distances: tuple[FiniteFloat, ...]
    tolerance_name: StableId
    tolerance_value: FiniteFloat
    decision: Literal["accepted", "rejected"]
    derived_span: SourceIntervalV1 | None
    decision_sha256: Hex64

    @model_validator(mode="after")
    def _shape(self):
        if not (len(self.elevation_locators) == len(self.elevation_projected_intervals) == len(self.distances)):
            raise ValueError("elevation locators/projected intervals/distances must align 1:1")
        if len(set(self.elevation_locators)) != len(self.elevation_locators):
            raise ValueError("elevation_locators must not repeat within one decision")
        if any(d < 0 for d in self.distances):
            raise ValueError("distances must be non-negative")
        if self.tolerance_value <= 0:
            raise ValueError("tolerance_value must be positive")
        if self.canonical_window_key != _canonical_window_key(self.plan_locator):
            raise ValueError("canonical_window_key does not match hash(plan_locator)")
        if self.decision == "accepted":
            if not self.elevation_locators:
                raise ValueError("accepted decision requires at least one elevation corroborator")
            if any(d > self.tolerance_value for d in self.distances):
                raise ValueError("accepted decision requires every distance within tolerance_value")
            if self.derived_span is None:
                raise ValueError("accepted decision requires derived_span")
            if self.derived_span != self.plan_world_interval:
                raise ValueError(
                    "accepted decision's derived_span must equal plan_world_interval "
                    "(v2.1 §5.4: plan authority is the sole span source)"
                )
        if self.decision == "rejected" and self.derived_span is not None:
            raise ValueError("rejected decision must not carry derived_span")
        if self.decision_sha256 != canonical_sha256(_without_key(self, "decision_sha256")):
            raise ValueError("decision_sha256 does not match canonical position decision")
        return self


class WindowPositionEvidenceArtifactV1(BaseModel):
    """v2.1 §8.1 wrapper. REWORKED 2026-08-11 per sol MAJOR-2: now
    cross-validates that every embedded decision's OWN `raw_draw_sha256`/
    `resolver_hash` agree with this wrapper's — the outer envelope can no
    longer claim a binding a decision does not itself carry.
    """

    model_config = _CFG
    raw_draw_sha256: Hex64
    resolver_hash: Hex64
    decisions: tuple[WindowPositionDecisionV1, ...]
    content_sha256: Hex64

    @model_validator(mode="after")
    def _hash(self):
        ids = [d.window_id for d in self.decisions]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("decisions must be sorted by window_id and unique")
        for decision in self.decisions:
            if decision.raw_draw_sha256 != self.raw_draw_sha256 or decision.resolver_hash != self.resolver_hash:
                raise ValueError(
                    f"decision {decision.window_id!r} raw_draw_sha256/resolver_hash "
                    "does not match this evidence artifact's own binding"
                )
        if self.content_sha256 != canonical_sha256(_without_key(self, "content_sha256")):
            raise ValueError("content_sha256 does not match canonical position evidence artifact")
        return self


def build_window_position_decision(
    *, raw_draw_sha256: str, resolver_hash: str, raw_projection_context_sha256: str,
    window_id: str, plan_locator: str, elevation_locators: tuple[str, ...],
    plan_world_interval: SourceIntervalV1, elevation_projected_intervals: tuple[SourceIntervalV1, ...],
    distances: tuple[float, ...], tolerance_name: str, tolerance_value: float,
    decision: Literal["accepted", "rejected"], derived_span: SourceIntervalV1 | None,
) -> WindowPositionDecisionV1:
    """Convenience constructor that computes `decision_sha256` and
    `canonical_window_key` from the rest of the fields, so a caller (or
    test) never hand-supplies either in a way that could silently drift
    from its preimage.
    """
    canonical_window_key = _canonical_window_key(plan_locator)
    payload = {
        "raw_draw_sha256": raw_draw_sha256, "resolver_hash": resolver_hash,
        "raw_projection_context_sha256": raw_projection_context_sha256,
        "canonical_window_key": canonical_window_key,
        "window_id": window_id, "plan_locator": plan_locator,
        "elevation_locators": list(elevation_locators),
        "plan_world_interval": plan_world_interval.model_dump(mode="json"),
        "elevation_projected_intervals": [i.model_dump(mode="json") for i in elevation_projected_intervals],
        "distances": list(distances), "tolerance_name": tolerance_name, "tolerance_value": tolerance_value,
        "decision": decision,
        "derived_span": None if derived_span is None else derived_span.model_dump(mode="json"),
    }
    return WindowPositionDecisionV1(
        raw_draw_sha256=raw_draw_sha256, resolver_hash=resolver_hash,
        raw_projection_context_sha256=raw_projection_context_sha256, canonical_window_key=canonical_window_key,
        window_id=window_id, plan_locator=plan_locator, elevation_locators=elevation_locators,
        plan_world_interval=plan_world_interval, elevation_projected_intervals=elevation_projected_intervals,
        distances=distances, tolerance_name=tolerance_name, tolerance_value=tolerance_value,
        decision=decision, derived_span=derived_span, decision_sha256=canonical_sha256(payload),
    )


# ---------------------------------------------------------------------------
# §4.1/§10 S0 explicit loader registry: dispatch on the wire's OWN
# `artifact_version` string, never on "has span" (v2.1 §4.1 last line: "禁止
# 按『有没有 span』猜版本"). Unknown version fails closed.
# ---------------------------------------------------------------------------

ArtifactLoader = Callable[[bytes], BaseModel]


def _load_window_resolver_inputs_v1(raw_bytes: bytes) -> WindowResolverInputsArtifactV1:
    # Historical v3 producer artifact V1 -- untouched legacy replay, owned by
    # window_sources.py. This loader does not reinterpret it in any way.
    return WindowResolverInputsArtifactV1.model_validate_json(raw_bytes)


def _load_window_resolver_inputs_v2(raw_bytes: bytes) -> WindowResolverInputsArtifactV2:
    return WindowResolverInputsArtifactV2.model_validate_json(raw_bytes)


WINDOW_RESOLVER_ARTIFACT_LOADER_REGISTRY: dict[str, ArtifactLoader] = {
    "window_resolver_inputs_v1": _load_window_resolver_inputs_v1,
    "window_resolver_inputs_v2": _load_window_resolver_inputs_v2,
}


def load_window_resolver_artifact(raw_bytes: bytes) -> BaseModel:
    """The single dispatch point for either resolver-artifact wire.

    Reads ONLY the `artifact_version` field to pick a loader — this is the
    explicit registry v2.1 §10 S0 requires, and the reason S0's "Legacy
    scope" lock (§12.2) can assert "以『有无 span』猜版本...锁红": this
    function structurally cannot do that, it never inspects `windows` at
    all before dispatching.
    """
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise position_evidence_error("source_identity_invalid", {"artifact": "window_resolver_artifact"}) from exc
    version = payload.get("artifact_version") if isinstance(payload, dict) else None
    loader = WINDOW_RESOLVER_ARTIFACT_LOADER_REGISTRY.get(version) if isinstance(version, str) else None
    if loader is None:
        raise position_evidence_error("unknown_artifact_version", {"artifact_version": version})
    return loader(raw_bytes)


# ============================================================================
# v2.1 §10 S2: authoritative frame V2, unified projection dispatcher, and the
# per-window shadow pairing decision.
#
# Runs ALONGSIDE the existing model-authored `window.span` path
# (`window_host.py::resolve_window_hosts`'s coarse "does span overlap every
# cited existence source" check, which stays completely untouched by this
# batch) — it never writes `span`, and its own report is consumed ONLY as a
# CROSS_CHECK (non-blocking) row: see `src/validator/checks/correction.py::
# _window_position_evidence_shadow`, wired at `CheckLayer.CROSS_CHECK`, which
# `src/validator/checks/schema.py::disposition()` maps to `Disposition.FLAG`
# on every `run_profile` for every FAIL status other than `CheckStatus.ERROR`
# — this module's own `compute_window_position_evidence_shadow` is written to
# be TOTAL for any well-typed input specifically so it never raises a
# data-quality exception into that ERROR path (v2.1 §10 S2: "不得...block").
#
# Deliberately NOT reusing `WindowPositionDecisionV1`/
# `WindowPositionEvidenceArtifactV1` above: those S0 types are bound to the
# FUTURE raw-v2 citation wire (`raw_draw_sha256` keyed to a
# `CorrectionDrawV3CitationV2`, which no live producer emits yet — S3/S4
# scope). S2 runs against TODAY's live model-authored FULL `CorrectedGeometryV3`
# wire and its already-authenticated `VerifiedWindowResolverInputs` — a
# genuinely different identity root (`producer_draw_sha256`/
# `resolver_inputs_sha256`, both already computed by `window_sources.
# build_verified_window_resolver_inputs`). Overloading the S0 types' fields to
# mean something else would be exactly the "same field, silently different
# meaning" drift this project's CLAUDE.md keeps re-finding. The shadow
# check-id (`correction.window_position_evidence_shadow`) is likewise
# deliberately DIFFERENT from the eventual S3 blocking check-id
# (`correction.window_position_evidence`, v2.1 §9's error table) — the two
# are unified only at the S4 cutover, not before.
# ============================================================================

WINDOW_EVIDENCE_PAIRING_TOL_M: float = 0.300
"""v2.1 §5.3: the endpoint L-infinity pairing tolerance between a cited plan
authority and a cited elevation corroborator, projected through the
authoritative current-ring frame. A FROZEN, named, own constant — v2.1 §5.3
explicitly forbids borrowing `window_host_span_epsilon_m` (a ~1e-9 numeric
equality guard, not a measurement tolerance) for this purpose. Cross-checked
against `envelope_reconcile_tol_m` by `_validate_window_evidence_pairing_
tolerance` below every time shadow evidence is computed (v2.1 §5.3: "配置
校验要求 0 < window_evidence_pairing_tol_m <= envelope_reconcile_tol_m").
Kept as a hardcoded module constant rather than a `configs/correction.yaml`
entry: v2.1 §5.3 calls for this value to be "冻结" (frozen/pinned), which a
hardcoded constant satisfies more directly than a YAML-tunable field would.
"""

WINDOW_EVIDENCE_PAIRING_TOLERANCE_NAME: str = "window_evidence_pairing_tol_m"

PROJECTION_SCOPE_EPSILON_M: float = 1e-9
"""v2.1 §7.3: "冻结纯数值边界量 projection_scope_epsilon_m = 1e-9；它不是
测量容差". Governs only the closed-interval containment test in
`resolve_elevation_source_floor_scope` below — never the pairing-distance
comparison, which uses `WINDOW_EVIDENCE_PAIRING_TOL_M` instead."""

Z_DATUM_MODE_WORLD_Z: Literal["world_z"] = "world_z"
"""v2.1 §7.3 names two z-datum modes (`world_z` / `floor_local_z`); today's
live wire (`ElevationSourceWindowV1.local_z_interval`, produced by
`window_sources._window_strokes` straight off a stroke's `y_range_m`) has no
per-source or per-scope declaration of which mode applies — there is no live
channel carrying a `z_datum_mode`/`z_origin_world_m` fact yet (that is
raw-v2/S3/S4 scope, v2.1 §7.1's `ElevationProjectionScopeV1`).
`resolve_elevation_source_floor_scope` therefore operates under EXACTLY ONE,
explicitly named mode (this constant) — not an inline `local_z == world_z`
comparison. This is how the F-9 route② S2 dispatch's hard constraint #6
("z 轴 datum 不许靠『今天恰好相等』...请落实成代码里的显式规则，不是隐式
依赖") is met: the choice is named, documented, and pinned by
`tests/test_f9_route2_s2_authoritative_projector.py`'s z-scope lock as a
discrete decision a future `floor_local_z` mode could replace — not a bare
numeric coincidence nobody could see or test.
"""

PAIRING_AMBIGUITY_EPSILON_M: float = 1e-9
"""v2.1 §5.3 condition 3: "最优与次优距离之差必须大于纯数值 ambiguity
epsilon". A FROZEN, PURE-NUMERIC safety margin against floating-point/exact-
tie coincidence in the mutual-nearest ranking below (see
`_rank_candidates_by_distance`) — explicitly NOT a measurement tolerance
(unlike `WINDOW_EVIDENCE_PAIRING_TOL_M`) and NOT borrowed from
`PROJECTION_SCOPE_EPSILON_M` (that one governs z-scope closed-interval
containment, a structurally different comparison this constant never enters).
Same order of magnitude as `PROJECTION_SCOPE_EPSILON_M` for the same reason:
both exist only to reject exact/near-exact numeric coincidence, not to
express physical measurement uncertainty — §12.2's own "Pair positive" lock
table forbids conflating this with the 0.300 m band."""


# ---------------------------------------------------------------------------
# MAJOR-B1 (`AI_agent/logs/reviews/verdict/
# 2026-08-12_round2_blocker1_and_bcd_crossreview_sol.md`): a named, versioned
# declaration of WHICH of v2.1 §5.3's six numbered pass conditions this
# module's shadow decision actually evaluates — sol's finding was that the
# shadow reported an unqualified `accepted`/`PASS` while covering only a
# SUBSET of the ruleset, with no machine-readable way for a consumer to tell.
# `WindowPositionEvidenceShadowReportV1.evaluated_conditions`/
# `.unevaluated_conditions` must EXACTLY partition `ALL_POSITION_EVIDENCE_
# CONDITIONS` (its own validator enforces this), and a report cannot claim
# `all_accepted=True` while `unevaluated_conditions` is non-empty (same
# validator) — so a FUTURE partial re-implementation that again narrows
# coverage is structurally prevented from silently looking like a clean PASS,
# not just discouraged by a comment.
# ---------------------------------------------------------------------------

POSITION_EVIDENCE_CONDITION_DISTANCE_TOLERANCE: str = "distance_within_tolerance"
POSITION_EVIDENCE_CONDITION_UNIQUE_MUTUAL_NEAREST: str = "unique_mutual_nearest"
POSITION_EVIDENCE_CONDITION_AMBIGUITY_MARGIN: str = "ambiguity_margin"
POSITION_EVIDENCE_CONDITION_SOURCE_NOT_REUSED: str = "source_not_reused"
POSITION_EVIDENCE_CONDITION_CLAIM_CONSISTENCY: str = "claim_consistency"
POSITION_EVIDENCE_CONDITION_SCOPE_RESOLUTION: str = "scope_resolution"

ALL_POSITION_EVIDENCE_CONDITIONS: tuple[str, ...] = tuple(sorted((
    POSITION_EVIDENCE_CONDITION_DISTANCE_TOLERANCE,
    POSITION_EVIDENCE_CONDITION_UNIQUE_MUTUAL_NEAREST,
    POSITION_EVIDENCE_CONDITION_AMBIGUITY_MARGIN,
    POSITION_EVIDENCE_CONDITION_SOURCE_NOT_REUSED,
    POSITION_EVIDENCE_CONDITION_CLAIM_CONSISTENCY,
    POSITION_EVIDENCE_CONDITION_SCOPE_RESOLUTION,
)))
"""v2.1 §5.3's six numbered conditions:
1 = `distance_within_tolerance`, 2 = `unique_mutual_nearest`,
3 = `ambiguity_margin`, 4 = `source_not_reused`, 5 = `claim_consistency`
(plan plane-interval/floor_ref consistent with the window's own floor/facade
claim — enforced below as a side effect of the family-consistent plan-domain
filter itself, since a plan candidate that fails family-consistency cannot be
the argmin of its own domain), 6 = `scope_resolution` (this module's
pre-existing z-scope resolution + floor match)."""

POSITION_EVIDENCE_RULESET_VERSION: str = "f9_route2_s2_position_evidence_ruleset_v2"
"""Bumped from the implicit, never-declared "v1" (distance + scope only) the
moment conditions 2/3/4 below were added. Any future change to WHICH
conditions are evaluated must bump this string too — it is part of every
report's identity, not decoration (see `WindowPositionEvidenceShadowReportV1`
below, where it sits alongside `evaluated_conditions`/`unevaluated_
conditions` in the coverage declaration MAJOR-B1 requires)."""

CURRENTLY_EVALUATED_POSITION_EVIDENCE_CONDITIONS: tuple[str, ...] = ALL_POSITION_EVIDENCE_CONDITIONS
CURRENTLY_UNEVALUATED_POSITION_EVIDENCE_CONDITIONS: tuple[str, ...] = ()
"""What `compute_window_position_evidence_shadow` actually evaluates today,
post-MAJOR-B1: all six. Kept as its own pair of named module constants (not
inlined at the report-construction call site) so there is exactly ONE place
to edit if a future change narrows coverage again — forcing that edit to be
an honest, visible declaration change, not a silent drop from the evidence."""


def _validate_window_evidence_pairing_tolerance(*, envelope_reconcile_tol_m: float) -> None:
    """v2.1 §5.3's required cross-field bound, executed every time shadow
    evidence is computed (see `compute_window_position_evidence_shadow`)
    rather than only once at import time, so a caller feeding an unusually
    small `envelope_reconcile_tol_m` cannot silently sail past the invariant.
    """
    if not (0 < WINDOW_EVIDENCE_PAIRING_TOL_M <= envelope_reconcile_tol_m):
        raise ValueError(
            "window_evidence_pairing_tol_m must be in (0, envelope_reconcile_tol_m]; "
            f"got {WINDOW_EVIDENCE_PAIRING_TOL_M} vs "
            f"envelope_reconcile_tol_m={envelope_reconcile_tol_m}"
        )


class AuthoritativeViewProjectionFrameV2(BaseModel):
    """v2.1 §6.3: "datum 来自已认证 direction fact + 当前 geometry projection
    context；仅它能进入 evidence gate / hydrator / host / Va". The ONLY frame
    type `project_window_source_along` accepts for an elevation source — see
    that function's `isinstance` gate. Constructible ONLY via
    `from_current_ring_binding`, never by hand-typing a sign/origin: the one
    legal source of these numbers is `window_sources.materialize_current_
    ring_va_elevation_bindings`'s already-hash-bound `VaElevationViewBindingV1`
    — the SAME authoritative binding `window_host.py::_source_world_interval`
    already uses for the existing coarse overlap check. S2 does not invent a
    second, independent projection; it reuses the one production already
    trusts and applies a strict distance+threshold decision to it instead of
    a loose overlap test.
    """

    model_config = _CFG
    input_id: StableId
    resolved_building_direction: Literal["North", "South", "East", "West"]
    world_axis: Literal["x", "y"]
    sign: Literal[-1, 1]
    along_origin: FiniteFloat
    mirrored: Annotated[bool, Strict()]
    local_x_positive: Literal["image_left_to_right", "image_right_to_left"]
    frame_transform_sha256: Hex64

    @classmethod
    def from_current_ring_binding(cls, binding) -> "AuthoritativeViewProjectionFrameV2":
        return cls(
            input_id=binding.input_id, resolved_building_direction=binding.resolved_building_direction,
            world_axis=binding.world_axis, sign=binding.sign, along_origin=binding.along_origin,
            mirrored=binding.mirrored, local_x_positive=binding.local_x_positive,
            frame_transform_sha256=binding.frame_transform_sha256,
        )


class AdvisoryViewProjectionFrameV1(BaseModel):
    """v2.1 §6.3: "datum 来自 reading overall width；仅 prompt formatter 接受".
    Structurally distinct from `AuthoritativeViewProjectionFrameV2` — exists
    here ONLY so a neuter test can construct one and hand it to
    `project_window_source_along` to prove the dispatcher's type gate
    rejects it (v2.1 §1.3/§12.2 "Advisory 隔离" lock: "advisory 类型传入
    enforcement 必须 type/error FAIL"). No production code path constructs
    this class; the live advisory computation remains `window_sources.py::
    _advisory_elevation_world_frame` (an untyped `(sign, width)` tuple
    consumed only by the prompt formatter, per that function's own
    docstring) — this type is not wired to it and does not change its
    behavior.
    """

    model_config = _CFG
    input_id: StableId
    sign: Literal[-1, 1]
    overall_width_m: FiniteFloat


class ProjectedAlongEvidenceV1(BaseModel):
    """v2.1 §6.3's unified `project_window_source_along` return shape: one
    origin-tagged world-along interval, regardless of whether the source was
    a plan observation (already world-frame) or an elevation observation
    (projected through the authoritative current-ring frame)."""

    model_config = _CFG
    channel: Literal["plan", "elevation"]
    source_locator: SourceLocator
    world_interval: SourceIntervalV1
    frame_sha256: Hex64 | None
    scope_floor_id: StableId | None

    @model_validator(mode="after")
    def _shape(self):
        if self.channel == "plan" and (self.frame_sha256 is not None or self.scope_floor_id is not None):
            raise ValueError("a plan channel projection carries no elevation frame/scope")
        if self.channel == "elevation" and self.frame_sha256 is None:
            raise ValueError("an elevation channel projection must record the frame it used")
        return self


def project_window_source_along(
    source, *, window_facade: str, frame=None, scope_floor_id: str | None = None,
) -> ProjectedAlongEvidenceV1:
    """v2.1 §6.3's single public projection entry point.

    Plan sources are already in world frame (the plan adapter): the family's
    along-facade axis (x for North/South, y for East/West) selects
    `world_x_interval`/`world_y_interval` directly, `frame` is ignored.

    Elevation sources are projected through `frame`, which MUST be an
    `AuthoritativeViewProjectionFrameV2` — the type gate v2.1 §1.3/§6.3
    requires ("enforcement 不接受 advisory frame"): an
    `AdvisoryViewProjectionFrameV1`, `None`, or any other object raises
    `TypeError` — never silently falls back to identity/zero.
    """
    if isinstance(source, PlanSourceWindowV1):
        interval = source.world_x_interval if window_facade in ("North", "South") else source.world_y_interval
        return ProjectedAlongEvidenceV1(
            channel="plan", source_locator=source.source_locator, world_interval=interval,
            frame_sha256=None, scope_floor_id=None,
        )
    if isinstance(source, ElevationSourceWindowV1):
        if not isinstance(frame, AuthoritativeViewProjectionFrameV2):
            raise TypeError(
                "project_window_source_along requires an AuthoritativeViewProjectionFrameV2 "
                f"for an elevation source; got {type(frame).__name__} "
                "(v2.1 §1.3: an advisory frame is never accepted by an enforcement path)"
            )
        lo, hi = facade_convention.project_affine_interval(
            along_origin=frame.along_origin, sign=frame.sign,
            local_lo=source.local_along_interval.lo, local_hi=source.local_along_interval.hi,
        )
        return ProjectedAlongEvidenceV1(
            channel="elevation", source_locator=source.source_locator,
            world_interval=SourceIntervalV1(lo=lo, hi=hi),
            frame_sha256=frame.frame_transform_sha256, scope_floor_id=scope_floor_id,
        )
    raise TypeError(f"project_window_source_along: unknown source channel type {type(source)!r}")


@dataclass(frozen=True)
class _FloorScopeResolution:
    """Tri-state result of `resolve_elevation_source_floor_scope` — kept as
    its own small type (not a bare `str | None`) so "z not observed on this
    source" and "z observed but does not resolve to exactly one floor" stay
    two DIFFERENTLY named outcomes instead of collapsing onto the same
    `None` (v2.1 §9: "不可用 None 表示『跑过但没结果』...每种『没算出来』
    的原因必须各自具名" — the same discipline the report-level type below
    applies at the whole-check level, applied here at this one sub-decision).
    """

    status: Literal["not_declared", "resolved", "unresolved"]
    floor_id: str | None


def resolve_elevation_source_floor_scope(source: ElevationSourceWindowV1, floors) -> _FloorScopeResolution:
    """v2.1 §7.3 z-datum scope resolution, restricted to the single
    `Z_DATUM_MODE_WORLD_Z` mode this batch's live wire can authenticate (see
    that constant's docstring). `floors` is any iterable of objects with
    `.id`/`.z_floor`/`.ceiling_height` (a `CorrectedGeometryV3.floors` list).

    Full-containment, closed-interval, with `PROJECTION_SCOPE_EPSILON_M`
    slack on both ends (v2.1 §7.3 rule 4): a source whose z-interval is not
    fully contained by EXACTLY ONE floor's `[z_floor, z_floor +
    ceiling_height]` band resolves to `"unresolved"` — never to the nearest
    or first candidate (v2.1 §7.3 rule 5: "重叠 scope 不取第一项、不选最窄/
    最大 overlap").

    This is the disambiguator the real `East_view` S3/S4 pair in
    `tests/fixtures/f9_window_host_crash/` needs: both project to the
    IDENTICAL world-along interval `[3.52, 4.72]` (same `local_along`, same
    frame) but S3's `local_z=(4.0, 5.8)` only fits floor-2's
    `[3.0, 6.6]` band and S4's `local_z=(1.0, 2.8)` only fits floor-1's
    `[0.0, 3.0]` band — an along-only comparison cannot tell them apart,
    only this z-scope check can.
    """
    if source.local_z_interval is None:
        return _FloorScopeResolution(status="not_declared", floor_id=None)
    lo, hi = source.local_z_interval.lo, source.local_z_interval.hi
    candidates = [
        floor.id for floor in floors
        if lo >= floor.z_floor - PROJECTION_SCOPE_EPSILON_M
        and hi <= floor.z_floor + floor.ceiling_height + PROJECTION_SCOPE_EPSILON_M
    ]
    if len(candidates) == 1:
        return _FloorScopeResolution(status="resolved", floor_id=candidates[0])
    return _FloorScopeResolution(status="unresolved", floor_id=None)


class WindowPositionEvidenceShadowDecisionV1(BaseModel):
    """v2.1 §10 S2's per-window shadow pairing decision — computed against
    TODAY's already-authenticated `VerifiedWindowResolverInputs` (identity
    root: `producer_draw_sha256`/`resolver_inputs_sha256`), NOT the future
    raw-v2 wire `WindowPositionDecisionV1` above targets (see this module's
    "§10 S2" section docstring for why the two are deliberately not the same
    type).

    `legacy_span_delta_m` is the Chebyshev distance between `derived_span`
    (this decision's own plan-authority-based span) and `legacy_model_span`
    (`window.span` on the SAME `geom` being checked — i.e. the value
    actually driving today's accept/reject/IDF output). It is `None` in
    EXACTLY one situation — no `derived_span` exists to diff against because
    `decision == "rejected"` — never because the diff happened to compute to
    zero (a real zero is written as the float `0.0`).
    """

    model_config = _CFG
    producer_draw_sha256: Hex64
    resolver_inputs_sha256: Hex64
    window_id: StableId
    window_floor_id: StableId
    plan_locator: SourceLocator | None
    plan_world_interval: SourceIntervalV1 | None
    elevation_locators: tuple[SourceLocator, ...]
    elevation_projected_intervals: tuple[SourceIntervalV1, ...]
    elevation_scope_status: tuple[Literal["not_declared", "resolved", "unresolved"], ...]
    elevation_scope_floor_ids: tuple[StableId | None, ...]
    distances: tuple[FiniteFloat, ...]
    tolerance_name: StableId
    tolerance_value: FiniteFloat
    decision: Literal["accepted", "rejected"]
    reject_code: StableId | None
    derived_span: SourceIntervalV1 | None
    legacy_model_span: SourceIntervalV1 | None
    legacy_span_delta_m: FiniteFloat | None
    decision_sha256: Hex64

    @model_validator(mode="after")
    def _shape(self):
        # `elevation_locators`/`elevation_scope_status`/`elevation_scope_floor_ids`
        # are recorded for every cited corroborator the decision EXAMINED
        # (n entries); `elevation_projected_intervals`/`distances` are
        # recorded only for the ones that reached a successful projection
        # (m entries, m <= n). A rejection at `projection_scope_unresolved`/
        # `projection_datum_unresolved` legitimately has m == n - 1 — the
        # examined-but-unprojectable corroborator that TRIGGERED the
        # rejection is still named in the first three tuples, just not in
        # the projection/distance pair (there is nothing to project).
        n = len(self.elevation_locators)
        if not (n == len(self.elevation_scope_status) == len(self.elevation_scope_floor_ids)):
            raise ValueError("elevation_locators/elevation_scope_status/elevation_scope_floor_ids must align 1:1")
        m = len(self.elevation_projected_intervals)
        if m != len(self.distances):
            raise ValueError("elevation_projected_intervals/distances must align 1:1")
        if m > n:
            raise ValueError("cannot have more projected intervals than examined corroborators")
        if len(set(self.elevation_locators)) != n:
            raise ValueError("elevation_locators must not repeat within one decision")
        if any(d < 0 for d in self.distances):
            raise ValueError("distances must be non-negative")
        if self.tolerance_value <= 0:
            raise ValueError("tolerance_value must be positive")
        for status, floor_id in zip(self.elevation_scope_status, self.elevation_scope_floor_ids):
            if (status == "resolved") != (floor_id is not None):
                raise ValueError("scope floor_id must be set iff status is 'resolved'")
        if self.decision == "accepted":
            if self.reject_code is not None:
                raise ValueError("accepted decision must not carry a reject_code")
            if self.plan_locator is None or self.plan_world_interval is None:
                raise ValueError("accepted decision requires a resolved plan authority")
            if not self.elevation_locators:
                raise ValueError("accepted decision requires at least one elevation corroborator")
            if m != n:
                raise ValueError(
                    "accepted decision requires every examined corroborator to have "
                    "reached a projected interval/distance"
                )
            for status, floor_id in zip(self.elevation_scope_status, self.elevation_scope_floor_ids):
                if status == "unresolved":
                    raise ValueError("accepted decision cannot carry an unresolved z-scope corroborator")
                if status == "resolved" and floor_id != self.window_floor_id:
                    raise ValueError("accepted decision's resolved z-scope must match the window's own floor")
            if any(d > self.tolerance_value for d in self.distances):
                raise ValueError("accepted decision requires every distance within tolerance_value")
            if self.derived_span is None or self.derived_span != self.plan_world_interval:
                raise ValueError(
                    "accepted decision's derived_span must equal plan_world_interval "
                    "(v2.1 §5.4: plan authority is the sole span source)"
                )
        else:
            if self.reject_code is None:
                raise ValueError("rejected decision requires a reject_code")
            if self.derived_span is not None:
                raise ValueError("rejected decision must not carry derived_span")
        if self.decision_sha256 != canonical_sha256(_without_key(self, "decision_sha256")):
            raise ValueError("decision_sha256 does not match canonical shadow decision")
        return self


class WindowPositionEvidenceShadowReportV1(BaseModel):
    """v2.1 §10 S2's per-run shadow report.

    `status` distinguishes "ran and produced real per-window decisions"
    (`"evaluated"` — the only status `decisions`/`all_accepted` are
    meaningful for) from "could not even build the current-ring
    authoritative frames for this ring" (`"binding_unavailable"` — e.g. the
    ring's own facade-segment/fingerprint invariants that
    `materialize_current_ring_va_elevation_bindings` already enforces for
    the EXISTING coarse check are violated; a pre-existing limitation of
    that shared helper, not something S2 papers over). Both are non-`None`
    objects with their own hash (v2.1 §9: "不可用 None 表示『跑过但没结果』")
    — so "ran with zero windows" (`window_count=0`, `all_accepted=True`,
    `status="evaluated"`), "ran but the ring's own binding invariants
    failed" (`status="binding_unavailable"`), and "did not run at all
    because no `verified_window_inputs` was supplied" (the caller in
    `src/validator/checks/correction.py` emits `CheckStatus.NOT_APPLICABLE`
    and never calls `compute_window_position_evidence_shadow` in that case)
    stay THREE separately observable states, never collapsed onto one `None`.

    MAJOR-B1 (`AI_agent/logs/reviews/verdict/
    2026-08-12_round2_blocker1_and_bcd_crossreview_sol.md`): `ruleset_version`
    + `evaluated_conditions` + `unevaluated_conditions` are the machine-
    readable coverage declaration sol's finding demanded — "shadow 用无修饰的
    accepted/PASS 表达只覆盖部分判据的结果...把结论说得比证据强". The
    `_shape` validator below enforces, at the TYPE level (not just by
    convention), that `evaluated_conditions`/`unevaluated_conditions` exactly
    partition `ALL_POSITION_EVIDENCE_CONDITIONS`, and that `all_accepted` can
    NEVER be `True` while `unevaluated_conditions` is non-empty — so a future
    partial re-implementation that narrows coverage again cannot construct an
    object that both declares incomplete coverage and claims a clean PASS.
    """

    model_config = _CFG
    producer_draw_sha256: Hex64
    resolver_inputs_sha256: Hex64
    status: Literal["evaluated", "binding_unavailable"]
    binding_error_code: StableId | None
    window_count: Annotated[int, Field(ge=0)]
    decisions: tuple[WindowPositionEvidenceShadowDecisionV1, ...]
    all_accepted: Annotated[bool, Strict()]
    ruleset_version: StableId
    evaluated_conditions: tuple[StableId, ...]
    unevaluated_conditions: tuple[StableId, ...]
    content_sha256: Hex64

    @model_validator(mode="after")
    def _shape(self):
        if self.status == "evaluated":
            if self.binding_error_code is not None:
                raise ValueError("an evaluated report must not carry a binding_error_code")
            if len(self.decisions) != self.window_count:
                raise ValueError("decisions must have exactly window_count entries")
            ids = [d.window_id for d in self.decisions]
            if ids != sorted(ids) or len(ids) != len(set(ids)):
                raise ValueError("decisions must be sorted by window_id and unique")
            for decision in self.decisions:
                if (decision.producer_draw_sha256 != self.producer_draw_sha256
                        or decision.resolver_inputs_sha256 != self.resolver_inputs_sha256):
                    raise ValueError(
                        f"decision {decision.window_id!r} identity does not match "
                        "this report's own binding"
                    )
            if self.all_accepted != all(d.decision == "accepted" for d in self.decisions):
                raise ValueError("all_accepted does not match the per-window decisions")
        else:
            if self.binding_error_code is None:
                raise ValueError("a binding_unavailable report requires a binding_error_code")
            if self.decisions:
                raise ValueError("a binding_unavailable report must not carry per-window decisions")
            if self.all_accepted:
                raise ValueError("a binding_unavailable report must not claim all_accepted")
        # --- MAJOR-B1 coverage lock ------------------------------------------
        if tuple(sorted(set(self.evaluated_conditions))) != tuple(sorted(self.evaluated_conditions)):
            raise ValueError("evaluated_conditions must be sorted and unique")
        if tuple(sorted(set(self.unevaluated_conditions))) != tuple(sorted(self.unevaluated_conditions)):
            raise ValueError("unevaluated_conditions must be sorted and unique")
        evaluated = set(self.evaluated_conditions)
        unevaluated = set(self.unevaluated_conditions)
        if evaluated & unevaluated:
            raise ValueError("evaluated_conditions and unevaluated_conditions must be disjoint")
        if (evaluated | unevaluated) != set(ALL_POSITION_EVIDENCE_CONDITIONS):
            raise ValueError(
                "evaluated_conditions + unevaluated_conditions must exactly partition "
                "ALL_POSITION_EVIDENCE_CONDITIONS -- a condition cannot silently vanish "
                "from the coverage declaration"
            )
        if self.unevaluated_conditions and self.all_accepted:
            raise ValueError(
                "a report with any unevaluated_conditions must not claim all_accepted=True "
                "(MAJOR-B1: a PASS must never be reported stronger than what was actually checked)"
            )
        if self.content_sha256 != canonical_sha256(_without_key(self, "content_sha256")):
            raise ValueError("content_sha256 does not match canonical shadow report")
        return self


def _build_authoritative_frames(
    geom: CorrectedGeometryV3, verified_inputs: VerifiedWindowResolverInputs, tol,
) -> dict[str, AuthoritativeViewProjectionFrameV2]:
    """Private, deliberately PATCHABLE seam: the one place S2 asks the
    current-ring binding materializer for authoritative frames, keyed by
    elevation `input_id`.

    Kept as its own top-level function (not inlined into
    `compute_window_position_evidence_shadow`) specifically so a neuter test
    can monkeypatch it: v2.1 §12.2's two named must-red neuters ("摘掉
    projector" / "改用 advisory frame") both act by replacing what THIS
    function returns, not by editing `project_window_source_along` itself —
    which is what actually proves those are two independently observable
    failure MODES (a numeric one and a type one), not one lock wearing two
    names. See `tests/test_f9_route2_s2_authoritative_projector.py`.
    """
    visibility_tolerances = _facade_visibility_tolerances(tol)
    bindings = materialize_current_ring_va_elevation_bindings(
        geom=geom, manifest=verified_inputs.inputs.view_manifest,
        direction_facts=verified_inputs.inputs.elevation_direction_facts,
        visibility_tolerances=visibility_tolerances,
    ) if geom.windows else ()
    return {
        binding.input_id: AuthoritativeViewProjectionFrameV2.from_current_ring_binding(binding)
        for binding in bindings
    }


def _facade_visibility_tolerances(tol):
    """Small shared adapter (`tol` -> `VisibilityTolerances`) so
    `_build_authoritative_frames` does not need its own copy of the two-field
    mapping `window_host.py::_visibility_tolerances` already performs for the
    same underlying `CoreTolerances` object — imported lazily to avoid a
    module-load-order dependency on `facade_visibility.py` for callers that
    never touch S2."""
    from src.agent.correction.facade_visibility import VisibilityTolerances

    return VisibilityTolerances(
        depth_epsilon_m=tol.facade_visibility_depth_epsilon_m,
        endpoint_epsilon_m=tol.facade_visibility_endpoint_epsilon_m,
    )


def _window_existence_sources(window_id: str, *, links, sources_by_locator):
    """`(plan_sources, elevation_sources)` cited by this window's
    `existence` claim, in the SAME canonical order
    `verified_inputs.inputs.claim_links` already lists them.

    Deliberately reads `claim_links` (the authenticated, hash-bound
    locators) rather than re-reading `window.provenance.existence.source_ids`
    directly — this consumes exactly the SAME sources the existing host
    resolver's coarse overlap check already trusts (v2.1 §5.1: existence's
    citations are the input to this decision, not something it re-derives).
    """
    plan: list[PlanSourceWindowV1] = []
    elevation: list[ElevationSourceWindowV1] = []
    for link in links:
        if link.window_id != window_id or link.claim != "existence":
            continue
        source = sources_by_locator.get(link.source_locator)
        if isinstance(source, PlanSourceWindowV1):
            plan.append(source)
        elif isinstance(source, ElevationSourceWindowV1):
            elevation.append(source)
    return tuple(plan), tuple(elevation)


# ---------------------------------------------------------------------------
# MAJOR-B1 condition 2/3: the mutual-nearest ranking + its plan-side facade
# candidate domain.
# ---------------------------------------------------------------------------

def _plan_source_consistent_with_family(
    source: PlanSourceWindowV1, family: str, segments, floor_id: str, *, tol_m: float,
) -> bool:
    """v2.1 §5.3 condition 2's "facade 候选域" restriction on the PLAN side.

    A `PlanSourceWindowV1` carries only `world_x_interval`/`world_y_interval`
    — no facade tag — so comparing a North-family elevation source against
    EVERY plan stroke on a floor, using only the along-axis, is not enough:
    a real, symmetric building routinely has a North window and a South
    window at the IDENTICAL along-position (verified against the real clean
    e2e fixture below: `1f_view/S12` North at world_x=[6.30,8.70] and
    `1f_view/S15` South at the SAME world_x=[6.30,8.70] — an exact tie with
    zero facade filter, confirmed empirically before writing this function).

    This does NOT use a single building-wide bbox extreme as a wall plane —
    `facade_convention.FACADE_BASE_PLANE_SIDE`'s own docstring forbids that
    exact pattern for "new code (Vg/Va, window-position)". Instead it uses
    the ALREADY-MATERIALIZED, per-segment `geom.facade_segments` (Vg): each
    segment is derived from the building's OWN polygon edges (not a global
    extreme), carries its own axis-aligned plane value and along-range, and
    both real fixtures' `finalize_correction_draw` already materializes it
    before `check_correction` runs (confirmed by reading `finalize.py` and
    `pipeline.py`/`run_stage.py`'s call order) — so it is available, real,
    GT-blind building geometry, not a citation-dependent or invented signal.
    This scales to L/U-shaped buildings with multiple segments per family
    (v2.1 §7.4): a candidate only needs to be consistent with ONE segment of
    `family`, and each segment's own along-range bounds where it applies.

    `tol_m` is `WINDOW_EVIDENCE_PAIRING_TOL_M` — the SAME measurement-scale
    tolerance that already governs "is this stroke close enough to count as
    evidence" elsewhere in this module, not a second, undocumented constant.
    """
    if family in ("North", "South"):
        cross = source.world_y_interval
        along = source.world_x_interval
    else:
        cross = source.world_x_interval
        along = source.world_y_interval
    for segment in segments:
        if segment.floor_id != floor_id or segment.facade_family != family:
            continue
        plane = segment.p1[1] if family in ("North", "South") else segment.p1[0]
        seg_lo, seg_hi = segment.world_along_interval.lo, segment.world_along_interval.hi
        if along.hi < seg_lo - tol_m or along.lo > seg_hi + tol_m:
            continue
        if cross.lo - tol_m <= plane <= cross.hi + tol_m:
            return True
    return False


def _rank_candidates_by_distance(candidates: list[tuple[float, str]]) -> tuple[tuple[float, str], ...]:
    """Sort `(distance, locator)` pairs ascending by distance, locator as a
    deterministic tiebreak key for the sort itself (never for the ACCEPT
    decision — an exact-distance tie between two DIFFERENT locators is
    exactly what `PAIRING_AMBIGUITY_EPSILON_M` below must catch, not silently
    resolve via locator ordering)."""
    return tuple(sorted(candidates, key=lambda pair: (pair[0], pair[1])))


def _is_unique_nearest(ranked: tuple[tuple[float, str], ...], *, expected_locator: str) -> bool:
    """v2.1 §5.3 conditions 2+3 together: `expected_locator` must be the
    STRICT argmin of `ranked` (condition 2) with a margin over the runner-up
    exceeding `PAIRING_AMBIGUITY_EPSILON_M` (condition 3) — a single boolean
    so callers cannot accidentally check one without the other. `ranked` is
    never empty when this is called with a locator that is itself a member of
    the candidate set the caller built it from."""
    if not ranked or ranked[0][1] != expected_locator:
        return False
    if len(ranked) >= 2 and (ranked[1][0] - ranked[0][0]) <= PAIRING_AMBIGUITY_EPSILON_M:
        return False
    return True


def _build_window_position_evidence_shadow_decision(
    window, *, plan_sources: tuple, elevation_sources: tuple,
    frames_by_input: Mapping[str, AuthoritativeViewProjectionFrameV2], floors,
    producer_draw_sha256: str, resolver_inputs_sha256: str, tolerance_value: float,
    catalog: tuple, facade_segments: tuple,
) -> WindowPositionEvidenceShadowDecisionV1:
    """Total for any well-typed `(window, cited-sources)` pair — every
    "could not verify" reason becomes a named `reject_code` on a real, hashed
    decision object, never an exception (v2.1 §10 S2: "不得...block"; an
    uncaught exception here would propagate out of a non-blocking cross-check
    and defeat that guarantee). Only `project_window_source_along`'s TYPE
    gate can still raise from inside this function, and that is deliberate:
    a caller passing the wrong frame TYPE is a wiring bug, not a
    data-quality fact to represent as a rejection — see
    `src/validator/checks/correction.py::_window_position_evidence_shadow`
    for where that specific exception is caught and turned into a FAIL row
    instead of an uncaught crash.

    Scope note: on a structural rejection (authority invalid / insufficient
    evidence / scope unresolved / datum unresolved / pair mismatch) this
    function stops accumulating `elevation_*` tuples at the FIRST corroborator
    that fails — a rejected decision may therefore list fewer corroborators
    than were actually cited, not the full cited set. This is a deliberate,
    documented S2 scope cut (not silently dropped data): S2 only needs to
    show ONE reason a window's evidence does not cohere, not enumerate every
    possible reason at once; S3's active detector, which turns this into a
    blocking gate, is where a fuller audit trail would earn its complexity.

    MAJOR-B1 (2026-08-12): `catalog` is the FULL, draw-wide source catalog
    (`verified_inputs.inputs.source_windows`, both channels, every view/floor
    — NOT just what this window cited) and `facade_segments` is `geom`'s
    already-materialized Vg segments. Both are now REQUIRED so conditions
    2 (unique mutual-nearest) and 3 (ambiguity margin) can be checked against
    candidates the model never mentioned at all (v2.1 §5.3: "全 catalog 只用于
    验证 cited pair 是否在...候选域中唯一最佳" — a domain built only from
    ALREADY-cited sources could never surface an uncited-but-better match,
    defeating the whole point of condition 2). Condition 4 (source reuse) is
    a DRAW-LEVEL property no single window's decision can see by itself —
    it is checked by `compute_window_position_evidence_shadow`'s caller-side
    second pass (`_detect_position_source_reuse`), after every window's
    decision from THIS function is in hand.
    """
    def _decision(
        *, decision: str, reject_code, derived_span, plan_locator, plan_world_interval,
        elevation_locators: tuple, elevation_projected_intervals: tuple,
        elevation_scope_status: tuple, elevation_scope_floor_ids: tuple, distances: tuple,
    ) -> WindowPositionEvidenceShadowDecisionV1:
        try:
            legacy_model_span = SourceIntervalV1(lo=float(window.span[0]), hi=float(window.span[1]))
        except (IndexError, TypeError, ValueError):
            legacy_model_span = None
        legacy_span_delta_m = None
        if derived_span is not None and legacy_model_span is not None:
            legacy_span_delta_m = max(
                abs(derived_span.lo - legacy_model_span.lo), abs(derived_span.hi - legacy_model_span.hi),
            )
        payload = {
            "producer_draw_sha256": producer_draw_sha256, "resolver_inputs_sha256": resolver_inputs_sha256,
            "window_id": window.id, "window_floor_id": window.floor_id,
            "plan_locator": plan_locator,
            "plan_world_interval": None if plan_world_interval is None else plan_world_interval.model_dump(mode="json"),
            "elevation_locators": list(elevation_locators),
            "elevation_projected_intervals": [i.model_dump(mode="json") for i in elevation_projected_intervals],
            "elevation_scope_status": list(elevation_scope_status),
            "elevation_scope_floor_ids": list(elevation_scope_floor_ids),
            "distances": list(distances),
            "tolerance_name": WINDOW_EVIDENCE_PAIRING_TOLERANCE_NAME, "tolerance_value": tolerance_value,
            "decision": decision, "reject_code": reject_code,
            "derived_span": None if derived_span is None else derived_span.model_dump(mode="json"),
            "legacy_model_span": None if legacy_model_span is None else legacy_model_span.model_dump(mode="json"),
            "legacy_span_delta_m": legacy_span_delta_m,
        }
        return WindowPositionEvidenceShadowDecisionV1(
            producer_draw_sha256=producer_draw_sha256, resolver_inputs_sha256=resolver_inputs_sha256,
            window_id=window.id, window_floor_id=window.floor_id,
            plan_locator=plan_locator, plan_world_interval=plan_world_interval,
            elevation_locators=elevation_locators, elevation_projected_intervals=elevation_projected_intervals,
            elevation_scope_status=elevation_scope_status, elevation_scope_floor_ids=elevation_scope_floor_ids,
            distances=distances, tolerance_name=WINDOW_EVIDENCE_PAIRING_TOLERANCE_NAME,
            tolerance_value=tolerance_value, decision=decision, reject_code=reject_code,
            derived_span=derived_span, legacy_model_span=legacy_model_span,
            legacy_span_delta_m=legacy_span_delta_m, decision_sha256=canonical_sha256(payload),
        )

    def _reject(code: str, *, plan_locator=None, plan_world_interval=None,
                elevation_locators=(), elevation_projected_intervals=(),
                elevation_scope_status=(), elevation_scope_floor_ids=(), distances=()):
        return _decision(
            decision="rejected", reject_code=code, derived_span=None,
            plan_locator=plan_locator, plan_world_interval=plan_world_interval,
            elevation_locators=elevation_locators, elevation_projected_intervals=elevation_projected_intervals,
            elevation_scope_status=elevation_scope_status, elevation_scope_floor_ids=elevation_scope_floor_ids,
            distances=distances,
        )

    if len(plan_sources) != 1:
        # Zero -> no authority to derive a span from; >1 -> ambiguous authority.
        # Both are v2.1 §5.1's "existence｜...恰有一个 plan source" violated.
        return _reject("position_evidence_authority_invalid")
    plan_source = plan_sources[0]
    plan_world_interval = project_window_source_along(plan_source, window_facade=window.facade).world_interval

    if not elevation_sources:
        return _reject(
            "position_evidence_insufficient",
            plan_locator=plan_source.source_locator, plan_world_interval=plan_world_interval,
        )
    if len({source.source_input_id for source in elevation_sources}) != len(elevation_sources):
        # v2.1 §5.1: "同一 view 最多一条 position corroborator".
        return _reject(
            "position_evidence_authority_invalid",
            plan_locator=plan_source.source_locator, plan_world_interval=plan_world_interval,
        )

    elevation_locators: list[str] = []
    elevation_projected: list[SourceIntervalV1] = []
    scope_status: list[str] = []
    scope_floor_ids: list[str | None] = []
    distances: list[float] = []
    floors_list = list(floors)
    # MAJOR-B1 plan-side domain: the facade-segment consistency lookup below
    # must use the FLOOR THE PLAN CANDIDATES THEMSELVES BELONG TO (derived
    # from `plan_source.floor_ref`, the SAME key the domain's own `floor_ref`
    # filter uses), never `scope.floor_id` (the CITED elevation source's own
    # resolved floor). These two coincide whenever `along = window.facade`'s
    # own citations happen to be internally consistent, but a malformed draw
    # can pass a `plan_source` and an elevation `scope` that disagree — using
    # the elevation's floor for the plan-side lookup would silently borrow
    # the wrong floor's wall geometry in exactly that malformed case.
    floor_rank = {floor.id: index for index, floor in enumerate(sorted(floors_list, key=lambda f: f.z_floor), start=1)}
    floor_id_by_ref = {ref: floor_id for floor_id, ref in floor_rank.items()}
    plan_source_floor_id = floor_id_by_ref.get(plan_source.floor_ref)
    for source in elevation_sources:
        scope = resolve_elevation_source_floor_scope(source, floors_list)
        # The examined corroborator's locator/scope are recorded FIRST,
        # before either early-exit check below — a rejection must still name
        # the specific source that caused it, not silently omit it from the
        # audit trail (see `WindowPositionEvidenceShadowDecisionV1._shape`'s
        # m <= n alignment rule for why elevation_projected_intervals/
        # distances are allowed to be one entry shorter here).
        elevation_locators.append(source.source_locator)
        scope_status.append(scope.status)
        scope_floor_ids.append(scope.floor_id)
        if scope.status == "unresolved":
            return _reject(
                "projection_scope_unresolved",
                plan_locator=plan_source.source_locator, plan_world_interval=plan_world_interval,
                elevation_locators=tuple(elevation_locators), elevation_projected_intervals=tuple(elevation_projected),
                elevation_scope_status=tuple(scope_status), elevation_scope_floor_ids=tuple(scope_floor_ids),
                distances=tuple(distances),
            )
        frame = frames_by_input.get(source.source_input_id)
        if frame is None:
            return _reject(
                "projection_datum_unresolved",
                plan_locator=plan_source.source_locator, plan_world_interval=plan_world_interval,
                elevation_locators=tuple(elevation_locators), elevation_projected_intervals=tuple(elevation_projected),
                elevation_scope_status=tuple(scope_status), elevation_scope_floor_ids=tuple(scope_floor_ids),
                distances=tuple(distances),
            )
        projected = project_window_source_along(
            source, window_facade=window.facade, frame=frame, scope_floor_id=scope.floor_id,
        ).world_interval
        distance = max(
            abs(plan_world_interval.lo - projected.lo), abs(plan_world_interval.hi - projected.hi),
        )
        elevation_projected.append(projected)
        distances.append(distance)
        if scope.status == "resolved" and scope.floor_id != window.floor_id:
            return _reject(
                "position_evidence_pair_mismatch",
                plan_locator=plan_source.source_locator, plan_world_interval=plan_world_interval,
                elevation_locators=tuple(elevation_locators), elevation_projected_intervals=tuple(elevation_projected),
                elevation_scope_status=tuple(scope_status), elevation_scope_floor_ids=tuple(scope_floor_ids),
                distances=tuple(distances),
            )
        if distance > tolerance_value:
            return _reject(
                "position_evidence_pair_mismatch",
                plan_locator=plan_source.source_locator, plan_world_interval=plan_world_interval,
                elevation_locators=tuple(elevation_locators), elevation_projected_intervals=tuple(elevation_projected),
                elevation_scope_status=tuple(scope_status), elevation_scope_floor_ids=tuple(scope_floor_ids),
                distances=tuple(distances),
            )

        # --- MAJOR-B1 condition 2+3, elevation side -------------------------
        # `E_i` must be the UNIQUE (with ambiguity margin) nearest elevation
        # candidate, among sources sharing E_i's OWN view (`source_input_id`)
        # AND E_i's OWN resolved scope (`scope.floor_id`) -- scope filtering
        # happens BEFORE this ranking is built (v2.1 §5.3: "scope 过滤必须
        # 发生在距离计算和 mutual-nearest 排名之前"; the §12.2 顺序锁), so a
        # same-along/different-floor stroke (e.g. the real East_view S3/S4
        # pair, which project to the IDENTICAL along interval) can never
        # enter this ranking as a false competitor.
        elev_domain_ranked_input: list[tuple[float, str]] = []
        for candidate in catalog:
            if candidate.channel != "elevation" or candidate.source_input_id != source.source_input_id:
                continue
            candidate_scope = resolve_elevation_source_floor_scope(candidate, floors_list)
            if candidate_scope.status != "resolved" or candidate_scope.floor_id != scope.floor_id:
                continue
            candidate_projected = project_window_source_along(
                candidate, window_facade=window.facade, frame=frame, scope_floor_id=candidate_scope.floor_id,
            ).world_interval
            candidate_distance = max(
                abs(plan_world_interval.lo - candidate_projected.lo),
                abs(plan_world_interval.hi - candidate_projected.hi),
            )
            elev_domain_ranked_input.append((candidate_distance, candidate.source_locator))
        elev_ranked = _rank_candidates_by_distance(elev_domain_ranked_input)
        if not _is_unique_nearest(elev_ranked, expected_locator=source.source_locator):
            return _reject(
                "position_evidence_pair_mismatch",
                plan_locator=plan_source.source_locator, plan_world_interval=plan_world_interval,
                elevation_locators=tuple(elevation_locators), elevation_projected_intervals=tuple(elevation_projected),
                elevation_scope_status=tuple(scope_status), elevation_scope_floor_ids=tuple(scope_floor_ids),
                distances=tuple(distances),
            )

        # --- MAJOR-B1 condition 2+3, plan side -------------------------------
        # `P` must be the UNIQUE (with ambiguity margin) nearest plan
        # candidate to THIS `E_i`, among plan sources sharing P's OWN
        # `floor_ref` AND consistent with `window.facade`'s family (the
        # facade-segment domain filter above) -- this is what stops a
        # symmetric building's opposite-facade twin (same along-position,
        # different wall) from producing a false tie (empirically verified
        # against both `tests/fixtures/f9_window_host_crash/` and the real
        # clean e2e run before this was written: zero false rejections on
        # either fixture with the filter in place, six false rejections
        # without it).
        plan_domain_ranked_input: list[tuple[float, str]] = []
        for candidate in catalog:
            if candidate.channel != "plan" or candidate.floor_ref != plan_source.floor_ref:
                continue
            if not _plan_source_consistent_with_family(
                candidate, window.facade, facade_segments, plan_source_floor_id, tol_m=tolerance_value,
            ):
                continue
            candidate_world = project_window_source_along(candidate, window_facade=window.facade).world_interval
            candidate_distance = max(
                abs(candidate_world.lo - projected.lo), abs(candidate_world.hi - projected.hi),
            )
            plan_domain_ranked_input.append((candidate_distance, candidate.source_locator))
        plan_ranked = _rank_candidates_by_distance(plan_domain_ranked_input)
        if not plan_ranked:
            # No candidate (not even P itself) is consistent with this
            # family/floor's facade geometry -- the catalog cannot
            # independently judge this pair at all (v2.1 §5.3: "若 catalog
            # 自身存在...缺一 channel，使代码无法独立判 identity，结果是
            # position_evidence_insufficient").
            return _reject(
                "position_evidence_insufficient",
                plan_locator=plan_source.source_locator, plan_world_interval=plan_world_interval,
                elevation_locators=tuple(elevation_locators), elevation_projected_intervals=tuple(elevation_projected),
                elevation_scope_status=tuple(scope_status), elevation_scope_floor_ids=tuple(scope_floor_ids),
                distances=tuple(distances),
            )
        if not _is_unique_nearest(plan_ranked, expected_locator=plan_source.source_locator):
            return _reject(
                "position_evidence_pair_mismatch",
                plan_locator=plan_source.source_locator, plan_world_interval=plan_world_interval,
                elevation_locators=tuple(elevation_locators), elevation_projected_intervals=tuple(elevation_projected),
                elevation_scope_status=tuple(scope_status), elevation_scope_floor_ids=tuple(scope_floor_ids),
                distances=tuple(distances),
            )

    return _decision(
        decision="accepted", reject_code=None, derived_span=plan_world_interval,
        plan_locator=plan_source.source_locator, plan_world_interval=plan_world_interval,
        elevation_locators=tuple(elevation_locators), elevation_projected_intervals=tuple(elevation_projected),
        elevation_scope_status=tuple(scope_status), elevation_scope_floor_ids=tuple(scope_floor_ids),
        distances=tuple(distances),
    )


def _downgrade_decision_to_rejected(
    decision: WindowPositionEvidenceShadowDecisionV1, *, code: str,
) -> WindowPositionEvidenceShadowDecisionV1:
    """MAJOR-B1 condition 4 support: rebuild `decision` as `rejected`/`code`,
    preserving every EXAMINED field (locators, projected intervals, scope
    status, distances) so the audit trail still shows what was cited — only
    `decision`/`reject_code`/`derived_span`/`legacy_span_delta_m` change.
    `WindowPositionEvidenceShadowDecisionV1` is frozen, so this constructs a
    NEW object (not a mutation) and recomputes `decision_sha256` accordingly;
    it deliberately does not go through the `_decision`/`_reject` closures
    inside `_build_window_position_evidence_shadow_decision` (those are
    per-window-local and do not have access to a decision built for a
    DIFFERENT window's context) — this is the draw-level counterpart.
    """
    payload = {
        "producer_draw_sha256": decision.producer_draw_sha256, "resolver_inputs_sha256": decision.resolver_inputs_sha256,
        "window_id": decision.window_id, "window_floor_id": decision.window_floor_id,
        "plan_locator": decision.plan_locator,
        "plan_world_interval": None if decision.plan_world_interval is None else decision.plan_world_interval.model_dump(mode="json"),
        "elevation_locators": list(decision.elevation_locators),
        "elevation_projected_intervals": [i.model_dump(mode="json") for i in decision.elevation_projected_intervals],
        "elevation_scope_status": list(decision.elevation_scope_status),
        "elevation_scope_floor_ids": list(decision.elevation_scope_floor_ids),
        "distances": list(decision.distances),
        "tolerance_name": decision.tolerance_name, "tolerance_value": decision.tolerance_value,
        "decision": "rejected", "reject_code": code,
        "derived_span": None,
        "legacy_model_span": None if decision.legacy_model_span is None else decision.legacy_model_span.model_dump(mode="json"),
        "legacy_span_delta_m": None,
    }
    return WindowPositionEvidenceShadowDecisionV1(
        producer_draw_sha256=decision.producer_draw_sha256, resolver_inputs_sha256=decision.resolver_inputs_sha256,
        window_id=decision.window_id, window_floor_id=decision.window_floor_id,
        plan_locator=decision.plan_locator, plan_world_interval=decision.plan_world_interval,
        elevation_locators=decision.elevation_locators, elevation_projected_intervals=decision.elevation_projected_intervals,
        elevation_scope_status=decision.elevation_scope_status, elevation_scope_floor_ids=decision.elevation_scope_floor_ids,
        distances=decision.distances, tolerance_name=decision.tolerance_name, tolerance_value=decision.tolerance_value,
        decision="rejected", reject_code=code, derived_span=None,
        legacy_model_span=decision.legacy_model_span, legacy_span_delta_m=None,
        decision_sha256=canonical_sha256(payload),
    )


def _detect_position_source_reuse(
    decisions: tuple[WindowPositionEvidenceShadowDecisionV1, ...],
) -> tuple[WindowPositionEvidenceShadowDecisionV1, ...]:
    """MAJOR-B1 condition 4 / v2.1 §5.1: "一个 plan authority 或 elevation
    position source 在同一 draw 中不得被两个 window 复用". A DRAW-LEVEL pass
    (needs every window's decision at once, which no single per-window call
    can see) that runs AFTER conditions 1/2/3/5/6 have each independently
    produced a per-window decision.

    Any source locator (the `plan_locator`, or any entry of
    `elevation_locators`) that is claimed by MORE THAN ONE `accepted`
    decision is a reuse: every one of those decisions is downgraded to
    `rejected`/`position_evidence_authority_invalid` — v2.1 §9's own mapping
    ("...source 复用 -> position_evidence_authority_invalid"). An ALREADY-
    rejected decision is left untouched: it never won the authority it would
    be "fighting over", so it does not participate in this check.
    """
    claim_counts: dict[str, list[str]] = {}
    for decision in decisions:
        if decision.decision != "accepted":
            continue
        claimed = set(decision.elevation_locators)
        if decision.plan_locator is not None:
            claimed.add(decision.plan_locator)
        for locator in claimed:
            claim_counts.setdefault(locator, []).append(decision.window_id)
    reused_window_ids = {
        window_id
        for window_ids in claim_counts.values() if len(window_ids) > 1
        for window_id in window_ids
    }
    if not reused_window_ids:
        return decisions
    return tuple(
        _downgrade_decision_to_rejected(decision, code="position_evidence_authority_invalid")
        if decision.window_id in reused_window_ids else decision
        for decision in decisions
    )


def compute_window_position_evidence_shadow(
    geom: CorrectedGeometryV3, *, verified_inputs: VerifiedWindowResolverInputs, tol,
) -> WindowPositionEvidenceShadowReportV1:
    """v2.1 §10 S2's public entry point.

    Runs the independent plan-authority vs elevation-corroborator pairing
    decision for every window in `geom`, against `geom`'s OWN cited
    `existence` sources (the same sources the live coarse overlap check in
    `window_host.py::resolve_window_hosts` already trusts) — see this
    module's "§10 S2" section docstring for the full contract.

    Total for a well-formed `geom`/`verified_inputs` pair drawn from the SAME
    producer draw: never raises for a data-quality reason. The one exception
    a caller must still expect is `WindowDirectionBindingError` from the
    shared current-ring binding materializer (this ring's OWN
    facade-segment/fingerprint invariants failing — a pre-existing
    limitation of that helper, not new to S2); this function catches that
    itself and returns a `status="binding_unavailable"` report rather than
    propagating it, so `compute_window_position_evidence_shadow` itself never
    raises for ANY well-typed input.

    MAJOR-B1 (2026-08-12): after every window's PER-WINDOW decision (which
    now also evaluates conditions 2/3, the mutual-nearest ranking) is built,
    this function runs `_detect_position_source_reuse` (condition 4) as a
    second, draw-level pass before computing the report's own `all_accepted`
    — so `all_accepted` reflects ALL SIX §5.3 conditions, not just the five a
    single window can see on its own. Every report also carries the
    machine-readable `ruleset_version`/`evaluated_conditions`/
    `unevaluated_conditions` coverage declaration (`WindowPositionEvidence
    ShadowReportV1`'s own validator forbids `all_accepted=True` alongside any
    `unevaluated_conditions`).
    """
    _validate_window_evidence_pairing_tolerance(envelope_reconcile_tol_m=tol.envelope_reconcile_tol_m)
    producer_draw_sha256 = verified_inputs.inputs.producer_draw_sha256
    resolver_inputs_sha256 = verified_inputs.inputs.content_sha256
    ruleset_version = POSITION_EVIDENCE_RULESET_VERSION
    evaluated_conditions = CURRENTLY_EVALUATED_POSITION_EVIDENCE_CONDITIONS
    unevaluated_conditions = CURRENTLY_UNEVALUATED_POSITION_EVIDENCE_CONDITIONS

    try:
        frames_by_input = _build_authoritative_frames(geom, verified_inputs, tol)
    except WindowDirectionBindingError as exc:
        payload = {
            "producer_draw_sha256": producer_draw_sha256, "resolver_inputs_sha256": resolver_inputs_sha256,
            "status": "binding_unavailable", "binding_error_code": exc.code,
            "window_count": len(geom.windows), "decisions": [], "all_accepted": False,
            "ruleset_version": ruleset_version, "evaluated_conditions": list(evaluated_conditions),
            "unevaluated_conditions": list(unevaluated_conditions),
        }
        return WindowPositionEvidenceShadowReportV1(
            producer_draw_sha256=producer_draw_sha256, resolver_inputs_sha256=resolver_inputs_sha256,
            status="binding_unavailable", binding_error_code=exc.code, window_count=len(geom.windows),
            decisions=(), all_accepted=False,
            ruleset_version=ruleset_version, evaluated_conditions=evaluated_conditions,
            unevaluated_conditions=unevaluated_conditions, content_sha256=canonical_sha256(payload),
        )

    sources_by_locator = {row.source_locator: row for row in verified_inputs.inputs.source_windows}
    links = verified_inputs.inputs.claim_links
    catalog = verified_inputs.inputs.source_windows
    facade_segments = tuple(geom.facade_segments)

    def _decision_for(window) -> WindowPositionEvidenceShadowDecisionV1:
        plan_sources, elevation_sources = _window_existence_sources(
            window.id, links=links, sources_by_locator=sources_by_locator,
        )
        return _build_window_position_evidence_shadow_decision(
            window, plan_sources=plan_sources, elevation_sources=elevation_sources,
            frames_by_input=frames_by_input, floors=geom.floors,
            producer_draw_sha256=producer_draw_sha256, resolver_inputs_sha256=resolver_inputs_sha256,
            tolerance_value=WINDOW_EVIDENCE_PAIRING_TOL_M,
            catalog=catalog, facade_segments=facade_segments,
        )

    per_window_decisions = tuple(_decision_for(window) for window in sorted(geom.windows, key=lambda w: w.id))
    decisions = _detect_position_source_reuse(per_window_decisions)
    all_accepted = all(d.decision == "accepted" for d in decisions)
    payload = {
        "producer_draw_sha256": producer_draw_sha256, "resolver_inputs_sha256": resolver_inputs_sha256,
        "status": "evaluated", "binding_error_code": None, "window_count": len(decisions),
        "decisions": [d.model_dump(mode="json") for d in decisions], "all_accepted": all_accepted,
        "ruleset_version": ruleset_version, "evaluated_conditions": list(evaluated_conditions),
        "unevaluated_conditions": list(unevaluated_conditions),
    }
    return WindowPositionEvidenceShadowReportV1(
        producer_draw_sha256=producer_draw_sha256, resolver_inputs_sha256=resolver_inputs_sha256,
        status="evaluated", binding_error_code=None, window_count=len(decisions),
        decisions=decisions, all_accepted=all_accepted,
        ruleset_version=ruleset_version, evaluated_conditions=evaluated_conditions,
        unevaluated_conditions=unevaluated_conditions, content_sha256=canonical_sha256(payload),
    )


__all__ = [
    "PositionEvidenceErrorCategory", "POSITION_EVIDENCE_ERROR_CATEGORY", "WindowPositionEvidenceError",
    "position_evidence_error",
    "FloorRingFactV1", "RawProjectionContextV1", "materialize_raw_projection_context",
    "WindowResolverInputsArtifactV2",
    "WindowPositionDecisionV1", "WindowPositionEvidenceArtifactV1", "build_window_position_decision",
    "ArtifactLoader", "WINDOW_RESOLVER_ARTIFACT_LOADER_REGISTRY", "load_window_resolver_artifact",
    # S2
    "WINDOW_EVIDENCE_PAIRING_TOL_M", "WINDOW_EVIDENCE_PAIRING_TOLERANCE_NAME", "PROJECTION_SCOPE_EPSILON_M",
    "Z_DATUM_MODE_WORLD_Z", "PAIRING_AMBIGUITY_EPSILON_M",
    "AuthoritativeViewProjectionFrameV2", "AdvisoryViewProjectionFrameV1", "ProjectedAlongEvidenceV1",
    "project_window_source_along", "resolve_elevation_source_floor_scope",
    "WindowPositionEvidenceShadowDecisionV1", "WindowPositionEvidenceShadowReportV1",
    "compute_window_position_evidence_shadow",
    # MAJOR-B1 (§5.3 conditions 2/3/4 + coverage declaration)
    "POSITION_EVIDENCE_CONDITION_DISTANCE_TOLERANCE", "POSITION_EVIDENCE_CONDITION_UNIQUE_MUTUAL_NEAREST",
    "POSITION_EVIDENCE_CONDITION_AMBIGUITY_MARGIN", "POSITION_EVIDENCE_CONDITION_SOURCE_NOT_REUSED",
    "POSITION_EVIDENCE_CONDITION_CLAIM_CONSISTENCY", "POSITION_EVIDENCE_CONDITION_SCOPE_RESOLUTION",
    "ALL_POSITION_EVIDENCE_CONDITIONS", "POSITION_EVIDENCE_RULESET_VERSION",
    "CURRENTLY_EVALUATED_POSITION_EVIDENCE_CONDITIONS", "CURRENTLY_UNEVALUATED_POSITION_EVIDENCE_CONDITIONS",
]
