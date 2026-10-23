"""Bounded, hash-verified source-edit history beside the existing edit route.

This advice never grants parameter authority or computes a net geometry diff.
Only explicit parent links are followed; candidate numbering is not recency.
"""
from __future__ import annotations

from collections import Counter
import copy
import hashlib
import json


MAX_ADVISORY_BYTES = 2048
MAX_CHAIN_LENGTH = 32
_MEANING = ("Source-edit history, not a net diff or a replay list. A parameter patch starts "
            "from its saved plan; later source edits are not automatically inherited. "
            "Preserve, revise or drop them explicitly.")


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')


def _short(value, limit=160):
    return str(value).encode('utf-8')[:limit].decode('utf-8', errors='ignore')


def _read_node(toolkit, candidate, bindings):
    from scripts.tool_scripts.bim_agent_parametric import _candidate, _inside, _load
    from src.agent.geometry.source_model import _digest
    folder = _candidate(toolkit, candidate)
    values, hashes = {}, {}
    for name in ('source_model.json', 'report.json', 'proposal.json'):
        path = _inside(toolkit, folder / name)
        raw = path.read_bytes()
        values[name] = _load(raw)
        hashes[name] = hashlib.sha256(raw).hexdigest()
        bindings[path] = hashes[name]
    source, report, proposal = (values[name] for name in
                                ('source_model.json', 'report.json', 'proposal.json'))
    if not all(isinstance(value, dict) for value in (source, report, proposal)):
        raise ValueError('source, report and proposal must be objects')
    source_hash = source.get('source_model_sha256')
    if source_hash != _digest({key: value for key, value in source.items() if key != 'source_model_sha256'}):
        raise ValueError('source internal hash mismatch')
    generation = source.get('generation')
    if not isinstance(generation, dict):
        raise ValueError('source generation record unavailable')
    if (generation.get('proposal_sha256') != hashes['proposal.json']
            or report.get('proposal_sha256') != hashes['proposal.json']
            or report.get('source_model_sha256') != source_hash):
        raise ValueError('source/report/proposal binding mismatch')
    provenance = generation.get('provenance')
    if not isinstance(provenance, dict) or provenance != report.get('provenance'):
        raise ValueError('source/report provenance mismatch')
    corrections = generation.get('corrections')
    if not isinstance(corrections, list) or any(not isinstance(row, dict) for row in corrections):
        raise ValueError('source correction history unavailable')
    if corrections != proposal.get('geometry', {}).get('corrections', []):
        raise ValueError('source/proposal correction history mismatch')
    return {'candidate': candidate, 'source_sha256': hashes['source_model.json'],
            'source_model_sha256': source_hash, 'proposal_sha256': hashes['proposal.json'],
            'report_sha256': hashes['report.json'], 'provenance': provenance,
            'corrections': corrections}


def _operation(row, index):
    name = row.get('operation', row.get('op'))
    if not isinstance(name, str) or not name.strip():
        raise ValueError('correction operation identity unavailable')
    identities = set()
    for field in ('id', 'space_id', 'neighbor_space_id'):
        if isinstance(row.get(field), str):
            identities.add(row[field])
    for field in ('space_ids', 'opening_ids'):
        if isinstance(row.get(field), list):
            identities.update(value for value in row[field] if isinstance(value, str))
    return {'correction_index': index, 'operation': name, 'object_ids': sorted(identities),
            'source_pointer': f'/generation/corrections/{index}', 'record': copy.deepcopy(row)}


def _collect(toolkit, candidate):
    from scripts.tool_scripts.bim_agent_parametric import _current_state, _sha
    result = {'schema_version': 1, 'status': 'unavailable', 'source_candidate': candidate,
              'chain': [], 'steps': [], 'automatic_merge': False, 'meaning': _MEANING,
              'scope': 'Explicit named object IDs in verified history; not geometric impact or fidelity.'}
    bindings, seen = {}, set()
    try:
        node = _read_node(toolkit, candidate, bindings)
        result['source_sha256'] = node['source_sha256']
        for _ in range(MAX_CHAIN_LENGTH):
            cursor = node['candidate']
            if cursor in seen:
                raise ValueError('cycle in explicit parent chain')
            seen.add(cursor)
            result['chain'].append({key: node[key] for key in
                ('candidate', 'source_sha256', 'source_model_sha256', 'proposal_sha256', 'report_sha256')})
            state = _current_state(toolkit, cursor)
            if state is not None:
                if state.get('source_file_sha256') != node['source_sha256']:
                    raise ValueError('exact ancestor source binding mismatch')
                result['exact_parametric_ancestor'] = {'candidate': cursor,
                    'plan_sha256': state['plan_sha256'], 'source_sha256': node['source_sha256']}
                result['status'] = 'verified_lineage'
                break
            parent = node['provenance'].get('parent_candidate')
            if not parent:
                raise ValueError('no exact parametric ancestor in the explicit parent chain')
            if parent in seen:
                raise ValueError('cycle in explicit parent chain')
            previous = _read_node(toolkit, parent, bindings)
            if node['provenance'].get('parent_proposal_sha256') != previous['proposal_sha256']:
                raise ValueError('parent proposal hash mismatch')
            old, new = previous['corrections'], node['corrections']
            if len(new) < len(old) or new[:len(old)] != old:
                raise ValueError('parent correction history is not an exact prefix')
            operations = [_operation(row, index) for index, row in enumerate(new) if index >= len(old)]
            result['steps'].append({'candidate': cursor, 'parent_candidate': parent,
                'parent_proposal_sha256': previous['proposal_sha256'], 'operations': operations})
            node = previous
        else:
            raise ValueError(f'parent chain exceeds {MAX_CHAIN_LENGTH} candidates')
        if any(_sha(path) != sha for path, sha in bindings.items()):
            raise ValueError('candidate files changed while reading lineage')
        # Recheck the complete parameter/draft binding after chain traversal.
        if _current_state(toolkit, cursor) != state:
            raise ValueError('exact parameter binding changed while reading lineage')
    except (ValueError, TypeError, KeyError, OSError) as error:
        result['status'] = 'partial' if result['steps'] else 'unavailable'
        result['reason'] = str(error)
        result.pop('exact_parametric_ancestor', None)
    operations = [operation for step in result['steps'] for operation in step['operations']]
    objects = sorted({identity for operation in operations for identity in operation['object_ids']})
    result['intervening_changes'] = {'operation_count': len(operations),
        'by_operation': dict(sorted(Counter(row['operation'] for row in operations).items())),
        'object_count': len(objects), 'object_ids': objects,
        'history_complete': result['status'] == 'verified_lineage'}
    return result


def _store_report(toolkit, report):
    """Use the existing immutable tool_reports reader; never alter candidate files."""
    from scripts.tool_scripts.bim_agent_parametric import _inside
    raw = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    sha = hashlib.sha256(raw).hexdigest()
    relative = f'tool_reports/parametric_lineage_{sha}.json'
    path = _inside(toolkit, toolkit.run / relative)
    path.parent.mkdir(exist_ok=True)
    try:
        with path.open('xb') as stream:
            stream.write(raw)
    except FileExistsError:
        if path.read_bytes() != raw:
            raise ValueError('existing immutable lineage report changed')
    return {'report_file': relative, 'report_sha256': sha,
            'read': {'tool': 'read_candidate_items', 'arguments': {
                'candidate': '', 'collection': 'report', 'report_file': relative, 'json_pointer': '/steps'}}}


def _summary(report, reference):
    changes = report['intervening_changes']
    result = {key: report[key] for key in ('status', 'source_candidate', 'source_sha256',
              'exact_parametric_ancestor') if key in report}
    if 'reason' in report:
        result['reason'] = _short(report['reason'])
    result.update(chain=[row['candidate'] for row in report['chain'][:3]],
        omitted_chain_candidates=max(0, len(report['chain']) - 3),
        intervening_changes={key: changes[key] for key in
            ('operation_count', 'by_operation', 'object_count', 'history_complete')},
        meaning=_MEANING, automatic_merge=False, details=reference)
    summary = result['intervening_changes']
    summary['by_operation'] = dict(summary['by_operation'])
    summary['object_ids_preview'] = changes['object_ids'][:6]
    summary['omitted_object_ids'] = len(changes['object_ids']) - len(summary['object_ids_preview'])
    summary['omitted_operation_types'] = 0
    summary['object_count_basis'] = 'Distinct explicit IDs, not affected spaces or geometry scope.'
    while len(_bytes(result)) > MAX_ADVISORY_BYTES:
        if summary['object_ids_preview']:
            summary['object_ids_preview'].pop()
            summary['omitted_object_ids'] += 1
        elif summary['by_operation']:
            summary['by_operation'].popitem()
            summary['omitted_operation_types'] += 1
        elif result['chain']:
            result['chain'].pop()
            result['omitted_chain_candidates'] += 1
        else:
            # Candidate IDs are filesystem names, but still do not let an
            # unusual name turn an advisory into an unbounded model response.
            return {'status': 'unavailable', 'reason': 'lineage identities exceed summary byte budget',
                    'automatic_merge': False, 'details': reference}
    return result


def parametric_lineage_advisory(toolkit, candidate):
    """Failure of this optional advice must never mask an existing save result."""
    try:
        report = _collect(toolkit, candidate)
        return _summary(report, _store_report(toolkit, report))
    except Exception as error:
        return {'status': 'unavailable', 'reason': _short(f'lineage advice unavailable: {error}'),
                'automatic_merge': False}
