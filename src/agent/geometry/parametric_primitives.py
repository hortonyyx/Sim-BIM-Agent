"""Lower explicit local rectangles and aperture baselines to the common schema.

This is declaration expansion, not fitting: unions happen in the declared local
frame, retain one physical space and must form one ring without holes. Original
plans remain the caller's provenance authority; this module only returns copies.
"""
from __future__ import annotations

import copy
import math

from shapely.geometry import Polygon, box
from shapely.geometry.polygon import orient
from shapely.ops import unary_union


def _fields(value, required, label):
    if not isinstance(value, dict) or set(value) != set(required):
        raise ValueError(f'{label} requires exactly {", ".join(required)}')


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{label} must contain finite numbers')
    return float(value)


def _values(value, count, label):
    if not isinstance(value, list) or len(value) != count:
        raise ValueError(f'{label} requires {count} numbers')
    return [_number(v, label) for v in value]


def _frames(plan):
    declarations = plan.get('primitive_frames', {})
    if not isinstance(declarations, dict):
        raise ValueError('primitive_frames must be an object')
    frames = {}
    for identity, row in declarations.items():
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError('primitive frame IDs must be nonblank strings')
        _fields(row, ('origin', 'yaw_degrees'), f'primitive_frames.{identity}')
        origin = _values(row['origin'], 2, 'primitive frame origin')
        yaw = _number(row['yaw_degrees'], 'primitive frame yaw_degrees')
        angle = math.radians(yaw % 360)
        c, s = math.cos(angle), math.sin(angle)
        # Exact quarter-turns must not acquire artificial slanted edges.
        c = 0.0 if abs(c) < 1e-15 else c
        s = 0.0 if abs(s) < 1e-15 else s
        frames[identity] = dict(origin=origin, yaw_degrees=yaw, c=c, s=s)
    return frames


def _templates(plan):
    templates = plan.get('templates', {})
    if not isinstance(templates, dict) or any(not isinstance(row, dict) for row in templates.values()):
        raise ValueError('templates must be an object of template objects')
    return templates


def _rows(owner, collection):
    rows = owner.get(collection, [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f'{collection} must be a list of objects')
    return rows


def _frame(value, frames):
    identity = value.get('frame')
    if not isinstance(identity, str) or identity not in frames:
        raise ValueError(f'unknown primitive frame {identity!r}')
    return frames[identity]


def _world(point, frame):
    x, y = point
    origin, c, s = frame['origin'], frame['c'], frame['s']
    values = [origin[0] + c*x - s*y, origin[1] + s*x + c*y]
    if not all(math.isfinite(value) for value in values):
        raise ValueError('primitive transform exceeds finite coordinates')
    return [round(value, 12) for value in values]


def _primitive(value, frames):
    _fields(value, ('frame', 'rects'), 'primitive')
    frame = _frame(value, frames)
    rects = value['rects']
    if not isinstance(rects, list) or not 1 <= len(rects) <= 1000:
        raise ValueError('primitive rects requires 1 to 1000 explicit rectangles')
    bounds = []
    for rect in rects:
        x0, y0, x1, y1 = _values(rect, 4, 'primitive rectangle')
        if x0 >= x1 or y0 >= y1:
            raise ValueError('primitive rectangle needs positive width and height')
        bounds.append((x0, y0, x1, y1))
    # Input component order and duplicate rectangles cannot change ring IDs.
    polygon = unary_union([box(*row) for row in sorted(set(bounds))])
    if (polygon.geom_type != 'Polygon' or not polygon.is_valid
            or not math.isfinite(polygon.area) or polygon.area <= 0 or polygon.interiors):
        raise ValueError('primitive union must be one connected positive-area polygon without holes')
    points = list(orient(polygon, sign=1).exterior.coords)[:-1]
    # Remove only exactly collinear local axis vertices, never simplify a shape.
    points = [point for index, point in enumerate(points)
        if not (points[index-1][0] == point[0] == points[(index+1) % len(points)][0]
                or points[index-1][1] == point[1] == points[(index+1) % len(points)][1])]
    start = min(range(len(points)), key=lambda index: points[index])
    points = points[start:] + points[:start]
    result = [_world(point, frame) for point in points]
    transformed = Polygon(result)
    if not transformed.is_valid or transformed.area <= 0 or len(set(map(tuple, result))) != len(result):
        raise ValueError('primitive transform collapses or invalidates its ring')
    return result


def _baseline(row, frames):
    if 'local_baseline' not in row:
        return
    if 'p1' in row or 'p2' in row:
        raise ValueError('local_baseline and p1/p2 are mutually exclusive')
    baseline = row.pop('local_baseline')
    _fields(baseline, ('frame', 'axis', 'fixed', 'range'), 'local_baseline')
    frame = _frame(baseline, frames)
    axis = baseline['axis']
    if axis not in ('u', 'v'):
        raise ValueError('local_baseline axis must be u or v')
    fixed = _number(baseline['fixed'], 'local_baseline fixed')
    lo, hi = _values(baseline['range'], 2, 'local_baseline range')
    if lo >= hi:
        raise ValueError('local_baseline range must have positive length')
    points = [[value, fixed] if axis == 'u' else [fixed, value] for value in (lo, hi)]
    row['p1'], row['p2'] = [_world(point, frame) for point in points]
    if row['p1'] == row['p2']:
        raise ValueError('local_baseline transform collapses its length')


def _template_references(template):
    values = []
    footprint = template.get('footprint')
    if isinstance(footprint, dict) and 'primitive' in footprint:
        values.append(footprint['primitive'])
    for space in _rows(template, 'spaces'):
        if 'primitive' in space:
            values.append(space['primitive'])
    for collection in ('doors', 'opening_rows'):
        values.extend(row['local_baseline'] for row in _rows(template, collection)
                      if 'local_baseline' in row)
    return values


def parametric_primitive_frame_context(plan: dict) -> list[dict]:
    """Return declared axes for templates using exactly one primitive frame.

    These are optional regularization metadata, never coordinate transforms for
    existing geometry. Mixed-frame templates are omitted; the caller decides the
    policy and records that boundary. Explicit modelling frames take precedence.
    """
    frames = _frames(plan)
    grouped = {}
    for name, template in _templates(plan).items():
        references = _template_references(template)
        for value in references:
            if not isinstance(value, dict):
                raise ValueError('primitive or local_baseline must be an object')
            _frame(value, frames)
        identities = {value['frame'] for value in references}
        if len(identities) == 1:
            identity = next(iter(identities))
            grouped.setdefault(identity, []).append(name)
    return [dict(id=f'primitive:{identity}', origin=list(frames[identity]['origin']),
                 yaw_degrees=frames[identity]['yaw_degrees'], templates=sorted(names))
            for identity, names in sorted(grouped.items())]


def normalize_parametric_primitives(plan: dict) -> dict:
    """Return an idempotent legacy-schema copy, without primitive declarations.

    All primitive forms require source_polygon_v1. Aperture baseline expansion
    preserves IDs, declared sides, span distances and heights. The common source
    kernel remains responsible for complete real hosts and shared-wall contacts.
    """
    if not isinstance(plan, dict):
        raise ValueError('parametric plan must be an object')
    result = copy.deepcopy(plan)
    frames = _frames(result)
    used = False
    for template in _templates(result).values():
        footprint = template.get('footprint')
        if isinstance(footprint, dict) and 'primitive' in footprint:
            _fields(footprint, ('primitive',), 'primitive footprint')
            template['footprint'] = {'vertices': _primitive(footprint['primitive'], frames)}
            used = True
        for space in _rows(template, 'spaces'):
            if 'primitive' in space:
                if any(key in space for key in ('rect', 'polygon', 'boundary_path')):
                    raise ValueError('space primitive and rect/polygon/boundary_path are mutually exclusive')
                space['polygon'] = _primitive(space.pop('primitive'), frames)
                used = True
        for collection in ('doors', 'opening_rows'):
            for row in _rows(template, collection):
                used |= 'local_baseline' in row
                _baseline(row, frames)
    for collection in ('connections', 'core_connections'):
        for row in _rows(result, collection):
            used |= 'local_baseline' in row
            _baseline(row, frames)
    if used and result.get('schema_version', '2') != 'source_polygon_v1':
        raise ValueError('primitive and local_baseline require source_polygon_v1')
    result.pop('primitive_frames', None)
    return result
