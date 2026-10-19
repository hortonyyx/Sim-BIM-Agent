"""Offline native entry exercises the mesh framework with actual local MCP tools."""
import asyncio
import hashlib
import json
from pathlib import Path

from src.agent import runtime_entry
from src.agent_runtime.store import EventStore
from tests.test_mesh_input_admission import _case
from tests.test_mesh_worker_entry import _response
from tests.test_parametric_proposal import curved_plan


def test_native_curved_templates_sections_correspondence_and_worker_handoff(tmp_path):
    asyncio.run(_exercise(tmp_path))


async def _exercise(tmp_path):
    case = _case(tmp_path)
    inputs = Path(case['input_root'])
    mesh = inputs / 'building.glb'
    # Exercise the actual MCP image/evidence path with partial transparency.
    import numpy as np
    from PIL import Image
    import trimesh
    scene = trimesh.load(mesh, force='scene')
    shell = trimesh.creation.box(extents=[1.3, 1.3, 1.3])
    shell.visual = trimesh.visual.TextureVisuals(uv=np.zeros((len(shell.vertices), 2)),
        material=trimesh.visual.material.PBRMaterial(
            baseColorTexture=Image.new('RGBA', (4, 4), (255, 0, 0, 128)), alphaMode='BLEND'))
    scene.add_geometry(shell, node_name='transparent_shell')
    mesh.write_bytes(scene.export(file_type='glb'))
    plan = curved_plan()
    plan['mesh_frame'] = {'mesh_sha256': hashlib.sha256(mesh.read_bytes()).hexdigest(),
        'yaw_degrees': 0, 'translation_m': [0, 0, 0],
        'reason': 'Synthetic protocol check; mismatch with the box is intentional.',
        'source_refs': ['synthetic']}
    tasks = [{'task_id': 'compare', 'candidate': 'candidate_01', 'observation_ids': ['mesh_001'],
              'question': 'Inspect one original section and compare the supplied geometry.',
              'budget': {'model_calls': 3, 'tool_calls': 2}}]
    parent = [
        _response('p1', 'build_parametric_bim', {'plan_json': json.dumps(plan)}),
        _response('p2', 'view_mesh'),
        _response('p3', 'delegate_mesh_workers', {'tasks': tasks}),
        _response('p4', 'read_mesh_worker', {'task_id': 'compare', 'view_ids': ['section_001']}),
        _response('p5', 'overlay_mesh_candidate', {'candidate': 'candidate_01', 'observation': 'mesh_001'}),
        _response('p6', 'finish_bim', {'candidate': 'candidate_01', 'reviews_json': json.dumps({
            'mesh_review': {'coverage': 'whole_building', 'summary': 'Synthetic plumbing only; no fidelity verdict.',
                            'overlay_ids': ['overlay_001']}})}),
    ]
    worker_answer = {'directly_seen': [
        {'observation_id': 'section', 'statement': 'An original-mesh section was generated.',
         'evidence_kind': 'mesh_section', 'evidence_id': 'section_001', 'measurement_call_id': 'w1'},
        {'observation_id': 'depth', 'statement': 'The geometric correspondence is a scoped computation.',
         'evidence_kind': 'mesh_observation', 'evidence_id': 'mesh_001', 'measurement_call_id': 'w2'}],
        'uncertain': [{'uncertainty_id': 'quality', 'statement': 'No texture opening or interior quality is assessed.'}]}
    workers = {'compare': [
        _response('w1', 'view_mesh_section', {'height_m': 0.1, 'candidate': 'candidate_01'}),
        _response('w2', 'compare_mesh_source_pixels', {'candidate': 'candidate_01', 'observation': 'mesh_001',
                                                     'grid': [8, 8]}),
        _response('w3', answer=json.dumps(worker_answer)),
    ]}
    parent_script, worker_script = tmp_path / 'parent.json', tmp_path / 'workers.json'
    parent_script.write_text(json.dumps(parent), encoding='utf-8')
    worker_script.write_text(json.dumps(workers), encoding='utf-8')
    out = tmp_path / 'framework'
    args = runtime_entry.parser().parse_args([
        '--out', str(out), '--run-root', str(tmp_path), '--mesh', str(mesh),
        '--images', str(inputs / 'sketches'), '--building-input', str(inputs / 'declaration.json'),
        '--provider', 'scripted', '--script', str(parent_script), '--mesh-workers', '2',
        '--mesh-worker-script', str(worker_script), '--model-calls', '12', '--tool-calls', '20',
        '--seconds', '300', '--no-token-limit', '--image-history-batches', '1'])
    receipt = await runtime_entry.execute(args)
    assert receipt['status'] == 'completed', receipt
    assert receipt['finalization']['completion_valid'] is True
    assert receipt['finalization']['delivery']['complete_building'] is True
    assert receipt['completion']['mesh_review']['fidelity'] == 'not_evaluated'
    assert receipt['whole_run_counts']['model_requests'] == 9
    assert receipt['whole_run_counts']['tool_calls'] == 8
    assert receipt['root_usage_accounting']['requests'] == 9
    assert receipt['mesh_workers']['tasks'][0]['status'] == 'completed'
    source_files = list(out.rglob('candidate_01/source_model.json'))
    assert source_files
    source = json.loads(source_files[0].read_text(encoding='utf-8'))
    assert len(source['spaces']) == 2
    assert source['curve_discretization']
    events = EventStore.read_events(out / 'events.jsonl')
    assert all(event.payload.outcome == 'succeeded' for event in events
               if event.payload.event_type == 'tool_execution')
    section_files = list(out.rglob('section_001.json'))
    comparisons = list(out.rglob('correspondence_001.json'))
    assert section_files and comparisons
    comparison = json.loads(comparisons[0].read_bytes())
    assert comparison['counts'].get('mesh_transparent_composite', 0) > 0
    assert all(row['valid_for_residual_summary'] is False
               and 'signed_depth_residual_m' not in row
               and 'absolute_depth_residual_m' not in row for row in comparison['samples']
               if row['status'] == 'mesh_transparent_composite')
    before = (out / 'events.jsonl').read_bytes()
    args.resume = True
    resumed = await runtime_entry.execute(args)
    assert resumed['whole_run_counts'] == receipt['whole_run_counts']
    assert (out / 'events.jsonl').read_bytes() == before
