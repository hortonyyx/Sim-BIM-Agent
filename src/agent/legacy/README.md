# Explicit historical workflows

This package separates the old EnergyPlus orchestration from the current BIM
domain. It is loaded only by an explicitly selected consumer.

- [pipeline.py](pipeline.py): historical vector correction, deterministic geometry,
  physical-property authoring and IntakeOutput assembly.
- [graph.py](graph.py): historical LangGraph EnergyPlus graph.
- [intake](intake/README.md): retained reading/correction/specification/MEP resources.
- [resources.py](resources.py): resource locations used by consumers and diagnostics.

The source modules were moved, not wrapped in a second implementation. Importing
the current product CLI does not load this graph or pipeline. The existing
`src.agent.build_graph` explicit public API resolves here; `runner` and historical
scripts use explicit imports. Shared geometry, schemas, validators and optional
EnergyPlus node/tool implementations remain at their established paths.

Historical serialized stage IDs, run directories, provenance keys and original
evidence remain unchanged. This preserves readable evidence without imposing
that workflow on new tasks. Current model guidance and capability boundaries are
listed in [the domain entry](../README.md).
