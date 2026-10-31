"""Deterministic-core tolerance config loader.

Single source for the geometry-correction tolerances consumed by
`deterministic.py`. Values live in `src/configs/correction.yaml` (basis tracked
in PartA-correction/A0_contract.md §4), not hardcoded in the core, so:

  - there is one place to change them (no Python-constant vs A0-doc drift), and
  - a test run can pin its own granularity via `$EP_AGENT_CORRECTION_CONFIG`
    (mirrors `$EP_AGENT_LLM_CONFIG`), without editing the shared file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from omegaconf import OmegaConf

CORRECTION_CONFIG_ENV: str = "EP_AGENT_CORRECTION_CONFIG"

_DEFAULT_CONFIG: Path = (
    Path(__file__).resolve().parent.parent.parent / "configs" / "correction.yaml"
)


def resolve_correction_config_path() -> Path:
    """Active config file: `$EP_AGENT_CORRECTION_CONFIG` if set, else the default."""
    override = os.environ.get(CORRECTION_CONFIG_ENV)
    if override:
        p = Path(override)
        if not p.is_file():
            raise FileNotFoundError(
                f"{CORRECTION_CONFIG_ENV}={override} does not point to a file"
            )
        return p
    return _DEFAULT_CONFIG


@dataclass(frozen=True)
class CoreTolerances:
    """Resolved tolerances for one deterministic-core run (all lengths in metres).

    See `src/configs/correction.yaml` for the per-field constraint each governs.
    """

    axis_jitter_tol_m: float  # same-axis identity clustering threshold
    cross_floor_align_tol_m: float  # conservative cross-floor same-wall alignment threshold
    structural_snap_grid_m: float  # grid for room/wall/footprint axes (post-cluster)
    min_edge_length_m: float  # sliver floor; no two canonical axes closer than this
    output_precision_m: float  # format precision + correction-logging threshold
    window_snap_grid_m: float  # finer grid for window span/z (no structural role)
    window_clamp_to_parent: bool  # clamp window span/z into its parent cell/floor
    envelope_reconcile_tol_m: float  # facade-envelope vs footprint basis correction threshold
    gap_close_threshold_m: float  # auto-close a cell edge this close to the footprint
    gap_arbitration_band_m: float  # above gap_close, escalate to A3 (doc/A3; not code)
    coverage_area_tol_m2: float  # B3 cells-union vs footprint area-conservation threshold
    # B2b envelope transform contracts.  Required deliberately: a missing
    # shipped-config value must fail before a v3 transform can run.
    envelope_axis_attach_tol_m: float
    envelope_endpoint_match_tol_m: float
    envelope_candidate_agreement_tol_m: float
    # Vg (C2 §10.1, rework CR5): these two carry NO dataclass default — every
    # direct `CoreTolerances(...)` construction (shipped-config loader or a
    # test helper) must pass them explicitly; a silent 1e-9 fallback here
    # would let a caller run Vg's degeneracy guards without ever having
    # chosen a value. They must sit ahead of any defaulted field below
    # (dataclass field-ordering rule: non-default fields cannot follow a
    # defaulted one).
    facade_visibility_depth_epsilon_m: float  # Vg same-depth tie / negative-depth INVARIANT guard
    facade_visibility_endpoint_epsilon_m: float  # Vg half-open sweep near-endpoint/short-edge INVARIANT guard
    # B5 window-host resolver.  These deliberately have no defaults: B5 must
    # never silently borrow a Vg or legacy window tolerance.
    window_segment_endpoint_clamp_tol_m: float
    window_host_span_epsilon_m: float
    window_host_plane_epsilon_m: float
    facade_frame_cross_check_tol_m: float = 0.30  # reading facade-frame vs LLM window placement flag threshold

    def validate(self) -> None:
        """Guard the cross-field invariants the config comments promise."""
        if not (0 < self.structural_snap_grid_m <= self.min_edge_length_m):
            raise ValueError(
                "structural_snap_grid_m must be in (0, min_edge_length_m]; got "
                f"{self.structural_snap_grid_m} vs min_edge {self.min_edge_length_m}"
            )
        if self.axis_jitter_tol_m <= 0 or self.window_snap_grid_m <= 0:
            raise ValueError("jitter tol and window grid must be positive")
        if self.envelope_reconcile_tol_m <= self.cross_floor_align_tol_m:
            raise ValueError(
                "envelope_reconcile_tol_m must be greater than cross_floor_align_tol_m; got "
                f"{self.envelope_reconcile_tol_m} vs {self.cross_floor_align_tol_m}"
            )
        if self.facade_frame_cross_check_tol_m <= 0:
            raise ValueError("facade_frame_cross_check_tol_m must be positive")
        if self.coverage_area_tol_m2 <= 0:
            raise ValueError("coverage_area_tol_m2 must be positive")
        if not (0 < self.envelope_axis_attach_tol_m <= self.envelope_endpoint_match_tol_m
                <= self.envelope_reconcile_tol_m):
            raise ValueError(
                "must hold 0 < envelope_axis_attach_tol_m <= "
                "envelope_endpoint_match_tol_m <= envelope_reconcile_tol_m"
            )
        if not (0 < self.envelope_candidate_agreement_tol_m <= self.envelope_reconcile_tol_m):
            raise ValueError(
                "envelope_candidate_agreement_tol_m must be in "
                "(0, envelope_reconcile_tol_m]"
            )
        # Vg (C2 §10.1): numeric-equivalence/degeneracy guards only, never a
        # measurement tolerance — must sit strictly inside the finer of the two
        # snap/edge floors so they never mask or duplicate that guard's job.
        if not (0 < self.facade_visibility_depth_epsilon_m < self.structural_snap_grid_m):
            raise ValueError(
                "facade_visibility_depth_epsilon_m must be in (0, structural_snap_grid_m); got "
                f"{self.facade_visibility_depth_epsilon_m} vs {self.structural_snap_grid_m}"
            )
        if not (0 < self.facade_visibility_endpoint_epsilon_m < self.min_edge_length_m):
            raise ValueError(
                "facade_visibility_endpoint_epsilon_m must be in (0, min_edge_length_m); got "
                f"{self.facade_visibility_endpoint_epsilon_m} vs {self.min_edge_length_m}"
            )
        if not (0 < self.window_host_plane_epsilon_m <= self.window_host_span_epsilon_m):
            raise ValueError(
                "must hold 0 < window_host_plane_epsilon_m <= window_host_span_epsilon_m"
            )
        if not (self.window_host_span_epsilon_m < self.window_segment_endpoint_clamp_tol_m
                <= self.min_edge_length_m):
            raise ValueError(
                "must hold window_host_span_epsilon_m < "
                "window_segment_endpoint_clamp_tol_m <= min_edge_length_m"
            )
        # cross-floor identity is coarser than per-floor jitter, and connectivity
        # is coarser still but below the arbitration band.
        if not (self.axis_jitter_tol_m < self.cross_floor_align_tol_m
                < self.gap_close_threshold_m
                < self.gap_arbitration_band_m):
            raise ValueError(
                "must hold axis_jitter_tol < cross_floor_align_tol < "
                "gap_close_threshold < gap_arbitration_band; got "
                f"{self.axis_jitter_tol_m} / {self.cross_floor_align_tol_m} / "
                f"{self.gap_close_threshold_m} / {self.gap_arbitration_band_m}"
            )


@lru_cache(maxsize=8)
def _load_cached(path_str: str) -> CoreTolerances:
    raw = OmegaConf.load(path_str)
    data = OmegaConf.to_container(raw, resolve=True)
    assert isinstance(data, dict), "correction.yaml must be a mapping"
    c = data.get("correction", data)  # tolerate a flat layout too
    tol = CoreTolerances(
        axis_jitter_tol_m=float(c["axis_jitter_tol_m"]),
        cross_floor_align_tol_m=float(c["cross_floor_align_tol_m"]),
        structural_snap_grid_m=float(c["structural_snap_grid_m"]),
        min_edge_length_m=float(c["min_edge_length_m"]),
        output_precision_m=float(c["output_precision_m"]),
        window_snap_grid_m=float(c["window_snap_grid_m"]),
        window_clamp_to_parent=bool(c.get("window_clamp_to_parent", True)),
        envelope_reconcile_tol_m=float(c["envelope_reconcile_tol_m"]),
        facade_frame_cross_check_tol_m=float(c["facade_frame_cross_check_tol_m"]),
        gap_close_threshold_m=float(c["gap_close_threshold_m"]),
        gap_arbitration_band_m=float(c["gap_arbitration_band_m"]),
        coverage_area_tol_m2=float(c["coverage_area_tol_m2"]),
        envelope_axis_attach_tol_m=float(c["envelope_axis_attach_tol_m"]),
        envelope_endpoint_match_tol_m=float(c["envelope_endpoint_match_tol_m"]),
        envelope_candidate_agreement_tol_m=float(c["envelope_candidate_agreement_tol_m"]),
        facade_visibility_depth_epsilon_m=float(c["facade_visibility_depth_epsilon_m"]),
        facade_visibility_endpoint_epsilon_m=float(c["facade_visibility_endpoint_epsilon_m"]),
        window_segment_endpoint_clamp_tol_m=float(c["window_segment_endpoint_clamp_tol_m"]),
        window_host_span_epsilon_m=float(c["window_host_span_epsilon_m"]),
        window_host_plane_epsilon_m=float(c["window_host_plane_epsilon_m"]),
    )
    tol.validate()
    return tol


def load_core_tolerances() -> CoreTolerances:
    """Load the active deterministic-core tolerances (cached per resolved path)."""
    return _load_cached(str(resolve_correction_config_path()))
