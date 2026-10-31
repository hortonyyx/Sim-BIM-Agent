"""Facade image-local → world translation (M2a / S1).

P1b puts world placement squarely in 1_correction: 0_reading emits only
image-local facade orientation (``view_facade`` / ``local_x_positive`` /
``mirrored``); this module derives the **world** frame (axis, sign, base plane)
from the authoritative facade name + the reconciled footprint + the z-stack, and
translates an along-facade image-local coordinate into a world coordinate.

The world frame uses the standard architectural elevation convention — looking at
a facade *from outside*:

  facade  wall plane           along-facade local-x → world      outward normal
  ──────  ───────────────────  ───────────────────────────────  ──────────────
  South   y = footprint_y_min  west→east  (+x)                   −y
  North   y = footprint_y_max  east→west  (−x)                   +y
  East    x = footprint_x_max  south→north (+y)                  +x
  West    x = footprint_x_min  north→south (−y)                  −x

``mirrored=true`` flips the sign (the drawing is reversed left-for-right). Sign is
NOT taken from a VLM self-declaration: it comes from the convention + the mirror
flag, and the cross-image reconcile + window-on-wall checks (validator/checks/
correction.py) catch a wrong sign downstream.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.agent.correction import facade_convention

FacadeFamily = Literal["North", "South", "East", "West"]

# F-9 route② S1 (2026-08-11): the (world_axis, base_plane_side, base_sign)
# table used to be a hand-typed literal here; it is now sourced from
# `facade_convention` (the single, gt-free convention module — see that
# module's docstring for why four independent copies of this table was a
# defect shape this project already paid for once). `base_plane_side` is
# only used by this file's legacy `derive_facade_frame` wrapper below.
FACADE_NORMAL = facade_convention.FACADE_OUTWARD_NORMAL


@dataclass(frozen=True)
class FacadeWorldFrame:
    """Deterministic world frame for one facade, derived from image-local data."""

    facade: str
    world_axis: str          # "x" or "y" — the along-facade world axis
    sign: int                # +1 / -1 — image-left→right maps to this world dir
    base_world: float        # the constant world coordinate of the wall plane
    along_origin: float      # world coord that image-local-x = 0 maps to
    normal: tuple[float, float]

    def to_world_along(self, local_x: float) -> float:
        """Translate an image-local along-facade x to a world coordinate."""
        return self.along_origin + self.sign * local_x


def _is_mirrored(mirrored) -> bool:
    # F-9 route② MINOR-3 debt (2026-08-11, sol verdict
    # AI_agent/logs/reviews/verdict/2026-08-11_f22_f9s0s1_crossreview_sol.md):
    # this LEGACY, case-insensitive coercion ("TRUE"/"true"/True all -> True,
    # anything else -> False, never raises) is deliberately DIFFERENT from
    # `window_sources.py::_resolve_facade_flip_fields`'s coercion (only a
    # real bool is honored; ANY string, including "true", silently becomes
    # False) and from `facade_convention.normalize_mirror_flag`'s strict
    # typed-fail-closed adapter (only bool or the exact strings "true"/
    # "false"; everything else raises `UnresolvedMirrorError`). Three
    # DIFFERENT coercions of the same fact is a real latent defect (sol
    # confirmed this project's own materials show it: the same
    # `mirrored="true"` string produces opposite bools depending which of
    # this function vs `_resolve_facade_flip_fields` reads it), NOT
    # something S1 is fixing now: sol also confirmed unifying it here by
    # switching to the strict adapter would be a genuine BEHAVIOR CHANGE
    # (unknown/garbage values currently return False silently; the strict
    # adapter raises), which v2.1 §10 S1's "行为保持不变" rule forbids.
    # `test_f9_route2_s1_convention_truth.py::
    # test_minor3_legacy_mirror_coercions_disagree_is_a_pinned_debt_fact`
    # pins the disagreement as an executable fact so it cannot silently
    # widen or narrow without that test failing. Cutover plan: S3/S4's new
    # live v3 raw-citation boundary must use ONLY the strict adapter and
    # fail closed; this lenient legacy coercion stays scoped to the
    # rectangular/`derive_facade_frame` profile it has always served.
    if isinstance(mirrored, bool):
        return mirrored
    return str(mirrored).lower() == "true"


def derive_facade_frame(
    *,
    view_facade: str,
    footprint_x: list[float],
    footprint_y: list[float],
    mirrored="false",
    local_x_positive: str = "image_left_to_right",
) -> FacadeWorldFrame:
    """Derive the world frame for an elevation from image-local orientation +
    the reconciled footprint. Raises on an unknown facade."""
    if view_facade not in facade_convention.FACADE_WORLD_AXIS:
        raise ValueError(f"unknown view_facade '{view_facade}'")
    axis = facade_convention.FACADE_WORLD_AXIS[view_facade]
    base_side = facade_convention.FACADE_BASE_PLANE_SIDE[view_facade]
    xmin, xmax = min(footprint_x), max(footprint_x)
    ymin, ymax = min(footprint_y), max(footprint_y)

    base_world = {
        "x_min": xmin, "x_max": xmax, "y_min": ymin, "y_max": ymax,
    }[base_side]

    # F-9 route② S1 rework (2026-08-11, sol MAJOR-3): this used to compute
    # `sign` with two sequential in-place negations -- algebraically the same
    # XOR `facade_convention.resolve_sign` implements, but written so no AST
    # pattern search for "^"/local tables could ever recognize it, and so
    # this real, live-called function (src/validator/checks/correction.py
    # calls it directly) never actually routed through the merged formula.
    # `_is_mirrored`'s LENIENT coercion is deliberately kept as-is (sol: "别
    # 换严格版，那会真的改行为") -- only bool passthrough plus
    # case-insensitive "true" string, anything else silently False, matching
    # this function's historical behavior exactly. `local_x_positive` gets
    # the same treatment: the ORIGINAL code only special-cased the exact
    # string "image_right_to_left" (anything else, including garbage, was
    # left-to-right) -- normalizing it the same way before handing it to
    # `resolve_sign` (which is strict about the literal) preserves that.
    mirrored_bool = _is_mirrored(mirrored)
    normalized_local = (
        "image_right_to_left" if local_x_positive == "image_right_to_left" else "image_left_to_right"
    )
    sign = facade_convention.resolve_sign(view_facade, mirrored=mirrored_bool, local_x_positive=normalized_local)

    # The along-facade extent of the building in the world axis.
    lo, hi = (xmin, xmax) if axis == "x" else (ymin, ymax)
    # image-local x=0 maps to the world extreme the along-facade direction starts
    # from: +sign starts at lo, −sign starts at hi.
    along_origin = lo if sign > 0 else hi

    return FacadeWorldFrame(
        facade=view_facade,
        world_axis=axis,
        sign=sign,
        base_world=base_world,
        along_origin=along_origin,
        normal=FACADE_NORMAL[view_facade],
    )


# ---------------------------------------------------------------------------
# C2 Vg/Va frame split (proposals/c2_vg_detail_spec.md §3.2).
#
# `FacadeWorldFrame`/`derive_facade_frame` above stay a legacy compatibility
# wrapper for the existing rectangular cross-check only: it conflates a view's
# local->world sign with a single bbox-extreme wall plane, which cannot
# express a concave multi-segment facade. Vg/Va new code must use the two
# frames below instead and must not import the aggregate legacy type.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ViewProjectionFrame:
    """One elevation view's local-along -> world-along projection only.

    One instance per view. It owns *only* the mapping from an image-local
    along-facade x to a world coordinate — no wall plane, segment id, or
    visibility (that is `FacadeSegmentFrame`'s job). A view is not one wall
    plane: several `FacadeSegmentFrame`s at different depths may share one
    `ViewProjectionFrame`.
    """

    facade_family: FacadeFamily
    world_axis: Literal["x", "y"]
    sign: Literal[-1, 1]
    along_origin: float
    mirrored: bool
    local_x_positive: Literal["image_left_to_right", "image_right_to_left"]

    def to_world_along(self, local_x: float) -> float:
        return self.along_origin + self.sign * local_x


@dataclass(frozen=True)
class FacadeSegmentFrame:
    """One boundary-ring candidate edge's world geometry, pre-visibility.

    One instance per candidate edge (before the Vg skyline decides what part
    of it is visible). It owns *only* world geometry — no local image, mirror,
    manifest, or claim data (that is `ViewProjectionFrame`'s / Va's job).
    """

    facade_family: FacadeFamily
    p1: tuple[float, float]
    p2: tuple[float, float]
    base_world: float
    outward_normal: tuple[Literal[-1, 0, 1], Literal[-1, 0, 1]]
    world_along_interval: tuple[float, float]
    depth: float


def derive_view_projection_frame(
    *,
    vertices,
    facade_family: FacadeFamily,
    mirrored: bool = False,
    local_x_positive: Literal["image_left_to_right", "image_right_to_left"] = "image_left_to_right",
) -> ViewProjectionFrame:
    """Derive one elevation view's local-along -> world-along projection.

    `along_lo/along_hi` come only from `vertices`' extent on the family's
    world axis (§3.1); this builder does not read a manifest or reading
    artifact — direction/mirror validity is the caller's job. It only accepts
    a *resolved* bool `mirrored`; an unresolved ("unknown") mirror flag must
    fail closed before calling this, not be silently treated as `False`.

    The two flip bits (`mirrored` and a right-to-left local convention)
    combine by XOR (v2 §3.2): both non-default at once cancels out and
    restores the standard `base_sign`.
    """
    if facade_family not in facade_convention.FACADE_WORLD_AXIS:
        raise ValueError(f"unknown facade_family {facade_family!r}")
    if not isinstance(mirrored, bool):
        raise ValueError(
            "derive_view_projection_frame requires a resolved bool 'mirrored' "
            "flag; an unresolved value must fail closed before this call"
        )
    if local_x_positive not in ("image_left_to_right", "image_right_to_left"):
        raise ValueError(f"unknown local_x_positive {local_x_positive!r}")

    axis = facade_convention.world_axis(facade_family)
    xs = [float(p[0]) for p in vertices]
    ys = [float(p[1]) for p in vertices]
    lo, hi = (min(xs), max(xs)) if axis == "x" else (min(ys), max(ys))

    # F-9 route② S1: single merged formula (facade_convention.resolve_sign),
    # not a locally inlined XOR. The three guard clauses above already
    # guarantee valid inputs, so resolve_sign's own (identical) checks never
    # fire differently here — this is a pure call-site substitution.
    sign = facade_convention.resolve_sign(facade_family, mirrored=mirrored, local_x_positive=local_x_positive)
    along_origin = lo if sign > 0 else hi

    return ViewProjectionFrame(
        facade_family=facade_family,
        world_axis=axis,
        sign=sign,
        along_origin=along_origin,
        mirrored=mirrored,
        local_x_positive=local_x_positive,
    )
