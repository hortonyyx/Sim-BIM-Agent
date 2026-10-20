"""Explicit primitives preserve physical spaces, local axes and real hosts."""
import copy
import json
import math

import pytest
from shapely.geometry import Polygon

from src.agent.execution.source_proposal import export_source_proposal
from src.agent.geometry.parametric_primitives import (
    normalize_parametric_primitives, parametric_primitive_frame_context,
)
from src.agent.geometry.parametric_proposal import expand_parametric_proposal


def primitive(*rects, frame='core'):
    return {'frame': frame, 'rects': [list(rect) for rect in rects]}


def baseline(axis='v', fixed=3, span=(1, 2), frame='core'):
    return {'frame': frame, 'axis': axis, 'fixed': fixed, 'range': list(span)}


def plan(rects=((0, 0, 2.4, 2.2),), yaw=31):
    shape = primitive(*rects)
    return {
        'schema_version': 'source_polygon_v1',
        'primitive_frames': {'core': {'origin': [100.023, -40.017], 'yaw_degrees': yaw}},
        'templates': {'main': {
            'footprint': {'primitive': copy.deepcopy(shape)},
            'spaces': [{'id': 'room', 'role': 'office', 'primitive': shape,
                        'source_refs': ['explicit synthetic space']}]}},
        'instances': [{'id': 'F1', 'template': 'main', 'z': 0, 'height': 3}],
        'assumptions': ['synthetic geometry'], 'unresolved': [],
    }


def shared_plan():
    value = plan(((0, 0, 6, 4),))
    template = value['templates']['main']
    template['spaces'] = [
        {'id': 'hall', 'role': 'corridor', 'primitive': primitive((0, 0, 3, 4)),
         'source_refs': ['synthetic hall']},
        {'id': 'room', 'role': 'office', 'primitive': primitive((3, 0, 6, 4)),
         'source_refs': ['synthetic room']},
    ]
    template['doors'] = [{'id': 'shared', 'space': 'hall', 'other_space': 'room',
        'local_baseline': baseline(), 'z': [0, 2.1], 'source_refs': ['synthetic door']}]
    template['opening_rows'] = [{'id': 'south', 'space': 'hall', 'kind': 'window',
        'local_baseline': baseline('u', 0, (0, 3)), 'spans': [[.5, 1.5]],
        'z': [.8, 2.2], 'source_refs': ['synthetic window']}]
    return value


def test_rotated_rectangle_preserves_right_angles_distinct_dimensions_and_input():
    original = plan()
    before = copy.deepcopy(original)
    normalized = normalize_parametric_primitives(original)
    assert original == before
    assert normalize_parametric_primitives(normalized) == normalized
    assert 'primitive_frames' not in normalized
    assert 'primitive' not in normalized['templates']['main']['spaces'][0]
    ring = normalized['templates']['main']['spaces'][0]['polygon']
    edges = [[b[0]-a[0], b[1]-a[1]] for a, b in zip(ring, ring[1:]+ring[:1])]
    assert [math.hypot(*edge) for edge in edges] == pytest.approx([2.4, 2.2, 2.4, 2.2])
    assert all(abs(sum(a*b for a, b in zip(edge, edges[(i+1) % 4]))) < 1e-10
               for i, edge in enumerate(edges))
    assert expand_parametric_proposal(original) == expand_parametric_proposal(normalized)


def test_rectangle_union_remains_one_space_without_internal_walls_and_is_order_stable(tmp_path):
    value = plan(((0, 0, 4, 2), (0, 2, 2, 4)))
    normalized = normalize_parametric_primitives(value)
    ring = normalized['templates']['main']['spaces'][0]['polygon']
    assert len(ring) == 6
    assert Polygon(ring).area == pytest.approx(12)
    report = export_source_proposal(expand_parametric_proposal(value), tmp_path/'union')
    assert report['source_geometry_ready'], report
    source = json.loads((tmp_path/'union/source_model.json').read_bytes())
    assert len(source['spaces']) == 1
    assert len(source['boundaries']) == 8  # Six exterior walls plus floor and ceiling.
    reordered = copy.deepcopy(value)
    for shape in (reordered['templates']['main']['footprint']['primitive'],
                  reordered['templates']['main']['spaces'][0]['primitive']):
        shape['rects'].reverse()
        shape['rects'].append(list(shape['rects'][0]))
    assert normalize_parametric_primitives(reordered) == normalized


def test_rotated_shared_wall_door_and_window_retain_complete_hosts(tmp_path):
    value = shared_plan()
    before = copy.deepcopy(value)
    proposal = expand_parametric_proposal(value)
    assert value == before
    report = export_source_proposal(proposal, tmp_path/'shared')
    assert report['source_geometry_ready'], report
    source = json.loads((tmp_path/'shared/source_model.json').read_bytes())
    assert len(source['spaces']) == 2
    assert len(source['opening_hosts']['F1:door:shared']) == 2
    assert len(source['opening_hosts']['F1:row:south:1']) == 1
    assert source['connections'] == [{'opening_id': 'F1:door:shared', 'kind': 'door',
        'space_ids': ['F1:hall', 'F1:room'], 'exterior': False, 'state': 'unknown'}]
    opening = next(row for row in source['openings'] if row['id'] == 'F1:row:south:1')
    assert math.dist(opening['vertices'][0], opening['vertices'][1]) == pytest.approx(1)


def core_plan(connection_kind):
    value = plan(((0, 0, 2, 4),))
    value['templates']['core'] = value['templates'].pop('main')
    value['templates']['core']['spaces'][0].update(id='core', role='stairwell')
    value['templates']['storey'] = {
        'footprint': {'primitive': primitive((0, 0, 6, 4))},
        'spaces': [{'id': 'room', 'role': 'office', 'primitive': primitive((2, 0, 6, 4)),
                    'source_refs': ['synthetic storey']}],
    }
    value['instances'] = [{'id': 'CORE', 'template': 'core', 'z': 0, 'height': 6},
        *[{'id': f'F{i+1}', 'template': 'storey', 'z': 3*i, 'height': 3,
           'spanning_space_ids': ['CORE:core']} for i in range(2)]]
    if connection_kind == 'core_connections':
        value[connection_kind] = [{'id': 'landing', 'core_space_id': 'CORE:core', 'space': 'room',
            'instance_ids': ['F1', 'F2'], 'local_baseline': baseline(fixed=2),
            'z': [0, 2.1], 'source_refs': ['synthetic landing']}]
    else:
        value[connection_kind] = [{'id': f'landing{i}', 'space_id': f'F{i+1}:room',
            'other_space_id': 'CORE:core', 'kind': 'door', 'state': 'unknown',
            'local_baseline': baseline(fixed=2), 'z': [3*i, 3*i+2.1],
            'source_refs': ['synthetic landing']} for i in range(2)]
    return value


@pytest.mark.parametrize('collection', ['connections', 'core_connections'])
def test_global_and_repeated_core_baselines_preserve_continuous_core_hosts(tmp_path, collection):
    value = core_plan(collection)
    before = copy.deepcopy(value)
    proposal = expand_parametric_proposal(value)
    report = export_source_proposal(proposal, tmp_path/collection)
    assert report['source_geometry_ready'], report
    assert value == before
    source = json.loads((tmp_path/collection/'source_model.json').read_bytes())
    assert len(source['spaces']) == 3
    assert len(source['connections']) == 2
    assert all(len(hosts) == 2 for hosts in source['opening_hosts'].values())
    assert [row['z'] for row in proposal['geometry']['openings']] == [[0, 2.1], [3, 5.1]]


def test_baseline_on_a_removed_internal_component_seam_is_not_a_wall(tmp_path):
    value = plan(((0, 0, 2, 4), (2, 0, 4, 4)))
    value['templates']['main']['doors'] = [{'id': 'false', 'space': 'room', 'other_space': None,
        'local_baseline': baseline(fixed=2), 'z': [0, 2.1], 'source_refs': ['unsupported seam']}]
    report = export_source_proposal(expand_parametric_proposal(value), tmp_path/'false-wall')
    assert not report['source_geometry_ready']
    source = json.loads((tmp_path/'false-wall/source_model.json').read_bytes())
    assert source['openings'] == []
    assert 'complete source boundary' in source['unbuilt_openings'][0]['reason']


@pytest.mark.parametrize('rects', [
    ((0, 0, 1, 1), (2, 0, 3, 1)),
    ((0, 0, 1, 1), (1, 1, 2, 2)),
    ((0, 0, 3, 1), (0, 2, 3, 3), (0, 1, 1, 2), (2, 1, 3, 2)),
])
def test_union_rejects_disconnected_point_touching_and_holey_shapes(rects):
    with pytest.raises(ValueError, match='one connected positive-area polygon without holes'):
        expand_parametric_proposal(plan(rects))


@pytest.mark.parametrize(('change', 'message'), [
    ('schema', 'require source_polygon_v1'),
    ('unknown_frame', 'unknown primitive frame'),
    ('zero_width', 'positive width and height'),
    ('bool', 'finite numbers'),
    ('nan', 'finite numbers'),
    ('mixed_geometry', 'mutually exclusive'),
    ('footprint_holes', 'primitive footprint requires exactly'),
    ('extra_primitive', 'primitive requires exactly'),
    ('extra_frame', 'requires exactly'),
])
def test_invalid_primitive_declarations_are_rejected_without_repair(change, message):
    value = plan()
    shape = value['templates']['main']['spaces'][0]['primitive']
    if change == 'schema':
        value.pop('schema_version')
    elif change == 'unknown_frame':
        shape['frame'] = 'missing'
    elif change == 'zero_width':
        shape['rects'][0][2] = 0
    elif change == 'bool':
        shape['rects'][0][0] = False
    elif change == 'nan':
        value['primitive_frames']['core']['yaw_degrees'] = float('nan')
    elif change == 'mixed_geometry':
        value['templates']['main']['spaces'][0]['rect'] = [0, 0, 2, 2]
    elif change == 'footprint_holes':
        value['templates']['main']['footprint']['holes'] = []
    elif change == 'extra_primitive':
        shape['scale'] = 1
    elif change == 'extra_frame':
        value['primitive_frames']['core']['scale'] = 1
    with pytest.raises(ValueError, match=message):
        expand_parametric_proposal(value)


@pytest.mark.parametrize(('change', 'message'), [
    ('endpoints', 'mutually exclusive'), ('axis', 'axis must be u or v'),
    ('range', 'positive length'), ('extra', 'requires exactly'),
])
def test_local_baseline_rejects_ambiguous_or_invalid_parameters(change, message):
    value = shared_plan()
    door = value['templates']['main']['doors'][0]
    if change == 'endpoints':
        door['p1'] = [3, 1]
    elif change == 'axis':
        door['local_baseline']['axis'] = 'x'
    elif change == 'range':
        door['local_baseline']['range'] = [2, 1]
    else:
        door['local_baseline']['offset'] = 1
    with pytest.raises(ValueError, match=message):
        expand_parametric_proposal(value)


def test_frame_context_groups_shared_axes_omits_mixed_and_never_rewrites_modelling():
    value = shared_plan()
    value['templates']['repeat'] = copy.deepcopy(value['templates']['main'])
    value['primitive_frames']['other'] = {'origin': [0, 0], 'yaw_degrees': 15}
    value['templates']['mixed'] = copy.deepcopy(value['templates']['main'])
    value['templates']['mixed']['spaces'][0]['primitive']['frame'] = 'other'
    value['modelling'] = {'frames': [{'id': 'manual', 'origin': [0, 0], 'yaw_degrees': 0,
                                     'templates': ['main']}]}
    before = copy.deepcopy(value)
    assert parametric_primitive_frame_context(value) == [{'id': 'primitive:core',
        'origin': [100.023, -40.017], 'yaw_degrees': 31, 'templates': ['main', 'repeat']}]
    assert value == before
    assert normalize_parametric_primitives(value)['modelling'] == value['modelling']


def test_legacy_plan_is_an_unchanged_independent_copy():
    value = {'templates': {'main': {'footprint': [[0, 0], [2, 0], [2, 2], [0, 2]],
        'spaces': [{'id': 'room', 'role': 'office', 'rect': [0, 0, 2, 2], 'source_refs': ['test']}]}},
        'instances': [{'id': 'F1', 'template': 'main', 'z': 0, 'height': 3}],
        'assumptions': [], 'unresolved': []}
    normalized = normalize_parametric_primitives(value)
    assert normalized == value and normalized is not value
    assert expand_parametric_proposal(value) == expand_parametric_proposal(normalized)
