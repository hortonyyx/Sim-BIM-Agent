"""Geometry schema/profile compatibility rules.

``schema_version`` declares the shape contract of the correction artifact.
``capability_profile`` declares what this run is allowed to consume.
"""

from __future__ import annotations

from typing import Any

from src.agent.correction.constants import SCHEMA_VERSION_V1, SCHEMA_VERSION_V2, SCHEMA_VERSION_V3, SCHEMA_VERSION_SOURCE_POLYGON
SHAPE_RECTANGULAR = "rectangular"
SHAPE_ORTHOGONAL_POLYGON = "orthogonal_polygon"
SHAPE_SIMPLE_POLYGON = "simple_polygon"

CAPABILITY_PROFILE_RECTANGULAR = "rectangular"
CAPABILITY_PROFILE_ORTHOGONAL_POLYGON = "orthogonal_polygon"
CAPABILITY_PROFILE_SIMPLE_POLYGON = "simple_polygon"

CHECK_SCHEMA_VERSION_SUPPORTED = "correction.schema_version_supported"
CHECK_CAPABILITY_PROFILE_SHAPES = "correction.capability_profile_shapes"

FEATURE_CELL_POLYGON = "cell_polygon"
FEATURE_PER_FLOOR_FOOTPRINT = "per_floor_footprint"
FEATURE_FACADE_SEGMENTS = "facade_segments"
FEATURE_TYPED_NORTH_AXIS = "typed_north_axis"
FEATURE_SIMPLE_POLYGON = "simple_polygon"

SUPPORTED_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION_V1, SCHEMA_VERSION_V2, SCHEMA_VERSION_V3, SCHEMA_VERSION_SOURCE_POLYGON})
SCHEMA_VERSION_SHAPES = {
    SCHEMA_VERSION_V1: frozenset({SHAPE_RECTANGULAR}),
    SCHEMA_VERSION_V2: frozenset({SHAPE_RECTANGULAR, SHAPE_ORTHOGONAL_POLYGON}),
    SCHEMA_VERSION_V3: frozenset({SHAPE_RECTANGULAR, SHAPE_ORTHOGONAL_POLYGON}),
    SCHEMA_VERSION_SOURCE_POLYGON: frozenset({SHAPE_RECTANGULAR, SHAPE_ORTHOGONAL_POLYGON, SHAPE_SIMPLE_POLYGON}),
}
SCHEMA_VERSION_FEATURES = {
    SCHEMA_VERSION_V1: frozenset(),
    SCHEMA_VERSION_V2: frozenset({FEATURE_CELL_POLYGON}),
    SCHEMA_VERSION_V3: frozenset({
        FEATURE_CELL_POLYGON, FEATURE_PER_FLOOR_FOOTPRINT,
        FEATURE_FACADE_SEGMENTS, FEATURE_TYPED_NORTH_AXIS,
    }),
    SCHEMA_VERSION_SOURCE_POLYGON: frozenset({FEATURE_CELL_POLYGON, FEATURE_PER_FLOOR_FOOTPRINT, FEATURE_SIMPLE_POLYGON}),
}
CAPABILITY_PROFILE_SHAPES = {
    CAPABILITY_PROFILE_RECTANGULAR: frozenset({SHAPE_RECTANGULAR}),
    CAPABILITY_PROFILE_ORTHOGONAL_POLYGON: frozenset(
        {SHAPE_RECTANGULAR, SHAPE_ORTHOGONAL_POLYGON}
    ),
    CAPABILITY_PROFILE_SIMPLE_POLYGON: frozenset({SHAPE_RECTANGULAR, SHAPE_ORTHOGONAL_POLYGON, SHAPE_SIMPLE_POLYGON}),
}


def schema_version_of(geom: Any) -> str:
    return str(getattr(geom, "schema_version", SCHEMA_VERSION_V1) or SCHEMA_VERSION_V1)


def schema_version_supported(geom: Any) -> bool:
    return schema_version_of(geom) in SUPPORTED_SCHEMA_VERSIONS


def schema_supports(version_or_geom: Any, feature: str) -> bool:
    """Whether a known schema statically declares ``feature`` (unknown = false)."""
    version = version_or_geom if isinstance(version_or_geom, str) else schema_version_of(version_or_geom)
    return feature in SCHEMA_VERSION_FEATURES.get(str(version), frozenset())


def declared_shapes(geom: Any) -> frozenset[str]:
    version = schema_version_of(geom)
    return SCHEMA_VERSION_SHAPES.get(version, frozenset())


def allowed_shapes(capability_profile: str) -> frozenset[str]:
    return CAPABILITY_PROFILE_SHAPES.get(capability_profile, frozenset())


def capability_profile_allows(
    geom: Any, capability_profile: str
) -> bool:
    shapes = declared_shapes(geom)
    return bool(shapes) and shapes.issubset(allowed_shapes(capability_profile))


def require_supported_geometry_contract(
    geom: Any, capability_profile: str
) -> None:
    version = schema_version_of(geom)
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_SCHEMA_VERSIONS))
        raise ValueError(
            f"{CHECK_SCHEMA_VERSION_SUPPORTED}: unsupported schema_version "
            f"{version!r}; supported versions: {supported}"
        )
    shapes = declared_shapes(geom)
    allowed = allowed_shapes(capability_profile)
    if not shapes.issubset(allowed):
        raise ValueError(
            f"{CHECK_CAPABILITY_PROFILE_SHAPES}: capability_profile "
            f"{capability_profile!r} allows {sorted(allowed)}, but "
            f"schema_version {version!r} declares {sorted(shapes)}"
        )
