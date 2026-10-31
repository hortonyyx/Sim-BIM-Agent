"""EnergyPlus branch of a frozen source BIM, with independent physics inputs.

No correction, drawing, or historical model geometry is read by this adapter.
The adapter keeps one thermal zone per source space and supports exterior
windows plus explicitly closed doors. Open passages are never silently sealed.
"""
from __future__ import annotations

from hashlib import sha256
from io import StringIO
import json
from pathlib import Path

import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union

from src.agent.geometry.modelling import BuildingGeometry, NameRegistry, Window, ZoneVolume, _newell
from src.agent.geometry.source_model import _digest
from src.agent.geometry.split_pairing import pair_surfaces

# The reused pairer trims exterior segment ends by 1 micrometre. This is an
# explicit backend numerical tolerance, not permission to regularize source BIM.
TOL = 2e-6


def _project(vertices, reference):
    pts = np.asarray(reference, dtype=float)
    normal = _newell(reference)
    if not np.isfinite(pts).all() or np.linalg.norm(normal) < .99:
        raise ValueError("invalid source boundary plane")
    incoming = np.asarray(vertices, dtype=float)
    if not np.isfinite(incoming).all():
        raise ValueError("non-finite geometry")
    if np.max(np.abs((incoming - pts[0]) @ normal)) > TOL:
        return None
    axis = int(np.argmax(np.abs(normal)))
    return Polygon(np.delete(incoming, axis, axis=1))


def _covers(parent, child):
    return child is not None and child.is_valid and child.area > 0 and parent.buffer(TOL).covers(child)


def derive_ep_geometry(source: dict, zone_names: dict[str, str], *, opening_policy: dict | None = None):
    """Return derived geometry and audited many-to-one source mappings."""
    if source.get("schema_version") != "source_bim_v2":
        raise ValueError("EP branch requires source_bim_v2")
    if source.get("source_model_sha256") != _digest({k: v for k, v in source.items() if k != "source_model_sha256"}):
        raise ValueError("source BIM digest mismatch")
    from src.agent.geometry.opening_segments import expand_opening_policy
    opening_policy = expand_opening_policy(source, opening_policy)
    if source.get("validation", {}).get("status") != "pass" or any(source.get(k) for k in ("unbuilt_openings", "unsupported")):
        raise ValueError("source BIM has unresolved geometry; EP branch blocked")
    if source["coordinate_system"]["units"] != "m" or source["coordinate_system"]["up_axis"] != "Z":
        raise ValueError("EP branch requires metres and Z-up")
    if any(o["kind"] not in {"window", "door", "open"} for o in source["openings"]):
        raise ValueError("unsupported source opening kind")
    if any(o["kind"] == "window" and not o["exterior"] for o in source["openings"]):
        raise ValueError("EP branch currently supports exterior windows only")
    spaces = {s["id"]: s for s in source["spaces"]}
    if not spaces or len(spaces) != len(source["spaces"]) or set(zone_names) != set(spaces):
        raise ValueError("physics zone bindings must cover every source space exactly once")
    if any(not isinstance(n, str) or not n.strip() or any(c in n for c in ',;!\n\r') for n in zone_names.values()) or len({n.casefold() for n in zone_names.values()}) != len(spaces):
        raise ValueError("EP zone names must be nonempty and unique (case insensitive)")
    floors = sorted(source["floors"], key=lambda f: f["z_floor"])
    if len({f["id"] for f in floors}) != len(floors):
        raise ValueError("duplicate source floors")
    floor_index = {f["id"]: i for i, f in enumerate(floors)}
    volumes = []
    for sid, space in spaces.items():
        if space.get('vertical_sections'):
            raise ValueError('EP adapter does not yet support source spaces with vertical sections; no prism approximation is allowed')
        if space.get('holes'):
            raise ValueError('EP adapter does not yet support source spaces with holes')
        fi = floor_index[space["floor_id"]]
        floor = floors[fi]
        if abs(space["z_floor"]-floor["z_floor"]) > 1e-7 or abs(space["height"]-floor["height"]) > 1e-7 or space["height"] <= 0:
            raise ValueError("EP adapter requires a common positive height within each floor")
        poly = Polygon(space["polygon"])
        if not poly.is_valid or poly.area <= 0 or not np.isfinite(np.asarray(space["polygon"])).all():
            raise ValueError(f"invalid source space {sid}")
        ring = list(poly.exterior.coords)
        if any(abs(a[0]-b[0]) > TOL and abs(a[1]-b[1]) > TOL for a,b in zip(ring, ring[1:])):
            raise ValueError("EP adapter currently requires orthogonal source polygons")
        volumes.append(ZoneVolume(zone_names[sid], sid, poly, space["z_floor"],
                                  space["z_floor"]+space["height"], fi,
                                  role=space["role"], handle=f"EP{len(volumes)+1:03d}"))
    for i, a in enumerate(volumes):
        for b in volumes[i+1:]:
            if min(a.zt,b.zt)-max(a.zf,b.zf) > TOL and a.polygon.intersection(b.polygon).area > TOL**2:
                raise ValueError("overlapping source spaces")
    bg = BuildingGeometry(zones=[v.zone for v in volumes], zone_volumes=volumes)
    bg.surfaces = pair_surfaces(volumes, NameRegistry(), capability_profile="orthogonal_polygon")
    boundaries = {b["id"]: b for b in source["boundaries"]}
    if len(boundaries) != len(source["boundaries"]):
        raise ValueError("duplicate source boundary identities")
    source_by_zone = {n: sid for sid,n in zone_names.items()}
    mapping, pieces = {}, {bid: [] for bid in boundaries}
    for surface in bg.surfaces:
        kind = {"Wall": "wall", "Floor": "floor", "Roof": "ceiling", "Ceiling": "ceiling"}[surface.stype]
        matches = []
        for bid, b in boundaries.items():
            if b["space_id"] != source_by_zone[surface.zone] or b["geometry_type"] != kind:
                continue
            parent = _project(b["vertices"], b["vertices"])
            child = _project(surface.verts, b["vertices"])
            if b["kind"] == "physical" and parent.is_valid and _covers(parent, child) and _newell(surface.verts) @ _newell(b["vertices"]) > .999:
                matches.append((bid, child))
        if len(matches) != 1:
            raise ValueError(f"derived face {surface.name} does not have exactly one source boundary")
        bid, child = matches[0]
        mapping[surface.name] = bid
        pieces[bid].append(child)
    for bid, b in boundaries.items():
        parent = _project(b["vertices"], b["vertices"])
        union = unary_union(pieces[bid])
        if (not pieces[bid] or not parent.is_valid or not union.buffer(TOL).covers(parent)
                or sum(p.area for p in pieces[bid])-union.area > TOL**2):
            raise ValueError(f"incomplete or duplicated derived coverage of source boundary {bid}")
    # Reconcile actual contacts, including both area and location. This also
    # catches a pairer threshold silently discarding a small source contact.
    relations = {}
    for rel in source["boundary_relations"]:
        key = tuple(sorted(rel["boundary_ids"]))
        if len(key) != 2 or key in relations or any(k not in boundaries for k in key):
            raise ValueError("invalid or duplicate source contact")
        relations[key] = rel
    paired = {}
    by_name = {s.name: s for s in bg.surfaces}
    for surface in bg.surfaces:
        if surface.obc != "Surface":
            continue
        other = by_name[surface.obc_obj]
        key = tuple(sorted([mapping[surface.name], mapping[other.name]]))
        if key not in relations:
            raise ValueError("EP pairing introduces a contact absent from source BIM")
        if mapping[surface.name] == key[0]:
            paired.setdefault(key, []).append(_project(surface.verts, boundaries[key[0]]["vertices"]))
    for key, rel in relations.items():
        a,b = [boundaries[k] for k in key]
        if set(rel["space_ids"]) != {a["space_id"], b["space_id"]}:
            raise ValueError("source contact space identities disagree")
        reference = a["vertices"]
        regions = []
        for region in rel["regions"]:
            p = _project(region["vertices"], reference)
            if p is None:
                raise ValueError("source contact is not coplanar")
            for hole in region.get("holes", []):
                p = p.difference(_project(hole, reference))
            regions.append(p)
        expected = unary_union(regions)
        actual = unary_union(paired.get(key, []))
        if expected.is_empty or not actual.buffer(TOL).covers(expected) or not expected.buffer(TOL).covers(actual):
            raise ValueError("EP contact coverage differs from source BIM")
    for bid,b in boundaries.items():
        counterparts = {other for key in relations if bid in key for other in key if other != bid}
        if set(b["counterpart_ids"]) != counterparts or set(b["adjacent_space_ids"]) != {boundaries[k]["space_id"] for k in counterparts}:
            raise ValueError("source boundary adjacency disagrees with contact regions")
    windows = {}
    apertures = {}
    for opening in source["openings"]:
        if opening["kind"] != "window":
            continue
        bid = opening["host_boundary_id"]
        if (opening["id"] in windows or source["opening_hosts"].get(opening["id"]) != [bid]
                or opening["space_ids"] != [boundaries[bid]["space_id"]]):
            raise ValueError("source window identity/host disagreement")
        hosts = [s for s in bg.surfaces if mapping[s.name] == bid and s.obc == "Outdoors"
                 and _covers(_project(s.verts, s.verts), _project(opening["vertices"], s.verts))]
        if len(hosts) != 1:
            raise ValueError(f"source window {opening['id']} cannot be preserved on one EP exterior face")
        aperture = _project(opening["vertices"], boundaries[bid]["vertices"])
        if any(aperture.intersection(p).area > TOL**2 for p in apertures.get(bid, [])):
            raise ValueError("overlapping source windows")
        apertures.setdefault(bid, []).append(aperture)
        name = f"EP_Window_{len(windows)+1:03d}"
        bg.windows.append(Window(name, hosts[0].name, [tuple(v) for v in opening["vertices"]], source_window_id=opening["id"]))
        windows[opening["id"]] = name
    from src.agent.execution.ep_openings import derive_ep_doors
    bg.openings, doors = derive_ep_doors(source, bg, mapping, {} if opening_policy is None else opening_policy)
    return bg, {"source_model_sha256": source["source_model_sha256"],
                "zones": zone_names, "surfaces": mapping, "windows": windows,
                "doors": doors,
                "assumptions": ["one thermal zone per source space", "lowest source floor is ground-contact"],
                "coverage_tolerance_m": TOL}


# Initial collaborator interface: deliberately bounded to the proven ideal-loads
# template. Unsupported objects fail explicitly, especially embedded geometry.
PHYSICS_OBJECTS = frozenset({
    "VERSION", "SIMULATIONCONTROL", "BUILDING", "SITE:LOCATION", "RUNPERIOD", "TIMESTEP",
    "SCHEDULETYPELIMITS", "SCHEDULE:COMPACT", "SCHEDULE:CONSTANT", "MATERIAL", "MATERIAL:NOMASS",
    "MATERIAL:AIRGAP", "WINDOWMATERIAL:SIMPLEGLAZINGSYSTEM", "CONSTRUCTION", "PEOPLE", "LIGHTS",
    "ELECTRICEQUIPMENT", "HVACTEMPLATE:THERMOSTAT", "HVACTEMPLATE:ZONE:IDEALLOADSAIRSYSTEM",
    "OUTPUT:VARIABLEDICTIONARY", "OUTPUT:TABLE:SUMMARYREPORTS", "OUTPUTCONTROL:TABLE:STYLE",
    "OUTPUT:VARIABLE", "OUTPUT:DIAGNOSTICS", "OUTPUT:METER", "SITE:GROUNDTEMPERATURE:BUILDINGSURFACE",
})


def assemble_idf(bg, physics_text: str, source: dict, *, door_audit: dict | None = None):
    from eppy.modeleditor import IDF
    from src.agent._share import ensure_schema_initialized
    from src.agent.geometry.specs import _construction_for
    from src.agent.geometry.to_idf import building_to_idf

    ensure_schema_initialized()
    idf = IDF(StringIO(physics_text))
    unsupported = [key for key, objs in idf.idfobjects.items() if objs and key not in PHYSICS_OBJECTS]
    if unsupported:
        raise ValueError(f"physics template must be geometry-free; unsupported objects: {unsupported}")
    if len(idf.idfobjects["BUILDING"]) != 1:
        raise ValueError("physics template needs exactly one BUILDING")
    # Same building-axis convention as output_coordinates.py, but orientation
    # comes from the frozen source BIM, never an accepted-correction reload.
    north = source["coordinate_system"].get("north_axis")
    if north is not None:
        from src.agent.correction.schema import NorthAxisEvidence
        idf.idfobjects["BUILDING"][0].North_Axis = NorthAxisEvidence.model_validate(north).value_deg
    zone_set = {n.casefold() for n in bg.zones}
    for key in ("PEOPLE", "LIGHTS", "ELECTRICEQUIPMENT", "HVACTEMPLATE:ZONE:IDEALLOADSAIRSYSTEM"):
        for obj in idf.idfobjects[key]:
            field = "Zone_Name" if key.startswith("HVACTEMPLATE") else "Zone_or_ZoneList_or_Space_or_SpaceList_Name"
            if str(getattr(obj, field)).casefold() not in zone_set:
                raise ValueError(f"physics {key} references a zone absent from source bindings")
    systems = idf.idfobjects["HVACTEMPLATE:ZONE:IDEALLOADSAIRSYSTEM"]
    if len(systems) != len(zone_set) or {o.Zone_Name.casefold() for o in systems} != zone_set:
        raise ValueError("initial EP adapter requires exactly one ideal-loads system per source zone")
    constructions = {o.Name.casefold() for o in idf.idfobjects["CONSTRUCTION"]}
    # Parent-face/window projection only; doors are added by the explicit
    # backend adapter below, so the legacy aperture guard is not a bypass.
    geo = building_to_idf(bg, boundary_validation_only=True)
    by_name = {s.name:s for s in bg.surfaces}
    for obj in geo.idfobjects["BUILDINGSURFACE:DETAILED"]:
        obj.Construction_Name = _construction_for(by_name[obj.Name])
    for obj in geo.idfobjects["FENESTRATIONSURFACE:DETAILED"]:
        obj.Construction_Name = "Default_Window"
    for key in ("BUILDINGSURFACE:DETAILED", "FENESTRATIONSURFACE:DETAILED"):
        if any(o.Construction_Name.casefold() not in constructions for o in geo.idfobjects[key]):
            raise ValueError("physics template missing required surface construction")
    idf.newidfobject("GLOBALGEOMETRYRULES", Starting_Vertex_Position="UpperLeftCorner",
                     Vertex_Entry_Direction="Counterclockwise", Coordinate_System="Relative")
    for key in ("ZONE", "BUILDINGSURFACE:DETAILED", "FENESTRATIONSURFACE:DETAILED"):
        for obj in geo.idfobjects[key]:
            idf.copyidfobject(obj)
    from src.agent.execution.ep_openings import write_ep_doors
    write_ep_doors(idf, bg.openings, door_audit if door_audit is not None else
                   {"source_doors": 0, "door_surfaces": 0, "doors": {}})
    return idf


def export_ep_branch(source_path: Path, physics_path: Path, bindings_path: Path, out_dir: Path, *, epw: Path | None = None,
                     opening_policy_path: Path | None = None):
    """Create an immutable branch run. Failure evidence survives in report.json."""
    from src.agent.geometry.specs import building_geometry_dict
    from src.validator.checks.kernel import check_kernel
    from src.validator.interzone import validate_interzone_surface_pairs

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": "ep_branch_v1", "status": "failed", "simulation": {"status": "not_run"},
              "drawing_fidelity": "not_evaluated", "source_geometry_input": str(source_path)}
    def save(name, value):
        (out_dir/name).write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    try:
        raw = Path(source_path).read_bytes()
        source = json.loads(raw)
        report["source_evidence_conflicts"] = source.get("conflicts", [])
        physics_raw = Path(physics_path).read_bytes()
        bindings_raw = Path(bindings_path).read_bytes()
        (out_dir/"source_model.json").write_bytes(raw)
        (out_dir/"physics.idf").write_bytes(physics_raw)
        (out_dir/"zone_bindings.json").write_bytes(bindings_raw)
        report["inputs"] = {"source_file_sha256": sha256(raw).hexdigest(),
                            "source_model_sha256": source.get("source_model_sha256"),
                            "physics_sha256": sha256(physics_raw).hexdigest(),
                            "zone_bindings_sha256": sha256(bindings_raw).hexdigest()}
        opening_policy = None
        if opening_policy_path is not None:
            policy_raw = Path(opening_policy_path).read_bytes()
            opening_policy = json.loads(policy_raw)
            (out_dir/"opening_policy.json").write_bytes(policy_raw)
            report["inputs"]["opening_policy_sha256"] = sha256(policy_raw).hexdigest()
        bg, mapping = derive_ep_geometry(source, json.loads(bindings_raw), opening_policy=opening_policy)
        checks = check_kernel(bg, capability_profile="orthogonal_polygon")
        save("geometry_checks.json", checks.model_dump(mode="json"))
        if checks.blocking():
            raise ValueError("EP derived geometry failed kernel checks")
        idf = assemble_idf(bg, physics_raw.decode("utf-8"), source, door_audit=mapping["doors"])
        issues = validate_interzone_surface_pairs(idf)
        save("idf_pair_checks.json", {"issues": issues})
        if issues:
            raise ValueError("assembled IDF failed interzone checks")
        mapping["coordinates"] = {"frame": "building axes", "idf_coordinate_system": "Relative",
                                  "zone_origins_and_rotation": "all_zero",
                                  "north_axis_deg": idf.idfobjects["BUILDING"][0].North_Axis,
                                  "north_source": source["coordinate_system"].get("north_axis"),
                                  "idf_vertex_rounding_decimal_places": 4}
        if source["coordinate_system"].get("north_axis") is None:
            mapping["assumptions"].append(f"source north unknown; EP physics BUILDING North_Axis={idf.idfobjects['BUILDING'][0].North_Axis}")
        save("source_mapping.json", mapping)
        save("building_geometry.json", building_geometry_dict(bg))
        idf.saveas(str(out_dir/"model.idf"))
        report["counts"] = {"zones": len(bg.zones), "surfaces": len(bg.surfaces), "windows": len(bg.windows)}
        report["counts"].update(source_doors=len({o.source_opening_id for o in bg.openings}), door_surfaces=len(bg.openings))
        report["status"] = "exported"
        if epw is not None:
            from src.runner.runner import EnergyPlusRunner, read_ep_end
            report["inputs"]["weather_sha256"] = sha256(Path(epw).read_bytes()).hexdigest()
            report["weather"] = str(epw)
            save("report.json", report)
            ok = EnergyPlusRunner(idf=idf).run_idf(epw_file_path=epw, idf_file_path=out_dir/"model.idf", output_directory=out_dir/"EP")
            end = read_ep_end(out_dir/"EP")
            report["simulation"] = {"status": "passed" if ok and end and end["completed"] and end["severe"] == 0 else "failed", "end": end}
            err_path = out_dir/"EP/eplusout.err"
            if err_path.is_file():
                report["simulation"]["warning_messages"] = [line.strip() for line in err_path.read_text(errors="replace").splitlines() if "** Warning **" in line]
            report["status"] = "passed" if report["simulation"]["status"] == "passed" else "failed"
        report["source_file_unchanged"] = Path(source_path).read_bytes() == raw
        if not report["source_file_unchanged"]:
            raise ValueError("source BIM changed during EP branch execution")
    except Exception as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    save("report.json", report)
    return report
