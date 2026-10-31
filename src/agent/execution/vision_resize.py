"""F-51 (second cut): pre-resize staged reader images to the vision API's own
resize target, so the frame the reading VLM sees, the frame on disk in
staging, and the frame ``cv_probe`` reads are the SAME frame by construction.

Background (see ``AI_agent/logs/experiments/2026-08-16_707_repro/``): the
Claude vision API silently downsamples an oversized image before the model
ever sees it (Anthropic docs, "How Claude resizes and pads images"). The
reading agent's self-reported pixel coordinates therefore live in the
POST-resize frame, while ``cv_probe`` (and the "F-51 first cut" sidecar
``source_image.width_px/height_px`` echo in ``cv_toolbox/sidecar.py``) reads
the ORIGINAL file on disk. A calibration built from such mismatched
coordinates can be self-consistent yet globally wrong (uniform scale error),
and crops land in the wrong region as honest false negatives.

Anthropic's own recommendation (not a workaround this project invented):
resize the image yourself before it is ever shown, so the reader's pixel
coordinates need no frame conversion at all. This module implements that
resize using the documented rule so the staged copy in ``case_data/`` is
already at the size the API would have produced:

- Visual tokens: ``ceil(w/28) * ceil(h/28)``.
- A tier caps both the longest edge (rounded up to a 28px patch) AND the
  token count; the target size is the largest that satisfies both, holding
  aspect ratio, found by binary search along the long edge (short edge via
  Python's banker's-rounding ``round()`` — this matches the documented
  algorithm, verified against the docs' own worked example: an image of
  1075x1520 must resize to exactly 924x1307 under the standard tier).
- The API pads the resized frame to a multiple of 28px afterward, but the
  pad is bottom/right only and never shifts the origin, so callers should
  convert using the RESIZED size below, never the padded size (per the same
  Anthropic doc page) — this module returns the resized (pre-pad) size,
  which is what a caller should compare pixel coordinates against.

Only images that exceed a tier's limits are touched; an image already within
the tier round-trips byte-for-byte (no PIL re-encode), so this is a no-op for
this project's elevation drawings (verified: all four elevations for the
current anchor cases already fit the standard tier; only the two plan views
do not).
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image

# (max_edge_px, max_tokens). Values from the Anthropic vision-coordinates doc,
# "How Claude resizes and pads images": standard tier applies to Haiku 4.5 and
# other non-4.7+ models; high_res applies to Claude 4.7 and later. Keeping both
# tiers named here (rather than inlining 1568 at each call site) is what makes
# the tier "configurable, not hardcoded in multiple places" (dispatch F-51
# requirement) — a caller selects a tier by name; the literal edge/token
# numbers exist exactly once.
VISION_RESIZE_TIERS: dict[str, tuple[int, int]] = {
    "standard": (1568, 1568),
    "high_res": (2576, 4784),
}
# "none" is not a vision tier — it is the explicit choice NOT to touch the file.
# Kept out of VISION_RESIZE_TIERS so that the two real tiers stay a faithful copy
# of the Anthropic table; callers select it by name like any other option.
NO_VISION_RESIZE = "none"
VISION_RESIZE_CHOICES: tuple[str, ...] = (NO_VISION_RESIZE, "standard", "high_res")

# 2026-08-19 (user ruling): stage the ORIGINAL drawing. Cost-driven downscaling is
# off the table for now, and the frame-alignment tradeoff F-51 was built to manage
# — the reader's displayed frame vs the frame cv_probe measures — is packaged into
# the reading 专项 rather than decided by a default here. A caller that wants the
# old behaviour asks for it by name.
DEFAULT_VISION_RESIZE_TIER = NO_VISION_RESIZE

# High-quality downsampling for a full-image resize the model will read as a
# drawing (LANCZOS preserves thin lines/text better than the NEAREST filter
# cv_toolbox.tools uses for pixel-exact crop zooms — those are a different
# operation with a different fidelity goal).
_RESAMPLE = Image.Resampling.LANCZOS


def count_image_tokens(width: int, height: int) -> int:
    """Anthropic's visual-token count for an image of this pixel size."""
    return math.ceil(width / 28) * math.ceil(height / 28)


def resized_size(
    width: int, height: int, max_edge: int = 1568, max_tokens: int = 1568
) -> tuple[int, int]:
    """The Anthropic API's own target size for an image of ``width x height``.

    Identity when the image already fits both the edge and token caps.
    Otherwise holds aspect ratio and binary-searches the long edge for the
    largest size that fits (short edge via ``round()`` — banker's rounding,
    per the documented algorithm). Verified against the docs' own worked
    example: ``resized_size(1075, 1520) == (924, 1307)``.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"width/height must be positive, got {width}x{height}")

    def fits(w: float, h: float) -> bool:
        return (
            math.ceil(w / 28) * 28 <= max_edge
            and math.ceil(h / 28) * 28 <= max_edge
            and count_image_tokens(w, h) <= max_tokens
        )

    if fits(width, height):
        return (width, height)
    if height > width:
        rh, rw = resized_size(height, width, max_edge, max_tokens)
        return (rw, rh)
    ar = width / height
    lo, hi = 1, width  # invariant: fits(lo, ...) True, fits(hi, ...) False
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if fits(mid, max(round(mid / ar), 1)):
            lo = mid
        else:
            hi = mid
    return (lo, max(round(lo / ar), 1))


def resized_size_for_tier(
    width: int, height: int, tier: str = DEFAULT_VISION_RESIZE_TIER
) -> tuple[int, int]:
    if tier == NO_VISION_RESIZE:
        return (width, height)
    if tier not in VISION_RESIZE_TIERS:
        raise ValueError(
            f"unknown vision resize tier {tier!r}; known tiers: "
            f"{sorted(VISION_RESIZE_TIERS)}"
        )
    max_edge, max_tokens = VISION_RESIZE_TIERS[tier]
    return resized_size(width, height, max_edge=max_edge, max_tokens=max_tokens)


def resize_image_file_to_tier(
    path: str | Path, tier: str = DEFAULT_VISION_RESIZE_TIER
) -> tuple[int, int]:
    """Resize the PNG at ``path`` IN PLACE to the vision tier's target size.

    A no-op (file left byte-for-byte untouched) when the image already fits
    the tier. Returns the final on-disk size either way, so the caller can
    assert it against :func:`resized_size_for_tier` without re-opening the
    file.
    """
    path = Path(path)
    with Image.open(path) as img:
        width, height = img.size
        target = resized_size_for_tier(width, height, tier)
        if target == (width, height):
            return (width, height)
        # F-51: this is a full-image resize of a drawing the model will read
        # (not a pixel-exact CV crop), so a high-quality filter is the right
        # tradeoff — the API's own downsampling is not pixel-nearest either.
        # Preserve the source mode (RGB for this project's drawings) rather
        # than forcing one; only palette images are flattened first, since
        # LANCZOS on "P" mode degrades badly — same simplification
        # cv_toolbox.tools._load_rgb already makes for the same reason.
        source = img.convert("RGB") if img.mode == "P" else img
        resized = source.resize(target, _RESAMPLE)
        resized.save(path)
        return target
