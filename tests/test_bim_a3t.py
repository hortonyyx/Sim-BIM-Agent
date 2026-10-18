"""A3-T: actionable compact replies preserve real evidence and geometry."""
import asyncio
import copy
import hashlib
import json

import pytest

from scripts.tool_scripts.run_bim_agent import Toolkit
from scripts.tool_scripts.bim_agent_replies import compact_reply, read_report, summarize_reply
from src.agent.geometry.plan_input import plan_error_hint
from src.agent.geometry.plan_partition import compile_plan_partition
from tests.test_bim_agent_plan_partition import example
from tests.test_bim_agent_tools import _json_result, _run_with_one_image, _server_session
from tests.test_plan_partition import _plan


def test_aliases_keep_geometry_and_original_bytes_with_explicit_receipt(tmp_path):
    run = _run_with_one_image(tmp_path)
    original = example()
    alias = copy.deepcopy(original)
    for row in alias['partitions']:
        row['pixels'] = row.pop('points')
    for row in alias.get('space_seeds', []):
        row['pixels'] = row.pop('point')
    canonical, _ = compile_plan_partition(original, image_size=(12, 8), image_name='plan.png')
    compiled, meta = compile_plan_partition(alias, image_size=(12, 8), image_name='plan.png')
    assert compiled == canonical and meta['field_aliases']
    raw = json.dumps(alias)
    result = Toolkit(run).build_plan('plan.png', raw)
    assert result['source_geometry_ready']
    record = result['plan_input']
    assert record['field_aliases'] == meta['field_aliases']
    assert (run / record['submitted_plan_file']).read_text() == raw
    saved = json.loads((run / record['plan_file']).read_text())
    assert saved.pop('regularization')['changes'] == []  # Q1: the audit travels with the effective plan
    assert saved == original


def test_conflicting_alias_and_unknown_field_do_not_guess(tmp_path):
    run = _run_with_one_image(tmp_path)
    plan = example()
    plan['partitions'][0]['pixels'] = [[1, 1], [2, 1]]
    result = Toolkit(run).build_plan('plan.png', json.dumps(plan))
    assert not result['source_geometry_ready']
    assert 'both pixels and points' in result['error']
    assert result['repair_hint']['path'] == 'plan.partitions[0]'
    assert not list(run.glob('candidate_*'))
    plan = example()
    plan['openings'][0]['pixels'] = plan['openings'][0].pop('p1')
    result = Toolkit(run).build_plan('plan.png', json.dumps(plan))
    assert not result['source_geometry_ready']
    assert result['repair_hint']['path'] == 'plan.openings[0]'
    assert set(result['repair_hint']['example']) >= {'id', 'kind', 'p1', 'p2', 'z', 'source_refs'}


def test_disconnected_endpoint_reports_nearest_declared_line_without_snapping():
    plan = _plan()
    plan['partitions'][1]['points'][-1] = [49, 50]
    before = copy.deepcopy(plan)
    with pytest.raises(ValueError) as exc:
        compile_plan_partition(plan, image_size=(100, 100), image_name='plan.png')
    message = str(exc.value)
    assert "'nearest_line': 'P_vertical'" in message
    assert "'distance_pixels': 1.0" in message
    assert 'no automatic snapping' in message
    hint = plan_error_hint(plan, message)
    assert hint['path'] == 'plan.partitions[1].points[1]'
    assert hint['junction_repairs'] == [{
        'path': 'plan.partitions[1].points[1]',
        'partition_id': 'P_left_branch',
        'original_endpoint_pixel': [49, 50],
        'failed_endpoint_pixel': [49.0, 50.0],
        'target': {
            'path': 'plan.partitions[0].points',
            'partition_id': 'P_vertical',
            'segment_index': 0,
            'segment_original_pixels': [[50, 10], [50, 50]],
            'line_id': 'P_vertical',
            'reported_point_after_alignment_original_pixels': [50.0, 50.0],
            'point_original_pixels': [50.0, 50.0],
        },
        'set_to_original_pixels': [50.0, 50.0],
        'requires_synchronous_target_endpoint_move': False,
        'distance_pixels': 1.0,
        'original_distance_m': 0.1,
    }]
    assert plan == before


def test_disconnected_endpoint_hint_maps_an_aligned_target_back_to_the_original_line():
    plan = _plan()
    plan['partitions'][1]['points'][-1] = [49, 50]
    message = (
        "polygonize produced dangles: []; nearest disconnected endpoints: "
        "[{'partition_id': 'P_left_branch', 'endpoint_pixel': [49.0, 50.0], "
        "'nearest_line': 'P_vertical', 'distance_pixels': 2.0, "
        "'nearest_point_pixel': [51.0, 50.0]}]; aligned draft")
    repair = plan_error_hint(plan, message)['junction_repairs'][0]
    assert repair['target']['reported_point_after_alignment_original_pixels'] == [51.0, 50.0]
    assert repair['target']['segment_original_pixels'] == [[50, 10], [50, 50]]
    assert repair['set_to_original_pixels'] == [50, 50]
    assert repair['requires_synchronous_target_endpoint_move'] is False


@pytest.mark.parametrize('scale', [.01, .1])
@pytest.mark.parametrize('gap_m', [.299, .3, .301])
@pytest.mark.parametrize('transpose,reverse,mirror', [(False, False, False), (True, True, True)])
def test_junction_hint_bounds_original_metres_not_aligned_pixel_distance(scale, gap_m, transpose, reverse, mirror):
    plan = _plan()
    plan['x_anchors'] = [[0, 0], [99, 99 * scale * (-1 if mirror else 1)]]
    plan['y_anchors'] = [[0, 0], [99, 99 * scale * 2]]
    source = [50 - gap_m / scale, 50]
    plan['partitions'][1]['points'][-1] = source
    if transpose:
        for row in plan['partitions']:
            row['points'] = [point[::-1] for point in row['points']]
        source = source[::-1]
        plan['x_anchors'], plan['y_anchors'] = plan['y_anchors'], plan['x_anchors']
    if reverse:
        for row in plan['partitions']:
            row['points'].reverse()
    before = copy.deepcopy(plan)
    row = dict(partition_id='P_left_branch', endpoint_pixel=source,
               nearest_line='P_vertical', nearest_point_pixel=[50, 50], distance_pixels=.01)
    repair = plan_error_hint(plan, 'nearest disconnected endpoints: ' + repr([row]))['junction_repairs'][0]
    assert repair['original_distance_m'] == pytest.approx(gap_m)
    assert repair['original_endpoint_pixel'] == source
    if gap_m < .3:
        assert repair['set_to_original_pixels'] == [50, 50]
    else:
        assert 'set_to_original_pixels' not in repair
        assert 'requires_synchronous_target_endpoint_move' not in repair
    assert plan == before


@pytest.mark.parametrize('anchors', [None, [], [[0, 0], [0, 10]], [[0, 1], [99, 1]],
                                     [[0, 0], [99, float('nan')]], [[False, 0], [99, 10]]])
def test_junction_hint_without_valid_calibration_never_commands_endpoint_moves(anchors):
    plan = _plan()
    plan['x_anchors'] = anchors
    plan['partitions'][1]['points'][-1] = [49, 50]
    row = dict(partition_id='P_left_branch', endpoint_pixel=[49, 50],
               nearest_line='P_vertical', nearest_point_pixel=[50, 50], distance_pixels=1)
    repair = plan_error_hint(plan, 'nearest disconnected endpoints: ' + repr([row]))['junction_repairs'][0]
    assert repair['original_distance_m'] is None
    assert 'set_to_original_pixels' not in repair
    assert 'requires_synchronous_target_endpoint_move' not in repair


@pytest.mark.parametrize('target_end_y,local', [(49, True), (45, False)])
def test_synchronous_junction_hint_also_requires_a_local_original_target(target_end_y, local):
    plan = _plan()
    plan['partitions'][0]['points'] = [[50, 10], [50, target_end_y]]
    plan['partitions'][1]['points'][-1] = [49, 50]
    row = dict(partition_id='P_left_branch', endpoint_pixel=[49, 50],
               nearest_line='P_vertical', nearest_point_pixel=[50, 50], distance_pixels=1)
    repair = plan_error_hint(plan, 'nearest disconnected endpoints: ' + repr([row]))['junction_repairs'][0]
    assert 'set_to_original_pixels' not in repair
    assert ('requires_synchronous_target_endpoint_move' in repair) == local


def test_missing_wall_hint_preserves_diagnostic_without_shrinking_a_room():
    from tests.test_plan_regularization import _plan as regularization_plan, IMAGE_SIZE
    plan = regularization_plan([('spine', [[40, 0], [40, 70]]), ('divider', [[40, 50], [100, 50]])])
    before = copy.deepcopy(plan)
    with pytest.raises(ValueError) as exc:
        compile_plan_partition(plan, image_size=IMAGE_SIZE, image_name='plan.png')
    repair = plan_error_hint(plan, str(exc.value))['junction_repairs'][0]
    assert repair['original_endpoint_pixel'] == [40, 70]
    assert repair['original_distance_m'] == 2
    assert 'set_to_original_pixels' not in repair
    assert 'requires_synchronous_target_endpoint_move' not in repair
    # Even when alignment made the reported gap tiny, the original edit is 2 m.
    row = dict(partition_id='spine', endpoint_pixel=[40, 50.1], nearest_line='divider',
               nearest_point_pixel=[40, 50], distance_pixels=.1)
    repair = plan_error_hint(plan, 'nearest disconnected endpoints: ' + repr([row]))['junction_repairs'][0]
    assert repair['original_distance_m'] == 2
    assert 'set_to_original_pixels' not in repair
    assert plan == before
    plan['partitions'].append(dict(id='missing-bottom', points=[[40, 70], [100, 70]],
                                   source_refs=['synthetic observed divider']))
    _, mapping = compile_plan_partition(plan, image_size=IMAGE_SIZE, image_name='plan.png')
    assert len(mapping['space_mapping']) == 3


def test_same_space_seed_hint_points_to_the_original_region_without_inventing_a_wall():
    plan = _plan()
    plan['space_seeds'] = [
        {'id': 'west-a', 'point': [20, 20]},
        {'id': 'west-b', 'point': [30, 40]},
    ]
    hint = plan_error_hint(plan, 'space seeds west-a and west-b occupy the same space')
    assert [row['path'] for row in hint['space_seeds']] == [
        'plan.space_seeds[0]', 'plan.space_seeds[1]']
    assert hint['missing_wall_check'] == {
        'between_seed_points_original_pixels': [[20, 20], [30, 40]],
        'midpoint_original_pixels': [25.0, 30.0],
        'search_box_original_pixels': [20, 20, 30, 40],
    }
    assert 'only if that wall is visible' in hint['note']
    assert 'does not create a wall' in hint['note']


def test_opening_host_and_height_failures_name_the_declared_object_without_deleting_topology():
    plan = _plan()
    opening = plan['openings'][0]
    host = plan_error_hint(
        plan,
        f"opening {opening['id']} at pixels {opening['p1']} -> {opening['p2']} "
        "requires one exterior or two interior full-boundary hosts; found []",
    )
    assert host['path'] == 'plan.openings[0]'
    assert 'correct its points rather than deleting it' in host['note']

    plan['z_floor'] = 4.0
    plan['ceiling_height'] = 3.6
    opening['z'] = [1.0, 2.4]
    height = plan_error_hint(plan, f"opening {opening['id']}.z [1.0, 2.4] is outside floor vertical bounds [4.0, 7.6]")
    assert height == {
        'path': 'plan.openings[0].z', 'current': [1.0, 2.4],
        'allowed_floor_bounds_m': [4.0, 7.6],
        'note': ('Set both absolute opening heights within the floor bounds while preserving the '
                 'observed or stated sill/head relationship; record any assumed height.'),
    }


def test_compact_reply_keeps_usable_ids_geometry_images_and_complete_readback(tmp_path):
    async def scenario():
        run = _run_with_one_image(tmp_path)
        async with _server_session(run, readonly=False) as session:
            response = await session.call_tool('build_plan_bim', {'image':'plan.png','plan_json':json.dumps(example())})
            result = _json_result(response)
            assert len([b for b in response.content if b.type == 'image']) == 2
            raw = (run / result['details_file']).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == result['details_sha256']
            full = json.loads(raw)
            heights = result['height_coverage']
            assert full['height_coverage']['summary'] == heights['summary']
            assert all(row in full['height_coverage']['openings'] for row in heights['openings'])
            assert len(heights['openings']) + heights['unbound_without_other_issues'] == len(full['height_coverage']['openings'])
            assert full['source_validation'] == result['source_validation']
            assert full['plan_input'] == result['plan_input']
            assert full['plan_compilation']['space_count'] == result['plan_compilation']['space_count']
            for key, identity, fields in (
                ('partition_mapping', 'partition_id', ('pixel_points', 'world_points_m')),
                ('opening_hosts', 'opening_id', ('p1_pixel', 'p2_pixel', 'width_m')),
                ('space_mapping', 'space_id', ('seed', 'source_refs')),
            ):
                selected = result['plan_compilation'][key]
                actual = {row[identity]: row for row in
                          (dict(zip(selected['columns'], values)) for values in selected['rows'])}
                for item in full['plan_compilation'][key]:
                    for field in fields:
                        if field in item:
                            assert actual[item[identity]][field] == item[field]
            returned_ids = [r[0] for f in result['opening_inventory']['floors'] for r in f['rows']]
            assert sorted(returned_ids) == sorted(o['id'] for f in full['opening_inventory']['floors'] for o in f['openings'])
            offset = 0
            pieces = []
            while True:
                page = _json_result(await session.call_tool('read_candidate_items', {
                    'candidate':'','collection':'report','report_file':result['details_file'], 'offset':offset,'limit':12000}))
                pieces.append(page['text'])
                if page['next_offset'] is None:break
                offset = page['next_offset']
            assert ''.join(pieces).encode() == raw
            for path in ['inputs.json', '../private.json', str(tmp_path / 'outside.json')]:
                failure = await session.call_tool('read_candidate_items', {
                    'candidate':'','collection':'report','report_file':path})
                assert failure.isError
    asyncio.run(scenario())


def test_report_detects_tampering_and_does_not_mutate_full_result(tmp_path):
    raw = {'candidate':'candidate_01', 'provenance':{'plan_input':{'id':'draft_001'}},
           'plan_input':{'id':'draft_001'},'error':'keep this error'}
    before = copy.deepcopy(raw)
    compact = compact_reply(tmp_path, 'build_bim', raw)
    assert raw == before and compact['error'] == raw['error']
    (tmp_path / compact['details_file']).write_text('{}')
    with pytest.raises(ValueError, match='hash mismatch'):
        read_report(tmp_path, compact['details_file'], 0, 50)


def test_only_identical_submitted_operations_are_deduplicated():
    submitted = [{'op': 'update_opening', 'id': 'D1', 'changes': {'z': [0, 2.1]}}]
    full = {'claim_application': {'submitted_operations': submitted,
                                 'resolved_operations': copy.deepcopy(submitted),
                                 'changes': [{'kind': 'door', 'id': 'D1'}]}}
    receipt = summarize_reply(full)['claim_application']
    assert receipt['submitted_operations_equal_resolved']
    assert 'resolved_operations' not in receipt and 'submitted_operations' not in receipt
    assert receipt['changes'] == full['claim_application']['changes']
    full['claim_application']['resolved_operations'][0]['changes']['z'] = [0, 2.2]
    assert summarize_reply(full)['claim_application'] == full['claim_application']


def test_fixed_parameter_values_are_in_catalog(tmp_path):
    async def scenario():
        run = _run_with_one_image(tmp_path)
        async with _server_session(run, readonly=False) as session:
            tools = {t.name:t.inputSchema for t in (await session.list_tools()).tools}
            expected = {('pixel_profile','axis'):['x','y'], ('view_pixel_profile','axis'):['x','y'],
                        ('map_dimension_chain','unit'):['mm','m'], ('map_dimension_chain','direction'):[-1,1],
                        ('view_elevation_candidate','facade'):['North','South','East','West'],
                        ('record_work_review','decision'):['continue','stop']}
            for (tool,key), values in expected.items():
                assert tools[tool]['properties'][key]['enum'] == values
            assert 'report' in tools['read_candidate_items']['properties']['collection']['enum']
            assert 'plan_partition' in tools['get_bim_reference']['properties']['topic']['enum']
    asyncio.run(scenario())


def test_empty_profile_explains_color_and_support_adjustments(tmp_path):
    run = _run_with_one_image(tmp_path)
    result = Toolkit(run).profile('plan.png', [0,0,12,8], 'x', [255,0,0], 0)
    diagnostic = result['empty_filter_diagnostics']
    assert 'tolerance' in diagnostic['next_action'] and 'RGB' in diagnostic['next_action']
    import numpy as np
    from scripts.tool_scripts.run_bim_agent import _empty_profile_diagnostics
    diagnostic = _empty_profile_diagnostics(np.full((5,5,3),255), [255,255,255], np.array([1,1,1,1,1]), 4, 5)
    assert 'min_fraction' in diagnostic['next_action'] and 'cross_axis_profile' in diagnostic['next_action']
