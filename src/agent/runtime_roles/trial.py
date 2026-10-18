"""Isolated build_plan_bim trials for plan-reader deliverables."""

from __future__ import annotations

import base64
import copy
from collections.abc import Mapping
import hashlib
import json
from pathlib import Path
import shutil
import sys
from typing import Any

from PIL import Image, UnidentifiedImageError

from src.agent.geometry.plan_feedback import resolve_plan_lengths
from src.agent.geometry.plan_input import normalize_plan_fields, plan_error_hint
from src.agent.geometry.plan_dimension_alignment import align_plan_to_dimensions
from src.agent.geometry.plan_ink_alignment import _set_reading_alignment
from src.agent.geometry.plan_regularization import PlanRegularizationError
from src.agent.geometry.lite_bim_regularization import DEFAULT_LITE_GRID_M, regularize_lite_plan
from src.agent.geometry.profile_observation_binding import resolve_plan_pixels
from .plan_format import audit_plan_replacement, declaration_editability, format_failure, plan_format_errors
from .plan_review import loose_partition_ends, revise_operations, topology_issues, unhosted_openings
from .plan_reading import geometry_ready, product_identity


ROOT = Path(__file__).resolve().parents[3]
CORRIDOR_REVIEW_HINT = (
    "A corridor bend or continuation without a physical separator is one space. "
    "Several doors into a claimed single space are a reason to recheck the original "
    "for dividers; do not add a closing wall only to host a passage."
)


_REWORK_TARGET_EXAMPLES = "plan.openings, openings, openings:W1"


def normalize_rework_targets(targets):
    """Accept concise opening scopes while retaining the existing plan scope gate."""
    from src.agent.geometry.plan_revision import COLLECTIONS, SCALARS

    if not isinstance(targets, list) or not targets:
        raise ValueError("plan rework needs nonempty rework_targets; examples: "
                         + _REWORK_TARGET_EXAMPLES)
    normalized = []
    for target in targets:
        if not isinstance(target, str) or not target.strip():
            raise ValueError("unknown rework target; examples: " + _REWORK_TARGET_EXAMPLES)
        text = target.strip()
        if text in {"openings", "plan.openings"}:
            canonical = "plan.openings"
        else:
            body = text.removeprefix("plan.")
            field, separator, identity = body.partition(":")
            if separator and field == "openings" and identity.strip():
                canonical = "plan.openings:" + identity.strip()
            elif text.startswith("plan.") and (
                    (not separator and field in SCALARS)
                    or (separator and field in COLLECTIONS and identity.strip())):
                canonical = "plan." + field + (":" + identity.strip() if separator else "")
            else:
                raise ValueError(f"unknown rework target {text!r}; examples: "
                                 + _REWORK_TARGET_EXAMPLES)
        if canonical not in normalized:
            normalized.append(canonical)
    return normalized


def _expanded_rework_targets(targets, *plans):
    """Turn the openings collection scope into exact rows for the existing audit."""
    if targets is None:
        return None
    expanded = []
    for target in targets or []:
        if target != "plan.openings":
            expanded.append(target)
            continue
        identities = sorted({row.get("id") for plan in plans if isinstance(plan, Mapping)
                             for row in plan.get("openings", []) if isinstance(row, Mapping)
                             and isinstance(row.get("id"), str) and row["id"]})
        expanded.extend("plan.openings:" + identity for identity in identities)
    return list(dict.fromkeys(expanded))


def _plan_bytes(plan: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError(f"plan cannot be encoded as canonical JSON: {error}") from error


def _canonical_plan_bytes(plan: Mapping[str, Any]) -> bytes:
    normalized, _ = normalize_plan_fields(dict(plan))
    return _plan_bytes(normalized)


def canonical_plan_sha256(plan: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_plan_bytes(plan)).hexdigest()


class ReadingAlignmentError(PlanRegularizationError):
    """Preserve the actual stage and numeric input when reader preparation fails."""

    def __init__(self, error, *, stage, plan, reading, alignment):
        super().__init__(str(error), report=getattr(error, "report", None))
        self.numeric_plan = copy.deepcopy(plan)
        self.failure_context = {
            "failure_stage": stage,
            "reading_alignment": copy.deepcopy(reading),
            "alignment": {**copy.deepcopy(alignment), "status": "failed", "failed_stage": stage},
        }


def _reading_align(
    numeric: Mapping[str, Any], *, image_path: Path | None, image_name: str,
    grid_step_m: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Apply declared dimensions and the common grid in one observed pixel frame.

    Ink is visual evidence for the reader, not authority to move geometry under
    an independently declared calibration. The single-model path uses this same
    grid without an extra image-dependent mutation.
    """

    result = copy.deepcopy(dict(numeric))
    if result.get('reading_type') == 'plan':
        from src.agent.geometry.reader_wall_metadata import require_wall_thickness
        result = require_wall_thickness(result)
    reading_alignment: dict[str, Any] = {"ink": {
        "status": "not_applied",
        "reason": "Original-image ink is reviewed on the overlay; it does not overwrite declared pixels or calibration.",
        "summary": {"moved_or_aligned": 0, "not_moved": 0, "rejected": 0},
    }}
    alignment: dict[str, Any] = {
        "schema_version": "plan_trial_alignment_v1",
        "status": "not_checked",
        "junction_preparation": None,
        "post_alignment_preparation": None,
        "fallback": None,
    }
    image_size: tuple[int, int] | None = None
    if image_path is not None:
        try:
            with Image.open(image_path) as original:
                image_size = original.size
        except (UnidentifiedImageError, OSError):
            # Isolated tests may use placeholder bytes; Toolkit still checks
            # real image bounds when compiling the production draft.
            pass
    stage = "dimension_alignment"
    try:
        if "dimension_chains" in result:
            result, dimension_report = align_plan_to_dimensions(result, grid_step_m=grid_step_m)
            reading_alignment["dimensions"] = dimension_report
            if dimension_report["rejections"]:
                raise PlanRegularizationError(
                    "dimension chain: " + str(dimension_report["rejections"][0]), report=dimension_report)
    except (ValueError, TypeError, KeyError) as error:
        raise ReadingAlignmentError(error, stage=stage, plan=result,
                                    reading=reading_alignment, alignment=alignment) from error
    for key in ("ink", "dimensions"):
        report = reading_alignment.get(key)
        if isinstance(report, Mapping):
            _set_reading_alignment(result, key, report)
    if grid_step_m is not None:
        if image_size is None:
            # Isolated callers may have no decodable image. Bounds are inferred
            # only for numeric preflight; the real Toolkit checks image bounds.
            import math
            points = [*result["footprint_pixels"],
                      *(p for wall in result.get("partitions", []) for p in wall["points"]),
                      *(o[field] for o in result.get("openings", []) for field in ("p1", "p2")),
                      *(seed["point"] for seed in result.get("space_seeds", []))]
            image_size = tuple(max(2, math.ceil(max(
                [float(p[i]) for p in points] + [float(a[0]) for a in result[f"{axis}_anchors"]])) + 1)
                               for i, axis in enumerate(("x", "y")))
        try:
            result, lite_report = regularize_lite_plan(
                result, image_size=image_size, image_name=image_name, grid_step_m=grid_step_m)
        except (ValueError, TypeError, KeyError) as error:
            raise ReadingAlignmentError(error, stage="lite_regularization", plan=result,
                                        reading=reading_alignment, alignment=alignment) from error
        reading_alignment["lite_bim"] = lite_report
        alignment["status"] = "lite_grid_applied"
    return result, reading_alignment, alignment


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _literal_geometry_diagnostic(plan: Mapping[str, Any], *, image_size: tuple[int, int],
                                 image_name: str) -> dict[str, Any] | None:
    """Report independent original-geometry failures without adopting a draft.

    Call only after quantity/profile resolution and a dimension-stage rejection.
    The literal compiler cannot consume the reader's dimension chains; removing
    that field is the only input change. No junction preparation, grid, saved
    candidate, or source BIM is involved.
    """
    from src.agent.geometry.plan_partition import compile_plan_partition

    literal = copy.deepcopy(dict(plan))
    literal.pop("dimension_chains", None)
    try:
        compile_plan_partition(literal, image_size=image_size, image_name=image_name,
                               include_audit_metadata=False)
    except (ValueError, TypeError, KeyError) as error:
        result = {
            "status": "rejected", "reason": str(error),
            "repair_hint": plan_error_hint(dict(plan), str(error)),
        }
        for field, check in (("loose_partition_ends", loose_partition_ends),
                             ("unhosted_openings", unhosted_openings)):
            try:
                result[field] = check(plan)
            except (ValueError, TypeError, KeyError, IndexError):
                result[field] = []
        return result
    return None


def _audit_reference(receipt: Mapping[str, Any], field: str, value: object) -> dict[str, Any]:
    """Point model-visible audit data at the complete durable receipt."""

    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    reference: dict[str, Any] = {
        "json_pointer": f"/{field}",
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }
    if isinstance(receipt.get("receipt_file"), str):
        reference["receipt_file"] = receipt["receipt_file"]
    if isinstance(value, (list, dict)):
        reference["item_count"] = len(value)
    return reference


def _model_visible_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Keep actionable trial feedback while referencing duplicated audit detail.

    ``run`` and the on-disk receipt retain the complete provenance record.  This
    projection is used only at the model boundary, where image origins and the
    just-submitted operation audit otherwise repeat tens of thousands of
    characters without adding a repair target.
    """

    if receipt.get("status") == "passed":
        from .trial_feedback import passed_receipt
        return passed_receipt(receipt)

    visible = copy.deepcopy(dict(receipt))
    visible["returned_images"] = [
        {key: image[key] for key in ("file", "sha256") if isinstance(image.get(key), str)}
        for image in receipt.get("returned_images", [])
        if isinstance(image, Mapping)
    ]
    audit_refs = {}
    for field in ("operations", "plan_revision"):
        value = visible.pop(field, None)
        if value not in (None, [], {}):
            audit_refs[field] = _audit_reference(receipt, field, value)
    if audit_refs:
        visible["audit_refs"] = audit_refs
    failure = visible.get("regularization_failure")
    if isinstance(failure, Mapping):
        visible.setdefault("audit_refs", {})["regularization_failure"] = _audit_reference(
            receipt, "regularization_failure", failure)
        visible["regularization_failure"] = {
            field: copy.deepcopy(failure[field]) for field in ("status", "grid_step_m", "summary") if field in failure
        }
        visible["regularization_failure"]["rejections"] = copy.deepcopy(failure.get("rejections", [])[:4])
    diagnostic = receipt.get("literal_geometry_diagnostic")
    if isinstance(diagnostic, Mapping):
        visible.setdefault("audit_refs", {})["literal_geometry_diagnostic"] = _audit_reference(
            receipt, "literal_geometry_diagnostic", diagnostic)
    reason = visible.get("reason")
    if isinstance(reason, str) and len(reason) > 1200:
        visible.setdefault("audit_refs", {})["reason"] = _audit_reference(receipt, "reason", reason)
        visible["reason"] = (
            "Compilation reported many located geometry failures; use repair_hint and the located findings below. "
            "The complete compiler reason is preserved by audit_refs.reason."
        )
    changes = visible.get("changes")
    if isinstance(changes, list) and len(changes) > 12:
        visible.setdefault("audit_refs", {})["changes"] = _audit_reference(receipt, "changes", changes)
        visible["changes"] = copy.deepcopy(changes[:12])
        visible["changes_remaining"] = len(changes) - 12
    hint = visible.get("repair_hint")
    if isinstance(hint, Mapping):
        hint = copy.deepcopy(dict(hint))
        repairs = hint.get("junction_repairs")
        if isinstance(repairs, list) and len(repairs) > 8:
            visible.setdefault("audit_refs", {})["repair_hint"] = _audit_reference(
                receipt, "repair_hint", receipt["repair_hint"]
            )
            hint["junction_repairs"] = copy.deepcopy(repairs[:8])
            hint["junction_repairs_remaining"] = len(repairs) - 8
        visible["repair_hint"] = hint
    alignment = visible.get("reading_alignment")
    if isinstance(alignment, Mapping):
        compact = {}
        for key in ("ink", "dimensions", "lite_bim"):
            row = alignment.get(key)
            if isinstance(row, Mapping):
                compact[key] = {
                    field: copy.deepcopy(row[field])
                    for field in ("status", "summary", "search", "tolerances", "grid_step_m", "selection_basis") if field in row
                }
        compact["full_list"] = (
            "The complete reading_alignment record is saved in this trial receipt and "
            "regularization_inputs.reading_alignment of the compiled plan."
        )
        visible["reading_alignment"] = compact
    preflight = visible.get("alignment")
    if isinstance(preflight, Mapping):
        compact_preflight = {
            field: copy.deepcopy(preflight[field])
            for field in ("schema_version", "status") if field in preflight
        }
        fallback = preflight.get("fallback")
        if isinstance(fallback, Mapping):
            compact_preflight["fallback"] = {
                "status": fallback.get("status"),
                "has_aligned_compile_error": bool(fallback.get("aligned_compile_error")),
                "has_original_compile_error": bool(fallback.get("original_compile_error")),
            }
        for field in ("junction_preparation", "post_alignment_preparation"):
            row = preflight.get(field)
            if isinstance(row, Mapping):
                compact_preflight[field] = {
                    "status": row.get("status"),
                    "preflight_only": row.get("preflight_only"),
                    "changes_applied_to_returned_plan": row.get("changes_applied_to_returned_plan"),
                    "application": row.get("application"),
                    "has_compile_error_before": bool(row.get("compile_error_before")),
                    "has_compile_error": bool(row.get("compile_error")),
                    "has_rejection": bool(row.get("rejection")),
                    "changes": len(row.get("changes", [])),
                    "attempted_changes": len(row.get("attempted_changes", [])),
                }
        compact_preflight["full_record"] = _audit_reference(receipt, "alignment", preflight)
        visible["alignment"] = compact_preflight
    if visible.get("status") != "passed":
        paths = []
        hint = visible.get("repair_hint")
        if isinstance(hint, Mapping) and isinstance(hint.get("path"), str):
            paths.append(hint["path"])
        if isinstance(diagnostic, Mapping) and diagnostic.get("status") == "rejected":
            literal_hint = diagnostic.get("repair_hint")
            if isinstance(literal_hint, Mapping):
                for row in [literal_hint, *literal_hint.get("junction_repairs", [])]:
                    if isinstance(row, Mapping) and isinstance(row.get("path"), str) and row["path"] not in paths:
                        paths.append(row["path"])
        for row in visible.get("format_errors", []):
            if isinstance(row, Mapping) and isinstance(row.get("path"), str) and row["path"] not in paths:
                paths.append(row["path"])
        next_action = {
            "objects": paths[:16],
            "instruction": (
                "Edit the named objects/fields using repair_hint and the original drawing, then retry the complete plan "
                "or operations against an editable draft. Preserve all other observed walls, openings and room topology."
            ),
        }
        if isinstance(diagnostic, Mapping) and diagnostic.get("status") == "rejected":
            next_action["instruction"] = (
                "Resolve the primary dimension error and the independent original-geometry issues in "
                "literal_geometry_diagnostic together. Use its original-pixel repair_hint and the drawing; "
                "preserve other observed walls, openings and topology, then retry the plan or operations."
            )
        if len(paths) > 16:
            next_action["additional_object_count"] = len(paths) - 16
        visible["next_action"] = next_action
        visible["message"] = (
            "trial_plan_bim failed; revise the located objects below and keep the complete receipt as evidence."
        )
    # A compiled candidate can fail a later check while still carrying the same
    # enormous precision ledger as a passed trial. Preserve its failure actions.
    from .trial_feedback import bound_receipt_projection, reference, report_summary
    for field in ("building_precision", "drawing_differences", "corridor_review"):
        if field in receipt and len(_plan_bytes({"value": receipt[field]})) > 8000:
            visible[field] = report_summary(receipt, field)
            visible.setdefault("audit_refs", {})[field] = reference(receipt, "/" + field, receipt[field])
    visible["receipt_audit"] = reference(receipt, "", receipt)
    return bound_receipt_projection(receipt, visible)


def _inside(base: Path, relative: str) -> Path:
    target = (base / relative).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError as error:
        raise ValueError(f"trial receipt path escapes workspace: {relative!r}") from error
    return target


def _payload(result: object) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        raise ValueError("build_plan_bim returned a non-object MCP envelope")
    structured = result.get("structuredContent")
    if isinstance(structured, Mapping):
        return dict(structured)
    content = result.get("content", [])
    if isinstance(content, list):
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "text" and isinstance(block.get("text"), str):
                try:
                    decoded = json.loads(block["text"])
                except json.JSONDecodeError:
                    continue
                if isinstance(decoded, Mapping):
                    return dict(decoded)
    raise ValueError("build_plan_bim MCP envelope has no structured object result")


def _verified_report(workspace: Path | None, summary: object) -> object:
    if workspace is None or not isinstance(summary, Mapping):
        return summary
    relative, expected = summary.get("file"), summary.get("sha256")
    if not isinstance(relative, str) or not isinstance(expected, str):
        return summary
    path = _inside(workspace, relative)
    if not path.is_file() or _file_sha256(path) != expected:
        raise ValueError(f"trial report changed or is missing: {relative}")
    return json.loads(path.read_bytes())


def _requires_saved_toolkit_plan(plan_input: Mapping[str, Any]) -> bool:
    """Current A-C regularization always returns a saved effective plan."""

    return (isinstance(plan_input.get("regularization"), Mapping)
            or isinstance(plan_input.get("submitted_plan_file"), str))


def _corridor_review(workspace: Path | None, result: Mapping[str, Any],
                     precision: object, differences: object) -> dict[str, Any]:
    review: dict[str, Any] = {
        "status": "not_assessed",
        "source": "actual trial source, building_precision and drawing_differences",
        "corridor_space_ids": [],
        "precision_items": [],
        "drawing_difference_items": [],
        "hint": CORRIDOR_REVIEW_HINT,
    }
    candidate = result.get("candidate")
    if workspace is None or not isinstance(candidate, str):
        review["reason"] = "trial produced no saved source candidate"
        return review
    source_path = _inside(workspace, f"{candidate}/source_model.json")
    if not source_path.is_file():
        review["reason"] = "saved trial source_model.json is unavailable"
        return review
    source = json.loads(source_path.read_bytes())
    corridor_ids = sorted(
        row["id"] for row in source.get("spaces", [])
        if isinstance(row, Mapping) and row.get("role") == "corridor" and isinstance(row.get("id"), str)
    )
    review["corridor_space_ids"] = corridor_ids
    boundary_spaces = {
        row.get("id"): row.get("space_id") for row in source.get("boundaries", [])
        if isinstance(row, Mapping)
    }
    precision_items = precision.get("items", []) if isinstance(precision, Mapping) else []
    review["precision_items"] = [
        row for row in precision_items if isinstance(row, Mapping) and (
            row.get("space_id") in corridor_ids
            or any(boundary_spaces.get(identity) in corridor_ids for identity in row.get("boundary_ids", []))
        )
    ]
    difference_items = differences.get("items", []) if isinstance(differences, Mapping) else []
    suspicious_kinds = {"declared_divider_with_little_ink", "unsupported_open_separator"}
    review["drawing_difference_items"] = [
        row for row in difference_items
        if isinstance(row, Mapping) and (row.get("type", row.get("kind")) in suspicious_kinds
            or (row.get("type") == "opening_offset_from_gap" and "one continuous space" in row.get("check", "")))
    ]
    findings = []
    if len(corridor_ids) > 1:
        findings.append({
            "kind": "multiple_corridor_spaces",
            "count": len(corridor_ids),
            "meaning": "review whether the drawing contains physical separators between these spaces",
        })
    if review["precision_items"]:
        findings.append({"kind": "corridor_precision_findings", "count": len(review["precision_items"])})
    if review["drawing_difference_items"]:
        findings.append({
            "kind": "unsupported_corridor_separator_candidates",
            "count": len(review["drawing_difference_items"]),
            "meaning": "these trial drawing differences can indicate an invented cross-wall or open separator",
        })
    review["findings"] = findings
    if corridor_ids:
        review["status"] = "warning" if findings else "reported"
    else:
        review["reason"] = "no trial source space is labelled corridor; automatic corridor-specific filtering is unavailable"
    return review


class DraftRevisionRejected(ValueError):
    """A declaration edit rejected before any draft or compiler write."""


class PlanTrial:
    """Run the existing compiler/check/overlay stack in a child task service.

    ``tools`` points only at the writable trial workspace. It remains private;
    the reader receives this wrapper and can call only ``trial_plan_bim``.
    """

    def __init__(self, tools, *, image_name: str, receipt_directory: Path | None = None,
                 workspace: Path | None = None, profile_directory: Path | None = None,
                 grid_step_m: float | None = DEFAULT_LITE_GRID_M):
        if not isinstance(image_name, str) or not image_name.strip():
            raise ValueError("plan trial requires one image filename")
        self.tools = tools
        self.grid_step_m = grid_step_m
        self.image_name = image_name
        self.workspace = Path(workspace).resolve() if workspace is not None else None
        self.receipt_directory = Path(receipt_directory).resolve() if receipt_directory is not None else None
        self.profile_directory = (Path(profile_directory).resolve()
                                  if profile_directory is not None else None)
        self.receipts: list[dict[str, Any]] = []
        self._memory_plans: dict[str, dict[str, Any]] = {}
        self._memory_numeric: dict[str, dict[str, Any]] = {}
        self.reference_plan = None
        self.reference_validation_passed = None
        self.reference_receipt = None
        self._reference_report = None
        self._reference_profiles = {}
        self.allowed_rework_targets = None
        self.inherited_topology_issues = []
        self._memory_images: dict[str, list[dict[str, Any]]] = {}
        self._draft_profile_records = []
        self._load_receipts()

    def _relative_to_workspace(self, path: Path) -> str:
        if self.workspace is None:
            return str(path)
        return path.resolve().relative_to(self.workspace).as_posix()

    def _verify_failed_stage_input(self, receipt):
        filename = receipt.get("failed_stage_input_file")
        digest = receipt.get("failed_stage_input_sha256")
        if filename is None and digest is None:
            return
        if self.workspace is None and self.receipt_directory is None:
            return  # In-memory trial has no persisted input to restore.
        if not isinstance(filename, str) or not isinstance(digest, str):
            raise ValueError("trial failed-stage input receipt is incomplete")
        path = _inside(self.workspace or self.receipt_directory, filename)
        if not path.is_file() or _file_sha256(path) != digest:
            raise ValueError(f"trial failed-stage input changed: {path}")
        if not isinstance(json.loads(path.read_bytes()), dict):
            raise ValueError("trial failed-stage input must be a plan object")

    def _load_receipts(self) -> None:
        if self.receipt_directory is None or not self.receipt_directory.is_dir():
            return
        for path in sorted(self.receipt_directory.glob("trial_[0-9][0-9][0-9].json")):
            receipt = json.loads(path.read_bytes())
            if not isinstance(receipt, dict) or receipt.get("receipt_file") != self._relative_to_workspace(path):
                raise ValueError(f"invalid trial receipt identity: {path}")
            self._verify_failed_stage_input(receipt)
            input_path = _inside(self.workspace or self.receipt_directory, receipt["input_plan_file"])
            if not input_path.is_file() or _file_sha256(input_path) != receipt.get("plan_sha256"):
                raise ValueError(f"trial input plan changed: {input_path}")
            decoded = json.loads(input_path.read_bytes())
            try:
                decoded_sha256 = canonical_plan_sha256(decoded)
            except (ValueError, TypeError, KeyError):
                # A failed receipt may deliberately preserve contradictory
                # aliases that could not be normalized. Its exact bytes still
                # remain hash checked and available for repair.
                decoded_sha256 = hashlib.sha256(_plan_bytes(decoded)).hexdigest()
            if decoded_sha256 != receipt["plan_sha256"]:
                raise ValueError(f"trial input plan is not canonical/hash-consistent: {input_path}")
            numeric_file = receipt.get("compiled_numeric_plan_file")
            numeric_sha = receipt.get("compiled_numeric_plan_sha256")
            if numeric_file is not None or numeric_sha is not None:
                if not isinstance(numeric_file, str) or not isinstance(numeric_sha, str):
                    raise ValueError("trial numeric plan receipt is incomplete")
                numeric_path = _inside(self.workspace or self.receipt_directory, numeric_file)
                if not numeric_path.is_file() or _file_sha256(numeric_path) != numeric_sha:
                    raise ValueError(f"trial numeric plan changed: {numeric_path}")
                # Besides the byte hash, require the saved product to remain JSON.
                json.loads(numeric_path.read_bytes())
            aligned_file = receipt.get("aligned_numeric_input_file")
            aligned_sha = receipt.get("aligned_numeric_input_sha256")
            if aligned_file is not None or aligned_sha is not None:
                if not isinstance(aligned_file, str) or not isinstance(aligned_sha, str):
                    raise ValueError("trial aligned numeric input receipt is incomplete")
                aligned_path = _inside(self.workspace or self.receipt_directory, aligned_file)
                if not aligned_path.is_file() or _file_sha256(aligned_path) != aligned_sha:
                    raise ValueError(f"trial aligned numeric input changed: {aligned_path}")
                json.loads(aligned_path.read_bytes())
            unaligned_file = receipt.get("unaligned_numeric_input_file")
            unaligned_sha = receipt.get("unaligned_numeric_input_sha256")
            if unaligned_file is not None or unaligned_sha is not None:
                if not isinstance(unaligned_file, str) or not isinstance(unaligned_sha, str):
                    raise ValueError("trial unaligned numeric input receipt is incomplete")
                unaligned_path = _inside(self.workspace or self.receipt_directory, unaligned_file)
                if not unaligned_path.is_file() or _file_sha256(unaligned_path) != unaligned_sha:
                    raise ValueError(f"trial unaligned numeric input changed: {unaligned_path}")
                json.loads(unaligned_path.read_bytes())
            for profile in receipt.get("measurement_profiles", []):
                if not isinstance(profile, Mapping):
                    raise ValueError("trial measurement profile receipt is malformed")
                profile_path = _inside(
                    self.workspace or self.receipt_directory, profile.get("file", "")
                )
                if (not profile_path.is_file()
                        or _file_sha256(profile_path) != profile.get("sha256")):
                    raise ValueError(f"trial measurement profile changed: {profile_path}")
                profile_id = profile.get("profile_id")
                if not isinstance(profile_id, str) or not profile_id:
                    raise ValueError("trial measurement profile has no profile_id")
                inherited = self._reference_profiles.get(profile_id)
                if inherited and inherited["sha256"] != profile["sha256"]:
                    raise ValueError(f"trial measurement profile identity changed: {profile_id}")
                self._reference_profiles[profile_id] = {
                    "path": profile_path, "sha256": profile["sha256"],
                }
            if receipt.get("status") == "passed":
                candidate = receipt.get("candidate")
                if not isinstance(candidate, str):
                    raise ValueError("successful trial receipt has no candidate")
                product_file, product_sha = product_identity(receipt)
                source = _inside(self.workspace or self.receipt_directory, product_file)
                if not source.is_file() or _file_sha256(source) != product_sha:
                    raise ValueError(f"successful trial candidate changed: {candidate!r}")
            for image in receipt.get("returned_images", []):
                image_path = _inside(self.workspace or self.receipt_directory, image["file"])
                if not image_path.is_file() or _file_sha256(image_path) != image.get("sha256"):
                    raise ValueError(f"trial feedback image changed: {image_path}")
            self.receipts.append(receipt)

    def _existing(self, plan_hash: str) -> dict[str, Any] | None:
        return next((row for row in reversed(self.receipts) if row.get("plan_sha256") == plan_hash), None)

    def _successful(self, plan_hash=None):
        return next((row for row in reversed(self.receipts)
                     if row.get("status") == "passed" and geometry_ready(row)
                     and (plan_hash is None or row.get("plan_sha256") == plan_hash)), None)

    def _numeric_plan(self, plan: Mapping[str, Any], number: int) -> tuple[
        dict[str, Any], dict[str, Any], list[str], list[dict[str, Any]], list[dict[str, Any]],
        list[dict[str, Any]], Path | None, str, Path | None, str,
        dict[str, Any], dict[str, Any],
    ]:
        """Resolve only explicit aliases, quantities and selected profile candidates."""

        normalized, aliases = normalize_plan_fields(dict(plan))
        numeric, length_bindings = resolve_plan_lengths(normalized)
        used_profiles: dict[str, tuple[bytes, str]] = {}

        image_sha256 = "numeric-plan-has-no-profile-reference"
        image_path = None
        if self.workspace is not None:
            image_path = self.workspace / "images" / self.image_name
            if not image_path.is_file():
                raise ValueError("trial original image is missing")
            image_sha256 = _file_sha256(image_path)

        def load_profile(profile_id: str) -> dict[str, Any]:
            path = None
            if self.profile_directory is not None:
                folder = self.profile_directory.resolve()
                candidate = (folder / f"{profile_id}.json").resolve()
                if candidate.parent == folder and candidate.is_file():
                    path = candidate
            inherited = self._reference_profiles.get(profile_id)
            if path is None and inherited:
                path = inherited["path"]
            if path is None:
                raise ValueError(f"choose an existing profile_id returned by this reader: {profile_id}")
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if inherited and digest != inherited["sha256"]:
                raise ValueError(f"inherited profile changed or collides with a new reader profile: {profile_id}")
            used_profiles[profile_id] = (raw, digest)
            # Save selected source evidence before alignment/compilation, which
            # can fail while leaving a useful editable declaration.
            if self.receipt_directory is not None:
                target = self.receipt_directory / f"trial_{number:03d}_profiles" / f"{profile_id}.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
                row = {"profile_id": profile_id, "file": self._relative_to_workspace(target), "sha256": digest}
                if row not in self._draft_profile_records:
                    self._draft_profile_records.append(row)
            return {"record": json.loads(raw), "sha256": digest}

        numeric, measurement_bindings = resolve_plan_pixels(
            numeric,
            image=self.image_name,
            image_sha256=image_sha256,
            load_profile=load_profile,
        )
        unaligned_numeric = copy.deepcopy(numeric)
        unaligned_numeric_bytes = _canonical_plan_bytes(numeric)
        unaligned_numeric_sha256 = hashlib.sha256(unaligned_numeric_bytes).hexdigest()
        unaligned_numeric_path = None
        if self.receipt_directory is not None:
            self.receipt_directory.mkdir(parents=True, exist_ok=True)
            unaligned_numeric_path = self.receipt_directory / f"trial_{number:03d}_unaligned_input.json"
            unaligned_numeric_path.write_bytes(unaligned_numeric_bytes)
        try:
            numeric, reading_alignment, alignment = _reading_align(
                numeric, image_path=image_path, image_name=self.image_name, grid_step_m=self.grid_step_m,
            )
        except ReadingAlignmentError as error:
            failed_bytes = _canonical_plan_bytes(error.numeric_plan)
            failed_path = None
            if self.receipt_directory is not None:
                failed_path = self.receipt_directory / f"trial_{number:03d}_failed_stage_input.json"
                failed_path.write_bytes(failed_bytes)
            error.failure_context.update(
                unaligned_numeric_input_file=(self._relative_to_workspace(unaligned_numeric_path)
                                              if unaligned_numeric_path else None),
                unaligned_numeric_input_sha256=unaligned_numeric_sha256,
                failed_stage_input_file=(self._relative_to_workspace(failed_path) if failed_path else None),
                failed_stage_input_sha256=hashlib.sha256(failed_bytes).hexdigest(),
                field_aliases=aliases, length_bindings=length_bindings,
                measurement_bindings=measurement_bindings,
            )
            if error.failure_context.get("failure_stage") == "dimension_alignment" and image_path is not None:
                # Diagnose only the same resolved original whose bytes/hash were
                # saved above, never the partly dimension-aligned failure input.
                # A secondary diagnostic must not replace the primary failure.
                try:
                    with Image.open(image_path) as image:
                        diagnostic = _literal_geometry_diagnostic(
                            unaligned_numeric, image_size=image.size, image_name=self.image_name)
                except Exception as diagnostic_error:
                    diagnostic = {"status": "unavailable", "reason": str(diagnostic_error),
                                  "error_type": type(diagnostic_error).__name__}
                if diagnostic is not None:
                    error.failure_context["literal_geometry_diagnostic"] = {
                        **diagnostic,
                        "scope": "Independent literal check of resolved original pixels before dimension alignment; no geometry adopted.",
                        "input_file": (self._relative_to_workspace(unaligned_numeric_path)
                                       if unaligned_numeric_path else None),
                        "input_sha256": unaligned_numeric_sha256,
                        "image": self.image_name, "image_sha256": image_sha256,
                        "omitted_reader_fields": ["dimension_chains"],
                    }
            raise
        numeric_bytes = _canonical_plan_bytes(numeric)
        numeric_sha256 = hashlib.sha256(numeric_bytes).hexdigest()
        numeric_path = None
        profile_records = []
        if self.receipt_directory is not None:
            self.receipt_directory.mkdir(parents=True, exist_ok=True)
            numeric_path = self.receipt_directory / f"trial_{number:03d}_aligned_input.json"
            numeric_path.write_bytes(numeric_bytes)
            profile_folder = self.receipt_directory / f"trial_{number:03d}_profiles"
            for profile_id, (raw, digest) in sorted(used_profiles.items()):
                profile_folder.mkdir(parents=True, exist_ok=True)
                target = profile_folder / f"{profile_id}.json"
                target.write_bytes(raw)
                profile_records.append({
                    "profile_id": profile_id,
                    "file": self._relative_to_workspace(target),
                    "sha256": digest,
                })
        else:
            profile_records = [
                {"profile_id": profile_id, "file": None, "sha256": digest}
                for profile_id, (_, digest) in sorted(used_profiles.items())
            ]
        return (
            numeric,
            unaligned_numeric,
            aliases,
            length_bindings,
            measurement_bindings,
            profile_records,
            numeric_path,
            numeric_sha256,
            unaligned_numeric_path,
            unaligned_numeric_sha256,
            reading_alignment,
            alignment,
        )

    def _compiled_numeric_product(
        self,
        *,
        plan_input: Mapping[str, Any],
        aligned_plan: Mapping[str, Any],
        number: int,
    ) -> tuple[dict[str, Any], Path | None, str]:
        """Persist the effective plan which the trusted Toolkit actually compiled.

        Current Toolkit runs rewrite their saved ``plan_file`` after A-C
        regularization.  Legacy adapters and small mocks do not, so they retain
        the historical exact-input behavior.
        """

        strict_saved_plan = _requires_saved_toolkit_plan(plan_input)
        if strict_saved_plan:
            if self.workspace is None:
                raise ValueError("regularized Toolkit result cannot be verified without its workspace")
            relative = plan_input.get("plan_file")
            expected = plan_input.get("plan_sha256")
            if not isinstance(relative, str) or not isinstance(expected, str):
                raise ValueError("regularized Toolkit result omitted plan_file or plan_sha256")
            source = _inside(self.workspace, relative)
            if not source.is_file() or _file_sha256(source) != expected:
                raise ValueError(f"Toolkit effective plan changed or is missing: {relative}")
            if plan_input.get("image") not in {None, self.image_name}:
                raise ValueError("Toolkit effective plan belongs to a different image")
            declared_image_sha = plan_input.get("image_sha256")
            if declared_image_sha is not None:
                image_path = self.workspace / "images" / self.image_name
                if (not isinstance(declared_image_sha, str) or not image_path.is_file()
                        or _file_sha256(image_path) != declared_image_sha):
                    raise ValueError("Toolkit effective plan image hash does not match this workspace")
            raw = source.read_bytes()
            decoded = json.loads(raw)
            if not isinstance(decoded, dict):
                raise ValueError("Toolkit effective plan is not a JSON object")
        else:
            decoded = copy.deepcopy(dict(aligned_plan))
            raw = _canonical_plan_bytes(decoded)

        digest = hashlib.sha256(raw).hexdigest()
        path = None
        if self.receipt_directory is not None:
            self.receipt_directory.mkdir(parents=True, exist_ok=True)
            path = self.receipt_directory / f"trial_{number:03d}_numeric_plan.json"
            path.write_bytes(raw)
        return decoded, path, digest

    def _save_returned_images(self, raw: object, number: int, plan_hash: str) -> list[dict[str, Any]]:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("content"), list):
            return []
        origins = {}
        try:
            origins = self.tools.image_origins(raw)
        except (AttributeError, TypeError, ValueError):
            origins = {}
        records, memory = [], []
        for index, block in enumerate(raw["content"], 1):
            if not isinstance(block, Mapping) or block.get("type") != "image" or not isinstance(block.get("data"), str):
                continue
            try:
                data = base64.b64decode(block["data"], validate=True)
            except ValueError as error:
                raise ValueError("trial feedback image is not valid base64") from error
            digest = hashlib.sha256(data).hexdigest()
            mime = block.get("mimeType", "image/png")
            suffix = ".png" if mime == "image/png" else ".jpg"
            origin = dict(origins.get(digest, {}))
            origin.setdefault("sent_sha256", digest)
            origin.update({
                "source_image_name": self.image_name,
                "transport": "trial compiler/check feedback derived from the one admitted original and plan",
                "origin_status": "verified trial input lineage",
            })
            if self.workspace is not None:
                original = self.workspace / "images" / self.image_name
                if original.is_file():
                    origin["original_path"] = str(original)
                    origin["original_sha256"] = _file_sha256(original)
            record = {"sha256": digest, "mime_type": mime, "origin": origin}
            memory.append(dict(block))
            if self.receipt_directory is not None:
                folder = self.receipt_directory / f"trial_{number:03d}_images"
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / f"{index:02d}_{digest[:12]}{suffix}"
                if path.is_file() and path.read_bytes() != data:
                    raise ValueError(f"trial feedback image collision: {path}")
                if not path.is_file():
                    path.write_bytes(data)
                record["file"] = self._relative_to_workspace(path)
            records.append(record)
        self._memory_images[plan_hash] = memory
        return records

    def _image_content(self, receipt: Mapping[str, Any]) -> list[dict[str, Any]]:
        result = self._memory_images.get(str(receipt.get("plan_sha256")), [])
        if result:
            return [dict(row) for row in result]
        if self.workspace is None:
            return []
        blocks = []
        for image in receipt.get("returned_images", []):
            path = _inside(self.workspace, image["file"])
            blocks.append({
                "type": "image",
                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                "mimeType": image["mime_type"],
            })
        return blocks

    def baseline(self):
        """Continue the latest editable declaration; accepted geometry stays separate."""
        for receipt in reversed(self.receipts):
            plan = self.load_plan(receipt)
            if declaration_editability(plan)["editable"]:
                return plan, receipt["plan_sha256"]
        if self.reference_plan is not None:
            return copy.deepcopy(self.reference_plan), canonical_plan_sha256(self.reference_plan)
        return None, None

    def read_draft(self, *, trial_id=None, field=None, object_id=None):
        """Read the saved declaration, including rejected formats, without compiling."""
        receipt = (self.receipts[-1] if trial_id in (None, "latest") and self.receipts else
                   next((row for row in self.receipts if row["trial_id"] == trial_id), None))
        if receipt is not None:
            if isinstance(field, str) and field.startswith("report:"):
                self._verify_failed_stage_input(receipt)
                if self.workspace is not None:
                    saved = _inside(self.workspace, receipt["receipt_file"])
                    if not saved.is_file() or json.loads(saved.read_bytes()) != receipt:
                        raise ValueError("trial receipt changed since execution")
                from .trial_feedback import read_report
                return read_report(receipt, field.removeprefix("report:"), page=object_id)
            plan = self.load_plan(receipt)
            identity = {key: receipt.get(key) for key in ("trial_id", "plan_sha256", "status", "receipt_file", "input_plan_file")}
        elif trial_id in (None, "latest", "reference") and self.reference_plan is not None:
            if isinstance(field, str) and field.startswith("report:"):
                if self._reference_report is None:
                    raise ValueError("inherited draft has no bound full trial report")
                prior, receipt_file, digest = self._reference_report
                inherited = next((row for row in prior.receipts if row.get("receipt_file") == receipt_file), None)
                if inherited is None or hashlib.sha256(_plan_bytes(inherited)).hexdigest() != digest:
                    raise ValueError("inherited trial full receipt changed since admission")
                prior._verify_failed_stage_input(inherited)
                if prior.workspace is None:
                    raise ValueError("inherited trial report has no durable workspace")
                path = _inside(prior.workspace, receipt_file)
                if not path.is_file() or json.loads(path.read_bytes()) != inherited:
                    raise ValueError("inherited trial receipt changed since execution")
                from .trial_feedback import read_report
                return {**read_report(inherited, field.removeprefix("report:"), page=object_id,
                                      read_trial_id="reference"), "inherited": True}
            plan = copy.deepcopy(self.reference_plan)
            identity = {**(self.reference_receipt or {}), "inherited": True}
        else:
            raise ValueError("No saved declaration for this trial_id")
        _, baseline_sha = self.baseline()
        passed = self._successful()
        result = {**identity, **declaration_editability(plan),
                  "operations_base_sha256": baseline_sha,
                  "last_passed_plan_sha256": passed["plan_sha256"] if passed else
                      (self.reference_receipt or {}).get("plan_sha256") if self.reference_validation_passed else None,
                  "submission_requires_passed_trial": True}
        from .trial_feedback import read_plan
        return {**result, **read_plan(plan, identity, field=field, object_id=object_id,
                                    read_trial_id="reference" if identity.get("inherited") else None)}

    def _verified_original_image(self, expected_sha256: str) -> None:
        if self.workspace is None or not isinstance(expected_sha256, str) or not expected_sha256:
            raise ValueError("inherited trial needs the expected original image sha256")
        manifest_path = self.workspace / "inputs.json"
        if not manifest_path.is_file():
            raise ValueError("inherited trial has no inputs.json")
        manifest = json.loads(manifest_path.read_bytes())
        recorded = (manifest.get("images", {}).get(self.image_name, {}).get("sha256")
                    if isinstance(manifest, Mapping) else None)
        image_path = self.workspace / "images" / self.image_name
        if recorded != expected_sha256 or not image_path.is_file() or _file_sha256(image_path) != expected_sha256:
            raise ValueError("inherited trial original image does not match this reader task")

    def _inherit_profiles(self, prior, receipt) -> None:
        profiles = {}
        for row in receipt.get("measurement_profiles", []):
            if not isinstance(row, Mapping) or not isinstance(row.get("profile_id"), str):
                raise ValueError("inherited trial measurement profile is malformed")
            path = _inside(prior.workspace, row.get("file", ""))
            if not path.is_file() or _file_sha256(path) != row.get("sha256"):
                raise ValueError(f"inherited trial measurement profile changed: {row['profile_id']}")
            target = path
            if self.profile_directory is not None:
                self.profile_directory.mkdir(parents=True, exist_ok=True)
                target = self.profile_directory / f"{row['profile_id']}.json"
                if target.is_file() and _file_sha256(target) != row["sha256"]:
                    raise ValueError(f"inherited profile collides with current reader evidence: {row['profile_id']}")
                if not target.is_file():
                    shutil.copyfile(path, target)
            profiles[row["profile_id"]] = {"path": target, "sha256": row["sha256"]}
        self._reference_profiles = profiles

    def inherited_reference_ids(self) -> set[str]:
        """References copied into this same-image reader scope."""
        return set(self._reference_profiles)

    def verified_failed_draft(self, expected_image_sha256: str, *, receipt_file: str,
                              plan_sha256: str, compiled_numeric_plan_sha256: str | None = None):
        """Return one event-selected failed declaration, never an accepted plan.

        The caller must bind the selection to a known-failed tool event.  This
        method verifies its exact receipt and products; it never scans for a
        newer workspace file because that file may belong to an unknown write.
        """
        receipt = next((row for row in self.receipts
                        if row.get("receipt_file") == receipt_file
                        and row.get("plan_sha256") == plan_sha256), None)
        if receipt is None:
            raise ValueError("known failed trial event does not match a saved receipt")
        if (receipt.get("status") != "failed"
                or not isinstance(receipt.get("input_plan_file"), str)
                or not declaration_editability(self.load_plan(receipt))["editable"]):
            raise ValueError("known failed trial has no unambiguous editable declaration")
        if (compiled_numeric_plan_sha256 is not None
                and receipt["compiled_numeric_plan_sha256"] != compiled_numeric_plan_sha256):
            raise ValueError("known failed trial numeric plan differs from its event result")
        self._verified_original_image(expected_image_sha256)
        if self.workspace is None or not isinstance(receipt.get("receipt_file"), str):
            raise ValueError("editable failed trial has no durable receipt")
        saved = _inside(self.workspace, receipt["receipt_file"])
        if not saved.is_file() or json.loads(saved.read_bytes()) != receipt:
            raise ValueError("failed trial receipt changed since execution")
        self._verify_failed_stage_input(receipt)
        plan = self.load_plan(receipt)
        if canonical_plan_sha256(plan) != receipt["plan_sha256"]:
            raise ValueError("failed trial declaration hash mismatch")
        if receipt.get("compiled_numeric_plan_sha256") is not None:
            self.numeric_plan(receipt)
        return plan, {"validation_passed": False, "editable_draft": True, **receipt}

    def verified_unsubmitted_draft(self, expected_image_sha256: str, *, receipt_file: str,
                                  plan_sha256: str, compiled_numeric_plan_sha256: str,
                                  candidate: str, candidate_source_sha256: str | None = None,
                                  reading_product_file: str | None = None, reading_product_sha256: str | None = None,
                                  expected_receipt_sha256: str | None = None):
        """Verify one event-bound passed draft without treating it as a submission."""
        identities = {"receipt_file": receipt_file, "plan_sha256": plan_sha256,
                      "compiled_numeric_plan_sha256": compiled_numeric_plan_sha256,
                      "candidate": candidate}
        identities.update({"reading_product_file": reading_product_file, "reading_product_sha256": reading_product_sha256}
                          if reading_product_file else {"candidate_source_sha256": candidate_source_sha256})
        if any(not isinstance(value, str) or not value for value in identities.values()):
            raise ValueError("known passed trial event requires complete product identities")
        receipt = next((row for row in self.receipts
                        if row.get("receipt_file") == receipt_file and row.get("plan_sha256") == plan_sha256), None)
        if receipt is None or receipt.get("status") != "passed" or not geometry_ready(receipt):
            raise ValueError("known passed trial event does not match a saved successful receipt")
        if any(receipt.get(key) != value for key, value in identities.items()):
            raise ValueError("known passed trial products differ from the event result")
        if not isinstance(expected_receipt_sha256, str) or not expected_receipt_sha256:
            raise ValueError("known passed trial requires an event-bound full receipt digest")
        if hashlib.sha256(_plan_bytes(receipt)).hexdigest() != expected_receipt_sha256:
            raise ValueError("known passed trial full receipt differs from the event result")
        self._verified_original_image(expected_image_sha256)
        plan, verified = self.verified_plan(plan_sha256)
        if any(verified.get(key) != value for key, value in identities.items()):
            raise ValueError("verified successful trial differs from the event-selected receipt")
        return plan, verified

    def inherit_failed_reference(self, prior, expected_image_sha256: str, *, receipt_file: str,
                                 plan_sha256: str, compiled_numeric_plan_sha256: str | None = None,
                                 allowed_targets=None):
        """Use a same-image failed declaration as an editable, non-submit-ready base."""
        selected = prior.verified_failed_draft(
            expected_image_sha256, receipt_file=receipt_file, plan_sha256=plan_sha256,
            compiled_numeric_plan_sha256=compiled_numeric_plan_sha256,
        )
        plan, receipt = selected
        original = next(row for row in prior.receipts if row.get("receipt_file") == receipt_file
                        and row.get("plan_sha256") == plan_sha256)
        self._reference_report = (prior, receipt_file, hashlib.sha256(_plan_bytes(original)).hexdigest())
        self.reference_plan = copy.deepcopy(plan)
        self.reference_validation_passed = False
        self.reference_receipt = {
            "status": "failed", "validation_passed": False,
            "trial_id": receipt["trial_id"],
            "plan_sha256": receipt["plan_sha256"],
            "receipt_file": receipt["receipt_file"],
            "input_plan_file": receipt.get("input_plan_file"),
            "compiled_numeric_plan_sha256": receipt.get("compiled_numeric_plan_sha256"),
        }
        self.allowed_rework_targets = normalize_rework_targets(allowed_targets) if allowed_targets else None
        self._inherit_profiles(prior, receipt)
        return copy.deepcopy(self.reference_receipt)

    def inherit_reference(self, prior, plan_sha256, allowed_targets):
        from src.agent.geometry.plan_revision import SCALARS, COLLECTIONS

        allowed_targets = normalize_rework_targets(allowed_targets)
        for target in allowed_targets:
            if not isinstance(target, str):
                raise ValueError("rework_targets must name plan fields or collection:id objects")
            field, separator, identity = target.removeprefix("plan.").partition(":")
            if not target.startswith("plan.") or not (
                (not separator and field in SCALARS)
                or target == "plan.openings"
                or (separator and field in COLLECTIONS and identity.strip())
            ):
                raise ValueError(f"rework_targets must name an editable plan field or collection:id: {target}")
        plan, receipt = prior.verified_plan(plan_sha256)
        # Keep the prior report privately accessible; the default declaration
        # read stays small. Its admission digest also protects later local reads.
        original = next(row for row in prior.receipts if row.get("receipt_file") == receipt.get("receipt_file")
                        and row.get("plan_sha256") == plan_sha256)
        self._reference_report = (prior, original.get("receipt_file"), hashlib.sha256(_plan_bytes(original)).hexdigest())
        self.reference_plan = copy.deepcopy(plan)
        self.reference_validation_passed = True
        self.reference_receipt = {
            "status": "passed", "validation_passed": True,
            "trial_id": receipt["trial_id"],
            "plan_sha256": receipt["plan_sha256"],
            "input_plan_file": receipt.get("input_plan_file"),
        }
        if isinstance(receipt.get("receipt_file"), str):
            self.reference_receipt["receipt_file"] = receipt["receipt_file"]
        self.allowed_rework_targets = list(allowed_targets)
        self.inherited_topology_issues = copy.deepcopy(receipt.get("topology_issues", []))
        self._inherit_profiles(prior, receipt)

    async def run(self, plan=None, *, operations=None) -> dict[str, Any]:
        if (plan is None) == (operations is None):
            raise ValueError("trial_plan_bim requires exactly one of plan (draft) or operations (revision)")
        previous_plan, expected_base = self.baseline()
        preservation = None
        if operations is not None:
            if previous_plan is None:
                raise ValueError("No editable plan yet; submit a complete plan with valid fields and pixel references")
            manifest_path = self.workspace / "inputs.json" if self.workspace else None
            image_size = (json.loads(manifest_path.read_bytes()).get("images", {}).get(self.image_name, {}).get("size")
                          if manifest_path and manifest_path.is_file() else None)
            try:
                plan, preservation = revise_operations(previous_plan, operations, image_size=image_size,
                                                       allowed_targets=None)
            except ValueError as error:
                raise DraftRevisionRejected(str(error)) from error
        if not isinstance(plan, Mapping):
            raise ValueError("trial plan must be an object")
        errors = plan_format_errors(plan)
        try:
            normalized, _ = normalize_plan_fields(dict(plan))
        except (ValueError, TypeError, KeyError):
            normalized = copy.deepcopy(dict(plan))
        if previous_plan is not None and declaration_editability(normalized)["editable"]:
            try:
                audit = audit_plan_replacement(previous_plan, normalized,
                                              allowed_targets=_expanded_rework_targets(
                                                  self.allowed_rework_targets, previous_plan, normalized))
            except ValueError as error:
                raise DraftRevisionRejected(str(error)) from error
            preservation = {**(preservation or {}), **audit}
        try:
            plan_bytes = _plan_bytes(normalized)
        except ValueError:
            if errors:
                return format_failure(errors)
            raise
        plan_hash = hashlib.sha256(plan_bytes).hexdigest()
        existing = self._existing(plan_hash)
        if existing is not None and existing is self.receipts[-1] and operations is None:
            return existing
        change_report = preservation["actual_changes"] if preservation else []
        number = len(self.receipts) + 1
        self._memory_plans[plan_hash] = normalized
        self._draft_profile_records = []
        input_plan_file = None
        if self.receipt_directory is not None:
            self.receipt_directory.mkdir(parents=True, exist_ok=True)
            input_plan_file = self.receipt_directory / f"trial_{number:03d}_plan.json"
            input_plan_file.write_bytes(plan_bytes)
        try:
            if errors:
                raise ValueError("plan format errors")
            (
                numeric_plan,
                unaligned_numeric_plan,
                field_aliases,
                length_bindings,
                measurement_bindings,
                measurement_profiles,
                numeric_plan_path,
                numeric_plan_sha256,
                unaligned_numeric_plan_path,
                unaligned_numeric_plan_sha256,
                reading_alignment,
                alignment,
            ) = self._numeric_plan(plan, number)
        except (ValueError, TypeError, KeyError) as error:
            def located_findings(check):
                try:
                    return check(normalized)
                except (ValueError, TypeError, KeyError):
                    return []
            receipt = {
                "status": "failed",
                "trial_id": f"trial_{number:03d}",
                "plan_sha256": plan_hash,
                "original_plan_sha256": plan_hash,
                "image": self.image_name,
                "source_geometry_ready": False,
                "candidate": None,
                "draft": None,
                "compiled_plan_sha256": None,
                "input_plan_file": (self._relative_to_workspace(input_plan_file)
                                    if input_plan_file is not None else None),
                "aligned_numeric_input_file": None,
                "aligned_numeric_input_sha256": None,
                "unaligned_numeric_input_file": None,
                "unaligned_numeric_input_sha256": None,
                "compiled_numeric_plan_file": None,
                "compiled_numeric_plan_sha256": None,
                "field_aliases": [],
                "length_bindings": [],
                "measurement_bindings": [],
                "measurement_profiles": copy.deepcopy(self._draft_profile_records),
                "reading_alignment": {
                    "status": "unavailable",
                    "reason": "numeric plan resolution failed before reader alignment",
                },
                "alignment": {
                    "status": "unavailable",
                    "reason": "numeric plan resolution failed before alignment preflight",
                },
                "drawing_differences": {
                    "status": "unavailable",
                    "reason": "numeric plan resolution failed before compilation",
                },
                "building_precision": {
                    "status": "unavailable",
                    "reason": "numeric plan resolution failed before compilation",
                },
                "overlay": None,
                "corridor_review": {
                    "status": "not_assessed",
                    "reason": "numeric plan resolution failed before compilation",
                    "hint": CORRIDOR_REVIEW_HINT,
                },
                "returned_images": [],
                "reason": f"{getattr(error, 'failure_context', {}).get('failure_stage', 'numeric_plan_resolution')}: {error}",
                "repair_hint": plan_error_hint(normalized, str(error)),
                "unhosted_openings": located_findings(unhosted_openings),
                "loose_partition_ends": located_findings(loose_partition_ends),
                "changes": change_report,
                "base_plan_sha256": expected_base,
                "phase": "operations" if previous_plan is not None else "draft",
                "plan_revision": preservation,
                "operations": copy.deepcopy(operations),
                "regularization_failure": copy.deepcopy(getattr(error, "report", None)),
                **(format_failure(errors) if errors else {}),
                **copy.deepcopy(getattr(error, "failure_context", {})),
            }
            if isinstance(error, ReadingAlignmentError):
                receipt["repair_coordinate_basis"] = (
                    "Operations edit the saved original declaration. Failure coordinates refer to failed_stage_input_file; "
                    "compare the named object's original pixels before changing it. Do not copy an adopted coordinate "
                    "into only one endpoint of a shared junction."
                )
            receipt["editable_draft"] = {**declaration_editability(normalized),
                "plan_sha256": plan_hash, "input_plan_file": receipt["input_plan_file"],
                "validation_passed": False}
            if self.receipt_directory is not None:
                path = self.receipt_directory / f"trial_{number:03d}.json"
                receipt["receipt_file"] = self._relative_to_workspace(path)
                path.write_text(
                    json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8", newline="\n",
                )
            self.receipts.append(receipt)
            return receipt
        if numeric_plan.get("reading_type") == "plan":
            from .plan_reading import build_reading
            try:
                raw = build_reading(numeric_plan, workspace=self.workspace, image_name=self.image_name, number=number)
            except (ValueError, TypeError, KeyError) as error:
                raw = {"structuredContent": {"source_geometry_ready": False, "error": str(error)}, "content": []}
        else:
            raw = await self.tools.call_tool(
                "build_plan_bim",
                {"image": self.image_name, "plan_json": _canonical_plan_bytes(numeric_plan).decode("utf-8")},
            )
        try:
            result = _payload(raw)
            if self.workspace is not None and result.get("details_file"):
                from scripts.tool_scripts.bim_agent_saved_result import full_result_metadata
                result = full_result_metadata(raw, self.workspace)
        except ValueError as error:
            result = {"source_geometry_ready": False, "error": str(error)}
        ready = geometry_ready(result)
        plan_input = result.get("plan_input") if isinstance(result.get("plan_input"), Mapping) else {}
        compiled_numeric_plan = None
        compiled_numeric_path = None
        compiled_numeric_sha256 = None
        compiled_product_error = None
        try:
            (compiled_numeric_plan, compiled_numeric_path,
             compiled_numeric_sha256) = self._compiled_numeric_product(
                plan_input=plan_input, aligned_plan=numeric_plan, number=number,
            )
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            compiled_product_error = str(error)
        self._memory_numeric[plan_hash] = copy.deepcopy(compiled_numeric_plan or numeric_plan)
        differences = _verified_report(self.workspace, result.get("drawing_differences", {
            "status": "unavailable", "reason": "build result omitted drawing_differences"
        }))
        precision_path = None
        if self.workspace is not None and isinstance(result.get("candidate"), str):
            candidate_precision = _inside(self.workspace, f"{result['candidate']}/precision_report.json")
            if candidate_precision.is_file():
                precision_path = candidate_precision
        precision = (json.loads(precision_path.read_bytes()) if precision_path is not None
                     else result.get("building_precision", {
                         "status": "unavailable", "reason": "build result omitted building_precision"
                     }))
        receipt: dict[str, Any] = {
            "status": "passed" if ready else "failed",
            "trial_id": f"trial_{number:03d}",
            "plan_sha256": plan_hash,
            "original_plan_sha256": plan_hash,
            "image": self.image_name,
            "source_geometry_ready": ready and result.get("reading_geometry_ready") is not True,
            **({key: result[key] for key in ("reading_geometry_ready", "reading_product_file", "reading_product_sha256",
                                           "wall_metadata_summary") if key in result}
               if result.get("reading_geometry_ready") else {}),
            "candidate": result.get("candidate"),
            "draft": plan_input.get("plan_file"),
            "compiled_plan_sha256": plan_input.get("plan_sha256"),
            "input_plan_file": (self._relative_to_workspace(input_plan_file)
                                if input_plan_file is not None else None),
            "aligned_numeric_input_file": (
                self._relative_to_workspace(numeric_plan_path)
                if numeric_plan_path is not None else None
            ),
            "aligned_numeric_input_sha256": numeric_plan_sha256,
            "unaligned_numeric_input_file": (
                self._relative_to_workspace(unaligned_numeric_plan_path)
                if unaligned_numeric_plan_path is not None else None
            ),
            "unaligned_numeric_input_sha256": unaligned_numeric_plan_sha256,
            "compiled_numeric_plan_file": (
                self._relative_to_workspace(compiled_numeric_path)
                if compiled_numeric_path is not None else None
            ),
            "compiled_numeric_plan_sha256": compiled_numeric_sha256,
            "field_aliases": field_aliases,
            "length_bindings": length_bindings,
            "measurement_bindings": measurement_bindings,
            "measurement_profiles": measurement_profiles,
            "reading_alignment": reading_alignment,
            "alignment": alignment,
            "drawing_differences": differences,
            "building_precision": precision,
            "overlay": result.get("source_plan_views") or plan_input.get("draft_view"),
            "corridor_review": _corridor_review(self.workspace, result, precision, differences),
            "changes": change_report,
            "base_plan_sha256": expected_base,
            "plan_revision": preservation,
            "operations": copy.deepcopy(operations),
        }
        receipt["returned_images"] = self._save_returned_images(raw, number, plan_hash)
        if compiled_product_error is not None:
            ready = False
            receipt.update(
                status="failed",
                source_geometry_ready=False,
                reason=f"trial could not verify Toolkit effective plan: {compiled_product_error}",
            )
        elif ready and receipt["compiled_plan_sha256"] != compiled_numeric_sha256:
            ready = False
            receipt.update(
                status="failed",
                source_geometry_ready=False,
                reason=("trial compiler did not compile the verified effective numeric plan: "
                        f"expected {compiled_numeric_sha256}, got {receipt['compiled_plan_sha256']}"),
            )
        if ready and self.workspace is not None and isinstance(result.get("candidate"), str):
            product_file, product_sha = product_identity(result)
            source = _inside(self.workspace, product_file)
            if not source.is_file():
                ready = False
                receipt.update(status="failed", source_geometry_ready=False,
                               reason="trial claimed success but source_model.json is missing")
            elif result.get("reading_geometry_ready"):
                if _file_sha256(source) != product_sha:
                    ready = False
                    receipt.update(status="failed", reading_geometry_ready=False, reason="reading product hash mismatch")
            else:
                receipt["candidate_source_sha256"] = _file_sha256(source)
        if not ready:
            receipt.setdefault("reason", str(result.get("error") or result.get("status") or
                                             "trial did not produce source geometry"))
            # Toolkit errors refer to the effective aligned/prepared draft. Map
            # every actionable hint back to the reader's normalized declaration.
            receipt["repair_hint"] = plan_error_hint(unaligned_numeric_plan, receipt["reason"])
            if "host" in receipt["reason"]:
                try:
                    unhosted = unhosted_openings(unaligned_numeric_plan)
                except (ValueError, KeyError, TypeError, IndexError):
                    unhosted = []
                if unhosted:
                    receipt["unhosted_openings"] = unhosted
                    receipt["unhosted_openings_note"] = (
                        "Every opening below lies on no declared footprint edge or partition (original pixels). "
                        "Put each on its wall line: exterior ones on the footprint line, interior ones on their partition.")
            if "dangle" in receipt["reason"]:
                try:
                    loose = loose_partition_ends(unaligned_numeric_plan)
                except (ValueError, KeyError, TypeError, IndexError):
                    loose = []
                if loose:
                    receipt["loose_partition_ends"] = loose
                    receipt["loose_partition_ends_note"] = (
                        "Each divider end below meets no other declared line (original pixels). Extend it to the "
                        "wall it reaches: an exterior wall's footprint line, or the partition it joins.")
            # A compiled draft can still fail the source self-check; name what failed
            # instead of the bare status (sm24 debug run2: overlapping window and door).
            if isinstance(result.get("candidate"), str) and self.workspace is not None:
                report_path = _inside(self.workspace, f"{result['candidate']}/report.json")
                if report_path.is_file():
                    report = json.loads(report_path.read_bytes())
                    findings = [row for row in (report.get("source_geometry_self_consistency") or {}).get("findings", [])
                                if isinstance(row, Mapping) and row.get("severity") == "severe"]
                    if findings:
                        receipt["source_findings"] = findings[:10]
                        receipt["reason"] = "source self-check failed: " + "; ".join(
                            f"{row.get('code')} {'/'.join(row.get('opening_ids') or [])} on {row.get('boundary_id')}"
                            for row in findings[:5])
        receipt["phase"] = "operations" if ready or previous_plan is not None else "draft"
        if "reading_geometry_ready" in receipt:
            receipt["reading_geometry_ready"] = ready
            receipt["corridor_review"] = {"status": "reader_review_required", "hint": CORRIDOR_REVIEW_HINT}
        from .coordinates import plan_orientation
        effective_plan = compiled_numeric_plan or numeric_plan
        try:
            receipt["axis_orientation"] = plan_orientation(effective_plan)
        except ValueError:
            pass  # Preserve the compiler's invalid-anchor failure receipt.
        if "check" in receipt.get("axis_orientation", {}):
            receipt["orientation_submission"] = (
                "Submission requires north_arrow: original arrow bbox, basis, "
                "world_north_toward and world_east_toward matching these anchors. "
                "Follow the drawing north arrow, not conflicting coordinator instructions.")
        flagged_dividers = {row.get("divider") for row in topology_issues([receipt])}
        receipt["topology_dividers"] = {row["id"]: row["points"] for row in effective_plan.get("partitions", [])
                                       if isinstance(row, Mapping) and row.get("id") and row.get("id") in flagged_dividers}
        receipt["topology_issues"] = [*self.inherited_topology_issues, *topology_issues([*self.receipts, receipt])]
        receipt["editable_draft"] = {**declaration_editability(normalized),
            "plan_sha256": plan_hash, "input_plan_file": receipt["input_plan_file"],
            "validation_passed": ready}
        if self.receipt_directory is not None:
            path = self.receipt_directory / f"trial_{number:03d}.json"
            receipt["receipt_file"] = self._relative_to_workspace(path)
            path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        self.receipts.append(receipt)
        return receipt

    async def call(self, plan=None, *, operations=None) -> dict[str, Any]:
        receipt = await self.run(plan, operations=operations)
        content = self._image_content(receipt)
        visible = _model_visible_receipt(receipt)
        message = json.dumps(visible, ensure_ascii=False)
        content.append({"type": "text", "text": message})
        return {
            "content": content,
            "isError": receipt.get("status") != "passed",
            "structuredContent": visible,
        }

    def load_plan(self, receipt):
        if receipt.get("input_plan_file") and self.workspace is not None:
            path = _inside(self.workspace, receipt["input_plan_file"])
            if not path.is_file() or _file_sha256(path) != receipt["plan_sha256"]:
                raise ValueError("trial input plan hash mismatch")
            return json.loads(path.read_bytes())
        return copy.deepcopy(self._memory_plans[receipt["plan_sha256"]])

    def numeric_plan(self, receipt):
        if receipt.get("compiled_numeric_plan_file") and self.workspace is not None:
            path = _inside(self.workspace, receipt["compiled_numeric_plan_file"])
            if not path.is_file() or _file_sha256(path) != receipt["compiled_numeric_plan_sha256"]:
                raise ValueError("trial numeric plan hash mismatch")
            return json.loads(path.read_bytes())
        return copy.deepcopy(self._memory_numeric[receipt["plan_sha256"]])

    def verified_plan(self, plan_sha256):
        receipt = self._successful(plan_sha256)
        if receipt is None or receipt.get("status") != "passed":
            raise ValueError("submit_plan_reading requires the plan_sha256 of a successful isolated trial")
        if self.workspace is not None:
            saved = _inside(self.workspace, receipt["receipt_file"])
            if json.loads(saved.read_bytes()) != receipt:
                raise ValueError("trial receipt changed since validation")
            product_file, product_sha = product_identity(receipt)
            source = _inside(self.workspace, product_file)
            if not source.is_file() or _file_sha256(source) != product_sha:
                raise ValueError("successful trial source hash mismatch")
            manifest = json.loads((self.workspace / "inputs.json").read_bytes())
            if _file_sha256(self.workspace / "images" / self.image_name) != manifest["images"][self.image_name]["sha256"]:
                raise ValueError("trial original image changed")
        plan = self.load_plan(receipt)
        if canonical_plan_sha256(plan) != plan_sha256:
            raise ValueError("submitted plan hash mismatch")
        self.numeric_plan(receipt)
        return plan, {"validation_passed": True, **receipt}

    def require_success(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        plan_hash = canonical_plan_sha256(plan)
        receipt = self._successful(plan_hash) or self._existing(plan_hash)
        if receipt is not None and receipt.get("status") == "passed":
            return receipt
        if receipt is not None:
            raise ValueError(
                f"plan {plan_hash} has no successful isolated trial; latest failure: {receipt.get('reason', 'unknown')}"
            )
        raise ValueError(f"plan {plan_hash} has not been run through trial_plan_bim")

    def delivery_receipt(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        """Return the same-plan trial outcome for artifact registration.

        Failed trials remain readable/reworkable artifacts, but the explicit
        ``validation_passed`` flag lets build_from_artifact reject them without
        discarding the reader's plan or concrete compiler reason.
        """

        plan_hash = canonical_plan_sha256(plan)
        receipt = self._successful(plan_hash) or self._existing(plan_hash)
        if receipt is None:
            raise ValueError(f"plan {plan_hash} has not been run through trial_plan_bim")
        return {"validation_passed": receipt.get("status") == "passed", **receipt}

    def durable_snapshot(self) -> dict[str, Any]:
        """Hash stable trial receipts and products; omit clocks and live process state."""

        rows = []
        for receipt in self.receipts:
            row = {
                "plan_sha256": receipt.get("plan_sha256"),
                "status": receipt.get("status"),
                "candidate": receipt.get("candidate"),
                "candidate_source_sha256": receipt.get("candidate_source_sha256"),
                "reading_product_sha256": receipt.get("reading_product_sha256"),
                "compiled_plan_sha256": receipt.get("compiled_plan_sha256"),
                "aligned_numeric_input_sha256": receipt.get("aligned_numeric_input_sha256"),
                "compiled_numeric_plan_sha256": receipt.get("compiled_numeric_plan_sha256"),
                "measurement_profile_sha256": [
                    profile.get("sha256") for profile in receipt.get("measurement_profiles", [])
                ],
                "returned_image_sha256": [image.get("sha256") for image in receipt.get("returned_images", [])],
            }
            if self.workspace is not None and isinstance(receipt.get("receipt_file"), str):
                path = _inside(self.workspace, receipt["receipt_file"])
                row["receipt_sha256"] = _file_sha256(path)
            rows.append(row)
        snapshot = {
            "image": self.image_name,
            "reference_plan_sha256": (canonical_plan_sha256(self.reference_plan)
                                      if self.reference_plan is not None else None),
            "reference_receipt": self.reference_receipt,
            "allowed_rework_targets": self.allowed_rework_targets,
            "receipts": rows,
        }
        canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return {**snapshot, "snapshot_sha256": hashlib.sha256(canonical).hexdigest()}

    def artifacts(self) -> list[Path]:
        """Return the small durable set needed to inspect/recover trial decisions."""

        if self.workspace is None:
            return []
        paths = []
        for fixed in (self.workspace / "inputs.json", self.workspace / "images" / self.image_name):
            if fixed.is_file():
                paths.append(fixed)
        for receipt in self.receipts:
            for key in ("receipt_file", "input_plan_file", "aligned_numeric_input_file",
                        "unaligned_numeric_input_file", "failed_stage_input_file",
                        "compiled_numeric_plan_file"):
                if isinstance(receipt.get(key), str):
                    path = _inside(self.workspace, receipt[key])
                    if path.is_file():
                        paths.append(path)
            candidate = receipt.get("candidate")
            if isinstance(candidate, str):
                for name in ("source_model.json", "reading.json", "precision_report.json"):
                    path = _inside(self.workspace, f"{candidate}/{name}")
                    if path.is_file():
                        paths.append(path)
            for image in receipt.get("returned_images", []):
                if isinstance(image.get("file"), str):
                    path = _inside(self.workspace, image["file"])
                    if path.is_file():
                        paths.append(path)
            for profile in receipt.get("measurement_profiles", []):
                if isinstance(profile, Mapping) and isinstance(profile.get("file"), str):
                    path = _inside(self.workspace, profile["file"])
                    if path.is_file():
                        paths.append(path)
        return sorted(set(paths))

    def image_origins(self, raw_result: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        """Recover persisted lineage for trial images returned to the reader model."""

        if not isinstance(raw_result, Mapping):
            raise TypeError("raw_result must be an MCP envelope")
        structured = raw_result.get("structuredContent")
        if not isinstance(structured, Mapping):
            return {}
        result = {}
        for image in structured.get("returned_images", []):
            if isinstance(image, Mapping) and isinstance(image.get("sha256"), str):
                result[image["sha256"]] = dict(image.get("origin", {}))
        return result


class PlanTrialSession:
    """Prepare one-image writable trial state and hide it behind :class:`PlanTrial`."""

    def __init__(self, reader_run: Path, image_name: str, root: Path = ROOT,
                 *, grid_step_m: float = DEFAULT_LITE_GRID_M):
        self.reader_run = Path(reader_run).resolve()
        self.image_name = image_name
        self.root = Path(root).resolve()
        self.grid_step_m = grid_step_m
        self.workspace = self.reader_run / "trial_workspace"
        self._client_context = None
        self._client = None
        self.trial: PlanTrial | None = None

    def _prepare(self) -> None:
        manifest_path = self.reader_run / "inputs.json"
        if not manifest_path.is_file():
            raise ValueError("reader run has no inputs.json")
        parent = json.loads(manifest_path.read_bytes())
        images = parent.get("images", {})
        if self.image_name not in images or len(images) != 1:
            raise ValueError("plan trial requires the reader's one admitted original image")
        source = self.reader_run / "images" / self.image_name
        expected = images[self.image_name].get("sha256")
        if not source.is_file() or _file_sha256(source) != expected:
            raise ValueError("reader original image is missing or changed")
        (self.workspace / "images").mkdir(parents=True, exist_ok=True)
        target = self.workspace / "images" / self.image_name
        if target.is_file() and _file_sha256(target) != expected:
            raise ValueError("trial original image changed on resume")
        if not target.is_file():
            shutil.copyfile(source, target)
        trial_manifest = {
            "images": {self.image_name: images[self.image_name]},
            "image_kind": parent.get("image_kind", "drawings"),
            "input_mode": "role_plan_trial",
            "scope": "one plan-reader image; no parent building draft",
            "floor_plan_images": [self.image_name],
            "floor_scope_source": "plan_reader_single_image",
            "max_candidates": 24,
        }
        for key in ("started_epoch", "deadline_epoch", "time_budget_seconds"):
            if key in parent:
                trial_manifest[key] = parent[key]
        target_manifest = self.workspace / "inputs.json"
        if target_manifest.is_file() and json.loads(target_manifest.read_bytes()) != trial_manifest:
            raise ValueError("trial inputs or timing changed on resume")
        if not target_manifest.is_file():
            target_manifest.write_text(
                json.dumps(trial_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
            )

    async def __aenter__(self) -> PlanTrial:
        self._prepare()
        self.trial = PlanTrial(
            self, image_name=self.image_name,
            receipt_directory=self.workspace / "trial_receipts", workspace=self.workspace,
            profile_directory=self.reader_run / "pixel_profiles", grid_step_m=self.grid_step_m)
        return self.trial

    async def call_tool(self, name, arguments):
        # The current 2D reader never needs a second writable BIM server.
        # Open the legacy preview adapter only if a recorded 3D draft uses it.
        if self._client is None:
            from src.agent.runtime_tools import FrozenBimTools, coordinator_role, frozen_bim_client
            from src.harness_contracts.budget import BudgetAmounts
            self._client_context = frozen_bim_client(self.workspace, readonly=False, repository_root=self.root)
            self._client = await self._client_context.__aenter__()
            self._legacy_tools = FrozenBimTools(self._client, coordinator_role(BudgetAmounts(calls=64)),
                                               run_directory=self.workspace)
        return await self._legacy_tools.call_tool(name, arguments)

    def image_origins(self, raw):
        return self._legacy_tools.image_origins(raw) if self._client is not None else {}

    async def __aexit__(self, exc_type, exc, traceback):
        if self._client_context is not None:
            return await self._client_context.__aexit__(exc_type, exc, traceback)
        return False
