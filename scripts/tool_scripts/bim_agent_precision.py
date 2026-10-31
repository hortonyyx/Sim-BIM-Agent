"""Attach source precision diagnostics to existing saves and delivery replies."""
from functools import lru_cache
import json
from statistics import median

import numpy as np
from PIL import Image, ImageDraw

from src.agent.geometry.building_precision import precision_report
from src.agent.geometry.plan_drawing_differences import _axis, _ink, _strokes, _wall_like
from src.agent.geometry.wall_placement import annotation_tolerances, wall_placement_report


def annotated_wall_placement(toolkit, source, references, dimensions, *, positions=None):
    calibrations, errors = [], []
    for _, calibration in toolkit.registered_calibrations():
        try:
            toolkit.image_path(calibration['image'])
            if calibration['image_sha256'] != toolkit.manifest['images'][calibration['image']]['sha256']:
                raise ValueError('calibration original image changed')
            annotation_tolerances([calibration])
            calibrations.append(calibration)
        except (KeyError, ValueError, TypeError, ZeroDivisionError, OSError) as error:
            errors.append(dict(floor_id=calibration.get('floor_id'), reason=str(error)))
    result = wall_placement_report(source, references, dimensions, positions=positions,
        floor_tolerances=annotation_tolerances(calibrations))
    if errors:
        result['calibration_errors'] = errors
    return result


@lru_cache(maxsize=24)
def _drawing_width(path, sha256, x_anchors, y_anchors, footprint):
    # The caller verifies the admitted file hash. Cache includes both that hash
    # and the complete calibration/footprint, so scale changes cannot reuse it.
    _, mx = _axis(x_anchors)
    _, my = _axis(y_anchors)
    def pixel(value, anchors):
        (p0, w0), (p1, w1) = anchors
        return p0 + (value-w0) * (p1-p0) / (w1-w0)
    with Image.open(path) as image:
        ring = [(pixel(p[0], x_anchors), pixel(p[1], y_anchors)) for p in footprint]
        inside = Image.new('L', image.size, 0)
        ImageDraw.Draw(inside).polygon(ring, fill=1)
        mask = _ink(image) & np.asarray(inside, dtype=bool)
    widths = []
    for orientation, along, cross in [('v', my, mx), ('h', mx, my)]:
        widths.extend(width for _, lo, hi, width in _wall_like(_strokes(mask, orientation, 2/along), cross)
                      if .05 <= width <= .45 and (hi-lo)*along >= 2)
    # Image estimate only, not a measured construction attribute.
    return median(widths) if len(widths) >= 3 else None


def building_precision(toolkit, candidate, source=None):
    path = toolkit.candidate_path(candidate)
    source = source or json.loads((path/'source_model.json').read_bytes())
    proposal = json.loads((path/'proposal.json').read_bytes())
    floors = {f['id']: f for f in source['floors']}
    evidence, errors = {}, []
    try:
        calibrations = toolkit.registered_calibrations()
    except (OSError, ValueError, KeyError, TypeError) as error:
        # This evidence is optional after a successful save. Do not catch later
        # computation or artifact writes, or leave a partial precision report.
        return {'status': 'unavailable', 'source_model_sha256': source['source_model_sha256'],
                'reason': f'Could not load registered calibrations: {error}',
                'drawing_fidelity': 'not_evaluated'}
    for _, calibration in calibrations:
        fid = calibration['floor_id']
        if fid not in floors:
            continue
        try:
            anchors = [tuple(map(tuple, calibration[key])) for key in ('x_anchors', 'y_anchors')]
            mpp = [_axis(a)[1] for a in anchors]
            # Implausible plan scales must not grant a kilometres-wide tolerance.
            extent = floors[fid]['footprint']
            spans = [max(p[i] for p in extent)-min(p[i] for p in extent) for i in (0, 1)]
            if not all(2 <= value <= 300 for value in spans) or max(mpp) > .25:
                errors.append(dict(floor_id=fid, reason='scale outside plan precision-check range'))
                continue
            image_path = toolkit.image_path(calibration['image'])
            wall = _drawing_width(str(image_path), calibration['image_sha256'], *anchors, tuple(map(tuple, extent)))
            evidence[fid] = dict(floor_id=fid, metres_per_pixel=mpp, wall_thickness_m=wall,
                basis=dict(calibration_id=calibration['calibration_id'], image=calibration['image'],
                    image_sha256=calibration['image_sha256'],
                    thickness='median long double-line/band width estimate' if wall else 'unknown'))
        except (ValueError, KeyError, TypeError, OSError) as error:
            errors.append(dict(floor_id=fid, reason=str(error)))
    report = precision_report(source, floor_evidence=list(evidence.values()),
                              wall_references=proposal.get('wall_references', []))
    from scripts.tool_scripts.bim_agent_regularization import (
        apply_cross_storey_skip_policy, candidate_reports,
        classify_source_precision, collect_candidate_report, source_reports,
    )
    for regularization_report in candidate_reports(toolkit, candidate, source):
        report = apply_cross_storey_skip_policy(report, regularization_report)
    report = classify_source_precision(report, toolkit.manifest)
    report['wall_placement'] = annotated_wall_placement(toolkit, source,
        proposal.get('wall_references', []), proposal.get('wall_dimensions', []))
    audit_path = path / 'regularization_report.json'
    first_audit = not audit_path.is_file()
    regularization = collect_candidate_report(toolkit, candidate, source)
    if regularization:
        report['regularization'] = regularization
        # Later use/height edits keep an inherited audit in a sidecar. Refresh
        # just the view; source geometry, provenance and source hash stay exact.
        if first_audit and not source_reports(source):
            from src.agent.geometry.source_bim import source_view_geometry
            from src.agent.execution.source_proposal import build_source_viewer_html
            display = source_view_geometry(source)
            display['regularization_report'] = regularization
            saved = json.loads((path / 'report.json').read_text(encoding='utf-8'))
            viewer = build_source_viewer_html(display,
                source_geometry_ready=saved.get('source_geometry_ready', False),
                assumptions=source.get('assumptions', []),
                unresolved=source.get('generation', {}).get('unresolved', []),
                mesh_path=toolkit.run/'assets'/'input.glb' if source.get('mesh_frame') else None)
            (path / 'viewer.html').write_text(viewer, encoding='utf-8', newline='\n')
    if errors:
        report['evidence_errors'] = errors
    previous = source.get('generation', {}).get('provenance', {}).get('parent_candidate')
    previous_path = toolkit.run / str(previous) / 'precision_report.json'
    if previous and previous_path.is_file():
        before = json.loads(previous_path.read_bytes())
        key = lambda row: json.dumps(row, sort_keys=True)
        old, new = {key(r) for r in before['items']}, {key(r) for r in report['items']}
        report['changes'] = dict(status='compared', previous_candidate=previous,
            new=[i for i, r in enumerate(report['items']) if key(r) not in old],
            unchanged=[i for i, r in enumerate(report['items']) if key(r) in old],
            resolved=[r for r in before['items'] if key(r) not in new])
    else:
        report['changes'] = dict(status='no_previous_report', previous_candidate=previous)
    (path / 'precision_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    return report
