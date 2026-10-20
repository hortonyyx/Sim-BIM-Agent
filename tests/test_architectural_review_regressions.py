"""Independent review regressions for staged architectural diagnostics."""
from __future__ import annotations

import copy

import pytest
from shapely.geometry import Polygon

import src.agent.geometry.architectural_regularization as regularization
from src.agent.geometry.architectural_regularization import (
    ArchitecturalRegularizationError,
    DEFAULT_ARCHITECTURAL_POLICY,
    regularize_architectural_plan,
)


def test_duplicate_consecutive_vertices_report_all_rings_before_any_adoption(monkeypatch):
    raw = {
        'schema_version': 'source_polygon_v1',
        'templates': {},
        'instances': [],
        'assumptions': ['Synthetic independent rings with repeated observations'],
        'unresolved': [],
    }
    expected_paths = set()
    for name, offset in [('first', 0), ('second', 10)]:
        ring = [[offset + .024, .024], [offset + 4.024, .024],
                [offset + 4.024, .024], [offset + 4.024, 3.024],
                [offset + .024, 3.024]]
        # GEOS accepts these polygons, so the supporting-edge collection must
        # diagnose the repeated points without aborting other independent rings.
        assert Polygon(ring).is_valid and Polygon(ring).area > 0
        raw['templates'][name] = {
            'footprint': copy.deepcopy(ring),
            'spaces': [{'id': 'room', 'role': 'office', 'polygon': ring,
                        'source_refs': ['synthetic repeated boundary point']}],
        }
        raw['instances'].append({'id': name.upper(), 'template': name, 'z': 0, 'height': 3})
        expected_paths.update({f'templates.{name}.footprint',
                               f'templates.{name}.spaces[room].polygon'})
    # A downstream host failure must not replace the complete input-ring report.
    raw['templates']['first']['doors'] = [{
        'id': 'later', 'space': 'room', 'other_space': None,
        'p1': [500, 500], 'p2': [500, 501], 'z': [0, 2],
        'source_refs': ['deliberately invalid later-stage host'],
    }]
    before = copy.deepcopy(raw)

    def downstream_must_not_run(*args, **kwargs):
        pytest.fail('Invalid input rings must block all geometry adoption and downstream checks.')

    monkeypatch.setattr(regularization, '_snap', downstream_must_not_run)
    monkeypatch.setattr(regularization, '_vertical', downstream_must_not_run)
    monkeypatch.setattr(regularization, '_protect_volume_contacts', downstream_must_not_run)

    with pytest.raises(ArchitecturalRegularizationError) as error:
        regularize_architectural_plan(raw, policy=DEFAULT_ARCHITECTURAL_POLICY)

    audit = error.value.audit
    assert audit['status'] == 'rejected'
    assert len(audit['rejections']) == len(expected_paths)
    assert {item['code'] for item in audit['rejections']} == {'degenerate_edge'}
    assert {item['path'] for item in audit['rejections']} == expected_paths
    blocked = {item['path'] for item in audit['not_assessed']
               if item.get('blocked_by') == 'input_rings'}
    assert {'ring_adoption', 'apertures', 'vertical', 'contacts'} <= blocked
    assert audit['changes'] == []
    assert raw == before
