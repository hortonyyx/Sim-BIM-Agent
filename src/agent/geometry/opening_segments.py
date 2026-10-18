"""Logical drawing openings backed by thickness-uniform physical fragments."""
from __future__ import annotations

import copy
from shapely.geometry import Polygon, box
from shapely.ops import unary_union


def logical_openings(source):
    if not source.get('opening_segment_groups'):
        return copy.deepcopy(source['openings'])
    rows = {r['id']: copy.deepcopy(r) for r in source['openings']}
    if len(rows) != len(source['openings']):
        raise ValueError('duplicate source opening identity')
    consumed = set()
    for identity, ids in source.get('opening_segment_groups', {}).items():
        if (not isinstance(ids, list) or len(ids) < 2 or identity not in ids
                or len(set(ids)) != len(ids) or consumed.intersection(ids)
                or not set(ids).issubset(rows)):
            raise ValueError('invalid logical opening segment group')
        parts = [rows[i] for i in ids]
        first = parts[0]
        if any(any(p.get(k) != first.get(k) for k in ('kind', 'space_ids', 'exterior', 'connectivity'))
               for p in parts):
            raise ValueError('opening fragments disagree on kind or connectivity')
        vertices = [v for p in parts for v in p['vertices']]
        constant = [i for i in (0, 1) if max(v[i] for v in vertices)-min(v[i] for v in vertices) < 1e-7]
        if len(constant) != 1:
            raise ValueError('opening fragments must share one orthogonal wall plane')
        normal, along = constant[0], 1-constant[0]
        shapes = [Polygon([(v[along], v[2]) for v in p['vertices']]) for p in parts]
        union = unary_union(shapes)
        if (not union.equals(box(*union.bounds))
                or abs(sum(s.area for s in shapes)-union.area) > 1e-10):
            raise ValueError('opening fragments must cover one rectangle without gaps or overlaps')
        lo, bottom, hi, top = union.bounds
        def point(at, z):
            xy = [0., 0.]
            xy[normal], xy[along] = vertices[0][normal], at
            return [*xy, z]
        for key in ids:
            rows.pop(key)
        rows[identity] = {**first, 'id': identity, 'segment_ids': list(ids),
                          'vertices': [point(lo, bottom), point(hi, bottom), point(hi, top), point(lo, top)]}
        consumed.update(ids)
    return list(rows.values())


def expand_opening_policy(source, policy):
    result = copy.deepcopy(policy or {})
    logical_openings(source)  # Validate groups before allowing shared simulation decisions.
    for identity, ids in source.get('opening_segment_groups', {}).items():
        if identity in result:
            for part in ids:
                result.setdefault(part, copy.deepcopy(result[identity]))
    return result
