"""S-2 + S-3 run-level provisioning wrapper (the "declare → freeze → prove"
transaction).

``provision_view_manifest`` freezes CASE identity (the trusted view manifest);
this module freezes RUN identity (the effective run policy) and adds the two
strict-profile fail-closed gates that the lossy defaults used to bypass:

  - **L-13** (S-2): a NEW run must declare its structured ``run_profile`` — it
    may not silently default to exploratory. Delegates to
    :func:`provision_run_policy` (``run_profile=None`` raises
    ``run_profile_not_declared``).
  - **L-20** (S-3): a strict run (``golden``/``regression``) whose case declared
    a STRUCTURED ``dimensioned_views`` may not leave a required view ``unknown``
    — the exam question was never answered, which is a fail-closed provisioning
    fault, never a silent ``False`` manifest.

Legacy cases (absent / stem-string ``dimensioned_views``) and legacy runs (no
``run_config.yaml`` run_profile) are read-only replays and never fail here
(G-6): the strict fail-closed gates only trigger on a NEW provisioning that
declares a strict tier.
"""

from __future__ import annotations

from pathlib import Path

from src.agent.execution.run_policy_freeze import provision_run_policy
from src.agent.execution.view_manifest import (
    DimensionedApplicability,
    ViewManifest,
    build_view_manifest,
    provision_view_manifest,
)

_STRICT_PROFILES = ("golden", "regression")


def validate_dimensioned_applicability(manifest: ViewManifest, *, run_profile: str) -> None:
    """S-3 L-20: a strict run may not carry an ``unknown`` dimensioned view.

    A structured ``dimensioned_views`` declaration that left a required view out
    surfaces as ``DimensionedApplicability(state="unknown")`` on the manifest
    (see :func:`build_view_manifest`). In a strict run that is a fail-closed
    provisioning fault — the dimension checks would silently N/A a view the case
    meant to declare — so the run is refused before it starts, never silently
    defaulted to ``False``. Legacy manifests (bool ``dimensioned``) have no
    ``DimensionedApplicability`` and never trip this gate.
    """
    if run_profile not in _STRICT_PROFILES:
        return
    unknown = sorted(
        e.expected_output_id
        for e in manifest.required_entries()
        if isinstance(e.dimensioned, DimensionedApplicability) and e.dimensioned.state == "unknown"
    )
    if unknown:
        raise ValueError(
            f"dimensioned_applicability_unknown: run_profile={run_profile!r} but required "
            f"view(s) {unknown} have no structured dimensioned declaration — declare each "
            "view's applicability with provenance (dimensioned_views object list); a strict "
            "run may not silently default a required view to not-dimensioned"
        )


def provision_run(
    case_dir: Path | str,
    run_dir: Path | str,
    *,
    run_profile: str | None,
    capability_profile: str | None = None,
    context: dict | None = None,
    source: str = "structured_config",
) -> ViewManifest:
    """The run-level provisioning transaction (S-2 + S-3).

    For a strict run, FIRST fail-closes on any ``unknown`` dimensioned
    applicability (L-20) against the in-memory manifest, THEN freezes the view
    manifest + the effective run policy (L-13 fail-closed on a missing
    structured ``run_profile``). Returns the frozen manifest.

    R1-4 (派工单 §1.4): applicability is validated BEFORE any freeze write so a
    strict-profile refusal leaves NO usable artifact on disk. r0 validated AFTER
    ``provision_view_manifest`` + ``provision_run_policy`` had already written
    ``view_manifest.json`` + ``run_policy.json``; an operator could then ignore
    the raised error and proceed straight to isolation build, which only reads
    the already-frozen manifest + policy and never re-runs this gate. Build the
    manifest once up front; ``provision_view_manifest`` rebuilds it
    byte-identically when it writes (same case_data, deterministic).

    Callers that only need the case manifest may call
    :func:`provision_view_manifest` directly; this wrapper adds the policy freeze
    + strict applicability gate so a NEW strict run can never start with an
    undeclared tier or an unanswered dimension exam question.
    """
    if run_profile in _STRICT_PROFILES:
        validate_dimensioned_applicability(build_view_manifest(case_dir), run_profile=run_profile)
    manifest = provision_view_manifest(case_dir, run_dir)
    provision_run_policy(
        run_dir,
        run_profile=run_profile,
        capability_profile=capability_profile,
        context=context,
        source=source,
    )
    return manifest


__all__ = ["validate_dimensioned_applicability", "provision_run"]
