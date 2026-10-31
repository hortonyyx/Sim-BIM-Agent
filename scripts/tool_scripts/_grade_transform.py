"""Shared metric-to-pixel transform for gt grade renders."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricTransform:
    min_x: float
    min_y: float
    max_x: float
    max_y: float
    scale: float
    offset_x: float = 0.0
    offset_y: float = 0.0
    flip_y: bool = True

    @property
    def width_px(self) -> int:
        return max(1, int(round((self.max_x - self.min_x) * self.scale)))

    @property
    def height_px(self) -> int:
        return max(1, int(round((self.max_y - self.min_y) * self.scale)))

    def px(self, x: float, y: float) -> tuple[float, float]:
        px = self.offset_x + (float(x) - self.min_x) * self.scale
        if self.flip_y:
            py = self.offset_y + (self.max_y - float(y)) * self.scale
        else:
            py = self.offset_y + (float(y) - self.min_y) * self.scale
        return px, py

    def rect(self, x0: float, y0: float, x1: float, y1: float) -> list[float]:
        a = self.px(x0, y0)
        b = self.px(x1, y1)
        return [min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])]


def plan_transform(
    W: float,
    D: float,
    *,
    scale: float = 48.0,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    margin_m: float = 0.8,
) -> MetricTransform:
    return MetricTransform(
        min_x=-margin_m,
        min_y=-margin_m,
        max_x=float(W) + margin_m,
        max_y=float(D) + margin_m,
        scale=scale,
        offset_x=offset_x,
        offset_y=offset_y,
        flip_y=True,
    )
