from typing import Any

from langchain_core.tools import BaseTool, tool

from src.mcp.state import ConfigState
from src.mcp.tools.schedule import ScheduleCompactTool, ScheduleTypeLimitsTool


def make_schedule_tools(config: ConfigState) -> list[BaseTool]:
    stl = ScheduleTypeLimitsTool(config)
    sct = ScheduleCompactTool(config)

    @tool
    def create_schedule_type_limits(
        name: str,
        lower_limit_value: float | None = None,
        upper_limit_value: float | None = None,
        numeric_type: str = "CONTINUOUS",
        unit_type: str = "Dimensionless",
    ) -> str:
        """Create a ScheduleTypeLimits.

        Args:
            name: Unique name (e.g., 'Fraction', 'Temperature', 'OnOff').
            lower_limit_value: Minimum allowed value (None = unbounded).
            upper_limit_value: Maximum allowed value (None = unbounded).
            numeric_type: CONTINUOUS or DISCRETE.
            unit_type: EnergyPlus unit category (Dimensionless / Temperature / Power / ...).
        """
        return stl.create(
            {
                "Name": name,
                "Lower Limit Value": ""
                if lower_limit_value is None
                else lower_limit_value,
                "Upper Limit Value": ""
                if upper_limit_value is None
                else upper_limit_value,
                "Numeric Type": numeric_type,
                "Unit Type": unit_type,
            }
        ).model_dump_json()

    @tool
    def create_schedule_compact(
        name: str,
        schedule_type_limits_name: str,
        data: list[dict[str, Any]],
    ) -> str:
        """Create a Schedule:Compact.

        Args:
            name: Unique schedule name.
            schedule_type_limits_name: Existing ScheduleTypeLimits name.
            data: Nested schedule structure. Each element is one "Through"-block:

                {
                  "Through": "MM/DD",               # last block must be "12/31"
                  "Days": [
                    {
                      "For": "<DayType>",           # Weekdays / Weekends / Saturday /
                                                    # Sunday / AllDays / AllOtherDays /
                                                    # SummerDesignDay / WinterDesignDay /
                                                    # Monday...Friday / Holidays /
                                                    # CustomDay1 / CustomDay2
                      "Times": [
                        {"Until": {"Time": "HH:MM", "Value": <float>}},
                        ...
                        {"Until": {"Time": "24:00", "Value": <float>}},  # last must be 24:00
                      ],
                    },
                    ...  # additional day-type blocks under the same Through
                  ],
                }

                Example (office fraction schedule, weekdays 8-18 at 1.0, else 0.0):

                [
                  {
                    "Through": "12/31",
                    "Days": [
                      {"For": "Weekdays", "Times": [
                        {"Until": {"Time": "08:00", "Value": 0.0}},
                        {"Until": {"Time": "18:00", "Value": 1.0}},
                        {"Until": {"Time": "24:00", "Value": 0.0}},
                      ]},
                      {"For": "AllOtherDays", "Times": [
                        {"Until": {"Time": "24:00", "Value": 0.0}},
                      ]},
                    ],
                  },
                ]
        """
        return sct.create(
            {
                "Name": name,
                "Schedule Type Limits Name": schedule_type_limits_name,
                "Data": data,
            }
        ).model_dump_json()

    @tool
    def list_schedules() -> str:
        """List all Schedule:Compact objects."""
        return sct.list_all().model_dump_json()

    @tool
    def list_schedule_type_limits() -> str:
        """List all ScheduleTypeLimits objects."""
        return stl.list_all().model_dump_json()

    @tool
    def get_schedule(name: str) -> str:
        """Read a Schedule:Compact by name."""
        return sct.read(name).model_dump_json()

    @tool
    def delete_schedule(name: str) -> str:
        """Delete a Schedule:Compact. Fails if referenced."""
        return sct.delete(name).model_dump_json()

    return [
        create_schedule_type_limits,
        create_schedule_compact,
        list_schedules,
        list_schedule_type_limits,
        get_schedule,
        delete_schedule,
    ]
