"""Compare explicitly recorded wall dimensions with saved source coordinates.

The first wall of a chain is the stated reference, not an independently fixed
survey datum. Unknown faces, conflicting branches and missing annotations are
left unassessed. No OCR, nearest-wall matching, geometry changes or GT access.
"""
from __future__ import annotations

from collections import Counter
import math

from src.agent.geometry.wall_reference import convert_wall_dimensions, resolve_wall_references
from src.agent.geometry.dimension_chain import map_dimension_chain


DEFAULT_TOLERANCE_M = 0.02


def annotation_tolerances(calibrations):
    """Bound two picked endpoints at two original pixels each, without tuning to residuals.

    This deliberately misses small offsets in coarse drawings. The 2 cm floor
    handles numerical/representation noise when no pixel calibration is supplied.
    Keep axes/floors separate; another floor must not relax the current check.
    """
    result = {}
    for calibration in calibrations:
        axes = result.setdefault(calibration['floor_id'], {})
        for axis in ('x', 'y'):
            (p0, w0), (p1, w1) = calibration[axis + '_anchors']
            scale = abs((w1-w0)/(p1-p0))
            if not math.isfinite(scale) or not 0 < scale <= .25:
                raise ValueError('annotation calibration outside supported plan scale')
            axes[axis] = max(axes.get(axis, DEFAULT_TOLERANCE_M), 4 * scale)
    return result


def wall_placement_report(source, references, dimensions, *, positions=None,
                          tolerance_m=DEFAULT_TOLERANCE_M, floor_tolerances=None):
    """Use the existing wall_references/wall_dimensions contracts, including faces.

    Connected dimensions propagate from the first explicitly referenced wall.
    This finds accumulated displacement even if successive short spans are each
    within tolerance. Branches, cycles or discontinuous sides use only pairwise
    checks; an endpoint-side mistake is not presented as a misplaced wall.
    """
    if type(tolerance_m) not in (int, float) or not math.isfinite(tolerance_m) or tolerance_m <= 0:
        raise ValueError('wall placement tolerance must be positive finite metres')
    result = dict(schema_version='wall_placement_v1', status='not_assessed',
        tolerance_m=tolerance_m, tolerance_source='tool_default' if tolerance_m == DEFAULT_TOLERANCE_M else 'caller',
        checked_positions=0, items=[], skipped=[], total=0,
        delivery_blocked=False, geometry_changed=False,
        scope='Explicit saved annotation spans relative to their named reference wall; not all walls, drawing truth or an automatic repair.')
    floor_tolerances = floor_tolerances or {}
    for axes in floor_tolerances.values():
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in axes.values()):
            raise ValueError('floor tolerances must be positive finite metres')
    if floor_tolerances:
        result.update(floor_axis_tolerance_m=floor_tolerances,
            tolerance_source='max(2 cm, four original pixels): two endpoint picks at two pixels each')
    def tolerance(floor, axis):
        return max(tolerance_m, floor_tolerances.get(floor, {}).get(axis, tolerance_m))
    boundaries = {b['id']: b for b in source['boundaries']}
    spaces = {s['id']: s for s in source['spaces']}
    if positions is not None and not isinstance(positions, list):
        raise ValueError('positions must be a list')
    seen = set()
    for index, position in enumerate(positions or []):
        fields = {'id','boundary_id','axis','lengths','unit','origin_m','direction','node','offset_m','basis','source_refs'}
        path = f'positions[{index}]'
        if not isinstance(position, dict):
            raise ValueError(f'{path} must be an object')
        if set(position) != fields:
            missing, extra = sorted(fields - position.keys()), sorted(position.keys() - fields)
            details = []
            if missing:
                details.append('missing fields: ' + ', '.join(missing))
            if extra:
                details.append('extra fields: ' + ', '.join(extra))
            raise ValueError(f'{path} (id={position.get("id")!r}): ' + '; '.join(details))
        if not isinstance(position['id'], str) or not position['id'].strip() or position['id'] in seen:
            raise ValueError('position id must be nonempty and unique')
        seen.add(position['id'])
        if position['axis'] not in {'x','y'}:
            raise ValueError('position axis must be x or y')
        if not isinstance(position['basis'], str) or not position['basis'].strip():
            raise ValueError('position basis must describe the annotation reference plane')
        refs = position['source_refs']
        if not isinstance(refs, list) or not refs or any(not isinstance(v,str) or not v.strip() for v in refs):
            raise ValueError('position needs annotation source_refs')
        chain = map_dimension_chain(position['lengths'], unit=position['unit'],
            origin_m=position['origin_m'], direction=position['direction'])
        node = position['node']
        if type(node) is not int or not 0 <= node <= len(chain['segments']):
            raise ValueError('position node must select a cumulative chain endpoint, 0 through length count')
        offset = position['offset_m']
        if type(offset) not in (int,float) or not math.isfinite(offset):
            raise ValueError('position offset_m must be explicit finite metres, never assumed half thickness')
        wall = boundaries.get(position['boundary_id'])
        axis = 'xy'.index(position['axis'])
        if (wall is None or wall['geometry_type'] != 'wall' or wall['kind'] != 'physical'
                or max(v[axis] for v in wall['vertices'])-min(v[axis] for v in wall['vertices']) > 1e-8):
            result['skipped'].append(dict(position_id=position['id'], reason='missing_or_wrong_axis_physical_wall'))
            continue
        expected = (chain['origin_m'] if node == 0 else chain['segments'][node-1]['end_m']) + offset
        actual = wall['vertices'][0][axis]
        fid = spaces[wall['space_id']]['floor_id']
        limit = tolerance(fid, position['axis'])
        result['checked_positions'] += 1
        if abs(actual-expected) > limit + 1e-9:
            result['items'].append(dict(type='annotated_wall_position_mismatch',
                floor_id=fid, wall_id=wall['id'], boundary_ids=[wall['id']],
                axis=position['axis'], expected_coordinate_m=round(expected,9), actual_coordinate_m=round(actual,9),
                deviation_m=round(actual-expected,9), tolerance_m=limit, annotations=[position],
                action='Recheck this wall and the named annotation reference; revise or explain explicitly.'))
    result['total'] = len(result['items'])
    if result['checked_positions']:
        result['status'] = 'reported'
    if not dimensions:
        if not result['checked_positions']:
            result['reason'] = 'No usable structured wall annotations; prose, ink measurements and absent records are not checks.'
        return result
    try:
        walls = resolve_wall_references(source, references)
        converted = convert_wall_dimensions(walls, dimensions)
    except (KeyError, TypeError, ValueError) as error:
        result['skipped'].append(dict(reason='invalid_or_stale_wall_evidence', detail=str(error)))
        return result
    by_id = {w['id']: w for w in walls}
    invalid_ids = {f['dimension_id'] for f in converted['findings']}
    rows = []
    for row in converted['dimensions']:
        if row['id'] in invalid_ids or row['start']['wall_id'] == row['end']['wall_id']:
            result['skipped'].append(dict(dimension_id=row['id'], reason='wall_side_or_thickness_check_not_position'))
        elif row['representative_length_m'] is None:
            result['skipped'].append(dict(dimension_id=row['id'], reason='unknown_wall_face_offsets'))
        elif row['representative_length_m'] <= 0:
            result['skipped'].append(dict(dimension_id=row['id'], reason='nonpositive_representative_span'))
        else:
            rows.append(row)

    def node(row, end):
        endpoint = row[end]
        return (endpoint['wall_id'], endpoint['side'], endpoint['image'], row['axis'], row['direction'])

    outgoing = Counter(node(r, 'start') for r in rows)
    incoming = Counter(node(r, 'end') for r in rows)
    # Continue only through unique joins; no arbitrary choice among branches.
    following = {node(r, 'start'): r for r in rows if outgoing[node(r, 'start')] == 1}
    starts = [r for r in rows if incoming[node(r, 'start')] != 1 or outgoing[node(r, 'start')] != 1]
    visited = set()
    for first in starts + rows:
        if first['id'] in visited:
            continue
        anchor = by_id[first['start']['wall_id']]
        expected = anchor['coordinate_m']
        chain, row = [], first
        propagate = first in starts
        while row['id'] not in visited:
            visited.add(row['id'])
            chain.append(row)
            expected += row['direction'] * row['representative_length_m']
            end = by_id[row['end']['wall_id']]
            delta = end['coordinate_m'] - expected
            limit = tolerance(end['floor_id'], end['axis'])
            result['checked_positions'] += 1
            if abs(delta) > limit + 1e-9:
                result['items'].append(dict(type='annotated_wall_position_mismatch',
                    floor_id=end['floor_id'], wall_id=end['id'], boundary_ids=end['boundary_ids'],
                    anchor_wall_id=anchor['id'], anchor_boundary_ids=anchor['boundary_ids'],
                    axis=end['axis'], expected_coordinate_m=round(expected, 9),
                    actual_coordinate_m=round(end['coordinate_m'], 9), deviation_m=round(delta, 9),
                    tolerance_m=limit,
                    annotations=[{key: r[key] for key in ('id','value','unit','start','end','source_refs')}
                                 for r in chain],
                    action='Recheck named walls, annotation endpoints and reference faces; revise or explain explicitly.'))
            next_node = node(row, 'end')
            if not propagate or incoming[next_node] != 1 or outgoing[next_node] != 1:
                break
            row = following[next_node]
    result['total'] = len(result['items'])
    if result['checked_positions']:
        result['status'] = 'reported'
    return result
