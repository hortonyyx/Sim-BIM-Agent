"""Single source for CV toolbox recipe constants.

2026-08-19: the prescan macro recipes (``prescan_plan`` / ``prescan_elevation``) and
their ~840 lines of candidate-generation machinery were WITHDRAWN FROM THE WORKING
TREE on the user's ruling ("从现在的工作环境撤掉，不是说就永远不要了"). They had been in a half-dead state since
2026-08-15: the implementation was still here while the authorization table
(``run_cv_probe.ALLOWED_TOOLS``) no longer listed them, so the reader could not call
them at all and only the orchestrator could pre-stage their output. Nothing was lost: the full code, tests and restore
procedure are archived under ``AI_agent/archive/reading_prescan/``
(byte-identical to commit 0cfa289). Whether prescan comes back — and in what form —
is a reading-专项 decision, to be taken together with the "calibration anchors must be
a tool-emitted candidate_id" fix, because prescan's tick_candidate is exactly the
machine-detected tick source that fix needs.
"""

from __future__ import annotations

from copy import deepcopy


CLEAN_VECTOR_V1 = {
    "recipe_id": "clean_vector_v1",
    "applicability": "clean_vector",
    # Seeded from the sm21 forensics recipe: R ~= G ~= B and 60 < v < 230.
    # The exact inclusive bounds follow the adjudicated mask contract.
    "gray_lo": 60,
    "gray_hi": 230,
    "rgb_tol": 8,
    # Low enough for full-image sm21 plan smoke, high enough to suppress small
    # gray labels/ticks in synthetic fixtures after orthogonal normalization.
    "prominence": 0.04,
    "min_peak_distance_px": 6,
    "min_cc_area_px": 20,
    "merge_gap_px": 2,
    "merge_overlap_ratio": 0.5,
    "merge_iou": 0.2,
    "calibration_warn_residual_px": 2.0,
    "calibration_warn_residual_m": 0.05,
    # Measured clean-vector ceiling: accepted two-axis sidecars top out at
    # 0.138%, while an independently valid 1 px endpoint convention measured
    # 0.28%.  The rounded-up 0.30% ceiling still rejects the confirmed 1.92%
    # wrong-control-point case (execution log, 2026-07-31 G-2).
    "calibration_max_axis_relative_deviation": 0.003,
    "calibration_foreground_delta": 24,
    "calibration_min_line_px": 12,
    "calibration_min_span_px": 30,
    "calibration_intersection_tolerance_px": 2,
    "calibration_intersection_merge_px": 4,
}

_RECIPES = {
    "clean_vector_v1": CLEAN_VECTOR_V1,
}


def get_recipe(recipe_id: str = "clean_vector_v1") -> dict:
    """Return a copy of a known deterministic recipe."""

    try:
        return deepcopy(_RECIPES[recipe_id])
    except KeyError as exc:
        known = ", ".join(sorted(_RECIPES))
        raise ValueError(f"unknown CV recipe {recipe_id!r}; known recipes: {known}") from exc


TOOL_VERSION = "1"
