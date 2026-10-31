"""Compare original-mesh and saved-source hits on the same orthographic rays.

This is a deterministic depth reading of supplied geometry. It does not identify
texture windows, infer missing rooms, fit the source, or certify fidelity.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image
from shapely.geometry import Point, Polygon

from src.agent.geometry.mesh_bim_frame import (
    observation_to_source, source_to_observation, validate_mesh_frame,
)
from src.agent.geometry.source_model import _digest
from src.agent.geometry.source_visibility import camera_vectors, polygon_normal


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def load_observation(mesh, prefix):
    """Verify creation-time image/buffer bytes before using a saved camera."""
    prefix = Path(prefix)
    paths = {kind: prefix.with_suffix(suffix) for kind, suffix in
             (('metadata', '.json'), ('image', '.png'), ('pixel_buffer', '.npz'))}
    if any(path.is_symlink() or path.resolve() != path.absolute() for path in paths.values()):
        raise ValueError('mesh observation must use local regular evidence files')
    record = json.loads(paths['metadata'].read_bytes())
    hashes = {kind: file_sha256(path) for kind, path in paths.items()}
    if record.get('mesh_sha256') != mesh.mesh_sha256:
        raise ValueError('mesh observation belongs to a different original asset')
    if (record.get('image_sha256') != hashes['image']
            or record.get('pixel_buffer_sha256') != hashes['pixel_buffer']):
        raise ValueError('observation image/buffer changed or lacks creation hashes; call view_mesh again')
    size = record.get('resolution_px', {})
    width, height = size.get('width'), size.get('height')
    if any(type(value) is not int or not 1 <= value <= 1600 for value in (width, height)):
        raise ValueError('mesh observation resolution must contain valid integer dimensions')
    with Image.open(paths['image']) as picture:
        picture.verify()
    with Image.open(paths['image']) as picture:
        if picture.size != (width, height):
            raise ValueError('mesh observation PNG dimensions disagree with metadata')
    camera = record.get('camera', {})
    if camera.get('projection') != 'orthographic':
        raise ValueError('mesh comparison requires an orthographic observation')
    eye, right, up, forward = camera_vectors(camera)
    target = np.asarray(camera.get('target'), dtype=float)
    if target.shape != (3,) or not np.isfinite(target).all():
        raise ValueError('mesh observation target must be finite XYZ')
    distance = np.linalg.norm(target-eye)
    if distance < 1e-9 or not np.allclose((target-eye)/distance, forward, atol=1e-7):
        raise ValueError('mesh camera forward disagrees with eye and target')
    if not np.allclose(np.cross(right, up), -forward, atol=1e-7):
        raise ValueError('mesh camera image axes have inconsistent handedness')
    span = record.get('view_span_m', {})
    if any(isinstance(span.get(key), bool) or not isinstance(span.get(key), (int, float))
           or not math.isfinite(span[key]) or not 0 < span[key] <= 1_000_000 for key in ('width', 'height')):
        raise ValueError('mesh observation spans must be finite positive metres')
    yaw = record.get('source_coordinate_transform', {}).get('yaw_degrees_counterclockwise_about_positive_z')
    if isinstance(yaw, bool) or not isinstance(yaw, (int, float)) or not math.isfinite(yaw):
        raise ValueError('mesh observation yaw must be finite')
    with np.load(paths['pixel_buffer'], allow_pickle=False) as data:
        if (str(data['mesh_sha256'].item()) != mesh.mesh_sha256
                or data['image_shape'].tolist() != [height, width]
                or not math.isclose(float(data['operation_yaw_degrees'].item()), yaw, abs_tol=1e-9)):
            raise ValueError('mesh pixel buffer asset, resolution or yaw disagrees with metadata')
    return record, hashes


def sample_pixels(resolution, *, pixels=None, grid=None, box=None):
    width, height = resolution['width'], resolution['height']
    if pixels is not None:
        if grid is not None or box is not None:
            raise ValueError('choose explicit pixels or a grid with an optional pixel box')
        if not isinstance(pixels, list) or not 1 <= len(pixels) <= 256:
            raise ValueError('pixels must contain 1 through 256 integer pixel pairs')
        for pair in pixels:
            if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                    or any(type(value) is not int for value in pair)
                    or not 0 <= pair[0] < width or not 0 <= pair[1] < height):
                raise ValueError('pixel must be an integer [column,row] inside the observation')
        if len({tuple(pair) for pair in pixels}) != len(pixels):
            raise ValueError('duplicate comparison pixels are not independent samples')
        return [list(pair) for pair in pixels], {'method': 'explicit_pixels'}
    grid = [8, 8] if grid is None else grid
    if not isinstance(grid, (list, tuple)) or len(grid) != 2 or any(type(v) is not int or not 1 <= v <= 16 for v in grid):
        raise ValueError('grid must contain [columns,rows], each an integer from 1 to 16')
    box = [0, 0, width, height] if box is None else box
    if (not isinstance(box, (list, tuple)) or len(box) != 4 or any(type(v) is not int for v in box)
            or not 0 <= box[0] < box[2] <= width or not 0 <= box[1] < box[3] <= height):
        raise ValueError('grid box must be [left,top,right,bottom] within the image')
    columns, rows = min(grid[0], box[2]-box[0]), min(grid[1], box[3]-box[1])
    selected = [[int(box[0]+(x+.5)*(box[2]-box[0])/columns),
                 int(box[1]+(y+.5)*(box[3]-box[1])/rows)] for y in range(rows) for x in range(columns)]
    return selected, {'method': 'regular_pixel_grid', 'requested_grid': list(grid),
        'actual_grid': [columns, rows], 'box_pixels': list(box), 'pixel_centres': True}


def _source_faces(source, frame, yaw):
    open_apertures = {}
    for opening in source.get('openings', []):
        if opening.get('kind') != 'open' and not (opening.get('kind') == 'door' and opening.get('connectivity') == 'open'):
            continue
        for host in source.get('opening_hosts', {}).get(opening['id'], [opening['host_boundary_id']]):
            open_apertures.setdefault(host, []).append(opening['vertices'])
    faces = []
    for boundary in source['boundaries']:
        vertices = source_to_observation(boundary['vertices'], frame, yaw)
        normal = polygon_normal(vertices)
        origin = vertices[0]
        edge = next((edge for edge in vertices[1:]-origin if np.linalg.norm(edge) > 1e-9), None)
        if edge is None:
            raise ValueError('source boundary has no usable planar edge')
        right = edge / np.linalg.norm(edge)
        up = np.cross(normal, right)
        def projected(ring):
            world = source_to_observation(ring, frame, yaw)
            delta = world-origin
            if not np.isfinite(world).all() or np.max(np.abs(delta @ normal)) > 1e-6:
                raise ValueError('mesh correspondence requires planar source boundary rings')
            return np.column_stack((delta @ right, delta @ up))
        polygon = Polygon(projected(boundary['vertices']), [projected(ring) for ring in boundary.get('holes', [])])
        if not polygon.is_valid or polygon.area <= 1e-12:
            raise ValueError('mesh correspondence requires valid source boundary polygons')
        for region in boundary.get('enclosure_regions', []):
            if region.get('condition') == 'open':
                polygon = polygon.difference(Polygon(projected(region['vertices'])))
        for ring in open_apertures.get(boundary['id'], []):
            polygon = polygon.difference(Polygon(projected(ring)))
        faces.append({'id': boundary['id'], 'geometry_type': boundary['geometry_type'],
            'space_id': boundary.get('space_id'), 'origin': origin, 'normal': normal,
            'right': right, 'up': up, 'polygon': polygon})
    return faces


def _precise_mesh_hit(sample, plane, eye, forward):
    """Re-intersect the saved visible face; float32 pixels only check identity.

    pixel_query resolves the global face ID against this exact original asset,
    supplying float64 vertices after its saved yaw. Work relative to one vertex
    so geographic translations do not lose centimetres in the ray calculation.
    """
    evidence = sample.get('triangle_surface_evidence') or {}
    triangle = np.asarray(evidence.get('triangle_vertices_xyz'), dtype=np.float64)
    if (triangle.shape != (3, 3) or not np.isfinite(triangle).all()
            or evidence.get('global_face_id') != sample['face_id']):
        raise ValueError('mesh pixel buffer has no valid original face evidence')
    origin = triangle[0]
    edges = triangle[1:]-origin
    normal = np.cross(edges[0], edges[1])
    length = np.linalg.norm(normal)
    if length <= 1e-15:
        raise ValueError('saved mesh face is degenerate')
    normal /= length
    denominator = float(normal @ forward)
    if abs(denominator) <= 1e-12:
        raise ValueError('saved mesh face is parallel to the camera ray')
    relative_plane = plane-origin
    local = relative_plane-forward*float(relative_plane @ normal)/denominator
    weights, _, rank, _ = np.linalg.lstsq(edges.T, local, rcond=None)
    barycentric = np.array([1-float(weights.sum()), *weights])
    if rank != 2 or np.min(barycentric) < -1e-9:
        raise ValueError('saved camera ray does not intersect the recorded original mesh face')
    depth = float(((origin-eye)+local) @ forward)
    world = origin+local
    if not np.isfinite(world).all() or not math.isfinite(depth) or depth <= 0:
        raise ValueError('recorded original mesh face is not a positive camera ray hit')
    buffered = np.asarray(sample['world_xyz'], dtype=np.float64)
    # A float32 buffer at X=6,000,000 may round X by 25 cm. This tolerance only
    # checks saved bytes against the exact hit; it never relaxes a residual.
    half_ulp = np.abs(np.spacing(world.astype(np.float32))).astype(np.float64)/2
    if not np.isfinite(buffered).all() or np.any(np.abs(buffered-world) > half_ulp+1e-6):
        raise ValueError('mesh pixel buffer point disagrees with its original face camera ray hit')
    return world, depth, float(np.linalg.norm(buffered-world))


def compare_mesh_source(mesh, prefix, source, *, pixels=None, grid=None, box=None, boundary_ids=None):
    """Return same-ray surface correspondences; hidden/missing samples stay explicit."""
    observation, hashes = load_observation(mesh, prefix)
    expected = _digest({key: value for key, value in source.items() if key != 'source_model_sha256'})
    if source.get('source_model_sha256') != expected:
        raise ValueError('source BIM digest mismatch')
    frame = validate_mesh_frame(source.get('mesh_frame'))
    if frame['mesh_sha256'] != mesh.mesh_sha256:
        raise ValueError('source frame belongs to a different original mesh')
    identities = {boundary['id'] for boundary in source['boundaries']}
    if boundary_ids is not None and (not isinstance(boundary_ids, list) or not boundary_ids
            or any(not isinstance(identity, str) or identity not in identities for identity in boundary_ids)
            or len(set(boundary_ids)) != len(boundary_ids)):
        raise ValueError('boundary_ids must name distinct actual source boundaries')
    selected, sampling = sample_pixels(observation['resolution_px'], pixels=pixels, grid=grid, box=box)
    yaw = observation['source_coordinate_transform']['yaw_degrees_counterclockwise_about_positive_z']
    eye, right, up, forward = camera_vectors(observation['camera'])
    target = np.asarray(observation['camera']['target'])
    span, size = observation['view_span_m'], observation['resolution_px']
    width, height = size['width'], size['height']
    footprint = math.hypot(span['width']/width, span['height']/height)/2
    faces = _source_faces(source, frame, yaw)
    mesh_samples = mesh.pixel_query(prefix, selected)['queries']
    # Missing immediate neighbours expose a raster silhouette without treating
    # every tessellation seam as an architectural boundary.
    neighbours = sorted({(x+dx, y+dy) for x, y in selected for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1))
                         if 0 <= x+dx < width and 0 <= y+dy < height})
    adjacent = {tuple(row['pixel']): row for row in mesh.pixel_query(prefix, [list(p) for p in neighbours])['queries']}
    rows, residuals = [], []
    for pixel, mesh_sample in zip(selected, mesh_samples, strict=True):
        x, y = pixel
        plane = target + ((x+.5)/width-.5)*span['width']*right + (.5-(y+.5)/height)*span['height']*up
        hits = []
        for face in faces:
            denominator = float(face['normal'] @ forward)
            if abs(denominator) <= 1e-9:
                continue
            point = plane + forward * float((face['origin']-plane) @ face['normal']) / denominator
            depth = float((point-eye) @ forward)
            if depth <= 0:
                continue
            local = point-face['origin']
            projected = Point(float(local @ face['right']), float(local @ face['up']))
            if face['polygon'].covers(projected):
                hits.append({'depth_m': depth, 'point': point, 'face': face,
                             'edge_distance_m': projected.distance(face['polygon'].boundary)})
        hits.sort(key=lambda item: (item['depth_m'], item['face']['id']))
        front = hits[0] if hits else None
        tied = [hit for hit in hits if front and abs(hit['depth_m']-front['depth_m']) <= 1e-7]
        row = {'pixel': pixel, 'mesh_hit': bool(mesh_sample['hit']), 'source_hit': bool(front),
               'valid_for_residual_summary': False}
        if mesh_sample['hit']:
            world, depth, rounding = _precise_mesh_hit(mesh_sample, plane, eye, forward)
            missing = sum(not adjacent[p]['hit'] for p in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)) if p in adjacent)
            row['mesh'] = {'face_id': mesh_sample['face_id'], 'observation_xyz': world.tolist(),
                'source_xyz': observation_to_source([world], frame, yaw)[0].tolist(),
                'depth_m': depth, 'missing_adjacent_pixels': missing,
                'position_method': 'original_face_camera_ray_float64',
                'buffer_observation_xyz': mesh_sample['world_xyz'], 'buffer_rounding_distance_m': rounding}
        if front:
            row['source'] = {'boundary_ids': [hit['face']['id'] for hit in tied],
                'geometry_types': sorted({hit['face']['geometry_type'] for hit in tied}),
                'observation_xyz': front['point'].tolist(),
                'source_xyz': observation_to_source([front['point']], frame, yaw)[0].tolist(),
                'depth_m': front['depth_m'], 'nearest_polygon_edge_distance_m': front['edge_distance_m']}
        if mesh_sample.get('reason') == 'transparent_composite':
            row['status'] = 'mesh_transparent_composite'
            row['measurement_invalid_reason'] = mesh_sample['measurement_invalid_reason']
        elif not front or not mesh_sample['hit']:
            row['status'] = ('neither_hit' if not front and not mesh_sample['hit'] else
                             'mesh_no_hit' if not mesh_sample['hit'] else 'source_no_hit')
        elif boundary_ids is not None and not set(boundary_ids).intersection(row['source']['boundary_ids']):
            scoped = [hit for hit in hits if hit['face']['id'] in boundary_ids]
            row['status'] = 'selected_source_occluded' if scoped else 'source_outside_selected_boundaries'
            row['selected_source_hit_depth_m'] = scoped[0]['depth_m'] if scoped else None
        else:
            residual = front['depth_m']-row['mesh']['depth_m']
            normal = mesh_sample.get('triangle_surface_evidence', {}).get('triangle_normal_xyz')
            row['signed_depth_residual_m'] = residual
            row['absolute_depth_residual_m'] = abs(residual)
            row['surface_normal_angle_degrees_unsigned'] = (math.degrees(math.acos(np.clip(
                abs(np.asarray(normal) @ front['face']['normal']), 0, 1))) if normal is not None else None)
            edge = front['edge_distance_m'] <= footprint or row['mesh']['missing_adjacent_pixels'] > 0
            row['status'] = 'edge_sample' if edge else 'paired_visible_ray_hits'
            row['valid_for_residual_summary'] = not edge
            if not edge:
                residuals.append(abs(residual))
        rows.append(row)
    values = np.asarray(residuals)
    counts = {status: sum(row['status'] == status for row in rows) for status in sorted({row['status'] for row in rows})}
    bands = {'within_5cm': int(sum(values <= .05)), 'over_5_to_10cm': int(sum((values > .05) & (values <= .1))),
             'over_10_to_30cm': int(sum((values > .1) & (values <= .3))), 'over_30cm': int(sum(values > .3))}
    return {'mesh_sha256': mesh.mesh_sha256, 'source_model_sha256': expected, 'mesh_frame': frame,
        'observation_file_sha256': hashes, 'observation': Path(prefix).name, 'resolution_px': size,
        'sampling': sampling, 'boundary_ids': boundary_ids, 'sample_count': len(rows), 'counts': counts,
        'valid_sample_count': len(values), 'invalid_sample_count': len(rows)-len(values),
        'valid_sample_fraction': len(values)/len(rows), 'samples': rows,
        'depth_residual_summary_m': {'mean_absolute': float(np.mean(values)) if len(values) else None,
            'median_absolute': float(np.median(values)) if len(values) else None,
            'maximum_absolute': float(np.max(values)) if len(values) else None,
            'rms': float(np.sqrt(np.mean(values**2))) if len(values) else None},
        'absolute_depth_residual_bands': bands, 'edge_exclusion_footprint_m': footprint,
        'mesh_hit_method': 'Saved visible global face ID, re-intersected with the saved camera ray using original float64 triangle vertices.',
        'signed_residual_convention': 'source depth minus original mesh depth along the saved camera forward; positive means source is farther',
        'fitted_to_mesh': False, 'fidelity': 'not_evaluated', 'limitations': [
            'Sampled first positive ray hits only, not nearest 3D surface distance or whole-building coverage.',
            'No-hit is unknown, not a zero residual. Near source polygon edges and mesh silhouettes are excluded from aggregate bands.',
            'Pixels with contributing fractional mesh alpha have no unique measurable surface and are excluded, separately from background/no-hit.',
            'Glazing and closed/unknown doors use the host envelope; explicitly open doors/passages/enclosure regions let rays through.',
            'Unselected source still occludes selected surfaces. Mesh bounds may omit occluders; this uses precisely the saved observation selection.',
            'Source boundaries are the actual exported planar/tessellated geometry, including any declared approximation.',
            'No texture-window recognition, semantic correspondence, registration fit or independent fidelity approval.'],
        'mesh_selection_bounds': observation.get('selection_bounds')}
