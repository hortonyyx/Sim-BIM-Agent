"""Compare caller-observed plan relationships with actual source space ownership.

This samples source geometry using an explicit image calibration. It neither
interprets pixels nor treats an opening between spaces as a merged space.
"""
from __future__ import annotations

from copy import deepcopy

from shapely.geometry import Point, Polygon

from src.agent.geometry.source_floor_selection import select_source_floor, select_source_floor_openings
from src.agent.geometry.source_image_overlay import _axis_anchors, _number, _source_hash
from src.agent.geometry.source_volume import space_polygon_at_z, space_sections


def revalidate_saved_relation_review(parent, source, saved, images, *, calibration):
    """Recompute the same sampled observations after a notes-only source edit.

    Saved verdicts and derived point ownership are never adopted. Older reports
    retain the exact input pixel/expectation/evidence fields within their rows;
    these can be recovered without inventing another image observation.
    """
    parent_hash = _source_hash(parent)
    _source_hash(source)
    if saved.get('source_model_sha256') != parent_hash:
        raise ValueError('saved space relation review does not belong to the parent source')
    ignored = {'source_model_sha256', 'source_geometry_sha256', 'generation',
               'assumptions', 'corrections'}
    if ({k: v for k, v in parent.items() if k not in ignored}
            != {k: v for k, v in source.items() if k not in ignored}):
        raise ValueError('space relation revalidation requires unchanged physical source')
    image = images.get(saved.get('image'), {})
    if (not image or saved.get('image_sha256') != image.get('sha256')
            or saved.get('image_size', image.get('size')) != image.get('size')):
        raise ValueError('saved space relation original image identity changed')
    if (not isinstance(calibration, dict)
            or calibration.get('image') != saved.get('image')
            or calibration.get('floor_id') != saved.get('floor_id')
            or calibration.get('image_sha256') != image['sha256']):
        raise ValueError('saved space relation calibration identity changed')
    normalized = {f'{axis}_anchors': _axis_anchors(calibration.get(f'{axis}_anchors'),
        axis=axis, size=image['size'][index])[2] for index, axis in enumerate(('x', 'y'))}
    if saved.get('calibration') != normalized:
        raise ValueError('saved space relation calibration changed')
    try:
        recovered = [{'id': row['id'], 'expected': row['expected'], 'evidence': row['evidence'],
                      'points': [point['pixel'] for point in row['points']],
                      **({'z_m': row['z_m']} if 'z_m' in row else {})}
                     for row in saved['observations']]
    except (KeyError, TypeError) as error:
        raise ValueError('saved space relation has no original sampled observations') from error
    submitted = saved.get('submitted_observations', recovered)
    if not isinstance(submitted, list) or len(submitted) != len(recovered):
        raise ValueError('saved space relation submitted observations changed')
    if any(not isinstance(row, dict) or any(row.get(key) != original[key] for key in original)
           for row, original in zip(submitted, recovered)):
        raise ValueError('saved space relation submitted observations changed')
    observations = deepcopy(submitted)
    report = review_space_relations(source, floor_id=saved['floor_id'],
        image_size=image['size'], observations=observations, **normalized)
    return {**report, 'submitted_observations': observations}


def review_space_relations(source, *, floor_id, image_size, x_anchors, y_anchors,
                           observations):
    source_hash = _source_hash(source)
    floor, selected_spaces = select_source_floor(source, floor_id)
    visible_opening_ids = {row['id'] for row in select_source_floor_openings(source, floor, selected_spaces)}
    sx, ox, ax = _axis_anchors(x_anchors, axis='x', size=image_size[0])
    sy, oy, ay = _axis_anchors(y_anchors, axis='y', size=image_size[1])
    if not isinstance(observations, list) or not 1 <= len(observations) <= 32:
        raise ValueError('observations must contain 1 to 32 sampled relationships')
    polygons = [(row['id'], Polygon(row['polygon'], row.get('holes', []))) for row in selected_spaces]
    if any(not polygon.is_valid or polygon.is_empty for _, polygon in polygons):
        raise ValueError('source has invalid space polygons')
    # Numerical boundary ambiguity only, not a new geometry acceptance tolerance.
    epsilon = max(abs(sx), abs(sy)) * 1e-6
    sectioned = any(row.get('vertical_sections') for row in selected_spaces)
    floor_interval = ((float(floor['z_floor']), float(floor['z_floor']) + float(floor['height']))
                      if 'z_floor' in floor and 'height' in floor else None)
    if sectioned and floor_interval is None:
        raise ValueError('sectioned space relations require a floor height interval')
    sample_heights = []
    if sectioned:
        levels = sorted({*floor_interval, *[
            z for row in selected_spaces for section in space_sections(row) for z in section['z']
            if floor_interval[0] < z < floor_interval[1]]})
        sample_heights = [(a + b) / 2 for a, b in zip(levels, levels[1:])]

    def ownership(point, shapes):
        inside = [name for name, polygon in shapes if polygon.contains(point)]
        boundary = [name for name, polygon in shapes
                    if not polygon.is_empty and polygon.boundary.distance(point) <= epsilon]
        return inside, boundary

    def locate(pixel, z_m=None):
        if not isinstance(pixel, list) or len(pixel) != 2:
            raise ValueError('each pixel point must be [x, y]')
        xy = [_number(v, name='pixel coordinate') for v in pixel]
        if any(not 0 <= v < size for v, size in zip(xy, image_size)):
            raise ValueError('point is outside original image bounds')
        world = [sx * xy[0] + ox, sy * xy[1] + oy]
        point = Point(world)
        if z_m is not None or sectioned:
            heights = [z_m] if z_m is not None else sample_heights
            samples = [ownership(point, [(row['id'], space_polygon_at_z(row, z))
                                         for row in selected_spaces]) for z in heights]
            if any(sample != samples[0] for sample in samples[1:]):
                return {'pixel': xy, 'world_xy_m': world, 'status': 'height_dependent',
                        'space_id': None, 'interior_space_ids': sorted({sid for a, _ in samples for sid in a}),
                        'boundary_space_ids': sorted({sid for _, b in samples for sid in b}),
                        'sample_z_m': heights}
            inside, boundary = samples[0]
        else:
            inside, boundary = ownership(point, polygons)
        if boundary:
            status = 'on_boundary'
        elif len(inside) > 1:
            status = 'overlapping_spaces'
        elif not inside:
            status = 'outside_modelled_spaces'
        else:
            status = 'inside_one_space'
        return {'pixel': xy, 'world_xy_m': world, 'status': status,
                'space_id': inside[0] if status == 'inside_one_space' else None,
                'interior_space_ids': inside, 'boundary_space_ids': boundary,
                **({'z_m': z_m} if z_m is not None else {})}

    rows, ids = [], set()
    for observation in observations:
        if not isinstance(observation, dict):
            raise ValueError('each observation must be an object')
        name = observation.get('id')
        if not isinstance(name, str) or not name.strip() or name in ids:
            raise ValueError('observation ids must be nonempty and unique')
        ids.add(name)
        expected = observation.get('expected')
        if expected not in {'same_space', 'separate_spaces', 'uncertain'}:
            raise ValueError('expected must be same_space, separate_spaces, or uncertain')
        evidence = observation.get('evidence')
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError('explain the original-image evidence for each observation')
        points = observation.get('points')
        if not isinstance(points, list) or len(points) != 2:
            raise ValueError('each observation requires exactly two pixel points')
        z_m = _number(observation['z_m'], name='observation z_m') if 'z_m' in observation else None
        if z_m is not None and (floor_interval is None or not floor_interval[0] <= z_m < floor_interval[1]):
            raise ValueError('observation z_m must lie within the requested floor height interval')
        first, second = [locate(point, z_m) for point in points]
        owners = [first['space_id'], second['space_id']]
        actual = ('indeterminate' if None in owners else
                  'same_space' if owners[0] == owners[1] else 'separate_spaces')
        connections = [] if actual != 'separate_spaces' else [
            {key: connection.get(key) for key in ('opening_id', 'space_ids', 'kind', 'state')}
            for connection in source.get('connections', [])
            if set(connection.get('space_ids', [])) == set(owners)
            and connection.get('opening_id') in visible_opening_ids]
        consistency = ('not_assessed' if expected == 'uncertain' or actual == 'indeterminate'
                       else 'consistent_with_supplied_expectation' if actual == expected
                       else 'conflicts_with_supplied_expectation')
        rows.append({'id': name, 'expected': expected, 'evidence': evidence,
                     **({'z_m': z_m} if z_m is not None else {}),
                     'points': [first, second], 'actual_relation': actual,
                     'direct_connections': connections, 'consistency': consistency})
    return {'schema_version': 'source_space_relation_review_v1',
            'source_model_sha256': source_hash, 'floor_id': floor_id,
            'calibration': {'x_anchors': ax, 'y_anchors': ay},
            'calibration_unverified': True, 'drawing_fidelity': 'not_evaluated',
            'coverage': 'caller_selected_point_pairs_only', 'observations': rows,
            **({'height_sampling': 'An omitted z_m is assessed across every constant-footprint height band; varying ownership is indeterminate.'}
               if sectioned else {}),
            'conflict_count': sum(row['consistency'] == 'conflicts_with_supplied_expectation' for row in rows),
            'unassessed_count': sum(row['consistency'] == 'not_assessed' for row in rows),
            'interpretation': 'Different space IDs remain separate even when a door or open passage connects them. '
                              'A conflict is with the supplied observation, not an automatic drawing verdict. '
                              'Recheck original evidence, sample positions and calibration before editing. '
                              'Consistent samples do not establish whole-floor fidelity.'}
