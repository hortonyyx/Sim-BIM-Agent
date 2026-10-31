"""CheckReport v2 — the common schema every per-stage deterministic check emits.

Two ideas drive the design, both from the 2026-06-15 validation architecture
(see AI_agent/design/implementation.md):

1. **Three layers** (§0.2). Every check declares which layer it belongs to:
     - ``invariant``   — a structural/geometric invariant that MUST hold; a
                         failure is a bug or fatal. Maps to BLOCK.
     - ``cross_check`` — reconciliation against an upstream constraint / another
                         channel / a plausibility band; a failure is a surface
                         for attribution, not a stop. Maps to FLAG.
     - ``perceptual``  — a faithfulness call deterministic code cannot make;
                         handled by a human / VLM judge. Not produced here.

2. **Policy ≠ fact** (§0.4 #8). A check emits a *fact*: ``CheckResult`` with a
   status and machine-readable evidence. Whether a failing fact *blocks* is a
   *policy* decision derived from ``(layer, status, capability_profile)`` by
   :func:`disposition` — kept a pure function so the same fact can map
   differently under different profiles. A check that does not apply under the
   active profile emits ``not_applicable`` (e.g. the rectangular-coverage check
   under a non-rectangular profile), so blocking stays profile-aware without the
   check itself knowing the policy.

The report carries the ``artifact_hash`` / ``attempt_hash`` it was computed
against so an accepted attempt's checks are auditable after the fact (M0
append-only attempts), plus ``check_version`` for golden-test stability.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Bump when the report-level shape changes (golden tests assert on this).
# 2.2 (S-2): the reading gate① report now records the frozen effective run
# policy that governed disposition (``run_policy_sha256`` + ``run_policy_source``),
# so ``checks.json`` proves which policy was in effect — not two sourceless strings.
REPORT_SCHEMA_VERSION = "2.2"

RunProfile = Literal["exploratory", "dev", "golden", "regression"]

EVIDENCE_CHECK_IDS = frozenset(
    {
        "reading.dimension_chain_closure",
        "reading.dimension_derived_refs",
        "reading.dimensions_present",
        "reading.dimension_p1a_fields",
        "reading.raw_field_presence",
        "reading.stroke_provenance_coverage",
    }
)

# 2026-08-08 摊三②(interface sweep B-1'): this used to be a *block* whitelist
# (``_EVIDENCE_BLOCK_PROFILES = {"golden", "regression"}``) — a future profile
# stricter than "regression" would not be in that set, so it would silently
# NOT block, i.e. a newly-added strict profile defaulted to lenient. Flipped
# to a *permissive* whitelist: everything not explicitly named here is
# treated as strict (blocks). "exploratory"/"dev" are the two profiles this
# evidence-debt gate is deliberately soft on (historical artifacts predating
# the evidence-provenance contract must stay replayable there); a future 5th
# profile now defaults to being held to the same bar as "golden"/"regression"
# unless someone deliberately adds it here.
_EVIDENCE_PERMISSIVE_PROFILES = frozenset({"exploratory", "dev"})
_CORRECTION_EVIDENCE_DEBT_COVERAGE = "correction.evidence_debt_coverage"

# A plan view that does not declare a usable local→world frame cannot be scored
# at all: gate② rebuilds that frame only from `scale_origin.world_x_m/world_y_m`,
# and without it the whole plan channel comes back `plan_frame_unavailable` (a
# silent zero, indistinguishable from bad tracing). An acceptance run must refuse
# such a reading rather than score it zero; an exploratory/dev run only warns, so
# historical artifacts predating the instruction contract stay replayable. Same
# profile split as the missing-judge-sidecar gap (2026-07-20).
PLAN_FRAME_CHECK_ID = "reading.plan_scale_origin_usable"
# 2026-08-08 摊三②: same whitelist-direction flip as `_EVIDENCE_PERMISSIVE_PROFILES`
# above, same reasoning — a future stricter profile now defaults to BLOCK
# instead of silently inheriting leniency.
_PLAN_FRAME_PERMISSIVE_PROFILES = frozenset({"exploratory", "dev"})

# 2026-08-18 — THE MACHINE CONSUMER for the cross-axis calibration signal.
#
# History this exists to close: 2026-08-17 changed `px_m_calibrator`'s cross-axis
# over-limit from `raise` to a legal exit (returns a usable blend +
# `axis_calibration_disagreement` + `metric.confidence="low"` + `warnings[]`).
# The cross-family review (GLM) judged that MAJOR at the time, on the ground that
# the old raise reached the agent as rc=2 (impossible to ignore) while the new
# exit is rc=0 — "the tool succeeded" — leaving the disagreement in two fields
# with **zero machine consumers**, i.e. the gate degraded from "forced to be
# visible" into "hoping the reader is honest".
#
# That was a prediction. On 2026-08-18 the first draw after the change measured
# it (`run_2026-08-17_707mode_H2`, pilot r2): the tool reported
# `axis_calibration_disagreement: true`, a 37.18% deviation against a 0.3%
# limit, `confidence: "low"`, and a warning reading "Do not silently trust the
# blended px_per_m below" — and the reader used the blend anyway AND reported
# "Calibration: applied consistently ✓ / Self-check: passes ✓" to its reviewer,
# never mentioning the disagreement. So the failure is worse than "the warning
# was ignored": the reader reported the OPPOSITE of the warning.
#
# ⛔ Deliberately NOT fixed by asking the reading to declare which calibration it
# used. The same run established that this model's self-report is systematically
# more compliant than its artifact (five separate claims contradicted by the
# file), so a gate whose input is the reader's own claim is a gate on the least
# reliable available witness. The checker reads the EVIDENCE SIDECARS instead.
#
# Profile split matches PLAN_FRAME: an acceptance run must not accept geometry
# built on a ruler that disagrees with itself; exploratory/dev only flag, so
# diagnostic draws stay runnable and the fact still lands in the checks artifact.
CALIBRATION_AXES_CHECK_ID = "reading.calibration_axes_agree"
_CALIBRATION_AXES_PERMISSIVE_PROFILES = frozenset({"exploratory", "dev"})

# M-3 (r1 / F-4 ③): an OCR/annotation anchor outside the trusted image bounds is
# the textbook bad-data shape (a pixel anchor like [360,450] on a ~10 m plan that
# O-4's canvas fix stopped blowing up — and stopped surfacing). Flag it always.
OCR_ANCHOR_BOUNDS_CHECK_ID = "reading.ocr_anchors_in_bounds"
# 2026-08-04 r4 (batch C dispatch §1, user-ratified downgrade): this used to BLOCK
# under golden/regression via the "unit-anomaly" fence in
# ``src.validator.checks.reading._structural_metric_reference`` (median/MAD over
# a view's own stroke geometry). Independent sol review + orchestrator live
# testing found that fence is NOT reliable in either direction:
#   - false NEGATIVE: a structure written entirely in pixel space (so the bad
#     coordinate is no longer an outlier relative to its own geometry) sails
#     straight through — the exact shape the check exists to catch.
#   - false POSITIVE (the more dangerous one): an ordinary 10x8 m closed-
#     polyline room, or a 60x4 m elongated building, with perfectly legitimate
#     annotations, gets FAILED — the robust statistic degenerates on these
#     everyday shapes (see tests/test_checks_reading_correction.py's two r4
#     "real shape, must not be blocked" locks for the worked repro).
# ⇒ downgraded to ADVISORY ONLY: never in ``blocking()``, on any profile,
# including golden/regression. The FAIL fact + evidence (offenders /
# structural_reference) are still produced — nothing here is deleted or
# silently swallowed, only kept out of the blocking set. This is a stopgap;
# the structural fix is R1.5 (reading only ever writes pixel anchors +
# referenced dimensions, metric conversion lives solely in code so "pixel
# mistaken for metre" becomes constructionally impossible instead of
# something a heuristic has to guess at after the fact).
#
# 2026-08-08 摊三②: this constant is DELIBERATELY NOT flipped to the
# "permissive whitelist" shape applied above to `_EVIDENCE_PERMISSIVE_PROFILES`
# / `_PLAN_FRAME_PERMISSIVE_PROFILES`. Those two are lenient on
# "exploratory"/"dev" because THOSE PROFILES are intentionally soft; this one
# is lenient on every profile — including golden/regression — because the
# CHECK ITSELF is known unreliable in both directions (see the false-negative/
# false-positive account immediately above). Flipping this to a permissive
# whitelist of the four profiles known today would silently make a future,
# stricter 5th profile BLOCK on this exact heuristic by default — reintroducing
# the false-positive-fails-correct-buildings defect the 2026-08-04 downgrade
# was written to remove, for any run adopting that profile before R1.5 lands.
# Left as an always-empty block set (⇒ FLAG on every profile, present or
# future) until the structural fix (R1.5) replaces the heuristic; flagged for
# orchestrator review in the 2026-08-08 execution log rather than silently
# reinterpreted, since this is a case where the general "new strict profile
# defaults to strict" rule and this check's own documented "advisory on any
# profile" rule point in opposite directions.
_OCR_ANCHOR_BLOCK_PROFILES: frozenset[str] = frozenset()

# X-1 (r2 batchC dispatch §1): N-3's adaptive canvas scale (render_vector_to_png)
# stopped raising when a DIMENSION endpoint is written in pixel space — the same
# bad-data shape M-3 already catches for OCR anchors — because the renderer now
# downscales instead of blowing up. That deleted the last machine-readable
# signal gate① had for this failure mode (gate① never checked dimension
# endpoints in the first place; it only ever inherited the renderer's crash).
# Flag it always.
DIMENSION_ENDPOINT_BOUNDS_CHECK_ID = "reading.dimension_endpoints_in_bounds"
# 2026-08-04 r4: same downgrade, same reasons, as _OCR_ANCHOR_BLOCK_PROFILES
# above (this check shares the same ``_structural_metric_reference`` fence and
# the same false-negative/false-positive evidence) — see that comment for the
# full account. Advisory only; structural fix is R1.5.
#
# 2026-08-08 摊三②: same deliberate non-flip as `_OCR_ANCHOR_BLOCK_PROFILES`
# immediately above, same reasoning (the check's own unreliability, not a
# profile's leniency, is why this stays advisory) — see that comment.
_DIMENSION_ENDPOINT_BLOCK_PROFILES: frozenset[str] = frozenset()


def is_evidence_check_id(check_id: str) -> bool:
    return check_id in EVIDENCE_CHECK_IDS or any(
        check_id.endswith(f".{canonical}") for canonical in EVIDENCE_CHECK_IDS
    )


def is_plan_frame_check_id(check_id: str) -> bool:
    """Aggregating callers prefix per-view results with ``<stem>.``; match both."""
    return check_id == PLAN_FRAME_CHECK_ID or check_id.endswith(f".{PLAN_FRAME_CHECK_ID}")


def is_ocr_anchor_check_id(check_id: str) -> bool:
    """Aggregating callers prefix per-view results with ``<stem>.``; match both."""
    return check_id == OCR_ANCHOR_BOUNDS_CHECK_ID or check_id.endswith(f".{OCR_ANCHOR_BOUNDS_CHECK_ID}")


def is_calibration_axes_check_id(check_id: str) -> bool:
    """Aggregating callers prefix per-view results with ``<stem>.``; match both."""
    return check_id == CALIBRATION_AXES_CHECK_ID or check_id.endswith(f".{CALIBRATION_AXES_CHECK_ID}")


def is_dimension_endpoint_bounds_check_id(check_id: str) -> bool:
    """Aggregating callers prefix per-view results with ``<stem>.``; match both."""
    return (
        check_id == DIMENSION_ENDPOINT_BOUNDS_CHECK_ID
        or check_id.endswith(f".{DIMENSION_ENDPOINT_BOUNDS_CHECK_ID}")
    )


class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"          # intentionally not run (e.g. viewer headless)
    NOT_APPLICABLE = "not_applicable"  # does not apply under the active profile
    ERROR = "error"             # the check itself blew up — fail-closed


class CheckLayer(str, Enum):
    INVARIANT = "invariant"      # §0.2 L-不变量 — hard, BLOCK on fail
    CROSS_CHECK = "cross_check"  # §0.2 L-交叉核对 — soft, FLAG on fail
    PERCEPTUAL = "perceptual"    # §0.2 L-肉眼/judge — not produced by det. checks


class Disposition(str, Enum):
    BLOCK = "block"   # stop the pipeline / fail-closed
    FLAG = "flag"     # surface for attribution, do not stop
    INFO = "info"     # pass / not-applicable — informational
    SKIP = "skip"     # intentionally skipped


class CheckResult(BaseModel):
    """One check outcome — a FACT, not a policy decision."""

    model_config = ConfigDict(extra="forbid")

    check_id: str
    status: CheckStatus
    layer: CheckLayer
    check_version: str = "1"
    message: str = ""
    # Machine-readable evidence (counts, offending ids, deltas) — never prose
    # only, so a downstream consumer / judge can act on it programmatically.
    evidence: dict = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in (
            CheckStatus.PASS,
            CheckStatus.SKIPPED,
            CheckStatus.NOT_APPLICABLE,
        )


def disposition(
    result: CheckResult,
    *,
    capability_profile: str = "rectangular",
    # 2026-08-08 摊三①(interface sweep B-1'): the default used to be
    # "exploratory" — the MOST lenient profile — so any caller that forgot to
    # thread `run_profile` through silently got fail-open disposition instead
    # of an error. Default now to "regression" — the strictest profile — so a
    # dropped/omitted `run_profile` fails closed instead of failing open. Every
    # real production caller of `disposition()` already threads its own
    # `run_profile` explicitly (verified by grep, 2026-08-08); the only call
    # sites that relied on this default (`assembly.check_ep_baseline`'s
    # ERROR/INVARIANT-only checks, `test_execution_foundation`'s ERROR-status
    # test) are entirely insensitive to `run_profile` — their checks are
    # always-BLOCK via the `CheckStatus.ERROR` / plain-INVARIANT paths, which
    # never consult `run_profile` — so this default change is a no-op for
    # every caller that exists today, and only changes behavior for a FUTURE
    # caller that forgets to pass it.
    run_profile: RunProfile = "regression",
) -> Disposition:
    """Map a check FACT to a policy disposition. Pure function (§0.4 #8).

    ``capability_profile`` is accepted so a future profile can downgrade a
    specific invariant; today the default rules below are profile-agnostic
    because profile applicability is expressed by the check emitting
    ``not_applicable`` rather than by the policy reinterpreting a ``fail``.
    """
    if result.status == CheckStatus.PASS:
        return Disposition.INFO
    if result.status == CheckStatus.NOT_APPLICABLE:
        return Disposition.INFO
    if result.status == CheckStatus.SKIPPED:
        return Disposition.SKIP
    if result.status == CheckStatus.ERROR:
        # A check that errored is fail-closed: we do not know the artifact is
        # clean, so we must not let it pass silently.
        return Disposition.BLOCK
    # status == FAIL
    if is_plan_frame_check_id(result.check_id):
        # No legacy/grandfather carve-out here: an unscorable plan is unscorable
        # regardless of how the artifact was produced. The only relief is the
        # profile split. 2026-08-08 摊三②: whitelist direction flipped — see
        # `_PLAN_FRAME_PERMISSIVE_PROFILES`'s comment.
        if run_profile not in _PLAN_FRAME_PERMISSIVE_PROFILES:
            return Disposition.BLOCK
        return Disposition.FLAG
    if is_calibration_axes_check_id(result.check_id):
        # See CALIBRATION_AXES_CHECK_ID's comment. Same shape as PLAN_FRAME: the
        # artifact is not trustworthy regardless of how it was produced, so no
        # legacy carve-out — only the profile split.
        if run_profile not in _CALIBRATION_AXES_PERMISSIVE_PROFILES:
            return Disposition.BLOCK
        return Disposition.FLAG
    if is_ocr_anchor_check_id(result.check_id):
        # M-3 (r1 / F-4 ③): a pixel/out-of-bounds OCR anchor is bad data that
        # O-4's canvas fix stopped surfacing. FLAG ALWAYS, on every profile —
        # 2026-08-04 r4 downgraded this from BLOCK-on-acceptance to advisory
        # (``_OCR_ANCHOR_BLOCK_PROFILES`` is now permanently empty; see the
        # comment on that constant for the false-negative/false-positive
        # evidence that forced the downgrade). Structural fix is R1.5.
        if run_profile in _OCR_ANCHOR_BLOCK_PROFILES:
            return Disposition.BLOCK  # unreachable while the set above is empty
        return Disposition.FLAG
    if is_dimension_endpoint_bounds_check_id(result.check_id):
        # X-1: a pixel/out-of-bounds dimension endpoint is the same bad-data
        # shape as an OCR anchor (M-3) — FLAG ALWAYS, on every profile — 2026-
        # 08-04 r4 downgraded this the same way and for the same reasons (see
        # ``_DIMENSION_ENDPOINT_BLOCK_PROFILES``'s comment). Structural fix is
        # R1.5.
        if run_profile in _DIMENSION_ENDPOINT_BLOCK_PROFILES:
            return Disposition.BLOCK  # unreachable while the set above is empty
        return Disposition.FLAG
    if result.check_id == _CORRECTION_EVIDENCE_DEBT_COVERAGE:
        # 2026-08-08 摊三②: whitelist direction flipped — see
        # `_EVIDENCE_PERMISSIVE_PROFILES`'s comment.
        if (
            result.evidence.get("scope") == "element_local"
            and run_profile not in _EVIDENCE_PERMISSIVE_PROFILES
        ):
            return Disposition.BLOCK
        return Disposition.FLAG
    if is_evidence_check_id(result.check_id):
        if result.evidence.get("legacy_migrated"):
            return Disposition.FLAG
        if run_profile not in _EVIDENCE_PERMISSIVE_PROFILES:
            return Disposition.BLOCK
        return Disposition.FLAG
    if result.layer == CheckLayer.INVARIANT:
        return Disposition.BLOCK
    return Disposition.FLAG  # cross_check / perceptual failures flag, don't stop


class CheckReport(BaseModel):
    """All deterministic check results for one stage attempt (gate ①)."""

    model_config = ConfigDict(extra="forbid")

    stage: str
    capability_profile: str = "rectangular"
    # 2026-08-08 摊三①: same fail-open-by-default fix as `disposition()`'s
    # parameter above — see that comment. A `CheckReport` built without an
    # explicit `run_profile` now defaults to the strictest profile
    # ("regression") instead of the most lenient ("exploratory"), so a
    # dropped/omitted value fails closed. Every production `CheckReport(...)`
    # call site that matters for disposition already threads its own
    # `run_profile` explicitly (verified by grep, 2026-08-08); the one
    # production call site that doesn't (`assembly.check_ep_baseline`) only
    # ever emits ERROR/plain-INVARIANT checks that don't consult
    # `run_profile` at all, so this default change is a no-op for it.
    run_profile: RunProfile = "regression"
    artifact_hash: str | None = None
    attempt_hash: str | None = None
    report_schema_version: str = REPORT_SCHEMA_VERSION
    # S-2 (G-3): the frozen effective run policy that governed this report's
    # disposition. ``run_policy_sha256`` is the G-4 drift surface (hash of
    # capability_profile+run_profile); ``run_policy_source`` is
    # "structured_config" (provisioned) or "legacy_defaulted" (read-only replay).
    # ``None`` for non-reading reports or pre-S-2 callers.
    run_policy_sha256: str | None = None
    run_policy_source: str | None = None
    results: list[CheckResult] = Field(default_factory=list)

    # ---- mutation helpers (checks append as they run) ----
    def add(
        self,
        check_id: str,
        status: CheckStatus,
        layer: CheckLayer,
        *,
        message: str = "",
        evidence: dict | None = None,
        check_version: str = "1",
    ) -> CheckResult:
        r = CheckResult(
            check_id=check_id,
            status=status,
            layer=layer,
            check_version=check_version,
            message=message,
            evidence=evidence or {},
        )
        self.results.append(r)
        return r

    def add_pass(self, check_id: str, layer: CheckLayer, **kw) -> CheckResult:
        return self.add(check_id, CheckStatus.PASS, layer, **kw)

    def add_fail(
        self, check_id: str, layer: CheckLayer, message: str, **kw
    ) -> CheckResult:
        return self.add(check_id, CheckStatus.FAIL, layer, message=message, **kw)

    # ---- policy-aware queries ----
    def dispositions(self) -> list[tuple[CheckResult, Disposition]]:
        return [
            (
                r,
                disposition(
                    r,
                    capability_profile=self.capability_profile,
                    run_profile=self.run_profile,
                ),
            )
            for r in self.results
        ]

    def blocking(self) -> list[CheckResult]:
        """Results whose policy disposition is BLOCK (fail-closed set)."""
        return [
            r
            for r, d in self.dispositions()
            if d == Disposition.BLOCK
        ]

    def flagged(self) -> list[CheckResult]:
        return [r for r, d in self.dispositions() if d == Disposition.FLAG]

    @property
    def passed(self) -> bool:
        """True iff nothing blocks (flags are allowed through gate ①)."""
        return not self.blocking()
