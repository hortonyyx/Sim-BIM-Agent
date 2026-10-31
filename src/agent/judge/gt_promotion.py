"""Fail-closed promotion of a signed GT v3 candidate into a verified answer root."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from .gt import DEFAULT_GT_DIR, load_gt_document, load_gt_file
from .gt_schema import (GroundTruthV3, canonical_gt_v3_bytes,
                        compute_gt_v3_content_sha256)
from .tarch_converter_schema import ConversionReportV1, HumanReviewAckV1
from .tarch_normalize import _verify_human_review_ack
from .tarch_review_bundle import validate_review_index


@dataclass(frozen=True)
class PromotionResult:
    case: str
    destination: Path
    content_sha256: str


TEST_GT_ROOT_MARKER = ".gt-promotion-test-root"


def _approved_target_root(gt_dir: Path) -> Path:
    root = Path(gt_dir).resolve()
    if root == Path(DEFAULT_GT_DIR).resolve():
        return root
    # A non-default root is available only to explicitly marked test fixtures.
    # Path location alone is not authority to create a verified-looking answer.
    if (root / TEST_GT_ROOT_MARKER).is_file():
        return root
    raise ValueError("promotion_gt_dir_unprotected")


def _all_gates_green(report: ConversionReportV1) -> bool:
    gates = {gate.id: gate.passed for gate in report.gates}
    # Defense in depth: ConversionReportV1 already rejects PASS reports with a
    # red gate, so all(gates.values()) is schema-redundant today.  Keep this
    # explicit check at the promotion trust boundary in case that schema rule
    # changes; the PASS-status branch remains independently meaningful.
    return report.status == "PASS" and set(gates) == {f"G{i}" for i in range(1, 11)} and all(gates.values())


def _verified_document(candidate: GroundTruthV3, ack: HumanReviewAckV1) -> GroundTruthV3:
    verification = candidate.verification.model_copy(update={
        "status": "human_verified", "reviewer_id": ack.reviewer,
        "reviewed_on": date.fromisoformat(ack.signed_at[:10]).isoformat(),
        # The signed conversion's G9 preflight is the required topology round-trip;
        # the GT v3 validator requires this complete, ordered evidence vocabulary.
        "methods": ["dxf_topology_roundtrip", "direct_gt_render", "overlay_on_original_drawing",
                    "human_source_comparison"],
    })
    provisional = candidate.model_copy(update={"verification": verification, "content_sha256": "0" * 64})
    return provisional.model_copy(update={"content_sha256": compute_gt_v3_content_sha256(provisional)})


def _assert_promotion_semantics(candidate: GroundTruthV3, promoted: GroundTruthV3) -> None:
    """Promotion may alter verification evidence and its dependent content hash only."""
    before = candidate.model_dump(mode="json")
    after = promoted.model_dump(mode="json")
    before.pop("verification"); before.pop("content_sha256")
    after.pop("verification"); after.pop("content_sha256")
    if before != after:
        raise ValueError("promotion_semantic_invariant_failed")


def _signed_inputs_root(target_root: Path) -> Path:
    """The case-owned signed-inputs root, sibling of ``target_root`` (F-117).

    ``target_root`` is already the approved home for ``gt/`` (default:
    ``case_tests/test_baseline/gt``; in tests, a ``TEST_GT_ROOT_MARKER``-marked
    ``tmp_path`` stand-in).  ``gt_sources/`` has always lived as its sibling --
    this mirrors that relationship instead of importing the production
    :data:`~.tarch_converter_schema.GT_SOURCES_ROOT` constant directly, so a
    test promoting into a marked ``tmp_path`` root can never make this function
    write into the real repo tree.  For the real ``gt_dir``, the two resolve to
    the exact same path (locked by ``test_f117_*``).
    """
    return target_root.parent / "gt_sources"


def _promote_signed_inputs(bundle_dir: Path, destination: Path) -> None:
    """Copy the two signed conversion inputs into their case-owned persistent home.

    F-117: until now, only a human ever placed a case's signed ``source.dxf`` +
    ``request.json`` under ``gt_sources/<case>/`` -- ``promote_gt_v3`` read
    ``request.json`` (for ``report.request_sha256``) and ``source.dxf`` out of
    the bundle root but never wrote either anywhere durable, so the next
    promoted case would reproduce F-111 (the reproduction gate's signed inputs
    resolver, :func:`~.gt_raw_layer.find_signed_request`, would find nothing).

    Mirrors the ``gt/<case>`` write next door: build in a sibling staging dir
    under the destination's own parent, then atomically rename into place, so
    an interruption mid-write can never leave a half-written
    ``gt_sources/<case>/`` behind.  Any exception here (including the
    pre-existing-destination guard the caller runs first) is left to the
    caller's own ``except Exception`` block, which already rolls the sibling
    ``gt/<case>`` write back -- promotion lands as one unit: source dxf +
    request.json land with the answer, or neither does.
    """
    sources_root = destination.parent
    sources_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=sources_root))
    try:
        shutil.copyfile(bundle_dir / "source.dxf", staging / "source.dxf")
        shutil.copyfile(bundle_dir / "request.json", staging / "request.json")
        os.replace(staging, destination)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


def promote_gt_v3(bundle_dir: Path, *, case: str, gt_dir: Path = DEFAULT_GT_DIR) -> PromotionResult:
    """Atomically promote a PASS, hash-bound candidate; every precondition is fail-closed."""
    root = Path(bundle_dir).resolve()
    target_root = _approved_target_root(Path(gt_dir))
    destination = target_root / case
    if destination.exists():
        raise FileExistsError("promotion_target_exists")
    sources_destination = _signed_inputs_root(target_root) / case
    if sources_destination.exists():
        raise FileExistsError("promotion_sources_target_exists")
    report = ConversionReportV1.model_validate_json((root / "conversion_report.json").read_bytes())
    if not _all_gates_green(report):
        raise ValueError("promotion_report_not_all_green")
    index = validate_review_index(root)
    ack_path = root / "review_ack.json"
    if not ack_path.is_file():
        raise ValueError("promotion_ack_missing")
    ack = HumanReviewAckV1.model_validate_json(ack_path.read_bytes())
    if ack.decision != "approved" or ack.review_index_sha256 != index["inventory_sha256"]:
        raise ValueError("promotion_ack_index_mismatch")
    request = SimpleNamespace(request_version=3, request_sha256=report.request_sha256)
    verified, _evidence = _verify_human_review_ack(root, request, root / "source.dxf", root / "overlay_plan.svg")
    if not verified:
        raise ValueError("promotion_ack_verification_failed")
    candidate = load_gt_file(root / "gt/gt.json", allow_legacy=False)
    if not isinstance(candidate, GroundTruthV3) or candidate.verification.status != "candidate":
        raise ValueError("promotion_candidate_status_invalid")
    if candidate.case != case or candidate.content_sha256 != index.get("candidate_gt_sha256"):
        raise ValueError("promotion_candidate_identity_mismatch")
    if compute_gt_v3_content_sha256(candidate) != candidate.content_sha256:
        raise ValueError("promotion_candidate_content_hash_mismatch")
    promoted = _verified_document(candidate, ack)
    _assert_promotion_semantics(candidate, promoted)
    data = canonical_gt_v3_bytes(promoted)
    target_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{case}.tmp-", dir=target_root))
    try:
        (staging / "renders").mkdir()
        (staging / "review").mkdir()
        (staging / "gt.json").write_bytes(data)
        for path in sorted((root / "gt/renders").glob("*.png")):
            shutil.copyfile(path, staging / "renders" / path.name)
        for name in ("review_index.json", "review_ack.json", "opening_elevation_audit.json", "review_annotations.json", "conversion_report.json"):
            shutil.copyfile(root / name, staging / "review" / name)
        os.replace(staging, destination)
        loaded = load_gt_document(case, gt_dir=target_root)
        if loaded is None or not isinstance(loaded, GroundTruthV3) or (destination / "gt.json").read_bytes() != data:
            raise ValueError("promotion_postwrite_selfcheck_failed")
        _promote_signed_inputs(root, sources_destination)
    except Exception:
        if destination.exists():
            shutil.rmtree(destination)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return PromotionResult(case=case, destination=destination, content_sha256=promoted.content_sha256)
