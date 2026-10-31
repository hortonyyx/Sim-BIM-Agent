"""Build a 3D mesh from building_geometry.json (trimesh) → GLB + static PNG.

The user geometry-confirmation gate (contracts §1 2/3 ②a) needs a 3D view of the
deterministic build. Per the build plan this starts with **trimesh** (already a
dependency): triangulate every surface, export a GLB (for an interactive viewer)
plus a static axonometric PNG (for headless / quick eyeballing). A richer
interactive viewer (pyvista export_html / three.js) is a later spike.

Discipline (build plan M2b): the viewer NEVER blocks the geometry checks. If
trimesh is missing or a headless renderer is unavailable, this exits with an
explicit SKIP message and a non-zero-but-distinct code — it does not fake a PASS.

Usage:
    python scripts/tool_scripts/render_building_3d.py <building_geometry.json> [--glb out.glb] [--png out.png]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SKIP_EXIT = 3  # distinct from success(0)/error(1) so callers can tell SKIP apart


def _load_scene(data: dict):
    if data.get("openings"):
        raise ValueError(
            "GLB/PNG export does not yet support source doors/open passages; "
            "refusing to replace their openings with solid walls. "
            "Use render_geometry_viewer.py for a view that preserves openings."
        )
    import numpy as np
    import trimesh

    verts: list = []
    faces: list = []
    colors: list = []
    # color by OBC so split-pairing reciprocal/internal vs outdoor reads at a glance
    obc_color = {
        "Outdoors": [180, 200, 220, 255],
        "Surface": [120, 200, 120, 255],   # internal reciprocal interfaces
        "Ground": [150, 130, 100, 255],
        "Adiabatic": [220, 160, 90, 255],
    }
    for s in data.get("surfaces", []):
        ring = [tuple(v) for v in s["verts"]]
        if len(ring) < 3:
            continue
        base = len(verts)
        verts.extend(ring)
        # fan-triangulate the (convex, planar) face
        for i in range(1, len(ring) - 1):
            faces.append([base, base + i, base + i + 1])
            colors.append(obc_color.get(s.get("obc", "Outdoors"), [200, 200, 200, 255]))
    for w in data.get("windows", []):
        ring = [tuple(v) for v in w["verts"]]
        if len(ring) < 3:
            continue
        base = len(verts)
        verts.extend(ring)
        for i in range(1, len(ring) - 1):
            faces.append([base, base + i, base + i + 1])
            colors.append([60, 120, 200, 255])
    if not faces:
        raise ValueError("no surfaces to mesh")
    mesh = trimesh.Trimesh(
        vertices=np.asarray(verts, float),
        faces=np.asarray(faces, int),
        face_colors=np.asarray(colors, np.uint8),
        process=False,
    )
    return mesh


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("json", help="building_geometry.json")
    ap.add_argument("--glb", help="output GLB path (default: <json>.glb)")
    ap.add_argument("--png", help="output static PNG path (default: <json>_3d.png)")
    args = ap.parse_args()

    try:
        import trimesh  # noqa: F401
    except ImportError:
        print("SKIP: trimesh not available — 3D viewer not produced (not a PASS)",
              file=sys.stderr)
        return SKIP_EXIT

    j = Path(args.json)
    data = json.loads(j.read_text(encoding="utf-8"))
    mesh = _load_scene(data)

    glb = Path(args.glb) if args.glb else j.with_suffix(".glb")
    mesh.export(glb)
    print(f"wrote {glb}  ({len(mesh.faces)} faces)")

    png = Path(args.png) if args.png else j.with_name(j.stem + "_3d.png")
    try:
        png_bytes = mesh.scene().save_image(resolution=(1000, 800))
        Path(png).write_bytes(png_bytes)
        print(f"wrote {png}")
    except Exception as e:  # noqa: BLE001 — headless render unavailable is a SKIP
        print(f"SKIP static PNG: headless renderer unavailable ({type(e).__name__}: "
              f"{e}); GLB still written", file=sys.stderr)
        return SKIP_EXIT
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
