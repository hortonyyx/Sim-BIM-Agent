"""Render a saved source floor, without drawing calibration or a fidelity verdict."""
import re

from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import LineString, Polygon

from src.agent.geometry.source_floor_selection import select_source_floor, select_source_floor_openings
from src.agent.geometry.source_image_overlay import _source_hash
from src.agent.geometry.source_naming import public_names_for_display


def _name_label(draw, name, width):
    """Wrap at the public name's separators, keeping every character readable."""
    font = ImageFont.load_default(size=13)
    lines = [""]
    for part in re.findall(r"[^_-]+[_-]?", name):
        if lines[-1] and draw.textlength(lines[-1] + part, font=font) > width:
            lines.append("")
        lines[-1] += part
    return "\n".join(lines), font


def render_source_plan(source: dict, floor_id: str):
    source_hash = _source_hash(source)
    floor, rooms = select_source_floor(source, floor_id)
    if not rooms:
        raise ValueError("unknown floor_id")
    points = [point for space in rooms for point in space["polygon"]]
    x0, x1 = min(p[0] for p in points), max(p[0] for p in points)
    y0, y1 = min(p[1] for p in points), max(p[1] for p in points)
    scale = min(1000 / (x1 - x0), 700 / (y1 - y0))
    convert = lambda p: (40 + (p[0] - x0) * scale, 40 + (y1 - p[1]) * scale)
    pic = Image.new("RGB", (1080, 800), "white")
    draw = ImageDraw.Draw(pic)
    names = public_names_for_display(source)
    floor_name = names.get("floors", {}).get(floor_id, floor_id)
    draw.text((40, 12), f"SOURCE BIM / {floor_name} / not drawing evidence", fill="black")
    draw.text((1040, 15), "+Y / N", fill="black", anchor="rt")
    draw.line([(1050, 70), (1050, 30)], fill="black", width=3)
    draw.polygon([(1050, 25), (1045, 35), (1055, 35)], fill="black")
    for index, space in enumerate(rooms):
        ring = [convert(point) for point in space["polygon"]]
        holes = [[convert(point) for point in h] for h in space.get('holes', [])]
        mask = Image.new('L', pic.size, 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.polygon(ring, fill=255)
        for hole in holes:
            mask_draw.polygon(hole, fill=0)
        pic.paste((220 + (index * 11) % 30, 225, 235), mask=mask)
        for outline in [ring, *holes]:
            draw.line(outline + outline[:1], fill='black', width=3, joint='curve')
    # Draw labels after all room fills so a neighbouring polygon cannot erase
    # a label. Concave rooms use their interior horizontal span for wrapping.
    for space in rooms:
        polygon = Polygon(space["polygon"], space.get('holes', []))
        point = polygon.representative_point()
        span = polygon.intersection(LineString([(x0, point.y), (x1, point.y)]))
        spans = list(span.geoms) if hasattr(span, "geoms") else [span]
        local = next(part for part in spans if part.distance(point) < 1e-7)
        width = max(40, (local.bounds[2] - local.bounds[0]) * scale - 16)
        name = names.get("spaces", {}).get(space["id"], space["id"])
        label, font = _name_label(draw, name, width)
        draw.multiline_text(convert((point.x, point.y)), label, font=font,
                            fill="black", anchor="mm", align="center", spacing=3)
    openings = select_source_floor_openings(source, floor, rooms)
    for opening in openings:
        points = list(dict.fromkeys(tuple(vertex[:2]) for vertex in opening["vertices"]))
        draw.line([convert(point) for point in points],
                  fill="blue" if opening["kind"] == "window" else "red", width=6)
    draw.text((40, 765), f"x: {x0:g}..{x1:g} m; y: {y0:g}..{y1:g} m; blue: windows; red: doors/passages", fill="black")
    return pic, {
        "source_model_sha256": source_hash,
        "floor_id": floor_id,
        "floor_name": floor_name,
        "space_names": {space["id"]: names.get("spaces", {}).get(space["id"], space["id"]) for space in rooms},
        "naming_scheme": names.get("scheme_version"),
        "space_ids": [space["id"] for space in rooms],
        "opening_ids": [opening["id"] for opening in openings],
        **({"plan_geometry": "XY projection of volume within the requested storey; not a constant-height extrusion",
            "vertical_section_space_ids": [space["id"] for space in rooms if space.get("vertical_sections")]}
           if any(space.get("vertical_sections") for space in rooms) else {}),
        "world_bounds_m": {"x": [x0, x1], "y": [y0, y1]},
        "scope": "actual saved source plan; no original calibration, height verification or drawing-fidelity verdict",
    }
