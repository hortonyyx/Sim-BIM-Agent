"""Shared, pure geometric ring ordering for BIM and optional IDF consumers."""
from __future__ import annotations

import numpy as np
from shapely.geometry import Polygon
from shapely.validation import explain_validity


def canonicalize_ring_vertices(points: np.ndarray, normal_vector: np.ndarray) -> np.ndarray:
    """Canonicalize an *ordered simple planar ring* for IDF output.

    ``points`` must already follow the polygon boundary, with any starting
    vertex and either winding. That supplied adjacency is authoritative: an
    unordered point set cannot be reconstructed unambiguously, because one
    set of concave-polygon vertices can describe multiple simple rings.
    Self-intersecting/non-simple orders are therefore rejected with
    ``canonicalize_ring_vertices.non_simple_ring`` rather than guessed at.

    Canonicalization preserves every vertex and undirected boundary edge. It
    only (1) reverses the complete ring when its winding opposes the surface's
    independently-known ``normal_vector``, then (2) rotates the ring to start
    at the ``GlobalGeometryRules = UpperLeftCorner`` vertex (see
    :func:`top_left_corner_index`).
    """
    points = np.asarray(points, dtype=float)
    normal_vector = np.asarray(normal_vector, dtype=float)

    if points.ndim != 2 or points.shape[1:] != (3,):
        raise ValueError(
            "canonicalize_ring_vertices.non_simple_ring: ordered simple ring "
            f"must have shape (n, 3) (shape={points.shape!r})"
        )
    if not np.all(np.isfinite(points)):
        raise ValueError(
            "canonicalize_ring_vertices.non_simple_ring: ordered simple ring "
            f"contains a non-finite coordinate (vertex_count={len(points)})"
        )
    if len(points) < 3:
        raise ValueError(
            "canonicalize_ring_vertices.non_simple_ring: ordered simple ring "
            f"requires at least 3 vertices (vertex_count={len(points)})"
        )
    if len(np.unique(points, axis=0)) != len(points):
        raise ValueError(
            "canonicalize_ring_vertices.non_simple_ring: ordered simple ring "
            f"contains repeated vertices (vertex_count={len(points)})"
        )

    normal_norm = float(np.linalg.norm(normal_vector))
    if not np.isfinite(normal_norm) or normal_norm == 0.0:
        raise ValueError(
            "canonicalize_ring_vertices.invalid_normal: normal_vector must be "
            f"finite and non-zero (normal_vector={normal_vector.tolist()!r})"
        )
    normal = normal_vector / normal_norm

    # A planar polygon remains topologically identical when projected by
    # dropping the coordinate where its normal has greatest magnitude. That
    # gives Shapely a non-degenerate 2-D view for horizontal, vertical, and
    # oblique surfaces alike, without importing a second ordering algorithm.
    drop_axis = int(np.argmax(np.abs(normal)))
    projected = np.delete(points, drop_axis, axis=1)
    polygon = Polygon(projected)
    if not polygon.is_valid or not polygon.exterior.is_simple or polygon.area == 0.0:
        raise ValueError(
            "canonicalize_ring_vertices.non_simple_ring: ordered simple ring "
            f"required (vertex_count={len(points)}, "
            f"reason={explain_validity(polygon)!r})"
        )

    ring_normal = np.sum(np.cross(points, np.roll(points, -1, axis=0)), axis=0)
    ordered_points = points[::-1] if float(np.dot(ring_normal, normal)) < 0.0 else points
    top_left_index = top_left_corner_index(ordered_points, normal_vector)

    return np.roll(ordered_points, -top_left_index, axis=0)


def top_left_corner_index(points: np.ndarray, normal_vector: np.ndarray) -> int:
    """Index within `points` of the IDF ``UpperLeftCorner`` vertex relative to
    `normal_vector`: the corner a viewer standing outside the surface (facing
    along ``-normal_vector``, with world +Z as "up" unless the surface is
    itself near-horizontal) would call top-left."""
    normal = normal_vector / np.linalg.norm(normal_vector)

    world_up = np.array([0, 0, 1])

    if abs(np.dot(normal, world_up)) > 0.99:
        if np.dot(normal, world_up) > 0:
            world_up = np.array([0, 1, 0])
        else:
            world_up = np.array([0, -1, 0])

    right = np.cross(world_up, normal)
    right /= np.linalg.norm(right)

    up = np.cross(normal, right)
    up /= np.linalg.norm(up)

    centroid = np.mean(points, axis=0)
    relative_points = points - centroid

    x_coords = np.dot(relative_points, right)
    y_coords = np.dot(relative_points, up)

    sort_keys = np.column_stack((-y_coords, x_coords))
    top_left_index = np.lexsort((sort_keys[:, 1], sort_keys[:, 0]))[0]

    return top_left_index

