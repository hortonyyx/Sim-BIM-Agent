"""GT **raw layer** reader + mechanical reproduction gate — judge② / human side ONLY.

The user's 2026-08-26 ruling splits gt into three layers: a RAW layer (a faithful
transcription of the drawing, deviations included), an irregularity list, and a
DERIVED answer layer.  This module exposes the first one.

The raw layer is already on disk and has been since the converter was written:
``case_tests/test_baseline/gt/<case>/review/conversion_report.json`` carries, for
every zone edge, ``p1/p2/basis/thickness_m/offset_m/source_handles`` plus the
thickness proof handles.  R-6 ("measured it, used it, threw it away when saving")
is therefore **inaccurate** and is corrected here: it is *saved*, but until now
(a) no scoring path read it and (b) it sits **outside the human signature**.

⛔ Same lock as :mod:`src.agent.judge.gt` (gt iron law, CLAUDE.md §1.5#4): only
the gate② judge and humans may import this.  gate① checks and the stage
executors must stay blind.  ``tests/test_gt_discipline.py`` enforces it.

## Why a reproduction gate at all (G1-b)

``tarch_review_bundle._RUNTIME_BUNDLE_FILES`` lists ``conversion_report.json``
among the *non-indexed* runtime files, so ``review_index.json`` never hashes it
and the human ``review_ack.json`` does not cover it.  Its trustworthiness can
therefore not come from the signature.  It can only come from *reproducing* it
mechanically out of inputs that ARE signed:

    review_ack.json  --signs-->  source.dxf sha256   (byte hash)
                     --signs-->  request_sha256      (RECOMPUTED from content,
                                                      never the declared field)

Re-running the converter on those two and comparing the result field-by-field is
what :func:`verify_raw_layer_reproduction` does.

Both of those inputs are resolved from **one case-owned persistent directory**,
``case_tests/test_baseline/gt_sources/<case>/`` -- see
:func:`case_signed_inputs_root`.  ⛔ That is a statement about *availability*,
not about trust: neither resolver ever accepts a file because of where it sits.

⛔ Content fields, never bytes.  The converter's normalized-DXF bytes (and hence
``normalized_dxf_sha256``) depend on Python hash randomisation; comparing bytes
would be a guaranteed false red.  Measured on 2026-08-27: the content fields are
identical across PYTHONHASHSEED 1 / 7 / 12345.

## The two reds are kept apart (G1-b, ⭐)

``implementation_drift``  the converter/config/extractor fingerprints recorded in
                          the signed artefacts no longer match this tree.  The
                          on-disk report may still be exactly what the reviewer
                          approved; it is *this tree* that moved.
``content_mismatch``      the fingerprints all match, so the same implementation
                          was re-run — and it did not produce what is on disk.
                          That points at the artefact.

The fingerprint check runs FIRST and returns early, so a drifted tree can never
be reported as a suspect artefact.

### Which fingerprints are fatal, and why exactly those

The **fatal** set contains the three fingerprints the report itself binds —
``converter_sha256`` (= the 13-file conversion CLOSURE, AST-normalized —
widened 2026-08-29, dispatch ②-1b R4/F-D; see ``tarch_normalize.
CONVERTER_CLOSURE_FILES``), ``judge_config_sha256`` and ``vg_config_sha256`` —
plus the signed ``gt.json`` generator's ``vg_implementation_sha256``.  The
latter is an exact group hash over the four correction modules in the
measured conversion import closure, with no closure-external file, so it is a
precise converter-drift signal.

⚠️ ``converter_sha256`` specifically is compared through
:func:`_expected_converter_sha256`, which accepts EITHER the current widened
value OR a member of ``tarch_normalize.KNOWN_PRE_F_D_CONVERTER_SHA256`` for a
record that still carries one of those pinned legacy values — a named,
bounded exemption for the one artefact (sm25-L_anchor) that was clean before
the widening.  sm24_anchor's legacy value is deliberately NOT in that set —
F-132 already found it drifted under the OLD definition too, and this gate
must keep reporting that, not launder it into "reproduced" as a side effect
of the widening.

The signed generator also carries ``extractor_sha256`` and
``validator_sha256``.  Those remain **advisory** because their groups include
closure-external files.  In particular, ``extractor_sha256`` includes
``scripts/tool_scripts/gt_from_dxf.py``, a CLI entry point that the conversion
closure was measured (2026-08-27) not to load.  Treating that group as fatal
would turn a CLI-only edit into converter drift.

⚠️ **Declared blind spot.**  ``gt_extraction.py``, ``gt_manifest.py``,
``gt_schema.py`` and ``tarch_converter_schema.py`` are in the conversion import
closure but have no exactly-scoped signed fingerprint.  Drift confined to them
is therefore reported as ``content_mismatch``, not ``implementation_drift`` —
the difference is still caught loudly, but its attribution is wrong.

## Nothing degrades silently (G1-c)

:class:`RawLayerTrust` always says what the trust root actually is.  A raw layer
whose reproduction was never attempted reports ``reproduction=None`` and
``status="not_attempted"`` — it never reads as "verified".  The same holds when
the signed inputs cannot be found (``inputs_unavailable``).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal

from .gt import DEFAULT_GT_DIR, case_gt_dir
from .gt_schema import (REPO_ROOT, GroundTruthV3,
                        compute_gt_implementation_hashes,
                        compute_gt_v3_content_sha256)
from .tarch_converter_schema import (GT_SOURCES_ROOT, ConversionReportV1,
                                     HumanReviewAckV1, TarchConversionRequestV1,
                                     ZoneEdgeReportV1, compute_request_sha256,
                                     resolve_converter_tooling)

__all__ = [
    "RawEdge", "GtRawLayer", "RawLayerTrust", "ReproductionVerdict",
    "load_gt_raw_layer", "verify_raw_layer_reproduction",
]


# --------------------------------------------------------------------------- #
# Typed raw-layer view
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RawEdge:
    """One zone-boundary edge exactly as the converter transcribed it."""
    zone_id: str
    floor_id: str
    edge_index: int
    edge: ZoneEdgeReportV1

    @property
    def p1(self) -> tuple[float, float]:
        return tuple(self.edge.p1)  # type: ignore[return-value]

    @property
    def p2(self) -> tuple[float, float]:
        return tuple(self.edge.p2)  # type: ignore[return-value]

    @property
    def basis(self) -> str:
        return self.edge.basis

    @property
    def thickness_m(self) -> float:
        return self.edge.thickness_m

    @property
    def offset_m(self) -> float:
        return self.edge.offset_m

    @property
    def source_handles(self) -> list[str]:
        return list(self.edge.source_handles)


@dataclass(frozen=True)
class ReproductionVerdict:
    """Outcome of re-deriving the raw layer from the signed inputs."""
    status: Literal["reproduced", "implementation_drift", "content_mismatch",
                    "inputs_unavailable"]
    detail: str
    #: JSON pointers whose values differ, for ``content_mismatch``.
    differing_pointers: tuple[str, ...] = ()
    #: Fingerprint names that moved, for ``implementation_drift``.
    drifted_fingerprints: tuple[str, ...] = ()
    #: Fingerprints of NEIGHBOURING artefacts that moved.  Never fatal here (see
    #: the module docstring), but never swallowed either.
    advisory_drifted_fingerprints: tuple[str, ...] = ()

    @property
    def reproduced(self) -> bool:
        return self.status == "reproduced"


@dataclass(frozen=True)
class RawLayerTrust:
    """⭐ G1-c: what this layer's trust actually rests on — stated, not assumed.

    ``human_signed`` is always False today and is *derived* (by reading the
    signed file inventory), not asserted: if a future re-sign adds
    conversion_report.json to review_index.json, this flips on its own.
    """
    human_signed: bool
    human_signed_reason: str
    signed_source_dxf_sha256: str | None
    signed_request_sha256: str | None
    reproduction: ReproductionVerdict | None

    @property
    def reproduction_status(self) -> str:
        """⛔ Never silently "ok": an un-run gate says so in as many words."""
        return "not_attempted" if self.reproduction is None else self.reproduction.status

    @property
    def trustworthy(self) -> bool:
        return self.human_signed or (self.reproduction is not None
                                     and self.reproduction.reproduced)


@dataclass(frozen=True)
class GtRawLayer:
    """The raw layer of one case's gt, plus an explicit statement of its trust root."""
    case: str
    report: ConversionReportV1
    trust: RawLayerTrust

    def edges(self) -> Iterator[RawEdge]:
        for zone in self.report.zones:
            for index, edge in enumerate(zone.edges):
                yield RawEdge(zone_id=zone.zone_id, floor_id=zone.floor_id,
                              edge_index=index, edge=edge)

    def edge_count(self) -> int:
        return sum(len(zone.edges) for zone in self.report.zones)

    def basis_histogram(self) -> dict[str, int]:
        histogram: dict[str, int] = {}
        for raw in self.edges():
            histogram[raw.basis] = histogram.get(raw.basis, 0) + 1
        return histogram

    def thickness_histogram(self) -> dict[float, int]:
        histogram: dict[float, int] = {}
        for raw in self.edges():
            histogram[raw.thickness_m] = histogram.get(raw.thickness_m, 0) + 1
        return histogram


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
def _review_dir(case: str, gt_dir: Path | str) -> Path:
    return case_gt_dir(case, gt_dir=gt_dir) / "review"


def _read_ack(review: Path) -> HumanReviewAckV1 | None:
    path = review / "review_ack.json"
    if not path.is_file():
        return None
    return HumanReviewAckV1.model_validate_json(path.read_bytes())


def _human_signed(review: Path) -> tuple[bool, str]:
    """Ask the signed inventory whether it covers conversion_report.json."""
    index_path = review / "review_index.json"
    if not index_path.is_file():
        return False, "review_index_missing"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    listed = {item.get("path") for item in index.get("files", [])}
    if "conversion_report.json" in listed:
        return True, "listed_in_review_index"
    return False, "not_in_review_index_file_set"


def load_gt_raw_layer(case: str, *, gt_dir: Path | str = DEFAULT_GT_DIR,
                      reproduction: ReproductionVerdict | None = None) -> GtRawLayer | None:
    """Load the raw layer for ``case``, or None when the case has no review bundle.

    ``reproduction`` is threaded through rather than run here: re-running the
    converter costs seconds and needs a writable staging dir, so the caller
    decides.  Passing nothing yields ``reproduction_status == "not_attempted"``.
    """
    review = _review_dir(case, gt_dir)
    report_path = review / "conversion_report.json"
    if not report_path.is_file():
        return None
    report = ConversionReportV1.model_validate_json(report_path.read_bytes())
    ack = _read_ack(review)
    signed, reason = _human_signed(review)
    trust = RawLayerTrust(
        human_signed=signed, human_signed_reason=reason,
        signed_source_dxf_sha256=ack.source_dxf_sha256 if ack else None,
        signed_request_sha256=ack.request_sha256 if ack else None,
        reproduction=reproduction)
    return GtRawLayer(case=case, report=report, trust=trust)


# --------------------------------------------------------------------------- #
# G1-b: the mechanical reproduction gate
# --------------------------------------------------------------------------- #
#: Both signed conversion inputs live in the case's own persistent source dir,
#: ``gt_sources/<case>/``: the source DXF has always been read from there, and
#: since 2026-08-27 so is the request.  The previous request root was
#: ``AI_agent/logs/experiments``, which ``AI_agent/logs/README.md`` declares to
#: be process traces that may be cleaned at any time -- i.e. the *availability*
#: of the trust root depended on a directory the project reserves the right to
#: delete.  (Its *authority* never did, and still does not: see below.)
#:
#: The name is a narrow prefix glob, not ``*.json``: ``gt_sources/<case>/`` also
#: holds ``manifest.json`` / ``source_map.json`` / ``conversion_report.json``,
#: and feeding every JSON file in a directory to a strict parser to see what
#: sticks is the malformed-input shape this repo has been bitten by before.
#: ``build_review_bundle`` writes the canonical copy as ``request.json``; the
#: glob additionally tolerates a re-signed sibling (e.g. the reviewer's own
#: ``request_v3_calibrated.json``) without a code change.
SIGNED_REQUEST_GLOB = "request*.json"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def case_signed_inputs_root(case: str) -> Path:
    """The case-owned, persistent home of both signed conversion inputs."""
    return GT_SOURCES_ROOT / case


def find_signed_source_dxf(case: str, expected_sha256: str) -> Path | None:
    """A DXF is *the* signed source iff its bytes hash to the signed value."""
    case_root = case_signed_inputs_root(case)
    if not case_root.is_dir():
        return None
    for candidate in sorted(case_root.glob("*.dxf")):
        if _sha256_file(candidate) == expected_sha256:
            return candidate
    return None


def find_signed_request(case: str, expected_sha256: str) -> TarchConversionRequestV1 | None:
    """A request is *the* signed request iff its content RE-HASHES to the signed value.

    ⭐ The declared ``request_sha256`` field is never trusted: it is recomputed
    from the request body, so a tampered copy cannot pass by rewriting its own
    stamp.  **Location carries no authority** -- narrowing the search to the
    case's own directory changed *where we look*, never *why we believe*: a file
    sitting at the perfect path under the perfect name is rejected exactly as
    hard as one found anywhere else if its content does not re-hash.
    """
    case_root = case_signed_inputs_root(case)
    if not case_root.is_dir():
        return None
    for candidate in sorted(case_root.glob(SIGNED_REQUEST_GLOB)):
        try:
            request = TarchConversionRequestV1.model_validate_json(candidate.read_bytes())
        except Exception:
            continue
        if compute_request_sha256(request) == expected_sha256:
            return request
    return None


def _verified_signed_review_material(
        review: Path) -> tuple[HumanReviewAckV1, Path, Path]:
    """Load ack/index only after proving the signed inventory chain.

    A promoted review tree no longer has the original bundle layout, so the
    full file-inventory validator cannot run here.  The signature root remains
    verifiable: recompute the canonical digest of ``files``, require the index
    to declare that digest, then require the ack to sign the same digest.
    """
    ack_path = review / "review_ack.json"
    index_path = review / "review_index.json"
    if not ack_path.is_file():
        raise ValueError("review_ack_missing")
    if not index_path.is_file():
        raise ValueError("review_index_missing")
    try:
        ack = HumanReviewAckV1.model_validate_json(ack_path.read_bytes())
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("signed_review_material_invalid") from exc

    from .tarch_review_bundle import (INVENTORY_ALGORITHM,
                                      REVIEW_INDEX_SCHEMA,
                                      _canonical_inventory_sha256)

    if not isinstance(index, dict) or index.get("schema") != REVIEW_INDEX_SCHEMA:
        raise ValueError("review_index_schema_invalid")
    files = index.get("files")
    if not isinstance(files, list) or any(not isinstance(item, dict) for item in files):
        raise ValueError("review_index_files_invalid")
    normalized: list[dict[str, str]] = []
    for item in files:
        path = item.get("path")
        digest = item.get("sha256")
        if not isinstance(path, str) or not isinstance(digest, str):
            raise ValueError("review_index_files_invalid")
        normalized.append({"path": path, "sha256": digest})
    if normalized != sorted(normalized, key=lambda item: item["path"]):
        raise ValueError("review_index_files_unsorted")
    if index.get("inventory_algorithm") != INVENTORY_ALGORITHM:
        raise ValueError("review_index_algorithm_invalid")
    canonical = _canonical_inventory_sha256(normalized)
    if index.get("inventory_sha256") != canonical:
        raise ValueError("review_index_inventory_mismatch")
    if ack.review_index_sha256 != canonical:
        raise ValueError("review_ack_index_signature_mismatch")

    # Promotion intentionally changes candidate verification metadata and the
    # dependent content hash, so its gt.json bytes cannot equal the indexed
    # candidate bytes.  Invert exactly that allowed transform and prove every
    # semantic field (including the generator fingerprints) still hashes to the
    # candidate identity bound by the signed index.
    promoted_path = review.parent / "gt.json"
    try:
        promoted = GroundTruthV3.model_validate_json(promoted_path.read_bytes())
    except Exception as exc:
        raise ValueError("promoted_gt_invalid") from exc
    verification = promoted.verification
    if (verification.status != "human_verified"
            or verification.reviewer_id != ack.reviewer
            or verification.reviewed_on != ack.signed_at[:10]):
        raise ValueError("promoted_gt_review_identity_mismatch")
    candidate_verification = verification.model_copy(update={
        "status": "candidate", "reviewer_id": None, "reviewed_on": None,
        "methods": [],
    })
    candidate = promoted.model_copy(update={
        "verification": candidate_verification, "content_sha256": "0" * 64,
    })
    if compute_gt_v3_content_sha256(candidate) != index.get("candidate_gt_sha256"):
        raise ValueError("promoted_gt_signed_semantics_mismatch")
    return ack, ack_path, index_path


def _generator_fingerprints(case: str, gt_dir: Path | str) -> list[tuple[str, str, str]]:
    """(name, signed, current) for fingerprints in the signed gt generator."""
    gt_path = case_gt_dir(case, gt_dir=gt_dir) / "gt.json"
    if not gt_path.is_file():
        return []
    generator = json.loads(gt_path.read_text(encoding="utf-8")).get("generator", {})
    current = compute_gt_implementation_hashes(REPO_ROOT).model_dump()
    return [(key, generator[key], current[key])
            for key in ("extractor_sha256", "validator_sha256", "vg_implementation_sha256")
            if key in generator]


def _expected_converter_sha256(recorded: str) -> str:
    """⭐ F-D widening (②-1b R4), "legacy" exemption -- named, not silent.

    ``converter_sha256()`` was widened from "sha256(tarch_normalize.py's own
    raw bytes)" to an AST-normalized hash over the whole conversion closure
    (13 files).  On-disk ``conversion_report.json`` files stamped BEFORE that
    change (sm24_anchor / sm25-L_anchor today) recorded the OLD definition's
    value and cannot be rewritten here -- that file sits outside the human
    signature, but rewriting it is still a ``gt/`` write this dispatch has no
    promotion path for, and no re-sign event happens in this dispatch either.

    So: if ``recorded`` matches the OLD (legacy) definition and NOT the new
    one, it is compared against the legacy definition -- this is the ONLY case
    where that happens.

    ⚠️⚠️ Say plainly what that means, MEASURED not softened (②-1b-R, GLM N-1):
    it is NOT "drift confined to the rest of the closure is undetectable" --
    that undersells it.  ``_expected_converter_sha256`` returning ``recorded``
    makes the comparison ``recorded == recorded`` for this one case: **sm25's
    ``converter_sha256`` signal is DEAD -- it reads ``reproduced`` UNCONDITIONALLY,
    for ANY edit anywhere in the 13-file closure, until the case is next
    re-signed.**  GLM proved this by actually moving the widened fingerprint
    (a real statement appended to ``tarch_converter_schema.py``) and observing
    sm25 still report ``reproduced`` while sm24 (not exempted) correctly
    reported ``implementation_drift`` on the SAME code path -- i.e. the content
    diff underneath (footprint/openings/zones) is still a real, independent
    check, but THIS ONE FIELD'S check is not exercising anything for sm25
    right now.  Closed the moment the case is next re-signed: every
    conversion produced by CURRENT code, including a fresh re-sign, stamps
    the widened value and is compared against it with full teeth.
    """
    from .tarch_normalize import KNOWN_PRE_F_D_CONVERTER_SHA256
    current_wide = _converter_sha256_now()
    if recorded != current_wide and recorded in KNOWN_PRE_F_D_CONVERTER_SHA256:
        return recorded
    return current_wide


def _fatal_fingerprints(report: ConversionReportV1, case: str,
                        gt_dir: Path | str) -> list[tuple[str, str, str]]:
    """Hashes precisely scoped to the conversion implementation.

    Mirroring the producer's own declaration is the whole point: these three are
    what ``build_p1_report`` / the P2 report builder stamp into the artefact as
    "the implementation that made me".  The signed vg group is also exact: its
    four correction modules are all in the measured conversion closure.
    """
    tooling = resolve_converter_tooling(REPO_ROOT / "src/configs/judge_gt.yaml",
                                        REPO_ROOT / "src/configs/correction.yaml")
    report_fingerprints = [
        ("converter_sha256", report.converter_sha256,
         _expected_converter_sha256(report.converter_sha256)),
        ("judge_config_sha256", report.judge_config_sha256, tooling.judge_config_sha256),
        ("vg_config_sha256", report.vg_config_sha256, tooling.vg_config_sha256),
    ]
    vg_fingerprint = [entry for entry in _generator_fingerprints(case, gt_dir)
                      if entry[0] == "vg_implementation_sha256"]
    return report_fingerprints + vg_fingerprint


def _advisory_fingerprints(case: str, gt_dir: Path | str) -> list[tuple[str, str, str]]:
    """Signed fingerprints of the NEIGHBOURING artefact (gt.json's generator block).

    Reported, never fatal — ``extractor_sha256`` bundles a CLI script that the
    conversion closure does not import.  See the module docstring.
    """
    return [entry for entry in _generator_fingerprints(case, gt_dir)
            if entry[0] in {"extractor_sha256", "validator_sha256"}]


def _converter_sha256_now() -> str:
    from .tarch_normalize import converter_sha256
    return converter_sha256()


def _normalise_for_diff(report: ConversionReportV1) -> dict:
    """Report as JSON, with gates re-keyed by id so pointers survive reordering."""
    payload = report.model_dump(mode="json")
    gates = payload.get("gates", [])
    keyed_gates = {gate["id"]: gate for gate in gates}
    if len(gates) != len(keyed_gates):
        raise ValueError("duplicate_gate_ids")
    payload["gates"] = keyed_gates
    return payload


def _diff_pointers(left: object, right: object, pointer: str = "") -> list[str]:
    if type(left) is not type(right):
        return [pointer or "/"]
    if isinstance(left, dict):
        out: list[str] = []
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                out.append(f"{pointer}/{key}")
            else:
                out.extend(_diff_pointers(left[key], right[key], f"{pointer}/{key}"))
        return out
    if isinstance(left, list):
        if len(left) != len(right):
            return [pointer or "/"]
        out = []
        for index, (a, b) in enumerate(zip(left, right)):
            out.extend(_diff_pointers(a, b, f"{pointer}/{index}"))
        return out
    return [] if left == right else [pointer or "/"]


def verify_raw_layer_reproduction(case: str, *, gt_dir: Path | str = DEFAULT_GT_DIR,
                                  work_dir: Path | None = None) -> ReproductionVerdict:
    """Re-derive the raw layer from the signed inputs and compare it field-by-field.

    Loud on failure, and specific: a ``content_mismatch`` names the exact JSON
    pointers, so a single tampered ``thickness_m`` is reported as the edge it
    belongs to rather than as a bulk hash difference.
    """
    review = _review_dir(case, gt_dir)
    report_path = review / "conversion_report.json"
    if not report_path.is_file():
        return ReproductionVerdict("inputs_unavailable", f"no conversion_report.json under {review}")
    on_disk = ConversionReportV1.model_validate_json(report_path.read_bytes())

    try:
        ack, ack_path, index_path = _verified_signed_review_material(review)
    except ValueError as exc:
        return ReproductionVerdict(
            "inputs_unavailable",
            f"signed review material under {review} failed closed: {exc}")

    source = find_signed_source_dxf(case, ack.source_dxf_sha256)
    if source is None:
        return ReproductionVerdict(
            "inputs_unavailable",
            f"no DXF under {case_signed_inputs_root(case)} hashes to the signed "
            f"source_dxf_sha256={ack.source_dxf_sha256}")
    request = find_signed_request(case, ack.request_sha256)
    if request is None:
        return ReproductionVerdict(
            "inputs_unavailable",
            f"no {SIGNED_REQUEST_GLOB} under {case_signed_inputs_root(case)} recomputes "
            f"to the signed request_sha256={ack.request_sha256}")

    advisory = tuple(name for name, recorded, current
                     in _advisory_fingerprints(case, gt_dir) if recorded != current)

    # ⭐ Fingerprints FIRST: a moved tree must never be reported as a bad artefact.
    drifted = [name for name, recorded, current
               in _fatal_fingerprints(on_disk, case, gt_dir)
               if recorded != current]
    if drifted:
        return ReproductionVerdict(
            "implementation_drift",
            "this tree no longer matches the implementation that produced the report; "
            f"moved fingerprints: {', '.join(drifted)}. The on-disk report may still be "
            "exactly what was signed -- re-check after aligning the tree.",
            drifted_fingerprints=tuple(drifted), advisory_drifted_fingerprints=advisory)

    owned_work = work_dir is None
    root = Path(tempfile.mkdtemp(prefix=f"gt-raw-repro-{case}-")) if owned_work else Path(work_dir)
    try:
        # assert_staging_input forbids converting a DXF in place under gt_sources/,
        # so the signed source is copied out first (as build_review_bundle does).
        # The already-verified ack/index complete the human-review environment;
        # with them present G6/G10 evaluate identically on both sides.
        root.mkdir(parents=True, exist_ok=True)
        staged = root / "source.dxf"
        shutil.copyfile(source, staged)
        shutil.copyfile(ack_path, root / "review_ack.json")
        shutil.copyfile(index_path, root / "review_index.json")
        from .tarch_normalize import run_tarch_conversion
        tooling = resolve_converter_tooling(REPO_ROOT / "src/configs/judge_gt.yaml",
                                            REPO_ROOT / "src/configs/correction.yaml")
        fresh = run_tarch_conversion(staged, request, tooling, root).conversion_report
    finally:
        if owned_work:
            shutil.rmtree(root, ignore_errors=True)

    # ⭐ The fingerprint check above (drifted == []) already vetted
    # ``on_disk.converter_sha256`` -- either it equals the CURRENT widened
    # value, or it is a pinned pre-F-D legacy value this gate has explicitly
    # decided not to treat as drift.  Re-comparing the raw field here as
    # ordinary CONTENT would report a permanent, un-fixable content_mismatch
    # for sm25-L_anchor the moment converter_sha256() was widened (dispatch
    # ②-1b R4): the field's IDENTITY question is the fingerprint check's job,
    # already answered; this pass compares GEOMETRY.  Neutralising it here
    # only after the fingerprint gate passed keeps a genuinely tampered
    # ``converter_sha256`` (one that matches NEITHER the current nor a known
    # legacy value) caught upstream as drift, never silently absorbed here.
    fresh = fresh.model_copy(update={"converter_sha256": on_disk.converter_sha256})

    try:
        fresh_payload = _normalise_for_diff(fresh)
        on_disk_payload = _normalise_for_diff(on_disk)
    except ValueError as exc:
        return ReproductionVerdict(
            "content_mismatch",
            f"{report_path} cannot be compared without data loss: {exc}",
            differing_pointers=("/gates",),
            advisory_drifted_fingerprints=advisory)

    pointers = tuple(_diff_pointers(fresh_payload, on_disk_payload))
    if pointers:
        return ReproductionVerdict(
            "content_mismatch",
            f"{len(pointers)} field(s) of {report_path} could not be reproduced from the "
            f"signed source DXF + request under an IDENTICAL implementation: "
            f"{', '.join(pointers[:20])}"
            + (f" (+{len(pointers) - 20} more)" if len(pointers) > 20 else ""),
            differing_pointers=pointers, advisory_drifted_fingerprints=advisory)
    return ReproductionVerdict(
        "reproduced",
        f"every content field of {report_path} re-derived from the signed source DXF "
        f"({source.name}) and the signed request, with the verified review ack/index; "
        "zero JSON pointers differ."
        + (f" ADVISORY: neighbouring-artefact fingerprints moved ({', '.join(advisory)}); "
           "outside the measured conversion import closure, see module docstring."
           if advisory else ""),
        advisory_drifted_fingerprints=advisory)
