# Current BIM domain

This is the building-specific part of the harness. Runtime infrastructure lives
in `src/agent_runtime`; the user entry is `sim_bim.py`, not the historical
EnergyPlus graph. The product model is `work model + runtime + domain`.

| Capability | Source of truth |
|---|---|
| BIM rules and evidence contracts | [contracts](contracts/__init__.py), [source model](geometry/source_bim.py) |
| Deterministic geometry and relationships | [geometry](geometry/__init__.py) |
| Model-callable tools | [run_bim_agent.py](../../scripts/tool_scripts/run_bim_agent.py) and its `bim_agent_*` modules |
| Input methods and shared guidance | [bim_agent_guidance.py](../../scripts/tool_scripts/bim_agent_guidance.py) |
| Role definitions, artifacts and role tools | [runtime_roles](runtime_roles/__init__.py) |
| Role guidance | [runtime_roles/guidance.py](runtime_roles/guidance.py) |
| Runtime integration and configuration | [runtime_entry.py](runtime_entry.py), [runtime_configuration.py](runtime_configuration.py) |
| Viewing and delivery | [bim_delivery.py](geometry/bim_delivery.py), [viewer](../../scripts/tool_scripts/render_geometry_viewer.py) |
| Independent evaluation | [judge](judge/__init__.py); never supplied as work-model answers |
| Local drawing deliveries and architectural review | [drawing readings](../../scripts/tool_scripts/bim_agent_drawing_readings.py), [process reviews](../../scripts/tool_scripts/bim_agent_process_reviews.py); single-model opt-in |

These are responsibilities, not a required sequence or six new wrapper packages.
Methods may compose shared tools; roles are chosen for tasks, not for the three
research categories. Current role division supports drawings. Mesh single-model
configuration accepts an original GLB with optional PNGs and a user declaration;
the coordinator chooses shared tools directly. Mesh workers, photo generation and
automatic detail selection are not implemented. Simple polygon prisms use the
explicit source_polygon_v1 input contract with the same source builder and viewer;
curves, footprint holes and height-varying spaces remain unsupported. Mesh overlays
can scope arbitrary source hosts and test source visibility, without certifying
alignment to the original mesh.

## Retained code and history

[legacy](legacy/README.md) contains the old pipeline/graph entrypoints and their
reference resources. Persisted stage IDs remain only for that workflow and its
diagnostics. `reading`, `correction`, `execution`, EnergyPlus nodes/tools and
schemas still contain shared or optional consumers: their names do not define
the current task sequence. Do not delete them without inspecting dependencies.

The development repository's
[architecture](https://github.com/hortonyyx/Sim-BIM-Agent/blob/main/AI_agent/design/architecture.md)
and [lineage](https://github.com/hortonyyx/Sim-BIM-Agent/blob/main/AI_agent/design/domain_lineage.md)
explain current boundaries and what was reused, transformed or added. These
development documents are not bundled in the product. New tasks start from the
current tool and role guidance above, never from a staged prompt library.
