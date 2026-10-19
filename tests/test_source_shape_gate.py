"""Actual source saves apply axis-strip rules only where their premise holds."""
import copy
import json

import pytest

from scripts.tool_scripts.bim_agent_replies import precision_summary
from scripts.tool_scripts.run_bim_agent import Toolkit
from tests.test_bim_agent_tools import _run_with_one_image
from tests.test_source_curves import arc_proposal
from tests.test_source_simple_polygon import cell, proposal as diagonal_proposal


def single_polygon(points):
    return {'geometry': {'schema_version': 'source_polygon_v1', 'floors': [
        {'name': 'F1', 'z_floor': 0, 'ceiling_height': 3,
         'footprint': {'vertices': points}, 'cells': [cell('room', points)]}],
        'openings': []}, 'assumptions': [], 'unresolved': []}


def mixed_polygon(width):
    value = single_polygon([[0, 0], [4, 0], [5, 2], [4, 4], [0, 4]])
    value['geometry']['floors'][0]['cells'] = [
        cell('axis', [[0, 0], [width, 0], [width, 4], [0, 4]]),
        cell('slant', [[width, 0], [4, 0], [5, 2], [4, 4], [width, 4]])]
    return value


def rejection_report(run, result):
    assert result['error_stage'] == 'source_hard_constraints', result
    assert result['source_geometry_ready'] is False
    assert result['saved_candidate'] is None
    assert not list(run.glob('candidate_*'))
    return json.loads((run / result['regularization']['file']).read_bytes())['hard_constraints']


@pytest.mark.parametrize('kind', ['curve', 'triangle', 'diagonal_shared'])
def test_actual_build_saves_nonorthogonal_shapes_with_explicit_unassessed_width(tmp_path, kind):
    # The acute triangle and declared R4 arc have short axis cuts at corners.
    # Those cuts are not measurements of a room's usable minimum clear width.
    value = (arc_proposal() if kind == 'curve' else diagonal_proposal()
             if kind == 'diagonal_shared' else single_polygon([[0, 0], [4, 0], [.1, 4]]))
    before = copy.deepcopy(value)
    run = _run_with_one_image(tmp_path)
    result = Toolkit(run).build(value)
    assert result['source_geometry_ready'], result
    assert result['saved_candidate'] == 'candidate_01'
    assert value == before
    saved = json.loads((run / result['candidate'] / 'source_model.json').read_bytes())
    assert saved['validation']['status'] == 'pass'
    report = result['building_precision']
    assert report['status'] == 'pass' and report['total'] == 0
    applicability = report['axis_rule_applicability']
    assert applicability['status'] == 'not_applicable'
    assert set(applicability['not_applicable_space_ids']) == {s['id'] for s in saved['spaces']}
    assert applicability['checked_space_ids'] == []
    assert applicability['not_applicable_coverage_floor_ids'] == ['F1']
    assert precision_summary(report)['axis_rule_applicability'] == applicability
    if kind == 'curve':
        assert saved['boundary_curves'] == value['geometry']['boundary_curves']
        assert len(saved['spaces'][0]['polygon']) > 8
        assert saved['opening_hosts']['aperture']
    elif kind == 'diagonal_shared':
        assert len(saved['opening_hosts']['door']) == 2
        assert len(saved['opening_hosts']['window']) == 1
        assert len(saved['boundary_relations']) == 1
    else:
        assert saved['spaces'][0]['polygon'] == value['geometry']['floors'][0]['cells'][0]['polygon']


@pytest.mark.parametrize('width,ready', [(.4, False), (.6, True)])
def test_mixed_floor_keeps_real_axis_room_width_gate(tmp_path, width, ready):
    run = _run_with_one_image(tmp_path)
    result = Toolkit(run).build(mixed_polygon(width))
    assert result['source_geometry_ready'] is ready, result
    report = result['building_precision'] if ready else rejection_report(run, result)
    applicability = report['axis_rule_applicability']
    assert applicability['status'] == 'partially_applicable'
    assert applicability['checked_space_ids'] == ['axis']
    assert applicability['not_applicable_space_ids'] == ['slant']
    assert applicability['not_applicable_coverage_floor_ids'] == ['F1']
    if not ready:
        assert result['regularization']['axis_rule_applicability'] == applicability
        assert any(row['type'] == 'thin_space' and row['space_id'] == 'axis'
                   and row['width_m'] == pytest.approx(.4) for row in report['items'])


@pytest.mark.parametrize('offset,ready', [(.2, False), (.3, True)])
@pytest.mark.parametrize('schema', ['2', 'source_polygon_v1'])
def test_actual_orthogonal_geometry_keeps_alignment_gate_in_both_schemas(tmp_path, offset, ready, schema):
    floors = []
    for index, x in enumerate([2, 2 + offset]):
        floors.append({'name': f'F{index + 1}', 'z_floor': 3 * index, 'ceiling_height': 3,
                       'footprint': {'vertices': [[0, 0], [4, 0], [4, 4], [0, 4]]},
                       'cells': [{'id': f'left{index}', 'x': [0, x], 'y': [0, 4], 'role': 'office'},
                                 {'id': f'right{index}', 'x': [x, 4], 'y': [0, 4], 'role': 'office'}]})
    value = {'geometry': {'schema_version': schema, 'footprint_x': [0, 4], 'footprint_y': [0, 4],
                          'floors': floors, 'openings': [], 'windows': []}, 'assumptions': [], 'unresolved': []}
    run = _run_with_one_image(tmp_path)
    result = Toolkit(run).build(value)
    assert result['source_geometry_ready'] is ready, result
    report = result['building_precision'] if ready else rejection_report(run, result)
    assert report['axis_rule_applicability']['status'] == 'applicable'
    if not ready:
        assert report['counts']['storey_wall_offset'] == 1


def test_curved_floors_still_reject_vertical_gap(tmp_path):
    value = arc_proposal()
    upper = copy.deepcopy(value['geometry']['floors'][0])
    upper.update(name='F2', z_floor=3.15)
    upper['cells'][0]['id'] = 'upper'
    value['geometry']['floors'].append(upper)
    run = _run_with_one_image(tmp_path)
    result = Toolkit(run).build(value)
    report = rejection_report(run, result)
    assert report['counts'] == {'vertical_gap_or_overlap': 1}
    assert report['items'][0]['distance_m'] == pytest.approx(.15)
    assert report['axis_rule_applicability']['status'] == 'not_applicable'
    assert result['regularization']['axis_rule_applicability'] == report['axis_rule_applicability']


@pytest.mark.parametrize('invalid', ['overlap', 'coverage', 'opening_host'])
def test_nonorthogonal_scope_does_not_relax_source_validity(tmp_path, invalid):
    value = diagonal_proposal()
    floor = value['geometry']['floors'][0]
    if invalid == 'overlap':
        floor['cells'].append({**copy.deepcopy(floor['cells'][0]), 'id': 'duplicate'})
    elif invalid == 'coverage':
        floor['footprint']['vertices'] = [[0, 0], [4, 0], [4, 4], [0, 4]]
    else:
        value['geometry']['openings'][0]['p2'] = [1.5, 1.6]
    run = _run_with_one_image(tmp_path)
    result = Toolkit(run).build(value)
    assert result['source_geometry_ready'] is False, result
    assert result['building_precision']['axis_rule_applicability']['status'] == 'not_applicable'
    saved = json.loads((run / result['candidate'] / 'source_model.json').read_bytes())
    assert saved['validation']['status'] == 'severe'
    if invalid == 'opening_host':
        assert saved['unbuilt_openings'][0]['id'] == 'door'
