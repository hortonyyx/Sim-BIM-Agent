"""Legacy edit routes cannot erase the height-dependent part of one source zone."""
import copy
import json

import pytest

from src.agent.execution.source_proposal import export_source_proposal
from src.agent.geometry.parametric_proposal import expand_parametric_proposal
from src.agent.geometry.proposal_edits import apply_proposal_edits
from src.agent.geometry.source_volume import space_volume


def proposal():
    lower = [[0, 0], [8, 0], [8, 6], [0, 6]]
    upper = [[1, 0], [8, 0], [8, 6], [1, 6]]
    return dict(geometry=dict(schema_version='source_polygon_v1', footprint_x=[0, 8], footprint_y=[0, 6],
        floors=[dict(name='F', z_floor=0, ceiling_height=3.5, footprint=dict(vertices=lower), cells=[
            dict(id='office', role='office', x=[0, 8], y=[0, 6], polygon=lower,
                 vertical_sections=[dict(z=[0, 1.5], polygon=lower), dict(z=[1.5, 3.5], polygon=upper)])])],
        openings=[], windows=[]), assumptions=[], unresolved=[])


def test_reshape_does_not_leave_old_height_sections_behind():
    raw = proposal()
    before = copy.deepcopy(raw)
    with pytest.raises(ValueError, match='vertical_sections.*build_bim'):
        apply_proposal_edits(raw, [dict(op='reshape_spaces', spaces=[dict(id='office',
            polygon=[[0, 0], [7, 0], [7, 6], [0, 6]])], reason='observed correction', source_refs=['test:view'])])
    assert raw == before


def test_absolute_sections_are_not_repeated_as_parametric_template():
    floor = proposal()['geometry']['floors'][0]
    cell = floor['cells'][0]
    template = dict(footprint=floor['footprint'], spaces=[dict(
        id='office', role='office', source_refs=['test:view'], polygon=cell['polygon'],
        vertical_sections=cell['vertical_sections'])])
    with pytest.raises(ValueError, match='vertical_sections.*absolute elevations.*build_bim'):
        expand_parametric_proposal(dict(schema_version='source_polygon_v1', templates={'t':template},
            instances=[dict(id='F1', template='t', z=0, height=3.5), dict(id='F2', template='t', z=3.5, height=3.5)],
            assumptions=[], unresolved=[]))


def test_export_keeps_one_zone_and_its_actual_volume(tmp_path):
    output = tmp_path / 'stepped'
    report = export_source_proposal(proposal(), output)
    assert report['source_geometry_ready'], report
    saved = json.loads((output / 'source_model.json').read_text(encoding='utf-8'))
    assert len(saved['spaces']) == 1
    space = saved['spaces'][0]
    assert space['id'] == 'office' and len(space['vertical_sections']) == 2
    assert space_volume(space) == 48 * 1.5 + 42 * 2
    assert len(saved['public_names']['spaces']) == 1
    assert not saved['connections']
    assert (output / 'viewer.html').is_file()
