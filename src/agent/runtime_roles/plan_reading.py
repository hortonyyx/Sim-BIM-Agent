"""Saved two-dimensional reading products; no source BIM or invented heights."""
from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import LineString


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def product_identity(receipt):
    """Both old BIM trials and current planar trials retain exact product hashes."""
    if receipt.get("reading_product_file"):
        return receipt["reading_product_file"], receipt["reading_product_sha256"]
    return f"{receipt['candidate']}/source_model.json", receipt.get("candidate_source_sha256")


def geometry_ready(receipt):
    return receipt.get("reading_geometry_ready", receipt.get("source_geometry_ready")) is True


def comparison_facts(product):
    """XY-only relation projection for the existing assembly fidelity comparison."""
    from shapely.geometry import Polygon
    metadata = product["mapping"]
    spaces = [{"id": row["space_id"], "floor_id": metadata["floor_id"], "polygon": row["world_polygon_m"]}
              for row in metadata["space_mapping"]]
    adjacency = []
    for i, first in enumerate(spaces):
        for second in spaces[i + 1:]:
            if Polygon(first["polygon"]).boundary.intersection(Polygon(second["polygon"]).boundary).length > 1e-7:
                adjacency.append({"space_ids": [first["id"], second["id"]]})
    return {"spaces": spaces, "boundary_relations": adjacency,
            "openings": [{"id": row["opening_id"], "kind": row["kind"], "space_ids": row["space_ids"],
                          "vertices": [row["p1_world_m"], row["p2_world_m"]], "exterior": row["exterior"]}
                         for row in metadata["opening_hosts"]]}


def _opening_overlaps(plan):
    rows = plan.get("openings", [])
    for index, first in enumerate(rows):
        a = LineString([first["p1"], first["p2"]])
        for second in rows[index + 1:]:
            if a.intersection(LineString([second["p1"], second["p2"]])).length > 1e-7:
                # A plan can explicitly describe vertically stacked apertures.
                az, bz = first.get("z"), second.get("z")
                if az and bz and min(az[1], bz[1]) <= max(az[0], bz[0]):
                    continue
                raise ValueError(f"overlapping plan openings {first['id']} and {second['id']}; "
                                 "resolve duplicate apertures or supply observed separate vertical intervals")


def _font(size=16):
    try:
        return ImageFont.truetype("arial.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _wall_view_geometry(plan, metadata):
    """Project declared wall faces only; no measurement, snapping or data edits."""
    from src.agent.geometry.reader_wall_metadata import wall_reading_input
    reading = wall_reading_input(plan, metadata)
    calibration = metadata["calibration"]
    transforms = []
    for axis in ("x", "y"):
        (p0, v0), (p1, v1) = calibration[f"{axis}_anchors"]
        transforms.append((p0, v0, (p1 - p0) / (v1 - v0)))

    def pixel(point):
        return tuple(p0 + (point[i] - v0) * slope
                     for i, (p0, v0, slope) in enumerate(transforms))

    bands, lines, known_faces = [], [], 0
    for wall in reading["walls"]:
        thickness, side = wall["thickness_m"], wall["reference_side"]
        offsets = None if thickness is None or side == "unknown" else {
            "centerline": (-thickness / 2, thickness / 2),
            "negative_face": (0, thickness), "positive_face": (-thickness, 0),
        }[side]
        known_faces += offsets is not None
        exterior = wall["target"].startswith("footprint:")
        for a, b in wall["segments_m"]:
            # wall_reading_input returns exactly orthogonal, ordered segments.
            normal = 0 if a[0] == b[0] else 1
            along = 1 - normal
            position, start, end = a[normal], a[along], b[along]
            cuts = []
            for opening in metadata["opening_hosts"]:
                p, q = opening["p1_world_m"], opening["p2_world_m"]
                # Exact adopted line identity, never nearby ink or a new tolerance.
                if opening["exterior"] != exterior or p[normal] != position or q[normal] != position:
                    continue
                lo, hi = max(start, min(p[along], q[along])), min(end, max(p[along], q[along]))
                if lo < hi:
                    cuts.append((lo, hi))
            solid, cursor = [], start
            for lo, hi in sorted(cuts):
                if cursor < lo:
                    solid.append((cursor, lo))
                cursor = max(cursor, hi)
            if cursor < end:
                solid.append((cursor, end))
            for lo, hi in solid:
                def point(distance, offset=0):
                    return pixel([position + offset, distance] if normal == 0
                                 else [distance, position + offset])
                if offsets is None:
                    lines.append([point(lo), point(hi)])
                else:
                    bands.append([point(lo, offsets[0]), point(hi, offsets[0]),
                                  point(hi, offsets[1]), point(lo, offsets[1])])
    return {"bands": bands, "lines": lines, "summary": {
        "walls_with_known_faces": known_faces,
        "walls_shown_as_lines": len(reading["walls"]) - known_faces,
        "solid_band_segments": len(bands),
        "note": "Overlay key: gray is the original drawing; colors are adopted results. "
                "Bands show declared thickness and reference side, not code-measured wall faces. "
                "Unknown thickness or side stays a single line; hosted opening spans are left clear.",
    }}


def _views(plan, metadata, original, *, wall_views=None):
    """Render adopted coordinates through the original calibration, without refitting."""
    wall_views = wall_views if wall_views is not None else _wall_view_geometry(plan, metadata)
    pictures = [original.convert("L").convert("RGB"), Image.new("RGB", original.size, "white")]
    # One transparent adopted-geometry layer, composited over two backgrounds.
    # Coordinates, bands, IDs, opening colours and calibration are identical.
    layer = Image.new("RGBA", original.size)
    bands = ImageDraw.Draw(layer)
    for polygon in wall_views['bands']:
        bands.polygon(polygon, fill=(15, 110, 195, 42), outline=(15, 110, 195, 255), width=1)
    picture = layer
    draw = ImageDraw.Draw(picture)
    wall = (15, 110, 195)
    def line(points, color, width):
        draw.line([tuple(p) for p in points], fill=color, width=width, joint="curve")
    for points in wall_views["lines"]:
        line(points, wall, 3)
    for row in plan["openings"]:
        color = (10, 167, 151) if row["kind"] == "window" else (214, 83, 32)
        line([row["p1"], row["p2"]], color, 5)
        middle = [(a + b) / 2 for a, b in zip(row["p1"], row["p2"])]
        draw.text((middle[0] + 5, middle[1] + 4), row["id"], font=_font(13), fill=color)
    for row in metadata["space_mapping"]:
        from shapely.geometry import Polygon
        p = Polygon(row["pixel_polygon"]).representative_point()
        draw.text((p.x, p.y), row["space_id"], font=_font(16), fill=(65, 65, 65), anchor="mm")
    # The anchor marker lies on its declared original pixel, never on a
    # freshly fitted ink bbox. This makes global calibration drift visible.
    for axis in ("x", "y"):
        for px, metres in plan[f"{axis}_anchors"]:
            if axis == "x":
                draw.line([(px, 0), (px, picture.height)], fill=(205, 110, 180), width=1)
                draw.text((px + 3, 12), f"x {px:g}px = {metres:g}m", font=_font(14), fill=(125, 30, 105))
            else:
                draw.line([(0, px), (picture.width, px)], fill=(205, 110, 180), width=1)
                draw.text((12, px + 3), f"y {px:g}px = {metres:g}m", font=_font(14), fill=(125, 30, 105))
    return [Image.alpha_composite(p.convert('RGBA'), layer).convert('RGB') for p in pictures]


def build_reading(plan, *, workspace, image_name, number):
    from src.agent.geometry.plan_partition import compile_plan_partition
    from src.agent.geometry.reader_wall_metadata import validate_wall_metadata, require_wall_thickness
    plan = require_wall_thickness(plan)
    workspace = Path(workspace)
    image_path = workspace / "images" / image_name
    raw_image = image_path.read_bytes()
    with Image.open(io.BytesIO(raw_image)) as image:
        original = image.convert("RGB")
    proposal, metadata = compile_plan_partition(plan, image_size=original.size, image_name=image_name)
    from src.agent.geometry.plan_drawing_differences import drawing_differences
    differences = drawing_differences(original, plan, image_name=image_name,
        image_sha256=hashlib.sha256(raw_image).hexdigest(), plan_sha256=hashlib.sha256(_bytes(plan)).hexdigest())
    from src.agent.geometry.plan_wall_metadata_checks import append_wall_metadata_checks
    differences = append_wall_metadata_checks(differences, original, plan, metadata)
    _opening_overlaps(plan)
    walls = validate_wall_metadata(plan)
    supplied_wall_targets = {row["target"] for row in plan.get("wall_metadata", [])}
    known_wall_count = sum(row["thickness_m"] is not None for row in walls)
    wall_views = _wall_view_geometry(plan, metadata)
    wall_summary = {
        "counts": {"total": len(walls), "supplied": len(supplied_wall_targets),
                   "known": known_wall_count, "unknown": len(walls) - known_wall_count,
                   "defaulted_unknown": sum(row["target"] not in supplied_wall_targets for row in walls)},
        "scope": "Wall metadata for this floor; counts do not establish drawing completeness.",
        "note": "Every adopted wall has a 1 cm thickness. Inferred batch values remain labelled inferred; "
                "clean plan and overlay use the same adopted geometry layer.",
        "rendering": wall_views["summary"],
    }
    product = {"schema": "plan_reading_v1", "image": image_name,
               "image_sha256": hashlib.sha256(raw_image).hexdigest(),
               "geometry": proposal["geometry"], "mapping": metadata, "walls": walls,
               "coverage": {"floor_id": plan["floor_id"], "mode": "complete_floor_plan",
                            "vertical_geometry": "assembly_pending"},
               "validation": {"topology": "pass", "opening_hosts": "pass", "overlapping_openings": "pass",
                              "drawing_fidelity": "reader_review_required"}}
    folder = workspace / f"reading_{number:03d}"
    folder.mkdir(parents=True, exist_ok=True)
    product_path = folder / "reading.json"
    product_path.write_bytes(_bytes(product))
    plan_path = folder / "plan.json"
    plan_path.write_bytes(_bytes(plan))
    contents, views = [], []
    for label, picture in zip(("overlay", "plan"), _views(plan, metadata, original, wall_views=wall_views)):
        path = folder / f"{label}.png"
        picture.save(path, "PNG")
        raw = path.read_bytes()
        contents.append({"type": "image", "mimeType": "image/png", "data": base64.b64encode(raw).decode()})
        views.append({"file": path.relative_to(workspace).as_posix(), "sha256": hashlib.sha256(raw).hexdigest()})
    result = {"status": "passed", "source_geometry_ready": False, "reading_geometry_ready": True,
              "candidate": folder.name,
              "reading_product_file": product_path.relative_to(workspace).as_posix(),
              "reading_product_sha256": hashlib.sha256(product_path.read_bytes()).hexdigest(),
              "plan_input": {"plan_file": plan_path.relative_to(workspace).as_posix(),
                             "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
                             "submitted_plan_file": plan_path.relative_to(workspace).as_posix(),
                             "image": image_name, "image_sha256": product["image_sha256"]},
              "source_plan_views": views,
              "wall_metadata_summary": wall_summary,
              "building_precision": {"status": "pass", "scope": "2D topology, hosts, grid; no height test",
                                     "counts": {"spaces": len(metadata["space_mapping"]),
                                                "openings": len(metadata["opening_hosts"])}},
              "drawing_differences": differences}
    # Match the JSON boundary of MCP results (e.g. report tuples become arrays)
    # so in-memory receipts and restored receipts have identical values.
    return {"content": contents, "structuredContent": json.loads(_bytes(result)), "isError": False}
