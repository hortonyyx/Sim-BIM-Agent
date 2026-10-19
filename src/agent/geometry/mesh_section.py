"""Actual horizontal triangle/plane intersections of the admitted original mesh.

Segments are geometric evidence, not inferred walls, rooms or a closed footprint.
Coincident triangles are reported separately because a coplanar intersection is
an area, not a uniquely defined section line.
"""
from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw

from src.agent.geometry.mesh_observation import _finite_vector, _validated_yaw


_EPS = 1e-8  # Numerical intersection tolerance in metres, not a quality threshold.


def _clip_segment(points, bounds):
    if bounds is None:
        return points
    start, end = points
    delta = end - start
    lo, hi = 0., 1.
    for axis in (0, 1):
        if abs(delta[axis]) <= _EPS:
            if start[axis] < bounds[0, axis] or start[axis] > bounds[1, axis]:
                return None
        else:
            a, b = sorted((bounds[:, axis] - start[axis]) / delta[axis])
            lo, hi = max(lo, a), min(hi, b)
    if hi < lo or np.linalg.norm(delta * (hi-lo)) <= _EPS:
        return None
    return np.array([start + lo*delta, start + hi*delta])


def section_mesh(mesh, *, height_m, yaw_degrees=0, translation_m=(0, 0, 0), bounds_xy=None):
    """Cut all transformed triangles, then optionally clip the resulting segments.

    The requested frame is Rz(yaw)*original_Zup + translation. No face-centroid
    filter is used. The operation returns exact per-triangle segments, without
    fitting lines, joining gaps, removing small architectural details or tracing
    room loops. Duplicate on-plane segments retain every supporting face ID.
    """
    if isinstance(height_m, bool) or not isinstance(height_m, (int, float)) or not math.isfinite(height_m):
        raise ValueError('section height_m must be a finite number')
    yaw = _validated_yaw(yaw_degrees)
    translation = _finite_vector('translation_m', translation_m, 3)
    bounds = None if bounds_xy is None else np.asarray(bounds_xy, dtype=float)
    if bounds is not None and (bounds.shape != (2, 2) or not np.isfinite(bounds).all()
                               or np.any(bounds[1] <= bounds[0])):
        raise ValueError('bounds_xy must be finite [[xmin,ymin],[xmax,ymax]] with positive spans')
    parts, _ = mesh._operation_parts(yaw, None)
    lines, coincident, touched = {}, [], []
    counts = {'triangles_tested': 0, 'plane_crossing_triangles': 0, 'coplanar_triangles': 0,
              'point_touch_triangles': 0, 'degenerate_triangles': 0, 'segments_clipped_out': 0}
    for part, vertices, face_ids in parts:
        vertices = vertices + translation
        triangles = vertices[part.faces[face_ids]]
        distances = triangles[:, :, 2] - height_m
        selected = np.flatnonzero((distances.min(axis=1) <= _EPS) & (distances.max(axis=1) >= -_EPS))
        counts['triangles_tested'] += len(face_ids)
        for index in selected:
            triangle, signed = triangles[index], distances[index]
            global_id = int(part.face_offset + face_ids[index])
            if np.linalg.norm(np.cross(triangle[1]-triangle[0], triangle[2]-triangle[0])) <= _EPS**2:
                counts['degenerate_triangles'] += 1
                continue
            on = np.abs(signed) <= _EPS
            if on.all():
                counts['coplanar_triangles'] += 1
                coincident.append({'face_id': global_id, 'vertices_xyz': triangle.tolist()})
                continue
            points = [point.copy() for point in triangle[on]]
            for a, b in ((0, 1), (1, 2), (2, 0)):
                if signed[a] * signed[b] < 0 and not on[a] and not on[b]:
                    points.append(triangle[a] + signed[a] / (signed[a]-signed[b]) * (triangle[b]-triangle[a]))
            unique = []
            for point in points:
                point[2] = height_m
                if not any(np.linalg.norm(point-old) <= _EPS for old in unique):
                    unique.append(point)
            if len(unique) < 2:
                counts['point_touch_triangles'] += 1
                if unique:
                    touched.append({'face_id': global_id, 'xyz': unique[0].tolist()})
                continue
            counts['plane_crossing_triangles'] += 1
            clipped = _clip_segment(np.array(unique[:2]), bounds)
            if clipped is None:
                counts['segments_clipped_out'] += 1
                continue
            endpoints = sorted(clipped.tolist())
            key = tuple(round(value, 8) for point in endpoints for value in point)
            if key not in lines:
                lines[key] = {'endpoints_xyz': endpoints, 'face_ids': [],
                              'length_m': float(np.linalg.norm(clipped[1]-clipped[0]))}
            lines[key]['face_ids'].append(global_id)
    segments = list(lines.values())
    counts['segments'] = len(segments)
    return {'mesh_sha256': mesh.mesh_sha256, 'height_m': float(height_m),
            'coordinate_transform': {'yaw_degrees': yaw, 'translation_m': translation.tolist(),
                'formula': 'section XYZ = Rz(yaw)*[GLB.x,-GLB.z,GLB.y] + translation; after scene transforms'},
            'bounds_xy': None if bounds is None else bounds.tolist(),
            'selection_method': 'triangle/plane intersection, then exact XY segment clipping',
            'numerical_tolerance_m': _EPS, 'counts': counts, 'segments': segments,
            'coplanar_triangles': coincident, 'point_touches': touched,
            'total_segment_length_m': sum(row['length_m'] for row in segments),
            'fidelity': 'not_evaluated', 'limitations': [
                'Original mesh surface intersections; not inferred rooms, enclosure, wall thickness or a footprint.',
                'No gap closing or semantic filtering. No hit may mean missing mesh, an opening, or the chosen height.',
                'Coplanar triangles have area intersections and are excluded from the line-length total; use another height if needed.',
                'XY bounds clip line segments only; coplanar/point-touch diagnostics describe the full cutting plane.']}


def render_section(section, *, max_side=1000):
    """Render the actual cut in an aspect-preserving, explicitly mapped XY view."""
    if type(max_side) is not int or not 100 <= max_side <= 1600:
        raise ValueError('section image max_side must be an integer from 100 to 1600')
    points = [point[:2] for segment in section['segments'] for point in segment['endpoints_xyz']]
    if section['bounds_xy'] is not None:
        lo, hi = np.asarray(section['bounds_xy'])
    elif points:
        lo, hi = np.min(points, axis=0), np.max(points, axis=0)
    else:
        lo, hi = np.array([-1., -1.]), np.array([1., 1.])
    centre = (lo+hi)/2
    span = np.maximum(hi-lo, max(float(np.max(hi-lo)), 1.) * .08)
    span *= 1.12
    scale = max_side / max(span)
    size = np.maximum(1, np.ceil(span*scale).astype(int))
    span = size / scale
    lo, hi = centre-span/2, centre+span/2
    image = Image.new('RGB', tuple(size), '#f7f9fc')
    draw = ImageDraw.Draw(image)
    def pixel(point):
        return ((point[0]-lo[0])*scale-.5, (hi[1]-point[1])*scale-.5)
    for row in section['segments']:
        draw.line([pixel(point) for point in row['endpoints_xyz']], fill='#174c80', width=2)
    draw.rectangle((0, 0, min(size[0], 780), 33), fill='white')
    draw.text((5, 3), f'ORIGINAL MESH CUT z={section["height_m"]:.6g} m; +X right, +Y up', fill='black')
    draw.text((5, 17), f'{len(section["segments"])} segments; coplanar triangles excluded. Not a room plan.', fill='black')
    return image, {'resolution_px': {'width': int(size[0]), 'height': int(size[1])},
        'image_mapping': {'xy_bounds': [lo.tolist(), hi.tolist()], 'metres_per_pixel': 1/scale,
            'formula': 'x=xmin+(column+0.5)*mpp; y=ymax-(row+0.5)*mpp; z=height_m',
            'label_area_pixels': [0, 0, int(min(size[0], 780)), min(34, int(size[1]))]}}
