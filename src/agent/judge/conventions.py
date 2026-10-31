"""Report documented representation differences without rewriting the reference.

This is a judge-only allowance ledger, not a normalization tool. Policies bind
an unchanged reference and independently recorded image inventories. No global
expanded tolerance, candidate fitting, or inference from an Agent's self-report.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from itertools import combinations
import json
from pathlib import Path

from shapely import wkt
from shapely.geometry import LineString, Polygon, box
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[3]
POLICIES = Path(__file__).with_name('convention_policies.json')
EPS = 1e-8


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_policy(case: str, reference_path: Path, manifest: dict) -> tuple[dict, list[dict]]:
    policy = json.loads(POLICIES.read_bytes()).get(case, {})
    if not policy:
        return {}, []
    if sha256(reference_path) != policy['reference_sha256']:
        raise ValueError('convention policy reference hash changed; independent review required')
    observations = []
    for entry in policy['plan_observations']:
        path = ROOT / entry['path']
        if sha256(path) != entry['sha256']:
            raise ValueError('independent plan observations changed; review required')
        value = json.loads(path.read_bytes())
        for obs in value.get('floors', [value]):
            from src.agent.judge.partition_evidence import floor_number
            number = floor_number(Path(obs['source_image']).stem)
            if number is None:
                raise ValueError('observation needs an explicit floor identity')
            image = Path(obs['source_image']).name
            if manifest.get('images', {}).get(image, {}).get('sha256') != obs['source_sha256']:
                # A missing image is missing evaluation scope, never authority
                # to apply a policy for a different building or input subset.
                return policy, []
            observations.append(dict(obs, floor_id=f'F{number}'))
    return policy, observations


def _near(first, second, tolerance):
    return (first.boundary.difference(second.boundary.buffer(tolerance + EPS)).length <= EPS
            and second.boundary.difference(first.boundary.buffer(tolerance + EPS)).length <= EPS)


def _line(rule, value):
    axis = rule['axis']
    low, high = rule['span_m']
    return LineString([(value, low), (value, high)] if axis == 0 else [(low, value), (high, value)])


def _validated_rules(policy, references):
    """Reject broader policies rather than accepting an arbitrary tolerance."""
    result = []
    for rule in policy.get('line_rules', []):
        category = rule['category']
        delta = abs(rule['target_m'] - rule['reference_m'])
        thickness = rule['wall_thickness_m']
        if rule['axis'] not in (0, 1) or not 0 < delta <= thickness or not 0 < thickness <= .24:
            raise ValueError('invalid documented line allowance')
        if category == 'single_line_half_wall':
            if abs(delta - thickness / 2) > EPS or rule['target_floor'] != rule['floor_id']:
                raise ValueError('half-wall allowance must be exactly half the documented thickness')
        elif category == 'cross_floor_alignment':
            if rule['target_floor'] == rule['floor_id']:
                raise ValueError('cross-floor alignment requires another reference floor')
        else:
            raise ValueError('unsupported convention category')
        target_boundaries = unary_union([Polygon(s['polygon'], s.get('holes', [])).boundary for s in references
                                        if s['floor_id'] == rule['target_floor']])
        target = _line(rule, rule['target_m'])
        # The target must be a pre-existing reference line, never an average or
        # a location chosen from this candidate. A collinear exterior segment
        # may extend into the interior for a single-line reference conversion.
        if category == 'cross_floor_alignment':
            supported = target.difference(target_boundaries.buffer(1e-6)).length <= 1e-6
        else:
            exterior = unary_union([Polygon(s['polygon'], s.get('holes', [])) for s in references
                                    if s['floor_id'] == rule['floor_id']]).boundary
            extended = _line({**rule, 'span_m': [-1e5, 1e5]}, rule['target_m'])
            supported = exterior.intersection(extended.buffer(1e-6)).length > .1
        if not supported:
            raise ValueError('convention target lacks independent reference-line support')
        result.append(rule)
    return result


def _variant(reference, rules):
    vertices = deepcopy(reference['polygon'])
    holes = deepcopy(reference.get('holes', []))
    used = []
    for rule in rules:
        changed = False
        axis, (lo, hi) = rule['axis'], rule['span_m']
        for point in [p for ring in [vertices, *holes] for p in ring]:
            if abs(point[axis] - rule['reference_m']) <= 1e-6 and lo-EPS <= point[1-axis] <= hi+EPS:
                point[axis] = rule['target_m']
                changed = True
        if changed:
            used.append(rule)
    return Polygon(vertices, holes), used


def classify_conventions(raw: dict, source: dict, policy: dict, *, inventory: dict, heights: dict) -> dict:
    """Return adjusted findings alongside every unchanged measured finding."""
    report = deepcopy(raw)
    refs = {s['id']: s for s in raw.get('reference_spaces', [])}
    candidates = {s['id']: s for s in raw.get('candidate_spaces', [])}
    if not refs or 'comparison' not in raw:
        return dict(status='not_evaluated', convention_differences=[], retained_findings=[], partition=report)
    rules = _validated_rules(policy, list(refs.values()))
    tolerance = raw['comparison']['tolerance_m']
    differences, retained, waived, accepted_bands = [], [], {}, {}
    structural_codes = {'source_space_split', 'source_spaces_merged', 'extra_source_space', 'missing_source_space',
        'floor_assignment_changed', 'candidate_spaces_overlap', 'invalid_candidate_space'}
    structural = [f for f in raw['comparison']['findings'] if f['code'] in structural_codes and f['severity'] == 'severe']
    for finding in report['comparison']['findings']:
        match = finding.get('match')
        if not match:
            retained.append(deepcopy(finding)); continue
        ref, candidate = refs[match['reference_id']], candidates[match['candidate_id']]
        blocked = any(ref['id'] in f.get('reference_ids', []) or candidate['id'] in f.get('candidate_ids', []) for f in structural)
        classes, evidence = [], {}
        if not blocked and finding['code'] == 'vertical_extent_changed':
            floor_policy = policy.get('ground_threshold')
            if floor_policy and ref['floor_id'] == floor_policy['floor_id']:
                allowance = float(floor_policy['threshold_m'])
                if not 0 < allowance <= .2:
                    raise ValueError('ground threshold exceeds documented 0.2 m ceiling')
                delta = candidate['z_floor'] - ref['z_floor']
                top_delta = abs(candidate['z_floor'] + candidate['height'] - ref['z_floor'] - ref['height'])
                # Require the indoor threshold itself, not any value in a wide
                # vertical band. Never waive moved roof/storey/opening heights.
                doors = {o['id']: o for o in source['openings'] if o['kind'] == 'door' and o['exterior']
                         and any(candidates.get(s, {}).get('floor_id') == ref['floor_id'] for s in o['space_ids'])}
                supporting = [r['opening_id'] for r in heights.get('comparisons', [])
                    if r['opening_id'] in doors and r['within_strict']
                    and abs(r['candidate_z_m'][0] - candidate['z_floor']) <= tolerance]
                if abs(delta - allowance) <= EPS and top_delta <= tolerance + EPS and supporting:
                    classes = ['ground_threshold']
                    evidence = dict(delta_m=delta, threshold_m=allowance, top_delta_m=top_delta,
                                    supporting_doors=supporting)
        elif not blocked and finding['code'] == 'partition_boundary_changed':
            applicable = [r for r in rules if r['floor_id'] == ref['floor_id']]
            for count in range(1, len(applicable)+1):
                for subset in combinations(applicable, count):
                    alternative, used = _variant(ref, subset)
                    if not used or not alternative.is_valid or alternative.area <= 0:
                        continue
                    if _near(alternative, Polygon(candidate['polygon'], candidate.get('holes', [])), tolerance):
                        # A changed host or connection in these rooms prevents
                        # dimensional leniency for their walls. Other defects
                        # remain separate severe findings in all cases.
                        invalid_hosts = [r for r in inventory.get('comparisons', []) if
                            candidate['id'] in r['actual_hosts'] + r['expected_hosts'] and
                            (not r['host_match'] or r['connection_match'] is False)]
                        if invalid_hosts or inventory.get('status') == 'not_evaluated':
                            continue
                        classes = sorted({r['category'] for r in used})
                        evidence = dict(rules=used, residual_boundary_m=alternative.boundary.hausdorff_distance(
                            Polygon(candidate['polygon'], candidate.get('holes', [])).boundary))
                        for rule in used:
                            line = _line(rule, rule['reference_m']).union(_line(rule, rule['target_m']))
                            band = box(*line.bounds).buffer(tolerance + EPS)
                            accepted_bands.setdefault(ref['floor_id'], []).append(band)
                        break
                if classes:
                    break
        if classes:
            original = deepcopy(finding)
            difference = dict(category=classes, severity='convention', reference_ids=[ref['id']],
                candidate_ids=[candidate['id']], original_finding=original, evidence=evidence)
            differences.append(difference)
            finding.update(severity='convention', convention_categories=classes, convention_evidence=evidence)
            waived.setdefault(candidate['id'], set()).add(finding['code'])
        else:
            retained.append(deepcopy(finding))
    # The aggregate interior-line diagnostic is demoted only when every
    # residual lies within the locally admitted bands. Keep unmatched spans.
    topology = []
    for finding in raw.get('topology_findings', []):
        if finding['code'] == 'vertical_extent_changed' and finding.get('candidate_ids') and all(
                finding['code'] in waived.get(s, set()) for s in finding['candidate_ids']):
            continue
        value = deepcopy(finding)
        if finding['code'] == 'internal_partition_boundary_changed' and accepted_bands.get(finding['floor_id']):
            band = unary_union(accepted_bands[finding['floor_id']])
            residual = {key: wkt.loads(finding[key + '_wkt']).difference(band).length for key in ('missing', 'extra')}
            value['unexplained_length_m'] = residual
            if max(residual.values()) <= 2*tolerance + EPS:
                value['severity'] = 'convention'
                differences.append(dict(category=['internal_boundaries_covered_by_documented_conventions'],
                    severity='convention', original_finding=deepcopy(finding), evidence=residual))
            else:
                retained.append(value)
        elif value not in retained:
            retained.append(value)
        topology.append(value)
    report['topology_findings'] = topology
    for match in report['comparison']['matches']:
        relevant = [f for f in report['comparison']['findings'] if match['candidate_id'] in f.get('candidate_ids', [])]
        match['status'] = _status(relevant)
    report['comparison']['status'] = _status(report['comparison']['findings'])
    report['status'] = _status(report['comparison']['findings'] + topology)
    findings = retained + inventory.get('findings', []) + heights.get('findings', [])
    status = _status(findings)
    if status != 'severe' and (inventory.get('status') == 'not_evaluated' or heights.get('status') == 'not_evaluated'):
        status = 'not_evaluated'
    return dict(status=status, convention_differences=differences, retained_findings=findings,
        partition=report, opening_inventory=inventory, exterior_heights=heights,
        policy=policy, reference_unchanged=True,
        limits=['Allowances change reporting only; reference and candidate bytes are unchanged.',
                'Opening position/height, missing/extra rooms or openings and connectivity are never demoted.',
                'This report is not a certificate of reading accuracy or complete building acceptance.'])


def _status(findings):
    severities = {f['severity'] for f in findings}
    return 'severe' if 'severe' in severities else 'minor' if 'minor' in severities else 'pass'


def reading_report(run: Path, *, reference_spaces=()) -> dict:
    """Preserve ALL recorded declarations, including failed/superseded drafts.

    Saved drafts are observable model readings, not necessarily correct readings.
    No inverse snap is fabricated when an old run lacks pre-normalization data.
    """
    records = []
    for path in sorted((run / 'plan_drafts').glob('draft_*/plan.json')):
        data = path.read_bytes()
        try:
            raw = json.loads(data)
        except (ValueError, UnicodeDecodeError) as error:
            records.append(dict(path=path.relative_to(run).as_posix(), sha256=sha256(path),
                status='unparseable_saved_reading', raw_text=data.decode('utf-8', errors='replace'),
                parse_error=str(error)))
            continue
        row = dict(path=path.relative_to(run).as_posix(), sha256=sha256(path), declaration=raw)
        for name in ('input.json', 'compilation.json', 'geometry_feedback.json'):
            saved = path.with_name(name)
            if saved.is_file():
                try:
                    value = json.loads(saved.read_bytes())
                    row[name.removesuffix('.json')] = dict(sha256=sha256(saved), value=value)
                except (ValueError, UnicodeDecodeError) as error:
                    row[name.removesuffix('.json')] = dict(sha256=sha256(saved),
                        status='unparseable_saved_reading', parse_error=str(error))
        compilation = row.get('compilation', {}).get('value')
        if isinstance(compilation, dict) and isinstance(raw, dict) and reference_spaces:
            from src.agent.judge.source_partition import compare_partitions
            from src.agent.judge.partition_evidence import floor_number
            number = floor_number(raw.get('floor_id')) or floor_number(Path(compilation.get('image_name', '')).stem)
            refs = [s for s in reference_spaces if floor_number(s['floor_id']) == number] if number else []
            if refs and all(key in raw for key in ('z_floor', 'ceiling_height')) and 'space_mapping' in compilation:
                spaces = [dict(id=m['space_id'], floor_id=refs[0]['floor_id'], polygon=m['world_polygon_m'],
                    z_floor=raw['z_floor'], height=raw['ceiling_height']) for m in compilation['space_mapping']]
                row['strict_reading_partition'] = compare_partitions(refs, spaces)
                row['score_basis'] = 'Saved pre-delivery compilation, without convention allowances; no recompile or inverse snapping.'
        records.append(row)
    for folder, pattern in [('claims', 'claim_*.json'), ('pixel_profiles', 'profile_*.json'),
                            ('dimension_chains', '*.json')]:
        for path in sorted((run / folder).glob(pattern)):
            try:
                records.append(dict(path=path.relative_to(run).as_posix(), sha256=sha256(path),
                                    declaration=json.loads(path.read_bytes())))
            except (ValueError, UnicodeDecodeError) as error:
                records.append(dict(path=path.relative_to(run).as_posix(), sha256=sha256(path),
                    status='unparseable_saved_reading', raw_text=path.read_text(errors='replace'), parse_error=str(error)))
    return dict(mode='recorded_readings_before_delivery_allowances', records=records,
        status='recorded' if records else 'not_available',
        normalization_history_status='not_recorded_separately',
        reading_accuracy='not_inferred_from_delivery_quality',
        limits=['All saved drafts/readings retained with original values and hashes; later successful revisions do not erase failures.',
                'Historical runs have no separate before/after normalization ledger; absence is not proof of no manual normalization.',
                'No ground-truth data or convention adjustment is written back to these readings.'])
