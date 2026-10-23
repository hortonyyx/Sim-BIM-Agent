"""Expand explicit floor templates and aperture rows; no architectural inference.

The model supplies every partition, row, repetition and connection. This code
only repeats declared geometry, offsets relative heights and resolves complete
window spans to unique outward edges. No snapping, trimming or auto partitioning.
"""
from __future__ import annotations

import copy
import json
import math

from shapely.geometry import Polygon
from shapely.geometry.polygon import orient

from src.agent.geometry.input_scale import check_floor_placements, check_planar_scale, ring_spans


def fields(value, required, optional=()):
    if not isinstance(value, dict):
        raise ValueError('expected an object')
    missing, extra = set(required) - value.keys(), value.keys() - set(required) - set(optional)
    if missing or extra:
        raise ValueError(f'missing fields {sorted(missing)}; unknown fields {sorted(extra)}')


def identifier(value):
    if not isinstance(value, str) or not value.strip() or ':' in value:
        raise ValueError('IDs must be nonempty strings without colons')
    return value


def number(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError('coordinates/heights must be finite numbers')
    return float(value)


def interval(value):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError('expected a two-value interval')
    lo, hi = map(number, value)
    if lo >= hi:
        raise ValueError('interval must have positive length')
    return [lo, hi]


def ring(value, *, simple_polygon=False):
    minimum = 3 if simple_polygon else 4
    if not isinstance(value, list) or len(value) < minimum:
        raise ValueError(f'polygon needs at least {minimum} points')
    points = []
    for p in value:
        if not isinstance(p, list) or len(p) != 2:
            raise ValueError('polygon point must contain x,y')
        points.append([number(v) for v in p])
    if points[0] == points[-1]:
        points.pop()
    poly = Polygon(points)
    if not poly.is_valid or poly.area <= 0 or poly.interiors:
        raise ValueError('polygon must be a valid positive-area single ring')
    for a, b in zip(points, points[1:] + points[:1]):
        if a == b or (not simple_polygon and a[0] != b[0] and a[1] != b[1]):
            raise ValueError('polygon edges must be nonzero and, for schema 2, orthogonal')
    return [list(p) for p in list(orient(poly, sign=1).exterior.coords)[:-1]]


def strings(value, nonempty=False):
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError('expected a list of nonblank strings')
    if nonempty and not value:
        raise ValueError('source_refs must explain observation or hypothesis')
    return list(value)


def _integer(value, label, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f'{label} must be an integer from {minimum} to {maximum}')
    return value


def _instance_records(plan):
    """Expand only declared sequences; explicit exceptions never silently win."""
    explicit = plan['instances']
    repeats = plan.get('repeats', [])
    if not isinstance(explicit, list) or not isinstance(repeats, list):
        raise ValueError('instances and repeats must be lists')
    if len(explicit) > 100:
        raise ValueError('supply 1 to 100 expanded floor/volume instances')
    instances, ids, groups = [], set(), {}
    for instance in explicit:
        fields(instance, ('id', 'template', 'z', 'height'),
               ('spanning_space_ids', 'source_refs', 'assumptions'))
        fid = identifier(instance['id'])
        if fid in ids:
            raise ValueError(f'duplicate instance {fid}')
        ids.add(fid)
        instances.append(copy.deepcopy(instance))
    for repeat in repeats:
        fields(repeat, ('id', 'template', 'id_prefix', 'start', 'count', 'z', 'height', 'source_refs'),
               ('id_padding', 'spanning_space_ids', 'exclude_ids', 'assumptions'))
        rid = identifier(repeat['id'])
        if rid in groups:
            raise ValueError(f'duplicate repeat {rid}')
        prefix = identifier(repeat['id_prefix'])
        start = _integer(repeat['start'], 'repeat start', 0, 1000000)
        count = _integer(repeat['count'], 'repeat count', 1, 100)
        padding = _integer(repeat.get('id_padding', 0), 'id_padding', 0, 8)
        base, height = number(repeat['z']), number(repeat['height'])
        if height <= 0:
            raise ValueError('repeat height must be positive')
        refs = strings(repeat['source_refs'], True)
        assumptions = strings(repeat.get('assumptions', []))
        members = strings(repeat.get('spanning_space_ids', []))
        generated = [f'{prefix}{start+i:0{padding}d}' for i in range(count)]
        excluded = strings(repeat.get('exclude_ids', []))
        if len(set(excluded)) != len(excluded) or not set(excluded) <= set(generated):
            raise ValueError(f'{rid}: exclude_ids must be unique IDs in this repeat')
        explicit_ids = {instance['id'] for instance in explicit}
        if not set(excluded) <= explicit_ids:
            raise ValueError(f'{rid}: every excluded ID needs a complete explicit instance')
        groups[rid] = []
        for index, fid in enumerate(generated):
            if fid in excluded:
                continue
            if fid in ids:
                raise ValueError(f'duplicate instance {fid}; explicitly exclude a replacement')
            if len(instances) >= 100:
                raise ValueError('supply 1 to 100 expanded floor/volume instances')
            ids.add(fid)
            groups[rid].append(fid)
            instances.append(dict(id=fid, template=repeat['template'],
                z=round(base + index * height, 9), height=height,
                spanning_space_ids=list(members), source_refs=list(refs), assumptions=list(assumptions)))
    if not 1 <= len(instances) <= 100:
        raise ValueError('supply 1 to 100 expanded floor/volume instances')
    return instances, groups


def expand_parametric_instances(plan: dict) -> list[dict]:
    """Return deterministic instances for representative views; keep compact input intact."""
    return _instance_records(plan)[0]


def _evidence(row, instance):
    return dict(source_refs=strings(row['source_refs'], True) + strings(instance.get('source_refs', [])),
                assumptions=strings(row.get('assumptions', [])) + strings(instance.get('assumptions', [])))


def _role_evidence(space):
    from src.agent.geometry.parametric_validation import validated_role_evidence
    return validated_role_evidence(space)


def _core_openings(plan, instances, groups, floors, existing):
    declarations = plan.get('core_connections', [])
    if not isinstance(declarations, list):
        raise ValueError('core_connections must be a list')
    instance_map = {row['id']: row for row in instances}
    floor_map = {row['name']: row for row in floors}
    spaces = {cell['id']: floor for floor in floors for cell in floor['cells']}
    result, ids, served = [], set(), set()
    for declaration in declarations:
        fields(declaration, ('id', 'core_space_id', 'space', 'p1', 'p2', 'z', 'source_refs'),
               ('instance_ids', 'repeat_ids', 'kind', 'state', 'assumptions'))
        cid = identifier(declaration['id'])
        if cid in ids:
            raise ValueError(f'duplicate core connection declaration {cid}')
        ids.add(cid)
        local = identifier(declaration['space'])
        core = declaration['core_space_id']
        if not isinstance(core, str) or core not in spaces:
            raise ValueError(f'{cid}: unknown core space {core}')
        selected = strings(declaration.get('instance_ids', []))
        repeat_ids = strings(declaration.get('repeat_ids', []))
        for rid in repeat_ids:
            if rid not in groups:
                raise ValueError(f'{cid}: unknown repeat {rid}')
            selected.extend(groups[rid])
        if not selected or len(set(selected)) != len(selected) or len(set(repeat_ids)) != len(repeat_ids):
            raise ValueError(f'{cid}: service instances must be nonempty and unique')
        kind = declaration.get('kind', 'door')
        state = declaration.get('state', 'open' if kind == 'open' else 'unknown')
        from src.agent.geometry.parametric_validation import core_connection_field_issues
        if core_connection_field_issues(kind, state):
            raise ValueError('core connections must be doors or open passages with a valid state')
        points = [declaration['p1'], declaration['p2']]
        if any(not isinstance(point, list) or len(point) != 2 for point in points):
            raise ValueError('core connection endpoints must contain x,y')
        p1, p2 = [[number(value) for value in point] for point in points]
        if p1 == p2:
            raise ValueError('core connection must have positive width')
        relative_z = interval(declaration['z'])
        for fid in selected:
            if fid not in instance_map:
                raise ValueError(f'{cid}: unknown service instance {fid}')
            instance, floor, core_floor = instance_map[fid], floor_map[fid], spaces[core]
            space = f'{fid}:{local}'
            if space not in spaces or space == core:
                raise ValueError(f'{cid}: unknown or invalid local service space {space}')
            if core not in instance.get('spanning_space_ids', []):
                raise ValueError(f'{cid}: {core} must be an explicit spanning member of {fid}')
            base, height = floor['z_floor'], floor['ceiling_height']
            if core_floor['z_floor'] > base + 1e-9 or core_floor['z_floor'] + core_floor['ceiling_height'] < base + height - 1e-9:
                raise ValueError(f'{cid}: core does not cover the whole service instance {fid}')
            if relative_z[0] < 0 or relative_z[1] > height:
                raise ValueError(f'{cid}: core connection height outside service instance {fid}')
            if (core, fid) in served:
                raise ValueError(f'duplicate core service connection for {core} at {fid}')
            served.add((core, fid))
            z = [round(base + value, 9) for value in relative_z]
            for row in existing:
                if {row.get('space_id'), row.get('other_space_id')} == {space, core}:
                    old_z = interval(row.get('z'))
                    if min(old_z[1], z[1]) > max(old_z[0], z[0]):
                        raise ValueError(f'{cid}: explicit connection already serves {space} and {core}')
            result.append(dict(id=f'{fid}:core:{cid}', space_id=space, other_space_id=core,
                kind=kind, state=state, p1=list(p1), p2=list(p2), z=z,
                **_evidence(declaration, instance)))
            if len(result) + len(existing) > 5000:
                raise ValueError('aperture expansion exceeds experiment limit')
    return result


def _source_template(template, curves):
    """Use the common source geometry expansion for declared curved rings."""
    from src.agent.geometry.source_curves import expand_source_polygon_geometry
    cells = []
    for space in template['spaces']:
        if space.get('vertical_sections'):
            raise ValueError('vertical_sections uses absolute elevations and requires an explicit source_polygon_v1 '
                             'build_bim proposal; it is not a reusable parametric template')
        fields(space, ('id', 'role', 'source_refs'), ('polygon', 'rect', 'boundary_path', 'holes', 'assumptions', 'role_evidence'))
        if sum(key in space for key in ('polygon', 'rect', 'boundary_path')) != 1:
            raise ValueError('space needs exactly one of polygon, rect or boundary_path')
        cell = copy.deepcopy(space)
        if 'rect' in cell:
            x0, y0, x1, y1 = map(number, cell.pop('rect'))
            if x0 >= x1 or y0 >= y1:
                raise ValueError('rect is [xmin,ymin,xmax,ymax] with positive dimensions')
            cell['polygon'] = [[x0,y0],[x1,y0],[x1,y1],[x0,y1]]
        cells.append(cell)
    footprint = template['footprint']
    if isinstance(footprint, list):
        footprint = {'vertices': footprint}
    expanded = expand_source_polygon_geometry({'schema_version': 'source_polygon_v1',
        'boundary_curves': curves,
        'floors': [{'footprint': footprint, 'cells': cells}]})
    return expanded['floors'][0]


def expand_parametric_proposal(plan: dict, *, expansion_audit: dict | None = None) -> dict:
    from src.agent.geometry.parametric_primitives import normalize_parametric_primitives
    plan = normalize_parametric_primitives(plan)
    fields(plan, ('templates', 'instances', 'assumptions', 'unresolved'),
           ('connections', 'schema_version', 'mesh_frame', 'boundary_curves', 'repeats',
            'core_connections', 'continuous_spaces', 'separated_spaces'))
    json.dumps(plan, allow_nan=False)
    schema_version = plan.get('schema_version', '2')
    if schema_version not in ('2', 'source_polygon_v1'):
        raise ValueError('parametric schema_version must be 2 or source_polygon_v1')
    simple_polygon = schema_version == 'source_polygon_v1'
    if 'boundary_curves' in plan and not simple_polygon:
        raise ValueError('boundary_curves requires source_polygon_v1')
    if not isinstance(plan['templates'], dict) or not plan['templates']:
        raise ValueError('templates must be a nonempty object')
    instances, groups = _instance_records(plan)
    check_floor_placements(
        (f"plan.instances[{i}].z", instance.get('z'), f"plan.instances[{i}].height", instance.get('height'))
        for i, instance in enumerate(instances))
    floors, windows, openings, all_points = [], [], [], []
    expanded_templates = {}
    ids = set()
    for instance_index, instance in enumerate(instances):
        fid = identifier(instance['id'])
        if fid in ids:
            raise ValueError(f'duplicate instance {fid}')
        ids.add(fid)
        base, height = number(instance['z']), number(instance['height'])
        if height <= 0:
            raise ValueError('instance height must be positive')
        template = plan['templates'][instance['template']]
        fields(template, ('footprint', 'spaces'), ('window_rows', 'doors', 'opening_rows'))
        if simple_polygon and instance['template'] not in expanded_templates:
            expanded_templates[instance['template']] = _source_template(template, plan.get('boundary_curves', {}))
        source_template = expanded_templates.get(instance['template'])
        footprint_value = source_template['footprint']['vertices'] if source_template else template['footprint']
        check_planar_scale(
            ring_spans(footprint_value, f"plan.templates[{instance['template']!r}].footprint"),
            [(f"plan.instances[{instance_index}].height", height)],
        )
        footprint = ring(footprint_value, simple_polygon=simple_polygon)
        all_points.extend(footprint)
        cells, local_ids = [], set()
        for space in source_template['cells'] if source_template else template['spaces']:
            if not source_template:
                fields(space, ('id', 'role', 'source_refs'), ('polygon', 'rect', 'assumptions', 'role_evidence'))
            sid = identifier(space['id'])
            if sid in local_ids:
                raise ValueError(f'duplicate space {sid}')
            local_ids.add(sid)
            if not source_template and ('polygon' in space) == ('rect' in space):
                raise ValueError('space needs exactly one of polygon or rect')
            if 'rect' in space:
                x0, y0, x1, y1 = map(number, space['rect'])
                if x0 >= x1 or y0 >= y1:
                    raise ValueError('rect is [xmin,ymin,xmax,ymax] with positive dimensions')
                polygon = ring([[x0,y0],[x1,y0],[x1,y1],[x0,y1]], simple_polygon=simple_polygon)
            else:
                polygon = ring(space['polygon'], simple_polygon=simple_polygon)
            cells.append(dict(id=f'{fid}:{sid}', role=space['role'], polygon=polygon,
                x=[min(p[0] for p in polygon), max(p[0] for p in polygon)],
                y=[min(p[1] for p in polygon), max(p[1] for p in polygon)],
                **_evidence(space, instance), **_role_evidence(space)))
            if 'boundary_path' in space:
                cells[-1]['boundary_path'] = copy.deepcopy(space['boundary_path'])
            if space.get('holes'):
                cells[-1]['holes'] = copy.deepcopy(space['holes'])
        if not cells:
            raise ValueError('instance requires explicitly declared spaces')
        spanning_space_ids = strings(instance.get('spanning_space_ids', []))
        footprint_record = copy.deepcopy(source_template['footprint']) if source_template else {'vertices': footprint}
        floors.append(dict(name=fid, z_floor=base, ceiling_height=height,
                           footprint=footprint_record, cells=cells,
                           **({'spanning_space_ids': spanning_space_ids}
                              if spanning_space_ids else {})))
        row_ids = set()
        for row in template.get('window_rows', []):
            fields(row, ('id', 'facade', 'plane', 'spans', 'z', 'source_refs'), ('assumptions',))
            rid = identifier(row['id'])
            if rid in row_ids:
                raise ValueError(f'duplicate window row {rid}')
            row_ids.add(rid)
            facade = row['facade']
            if facade not in ('North', 'South', 'East', 'West'):
                raise ValueError('unknown facade')
            axis = 0 if facade in ('East', 'West') else 1
            along = 1-axis
            plane = number(row['plane'])
            z = interval(row['z'])
            if z[0] < 0 or z[1] > height:
                raise ValueError(f'{fid}:{rid}: window height outside instance')
            desired = {'North':(0,1), 'South':(0,-1), 'East':(1,0), 'West':(-1,0)}[facade]
            for i, span in enumerate(row['spans'], 1):
                lo, hi = interval(span)
                owners = set()
                for cell in cells:
                    polygon = cell['polygon']
                    for a,b in zip(polygon, polygon[1:]+polygon[:1]):
                        if abs(a[axis]-plane)>1e-7 or abs(b[axis]-plane)>1e-7:
                            continue
                        normal = (b[1]-a[1], a[0]-b[0])
                        if sum(n*d for n,d in zip(normal, desired)) <= 0:
                            continue
                        if min(a[along],b[along])-1e-7 <= lo and max(a[along],b[along])+1e-7 >= hi:
                            owners.add(cell['id'])
                if len(owners) != 1:
                    raise ValueError(f'{fid}:{rid}:{i}: span {span} on {facade} plane {plane} has {len(owners)} complete hosts; preserve aperture and recheck partitions')
                windows.append(dict(id=f'{fid}:{rid}:{i}', floor=fid, facade=facade,
                    span=[lo,hi], z=[round(base+v,9) for v in z], room=next(iter(owners)),
                    **_evidence(row, instance)))
        for row in template.get('opening_rows', []):
            if not simple_polygon:
                raise ValueError('opening_rows requires source_polygon_v1')
            from src.agent.geometry.parametric_validation import (
                OPENING_ROW_REQUIRED_FIELDS, OPENING_ROW_OPTIONAL_FIELDS)
            fields(row, OPENING_ROW_REQUIRED_FIELDS, OPENING_ROW_OPTIONAL_FIELDS)
            rid = identifier(row['id'])
            if rid in row_ids:
                raise ValueError(f'duplicate aperture row {rid}')
            row_ids.add(rid)
            for sid in (row['space'], row.get('other_space')):
                if sid is not None and sid not in local_ids:
                    raise ValueError(f'aperture row references unknown local space {sid}')
            if row['space'] is None:
                raise ValueError('aperture row needs a host space')
            points = [row['p1'], row['p2']]
            if any(not isinstance(p, list) or len(p) != 2 for p in points):
                raise ValueError('aperture baseline endpoints must contain x,y')
            start, end = [[number(v) for v in p] for p in points]
            length = math.dist(start, end)
            if length <= 0:
                raise ValueError('aperture baseline must have positive length')
            z = interval(row['z'])
            if z[0] < 0 or z[1] > height:
                raise ValueError('aperture row height outside instance')
            if not isinstance(row['spans'], list) or not row['spans']:
                raise ValueError('aperture row requires explicit spans in metres along p1 to p2')
            for i, span in enumerate(row['spans'], 1):
                lo, hi = interval(span)
                if lo < 0 or hi > length:
                    raise ValueError('aperture span outside baseline; no clipping is performed')
                endpoints = [[round(a + (b-a)*offset/length, 9) for a, b in zip(start, end)]
                             for offset in (lo, hi)]
                openings.append(dict(id=f'{fid}:row:{rid}:{i}', space_id=f'{fid}:{row["space"]}',
                    other_space_id=f'{fid}:{row["other_space"]}' if row.get('other_space') else None,
                    kind=row['kind'], state=row.get('state', 'open' if row['kind'] == 'open' else 'unknown'),
                    p1=endpoints[0], p2=endpoints[1], z=[round(base+v, 9) for v in z],
                    **_evidence(row, instance)))
        for door in template.get('doors', []):
            fields(door, ('id','space','other_space','p1','p2','z','source_refs'), ('kind','state','assumptions'))
            for sid in (door['space'],door['other_space']):
                if sid is not None and sid not in local_ids:
                    raise ValueError(f'door references unknown local space {sid}')
            z = interval(door['z'])
            if z[0] < 0 or z[1] > height:
                raise ValueError('door height outside instance')
            openings.append(dict(id=f'{fid}:door:{identifier(door["id"])}',
                space_id=f'{fid}:{door["space"]}',
                other_space_id=f'{fid}:{door["other_space"]}' if door['other_space'] is not None else None,
                kind=door.get('kind','door'),state=door.get('state','unknown'),
                p1=door['p1'],p2=door['p2'],z=[round(base+v,9) for v in z],
                **_evidence(door, instance)))
    # Cross-instance connections use explicit global IDs and absolute heights.
    connections = plan.get('connections', [])
    if not isinstance(connections, list) or any(not isinstance(row, dict) for row in connections):
        raise ValueError('connections must be a list of opening records')
    openings.extend(copy.deepcopy(connections))
    from src.agent.geometry.parametric_continuity import apply_continuous_spaces, expand_separated_spaces
    continuity = apply_continuous_spaces(plan, instances, groups, floors, windows, openings)
    separations = expand_separated_spaces(plan, instances, groups, floors, continuity)
    openings.extend(_core_openings(plan, instances, groups, floors, openings))
    if len(windows)+len(openings)>5000:
        raise ValueError('aperture expansion exceeds experiment limit')
    aperture_ids = [row['id'] for row in windows + openings]
    if len(set(aperture_ids)) != len(aperture_ids):
        raise ValueError('duplicate expanded aperture ID')
    proposal = dict(geometry=dict(schema_version=schema_version,
        footprint_x=[min(p[0] for p in all_points),max(p[0] for p in all_points)],
        footprint_y=[min(p[1] for p in all_points),max(p[1] for p in all_points)],
        floors=floors,windows=windows,openings=openings),
        assumptions=strings(plan['assumptions']),unresolved=strings(plan['unresolved']))
    if 'mesh_frame' in plan:
        from src.agent.geometry.mesh_bim_frame import validate_mesh_frame
        proposal['mesh_frame'] = validate_mesh_frame(copy.deepcopy(plan['mesh_frame']))
    if 'boundary_curves' in plan:
        proposal['geometry']['boundary_curves'] = copy.deepcopy(plan['boundary_curves'])
    if separations:
        proposal['geometry']['vertical_separations'] = separations
    if expansion_audit is not None and continuity:
        expansion_audit['continuous_spaces'] = continuity
    if expansion_audit is not None and separations:
        expansion_audit['separated_spaces'] = copy.deepcopy(separations)
    return proposal
