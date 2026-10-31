"""Deterministic CV probes for reading-stage audit evidence."""

from .recipes import CLEAN_VECTOR_V1, get_recipe
from .sidecar import allocate_sidecar_path, write_sidecar
from .tools import (
    crop_zoom,
    overlay_logger,
    px_m_calibrator,
    storey_line_profiler,
    wall_line_profiler,
    window_cc_detector,
)

__all__ = [
    "CLEAN_VECTOR_V1",
    "allocate_sidecar_path",
    "crop_zoom",
    "get_recipe",
    "overlay_logger",
    "px_m_calibrator",
    "storey_line_profiler",
    "wall_line_profiler",
    "window_cc_detector",
    "write_sidecar",
]
