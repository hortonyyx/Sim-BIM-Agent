"""Template expansion must preserve source shapes, apertures and explicit hypotheses."""
import copy
import json

import pytest

from src.agent.geometry.parametric_proposal import expand_parametric_proposal
from src.agent.geometry.source_plan_view import render_source_plan
from src.agent.execution.source_proposal import export_source_proposal


def plan():
    return {
        'templates': {'typical': {
            'footprint': [[0,0],[6,0],[6,2],[3,2],[3,5],[0,5]],
            'spaces': [
                {'id':'hall','role':'corridor','rect':[0,0,3,5],
                 'source_refs':['hypothesis']},
                {'id':'office','role':'office','rect':[3,0,6,2],
                 'source_refs':['hypothesis']}],
            'window_rows': [{'id':'west','facade':'West','plane':0,
                'spans':[[1,2],[3,4]],'z':[0.8,2.2], 'source_refs':['synthetic observation']}],
            'doors': [{'id':'d','space':'hall','other_space':'office',
                'p1':[3,0.4],'p2':[3,1.4],'z':[0,2.1],'source_refs':['hypothesis']}] }},
        'instances':[{'id':'F1','template':'typical','z':0,'height':3},
                     {'id':'F2','template':'typical','z':3,'height':3}],
        'assumptions':['Synthetic inference'], 'unresolved':['Actual interiors unknown']}


def test_l_shaped_repeated_floors_export_and_reload_without_false_floor_coverage(tmp_path):
    original=plan()
    before=copy.deepcopy(original)
    proposal=expand_parametric_proposal(original)
    assert original==before
    assert proposal['geometry']['windows'][2]['z']==[3.8,5.2]
    assert proposal['geometry']['openings'][1]['z']==[3,5.1]
    report=export_source_proposal(proposal,tmp_path/'a')
    assert report['source_geometry_ready'], report
    assert report['counts']['spaces']==4
    assert report['counts']['openings']==6
    source=json.loads((tmp_path/'a/source_model.json').read_text())
    assert all(f['footprint']==original['templates']['typical']['footprint'] for f in source['floors'])
    assert all('spanning_space_ids' not in floor for floor in proposal['geometry']['floors'])
    second=export_source_proposal(json.loads((tmp_path/'a/proposal.json').read_text()),tmp_path/'b')
    assert second['source_model_sha256']==report['source_model_sha256']


def test_whole_aperture_cannot_be_clipped_or_split_across_partition():
    value=plan()
    value['templates']['typical']['window_rows']=[{
        'id':'south','facade':'South','plane':0,'spans':[[2,4]],'z':[1,2],
        'source_refs':['synthetic spanning opening']}]
    with pytest.raises(ValueError,match='0 complete hosts'):
        expand_parametric_proposal(value)


def test_source_reports_real_overlap_without_expansion_repair(tmp_path):
    value=plan()
    value['instances'][1]['z']=2
    report=export_source_proposal(expand_parametric_proposal(value),tmp_path/'bad')
    assert not report['source_geometry_ready']
    assert (tmp_path/'bad/source_model.json').exists()


def test_nonrectangular_single_space_survives_without_extra_physical_dividers(tmp_path):
    value=plan()
    template=value['templates']['typical']
    template['spaces']=[{'id':'open','role':'office',
        'polygon':template['footprint'], 'source_refs':['explicit open-layout hypothesis']}]
    template['doors']=[]
    report=export_source_proposal(expand_parametric_proposal(value),tmp_path/'open')
    assert report['source_geometry_ready'],report
    assert report['counts']['spaces']==2


def test_unknown_row_fields_and_nonfinite_coordinates_are_rejected():
    value=plan()
    value['templates']['typical']['window_rows'][0]['kind']='door'
    with pytest.raises(ValueError,match='unknown fields'):
        expand_parametric_proposal(value)
    value=plan()
    value['instances'][0]['z']=float('nan')
    with pytest.raises(ValueError):
        expand_parametric_proposal(value)


def continuous_core_plan():
    return {
        'templates': {
            'core': {
                'footprint': [[0,0],[2,0],[2,4],[0,4]],
                'spaces': [{'id':'core','role':'stairwell','rect':[0,0,2,4],
                            'source_refs':['synthetic continuous core']}],
            },
            'storey': {
                'footprint': [[0,0],[6,0],[6,4],[0,4]],
                'spaces': [{'id':'room','role':'office','rect':[2,0,6,4],
                            'source_refs':['synthetic local room']}],
            },
        },
        'instances': [
            {'id':'CORE','template':'core','z':0,'height':6},
            {'id':'F1','template':'storey','z':0,'height':3,
             'spanning_space_ids':['CORE:core']},
            {'id':'F2','template':'storey','z':3,'height':3,
             'spanning_space_ids':['CORE:core']},
        ],
        'assumptions':['Synthetic continuous-space test'], 'unresolved':[],
    }


def test_instances_retain_explicit_global_spanning_space_membership_in_source_plan(tmp_path):
    proposal = expand_parametric_proposal(continuous_core_plan())
    floors = {floor['name']: floor for floor in proposal['geometry']['floors']}
    assert floors['F1']['spanning_space_ids'] == ['CORE:core']
    assert floors['F2']['spanning_space_ids'] == ['CORE:core']
    assert 'spanning_space_ids' not in floors['CORE']

    report = export_source_proposal(proposal, tmp_path/'continuous')
    assert report['source_geometry_ready'], report
    assert report['counts']['spaces'] == 3
    source = json.loads((tmp_path/'continuous/source_model.json').read_text())
    assert next(floor for floor in source['floors'] if floor['id'] == 'F2')[
        'spanning_space_ids'] == ['CORE:core']
    _, metadata = render_source_plan(source, 'F2')
    assert set(metadata['space_ids']) == {'F2:room', 'CORE:core'}


@pytest.mark.parametrize(('change', 'message'), [
    ('unknown', 'unknown spanning space CORE:missing'),
    ('noncovering', 'does not cover the whole storey height'),
    ('duplicate', 'duplicate spanning space references'),
])
def test_invalid_spanning_core_references_are_rejected_by_source_validation(tmp_path, change, message):
    value = continuous_core_plan()
    if change == 'unknown':
        value['instances'][1]['spanning_space_ids'] = ['CORE:missing']
    elif change == 'noncovering':
        value['instances'][0]['height'] = 4
    else:
        value['instances'][1]['spanning_space_ids'] = ['CORE:core', 'CORE:core']
    proposal = expand_parametric_proposal(value)
    report = export_source_proposal(proposal, tmp_path/change)
    assert not report['source_geometry_ready']
    assert message in report['error']


def oblique_plan():
    polygon = [[0, 0], [6, 0], [8, 4], [2, 4]]
    return {
        'schema_version': 'source_polygon_v1',
        'templates': {'typical': {
            'footprint': polygon,
            'spaces': [{'id': 'office', 'role': 'office', 'polygon': polygon,
                        'source_refs': ['synthetic exterior; interior inferred']}],
            'opening_rows': [{'id': 'oblique', 'space': 'office', 'kind': 'window',
                              'p1': [6, 0], 'p2': [8, 4], 'spans': [[0.5, 1.5], [2, 3]],
                              'z': [1, 2], 'source_refs': ['synthetic oblique facade']}]}},
        'instances': [{'id': 'F1', 'template': 'typical', 'z': 0, 'height': 3},
                      {'id': 'F2', 'template': 'typical', 'z': 3, 'height': 3}],
        'assumptions': ['Synthetic geometry'], 'unresolved': []}


def test_oblique_templates_keep_hosts_heights_and_individual_openings(tmp_path):
    value = oblique_plan()
    before = copy.deepcopy(value)
    proposal = expand_parametric_proposal(value)
    assert value == before
    assert proposal['geometry']['openings'][2]['z'] == [4, 5]
    report = export_source_proposal(proposal, tmp_path / 'oblique')
    assert report['source_geometry_ready'], report
    source = json.loads((tmp_path / 'oblique/source_model.json').read_text())
    assert len(source['spaces']) == 2
    assert len(source['openings']) == 4
    assert {opening['kind'] for opening in source['openings']} == {'window'}
    assert {opening['id'] for opening in source['openings']} == {
        f'{floor}:row:oblique:{index}' for floor in ['F1', 'F2'] for index in [1, 2]}


def test_parametric_triangle_requires_explicit_source_profile(tmp_path):
    value = oblique_plan()
    template = value['templates']['typical']
    template['footprint'] = template['spaces'][0]['polygon'] = [[0, 0], [6, 0], [3, 4]]
    template['opening_rows'] = []
    proposal = expand_parametric_proposal(value)
    assert export_source_proposal(proposal, tmp_path / 'triangle')['source_geometry_ready']
    del value['schema_version']
    with pytest.raises(ValueError):
        expand_parametric_proposal(value)


@pytest.mark.parametrize('change', ['clipped', 'unknown_host', 'wrong_host', 'wrong_height'])
def test_oblique_apertures_are_not_silently_repaired(tmp_path, change):
    value = oblique_plan()
    row = value['templates']['typical']['opening_rows'][0]
    if change == 'clipped':
        row['spans'] = [[3, 8]]
    elif change == 'unknown_host':
        row['space'] = 'missing'
    elif change == 'wrong_height':
        row['z'] = [2, 4]
    else:
        row['p1'], row['p2'] = [3, 0], [5, 4]
    if change == 'wrong_host':
        report = export_source_proposal(expand_parametric_proposal(value), tmp_path / change)
        assert not report['source_geometry_ready']
    else:
        with pytest.raises(ValueError):
            expand_parametric_proposal(value)


def curved_plan():
    path = [{'curve_id': name} for name in ['arc', 'a', 'b']]
    return {
        'schema_version': 'source_polygon_v1',
        'boundary_curves': {
            'arc': {'kind': 'circular_arc', 'center': [0, 0], 'radius_m': 4,
                    'start_degrees': 0, 'sweep_degrees': 90, 'max_chord_error_m': 0.02,
                    'source_refs': ['synthetic quarter circle']},
            'a': {'kind': 'line', 'p1': [0, 4], 'p2': [0, 0], 'source_refs': ['synthetic']},
            'b': {'kind': 'line', 'p1': [0, 0], 'p2': [4, 0], 'source_refs': ['synthetic']}},
        'templates': {'curved': {
            'footprint': {'boundary_path': path},
            'spaces': [{'id': 'room', 'role': 'office', 'boundary_path': path,
                        'source_refs': ['synthetic curved room']}]}},
        'instances': [{'id': 'L1', 'template': 'curved', 'z': 0, 'height': 3},
                      {'id': 'L2', 'template': 'curved', 'z': 3, 'height': 4}],
        'assumptions': ['Synthetic curve approximation'], 'unresolved': []}


def test_curved_template_uses_one_shared_approximation_and_no_false_rooms(tmp_path):
    value = curved_plan()
    before = copy.deepcopy(value)
    proposal = expand_parametric_proposal(value)
    assert value == before
    first, second = proposal['geometry']['floors']
    assert first['cells'][0]['polygon'] == second['cells'][0]['polygon']
    assert first['cells'][0]['boundary_path'] == second['cells'][0]['boundary_path']
    report = export_source_proposal(proposal, tmp_path / 'curved')
    assert report['source_geometry_ready'], report
    source = json.loads((tmp_path / 'curved/source_model.json').read_text())
    assert len(source['spaces']) == 2
    assert source['boundary_curves']['arc']['max_chord_error_m'] == 0.02
    assert source['curve_discretization']
    assert any(boundary.get('curve_origins') for boundary in source['boundaries'])


def test_parametric_courtyard_is_unfilled_and_not_a_fifth_room(tmp_path):
    value = oblique_plan()
    value['instances'] = value['instances'][:1]
    template = value['templates']['typical']
    template['footprint'] = {'vertices': [[0, 0], [6, 0], [6, 6], [0, 6]],
                             'holes': [[[2, 2], [4, 2], [4, 4], [2, 4]]]}
    template['spaces'] = [{'id': str(i), 'role': 'office', 'rect': rect,
                           'source_refs': ['synthetic separate courtyard room']}
                          for i, rect in enumerate([[0, 0, 6, 2], [0, 2, 2, 4],
                                                    [4, 2, 6, 4], [0, 4, 6, 6]])]
    template['opening_rows'] = []
    report = export_source_proposal(expand_parametric_proposal(value), tmp_path / 'court')
    assert report['source_geometry_ready'], report
    source = json.loads((tmp_path / 'court/source_model.json').read_text())
    assert len(source['spaces']) == 4
    from shapely.geometry import Polygon, Point
    assert not any(Polygon([p[:2] for p in boundary['vertices']]).contains(Point(3, 3))
                   for boundary in source['boundaries'] if boundary['geometry_type'] == 'floor')
