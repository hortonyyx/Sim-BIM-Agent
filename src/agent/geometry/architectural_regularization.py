"""Explicit, source-coordinate architectural parameter adoption.

This is a pure pre-compiler for compact parametric plans, not a mesh fitter.
Straight supporting lines share normal-offset anchors; their intersections may
have fractional coordinates. Straight curve paths retain their identities while
shared endpoints follow those supports; true curves and mesh_frame are preserved.
The caller must still run the common source/topology save gate.

``regularize_architectural_plan(plan, policy=None)`` consumes ``plan.modelling``
or an explicit policy and returns ``(adopted_plan, audit)``. Policy v1 accepts
``grid_m`` (default .1), ``frames`` ({id, origin, yaw_degrees, templates}),
``vertical`` ({datum_m, merge_groups}), and ``space_intents`` (template:space ->
component/shaft/cabinet/room). A height merge group has id, semantic and targets
({kind: instance/repeat, id, edge: base/top}); it is an explicit same-level claim,
not a distance-clustering request. No space, opening or curve is deleted.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from decimal import Decimal, ROUND_HALF_UP

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

SEAM_M = 0.00002
EPS = 1e-8
DEFAULT_ARCHITECTURAL_POLICY = {
    'schema_version': '1', 'grid_m': 0.1, 'simplification_tolerance_m': 0.3,
}


class ArchitecturalRegularizationError(ValueError):
    def __init__(self, message, audit):
        super().__init__(message)
        self.audit = audit


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('expected a finite number')
    return float(value)


def _snap(value, grid):
    return float((Decimal(str(value)) / Decimal(str(grid))).quantize(
        Decimal('1'), rounding=ROUND_HALF_UP) * Decimal(str(grid)))


def _fields(obj, allowed):
    if not isinstance(obj, dict) or set(obj) - set(allowed):
        raise ValueError(f'unknown or invalid fields; allowed: {sorted(allowed)}')


def _curved(template):
    footprint = template.get('footprint', {})
    return (isinstance(footprint, dict) and bool(footprint.get('boundary_path'))) or any(
        bool(s.get('boundary_path')) for s in template.get('spaces', []))


def _rings(template):
    """Return mutable straight rings, converting rect declarations on a copy."""
    footprint = template['footprint']
    if isinstance(footprint, list):
        yield 'footprint', footprint
    else:
        yield 'footprint.vertices', footprint['vertices']
        for i, ring in enumerate(footprint.get('holes', [])):
            yield f'footprint.holes[{i}]', ring
    for space in template['spaces']:
        if 'rect' in space:
            x0, y0, x1, y1 = map(_number, space.pop('rect'))
            if x0 >= x1 or y0 >= y1:
                raise ValueError('rect must have positive dimensions')
            space['polygon'] = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
        yield f"spaces[{space['id']}].polygon", space['polygon']
        for i, ring in enumerate(space.get('holes', [])):
            yield f"spaces[{space['id']}].holes[{i}]", ring


def _space_edges(space):
    return [(a, b) for ring in [space['polygon'], *space.get('holes', [])]
            for a, b in zip(ring, ring[1:] + ring[:1])]


def _width(points):
    box = Polygon(points).minimum_rotated_rectangle
    if box.geom_type != 'Polygon':
        return 0.0
    p = list(box.exterior.coords)
    return min(math.dist(a, b) for a, b in zip(p, p[1:]))


def _frames(policy, names):
    frames = {}
    for row in policy.get('frames', []):
        _fields(row, {'id', 'origin', 'yaw_degrees', 'templates'})
        origin = list(map(_number, row.get('origin', [0, 0])))
        if len(origin) != 2 or not isinstance(row.get('id'), str):
            raise ValueError('frame needs an id and two-coordinate origin')
        yaw = _number(row.get('yaw_degrees', 0))
        angle = math.radians(yaw)
        frame = dict(id=row['id'], origin=origin, yaw_degrees=yaw,
                     c=math.cos(angle), s=math.sin(angle))
        if not row.get('templates'):
            raise ValueError('frame templates must be nonempty')
        for name in row['templates']:
            if name not in names or name in frames:
                raise ValueError(f'unknown or multiply assigned template frame: {name}')
            frames[name] = frame
    default = dict(id='identity', origin=[0., 0.], yaw_degrees=0., c=1., s=0.)
    for name in names:
        frames.setdefault(name, default)
    identities = {}
    for frame in frames.values():
        signature = (tuple(frame['origin']), frame['yaw_degrees'])
        if frame['id'] in identities and identities[frame['id']] != signature:
            raise ValueError('same frame id must have the same origin and orientation')
        identities[frame['id']] = signature
    return frames


def _local(p, f):
    x, y = _number(p[0]) - f['origin'][0], _number(p[1]) - f['origin'][1]
    return [f['c'] * x + f['s'] * y, -f['s'] * x + f['c'] * y]


def _world(p, f):
    return [round(f['origin'][0] + f['c'] * p[0] - f['s'] * p[1], 12),
            round(f['origin'][1] + f['s'] * p[0] + f['c'] * p[1], 12)]


def _cluster(values):
    """Fixed-representative tolerance, deliberately not transitive clustering."""
    groups = []
    for value in sorted(values):
        if not groups or value - groups[-1][0] > SEAM_M + 1e-12:
            groups.append([value])
        else:
            groups[-1].append(value)
    return groups


def regularize_architectural_plan(plan, *, policy=None):
    audit = dict(schema_version='architectural_regularization_v1', status='pending',
                 changes=[], diagnostics=[], not_assessed=[], rejections=[],
                 model_calls=0, geometry_fitted=False,
                 required_next_check='common source kernel and topology save gate')

    def record_rejection(code, path, message, **details):
        audit['status'] = 'rejected'
        audit['rejections'].append(dict(code=code, path=path, message=message, **details))

    def reject(code, path, message, **details):
        record_rejection(code, path, message, **details)
        raise ArchitecturalRegularizationError(message, audit)

    def finish_stage(stage, blocked):
        if audit['rejections']:
            audit['not_assessed'].extend(dict(path=path, blocked_by=stage,
                reason='dependent checks require a valid adopted previous stage') for path in blocked)
            raise ArchitecturalRegularizationError(
                f"{stage}: {len(audit['rejections'])} independent problem(s); inspect rejections before retrying", audit)

    try:
        original = copy.deepcopy(plan)
        audit['input_sha256'] = _hash(original)
        from src.agent.geometry.parametric_primitives import (
            normalize_parametric_primitives, parametric_primitive_frame_context)
        primitive_frames = parametric_primitive_frame_context(original)
        original = normalize_parametric_primitives(original)
        embedded = original.pop('modelling', None)
        if policy is not None and embedded is not None:
            if not isinstance(embedded, dict) or any(k in policy and policy[k] != v for k, v in embedded.items()):
                reject('conflicting_policy', 'modelling', 'embedded modelling cannot override the supplied policy')
            policy = {**policy, **embedded}
        policy = copy.deepcopy(policy if policy is not None else embedded)
        if policy is None:
            audit.update(status='not_requested', adopted_sha256=_hash(original))
            return original, audit
        _fields(policy, {'schema_version', 'grid_m', 'simplification_tolerance_m', 'frames', 'vertical', 'space_intents'})
        explicit_names = {name for frame in policy.get('frames', []) for name in frame.get('templates', [])}
        derived_frames = []
        for frame in primitive_frames:
            frame['templates'] = [name for name in frame['templates'] if name not in explicit_names]
            if frame['templates']:
                derived_frames.append(frame)
        if derived_frames:
            policy['frames'] = [*policy.get('frames', []), *derived_frames]
            audit['primitive_frames'] = copy.deepcopy(derived_frames)
        assigned_names = explicit_names | {name for frame in derived_frames for name in frame['templates']}
        for name, template in plan['templates'].items():
            declarations = [space['primitive'] for space in template['spaces'] if 'primitive' in space]
            if isinstance(template['footprint'], dict) and 'primitive' in template['footprint']:
                declarations.append(template['footprint']['primitive'])
            declarations.extend(row['local_baseline'] for key in ('doors','opening_rows')
                                for row in template.get(key,[]) if 'local_baseline' in row)
            if declarations and name not in assigned_names:
                audit['diagnostics'].append(dict(type='mixed_primitive_frames', path=f'templates.{name}',
                    primitive_frames=sorted({row['frame'] for row in declarations}),
                    adopted_frame='identity', action='declare modelling.frames if another ruler is intended'))
        if policy.get('schema_version', '1') != '1':
            raise ValueError('modelling schema_version must be 1')
        grid = _number(policy.get('grid_m', .1))
        if grid <= 0:
            raise ValueError('grid_m must be positive')
        simplification = _number(policy.get('simplification_tolerance_m', grid))
        if simplification < 0:
            raise ValueError('simplification_tolerance_m must be nonnegative')
        audit['policy'] = copy.deepcopy(policy)
        audit['grid_m'] = grid
        audit['anchor_tolerance_m'] = SEAM_M
        displacement_limit = min(grid, simplification)
        audit['max_outline_displacement_m'] = displacement_limit
        audit['observed_max_vertex_displacement_m'] = 0.0
        from src.agent.geometry.architectural_line_paths import expand_straight_paths, restore_straight_paths
        original, line_bindings = expand_straight_paths(original)
        adopted = copy.deepcopy(original)
        frames = _frames(policy, adopted['templates'])
        audit['frames'] = {name: {k: f[k] for k in ('id', 'origin', 'yaw_degrees')}
                           for name, f in frames.items()}
        audit['frame_relation'] = 'frames are in source XY; mesh_frame remains unchanged and is not an anchor'
        intents = policy.get('space_intents', {})
        if not isinstance(intents, dict) or any(v not in ('component', 'shaft', 'cabinet', 'room') for v in intents.values()):
            raise ValueError('space_intents must map template:space to component/shaft/cabinet/room')
        known_intents = {f"{name}:{s['id']}" for name, t in adopted['templates'].items() for s in t['spaces']}
        if set(intents) - known_intents:
            raise ValueError('space_intents references unknown template space')

        # Collect all supporting lines before moving anything. Shared directions
        # and offsets, including partial wall segments, use one anchor family.
        families, records, old_polygons, host_supports = {}, [], {}, {}
        for name, template in adopted['templates'].items():
            if _curved(template):
                audit['not_assessed'].append(dict(path=f'templates.{name}',
                    scope='curve XY dimensions and narrow-space width', reason='preserve declared curves and their derived points'))
                continue
            ring_records = list(_rings(template))
            original['templates'][name] = copy.deepcopy(template)
            for space in template['spaces']:
                path = f"templates.{name}.spaces[{space['id']}]"
                old_polygons[(name, space['id'])] = copy.deepcopy(_space_edges(space))
                width = _width(space['polygon'])
                if width < .6 - EPS:
                    intent = intents.get(f"{name}:{space['id']}")
                    notes = json.dumps([space.get('assumptions', []), space.get('role_evidence', {})], ensure_ascii=False).lower()
                    geometry_only = ('geometry-only' in notes or 'geometry only' in notes or 'not occupied' in notes)
                    blade = any(word in notes for word in ('blade', 'parapet', 'narrow fin'))
                    audit['diagnostics'].append(dict(type='narrow_space', path=path, width_m=width,
                        method='minimum-area oriented bounding rectangle; not a room-neck-width proof',
                        role=space.get('role'), intent=intent, action='none'))
                    if intent == 'component' or (intent not in ('shaft', 'cabinet') and geometry_only and blade):
                        record_rejection('narrow_component_as_space', path,
                               'narrow geometry-only component needs a component representation or explicit design decision; no automatic widening/deletion', width_m=width)
            frame = frames[name]
            for path, ring in ring_records:
                if len(ring) < 3 or not Polygon(ring).is_valid or Polygon(ring).area <= EPS:
                    record_rejection('invalid_ring', f'templates.{name}.{path}', 'input ring must be a valid positive-area polygon')
                    continue
                points = [_local(p, frame) for p in ring]
                edges = []
                for a, b in zip(points, points[1:] + points[:1]):
                    length = math.dist(a, b)
                    if length <= EPS:
                        record_rejection('degenerate_edge', f'templates.{name}.{path}', 'zero-length polygon edge')
                        break
                    n = [(b[1]-a[1])/length, -(b[0]-a[0])/length]
                    if n[0] < -EPS or (abs(n[0]) <= EPS and n[1] < 0):
                        n = [-n[0], -n[1]]
                    bucket = families.setdefault(frame['id'], [])
                    family = next((g for g in bucket if math.dist(g['normal'], n) <= 1e-10), None)
                    if family is None:
                        family = dict(normal=n, offsets=[], endpoints=[])
                        bucket.append(family)
                    d = sum(v*w for v, w in zip(family['normal'], a))
                    family['offsets'].append(d)
                    family['endpoints'].append((d, a, b))
                    edges.append((family, d))
                if len(edges) != len(points):
                    continue
                records.append((name, path, ring, points, edges))
                for space in template['spaces']:
                    if any(ring is r for r in [space['polygon'], *space.get('holes', [])]):
                        host_supports.setdefault((name, space['id']), []).extend(edges)
                        break
        finish_stage('input_rings', ['ring_adoption', 'apertures', 'vertical', 'contacts'])
        for fid, groups in families.items():
            for group in groups:
                mapping, targets, tangent_shifts = {}, {}, {}
                n = group['normal']
                tangent = [-n[1], n[0]]
                if tangent[0] < -EPS or (abs(tangent[0]) <= EPS and tangent[1] < 0):
                    tangent = [-tangent[0], -tangent[1]]
                group['tangent'] = tangent
                for cluster in _cluster(group['offsets']):
                    anchor = cluster[0]
                    target = _snap(anchor, grid)
                    if target in targets:
                        audit['diagnostics'].append(dict(type='rounded_line_coincidence', frame_id=fid,
                            offsets_m=[targets[target], anchor], adopted_offset_m=target,
                            action='retain identities; reject collapsed rings or changed volume contacts'))
                    targets[target] = anchor
                    mapping.update({value: target for value in cluster})
                    cluster_values = set(cluster)
                    along = [sum(v*w for v,w in zip(tangent, point))
                             for d,a,b in group['endpoints'] if d in cluster_values for point in (a,b)]
                    tangent_anchor = min(along)
                    tangent_shift = _snap(tangent_anchor, grid) - tangent_anchor
                    tangent_shifts.update({value: tangent_shift for value in cluster})
                    if any(abs(value-target) > 1e-10 for value in cluster):
                        audit['changes'].append(dict(type='supporting_line_anchor', frame_id=fid,
                            normal=group['normal'], observed_offsets_m=sorted(set(cluster)),
                            representative_offset_m=anchor, adopted_offset_m=target,
                            tangent=tangent, tangent_anchor_m=tangent_anchor,
                            tangent_shift_m=tangent_shift))
                group['mapping'] = mapping
                group['tangent_shifts'] = tangent_shifts
        for name, path, ring, points, edges in records:
            rebuilt = []
            for i, point in enumerate(points):
                previous, dp = edges[i-1]
                current, dc = edges[i]
                a, b = previous['normal'], current['normal']
                x, y = previous['mapping'][dp], current['mapping'][dc]
                determinant = a[0]*b[1]-a[1]*b[0]
                if abs(determinant) <= 1e-10:
                    if abs(x-y) > EPS:
                        record_rejection('parallel_corner', f'templates.{name}.{path}', 'parallel corner supports disagree')
                        break
                    t = current['tangent']
                    along = sum(v*w for v,w in zip(t, point)) + current['tangent_shifts'][dc]
                    p = [b[j]*y + t[j]*along for j in (0,1)]
                else:
                    p = [(x*b[1]-a[1]*y)/determinant, (a[0]*y-x*b[0])/determinant]
                rebuilt.append(_world(p, frames[name]))
            if len(rebuilt) != len(points):
                continue
            polygon = Polygon(rebuilt)
            movements = [math.dist(_world(p, frames[name]), q) for p, q in zip(points, rebuilt)]
            movement = max(movements)
            audit['observed_max_vertex_displacement_m'] = max(audit['observed_max_vertex_displacement_m'], movement)
            if movement > displacement_limit + EPS:
                index = movements.index(movement)
                record_rejection('outline_displacement_exceeded', f'templates.{name}.{path}',
                       'support-line intersections amplify movement beyond the architectural displacement budget; preserve input and declare intended dimensions',
                       max_vertex_displacement_m=movement, limit_m=displacement_limit,
                       outline_hausdorff_m=Polygon(ring).hausdorff_distance(polygon),
                       worst_vertex=dict(index=index, before=copy.deepcopy(ring[index]), after=rebuilt[index],
                           displacement_m=movement, supports=[dict(normal=g['normal'], observed_offset_m=d,
                           adopted_offset_m=g['mapping'][d]) for g,d in (edges[index-1], edges[index])]))
                continue
            if not polygon.is_valid or polygon.area <= EPS or any(math.dist(a,b)<=EPS for a,b in zip(rebuilt,rebuilt[1:]+rebuilt[:1])):
                record_rejection('unsafe_polygon_change', f'templates.{name}.{path}', 'anchor adoption collapses or invalidates a polygon')
                continue
            ring[:] = rebuilt
        finish_stage('ring_adoption', ['apertures', 'vertical', 'contacts'])

        new_polygons = {(name, s['id']): _space_edges(s) for name, t in adopted['templates'].items()
                        if not _curved(t) for s in t['spaces']}

        def move_pair(name, sid, p1, p2, path, spans=None):
            if (name, sid) not in old_polygons:
                return copy.deepcopy([p1, p2])  # preserved curve template
            before, after = old_polygons[(name, sid)], new_polygons[(name, sid)]
            length = math.dist(p1,p2)
            if length <= EPS:
                reject('collapsed_aperture', path, 'aperture baseline must have positive length')
            physical_spans = [[0.,length]] if spans is None else spans
            if not physical_spans or any(len(span) != 2 or not 0 <= _number(span[0]) < _number(span[1]) <= length+EPS
                                         for span in physical_spans):
                reject('invalid_aperture_spans', path, 'physical spans must lie inside the directed baseline')
            def span_pair(pair, span):
                return [[a+(b-a)*distance/length for a,b in zip(*pair)] for distance in span]
            matched = []
            for span in physical_spans:
                physical = span_pair([p1,p2], span)
                matches = [i for i,(a,b) in enumerate(before)
                           if all(LineString([a,b]).distance(Point(p)) <= SEAM_M for p in physical)]
                if len(matches) != 1:
                    reject('ambiguous_aperture_host', path, 'aperture must have one complete original straight host',
                           host=dict(template=name, space=sid), endpoints=physical, matching_edges=matches)
                matched.append((span,matches[0]))
            i = matched[0][1]
            support, offset = host_supports[(name,sid)][i]
            n, t = support['normal'], support['tangent']
            normal = support['mapping'][offset]
            tangent_shift = support['tangent_shifts'][offset]
            result = []
            for p in (p1,p2):
                local = _local(p, frames[name])
                along = sum(v*w for v,w in zip(t,local)) + tangent_shift
                result.append(_world([n[j]*normal + t[j]*along for j in (0,1)], frames[name]))
            for span,i in matched:
                aa,bb = after[i]
                physical = span_pair(result,span)
                distances = [LineString([aa,bb]).distance(Point(p)) for p in physical]
                if max(distances) > SEAM_M:
                    reject('adopted_aperture_host_overrun', path,
                        'adopted aperture must remain fully inside every declared host; revise the baseline or intended host dimensions',
                        host=dict(template=name, space=sid, edge_index=i), original_endpoints=span_pair([p1,p2],span),
                        adopted_endpoints=physical, adopted_host=[aa,bb], endpoint_host_distances_m=distances)
            if math.dist(*result) <= EPS:
                reject('collapsed_aperture', path, 'aperture collapsed with its host')
            return result

        def template_pair(name, row, path, spans=None):
            result = move_pair(name, row['space'], row['p1'], row['p2'], path, spans)
            if row.get('other_space'):
                other = move_pair(name, row['other_space'], row['p1'], row['p2'], path, spans)
                if any(math.dist(a,b)>EPS for a,b in zip(result,other)):
                    reject('shared_host_conflict', path, 'the two declared hosts do not adopt the same aperture',
                           hosts=[row['space'],row['other_space']], adopted_endpoints=[result,other])
            return result

        for name, template in adopted['templates'].items():
            if _curved(template):
                continue
            for collection in ('doors', 'opening_rows'):
                for row in template.get(collection, []):
                    path = f"templates.{name}.{collection}[{row['id']}]"
                    try:
                        pair = template_pair(name, row, path, row['spans'] if collection == 'opening_rows' else None)
                    except ArchitecturalRegularizationError:
                        continue
                    row['p1'], row['p2'] = pair
                    audit['changes'].append(dict(type='aperture_host_link', path=path, endpoints=pair))
            for row in template.get('window_rows', []):
                axis = 0 if row['facade'] in ('East','West') else 1
                new_spans, planes = [], []
                for lo,hi in row['spans']:
                    pair = [[row['plane'],lo],[row['plane'],hi]] if axis == 0 else [[lo,row['plane']],[hi,row['plane']]]
                    owners = []
                    for sid in (s['id'] for s in original['templates'][name]['spaces']):
                        polygon = old_polygons[(name,sid)]
                        if any(LineString(edge).covers(LineString(pair)) for edge in polygon):
                            owners.append(sid)
                    if len(owners) != 1:
                        reject('ambiguous_window_host', f'templates.{name}.window_rows', 'window span needs one original host')
                    moved = move_pair(name, owners[0], *pair, f'templates.{name}.window_rows')
                    if abs(moved[0][axis]-moved[1][axis]) > EPS:
                        reject('window_direction_changed', f'templates.{name}.window_rows', 'cardinal window facade must stay cardinal')
                    planes.append(moved[0][axis])
                    new_spans.append(sorted([p[1-axis] for p in moved]))
                if max(planes)-min(planes)>EPS:
                    reject('window_row_split', f'templates.{name}.window_rows', 'window row hosts no longer share one plane')
                row['plane'], row['spans'] = planes[0], new_spans

        finish_stage('template_apertures', ['global_apertures', 'vertical', 'contacts'])
        from src.agent.geometry.parametric_proposal import expand_parametric_instances
        old_instances = expand_parametric_instances(original)
        by_id = {r['id']: r for r in old_instances}
        from src.agent.geometry.parametric_continuity import declared_continuity_members
        continuous_members = declared_continuity_members(original)
        continuous_hosts = {row['space_id']: row['member_space_ids'] for row in continuous_members}
        for row in continuous_members:
            members = [by_id[sid.split(':', 1)[0]] for sid in row['member_space_ids']]
            base = min(member['z'] for member in members)
            top = max(member['z'] + member['height'] for member in members)
            by_id[row['id']] = dict(id=row['id'], z=base, height=top-base)

        def global_pair(row, hosts, path):
            candidates = []
            # A derived continuous host must agree at every explicitly selected
            # member. Never choose a representative that hides differing frames.
            expanded_hosts = [member for host in hosts for member in continuous_hosts.get(host, [host])]
            for host in expanded_hosts:
                fid, sid = host.split(':',1)
                if fid not in by_id or 'template' not in by_id[fid]:
                    reject('unknown_connection_host', path, 'unknown global connection instance')
                candidates.append(move_pair(by_id[fid]['template'], sid, row['p1'], row['p2'], path))
            if not candidates or any(math.dist(a,b)>EPS for pair in candidates[1:] for a,b in zip(candidates[0],pair)):
                reject('cross_template_host_conflict', path, 'shared cross-template host anchors disagree',
                       hosts=hosts, adopted_endpoints=candidates)
            row['p1'], row['p2'] = candidates[0]

        for row in adopted.get('connections', []):
            global_pair(row, [s for s in (row['space_id'],row.get('other_space_id')) if s], f"connections[{row['id']}]")
        for row in adopted.get('core_connections', []):
            selected = set(row.get('instance_ids', []))
            for rid in row.get('repeat_ids', []):
                repeat = next((r for r in original.get('repeats', []) if r['id']==rid), None)
                if repeat is None:
                    reject('unknown_repeat', 'core_connections', 'unknown repeat selection')
                selected.update(f"{repeat['id_prefix']}{repeat['start']+i:0{repeat.get('id_padding',0)}d}"
                                for i in range(repeat['count'])
                                if f"{repeat['id_prefix']}{repeat['start']+i:0{repeat.get('id_padding',0)}d}" not in repeat.get('exclude_ids', []))
            global_pair(row, [row['core_space_id'], *[f"{fid}:{row['space']}" for fid in sorted(selected)]], f"core_connections[{row['id']}]")

        _vertical(original, adopted, policy.get('vertical', {}), grid, simplification, audit, reject, by_id)
        _protect_volume_contacts(original, adopted, audit, reject)
        restore_straight_paths(original, adopted, line_bindings, audit, reject)
        if adopted.get('mesh_frame') != original.get('mesh_frame'):
            reject('immutable_frame_changed', 'plan', 'source/mesh transform must be preserved')
        audit.update(status='adopted', adopted_sha256=_hash(adopted))
        return adopted, audit
    except ArchitecturalRegularizationError:
        raise
    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError) as exc:
        reject('invalid_modelling_contract', 'modelling', str(exc))


def _vertical(original, adopted, policy, grid, simplification, audit, reject, by_id):
    _fields(policy, {'datum_m', 'merge_groups'})
    datum = _number(policy.get('datum_m', 0))
    records = {(kind,row['id']): row for kind,key in (('instance','instances'),('repeat','repeats'))
               for row in original.get(key,[])}
    targets = {}
    for (kind, identity), row in records.items():
        base, height = _number(row['z']), _number(row['height'])
        if height <= 0:
            raise ValueError('height must be positive')
        count = row['count'] if kind == 'repeat' else 1
        targets[(kind,identity,'base')] = base
        targets[(kind,identity,'top')] = base+count*height
    levels = list(targets.values())
    forced = {}
    for (kind, identity), row in records.items():
        if kind != 'repeat':
            continue
        base, height = _number(row['z']), _number(row['height'])
        start, step = datum+_snap(base-datum,grid), _snap(height,grid)
        if step <= 0:
            reject('collapsed_repeat_height', f'repeats[{identity}]', 'repeat height collapsed')
        end_shift = abs((start+row['count']*step)-(base+row['count']*height))
        if end_shift > grid+EPS:
            reject('repeat_endpoint_conflict', f'repeats[{identity}]',
                   'uniform quantized floor height would move the main repeat endpoint by more than one grid step; declare the intended main levels', endpoint_shift_m=end_shift)
        for i in range(row['count']+1):
            old, new = base+i*height, start+i*step
            if old in forced and abs(forced[old]-new)>EPS:
                reject('repeat_anchor_conflict', f'repeats[{identity}]', 'repeats disagree on a shared elevation')
            forced[old] = new
            levels.append(old)
    groups = _cluster(levels)
    representative = {x: g[0] for g in groups for x in g}
    adopted_levels = {}
    for group in groups:
        values = [v for k,v in forced.items() if k in group]
        if values and max(values)-min(values)>EPS:
            reject('repeat_anchor_conflict', 'vertical', 'coincident repeat anchors disagree')
        adopted_levels[group[0]] = values[0] if values else datum+_snap(group[0]-datum,grid)
    explicitly_merged, used = set(), set()
    for merge in policy.get('merge_groups', []):
        _fields(merge, {'id','semantic','targets'})
        if not isinstance(merge.get('semantic'),str) or not merge['semantic'].strip() or len(merge.get('targets',[]))<2:
            raise ValueError('height merge requires semantic justification and at least two targets')
        keys, roles = [], []
        for t in merge['targets']:
            _fields(t, {'kind','id','edge'})
            key = (t['kind'],t['id'],t['edge'])
            if key not in targets or key in used:
                raise ValueError('unknown or repeated height merge target')
            used.add(key); keys.append(key)
            row = records[key[:2]]
            roles.append({s['role'] for s in original['templates'][row['template']]['spaces']})
        if any(role != roles[0] for role in roles[1:]) or len({k[2] for k in keys}) != 1:
            reject('height_semantic_conflict', f"vertical.merge_groups[{merge.get('id')}]", 'merge must retain compatible uses and base/top meaning')
        values = [targets[k] for k in keys]
        if max(values)-min(values)>simplification+EPS:
            reject('major_level_merge', f"vertical.merge_groups[{merge.get('id')}]", 'declared merge exceeds the simplification tolerance')
        reps = {representative[v] for v in values}
        reference = adopted_levels[representative[values[0]]]
        for rep in reps:
            if any(k in forced for k in next(g for g in groups if g[0]==rep)) and abs(adopted_levels[rep]-reference)>EPS:
                reject('repeat_merge_conflict', 'vertical.merge_groups', 'merge conflicts with a uniform repeat height')
            adopted_levels[rep] = reference
        explicitly_merged.update(frozenset((a,b)) for a in reps for b in reps if a!=b)
        audit['changes'].append(dict(type='declared_height_merge', id=merge.get('id'),
            semantic=merge['semantic'], observed_levels_m=values, adopted_level_m=reference))
    reps = sorted(adopted_levels)
    for i,a in enumerate(reps):
        for b in reps[i+1:]:
            if abs(adopted_levels[a]-adopted_levels[b])<=EPS and frozenset((a,b)) not in explicitly_merged:
                audit['diagnostics'].append(dict(type='rounded_height_coincidence', observed_levels_m=[a,b],
                    adopted_level_m=adopted_levels[a], action='retain volume identities; verify contact relationships'))
            if adopted_levels[a]>adopted_levels[b]+EPS:
                reject('height_order_changed', 'vertical', 'main height ordering would change')

    def z(value):
        candidates = [rep for rep in reps if abs(value-rep)<=SEAM_M+1e-10]
        if not candidates:
            raise ValueError('missing shared height anchor')
        return adopted_levels[min(candidates,key=lambda rep:abs(value-rep))]

    for kind,key in (('instance','instances'),('repeat','repeats')):
        for row in adopted.get(key,[]):
            old = records[(kind,row['id'])]
            base, top = targets[(kind,row['id'],'base')],targets[(kind,row['id'],'top')]
            row['z'] = round(z(base),9)
            row['height'] = round((z(top)-z(base))/(row['count'] if kind=='repeat' else 1),9)
            if row['height']<=0:
                reject('collapsed_height', f"{key}[{row['id']}]", 'height adoption collapses a real volume')
            if row['z']!=old['z'] or row['height']!=old['height']:
                audit['changes'].append(dict(type='main_height', path=f"{key}[{row['id']}]",
                    before=[base,old['height'],top],after=[row['z'],row['height'],z(top)]))
    for row in adopted.get('connections',[]):
        hosts = [by_id[s.split(':',1)[0]] for s in (row['space_id'],row.get('other_space_id')) if s]
        floor = max(hosts,key=lambda h:h['z'])
        shift = z(floor['z'])-floor['z']
        row['z'] = [round(_number(v)+shift,9) for v in row['z']]
        for host in hosts:
            if row['z'][0] < z(host['z'])-EPS or row['z'][1] > z(host['z']+host['height'])+EPS:
                reject('aperture_height_conflict', f"connections[{row['id']}]", 'adopted opening exceeds a declared host height')
    # Relative aperture heights stay relative to adopted storeys; never clip.
    from src.agent.geometry.parametric_proposal import expand_parametric_instances
    for inst in expand_parametric_instances(adopted):
        template = adopted['templates'][inst['template']]
        for collection in ('doors','opening_rows','window_rows'):
            for row in template.get(collection,[]):
                if row['z'][0]<0 or row['z'][1]>inst['height']+EPS:
                    reject('aperture_height_conflict', f"{inst['id']}:{row['id']}", 'adopted main height would clip an existing opening')


def _protect_volume_contacts(original, adopted, audit, reject):
    """Rounding may align separate roof tops, but cannot close a real gap.

    This checks prism contact classes, not a substitute for the source kernel's
    complete host, containment and topology validation. A 20 micrometre input
    seam is treated as contact; no transitive proximity groups are formed.
    """
    from src.agent.geometry.parametric_proposal import expand_parametric_instances

    def volumes(plan):
        result = {}
        for inst in expand_parametric_instances(plan):
            template = plan['templates'][inst['template']]
            if _curved(template):
                continue
            for space in template['spaces']:
                result[f"{inst['id']}:{space['id']}"] = (
                    Polygon(space['polygon'], space.get('holes', [])), inst['z'], inst['z']+inst['height'])
        return result

    def relation(a, b):
        pa, za, ta = a
        pb, zb, tb = b
        overlap_z = min(ta, tb)-max(za, zb)
        if overlap_z < -SEAM_M or pa.distance(pb) > SEAM_M:
            return 'separate'
        area = pa.intersection(pb).area
        # A small overlap is the other sign of the same input seam tolerance.
        # Erode half the fixed tolerance on each side; do not grow a cluster.
        interior_overlap = (area > EPS and
            pa.buffer(-SEAM_M/2).intersection(pb.buffer(-SEAM_M/2)).area > EPS)
        if interior_overlap:
            return 'volume_overlap' if overlap_z > SEAM_M else 'horizontal_contact'
        if overlap_z <= SEAM_M:
            return 'edge_or_point_contact'
        return 'lateral_contact'

    before, after = volumes(original), volumes(adopted)
    ids = list(before)
    for index, a in enumerate(ids):
        for b in ids[index+1:]:
            old, new = relation(before[a], before[b]), relation(after[a], after[b])
            if old != new:
                reject('volume_contact_changed', f'spaces[{a},{b}]',
                       'parameter adoption would change a real volume gap/contact; declare intended geometry explicitly',
                       before=old, after=new)
    audit['contact_check'] = dict(status='passed', straight_prism_count=len(ids),
        excluded='curved templates; caller must validate all source geometry')


def regularize_architectural_proposal(proposal, *, policy=None):
    """Adopt explicit generic floor/cell geometry through the same plan rules.

    IDs and all metadata are copied verbatim; the adapter writes back only
    polygons/bounds, floor placement and the coordinates of existing apertures.
    Generic frames use floor names in ``templates``; space_intents keys use
    ``floor_name:cell_id``. An omitted floor footprint uses the existing shared
    rectangle declaration in geometry.footprint_x/y, never a hull of the cells.
    Ambiguous window hosts and stale component bindings are rejected.
    """
    original = copy.deepcopy(proposal)
    audit = dict(schema_version='architectural_regularization_v1', status='pending',
                 changes=[], diagnostics=[], not_assessed=[], rejections=[], model_calls=0,
                 geometry_fitted=False, input_sha256=_hash(original))

    def reject(code, path, message):
        audit['status'] = 'rejected'
        audit['rejections'].append(dict(code=code, path=path, message=message))
        raise ArchitecturalRegularizationError(message, audit)

    if policy is None and 'modelling' not in original:
        audit.update(status='not_requested', adopted_sha256=_hash(original))
        return original, audit
    try:
        geometry = original['geometry']
        if geometry.get('schema_version', '2') not in ('2', 'source_polygon_v1'):
            reject('unsupported_generic_geometry', 'geometry.schema_version',
                   'architectural adoption requires schema 2 or source_polygon_v1; use an explicit parametric plan')
        plan = dict(templates={}, instances=[], connections=[], assumptions=[], unresolved=[])
        floor_names, cell_ids, cell_rows, openings, windows = {}, {}, {}, [], []
        footprint_bases = []
        for index, floor in enumerate(geometry['floors']):
            name, fid = floor['name'], f'F{index}'
            if name in floor_names:
                reject('duplicate_floor', 'geometry.floors', 'floor names must be unique')
            floor_names[name] = fid
            footprint = copy.deepcopy(floor.get('footprint'))
            if footprint is None:
                x0,x1 = map(_number, geometry['footprint_x'])
                y0,y1 = map(_number, geometry['footprint_y'])
                if x0 >= x1 or y0 >= y1:
                    reject('invalid_common_footprint', f'geometry.floors[{name}]',
                           'the declared common footprint intervals must have positive length')
                footprint = {'vertices': [[x0,y0],[x1,y0],[x1,y1],[x0,y1]]}
                footprint_bases.append(dict(type='footprint_declaration', path=f'geometry.floors[{name}].footprint',
                    basis='legacy_common_footprint', footprint_x=[x0,x1], footprint_y=[y0,y1]))
            template = dict(footprint=footprint, spaces=[])
            for j, cell in enumerate(floor['cells']):
                cid, sid = cell['id'], f'S{j}'
                if cid in cell_ids:
                    reject('duplicate_cell', 'geometry.floors.cells', 'source cell IDs must be globally unique')
                cell_ids[cid] = f'{fid}:{sid}'
                cell_rows[cid] = (fid, sid, cell)
                space = dict(id=sid, role=cell.get('role', 'unknown'), source_refs=copy.deepcopy(cell.get('source_refs', [])))
                for key in ('polygon', 'holes', 'boundary_path', 'assumptions', 'role_evidence'):
                    if key in cell:
                        space[key] = copy.deepcopy(cell[key])
                if 'polygon' not in space and not space.get('boundary_path'):
                    space['rect'] = [cell['x'][0], cell['y'][0], cell['x'][1], cell['y'][1]]
                template['spaces'].append(space)
            plan['templates'][fid] = template
            plan['instances'].append(dict(id=fid, template=fid, z=floor['z_floor'], height=floor['ceiling_height']))

        for row in geometry.get('openings', []):
            item = copy.deepcopy(row)
            for key in ('space_id', 'other_space_id'):
                if item.get(key) is not None:
                    item[key] = cell_ids[item[key]]
            plan['connections'].append(item)
            openings.append(len(plan['connections'])-1)
        for row in geometry.get('windows', []):
            floor_id = floor_names[row['floor']]
            declared = row.get('room')
            if declared is not None and cell_rows[declared][0] != floor_id:
                reject('window_floor_host_conflict', f"windows[{row['id']}]", 'window floor and declared host disagree')
            axis = 0 if row['facade'] in ('East', 'West') else 1
            if row['facade'] not in ('East', 'West', 'North', 'South'):
                reject('unknown_window_facade', f"windows[{row['id']}]", 'window facade must be cardinal')
            desired = {'North': (0, 1), 'South': (0, -1), 'East': (1, 0), 'West': (-1, 0)}[row['facade']]
            candidates = []
            floor_polygons = []
            for fid, _, cell in cell_rows.values():
                if fid == floor_id:
                    p = cell.get('polygon')
                    if p is None and not cell.get('boundary_path'):
                        x,y = cell['x'],cell['y']
                        p = [[x[0],y[0]],[x[1],y[0]],[x[1],y[1]],[x[0],y[1]]]
                    if p is not None:
                        floor_polygons.append(Polygon(p, cell.get('holes', [])))
            exterior = unary_union(floor_polygons).boundary
            for cid, (fid, sid, cell) in cell_rows.items():
                if fid != floor_id or (declared is not None and declared != cid):
                    continue
                points = cell.get('polygon')
                if not points and not cell.get('boundary_path'):
                    x, y = cell['x'], cell['y']
                    points = [[x[0],y[0]],[x[1],y[0]],[x[1],y[1]],[x[0],y[1]]]
                if not points:
                    reject('implicit_window_host', f"windows[{row['id']}]", 'curve window host needs saved derived points; use a parametric plan')
                orientation = 1 if Polygon(points).exterior.is_ccw else -1
                for a, b in zip(points, points[1:]+points[:1]):
                    normal = (orientation*(b[1]-a[1]), orientation*(a[0]-b[0]))
                    if (abs(a[axis]-b[axis]) <= EPS and sum(x*y for x,y in zip(normal,desired)) > 0
                            and min(a[1-axis],b[1-axis])-EPS <= row['span'][0]
                            and max(a[1-axis],b[1-axis])+EPS >= row['span'][1]):
                        segment = ([[a[axis],v] for v in row['span']] if axis == 0
                                   else [[v,a[axis]] for v in row['span']])
                        if exterior.covers(LineString(segment)):
                            candidates.append((cid, a[axis]))
            if len(candidates) != 1:
                reject('ambiguous_window_host', f"windows[{row['id']}]", 'window needs exactly one complete original facade edge')
            cid, plane = candidates[0]
            p1, p2 = ([[plane, v] for v in row['span']] if axis == 0
                      else [[v, plane] for v in row['span']])
            plan['connections'].append(dict(id=row['id'], space_id=cell_ids[cid], other_space_id=None,
                                            p1=p1, p2=p2, z=copy.deepcopy(row['z'])))
            windows.append((len(plan['connections'])-1, axis))
        if 'mesh_frame' in original:
            plan['mesh_frame'] = copy.deepcopy(original['mesh_frame'])
        if 'boundary_curves' in geometry:
            plan['boundary_curves'] = copy.deepcopy(geometry['boundary_curves'])

        def adapt_policy(value):
            value = copy.deepcopy(value)
            if value is None:
                return None
            for frame in value.get('frames', []):
                frame['templates'] = [floor_names[name] for name in frame['templates']]
            names = {f"{floor['name']}:{cell['id']}": f"{floor_names[floor['name']]}:{cell_rows[cell['id']][1]}"
                     for floor in geometry['floors'] for cell in floor['cells']}
            if 'space_intents' in value:
                value['space_intents'] = {names[key]: intent for key, intent in value['space_intents'].items()}
            for group in value.get('vertical', {}).get('merge_groups', []):
                for target in group['targets']:
                    if target['kind'] != 'instance':
                        reject('generic_repeat_target', 'vertical.merge_groups', 'generic height targets must name a floor instance')
                    target['id'] = floor_names[target['id']]
            return value

        if 'modelling' in original:
            plan['modelling'] = adapt_policy(original['modelling'])
        adopted, audit = regularize_architectural_plan(plan, policy=adapt_policy(policy))
        audit['diagnostics'].extend(footprint_bases)
        result = copy.deepcopy(original)
        result.pop('modelling', None)
        if 'boundary_curves' in adopted:
            result['geometry']['boundary_curves'] = copy.deepcopy(adopted['boundary_curves'])
            if adopted['boundary_curves'] != geometry.get('boundary_curves'):
                result['geometry'].pop('curve_discretization', None)
        points, curve_samples = [], None
        for index, floor in enumerate(result['geometry']['floors']):
            fid = f'F{index}'
            template, instance = adopted['templates'][fid], adopted['instances'][index]
            floor['footprint'] = copy.deepcopy(template['footprint'])
            floor['z_floor'], floor['ceiling_height'] = instance['z'], instance['height']
            footprint = floor['footprint']
            vertices = footprint if isinstance(footprint, list) else footprint.get('vertices')
            if vertices is None:
                reject('implicit_curve_points', f'geometry.floors[{floor["name"]}]',
                       'generic curve footprints need their saved derived vertices; use a parametric plan')
            points.extend(vertices)
            for cell, space in zip(floor['cells'], template['spaces']):
                polygon = space.get('polygon')
                if polygon is None and space.get('boundary_path'):
                    from src.agent.geometry.source_curves import discretize_curves, path_vertices
                    if curve_samples is None:
                        _, curve_samples = discretize_curves(adopted['boundary_curves'])
                    polygon, _ = path_vertices(space['boundary_path'], curve_samples)
                if polygon is not None:
                    cell['polygon'] = copy.deepcopy(polygon)
                    if space.get('holes'):
                        cell['holes'] = copy.deepcopy(space['holes'])
                    cell['x'] = [min(p[0] for p in cell['polygon']), max(p[0] for p in cell['polygon'])]
                    cell['y'] = [min(p[1] for p in cell['polygon']), max(p[1] for p in cell['polygon'])]
        result['geometry']['footprint_x'] = [min(p[0] for p in points), max(p[0] for p in points)]
        result['geometry']['footprint_y'] = [min(p[1] for p in points), max(p[1] for p in points)]
        for row, index in zip(result['geometry'].get('openings', []), openings):
            for key in ('p1', 'p2', 'z', 'curve_segment'):
                if key in adopted['connections'][index]:
                    row[key] = copy.deepcopy(adopted['connections'][index][key])
        for row, (index, axis) in zip(result['geometry'].get('windows', []), windows):
            connection = adopted['connections'][index]
            if abs(connection['p1'][axis]-connection['p2'][axis]) > EPS:
                reject('window_direction_changed', f"windows[{row['id']}]", 'cardinal window host no longer cardinal')
            row['span'] = sorted([connection['p1'][1-axis], connection['p2'][1-axis]])
            row['z'] = copy.deepcopy(connection['z'])
        if result['geometry'] != original['geometry'] and result.get('component_attributes'):
            reject('component_rebinding_required', 'component_attributes',
                   'geometry adoption requires explicit rebinding of existing component host identities; thickness metadata was not changed')
        audit.update(input_sha256=_hash(original), adopted_sha256=_hash(result), adapter='generic_floor_cells',
                     source_id_map={v:k for k,v in cell_ids.items()})
        return result, audit
    except ArchitecturalRegularizationError:
        raise
    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError) as exc:
        reject('unsupported_generic_geometry', 'geometry', f'{exc}; supply explicit geometry or use a parametric plan')
