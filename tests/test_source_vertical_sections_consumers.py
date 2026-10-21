"""Stepped spaces stay one identity without pretending their projection is volume."""
from copy import deepcopy

from PIL import Image
import pytest

from src.agent.execution.ep_branch import derive_ep_geometry
from src.agent.geometry.source_floor_selection import select_source_floor
from src.agent.geometry.source_image_overlay import render_source_overlay
from src.agent.geometry.source_model import _digest
from src.agent.geometry.source_plan_view import render_source_plan
from src.agent.geometry.source_space_relations import review_space_relations, revalidate_saved_relation_review
from src.agent.geometry.source_vertical_continuity import review_vertical_continuity
from src.agent.judge.source_partition import compare_partitions


def rectangle(x1):
    return [[0, 0], [x1, 0], [x1, 4], [0, 4]]


def source():
    value = dict(schema_version='source_bim_v2', coordinate_system=dict(units='m', up_axis='Z'),
        floors=[dict(id='F', name='F', z_floor=0, height=4, footprint=rectangle(6))],
        spaces=[dict(id='office', floor_id='F', role='office', polygon=rectangle(6), z_floor=0, height=4,
            vertical_sections=[dict(z=[0, 2], polygon=rectangle(6)), dict(z=[2, 4], polygon=rectangle(4))])],
        openings=[], connections=[], boundaries=[], boundary_relations=[], opening_hosts={},
        validation=dict(status='pass'))
    return rehash(value)


def rehash(value):
    value['source_model_sha256'] = _digest({k: v for k, v in value.items() if k != 'source_model_sha256'})
    return value


def review(value, z=None):
    observation = dict(id='pair', points=[[10, 30], [50, 30]], expected='same_space',
                       evidence='Two observed locations in a synthetic reference.')
    if z is not None:
        observation['z_m'] = z
    return review_space_relations(value, floor_id='F', image_size=[61, 41],
        x_anchors=[[0, 0], [60, 6]], y_anchors=[[40, 0], [0, 4]], observations=[observation])


def test_relations_require_height_when_point_ownership_changes():
    value = source()
    without_height = review(value)
    assert without_height['observations'][0]['actual_relation'] == 'indeterminate'
    assert without_height['observations'][0]['points'][1]['status'] == 'height_dependent'
    lower = review(value, 1)
    assert lower['observations'][0]['actual_relation'] == 'same_space'
    assert [p['space_id'] for p in lower['observations'][0]['points']] == ['office', 'office']
    upper = review(value, 3)
    assert upper['observations'][0]['points'][1]['status'] == 'outside_modelled_spaces'
    assert upper['observations'][0]['actual_relation'] == 'indeterminate'
    with pytest.raises(ValueError, match='within the requested floor'):
        review(value, 5)


def test_notes_revalidation_preserves_explicit_relation_sampling_height():
    parent = source()
    saved = review(parent, 1)
    saved.update(image='plan.png', image_sha256='a' * 64, image_size=[61, 41])
    current = rehash({**deepcopy(parent), 'assumptions': ['notes changed']})
    images = {'plan.png': dict(sha256='a' * 64, size=[61, 41])}
    calibration = dict(image='plan.png', floor_id='F', image_sha256='a' * 64,
                       x_anchors=[[0, 0], [60, 6]], y_anchors=[[40, 0], [0, 4]])
    actual = revalidate_saved_relation_review(parent, current, saved, images, calibration=calibration)
    assert actual['submitted_observations'][0]['z_m'] == 1
    assert actual['observations'][0]['actual_relation'] == 'same_space'


def test_spanning_floor_views_use_present_sections_and_keep_one_zone():
    value = source()
    value['spaces'][0]['floor_id'] = 'VOLUME'
    value['floors'] = [dict(id='F', name='Upper', z_floor=2, height=2,
                            footprint=rectangle(4), spanning_space_ids=['office']),
                       dict(id='VOLUME', name='Volume', z_floor=0, height=4, footprint=rectangle(6))]
    rehash(value)
    before = deepcopy(value)
    _, selected = select_source_floor(value, 'F')
    assert [r['id'] for r in selected] == ['office']
    assert max(p[0] for p in selected[0]['polygon']) == 4
    _, plan = render_source_plan(value, 'F')
    assert plan['space_ids'] == ['office']
    assert plan['world_bounds_m']['x'] == [0, 4]
    _, overlay = render_source_overlay(value, Image.new('RGB', (61, 41), 'white'), floor_id='F',
        x_anchors=[[0, 0], [60, 6]], y_anchors=[[40, 0], [0, 4]], basis='Synthetic coordinate frame')
    row = overlay['projected_spaces'][0]
    assert row['id'] == 'office' and row['projection_z_m'] == [2, 4]
    assert max(p[0] for p in row['pixel_polygon']) == 40
    assert value == before


def test_partition_evaluator_cannot_score_a_flat_projection_as_a_stepped_match():
    stepped = source()['spaces']
    flat = deepcopy(stepped)
    flat[0].pop('vertical_sections')
    for first, second in [(stepped, stepped), (stepped, flat), (flat, stepped)]:
        report = compare_partitions(first, second)
        assert report['status'] == 'not_evaluated'
        assert not report['matches'] and not report['evaluated']
        assert report['findings'][0]['code'] == 'vertical_sections_not_supported'


def test_ep_rejects_stepped_geometry_before_constructing_a_prism():
    with pytest.raises(ValueError, match='vertical sections; no prism approximation'):
        derive_ep_geometry(source(), {'office': 'Office'})


def test_vertical_continuity_uses_actual_interface_not_overall_projection():
    value = source()
    value['spaces'][0]['role'] = 'shaft'
    value['spaces'].append(dict(id='upper', floor_id='U', role='shaft', polygon=rectangle(4), z_floor=4, height=3))
    value['boundaries'] = [
        dict(id='lower-top', space_id='office', geometry_type='ceiling', kind='physical',
             vertices=[[*p, 4] for p in rectangle(4)]),
        dict(id='upper-base', space_id='upper', geometry_type='floor', kind='physical',
             vertices=[[*p, 4] for p in rectangle(4)])]
    result = review_vertical_continuity(value)
    assert result['status'] == 'decision_required'
    interface = result['unresolved_stacks'][0]['interfaces'][0]
    assert interface['boundary_state'] == 'physical_caps'
    assert result['coverage']['checked_aligned_interface_count'] == 1
