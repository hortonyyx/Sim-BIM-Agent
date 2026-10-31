"""Durable human review approvals for flow checkpoints.

Human review is a calling-policy checkpoint layered outside ``run_one_stage``.
The approval binds to the manifest-accepted output hash for a stage, so any
resample or accepted-attempt drift invalidates the approval fail-closed.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from src.agent.execution.run_meta import run_meta_path

REVIEW_NAME = "human_review.json"


class HumanReviewApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    output_hash: str
    actor: str
    timestamp: str = ""
    note: str = ""


def load_reviews(run_dir: Path) -> dict[str, HumanReviewApproval]:
    p = run_meta_path(run_dir, REVIEW_NAME)
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    return {
        stage: HumanReviewApproval.model_validate(value)
        for stage, value in data.items()
        if isinstance(value, dict)
    }


def record_review(
    run_dir: Path,
    *,
    stage: str,
    output_hash: str,
    actor: str,
    timestamp: str = "",
    note: str = "",
) -> HumanReviewApproval:
    reviews = load_reviews(run_dir)
    appr = HumanReviewApproval(
        stage=stage,
        output_hash=output_hash,
        actor=actor,
        timestamp=timestamp,
        note=note,
    )
    reviews[stage] = appr
    p = run_meta_path(run_dir, REVIEW_NAME, for_write=True)
    p.write_text(
        json.dumps(
            {k: v.model_dump() for k, v in reviews.items()},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return appr


def review_is_current(run_dir: Path, *, stage: str, output_hash: str) -> bool:
    appr = load_reviews(run_dir).get(stage)
    return appr is not None and appr.output_hash == output_hash


__all__ = [
    "REVIEW_NAME",
    "HumanReviewApproval",
    "load_reviews",
    "record_review",
    "review_is_current",
]
