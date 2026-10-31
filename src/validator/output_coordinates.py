"""E4 building-bound coordinate object registry + `validate_output_coordinate_contract`
gate (spec §7–§8).

Layering discipline (spec §7.3): the registry constants and the four
completeness scanners live HERE (validator layer); the strict
``OutputCoordinateContract`` type, its derivers, and ``apply_*`` business
logic live in ``src/agent/output_coordinates.py``. This module imports a
narrow set of pure-data types/pure-functions from that module (the contract
type itself, the two source-binding types, the verified-correction dataclass,
and a sha256 helper) — the same "validator imports one narrow agent-side
symbol" precedent already used by ``src/validator/idf_fragments.py``
(``ensure_schema_initialized``). It never imports the LangGraph orchestration
graph, nodes, or pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from src.agent.output_coordinates import (
    AcceptedCorrectionRef,
    LegacyStandaloneIntakeRef,
    OutputCoordinateContract,
    OutputCoordinateSnapshotV1,
    OutputCoordinateValidationContext,
    ZoneFrameNormalizationEntryV1,
    sha256_bytes,
)

Hex64 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

if TYPE_CHECKING:
    from eppy.modeleditor import IDF

    from src.mcp.state import ConfigState

DEFAULT_IDD_PATH = Path(__file__).resolve().parents[2] / "data" / "dependencies" / "Energy+.idd"


# --------------------------------------------------------------------------- #
# §7.3 — registry rule types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CoordinateObjectRule:
    object_type: str
    field_pattern: str
    variant_predicate: str
    frame_class: str
    controlling_ggr_field: str
    current_support: Literal["supported", "unsupported"]
    owner_route: str
    idd_version: str = "25.1"


@dataclass(frozen=True)
class CoordinateExclusionRule:
    object_type: str
    field_pattern: str
    reason: str
    idd_version: str = "25.1"


@dataclass(frozen=True)
class OutputCoordinateIssue:
    """BO-CR9: fully immutable — `detail` is stored as canonical (sorted-key)
    JSON text, never a mutable dict hiding inside a frozen shell. Constructors
    may still pass a dict; it is canonicalized at init."""

    code: Literal[
        "CONTRACT_IDENTITY", "MODE_SCHEMA_MISMATCH", "BUILDING_NORTH_AXIS",
        "GGR_COORDINATE_SYSTEM", "ZONE_FRAME_NONZERO", "VERTEX_FRAME_DRIFT",
        "UNCLASSIFIED_COORDINATE_OBJECT", "UNSUPPORTED_COORDINATE_OBJECT",
    ]
    message: str
    detail: str = "{}"

    def __post_init__(self) -> None:
        import json as _json

        if not isinstance(self.detail, str):
            object.__setattr__(
                self, "detail",
                _json.dumps(self.detail, sort_keys=True, default=str, ensure_ascii=False),
            )

    def detail_dict(self) -> dict:
        import json as _json

        return _json.loads(self.detail)


# --------------------------------------------------------------------------- #
# §7.4 — export coordinate audit (strict wire; BO-CR9 re-homed here so
# `offenders` can be typed against the frozen issue record without a
# validator->agent import cycle)
# --------------------------------------------------------------------------- #
class ExportCoordinateAuditV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"] = "1"
    contract_sha256: Hex64
    snapshot_sha256: Hex64 | None
    yaml_sha256: Hex64
    idf_sha256: Hex64
    registry_version: str
    registry_candidate_sha256: Hex64
    config_counts: tuple[tuple[str, int], ...]
    idf_counts: tuple[tuple[str, int], ...]
    zone_normalizations: tuple[ZoneFrameNormalizationEntryV1, ...]
    offenders: tuple[OutputCoordinateIssue, ...] = ()


# --------------------------------------------------------------------------- #
# §7.4 — post-simulate EP audit (BO-CR7): binds the ACTUAL IDF/EIO/ERR bytes
# plus the coordinate-warning behavior observed in the run.
# --------------------------------------------------------------------------- #
class EpCoordinateAuditV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"] = "1"
    contract_sha256: Hex64
    idf_sha256: Hex64
    eio_sha256: Hex64 | None
    err_sha256: Hex64 | None
    ignored_warning_hits: int
    completed: bool


# --------------------------------------------------------------------------- #
# §7.2 — registry (`ep25.1-v1`)
# --------------------------------------------------------------------------- #
COORDINATE_REGISTRY_VERSION = "ep25.1-v1"

REGISTRY: tuple[CoordinateObjectRule, ...] = (
    # frame controllers
    CoordinateObjectRule("Building", "North Axis", "always", "frame_controller",
                          "Building.North Axis", "supported", "BuildingConverter"),
    CoordinateObjectRule("Zone", "Direction of Relative North/X/Y/Z Origin", "always",
                          "frame_controller", "n/a", "supported", "ZoneConverter"),
    CoordinateObjectRule("GlobalGeometryRules", "Coordinate System/A4/A5", "always",
                          "frame_controller", "self", "supported", "SettingsConverter"),
    # detailed zone-bound
    CoordinateObjectRule("BuildingSurface:Detailed", "Vertex X/Y/Z", "always",
                          "detailed_zone_bound", "GlobalGeometryRules.A3",
                          "supported", "SurfaceConverter"),
    CoordinateObjectRule("Wall:Detailed", "Vertex X/Y/Z", "always", "detailed_zone_bound",
                          "GlobalGeometryRules.A3", "unsupported", "none"),
    CoordinateObjectRule("RoofCeiling:Detailed", "Vertex X/Y/Z", "always", "detailed_zone_bound",
                          "GlobalGeometryRules.A3", "unsupported", "none"),
    CoordinateObjectRule("Floor:Detailed", "Vertex X/Y/Z", "always", "detailed_zone_bound",
                          "GlobalGeometryRules.A3", "unsupported", "none"),
    CoordinateObjectRule("FenestrationSurface:Detailed", "Vertex X/Y/Z", "always",
                          "detailed_zone_bound", "GlobalGeometryRules.A3",
                          "supported", "FenestrationConverter"),
    CoordinateObjectRule("Shading:Zone:Detailed", "Vertex X/Y/Z", "always", "detailed_zone_bound",
                          "GlobalGeometryRules.A3", "unsupported", "none"),
    # building-origin bound
    CoordinateObjectRule("Shading:Building:Detailed", "Vertex X/Y/Z", "always",
                          "building_origin_bound", "GlobalGeometryRules.A3", "unsupported", "none"),
    CoordinateObjectRule("Shading:Building", "Starting X/Y/Z Coordinate", "always",
                          "building_rectangular_shading", "GlobalGeometryRules.A3",
                          "unsupported", "none"),
    # rectangular opaque (A5-controlled)
    *(
        CoordinateObjectRule(name, "Starting X/Y/Z Coordinate", "always", "rectangular_opaque",
                              "GlobalGeometryRules.A5", "unsupported", "none")
        for name in (
            "Wall:Exterior", "Wall:Adiabatic", "Wall:Underground", "Wall:Interzone", "Roof",
            "Ceiling:Adiabatic", "Ceiling:Interzone", "Floor:GroundContact",
            "Floor:Adiabatic", "Floor:Interzone",
        )
    ),
    # daylight coordinate (A4-controlled)
    CoordinateObjectRule("Daylighting:ReferencePoint", "X/Y/Z-Coordinate of Reference Point",
                          "always", "daylight_coordinate", "GlobalGeometryRules.A4",
                          "unsupported", "none"),
    CoordinateObjectRule("Output:IlluminanceMap", "X/Y coordinates", "always",
                          "daylight_coordinate", "GlobalGeometryRules.A4", "unsupported", "none"),
    # host-local (inherit host, no independent frame decision)
    *(
        CoordinateObjectRule(name, "Starting X/Y/Z Coordinate (relative to base surface)",
                              "always", "host_local", "inherits_host_surface",
                              "unsupported", "none")
        for name in ("Window", "Door", "GlazedDoor", "Window:Interzone", "Door:Interzone",
                     "GlazedDoor:Interzone")
    ),
    *(
        CoordinateObjectRule(name, "geometry derived from Window/Door Name", "always",
                              "host_local", "inherits_host_surface", "unsupported", "none")
        for name in ("Shading:Overhang", "Shading:Overhang:Projection", "Shading:Fin",
                     "Shading:Fin:Projection")
    ),
    # host-derived daylight (no independent building-frame vertices)
    *(
        CoordinateObjectRule(name, "Window/Building Surface Name reference", "always",
                              "host_derived_daylight", "inherits_host_surface",
                              "unsupported", "none")
        for name in ("DaylightingDevice:Tubular", "DaylightingDevice:Shelf",
                     "DaylightingDevice:LightWell", "Daylighting:DELight:ComplexFenestration")
    ),
    # site-world exempt (fixed to facility world; NOT rotated by Building North Axis)
    CoordinateObjectRule("Shading:Site", "Starting X/Y/Z Coordinate", "always",
                          "site_world_exempt", "GlobalGeometryRules.A3", "unsupported", "none"),
    CoordinateObjectRule("Shading:Site:Detailed", "Vertex X/Y/Z", "always",
                          "site_world_exempt", "GlobalGeometryRules.A3", "unsupported", "none"),
    # true-north parameter
    CoordinateObjectRule("AirflowNetwork:SimulationControl",
                          "Azimuth Angle of Long Axis of Building", "always",
                          "true_north_parameter", "n/a (defined from true north directly)",
                          "unsupported", "none"),
    # conditional orientation
    CoordinateObjectRule("Generator:PVWatts", "Azimuth Angle", "Array Geometry Type==TiltAzimuth",
                          "conditional_orientation", "n/a (predicate-dependent)",
                          "unsupported", "none"),
)

EXCLUSIONS: tuple[CoordinateExclusionRule, ...] = (
    CoordinateExclusionRule(
        "Site:Location", "Latitude/Longitude/Time Zone/Elevation",
        "defines the facility's geographic location, not a building-frame vertex; "
        "not subject to Building.North Axis rotation (georeference exempt)",
    ),
    CoordinateExclusionRule(
        "WindowProperty:StormWindow", "Window Name",
        "field-name-pattern false positive: this object only references a Window "
        "by name to swap a glazing layer seasonally — it carries no independent "
        "coordinate field of its own",
    ),
)

_REGISTRY_BY_TYPE: dict[str, CoordinateObjectRule] = {r.object_type: r for r in REGISTRY}
_EXCLUSIONS_BY_TYPE: dict[str, CoordinateExclusionRule] = {r.object_type: r for r in EXCLUSIONS}


def registered_object_types() -> frozenset[str]:
    return frozenset(_REGISTRY_BY_TYPE) | frozenset(_EXCLUSIONS_BY_TYPE)


def supported_object_types() -> frozenset[str]:
    return frozenset(t for t, r in _REGISTRY_BY_TYPE.items() if r.current_support == "supported")


# --------------------------------------------------------------------------- #
# §7.3 layer 1 — IDD candidate scan (real parse of the bundled IDD text)
# --------------------------------------------------------------------------- #
_IDD_PHRASES: tuple[str, ...] = (
    "GlobalGeometryRules coordinates",
    "relative to the building origin",
    "relative to the Zone Origin",
    "rotate with the BUILDING north axis",
    "Daylighting Reference Point Coordinate",
    "in world coordinates",
)
_IDD_FIELD_MARKERS: tuple[str, ...] = (
    "Starting X Coordinate",
    "Base Surface Name",
    "Window or Door Name",
    "Window Name",
    "Exterior Window Name",       # DaylightingDevice:LightWell
    "Dome Name",                  # DaylightingDevice:Tubular
    "Azimuth Angle of Long Axis of Building",
    "Array Geometry Type",
)
_IDD_OBJECT_NAME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9:_\-]*)\s*[,;]\s*(!.*)?$")


def parse_idd_object_blocks(idd_path: Path = DEFAULT_IDD_PATH) -> dict[str, str]:
    """Split the IDD text into ``{object_type: block_text}``. An object block
    starts at a column-0 line naming the object and runs until the next
    column-0 object-name line."""
    text = Path(idd_path).read_text(encoding="utf-8", errors="replace")
    blocks: dict[str, list[str]] = {}
    cur_name: str | None = None
    cur_lines: list[str] = []
    for line in text.split("\n"):
        m = _IDD_OBJECT_NAME_RE.match(line)
        if m and not line.startswith((" ", "\t")):
            if cur_name is not None:
                blocks[cur_name] = cur_lines
            cur_name = m.group(1)
            cur_lines = [line]
        elif cur_name is not None:
            cur_lines.append(line)
    if cur_name is not None:
        blocks[cur_name] = cur_lines
    return {name: "\n".join(lines) for name, lines in blocks.items()}


def idd_layer_candidates(idd_path: Path = DEFAULT_IDD_PATH) -> frozenset[str]:
    """Objects whose IDD block is tagged ``\\format vertices`` OR matches one of
    the documented coordinate-system-note phrases OR carries one of the
    documented coordinate-shaped field-name markers. This is a real scan of
    the bundled ``Energy+.idd`` (not a hardcoded snapshot) — extending the
    phrase/marker lists is how a future IDD upgrade gets re-audited."""
    blocks = parse_idd_object_blocks(idd_path)
    hits: set[str] = set()
    for name, block in blocks.items():
        if "\\format vertices" in block:
            hits.add(name)
            continue
        if any(p in block for p in _IDD_PHRASES):
            hits.add(name)
            continue
        if any(f"\\field {marker}" in block for marker in _IDD_FIELD_MARKERS):
            hits.add(name)
    return frozenset(hits)


def idd_layer_completeness_diff(idd_path: Path = DEFAULT_IDD_PATH) -> tuple[frozenset[str], frozenset[str]]:
    """Returns ``(unregistered, ghost)`` for the IDD layer alone. BO-CR6: the
    contract-level double-empty invariant is over the UNION of all candidate
    layers — use :func:`registry_completeness_diffs`, whose every diff must be
    empty; this function remains as the layer-1 slice of it."""
    candidates = idd_layer_candidates(idd_path)
    registered = registered_object_types()
    return frozenset(candidates - registered), frozenset(registered - candidates)


# --------------------------------------------------------------------------- #
# §7.3 layer 2 — schema/converter layer (BO-CR6)
# --------------------------------------------------------------------------- #
_SCHEMA_COORDINATE_FIELD_RE = re.compile(
    r"North Axis|Origin|Vertices|Coordinate System|Direction of Relative North"
    r"|Latitude|Longitude|Elevation|Azimuth|Starting [XYZ]",
    re.IGNORECASE,
)


def schema_layer_candidates() -> frozenset[str]:
    """Enumerate ``ConfigState.model_fields``' EnergyPlus object aliases and
    keep every object whose SCHEMA declares a coordinate-shaped field alias.
    This is a live reflection over the actual Pydantic schemas — a future
    schema/field addition changes this set and fails the diff test."""
    from src.mcp.state import ConfigState

    candidates: set[str] = set()
    for field_name, info in ConfigState.model_fields.items():
        alias = info.alias
        if not alias or ":" not in alias and alias not in (
            "Building", "Zone", "Material", "Construction", "People", "Light",
            "Schedule", "HVAC", "SimulationControl", "GlobalGeometryRules",
            "RunPeriod",
        ) and not alias.startswith("Output"):
            continue
        annotation = info.annotation
        # unwrap list[X] / X | None
        import typing

        schema_classes = []
        for candidate_type in typing.get_args(annotation) or (annotation,):
            args = typing.get_args(candidate_type)
            schema_classes.extend(a for a in (args or (candidate_type,))
                                  if hasattr(a, "model_fields"))
        if hasattr(annotation, "model_fields"):
            schema_classes.append(annotation)
        for schema_cls in schema_classes:
            for sub_info in schema_cls.model_fields.values():
                sub_alias = sub_info.alias or ""
                if _SCHEMA_COORDINATE_FIELD_RE.search(sub_alias):
                    candidates.add(alias)
                    break
    return frozenset(candidates)


# --------------------------------------------------------------------------- #
# §7.3 layer 3 — producer route layer (BO-CR6)
# --------------------------------------------------------------------------- #
# Every code path that writes a coordinate-candidate object type into an eppy
# IDF must be registered here (route id -> object type). The AST scan below
# discovers actual `newidfobject("<type>", ...)` writers; the diff test fails
# when a new coordinate producer appears without a route (or a route goes
# stale).
PRODUCER_ROUTES: tuple[tuple[str, str], ...] = (
    ("converters.building", "Building"),
    ("converters.zone", "Zone"),
    ("converters.settings.ggr", "GlobalGeometryRules"),
    ("converters.settings.site_location", "Site:Location"),
    ("converters.surface", "BuildingSurface:Detailed"),
    ("converters.fenestration", "FenestrationSurface:Detailed"),
    ("geometry.to_idf.zone", "Zone"),
    ("geometry.to_idf.surface", "BuildingSurface:Detailed"),
    ("geometry.to_idf.fenestration", "FenestrationSurface:Detailed"),
)

_PRODUCER_SCAN_ROOTS = ("src/converters", "src/agent/geometry", "src/mcp")


def producer_layer_scan(repo_root: Path | None = None) -> frozenset[str]:
    """AST-walk the producer scan roots for ``newidfobject("<type>", ...)``
    calls with a literal first argument; return the coordinate-candidate
    object types actually written (canonical registry casing)."""
    import ast

    root = Path(repo_root) if repo_root is not None else DEFAULT_IDD_PATH.parents[2]
    candidate_upper = _cached_idd_candidates_upper(str(DEFAULT_IDD_PATH)) | {
        name.upper() for name in registered_object_types()
    }
    found: set[str] = set()
    for scan_root in _PRODUCER_SCAN_ROOTS:
        for path in (root / scan_root).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "newidfobject"
                        and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)):
                    obj_type = node.args[0].value
                    if obj_type.upper() in candidate_upper:
                        found.add(_idf_object_type_to_registry_name(obj_type))
    return frozenset(found)


def producer_layer_diff(repo_root: Path | None = None) -> tuple[frozenset[str], frozenset[str]]:
    """(unrouted, stale_routes): coordinate producers in code without a
    registered route / routes whose object type no code writes."""
    actual = producer_layer_scan(repo_root)
    routed = frozenset(obj_type for _route, obj_type in PRODUCER_ROUTES)
    return frozenset(actual - routed), frozenset(routed - actual)


def registry_completeness_diffs(idd_path: Path = DEFAULT_IDD_PATH) -> dict[str, frozenset[str]]:
    """BO-CR6: the full four-layer closed-world audit. EVERY entry must be
    empty — including `ghost` (registry rows must be justified by the union
    of the IDD scan and the live schema layer; no standing exemption list)."""
    idd_candidates = idd_layer_candidates(idd_path)
    schema_candidates = schema_layer_candidates()
    all_candidates = idd_candidates | schema_candidates
    registered = registered_object_types()
    unrouted, stale_routes = producer_layer_diff()
    return {
        "idd_unregistered": frozenset(idd_candidates - registered),
        "schema_unregistered": frozenset(schema_candidates - registered),
        "ghost": frozenset(registered - all_candidates),
        "producer_unrouted": unrouted,
        "producer_stale_routes": stale_routes,
        "supported_without_producer_route": frozenset(
            supported_object_types() - {obj for _r, obj in PRODUCER_ROUTES}
        ),
    }


def registry_candidate_sha256(idd_path: Path = DEFAULT_IDD_PATH) -> str:
    """Hash the audited candidate universe, not merely the registry rows.

    The export/replay evidence must go stale when an IDD or schema candidate
    changes even if a reviewer forgot to edit the registry constant.
    """
    import hashlib

    candidates = sorted(idd_layer_candidates(idd_path) | schema_layer_candidates())
    return hashlib.sha256("\n".join(candidates).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# §7.3 layer 4 — final IDF layer (live eppy objects)
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=2)
def _cached_idd_candidates_upper(idd_path: str) -> frozenset[str]:
    return frozenset(name.upper() for name in idd_layer_candidates(Path(idd_path)))


def final_idf_layer_offenders(idf: "IDF") -> list[OutputCoordinateIssue]:
    """Walk the LIVE, converted IDF for any populated COORDINATE-CANDIDATE
    object type that is not in the registry (unclassified) or is registered
    but marked unsupported (a producer started emitting something we
    deliberately have not verified yet — must BLOCK, not silently pass).

    Candidacy is decided by the layer-1 IDD scan union the registry itself —
    Material / Schedule / HVAC and every other non-spatial object type in the
    converted IDF is not a coordinate candidate and is not this gate's
    business (spec §7.3 layer 4: "凡实际**空间候选**不在 rule/exclusion
    registry ... pre-EP BLOCK")."""
    issues: list[OutputCoordinateIssue] = []
    registered = _REGISTRY_BY_TYPE
    excluded = frozenset(_EXCLUSIONS_BY_TYPE)
    candidate_upper = _cached_idd_candidates_upper(str(DEFAULT_IDD_PATH))
    registry_upper = {name.upper() for name in registered_object_types()}
    for obj_type, objs in idf.idfobjects.items():
        if not objs:
            continue
        upper = obj_type.upper()
        if upper not in candidate_upper and upper not in registry_upper:
            continue  # not a spatial/coordinate candidate at all
        canonical = _idf_object_type_to_registry_name(obj_type)
        if canonical in excluded:
            continue
        rule = registered.get(canonical)
        if rule is None:
            issues.append(OutputCoordinateIssue(
                "UNCLASSIFIED_COORDINATE_OBJECT",
                f"IDF contains {len(objs)} populated {canonical!r} object(s) with no "
                "registry/exclusion entry",
                {"object_type": canonical, "count": len(objs)},
            ))
        elif rule.current_support != "supported":
            issues.append(OutputCoordinateIssue(
                "UNSUPPORTED_COORDINATE_OBJECT",
                f"IDF contains {len(objs)} populated {canonical!r} object(s), which the "
                f"registry marks unsupported ({rule.frame_class})",
                {"object_type": canonical, "count": len(objs), "frame_class": rule.frame_class},
            ))
        if rule is not None and rule.frame_class in ("host_local", "host_derived_daylight"):
            issues.extend(_host_chain_issues(canonical, objs, idf))
    return issues


_HOST_REF_FIELDS: tuple[str, ...] = (
    "Building_Surface_Name", "Window_or_Door_Name", "Window_Name",
    "Exterior_Window_Name", "Dome_Name", "Diffuser_Name",
)


def _host_chain_issues(canonical: str, objs, idf: "IDF") -> list[OutputCoordinateIssue]:
    """BO-CR6: a host-local object inherits its frame from a host surface —
    if the host reference is blank or unresolvable, the frame chain is broken
    and the object must BLOCK on its own account (spec §7.2: "出现须验证宿主
    链,否则 BLOCK"), independent of its supported/unsupported status."""
    known_hosts = {
        o.Name
        for key in ("BUILDINGSURFACE:DETAILED", "FENESTRATIONSURFACE:DETAILED",
                     "WINDOW", "DOOR", "GLAZEDDOOR")
        for o in idf.idfobjects.get(key, [])
    }
    issues: list[OutputCoordinateIssue] = []
    for obj in objs:
        host_ref = None
        for field in _HOST_REF_FIELDS:
            try:
                value = getattr(obj, field)
            except Exception:  # noqa: BLE001 — object type lacks this field
                continue
            host_ref = str(value or "")
            break
        if not host_ref:
            issues.append(OutputCoordinateIssue(
                "UNSUPPORTED_COORDINATE_OBJECT",
                f"host-local {canonical!r} object {getattr(obj, 'Name', '?')!r} has no host "
                "surface reference — its coordinate frame chain cannot be verified",
                {"object_type": canonical, "object": str(getattr(obj, "Name", ""))},
            ))
        elif host_ref not in known_hosts:
            issues.append(OutputCoordinateIssue(
                "UNSUPPORTED_COORDINATE_OBJECT",
                f"host-local {canonical!r} object {getattr(obj, 'Name', '?')!r} references host "
                f"{host_ref!r} which does not exist in the IDF — broken frame chain",
                {"object_type": canonical, "object": str(getattr(obj, "Name", "")), "host": host_ref},
            ))
    return issues


def _idf_object_type_to_registry_name(idf_object_type: str) -> str:
    """eppy's ``idfobjects`` keys are UPPERCASE with ``:`` kept (e.g.
    ``"BUILDINGSURFACE:DETAILED"``); the registry uses EnergyPlus's canonical
    mixed-case names. Match case-insensitively against the known registry
    keys rather than hand-writing a second name table."""
    upper = idf_object_type.upper()
    for name in registered_object_types():
        if name.upper() == upper:
            return name
    return idf_object_type


# --------------------------------------------------------------------------- #
# §8.1 — validate_output_coordinate_contract
# --------------------------------------------------------------------------- #
def validate_output_coordinate_contract(
    config: "ConfigState",
    contract: OutputCoordinateContract,
    context: OutputCoordinateValidationContext,
    *,
    idf: "IDF | None" = None,
    include_vertex_drift: bool = True,
) -> list[OutputCoordinateIssue]:
    """Run the E4 output-coordinate contract gate.

    Args:
        include_vertex_drift: The vertex-by-vertex comparison in step 6
            (``_vertex_drift_issues``) is a TERMINAL check — it requires the
            full geometry (surfaces + fenestration) to already exist in
            ``ConfigState``. Callers that run before the geometry kernel has
            built surfaces (``cross_ref_foundations``) pass ``False`` so the
            gate does not report every snapshot record as "missing from
            ConfigState" (a timing false-positive, not a real drift). Every
            boundary that runs after geometry is complete
            (``cross_ref_complete`` / ``validate`` / export) keeps the default
            ``True``, so no real drift is ever dropped. Steps 1–5 (contract
            identity, North Axis, GGR A3/A4/A5, Zone frame) and the snapshot
            hash-integrity sub-check always run regardless of this flag.
    """
    issues: list[OutputCoordinateIssue] = []

    # --- 1. contract strict round-trip ---
    try:
        OutputCoordinateContract.model_validate_json(contract.model_dump_json())
    except Exception as exc:  # noqa: BLE001 - report, don't crash the gate
        issues.append(OutputCoordinateIssue("CONTRACT_IDENTITY", f"contract failed strict round-trip: {exc}"))
        return issues

    # --- 2. source identity, re-hashed from context's raw bytes ---
    if isinstance(contract.source, AcceptedCorrectionRef):
        if context.verified_correction is None:
            issues.append(OutputCoordinateIssue(
                "CONTRACT_IDENTITY",
                "contract source is an accepted_correction ref but the validation "
                "context carries no verified_correction",
            ))
        else:
            actual_hash = sha256_bytes(context.verified_correction.raw_output_bytes)
            if actual_hash != contract.source.output_sha256:
                issues.append(OutputCoordinateIssue(
                    "CONTRACT_IDENTITY",
                    "verified correction raw bytes do not hash to contract.source.output_sha256",
                    {"expected": contract.source.output_sha256, "actual": actual_hash},
                ))
    elif isinstance(contract.source, LegacyStandaloneIntakeRef):
        actual_hash = sha256_bytes(context.raw_intake_output_bytes)
        if actual_hash != contract.source.intake_output_sha256:
            issues.append(OutputCoordinateIssue(
                "CONTRACT_IDENTITY",
                "raw IntakeOutput bytes do not hash to contract.source.intake_output_sha256",
                {"expected": contract.source.intake_output_sha256, "actual": actual_hash},
            ))

    # --- 3. Building North Axis ---
    if config.building is None:
        issues.append(OutputCoordinateIssue("BUILDING_NORTH_AXIS", "ConfigState.building is not seeded"))
    elif config.building.north_axis != contract.north_axis_deg:
        issues.append(OutputCoordinateIssue(
            "BUILDING_NORTH_AXIS",
            "ConfigState.building.north_axis does not equal contract.north_axis_deg",
            {"config": config.building.north_axis, "contract": contract.north_axis_deg},
        ))

    # --- 4. GGR A3/A4/A5 ---
    ggr = config.global_geometry_rules
    ggr_checks = (
        ("A3", ggr.coordinate_system, contract.global_geometry_coordinate_system),
        ("A4", ggr.daylighting_reference_point_coordinate_system,
         contract.daylighting_reference_point_coordinate_system),
        ("A5", ggr.rectangular_surface_coordinate_system, contract.rectangular_surface_coordinate_system),
    )
    for field_id, actual, expected in ggr_checks:
        if actual != expected:
            issues.append(OutputCoordinateIssue(
                "GGR_COORDINATE_SYSTEM", f"GlobalGeometryRules {field_id} is {actual!r}, expected {expected!r}",
                {"field": field_id, "actual": actual, "expected": expected},
            ))

    # --- 5. Zone frame (ALL zones, spec §6.2 item 6) ---
    if contract.zone_origin_policy == "all_zero":
        offenders = sorted(
            zone.name for zone in config.zones
            if (zone.x_origin, zone.y_origin, zone.z_origin, zone.direction_of_relative_north or 0.0)
            != (0.0, 0.0, 0.0, 0.0)
        )
        if offenders:
            issues.append(OutputCoordinateIssue(
                "ZONE_FRAME_NONZERO", f"{len(offenders)} zone(s) have a nonzero frame under an all_zero policy",
                {"offenders": offenders},
            ))

    # --- 6. vertex/frame-drift against the coordinate snapshot ---
    snapshot_bytes = context.raw_snapshot_bytes
    if snapshot_bytes is not None:
        snapshot = OutputCoordinateSnapshotV1.model_validate_json(snapshot_bytes.decode("utf-8"))
        if contract.geometry_snapshot_sha256 != sha256_bytes(snapshot_bytes):
            issues.append(OutputCoordinateIssue(
                "VERTEX_FRAME_DRIFT", "raw snapshot bytes do not hash to contract.geometry_snapshot_sha256",
            ))
        elif include_vertex_drift:
            # TERMINAL check (see validate_output_coordinate_contract docstring):
            # _vertex_drift_issues needs the full geometry in ConfigState. At a
            # pre-geometry boundary (cross_ref_foundations) surfaces do not exist
            # yet, so this is skipped there; cross_ref_complete / validate /
            # export keep it on and catch every real drift once geometry is done.
            issues.extend(_vertex_drift_issues(config, snapshot))
    elif contract.geometry_snapshot_sha256 is not None:
        issues.append(OutputCoordinateIssue(
            "VERTEX_FRAME_DRIFT",
            "contract declares a geometry_snapshot_sha256 but the validation context carries no snapshot bytes",
        ))

    # --- 7. live-IDF stage (BO-CR5): the FINAL gate reads the actual emitted
    # IDF fields — Building.North_Axis, GGR A3/A4/A5, every Zone's four frame
    # fields, and the vertices against the snapshot — never trusting that the
    # in-memory ConfigState still matches what a converter / raw-fragment
    # injection actually wrote. Plus the building-bound object registry scan.
    if idf is not None:
        issues.extend(_live_idf_field_issues(contract, idf))
        if snapshot_bytes is not None and contract.geometry_snapshot_sha256 == sha256_bytes(snapshot_bytes):
            issues.extend(_live_idf_vertex_drift_issues(
                OutputCoordinateSnapshotV1.model_validate_json(snapshot_bytes.decode("utf-8")), idf,
            ))
        issues.extend(final_idf_layer_offenders(idf))

    return issues


def _live_idf_field_issues(contract: OutputCoordinateContract, idf: "IDF") -> list[OutputCoordinateIssue]:
    issues: list[OutputCoordinateIssue] = []

    buildings = idf.idfobjects.get("BUILDING", [])
    if len(buildings) != 1:
        issues.append(OutputCoordinateIssue(
            "BUILDING_NORTH_AXIS", f"live IDF has {len(buildings)} Building object(s), expected exactly 1",
        ))
    else:
        actual = float(buildings[0].North_Axis or 0.0)
        if actual != contract.north_axis_deg:
            issues.append(OutputCoordinateIssue(
                "BUILDING_NORTH_AXIS",
                f"live IDF Building.North Axis is {actual!r}, contract requires {contract.north_axis_deg!r}",
                {"actual": actual, "expected": contract.north_axis_deg},
            ))

    ggrs = idf.idfobjects.get("GLOBALGEOMETRYRULES", [])
    if len(ggrs) != 1:
        issues.append(OutputCoordinateIssue(
            "GGR_COORDINATE_SYSTEM", f"live IDF has {len(ggrs)} GlobalGeometryRules object(s), expected exactly 1",
        ))
    else:
        ggr = ggrs[0]
        # A4/A5 may be blank in a hand-written legacy IDF; blank means the IDD
        # default Relative.
        live = (
            ("A3", str(ggr.Coordinate_System or ""), contract.global_geometry_coordinate_system),
            ("A4", str(ggr.Daylighting_Reference_Point_Coordinate_System or "Relative"),
             contract.daylighting_reference_point_coordinate_system),
            ("A5", str(ggr.Rectangular_Surface_Coordinate_System or "Relative"),
             contract.rectangular_surface_coordinate_system),
        )
        for field_id, actual, expected in live:
            if actual != expected:
                issues.append(OutputCoordinateIssue(
                    "GGR_COORDINATE_SYSTEM",
                    f"live IDF GlobalGeometryRules {field_id} is {actual!r}, contract requires {expected!r}",
                    {"field": field_id, "actual": actual, "expected": expected},
                ))

    if contract.zone_origin_policy == "all_zero":
        offenders = []
        for zone in idf.idfobjects.get("ZONE", []):
            frame = (
                float(zone.X_Origin or 0.0), float(zone.Y_Origin or 0.0),
                float(zone.Z_Origin or 0.0), float(zone.Direction_of_Relative_North or 0.0),
            )
            if frame != (0.0, 0.0, 0.0, 0.0):
                offenders.append(zone.Name)
        if offenders:
            issues.append(OutputCoordinateIssue(
                "ZONE_FRAME_NONZERO",
                f"{len(offenders)} live-IDF Zone object(s) have a nonzero frame under an all_zero policy",
                {"offenders": sorted(offenders)},
            ))
    return issues


def _idf_vertices(obj) -> tuple[tuple[float, float, float], ...]:
    verts = []
    i = 1
    while True:
        try:
            x = getattr(obj, f"Vertex_{i}_Xcoordinate")
            y = getattr(obj, f"Vertex_{i}_Ycoordinate")
            z = getattr(obj, f"Vertex_{i}_Zcoordinate")
        except Exception:  # noqa: BLE001 - eppy raises BadEPFieldError past the field list
            break
        if x == "" or y == "" or z == "":
            break
        verts.append((round(float(x), 2), round(float(y), 2), round(float(z), 2)))
        i += 1
    return tuple(verts)


def _live_idf_vertex_drift_issues(
    snapshot: OutputCoordinateSnapshotV1, idf: "IDF",
) -> list[OutputCoordinateIssue]:
    """BO-CR5: compare the LIVE IDF's surface/fenestration name/host/vertices
    against the canonical snapshot — the proof the coordinate-frame switch
    never rotated/translated a vertex must hold on the actual emitted IDF."""
    issues: list[OutputCoordinateIssue] = []
    surfaces = {o.Name: o for o in idf.idfobjects.get("BUILDINGSURFACE:DETAILED", [])}
    fenestrations = {o.Name: o for o in idf.idfobjects.get("FENESTRATIONSURFACE:DETAILED", [])}
    for rec in snapshot.records:
        pool = surfaces if rec.object_type == "BuildingSurface:Detailed" else fenestrations
        obj = pool.get(rec.name)
        if obj is None:
            issues.append(OutputCoordinateIssue(
                "VERTEX_FRAME_DRIFT",
                f"{rec.object_type} {rec.name!r} in the snapshot is missing from the live IDF",
            ))
            continue
        host = obj.Zone_Name if rec.object_type == "BuildingSurface:Detailed" else obj.Building_Surface_Name
        if host != rec.zone_or_parent:
            issues.append(OutputCoordinateIssue(
                "VERTEX_FRAME_DRIFT",
                f"live IDF {rec.name!r} host changed: snapshot={rec.zone_or_parent!r} idf={host!r}",
            ))
        actual = _idf_vertices(obj)
        expected = tuple(tuple(round(float(c), 2) for c in v) for v in rec.vertices)
        if actual != expected:
            issues.append(OutputCoordinateIssue(
                "VERTEX_FRAME_DRIFT",
                f"live IDF {rec.name!r} vertices differ from the pre-E4 snapshot",
                {"snapshot": expected, "actual": actual},
            ))
    return issues


def _vertex_drift_issues(config: "ConfigState", snapshot: OutputCoordinateSnapshotV1) -> list[OutputCoordinateIssue]:
    issues: list[OutputCoordinateIssue] = []
    surfaces_by_name = {s.name: s for s in config.surfaces}
    fenestrations_by_name = {f.name: f for f in config.fenestrations}
    for rec in snapshot.records:
        obj = (surfaces_by_name if rec.object_type == "BuildingSurface:Detailed" else fenestrations_by_name).get(rec.name)
        if obj is None:
            issues.append(OutputCoordinateIssue(
                "VERTEX_FRAME_DRIFT", f"{rec.object_type} {rec.name!r} in the snapshot is missing from ConfigState",
            ))
            continue
        host = obj.zone_name if rec.object_type == "BuildingSurface:Detailed" else obj.building_surface_name
        if host != rec.zone_or_parent:
            issues.append(OutputCoordinateIssue(
                "VERTEX_FRAME_DRIFT", f"{rec.name!r} host changed: snapshot={rec.zone_or_parent!r} config={host!r}",
            ))
        actual_vertices = tuple(
            tuple(round(float(c), 2) for c in vertex) for vertex in obj.vertices
        )
        if actual_vertices != rec.vertices:
            issues.append(OutputCoordinateIssue(
                "VERTEX_FRAME_DRIFT", f"{rec.name!r} vertices differ from the pre-E4 snapshot",
                {"snapshot": rec.vertices, "actual": actual_vertices},
            ))
    return issues
