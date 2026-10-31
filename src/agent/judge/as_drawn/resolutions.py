"""⭐ J-2 — the TWO grid resolutions, each DECLARED once and CONSUMED, never equal.

User ruling, 2026-09-04 (guide §15.11, the wording that supersedes every earlier
grain-size draft that day):

    「gt 分辨率按 1 mm，pipeline 出口按 10 mm。都统一按这个最新口径。」

and the load-bearing corollary, verbatim from the same ruling:

    两者【故意不相等】 ⇒ 判分侧核对的是「两个分辨率各自被显式声明、且各自被
    消费」，⛔ 不是「两个数相等」。

Before this module that rule existed only in prose: no code declared either
number as a *resolution*, nothing read a declaration, and a grader written
naively would compare coordinates with ``==`` — impossible precisely because
the two sides intentionally sit on different grids (a 10 mm pipeline output can
differ from the 1 mm gt by up to half a cell, 5 mm, by construction).

What this module owns
---------------------
1. ONE named constant per side, carrying the signed default:

     ``GT_RESOLUTION_M``                0.001  — gt-side modular resolution
     ``PIPELINE_OUTPUT_RESOLUTION_M``   0.010  — pipeline output grid

   ⚠️ A named constant here is the DEFAULT and the documentation anchor, ⛔ NOT
   the value the grader uses. The grader reads the DECLARATION (below); the
   constant is what the declaration is expected to say, and the fallback when
   a product carries no declaration of its own.

2. ONE declaration point per side, and a reader that reads it:

     gt side       ``generator.tolerances.dxf_axis_alignment_tolerance_m``
                   inside the gt document itself — the answer root owns it, so
                   this module only READS it (⛔ gt is never edited by a grader;
                   the gt-iron rule).  Absent declaration is a LOUD failure,
                   never a silent default: a gt that stops declaring its grid is
                   a different contract and must not be graded as today's.
     product side  the product's own ``resolution_m`` field.  Today's as-drawn
                   elevation products (``as_drawn_elevation_v0``) do not carry
                   one — ``declared`` reports that honestly, and the signed
                   pipeline-output default stands in, NAMED as a default.  The
                   day a product declares its own grid, every consumer here
                   follows it with no code change — that is the lock
                   ``test_j2_resolutions.py`` holds.

3. The consumption rule, in one place: before comparing, each side's coordinates
   are snapped to that side's OWN declared grid
   (:func:`snap_to_resolution`), and the worst-case displacement that snapping
   can introduce is :func:`quantization_band_m` — half a cell from each side.
   A comparison tolerance narrower than that band is not a tolerance, it is a
   bug (J-1's 5 mm, mechanised), so graders surface the band next to their own
   semantic tolerance instead of hiding it inside one number.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

#: gt-side modular resolution (user 2026-08-29 signed, reaffirmed 2026-09-04).
#: The authority for this value is the gt's own declaration; see module docstring.
GT_RESOLUTION_M = 0.001

#: pipeline output grid — the value ``output_precision_m`` / snap grids in the
#: correction config carry today (guide §14.7: signed by usage, by nobody).
PIPELINE_OUTPUT_RESOLUTION_M = 0.010

#: where the gt side's declaration lives, as a JSON pointer into the gt document.
GT_DECLARATION_POINTER = ("generator", "tolerances", "dxf_axis_alignment_tolerance_m")

#: where a product side's declaration lives, if it has one.
PRODUCT_DECLARATION_KEY = "resolution_m"


class ResolutionDeclarationMissing(RuntimeError):
    """The gt stopped declaring its grid — loud, ⛔ never a silent default.

    A gt document without ``generator.tolerances`` is a different contract, not
    this week's gt with a typo; grading it with today's default would measure
    yesterday's grid against tomorrow's answer ([[absence-conflates-causes-in-observables]]).
    """

    def __init__(self, pointer: tuple[str, ...]) -> None:
        self.pointer = pointer
        super().__init__(
            "gt_resolution_declaration_missing at /" + "/".join(pointer)
            + " — the gt no longer declares its grid; refusing to grade against"
            " an assumed one (J-2)")


@dataclass(frozen=True)
class ResolutionDeclaration:
    """One side's grid: the value actually in force, and where it came from.

    ``declared`` is the honest part: ``True`` means the document itself said it,
    ``False`` means the signed default stood in.  A report printing both rows
    lets any reader see whose number the grade consumed.
    """

    value_m: float
    source: str
    declared: bool


def read_gt_resolution(gt: Mapping) -> ResolutionDeclaration:
    """Read the gt side's grid from the gt's OWN declaration (J-2).

    ⛔ Never edits gt, never substitutes the constant for a missing declaration.
    """
    node: Mapping | None = gt
    for key in GT_DECLARATION_POINTER[:-1]:
        value = node.get(key) if isinstance(node, Mapping) else None
        node = value if isinstance(value, Mapping) else None
    leaf = GT_DECLARATION_POINTER[-1]
    if not isinstance(node, Mapping) or leaf not in node:
        raise ResolutionDeclarationMissing(GT_DECLARATION_POINTER)
    try:
        value = float(node[leaf])
    except (TypeError, ValueError) as exc:
        raise ResolutionDeclarationMissing(GT_DECLARATION_POINTER) from exc
    if not value > 0.0:
        raise ResolutionDeclarationMissing(GT_DECLARATION_POINTER)
    return ResolutionDeclaration(
        value_m=value,
        source="/" + "/".join(GT_DECLARATION_POINTER),
        declared=True,
    )


def read_product_resolution(doc: Mapping) -> ResolutionDeclaration:
    """Read the product side's grid: its own field when present, default otherwise.

    The default is the SIGNED pipeline-output grid, named as such — ⛔ not an
    anonymous number, and ⛔ not silently equal to the gt side's grid.
    """
    if PRODUCT_DECLARATION_KEY in doc:
        try:
            value = float(doc[PRODUCT_DECLARATION_KEY])
        except (TypeError, ValueError):
            value = -1.0
        if value > 0.0:
            return ResolutionDeclaration(
                value_m=value, source=PRODUCT_DECLARATION_KEY, declared=True)
    return ResolutionDeclaration(
        value_m=PIPELINE_OUTPUT_RESOLUTION_M,
        source=f"default:{PIPELINE_OUTPUT_RESOLUTION_M} (pipeline output grid;"
               f" product declared no {PRODUCT_DECLARATION_KEY})",
        declared=False,
    )


def snap_to_resolution(value_m: float, grid_m: float) -> float:
    """Snap one coordinate onto its own side's declared grid — the consumption.

    ⭐ This, not a widened tolerance, is what "each side consumes its own
    resolution" means mechanically: the number is honest to its own grain
    before it is compared.  ``grid_m <= 0`` returns the value untouched so a
    malformed declaration cannot zero out the coordinate system.
    """
    if grid_m <= 0.0:
        return float(value_m)
    return round(value_m / grid_m) * grid_m


def quantization_band_m(gt_resolution_m: float,
                        product_resolution_m: float) -> float:
    """Worst-case displacement two grid snaps can introduce between the sides.

    Half a cell from each side (J-1's 5 mm is the pipeline half-cell of the
    10 mm grid; the gt's own half-cell rides on top of it).  A comparison
    tolerance below this band cannot be met even by a perfect product.
    """
    return 0.5 * gt_resolution_m + 0.5 * product_resolution_m
