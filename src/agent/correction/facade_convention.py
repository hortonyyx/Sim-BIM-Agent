"""Single, gt-free source of the North/South/East/West world-frame sign
convention (F-9 route② v2.1 §6.1).

Before this module existed, `facade.py::_CONVENTION`, `window_sources.py::
_BASE_SIGN`, `facade_applicability.py::_BASE_SIGN`, and the judge
`score_inputs.py::_BASE_SIGN` each held an independent, hand-typed copy of
the same four-row (family -> axis, base_sign) table, and five call sites
each independently hand-inlined the identical `mirrored XOR (local_x_positive
== "image_right_to_left")` formula. Four independent copies of one fact is
exactly the "axis B" defect shape this project's CLAUDE.md keeps re-finding
(a real F-9 mirror bug already happened once with this exact table). This
module merges all of it into one source; v2.1 §10 S1 replaces every one of
those local tables/inline formulas with calls into this module, behavior
preserved (v2.1 §12.2 "Convention truth" lock proves it with a hand-written
external truth table, not by calling this module to generate its own
expected values).

Import boundary (v2.1 §11 last paragraph, §2.1 rule 7): this module imports
nothing from the judge package, `case_tests`, `tests`, or any GT path. The
judge-owned score-bindings module is a permitted CONSUMER of this module
(judge importing production is allowed; production/shared importing judge
is not).
"""
from __future__ import annotations

from typing import Literal

FacadeFamily = Literal["North", "South", "East", "West"]
LocalXPositive = Literal["image_left_to_right", "image_right_to_left"]

# (world_axis, base_sign) per cardinal family, looking at the facade FROM
# OUTSIDE, not mirrored, image-local-x running left-to-right (v2.1 §6.1
# table). This is now the ONLY place these four numbers are typed in.
FACADE_WORLD_AXIS: dict[FacadeFamily, Literal["x", "y"]] = {
    "North": "x", "South": "x", "East": "y", "West": "y",
}
FACADE_BASE_SIGN: dict[FacadeFamily, Literal[-1, 1]] = {
    "North": -1, "South": 1, "East": 1, "West": -1,
}
FACADE_OUTWARD_NORMAL: dict[FacadeFamily, tuple[float, float]] = {
    "South": (0.0, -1.0), "North": (0.0, 1.0), "East": (1.0, 0.0), "West": (-1.0, 0.0),
}
# `facade.py::derive_facade_frame`'s legacy bbox-extreme wall-plane wrapper is
# the only consumer of this column (v2.1 §6.1's table also lists it); new
# code (Vg/Va, window-position) must not use a single bbox extreme as a wall
# plane (see that function's own docstring in facade.py).
FACADE_BASE_PLANE_SIDE: dict[FacadeFamily, Literal["x_min", "x_max", "y_min", "y_max"]] = {
    "South": "y_min", "North": "y_max", "East": "x_max", "West": "x_min",
}

MIRROR_STRING_ADAPTER_VERSION = "mirror_string_adapter_v1"


class UnresolvedMirrorError(ValueError):
    """Raised by :func:`normalize_mirror_flag` for a mirror value this
    convention refuses to guess a sign for (v2.1 §6.2): ``"unknown"``,
    ``None``, or any string other than the two legacy literals ``"true"``/
    ``"false"``. Fail closed, never default to not-mirrored (v2.1 §2.1 rule
    10: "无隐式 fallback").
    """

    def __init__(self, value: object):
        self.value = value
        super().__init__(f"unresolved mirror flag: {value!r}")


def normalize_mirror_flag(mirrored: object) -> bool:
    """Bool passes through unchanged. Legacy string ``"true"``/``"false"``
    (exact, case-sensitive) is normalized by this NAMED, versioned adapter
    (``MIRROR_STRING_ADAPTER_VERSION``) — v2.1 §6.2. Anything else
    (``"unknown"``, ``None``, any other string/type) raises
    :class:`UnresolvedMirrorError`.

    This is new S1 scaffolding for the future v3 raw-citation live path
    (S3/S4); none of today's four merged call sites feed it a raw string —
    they already resolve to a real bool upstream — so wiring it in does not
    change any existing consumer's behavior.
    """
    if isinstance(mirrored, bool):
        return mirrored
    if mirrored == "true":
        return True
    if mirrored == "false":
        return False
    raise UnresolvedMirrorError(mirrored)


def world_axis(facade_family: str) -> Literal["x", "y"]:
    if facade_family not in FACADE_WORLD_AXIS:
        raise ValueError(f"unknown facade_family {facade_family!r}")
    return FACADE_WORLD_AXIS[facade_family]


def resolve_sign(facade_family: str, *, mirrored: bool, local_x_positive: str) -> Literal[-1, 1]:
    """The single ``effective_flip = mirrored XOR (local_x_positive ==
    "image_right_to_left")`` formula (v2.1 §6). Requires an already-resolved
    bool ``mirrored`` — call :func:`normalize_mirror_flag` first if the
    caller's ``mirrored`` might still be a string; this function itself does
    not accept anything but ``bool`` and raises ``TypeError`` otherwise (same
    fail-closed posture the merged call sites already had before S1).
    """
    if facade_family not in FACADE_BASE_SIGN:
        raise ValueError(f"unknown facade_family {facade_family!r}")
    if not isinstance(mirrored, bool):
        raise TypeError(
            "resolve_sign requires an already-resolved bool 'mirrored'; "
            "call normalize_mirror_flag first"
        )
    if local_x_positive not in ("image_left_to_right", "image_right_to_left"):
        raise ValueError(f"unknown local_x_positive {local_x_positive!r}")
    base_sign = FACADE_BASE_SIGN[facade_family]
    effective_flip = mirrored ^ (local_x_positive == "image_right_to_left")
    return -base_sign if effective_flip else base_sign


def project_affine_interval(*, along_origin: float, sign: int, local_lo: float, local_hi: float) -> tuple[float, float]:
    """v2.1 §6.3's single elevation affine projector: ``world = along_origin
    + sign * local``. Not wired into any live consumer by S1 (S2 owns the
    authoritative/advisory frame split that consumes this); it lives here so
    S2 does not need a second copy of the formula.
    """
    a = along_origin + sign * local_lo
    b = along_origin + sign * local_hi
    return (a, b) if a <= b else (b, a)


__all__ = [
    "FACADE_WORLD_AXIS", "FACADE_BASE_SIGN", "FACADE_OUTWARD_NORMAL", "FACADE_BASE_PLANE_SIDE",
    "MIRROR_STRING_ADAPTER_VERSION", "UnresolvedMirrorError",
    "normalize_mirror_flag", "world_axis", "resolve_sign", "project_affine_interval",
]
