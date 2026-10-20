"""Native mesh asset admission and on-demand observations for the BIM agent."""
from __future__ import annotations

import hashlib
import io
import json
import math
from pathlib import Path
import shutil

from mcp.server.fastmcp import Image
from PIL import Image as PILImage


def freeze_mesh(source: Path, run: Path) -> dict:
    from src.agent.geometry.mesh_observation import MeshObservation

    if source.suffix.lower() != '.glb':
        raise ValueError('native mesh input currently requires a self-contained GLB')
    target = run / 'assets' / 'input.glb'
    target.parent.mkdir()
    shutil.copyfile(source, target)
    description = MeshObservation(target).describe()
    return {'source_path': str(source.resolve()), 'frozen_path': 'assets/input.glb',
            'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
            'bytes': target.stat().st_size, 'geometry': description,
            'note': 'Original mesh asset; no preselected cameras or BIM observations. '
                    'Mesh surfaces may be incomplete/noisy and are not room semantics.'}


def save_mesh_overlay(toolkit, candidate, observation, *, floor_id=None,
                      exterior_only=True, visibility='visible', boundary_ids=None):
    """Render one explicitly selected camera against the current saved source.

    Shared by the observation tool and save feedback; never reuses an old BIM
    overlay as evidence for a new source.
    """
    from src.agent.geometry.mesh_bim_frame import render_mesh_bim_overlay
    record = toolkit.manifest.get('mesh_input') or {}
    asset_path = toolkit.run / 'assets' / 'input.glb'
    if not record or hashlib.sha256(asset_path.read_bytes()).hexdigest() != record['sha256']:
        raise ValueError('input mesh changed or is unavailable')
    folder = toolkit.run / 'mesh_observations'
    if observation not in {p.stem for p in folder.glob('mesh_*.json')}:
        raise ValueError('unknown mesh observation; first call view_mesh')
    prefix = folder / observation
    metadata = json.loads(prefix.with_suffix('.json').read_bytes())
    source_path = toolkit.candidate_path(candidate) / 'source_model.json'
    source = json.loads(source_path.read_bytes())
    with PILImage.open(prefix.with_suffix('.png')) as original:
        picture, overlay = render_mesh_bim_overlay(original, metadata, source,
            floor_id=floor_id, exterior_only=exterior_only,
            visibility=visibility, boundary_ids=boundary_ids)
    folder = toolkit.run / 'mesh_overlays'
    folder.mkdir(exist_ok=True)
    indices = [int(p.stem[len('overlay_'):]) for p in folder.glob('overlay_*.json')
               if p.stem[len('overlay_'):].isdigit()]
    overlay_id = f'overlay_{max(indices, default=0) + 1:03d}'
    image_path = folder / f'{overlay_id}.png'
    picture.save(image_path)
    overlay.update(candidate=candidate, observation=observation, overlay_id=overlay_id,
        image_file=image_path.relative_to(toolkit.run).as_posix(),
        image_sha256=hashlib.sha256(image_path.read_bytes()).hexdigest(),
        observation_metadata_sha256=hashlib.sha256(prefix.with_suffix('.json').read_bytes()).hexdigest(),
        observation_image_sha256=hashlib.sha256(prefix.with_suffix('.png').read_bytes()).hexdigest(),
        source_file_sha256=hashlib.sha256(source_path.read_bytes()).hexdigest())
    (folder / f'{overlay_id}.json').write_text(json.dumps(overlay, indent=2) + '\n')
    from scripts.tool_scripts.bim_agent_replies import compact_reply
    summary = compact_reply(toolkit.run, 'overlay_mesh_candidate',
        {k: v for k, v in overlay.items() if k != 'objects'}, full_result=overlay, force=True)
    summary['complete_projection_file'] = summary['details_file']
    summary['complete_projection_sha256'] = summary['details_sha256']
    toolkit.log('overlay_mesh_candidate', summary)
    return summary


def register_mesh_tools(server, toolkit):
    """Expose only the admitted mesh; image-only/detail runs gain no asset access."""
    if not toolkit.manifest.get('mesh_input'):
        return
    asset = None

    def mesh():
        nonlocal asset
        from src.agent.geometry.mesh_observation import MeshObservation
        record = toolkit.manifest['mesh_input']
        path = toolkit.run / 'assets' / 'input.glb'
        if hashlib.sha256(path.read_bytes()).hexdigest() != record['sha256']:
            raise ValueError('input mesh changed')
        if asset is None:
            asset = MeshObservation(path)
        return asset

    def observation_path(observation):
        folder = toolkit.run / 'mesh_observations'
        allowed = {p.stem for p in folder.glob('mesh_*.json')}
        if observation not in allowed:
            raise ValueError('unknown mesh observation; first call view_mesh')
        return folder / observation

    def next_record_name(folder, prefix):
        # A child may receive a sparse subset of frozen observations. Counting
        # records could overwrite one of those admitted inputs on the next view.
        indices = [int(path.stem[len(prefix):]) for path in folder.glob(f'{prefix}*.json')
                   if path.stem[len(prefix):].isdigit()]
        return f'{prefix}{max(indices, default=0) + 1:03d}'

    @server.tool()
    def inspect_mesh(yaw_degrees: float = 0, bounds: list[list[float]] | None = None) -> dict:
        """Query original mesh bounds/counts in Z-up metres, optionally in a selected box.
        Local coordinates = rotate_xy(yaw_degrees) of [GLB.x,-GLB.z,GLB.y].
        No inferred walls, floors or footprint. Bounds selection can hide surfaces.
        """
        result = mesh().describe(yaw_degrees=yaw_degrees, bounds=bounds)
        toolkit.log('inspect_mesh', {'yaw_degrees': yaw_degrees, 'bounds': bounds, 'result': result})
        return {**result, 'remaining_seconds': toolkit.remaining_seconds()}

    @server.tool()
    def inspect_mesh_directions(yaw_degrees: float = 0, bounds: list[list[float]] | None = None,
                                face_ids: list[int] | None = None,
                                max_plane_tilt_degrees: float = 15) -> dict:
        """Query area-weighted directions of near-vertical ORIGINAL mesh triangles.
        Bounds select triangle centres in the yawed frame; face_ids can restrict
        to surfaces you measured. Directions are unsigned horizontal plane traces,
        CCW from local +X modulo 180, NOT the yaw to apply. Different parallel
        walls share a direction: these statistics do not fit or identify a wall.
        Use texture views and independent local selections to interpret peaks;
        roofs/slopes are excluded at the reported tilt threshold. No supplied axis.
        """
        result = mesh().surface_direction_evidence(yaw_degrees=yaw_degrees, bounds=bounds,
            face_ids=face_ids, max_plane_tilt_degrees=max_plane_tilt_degrees)
        folder = toolkit.run / 'mesh_evidence'; folder.mkdir(exist_ok=True)
        evidence_id = next_record_name(folder, 'directions_')
        record = {'evidence_id': evidence_id, 'request': dict(yaw_degrees=yaw_degrees,
            bounds=bounds, face_ids=face_ids, max_plane_tilt_degrees=max_plane_tilt_degrees),
            'result': result}
        (folder / f'{evidence_id}.json').write_text(json.dumps(record, indent=2) + '\n')
        toolkit.log('inspect_mesh_directions', record)
        candidates = result['candidates']
        summary = {**result, 'candidates': candidates[:10],
            'candidate_detail_omitted_count': max(0, len(candidates)-10),
            'candidate_detail_omitted_area_m2': sum(c['surface_area_m2'] for c in candidates[10:]),
            'all_direction_bins': [{'direction_degrees': c['area_weighted_direction_degrees'],
                                    'surface_area_m2': c['surface_area_m2']} for c in candidates],
            'complete_evidence_file': (folder/f'{evidence_id}.json').relative_to(toolkit.run).as_posix()}
        return {'evidence_id': evidence_id, **summary, 'remaining_seconds': toolkit.remaining_seconds()}

    @server.tool()
    def view_mesh(azimuth_degrees: float = 45, elevation_degrees: float = 25,
                  yaw_degrees: float = 0, target: list[float] | None = None,
                  width_m: float | None = None, height_m: float | None = None,
                  bounds: list[list[float]] | None = None,
                  candidate: str | None = None):
        """Render the admitted textured mesh from a camera YOU choose, with metric mapping.
        Z-up local frame as inspect_mesh; azimuth=0 looks FROM +x, 90 FROM +y,
        180 FROM -x, 270 FROM -y. elevation=90 is top. target is local [x,y,z].
        Omit target/spans for an automatic overview; give smaller metric spans
        and a local target for detail. Optional bounds selects triangles by their
        centres before rendering; excluded/occluded areas are not blank-wall proof.
        Returns an image with equal metres per pixel on both axes (longest side
        1200px) and an observation ID. Read resolution_px for measurement coordinates.
        Original OPAQUE/BLEND alpha is preserved. Pixels with a contributing
        fractional-alpha surface are visible but cannot supply a unique surface XYZ.
        With candidate, also return its current visible exterior BIM overlay in this
        same camera and call. This does not declare that you have reviewed it.
        """
        for value in (azimuth_degrees, elevation_degrees):
            if not math.isfinite(value):
                raise ValueError('camera angles must be finite')
        if not -90 <= elevation_degrees <= 90:
            raise ValueError('elevation must be between -90 and 90 degrees')
        info = mesh().describe(yaw_degrees=yaw_degrees, bounds=bounds)
        if info['bounds'] is None:
            raise ValueError('bounds selected no mesh faces; widen the selection or inspect the full asset')
        lo, hi = info['bounds']
        centre = [(a+b)/2 for a,b in zip(lo,hi)] if target is None else target
        if len(centre) != 3 or not all(math.isfinite(v) for v in centre):
            raise ValueError('target must be a finite 3D point')
        diameter = max(math.dist(lo, hi), 1)
        if width_m is None and height_m is None:
            height_m = diameter * 1.05
        if width_m is None:
            width_m = height_m * 4 / 3
        if height_m is None:
            height_m = width_m * 3 / 4
        if not all(math.isfinite(v) and 0 < v <= 1_000_000 for v in (width_m, height_m)):
            raise ValueError('view spans must be finite positive metres, at most 1000000')
        requested_width, requested_height = width_m, height_m
        metres_per_pixel = max(width_m, height_m) / 1200
        width_px = max(1, math.ceil(width_m / metres_per_pixel - 1e-9))
        height_px = max(1, math.ceil(height_m / metres_per_pixel - 1e-9))
        # Pad by less than one pixel where needed; never stretch apparent angles.
        width_m, height_m = width_px * metres_per_pixel, height_px * metres_per_pixel
        a,e = math.radians(azimuth_degrees), math.radians(elevation_degrees)
        direction = [math.cos(a)*math.cos(e), math.sin(a)*math.cos(e), math.sin(e)]
        distance = max(100, diameter*3)
        eye = [c+distance*d for c,d in zip(centre,direction)]
        folder = toolkit.run / 'mesh_observations'; folder.mkdir(exist_ok=True)
        prefix = folder / next_record_name(folder, 'mesh_')
        picture, metadata = mesh().render(prefix, eye=eye, target=centre,
            width_m=width_m, height_m=height_m, width_px=width_px, height_px=height_px,
            yaw_degrees=yaw_degrees, bounds=bounds)
        request = dict(azimuth_degrees=azimuth_degrees, elevation_degrees=elevation_degrees,
            yaw_degrees=yaw_degrees, target=target, width_m=requested_width, height_m=requested_height, bounds=bounds)
        result = {**metadata, 'observation': prefix.name,
                  'requested_view_span_m': {'width': requested_width, 'height': requested_height},
                  'camera_request': request, 'remaining_seconds': toolkit.remaining_seconds()}
        # The geometry module's frozen JSON/buffers remain the measurement source.
        toolkit.log('view_mesh', {'observation': prefix.name, 'request': request,
                                 'mesh_sha256': toolkit.manifest['mesh_input']['sha256']})
        data = io.BytesIO(); picture.save(data, 'PNG')
        images = [Image(data=data.getvalue(), format='png')]
        if candidate is not None:
            overlay = save_mesh_overlay(toolkit, candidate, prefix.name)
            result['candidate_overlay'] = overlay
            images.append(Image(data=(toolkit.run / overlay['image_file']).read_bytes(), format='png'))
        return [*images, json.dumps(result)]

    @server.tool()
    def measure_mesh_pixels(observation: str, pixels: list[list[int]], candidate: str | None = None) -> dict:
        """Read visible original-mesh surface XYZ at pixels of a saved mesh view.
        Integer pixels refer to that view's reported resolution_px, origin upper left.
        Background has no geometry. A transparent_composite is not background:
        contributing fractional alpha makes its XYZ/distance unavailable.
        Two valid surface points also return their distance;
        texture marks still require your interpretation, and noisy mesh is not BIM truth.
        With a candidate, also convert hits to its explicitly saved mesh_frame;
        no implicit alignment or translation is guessed.
        """
        result = mesh().pixel_query(observation_path(observation), pixels)
        if candidate is not None:
            from src.agent.geometry.mesh_bim_frame import observation_to_source, validate_mesh_frame
            from src.agent.geometry.source_model import _digest
            source_raw = (toolkit.candidate_path(candidate) / 'source_model.json').read_bytes()
            source = json.loads(source_raw)
            source_sha = _digest({k: v for k, v in source.items() if k != 'source_model_sha256'})
            if source.get('source_model_sha256') != source_sha:
                raise ValueError('source BIM digest mismatch')
            frame = validate_mesh_frame(source.get('mesh_frame'))
            if frame['mesh_sha256'] != result['mesh_sha256']:
                raise ValueError('candidate frame belongs to a different mesh')
            meta = json.loads(observation_path(observation).with_suffix('.json').read_text())
            yaw = meta['source_coordinate_transform']['yaw_degrees_counterclockwise_about_positive_z']
            for point in result['queries']:
                if point['hit']:
                    point['source_xyz'] = observation_to_source([point['world_xyz']], frame, yaw)[0].tolist()
            result['candidate'] = candidate
            result['mesh_frame'] = frame
            result['source_model_sha256'] = source_sha
            result['source_file_sha256'] = hashlib.sha256(source_raw).hexdigest()
        points = result['queries']
        if len(points) >= 2 and points[0]['hit'] and points[1]['hit']:
            delta = [b-a for a,b in zip(points[0]['world_xyz'], points[1]['world_xyz'])]
            result['first_two_plan_geometry'] = {
                'delta_xyz_m': delta,
                'horizontal_heading_degrees': math.degrees(math.atan2(delta[1], delta[0]))
                    if math.hypot(delta[0], delta[1]) > 1e-7 else None,
                'note': 'Heading of your two selected surface points, not an inferred building axis.'}
        toolkit.log('measure_mesh_pixels', {'observation': observation, 'pixels': pixels,
                                          'candidate': candidate, 'result': result})
        return result

    @server.tool()
    def view_mesh_observation(observation: str):
        """Reopen the exact saved original-mesh rendering and camera metadata."""
        mesh()  # Verify original-asset identity even when reopening a view.
        prefix = observation_path(observation)
        metadata = json.loads(prefix.with_suffix('.json').read_text())
        with PILImage.open(prefix.with_suffix('.png')) as picture:
            data = io.BytesIO(); picture.save(data, 'PNG')
        return [Image(data=data.getvalue(), format='png'), json.dumps(metadata)]

    @server.tool()
    def overlay_mesh_candidate(candidate: str, observation: str, floor_id: str | None = None,
                                exterior_only: bool = True, visibility: str = 'visible',
                                boundary_ids: list[str] | None = None):
        """Overlay actual source BIM on an existing original-mesh camera view.
        First save its explicit mesh_frame; no automatic fit or text-note parsing.
        Magenta source wall edges, green windows, orange doors. Default visible
        filters by actual host facing and saved source geometry depth; with
        exterior_only=False interior inspection is two-sided. Optional
        xray includes hidden edges. boundary_ids scopes arbitrary wall hosts,
        without a cardinal facade assumption. Full source still occludes scoped
        views. Glazing is an envelope; open enclosure regions and explicitly open
        passages/doors are excluded. Unknown or closed door states still occlude.
        Visibility does not compare source depth with the original mesh or certify
        alignment; all projected objects and their visibility remain in the record.
        Read complete_projection_file with read_candidate_items(collection='report',
        candidate='', report_file=complete_projection_file); json_pointer='/objects'
        browses projected IDs, coordinates and visibility without changing source.
        """
        summary = save_mesh_overlay(toolkit, candidate, observation, floor_id=floor_id,
            exterior_only=exterior_only, visibility=visibility, boundary_ids=boundary_ids)
        return [Image(data=(toolkit.run / summary['image_file']).read_bytes(), format='png'),
                json.dumps(summary)]

    from scripts.tool_scripts.bim_agent_mesh_queries import register_mesh_query_tools
    register_mesh_query_tools(server, toolkit, mesh, observation_path, next_record_name)

    # An overlay only adds observational sidecars to this run. Readonly workers
    # can inspect an admitted candidate snapshot, but cannot register or edit it.
    if toolkit.readonly:
        return

    @server.tool()
    def set_candidate_mesh_frame(candidate: str, yaw_degrees: float, translation_m: list[float],
                                 reason: str, source_refs: list[str]) -> dict:
        """Save a NEW candidate with an explicit mesh-to-BIM registration.
        source XYZ = rotate_xy(yaw_degrees)*[GLB.x,-GLB.z,GLB.y] + translation_m,
        after scene-node transforms. Rotation is CCW about +Z, metres, no scaling.
        Geometry stays numerically unchanged; its placement relative to the original
        asset changes. Choose yaw/translation from evidence and inspect an overlay.
        A frame edit does not fix shape, storeys, openings or internal hypotheses.
        Cite observations; update obsolete frame assumptions with revise_bim notes.
        """
        from src.agent.geometry.mesh_bim_frame import validate_mesh_frame
        asset_sha = mesh().mesh_sha256
        proposal = json.loads((toolkit.candidate_path(candidate) / 'proposal.json').read_text())
        previous = proposal.get('mesh_frame')
        frame = validate_mesh_frame(dict(mesh_sha256=asset_sha, yaw_degrees=yaw_degrees,
            translation_m=translation_m, reason=reason, source_refs=source_refs))
        proposal['mesh_frame'] = frame
        result = toolkit.build(proposal, action='set_candidate_mesh_frame', parent=candidate,
            operations=[{'operation': 'set_mesh_frame', 'before': previous, 'after': frame}])
        # Full build inventory is on disk; a registration edit can concern hundreds
        # of openings whose unchanged full geometry need not flood the tool response.
        return {key: result[key] for key in ('candidate', 'status', 'source_geometry_ready',
            'source_geometry_self_consistency', 'counts', 'mesh_frame', 'error', 'remaining_seconds')
            if key in result}
