"""Mesh plan feedback is bounded by actual changed geometry, never floor count."""
import copy
import json
import pytest

from scripts.tool_scripts.bim_agent_view_selection import plan_signature, select_plan_feedback
from scripts.tool_scripts.run_bim_agent import Toolkit
from src.agent.geometry.parametric_proposal import expand_parametric_proposal
from tests.test_bim_agent_mesh import _prepare
from tests.test_parametric_proposal import plan


def _build(tmp_path, monkeypatch, count=3):
    _, run, _ = _prepare(tmp_path, monkeypatch)
    value=plan()
    value['instances']=[dict(id=f'F{i+1}',template='typical',z=3*i,height=3) for i in range(count)]
    built=Toolkit(run).build(expand_parametric_proposal(value))
    assert built['source_geometry_ready'], built
    source=json.loads((run/built['candidate']/'source_model.json').read_bytes())
    return run, built, source


def test_native_build_returns_one_actual_representative_and_any_floor_is_retrievable(tmp_path, monkeypatch):
    run, built, source=_build(tmp_path, monkeypatch, 21)
    assert len(built['source_plan_views'])==1
    selection=built['source_plan_selection']
    assert len(selection['changed_floor_ids'])==21
    assert len(selection['represented_by'])==20
    assert len(list((run/built['candidate']).glob('plan_F*.png')))==1
    original=(run/built['candidate']/'source_model.json').read_bytes()
    _, metadata=Toolkit(run).plan_view(built['candidate'],'F21')
    assert metadata['floor_id']=='F21' and (run/metadata['plan_image']).exists()
    assert (run/built['candidate']/'source_model.json').read_bytes()==original
    selected=select_plan_feedback(source, preferred_floor_ids=['F21'])
    assert selected['selected_floor_ids']==['F21']


def test_one_door_change_returns_only_its_floor_and_role_edits_need_no_plan(tmp_path, monkeypatch):
    run, built, source=_build(tmp_path, monkeypatch)
    toolkit=Toolkit(run)
    modified=toolkit.revise(built['candidate'],json.dumps([{
        'op':'update_opening','id':'F2:door:d','changes':{'p1':[3,0.5],'p2':[3,1.5]},
        'reason':'synthetic revised landing','source_refs':['fixture']}]))
    assert modified['source_geometry_ready'], modified
    assert [r['floor_id'] for r in modified['source_plan_views']]==['F2']
    assert modified['source_plan_selection']['changed_floor_ids']==['F2']
    role_only=copy.deepcopy(source)
    role_only['spaces'][0]['role']='meeting'
    role_only['spaces'][0]['role_evidence']={'basis':'inferred','source_refs':['fixture']}
    selection=select_plan_feedback(role_only,source)
    assert selection['selected_floor_ids']==[] and selection['unchanged_floor_count']==3


def test_changed_geometry_on_repeated_floors_groups_but_errors_never_disappear(tmp_path, monkeypatch):
    _, _, source=_build(tmp_path, monkeypatch)
    current=copy.deepcopy(source)
    for opening in current['openings']:
        if opening['kind']=='door':
            for point in opening['vertices']: point[1]+=0.1
    selected=select_plan_feedback(current,source)
    assert len(selected['selected_floor_ids'])==1 and len(selected['changed_floor_ids'])==3
    current['validation']['findings']=[{'code':'synthetic_error','space_id':'F2:office'}]
    selected=select_plan_feedback(current,source)
    assert 'F2' in selected['selected_floor_ids'] and 'F2' in selected['abnormal_floor_ids']
    current['validation']['findings']=[{'code':'unlocated_error'}]
    assert set(select_plan_feedback(current,source)['selected_floor_ids'])=={'F1','F2','F3'}


def test_external_precision_diagnostic_retains_an_unchanged_floor(tmp_path, monkeypatch):
    _, _, source=_build(tmp_path, monkeypatch)
    selected=select_plan_feedback(source,source,
        additional_findings=[{'type':'thin_coverage_gap','floor_id':'F2'}])
    assert selected['changed_floor_ids']==[]
    assert selected['selected_floor_ids']==['F2']
    assert selected['additional_finding_count']==1


def test_toolkit_retains_external_precision_floor_in_actual_feedback(tmp_path, monkeypatch):
    monkeypatch.setattr('scripts.tool_scripts.bim_agent_precision.building_precision',
        lambda *args: {'status':'rejected','items':[{'type':'thin_coverage_gap','floor_id':'F2'}]})
    _, built, _=_build(tmp_path, monkeypatch)
    assert set(built['source_plan_selection']['selected_floor_ids'])=={'F1','F2'}
    assert built['source_plan_selection']['abnormal_floor_ids']==['F2']


def test_optional_calibration_failure_keeps_bim_and_unlocated_diagnostics(tmp_path, monkeypatch):
    def invalid_calibrations(self):
        raise ValueError('synthetic corrupt calibration record')
    monkeypatch.setattr(Toolkit,'registered_calibrations',invalid_calibrations)
    _, built, _=_build(tmp_path,monkeypatch)
    assert built['source_geometry_ready']
    assert built['building_precision']['status']=='unavailable'
    assert built['projection_errors']
    assert set(built['source_plan_selection']['selected_floor_ids'])=={'F1','F2','F3'}


def test_diagnostic_identity_collision_does_not_hide_the_actual_floor():
    from src.agent.correction.schema import CorrectedGeometry
    from src.agent.geometry.source_bim import build_source_bim
    geometry=CorrectedGeometry.model_validate({'schema_version':'2',
        'footprint_x':[0,6],'footprint_y':[0,6], 'windows':[], 'openings':[],
        'floors':[{'name':'F1','z_floor':0,'ceiling_height':3,
                   'cells':[{'id':'F2','x':[0,6],'y':[0,6]}]},
                  {'name':'F2','z_floor':3,'ceiling_height':3,
                   'cells':[{'id':'room2','x':[0,6],'y':[0,6]}]}]})
    source=build_source_bim(geometry, capability_profile='orthogonal_polygon')
    assert source['validation']['status']=='pass'
    selected=select_plan_feedback(source,source,additional_findings=[{'floor_id':'F2'}])
    assert set(selected['selected_floor_ids'])=={'F1','F2'}


def test_enclosure_regions_group_by_relative_geometry_not_evidence(tmp_path, monkeypatch):
    from src.agent.geometry.source_enclosure import apply_source_enclosure
    _, _, source=_build(tmp_path, monkeypatch)
    declarations=[{'boundary_id':row['id'],'condition':'open','scope':'whole',
        'source_refs':['fixture'],'assumptions':[],'evidence_kind':'manual_annotation'}
        for row in source['boundaries'] if row['geometry_type']=='wall'
        and all(point[1]==5 for point in row['vertices'])]
    current=apply_source_enclosure(source,{'schema_version':'source_enclosure_input_v1',
        'base_source_model_sha256':source['source_model_sha256'],
        'spaces':[],'boundaries':declarations})
    assert len(select_plan_feedback(current)['selected_floor_ids'])==1
    before=copy.deepcopy(current)
    for row in current['boundaries']:
        for region in row.get('enclosure_regions',[]):
            region['source_refs']=['updated textual evidence']
    assert select_plan_feedback(current,before)['selected_floor_ids']==[]


def test_source_geometry_and_membership_changes_are_not_mistaken_for_labels(tmp_path, monkeypatch):
    _, _, source=_build(tmp_path, monkeypatch)
    current=copy.deepcopy(source)
    # A different edge order of the same point set must not compare equal.
    room=current['spaces'][0]
    room['polygon'][1],room['polygon'][2]=room['polygon'][2],room['polygon'][1]
    assert plan_signature(current,room['floor_id'])!=plan_signature(source,room['floor_id'])
    current=copy.deepcopy(source)
    current['floors'][0]['z_floor']+=0.1
    assert current['floors'][0]['id'] in select_plan_feedback(current,source)['changed_floor_ids']


def _small_source():
    from src.agent.correction.parse import ensure_corrected_geometry
    from src.agent.correction.schema import FootprintRing
    from src.agent.geometry.source_bim import build_source_bim
    value = plan()
    value['instances'] = [dict(id=f'F{i+1}', template='typical', z=3*i, height=3) for i in range(3)]
    geometry = ensure_corrected_geometry(expand_parametric_proposal(value)['geometry'])
    for floor in geometry.floors:
        floor.footprint = FootprintRing.model_validate(floor.footprint)
    return build_source_bim(geometry, capability_profile='orthogonal_polygon')


def test_equivalent_local_diagnostics_share_a_plan_but_keep_every_finding_and_location():
    source = _small_source()
    findings = [{'type': 'synthetic_width', 'space_id': f'F{i}:office', 'width': 0.4} for i in range(1, 4)]
    selected = select_plan_feedback(source, source, additional_findings=findings)
    assert selected['selected_floor_ids'] == ['F1']
    assert selected['abnormal_floor_ids'] == ['F1', 'F2', 'F3']
    assert selected['represented_by'] == {'F2': 'F1', 'F3': 'F1'}
    assert selected['findings'] == findings
    assert selected['diagnostic_groups'] == [{'representative_floor_id': 'F1',
        'floor_ids': ['F1', 'F2', 'F3'], 'finding_indices': [0, 1, 2],
        'located_ids': ['F1:office', 'F2:office', 'F3:office']}]


@pytest.mark.parametrize('difference', ['value', 'evidence_context', 'source_ref', 'cross_floor', 'global'])
def test_diagnostic_groups_do_not_hide_distinct_or_unbound_evidence(tmp_path, monkeypatch, difference):
    source = _small_source()
    findings = [{'type': 'synthetic_width', 'space_id': f'F{i}:office', 'width': 0.4} for i in range(1, 4)]
    contexts = {}
    if difference == 'value': findings[1]['width'] = 0.3
    elif difference == 'evidence_context': contexts['F2'] = {'image_sha256': 'different', 'calibration': [1, 2]}
    elif difference == 'source_ref': findings[1]['source_refs'] = ['different original image']
    elif difference == 'cross_floor': findings = [{'spaces': ['F1:office', 'F2:office', 'F3:office']}]
    else: findings = [{'error': 'unknown scope'}]
    selected = select_plan_feedback(source, source, additional_findings=findings,
                                    view_context_by_floor=contexts)
    assert set(selected['selected_floor_ids']) == ({'F1', 'F2', 'F3'}
        if difference in {'cross_floor', 'global'} else {'F1', 'F2'})
    assert selected['findings'] == findings


def _rehash_source(source):
    from src.agent.geometry.source_image_overlay import _digest
    source['source_model_sha256'] = _digest({k: v for k, v in source.items() if k != 'source_model_sha256'})
    return source


def test_plan_render_cache_reuses_unchanged_floor_with_current_source_metadata(tmp_path, monkeypatch):
    from scripts.tool_scripts.bim_agent_view_selection import render_source_plan_cached
    from src.agent.geometry import source_plan_view
    source = _small_source()
    original = source_plan_view.render_source_plan
    calls = []
    def counted(*args):
        calls.append(args[1])
        return original(*args)
    monkeypatch.setattr(source_plan_view, 'render_source_plan', counted)
    image, first = render_source_plan_cached(source, 'F1', tmp_path/'cache')
    current = copy.deepcopy(source)
    next(row for row in current['spaces'] if row['id']=='F2:office')['role'] = 'meeting'
    _rehash_source(current)
    reused, second = render_source_plan_cached(current, 'F1', tmp_path/'cache')
    assert calls == ['F1'] and second['render_cache']['hit']
    assert second['source_model_sha256'] == current['source_model_sha256'] != first['source_model_sha256']
    assert second['render_cache']['key'] == first['render_cache']['key']
    assert image.tobytes() == reused.tobytes()
    current['source_model_sha256'] = source['source_model_sha256']
    with pytest.raises(ValueError, match='does not match'):
        render_source_plan_cached(current, 'F1', tmp_path/'cache')


@pytest.mark.parametrize('changed', ['geometry', 'opening', 'role_label', 'renderer'])
def test_plan_render_cache_is_bound_to_content_labels_and_renderer(tmp_path, monkeypatch, changed):
    import scripts.tool_scripts.bim_agent_view_selection as selection
    source = _small_source()
    _, first = selection.render_source_plan_cached(source, 'F1', tmp_path/'cache')
    current = copy.deepcopy(source)
    if changed == 'geometry': current['spaces'][0]['polygon'][0][0] += 0.1
    elif changed == 'opening':
        for point in next(row for row in current['openings'] if row['id']=='F1:door:d')['vertices']:
            point[1] += 0.1
    elif changed == 'role_label': current['spaces'][0]['role'] = 'meeting'
    else:
        contract = selection._render_contract()
        monkeypatch.setattr(selection, '_render_contract', lambda: {**contract, 'new_renderer': True})
    _rehash_source(current)
    _, second = selection.render_source_plan_cached(current, 'F1', tmp_path/'cache')
    assert not second['render_cache']['hit']
    assert second['render_cache']['key'] != first['render_cache']['key']


@pytest.mark.parametrize('corrupt', ['png', 'metadata', 'record'])
def test_corrupted_plan_cache_is_rebuilt_instead_of_reused(tmp_path, monkeypatch, corrupt):
    from scripts.tool_scripts.bim_agent_view_selection import render_source_plan_cached
    source = _small_source()
    expected, first = render_source_plan_cached(source, 'F1', tmp_path/'cache')
    key = first['render_cache']['key']
    if corrupt == 'png': (tmp_path/'cache'/f'{key}.png').write_bytes(b'broken image')
    else:
        path = tmp_path/'cache'/f'{key}.json'
        if corrupt == 'record': path.write_bytes(b'{broken')
        else:
            value = json.loads(path.read_bytes())
            value['metadata']['space_ids'] = ['wrong identity']
            path.write_text(json.dumps(value), encoding='utf-8')
    rebuilt, second = render_source_plan_cached(source, 'F1', tmp_path/'cache')
    assert not second['render_cache']['hit'] and expected.tobytes() == rebuilt.tobytes()
    assert render_source_plan_cached(source, 'F1', tmp_path/'cache')[1]['render_cache']['hit']


def test_real_failed_draft_repair_reuses_verified_feedback_baseline_without_source_parent(tmp_path, monkeypatch):
    from scripts.tool_scripts.bim_agent_parametric import build_parametric, patch_parametric
    from tests.test_bim_agent_tools import _parametric_function_op
    _, run, _ = _prepare(tmp_path, monkeypatch)
    toolkit = Toolkit(run)
    first = build_parametric(toolkit, json.dumps(plan()))
    assert first['source_geometry_ready'], first
    failed = patch_parametric(toolkit, json.dumps([_parametric_function_op('remove', '/unresolved')]),
        candidate=first['candidate'], expected_sha=first['parametric']['plan_sha256'])
    assert failed['saved_candidate'] is None
    repaired = patch_parametric(toolkit, json.dumps([
        _parametric_function_op('add', '/unresolved', ['same geometry, revised uncertainty'])]),
        draft=failed['draft'], expected_sha=failed['plan_sha256'])
    assert repaired['source_geometry_ready'], repaired
    assert repaired['source_plan_selection']['selected_floor_ids'] == []
    assert repaired['source_plan_selection']['parent_source_model_sha256'] == first['source_model_sha256']
    source = json.loads((run/repaired['candidate']/'source_model.json').read_bytes())
    assert not source['generation']['provenance'].get('parent_candidate')
