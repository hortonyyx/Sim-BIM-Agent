"""Stepwise judge-in-the-loop driver (backlog #1, 2026-06-19).

The main Agent (orchestrator + judge②) drives a case ONE stage at a time, so each
stage gets its own judge report and a failing stage STOPS the run (new_case_guide
§2 ideal). This CLI is the thin wiring around src/agent/execution/step_orchestrator:
it owns the real stage executors + gate① checks + visual renders + the judge
packet, while the orchestrator owns the decision logic.

Verbs (one stage / action per call — the Agent invokes them in order):

    run    <case> <run> <stage>        draw + gate① (+ blind resample on block) → stop
    judge  <case> <run> <stage> --verdict v.json   record the Agent's StageVerdict → classify
    resample <case> <run> <stage>      force a fresh blind draw (judge-driven), same budget
    approve-geometry <case> <run> --actor X --digest SHA --date ISO   record the human geometry confirm
    status <case> <run>                print the orchestration ledger

Stages: 0_reading 1_correction 2_modelling 3_split_pairing 4_mep 5_intakeoutput.
After 5_intakeoutput passes, run downstream + EP with the existing driver:
    python scripts/run_full_pipeline.py <case> --base-dir <bd> \
        --intake-from <run>/5_intakeoutput/intake_output.json
then `record_baseline.py <case> <run> --require-ep` for _run/baseline.json
and report/REPORT.md.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import marshal
import os
import shutil
import sys
import tempfile
import warnings
from pathlib import Path

# Be robust to CWD: make `import src...` work whether or not the package is
# installed editable (mirrors how the repo runs scripts from the root).
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))  # Own checkout precedes any editable install.

from src.agent.execution import (  # noqa: E402
    RunManifest,
    RunManifestV2,
    RunPolicy,
    StageRunner,
    StageOutcome,
    StepStatus,
    approve_geometry,
    geometry_is_approved,
    invalidate,
    load_state,
    mark_geometry_approved,
    mark_review_approved,
    record_review,
    review_is_current,
    run_one_stage,
    submit_verdict,
    update_state,
    GEOMETRY_CHECKPOINT_STAGE,
)
from src.agent.execution.case_metadata import (  # noqa: E402
    dimensioned_view_names,
    expected_zone_total_from_testdata,
)
from src.agent.execution.policy import ConfirmationPolicy  # noqa: E402
from src.agent.execution.approval import GeometryApproval  # noqa: E402
from src.agent.execution.run_config import GradeConfig, RunConfig, load_run_config  # noqa: E402
from src.agent.judge.executor import rubric_for, run_judge  # noqa: E402
from src.agent.judge.verdict import StageVerdict  # noqa: E402
from src.agent.runner import load_intake_from, run_downstream_ep  # noqa: E402
from src.agent.state import AgentState  # noqa: E402
from src.validator.checks.schema import CheckLayer, CheckReport, CheckStatus  # noqa: E402

_STAGES = ["0_reading", "1_correction", "2_modelling", "3_split_pairing",
           "4_mep", "5_intakeoutput"]
# Phase D convergence: schema 0--7 artifacts are never cache candidates.
# v2 keeps its exact legacy projection below; its sidecar label is CURRENTLY
# v10 (history: v8 -> v9 -> v10, both bumps below). This constant is
# independent of src.agent.judge.score_schema.SCORE_SIDECAR_SCHEMA (the typed
# v3 side) — do NOT bump them in lockstep. (NIT-F25, 2026-08-13: renamed from
# `SCORER_SCHEMA` -- the OTHER, unrelated `score_schema.SCORER_SCHEMA` typed-
# contract-label constant this comment already warned not to confuse it with
# was itself removed in the same rename batch, in favor of the pre-existing
# `SCORE_SIDECAR_SCHEMA`; both were pure symbol renames, no behavior change.)
#   v8 -> v9: legacy scoring SEMANTICS changed (4a11097 F-1a/F-1b — envelope
#   unwrap + empty-scores headline): a sidecar labeled v8 or earlier holds
#   stale scores under the old semantics, so _grade_attempt_artifacts must
#   recompute rather than reuse it (MAJOR-1: a stale v8 sidecar otherwise
#   short-circuited grading and replayed the old false-pass headlines).
#   v9 -> v10: F-22 changed both the correction-boundary SEMANTICS (deleted
#   the double wall-thickness expansion that double-counted post-F-17
#   outer-skin products, then narrowed trust to schema-v3 identity only —
#   BLOCKER-1) and the sidecar SHAPE (`boundary` entries carry a `status`
#   field; a new `output_convention` provenance field was added) — a
#   v9-or-earlier cached sidecar holds stale deltas under the old (wrong)
#   semantics and lacks the new fields, so it must not be reused.
#   v10 -> v11: F-22 BLOCKER-1 round 2 (2026-08-13) changed the trust
#   SEMANTICS again (a self-reported `deterministic_core_stamp` alone is no
#   longer sufficient -- see `judge.correction_score._is_trusted_output_convention`)
#   and the sidecar SHAPE (`output_convention` gained a `declared` key,
#   independent of `trusted`). A v10-or-earlier cached sidecar may have
#   `trusted=True` under the OLD (now-forgeable) rule and lacks `declared`
#   entirely -- it must not be reused; every accepted B5 attempt needs a
#   rerun through the write path (which now also issues
#   `deterministic_core_proof.json`) to regain a trusted score.
#
#   F-24 (2026-08-13, sol re-review of the round-2 fix above): this hand-
#   maintained version number is NOT, on its own, sufficient to invalidate a
#   stale cache when the trust rule changes again -- it depends on an
#   engineer remembering to bump it every time `DETERMINISTIC_CORE_STAMP_VERSION`
#   or `CORRECTION_OUTPUT_CONVENTION` change. See `_current_scoring_semantics_identity`
#   below, which is read LIVE from those two constants (not copied) and
#   checked as a SEPARATE, ADDITIONAL cache-predicate field -- bumping either
#   constant now invalidates every cached `score_vs_gt.json` automatically,
#   with no `LEGACY_SCORE_CACHE_SCHEMA` bump required. This schema label is
#   kept (not removed) because it also covers scoring-SHAPE changes (new
#   sidecar fields, MAJOR-1's kind of change) that are not expressible as a
#   function of those two constants.
LEGACY_SCORE_CACHE_SCHEMA = "11"


def _current_scoring_semantics_identity() -> dict:
    """F-24 (2026-08-13): the cache-predicate half of the trust identity that
    `_is_trusted_output_convention` (`src/agent/judge/correction_score.py`)
    actually gates scoring on, read LIVE at cache-check time -- NOT a new
    hand-maintained version number (sol's dispatch: "不许写「不与行为绑定的
    声明」... 必须能通过「把对应实现中和掉、看该字段是否跟着变」的实测").

    Both fields already exist and already gate real scoring behavior; this
    function invents nothing new, it only reads the SAME two constants
    `_is_declared_output_convention`/`_is_trusted_output_convention` compare
    a candidate's stamp against, module-qualified (not `from ... import`) so
    a live mutation of either is observed here too -- the exact discipline
    `correction_score.py`'s own module docstring documents for
    `CORRECTION_OUTPUT_CONVENTION` and `DETERMINISTIC_CORE_STAMP_VERSION`:

    * `deterministic.DETERMINISTIC_CORE_STAMP_VERSION` -- which core revision
      a stamp/proof must carry to be trusted at all.
    * `correction_score.CORRECTION_OUTPUT_CONVENTION` -- which output-frame
      identity a trusted product is currently assumed to use.

    A cached sidecar whose `scoring_semantics` does not match the CURRENT
    live values of these two constants is stale and must be recomputed,
    regardless of whether anyone also remembered to bump
    `LEGACY_SCORE_CACHE_SCHEMA`. (0_reading sidecars carry this same field
    too, even though neither constant is read on that branch -- a bump on
    either still safely over-invalidates a reading cache entry rather than
    risk under-invalidating a correction one; there is exactly one predicate,
    not a per-stage one.)
    """
    import src.agent.correction.deterministic as deterministic_module
    import src.agent.judge.correction_score as correction_score_module

    return {
        "core_stamp_version": deterministic_module.DETERMINISTIC_CORE_STAMP_VERSION,
        "output_convention": correction_score_module.CORRECTION_OUTPUT_CONVENTION,
        # F-24 follow-up (2026-08-13): constants alone do not identify the
        # implementation that consumes them.  Derive this from the current
        # scorer code, so replacing an implementation changes the cache
        # predicate without a separately maintained version declaration.
        "scorer_implementation_sha256": _scorer_implementation_sha256(),
    }


def _scorer_implementation_sha256() -> str:
    """Hash the live legacy scoring implementation, not a declared version.

    The three functions are the legacy scoring dispatch, its evaluator, and
    the correction scorer that decides trusted output convention.  Their code
    objects are unwrapped first so observational decorators do not turn a
    cache hit into a miss, while replacing any implementation changes this
    value.  ``marshal`` serializes code objects for an interpreter-local
    identity; a Python/compiler change safely over-invalidates old sidecars.
    """
    import src.agent.judge.correction_score as correction_score_module

    implementations = (
        _score_attempt_output,
        _legacy_score_attempt_output,
        correction_score_module.score_correction_geometry,
    )
    digests: list[str] = []
    for implementation in implementations:
        code = getattr(inspect.unwrap(implementation), "__code__", None)
        if code is None:
            raise TypeError("legacy scorer implementation has no Python code object")
        digests.append(hashlib.sha256(marshal.dumps(code)).hexdigest())
    return hashlib.sha256("\n".join(digests).encode("ascii")).hexdigest()


FLOW_EXIT_OK = 0
FLOW_EXIT_CHECKPOINT = 10
FLOW_EXIT_STOP = 20
FLOW_EXIT_EP_RECORD = 30


# --------------------------------------------------------------------------- #
# path + metadata resolution
# --------------------------------------------------------------------------- #
def _resolve(base_dir: str, case: str, run: str):
    case_dir = Path(base_dir) / case
    run_dir = case_dir / run
    td = case_dir / "case_data" / "testdata_prompt.json"
    if not td.exists():
        td = case_dir / "testdata_prompt.json"
    return case_dir, run_dir, td


def _expected_zone_total(testdata_path: Path) -> int | None:
    if not testdata_path.exists():
        return None
    try:
        data = json.loads(testdata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return expected_zone_total_from_testdata(data) if isinstance(data, dict) else None


# --------------------------------------------------------------------------- #
# run-manifest acquisition (C2 B-M §5.1, CR-02)
# --------------------------------------------------------------------------- #
def _load_manifest_readonly(run_dir: Path):
    """Version-dispatched read-only load for report/replay/judge consumers:
    V1 and V2 both load as their own wire type (a V2 file parsed by the V1
    class would crash on extra=forbid); an absent manifest degrades to an
    empty V1 shell exactly as ``RunManifest.load`` always did."""
    from src.agent.execution.manifest import load_run_manifest

    return load_run_manifest(run_dir) or RunManifest(case=Path(run_dir).name)


def _manifest_for_attempts(
    case_dir: Path,
    run_dir: Path,
    *,
    run_profile: str | None,
    capability_profile: str | None = None,
    context: dict | None = None,
    source: str = "structured_config",
    reading_mode: dict | None = None,
):
    """Version-dispatched manifest for the attempt-creating commands
    (`run` / `flow` / `resample`) — B-M §5.1 + R1-1 (S-2):

    - persisted **V1** run → refuse before ANY write ("grandfather" hard gate:
      a v1 run is read-only for validation/replay/report; every new attempt is
      blocked until an explicit migration);
    - persisted **V2** run → verify + return it (`ensure` re-checks the bound
      view-manifest inputs, raising on drift);
    - **no manifest yet** → new runs are V2-by-default: provision the trusted
      view manifest AND freeze the effective run policy in the SAME
      ``provision_run`` transaction, then atomically mint the run's V2 identity.

    R1-1: the policy freeze is what makes a ``run_config.yaml`` declaration of
    ``regression`` actually take effect on the ``flow``/``run`` SOP path.
    Without it ``_draw_reading``'s resolver returned ``legacy_defaulted`` every
    time (no ``_run/run_policy.json``), so the declared strict tier was silently
    discarded for the CLI ``--run-profile`` default (``exploratory``).

    M-1 (r3 batchC dispatch §2 MAJOR): ``reading_mode``, when the caller has a
    declared ``reading_mode:`` section (``run_config.reading_mode``), is
    frozen HERE — the same choke point EVERY attempt-creating command
    (`run`/`flow`/`resample`) passes through before any stage attempt is drawn
    or validated, mirroring exactly how ``provision_run`` above already
    freezes ``run_profile``. Before this, the ONLY writer was
    ``record_baseline()``'s own ``provision_reading_mode`` call, invoked at
    RECORD time — meaning a run could execute 0_reading as ``controlled``
    (a reading-agent genuinely present), then have ``run_config.yaml`` edited
    to ``autonomous`` before ``flow --record``, and the record would freeze
    from the EDITED, post-execution declaration with nothing to compare it
    against (sol 2026-08-04 review §4, "LATE_FREEZE_PROBE"). Freezing here
    instead means: the run's SECOND ``flow`` invocation (the one that edits
    the declaration and then records) calls this SAME function again before
    doing anything else, sees the reading_mode already frozen from the FIRST
    invocation's declaration, and ``provision_reading_mode`` raises
    ``reading_mode_drift`` immediately — the tampering is caught before
    ``record_baseline`` is ever reached, not papered over there.
    Opportunistic (skipped entirely when ``reading_mode is None``): an
    undeclared reading_mode is still legal right up until a caller demands
    ``require_reading_mode=True`` (``record_baseline``'s own fail-closed
    check, unchanged) — this does not newly require every run to declare a
    lane, it only ensures that WHEN one is declared, it cannot be silently
    swapped after reading has already executed under the original one.
    """
    from src.agent.execution.manifest import (
        ensure_run_manifest_v2,
        load_run_manifest,
        reading_attempt_allowed,
    )
    from src.agent.execution.run_provision import provision_run

    allowed, refusal_reason = reading_attempt_allowed(run_dir)
    if not allowed:
        raise SystemExit(
            f"✗ {refusal_reason}; invoke explicitly: "
            "run_stage.py provision <case> <run> --migrate"
        )
    existing = load_run_manifest(run_dir)
    # `reading_attempt_allowed()` is the policy authority. This defensive
    # branch keeps the version-dispatched writer fail-closed if a future policy
    # change ever makes the helper more permissive than its current V1 rule.
    if existing is not None and not isinstance(existing, RunManifestV2):
        raise SystemExit(
            "✗ run manifest is not a writable V2 manifest; migrate explicitly: "
            "run_stage.py provision <case> <run> --migrate"
        )
    vm = provision_run(
        case_dir,
        run_dir,
        run_profile=run_profile,
        capability_profile=capability_profile,
        context=context,  # J-1 §1.2: non-hash audit snapshot (wired by callers)
        source=source,  # r2-2: real origin of the frozen pair
    )
    if reading_mode is not None:
        # M-1: freeze (or drift-check against the already-frozen record) HERE,
        # at the same attempt-creating choke point as the run_policy freeze
        # above — never at record time, which is too late to catch a
        # declaration edited after reading already executed. SystemExit (not
        # a bare ValueError) to match this function's existing fail-closed
        # idiom (the V1-grandfather refusal above) — a CLI command's refusal
        # is a controlled exit with a message, not an uncaught exception.
        from src.agent.execution.reading_mode import provision_reading_mode

        try:
            provision_reading_mode(run_dir, declared=reading_mode)
        except ValueError as exc:
            raise SystemExit(f"✗ {exc}") from exc
    return ensure_run_manifest_v2(run_dir, view_manifest_sha256=vm.content_sha256)


# --------------------------------------------------------------------------- #
# stage executors — each returns (output_obj, gate①_report)
# --------------------------------------------------------------------------- #
def _draw_reading(run_dir: Path, policy: RunPolicy, dimensioned_views: set[str]):
    """0_reading is MANUAL: validate the already-produced view JSONs (no LLM).

    Gate① now also runs `reading.view_manifest_coverage` (C2 B-M §6, INVARIANT,
    always BLOCK): the run's trusted view manifest is auto-provisioned here (the
    "0_reading preflight", §4.4 — idempotent, raises only on a genuine case_data
    change mid-run) and every produced `*_view.json` is checked against its
    `expected_output_id` set — a required view with no matching artifact is a
    miss, an artifact outside the expected set is an identity error, both BLOCK
    regardless of run_profile.
    """
    from src.agent.execution.view_manifest import (provision_view_manifest,
                                                    resolve_frozen_reading_exam_scope)
    from src.validator.checks.view_manifest import check_reading_stage

    rdir = run_dir / "0_reading"
    case_dir = run_dir.parent
    views = sorted(rdir.glob("*_view.json"))

    manifest = None
    manifest_missing_reason = "view manifest missing or unreadable"
    try:
        manifest = provision_view_manifest(case_dir, run_dir)
    except ValueError as exc:
        manifest_missing_reason = f"could not provision view manifest: {exc}"

    # F-74: a run that declares `reading_exam_scope` in run_config.yaml has that
    # subset frozen into `_run/`, and the isolation merge path already honours
    # it -- but this flat-flow path never passed it, so the SAME declaration
    # produced a pass through one entry point and an always-BLOCK through the
    # other. Scope drift is surfaced as a manifest failure rather than silently
    # falling back to the full manifest.
    exam_scope = None
    if manifest is not None:
        try:
            exam_scope = resolve_frozen_reading_exam_scope(run_dir, manifest)
        except ValueError as exc:
            manifest, manifest_missing_reason = None, f"reading exam scope drift: {exc}"

    out: dict = {}
    for vj in views:
        out[vj.stem] = json.loads(vj.read_text(encoding="utf-8"))

    # S-2 (G-3): a frozen run_policy.json is the authoritative gate① disposition
    # (same resolver as isolation's build/merge — the flat-flow path no longer
    # silently assembles rectangular/exploratory defaults). A legacy run without
    # one keeps its CLI policy and stamps no hash (G-6: legacy is read-only and
    # never impersonates a strict tier).
    from src.agent.execution.run_policy_freeze import resolve_frozen_run_policy
    policy_record = resolve_frozen_run_policy(run_dir)
    if policy_record.legacy_defaulted:
        eff_capability = policy.capability_profile
        eff_run_profile = policy.run_profile
        policy_sha256 = None
        policy_source = None
    else:
        eff_capability = policy_record.capability_profile
        eff_run_profile = policy_record.run_profile
        policy_sha256 = policy_record.policy_hash
        policy_source = policy_record.source
    rep = check_reading_stage(
        manifest,
        out,
        exam_scope=exam_scope,
        dimensioned_stems=dimensioned_views,
        manifest_missing_reason=manifest_missing_reason,
        capability_profile=eff_capability,
        run_profile=eff_run_profile,
        run_policy_sha256=policy_sha256,
        run_policy_source=policy_source,
    )
    if not views:
        rep.add("reading.present", CheckStatus.ERROR, CheckLayer.INVARIANT,
                message="no 0_reading/*_view.json found — produce reading first")
        return {}, rep
    return out, rep


def _w1_route_correction(rdir: Path, run_dir: Path) -> str | None:
    """W-1 T2-① (ratified 2026-09-07i): pick the 1_correction leg by the
    CLASSIFIER's verdict over the on-disk 0_reading products, ⛔ never by
    file name.  Returns "legacy" | "as_drawn", or exits LOUDLY on every
    shape the ratification table names (mixed / missing-elevation / unknown
    contract / missing product) — a silent fallback to the legacy leg would
    keep the new leg's outages invisible forever.

    No frozen view manifest (a pre-manifest standalone run) → the legacy leg,
    which is those runs' EXISTING behavior — the as_drawn leg is structurally
    unreachable without the manifest (floor_ref IS its storey order), so this
    is not a fallback, it is the only leg that run shape has ever had.
    """
    from src.agent.execution.run_meta import run_meta_path
    from src.agent.execution.view_manifest import ViewManifest
    from src.agent.reading.vector_contract import classify_vector_json

    mpath = run_meta_path(run_dir, "view_manifest.json")
    if not mpath.exists():
        return "legacy"
    manifest = ViewManifest.model_validate_json(mpath.read_text("utf-8"))
    contracts: dict[str, str] = {}
    for entry in manifest.required_entries():
        path = rdir / f"{entry.expected_output_id}.json"
        if not path.exists():
            raise SystemExit(
                f"CORRECTION_PRODUCT_MISSING: manifest entry {entry.input_id} "
                f"has no product at {path}"
            )
        decision = classify_vector_json(json.loads(path.read_text("utf-8")))
        contracts[entry.input_id] = decision.contract_id
    as_drawn = {"as_drawn_plan", "as_drawn_elevation_v0"}
    got = set(contracts.values())
    if got <= {"reading_view_legacy"}:
        return "legacy"
    if not got <= as_drawn | {"reading_view_legacy"}:
        # unknown / unwired contract — loud, with the classifier's verdicts
        raise SystemExit(
            "CORRECTION_MIXED_CONTRACTS: unknown/unwired contract in "
            f"0_reading: {sorted(contracts.items())}"
        )
    plan_ids = {
        e.input_id for e in manifest.required_entries() if e.view_type == "plan"
    }
    elevation_ids = {
        e.input_id
        for e in manifest.required_entries()
        if e.view_type == "elevation"
    }
    plan_contracts = {contracts[i] for i in plan_ids}
    elevation_contracts = {contracts[i] for i in elevation_ids}
    # the RATIFIED table's specific shape first: as_drawn plans with ZERO
    # as_drawn elevations has no ladder source at all — that is the more
    # precise answer than "mix" for why this run cannot take the new leg
    if plan_contracts == {"as_drawn_plan"} and (
        "as_drawn_elevation_v0" not in elevation_contracts
    ):
        raise SystemExit(
            "ELEVATION_EVIDENCE_MISSING: as_drawn plan products with ZERO "
            "as_drawn elevation products — the storey ladder has no source; "
            "⛔ never a silent fall back to the legacy leg"
        )
    # every remaining mix (a legacy product beside as_drawn ones, an as_drawn
    # product at the wrong slot's kind, ...) is CORRECTION_MIXED_CONTRACTS
    if plan_contracts != {"as_drawn_plan"} or elevation_contracts != {
        "as_drawn_elevation_v0"
    }:
        raise SystemExit(
            "CORRECTION_MIXED_CONTRACTS: legacy/as_drawn mix or a product at "
            f"the wrong slot kind in 0_reading: {sorted(contracts.items())}"
        )
    return "as_drawn"


def _w1_cross_check_elevation_ladders(
    elevation_entries, rdir: Path
):
    """T2-③ (ratified strict version): every elevation product adapts and
    derives its own storey ladder; the z sequences must AGREE.

    ⭐ 丁 (ruling 2026-09-07x §一, resolved the S4 stop-report): the z the
    ladders carry is each drawing's own DECLARED tick (calibration.z.cum_mm,
    ink-recognition unchanged, uniqueness PROVEN against the product's own
    noise bound) — so this STRICT text (tuple equality, ⛔ zero threshold)
    now compares declared INTEGERS, and the four real sm25 facades agree by
    construction (3600/7200 identical in all four chains, closure 0.0).
    The pixel-side scatter the stop-report measured (1.0–13.5 mm) did not
    vanish; it is a readout on each level (ink_snap_residual_mm) and stays
    out of the gate.  The flipped lock is
    tests/test_w1_flow_routing.py::test_real_products_now_agree_strictly_on_declared_ticks.
    """
    from src.agent.correction.evidence_adapters import adapt_as_drawn_elevation
    from src.agent.correction.multifloor import derive_floor_ladder

    elevation_evidence = None
    z_reference = None
    z_by_face: dict = {}
    for entry in elevation_entries:
        raw = (rdir / f"{entry.expected_output_id}.json").read_bytes()
        doc = json.loads(raw.decode("utf-8"))
        facade_label = doc.get("facade_label") if isinstance(doc, dict) else None
        artifact = adapt_as_drawn_elevation(
            raw,
            input_id=entry.input_id,
            facade_ref=(
                facade_label
                if isinstance(facade_label, str) and facade_label
                else entry.input_id
            ),
        )
        z_seq = tuple(
            (round(level.z_floor_m, 6), round(level.ceiling_height_m, 6))
            for level in derive_floor_ladder(artifact)
        )
        if not z_seq:
            raise SystemExit(
                f"ELEVATION_LADDER_EMPTY: {entry.input_id} derived zero storeys"
            )
        z_by_face[entry.input_id] = z_seq
        if z_reference is None:
            z_reference, elevation_evidence = z_seq, artifact
        elif z_seq != z_reference:
            raise SystemExit(
                "ELEVATION_LADDER_DISAGREEMENT: elevation products disagree "
                f"on the storey z sequence: {z_by_face!r}"
            )
    return elevation_evidence


def _draw_correction_as_drawn(
    run_dir: Path,
    expected_zones,
    relied,
    policy: RunPolicy,
):
    """The as_drawn evidence-chain leg (W-1 T2-②③⑤, ratified 2026-09-07i).

    Storey order comes from the FROZEN manifest's declared ``floor_ref``
    (⛔ never parsed from file names); the storey ladder is derived from the
    SEALED elevation evidence and CROSS-CHECKED across every elevation
    product (T2-③ strict version); the chains, the footprint snap, the
    channel-split debt ledger, the legal-empty-set window inputs and the
    finalize half are the pieces landed in S1/S2/S3 — this function only
    wires them in the flow's standard (result, report) shape so the SAME
    StageRunner writer archives the attempt.
    """
    from src.agent.correction.chain_provenance import build_chain_provenance, PlanWallOpeningPolicyV1
    from src.agent.correction.finalize import finalize_as_drawn_chain_geometry
    from src.agent.correction.parse import correction_target
    from src.agent.correction.window_sources import (
        build_verified_window_inputs_as_drawn,
    )
    from src.agent.execution.evidence_preflight import load_evidence_debt
    from src.agent.execution.run_meta import run_meta_path
    from src.agent.execution.view_manifest import ViewManifest
    from src.agent.legacy.pipeline import MultiFloorPlanRun, run_multifloor_correction
    from src.validator.checks.correction import check_correction

    s1 = run_dir / "1_correction"
    s1.mkdir(parents=True, exist_ok=True)
    rdir = run_dir / "0_reading"
    mpath = run_meta_path(run_dir, "view_manifest.json")
    raw_manifest_bytes = mpath.read_bytes()
    manifest = ViewManifest.model_validate_json(mpath.read_text("utf-8"))
    entries = manifest.required_entries()
    plan_entries = sorted(
        (e for e in entries if e.view_type == "plan"), key=lambda e: e.floor_ref
    )
    elevation_entries = [e for e in entries if e.view_type == "elevation"]

    # chain profile: the evidence-chain axis word (exploratory/strict).  The
    # permissive RunProfiles ride the chain's exploratory; the strict side of
    # RunProfile rides "strict" — where the FILED channel-split debt (S1)
    # blocks by ratified design, ⛔ not silently.
    chain_profile = (
        "strict"
        if policy.run_profile not in ("exploratory", "dev")
        else "exploratory"
    )
    plan_runs = [
        MultiFloorPlanRun(
            vector_dir=rdir,
            product_filename=f"{e.expected_output_id}.json",
            out_dir=s1 / f"floor_{e.floor_ref}",
            profile=chain_profile,
        )
        for e in plan_entries
    ]

    # T2-③ strict: every elevation derives its own ladder and the z sequences
    # must agree (see _w1_cross_check_elevation_ladders' stop-report note)
    elevation_evidence = _w1_cross_check_elevation_ladders(
        elevation_entries, rdir
    )

    geom = run_multifloor_correction(
        elevation_evidence,
        plan_runs,
        snap_ledger_path=s1 / "footprint_snap_ledger.json",
        evidence_debt_path=s1 / "evidence_debt.json",
    )
    evidence_debt = load_evidence_debt(s1 / "evidence_debt.json")

    target = correction_target(policy.capability_profile)
    raw_readings = {
        entry.input_id: (rdir / f"{entry.expected_output_id}.json").read_bytes()
        for entry in entries
    }
    # ⭐ 2026-09-08 补窗：把窗填进几何，**在建 marker 之前**。
    # 顺序被两条既有约束夹死（见 as_drawn_windows.populate_as_drawn_windows）：
    # 窗要 facade_segments 才能定归属，而 marker 绑 producer 字节且
    # `_claim_links` 在建 marker 那一刻校验 producer 已有的窗。
    # ⛔ 在此之前这条腿产出的是一栋【没有窗】的楼（projection 的 windows=[] 是硬写的），
    # gt 有洞口而模型没有 ⇒ 洞口通道零分，且无窗围护结构对能耗模型没有物理意义。
    from src.agent.correction.as_drawn_windows import populate_as_drawn_windows
    from src.agent.correction.config import load_core_tolerances as _load_tol
    from src.agent.correction.facade_visibility import (
        VisibilityTolerances as _VisTol,
    )

    _tol = _load_tol()
    geom, window_account = populate_as_drawn_windows(
        geom,
        raw_view_manifest_bytes=raw_manifest_bytes,
        raw_reading_artifacts=raw_readings,
        visibility_tolerances=_VisTol(
            depth_epsilon_m=_tol.facade_visibility_depth_epsilon_m,
            endpoint_epsilon_m=_tol.facade_visibility_endpoint_epsilon_m,
        ),
    )
    # 缺席做成信号，⛔ 不是静默空白（门/立面无对应/平面无立面 三类各自记账）
    (s1 / "as_drawn_window_account.json").write_text(
        json.dumps(window_account.to_payload(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    # W#3 (dispatch 2026-09-08c S-A): freeze each chain run's FINAL wall
    # compilation — filed by the chain itself next to its outcome — into the
    # candidate's provenance carrier.  The StageRunner writer then re-drives
    # the whole chain from these bytes + the marker's embedded products
    # (same strength as the legacy core replay, ⛔ never a leg-skip).
    # ``floor_ref`` here is the SAME derivation the production chain uses for
    # the adapter slot (``pipeline.run_correction_evidence_chain``'s plan
    # branch: the ``<N>f`` token inside the frozen product name) — the
    # manifest's own ``floor_ref`` is an int storey index and would break the
    # carrier's ``StableName`` typing; the run DIRECTORY keeps the manifest
    # int (``floor_1``), which is where the chain filed the compilation.
    import re as _re

    def _chain_floor_ref(product_filename: str) -> str:
        stem = Path(product_filename).stem
        m = _re.search(r"(\d+)\s*f", stem, _re.I)
        return m.group(0).lower() if m else stem

    opening_policy = PlanWallOpeningPolicyV1()
    provenance = build_chain_provenance([
        {
            "input_id": entry.input_id,
            "product_filename": f"{entry.expected_output_id}.json",
            "floor_ref": _chain_floor_ref(f"{entry.expected_output_id}.json"),
            "compilation_bytes": (
                s1 / f"floor_{entry.floor_ref}" / "evidence_chain_compilation.json"
            ).read_bytes(),
            "source_bytes_sha256": hashlib.sha256(
                (rdir / f"{entry.expected_output_id}.json").read_bytes()
            ).hexdigest(),
        }
        for entry in plan_entries
    ], wall_opening_policy=opening_policy,
       endpoint_connection_policy="preserve_endpoint_connections_v1")
    from src.agent.correction.as_drawn_openings import populate_as_drawn_openings

    gap_review_path = run_meta_path(run_dir, "wall_gap_decisions.json")
    if gap_review_path.exists():
        from src.agent.correction.wall_gap_review import load_wall_gap_decisions
        from src.agent.correction.chain_replay import derive_as_drawn_chain_producer
        from src.agent.correction.parse import ensure_corrected_geometry

        decisions = load_wall_gap_decisions(gap_review_path, image_root=_REPO_ROOT)
        rows = [dict(input_id=f.input_id, product_filename=f.product_filename, floor_ref=f.floor_ref,
                     compilation_bytes=f.compilation_bytes, source_bytes_sha256=f.source_bytes_sha256)
                for f in provenance.floors]
        projection_recipe = build_chain_provenance(
            rows, endpoint_connection_policy=provenance.endpoint_connection_policy,
            wall_gap_decisions=decisions)
        seed = build_verified_window_inputs_as_drawn(
            producer_draw=geom, raw_view_manifest_bytes=raw_manifest_bytes, raw_reading_artifacts=raw_readings)
        reviewed = derive_as_drawn_chain_producer(seed, projection_recipe)
        geom = ensure_corrected_geometry(json.loads(reviewed.producer_draw_canonical_bytes))
        provenance = build_chain_provenance(
            rows, endpoint_connection_policy=provenance.endpoint_connection_policy,
            wall_opening_policy=opening_policy, wall_gap_decisions=decisions)

    geom, opening_account = populate_as_drawn_openings(
        geom, raw_view_manifest_bytes=raw_manifest_bytes,
        raw_reading_artifacts=raw_readings,
        raw_wall_compilations={row.input_id: row.compilation_bytes for row in provenance.floors},
        assumed_height_m=opening_policy.assumed_height_m,
        wall_gap_decisions=provenance.wall_gap_decisions,
    )
    (s1 / "as_drawn_opening_account.json").write_text(
        json.dumps(opening_account.to_payload(), indent=2, ensure_ascii=False), encoding="utf-8",
    )
    vwi = build_verified_window_inputs_as_drawn(
        producer_draw=geom,
        raw_view_manifest_bytes=raw_manifest_bytes,
        raw_reading_artifacts=raw_readings,
    )
    result = finalize_as_drawn_chain_geometry(
        geom, verified_window_inputs=vwi, target=target,
        chain_provenance=provenance,
    )
    # gate①: the SAME check_correction the legacy leg uses (expected_zone_
    # total etc. all passed through).  The legacy leg's extra pre-gate
    # ``correction_draw_issues`` layer is deliberately not duplicated here:
    # its z-stack/coverage content is already enforced twice on this leg
    # (``assemble_multifloor_geometry``'s own checks + the invariants inside
    # ``check_correction``).
    rep = check_correction(
        result.geom,
        window_host_proof=result.window_host_claims,
        window_evidence=result.window_evidence_ledger,
        expected_zone_total=expected_zones,
        relied_on_testdata=relied,
        capability_profile=policy.capability_profile,
        run_profile=policy.run_profile,
        evidence_debt=evidence_debt,
        verified_window_inputs=vwi,
    )
    return result, rep


def _draw_correction(
    run_dir: Path,
    testdata_text: str,
    expected_zones,
    relied,
    policy: RunPolicy,
):
    from src.agent.correction.finalize import finalize_correction_draw
    from src.agent.correction.parse import correction_target
    from src.agent.execution.evidence_preflight import load_evidence_debt
    from src.agent.legacy.pipeline import (
        _reading_window_stroke_count,
        _schema_only_correction_validator,
        correction_draw_issues,
        run_correction,
    )
    from src.validator.checks.correction import check_correction

    s1 = run_dir / "1_correction"
    s1.mkdir(parents=True, exist_ok=True)
    rdir = run_dir / "0_reading"
    from src.agent.correction.window_sources import (
        verify_reading_stage_root_against_accepted_attempt,
    )

    verify_reading_stage_root_against_accepted_attempt(run_dir, rdir)
    # W-1 T2-①: route by the classifier BEFORE the legacy body runs — the
    # legacy leg below stays byte-identical for every legacy product set.
    if _w1_route_correction(rdir, run_dir) == "as_drawn":
        return _draw_correction_as_drawn(
            run_dir, expected_zones, relied, policy
        )
    # Inner retry handles ONLY schema/format robustness; semantic draw quality is
    # gate①'s job (so a content-bad draw is counted + filed, not silently re-drawn
    # inside the LLM call, bypassing the per-stage budget — review 2026-06-19 High-2).
    target = correction_target(policy.capability_profile)
    observation_reference_catalog = None
    if target.schema_version == "3":
        # F-7 (2026-08-05): mechanically-derived, from the same `_catalog`
        # exit the enforcement path validates against — see
        # `derive_observation_reference_catalog`'s docstring.
        from src.agent.correction.window_sources import build_observation_reference_catalog_from_run

        observation_reference_catalog = build_observation_reference_catalog_from_run(
            run_dir=run_dir, reading_dir=rdir, required_for_v3=True,
        )
    geom = run_correction(rdir, testdata_text, out_dir=s1, feedback=None,
                          draw_validate=_schema_only_correction_validator,
                          run_profile=policy.run_profile,
                          capability_profile=policy.capability_profile,
                          dimensioned_views=dimensioned_view_names(run_dir.parent), target=target,
                          observation_reference_catalog=observation_reference_catalog)
    evidence_debt = load_evidence_debt(s1 / "evidence_debt.json")

    # Keep flow aligned with pipeline: evidence debt is evaluated before any
    # deterministic mutation, and a BLOCKED draw is still filed as an attempt.
    from src.validator.checks.correction import check_evidence_debt_coverage
    pre_core_debt = check_evidence_debt_coverage(
        geom, evidence_debt, capability_profile=policy.capability_profile,
        run_profile=policy.run_profile,
    )
    # F-3a: early-exit on BLOCKING debt only (the policy-aware set), not on any
    # FAIL. ``evidence_debt_coverage`` is advisory (FLAG) under ``exploratory``,
    # so the old ``any(FAIL)`` test early-exited on a non-blocking flag —
    # returning the raw ``geom`` (NOT a FinalizeResult), which StageRunner then
    # accepted as a ``base_v2`` record despite ``schema_version=="3"``; the next
    # stage's accepted-loader fail-closed two stages later in
    # ``load_verified_accepted_correction`` with an opaque message. A blocked
    # draw is still filed as an attempt here; an advisory debt flows through to
    # ``finalize_correction_draw`` and the final ``check_correction`` report,
    # which re-evaluates ``evidence_debt`` itself — so the advisory is recorded
    # once there, neither lost nor double-counted (``pre_core_debt`` is dropped).
    if pre_core_debt.blocking():
        return geom, pre_core_debt

    # Semantic checks on the PRE-core draw. If bad, THIS draw blocks gate① → the
    # outer loop files it as an append-only attempt, counts it, and blind-resamples.
    # Do not run the deterministic core (it may raise on e.g. duplicate cell ids).
    draw_issues = correction_draw_issues(geom, _reading_window_stroke_count(rdir))
    if draw_issues:
        rep = CheckReport(
            stage="1_correction",
            capability_profile=policy.capability_profile,
            run_profile=policy.run_profile,
        )
        for msg in draw_issues:
            rep.add_fail("correction.draw_quality", CheckLayer.INVARIANT, msg)
        return geom, rep

    # F-7 (2026-08-05): `WindowResolverInputError` carries `.category` — the
    # throw site's own classification of who is at fault, never inferred here
    # from the message text. `model_draw_error` (the correction LLM cited an
    # observation it invented, mis-scoped, or was not entitled to use) is this
    # draw's fault: file it as a gate①-blocked attempt so the outer stochastic
    # loop archives it and blind-resamples, exactly like `correction_draw_issues`
    # above. `input_integrity_error` (reading artifact / manifest / accepted-
    # attempt bytes do not line up) is not this draw's fault — no resample can
    # fix a broken upstream artifact, so it re-raises and hard-crashes the flow
    # as before.
    from src.agent.correction.window_sources import WindowResolverInputError
    from src.agent.correction.window_host import WindowHostResolutionError

    def _window_source_error_report(exc: WindowResolverInputError) -> CheckReport:
        rep = CheckReport(
            stage="1_correction",
            capability_profile=policy.capability_profile,
            run_profile=policy.run_profile,
        )
        rep.add_fail(
            "correction.window_source_reference", CheckLayer.INVARIANT,
            f"window source reference rejected ({exc.code}): {exc.context}",
        )
        return rep

    # F-9 (2026-08-06): `WindowHostResolutionError` was a rejected接线缺口 —
    # a `ValueError` sibling of `WindowResolverInputError` with no
    # `.category`, so it fell through both except clauses below uncaught and
    # hard-crashed the flow. It now carries the same F-7-shaped `.category`
    # (see `WindowHostResolutionError.category`'s docstring for how that's
    # derived); wire it into the identical archive-and-blind-resample routing
    # `WindowResolverInputError` already gets, keyed the same way (never on
    # message text, never defaulted — a non-`model_draw_error` category still
    # re-raises and hard-crashes, exactly like the sibling type above).
    def _window_host_error_report(exc: WindowHostResolutionError) -> CheckReport:
        rep = CheckReport(
            stage="1_correction",
            capability_profile=policy.capability_profile,
            run_profile=policy.run_profile,
        )
        conflict_desc = "; ".join(f"{row.window_id}:{row.reason_code}" for row in exc.conflicts)
        rep.add_fail(
            "correction.window_host_resolution", CheckLayer.INVARIANT,
            f"window host resolution rejected ({len(exc.conflicts)} conflict(s)): {conflict_desc}",
        )
        return rep

    verified_window_inputs = None
    if geom.schema_version == "3":
        from src.agent.correction.window_sources import build_verified_window_inputs_from_run

        try:
            verified_window_inputs = build_verified_window_inputs_from_run(
                producer_draw=geom,
                run_dir=run_dir,
                reading_dir=rdir,
            )
        except WindowResolverInputError as exc:
            if exc.category != "model_draw_error":
                raise
            return geom, _window_source_error_report(exc)
    try:
        result = finalize_correction_draw(
            geom,
            vector_dir=rdir,
            target=target,
            verified_window_inputs=verified_window_inputs,
        )
    except WindowResolverInputError as exc:
        if exc.category != "model_draw_error":
            raise
        return geom, _window_source_error_report(exc)
    except WindowHostResolutionError as exc:
        if exc.category != "model_draw_error":
            raise
        return geom, _window_host_error_report(exc)
    geom = result.geom
    rep = check_correction(geom,
                           window_host_proof=result.window_host_claims,
                           window_evidence=result.window_evidence_ledger,
                           expected_zone_total=expected_zones,
                           relied_on_testdata=relied,
                           capability_profile=policy.capability_profile,
                           run_profile=policy.run_profile,
                           evidence_debt=evidence_debt,
                           # F-9 route② S2 (2026-08-12): non-blocking cross-check
                           # shadow of the same live model-authored `window.span`
                           # path, using the SAME `verified_window_inputs` this
                           # real entry already built above. See
                           # `check_correction`'s `_window_position_evidence_shadow`
                           # for the full contract (never overrides span, never
                           # blocks — CheckLayer.CROSS_CHECK only).
                           verified_window_inputs=verified_window_inputs)
    return result, rep


def _accepted_output_path(run_dir: Path, stage: str) -> Path | None:
    """Path to the manifest-accepted attempt's archived output for a stage, or
    None when the run has no manifest / no accepted attempt (standalone use)."""
    from src.agent.execution.run_meta import run_meta_path

    mpath = run_meta_path(run_dir, "run_manifest.json")
    if not mpath.exists():
        return None
    try:
        m = json.loads(mpath.read_text(encoding="utf-8"))
        rec = (m.get("stages") or {}).get(stage) or {}
        idx = rec.get("accepted_attempt")
        if not idx:
            return None
        p = run_dir / stage / "attempts" / f"{int(idx):03d}" / "output.json"
        return p if p.exists() else None
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def _load_snapped(run_dir: Path):
    from src.agent.correction.parse import ensure_corrected_geometry

    # Manifest-first: stage-root files mirror the LAST draw, which may be a
    # blocked one (a failed resample still overwrote them before its gate①
    # verdict). Downstream stages must consume the ACCEPTED attempt's archived
    # output — 2026-07-08 the sm24 resample fed a blocked draw's geometry into
    # the kernel through this loader.
    p = _accepted_output_path(run_dir, "1_correction") or (
        run_dir / "1_correction" / "correction_geometry_snapped.json"
    )
    if not p.exists():
        raise SystemExit(f"missing {p}; run 1_correction first")
    return ensure_corrected_geometry(json.loads(p.read_text(encoding="utf-8")))


def _load_snapped_with_proof(run_dir: Path):
    """Load accepted correction bytes and its B5 proof as one boundary."""
    from src.agent.correction.parse import ensure_corrected_geometry
    from src.agent.execution.manifest import RunManifestV2, load_run_manifest
    from src.agent.output_coordinates import load_verified_accepted_correction

    manifest = load_run_manifest(run_dir)
    if isinstance(manifest, RunManifestV2) and manifest.accepted("1_correction") is not None:
        verified = load_verified_accepted_correction(run_dir=run_dir, manifest=manifest)
        geom = ensure_corrected_geometry(json.loads(verified.raw_output_bytes.decode("utf-8")))
        return geom, verified.window_host_proof
    return _load_snapped(run_dir), None


def _draw_modelling(run_dir: Path, policy: RunPolicy):
    from src.agent.geometry.specs import building_geometry_dict
    from src.agent.legacy.pipeline import materialize_kernel_geometry
    from src.validator.checks.kernel import check_kernel

    geom, window_host_proof = _load_snapped_with_proof(run_dir)
    s2 = run_dir / "2_modelling"
    s2.mkdir(parents=True, exist_ok=True)
    bg, issues = materialize_kernel_geometry(
        geom,
        s2,
        capability_profile=policy.capability_profile,
        window_host_proof=window_host_proof,
    )
    if bg is None:
        if any(issue.startswith("source.") for issue in issues):
            _render_geometry_viewer(
                run_dir, run_dir.parent, geometry_path=s2 / "building_geometry.json",
            )
        rep = CheckReport(stage="2_modelling")
        rep.add("kernel.build", CheckStatus.ERROR, CheckLayer.INVARIANT,
                message="geometry kernel build failed: " + "; ".join(issues))
        return {}, rep
    # Make the current Stage 2 candidate viewable even if a solver gate fails.
    # This is a viewing artifact, not a grant of human geometry approval.
    _render_geometry_viewer(
        run_dir, run_dir.parent, geometry_path=s2 / "building_geometry.json",
    )
    rep = check_kernel(
        bg,
        window_host_proof=window_host_proof,
        capability_profile=policy.capability_profile,
        interzone_issues=issues,
        run_profile=policy.run_profile,
    )
    return building_geometry_dict(bg), rep


def _draw_split_pairing(run_dir: Path, policy: RunPolicy):
    from src.agent.geometry import build_geometry
    from src.agent.geometry.specs import (
        building_geometry_dict,
        geometry_specs_markdown,
        serialize_geometry,
    )

    geom, window_host_proof = _load_snapped_with_proof(run_dir)
    bg = build_geometry(
        geom,
        capability_profile=policy.capability_profile,
        window_host_proof=window_host_proof,
    )
    zone_specs, surface_specs, fen_specs, _ = serialize_geometry(bg)
    md = geometry_specs_markdown(zone_specs, surface_specs, fen_specs)
    s3 = run_dir / "3_split_pairing"
    s3.mkdir(parents=True, exist_ok=True)
    (s3 / "geometry_specs.md").write_text(md, encoding="utf-8")

    rep = CheckReport(stage="3_split_pairing")
    bg_disk = run_dir / "2_modelling" / "building_geometry.json"
    if bg_disk.exists() and json.loads(bg_disk.read_text()) == building_geometry_dict(bg):
        rep.add_pass("split_pairing.serialize_consistent", CheckLayer.INVARIANT,
                     evidence={"surfaces": len(bg.surfaces), "windows": len(bg.windows)})
    else:
        rep.add_fail("split_pairing.serialize_consistent", CheckLayer.INVARIANT,
                     "serialized specs do not match the committed 2_modelling geometry")
    return md, rep


def _geometry_zone_meta(run_dir: Path, policy: RunPolicy):
    """Rebuild zone_specs + used_constructions + zone_names for 4_mep / 5.

    ``zone_names`` is ORDERED (2026-08-14 dispatch §2.1): it used to be
    ``set(dict.fromkeys(bg.zones))``, which discarded order because its only
    consumer (check_mep) treats it as a membership/coverage set. run_mep now
    ALSO consumes it, to render hvac_specs deterministically — an unordered
    zone list there would make this run_stage code path's rendered text
    diverge from the flow (pipeline.py run_pipeline_artifacts) path's, which
    defeats the point of "deterministic". A plain list still satisfies
    check_mep's existing usage (membership test / sorted() / truthiness —
    checks/mep.py never does set arithmetic on it), so this is the one
    variable serving both call sites; no behavior change for check_mep."""
    from src.agent.geometry import build_geometry
    from src.agent.geometry.specs import serialize_geometry

    geom, window_host_proof = _load_snapped_with_proof(run_dir)
    bg = build_geometry(
        geom,
        capability_profile=policy.capability_profile,
        window_host_proof=window_host_proof,
    )
    zone_specs, surface_specs, fen_specs, used = serialize_geometry(bg)
    zone_names = list(dict.fromkeys(bg.zones))
    return zone_specs, surface_specs, fen_specs, used, zone_names


def _draw_mep(run_dir: Path, testdata_text: str, policy: RunPolicy):
    from src.agent.legacy.pipeline import run_mep
    from src.validator.checks.mep import check_mep

    zone_specs, _, _, used, zone_names = _geometry_zone_meta(run_dir, policy)
    s4 = run_dir / "4_mep"
    s4.mkdir(parents=True, exist_ok=True)
    mep = run_mep(
        zone_specs, used, testdata_text,
        zone_names=zone_names, out_dir=s4, feedback=None,
    )
    rep = check_mep(
        mep.model_dump(),
        used_constructions=used,
        zone_names=zone_names,
        capability_profile=policy.capability_profile,
        run_profile=policy.run_profile,
    )
    return mep, rep


def _ensure_orientation_enriched(run_dir: Path, policy: RunPolicy, manifest):
    """BO-CR1 (E4-oc v2 §8.3 steps 1-3): make the run's accepted correction
    orientation-ready before S5 assembly.

    - accepted `correction_e4_orientation_v1` (or legacy v1/v2): nothing to do;
    - accepted v3 `correction_b2_v1` (a Vg release): run the deterministic
      orientation resolution (content-addressed evidence-set artifact +
      run-config completion mode) + `finalize_orientation_enrichment`, and
      record the result as a NEW accepted 1_correction attempt (release "4"
      via the central map; input hashes bind base/evidence/resolution/config).

    Returns the (possibly refreshed) verified accepted correction."""
    from src.agent.correction.orientation import resolve_orientation_from_run_dir
    from src.agent.execution.stage_runner import StageRunner
    from src.agent.output_coordinates import (
        load_verified_accepted_correction,
        sha256_bytes,
    )

    verified = load_verified_accepted_correction(run_dir=run_dir, manifest=manifest)
    if verified.ref.schema_version != "3" or verified.ref.artifact_contract in (
        "correction_e4_orientation_v1",
        "correction_b5_orientation_v1",
    ):
        return verified

    run_config = load_run_config(run_dir)
    result, artifacts = resolve_orientation_from_run_dir(
        correction_dir=run_dir / "1_correction",
        base=verified,
        completion_mode=run_config.orientation_completion_mode,
        capability_profile=policy.capability_profile,
    )
    enrich_rep = CheckReport(stage="1_correction", capability_profile=policy.capability_profile)
    enrich_rep.add_pass(
        "correction.e4_orientation_enrichment", CheckLayer.INVARIANT,
        evidence={
            "resolution_kind": result.audit_payload["orientation"]["resolution_kind"],
            "north_axis_deg": result.geom.north_axis.value_deg,
            "completion_mode": run_config.orientation_completion_mode,
        },
    )
    StageRunner(run_dir, manifest).record(
        stage="1_correction", stage_dir=run_dir / "1_correction",
        output_obj=result, report=enrich_rep,
        input_hashes={
            "base_correction": verified.ref.output_sha256,
            "orientation_evidence_set": sha256_bytes(artifacts.raw_evidence_set_bytes),
            "orientation_resolution": sha256_bytes(artifacts.raw_resolution_input_bytes),
            "run_config": sha256_bytes(artifacts.raw_run_config_bytes),
        },
    )
    manifest.save(run_dir)
    (run_dir / "1_correction" / "orientation_audit.json").write_text(
        json.dumps(result.audit_payload["orientation"], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return load_verified_accepted_correction(run_dir=run_dir, manifest=manifest)


def _draw_assembly(run_dir: Path, policy: RunPolicy, manifest=None):
    from src.agent._share import ensure_schema_initialized
    from src.agent.correction.orientation import OrientationNeedsInputError
    from src.agent.correction.parse import ensure_corrected_geometry
    from src.agent.execution.manifest import RunManifestV2
    from src.agent.geometry import build_geometry
    from src.agent.geometry.specs import serialize_geometry
    from src.agent.intakeoutput import MepOutput, validate_contract
    from src.agent.output_coordinates import (
        AssemblyE4Write,
        assemble_intake_artifacts,
        build_assembly_coordinate_audit,
        build_output_coordinate_snapshot,
        canonical_json_bytes,
    )
    from src.validator.checks.assembly import check_assembly

    # MepOutput / IntakeOutput embed IDD-backed schemas (e.g. BuildingSchema's
    # terrain validator reads BaseSchema._idf_field). The integrated pipeline inits
    # the IDD; this stepwise driver must do the same before deserializing them.
    ensure_schema_initialized()

    def _err(check_id: str, message: str):
        rep = CheckReport(stage="5_intakeoutput", capability_profile=policy.capability_profile)
        rep.add(check_id, CheckStatus.ERROR, CheckLayer.INVARIANT, message=message)
        return {}, rep

    # BO-CR1: stepwise S5 consumes the ACCEPTED correction through the same
    # E4 chain as integrated — no contract-less assembly path exists.
    if not isinstance(manifest, RunManifestV2):
        return _err("assembly.manifest_v2_required",
                    "5_intakeoutput requires a v2 run manifest (E4 output-coordinate contract)")
    if manifest.accepted("1_correction") is None:
        return _err("assembly.correction_accepted_required",
                    "no accepted 1_correction record; run 1_correction first")
    try:
        verified = _ensure_orientation_enriched(run_dir, policy, manifest)
    except OrientationNeedsInputError as exc:
        return _err("assembly.orientation_needs_input", f"NEEDS_INPUT: {exc}")

    mep_record = manifest.accepted("4_mep")
    if mep_record is None:
        return _err("assembly.mep_accepted_required",
                    "no accepted 4_mep record; run 4_mep first")
    mep_path = _accepted_output_path(run_dir, "4_mep")
    if mep_path is None:
        return _err("assembly.mep_present", "missing accepted 4_mep output; run 4_mep first")
    mep_bytes = mep_path.read_bytes()
    from src.agent.execution.manifest import hash_bytes
    if hash_bytes(mep_bytes) != mep_record.output_hash:
        return _err("assembly.mep_identity",
                    "accepted 4_mep output hash does not match the run manifest")
    mep = MepOutput.model_validate_json(mep_bytes)

    # geometry from the VERIFIED accepted bytes (never a stage-root mirror)
    geom = ensure_corrected_geometry(json.loads(verified.raw_output_bytes.decode("utf-8")))
    bg = build_geometry(
        geom,
        capability_profile=policy.capability_profile,
        window_host_proof=verified.window_host_proof,
    )
    frame_label = "building_axis" if verified.ref.schema_version == "3" else "world"
    zone_specs, surface_specs, fen_specs, used = serialize_geometry(bg, frame_label=frame_label)

    snapshot = build_output_coordinate_snapshot(bg)
    bundle = assemble_intake_artifacts(
        zone_specs=zone_specs, surface_specs=surface_specs, fenestration_specs=fen_specs,
        mep=mep, correction=verified, coordinate_snapshot=snapshot,
    )
    audit = build_assembly_coordinate_audit(
        verified=verified, contract=bundle.output_coordinates,
        snapshot_bytes=canonical_json_bytes(snapshot),
        mep_placeholder_north_axis=float(mep.building.north_axis),
        final_building_north_axis=float(bundle.intake.building.north_axis),
    )
    s5 = run_dir / "5_intakeoutput"
    s5.mkdir(parents=True, exist_ok=True)
    issues = validate_contract(bundle.intake, used)
    if issues:
        (s5 / "contract_issues.json").write_text(
            json.dumps(issues, indent=2, ensure_ascii=False), encoding="utf-8")
    (s5 / "intake_output.json").write_text(
        bundle.intake.model_dump_json(indent=2, by_alias=False), encoding="utf-8")
    rep = check_assembly(bundle.intake, used)
    write = AssemblyE4Write(
        intake=bundle.intake, contract=bundle.output_coordinates,
        snapshot=snapshot, audit=audit,
        input_hashes=(
            ("1_correction", verified.ref.output_sha256),
            ("4_mep", mep_record.output_hash),
        ),
    )
    return write, rep


def _make_draw_fn(stage: str, run_dir: Path, testdata_text: str, td_path: Path, policy: RunPolicy,
                  manifest=None):
    if stage == "0_reading":
        return lambda _fb: _draw_reading(run_dir, policy, dimensioned_view_names(run_dir.parent))
    if stage == "1_correction":
        ez = _expected_zone_total(td_path)
        relied = td_path.exists()
        return lambda _fb: _draw_correction(run_dir, testdata_text, ez, relied, policy)
    if stage == "2_modelling":
        return lambda _fb: _draw_modelling(run_dir, policy)
    if stage == "3_split_pairing":
        return lambda _fb: _draw_split_pairing(run_dir, policy)
    if stage == "4_mep":
        return lambda _fb: _draw_mep(run_dir, testdata_text, policy)
    if stage == "5_intakeoutput":
        return lambda _fb: _draw_assembly(run_dir, policy, manifest)
    raise SystemExit(f"unknown stage '{stage}'; known: {', '.join(_STAGES)}")


# --------------------------------------------------------------------------- #
# O-1: reading render finalization (aggregate views -> per-attempt renders)
# --------------------------------------------------------------------------- #
# Bumped when the reading render contract (render_vector_to_png.render's
# input/output) changes meaningfully. Recorded in render_manifest.json so a
# manifest rendered by an older helper is detectable as stale.
READING_RENDER_HELPER_VERSION = "render_vector_to_png:v1"


def _extract_reading_views(out_obj) -> dict:
    """B-1 (r1): pull the per-image view payloads out of a reading attempt's
    archived output, recognizing BOTH living layouts (the "views extractor"
    seam — do NOT bake ``.get("views")`` into the call site again):

    * isolated-merge aggregate: ``{'views': {<expected_output_id>: <view>}}``
      (what ``merge_isolated_output`` writes — isolation.py).
    * flat / blind-re-read recovery: ``{'<stem>': <view>, ...}`` with NO
      ``views`` wrapper. ``StageRunner`` archives ``_draw_reading``'s
      ``{vj.stem: view}`` dict verbatim (see
      ``window_sources.verify_reading_stage_root_against_accepted_attempt``,
      which rebuilds the stage-root mirrors in the accepted product's OWN
      contract shape — flat here, envelope for the isolated merge — and
      compares a canonical hash), and
      the judge-rubric / this CLI's own blind-re-read protocol both command the
      reader to write the flat ``0_reading/*_view.json`` working copy — so this
      path is alive, not dead code.

    The previous O-1 code recognized ONLY the aggregate (``.get("views")``), so a
    healthy flat-flow run parsed to zero views, rendered nothing, and was
    reverse-blocked at review — the exact regression the cross-review reproduced
    (two PNGs before O-1, zero after). Non-dict payloads are skipped; a payload
    that is not one of these shapes yields ``{}`` (finalized as the distinct,
    non-blocking ``'empty'`` status — NOT a render failure)."""
    if isinstance(out_obj, dict):
        wrapped = out_obj.get("views")
        if isinstance(wrapped, dict):
            return {k: v for k, v in wrapped.items() if isinstance(v, dict)}
        return {k: v for k, v in out_obj.items() if isinstance(v, dict)}
    return {}


def _write_render_failure_manifest(
    attempt_dir: Path, *, reason: str, source_output_hash: str | None = None,
) -> dict:
    """M-1 (r1): drop a machine-readable failure manifest for a reading attempt
    whose renders could not be produced (unreadable/unparseable output, or a
    catastrophic finalize crash). Status is the real-failure ``'unavailable'``
    (which blocks review) with the reason in ``error`` and an empty ``views``
    list — DISTINCT from ``'missing'`` (no manifest = renders never tried / a
    pre-O-1 run), so a tried-but-broken run is never silently masked as a
    pre-O-1 run that approve-review would let through."""
    manifest = {
        "source_output_hash": source_output_hash,
        "render_helper_version": READING_RENDER_HELPER_VERSION,
        "status": "unavailable",
        "views": [],
        "error": reason,
    }
    (attempt_dir / "render_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return manifest


def _finalize_reading_renders(attempt_dir: Path) -> dict:
    """O-1: render the aggregate reading views from ``<attempt_dir>/output.json``
    — the hard-isolated merge product — to ``<attempt_dir>/renders/<eid>.png``
    and write a machine-readable ``render_manifest.json``.

    This replaces the old ``0_reading/*_view.json`` flat glob: under hard
    isolation merge writes the aggregate to ``attempts/NNN/output.json`` and
    leaves nothing at the stage root, so the glob matched zero files and 07-08
    onward every reading run rendered no images (the user saw no product). The
    SAME renderer (``render_vector_to_png.render``) is used — only the source of
    the views changes. Any accepted-stage-root alias is a convenience copy only;
    these per-attempt renders + manifest are the evidence.

    A per-view render failure is RECORDED, never swallowed into 'complete'
    (O-1 L-41): the view is marked ``status='failed'`` with the error and the
    manifest's overall ``status`` flips to ``'unavailable'``. A review-required
    reading run whose manifest is unavailable must not be reported as
    review-complete — ``cmd_approve_review`` blocks it. Returns the manifest
    dict (also written to ``render_manifest.json``)."""
    sys.path.insert(0, str(_REPO_ROOT / "scripts" / "tool_scripts"))
    import render_vector_to_png as rv
    from src.agent.execution.manifest import hash_file, hash_text

    output_path = attempt_dir / "output.json"
    try:
        out_text = output_path.read_text(encoding="utf-8")
        out_obj = json.loads(out_text)
    except (OSError, json.JSONDecodeError) as exc:
        # M-1 (r1): an unreadable/unparseable archived output is a REAL failure
        # (the attempt was tried but its output is unusable). Drop a machine-
        # readable failure manifest with a real-failure status — DISTINCT from
        # 'missing' (no manifest = never tried / pre-O-1) — so the failure is
        # never silently masked as a pre-O-1 run that approve-review would let
        # through. (Previously this raised to _render_stage's stage-level except,
        # wrote NO manifest, and read back as 'missing'.)
        return _write_render_failure_manifest(
            attempt_dir, reason=f"output unreadable: {type(exc).__name__}: {exc}")
    # source_output_hash uses the SAME hasher as merge's output_hash (hash_text),
    # so the manifest is cross-referenceable to the stage record's output_hash.
    source_output_hash = hash_text(out_text)
    views = _extract_reading_views(out_obj)

    renders_dir = attempt_dir / "renders"
    renders_dir.mkdir(parents=True, exist_ok=True)
    view_records: list[dict] = []
    any_failed = False
    for eid in sorted(views):
        record: dict = {"expected_output_id": eid}
        try:
            img = rv.render(views[eid])
            png_path = renders_dir / f"{eid}.png"
            img.save(png_path)
            record["status"] = "rendered"
            record["render_hash"] = hash_file(png_path)
            record["error"] = None
        except Exception as exc:  # noqa: BLE001 — recorded in the manifest, NOT swallowed into success
            any_failed = True
            record["status"] = "failed"
            record["render_hash"] = None
            record["error"] = f"{type(exc).__name__}: {exc}"
        view_records.append(record)

    # B-1 (r1): "no images to render" (a recognized shape with zero views) must
    # NOT share a state with "a view failed to render". Only a REAL render
    # failure blocks review ('unavailable'); a recognized-but-empty output is the
    # distinct, non-blocking 'empty' state, so a healthy run is never
    # reverse-blocked for having nothing to render (the cross-review's empty-vs-
    # failure requirement). 'missing' stays reserved for "no manifest at all".
    if any_failed:
        status = "unavailable"
    elif view_records:
        status = "complete"
    else:
        status = "empty"
    manifest = {
        "source_output_hash": source_output_hash,
        "render_helper_version": READING_RENDER_HELPER_VERSION,
        "status": status,
        "views": view_records,
    }
    (attempt_dir / "render_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return manifest


def _reading_render_status(attempt_dir: Path) -> str:
    """O-1 L-41 / B-1 (r1): the machine-readable review-material status for a
    reading attempt's renders. ``'complete'`` = at least one view rendered, none
    failed; ``'unavailable'`` = a render was attempted but at least one view
    failed (or the manifest is unreadable) — a real failure; ``'empty'`` = the
    output was a recognized shape with zero views (nothing to render — NOT a
    failure); ``'missing'`` = no manifest (renders never produced, e.g. a
    pre-O-1 run). ``cmd_approve_review`` blocks a review-required reading run
    whose status is ``'unavailable'`` so a failed render never masquerades as
    complete review material; ``'empty'``/``'missing'``/``'complete'`` do not
    block."""
    manifest_path = attempt_dir / "render_manifest.json"
    if not manifest_path.exists():
        return "missing"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "unavailable"
    return manifest.get("status", "unavailable")


def _render_reading_attempts(run_dir: Path) -> list[str]:
    """O-1: render every reading attempt that has an aggregate ``output.json``
    (the hard-isolated merge product) via :func:`_finalize_reading_renders`.
    Returns the rendered png paths (accepted attempt first, for the judge
    packet). Idempotent — re-running overwrites renders + manifest."""
    attempts = run_dir / "0_reading" / "attempts"
    if not attempts.exists():
        return []
    order: list[Path] = []
    accepted = _accepted_output_path(run_dir, "0_reading")
    if accepted is not None:
        order.append(accepted.parent)
    for d in sorted(attempts.iterdir()):
        if d.is_dir() and d.name.isdigit() and (d / "output.json").exists() and d not in order:
            order.append(d)
    produced: list[str] = []
    for d in order:
        try:
            _finalize_reading_renders(d)
        except Exception as exc:  # noqa: BLE001 — M-1: keep the loop alive
            # A failure BEYOND the read/parse handled inside _finalize_reading_renders
            # (e.g. cannot even write the manifest) must not kill the whole loop —
            # F-2: the accepted attempt's already-rendered images used to vanish
            # when a later attempt raised. Drop a best-effort failure manifest so
            # this attempt is never silently 'missing', then keep going.
            try:
                _write_render_failure_manifest(
                    d, reason=f"finalize crashed: {type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001 — nothing more we can do (e.g. disk full)
                pass
        renders_dir = d / "renders"
        if renders_dir.exists():
            produced.extend(str(p) for p in sorted(renders_dir.glob("*.png")))
    return produced


# --------------------------------------------------------------------------- #
# visual renders (best-effort) + judge packet
# --------------------------------------------------------------------------- #
def _render_stage(stage: str, run_dir: Path, case_dir: Path) -> list[str]:
    """Render the stage's visual artifacts for the judge / eyeball gate.

    Best-effort at the STAGE level: a catastrophic failure (e.g. unreadable
    output) is logged, never fatal. Per-IMAGE reading render failures are NOT
    swallowed here — :func:`_finalize_reading_renders` records each in
    ``render_manifest.json`` with ``status='failed'`` and flips the manifest's
    overall status to ``'unavailable'``, so a failed render can never masquerade
    as complete review material (O-1 L-41)."""
    sys.path.insert(0, str(_REPO_ROOT / "scripts" / "tool_scripts"))
    produced: list[str] = []

    try:
        if stage == "0_reading":
            # O-1: hard-isolated merge writes the aggregate to
            # attempts/NNN/output.json (NOT 0_reading/*_view.json at the stage
            # root), so the old flat glob matched nothing and rendered zero
            # images 07-08 onward. Read each attempt's aggregate views with the
            # SAME renderer, writing attempts/NNN/renders/ + render_manifest.json.
            produced.extend(_render_reading_attempts(run_dir))
        elif stage == "1_correction":
            import render_corrected_geometry as rc
            # User-stamped artifact set (2026-07-08): grade (via the gt-gated
            # grade artifacts pass) + per-floor zones_<floor>.png only.
            src = _accepted_output_path(run_dir, "1_correction") or (
                run_dir / "1_correction" / "correction_geometry_snapped.json"
            )
            if src.exists():
                data = json.loads(src.read_text(encoding="utf-8"))
                for path in rc.render_all_to_dir(data, run_dir / "1_correction"):
                    produced.append(str(path))
        # 2_modelling / 3_split_pairing geometry is handled by the dedicated
        # offline 3D viewer at the confirmation gate (_render_geometry_viewer),
        # not here — those are not judge stages.
    except Exception as e:  # noqa: BLE001 — stage-level renders are best-effort
        produced.append(f"(render error for {stage}: {type(e).__name__}: {e})")
    return produced


def _render_geometry_viewer(
    run_dir: Path, case_dir: Path, *, geometry_path: Path | None = None,
) -> str | None:
    """Generate the self-contained offline interactive 3D viewer for the geometry
    confirmation gate (backlog #3). Returns its path, or None / an error string.
    The GLB exporter (render_building_3d.py) is kept as a tool but no longer wired
    into the main flow."""
    sys.path.insert(0, str(_REPO_ROOT / "scripts" / "tool_scripts"))
    bg = geometry_path or _accepted_output_path(run_dir, "2_modelling") or (
        run_dir / "2_modelling" / "building_geometry.json"
    )
    if not bg.exists():
        return None
    try:
        import render_geometry_viewer as rgv

        from src.agent.execution.run_policy_freeze import effective_run_policy
        from src.agent.execution.source_checkpoint import inspect_source_checkpoint, save_source_review
        import html as html_module

        state = inspect_source_checkpoint(run_dir, policy=effective_run_policy(run_dir), geometry_path=bg)
        data = {**state["geometry"], "source_model": state["source_model"]}
        roles = {s["id"]: s.get("role") for s in (state["source_model"] or {}).get("spaces", [])}
        viewer = rgv.build_viewer_html(data, title=f"{case_dir.name} / {run_dir.name}", roles=roles)
        status = ("源模型及当前几何检查可供确认；确认后进入下游处理。"
                  if state["approval_ready"] else "当前候选仅供查看：来源或检查尚未通过，不能确认继续。")
        unsupported = len((state["source_model"] or {}).get("unsupported", []))
        banner = (f'<aside style="position:fixed;bottom:0;left:0;right:0;z-index:1000;background:#fff3cd;color:#332b00;padding:8px 16px;font:14px/1.5 system-ui">'
                  f'{status} 未建/未支持项：{unsupported}。版本 <span title="{state["digest"]}">{state["digest"][:12]}</span> '
                  f'<a href="checkpoint.json">检查与版本记录</a> · <a href="source_model.json">源对象与未完成项</a>。图纸完整性与仿真结果仍需独立核对。</aside>')
        viewer = viewer.replace("</body>", banner + "</body>")
        out = save_source_review(run_dir, state, viewer)
        landing = run_dir / "manual_review/geometry_viewer.html"
        landing.write_text('<!doctype html><meta charset="utf-8"><title>源模型查看</title>'
                           f'<p>{status}</p><a href="{html_module.escape(out.relative_to(landing.parent).as_posix())}">打开当前源模型</a>', encoding="utf-8")
        return str(out)
    except Exception as e:  # noqa: BLE001 — viewer is best-effort, never fatal
        return f"(geometry viewer render failed: {type(e).__name__}: {e})"


def _source_review_digest(run_dir: Path) -> str | None:
    from src.agent.execution.source_checkpoint import REVIEW_NAME
    from src.agent.execution.run_meta import run_meta_path

    try:
        return json.loads(run_meta_path(run_dir, REVIEW_NAME).read_bytes())["digest"]
    except (OSError, ValueError, KeyError):
        return None


def _source_images(case_dir: Path) -> list[str]:
    cd = case_dir / "case_data"
    imgs = sorted(cd.glob("*_view.png")) + sorted(cd.glob("*.png"))
    seen, out = set(), []
    for p in imgs:
        if p.name not in seen:
            seen.add(p.name)
            out.append(str(p))
    return out


def _line_match_dict(m) -> dict:
    return {"truth": m.truth, "read": m.read, "delta": m.delta, "status": m.status}


def _piece_dict(piece) -> dict:
    return {
        "kind": piece.kind,
        "span": [round(float(piece.span[0]), 3), round(float(piece.span[1]), 3)],
        "within_tol": bool(piece.within_tol),
    }


def _wall_segment_dict(seg) -> list[float] | None:
    if seg is None:
        return None
    return [
        round(float(seg.coord), 3),
        round(float(seg.start), 3),
        round(float(seg.end), 3),
    ]


def _wall_match_dict(m) -> dict:
    truth_coord = m.truth.coord if m.truth is not None else None
    read_coord = m.read.coord if m.read is not None else None
    return {
        "status": m.status,
        "orientation": m.orientation,
        "truth": round(float(truth_coord), 3) if truth_coord is not None else None,
        "read": round(float(read_coord), 3) if read_coord is not None else None,
        "delta": m.delta,
        "lateral_drift": bool(getattr(m, "lateral_drift", False)),
        "extent_drift": bool(getattr(m, "extent_drift", False)),
        "extent_start_drift": bool(getattr(m, "extent_start_drift", False)),
        "extent_end_drift": bool(getattr(m, "extent_end_drift", False)),
        "product": _wall_segment_dict(m.read),
        "gt": _wall_segment_dict(m.truth),
        "product_intervals": [_wall_segment_dict(seg) for seg in getattr(m, "read_intervals", [])],
        "gt_intervals": [_wall_segment_dict(seg) for seg in getattr(m, "truth_intervals", [])],
        "pieces": [_piece_dict(p) for p in m.pieces],
    }


def _win_match_dict(m) -> dict:
    return {
        "status": m.status,
        "facade": m.facade,
        "truth": list(m.truth) if m.truth else None,
        "read": list(m.read) if m.read else None,
        "product": list(m.read) if m.read else None,
        "gt": list(m.truth) if m.truth else None,
        "product_intervals": [list(span) for span in getattr(m, "read_intervals", [])],
        "gt_intervals": [list(span) for span in getattr(m, "truth_intervals", [])],
        "centre_delta": m.centre_delta,
        "pieces": [_piece_dict(p) for p in m.pieces],
    }


def _elevation_box_dict(box) -> dict | None:
    if box is None:
        return None
    out = {
        "span": [round(float(box.span[0]), 3), round(float(box.span[1]), 3)],
        "z": [round(float(box.z[0]), 3), round(float(box.z[1]), 3)],
        "center": round(float(box.center), 3),
        "width": round(float(box.width), 3),
    }
    if getattr(box, "source_id", None) is not None:
        out["source_id"] = box.source_id
    if getattr(box, "original_span", None) is not None:
        out["source_span"] = [
            round(float(box.original_span[0]), 3),
            round(float(box.original_span[1]), 3),
        ]
    return out


def _elevation_truth_dict(truth) -> dict | None:
    if truth is None:
        return None
    return {
        "id": truth.id,
        "span": [round(float(truth.span[0]), 3), round(float(truth.span[1]), 3)],
        "z": [round(float(truth.z[0]), 3), round(float(truth.z[1]), 3)],
        "center": round(float(truth.center), 3),
        "width": round(float(truth.width), 3),
    }


def _elevation_match_dict(match) -> dict:
    return {
        "status": match.status,
        "facade": match.facade,
        "floor": match.floor,
        "orientation": match.orientation,
        "source_id": match.source_id,
        "truth": _elevation_truth_dict(match.truth),
        "read": _elevation_box_dict(match.read),
        "product_box": _elevation_box_dict(match.read),
        "gt_box": _elevation_truth_dict(match.truth),
        "deltas": match.deltas,
        "overlap_ratio": match.overlap_ratio,
        "overlap_fraction": match.overlap_ratio,
        "gt_coverage": getattr(match, "gt_coverage", None),
        "product_coverage": getattr(match, "product_coverage", None),
    }


def _floor_line_match_dict(line) -> dict:
    return {
        "facade": line.facade,
        "gt_z": round(float(line.gt_z), 3),
        "product_z": round(float(line.product_z), 3) if line.product_z is not None else None,
        "status": line.status,
        "delta": round(float(line.delta), 3) if line.delta is not None else None,
    }


def _floor_line_extra_dict(line) -> dict:
    return {
        "facade": line.facade,
        "product_z": round(float(line.product_z), 3),
        "status": line.status,
    }


def _floor_line_score_dict(score) -> dict:
    return {
        "facade": score.facade,
        "gt_floor_lines": [round(float(z), 3) for z in score.gt_floor_lines],
        "product_floor_lines": [round(float(z), 3) for z in score.product_floor_lines],
        "matches": [_floor_line_match_dict(line) for line in score.matches],
        "extras": [_floor_line_extra_dict(line) for line in score.extras],
        "no_data": bool(score.no_data),
        "no_data_reason": score.no_data_reason,
    }


def _boundary_match_dict(match, source_side: str) -> dict:
    if match is None:
        return {"source_boundary": source_side, "status": "no_data", "truth": None, "product": None, "delta": None}
    return {
        "source_boundary": source_side,
        "status": match.status,
        "truth": match.truth,
        "product": match.read,
        "delta": match.delta,
    }


def _elevation_boundary_dict(scores: dict) -> dict:
    mapping = {
        "North": ("W", "E"),
        "South": ("W", "E"),
        "East": ("S", "N"),
        "West": ("S", "N"),
    }
    scores_by_floor = {score.floor: score for score in scores.values()}
    out: dict[str, dict] = {}
    for facade, (left_side, right_side) in mapping.items():
        floors = {}
        for floor_name, score in scores_by_floor.items():
            boundary = score.boundary or {}
            floors[floor_name] = {
                "side_left": _boundary_match_dict(boundary.get(left_side), left_side),
                "side_right": _boundary_match_dict(boundary.get(right_side), right_side),
            }
            if score.boundary is None:
                floors[floor_name]["side_left"]["status"] = "no_data"
                floors[floor_name]["side_right"]["status"] = "no_data"
        out[facade] = {"floors": floors}
    return out


def _elevation_score_dict(result, gt: dict, scores: dict | None = None) -> dict:
    facades = {}
    for facade, by_floor in result.scores.items():
        floors = {}
        for floor, score in by_floor.items():
            placed, gt_total = score.placed_hits()
            matched, _ = score.matched_hits()
            complete = sum(1 for m in score.matches if m.status == "complete")
            within = sum(1 for m in score.matches if m.status == "within_tol")
            floors[floor] = {
                "facade": facade,
                "floor": floor,
                "orientation": score.orientation,
                "no_data": score.no_data,
                "gt_count": score.gt_count,
                "read_count": score.read_count,
                "matched_total": matched,
                "placed_hit_total": placed,
                "complete_total": complete,
                "within_tol_total": within,
                "matches": [_elevation_match_dict(m) for m in score.matches],
                "extras": [_elevation_match_dict(m) for m in score.extras],
            }
        facades[facade] = {
            "orientation": result.orientation_by_facade.get(facade, "aligned"),
            "span_limit_m": (
                float(gt["footprint"]["W_m"])
                if facade in {"North", "South"}
                else float(gt["footprint"]["D_m"])
            ),
            "floors": floors,
        }
    return {
        "summary": result.summary(),
        "facades": facades,
        "floor_lines": {
            facade: _floor_line_score_dict(score)
            for facade, score in getattr(result, "floor_lines", {}).items()
        },
        "boundary": _elevation_boundary_dict(scores or {}),
        "evidence": result.evidence,
    }


def _floor_score_dict(score) -> dict:
    wh, wt = score.wall_hits()
    winh, wint = score.window_hits()
    out = {
        "floor": score.floor,
        "wall_hits": wh,
        "wall_total": wt,
        "window_hits": winh,
        "window_total": wint,
        "max_wall_offset_m": score.max_wall_offset(),
        "vwalls": [_wall_match_dict(m) for m in score.vwalls],
        "hwalls": [_wall_match_dict(m) for m in score.hwalls],
        "extra_vwalls": [m.read.coord for m in score.extra_vwalls if m.read is not None],
        "extra_hwalls": [m.read.coord for m in score.extra_hwalls if m.read is not None],
        "vwall_records": [_wall_match_dict(m) for m in score.vwalls + score.extra_vwalls],
        "hwall_records": [_wall_match_dict(m) for m in score.hwalls + score.extra_hwalls],
        "windows": {
            facade: [_win_match_dict(m) for m in matches]
            for facade, matches in score.windows.items()
        },
        "extra_windows": {
            facade: [list(m.read) for m in matches if m.read is not None]
            for facade, matches in score.extra_windows.items()
        },
        "extra_window_records": {
            facade: [_win_match_dict(m) for m in matches]
            for facade, matches in score.extra_windows.items()
        },
    }
    if score.boundary is not None:
        out["boundary"] = {
            side: _line_match_dict(match)
            for side, match in score.boundary.items()
        }
    return out


def _score_reading_attempt_output(
    output: dict,
    gt: dict,
    *,
    wall_tol: float,
    win_tol: float,
    position_tol: float,
    extent_tol: float,
    complete_eps: float,
):
    from src.agent.judge.reading_score import floor_name_for_image, score_floor

    scores = {}
    evidence: list[dict] = []
    for stem, view in sorted(output.items()):
        if not isinstance(view, dict) or view.get("image_kind") not in (None, "plan"):
            continue
        floor_name = floor_name_for_image(stem, gt)
        if floor_name is None:
            evidence.append(
                {"type": "unmatched_reading_view", "view": stem,
                 "reason": "could not map view stem to gt floor"}
            )
            continue
        scores[stem] = score_floor(
            view,
            gt,
            floor_name,
            wall_tol=wall_tol,
            win_tol=win_tol,
            position_tol=position_tol,
            extent_tol=extent_tol,
            complete_eps=complete_eps,
        )
    return scores, evidence, {}


def _unwrap_reading_views_envelope(output: dict) -> dict:
    """Flatten a ReadingViews v2 envelope at the single legacy scoring seam.

    The legacy plan/elevation scorers consume a flat ``{stem: view}`` mapping in
    which every top-level key is one image stem.  Today's reading product wraps
    that mapping in a ``{"views": {...}}`` envelope (ReadingViews v2).  The typed
    judge path recognizes the envelope via ``identify_reading_contract``; the
    legacy path did not, so the whole envelope collapsed into a single bogus
    ``"views"`` stem and zero floors were scored -- while the headline criteria
    still reported ``pass`` (see ``reading_score_criteria``'s empty-scores guard).

    Normalize here, at the one common ancestor of both legacy consumers
    (``_score_reading_attempt_output`` and ``score_reading_elevation_views``),
    rather than duplicating the unwrap at each consumer.  Flat inputs and
    non-reading products are returned unchanged.
    """
    from src.agent.judge.reading_typed_adapter import identify_reading_contract

    if identify_reading_contract(output).contract_id == "reading_views_v2":
        return output["views"]
    return output


def _as_reading_views_envelope(output: object) -> object:
    """Present a flat ``{stem: view}`` reading product in the v2 envelope.

    Exact mirror of :func:`_unwrap_reading_views_envelope`, which already
    normalizes the other direction for the legacy scorers. Both product layouts
    are alive and deliberately supported (see ``_extract_reading_views``: the
    isolation merge writes the envelope, the flat flow and the blind-re-read
    protocol write ``{stem: view}``), but ``identify_reading_contract`` only
    recognizes the envelope -- so a healthy flat product reached the typed
    scorer as ``unrecognized`` and the capability gate answered
    ``unsupported_reading_contract``, i.e. the AUTHORITATIVE scoring layer
    silently degraded to not_applicable while gate (1) still reported normally.
    Same shape as F-68 (authoritative judging silently skipped) and F-64 (zero
    product does not go red).

    Presentation only: the on-disk bytes are untouched and the product identity
    still hashes ``output.json`` itself, so this cannot launder a wrong artifact
    -- it only stops the scorer declining a shape it already supports elsewhere.
    Non-reading and already-enveloped payloads are returned unchanged.
    """
    from src.agent.judge.reading_typed_adapter import identify_reading_contract

    if not isinstance(output, dict):
        return output
    if identify_reading_contract(output).contract_id == "reading_views_v2":
        return output
    if not output or not all(isinstance(v, dict) for v in output.values()):
        return output
    return {"views": output}


def _resolve_core_proof_with_identity_for_attempt(stage: str, attempt_dir: Path):
    """Resolve a writer-issued proof and its current manifest-bound identity.

    The returned identity is deliberately derived only after the proof bytes
    and accepted record have both been verified.  It is a cache predicate, not
    a claim that files in a writable run directory cannot be edited.

    F-22 BLOCKER-1 round 2 (2026-08-13): the ONLY place a judge-side
    caller obtains a `DeterministicCoreProofV1` for an
    attempt -- `score_correction_geometry` itself deliberately takes no
    run/manifest context (P3 of the dispatch), so this lives here, at the
    one seam that DOES have it.

    Mechanically walks `attempt_dir` -> `stage_dir` -> `run_dir` (dispatch's
    P5: the fixed `<run_dir>/<stage>/attempts/<NNN>/` layout `next_attempt_index`
    / `new_attempt_dir` establish in manifest.py) to locate the SAME
    `run_manifest.json` the writer (`StageRunner.record`) itself wrote the
    proof hash into, then RE-VERIFIES -- never merely trusts -- that:

    1. this exact attempt is the one the manifest currently calls accepted
       (a non-accepted/rejected attempt, e.g. while `_render_all_attempt_grades`
       renders every attempt for review, never gets a proof — only the
       artifact the manifest actually points to can be `trusted`);
    2. its `artifact_contract` is one of the two B5 contracts that actually
       ran the full replay-and-compare gauntlet (a plain `base_v2`/
       `correction_b2_v1` record never had a writer-issued proof at all);
    3. the artifact_hashes-bound proof sidecar is present on disk and its
       bytes still hash to what the manifest recorded at acceptance time.

    Any failure at any step degrades to `None` (no proof) -- fail CLOSED to
    `declared`, NEVER raises: a missing/stale/malformed/tampered proof must
    never crash grading, only withhold trust. `score_correction_geometry`
    independently re-verifies the returned proof's `core_projection_hash`
    against the geometry actually being scored (this function does not
    parse or trust the geometry at all), so returning a proof here is not
    itself sufficient for trust either.
    """
    if stage != "1_correction":
        return None
    from src.agent.execution.manifest import (
        RunManifestV2, StageRecordV2, attempt_index_of, hash_bytes, hash_obj,
    )
    from src.agent.correction.deterministic import DeterministicCoreProofV1

    try:
        attempt_dir = Path(attempt_dir)
        if attempt_dir.parent.name != "attempts":
            return None
        run_dir = attempt_dir.parents[2]
        if attempt_dir.parents[1].name != stage:
            return None
        attempt = attempt_index_of(attempt_dir)
        manifest = _load_manifest_readonly(run_dir)
        if not isinstance(manifest, RunManifestV2):
            return None
        rec = manifest.accepted(stage)
        if rec is None or not isinstance(rec, StageRecordV2) or rec.accepted_attempt != attempt:
            return None
        if rec.artifact_contract not in {"correction_b5_v1", "correction_b5_orientation_v1"}:
            return None
        expected_hash = rec.artifact_hashes.get("deterministic_core_proof")
        if not expected_hash:
            return None
        proof_path = attempt_dir / "deterministic_core_proof.json"
        proof_bytes = proof_path.read_bytes()
        if hash_bytes(proof_bytes) != expected_hash:
            return None
        proof = DeterministicCoreProofV1.model_validate_json(proof_bytes)
        return proof, {
            "proof_bytes_sha256": hash_bytes(proof_bytes),
            "accepted_record_sha256": hash_obj(rec.model_dump(mode="json")),
        }
    except Exception:
        return None


def _resolve_core_proof_for_attempt(stage: str, attempt_dir: Path):
    """Compatibility wrapper returning only the verified proof object."""
    resolved = _resolve_core_proof_with_identity_for_attempt(stage, attempt_dir)
    return None if resolved is None else resolved[0]


def _legacy_score_attempt_output(
    stage: str,
    output: dict,
    gt: dict,
    *,
    grade: GradeConfig,
    core_proof=None,
):
    from src.agent.judge.correction_score import score_correction_geometry
    from src.agent.judge.elevation_score import score_reading_elevation_views
    from src.agent.judge.score_policy import reading_score_criteria

    wall_tol = grade.wall_tol_m
    win_tol = grade.window_centre_tol_m
    elevation = None
    # F-22 BLOCKER-1: correction-stage provenance for the boundary/interior-
    # wall-extent output convention this run assumed; 0_reading has no such
    # concept (reading strokes are never wall-thickness-expanded) so it stays
    # None there.
    output_convention: dict | None = None
    if stage == "0_reading":
        # F-1a: consume today's ReadingViews v2 envelope exactly once, at the
        # single seam that feeds both legacy plan and elevation scorers.  The
        # correction branch is intentionally left untouched.
        output = _unwrap_reading_views_envelope(output)
        scores, evidence, floor_map = _score_reading_attempt_output(
            output,
            gt,
            wall_tol=wall_tol,
            win_tol=win_tol,
            position_tol=grade.position_tol_m,
            extent_tol=grade.extent_tol_m,
            complete_eps=grade.complete_eps_m,
        )
        elevation = score_reading_elevation_views(
            output,
            gt,
            elevation_along_tol_m=grade.elevation_along_tol_m,
            sill_tol_m=grade.sill_tol_m,
            head_tol_m=grade.head_tol_m,
            width_tol_m=grade.width_tol_m,
            overlap_accept=grade.overlap_accept,
            overlap_complete=grade.overlap_complete,
            floor_line_tol_m=grade.floor_line_tol_m,
        )
    elif stage == "1_correction":
        result = score_correction_geometry(
            output,
            gt,
            wall_tol=wall_tol,
            win_tol=win_tol,
            position_tol=grade.position_tol_m,
            extent_tol=grade.extent_tol_m,
            complete_eps=grade.complete_eps_m,
            elevation_along_tol_m=grade.elevation_along_tol_m,
            sill_tol_m=grade.sill_tol_m,
            head_tol_m=grade.head_tol_m,
            width_tol_m=grade.width_tol_m,
            overlap_accept=grade.overlap_accept,
            overlap_complete=grade.overlap_complete,
            floor_line_tol_m=grade.floor_line_tol_m,
            core_proof=core_proof,
        )
        scores, evidence, floor_map = result.scores, result.evidence, result.floor_map
        elevation = result.elevation
        output_convention = result.output_convention
    else:
        return None
    return {
        "scores": scores,
        "elevation": elevation,
        "evidence": evidence,
        "floor_map": floor_map,
        "output_convention": output_convention,
        "score_criteria": reading_score_criteria(
            scores,
            wall_tol_m=wall_tol,
            window_centre_tol_m=win_tol,
            elevation=elevation,
            elevation_along_tol_m=grade.elevation_along_tol_m,
            sill_tol_m=grade.sill_tol_m,
            head_tol_m=grade.head_tol_m,
            width_tol_m=grade.width_tol_m,
            overlap_accept=grade.overlap_accept,
            overlap_complete=grade.overlap_complete,
            extra_evidence=evidence,
        ),
    }


def _score_attempt_output(stage: str, output: dict, gt: dict, *, grade: GradeConfig, core_proof=None):
    """Run-stage reaches scoring through the shared judge service seam.

    `core_proof` is bound into a closure rather than threaded through
    `score_service.score_attempt_service`'s own signature -- that seam is
    shared with the unrelated typed C2 dispatch path, and F-22 BLOCKER-1
    round 2 is scoped to the legacy correction scorer only (P4 of the
    2026-08-13 dispatch: `score_correction_geometry` has exactly one
    production call site, right here).
    """
    from src.agent.judge.score_service import score_attempt_service

    def _legacy_evaluator_with_proof(stage_, output_, gt_, *, grade):
        return _legacy_score_attempt_output(stage_, output_, gt_, grade=grade, core_proof=core_proof)

    return score_attempt_service(stage=stage, output=output, gt=gt, grade=grade,
                                 legacy_evaluator=_legacy_evaluator_with_proof)


def _commit_legacy_grade_pair(*, score_path: Path, grade_path: Path, sidecar: dict, grade_png: bytes) -> None:
    """Atomic rollback pair for legacy v2 projection artifacts.

    This is intentionally separate from the v8 strict sidecar committer: v2
    retains its historical projection wire, while still using the same
    temporary/fsync/replace/rollback publication discipline.
    """
    old_score = score_path.read_bytes() if score_path.exists() else None
    old_grade = grade_path.read_bytes() if grade_path.exists() else None
    temporary: list[Path] = []
    def write_temp(path: Path, data: bytes) -> Path:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        tmp = Path(name); temporary.append(tmp)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        return tmp
    try:
        png_temp = write_temp(grade_path, grade_png)
        from PIL import Image
        with Image.open(png_temp) as image:
            image.verify()
        score_temp = write_temp(score_path, json.dumps(sidecar, indent=2, ensure_ascii=False).encode("utf-8"))
        json.loads(score_temp.read_text(encoding="utf-8"))
        os.replace(png_temp, grade_path); temporary.remove(png_temp)
        os.replace(score_temp, score_path); temporary.remove(score_temp)
    except Exception as exc:
        try:
            if old_grade is None:
                grade_path.unlink(missing_ok=True)
            else:
                restore = write_temp(grade_path, old_grade); os.replace(restore, grade_path); temporary.remove(restore)
            if old_score is None:
                score_path.unlink(missing_ok=True)
            else:
                restore = write_temp(score_path, old_score); os.replace(restore, score_path); temporary.remove(restore)
        except Exception:
            pass
        raise RuntimeError("score_atomic_write_failed") from exc
    finally:
        for path in temporary:
            path.unlink(missing_ok=True)


def _load_valid_score_sidecar(
    sidecar: Path,
    *,
    stage: str,
    attempt: int,
    output_hash: str,
    tolerances: dict[str, float],
    scoring_semantics: dict,
    accepted_proof_identity: dict | None,
) -> dict | None:
    if not sidecar.exists():
        return None
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    output_convention = data.get("output_convention")
    cached_claims_trusted = (
        isinstance(output_convention, dict)
        and output_convention.get("trusted") is True
    )
    if (
        data.get("stage") == stage
        and data.get("attempt") == attempt
        and data.get("output_hash") == output_hash
        and data.get("source") == "attempt_output"
        and data.get("scorer_schema") == LEGACY_SCORE_CACHE_SCHEMA
        and data.get("tolerances") == tolerances
        # F-24 (2026-08-13): a cache entry whose `scoring_semantics` no longer
        # matches the CURRENT live core-stamp-version/output-convention
        # identity is stale even if `scorer_schema` happens to match -- see
        # `_current_scoring_semantics_identity`'s docstring.
        and data.get("scoring_semantics") == scoring_semantics
        # Cache reuse is bound to the proof bytes and accepted-record identity
        # verified for THIS call, rather than merely to a prior score's claim.
        and data.get("accepted_proof_identity") == accepted_proof_identity
        # A sidecar may never supply trusted=True by itself.  If the current
        # proof is absent or invalid, force a rescore that records refusal.
        and (not cached_claims_trusted or accepted_proof_identity is not None)
    ):
        return data
    return None


def _judge_gt_artifacts(
    stage: str,
    case: str,
    run_dir: Path,
    attempt_dir: Path,
    gt: dict,
    *,
    manifest: RunManifest | None = None,
    grade: GradeConfig | None = None,
) -> dict:
    from src.agent.execution.manifest import attempt_index_of, hash_text

    if stage not in {"0_reading", "1_correction"}:
        return {"score_vs_gt": None, "grade": None, "score_criteria": []}

    active_manifest = manifest or _load_manifest_readonly(run_dir)
    rec = active_manifest.accepted(stage)
    attempt = attempt_index_of(attempt_dir)
    if rec is None or rec.accepted_attempt != attempt:
        return {"score_vs_gt": None, "grade": None, "score_criteria": []}

    output_path = attempt_dir / "output.json"
    output_text = output_path.read_text(encoding="utf-8")
    if hash_text(output_text) != rec.output_hash:
        raise RuntimeError(
            f"{stage} attempt output hash does not match manifest accepted hash"
        )

    return _grade_attempt_artifacts(
        stage,
        case,
        attempt_dir,
        gt,
        grade=grade or GradeConfig(),
        output_hash=rec.output_hash,
    )


def _grade_attempt_artifacts(
    stage: str,
    case: str,
    attempt_dir: Path,
    gt: dict,
    *,
    grade: GradeConfig,
    output_hash: str | None = None,
) -> dict:
    from src.agent.execution.manifest import attempt_index_of, hash_text

    if stage not in {"0_reading", "1_correction"}:
        return {"score_vs_gt": None, "grade": None, "score_criteria": []}

    output_path = attempt_dir / "output.json"
    if not output_path.exists():
        return {"score_vs_gt": None, "grade": None, "score_criteria": []}

    output_text = output_path.read_text(encoding="utf-8")
    output_hash = output_hash or hash_text(output_text)
    attempt = attempt_index_of(attempt_dir)
    score_path = attempt_dir / "score_vs_gt.json"
    tolerances = grade.as_tolerances()
    # F-24 (2026-08-13): read live, at cache-check time -- see
    # `_current_scoring_semantics_identity`'s docstring for why this cannot
    # be folded into `LEGACY_SCORE_CACHE_SCHEMA` alone.
    scoring_semantics = _current_scoring_semantics_identity()
    # Resolve and verify the CURRENT accepted proof before consulting cache.
    # A stale trusted sidecar is not evidence that this call still has one.
    resolved_proof = _resolve_core_proof_with_identity_for_attempt(stage, attempt_dir)
    core_proof, accepted_proof_identity = (
        resolved_proof if resolved_proof is not None else (None, None)
    )
    sidecar = _load_valid_score_sidecar(
        score_path,
        stage=stage,
        attempt=attempt,
        output_hash=output_hash,
        tolerances=tolerances,
        scoring_semantics=scoring_semantics,
        accepted_proof_identity=accepted_proof_identity,
    )
    render_needed = sidecar is None
    if sidecar is None:
        output = json.loads(output_text)
        scored = _score_attempt_output(stage, output, gt, grade=grade, core_proof=core_proof)
        if scored is None:
            return {"score_vs_gt": None, "grade": None, "score_criteria": []}
        sidecar = {
            "stage": stage,
            "attempt": attempt,
            "output_hash": output_hash,
            "source": "attempt_output",
            "scorer_schema": LEGACY_SCORE_CACHE_SCHEMA,
            # F-24 (2026-08-13): the identity the cache predicate above
            # re-checks live on every load -- see
            # `_current_scoring_semantics_identity`.
            "scoring_semantics": scoring_semantics,
            "accepted_proof_identity": accepted_proof_identity,
            "case": case,
            "tolerances": tolerances,
            "scores": {
                key: _floor_score_dict(score)
                for key, score in scored["scores"].items()
            },
            "elevation": _elevation_score_dict(scored["elevation"], gt, scored["scores"])
            if scored.get("elevation") is not None
            else None,
            "floor_map": scored["floor_map"],
            "evidence": scored["evidence"],
            "score_criteria": scored["score_criteria"],
            # F-22 BLOCKER-1: schema/profile provenance for the boundary /
            # interior-wall-extent output convention this run assumed (None
            # for 0_reading, which has no such concept).
            "output_convention": scored.get("output_convention"),
        }
    grade_path = attempt_dir / "grade.png"
    if render_needed or not grade_path.exists():
        sys.path.insert(0, str(_REPO_ROOT / "scripts" / "tool_scripts"))
        import render_grade
        from io import BytesIO
        image = render_grade.render_grade(stage, sidecar, gt)
        buffer = BytesIO(); image.save(buffer, format="PNG")
        _commit_legacy_grade_pair(score_path=score_path, grade_path=grade_path,
                                  sidecar=sidecar, grade_png=buffer.getvalue())

    return {
        "score_vs_gt": str(score_path),
        "grade": str(grade_path),
        "score_criteria": sidecar.get("score_criteria", []),
    }


def _render_all_attempt_grades(
    stage: str,
    case: str,
    run_dir: Path,
    gt: dict,
    *,
    manifest: RunManifest,
    grade: GradeConfig,
) -> dict[int, dict]:
    if stage not in {"0_reading", "1_correction"}:
        return {}
    attempts_dir = run_dir / stage / "attempts"
    if not attempts_dir.exists():
        return {}

    out: dict[int, dict] = {}
    for attempt_dir in sorted(p for p in attempts_dir.iterdir() if p.is_dir()):
        try:
            attempt = int(attempt_dir.name)
        except ValueError:
            continue
        out[attempt] = _grade_attempt_artifacts(
            stage,
            case,
            attempt_dir,
            gt,
            grade=grade,
        )

    rec = manifest.accepted(stage)
    if rec is not None:
        accepted_grade = run_dir / stage / "attempts" / f"{rec.accepted_attempt:03d}" / "grade.png"
        if accepted_grade.exists():
            shutil.copy2(accepted_grade, run_dir / stage / "grade.png")
    return out


def _render_stage_grade_artifacts(
    stage: str,
    case: str,
    run_dir: Path,
    *,
    manifest: RunManifest,
    run_config: RunConfig,
    run_profile: str = "exploratory",
) -> dict[int, dict]:
    if stage not in {"0_reading", "1_correction"}:
        return {}
    from src.agent.judge.gt import gt_path, has_gt, load_gt, load_gt_document
    from src.agent.judge.gt_schema import GroundTruthV3

    if not has_gt(case):
        return {}
    document = load_gt_document(case)
    if isinstance(document, GroundTruthV3):
        return _render_all_typed_attempt_grades(stage, case, run_dir, document,
                                                manifest=manifest, grade=run_config.grade_for(stage),
                                                gt_file=gt_path(case), run_profile=run_profile)
    gt = load_gt(case)
    if gt is None:
        return {}
    return _render_all_attempt_grades(
        stage,
        case,
        run_dir,
        gt,
        manifest=manifest,
        grade=run_config.grade_for(stage),
    )


def _typed_score_input_paths(run_dir: Path) -> tuple[Path, Path, Path | None]:
    """Judge-owned sidecars; base ViewManifest remains execution's emitter."""
    meta = run_dir / "_run"
    return meta / "view_manifest.json", meta / "judge_score_bindings.json", meta / "judge_completeness_overlay.json"


def _grade_as_drawn_reading_branch(case: str, attempt_dir: Path, document, output) -> dict | None:
    """J §2.1: route an attempt to the as-drawn judges BY PRODUCT CONTRACT.

    Returns the graded dict (same keys the typed path returns) when ANY view
    of the flat output declares an as-drawn contract, else ``None`` so the
    caller keeps today's path.  The routing decision itself lives in
    ``flow_wiring`` (one classifier, reused read-only); this function only
    feeds it the case's signed-inputs root and the gt the typed layer already
    loaded — ⛔ no second gt read, ⛔ no filename sniffing here.
    """
    from src.agent.judge.as_drawn.flow_wiring import (
        AS_DRAWN_CONTRACTS, grade_as_drawn_attempt, split_output_by_contract,
    )

    decisions = split_output_by_contract(output)
    if not any(d.contract_id in AS_DRAWN_CONTRACTS for d in decisions.values()):
        return None
    gt_dict = json.loads(document.model_dump_json())   # the loaded gt, as raw
    gt_sources = _REPO_ROOT / "case_tests/test_baseline/gt_sources" / case
    bundle = grade_as_drawn_attempt(output, gt=gt_dict, gt_sources_dir=gt_sources,
                                    attempt_dir=attempt_dir)
    criteria = []
    for view in bundle["views"]:
        scores = view.get("scores") or {}
        if "E1_openings_placed_and_sized_pct" in scores:      # elevation view
            criteria.append({
                "id": f"e1_placed_sized_{view['view_id']}",
                "name": "elevation openings placed and sized",
                "passed": scores["E1_openings_placed_and_sized_pct"] >= 60.0,
                "readout": scores["E1_openings_placed_and_sized_pct"]})
        elif "C1_C2_targets_drawn_pct" in scores:              # plan view
            criteria.append({
                "id": f"c1_c2_drawn_{view['view_id']}",
                "name": "plan targets drawn",
                "passed": scores["C1_C2_targets_drawn_pct"] >= 60.0,
                "readout": scores["C1_C2_targets_drawn_pct"]})
    png = next((view["grade_png"] for view in bundle["views"]
                if view.get("grade_png")), None)
    return {
        "score_vs_gt": str(attempt_dir / "score_vs_gt.json"),
        "grade": png,
        "score_vs_gt_bundle_schema": bundle["schema"],
        "score_criteria": criteria,
    }


def _grade_typed_attempt_artifacts(stage: str, case: str, attempt_dir: Path, document, *,
                                   gt_file: Path, manifest: RunManifest, grade: GradeConfig,
                                   run_profile: str = "exploratory") -> dict:
    """Real v3 run-stage assembler: all scorer policy stays in score_service."""
    from src.agent.execution.manifest import attempt_index_of, hash_text
    from src.agent.execution.view_manifest import ViewManifest, resolve_frozen_reading_exam_scope
    from src.agent.judge.score_config import load_judge_score_config
    from src.agent.judge.score_inputs import (load_completeness_overlay,
                                              load_score_view_bindings,
                                              select_score_view_bindings,
                                              validate_score_view_bindings_against_gt)
    from src.agent.judge.score_schema import (build_product_identity, commit_score_artifacts,
                                               load_cached_score, load_score_gt_identity)
    from src.agent.judge.reading_typed_adapter import identify_reading_contract
    from src.agent.judge.score_service import (
        TopLevelNotApplicableError,
        score_attempt_service,
        score_criteria_for_payload,
        strict_payload_violation_reason,
    )

    output_path = attempt_dir / "output.json"
    if not output_path.exists():
        return {"score_vs_gt": None, "grade": None, "score_criteria": []}
    output_text = output_path.read_text(encoding="utf-8")
    output = json.loads(output_text)
    if stage == "0_reading":
        # ⭐ J wiring (2026-09-06): a view whose DECLARED contract is an
        # as-drawn product grades on the as-drawn judges (denominator +
        # reading_grade for plans, elevation_targets + elevation_grade for
        # facades) — decided by vector_contract's per-file classifier, ⛔ not
        # by filename, ⛔ not by a second classifier.  Every other shape keeps
        # today's typed/legacy path untouched (the old scorer still serves
        # historical runs; new and old coexist, the PRODUCT's contract routes).
        as_drawn = _grade_as_drawn_reading_branch(case, attempt_dir, document, output)
        if as_drawn is not None:
            return as_drawn
        # F-75: accept BOTH living reading product layouts. output_hash below is
        # taken from output_text (the file bytes), so normalizing the in-memory
        # object here changes no identity.
        output = _as_reading_views_envelope(output)
    if stage != "0_reading" and not isinstance(output, dict):
        return {"score_vs_gt": None, "grade": None, "score_criteria": []}
    attempt = attempt_index_of(attempt_dir)
    accepted = manifest.accepted(stage)
    accepted_record = accepted if accepted is not None and accepted.accepted_attempt == attempt else None
    # Official typed correction scoring is defined only for the manifest-
    # accepted B5 six-artifact bundle.  Historical/blocked correction attempts
    # remain auditable on disk but have no official score; reading attempts do
    # not have this restriction and must continue through the scorer.
    if stage == "1_correction" and accepted_record is None:
        return {"score_vs_gt": None, "grade": None, "score_criteria": []}
    output_hash = hash_text(output_text)
    if accepted_record is not None:
        record_output = getattr(accepted_record, "output_hash", None)
        artifact_output = getattr(accepted_record, "artifact_hashes", {}).get("output", record_output)
        if record_output != output_hash or artifact_output != output_hash:
            from src.agent.judge.score_schema import ScoreContractError
            raise ScoreContractError("score_product_identity_invalid", "scoring.input_identity",
                                     context={"reason": "accepted_stage_record_output_mismatch"})
    exam_scope = None
    if stage == "0_reading":
        output_schema = identify_reading_contract(output).contract_id
    else:
        declared_schema = output.get("schema_version")
        output_schema = (
            str(declared_schema)
            if declared_schema is not None
            else "unrecognized"
        )
    product = build_product_identity(stage="reading" if stage == "0_reading" else "correction", attempt=attempt,
        output_sha256=output_hash, output_schema=output_schema, source="attempt_output",
        accepted_stage_record=accepted_record)
    gt_identity, typed_gt = load_score_gt_identity(gt_file)
    if typed_gt is None:
        return {"score_vs_gt": None, "grade": None, "score_criteria": []}
    base_path, bindings_path, overlay_path = _typed_score_input_paths(attempt_dir.parents[2])
    # A typed GT makes these judge sidecars mandatory.  Only cases without a
    # v3 GT may silently have no typed score layer.
    if not base_path.exists() or not bindings_path.exists():
        missing = [
            name
            for name, path in (("view_manifest.json", base_path),
                              ("judge_score_bindings.json", bindings_path))
            if not path.exists()
        ]
        message = (
            "v3 GT is present but required judge sidecar(s) are missing "
            f"({', '.join(missing)}); the v3 scoring layer was skipped"
        )
        if run_profile in {"golden", "regression"}:
            raise RuntimeError(f"{message} under run_profile={run_profile}")
        warnings.warn(message, RuntimeWarning, stacklevel=2)
        return {"score_vs_gt": None, "grade": None, "score_criteria": []}
    base = ViewManifest.model_validate_json(base_path.read_text(encoding="utf-8"))
    bindings = load_score_view_bindings(bindings_path, expected_case_id=document.case,
        expected_gt_content_sha256=gt_identity.content_sha256, expected_case_metadata_sha256=base.case_metadata_sha256,
        expected_base_view_manifest_sha256=base.content_sha256)
    if stage == "0_reading":
        try:
            exam_scope = resolve_frozen_reading_exam_scope(attempt_dir.parents[2], base)
        except ValueError as exc:
            raise RuntimeError(f"reading exam scope verification failed: {exc}") from exc
        if exam_scope is not None:
            bindings = select_score_view_bindings(
                bindings=bindings,
                input_ids=set(exam_scope.input_ids),
            )
    if stage == "0_reading" and exam_scope is not None:
        validate_score_view_bindings_against_gt(
            bindings=bindings,
            base=base,
            gt=typed_gt,
            input_ids=None if exam_scope is None else set(exam_scope.input_ids),
        )
    overlay = load_completeness_overlay(overlay_path if overlay_path.exists() else None,
        expected_case_id=document.case, expected_gt_content_sha256=gt_identity.content_sha256,
        expected_base_view_manifest_sha256=base.content_sha256)
    window_host_proof = None
    if stage == "1_correction" and accepted_record is not None:
        from src.agent.output_coordinates import load_verified_accepted_correction

        verified = load_verified_accepted_correction(
            run_dir=attempt_dir.parents[2], manifest=manifest
        )
        if verified.ref.accepted_attempt != attempt:
            raise RuntimeError("typed correction scorer resolved a different accepted attempt")
        window_host_proof = verified.window_host_proof
    request = {"gt_identity": gt_identity, "gt": typed_gt, "stage": product.stage,
        "product_payload": output, "product_identity": product, "base_view_manifest": base,
        "score_bindings": bindings, "completeness_overlay": overlay,
        "c2_config": load_judge_score_config(_REPO_ROOT / "src/configs/judge_score.yaml"),
        "window_host_proof": window_host_proof, "run_profile": run_profile}
    if exam_scope is not None:
        request["reading_exam_scope_input_ids"] = set(exam_scope.input_ids)
        request["reading_exam_scope_source"] = exam_scope.source
    result = score_attempt_service(typed_request=request)
    score_path, grade_path = attempt_dir / "score_vs_gt.json", attempt_dir / "grade.png"
    cached = load_cached_score(score_path, grade_path=grade_path, expected_identity=result.identity)
    if cached is None:
        commit_score_artifacts(sidecar_path=score_path, grade_path=grade_path,
                               sidecar=result.sidecar, grade_png=result.grade_png)
    # 2026-08-20 — strict profiles also refuse the structural plan-frame zero
    # (legal null `scale_origin` scoring as all-miss); see
    # strict_payload_violation_reason for the commit-then-raise contract.
    strict_violation = strict_payload_violation_reason(result.payload)
    if strict_violation is not None and run_profile in {"golden", "regression"}:
        raise TopLevelNotApplicableError(strict_violation)
    return {"score_vs_gt": str(score_path), "grade": str(grade_path),
            "score_payload_detail": getattr(result.payload, "detail", None),
            "score_criteria": [
                item.model_dump(mode="json")
                for item in score_criteria_for_payload(result.payload)
            ]}


def _render_all_typed_attempt_grades(stage: str, case: str, run_dir: Path, document, *,
                                     manifest: RunManifest, grade: GradeConfig, gt_file: Path,
                                     run_profile: str = "exploratory") -> dict[int, dict]:
    if stage not in {"0_reading", "1_correction"}:
        return {}
    attempts = run_dir / stage / "attempts"
    if not attempts.exists():
        return {}
    result: dict[int, dict] = {}
    for attempt_dir in sorted(path for path in attempts.iterdir() if path.is_dir() and path.name.isdigit()):
        result[int(attempt_dir.name)] = _grade_typed_attempt_artifacts(stage, case, attempt_dir, document,
            gt_file=gt_file, manifest=manifest, grade=grade, run_profile=run_profile)
    return result


def _judge_packet(stage: str, case: str, case_dir: Path, run_dir: Path,
                  attempt_dir: Path, report: CheckReport,
                  *, manifest: RunManifest | None = None,
                  run_config: RunConfig | None = None,
                  run_profile: str = "exploratory") -> dict:
    # gt is judge-only — import it inside the judge path, never at module load.
    from src.agent.judge.gt import gt_path, has_gt, load_gt, load_gt_document
    from src.agent.judge.gt_schema import GroundTruthV3

    reg = rubric_for(stage)
    rubric_id = reg[0] if reg else "none"
    renders = _render_stage(stage, run_dir, case_dir)
    cfg = run_config or RunConfig.defaults(path=run_dir / "run_config.yaml", present=False)
    document = load_gt_document(case) if has_gt(case) else None
    partition_artifacts = {}
    if stage == "1_correction":
        from src.agent.judge.partition_evidence import attempt_partition_evidence

        partition_report = attempt_partition_evidence(
            run_dir, attempt_dir, document=document,
            reference_path=gt_path(case) if document is not None else None,
        )
        partition_path = attempt_dir / "source_partition_evidence.json"
        partition_path.write_text(json.dumps(partition_report, indent=2, ensure_ascii=False), encoding="utf-8")
        partition_artifacts = {
            "source_partition_evidence": str(partition_path),
            "source_partition_criterion": partition_report["criterion"],
        }
    if isinstance(document, GroundTruthV3):
        gt_artifacts = _grade_typed_attempt_artifacts(stage, case, attempt_dir, document,
            gt_file=gt_path(case), manifest=manifest or _load_manifest_readonly(run_dir),
            grade=cfg.grade_for(stage), run_profile=run_profile)
    else:
        gt = load_gt(case) if has_gt(case) else None
        gt_artifacts = (
        _judge_gt_artifacts(
            stage,
            case,
            run_dir,
            attempt_dir,
            gt,
            manifest=manifest,
            grade=cfg.grade_for(stage),
        )
        if gt is not None
        else {"score_vs_gt": None, "grade": None, "score_criteria": []}
        )
    from src.agent.legacy.resources import rubric_relative_path

    pkt = {
        "stage": stage,
        "rubric_id": rubric_id,
        "rubric_doc": rubric_relative_path(stage),
        "accepted_attempt_dir": str(attempt_dir),
        "source_images": _source_images(case_dir),
        "renders": renders,
        "gt_path": str(gt_path(case)) if has_gt(case) else None,
        "score_vs_gt": gt_artifacts["score_vs_gt"],
        "score_payload_detail": gt_artifacts.get("score_payload_detail"),
        "score_criteria": gt_artifacts["score_criteria"],
        "grade": gt_artifacts["grade"],
        **partition_artifacts,
        "gate1": {
            "passed": report.passed,
            "flags": [f"{r.check_id}: {r.message}" for r in report.flagged()],
        },
        "note": "You are gate② judge. View the source images + renders (+ gt), then "
                "write a StageVerdict JSON and submit it with `judge ... --verdict`. "
                "score_criteria is machine-readable gt reconciliation evidence only; "
                "StageVerdict remains the authoritative checklist decision. "
                "Use the reconciliation first, images second; tolerances are relaxed. "
                + ("For J1, read source_partition_evidence before ruling on rooms: "
                   "resolve object-level split/merge and unsupported-boundary findings; "
                   "counts and exterior reference-plane offsets are not a partition verdict. "
                   if stage == "1_correction" else ""),
    }
    (attempt_dir / "judge_packet.json").write_text(
        json.dumps(pkt, indent=2, ensure_ascii=False), encoding="utf-8")
    return pkt


# --------------------------------------------------------------------------- #
# verbs
# --------------------------------------------------------------------------- #
# argparse CLI defaults for the profile flags (must match the add_argument
# defaults in main() — R1-7 uses them to tell "operator passed --run-profile"
# apart from "the argparse default was used").
_RUN_PROFILE_CLI_DEFAULT = "exploratory"
_CAPABILITY_PROFILE_CLI_DEFAULT = "rectangular"


def _resolve_run_profiles(run_config, args) -> tuple[str, str, str]:
    """R1-1 (S-2): ``run_profile`` and ``capability_profile`` follow the SAME
    source rule — the structured ``run_config.yaml`` declaration wins, the CLI
    flag is the fallback. Previously ``run_profile`` came from CLI only
    (``args.run_profile``) while ``capability_profile`` came from config, an
    asymmetry that let a ``run_config.yaml`` declaring ``regression`` silently
    run ``exploratory`` on the standard ``flow``/``run`` SOP path — because the
    ``--run-profile`` argparse default is ``exploratory`` (not None), the
    declared strict tier was discarded the moment the operator did not pass it
    explicitly on the command line. Both knobs now resolve identically, and the
    resolved pair is what freezes into the run policy (see
    :func:`_manifest_for_attempts`).

    R1-7 (派工单 §1.7): a structured config declaration and an EXPLICIT CLI flag
    that DISAGREE is fail-closed, not silent "config wins". The argparse CLI
    defaults (exploratory / rectangular) count as "not passed" — a declared
    regression stays regression unless the operator explicitly passes a
    CONFLICTING ``--run-profile`` (e.g. golden), which is refused so a strict
    declaration cannot be silently overridden by a conflicting CLI flag. Passing
    the default value explicitly is indistinguishable from not passing it, so
    config still wins there (the frozen-declaration-is-authoritative intent of
    R1-1).

    r2-2 (ruling 2026-08-04 §r2-2): also return the ``source`` of the frozen
    pair — ``structured_config`` (both declared in config) / ``cli`` (neither
    declared; CLI flags + argparse defaults are the authority, e.g. a pure
    ``--run-profile`` run) / ``mixed`` (exactly one declared). Previously the
    freeze layer hardcoded ``structured_config`` for every new run, so a pure
    CLI run was mislabeled "from structured config" and the ``source`` field was
    a constant (R1-1b's ``assert source == structured_config`` was vacuous)."""
    cfg_run = run_config.run_profile
    cli_run = getattr(args, "run_profile", None)
    cfg_cap = run_config.capability_profile
    cli_cap = getattr(args, "capability_profile", None)
    if cfg_run is not None and cli_run is not None and cli_run != _RUN_PROFILE_CLI_DEFAULT and cfg_run != cli_run:
        raise ValueError(
            f"run_profile conflict: run_config.yaml declares {cfg_run!r} but CLI "
            f"--run-profile={cli_run!r}; a strict declaration must not be silently "
            f"overridden — remove one source"
        )
    if cfg_cap is not None and cli_cap is not None and cli_cap != _CAPABILITY_PROFILE_CLI_DEFAULT and cfg_cap != cli_cap:
        raise ValueError(
            f"capability_profile conflict: run_config.yaml declares {cfg_cap!r} but CLI "
            f"--capability-profile={cli_cap!r}; remove one source"
        )
    run_profile = cfg_run or cli_run or _RUN_PROFILE_CLI_DEFAULT
    capability_profile = cfg_cap or cli_cap or _CAPABILITY_PROFILE_CLI_DEFAULT
    run_from_cfg = cfg_run is not None
    cap_from_cfg = cfg_cap is not None
    if run_from_cfg and cap_from_cfg:
        source = "structured_config"
    elif run_from_cfg or cap_from_cfg:
        source = "mixed"
    else:
        source = "cli"
    return run_profile, capability_profile, source


def _run_policy_context(args, run_config) -> dict:
    """J-1 §1.2 (orchestrator ruling 2026-08-03): build the NON-hash audit context
    for ``run_policy.json`` — the actual values + sources of the RunPolicy toggles
    that do NOT participate in drift detection
    (``validation_scope`` / ``require_ep`` / ``confirmation_policy`` /
    ``judge_enabled``). Only ``(capability_profile, run_profile)`` are hash-bound
    (they are what gate① consumes); the rest are recorded so an audit can see what
    governed the run even though toggling them never trips a drift refusal.

    Each entry carries ``value`` + ``source`` (``structured_config`` / ``cli`` /
    ``default`` / ``sop``). R1-1 left this as ``context=None`` pending the J-1
    ruling; the ruling adopted "keep the hash narrowed, but wire context for
    real" — the narrowing's justification rests on "the other toggles ARE
    recorded, just not drift-bound", which was never true until now."""
    if run_config.present and run_config.judge_mode is not None:
        judge_mode, judge_source = run_config.judge_mode, "structured_config"
    elif hasattr(args, "judge"):
        judge_mode, judge_source = args.judge, "cli"
    else:
        judge_mode, judge_source = "off", "default"
    return {
        "judge_enabled": {
            "value": judge_mode != "off", "judge_mode": judge_mode, "source": judge_source,
        },
        # flow/run SOP fixes the geometry-confirmation gate at REQUIRED
        "confirmation_policy": {"value": "required", "source": "sop"},
        # run_stage.py has no --intake-from (that is run_full_pipeline's path);
        # the flat-flow SOP is always full-scope here
        "validation_scope": {"value": "full", "source": "default"},
        "require_ep": {
            "value": bool(getattr(args, "with_ep", False)),
            "source": "cli" if hasattr(args, "with_ep") else "default",
        },
    }


def _make_policy(
    *,
    reading_runner_available: bool = False,
    run_profile: str = "exploratory",
    capability_profile: str = "rectangular",
    judge_enabled: bool = True,
    confirmation_policy: ConfirmationPolicy = ConfirmationPolicy.REQUIRED,
    budget_draws: int | None = None,
) -> RunPolicy:
    # dev baseline: judge on, geometry confirmation REQUIRED (blocking human gate)
    policy = RunPolicy(
        confirmation_policy=confirmation_policy,
        judge_enabled=judge_enabled,
        reading_runner_available=reading_runner_available,
        run_profile=run_profile,
        capability_profile=capability_profile,
    )
    if budget_draws is not None:
        # Human-triage affordance: a quarantined stage counts EXISTING attempt
        # dirs against per_stage_draws, so granting more draws after a code fix
        # requires an explicit, provenance-visible budget raise (not attempt
        # deletion — the manifest is append-only history).
        policy.budget.per_stage_draws = int(budget_draws)
    return policy


def _stage_index(stage: str) -> int:
    try:
        return _STAGES.index(stage)
    except ValueError as e:
        raise SystemExit(f"unknown stage '{stage}'; known: {', '.join(_STAGES)}") from e


def _enabled_judge(stage: str, policy: RunPolicy) -> bool:
    reg = rubric_for(stage)
    return bool(reg is not None and reg[1] and policy.judge_enabled)


def _accepted_attempt_dir(run_dir: Path, stage: str, attempt: int) -> Path:
    return run_dir / stage / "attempts" / f"{attempt:03d}"


def _accepted_verdict(run_dir: Path, stage: str, attempt: int) -> StageVerdict | None:
    p = _accepted_attempt_dir(run_dir, stage, attempt) / "judge.json"
    if not p.exists():
        return None
    try:
        return StageVerdict.model_validate_json(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — malformed means not safely advanceable
        return None


def _stage_advance_ready(
    *,
    stage: str,
    manifest: RunManifest,
    run_dir: Path,
    case_dir: Path,
    policy: RunPolicy,
    review_switches: set[str],
) -> bool:
    rec = manifest.accepted(stage)
    if rec is None:
        return False
    if _enabled_judge(stage, policy):
        verdict = _accepted_verdict(run_dir, stage, rec.accepted_attempt)
        if verdict is None or verdict.blocking:
            return False
    if stage in review_switches and not review_is_current(
        run_dir, stage=stage, output_hash=rec.output_hash
    ):
        return False
    if stage == GEOMETRY_CHECKPOINT_STAGE and policy.confirmation_policy == ConfirmationPolicy.REQUIRED:
        if not geometry_is_approved(run_dir, case_dir=case_dir):
            return False
    return True


def _auto_start_stage(
    *,
    manifest: RunManifest,
    run_dir: Path,
    case_dir: Path,
    policy: RunPolicy,
    review_switches: set[str],
    to_stage: str,
) -> str:
    end = _stage_index(to_stage)
    state = load_state(run_dir)
    if state.get("stop_reason"):
        print(f"  pending hint from state: {state['stop_reason']} (manifest remains authoritative)")
    for stage in _STAGES[: end + 1]:
        if not _stage_advance_ready(
            stage=stage,
            manifest=manifest,
            run_dir=run_dir,
            case_dir=case_dir,
            policy=policy,
            review_switches=review_switches,
        ):
            return stage
    return to_stage


def _parse_review_switches(raw: str) -> set[str]:
    mapping = {
        "reading": "0_reading",
        "0_reading": "0_reading",
        "correction": "1_correction",
        "1_correction": "1_correction",
    }
    out: set[str] = set()
    for item in [x.strip() for x in raw.split(",") if x.strip()]:
        if item not in mapping:
            raise SystemExit(
                f"unknown review checkpoint '{item}'; use reading,correction"
            )
        out.add(mapping[item])
    return out


def _print_review_checkpoint(run_dir: Path, stage: str, attempt: int) -> None:
    adir = _accepted_attempt_dir(run_dir, stage, attempt)
    grade = adir / "grade.png"
    score = adir / "score_vs_gt.json"
    print("  human review checkpoint:")
    if grade.exists():
        print(f"     grade: {grade}")
    else:
        print("     grade: not generated yet")
    if score.exists():
        print(f"     score_vs_gt: {score}")
    else:
        print("     score_vs_gt: not generated yet (Batch B 未落地)")
    print("     approve after review:")
    print(f"        run_stage.py approve-review <case> <run> {stage} --actor <you>")


def _resolve_flow_llm_config(args, case_dir: Path, run_dir: Path) -> Path:
    global_cfg = _REPO_ROOT / "src" / "configs" / "llm.yaml"
    if args.llm_config is not None:
        cfg = Path(args.llm_config)
        if not cfg.is_absolute():
            cfg = Path.cwd() / cfg
        if not cfg.is_file():
            raise SystemExit(f"--llm-config not found: {cfg}")
        return cfg
    if os.environ.get("EP_AGENT_LLM_CONFIG"):
        cfg = Path(os.environ["EP_AGENT_LLM_CONFIG"])
        if not cfg.is_file():
            raise SystemExit("EP_AGENT_LLM_CONFIG does not point to a file")
        return cfg
    for cfg in (run_dir / "llm.yaml", case_dir / "llm.yaml", global_cfg):
        if cfg.is_file():
            return cfg
    return global_cfg


def _flow_ep(run_dir: Path, testdata_text: str, args, case_dir: Path) -> int:
    cfg = _resolve_flow_llm_config(args, case_dir, run_dir)
    os.environ["EP_AGENT_LLM_CONFIG"] = str(cfg.resolve())
    print(f"  LLM config: {cfg}")
    epw = Path(args.epw)
    if not epw.is_absolute():
        epw = _REPO_ROOT / epw
    if not epw.exists():
        print(f"✗ EPW not found: {epw}")
        return FLOW_EXIT_EP_RECORD
    intake_path = run_dir / "5_intakeoutput" / "intake_output.json"
    if not intake_path.exists():
        print(f"✗ missing intake output: {intake_path}")
        return FLOW_EXIT_EP_RECORD
    try:
        # E4 (spec §8.2): the flow EP entrance loads through the bundle API so
        # the output-coordinate contract travels with the IntakeOutput into
        # the downstream graph (same gate as integrated/--intake-from paths).
        from src.agent._share import ensure_schema_initialized
        from src.agent.output_coordinates import load_intake_bundle

        ensure_schema_initialized()
        bundle = load_intake_bundle(intake_path, run_dir=run_dir)
        intake = bundle.intake
        initial = AgentState(
            user_input="",
            image_paths=[],
            intake_output=intake,
            reading_vector_dir=None,
            testdata_text=testdata_text,
            pipeline_out_dir=None,
            output_coordinate_contract=bundle.output_coordinates,
            output_coordinate_context=bundle.validation_context,
        )

        def on_event(node: str, update: dict) -> None:
            print(f"  [EP node={node}] keys={list(update.keys()) if update else []}")

        run_downstream_ep(
            initial_state=initial,
            epw=epw,
            output_dir=run_dir / "EP",
            ep_run_subdir="EP_run",
            run_simulate=True,
            thread_id=f"{args.case}/{args.run}",
            on_event=on_event,
        )
    except Exception as e:  # noqa: BLE001 — flow owns EP failure code
        print(f"✗ EP downstream run failed: {type(e).__name__}: {e}")
        return FLOW_EXIT_EP_RECORD
    end = run_dir / "EP" / "EP_run" / "eplusout.end"
    if not end.exists():
        print(f"✗ EP downstream run did not produce {end}")
        return FLOW_EXIT_EP_RECORD
    print(f"  EP complete: {end}")
    return FLOW_EXIT_OK


def _print_outcome(outcome, packet: dict | None = None) -> None:
    print(f"[{outcome.stage}] {outcome.status.value}  "
          f"(attempts={outcome.attempts_used}, accepted={outcome.accepted_attempt})")
    print(f"  → {outcome.message}")
    if outcome.report is not None:
        s = {"passed": outcome.report.passed,
             "block": len(outcome.report.blocking()),
             "flag": len(outcome.report.flagged())}
        print(f"  gate①: {s}")
        for r in outcome.report.blocking():
            print(f"    ⛔ {r.check_id}: {r.message}")
        for r in outcome.report.flagged():
            print(f"    ⚠️  {r.check_id}: {r.message}")
    if packet is not None:
        print(f"  📋 judge packet → {packet['accepted_attempt_dir']}/judge_packet.json")
        print(f"     原图: {packet['source_images']}")
        print(f"     渲染: {packet['renders']}")
        if packet.get("gt_path"):
            print(f"     gt(judge-only): {packet['gt_path']}")


def _print_reread_protocol(args, outcome) -> None:
    target = outcome.route_target or "0_reading"
    date_arg = f" --date {args.date}" if args.date else ""
    print("  ↻ blind re-read protocol:")
    print("     1. Spawn a fresh isolated cold-start sub-agent for 0_reading.")
    print("     2. Give it ONLY case_data/*.png, testdata_prompt.json, and the 0_reading skill.")
    print("        Do NOT give prior strokes, prior attempts, judge commentary, or gt.")
    print("     3. Use original-resolution images / crops and any predeclared model-effort ladder; log the runner config out-of-band.")
    print("     4. Have it write/replace the flat working copy: 0_reading/*_view.json plus reading_summary.md.")
    print("     5. Then record and re-gate that flat copy:")
    print(
        "        python scripts/tool_scripts/run_stage.py"
        f" --base-dir {args.base_dir}{date_arg} resample {args.case} {args.run} {target} --force"
    )


def cmd_run(args) -> int:
    case_dir, run_dir, td_path = _resolve(args.base_dir, args.case, args.run)
    testdata_text = td_path.read_text(encoding="utf-8") if td_path.exists() else ""
    run_config = load_run_config(run_dir)
    run_profile, capability_profile, source = _resolve_run_profiles(run_config, args)
    policy = _make_policy(
        reading_runner_available=args.reading_runner_available,
        run_profile=run_profile,
        capability_profile=capability_profile,
        budget_draws=getattr(args, "budget_draws", None),
    )
    # CR-02: attempt-creating entrance — grandfather V1 refusal + V2-by-default
    # provisioning happen here, BEFORE any attempt/manifest write.
    manifest = _manifest_for_attempts(
        case_dir, run_dir,
        run_profile=run_profile, capability_profile=capability_profile,
        context=_run_policy_context(args, run_config),
        source=source,
        reading_mode=run_config.reading_mode,  # M-1: freeze at the attempt choke point, not at record time
    )
    runner = StageRunner(run_dir, manifest)
    stage = args.stage
    stage_dir = run_dir / stage
    draw_fn = _make_draw_fn(stage, run_dir, testdata_text, td_path, policy, manifest=manifest)

    def _packet_fn(adir: Path, rep: CheckReport) -> dict:
        return _judge_packet(
            stage,
            args.case,
            case_dir,
            run_dir,
            adir,
            rep,
            manifest=manifest,
            run_config=run_config,
            run_profile=policy.run_profile,
        )

    outcome = run_one_stage(
        stage=stage, runner=runner, stage_dir=stage_dir, policy=policy,
        draw_fn=draw_fn, packet_fn=_packet_fn, force_draw=args.force,
        geometry_approved=lambda: geometry_is_approved(run_dir, case_dir=case_dir),
        stage_dir_for=lambda target: run_dir / target,
    )
    manifest.save(run_dir)
    _render_stage(stage, run_dir, case_dir)
    _render_stage_grade_artifacts(
        stage,
        args.case,
        run_dir,
        manifest=manifest,
        run_config=run_config,
        run_profile=policy.run_profile,
    )
    # 4_mep J4 is a disabled judge — record the explicit disabled verdict (not a PASS).
    if outcome.status == StepStatus.DETERMINISTIC_PASS and stage == "4_mep":
        run_judge("4_mep", {}, judge_fn=None, verdict_dir=run_dir / "verdicts")
    update_state(run_dir, outcome, timestamp=args.date or "")
    _print_outcome(outcome, outcome.packet)
    if outcome.status == StepStatus.AWAITING_REREAD:
        _print_reread_protocol(args, outcome)
    # geometry confirmation gate: produce the interactive offline 3D viewer for the
    # human to inspect (orbit / 半透明 / 截面 / explode / measure) before approving.
    if (stage in ("2_modelling", "3_split_pairing")
            and outcome.report is not None and outcome.report.passed):
        vpath = _render_geometry_viewer(run_dir, case_dir)
        if vpath:
            print(f"  🧊 3D viewer (open in a browser to inspect, then "
                  f"`approve-geometry`): {vpath}")
            print(f"  source confirmation version: --digest {_source_review_digest(run_dir)}")
    return 0 if not outcome.terminal_stop else 2


def cmd_resample(args) -> int:
    case_dir, run_dir, _td = _resolve(args.base_dir, args.case, args.run)
    # CR-02 (terra r1 点名): the grandfather refusal must fire BEFORE this
    # command's invalidate()/save() — a persisted-V1 run's manifest bytes are
    # never touched by a refused resample.
    run_config = load_run_config(run_dir)
    run_profile, capability_profile, source = _resolve_run_profiles(run_config, args)
    manifest = _manifest_for_attempts(
        case_dir, run_dir,
        run_profile=run_profile, capability_profile=capability_profile,
        context=_run_policy_context(args, run_config),
        source=source,
        reading_mode=run_config.reading_mode,  # M-1: freeze at the attempt choke point, not at record time
    )
    dropped = invalidate(manifest, args.stage)
    manifest.save(run_dir)
    if dropped:
        print(f"  invalidated downstream accepted pointers: {dropped}")
    args.force = True
    return cmd_run(args)


def cmd_judge(args) -> int:
    case_dir, run_dir, _td = _resolve(args.base_dir, args.case, args.run)
    # CR-06: judge-only replay is a read-only consumer of the trusted view
    # manifest (§4.4). Missing manifest = NOT_APPLICABLE (a run that predates
    # the wire is judged as before); present-but-drifted/corrupt = INVARIANT
    # fail (the inputs this run was judged against are no longer trustworthy).
    # NEVER provisions — provisioning belongs to run provisioning / 0_reading
    # preflight only.
    from src.agent.execution.run_meta import run_meta_path as _rmp
    from src.agent.execution.view_manifest import (
        VIEW_MANIFEST_NAME,
        verify_view_manifest,
    )

    if _rmp(run_dir, VIEW_MANIFEST_NAME).exists():
        verification = verify_view_manifest(case_dir, run_dir)
        if not verification.ok:
            print(f"✗ view manifest INVARIANT fail (judge-only path is read-only): {verification.reason}")
            return 2
    else:
        print("  view manifest: NOT_APPLICABLE (run predates the view-manifest wire)")
    # r2-3 (ruling 2026-08-04 §1.3.2): judge-only replay has NO tier consumer
    # today — submit_verdict / _verdict_outcome read only draw budget +
    # reading_runner_available, never the tier (verified in the r1 light-gate).
    # The tier is therefore not a decision input on this path; we still source it
    # from the frozen record instead of the argparse default so the policy's tier
    # is never a fabricated exploratory/rectangular, and a run with no frozen
    # artifact resolves to a visibly-legacy default (G-6). (Debt D-4: should a
    # future judge-path consumer read the tier, a regression lock must land in
    # the same change.)
    from src.agent.execution.run_policy_freeze import resolve_frozen_run_policy
    _frozen_policy = resolve_frozen_run_policy(run_dir)
    policy = _make_policy(
        reading_runner_available=args.reading_runner_available,
        run_profile=_frozen_policy.run_profile,
        capability_profile=_frozen_policy.capability_profile,
        budget_draws=getattr(args, "budget_draws", None),
    )
    stage = args.stage
    stage_dir = run_dir / stage
    # read-only version-dispatched load: judge/replay stays allowed on a
    # grandfathered V1 run (validation/replay/report are its permitted uses).
    manifest = _load_manifest_readonly(run_dir)
    accepted = manifest.accepted(stage)
    if accepted is None:
        raise SystemExit(f"{stage} has no gate①-accepted attempt; run it first")
    verdict = StageVerdict.model_validate_json(Path(args.verdict).read_text(encoding="utf-8"))
    verdict.stage = stage
    if not verdict.rubric_id:
        reg = rubric_for(stage)
        verdict.rubric_id = reg[0] if reg else "none"
    outcome = submit_verdict(
        stage=stage, stage_dir=stage_dir, attempt_index=accepted.accepted_attempt,
        verdict=verdict, verdict_dir=run_dir / "verdicts",
        policy=policy, stage_dir_for=lambda target: run_dir / target,
    )
    update_state(run_dir, outcome, timestamp=args.date or "")
    _print_outcome(outcome)
    if outcome.status == StepStatus.JUDGE_BLOCK:
        # resample the judge-attributed ROOT stage, which may differ from the judged
        # stage (e.g. a J1 verdict rooted in a stochastic upstream stage).
        target = outcome.route_target or stage
        print(f"  ↻ blind resample: `run_stage.py resample {args.case} {args.run} {target}`")
    elif outcome.status == StepStatus.AWAITING_REREAD:
        _print_reread_protocol(args, outcome)
    return 0 if not outcome.terminal_stop else 2


def cmd_approve_geometry(args) -> int:
    case_dir, run_dir, _td = _resolve(args.base_dir, args.case, args.run)
    appr = approve_geometry(run_dir, actor=args.actor, timestamp=args.date,
                            policy=args.policy, note=args.note or "", case_dir=case_dir,
                            expected_digest=getattr(args, "digest", None))
    if appr is None:
        print("✗ no current, displayed Stage 2 source checkpoint matches --digest; "
              "reopen the source review and resolve blocking findings before confirming")
        return 2
    # reflect approval in the ledger so a pending geometry stop_reason is cleared
    mark_geometry_approved(run_dir, timestamp=args.date or "")
    print(f"✓ geometry approved by {appr.actor} @ {appr.timestamp}")
    print(f"  digest={appr.digest}")
    print(f"  → continue at 3_split_pairing: run_stage.py flow {args.case} {args.run} --target legacy-ep")
    return 0


def cmd_approve_review(args) -> int:
    _case_dir, run_dir, _td = _resolve(args.base_dir, args.case, args.run)
    manifest = _load_manifest_readonly(run_dir)
    rec = manifest.accepted(args.stage)
    if rec is None:
        raise SystemExit(f"{args.stage} has no accepted attempt; run it first")
    # O-1 L-41 / B-1 (r1): a review-required reading run whose renders FAILED
    # must NOT be markable review-complete — the human would be approving with
    # no visual material. Block approve-review with a machine-readable reason
    # ONLY on 'unavailable' (a view was attempted and failed to render — a real
    # failure). 'complete' (all rendered), 'empty' (recognized output with
    # nothing to render — NOT a failure), and 'missing' (pre-O-1 / never
    # rendered) are intentionally NOT blocked, so a healthy or pre-O-1 run stays
    # approvable.
    if args.stage == "0_reading":
        attempt_dir = run_dir / "0_reading" / "attempts" / f"{int(rec.accepted_attempt):03d}"
        if _reading_render_status(attempt_dir) == "unavailable":
            # Surface the machine-readable reason: a per-view render failure vs
            # an unreadable/unusable output (M-1) vs a finalize crash — read from
            # the manifest's top-level 'error' (set only for the non-per-view
            # failures); fall back to the per-view render phrasing otherwise.
            reason = "at least one view failed to render"
            try:
                mf = json.loads((attempt_dir / "render_manifest.json").read_text(encoding="utf-8"))
                if mf.get("error"):
                    reason = mf["error"]
            except (OSError, json.JSONDecodeError):
                pass
            raise SystemExit(
                f"review blocked: 0_reading renders are unavailable for attempt "
                f"{rec.accepted_attempt} (render_manifest.json status=unavailable — "
                f"{reason}). Fix the render failure before approving review; see "
                "that attempt's renders/ + render_manifest.json."
            )
    appr = record_review(
        run_dir,
        stage=args.stage,
        output_hash=rec.output_hash,
        actor=args.actor,
        timestamp=args.date or "",
        note=args.note or "",
    )
    mark_review_approved(run_dir, args.stage, timestamp=args.date or "")
    print(f"✓ review approved by {appr.actor} @ {appr.timestamp}")
    print(f"  stage={appr.stage}")
    print(f"  output_hash={appr.output_hash}")
    return 0


def cmd_flow(args) -> int:
    """Bind the chosen configuration before reading/correction, then restore it."""
    case_dir, run_dir, _ = _resolve(args.base_dir, args.case, args.run)
    config = _resolve_flow_llm_config(args, case_dir, run_dir).resolve()
    if getattr(args, "reading_model", None):
        if getattr(args, "target", "source-bim") not in {"source-bim", "ep"}:
            raise SystemExit("automatic reading requires the shared source-bim/ep target")
        if getattr(args, "llm_config", None) is None:
            raise SystemExit("automatic reading requires explicit --llm-config for correction")
        from omegaconf import OmegaConf
        selected = OmegaConf.to_container(OmegaConf.load(config), resolve=True)
        correction = selected.get("intake_correction", {})
        decision = selected.get("correction_decision", correction)
        for section in (correction, decision):
            if (section.get("provider") != "claude_subscription"
                    or section.get("model_name") not in {"haiku", "sonnet", "claude-haiku-4-5-20251001", "claude-sonnet-4-6"}
                    or section.get("api_key") or section.get("base_url")):
                raise SystemExit("automatic reading currently requires explicit Claude subscription correction sections (Haiku/Sonnet)")
    previous = os.environ.get("EP_AGENT_LLM_CONFIG")
    os.environ["EP_AGENT_LLM_CONFIG"] = str(config)
    try:
        return _cmd_flow_with_config(args)
    finally:
        if previous is None:
            os.environ.pop("EP_AGENT_LLM_CONFIG", None)
        else:
            os.environ["EP_AGENT_LLM_CONFIG"] = previous


def _cmd_flow_with_config(args) -> int:
    branch_target = getattr(args, "target", "source-bim")
    source_target = branch_target in ("source-bim", "ep")
    if not source_target and getattr(args, "enclosure_input", None) is not None:
        raise SystemExit("legacy-ep cannot consume source enclosure declarations; use the source-bim target")
    if source_target:
        if (branch_target == "source-bim" and args.with_ep) or args.record:
            raise SystemExit("source-bim target cannot run EP; source branches cannot write a legacy EP baseline")
        if getattr(args, "bim_out", None) is None:
            raise SystemExit("source-bim/ep target requires --bim-out NEW_DIRECTORY")
        if args.bim_out.exists():
            raise SystemExit("--bim-out must be a new directory; previous outputs are immutable")
        if args.to_stage not in ("1_correction", "5_intakeoutput") or args.from_stage not in ("auto", "0_reading", "1_correction"):
            raise SystemExit("source-bim target runs reading/correction and exports source geometry; legacy stages 2-5 are not used")
        if branch_target == "ep":
            if not all(getattr(args, name, None) for name in ("physics_template", "zone_bindings", "backend_out")):
                raise SystemExit("ep target requires --physics-template, --zone-bindings and --backend-out")
            if args.backend_out.exists() or args.backend_out.resolve() == args.bim_out.resolve():
                raise SystemExit("--backend-out must be a separate new directory")
    case_dir, run_dir, td_path = _resolve(args.base_dir, args.case, args.run)
    testdata_text = td_path.read_text(encoding="utf-8") if td_path.exists() else ""
    run_config = load_run_config(run_dir)
    if source_target and run_config.scope_stages and "1_correction" not in run_config.scope_stages:
        raise SystemExit("source-bim target requires correction, but run_config scope excludes it")
    review_switches = (
        _parse_review_switches(args.review or "")
        if args.review
        else run_config.review_stages()
    )
    judge_mode = run_config.judge_mode if run_config.present else args.judge
    to_stage = args.to_stage
    if run_config.present and args.to_stage == "5_intakeoutput" and run_config.scope_stages:
        to_stage = run_config.scope_stages[-1]
    if source_target:
        to_stage = "1_correction"
    run_profile, capability_profile, source = _resolve_run_profiles(run_config, args)
    policy = _make_policy(
        reading_runner_available=args.reading_runner_available,
        run_profile=run_profile,
        capability_profile=capability_profile,
        judge_enabled=(judge_mode != "off"),
        confirmation_policy=ConfirmationPolicy.REQUIRED,
        budget_draws=getattr(args, "budget_draws", None),
    )
    # CR-02: attempt-creating entrance — same gate as cmd_run (grandfather V1
    # refusal + V2-by-default provisioning), before any stage is touched.
    manifest = _manifest_for_attempts(
        case_dir, run_dir,
        run_profile=run_profile, capability_profile=capability_profile,
        context=_run_policy_context(args, run_config),
        source=source,
        reading_mode=run_config.reading_mode,  # M-1: freeze at the attempt choke point, not at record time
    )
    if getattr(args, "reading_model", None):
        from src.agent.execution.automatic_reading import run_automatic_reading
        from src.agent.execution.manifest import load_run_manifest
        reading = run_automatic_reading(case_dir, run_dir, model=args.reading_model,
                                         timeout_seconds=args.reading_timeout)
        print(f"  automatic reading: {reading['status']}")
        if reading["status"] not in {"accepted", "reused"}:
            print(f"  reading evidence: {run_dir / '_run/automatic_reading.json'}")
            return FLOW_EXIT_CHECKPOINT
        # The isolated merge owns the accepted reading record and its mirrors.
        manifest = load_run_manifest(run_dir)
    start_stage = (
        _auto_start_stage(
            manifest=manifest,
            run_dir=run_dir,
            case_dir=case_dir,
            policy=policy,
            review_switches=review_switches,
            to_stage=to_stage,
        )
        if args.from_stage == "auto"
        else args.from_stage
    )
    start = _stage_index(start_stage)
    end = _stage_index(to_stage)
    if start > end:
        raise SystemExit(f"--from {start_stage} is after --to {to_stage}")

    runner = StageRunner(run_dir, manifest)
    force_next: set[str] = set()
    i = start
    while i <= end:
        stage = _STAGES[i]
        stage_dir = run_dir / stage
        draw_fn = _make_draw_fn(stage, run_dir, testdata_text, td_path, policy, manifest=manifest)

        def _packet_fn(adir: Path, rep: CheckReport, *, _stage=stage) -> dict:
            return _judge_packet(
                _stage,
                args.case,
                case_dir,
                run_dir,
                adir,
                rep,
                manifest=manifest,
                run_config=run_config,
                run_profile=policy.run_profile,
            )

        outcome = run_one_stage(
            stage=stage,
            runner=runner,
            stage_dir=stage_dir,
            policy=policy,
            draw_fn=draw_fn,
            packet_fn=_packet_fn,
            force_draw=stage in force_next,
            geometry_approved=lambda: geometry_is_approved(run_dir, case_dir=case_dir),
            stage_dir_for=lambda target: run_dir / target,
        )
        force_next.discard(stage)
        manifest.save(run_dir)
        _render_stage(stage, run_dir, case_dir)
        _render_stage_grade_artifacts(
            stage,
            args.case,
            run_dir,
            manifest=manifest,
            run_config=run_config,
            run_profile=policy.run_profile,
        )
        if outcome.status == StepStatus.DETERMINISTIC_PASS and stage == "4_mep":
            run_judge("4_mep", {}, judge_fn=None, verdict_dir=run_dir / "verdicts")
        update_state(run_dir, outcome, timestamp=args.date or "")
        _print_outcome(outcome, outcome.packet)

        if outcome.status in (StepStatus.DETERMINISTIC_PASS, StepStatus.JUDGE_PASS):
            rec = manifest.accepted(stage)
            if rec is not None and stage in review_switches and not review_is_current(
                run_dir, stage=stage, output_hash=rec.output_hash
            ):
                review_outcome = StageOutcome(
                    stage=stage,
                    status=StepStatus.AWAITING_HUMAN_REVIEW,
                    attempts_used=outcome.attempts_used,
                    accepted_attempt=outcome.accepted_attempt,
                    report=outcome.report,
                    message="stage passed — awaiting durable human review approval",
                )
                update_state(run_dir, review_outcome, timestamp=args.date or "")
                _print_outcome(review_outcome)
                _print_review_checkpoint(run_dir, stage, rec.accepted_attempt)
                return FLOW_EXIT_CHECKPOINT
            i += 1
            continue

        if outcome.status == StepStatus.AWAITING_JUDGE:
            print("  submit a verdict, then rerun flow to resume:")
            print(
                "     run_stage.py"
                f" --base-dir {args.base_dir} judge {args.case} {args.run} {stage}"
                " --verdict <verdict.json>"
            )
            return FLOW_EXIT_CHECKPOINT

        if outcome.status == StepStatus.AWAITING_GEOMETRY_APPROVAL:
            vpath = _render_geometry_viewer(run_dir, case_dir)
            if vpath:
                print(f"  3D viewer: {vpath}")
            if args.geometry == "auto":
                appr = approve_geometry(
                    run_dir,
                    actor="flow:auto",
                    timestamp=args.date or "",
                    policy="auto",
                    note="flow --geometry auto",
                    case_dir=case_dir,
                    expected_digest=_source_review_digest(run_dir),
                )
                if appr is None:
                    print("✗ geometry auto-approval failed: no consistent checkpoint")
                    return FLOW_EXIT_STOP
                mark_geometry_approved(run_dir, timestamp=args.date or "")
                print(f"  geometry auto-approved digest={appr.digest}")
                continue
            print("  approve after inspection:")
            print(
                "     run_stage.py"
                f" --base-dir {args.base_dir} approve-geometry {args.case} {args.run}"
                " --actor <you>"
                f" --digest {_source_review_digest(run_dir)}"
            )
            return FLOW_EXIT_CHECKPOINT

        if outcome.status == StepStatus.AWAITING_REREAD:
            _print_reread_protocol(args, outcome)
            return FLOW_EXIT_CHECKPOINT

        if outcome.status == StepStatus.JUDGE_BLOCK:
            target = outcome.route_target
            if target not in _STAGES:
                print(f"  cannot auto-resample judge route target: {target!r}")
                return FLOW_EXIT_CHECKPOINT
            if _stage_index(target) > end:
                print(f"  judge route target {target} is beyond --to {to_stage}")
                return FLOW_EXIT_CHECKPOINT
            dropped = invalidate(manifest, target)
            manifest.save(run_dir)
            if dropped:
                print(f"  invalidated downstream accepted pointers: {dropped}")
            print(f"  auto blind-resample target: {target}")
            force_next.add(target)
            i = _stage_index(target)
            continue

        if outcome.terminal_stop:
            return FLOW_EXIT_STOP

        print(f"  unhandled flow status: {outcome.status.value}")
        return FLOW_EXIT_CHECKPOINT

    if source_target:
        from src.agent.execution.source_bim import export_source_bim
        report = export_source_bim(run_dir, args.bim_out, capability_profile=policy.capability_profile,
                                   enclosure_input_path=getattr(args, "enclosure_input", None))
        print(f"  source BIM: {args.bim_out} (source geometry ready={report['source_geometry_ready']}; drawing fidelity={report.get('drawing_fidelity', 'not_evaluated')})")
        if branch_target == "ep" and report["source_geometry_ready"]:
            from src.agent.execution.ep_branch import export_ep_branch
            branch = export_ep_branch(args.bim_out / "source_model.json", args.physics_template,
                                      args.zone_bindings, args.backend_out,
                                      epw=Path(args.epw) if args.with_ep else None,
                                      opening_policy_path=getattr(args, "opening_policy", None))
            print(json.dumps(branch, ensure_ascii=False))
            return FLOW_EXIT_OK if branch["status"] in ("exported", "passed") else FLOW_EXIT_STOP
        return FLOW_EXIT_OK if report["source_geometry_ready"] else FLOW_EXIT_STOP

    if args.with_ep:
        code = _flow_ep(run_dir, testdata_text, args, case_dir)
        if code:
            return code

    if args.record:
        state = load_state(run_dir)
        if state.get("stop_reason") and not args.record_partial:
            print(f"✗ refusing record with pending stop_reason={state['stop_reason']}")
            return FLOW_EXIT_EP_RECORD
        if not args.orchestrator:
            print("✗ --record requires --orchestrator")
            return FLOW_EXIT_EP_RECORD
        if policy.run_profile == "golden":
            appr = GeometryApproval.load(run_dir)
            if appr is not None and appr.actor == "flow:auto" and appr.policy == "auto":
                print("⚠ golden record with flow:auto geometry approval; human HTML review is recommended")
        try:
            sys.path.insert(0, str(_REPO_ROOT / "scripts" / "tool_scripts"))
            from record_baseline import record_baseline

            record_baseline(
                run_dir,
                date=args.date or "",
                orchestrator=args.orchestrator,
                require_ep=args.with_ep,
                run_profile=args.run_profile,
                # R4-a / L-R2: `flow --record` is the official-record ritual
                # (CLAUDE.md §6 #12) — the one call site of record_baseline
                # that fail-closes on an undeclared reading_mode. See
                # src/agent/execution/reading_mode.py module docstring.
                require_reading_mode=True,
            )
        except Exception as e:  # noqa: BLE001 — flow owns record failure code
            print(f"✗ record failed: {type(e).__name__}: {e}")
            return FLOW_EXIT_EP_RECORD
        print(f"  baseline recorded: {run_dir / '_run' / 'baseline.json'}")

    return FLOW_EXIT_OK


def cmd_status(args) -> int:
    _case_dir, run_dir, _td = _resolve(args.base_dir, args.case, args.run)
    state = load_state(run_dir)
    print(f"orchestration state — {args.case}/{args.run}")
    print(f"  stop_reason: {state.get('stop_reason')}")
    for stage in _STAGES:
        st = state.get("stages", {}).get(stage)
        if st:
            g = st.get("gate1", {})
            print(f"  {stage:16s} {st['status']:28s} "
                  f"attempts={st['attempts_used']} gate①={g}")
        else:
            print(f"  {stage:16s} (not run)")
    return 0


def cmd_artifacts(args) -> int:
    """`artifacts <case> <run> <stage>` — produce the stage's REQUIRED viewing
    artifacts for an already-merged run, without re-running the stage.

    Why this verb exists (F-38, 2026-08-15): the artifact set is contract, not
    preference — `pipeline_stage_contracts.md` §0_reading lists `*_render.png`,
    and `new_case_guide.md` §"grade 批卷" requires a `score_vs_gt.json` +
    `grade.png` per attempt with the accepted one promoted to the stage root.
    Both producers already existed (`_render_reading_attempts`,
    `_render_stage_grade_artifacts`) but were reachable ONLY through `flow`.
    A run driven the hard-isolation way (`spawn_isolated_reader.py build/spawn/
    merge`) therefore produced no images at all — the same "the user saw no
    product" symptom O-1 was written to fix, resurfacing on a second path.

    Kept a SEPARATE verb rather than folded into `merge` on purpose: grade
    artifacts read gt, and `merge` runs gate① in-process (invariant #4 — gate①
    and the executor never import gt). This verb is the judge-side step, run
    after merge.

    `--renders-only` skips the gt-reading half, for a run with no gt or when the
    caller must stay gt-free. Both halves are idempotent.
    """
    from src.agent.execution.run_config import load_run_config

    _case_dir, run_dir, _td = _resolve(args.base_dir, args.case, args.run)
    result: dict = {"stage": args.stage, "renders": [], "grade": {}}

    if args.stage == "0_reading":
        result["renders"] = _render_reading_attempts(run_dir)

    if not args.renders_only:
        run_config = load_run_config(run_dir)
        manifest = _load_manifest_readonly(run_dir)
        graded = _render_stage_grade_artifacts(
            args.stage, args.case, run_dir,
            manifest=manifest, run_config=run_config,
            run_profile=run_config.run_profile or "exploratory",
        )
        result["grade"] = {str(k): v for k, v in (graded or {}).items()}

    print(json.dumps(result, sort_keys=True, default=str))
    return 0


def cmd_provision(args) -> int:
    """`provision <case> <run>` — provision (or re-verify) this run's trusted
    view manifest (§4.4, the "唯一 emitter" for `<run>/_run/view_manifest.json`).

    `--migrate` is the ONE explicit write-path exception (§5.1): migrates this
    run's `run_manifest.json` from v1 to v2 in place (backfilling every
    accepted stage's artifact_hashes from the real on-disk attempt files).
    Never invoked automatically — a v1 (grandfathered) run stays read-only for
    new 0_reading attempts until an operator runs this explicitly.
    """
    from src.agent.execution.manifest import migrate_run_to_v2
    from src.agent.execution.run_config import load_run_config
    from src.agent.execution.run_provision import provision_run

    case_dir, run_dir, _td = _resolve(args.base_dir, args.case, args.run)
    if args.migrate:
        v2 = migrate_run_to_v2(case_dir, run_dir)
        print(json.dumps({"migrated": True, "run_id": v2.run_id, "stages": sorted(v2.stages)}, sort_keys=True))
        return 0
    # S-2 + S-3: run-level provisioning freezes BOTH the view manifest and the
    # effective run policy, then fail-closes on the strict-profile invariants
    # (L-13 missing run_profile; L-20 unknown dimensioned applicability). The
    # structured run_config.yaml declaration wins; the CLI --run-profile is the
    # fallback an operator supplies (R1-1). R1-7: a conflicting explicit CLI flag
    # is refused by _resolve_run_profiles rather than silently overriding config.
    run_config = load_run_config(run_dir)
    run_profile, capability_profile, source = _resolve_run_profiles(run_config, args)
    manifest = provision_run(
        case_dir, run_dir,
        run_profile=run_profile,
        capability_profile=capability_profile,
        context=_run_policy_context(args, run_config),
        source=source,
    )
    print(json.dumps(
        {"provisioned": True, "content_sha256": manifest.content_sha256, "entries": len(manifest.entries)},
        sort_keys=True,
    ))
    return 0


def cmd_bim(args) -> int:
    """Generate the source BIM exit, independently of the legacy EP flow."""
    from src.agent.execution.source_bim import export_source_bim
    from src.agent.execution.run_policy_freeze import resolve_frozen_run_policy

    _case_dir, run_dir, _td_path = _resolve(args.base_dir, args.case, args.run)
    frozen = resolve_frozen_run_policy(run_dir)
    capability = args.capability_profile if frozen.legacy_defaulted else frozen.capability_profile
    report = export_source_bim(run_dir, args.out, capability_profile=capability,
                               candidate_attempt=args.candidate_attempt,
                               enclosure_input_path=getattr(args, "enclosure_input", None))
    print(json.dumps({"status": report["status"], "counts": report.get("counts"),
                      "output": str(args.out), "error": report.get("error")}, ensure_ascii=False))
    return 0 if report["source_geometry_ready"] else 1


def cmd_backend_ep(args) -> int:
    from src.agent.execution.ep_branch import export_ep_branch
    report = export_ep_branch(args.source, args.physics_template, args.zone_bindings,
                              args.out, epw=args.epw if args.with_ep else None,
                              opening_policy_path=args.opening_policy)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] in ("exported", "passed") else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-dir", default="case_tests/e2e_tests")
    ap.add_argument("--budget-draws", type=int, default=None,
                    help="override per-stage draw budget (default 3) — explicit human-triage knob to grant extra draws after a quarantine")
    ap.add_argument("--date", default="", help="ISO date stamp for state/approval")
    ap.add_argument("--reading-runner-available", action="store_true",
                    help="enable awaiting_reread decisions; the main Agent still runs the sub-agent protocol")
    ap.add_argument(
        "--run-profile",
        choices=("exploratory", "dev", "golden", "regression"),
        default="exploratory",
        help="evidence gate policy: exploratory/dev flag, golden/regression block",
    )
    ap.add_argument(
        "--capability-profile",
        choices=("rectangular", "orthogonal_polygon"),
        default="rectangular",
        help="geometry capability profile for correction/kernel gates",
    )
    sub = ap.add_subparsers(dest="verb", required=True)

    pb = sub.add_parser("bim", help="export source BIM without EP cutting/pairing or physics")
    pb.add_argument("case"); pb.add_argument("run")
    pb.add_argument("--out", type=Path, required=True, help="new source BIM output directory")
    pb.add_argument("--candidate-attempt", type=int, default=None,
                    help="explicitly preview an unaccepted correction attempt; never grants acceptance")
    pb.add_argument("--enclosure-input", type=Path, help="source geometry declaration of open/unknown enclosure regions")

    pe = sub.add_parser("backend-ep", help="derive EP from a frozen source BIM and independent physics")
    pe.add_argument("--source", type=Path, required=True)
    pe.add_argument("--physics-template", type=Path, required=True)
    pe.add_argument("--zone-bindings", type=Path, required=True)
    pe.add_argument("--opening-policy", type=Path, help="explicit per-source-door closed state/construction/reason JSON")
    pe.add_argument("--out", type=Path, required=True)
    pe.add_argument("--with-ep", action="store_true", help="also run the actual EnergyPlus simulation")
    pe.add_argument("--epw", type=Path, default=Path("data/weather/Shenzhen.epw"))

    for verb in ("run", "resample"):
        p = sub.add_parser(verb)
        p.add_argument("case"); p.add_argument("run"); p.add_argument("stage")
        p.add_argument("--force", action="store_true")

    pj = sub.add_parser("judge")
    pj.add_argument("case"); pj.add_argument("run"); pj.add_argument("stage")
    pj.add_argument("--verdict", required=True, help="path to a StageVerdict JSON")

    pa = sub.add_parser("approve-geometry")
    pa.add_argument("case"); pa.add_argument("run")
    pa.add_argument("--actor", required=True); pa.add_argument("--note")
    pa.add_argument("--digest", required=True, help="full source checkpoint digest shown with the Stage 2 review")
    pa.add_argument("--policy", choices=("required", "auto"), default="required")

    pr = sub.add_parser("approve-review")
    pr.add_argument("case"); pr.add_argument("run"); pr.add_argument("stage", choices=_STAGES)
    pr.add_argument("--actor", required=True); pr.add_argument("--note")

    pf = sub.add_parser("flow")
    pf.add_argument("case"); pf.add_argument("run")
    pf.add_argument("--target", choices=("ep", "source-bim", "legacy-ep"), default="source-bim",
                    help="shared source BIM trunk, optionally followed by EP; old stages 2-5 require explicit legacy-ep")
    pf.add_argument("--bim-out", type=Path, help="new source output directory, required for --target source-bim")
    pf.add_argument("--enclosure-input", type=Path, help="source geometry declaration of open/unknown enclosure regions")
    pf.add_argument("--reading-model", choices=("haiku", "sonnet"),
                    help="read original drawings once through the Claude subscription; reuse an accepted reading")
    pf.add_argument("--reading-timeout", type=float, default=900,
                    help="automatic reader timeout in seconds (default 900)")
    pf.add_argument("--physics-template", type=Path, help="geometry-free EP physics template")
    pf.add_argument("--zone-bindings", type=Path, help="JSON source space ID to physics zone name map")
    pf.add_argument("--opening-policy", type=Path, help="explicit per-source-door backend policy JSON")
    pf.add_argument("--backend-out", type=Path, help="new EP branch directory")
    pf.add_argument("--from", dest="from_stage", default="auto",
                    choices=["auto", *_STAGES])
    pf.add_argument("--to", dest="to_stage", default="5_intakeoutput",
                    choices=_STAGES)
    pf.add_argument("--judge", choices=("stop", "off"), default="stop")
    pf.add_argument("--review", default="",
                    help="comma-separated human checkpoints: reading,correction")
    pf.add_argument("--geometry", choices=("required", "auto"), default="required")
    pf.add_argument("--with-ep", action="store_true")
    pf.add_argument("--record", action="store_true")
    pf.add_argument("--record-partial", action="store_true")
    pf.add_argument("--orchestrator", default="")
    pf.add_argument("--llm-config", type=Path, default=None)
    pf.add_argument("--epw", default="data/weather/Shenzhen.epw")

    ps = sub.add_parser("status")
    ps.add_argument("case"); ps.add_argument("run")

    pt = sub.add_parser("artifacts")
    pt.add_argument("case"); pt.add_argument("run")
    pt.add_argument("stage", choices=_STAGES)
    pt.add_argument("--renders-only", action="store_true",
                    help="skip the gt-reading grade half (no gt, or caller must stay gt-free)")

    pp = sub.add_parser("provision")
    pp.add_argument("case"); pp.add_argument("run")
    pp.add_argument("--migrate", action="store_true",
                     help="explicit v1->v2 run manifest migration (the only other write path besides normal provisioning)")

    args = ap.parse_args()
    if not hasattr(args, "force"):
        args.force = False
    return {
        "run": cmd_run, "resample": cmd_resample, "judge": cmd_judge,
        "approve-geometry": cmd_approve_geometry,
        "approve-review": cmd_approve_review,
        "flow": cmd_flow,
        "status": cmd_status,
        "artifacts": cmd_artifacts,
        "provision": cmd_provision,
        "bim": cmd_bim,
        "backend-ep": cmd_backend_ep,
    }[args.verb](args)


if __name__ == "__main__":
    raise SystemExit(main())
