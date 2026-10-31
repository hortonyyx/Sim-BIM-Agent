"""Clean-room workspace support for the optional legacy reading workflow."""

from __future__ import annotations

from src.utils import file_lock
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from src.agent.legacy.resources import READING_RESOURCE_RELATIVE

from src.agent.execution.manifest import (
    RunManifestV2,
    StageRecordV2,
    ensure_run_manifest_v2,
    hash_file,
    hash_text,
    load_run_manifest,
    reading_attempt_allowed,
    save_run_manifest,
)
from src.agent.execution.view_manifest import (
    ViewManifest,
    ReadingExamScope,
    build_view_manifest,
    derive_input_inventory,
    verify_view_manifest,
)
from src.agent.execution.run_policy_freeze import resolve_frozen_run_policy
from src.agent.execution.vision_resize import (
    DEFAULT_VISION_RESIZE_TIER,
    resize_image_file_to_tier,
)
from src.validator.checks.reading import check_calibration_evidence
from src.validator.checks.schema import CheckLayer
from src.validator.checks.view_manifest import check_reading_stage

ISOLATION_SCHEMA_VERSION = "1"
PILOT_REVIEW_STATE_SCHEMA_VERSION = "1"
READER_INVOCATION_SCHEMA_VERSION = "1"
READING_EXPERIMENT_SCHEMA_VERSION = "1"
STAGE = "0_reading"

# F-55: named to match guard.py's own WRITE_ALLOWED_DIRS (the reader's real,
# sanctioned write surface) rather than adding a second, differently-spelled
# copy of the same two directory names — this file did not previously have a
# shared constant for them (the mkdir calls below used to spell "out"/
# "requests" as bare literals); `_lock_down_readonly_surface` is the first
# consumer here that needs the pair as a set, not one at a time.
WRITE_ALLOWED_DIRS = ("out", "requests")

# The worked-example reading-view JSON the kickoff tells the reader to read as a
# style/format anchor (session_kickoff.md §"First"). Keep it with the isolation
# templates, not under case_tests: the former smalloffice_20 example had exactly
# the same 15 m x 8 m / x={5,10} / y={3,5} wall grid as sm21 and therefore leaked
# an answer-shaped prior into that benchmark. This synthetic example deliberately
# uses a different envelope and asymmetric wall grid. Build stages it at a stable
# non-denied path and rewrites the kickoff pointer to that path (F-2).
WORKED_EXAMPLE_SOURCE = "src/agent/execution/isolation_templates/worked_example_plan.json"
WORKED_EXAMPLE_STAGED = "reference/worked_example_plan.json"

HARD_BLOCK_FILENAMES = {
    "gt" + ".json",
    "judge.json",
    "judge_rubric.md",
}
HARD_BLOCK_PARTS = {
    "attempts",
    "gt",
}
SEMANTIC_WARNING_TOKENS = (
    "judge",
    "verdict",
    "grade",
    "score",
    "attempt",
)

FEEDBACK_FORBIDDEN_SUBSTRINGS = (
    Path(__file__).resolve().parents[3].as_posix().lower(),
    "gt" + ".json",
    "test_baseline",
    "case_tests",
    "attempts/",
    "judge.json",
    "judge_rubric.md",
    "verdict",
)

# Concrete judge artifacts produced by run_stage/report assembly and named in
# judge packets/score sidecars. Keep these shapes narrow: ``grade line`` and
# other architectural prose are reader input, while these names expose
# controller-only judgment material.
FEEDBACK_FORBIDDEN_PATTERNS = (
    (
        "grade artifact filename",
        re.compile(r"(?<![a-z0-9_])grade\.(?:png|json)(?![a-z0-9_])"),
    ),
    ("grade path segment", re.compile(r"(?:^|[/\\])grade(?:[/\\]|$)")),
    ("stage grade artifact", re.compile(r"[a-z0-9]+_grade\.(?:png|json)(?![a-z0-9_])")),
    ("grade artifact field", re.compile(r'''["']grade["']\s*:''')),
    (
        "grade sidecar field",
        re.compile(r"(?<![a-z0-9_])grade_(?:kind|png_sha256|renderer)(?![a-z0-9_])"),
    ),
    (
        "report grade field",
        re.compile(r"(?<![a-z0-9_])(?:reading|correction)_grade(?![a-z0-9_])"),
    ),
)


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    source_path: str
    category: str
    sha256: str


@dataclass
class WorkspaceManifest:
    staging_root: Path
    case_dir: Path
    run_dir: Path | None
    files: list[ManifestEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # §2 reader-visibility ledger: images that were classified `excluded_input`
    # by the view manifest and therefore deliberately NOT copied into staging
    # (not a warning — this is the expected, audited shape of a clean build).
    excluded_from_staging: list[dict] = field(default_factory=list)
    merge_eligible: bool = False

    @property
    def manifest_path(self) -> Path:
        return self.staging_root / "MANIFEST.json"

    @property
    def settings_path(self) -> Path:
        return self.staging_root / "isolation_settings.json"

    def to_json_obj(self) -> dict:
        return {
            "schema_version": ISOLATION_SCHEMA_VERSION,
            "staging_root": str(self.staging_root),
            "case_dir": _repo_relative(self.case_dir),
            "run_dir": _repo_relative(self.run_dir) if self.run_dir else None,
            "merge_eligible": self.merge_eligible,
            "files": [entry.__dict__ for entry in sorted(self.files, key=lambda item: item.path)],
            "excluded_from_staging": sorted(
                self.excluded_from_staging, key=lambda item: item["input_id"]
            ),
            "warnings": sorted(set(self.warnings)),
        }

    def save(self) -> Path:
        self.manifest_path.write_text(
            json.dumps(self.to_json_obj(), indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
        return self.manifest_path


def build_isolation_workspace(
    case_dir: Path,
    run_dir: Path | None = None,
    staging_root: Path | None = None,
    vision_resize_tier: str | None = None,
    *,
    pilot_review_gate: bool = True,
    guard_profile: str = "observe",
) -> WorkspaceManifest:
    """Build a clean-room staging tree for an isolated 0_reading executor.

    ``vision_resize_tier`` (F-51, second cut): before a ``case_data/`` PNG is
    handed to the reader, resize it in place to the given
    :mod:`src.agent.execution.vision_resize` tier (default
    :data:`~src.agent.execution.vision_resize.DEFAULT_VISION_RESIZE_TIER`,
    i.e. "standard" — matches Haiku 4.5 and the other models this project
    currently runs). This is what makes the frame the model sees, the frame
    on disk in staging, and the frame ``cv_probe`` reads the SAME frame: the
    Claude vision API downsamples an oversized image before the model ever
    sees it, while ``cv_probe`` reads the staged file directly — mismatched,
    those two frames disagree in a way that is self-consistent (so it does
    not trip the cross-axis calibration check) yet globally wrong. Resizing
    the staged copy to the size the API would have produced anyway (per
    Anthropic's own documented recommendation) removes the mismatch instead
    of only making it detectable. An image already within the tier round-trips
    unchanged (verified byte-for-byte identical). Never touches the repo's
    original case image — only the staged copy under ``case_data/``.

    Two modes (§5.2):

    - **preview/unbound** (``run_dir=None``): no run to bind to yet. The build
      always produces ``merge_eligible: false`` — :func:`merge_isolated_output`
      refuses unconditionally for a workspace built this way, no matter what
      target run_dir is later supplied.
    - **formal** (``run_dir`` given): the caller must have already provisioned
      this run's view manifest (``provision_view_manifest`` / the
      ``provision`` CLI) — this function only *verifies* it and refuses to
      build otherwise (fail closed, never silently provisions here). On
      success it binds/creates the run's v2 identity (:func:`ensure_run_manifest_v2`)
      and records immutable provenance (run_id, case_id, view_manifest_sha256,
      case_metadata_sha256, per-image hashes) that :func:`merge_isolated_output`
      re-checks before ever accepting.

    In both modes, only ``required_view``-classified images are copied into
    ``case_data/`` — an image the view manifest classifies ``excluded_input``
    is never reader-visible (§2); a derived ``input_inventory.json`` projection
    (denominator-free: no negative-evidence/completeness content) tells the
    reader what to read and what to name its output.
    """
    case_dir = Path(case_dir).resolve()
    run_dir = Path(run_dir).resolve() if run_dir else None
    if staging_root is None:
        temporary_root = Path(tempfile.gettempdir()) / "ep_isolation"
        temporary_root.mkdir(parents=True, exist_ok=True)
        staging_root = Path(tempfile.mkdtemp(prefix=f"{case_dir.name}_", dir=temporary_root))
    else:
        staging_root = Path(staging_root).resolve()
        staging_root.mkdir(parents=True, exist_ok=True)
    _require_outside_repo(staging_root)
    # F-55: create the audit log's real home before the reader's first command
    # can possibly run — guard.py's `_append_log` also mkdir's this
    # defensively, but doing it here means the directory exists from the very
    # start of the build, not only after the first hook invocation.
    _audit_dir(staging_root).mkdir(parents=True, exist_ok=True)

    view_manifest = build_view_manifest(case_dir)
    binding: dict = {
        "merge_eligible": False,
        "pilot_review_gate": bool(pilot_review_gate),
    }

    if run_dir is not None:
        verification = verify_view_manifest(case_dir, run_dir)
        if not verification.ok:
            raise ValueError(
                "cannot build a formal (run-bound) isolation workspace: "
                f"{verification.reason} — provision this run's view manifest first "
                "(`provision_view_manifest` / the `provision` CLI)"
            )
        view_manifest = verification.on_disk  # the committed one is authoritative
        run_manifest_v2 = ensure_run_manifest_v2(
            run_dir, view_manifest_sha256=view_manifest.content_sha256
        )
        # S-2 (G-3): bind the frozen effective run policy so merge can re-verify
        # it did not drift between build and merge, then feed the SAME typed
        # profiles to check_reading_stage (no more silent rectangular/exploratory
        # defaults on the hard-isolation path).
        policy_record = resolve_frozen_run_policy(run_dir)
        binding = {
            "merge_eligible": True,
            "pilot_review_gate": bool(pilot_review_gate),
            "run_id": run_manifest_v2.run_id,
            "case_id": view_manifest.case_id,
            "case_dir": _repo_relative(case_dir),
            "run_dir": _repo_relative(run_dir),
            "view_manifest_sha256": view_manifest.content_sha256,
            "case_metadata_sha256": view_manifest.case_metadata_sha256,
            "image_sha256": {e.input_id: e.image_sha256 for e in view_manifest.required_entries()},
            "run_policy_sha256": policy_record.policy_hash,
            "run_policy_run_profile": policy_record.run_profile,
            "run_policy_capability_profile": policy_record.capability_profile,
            "run_policy_legacy_defaulted": policy_record.legacy_defaulted,
        }
        if verification.exam_scope is not None:
            binding.update(
                {
                    "reading_exam_scope_sha256": verification.exam_scope.content_sha256,
                    "reading_exam_scope_input_ids": verification.exam_scope.input_ids,
                }
            )

    manifest = WorkspaceManifest(
        staging_root=staging_root, case_dir=case_dir, run_dir=run_dir,
        merge_eligible=bool(binding["merge_eligible"]),
    )
    (staging_root / "out").mkdir(parents=True, exist_ok=True)
    (staging_root / "tools").mkdir(parents=True, exist_ok=True)
    # S2a: dedicated dir for CV-probe request JSONs — the only other place (with
    # out/) the guard lets a write tool land. Pre-created so the kickoff's
    # instruction to write here is immediately actionable.
    (staging_root / "requests").mkdir(parents=True, exist_ok=True)

    scope = verification.exam_scope if run_dir is not None else None
    resolved_vision_tier = vision_resize_tier or DEFAULT_VISION_RESIZE_TIER
    _copy_case_data(case_dir, staging_root, manifest, view_manifest, scope, resolved_vision_tier)
    _copy_reading_guidance(staging_root, manifest)
    _copy_worked_example(staging_root, manifest)
    _copy_cv_toolbox(staging_root, manifest)
    _write_kickoff(case_dir, staging_root, manifest, pilot_review_gate=pilot_review_gate)
    _write_guard_profile(staging_root, guard_profile)
    _write_guard_and_wrappers(staging_root, manifest)
    _write_settings(staging_root, manifest)
    _write_input_inventory(staging_root, manifest, view_manifest, scope)
    _write_binding(staging_root, manifest, binding)
    _write_pilot_review_state(staging_root, enabled=pilot_review_gate)
    manifest.save()
    _assert_manifest_clean(manifest)
    # F-55: the read-only lockdown is the LAST step — everything above still
    # needs to write into staging_root while the tree is being assembled.
    _lock_down_readonly_surface(staging_root)
    return manifest


def prepare_single_plan_experiment(
    case_dir: Path,
    run_dir: Path,
    staging_root: Path,
    *,
    input_id: str,
    model: str,
    capability_profile: str = "orthogonal_polygon",
    guard_profile: str = "observe",
    vision_resize_tier: str | None = None,
) -> dict:
    """Freeze and build a new one-plan, same-session recovery experiment.

    This is deliberately a new-run-only entry point. Reusing an existing run or
    staging tree would make the nominally controlled draw inherit unknown
    outputs, policy state, or session metadata. The generated run config pins
    the sole image, exploratory gate profile, and exact reader model; the audit
    sibling spec then binds those bytes to the built staging inputs.
    """
    case_dir = Path(case_dir).resolve()
    run_dir = Path(run_dir).resolve()
    staging_root = Path(staging_root).resolve()
    if not model or not model.strip():
        raise ValueError("single-plan experiment requires an explicit reader model")
    if run_dir.exists():
        raise ValueError(f"single-plan experiment requires a new run directory: {run_dir}")
    if staging_root.exists() or _audit_dir(staging_root).exists():
        raise ValueError(
            "single-plan experiment requires a new staging root and audit sibling"
        )
    _require_outside_repo(staging_root)

    base_manifest = build_view_manifest(case_dir)
    matches = [entry for entry in base_manifest.required_entries() if entry.input_id == input_id]
    if len(matches) != 1:
        raise ValueError(f"input_id is not exactly one required view: {input_id!r}")
    if matches[0].view_type != "plan":
        raise ValueError("single-plan experiment input_id must select a plan view")
    if capability_profile not in {"rectangular", "orthogonal_polygon"}:
        raise ValueError(f"unsupported capability_profile: {capability_profile!r}")

    run_dir.mkdir(parents=True)
    run_config = run_dir / "run_config.yaml"
    config_text = (
        "run_profile: exploratory\n"
        f"capability_profile: {capability_profile}\n"
        "scope:\n"
        "  stages: [0_reading]\n"
        "judge:\n"
        "  mode: stop\n"
        "review:\n"
        "  reading: false\n"
        "  correction: false\n"
        "  geometry: false\n"
        "models:\n"
        "  reading:\n"
        "    role: 0_reading\n"
        f"    model_id: {json.dumps(model, ensure_ascii=False)}\n"
        "    provider: anthropic\n"
        "    effort: default\n"
        "    source: controlled single-plan same-session isolation experiment\n"
        "reading_exam_scope:\n"
        f"  input_ids: [{json.dumps(input_id, ensure_ascii=False)}]\n"
        "  reason: controlled reading-recovery single-plan reproduction\n"
    )
    run_config.write_text(config_text, encoding="utf-8")

    from src.agent.execution.run_provision import provision_run

    frozen_manifest = provision_run(
        case_dir,
        run_dir,
        run_profile="exploratory",
        capability_profile=capability_profile,
        context={"experiment_kind": "single_plan_same_session", "input_id": input_id},
        source="structured_config",
    )
    ensure_run_manifest_v2(run_dir, view_manifest_sha256=frozen_manifest.content_sha256)
    workspace = build_isolation_workspace(
        case_dir,
        run_dir=run_dir,
        staging_root=staging_root,
        pilot_review_gate=True,
        guard_profile=guard_profile,
        vision_resize_tier=vision_resize_tier,
    )
    directive_path = staging_root / "directive.md"
    directive_path.write_text(
        _CONTROLLED_MEASURE_BEFORE_DRAW_DIRECTIVE, encoding="utf-8"
    )

    spec = {
        "schema_version": READING_EXPERIMENT_SCHEMA_VERSION,
        "experiment_kind": "single_plan_same_session",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "case_dir": str(case_dir),
        "run_dir": str(run_dir),
        "staging_root": str(staging_root),
        "input_id": input_id,
        "expected_output_id": matches[0].expected_output_id,
        "model": model,
        "session_policy": "start_then_resume_same_session",
        "run_profile": "exploratory",
        "capability_profile": capability_profile,
        "guard_profile": guard_profile,
        "run_config_sha256": hash_file(run_config),
        "manifest_sha256": hash_file(workspace.manifest_path),
        "binding_sha256": hash_file(staging_root / "binding.json"),
        "input_inventory_sha256": hash_file(staging_root / "input_inventory.json"),
        "directive_sha256": hash_file(directive_path),
    }
    spec["content_sha256"] = _reading_experiment_content_hash(spec)
    spec_path = reading_experiment_spec_path(staging_root)
    spec_path.write_text(
        json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return {
        "run_dir": str(run_dir),
        "staging_root": str(staging_root),
        "experiment_spec": str(spec_path),
        "model": model,
        "input_id": input_id,
    }


def check_feedback_text(text: str) -> None:
    lowered = text.replace("\\", "/").lower()
    for token in FEEDBACK_FORBIDDEN_SUBSTRINGS:
        if token in lowered:
            raise ValueError(f"feedback contains forbidden token: {token}")
    for label, pattern in FEEDBACK_FORBIDDEN_PATTERNS:
        if pattern.search(lowered):
            raise ValueError(f"feedback contains forbidden pattern: {label}")


def pilot_review_state_path(staging_root: Path) -> Path:
    """Operator-owned pilot-review state, outside the reader's write surface."""
    return _audit_dir(Path(staging_root).resolve()) / "pilot_review_state.json"


def _read_pilot_review_state(
    staging_root: Path, *, required: bool = False
) -> dict | None:
    path = pilot_review_state_path(staging_root)
    if not path.is_file():
        if required:
            raise ValueError(
                "pilot review state is missing; this workspace predates the "
                "machine review gate and cannot be merged safely"
            )
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"pilot review state is unreadable: {path}") from exc
    if state.get("schema_version") != PILOT_REVIEW_STATE_SCHEMA_VERSION:
        raise ValueError("pilot review state has an unsupported schema_version")
    if state.get("phase") not in {"disabled", "awaiting_pilot", "feedback_issued", "approved"}:
        raise ValueError("pilot review state has an invalid phase")
    if not isinstance(state.get("enabled"), bool):
        raise ValueError("pilot review state has an invalid enabled flag")
    return state


def _save_pilot_review_state(staging_root: Path, state: dict) -> Path:
    path = pilot_review_state_path(staging_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def _write_pilot_review_state(staging_root: Path, *, enabled: bool) -> Path:
    return _save_pilot_review_state(
        staging_root,
        {
            "schema_version": PILOT_REVIEW_STATE_SCHEMA_VERSION,
            "enabled": bool(enabled),
            "phase": "awaiting_pilot" if enabled else "disabled",
            "feedback_round": 0,
            "feedback_sha256": None,
            "approved_pilot_path": None,
            "approved_pilot_sha256": None,
            "approved_at": None,
        },
    )


def write_feedback(staging_root: Path, text: str, *, name: str = "feedback.md") -> Path:
    check_feedback_text(text)
    staging_root = Path(staging_root).resolve()
    path = staging_root / name
    if path.name != name:
        raise ValueError(f"feedback name must be a filename: {name!r}")
    state = _read_pilot_review_state(staging_root)
    path.write_text(text, encoding="utf-8")
    if name == "feedback.md" and state is not None and state["enabled"]:
        state.update(
            {
                "phase": "feedback_issued",
                "feedback_round": int(state.get("feedback_round", 0)) + 1,
                "feedback_sha256": hash_file(path),
                "approved_pilot_path": None,
                "approved_pilot_sha256": None,
                "approved_at": None,
            }
        )
        _save_pilot_review_state(staging_root, state)
    return path


def approve_pilot(staging_root: Path) -> Path:
    """Approve exactly one completed plan pilot and bind its current bytes.

    Approval is intentionally an operator action. The state lives beside the
    audit log, not under ``out/``, so the reader cannot approve itself. Refuse
    when more than one expected output already exists: that means batching
    started before the review point and the run no longer exercises this gate.
    """
    staging_root = Path(staging_root).resolve()
    state = _read_pilot_review_state(staging_root, required=True)
    assert state is not None
    if not state["enabled"]:
        raise ValueError("pilot approval is disabled for this control-arm workspace")

    premature_batch_artifacts = [
        path
        for path in (
            staging_root / "out" / "output.json",
            staging_root / "out" / "reading_summary.md",
        )
        if path.exists()
    ]
    if premature_batch_artifacts:
        raise ValueError(
            "cannot approve pilot: batch/summary artifact exists before external "
            "approval: "
            + ", ".join(path.name for path in premature_batch_artifacts)
        )

    inventory_path = staging_root / "input_inventory.json"
    try:
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError("cannot approve pilot: input_inventory.json is unreadable") from exc
    expected = {
        entry["expected_output_id"]: entry.get("view_type")
        for entry in inventory
        if entry.get("expected_output_id")
    }
    present = [
        staging_root / "out" / f"{output_id}.json"
        for output_id in expected
        if (staging_root / "out" / f"{output_id}.json").is_file()
    ]
    if len(present) != 1:
        raise ValueError(
            "cannot approve pilot: expected exactly one completed expected-output "
            f"JSON before batch, found {len(present)}"
        )
    pilot = present[0]
    if expected[pilot.stem] != "plan":
        raise ValueError("cannot approve pilot: the sole completed output is not a plan")
    try:
        payload = json.loads(pilot.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("cannot approve pilot: pilot output is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("cannot approve pilot: pilot output must be a JSON object")

    feedback = staging_root / "feedback.md"
    state.update(
        {
            "phase": "approved",
            "feedback_sha256": hash_file(feedback) if feedback.is_file() else None,
            "approved_pilot_path": pilot.relative_to(staging_root).as_posix(),
            "approved_pilot_sha256": hash_file(pilot),
            "approved_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    return _save_pilot_review_state(staging_root, state)


def _verify_pilot_review_for_merge(staging_root: Path, binding: dict) -> dict:
    state = _read_pilot_review_state(staging_root, required=True)
    assert state is not None
    expected_enabled = bool(binding.get("pilot_review_gate"))
    if state["enabled"] != expected_enabled:
        raise ValueError("merge refused: pilot review state disagrees with the bound run mode")
    if not expected_enabled:
        if state["phase"] != "disabled":
            raise ValueError("merge refused: disabled pilot gate has a non-disabled state")
        return state
    if state["phase"] != "approved":
        raise ValueError(
            "merge refused: pilot revision has not received external approval "
            f"(phase={state['phase']})"
        )
    rel = state.get("approved_pilot_path")
    approved_hash = state.get("approved_pilot_sha256")
    if not rel or not approved_hash:
        raise ValueError("merge refused: pilot approval is missing its artifact binding")
    pilot = staging_root / rel
    _require_under(pilot, staging_root / "out")
    if not pilot.is_file() or hash_file(pilot) != approved_hash:
        raise ValueError("merge refused: approved pilot changed after external review")
    feedback = staging_root / "feedback.md"
    expected_feedback_hash = state.get("feedback_sha256")
    if expected_feedback_hash is not None and (
        not feedback.is_file() or hash_file(feedback) != expected_feedback_hash
    ):
        raise ValueError("merge refused: feedback changed after pilot approval")
    return state


def _load_reader_invocations(staging_root: Path) -> list[dict]:
    path = reader_invocations_path(staging_root)
    if not path.is_file():
        return []
    records: list[dict] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"reader invocation ledger is corrupt at line {line_number}"
            ) from exc
        if record.get("schema_version") != READER_INVOCATION_SCHEMA_VERSION:
            raise ValueError("reader invocation ledger has an unsupported schema_version")
        records.append(record)
    return records


def _verify_controlled_experiment_launches(
    staging_root: Path, spec: dict, pilot_review_state: dict
) -> None:
    records = _load_reader_invocations(staging_root)
    if not records:
        raise ValueError("merge refused: controlled experiment has no reader invocation record")
    if any(record.get("requested_model") != spec["model"] for record in records):
        raise ValueError("merge refused: controlled experiment reader model drifted")

    successful = [record for record in records if record.get("returncode") == 0]
    if not successful:
        raise ValueError("merge refused: controlled experiment has no successful reader launch")
    first = successful[0]
    session_id = first.get("reported_session_id")
    stable_runtime = (
        first.get("runner_path"),
        first.get("runner_version"),
        first.get("launcher_module_sha256"),
    )
    if first.get("runner_version_returncode") != 0 or not all(stable_runtime):
        raise ValueError(
            "merge refused: controlled experiment did not resolve its reader runtime"
        )
    if first.get("session_form") != "start" or not session_id:
        raise ValueError(
            "merge refused: controlled experiment did not record a session-start id"
        )
    for record in successful:
        inputs = record.get("input_hashes") or {}
        if (
            inputs.get("manifest_sha256") != spec["manifest_sha256"]
            or inputs.get("binding_sha256") != spec["binding_sha256"]
            or inputs.get("input_inventory_sha256") != spec["input_inventory_sha256"]
            or inputs.get("directive_sha256") != spec["directive_sha256"]
            or inputs.get("experiment_spec_sha256")
            != hash_file(reading_experiment_spec_path(staging_root))
        ):
            raise ValueError(
                "merge refused: controlled experiment launch inputs drifted"
            )
        if (
            record.get("runner_path"),
            record.get("runner_version"),
            record.get("launcher_module_sha256"),
        ) != stable_runtime:
            raise ValueError(
                "merge refused: controlled experiment reader runtime drifted"
            )
    for record in successful[1:]:
        if record.get("session_form") != "resume" or record.get("session_id") != session_id:
            raise ValueError(
                "merge refused: controlled experiment did not preserve one reader session"
            )
    required_successes = 1 + int(pilot_review_state.get("feedback_round", 0))
    if len(successful) < required_successes:
        raise ValueError(
            "merge refused: controlled experiment has fewer successful reader turns "
            "than its review rounds require"
        )


def merge_isolated_output(
    staging_root: Path,
    run_dir: Path,
    *,
    output_path: Path | None = None,
    accept: bool = True,
) -> Path:
    """Merge isolated output into ``<run>/0_reading/attempts`` with provenance.

    §5.2 "merge 同门": before ever touching the manifest, this (1) refuses a
    workspace that was not built ``merge_eligible`` (preview/unbound, or built
    for a different run_dir); (2) refuses a target run whose *current* identity
    (run_id / view_manifest_sha256 / case_metadata_sha256) no longer matches
    what was bound at build time — covers a tampered image, a directly-edited
    view manifest, a run manifest replaced/swapped, or a workspace built for
    run A merged into run B; (3) refuses a v1 (grandfathered) target run
    outright; (4) runs the identical coverage/schema checker the flat-flow
    reader uses against the aggregate payload — ``report.blocking()`` non-empty
    means ``accept=True`` is silently downgraded to a filed-but-not-accepted
    attempt (the caller's ``accept=True`` can never override a blocking gate①).
    """
    staging_root = Path(staging_root).resolve()
    run_dir = Path(run_dir).resolve()
    output_path = Path(output_path) if output_path else staging_root / "out" / "output.json"
    if not output_path.is_absolute():
        output_path = staging_root / output_path
    # strict=False: in the S4 per-image-assembly path there is no output.json.
    output_path = output_path.resolve(strict=False)
    _require_under(output_path, staging_root)

    binding = _read_binding(staging_root)
    if not binding.get("merge_eligible"):
        raise ValueError(
            "this staging workspace is not merge-eligible — it was built without a "
            "bound run_dir (preview/unbound mode never merges, §5.2)"
        )
    case_dir = _repo_root() / binding["case_dir"]
    bound_run_dir = _repo_root() / binding["run_dir"]
    if bound_run_dir.resolve() != run_dir.resolve():
        raise ValueError(
            f"this staging workspace was bound to {bound_run_dir}, not {run_dir} — a "
            "workspace built for one run cannot be merged into another (even the same "
            "case/images with only run_id differing)"
        )

    allowed, reason = reading_attempt_allowed(run_dir)
    if not allowed:
        raise ValueError(f"merge refused: {reason}")

    current_manifest = load_run_manifest(run_dir)
    if not isinstance(current_manifest, RunManifestV2) or current_manifest.run_id != binding["run_id"]:
        raise ValueError(
            "merge refused: the target run's identity has changed since this "
            "workspace was built (run_id mismatch, or the run manifest was replaced)"
        )

    verification = verify_view_manifest(case_dir, run_dir)
    if (
        not verification.ok
        or verification.on_disk.content_sha256 != binding["view_manifest_sha256"]
        or verification.on_disk.case_metadata_sha256 != binding["case_metadata_sha256"]
    ):
        raise ValueError(
            "merge refused: the view manifest has drifted since this workspace was "
            "built (case_data image(s) or the committed view manifest changed)"
        )

    if binding.get("reading_exam_scope_sha256") != (
        verification.exam_scope.content_sha256 if verification.exam_scope else None
    ):
        raise ValueError("merge refused: the reading exam scope changed since this workspace was built")
    # S-2 (G-3): re-resolve the frozen run policy (re-verifies it against the
    # current run_config.yaml declaration — a build→merge policy drift raises
    # run_policy_drift here, BEFORE any attempt is created — L-12), then confirm
    # it is still the same record bound at build time.
    policy_record = resolve_frozen_run_policy(run_dir)
    if binding.get("run_policy_sha256") != policy_record.policy_hash:
        raise ValueError(
            "merge refused: the run policy changed since this workspace was built "
            f"(bound run_policy_sha256={binding.get('run_policy_sha256')}, "
            f"current={policy_record.policy_hash}) — run_policy_drift"
        )
    # Run/view/policy identity errors take precedence over workflow state: an
    # approval cannot legitimize a workspace bound to a different or drifted
    # run. The pilot gate still runs before any output is loaded or attempt is
    # created.
    experiment_spec = _read_reading_experiment_spec(staging_root)
    if experiment_spec is not None:
        _verify_reading_experiment_spec(staging_root, experiment_spec)
    pilot_review_state = _verify_pilot_review_for_merge(staging_root, binding)
    if experiment_spec is not None:
        _verify_controlled_experiment_launches(
            staging_root, experiment_spec, pilot_review_state
        )
    payload, out_text = _load_isolated_views(
        output_path, staging_root / "out", verification.on_disk, verification.exam_scope
    )
    views = payload["views"]
    if pilot_review_state["enabled"]:
        approved_path = staging_root / pilot_review_state["approved_pilot_path"]
        approved_view = json.loads(approved_path.read_text(encoding="utf-8"))
        approved_id = approved_path.stem
        if approved_id not in views or views[approved_id] != approved_view:
            raise ValueError(
                "merge refused: the pilot inside the merged payload differs from "
                "the externally approved pilot artifact"
            )

    report = check_reading_stage(
        verification.on_disk,
        views,
        exam_scope=verification.exam_scope,
        capability_profile=policy_record.capability_profile,
        run_profile=policy_record.run_profile,
        run_policy_sha256=policy_record.policy_hash,
        run_policy_source=policy_record.source,
    )
    # 2026-08-18 — the cross-axis calibration signal gets a machine consumer.
    # This is the ONE place in the pipeline that holds both the gate① report and
    # the CV evidence the reader produced: `check_reading_stage` sees only the
    # ReadingView, and the evidence never travels into the attempt (F-35). So the
    # audit has to happen here, while the staging workspace is still on disk.
    #
    # It is appended to the SAME report deliberately: `report.blocking()` below
    # is what downgrades `accept=True`, so under golden/regression a reading
    # built on a self-contradicting ruler now cannot be accepted, while
    # exploratory/dev still merge with the fact recorded. Before this, the
    # disagreement existed only in two sidecar fields that nothing read.
    check_calibration_evidence(report, staging_root / "out" / "cv")

    with _merge_lock(run_dir):
        stage_dir = run_dir / STAGE
        attempt_dir = _new_attempt_dir_retry(stage_dir)
        (attempt_dir / "output.json").write_text(out_text, encoding="utf-8")
        output_hash = hash_text(out_text)

        # F-2a: carry the reading summary to the stage root when the isolated
        # reader produced one. The flat reader writes reading_summary.md directly
        # under 0_reading/; the isolated reader writes it to staging out/, and
        # until now merge only carried output.json — so a clean-room reading
        # product stranded one stage short of correction
        # (pipeline._build_correction_messages reads the summary via a bare
        # _read). Same hash-accounting as output: when present, the summary's
        # hash travels in the audited isolation provenance (bound through the
        # isolation_provenance hash). The manifest's reading_isolated_v2 contract
        # permits only output/checks/isolation_provenance artifact keys, so the
        # summary is deliberately NOT a manifest artifact key.
        # F-2b: a missing summary is NOT silently swallowed — it is recorded as
        # an explicit null in provenance, and the named, localizable failure
        # fires at the correction entry (pipeline._build_correction_messages),
        # which the kickoff contract makes a hard dependency.
        summary_src = staging_root / "out" / "reading_summary.md"
        summary_hash: str | None = None
        if summary_src.is_file():
            summary_text = summary_src.read_text(encoding="utf-8")
            (stage_dir / "reading_summary.md").write_text(summary_text, encoding="utf-8")
            summary_hash = hash_text(summary_text)

        provenance = _build_provenance(staging_root, output_hash)
        provenance["reading_summary_sha256"] = summary_hash
        # Archived before provenance is written so the provenance hash — which
        # checks.json then records — covers the evidence digest too. Recorded
        # even when absent: an explicit null says "the reader produced no CV
        # sidecars", which is itself a finding, whereas a missing key would read
        # as "nobody looked".
        provenance["cv_evidence"] = _archive_cv_evidence(staging_root, attempt_dir)
        provenance.update(
            {
                "run_id": binding["run_id"],
                "case_id": binding["case_id"],
                "view_manifest_sha256": binding["view_manifest_sha256"],
            }
        )
        prov_path = attempt_dir / "isolation_provenance.json"
        prov_path.write_text(
            json.dumps(provenance, indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
        provenance_hash = hash_file(prov_path)
        report.add_pass(
            "reading.isolation_provenance_bound",
            CheckLayer.INVARIANT,
            evidence={"isolation_provenance_hash": provenance_hash},
        )

        report.attempt_hash = output_hash
        report.artifact_hash = output_hash
        checks_path = attempt_dir / "checks.json"
        checks_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        checks_hash = hash_file(checks_path)

        _archive_isolation_artifacts(staging_root, attempt_dir)

        blocking = report.blocking()
        do_accept = bool(accept) and not blocking  # accept=True can never override a block
        if do_accept:
            current_manifest.accept(
                StageRecordV2(
                    stage=STAGE,
                    accepted_attempt=int(attempt_dir.name),
                    output_hash=output_hash,
                    input_hashes={
                        "isolation_provenance": provenance_hash,
                        "isolation_manifest": provenance.get("manifest_sha256", ""),
                        "isolation_settings": provenance.get("settings_sha256", ""),
                        "isolation_guard": provenance.get("guard_sha256", ""),
                        "isolation_access_log": provenance.get("access_log_sha256", ""),
                        "isolation_reader_invocations": provenance.get(
                            "reader_invocations_sha256", ""
                        ),
                        "isolation_experiment_spec": provenance.get(
                            "reading_experiment_spec_sha256", ""
                        ),
                        "isolation_directive": provenance.get("directive_sha256", ""),
                    },
                    capability="manual",
                    check_passed=not blocking,
                    artifact_contract="reading_isolated_v2",
                    artifact_hashes={
                        "output": output_hash,
                        "checks": checks_hash,
                        "isolation_provenance": provenance_hash,
                    },
                )
            )
            # F-2c-1 + F-2c rework r1 (MAJOR ④): mirror each accepted view to
            # the stage root as ``0_reading/<expected_output_id>.json`` so the
            # isolated path is indistinguishable from the flat path there.
            # Downstream readers — ``verify_reading_stage_root_against_accepted_attempt``,
            # ``build_verified_window_inputs_from_run``, ``_render_stage`` and
            # the ``reading.present`` glob — all read ``0_reading/*_view.json``
            # at the stage root, which the isolated merge never produced (it
            # archived the aggregate envelope under ``attempts/NNN/output.json``
            # only). Each mirror's content is the view object itself (same
            # ``views`` payload already gate①-validated above — never re-parsed
            # from source). Written only on accept so the mirrors always reflect
            # the accepted product the source verifier binds against, never a
            # later blocking draw that would clobber them.
            #
            # MAJOR ④ (rework r1) ordering + stale cleanup — two real pre-state
            # hazards a clean tmp fixture masks:
            #   (a) a prior round's leftover *_view.json at the stage root
            #       survives an overwrite-only loop, so the next stage's source
            #       verifier rebuilds `current` from ALL *_view.json (stale
            #       included) and hard-crashes on accepted_attempt_mismatch
            #       ("accept first, crash next stage"). Remove every *_view.json
            #       first, then write exactly the accepted set.
            #   (b) writing the accepted pointer before the mirrors left an
            #       interruption window of accepted-but-unmirrored; mirrors-first
            #       means an interruption leaves the run UNaccepted (the verifier
            #       early-returns when there is no accepted attempt), never
            #       half-mirrored. `save_run_manifest` (the persist) is therefore
            #       the LAST step.
            for stale in stage_dir.glob("*_view.json"):
                stale.unlink()
            for view_id, view in views.items():
                (stage_dir / f"{view_id}.json").write_text(
                    json.dumps(view, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            save_run_manifest(current_manifest, run_dir)
        return attempt_dir


_FEEDBACK_POINTER = (
    "A review of your previous output exists at feedback.md — read it FIRST and "
    "apply it to the pilot only before doing anything else. Re-run the pilot "
    "self-check, then STOP again for external approval. Do not start any remaining "
    "image until a later turn explicitly says the revised pilot was approved.\n"
)

_PILOT_APPROVAL_POINTER = (
    "External review approved the current pilot artifact. Do not rewrite that "
    "approved pilot. Continue with the remaining images and reading_summary.md, "
    "applying the approved method consistently.\n"
)

_CONTROLLED_MEASURE_BEFORE_DRAW_DIRECTIVE = """# Per-run directive (directed mode)

`cv_toolbox.md` is REQUIRED reading for this run — not an optional pointer.

Wall-line, window-box and storey-line positions MUST be measured with the pixel
probe before you draw them (measure-before-draw). Do not write an eyeballed
coordinate for any of those three when the clean-vector toolbox applies.
"""


def session_id_path(staging_root: Path) -> Path:
    """Where this workspace's reader session id is recorded.

    Deliberately in the AUDIT sibling directory, not in staging: F-55 moved the
    access log out of the reader's writable surface for exactly this reason, and
    the session id is the same class of thing — operator metadata about the run,
    which the audited party must not be able to forge or erase. It is also what
    makes `--resume` safe to automate: the id comes from the launcher's own
    record, never from anything the reader wrote.
    """
    staging_root = Path(staging_root).resolve()
    return staging_root.parent / f"{staging_root.name}.audit" / "reader_session_id"


def reader_invocations_path(staging_root: Path) -> Path:
    """Append-only controller record of actual reader process launches."""
    return _audit_dir(Path(staging_root).resolve()) / "reader_invocations.jsonl"


def reading_experiment_spec_path(staging_root: Path) -> Path:
    """Frozen controlled-experiment declaration outside reader write access."""
    return _audit_dir(Path(staging_root).resolve()) / "reading_experiment_spec.json"


def _reading_experiment_content_hash(payload: dict) -> str:
    canonical = {key: value for key, value in payload.items() if key != "content_sha256"}
    return hash_text(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    )


def _optional_hash(path: Path) -> str | None:
    return hash_file(path) if path.is_file() else None


def _read_reading_experiment_spec(staging_root: Path) -> dict | None:
    path = reading_experiment_spec_path(staging_root)
    if not path.is_file():
        return None
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"reading experiment spec is unreadable: {path}") from exc
    if spec.get("schema_version") != READING_EXPERIMENT_SCHEMA_VERSION:
        raise ValueError("reading experiment spec has an unsupported schema_version")
    if spec.get("experiment_kind") != "single_plan_same_session":
        raise ValueError("reading experiment spec has an unsupported experiment_kind")
    if not isinstance(spec.get("model"), str) or not spec["model"]:
        raise ValueError("reading experiment spec has no fixed model")
    if spec.get("content_sha256") != _reading_experiment_content_hash(spec):
        raise ValueError("reading experiment spec content hash does not match its payload")
    return spec


def _verify_reading_experiment_spec(staging_root: Path, spec: dict) -> None:
    expected_root = str(Path(staging_root).resolve())
    if spec.get("staging_root") != expected_root:
        raise ValueError("reading experiment spec is bound to a different staging root")
    paths = {
        "manifest_sha256": Path(staging_root) / "MANIFEST.json",
        "binding_sha256": Path(staging_root) / "binding.json",
        "input_inventory_sha256": Path(staging_root) / "input_inventory.json",
        "directive_sha256": Path(staging_root) / "directive.md",
        "run_config_sha256": Path(spec["run_dir"]) / "run_config.yaml",
    }
    for field, path in paths.items():
        expected = spec.get(field)
        if not expected or not path.is_file() or hash_file(path) != expected:
            raise ValueError(
                f"reading experiment drift: {field} no longer matches its frozen bytes"
            )


def _append_reader_invocation(staging_root: Path, record: dict) -> Path:
    path = reader_invocations_path(staging_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        file_lock.flock(fh, file_lock.LOCK_EX)
        fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
        file_lock.flock(fh, file_lock.LOCK_UN)
    return path


def _archive_reader_process_output(
    staging_root: Path, stdout: str, stderr: str
) -> tuple[Path, Path]:
    """Persist each CLI result outside the reader-writable tree.

    The invocation ledger only needs hashes for merge provenance, but the
    controller also needs the raw Claude JSON (including model usage and a
    successful-process error payload) to explain a stopped automatic run.
    """
    output_dir = _audit_dir(staging_root) / "reader_process_outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    index = len(list(output_dir.glob("*_stdout.txt"))) + 1
    stdout_path = output_dir / f"{index:03d}_stdout.txt"
    stderr_path = output_dir / f"{index:03d}_stderr.txt"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    return stdout_path, stderr_path


def _reader_response_summary(stdout: str) -> dict[str, object]:
    """Extract only the CLI completion status from a JSON-mode response."""
    empty = {
        "response_json": False,
        "response_is_error": False,
        "response_error": None,
        "response_subtype": None,
        "response_usage": None,
    }
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return empty
    if not isinstance(payload, dict):
        return empty
    return {
        "response_json": True,
        "response_is_error": bool(payload.get("is_error")),
        "response_error": payload.get("error"),
        "response_subtype": payload.get("subtype"),
        "response_usage": payload.get("usage"),
    }


def _reported_session_id(stdout: str) -> str | None:
    try:
        value = (json.loads(stdout) or {}).get("session_id")
    except (json.JSONDecodeError, AttributeError):
        return None
    return str(value) if value else None


def _reader_input_hashes(staging_root: Path) -> dict[str, str | None]:
    audit = _audit_dir(staging_root)
    return {
        "manifest_sha256": _optional_hash(staging_root / "MANIFEST.json"),
        "binding_sha256": _optional_hash(staging_root / "binding.json"),
        "input_inventory_sha256": _optional_hash(staging_root / "input_inventory.json"),
        "settings_sha256": _optional_hash(staging_root / "isolation_settings.json"),
        "guard_sha256": _optional_hash(staging_root / "guard.py"),
        "directive_sha256": _optional_hash(staging_root / "directive.md"),
        "feedback_sha256": _optional_hash(staging_root / "feedback.md"),
        "pilot_review_state_sha256": _optional_hash(audit / "pilot_review_state.json"),
        "experiment_spec_sha256": _optional_hash(audit / "reading_experiment_spec.json"),
    }


def spawn_command(
    staging_root: Path,
    *,
    model: str | None = None,
    execute: bool = False,
    directive: str | Path | None = None,
    resume: bool = False,
    timeout_seconds: float | None = None,
    subscription_only: bool = False,
) -> list[str]:
    """Build (and optionally run) the reader spawn command.

    ``resume`` (A1, 2026-08-18) selects the SESSION FORM, which is the last big
    untested variable separating today's runs from the 2026-07-07 baseline:

    * ``False`` (default, unchanged): every round is a fresh ``claude -p`` cold
      start. Round N+1 has no memory of round N and must re-read its own prior
      output off disk to know what it did.
    * ``True``: resume the session recorded by the previous round, so the review
      lands in a conversation that still contains the work being reviewed —
      which is how 07-07 actually ran, and is the shape under which its
      measurement depth appeared.

    Why this matters enough to be a parameter rather than a rewrite: the repo has
    spent eight draws attributing "the reader does not go deep" to prompts, tool
    availability and model identity, while the session form was named as gap #6
    on 2026-07-09 and never isolated. Keeping cold start as the default means
    turning it on is a single-variable change, which is the only way the next
    draw can attribute anything.

    ⚠️ Resuming carries the reader's own prior turns and nothing else — same
    workspace, same settings, same clean-room surface. It widens what the reader
    REMEMBERS of its own work, not what it can see or write.
    """
    staging_root = Path(staging_root).resolve()
    experiment_spec = _read_reading_experiment_spec(staging_root)
    model_source = "explicit_argument" if model else "claude_cli_default"
    if experiment_spec is not None:
        _verify_reading_experiment_spec(staging_root, experiment_spec)
        declared_model = experiment_spec["model"]
        if model is None:
            model = declared_model
            model_source = "reading_experiment_spec"
        elif model != declared_model:
            raise ValueError(
                "controlled reading experiment model mismatch: "
                f"declared={declared_model!r}, requested={model!r}"
            )
        else:
            model_source = "explicit_argument_matching_experiment_spec"
    review_state = _read_pilot_review_state(staging_root)
    resume_id = None
    if resume:
        recorded = session_id_path(staging_root)
        if not recorded.is_file():
            raise ValueError(
                "cannot resume: no reader session id was recorded for this workspace "
                f"({recorded}). The first round must run without --resume; only a "
                "session this launcher itself started can be resumed."
            )
        resume_id = recorded.read_text(encoding="utf-8").strip()
        if not resume_id:
            raise ValueError(f"cannot resume: recorded session id is empty ({recorded})")

    if resume_id:
        # The kickoff (and any directive) is already in the resumed context;
        # re-sending it would both waste the window and change the conversation
        # shape we are trying to reproduce. The turn is only the machine-recorded
        # review transition.
        if review_state is not None and review_state["enabled"]:
            if review_state["phase"] == "feedback_issued":
                if not (staging_root / "feedback.md").is_file():
                    raise ValueError("cannot resume rework: feedback.md is missing")
                prompt = _FEEDBACK_POINTER
            elif review_state["phase"] == "approved":
                prompt = _PILOT_APPROVAL_POINTER
            else:
                raise ValueError(
                    "cannot resume: pilot is awaiting an external review decision; "
                    "write feedback or approve the pilot first"
                )
        else:
            prompt = _FEEDBACK_POINTER if (staging_root / "feedback.md").exists() else (
                "Continue from where you stopped.\n"
            )
    else:
        prompt = (staging_root / "kickoff_prompt.md").read_text(encoding="utf-8")
        if experiment_spec is not None:
            frozen_directive = (staging_root / "directive.md").read_text(encoding="utf-8")
            if directive is not None and Path(directive).read_text(
                encoding="utf-8"
            ) != frozen_directive:
                raise ValueError(
                    "controlled reading experiment directive differs from its frozen bytes"
                )
            check_feedback_text(frozen_directive)
            prompt += (
                "\n## Per-run directive (binding for this run)\n" + frozen_directive
            )
        elif directive is not None:
            text = Path(directive).read_text(encoding="utf-8")
            # Same contamination bar as feedback: the directive is a prompt channel.
            check_feedback_text(text)
            (staging_root / "directive.md").write_text(text, encoding="utf-8")
            prompt += "\n## Per-run directive (binding for this run)\n" + text
        if review_state is not None and review_state.get("phase") == "approved":
            prompt += "\nIMPORTANT: " + _PILOT_APPROVAL_POINTER
        elif (staging_root / "feedback.md").exists():
            prompt += "\nIMPORTANT: " + _FEEDBACK_POINTER

    cmd = ["claude", "-p", prompt, "--settings", str(staging_root / "isolation_settings.json")]
    if subscription_only:
        # A subscription run must use the locally logged-in Claude CLI, never a
        # key or endpoint inherited from a provider-wrapper shell.  Empty
        # setting sources also prevents user/project settings from adding an
        # unreviewed MCP or environment route; this workspace's settings file is
        # the sole explicit configuration source.
        cmd.extend(["--setting-sources", "", "--strict-mcp-config"])
    if model:
        cmd.extend(["--model", model])
    if resume_id:
        cmd.extend(["-r", resume_id])
    else:
        # Only the session-STARTING round asks for JSON, because that is the
        # round whose id has to be captured. A resumed round keeps the same id,
        # so its output format is left alone.
        cmd.extend(["--output-format", "json"])
    if execute:
        spawn_env = clean_spawn_env(staging_root, subscription_only=subscription_only)
        runner_path = shutil.which(cmd[0], path=spawn_env.get("PATH")) or cmd[0]
        try:
            version_proc = subprocess.run(
                [runner_path, "--version"], cwd=staging_root, env=spawn_env,
                check=False, text=True, capture_output=True,
            )
            runner_version = (version_proc.stdout or version_proc.stderr).strip()
            runner_version_returncode: int | None = version_proc.returncode
        except OSError as exc:
            runner_version = f"{type(exc).__name__}: {exc}"
            runner_version_returncode = None

        started_at = datetime.now(timezone.utc)
        started_clock = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.run(
                cmd, cwd=staging_root, env=spawn_env,
                check=False, text=True, capture_output=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            proc = subprocess.CompletedProcess(cmd, 124, stdout, stderr)
        completed_at = datetime.now(timezone.utc)
        stdout_path, stderr_path = _archive_reader_process_output(
            staging_root, proc.stdout, proc.stderr
        )
        response = _reader_response_summary(proc.stdout)
        prompt_hash = hash_text(prompt)
        redacted_argv = list(cmd)
        redacted_argv[2] = f"<prompt sha256={prompt_hash}>"
        invocation = {
            "schema_version": READER_INVOCATION_SCHEMA_VERSION,
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "elapsed_ms": round((time.monotonic() - started_clock) * 1000),
            "runner_path": str(Path(runner_path).resolve()),
            "runner_version": runner_version,
            "runner_version_returncode": runner_version_returncode,
            "requested_model": model,
            "model_source": model_source,
            "subscription_only": bool(subscription_only),
            "timeout_seconds": timeout_seconds,
            "timed_out": timed_out,
            "session_form": "resume" if resume_id else "start",
            "session_id": resume_id,
            "reported_session_id": _reported_session_id(proc.stdout),
            "prompt_sha256": prompt_hash,
            "argv_redacted": redacted_argv,
            "argv_sha256": hash_text(json.dumps(cmd, ensure_ascii=False)),
            "returncode": proc.returncode,
            "stdout_sha256": hash_text(proc.stdout),
            "stderr_sha256": hash_text(proc.stderr),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "launcher_module_sha256": hash_file(Path(__file__)),
            "input_hashes": _reader_input_hashes(staging_root),
            **response,
        }
        _append_reader_invocation(staging_root, invocation)
        # stdout is the reader's product record either way; print it so the
        # caller's redirect keeps behaving as before this change.
        print(proc.stdout, end="")
        if proc.stderr:
            print(proc.stderr, end="", file=sys.stderr)
        if timed_out:
            raise subprocess.TimeoutExpired(
                cmd, timeout_seconds, output=proc.stdout, stderr=proc.stderr
            )
        proc.check_returncode()
        if response["response_is_error"] or response["response_subtype"] == "error_max_turns":
            detail = response["response_error"] or response["response_subtype"] or "unknown CLI error"
            raise RuntimeError(f"Claude CLI returned an error payload: {detail}")
        if not resume_id:
            _record_reader_session_id(staging_root, proc.stdout)
    return cmd


def _record_reader_session_id(staging_root: Path, stdout: str) -> None:
    """Persist the session id from a ``--output-format json`` spawn.

    A missing/unparseable id is NOT fatal: the round itself succeeded and its
    product is on disk. It only costs the ability to resume, and a later
    ``--resume`` says so explicitly rather than silently cold-starting — which
    would quietly turn the A1 experiment back into the thing it is testing
    against.
    """
    session_id = _reported_session_id(stdout)
    if not session_id:
        return
    target = session_id_path(staging_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(session_id), encoding="utf-8")


def clean_spawn_env(staging_root: Path, *, subscription_only: bool = False) -> dict[str, str]:
    """Return the narrow environment available to an isolated reader.

    ``subscription_only`` is for the Claude CLI's logged-in subscription
    route.  It intentionally omits all provider API credentials and endpoint
    selectors, while preserving the CLI's OAuth token when the host supplied
    one. The reader uses the launching interpreter's environment on each OS.
    """
    keep = {"PATH", "HOME", "LANG", "LC_ALL"}
    if os.name == "nt":
        keep.update({"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE",
                     "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"})
    if subscription_only:
        keep.add("CLAUDE_CODE_OAUTH_TOKEN")
    else:
        keep.add("ANTHROPIC_API_KEY")
    env = {key: value for key, value in os.environ.items() if key in keep}
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(Path(staging_root).resolve())
    return env


def _copy_case_data(
    case_dir: Path, staging_root: Path, manifest: WorkspaceManifest, view_manifest: ViewManifest,
    scope: ReadingExamScope | None = None,
    vision_resize_tier: str = DEFAULT_VISION_RESIZE_TIER,
) -> None:
    """Copy only what the reader is entitled to see (§2 reader-visibility
    铁律): every ``required_view`` image + ``testdata_prompt.json``. An image
    the view manifest classifies ``excluded_input`` (derived working copy /
    non-drawing asset) is never copied — it is logged in
    ``excluded_from_staging`` instead, not silently dropped.

    F-51: every staged ``required_view`` PNG is then resized in place to
    ``vision_resize_tier`` (:func:`_copy_case_data_image`) — this is the one
    place that decides what the reader, cv_probe, and the manifest hash all
    see, so it is the one place the resize belongs. ``testdata_prompt.json``
    is not an image and is copied via the untouched :func:`_copy_file`.
    """
    src = case_dir / "case_data"
    if not src.exists():
        src = case_dir
    dest = staging_root / "case_data"
    dest.mkdir(parents=True, exist_ok=True)
    excluded_by_basename = {
        e.source_image.rsplit("/", 1)[-1]: e for e in view_manifest.excluded_entries()
    }
    for path in sorted(src.iterdir()):
        if not path.is_file():
            continue
        if path.name == "testdata_prompt.json":
            _copy_file(path, dest / path.name, "case_data", manifest)
            continue
        if path.suffix.lower() != ".png":
            continue
        excluded = excluded_by_basename.get(path.name)
        if excluded is not None:
            manifest.excluded_from_staging.append(
                {
                    "input_id": excluded.input_id,
                    "source_image": excluded.source_image,
                    "excluded_reason": excluded.excluded_reason,
                }
            )
            continue
        required_by_basename = {
            entry.source_image.rsplit("/", 1)[-1]: entry for entry in view_manifest.required_entries()
        }
        required = required_by_basename.get(path.name)
        if required is None:
            continue
        if scope is not None and required.input_id not in scope.input_ids:
            manifest.excluded_from_staging.append(
                {"input_id": required.input_id, "source_image": required.source_image,
                 "excluded_reason": "outside_reading_exam_scope", "source": scope.source}
            )
            continue
        _copy_case_data_image(path, dest / path.name, "case_data", manifest, vision_resize_tier)


def _write_input_inventory(
    staging_root: Path, manifest: WorkspaceManifest, view_manifest: ViewManifest,
    scope: ReadingExamScope | None = None,
) -> None:
    """§2 staging projection: reader-visible identity only (input_id, file,
    view_type, declared_direction_token, floor_ref, expected_output_id) — no
    denominator/negative-evidence content. Lives at staging ROOT (not under
    ``out/``), so the existing Write-allow list already makes it read-only to
    the reader without any additional guard logic."""
    payload = derive_input_inventory(view_manifest, scope)
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    _write_generated(staging_root / "input_inventory.json", text, "input_inventory", manifest)


def _write_binding(staging_root: Path, manifest: WorkspaceManifest, binding: dict) -> None:
    """Immutable staging-provenance binding (§5.2). Never copied from — and
    never visible to — anything but :func:`merge_isolated_output`'s own
    re-verification; lives at staging root so the reader cannot write it."""
    text = json.dumps(binding, indent=2, sort_keys=True, ensure_ascii=False)
    _write_generated(staging_root / "binding.json", text, "binding", manifest)


def _read_binding(staging_root: Path) -> dict:
    path = Path(staging_root) / "binding.json"
    if not path.exists():
        raise ValueError(
            f"no binding.json under {staging_root} — this staging workspace was not "
            "built by build_isolation_workspace (or predates the isolation run-binding wire)"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _load_isolated_views(
    output_path: Path, out_dir: Path, view_manifest: ViewManifest,
    scope: ReadingExamScope | None = None,
) -> tuple[dict, str]:
    """Return ``(payload, out_text)`` where ``payload`` is ``{'views': {...}}``.

    Old path (unchanged, F-5 keeps it working): ``output.json`` shaped
    ``{'views': {<expected_output_id>: <ReadingView>, ...}}`` — accepted verbatim.

    S4 path: if there is no single aggregate, mechanically assemble the
    per-image ``<expected_output_id>.json`` files under ``out/`` (one JSON per
    source drawing, exactly what session_kickoff.md tells the reader to write)
    into ``{'views': {...}}``. Assembly is pure搬运 — no normalization, no
    defaults, no reordering; each view is ``json.loads`` of its source file.
    Fail-closed on missing (a manifest id with no file) or extra (a
    ``*_view.json`` not declared in the manifest) — the kickoff/merge contract
    gap nobody owned (F-5), now owned by merge, not by the LLM's memory.
    """
    if output_path.exists():
        out_text = output_path.read_text(encoding="utf-8")
        try:
            candidate = json.loads(out_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"aggregate output.json is not valid JSON: {exc}") from exc
        if not isinstance(candidate, dict) or not isinstance(candidate.get("views"), dict):
            raise ValueError(
                "aggregate output.json must be shaped {'views': {<expected_output_id>: "
                "<ReadingView JSON>, ...}}"
            )
        return candidate, out_text

    expected_ids = {
        output_id for output_id, input_id in view_manifest.expected_output_ids().items()
        if scope is None or input_id in scope.input_ids
    }
    per_image = {p.stem: p for p in sorted(out_dir.glob("*_view.json"))}
    missing = sorted(eid for eid in expected_ids if eid not in per_image)
    if missing:
        raise ValueError(
            "no aggregate output.json and missing per-image view files for "
            f"expected_output_ids: {missing}"
        )
    extra = sorted(stem for stem in per_image if stem not in expected_ids)
    if extra:
        raise ValueError(
            "merge refused: unexpected per-image view files not declared in the "
            f"view manifest: {extra}"
        )
    views = {
        eid: json.loads(per_image[eid].read_text(encoding="utf-8"))
        for eid in sorted(expected_ids)
    }
    payload = {"views": views}
    return payload, json.dumps(payload, ensure_ascii=False)


def _copy_reading_guidance(staging_root: Path, manifest: WorkspaceManifest) -> None:
    resource_root = _repo_root() / READING_RESOURCE_RELATIVE
    dest_root = staging_root / READING_RESOURCE_RELATIVE
    for path in sorted(resource_root.rglob("*")):
        if not path.is_file():
            continue
        if path.name == "judge_rubric.md":
            continue
        _assert_source_allowed(path)
        rel = path.relative_to(resource_root)
        if path.name == "session_kickoff.md":
            # F-2: the kickoff names the repository source of the worked example.
            # Rewrite the pointer to the staged copy so the reader only sees files
            # present in the isolation manifest. Both sides agree on
            # WORKED_EXAMPLE_STAGED (the consistency lock stats this path in staging).
            _copy_reading_kickoff(path, dest_root / rel, manifest)
            continue
        _copy_file(path, dest_root / rel, "skill", manifest)


def _copy_reading_kickoff(src: Path, dest: Path, manifest: WorkspaceManifest) -> None:
    text = src.read_text(encoding="utf-8")
    if WORKED_EXAMPLE_SOURCE in text:
        text = text.replace(WORKED_EXAMPLE_SOURCE, WORKED_EXAMPLE_STAGED)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    _add_manifest_entry(dest, src, "skill", manifest)


def _copy_worked_example(staging_root: Path, manifest: WorkspaceManifest) -> None:
    """Stage the kickoff's worked-example reading-view JSON (F-2).

    Staged at a path that trips no DENY_TOKEN and registered in MANIFEST — the
    07-30 hand-staged copy was *not* in MANIFEST, so the merge provenance ledger
    missed it. Recording it here is this slice's primary value, not a side effect.
    """
    src = _repo_root() / WORKED_EXAMPLE_SOURCE
    dest = staging_root / WORKED_EXAMPLE_STAGED
    _copy_file(src, dest, "reference", manifest)


def _copy_cv_toolbox(staging_root: Path, manifest: WorkspaceManifest) -> None:
    src_root = _repo_root() / "src" / "agent" / "reading" / "cv_toolbox"
    dest_root = staging_root / "src" / "agent" / "reading" / "cv_toolbox"
    for path in sorted(src_root.rglob("*")):
        if path.is_file():
            _copy_file(path, dest_root / path.relative_to(src_root), "cv_toolbox", manifest)
    for pkg in [
        staging_root / "src" / "__init__.py",
        staging_root / "src" / "agent" / "__init__.py",
        staging_root / "src" / "agent" / "reading" / "__init__.py",
    ]:
        _write_generated(pkg, "", "cv_toolbox", manifest)
    cv_probe_src = (_repo_root() / "scripts" / "tool_scripts" / "cv_probe.py").read_text(encoding="utf-8")
    cv_probe_src = cv_probe_src.replace("Path(__file__).resolve().parents[2]", "Path(__file__).resolve().parents[1]")
    _write_generated(staging_root / "tools" / "cv_probe.py", cv_probe_src, "tool", manifest)



def _write_kickoff(
    case_dir: Path, staging_root: Path, manifest: WorkspaceManifest,
    *, pilot_review_gate: bool = True,
) -> None:
    text = (
        f"Read {READING_RESOURCE_RELATIVE.as_posix()}/session_kickoff.md and follow it "
        f"for case {case_dir.name}.\n"
        "The drawings are at case_data/. Write reading outputs under out/ — one "
        "file per source image named exactly `<expected_output_id>.json` (each "
        "image's expected_output_id is listed in `input_inventory.json`; do NOT "
        "derive the name from the source PNG by appending `_view` — a stem that "
        "already ends in `_view` IS its own expected_output_id) — plus "
        "reading_summary.md. Write CV-probe request JSON files under requests/. "
        "The normal probe form is "
        "`python tools/run_cv_probe.py --tool <tool> --image <path> --out-dir "
        "out/<name> [--<key> <value> ...]`; use "
        "`python tools/run_cv_probe.py --batch requests/<name>.json` for sweeps "
        "(maximum 32 requests, all validated before any run). The legacy "
        "`python tools/run_cv_probe.py --request requests/<name>.json` form is "
        "also available. "
        + (
            "This run has ONE pilot approval gate after the pilot image. Finish "
            "the first plan image, run the guide's self-check against it, then STOP "
            "and say the pilot is ready for review — ending your turn there is "
            "correct, not a failure to finish. If `feedback.md` exists in this "
            "directory it IS a rework request: read it first, revise ONLY the "
            "pilot, rerun its self-check, and STOP AGAIN. Do not start the "
            "remaining images until a later launcher turn explicitly says external "
            "review approved the revised pilot. After that approval, work through "
            "the remaining images and summary on your own; review is a one-way "
            "state transition, not a conversation, so do not ask questions.\n"
            if pilot_review_gate
            else
            # 2026-08-18 control-arm form. Stated as an explicit OVERRIDE rather
            # than by editing the staged product doc: a silently divergent copy of
            # session_kickoff.md would make the run unauditable against the
            # committed skill, and this repo has already been bitten by documents
            # that quietly contradict each other (D1).
            "FOR THIS RUN ONLY, overriding the pilot-review step in "
            "session_kickoff.md: there is NO review point. Nobody will read your "
            "pilot and nobody will answer a question. Work straight through — "
            "first image, its self-check, then the remaining images and the "
            "summary — and finish in this one turn.\n"
        )
    )
    _write_generated(staging_root / "kickoff_prompt.md", text, "kickoff", manifest)



def _write_guard_profile(staging_root: Path, profile: str) -> None:
    """Record which guard policy this workspace runs under.

    Written to the AUDIT sibling directory, never into staging: `guard.py` reads
    it from there, so the audited party cannot grant itself a weaker policy by
    writing a file it is allowed to write. Anything other than the exact string
    "relaxed" resolves to strict on the guard side too, so a corrupted or
    truncated marker fails closed rather than silently opening the boundary.
    """
    if profile not in {"strict", "relaxed", "observe"}:
        raise ValueError(
            f"unknown guard profile: {profile!r} "
            "(expected 'strict', 'relaxed' or 'observe')"
        )
    target = _audit_dir(staging_root) / "guard_profile"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(profile, encoding="utf-8")


def _write_guard_and_wrappers(staging_root: Path, manifest: WorkspaceManifest) -> None:
    for name, dest in [
        ("guard.py", staging_root / "guard.py"),
        ("run_cv_probe.py", staging_root / "tools" / "run_cv_probe.py"),
    ]:
        text = resources.files("src.agent.execution.isolation_templates").joinpath(name).read_text(encoding="utf-8")
        if name == "guard.py":
            text = text.replace('"__ISOLATION_SOURCE_REPOSITORY__"', repr(_repo_root().as_posix()))
        _write_generated(dest, text, "tool", manifest)
        dest.chmod(0o755)


def _write_settings(staging_root: Path, manifest: WorkspaceManifest) -> None:
    settings = {
        "permissions": {
            "defaultMode": "default",
            "allow": [
                f"Read({_claude_abs(staging_root)}/**)",
                f"Write({_claude_abs(staging_root / 'out')}/**)",
                f"Edit({_claude_abs(staging_root / 'out')}/**)",
                f"Write({_claude_abs(staging_root / 'requests')}/**)",
                f"Edit({_claude_abs(staging_root / 'requests')}/**)",
                "Bash",
            ],
            "deny": [
                f"Read({_claude_abs(_repo_root())}/**)",
                "Read(//root/**)",
                "Read(//home/**)",
                "WebFetch",
                "WebSearch",
                "Agent",
                "Task",
                "mcp__*",
            ],
        },
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": subprocess.list2cmdline([sys.executable, str(staging_root / 'guard.py')])
                            if os.name == "nt" else shlex.join([sys.executable, str(staging_root / 'guard.py')]),
                        }
                    ],
                }
            ]
        },
    }
    _write_generated(
        staging_root / "isolation_settings.json",
        json.dumps(settings, indent=2, sort_keys=True),
        "settings",
        manifest,
    )


def _assert_manifest_clean(manifest: WorkspaceManifest) -> None:
    for entry in manifest.files:
        _assert_rel_allowed(Path(entry.source_path))


def _assert_source_allowed(path: Path) -> None:
    rel = Path(_repo_relative(path))
    _assert_rel_allowed(rel)



def _assert_rel_allowed(rel: Path) -> None:
    parts = set(rel.parts)
    name = rel.name
    if name in HARD_BLOCK_FILENAMES:
        raise ValueError(f"forbidden source file: {rel}")
    if "test_baseline" in parts and "gt" in parts:
        raise ValueError(f"forbidden gt source path: {rel}")
    # 2026-08-19: prescan was withdrawn from the working tree (archived under
    # AI_agent/archive/reading_prescan/), so the one exception that used to let
    # run_*/0_reading/cv_evidence/<stem>/prescan/** through is gone with it.
    # Every run_* source path is now forbidden, full stop.
    if any(part.startswith("run_") for part in rel.parts[:-1]):
        raise ValueError(f"forbidden run source path: {rel}")
    if parts & HARD_BLOCK_PARTS:
        raise ValueError(f"forbidden source path component: {rel}")
    if name.startswith("verdict") or name.startswith("grade") or name.endswith("_score.json"):
        raise ValueError(f"forbidden generated judgment artifact: {rel}")


def _semantic_warnings(path: Path) -> list[str]:
    text = str(path).lower()
    return [f"semantic token present in allowed source path: {token}:{path}" for token in SEMANTIC_WARNING_TOKENS if token in text]


def _copy_file(src: Path, dest: Path, category: str, manifest: WorkspaceManifest) -> None:
    _assert_source_allowed(src)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    _add_manifest_entry(dest, src, category, manifest)


def _copy_case_data_image(
    src: Path, dest: Path, category: str, manifest: WorkspaceManifest,
    vision_resize_tier: str = DEFAULT_VISION_RESIZE_TIER,
) -> None:
    """Like :func:`_copy_file`, but for a reader-visible ``case_data/*.png``:
    resize the staged copy to ``vision_resize_tier`` (F-51) BEFORE the
    manifest hash is computed, so ``MANIFEST.json`` records the hash of what
    is actually on disk (and what the reader/cv_probe actually see), not a
    stale pre-resize hash. A no-op resize (image already within the tier)
    leaves the copy byte-for-byte identical to ``shutil.copy2``'s output, so
    this only ever changes behavior for an oversized image.

    ``manifest``'s ``source_path`` still names the true repo original (audit
    provenance is about WHERE the bytes came from, not whether they were
    resized in staging) — only the recorded ``sha256`` reflects the resize.
    """
    _assert_source_allowed(src)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    resize_image_file_to_tier(dest, vision_resize_tier)
    _add_manifest_entry(dest, src, category, manifest)


def _write_generated(dest: Path, text: str, category: str, manifest: WorkspaceManifest) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    _add_manifest_entry(dest, None, category, manifest)


def _add_manifest_entry(dest: Path, src: Path | None, category: str, manifest: WorkspaceManifest) -> None:
    rel_dest = dest.relative_to(manifest.staging_root).as_posix()
    source = _repo_relative(src) if src else f"<generated>/{rel_dest}"
    manifest.warnings.extend(_semantic_warnings(Path(source)))
    manifest.files.append(
        ManifestEntry(
            path=rel_dest,
            source_path=source,
            category=category,
            sha256=hash_file(dest),
        )
    )


def _build_provenance(staging_root: Path, output_hash: str) -> dict:
    manifest_path = staging_root / "MANIFEST.json"
    settings_path = staging_root / "isolation_settings.json"
    guard_path = staging_root / "guard.py"
    # F-55: access_log.jsonl no longer lives under staging_root — see
    # `_audit_dir`.
    access_log_path = _audit_dir(staging_root) / "access_log.jsonl"
    pilot_review_path = pilot_review_state_path(staging_root)
    reader_invocations = reader_invocations_path(staging_root)
    experiment_spec = reading_experiment_spec_path(staging_root)
    directive_path = staging_root / "directive.md"
    denied = 0
    entries = 0
    if access_log_path.exists():
        for line in access_log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entries += 1
            if json.loads(line).get("decision") == "deny":
                denied += 1
    return {
        "schema_version": ISOLATION_SCHEMA_VERSION,
        "staging_root": str(staging_root),
        "output_hash": output_hash,
        "manifest_sha256": hash_file(manifest_path) if manifest_path.exists() else "",
        "settings_sha256": hash_file(settings_path) if settings_path.exists() else "",
        "guard_sha256": hash_file(guard_path) if guard_path.exists() else "",
        "access_log_sha256": hash_file(access_log_path) if access_log_path.exists() else "",
        "access_log_entries": entries,
        "access_log_denied": denied,
        "pilot_review_state_sha256": (
            hash_file(pilot_review_path) if pilot_review_path.exists() else ""
        ),
        "reader_invocations_sha256": (
            hash_file(reader_invocations) if reader_invocations.exists() else ""
        ),
        "reading_experiment_spec_sha256": (
            hash_file(experiment_spec) if experiment_spec.exists() else ""
        ),
        "directive_sha256": hash_file(directive_path) if directive_path.exists() else "",
    }


def _archive_isolation_artifacts(staging_root: Path, attempt_dir: Path) -> None:
    archive = attempt_dir / "isolation_archive"
    archive.mkdir(parents=True, exist_ok=True)
    sources = {
        "MANIFEST.json": staging_root / "MANIFEST.json",
        "isolation_settings.json": staging_root / "isolation_settings.json",
        "guard.py": staging_root / "guard.py",
        # F-55: relocated out of staging_root — see `_audit_dir`. The archive
        # keeps the same filename so nothing downstream of this function
        # needs to know the log moved.
        "access_log.jsonl": _audit_dir(staging_root) / "access_log.jsonl",
        "pilot_review_state.json": pilot_review_state_path(staging_root),
        "reader_invocations.jsonl": reader_invocations_path(staging_root),
        "reading_experiment_spec.json": reading_experiment_spec_path(staging_root),
        "directive.md": staging_root / "directive.md",
    }
    for name, src in sources.items():
        if src.exists():
            dest = archive / name
            shutil.copy2(src, dest)
            # F-55: copy2 preserves the source's mode bits, and guard.py (and,
            # after this batch, everything else outside out/+requests/) is
            # now read-only in staging_root. attempt_dir lives under run_dir,
            # which this fix never locks down, so undo that side effect here
            # rather than let a read-only file appear somewhere no other code
            # in this tree expects one.
            dest.chmod(0o644)


def _archive_cv_evidence(staging_root: Path, attempt_dir: Path) -> dict | None:
    """Carry the reader's CV sidecars out of staging before it is discarded.

    2026-08-21 (F-35). ``check_calibration_evidence`` already reads this tree
    during merge, then the workspace goes away and the evidence with it. The
    2026-07-07 runs kept 92 and 38 sidecars — per-image calibration residuals,
    profiler runs, band-restricted detections, and overlay_logger ledgers giving
    a disposition and a reason for every candidate — and that is why those
    readings can still be audited step by step today. The 2026-08-21 sm25 run
    made 42 cv_probe / 19 profiler / 56 crop calls and left nothing behind, so
    how it reached a wrong answer can only be inferred from one line of prose in
    a stroke note. Whether a reading measured or eyeballed is exactly what the
    evidence shows and the product hides.

    Archived per attempt rather than at the stage root: attempts are append-only,
    so the evidence stays bound to the attempt that produced it even when a later
    attempt supersedes it.

    Size, measured on the 2026-07-07 sm21 tree: 12 MB total, of which the JSON
    sidecars are 984 KB and the overlay PNGs are 11 MB. The 2026-08-21 dissection
    that recovered how those readings worked read only the JSON — the overlays
    are for a human doing visual review, which is a real consumer, so both are
    kept and ``total_bytes`` is recorded rather than silently trimmed. If repo
    growth ever forces a choice, that number is where to start.
    """
    src_root = staging_root / "out" / "cv"
    if not src_root.is_dir():
        return None
    dest_root = attempt_dir / "cv_evidence"
    files: list[tuple[str, str]] = []
    total_bytes = 0
    for src in sorted(p for p in src_root.rglob("*") if p.is_file()):
        rel = src.relative_to(src_root)
        dest = dest_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        dest.chmod(0o644)
        files.append((rel.as_posix(), hash_file(dest)))
        total_bytes += dest.stat().st_size
    if not files:
        return None
    digest = hash_text("\n".join(f"{name}:{h}" for name, h in files))
    return {
        "file_count": len(files),
        "total_bytes": total_bytes,
        "content_sha256": digest,
    }


def _new_attempt_dir_retry(stage_dir: Path, retries: int = 5) -> Path:
    root = stage_dir / "attempts"
    root.mkdir(parents=True, exist_ok=True)
    for _ in range(retries):
        used = [int(p.name) for p in root.iterdir() if p.is_dir() and p.name.isdigit()]
        idx = (max(used) + 1) if used else 1
        path = root / f"{idx:03d}"
        try:
            path.mkdir(parents=True, exist_ok=False)
            return path
        except FileExistsError:
            time.sleep(0.01)
    raise FileExistsError(f"could not allocate attempt dir under {root}")


@contextmanager
def _merge_lock(run_dir: Path):
    lock_path = run_dir / ".isolation_merge.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as fh:
        file_lock.flock(fh, file_lock.LOCK_EX)
        try:
            yield
        finally:
            file_lock.flock(fh, file_lock.LOCK_UN)


def _require_under(path: Path, root: Path) -> None:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise ValueError(f"path escapes staging: {path}") from exc


def _require_outside_repo(path: Path) -> None:
    try:
        path.resolve(strict=False).relative_to(_repo_root())
    except ValueError:
        return
    raise ValueError(f"staging root must be outside repo: {path}")


# F-55: where the guard's append-only access_log.jsonl actually lives — a
# SIBLING of staging_root, never a descendant of it. `guard.py` is a
# standalone, stdlib-only file copied byte for byte into every staging tree
# (it cannot receive a per-run injected constant), so it recomputes this exact
# same rule from its own `_staging_root()`; see that file's `_audit_dir`
# docstring for the full rationale. The two MUST NEVER disagree, mirrored the
# same way `writable_root`/`OUTPUT_ROOT_DIR` already are between guard.py and
# run_cv_probe.py.
def _audit_dir(staging_root: Path) -> Path:
    root = Path(staging_root).resolve()
    return root.parent / f"{root.name}.audit"


# F-55, part 2: after the workspace is fully built, remove the WRITE bit from
# every FILE outside out/ and requests/ — an OS-level backstop that does not
# replace the guard's Write/Edit-tool-only check (S2a/_check_write_target)
# but covers what it structurally cannot: A3 (2026-08-16, same day) let
# reader-authored Python run, and nothing wires WRITE_ALLOWED_DIRS into what
# an interpreter's own `open()` may target — a plain
# `python -c 'open("guard.py","a").write(...)'` reached the real file (the
# controller reproduced this: guard.py grew a line, 1224 -> 1225). This is the
# read-only half of the direction the user ratified 2026-08-16: physical
# unwritability for files an accidental hand-slip (a measurement script that
# typos its output path) could otherwise clobber, NOT a new scanner rule that
# recognizes "code that writes to a protected path" — the 2026-08-13 threat
# model narrowing is explicit that this guard defends an accidental/ordinary
# path, not a reader that deliberately `os.chmod`s a file back to writable
# first (see the module docstring at the top of guard.py).
#
# Only FILES are chmod'd, not directories: a file's own write bit is what
# governs `open(path, "a"/"w")` on an EXISTING file (the demonstrated repro,
# both for guard.py and for the pre-fix access_log.jsonl), while a
# directory's write bit governs create/delete/rename of the entries inside
# it. Leaving directories writable keeps `shutil.rmtree`/pytest's own
# tmp_path teardown working (deleting a file only needs the *directory's*
# write bit on POSIX, never the file's own) and keeps the many existing test
# fixtures that stage a NEW file directly under a built workspace (e.g.
# writing `request.json` at staging root, or `feedback.md` after a pilot
# review) unaffected — only pre-existing files become unwritable, exactly the
# set this fix needs to protect. Deleting a protected file and recreating it
# under the same name is a real residual gap, but it takes a DELIBERATE
# two-step bypass, which is the class of attack the ratified direction
# explicitly does not ask this fix to close.
#
# ⚠️ KNOWN, TESTED, DOCUMENTED LIMITATION (found while verifying this fix,
# 2026-08-16 — not something the ratified direction anticipated): this whole
# mechanism is DAC permission bits, and DAC is a no-op for a process running
# as root (uid 0) against files it owns — which is this dev container's
# reality end to end (`whoami` -> root for the orchestrator, the spawned
# reader, and every guard subprocess alike). `open(chmod_0o444_file, "a")`
# from a plain root `python -c` STILL SUCCEEDS here; it was verified failing
# only under an explicitly capability-dropped child
# (`setpriv --bounding-set=-dac_override,-fowner,-chown`, confirmed to work on
# this host). The immutable-attribute alternative (`chattr +i`, which even
# root cannot write through without first clearing the flag) was also tried
# and is unavailable — this container's `/` and `/tmp` are overlayfs, which
# refuses FS_IOC_SETFLAGS outright ("Operation not permitted while setting
# flags"). Wiring capability-dropping into `spawn_command` (the one call site
# that launches the reader's whole top-level session, so a bounding-set drop
# there would be inherited by everything the reader subsequently runs,
# including every guard.py hook subprocess) would close this, but it is a
# materially bigger, higher-blast-radius change than "make some files
# read-only" — it cannot be validated end to end without actually launching a
# real reader session, which this batch's scope and time-box do not cover.
# Left as an explicit follow-up (see the execution log) rather than silently
# shipped or silently ignored: this fix is still correct and still valuable
# (it protects a hand-slip in any deployment where the reader runs less
# privileged than its own protected files, which is the ordinary shape this
# kind of boundary is deployed under), it is simply NOT a complete defense in
# THIS specific all-root container today.
def _lock_down_readonly_surface(staging_root: Path) -> None:
    staging_root = Path(staging_root).resolve()
    protected_roots = [(staging_root / name).resolve() for name in WRITE_ALLOWED_DIRS]
    for path in staging_root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        resolved = path.resolve()
        if any(root in resolved.parents for root in protected_roots):
            continue
        mode = path.stat().st_mode
        path.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _claude_abs(path: Path) -> str:
    return "/" + Path(path).resolve().as_posix()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _repo_relative(path: Path | None) -> str:
    if path is None:
        return ""
    path = Path(path).resolve()
    try:
        return path.relative_to(_repo_root()).as_posix()
    except ValueError:
        return path.as_posix()
