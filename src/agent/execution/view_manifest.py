"""Trusted view manifest — schema v1 (strict typed models) + strict generator.

Per C2 B-M (``AI_agent/archive/2026-09-08_management_rebuild/original/AI_agent/proposals/c2_bm_view_manifest_spec.md``, v6 定稿): the
manifest is generated **deterministically from case metadata**, before
0_reading runs, by the orchestration side — never by the product (reader /
correction LLM). It is the single trusted record of "what input images exist,
what each one is declared to be, and whether a reader's silence about it is
honest 'unobserved' or a dishonest 'miss'" (§0 core invariant).

Two entry points cover the whole lifecycle:

  - :func:`provision_view_manifest` — the **only** writer. Called by run
    provisioning / the 0_reading preflight. Idempotent: a second call with an
    unchanged case_data returns the existing on-disk manifest; a changed
    case_data raises (mid-run case_data swap is an INVARIANT violation).
  - :func:`verify_view_manifest` — **never writes**. Called by ``validate_case``,
    judge-only/replay paths, and isolation build/merge to compare the on-disk
    manifest against an in-memory rebuild.

Everything else in this module is the typed schema (§3) and the strict
generator internals (§4) those two functions wrap.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.agent.correction.claims import (
    CLAIMS_VOCAB_VERSION,
    ELEVATION_POTENTIALLY_OBSERVABLE_CLAIMS,
    PLAN_POTENTIALLY_OBSERVABLE_CLAIMS,
    WINDOW_CLAIMS,
)
from src.agent.execution.case_metadata import testdata_path
from src.agent.execution.manifest import Hex64, hash_bytes, hash_file, hash_obj
from src.agent.execution.run_meta import run_meta_path

VIEW_MANIFEST_NAME = "view_manifest.json"
READING_EXAM_SCOPE_NAME = "reading_exam_scope.json"
VIEW_MANIFEST_SCHEMA_VERSION = "1"
GENERATOR_VERSION = "1"
COMPLETENESS_RULESET_VERSION = "1"

# --------------------------------------------------------------------------- #
# §4.2 declaration families — the EXPLICIT mapping table (CR-05).
#
# Every trusted input reaches the manifest through exactly one *declaration
# family* (a metadata declaration shape); each family row fixes both the
# ``view_type`` and the ``expected_output_id`` transform. There is NO generic
# stem-suffix guessing fallback: an input that does not arrive through a known
# family is the §4.3 unclassified-image hard gate's problem (raise), never a
# guessed entry.
#
# | family                | declaration source                          | view_type | expected_output_id  |
# |-----------------------|---------------------------------------------|-----------|---------------------|
# | floor_plans           | `Floor plans[]` rows                        | plan      | stem (identity)     |
# | cardinal_elevations   | `"<Dir> view path of the building"` keys    | elevation | stem (identity)     |
# | supplementary_plan    | `"Path of the supplementary plan ..."` key  | detail    | stem + "_view"      |
#
# The identity transform for floor_plans/cardinal_elevations matches the whole
# observed corpus (stems `1f_view`/`South_view`/... already ARE the produced
# artifact stems); `supp_plan -> supp_plan_view` is the spec's written-out
# supplementary example (B-M §3.2/§4.2). A future declaration key gets its own
# table row here — extending the table is the only sanctioned way in.
_TRANSFORM_IDENTITY = "identity"
_TRANSFORM_APPEND_VIEW = "append_view"


def _family_expected_output_id(transform: str, input_id: str) -> str:
    if transform == _TRANSFORM_IDENTITY:
        return input_id
    if transform == _TRANSFORM_APPEND_VIEW:
        return f"{input_id}_view"
    raise ValueError(f"unknown declaration-family output-id transform: {transform!r}")


# Supplementary/site/detail declaration keys (one row per known metadata key).
_SUPPLEMENTARY_KEYS: dict[str, dict] = {
    "Path of the supplementary plan example drawing for the building": {
        "family": "supplementary_plan",
        "view_type": "detail",
        "output_id_transform": _TRANSFORM_APPEND_VIEW,
    },
}
_FLOOR_PLAN_FAMILY = {
    "family": "floor_plans",
    "view_type": "plan",
    "output_id_transform": _TRANSFORM_IDENTITY,
}
_ELEVATION_FAMILY = {
    "family": "cardinal_elevations",
    "view_type": "elevation",
    "output_id_transform": _TRANSFORM_IDENTITY,
}
_ELEVATION_KEYS: dict[str, str] = {
    "South view path of the building": "South",
    "North view path of the building": "North",
    "East view path of the building": "East",
    "West view path of the building": "West",
}
_DIRECTION_SEMANTICS_VALUES = ("building_axis", "true_azimuth", "unknown")


# --------------------------------------------------------------------------- #
# §3.6 CompletenessAssertion strict wire (frozen, r4 R4-BM-01)
# --------------------------------------------------------------------------- #
class CaseMetadataSourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["case_metadata"]
    json_pointer: str
    case_metadata_sha256: Hex64


class UserSourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["user"]
    content_sha256: Hex64


class DatasetSourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["dataset_ref"]
    dataset_id: str
    dataset_version: str
    contract_id: str
    content_sha256: Hex64


CompletenessSourceRef = Annotated[
    Union[CaseMetadataSourceRef, UserSourceRef, DatasetSourceRef],
    Field(discriminator="source"),
]


class CompletenessAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assertion_id: str
    source_ref: CompletenessSourceRef


# --------------------------------------------------------------------------- #
# §3.3 coverage typed domain
# --------------------------------------------------------------------------- #
_FRAME_REGION_PAIRS = {
    "plan_floor_region": "full_floor",
    "elevation_local_along": "full_facade",
}


class Coverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frame: Literal["plan_floor_region", "elevation_local_along"]
    region: Literal["full_floor", "full_facade"]
    completeness_assertion_id: str


# --------------------------------------------------------------------------- #
# §3.2 opening_evidence
# --------------------------------------------------------------------------- #
class OpeningEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    potentially_observable_claims: list[str] = Field(default_factory=list)
    negative_evidence_capable_claims: list[str] = Field(default_factory=list)
    coverage: Coverage | None = None
    completeness_assertion: CompletenessAssertion | None = None

    @field_validator("potentially_observable_claims", "negative_evidence_capable_claims")
    @classmethod
    def _claims_in_vocab_dedup_sorted(cls, v: list[str]) -> list[str]:
        bad = sorted(set(v) - WINDOW_CLAIMS)
        if bad:
            raise ValueError(f"claim(s) outside claims vocabulary: {bad}")
        return sorted(set(v))

    @model_validator(mode="after")
    def _linked_constraints(self) -> "OpeningEvidence":
        # 1. negative_evidence_capable_claims ⊆ potentially_observable_claims
        extra = set(self.negative_evidence_capable_claims) - set(self.potentially_observable_claims)
        if extra:
            raise ValueError(
                f"negative_evidence_capable_claims not a subset of "
                f"potentially_observable_claims: {sorted(extra)}"
            )
        # 2. negative non-empty <=> coverage and completeness_assertion both present
        has_negative = bool(self.negative_evidence_capable_claims)
        has_coverage = self.coverage is not None
        has_assertion = self.completeness_assertion is not None
        if has_coverage != has_assertion:
            raise ValueError(
                "coverage and completeness_assertion must be both present or both absent"
            )
        if has_negative != has_coverage:
            raise ValueError(
                "negative_evidence_capable_claims non-empty iff coverage/"
                "completeness_assertion are present"
            )
        # 3. coverage.completeness_assertion_id references completeness_assertion.assertion_id
        if has_coverage and self.coverage.completeness_assertion_id != self.completeness_assertion.assertion_id:
            raise ValueError(
                "coverage.completeness_assertion_id must reference "
                "completeness_assertion.assertion_id (no dangling reference)"
            )
        # 4. frame-region pairing (C2 domain)
        if has_coverage and _FRAME_REGION_PAIRS.get(self.coverage.frame) != self.coverage.region:
            raise ValueError(
                f"coverage frame/region pairing invalid for C2: "
                f"{self.coverage.frame} requires region="
                f"{_FRAME_REGION_PAIRS.get(self.coverage.frame)!r}, got {self.coverage.region!r}"
            )
        return self


def _observable_claims_for(view_type: str) -> list[str]:
    if view_type == "plan":
        return sorted(PLAN_POTENTIALLY_OBSERVABLE_CLAIMS)
    if view_type == "elevation":
        return sorted(ELEVATION_POTENTIALLY_OBSERVABLE_CLAIMS)
    return []


# CR-04 (主控 2026-07-11 冻结的 metadata 形状 — 写死于此，测试同步断言):
#   testdata_prompt.json:  "views": { "<stem>": { "completeness": {
#       "assertion_id": "<non-empty str>",
#       "claims": ["existence", ...]          # ⊆ 该图种 potentially_observable_claims
#   } } }
# 生成规则:
#   - claims 必须是该图种 observable 集的子集，越界 = 生成期 raise;
#   - source_ref = CaseMetadataSourceRef{source:"case_metadata",
#       json_pointer:"/views/<stem>/completeness", case_metadata_sha256:<顶层值>};
#   - Coverage 按 view_type: plan→(plan_floor_region, full_floor)、
#       elevation→(elevation_local_along, full_facade);
#   - negative_evidence_capable_claims = claims;
#   - 非 plan/elevation 图种带 completeness = raise（C2 域无其余 frame）。
_COMPLETENESS_FRAME_BY_VIEW_TYPE = {
    "plan": ("plan_floor_region", "full_floor"),
    "elevation": ("elevation_local_along", "full_facade"),
}


def _opening_evidence_for(
    view_type: str,
    *,
    stem: str,
    overlay: dict,
    case_metadata_sha256: str,
) -> OpeningEvidence:
    observable = _observable_claims_for(view_type)
    completeness = overlay.get("completeness")
    if completeness is None:
        return OpeningEvidence(potentially_observable_claims=observable)

    if view_type not in _COMPLETENESS_FRAME_BY_VIEW_TYPE:
        raise ValueError(
            f"views{{}} completeness assertion on {stem!r}: view_type={view_type!r} "
            "has no C2 coverage frame (only plan/elevation may carry one)"
        )
    if not isinstance(completeness, dict):
        raise ValueError(f"views{{}} completeness on {stem!r} must be an object")
    assertion_id = completeness.get("assertion_id")
    if not isinstance(assertion_id, str) or not assertion_id:
        raise ValueError(
            f"views{{}} completeness on {stem!r}: assertion_id must be a non-empty string"
        )
    claims = completeness.get("claims")
    if not isinstance(claims, list) or not claims or not all(isinstance(c, str) for c in claims):
        raise ValueError(
            f"views{{}} completeness on {stem!r}: claims must be a non-empty list of strings"
        )
    unknown_keys = set(completeness) - {"assertion_id", "claims"}
    if unknown_keys:
        raise ValueError(
            f"views{{}} completeness on {stem!r}: unknown key(s) {sorted(unknown_keys)}"
        )
    out_of_bounds = sorted(set(claims) - set(observable))
    if out_of_bounds:
        raise ValueError(
            f"views{{}} completeness on {stem!r}: claim(s) {out_of_bounds} are not "
            f"potentially observable on a {view_type} (allowed: {observable})"
        )
    frame, region = _COMPLETENESS_FRAME_BY_VIEW_TYPE[view_type]
    return OpeningEvidence(
        potentially_observable_claims=observable,
        negative_evidence_capable_claims=sorted(set(claims)),
        coverage=Coverage(frame=frame, region=region, completeness_assertion_id=assertion_id),
        completeness_assertion=CompletenessAssertion(
            assertion_id=assertion_id,
            source_ref=CaseMetadataSourceRef(
                source="case_metadata",
                json_pointer=f"/views/{stem}/completeness",
                case_metadata_sha256=case_metadata_sha256,
            ),
        ),
    )


# --------------------------------------------------------------------------- #
# §3.2 ManifestEntry — discriminated union on `kind`
# --------------------------------------------------------------------------- #
class _EntryBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_id: str
    source_image: str
    image_sha256: Hex64

    @field_validator("input_id")
    @classmethod
    def _input_id_nonempty(cls, v: str) -> str:
        if not v:
            raise ValueError("input_id must be non-empty")
        return v

    @field_validator("source_image")
    @classmethod
    def _source_image_normalized(cls, v: str) -> str:
        if (
            not v.startswith("case_data/")
            or v.startswith("/")
            or ".." in Path(v).parts
            or len(Path(v).parts) != 2
        ):
            raise ValueError(
                f"source_image must be a normalized 'case_data/<name>' relative "
                f"path (no absolute paths, no '..'): {v!r}"
            )
        return v


class DimensionedApplicability(BaseModel):
    """S-3: structured per-view applicability for the dimension checks.

    Replaces the lossy ``dimensioned: bool`` when a case declares applicability
    *explicitly with provenance*. The 3-state ``state`` keeps ``unknown`` distinct
    from ``declared_false`` end-to-end — the lossy bool folded both into ``False``
    (and sm24's undeclared views into ``False`` too), which is exactly the
    "exam question was never asked" root cause (31 N/A rows, dimension checks the
    bulk). ``authority`` + ``source_hash`` make the declaration auditable so a
    reviewer/sign-off is machine-visible (the sm24 true-values carry reviewer
    ``hortonyyx`` + the closed dimension-chain basis).

    Wire form is a *union* (``bool | DimensionedApplicability``) so legacy cases
    that never declared structured applicability keep their byte-identical bool
    (and therefore their ``content_sha256``): the upgrade is opt-in per case.
    """

    model_config = ConfigDict(extra="forbid")

    state: Literal["declared_true", "declared_false", "unknown"]
    authority: str
    source_hash: Hex64

    @field_validator("authority")
    @classmethod
    def _authority_nonempty(cls, v: str) -> str:
        if not v:
            raise ValueError("authority must be non-empty")
        return v


# S-3: the 4-state machine value downstream code (checker evidence, N/A reasons)
# normalizes to. ``legacy_default`` is NOT ``declared_false``: a legacy bool
# ``False`` is an undeclared legacy view (N/A, read-only), while a real
# ``declared_false`` is an affirmative "this view has no dimensions" — the two
# must never fold back together (ruling追加约束 #2).
DimensionedState = Literal["declared_true", "declared_false", "unknown", "legacy_default"]


def dimensioned_state(entry_dimensioned: "bool | DimensionedApplicability") -> DimensionedState:
    """Normalize a ``RequiredViewEntry.dimensioned`` field to a 4-state value.

    bool ``True``  → ``declared_true``
    bool ``False`` → ``legacy_default`` (undeclared legacy, NOT ``declared_false``)
    object         → its own ``state`` (``declared_true``/``declared_false``/``unknown``)

    Downstream N/A reasons and checker evidence carry this 4-state value to
    ``checks.json`` so the ``unknown``↔``declared_false``↔``legacy_default``
    distinction is never folded back to a bool (ruling追加约束 #1).
    """
    if isinstance(entry_dimensioned, DimensionedApplicability):
        return entry_dimensioned.state
    return "declared_true" if entry_dimensioned else "legacy_default"


class RequiredViewEntry(_EntryBase):
    kind: Literal["required_view"] = "required_view"

    view_type: Literal["plan", "elevation", "site_plan", "detail"]
    view_kind: Literal["full"] = "full"
    floor_ref: int | None = None
    declared_direction_token: str | None = None
    direction_source: Literal["standard_assumption", "title_hint", "matcher", "user"]
    direction_semantics: Literal["building_axis", "true_azimuth", "unknown"]
    semantics_source: Literal["standard_assumption", "case_metadata", "user"]
    azimuth_deg: float | None = None
    building_view_direction: str | None = None
    dimensioned: bool | DimensionedApplicability
    expected_output_id: str
    reader_output_required: Literal[True] = True
    opening_evidence: OpeningEvidence

    @model_validator(mode="after")
    def _conditional_constraints(self) -> "RequiredViewEntry":
        if self.view_type == "plan":
            if self.floor_ref is None:
                raise ValueError("floor_ref is required when view_type=plan")
        elif self.floor_ref is not None:
            raise ValueError(f"floor_ref is forbidden when view_type={self.view_type!r}")

        if self.view_type == "elevation" and not self.declared_direction_token:
            raise ValueError("declared_direction_token is required when view_type=elevation")

        if self.direction_semantics == "true_azimuth":
            if self.azimuth_deg is None or not math.isfinite(self.azimuth_deg) or not (0.0 <= self.azimuth_deg < 360.0):
                raise ValueError(
                    "azimuth_deg is required, finite, and in [0,360) when "
                    "direction_semantics=true_azimuth"
                )
        elif self.azimuth_deg is not None:
            raise ValueError(
                f"azimuth_deg is forbidden when direction_semantics={self.direction_semantics!r}"
            )

        if self.direction_semantics != "building_axis" and self.building_view_direction is not None:
            raise ValueError(
                "building_view_direction must be null when direction_semantics is "
                "true_azimuth or unknown"
            )

        if not self.expected_output_id:
            raise ValueError("expected_output_id must be non-empty")

        return self


class ExcludedInputEntry(_EntryBase):
    kind: Literal["excluded_input"] = "excluded_input"

    excluded_reason: Literal["derived_working_copy", "non_drawing_asset"]
    parent_input_id: str | None = None

    @model_validator(mode="after")
    def _conditional_constraints(self) -> "ExcludedInputEntry":
        if self.excluded_reason == "derived_working_copy" and not self.parent_input_id:
            raise ValueError("parent_input_id is required when excluded_reason=derived_working_copy")
        if self.excluded_reason == "non_drawing_asset" and self.parent_input_id is not None:
            raise ValueError("parent_input_id is forbidden when excluded_reason=non_drawing_asset")
        return self


ManifestEntry = Annotated[
    Union[RequiredViewEntry, ExcludedInputEntry],
    Field(discriminator="kind"),
]


# --------------------------------------------------------------------------- #
# §3.0 top-level ViewManifest
# --------------------------------------------------------------------------- #
class ViewManifest(BaseModel):
    """A successfully-parsed ViewManifest is self-verified: schema versions are
    frozen Literals (unknown version = fail closed, §3.0) and the model
    validator recomputes ``content_sha256`` over the canonical payload — so a
    tampered field with a stale hash can NEVER survive ``model_validate_json``
    (CR-01: every consumer entry point — provision reuse, verify, isolation
    build/merge, migration orphan reuse — parses through this model and
    therefore only ever handles hash-consistent objects)."""

    model_config = ConfigDict(extra="forbid")

    view_manifest_schema_version: Literal["1"] = VIEW_MANIFEST_SCHEMA_VERSION
    claims_vocab_version: Literal["1"] = CLAIMS_VOCAB_VERSION
    generator_version: Literal["1"] = GENERATOR_VERSION
    completeness_ruleset_version: Literal["1"] = COMPLETENESS_RULESET_VERSION
    case_id: str
    case_metadata_sha256: Hex64
    entries: list[ManifestEntry] = Field(default_factory=list)
    content_sha256: Hex64

    @model_validator(mode="after")
    def _entries_canonical_and_hash_consistent(self) -> "ViewManifest":
        ids = [e.input_id for e in self.entries]
        if len(set(ids)) != len(ids):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"duplicate input_id in entries: {dupes}")
        if ids != sorted(ids):
            raise ValueError("entries must be sorted by input_id (canonical order)")
        # B5's floor mapping is intentionally not OCR/name based.  The B-M
        # manifest contract defines plan floor_ref as a 1-based, ascending
        # sequence, so gaps cannot be deferred to a resolver guess.
        plan_refs = [e.floor_ref for e in self.entries
                     if isinstance(e, RequiredViewEntry) and e.view_type == "plan"]
        if plan_refs:
            expected = list(range(1, max(plan_refs) + 1))
            if sorted(plan_refs) != expected:
                raise ValueError("manifest_floor_ref_non_contiguous")
        recomputed = _content_hash_of_payload(self.model_dump(mode="json"))
        if self.content_sha256 != recomputed:
            raise ValueError(
                "content_sha256 does not match the canonical payload hash "
                f"(declared {self.content_sha256}, recomputed {recomputed}) — "
                "manifest bytes were modified without recomputing the hash"
            )
        return self

    # ---- accessors ----
    def required_entries(self) -> list[RequiredViewEntry]:
        return [e for e in self.entries if e.kind == "required_view"]

    def excluded_entries(self) -> list[ExcludedInputEntry]:
        return [e for e in self.entries if e.kind == "excluded_input"]

    def entry_by_input_id(self, input_id: str) -> "RequiredViewEntry | ExcludedInputEntry | None":
        for e in self.entries:
            if e.input_id == input_id:
                return e
        return None

    def expected_output_ids(self) -> dict[str, str]:
        """``expected_output_id -> input_id`` for every required_view entry."""
        return {e.expected_output_id: e.input_id for e in self.required_entries()}


class ReadingExamScope(BaseModel):
    """Run-local, pre-start declaration of the views this reading exam covers.

    It deliberately contains only input identity plus a human reason.  The base
    manifest remains the case identity; this is a frozen consumption subset.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    source: Literal["run_config.yaml:reading_exam_scope"]
    base_view_manifest_sha256: Hex64
    input_ids: list[str]
    reason: str
    declaration_sha256: Hex64
    content_sha256: Hex64

    @model_validator(mode="after")
    def _canonical_and_hash_consistent(self) -> "ReadingExamScope":
        if not self.input_ids or self.input_ids != sorted(set(self.input_ids)):
            raise ValueError("input_ids must be a non-empty sorted unique list")
        if not self.reason:
            raise ValueError("reason must be non-empty")
        expected = hash_obj({k: v for k, v in self.model_dump(mode="json").items() if k != "content_sha256"})
        if self.content_sha256 != expected:
            raise ValueError("content_sha256 does not match the canonical payload hash")
        return self


def _content_hash_of_payload(payload: dict) -> str:
    """Hash of the canonical JSON payload, excluding ``content_sha256`` itself."""
    return hash_obj({k: v for k, v in payload.items() if k != "content_sha256"})


def compute_content_hash(manifest: ViewManifest) -> str:
    """Canonicalized-payload hash (excludes ``content_sha256`` itself, §3.0)."""
    return _content_hash_of_payload(manifest.model_dump(mode="json"))


def _scope_content_hash(payload: dict) -> str:
    return hash_obj({k: v for k, v in payload.items() if k != "content_sha256"})


def _declared_reading_exam_scope(run_dir: Path, manifest: ViewManifest) -> ReadingExamScope | None:
    """Strictly read the optional run-level subset declaration.

    `load_run_config` is intentionally soft for historical pipeline settings;
    an exam scope is different: if present it changes what is being examined and
    must therefore fail closed on malformed data rather than disappear.
    """
    config_path = run_dir / "run_config.yaml"
    if not config_path.exists():
        return None
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - scope declarations are fail-closed
        raise ValueError(f"reading exam scope config is unreadable: {exc}") from exc
    if not isinstance(raw, dict) or "reading_exam_scope" not in raw:
        return None
    declaration = raw["reading_exam_scope"]
    if not isinstance(declaration, dict) or set(declaration) != {"input_ids", "reason"}:
        raise ValueError("reading_exam_scope must contain exactly input_ids and reason")
    input_ids = declaration["input_ids"]
    reason = declaration["reason"]
    if not isinstance(input_ids, list) or not input_ids or not all(isinstance(item, str) and item for item in input_ids):
        raise ValueError("reading_exam_scope.input_ids must be a non-empty list of strings")
    if input_ids != sorted(set(input_ids)):
        raise ValueError("reading_exam_scope.input_ids must be sorted and unique")
    if not isinstance(reason, str) or not reason:
        raise ValueError("reading_exam_scope.reason must be a non-empty string")
    available = {entry.input_id for entry in manifest.required_entries()}
    unknown = sorted(set(input_ids) - available)
    if unknown:
        raise ValueError(f"reading_exam_scope.input_ids are not required views: {unknown}")
    declaration_sha256 = hash_obj(declaration)
    payload = {
        "schema_version": "1",
        "source": "run_config.yaml:reading_exam_scope",
        "base_view_manifest_sha256": manifest.content_sha256,
        "input_ids": input_ids,
        "reason": reason,
        "declaration_sha256": declaration_sha256,
    }
    payload["content_sha256"] = _scope_content_hash(payload)
    return ReadingExamScope.model_validate(payload)


def _canonical_reading_exam_scope_json(scope: ReadingExamScope) -> str:
    return json.dumps(scope.model_dump(mode="json"), indent=2, sort_keys=True, separators=(",", ": "), ensure_ascii=False)


def _provision_reading_exam_scope(case_dir: Path, run_dir: Path, manifest: ViewManifest) -> ReadingExamScope | None:
    expected = _declared_reading_exam_scope(run_dir, manifest)
    path = run_meta_path(run_dir, READING_EXAM_SCOPE_NAME, for_write=expected is not None)
    if expected is None:
        if path.exists():
            raise ValueError("reading exam scope drift: frozen scope exists but run_config.yaml no longer declares it")
        return None
    if path.exists():
        try:
            actual = ReadingExamScope.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"existing reading exam scope at {path} is corrupt: {exc}") from exc
        if actual.content_sha256 != expected.content_sha256:
            raise ValueError("reading exam scope drift: run_config.yaml changed after this run was provisioned")
        return actual
    _atomic_write_text(path, _canonical_reading_exam_scope_json(expected))
    return expected


def resolve_frozen_reading_exam_scope(
    run_dir: Path | str, base_manifest: ViewManifest
) -> ReadingExamScope | None:
    """Return the run's frozen reading subset, or ``None`` when unscoped.

    This is the single read-only scope consumer for both manifest verification
    and judge scoring.  Unlike :func:`verify_view_manifest`, it deliberately
    does not need case data: consumers already holding the base manifest can
    verify that the frozen declaration is bound to precisely that manifest.
    """
    run_dir = Path(run_dir)
    declared = _declared_reading_exam_scope(run_dir, base_manifest)
    scope_path = run_meta_path(run_dir, READING_EXAM_SCOPE_NAME)
    if declared is None:
        if scope_path.exists():
            raise ValueError(
                "reading exam scope drift: frozen scope exists but "
                "run_config.yaml has no reading_exam_scope declaration"
            )
        return None
    if not scope_path.exists():
        raise ValueError(
            "reading exam scope drift: run_config.yaml declares a scope but "
            "the frozen scope artifact is missing"
        )
    try:
        frozen = ReadingExamScope.model_validate_json(scope_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - frozen scope is an invariant
        raise ValueError(f"reading exam scope drift: frozen scope artifact is corrupt: {exc}") from exc
    if frozen.base_view_manifest_sha256 != base_manifest.content_sha256:
        raise ValueError(
            "reading exam scope drift: frozen scope is bound to a different "
            "base view manifest"
        )
    if frozen.content_sha256 != declared.content_sha256:
        raise ValueError(
            "reading exam scope drift: frozen scope does not match the "
            "current declaration"
        )
    return frozen


def canonical_view_manifest_json(manifest: ViewManifest) -> str:
    """The single canonical on-disk serialization (CR-06: sorted keys, fixed
    separators, UTF-8, 2-space indent) shared by provision and migration. The
    content hash itself is computed over ``hash_obj``'s compact sorted form of
    the payload, NOT over these display bytes — so this formatting choice does
    not participate in (and cannot drift) ``content_sha256``."""
    return json.dumps(
        manifest.model_dump(mode="json"),
        indent=2,
        sort_keys=True,
        separators=(",", ": "),
        ensure_ascii=False,
    )


# --------------------------------------------------------------------------- #
# §4.1 strict loader helpers
# --------------------------------------------------------------------------- #
def _dimensioned_stems_declared(data: dict) -> set[str]:
    stems: set[str] = set()
    for v in data.get("dimensioned_views") or []:
        if isinstance(v, str) and v:
            p = Path(v)
            stems.add(p.stem if p.suffix else v)
    return stems


def _structured_dimensioned_map(
    data: dict, case_metadata_sha256: str
) -> tuple[dict[str, DimensionedApplicability] | None, dict[str, str]]:
    """S-3 + R1-6: parse a structured ``dimensioned_views`` declaration (a list of
    ``{view, dimensioned, source}`` objects) into per-stem applicability records
    AND the per-stem declared ``source.image_sha256`` for provenance verification.

    Returns ``(None, {})`` for the legacy forms (absent key, or stem-string list)
    so :func:`build_view_manifest` keeps byte-identical bools — and therefore a
    stable ``content_sha256`` — for every existing case (sm24 absent ⇒ all-False,
    sm21 stem-list ⇒ membership bools). Only a list-of-objects declaration
    activates the structured, provenance-bound wire.

    Each object's ``source`` (``{image_sha256, reviewer, date, basis}``) becomes
    the audit basis: ``authority`` = the human reviewer, ``source_hash`` = the
    hash of the whole source object (so the sm24 sign-off is machine-visible and
    tamper-evident). ``declared_hashes`` carries each entry's declared
    ``source.image_sha256`` so :func:`build_view_manifest` can verify it against
    the view's REAL image hash (R1-6): ``source_hash`` only proves the declaration
    was not tampered with after the fact, not that it was ever true — so a forged
    ``hortonyyx`` sign-off with a placeholder hash is refused. A required view
    MISSING from the declaration is not folded to False here —
    :func:`_entry_dimensioned` surfaces it as ``unknown`` and the strict-profile
    provisioning wrapper fail-closes on it (L-20).
    """
    raw = data.get("dimensioned_views")
    if not isinstance(raw, list) or not raw:
        return None, {}
    # J-2 (orchestrator ruling 2026-08-03 §2): a ``dimensioned_views`` list must
    # be ALL strings (legacy) or ALL objects (structured with provenance). A MIXED
    # list (strings + objects) is malformed — it is neither legal form — and was
    # previously folded to legacy here, silently dropping every structured
    # declaration. Reject it fail-closed and name the offending entry instead of
    # guessing which form the operator meant (same spec as R1-2: invalid ⇒ fail).
    has_object = any(isinstance(item, dict) for item in raw)
    has_non_object = any(not isinstance(item, dict) for item in raw)
    if has_object and has_non_object:
        offender = next(item for item in raw if not isinstance(item, dict))
        raise ValueError(
            "dimensioned_views mixed list: entries must be ALL strings (legacy) or "
            "ALL objects (structured with provenance); found both forms mixed — "
            f"first non-object entry: {offender!r}"
        )
    # all strings (or all non-objects) ⇒ legacy form; object-list wire stays off
    if has_non_object:
        return None, {}
    out: dict[str, DimensionedApplicability] = {}
    declared_hashes: dict[str, str] = {}
    for item in raw:
        view = item.get("view")
        if not isinstance(view, str) or not view:
            raise ValueError(
                f"dimensioned_views structured entry must have a non-empty 'view' string: {item!r}"
            )
        stem = Path(view).stem if Path(view).suffix else view
        dim_flag = item.get("dimensioned")
        if not isinstance(dim_flag, bool):
            raise ValueError(
                f"dimensioned_views entry {view!r} 'dimensioned' must be a bool: {item!r}"
            )
        source = item.get("source")
        if not isinstance(source, dict):
            raise ValueError(
                f"dimensioned_views entry {view!r} must carry a 'source' object: {item!r}"
            )
        reviewer = source.get("reviewer")
        if not isinstance(reviewer, str) or not reviewer:
            raise ValueError(
                f"dimensioned_views entry {view!r} source.reviewer must be a non-empty string"
            )
        # R1-6 (派工单 §1.6): source.image_sha256 must be present; build_view_manifest
        # verifies it against the view's REAL image hash (a forged sign-off with a
        # placeholder hash is refused). source_hash alone cannot prove authenticity.
        decl_hash = source.get("image_sha256")
        if not isinstance(decl_hash, str) or not decl_hash:
            raise ValueError(
                f"dimensioned_views entry {view!r} source.image_sha256 must be a non-empty string"
            )
        if stem in out:
            raise ValueError(f"dimensioned_views duplicate view: {stem!r}")
        out[stem] = DimensionedApplicability(
            state="declared_true" if dim_flag else "declared_false",
            authority=reviewer,
            source_hash=hash_obj(source),
        )
        declared_hashes[stem] = decl_hash
    return out, declared_hashes


def _entry_dimensioned(
    input_id: str,
    overlay: dict,
    legacy_bool: bool,
    structured_dim: dict[str, DimensionedApplicability] | None,
    case_metadata_sha256: str,
) -> "bool | DimensionedApplicability":
    """Resolve one required view's ``dimensioned`` field (S-3).

    Structured form (``dimensioned_views`` is an object list): the declaration is
    authoritative and provenance-bound — a view present in the declaration gets
    its :class:`DimensionedApplicability`; a view absent gets ``unknown`` (the
    strict-profile provisioning wrapper fail-closes on ``unknown``, L-20; it is
    never silently folded to False). The legacy ``views{{}}.dimensioned`` bool
    override is refused in this form (it would strip the provenance).

    Legacy form (absent / stem-string list): the caller's precomputed bool wins,
    and the per-view ``views{{}}.dimensioned`` bool override still applies
    byte-identically to v1 (so ``content_sha256`` is stable).
    """
    if structured_dim is not None:
        if "dimensioned" in overlay:
            raise ValueError(
                f"views{{{input_id!r}}}.dimensioned override is forbidden when "
                "dimensioned_views is a structured (object-list) declaration; "
                "declare applicability with provenance in dimensioned_views instead"
            )
        if input_id in structured_dim:
            return structured_dim[input_id]
        return DimensionedApplicability(
            state="unknown",
            authority="case_metadata",
            source_hash=case_metadata_sha256,
        )
    if "dimensioned" in overlay:
        return bool(overlay["dimensioned"])
    return legacy_bool


def _normalize_declared_path(case_dir: Path, declared: object) -> tuple[str, str, str]:
    """Normalize a declared metadata path to ``case_data/<basename>``.

    Single algorithm for both case_data-relative and legacy full-repo-relative
    declarations (§4.1): only the basename is trusted, and it must resolve to a
    real file *inside* ``<case_dir>/case_data`` — this cannot escape the case
    input root by construction (directory components of the declared path are
    discarded, never followed).
    """
    if not isinstance(declared, str) or not declared:
        raise ValueError(f"declared path is empty or not a string: {declared!r}")
    basename = Path(declared).name
    if not basename:
        raise ValueError(f"declared path has no filename component: {declared!r}")
    case_data_root = (case_dir / "case_data").resolve(strict=False)
    candidate = case_dir / "case_data" / basename
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(case_data_root)
    except ValueError as exc:
        raise ValueError(f"declared path escapes case input root: {declared!r}") from exc
    if not resolved.is_file():
        raise ValueError(f"declared path does not exist or is unreadable: {declared!r} (resolved {candidate})")
    if resolved.suffix.lower() != ".png":
        raise ValueError(f"declared path is not a PNG: {declared!r}")
    return f"case_data/{basename}", basename, hash_file(candidate)


def _resolve_semantics(overlay: dict) -> tuple[str, str, float | None]:
    """§4.2 ``views:{}`` per-view override row for direction_semantics/azimuth_deg.
    Absent an override, C2's uniform default applies: building_axis +
    standard_assumption (no case currently declares true geographic azimuth)."""
    if "direction_semantics" in overlay:
        semantics = overlay["direction_semantics"]
        if semantics not in _DIRECTION_SEMANTICS_VALUES:
            raise ValueError(f"invalid direction_semantics override: {semantics!r}")
        azimuth = overlay.get("azimuth_deg")
        if semantics == "true_azimuth":
            if not isinstance(azimuth, (int, float)) or isinstance(azimuth, bool) or not math.isfinite(azimuth) or not (0.0 <= azimuth < 360.0):
                raise ValueError(
                    f"views{{}} override: azimuth_deg is required, finite, and in "
                    f"[0,360) when direction_semantics=true_azimuth (got {azimuth!r})"
                )
            return semantics, "case_metadata", float(azimuth)
        if azimuth is not None:
            raise ValueError(
                f"views{{}} override: azimuth_deg is only valid with "
                f"direction_semantics=true_azimuth (semantics={semantics!r})"
            )
        return semantics, "case_metadata", None
    return "building_axis", "standard_assumption", None


def _resolve_view_kind(overlay: dict) -> str:
    kind = overlay.get("view_kind", "full")
    if kind == "partial":
        raise ValueError("view_kind=partial declared — partial views not supported in C2")
    if kind != "full":
        raise ValueError(f"unsupported view_kind override: {kind!r}")
    return "full"


def _register(
    entries: dict[str, "RequiredViewEntry | ExcludedInputEntry"],
    expected_ids: dict[str, str],
    classified_files: set[str],
    entry: "RequiredViewEntry | ExcludedInputEntry",
    basename: str,
) -> None:
    if entry.input_id in entries:
        raise ValueError(f"duplicate input_id: {entry.input_id}")
    if isinstance(entry, RequiredViewEntry):
        if entry.expected_output_id in expected_ids:
            raise ValueError(
                f"duplicate expected_output_id: {entry.expected_output_id} "
                f"(from {expected_ids[entry.expected_output_id]!r} and {entry.input_id!r})"
            )
        expected_ids[entry.expected_output_id] = entry.input_id
    entries[entry.input_id] = entry
    classified_files.add(basename)


def _verify_derived_copy_relation(
    entries: dict[str, "RequiredViewEntry | ExcludedInputEntry"], entry: ExcludedInputEntry
) -> None:
    parent = entries.get(entry.parent_input_id or "")
    if parent is None or parent.image_sha256 != entry.image_sha256:
        raise ValueError(
            f"derived_working_copy {entry.input_id!r}: hash relation with parent "
            f"{entry.parent_input_id!r} does not hold (parent not found or bytes differ)"
        )


# --------------------------------------------------------------------------- #
# §4 generator (pure, in-memory — no I/O side effects beyond reading case_data)
# --------------------------------------------------------------------------- #
def build_view_manifest(case_dir: Path | str) -> ViewManifest:
    """Deterministically rebuild the expected view manifest for a case,
    entirely from ``case_data/testdata_prompt.json`` + the images physically
    present under ``case_data/`` — never from any product artifact. Raises on
    every §4.3 hard gate violation (fail closed)."""
    case_dir = Path(case_dir)
    case_data_root = case_dir / "case_data"
    meta_path = testdata_path(case_dir)
    if meta_path is None:
        raise ValueError(
            f"no case metadata found under {case_dir} "
            "(case_data/testdata_prompt.json or testdata_prompt.json)"
        )
    raw_bytes = meta_path.read_bytes()
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"case metadata is not valid JSON: {meta_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"case metadata root must be a JSON object: {meta_path}")
    case_metadata_sha256 = hash_bytes(raw_bytes)

    if not case_data_root.is_dir():
        raise ValueError(f"case_data directory not found: {case_data_root}")

    views_overlay = data.get("views") if isinstance(data.get("views"), dict) else {}
    dimensioned_declared = "dimensioned_views" in data
    dimensioned_stems = _dimensioned_stems_declared(data)
    # S-3: structured dimensioned_views (object list) activates the provenance-bound
    # wire; None = legacy (absent / stem-string list) ⇒ bools stay byte-identical.
    structured_dim, declared_image_hashes = _structured_dimensioned_map(data, case_metadata_sha256)

    entries: dict[str, "RequiredViewEntry | ExcludedInputEntry"] = {}
    expected_ids: dict[str, str] = {}
    classified_files: set[str] = set()
    floor_refs_seen: set[int] = set()
    direction_tokens_seen: set[str] = set()

    def _overlay_for(stem: str) -> dict:
        o = views_overlay.get(stem)
        return o if isinstance(o, dict) else {}

    # 1. Floor plans[] -> required_view(view_type=plan)
    for item in data.get("Floor plans") or []:
        if not isinstance(item, dict) or "path" not in item:
            raise ValueError(f"malformed 'Floor plans' entry: {item!r}")
        source_rel, basename, image_hash = _normalize_declared_path(case_dir, item.get("path"))
        input_id = Path(basename).stem
        floor = item.get("floor")
        if not isinstance(floor, int) or isinstance(floor, bool):
            raise ValueError(f"'Floor plans' entry missing integer 'floor': {item!r}")
        if floor in floor_refs_seen:
            raise ValueError(f"duplicate floor_ref in 'Floor plans': {floor}")
        floor_refs_seen.add(floor)

        overlay = _overlay_for(input_id)
        direction_semantics, semantics_source, azimuth_deg = _resolve_semantics(overlay)
        view_kind = _resolve_view_kind(overlay)
        # S-3: a structured dimensioned_views declaration is authoritative +
        # provenance-bound; the legacy per-plan contradiction check and bool only
        # apply to the legacy form (absent / stem-string list).
        if structured_dim is None:
            per_plan_flag = item.get("dimensioned")
            in_top_list = input_id in dimensioned_stems
            if isinstance(per_plan_flag, bool) and dimensioned_declared and per_plan_flag != in_top_list:
                raise ValueError(
                    f"dimensioned contradiction for {input_id!r}: 'Floor plans'.dimensioned="
                    f"{per_plan_flag} vs top-level dimensioned_views membership={in_top_list}"
                )
            legacy_dim = bool(per_plan_flag) or in_top_list
        else:
            legacy_dim = False
        dimensioned = _entry_dimensioned(input_id, overlay, legacy_dim, structured_dim, case_metadata_sha256)

        entry = RequiredViewEntry(
            input_id=input_id,
            source_image=source_rel,
            image_sha256=image_hash,
            view_type="plan",
            view_kind=view_kind,
            floor_ref=floor,
            declared_direction_token=None,
            direction_source="standard_assumption",
            direction_semantics=direction_semantics,
            semantics_source=semantics_source,
            azimuth_deg=azimuth_deg,
            building_view_direction=None,
            dimensioned=dimensioned,
            expected_output_id=_family_expected_output_id(
                _FLOOR_PLAN_FAMILY["output_id_transform"], input_id
            ),
            opening_evidence=_opening_evidence_for(
                "plan", stem=input_id, overlay=overlay,
                case_metadata_sha256=case_metadata_sha256,
            ),
        )
        _register(entries, expected_ids, classified_files, entry, basename)

    # 2. "<Dir> view path of the building" -> required_view(view_type=elevation)
    for key, token in _ELEVATION_KEYS.items():
        if key not in data:
            continue
        source_rel, basename, image_hash = _normalize_declared_path(case_dir, data.get(key))
        input_id = Path(basename).stem
        if token in direction_tokens_seen:
            raise ValueError(f"duplicate elevation direction: {token}")
        direction_tokens_seen.add(token)
        overlay = _overlay_for(input_id)
        direction_semantics, semantics_source, azimuth_deg = _resolve_semantics(overlay)
        view_kind = _resolve_view_kind(overlay)
        dimensioned = _entry_dimensioned(
            input_id, overlay, input_id in dimensioned_stems, structured_dim, case_metadata_sha256,
        )
        building_view_direction = token if direction_semantics == "building_axis" else None

        entry = RequiredViewEntry(
            input_id=input_id,
            source_image=source_rel,
            image_sha256=image_hash,
            view_type="elevation",
            view_kind=view_kind,
            floor_ref=None,
            declared_direction_token=token,
            direction_source="user",
            direction_semantics=direction_semantics,
            semantics_source=semantics_source,
            azimuth_deg=azimuth_deg,
            building_view_direction=building_view_direction,
            dimensioned=dimensioned,
            expected_output_id=_family_expected_output_id(
                _ELEVATION_FAMILY["output_id_transform"], input_id
            ),
            opening_evidence=_opening_evidence_for(
                "elevation", stem=input_id, overlay=overlay,
                case_metadata_sha256=case_metadata_sha256,
            ),
        )
        _register(entries, expected_ids, classified_files, entry, basename)

    # 3. known supplementary/site/detail metadata keys -> typed required_view
    for key, spec in _SUPPLEMENTARY_KEYS.items():
        if key not in data:
            continue
        source_rel, basename, image_hash = _normalize_declared_path(case_dir, data.get(key))
        input_id = Path(basename).stem
        overlay = _overlay_for(input_id)
        direction_semantics, semantics_source, azimuth_deg = _resolve_semantics(overlay)
        view_kind = _resolve_view_kind(overlay)
        dimensioned = _entry_dimensioned(
            input_id, overlay, input_id in dimensioned_stems, structured_dim, case_metadata_sha256,
        )
        view_type = spec["view_type"]

        entry = RequiredViewEntry(
            input_id=input_id,
            source_image=source_rel,
            image_sha256=image_hash,
            view_type=view_type,
            view_kind=view_kind,
            floor_ref=None,
            declared_direction_token=None,
            direction_source="standard_assumption",
            direction_semantics=direction_semantics,
            semantics_source=semantics_source,
            azimuth_deg=azimuth_deg,
            building_view_direction=None,
            dimensioned=dimensioned,
            expected_output_id=_family_expected_output_id(
                spec["output_id_transform"], input_id
            ),
            opening_evidence=_opening_evidence_for(
                view_type, stem=input_id, overlay=overlay,
                case_metadata_sha256=case_metadata_sha256,
            ),
        )
        _register(entries, expected_ids, classified_files, entry, basename)

    # R1-6 (派工单 §1.6): a structured declaration's source.image_sha256 must
    # match the view's REAL image hash. source_hash only proves the declaration
    # was not tampered with AFTER the fact, not that it was ever true — so a
    # forged sign-off (reviewer "hortonyyx" + a placeholder hash) is refused.
    # Legacy cases (no structured declaration) have an empty declared_image_hashes
    # and skip this entirely, so sm24/sm21 stay byte-identical.
    if declared_image_hashes:
        _real_image_hashes = {
            e.expected_output_id: e.image_sha256
            for e in entries.values()
            if isinstance(e, RequiredViewEntry)
        }
        for stem, declared in sorted(declared_image_hashes.items()):
            real = _real_image_hashes.get(stem)
            if real is None:
                raise ValueError(
                    f"dimensioned_views entry {stem!r} source declares a view with no "
                    f"matching required input image"
                )
            if declared != real:
                raise ValueError(
                    f"dimensioned_views entry {stem!r} source.image_sha256 mismatch — "
                    f"declared {declared} does not match the real image hash {real}; "
                    f"a forged sign-off declaration is refused (R1-6)"
                )

    # 4. explicit `views{}` overlay exclusions (non_drawing_asset / derived_working_copy
    #    declared by stem, not auto-detected — the only machine-readable path to mark a
    #    stray image excluded without inventing an undeclared-file loophole, §2)
    for stem, overlay in views_overlay.items():
        if not isinstance(overlay, dict) or "excluded_reason" not in overlay:
            continue
        declared_file = overlay.get("file") or f"{stem}.png"
        source_rel, basename, image_hash = _normalize_declared_path(case_dir, declared_file)
        entry = ExcludedInputEntry(
            input_id=stem,
            source_image=source_rel,
            image_sha256=image_hash,
            excluded_reason=overlay["excluded_reason"],
            parent_input_id=overlay.get("parent_input_id"),
        )
        if entry.excluded_reason == "derived_working_copy":
            _verify_derived_copy_relation(entries, entry)
        _register(entries, expected_ids, classified_files, entry, basename)

    # 5. auto-detected `<stem>_source.png` derived working copies (byte-identical
    #    to a classified parent — the existing `_source.png` convention, §2)
    for png in sorted(case_data_root.glob("*.png")):
        if png.name in classified_files or not png.stem.endswith("_source"):
            continue
        parent_stem = png.stem[: -len("_source")]
        parent_basename = f"{parent_stem}.png"
        parent_path = case_data_root / parent_basename
        image_hash = hash_file(png)
        if not parent_path.is_file():
            raise ValueError(
                f"derived_working_copy {png.name!r}: parent image {parent_basename!r} not found"
            )
        if hash_file(parent_path) != image_hash:
            raise ValueError(
                f"derived_working_copy {png.name!r}: byte mismatch against parent {parent_basename!r}"
            )
        entry = ExcludedInputEntry(
            input_id=png.stem,
            source_image=f"case_data/{png.name}",
            image_sha256=image_hash,
            excluded_reason="derived_working_copy",
            parent_input_id=parent_stem,
        )
        _register(entries, expected_ids, classified_files, entry, png.name)

    # 6. unclassified image hard gate ("audit-only undeclared" concept is dead)
    for png in sorted(case_data_root.glob("*.png")):
        if png.name not in classified_files:
            raise ValueError(
                f"unclassified image in case_data: {png.name!r} — declare it in case "
                "metadata ('Floor plans' / '<Dir> view path of the building' / a known "
                "supplementary key / a `views{}` override) or mark it excluded"
            )

    # 6b. dangling `views{}` overlay stems: an overlay row that never bound to a
    #     registered entry would otherwise be a silent no-op — a completeness
    #     assertion (CR-04) or exclusion the operator believes they declared but
    #     that never landed anywhere. Fail closed, never guess an entry (CR-05).
    for stem in views_overlay:
        if stem not in entries:
            raise ValueError(
                f"views{{}} overlay references unknown input {stem!r} — it does not "
                "match any declared floor plan / cardinal elevation / supplementary "
                "input, nor a registered excluded input (no declaration family; "
                "inputs are never invented from an overlay row)"
            )

    # CR-01: build the canonical payload as plain data, hash it, then run ONE
    # final strict parse — the returned object has passed the full validator
    # stack including the content-hash self-check (no model_copy bypass).
    ordered = [entries[k] for k in sorted(entries)]
    payload = {
        "view_manifest_schema_version": VIEW_MANIFEST_SCHEMA_VERSION,
        "claims_vocab_version": CLAIMS_VOCAB_VERSION,
        "generator_version": GENERATOR_VERSION,
        "completeness_ruleset_version": COMPLETENESS_RULESET_VERSION,
        "case_id": case_dir.name,
        "case_metadata_sha256": case_metadata_sha256,
        "entries": [e.model_dump(mode="json") for e in ordered],
    }
    payload["content_sha256"] = _content_hash_of_payload(payload)
    return ViewManifest.model_validate(payload)


# --------------------------------------------------------------------------- #
# §4.4 provision / verify API (no double-emitter)
# --------------------------------------------------------------------------- #
def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def provision_view_manifest(case_dir: Path | str, run_dir: Path | str) -> ViewManifest:
    """The **only** emitter of ``<run>/_run/view_manifest.json``. Idempotent —
    a second call with byte-identical case_data returns the existing manifest;
    a case_data change mid-run raises (INVARIANT: prevents a swapped-out case
    from silently re-scoping an in-flight run)."""
    case_dir = Path(case_dir)
    run_dir = Path(run_dir)
    expected = build_view_manifest(case_dir)
    path = run_meta_path(run_dir, VIEW_MANIFEST_NAME, for_write=True)
    if path.exists():
        try:
            existing = ViewManifest.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — fail closed on a corrupt on-disk manifest
            raise ValueError(f"existing view manifest at {path} is corrupt: {exc}") from exc
        if existing.content_sha256 != expected.content_sha256:
            raise ValueError(
                "view manifest drift: case_data (or testdata_prompt.json) changed after "
                f"this run was provisioned (on-disk content_sha256={existing.content_sha256}, "
                f"recomputed={expected.content_sha256}) — case_data must not change mid-run"
            )
        _provision_reading_exam_scope(case_dir, run_dir, existing)
        return existing
    _atomic_write_text(path, canonical_view_manifest_json(expected))
    _provision_reading_exam_scope(case_dir, run_dir, expected)
    return expected


@dataclass
class ViewManifestVerification:
    ok: bool
    reason: str = ""
    expected: ViewManifest | None = None
    on_disk: ViewManifest | None = None
    exam_scope: ReadingExamScope | None = None


def verify_view_manifest(case_dir: Path | str, run_dir: Path | str) -> ViewManifestVerification:
    """Read-only comparison of the on-disk manifest against an in-memory
    rebuild. Never writes. Used by ``validate_case``, judge-only/replay, and
    isolation build/merge."""
    case_dir = Path(case_dir)
    run_dir = Path(run_dir)
    try:
        expected = build_view_manifest(case_dir)
    except ValueError as exc:
        return ViewManifestVerification(ok=False, reason=f"cannot rebuild expected manifest: {exc}")
    path = run_meta_path(run_dir, VIEW_MANIFEST_NAME)
    if not path.exists():
        return ViewManifestVerification(ok=False, reason="view_manifest.json missing", expected=expected)
    try:
        on_disk = ViewManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — a corrupt on-disk manifest is a verification failure
        return ViewManifestVerification(
            ok=False, reason=f"on-disk view manifest is corrupt: {exc}", expected=expected
        )
    if on_disk.content_sha256 != expected.content_sha256:
        return ViewManifestVerification(
            ok=False,
            reason="view manifest drift (content_sha256 mismatch between on-disk and rebuilt)",
            expected=expected,
            on_disk=on_disk,
        )
    try:
        exam_scope = resolve_frozen_reading_exam_scope(run_dir, on_disk)
    except Exception as exc:  # noqa: BLE001
        return ViewManifestVerification(ok=False, reason=f"reading exam scope invalid: {exc}", expected=expected, on_disk=on_disk)
    return ViewManifestVerification(ok=True, expected=expected, on_disk=on_disk, exam_scope=exam_scope)


# --------------------------------------------------------------------------- #
# §2 staging input_inventory.json projection (reader-visible, denominator-free)
# --------------------------------------------------------------------------- #
def derive_input_inventory(manifest: ViewManifest, scope: ReadingExamScope | None = None) -> list[dict]:
    """Every required-view entry's reader-visible identity — "read this,
    produce that name" — with no negative-evidence/completeness content."""
    return [
        {
            "input_id": e.input_id,
            "file": e.source_image,
            "view_type": e.view_type,
            "declared_direction_token": e.declared_direction_token,
            "floor_ref": e.floor_ref,
            "expected_output_id": e.expected_output_id,
        }
        for e in manifest.required_entries()
        if scope is None or e.input_id in scope.input_ids
    ]


# B-1 (r3 batchC dispatch §1 BLOCKER): ``resolve_view_pixel_bounds`` used to
# live here — it resolved each view's case_data source image's real PIXEL
# size and fed it into gate①'s OCR-anchor/dimension-endpoint bounds checks as
# a "trusted" bound (r2's X-2). That was a dimensional-mismatch bug: reading
# coordinates are METRES, so a metre-scale garbage value (e.g. the exact
# repro payload ``[360, 450]``) compared cleanly less-than a 790-3000 px real
# image width/height and sailed straight through on every real case_data
# image — the regression gate only ever blocked it on the retired test
# suite's synthetic 2x2 px fixture. It has been removed rather than repaired:
# no externally-rooted pixel-per-metre ratio exists anywhere in this
# repository to convert its px output into a metre bound (the only
# calibration data, ``scale_origin``, is written by the reading-agent itself
# — the party being checked — so using it would fail decision_log.md §5.14's
# "the judged party cannot write the basis" test the same way the original
# bug did). The replacement is a self-contained, image-independent
# unit-anomaly check computed entirely from each view's own stroke geometry —
# see ``src.validator.checks.reading._structural_metric_reference``. This
# also closes M-2 (the per-stem silent fallback when a source image hashed/
# manifested fine but could not be PIL-decoded): there is no more image-decode
# step on this path at all, so that fallback path no longer exists.


__all__ = [
    "VIEW_MANIFEST_NAME",
    "READING_EXAM_SCOPE_NAME",
    "VIEW_MANIFEST_SCHEMA_VERSION",
    "GENERATOR_VERSION",
    "COMPLETENESS_RULESET_VERSION",
    "Hex64",
    "CaseMetadataSourceRef",
    "UserSourceRef",
    "DatasetSourceRef",
    "CompletenessSourceRef",
    "CompletenessAssertion",
    "Coverage",
    "OpeningEvidence",
    "RequiredViewEntry",
    "ExcludedInputEntry",
    "ManifestEntry",
    "ViewManifest",
    "ReadingExamScope",
    "ViewManifestVerification",
    "compute_content_hash",
    "canonical_view_manifest_json",
    "build_view_manifest",
    "provision_view_manifest",
    "verify_view_manifest",
    "resolve_frozen_reading_exam_scope",
    "derive_input_inventory",
]
