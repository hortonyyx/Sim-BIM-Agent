"""Shared architectural anchors retain one stepped space and its real hosts."""
import copy

import pytest

from src.agent.geometry.architectural_regularization import (
    ArchitecturalRegularizationError, DEFAULT_ARCHITECTURAL_POLICY,
    regularize_architectural_proposal,
)


def rect(x0, y0, x1, y1):
    return [[x0,y0],[x1,y0],[x1,y1],[x0,y1]]


def proposal():
    lower = rect(.024,.024,6.024,4.024)
    upper = rect(1.024,.024,6.024,4.024)
    hole = rect(2.024,1.024,3.024,2.024)
    cell = dict(id='office', role='office', polygon=lower, holes=[hole],
        x=[.024,6.024], y=[.024,4.024],
        vertical_sections=[dict(z=[.024,1.524], polygon=copy.deepcopy(lower), holes=[copy.deepcopy(hole)]),
                           dict(z=[1.524,3.024], polygon=upper, holes=[copy.deepcopy(hole)])])
    return dict(geometry=dict(schema_version='source_polygon_v1', footprint_x=[.024,6.024],
        footprint_y=[.024,4.024], floors=[dict(name='F1', z_floor=.024, ceiling_height=3,
            footprint={'vertices':copy.deepcopy(lower)}, cells=[cell])], openings=[], windows=[]))


def adopt(raw):
    return regularize_architectural_proposal(raw, policy=DEFAULT_ARCHITECTURAL_POLICY)


def test_sections_round_trip_shared_xy_z_anchors_and_holes_without_splitting_space():
    raw = proposal()
    before = copy.deepcopy(raw)
    raw['geometry']['openings'] = [dict(id='east-door', space_id='office', other_space_id=None,
        p1=[6.024,.624], p2=[6.024,1.224], z=[.924,2.124], kind='door')]
    adopted, audit = adopt(raw)
    cells = adopted['geometry']['floors'][0]['cells']
    assert len(cells) == 1 and cells[0]['id'] == 'office'
    sections = cells[0]['vertical_sections']
    assert [s['z'] for s in sections] == [[0,1.5],[1.5,3]]
    assert sections[0]['polygon'] == rect(0,0,6,4)
    assert sections[1]['polygon'] == rect(1,0,6,4)
    assert sections[0]['holes'] == sections[1]['holes'] == [rect(2,1,3,2)]
    assert adopted['geometry']['openings'][0]['p1'] == [6,.6]
    assert adopted['geometry']['openings'][0]['z'] == [.9,2.1]
    assert audit['contact_check']['section_prism_count'] == 2
    assert raw['geometry']['floors'] == before['geometry']['floors']


def test_aperture_in_section_hole_can_cross_a_vertical_seam():
    raw = proposal()
    raw['geometry']['openings'] = [dict(id='core-door', space_id='office', other_space_id=None,
        p1=[2.024,1.224], p2=[2.024,1.824], z=[.924,2.124], kind='door')]
    adopted, _ = adopt(raw)
    assert adopted['geometry']['openings'][0]['p1'] == [2,1.2]
    assert adopted['geometry']['openings'][0]['p2'] == [2,1.8]


def test_aperture_cannot_bind_projection_wall_that_vanishes_above_setback():
    raw = proposal()
    raw['geometry']['openings'] = [dict(id='bad-upper-window', space_id='office', other_space_id=None,
        p1=[.024,.624], p2=[.024,1.224], z=[1.724,2.724], kind='window')]
    with pytest.raises(ArchitecturalRegularizationError) as failure:
        adopt(raw)
    assert failure.value.audit['rejections'][0]['code'] == 'ambiguous_aperture_host'


def test_opening_at_a_section_seam_follows_that_shared_absolute_height():
    raw = proposal()
    sections = raw['geometry']['floors'][0]['cells'][0]['vertical_sections']
    sections[0]['z'][1] = sections[1]['z'][0] = 1.56
    raw['geometry']['openings'] = [dict(id='upper-west', space_id='office', other_space_id=None,
        p1=[1.024,.624], p2=[1.024,1.224], z=[1.56,2.724], kind='window')]
    adopted, _ = adopt(raw)
    assert adopted['geometry']['floors'][0]['cells'][0]['vertical_sections'][1]['z'][0] == 1.6
    assert adopted['geometry']['openings'][0]['z'] == [1.6,2.7]


def test_grid_cannot_delete_a_thin_vertical_section():
    raw = proposal()
    sections = raw['geometry']['floors'][0]['cells'][0]['vertical_sections']
    sections[0]['z'][1] = sections[1]['z'][0] = .04
    with pytest.raises(ArchitecturalRegularizationError) as failure:
        adopt(raw)
    assert failure.value.audit['rejections'][0]['code'] == 'collapsed_section_height'


def test_cardinal_window_uses_actual_upper_section_and_deduplicates_lower_shared_edges():
    raw = proposal()
    raw['geometry']['windows'] = [dict(id='upper-west', floor='F1', room='office',
        facade='West', span=[.624,1.224], z=[1.724,2.724]),
        dict(id='east-seam', floor='F1', room='office',
        facade='East', span=[.624,1.224], z=[.924,2.124])]
    adopted, _ = adopt(raw)
    assert [w['span'] for w in adopted['geometry']['windows']] == [[.6,1.2],[.6,1.2]]
    assert adopted['geometry']['windows'][0]['z'] == [1.7,2.7]


def test_contact_guard_uses_sections_instead_of_overlapping_plan_projection():
    raw = proposal()
    cell = raw['geometry']['floors'][0]['cells'][0]
    cell.pop('holes')
    for section in cell['vertical_sections']:
        section.pop('holes')
    cell['vertical_sections'][1]['polygon'] = rect(2.024,.024,6.024,4.024)
    neighbor = rect(.024,.024,2.004,4.024)
    raw['geometry']['floors'].append(dict(name='F2', z_floor=1.624, ceiling_height=1.4,
        footprint={'vertices':neighbor}, cells=[dict(id='neighbor',role='office', polygon=neighbor,
            x=[.024,2.004],y=[.024,4.024])]))
    with pytest.raises(ArchitecturalRegularizationError) as failure:
        adopt(raw)
    rejection = failure.value.audit['rejections'][0]
    assert rejection['code'] == 'volume_contact_changed'
    assert rejection['before'] == 'separate'
    assert rejection['after'] == 'lateral_contact'
