"""Reading-product envelope contract: the shape detector for ReadingViews v2.

This module is the single canonical home of the reading-product *contract*
shape — the lightweight recognition used by both production code (the
correction source verifier, the run-stage envelope wrapper) and the judge
typed-score adapter.  It depends on no judge, execution, or scoring code:
only the stdlib ``dataclasses``/``typing``.  Keeping it here lets non-judge
modules recognize the v2 envelope without importing judge score code (the
B5 A6 ``window_sources`` judge-blind boundary).

Symbols historically defined in the judge score-schema and typed-adapter
modules are re-exported from there for unchanged call sites; the definitions
themselves live only here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

# Single record for the v2 contract id and the detector that recognizes it.
# ``ReadingContractDecision.contract_id`` derives from ``READING_PRODUCT_CONTRACT``
# (see below) so the recognized id is named in exactly one place.
READING_PRODUCT_CONTRACT: Final = "reading_views_v2"
READING_CONTRACT_DETECTOR_VERSION: Final = "reading_contract_detector_v2"


@dataclass(frozen=True)
class ReadingContractDecision:
    contract_id: Literal[READING_PRODUCT_CONTRACT, "unrecognized"]
    reason: str | None


def identify_reading_contract(raw: object) -> ReadingContractDecision:
    """Recognize a ReadingViews v2 envelope without interpreting facade direction."""
    if not isinstance(raw, dict):
        return ReadingContractDecision("unrecognized", "reading_output_not_object")
    if "views" not in raw:
        return ReadingContractDecision("unrecognized", "reading_views_missing")
    views = raw["views"]
    if not isinstance(views, dict):
        return ReadingContractDecision("unrecognized", "reading_views_not_object")
    if any(not isinstance(key, str) or not key for key in views):
        return ReadingContractDecision("unrecognized", "reading_view_id_invalid")
    return ReadingContractDecision(READING_PRODUCT_CONTRACT, None)
