"""Per-run flow configuration loaded from ``<run>/run_config.yaml``.

This file is orchestration metadata only: scope, judge/review switches, model
provenance, and judge-side grade tolerances. Missing or malformed files soft
degrade to the historical defaults so old runs remain replayable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import warnings

import yaml

DEFAULT_STAGES = (
    "0_reading",
    "1_correction",
    "2_modelling",
    "3_split_pairing",
    "4_mep",
    "5_intakeoutput",
)
RUN_CONFIG_NAME = "run_config.yaml"
DEFAULT_WALL_TOL_M = 0.30
DEFAULT_WINDOW_CENTRE_TOL_M = 0.40
DEFAULT_ELEVATION_ALONG_TOL_M = 0.40
DEFAULT_SILL_TOL_M = 0.30
DEFAULT_HEAD_TOL_M = 0.30
DEFAULT_WIDTH_TOL_M = 0.40
DEFAULT_POSITION_TOL_M = DEFAULT_WALL_TOL_M
DEFAULT_EXTENT_TOL_M = 0.30
DEFAULT_COMPLETE_EPS_M = 0.05
DEFAULT_OVERLAP_ACCEPT = 0.75
DEFAULT_OVERLAP_COMPLETE = 0.95
DEFAULT_FLOOR_LINE_TOL_M = 0.30


@dataclass(frozen=True)
class GradeConfig:
    wall_tol_m: float = DEFAULT_WALL_TOL_M
    window_centre_tol_m: float = DEFAULT_WINDOW_CENTRE_TOL_M
    elevation_along_tol_m: float = DEFAULT_ELEVATION_ALONG_TOL_M
    sill_tol_m: float = DEFAULT_SILL_TOL_M
    head_tol_m: float = DEFAULT_HEAD_TOL_M
    width_tol_m: float = DEFAULT_WIDTH_TOL_M
    position_tol_m: float = DEFAULT_POSITION_TOL_M
    extent_tol_m: float = DEFAULT_EXTENT_TOL_M
    complete_eps_m: float = DEFAULT_COMPLETE_EPS_M
    overlap_accept: float = DEFAULT_OVERLAP_ACCEPT
    overlap_complete: float = DEFAULT_OVERLAP_COMPLETE
    floor_line_tol_m: float = DEFAULT_FLOOR_LINE_TOL_M

    def __post_init__(self) -> None:
        vals = self.as_tolerances()
        for name, value in vals.items():
            if value < 0:
                raise ValueError(f"{name} must be nonnegative")
        if self.complete_eps_m > self.extent_tol_m:
            raise ValueError("complete_eps_m must be <= extent_tol_m")
        if self.overlap_accept > self.overlap_complete:
            raise ValueError("overlap_accept must be <= overlap_complete")
        if self.overlap_complete > 1:
            raise ValueError("overlap_complete must be <= 1")

    def as_tolerances(self) -> dict[str, float]:
        return {
            "wall_tol_m": float(self.wall_tol_m),
            "window_centre_tol_m": float(self.window_centre_tol_m),
            "elevation_along_tol_m": float(self.elevation_along_tol_m),
            "sill_tol_m": float(self.sill_tol_m),
            "head_tol_m": float(self.head_tol_m),
            "width_tol_m": float(self.width_tol_m),
            "position_tol_m": float(self.position_tol_m),
            "extent_tol_m": float(self.extent_tol_m),
            "complete_eps_m": float(self.complete_eps_m),
            "overlap_accept": float(self.overlap_accept),
            "overlap_complete": float(self.overlap_complete),
            "floor_line_tol_m": float(self.floor_line_tol_m),
        }


DEFAULT_ORIENTATION_COMPLETION_MODE = "prior_fill"
_ORIENTATION_COMPLETION_MODES = ("prior_fill", "interactive")
_CAPABILITY_PROFILES = ("rectangular", "orthogonal_polygon")
# S-2 (G-3): run_profile is now a *structured declaration* in run_config.yaml
# (durable, hash-bound via run_policy.json), not only a transient CLI flag.
_RUN_PROFILES = ("exploratory", "dev", "golden", "regression")


@dataclass(frozen=True)
class RunConfig:
    path: Path | None = None
    present: bool = False
    scope_stages: tuple[str, ...] = DEFAULT_STAGES
    judge_mode: str = "stop"
    review: dict[str, bool] = field(
        default_factory=lambda: {"reading": False, "correction": False, "geometry": False}
    )
    models: dict[str, object] = field(default_factory=dict)
    grade: dict[str, GradeConfig] = field(
        default_factory=lambda: {
            "reading": GradeConfig(),
            "correction": GradeConfig(),
        }
    )
    # E4 (BO-CR3): run-level orientation completion mode consumed by the
    # §3.2bis resolution step. `prior_fill` (default) mechanically produces
    # the assumed-0 NorthAxisEvidence on a genuinely empty trusted evidence
    # set; `interactive` stops with NEEDS_INPUT instead. The pipeline persists
    # this value as the hash-bound orientation_run_config.json artifact — it
    # never hardcodes a mode literal (BO-CR3).
    orientation_completion_mode: str = DEFAULT_ORIENTATION_COMPLETION_MODE
    # ``None`` means the key was absent and the CLI value remains authoritative.
    capability_profile: str | None = None
    # S-2 (G-3): structured run_profile declaration. ``None`` = the key was
    # absent (legacy run or CLI-authoritative); the freeze layer treats an
    # absent declaration as legacy_defaulted (read-only), and a NEW strict
    # provisioning that fails to declare it fails closed (L-13).
    run_profile: str | None = None
    # R4-a: raw ``reading_mode:`` mapping (lane / dev_function / reading_agent
    # / reading_worker_agent / toolbox_version / isolation_profile), or
    # ``None`` when the key is absent. Left as a raw dict here (parsed +
    # validated by src.agent.execution.reading_mode.provision_reading_mode) —
    # RunConfig only carries the declaration through, it is not the schema
    # authority for this block (mirrors how ``models`` is carried raw).
    reading_mode: dict | None = None

    @classmethod
    def defaults(cls, *, path: Path | None = None, present: bool = False) -> "RunConfig":
        return cls(path=path, present=present)

    def grade_for(self, stage: str) -> GradeConfig:
        key = {"0_reading": "reading", "1_correction": "correction"}.get(stage, stage)
        return self.grade.get(key, GradeConfig())

    def review_stages(self) -> set[str]:
        mapping = {"reading": "0_reading", "correction": "1_correction"}
        return {
            stage
            for key, stage in mapping.items()
            if bool(self.review.get(key))
        }


def load_run_config(run_dir: Path | str) -> RunConfig:
    path = Path(run_dir) / RUN_CONFIG_NAME
    if not path.exists():
        warnings.warn(
            f"{RUN_CONFIG_NAME} not found under {Path(run_dir)}; using flow defaults",
            RuntimeWarning,
            stacklevel=2,
        )
        return RunConfig.defaults(path=path, present=False)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001 - metadata must soft-degrade
        warnings.warn(
            f"could not read {path}: {type(e).__name__}: {e}; using flow defaults",
            RuntimeWarning,
            stacklevel=2,
        )
        return RunConfig.defaults(path=path, present=False)
    if not isinstance(raw, dict):
        warnings.warn(
            f"{path} root is not a mapping; using flow defaults",
            RuntimeWarning,
            stacklevel=2,
        )
        return RunConfig.defaults(path=path, present=False)
    return _parse_run_config(raw, path)


def _parse_run_config(raw: dict, path: Path) -> RunConfig:
    scope_stages = _parse_scope_stages(raw.get("scope"), path)
    judge_mode = _parse_judge_mode(raw.get("judge"), path)
    review = _parse_review(raw.get("review"))
    models = raw.get("models") if isinstance(raw.get("models"), dict) else {}
    grade = {
        "reading": _parse_grade(raw.get("grade"), "reading", path),
        "correction": _parse_grade(raw.get("grade"), "correction", path),
    }
    orientation_completion_mode = _parse_orientation_completion_mode(raw.get("orientation"), path)
    capability_profile = _parse_capability_profile(raw.get("capability_profile"), path)
    run_profile = _parse_run_profile(raw.get("run_profile"), path)
    reading_mode = raw.get("reading_mode") if isinstance(raw.get("reading_mode"), dict) else None
    return RunConfig(
        path=path,
        present=True,
        scope_stages=scope_stages,
        judge_mode=judge_mode,
        review=review,
        models=dict(models),
        grade=grade,
        orientation_completion_mode=orientation_completion_mode,
        capability_profile=capability_profile,
        run_profile=run_profile,
        reading_mode=reading_mode,
    )


def _parse_capability_profile(value: object, path: Path) -> str | None:
    """r2-1 (symmetric with ``_parse_run_profile``, ruling 2026-08-04 §r2-1): a
    structured ``capability_profile`` declaration that is PRESENT but INVALID
    (e.g. a one-letter typo ``orthogonal_polygone``) is fail-closed, not
    warn+ignore. Previously an invalid value warned and returned ``None``, which
    ``_resolve_run_profiles`` then fell back past to the CLI default
    (``rectangular``) — so a single typo silently demoted a declared
    ``orthogonal_polygon`` capability to ``rectangular``. ``capability_profile``
    decides whether correction runs the v2 or v3 schema, so the blast radius is
    wider than judging strictness. An absent key (``value is None``) still
    returns ``None`` so a legacy run with no declaration remains
    CLI-authoritative / legacy_defaulted (G-6). This raises out of
    ``load_run_config`` on every NEW-run provisioning path; the read-only replay
    resolver (``_declared_policy``) reads YAML itself and tolerates a bad value
    as legacy, so historical replays are unaffected."""
    if value is None:
        return None
    profile = str(value)
    if profile not in _CAPABILITY_PROFILES:
        raise ValueError(
            f"capability_profile_invalid: {path} capability_profile={profile!r} is not one of "
            f"{list(_CAPABILITY_PROFILES)} — a present-but-invalid capability declaration may "
            f"not silently fall back to the CLI default (rectangular); fix the "
            f"typo in run_config.yaml"
        )
    return profile


def _parse_run_profile(value: object, path: Path) -> str | None:
    """R1-2 (派工单 §1.2): a structured ``run_profile`` declaration that is
    PRESENT but INVALID (e.g. a one-letter typo ``regresion``) is fail-closed,
    not warn+ignore. Previously an invalid value warned and returned ``None``,
    which ``_resolve_run_profiles`` then fell back past to the CLI
    ``--run-profile`` default (``exploratory``) — so a single typo silently
    demoted a declared strict run. An absent key (``value is None``) still
    returns ``None`` so a legacy run with no declaration remains
    CLI-authoritative / legacy_defaulted (G-6). This raises out of
    ``load_run_config`` on every NEW-run provisioning path (cmd_run/flow/
    resample/provision all call load_run_config before any freeze); the
    read-only replay resolver (``_declared_policy``) reads YAML itself and
    tolerates a bad value as legacy, so historical replays are unaffected."""
    if value is None:
        return None
    profile = str(value)
    if profile not in _RUN_PROFILES:
        raise ValueError(
            f"run_profile_invalid: {path} run_profile={profile!r} is not one of "
            f"{list(_RUN_PROFILES)} — a present-but-invalid tier declaration may "
            f"not silently fall back to the CLI default (exploratory); fix the "
            f"typo in run_config.yaml"
        )
    return profile


def _parse_orientation_completion_mode(orientation: object, path: Path) -> str:
    if not isinstance(orientation, dict):
        return DEFAULT_ORIENTATION_COMPLETION_MODE
    mode = str(orientation.get("completion_mode", DEFAULT_ORIENTATION_COMPLETION_MODE))
    if mode not in _ORIENTATION_COMPLETION_MODES:
        warnings.warn(
            f"{path} orientation.completion_mode={mode!r} is invalid; using "
            f"{DEFAULT_ORIENTATION_COMPLETION_MODE}",
            RuntimeWarning,
        )
        return DEFAULT_ORIENTATION_COMPLETION_MODE
    return mode


def _parse_scope_stages(scope: object, path: Path) -> tuple[str, ...]:
    if not isinstance(scope, dict):
        return DEFAULT_STAGES
    stages = scope.get("stages")
    if not isinstance(stages, list) or not stages:
        return DEFAULT_STAGES
    out = tuple(str(s) for s in stages if str(s) in DEFAULT_STAGES)
    if not out:
        warnings.warn(f"{path} scope.stages has no known stages; using defaults", RuntimeWarning)
        return DEFAULT_STAGES
    return out


def _parse_judge_mode(judge: object, path: Path) -> str:
    if not isinstance(judge, dict):
        return "stop"
    raw = judge.get("mode", "stop")
    if raw is False:
        return "off"
    if raw is True:
        return "stop"
    mode = str(raw)
    if mode not in {"stop", "off"}:
        warnings.warn(f"{path} judge.mode={mode!r} is invalid; using stop", RuntimeWarning)
        return "stop"
    return mode


def _parse_review(review: object) -> dict[str, bool]:
    defaults = {"reading": False, "correction": False, "geometry": False}
    if not isinstance(review, dict):
        return defaults
    return {key: bool(review.get(key, defaults[key])) for key in defaults}


def _parse_grade(grade: object, key: str, path: Path) -> GradeConfig:
    if not isinstance(grade, dict):
        return GradeConfig()
    sec = grade.get(key)
    if not isinstance(sec, dict):
        return GradeConfig()
    return GradeConfig(
        wall_tol_m=float(sec.get("wall_tol_m", DEFAULT_WALL_TOL_M)),
        window_centre_tol_m=float(
            sec.get("window_centre_tol_m", DEFAULT_WINDOW_CENTRE_TOL_M)
        ),
        elevation_along_tol_m=float(
            sec.get("elevation_along_tol_m", DEFAULT_ELEVATION_ALONG_TOL_M)
        ),
        sill_tol_m=float(sec.get("sill_tol_m", DEFAULT_SILL_TOL_M)),
        head_tol_m=float(sec.get("head_tol_m", DEFAULT_HEAD_TOL_M)),
        width_tol_m=float(sec.get("width_tol_m", DEFAULT_WIDTH_TOL_M)),
        position_tol_m=float(
            sec.get("position_tol_m", sec.get("wall_tol_m", DEFAULT_POSITION_TOL_M))
        ),
        extent_tol_m=float(sec.get("extent_tol_m", DEFAULT_EXTENT_TOL_M)),
        complete_eps_m=float(sec.get("complete_eps_m", DEFAULT_COMPLETE_EPS_M)),
        overlap_accept=float(
            sec.get("overlap_accept", sec.get("elevation_overlap_min", DEFAULT_OVERLAP_ACCEPT))
        ),
        overlap_complete=float(sec.get("overlap_complete", DEFAULT_OVERLAP_COMPLETE)),
        floor_line_tol_m=float(sec.get("floor_line_tol_m", DEFAULT_FLOOR_LINE_TOL_M)),
    )
