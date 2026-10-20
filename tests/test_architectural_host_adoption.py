"""Architectural anchors translate whole apertures and preserve both hosts."""
from __future__ import annotations

import copy
import math

import pytest
from shapely.geometry import LineString, Polygon

import src.agent.geometry.architectural_regularization as regularization
from src.agent.geometry.architectural_regularization import (
    ArchitecturalRegularizationError,
    DEFAULT_ARCHITECTURAL_POLICY,
    regularize_architectural_plan,
)
from src.agent.geometry.parametric_proposal import expand_parametric_proposal


def _rectangle(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def _space(identity, polygon, role='office'):
    return {'id': identity, 'role': role, 'polygon': polygon,
            'source_refs': ['synthetic host dimensions']}


def _shared(*, reverse_room=False, seam=None):
    left_x, right_x = seam or (3.024, 3.024)
    hall = _rectangle(.024, .024, left_x, 5.036)
    room = _rectangle(right_x, 1.044, 6.024, 4.026)
    if reverse_room:
        room.reverse()
    footprint = [[.024, .024], [left_x, .024], [left_x, 1.044],
                 [6.024, 1.044], [6.024, 4.026], [left_x, 4.026],
                 [left_x, 5.036], [.024, 5.036]]
    return {
        'schema_version': 'source_polygon_v1',
        'templates': {'main': {'footprint': footprint,
            'spaces': [_space('hall', hall, 'corridor'), _space('room', room)],
            'doors': [{'id': 'link', 'space': 'hall', 'other_space': 'room',
                'p1': [(left_x + right_x) / 2, 1.524],
                'p2': [(left_x + right_x) / 2, 2.254],
                'z': [0, 2.1], 'source_refs': ['synthetic 0.73 metre door']}]}},
        'instances': [{'id': 'F1', 'template': 'main', 'z': 0, 'height': 3}],
        'assumptions': ['Synthetic local-layout hypothesis'], 'unresolved': [],
    }


def _adopt(raw):
    before = copy.deepcopy(raw)
    adopted, audit = regularize_architectural_plan(raw, policy=DEFAULT_ARCHITECTURAL_POLICY)
    assert raw == before, 'Adoption must not rewrite the supplied observation.'
    assert audit['status'] == 'adopted'
    return adopted, audit


def _complete_host(template, identity, pair):
    ring = next(space['polygon'] for space in template['spaces'] if space['id'] == identity)
    assert Polygon(ring).boundary.buffer(1e-9).covers(LineString(pair))


@pytest.mark.parametrize('reverse_room', [False, True])
@pytest.mark.parametrize('reverse_door', [False, True])
def test_different_length_shared_hosts_use_one_translation_and_keep_door_width(
        reverse_room, reverse_door):
    raw = _shared(reverse_room=reverse_room)
    if reverse_door:
        door = raw['templates']['main']['doors'][0]
        door['p1'], door['p2'] = door['p2'], door['p1']
    adopted, audit = _adopt(raw)
    template = adopted['templates']['main']
    door = template['doors'][0]
    pair = [door['p1'], door['p2']]
    expected = [[3.0, 1.5], [3.0, 2.23]]
    if reverse_door:
        expected.reverse()
    for point, wanted in zip(pair, expected):
        assert point == pytest.approx(wanted)
    assert math.dist(*pair) == pytest.approx(.73)
    for owner in ('hall', 'room'):
        _complete_host(template, owner, pair)
    assert audit['contact_check']['status'] == 'passed'
    opening = expand_parametric_proposal(adopted)['geometry']['openings'][0]
    assert opening['space_id'] == 'F1:hall' and opening['other_space_id'] == 'F1:room'
    assert math.dist(opening['p1'], opening['p2']) == pytest.approx(.73)
    assert opening['source_refs'] == ['synthetic 0.73 metre door']


def test_cardinal_window_and_opening_row_keep_metric_spans_without_length_scaling():
    raw = _shared()
    template = raw['templates']['main']
    template['doors'] = []  # Isolate row preservation from shared-door conflicts.
    template['window_rows'] = [{'id': 'west', 'facade': 'West', 'plane': .024,
        'spans': [[1.324, 2.054], [3.194, 4.014]], 'z': [.8, 2.2],
        'source_refs': ['synthetic window widths']}]
    template['opening_rows'] = [{'id': 'row', 'space': 'hall', 'kind': 'window',
        'p1': [.024, .524], 'p2': [.024, 4.524],
        'spans': [[.17, .9], [2.03, 2.81]], 'z': [.8, 2.2],
        'source_refs': ['synthetic metre offsets along baseline']}]
    adopted, _ = _adopt(raw)
    template = adopted['templates']['main']
    windows, row = template['window_rows'][0], template['opening_rows'][0]
    assert windows['plane'] == pytest.approx(0)
    for actual, expected in zip(windows['spans'], [[1.3, 2.03], [3.17, 3.99]]):
        assert actual == pytest.approx(expected)
    assert row['spans'] == raw['templates']['main']['opening_rows'][0]['spans']
    assert row['p1'] == pytest.approx([0, .5])
    assert row['p2'] == pytest.approx([0, 4.5])
    assert math.dist(row['p1'], row['p2']) == pytest.approx(4)
    proposal = expand_parametric_proposal(adopted)
    openings = [item for item in proposal['geometry']['openings'] if ':row:' in item['id']]
    assert [math.dist(item['p1'], item['p2']) for item in openings] == pytest.approx([.73, .78])
    for item in openings:
        _complete_host(template, 'hall', [item['p1'], item['p2']])


@pytest.mark.parametrize('connection_kind', ['connections', 'core_connections'])
def test_cross_template_core_and_storeys_share_the_same_aperture_anchor(connection_kind):
    shared = _shared()['templates']['main']
    core_ring = copy.deepcopy(shared['spaces'][0]['polygon'])
    room_ring = copy.deepcopy(shared['spaces'][1]['polygon'])
    raw = {'schema_version': 'source_polygon_v1',
        'templates': {
            'core': {'footprint': core_ring, 'spaces': [_space('core', copy.deepcopy(core_ring), 'stairwell')]},
            'storey': {'footprint': shared['footprint'], 'spaces': [_space('room', room_ring)]}},
        'instances': [{'id': 'CORE', 'template': 'core', 'z': 0, 'height': 6},
            {'id': 'F1', 'template': 'storey', 'z': 0, 'height': 3, 'spanning_space_ids': ['CORE:core']},
            {'id': 'F2', 'template': 'storey', 'z': 3, 'height': 3, 'spanning_space_ids': ['CORE:core']}],
        'assumptions': ['Synthetic spanning core'], 'unresolved': []}
    common = {'id': 'landing', 'p1': [3.024, 1.524], 'p2': [3.024, 2.254],
              'z': [0, 2.1], 'source_refs': ['synthetic service door']}
    if connection_kind == 'core_connections':
        raw[connection_kind] = [{**common, 'core_space_id': 'CORE:core', 'space': 'room',
                                  'instance_ids': ['F1', 'F2']}]
    else:
        raw[connection_kind] = [{**common, 'space_id': 'F1:room', 'other_space_id': 'CORE:core',
                                  'kind': 'door', 'state': 'unknown'}]
    adopted, _ = _adopt(raw)
    connection = adopted[connection_kind][0]
    assert connection['p1'] == pytest.approx([3, 1.5])
    assert connection['p2'] == pytest.approx([3, 2.23])
    pair = [connection['p1'], connection['p2']]
    _complete_host(adopted['templates']['core'], 'core', pair)
    _complete_host(adopted['templates']['storey'], 'room', pair)
    openings = expand_parametric_proposal(adopted)['geometry']['openings']
    assert len(openings) == (2 if connection_kind == 'core_connections' else 1)
    assert all(math.dist(item['p1'], item['p2']) == pytest.approx(.73) for item in openings)
    if connection_kind == 'core_connections':
        assert [item['z'] for item in openings] == [[0, 2.1], [3, 5.1]]


def test_shared_seam_across_rounding_threshold_has_one_normal_and_tangent_anchor():
    raw = _shared(seam=(3.04999, 3.050005))
    adopted, audit = _adopt(raw)
    template = adopted['templates']['main']
    door = template['doors'][0]
    assert door['p1'] == pytest.approx([3, 1.5])
    assert door['p2'] == pytest.approx([3, 2.23])
    for owner in ('hall', 'room'):
        _complete_host(template, owner, [door['p1'], door['p2']])
    assert audit['contact_check']['status'] == 'passed'


def test_real_original_aperture_overrun_is_rejected_even_if_rounding_would_hide_it():
    raw = _shared()
    door = raw['templates']['main']['doors'][0]
    door['p1'], door['p2'] = [3.024, 3.297], [3.024, 4.027]
    before = copy.deepcopy(raw)
    with pytest.raises(ArchitecturalRegularizationError) as error:
        regularize_architectural_plan(raw, policy=DEFAULT_ARCHITECTURAL_POLICY)
    rejection = error.value.audit['rejections'][0]
    assert 'host' in rejection['code']
    assert 'doors[link]' in rejection['path']
    assert raw == before


def test_originally_contained_aperture_cannot_be_clipped_after_host_adoption():
    raw = _shared()
    template = raw['templates']['main']
    # A single exterior host isolates containment from dual-host disagreement.
    template['spaces'] = [template['spaces'][0]]
    template['footprint'] = copy.deepcopy(template['spaces'][0]['polygon'])
    door = raw['templates']['main']['doors'][0]
    door['other_space'] = None
    # The common tangent shift is -0.024 m. This host top adopts 5.0,
    # so its valid original endpoint would land at 5.012, outside the host.
    door['p1'], door['p2'] = [.024, 4.306], [.024, 5.036]
    _complete_host(template, 'hall', [door['p1'], door['p2']])
    before = copy.deepcopy(raw)
    with pytest.raises(ArchitecturalRegularizationError) as error:
        regularize_architectural_plan(raw, policy=DEFAULT_ARCHITECTURAL_POLICY)
    rejection = error.value.audit['rejections'][0]
    assert 'host' in rejection['code']
    assert 'doors[link]' in rejection['path']
    assert raw == before


def test_independent_ring_displacements_are_aggregated_with_local_vertex_evidence(monkeypatch):
    raw = {'schema_version': 'source_polygon_v1', 'templates': {}, 'instances': [],
           'assumptions': ['Two independent synthetic acute outlines'], 'unresolved': [],
           'modelling': {'frames': []}}
    for name, shift in [('first', 0), ('second', 100)]:
        ring = [[shift + .024, .024], [shift + 20.024, .024], [shift + .024, 1.073]]
        raw['templates'][name] = {'footprint': copy.deepcopy(ring), 'spaces': [_space('room', ring)]}
        raw['instances'].append({'id': name.upper(), 'template': name, 'z': 0, 'height': 3})
        raw['modelling']['frames'].append({'id': name, 'origin': [shift, 0],
                                          'yaw_degrees': 0, 'templates': [name]})
    # This separate bad host must not distract from the complete ring-stage
    # findings or execute against half-adopted geometry.
    raw['templates']['first']['doors'] = [{'id': 'later', 'space': 'room', 'other_space': None,
        'p1': [500, 500], 'p2': [500, 501], 'z': [0, 2], 'source_refs': ['deliberately invalid later-stage host']}]
    before = copy.deepcopy(raw)

    def downstream_must_not_run(*args, **kwargs):
        pytest.fail('A rejected ring stage must block downstream adoption/checks.')

    monkeypatch.setattr(regularization, '_vertical', downstream_must_not_run)
    monkeypatch.setattr(regularization, '_protect_volume_contacts', downstream_must_not_run)
    with pytest.raises(ArchitecturalRegularizationError) as error:
        regularize_architectural_plan(raw, policy=DEFAULT_ARCHITECTURAL_POLICY)
    audit = error.value.audit
    displacements = [row for row in audit['rejections'] if row['code'] == 'outline_displacement_exceeded']
    for name in ('first', 'second'):
        assert any(f'templates.{name}.spaces[room].polygon' == row['path'] for row in displacements)
    assert not any('host' in row['code'] for row in audit['rejections'])
    for row in displacements:
        vertex = row['worst_vertex']
        assert set(('index', 'before', 'after', 'displacement_m', 'supports')) <= set(vertex)
        assert vertex['index'] in (0, 1, 2)
        assert len(vertex['supports']) == 2
        assert math.dist(vertex['before'], vertex['after']) == pytest.approx(vertex['displacement_m'])
        assert vertex['displacement_m'] == pytest.approx(row['max_vertex_displacement_m'])
    assert all(row['max_vertex_displacement_m'] > row['limit_m'] == .1 for row in displacements)
    assert audit['observed_max_vertex_displacement_m'] == pytest.approx(
        max(row['max_vertex_displacement_m'] for row in displacements))
    blocked = {row['path'] for row in audit['not_assessed']
               if row.get('blocked_by') == 'ring_adoption'}
    assert {'apertures', 'vertical', 'contacts'} <= blocked
    assert raw == before
