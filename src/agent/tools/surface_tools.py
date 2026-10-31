from langchain_core.tools import BaseTool, tool

from src.mcp.interface import ToolResponse
from src.mcp.state import ConfigState
from src.mcp.tools.construction import ConstructionTool
from src.mcp.tools.surface import SurfaceTool
from src.mcp.tools.zone import ZoneTool


# 2026-08-13 (batch create surfaces, structural-safety follow-up): a real
# reproduction of the `surface` node against production intake_output.json
# showed the model can send an oversized `create_surfaces_batch` call whose
# generation gets cut off mid-turn by the provider (observed
# finish_reason='length' on a 6-item call under an earlier, looser "one call
# per zone" prompt — up to ~8 items/call), which desyncs the provider's
# server-side tool-call bookkeeping from our local transcript and 400s the
# NEXT turn ("insufficient tool messages following tool_calls message" — the
# same error class this whole batch tool was built to avoid). Relying on
# prose ("send at most 4 per call") is not a defense on its own — this repo
# has repeatedly found prompt-only constraints get ignored/drift. This caps
# batch size in CODE so an oversized call is rejected with a clear,
# model-visible error instead of silently risking truncation.
#
# The exact number is an empirically-motivated, NOT rigorously derived,
# conservative choice: in real single-shot reproductions against the same
# 100-surface production spec, one 25-call run at <= 4 items/call completed
# cleanly (0 truncations); a looser <= 8-items-per-call ("one zone per call")
# prompt truncated on its 2nd of 2 real trials. This is n=1 clean / n=1
# truncated at the two tested sizes — NOT a proven safe threshold, just the
# best evidence available without a token-budget-based derivation (raw
# max_tokens=64000 is not the binding constraint here: even a 100-item batch
# is only ~15-25k tokens, well under budget, so the truncation is some other
# provider-side limit this repo does not have visibility into).
_MAX_BATCH_ITEMS = 4


def make_surface_tools(config: ConfigState) -> list[BaseTool]:
    st = SurfaceTool(config)

    @tool
    def create_surface(
        name: str,
        surface_type: str,
        construction_name: str,
        zone_name: str,
        outside_boundary_condition: str,
        vertices: list[dict[str, float]],
        sun_exposure: str = "NoSun",
        wind_exposure: str = "NoWind",
        outside_boundary_condition_object: str | None = None,
    ) -> str:
        """Create a BuildingSurface:Detailed (wall/floor/roof/ceiling).

        Args:
            name: Unique surface name.
            surface_type: Wall / Floor / Roof / Ceiling.
            construction_name: Existing Construction name.
            zone_name: Existing Zone name the surface belongs to.
            outside_boundary_condition: Outdoors / Ground / Zone / Adiabatic / Surface.
            vertices: List of vertex dicts in meters. Each vertex is
                      `{"X": float, "Y": float, "Z": float}`. >= 3 points,
                      ordered counter-clockwise when viewed from OUTSIDE.
                      Example 4-vertex south wall (2m tall, 5m wide, at y=0):
                        [{"X": 0.0, "Y": 0.0, "Z": 0.0},
                         {"X": 5.0, "Y": 0.0, "Z": 0.0},
                         {"X": 5.0, "Y": 0.0, "Z": 2.0},
                         {"X": 0.0, "Y": 0.0, "Z": 2.0}]
            sun_exposure: SunExposed / NoSun (use SunExposed for outdoor-facing walls/roof).
            wind_exposure: WindExposed / NoWind.
            outside_boundary_condition_object: Matching surface name when
                                               outside_boundary_condition in {Surface, Zone}.
        """
        return st.create(
            {
                "Name": name,
                "Surface Type": surface_type,
                "Construction Name": construction_name,
                "Zone Name": zone_name,
                "Outside Boundary Condition": outside_boundary_condition,
                "Outside Boundary Condition Object": outside_boundary_condition_object,
                "Sun Exposure": sun_exposure,
                "Wind Exposure": wind_exposure,
                "Vertices": vertices,
            }
        ).model_dump_json()

    @tool
    def create_surfaces_batch(items: list[dict]) -> str:
        """Create multiple BuildingSurface:Detailed objects (walls/floors/
        roofs/ceilings) in a single call.

        Use this instead of looping `create_surface` once per surface — for
        buildings with many surfaces, a long run of sequential single-surface
        tool calls can build a conversation history too large for some
        providers to accept mid-flow. Each item is validated and applied
        independently; a failure on one item does NOT stop the rest of the
        batch (2026-08-13, batch create surfaces — see
        AI_agent/logs/downstream_agent_changes.md).

        Args:
            items: List of dicts. Each dict accepts the same fields as
                `create_surface`: `name`, `surface_type`, `construction_name`,
                `zone_name`, `outside_boundary_condition`, `vertices`
                (all required — same shapes/semantics as `create_surface`'s
                own arguments, including the vertex dict form
                `{"X": float, "Y": float, "Z": float}`), plus optional
                `sun_exposure` (default 'NoSun'), `wind_exposure` (default
                'NoWind'), and `outside_boundary_condition_object`.

        Returns:
            JSON `{"success": bool, "message": str, "data": {"count": N,
            "succeeded": [name, ...], "failed": [{"name": ..., "error": ...},
            ...]}}`. `success` is True only when `failed` is empty. Items
            missing a usable `name` are reported under a placeholder
            `"<item_i>"` in `failed`. A call with more than 4 items is
            rejected outright (nothing is created) with `data.count` still
            reporting how many were sent — split into groups of at most 4
            and call again once per group.
        """
        if len(items) > _MAX_BATCH_ITEMS:
            return ToolResponse(
                success=False,
                message=(
                    f"Batch too large: {len(items)} items "
                    f"(max {_MAX_BATCH_ITEMS} per call). Nothing was "
                    f"created. Split into groups of at most "
                    f"{_MAX_BATCH_ITEMS} and call create_surfaces_batch "
                    f"again once per group."
                ),
                data={"count": len(items), "succeeded": [], "failed": []},
            ).model_dump_json()

        succeeded: list[str] = []
        failed: list[dict] = []

        for i, raw in enumerate(items):
            if not isinstance(raw, dict):
                failed.append({"name": f"<item_{i}>", "error": "item must be a dict"})
                continue
            name = raw.get("name") or f"<item_{i}>"
            record = {
                "Name": raw.get("name"),
                "Surface Type": raw.get("surface_type"),
                "Construction Name": raw.get("construction_name"),
                "Zone Name": raw.get("zone_name"),
                "Outside Boundary Condition": raw.get("outside_boundary_condition"),
                "Outside Boundary Condition Object": raw.get(
                    "outside_boundary_condition_object"
                ),
                "Sun Exposure": raw.get("sun_exposure", "NoSun"),
                "Wind Exposure": raw.get("wind_exposure", "NoWind"),
                "Vertices": raw.get("vertices"),
            }
            r = st.create(record)
            if r.success:
                succeeded.append((r.data or {}).get("name", name))
            else:
                failed.append({"name": name, "error": r.message})

        return ToolResponse(
            success=not failed,
            message=f"Batch create_surface: {len(succeeded)} succeeded, "
            f"{len(failed)} failed.",
            data={
                "count": len(items),
                "succeeded": succeeded,
                "failed": failed,
            },
        ).model_dump_json()

    @tool
    def list_surfaces() -> str:
        """List all building surfaces."""
        return st.list_all().model_dump_json()

    @tool
    def get_surface(name: str) -> str:
        """Read a surface by name."""
        return st.read(name).model_dump_json()

    @tool
    def delete_surface(name: str) -> str:
        """Delete a surface. Fails if fenestration references it."""
        return st.delete(name).model_dump_json()

    @tool
    def list_zones() -> str:
        """Read-only: list zones a surface can be assigned to."""
        return ZoneTool(config).list_all().model_dump_json()

    @tool
    def list_constructions() -> str:
        """Read-only: list constructions a surface can reference."""
        return ConstructionTool(config).list_all().model_dump_json()

    return [
        create_surface,
        create_surfaces_batch,
        list_surfaces,
        get_surface,
        delete_surface,
        list_zones,
        list_constructions,
    ]
