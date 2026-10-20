"""Normalize shared straight paths for architectural adoption, then restore IDs.

Only templates whose referenced paths are entirely lines enter this adapter.
The common curve expander remains the authority for path joins and derived rings.
No curve is fitted, no path is discarded, and a shared declaration is updated
only when all its uses agree on both endpoints.
"""
from __future__ import annotations

import copy
import math

from pydantic import TypeAdapter

from src.agent.geometry.source_curves import (
    CurveSegment, CurveUse, JOIN_TOLERANCE_M, expand_source_polygon_geometry,
)


def _nodes(template):
    footprint = template.get('footprint')
    if isinstance(footprint, dict) and footprint.get('boundary_path') is not None:
        yield 'footprint', None, footprint, 'vertices'
    for index, space in enumerate(template.get('spaces', [])):
        if space.get('boundary_path') is not None:
            yield f"spaces[{space['id']}]", index, space, 'polygon'


def _uses(node):
    uses = TypeAdapter(list[CurveUse]).validate_python(node['boundary_path'])
    if not uses:
        raise ValueError('boundary_path must contain at least one curve')
    return uses


def _expanded(template, curves):
    footprint = copy.deepcopy(template['footprint'])
    if isinstance(footprint, list):
        footprint = {'vertices': footprint}
    return expand_source_polygon_geometry({
        'schema_version': 'source_polygon_v1', 'boundary_curves': curves,
        'floors': [{'footprint': footprint, 'cells': copy.deepcopy(template['spaces'])}],
    })['floors'][0]


def _node(plan, binding):
    template = plan['templates'][binding['template']]
    if binding['space_index'] is None:
        return template['footprint']
    space = template['spaces'][binding['space_index']]
    if space['id'] != binding['space_id']:
        raise ValueError('straight path space identity changed during adoption')
    return space


def _same_pair(first, second):
    return all(math.dist(a, b) <= JOIN_TOLERANCE_M for a, b in zip(first, second))


def expand_straight_paths(plan):
    """Return a copy with eligible paths replaced by rings and restore bindings.

Non-line templates remain untouched. A line used by one of those templates is
protected even when another all-line template references the same declaration.
Bindings are local pre-compiler state, not a new user-facing plan schema.
"""
    normalized = copy.deepcopy(plan)
    curves = plan.get('boundary_curves', {})
    bindings = {'rings': [], 'protected_line_uses': {}}
    for name, template in plan['templates'].items():
        nodes = list(_nodes(template))
        if not nodes:
            continue
        uses_by_node = [(path, index, node, field, _uses(node))
                        for path, index, node, field in nodes]
        identities = {use.curve_id for *_, uses in uses_by_node for use in uses}
        if any(identity not in curves for identity in identities):
            raise ValueError(f'templates.{name}: boundary_path references an unknown curve')
        if any(curves[identity].get('kind') != 'line' for identity in identities):
            # Preserve the historical non-line branch; the common source gate
            # still validates declarations that are outside this adapter.
            for path, _, _, _, uses in uses_by_node:
                for use in uses:
                    if curves[use.curve_id].get('kind') == 'line':
                        bindings['protected_line_uses'].setdefault(use.curve_id, []).append(
                            f'templates.{name}.{path}')
            continue
        expanded = _expanded(template, {identity: curves[identity] for identity in sorted(identities)})
        for path, index, node, field, uses in uses_by_node:
            if index is not None and 'rect' in node:
                raise ValueError(f'templates.{name}.{path}: a space cannot declare both rect and boundary_path')
            points = copy.deepcopy((expanded['footprint'] if index is None else expanded['cells'][index])[field])
            endpoints = []
            for use in uses:
                indices = []
                for point in (curves[use.curve_id]['p1'], curves[use.curve_id]['p2']):
                    matches = [i for i, vertex in enumerate(points)
                               if math.dist(point, vertex) <= JOIN_TOLERANCE_M]
                    if len(matches) != 1:
                        raise ValueError(f'templates.{name}.{path}: line endpoint has no unique ring vertex')
                    indices.append(matches[0])
                endpoints.append({'curve_id': use.curve_id, 'reverse': use.reverse,
                                  'vertex_indices': indices})
            binding = {'template': name, 'space_index': index,
                       'space_id': node.get('id') if index is not None else None,
                       'path': f'templates.{name}.{path}', 'field': field,
                       'boundary_path': copy.deepcopy(node['boundary_path']),
                       'had_derived_ring': field in node, 'before_ring': points,
                       'endpoints': endpoints}
            target = _node(normalized, binding)
            target.pop('boundary_path')
            target[field] = copy.deepcopy(points)
            bindings['rings'].append(binding)
    return normalized, bindings


def _apertures(plan):
    for collection in ('connections', 'core_connections'):
        for row in plan.get(collection, []):
            yield f"{collection}[{row['id']}]", row
    for name, template in plan.get('templates', {}).items():
        for collection in ('doors', 'opening_rows', 'window_rows'):
            for row in template.get(collection, []):
                yield f"templates.{name}.{collection}[{row['id']}]", row


def _rebind_segments(before, restored, moved, changes, reject):
    """Keep adopted physical endpoints; recompute fractions on the same line.

The old fractions must already agree with the original endpoints. This does not
repair a stale original binding, choose a different curve, clip an aperture, or
change an arc/chord binding. New fractions may change when line ends move.
"""
    originals = dict(_apertures(before))
    for path, row in _apertures(restored):
        raw_use = row.get('curve_segment')
        if raw_use is None:
            continue
        use = CurveSegment.model_validate(raw_use)
        if use.curve_id not in moved:
            continue
        old = originals[path]
        if old.get('curve_segment') != raw_use:
            reject('line_aperture_binding_changed', path, 'curve identity and binding must remain stable during adoption')
        old_curve = before['boundary_curves'][use.curve_id]
        lo, hi = use.span_fraction
        if use.segment_index != 0 or not 0 <= lo < hi <= 1:
            reject('invalid_line_aperture_binding', path, 'a line aperture needs segment 0 and ordered fractions within [0,1]')
        expected = [[a + (b-a)*fraction for a, b in zip(old_curve['p1'], old_curve['p2'])]
                    for fraction in (lo, hi)]
        if not _same_pair(expected, [old['p1'], old['p2']]):
            reject('stale_line_aperture_binding', path,
                   'original aperture endpoints disagree with the declared line fractions',
                   curve_id=use.curve_id, expected_endpoints=expected,
                   original_endpoints=[old['p1'], old['p2']])
        curve = restored['boundary_curves'][use.curve_id]
        a, b = curve['p1'], curve['p2']
        vector = [q-p for p, q in zip(a, b)]
        length2 = sum(value*value for value in vector)
        fractions = [sum((p-q)*v for p, q, v in zip(point, a, vector))/length2
                     for point in (row['p1'], row['p2'])]
        # Only remove endpoint roundoff within the common geometric join limit.
        tolerance = JOIN_TOLERANCE_M / math.sqrt(length2)
        if (fractions[0] < -tolerance or fractions[1] > 1+tolerance
                or fractions[0] >= fractions[1]):
            reject('line_aperture_outside_host', path,
                   'adopted aperture must remain inside its original declared line',
                   curve_id=use.curve_id, adopted_fractions=fractions,
                   adopted_endpoints=[row['p1'], row['p2']], line_endpoints=[a, b])
        fractions = [max(0., min(1., value)) for value in fractions]
        projected = [[p + v*fraction for p, v in zip(a, vector)] for fraction in fractions]
        if not _same_pair(projected, [row['p1'], row['p2']]):
            reject('line_aperture_off_host', path,
                   'adopted aperture endpoints do not lie on their declared line',
                   curve_id=use.curve_id, adopted_endpoints=[row['p1'], row['p2']],
                   projected_endpoints=projected)
        row['curve_segment'] = {**copy.deepcopy(raw_use), 'span_fraction': fractions}
        if row['curve_segment'] != raw_use:
            changes.append({'type': 'line_aperture_binding', 'path': path,
                            'curve_id': use.curve_id, 'before_fractions': list(use.span_fraction),
                            'adopted_fractions': fractions, 'physical_endpoints_preserved': True})


def restore_straight_paths(before, adopted, bindings, audit, reject):
    """Restore paths in place only after all shared endpoint proposals agree.

Call after ordinary ring/host/contact adoption. ``reject`` must raise the
caller's regularization error and accept structured diagnostic keyword fields.
The adopted plan remains unchanged if restoration fails.
"""
    if before.get('boundary_curves') != adopted.get('boundary_curves'):
        reject('immutable_curve_changed', 'boundary_curves',
               'curve declarations must remain unchanged until shared line restoration')
    proposals = {}
    for binding in bindings['rings']:
        ring = _node(adopted, binding)[binding['field']]
        if len(ring) != len(binding['before_ring']):
            reject('line_path_vertex_identity_changed', binding['path'],
                   'straight-path adoption must preserve ring vertex identities')
        for use in binding['endpoints']:
            pair = [copy.deepcopy(ring[index]) for index in use['vertex_indices']]
            if math.dist(*pair) <= JOIN_TOLERANCE_M:
                reject('collapsed_shared_line', binding['path'], 'shared line collapsed during adoption',
                       curve_id=use['curve_id'], adopted_endpoints=pair)
            proposals.setdefault(use['curve_id'], []).append({'path': binding['path'], 'endpoints': pair})
    changes, updates = [], {}
    for identity, uses in proposals.items():
        pair = uses[0]['endpoints']
        if any(not _same_pair(pair, use['endpoints']) for use in uses[1:]):
            reject('shared_line_endpoint_conflict', f'boundary_curves[{identity}]',
                   'all uses of one shared line must adopt the same canonical endpoints',
                   curve_id=identity, uses=uses)
        original_curve = before['boundary_curves'][identity]
        old = [original_curve['p1'], original_curve['p2']]
        if _same_pair(old, pair):
            continue
        protected = bindings['protected_line_uses'].get(identity, [])
        if protected:
            reject('line_shared_with_protected_curve', f'boundary_curves[{identity}]',
                   'moving this shared line would change a protected non-line template',
                   curve_id=identity, protected_paths=protected, uses=uses,
                   original_endpoints=old, adopted_endpoints=pair)
        updates[identity] = pair
        changes.append({'type': 'shared_line_path', 'path': f'boundary_curves[{identity}]',
                        'curve_id': identity, 'original_endpoints': copy.deepcopy(old),
                        'adopted_endpoints': copy.deepcopy(pair), 'uses': [use['path'] for use in uses]})
    restored = copy.deepcopy(adopted)
    for identity, pair in updates.items():
        restored['boundary_curves'][identity]['p1'], restored['boundary_curves'][identity]['p2'] = pair
    for binding in bindings['rings']:
        target = _node(restored, binding)
        target['boundary_path'] = copy.deepcopy(binding['boundary_path'])
        # Parametric spaces accept one geometry declaration. Generic cells may
        # already contain derived polygons; preserve that existing representation.
        if binding['space_index'] is not None and not binding['had_derived_ring']:
            target.pop('polygon')
    _rebind_segments(before, restored, updates, changes, reject)
    for name in dict.fromkeys(binding['template'] for binding in bindings['rings']):
        template = restored['templates'][name]
        identities = {use.curve_id for _, _, node, _ in _nodes(template) for use in _uses(node)}
        _expanded(template, {identity: restored['boundary_curves'][identity] for identity in sorted(identities)})
    adopted.clear()
    adopted.update(restored)
    audit.setdefault('changes', []).extend(changes)
    return adopted
