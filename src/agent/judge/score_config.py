"""Strict, content-addressed B4b judge-score configuration loader."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from .score_schema import JudgeScoreConfigV1, ScoreContractError, canonical_sha256


def load_judge_score_config(path: Path | str) -> JudgeScoreConfigV1:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return JudgeScoreConfigV1.model_validate(data)
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValidationError, TypeError) as exc:
        raise ScoreContractError("score_gt_identity_invalid", "scoring.input_identity", context={"input": "judge_score_config"}) from exc


def judge_score_config_sha256(config: JudgeScoreConfigV1) -> str:
    return canonical_sha256(config.model_dump(mode="json"))
