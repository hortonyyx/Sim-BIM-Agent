#!/usr/bin/env python3
"""Promote a signed PASS GT candidate bundle into an approved GT root."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.agent.judge.gt_promotion import promote_gt_v3


def main() -> None:
    parser = argparse.ArgumentParser(description="Promote a signed GT v3 candidate bundle.")
    parser.add_argument("bundle_dir")
    parser.add_argument("--case", required=True)
    parser.add_argument("--gt-dir", type=Path)
    args = parser.parse_args()
    kwargs = {"case": args.case}
    if args.gt_dir is not None:
        kwargs["gt_dir"] = args.gt_dir
    result = promote_gt_v3(args.bundle_dir, **kwargs)
    print(result.destination)


if __name__ == "__main__":
    main()
