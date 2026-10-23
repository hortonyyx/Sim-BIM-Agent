"""Small cuts retain exact evidence without requiring fixed eight-row pages."""
import asyncio
import copy
import hashlib
import json

import pytest

from scripts.tool_scripts.bim_agent_mesh_queries import _complete_small_section_preview


def segments(count):
    return [{'endpoints_xyz': [[i + 0.123456789, 0, 4], [i + 0.123456789, 1, 4]],
             'face_ids': [i, i + 10000], 'length_m': 1.0} for i in range(count)]


def summary(rows, **extra):
    return {'segment_preview': rows[:8], 'omitted_segment_count': max(0, len(rows)-8),
            'details_file': 'tool_reports/view_mesh_section_' + 'a'*64 + '.json',
            'details_sha256': 'a'*64, 'coordinate_frame': 'source_BIM', **extra}


@pytest.mark.parametrize('count', [0, 1, 8, 25, 46])
def test_small_preview_preserves_complete_rows_order_and_input(count):
    rows = segments(count)
    small = summary(rows)
    before = copy.deepcopy((small, rows))
    result = _complete_small_section_preview(small, rows, remaining_seconds=None)
    assert result['segment_preview'] == rows
    assert result['omitted_segment_count'] == 0
    assert (small, rows) == before
    assert result['details_file'] == small['details_file']
    assert result['details_sha256'] == small['details_sha256']
    assert len(json.dumps({**result, 'remaining_seconds': None}).encode()) <= 12000


@pytest.mark.parametrize('count', [112, 116])
def test_large_cut_retains_existing_preview_and_full_report(count):
    rows = segments(count)
    small = summary(rows)
    assert _complete_small_section_preview(small, rows, remaining_seconds=30) is small
    assert small['omitted_segment_count'] == count-8


def test_large_supporting_face_list_is_never_silently_trimmed():
    rows = segments(12)
    rows[-1]['face_ids'] = list(range(5000))
    small = summary(rows)
    assert _complete_small_section_preview(small, rows, remaining_seconds=30) is small
    assert len(rows[-1]['face_ids']) == 5000


def test_wire_budget_counts_escaped_unicode_and_remaining_field():
    rows = segments(25)
    small = summary(rows, source_reason='测'*1700)
    assert len(json.dumps({**small, 'segment_preview': rows}, ensure_ascii=False).encode()) < 12000
    assert _complete_small_section_preview(small, rows, remaining_seconds=123.456789) is small
    small = summary(rows, source_reason='x'*7000)
    assert _complete_small_section_preview(small, rows, remaining_seconds='x'*3000) is small


def test_prior_compaction_without_preview_is_not_undone():
    rows = segments(25)
    small = {'details_file': 'tool_reports/large.json', 'report_view': {'reference': 'full'}}
    assert _complete_small_section_preview(small, rows, remaining_seconds=0) is small


def test_actual_mcp_section_has_exact_complete_preview_and_readable_bound_report(tmp_path):
    import numpy as np
    import trimesh
    from PIL import Image
    from src.agent.bim_inputs import prepare_bim_inputs
    from tests.test_bim_agent_tools import _server_session, _json_result

    scene = trimesh.Scene()
    for offset in (0, 6, 12, 18):
        box = trimesh.creation.box(extents=[4, 6, 8])
        box.visual = trimesh.visual.TextureVisuals(uv=np.zeros((len(box.vertices), 2)),
            image=Image.new('RGB', (4, 4), (100, 150, 200)))
        box.apply_translation([offset, 0, 0])
        scene.add_geometry(box)
    asset = tmp_path/'input.glb'
    asset.write_bytes(scene.export(file_type='glb'))
    run = tmp_path/'bim'
    prepare_bim_inputs(run, images_path=None, mesh_path=asset, building_input_path=None,
        scope='Synthetic section presentation only', image_kind='unknown', max_candidates=1, floor_images=[])
    original_hash = hashlib.sha256(asset.read_bytes()).hexdigest()

    async def exercise():
        async with _server_session(run, readonly=True) as session:
            reply = await session.call_tool('view_mesh_section', {'height_m': 0.3, 'yaw_degrees': 13})
            assert not reply.isError, reply
            text = next(block.text for block in reply.content if block.type == 'text')
            value = json.loads(text)
            record = json.loads((run/value['evidence_file']).read_bytes())
            assert len(record['segments']) > 8
            assert value['segment_preview'] == record['segments']
            assert value['omitted_segment_count'] == 0
            assert value['coordinate_frame'] == record['coordinate_frame'] == 'original_mesh_Zup_yawed'
            assert len(text.encode()) <= 12000
            assert hashlib.sha256((run/value['evidence_file']).read_bytes()).hexdigest() == value['evidence_sha256']
            assert hashlib.sha256((run/value['details_file']).read_bytes()).hexdigest() == value['details_sha256']
            assert json.loads((run/value['details_file']).read_bytes()) == record
            png = next(block for block in reply.content if block.type == 'image')
            import base64
            assert hashlib.sha256(base64.b64decode(png.data)).hexdigest() == record['image_sha256']
            read = _json_result(await session.call_tool('read_candidate_items', {
                'candidate':'', 'collection':'report', 'report_file':value['details_file'],
                'json_pointer':'/segments/12'}))
            assert read['value'] == record['segments'][12]
            assert hashlib.sha256(asset.read_bytes()).hexdigest() == original_hash

    asyncio.run(exercise())
