from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool, tool

from src.mcp.interface import ToolResponse
from src.mcp.state import ConfigState
from src.mcp.tools.zone import ZoneTool

if TYPE_CHECKING:
    from src.agent.output_coordinates import OutputCoordinateContract

_NONZERO_FRAME_REJECT_MSG = (
    "Zone frame fields (x_origin/y_origin/z_origin/direction_of_relative_north) "
    "must be 0 under the active Relative output-coordinate contract — floor "
    "elevation belongs in the surface vertices, not the Zone origin, and "
    "Building.North Axis (not Zone rotation) carries the building's true-north "
    "angle. Pass 0 (or omit the argument) for every frame field."
)


def _frame_policy(contract: "OutputCoordinateContract | None") -> str:
    """`preserve_legacy` (default / world_legacy / no contract at all — the
    standalone MCP path with no AgentState) or `all_zero` (relative_north_axis).
    Read once from the caller-supplied contract; tools never inspect GGR/theta
    to guess this (spec §5.2/§1.2 invariant 4)."""
    return "all_zero" if contract is not None and contract.zone_origin_policy == "all_zero" else "preserve_legacy"


def make_zone_tools(config: ConfigState, *, contract: "OutputCoordinateContract | None" = None) -> list[BaseTool]:
    """Create Zone CRUD tools bound to `config`.

    ``contract`` (E4-output-contract spec v2 §5.2/§6.2 item 5): when its
    ``zone_origin_policy`` is ``all_zero``, `create_zone`/`update_zone` reject
    any nonzero frame field outright instead of silently accepting it (the
    tail-of-stage normalizer in `zone_agent` is a belt-and-suspenders backstop,
    not the primary enforcement point). ``contract=None`` (including a
    standalone MCP caller with no AgentState) preserves the exact legacy
    behavior — frame fields pass through unchanged.
    """
    zt = ZoneTool(config)
    policy = _frame_policy(contract)

    def _reject_if_nonzero(values: dict[str, float | None]) -> ToolResponse | None:
        if policy != "all_zero":
            return None
        offenders = {k: v for k, v in values.items() if v not in (None, 0, 0.0)}
        if offenders:
            return ToolResponse(
                success=False,
                message=_NONZERO_FRAME_REJECT_MSG,
                data={"rejected_fields": offenders},
            )
        return None

    @tool
    def create_zone(
        name: str,
        x_origin: float = 0.0,
        y_origin: float = 0.0,
        z_origin: float = 0.0,
        direction_of_relative_north: float = 0.0,
        multiplier: int = 1,
    ) -> str:
        """Create a thermal zone.

        Args:
            name: Unique deterministic zone name (e.g., 'Z01_F1_Office_SW').
            x_origin: X of zone origin (meters). Under a Relative output
                coordinate contract this MUST be 0 — floor elevation lives in
                the surface vertices, not here.
            y_origin: Y of zone origin (meters). Same 0-only rule as x_origin.
            z_origin: Z of zone origin (meters). Same 0-only rule as x_origin.
            direction_of_relative_north: Zone rotation (degrees, 0-360). Under
                a Relative output coordinate contract this MUST be 0 —
                Building.North Axis is the single true-north owner.
            multiplier: Zone multiplier (>= 1) for repeated identical zones.
        """
        rejected = _reject_if_nonzero({
            "x_origin": x_origin, "y_origin": y_origin, "z_origin": z_origin,
            "direction_of_relative_north": direction_of_relative_north,
        })
        if rejected is not None:
            return rejected.model_dump_json()
        return zt.create(
            {
                "Name": name,
                "X Origin": x_origin,
                "Y Origin": y_origin,
                "Z Origin": z_origin,
                "Direction of Relative North": direction_of_relative_north,
                "Multiplier": multiplier,
            }
        ).model_dump_json()

    @tool
    def list_zones() -> str:
        """List all existing thermal zones."""
        return zt.list_all().model_dump_json()

    @tool
    def get_zone(name: str) -> str:
        """Read a zone by name."""
        return zt.read(name).model_dump_json()

    @tool
    def update_zone(
        name: str,
        x_origin: float | None = None,
        y_origin: float | None = None,
        z_origin: float | None = None,
        direction_of_relative_north: float | None = None,
        multiplier: int | None = None,
    ) -> str:
        """Update a zone's origin coordinates."""
        rejected = _reject_if_nonzero({
            "x_origin": x_origin, "y_origin": y_origin, "z_origin": z_origin,
            "direction_of_relative_north": direction_of_relative_north,
        })
        if rejected is not None:
            return rejected.model_dump_json()
        payload: dict = {}
        if x_origin is not None:
            payload["X Origin"] = x_origin
        if y_origin is not None:
            payload["Y Origin"] = y_origin
        if z_origin is not None:
            payload["Z Origin"] = z_origin
        if direction_of_relative_north is not None:
            payload["Direction of Relative North"] = direction_of_relative_north
        if multiplier is not None:
            payload["Multiplier"] = multiplier
        return zt.update(name, payload).model_dump_json()

    @tool
    def delete_zone(name: str) -> str:
        """Delete a zone by name."""
        return zt.delete(name).model_dump_json()

    return [create_zone, list_zones, get_zone, update_zone, delete_zone]
