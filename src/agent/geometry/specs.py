"""Serialize deterministic BuildingGeometry -> downstream spec text (fork a).

The geometry kernel produces fully-resolved surfaces (vertices CCW-from-outside,
OBC, reciprocal interzone pairing). This module turns that into the free-text
`zone_specs` / `surface_specs` / `fenestration_specs` the downstream zone /
surface / fenestration agents transcribe verbatim — so geometry is deterministic
end-to-end while the `IntakeOutput` contract and downstream code stay unchanged.

Construction names are assigned here from a fixed vocabulary keyed on surface
type + OBC; the 4_MEP stage must define exactly the constructions this serializer
emits (returned as `used_constructions`). Paired interzone faces both get
`Cons_InterFloor`, so the reverse-layer symmetry EnergyPlus requires holds
trivially (rules.md §5.1).
"""

from __future__ import annotations

import json
import re
from typing import Literal

from src.agent.geometry.modelling import BuildingGeometry, Surface, Window


def _zone_order(bg: BuildingGeometry) -> list[str]:
    if bg.zone_volumes:
        return [zv.zone for zv in bg.zone_volumes]
    return list(dict.fromkeys(bg.zones))


def _surface_sort_key(bg: BuildingGeometry):
    zone_idx = {zone: i for i, zone in enumerate(_zone_order(bg))}
    type_rank = {"Wall": 0, "Floor": 1, "Ceiling": 2, "Roof": 3}

    def piece_index(s: Surface) -> int:
        if s.stype == "Wall":
            m = re.search(r"_W(\d+)$", s.name)
            return int(m.group(1)) if m else 999999
        m = re.search(rf"_{re.escape(s.stype)}(\d*)$", s.name)
        if m:
            return int(m.group(1) or "1")
        return 999999

    return lambda s: (zone_idx.get(s.zone, 999999), type_rank.get(s.stype, 9), piece_index(s), s.name)


def _window_sort_key(bg: BuildingGeometry):
    surf_idx = {s.name: i for i, s in enumerate(sorted(bg.surfaces, key=_surface_sort_key(bg)))}

    def key(w: Window) -> tuple:
        m = re.search(r"_Win(\d+)$", w.name)
        k = int(m.group(1)) if m else 999999
        return (surf_idx.get(w.parent, 999999), k, w.name)

    return key


GeometryContract = Literal["legacy", "c2_b5_v1"]


def _selected_contract(
    bg: BuildingGeometry,
    geometry_contract: GeometryContract | None,
) -> GeometryContract:
    selected = bg.geometry_contract if geometry_contract is None else geometry_contract
    if selected not in {"legacy", "c2_b5_v1"}:
        raise ValueError(f"unknown geometry_contract {selected!r}")
    if selected != bg.geometry_contract:
        raise ValueError(
            "serializer geometry_contract must match the built geometry contract"
        )
    return selected


def building_geometry_dict(
    bg: BuildingGeometry,
    *,
    geometry_contract: GeometryContract | None = None,
) -> dict:
    """Canonical JSON-able form of a BuildingGeometry — the single source of truth
    for the ``2_modelling/building_geometry.json`` artifact. The pipeline writer
    and the validator (validation_run.consistency check) both use this so the
    on-disk artifact can be reconciled against a deterministic rebuild."""
    contract = _selected_contract(bg, geometry_contract)
    zones = _zone_order(bg)
    payload = {
        "zones": zones,
        "zone_meta": [
            {"name": zv.zone, "role": zv.role, "cell_id": zv.cell_id, "fi": zv.fi}
            for zv in bg.zone_volumes
        ],
        "surfaces": [
            {
                "name": s.name,
                "zone": s.zone,
                "type": s.stype,
                "obc": s.obc,
                "obc_obj": s.obc_obj,
                "verts": [list(v) for v in s.verts],
            }
            for s in sorted(bg.surfaces, key=_surface_sort_key(bg))
        ],
        "windows": [],
    }
    for window in sorted(bg.windows, key=_window_sort_key(bg)):
        row = {
            "name": window.name,
            "parent": window.parent,
            "verts": [list(vertex) for vertex in window.verts],
        }
        if contract == "c2_b5_v1":
            if not all((
                window.source_window_id,
                window.facade_segment_id,
                window.host_resolution_sha256,
            )):
                raise ValueError("c2_b5_v1 windows require source/segment/host proof identity")
            row.update({
                "source_window": window.source_window_id,
                "facade_segment": window.facade_segment_id,
                "host_resolution_sha256": window.host_resolution_sha256,
            })
        payload["windows"].append(row)
    if contract == "c2_b5_v1":
        payload["geometry_contract"] = contract
    if bg.openings:
        from dataclasses import asdict

        payload["openings"] = [asdict(o) for o in sorted(bg.openings, key=lambda o: o.name)]
    return payload


def geometry_specs_markdown(
    zone_specs: str,
    surface_specs: str,
    fenestration_specs: str,
    *,
    geometry_contract: GeometryContract = "legacy",
) -> str:
    """Canonical text of the ``3_split_pairing/geometry_specs.md`` artifact."""
    if geometry_contract not in {"legacy", "c2_b5_v1"}:
        raise ValueError(f"unknown geometry_contract {geometry_contract!r}")
    return (
        f"# zone_specs\n\n{zone_specs}\n\n# surface_specs\n\n{surface_specs}\n\n"
        f"# fenestration_specs\n\n{fenestration_specs}\n"
    )


def building_geometry_json(
    bg: BuildingGeometry,
    *,
    indent: int = 2,
    geometry_contract: GeometryContract | None = None,
) -> str:
    return json.dumps(
        building_geometry_dict(bg, geometry_contract=geometry_contract),
        indent=indent,
        ensure_ascii=False,
    )

# Fixed construction vocabulary — the seam between the geometry serializer and
# the 4_MEP author. Both reference these exact names.
CONSTRUCTION_VOCAB = {
    "ext_wall": "Default_Ext_Wall",
    "int_wall": "Default_Int_Wall",
    "ground_wall": "Default_GroundWall",  # below-grade wall (not emitted by the
                                          # current kernel; defensive)
    "ground_floor": "Default_GroundFloor",
    "ext_floor": "Default_ExtFloor",   # exposed underside (cantilever); rare
    "roof": "Default_Roof",
    "interfloor": "Cons_InterFloor",
    "window": "Default_Window",
}


def _construction_for(s: Surface) -> str:
    if s.stype == "Wall":
        if s.obc == "Outdoors":
            return CONSTRUCTION_VOCAB["ext_wall"]
        if s.obc == "Ground":
            return CONSTRUCTION_VOCAB["ground_wall"]
        return CONSTRUCTION_VOCAB["int_wall"]
    if s.stype == "Floor":
        if s.obc == "Ground":
            return CONSTRUCTION_VOCAB["ground_floor"]
        if s.obc == "Surface":
            return CONSTRUCTION_VOCAB["interfloor"]
        return CONSTRUCTION_VOCAB["ext_floor"]  # Outdoors underside
    if s.stype == "Ceiling":
        return CONSTRUCTION_VOCAB["interfloor"]
    if s.stype == "Roof":
        return CONSTRUCTION_VOCAB["roof"]
    return CONSTRUCTION_VOCAB["ext_wall"]


def _kind_label(s: Surface) -> str:
    if s.stype == "Wall":
        return {"Outdoors": "exterior", "Ground": "ground"}.get(s.obc, "interior")
    if s.stype == "Floor":
        return {"Ground": "ground", "Surface": "interzone"}.get(s.obc, "exterior")
    if s.stype == "Ceiling":
        return "interzone"
    return "roof"


def _fmt_verts(verts) -> str:
    return "-".join(f"({v[0]:.2f},{v[1]:.2f},{v[2]:.2f})" for v in verts)


def _xy_range(bg_zone_volume) -> tuple[float, float, float, float]:
    minx, miny, maxx, maxy = bg_zone_volume.polygon.bounds
    return minx, miny, maxx, maxy


# Frame-label text (E4-output-contract spec v2 §6.2 item 1). The kernel never
# rotates/translates a single vertex either way — this only changes the prose
# a downstream LLM agent reads, so a v3/E4 (Relative) run doesn't call these
# "world coordinates" (which they no longer are, once Zone frame is all-zero
# and Building.North Axis carries theta) while a legacy v1/v2 (World) run's
# text and bytes stay pixel-for-pixel identical to pre-E4.
_FRAME_LABEL_TEXT = {
    "world": "world coordinates",
    "building_axis": "building-axis coordinates; values are absolute within the project building frame",
}
_FRAME_LABEL_SURFACE_TEXT = {
    "world": "absolute world coordinates",
    "building_axis": "absolute building-axis coordinates (values are absolute within the project building frame)",
}


def serialize_geometry(
    bg: BuildingGeometry,
    *,
    frame_label: str = "world",
    geometry_contract: GeometryContract | None = None,
) -> tuple[str, str, str, set[str]]:
    """Return (zone_specs, surface_specs, fenestration_specs, used_constructions).

    ``frame_label``: ``"world"`` (default — byte-identical to pre-E4 text) or
    ``"building_axis"`` (v3/E4 Relative runs). Selects prose only; the
    vertex/coordinate VALUES emitted below are identical either way — this
    function never rotates or translates geometry.
    """
    from src.agent.geometry.openings import require_supported_ep_openings

    require_supported_ep_openings(bg)
    if frame_label not in _FRAME_LABEL_TEXT:
        raise ValueError(f"serialize_geometry: unknown frame_label {frame_label!r}")
    contract = _selected_contract(bg, geometry_contract)
    frame_words = _FRAME_LABEL_TEXT[frame_label]
    surf_by_name = {s.name: s for s in bg.surfaces}

    # ---- zone_specs ----
    z_lines = [
        f"Zones ({frame_words}, meters, two decimals). Every zone name below "
        "is referenced literally by surface_specs / fenestration_specs / "
        "people_specs / lights_specs / hvac_specs.",
    ]
    by_fi: dict[int, list] = {}
    for zv in sorted(bg.zone_volumes, key=lambda z: (_zone_order(bg).index(z.zone), z.zone)):
        by_fi.setdefault(zv.fi, []).append(zv)
    for fi in sorted(by_fi):
        zvs = by_fi[fi]
        zf = zvs[0].zf
        zt = zvs[0].zt
        z_lines.append(f"\nFloor {fi + 1} (z {zf:.2f} to {zt:.2f}):")
        for zv in zvs:
            minx, miny, maxx, maxy = _xy_range(zv)
            z_lines.append(
                f"- {zv.zone}: x[{minx:.2f},{maxx:.2f}], y[{miny:.2f},{maxy:.2f}], "
                f"z_floor={zv.zf:.2f}, ceiling_height={zv.zt - zv.zf:.2f}, "
                f"role: {zv.role}."
            )
    zone_specs = "\n".join(z_lines)

    # ---- surface_specs ----
    used: set[str] = set()
    s_lines = [
        f"Surfaces (vertices CCW from outside, {_FRAME_LABEL_SURFACE_TEXT[frame_label]} in "
        "meters). Construction names and adjacent zone names are authoritative — "
        "transcribe them verbatim. Interzone faces are pre-paired: the named "
        "adjacent surface is its reciprocal partner.",
    ]
    surfaces_by_zone: dict[str, list[Surface]] = {}
    ordered_surfaces = sorted(bg.surfaces, key=_surface_sort_key(bg))
    for s in ordered_surfaces:
        surfaces_by_zone.setdefault(s.zone, []).append(s)
    # keep zone order stable (zone_volumes order)
    zone_order = _zone_order(bg)
    for zone in zone_order:
        if zone not in surfaces_by_zone:
            continue
        s_lines.append(f"\n**{zone}**:")
        for s in surfaces_by_zone[zone]:
            cons = _construction_for(s)
            used.add(cons)
            kind = _kind_label(s)
            extra = ""
            if s.obc == "Surface":
                if s.obc_obj in surf_by_name:
                    partner = surf_by_name[s.obc_obj]
                    extra = (
                        f", adjacent_zone={partner.zone}, "
                        f"adjacent_surface={s.obc_obj}"
                    )
                else:
                    # kernel pairing should always set a resolvable partner;
                    # surface this loudly rather than dropping adjacency silently
                    extra = ", adjacent_surface=UNRESOLVED"
            s_lines.append(
                f"- {s.name} ({kind} {s.stype.lower()}, {cons}{extra}): "
                f"{_fmt_verts(s.verts)}"
            )
    surface_specs = "\n".join(s_lines)

    # ---- fenestration_specs ----
    if not bg.windows:
        # Be explicit: an empty list is "zero windows", NOT "decide for yourself".
        # A bare header let the downstream agent invent windows (sm21 e2e: 24
        # hallucinated). State it unambiguously so it creates none.
        fenestration_specs = (
            "This model has NO windows. The geometry contains zero fenestration "
            "surfaces. Do NOT create any FenestrationSurface:Detailed objects and "
            "do NOT invent windows on any facade."
        )
        return zone_specs, surface_specs, fenestration_specs, used

    f_lines = [
        "Windows are FenestrationSurface:Detailed, vertices CCW from outside, "
        f"Construction={CONSTRUCTION_VOCAB['window']}. parent is the exterior "
        "wall surface name (transcribe verbatim). Create EXACTLY the windows "
        "listed below — no more, no fewer.",
    ]
    used.add(CONSTRUCTION_VOCAB["window"])
    for w in sorted(bg.windows, key=_window_sort_key(bg)):
        zs = [v[2] for v in w.verts]
        identity = ""
        if contract == "c2_b5_v1":
            if not all((
                w.source_window_id,
                w.facade_segment_id,
                w.host_resolution_sha256,
            )):
                raise ValueError("c2_b5_v1 fenestration specs require proof identity")
            identity = (
                f"source_window={w.source_window_id}, "
                f"segment={w.facade_segment_id}, "
                f"host_proof={w.host_resolution_sha256}, "
            )
        f_lines.append(
            f"- {w.name}: parent={w.parent}, "
            f"{identity}"
            f"Construction={CONSTRUCTION_VOCAB['window']}, "
            f"z={min(zs):.2f}-{max(zs):.2f}, vertices: {_fmt_verts(w.verts)}"
        )
    fenestration_specs = "\n".join(f_lines)

    return zone_specs, surface_specs, fenestration_specs, used


def check_geometry_specs_consistency(
    bg: BuildingGeometry,
    *,
    building_geometry: dict,
    geometry_specs: str,
    frame_label: str = "world",
    geometry_contract: GeometryContract | None = None,
) -> None:
    """Fail closed unless both downstream artifacts equal a fresh built projection."""
    contract = _selected_contract(bg, geometry_contract)
    expected_building = building_geometry_dict(bg, geometry_contract=contract)
    zone_specs, surface_specs, fenestration_specs, _used = serialize_geometry(
        bg,
        frame_label=frame_label,
        geometry_contract=contract,
    )
    expected_specs = geometry_specs_markdown(
        zone_specs,
        surface_specs,
        fenestration_specs,
        geometry_contract=contract,
    )
    issues = []
    if building_geometry != expected_building:
        issues.append("building_geometry")
    if geometry_specs != expected_specs:
        issues.append("geometry_specs")
    if issues:
        raise ValueError(
            "geometry artifact consistency failed for: " + ", ".join(issues)
        )
