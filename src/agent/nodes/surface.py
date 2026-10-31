import re

from langchain_core.messages import AIMessage, HumanMessage
from loguru import logger

from src.agent.llm import create_llm
from src.agent.nodes._share import invoke_with_self_repair
from src.agent.react import ReactState, build_react_agent
from src.agent.state import AgentState, AgentStateUpdate
from src.agent.tools import make_surface_tools
from src.agent.trace import TraceCollector, record_phase_trace

SURFACE_SYSTEM_PROMPT = """You are a building geometry expert for EnergyPlus.
Given surface specifications + zone specifications, create all
BuildingSurface:Detailed objects (walls, floors, roofs, ceilings) with
3D vertex polygons.

Vertices MUST be a list of dicts, each with explicit X / Y / Z keys (not
a bare [x, y, z] list). Meters, in the global (world) coordinate system.

## CRITICAL: transcribe vertices verbatim from surface_specs — do NOT recompute them

The user message starts with a `=== ZONE_SPECS ===` block followed by a
`=== SURFACE_SPECS ===` block. Use them this way:

- surface_specs already lists, for every surface, its COMPLETE vertex
  polygon as absolute world-coordinate (X, Y, Z) tuples, already in
  CCW-from-outside order. This is the authoritative geometry. Copy each
  vertex's X, Y, Z straight into the `create_surface` call, IN THE SAME
  ORDER they are listed. Do NOT recompute, re-derive, reorder, round, drop,
  or add vertices. Do NOT use zone_specs' `z_floor` / `ceiling_height` to
  compute a vertex Z value — surface_specs' own Z values are already
  correct and already reflect each zone's floor level and storey height.
- zone_specs is for zone NAMES and adjacency / construction semantics only
  (e.g. confirming which zone a surface belongs to). It is NOT a source of
  vertex coordinates for surface creation.

The only "work" you do on a vertex is copy it — no arithmetic.

## Workflow

1. FIRST call `list_zones` to discover the exact zone names created by
   the zone phase.
2. THEN call `list_constructions` to discover the exact construction
   names and their layer composition (helps you match the right
   construction to each surface type — wall / floor / roof / window).
3. Create surfaces via `create_surfaces_batch`, in SMALL chunks of AT
   MOST 4 SURFACES per call (never more than 4 items in one
   `create_surfaces_batch` call, even within a single zone — if a zone
   has more than 4 surfaces, split that zone's own surfaces across
   multiple calls too). ⚠️ This limit is ENFORCED BY THE TOOL ITSELF — a
   call with more than 4 items is rejected outright (nothing gets
   created, you must resend in smaller groups), so do not try to exceed
   it. Do NOT loop `create_surface` one surface at a time either
   (buildings can have dozens to hundreds of surfaces total, and a long
   run of sequential single-surface calls builds a conversation history
   too large for some providers to accept) — the 4-per-call batches are
   the required middle ground between "one call per surface" (too many
   calls) and "one call for everything" (too much generated content in a
   single turn, which can get cut off mid-call).
   Each `create_surfaces_batch` call's `items` list holds one entry per
   surface, each entry using the same fields `create_surface` takes
   (`name`, `surface_type`, `construction_name`, `zone_name`,
   `outside_boundary_condition`, `vertices`, and optionally
   `sun_exposure`, `wind_exposure`, `outside_boundary_condition_object`),
   reusing zone/construction names verbatim and transcribing each
   surface's own vertex list from surface_specs exactly (same
   coordinates, same order). Use single-item `create_surface` only to
   retry/fix an individual surface reported in `failed`.
   ⚠️ Do NOT send a text-only turn that only announces what you are
   about to do ("I'll now create the surfaces...") — the turn where you
   have zone/construction names available must itself CONTAIN the first
   `create_surfaces_batch` tool call, not a plan to make one later. Keep
   making `create_surfaces_batch` calls, at most 4 surfaces each, until
   every surface in surface_specs has been submitted.
4. Call `list_surfaces` once at the end to confirm all surfaces were
   created; if any are missing, use `create_surface` to add just those.

## Rules

- `zone_name` and `construction_name` MUST appear verbatim in the
  list_zones / list_constructions results (exact case, underscores).
- If a needed zone or construction is missing after list, STOP and
  report; do NOT invent names or create a surface with a broken reference.
- >= 3 vertices per surface; four-vertex rectangles are most common.
- surface_specs' vertex order is already CCW-from-outside — transcribe it
  as given; do NOT re-derive or re-sort the vertex order yourself.
- No two vertices may coincide (tolerance 1e-10 m).
- outside_boundary_condition:
    * Walls/roofs facing outdoors: 'Outdoors',
      sun_exposure='SunExposed', wind_exposure='WindExposed'
    * Floors on ground slab: 'Ground',
      sun_exposure='NoSun', wind_exposure='NoWind'
    * Internal partitions between zones: 'Surface',
      sun_exposure='NoSun', wind_exposure='NoWind',
      and outside_boundary_condition_object must reference the matching
      partner surface in the other zone
    * Adiabatic walls (e.g., between identical thermal zones): 'Adiabatic'
- surface_type is one of Wall, Floor, Roof, Ceiling (case-insensitive).
- Surface names are deterministic public names from surface_specs. Walls use
  the short zone handle plus ring order, e.g. 'Z01_W1'; horizontal faces use
  'Z01_Floor', 'Z01_Ceiling', or 'Z01_Roof' with numeric suffixes for pieces.
  Transcribe them exactly.
"""

# 2026-08-13 (batch create surfaces, structural-safety follow-up): a real
# reproduction against production intake_output.json showed the surface
# node's LLM can announce ("I'll now create the surfaces...") and then emit
# a turn with ZERO tool_calls, which ends the ReAct loop (tools_condition
# routes an AIMessage without tool_calls to END) with 0 surfaces actually
# created and no error raised anywhere — `invoke_with_self_repair`'s
# `validate_references()` check does NOT catch this, because with 0
# surfaces created there is nothing yet for fenestration/hvac/etc. to
# reference inconsistently. The only place this used to surface was the
# very last coordinate-audit gate at the end of the whole downstream graph
# (`VERTEX_FRAME_DRIFT: ... missing from ConfigState`, one line per missing
# surface), with no indication of which phase or why. This regex + check
# make the omission observable right where it happens instead.
_SURFACE_NAME_RE = re.compile(r"^- (\S+) \(", re.MULTILINE)


def _expected_surface_names(surface_specs: str) -> set[str]:
    """Extract the surface names surface_specs declares (bullet lines of
    the form `- <NAME> (...)`, the exact shape `specs.py`'s serializer
    emits). Used only for the post-hoc completeness self-check below — NOT
    a source of geometry and not otherwise consumed."""
    return set(_SURFACE_NAME_RE.findall(surface_specs))


def surface_agent(state: AgentState) -> AgentStateUpdate:
    local = state.config_state.model_copy(deep=True)
    tools = make_surface_tools(local)
    collector = TraceCollector(phase="surface")

    agent = build_react_agent(
        llm=create_llm(node_name="surface"),
        tools=tools,
        system_prompt=SURFACE_SYSTEM_PROMPT,
        trace_collector=collector,
    )

    # 2026-05-12: bundle zone_specs + surface_specs so the agent has zone
    # names/adjacency alongside the surface geometry.
    # 2026-08-06 (F-12): surface_specs already carries each surface's complete
    # absolute-world-coordinate vertex polygon (kernel-computed, CCW-from-
    # outside) — the agent transcribes those verbatim (see SURFACE_SYSTEM_PROMPT
    # "transcribe vertices verbatim from surface_specs"). zone_specs is kept
    # only for zone names / adjacency, NOT as a vertex-Z source anymore: the
    # prior "derive Z from zone_specs" instruction caused the LLM to
    # re-derive wall vertices instead of copying surface_specs' own values,
    # producing a different start-vertex/order than the deterministic kernel
    # emitted (VERTEX_FRAME_DRIFT in src/validator/output_coordinates.py).
    if state.intake_output:
        specs = (
            "=== ZONE_SPECS (zone names / adjacency only — NOT a vertex source) ===\n"
            f"{state.intake_output.zone_specs}\n\n"
            "=== SURFACE_SPECS (authoritative vertices — transcribe verbatim) ===\n"
            f"{state.intake_output.surface_specs}"
        )
    else:
        specs = state.user_input
    result = invoke_with_self_repair(agent, local, specs, phase="surface")

    # Completeness self-check (see module docstring above the regex): give
    # the model exactly one more turn naming precisely what's missing
    # before returning. This does not raise — it mirrors the existing
    # "escalate to the outer validate loop" convention `_share.py` already
    # uses for cross-reference errors, just with a much more specific log
    # line pointing at this phase instead of a downstream coordinate-audit
    # gate discovering it at the very end with no phase attribution.
    if state.intake_output and state.intake_output.surface_specs:
        expected = _expected_surface_names(state.intake_output.surface_specs)
        have = {s.name for s in local.surfaces}
        missing = sorted(expected - have)
        if missing:
            logger.warning(
                "[surface] completeness self-check: {} of {} expected "
                "surfaces missing after first pass ({}{}); issuing one "
                "repair turn",
                len(missing),
                len(expected),
                ", ".join(missing[:10]),
                ", ..." if len(missing) > 10 else "",
            )
            nudge = HumanMessage(
                content=(
                    "Completeness check failed: the following surfaces "
                    "from surface_specs were NOT created (a turn with zero "
                    "tool calls, or a failed item, may have gone "
                    "unaddressed): " + ", ".join(missing) + ". Create ALL "
                    "of these now via create_surfaces_batch (at most 4 "
                    "per call — it will reject larger calls), "
                    "transcribing each one's vertices verbatim from "
                    "surface_specs exactly as before. Do not stop until "
                    "list_surfaces confirms every one of these names "
                    "exists."
                )
            )
            result = agent.invoke(
                ReactState(messages=[*result["messages"], nudge])
            )
            have_after = {s.name for s in local.surfaces}
            still_missing = sorted(expected - have_after)
            if still_missing:
                logger.error(
                    "[surface] completeness self-check FAILED after "
                    "repair turn: {} of {} expected surfaces still "
                    "missing: {}",
                    len(still_missing),
                    len(expected),
                    still_missing,
                )
            else:
                logger.info(
                    "[surface] completeness self-check: repair turn "
                    "created all {} previously-missing surfaces",
                    len(missing),
                )

    final = [
        m for m in result["messages"] if isinstance(m, AIMessage) and not m.tool_calls
    ]
    summary = final[-1].content if final else "surface done"

    record_phase_trace("surface", collector.export())
    return AgentStateUpdate(
        config_state=local,
        messages=[AIMessage(content=f"[surface] {summary}")],
    )
