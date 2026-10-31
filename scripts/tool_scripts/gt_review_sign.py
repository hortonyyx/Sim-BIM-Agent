#!/usr/bin/env python3
"""Create one hash-bound human-review acknowledgement for a candidate bundle."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.agent.judge.tarch_review_bundle import sign_review_bundle


def main() -> None:
    parser = argparse.ArgumentParser(description="Sign a validated GT candidate review bundle.")
    parser.add_argument("bundle_dir")
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--signed-at", required=True)
    parser.add_argument("--confirm-near-threshold", action="store_true")
    args = parser.parse_args()
    ack = sign_review_bundle(args.bundle_dir, reviewer=args.reviewer, signed_at=args.signed_at,
                             confirm_near_threshold=args.confirm_near_threshold)
    print(ack.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
