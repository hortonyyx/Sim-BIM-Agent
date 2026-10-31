import sqlite3
from typing import Any


def _format_field(label: str, value: Any, unit: str = "") -> str:
    if value is None or str(value).strip().lower() == "none" or value == "":
        return ""
    return f"{label}: {value}{unit}"


def _gen_description_material(data: list):
    fields = [
        _format_field("ID", data[0]),
        _format_field("Name", data[1]),
        _format_field("Latitude", data[2]),
        _format_field("Longitude", data[3]),
        _format_field("Building Type", data[4]),
        _format_field("Roughness", data[5]),
        _format_field("Thickness", data[6], "m"),
        _format_field("Thermal Conductivity", data[7], "W/(m*k)"),
        _format_field("Density", data[8], "kg/m3"),
        _format_field("Specific Heat", data[9], "J/(kg*k)"),
        _format_field("Thermal Absorptance", data[10]),
        _format_field("Solar Absorptance", data[11]),
        _format_field("Visible Absorptance", data[12]),
    ]
    content = " | ".join([f for f in fields if f])
    de = "This data includes parameter data defined for a standard_material in EnergyPlus. The ID represents its ID in the standard_materials table, the longitude and latitude data indicate the geographical location of the building in its source data, and the building type refers to its metadata building type."
    return f"This is a standard material data in our EnergyPlus database. {de} EnergyPlus Standard Material Details: {content}"


def _gen_description_nomass_material(data: list):
    fields = [
        _format_field("ID", data[0]),
        _format_field("Name", data[1]),
        _format_field("Latitude", data[2]),
        _format_field("Longitude", data[3]),
        _format_field("Building Type", data[4]),
        _format_field("Roughness", data[5]),
        _format_field("Thermal Resistance (R-value)", data[6], "m^2*K/W"),
        _format_field("Thermal Absorptance", data[7]),
        _format_field("Solar Absorptance", data[8]),
        _format_field("Visible Absorptance", data[9]),
    ]
    content = " | ".join([f for f in fields if f])
    de = "This data includes parameter data defined for a no mass material in EnergyPlus. The ID represents its ID in the no_mass_materials table, the longitude and latitude data indicate the geographical location of the building in its source data, and the building type refers to its metadata building type."
    return f"This is a no mass material data in our EnergyPlus database. {de} EnergyPlus No-Mass Material Details: {content}"


def _gen_description_construction(data: list):
    layers = [str(layer) for layer in data[5:25] if layer is not None]
    layers_str = f"Layers(All_Materials IDs): {', '.join(layers)}" if layers else ""

    fields = [
        _format_field("ID", data[0]),
        _format_field("Name", data[1]),
        _format_field("Latitude", data[2]),
        _format_field("Longitude", data[3]),
        _format_field("Building Type", data[4]),
        layers_str,
    ]
    content = " | ".join([f for f in fields if f])
    de = "This construction data contains the parameters required in EnergyPlus. The ID represents its ID in the constructions table, the longitude and latitude data indicate the geographical location of the building in its source data, and the building type refers to its metadata building type. The layer data in this dataset points to the id of the all_materials table."
    logic_note = "Syntax Note: The construction Layers data ID is the All Materials index ID in our database."
    return f"This is a construction data in our EnergyPlus database. {de} EnergyPlus Construction Assembly: {content} | {logic_note}"


def _gen_description_schedule_type_limits(data: list):
    fields = [
        _format_field("ID", data[0]),
        _format_field("Name", data[1]),
        _format_field("Latitude", data[2]),
        _format_field("Longitude", data[3]),
        _format_field("Building Type", data[4]),
        _format_field("Lower Limit", data[5]),
        _format_field("Upper Limit", data[6]),
        _format_field("Numeric Type", data[7]),
        _format_field("Unit Type", data[8]),
    ]
    content = " | ".join([f for f in fields if f])
    de = "This schedule type limits the data to include the parameters required in EnergyPlus. It will be referenced by schedule_compact data as a data type limits. The ID represents its ID in the schedule_type_limits table, the longitude and latitude data indicate the geographical location of the building in its source data, and the building type refers to its metadata building type. "
    return f"This is a schedule type limits data in our EnergyPlus database. {de} EnergyPlus Schedule Type Limits: {content}"


def _gen_description_schedule_compact(data: list):
    schedule_parts = [
        str(item) for item in data[6:] if item is not None and item != "null"
    ]
    schedule_str = (
        f"Schedule Definition: {' '.join(schedule_parts)}" if schedule_parts else ""
    )

    fields = [
        _format_field("ID", data[0]),
        _format_field("Name", data[1]),
        _format_field("Latitude", data[2]),
        _format_field("Longitude", data[3]),
        _format_field("Building Type", data[4]),
        _format_field("Schedule Type Limits", data[5]),
        schedule_str,
    ]
    content = " | ".join([f for f in fields if f])
    de = "The schedule_compact data contains the parameters required in EnergyPlus. The Schedule Type Limits attribute will reference the id of the schedule_type_limits table. The ID represents its ID in the schedule_compact table, the longitude and latitude data indicate the geographical location of the building in its source data, and the building type refers to its metadata building type. "
    logic_note = "Syntax Note: 'Through' defines date range, 'For' defines day types, 'Until' defines time value."
    return f"This is a schedule compact data in our EnergyPlus database. {de} EnergyPlus Compact Schedule: {content} | {logic_note}"


def _gen_description_sizingperiod_designday(data: list):
    fields = [
        _format_field("ID", data[0]),
        _format_field("Name", data[1]),
        _format_field("Latitude", data[2]),
        _format_field("Longitude", data[3]),
        _format_field("Building Type", data[4]),
        _format_field("Month", data[5]),
        _format_field("Day", data[6]),
        _format_field("Day Type", data[7]),
        _format_field("Max Dry Bulb Temp", data[8], "C"),
        _format_field("Daily Drybulb temperature Range", data[9], "C"),
        _format_field("Drybulb temperature range modifier type", data[10]),
        _format_field("Drybulb temperature range modifier day schedule name", data[11]),
        _format_field("Humidity Type", data[12]),
        _format_field("Wet Bulb/Dew Point at Max DryBulb", data[13], "C"),
        _format_field("Humidity Condition Day Schedule Name", data[14]),
        _format_field("Humidity Ratio at Max Drybulb", data[15]),
        _format_field("Enthalpy at Max Drybulb", data[16]),
        _format_field("Daily Wetbulb Temperature Range", data[17]),
        _format_field("Barometric Pressure", data[18], "Pa"),
        _format_field("Wind Speed", data[19], "m/s"),
        _format_field("Wind Direction", data[20], "deg"),
        _format_field("Rain Indicator", data[21]),
        _format_field("Snow Indicator", data[22]),
        _format_field("Daylight Saving Time Indicator", data[23]),
        _format_field("Solar Model Indicator", data[24]),
        _format_field("Beam Solar Day Schedule Name", data[25]),
        _format_field("Diffuse Solar Day Schedule Name", data[26]),
        _format_field(
            "Ashrae Clear Sky Optical Depth for Beam Irradiance Taub", data[27]
        ),
        _format_field(
            "Ashrae Clear Sky Optical Depth for Diffuse Irradiance Taub", data[28]
        ),
        _format_field("Sky Clearness", data[29]),
        _format_field("Max Number Warmup Days", data[30]),
        _format_field("Begin Environment Reset Mode", data[31]),
    ]
    content = " | ".join([f for f in fields if f])
    de = "The sizingperiod designday data contains the parameters required in EnergyPlus. The ID represents its ID in the sizingperiod_designday table, the longitude and latitude data indicate the geographical location of the building in its source data, and the building type refers to its metadata building type. "
    return f"This is a sizingperiod designday data in our EnergyPlus database. {de} EnergyPlus Design Day Weather Data: {content}"


def _gen_description_all_materials(data: list):
    fields = [
        _format_field("Global ID", data[0]),
        _format_field("Name", data[1]),
        _format_field("Material Category", data[2]),
        _format_field("Standard Material Ref ID", data[3]),
        _format_field("No-Mass Material Ref ID", data[4]),
    ]
    content = " | ".join([f for f in fields if f])
    de = "The all_materials index data contains the parameters required in EnergyPlus. The attribute id corresponds to the id of this material in the corresponding data table. The ID represents its ID in the all_materials table. "
    return f"This is all_materials index data in our EnergyPlus database. {de} EnergyPlus Material Cross-Reference: {content}"


def _update_description(table_name, data, gen_func, cur):
    if cur is None:
        raise ValueError("cursor cannot be None")
    try:
        cur.execute("SELECT 1")
    except sqlite3.ProgrammingError as e:
        raise ValueError("Cursor connection is closed or invalid") from e
    cursor = cur
    description = gen_func(data)
    sql = "UPDATE " + table_name + " SET description = ? WHERE id = ?"
    cursor.execute(sql, (description, data[0]))


def update_description_material(data: list, cur):
    _update_description("standard_materials", data, _gen_description_material, cur=cur)


def update_description_nomass_material(data: list, cur):
    _update_description(
        "no_mass_materials", data, _gen_description_nomass_material, cur=cur
    )


def update_description_construction(data: list, cur):
    _update_description("constructions", data, _gen_description_construction, cur=cur)


def update_description_schedule_type_limits(data: list, cur):
    _update_description(
        "schedule_type_limits", data, _gen_description_schedule_type_limits, cur=cur
    )


def update_description_schedule_compact(data: list, cur):
    _update_description(
        "schedule_compact", data, _gen_description_schedule_compact, cur=cur
    )


def update_description_sizingperiod_designday(data: list, cur):
    _update_description(
        "sizingperiod_designday", data, _gen_description_sizingperiod_designday, cur=cur
    )


def update_description_all_materials(data: list, cur):
    _update_description("all_materials", data, _gen_description_all_materials, cur=cur)
