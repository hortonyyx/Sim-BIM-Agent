"""Thickness-aware Lite BIM wall selection; original lines remain auditable."""
from __future__ import annotations

import copy
import hashlib
import json

from src.agent.geometry.reader_wall_metadata import require_wall_thickness


def _adopted_digest(plan):
    payload = {k: plan.get(k) for k in ('x_anchors', 'y_anchors', 'footprint_pixels', 'partitions')}
    payload['walls'] = [(r['target'], r['thickness_m'], r['reference_side'])
                        for r in plan.get('wall_metadata', [])]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def stamp_wall_state(plan):
    if plan.get('wall_metadata'):
        _state(plan)['__adopted_sha256'] = _adopted_digest(plan)


def prepare_thickness_plan(plan):
    """Materialize batch metadata and give differently thick legs stable IDs."""
    result = require_wall_thickness(plan)
    rows = {r['target']: r for r in result['wall_metadata']}
    partitions, metadata = [], [r for r in rows.values() if r['target'].startswith('footprint:')]
    refs = result.get('regularization_inputs', {}).get('line_references', [])
    used = {p['id'] for p in result['partitions']}
    for part in result['partitions']:
        target = 'partition:' + part['id']
        for i, (a, b) in enumerate(zip(part['points'], part['points'][1:])):
            identity = part['id'] if i == 0 else part['id'] + f'::thickness-{i + 1}'
            while i and identity in used:
                identity += '-2'
            used.add(identity)
            partitions.append({**copy.deepcopy(part), 'id': identity, 'points': [a, b]})
            record = copy.deepcopy(rows.get(f'{target}:{i}', rows.get(target)))
            record['target'] = 'partition:' + identity
            metadata.append(record)
            if i:
                refs.extend({**copy.deepcopy(r), 'partition_id': identity}
                            for r in list(refs) if r['partition_id'] == part['id'])
    result['partitions'], result['wall_metadata'] = partitions, metadata
    from src.agent.geometry.reader_wall_metadata import wall_reading_input, _span
    state = _state(result)
    if state.get('__adopted_sha256') != _adopted_digest(result):
        state.clear()  # An authored coordinate/thickness revision invalidates old membership.
    for wall in wall_reading_input(result)['walls']:
        members = []
        for a, b in wall['segments_m']:
            axis, at, _, _ = _span(a, b)
            members.append(dict(floor_id=result.get('floor_id'), target=wall['target'],
                                axis='xy'[axis], coordinate_m=at, thickness_m=wall['thickness_m']))
        state.setdefault(wall['target'], members)
    return result


def _state(plan):
    if not plan.get('regularization_inputs', {}).get('reading_alignment'):
        from src.agent.geometry.plan_ink_alignment import _set_reading_alignment
        _set_reading_alignment(plan, 'dimensions', {'status': 'not_supplied'})
    return plan.setdefault('regularization_inputs', {}).setdefault(
        'reading_alignment', {}).setdefault('wall_merging', {})


def target_key(segment):
    return (f"footprint:{segment['segment_index']}" if segment.get('fixed_exterior')
            else 'partition:' + segment['partition_id'])


def decorate_segments(plan, segments):
    rows = {r['target']: r for r in plan.get('wall_metadata', [])}
    state = plan.get('regularization_inputs', {}).get('reading_alignment', {}).get('wall_merging', {})
    for segment in segments:
        key = target_key(segment)
        row = rows.get(f"{key}:{segment['segment_index']}", rows.get(key))
        if not row or not row.get('thickness_m'):
            continue
        segment['thickness_m'] = row['thickness_m']
        segment['reference_side'] = row['reference_side']
        segment['wall_members'] = copy.deepcopy(state.get(key, [{
            'floor_id': plan.get('floor_id'), 'target': key, 'axis': segment['axis'],
            'coordinate_m': segment['coordinate_m'], 'thickness_m': row['thickness_m'],
        }]))
    return segments


def within_wall_tolerance(first, second, *, allow_equal=False):
    distance = abs(first['coordinate_m'] - second['coordinate_m'])
    if distance <= 1e-7:
        return allow_equal
    a, b = first.get('thickness_m'), second.get('thickness_m')
    if not a or not b:
        return distance < .30 - 1e-8
    first_members, second_members = first.get('wall_members', []), second.get('wall_members', [])
    if not first_members or not second_members:
        return distance <= 2 * max(a, b) + 1e-8
    # A chain of individually close walls must not grow into an arbitrarily
    # wide group. Check original member pairs, not only the last moved line.
    return all(abs(x['coordinate_m'] - y['coordinate_m']) <=
               2 * max(x['thickness_m'], y['thickness_m']) + 1e-8
               for x in first_members for y in second_members)


def remember_merge(plan, source, target, *, target_plan=None, keep_source=False):
    if not source.get('thickness_m') or not target.get('thickness_m'):
        return
    target_plan = plan if target_plan is None else target_plan
    members = []
    for row in [*source.get('wall_members', []), *target.get('wall_members', [])]:
        if row not in members:
            members.append(copy.deepcopy(row))
    _state(target_plan)[target_key(target)] = members
    if keep_source:
        _state(plan)[target_key(source)] = members
        for row in plan['wall_metadata']:
            if row['target'] == target_key(source):
                row['reference_side'] = target['reference_side']


def retain_remainder_metadata(plan, source_id, remainder_ids):
    key = 'partition:' + source_id
    rows = plan.get('wall_metadata', [])
    original = next((r for r in rows if r['target'] == key), None)
    if original is None:
        return
    rows[:] = [r for r in rows if r['target'] != key]
    for identity in remainder_ids:
        rows.append({**copy.deepcopy(original), 'target': 'partition:' + identity})


def prune_removed_metadata(plan):
    from src.agent.geometry.reader_wall_metadata import _targets
    ids, _, _ = _targets(plan)
    if 'wall_metadata' in plan:
        plan['wall_metadata'] = [r for r in plan['wall_metadata']
                                 if r['target'] in ids or r['target'] in {'footprint:*', 'partition:*'}]
