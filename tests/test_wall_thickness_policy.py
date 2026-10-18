"""Current Lite BIM contract: thickness batches, anchored merging and paired views."""
import copy

import pytest
from PIL import Image

from src.agent.correction.parse import ensure_corrected_geometry
from src.agent.geometry.lite_bim_regularization import regularize_lite_plan, compiled_grid_report
from src.agent.geometry.plan_partition import compile_plan_partition
from src.agent.geometry.plan_regularization import regularize_plan_stack
from src.agent.geometry.reader_wall_metadata import (
    require_wall_thickness, wall_reading_input, bind_wall_readings, WALL_THICKNESS_PRESETS_CM,
)
from src.agent.geometry.source_bim import build_source_bim
from src.agent.geometry.wall_merge_policy import within_wall_tolerance
from src.agent.runtime_roles.plan_reading import _views, build_reading
from tests.test_plan_regularization import _plan, _door, _stack_items, IMAGE_SIZE


def thickness(plan, interior=.24):
    plan['wall_metadata'] = [
        dict(target=target, thickness_m=value, evidence_status='inferred',
             basis='Synthetic construction estimate', source_refs=['fixture'])
        for target, value in [('footprint:*', .37), ('partition:*', interior)]]
    return plan


def test_batch_override_centimetres_and_required_final_coverage(tmp_path):
    plan = thickness(_plan([('A', [[40, 0], [40, 100]])]))
    plan['wall_metadata'].append(dict(plan['wall_metadata'][1], target='partition:A', thickness_m=.125))
    result = require_wall_thickness(plan)
    assert [r['thickness_m'] for r in result['wall_metadata']] == [.37] * 4 + [.13]
    assert result['wall_metadata'][-1]['reference_side'] == 'centerline'
    assert {r['reference_side'] for r in result['wall_metadata'][:4]} == {'negative_face', 'positive_face'}
    assert 62 in WALL_THICKNESS_PRESETS_CM and 200 in WALL_THICKNESS_PRESETS_CM
    plan['wall_metadata'][-1]['thickness_m'] = 2.17
    assert require_wall_thickness(plan)['wall_metadata'][-1]['thickness_m'] == 2.17
    plan.pop('wall_metadata')
    with pytest.raises(ValueError, match='Every wall'):
        build_reading(plan, workspace=tmp_path, image_name='missing.png', number=1)


def test_same_floor_merges_opposite_faces_then_snaps_anchor_and_door():
    plan = thickness(_plan([('A', [[40.3, 0], [40.3, 100]]), ('B', [[44.3, 0], [44.3, 100]])],
                           openings=[_door('D', 44.3)]))
    plan['regularization_inputs'] = {'line_references': [
        dict(partition_id='A', basis='dimension', source_refs=['fixture'])]}
    plan['wall_metadata'] += [
        dict(plan['wall_metadata'][1], target='partition:A', reference_side='negative_face'),
        dict(plan['wall_metadata'][1], target='partition:B', reference_side='positive_face')]
    original = copy.deepcopy(plan)
    result, report = regularize_lite_plan(plan, image_size=IMAGE_SIZE)
    assert original == plan
    assert len(result['partitions']) == 1
    assert result['partitions'][0]['points'] == [[40., 0.], [40., 100.]]
    assert result['openings'][0]['p1'][0] == 40
    assert len(result['wall_metadata']) == 5
    assert report['wall_merging']['semantic_mapping']['space_count_after'] == 2
    assert report['wall_merging']['summary']['maximum_movement_m'] == pytest.approx(.4)
    again, replay = regularize_lite_plan(result, image_size=IMAGE_SIZE)
    assert again == result and replay == report


def test_connected_different_thickness_segments_survive_source_binding():
    plan = thickness(_plan([('A', [[40, 0], [40, 50]]), ('B', [[44, 50], [44, 100]])]))
    plan['wall_metadata'].append(dict(plan['wall_metadata'][1], target='partition:B', thickness_m=.12))
    adopted, _ = regularize_lite_plan(plan, image_size=IMAGE_SIZE)
    assert {p['points'][0][0] for p in adopted['partitions']} == {40}
    assert {r['thickness_m'] for r in adopted['wall_metadata'] if r['target'].startswith('partition:')} == {.12, .24}
    proposal, mapping = compile_plan_partition(adopted, image_size=IMAGE_SIZE, image_name='plan.png')
    readings = [wall_reading_input(adopted, mapping)]
    source = build_source_bim(ensure_corrected_geometry(proposal['geometry']),
                             capability_profile='orthogonal_polygon', wall_readings=readings)
    patch = bind_wall_readings(source, readings)
    assert {r['thickness_m'] for r in patch['component_attributes']} >= {.12, .24}


def test_cross_floor_moves_xy_and_openings_retains_floor_thickness_and_z():
    lower = thickness(_plan([('A', [[40.3, 0], [40.3, 100]])], openings=[_door('D', 40.3)]))
    upper = thickness(_plan([('A', [[44.3, 0], [44.3, 100]])], openings=[_door('D', 44.3)]), .12)
    items = _stack_items(lower, upper)
    original = copy.deepcopy(items)
    result, report = regularize_plan_stack(items, grid_step_m=.1)
    assert items == original and report['status'] == 'pass'
    for i, row in enumerate(result):
        assert row['plan']['partitions'][0]['points'][0][0] == 40
        assert row['plan']['openings'][0]['p1'][0] == 40
        assert row['z_floor'] == 3 * i
        assert row['plan']['wall_metadata'][-1]['thickness_m'] == [.24, .12][i]
        assert compiled_grid_report(row['proposal'])['status'] == 'pass'


def test_tolerance_is_inclusive_and_group_does_not_chain_outwards():
    def segment(x, origins=None):
        return dict(coordinate_m=x, thickness_m=.24, wall_members=[
            dict(coordinate_m=p, thickness_m=.24) for p in origins or [x]])
    assert within_wall_tolerance(segment(0), segment(.48))
    assert not within_wall_tolerance(segment(0), segment(.49))
    assert not within_wall_tolerance(segment(.4, [0, .4]), segment(.8))


def test_clean_and_overlay_are_identical_over_a_white_source():
    plan = require_wall_thickness(thickness(_plan([('A', [[40, 0], [40, 100]])])))
    _, mapping = compile_plan_partition(plan, image_size=IMAGE_SIZE, image_name='plan.png')
    overlay, clean = _views(plan, mapping, Image.new('RGB', IMAGE_SIZE, 'white'))
    assert overlay.tobytes() == clean.tobytes()


def test_partial_overlap_preserves_remainders_and_their_thickness():
    plan = thickness(_plan([
        ('A', [[40, 30], [40, 70]]), ('B', [[44, 0], [44, 100]]),
        ('top', [[0, 30], [44, 30]]), ('bottom', [[0, 70], [44, 70]])]))
    plan['regularization_inputs'] = {'line_references': [
        dict(partition_id='A', basis='dimension', source_refs=['fixture'])]}
    plan['wall_metadata'].append(dict(plan['wall_metadata'][1], target='partition:B', thickness_m=.12))
    adopted, _ = regularize_lite_plan(plan, image_size=IMAGE_SIZE)
    rows = {r['target']: r for r in adopted['wall_metadata']}
    assert set(rows) == {*(f'footprint:{i}' for i in range(4)),
                         *('partition:' + p['id'] for p in adopted['partitions'])}
    assert rows['partition:A']['thickness_m'] == .24
    assert all(row['thickness_m'] == .12 for key, row in rows.items() if key.startswith('partition:B'))


def test_cross_floor_exterior_uses_outer_face_and_preserves_each_thickness():
    lower = thickness(_plan([]))
    upper = thickness(_plan([], footprint=[[4, 0], [100, 0], [100, 100], [4, 100]]))
    upper['wall_metadata'][0]['thickness_m'] = .24
    result, _ = regularize_plan_stack(_stack_items(lower, upper), grid_step_m=.1)
    assert result[0]['plan']['footprint_pixels'] == result[1]['plan']['footprint_pixels']
    assert {r['thickness_m'] for r in result[0]['plan']['wall_metadata']} == {.37}
    assert {r['thickness_m'] for r in result[1]['plan']['wall_metadata']} == {.24}


def test_known_strip_seed_is_removed_with_explicit_audit():
    plan = thickness(_plan([('A', [[40, 0], [40, 100]]), ('B', [[44, 0], [44, 100]])],
                           seeds=[dict(id='gap', point=[42, 50])]))
    adopted, report = regularize_lite_plan(plan, image_size=IMAGE_SIZE)
    assert not adopted['space_seeds']
    assert report['wall_merging']['semantic_mapping']['removed_space_seeds']


@pytest.mark.parametrize('positions', [(40, 40), (40.4, 45.2)])
def test_cross_floor_replay_and_original_threshold_before_grid(positions):
    plans = [thickness(_plan([('A', [[x, 0], [x, 100]])])) for x in positions]
    result, _ = regularize_plan_stack(_stack_items(*plans), grid_step_m=.1)
    assert [r['plan']['partitions'][0]['points'][0][0] for r in result] == [40, 40]


def test_junction_cleanup_inherits_every_generated_wall_record():
    plan = thickness(_plan([('A', [[40, 30], [40, 70]]), ('B', [[40, 0], [40, 100]])]))
    adopted, _ = regularize_lite_plan(plan, image_size=IMAGE_SIZE)
    complete = require_wall_thickness(adopted)
    assert len(complete['wall_metadata']) == 4 + len(complete['partitions'])


def test_door_across_thickness_change_keeps_area_connectivity_and_logical_id():
    plan = thickness(_plan([('A', [[40, 0], [40, 50]]), ('B', [[40, 50], [40, 100]])],
                           openings=[_door('D', 40, 45, 55)]))
    plan['wall_metadata'].append(dict(plan['wall_metadata'][1], target='partition:B', thickness_m=.12))
    adopted, _ = regularize_lite_plan(plan, image_size=IMAGE_SIZE)
    proposal, mapping = compile_plan_partition(adopted, image_size=IMAGE_SIZE, image_name='plan.png')
    readings = [wall_reading_input(adopted, mapping)]
    source = build_source_bim(ensure_corrected_geometry(proposal['geometry']),
                             capability_profile='orthogonal_polygon', wall_readings=readings)
    assert len(source['openings']) == 2
    assert source['opening_segment_groups']['D'] == ['D', 'D::thickness-2']
    assert len({tuple(o['space_ids']) for o in source['openings']}) == 1
    assert sum(abs(o['vertices'][1][1] - o['vertices'][0][1]) for o in source['openings']) == pytest.approx(1)
    assert bind_wall_readings(source, readings)['component_attributes']
    from src.agent.geometry.opening_review import opening_inventory, review_openings
    from src.agent.execution.ep_branch import derive_ep_geometry
    assert opening_inventory(source)['counts']['door'] == 1
    from tests.test_opening_review import _images
    review = dict(floor_id='F1', kind='door', image='plan.png', coverage='complete', marks=[
        dict(mark_id='D', box=[1, 2, 10, 12], opening_ids=['D'],
             space_ids=source['openings'][0]['space_ids'], basis='visible')])
    assert not review_openings(source, review, _images())['findings']
    derive_ep_geometry(source, {s['id']: s['id'] for s in source['spaces']}, opening_policy={
        'D': dict(state='closed', construction='Default_Door', reason='Synthetic closed door')})


def test_window_across_perimeter_thickness_change_keeps_complete_aperture():
    plan = thickness(_plan([], footprint=[[0, 0], [50, 0], [100, 0], [100, 100], [0, 100]],
                           openings=[dict(id='W', kind='window', p1=[45, 0], p2=[55, 0],
                                          z=[1, 2], source_refs=['fixture'])]))
    plan['wall_metadata'].append(dict(plan['wall_metadata'][0], target='footprint:1', thickness_m=.24))
    adopted, _ = regularize_lite_plan(plan, image_size=IMAGE_SIZE)
    proposal, mapping = compile_plan_partition(adopted, image_size=IMAGE_SIZE, image_name='plan.png')
    readings = [wall_reading_input(adopted, mapping)]
    source = build_source_bim(ensure_corrected_geometry(proposal['geometry']),
                             capability_profile='orthogonal_polygon', wall_readings=readings)
    assert len(source['openings']) == 2
    assert source['opening_segment_groups']['W'] == ['W', 'W::thickness-2']
    from src.agent.geometry.opening_review import opening_inventory, facade_inventory, review_openings
    from tests.test_opening_review import _images
    assert opening_inventory(source)['counts']['window'] == 1
    assert facade_inventory(source)['opening_classifications']['W']['facade'] == 'North'
    assert not review_openings(source, dict(floor_id='F1', kind='window', facade='North',
        image='plan.png', coverage='complete', marks=[dict(mark_id='W', box=[1, 2, 10, 12],
        opening_ids=['W'], space_ids=source['openings'][0]['space_ids'], basis='visible')]), _images())['findings']
    assert sum(max(v[0] for v in o['vertices']) - min(v[0] for v in o['vertices'])
               for o in source['openings']) == pytest.approx(1)
    assert bind_wall_readings(source, readings)['component_attributes']


def test_authored_relocation_invalidates_old_cross_floor_membership():
    plans = [thickness(_plan([('A', [[x, 0], [x, 100]])])) for x in (40, 44)]
    first, _ = regularize_plan_stack(_stack_items(*plans), grid_step_m=.1)
    first[1]['plan']['partitions'][0]['points'] = [[60, 0], [60, 100]]
    second, _ = regularize_plan_stack(first, grid_step_m=.1)
    assert [r['plan']['partitions'][0]['points'][0][0] for r in second] == [40, 60]
