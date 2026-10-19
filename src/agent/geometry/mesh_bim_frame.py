"""Explicit rigid registration of a source BIM to one original GLB asset.

This records an agent's interpretation; neither validation nor projection fits
the candidate to the mesh or establishes surveyed placement.
"""
from __future__ import annotations

import copy
import math
import re

import numpy as np
from PIL import ImageDraw


def validate_mesh_frame(value):
    fields = {'mesh_sha256', 'yaw_degrees', 'translation_m', 'reason', 'source_refs'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f'mesh_frame requires exactly {sorted(fields)}')
    if not isinstance(value['mesh_sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', value['mesh_sha256']):
        raise ValueError('mesh_frame.mesh_sha256 must identify the original GLB')
    numbers = [value['yaw_degrees']]
    translation = value['translation_m']
    if not isinstance(translation, list) or len(translation) != 3:
        raise ValueError('mesh_frame.translation_m must contain three metres')
    numbers += translation
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in numbers):
        raise ValueError('mesh_frame rotation and translation must be finite numbers')
    if not isinstance(value['reason'], str) or not value['reason'].strip():
        raise ValueError('mesh_frame.reason must explain the placement')
    refs = value['source_refs']
    if not isinstance(refs, list) or not refs or any(not isinstance(r, str) or not r.strip() for r in refs):
        raise ValueError('mesh_frame.source_refs must cite observations or an explicit assumption')
    return copy.deepcopy(value)


def rotate_points(points, yaw):
    angle = math.radians(yaw)
    c, s = math.cos(angle), math.sin(angle)
    return np.asarray(points, dtype=float) @ np.array([[c, s, 0], [-s, c, 0], [0, 0, 1]])


def source_to_observation(points, frame, observation_yaw):
    """source = R(frame.yaw) * original_Zup + translation, no scale change."""
    frame = validate_mesh_frame(frame)
    return rotate_points(np.asarray(points, dtype=float) - frame['translation_m'],
                         observation_yaw - frame['yaw_degrees'])


def observation_to_source(points, frame, observation_yaw):
    frame = validate_mesh_frame(frame)
    return rotate_points(points, frame['yaw_degrees'] - observation_yaw) + frame['translation_m']


def render_mesh_bim_overlay(picture, observation, source, *, floor_id=None, exterior_only=True,
                            visibility='xray', boundary_ids=None):
    """Project saved source edges with explicit X-ray or source visibility semantics."""
    from shapely.ops import unary_union

    from src.agent.geometry.openings import _frame, _lift, _project
    from src.agent.geometry.source_bim import _parts
    from src.agent.geometry.source_model import _digest
    from src.agent.geometry.source_visibility import (
        camera_vectors, polygon_normal, source_depth_buffer, visible_edge_segments,
    )

    if visibility not in {'xray', 'visible'}:
        raise ValueError('visibility must be visible or xray')
    all_boundaries = {b['id']: b for b in source['boundaries']}
    if boundary_ids is not None:
        if (not isinstance(boundary_ids, list) or not boundary_ids or
                any(not isinstance(b, str) or b not in all_boundaries for b in boundary_ids)):
            raise ValueError('boundary_ids must contain existing source boundary IDs')
        boundary_ids = set(boundary_ids)

    expected = _digest({k: v for k, v in source.items() if k != 'source_model_sha256'})
    if source.get('source_model_sha256') != expected:
        raise ValueError('source BIM digest mismatch')
    frame = validate_mesh_frame(source.get('mesh_frame'))
    if frame['mesh_sha256'] != observation['mesh_sha256']:
        raise ValueError('candidate frame and mesh observation refer to different assets')
    width, height = (observation['resolution_px'][k] for k in ('width', 'height'))
    if picture.size != (width, height):
        raise ValueError('mesh observation image dimensions changed')
    floor_ids = {f['id'] for f in source['floors']}
    if floor_id is not None and floor_id not in floor_ids:
        raise ValueError('unknown floor_id')
    spaces = {s['id'] for s in source['spaces'] if floor_id is None or s['floor_id'] == floor_id}
    if floor_id is not None:
        floor = next(f for f in source['floors'] if f['id'] == floor_id)
        spaces.update(floor.get('spanning_space_ids', []))
    bounds = [b for b in source['boundaries'] if b['space_id'] in spaces
              and b['geometry_type'] == 'wall'
              and (boundary_ids is None or b['id'] in boundary_ids)]
    openings = [o for o in source['openings'] if spaces.intersection(o['space_ids'])
                and (not exterior_only or o['exterior'])
                and (boundary_ids is None or boundary_ids.intersection(
                    source.get('opening_hosts', {}).get(o['id'], [o['host_boundary_id']])))]
    boundary_parts = []
    for boundary in bounds:
        if not exterior_only:
            boundary_parts.append({**boundary, 'holes': []})
            continue
        origin = boundary['vertices'][0][:2]
        direction, _ = _frame(origin, boundary['vertices'][1][:2])
        parent = _project(boundary['vertices'], origin, direction)
        contacts = []
        for relation in source.get('boundary_relations', []):
            if boundary['id'] not in relation.get('boundary_ids', []):
                continue
            for region in relation.get('regions', []):
                patch = _project(region['vertices'], origin, direction)
                if region.get('holes'):
                    patch = patch.difference(unary_union(
                        [_project(hole, origin, direction) for hole in region['holes']]))
                contacts.append(patch)
        contact = unary_union(contacts)
        exterior = parent.difference(contact) if contacts else parent
        for part in _parts(exterior):
            boundary_parts.append({
                **boundary,
                'vertices': _lift(list(part.exterior.coords)[:-1], origin, direction),
                'holes': [_lift(list(ring.coords)[:-1], origin, direction)
                          for ring in part.interiors],
            })
    yaw = observation['source_coordinate_transform']['yaw_degrees_counterclockwise_about_positive_z']
    camera = observation['camera']
    target = np.asarray(camera['target'])
    right, up = np.asarray(camera['screen_right']), np.asarray(camera['screen_up'])
    span = observation['view_span_m']
    rendered = picture.convert('RGB').copy()
    draw = ImageDraw.Draw(rendered)
    depth = None
    if visibility == 'visible':
        _, _, _, forward = camera_vectors(camera)
        open_apertures = {}
        for aperture in source['openings']:
            if aperture['kind'] != 'open' and not (
                    aperture['kind'] == 'door' and aperture.get('connectivity') == 'open'):
                continue
            for host in source.get('opening_hosts', {}).get(aperture['id'], [aperture['host_boundary_id']]):
                open_apertures.setdefault(host, []).append(aperture['vertices'])
        depth = source_depth_buffer(source['boundaries'],
            lambda vertices: source_to_observation(vertices, frame, yaw),
            camera, span, (width, height), open_apertures=open_apertures)
    rows = []
    for obj in boundary_parts + openings:
        source_rings = [obj['vertices'], *obj.get('holes', [])]
        world_rings = [source_to_observation(ring, frame, yaw) for ring in source_rings]
        screen_rings = []
        for world in world_rings:
            delta = world - target
            screen_rings.append(np.column_stack(
                ((delta @ right / span['width'] + .5) * width - .5,
                 (.5 - delta @ up / span['height']) * height - .5)))
        kind = obj.get('geometry_type', obj.get('kind'))
        color = {'window': '#00b85b', 'door': '#ff8800', 'passage': '#ff8800'}.get(kind, '#dc24c5')
        host_id = obj.get('host_boundary_id', obj['id'])
        host = all_boundaries[host_id]
        hosts = source.get('opening_hosts', {}).get(obj['id'], [host_id]) if 'kind' in obj else [host_id]
        scoped_hosts = [identity for identity in hosts if boundary_ids is None or identity in boundary_ids]
        visibility_record = {'mode': visibility}
        if visibility == 'visible':
            facing_hosts = [identity for identity in scoped_hosts if polygon_normal(
                source_to_observation(all_boundaries[identity]['vertices'], frame, yaw)) @ forward < -1e-8]
            # Interior observations see the inside of outward-oriented source
            # walls. Depth, not the arbitrary opening owner, decides visibility.
            facing = bool(facing_hosts) or not exterior_only
            visibility_record.update(host_front_facing=bool(facing_hosts),
                                     facing_host_boundary_ids=facing_hosts,
                                     facing_policy='front_facing_exterior' if exterior_only else 'two_sided_inspection',
                                     tested_edge_samples=0,
                                     visible_edge_samples=0, occluded_edge_samples=0)
            for world, screen in zip(world_rings, screen_rings):
                segments, stats = visible_edge_segments(world, screen, depth, camera, span)
                visibility_record['raster_depth_tolerance_m'] = stats['raster_depth_tolerance_m']
                visibility_record['tested_edge_samples'] += stats['tested_edge_samples']
                visible_count = stats['visible_edge_samples'] if facing else 0
                visibility_record['visible_edge_samples'] += visible_count
                visibility_record['occluded_edge_samples'] += stats['tested_edge_samples']-visible_count
                if facing:
                    for segment in segments:
                        if len(segment) >= 2:
                            draw.line([tuple(p) for p in segment], fill=color, width=2)
            visibility_record['status'] = ('visible' if visibility_record['visible_edge_samples'] else
                'back_facing_or_edge_on' if not facing else
                'occluded' if visibility_record['tested_edge_samples'] else 'outside_view')
        else:
            for screen in screen_rings:
                points = [tuple(p) for p in screen.tolist()]
                draw.line(points + points[:1], fill=color, width=2)
        rows.append({'id': obj['id'], 'kind': kind,
                     'host_boundary_id': host_id, 'space_id': host['space_id'],
                     'host_boundary_ids': hosts, 'scoped_host_boundary_ids': scoped_hosts,
                     'visibility': visibility_record,
                     'observation_xyz': world_rings[0].tolist(),
                     'projected_pixels': screen_rings[0].tolist(),
                     'hole_observation_xyz': [ring.tolist() for ring in world_rings[1:]],
                     'hole_projected_pixels': [ring.tolist() for ring in screen_rings[1:]]})
    draw.rectangle((0, 0, min(width, 760), 31), fill='white')
    label = 'X-RAY' if visibility == 'xray' else 'VISIBLE EDGES'
    draw.text((6, 4), f'SOURCE BIM {label}: magenta walls / green windows / orange doors', fill='black')
    draw.text((6, 17), ('Hidden source edges shown.' if visibility == 'xray' else
        'Source geometry occlusion only; not a mesh fidelity check.') + ' No automatic fitting.', fill='black')
    return rendered, {'mesh_sha256': observation['mesh_sha256'], 'source_model_sha256': expected,
        'mesh_frame': frame, 'floor_id': floor_id, 'exterior_only': exterior_only,
        'projection': 'orthographic source edges over unchanged original textured mesh image',
        'visibility': visibility, 'boundary_ids': sorted(boundary_ids) if boundary_ids is not None else None,
        'occlusion': ('source edges are not depth-tested; hidden edges remain visible' if visibility == 'xray'
            else ('front-facing exterior hosts' if exterior_only else 'two-sided interior inspection') +
                 ' and full saved source depth; glazing occludes as host envelope; '
                 'open enclosure regions and explicitly open apertures excluded; '
                 'unknown enclosure/door state remains an occluder; no original mesh depth test'),
        'visible_opening_count': (sum(row['kind'] in {'window', 'door', 'passage', 'open'} and
            row['visibility'].get('visible_edge_samples', 0) > 0 for row in rows)
            if visibility == 'visible' else None),
        'fitted_to_mesh': False, 'fidelity': 'not_evaluated',
        'boundary_count': len({part['id'] for part in boundary_parts}),
        'boundary_part_count': len(boundary_parts), 'opening_count': len(openings), 'objects': rows}
