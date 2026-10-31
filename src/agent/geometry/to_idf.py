"""Materialize a BuildingGeometry into a minimal eppy IDF.

Used to validate the geometry kernel against the real InterZone gate (which reads
eppy BuildingSurface:Detailed), and reusable as the deterministic surface writer
for pipeline integration. Geometry only — zones + building surfaces (+ optional
fenestration); no constructions/loads.
"""

from __future__ import annotations

from io import StringIO

from eppy.modeleditor import IDF

from src.agent._share import ensure_schema_initialized
from src.agent.geometry.build import BuildingGeometry


def building_to_idf(
    bg: BuildingGeometry, *, construction: str = "Default", boundary_validation_only: bool = False,
) -> IDF:
    # The boundary checker needs closed parent faces. This explicit projection
    # is never the actual downstream aperture adapter.
    if not boundary_validation_only:
        from src.agent.geometry.openings import require_supported_ep_openings

        require_supported_ep_openings(bg)
    ensure_schema_initialized()
    idf = IDF(StringIO(""))

    for z in dict.fromkeys(bg.zones):  # dedupe, keep order
        idf.newidfobject("ZONE", Name=z)

    for s in bg.surfaces:
        obj = idf.newidfobject(
            "BuildingSurface:Detailed",
            Name=s.name,
            Surface_Type=s.stype,
            Construction_Name=construction,
            Zone_Name=s.zone,
            Outside_Boundary_Condition=s.obc,
            Outside_Boundary_Condition_Object=s.obc_obj or "",
            Sun_Exposure="SunExposed" if s.obc == "Outdoors" else "NoSun",
            Wind_Exposure="WindExposed" if s.obc == "Outdoors" else "NoWind",
        )
        obj.Number_of_Vertices = len(s.verts)
        for i, v in enumerate(s.verts, 1):
            setattr(obj, f"Vertex_{i}_Xcoordinate", round(v[0], 4))
            setattr(obj, f"Vertex_{i}_Ycoordinate", round(v[1], 4))
            setattr(obj, f"Vertex_{i}_Zcoordinate", round(v[2], 4))

    for w in bg.windows:
        obj = idf.newidfobject(
            "FenestrationSurface:Detailed",
            Name=w.name,
            Surface_Type="Window",
            Construction_Name=construction,
            Building_Surface_Name=w.parent,
        )
        obj.Number_of_Vertices = len(w.verts)
        for i, v in enumerate(w.verts, 1):
            setattr(obj, f"Vertex_{i}_Xcoordinate", round(v[0], 4))
            setattr(obj, f"Vertex_{i}_Ycoordinate", round(v[1], 4))
            setattr(obj, f"Vertex_{i}_Zcoordinate", round(v[2], 4))

    return idf
