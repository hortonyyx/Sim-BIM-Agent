"""Select source-plan feedback by saved geometry; never infer template validity.

This presentation policy applies to native-mesh runs and parametric builds. All source entities and
diagnostics remain saved, and every floor can still be rendered on demand.
"""
from __future__ import annotations

import hashlib
import json

from src.agent.geometry.source_floor_selection import select_source_floor, select_source_floor_openings


def _key(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def plan_signature(source, floor_id):
    """Translation-in-Z invariant geometry/topology signature, excluding labels.

    Nanometre rounding only absorbs floating-point addition of storey bases; it
    never changes saved coordinates or supplies a geometric acceptance tolerance.
    Roles/evidence are reviewed as text, rather than forcing identical plans back.
    """
    floor, spaces = select_source_floor(source, floor_id)
    base, height = floor['z_floor'], floor['height']
    spanning = set(floor.get('spanning_space_ids', []))
    def points(vertices, *, relative=False):
        ring = [[round(value - (base if relative and i == 2 else 0), 9)
                 for i, value in enumerate(p)] for p in vertices]
        # Ignore start vertex/winding, but preserve edges of a concave ring.
        if len(ring) > 1 and ring[0] == ring[-1]: ring.pop()
        return min([r[i:]+r[:i] for r in (ring, list(reversed(ring))) for i in range(len(r))]) if ring else []
    room_keys = {s['id']: _key({'polygon': points(s['polygon']),
        'z': [round(max(base, s['z_floor']) - base, 9),
              round(min(base+height, s['z_floor']+s['height']) - base, 9)],
        'spanning': s['id'] in spanning, 'enclosure': s.get('enclosure')}) for s in spaces}
    openings = select_source_floor_openings(source, floor, spaces)
    aperture_keys = []
    for row in openings:
        aperture_keys.append(_key({'kind': row['kind'], 'vertices': points(row['vertices'], relative=True),
            'owners': sorted(room_keys.get(sid, 'outside-selected-floor:'+sid) for sid in row['space_ids']),
            'exterior': row.get('exterior'), 'connectivity': row.get('connectivity')}))
    walls = []
    for row in source['boundaries']:
        if row['space_id'] not in room_keys or row.get('geometry_type') != 'wall':
            continue
        vertices = row['vertices']
        if max(p[2] for p in vertices) <= base or min(p[2] for p in vertices) >= base+height:
            continue
        clipped = [[p[0],p[1],min(base+height,max(base,p[2]))] for p in vertices]
        regions = []
        for region in row.get('enclosure_regions', []):
            vertices = region['vertices']
            if max(p[2] for p in vertices) <= base or min(p[2] for p in vertices) >= base+height:
                continue
            clipped_region = [[p[0],p[1],min(base+height,max(base,p[2]))] for p in vertices]
            regions.append(_key({'condition': region['condition'],
                'vertices': points(clipped_region, relative=True)}))
        walls.append(_key({'owner': room_keys[row['space_id']], 'vertices': points(clipped, relative=True),
            'kind': row.get('kind'), 'enclosure': row.get('enclosure'),
            'regions': sorted(regions)}))
    payload = {'height': round(height, 9), 'footprint': points(floor['footprint']),
               'spaces': sorted(room_keys.values()), 'walls': sorted(walls), 'openings': sorted(aperture_keys)}
    return hashlib.sha256(_key(payload).encode()).hexdigest()


def select_plan_feedback(source, parent=None, *, preferred_floor_ids=(), additional_findings=()):
    floors = {f['id']: f for f in source['floors']}
    old = {f['id']: f for f in parent['floors']} if parent else {}
    signatures, failures = {}, {}
    for fid in floors:
        try:
            signatures[fid] = plan_signature(source, fid)
        except (KeyError, ValueError, TypeError) as error:
            failures[fid] = str(error)
    changed = []
    for fid, floor in floors.items():
        if fid not in old or fid in failures:
            changed.append(fid)
            continue
        try:
            same = (floor['z_floor'] == old[fid]['z_floor']
                    and signatures[fid] == plan_signature(parent, fid))
        except (KeyError, ValueError, TypeError):
            same = False
        if not same:
            changed.append(fid)
    # Never let deduplication hide a failing floor. Unknown/global diagnostics
    # conservatively keep every current floor rather than guessing their scope.
    findings = [*source.get('validation', {}).get('findings', []),
                *source.get('unbuilt_openings', []), *source.get('unsupported', []), *additional_findings]
    abnormal = set(failures)
    lookup = {fid: {fid} for fid in floors}
    def retain(identity, floor_ids):
        # IDs need only be unique within their entity kind. A floor and space
        # may share a string; retaining their union cannot hide either scope.
        lookup.setdefault(identity, set()).update(floor_ids)
    for space in source['spaces']:
        retain(space['id'], {fid for fid, floor in floors.items()
            if fid == space['floor_id'] or space['id'] in floor.get('spanning_space_ids', [])})
    for boundary in source['boundaries']:
        retain(boundary['id'], lookup.get(boundary['space_id'], set()))
    for opening in source['openings']:
        retain(opening['id'], set().union(*(lookup.get(sid, set()) for sid in opening['space_ids'])))
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values(): yield from strings(item)
        elif isinstance(value, list):
            for item in value: yield from strings(item)
    for finding in findings:
        located = set().union(*(lookup.get(value, set()) for value in strings(finding)))
        abnormal.update(located or floors)
    wanted = set(changed) | abnormal
    order = list(dict.fromkeys([fid for fid in preferred_floor_ids if fid in floors] + list(floors)))
    selected, represented = [], {}
    for fid in order:
        if fid not in wanted:
            continue
        signature = signatures.get(fid, fid)
        if fid in abnormal or signature not in represented:
            selected.append(fid)
            represented.setdefault(signature, fid)
    return {'policy': 'changed_geometry_representatives_and_all_diagnostics',
        'source_model_sha256': source.get('source_model_sha256'),
        'parent_source_model_sha256': parent.get('source_model_sha256') if parent else None,
        'selected_floor_ids': selected, 'changed_floor_ids': changed,
        'abnormal_floor_ids': sorted(abnormal), 'removed_floor_ids': sorted(set(old)-set(floors)),
        'additional_finding_count': len(additional_findings),
        'represented_by': {fid: represented[signatures[fid]] for fid in wanted
                           if fid in signatures and fid not in selected},
        'unchanged_floor_count': len(floors)-len(changed), 'signature_errors': failures,
        'read_any_floor': {'tool': 'inspect_candidate',
            'arguments': {'floor_id': '<source floor id>', 'include_plan': True}},
        'meaning': 'Only automatic plan feedback is selected; geometry checks and source are complete. '
                   'Changed floors describe plan-visible geometry, not every 3D boundary attribute. '
                   'Role/evidence/note-only edits are textual. Unshown floor plans remain available on demand; '
                   'representative geometry is not original-input fidelity approval.'}
