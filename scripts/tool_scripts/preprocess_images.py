"""Downscale case images so open-source VLMs stay under tight TPM budgets
AND within the Continue client's per-attachment token limit.

Two distinct token metrics matter:

1. **Vision tokens (model side)**: what Qwen-VL / InternVL actually consume
   during inference, roughly `ceil(W/28) * ceil(H/28)`. Drives TPM usage.
2. **Continue tokens (client side)**: Continue's own pre-flight check before
   attaching an image; empirically ~`W * H / 28` (no ceil, total-pixel based).
   A 1536x952 image produces ~52k "Continue tokens" which exceeds the
   client's 16,384 per-attachment hard cap, even though the model would only
   see ~1.9k real vision tokens.

Defaults below target the tighter constraint (Continue <= 16,384):
    top_edge    = 800   -> ~14k Continue tokens, ~1.0k vision tokens
    facade_edge = 640   -> ~9k Continue tokens, ~0.9k vision tokens

Usage:
    python scripts/tool_scripts/preprocess_images.py <case_dir>
    python scripts/tool_scripts/preprocess_images.py <case_dir> --top-edge 896 --facade-edge 704

Outputs are written to <case_dir>/output/preprocessed/ alongside a
manifest.json. Point the VLM (Continue / intake_node) at this directory
instead of the raws.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, asdict
from pathlib import Path

from PIL import Image, ImageChops

FACADE_STEMS = ("South_view", "North_view", "East_view", "West_view")
TOP_STEMS = ("top_view", "top_view_annotated")
VISION_PATCH = 28  # Qwen-VL / InternVL default patch size in pixels
CONTINUE_PIXEL_DIVISOR = 28  # Continue client's empirical tokens-per-pixel ~ W*H/28
CONTINUE_PER_ATTACHMENT_CAP = 16384  # hard cap that Continue enforces client-side
TRIM_THRESHOLD_DEFAULT = 248  # >=248 per channel counts as "white" background
TRIM_PADDING_DEFAULT = 24      # px of white padding kept around content bbox
TOP_EDGE_DEFAULT = 800          # keeps Continue tokens ~14k, leaves headroom
FACADE_EDGE_DEFAULT = 640       # keeps Continue tokens ~9k


@dataclass
class ImageReport:
    src: str
    dst: str
    orig_size: tuple[int, int]
    trimmed_size: tuple[int, int]
    new_size: tuple[int, int]
    scale: float
    est_vision_tokens: int
    est_continue_tokens: int
    continue_ok: bool
    orig_kb: float
    new_kb: float


def estimate_tokens(w: int, h: int, patch: int = VISION_PATCH) -> int:
    return math.ceil(w / patch) * math.ceil(h / patch)


def estimate_continue_tokens(w: int, h: int) -> int:
    return (w * h) // CONTINUE_PIXEL_DIVISOR


def trim_white_border(im: Image.Image, threshold: int, padding: int) -> Image.Image:
    """Crop near-white page borders while keeping content + a padding gutter.

    Works on a flattened RGB copy so alpha channels don't confuse the threshold.
    Uses a conservative threshold (default 248) so faint grid lines, dimension
    chains, and light gray annotations are never treated as background.
    """
    rgb = im.convert("RGB") if im.mode != "RGB" else im
    # Build a mask where pixels BRIGHTER than threshold on all channels are white.
    # Subtracting from a pure white image yields 0 for whites and >0 for content.
    white_bg = Image.new("RGB", rgb.size, (255, 255, 255))
    diff = ImageChops.difference(rgb, white_bg)
    # Fold channels into a grayscale "content amount" map, then binarize at
    # (255 - threshold). A pixel counts as content if ANY channel deviates
    # from pure white by more than (255 - threshold).
    gray = diff.convert("L")
    content_mask = gray.point(lambda v: 255 if v > (255 - threshold) else 0)
    bbox = content_mask.getbbox()
    if bbox is None:
        return im  # empty / all-white image
    left, top, right, bottom = bbox
    w, h = rgb.size
    left = max(0, left - padding)
    top = max(0, top - padding)
    right = min(w, right + padding)
    bottom = min(h, bottom + padding)
    return im.crop((left, top, right, bottom))


def resize_long_edge(
    src: Path,
    dst: Path,
    long_edge: int,
    trim: bool,
    trim_threshold: int,
    trim_padding: int,
) -> ImageReport:
    with Image.open(src) as im:
        if im.mode not in ("RGB", "RGBA", "L"):
            im = im.convert("RGB")
        orig_w, orig_h = im.size

        if trim:
            im = trim_white_border(im, threshold=trim_threshold, padding=trim_padding)
        trimmed_w, trimmed_h = im.size

        scale = min(1.0, long_edge / max(trimmed_w, trimmed_h))
        if scale < 1.0:
            new_w = max(1, int(round(trimmed_w * scale)))
            new_h = max(1, int(round(trimmed_h * scale)))
            im = im.resize((new_w, new_h), Image.LANCZOS)
        else:
            new_w, new_h = trimmed_w, trimmed_h

        dst.parent.mkdir(parents=True, exist_ok=True)
        im.save(dst, format="PNG", optimize=True)

    continue_tok = estimate_continue_tokens(new_w, new_h)
    return ImageReport(
        src=str(src),
        dst=str(dst),
        orig_size=(orig_w, orig_h),
        trimmed_size=(trimmed_w, trimmed_h),
        new_size=(new_w, new_h),
        scale=round(scale, 4),
        est_vision_tokens=estimate_tokens(new_w, new_h),
        est_continue_tokens=continue_tok,
        continue_ok=continue_tok <= CONTINUE_PER_ATTACHMENT_CAP,
        orig_kb=round(src.stat().st_size / 1024, 1),
        new_kb=round(dst.stat().st_size / 1024, 1),
    )


def process_case(
    case_dir: Path,
    top_edge: int,
    facade_edge: int,
    trim: bool,
    trim_threshold: int,
    trim_padding: int,
) -> dict:
    if not case_dir.is_dir():
        raise FileNotFoundError(f"Case dir not found: {case_dir}")

    out_dir = case_dir / "output" / "preprocessed"
    out_dir.mkdir(parents=True, exist_ok=True)

    def run(src: Path, dst_name: str, long_edge: int) -> None:
        reports.append(resize_long_edge(
            src, out_dir / dst_name, long_edge,
            trim=trim, trim_threshold=trim_threshold, trim_padding=trim_padding,
        ))

    reports: list[ImageReport] = []
    for stem in TOP_STEMS:
        src = case_dir / f"{stem}.png"
        if src.exists():
            run(src, f"{stem}.png", top_edge)
    for stem in FACADE_STEMS:
        src = case_dir / f"{stem}.png"
        if src.exists():
            run(src, f"{stem}.png", facade_edge)

    supp = case_dir / "supp_plan.png"
    if supp.exists():
        run(supp, "supp_plan.png", top_edge)

    total_tokens = sum(r.est_vision_tokens for r in reports)
    manifest = {
        "case": case_dir.name,
        "profile": {
            "top_edge": top_edge,
            "facade_edge": facade_edge,
            "patch": VISION_PATCH,
            "trim": trim,
            "trim_threshold": trim_threshold,
            "trim_padding": trim_padding,
        },
        "total_est_vision_tokens": total_tokens,
        "images": [asdict(r) for r in reports],
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("case_dir", type=Path, help="Path to a case directory, e.g. backup/tests_history/SmallOffice/smalloffice_13")
    ap.add_argument("--top-edge", type=int, default=TOP_EDGE_DEFAULT,
                    help=f"Long edge cap for top_view / supp_plan (default {TOP_EDGE_DEFAULT} px, "
                         "~14k Continue tokens / ~1.0k vision tokens)")
    ap.add_argument("--facade-edge", type=int, default=FACADE_EDGE_DEFAULT,
                    help=f"Long edge cap for {{S,N,E,W}}_view.png (default {FACADE_EDGE_DEFAULT} px, "
                         "~9k Continue tokens / ~0.9k vision tokens)")
    ap.add_argument("--no-trim", action="store_true",
                    help="Skip white-border trimming (default: trim enabled)")
    ap.add_argument("--trim-threshold", type=int, default=TRIM_THRESHOLD_DEFAULT,
                    help=f"Per-channel brightness >= this counts as white (default {TRIM_THRESHOLD_DEFAULT}). "
                         "Lower = more aggressive trim; raise if dimension chains get clipped.")
    ap.add_argument("--trim-padding", type=int, default=TRIM_PADDING_DEFAULT,
                    help=f"White gutter kept around content bbox in px (default {TRIM_PADDING_DEFAULT})")
    args = ap.parse_args()

    manifest = process_case(
        args.case_dir,
        top_edge=args.top_edge,
        facade_edge=args.facade_edge,
        trim=not args.no_trim,
        trim_threshold=args.trim_threshold,
        trim_padding=args.trim_padding,
    )
    out_dir = args.case_dir / "output" / "preprocessed"
    print(f"[ok] wrote {len(manifest['images'])} images to {out_dir}")
    print(f"[ok] total vision tokens (model side):  {manifest['total_est_vision_tokens']}")
    print(f"[ok] Continue per-attachment cap:       {CONTINUE_PER_ATTACHMENT_CAP}")
    any_over = False
    for r in manifest["images"]:
        mark = "OK " if r["continue_ok"] else "!! "
        if not r["continue_ok"]:
            any_over = True
        print(f"  {mark}{Path(r['src']).name}: {r['orig_size']} "
              f"-> trimmed {r['trimmed_size']} -> {r['new_size']}  "
              f"vision={r['est_vision_tokens']}  continue={r['est_continue_tokens']}  "
              f"{r['orig_kb']}KB -> {r['new_kb']}KB")
    if any_over:
        print(f"[warn] one or more images exceed Continue's {CONTINUE_PER_ATTACHMENT_CAP} cap "
              "-- rerun with smaller --top-edge / --facade-edge")


if __name__ == "__main__":
    main()
