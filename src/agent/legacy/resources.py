"""Locations of the optional intake workflow's retained reference documents."""
from pathlib import Path

INTAKE_RESOURCE_RELATIVE = Path("src/agent/legacy/intake")
READING_RESOURCE_RELATIVE = INTAKE_RESOURCE_RELATIVE / "reading"
INTAKE_RESOURCES = Path(__file__).with_name("intake")

# These keys belong to persisted historical runs, not the current task model.
RUBRIC_DIRECTORIES = {"0_reading": "reading", "1_correction": "correction"}


def rubric_relative_path(stage: str) -> str | None:
    directory = RUBRIC_DIRECTORIES.get(stage)
    if directory is None:
        return None
    return (INTAKE_RESOURCE_RELATIVE / directory / "judge_rubric.md").as_posix()
