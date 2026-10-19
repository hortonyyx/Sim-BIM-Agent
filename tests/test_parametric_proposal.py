"""Template expansion must preserve source shapes, apertures and explicit hypotheses."""
import copy
import json

import pytest

from src.agent.geometry.parametric_proposal import expand_parametric_instances, expand_parametric_proposal
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


def repeated_plan():
    value = plan()
    value['instances'] = []
    value['repeats'] = [{'id': 'offices', 'template': 'typical', 'id_prefix': 'F',
                         'start': 9, 'count': 2, 'id_padding': 2, 'z': 0.3, 'height': 3,
                         'source_refs': ['explicit repeated-layout hypothesis'],
                         'assumptions': ['These two storeys are assumed identical']}]
    return value


def test_repeat_is_exact_explicit_expansion_with_stable_ids_and_evidence(tmp_path):
    compact = repeated_plan()
    before = copy.deepcopy(compact)
    instances = expand_parametric_instances(compact)
    assert [(row['id'], row['z']) for row in instances] == [('F09', 0.3), ('F10', 3.3)]
    expanded = {**compact, 'instances': instances}
    del expanded['repeats']
    proposal = expand_parametric_proposal(compact)
    assert proposal == expand_parametric_proposal(expanded)
    assert compact == before
    assert proposal['geometry']['windows'][2]['z'] == [4.1, 5.5]
    for row in [*proposal['geometry']['floors'][0]['cells'],
                *proposal['geometry']['windows'], *proposal['geometry']['openings']]:
        assert 'explicit repeated-layout hypothesis' in row['source_refs']
        assert 'These two storeys are assumed identical' in row['assumptions']
    compact['repeats'][0]['count'] = 3
    extended = expand_parametric_proposal(compact)
    assert extended['geometry']['floors'][:2] == proposal['geometry']['floors']
    assert extended['geometry']['windows'][:4] == proposal['geometry']['windows']
    report = export_source_proposal(proposal, tmp_path / 'repeat')
    assert report['source_geometry_ready'], report


def test_repeat_exception_requires_explicit_exclusion_and_keeps_sequence_heights():
    value = repeated_plan()
    value['repeats'][0].update(count=3, exclude_ids=['F10'])
    value['instances'] = [{'id': 'F10', 'template': 'typical', 'z': 3.3, 'height': 3,
                           'source_refs': ['explicit exception layout']}]
    instances = {row['id']: row for row in expand_parametric_instances(value)}
    assert instances['F11']['z'] == 6.3
    assert instances['F10']['source_refs'] == ['explicit exception layout']
    assert 'spanning_space_ids' not in instances['F10']
    del value['repeats'][0]['exclude_ids']
    with pytest.raises(ValueError, match='duplicate instance F10'):
        expand_parametric_proposal(value)


@pytest.mark.parametrize('change', ['missing_exception', 'foreign_exclusion', 'duplicate_exclusion',
                                  'duplicate_group', 'duplicate_explicit', 'count_zero',
                                  'count_bool', 'count_limit', 'height_zero', 'height_expression'])
def test_repeat_refuses_ambiguous_exceptions_and_invalid_ranges(change):
    value = repeated_plan()
    repeat = value['repeats'][0]
    if change == 'missing_exception':
        repeat['exclude_ids'] = ['F10']
    elif change == 'foreign_exclusion':
        repeat['exclude_ids'] = ['F99']
    elif change == 'duplicate_exclusion':
        repeat['exclude_ids'] = ['F10', 'F10']
    elif change == 'duplicate_group':
        value['repeats'].append(copy.deepcopy(repeat))
    elif change == 'duplicate_explicit':
        value['instances'] = [{'id': 'X', 'template': 'typical', 'z': 20, 'height': 3}] * 2
    elif change == 'count_zero':
        repeat['count'] = 0
    elif change == 'count_bool':
        repeat['count'] = True
    elif change == 'count_limit':
        repeat['count'] = 101
    elif change == 'height_zero':
        repeat['height'] = 0
    else:
        repeat['height'] = '3+0.2'
    with pytest.raises((ValueError, TypeError)):
        expand_parametric_proposal(value)


def repeated_core_plan():
    value = continuous_core_plan()
    value['templates']['core']['spaces'][0]['role'] = 'shaft'
    value['instances'] = [value['instances'][0],
        {'id': 'EQUIPMENT', 'template': 'storey', 'z': 9, 'height': 3,
         'spanning_space_ids': ['CORE:core']}]
    value['instances'][0]['height'] = 12
    value['repeats'] = [{'id': 'offices', 'template': 'storey', 'id_prefix': 'F',
        'start': 1, 'count': 3, 'id_padding': 2, 'z': 0, 'height': 3,
        'spanning_space_ids': ['CORE:core'], 'source_refs': ['three explicitly repeated storeys']}]
    value['core_connections'] = [{'id': 'lift', 'core_space_id': 'CORE:core', 'space': 'room',
        'repeat_ids': ['offices'], 'p1': [2, 1], 'p2': [2, 2], 'z': [0, 2.1],
        'source_refs': ['declared service and repeated landing layout'],
        'assumptions': ['landing position and size inferred']}]
    return value


def test_core_service_expansion_is_explicit_complete_hosted_and_avoids_equipment(tmp_path):
    value = repeated_core_plan()
    before = copy.deepcopy(value)
    proposal = expand_parametric_proposal(value)
    assert value == before
    expected_ids = {f'F{i:02d}:core:lift' for i in range(1, 4)}
    assert {row['id'] for row in proposal['geometry']['openings']} == expected_ids
    assert [row['z'] for row in proposal['geometry']['openings']] == [[0, 2.1], [3, 5.1], [6, 8.1]]
    report = export_source_proposal(proposal, tmp_path / 'served')
    assert report['source_geometry_ready'], report
    source = json.loads((tmp_path / 'served/source_model.json').read_text())
    assert len(source['connections']) == 3
    assert not any('EQUIPMENT' in row['id'] for row in source['openings'])
    for row in source['openings']:
        fid = row['id'].split(':', 1)[0]
        assert set(row['space_ids']) == {f'{fid}:room', 'CORE:core'}
        assert len(source['opening_hosts'][row['id']]) == 2
        hosts = {boundary['id']: boundary for boundary in source['boundaries']}
        assert {hosts[bid]['space_id'] for bid in source['opening_hosts'][row['id']]} == set(row['space_ids'])
    core = next(space for space in source['spaces'] if space['id'] == 'CORE:core')
    assert core['height'] == 12
    assert len([row for row in source['boundaries'] if row['space_id'] == core['id']
                and row['geometry_type'] == 'floor']) == 1


def test_exception_is_not_implicitly_served_and_can_declare_a_different_landing():
    value = repeated_core_plan()
    value['repeats'][0]['exclude_ids'] = ['F02']
    value['instances'].append({'id': 'F02', 'template': 'storey', 'z': 3, 'height': 3,
                              'spanning_space_ids': ['CORE:core']})
    proposal = expand_parametric_proposal(value)
    assert {row['id'] for row in proposal['geometry']['openings']} == {'F01:core:lift', 'F03:core:lift'}
    special = copy.deepcopy(value['core_connections'][0])
    special.update(id='lift_exception', instance_ids=['F02'], p1=[2, 2.5], p2=[2, 3.5])
    del special['repeat_ids']
    value['core_connections'].append(special)
    revised = expand_parametric_proposal(value)
    assert revised['geometry']['openings'][:2] == proposal['geometry']['openings']
    assert revised['geometry']['openings'][2]['id'] == 'F02:core:lift_exception'
    assert revised['geometry']['openings'][2]['z'] == [3, 5.1]


@pytest.mark.parametrize('change', ['unknown_floor', 'unknown_repeat', 'duplicate_floor',
                                  'duplicate_repeat', 'overlapping_selection', 'missing_scope',
                                  'unknown_core', 'unknown_local', 'missing_member', 'short_core',
                                  'outside_height', 'duplicate_service', 'explicit_duplicate'])
def test_core_service_refuses_missing_ambiguous_or_uncovered_connections(change):
    value = repeated_core_plan()
    declaration = value['core_connections'][0]
    if change in ('unknown_floor', 'duplicate_floor', 'missing_scope'):
        declaration.pop('repeat_ids')
        declaration['instance_ids'] = {'unknown_floor': ['missing'],
                                       'duplicate_floor': ['F01', 'F01'], 'missing_scope': []}[change]
    elif change == 'unknown_repeat':
        declaration['repeat_ids'] = ['missing']
    elif change == 'duplicate_repeat':
        declaration['repeat_ids'] = ['offices', 'offices']
    elif change == 'overlapping_selection':
        declaration['instance_ids'] = ['F01']
    elif change == 'unknown_core':
        declaration['core_space_id'] = 'CORE:missing'
    elif change == 'unknown_local':
        declaration['space'] = 'missing'
    elif change == 'missing_member':
        value['repeats'][0]['spanning_space_ids'] = []
    elif change == 'short_core':
        value['instances'][0]['height'] = 8
    elif change == 'outside_height':
        declaration['z'] = [1, 4]
    elif change == 'duplicate_service':
        duplicate = copy.deepcopy(declaration)
        duplicate['id'] = 'another_lift'
        value['core_connections'].append(duplicate)
    else:
        value['connections'] = [{'id': 'manual_landing', 'space_id': 'F01:room',
            'other_space_id': 'CORE:core', 'p1': [2, 1], 'p2': [2, 2], 'z': [0, 2.1], 'kind': 'door'}]
    with pytest.raises(ValueError):
        expand_parametric_proposal(value)


def test_core_service_does_not_move_a_declared_door_to_find_a_host(tmp_path):
    value = repeated_core_plan()
    value['core_connections'][0].update(p1=[3, 1], p2=[3, 2])
    proposal = expand_parametric_proposal(value)
    assert proposal['geometry']['openings'][0]['p1'] == [3, 1]
    report = export_source_proposal(proposal, tmp_path / 'wrong_host')
    assert not report['source_geometry_ready'], report


@pytest.mark.parametrize('schema', ['2', 'source_polygon_v1'])
def test_template_role_evidence_reuses_shared_contract_and_survives_repetition(tmp_path, schema):
    value = repeated_core_plan()
    value['schema_version'] = schema
    evidence = {'role': 'office', 'basis': 'inferred', 'source_refs': ['office use inference'],
                'assumptions': ['Unknown internals use the explicitly repeated office layout']}
    value['templates']['storey']['spaces'][0]['role_evidence'] = evidence
    proposal = expand_parametric_proposal(value)
    assert value['templates']['storey']['spaces'][0]['role_evidence'] == evidence
    for floor in proposal['geometry']['floors']:
        if floor['name'] != 'CORE':
            assert floor['cells'][0]['role_evidence'] == evidence
    report = export_source_proposal(proposal, tmp_path / schema)
    assert report['source_geometry_ready'], report
    source = json.loads((tmp_path / schema / 'source_model.json').read_text())
    assert all(space['role_evidence'] == evidence for space in source['spaces'] if space['id'] != 'CORE:core')
    value['templates']['storey']['spaces'][0]['role_evidence']['role'] = 'retail'
    with pytest.raises(ValueError, match='does not match'):
        expand_parametric_proposal(value)


def test_repeat_limits_apply_to_total_expansion_and_openings():
    value = repeated_plan()
    value['repeats'][0]['count'] = 100
    assert len(expand_parametric_instances(value)) == 100
    value['instances'] = [{'id': 'extra', 'template': 'typical', 'z': 400, 'height': 3}]
    with pytest.raises(ValueError, match='1 to 100 expanded'):
        expand_parametric_proposal(value)
    value = repeated_plan()
    value['connections'] = [{'id': f'explicit{i}'} for i in range(5000)]
    with pytest.raises(ValueError, match='aperture expansion'):
        expand_parametric_proposal(value)


def test_core_connection_and_explicit_aperture_id_collision_is_rejected():
    value = repeated_core_plan()
    value['connections'] = [{'id': 'F01:core:lift', 'space_id': 'EQUIPMENT:room',
                             'other_space_id': None, 'z': [9, 11]}]
    with pytest.raises(ValueError, match='duplicate expanded aperture ID'):
        expand_parametric_proposal(value)
