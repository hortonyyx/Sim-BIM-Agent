"""Unambiguous plan field aliases and local format help; no geometry repair."""
from __future__ import annotations

import ast
import copy
import math
import re


def normalize_plan_fields(plan):
    """Only partitions[].pixels and space_seeds[].pixels are aliases.

    Do not guess the meaning of any other pixels field, point nesting or units.
    Both spellings in one object are rejected even if their values look equal.
    """
    if not isinstance(plan, dict):
        return plan, []
    result = copy.deepcopy(plan)
    notes = []
    for collection, field in (('partitions', 'points'), ('space_seeds', 'point')):
        rows = result.get(collection, [])
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or 'pixels' not in row:
                continue
            path = f'plan.{collection}[{index}]'
            if field in row:
                raise ValueError(f'{path}: both pixels and {field} supplied; use only {field}')
            row[field] = row.pop('pixels')
            notes.append(f'{path}.pixels accepted as {path}.{field}; coordinates unchanged.')
    return result, notes


def bind_missing_plan_sources(plan, *, image, image_sha256):
    """Associate required pixel declarations with the tool's verified input.

    This associates a caller declaration with its input, not with an observed
    feature or measured height. Explicit values (including invalid ones) remain
    untouched for ordinary compiler validation. Optional seed references and
    structured measurement/height evidence are not created here.
    """
    if not isinstance(plan, dict):
        return plan, []
    missing = [(collection, index) for collection in ('partitions', 'openings')
               if isinstance(plan.get(collection), list)
               for index, row in enumerate(plan[collection])
               if isinstance(row, dict) and 'source_refs' not in row]
    if not missing:
        return plan, []
    result = copy.deepcopy(plan)
    bindings = []
    for collection, index in missing:
        row = result[collection][index]
        path = f'plan.{collection}[{index}]'
        ref = (f'harness-input:{image}#sha256={image_sha256}; {path}; '
               'caller pixel declaration provenance only; feature observation and height evidence unverified')
        row['source_refs'] = [ref]
        bindings.append({'path': f'{path}.source_refs', 'collection': collection,
                         'object_id': row.get('id'), 'source_refs': [ref]})
    return result, bindings


def _identified_row(plan, collection, identity):
    rows = plan.get(collection, []) if isinstance(plan, dict) else []
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        if isinstance(row, dict) and row.get('id') == identity:
            return index, row
    return None, None


def _numeric_point(value):
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and all(isinstance(item, (int, float)) and not isinstance(item, bool)
                    and math.isfinite(item) for item in value))


def _nearest_endpoint_index(points, failed):
    if not isinstance(points, list) or not _numeric_point(failed):
        return None
    endpoint_indices = list(dict.fromkeys((0, len(points) - 1))) if points else []
    candidates = [(math.dist(points[index], failed), index) for index in endpoint_indices
                  if _numeric_point(points[index])]
    return min(candidates)[1] if candidates else None


def _target_path(plan, identity, point):
    """Map a compiler line name back to the reader's declaration and segment."""
    if identity.startswith('footprint[') and identity.endswith(']'):
        try:
            segment_index = int(identity[len('footprint['):-1])
        except ValueError:
            segment_index = None
        ring = plan.get('footprint_pixels', []) if isinstance(plan, dict) else []
        if segment_index is not None and isinstance(ring, list) and ring:
            return {
                'path': 'plan.footprint_pixels',
                'segment_index': segment_index,
                'segment_original_pixels': [
                    copy.deepcopy(ring[segment_index % len(ring)]),
                    copy.deepcopy(ring[(segment_index + 1) % len(ring)]),
                ],
            }
        return {'path': 'plan.footprint_pixels'}
    index, row = _identified_row(plan, 'partitions', identity)
    if row is None:
        return {'partition_id': identity}
    result = {'path': f'plan.partitions[{index}].points', 'partition_id': identity}
    points = row.get('points')
    candidates = []
    if isinstance(points, list) and _numeric_point(point):
        for segment_index, (first, second) in enumerate(zip(points, points[1:])):
            if not (_numeric_point(first) and _numeric_point(second)):
                continue
            if first[0] == second[0]:
                projected = [first[0], min(max(point[1], min(first[1], second[1])), max(first[1], second[1]))]
            elif first[1] == second[1]:
                projected = [min(max(point[0], min(first[0], second[0])), max(first[0], second[0])), first[1]]
            else:
                continue
            candidates.append((math.dist(point, projected), segment_index, first, second))
    if candidates:
        _, segment_index, first, second = min(candidates, key=lambda row: (row[0], row[1]))
        result.update(segment_index=segment_index,
                      segment_original_pixels=[copy.deepcopy(first), copy.deepcopy(second)])
    return result


def _original_junction_point(partition, endpoint_index, target):
    """Intersect the original source axis with the original target segment."""
    points = partition.get('points') if isinstance(partition, dict) else None
    target_segment = target.get('segment_original_pixels')
    if (not isinstance(points, list) or endpoint_index is None or len(points) < 2
            or not isinstance(target_segment, list) or len(target_segment) != 2):
        return None, 'The original source or target segment is unavailable; move the related endpoints together.'
    source = points[endpoint_index]
    neighbor = points[1] if endpoint_index == 0 else points[-2]
    first, second = target_segment
    if not all(_numeric_point(value) for value in (source, neighbor, first, second)):
        return None, 'The original source or target segment is not numeric; resolve it before copying a coordinate.'
    if source[0] == neighbor[0] and first[1] == second[1]:
        junction = [source[0], first[1]]
        covered = min(first[0], second[0]) <= junction[0] <= max(first[0], second[0])
        move_axis = 1
    elif source[1] == neighbor[1] and first[0] == second[0]:
        junction = [first[0], source[1]]
        covered = min(first[1], second[1]) <= junction[1] <= max(first[1], second[1])
        move_axis = 0
    else:
        return None, 'The original source and target segments are not perpendicular; do not copy the aligned coordinate.'
    if not covered:
        return None, ('The original target segment does not reach the source wall axis; '
                      'the named source and target endpoints must move together.')
    if ((junction[move_axis] - neighbor[move_axis])
            * (source[move_axis] - neighbor[move_axis]) <= 0):
        return None, ('The target lies at or beyond the source wall neighbor; moving only this '
                      'endpoint would collapse or reverse its segment, so related endpoints must move together.')
    return junction, None


def _original_edit_distance_m(plan, source, target):
    """Measure a hint in the original calibration, independently on both axes."""
    if not (_numeric_point(source) and _numeric_point(target)):
        return None
    deltas = []
    for index, axis in enumerate(('x', 'y')):
        anchors = plan.get(f'{axis}_anchors')
        if not (isinstance(anchors, list) and len(anchors) == 2
                and all(_numeric_point(point) and point[0] >= 0 for point in anchors)):
            return None
        (p0, w0), (p1, w1) = anchors
        if p0 == p1 or w0 == w1:
            return None
        deltas.append((target[index] - source[index]) * (w1 - w0) / (p1 - p0))
    distance = math.hypot(*deltas)
    return distance if math.isfinite(distance) else None


def _original_nearest_target(source, target):
    segment = target.get('segment_original_pixels')
    if not (_numeric_point(source) and isinstance(segment, list) and len(segment) == 2
            and all(_numeric_point(point) for point in segment)):
        return None
    first, second = segment
    if first[0] != second[0] and first[1] != second[1]:
        return None
    return [min(max(source[i], min(first[i], second[i])), max(first[i], second[i]))
            for i in range(2)]


def _junction_hint(plan, message):
    marker = 'nearest disconnected endpoints: '
    start = message.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = message.find(';', start)
    if end < 0:
        end = len(message)
    try:
        rows = ast.literal_eval(message[start:end])
    except (SyntaxError, ValueError):
        return None
    repairs = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        partition_id = row.get('partition_id')
        failed = row.get('endpoint_pixel')
        target_id = row.get('nearest_line')
        target_point = row.get('nearest_point_pixel')
        if not (isinstance(partition_id, str) and _numeric_point(failed)
                and isinstance(target_id, str) and _numeric_point(target_point)):
            continue
        index, partition = _identified_row(plan, 'partitions', partition_id)
        endpoint_index = _nearest_endpoint_index(
            partition.get('points') if isinstance(partition, dict) else None, failed)
        original = None
        if endpoint_index is not None:
            original = copy.deepcopy(partition['points'][endpoint_index])
        target = _target_path(plan, target_id, target_point)
        junction, blocked_reason = _original_junction_point(partition, endpoint_index, target)
        target.update(line_id=target_id,
                      reported_point_after_alignment_original_pixels=list(target_point))
        if junction is not None:
            target['point_original_pixels'] = copy.deepcopy(junction)
        distance_m = _original_edit_distance_m(
            plan, original, junction if junction is not None else _original_nearest_target(original, target))
        repair = {
            'path': (f'plan.partitions[{index}].points[{endpoint_index}]'
                     if index is not None and endpoint_index is not None else None),
            'partition_id': partition_id,
            'original_endpoint_pixel': original,
            'failed_endpoint_pixel': list(failed),
            'target': target,
            'distance_pixels': row.get('distance_pixels'),
            'original_distance_m': distance_m,
        }
        # Match the existing strict sub-0.30 m endpoint rule. Wall thickness
        # does not justify retracting a dangling wall towards a distant divider.
        if distance_m is None or distance_m >= .30 - 1e-8:
            repair['blocked_reason'] = (
                'The original gap is uncalibrated or at least 0.30 m. Inspect the original '
                'around this endpoint for a missing or misread wall; add or correct a wall '
                'only when visible. The nearest line is diagnostic, not a relocation target.')
        elif junction is not None:
            repair['set_to_original_pixels'] = copy.deepcopy(junction)
            repair['requires_synchronous_target_endpoint_move'] = False
        else:
            repair['requires_synchronous_target_endpoint_move'] = True
            repair['blocked_reason'] = blocked_reason
        repairs.append(repair)
    if not repairs:
        return None
    return {
        'path': repairs[0]['path'],
        'junction_repairs': repairs,
        'note': ('Copy set_to_original_pixels only when present; it is the intersection of the '
                 'original source wall axis and original target segment, not an aligned-draft value. '
                 'For a flagged local synchronous repair, inspect both named endpoints together. '
                 'For a large or uncalibrated gap, inspect for a missing or misread wall. '
                 'Do not round original coordinates or move to a nearest line merely to compile.'),
    }


def _same_space_hint(plan, message):
    rows = plan.get('space_seeds', []) if isinstance(plan, dict) else []
    indexed = [(index, row) for index, row in enumerate(rows if isinstance(rows, list) else [])
               if isinstance(row, dict) and isinstance(row.get('id'), str)]
    pair = None
    for first_index, first in indexed:
        for second_index, second in indexed:
            if first_index == second_index:
                continue
            if f"space seeds {first['id']} and {second['id']} occupy the same space" in message:
                pair = ((first_index, first), (second_index, second))
                break
        if pair:
            break
    if pair is None:
        return None
    seeds = [{
        'path': f'plan.space_seeds[{index}]',
        'id': row['id'],
        'point_original_pixels': copy.deepcopy(row.get('point')),
    } for index, row in pair]
    points = [row['point_original_pixels'] for row in seeds]
    inspection = {'between_seed_points_original_pixels': copy.deepcopy(points)}
    if all(_numeric_point(point) for point in points):
        inspection.update({
            'midpoint_original_pixels': [
                (points[0][0] + points[1][0]) / 2,
                (points[0][1] + points[1][1]) / 2,
            ],
            'search_box_original_pixels': [
                min(points[0][0], points[1][0]), min(points[0][1], points[1][1]),
                max(points[0][0], points[1][0]), max(points[0][1], points[1][1]),
            ],
        })
    return {
        'path': ' and '.join(row['path'] for row in seeds),
        'space_seeds': seeds,
        'missing_wall_check': inspection,
        'note': ('Inspect the original drawing between these two seed coordinates for an omitted '
                 'physical divider. Add a partition only if that wall is visible; otherwise remove '
                 'or relocate the redundant seed. This hint does not create a wall.'),
    }


def plan_error_hint(plan, message):
    """Offer an actionable original-declaration hint without repairing geometry."""
    examples = {
        'partitions': dict(id='wall-id', points=[[10, 20], [40, 20]], source_refs=['image: observed wall']),
        'space_seeds': dict(id='space-id', point=[20, 30]),
        'openings': dict(id='opening-id', kind='window', p1=[10, 20], p2=[30, 20],
                         z=[1.0, 2.4], source_refs=['image: observed opening']),
    }
    junction = _junction_hint(plan, message)
    if junction is not None:
        return junction
    same_space = _same_space_hint(plan, message)
    if same_space is not None:
        return same_space
    match = re.search(r'plan\.(partitions|space_seeds|openings)\[(\d+)\]', message)
    collection, index = (match[1], int(match[2])) if match else (None, None)
    if collection is None:
        for prefix, key in (('partition ', 'partitions'), ('opening ', 'openings'), ('space seed ', 'space_seeds')):
            if message.startswith(prefix) and isinstance(plan, dict):
                rows = plan.get(key, [])
                if isinstance(rows, list):
                    for i, row in enumerate(rows):
                        if isinstance(row, dict) and str(row.get('id', '')) and re.match(re.escape(prefix + str(row['id'])) + r'(?:[: .]|$)', message):
                            collection, index = key, i
                            break
    if collection:
        example = examples[collection].copy()
        row = None
        if isinstance(plan, dict) and isinstance(plan.get(collection), list) and index < len(plan[collection]):
            row = plan[collection][index]
            if isinstance(row, dict) and isinstance(row.get('id'), str):
                example['id'] = row['id']
                if collection == 'openings' and row.get('kind') in {'window', 'door', 'open', 'passage'}:
                    example['kind'] = row['kind']
        if collection == 'openings' and isinstance(row, dict) and message.startswith(f"opening {row.get('id')}.z "):
            z_floor = plan.get('z_floor')
            height = plan.get('ceiling_height')
            bounds = ([z_floor, z_floor + height]
                      if all(isinstance(value, (int, float)) and not isinstance(value, bool)
                             and math.isfinite(value) for value in (z_floor, height)) else None)
            return dict(path=f'plan.openings[{index}].z', current=copy.deepcopy(row.get('z')),
                        allowed_floor_bounds_m=bounds,
                        note=('Set both absolute opening heights within the floor bounds while preserving the '
                              'observed or stated sill/head relationship; record any assumed height.'))
        if any(text in message for text in ('outside footprint', 'full-boundary hosts', 'full space host',
                                             'only one full space host')):
            note = ('Inspect this declared object and its reported host/footprint relation in the original. '
                    'Keep the opening or wall if it is observed; correct its points rather than deleting it or '
                    'adding an unsupported wall to satisfy compilation.')
        else:
            note = ('Minimum item format only; replace example coordinates and evidence with observations. '
                    'No geometry was adjusted.')
        return dict(path=f'plan.{collection}[{index}]', example=example,
                    note=note)
    if any(word in message for word in ('polygonize', 'overlap', 'outside footprint',
            'full-boundary hosts', 'full space host', 'occupy the same space', 'outside floor vertical bounds')):
        return dict(note='The item shape parsed; inspect the reported geometry in the original. No snapping, host trimming or unit conversion was applied.')
    fields = {
        'x_anchors': [[10, 0.0], [110, 10.0]], 'y_anchors': [[10, 10.0], [110, 0.0]],
        'footprint_pixels': [[10, 10], [110, 10], [110, 110], [10, 110]],
        'z_floor': 0.0, 'ceiling_height': 3.0, 'assumptions': [], 'unresolved': [],
        'floor_id': 'F1', 'basis': 'observed calibration',
    }
    for field, value in fields.items():
        if field in message:
            return dict(path='plan.' + field, example={field: value},
                        note='Format example only; world values use metres, pixel values use original image coordinates.')
    for key, example in examples.items():
        if f'plan.{key}' in message:
            return dict(path='plan.' + key, example={key: [example]})
    # Do not offer a guessed coordinate correction for a topology/host failure.
    if any(word in message for word in ('polygonize', 'overlap', 'outside footprint', 'scale')):
        return dict(note='Keep declared coordinates; inspect the reported objects in the original. No snapping or unit conversion was applied.')
    return dict(path='plan_json', example={'floor_id':'F1','z_floor':0.0,'ceiling_height':3.0,
        'x_anchors':fields['x_anchors'],'y_anchors':fields['y_anchors'],'basis':'observed calibration',
        'footprint_pixels':fields['footprint_pixels'],'partitions':[],'openings':[],
        'assumptions':[],'unresolved':[]}, note='Minimum plan shape; example coordinates are not observations.')
