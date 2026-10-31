"""S4 — 4_mep deterministic check adapter (M2c, gate ①).

4_mep OWNS the entire MEP reference graph + object semantics (contracts §0.4 #7,
§1 5 attribution): everything is checked here, off the SINGLE unified parse
(idf_fragments.py) — no check writes its own regex. 5_intakeoutput only re-runs a
backstop.

Reference graph (two graphs, block):
  - ``geometry → construction → material``: every used construction is defined;
    every construction layer is a defined material.
  - ``people/lights/hvac → zone / schedule``: per-zone loads reference an existing
    zone and a defined schedule.

Object semantics (block):
  - ``WindowMaterial:SimpleGlazingSystem`` must be a STANDALONE construction layer
    (single-layer construction — the sm_16 NaN-fatal class).
  - ``Material:NoMass`` thermal resistance must be positive.
  - ``Schedule:Compact`` must reference a defined ``ScheduleTypeLimits``.
  - ``Schedule:Compact`` day-type completeness (reuses validator/schedules.py).

Reasonability bands (LPD / people density / setpoints / U-value) are a FLAG
placeholder — filled in once MEP input is richer (§5.2). A parse failure is an
ERROR (fail-closed → block + resample).
"""

from __future__ import annotations

import re

from src.validator.checks.schema import CheckLayer, CheckReport, CheckStatus, RunProfile
from src.validator.idf_fragments import IdfFragmentIndex, parse_mep_fragments
from src.validator.schedules import validate_schedule_completeness

# Object types whose names can appear as a Construction layer.
_MATERIAL_TYPES = (
    "MATERIAL", "MATERIAL:NOMASS", "MATERIAL:AIRGAP",
    "WINDOWMATERIAL:SIMPLEGLAZINGSYSTEM", "WINDOWMATERIAL:GLAZING",
    "WINDOWMATERIAL:GAS", "WINDOWMATERIAL:BLIND", "MATERIAL:INFRAREDTRANSPARENT",
)
_WINDOW_MATERIAL_TYPES = tuple(t for t in _MATERIAL_TYPES if t.startswith("WINDOWMATERIAL:"))
_LOAD_TYPES = ("PEOPLE", "LIGHTS", "ELECTRICEQUIPMENT")
# IDD People A4 "Number of People Calculation Method" — the ONLY legal enum keys
# (Energy+.idd People group \key lines). OpenStudio-style keys like
# ZoneFloorAreaPerPerson / FloorAreaPerPerson are NOT legal EnergyPlus IDD values
# and EnergyPlus rejects them even when the field position is otherwise correct.
_PEOPLE_CALC_METHODS = ("People", "People/Area", "Area/Person")
_HVAC_SCHEDULE_REF_FIELDS = {
    "ZONECONTROL:THERMOSTAT": ("Control_Type_Schedule_Name",),
    "THERMOSTATSETPOINT:DUALSETPOINT": (
        "Heating_Setpoint_Temperature_Schedule_Name",
        "Cooling_Setpoint_Temperature_Schedule_Name",
    ),
    "THERMOSTATSETPOINT:SINGLEHEATING": ("Setpoint_Temperature_Schedule_Name",),
    "THERMOSTATSETPOINT:SINGLECOOLING": ("Setpoint_Temperature_Schedule_Name",),
    "THERMOSTATSETPOINT:SINGLEHEATINGORCOOLING": ("Setpoint_Temperature_Schedule_Name",),
    "ZONEHVAC:IDEALLOADSAIRSYSTEM": ("Availability_Schedule_Name",),
    "HVACTEMPLATE:ZONE:IDEALLOADSAIRSYSTEM": ("System_Availability_Schedule_Name",),
    "HVACTEMPLATE:THERMOSTAT": (
        "Heating_Setpoint_Schedule_Name",
        "Cooling_Setpoint_Schedule_Name",
    ),
}

# 2026-08-14 摊 B (user-ratified tiering): object types whose 4_mep lines are
# rendered by DETERMINISTIC CODE (摊 A's HVACTemplate:* generators, replacing the
# old model-authored ZoneControl:Thermostat / ZoneHVAC:IdealLoadsAirSystem) ⇒ a
# field misalignment there is a CODE BUG and must stop the run. Every other object
# type is model-authored ⇒ only reported (FLAG), never blocking, so this gate
# cannot change any historical run's blocking status. ⛔ 摊 A 接缝: if 摊 A changes
# which types it renders deterministically, THIS SET IS VOID — stop and report;
# do not guess a new set (dispatch §3).
_IDD_ALIGNMENT_BLOCK_TYPES = frozenset(
    {"HVACTEMPLATE:THERMOSTAT", "HVACTEMPLATE:ZONE:IDEALLOADSAIRSYSTEM"}
)
_NAME_CHARSET_TYPES = _MATERIAL_TYPES + ("CONSTRUCTION", "SCHEDULE:COMPACT", "SCHEDULETYPELIMITS")
_NAME_CHARSET_RE = re.compile(r"^[A-Za-z0-9_ -]+$")
_SITE_FIELD_ALIASES = {
    "latitude": ("latitude", "Latitude"),
    "longitude": ("longitude", "Longitude"),
    "time_zone": ("time_zone", "time zone", "timezone", "Time Zone", "TimeZone"),
    "elevation": ("elevation", "Elevation"),
}
_SITE_FIELD_TOLERANCES = {
    "latitude": 0.01,
    "longitude": 0.01,
    "time_zone": 0.1,
    "elevation": 1.0,
}
_TESTDATA_SITE_CONTAINERS = ("site_location", "Site Location", "site", "Site")
_PLACEHOLDER_PATTERNS = (
    ("TBD", re.compile(r"(?<![A-Za-z0-9_])tbd(?![A-Za-z0-9_])", re.IGNORECASE)),
    (
        "same as above",
        re.compile(r"(?<![A-Za-z0-9_])same\s+as\s+above(?![A-Za-z0-9_])", re.IGNORECASE),
    ),
    (
        "see above",
        re.compile(r"(?<![A-Za-z0-9_])see\s+above(?![A-Za-z0-9_])", re.IGNORECASE),
    ),
    ("etc.", re.compile(r"(?<![A-Za-z0-9_])etc\.(?![A-Za-z0-9_])", re.IGNORECASE)),
    ("ellipsis", re.compile(r"\.\.\.|…")),
    (
        "angle-placeholder",
        re.compile(
            r"<\s*[A-Za-z0-9_ -]*(?:placeholder|tbd|todo|insert|replace)[A-Za-z0-9_ -]*\s*>",
            re.IGNORECASE,
        ),
    ),
)


def check_mep(
    mep: dict | object,
    *,
    used_constructions: set[str] | None = None,
    zone_names: set[str] | None = None,
    geometry_idf: str = "",
    testdata: dict | None = None,
    capability_profile: str = "rectangular",
    run_profile: RunProfile = "exploratory",
) -> CheckReport:
    rep = CheckReport(
        stage="4_mep",
        capability_profile=capability_profile,
        run_profile=run_profile,
    )
    idx = parse_mep_fragments(mep, extra_idf=geometry_idf)
    if not idx.ok:
        rep.add("mep.idf_parse", CheckStatus.ERROR, CheckLayer.INVARIANT,
                message=f"MEP fragment parse failed: {idx.parse_error}")
        return rep  # fail-closed; nothing else can be trusted
    rep.add_pass("mep.idf_parse", CheckLayer.INVARIANT)

    _placeholder_ban(rep, mep, idx)
    _name_charset(rep, idx)
    _north_axis_placeholder(rep, mep)
    _site_matches_testdata(rep, mep, testdata)
    _construction_coverage(rep, idx, used_constructions)
    _construction_to_material(rep, idx)
    _construction_thermal_mass(rep, idx)
    _schedule_type_refs(rep, idx)
    _schedule_completeness(rep, idx)
    # IDD field-alignment findings feed the disease cross-references that
    # load_to_schedule / hvac_schedule_refs attach to their symptom offenders, so
    # they must be computed before those symptom checks run.
    idd_findings, idd_diseased = _idd_field_findings(idx)
    _idd_field_alignment(rep, idd_findings)
    _load_refs(rep, idx, zone_names, idd_diseased)
    _people_field_alignment(rep, idx)
    _hvac_schedule_refs(rep, idx, idd_diseased)
    _per_zone_coverage(rep, idx, zone_names)
    _simpleglazing_standalone(rep, idx)
    _nomass_positive_resistance(rep, idx)
    _reasonability_placeholder(rep)
    return rep


def _material_names(idx: IdfFragmentIndex) -> set[str]:
    return idx.has_name(*_MATERIAL_TYPES)


def _material_types_by_name(idx: IdfFragmentIndex) -> dict[str, set[str]]:
    by_name: dict[str, set[str]] = {}
    for obj in idx.of_type(*_MATERIAL_TYPES):
        by_name.setdefault(obj.name, set()).add(obj.obj_type)
    return by_name


def _construction_layers(obj) -> list[str]:
    # Strip trailing empties (eppy pads optional layer fields) before judging.
    layers = [str(layer or "").strip() for layer in obj.fields[1:]]
    while layers and not layers[-1]:
        layers.pop()
    return layers


def _placeholder_ban(rep: CheckReport, mep: dict | object, idx: IdfFragmentIndex) -> None:
    offenders = []
    for obj in idx.objects:
        for i, value in enumerate(obj.fields):
            match = _placeholder_match(value)
            if match:
                offenders.append(
                    {
                        "surface": "parsed_idf_field",
                        "object_type": obj.obj_type,
                        "object": obj.name,
                        "field_index": i,
                        "token": match,
                        "value": value,
                    }
                )
    for path, value in _structured_string_values(mep):
        match = _placeholder_match(value)
        if match:
            offenders.append(
                {"surface": "structured_mep_field", "path": path, "token": match, "value": value}
            )
    evidence = {
        "scanned_surface": (
            "parsed IDF fragment field values plus structured building/site string fields"
        ),
        "guards": (
            "case-insensitive token/phrase patterns use non-word boundaries; ellipsis "
            "requires literal ... or …; angle placeholders require placeholder/tbd/todo/"
            "insert/replace text inside <>"
        ),
    }
    if offenders:
        rep.add_fail(
            "mep.placeholder_ban",
            CheckLayer.INVARIANT,
            f"{len(offenders)} MEP-authored field(s) contain placeholder/template prose",
            evidence=evidence | {"offenders": offenders},
        )
    else:
        rep.add_pass("mep.placeholder_ban", CheckLayer.INVARIANT, evidence=evidence)


def _placeholder_match(value: object) -> str | None:
    text = str(value or "")
    for label, pattern in _PLACEHOLDER_PATTERNS:
        if pattern.search(text):
            return label
    return None


def _structured_string_values(mep: dict | object) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for parent_name in ("building", "site_location"):
        parent = _get_value(mep, parent_name)
        name = _get_value(parent, "name")
        if isinstance(name, str):
            out.append((f"{parent_name}.name", name))
    return out


def _get_value(obj: object, key: str) -> object:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _name_charset(rep: CheckReport, idx: IdfFragmentIndex) -> None:
    offenders = []
    for obj in idx.of_type(*_NAME_CHARSET_TYPES):
        if _NAME_CHARSET_RE.fullmatch(obj.name or ""):
            continue
        offenders.append(
            {
                "object_type": obj.obj_type,
                "name": obj.name,
                "illegal_chars": sorted({c for c in obj.name if not _name_char_allowed(c)}),
            }
        )
    evidence = {
        "allowed_charset": "letters, digits, underscore, hyphen, and space",
        "allowed_pattern": r"^[A-Za-z0-9_ -]+$",
        "scanned_object_types": sorted(set(_NAME_CHARSET_TYPES)),
    }
    if offenders:
        rep.add_fail(
            "mep.name_charset",
            CheckLayer.CROSS_CHECK,
            f"{len(offenders)} MEP-authored object name(s) contain non EP-safe characters",
            evidence=evidence | {"offenders": offenders},
        )
    else:
        rep.add_pass("mep.name_charset", CheckLayer.CROSS_CHECK, evidence=evidence)


def _name_char_allowed(char: str) -> bool:
    return char.isascii() and (char.isalnum() or char in {"_", "-", " "})


def _north_axis_placeholder(rep: CheckReport, mep: dict | object) -> None:
    """S4 (E4-output-contract spec v2 §4.1): 4_MEP's `building.north_axis` is a
    compatibility placeholder ONLY — the single authoritative owner of the
    final `Building.North Axis` is the accepted correction's orientation
    evidence (S5 override, §4.2). MEP may never author a real value here;
    `0.0` (the BuildingSchema default) is the only legal placeholder, for
    every schema_version/capability_profile, so a non-zero LLM guess is
    caught here rather than silently "corrected" downstream. `-0.0` compares
    equal to `0.0` in Python and is accepted."""
    building = _get_value(mep, "building")
    value = _get_value(building, "north_axis")
    evidence = {
        "field": "building.north_axis",
        "required_placeholder": 0.0,
        "observed": value,
        "owner": (
            "the accepted correction's NorthAxisEvidence (S5 unconditional override); "
            "4_mep may not author a real value"
        ),
    }
    is_numeric_zero = isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) == 0.0
    if not is_numeric_zero:
        rep.add_fail(
            "mep.building_north_axis_placeholder", CheckLayer.INVARIANT,
            f"building.north_axis must be the 0.0 compatibility placeholder, got {value!r}",
            evidence=evidence,
        )
    else:
        rep.add_pass("mep.building_north_axis_placeholder", CheckLayer.INVARIANT, evidence=evidence)


def _site_matches_testdata(
    rep: CheckReport, mep: dict | object, testdata: dict | None
) -> None:
    expected, schema_evidence = _extract_testdata_site(testdata)
    evidence = {
        **schema_evidence,
        "tolerances": _SITE_FIELD_TOLERANCES,
    }
    if not expected:
        rep.add(
            "mep.site_matches_testdata",
            CheckStatus.NOT_APPLICABLE,
            CheckLayer.CROSS_CHECK,
            message=(
                "no comparable structured site fields in testdata "
                "(latitude/longitude/time_zone/elevation)"
            ),
            evidence=evidence,
        )
        return

    actual = _extract_mep_site(mep)
    offenders = []
    for field, expected_value in expected.items():
        actual_value = actual.get(field)
        if actual_value is None:
            offenders.append({"field": field, "expected": expected_value, "actual": None})
            continue
        delta = abs(actual_value - expected_value)
        tolerance = _SITE_FIELD_TOLERANCES[field]
        if delta > tolerance:
            offenders.append(
                {
                    "field": field,
                    "expected": expected_value,
                    "actual": actual_value,
                    "delta": delta,
                    "tolerance": tolerance,
                }
            )
    evidence |= {
        "comparison": "real",
        "compared_fields": sorted(expected),
    }
    if offenders:
        rep.add_fail(
            "mep.site_matches_testdata",
            CheckLayer.CROSS_CHECK,
            f"{len(offenders)} MEP site field(s) differ from structured testdata",
            evidence=evidence | {"offenders": offenders},
        )
    else:
        rep.add_pass("mep.site_matches_testdata", CheckLayer.CROSS_CHECK, evidence=evidence)


def _extract_testdata_site(testdata: dict | None) -> tuple[dict[str, float], dict]:
    evidence = {
        "testdata_schema": "none supplied",
        "structured_site_fields": [],
        "location_string_fields": [],
    }
    if not isinstance(testdata, dict):
        return {}, evidence

    candidates = [testdata]
    for key in _TESTDATA_SITE_CONTAINERS:
        value = testdata.get(key)
        if isinstance(value, dict):
            candidates.insert(0, value)

    out: dict[str, float] = {}
    for candidate in candidates:
        for field, aliases in _SITE_FIELD_ALIASES.items():
            if field in out:
                continue
            value = _first_numeric(candidate, aliases)
            if value is not None:
                out[field] = value

    string_fields = [
        key
        for key in ("Building location", "building_location", "location", "Location")
        if isinstance(testdata.get(key), str)
    ]
    evidence = {
        "testdata_schema": (
            "searched top-level keys and nested site/site_location objects for numeric "
            "latitude/longitude/time_zone/elevation"
        ),
        "structured_site_fields": sorted(out),
        "location_string_fields": string_fields,
    }
    return out, evidence


def _extract_mep_site(mep: dict | object) -> dict[str, float]:
    site = _get_value(mep, "site_location")
    out: dict[str, float] = {}
    for field, aliases in _SITE_FIELD_ALIASES.items():
        value = _first_numeric(site, aliases)
        if value is not None:
            out[field] = value
    return out


def _first_numeric(obj: object, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _get_value(obj, key)
        if value is None:
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                continue
    return None


def _construction_coverage(
    rep: CheckReport, idx: IdfFragmentIndex, used: set[str] | None
) -> None:
    if not used:
        rep.add("mep.construction_coverage", CheckStatus.NOT_APPLICABLE,
                CheckLayer.INVARIANT, message="no used_constructions supplied")
        return
    defined = idx.has_name("CONSTRUCTION")
    missing = sorted(c for c in used if c not in defined)
    if missing:
        rep.add_fail("mep.construction_coverage", CheckLayer.INVARIANT,
                     f"{len(missing)} geometry-referenced construction(s) undefined",
                     evidence={"missing": missing})
    else:
        rep.add_pass("mep.construction_coverage", CheckLayer.INVARIANT,
                     evidence={"used": len(used)})


def _construction_to_material(rep: CheckReport, idx: IdfFragmentIndex) -> None:
    mats = _material_names(idx)
    bad = []
    for c in idx.of_type("CONSTRUCTION"):
        layers = _construction_layers(c)
        if not layers:
            bad.append({"construction": c.name, "reason": "no layers (empty construction)"})
            continue
        for layer in layers:
            if not str(layer).strip():
                bad.append({"construction": c.name, "reason": "blank layer field (gap)"})
            elif layer not in mats:
                bad.append({"construction": c.name, "missing_layer": layer})
    if bad:
        rep.add_fail("mep.construction_to_material", CheckLayer.INVARIANT,
                     f"{len(bad)} construction layer(s) reference undefined materials",
                     evidence={"offenders": bad})
    else:
        rep.add_pass("mep.construction_to_material", CheckLayer.INVARIANT)


def _construction_thermal_mass(rep: CheckReport, idx: IdfFragmentIndex) -> None:
    layer_types = _material_types_by_name(idx)
    bad = []
    skipped_fenestration = []
    checked = 0
    for c in idx.of_type("CONSTRUCTION"):
        layers = _construction_layers(c)
        resolved = [
            {"layer": layer, "types": sorted(layer_types.get(layer, set()))}
            for layer in layers
            if layer
        ]
        if any(
            obj_type in _WINDOW_MATERIAL_TYPES
            for item in resolved
            for obj_type in item["types"]
        ):
            skipped_fenestration.append(c.name)
            continue
        checked += 1
        has_mass_layer = any(layer_types.get(layer, set()) == {"MATERIAL"} for layer in layers)
        if not has_mass_layer:
            bad.append(
                {
                    "construction": c.name,
                    "layers": layers,
                    "resolved_layer_types": resolved,
                }
            )
    evidence = {
        "checked_opaque_constructions": checked,
        "skipped_fenestration_constructions": skipped_fenestration,
        "mass_layer_type": "MATERIAL",
        "non_mass_layer_types": [
            "MATERIAL:NOMASS",
            "MATERIAL:AIRGAP",
            "MATERIAL:INFRAREDTRANSPARENT",
        ],
        "out_of_scope_object_types": ["CONSTRUCTION:AIRBOUNDARY"],
    }
    if bad:
        rep.add_fail(
            "mep.construction_thermal_mass",
            CheckLayer.INVARIANT,
            f"{len(bad)} opaque construction(s) have no exact Material mass layer",
            evidence=evidence | {"offenders": bad},
        )
    else:
        rep.add_pass("mep.construction_thermal_mass", CheckLayer.INVARIANT, evidence=evidence)


def _schedule_type_refs(rep: CheckReport, idx: IdfFragmentIndex) -> None:
    types = idx.has_name("SCHEDULETYPELIMITS")
    bad = []
    for s in idx.of_type("SCHEDULE:COMPACT"):
        type_name = s.fields[1] if len(s.fields) > 1 else ""
        if type_name and type_name not in types:
            bad.append({"schedule": s.name, "missing_type": type_name})
    if bad:
        rep.add_fail("mep.schedule_type_refs", CheckLayer.INVARIANT,
                     f"{len(bad)} schedule(s) reference undefined ScheduleTypeLimits",
                     evidence={"offenders": bad})
    else:
        rep.add_pass("mep.schedule_type_refs", CheckLayer.INVARIANT)


def _schedule_completeness(rep: CheckReport, idx: IdfFragmentIndex) -> None:
    issues = validate_schedule_completeness(idx.idf)
    if issues:
        rep.add_fail("mep.schedule_completeness", CheckLayer.INVARIANT,
                     f"{len(issues)} incomplete Schedule:Compact day-type coverage",
                     evidence={"issues": issues})
    else:
        rep.add_pass("mep.schedule_completeness", CheckLayer.INVARIANT)


def _load_refs(
    rep: CheckReport,
    idx: IdfFragmentIndex,
    zone_names: set[str] | None,
    idd_diseased: dict | None = None,
) -> None:
    """People/Lights/Equipment → zone (field 1) and → schedule references."""
    sched_names = idx.has_name("SCHEDULE:COMPACT")
    zone_bad, sched_bad = [], []
    for obj in idx.of_type(*_LOAD_TYPES):
        zref = obj.fields[1] if len(obj.fields) > 1 else ""
        sref = obj.fields[2] if len(obj.fields) > 2 else ""
        if zone_names is not None and zref and zref not in zone_names:
            zone_bad.append({"object": obj.name, "zone_ref": zref})
        if sref and sref not in sched_names:
            off = {"object": obj.name, "schedule_ref": sref}
            dref = _disease_ref(idd_diseased, obj.obj_type, obj.name)
            if dref:
                off["disease_ref"] = dref
            sched_bad.append(off)
        if obj.obj_type == "PEOPLE":
            activity_ref = _people_activity_schedule_name(obj)
            if not activity_ref:
                off = {"object": obj.name, "activity_schedule_ref": "", "reason": "missing"}
                dref = _disease_ref(idd_diseased, obj.obj_type, obj.name)
                if dref:
                    off["disease_ref"] = dref
                sched_bad.append(off)
            elif activity_ref not in sched_names:
                off = {"object": obj.name, "activity_schedule_ref": activity_ref}
                dref = _disease_ref(idd_diseased, obj.obj_type, obj.name)
                if dref:
                    off["disease_ref"] = dref
                sched_bad.append(off)
    if zone_names is None:
        rep.add("mep.load_to_zone", CheckStatus.NOT_APPLICABLE, CheckLayer.INVARIANT,
                message="no zone list supplied")
    elif zone_bad:
        rep.add_fail("mep.load_to_zone", CheckLayer.INVARIANT,
                     f"{len(zone_bad)} load(s) reference an unknown zone",
                     evidence={"offenders": zone_bad})
    else:
        rep.add_pass("mep.load_to_zone", CheckLayer.INVARIANT)
    if sched_bad:
        evidence = {"offenders": sched_bad}
        diseased_count = sum(1 for o in sched_bad if "disease_ref" in o)
        if diseased_count:
            evidence["disease_cross_ref"] = (
                f"{diseased_count} offender(s) also flagged by mep.idd_field_alignment "
                f"(per-offender disease_ref names the missing IDD field) — a blank/"
                f"undefined schedule is usually the symptom of a positional shift, "
                f"not a missing schedule"
            )
        rep.add_fail("mep.load_to_schedule", CheckLayer.INVARIANT,
                     f"{len(sched_bad)} People/Lights schedule field(s) are blank or "
                     f"reference a name not defined in schedule_specs — a blank People "
                     f"activity-level schedule is usually a field-misalignment symptom, "
                     f"not a missing schedule (see mep.people_field_alignment)",
                     evidence=evidence)
    else:
        rep.add_pass("mep.load_to_schedule", CheckLayer.INVARIANT)


def _hvac_schedule_refs(
    rep: CheckReport, idx: IdfFragmentIndex, idd_diseased: dict | None = None
) -> None:
    sched_names = idx.has_name("SCHEDULE:COMPACT")
    bad = []
    checked = 0
    for obj_type, field_names in _HVAC_SCHEDULE_REF_FIELDS.items():
        for obj in idx.of_type(obj_type):
            for field_name in field_names:
                checked += 1
                schedule_ref = _raw_field_value(obj, field_name)
                if schedule_ref and schedule_ref not in sched_names:
                    off = {
                        "object_type": obj.obj_type,
                        "object": obj.name,
                        "field": field_name,
                        "schedule_ref": schedule_ref,
                    }
                    dref = _disease_ref(idd_diseased, obj.obj_type, obj.name)
                    if dref:
                        off["disease_ref"] = dref
                    bad.append(off)
    evidence = {
        "checked_fields": checked,
        "field_table": _HVAC_SCHEDULE_REF_FIELDS,
        "blank_reference_policy": "pass",
        "resolved_schedule_type": "SCHEDULE:COMPACT",
        "deferred_fields": [
            "ZoneHVAC:IdealLoadsAirSystem.Heating_Availability_Schedule_Name",
            "ZoneHVAC:IdealLoadsAirSystem.Cooling_Availability_Schedule_Name",
            "HVACTemplate:Zone:IdealLoadsAirSystem.Heating_Availability_Schedule_Name",
            "HVACTemplate:Zone:IdealLoadsAirSystem.Cooling_Availability_Schedule_Name",
        ],
    }
    if bad:
        diseased_count = sum(1 for o in bad if "disease_ref" in o)
        if diseased_count:
            evidence["disease_cross_ref"] = (
                f"{diseased_count} offender(s) also flagged by mep.idd_field_alignment "
                f"(per-offender disease_ref names the missing IDD field) — an undefined "
                f"schedule reference is often the symptom of a positional shift that "
                f"landed an object-type name (or other non-schedule value) in a schedule "
                f"slot, not a genuinely missing schedule"
            )
        rep.add_fail(
            "mep.hvac_schedule_refs",
            CheckLayer.INVARIANT,
            f"{len(bad)} HVAC schedule reference(s) are undefined",
            evidence=evidence | {"offenders": bad},
        )
    else:
        rep.add_pass("mep.hvac_schedule_refs", CheckLayer.INVARIANT, evidence=evidence)


def _raw_field_value(obj, field_name: str) -> str:
    raw = getattr(obj, "raw", None)
    if raw is not None:
        try:
            return str(getattr(raw, field_name, "") or "").strip()
        except Exception:  # noqa: BLE001 - missing/invalid raw fields count as blank
            return ""
    return ""


def _people_activity_schedule_name(obj) -> str:
    """People.Activity Level Schedule Name via eppy raw object, then field 9."""
    raw = getattr(obj, "raw", None)
    if raw is not None:
        try:
            return str(getattr(raw, "Activity_Level_Schedule_Name", "") or "").strip()
        except Exception:  # noqa: BLE001 - fall back to parsed field layout
            pass
    if len(obj.fields) > 9:
        return str(obj.fields[9] or "").strip()
    return ""


def _people_field_alignment(rep: CheckReport, idx: IdfFragmentIndex) -> None:
    """Catch People field misalignment directly (the disease), not its symptom.

    IDD puts ``Number of People Calculation Method`` at field A4 (a choice:
    People / People/Area / Area/Person) and the *required* ``Activity Level
    Schedule Name`` at A5 (the 10th field). When the two schedule names are
    authored back-to-back, the activity schedule lands in the A4 slot and every
    later field shifts one position — A5 ends up blank, the calculation method
    becomes a schedule name, and the numeric density fields absorb garbage (a
    real LLM product read as "10 people per m²" instead of "10 m² per person").

    ``mep.load_to_schedule`` only sees the *symptom* (A5 blank → "missing") and
    reads as if the schedule were never authored. This check sees the *disease*:
    A4 does not hold a legal calculation-method enum. It also distinguishes a
    true misalignment (A4 holds a defined schedule name) from a merely illegal
    calculation-method value (e.g. an OpenStudio-style key).
    """
    sched_names = idx.has_name("SCHEDULE:COMPACT")
    offenders = []
    for obj in idx.of_type("PEOPLE"):
        a4 = str(obj.fields[3]).strip() if len(obj.fields) > 3 else ""
        if a4 in _PEOPLE_CALC_METHODS:
            continue  # calc-method slot holds a legal enum → not misaligned here;
                      # a blank/undefined A5 is diagnosed by mep.load_to_schedule.
        a5 = _people_activity_schedule_name(obj)
        misplaced = bool(a4) and a4 in sched_names
        offenders.append(
            {
                "object": obj.name,
                "reason": (
                    "activity_schedule_misplaced_into_calc_method_slot"
                    if misplaced
                    else "illegal_calculation_method"
                ),
                "field_A4_Number_of_People_Calculation_Method": a4,
                "expected_A4_one_of": list(_PEOPLE_CALC_METHODS),
                "field_A5_Activity_Level_Schedule_Name": a5 or "<blank>",
                "diagnostic": (
                    f"activity schedule name {a4!r} occupies field A4 (Number of "
                    f"People Calculation Method, must be one of "
                    f"{'/'.join(_PEOPLE_CALC_METHODS)}); it was authored adjacent "
                    f"to the number-of-people schedule and shifted every later "
                    f"field one position, leaving the required A5 Activity Level "
                    f"Schedule Name blank"
                )
                if misplaced
                else (
                    f"field A4 value {a4!r} is not a legal Number of People "
                    f"Calculation Method (expected one of "
                    f"{'/'.join(_PEOPLE_CALC_METHODS)}); OpenStudio-style keys "
                    f"like ZoneFloorAreaPerPerson are rejected by EnergyPlus"
                ),
            }
        )
    evidence = {
        "idd_field_order": [
            "A1 Name",
            "A2 Zone or ZoneList Name",
            "A3 Number of People Schedule Name",
            "A4 Number of People Calculation Method (People|People/Area|Area/Person)",
            "N1 Number of People",
            "N2 People per Floor Area",
            "N3 Floor Area per Person",
            "N4 Fraction Radiant",
            "N5 Sensible Heat Fraction",
            "A5 Activity Level Schedule Name (required)",
        ],
        "disease_vs_symptom": (
            "mep.load_to_schedule reports the symptom (A5 blank → 'missing'); "
            "this check reports the disease (A4 holds a non-enum value)"
        ),
    }
    if offenders:
        rep.add_fail(
            "mep.people_field_alignment",
            CheckLayer.INVARIANT,
            f"{len(offenders)} People object(s) have a field misalignment or an "
            f"illegal calculation method (the activity schedule was misplaced "
            f"into the calculation-method slot, or A4 is not "
            f"{'/'.join(_PEOPLE_CALC_METHODS)})",
            evidence=evidence | {"offenders": offenders},
        )
    else:
        rep.add_pass("mep.people_field_alignment", CheckLayer.INVARIANT, evidence=evidence)


def _per_zone_coverage(
    rep: CheckReport, idx: IdfFragmentIndex, zone_names: set[str] | None
) -> None:
    """Each zone should carry People + Lights (flag — soft coverage)."""
    if not zone_names:
        rep.add("mep.per_zone_coverage", CheckStatus.NOT_APPLICABLE,
                CheckLayer.CROSS_CHECK, message="no zone list supplied")
        return
    people_zones = {o.fields[1] for o in idx.of_type("PEOPLE") if len(o.fields) > 1}
    lights_zones = {o.fields[1] for o in idx.of_type("LIGHTS") if len(o.fields) > 1}
    missing = {}
    for z in sorted(zone_names):
        gaps = [k for k, s in (("people", people_zones), ("lights", lights_zones)) if z not in s]
        if gaps:
            missing[z] = gaps
    if missing:
        rep.add_fail("mep.per_zone_coverage", CheckLayer.CROSS_CHECK,
                     f"{len(missing)} zone(s) missing a load object",
                     evidence={"missing": missing})
    else:
        rep.add_pass("mep.per_zone_coverage", CheckLayer.CROSS_CHECK,
                     evidence={"zones": len(zone_names)})


def _simpleglazing_standalone(rep: CheckReport, idx: IdfFragmentIndex) -> None:
    glaz = idx.has_name("WINDOWMATERIAL:SIMPLEGLAZINGSYSTEM")
    bad = []
    for c in idx.of_type("CONSTRUCTION"):
        layers = [x for x in c.fields[1:] if x]
        if any(layer in glaz for layer in layers) and len(layers) != 1:
            bad.append({"construction": c.name, "layers": layers})
    if bad:
        rep.add_fail("mep.simpleglazing_standalone", CheckLayer.INVARIANT,
                     f"{len(bad)} construction(s) embed SimpleGlazingSystem in a "
                     f"multi-layer construction (must be standalone — EP NaN fatal)",
                     evidence={"offenders": bad})
    else:
        rep.add_pass("mep.simpleglazing_standalone", CheckLayer.INVARIANT)


def _nomass_positive_resistance(rep: CheckReport, idx: IdfFragmentIndex) -> None:
    bad = []
    for m in idx.of_type("MATERIAL:NOMASS"):
        # fields = [name, roughness, thermal_resistance, ...]
        try:
            r = float(m.fields[2]) if len(m.fields) > 2 else 0.0
        except ValueError:
            r = 0.0
        if r <= 0:
            bad.append({"material": m.name, "thermal_resistance": m.fields[2] if len(m.fields) > 2 else None})
    if bad:
        rep.add_fail("mep.nomass_positive_resistance", CheckLayer.INVARIANT,
                     f"{len(bad)} Material:NoMass with non-positive thermal resistance",
                     evidence={"offenders": bad})
    else:
        rep.add_pass("mep.nomass_positive_resistance", CheckLayer.INVARIANT)


def _reasonability_placeholder(rep: CheckReport) -> None:
    """Placeholder: LPD/density/setpoint/U-value bands — filled once MEP input is
    richer (§5.2). Recorded as NOT_APPLICABLE so the framework slot is visible."""
    rep.add("mep.reasonability_bands", CheckStatus.NOT_APPLICABLE,
            CheckLayer.CROSS_CHECK,
            message="reasonability bands deferred until MEP input is richer (§5.2)")


# --------------------------------------------------------------------------- #
# IDD-driven field-alignment gate (摊 B, 2026-08-14)
# --------------------------------------------------------------------------- #
# A missing required field — or a positional shift where one blank cell collapses
# the whole row — is caught for EVERY object type off the authoritative IDD, not
# just People (where mep.people_field_alignment was bolted on after the fact, the
# "whack-a-mole" this gate retires). The IDD lives in-repo
# (data/dependencies/Energy+.idd) and eppy exposes its \field / \required-field /
# \extensible directives on each parsed object's objidd, so this needs no
# hand-maintained per-type rule and no new dependency. Parsing reuses the single
# idf_fragments parser — no regex (施工 H3).

_IDD_ALIGNMENT_PASS_EVIDENCE = {
    "criteria": [
        "missing_required: an IDD \\required-field is absent or blank",
        "too_many_fields: authored count > IDD count (extensible objects exempt)",
    ],
    "idd_source": (
        "data/dependencies/Energy+.idd via eppy objidd "
        "(\\field / \\required-field / \\extensible)"
    ),
    "extensible_detection": (
        "objidd[0] key matching 'extensible:<N>' — eppy exposes the IDD "
        "\\extensible:N directive as a key literally named 'extensible:N', NOT a "
        "plain 'extensible' field (discovered by dumping objidd[0].keys(): "
        "Schedule:Compact carries 'extensible:1', Material carries none)"
    ),
    "dedup": (
        "does not re-state mep.people_field_alignment's misalignment diagnosis "
        "for People objects"
    ),
}


def _idd_object_meta(raw):
    """Return (idd_fields, idd_count, extensible_group) for one eppy object.

    - idd_fields: [(field_name, is_required)] for every IDD-declared field.
    - idd_count: len(idd_fields). For extensible objects eppy expands the IDD to
      a large fixed cap (e.g. Schedule:Compact → 10000), so this is the cap, not
      the authored count.
    - extensible_group: the N from the IDD ``\\extensible:N`` directive, or None
      for non-extensible objects.
    """
    objidd = getattr(raw, "objidd", None)
    if not objidd:
        return None
    head = objidd[0] if isinstance(objidd, list) and objidd else {}
    idd_fields = []
    for entry in objidd[1:]:  # entry[0] is the object-level metadata
        fname = entry.get("field")
        if not fname:
            continue
        name = fname[0] if isinstance(fname, (list, tuple)) else fname
        idd_fields.append((name, "required-field" in entry))
    extensible_group = None
    for key in head:
        if isinstance(key, str) and key.startswith("extensible:"):
            try:
                extensible_group = int(key.split(":", 1)[1])
            except ValueError:
                extensible_group = 0  # marked extensible but unparseable → treat as extensible
            break
    return idd_fields, len(idd_fields), extensible_group


def _idd_field_findings(idx: IdfFragmentIndex):
    """Compute IDD field-alignment findings for every parsed object.

    Mirrors the orchestrator's read-only prescan (probe_arity.audit_object) so
    that B2 (prescan reproduction) is a structural guarantee, not a coincidence.

    Two criteria:
      (1) missing_required — an IDD \\required-field is absent or blank.
      (2) too_many_fields  — authored count exceeds the IDD field count.
          Extensible objects are exempt (their count is variable by design).

    ⚠️ Criterion (2) is STRUCTURALLY UNOBSERVABLE on the real parse path: eppy
    either silently truncates an over-long object to its IDD count (e.g. Lights)
    or raises TypeError → mep.idf_parse ERROR/fail-closed (e.g. Material /
    Construction). Either way `authored > idd` can never hold for an object that
    reaches this loop. The logic is kept as defense-in-depth (if the parser ever
    changes) and exercised by a monkeypatch fixture (B1) since no real artifact
    can reach it.

    Returns (findings, diseased): diseased maps (obj_type, name) →
    {field, check_id} for missing_required findings, so the symptom checks
    (load_to_schedule / hvac_schedule_refs) can cite the disease.
    """
    findings = []
    diseased = {}
    meta_cache: dict[str, object] = {}  # IDD defs are per-type, identical across instances
    for obj in idx.objects:
        meta = meta_cache.get(obj.obj_type)
        if meta is None:
            meta = _idd_object_meta(obj.raw)
            if meta is not None:
                meta_cache[obj.obj_type] = meta
        if meta is None:
            continue
        idd_fields, idd_count, extensible_group = meta
        authored = obj.fields  # str-ized; len == authored count (fieldvalues does NOT pad)
        for i, (fname, required) in enumerate(idd_fields):
            if not required:
                continue
            val = authored[i] if i < len(authored) else None
            if val is None or str(val).strip() == "":
                findings.append({
                    "object_type": obj.obj_type,
                    "object": obj.name,
                    "kind": "missing_required",
                    "field_index": i + 1,
                    "field": fname,
                    "authored_field_count": len(authored),
                    "idd_field_count": idd_count,
                })
                diseased[(obj.obj_type, obj.name)] = {
                    "field": fname,
                    "check_id": "mep.idd_field_alignment",
                }
        if extensible_group is None and idd_count and len(authored) > idd_count:
            findings.append({
                "object_type": obj.obj_type,
                "object": obj.name,
                "kind": "too_many_fields",
                "authored_field_count": len(authored),
                "idd_field_count": idd_count,
            })
    return findings, diseased


def _disease_ref(idd_diseased, obj_type, name):
    """Cross-reference to mep.idd_field_alignment when the same object is also
    flagged there for a missing required field — the disease behind a symptom
    offender that load_to_schedule / hvac_schedule_refs report."""
    if not idd_diseased:
        return None
    d = idd_diseased.get((obj_type, name))
    if not d:
        return None
    return {"check_id": d["check_id"], "missing_field": d["field"]}


def _idd_field_alignment(rep: CheckReport, findings: list) -> None:
    """IDD-driven field-alignment gate — the disease, for every object type.

    Tiered disposition (user 2026-08-14): a finding on a deterministic-code
    object type (摊 A's HVACTemplate:* generators) is an INVARIANT (BLOCK — a code
    bug); any other type is a CROSS_CHECK (FLAG — reported, never blocking, so no
    historical run's blocking status changes). The layer is chosen from whether
    any block-list offender is present, so the framework's pure disposition()
    maps the result correctly with NO schema.py change.

    De-dup vs mep.people_field_alignment (dispatch §2.4): that check owns the
    People MISALIGNMENT disease (A4 holds a non-enum value). This gate reports
    the same root cause from the generic IDD angle (A5 required field blank) and
    does NOT re-state the misalignment diagnosis — the two report different
    cells, so a reader never sees "field misalignment" counted twice for one
    People object. people_dedup_note makes that explicit in evidence.
    """
    if not findings:
        rep.add_pass("mep.idd_field_alignment", CheckLayer.INVARIANT,
                     evidence=_IDD_ALIGNMENT_PASS_EVIDENCE)
        return
    block_offenders = [f for f in findings if f["object_type"] in _IDD_ALIGNMENT_BLOCK_TYPES]
    layer = CheckLayer.INVARIANT if block_offenders else CheckLayer.CROSS_CHECK
    by_kind: dict[str, int] = {}
    for f in findings:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
    evidence = {
        **_IDD_ALIGNMENT_PASS_EVIDENCE,
        "block_list_types": sorted(_IDD_ALIGNMENT_BLOCK_TYPES),
        "block_offenders_count": len(block_offenders),
        "disposition": (
            "block (INVARIANT) — block-list offender present"
            if block_offenders
            else "report-only (CROSS_CHECK/FLAG) — no block-list offender"
        ),
        "findings_by_kind": by_kind,
    }
    if any(f["object_type"] == "PEOPLE" for f in findings):
        evidence["people_dedup_note"] = (
            "People missing_required findings here are the generic IDD view of the "
            "same root cause mep.people_field_alignment diagnoses as a misalignment "
            "(A4 non-enum); this gate does not re-state the misalignment, only the "
            "blank required field"
        )
    missing = by_kind.get("missing_required", 0)
    too_many = by_kind.get("too_many_fields", 0)
    parts = []
    if missing:
        parts.append(f"{missing} missing-required-field")
    if too_many:
        parts.append(f"{too_many} too-many-field")
    rep.add_fail(
        "mep.idd_field_alignment", layer,
        f"{len(findings)} object(s) with IDD field-alignment findings ({', '.join(parts)})",
        evidence=evidence | {"offenders": findings},
    )
