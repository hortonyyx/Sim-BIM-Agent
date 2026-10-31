"""Independent, evaluation-only plan inventory and exterior-height comparisons.

The observations are frozen original-image measurements, never candidate-fitted.
This factors the existing historical inventory criteria into a reusable scorer.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import linear_sum_assignment
from shapely.geometry import Point, Polygon


def compare_plan_inventory(source: dict, observations: list[dict], floor_mapping: dict) -> dict:
    rows, floors, findings = [], [], []
    covered_openings = set()
    from src.agent.judge.partition_evidence import floor_number
    normalized_floors = {key: f'F{floor_number(value)}' if floor_number(value) else value for key, value in floor_mapping.items()}
    inverse = {value: key for key, value in normalized_floors.items()}
    for obs in observations:
        fid = inverse.get(obs['floor_id'])
        if fid is None:
            findings.append(dict(code='inventory_floor_missing', severity='severe', reference_floor=obs['floor_id']))
            continue

        def coordinate(value, axis):
            (p0, v0), (p1, v1) = obs['calibration'][axis + '_anchors']
            return v0 + (value - p0) * (v1 - v0) / (p1 - p0)

        spaces = {s['id']: Polygon(s['polygon'], s.get('holes', [])) for s in source['spaces'] if s['floor_id'] == fid}
        identities = {key: [sid for sid, poly in spaces.items()
                           if poly.contains(Point(coordinate(p[0], 'x'), coordinate(p[1], 'y')))]
                      for key, p in obs['spaces'].items()}
        unique = [ids[0] for ids in identities.values() if len(ids) == 1]
        if len(unique) != len(identities) or len(set(unique)) != len(unique) or set(unique) != set(spaces):
            findings.append(dict(code='room_identity_not_bijective', severity='severe', floor_id=fid,
                                 identities=identities, candidate_space_ids=sorted(spaces)))
        actual = []
        for opening in source['openings']:
            if not any(s in spaces for s in opening['space_ids']):
                continue
            covered_openings.add(opening['id'])
            xyz = np.array(opening['vertices'])
            dim = int(np.argmax(np.ptp(xyz[:, :2], axis=0)))
            actual.append(dict(id=opening['id'], kind=opening['kind'], axis='xy'[dim],
                span=[float(xyz[:, dim].min()), float(xyz[:, dim].max())],
                cross=float(xyz[:, 1-dim].mean()), space_ids=opening['space_ids'], exterior=opening['exterior']))
        costs = np.full((len(obs['apertures']), len(actual)), 1e6)
        metrics = {}
        for i, ref in enumerate(obs['apertures']):
            span = sorted(coordinate(v, ref['axis']) for v in ref['span_pixels'])
            cross = coordinate(ref['cross_pixel'], 'y' if ref['axis'] == 'x' else 'x')
            for j, item in enumerate(actual):
                if (ref['kind'], ref['axis']) != (item['kind'], item['axis']):
                    continue
                along = max(abs(x-y) for x, y in zip(span, item['span']))
                across = abs(cross-item['cross'])
                costs[i, j] = along + across
                metrics[i, j] = (span, cross, along, across)
        used_r, used_a = set(), set()
        for i, j in zip(*linear_sum_assignment(costs)):
            if costs[i, j] >= 1e6:
                continue
            used_r.add(int(i)); used_a.add(int(j))
            ref, item = obs['apertures'][i], actual[j]
            span, cross, along, across = metrics[i, j]
            hosts = sorted(s for h in ref['hosts'] for s in identities[h])
            exterior = len(ref['hosts']) == 1
            host_ok = (all(len(identities[h]) == 1 for h in ref['hosts']) and
                len(set(hosts)) == len(ref['hosts']) and sorted(item['space_ids']) == hosts and item['exterior'] == exterior)
            connections = [c for c in source['connections'] if c['opening_id'] == item['id']]
            connected = None if ref['kind'] != 'door' else bool(host_ok and len(connections) == 1 and
                sorted(connections[0]['space_ids']) == hosts and connections[0]['exterior'] == exterior)
            tol = obs['tolerance']
            position = along <= tol['along_m'] and across <= tol['external_cross_m' if exterior else 'internal_cross_m']
            row = dict(floor_id=fid, reference_id=ref['id'], opening_id=item['id'], kind=ref['kind'],
                reference_span_m=span, candidate_span_m=item['span'], reference_cross_m=cross,
                candidate_cross_m=item['cross'], max_endpoint_error_m=along, perpendicular_error_m=across,
                position_match=bool(position), expected_hosts=hosts, actual_hosts=item['space_ids'],
                host_match=bool(host_ok), connection_match=connected)
            rows.append(row)
            if not (position and host_ok and connected is not False):
                findings.append(dict(code='opening_position_host_or_connection_changed', severity='severe', **row))
        missing = [r['id'] for i, r in enumerate(obs['apertures']) if i not in used_r]
        extra = [a['id'] for i, a in enumerate(actual) if i not in used_a]
        for key, ids in [('missing_opening', missing), ('extra_opening', extra)]:
            if ids:
                findings.append(dict(code=key, severity='severe', floor_id=fid, opening_ids=ids))
        floors.append(dict(floor_id=fid, reference_floor_id=obs['floor_id'],
            space_identity_by_interior_point=identities, unmatched_reference=missing, unmatched_actual=extra))
    if set(normalized_floors.values()) != {o['floor_id'] for o in observations}:
        findings.append(dict(code='inventory_floor_scope_changed', severity='severe'))
    orphaned = sorted(o['id'] for o in source['openings'] if o['id'] not in covered_openings)
    if orphaned:
        findings.append(dict(code='opening_without_observed_floor_host', severity='severe', opening_ids=orphaned))
    opening_ids = {o['id'] for o in source['openings']}
    orphan_connections = [c['opening_id'] for c in source['connections'] if c['opening_id'] not in opening_ids]
    if orphan_connections:
        findings.append(dict(code='connection_without_opening', severity='severe', opening_ids=orphan_connections))
    return dict(status='severe' if findings else 'pass', reference_count=sum(len(o['apertures']) for o in observations),
        matched=len(rows), positions=sum(r['position_match'] for r in rows), hosts=sum(r['host_match'] for r in rows),
        door_connections=sum(r['connection_match'] is True for r in rows),
        floors=floors, comparisons=rows, findings=findings,
        limits=['Original-pixel observations retain their recorded tolerances.',
                'Interior heights, operating state and vertical circulation are not verified.'])


def compare_exterior_heights(source: dict, document, partition: dict, tolerance_m: float = .05) -> dict:
    """Match verified exterior GT to actual source openings; keep all unmatched items."""
    from src.agent.judge.gt_schema import GroundTruthV3, LegacyGroundTruthV2
    from types import SimpleNamespace
    if not isinstance(document, (GroundTruthV3, LegacyGroundTruthV2)) or 'comparison' not in partition:
        return dict(status='not_evaluated', reason='verified_exterior_reference_required', findings=[])
    typed = isinstance(document, GroundTruthV3)
    space_map = {m['candidate_id']: m['reference_id'] for m in partition['comparison']['matches']}
    segments = {b.id: b for f in document.floors for b in f.boundary_segments} if typed else {}
    spaces = {s['id']: s for s in source['spaces']}
    actual = []
    for opening in source['openings']:
        if not opening['exterior']:
            continue
        xyz = np.array(opening['vertices'])
        dim = int(np.argmax(np.ptp(xyz[:, :2], axis=0)))
        host = opening['space_ids'][0] if opening['space_ids'] else None
        floor = partition['floor_mapping'].get(spaces.get(host, {}).get('floor_id'))
        actual.append(dict(id=opening['id'], kind=opening['kind'], dim=dim, floor=floor,
            span=[float(xyz[:, dim].min()), float(xyz[:, dim].max())],
            cross=float(xyz[:, 1-dim].mean()), z=[float(xyz[:, 2].min()), float(xyz[:, 2].max())],
            hosts=[space_map.get(s) for s in opening['space_ids']]))
    refs = []
    if typed:
        for ref in document.openings:
            boundary = segments[ref.boundary_segment_id]
            dim = int(np.argmax(np.ptp(np.array([boundary.p1, boundary.p2]), axis=0)))
            refs.append(SimpleNamespace(id=ref.id, kind=ref.kind, floor_id=ref.floor_id,
                dim=dim, span=[ref.world_along_interval.lo, ref.world_along_interval.hi],
                cross=boundary.p1[1-dim], z_interval=ref.z_interval, host_zone_id=ref.host_zone_id))
    else:
        # Legacy v2 has global along/Z values but no host-zone fields. Keep
        # host/connection validation in the independent original-plan report.
        data = document.model_dump(mode='json')
        footprint = data['footprint']
        def legacy(ref, identity, kind, floor, facade):
            dim = 0 if facade in {'North', 'South'} else 1
            cross = {'North': footprint['D_m'], 'South': 0., 'East': footprint['W_m'], 'West': 0.}[facade]
            refs.append(SimpleNamespace(id=identity, kind=kind, floor_id=floor, dim=dim, cross=cross,
                span=[ref.x_m, ref.x_m+ref.width_m],
                z_interval=SimpleNamespace(lo=ref.sill_m, hi=ref.head_m), host_zone_id=None))
        for i, group in enumerate(document.windows):
            for j, ref in enumerate(group.openings):
                legacy(ref, f'window_group_{i}_item_{j}', 'window', group.floor, group.facade)
        for i, ref in enumerate(document.doors):
            legacy(ref, f'door_{i}', 'door', ref.floor, ref.facade)
    costs, metrics = np.full((len(refs), len(actual)), 1e6), {}
    for i, ref in enumerate(refs):
        dim, span, cross = ref.dim, ref.span, ref.cross
        for j, item in enumerate(actual):
            if (ref.kind, dim, ref.floor_id) != (item['kind'], item['dim'], item['floor']):
                continue
            along = max(abs(a-b) for a, b in zip(span, item['span']))
            across = abs(cross-item['cross'])
            costs[i, j] = along + across
            metrics[i, j] = along, across
    rows, findings, used_r, used_a = [], [], set(), set()
    for i, j in zip(*linear_sum_assignment(costs)):
        if costs[i, j] >= 1e6:
            continue
        used_r.add(int(i)); used_a.add(int(j))
        ref, item = refs[i], actual[j]
        expected = [ref.z_interval.lo, ref.z_interval.hi]
        delta = max(abs(a-b) for a, b in zip(expected, item['z']))
        along, across = metrics[i, j]
        # Position is reported by the original plan inventory. Here it bounds
        # identity association, never excuses a height discrepancy.
        identity = along <= .35 and across <= .25 and (ref.host_zone_id is None or item['hosts'] == [ref.host_zone_id])
        row = dict(reference_id=ref.id, opening_id=item['id'], reference_z_m=expected,
            candidate_z_m=item['z'], max_z_delta_m=delta, identity_match=identity,
            within_strict=delta <= tolerance_m + 1e-9)
        rows.append(row)
        if not row['within_strict'] or not identity:
            findings.append(dict(code='exterior_opening_height_or_identity_changed', severity='severe', **row))
    missing = [r.id for i, r in enumerate(refs) if i not in used_r]
    extra = [a['id'] for i, a in enumerate(actual) if i not in used_a]
    for code, ids in [('missing_exterior_opening', missing), ('extra_exterior_opening', extra)]:
        if ids:
            findings.append(dict(code=code, severity='severe', opening_ids=ids))
    return dict(status='severe' if findings else 'pass', tolerance_m=tolerance_m,
        expected=len(refs), matched=len(rows), within=sum(r['within_strict'] for r in rows),
        comparisons=rows, unmatched_reference=missing, unmatched_actual=extra, findings=findings)
