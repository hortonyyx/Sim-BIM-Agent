"""R4-a: reading-stage lane/provenance record ("who does the reading score
belong to").

CLAUDE.md §1.5 #7 is the sole authority for the accounting vocabulary this
module encodes — copied verbatim, not re-derived:

  - Exactly two official lanes: ``autonomous`` (target VLM + frozen toolbox,
    ZERO ``reading-agent``) and ``controlled`` (a ``reading-agent`` is
    present). ``controlled`` counts as real engineering success but must
    never be recorded as "a weak model succeeded on its own".
  - A separate dev-time function (``dev_function``) exists (tool invention /
    method extraction) — it is NOT a third lane and produces NO official
    score. It must never be written as a third parallel lane value.

This module mirrors the "declare -> freeze -> resolve" transaction already
used by :mod:`src.agent.execution.run_policy_freeze` for the same reason that
module exists: a knob that decides how a result may be reported needs a
single writer, a single read-only consumer, and a legacy-safe fallback that
never fails a read-only replay (G-6 style) while still fail-closing a NEW
declaration that is missing or malformed.

Placement rationale (recorded here because the dispatch left the mount point
to this executor's judgment, see AI_agent/logs/reviews/execution/
2026-08-04_batchD_R4a_claude.md):

  - ``reading_mode`` is declared as an OPTIONAL ``reading_mode:`` section of
    ``run_config.yaml`` (:class:`~src.agent.execution.run_config.RunConfig`),
    the same file that already carries ``run_profile`` / ``capability_profile``
    / ``models``.  This keeps the declaration next to the other structured,
    hash-bound run-level knobs an operator already edits per run.
  - The record is frozen into ``<run>/_run/reading_mode.json`` by
    :func:`provision_reading_mode`, the only writer.
  - :func:`resolve_reading_mode` is the only read-only consumer.  Absence of
    the frozen artifact is NEVER an error for a read-only replay — it
    resolves to ``status="legacy_unknown"`` (L-R3): a run that predates this
    provenance block must not be judged non-compliant, and must not be made
    to impersonate either lane.
  - :func:`require_reading_mode` is the fail-closed consumer (L-R2): it calls
    :func:`resolve_reading_mode` and raises if the run has no frozen record.
    It is wired into ``record_baseline()`` (``scripts/tool_scripts/
    record_baseline.py``) — the single place in this codebase where a run's
    scores become an official, reported result (the ``记录这次跑 <case> <tag>``
    ritual, CLAUDE.md §6 #12) — gated behind a ``require_reading_mode``
    keyword that defaults to ``False`` so the 27 pre-existing direct-function
    callers in ``tests/test_orchestrate_baseline.py`` /
    ``tests/test_provenance_baseline.py`` (none of which declare
    ``reading_mode``, and all of which predate this feature) are completely
    unaffected. The one CLI path that flips it to ``True`` —
    ``run_stage.py flow --record`` — had ZERO existing test coverage before
    this change (verified by grep across ``tests/``), so making that single
    real entry point strict is a zero-regression, genuinely fail-closed gate
    for every NEW run recorded through the documented ritual going forward.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from src.agent.execution.run_meta import run_meta_path

READING_MODE_NAME = "reading_mode.json"
READING_MODE_SCHEMA_VERSION = "1"

LANE_VALUES = ("autonomous", "controlled")
LEGACY_UNKNOWN = "legacy_unknown"


class ReadingAgentInfo(BaseModel):
    """The in-场 (present) reading-agent for a ``controlled``-lane run."""

    model_config = ConfigDict(extra="forbid")

    model: str
    sees_images: bool
    rework_rounds: int

    @model_validator(mode="after")
    def _rework_rounds_nonnegative(self) -> "ReadingAgentInfo":
        if self.rework_rounds < 0:
            raise ValueError("rework_rounds must be >= 0")
        return self


class ReadingWorkerAgentInfo(BaseModel):
    """The reading-worker-agent (the VLM that actually produces strokes)."""

    model_config = ConfigDict(extra="forbid")

    model: str
    effort: str


class ReadingModeRecord(BaseModel):
    """Frozen reading_mode provenance for one run (R4-a)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = READING_MODE_SCHEMA_VERSION
    lane: Literal["autonomous", "controlled"]
    dev_function: bool
    reading_agent: ReadingAgentInfo | None = None
    reading_worker_agent: ReadingWorkerAgentInfo
    toolbox_version: str
    isolation_profile: str

    @model_validator(mode="after")
    def _lane_reading_agent_consistent(self) -> "ReadingModeRecord":
        # CLAUDE.md §1.5 #7: autonomous IS DEFINED as "zero reading-agent";
        # controlled IS DEFINED as "a reading-agent is present". A record that
        # contradicts its own lane definition is not a legal declaration.
        if self.lane == "autonomous" and self.reading_agent is not None:
            raise ValueError(
                "reading_mode_lane_contract_violation: lane=autonomous requires "
                "reading_agent=null (autonomous is defined as zero reading-agent)"
            )
        if self.lane == "controlled" and self.reading_agent is None:
            raise ValueError(
                "reading_mode_lane_contract_violation: lane=controlled requires a "
                "reading_agent record (controlled is defined as reading-agent present)"
            )
        return self


class ReadingModeResolution(BaseModel):
    """Result of resolving reading_mode for a run.

    ``status="legacy_unknown"`` is a distinct, first-class outcome (L-R3) —
    it is never coerced into either lane value, and callers must not treat it
    as equivalent to ``autonomous`` (the exact failure mode R4-a exists to
    prevent — "记成弱模型独立满分").
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["present", "legacy_unknown"]
    record: ReadingModeRecord | None = None

    @model_validator(mode="after")
    def _status_record_consistent(self) -> "ReadingModeResolution":
        if self.status == "present" and self.record is None:
            raise ValueError("status=present requires a record")
        if self.status == "legacy_unknown" and self.record is not None:
            raise ValueError("status=legacy_unknown must not carry a record")
        return self


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


def _canonical_record_json(record: ReadingModeRecord) -> str:
    return json.dumps(
        record.model_dump(mode="json"),
        indent=2,
        sort_keys=True,
        separators=(",", ": "),
        ensure_ascii=False,
    )


def provision_reading_mode(
    run_dir: Path | str,
    *,
    declared: dict | None,
) -> ReadingModeRecord:
    """The **only** writer of ``<run>/_run/reading_mode.json``.

    ``declared`` is the raw ``reading_mode:`` mapping read from
    ``run_config.yaml`` (or an equivalently-shaped dict from a direct caller).
    ``None`` (the section is absent) fail-closes (L-R2) — a NEW provisioning
    may not silently proceed without a lane declaration, exactly mirroring
    :func:`~src.agent.execution.run_policy_freeze.provision_run_policy`'s
    ``run_profile=None`` guard.

    Idempotent: a second call with a byte-identical declaration returns the
    existing frozen record. A declaration that resolves to a DIFFERENT record
    than what is already frozen raises ``reading_mode_drift`` — reading_mode
    must not change mid-run, same rationale as run_policy.
    """
    run_dir = Path(run_dir)
    if declared is None:
        raise ValueError(
            "reading_mode_not_declared: a new run recorded through the official "
            "ritual must declare a reading_mode: section in run_config.yaml "
            "(lane / dev_function / reading_agent / reading_worker_agent / "
            "toolbox_version / isolation_profile) — it may not silently be "
            "recorded as an unlabeled or default lane"
        )
    try:
        expected = ReadingModeRecord.model_validate(declared)
    except ValidationError as exc:
        raise ValueError(f"reading_mode_invalid: {exc}") from exc

    path = run_meta_path(run_dir, READING_MODE_NAME, for_write=True)
    if path.exists():
        try:
            existing = ReadingModeRecord.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"existing reading_mode.json at {path} is corrupt: {exc}"
            ) from exc
        if existing != expected:
            raise ValueError(
                "reading_mode_drift: the declared reading_mode changed after this "
                f"run was provisioned (on-disk={existing.model_dump(mode='json')}, "
                f"requested={expected.model_dump(mode='json')}) — reading_mode must "
                "not change mid-run"
            )
        return existing
    _atomic_write_text(path, _canonical_record_json(expected))
    return expected


def resolve_reading_mode(run_dir: Path | str) -> ReadingModeResolution:
    """The single read-only consumer for report/replay callers.

    Never raises on absence (G-6 style): a run with no frozen
    ``reading_mode.json`` is a legacy replay — predates this provenance block
    entirely, or never opted in — and resolves to
    ``status="legacy_unknown"`` (L-R3). A CORRUPT on-disk record (present but
    unparseable) is a genuine integrity fault and does raise, same as
    :func:`~src.agent.execution.run_policy_freeze.resolve_frozen_run_policy`.
    """
    run_dir = Path(run_dir)
    path = run_meta_path(run_dir, READING_MODE_NAME)
    if not path.exists():
        return ReadingModeResolution(status="legacy_unknown", record=None)
    try:
        record = ReadingModeRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"reading_mode.json is corrupt at {path}: {exc}") from exc
    return ReadingModeResolution(status="present", record=record)


def require_reading_mode(run_dir: Path | str) -> ReadingModeRecord:
    """Fail-closed consumer (L-R2): raise unless reading_mode is resolvable.

    Callers that need to attach an official lane label to a NEW run's score
    (currently: ``record_baseline`` when invoked with
    ``require_reading_mode=True``) must go through this function rather than
    inventing their own "missing means autonomous" fallback — that fallback is
    exactly the failure mode this module exists to prevent.
    """
    resolution = resolve_reading_mode(run_dir)
    if resolution.status != "present" or resolution.record is None:
        raise ValueError(
            "reading_mode_missing: this run has no frozen reading_mode.json — "
            "a score may not be recorded as an official result without a "
            "declared lane (run_stage.py flow --record requires a "
            "reading_mode: section in run_config.yaml; see CLAUDE.md §1.5 #7)"
        )
    return resolution.record


__all__ = [
    "READING_MODE_NAME",
    "READING_MODE_SCHEMA_VERSION",
    "LANE_VALUES",
    "LEGACY_UNKNOWN",
    "ReadingAgentInfo",
    "ReadingWorkerAgentInfo",
    "ReadingModeRecord",
    "ReadingModeResolution",
    "provision_reading_mode",
    "resolve_reading_mode",
    "require_reading_mode",
]
