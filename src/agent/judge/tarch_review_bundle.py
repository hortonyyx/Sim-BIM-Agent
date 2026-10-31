"""Authoritative candidate review-bundle construction and signing helpers."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterable, Mapping

from .gt_extraction import ExtractionInputs, extract_gt_v3
from .gt_render_model import gt_to_render_model, render_elevation_model, render_plan_model
from .gt_schema import REPO_ROOT, canonical_gt_v3_bytes, compute_gt_implementation_hashes
from .tarch_converter_schema import (ConversionReportV1, HumanReviewAckV1,
                                     TarchConversionRequestV1, resolve_converter_tooling)
from .tarch_normalize import run_tarch_conversion


REVIEW_INDEX_SCHEMA = "tarch_review_index_v1"
INVENTORY_ALGORITHM = "sha256(json(files, sort_keys, separators=(',',':')) + b'\\n')"

# A review bundle is also the converter work directory.  These are the precise
# non-indexed runtime files produced or consumed by that workflow; every other
# file must be declared in review_index.json.  ``rasters/`` is deliberately the
# sole directory exception: it is the caller-supplied raster input tree and may
# contain a variable, nested set of source-view assets.  It is not review
# evidence and the renderer consumes it during bundle construction.
_RUNTIME_BUNDLE_FILES = frozenset({
    "conversion_report.json",  # converter PASS evidence
    "manifest.json",           # converter provenance
    "normalized.dxf",          # deterministic converter intermediate
    "overlay_plan.svg",        # existing human-ack verifier input
    "request.json",            # converter request input
    "review_ack.json",         # written after the signed review index
    "review_index.json",       # index root cannot index itself
    "source.dxf",              # copied converter source input
})
_RUNTIME_BUNDLE_DIRECTORIES = frozenset({"rasters"})


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_inventory_sha256(files: list[dict[str, str]]) -> str:
    payload = json.dumps(files, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
    return hashlib.sha256(payload).hexdigest()


def _sort_inventory_entries(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(entries, key=lambda item: item["path"])


def build_review_index(files: Iterable[Path], *, root: Path, candidate_gt_sha256: str,
                       manifest_sha256: str) -> dict:
    """Build the v1 inventory using the already-reviewed, frozen formula."""
    root = Path(root).resolve()
    entries = [{"path": Path(path).resolve().relative_to(root).as_posix(), "sha256": _sha256(Path(path))}
               for path in files]
    entries = _sort_inventory_entries(entries)
    return {"schema": REVIEW_INDEX_SCHEMA, "candidate_gt_sha256": candidate_gt_sha256,
            "manifest_sha256": manifest_sha256, "files": entries,
            "inventory_sha256": _canonical_inventory_sha256(entries),
            "inventory_algorithm": INVENTORY_ALGORITHM}


def validate_review_index(bundle_dir: Path) -> dict:
    """Fail closed unless every indexed candidate-review file remains byte-identical."""
    root = Path(bundle_dir).resolve()
    try:
        index = json.loads((root / "review_index.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("review_index_invalid") from exc
    if index.get("schema") != REVIEW_INDEX_SCHEMA:
        raise ValueError("review_index_schema_invalid")
    files = index.get("files")
    if not isinstance(files, list) or any(not isinstance(item, dict) for item in files):
        raise ValueError("review_index_files_invalid")
    normalized: list[dict[str, str]] = []
    for item in files:
        path = item.get("path"); digest = item.get("sha256")
        if not isinstance(path, str) or not isinstance(digest, str):
            raise ValueError("review_index_files_invalid")
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file() or _sha256(candidate) != digest:
            raise ValueError("review_index_file_hash_mismatch")
        normalized.append({"path": path, "sha256": digest})
    if normalized != sorted(normalized, key=lambda item: item["path"]):
        raise ValueError("review_index_files_unsorted")
    if index.get("inventory_algorithm") != INVENTORY_ALGORITHM:
        raise ValueError("review_index_algorithm_invalid")
    if index.get("inventory_sha256") != _canonical_inventory_sha256(normalized):
        raise ValueError("review_index_inventory_mismatch")
    expected = {path.relative_to(root).as_posix() for path in _review_files(root)}
    listed = {item["path"] for item in normalized}
    if listed != expected:
        raise ValueError("review_index_file_set_mismatch")
    allowed_files = listed | _RUNTIME_BUNDLE_FILES
    actual_files = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    # The bundle is what a reviewer sees.  Reject unlisted files rather than
    # letting an unsigned attachment coexist with the signed review evidence.
    if not actual_files <= allowed_files | {
        path for path in actual_files
        if path.split("/", 1)[0] in _RUNTIME_BUNDLE_DIRECTORIES
    }:
        raise ValueError("review_index_directory_file_set_mismatch")
    return index


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _review_files(root: Path) -> list[Path]:
    return [root / "gt/gt.json", *sorted((root / "gt/renders").glob("*.png")),
            root / "opening_elevation_audit.json", root / "review_annotations.json"]


def _render_candidate(document, manifest, root: Path, annotations: Mapping[str, str]) -> None:
    renders = root / "gt/renders"
    renders.mkdir(parents=True, exist_ok=True)
    model = gt_to_render_model(document)
    render_plan_model(model, review_annotations=annotations).save(renders / "gt_plan.png")
    render_elevation_model(model).save(renders / "gt_elev.png")
    from scripts.tool_scripts import render_gt_overlay
    images = render_gt_overlay.build_gt_overlay_images_v3(document, manifest, raster_root=root / "rasters",
                                                           review_annotations=annotations)
    for view_id, image in images.items():
        name = "overlay_1f_view.png" if view_id == "plan-F1" else f"overlay_{view_id}.png"
        image.save(renders / name)


def _write_candidate_outputs(root: Path, result, request: TarchConversionRequestV1,
                             tooling, annotations: Mapping[str, str]) -> None:
    document = extract_gt_v3(ExtractionInputs(result.augmented_dxf_path, result.manifest, tooling,
                                               compute_gt_implementation_hashes(REPO_ROOT)))
    gt_dir = root / "gt"; gt_dir.mkdir(parents=True, exist_ok=True)
    (gt_dir / "gt.json").write_bytes(canonical_gt_v3_bytes(document))
    _write_json(root / "manifest.json", result.manifest.model_dump(mode="json"))
    _write_json(root / "conversion_report.json", result.conversion_report.model_dump(mode="json"))
    _write_json(root / "opening_elevation_audit.json", {
        "candidate_gt_sha256": document.content_sha256,
        "manifest_sha256": result.manifest.manifest_sha256,
        "rows": result.conversion_report.elevation_audit_rows,
    })
    _render_candidate(document, result.manifest, root, annotations)


def build_review_bundle(source_dxf: Path, request: TarchConversionRequestV1, *, output_dir: Path,
                        raster_root: Path, review_annotations: Mapping[str, str], tooling=None) -> Path:
    """Create a candidate bundle in a sibling temporary directory then atomically rename."""
    output = Path(output_dir)
    if output.exists():
        raise FileExistsError("review_bundle_exists")
    source = Path(source_dxf)
    tooling = tooling or resolve_converter_tooling(REPO_ROOT / "src/configs/judge_gt.yaml",
                                                   REPO_ROOT / "src/configs/correction.yaml")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))
    try:
        bundled_source = staging / "source.dxf"
        shutil.copyfile(source, bundled_source)
        if _sha256(bundled_source) != request.source_dxf_sha256:
            raise ValueError("review_bundle_source_hash_mismatch")
        _write_json(staging / "request.json", request.model_dump(mode="json"))
        shutil.copytree(raster_root, staging / "rasters")
        _write_json(staging / "review_annotations.json", {"zone_roles": dict(review_annotations)})
        result = run_tarch_conversion(bundled_source, request, tooling, staging)
        _write_candidate_outputs(staging, result, request, tooling, review_annotations)
        document_sha = json.loads((staging / "gt/gt.json").read_text(encoding="utf-8"))["content_sha256"]
        index = build_review_index(_review_files(staging), root=staging, candidate_gt_sha256=document_sha,
                                   manifest_sha256=result.manifest.manifest_sha256)
        _write_json(staging / "review_index.json", index)
        os.replace(staging, output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return output


def sign_review_bundle(bundle_dir: Path, *, reviewer: str, signed_at: str,
                       confirm_near_threshold: bool = False) -> HumanReviewAckV1:
    """Validate a candidate bundle and create one non-overwritable human acknowledgement."""
    root = Path(bundle_dir).resolve()
    ack_path = root / "review_ack.json"
    if ack_path.exists():
        raise FileExistsError("review_ack_exists")
    index = validate_review_index(root)
    request = TarchConversionRequestV1.model_validate_json((root / "request.json").read_bytes())
    source = root / "source.dxf"
    if not source.is_file() or _sha256(source) != request.source_dxf_sha256:
        raise ValueError("review_bundle_source_hash_mismatch")
    report = ConversionReportV1.model_validate_json((root / "conversion_report.json").read_bytes())
    gates = {gate.id: gate for gate in report.gates}
    if set(gates) != {f"G{i}" for i in range(1, 11)} or any(not gates[f"G{i}"].passed for i in range(1, 11) if i not in {6, 10}):
        raise ValueError("review_bundle_nonreview_gate_failed")
    near_faces = gates["G6"].evidence.get("near_threshold_faces", [])
    if near_faces and not confirm_near_threshold:
        raise ValueError(f"review_bundle_near_threshold_confirmation_required:{near_faces}")
    ack = HumanReviewAckV1(reviewer=reviewer, signed_at=signed_at, decision="approved",
                           source_dxf_sha256=_sha256(source), request_sha256=request.request_sha256,
                           overlay_sha256=_sha256(root / "overlay_plan.svg"),
                           review_index_sha256=index["inventory_sha256"],
                           near_threshold_confirmed=confirm_near_threshold)
    _write_json(ack_path, ack.model_dump(mode="json"))
    return ack


def rerun_signed_review_bundle(bundle_dir: Path, *, tooling=None) -> None:
    """Perform the mandatory signed second conversion and refresh its PASS evidence."""
    root = Path(bundle_dir).resolve()
    request = TarchConversionRequestV1.model_validate_json((root / "request.json").read_bytes())
    tooling = tooling or resolve_converter_tooling(REPO_ROOT / "src/configs/judge_gt.yaml",
                                                   REPO_ROOT / "src/configs/correction.yaml")
    annotations = json.loads((root / "review_annotations.json").read_text(encoding="utf-8"))["zone_roles"]
    result = run_tarch_conversion(root / "source.dxf", request, tooling, root)
    _write_candidate_outputs(root, result, request, tooling, annotations)
    validate_review_index(root)
