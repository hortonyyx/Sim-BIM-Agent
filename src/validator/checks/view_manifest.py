"""gate① check ``reading.view_manifest_coverage`` (C2 B-M §6).

CheckLayer.INVARIANT, always BLOCK on fail regardless of ``run_profile`` — r1
裁决: input identity/completeness gets no run_profile carve-out, the same
precedent as ``reading.present`` (both plain INVARIANT check_ids, outside
``EVIDENCE_CHECK_IDS``, so :func:`src.validator.checks.schema.disposition`
already maps every FAIL to BLOCK unconditionally — no special-casing needed
here).

Reused by both the flat-flow reader (``run_stage._draw_reading``, one call per
attempt) and isolation's merge writer (the "merge 同门" checker, §5.2) so both
paths judge honesty against the identical rule: a required_view's declared
``expected_output_id`` must have a matching produced artifact, and no produced
artifact may claim a stem outside the expected set. Denominator-level scoring
(what fraction of *claims* were actually answered) belongs to B4b; this check
only guards honesty/identity (miss, extra, manifest drift).
"""

from __future__ import annotations

from src.agent.execution.view_manifest import (
    ReadingExamScope,
    ViewManifest,
    dimensioned_state,
)
from src.validator.checks.schema import CheckLayer, CheckReport, CheckStatus, RunProfile

CHECK_ID = "reading.view_manifest_coverage"


def check_reading_stage(
    manifest: ViewManifest | None,
    produced: dict[str, dict],
    *,
    exam_scope: ReadingExamScope | None = None,
    dimensioned_stems: set[str] | None = None,
    manifest_missing_reason: str = "view manifest missing or unreadable",
    capability_profile: str = "rectangular",
    run_profile: RunProfile = "exploratory",
    run_policy_sha256: str | None = None,
    run_policy_source: str | None = None,
) -> CheckReport:
    """The "merge 同门" checker (§5.2): coverage (:func:`check_view_manifest_coverage`)
    + per-view schema linting (:func:`src.validator.checks.reading.check_reading_view`)
    against the SAME rule, whether the caller is the flat-flow reader (one
    ``*_view.json`` file per call) or isolation's merge writer (one aggregate
    ``{"views": {stem: {...}}}`` payload). ``produced`` is ``{stem: raw dict}``.

    B-1 (r3 batchC dispatch §1 BLOCKER): this function used to take a
    ``case_dir`` and resolve per-stem "trusted" PIXEL image bounds from it
    (r2's X-2) to feed into the per-view OCR-anchor/dimension-endpoint bounds
    checks. That was dimensionally wrong (reading coordinates are metres) and
    has been retired — those two checks now use a self-contained, image-
    independent unit-anomaly reference computed from each view's own stroke
    geometry (see ``src.validator.checks.reading._structural_metric_reference``).
    There is therefore no more image-decode step on this path at all, which
    also closes M-2 (a source image that hashes/manifests fine but cannot be
    PIL-decoded used to silently drop that one stem's trusted bounds and fall
    back to the weaker product-derived bounds while ``reading.view_manifest_coverage``
    still passed — with no more image-decode step, that fallback path no
    longer exists to be silently taken).

    ``dimensioned_stems``, if given, overrides the manifest-derived dimensioned
    set for the per-view schema check — the flat-flow caller passes its
    existing ``dimensioned_view_names()`` result so this refactor does not
    silently change that pre-existing check's behavior; it defaults to the
    manifest's own ``dimensioned`` flags (isolation's merge path, which has no
    other source).

    S-3: the per-view applicability is the manifest's 4-state dimensioned value
    (``declared_true``/``declared_false``/``unknown``/``legacy_default``), so a
    structured :class:`DimensionedApplicability` is normalized rather than
    folded by truthiness (``if e.dimensioned`` would wrongly treat a
    ``declared_false`` object as dimensioned). ``dimensioned_stems`` is now a
    fallback for stems with no manifest entry (e.g. flat-flow when manifest
    provisioning failed)."""
    from src.validator.checks.reading_product import check_reading_product

    rep = check_view_manifest_coverage(
        manifest,
        set(produced),
        exam_scope=exam_scope,
        manifest_missing_reason=manifest_missing_reason,
        capability_profile=capability_profile,
        run_profile=run_profile,
    )
    # S-3: per-stem 4-state applicability straight from the manifest wire; never
    # fold a structured DimensionedApplicability by truthiness.
    manifest_state: dict[str, str] = {}
    if manifest is not None:
        for e in manifest.required_entries():
            manifest_state[e.expected_output_id] = dimensioned_state(e.dimensioned)

    for stem in sorted(produced):
        raw = produced[stem]
        state = manifest_state.get(stem)
        if state is None:
            state = "declared_true" if (dimensioned_stems and stem in dimensioned_stems) else "legacy_default"
        sub = check_reading_product(
            raw,
            capability_profile=capability_profile,
            run_profile=run_profile,
            dimensioned_state=state,
        )
        for r in sub.results:
            rep.results.append(r.model_copy(update={"check_id": f"{stem}.{r.check_id}"}))
    # S-2 (G-3): stamp the frozen policy that governed this report so checks.json
    # proves which policy was in effect (not two sourceless strings).
    rep.run_policy_sha256 = run_policy_sha256
    rep.run_policy_source = run_policy_source
    return rep


def check_view_manifest_coverage(
    manifest: ViewManifest | None,
    produced_stems: set[str],
    *,
    exam_scope: ReadingExamScope | None = None,
    manifest_missing_reason: str = "view manifest missing or unreadable",
    capability_profile: str = "rectangular",
    run_profile: RunProfile = "exploratory",
) -> CheckReport:
    """``manifest=None`` covers both "never provisioned" and "on-disk manifest
    failed verification" (hash drift / corrupt) — callers resolve that via
    :func:`src.agent.execution.view_manifest.verify_view_manifest` /
    :func:`~src.agent.execution.view_manifest.provision_view_manifest` and pass
    the reason through for the evidence trail."""
    rep = CheckReport(stage="0_reading", capability_profile=capability_profile, run_profile=run_profile)
    if manifest is None:
        rep.add_fail(CHECK_ID, CheckLayer.INVARIANT, manifest_missing_reason)
        return rep

    all_expected = manifest.expected_output_ids()  # expected_output_id -> input_id
    if exam_scope is None:
        expected = all_expected
    else:
        if exam_scope.base_view_manifest_sha256 != manifest.content_sha256:
            rep.add_fail(CHECK_ID, CheckLayer.INVARIANT, "reading exam scope is not bound to this view manifest")
            return rep
        selected = set(exam_scope.input_ids)
        expected = {output_id: input_id for output_id, input_id in all_expected.items() if input_id in selected}
        for output_id, input_id in sorted(all_expected.items()):
            if input_id not in selected:
                rep.add(
                    f"{CHECK_ID}.out_of_scope.{output_id}",
                    CheckStatus.NOT_APPLICABLE,
                    CheckLayer.INVARIANT,
                    message="view is outside this run's declared reading exam scope",
                    evidence={"input_id": input_id, "source": exam_scope.source},
                )
    expected_ids = set(expected)
    missing = sorted(expected_ids - produced_stems)
    extra = sorted(produced_stems - expected_ids)

    if missing or extra:
        parts = []
        if missing:
            parts.append(f"{len(missing)} required view(s) have no matching produced artifact")
        if extra:
            parts.append(f"{len(extra)} produced artifact(s) do not match any expected_output_id")
        rep.add_fail(
            CHECK_ID,
            CheckLayer.INVARIANT,
            "; ".join(parts),
            evidence={"missing_expected_output_ids": missing, "extra_stems": extra},
        )
    else:
        rep.add_pass(
            CHECK_ID,
            CheckLayer.INVARIANT,
            evidence={"expected_output_ids": sorted(expected_ids)},
        )
    return rep


__all__ = ["CHECK_ID", "check_view_manifest_coverage"]
