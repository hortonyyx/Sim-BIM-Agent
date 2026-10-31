# 5_intakeoutput — assembly + contract check (deterministic)

Stage **产契约** of the pipeline. Deterministic code, no LLM. Stitches the
deterministic geometry specs (from the kernel serializer, stages 2_modelling +
3_split_pairing) and the non-geometry specs (from stage 4_mep) into one
`IntakeOutput`, then runs a deterministic contract check before it leaves the
project boundary.

Implementation: [`src/agent/intakeoutput.py`](../../../../../src/agent/intakeoutput.py)
(`assemble_intake_output` + `validate_contract`); orchestrated by
[`src/agent/legacy/pipeline.py`](../../../../../src/agent/legacy/pipeline.py) `run_pipeline`.

> **Why this stage has a spec but no LLM prompt:** like 2_modelling / 3_split_pairing
> it is pure deterministic code — this doc is the *spec the code realizes*, not a
> prompt fed to a model. It is the 6th stage that completes the 0–5 numbering.

## Input / Output

- **In**: `zone_specs` / `surface_specs` / `fenestration_specs` (3 geometry strings +
  the `used_constructions` set, from the kernel serializer) + `MepOutput` (the 8
  non-geometry fields from 4_mep).
- **Out**: the 11-field `IntakeOutput` — **the project-side handoff contract** —
  materialized to `5_intakeoutput/intake_output.json`.

## Rules

1. **Mechanical merge only.** Assembly invents no field: geometry 3 + MEP 8 = the 11
   `IntakeOutput` fields, in their exact positions. The contract is unchanged from
   before the staged refactor, so the downstream 9 subagents / cross_ref / validate /
   InterZone gate are untouched.
2. **Contract check (deterministic, fail-fast).** Every construction the geometry
   serializer referenced (`used_constructions`, e.g. `Cons_InterFloor`) must be
   defined in `construction_specs`. Matching is case-sensitive whole-token
   (`Default_Ext_Wall` does not satisfy `Default_Ext_Wall_2`). A miss means 4_mep
   omitted a construction the surfaces need → those surfaces would drop and EnergyPlus
   would fatal; the check **raises by name** here instead, before EP.

## Boundary

This stage owns assembly + the cross-stage contract check only. It does not author
geometry (kernel) or physics (4_mep). The `building` / `site_location` objects come
from 4_mep (non-geometry, from testdata), not from here.
