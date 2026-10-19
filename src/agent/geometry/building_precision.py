"""Post-regularisation source hard rules for a candidate save gate.

The source is inspected directly in x, y and z; no EP slicing or GT is used.
The report never edits geometry. Axis-strip rules require axis-orthogonal space
rings; other plan shapes remain explicit coverage limits. Z gaps are independent.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import math

from shapely.geometry import Polygon
from shapely.ops import unary_union

from src.agent.geometry.plan_regularization import minimum_orthogonal_space_width

NUMERICAL_M = 1e-7
REGULARIZATION_M = .30
MIN_SPACE_WIDTH_M = .60


def _axis_line(points):
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    if max(xs) - min(xs) <= NUMERICAL_M and max(ys) - min(ys) > NUMERICAL_M:
        return "x", xs[0], min(ys), max(ys)
    if max(ys) - min(ys) <= NUMERICAL_M and max(xs) - min(xs) > NUMERICAL_M:
        return "y", ys[0], min(xs), max(xs)
    return None


def _overlap(a, b):
    return max(0., min(a[3], b[3]) - max(a[2], b[2]))


def _orthogonal_ring(points):
    """The axis-strip rules require every real edge, not just a schema label."""
    return bool(points) and all(_axis_line([a, b]) is not None
                               for a, b in zip(points, points[1:] + points[:1]))


def _line(row):
    return dict(floor_id=row['floor'], boundary_ids=sorted(row['ids']),
                axis=row['line'][0], coordinate_m=row['line'][1], span_m=list(row['line'][2:]))


def _width(polygon):
    if polygon.is_empty:
        return 0., 0.
    rectangle = polygon.minimum_rotated_rectangle
    if rectangle.geom_type != 'Polygon':
        return 0., 0.
    points = list(rectangle.exterior.coords)
    lengths = [math.dist(a, b) for a, b in zip(points, points[1:])]
    return min(lengths), max(lengths)


def _polygons(geometry):
    if geometry.geom_type == 'Polygon':
        yield geometry
    elif hasattr(geometry, 'geoms'):
        for part in geometry.geoms:
            yield from _polygons(part)


def precision_report(source, *, floor_evidence=(), wall_references=()):
    """Return fixed 0.30/0.60 m hard-rule violations and coverage limits.

    Evidence rows remain provenance for display.  They do not shrink, expand or
    disable the product hard thresholds.
    """
    spaces = {s['id']: s for s in source['spaces']}
    floors = {f['id']: f for f in source['floors']}
    orthogonal_spaces = {sid for sid, space in spaces.items() if _orthogonal_ring(space['polygon'])}
    excluded_spaces = sorted(spaces.keys() - orthogonal_spaces)
    excluded_floors = {fid for fid, floor in floors.items()
                       if not _orthogonal_ring(floor['footprint'])
                       or any(not _orthogonal_ring(hole) for hole in floor.get('footprint_holes', []))
                       or any(space['floor_id'] == fid and sid not in orthogonal_spaces
                              for sid, space in spaces.items())
                       or any(sid not in orthogonal_spaces for sid in floor.get('spanning_space_ids', []))}
    evidence = {r['floor_id']: r for r in floor_evidence}
    dimensions = {r['boundary_id'] for r in wall_references
                  if r.get('evidence_status') == 'observed' and r.get('reference_basis') == 'dimensioned'}
    thickness = {r['boundary_id']: r['thickness_m'] for r in wall_references if r.get('thickness_m')}
    for row in source.get('component_attributes', []):
        if row.get('kind') == 'wall':
            thickness.update({bid: row['thickness_m'] for bid in row['boundary_ids']})
    tolerances = {}
    for fid in floors:
        row = evidence.get(fid, {})
        pixels = [float(v) for v in row.get('metres_per_pixel', []) if math.isfinite(float(v)) and v > 0]
        widths = [float(row['wall_thickness_m'])] if row.get('wall_thickness_m') else []
        widths.extend(float(thickness[b['id']]) for b in source['boundaries']
                      if b['id'] in thickness and spaces[b['space_id']]['floor_id'] == fid)
        wall = min(widths) if widths else None
        noise = 2 * max(pixels) if pixels else None
        tolerances[fid] = dict(default_m=REGULARIZATION_M, pixel_noise_m=noise, wall_thickness_m=wall,
            basis=row.get('basis', 'no plan scale or wall thickness supplied'),
            rule='fixed post-regularisation hard threshold; align only when distance is strictly below 0.30 m',
            thickness_cap_known=wall is not None)

    # Merge only exactly collinear overlapping source sides. This does not move a vertex.
    grouped = defaultdict(list)
    nonorthogonal = excluded_walls = 0
    for boundary in source['boundaries']:
        if boundary['geometry_type'] != 'wall' or boundary.get('kind') != 'physical':
            continue
        line = _axis_line(boundary['vertices'])
        if line is None:
            nonorthogonal += 1
        if boundary['space_id'] not in orthogonal_spaces:
            excluded_walls += 1
            continue
        if line is None:
            continue
        floor = spaces[boundary['space_id']]['floor_id']
        grouped[(floor, line[0], line[1])].append(dict(floor=floor, line=line,
            ids={boundary['id']}, exterior=not boundary.get('adjacent_space_ids'),
            dimensioned=boundary['id'] in dimensions))
    walls = []
    for group in grouped.values():
        merged = []
        for row in sorted(group, key=lambda r: r['line'][2:]):
            if merged and row['line'][2] <= merged[-1]['line'][3] + NUMERICAL_M:
                prev = merged[-1]
                prev['line'] = (*prev['line'][:3], max(prev['line'][3], row['line'][3]))
                prev['ids'].update(row['ids'])
                prev['exterior'] |= row['exterior']
                prev['dimensioned'] |= row['dimensioned']
            else:
                merged.append(row)
        walls.extend(merged)

    def tolerance(*fids, ids=()):
        del ids
        return REGULARIZATION_M if fids else None

    def suggestion(a, b):
        def priority(row):
            same = [w for w in walls if w['line'][:2] == row['line'][:2]]
            return (row['dimensioned'], row['exterior'], len({w['floor'] for w in same}),
                    sum(len(w['ids']) for w in same))
        pa, pb = priority(a), priority(b)
        if pa == pb:
            return dict(align_to_options=[_line(a), _line(b)], decision='choose_existing_line_with_evidence')
        target = a if pa > pb else b
        return dict(align_to=_line(target), decision='prefer_dimensioned_then_exterior_then_shared_line')

    def polygon_lines(polygon, fids):
        points = list(polygon.exterior.coords)
        edges = [line for a, b in zip(points, points[1:]) if (line := _axis_line([a, b]))]
        choices = []
        for edge in sorted(edges, key=lambda e: e[2]-e[3]):
            for wall in walls:
                if (wall['floor'] in fids and wall['line'][:2] == edge[:2]
                        and _overlap(wall['line'], edge) > NUMERICAL_M
                        and (not choices or wall['line'][0] == choices[0]['line'][0])
                        and not any(w['line'][:2] == wall['line'][:2] for w in choices)):
                    choices.append(wall)
            if len(choices) >= 2:
                return suggestion(*choices[:2])
        return dict(align_to_options=[_line(w) for w in choices], decision='review_existing_boundary_lines')

    items, pairs = [], []
    ordered = sorted(floors.values(), key=lambda f: (f['z_floor'], f['id']))
    for lower, upper in zip(ordered, ordered[1:]):
        if (upper['z_floor'] <= lower['z_floor']
                or Polygon(lower['footprint'], lower.get('footprint_holes', [])).intersection(
                    Polygon(upper['footprint'], upper.get('footprint_holes', []))).area <= .25):
            continue
        lf, uf = lower['id'], upper['id']
        pairs.append([lf, uf])
        # Source BIM floors persist their clear storey extent as ``height``.
        # Accept ``ceiling_height`` only as a compatibility fallback for
        # pre-source fixtures; a missing height cannot support a Z hard rule.
        lower_height = lower.get('height', lower.get('ceiling_height'))
        if lower_height is None:
            raise ValueError(f"Floor {lf!r} has no height for the vertical precision gate")
        separation = upper['z_floor'] - (lower['z_floor'] + lower_height)
        if NUMERICAL_M < abs(separation) < REGULARIZATION_M - NUMERICAL_M:
            items.append(dict(type='vertical_gap_or_overlap', floor_ids=[lf, uf],
                objects=[dict(floor_id=lf, surface='ceiling',
                              elevation_m=lower['z_floor'] + lower_height),
                         dict(floor_id=uf, surface='floor', elevation_m=upper['z_floor'])],
                distance_m=round(abs(separation), 9), signed_gap_m=round(separation, 9),
                tolerance_m=REGULARIZATION_M,
                fix='Use the trusted elevation so floor and ceiling touch exactly, or separate them by at least 0.30 m; do not change floor height silently.'))
        aa = [w for w in walls if w['floor'] == lf]
        bb = [w for w in walls if w['floor'] == uf]
        for a in aa:
            for b in bb:
                limit = tolerance(lf, uf, ids=a['ids'] | b['ids'])
                if limit is None or a['line'][0] != b['line'][0]:
                    continue
                delta = abs(a['line'][1] - b['line'][1])
                shared = _overlap(a['line'], b['line'])
                if not (NUMERICAL_M < delta < limit - NUMERICAL_M and shared > NUMERICAL_M):
                    continue
                # A closer matching wall over the same span defeats the proposed pairing.
                shared_line = (a['line'][0], a['line'][1], max(a['line'][2], b['line'][2]),
                               min(a['line'][3], b['line'][3]))
                if any(w['line'][0] == a['line'][0]
                       and abs(w['line'][1] - a['line'][1]) < delta - NUMERICAL_M
                       and _overlap(shared_line, w['line']) >= shared - NUMERICAL_M for w in bb):
                    continue
                if any(w['line'][0] == b['line'][0]
                       and abs(w['line'][1] - b['line'][1]) < delta - NUMERICAL_M
                       and _overlap(shared_line, w['line']) >= shared - NUMERICAL_M for w in aa):
                    continue
                items.append(dict(type='storey_wall_offset', lines=[_line(a), _line(b)],
                    deviation_m=round(delta, 9), overlap_m=round(shared, 9), tolerance_m=limit,
                    objects=[_line(a), _line(b)],
                    fix='Align the affected plan draft to one higher-priority existing wall line, recompile it, and preserve opening hosts and connections.',
                    **suggestion(a, b)))

    # Same-floor near-parallel source walls are forbidden even when a thin
    # derived space happens not to expose the duplicate representation clearly.
    for fid in floors:
        same_floor = [w for w in walls if w['floor'] == fid]
        for index, a in enumerate(same_floor):
            for b in same_floor[index + 1:]:
                if a['line'][0] != b['line'][0]:
                    continue
                delta = abs(a['line'][1] - b['line'][1])
                shared = _overlap(a['line'], b['line'])
                if not (NUMERICAL_M < delta < REGULARIZATION_M - NUMERICAL_M
                        and shared > NUMERICAL_M):
                    continue
                items.append(dict(type='same_floor_parallel_wall_offset',
                    floor_id=fid, lines=[_line(a), _line(b)], objects=[_line(a), _line(b)],
                    deviation_m=round(delta, 9), overlap_m=round(shared, 9),
                    tolerance_m=REGULARIZATION_M,
                    fix='Merge a sub-0.30 m strip into one representative wall and audit removed strip seeds plus moved/merged openings, or keep two walls at least 0.30 m apart.'))

    boundaries = {b['id']: b for b in source['boundaries']}
    for relation in source.get('boundary_relations', []):
        hosts = [boundaries[bid] for bid in relation['boundary_ids']]
        if {b['geometry_type'] for b in hosts} != {'floor', 'ceiling'}:
            continue
        if any(b['space_id'] not in orthogonal_spaces for b in hosts):
            continue
        fids = list(dict.fromkeys(spaces[b['space_id']]['floor_id'] for b in hosts))
        limit = tolerance(*fids)
        if limit is None:
            continue
        for index, region in enumerate(relation['regions']):
            polygon = Polygon([p[:2] for p in region['vertices']],
                              [[p[:2] for p in hole] for hole in region.get('holes', [])])
            width, length = _width(polygon)
            if NUMERICAL_M < width < limit - NUMERICAL_M and length > NUMERICAL_M:
                items.append(dict(type='thin_horizontal_contact', boundary_ids=relation['boundary_ids'],
                    floor_ids=fids, region=index, width_m=round(width, 9), length_m=round(length, 9),
                    area_m2=region['area_m2'], tolerance_m=limit,
                    objects=[dict(boundary_id=bid) for bid in relation['boundary_ids']],
                    fix='Align the affected saved plan surfaces to an existing elevation or boundary; keep the contact at least 0.30 m if it is intentional.',
                    **polygon_lines(polygon, fids)))

    # A step has long parallel legs joined by a short transverse segment. A short
    # room edge alone is insufficient (narrow rooms/shafts are not automatically errors).
    seen_steps = set()
    for space in spaces.values():
        if space['id'] not in orthogonal_spaces:
            continue
        fid = space['floor_id']
        limit = tolerance(fid)
        if limit is None:
            continue
        ring = space['polygon']
        polygon = Polygon(ring)
        width, location = minimum_orthogonal_space_width(polygon)
        _, length = _width(polygon)
        if NUMERICAL_M < width < MIN_SPACE_WIDTH_M - NUMERICAL_M:
            items.append(dict(type='thin_space', space_id=space['id'], floor_id=fid,
                width_m=round(width, 9), length_m=round(length, 9), tolerance_m=MIN_SPACE_WIDTH_M,
                minimum_width_location=location, objects=[dict(space_id=space['id'], floor_id=fid)],
                fix='Make every remaining space at least 0.60 m wide; only a sub-0.30 m overlapping wall strip may be removed, with its seed/opening changes and connections fully audited.',
                **polygon_lines(polygon, [fid])))
        for i in range(len(ring)):
            p, q, r, s = [ring[j % len(ring)] for j in (i-1, i, i+1, i+2)]
            a, connector, b = _axis_line([p, q]), _axis_line([q, r]), _axis_line([r, s])
            if not a or not b or not connector or a[0] != b[0] or a[0] == connector[0]:
                continue
            delta = abs(a[1] - b[1])
            along = 1 if a[0] == 'x' else 0
            if not (NUMERICAL_M < delta < limit - NUMERICAL_M and (q[along]-p[along]) * (s[along]-r[along]) > 0):
                continue
            key = (fid, tuple(sorted((tuple(q), tuple(r)))))
            if key in seen_steps:
                continue
            seen_steps.add(key)
            items.append(dict(type='small_step', space_id=space['id'], floor_id=fid,
                endpoints_m=[q, r], width_m=round(delta, 9), tolerance_m=limit,
                objects=[dict(space_id=space['id'], floor_id=fid, endpoints_m=[q, r])],
                fix='Align the two parallel legs to one existing line, or make the intentional step at least 0.30 m.',
                align_to_options=[dict(axis=line[0], coordinate_m=line[1], span_m=list(line[2:])) for line in (a, b)],
                decision='choose_existing_line_with_evidence; preserve rooms, openings and connections'))
    for fid, floor in floors.items():
        if fid in excluded_floors:
            continue
        limit = tolerance(fid)
        polygons = [Polygon(s['polygon']) for s in spaces.values() if s['floor_id'] == fid]
        if limit is None or not polygons or not floor.get('footprint'):
            continue
        gap = Polygon(floor['footprint'], floor.get('footprint_holes', [])).difference(unary_union(polygons))
        for polygon in _polygons(gap):
            width, length = _width(polygon)
            if NUMERICAL_M < width < limit - NUMERICAL_M and length > NUMERICAL_M:
                items.append(dict(type='thin_coverage_gap', floor_id=fid,
                    polygon_m=list(map(list, polygon.exterior.coords))[:-1], width_m=round(width, 9),
                    length_m=round(length, 9), area_m2=polygon.area, tolerance_m=limit,
                    objects=[dict(floor_id=fid, polygon_m=list(map(list, polygon.exterior.coords))[:-1])],
                    fix='Close the gap by moving the responsible existing plan boundary; do not add a source space or wall silently.',
                    **polygon_lines(polygon, [fid])))
    priority = {'vertical_gap_or_overlap':0, 'storey_wall_offset':1,
                'same_floor_parallel_wall_offset':2, 'thin_space':3,
                'thin_coverage_gap':4, 'small_step':5, 'thin_horizontal_contact':6}
    items.sort(key=lambda r: (priority[r['type']], -r.get('deviation_m', r.get('width_m',0))))
    return dict(schema='building_precision_v2', status='pass' if not items else 'rejected', source_model_sha256=source.get('source_model_sha256'),
        total=len(items), counts=dict(Counter(r['type'] for r in items)), items=items, tolerances=tolerances,
        axis_rule_applicability=dict(
            status=('partially_applicable' if orthogonal_spaces else 'not_applicable')
                   if excluded_spaces or excluded_floors else 'applicable',
            checked_space_ids=sorted(orthogonal_spaces), not_applicable_space_ids=excluded_spaces,
            not_applicable_coverage_floor_ids=sorted(excluded_floors),
            basis='Plan strip/width rules require complete axis-orthogonal footprints; inferred schema labels cannot opt out. '
                  'Nonorthogonal-space boundaries and contacts are excluded from axis matching; their minimum room width is not evaluated. '
                  'Vertical gaps, source coverage, true spatial overlaps and opening hosts retain their independent checks.'),
        coverage=dict(floors=list(floors), adjacent_storey_pairs=pairs, orthogonal_wall_lines=len(walls),
            nonorthogonal_walls_not_checked=nonorthogonal,
            walls_excluded_with_nonorthogonal_space=excluded_walls,
            nonorthogonal_space_ids_not_checked=excluded_spaces,
            floors_without_tolerance=[]),
        meaning='Post-regularisation hard audit of applicable orthogonal plan rules and independent vertical gaps; '
                'pass only covers the evaluated scopes. This report changes no geometry. A save gate must reject each item. '
                'Align to an existing line; only sub-0.30 m duplicate-wall strips may remove seeds or merge openings, with complete identity and connectivity audit.',
        not_checked=['nonorthogonal room minimum width and axis-strip/step/contact/coverage rules',
                     'nonorthogonal wall alignment', 'non-overlapping storeys', 'opening positions/heights',
                     'drawing fidelity', 'user fineness cap (not configured)'])
