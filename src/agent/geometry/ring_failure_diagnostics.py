"""Bounded, read-only explanations for an already rejected polygon ring.

This module never decides validity for adoption and never repairs coordinates.
GEOS reports one validity location, not every fault in a polygon. Local edge
samples are verified by actual intersections; a nearest edge is not evidence.
"""
from __future__ import annotations

import math
import re

from shapely.errors import GEOSException
from shapely.geometry import LineString, Point, Polygon
from shapely.validation import explain_validity


_EDGE_LIMIT = 16
_SAMPLE_LIMIT = 4


def _edge(ring, index):
    return {'index': index, 'p1': list(ring[index]),
            'p2': list(ring[(index + 1) % len(ring)])}


def _location(reason):
    match = re.search(r'\[([^\[\]]+)\]$', reason)
    if match:
        try:
            point = [float(value) for value in match[1].split()]
            if len(point) == 2 and all(math.isfinite(value) for value in point):
                return point
        except ValueError:
            pass
    return None


def _intersection_geometry(intersection):
    if intersection.geom_type == 'Point':
        return {'type': 'Point', 'coordinates': list(intersection.coords[0])}
    if intersection.geom_type == 'LineString':
        return {'type': 'LineString', 'coordinates': [list(p) for p in intersection.coords]}
    # Two straight segments only intersect in a point or straight overlap.
    return None


def ring_failure_diagnostic(ring, *, source_ring=None, epsilon):
    """Explain an existing rejection, with O(n) lookup and bounded pair work.

    ``epsilon`` only localizes the GEOS-reported point and lists short edges. It
    does not create a contact, change a coordinate or alter any acceptance gate.
    The result deliberately says unlocated when no reliable pair can be proved.
    """
    result = {
        'diagnostic_only': True,
        'basis': 'adopted_ring' if source_ring is not None else 'input_ring',
        'vertex_count': len(ring),
        'edge_indexing': 'zero-based vertex i to (i + 1) modulo vertex_count',
        'location_status': 'unlocated',
        'intersection_samples': [],
        'short_edge_samples': [],
        'truncated': False,
        'scope': 'Samples near the first GEOS validity location, not an exhaustive intersection search; no geometry repair.',
    }
    try:
        polygon = Polygon(ring)
        result.update(validity_reason=explain_validity(polygon), is_valid=polygon.is_valid,
                      area_m2=polygon.area)
        if source_ring is not None:
            source_polygon = Polygon(source_ring)
            result['source'] = {'validity_reason': explain_validity(source_polygon),
                                'is_valid': source_polygon.is_valid, 'area_m2': source_polygon.area}
        short = [i for i, (a, b) in enumerate(zip(ring, ring[1:] + ring[:1]))
                 if math.dist(a, b) <= epsilon]
        result['short_edge_count'] = len(short)
        result['short_edge_sample_limit'] = _SAMPLE_LIMIT
        result['short_edge_samples'] = [
            {**_edge(ring, i), 'length_m': math.dist(ring[i], ring[(i + 1) % len(ring)]),
             **({'source_edge': _edge(source_ring, i)} if source_ring is not None else {})}
            for i in short[:_SAMPLE_LIMIT]]
        result['short_edges_truncated'] = len(short) > _SAMPLE_LIMIT
        result['truncated'] = result['short_edges_truncated']
        location = _location(result['validity_reason'])
        if location is None:
            result['unlocated_reason'] = 'Validity report has no finite XY location; short edges, if present, are listed separately.'
            return result
        result['validity_location_xy'] = location
        # GEOS formats its diagnostic coordinate as text. Account only for
        # floating-point text/coordinate precision when finding nearby edges.
        tolerance = max(epsilon, 32 * max(math.ulp(value) for value in location))
        result['localization_tolerance_m'] = tolerance
        point = Point(location)
        nearby = []
        for i, (a, b) in enumerate(zip(ring, ring[1:] + ring[:1])):
            segment = LineString([a, b])
            if segment.distance(point) <= tolerance:
                nearby.append((i, segment))
        result['candidate_edge_count'] = len(nearby)
        result['candidate_edge_limit'] = _EDGE_LIMIT
        result['candidate_edges_truncated'] = len(nearby) > _EDGE_LIMIT
        nearby = nearby[:_EDGE_LIMIT]
        samples = []
        checked = 0
        for offset, (i, left) in enumerate(nearby):
            for j, right in nearby[offset + 1:]:
                checked += 1
                intersection = left.intersection(right)
                if intersection.is_empty or intersection.distance(point) > tolerance:
                    continue
                adjacent = (j - i) % len(ring) in (1, len(ring) - 1)
                # Ordinary neighboring edges share a vertex in every valid ring.
                if adjacent and intersection.geom_type == 'Point':
                    continue
                geometry = _intersection_geometry(intersection)
                if geometry is None:
                    continue
                sample = {'edge_indices': [i, j], 'intersection': geometry,
                          'edges': [_edge(ring, i), _edge(ring, j)]}
                if source_ring is not None:
                    source_edges = [_edge(source_ring, k) for k in (i, j)]
                    sample.update(source_edges=source_edges,
                        source_segment_distance_m=LineString([source_edges[0]['p1'], source_edges[0]['p2']]).distance(
                            LineString([source_edges[1]['p1'], source_edges[1]['p2']])))
                samples.append(sample)
        result.update(examined_pair_count=checked, intersections_in_examined_pairs=len(samples),
                      intersection_sample_limit=_SAMPLE_LIMIT,
                      intersection_samples=samples[:_SAMPLE_LIMIT],
                      intersection_samples_truncated=len(samples) > _SAMPLE_LIMIT)
        result['truncated'] = any(result[key] for key in (
            'short_edges_truncated', 'candidate_edges_truncated', 'intersection_samples_truncated'))
        if samples:
            result['location_status'] = 'located'
        else:
            result['unlocated_reason'] = 'No non-neighbor point intersection or overlapping edge pair was verified near the reported location within the bounded search.'
        return result
    except (GEOSException, TypeError, ValueError, IndexError, OverflowError) as error:
        # A diagnostic must not replace the original rejection with an exception.
        result['unlocated_reason'] = f'Diagnostic unavailable: {type(error).__name__}: {error}'
        return result
