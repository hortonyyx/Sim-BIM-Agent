import copy

import numpy as np
import pytest
from PIL import Image, ImageDraw

from src.agent.geometry.plan_partition import compile_plan_partition
from src.agent.geometry.plan_wall_metadata_checks import append_wall_metadata_checks
from src.agent.geometry.plan_window_span_checks import append_window_span_checks


def scene(*, declared=(270, 470), spans=((300, 500),), transpose=False,
          mirror=False, reverse=False, along_mpp=.01, background='white'):
    plan = {'reading_type': 'plan', 'floor_id': 'F1', 'basis': 'Synthetic frame test',
            'x_anchors': [[100, 0], [900, 800 * along_mpp]], 'y_anchors': [[100, 8], [900, 0]],
            'footprint_pixels': [[100, 100], [900, 100], [900, 900], [100, 900]],
            'partitions': [], 'space_seeds': [], 'assumptions': [], 'unresolved': [],
            'openings': [{'id': 'window', 'kind': 'window', 'p1': [declared[0], 100], 'p2': [declared[1], 100]}],
            'wall_metadata': [{'target': 'footprint:*', 'thickness_m': .24, 'evidence_status': 'inferred',
                               'basis': 'Synthetic batch', 'source_refs': ['fixture']}]}
    image = Image.new('RGB', (1000, 1000), background)
    color = 'black' if background == 'white' else 'white'
    draw = ImageDraw.Draw(image)
    for y in (100, 124):
        draw.line([(100, y), (900, y)], fill=color)
    for start, end in spans:
        for y in (108, 116):
            draw.line([(start, y), (end, y)], fill=color)
    if transpose:
        image = image.transpose(Image.Transpose.TRANSPOSE)
        for point in plan['footprint_pixels']:
            point.reverse()
        for opening in plan['openings']:
            opening['p1'].reverse(); opening['p2'].reverse()
        plan['x_anchors'], plan['y_anchors'] = plan['y_anchors'], plan['x_anchors']
    if mirror:
        for axis in ('x', 'y'):
            for anchor in plan[axis + '_anchors']:
                anchor[1] *= -1
    if reverse:
        plan['footprint_pixels'].reverse()
        plan['openings'][0]['p1'], plan['openings'][0]['p2'] = plan['openings'][0]['p2'], plan['openings'][0]['p1']
    return plan, image


def integrated(plan, image):
    _, metadata = compile_plan_partition(plan, image_size=image.size, image_name='plan.png')
    report = {'status': 'reported', 'items': [], 'counts': {}, 'total': 0,
              'coverage': {'not_checked': ['exterior walls/windows']}, 'scope': 'Base only'}
    before, pixels = copy.deepcopy((plan, metadata, report)), image.tobytes()
    result = append_wall_metadata_checks(report, image, plan, metadata)
    assert (plan, metadata, report) == before and image.tobytes() == pixels
    return result


@pytest.mark.parametrize('transpose,mirror,reverse,along_mpp,background', [
    (False, False, False, .01, 'white'), (True, False, True, .01, 'black'),
    (False, True, True, .02, 'black'), (True, True, False, .02, 'white')])
def test_locates_span_error_from_observed_faces_without_geometry_edits(
        transpose, mirror, reverse, along_mpp, background):
    plan, image = scene(declared=(280, 480), transpose=transpose, mirror=mirror, reverse=reverse,
                        along_mpp=along_mpp, background=background)
    # Deliberately wrong inferred thickness must not move the observed sample band.
    plan['wall_metadata'][0]['thickness_m'] = .48
    result = integrated(plan, image)
    issues = [r for r in result['items'] if r['type'] == 'window_span_ink_mismatch']
    assert len(issues) == 1
    issue = issues[0]
    assert issue['opening_id'] == 'window' and issue['span_axis'] == ('y' if transpose else 'x')
    assert issue['adopted_span_px'] == [280, 480] and issue['ink_span_px'] == [300, 500]
    assert issue['endpoint_residual_m'] == pytest.approx([20 * along_mpp] * 2)
    assert result['coverage']['exterior_window_spans']['compared_spans'] == 1


@pytest.mark.parametrize('declared', [(300, 500), (295, 505), (285, 485)])
def test_raster_and_small_adopted_offsets_are_not_extra_work(declared):
    result = integrated(*scene(declared=declared))
    assert not any(r['type'] == 'window_span_ink_mismatch' for r in result['items'])
    assert result['coverage']['exterior_window_spans']['compared_spans'] == 1


@pytest.mark.parametrize('variant', ['blank', 'single', 'thick_stroke', 'filled', 'multiple', 'clipped', 'unknown_faces'])
def test_ambiguous_or_unavailable_strokes_are_not_negative_evidence(variant):
    plan, image = scene()
    draw = ImageDraw.Draw(image)
    draw.rectangle([101, 101, 899, 123], fill='white')
    if variant == 'single':
        draw.line([(300, 108), (500, 108)], fill='black')
    elif variant == 'thick_stroke':
        draw.line([(300, 108), (500, 108)], fill='black', width=4)
    elif variant == 'filled':
        draw.rectangle([300, 102, 500, 122], fill='black')
    elif variant == 'multiple':
        plan['openings'][0].update(p1=[300, 100], p2=[500, 100])
        for start, end in ((280, 360), (440, 520)):
            for y in (108, 116):
                draw.line([(start, y), (end, y)], fill='black')
    elif variant in {'clipped', 'unknown_faces'}:
        for y in (108, 116):
            draw.line([(210 if variant == 'clipped' else 300, y), (500, y)], fill='black')
        if variant == 'unknown_faces':
            draw.line([(100, 140), (900, 140)], fill='black')
    result = integrated(plan, image)
    assert not any(r['type'] == 'window_span_ink_mismatch' for r in result['items'])
    coverage = result['coverage']['exterior_window_spans']
    assert coverage['compared_spans'] == 0 and coverage['unavailable_or_ambiguous'] == 1


def test_a_one_pixel_bridge_cannot_absorb_a_neighboring_aperture():
    plan = {'x_anchors': [[0, 0], [200, 4]], 'y_anchors': [[0, 0], [100, 2]],
            'footprint_pixels': [[0, 20], [200, 20], [200, 90], [0, 90]],
            'openings': [{'id': 'main', 'kind': 'window', 'p1': [60, 20], 'p2': [100, 20]},
                         {'id': 'next', 'kind': 'window', 'p1': [102, 20], 'p2': [122, 20]}]}
    mask = np.zeros((101, 201), dtype=bool)
    for y in (23, 27):
        mask[y, 60:101] = True
        mask[y, 102:123] = True
    result = append_window_span_checks({'items': [], 'coverage': {}}, mask, plan, {'footprint:0': [20, 30]})
    assert result['items'] == []
    assert result['coverage']['exterior_window_spans']['compared_spans'] == 0
