"""Deterministic observations of a textured binary glTF building mesh.

The source glTF convention is Y-up.  Public coordinates from this module are
BIM-local Z-up coordinates, using ``(x, y, z) -> (x, -z, y)`` after each scene
node transform.  An operation may additionally rotate that frame counter-
clockwise around +Z with ``yaw_degrees``.

This module reports and renders mesh evidence.  A mesh surface, its horizontal
extent, or a face-centroid selection is not evidence of rooms, enclosure, or a
true building footprint.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import struct
from typing import Any

import numpy as np
from PIL import Image
import trimesh


_MAX_IMAGE_SIDE = 1600
_MAX_IMAGE_PIXELS = _MAX_IMAGE_SIDE**2
_MAX_VIEW_SPAN_M = 1_000_000.0
_MAX_CANDIDATE_FACE_ID_SAMPLE = 12
_BLEND_TILE_SIDE = 64
_MAX_BLEND_TILE_FRAGMENTS = 131_072
_MAX_BLEND_TOTAL_FRAGMENTS = 8_000_000
_MAX_BLEND_PIXEL_LAYERS = 64
_BACKGROUND = np.array([238, 242, 246], dtype=np.uint8)
_Y_UP_TO_Z_UP = np.array(
    [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 0.0, -1.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ],
    dtype=np.float64,
)


@dataclass(frozen=True)
class _MeshPart:
    vertices: np.ndarray
    faces: np.ndarray
    uv: np.ndarray
    texture: Image.Image
    base_color_factor: np.ndarray
    alpha_mode: str
    alpha_factor: float
    node_name: str
    geometry_name: str
    face_offset: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_self_contained_glb(path: Path) -> None:
    """Reject non-GLB and external buffer/image references before loading."""

    with path.open("rb") as stream:
        header = stream.read(20)
        if len(header) < 20 or header[:4] != b"glTF":
            raise ValueError(f"mesh input must be a binary glTF (GLB) file: {path}")
        version, total_length, json_length, json_kind = struct.unpack_from(
            "<IIII", header, 4
        )
        if version != 2 or total_length != path.stat().st_size:
            raise ValueError(f"invalid GLB v2 header: {path}")
        if json_kind != 0x4E4F534A or json_length <= 0:
            raise ValueError(f"GLB first chunk must contain JSON: {path}")
        json_bytes = stream.read(json_length)
    try:
        document = json.loads(json_bytes.rstrip(b" \t\r\n\x00"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid GLB JSON chunk: {path}") from exc
    for collection in ("buffers", "images"):
        for index, item in enumerate(document.get(collection, [])):
            uri = item.get("uri")
            if uri is not None and not str(uri).startswith("data:"):
                raise ValueError(
                    f"GLB {collection}[{index}] has external URI {uri!r}; "
                    "mesh observations require self-contained assets"
                )
    if "KHR_texture_transform" in document.get("extensionsUsed", []):
        raise ValueError(
            "GLB uses KHR_texture_transform, which this rasterizer does not support"
        )
    for index, sampler in enumerate(document.get("samplers", [])):
        wrap_s = sampler.get("wrapS", 10497)
        wrap_t = sampler.get("wrapT", 10497)
        if wrap_s != 10497 or wrap_t != 10497:
            raise ValueError(
                f"GLB sampler[{index}] uses unsupported texture wrap modes "
                f"wrapS={wrap_s}, wrapT={wrap_t}; only glTF REPEAT is supported"
            )


def _json_value(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"cannot serialise {type(value).__name__}")


def _finite_vector(name: str, value: Any, length: int) -> np.ndarray:
    result = np.asarray(value, dtype=np.float64)
    if result.shape != (length,) or not np.isfinite(result).all():
        raise ValueError(f"{name} must contain {length} finite numbers")
    return result


def _validated_yaw(yaw_degrees: float) -> float:
    try:
        yaw = float(yaw_degrees)
    except (TypeError, ValueError) as exc:
        raise ValueError("yaw_degrees must be finite") from exc
    if not math.isfinite(yaw):
        raise ValueError("yaw_degrees must be finite")
    return yaw


def _yaw_matrix(yaw_degrees: float) -> np.ndarray:
    angle = math.radians(_validated_yaw(yaw_degrees))
    cosine, sine = math.cos(angle), math.sin(angle)
    return np.array(
        [
            [cosine, -sine, 0.0],
            [sine, cosine, 0.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )


def _validated_bounds(bounds: Any | None) -> np.ndarray | None:
    if bounds is None:
        return None
    result = np.asarray(bounds, dtype=np.float64)
    if result.shape != (2, 3) or not np.isfinite(result).all():
        raise ValueError("bounds must be [[xmin, ymin, zmin], [xmax, ymax, zmax]]")
    if np.any(result[1] <= result[0]):
        raise ValueError("each bounds maximum must be greater than its minimum")
    return result


def _finite_nonnegative(name: str, value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite non-negative number") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return result


def _axial_angle_degrees(vector_xy: np.ndarray) -> float | None:
    """Return an unoriented horizontal angle in [0, 180), or None if undefined."""

    length = float(np.linalg.norm(vector_xy))
    if length <= 1e-12:
        return None
    return math.degrees(math.atan2(float(vector_xy[1]), float(vector_xy[0]))) % 180.0


def _axial_delta_degrees(angle: np.ndarray, reference: float) -> np.ndarray:
    """Smallest signed differences between axial angles, in [-90, 90)."""

    return (angle - reference + 90.0) % 180.0 - 90.0


def _weighted_axial_mean_degrees(angles: np.ndarray, weights: np.ndarray) -> float:
    doubled = np.radians(angles * 2.0)
    sine = float(np.sum(weights * np.sin(doubled)))
    cosine = float(np.sum(weights * np.cos(doubled)))
    return (math.degrees(math.atan2(sine, cosine)) / 2.0) % 180.0


def _material_texture(
    mesh: trimesh.Trimesh, label: str
) -> tuple[Image.Image, np.ndarray, str, float]:
    if mesh.visual.kind != "texture":
        raise ValueError(
            f"{label} uses unsupported visual kind {mesh.visual.kind!r}; "
            "a UV-mapped base-color texture is required"
        )
    uv = getattr(mesh.visual, "uv", None)
    if uv is None or np.asarray(uv).shape != (len(mesh.vertices), 2):
        raise ValueError(f"{label} has no per-vertex TEXCOORD_0 UV mapping")
    material = getattr(mesh.visual, "material", None)
    if material is None or material.__class__.__name__ == "MultiMaterial":
        raise ValueError(f"{label} uses an unsupported multi-material scheme")
    image = getattr(material, "baseColorTexture", None)
    if image is None:
        image = getattr(material, "image", None)
    if image is None or not isinstance(image, Image.Image):
        raise ValueError(f"{label} has UVs but no supported base-color texture image")
    alpha_mode = getattr(material, "alphaMode", None)
    if alpha_mode not in (None, "OPAQUE", "BLEND"):
        raise ValueError(
            f"{label} uses unsupported material alpha mode {alpha_mode!r}"
        )
    rgba = image.convert("RGBA")
    alpha_min, alpha_max = rgba.getchannel("A").getextrema()
    if alpha_mode != "BLEND" and (alpha_min != 255 or alpha_max != 255):
        raise ValueError(
            f"{label} has texture alpha without an explicit supported BLEND material"
        )
    alpha_factor = 1.0
    factor = getattr(material, "baseColorFactor", None)
    if factor is None:
        factor_rgb = np.ones(3, dtype=np.float64)
    else:
        factor_array = np.asarray(factor, dtype=np.float64)
        if factor_array.shape not in ((3,), (4,)) or not np.isfinite(factor_array).all():
            raise ValueError(f"{label} has an invalid baseColorFactor")
        if factor_array.max(initial=0.0) > 1.0:
            factor_array = factor_array / 255.0
        if np.any(factor_array < 0.0) or np.any(factor_array > 1.0):
            raise ValueError(f"{label} has an out-of-range baseColorFactor")
        if len(factor_array) == 4:
            alpha_factor = float(factor_array[3])
        if alpha_mode != "BLEND" and alpha_factor < 1.0:
            raise ValueError(
                f"{label} has transparent baseColorFactor without an explicit BLEND material"
            )
        factor_rgb = factor_array[:3]
    return (rgba if alpha_mode == "BLEND" else rgba.convert("RGB"),
            factor_rgb, alpha_mode or "OPAQUE", alpha_factor)


def _apply_linear_rgb_factor(
    sampled_srgb: np.ndarray, factor_linear: np.ndarray
) -> np.ndarray:
    """Apply glTF's linear baseColorFactor to sRGB texture samples."""

    return _encode_linear_rgb(_decode_srgb(sampled_srgb) * factor_linear[None, :])


def _decode_srgb(sampled_srgb: np.ndarray) -> np.ndarray:
    srgb = sampled_srgb.astype(np.float64) / 255.0
    return np.where(
        srgb <= 0.04045,
        srgb / 12.92,
        ((srgb + 0.055) / 1.055) ** 2.4,
    )


def _encode_linear_rgb(linear: np.ndarray) -> np.ndarray:
    factored = np.clip(linear, 0.0, 1.0)
    encoded = np.where(
        factored <= 0.0031308,
        factored * 12.92,
        1.055 * factored ** (1.0 / 2.4) - 0.055,
    )
    return np.rint(encoded * 255.0).clip(0, 255).astype(np.uint8)


def _blend_over_opaque(selected, *, eye, target, right, up, forward, span,
                       colors, linear_backing, depth, face_buffer, world_buffer):
    """Bounded per-pixel fragment sorting, without modifying source geometry.

    Tiles bound temporary fragment storage. A half-open edge convention assigns
    a shared triangle edge once, including a textured quad's diagonal. Distinct
    coincident faces remain distinct; exact depth ties use global face ID.
    """
    height, width = depth.shape
    view_width, view_height = span
    prepared = []
    for part, vertices, face_ids in selected:
        if part.alpha_mode != "BLEND" or not len(face_ids):
            continue
        triangles = vertices[part.faces[face_ids]]
        relative = triangles - target
        sx, sy = relative @ right, relative @ up
        depths = (triangles - eye) @ forward
        denominator = ((sy[:, 1]-sy[:, 2])*(sx[:, 0]-sx[:, 2])
                       + (sx[:, 2]-sx[:, 1])*(sy[:, 0]-sy[:, 2]))
        columns = (sx/view_width + .5)*width - .5
        rows = (.5-sy/view_height)*height - .5
        # Use floating boxes until clipping to avoid integer overflow off screen.
        boxes = np.column_stack((np.maximum(0, np.ceil(columns.min(axis=1)-1e-9)),
            np.minimum(width-1, np.floor(columns.max(axis=1)+1e-9)),
            np.maximum(0, np.ceil(rows.min(axis=1)-1e-9)),
            np.minimum(height-1, np.floor(rows.max(axis=1)+1e-9))))
        usable = ((np.abs(denominator) > 1e-14) & (boxes[:, 0] <= boxes[:, 1])
                  & (boxes[:, 2] <= boxes[:, 3]) & (depths.max(axis=1) > 1e-9))
        prepared.append((part, triangles, part.uv[part.faces[face_ids]], face_ids,
                         sx, sy, depths, denominator, boxes, usable))
    candidate_faces = sum(int(item[-1].sum()) for item in prepared)
    invalid = np.zeros((height, width), dtype=bool)
    total_fragments = 0
    peak_tile_fragments = 0
    peak_pixel_layers = 0
    for row_min in range(0, height, _BLEND_TILE_SIDE):
        row_max = min(row_min + _BLEND_TILE_SIDE, height)
        for col_min in range(0, width, _BLEND_TILE_SIDE):
            col_max = min(col_min + _BLEND_TILE_SIDE, width)
            tile_width = col_max-col_min
            grid_x, grid_y = np.meshgrid(
                ((np.arange(col_min, col_max)+.5)/width-.5)*view_width,
                (.5-(np.arange(row_min, row_max)+.5)/height)*view_height)
            fragments = []
            tile_fragments = 0
            for part, triangles, uv, face_ids, sx_all, sy_all, depths, denominators, boxes, usable in prepared:
                in_tile = (usable & (boxes[:, 0] < col_max) & (boxes[:, 1] >= col_min)
                           & (boxes[:, 2] < row_max) & (boxes[:, 3] >= row_min))
                texture = np.asarray(part.texture)
                for i in np.flatnonzero(in_tile):
                    sx, sy, denom = sx_all[i], sy_all[i], denominators[i]
                    w0 = ((sy[1]-sy[2])*(grid_x-sx[2]) + (sx[2]-sx[1])*(grid_y-sy[2]))/denom
                    w1 = ((sy[2]-sy[0])*(grid_x-sx[2]) + (sx[0]-sx[2])*(grid_y-sy[2]))/denom
                    weights = (w0, w1, 1-w0-w1)
                    inside = np.ones(grid_x.shape, dtype=bool)
                    for weight, (a, b) in zip(weights, ((1, 2), (2, 0), (0, 1)), strict=True):
                        dx, dy = (sx[b]-sx[a])*np.sign(denom), (sy[b]-sy[a])*np.sign(denom)
                        inclusive = dy > 0 or (dy == 0 and dx < 0)
                        inside &= (weight > 1e-10) | ((np.abs(weight) <= 1e-10) & inclusive)
                    candidate_depth = sum(weight*depths[i, j] for j, weight in enumerate(weights))
                    active = (inside & (candidate_depth > 1e-9)
                              & (candidate_depth < depth[row_min:row_max, col_min:col_max]))
                    rr, cc = np.nonzero(active)
                    if not len(rr):
                        continue
                    bary = np.column_stack([weight[active] for weight in weights])
                    sampled_uv = bary @ uv[i]
                    tx = np.floor(np.mod(sampled_uv[:, 0], 1)*texture.shape[1]).astype(np.int64)
                    ty = np.floor(np.mod(1-sampled_uv[:, 1], 1)*texture.shape[0]).astype(np.int64)
                    rgba = texture[ty, tx]
                    alpha = rgba[:, 3].astype(np.float64)/255 * part.alpha_factor
                    nonzero = alpha > 0
                    count = int(nonzero.sum())
                    if not count:
                        continue
                    tile_fragments += count
                    total_fragments += count
                    if (tile_fragments > _MAX_BLEND_TILE_FRAGMENTS
                            or total_fragments > _MAX_BLEND_TOTAL_FRAGMENTS):
                        raise ValueError("BLEND fragment budget exceeded; use a smaller view or resolution; no partial observation saved")
                    fragments.append((
                        (rr[nonzero]*tile_width+cc[nonzero]).astype(np.int32),
                        candidate_depth[active][nonzero],
                        _decode_srgb(rgba[nonzero, :3])*part.base_color_factor[None, :],
                        alpha[nonzero],
                        np.full(count, part.face_offset+int(face_ids[i]), dtype=np.int32),
                        bary[nonzero] @ triangles[i]))
            if not fragments:
                continue
            flat, fragment_depth, rgb, alpha, identities, points = (
                np.concatenate([fragment[j] for fragment in fragments]) for j in range(6))
            order = np.lexsort((identities, fragment_depth, flat))
            flat, rgb, alpha, identities, points = (
                array[order] for array in (flat, rgb, alpha, identities, points))
            unique, starts, counts = np.unique(flat, return_index=True, return_counts=True)
            maximum_layers = int(counts.max())
            if maximum_layers > _MAX_BLEND_PIXEL_LAYERS:
                raise ValueError("BLEND per-pixel layer budget exceeded; no partial observation saved")
            peak_tile_fragments = max(peak_tile_fragments, tile_fragments)
            peak_pixel_layers = max(peak_pixel_layers, maximum_layers)
            transmission = np.ones(len(unique), dtype=np.float64)
            composite = np.zeros((len(unique), 3), dtype=np.float64)
            partial = np.zeros(len(unique), dtype=bool)
            for layer in range(maximum_layers):
                groups = np.flatnonzero((counts > layer) & (transmission > 0))
                indices = starts[groups]+layer
                a = alpha[indices]
                partial[groups] |= (a > 0) & (a < 1)
                composite[groups] += (transmission[groups]*a)[:, None]*rgb[indices]
                transmission[groups] *= 1-a
            rows = row_min + unique//tile_width
            columns = col_min + unique % tile_width
            composite += transmission[:, None]*linear_backing[rows, columns]
            colors[rows, columns] = _encode_linear_rgb(composite)
            # Alpha-zero fragments never entered this list. The closest remaining
            # fragment is a measurable hit only if no fractional alpha contributes.
            face_buffer[rows, columns] = np.where(partial, -1, identities[starts])
            world_buffer[rows, columns] = np.where(partial[:, None], np.nan, points[starts])
            invalid[rows, columns] = partial
    return invalid, {"candidate_faces": candidate_faces, "retained_fragments": total_fragments,
                     "peak_tile_fragments": peak_tile_fragments, "peak_pixel_layers": peak_pixel_layers}


class MeshObservation:
    """Load a GLB once and produce metric, queryable software-rendered views."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file():
            raise FileNotFoundError(f"mesh asset does not exist: {self.path}")
        _validate_self_contained_glb(self.path)
        self.mesh_sha256 = _sha256(self.path)
        try:
            scene = trimesh.load(self.path, force="scene", process=False)
        except Exception as exc:  # trimesh raises several loader-specific types
            raise ValueError(f"failed to load GLB mesh {self.path}: {exc}") from exc
        if not isinstance(scene, trimesh.Scene) or not scene.graph.nodes_geometry:
            raise ValueError(f"GLB contains no scene mesh geometry: {self.path}")

        parts: list[_MeshPart] = []
        face_offset = 0
        for node_name in scene.graph.nodes_geometry:
            scene_transform, geometry_name = scene.graph.get(node_name)
            mesh = scene.geometry[geometry_name]
            label = f"scene node {node_name!r} geometry {geometry_name!r}"
            if not isinstance(mesh, trimesh.Trimesh):
                raise ValueError(f"{label} is not triangular mesh geometry")
            vertices = np.asarray(mesh.vertices, dtype=np.float64)
            faces = np.asarray(mesh.faces, dtype=np.int64)
            if vertices.ndim != 2 or vertices.shape[1:] != (3,) or not len(vertices):
                raise ValueError(f"{label} has invalid or empty POSITION data")
            if faces.ndim != 2 or faces.shape[1:] != (3,) or not len(faces):
                raise ValueError(f"{label} has invalid or empty triangle indices")
            texture, base_color_factor, alpha_mode, alpha_factor = _material_texture(mesh, label)
            uv = np.asarray(mesh.visual.uv, dtype=np.float64)
            if not np.isfinite(vertices).all() or not np.isfinite(uv).all():
                raise ValueError(f"{label} has non-finite position or UV data")
            if faces.min() < 0 or faces.max() >= len(vertices):
                raise ValueError(f"{label} has out-of-range triangle indices")
            transformed = trimesh.transform_points(vertices, scene_transform)
            transformed = np.column_stack(
                (transformed[:, 0], -transformed[:, 2], transformed[:, 1])
            )
            parts.append(
                _MeshPart(
                    vertices=transformed,
                    faces=faces,
                    uv=uv,
                    texture=texture,
                    base_color_factor=base_color_factor,
                    alpha_mode=alpha_mode,
                    alpha_factor=alpha_factor,
                    node_name=str(node_name),
                    geometry_name=str(geometry_name),
                    face_offset=face_offset,
                )
            )
            face_offset += len(faces)
        self._parts = tuple(parts)
        self.vertex_count = sum(len(part.vertices) for part in self._parts)
        self.face_count = face_offset

    def _face_location(self, global_face_id: int) -> tuple[_MeshPart, int]:
        if global_face_id < 0 or global_face_id >= self.face_count:
            raise ValueError(
                f"face_id {global_face_id} is outside mesh face range "
                f"[0,{self.face_count - 1}]"
            )
        for part in self._parts:
            local_face_id = global_face_id - part.face_offset
            if 0 <= local_face_id < len(part.faces):
                return part, local_face_id
        raise RuntimeError(f"could not resolve valid global face_id {global_face_id}")

    @staticmethod
    def _triangle_evidence(
        part: _MeshPart,
        local_face_id: int,
        vertices: np.ndarray,
    ) -> dict[str, Any]:
        triangle = vertices[part.faces[local_face_id]]
        cross = np.cross(triangle[1] - triangle[0], triangle[2] - triangle[0])
        doubled_area = float(np.linalg.norm(cross))
        normal = cross / doubled_area if doubled_area > 1e-15 else None
        xy_edges = triangle[[1, 2, 0], :2] - triangle[[0, 1, 2], :2]
        horizontal_span = float(np.linalg.norm(xy_edges, axis=1).max(initial=0.0))
        if normal is None:
            plane_tilt = None
            trace_angle = None
        else:
            # A vertical plane has a horizontal normal.  Its horizontal trace is
            # perpendicular to that normal and is axial, so triangle winding
            # cannot change the reported direction.
            plane_tilt = math.degrees(math.asin(min(1.0, abs(float(normal[2])))))
            trace_angle = _axial_angle_degrees(np.array([-normal[1], normal[0]]))
        return {
            "global_face_id": part.face_offset + local_face_id,
            "local_face_id": local_face_id,
            "node_name": part.node_name,
            "geometry_name": part.geometry_name,
            "material_alpha_mode": part.alpha_mode,
            "material_geometry_caveat": (
                "Original carrier triangle; texture alpha does not cut or remove its geometry."
                if part.alpha_mode == "BLEND" else None
            ),
            "triangle_vertices_xyz": triangle.tolist(),
            "triangle_centroid_xyz": triangle.mean(axis=0).tolist(),
            "triangle_area_m2": doubled_area / 2.0,
            "triangle_horizontal_span_m": horizontal_span,
            "triangle_normal_xyz": None if normal is None else normal.tolist(),
            "plane_tilt_from_vertical_degrees": plane_tilt,
            "horizontal_surface_trace_direction_degrees": trace_angle,
        }

    def _operation_parts(
        self, yaw_degrees: float, bounds: Any | None
    ) -> tuple[list[tuple[_MeshPart, np.ndarray, np.ndarray]], np.ndarray | None]:
        yaw = _yaw_matrix(yaw_degrees)
        clip = _validated_bounds(bounds)
        selected: list[tuple[_MeshPart, np.ndarray, np.ndarray]] = []
        for part in self._parts:
            vertices = part.vertices @ yaw.T
            face_ids = np.arange(len(part.faces), dtype=np.int64)
            if clip is not None:
                centres = vertices[part.faces].mean(axis=1)
                inside = np.logical_and(centres >= clip[0], centres <= clip[1]).all(axis=1)
                face_ids = face_ids[inside]
            selected.append((part, vertices, face_ids))
        return selected, clip

    def describe(self, yaw_degrees: float = 0, bounds: Any | None = None) -> dict[str, Any]:
        """Return geometric mesh evidence in the requested operation frame."""

        selected, clip = self._operation_parts(yaw_degrees, bounds)
        selected_faces = sum(len(face_ids) for _, _, face_ids in selected)
        selected_vertex_sets = [
            np.unique(part.faces[face_ids].reshape(-1))
            for part, _, face_ids in selected
            if len(face_ids)
        ]
        selected_vertices = sum(len(ids) for ids in selected_vertex_sets)
        points = [
            vertices[ids]
            for (part, vertices, face_ids), ids in zip(
                (item for item in selected if len(item[2])), selected_vertex_sets, strict=True
            )
        ]
        geometric_bounds = (
            np.vstack((np.vstack(points).min(axis=0), np.vstack(points).max(axis=0)))
            if points
            else None
        )
        yaw = _validated_yaw(yaw_degrees)
        return {
            "mesh_path": str(self.path),
            "mesh_sha256": self.mesh_sha256,
            "coordinate_frame": "BIM-local right-handed Z-up metres",
            "source_coordinate_transform": {
                "order": "scene node transform, then glTF Y-up to BIM Z-up, then operation yaw",
                "y_up_to_z_up": "[X, -Z, Y]",
                "y_up_to_z_up_matrix": _Y_UP_TO_Z_UP.tolist(),
                "yaw_degrees_counterclockwise_about_positive_z": yaw,
                "yaw_matrix": _yaw_matrix(yaw).tolist(),
            },
            "vertex_count": self.vertex_count,
            "face_count": self.face_count,
            "scene_instance_count": len(self._parts),
            "selected_vertex_count": selected_vertices,
            "selected_face_count": selected_faces,
            "selected_material_face_counts": {
                mode: sum(len(ids) for part, _, ids in selected if part.alpha_mode == mode)
                for mode in ("OPAQUE", "BLEND")
            },
            "bounds": None if geometric_bounds is None else geometric_bounds.tolist(),
            "selection_bounds": None if clip is None else clip.tolist(),
            "selection_method": (
                "all faces"
                if clip is None
                else "triangle centroid inside inclusive bounds in the yaw-rotated BIM frame"
            ),
            "selection_caveat": (
                "A bounded centroid selection can hide intersecting or occluding surfaces; "
                "it is an observation filter, not evidence of enclosure truth."
            ),
            "evidence_caveat": (
                "Bounds and counts describe the supplied surface mesh only; they do not "
                "establish floors, rooms, enclosure, or a true footprint. BLEND texture "
                "alpha affects display and pixel measurement, not the original carrier geometry."
            ),
        }

    def surface_direction_evidence(
        self,
        *,
        yaw_degrees: float = 0,
        bounds: Any | None = None,
        face_ids: list[int] | None = None,
        max_plane_tilt_degrees: float = 15,
        min_triangle_area_m2: float = 1e-6,
        min_horizontal_span_m: float = 0.05,
        angle_bin_degrees: float = 2,
    ) -> dict[str, Any]:
        """Summarise near-vertical triangle surface directions in a selection.

        Directions describe the unoriented horizontal trace of supplied mesh
        triangles.  They are area-weighted orientation candidates, not fitted
        wall lines or a declaration of a building coordinate frame.
        """

        yaw = _validated_yaw(yaw_degrees)
        maximum_tilt = _finite_nonnegative(
            "max_plane_tilt_degrees", max_plane_tilt_degrees
        )
        if maximum_tilt > 90:
            raise ValueError("max_plane_tilt_degrees must be no greater than 90")
        minimum_area = _finite_nonnegative(
            "min_triangle_area_m2", min_triangle_area_m2
        )
        minimum_span = _finite_nonnegative(
            "min_horizontal_span_m", min_horizontal_span_m
        )
        bin_width = _finite_nonnegative("angle_bin_degrees", angle_bin_degrees)
        if bin_width <= 0 or bin_width > 45:
            raise ValueError("angle_bin_degrees must be greater than 0 and at most 45")
        bin_count = int(round(180.0 / bin_width))
        if bin_count < 1 or not math.isclose(
            bin_count * bin_width, 180.0, rel_tol=0.0, abs_tol=1e-9
        ):
            raise ValueError("angle_bin_degrees must divide the 180-degree axial range")

        requested_face_ids: set[int] | None = None
        if face_ids is not None:
            if not isinstance(face_ids, list):
                raise ValueError("face_ids must be a list of global integer face IDs")
            requested_face_ids = set()
            for face_id in face_ids:
                if isinstance(face_id, bool) or not isinstance(
                    face_id, (int, np.integer)
                ):
                    raise ValueError(
                        "face_ids must be a list of global integer face IDs"
                    )
                value = int(face_id)
                self._face_location(value)
                requested_face_ids.add(value)

        selected, clip = self._operation_parts(yaw, bounds)
        observations: list[dict[str, Any]] = []
        for part, vertices, local_face_ids in selected:
            for local_face_id_raw in local_face_ids:
                local_face_id = int(local_face_id_raw)
                global_face_id = part.face_offset + local_face_id
                if (
                    requested_face_ids is not None
                    and global_face_id not in requested_face_ids
                ):
                    continue
                observations.append(
                    self._triangle_evidence(part, local_face_id, vertices)
                )

        excluded = {
            "below_minimum_area": 0,
            "below_minimum_horizontal_span": 0,
            "outside_plane_tilt_limit": 0,
        }
        eligible: list[dict[str, Any]] = []
        for item in observations:
            if item["triangle_area_m2"] < minimum_area:
                excluded["below_minimum_area"] += 1
            elif item["triangle_horizontal_span_m"] < minimum_span:
                excluded["below_minimum_horizontal_span"] += 1
            elif (
                item["plane_tilt_from_vertical_degrees"] is None
                or item["plane_tilt_from_vertical_degrees"] > maximum_tilt
                or item["horizontal_surface_trace_direction_degrees"] is None
            ):
                excluded["outside_plane_tilt_limit"] += 1
            else:
                eligible.append(item)

        eligible_area = float(sum(item["triangle_area_m2"] for item in eligible))
        buckets: dict[int, list[dict[str, Any]]] = {}
        for item in eligible:
            angle = item["horizontal_surface_trace_direction_degrees"]
            bin_index = int(math.floor(angle / bin_width + 0.5)) % bin_count
            buckets.setdefault(bin_index, []).append(item)

        candidates: list[dict[str, Any]] = []
        for bin_index, items in buckets.items():
            weights = np.asarray(
                [item["triangle_area_m2"] for item in items], dtype=np.float64
            )
            angles = np.asarray(
                [item["horizontal_surface_trace_direction_degrees"] for item in items],
                dtype=np.float64,
            )
            spans = np.asarray(
                [item["triangle_horizontal_span_m"] for item in items], dtype=np.float64
            )
            tilts = np.asarray(
                [item["plane_tilt_from_vertical_degrees"] for item in items],
                dtype=np.float64,
            )
            mean_angle = _weighted_axial_mean_degrees(angles, weights)
            residuals = _axial_delta_degrees(angles, mean_angle)
            direction = np.array(
                [math.cos(math.radians(mean_angle)), math.sin(math.radians(mean_angle))]
            )
            vertices = np.vstack(
                [
                    np.asarray(item["triangle_vertices_xyz"], dtype=np.float64)
                    for item in items
                ]
            )
            projected = vertices[:, :2] @ direction
            area = float(weights.sum())
            candidate_face_ids = sorted(item["global_face_id"] for item in items)
            candidates.append(
                {
                    "angle_bin_center_degrees": bin_index * bin_width,
                    "area_weighted_direction_degrees": mean_angle,
                    "area_weighted_angular_rms_degrees": float(
                        math.sqrt(float(np.average(residuals**2, weights=weights)))
                    ),
                    "surface_area_m2": area,
                    "eligible_area_fraction": (
                        area / eligible_area if eligible_area > 0 else None
                    ),
                    "triangle_count": len(items),
                    "face_id_sample": candidate_face_ids[
                        :_MAX_CANDIDATE_FACE_ID_SAMPLE
                    ],
                    "omitted_face_id_count": max(
                        0, len(candidate_face_ids) - _MAX_CANDIDATE_FACE_ID_SAMPLE
                    ),
                    "area_weighted_plane_tilt_degrees": float(
                        np.average(tilts, weights=weights)
                    ),
                    "triangle_horizontal_span_m": {
                        "minimum": float(spans.min()),
                        "area_weighted_mean": float(np.average(spans, weights=weights)),
                        "maximum": float(spans.max()),
                    },
                    "selected_vertex_projection_extent_m": float(
                        projected.max() - projected.min()
                    ),
                }
            )
        candidates.sort(
            key=lambda candidate: (
                -candidate["surface_area_m2"],
                candidate["angle_bin_center_degrees"],
            )
        )

        return {
            "mesh_sha256": self.mesh_sha256,
            "coordinate_frame": "BIM-local right-handed Z-up metres",
            "yaw_degrees_counterclockwise_about_positive_z": yaw,
            "selection_bounds": None if clip is None else clip.tolist(),
            "selection_method": (
                (
                    "triangle centroid inside inclusive bounds in the yaw-rotated BIM "
                    "frame, intersected with requested global face IDs"
                )
                if clip is not None and requested_face_ids is not None
                else (
                    "triangle centroid inside inclusive bounds in the yaw-rotated BIM frame"
                    if clip is not None
                    else (
                        "requested global face IDs"
                        if requested_face_ids is not None
                        else "all mesh faces"
                    )
                )
            ),
            "requested_face_ids": (
                None if requested_face_ids is None else sorted(requested_face_ids)
            ),
            "selected_triangle_count": len(observations),
            "eligible_triangle_count": len(eligible),
            "eligible_surface_area_m2": eligible_area,
            "excluded_triangle_counts": excluded,
            "filters": {
                "max_plane_tilt_degrees": maximum_tilt,
                "max_abs_normal_z": math.sin(math.radians(maximum_tilt)),
                "min_triangle_area_m2": minimum_area,
                "min_horizontal_span_m": minimum_span,
                "angle_bin_degrees": bin_width,
                "angle_binning": (
                    "nearest bin centre over an unoriented 180-degree axial range"
                ),
            },
            "direction_semantics": (
                "degrees counter-clockwise from operation-frame +X, modulo 180; "
                "the direction is the horizontal trace of each near-vertical triangle "
                "plane; it is not an alignment yaw"
            ),
            "candidates": candidates,
            "evidence_caveat": (
                "These are area-weighted orientation statistics of selected mesh "
                "triangles. A bin may combine disconnected, parallel, adjacent, or "
                "differently semantic surfaces; its area share, angular concentration, "
                "and projection extent do not make it a fitted wall, a reliable wall "
                "edge, or a true coordinate axis. Use tight bounds or explicit face IDs "
                "and inspect the underlying surface evidence. Areas include original "
                "BLEND carrier triangles, not only their texture-visible coverage."
            ),
        }

    def render(
        self,
        out_prefix: Path,
        *,
        eye: list[float],
        target: list[float],
        width_m: float,
        height_m: float,
        width_px: int = 1200,
        height_px: int = 900,
        yaw_degrees: float = 0,
        bounds: Any | None = None,
    ) -> tuple[Image.Image, dict[str, Any]]:
        """Render a textured orthographic view and save queryable raster evidence."""

        camera_eye = _finite_vector("eye", eye, 3)
        camera_target = _finite_vector("target", target, 3)
        try:
            view_width, view_height = float(width_m), float(height_m)
        except (TypeError, ValueError) as exc:
            raise ValueError("width_m and height_m must be finite positive numbers") from exc
        if (
            not math.isfinite(view_width)
            or not math.isfinite(view_height)
            or view_width <= 0
            or view_height <= 0
            or view_width > _MAX_VIEW_SPAN_M
            or view_height > _MAX_VIEW_SPAN_M
        ):
            raise ValueError(
                f"width_m and height_m must be positive and no greater than {_MAX_VIEW_SPAN_M:g}"
            )
        if isinstance(width_px, bool) or isinstance(height_px, bool):
            raise ValueError("width_px and height_px must be integers")
        try:
            image_width, image_height = int(width_px), int(height_px)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("width_px and height_px must be integers") from exc
        if image_width != width_px or image_height != height_px:
            raise ValueError("width_px and height_px must be integers")
        if (
            image_width < 1
            or image_height < 1
            or image_width > _MAX_IMAGE_SIDE
            or image_height > _MAX_IMAGE_SIDE
            or image_width * image_height > _MAX_IMAGE_PIXELS
        ):
            raise ValueError(
                f"image resolution must be positive, at most {_MAX_IMAGE_SIDE} per side, "
                f"and at most {_MAX_IMAGE_PIXELS} pixels"
            )

        forward = camera_target - camera_eye
        camera_distance = float(np.linalg.norm(forward))
        if camera_distance <= 1e-9:
            raise ValueError("eye and target must be distinct")
        forward /= camera_distance
        reference_up = np.array([0.0, 0.0, 1.0])
        up_choice = "+Z"
        if abs(float(forward @ reference_up)) > 0.999:
            reference_up = np.array([0.0, 1.0, 0.0])
            up_choice = "+Y fallback because the view is parallel to +Z"
        right = np.cross(forward, reference_up)
        right /= np.linalg.norm(right)
        camera_up = np.cross(right, forward)
        camera_up /= np.linalg.norm(camera_up)

        selected, clip = self._operation_parts(yaw_degrees, bounds)
        if not any(len(face_ids) for _, _, face_ids in selected):
            raise ValueError("bounds selected no mesh faces")

        colors = np.empty((image_height, image_width, 3), dtype=np.uint8)
        colors[:] = _BACKGROUND
        depth = np.full((image_height, image_width), np.inf, dtype=np.float64)
        face_buffer = np.full((image_height, image_width), -1, dtype=np.int32)
        world_buffer = np.full((image_height, image_width, 3), np.nan, dtype=np.float32)
        has_blend = any(part.alpha_mode == "BLEND" and len(ids) for part, _, ids in selected)
        linear_backing = None
        if has_blend:
            linear_backing = np.empty((image_height, image_width, 3), dtype=np.float64)
            linear_backing[:] = _decode_srgb(_BACKGROUND)
        texture_arrays: dict[int, np.ndarray] = {}
        visible_candidate_faces = 0

        for part, vertices, local_face_ids in selected:
            if part.alpha_mode == "BLEND":
                continue
            texture_key = id(part.texture)
            texture = texture_arrays.setdefault(texture_key, np.asarray(part.texture))
            triangles = vertices[part.faces[local_face_ids]]
            triangle_uv = part.uv[part.faces[local_face_ids]]
            relative = triangles - camera_target
            screen_x = relative @ right
            screen_y = relative @ camera_up
            triangle_depth = (triangles - camera_eye) @ forward

            for index, local_face_id in enumerate(local_face_ids):
                sx, sy = screen_x[index], screen_y[index]
                denominator = (sy[1] - sy[2]) * (sx[0] - sx[2]) + (
                    sx[2] - sx[1]
                ) * (sy[0] - sy[2])
                if abs(float(denominator)) <= 1e-14:
                    continue
                col_float = (sx / view_width + 0.5) * image_width - 0.5
                row_float = (0.5 - sy / view_height) * image_height - 0.5
                col_min = max(0, int(math.ceil(float(col_float.min()) - 1e-9)))
                col_max = min(image_width - 1, int(math.floor(float(col_float.max()) + 1e-9)))
                row_min = max(0, int(math.ceil(float(row_float.min()) - 1e-9)))
                row_max = min(image_height - 1, int(math.floor(float(row_float.max()) + 1e-9)))
                if col_min > col_max or row_min > row_max:
                    continue
                visible_candidate_faces += 1
                columns = np.arange(col_min, col_max + 1)
                rows = np.arange(row_min, row_max + 1)
                px = ((columns + 0.5) / image_width - 0.5) * view_width
                py = (0.5 - (rows + 0.5) / image_height) * view_height
                grid_x, grid_y = np.meshgrid(px, py)
                weight0 = (
                    (sy[1] - sy[2]) * (grid_x - sx[2])
                    + (sx[2] - sx[1]) * (grid_y - sy[2])
                ) / denominator
                weight1 = (
                    (sy[2] - sy[0]) * (grid_x - sx[2])
                    + (sx[0] - sx[2]) * (grid_y - sy[2])
                ) / denominator
                weight2 = 1.0 - weight0 - weight1
                inside = (
                    (weight0 >= -1e-10)
                    & (weight1 >= -1e-10)
                    & (weight2 >= -1e-10)
                )
                candidate_depth = (
                    weight0 * triangle_depth[index, 0]
                    + weight1 * triangle_depth[index, 1]
                    + weight2 * triangle_depth[index, 2]
                )
                current = depth[row_min : row_max + 1, col_min : col_max + 1]
                update = inside & (candidate_depth > 1e-9) & (candidate_depth < current)
                if not update.any():
                    continue
                rr, cc = np.nonzero(update)
                global_rows, global_cols = rr + row_min, cc + col_min
                w0, w1, w2 = weight0[update], weight1[update], weight2[update]
                current[update] = candidate_depth[update]
                points = (
                    w0[:, None] * triangles[index, 0]
                    + w1[:, None] * triangles[index, 1]
                    + w2[:, None] * triangles[index, 2]
                )
                uv = (
                    w0[:, None] * triangle_uv[index, 0]
                    + w1[:, None] * triangle_uv[index, 1]
                    + w2[:, None] * triangle_uv[index, 2]
                )
                # glTF's default REPEAT sampler has a period of exactly one in
                # UV space.  PIL rows run downward, hence the V inversion.
                texture_x = np.floor(
                    np.mod(uv[:, 0], 1.0) * texture.shape[1]
                ).astype(np.int64)
                texture_y = np.floor(
                    np.mod(1.0 - uv[:, 1], 1.0) * texture.shape[0]
                ).astype(np.int64)
                colors[global_rows, global_cols] = _apply_linear_rgb_factor(
                    texture[texture_y, texture_x, :3], part.base_color_factor
                )
                if linear_backing is not None:
                    linear_backing[global_rows, global_cols] = (
                        _decode_srgb(texture[texture_y, texture_x, :3])*part.base_color_factor[None, :])
                face_buffer[global_rows, global_cols] = part.face_offset + int(local_face_id)
                world_buffer[global_rows, global_cols] = points.astype(np.float32)

        invalid = np.zeros((image_height, image_width), dtype=bool)
        blend_statistics = None
        if has_blend:
            invalid, blend_statistics = _blend_over_opaque(
                selected, eye=camera_eye, target=camera_target, right=right, up=camera_up,
                forward=forward, span=(view_width, view_height), colors=colors,
                linear_backing=linear_backing, depth=depth, face_buffer=face_buffer,
                world_buffer=world_buffer)
            visible_candidate_faces += blend_statistics["candidate_faces"]
        image = Image.fromarray(colors, mode="RGB")
        prefix = Path(out_prefix).expanduser().resolve()
        prefix.parent.mkdir(parents=True, exist_ok=True)
        png_path = Path(f"{prefix}.png")
        json_path = Path(f"{prefix}.json")
        npz_path = Path(f"{prefix}.npz")
        image.save(png_path)
        hit_flat = np.flatnonzero(face_buffer.reshape(-1) >= 0).astype(np.int32)
        np.savez_compressed(
            npz_path,
            image_shape=np.asarray((image_height, image_width), dtype=np.int32),
            pixel_indices=hit_flat,
            world_points=world_buffer.reshape(-1, 3)[hit_flat],
            face_ids=face_buffer.reshape(-1)[hit_flat],
            mesh_sha256=np.asarray(self.mesh_sha256),
            operation_yaw_degrees=np.asarray(_validated_yaw(yaw_degrees)),
            measurement_contract=np.asarray("opaque-or-unambiguous-blend-v1"),
            transparent_composite_indices=np.flatnonzero(invalid.reshape(-1)).astype(np.int32),
        )

        pixel_dx = view_width / image_width
        pixel_dy = view_height / image_height
        top_left = (
            camera_target
            + (-view_width / 2.0 + pixel_dx / 2.0) * right
            + (view_height / 2.0 - pixel_dy / 2.0) * camera_up
        )
        description = self.describe(yaw_degrees=yaw_degrees, bounds=bounds)
        metadata = {
            **description,
            "image_sha256": _sha256(png_path),
            "pixel_buffer_sha256": _sha256(npz_path),
            "camera": {
                "projection": "orthographic",
                "eye": camera_eye.tolist(),
                "target": camera_target.tolist(),
                "eye_target_distance_m": camera_distance,
                "forward_from_eye": forward.tolist(),
                "screen_right": right.tolist(),
                "screen_up": camera_up.tolist(),
                "up_reference": up_choice,
                "top_view_fallback": (
                    "When the view direction is parallel to Z, +Y is used as the stable "
                    "up reference; otherwise BIM +Z is used."
                ),
            },
            "view_span_m": {"width": view_width, "height": view_height},
            "resolution_px": {"width": image_width, "height": image_height},
            "pixel_center_mapping": {
                "formula": "plane_xyz(row,col)=top_left+col*column_step+row*row_step; surface_xyz additionally advances along camera forward to the visible triangle",
                "top_left_pixel_center_plane_xyz": top_left.tolist(),
                "column_step_world_xyz": (right * pixel_dx).tolist(),
                "row_step_world_xyz": (-camera_up * pixel_dy).tolist(),
                "pixel_width_m": pixel_dx,
                "pixel_height_m": pixel_dy,
                "pixel_coordinates": "zero-based [column, row] with image origin at top-left",
            },
            "rasterizer": {
                "texture_interpolation": "barycentric UV, glTF REPEAT wrap, nearest texture pixel",
                "texture_v_mapping": "PIL image rows are the vertical inverse of glTF UV",
                "depth_test": "nearest positive distance along camera forward",
                "material_display": (
                    "unlit, double-sided geometric observation; sRGB base-color texture is "
                    "decoded to linear RGB, multiplied by baseColorFactor, and encoded back "
                    "to sRGB; this is not PBR-lighting or backface-culling faithful"
                ),
                "candidate_faces_overlapping_view": visible_candidate_faces,
                "visible_pixel_count": int(len(hit_flat)+invalid.sum()),
                "measurement_valid_pixel_count": int(len(hit_flat)),
                "transparent_composite_pixel_count": int(invalid.sum()),
                "blend": {
                    "method": "per-pixel depth sorted linear RGB over; zero alpha skipped; half-open triangle edges",
                    "depth_ties": "OPAQUE wins equal depth; distinct BLEND faces ordered by global face ID",
                    "measurement": "Any contributing fractional alpha invalidates the single surface point; fully occluded and zero-alpha fragments do not",
                    "tile_side_px": _BLEND_TILE_SIDE,
                    "maximum_tile_fragments": _MAX_BLEND_TILE_FRAGMENTS,
                    "maximum_total_fragments": _MAX_BLEND_TOTAL_FRAGMENTS,
                    "maximum_pixel_layers": _MAX_BLEND_PIXEL_LAYERS,
                    "budget_failure": "explicit refusal before writing observation files; no fragment truncation",
                    "statistics": blend_statistics,
                },
            },
            "selection_bounds": None if clip is None else clip.tolist(),
            "artifacts": {
                "png": str(png_path),
                "metadata_json": str(json_path),
                "pixel_buffer_npz": str(npz_path),
                "pixel_buffer_layout": "sorted measurable flat pixel indices with compact world XYZ and global face IDs; separate sorted transparent-composite invalid indices",
            },
        }
        json_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, default=_json_value) + "\n",
            encoding="utf-8",
        )
        return image, metadata

    def pixel_query(
        self, out_prefix: Path, pixels: list[list[int]]
    ) -> dict[str, Any]:
        """Query visible world points saved by :meth:`render`."""

        if not isinstance(pixels, list):
            raise ValueError("pixels must be a list of [column, row] pairs")
        prefix = Path(out_prefix).expanduser().resolve()
        npz_path = Path(f"{prefix}.npz")
        if not npz_path.is_file():
            raise FileNotFoundError(f"render pixel buffer does not exist: {npz_path}")
        with np.load(npz_path, allow_pickle=False) as data:
            required = {
                "image_shape",
                "pixel_indices",
                "world_points",
                "face_ids",
                "mesh_sha256",
            }
            if not required.issubset(data.files):
                raise ValueError(f"invalid render pixel buffer: {npz_path}")
            stored_sha = str(data["mesh_sha256"].item())
            if stored_sha != self.mesh_sha256:
                raise ValueError(
                    "render pixel buffer belongs to a different mesh asset: "
                    f"expected {self.mesh_sha256}, found {stored_sha}"
                )
            image_shape = np.asarray(data["image_shape"], dtype=np.int64)
            hit_indices = np.asarray(data["pixel_indices"], dtype=np.int64)
            world_points = np.asarray(data["world_points"], dtype=np.float64)
            face_ids = np.asarray(data["face_ids"], dtype=np.int64)
            operation_yaw = (
                float(data["operation_yaw_degrees"].item())
                if "operation_yaw_degrees" in data.files
                else None
            )
            has_contract = "measurement_contract" in data.files
            if (has_contract and str(data["measurement_contract"].item()) != "opaque-or-unambiguous-blend-v1"):
                raise ValueError("unsupported render measurement contract")
            if ((has_contract or any(part.alpha_mode == "BLEND" for part in self._parts))
                    and (not has_contract or "transparent_composite_indices" not in data.files)):
                raise ValueError("BLEND-aware render measurement contract and invalid indices are required")
            invalid_indices = (np.asarray(data["transparent_composite_indices"], dtype=np.int64)
                               if "transparent_composite_indices" in data.files else np.empty(0, dtype=np.int64))
        if image_shape.shape != (2,) or np.any(image_shape <= 0):
            raise ValueError(f"invalid image shape in render pixel buffer: {npz_path}")
        if (
            hit_indices.ndim != 1
            or world_points.shape != (len(hit_indices), 3)
            or face_ids.shape != (len(hit_indices),)
            or (len(hit_indices) and np.any(hit_indices[1:] <= hit_indices[:-1]))
            or invalid_indices.ndim != 1
            or (len(invalid_indices) and np.any(invalid_indices[1:] <= invalid_indices[:-1]))
            or np.any(hit_indices < 0) or np.any(hit_indices >= int(np.prod(image_shape)))
            or np.any(invalid_indices < 0) or np.any(invalid_indices >= int(np.prod(image_shape)))
            or np.intersect1d(hit_indices, invalid_indices, assume_unique=True).size
        ):
            raise ValueError(f"invalid compact arrays in render pixel buffer: {npz_path}")

        height, width = (int(image_shape[0]), int(image_shape[1]))
        results: list[dict[str, Any]] = []
        hit_points: list[np.ndarray | None] = []
        evidence_vertices = {}
        for pixel in pixels:
            if (
                not isinstance(pixel, (list, tuple))
                or len(pixel) != 2
                or isinstance(pixel[0], bool)
                or isinstance(pixel[1], bool)
                or not isinstance(pixel[0], (int, np.integer))
                or not isinstance(pixel[1], (int, np.integer))
            ):
                raise ValueError("each pixel must be an integer [column, row] pair")
            column, row = int(pixel[0]), int(pixel[1])
            if column < 0 or column >= width or row < 0 or row >= height:
                raise ValueError(
                    f"pixel {[column, row]} is outside image bounds "
                    f"[0,{width - 1}] x [0,{height - 1}]"
                )
            flat_index = row * width + column
            location = int(np.searchsorted(hit_indices, flat_index))
            if location < len(hit_indices) and hit_indices[location] == flat_index:
                point = world_points[location]
                face_id = int(face_ids[location])
                result = {
                    "pixel": [column, row],
                    "hit": True,
                    "measurement_valid": True,
                    "world_xyz": point.tolist(),
                    "face_id": face_id,
                }
                if operation_yaw is not None and math.isfinite(operation_yaw):
                    part, local_face_id = self._face_location(face_id)
                    if id(part) not in evidence_vertices:
                        evidence_vertices[id(part)] = part.vertices @ _yaw_matrix(operation_yaw).T
                    vertices = evidence_vertices[id(part)]
                    result["triangle_surface_evidence"] = self._triangle_evidence(
                        part, local_face_id, vertices
                    )
                else:
                    result["triangle_surface_evidence"] = None
                    result["triangle_surface_evidence_unavailable_reason"] = (
                        "legacy pixel buffer does not record its operation yaw"
                    )
                results.append(result)
                hit_points.append(point)
            else:
                invalid_location = int(np.searchsorted(invalid_indices, flat_index))
                transparent = (invalid_location < len(invalid_indices)
                               and invalid_indices[invalid_location] == flat_index)
                result = {"pixel": [column, row], "hit": False,
                          "measurement_valid": False, "background": not transparent}
                if transparent:
                    result["reason"] = "transparent_composite"
                    result["measurement_invalid_reason"] = (
                        "A fractional-alpha fragment contributes to this displayed pixel; no unique mesh surface point is assigned.")
                results.append(result)
                hit_points.append(None)

        pairwise: dict[str, Any] | None = None
        if len(results) >= 2:
            pairwise = {"pixels": [results[0]["pixel"], results[1]["pixel"]]}
            if hit_points[0] is not None and hit_points[1] is not None:
                pairwise.update(
                    {
                        "available": True,
                        "distance_m": float(np.linalg.norm(hit_points[1] - hit_points[0])),
                        "same_triangle": results[0]["face_id"] == results[1]["face_id"],
                        "surface_relation_caveat": (
                            "Different face IDs may still tessellate one surface, while "
                            "similar normals on different faces do not establish one wall "
                            "or wall edge."
                        ),
                    }
                )
            else:
                pairwise.update(
                    {
                        "available": False,
                        "distance_m": None,
                        "reason": "both of the first two pixels must have an unambiguous measurable mesh hit",
                    }
                )
        return {
            "mesh_sha256": self.mesh_sha256,
            "pixel_buffer_npz": str(npz_path),
            "resolution_px": {"width": width, "height": height},
            "operation_yaw_degrees_counterclockwise_about_positive_z": operation_yaw,
            "queries": results,
            "first_two_distance": pairwise,
        }
