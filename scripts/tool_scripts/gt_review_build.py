#!/usr/bin/env python3
"""Build a GT candidate review bundle from a conversion request (the missing third CLI).

Pairs with ``gt_review_sign.py`` (human G10 signature) and ``gt_review_rerun.py``
(mandatory signed second conversion).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.agent.judge.tarch_converter_schema import TarchConversionRequestV1
from src.agent.judge.tarch_review_bundle import build_review_bundle


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source-dxf", type=Path, required=True)
    ap.add_argument("--request", type=Path, required=True)
    ap.add_argument("--raster-dir", type=Path, required=True,
                    help="directory holding the case rasters (only *.png are bundled)")
    ap.add_argument("--out", type=Path, required=True, help="bundle dir; must not exist")
    ap.add_argument("--zone-roles", type=Path, default=None,
                    help="optional JSON {zone_id: role}; REVIEW-ONLY tint, never enters the GT")
    args = ap.parse_args()

    request = TarchConversionRequestV1.model_validate_json(args.request.read_bytes())
    staging = Path(tempfile.mkdtemp(prefix="gt_rasters_"))
    try:
        for png in sorted(args.raster_dir.glob("*.png")):
            shutil.copy2(png, staging / png.name)
        roles = json.loads(args.zone_roles.read_text(encoding="utf-8")) if args.zone_roles else {}
        roles = {k: v for k, v in roles.items() if not k.startswith("_")}  # 下划线开头 = 注释键
        out = build_review_bundle(args.source_dxf, request, output_dir=args.out,
                                  raster_root=staging, review_annotations=roles)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    print(f"bundle: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
