"""Independent source-save and Toolkit checks across geometry representations."""
import copy
import hashlib
import json
import math

import pytest

from scripts.tool_scripts.bim_agent_architectural import validate_parametric_regularization
from scripts.tool_scripts.bim_agent_parametric import build_parametric, inspect_parametric
from scripts.tool_scripts.run_bim_agent import Toolkit
from src.agent.execution.source_proposal import export_source_proposal
from src.agent.geometry.architectural_regularization import (
    ArchitecturalRegularizationError, DEFAULT_ARCHITECTURAL_POLICY, regularize_architectural_plan,
    regularize_architectural_proposal,
)
from src.agent.geometry.parametric_proposal import expand_parametric_proposal
from src.agent.geometry.source_curves import expand_source_polygon_geometry
from tests.test_bim_agent_mesh import _prepare


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _line_plan():
    points = {'a': [.024, .024], 'b': [3.024, .024], 'c': [6.024, .024],
              'd': [6.024, 4.024], 'e': [3.024, 4.024], 'f': [.024, 4.024]}
    edges = {'south_hall': ('a', 'b'), 'south_room': ('b', 'c'), 'east': ('c', 'd'),
             'north_room': ('d', 'e'), 'north_hall': ('e', 'f'), 'west': ('f', 'a'),
             'shared': ('b', 'e')}
    curves = {name: {'kind': 'line', 'p1': points[start], 'p2': points[end],
                     'source_refs': [f'synthetic measured line {name}']}
              for name, (start, end) in edges.items()}
    def path(*names):
        return [{'curve_id': name.lstrip('-'), **({'reverse': True} if name.startswith('-') else {})}
                for name in names]
    return {'schema_version': 'source_polygon_v1', 'boundary_curves': curves,
        'templates': {'floor': {'footprint': {'boundary_path': path(
            'south_hall', 'south_room', 'east', 'north_room', 'north_hall', 'west')},
            'spaces': [
                {'id': 'hall', 'role': 'corridor', 'source_refs': ['synthetic hall'],
                 'boundary_path': path('south_hall', 'shared', 'north_hall', 'west')},
                {'id': 'room', 'role': 'office', 'source_refs': ['synthetic room'],
                 'boundary_path': path('south_room', 'east', 'north_room', '-shared')},
            ]}},
        'instances': [{'id': 'F1', 'template': 'floor', 'z': 0, 'height': 3}],
        'connections': [{'id': 'door', 'space_id': 'F1:hall', 'other_space_id': 'F1:room',
            'p1': [3.024, 1.024], 'p2': [3.024, 2.024], 'z': [0, 2.1],
            'kind': 'door', 'state': 'unknown', 'source_refs': ['synthetic door'],
            'curve_segment': {'curve_id': 'shared', 'segment_index': 0,
                              'span_fraction': [.25, .5]}}],
        'assumptions': ['synthetic representation integration'], 'unresolved': []}


def _primitive_plan():
    shape = {'frame': 'wing', 'rects': [[.024, .024, 4.038, 2.038]]}
    return {'schema_version': 'source_polygon_v1',
        'primitive_frames': {'wing': {'origin': [100.023, -40.017], 'yaw_degrees': 31}},
        'templates': {'office': {'footprint': {'primitive': copy.deepcopy(shape)},
            'spaces': [{'id': 'room', 'role': 'office', 'primitive': shape,
                        'source_refs': ['synthetic explicit rectangular room']}]}},
        'instances': [{'id': 'F1', 'template': 'office', 'z': 0, 'height': 3}],
        'assumptions': ['synthetic primitive integration'], 'unresolved': []}


def _assert_curve_opening(geometry):
    opening = geometry['openings'][0]
    binding = opening['curve_segment']
    assert binding['curve_id'] == 'shared' and binding['segment_index'] == 0
    curve = geometry['boundary_curves']['shared']
    for endpoint, fraction in zip(('p1', 'p2'), binding['span_fraction']):
        expected = [a+(b-a)*fraction for a, b in zip(curve['p1'], curve['p2'])]
        assert opening[endpoint] == pytest.approx(expected, abs=1e-9)


def _export_checked(proposal, folder):
    report = export_source_proposal(proposal, folder)
    assert report['source_geometry_ready'], report
    source = json.loads((folder/'source_model.json').read_bytes())
    assert source['validation']['status'] == 'pass'
    return source


def test_shared_pure_line_paths_undergo_adoption_and_export_with_one_real_door(tmp_path):
    raw = _line_plan()
    before = copy.deepcopy(raw)
    adopted, audit = regularize_architectural_plan(raw, policy=DEFAULT_ARCHITECTURAL_POLICY)
    assert raw == before
    assert audit['input_sha256'] == _hash(raw)
    assert audit['adopted_sha256'] == _hash(adopted)
    assert audit['not_assessed'] == []
    assert audit['contact_check']['straight_prism_count'] == 2
    assert set(adopted['boundary_curves']) == set(raw['boundary_curves'])
    for name, curve in adopted['boundary_curves'].items():
        assert curve['source_refs'] == raw['boundary_curves'][name]['source_refs']
        assert all(abs(value*10-round(value*10)) < 1e-8
                   for point in (curve['p1'], curve['p2']) for value in point)
    proposal = expand_parametric_proposal(adopted)
    _assert_curve_opening(proposal['geometry'])
    source = _export_checked(proposal, tmp_path/'line')
    assert len(source['spaces']) == 2
    assert len(source['opening_hosts']['door']) == 2
    assert len(source['connections']) == 1
    assert source['connections'][0]['space_ids'] == ['F1:hall', 'F1:room']


def test_generic_derived_line_geometry_and_curve_segment_are_rebound_together(tmp_path):
    raw = expand_parametric_proposal(_line_plan())
    # Save exactly the enriched geometry consumed by source/generic revision.
    raw['geometry'] = expand_source_polygon_geometry(raw['geometry'])
    before = copy.deepcopy(raw)
    adopted, audit = regularize_architectural_proposal(raw, policy=DEFAULT_ARCHITECTURAL_POLICY)
    assert raw == before
    assert audit['not_assessed'] == []
    _assert_curve_opening(adopted['geometry'])
    reexpanded = expand_source_polygon_geometry(adopted['geometry'])
    assert 'curve_discretization' not in adopted['geometry']
    rebuilt_discretization = reexpanded.pop('curve_discretization')
    assert reexpanded == adopted['geometry']
    assert rebuilt_discretization['shared']['points'] == [[3, 0], [3, 4]]
    source = _export_checked(adopted, tmp_path/'generic-line')
    assert len(source['opening_hosts']['door']) == 2
    assert adopted['geometry']['boundary_curves']['shared']['p1'] == [3, 0]
    assert adopted['geometry']['boundary_curves']['shared']['p2'] == [3, 4]


def test_generic_path_only_cells_refresh_derived_polygons_and_bounds_after_adoption(tmp_path):
    raw = expand_parametric_proposal(_line_plan())
    raw['geometry'] = expand_source_polygon_geometry(raw['geometry'])
    for floor in raw['geometry']['floors']:
        for cell in floor['cells']:
            del cell['polygon']
            assert 'boundary_path' in cell and 'x' in cell and 'y' in cell
    # This is a valid public representation before adoption, not a stale input.
    expand_source_polygon_geometry(raw['geometry'])
    before = copy.deepcopy(raw)
    adopted, _ = regularize_architectural_proposal(raw, policy=DEFAULT_ARCHITECTURAL_POLICY)
    assert raw == before
    expanded = expand_source_polygon_geometry(adopted['geometry'])
    for cell in expanded['floors'][0]['cells']:
        assert cell['y'] == [0, 4]
        assert cell['x'] == ([0, 3] if cell['id'] == 'F1:hall' else [3, 6])
        assert adopted['geometry']['floors'][0]['cells'][
            0 if cell['id'] == 'F1:hall' else 1]['polygon'] == cell['polygon']
    _assert_curve_opening(expanded)
    source = _export_checked(adopted, tmp_path/'generic-path-only')
    assert len(source['opening_hosts']['door']) == 2


def test_primitive_uses_declared_local_axis_through_adoption_and_source_save(tmp_path):
    raw = _primitive_plan()
    before = copy.deepcopy(raw)
    adopted, audit = regularize_architectural_plan(raw, policy=DEFAULT_ARCHITECTURAL_POLICY)
    assert raw == before
    assert audit['frames']['office'] == {'id': 'primitive:wing',
        'origin': [100.023, -40.017], 'yaw_degrees': 31}
    assert audit['input_sha256'] == _hash(raw)
    ring = adopted['templates']['office']['spaces'][0]['polygon']
    assert ring[0] == pytest.approx([100.023, -40.017])
    assert math.dist(ring[0], ring[1]) == pytest.approx(4)
    assert math.dist(ring[1], ring[2]) == pytest.approx(2)
    assert any(abs(value*10-round(value*10)) > .01 for point in ring for value in point)
    source = _export_checked(expand_parametric_proposal(adopted), tmp_path/'primitive')
    assert len(source['spaces']) == 1


def _arc_plan():
    path = [{'curve_id': 'diameter'}, {'curve_id': 'arc'}]
    return {'schema_version': 'source_polygon_v1', 'boundary_curves': {
        'diameter': {'kind': 'line', 'p1': [-2.024, .024], 'p2': [2.024, .024],
                     'source_refs': ['synthetic shared diameter']},
        'arc': {'kind': 'circular_arc', 'center': [0, .024], 'radius_m': 2.024,
                'start_degrees': 0, 'sweep_degrees': 180, 'max_chord_error_m': .02,
                'source_refs': ['synthetic explicit arc']}},
        'templates': {'curved': {'footprint': {'boundary_path': copy.deepcopy(path)},
            'spaces': [{'id': 'curved', 'role': 'office', 'boundary_path': path,
                        'source_refs': ['synthetic curved room']}]}},
        'instances': [{'id': 'ARC', 'template': 'curved', 'z': 0, 'height': 3}],
        'assumptions': [], 'unresolved': []}


def test_true_arc_is_preserved_and_explicitly_unassessed_through_source_save(tmp_path):
    raw = _arc_plan()
    adopted, audit = regularize_architectural_plan(raw, policy=DEFAULT_ARCHITECTURAL_POLICY)
    assert adopted['boundary_curves'] == raw['boundary_curves']
    assert audit['contact_check']['straight_prism_count'] == 0
    assert any(row['path'] == 'templates.curved' and 'curve XY' in row['scope']
               for row in audit['not_assessed'])
    source = _export_checked(expand_parametric_proposal(adopted), tmp_path/'actual-arc')
    assert len(source['spaces']) == 1
    assert source['curve_discretization']['arc']['kind'] == 'circular_arc'
    assert source['curve_discretization']['arc']['segment_count'] > 1


def test_all_line_template_cannot_move_a_diameter_shared_with_a_true_arc():
    raw = _arc_plan()
    raw['boundary_curves'].update({
        'right': {'kind': 'line', 'p1': [2.024, .024], 'p2': [2.024, -3.024],
                  'source_refs': ['synthetic rectangle']},
        'bottom': {'kind': 'line', 'p1': [2.024, -3.024], 'p2': [-2.024, -3.024],
                   'source_refs': ['synthetic rectangle']},
        'left': {'kind': 'line', 'p1': [-2.024, -3.024], 'p2': [-2.024, .024],
                 'source_refs': ['synthetic rectangle']},
    })
    path = [{'curve_id': name} for name in ('diameter', 'right', 'bottom', 'left')]
    raw['templates']['straight'] = {'footprint': {'boundary_path': copy.deepcopy(path)},
        'spaces': [{'id': 'room', 'role': 'office', 'boundary_path': path,
                    'source_refs': ['synthetic room below arc']}]}
    raw['instances'].append({'id': 'RECT', 'template': 'straight', 'z': 0, 'height': 3})
    before = copy.deepcopy(raw)
    with pytest.raises(ArchitecturalRegularizationError) as caught:
        regularize_architectural_plan(raw, policy=DEFAULT_ARCHITECTURAL_POLICY)
    assert raw == before
    assert any(row['code'] == 'line_shared_with_protected_curve'
               for row in caught.value.audit['rejections'])


def _assert_reference_files(run, reference):
    for key in ('adopted', 'audit'):
        data = (run/reference[key]['file']).read_bytes()
        assert hashlib.sha256(data).hexdigest() == reference[key]['sha256']
    adopted = json.loads((run/reference['adopted']['file']).read_bytes())
    assert _hash(adopted) == reference['adopted_sha256']
    return adopted


def _assert_same_floor_geometry(actual, expected):
    """Reintersection may round coordinates at 1e-12 m; IDs/structure stay exact."""
    checked = copy.deepcopy(actual)
    assert len(actual) == len(expected)
    for floor, original in zip(checked, expected):
        assert len(floor['footprint']['vertices']) == len(original['footprint']['vertices'])
        for point, before in zip(floor['footprint']['vertices'], original['footprint']['vertices']):
            assert point == pytest.approx(before, abs=1e-9, rel=0)
        floor['footprint']['vertices'] = original['footprint']['vertices']
        assert len(floor['cells']) == len(original['cells'])
        for cell, old_cell in zip(floor['cells'], original['cells']):
            assert len(cell['polygon']) == len(old_cell['polygon'])
            for point, before in zip(cell['polygon'], old_cell['polygon']):
                assert point == pytest.approx(before, abs=1e-9, rel=0)
            cell['polygon'] = old_cell['polygon']
            for axis in ('x', 'y'):
                assert cell[axis] == pytest.approx(old_cell[axis], abs=1e-9, rel=0)
                cell[axis] = old_cell[axis]
    assert checked == expected


def test_toolkit_primitive_parametric_to_generic_revisions_keep_axis_and_hash_bindings(tmp_path, monkeypatch):
    _, run, _ = _prepare(tmp_path, monkeypatch)
    toolkit = Toolkit(run)
    raw = _primitive_plan()
    first = build_parametric(toolkit, json.dumps(raw))
    assert first['source_geometry_ready'], first
    reference = first['architectural_regularization']
    assert reference['original_sha256'] == _hash(raw)
    assert reference['modelling']['frames'] == [{'id': 'primitive:wing',
        'origin': [100.023, -40.017], 'yaw_degrees': 31, 'templates': ['office']}]
    adopted = _assert_reference_files(run, reference)
    assert reference['proposal_sha256'] == _hash(expand_parametric_proposal(adopted))
    assert json.loads((run/first['candidate']/'parametric_plan.json').read_bytes()) == raw
    assert inspect_parametric(toolkit, candidate=first['candidate'])['exact_plan']
    baseline = json.loads((run/first['candidate']/'proposal.json').read_bytes())
    prior = first
    for index in range(2):
        revised = toolkit.revise(prior['candidate'], json.dumps([
            {'op': 'set_notes', 'assumptions': [f'synthetic revision {index}'], 'unresolved': []}]))
        assert revised['source_geometry_ready'], revised
        reference = revised['architectural_regularization']
        assert reference['frame_context_parent'] == prior['candidate']
        assert reference['modelling']['frames'] == [{'id': 'primitive:wing',
            'origin': [100.023, -40.017], 'yaw_degrees': 31, 'templates': ['F1']}]
        proposal = json.loads((run/revised['candidate']/'proposal.json').read_bytes())
        _assert_same_floor_geometry(proposal['geometry']['floors'], baseline['geometry']['floors'])
        assert reference['proposal_sha256'] == _hash(proposal)
        assert _assert_reference_files(run, reference) == proposal
        original = json.loads((run/reference['original']['file']).read_bytes())
        assert reference['original_sha256'] == _hash(original)
        source = json.loads((run/revised['candidate']/'source_model.json').read_bytes())
        assert source['validation']['status'] == 'pass'
        assert source['generation']['provenance']['architectural_regularization'] == reference
        prior = revised


@pytest.mark.parametrize('artifact', ['original', 'adopted', 'audit', 'proposal'])
def test_toolkit_parametric_binding_rejects_tampered_declaration_or_adoption(tmp_path, monkeypatch, artifact):
    _, run, _ = _prepare(tmp_path, monkeypatch)
    toolkit = Toolkit(run)
    result = build_parametric(toolkit, json.dumps(_primitive_plan()))
    assert result['source_geometry_ready'], result
    reference = result['architectural_regularization']
    path = (run/result['candidate']/('parametric_plan.json' if artifact == 'original' else 'proposal.json')
            if artifact in ('original', 'proposal') else run/reference[artifact]['file'])
    path.write_text('{}\n', encoding='utf-8')
    with pytest.raises(ValueError, match='changed|adoption'):
        validate_parametric_regularization(toolkit, result)
