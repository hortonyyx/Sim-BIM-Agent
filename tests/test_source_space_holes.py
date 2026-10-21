"""An office around two cores stays one space, with real cap and host geometry."""
import copy
import json

import numpy as np
import pytest
from shapely.geometry import Point, Polygon

from src.agent.correction.parse import ensure_corrected_geometry
from src.agent.execution.source_proposal import export_source_proposal
from src.agent.geometry.architectural_regularization import regularize_architectural_plan, regularize_architectural_proposal
from src.agent.geometry.parametric_proposal import expand_parametric_proposal
from src.agent.geometry.source_bim import build_source_bim, source_view_geometry
from src.agent.geometry.source_enclosure import _boundary_frame, apply_source_enclosure
from src.agent.geometry.source_plan_view import render_source_plan
from src.agent.geometry.source_visibility import source_depth_buffer
from src.agent.judge.source_partition import compare_partitions


def plan():
    outer = [[0, 0], [12, 0], [12, 10], [0, 10]]
    holes = [[[3, 3], [5, 3], [5, 6], [3, 6]], [[8, 3], [10, 3], [10, 6], [8, 6]]]
    return dict(schema_version='source_polygon_v1', templates={'typical': {
        'footprint': outer,
        'spaces': [dict(id='office', polygon=outer, holes=holes, role='office', source_refs=['test:open-office']),
                   *[dict(id=f'core{i}', polygon=h, role='shaft', source_refs=['test:separate-core']) for i,h in enumerate(holes)]],
        'doors': [dict(id=f'd{i}', space='office', other_space=f'core{i}',
                       p1=[h[0][0], 4], p2=[h[0][0], 5], z=[0, 2.1], source_refs=['test:landing']) for i,h in enumerate(holes)]}},
        instances=[], repeats=[dict(id='run', template='typical', id_prefix='F', start=1, count=2,
                                   z=0, height=3, source_refs=['test:two-storeys'])],
        continuous_spaces=[dict(id=f'C{i}', space=f'core{i}', repeat_ids=['run'],
                                source_refs=['test:continuous-core']) for i in range(2)],
        assumptions=[], unresolved=[])


def build(value):
    return build_source_bim(ensure_corrected_geometry(value['geometry']), capability_profile='simple_polygon')


def area(boundary):
    return Polygon([p[:2] for p in boundary['vertices']], [[p[:2] for p in h] for h in boundary.get('holes', [])]).area


def test_two_cores_identity_doors_caps_contacts_and_saved_view(tmp_path):
    value = expand_parametric_proposal(plan())
    source = build(value)
    assert source['validation']['status'] == 'pass', source['validation']
    assert len(source['spaces']) == 4  # two offices, two continuous cores
    assert len(source['openings']) == 4
    assert all(len(hosts) == 2 for hosts in source['opening_hosts'].values())
    office = next(s for s in source['spaces'] if s['id'] == 'F1:office')
    shape = Polygon(office['polygon'], office['holes'])
    assert shape.area == 108 and not shape.covers(Point(4, 4))
    caps = [b for b in source['boundaries'] if b['space_id'] == office['id'] and b['geometry_type'] != 'wall']
    assert len(caps) == 2 and all(area(b) == 108 for b in caps)
    assert len([b for b in source['boundaries'] if b['space_id'] == office['id'] and b['geometry_type'] == 'wall']) == 12
    contacts = [r for r in source['boundary_relations'] if set(r['space_ids']) == {'F1:office','F2:office'}]
    assert sum(p['area_m2'] for r in contacts for p in r['regions']) == 108
    for core in ('C0:core0', 'C1:core1'):
        core_caps = [b for b in source['boundaries'] if b['space_id'] == core and b['geometry_type'] != 'wall']
        assert {p[2] for b in core_caps for p in b['vertices']} == {0, 6}
    display = source_view_geometry(source)
    assert sum(Polygon([p[:2] for p in r['verts']], [[p[:2] for p in h] for h in r['holes']]).area
               for r in display['display_surface_parts'][caps[0]['id']]) == 108
    assert _boundary_frame(caps[0], allow_nonorthogonal=True)['parent'].area == 108
    report = export_source_proposal(value, tmp_path / 'saved')
    assert report['source_geometry_ready'], report
    saved = json.loads((tmp_path / 'saved/source_model.json').read_text(encoding='utf-8'))
    assert saved['spaces'] == source['spaces'] and saved['boundaries'] == source['boundaries']
    picture, _ = render_source_plan(saved, 'F1')
    assert picture.size == (1080, 800)


def test_adoption_moves_inner_hosts_with_cores_and_generic_roundtrip():
    value = plan()
    value = json.loads(json.dumps(value))
    t = value['templates']['typical']
    for ring in [t['footprint'], *[r for s in t['spaces'] for r in [s['polygon'], *s.get('holes', [])]]]:
        for p in ring:
            p[0] += .02
            p[1] += .03
    for door in t['doors']:
        for key in ('p1','p2'):
            door[key][0] += .02
            door[key][1] += .03
    adopted, _ = regularize_architectural_plan(value)
    proposal = expand_parametric_proposal(adopted)
    assert build(proposal)['validation']['status'] == 'pass'
    generic, _ = regularize_architectural_proposal(proposal)
    assert build(generic)['validation']['status'] == 'pass'
    office = next(c for f in generic['geometry']['floors'] for c in f['cells'] if c['id']=='F1:office')
    assert len(office['holes']) == 2


@pytest.mark.parametrize('hole', [
    [[-1,3],[5,3],[5,6],[-1,6]],
    [[0,3],[5,3],[5,6],[0,6]],
    [[3,3],[5,6],[5,3],[3,6]],
    [[4,4],[9,4],[9,5],[4,5]],
])
def test_invalid_or_overlapping_holes_reject_without_repair(hole):
    value = copy.deepcopy(plan())
    value['templates']['typical']['spaces'][0]['holes'][0] = hole
    with pytest.raises(ValueError):
        expand_parametric_proposal(value)


def test_top_view_depth_does_not_seal_a_space_hole():
    source = build(expand_parametric_proposal(plan()))
    cap = next(b for b in source['boundaries'] if b['space_id']=='F1:office' and b['geometry_type']=='ceiling')
    camera = dict(eye=[6,5,20], target=[6,5,3], screen_right=[1,0,0], screen_up=[0,1,0], forward_from_eye=[0,0,-1])
    depths = source_depth_buffer([cap], lambda p: np.asarray(p), camera, {'width':12,'height':10}, (120,100))
    assert np.isinf(depths[55,40])  # within core 0 cutout
    assert np.isfinite(depths[10,10])


def test_comparison_detects_filled_hole_in_an_otherwise_identical_space():
    source = build(expand_parametric_proposal(plan()))
    office = next(s for s in source['spaces'] if s['id']=='F1:office')
    assert compare_partitions([office], [copy.deepcopy(office)])['status'] == 'pass'
    filled = copy.deepcopy(office)
    filled.pop('holes')
    report = compare_partitions([office], [filled], tolerance_m=0)
    assert report['status'] == 'severe'
    assert report['matches'][0]['iou'] == pytest.approx(.9)


def test_open_cap_keeps_hole_and_omits_only_the_selected_display_surface():
    source = build(expand_parametric_proposal(plan()))
    cap = next(b for b in source['boundaries'] if b['space_id']=='F2:office' and b['geometry_type']=='ceiling')
    result = apply_source_enclosure(source, dict(schema_version='source_enclosure_input_v1',
        base_source_model_sha256=source['source_model_sha256'], spaces=[], boundaries=[dict(
            boundary_id=cap['id'], condition='open', scope='whole', source_refs=['test:open-terrace'],
            assumptions=[], evidence_kind='observed')]))
    adopted = next(b for b in result['boundaries'] if b['id']==cap['id'])
    assert adopted['enclosure_regions'][0]['holes'] == cap['holes']
    display = source_view_geometry(result)
    assert not display['display_surface_parts'].get(cap['id'])
    core_cap = next(b for b in result['boundaries'] if b['space_id']=='C0:core0' and b['geometry_type']=='ceiling')
    assert display['display_surface_parts'][core_cap['id']]


def test_wall_convention_preserves_and_changes_inner_ring_without_filling_it():
    from src.agent.judge.conventions import _variant
    office = dict(polygon=[[0,0],[12,0],[12,10],[0,10]], holes=[[[3,3],[5,3],[5,6],[3,6]]])
    changed, used = _variant(office, [dict(axis=0,span_m=[3,6],reference_m=3,target_m=3.1)])
    assert used and len(changed.interiors)==1
    assert changed.area == pytest.approx(114.3)
    assert not changed.covers(Point(4,4))
