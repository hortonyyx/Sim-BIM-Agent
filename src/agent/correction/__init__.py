"""Staged correction layer.

correction stage (LLM correction) -> CorrectedGeometry  (materialized intermediate)
   -> deterministic core (code: canonical axis snap + sliver guard)
   -> the modeling/MEP stages (LLM modeling) -> IntakeOutput

Decoupling the stages makes each model swappable and the correction checkpoint
verifiable / diffable for evaluation. Legacy reference: src/agent/legacy/intake/correction/.
PartA-correction/.
"""

from src.agent.correction.deterministic import apply_deterministic_core
from src.agent.correction.envelope import AuthoritativeEnvelope, EnvelopeCandidate
from src.agent.correction.schema import CorrectedGeometry

__all__ = [
    "AuthoritativeEnvelope",
    "CorrectedGeometry",
    "EnvelopeCandidate",
    "apply_deterministic_core",
]
