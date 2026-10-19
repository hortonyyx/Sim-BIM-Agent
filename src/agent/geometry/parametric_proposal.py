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


def _source_template(template, curves):
    """Use the common source geometry expansion for declared curved rings."""
    from src.agent.geometry.source_curves import expand_source_polygon_geometry
    cells = []
    for space in template['spaces']:
        fields(space, ('id', 'role', 'source_refs'), ('polygon', 'rect', 'boundary_path', 'assumptions'))
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


def expand_parametric_proposal(plan: dict) -> dict:
    fields(plan, ('templates', 'instances', 'assumptions', 'unresolved'),
           ('connections', 'schema_version', 'mesh_frame', 'boundary_curves'))
    json.dumps(plan, allow_nan=False)
    schema_version = plan.get('schema_version', '2')
    if schema_version not in ('2', 'source_polygon_v1'):
        raise ValueError('parametric schema_version must be 2 or source_polygon_v1')
    simple_polygon = schema_version == 'source_polygon_v1'
    if 'boundary_curves' in plan and not simple_polygon:
        raise ValueError('boundary_curves requires source_polygon_v1')
    if not isinstance(plan['templates'], dict) or not plan['templates']:
        raise ValueError('templates must be a nonempty object')
    if not isinstance(plan['instances'], list) or not 1 <= len(plan['instances']) <= 100:
        raise ValueError('supply 1 to 100 explicit floor/volume instances')
    check_floor_placements(
        (f"plan.instances[{i}].z", instance.get('z'), f"plan.instances[{i}].height", instance.get('height'))
        for i, instance in enumerate(plan['instances']) if isinstance(instance, dict))
    floors, windows, openings, all_points = [], [], [], []
    expanded_templates = {}
    ids = set()
    for instance_index, instance in enumerate(plan['instances']):
        fields(instance, ('id', 'template', 'z', 'height'), ('spanning_space_ids',))
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
                fields(space, ('id', 'role', 'source_refs'), ('polygon', 'rect', 'assumptions'))
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
                source_refs=strings(space['source_refs'], True),
                assumptions=strings(space.get('assumptions', []))))
            if 'boundary_path' in space:
                cells[-1]['boundary_path'] = copy.deepcopy(space['boundary_path'])
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
                    source_refs=strings(row['source_refs'],True), assumptions=strings(row.get('assumptions',[]))))
        for row in template.get('opening_rows', []):
            if not simple_polygon:
                raise ValueError('opening_rows requires source_polygon_v1')
            fields(row, ('id', 'space', 'p1', 'p2', 'spans', 'z', 'kind', 'source_refs'),
                   ('other_space', 'state', 'assumptions'))
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
                    source_refs=strings(row['source_refs'], True),
                    assumptions=strings(row.get('assumptions', []))))
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
                source_refs=strings(door['source_refs'],True),assumptions=strings(door.get('assumptions',[]))))
    # Cross-instance connections use explicit global IDs and absolute heights.
    openings.extend(copy.deepcopy(plan.get('connections', [])))
    if len(windows)+len(openings)>5000:
        raise ValueError('aperture expansion exceeds experiment limit')
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
    return proposal
