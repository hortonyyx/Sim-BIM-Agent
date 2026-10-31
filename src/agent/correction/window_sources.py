"""B5 Phase-A source trust and current-ring direction bindings.

This module is deliberately the sole production owner of the source locator
catalogue and the ephemeral, ring-dependent Va elevation bindings.  It imports
no judge code: score bindings are an oracle for tests only.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Mapping, Union

from pydantic import AllowInfNan, BaseModel, ConfigDict, Field, Strict, StringConstraints, model_validator

from src.agent.correction import facade_convention
from src.agent.correction.claims import CLAIMS_VOCAB_VERSION, WINDOW_CLAIMS
from src.agent.correction.facade_applicability import ElevationViewBindingV1 as VaElevationViewBindingV1
from src.agent.correction.facade_visibility import FacadeVisibilityInvariantError, VisibilityTolerances, validate_materialized_facade_segments
from src.agent.correction.footprint import floor_footprint_fingerprint
from src.agent.correction.schema import CorrectedGeometryV3
from src.agent.execution.view_manifest import RequiredViewEntry, ViewManifest
from src.agent.reading import parse_reading_view
from src.agent.reading.contract import (
    READING_PRODUCT_CONTRACT,
    identify_reading_contract,
)

WINDOW_RESOLVER_INPUT_SCHEMA_VERSION = "1"
SOURCE_LOCATOR_PREFIX = "src:"
CURRENT_RING_BINDING_HELPER_VERSION = "window_direction_frame_v1"

FiniteFloat = Annotated[float, Strict(), AllowInfNan(False)]
StableId = Annotated[str, Strict(), StringConstraints(min_length=1)]
Hex64 = Annotated[str, Strict(), StringConstraints(pattern=r"^[0-9a-f]{64}$")]
SourceLocator = Annotated[str, Strict(), StringConstraints(pattern=r"^src:[0-9a-f]{64}$")]
ClaimName = Literal["existence", "host", "along", "width", "sill", "head", "appearance"]
CardinalFamily = Literal["North", "South", "East", "West"]
_CFG = ConfigDict(extra="forbid", frozen=True, strict=True)
# F-9 route② S1 (2026-08-11): _AXIS/_BASE_SIGN used to be local literal
# tables here; both call sites now go through `facade_convention` (single,
# gt-free source — see that module's docstring). Kept as no-op removed names
# intentionally absent, not aliased, so a reintroduced local table would be a
# NameError, not a silent shadow.


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _without_hash(model: BaseModel) -> dict:
    data = model.model_dump(mode="json")
    data.pop("content_sha256", None)
    return data


# F-7 (2026-08-05): every window-source rejection is either the correction
# LLM's fault (it cited an observation reference it invented, mis-scoped, or
# was not entitled to use) or an upstream input-integrity fault (the reading
# artifact / view manifest / accepted-attempt bytes do not line up with each
# other — no resample of the correction draw can fix that). The caller that
# turns a rejection into either an archived failed attempt + blind resample
# (model's fault) or a hard, non-retried crash (input's fault) must be able
# to tell the two apart WITHOUT parsing the message text or the `code`
# string — `code` values are reused across both categories at different call
# sites, so only the throw site itself knows which one applies. `category`
# is therefore a required keyword: a raise site that forgets to classify
# itself fails loudly (TypeError) instead of silently defaulting into either
# bucket.
WindowSourceErrorCategory = Literal["model_draw_error", "input_integrity_error"]


class WindowResolverInputError(ValueError):
    """Typed, stable B5 draw/source-input rejection.

    ``category`` distinguishes a correction-draw mistake (archive as a
    failed attempt, blind-resample) from an input-integrity fault (hard
    crash, resampling cannot help) — see the module-level note above.
    """

    def __init__(
        self,
        code: str,
        context: Mapping[str, object] | None = None,
        *,
        category: WindowSourceErrorCategory,
    ):
        self.code = code
        self.category = category
        self.context = dict(context or {})
        super().__init__(f"{code}: {self.context}")


class SourceIntervalV1(BaseModel):
    model_config = _CFG
    lo: FiniteFloat
    hi: FiniteFloat

    @model_validator(mode="after")
    def _ordered(self):
        if self.lo >= self.hi:
            raise ValueError("source interval requires lo < hi")
        return self


class PlanSourceWindowV1(BaseModel):
    model_config = _CFG
    channel: Literal["plan"]
    source_locator: SourceLocator
    source_input_id: StableId
    source_output_sha256: Hex64
    observation_id: StableId
    floor_ref: Annotated[int, Strict(), Field(gt=0)]
    world_x_interval: SourceIntervalV1
    world_y_interval: SourceIntervalV1
    positive_claims: tuple[ClaimName, ...]


class ElevationSourceWindowV1(BaseModel):
    model_config = _CFG
    channel: Literal["elevation"]
    source_locator: SourceLocator
    source_input_id: StableId
    source_output_sha256: Hex64
    observation_id: StableId
    local_along_interval: SourceIntervalV1
    local_z_interval: SourceIntervalV1 | None
    positive_claims: tuple[ClaimName, ...]


SourceWindowV1 = Annotated[Union[PlanSourceWindowV1, ElevationSourceWindowV1], Field(discriminator="channel")]


class WindowClaimSourceLinkV1(BaseModel):
    model_config = _CFG
    window_id: StableId
    claim: ClaimName
    source_locator: SourceLocator


class ReadingArtifactIdentityV1(BaseModel):
    model_config = _CFG
    input_id: StableId
    expected_output_id: StableId
    output_sha256: Hex64


class ElevationDirectionFactV1(BaseModel):
    model_config = _CFG
    input_id: StableId
    resolved_building_direction: CardinalFamily
    resolution_source: Literal["manifest_building_axis", "resolved_direction_sidecar"]
    mirrored: Annotated[bool, Strict()]
    local_x_positive: Literal["image_left_to_right", "image_right_to_left"]
    orientation_output_hash: Hex64 | None
    adapter_version: StableId | None
    view_manifest_sha256: Hex64


class WindowResolverInputsV1(BaseModel):
    model_config = _CFG
    schema_version: Literal["1"]
    claims_vocab_version: Literal["1"]
    producer_draw_sha256: Hex64
    view_manifest: ViewManifest
    reading_artifacts: tuple[ReadingArtifactIdentityV1, ...]
    elevation_direction_facts: tuple[ElevationDirectionFactV1, ...]
    source_windows: tuple[SourceWindowV1, ...]
    claim_links: tuple[WindowClaimSourceLinkV1, ...]
    content_sha256: Hex64

    @model_validator(mode="after")
    def _hash(self):
        if tuple(item.input_id for item in self.reading_artifacts) != tuple(sorted(item.input_id for item in self.reading_artifacts)):
            raise ValueError("reading_artifacts must be sorted by input_id")
        if tuple(item.input_id for item in self.elevation_direction_facts) != tuple(sorted(item.input_id for item in self.elevation_direction_facts)):
            raise ValueError("elevation_direction_facts must be sorted by input_id")
        if tuple((item.channel, item.source_input_id, item.observation_id) for item in self.source_windows) != tuple(sorted((item.channel, item.source_input_id, item.observation_id) for item in self.source_windows)):
            raise ValueError("source_windows must be canonical sorted")
        rank = {claim: index for index, claim in enumerate(("existence", "host", "along", "width", "sill", "head", "appearance"))}
        if tuple((item.window_id, rank[item.claim], item.source_locator) for item in self.claim_links) != tuple(sorted((item.window_id, rank[item.claim], item.source_locator) for item in self.claim_links)):
            raise ValueError("claim_links must be canonical sorted")
        if self.content_sha256 != canonical_sha256(_without_hash(self)):
            raise ValueError("content_sha256 does not match canonical resolver inputs")
        return self


class RawReadingArtifactV1(BaseModel):
    """Exact raw reading bytes embedded in the persisted resolver artifact."""

    model_config = _CFG
    input_id: StableId
    raw_bytes: bytes


class WindowResolverInputsArtifactV1(BaseModel):
    """Self-contained attempt artifact used by accepted and integrated loaders.

    ``WindowResolverInputsV1`` remains the stable resolver-content wire and
    hash root.  This outer artifact carries the immutable raw sources needed
    to re-authenticate that wire without adding parameters to the integrated
    verifier's frozen signature.
    """

    model_config = _CFG
    artifact_version: Literal["window_resolver_inputs_v1"]
    inputs: WindowResolverInputsV1
    producer_draw_canonical_bytes: bytes
    raw_view_manifest_bytes: bytes
    raw_reading_artifacts: tuple[RawReadingArtifactV1, ...]
    content_sha256: Hex64

    @model_validator(mode="after")
    def _canonical_and_hashed(self):
        ids = tuple(row.input_id for row in self.raw_reading_artifacts)
        if ids != tuple(sorted(ids)) or len(ids) != len(set(ids)):
            raise ValueError("raw_reading_artifacts must have unique canonical input_id order")
        if self.content_sha256 != canonical_sha256(_without_hash(self)):
            raise ValueError("content_sha256 does not match resolver-input artifact")
        return self


class WindowSourceOfferV1(BaseModel):
    model_config = _CFG
    schema_version: Literal["1"]
    view_manifest_sha256: Hex64
    source_windows: tuple[SourceWindowV1, ...]
    allowed_claims_by_locator: tuple[tuple[SourceLocator, tuple[ClaimName, ...]], ...]
    content_sha256: Hex64

    @model_validator(mode="after")
    def _hash(self):
        if tuple((item.channel, item.source_input_id, item.observation_id) for item in self.source_windows) != tuple(sorted((item.channel, item.source_input_id, item.observation_id) for item in self.source_windows)):
            raise ValueError("source_windows must be canonical sorted")
        if self.content_sha256 != canonical_sha256(_without_hash(self)):
            raise ValueError("content_sha256 does not match canonical source offer")
        return self


@dataclass(frozen=True)
class VerifiedWindowResolverInputs:
    """Opaque-in-practice marker; only builders in this module create it."""

    inputs: WindowResolverInputsV1
    raw_inputs_bytes: bytes
    producer_draw_canonical_bytes: bytes
    raw_view_manifest_bytes: bytes
    raw_reading_artifacts: tuple[tuple[str, bytes], ...]


def build_window_resolver_inputs_artifact(
    verified: VerifiedWindowResolverInputs,
) -> WindowResolverInputsArtifactV1:
    """Bind the resolver wire to the exact producer/manifest/reading bytes."""
    readings = tuple(
        RawReadingArtifactV1(input_id=input_id, raw_bytes=raw)
        for input_id, raw in sorted(verified.raw_reading_artifacts)
    )
    payload = {
        "artifact_version": "window_resolver_inputs_v1",
        "inputs": verified.inputs.model_dump(mode="json"),
        "producer_draw_canonical_bytes": verified.producer_draw_canonical_bytes.decode("utf-8"),
        "raw_view_manifest_bytes": verified.raw_view_manifest_bytes.decode("utf-8"),
        "raw_reading_artifacts": [row.model_dump(mode="json") for row in readings],
    }
    return WindowResolverInputsArtifactV1(
        artifact_version="window_resolver_inputs_v1",
        inputs=verified.inputs,
        producer_draw_canonical_bytes=verified.producer_draw_canonical_bytes,
        raw_view_manifest_bytes=verified.raw_view_manifest_bytes,
        raw_reading_artifacts=readings,
        content_sha256=canonical_sha256(payload),
    )


def serialize_window_resolver_inputs_artifact(
    verified: VerifiedWindowResolverInputs,
) -> bytes:
    return canonical_json_bytes(
        build_window_resolver_inputs_artifact(verified).model_dump(mode="json")
    )


def source_locator(*, input_id: str, observation_id: str, output_sha256: str) -> str:
    return SOURCE_LOCATOR_PREFIX + canonical_sha256({
        "input_id": input_id, "observation_id": observation_id,
        "output_sha256": output_sha256, "schema": "window_source_locator_v1",
    })


def _parse_manifest(raw: bytes) -> ViewManifest:
    try:
        return ViewManifest.model_validate_json(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise WindowResolverInputError("source_identity_invalid", {"artifact": "view_manifest"}, category="input_integrity_error") from exc


def _interval(raw: object, *, observation_id: str, field: str) -> SourceIntervalV1:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise WindowResolverInputError("source_identity_invalid", {"observation_id": observation_id, "field": field}, category="input_integrity_error")
    try:
        return SourceIntervalV1(lo=raw[0], hi=raw[1])
    except ValueError as exc:
        raise WindowResolverInputError("source_identity_invalid", {"observation_id": observation_id, "field": field}, category="input_integrity_error") from exc


def _window_strokes(raw: bytes, entry: RequiredViewEntry):
    try:
        payload = json.loads(raw.decode("utf-8"))
        view = parse_reading_view(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise WindowResolverInputError("source_identity_invalid", {"input_id": entry.input_id, "artifact": "reading"}, category="input_integrity_error") from exc
    output_sha = hashlib.sha256(raw).hexdigest()
    for stroke in view.strokes:
        if stroke.pen != "window":
            continue
        geometry = stroke.geometry
        locator = source_locator(input_id=entry.input_id, observation_id=stroke.id, output_sha256=output_sha)
        if entry.view_type == "plan":
            yield PlanSourceWindowV1(channel="plan", source_locator=locator, source_input_id=entry.input_id,
                source_output_sha256=output_sha, observation_id=stroke.id, floor_ref=entry.floor_ref,
                world_x_interval=_interval(geometry.get("x_range_m"), observation_id=stroke.id, field="x_range_m"),
                world_y_interval=_interval(geometry.get("y_range_m"), observation_id=stroke.id, field="y_range_m"),
                positive_claims=("existence", "host", "along", "width"))
        elif entry.view_type == "elevation":
            # Elevation vertical span (sill→head) lives in the rect's y_range_m
            # (image-local vertical = height); there is no z_range contract field.
            z = geometry.get("y_range_m")
            yield ElevationSourceWindowV1(channel="elevation", source_locator=locator, source_input_id=entry.input_id,
                source_output_sha256=output_sha, observation_id=stroke.id,
                local_along_interval=_interval(geometry.get("x_range_m"), observation_id=stroke.id, field="x_range_m"),
                local_z_interval=None if z is None else _interval(z, observation_id=stroke.id, field="y_range_m"),
                positive_claims=("existence", "along", "width", "sill", "head", "appearance"))


# ── as_drawn 腿的窗源目录（2026-09-08 补窗 S1）────────────────────────────── #
#
# WHY THIS EXISTS: the as_drawn leg's catalog used to be the LEGAL EMPTY SET
# (T2-⑤) — honest only while the projection emitted ``windows=[]``.  Measured
# 2026-09-08: the plan products carry 85/87 opening candidates that are
# consumed ONLY to cut walls, and the projection's ``windows=[]`` is a hard
# literal ⇒ the leg produced a WINDOWLESS building (gt has openings, the model
# had none ⇒ the opening channels score zero, and a no-window envelope has no
# physical meaning for an energy model).  This builder is the as_drawn twin of
# ``_window_strokes`` above: same carrier types, same claim vocabulary, ⛔ no
# second definition of what a window source is.
#
# ⭐ THE CHANNEL SPLIT IS NOT A DESIGN CHOICE HERE — it is what the two
# products actually observe (measured, ``logs/experiments/2026-09-08c_*``):
#   plan      declares ``hypotheses.opening_types`` (window/door/not_opening)
#             and the along-wall span, ⛔ carries NO height;
#   elevation declares ``z_range_m`` (sill→head), ⛔ carries NO door/window
#             classification (``ledger.door_window_classified: false``).
# which is exactly the permission matrix ``_claim_links`` already enforces
# (plan → existence/host/along/width, elevation → …/sill/head/appearance).
#
# ⛔ THICKNESS IS THE DECLARED ONE, NOT THE MEASURED ONE: a wall's cross
# interval uses ``pairs[].matched_declared_mm`` (the callout the drawing
# itself declares), ⛔ not ``spacing_m`` (the pixel-measured 0.2384 against a
# declared 240).  Same ruling as the ladder/footprint work the same day: when
# the drawing declares the number, the declaration is the answer and the ink
# is the observation of it.


def _as_drawn_plan_gap_groups(doc: dict, entry: RequiredViewEntry):
    """Window-typed opening candidates grouped into **physical openings**.

    ⭐ 一个物理洞在墙的【两条面线】上各留一个缺口，而哪两条面线是同一堵墙
    是模型自己的声明（``hypotheses.pairs``）⇒ 同墙两面、span 两端偏差 ≤
    **半堵声明墙厚**的两个候选 = 同一个洞被量了两次（与
    ``derive_match_tolerance_m`` 同一语义：边被放错超过半堵墙就不是同一个洞）。

    组锚 = 组内字典序首个候选的 span —— 确定性、与输入顺序无关。
    每组返回 ``[(candidate, line, partner), …]``，``[0]`` 即幸存记录。
    """
    observations = doc.get("observations") or {}
    lines = {line["id"]: line for line in (observations.get("face_lines") or [])}
    hypotheses = doc.get("hypotheses") or {}
    types = hypotheses.get("opening_types") or {}
    # face line -> (partner line id, declared thickness in metres), from the
    # MODEL's own pairing (``hypotheses.pairs``) — ⛔ never re-paired here:
    # "which two faces are one wall" is the model's call by project rule.
    wall_of: dict[str, tuple[str, float]] = {}
    for pair in hypotheses.get("pairs") or []:
        declared = pair.get("matched_declared_mm") or []
        if not declared:
            continue
        thickness_m = float(min(declared)) / 1000.0
        wall_of[pair["face_a"]] = (pair["face_b"], thickness_m)
        wall_of[pair["face_b"]] = (pair["face_a"], thickness_m)
    window_candidates = []
    for candidate in hypotheses.get("opening_candidates") or []:
        if types.get(candidate.get("id")) != "window":
            continue
        line = lines.get(candidate.get("face_line"))
        partner = wall_of.get(candidate.get("face_line"))
        if line is None or partner is None:
            # An opening on an UNPAIRED face has no wall and therefore no
            # cross interval — loud, ⛔ never dropped silently.
            raise WindowResolverInputError(
                "source_identity_invalid",
                {"input_id": entry.input_id, "observation_id": candidate.get("id"),
                 "reason": "opening candidate on an unpaired face line"},
                category="input_integrity_error",
            )
        if lines.get(partner[0]) is None:
            raise WindowResolverInputError(
                "source_identity_invalid",
                {"input_id": entry.input_id, "observation_id": candidate.get("id"),
                 "reason": "paired face line absent from observations"},
                category="input_integrity_error",
            )
        window_candidates.append((candidate, line, partner, lines[partner[0]]))
    groups: list[list[tuple[dict, dict, tuple[str, float], dict]]] = []
    for item in sorted(window_candidates, key=lambda item: item[0]["id"]):
        candidate, _line, partner, _partner_line = item
        span = _interval(candidate.get("span_m"),
                         observation_id=candidate["id"], field="span_m")
        for group in groups:
            head_candidate, _head_line, head_partner, _head_partner_line = group[0]
            same_wall = ({head_candidate["face_line"], head_partner[0]}
                         == {candidate["face_line"], partner[0]})
            if not same_wall:
                continue
            head_span = _interval(head_candidate.get("span_m"),
                                  observation_id=head_candidate["id"], field="span_m")
            if (abs(float(span.lo) - float(head_span.lo)) <= head_partner[1] / 2.0
                    and abs(float(span.hi) - float(head_span.hi)) <= head_partner[1] / 2.0):
                group.append(item)
                break
        else:
            groups.append([item])
    return groups


def _as_drawn_plan_rows(doc: dict, entry: RequiredViewEntry, output_sha: str):
    """Plan-channel source rows from one ``as_drawn_plan_v2`` product.

    ⭐ 每个**物理洞**一行（不是每条面线一行）：同一堵墙两条面线上的孪生
    缺口在 :func:`_as_drawn_plan_gap_groups` 里收编为一组，只有幸存记录
    （字典序最小的观测 id）进目录 —— 双胞胎并列会让「最近邻」不唯一，
    而配对决策必须唯一（收编账见 :func:`as_drawn_plan_record_folds`）。
    """
    for group in _as_drawn_plan_gap_groups(doc, entry):
        candidate, line, partner, partner_line = group[0]
        centre = (float(line["pos_m"]) + float(partner_line["pos_m"])) / 2.0
        half = partner[1] / 2.0
        cross = SourceIntervalV1(lo=centre - half, hi=centre + half)
        along = _interval(candidate.get("span_m"),
                          observation_id=candidate["id"], field="span_m")
        # ``constant_world_axis`` names the FIXED axis: a line with constant x
        # runs in y, so its openings' along-span is the y interval.
        if line.get("constant_world_axis") == "x":
            world_x, world_y = cross, along
        else:
            world_x, world_y = along, cross
        yield PlanSourceWindowV1(
            channel="plan",
            source_locator=source_locator(input_id=entry.input_id,
                                          observation_id=candidate["id"],
                                          output_sha256=output_sha),
            source_input_id=entry.input_id, source_output_sha256=output_sha,
            observation_id=candidate["id"], floor_ref=entry.floor_ref,
            world_x_interval=world_x, world_y_interval=world_y,
            positive_claims=("existence", "host", "along", "width"),
        )


def as_drawn_plan_record_folds(
    *, manifest, raw_reading_artifacts: Mapping[str, bytes],
) -> tuple[tuple[str, str], ...]:
    """物理洞收编账：``((folded_id, survivor_id), …)``。

    同一物理洞的第二条面线记录不入目录，但**折叠这一动作必须可对账**
    —— 目录里少掉的观测在这里逐条有名有姓（缺席是信号，⛔ 不是静默）。
    """
    folds: list[tuple[str, str]] = []
    required = {entry.input_id: entry for entry in manifest.required_entries()}
    for input_id in sorted(required):
        entry = required[input_id]
        if entry.view_type != "plan":
            continue
        doc = json.loads(raw_reading_artifacts[input_id].decode("utf-8"))
        for group in _as_drawn_plan_gap_groups(doc, entry):
            survivor = group[0][0]["id"]
            for candidate, _line, _partner, _partner_line in group[1:]:
                folds.append((f"{input_id}/{candidate['id']}",
                              f"{input_id}/{survivor}"))
    return tuple(folds)


def _as_drawn_elevation_rows(doc: dict, entry: RequiredViewEntry, output_sha: str):
    """Elevation-channel source rows from one ``as_drawn_elevation_v0`` product.

    ⭐ ``local_along_interval`` stays IMAGE-LOCAL (as the legacy rows do): the
    per-facade flip is applied downstream by ``facade_convention``, ⛔ never
    pre-baked here — one flip rule, one place.
    """
    for opening in doc.get("openings") or []:
        yield ElevationSourceWindowV1(
            channel="elevation",
            source_locator=source_locator(input_id=entry.input_id,
                                          observation_id=opening["id"],
                                          output_sha256=output_sha),
            source_input_id=entry.input_id, source_output_sha256=output_sha,
            observation_id=opening["id"],
            local_along_interval=_interval(opening.get("x_range_m"),
                                           observation_id=opening["id"],
                                           field="x_range_m"),
            local_z_interval=_interval(opening.get("z_range_m"),
                                       observation_id=opening["id"],
                                       field="z_range_m"),
            positive_claims=("existence", "along", "width", "sill", "head",
                             "appearance"),
        )


def build_as_drawn_window_catalog(
    *, manifest: ViewManifest, raw_reading_artifacts: Mapping[str, bytes],
) -> tuple[SourceWindowV1, ...]:
    """``_catalog``'s as_drawn twin — same shape, same validation, same sort."""
    required = {entry.input_id: entry for entry in manifest.required_entries()}
    if set(raw_reading_artifacts) != set(required):
        raise WindowResolverInputError(
            "source_identity_invalid",
            {"reading_inputs": sorted(raw_reading_artifacts),
             "required_inputs": sorted(required)},
            category="input_integrity_error",
        )
    rows: list[SourceWindowV1] = []
    for input_id in sorted(required):
        entry = required[input_id]
        raw = raw_reading_artifacts[input_id]
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WindowResolverInputError(
                "source_identity_invalid",
                {"input_id": input_id, "artifact": "reading"},
                category="input_integrity_error",
            ) from exc
        output_sha = hashlib.sha256(raw).hexdigest()
        if entry.view_type == "plan":
            rows.extend(_as_drawn_plan_rows(doc, entry, output_sha))
        elif entry.view_type == "elevation":
            rows.extend(_as_drawn_elevation_rows(doc, entry, output_sha))
    _validate_catalog(rows, raw_parser=True)
    return tuple(sorted(rows, key=lambda item: (item.channel, item.source_input_id, item.observation_id)))


def _catalog(*, manifest: ViewManifest, raw_reading_artifacts: Mapping[str, bytes]) -> tuple[SourceWindowV1, ...]:
    required = {entry.input_id: entry for entry in manifest.required_entries()}
    if set(raw_reading_artifacts) != set(required):
        raise WindowResolverInputError("source_identity_invalid", {"reading_inputs": sorted(raw_reading_artifacts), "required_inputs": sorted(required)}, category="input_integrity_error")
    rows: list[SourceWindowV1] = []
    for input_id in sorted(required):
        entry = required[input_id]
        if entry.view_type in ("plan", "elevation"):
            rows.extend(_window_strokes(raw_reading_artifacts[input_id], entry))
    _validate_catalog(rows, raw_parser=True)
    return tuple(sorted(rows, key=lambda item: (item.channel, item.source_input_id, item.observation_id)))


def _validate_catalog(rows: tuple[SourceWindowV1, ...] | list[SourceWindowV1], *, raw_parser: bool = False) -> None:
    locators = [item.source_locator for item in rows]
    observations = [(item.source_input_id, item.observation_id) for item in rows]
    if raw_parser and len(observations) != len(set(observations)):
        raise WindowResolverInputError("duplicate_source_observation", category="input_integrity_error")
    if len(locators) != len(set(locators)):
        raise WindowResolverInputError("duplicate_source_locator", category="input_integrity_error")
    if not raw_parser and len(observations) != len(set(observations)):
        raise WindowResolverInputError("duplicate_source_observation", category="input_integrity_error")


def verify_window_resolver_inputs(inputs: WindowResolverInputsV1) -> None:
    """Narrow verifier for already-parsed untrusted input wire (test/loader seam).

    It deliberately returns no verified marker: only the raw-artifact builder
    below can issue one.  Keeping this seam public lets loaders reject a
    syntactically valid but duplicate catalog before any marker exists.
    """
    if inputs.content_sha256 != canonical_sha256(_without_hash(inputs)):
        raise WindowResolverInputError("source_identity_invalid", {"artifact": "resolver_inputs"}, category="input_integrity_error")
    _validate_catalog(inputs.source_windows)
    by_locator = {row.source_locator: row for row in inputs.source_windows}
    for link in inputs.claim_links:
        row = by_locator.get(link.source_locator)
        if row is None:
            raise WindowResolverInputError("source_identity_invalid", {"source_locator": link.source_locator}, category="input_integrity_error")
        entry = inputs.view_manifest.entry_by_input_id(row.source_input_id)
        if not isinstance(entry, RequiredViewEntry):
            raise WindowResolverInputError("source_identity_invalid", {"input_id": row.source_input_id}, category="input_integrity_error")
        # Permission is intentionally checked before declaration/manifest
        # observability: it is a channel hard boundary, not a missing claim.
        permitted = ((row.channel == "plan" and link.claim in {"existence", "host", "along", "width"}) or
                     (row.channel == "elevation" and link.claim in {"existence", "along", "width", "sill", "head", "appearance"}))
        if not permitted:
            raise WindowResolverInputError("claim_permission_invalid", {"window_id": link.window_id, "claim": link.claim}, category="input_integrity_error")
        if link.claim not in row.positive_claims:
            raise WindowResolverInputError("source_claim_undeclared", {"window_id": link.window_id, "claim": link.claim}, category="input_integrity_error")
        if link.claim not in entry.opening_evidence.potentially_observable_claims:
            raise WindowResolverInputError("manifest_claim_not_observable", {"window_id": link.window_id, "claim": link.claim}, category="input_integrity_error")


def catalog_for_contract(
    *, manifest: ViewManifest, raw_reading_artifacts: Mapping[str, bytes],
) -> tuple[SourceWindowV1, ...]:
    """⭐ 窗源目录的【唯一】腿分派点 —— 按产物契约选 legacy / as_drawn 的目录构建器。

    **为什么必须集中在一处（2026-09-08 实测教训）**：
    腿分派最初只加在 `verify_window_resolver_inputs_artifact` 一个地方，
    而 `_catalog(...)` 在本模块里有**五个**重建点。补窗把 as_drawn 目录从
    T2-⑤ 的「合法空集」填成真观测之后，没被分派的那几处立刻现形 ——
    `2_modelling` 的加载边界撞 `source_identity_invalid: {'artifact': 'source_catalog'}`
    （`verify_window_resolver_inputs_against_raw_artifacts`）。
    ⇒ 这正是「修了例子没修那一类」：⛔ 逐点打补丁只会让下一个重建点在更远处爆。

    选腿的键 = 分类器对**产物本身**的判定（项目口径：路由是分类器的判定，
    ⛔ 永不按文件名）。混合契约响亮失败，⛔ 不静默挑一条腿。
    """
    from src.agent.reading.vector_contract import (
        CONTRACT_AS_DRAWN_ELEVATION_V0,
        CONTRACT_AS_DRAWN_PLAN,
        CONTRACT_AS_DRAWN_PLAN_V0,
        classify_vector_json,
    )

    as_drawn_ids = {CONTRACT_AS_DRAWN_PLAN, CONTRACT_AS_DRAWN_PLAN_V0,
                    CONTRACT_AS_DRAWN_ELEVATION_V0}
    contracts: set[str] = set()
    for input_id, raw in sorted(raw_reading_artifacts.items()):
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WindowResolverInputError(
                "source_identity_invalid", {"input_id": input_id, "artifact": "reading"},
                category="input_integrity_error",
            ) from exc
        contracts.add(classify_vector_json(doc).contract_id)
    as_drawn = contracts & as_drawn_ids
    if as_drawn and as_drawn != contracts:
        raise WindowResolverInputError(
            "source_identity_invalid",
            {"artifact": "source_catalog",
             "reason": "mixed reading contracts — cannot pick a catalog leg",
             "contracts": sorted(contracts)},
            category="input_integrity_error",
        )
    if as_drawn:
        return build_as_drawn_window_catalog(
            manifest=manifest, raw_reading_artifacts=raw_reading_artifacts
        )
    return _catalog(manifest=manifest, raw_reading_artifacts=raw_reading_artifacts)


def verify_window_resolver_inputs_against_raw_artifacts(
    inputs: WindowResolverInputsV1,
    *,
    raw_view_manifest_bytes: bytes,
    raw_reading_artifacts: Mapping[str, bytes],
) -> None:
    """Re-authenticate persisted resolver inputs against accepted raw sources."""
    verify_window_resolver_inputs(inputs)
    manifest = _parse_manifest(raw_view_manifest_bytes)
    if manifest != inputs.view_manifest:
        raise WindowResolverInputError(
            "source_identity_invalid", {"artifact": "view_manifest"}, category="input_integrity_error"
        )
    rows = catalog_for_contract(manifest=manifest, raw_reading_artifacts=raw_reading_artifacts)
    if rows != inputs.source_windows:
        raise WindowResolverInputError(
            "source_identity_invalid", {"artifact": "source_catalog"}, category="input_integrity_error"
        )
    facts = _check_direction_facts(
        manifest,
        inputs.elevation_direction_facts,
        raw_reading_artifacts,
    )
    if facts != inputs.elevation_direction_facts:
        raise WindowResolverInputError(
            "direction_fact_invalid", {"artifact": "direction_facts"}, category="input_integrity_error"
        )
    identities = tuple(
        ReadingArtifactIdentityV1(
            input_id=entry.input_id,
            expected_output_id=entry.expected_output_id,
            output_sha256=hashlib.sha256(raw_reading_artifacts[entry.input_id]).hexdigest(),
        )
        for entry in sorted(manifest.required_entries(), key=lambda item: item.input_id)
    )
    if identities != inputs.reading_artifacts:
        raise WindowResolverInputError(
            "source_identity_invalid", {"artifact": "reading_artifacts"}, category="input_integrity_error"
        )


def build_window_source_offer(*, raw_view_manifest_bytes: bytes,
                              raw_reading_artifacts: Mapping[str, bytes]) -> WindowSourceOfferV1:
    manifest = _parse_manifest(raw_view_manifest_bytes)
    rows = catalog_for_contract(manifest=manifest, raw_reading_artifacts=raw_reading_artifacts)
    allowed = tuple((row.source_locator, row.positive_claims) for row in rows)
    payload = {"schema_version": "1", "view_manifest_sha256": manifest.content_sha256,
               "source_windows": [row.model_dump(mode="json") for row in rows],
               "allowed_claims_by_locator": [[loc, list(claims)] for loc, claims in allowed]}
    return WindowSourceOfferV1(schema_version="1", view_manifest_sha256=manifest.content_sha256,
        source_windows=rows, allowed_claims_by_locator=allowed, content_sha256=canonical_sha256(payload))


@dataclass(frozen=True)
class ObservationReferenceCatalogEntry:
    """One legal ``<expected_output_id>/<observation_id>`` a correction draw
    may cite in ``source_ids`` — the model-facing counterpart of a
    :class:`SourceWindowV1` row, with the internal locator hash stripped out
    (the model must never see or copy a ``src:`` hash).

    ``approx_world_along_interval`` (F-9, 2026-08-07): for an ``elevation``
    channel observation only, an ADVISORY pre-flipped world-along interval —
    see :func:`_advisory_elevation_world_frame` for the derivation and why it
    is sound to compute here (before any correction draw exists) without the
    real ``along_origin``. ``None`` for plan-channel rows (already natively
    world-frame, no flip ambiguity to resolve) and whenever the frame is not
    resolvable from the manifest + reading alone (true_azimuth/unknown
    direction, or the reading declares no ``overall`` dimension on this
    facade's along-axis). This field is never consumed by any enforcement
    path — see the module docstring on :func:`build_observation_reference_catalog_from_run`.
    """

    reference: str
    allowed_claims: tuple[str, ...]
    approx_world_along_interval: tuple[float, float] | None = None


def _advisory_elevation_world_frame(entry: RequiredViewEntry, raw_reading_bytes: bytes) -> tuple[int, float] | None:
    """Best-effort ``(sign, along_axis_width_m)`` for translating an elevation
    view's OWN image-local along-facade coordinate into an approximate
    world-oriented coordinate, using only facts knowable before any
    correction draw exists (A3: this run's own view manifest + reading
    artifact — no ``along_origin``, which depends on the correction draw's
    envelope).

    B-1 math (F-9 dispatch, 2026-08-07): the real transform is
    ``world = along_origin + sign * local``, with ``along_origin = lo if
    sign > 0 else hi`` where ``(lo, hi)`` is the facade family's world-along
    extent. Under this project's world-coordinate invariant (origin = the
    overall bounding box's SW corner, #2 in CLAUDE.md §1.5) and the current
    single-shared-footprint-per-floor architecture, ``lo == 0`` for every
    facade family's along-axis, so the extent collapses to ``[0, W]`` where
    ``W`` is that facade's own along-axis total width — which the elevation's
    OWN dimension chain already declares (``role="overall"``) independent of
    any correction draw. The transform therefore reduces to exactly:
    ``world = local`` when ``sign == +1``, or ``world = W - local`` when
    ``sign == -1`` — no ``along_origin`` lookup needed.

    This is advisory prompt content only (never authoritative): the real
    host-resolution transform in `materialize_current_ring_va_elevation_bindings`
    independently re-derives `along_origin` from the correction draw's actual
    envelope and remains the sole enforced source of truth, so a residual
    mismatch between this hint's `W` (read straight off the elevation's own
    dimension chain) and the eventual envelope-derived width (already a
    registered, untouched tolerance — see A5/CLAUDE.md 0.12m debt) cannot
    relax or bypass any check.

    Returns ``None`` (never guesses) when the direction is not resolvable
    without a verified orientation sidecar (true_azimuth/unknown — the same
    condition `derive_manifest_direction_facts` fails closed on) or the
    reading declares no ``overall`` dimension on this facade's along-axis.
    """
    if entry.direction_semantics != "building_axis" or entry.building_view_direction is None:
        return None
    family = entry.building_view_direction
    axis = facade_convention.world_axis(family)
    try:
        reading = parse_reading_view(json.loads(raw_reading_bytes.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    mirrored, local_x_positive = _resolve_facade_flip_fields(reading.facade)
    sign = facade_convention.resolve_sign(family, mirrored=mirrored, local_x_positive=local_x_positive)
    widths = [
        dimension.value_m for dimension in reading.dimensions
        if dimension.role == "overall" and dimension.axis == axis and dimension.value_m is not None
    ]
    if not widths:
        return None
    return sign, widths[0]


def derive_observation_reference_catalog(
    *, raw_view_manifest_bytes: bytes, raw_reading_artifacts: Mapping[str, bytes],
) -> tuple[ObservationReferenceCatalogEntry, ...]:
    """The single, mechanically-derived catalogue of legal observation
    references for this run's reading artifacts.

    Built from the exact same :func:`_catalog` (and therefore the same
    ``source_locator`` computation) that :func:`_translate_observation_reference`
    validates against — the prompt-facing listing and the enforced catalogue
    can never diverge because both are read off this one function's output.

    F-9 (2026-08-07): elevation rows additionally carry an advisory
    pre-flipped ``approx_world_along_interval`` (see
    :func:`_advisory_elevation_world_frame`) so a correction draw citing an
    elevation stroke does not have to mentally apply the North/West mirror
    sign — the model already knows each window's own plan world span; giving
    it the elevation observation's span in the SAME (world) frame lets plain
    numeric-overlap matching land on the window itself instead of its mirror
    twin. This is computed once per elevation ``entry`` and reused across
    every row from that same view (all its stroke rows share the same frame).
    """
    manifest = _parse_manifest(raw_view_manifest_bytes)
    rows = catalog_for_contract(manifest=manifest, raw_reading_artifacts=raw_reading_artifacts)
    entries_by_input = {entry.input_id: entry for entry in manifest.required_entries()}
    expected_output_id_by_input = {entry.input_id: entry.expected_output_id for entry in manifest.required_entries()}
    frame_cache: dict[str, tuple[int, float] | None] = {}
    result = []
    for row in rows:
        hint = None
        if isinstance(row, ElevationSourceWindowV1):
            if row.source_input_id not in frame_cache:
                entry = entries_by_input.get(row.source_input_id)
                frame_cache[row.source_input_id] = (
                    None if entry is None
                    else _advisory_elevation_world_frame(entry, raw_reading_artifacts[row.source_input_id])
                )
            frame = frame_cache[row.source_input_id]
            if frame is not None:
                sign, width = frame
                def _to_world(local: float, *, _sign=sign, _width=width) -> float:
                    return local if _sign > 0 else _width - local
                a = _to_world(row.local_along_interval.lo)
                b = _to_world(row.local_along_interval.hi)
                hint = (min(a, b), max(a, b))
        result.append(ObservationReferenceCatalogEntry(
            reference=f"{expected_output_id_by_input[row.source_input_id]}/{row.observation_id}",
            allowed_claims=row.positive_claims,
            approx_world_along_interval=hint,
        ))
    return tuple(result)


def format_observation_reference_catalog(entries: tuple[ObservationReferenceCatalogEntry, ...]) -> str:
    """Human-readable prompt text for :func:`derive_observation_reference_catalog`.

    Never emits a ``src:`` locator or a 64-hex hash: only the reference form
    the model can actually produce (and its allowed claims) is shown.
    """
    if not entries:
        return "(no window observations found in this run's reading artifacts)"
    lines = [
        f"- {entry.reference}: allowed claims = [{', '.join(entry.allowed_claims)}]"
        + (
            f"; approx world-along interval ≈ [{entry.approx_world_along_interval[0]:.3g}, "
            f"{entry.approx_world_along_interval[1]:.3g}] (informational only, already translated "
            "out of image-local so you can compare it directly against a window's own plan world "
            "span instead of mentally mirroring it — not authoritative, re-verified independently "
            "after your draw)"
            if entry.approx_world_along_interval is not None else ""
        )
        for entry in entries
    ]
    return "\n".join(lines)


def build_observation_reference_catalog_from_run(
    *, run_dir: Path, reading_dir: Path, required_for_v3: bool = False,
) -> str | None:
    """Prompt-facing catalog listing for a run's correction-draw guidance.

    Returns ``None`` when the view manifest (or one of its required reading
    artifacts) is not yet on disk — this is advisory prompt content only;
    the enforcement path (`build_verified_window_inputs_from_run` ->
    `build_verified_window_resolver_inputs` -> `_claim_links`) independently
    re-derives and re-validates everything after the draw regardless of
    whether this listing was shown, so a missing catalog here weakens
    guidance, never the contract.

    Under a v3 target, though, that advisory fallback is a usability trap
    (F-2c/F-7 rework r1, MAJOR ①): a missing catalog leaves the model with no
    guidance, it fills wrong references, and the run burns its resample budget
    surfacing as an opaque failure two stages later — the same silent-degrade
    shape F-3/F-4 hit. Pass ``required_for_v3=True`` to make a non-exportable
    catalog fail immediately and loudly instead: the error names the missing
    file and the stage (0_reading) that should have produced it.
    """
    from src.agent.execution.run_meta import run_meta_path
    from src.agent.execution.view_manifest import VIEW_MANIFEST_NAME

    def _unavailable(**context) -> None:
        raise WindowResolverInputError(
            "observation_reference_catalog_unavailable",
            {**context, "produced_by_stage": "0_reading"},
            category="input_integrity_error",
        )

    manifest_path = run_meta_path(Path(run_dir), VIEW_MANIFEST_NAME)
    if not manifest_path.is_file():
        if required_for_v3:
            _unavailable(missing_artifact=str(manifest_path), artifact="view_manifest")
        return None
    raw_manifest = manifest_path.read_bytes()
    try:
        manifest = _parse_manifest(raw_manifest)
    except WindowResolverInputError:
        if required_for_v3:
            raise
        return None
    raw_readings: dict[str, bytes] = {}
    for entry in manifest.required_entries():
        path = Path(reading_dir) / f"{entry.expected_output_id}.json"
        if not path.is_file():
            if required_for_v3:
                _unavailable(
                    missing_artifact=str(path), artifact="reading",
                    expected_output_id=entry.expected_output_id,
                )
            return None
        raw_readings[entry.input_id] = path.read_bytes()
    try:
        entries = derive_observation_reference_catalog(
            raw_view_manifest_bytes=raw_manifest, raw_reading_artifacts=raw_readings,
        )
    except WindowResolverInputError:
        if required_for_v3:
            raise
        return None
    return format_observation_reference_catalog(entries)


def _resolve_facade_flip_fields(facade) -> tuple[bool, str]:
    """Shared coercion: a reading's own ``facade.mirrored``/``local_x_positive``
    into the ``(mirrored: bool, local_x_positive: str)`` pair the world-frame
    convention consumes.

    Extracted out of :func:`derive_manifest_direction_facts` (B-9/F-9,
    2026-08-07) so the advisory catalog hint below computes the flip from the
    exact same rule instead of a second hand-copied inline version that could
    silently drift from it. Behaviour-preserving: a non-``bool`` ``mirrored``
    (e.g. the schema's legacy string literals ``"true"``/``"false"``/``"unknown"``)
    still defaults to not-mirrored, same as before this extraction.

    F-9 route② MINOR-3 debt (2026-08-11, sol verdict): this coercion is a
    DIFFERENT lenient policy than ``facade.py::_is_mirrored`` (which accepts
    a case-insensitive ``"true"`` STRING as mirrored=True; this function
    only ever honors a real ``bool`` and treats every string, including
    ``"true"``, as not-mirrored) and different again from
    ``facade_convention.normalize_mirror_flag``'s strict typed adapter. Same
    input, three different answers depending which one reads it -- a real
    latent defect, registered (not silently unified) here because unifying
    it would change behavior, which S1's "行为保持不变" rule forbids.
    Pinned as an executable fact by
    ``tests/test_f9_route2_s1_convention_truth.py::
    test_minor3_legacy_mirror_coercions_disagree_is_a_pinned_debt_fact``.
    S3/S4's new live v3 raw-citation boundary must use only the strict
    adapter and fail closed.
    """
    mirrored = False
    local_x_positive = "image_left_to_right"
    if facade is not None:
        if isinstance(facade.mirrored, bool):
            mirrored = facade.mirrored
        if facade.local_x_positive in {"image_left_to_right", "image_right_to_left"}:
            local_x_positive = facade.local_x_positive
    return mirrored, local_x_positive


def derive_manifest_direction_facts(
    *,
    raw_view_manifest_bytes: bytes,
    raw_reading_artifacts: Mapping[str, bytes],
) -> tuple[ElevationDirectionFactV1, ...]:
    """Derive ring-free facts for manifest-declared building-axis elevations.

    True-azimuth/unknown entries require the separately verified orientation
    sidecar and therefore fail closed here instead of inventing a direction.
    """
    manifest = _parse_manifest(raw_view_manifest_bytes)
    facts: list[ElevationDirectionFactV1] = []
    for entry in manifest.required_entries():
        if entry.view_type != "elevation":
            continue
        if entry.direction_semantics != "building_axis" or entry.building_view_direction is None:
            raise WindowResolverInputError(
                "direction_fact_invalid",
                {"input_id": entry.input_id, "reason": "verified_orientation_sidecar_required"},
                category="input_integrity_error",
            )
        try:
            reading = parse_reading_view(
                json.loads(raw_reading_artifacts[entry.input_id].decode("utf-8"))
            )
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise WindowResolverInputError(
                "direction_fact_invalid", {"input_id": entry.input_id, "artifact": "reading"}, category="input_integrity_error"
            ) from exc
        mirrored, local_x_positive = _resolve_facade_flip_fields(reading.facade)
        facts.append(ElevationDirectionFactV1(
            input_id=entry.input_id,
            resolved_building_direction=entry.building_view_direction,
            resolution_source="manifest_building_axis",
            mirrored=mirrored,
            local_x_positive=local_x_positive,
            orientation_output_hash=None,
            adapter_version=None,
            view_manifest_sha256=manifest.content_sha256,
        ))
    return tuple(sorted(facts, key=lambda fact: fact.input_id))


def build_verified_window_inputs_from_run(
    *,
    producer_draw: CorrectedGeometryV3,
    run_dir: Path,
    reading_dir: Path,
) -> VerifiedWindowResolverInputs:
    """Production run-directory adapter for the unique raw-input builder."""
    from src.agent.execution.run_meta import run_meta_path
    from src.agent.execution.view_manifest import VIEW_MANIFEST_NAME

    verify_reading_stage_root_against_accepted_attempt(run_dir, reading_dir)
    manifest_path = run_meta_path(Path(run_dir), VIEW_MANIFEST_NAME)
    if not manifest_path.is_file():
        raise WindowResolverInputError(
            "source_identity_invalid", {"artifact": "view_manifest", "path": str(manifest_path)}, category="input_integrity_error"
        )
    raw_manifest = manifest_path.read_bytes()
    manifest = _parse_manifest(raw_manifest)
    raw_readings: dict[str, bytes] = {}
    for entry in manifest.required_entries():
        path = Path(reading_dir) / f"{entry.expected_output_id}.json"
        if not path.is_file():
            raise WindowResolverInputError(
                "source_identity_invalid", {"input_id": entry.input_id, "path": str(path)}, category="input_integrity_error"
            )
        raw_readings[entry.input_id] = path.read_bytes()
    facts = derive_manifest_direction_facts(
        raw_view_manifest_bytes=raw_manifest,
        raw_reading_artifacts=raw_readings,
    )
    return build_verified_window_resolver_inputs(
        producer_draw=producer_draw,
        raw_view_manifest_bytes=raw_manifest,
        raw_reading_artifacts=raw_readings,
        elevation_direction_facts=facts,
    )


def verify_reading_stage_root_against_accepted_attempt(
    run_dir: Path,
    reading_dir: Path,
) -> None:
    """Bind the stage-root reading mirrors to 0_reading's accepted attempt.

    The accepted draw is archived under ``attempts/NNN/output.json``; the
    stage-root ``*_view.json`` files are convenience mirrors of that same draw.
    Two independent anti-swap checks anchor the accepted product: (1) the
    archived ``output.json`` bytes must still hash to the manifest's
    ``output_hash`` (the archive itself was not swapped); (2) the stage-root
    mirrors must canonicalize to the SAME payload as that verified archive (the
    mirrors were not swapped or diverged — the only reason this function exists).

    The accepted product's OWN contract decides the mirror shape (reuses
    ``identify_reading_contract`` — no second shape detector): the isolated
    merge archives an envelope ``{"views": {stem: ...}}`` while the legacy flat
    writer archives ``{stem: ...}``, so the reconstructed mirrors are wrapped in
    the envelope only when the accepted product is the v2 envelope. Both paths
    then compare a canonical hash, so an isolated run's mirrors rebuild
    byte-equal to its envelope accepted product and a flat run is unchanged.
    Runs without an accepted reading attempt keep the standalone behavior.
    """
    from src.agent.execution.manifest import hash_bytes, load_run_manifest

    manifest = load_run_manifest(Path(run_dir))
    if manifest is None:
        return
    accepted = manifest.accepted("0_reading")
    if accepted is None:
        return
    attempt_path = (
        Path(run_dir)
        / "0_reading"
        / "attempts"
        / f"{accepted.accepted_attempt:03d}"
        / "output.json"
    )
    if not attempt_path.is_file():
        raise WindowResolverInputError(
            "source_identity_invalid",
            {"artifact": "accepted_reading", "path": str(attempt_path)},
            category="input_integrity_error",
        )
    try:
        accepted_bytes = attempt_path.read_bytes()
        if hash_bytes(accepted_bytes) != accepted.output_hash:
            raise WindowResolverInputError(
                "source_identity_invalid",
                {"artifact": "accepted_reading", "reason": "output_hash_mismatch"},
                category="input_integrity_error",
            )
        accepted_payload = json.loads(accepted_bytes)
        # Reconstruct the stage-root mirrors keyed by each *_view.json stem.
        current = {
            path.stem: json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(Path(reading_dir).glob("*_view.json"))
        }
        # Match the accepted product's own contract shape: wrap the mirrors in
        # the v2 envelope only when the accepted archive IS that envelope.
        if identify_reading_contract(accepted_payload).contract_id == READING_PRODUCT_CONTRACT:
            current = {"views": current}
        current_hash = canonical_sha256(current)
        accepted_hash = canonical_sha256(accepted_payload)
    except WindowResolverInputError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise WindowResolverInputError(
            "source_identity_invalid", {"artifact": "reading_stage_root"}, category="input_integrity_error"
        ) from exc
    if current_hash != accepted_hash:
        raise WindowResolverInputError(
            "source_identity_invalid",
            {"artifact": "reading_stage_root", "reason": "accepted_attempt_mismatch"},
            category="input_integrity_error",
        )


def _check_direction_facts(manifest: ViewManifest, facts: tuple[ElevationDirectionFactV1, ...],
                           raw_reading_artifacts: Mapping[str, bytes]) -> tuple[ElevationDirectionFactV1, ...]:
    expected = {entry.input_id: entry for entry in manifest.required_entries() if entry.view_type == "elevation"}
    got = {fact.input_id: fact for fact in facts}
    if len(got) != len(facts) or set(got) != set(expected):
        raise WindowResolverInputError("direction_fact_invalid", {"declared": sorted(got), "required": sorted(expected)}, category="input_integrity_error")
    out = []
    for input_id, entry in expected.items():
        fact = got[input_id]
        if fact.view_manifest_sha256 != manifest.content_sha256:
            raise WindowResolverInputError("direction_fact_invalid", {"input_id": input_id, "field": "view_manifest_sha256"}, category="input_integrity_error")
        try:
            reading = parse_reading_view(json.loads(raw_reading_artifacts[input_id].decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise WindowResolverInputError("direction_fact_invalid", {"input_id": input_id, "artifact": "reading"}, category="input_integrity_error") from exc
        if reading.facade is not None:
            mirrored = reading.facade.mirrored
            if isinstance(mirrored, bool) and (fact.mirrored != mirrored or fact.local_x_positive != reading.facade.local_x_positive):
                raise WindowResolverInputError("direction_fact_invalid", {"input_id": input_id, "field": "reading_facade"}, category="input_integrity_error")
        if fact.resolution_source == "manifest_building_axis":
            if (entry.direction_semantics != "building_axis" or
                    fact.resolved_building_direction != entry.building_view_direction or
                    fact.orientation_output_hash is not None or fact.adapter_version is not None):
                raise WindowResolverInputError("direction_fact_invalid", {"input_id": input_id}, category="input_integrity_error")
        elif (entry.direction_semantics not in ("true_azimuth", "unknown") or
              fact.orientation_output_hash is None or fact.adapter_version is None):
            raise WindowResolverInputError("direction_fact_invalid", {"input_id": input_id}, category="input_integrity_error")
        out.append(fact)
    return tuple(sorted(out, key=lambda item: item.input_id))


def _producer_preflight(producer: CorrectedGeometryV3) -> None:
    # These two are model_draw_error (not input_integrity_error): a
    # correction draw pre-filling deterministic-core-only fields is the LLM
    # writing outside its scope, not a broken upstream artifact — retrying
    # the draw can fix it.
    #
    # F-16 follow-up (2026-08-08, orchestrator interface sweep §6 摊一
    # Step 1): the nested (per-window) half of this check used to hardcode
    # `window.facade_segment_id is not None` directly — independently of
    # the top-level `producer.facade_segments` check right next to it, and
    # of the SAME marker vocab.producer_facing_json_schema already reads to
    # strip this exact field from the model-facing schema. Deriving both
    # halves from schema.{draw_forbidden_field_names,
    # nested_draw_forbidden_fields} closes that drift the same way F-15's
    # original fix closed it for top-level-only fields.
    #
    # Deliberately NOT `nested_draw_derived_fields` too (which would add
    # `WindowV3.floor`, §6 摊一 Step 2): `producer` here is an ALREADY
    # VALIDATED instance, and `floor` is unconditionally populated by the
    # schema's own validator by the time any instance exists — checking
    # "is it None" here would misfire on every valid v3 draw, not just a
    # model-authored one. The only place that distinction is still
    # observable is the raw payload dict, before `model_validate` ever
    # runs — see `parse.parse_correction_draw`'s b2 gate, which is where
    # that check actually lives.
    from src.agent.correction.schema import draw_forbidden_field_names, nested_draw_forbidden_fields
    if any(getattr(producer, name) for name in draw_forbidden_field_names(type(producer))):
        raise WindowResolverInputError("producer_segment_ref_prefilled", category="model_draw_error")
    for list_field, marked_names in nested_draw_forbidden_fields(type(producer)).items():
        for item in getattr(producer, list_field):
            if any(getattr(item, name) is not None for name in marked_names):
                raise WindowResolverInputError("producer_segment_ref_prefilled", category="model_draw_error")
    if any(isinstance(row, dict) and row.get("kind") == "window_host_resolution"
           for rows in (producer.corrections, producer.conflicts, producer.unsupported) for row in rows):
        raise WindowResolverInputError("producer_resolver_audit_prefilled", category="model_draw_error")


_SOURCE_LOCATOR_PATTERN = re.compile(r"^src:[0-9a-f]{64}$")


def _translate_observation_reference(
    reference: str,
    *,
    rows: tuple[SourceWindowV1, ...],
    manifest: ViewManifest,
    window_id: str,
    claim: str,
) -> str:
    """Translate a correction draw's cited source into its internal locator.

    F-7 (2026-08-05): a correction draw cannot see or compute the 64-hex
    locator hash (it embeds the reading-artifact byte hash, which the LLM has
    no access to) — so it must cite what it CAN see: the reading vector
    file's own name and the observation ``id`` printed in that file's
    ``strokes`` array, as ``<expected_output_id>/<observation_id>`` (e.g.
    ``1f_view/S11``). This is the sole translation point; the resulting
    locator is then validated exactly as before by every existing
    ``_claim_links`` check below — nothing downstream is relaxed.

    Two forms are accepted:
      1. An already-valid ``src:<64hex>`` locator — passed through unchanged
         (backward compatibility for callers/fixtures that already carry a
         real locator, e.g. computed directly via :func:`source_locator`).
      2. ``<expected_output_id>/<observation_id>`` — the only form a live
         draw can produce.
    Anything else (no ``/``, an unknown view name, or an unknown observation
    id within a known view) is a named, distinct model-draw error — never a
    silent guess at which view/observation was meant.
    """
    if _SOURCE_LOCATOR_PATTERN.match(reference):
        return reference
    parts = reference.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise WindowResolverInputError(
            "observation_reference_ambiguous",
            {"window_id": window_id, "claim": claim, "source_ids": reference},
            category="model_draw_error",
        )
    view_name, observation_id = parts
    input_id = manifest.expected_output_ids().get(view_name)
    if input_id is None:
        raise WindowResolverInputError(
            "observation_reference_view_unknown",
            {"window_id": window_id, "claim": claim, "expected_output_id": view_name},
            category="model_draw_error",
        )
    for row in rows:
        if row.source_input_id == input_id and row.observation_id == observation_id:
            return row.source_locator
    raise WindowResolverInputError(
        "observation_reference_observation_unknown",
        {"window_id": window_id, "claim": claim, "expected_output_id": view_name, "observation_id": observation_id},
        category="model_draw_error",
    )


def _claim_links(producer: CorrectedGeometryV3, rows: tuple[SourceWindowV1, ...], manifest: ViewManifest) -> tuple[WindowClaimSourceLinkV1, ...]:
    by_locator = {row.source_locator: row for row in rows}
    used_existence: set[str] = set()
    links: list[WindowClaimSourceLinkV1] = []
    for window in producer.windows:
        provenance = window.provenance or {}
        existence = provenance.get("existence")
        if existence is None or not existence.source_ids or existence.provenance == "assumed":
            raise WindowResolverInputError("source_identity_invalid", {"window_id": window.id, "claim": "existence"}, category="model_draw_error")
        for claim, detail in provenance.items():
            for reference in detail.source_ids:
                locator = _translate_observation_reference(
                    reference, rows=rows, manifest=manifest, window_id=window.id, claim=claim,
                )
                source = by_locator.get(locator)
                if source is None:
                    raise WindowResolverInputError("source_identity_invalid", {"window_id": window.id, "source_locator": locator}, category="model_draw_error")
                if claim not in source.positive_claims:
                    raise WindowResolverInputError("source_claim_undeclared", {"window_id": window.id, "claim": claim}, category="model_draw_error")
                entry = manifest.entry_by_input_id(source.source_input_id)
                if not isinstance(entry, RequiredViewEntry):
                    raise WindowResolverInputError("source_identity_invalid", {
                        "window_id": window.id, "input_id": source.source_input_id,
                    }, category="input_integrity_error")
                if claim not in entry.opening_evidence.potentially_observable_claims:
                    raise WindowResolverInputError("manifest_claim_not_observable", {"window_id": window.id, "claim": claim}, category="model_draw_error")
                permitted = ((source.channel == "plan" and claim in {"existence", "host", "along", "width"}) or
                             (source.channel == "elevation" and claim in {"existence", "along", "width", "sill", "head", "appearance"}))
                if not permitted:
                    raise WindowResolverInputError("claim_permission_invalid", {"window_id": window.id, "claim": claim}, category="model_draw_error")
                if claim == "existence":
                    if locator in used_existence:
                        raise WindowResolverInputError("source_identity_invalid", {"source_locator": locator, "reason": "multiple_windows"}, category="model_draw_error")
                    used_existence.add(locator)
                links.append(WindowClaimSourceLinkV1(window_id=window.id, claim=claim, source_locator=locator))
    order = {claim: index for index, claim in enumerate(("existence", "host", "along", "width", "sill", "head", "appearance"))}
    return tuple(sorted(links, key=lambda link: (link.window_id, order[link.claim], link.source_locator)))


def _check_floor_order(manifest: ViewManifest, producer: CorrectedGeometryV3,
                       rows: tuple[SourceWindowV1, ...], links: tuple[WindowClaimSourceLinkV1, ...]) -> None:
    refs = sorted({entry.floor_ref for entry in manifest.required_entries() if entry.view_type == "plan"})
    # F-2c/F-7 rework r1 (MAJOR ②): this was one compound `A or B` raise, but the
    # two halves are decided by different actors and must classify separately —
    # misclassifying B as input_integrity made a model floor-count mistake hard-
    # crash instead of archive+resample.
    #   A) manifest plan floor_refs not contiguous 1..N — a manifest (upstream)
    #      defect; no resample fixes it. Redundant in practice with the manifest's
    #      own validator (view_manifest._contiguous plan-ref check), kept only as
    #      a defensive re-check honest about its (upstream) deciding factor.
    #   B) the model drew a different NUMBER of floors than the manifest declares
    #      — `producer.floors` is THIS draw's output, so a fresh draw can fix it.
    if refs != list(range(1, len(refs) + 1)):
        raise WindowResolverInputError("manifest_floor_ref_non_contiguous", category="input_integrity_error")
    if len(refs) != len(producer.floors):
        raise WindowResolverInputError(
            "producer_floor_count_mismatch",
            {"manifest_floor_count": len(refs), "producer_floor_count": len(producer.floors)},
            category="model_draw_error",
        )
    floor_by_id = {floor.id: floor for floor in producer.floors}
    rank = {floor.id: index for index, floor in enumerate(sorted(producer.floors, key=lambda floor: floor.z_floor), start=1)}
    windows = {window.id: window for window in producer.windows}
    for row in rows:
        if isinstance(row, PlanSourceWindowV1):
            for link in (link for link in links if link.source_locator == row.source_locator):
                if rank[windows[link.window_id].floor_id] != row.floor_ref:
                    raise WindowResolverInputError("floor_ref_window_mismatch", {"window_id": link.window_id}, category="model_draw_error")
        elif isinstance(row, ElevationSourceWindowV1) and row.local_z_interval is not None:
            candidates = [floor for floor in producer.floors
                          if row.local_z_interval.lo >= floor.z_floor and row.local_z_interval.hi <= floor.z_floor + floor.ceiling_height]
            for link in (link for link in links if link.source_locator == row.source_locator):
                if len(candidates) != 1 or windows[link.window_id].floor_id != candidates[0].id:
                    raise WindowResolverInputError("elevation_floor_mismatch", {"window_id": link.window_id,
                        "candidate_floors": [floor.id for floor in candidates]}, category="model_draw_error")
    if set(floor_by_id) != set(rank):  # defensive, makes the mapping invariant explicit
        # Both sets derive from `producer.floors`, so if this ever fires it is a
        # model-side inconsistency (the draw's floors) — model_draw_error, not an
        # upstream-integrity fault. (Defensive: under the floor-id uniqueness the
        # schema enforces, this branch is unreachable.)
        raise WindowResolverInputError("floor_ref_window_mismatch", category="model_draw_error")


def _assemble_verified_resolver_inputs(
    *, producer_draw: CorrectedGeometryV3, manifest: ViewManifest,
    rows: tuple[SourceWindowV1, ...], facts: tuple[ElevationDirectionFactV1, ...],
    raw_view_manifest_bytes: bytes, raw_reading_artifacts: Mapping[str, bytes],
) -> VerifiedWindowResolverInputs:
    """The CANONICAL assembly half of ``build_verified_window_resolver_inputs``
    (W-1 T2-⑤, 2026-09-07): everything from the producer preflight onward, as
    one pure function of (producer, manifest, rows, facts, raw bytes).

    Extracted — not copied — so the legacy builder and the as_drawn builder
    (``build_verified_window_inputs_as_drawn``) mint the SAME carrier type
    through the SAME fingerprint rules (producer-draw canonical bytes, claim
    links, floor order, content hash): one definition of "what a verified
    window-inputs marker is", two catalog/fact sources.  ⛔ No behavior change
    for the legacy path — its assembly is this function, byte for byte.
    """
    producer = CorrectedGeometryV3.model_validate(producer_draw.model_dump(mode="json"))
    _producer_preflight(producer)
    links = _claim_links(producer, rows, manifest)
    _check_floor_order(manifest, producer, rows, links)
    producer_bytes = canonical_json_bytes(producer.model_dump(mode="json"))
    artifacts = tuple(ReadingArtifactIdentityV1(input_id=entry.input_id, expected_output_id=entry.expected_output_id,
        output_sha256=hashlib.sha256(raw_reading_artifacts[entry.input_id]).hexdigest())
        for entry in sorted(manifest.required_entries(), key=lambda item: item.input_id))
    payload = {"schema_version": "1", "claims_vocab_version": CLAIMS_VOCAB_VERSION,
               "producer_draw_sha256": hashlib.sha256(producer_bytes).hexdigest(),
               "view_manifest": manifest.model_dump(mode="json"),
               "reading_artifacts": [item.model_dump(mode="json") for item in artifacts],
               "elevation_direction_facts": [item.model_dump(mode="json") for item in facts],
               "source_windows": [item.model_dump(mode="json") for item in rows],
               "claim_links": [item.model_dump(mode="json") for item in links]}
    inputs = WindowResolverInputsV1(schema_version="1", claims_vocab_version=CLAIMS_VOCAB_VERSION,
        producer_draw_sha256=payload["producer_draw_sha256"], view_manifest=manifest,
        reading_artifacts=artifacts, elevation_direction_facts=facts, source_windows=rows,
        claim_links=links, content_sha256=canonical_sha256(payload))
    raw_inputs = canonical_json_bytes(inputs.model_dump(mode="json"))
    return VerifiedWindowResolverInputs(inputs=inputs, raw_inputs_bytes=raw_inputs,
        producer_draw_canonical_bytes=producer_bytes, raw_view_manifest_bytes=raw_view_manifest_bytes,
        raw_reading_artifacts=tuple((key, raw_reading_artifacts[key]) for key in sorted(raw_reading_artifacts)))


def build_verified_window_resolver_inputs(*, producer_draw: CorrectedGeometryV3,
                                          raw_view_manifest_bytes: bytes,
                                          raw_reading_artifacts: Mapping[str, bytes],
                                          elevation_direction_facts: tuple[ElevationDirectionFactV1, ...]) -> VerifiedWindowResolverInputs:
    manifest = _parse_manifest(raw_view_manifest_bytes)
    rows = catalog_for_contract(manifest=manifest, raw_reading_artifacts=raw_reading_artifacts)
    facts = _check_direction_facts(manifest, elevation_direction_facts, raw_reading_artifacts)
    return _assemble_verified_resolver_inputs(
        producer_draw=producer_draw, manifest=manifest, rows=rows, facts=facts,
        raw_view_manifest_bytes=raw_view_manifest_bytes,
        raw_reading_artifacts=raw_reading_artifacts,
    )


# ── the as_drawn leg's window-inputs builder (W-1 T2-⑤, 2026-09-07) ────────── #
def derive_manifest_direction_facts_as_drawn(
    *,
    raw_view_manifest_bytes: bytes,
    raw_reading_artifacts: Mapping[str, bytes],
) -> tuple[ElevationDirectionFactV1, ...]:
    """``derive_manifest_direction_facts`` for as_drawn elevation products.

    Same manifest-side rule (fail-closed on anything but a ``building_axis``
    elevation with a declared ``building_view_direction``), but the flip fields
    come from the product's OWN shape: an ``as_drawn_elevation_v0`` declares a
    ``facade_label`` (the B3 semantic slot) and NO facade-mirror object, so the
    ``(mirrored, local_x_positive)`` pair is the no-declaration default of
    ``_resolve_facade_flip_fields(None)`` — applied, ⛔ never inferred from
    ink.  The emptiness that matters downstream is carried as data on the fact
    (``resolution_source="manifest_building_axis"``), and the wiring-side debt
    ``WINDOW_EVIDENCE_ON_CHAIN_NOT_ON_LEDGER`` accounts for the channel split.
    """
    manifest = _parse_manifest(raw_view_manifest_bytes)
    facts: list[ElevationDirectionFactV1] = []
    for entry in manifest.required_entries():
        if entry.view_type != "elevation":
            continue
        if entry.direction_semantics != "building_axis" or entry.building_view_direction is None:
            raise WindowResolverInputError(
                "direction_fact_invalid",
                {"input_id": entry.input_id, "reason": "verified_orientation_sidecar_required"},
                category="input_integrity_error",
            )
        try:
            doc = json.loads(raw_reading_artifacts[entry.input_id].decode("utf-8"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WindowResolverInputError(
                "direction_fact_invalid", {"input_id": entry.input_id, "artifact": "reading"}, category="input_integrity_error"
            ) from exc
        # The as_drawn contract's own identity fields — a product that does not
        # declare which facade it is cannot be given a direction fact.
        facade_label = doc.get("facade_label") if isinstance(doc, dict) else None
        if not isinstance(facade_label, str) or not facade_label:
            raise WindowResolverInputError(
                "direction_fact_invalid",
                {"input_id": entry.input_id, "reason": "as_drawn_facade_label_missing"},
                category="input_integrity_error",
            )
        mirrored, local_x_positive = _resolve_facade_flip_fields(None)
        facts.append(ElevationDirectionFactV1(
            input_id=entry.input_id,
            resolved_building_direction=entry.building_view_direction,
            resolution_source="manifest_building_axis",
            mirrored=mirrored,
            local_x_positive=local_x_positive,
            orientation_output_hash=None,
            adapter_version=None,
            view_manifest_sha256=manifest.content_sha256,
        ))
    return tuple(sorted(facts, key=lambda fact: fact.input_id))


def _check_direction_facts_as_drawn(
    manifest: ViewManifest, facts: tuple[ElevationDirectionFactV1, ...],
) -> tuple[ElevationDirectionFactV1, ...]:
    """``_check_direction_facts``'s manifest-side rules, as_drawn flip source.

    A producer-minted fact tuple is re-checked against the manifest (coverage,
    hash, ``manifest_building_axis`` semantics) exactly as the legacy check
    does; the only deliberate difference is the flip-field source — the legacy
    check re-reads ``reading.facade`` through ``parse_reading_view``, which on
    an as_drawn product silently parses to an empty shell (measured, T1 B2),
    so this check instead pins the no-declaration defaults the as_drawn
    derivation itself applies.  A drifted fact (tampered tuple) still dies by
    name.

    ⚠ This check deliberately consumes NO reading artifacts (no
    ``raw_reading_artifacts`` parameter — rework N-2, 2026-09-07l): unlike
    the legacy check, whose flip fields are READ from the products, this
    leg's flip fields are pinned defaults, so a product-bytes parameter here
    would only fake the impression that artifacts were consumed.
    """
    expected = {entry.input_id: entry for entry in manifest.required_entries() if entry.view_type == "elevation"}
    got = {fact.input_id: fact for fact in facts}
    if len(got) != len(facts) or set(got) != set(expected):
        raise WindowResolverInputError("direction_fact_invalid", {"declared": sorted(got), "required": sorted(expected)}, category="input_integrity_error")
    out = []
    for input_id, entry in expected.items():
        fact = got[input_id]
        if fact.view_manifest_sha256 != manifest.content_sha256:
            raise WindowResolverInputError("direction_fact_invalid", {"input_id": input_id, "field": "view_manifest_sha256"}, category="input_integrity_error")
        if fact.resolution_source == "manifest_building_axis":
            if (entry.direction_semantics != "building_axis" or
                    fact.resolved_building_direction != entry.building_view_direction or
                    fact.orientation_output_hash is not None or fact.adapter_version is not None):
                raise WindowResolverInputError("direction_fact_invalid", {"input_id": input_id}, category="input_integrity_error")
        else:
            raise WindowResolverInputError(
                "direction_fact_invalid",
                {"input_id": input_id, "reason": "as_drawn_leg_has_no_orientation_sidecar"},
                category="input_integrity_error",
            )
        mirrored, local_x_positive = _resolve_facade_flip_fields(None)
        if fact.mirrored != mirrored or fact.local_x_positive != local_x_positive:
            raise WindowResolverInputError("direction_fact_invalid", {"input_id": input_id, "field": "as_drawn_flip_defaults"}, category="input_integrity_error")
        out.append(fact)
    return tuple(sorted(out, key=lambda item: item.input_id))


def build_verified_window_inputs_as_drawn(
    *,
    producer_draw: CorrectedGeometryV3,
    raw_view_manifest_bytes: bytes,
    raw_reading_artifacts: Mapping[str, bytes],
) -> VerifiedWindowResolverInputs:
    """The as_drawn evidence-chain leg's LEGAL EMPTY SET of window sources.

    ⭐ Why empty is the honest catalog here (W-1 T2-⑤, verdict 2026-09-07i):
    the new leg's windows travel as the chain's ``opening_claims``
    (evidence-chain channel, decided by the model's adjudication rounds), ⛔
    NOT as legacy ``pen=="window"`` ledger strokes — and the projection
    producer emits ``windows=[]``, so the producer and the catalog AGREE (no
    window, no ledger row).  The emptiness is ACCOUNTED, ⛔ never silent: the
    wiring registers ``WINDOW_EVIDENCE_ON_CHAIN_NOT_ON_LEDGER`` (exploratory
    FLAG / strict blocks) so the channel split is a recorded fact, and
    ``_claim_links`` still hard-fails any producer that draws a window while
    this catalog is empty (a cited source that does not exist is a named
    ``model_draw_error``) — the empty set cannot launder a window in.

    Same canonical assembly as the legacy builder (one fingerprint rule, ⛔ no
    second marker definition); only the catalog and the direction-fact source
    are this leg's.
    """
    manifest = _parse_manifest(raw_view_manifest_bytes)
    facts = derive_manifest_direction_facts_as_drawn(
        raw_view_manifest_bytes=raw_view_manifest_bytes,
        raw_reading_artifacts=raw_reading_artifacts,
    )
    facts = _check_direction_facts_as_drawn(manifest, facts)
    # ⭐ 2026-09-08 补窗：目录不再是 T2-⑤ 的「合法空集」——它现在装的是这条腿
    # 真正的窗源观测（平面 opening_types 的 window 候选 + 立面 openings）。
    # 空集在投影 windows=[] 时才诚实；补窗后仍传空集会让 _claim_links 把每一个
    # 窗都判成 model_draw_error（引用了不存在的源）。
    rows = build_as_drawn_window_catalog(
        manifest=manifest, raw_reading_artifacts=raw_reading_artifacts,
    )
    return _assemble_verified_resolver_inputs(
        producer_draw=producer_draw, manifest=manifest, rows=rows, facts=facts,
        raw_view_manifest_bytes=raw_view_manifest_bytes,
        raw_reading_artifacts=raw_reading_artifacts,
    )


def verify_window_resolver_inputs_artifact(
    raw_artifact_bytes: bytes,
) -> VerifiedWindowResolverInputs:
    """Rebuild a verified marker solely from a self-contained attempt artifact.

    Direction facts for the currently supported production building-axis path
    are independently re-derived from the raw manifest/readings.  A future
    true-azimuth adapter must embed and verify its own raw orientation sidecar;
    it may not fall back to trusting the persisted fact tuple.
    """
    artifact = WindowResolverInputsArtifactV1.model_validate_json(raw_artifact_bytes)
    canonical = canonical_json_bytes(artifact.model_dump(mode="json"))
    if raw_artifact_bytes != canonical:
        raise WindowResolverInputError(
            "source_identity_invalid", {"artifact": "window_resolver_inputs_canonical_bytes"}, category="input_integrity_error",
        )
    raw_readings = {
        row.input_id: bytes(row.raw_bytes) for row in artifact.raw_reading_artifacts
    }
    producer = CorrectedGeometryV3.model_validate_json(
        bytes(artifact.producer_draw_canonical_bytes)
    )
    # ⭐ 2026-09-08：重建必须【按腿分派】，⛔ 不能一律用 legacy builder。
    #
    # 这个缺口一直存在，只是【被空集藏住了】：在 as_drawn 产物上，legacy 的
    # `_window_strokes` 找不到任何 `pen == "window"` 笔画（产物解析成空壳），
    # 而 as_drawn 侧当时传的是 T2-⑤ 的 `rows=()` —— 两边【恰好都空、于是相等】，
    # 重放比对永远通过。补窗把 as_drawn 目录填成真观测后，它当场现形
    # （`resolver_inputs_replay` 具名红，8 条锁同因）。
    # ⇒ 「合法空集」不只是占位符，它还在掩盖一处腿分派缺失。
    #
    # 选腿的键 = 分类器对【产物本身】的判定（项目口径：路由是分类器的判定，
    # ⛔ 永不按文件名）。混合契约在这里是响亮失败，⛔ 不静默挑一条腿。
    from src.agent.reading.vector_contract import (
        CONTRACT_AS_DRAWN_ELEVATION_V0,
        CONTRACT_AS_DRAWN_PLAN,
        CONTRACT_AS_DRAWN_PLAN_V0,
        classify_vector_json,
    )

    _AS_DRAWN = {CONTRACT_AS_DRAWN_PLAN, CONTRACT_AS_DRAWN_PLAN_V0,
                 CONTRACT_AS_DRAWN_ELEVATION_V0}
    contracts = set()
    for input_id, raw in sorted(raw_readings.items()):
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WindowResolverInputError(
                "source_identity_invalid", {"input_id": input_id, "artifact": "reading"},
                category="input_integrity_error",
            ) from exc
        contracts.add(classify_vector_json(doc).contract_id)
    as_drawn = contracts & _AS_DRAWN
    if as_drawn and as_drawn != contracts:
        raise WindowResolverInputError(
            "source_identity_invalid",
            {"artifact": "resolver_inputs_replay",
             "reason": "mixed reading contracts — replay cannot pick a leg",
             "contracts": sorted(contracts)},
            category="input_integrity_error",
        )
    if as_drawn:
        rebuilt = build_verified_window_inputs_as_drawn(
            producer_draw=producer,
            raw_view_manifest_bytes=bytes(artifact.raw_view_manifest_bytes),
            raw_reading_artifacts=raw_readings,
        )
    else:
        fresh_facts = derive_manifest_direction_facts(
            raw_view_manifest_bytes=bytes(artifact.raw_view_manifest_bytes),
            raw_reading_artifacts=raw_readings,
        )
        rebuilt = build_verified_window_resolver_inputs(
            producer_draw=producer,
            raw_view_manifest_bytes=bytes(artifact.raw_view_manifest_bytes),
            raw_reading_artifacts=raw_readings,
            elevation_direction_facts=fresh_facts,
        )
    if rebuilt.inputs != artifact.inputs:
        raise WindowResolverInputError(
            "source_identity_invalid", {"artifact": "resolver_inputs_replay"}, category="input_integrity_error",
        )
    return rebuilt


DirectionBindingErrorCode = Literal["direction_binding_ring_invalid", "direction_binding_ring_incompatible"]


class WindowDirectionBindingError(ValueError):
    def __init__(self, code: DirectionBindingErrorCode, context: Mapping[str, object]):
        self.code = code
        self.context = dict(context)
        super().__init__(f"{code}: {self.context}")


def _frame_hash(*, input_id: str, family: str, fingerprint: str, axis: str, sign: int,
                origin: float, mirrored: bool, local: str) -> str:
    return canonical_sha256({"schema": "view_projection_binding_v1", "input_id": input_id,
        "resolved_building_direction": family, "source_footprint_fingerprint": fingerprint,
        "world_axis": axis, "sign": sign, "along_origin": origin, "mirrored": mirrored,
        "local_x_positive": local})


def materialize_current_ring_va_elevation_bindings(*, geom: CorrectedGeometryV3, manifest: ViewManifest,
                                                    direction_facts: tuple[ElevationDirectionFactV1, ...],
                                                    visibility_tolerances: VisibilityTolerances) -> tuple[VaElevationViewBindingV1, ...]:
    """Fresh, non-cached 13-field Va bindings for exactly ``geom``'s ring."""
    try:
        validate_materialized_facade_segments(geom, tolerances=visibility_tolerances)
    except FacadeVisibilityInvariantError as exc:
        raise WindowDirectionBindingError("direction_binding_ring_invalid", {"upstream_error": exc.code, **exc.context}) from exc
    # Facts are authenticated by ``build_verified_window_resolver_inputs``.
    # This helper deliberately rechecks only totality/manifest identity and
    # derives every ring-dependent field afresh from ``geom``.
    facts = tuple(sorted(direction_facts, key=lambda item: item.input_id))
    expected = {entry.input_id: entry for entry in manifest.required_entries() if entry.view_type == "elevation"}
    if {fact.input_id for fact in facts} != set(expected) or len({fact.input_id for fact in facts}) != len(facts):
        raise WindowDirectionBindingError("direction_binding_ring_invalid", {"reason": "direction_fact_totality"})
    for fact in facts:
        entry = expected[fact.input_id]
        if fact.view_manifest_sha256 != manifest.content_sha256:
            raise WindowDirectionBindingError("direction_binding_ring_invalid", {"input_id": fact.input_id, "reason": "manifest_identity"})
        if fact.resolution_source == "manifest_building_axis":
            valid = (entry.direction_semantics == "building_axis" and
                     fact.resolved_building_direction == entry.building_view_direction and
                     fact.orientation_output_hash is None and fact.adapter_version is None)
        else:
            valid = (entry.direction_semantics in ("true_azimuth", "unknown") and
                     fact.orientation_output_hash is not None and fact.adapter_version is not None)
        if not valid:
            raise WindowDirectionBindingError("direction_binding_ring_invalid", {"input_id": fact.input_id, "reason": "direction_fact_invalid"})
    floors = {floor.id: floor for floor in geom.floors}
    output = []
    for fact in facts:
        family = fact.resolved_building_direction
        per_floor = []
        for floor_id, floor in sorted(floors.items()):
            fingerprint = floor_footprint_fingerprint(geom, floor)
            segments = [segment for segment in geom.facade_segments if segment.floor_id == floor_id and segment.facade_family == family]
            if not segments:
                raise WindowDirectionBindingError("direction_binding_ring_invalid", {"floor_id": floor_id, "input_id": fact.input_id})
            if any(segment.source_footprint_fingerprint != fingerprint for segment in segments):
                raise WindowDirectionBindingError("direction_binding_ring_invalid", {"floor_id": floor_id, "input_id": fact.input_id,
                    "declared_fingerprint": [segment.source_footprint_fingerprint for segment in segments], "recomputed_fingerprint": fingerprint})
            per_floor.append((floor_id, fingerprint, (min(s.world_along_interval.lo for s in segments), max(s.world_along_interval.hi for s in segments))))
        fingerprints = {item[1] for item in per_floor}; extents = {item[2] for item in per_floor}
        if len(fingerprints) != 1 or len(extents) != 1:
            raise WindowDirectionBindingError("direction_binding_ring_incompatible", {"input_id": fact.input_id,
                "floor_ids": [item[0] for item in per_floor], "fingerprints": [item[1] for item in per_floor], "family_extents": [item[2] for item in per_floor]})
        fingerprint = per_floor[0][1]; lo, hi = per_floor[0][2]
        axis = facade_convention.world_axis(family)
        sign = facade_convention.resolve_sign(family, mirrored=fact.mirrored, local_x_positive=fact.local_x_positive)
        origin = lo if sign == 1 else hi
        output.append(VaElevationViewBindingV1(input_id=fact.input_id, resolved_building_direction=family,
            resolution_source=fact.resolution_source, view_manifest_sha256=manifest.content_sha256,
            orientation_output_hash=fact.orientation_output_hash, adapter_version=fact.adapter_version,
            source_footprint_fingerprint=fingerprint, world_axis=axis, sign=sign, along_origin=origin,
            mirrored=fact.mirrored, local_x_positive=fact.local_x_positive,
            frame_transform_sha256=_frame_hash(input_id=fact.input_id, family=family, fingerprint=fingerprint,
                axis=axis, sign=sign, origin=origin, mirrored=fact.mirrored, local=fact.local_x_positive)))
    return tuple(sorted(output, key=lambda item: item.input_id))


__all__ = [
    "CURRENT_RING_BINDING_HELPER_VERSION", "ElevationDirectionFactV1", "ElevationSourceWindowV1",
    "ObservationReferenceCatalogEntry",
    "PlanSourceWindowV1", "RawReadingArtifactV1", "ReadingArtifactIdentityV1", "SourceIntervalV1", "VerifiedWindowResolverInputs",
    "WindowClaimSourceLinkV1", "WindowDirectionBindingError", "WindowResolverInputError", "WindowResolverInputsV1",
    "WindowResolverInputsArtifactV1", "WindowSourceErrorCategory", "WindowSourceOfferV1",
    "build_observation_reference_catalog_from_run",
    "build_verified_window_inputs_from_run",
    "build_verified_window_resolver_inputs", "build_window_source_offer",
    "build_window_resolver_inputs_artifact",
    "derive_manifest_direction_facts",
    "derive_observation_reference_catalog", "format_observation_reference_catalog",
    "materialize_current_ring_va_elevation_bindings", "source_locator", "verify_window_resolver_inputs",
    "serialize_window_resolver_inputs_artifact", "verify_window_resolver_inputs_against_raw_artifacts",
    "verify_window_resolver_inputs_artifact",
]
