"""Model-facing summaries; full immutable replies remain readable through MCP.

A3-T usage audit: five GLM/Qwen/T1 runs reuse IDs, draft hashes, calibration,
partition coordinates and opening heights. Keep those; remove duplicated
provenance, polygon/vertex copies and projection internals from routine replies.
"""
from __future__ import annotations

import copy
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re

COMPACT_TOOLS = frozenset({'build_bim', 'build_plan_bim', 'revise_bim', 'revise_plan_bim',
                          'build_parametric_bim', 'assemble_plan_bim', 'check_openings', 'finish_bim',
                          'claim_transaction', 'view_pixel_profile'})


def pick(value, keys):
    return {key: value[key] for key in keys if key in value}


def table(rows):
    """Keep every selected value and its column while writing repeated keys once."""
    if not rows:
        return rows
    columns = list(dict.fromkeys(key for row in rows for key in row))
    return dict(columns=columns, rows=[[row.get(key) for key in columns] for row in rows])


def compilation_summary(value, *, inventory_present=False):
    result = pick(value, ('mode', 'floor_id', 'image_name', 'image_size', 'space_count', 'footprint'))
    result['calibration'] = pick(value.get('calibration', {}),
        ('status', 'world_metres_per_pixel', 'x_anchors', 'y_anchors'))
    result['partition_mapping'] = [pick(row, ('id', 'partition_id', 'pixel_points', 'world_points_m'))
                                   for row in value.get('partition_mapping', [])]
    result['space_mapping'] = [pick(row, ('space_id', 'seed', 'source_refs'))
                               for row in value.get('space_mapping', [])]
    result['opening_hosts'] = [pick(row, ('opening_id', 'p1_pixel', 'p2_pixel', 'width_m')) if inventory_present
                               else {k: v for k, v in row.items() if k != 'source_refs'}
                               for row in value.get('opening_hosts', [])]
    for key in ('partition_mapping', 'space_mapping', 'opening_hosts'):
        result[key] = table(result[key])
    return result


def inventory_summary(value):
    result = pick(value, ('counts', 'opening_count', 'scope'))
    result['floors'] = []
    for floor in value.get('floors', []):
        row = pick(floor, ('floor_id', 'counts', 'opening_count'))
        columns = ('id', 'kind', 'space_ids', 'plan_endpoints', 'z_m', 'exterior')
        if any('floor_ids' in opening for opening in floor.get('openings', [])):
            columns += ('floor_ids',)
        row['columns'], row['rows'] = list(columns), []
        for opening in floor.get('openings', []):
            item = dict(opening)
            zs = [v[2] for v in opening.get('vertices', [])]
            if zs:
                item['z_m'] = [min(zs), max(zs)]
            row['rows'].append([item.get(key) for key in columns])
        result['floors'].append(row)
    result['spaces'] = table([pick(row, ('space_id', 'floor_id', 'opening_count', 'counts'))
                             for row in value.get('spaces', [])])
    return result


def projection_summary(value):
    # The image itself is still delivered, so retain its coordinate frame/legend.
    rows = []
    for row in value:
        summary = pick(row, ('candidate', 'floor_id', 'image', 'image_sha256', 'source_model_sha256', 'anchors',
            'box_original_pixels', 'original_size', 'returned_size', 'original_pixels_per_returned_pixel',
            'overlay_image', 'overlay_metadata', 'legend', 'out_of_image', 'calibration_unverified',
            'drawing_fidelity', 'automatic_projection', 'trigger_action'))
        if 'reused_calibration' in row:
            summary['reused_calibration'] = pick(row['reused_calibration'],
                ('calibration_id', 'registered_by_candidate', 'image', 'floor_id'))
        evidence = row.get('wall_evidence_projection', {})
        if evidence.get('endpoints') or evidence.get('walls'):
            summary['wall_evidence_projection'] = evidence
        rows.append(summary)
    return rows



def height_summary(report):
    result = copy.deepcopy(report)
    rows = result.get('openings', [])
    # Missing bindings are a coverage limit, not an individual geometric fault.
    retained = [r for r in rows if r.get('evidence') or r.get('status') != 'missing'
                or set(r.get('issues', [])) - {'no_current_height_binding'}]
    result['openings'] = retained
    result['unbound_without_other_issues'] = len(rows) - len(retained)
    return result


def change_fields(rows):
    result = []
    for row in rows:
        if 'before' not in row and 'after' not in row:
            result.append(row)
            continue
        before, after = row.get('before'), row.get('after')
        if not isinstance(before, dict) or not isinstance(after, dict):
            fields = sorted((before or after or {}).keys())
        else:
            fields = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
        result.append(dict(kind=row.get('kind'), id=row['id'], fields=fields,
            change='added' if before is None else 'removed' if after is None else 'updated'))
    return result


def precision_summary(report):
    # building_precision_v1 reported; v2 (Q1 hard constraints) passes or rejects.
    if report.get('status') not in {'reported', 'pass', 'rejected'}:
        return report
    result = pick(report, ('schema', 'status', 'source_model_sha256', 'total', 'counts', 'evidence_errors'))
    placement = copy.deepcopy(report.get('wall_placement', {}))
    if placement:
        # No truncation, including serious placement findings after the fourth.
        if placement.get('status') == 'not_assessed' and not placement.get('total'):
            placement = pick(placement, ('status', 'checked_positions', 'total', 'reason', 'skipped', 'calibration_errors'))
        elif not placement.get('total'):
            placement.pop('items', None)
        result['wall_placement'] = placement
    changes = report.get('changes', {})
    result['comparison'] = pick(changes, ('status', 'previous_candidate'))
    if not report.get('items') and not changes.get('resolved'):
        c = report.get('coverage', {})
        tolerances = {f: t.get('default_m') for f, t in report.get('tolerances', {}).items()}
        result['coverage'] = (f"0 findings; floors {c.get('floors', [])}, tolerances m {tolerances}, "
            f"storey pairs {c.get('adjacent_storey_pairs', [])}, {c.get('orthogonal_wall_lines', 0)} wall lines; "
            f"unassessed floors {c.get('floors_without_tolerance', [])}, "
            f"nonorthogonal walls {c.get('nonorthogonal_walls_not_checked', 0)}; "
            f"not checked: {', '.join(report.get('not_checked', []))}; source/basis in details_file.")
        return result
    result.update(pick(report, ('coverage', 'tolerances', 'not_checked', 'meaning')))
    lines, identities, groups = {}, {}, defaultdict(list)
    def line_id(line):
        key = json.dumps(line, sort_keys=True)
        if key not in identities:
            identity = 'L' + str(len(lines) + 1)
            identities[key] = identity
            lines[identity] = line
        return identities[key]
    rows = [(row, 'new' if i in changes.get('new', []) else 'unchanged'
             if i in changes.get('unchanged', []) else 'current') for i, row in enumerate(report['items'])]
    rows += [(row, 'resolved') for row in changes.get('resolved', [])]
    for row, state in rows:
        item = {k: v for k, v in row.items() if k != 'type'}
        for key in ('lines', 'align_to_options'):
            if key in item:
                item[key] = [line_id(v) for v in item[key]]
        if 'align_to' in item:
            item['align_to'] = line_id(item['align_to'])
        groups[(row['type'], state)].append(item)
    result['wall_lines'] = table([dict(line_id=identity, **line) for identity, line in lines.items()])
    result['groups'] = []
    for (kind, state), rows in groups.items():
        common = {key: value for key, value in rows[0].items()
                  if len(rows) > 1 and all(row.get(key) == value for row in rows[1:])}
        result['groups'].append(dict(type=kind, change=state, common=common,
            **table([{k: v for k, v in row.items() if k not in common} for row in rows])))
    return result


def profile_summary(result):
    reply = copy.deepcopy(result)
    for key in ('panel_note', 'evidence_note', 'display_note'):
        reply.pop(key, None)
    positive = reply.pop('positive_support_runs', [])
    reply['positive_support_summary'] = dict(run_count=len(positive),
        local_peak_count=sum(len(row.get('support_peaks', [])) for row in positive),
        max_count=max((row.get('max_count', 0) for row in positive), default=0))
    def excluded(value):
        return {**pick(value, ('coordinate_count', 'matching_pixels', 'minimum_count', 'support_length')),
                'interval_count': len(value.get('intervals', []))}
    reply['threshold_excluded_support'] = excluded(result.get('threshold_excluded_support', {}))
    cross = result.get('cross_axis_profile', {})
    reply['cross_axis_profile'] = {**pick(cross, ('axis', 'min_fraction', 'minimum_count', 'support_length')),
        'run_count': len(cross.get('runs', [])),
        'max_count': max((r.get('max_count', 0) for r in cross.get('runs', [])), default=0),
        'threshold_excluded_support': excluded(cross.get('threshold_excluded_support', {}))}
    crop = reply.get('crop_context', {})
    crop.pop('note', None)
    edges = crop.pop('edge_support_intervals', {})
    crop['edge_interval_counts'] = {k: len(v) for k, v in edges.items()}
    return reply


def summarize_reply(result):
    """Pure presentation: errors, findings, IDs, heights and geometry stay explicit."""
    reply = copy.deepcopy(result)
    precision = reply.get('building_precision')
    if isinstance(precision, dict):
        reply['building_precision'] = precision_summary(precision)
    if isinstance(reply.get('height_coverage'), dict):
        reply['height_coverage'] = height_summary(reply['height_coverage'])
    for key in ('opening_inventory', 'inventory'):
        if isinstance(reply.get(key), dict):
            reply[key] = inventory_summary(reply[key])
    if isinstance(reply.get('plan_compilation'), dict):
        reply['plan_compilation'] = compilation_summary(reply['plan_compilation'], inventory_present='opening_inventory' in reply)
    if isinstance(reply.get('source_image_projections'), list):
        reply['source_image_projections'] = projection_summary(reply['source_image_projections'])
    if isinstance(reply.get('provenance'), dict):
        # plan_input and assembly already exist at the top level; hash bindings
        # and claim application file references remain in their original places.
        reply['provenance'] = {k: v for k, v in reply['provenance'].items()
                               if k not in {'plan_input', 'plan_assembly'}}
    application = reply.get('claim_application')
    if (isinstance(application, dict) and 'submitted_operations' in application
            and application['submitted_operations'] == application.get('resolved_operations')):
        # These are the exact operations the caller just supplied, not new facts.
        application.pop('submitted_operations')
        application.pop('resolved_operations')
        application['submitted_operations_equal_resolved'] = True
    if isinstance(application, dict):
        if 'changes' in application:
            application['changes'] = change_fields(application['changes'])
        if isinstance(application.get('outside_declared_scope'), list):
            application['outside_declared_scope'] = change_fields(application['outside_declared_scope'])
    return reply


def _reply_bytes(value):
    return len(json.dumps(value, ensure_ascii=False).encode('utf-8'))


def compact_reply(run: Path, tool: str, result: dict, *, full_result: dict | None = None,
                  force: bool = False) -> dict:
    if tool not in COMPACT_TOOLS and not force:
        return result
    # Even a small failed transaction must expose its complete diagnostics via
    # the same hash-bound report reader as a compacted large result.
    force = force or bool(result.get('failure_summary'))
    reply = profile_summary(result) if tool == 'view_pixel_profile' else summarize_reply(result)
    large = _reply_bytes(reply) > 24000
    if reply == result and full_result is None and not large and not force:
        return result
    raw = (json.dumps(result if full_result is None else full_result, ensure_ascii=False, indent=2) + '\n').encode()
    sha = hashlib.sha256(raw).hexdigest()
    label = re.sub(r'[^A-Za-z0-9_-]', '_', tool)[:80] or 'tool'
    relative = f'tool_reports/{label}_{sha}.json'
    path = run / relative
    path.parent.mkdir(exist_ok=True)
    if not path.exists():
        path.write_bytes(raw)
    if large:
        from src.agent.runtime_roles.trial_feedback import bounded_json_view

        document = result if full_result is None else full_result
        for key, value in list(reply.items()):
            if key in document and _reply_bytes(value) > 3000:
                pointer = '/' + key.replace('~', '~0').replace('/', '~1')
                reply[key] = bounded_json_view(document, pointer=pointer,
                    reference_for=lambda at, item: report_reference(relative, sha, at, item))
    reply.update(details_file=relative, details_sha256=sha,
        details_read='read_candidate_items(candidate="", collection="report", report_file=details_file); offset/limit page by characters.')
    if _reply_bytes(reply) > 24000:
        from src.agent.runtime_roles.trial_feedback import bounded_json_view
        document = result if full_result is None else full_result
        bounded = {key: reply[key] for key in ('details_file', 'details_sha256', 'details_read')}
        bounded['report_view'] = bounded_json_view(document,
            reference_for=lambda at, item: report_reference(relative, sha, at, item))
        if _reply_bytes(bounded) > 22000:
            bounded['report_view'] = {'reference': report_reference(relative, sha, '', document)}
        # Verdicts, repair locations and saved identities remain immediately useful;
        # every other field is available in the bound report, including its keys.
        for key in ('status', 'source_geometry_ready', 'candidate', 'saved_candidate', 'save_effects',
                    'source_model_sha256', 'source_file_sha256',
                    'result_candidate', 'failure_summary', 'height_conflicts',
                    'reading_id', 'image', 'facade', 'artifact_sha256',
                    'matched_openings', 'all_readings_consistent', 'comparison_summary',
                    'saved_reviews', 'process_review',
                    'error', 'error_stage', 'error_type', 'tool_name', 'repair_hint', 'remaining_seconds',
                    'plan_input', 'plan_revision', 'drawing_differences', 'building_precision', 'counts'):
            if key in reply and _reply_bytes({**bounded, key: reply[key]}) <= 24000:
                bounded[key] = reply[key]
        reply = bounded
    return reply


def report_reference(report_file: str, sha256: str, pointer: str, value) -> dict:
    """Bind a JSON value to the exact immutable tool reply that owns it."""
    from src.agent.runtime_roles.trial_feedback import _bytes
    raw = _bytes(value)
    result = {'report_file': report_file, 'report_sha256': sha256, 'json_pointer': pointer,
              'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw),
              'read': {'tool': 'read_candidate_items', 'arguments': {
                  'candidate': '', 'collection': 'report', 'report_file': report_file,
                  'json_pointer': pointer}}}
    if isinstance(value, (list, dict)):
        result['item_count'] = len(value)
    return result


def read_report(run: Path, report_file: str, offset: int, limit: int, *, json_pointer: str | None = None) -> dict:
    """Read only an immutable tool reply; cannot reach arbitrary run/private files."""
    path = (run / report_file).resolve()
    folder = (run / 'tool_reports').resolve()
    if path.parent != folder or path.suffix != '.json' or not path.is_file():
        raise ValueError('report_file must be a saved tool_reports/*.json path from a tool reply')
    if offset < 0 or not 1 <= limit <= 12000:
        raise ValueError('report requires offset >= 0 and limit 1..12000 characters')
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if not path.stem.endswith('_' + sha):
        raise ValueError('saved tool report hash mismatch')
    if json_pointer is not None:
        from src.agent.runtime_roles.trial_feedback import bounded_json_view
        result = {'report_file': report_file, 'sha256': sha,
                  **bounded_json_view(json.loads(raw), pointer=json_pointer, offset=offset,
                      reference_for=lambda at, item: report_reference(report_file, sha, at, item))}
        if 'next_offset' in result:
            result['next'] = {'candidate': '', 'collection': 'report', 'report_file': report_file,
                              'json_pointer': json_pointer, 'offset': result['next_offset']}
        return result
    text = raw.decode()
    page = text[offset:offset + limit]
    return dict(report_file=report_file, sha256=sha, text=page, offset=offset,
                next_offset=offset+len(page) if offset+len(page)<len(text) else None,
                total_characters=len(text), page_is_partial=bool(offset or len(page)<len(text)))
