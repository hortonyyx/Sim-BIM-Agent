"""A shared corner is geometric incidence, not an equality of vertex lists."""
import asyncio
import copy
import hashlib
import json
import math

import pytest
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from src.agent.geometry.architectural_regularization import (
    ArchitecturalRegularizationError, DEFAULT_ARCHITECTURAL_POLICY as POLICY,
    regularize_architectural_plan,
)
from src.agent.geometry.parametric_proposal import expand_parametric_proposal
from src.agent.execution.source_proposal import export_source_proposal


def junction_plan():
    a, b, c, d = [-2.5, -2.684], [3.5, -2.684], [3.5, 3.316], [-2.5, 3.316]
    j = [.524, .34]
    top, right = [j[0], c[1]], [c[0], j[1]]
    return dict(schema_version='source_polygon_v1', templates={'main': dict(
        footprint=[a, b, c, d], spaces=[dict(id=str(i), role='office', polygon=points,
            source_refs=['synthetic declared partition'])
            for i, points in enumerate([[j, top, d, a], [j, a, b, right], [j, right, c, top]])])},
        instances=[dict(id='F1', template='main', z=0, height=3)], assumptions=[], unresolved=[])


def framed(plan, origin=(.524, .34), yaw=0):
    plan['modelling'] = {'frames': [dict(id='junction', origin=list(origin), yaw_degrees=yaw,
                                       templates=list(plan['templates']))]}
    return plan


def missing(template):
    fp = template['footprint']
    expected = Polygon(fp if isinstance(fp, list) else fp['vertices'],
                       [] if isinstance(fp, list) else fp.get('holes', []))
    actual = unary_union([Polygon(s['polygon'], s.get('holes', [])) for s in template['spaces']])
    return expected.difference(actual.buffer(1e-7)).area


def test_complete_partition_is_rejected_before_shared_corners_split():
    plan = junction_plan()
    before = copy.deepcopy(plan)
    assert missing(plan['templates']['main']) == 0
    with pytest.raises(ArchitecturalRegularizationError) as caught:
        regularize_architectural_plan(plan, policy=POLICY)
    audit = caught.value.audit
    rows = audit['rejections']
    assert len(rows) == 2 and {r['code'] for r in rows} == {'shared_junction_split'}
    node = next(r for r in rows if r['observed_point'] == [.524, .34])
    assert node['max_adopted_separation_m'] > .08
    assert len(node['vertices']) == 3 and node['scope'] == ['projection']
    assert {v['normalized_vertex_pointer'] for v in node['vertices']} == {
        '/templates/main/spaces/0/polygon/0', '/templates/main/spaces/1/polygon/0',
        '/templates/main/spaces/2/polygon/0'}
    assert node['frame']['origin'] == [0, 0] and node['numerical_tolerance_m'] == 1e-8
    assert 'adopted_sha256' not in audit and plan == before
    assert {r['blocked_by'] for r in audit['not_assessed']} == {'shared_junctions'}


def test_explicit_common_frame_repairs_partition_and_keeps_opening_host(tmp_path):
    plan = framed(junction_plan())
    plan['templates']['main']['doors'] = [dict(id='d', space='0', other_space='2',
        p1=[.524, 1.34], p2=[.524, 2.34], z=[0, 2.1], source_refs=['synthetic door'])]
    adopted, _ = regularize_architectural_plan(plan, policy=POLICY)
    assert missing(adopted['templates']['main']) == 0
    assert [s['polygon'][0] for s in adopted['templates']['main']['spaces']] == [[.524, .34]] * 3
    door = adopted['templates']['main']['doors'][0]
    assert door['space'] == '0' and door['other_space'] == '2'
    assert door['p1'] == pytest.approx([.524, 1.34])
    result = export_source_proposal(expand_parametric_proposal(adopted), tmp_path / 'source')
    assert result['source_geometry_ready'], result


def test_collinear_subdivision_may_slide_without_changing_partition(tmp_path):
    plan = junction_plan()
    t = plan['templates']['main']
    t['footprint'] = [[0, 0], [4, 0], [4, 4], [0, 4]]
    polygons = [[[0, 0], [1.03, 0], [1.03, 2], [1.03, 4], [0, 4]],
                [[1.03, 2], [4, 4], [1.03, 4]], [[1.03, 0], [4, 0], [4, 4], [1.03, 2]]]
    for s, polygon in zip(t['spaces'], polygons):
        s['polygon'] = polygon
    framed(plan, [4, 4])
    adopted, _ = regularize_architectural_plan(plan, policy=POLICY)
    cells = adopted['templates']['main']['spaces']
    assert cells[0]['polygon'][2] != cells[1]['polygon'][0]
    assert missing(adopted['templates']['main']) == 0
    assert export_source_proposal(expand_parametric_proposal(adopted), tmp_path / 'source')['source_geometry_ready']


def test_source_tolerated_numerical_wedge_is_not_a_new_rejection(tmp_path):
    plan = junction_plan()
    a, b, c, d = [-2.5, -2.5], [3.5, -2.5], [3.5, 3.5], [-2.5, 3.5]
    j = [.5 + 1e-7, .5]
    t = plan['templates']['main']
    t['footprint'] = [a, b, c, d]
    for space, ring in zip(t['spaces'], [[j, [j[0], 3.5], d, a],
                           [j, a, b, [3.5, .5]], [j, [3.5, .5], c, [j[0], 3.5]]]):
        space['polygon'] = ring
    adopted, _ = regularize_architectural_plan(plan, policy=POLICY)
    points = [s['polygon'][0] for s in adopted['templates']['main']['spaces']]
    assert max(math.dist(a, b) for a in points for b in points) > 1e-8
    assert missing(adopted['templates']['main']) == 0
    assert export_source_proposal(expand_parametric_proposal(adopted), tmp_path / 'source')['source_geometry_ready']


def test_rotated_frame_reports_source_coordinates_and_accepts_explicit_shared_origin():
    plan = junction_plan()
    yaw = 31
    angle = math.radians(yaw)
    origin = [100, -40]
    def world(p):
        x, y = p
        return [origin[0] + math.cos(angle)*x - math.sin(angle)*y,
                origin[1] + math.sin(angle)*x + math.cos(angle)*y]
    t = plan['templates']['main']
    t['footprint'] = list(map(world, t['footprint']))
    for space in t['spaces']:
        space['polygon'] = list(map(world, space['polygon']))
    framed(plan, origin, yaw)
    with pytest.raises(ArchitecturalRegularizationError) as caught:
        regularize_architectural_plan(plan, policy=POLICY)
    assert any(math.dist(row['observed_point'], world([.524, .34])) < 1e-8
               for row in caught.value.audit['rejections'])
    framed(plan, world([.524, .34]), yaw)
    adopted, _ = regularize_architectural_plan(plan, policy=POLICY)
    assert missing(adopted['templates']['main']) == 0


def test_hole_rings_and_same_frame_different_templates_do_not_false_merge():
    from tests.test_architectural_regularization import single, rectangle
    raw = single(rectangle(.024, .024, 8, 8))
    t = raw['templates']['main']
    hole = rectangle(2.024, 2.024, 2, 2)
    t['footprint'] = {'vertices': t['footprint'], 'holes': [copy.deepcopy(hole)]}
    t['spaces'][0]['holes'] = [copy.deepcopy(hole)]
    raw['templates']['other'] = copy.deepcopy(t)
    raw['instances'].append(dict(id='F2', template='other', z=6, height=3))
    adopted, _ = regularize_architectural_plan(raw, policy=POLICY)
    for t in adopted['templates'].values():
        assert t['footprint']['holes'] == t['spaces'][0]['holes']
        assert missing(t) == 0


def test_hole_corner_split_is_reported_with_the_hole_pointer():
    outer = [[-3, -3], [4, -3], [4, 4], [-3, 4]]
    hole = [[.524, .34], [1.524, 2.34], [-1.476, 2.34]]
    shape = Polygon(outer, [hole])
    parts = [shape.intersection(box(-10, -10, .524, 10)),
             shape.intersection(box(.524, -10, 10, 10))]
    raw = junction_plan()
    raw['templates']['main'] = dict(footprint=dict(vertices=outer, holes=[hole]),
        spaces=[dict(id=str(i), role='office', polygon=[list(p) for p in part.exterior.coords][:-1])
                for i, part in enumerate(parts)])
    assert missing(raw['templates']['main']) == 0
    with pytest.raises(ArchitecturalRegularizationError) as caught:
        regularize_architectural_plan(raw, policy=POLICY)
    node, = caught.value.audit['rejections']
    assert node['code'] == 'shared_junction_split' and node['observed_point'] == [.524, .34]
    assert '/templates/main/footprint/holes/0/0' in {v['normalized_vertex_pointer'] for v in node['vertices']}


def test_different_height_sections_do_not_bind_same_xy_corners():
    rectangle = [[.524, .34], [6.524, .34], [6.524, 6.34], [.524, 6.34]]
    triangle = [[.524, .34], [3.524, 1.34], [1.524, 3.34]]
    raw = junction_plan()
    raw['instances'][0]['height'] = 6
    raw['templates']['main'] = dict(footprint=copy.deepcopy(rectangle), spaces=[dict(
        id='stepped', role='office', polygon=copy.deepcopy(rectangle), vertical_sections=[
            dict(z=[0, 3], polygon=rectangle), dict(z=[3, 6], polygon=triangle)])])
    adopted, _ = regularize_architectural_plan(raw, policy=POLICY)
    lower, upper = adopted['templates']['main']['spaces'][0]['vertical_sections']
    assert lower['polygon'][0] != upper['polygon'][0]
    assert lower['z'] == [0, 3] and upper['z'] == [3, 6]


def test_same_frame_different_templates_keep_independent_corners():
    polygons = [[[.524, .34], [6.524, .34], [6.524, 6.34], [.524, 6.34]],
                [[.524, .34], [3.524, 1.34], [1.524, 3.34]]]
    raw = junction_plan()
    raw['templates'] = {str(i): dict(footprint=copy.deepcopy(p),
        spaces=[dict(id='room', role='office', polygon=p)]) for i, p in enumerate(polygons)}
    raw['instances'] = [dict(id=f'F{i}', template=str(i), z=i * 10, height=3) for i in range(2)]
    adopted, _ = regularize_architectural_plan(raw, policy=POLICY)
    assert adopted['templates']['0']['spaces'][0]['polygon'][0] != adopted['templates']['1']['spaces'][0]['polygon'][0]


def test_unused_template_does_not_add_a_source_rejection(tmp_path):
    from tests.test_architectural_regularization import single, rectangle
    raw = junction_plan()
    raw['templates']['used'] = single(rectangle(10, 10, 4, 4))['templates']['main']
    raw['instances'][0]['template'] = 'used'
    adopted, _ = regularize_architectural_plan(raw, policy=POLICY)
    assert missing(adopted['templates']['main']) > 0  # Kept declaration is not compiled into this source.
    assert export_source_proposal(expand_parametric_proposal(adopted), tmp_path / 'source')['source_geometry_ready']


def test_real_gap_is_still_rejected_without_welding(tmp_path):
    from tests.test_architectural_regularization import shared_plan
    raw = shared_plan()
    raw['templates']['main'].pop('doors')
    raw['templates']['main']['spaces'][1]['rect'][0] += .02
    before = copy.deepcopy(raw)
    with pytest.raises(ArchitecturalRegularizationError) as caught:
        regularize_architectural_plan(raw, policy=POLICY)
    assert 'volume_contact_changed' in {r['code'] for r in caught.value.audit['rejections']}
    assert 'shared_junction_split' not in {r['code'] for r in caught.value.audit['rejections']}
    assert raw == before


def _result(value):
    if isinstance(value, list):
        return json.loads(value[0].text)
    if isinstance(value, tuple):
        return value[1]
    assert not value.isError
    return value.structuredContent


def test_formal_mcp_failure_preserves_input_and_has_visible_repair_coordinates(tmp_path):
    from tests.test_bim_agent_tools import _run_with_one_image, _parametric_file_hashes
    from scripts.tool_scripts.run_bim_agent import build_tool_server
    run = _run_with_one_image(tmp_path)
    manifest = json.loads((run / 'inputs.json').read_bytes())
    manifest['architectural_modelling_policy'] = POLICY
    (run / 'inputs.json').write_text(json.dumps(manifest), encoding='utf-8')
    before = _parametric_file_hashes(run)
    server = build_tool_server(run)
    value = junction_plan()
    result = _result(asyncio.run(server.call_tool('build_parametric_bim', {'plan_json': json.dumps(value)})))
    assert result['error_stage'] == 'parametric_expansion' and result['saved_candidate'] is None
    assert not result['save_effects']['geometry_applied']
    assert not list(run.glob('candidate_*')) and not list(run.rglob('*.adopted.json'))
    after = _parametric_file_hashes(run)
    assert all(after[name] == sha for name, sha in before.items())
    assert json.loads((run / result['draft']).read_bytes()) == value
    feedback = result['error_details']['shared_junctions']
    assert feedback['junction_count'] == feedback['shown_junction_count'] == 2
    node = next(r for r in feedback['junctions'] if r['observed_point'] == [.524, .34])
    assert len(node['vertices']) == 3 and len({tuple(v['after']) for v in node['vertices']}) == 3
    args = dict(feedback['read_all']['arguments'], json_pointer='/rejections/0')
    recovered = _result(asyncio.run(server.call_tool('read_candidate_items', args)))
    assert recovered['value']['code'] == 'shared_junction_split'


def test_large_failure_keeps_bounded_representatives_and_exact_full_report(tmp_path):
    from scripts.tool_scripts.bim_agent_parametric import _shared_junction_feedback
    from scripts.tool_scripts.bim_agent_replies import compact_reply, read_report
    from types import SimpleNamespace
    with pytest.raises(ArchitecturalRegularizationError) as caught:
        regularize_architectural_plan(junction_plan(), policy=POLICY)
    audit = copy.deepcopy(caught.value.audit)
    node = audit['rejections'][0]
    audit['rejections'] = [{**copy.deepcopy(node), 'template': f'T{i % 20}'} for i in range(200)]
    feedback = _shared_junction_feedback(SimpleNamespace(run=tmp_path), audit)
    assert len(json.dumps(feedback).encode()) < 10000
    assert feedback['junction_count'] == 200 and feedback['omitted_junction_count'] > 0
    assert feedback['template_count'] == 20 and all(r['junction_count'] == 10 for r in feedback['templates'])
    assert len({r['template'] for r in feedback['junctions']}) == len(feedback['junctions'])
    projected = compact_reply(tmp_path, 'build_parametric_bim', {
        'parametric': {'regularization_failure': audit}, 'error_details': {'shared_junctions': feedback}})
    assert projected['error_details']['shared_junctions'] == feedback
    pieces, offset = [], 0
    while True:
        page = read_report(tmp_path, feedback['details_file'], offset, 12000)
        pieces.append(page['text'])
        if page['next_offset'] is None:
            break
        offset = page['next_offset']
    assert json.loads(''.join(pieces)) == audit
