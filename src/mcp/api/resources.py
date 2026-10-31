from fastmcp import FastMCP
from omegaconf import OmegaConf

from src.mcp.state import ConfigState


def register_resources(mcp: FastMCP, state: ConfigState) -> None:
    """Register MCP resource endpoints for configuration access.

    Args:
        mcp: FastMCP server instance.
        state: Shared ConfigState instance.
    """

    @mcp.resource("config://current")
    def get_current_config() -> str:
        """Get the full current configuration as YAML.

        Returns:
            YAML string representation of the entire configuration state.
        """
        return OmegaConf.to_yaml(state.to_yaml_dict())

    @mcp.resource("config://summary")
    def get_summary_resource() -> str:
        """Get a summary of the current configuration as YAML.

        Returns:
            YAML string with component counts and key settings.
        """
        return OmegaConf.to_yaml(
            state.get_summary().model_dump(by_alias=True, exclude_none=True)
        )
