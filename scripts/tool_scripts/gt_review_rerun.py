#!/usr/bin/env python3
"""Run the mandatory signed second conversion for a GT review bundle."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.agent.judge.tarch_review_bundle import rerun_signed_review_bundle


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-run a signed GT candidate bundle through G10.")
    parser.add_argument("bundle_dir")
    args = parser.parse_args()
    rerun_signed_review_bundle(args.bundle_dir)
    print("signed review bundle re-run complete")


if __name__ == "__main__":
    main()
