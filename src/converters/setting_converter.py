from typing import Any

from eppy.modeleditor import IDF

from src.converters.base_converter import BaseConverter
from src.validator.data_model import (
    GlobalGeometryRulesSchema,
    OutputControlTableStyleSchema,
    OutputDiagnosticsSchema,
    OutputTableSummaryReportsSchema,
    OutputVariableDictionarySchema,
    OutputVariableSchema,
    RunPeriodSchema,
    SimulationControlSchema,
    SiteLocationSchema,
    TimestepSchema,
    VersionSchema,
)


class SettingsConverter(BaseConverter):
    def __init__(self, idf: IDF):
        super().__init__(idf)

        self.setting_map = {
            "SimulationControl": SimulationControlSchema,
            "Timestep": TimestepSchema,
            "RunPeriod": RunPeriodSchema,
            "GlobalGeometryRules": GlobalGeometryRulesSchema,
            "Site:Location": SiteLocationSchema,
            "Output:VariableDictionary": OutputVariableDictionarySchema,
            "Output:Diagnostics": OutputDiagnosticsSchema,
            "Output:Table:SummaryReports": OutputTableSummaryReportsSchema,
            "OutputControl:Table:Style": OutputControlTableStyleSchema,
            "Output:Variable": OutputVariableSchema,
        }

        self.apply_function_map = {
            "SimulationControl": self._simulation_control_apply,
            "Timestep": self._timestep_apply,
            "RunPeriod": self._run_period_apply,
            "GlobalGeometryRules": self._global_geometry_rules_apply,
            "Site:Location": self._site_location_apply,
            "Output:VariableDictionary": self._output_variable_dictionary_apply,
            "Output:Diagnostics": self._output_diagnostics_apply,
            "Output:Table:SummaryReports": self._output_table_summary_reports_apply,
            "OutputControl:Table:Style": self._output_control_table_style_apply,
            "Output:Variable": self._output_variable_apply,
        }

    def convert(self, data: dict[str, Any]) -> None:
        self.logger.info("Settings Converter Starting...")

        version_tuple: tuple[int, ...] = self.idf.idd_version  # type: ignore
        global_settings_data = {
            key: data.get(key) for key in self.setting_map if key in data
        }

        try:
            data_to_validate = {
                "version_data": {"version": version_tuple},
                "global_settings_data": global_settings_data,
            }
            validated_data = self.validate(data_to_validate)
            self._add_to_idf(validated_data)
            self.state["success"] += 1
        except Exception:
            self.state["failed"] += 1
            self.logger.exception("Error during settings conversion process")

    def validate(self, data: dict) -> dict:
        self.logger.info("Validating global settings...")

        val_version_data = VersionSchema.model_validate(data.get("version_data", {}))

        validated_settings = {}
        raw_global_settings = data.get("global_settings_data", {})

        for idf_key, setting_data in raw_global_settings.items():
            if setting_data is None:
                continue

            schema = self.setting_map.get(idf_key)
            if not schema:
                self.logger.warning(
                    "No schema found for '{}', skipping validation for this item.",
                    idf_key,
                )
                continue

            try:
                if isinstance(setting_data, list):
                    validated_settings[idf_key] = [
                        schema.model_validate(item) for item in setting_data
                    ]
                else:
                    validated_settings[idf_key] = schema.model_validate(setting_data)
            except Exception:
                self.logger.exception("Validation failed for '{}'", idf_key)
                raise

        return {
            "version_info": val_version_data,
            "validated_settings": validated_settings,
        }

    def _add_to_idf(self, val_data: dict) -> None:
        version_info = val_data.get("version_info")
        settings_to_add = val_data.get("validated_settings", {})

        if version_info and not self.idf.idfobjects.get("Version"):
            self.logger.info("Adding Version object '{}' to IDF.", version_info.version)
            self.idf.newidfobject("Version", Version_Identifier=version_info.version)

        for idf_key, validated_model_or_list in settings_to_add.items():
            items_to_process = (
                validated_model_or_list
                if isinstance(validated_model_or_list, list)
                else [validated_model_or_list]
            )
            for validated_model in items_to_process:
                self._add_single_object_to_idf(idf_key, validated_model)

    def _add_single_object_to_idf(self, idf_key: str, validated_model) -> None:
        if (
            idf_key != "Output:Variable"
            and len(self.idf.idfobjects.get(idf_key, [])) > 0
        ):
            self.logger.warning(
                "Object of type '{}' already exists. Skipping addition.", idf_key
            )
            return

        apply_function = self.apply_function_map.get(idf_key)
        if apply_function:
            apply_function(validated_model)
        else:
            self.logger.error("No apply function found for '{}'", idf_key)

    def _simulation_control_apply(self, model: SimulationControlSchema) -> None:
        self.idf.newidfobject(
            "SimulationControl",
            Do_Zone_Sizing_Calculation=model.do_zone_sizing_calculation,
            Do_System_Sizing_Calculation=model.do_system_sizing_calculation,
            Do_Plant_Sizing_Calculation=model.do_plant_sizing_calculation,
            Run_Simulation_for_Sizing_Periods=model.run_simulation_for_sizing_periods,
            Run_Simulation_for_Weather_File_Run_Periods=model.run_simulation_for_weather_file_run_periods,
            Do_HVAC_Sizing_Simulation_for_Sizing_Periods=model.do_hvac_sizing_simulation_for_sizing_periods,
            Maximum_Number_of_HVAC_Sizing_Simulation_Passes=model.maximum_number_of_hvac_sizing_simulation_passes,
        )
        self.logger.success("Added setting 'SimulationControl' to IDF.")

    def _timestep_apply(self, model: TimestepSchema) -> None:
        self.idf.newidfobject(
            "Timestep",
            Number_of_Timesteps_per_Hour=model.number_of_timesteps_per_hour,
        )
        self.logger.success("Added setting 'Timestep' to IDF.")

    def _run_period_apply(self, model: RunPeriodSchema) -> None:
        kwargs = {
            "Name": model.name,
            "Begin_Month": model.begin_month,
            "Begin_Day_of_Month": model.begin_day_of_month,
            "End_Month": model.end_month,
            "End_Day_of_Month": model.end_day_of_month,
        }
        optional = {
            "Begin_Year": model.begin_year,
            "End_Year": model.end_year,
            "Day_of_Week_for_Start_Day": model.day_of_week_for_start_day,
            "Use_Weather_File_Holidays_and_Special_Days": model.use_weather_file_holidays_and_special_days,
            "Use_Weather_File_Daylight_Saving_Period": model.use_weather_file_daylight_saving_period,
            "Apply_Weekend_Holiday_Rule": model.apply_weekend_holiday_rule,
            "Use_Weather_File_Rain_Indicators": model.use_weather_file_rain_indicators,
            "Use_Weather_File_Snow_Indicators": model.use_weather_file_snow_indicators,
        }
        kwargs.update({k: v for k, v in optional.items() if v is not None})
        self.idf.newidfobject("RunPeriod", **kwargs)
        self.logger.success("Added setting 'RunPeriod' to IDF.")

    def _global_geometry_rules_apply(self, model: GlobalGeometryRulesSchema) -> None:
        # A4/A5 written explicitly (E4-output-contract spec v2 §5.1) — never
        # left to the EP IDD default, so World vs Relative is self-evident
        # from the emitted IDF regardless of which contract mode produced it.
        self.idf.newidfobject(
            "GlobalGeometryRules",
            Starting_Vertex_Position=model.starting_vertex_position,
            Vertex_Entry_Direction=model.vertex_entry_direction,
            Coordinate_System=model.coordinate_system,
            Daylighting_Reference_Point_Coordinate_System=(
                model.daylighting_reference_point_coordinate_system
            ),
            Rectangular_Surface_Coordinate_System=model.rectangular_surface_coordinate_system,
        )
        self.logger.success("Added setting 'GlobalGeometryRules' to IDF.")

    def _site_location_apply(self, model: SiteLocationSchema) -> None:
        obj = self.idf.newidfobject(
            "Site:Location",
            Name=model.name,
            Latitude=model.latitude,
            Longitude=model.longitude,
            Time_Zone=model.time_zone,
            Elevation=model.elevation,
        )
        # E+ 25.1 IDD vs internal JSON schema mismatch: the IDD defines an
        # optional "Keep Site Location Information" field (populated by eppy
        # to "No" via its IDD default) but the 25.1 validator rejects any
        # Site:Location with more than 5 fields. Truncate the raw field list
        # so eppy omits the trailing field when it writes the IDF.
        obj.obj = obj.obj[:6]
        self.logger.success("Added setting 'Site:Location' to IDF.")

    def _output_variable_dictionary_apply(
        self, model: OutputVariableDictionarySchema
    ) -> None:
        self.idf.newidfobject(
            "Output:VariableDictionary",
            Key_Field=model.key_field,
        )
        self.logger.success("Added setting 'Output:VariableDictionary' to IDF.")

    def _output_diagnostics_apply(self, model: OutputDiagnosticsSchema) -> None:
        self.idf.newidfobject(
            "Output:Diagnostics",
            Key_1=model.key_1,
        )
        self.logger.success("Added setting 'Output:Diagnostics' to IDF.")

    def _output_table_summary_reports_apply(
        self, model: OutputTableSummaryReportsSchema
    ) -> None:
        self.idf.newidfobject(
            "Output:Table:SummaryReports",
            Report_1_Name=model.report_1_name,
        )
        self.logger.success("Added setting 'Output:Table:SummaryReports' to IDF.")

    def _output_control_table_style_apply(
        self, model: OutputControlTableStyleSchema
    ) -> None:
        self.idf.newidfobject(
            "OutputControl:Table:Style",
            Column_Separator=model.column_separator,
            Unit_Conversion=model.unit_conversion,
        )
        self.logger.success("Added setting 'OutputControl:Table:Style' to IDF.")

    def _output_variable_apply(self, model: OutputVariableSchema) -> None:
        self.idf.newidfobject(
            "Output:Variable",
            Key_Value=model.key_value,
            Variable_Name=model.variable_name,
            Reporting_Frequency=model.reporting_frequency,
        )
        self.logger.success("Added setting 'Output:Variable' to IDF.")
