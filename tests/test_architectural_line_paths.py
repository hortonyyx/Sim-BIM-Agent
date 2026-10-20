"""Straight-path adoption preserves shared declarations and physical apertures."""
import copy

import pytest

from src.agent.geometry.architectural_line_paths import (
    expand_straight_paths, restore_straight_paths,
)
from src.agent.geometry.parametric_proposal import expand_parametric_proposal
from src.agent.geometry.source_curves import expand_source_polygon_geometry


class Rejected(ValueError):
    def __init__(self, code, path, message, details):
        super().__init__(message)
        self.code, self.path, self.details = code, path, details


def reject(code, path, message, **details):
    raise Rejected(code, path, message, details)


def plan(*, reverse=False, derived=False):
    points = [[.024, .024], [4.024, .024], [4.024, 3.024], [.024, 3.024]]
    curves = {f'L{i}': {'kind': 'line', 'p1': point,
                       'p2': points[(i+1) % 4], 'source_refs': [f'observation:{i}']}
              for i, point in enumerate(points)}
    path = ([{'curve_id': f'L{i}', 'reverse': True} for i in reversed(range(4))]
            if reverse else [{'curve_id': f'L{i}'} for i in range(4)])
    footprint = {'boundary_path': copy.deepcopy(path)}
    space = {'id': 'room', 'role': 'office', 'source_refs': ['room evidence'],
             'boundary_path': copy.deepcopy(path)}
    if derived:
        footprint['vertices'] = copy.deepcopy(points)
        space['polygon'] = copy.deepcopy(points)
    return {'schema_version': 'source_polygon_v1', 'boundary_curves': curves,
            'templates': {'main': {'footprint': footprint, 'spaces': [space]}},
            'instances': [{'id': 'F1', 'template': 'main', 'z': 0, 'height': 3}],
            'assumptions': ['Synthetic declaration'], 'unresolved': []}


def transform(normalized, function, names=('main',)):
    adopted = copy.deepcopy(normalized)
    for name in names:
        template = adopted['templates'][name]
        template['footprint']['vertices'] = [function(*p) for p in template['footprint']['vertices']]
        for space in template['spaces']:
            space['polygon'] = [function(*p) for p in space['polygon']]
    return adopted


def restore(before, adopted, bindings):
    audit = {'changes': []}
    assert restore_straight_paths(before, adopted, bindings, audit, reject) is adopted
    return audit


@pytest.mark.parametrize('reverse', [False, True])
def test_shared_ids_and_path_direction_survive_translation_and_common_expansion(reverse):
    raw = plan(reverse=reverse)
    pristine = copy.deepcopy(raw)
    before, bindings = expand_straight_paths(raw)
    assert raw == pristine
    assert len(bindings['rings']) == 2
    assert 'boundary_path' not in before['templates']['main']['spaces'][0]
    adopted = transform(before, lambda x, y: [x+.1, y-.2])
    audit = restore(before, adopted, bindings)
    template = adopted['templates']['main']
    assert template['spaces'][0]['boundary_path'] == raw['templates']['main']['spaces'][0]['boundary_path']
    assert 'polygon' not in template['spaces'][0]
    assert set(adopted['boundary_curves']) == set(raw['boundary_curves'])
    for identity, curve in adopted['boundary_curves'].items():
        assert curve['source_refs'] == raw['boundary_curves'][identity]['source_refs']
        for endpoint in ('p1', 'p2'):
            x, y = raw['boundary_curves'][identity][endpoint]
            assert curve[endpoint] == pytest.approx([x+.1, y-.2])
    expanded = expand_parametric_proposal(adopted)
    assert expanded['geometry']['floors'][0]['cells'][0]['id'] == 'F1:room'
    assert len(audit['changes']) == 4
    assert raw == pristine


def test_existing_derived_cell_polygon_is_updated_and_retained():
    before, bindings = expand_straight_paths(plan(derived=True))
    adopted = transform(before, lambda x, y: [x+.1, y])
    restore(before, adopted, bindings)
    template = adopted['templates']['main']
    cell = template['spaces'][0]
    assert cell['polygon'][0] == pytest.approx([.124, .024])
    expanded = expand_source_polygon_geometry({
        'schema_version': 'source_polygon_v1', 'boundary_curves': adopted['boundary_curves'],
        'floors': [{'footprint': template['footprint'], 'cells': template['spaces']}],
    })
    assert expanded['floors'][0]['cells'][0]['x'] == pytest.approx([.124, 4.124])


def test_same_line_used_by_multiple_templates_must_have_one_endpoint_pair():
    raw = plan()
    raw['templates']['other'] = copy.deepcopy(raw['templates']['main'])
    before, bindings = expand_straight_paths(raw)
    adopted = transform(before, lambda x, y: [x+.1, y])
    unchanged = copy.deepcopy(adopted)
    with pytest.raises(Rejected) as caught:
        restore(before, adopted, bindings)
    assert caught.value.code == 'shared_line_endpoint_conflict'
    paths = {use['path'] for use in caught.value.details['uses']}
    assert any('main' in path for path in paths) and any('other' in path for path in paths)
    assert adopted == unchanged


def mixed_plan():
    raw = plan()
    curve = raw['boundary_curves']['L0']
    raw['boundary_curves']['arc'] = {
        'kind': 'circular_arc', 'center': [2.024, .024], 'radius_m': 2,
        'start_degrees': 0, 'sweep_degrees': 180, 'max_chord_error_m': .02,
        'source_refs': ['actual declared arc'],
    }
    path = [{'curve_id': 'L0'}, {'curve_id': 'arc'}]
    raw['templates']['curved'] = {
        'footprint': {'boundary_path': copy.deepcopy(path)},
        'spaces': [{'id': 'curved_room', 'role': 'office', 'source_refs': ['arc room'],
                    'boundary_path': copy.deepcopy(path)}],
    }
    assert curve['p1'] == [.024, .024]
    return raw


def test_line_shared_with_true_arc_template_cannot_move_that_template():
    raw = mixed_plan()
    before, bindings = expand_straight_paths(raw)
    assert before['templates']['curved'] == raw['templates']['curved']
    assert bindings['protected_line_uses']['L0']
    adopted = transform(before, lambda x, y: [x, y+.1])
    with pytest.raises(Rejected) as caught:
        restore(before, adopted, bindings)
    assert caught.value.code == 'line_shared_with_protected_curve'
    assert 'curved' in caught.value.details['protected_paths'][0]
    assert adopted['boundary_curves'] == raw['boundary_curves']


def test_unmoved_shared_line_and_arc_declaration_stay_exactly_unchanged():
    raw = mixed_plan()
    before, bindings = expand_straight_paths(raw)
    adopted = copy.deepcopy(before)
    audit = restore(before, adopted, bindings)
    assert adopted['boundary_curves'] == raw['boundary_curves']
    assert adopted['templates']['curved'] == raw['templates']['curved']
    assert audit['changes'] == []


def test_external_curve_mutation_is_rejected_even_without_eligible_paths():
    raw = mixed_plan()
    del raw['templates']['main']
    before, bindings = expand_straight_paths(raw)
    adopted = copy.deepcopy(before)
    adopted['boundary_curves']['arc']['radius_m'] = 2.1
    with pytest.raises(Rejected) as caught:
        restore(before, adopted, bindings)
    assert caught.value.code == 'immutable_curve_changed'


def test_expansion_refuses_a_stale_derived_polygon():
    raw = plan(derived=True)
    raw['templates']['main']['spaces'][0]['polygon'][1][0] += .1
    with pytest.raises(ValueError, match='disagrees with boundary_path'):
        expand_straight_paths(raw)


def test_rect_cannot_silently_replace_an_explicit_line_path():
    raw = plan()
    raw['templates']['main']['spaces'][0]['rect'] = [0, 0, 8, 6]
    with pytest.raises(ValueError, match='both rect and boundary_path'):
        expand_straight_paths(raw)


def aperture_plan():
    raw = plan()
    raw['connections'] = [{
        'id': 'window', 'space_id': 'F1:room', 'other_space_id': None,
        'p1': [1.024, .024], 'p2': [2.024, .024], 'z': [.5, 2],
        'curve_segment': {'curve_id': 'L0', 'segment_index': 0, 'span_fraction': [.25, .5]},
    }]
    return raw


def adopted_aperture(before):
    adopted = transform(before, lambda x, y: [(x-.024)*1.05, y-.024])
    row = adopted['connections'][0]
    row['p1'], row['p2'] = [1.024, 0], [2.024, 0]
    return adopted


def test_line_fraction_rebinding_preserves_adopted_physical_opening_endpoints():
    before, bindings = expand_straight_paths(aperture_plan())
    adopted = adopted_aperture(before)
    endpoints = copy.deepcopy([adopted['connections'][0][key] for key in ('p1', 'p2')])
    audit = restore(before, adopted, bindings)
    row = adopted['connections'][0]
    assert [row['p1'], row['p2']] == endpoints
    assert row['curve_segment']['span_fraction'] == pytest.approx([1.024/4.2, 2.024/4.2])
    assert any(change['type'] == 'line_aperture_binding' for change in audit['changes'])
    expanded = expand_source_polygon_geometry({
        'schema_version': 'source_polygon_v1', 'boundary_curves': adopted['boundary_curves'],
        'openings': [row],
    })
    assert expanded['openings'][0]['p1'] == pytest.approx(endpoints[0])
    assert expanded['openings'][0]['p2'] == pytest.approx(endpoints[1])


@pytest.mark.parametrize('problem,code', [
    ('old_fraction', 'stale_line_aperture_binding'),
    ('outside', 'line_aperture_outside_host'),
    ('off_line', 'line_aperture_off_host'),
])
def test_line_rebinding_does_not_hide_original_mismatch_or_new_host_errors(problem, code):
    raw = aperture_plan()
    if problem == 'old_fraction':
        raw['connections'][0]['curve_segment']['span_fraction'] = [.2, .5]
    before, bindings = expand_straight_paths(raw)
    adopted = adopted_aperture(before)
    if problem == 'outside':
        adopted['connections'][0]['p2'][0] = 4.3
    elif problem == 'off_line':
        adopted['connections'][0]['p2'][1] = .05
    unchanged = copy.deepcopy(adopted)
    with pytest.raises(Rejected) as caught:
        restore(before, adopted, bindings)
    assert caught.value.code == code
    assert adopted == unchanged
