"""Project evidence onto one saved candidate, without equating history with truth."""
import hashlib
import json

from src.agent.execution.bim_claims import geometry_state, objects, sha, value_targets, parameter_slots
from src.agent.geometry.source_model import _digest


def targets(operation):
    name = operation['op']
    if name in {'update_window', 'update_opening'}:
        return [(name.removeprefix('update_'), operation['id'])]
    if name == 'move_shared_wall':
        return [('space', identity) for identity in operation['space_ids']]
    if name == 'set_component_thickness':
        return [('boundary', operation['boundary_id'])]
    if name == 'reshape_spaces':
        return [('space', row['id']) for row in operation['spaces']]
    return []


def parameter_present(proposal, operation, parameter):
    """A successful batch may subsequently overwrite one of its own bindings."""
    name = operation['op']
    if name == 'add_opening':
        intended = operation['opening']
        row = next((r for r in proposal['geometry'].get('openings', []) if r['id'] == intended['id']), None)
        return row is not None and row.get(parameter) == intended[parameter] and all(
            row.get(k) == intended.get(k) for k in ('kind', 'space_id', 'other_space_id'))
    if name == 'reshape_spaces':
        _, space_index, _, vertex, axis = parameter.split('.')
        intended = operation['spaces'][int(space_index)]
        row = next((c for f in proposal['geometry']['floors'] for c in f['cells']
                    if c['id'] == intended['id']), None)
        if row is None:
            return False
        ring = row.get('polygon')
        if ring is None:
            x, y = row['x'], row['y']
            ring = [[x[0], y[0]], [x[1], y[0]], [x[1], y[1]], [x[0], y[1]]]
        try:
            return ring[int(vertex)][int(axis)] == intended['polygon'][int(vertex)][int(axis)]
        except IndexError:
            return False
    if name in {'update_window', 'update_opening'}:
        rows = proposal['geometry'].get(name.removeprefix('update_') + 's', [])
        row = next((r for r in rows if r['id'] == operation['id']), None)
        return row is not None and row.get(parameter) == operation['changes'][parameter]
    if name == 'set_component_thickness':
        return any(operation['boundary_id'] in row['boundary_ids'] and
                   row['thickness_m'] == operation['thickness_m']
                   for row in proposal.get('component_attributes', []))
    if name == 'move_shared_wall':
        from src.agent.geometry.proposal_edits import apply_proposal_edits
        try:
            return geometry_state(apply_proposal_edits(proposal, [operation])) == geometry_state(proposal)
        except (ValueError, KeyError, TypeError):
            return False
    return False


def context(proposal, source, references):
    """Include physical hosts so an unchanged number on a moved host goes stale."""
    inventory = objects(proposal, source)
    # Numerical claims concern the physical space, not its use label/evidence.
    # Use the compiled geometry (including storey heights) so a role-only edit
    # retains height confirmations, while moved/resized/rehosted objects do not.
    for space in source['spaces']:
        inventory[('space', space['id'])] = {key: space[key] for key in
            ('id', 'floor_id', 'polygon', 'z_floor', 'height')}
    result = {}
    for kind, identity in references:
        row = inventory.get((kind, identity))
        result[f'{kind}:{identity}'] = row
        if row is None:
            continue
        if kind in {'window', 'opening'}:
            actual = next((o for o in source['openings'] if o['id'] == identity), None)
            if actual:
                result[f'actual:{identity}'] = {k: v for k, v in actual.items() if k not in {'source_refs', 'assumptions'}}
                for host in actual.get('space_ids', []):
                    result[f'host:{host}'] = inventory.get(('space', host))
        elif kind == 'boundary':
            result[f'thickness:{identity}'] = [r for r in proposal.get('component_attributes', [])
                                              if identity in r['boundary_ids']]
    return result


def lineage(store, candidate):
    found = {}
    while candidate not in found:
        proposal, source = store.candidate(candidate)
        found[candidate] = (proposal, source)
        provenance = source.get('generation', {}).get('provenance', {})
        parent = provenance.get('parent_candidate')
        if not parent:
            break
        path = store.toolkit.candidate_path(parent) / 'proposal.json'
        if hashlib.sha256(path.read_bytes()).hexdigest() != provenance.get('parent_proposal_sha256'):
            raise ValueError('candidate ancestry changed; cannot project claim state')
        candidate = parent
    return found


def project(store, candidate):
    ancestry = lineage(store, candidate)
    current, source = ancestry[candidate]
    history = store.status()
    confirmations = [json.loads(p.read_text()) for p in sorted(store.folder.glob('confirmation_*.json'))]
    states, other = [], []
    # Ancestry is one read-only snapshot for this projection. Reuse its computed
    # hashes locally, still comparing every claim/application's expected value.
    proposal_hashes, source_hashes = {}, {}
    for row in history['claims']:
        origin = row['claim']['candidate']
        if origin in ancestry and origin not in proposal_hashes:
            proposal_hashes[origin] = sha(ancestry[origin][0])
        if origin not in ancestry or proposal_hashes[origin] != row['parent_proposal_sha256']:
            other.append(row['id'])
            continue
        disposition = (row['decision'] or {}).get('disposition', 'undecided')
        applicable = [a for a in row['applications'] if a.get('candidate') in ancestry and a['status'] == 'applied']
        applicable += [c for c in confirmations if c['candidate'] in ancestry and row['id'] in c['claim_ids']]
        retained, stale = [], []
        for application in applicable:
            saved_candidate = application['candidate']
            saved, saved_source = ancestry[saved_candidate]
            expected_hash = application.get('source_model_sha256')
            if expected_hash:
                if saved_candidate not in source_hashes:
                    source_hashes[saved_candidate] = _digest(
                        {k: v for k, v in saved_source.items() if k != 'source_model_sha256'})
                if source_hashes[saved_candidate] != expected_hash:
                    raise ValueError('applied source changed; cannot project claim state')
            for binding in application['evidence']['bindings']:
                if binding['claim_id'] != row['id']:
                    continue
                operation = application['resolved_operations'][binding['operation_index']]
                refs = binding.get('targets', targets(operation))
                result_refs = ([['opening', operation['opening']['id']]]
                               if operation['op'] == 'add_opening' else refs)
                item = {'record': application['id'], 'kind': application.get('kind', 'application'),
                        'value': binding['value_field'], 'targets': refs, 'parameter': binding['parameter'],
                        'result_targets': result_refs}
                matches = (parameter_present(current, operation, binding['parameter']) and
                           context(saved, saved_source, refs + result_refs) == context(current, source, refs + result_refs))
                (retained if matches else stale).append(item)
        expected = {(value, ref['kind'], ref['id']) for value in row['resolved_values']
                    for ref in value_targets(row['claim'], value)}
        # A repeated scalar can drive several vertices of the SAME space. One
        # surviving vertex cannot stand in for another overwritten in that batch.
        failed_groups = {(b['record'], b['value'], kind, identity)
                         for b in stale for kind, identity in b['targets']}
        covered = {(b['value'], kind, identity) for b in retained for kind, identity in b['targets']
                   if (b['record'], b['value'], kind, identity) not in failed_groups}
        if disposition != 'adopted':
            state = disposition
        elif expected <= covered:
            state = 'applied_current' if any(b['kind'] == 'application' for b in retained) else 'confirmed_unchanged'
        elif covered:
            state = 'partially_satisfied'
        elif stale:
            state = 'changed_since_check'
        else:
            state = 'pending_application'
        states.append({'id': row['id'], 'state': state, 'decision': row['decision'],
            'objects': row['claim']['objects'], 'values': row['resolved_values'],
            'reason': row['claim']['reason'], 'unresolved': row['claim']['unresolved'],
            'retained_bindings': retained, 'changed_bindings': stale,
            'missing_bindings': [list(v) for v in sorted(expected - covered)],
            'failed_attempts': [a['id'] for a in row['applications'] if a['status'] == 'failed' and a['parent_candidate'] in ancestry]})
    # Old runs can carry applied evidence in their proposal. Keep that history
    # explicit; never alias its run-local claim IDs to this run's new decisions.
    inherited = []
    for audit in current['geometry'].get('corrections', []):
        if audit.get('operation') != 'claim_application':
            continue
        for identity, snapshot in audit.get('claims', {}).items():
            record = snapshot['record']
            if not any(sha(record) == sha({k: v for k, v in r.items() if k not in {'decision', 'applications', 'applied_anywhere'}}) for r in history['claims']):
                inherited.append({'id': identity, 'record_sha256': sha(record),
                    'state': 'inherited_application_not_rechecked', 'unresolved': record['claim'].get('unresolved', [])})
    replaced = [a for a in current['geometry'].get('corrections', []) if a.get('operation') == 'replace_note']
    return {'candidate': candidate, 'claims': states, 'other_branch_claim_ids': other,
            'inherited_observations': inherited, 'superseded_notes': replaced,
            'drawing_fidelity': 'not_evaluated',
            'note': 'Current numerical/context consistency only; neither retained nor confirmed means drawing truth.'}


def confirm(store, candidate, operations):
    """Validate exact no-op parameter intents, saving evidence without a new BIM."""
    from src.agent.geometry.proposal_edits import apply_proposal_edits
    proposal, source = store.candidate(candidate)
    resolved, evidence = store.resolve_operations(candidate, operations)
    if any(o['op'] not in {'update_window', 'update_opening', 'move_shared_wall', 'reshape_spaces'} for o in resolved):
        raise ValueError('confirmation supports window/opening parameters and shared walls')
    bound = {(b['operation_index'], b['parameter']) for b in evidence['bindings']}
    required = {(i, path) for i, o in enumerate(resolved) for _, _, path, _, _ in parameter_slots(o)}
    if not bound or bound != required:
        raise ValueError('every confirmed parameter must reference an adopted claim')
    checked = apply_proposal_edits(proposal, resolved)
    if geometry_state(checked) != geometry_state(proposal):
        raise ValueError('claim differs from current geometry; revise or defer instead of confirming unchanged')
    return store._write('confirmation', {'candidate': candidate, 'kind': 'confirmation',
        'parent_proposal_sha256': sha(proposal), 'claim_ids': sorted(evidence['claims']),
        'source_model_sha256': source['source_model_sha256'],
        'resolved_operations': resolved, 'evidence': evidence, 'status': 'confirmed_unchanged'})
