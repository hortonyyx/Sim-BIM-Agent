"""Declared curve error, shared chords and source topology stay auditable."""
import copy
import json
import math

import pytest
from shapely.geometry import Point, Polygon

from src.agent.correction.parse import ensure_corrected_geometry
from src.agent.execution.source_proposal import export_source_proposal
from src.agent.geometry.proposal_edits import apply_proposal_edits
from src.agent.geometry.source_bim import build_source_bim, source_view_geometry
from src.agent.geometry.source_curves import expand_source_polygon_geometry
from src.agent.geometry.source_model import _digest
from src.agent.geometry.source_plan_view import render_source_plan


def arc_proposal(*, shared=False):
    curves = {
        'arc': {'kind': 'circular_arc', 'center': [0, 0], 'radius_m': 4,
                'start_degrees': 0, 'sweep_degrees': 90, 'max_chord_error_m': .02,
                'source_refs': ['mesh:declared-arc']},
        'west': {'kind': 'line', 'p1': [0, 4], 'p2': [0, 0], 'source_refs': ['mesh:west']},
        'south': {'kind': 'line', 'p1': [0, 0], 'p2': [4, 0], 'source_refs': ['mesh:south']},
    }
    path = [{'curve_id': key} for key in ('arc', 'west', 'south')]
    cells = [{'id': 'inside', 'role': 'office', 'boundary_path': path, 'source_refs': ['mesh:inside']}]
    footprint = {'boundary_path': path}
    opening = {'id': 'aperture', 'kind': 'window', 'space_id': 'inside', 'other_space_id': None,
               'curve_segment': {'curve_id': 'arc', 'segment_index': 3, 'span_fraction': [.2, .8]},
               'z': [1, 2], 'source_refs': ['mesh:aperture']}
    if shared:
        curves.update(east={'kind': 'line', 'p1': [4, 0], 'p2': [4, 4], 'source_refs': ['mesh:east']},
                      north={'kind': 'line', 'p1': [4, 4], 'p2': [0, 4], 'source_refs': ['mesh:north']})
        cells.append({'id': 'outside', 'role': 'office', 'source_refs': ['mesh:outside'],
                      'boundary_path': [{'curve_id': 'arc', 'reverse': True}, {'curve_id': 'east'}, {'curve_id': 'north'}]})
        footprint = {'vertices': [[0, 0], [4, 0], [4, 4], [0, 4]]}
        opening.update(kind='door', other_space_id='outside')
    return {'geometry': {'schema_version': 'source_polygon_v1', 'boundary_curves': curves,
                         'floors': [{'name': 'F1', 'z_floor': 0, 'ceiling_height': 3,
                                     'footprint': footprint, 'cells': cells}], 'openings': [opening]},
            'assumptions': ['declared curve is an approximation, not mesh fitting'], 'unresolved': []}


def build(proposal):
    return build_source_bim(ensure_corrected_geometry(proposal['geometry']), capability_profile='simple_polygon')


def test_arc_sagitta_provenance_single_opening_and_saved_geometry(tmp_path):
    value = arc_proposal()
    untouched = copy.deepcopy(value)
    expanded = expand_source_polygon_geometry(value['geometry'])
    sample = expanded['curve_discretization']['arc']
    errors = []
    for a, b in zip(sample['points'], sample['points'][1:]):
        midpoint = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]
        errors.append(4 - math.hypot(*midpoint))
    assert max(errors) <= .02
    assert max(errors) == pytest.approx(sample['maximum_chord_deviation_m'])
    assert expanded == expand_source_polygon_geometry(expanded)
    out = tmp_path / 'saved'
    report = export_source_proposal(value, out)
    assert report['source_geometry_ready'], report
    assert value == untouched == json.loads((out / 'proposal.json').read_bytes())
    source = json.loads((out / 'source_model.json').read_bytes())
    assert len(source['spaces']) == 1 and len(source['openings']) == 1
    assert source['boundary_curves']['arc'] == value['geometry']['boundary_curves']['arc']
    assert source['curve_discretization'] == expanded['curve_discretization']
    assert source['floors'][0]['footprint_boundary_path'][0]['curve_id'] == 'arc'
    curved = [row for row in source['boundaries'] if any(origin['curve_id'] == 'arc' for origin in row.get('curve_origins', []))]
    assert len(curved) == sample['segment_count']
    assert all('mesh:declared-arc' in row['source_refs'] for row in curved)
    opening = source['openings'][0]
    assert opening['curve_origin']['segment_index'] == 3
    assert opening['curve_origin']['curve_parameter'] == pytest.approx([3.2 / sample['segment_count'], 3.8 / sample['segment_count']])
    assert len(source['opening_hosts']['aperture']) == 1
    assert len(source_view_geometry(source)['windows']) == 1
    assert len(json.loads((out / 'display_geometry.json').read_bytes())['windows']) == 1
    assert source['source_model_sha256'] == _digest({k: v for k, v in source.items() if k != 'source_model_sha256'})
    assert (out / 'viewer.html').is_file()
    _, plan = render_source_plan(source, 'F1')
    assert plan['opening_ids'] == ['aperture']


def test_reversed_shared_arc_has_actual_contacts_and_one_logical_door():
    source = build(arc_proposal(shared=True))
    assert source['validation']['status'] == 'pass'
    sample = source['curve_discretization']['arc']
    assert len(source['spaces']) == 2
    assert len(source['boundary_relations']) == sample['segment_count']
    area = sum(region['area_m2'] for contact in source['boundary_relations'] for region in contact['regions'])
    assert area == pytest.approx(3 * sum(math.dist(a, b) for a, b in zip(sample['points'], sample['points'][1:])))
    assert len(source['opening_hosts']['aperture']) == 2
    assert len(source['connections']) == len(source['openings']) == 1
    assert all(row['counterpart_ids'] for row in source['boundaries'] if any(
        origin['curve_id'] == 'arc' for origin in row.get('curve_origins', [])))


def test_full_circle_is_one_room_and_a_tighter_tolerance_increases_chords():
    value = arc_proposal()
    value['geometry']['boundary_curves']['arc']['sweep_degrees'] = 360
    value['geometry']['floors'][0]['cells'][0]['boundary_path'] = [{'curve_id': 'arc'}]
    value['geometry']['floors'][0]['footprint']['boundary_path'] = [{'curve_id': 'arc'}]
    source = build(value)
    assert source['validation']['status'] == 'pass'
    assert len(source['spaces']) == 1
    coarse = source['curve_discretization']['arc']['segment_count']
    value['geometry']['boundary_curves']['arc']['max_chord_error_m'] = .005
    fine = build(value)
    assert fine['curve_discretization']['arc']['segment_count'] > coarse
    assert fine['curve_discretization']['arc']['maximum_chord_deviation_m'] <= .005


def test_curve_aperture_revisions_keep_identity_and_do_not_move_other_geometry(tmp_path):
    value = arc_proposal()
    source = build(value)
    updated = apply_proposal_edits(value, [{'op': 'update_opening', 'id': 'aperture',
        'changes': {'curve_segment': {'curve_id': 'arc', 'segment_index': 4, 'span_fraction': [.1, .9]}},
        'reason': 'explicit new chord observation', 'source_refs': ['mesh:rechecked']}])
    revised = build(updated)
    assert revised['boundaries'] == source['boundaries'] and revised['spaces'] == source['spaces']
    assert [row['id'] for row in revised['openings']] == ['aperture']
    assert revised['openings'][0]['vertices'] != source['openings'][0]['vertices']
    assert export_source_proposal(updated, tmp_path / 'revised')['source_geometry_ready']
    removed = apply_proposal_edits(value, [{'op': 'remove_opening', 'id': 'aperture',
        'reason': 'test replacement', 'source_refs': ['mesh:rechecked']}])
    added = apply_proposal_edits(removed, [{'op': 'add_opening', 'opening': value['geometry']['openings'][0],
        'reason': 'explicit chord aperture', 'source_refs': ['mesh:rechecked']}])
    assert len(build(added)['openings']) == 1


@pytest.mark.parametrize('fault', ['missing_basis', 'zero_sweep', 'negative_tolerance', 'too_many_chords',
                                 'gap', 'unknown_curve', 'wrong_polygon', 'multi_chord_aperture'])
def test_unsupported_or_contradictory_curve_declarations_fail_closed(fault):
    value = arc_proposal()
    geometry = value['geometry']
    arc = geometry['boundary_curves']['arc']
    if fault == 'missing_basis':
        arc['source_refs'] = []
    elif fault == 'zero_sweep':
        arc['sweep_degrees'] = 0
    elif fault == 'negative_tolerance':
        arc['max_chord_error_m'] = -1
    elif fault == 'too_many_chords':
        arc['max_chord_error_m'] = 1e-14
    elif fault == 'gap':
        geometry['boundary_curves']['west']['p1'] = [0, 4.1]
    elif fault == 'unknown_curve':
        geometry['floors'][0]['cells'][0]['boundary_path'][0]['curve_id'] = 'absent'
    elif fault == 'wrong_polygon':
        geometry['floors'][0]['cells'][0]['polygon'] = [[0, 0], [4, 0], [0, 4]]
    else:
        geometry['openings'][0]['curve_segment']['span_fraction'] = [.1, 1.1]
    with pytest.raises(ValueError):
        build(value)


def test_floor_footprint_hole_preserves_declared_spaces_and_has_no_fake_slab(tmp_path):
    outer = [[0, 0], [10, 0], [10, 10], [0, 10]]
    hole = [[4, 4], [6, 4], [6, 6], [4, 6]]
    cells = [{'id': identity, 'role': 'office', 'x': x, 'y': y}
             for identity, x, y in [('west', [0, 4], [0, 10]), ('east', [6, 10], [0, 10]),
                                     ('south', [4, 6], [0, 4]), ('north', [4, 6], [6, 10])]]
    value = {'geometry': {'schema_version': 'source_polygon_v1', 'floors': [
        {'name': 'F1', 'z_floor': 0, 'ceiling_height': 3, 'footprint': {'vertices': outer, 'holes': [hole]}, 'cells': cells}]},
        'assumptions': [], 'unresolved': []}
    source = build(value)
    assert source['validation']['status'] == 'pass'
    assert len(source['spaces']) == 4
    assert source['floors'][0]['footprint_holes'] == [hole]
    assert sum(Polygon(row['polygon']).area for row in source['spaces']) == 96
    assert all(not Polygon([p[:2] for p in boundary['vertices']]).covers(Point(5, 5))
               for boundary in source['boundaries'] if boundary['geometry_type'] in {'floor', 'ceiling'})
    picture, _ = render_source_plan(source, 'F1')
    assert picture.getpixel((390, 390)) == (255, 255, 255)
    assert export_source_proposal(value, tmp_path / 'courtyard')['source_geometry_ready']
    bad = copy.deepcopy(value)
    bad['geometry']['floors'][0]['cells'] = [{'id': 'one', 'role': 'office', 'x': [0, 10], 'y': [0, 10], 'holes': [hole]}]
    with pytest.raises(ValueError, match='single-space holes'):
        build(bad)
    bad['geometry']['floors'][0]['cells'][0].pop('holes')
    assert any(row['code'] == 'source.floor_coverage' for row in build(bad)['validation']['findings'])


def test_curves_require_explicit_source_schema_and_profile():
    value = arc_proposal()
    geometry = ensure_corrected_geometry(value['geometry'])
    with pytest.raises(ValueError, match='capability_profile'):
        build_source_bim(geometry, capability_profile='orthogonal_polygon')
    with pytest.raises(ValueError, match='source_polygon_v1'):
        expand_source_polygon_geometry({**value['geometry'], 'schema_version': '2'})


def test_precision_does_not_treat_a_declared_narrow_courtyard_as_missing_floor():
    from src.agent.geometry.building_precision import precision_report
    cells = [{'id': identity, 'role': 'office', 'x': x, 'y': y}
             for identity, x, y in [('west', [0, 4], [0, 10]), ('east', [6, 10], [0, 10]),
                                     ('south', [4, 6], [0, 4]), ('north', [4, 6], [4.1, 10])]]
    source = build({'geometry': {'schema_version': 'source_polygon_v1', 'floors': [
        {'name': 'F1', 'z_floor': 0, 'ceiling_height': 3,
         'footprint': {'vertices': [[0, 0], [10, 0], [10, 10], [0, 10]],
                       'holes': [[[4, 4], [6, 4], [6, 4.1], [4, 4.1]]]}, 'cells': cells}]},
        'assumptions': [], 'unresolved': []})
    assert source['validation']['status'] == 'pass'
    assert 'thin_coverage_gap' not in precision_report(source)['counts']
