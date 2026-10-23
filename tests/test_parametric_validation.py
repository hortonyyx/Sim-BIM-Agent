"""Independent field errors remain visible beside geometry-stage failures."""
import copy
import json

import pytest

from src.agent.geometry.architectural_regularization import (
    ArchitecturalRegularizationError, regularize_architectural_plan,
)
from src.agent.geometry.parametric_proposal import expand_parametric_proposal
from src.agent.geometry.parametric_validation import parametric_field_diagnostics
from tests.test_architectural_line_paths import plan as path_plan
from tests.test_parametric_primitives import shared_plan as primitive_plan


def evidence(**changes):
    return {'role': 'office', 'basis': 'observed', 'source_refs': ['plan label'],
            'assumptions': [], **changes}


def plan():
    ring = [[0, 0], [4, 0], [4, 4], [0, 4]]
    return {'schema_version': 'source_polygon_v1', 'templates': {'main': {
        'footprint': {'vertices': ring},
        'spaces': [{'id': 'room', 'role': 'office', 'polygon': copy.deepcopy(ring),
                    'source_refs': ['plan'], 'role_evidence': evidence()}],
        'opening_rows': [{'id': 'south', 'space': 'room', 'kind': 'window',
            'p1': [0, 0], 'p2': [4, 0], 'spans': [[1, 2]], 'z': [1, 2],
            'source_refs': ['window observation']}]}},
        'instances': [{'id': 'F1', 'template': 'main', 'z': 0, 'height': 3}],
        'assumptions': [], 'unresolved': []}


def test_independent_errors_aggregate_despite_invalid_ring_without_mutating_input():
    value = plan()
    template = value['templates']['main']
    del template['spaces'][0]['role_evidence']['role']
    del template['spaces'][0]['role_evidence']['assumptions']
    del template['opening_rows'][0]['kind']
    template['footprint']['vertices'] = [[0, 0], [4, 4], [4, 0], [0, 4]]
    pristine = copy.deepcopy(value)
    diagnostics = parametric_field_diagnostics(value)
    assert {row['pointer'] for row in diagnostics} == {
        '/templates/main/spaces/0/role_evidence/role',
        '/templates/main/spaces/0/role_evidence/assumptions',
        '/templates/main/opening_rows/0/kind'}
    with pytest.raises(ArchitecturalRegularizationError) as caught:
        regularize_architectural_plan(value, policy={'schema_version': '1'})
    assert caught.value.audit['rejections']
    assert value == pristine
    assert diagnostics[-1]['allowed_values'] == ['window', 'door', 'open']


def test_exact_json_pointers_escape_names_and_collect_every_row():
    value = plan()
    template = value['templates'].pop('main')
    value['templates']['a/b~c'] = template
    value['instances'][0]['template'] = 'a/b~c'
    template['opening_rows'] *= 2
    template['opening_rows'][0]['kind'] = 'glazing'
    template['opening_rows'][0]['state'] = 'maybe'
    diagnostics = parametric_field_diagnostics(value)
    assert {row['pointer'] for row in diagnostics} == {
        f'/templates/a~1b~0c/opening_rows/{i}/{field}'
        for i in range(2) for field in ('kind', 'state')}
    assert all('allowed_values' in row for row in diagnostics)


def test_compiler_retains_the_same_required_field_and_role_evidence_contracts():
    value = plan()
    template = value['templates']['main']
    del template['opening_rows'][0]['kind']
    del template['spaces'][0]['role_evidence']['assumptions']
    assert len(parametric_field_diagnostics(value)) == 2
    with pytest.raises(ValueError):
        expand_parametric_proposal(value)
    template['spaces'][0]['role_evidence']['assumptions'] = []
    assert len(parametric_field_diagnostics(value)) == 1
    with pytest.raises(ValueError):
        expand_parametric_proposal(value)
    template['opening_rows'][0]['kind'] = 'window'
    assert parametric_field_diagnostics(value) == []
    assert expand_parametric_proposal(value)['geometry']['openings'][0]['kind'] == 'window'


@pytest.mark.parametrize('factory', [primitive_plan, path_plan])
def test_valid_primitives_baselines_and_boundary_paths_remain_unchanged(factory):
    value = factory()
    pristine = copy.deepcopy(value)
    before = expand_parametric_proposal(value)
    assert parametric_field_diagnostics(value) == []
    assert value == pristine
    assert expand_parametric_proposal(value) == before


def test_primitive_geometry_failure_does_not_hide_unrelated_fields():
    value = primitive_plan()
    template = value['templates']['main']
    template['spaces'][0]['primitive']['rects'] = [[0, 0, 0, 1]]
    template['spaces'][0]['role_evidence'] = {'basis': 'observed'}
    del template['opening_rows'][0]['kind']
    diagnostics = parametric_field_diagnostics(value)
    assert len(diagnostics) == 4
    assert any(row['pointer'].endswith('/opening_rows/0/kind') for row in diagnostics)
    with pytest.raises(ValueError):
        expand_parametric_proposal(value)


@pytest.mark.parametrize('version,allowed_window', [('2', False), ('source_polygon_v1', True)])
def test_field_values_follow_the_selected_formal_opening_schema(version, allowed_window):
    value = plan()
    value['schema_version'] = version
    template = value['templates']['main']
    template['opening_rows'] = []
    template['doors'] = [{'kind': 'window', 'state': None, 'source_refs': ['door observation']}]
    diagnostics = parametric_field_diagnostics(value)
    assert bool(diagnostics) is not allowed_window
    if diagnostics:
        assert diagnostics[0]['pointer'] == '/templates/main/doors/0/kind'
        assert diagnostics[0]['allowed_values'] == ['door', 'open']
    template['doors'] = [{'source_refs': ['door observation']}]  # Kind/state defaults remain optional.
    assert parametric_field_diagnostics(value) == []


def test_role_evidence_uses_formal_normalization_and_does_not_require_absent_evidence():
    value = plan()
    space = value['templates']['main']['spaces'][0]
    space['role_evidence']['role'] = 'Office'
    assert parametric_field_diagnostics(value) == []
    space['role_evidence']['role'] = 'corridor'
    diagnostics = parametric_field_diagnostics(value)
    assert diagnostics[0]['code'] == 'role_evidence_mismatch'
    assert diagnostics[0]['pointer'] == '/templates/main/spaces/0/role_evidence/role'
    space['role'] = 'invented_floor_use'
    assert parametric_field_diagnostics(value)[0]['pointer'] == '/templates/main/spaces/0/role'
    del space['role_evidence']
    assert parametric_field_diagnostics(value) == []


def test_invalid_evidence_basis_and_inferred_assumption_reuse_formal_validation():
    value = plan()
    space = value['templates']['main']['spaces'][0]
    space['role_evidence']['basis'] = 'guessed'
    diagnostics = parametric_field_diagnostics(value)
    assert diagnostics[0]['allowed_values'] == ['observed', 'inferred', 'unknown']
    space['role_evidence']['basis'] = 'inferred'
    assert parametric_field_diagnostics(value)[0]['pointer'] == '/templates/main/spaces/0/role_evidence'
    space['role_evidence']['assumptions'] = ['Inferred from the plan label.']
    assert parametric_field_diagnostics(value) == []


def test_only_used_templates_are_checked_and_malformed_containers_remain_parser_owned():
    value = plan()
    unused = copy.deepcopy(value['templates']['main'])
    unused['spaces'][0]['role_evidence'] = {}
    value['templates']['unused'] = unused
    assert parametric_field_diagnostics(value) == []
    for malformed in (None, [], {'templates': []}, {'templates': {'main': None}},
                      {'schema_version': '3', 'templates': value['templates']}):
        assert parametric_field_diagnostics(malformed) == []


def test_core_state_uses_its_stricter_existing_contract_without_changing_generic_defaults():
    value = plan()
    value['connections'] = [{'kind': 'door', 'state': None, 'source_refs': ['connection']}]
    value['core_connections'] = [{'kind': 'door', 'state': None, 'source_refs': ['landing']},
                                 {'kind': 'open', 'state': 'closed', 'source_refs': ['landing']},
                                 {'source_refs': ['landing']}]
    diagnostics = parametric_field_diagnostics(value)
    assert [row['pointer'] for row in diagnostics] == [
        '/core_connections/0/state', '/core_connections/1/state']
    assert None not in diagnostics[0]['allowed_values']
    assert diagnostics[1]['allowed_values'] == ['open']


def test_fully_replaced_repeat_does_not_make_unused_template_newly_invalid():
    value = plan()
    unused = copy.deepcopy(value['templates']['main'])
    unused['spaces'][0]['role_evidence'] = {}
    value['templates']['unused'] = unused
    value['repeats'] = [{'id': 'replaced', 'template': 'unused', 'id_prefix': 'F',
        'start': 1, 'count': 1, 'z': 0, 'height': 3,
        'source_refs': ['explicit replacement'], 'exclude_ids': ['F1']}]
    pristine = copy.deepcopy(value)
    assert [floor['name'] for floor in expand_parametric_proposal(value)['geometry']['floors']] == ['F1']
    assert parametric_field_diagnostics(value) == []
    assert value == pristine
    value['repeats'][0]['count'] = 2
    diagnostics = parametric_field_diagnostics(value)
    assert {row['pointer'] for row in diagnostics} == {
        f'/templates/unused/spaces/0/role_evidence/{field}'
        for field in ('role', 'basis', 'source_refs', 'assumptions')}


def connection(**changes):
    return {'id': 'global', 'kind': 'door', 'space_id': 'F1:room', 'other_space_id': None,
            'p1': [0, 1], 'p2': [0, 2], 'z': [0, 2],
            'source_refs': ['observed global opening'], **changes}


def test_source_refs_aggregate_at_original_declarations_not_expanded_indices():
    value = plan()
    template = value['templates'].pop('main')
    value['templates']['a/b~c'] = template
    value['instances'][0]['template'] = 'a/b~c'
    value['instances'][0]['source_refs'] = ['instance evidence does not replace row evidence']
    del template['spaces'][0]['source_refs']
    del template['opening_rows'][0]['source_refs']
    del template['opening_rows'][0]['kind']
    template['doors'] = [{}]
    template['window_rows'] = [{'spans': [[1, 2]]}]
    value['connections'] = [connection(), connection()]
    del value['connections'][0]['source_refs']
    del value['connections'][1]['source_refs']
    value['core_connections'] = [{}]
    pristine = copy.deepcopy(value)
    diagnostics = parametric_field_diagnostics(value)
    assert {row['pointer'] for row in diagnostics} == {
        '/templates/a~1b~0c/spaces/0/source_refs',
        '/templates/a~1b~0c/opening_rows/0/source_refs',
        '/templates/a~1b~0c/opening_rows/0/kind',
        '/templates/a~1b~0c/doors/0/source_refs',
        '/templates/a~1b~0c/window_rows/0/source_refs',
        '/connections/0/source_refs', '/connections/1/source_refs',
        '/core_connections/0/source_refs'}
    assert all(row['code'] == 'missing_field' for row in diagnostics)
    assert all(row['required_fields'] == ['source_refs'] for row in diagnostics
               if row['pointer'].endswith('source_refs'))
    assert value == pristine and 'input' not in json.dumps(diagnostics)


@pytest.mark.parametrize('version', ['2', 'source_polygon_v1'])
@pytest.mark.parametrize('refs', [None, [], [''], ['  '], [123], 'observation',
                                 ['observation'], ('observation',), [b'observation']])
def test_direct_source_ref_values_follow_formal_opening_schema(version, refs):
    from pydantic import ValidationError
    from src.agent.correction.schema import WallOpening
    from src.agent.geometry.source_schema import SourceWallOpening
    value = plan()
    value['schema_version'] = version
    value['templates']['main']['opening_rows'] = []
    row = connection(source_refs=refs)
    value['connections'] = [row]
    model = SourceWallOpening if version == 'source_polygon_v1' else WallOpening
    try:
        model.model_validate(row)
        accepted = True
    except ValidationError:
        accepted = False
    diagnostics = parametric_field_diagnostics(value)
    assert bool(diagnostics) is not accepted
    assert all(item['pointer'].startswith('/connections/0/source_refs') for item in diagnostics)


@pytest.mark.parametrize('refs', [None, [], [''], ['  '], [123], 'observation',
                                 ('observation',), [b'observation']])
def test_compact_row_refs_reuse_strict_compiler_contract_even_with_instance_refs(refs):
    value = plan()
    value['instances'][0]['source_refs'] = ['instance observation']
    value['templates']['main']['opening_rows'][0]['source_refs'] = refs
    diagnostics = parametric_field_diagnostics(value)
    assert [row['pointer'] for row in diagnostics] == ['/templates/main/opening_rows/0/source_refs']
    with pytest.raises((ValueError, TypeError)):
        expand_parametric_proposal(value)


def test_repeat_and_continuous_space_refs_append_without_requiring_instance_override():
    from tests.test_parametric_continuity import continuity, storeys
    value = storeys()
    value['continuous_spaces'] = [continuity()]
    value['core_connections'] = [{'id': 'extra', 'core_space_id': 'STAIR:stair',
        'space': 'room', 'repeat_ids': ['storeys'], 'p1': [2, 2], 'p2': [2, 3],
        'z': [0, 2], 'source_refs': ['declared landing']}]
    value['templates']['typical']['doors'] = []
    pristine = copy.deepcopy(value)
    assert parametric_field_diagnostics(value) == []
    proposal = expand_parametric_proposal(value)
    assert len(proposal['geometry']['openings']) == 3
    assert all(row['source_refs'] == ['declared landing', 'explicit three storeys']
               for row in proposal['geometry']['openings'])
    assert value == pristine
    del value['core_connections'][0]['source_refs']
    assert [row['pointer'] for row in parametric_field_diagnostics(value)] == [
        '/core_connections/0/source_refs']
    with pytest.raises(ValueError):
        expand_parametric_proposal(value)


def test_unused_and_fully_excluded_template_source_refs_are_not_diagnosed():
    value = plan()
    unused = copy.deepcopy(value['templates']['main'])
    for row in unused['spaces'] + unused['opening_rows']:
        del row['source_refs']
    value['templates']['unused'] = unused
    value['repeats'] = [{'id': 'replaced', 'template': 'unused', 'id_prefix': 'F',
        'start': 1, 'count': 1, 'z': 0, 'height': 3,
        'source_refs': ['explicit replacement'], 'exclude_ids': ['F1']}]
    assert parametric_field_diagnostics(value) == []
    assert len(expand_parametric_proposal(value)['geometry']['floors']) == 1
    value['repeats'][0]['count'] = 2
    assert {row['pointer'] for row in parametric_field_diagnostics(value)} == {
        '/templates/unused/spaces/0/source_refs', '/templates/unused/opening_rows/0/source_refs'}


def test_empty_legacy_window_row_keeps_existing_unevaluated_ref_value_semantics():
    value = plan()
    value['templates']['main']['window_rows'] = [{'id': 'empty', 'facade': 'South',
        'plane': 0, 'spans': [], 'z': [1, 2], 'source_refs': None}]
    assert parametric_field_diagnostics(value) == []
    assert expand_parametric_proposal(value)['geometry']['windows'] == []
    del value['templates']['main']['window_rows'][0]['source_refs']
    assert [row['pointer'] for row in parametric_field_diagnostics(value)] == [
        '/templates/main/window_rows/0/source_refs']
    with pytest.raises(ValueError):
        expand_parametric_proposal(value)


def test_source_ref_feedback_survives_formal_build_failure_and_preserves_draft(tmp_path):
    from scripts.tool_scripts.bim_agent_parametric import build_parametric
    from src.agent.correction.parse import ensure_corrected_geometry
    from tests.test_bim_agent_tools import _ParametricFunctionToolkit

    class FormalSourceToolkit(_ParametricFunctionToolkit):
        def build(self, proposal, **kwargs):
            ensure_corrected_geometry(proposal['geometry'])
            return super().build(proposal, **kwargs)

    toolkit = FormalSourceToolkit(tmp_path / 'run')
    value = plan()
    value['connections'] = [connection(), connection(id='second')]
    for row in value['connections']:
        del row['source_refs']
    result = build_parametric(toolkit, json.dumps(value))
    assert result['error_stage'] == 'parametric_build'
    assert 'openings.1.source_refs' in result['error']
    assert {row['pointer'] for row in result['error_details']['field_diagnostics']} == {
        '/connections/0/source_refs', '/connections/1/source_refs'}
    assert list(toolkit.run.glob('candidate_*')) == []
    assert json.loads((toolkit.run / result['draft']).read_bytes()) == value
    assert toolkit.calls == []
