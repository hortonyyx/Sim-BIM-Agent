"""0_reading vector-JSON data model + legacy migration (M1)."""

from __future__ import annotations

from src.agent.reading.legacy import (
    attach_raw_metadata,
    load_reading_view,
    migrate_view,
    parse_reading_view,
    parse_value_m,
    reading_raw_metadata,
)
from src.agent.reading.contract import (
    READING_CONTRACT_DETECTOR_VERSION,
    READING_PRODUCT_CONTRACT,
    ReadingContractDecision,
    identify_reading_contract,
)
from src.agent.reading.schema import (
    Dimension,
    DimensionRole,
    Facade,
    FacadeOrientation,
    OrientationEvidence,
    ReadingView,
    RoomRoleObservation,
    Stroke,
)

__all__ = [
    "load_reading_view",
    "parse_reading_view",
    "migrate_view",
    "parse_value_m",
    "attach_raw_metadata",
    "reading_raw_metadata",
    "Dimension",
    "DimensionRole",
    "Facade",
    "FacadeOrientation",
    "OrientationEvidence",
    "ReadingView",
    "RoomRoleObservation",
    "Stroke",
    "READING_PRODUCT_CONTRACT",
    "READING_CONTRACT_DETECTOR_VERSION",
    "ReadingContractDecision",
    "identify_reading_contract",
]
