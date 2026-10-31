"""Measure ink along declared partition paths without accepting or editing walls."""
from __future__ import annotations

import math
import numpy as np
from PIL import Image, ImageDraw
from shapely.geometry import LineString, Polygon

from src.agent.geometry.plan_partition import _point, _orthogonal


def _runs(flags, offset):
    start = None
    result = []
    for i, flag in enumerate([*flags, False]):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            result.append([start + offset, i - 1 + offset])
            start = None
    return result


def measure_plan_wall_support(image, plan, *, rgb, tolerance=70, radius_pixels=6,
                              minimum_ink_pixels=1, spaces=None):
    """Sample every integer along-path position across a caller-selected strip.

    A declared collinear aperture is reported separately, never certified by
    masking it out. Nearby openings cannot excuse a gap on another wall plane.
    Adjacency, when available, describes the submitted compilation, not truth.
    """
    if (len(rgb) != 3 or any(isinstance(v, bool) or not isinstance(v, (int, float))
                           or not math.isfinite(v) or not 0 <= v <= 255 for v in rgb)
            or not isinstance(tolerance, (int, float)) or not math.isfinite(tolerance)
            or not 0 <= tolerance <= 442):
        raise ValueError('RGB in 0..255 and finite tolerance in 0..442 required')
    if (isinstance(radius_pixels, bool) or not isinstance(radius_pixels, int)
            or not 1 <= radius_pixels <= 32):
        raise ValueError('radius_pixels must be an integer in 1..32')
    if (isinstance(minimum_ink_pixels, bool) or not isinstance(minimum_ink_pixels, int)
            or not 1 <= minimum_ink_pixels <= 2 * radius_pixels + 1):
        raise ValueError('minimum_ink_pixels must fit the sampling strip')
    original = image.convert('RGB')
    mask = np.linalg.norm(np.asarray(original).astype(float)-np.asarray(rgb), axis=2) <= tolerance
    point = lambda p: _point(p, path='wall support point', width=image.width, height=image.height)
    openings = []
    for opening in plan.get('openings', []):
        p, q = point(opening['p1']), point(opening['p2'])
        _orthogonal([p, q], path='opening', closed=False)
        openings.append((opening['id'], p, q))
    polygons = [(s['space_id'], Polygon(s['pixel_polygon'])) for s in spaces or []]
    segments = []
    for partition in plan['partitions']:
        points = [point(p) for p in partition['points']]
        if len(points) < 2:
            raise ValueError('partition needs at least two points')
        _orthogonal(points, path='partition', closed=False)
        for index, (p, q) in enumerate(zip(points, points[1:]), 1):
            along = 0 if p[1] == q[1] else 1
            cross = 1-along
            lo, hi = math.ceil(min(p[along], q[along])), math.floor(max(p[along], q[along]))
            cross_limit = image.height if along == 0 else image.width
            c0 = max(0, math.ceil(p[cross]-radius_pixels))
            c1 = min(cross_limit-1, math.floor(p[cross]+radius_pixels))
            strip = mask[c0:c1+1, lo:hi+1] if along == 0 else mask[lo:hi+1, c0:c1+1]
            counts = strip.sum(axis=0 if along == 0 else 1)
            ticks = np.arange(lo, hi+1)
            supported = counts >= minimum_ink_pixels
            aperture = np.zeros(len(ticks), dtype=bool)
            declared = []
            for oid, a, b in openings:
                if abs(a[cross]-p[cross]) > 1e-7 or abs(b[cross]-p[cross]) > 1e-7:
                    continue
                start, end = max(lo, min(a[along], b[along])), min(hi, max(a[along], b[along]))
                if start > end:
                    continue
                aperture |= (ticks >= start) & (ticks <= end)
                declared.append(dict(id=oid, clipped_span_pixels=[start, end], verified=False))
            gaps = []
            for start, end in _runs(~supported & ~aperture, lo):
                a, b = list(p), list(p)
                a[along], b[along] = start, end
                line = LineString([a, b])
                gaps.append(dict(span_pixels=[start, end], sample_count=end-start+1,
                    points=[a, b], proposed_adjacent_space_ids=[sid for sid, poly in polygons
                        if poly.boundary.intersection(line).length > 0]))
            segments.append(dict(partition_id=partition['id'], segment_index=index,
                points=[list(p), list(q)], axis='xy'[along], cross_pixel=p[cross],
                strip_cross_pixels=[c0, c1], sample_span_pixels=[lo, hi],
                sample_count=len(ticks), matching_ink_counts=counts.tolist(),
                supported_intervals_pixels=_runs(supported, lo),
                declared_openings=declared, declared_opening_sample_count=int(aperture.sum()),
                unsupported_outside_declared_openings=gaps,
                unsupported_sample_count=sum(g['sample_count'] for g in gaps)))
    ranked = sorted([dict(partition_id=s['partition_id'], segment_index=s['segment_index'], **gap)
                     for s in segments for gap in s['unsupported_outside_declared_openings']],
                    key=lambda g: g['sample_count'], reverse=True)
    marked = original.copy()
    draw = ImageDraw.Draw(marked)
    for s in segments:
        draw.line([tuple(p) for p in s['points']], fill=(255, 70, 200), width=1)
    for i, gap in enumerate(ranked):
        gap['id'] = f'G{i+1:02d}'
        p, q = gap['points']
        draw.line([tuple(p), tuple(q)], fill=(255, 160, 0), width=3)
        if i < 12:
            draw.text(((p[0]+q[0])/2+5, (p[1]+q[1])/2), gap['id'],
                      fill='yellow', stroke_width=1, stroke_fill='black')
    combined = Image.new('RGB', (original.width*2+8, original.height), 'white')
    combined.paste(original, (0, 0)); combined.paste(marked, (original.width+8, 0))
    report = dict(schema_version='plan_wall_support_v1', rgb=rgb, tolerance=tolerance,
        radius_pixels=radius_pixels, minimum_ink_pixels=minimum_ink_pixels,
        segments=segments, review_intervals=ranked,
        proposed_space_adjacency_available=bool(polygons), drawing_fidelity='not_evaluated',
        coordinate_note='All intervals are inclusive ORIGINAL pixel samples; no gap bridging or inferred continuation.',
        evidence_note='Colour support is not wall identity. Absence may reflect colour, offset, opening or drawing style. '
                      'Declared apertures remain unverified; excluded intervals are not certified doors. '
                      'Adjacent spaces come from the submitted plan only. Reinspect clean originals, both ends and both spaces; '
                      'do not delete walls or add apertures just to clear gaps.',
        panel_note='Left: untouched original. Right: submitted partition paths in magenta; unsupported samples outside '
                   'declared apertures in orange. G labels order by sample count, not semantic severity.')
    return combined, report
