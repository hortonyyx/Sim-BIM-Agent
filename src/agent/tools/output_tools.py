import json

from langchain_core.tools import BaseTool, tool

from src.mcp.state import ConfigState
from src.validator import OutputVariableSchema


def make_output_tools(config: ConfigState) -> list[BaseTool]:
    @tool
    def add_output_variable(
        variable_name: str,
        key_value: str = "*",
        reporting_frequency: str = "Hourly",
    ) -> str:
        """Add an Output:Variable request.

        Args:
            variable_name: Report variable name (e.g., 'Zone Air Temperature').
            key_value: Object key filter; '*' means all matching objects.
            reporting_frequency: Detailed / Timestep / Hourly / Daily / Monthly / RunPeriod.
        """
        try:
            ov = OutputVariableSchema.model_validate(
                {
                    "Key Value": key_value,
                    "Variable Name": variable_name,
                    "Reporting Frequency": reporting_frequency,
                }
            )
        except Exception as exc:
            return json.dumps({"success": False, "message": f"validation error: {exc}"})
        config.output_variable.append(ov)
        return json.dumps({"success": True, "message": f"added {variable_name}"})

    @tool
    def list_output_variables() -> str:
        """List all registered Output:Variable entries."""
        items = [ov.model_dump(by_alias=True) for ov in config.output_variable]
        return json.dumps({"success": True, "count": len(items), "items": items})

    return [add_output_variable, list_output_variables]
