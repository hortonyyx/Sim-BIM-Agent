"""Official stage-0 building contracts for the unified agent."""

from types import ModuleType

from .bundle import BuildingContractBundle
from .declarations import (
    BuildingDeclaration,
    BuildingModelVersion,
    DeclarationScope,
    ExistingToolCall,
    FieldPartition,
    InferenceHypothesis,
    RepetitionInstance,
    RepetitionRule,
)
from .evidence import (
    BuildingEvidenceLedger,
    CalculationChain,
    CalculationStep,
    EvidenceConflict,
    EvidenceGroup,
    EvidenceItem,
    EvidenceTemplate,
    ObjectEvidenceBinding,
    Quantity,
    UserRequirement,
)
from .quality import (
    CheckResult,
    ConnectivityState,
    CoverageEntry,
    CoverageIssue,
    DimensionState,
    DimensionEdit,
    ExistingTargetLine,
    FeatureDisposition,
    NormalizationProposal,
    OpeningSemanticState,
    SavedModelSnapshot,
    SavedVerification,
    SemanticSnapshot,
    SuggestedAction,
    ToleranceDecision,
    ValueSnapshot,
)
from .refs import BuildingObjectRef, CoordinateRelation, ExistingEvidenceRef, RunQualifiedEvidenceRef
from .tasks import (
    DirectObservation,
    EvidencePackage,
    FloorDraftContract,
    LocalInterpretation,
    LocalUncertainty,
    LocalizedEvidenceResult,
    PixelLocation,
    assert_result_applicable,
)

__all__ = [
    name
    for name, value in globals().items()
    if not name.startswith("_") and not isinstance(value, ModuleType) and name != "ModuleType"
]
