"""Frozen effective run-policy record (S-2 / G-3).

The reading gate① disposition depends on ``(capability_profile, run_profile)``,
but those two knobs were previously assembled from defaults at each call site —
so the hard-isolation merge path silently ran ``rectangular/exploratory`` even
when the run was declared ``orthogonal_polygon/regression`` (the root cause sol
labelled G-3). This module makes "declare → issue → merge → check → prove" a
single hash-bound transaction, mirroring :func:`ReadingExamScope`:

  - :func:`provision_run_policy` — the **only** writer of ``<run>/_run/run_policy.json``.
    Idempotent (a second call with the same resolved policy returns the existing
    record); a policy change mid-run raises (``run_policy_drift``). A new
    provisioning with no declared ``run_profile`` raises fail-closed
    (``run_profile_not_declared``) — a strict run may never silently default to
    exploratory (L-13).
  - :func:`resolve_frozen_run_policy` — the **only** read consumer for both the
    flat-flow reader and isolation build/merge. Returns the frozen record after
    re-verifying it against the current ``run_config.yaml`` declaration; a run
    with no frozen artifact is a legacy replay and resolves to a synthetic
    ``legacy_defaulted=exploratory`` record (read-only, never fails — G-6).

r2-4 (ruling 2026-08-04 §2) — what the drift-detection hash covers, and why.
The hash covers **only** ``capability_profile + run_profile``. The governing
rule (§2.1): a knob may be frozen into the "tier policy" and participate in
drift detection ONLY if it has an external trust root — i.e. it is declared in
``run_config.yaml``. ``capability_profile`` + ``run_profile`` are the only such
knobs (structured-declared, and they decide gate① blocking). The operational
switches — ``require_ep`` (``--with-ep``), draw budget, reread availability,
``judge_enabled``, ``confirmation_policy``, ``validation_scope`` — are runtime
knobs with NO external trust root in ``run_config.yaml``; they come from the
per-invocation CLI call. They are therefore NEVER frozen as tier policy and
NEVER participate in drift detection. They ARE recorded into the frozen
record's ``context`` field, but strictly as a NON-authoritative audit snapshot
(see :attr:`RunPolicyRecord.context`): :func:`effective_run_policy` sources
them from the caller, never from ``context``, so editing ``context`` (even
after recomputing this file's self-hash) cannot change any decision — the
tamper surface is removed by the no-context-consumption rule, not by the hash.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.agent.execution.manifest import Hex64, hash_obj
from src.agent.execution.run_meta import run_meta_path

RUN_POLICY_NAME = "run_policy.json"
RUN_POLICY_SCHEMA_VERSION = "1"

RunProfile = Literal["exploratory", "dev", "golden", "regression"]
_RUN_PROFILES = ("exploratory", "dev", "golden", "regression")
_CAPABILITY_PROFILES = ("rectangular", "orthogonal_polygon")


def _run_policy_hash(capability_profile: str, run_profile: str) -> str:
    """G-4 drift surface: hash of the two gate①-consumed knobs only."""
    return hash_obj(
        {"capability_profile": capability_profile, "run_profile": run_profile}
    )


def _content_hash(payload: dict) -> str:
    return hash_obj({k: v for k, v in payload.items() if k != "content_sha256"})


class RunPolicyRecord(BaseModel):
    """Frozen effective run policy for one run (S-2)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = RUN_POLICY_SCHEMA_VERSION
    # r2-2 (ruling 2026-08-04 §r2-2): source must reflect WHERE the frozen
    # (run_profile, capability_profile) came from, not be a hardcoded constant.
    #   - structured_config: BOTH declared in run_config.yaml
    #   - cli:               NEITHER declared — CLI flags / argparse defaults
    #                       are the authority (incl. a pure --run-profile run)
    #   - mixed:             exactly one declared in config, the other CLI-sourced
    #   - legacy_defaulted:  synthetic read-only replay (no frozen artifact)
    # Drift re-verification is scoped to config-declared fields, so a cli/mixed
    # source run has partial/no config drift coverage BY DESIGN — the source
    # makes that applicability machine-visible.
    source: Literal["structured_config", "cli", "mixed", "legacy_defaulted"]
    run_profile: RunProfile
    capability_profile: str
    # r2-4 (ruling 2026-08-04 §2.2): NON-AUTHORITATIVE audit snapshot of the
    # operational RunPolicy toggles recorded at provisioning (require_ep /
    # confirmation_policy / judge_enabled / validation_scope). Audit-only — NEVER
    # authoritative, NEVER consumed for decisions (effective_run_policy sources
    # these from the caller per-invocation, not from here) and never participates
    # in drift detection. Kept so an auditor can see what the operator declared at
    # freeze time; tampering it (even after recomputing content_sha256) cannot
    # change any decision because nothing reads it for decisions.
    context: dict[str, Any] = Field(default_factory=dict)
    legacy_defaulted: bool = False
    policy_hash: Hex64
    content_sha256: Hex64

    @model_validator(mode="after")
    def _canonical_and_hash_consistent(self) -> "RunPolicyRecord":
        if self.capability_profile not in _CAPABILITY_PROFILES:
            raise ValueError(f"invalid capability_profile: {self.capability_profile!r}")
        if self.run_profile not in _RUN_PROFILES:
            raise ValueError(f"invalid run_profile: {self.run_profile!r}")
        expected_hash = _run_policy_hash(self.capability_profile, self.run_profile)
        if self.policy_hash != expected_hash:
            raise ValueError(
                "policy_hash does not match the canonical (capability_profile, "
                "run_profile) hash — record bytes were modified without recomputing"
            )
        recomputed = _content_hash(self.model_dump(mode="json"))
        if self.content_sha256 != recomputed:
            raise ValueError(
                "content_sha256 does not match the canonical payload hash — "
                "record bytes were modified without recomputing"
            )
        if self.source == "legacy_defaulted" and not self.legacy_defaulted:
            raise ValueError("source=legacy_defaulted requires legacy_defaulted=true")
        return self


def _canonical_record_json(record: RunPolicyRecord) -> str:
    return json.dumps(
        record.model_dump(mode="json"),
        indent=2,
        sort_keys=True,
        separators=(",", ": "),
        ensure_ascii=False,
    )


def _atomic_write_text(path: Path, text: str) -> None:
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _build_record(
    *,
    run_profile: str,
    capability_profile: str,
    source: str,
    context: dict[str, Any] | None = None,
    legacy_defaulted: bool = False,
) -> RunPolicyRecord:
    # r2-1 (ruling 2026-08-04): a NEW run (source != legacy_defaulted) may not
    # silently default an ABSENT capability to rectangular here — the resolver
    # (_resolve_run_profiles) is the CLI/legacy authority that fills an absent
    # declaration; if a None reaches this layer for a new run it is a
    # provisioning defect, fail-closed (symmetric with run_profile's
    # run_profile_not_declared). Legacy replay passes its own non-None
    # fallback_capability_profile, so this guard never fires for legacy.
    if source != "legacy_defaulted" and capability_profile is None:
        raise ValueError(
            "capability_profile_not_declared: a new run provisioning must resolve its "
            "capability_profile (structured in run_config.yaml or via CLI) — it may not "
            "silently default to rectangular"
        )
    capability_profile = capability_profile or "rectangular"
    if run_profile is None or run_profile not in _RUN_PROFILES:
        raise ValueError(
            f"run_profile must be one of {_RUN_PROFILES} (got {run_profile!r}); a new "
            "provisioning must declare its tier — it may not silently default to "
            "exploratory (run_profile_not_declared)"
        )
    if capability_profile not in _CAPABILITY_PROFILES:
        raise ValueError(f"capability_profile must be one of {_CAPABILITY_PROFILES}")
    payload = {
        "schema_version": RUN_POLICY_SCHEMA_VERSION,
        "source": source,
        "run_profile": run_profile,
        "capability_profile": capability_profile,
        "context": dict(context or {}),
        "legacy_defaulted": legacy_defaulted,
        "policy_hash": _run_policy_hash(capability_profile, run_profile),
    }
    payload["content_sha256"] = _content_hash(payload)
    return RunPolicyRecord.model_validate(payload)


def _declared_policy(run_dir: Path) -> tuple[str | None, str | None]:
    """Re-derive the structured (run_profile, capability_profile) declared in
    ``run_config.yaml``. Either may be ``None`` when the run pre-dates the
    structured declaration (legacy). Used only for drift re-verification."""
    config_path = Path(run_dir) / "run_config.yaml"
    if not config_path.exists():
        return None, None
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — legacy replay must not crash on a bad yaml
        return None, None
    if not isinstance(raw, dict):
        return None, None
    run_profile = raw.get("run_profile")
    if run_profile is not None:
        run_profile = str(run_profile)
        if run_profile not in _RUN_PROFILES:
            run_profile = None
    capability_profile = raw.get("capability_profile")
    if capability_profile is not None:
        capability_profile = str(capability_profile)
        if capability_profile not in _CAPABILITY_PROFILES:
            capability_profile = None
    return run_profile, capability_profile


def provision_run_policy(
    run_dir: Path | str,
    *,
    run_profile: str | None,
    capability_profile: str | None,
    context: dict[str, Any] | None = None,
    source: str = "structured_config",
) -> RunPolicyRecord:
    """The **only** emitter of ``<run>/_run/run_policy.json``.

    Idempotent — a second call with the same resolved policy returns the
    existing record; a policy change mid-run raises ``run_policy_drift``.
    ``run_profile`` is required (a new provisioning must declare its tier);
    ``None`` raises fail-closed (L-13).

    r2-2: ``source`` reflects where the frozen (run_profile, capability_profile)
    came from (``structured_config`` / ``cli`` / ``mixed``); the production SOP
    path computes it in ``_resolve_run_profiles``. Direct callers default to
    ``structured_config`` (both profiles explicitly supplied)."""
    run_dir = Path(run_dir)
    if run_profile is None:
        raise ValueError(
            "run_profile_not_declared: a new run provisioning must declare its "
            "run_profile (structured in run_config.yaml or via CLI) — it may not "
            "silently default to exploratory"
        )
    expected = _build_record(
        run_profile=run_profile,
        # r2-1: do NOT default an absent capability to rectangular here — a NEW
        # run must carry the resolver's value (config or CLI). _build_record
        # fail-closes (capability_profile_not_declared) if None reaches it.
        capability_profile=capability_profile,
        # r2-2: source reflects the real origin, not a hardcoded constant.
        source=source,
        context=context,
        legacy_defaulted=False,
    )
    path = run_meta_path(run_dir, RUN_POLICY_NAME, for_write=True)
    if path.exists():
        try:
            existing = RunPolicyRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — a corrupt frozen record is a drift fault
            raise ValueError(f"existing run_policy.json at {path} is corrupt: {exc}") from exc
        if existing.legacy_defaulted:
            raise ValueError(
                "run_policy_drift: a legacy_defaulted run_policy.json already exists "
                "but a structured provisioning was requested — refuse to overwrite a "
                "legacy replay record in place"
            )
        if existing.policy_hash != expected.policy_hash:
            raise ValueError(
                "run_policy_drift: the run policy changed after this run was "
                f"provisioned (on-disk policy_hash={existing.policy_hash}, "
                f"requested={expected.policy_hash}) — run policy must not change mid-run"
            )
        return existing
    _atomic_write_text(path, _canonical_record_json(expected))
    return expected


def resolve_frozen_run_policy(
    run_dir: Path | str,
    *,
    fallback_run_profile: str = "exploratory",
    fallback_capability_profile: str = "rectangular",
) -> RunPolicyRecord:
    """The single read-only run-policy consumer (flat-flow + isolation).

    Returns the frozen record after re-verifying it against the current
    ``run_config.yaml`` declaration. A run with no frozen artifact is a legacy
    replay: it resolves to a synthetic ``legacy_defaulted=exploratory`` record
    that is **read-only** — it never fails and never impersonates a strict tier
    (G-6). The ``legacy_defaulted`` flag + ``source`` make "this is a legacy
    default" machine-visible, distinct from a real ``structured_config`` record.
    """
    run_dir = Path(run_dir)
    path = run_meta_path(run_dir, RUN_POLICY_NAME)
    if not path.exists():
        # Legacy replay (predates S-2). Synthetic, read-only, never fails.
        return _build_record(
            run_profile=fallback_run_profile,
            capability_profile=fallback_capability_profile,
            source="legacy_defaulted",
            legacy_defaulted=True,
        )
    try:
        frozen = RunPolicyRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — a corrupt frozen record is a hard fault
        raise ValueError(f"run_policy_drift: frozen run_policy.json is corrupt: {exc}") from exc
    if frozen.legacy_defaulted:
        # A legacy-defaulted artifact on disk is itself read-only replay state.
        return frozen
    # Re-verify against the current structured declaration (drift detection).
    decl_run, decl_cap = _declared_policy(run_dir)
    if decl_run is not None and decl_run != frozen.run_profile:
        raise ValueError(
            f"run_policy_drift: run_config.yaml run_profile={decl_run!r} differs from "
            f"the frozen run_policy.json run_profile={frozen.run_profile!r}"
        )
    if decl_cap is not None and decl_cap != frozen.capability_profile:
        raise ValueError(
            f"run_policy_drift: run_config.yaml capability_profile={decl_cap!r} differs "
            f"from the frozen run_policy.json capability_profile={frozen.capability_profile!r}"
        )
    return frozen


def effective_run_policy(
    run_dir: Path | str,
    *,
    require_ep: bool = False,
    confirmation_policy: "ConfirmationPolicy | str | None" = None,
    judge_enabled: bool = False,
    validation_scope: "ValidationScope | str | None" = None,
):
    """R1-5 + r2-4 (orchestrator ruling 2026-08-04 §2): reconstruct the effective
    :class:`RunPolicy` from the FROZEN TIER plus per-invocation operational
    knobs, so the geometry-confirmation gate (``confirm_geometry`` /
    ``geometry_is_approved``) and ``record_baseline`` judge on the run's declared
    tier instead of ``RunPolicy()`` defaults.

    The tier (``run_profile`` / ``capability_profile``) is the only part with an
    external trust root — it is declared in ``run_config.yaml`` and frozen — so it
    comes from the frozen record (and a legacy run resolves to a visibly-legacy
    default, G-6). The operational knobs (``require_ep`` / ``confirmation_policy``
    / ``judge_enabled`` / ``validation_scope``) are CLI / runtime switches with NO
    external trust root in ``run_config.yaml``. r2-4 (b) rules they are NEVER read
    from the frozen record's ``context`` (a non-authoritative audit snapshot — see
    :attr:`RunPolicyRecord.context`): they are supplied by the caller for the
    current invocation and default to the ``RunPolicy()`` defaults. This removes
    the tamper surface outright — editing ``context`` (even after recomputing this
    file's self-hash) cannot change any decision, because ``context`` is no longer
    a decision input."""
    from src.agent.execution.policy import (
        ConfirmationPolicy,
        RunPolicy,
        ValidationScope,
    )

    record = resolve_frozen_run_policy(run_dir)
    if confirmation_policy is None:
        confirmation_policy = ConfirmationPolicy.OPTIONAL
    elif not isinstance(confirmation_policy, ConfirmationPolicy):
        confirmation_policy = ConfirmationPolicy(confirmation_policy)
    if validation_scope is None:
        validation_scope = ValidationScope.FULL
    elif not isinstance(validation_scope, ValidationScope):
        validation_scope = ValidationScope(validation_scope)
    return RunPolicy(
        run_profile=record.run_profile,
        capability_profile=record.capability_profile,
        confirmation_policy=confirmation_policy,
        judge_enabled=judge_enabled,
        validation_scope=validation_scope,
        require_ep=require_ep,
    )


__all__ = [
    "RUN_POLICY_NAME",
    "RUN_POLICY_SCHEMA_VERSION",
    "RunPolicyRecord",
    "effective_run_policy",
    "provision_run_policy",
    "resolve_frozen_run_policy",
]
