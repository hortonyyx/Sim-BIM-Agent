#!/usr/bin/env python3
"""Build or validate candidate B4b completeness declarations.

This is intentionally not an asset writer: protected GT/golden roots are
rejected even when the caller has filesystem permissions.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.agent.judge.score_schema import (  # noqa: E402
    DatasetCompletenessDeclarationV1,
    DatasetDeclarationBodyV1,
    ElevationFullFacadeCoverageV1,
    PlanFullFloorCoverageV1,
    UserCompletenessDeclarationV1,
    UserDeclarationBodyV1,
    canonical_sha256,
)


def _protected(path: Path) -> bool:
    parts = path.resolve().parts
    return "gt" in parts or "golden" in parts or "verified" in parts


def _coverage(args):
    if args.coverage_kind == "full_floor":
        return PlanFullFloorCoverageV1(kind="full_floor", floor_id=args.floor_id)
    return ElevationFullFacadeCoverageV1(kind="full_facade", floor_ids=tuple(args.floor_ids), facade_family=args.facade_family)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source", choices=("user", "dataset_ref"), required=True)
    parser.add_argument("--input-id", required=True)
    parser.add_argument("--assertion-id", required=True)
    parser.add_argument("--negative-claim", action="append", required=True)
    parser.add_argument("--coverage-kind", choices=("full_floor", "full_facade"), required=True)
    parser.add_argument("--floor-id", default="")
    parser.add_argument("--floor-ids", nargs="*", default=[])
    parser.add_argument("--facade-family", choices=("North", "South", "East", "West"))
    parser.add_argument("--asserted-by")
    parser.add_argument("--assertion-revision", type=int)
    parser.add_argument("--dataset-id")
    parser.add_argument("--dataset-version")
    parser.add_argument("--contract-id")
    args = parser.parse_args()
    if _protected(args.output):
        parser.error("output must be a candidate or temporary path, never GT/golden/verified")
    coverage = _coverage(args)
    if args.source == "user":
        if not args.asserted_by or args.assertion_revision is None:
            parser.error("user declarations require --asserted-by and --assertion-revision")
        body = UserDeclarationBodyV1(input_id=args.input_id, assertion_id=args.assertion_id,
            negative_claims=tuple(args.negative_claim), coverage=coverage, asserted_by=args.asserted_by,
            assertion_revision=args.assertion_revision)
        declaration = UserCompletenessDeclarationV1(source="user", body=body,
            body_sha256=canonical_sha256(body.model_dump(mode="json")))
    else:
        if not all((args.dataset_id, args.dataset_version, args.contract_id)):
            parser.error("dataset declarations require --dataset-id --dataset-version --contract-id")
        body = DatasetDeclarationBodyV1(input_id=args.input_id, assertion_id=args.assertion_id,
            negative_claims=tuple(args.negative_claim), coverage=coverage, dataset_id=args.dataset_id,
            dataset_version=args.dataset_version, contract_id=args.contract_id)
        declaration = DatasetCompletenessDeclarationV1(source="dataset_ref", body=body,
            body_sha256=canonical_sha256(body.model_dump(mode="json")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(declaration.model_dump(mode="json"), sort_keys=True, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
