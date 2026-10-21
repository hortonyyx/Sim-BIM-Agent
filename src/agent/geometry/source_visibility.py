"""Orthographic visibility of saved source boundaries, independent of input type.

This is a geometric occlusion test, not a material renderer or mesh fit. Glazing
is treated as its host envelope so rear facades do not appear through windows.
Explicit open enclosure regions and open apertures are excluded; unknown regions
and doors with unknown/closed state stay occluders.
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw


def project_points(points, camera, span, size):
    width, height = size
    delta = np.asarray(points, dtype=float) - camera['target']
    return np.column_stack(((delta @ camera['screen_right'] / span['width'] + .5) * width - .5,
                            (.5 - delta @ camera['screen_up'] / span['height']) * height - .5))


def camera_vectors(camera):
    vectors = [np.asarray(camera[key], dtype=float)
               for key in ('eye', 'screen_right', 'screen_up', 'forward_from_eye')]
    if any(v.shape != (3,) or not np.isfinite(v).all() for v in vectors):
        raise ValueError('visible overlay requires finite camera vectors')
    eye, right, up, forward = vectors
    basis = np.array([right, up, forward])
    if not np.allclose(basis @ basis.T, np.eye(3), atol=1e-6):
        raise ValueError('visible overlay requires an orthonormal camera')
    return eye, right, up, forward


def polygon_normal(vertices):
    vertices = np.asarray(vertices, dtype=float)
    normal = np.cross(vertices, np.roll(vertices, -1, axis=0)).sum(axis=0)
    length = np.linalg.norm(normal)
    if length <= 1e-12:
        raise ValueError('source visibility requires a nondegenerate boundary')
    return normal / length


def source_depth_buffer(boundaries, transform, camera, span, size, *, open_apertures=None):
    """Use all source surfaces as occluders, even when display is floor-scoped."""
    width, height = size
    eye, right, up, forward = camera_vectors(camera)
    depth = np.full((height, width), np.inf)
    for boundary in boundaries:
        world = transform(boundary['vertices'])
        normal = polygon_normal(world)
        denominator = normal @ forward
        if abs(denominator) < 1e-8:
            continue
        screen = project_points(world, camera, span, size)
        lo = np.maximum(np.floor(screen.min(axis=0)).astype(int) - 1, 0)
        hi = np.minimum(np.ceil(screen.max(axis=0)).astype(int) + 1, [width-1, height-1])
        if np.any(lo > hi):
            continue
        mask_image = Image.new('1', tuple((hi-lo+1).tolist()))
        draw = ImageDraw.Draw(mask_image)
        draw.polygon([tuple(p) for p in screen-lo], fill=1)
        for vertices in boundary.get('holes', []):
            hole = project_points(transform(vertices), camera, span, size)
            draw.polygon([tuple(p) for p in hole-lo], fill=0)
        for region in boundary.get('enclosure_regions', []):
            if region.get('condition') == 'open':
                hole = project_points(transform(region['vertices']), camera, span, size)
                draw.polygon([tuple(p) for p in hole-lo], fill=0)
        for vertices in (open_apertures or {}).get(boundary.get('id'), []):
            hole = project_points(transform(vertices), camera, span, size)
            draw.polygon([tuple(p) for p in hole-lo], fill=0)
        rows, cols = np.mgrid[lo[1]:hi[1]+1, lo[0]:hi[0]+1]
        plane = (np.asarray(camera['target']) +
                 ((cols+.5)/width-.5)[..., None]*span['width']*right +
                 (.5-(rows+.5)/height)[..., None]*span['height']*up)
        distances = (world[0]-plane) @ normal / denominator
        z = (plane-eye) @ forward + distances
        valid = np.asarray(mask_image, dtype=bool) & (z > 0)
        patch = depth[lo[1]:hi[1]+1, lo[0]:hi[0]+1]
        np.minimum(patch, np.where(valid, z, np.inf), out=patch)
    return depth


def visible_edge_segments(world, screen, depth, camera, span):
    """Sample clipped edge pixels and retain only source-visible segments.

    Tolerance is raster footprint only, never a fidelity threshold. Return
    counts for auditing; an entirely occluded object remains in the record.
    """
    eye, right, up, forward = camera_vectors(camera)
    height, width = depth.shape
    tolerance = 2 * max(span['width']/width, span['height']/height)
    segments, tested, visible = [], 0, 0
    for index, start in enumerate(screen):
        end = screen[(index+1) % len(screen)]
        # Clip the segment to the viewport before sampling, to bound work for
        # close-ups where source vertices may project millions of pixels away.
        delta = end-start
        low, high = 0., 1.
        for axis, limit in ((0, width-1), (1, height-1)):
            if abs(delta[axis]) < 1e-12:
                if not 0 <= start[axis] <= limit:
                    high = -1
                    break
            else:
                a, b = sorted(((0-start[axis])/delta[axis], (limit-start[axis])/delta[axis]))
                low, high = max(low, a), min(high, b)
        if low > high:
            continue
        samples = max(2, int(np.ceil(np.linalg.norm(delta)*(high-low)*2))+1)
        t = np.linspace(low, high, samples)
        pixels = start + t[:, None]*delta
        positions = world[index] + t[:, None]*(world[(index+1) % len(world)]-world[index])
        rounded = np.rint(pixels).astype(int)
        z = (positions-eye) @ forward
        accepted = (z > 0) & (z <= depth[rounded[:, 1], rounded[:, 0]]+tolerance)
        tested += len(t)
        visible += int(accepted.sum())
        begin = None
        for i, ok in enumerate(accepted):
            if ok and begin is None:
                begin = i
            if begin is not None and (not ok or i == len(accepted)-1):
                stop = i if ok else i-1
                segments.append(pixels[begin:stop+1].tolist())
                begin = None
    return segments, {'tested_edge_samples': tested, 'visible_edge_samples': visible,
                      'occluded_edge_samples': tested-visible,
                      'raster_depth_tolerance_m': tolerance}
