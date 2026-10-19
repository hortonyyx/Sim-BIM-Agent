"""Select source-plan feedback by saved geometry; never infer template validity.

This presentation policy applies to native-mesh runs and parametric builds. All source entities and
diagnostics remain saved, and every floor can still be rendered on demand.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

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


def _diagnostic_tokens(source, floor_id):
    """Map exact local IDs to geometry identities; prose is never rewritten."""
    floor, spaces = select_source_floor(source, floor_id)
    base = floor['z_floor']
    tokens = {floor_id: _key(['floor'])}
    def add(identity, value):
        token = _key(value)
        # Cross-kind ID collisions cannot be normalized safely.
        tokens[identity] = token if identity not in tokens else None
    def vertices(row):
        return [[round(p[0], 9), round(p[1], 9), round(p[2]-base, 9)]
                for p in row['vertices']]
    for row in spaces:
        add(row['id'], ['space', row['polygon'], round(row['z_floor']-base, 9),
                        row['height'], row.get('role'), row.get('enclosure')])
    for row in source['boundaries']:
        if row['space_id'] in tokens:
            add(row['id'], ['boundary', tokens[row['space_id']], vertices(row),
                            row.get('kind'), row.get('geometry_type'), row.get('enclosure')])
    for row in select_source_floor_openings(source, floor, spaces):
        add(row['id'], ['opening', row['kind'], vertices(row),
            sorted(tokens.get(sid) or sid for sid in row['space_ids']), row.get('connectivity')])
    return tokens


def select_plan_feedback(source, parent=None, *, preferred_floor_ids=(), additional_findings=(),
                         view_context_by_floor=None):
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
    # Unknown/global/cross-floor diagnostics retain every affected floor. Only
    # exact local diagnostics over the same plan and evidence context can share
    # an automatic image; all original findings and IDs remain in this record.
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
    per_floor = {fid: [] for fid in floors}
    ungroupable = set(failures)
    diagnostic_records = []
    for index, finding in enumerate(findings):
        identities = sorted({value for value in strings(finding) if value in lookup})
        located = set().union(*(lookup[value] for value in identities))
        abnormal.update(located or floors)
        affected = located or set(floors)
        if len(located) != 1:
            ungroupable.update(affected)
        for fid in affected:
            per_floor[fid].append(index)
        diagnostic_records.append({'finding_index': index, 'floor_ids': sorted(affected),
                                   'located_ids': identities, 'unlocated': not bool(located)})
    def diagnostic_key(fid):
        tokens = _diagnostic_tokens(source, fid)
        def normalized(value):
            if isinstance(value, str) and value in lookup:
                if not tokens.get(value):
                    raise ValueError('ambiguous diagnostic identity')
                return {'entity_geometry': tokens[value]}
            if isinstance(value, list):
                return [normalized(item) for item in value]
            if isinstance(value, dict):
                return {key: normalized(item) for key, item in value.items()}
            return value
        return _key([signatures[fid], (view_context_by_floor or {}).get(fid),
                     sorted(_key(normalized(findings[i])) for i in per_floor[fid])])
    wanted = set(changed) | abnormal
    order = list(dict.fromkeys([fid for fid in preferred_floor_ids if fid in floors] + list(floors)))
    selected, representatives, represented = [], {}, {}
    groups = {}
    for fid in order:
        if fid not in wanted:
            continue
        signature = ('geometry', signatures.get(fid, fid))
        if fid in abnormal:
            try:
                signature = ('diagnostic', diagnostic_key(fid)) if fid not in ungroupable else ('floor', fid)
            except (KeyError, ValueError, TypeError):
                signature = ('floor', fid)
        if signature not in representatives:
            selected.append(fid)
            representatives[signature] = fid
        else:
            represented[fid] = representatives[signature]
        if fid in abnormal:
            groups.setdefault(representatives[signature], []).append(fid)
    return {'policy': 'changed_geometry_and_equivalent_diagnostic_representatives',
        'source_model_sha256': source.get('source_model_sha256'),
        'parent_source_model_sha256': parent.get('source_model_sha256') if parent else None,
        'selected_floor_ids': selected, 'changed_floor_ids': changed,
        'abnormal_floor_ids': sorted(abnormal), 'removed_floor_ids': sorted(set(old)-set(floors)),
        'additional_finding_count': len(additional_findings),
        'represented_by': represented,
        'diagnostic_groups': [{'representative_floor_id': representative, 'floor_ids': members,
            'finding_indices': sorted({i for fid in members for i in per_floor[fid]}),
            'located_ids': sorted({identity for fid in members for i in per_floor[fid]
                                   for identity in diagnostic_records[i]['located_ids']})}
            for representative, members in groups.items()],
        'diagnostic_locations': diagnostic_records, 'findings': findings,
        'unchanged_floor_count': len(floors)-len(changed), 'signature_errors': failures,
        'read_any_floor': {'tool': 'inspect_candidate',
            'arguments': {'floor_id': '<source floor id>', 'include_plan': True}},
        'meaning': 'Only automatic plan feedback is selected; geometry checks and source are complete. '
                   'Changed floors describe plan-visible geometry, not every 3D boundary attribute. '
                   'Role/evidence/note-only edits are textual. Unshown floor plans remain available on demand; '
                   'representative geometry is not original-input fidelity approval.'}


def _render_contract():
    import PIL
    import shapely
    from src.agent.geometry import source_plan_view, source_floor_selection, source_naming
    return {'schema': 1, 'size': [1080, 800], 'Pillow': PIL.__version__,
            'Shapely': shapely.__version__, 'GEOS': shapely.geos_version_string,
            'code': {Path(module.__file__).name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                     for module in (source_plan_view, source_floor_selection, source_naming)},
            'cache_code': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def render_source_plan_cached(source, floor_id, cache_dir):
    """Reuse only identical render inputs, never geometry checks or old source metadata."""
    from PIL import Image
    from src.agent.geometry.source_image_overlay import _source_hash
    from src.agent.geometry.source_naming import public_names_for_display
    from src.agent.geometry.source_plan_view import render_source_plan
    source_hash = _source_hash(source)
    floor, spaces = select_source_floor(source, floor_id)
    if not spaces:
        raise ValueError('unknown floor_id')
    openings = select_source_floor_openings(source, floor, spaces)
    names = public_names_for_display(source)
    points = [point for row in spaces for point in row['polygon']]
    metadata = {'floor_id': floor_id, 'floor_name': names.get('floors', {}).get(floor_id, floor_id),
        'space_names': {row['id']: names.get('spaces', {}).get(row['id'], row['id']) for row in spaces},
        'naming_scheme': names.get('scheme_version'), 'space_ids': [row['id'] for row in spaces],
        'opening_ids': [row['id'] for row in openings],
        'world_bounds_m': {'x': [min(p[0] for p in points), max(p[0] for p in points)],
                           'y': [min(p[1] for p in points), max(p[1] for p in points)]},
        'scope': 'actual saved source plan; no original calibration, height verification or drawing-fidelity verdict'}
    key = hashlib.sha256(_key({'contract': _render_contract(), 'floor': floor,
        'spaces': spaces, 'openings': openings, 'metadata': metadata}).encode()).hexdigest()
    directory = Path(cache_dir)
    png_path, record_path = directory / (key+'.png'), directory / (key+'.json')
    try:
        record = json.loads(record_path.read_bytes())
        if record['key'] != key or record['metadata'] != metadata:
            raise ValueError('cache metadata changed')
        raw = png_path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != record['image_sha256']:
            raise ValueError('cache PNG changed')
        with Image.open(io.BytesIO(raw)) as saved:
            if saved.size != (1080, 800) or saved.mode != 'RGB':
                raise ValueError('cache PNG shape changed')
            saved.load()
            picture = saved.copy()
        return picture, {**metadata, 'source_model_sha256': source_hash,
                         'render_cache': {'key': key, 'hit': True, 'image_sha256': digest}}
    except (OSError, ValueError, TypeError, KeyError):
        pass
    picture, actual_metadata = render_source_plan(source, floor_id)
    buffer = io.BytesIO()
    picture.save(buffer, format='PNG')
    raw = buffer.getvalue()
    digest = hashlib.sha256(raw).hexdigest()
    # A renderer metadata change is still valid feedback, but not a reusable
    # cache entry until this adapter understands the complete new contract.
    if actual_metadata == {**metadata, 'source_model_sha256': source_hash}:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            png_path.write_bytes(raw)
            record_path.write_bytes((_key({'key': key, 'metadata': metadata,
                                          'image_sha256': digest})+'\n').encode())
        except OSError:
            pass
    return picture, {**actual_metadata,
                     'render_cache': {'key': key, 'hit': False, 'image_sha256': digest}}
