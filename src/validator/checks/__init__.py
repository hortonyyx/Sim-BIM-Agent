"""Per-stage deterministic check adapters + the common CheckReport v2 schema.

Each module here is a thin *stage adapter*: it knows how to turn a stage's
artifact into a :class:`CheckReport` (facts), reusing the low-level domain
validators in ``src/validator/{interzone,schedules,data_model}.py`` and
``src/validator/idf_fragments.py`` rather than re-implementing parsing/regex.
Policy (block vs flag) lives in :mod:`schema` as a pure function of the facts.
"""

from __future__ import annotations

from .schema import (
    REPORT_SCHEMA_VERSION,
    CheckLayer,
    CheckReport,
    CheckResult,
    CheckStatus,
    Disposition,
    EVIDENCE_CHECK_IDS,
    disposition,
    is_evidence_check_id,
)

__all__ = [
    "REPORT_SCHEMA_VERSION",
    "CheckLayer",
    "CheckReport",
    "CheckResult",
    "CheckStatus",
    "Disposition",
    "EVIDENCE_CHECK_IDS",
    "disposition",
    "is_evidence_check_id",
]
