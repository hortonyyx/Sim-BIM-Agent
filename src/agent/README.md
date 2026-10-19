# Current BIM domain

This is the building-specific part of the harness. Runtime infrastructure lives
in `src/agent_runtime`; the user entry is `sim_bim.py`, not the historical
EnergyPlus graph. The product model is `work model + runtime + domain`.

| Capability | Source of truth |
|---|---|
| BIM rules and evidence contracts | [contracts](contracts/__init__.py), [source model](geometry/source_bim.py) |
| Deterministic geometry and relationships | [geometry](geometry/__init__.py) |
| Declared curve approximation and courtyard footprints | [source curves](geometry/source_curves.py), [shared parametric templates](geometry/parametric_proposal.py) |
| Original mesh sections and same-view geometry comparison | [sections](geometry/mesh_section.py), [correspondence](geometry/mesh_correspondence.py) |
| Original OPAQUE/BLEND views and scoped pixel measurement | [observation](geometry/mesh_observation.py); contributing fractional alpha has no unique measured surface |
| Model-callable tools | [run_bim_agent.py](../../scripts/tool_scripts/run_bim_agent.py) and its `bim_agent_*` modules |
| Input methods and shared guidance | [bim_agent_guidance.py](../../scripts/tool_scripts/bim_agent_guidance.py) |
| Role definitions, artifacts and role tools | [runtime_roles](runtime_roles/__init__.py) |
| Role guidance | [runtime_roles/guidance.py](runtime_roles/guidance.py) |
| Runtime integration and configuration | [runtime_entry.py](runtime_entry.py), [runtime_configuration.py](runtime_configuration.py) |
| Optional read-only mesh tasks | [dispatch](runtime_mesh_workers.py), [frozen evidence](runtime_mesh_evidence.py); shared Runtime and root budget |
| Viewing and delivery | [bim_delivery.py](geometry/bim_delivery.py), [viewer](../../scripts/tool_scripts/render_geometry_viewer.py) |
| Independent evaluation | [judge](judge/__init__.py); never supplied as work-model answers |
| Local drawing deliveries and architectural review | [drawing readings](../../scripts/tool_scripts/bim_agent_drawing_readings.py), [process reviews](../../scripts/tool_scripts/bim_agent_process_reviews.py); single-model opt-in |

These are responsibilities, not a required sequence or six new wrapper packages.
Methods may compose shared tools; roles are chosen for tasks, not for the three
research categories. Current role division supports drawings. Mesh single-model
configuration accepts an original GLB with optional PNGs and a user declaration;
the coordinator chooses shared tools directly or delegates optional read-only
questions using the same configured model and shared budget. Worker advice binds
actual frozen inputs and an optional source snapshot; only the coordinator edits
the main BIM. Photo generation and automatic detail selection are not implemented.
Simple polygon prisms use the explicit source_polygon_v1 contract with the same
source builder and viewer. Declared circular arcs use a bounded chord approximation
with retained provenance, and floor footprints can contain courtyard holes covered
by actual member spaces. Single-space holes and continuously height-varying/sloped
spaces remain unsupported. Mesh overlays scope arbitrary source hosts and test
source visibility. Original triangle sections and scoped same-ray depth comparisons
provide reproducible geometric evidence, without certifying texture-window accuracy
or whole-building fidelity. The development-only
[readiness check](../../scripts/dev/check_mesh_readiness.py) validates a selected
case's frozen input allowlist, configuration and version seals without model calls.
Schema 2 admits one original GLB, optional PNGs and one JSON declaration with
source/applicability records; path and allowlist checks precede input decoding.

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
