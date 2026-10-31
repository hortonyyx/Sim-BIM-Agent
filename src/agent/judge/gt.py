"""Ground-truth (evaluation answer) loader — judge② side ONLY.

Per-case bundle `gt/<case>/` under `case_tests/test_baseline/gt/` holds all gt
content for a case together: `gt.json` (the EVALUATION answer key — true
zonification / per-facade window counts / dimension truth), the `source.dxf` it
was derived from, and `renders/`. The answer key is read ONLY by the gate② judge
(the main Agent's judging path). It must NEVER be read by:
  - gate① deterministic checks (`src/validator/checks/*`) — they ship to
    production, which has no answer key; depending on gt would make dev and prod
    behave differently;
  - stage executors (`src/agent/legacy/pipeline.py` run_correction / run_mep) — feeding
    the answer would collapse the error budget.

`tests/test_gt_discipline.py` mechanically enforces that those modules do not
import this one. See `case_tests/test_baseline/gt/README.md`.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from .gt_schema import (GtDocument, GtValidationError, GtValidationIssue,
                        GroundTruthV3, LegacyGroundTruthV2,
                        REPO_ROOT, StableId, validate_gt_v3, validate_legacy_v2)

DEFAULT_GT_DIR = REPO_ROOT / "case_tests/test_baseline/gt"


def gt_path(case: str, *, gt_dir: Path | str = DEFAULT_GT_DIR) -> Path:
    _validate_case(case)
    root = Path(gt_dir).resolve()
    path = (root / case / "gt.json").resolve()
    if not path.is_relative_to(root):
        _fail("gt_path_traversal", "/case")
    return path


def case_gt_dir(case: str, *, gt_dir: Path | str = DEFAULT_GT_DIR) -> Path:
    """The per-case gt bundle dir holding gt.json + renders/ (source DXFs live in gt_sources/<case>/)."""
    _validate_case(case)
    root = Path(gt_dir).resolve()
    path = (root / case).resolve()
    if not path.is_relative_to(root):
        _fail("gt_path_traversal", "/case")
    return path


def has_gt(case: str, *, gt_dir: Path | str = DEFAULT_GT_DIR) -> bool:
    return gt_path(case, gt_dir=gt_dir).is_file()


def load_gt(case: str, *, gt_dir: Path | str = DEFAULT_GT_DIR) -> dict | None:
    """Load the evaluation ground truth for a case, or None if absent.

    A judge with no gt simply judges against the original drawings + testdata
    (more subjective); gt makes the call objective. Returns the parsed dict."""
    p = gt_path(case, gt_dir=gt_dir)
    if not p.is_file():
        return None
    raw = _read_raw(p)
    document = _parse_document(raw, allow_legacy=True)
    if isinstance(document, GroundTruthV3):
        _fail("gt_v3_requires_typed_consumer", "/schema_version")
    validate_legacy_v2(document, expected_case=case)
    # Compatibility deliberately returns the original decoder output, never a
    # Pydantic dump: callers retain aliases, list order and numeric categories.
    return raw


def load_gt_file(path: Path | str, *, allow_legacy: bool = True) -> GtDocument:
    """Read an explicit v2/v3 GT file through L0/L1/L2, without baseline policy."""
    raw = _read_raw(Path(path))
    document = _parse_document(raw, allow_legacy=allow_legacy)
    if isinstance(document, GroundTruthV3):
        validate_gt_v3(document, tolerances=document.generator.tolerances, expected_case=None)
    else:
        validate_legacy_v2(document)
    return document


def load_gt_document(case: str, *, gt_dir: Path | str = DEFAULT_GT_DIR) -> GtDocument | None:
    """Typed case loader: L0/L1/L2 followed by verified-baseline policy."""
    path = gt_path(case, gt_dir=gt_dir)
    if not path.is_file():
        return None
    raw = _read_raw(path)
    document = _parse_document(raw, allow_legacy=True)
    if isinstance(document, GroundTruthV3):
        validate_gt_v3(document, tolerances=document.generator.tolerances, expected_case=case)
        # A case-based root is never a candidate entrance, including a custom
        # root. Candidates are intentionally available only through file API.
        if document.verification.status != "human_verified":
            _fail("gt_default_root_candidate_forbidden", "/verification/status")
    else:
        validate_legacy_v2(document, expected_case=case)
    return document


def _validate_case(case: str) -> None:
    try:
        # StableId's strict contract also rejects path separators and whitespace.
        from pydantic import TypeAdapter
        TypeAdapter(StableId).validate_python(case)
    except ValidationError as exc:
        raise GtValidationError([GtValidationIssue("gt_path_invalid_case", "/case", {})]) from exc


def _read_raw(path: Path) -> dict:
    try:
        value = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GtValidationError([GtValidationIssue("gt_wire_decode_failed", "", {})]) from exc
    if not isinstance(value, dict):
        _fail("gt_wire_not_object", "")
    return value


def _parse_document(raw: dict, *, allow_legacy: bool) -> GtDocument:
    version = raw.get("schema_version")
    # bool is an int subclass: explicitly exclude it before integer dispatch.
    if isinstance(version, bool) or not isinstance(version, int):
        _fail("gt_wire_unsupported_legacy_version", "/schema_version")
    try:
        if version == 2:
            if not allow_legacy:
                _fail("gt_wire_legacy_forbidden", "/schema_version")
            return LegacyGroundTruthV2.model_validate(raw)
        if version == 3:
            return GroundTruthV3.model_validate(raw)
    except ValidationError as exc:
        pointer = "/" + "/".join(str(part) for part in exc.errors()[0]["loc"])
        raise GtValidationError([GtValidationIssue("gt_wire_invalid", pointer, {})]) from exc
    _fail("gt_wire_unsupported_legacy_version", "/schema_version")


def _fail(code: str, pointer: str) -> None:
    raise GtValidationError([GtValidationIssue(code, pointer, {})])
