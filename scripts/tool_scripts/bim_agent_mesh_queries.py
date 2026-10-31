"""Read-only native mesh cuts and same-view source correspondence tools."""
from __future__ import annotations

import io
import json

from mcp.server.fastmcp import Image

from src.agent.geometry.mesh_bim_frame import validate_mesh_frame
from src.agent.geometry.mesh_correspondence import compare_mesh_source, file_sha256
from src.agent.geometry.mesh_section import render_section, section_mesh
from src.agent.geometry.source_model import _digest


def _complete_small_section_preview(summary, segments, *, remaining_seconds):
    """Expand only small exact lists within the actual JSON text byte budget."""
    if len(segments) <= 8 or 'segment_preview' not in summary:
        return summary
    # Match the tool's json.dumps wire encoding, including escaped Unicode.
    if len(json.dumps(segments).encode('utf-8')) > 8000:
        return summary
    complete = {**summary, 'segment_preview': segments, 'omitted_segment_count': 0,
        'segment_index_note': 'Complete segments in original /segments order; zero-based indices are unchanged.'}
    if len(json.dumps({**complete, 'remaining_seconds': remaining_seconds}).encode('utf-8')) > 12000:
        return summary
    return complete


def register_mesh_query_tools(server, toolkit, mesh, observation_path, next_record_name):
    def source_snapshot(candidate):
        path = toolkit.candidate_path(candidate) / 'source_model.json'
        source = json.loads(path.read_bytes())
        digest = _digest({key: value for key, value in source.items() if key != 'source_model_sha256'})
        if source.get('source_model_sha256') != digest:
            raise ValueError('source BIM digest mismatch')
        frame = validate_mesh_frame(source.get('mesh_frame'))
        if frame['mesh_sha256'] != mesh().mesh_sha256:
            raise ValueError('candidate frame belongs to a different original mesh')
        return source, {'candidate': candidate, 'source_file_sha256': file_sha256(path),
                        'source_model_sha256': digest, 'mesh_frame': frame}

    @server.tool()
    def view_mesh_section(height_m: float, yaw_degrees: float = 0,
                          bounds_xy: list[list[float]] | None = None, candidate: str | None = None):
        """Cut the actual ORIGINAL mesh triangles at a chosen horizontal plane.
        Without candidate, height/bounds use original Z-up metres after yaw.
        With candidate, height/bounds use its saved source frame; yaw must be 0
        because the frame supplies rotation. Choose any floor or offset height.
        XY bounds clip intersection segments, unlike view_mesh centroid bounds.
        Returns a mapped section PNG, supporting face IDs and a complete hash-bound
        segment report. Small segment lists are returned completely in the preview;
        larger lists keep a bounded preview and an explicit omitted count.
        Coplanar faces and point touches are explicit diagnostics;
        no room/footprint inference, gap closing or automatic alignment is performed.
        """
        binding = {}
        translation = [0, 0, 0]
        if candidate is not None:
            if yaw_degrees != 0:
                raise ValueError('candidate section uses its saved mesh_frame; omit yaw_degrees')
            _, binding = source_snapshot(candidate)
            yaw_degrees = binding['mesh_frame']['yaw_degrees']
            translation = binding['mesh_frame']['translation_m']
        record = section_mesh(mesh(), height_m=height_m, yaw_degrees=yaw_degrees,
                              translation_m=translation, bounds_xy=bounds_xy)
        picture, mapping = render_section(record)
        folder = toolkit.run / 'mesh_sections'
        folder.mkdir(exist_ok=True)
        identity = next_record_name(folder, 'section_')
        image_path = folder / f'{identity}.png'
        picture.save(image_path)
        record.update(mapping, **binding, section_id=identity,
            coordinate_frame='source_BIM' if candidate is not None else 'original_mesh_Zup_yawed',
            image_file=image_path.relative_to(toolkit.run).as_posix(), image_sha256=file_sha256(image_path))
        record_path = folder / f'{identity}.json'
        record_path.write_bytes((json.dumps(record, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
        summary = {key: value for key, value in record.items() if key not in {'segments', 'coplanar_triangles', 'point_touches'}}
        summary['segment_preview'] = record['segments'][:8]
        summary['omitted_segment_count'] = max(0, len(record['segments'])-8)
        summary.update(evidence_file=record_path.relative_to(toolkit.run).as_posix(), evidence_sha256=file_sha256(record_path))
        from scripts.tool_scripts.bim_agent_replies import compact_reply
        summary = compact_reply(toolkit.run, 'view_mesh_section', summary, full_result=record, force=True)
        remaining_seconds = toolkit.remaining_seconds()
        summary = _complete_small_section_preview(summary, record['segments'],
                                                  remaining_seconds=remaining_seconds)
        toolkit.log('view_mesh_section', summary)
        data = io.BytesIO()
        picture.save(data, 'PNG')
        return [Image(data=data.getvalue(), format='png'), json.dumps({**summary, 'remaining_seconds': remaining_seconds})]

    @server.tool()
    def compare_mesh_source_pixels(candidate: str, observation: str, pixels: list[list[int]] | None = None,
                                   grid: list[int] | None = None, box: list[int] | None = None,
                                   boundary_ids: list[str] | None = None) -> dict:
        """Read original mesh versus source FIRST SURFACE hits on identical saved camera rays.
        Supply 1-256 [column,row] pixels, or grid=[columns,rows] (1-16 each) and
        optional original-pixel box=[left,top,right,bottom]; default grid is 8x8.
        Full source remains the occluder when boundary_ids scopes surfaces.
        Reports per-point hit/miss/edge/occlusion and signed source-minus-mesh depth,
        valid coverage and 5/10/30cm residual bands. Missing hits are not zero error.
        Glazing/closed or unknown doors use the host envelope; explicit open regions
        let rays through. No texture-window recognition, fit, room inference, or
        whole-building quality approval. Old observations lacking creation hashes
        must be re-rendered. Read details_file for all samples and precise hashes.
        """
        source, binding = source_snapshot(candidate)
        result = compare_mesh_source(mesh(), observation_path(observation), source,
            pixels=pixels, grid=grid, box=box, boundary_ids=boundary_ids)
        result.update(binding)
        folder = toolkit.run / 'mesh_evidence'
        folder.mkdir(exist_ok=True)
        identity = next_record_name(folder, 'correspondence_')
        record_path = folder / f'{identity}.json'
        result['evidence_id'] = identity
        record_path.write_bytes((json.dumps(result, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
        summary = {key: value for key, value in result.items() if key != 'samples'}
        summary['sample_preview'] = result['samples'][:8]
        summary['omitted_sample_count'] = max(0, len(result['samples'])-8)
        summary.update(evidence_file=record_path.relative_to(toolkit.run).as_posix(), evidence_sha256=file_sha256(record_path))
        from scripts.tool_scripts.bim_agent_replies import compact_reply
        summary = compact_reply(toolkit.run, 'compare_mesh_source_pixels', summary, full_result=result, force=True)
        toolkit.log('compare_mesh_source_pixels', summary)
        return {**summary, 'remaining_seconds': toolkit.remaining_seconds()}
