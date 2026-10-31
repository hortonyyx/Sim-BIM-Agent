"""Deterministic arithmetic for model-observed dimension chains, without OCR."""
from __future__ import annotations

import math


def map_dimension_chain(lengths: list[float], *, unit: str = "mm",
                        origin_m: float = 0.0, direction: int = 1,
                        expected_total: float | None = None) -> dict:
    """Accumulate observed lengths into world-metre intervals in either direction.

    Segment identity, text transcription, origin and direction remain the
    caller's evidence/assumptions. A closed sum does not verify the drawing.
    ``expected_total`` uses the same unit as ``lengths``.
    """
    def finite(value):
        return type(value) in (int, float) and math.isfinite(value)

    if unit not in {"mm", "m"}:
        raise ValueError("unit must be mm or m")
    if not isinstance(lengths, list) or not lengths or any(not finite(v) or v < 0 for v in lengths):
        raise ValueError("lengths must be a nonempty list of finite nonnegative numbers")
    if not finite(origin_m) or type(direction) is not int or direction not in {-1, 1}:
        raise ValueError("finite origin_m and direction +1 or -1 required")
    if expected_total is not None and (not finite(expected_total) or expected_total <= 0):
        raise ValueError("expected_total must be finite and positive")
    factor = 0.001 if unit == "mm" else 1.0
    values = [v * factor for v in lengths]
    segments = []
    for index, length in enumerate(values):
        start = origin_m + direction * math.fsum(values[:index])
        end = origin_m + direction * math.fsum(values[:index + 1])
        segments.append({"index": index, "length_m": round(length, 9),
                         "start_m": round(start, 9), "end_m": round(end, 9),
                         "span_m": [round(min(start, end), 9), round(max(start, end), 9)]})
    total = math.fsum(values)
    return {
        "input_unit": unit, "origin_m": origin_m, "direction": direction,
        "segments": segments, "total_m": round(total, 9),
        "end_m": round(origin_m + direction * total, 9),
        "expected_total_m": None if expected_total is None else expected_total * factor,
        "closure_error_m": None if expected_total is None else round(total - expected_total * factor, 9),
        "evidence_status": "caller_supplied_dimensions_and_frame_not_independently_verified",
        "scope": "arithmetic only; no image interpretation, opening classification, or geometry mutation",
    }
